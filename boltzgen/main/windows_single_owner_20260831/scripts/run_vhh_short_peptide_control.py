#!/usr/bin/env python3
"""Run one independently registered 9NK9 control: two free folds, <=900 seconds.

Reuses accepted execution/locking/assets checks but never impersonates the old
6JB8 stage gate. No sequence design, retries, training, or downstream GPU launch.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_vhh_short_peptide_control as P
from run_owner_t12_split_template import (RunFailure, atomic_write, compute_processes, json_object,
    locate_acceptance, run_folding, runtime_contract, scan_fatal_logs, sha256_file, utc_now,
    validate_owner, verify_manifest, verify_runtime)
from run_vhh_diversified_pilot import ensure_clean_repository


def binding(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 8*1024**2:
        raise ValueError("small ordinary absolute evidence file required")
    return {"path": str(path.resolve(strict=True)), "sha256": sha256_file(path)}


def bound_json(value):
    if binding(value["path"]) != value:
        raise ValueError("evidence binding changed")
    return json_object(Path(value["path"]))


def check_preparation(prepared):
    receipt = json_object(prepared/"PREPARATION.json")
    if (receipt.get("protocol_id") != P.PROTOCOL_ID or receipt.get("status") != "CPU_READY"
            or receipt.get("target_tokens") != 10 or receipt.get("binder_tokens") != 118
            or receipt.get("fold_samples") != 2 or receipt.get("minimum_recovered_samples_of_two") != 2
            or receipt.get("calibration_thresholds") != P.CALIBRATION_THRESHOLDS
            or receipt.get("implementation_sha256") != P.implementation_hashes()
            or receipt.get("preflight", {}).get("strict_full_canonical_heavy_atom_validation") is not True
            or receipt.get("automatic_retry") is not False or receipt.get("gpu_started") is not False):
        raise ValueError("short-peptide preparation/protocol/source differs")
    registered = datetime.fromisoformat(receipt["prepared_at_utc"].replace("Z", "+00:00"))
    if registered.tzinfo is None or registered > datetime.fromisoformat(utc_now().replace("Z", "+00:00")):
        raise ValueError("preparation must be frozen before launch")
    return receipt, verify_manifest(prepared, prepared/"INPUT_SHA256SUMS")


def validate_calibration_receipt(path):
    """Read-only independent prerequisite: verify both measured folds, not a label."""
    receipt = json_object(Path(path))
    if (receipt.get("schema") != "VHH_SHORT_PEPTIDE_CONTROL_RUN_V1" or receipt.get("status") != "COMPLETED"
            or receipt.get("calibration_status") != "PASSED_TWO_OF_TWO"
            or receipt.get("protocol_id") != P.PROTOCOL_ID or receipt.get("biological_pass") is not False
            or receipt.get("actual", {}).get("fold_samples") != 2
            or receipt.get("actual", {}).get("execution_device") != "cuda"
            or receipt.get("checks", {}).get("weights_unchanged") is not True):
        raise ValueError("short-peptide calibration is not a completed two-fold prerequisite")
    wall = receipt["actual"]["wall_seconds"]
    if isinstance(wall, bool) or not isinstance(wall, (float, int)) or not math.isfinite(wall) or not 0 < wall <= 900:
        raise ValueError("short-peptide wall bound failed")
    prep = bound_json(receipt["preparation"])
    check_preparation(Path(receipt["preparation"]["path"]).parent)
    times = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (
        prep["prepared_at_utc"], receipt["started_at_utc"], receipt["finished_at_utc"])]
    if any(value.tzinfo is None for value in times) or not times[0] <= times[1] <= times[2]:
        raise ValueError("calibration was not registered before execution")
    result = bound_json(receipt["calibration_result"])
    if (result.get("thresholds") != prep["calibration_thresholds"] or result.get("protocol_id") != P.PROTOCOL_ID
            or result.get("strict_full_canonical_heavy_atom_validation") is not True
            or result.get("target_tokens") != 10 or result.get("binder_tokens") != 118
            or [row.get("sample_index") for row in result.get("samples", [])] != [0, 1]):
        raise ValueError("calibration result identity/count/threshold binding differs")
    for row in result["samples"]:
        for key, rule in P.CALIBRATION_THRESHOLDS.items():
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("nonfinite/non-numeric measured calibration metric")
            if not (value <= rule["value"] if rule["direction"] == "max" else value >= rule["value"]):
                raise ValueError("both calibration samples must recover the native interface")
    return receipt


def run(args):
    started, start_utc = time.monotonic(), utc_now()
    workspace, repo, prepared = (p.resolve(strict=True) for p in (args.workspace, args.repo_root, args.prepared))
    output = args.output.absolute()
    ensure_clean_repository(repo)
    if output.exists() or output.is_symlink() or not 136 <= args.hard_timeout_seconds <= 900:
        raise RunFailure("fresh output and bounded 136..900 second total budget required")
    preparation, source_hashes = check_preparation(prepared)
    validate_owner(workspace/"WINDOWS_OWNER_MODE.json")
    _, acceptance = locate_acceptance(workspace)
    python = Path(acceptance["python_bin"])
    if Path(sys.executable).resolve() != python.resolve():
        raise RunFailure("locally accepted Python required")
    runtime = args.runtime_root.resolve(strict=True)
    output.mkdir(parents=True, mode=0o700)
    logs = output/"operator_logs"
    logs.mkdir(mode=0o700)
    receipt = {"schema": "VHH_SHORT_PEPTIDE_CONTROL_RUN_V1", "protocol_id": P.PROTOCOL_ID,
               "status": "FAILED", "calibration_status": "NOT_EVALUATED", "started_at_utc": start_utc,
               "preparation": binding(prepared/"PREPARATION.json"), "automatic_retry": False,
               "biological_pass": False, "checks": {}, "limitations": P.LIMITATIONS}
    before, expected, lock, code = None, None, None, 1
    try:
        lock = os.open(Path(f"/run/user/{os.getuid()}"), os.O_RDONLY)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if compute_processes().strip():
            raise RunFailure("another GPU compute task is active")
        if shutil.disk_usage(output).free < 2*1024**3:
            raise RunFailure("less than 2 GiB free")
        expected = runtime_contract(runtime)
        before = verify_runtime(runtime, expected)
        for name in ("intermediate_designs", "scoring_only"):
            shutil.copytree(prepared/name, output/name)
        config = P.control_config(output/"intermediate_designs", runtime)
        (output/"config").mkdir(mode=0o700)
        atomic_write(output/"config/folding.yaml", yaml.safe_dump(config, sort_keys=False))
        atomic_write(output/"steps.yaml", yaml.safe_dump({"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}))
        # Re-run actual CPU feature/strict atom checks immediately before inference.
        with P.np.load(output/"scoring_only/atom_mapping.npz", allow_pickle=False) as data:
            reference_ca = data["reference_ca"].copy()
        checks = P.preflight(config, output, reference_ca, write_mapping=False)
        receipt["checks"] = {**checks, "weights_unchanged": False}
        remaining = args.hard_timeout_seconds-(time.monotonic()-started)-135
        if remaining <= 1:
            raise RunFailure("wall budget exhausted before GPU launch")
        exit_code, gpu_seconds, timed_out = run_folding(python.parent/"boltzgen-wsl-sm120", output, repo,
            Path(__file__).resolve().parent, logs, remaining)
        receipt.update(folding_exit_code=exit_code, folding_command_wall_seconds=gpu_seconds, timed_out=timed_out)
        if exit_code != 0 or timed_out or scan_fatal_logs(logs):
            raise RunFailure("short-peptide folding failed; no retry")
        result = P.score_outputs(output)
        atomic_write(output/"CALIBRATION_RESULT.json", json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n")
        receipt["calibration_result"] = binding(output/"CALIBRATION_RESULT.json")
        if verify_manifest(prepared, prepared/"INPUT_SHA256SUMS") != source_hashes:
            raise RunFailure("frozen source inputs changed")
        for relative, digest in source_hashes.items():
            if relative.startswith(("intermediate_designs/", "scoring_only/")) and sha256_file(output/relative) != digest:
                raise RunFailure("copied input or scoring reference changed")
        receipt["status"] = "COMPLETED"
        receipt["calibration_status"] = "PASSED_TWO_OF_TWO" if result["native_interface_recovered"] else "NOT_RECOVERED"
        code = 0 if result["native_interface_recovered"] else 2
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if before is not None and expected is not None:
            try:
                after = verify_runtime(runtime, expected)
                receipt["checks"]["weights_unchanged"] = before == after
                receipt.update(runtime_assets_before=before, runtime_assets_after=after)
                if before != after:
                    raise RunFailure("runtime weights changed")
            except Exception as exc:
                receipt["status"], receipt["runtime_verification_error"], code = "FAILED", str(exc), 1
        elapsed = time.monotonic()-started
        if elapsed > args.hard_timeout_seconds:
            receipt["status"], receipt["error"], code = "FAILED", "wall budget exceeded", 1
        receipt.update(finished_at_utc=utc_now(), actual={"fold_samples": 2 if "calibration_result" in receipt else 0,
                       "wall_seconds": elapsed, "execution_device": "cuda"})
        atomic_write(output/"SHORT_PEPTIDE_CONTROL_RUN.json", json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False)+"\n")
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
    print(json.dumps({"status": receipt["status"], "calibration_status": receipt["calibration_status"], "wall_seconds": elapsed}))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "repo-root", "prepared", "output", "runtime-root"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--hard-timeout-seconds", type=int, default=900)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
