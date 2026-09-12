#!/usr/bin/env python3
"""Independently gated same-candidate deletion folding, never an old-gate pass.

Requires actual 9NK9 two-of-two calibration before freezing a new execution
plan. No design, retries, chemistry synthesis or automatic downstream jobs.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import time

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_vhh_matched_pairs as M
import prepare_vhh_same_candidate_pairing_20260913 as P
from run_vhh_short_peptide_control import binding, bound_json, validate_calibration_receipt
from run_vhh_diversified_pilot import ensure_clean_repository
from run_owner_t12_split_template import (RunFailure, atomic_write, compute_processes, json_object,
    locate_acceptance, run_folding, runtime_contract, scan_fatal_logs, sha256_file, utc_now,
    validate_owner, verify_runtime)
from vhh_stage_gate import candidate_set_digest, timestamp

SCHEMA = "VHH_INDEPENDENT_MATCHED_DELETION_EXECUTION_PLAN_20260913_V1"
IMPLEMENTATIONS = ("run_vhh_same_candidate_pairing_20260913.py", "prepare_vhh_same_candidate_pairing_20260913.py",
    "build_vhh_matched_pair_inputs.py", "run_vhh_matched_pairs.py", "evaluate_vhh_epitope.py",
    "prepare_vhh_native_control.py", "run_owner_t12_split_template.py", "vhh_stage_gate.py",
    "run_vhh_short_peptide_control.py", "prepare_vhh_short_peptide_control.py", "run_vhh_diversified_pilot.py")


def implementations():
    return {name: sha256_file(Path(__file__).with_name(name)) for name in IMPLEMENTATIONS}


def check_blueprint(blueprint):
    if (blueprint.get("schema") != "VHH_INDEPENDENT_MATCHED_DELETION_PLAN_20260913_V1"
            or blueprint.get("status") != "INPUTS_READY_FOR_SEPARATE_NEW_BATCH_EXECUTOR"
            or [blueprint.get(k) for k in ("candidate_count", "task_count", "fold_sample_count")] != [6, 12, 24]
            or blueprint.get("old_stage_gate_status") != "BLOCKED_UNCHANGED"
            or blueprint.get("terminal_chemistry_status") != "NOT_ATOMICALLY_VERIFIED"
            or blueprint.get("biological_pass") is not False or blueprint.get("gpu_started") is not False
            or blueprint.get("contact_cutoff_angstrom") != 4.5):
        raise ValueError("new matched-deletion blueprint differs from bounded exploratory contract")
    if timestamp(blueprint["created_at_utc"]) > timestamp(utc_now()):
        raise ValueError("pairing blueprint must predate execution")
    for name, digest in blueprint["implementation_sha256"].items():
        if Path(name).name != name or sha256_file(Path(__file__).with_name(name)) != digest:
            raise ValueError("blueprint implementation changed")
    source = bound_json(blueprint["source_index"])
    if (source.get("status") != "COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY"
            or [c.get("condition_id") for c in source.get("cells", [])] != ["A", "B", "C"]):
        raise ValueError("real completed six-candidate source required")
    preparation, audit = bound_json(blueprint["preparation"]), bound_json(blueprint["cpu_audit"])
    if (audit.get("status") != "CPU_PREPARATION_COMPLETE" or audit.get("gpu_started") is not False
            or audit.get("candidate_set_sha256") != blueprint["candidate_set_sha256"]
            or audit.get("actual_freefold_CPU_tasks_verified") != 12
            or audit.get("source_index") != blueprint["source_index"]
            or audit.get("paired_preparation") != blueprint["preparation"]):
        raise ValueError("actual same-candidate CPU audit missing or mismatched")
    reassessment = bound_json(audit["source_reassessment"])
    if (reassessment.get("candidate_count") != 6 or reassessment.get("fold_sample_count") != 30
            or reassessment.get("strict_all_atom_validation") is not True
            or reassessment.get("partial_fallback_used") is not False):
        raise ValueError("source strict all-atom evaluation incomplete")
    # Content bindings include the actual original 30-fold tensors and receipts.
    for record in reassessment["inputs"]:
        P.T.S.P.read_bound(record["path"], record["sha256"])
    for record in audit["runtime_implementation"].values():
        if binding(record["path"]) != record:
            raise ValueError("audited runtime parser changed")
    if (preparation.get("candidate_count") != 6 or preparation.get("task_count") != 12
            or preparation.get("expected_fold_samples") != 24
            or candidate_set_digest(preparation["candidates"]) != blueprint["candidate_set_sha256"]
            or sorted(c["vhh_sequence_sha256"] for c in preparation["candidates"]) != sorted(c["vhh_sequence_sha256"] for c in reassessment["candidates"])):
        raise ValueError("candidate multiset or task budget changed")
    prepared = Path(blueprint["prepared"])
    if binding(prepared / "MATCHED_PAIR_INPUTS.json") != blueprint["preparation"]:
        raise ValueError("prepared directory does not match the bound preparation")
    M.verify_preparation(prepared, preparation)
    if binding(prepared / "folding.yaml") != blueprint["folding_config"]:
        raise ValueError("folding configuration changed")
    for candidate in preparation["candidates"]:
        P.verify_pair(candidate, prepared)
    return preparation


def prepare(args):
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("execution plan output must be new")
    blueprint = json_object(args.pairing_plan)
    check_blueprint(blueprint)
    calibration = validate_calibration_receipt(args.calibration_receipt)
    if calibration.get("finished_at_utc") and timestamp(calibration["finished_at_utc"]) > timestamp(utc_now()):
        raise ValueError("calibration must be complete before freezing the plan")
    plan = {"schema": SCHEMA, "created_at_utc": utc_now(), "pairing_plan": binding(args.pairing_plan),
        "calibration_receipt": binding(args.calibration_receipt), "implementation_sha256": implementations(),
        "candidate_set_sha256": blueprint["candidate_set_sha256"], "candidate_count": 6, "fold_sample_count": 24,
        "hard_timeout_seconds": 1800, "old_stage_gate_status": "BLOCKED_UNCHANGED",
        "registration": "NEW_USER_AUTHORIZED_BATCH_AFTER_SHORT_PEPTIDE_CALIBRATION",
        "automatic_retry": False, "automatic_downstream_gpu": False, "biological_pass": False,
        "claim_boundary": P.CLAIM, "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED"}
    output.mkdir(parents=True, mode=0o700)
    atomic_write(output / "RUN_PLAN.json", json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"plan": str(output / "RUN_PLAN.json"), "sha256": sha256_file(output / "RUN_PLAN.json"), "gpu_started": False}))
    return 0


def check_plan(plan):
    if (plan.get("schema") != SCHEMA or plan.get("implementation_sha256") != implementations()
            or plan.get("candidate_count") != 6 or plan.get("fold_sample_count") != 24
            or plan.get("hard_timeout_seconds") != 1800
            or plan.get("old_stage_gate_status") != "BLOCKED_UNCHANGED"
            or plan.get("automatic_retry") is not False or plan.get("biological_pass") is not False):
        raise ValueError("independent pairing execution plan changed")
    if timestamp(plan["created_at_utc"]) > timestamp(utc_now()):
        raise ValueError("execution plan must be registered before launch")
    blueprint = bound_json(plan["pairing_plan"])
    preparation = check_blueprint(blueprint)
    bound_json(plan["calibration_receipt"])
    calibration = validate_calibration_receipt(Path(plan["calibration_receipt"]["path"]))
    if plan["candidate_set_sha256"] != preparation["candidate_set_sha256"]:
        raise ValueError("execution candidate identity differs")
    return blueprint, preparation, calibration


def run(args):
    started, started_utc = time.monotonic(), utc_now()
    workspace, repo, runtime = (p.resolve(strict=True) for p in (args.workspace, args.repo_root, args.runtime_root))
    ensure_clean_repository(repo)
    if not 136 <= args.hard_timeout_seconds <= 1800:
        raise RunFailure("total wall budget must be within 136..1800 seconds")
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise RunFailure("fresh separate output required")
    plan = json_object(args.plan)
    blueprint, preparation, calibration = check_plan(plan)
    prepared = Path(blueprint["prepared"])
    if prepared == output or prepared in output.parents:
        raise RunFailure("output may not be inside source preparation")
    expected_config = M.control_config(prepared / "design_inputs", runtime)
    if yaml.safe_load((prepared / "folding.yaml").read_text()) != expected_config:
        raise RunFailure("prepared folding configuration differs from fixed current runtime")
    validate_owner(workspace / "WINDOWS_OWNER_MODE.json")
    _, acceptance = locate_acceptance(workspace)
    python = Path(acceptance["python_bin"])
    if Path(sys.executable).resolve() != python.resolve():
        raise RunFailure("locally accepted Python required")
    output.mkdir(parents=True, mode=0o700)
    logs = output / "operator_logs"
    logs.mkdir(mode=0o700)
    receipt = {"schema": "VHH_INDEPENDENT_MATCHED_DELETION_RUN_20260913_V1", "status": "FAILED",
        "started_at_utc": started_utc, "plan": binding(args.plan), "checks": {},
        "old_stage_gate_status": "BLOCKED_UNCHANGED", "short_peptide_calibration": plan["calibration_receipt"],
        "automatic_retry": False, "biological_pass": False, "claim_boundary": P.CLAIM,
        "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED"}
    before, expected, lock, code, completed = None, None, None, 1, 0
    try:
        lock = os.open(Path(f"/run/user/{os.getuid()}"), os.O_RDONLY)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if compute_processes().strip() or shutil.disk_usage(output).free < 2 * 1024**3:
            raise RunFailure("GPU busy or insufficient free disk")
        expected = runtime_contract(runtime)
        if any(value != preparation["runtime_assets_from_manifest"][name]["sha256_from_runtime_manifest"] for name, value in expected.items()):
            raise RunFailure("runtime assets differ from prepared sources")
        before = verify_runtime(runtime, expected)
        if calibration.get("runtime_assets_after") != before:
            raise RunFailure("pairing must use the same runtime assets as short-peptide calibration")
        shutil.copytree(prepared / "design_inputs", output / "intermediate_designs")
        config = M.control_config(output / "intermediate_designs", runtime)
        atomic_write(logs / "CPU_PREFLIGHT.json", json.dumps(M.preflight(config, preparation), indent=2) + "\n")
        (output / "config").mkdir()
        atomic_write(output / "config/folding.yaml", yaml.safe_dump(config, sort_keys=False))
        atomic_write(output / "steps.yaml", yaml.safe_dump({"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}))
        remaining = args.hard_timeout_seconds - (time.monotonic() - started) - 135
        if remaining <= 1:
            raise RunFailure("wall budget exhausted before GPU launch")
        exit_code, seconds, timed_out = run_folding(python.parent / "boltzgen-wsl-sm120", output, repo, Path(__file__).parent, logs, remaining)
        receipt.update(folding_exit_code=exit_code, folding_seconds=seconds, timed_out=timed_out)
        if exit_code != 0 or timed_out or scan_fatal_logs(logs):
            raise RunFailure("paired folding failed; no retry")
        private, public = M.score_outputs(output, preparation)
        if private["actual_candidate_set_sha256"] != plan["candidate_set_sha256"] or private["fold_sample_count"] != 24:
            raise RunFailure("actual paired results have changed candidate identity or count")
        M.verify_preparation(prepared, preparation)
        for relative, digest in preparation["output_sha256"].items():
            if relative.startswith("design_inputs/") and sha256_file(output / relative.replace("design_inputs/", "intermediate_designs/", 1)) != digest:
                raise RunFailure("copied source changed during folding")
        check_plan(plan)
        receipt["result_artifacts"] = []
        for name, value in (("PRIVATE_MATCHED_PAIR_RESULTS.json", private), ("PUBLIC_MATCHED_PAIR_SUMMARY.json", public)):
            value.update(old_stage_gate_status="BLOCKED_UNCHANGED", source_condition_groups=["A", "B", "C"],
                         nominal_NH2_state_ids_do_not_establish_terminal_amidation=True)
            path = output / name
            atomic_write(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
            receipt["result_artifacts"].append(binding(path))
        receipt["checks"] = {"same_actual_candidate_sequences": True, "strict_outputs_complete": True,
            "all_24_folds_finite_and_mapped": True, "terminal_chemistry_explicitly_unverified": True,
            "short_peptide_two_of_two_calibration": True, "target_only_template": True, "weights_unchanged": False}
        receipt["status"], completed, code = "COMPLETED", 24, 0
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if before is not None:
            try:
                after = verify_runtime(runtime, expected)
                if before != after:
                    raise RunFailure("used weights changed")
                receipt["checks"]["weights_unchanged"] = True
                receipt.update(runtime_assets_before=before, runtime_assets_after=after)
            except Exception as exc:
                receipt["status"], code, receipt["runtime_verification_error"] = "FAILED", 1, str(exc)
        elapsed = time.monotonic() - started
        if elapsed > args.hard_timeout_seconds:
            receipt["status"], code, receipt["wall_budget_exceeded"] = "FAILED", 1, True
        receipt["finished_at_utc"] = utc_now()
        receipt["actual"] = {"candidates": 6, "fold_samples": completed, "wall_seconds": elapsed, "execution_device": "cuda"}
        atomic_write(output / "MATCHED_PAIR_RUN.json", json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n")
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
    print(json.dumps({"status": receipt["status"], "fold_samples": completed, "wall_seconds": elapsed, "error": receipt.get("error")}))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    p = actions.add_parser("prepare")
    for name in ("pairing-plan", "calibration-receipt", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    r = actions.add_parser("run")
    for name in ("plan", "workspace", "repo-root", "runtime-root", "output"):
        r.add_argument("--" + name, type=Path, required=True)
    r.add_argument("--hard-timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    return prepare(args) if args.action == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
