#!/usr/bin/env python3
"""Freeze/run the independent 20260910 three-framework exploratory experiment.

Reuses the accepted legacy execution safety wrapper, not the failed 20260909
pilot's scientific gate. Small CPU evidence and native calibration must be bound
before launch. Outputs/logs remain in a fresh private directory, never reused.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_owner_t12_split_template import json_object, sha256_file, utc_now, parse_manifest, verify_runtime
from run_vhh_diversified_pilot import bounded_cell, ensure_clean_repository, spec_record
from vhh_stage_gate import digest, timestamp, validate_artifacts, validate_receipt

RULE = {"experiment_id": "vhh-controlled-expansion-20260910",
    "scaffolds": ["01_pdb_00007xl0-A", "02_pdb_00006apo-A", "09_pdb_00008im0-B"],
    "candidates_per_scaffold": 2, "free_folds_per_candidate": 5,
    "hard_timeout_seconds": 1800, "checkpoint": "adherence",
    "matched_framework_comparison": ["01_pdb_00007xl0-A", "09_pdb_00008im0-B"],
    "matched_cdr_lengths": [8, 7, 15], "automatic_retry": False,
    "primary_descriptors": ["generated_his_ala_contact", "free_fold_his_ala_contact", "within_candidate_repeat_consistency"],
    "pose_class_count_role": "DESCRIPTIVE_NOT_REQUIRED_SUCCESS_GATE",
    "binding_condition_role": "TRAINED_SOFT_CONDITION_NOT_HARD_CONTACT_CONSTRAINT",
    "old_pilot_status": "BLOCKED_UNCHANGED", "biological_pass": False}
CPU_CHECKS = ("scaffold_library_screened", "new_framework_identity_distinct",
    "matched_cdr_lengths_verified", "generation_binding_feature_verified",
    "generation_trained_branch_verified", "pose_v2_synthetic_tests_passed",
    "old_failed_pilot_preserved", "no_untrained_conditioning_switch")
IMPLEMENTATIONS = ("run_vhh_controlled_expansion.py", "summarize_vhh_controlled_expansion.py",
    "evaluate_vhh_pose_v2.py", "evaluate_vhh_epitope.py", "summarize_vhh_pilot.py",
    "run_vhh_diversified_pilot.py", "run_owner_exploratory_cell.sh",
    "run_owner_t12_split_template.py", "vhh_stage_gate.py")


def write_new(path, value):
    """Exclusive JSON publication; never overwrite a result or plan."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def bound_json(path):
    """Bind one small, nonsymlink JSON evidence object."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("symlink evidence is forbidden")
    path = path.resolve(strict=True)
    if path.suffix != ".json" or path.stat().st_size > 8 * 1024**2:
        raise ValueError("small JSON evidence required")
    return {"path": str(path), "sha256": sha256_file(path)}


def check_cpu_receipt(receipt):
    """Fail closed on missing method preparation or source evidence."""
    if receipt.get("schema") != "VHH_CONTROLLED_EXPANSION_CPU_V1" or receipt.get("status") != "READY":
        raise ValueError("independent CPU preparation is not ready")
    if receipt.get("experiment_rule_sha256") != digest(RULE):
        raise ValueError("CPU preparation identifies different experiment rules")
    if any(receipt.get("checks", {}).get(key) is not True for key in CPU_CHECKS):
        raise ValueError("CPU prerequisite check not passed")
    issues = validate_artifacts(receipt.get("result_artifacts"))
    if issues:
        raise ValueError("; ".join(issues))


def check_native(contract, bindings, receipt):
    """Reuse only successful native calibration; never reopen the old pilot."""
    stage = next(row for row in contract["stages"] if row["id"] == "native_free_refold")
    issues = validate_receipt(contract, stage, bindings["native_free_refold"], receipt, timestamp(utc_now()))
    if issues:
        raise ValueError("native calibration invalid: " + "; ".join(issues))


def controlled_cells(spec_root, prefix):
    """Keep the two previous frameworks, add one length-matched framework."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,45}", prefix):
        raise ValueError("safe fresh prefix required")
    cells = [spec_record(Path(spec_root), label, f"{prefix}_{label.split('_')[2].lower()}_n2") for label in RULE["scaffolds"]]
    lengths = lambda row: [end-start+1 for start, end in row["cdr_ranges_one_based"]]
    if lengths(cells[0]) != RULE["matched_cdr_lengths"] or lengths(cells[2]) != RULE["matched_cdr_lengths"]:
        raise ValueError("old/new matched frameworks must have CDR lengths 8/7/15")
    if lengths(cells[1]) != [8, 7, 11]:
        raise ValueError("second historical framework CDR definition changed")
    if len({cell["spec_hashes"]["target.cif"] for cell in cells}) != 1:
        raise ValueError("all cells must share exactly the same source target")
    if len({cell["spec_hashes"]["design.yaml"] for cell in cells}) != 1:
        raise ValueError("all cells must share the same target conditioning specification")
    return cells


def prepare(args):
    """Freeze rules, implementations and real prerequisites before any GPU work."""
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("preparation output must be new")
    cpu = json_object(args.cpu_receipt)
    check_cpu_receipt(cpu)
    check_native(json_object(args.native_contract), json_object(args.native_bindings), json_object(args.native_receipt))
    from evaluate_vhh_pose_v2 import POSE_V2_RULE
    plan = {"schema": "VHH_CONTROLLED_EXPANSION_PLAN_V1", "rule": RULE,
        "pose_rule": POSE_V2_RULE, "created_at_utc": utc_now(),
        "registration": "PROSPECTIVE_NEW_EXPERIMENT_AFTER_RETROSPECTIVE_METHOD_DEVELOPMENT",
        "cells": controlled_cells(args.spec_root.resolve(strict=True), args.prefix),
        "prerequisites": {key: bound_json(getattr(args, key)) for key in ("cpu_receipt", "native_contract", "native_bindings", "native_receipt")},
        "implementation_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in IMPLEMENTATIONS},
        "downstream_gpu_automatic": False, "claim_boundary": "COMPUTATIONAL_EXPLORATION_NO_BINDING_OR_SELECTIVITY_CLAIM"}
    write_new(output / "PLAN.json", plan)
    print(json.dumps({"plan": str(output / "PLAN.json"), "sha256": sha256_file(output / "PLAN.json"), "candidate_count": 6, "fold_sample_count": 30}))
    return 0


def validate_plan(plan):
    """Check frozen code, rules, sources and prerequisites; no weight scan."""
    from evaluate_vhh_pose_v2 import POSE_V2_RULE
    if plan.get("schema") != "VHH_CONTROLLED_EXPANSION_PLAN_V1" or plan.get("rule") != RULE or plan.get("pose_rule") != POSE_V2_RULE:
        raise ValueError("plan rules differ from the frozen implementation")
    if timestamp(plan["created_at_utc"]) > timestamp(utc_now()):
        raise ValueError("plan must predate execution")
    for name in IMPLEMENTATIONS:
        if plan["implementation_sha256"].get(name) != sha256_file(Path(__file__).with_name(name)):
            raise ValueError("implementation changed after freeze: " + name)
    if validate_artifacts(list(plan["prerequisites"].values())):
        raise ValueError("prerequisite evidence changed after freeze")
    prerequisites = {key: json_object(Path(value["path"])) for key, value in plan["prerequisites"].items()}
    check_cpu_receipt(prerequisites["cpu_receipt"])
    check_native(prerequisites["native_contract"], prerequisites["native_bindings"], prerequisites["native_receipt"])
    cells = plan.get("cells", [])
    if [row["scaffold_id"] for row in cells] != RULE["scaffolds"] or len({row["cell_id"] for row in cells}) != 3:
        raise ValueError("exactly the three frozen unique cells are required")
    for cell in cells:
        current = spec_record(Path(cell["spec_path"]).parent.parent, cell["scaffold_id"], cell["cell_id"])
        if current != cell or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", cell["cell_id"]):
            raise ValueError("source spec or cell identity changed after freeze")


def run(args):
    """Run three serial, protected cells once, with a single outer deadline."""
    started, started_utc = time.monotonic(), utc_now()
    repo, workspace = args.repo_root.resolve(strict=True), args.workspace.resolve(strict=True)
    ensure_clean_repository(repo)
    plan = json_object(args.plan)
    validate_plan(plan)
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("run output must be new")
    for cell in plan["cells"]:
        if (workspace / "gpu_work/owner_mode/t8_exploratory_inference" / cell["cell_id"]).exists():
            raise ValueError("cell ID already used; no retry/reuse")
    output.mkdir(parents=True, mode=0o700)
    index = {**plan, "plan_sha256": sha256_file(args.plan), "started_at_utc": started_utc,
        "status": "RUNNING", "cells": [], "biological_pass": False}
    code = 1
    try:
        for position, cell in enumerate(plan["cells"]):
            remaining = RULE["hard_timeout_seconds"] - (time.monotonic() - started) - 75
            if remaining <= 1:
                raise RuntimeError("outer budget exhausted before next cell")
            print(f"CELL_START {position + 1}/3 scaffold={cell['scaffold_id']} remaining_seconds={remaining:.1f}", flush=True)
            command = ["bash", str(Path(__file__).with_name("run_owner_exploratory_cell.sh")), str(workspace), cell["cell_id"], cell["spec_path"], "adherence", "2"]
            exit_code, timed_out = bounded_cell(command, output / f"cell_{position}.log", repo, remaining)
            attempts = sorted((workspace / "gpu_work/owner_mode/t8_exploratory_inference" / cell["cell_id"]).glob("attempt_*"))
            row = {**cell, "exit_code": exit_code, "timed_out": timed_out,
                "attempt_root": str(attempts[0]) if len(attempts) == 1 else None}
            index["cells"].append(row)
            if exit_code != 0 or len(attempts) != 1:
                raise RuntimeError("cell failed; preserve partial outputs, no expansion")
            logs = attempts[0] / "operator_logs"
            receipt = json_object(logs / "EXPLORATORY_INFERENCE.json")
            if (receipt.get("status") != "EXPLORATORY_INFERENCE_COMPLETE" or receipt.get("exit_code") != 0
                    or receipt.get("observed_designs") != 2 or receipt.get("fold_samples_per_candidate") != 5
                    or receipt.get("cuda_oom_detected") is not False
                    or (logs / "STATUS.txt").read_text().strip() != "EXPLORATORY_INFERENCE_COMPLETE"):
                raise RuntimeError("terminal output/used-asset validation did not complete")
            row.update(receipt_sha256=sha256_file(logs / "EXPLORATORY_INFERENCE.json"),
                terminal_status_sha256=sha256_file(logs / "STATUS.txt"), legacy_filter_pass_count=receipt["filter_pass_count"])
            write_new(output / f"CELL_{position}_COMPLETED.json", row)
        index["status"], code = "GPU_COMPLETE_PENDING_DESCRIPTIVE_ANALYSIS", 0
    except Exception as exc:
        index["status"], index["error"] = "EXECUTION_FAILED", f"{type(exc).__name__}: {exc}"
        try:
            runtime = workspace / "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
            manifest = dict(parse_manifest(runtime / "SHA256SUMS"))
            names = ("boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt", "boltz2_conf_final.ckpt", "mols.zip")
            index["failure_path_runtime_assets_after"] = verify_runtime(runtime, {name: manifest[name] for name in names})
        except Exception as verification_error:
            index["failure_path_runtime_verification_error"] = str(verification_error)
    finally:
        index.update(wall_seconds=time.monotonic()-started, finished_at_utc=utc_now())
        if index["wall_seconds"] > RULE["hard_timeout_seconds"]:
            index["status"], index["error"], code = "EXECUTION_FAILED", "outer wall budget exceeded", 1
        write_new(output / "INDEX.json", index)
    print(json.dumps({key: index[key] for key in ("status", "wall_seconds")}))
    return code


def main():
    """Separate CPU preparation from explicit GPU execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    for name in ("spec-root", "output", "cpu-receipt", "native-contract", "native-bindings", "native-receipt"):
        prep.add_argument("--" + name, type=Path, required=True)
    prep.add_argument("--prefix", required=True)
    run_parser = sub.add_parser("run")
    for name in ("workspace", "repo-root", "plan", "output"):
        run_parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    return prepare(args) if args.action == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
