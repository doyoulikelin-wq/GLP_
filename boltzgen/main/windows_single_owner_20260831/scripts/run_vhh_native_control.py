#!/usr/bin/env python3
"""Run one 2-sample native VHH control, with <=15 minute GPU wall budget.

The control does not design sequences, load a native complex template, train,
run affinity prediction or claim prospective binding. Raw outputs stay private.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_vhh_native_control import (CALIBRATION_THRESHOLDS, CONTROL_ID,
                                       aligned_binder_rmsd, control_config, heavy_residue_contacts)
from run_owner_t12_split_template import (RunFailure, atomic_write, compute_processes,
    json_object, locate_acceptance, run_folding, runtime_contract, scan_fatal_logs,
    sha256_file, utc_now, validate_owner, verify_manifest, verify_runtime)
from vhh_stage_gate import evaluate as evaluate_stage_gate


def canonical_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def require_native_ready(contract: dict, bindings: dict, prerequisite: dict) -> dict:
    gate = evaluate_stage_gate(contract, bindings, [prerequisite])
    if gate.get("status") != "READY" or gate.get("next_stage") != "native_free_refold":
        raise RunFailure("CPU prerequisite gate does not permit native_free_refold")
    return gate


def score_outputs(attempt: Path) -> dict:
    design_dir = attempt / "intermediate_designs"
    outputs = list((design_dir / "fold_out_npz").glob("*.npz"))
    cif_outputs = list((design_dir / "refold_cif").glob("*.cif"))
    if [p.name for p in outputs] != [f"{CONTROL_ID}.npz"] or [p.name for p in cif_outputs] != [f"{CONTROL_ID}.cif"]:
        raise RunFailure("native control output closure mismatch")
    if cif_outputs[0].stat().st_size == 0:
        raise RunFailure("empty native control CIF")
    with np.load(attempt / "scoring_only" / "atom_mapping.npz", allow_pickle=False) as mapping, np.load(outputs[0], allow_pickle=False) as fold:
        coords = np.asarray(fold["coords"])
        atom_to_token = np.asarray(fold["atom_to_token"])[0]
        expected_map = np.asarray(mapping["atom_to_token"])
        pad = np.asarray(mapping["atom_pad_mask"])
        if coords.shape != (2, len(pad), 3) or not np.isfinite(coords).all():
            raise RunFailure("native control sample shape or finite coordinate check failed")
        if not np.array_equal(atom_to_token, expected_map):
            raise RunFailure("native control atom mapping changed")
        for metric in ("iptm", "ptm", "design_to_target_iptm", "design_ptm"):
            if np.asarray(fold[metric]).shape != (2,) or not np.isfinite(fold[metric]).all():
                raise RunFailure(f"invalid confidence metric: {metric}")
        ca = np.asarray(mapping["ca_atom_indices"])
        reference = np.asarray(mapping["reference_ca"])
        token = expected_map.argmax(axis=1)
        heavy = np.asarray(mapping["contact_heavy_mask"])
        native_pairs = {tuple(map(int, row)) for row in mapping["native_residue_contact_pairs"]}
        if not native_pairs:
            raise RunFailure("empty native contact reference")
        samples = []
        for index, coordinates in enumerate(coords):
            pairs = heavy_residue_contacts(coordinates, token, heavy, 129)
            samples.append({"sample_index": index,
                "target_aligned_binder_ca_rmsd_angstrom": aligned_binder_rmsd(coordinates[ca], reference, 129),
                "native_heavy_residue_contact_recall": len(native_pairs & pairs) / len(native_pairs),
                "native_heavy_residue_contact_count": len(native_pairs),
                "predicted_heavy_residue_contact_count": len(pairs),
                "recovered_native_heavy_residue_contact_count": len(native_pairs & pairs),
                "iptm": float(fold["iptm"][index])})
    recovered = all(all(row[key] <= rule["value"] if rule["direction"] == "max" else row[key] >= rule["value"]
                        for key, rule in CALIBRATION_THRESHOLDS.items()) for row in samples)
    return {"thresholds": CALIBRATION_THRESHOLDS, "samples": samples,
            "native_interface_recovered": recovered,
            "contact_cutoff_angstrom": 4.5,
            "interpretation": "two-sample known-complex coarse geometry check, not DockQ or GLP1 binding evidence"}


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    started_utc = utc_now()
    workspace, repo = args.workspace.resolve(strict=True), args.repo_root.resolve(strict=True)
    prepared, attempt = args.prepared.resolve(strict=True), args.output.absolute()
    if attempt.exists() or attempt.is_symlink() or not 1 <= args.hard_timeout_seconds <= 900:
        raise RunFailure("fresh output and timeout in 1..900 seconds required")
    contract, bindings = json_object(args.contract), json_object(args.bindings)
    prerequisite = json_object(args.prerequisite_receipt)
    prior_gate = require_native_ready(contract, bindings, prerequisite)
    binding = bindings["native_free_refold"]
    if binding.get("calibration_thresholds") != CALIBRATION_THRESHOLDS:
        raise RunFailure("bound native calibration thresholds differ from runner")
    preparation = json_object(prepared / "PREPARATION.json")
    preparation_sha = sha256_file(prepared / "PREPARATION.json")
    if binding.get("preparation_sha256") != preparation_sha:
        raise RunFailure("native input binding does not identify this preparation")
    preregistered = binding["preregistered_at_utc"]
    frozen_time = datetime.fromisoformat(preregistered.replace("Z", "+00:00"))
    if frozen_time.tzinfo is None or frozen_time > datetime.fromisoformat(started_utc.replace("Z", "+00:00")):
        raise RunFailure("native preregistration timestamp must precede launch and include timezone")
    native_stage = next(stage for stage in contract["stages"] if stage["id"] == "native_free_refold")
    if contract.get("protocol_id") != "vhh-revision-20260909" or native_stage["calibration_thresholds"] != CALIBRATION_THRESHOLDS:
        raise RunFailure("native protocol/thresholds mismatch")
    if preparation.get("status") != "CPU_READY" or preparation["diagnostic_rule"]["thresholds"] != CALIBRATION_THRESHOLDS:
        raise RunFailure("current CPU preparation/threshold contract missing")
    if preparation["diagnostic_rule"]["minimum_recovered_samples_of_two"] != 2:
        raise RunFailure("both native samples must satisfy diagnostic recovery")
    validate_owner(workspace / "WINDOWS_OWNER_MODE.json")
    source_hashes = verify_manifest(prepared, prepared / "INPUT_SHA256SUMS")
    _, acceptance = locate_acceptance(workspace)
    python_bin = Path(acceptance["python_bin"])
    if Path(sys.executable).resolve() != python_bin.resolve():
        raise RunFailure("must use locally accepted Python")
    runtime = args.runtime_root.resolve(strict=True)
    launcher = python_bin.parent / "boltzgen-wsl-sm120"
    attempt.mkdir(parents=True, mode=0o700)
    logs = attempt / "operator_logs"
    logs.mkdir(mode=0o700)
    atomic_write(logs / "PREREQUISITE_GATE.json", json.dumps(prior_gate, indent=2, sort_keys=True) + "\n")
    receipt = {"schema": "VHH_STAGE_RECEIPT_V1", "stage_id": "native_free_refold", "status": "FAILED",
        "evidence_kind": "native_complex_free_refold_gpu", "registration": "PROSPECTIVE",
        "protocol_id": "vhh-revision-20260909", "protocol_sha256": canonical_digest(contract),
        "input_binding_sha256": canonical_digest(binding), "created_at_utc": started_utc,
        "preregistered_at_utc": preregistered, "started_at_utc": started_utc,
        "biological_pass": False, "checks": {},
        "claim_boundary": "known-complex native control can have training-set overlap; not GLP1 or prospective validation",
        "source_preparation_sha256": preparation_sha,
        "prerequisite_receipt_sha256": sha256_file(args.prerequisite_receipt)}
    before, expected, lock, code = None, None, None, 1
    try:
        lock = os.open(Path(f"/run/user/{os.getuid()}"), os.O_RDONLY)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if compute_processes().strip():
            raise RunFailure("another GPU compute task is active")
        if shutil.disk_usage(attempt).free < 2 * 1024**3:
            raise RunFailure("less than 2GiB disk free")
        expected = runtime_contract(runtime)
        before = verify_runtime(runtime, expected)
        for name in ("intermediate_designs", "scoring_only"):
            shutil.copytree(prepared / name, attempt / name)
        config = control_config(attempt / "intermediate_designs", runtime)
        (attempt / "config").mkdir(mode=0o700)
        atomic_write(attempt / "config" / "folding.yaml", yaml.safe_dump(config, sort_keys=False))
        atomic_write(attempt / "steps.yaml", yaml.safe_dump({"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}))
        atomic_write(logs / "runtime_before.json", json.dumps(before, indent=2) + "\n")
        # Existing launcher permits up to90s group termination; reserve that and
        # post-run single asset verification rather than exceeding the wall cap.
        remaining = args.hard_timeout_seconds - (time.monotonic() - started) - 135
        if remaining <= 1:
            raise RunFailure("wall budget exhausted before GPU launch")
        print(f"NATIVE_CONTROL_GPU_START samples=2 remaining_seconds={remaining:.1f}", flush=True)
        exit_code, gpu_seconds, timed_out = run_folding(launcher, attempt, repo,
                                                     Path(__file__).resolve().parent, logs, remaining)
        receipt.update({"folding_exit_code": exit_code, "folding_seconds": gpu_seconds, "timed_out": timed_out})
        if exit_code != 0 or scan_fatal_logs(logs):
            raise RunFailure("native folding failed; inspect private logs")
        calibration = score_outputs(attempt)
        receipt["calibration"] = calibration
        calibration_path = attempt / "CALIBRATION_RESULT.json"
        atomic_write(calibration_path, json.dumps(calibration, indent=2, sort_keys=True, allow_nan=False) + "\n")
        receipt["result_artifacts"] = [{"path": str(calibration_path), "sha256": sha256_file(calibration_path)}]
        if verify_manifest(prepared, prepared / "INPUT_SHA256SUMS") != source_hashes:
            raise RunFailure("native source preparation changed")
        for relative, digest in source_hashes.items():
            if relative.startswith("intermediate_designs/") or relative.startswith("scoring_only/"):
                if sha256_file(attempt / relative) != digest:
                    raise RunFailure("copied native scoring/folding input changed")
        receipt["checks"] = {"source_identity_valid": True, "atom_mapping_valid": True,
            "finite_coordinates": True, "outputs_complete": True, "thresholds_frozen_before_run": True,
            "no_native_cross_chain_template": True, "free_evaluation_configuration_verified": True,
            "native_interface_recovered": calibration["native_interface_recovered"],
            "no_untrained_conditioning_switch": True, "weights_unchanged": False}
        receipt["status"] = "COMPLETED"
        code = 0
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        print(receipt["error"], file=sys.stderr, flush=True)
    finally:
        if before is not None and expected is not None:
            try:
                after = verify_runtime(runtime, expected)
                receipt["checks"]["weights_unchanged"] = after == before
                receipt["runtime_assets_before"] = before
                receipt["runtime_assets_after"] = after
            except Exception as exc:
                receipt["status"], code = "FAILED", 1
                receipt["runtime_verification_error"] = str(exc)
        elapsed = time.monotonic() - started
        if elapsed > args.hard_timeout_seconds:
            receipt["status"], code = "FAILED", 1
            receipt["wall_budget_exceeded"] = True
        receipt["actual"] = {"fold_samples": 2 if "calibration" in receipt else 0,
                             "wall_seconds": elapsed, "execution_device": "cuda"}
        receipt["finished_at_utc"] = utc_now()
        receipt["created_at_utc"] = receipt["finished_at_utc"]
        atomic_write(attempt / "NATIVE_CONTROL_RUN.json", json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n")
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
    print(json.dumps({"status": receipt["status"], "wall_seconds": receipt["actual"]["wall_seconds"],
                      "native_interface_recovered": receipt["checks"].get("native_interface_recovered", False)}))
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "repo-root", "prepared", "output", "runtime-root", "contract", "bindings", "prerequisite-receipt"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--hard-timeout-seconds", type=int, default=900)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
