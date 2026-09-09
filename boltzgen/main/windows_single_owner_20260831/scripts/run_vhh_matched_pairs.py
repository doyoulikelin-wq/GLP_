#!/usr/bin/env python3
"""Run a bounded, input-bound GLP-1 active/deletion paired free-refold stage."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as geometry
from build_vhh_matched_pair_inputs import (ACTIVE_GLP1, MAX_CANDIDATES, sequence_digest,
    load_structure, select_candidate_chains, chain_sequence)
from prepare_vhh_native_control import control_config
from run_owner_t12_split_template import (RunFailure, atomic_write, compute_processes,
    json_object, locate_acceptance, run_folding, runtime_contract, scan_fatal_logs,
    sha256_file, utc_now, validate_owner, verify_runtime)
from vhh_stage_gate import candidate_set_digest, digest, evaluate, timestamp

STAGE = "active_truncated_pairing"
STATE_IDS = ["GLP1_7_36_NH2", "GLP1_9_36_NH2"]
ONE_LETTER = "ARNDCQEGHILKMFPSTWYV"
CLAIM = "MATCHED_GEOMETRIC_DELETION_ONLY_NOT_BINDING_AFFINITY_OR_SELECTIVITY"


def require_ready(contract, bindings, prerequisites, preparation):
    gate = evaluate(contract, bindings, prerequisites)
    if gate["status"] != "READY" or gate["next_stage"] != STAGE:
        raise RunFailure("stage gate does not permit active_truncated_pairing")
    if preparation.get("status") != "INPUTS_PREPARED_GPU_NOT_RUN":
        raise RunFailure("prepared inputs must be complete and not reused")
    actual = next(row["actual_candidate_set_sha256"] for row in prerequisites if row["stage_id"] == "diversified_pilot")
    if bindings[STAGE].get("candidate_set_sha256") != actual or preparation.get("candidate_set_sha256") != actual:
        raise RunFailure("matched inputs do not identify completed pilot actual output sequences")
    if candidate_set_digest(preparation["candidates"]) != actual:
        raise RunFailure("prepared candidate sequence identities mismatch")
    n = preparation.get("candidate_count")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_CANDIDATES:
        raise RunFailure("matched runner supports 1..6 unique candidates")
    if len(preparation["candidates"]) != n or preparation.get("task_count") != 2*n or preparation.get("expected_fold_samples") != 4*n:
        raise RunFailure("matched input sample counts inconsistent")
    if preparation.get("terminal_chemistry_status") != "NOT_ATOMICALLY_VERIFIED":
        raise RunFailure("this builder/runner only supports explicit geometry-only terminal chemistry")
    tasks = [state["task_id"] for candidate in preparation["candidates"] for state in candidate["states"]]
    if len(tasks) != len(set(tasks)) or any(Path(task).name != task or "/" in task or "\\" in task for task in tasks):
        raise RunFailure("unsafe or duplicate task IDs")
    for candidate in preparation["candidates"]:
        if [state["state_id"] for state in candidate["states"]] != STATE_IDS:
            raise RunFailure("both ordered primary target states required")
        for state, count in zip(candidate["states"], (30, 28)):
            if state["target_token_count"] != count or state["folds"] != 2 or state["vhh_sequence_sha256"] != candidate["vhh_sequence_sha256"]:
                raise RunFailure("state target length, sample count or candidate identity mismatch")
    return gate


def verify_preparation(prepared, preparation):
    expected = {"folding.yaml"} | {f"design_inputs/{state['task_id']}{suffix}" for candidate in preparation["candidates"] for state in candidate["states"] for suffix in (".cif", ".npz")}
    if set(preparation["output_sha256"]) != expected:
        raise RunFailure("prepared input hash closure mismatch")
    for relative, expected_hash in preparation["output_sha256"].items():
        path = prepared / relative
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected_hash:
            raise RunFailure("prepared file hash mismatch")


def preflight(config, preparation):
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    tasks = {state["task_id"]: state for candidate in preparation["candidates"] for state in candidate["states"]}
    module = instantiate(OmegaConf.create(config["data"]))
    if len(module.predict_set) != len(tasks):
        raise RunFailure("free-refold CPU feature task count mismatch")
    seen = set()
    for index in range(len(module.predict_set)):
        sample = module.predict_set[index]
        task = str(sample["id"])
        if task not in tasks or task in seen:
            raise RunFailure("free-refold CPU feature task identity mismatch")
        state = tasks[task]
        count, n = state["target_token_count"], state["target_token_count"] + state["vhh_token_count"]
        mask = sample["template_mask"].numpy()
        if mask.shape != (1, n) or not np.all(mask[0, :count] == 1) or np.any(mask[0, count:]):
            raise RunFailure("free evaluation leaked binder template or lost target mask")
        if np.flatnonzero(sample["design_mask"].numpy()).tolist() != state["designed_token_indices"]:
            raise RunFailure("CPU feature CDR mask differs from prepared metadata")
        seen.add(task)
    return {"task_count": len(seen), "target_only_template": True}


def score_outputs(attempt, preparation):
    root = attempt / "intermediate_designs"
    tasks = {state["task_id"] for candidate in preparation["candidates"] for state in candidate["states"]}
    for directory, suffix in (("fold_out_npz", ".npz"), ("refold_cif", ".cif")):
        files = list((root / directory).glob(f"*{suffix}"))
        if {path.stem for path in files} != tasks or any(path.is_symlink() or path.stat().st_size == 0 for path in files):
            raise RunFailure("fold output closure/empty-file check failed")
    candidates = []
    for candidate in preparation["candidates"]:
        states = []
        for state in candidate["states"]:
            task, count = state["task_id"], state["target_token_count"]
            design, source = geometry._load_npz(root / f"{task}.npz")
            fold, folded = geometry._load_npz(root / "fold_out_npz" / f"{task}.npz")
            rows = geometry.evaluate_arrays(design, fold, target_tokens=range(count),
                                            his_token=0 if count == 30 else None, ala_token=1 if count == 30 else None)
            if len(rows) != 2:
                raise RunFailure("each candidate/state requires exactly two folds")
            sequence = "".join(ONE_LETTER[int(i)-2] for i in fold["res_type"][0].argmax(axis=1))
            if sequence[:count] != (ACTIVE_GLP1 if count == 30 else ACTIVE_GLP1[2:]) or sequence_digest(sequence[count:]) != candidate["vhh_sequence_sha256"]:
                raise RunFailure("folded candidate/target sequence differs from prepared identity")
            _, target_residues, _, binder_residues = select_candidate_chains(load_structure(root / "refold_cif" / f"{task}.cif"))
            if chain_sequence(target_residues)+chain_sequence(binder_residues) != sequence:
                raise RunFailure("refolded CIF sequence differs from fold NPZ")
            for metric in ("iptm", "ptm", "design_to_target_iptm", "design_ptm"):
                values = np.asarray(fold[metric])
                if values.shape != (2,) or not np.isfinite(values).all():
                    raise RunFailure("invalid confidence metric values")
            for row in rows:
                row["shared_segment_contact_pairs"] = [[t+(7 if count == 30 else 9), v-count+1] for t,v in row["contact_residue_pairs_zero_based"] if t+(7 if count == 30 else 9) >= 9]
            states.append({"state_id": state["state_id"], "target_token_count": count,
                           "epitope_status": "HIS7_ALA8_PRESENT" if count == 30 else "ABSENT_NOT_APPLICABLE",
                           "summary": geometry.candidate_summary(rows), "samples": rows,
                           "shared_segment_contact_pair_count": geometry._summary([len(row["shared_segment_contact_pairs"]) for row in rows]),
                           "confidence_descriptive_only": {metric: geometry._summary(fold[metric]) for metric in ("iptm", "ptm", "design_to_target_iptm", "design_ptm")},
                           "source_files": [source, folded]})
        cross = []
        for left in states[0]["samples"]:
            for right in states[1]["samples"]:
                a,b = {tuple(p) for p in left["shared_segment_contact_pairs"]},{tuple(p) for p in right["shared_segment_contact_pairs"]}
                if a|b:
                    cross.append(len(a&b)/len(a|b))
        candidates.append({"candidate_id": candidate["candidate_id"], "vhh_sequence_sha256": candidate["vhh_sequence_sha256"],
                           "states": states, "shared_segment_cross_state_jaccard": geometry._summary(cross),
                           "shared_segment_contact_change_truncated_minus_active": states[1]["shared_segment_contact_pair_count"]["median"]-states[0]["shared_segment_contact_pair_count"]["median"]})
    common = {"schema": "VHH_MATCHED_PAIR_RESULTS_V1", "status": "COMPUTATIONAL_PAIRING_COMPLETE", "candidate_count": len(candidates),
              "fold_sample_count": len(candidates)*4, "target_states": STATE_IDS, "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED",
              "claim_boundary": CLAIM, "biological_pass": False, "contact_cutoff_angstrom": 4.5,
              "source_group_rule": "two matched deletion states from the same active geometry per candidate; not independent natural conformers",
              "repeat_rule": "candidate paired; repeats not seed-paired; all four cross-state map comparisons descriptive"}
    public = {**common, "state_aggregates": [{"state_id": state_id,
              "candidate_all_two_folds_contact_count": sum(c["states"][index]["summary"]["counts"]["any_target_cdr_contact"] == 2 for c in candidates),
              "candidate_median_shared_contact_pair_distribution": geometry._summary([c["states"][index]["shared_segment_contact_pair_count"]["median"] for c in candidates])} for index,state_id in enumerate(STATE_IDS)],
              "active_candidate_all_two_folds_both_terminal_contacts": sum(c["states"][0]["summary"]["counts"]["both_epitope_contacts"] == 2 for c in candidates),
              "truncated_his_ala_contact_status": "ABSENT_NOT_APPLICABLE_NOT_ZERO_MEASUREMENT",
              "candidate_shared_contact_change_distribution": geometry._summary([c["shared_segment_contact_change_truncated_minus_active"] for c in candidates]),
              "limitations": ["No raw iPTM difference is interpreted as selectivity.", "New N-terminus and C-terminal amide chemistry not atomically verified.", "Contact distance and close-overlap indicators are not affinity or atom-type-aware clash measurements."]}
    return {**common, "actual_candidate_set_sha256": candidate_set_digest(candidates), "candidates": candidates}, public


def run(args):
    started, started_utc = time.monotonic(), utc_now()
    workspace, prepared, runtime = args.workspace.resolve(strict=True), args.prepared.resolve(strict=True), args.runtime_root.resolve(strict=True)
    attempt = args.output.resolve(strict=False)
    if attempt.exists() or not 1 <= args.hard_timeout_seconds <= 1800 or prepared in attempt.parents:
        raise RunFailure("require a fresh separate output and timeout<=1800 seconds")
    contract, bindings = json_object(args.contract), json_object(args.bindings)
    prerequisites, preparation = [json_object(p) for p in args.prerequisite_receipts], json_object(prepared / "MATCHED_PAIR_INPUTS.json")
    gate = require_ready(contract, bindings, prerequisites, preparation)
    binding = bindings[STAGE]
    if binding.get("preparation_sha256") != sha256_file(prepared / "MATCHED_PAIR_INPUTS.json") or binding.get("contact_cutoff_angstrom") != 4.5:
        raise RunFailure("paired preparation/metric definition not bound")
    if binding.get("runner_sha256") != sha256_file(Path(__file__)) or binding.get("evaluator_sha256") != sha256_file(Path(geometry.__file__)):
        raise RunFailure("bound runner/evaluator code changed")
    if timestamp(binding["preregistered_at_utc"]) > timestamp(started_utc):
        raise RunFailure("pairing rules must be frozen before launch")
    validate_owner(workspace / "WINDOWS_OWNER_MODE.json")
    verify_preparation(prepared, preparation)
    if yaml.safe_load((prepared / "folding.yaml").read_text()) != control_config(prepared / "design_inputs", runtime):
        raise RunFailure("prepared folding configuration changed")
    _, acceptance = locate_acceptance(workspace)
    python = Path(acceptance["python_bin"])
    if Path(sys.executable).resolve() != python.resolve():
        raise RunFailure("locally accepted Python required")
    attempt.mkdir(parents=True, mode=0o700)
    logs = attempt / "operator_logs"; logs.mkdir(mode=0o700)
    atomic_write(logs / "PREREQUISITE_GATE.json", json.dumps(gate, indent=2)+"\n")
    receipt = {"schema": "VHH_STAGE_RECEIPT_V1", "stage_id": STAGE, "status": "FAILED", "protocol_id": contract["protocol_id"],
               "protocol_sha256": digest(contract), "input_binding_sha256": digest(binding), "registration": "PROSPECTIVE",
               "evidence_kind": "matched_active_truncated_free_refold_gpu", "preregistered_at_utc": binding["preregistered_at_utc"],
               "started_at_utc": started_utc, "checks": {}, "biological_pass": False, "claim_boundary": CLAIM,
               "target_states": STATE_IDS, "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED"}
    before, expected, lock, code, completed = None, None, None, 1, 0
    try:
        lock = os.open(Path(f"/run/user/{os.getuid()}"), os.O_RDONLY)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if compute_processes().strip() or shutil.disk_usage(attempt).free < 2*1024**3:
            raise RunFailure("GPU busy or less than 2GiB disk free")
        expected = runtime_contract(runtime)
        if any(value != preparation["runtime_assets_from_manifest"][name]["sha256_from_runtime_manifest"] for name,value in expected.items()):
            raise RunFailure("runtime assets differ from prepared input binding")
        before = verify_runtime(runtime, expected)
        shutil.copytree(prepared / "design_inputs", attempt / "intermediate_designs")
        config = control_config(attempt / "intermediate_designs", runtime)
        atomic_write(logs / "CPU_PREFLIGHT.json", json.dumps(preflight(config, preparation), indent=2)+"\n")
        (attempt / "config").mkdir()
        atomic_write(attempt / "config/folding.yaml", yaml.safe_dump(config, sort_keys=False))
        atomic_write(attempt / "steps.yaml", yaml.safe_dump({"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}))
        remaining = args.hard_timeout_seconds - (time.monotonic()-started) - 135
        if remaining <= 1:
            raise RunFailure("wall budget exhausted before GPU launch")
        exit_code, seconds, timed_out = run_folding(python.parent / "boltzgen-wsl-sm120", attempt, args.repo_root.resolve(strict=True), Path(__file__).parent, logs, remaining)
        receipt.update(folding_exit_code=exit_code, folding_seconds=seconds, timed_out=timed_out)
        if exit_code != 0 or timed_out or scan_fatal_logs(logs):
            raise RunFailure("paired free-refolding failed; inspect private logs")
        private, public = score_outputs(attempt, preparation)
        if private["actual_candidate_set_sha256"] != binding["candidate_set_sha256"]:
            raise RunFailure("output sequence set differs from completed pilot")
        verify_preparation(prepared, preparation)
        for relative, hash_value in preparation["output_sha256"].items():
            if relative.startswith("design_inputs/") and sha256_file(attempt / relative.replace("design_inputs/", "intermediate_designs/", 1)) != hash_value:
                raise RunFailure("copied input changed during folding")
        receipt["result_artifacts"] = []
        for name,result in (("PRIVATE_MATCHED_PAIR_RESULTS.json", private), ("PUBLIC_MATCHED_PAIR_SUMMARY.json", public)):
            path = attempt / name; atomic_write(path, json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n")
            receipt["result_artifacts"].append({"path": str(path), "sha256": sha256_file(path)})
        receipt["checks"] = {"source_identity_valid": True, "atom_mapping_valid": True,
            "finite_coordinates": True, "outputs_complete": True, "thresholds_frozen_before_run": True,
            "candidate_sequences_match_pilot": True, "matched_inference_settings": True,
            "terminal_chemistry_status_explicit": True, "source_group_balance_reported": True,
            "candidate_repeat_summaries_reported": True, "no_raw_iptm_selectivity_claim": True,
            "no_native_cross_chain_template": True, "weights_unchanged": False}
        completed, code, receipt["status"] = private["fold_sample_count"], 0, "COMPLETED"
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        print(receipt["error"], file=sys.stderr, flush=True)
    finally:
        if before is not None:
            try:
                after = verify_runtime(runtime, expected)
                receipt["checks"]["weights_unchanged"] = before == after
                receipt.update(runtime_assets_before=before, runtime_assets_after=after)
            except Exception as exc:
                receipt["status"], code, receipt["runtime_verification_error"] = "FAILED", 1, str(exc)
        elapsed = time.monotonic()-started
        if elapsed > args.hard_timeout_seconds:
            receipt["status"], code, receipt["wall_budget_exceeded"] = "FAILED", 1, True
        receipt["created_at_utc"] = utc_now()
        receipt["actual"] = {"candidates": preparation["candidate_count"], "fold_samples": completed, "wall_seconds": elapsed, "execution_device": "cuda"}
        atomic_write(attempt / "MATCHED_PAIR_RUN.json", json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False)+"\n")
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN); os.close(lock)
    print(json.dumps({"status": receipt["status"], "fold_samples": completed, "wall_seconds": elapsed, "biological_pass": False}))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "repo-root", "prepared", "output", "runtime-root", "contract", "bindings"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--prerequisite-receipts", type=Path, nargs="+", required=True)
    parser.add_argument("--hard-timeout-seconds", type=int, default=1800)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
