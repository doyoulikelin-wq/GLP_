#!/usr/bin/env python3
"""CPU-revalidate the specific optional-column failure, then run only B/C once.

Historical outputs stay unchanged. The original absolute 1800-second deadline
includes CPU repair time; no new budget, A rerun, retry, or biological pass is
created. Preparation is CPU-only; the separate run command uses the V2 adapter.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_vhh_terminal_controls as R

IMPLEMENTATIONS = ("validate_vhh_terminal_cell.py", "run_vhh_terminal_cell_v2.py",
                   "resume_vhh_terminal_controls.py", "summarize_vhh_terminal_continuation.py")
PLAN_SCHEMA = "VHH_TERMINAL_CONTROL_CONTINUATION_PLAN_V1"
INDEX_SCHEMA = "VHH_TERMINAL_CONTROL_CONTINUATION_INDEX_V1"
FAILURE = "validation failed: ('missing required analysis metrics', {'bindsite_under_8rmsd'})"
FAILED = "EXPLORATORY_INFERENCE_FAILED"


def implementation_hashes():
    return {name: R.small_binding(Path(__file__).with_name(name))["sha256"] for name in IMPLEMENTATIONS}


def original_failure(plan, index, plan_sha256):
    """Accept only the completed A computation with one precisely known CPU error."""
    if (index.get("status") != "EXECUTION_FAILED" or index.get("rule") != R.RULE
            or index.get("plan_sha256") != plan_sha256 or index.get("frozen_cells") != plan["cells"]
            or len(index.get("cells", [])) != 1):
        raise ValueError("only the original single-A failure is recoverable")
    row = index["cells"][0]
    if ({k: v for k, v in row.items() if k not in R.TERMINAL_FIELDS} != plan["cells"][0]
            or row.get("condition_id") != "A" or row.get("exit_code") != 1 or row.get("timed_out") is not False):
        raise ValueError("A source identity or failure mode differs")
    root = Path(row["attempt_root"])
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("A attempt must be a canonical ordinary directory")
    logs = root / "operator_logs"
    receipt = R.json_object(logs / "EXPLORATORY_INFERENCE.json")
    if (receipt.get("status") != FAILED or receipt.get("exit_code") != 1
            or receipt.get("cell_id") != row["cell_id"] or receipt.get("run_root") != str(root)
            or receipt.get("cuda_oom_detected") is not False or receipt.get("expected_designs") != 2
            or receipt.get("resolved_config_contract", {}).get("status") != "PASS"
            or (logs / "STATUS.txt").read_text().strip() != FAILED):
        raise ValueError("original receipt is not the eligible A failure")
    evidence = ["EXPLORATORY_INFERENCE.json", "STATUS.txt", "OUTPUT_SHA256SUMS"]
    for stage in ("design", "inverse_folding", "folding", "analysis", "filtering", "validation"):
        name = stage + ".exit_code.txt"
        if (logs / name).read_text().strip() != ("1" if stage == "validation" else "0"):
            raise ValueError("original computation stage failed: " + stage)
        evidence.append(name)
    streams = {name: (logs / ("validation." + name + ".txt")).read_text().strip()
               for name in ("stdout", "stderr")}
    matches = [name for name, value in streams.items() if value == FAILURE]
    if len(matches) != 1 or streams["stderr" if matches[0] == "stdout" else "stdout"]:
        raise ValueError("failure is not exclusively the known optional-column error")
    evidence += ["validation.stdout.txt", "validation.stderr.txt"]
    # This is the small completed A output manifest, not a workspace/weight scan.
    for relative, expected in R.parse_manifest(logs / "OUTPUT_SHA256SUMS"):
        path = root / relative
        if not path.resolve(strict=True).is_relative_to(root) or R.small_binding(path)["sha256"] != expected:
            raise ValueError("original A output changed: " + relative)
    return {"error_stream": matches[0], "original_failure": FAILURE,
            "source_bindings": {name: R.small_binding(logs / name) for name in evidence}}


def cpu_validate_a(row):
    """Run both real output validation and unchanged strict E without a model."""
    from validate_vhh_terminal_cell import validate
    saved = {key: os.environ.get(key) for key in ("EXPECTED_DESIGNS", "EXPECTED_FOLD_SAMPLES")}
    try:
        os.environ.update(EXPECTED_DESIGNS="2", EXPECTED_FOLD_SAMPLES="5")
        result = validate(row["attempt_root"])
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    strict = R.strict_cell_check(row)
    if (result.get("status") != "PASS" or result.get("fold_samples_per_candidate") != 5
            or result.get("observed_unique_ids") != 2 or strict.get("status") != "PASS"
            or strict.get("candidate_count") != 2 or strict.get("free_fold_sample_count") != 10
            or strict.get("scope") != R.RULE["evaluation_scope"]):
        raise ValueError("A CPU validation or strict full-atom evaluation failed")
    return result, strict


def prepare(args):
    """Freeze a new continuation record without touching the original attempt."""
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("preparation output must be new")
    original_binding, failed_binding = R.small_binding(args.plan), R.small_binding(args.failed_index)
    original, failed = R.json_object(args.plan), R.json_object(args.failed_index)
    R.validate_plan(original, recheck_inputs=True)
    evidence = original_failure(original, failed, original_binding["sha256"])
    hashes = implementation_hashes()
    result, strict = cpu_validate_a(failed["cells"][0])
    deadline = R.timestamp(failed["started_at_utc"]) + timedelta(seconds=R.RULE["hard_timeout_seconds"])
    if R.timestamp(R.utc_now()) >= deadline:
        raise ValueError("original absolute deadline expired")
    proof = {"schema": "VHH_TERMINAL_A_CPU_REVALIDATION_V1", "created_at_utc": R.utc_now(),
             "validator_result": result, "strict_check": strict, **evidence,
             "historical_terminal_status": FAILED, "source_outputs_modified": False, "biological_pass": False}
    R.write_new(output / "A_CPU_VALIDATION.json", proof)
    plan = {"schema": PLAN_SCHEMA, "rule": R.RULE, "created_at_utc": R.utc_now(),
            "original_plan": original_binding, "failed_index": failed_binding,
            "a_cpu_validation": R.small_binding(output / "A_CPU_VALIDATION.json"), "a_strict_check": strict,
            "original_started_at_utc": failed["started_at_utc"], "deadline_at_utc": deadline.isoformat(),
            "implementation_sha256": hashes, "biological_pass": False, "automatic_retry": False}
    R.write_new(output / "RESUME_PLAN.json", plan)
    print(json.dumps({"resume_plan": str(output / "RESUME_PLAN.json"), "deadline_at_utc": plan["deadline_at_utc"]}))
    return 0


def validate_resume_plan(plan, recheck_inputs=False):
    """Recheck original and new source bindings; never renew the original deadline."""
    if (plan.get("schema") != PLAN_SCHEMA or plan.get("rule") != R.RULE
            or plan.get("implementation_sha256") != implementation_hashes()
            or plan.get("automatic_retry") is not False or plan.get("biological_pass") is not False):
        raise ValueError("continuation implementation or rules changed")
    for key in ("original_plan", "failed_index", "a_cpu_validation"):
        R.verify_small_binding(plan[key])
    original, failed, proof = [R.json_object(Path(plan[key]["path"]))
                               for key in ("original_plan", "failed_index", "a_cpu_validation")]
    R.validate_plan(original, recheck_inputs=recheck_inputs)
    evidence = original_failure(original, failed, plan["original_plan"]["sha256"])
    deadline = R.timestamp(failed["started_at_utc"]) + timedelta(seconds=R.RULE["hard_timeout_seconds"])
    if (plan["original_started_at_utc"] != failed["started_at_utc"] or R.timestamp(plan["deadline_at_utc"]) != deadline
            or R.timestamp(plan["created_at_utc"]) > deadline
            or any(proof.get(key) != value for key, value in evidence.items())
            or proof.get("strict_check") != plan.get("a_strict_check")
            or proof.get("historical_terminal_status") != FAILED
            or proof.get("validator_result", {}).get("status") != "PASS"):
        raise ValueError("A evidence or original deadline differs")
    if recheck_inputs:
        result, strict = cpu_validate_a(failed["cells"][0])
        if result != proof["validator_result"] or strict != proof["strict_check"]:
            raise ValueError("A CPU evidence changed")


def run(args):
    """Launch B and C serially once, preserving A's failed historical row."""
    started, started_utc = time.monotonic(), R.utc_now()
    repo, workspace = args.repo_root.resolve(strict=True), args.workspace.resolve(strict=True)
    R.ensure_clean_repository(repo)
    plan = R.json_object(args.resume_plan)
    validate_resume_plan(plan, recheck_inputs=True)
    original = R.json_object(Path(plan["original_plan"]["path"]))
    failed = R.json_object(Path(plan["failed_index"]["path"]))
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("continuation output must be new")
    for cell in original["cells"][1:]:
        if (workspace / "gpu_work/owner_mode/t8_exploratory_inference" / cell["cell_id"]).exists():
            raise ValueError("B/C cell ID already used; no retry/reuse")
    if (R.timestamp(plan["deadline_at_utc"]) - R.timestamp(R.utc_now())).total_seconds() <= 76:
        raise ValueError("original absolute deadline exhausted")
    output.mkdir(parents=True, mode=0o700)
    a = {**failed["cells"][0], "reused_original_gpu_outputs": True,
         "cpu_revalidation_sha256": plan["a_cpu_validation"]["sha256"]}
    index = {**plan, "schema": INDEX_SCHEMA, "resume_plan": R.small_binding(args.resume_plan),
             "resume_plan_sha256": R.sha256_file(args.resume_plan), "cells": [a], "started_at_utc": started_utc}
    R.write_new(output / "RUN_STARTED.json", {"started_at_utc": started_utc, "deadline_at_utc": plan["deadline_at_utc"]})
    code = 1
    try:
        for cell in original["cells"][1:]:
            remaining = (R.timestamp(plan["deadline_at_utc"]) - R.timestamp(R.utc_now())).total_seconds() - 75
            if remaining <= 1:
                raise RuntimeError("original absolute budget exhausted before next cell")
            command = [sys.executable, str(Path(__file__).with_name("run_vhh_terminal_cell_v2.py")),
                       str(workspace), cell["cell_id"], cell["spec_path"], "adherence", "2"]
            exit_code, timed_out = R.bounded_cell(command, output / f"condition_{cell['condition_id']}.log", repo, remaining)
            attempts = sorted((workspace / "gpu_work/owner_mode/t8_exploratory_inference" / cell["cell_id"]).glob("attempt_*"))
            row = {**cell, "exit_code": exit_code, "timed_out": timed_out,
                   "attempt_root": str(attempts[0]) if len(attempts) == 1 else None}
            index["cells"].append(row)
            if exit_code != 0 or timed_out or len(attempts) != 1:
                raise RuntimeError("continuation cell failed; no retry")
            logs = attempts[0] / "operator_logs"
            receipt = R.json_object(logs / "EXPLORATORY_INFERENCE.json")
            if (receipt.get("status") != "EXPLORATORY_INFERENCE_COMPLETE" or receipt.get("exit_code") != 0
                    or receipt.get("observed_designs") != 2 or receipt.get("fold_samples_per_candidate") != 5
                    or receipt.get("cuda_oom_detected") is not False
                    or receipt.get("output_validation", {}).get("status") != "PASS"
                    or (logs / "STATUS.txt").read_text().strip() != "EXPLORATORY_INFERENCE_COMPLETE"):
                raise RuntimeError("continuation terminal validation failed")
            row.update(receipt_sha256=R.sha256_file(logs / "EXPLORATORY_INFERENCE.json"),
                       terminal_status_sha256=R.sha256_file(logs / "STATUS.txt"),
                       legacy_filter_pass_count=receipt["filter_pass_count"], strict_output_check=R.strict_cell_check(row))
            R.write_new(output / f"CONDITION_{cell['condition_id']}_COMPLETED.json", row)
        index["status"], code = "COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY", 0
    except Exception as exc:
        index["status"], index["error"] = "EXECUTION_FAILED", f"{type(exc).__name__}: {exc}"
        try:
            runtime = workspace / "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
            manifest = dict(R.parse_manifest(runtime / "SHA256SUMS"))
            names = ("boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt", "boltz2_conf_final.ckpt", "mols.zip")
            index["failure_path_runtime_assets_after"] = R.verify_runtime(runtime, {name: manifest[name] for name in names})
        except Exception as error:
            index["failure_path_runtime_verification_error"] = str(error)
    finally:
        finished = R.utc_now()
        elapsed = (R.timestamp(finished) - R.timestamp(plan["original_started_at_utc"])).total_seconds()
        index.update(finished_at_utc=finished, continuation_wall_seconds=time.monotonic()-started,
                     overall_elapsed_seconds=elapsed)
        if elapsed > R.RULE["hard_timeout_seconds"]:
            index["status"], index["error"], code = "EXECUTION_FAILED", "original absolute wall budget exceeded", 1
        R.write_new(output / "INDEX.json", index)
    print(json.dumps({key: index[key] for key in ("status", "overall_elapsed_seconds", "continuation_wall_seconds")}))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action, names in (("prepare", ("plan", "failed-index", "output")),
                          ("run", ("resume-plan", "repo-root", "workspace", "output"))):
        command = sub.add_parser(action)
        for name in names:
            command.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    return prepare(args) if args.action == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
