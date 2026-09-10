#!/usr/bin/env python3
"""Freeze and run the 20260911 input-compatible terminal-condition experiment.

Only three controlled spec bundles and their CPU evidence are accepted. Strict
input compatibility is checked during preparation and again before any GPU cell.
Each completed cell is also checked with the original full-atom evaluator before
the next cell starts. Historical rules and outputs are not modified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

import yaml
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_owner_t12_split_template import json_object, sha256_file, utc_now, parse_manifest, verify_runtime
from run_vhh_controlled_expansion import bound_json, check_native, write_new
from run_vhh_diversified_pilot import bounded_cell, ensure_clean_repository, spec_record
from vhh_stage_gate import timestamp, validate_artifacts

RULE = {
    "experiment_id": "vhh-terminal-control-20260911",
    "condition_ids": ["A", "B", "C"], "scaffold_id": "01_pdb_00007xl0-A",
    "candidates_per_condition": 2, "free_folds_per_candidate": 5,
    "hard_timeout_seconds": 1800, "checkpoint": "adherence",
    "source_cdr_lengths": [8, 7, 15],
    "source_file_sha256": {
        "scaffold.cif": "68a4c9545a51c56f652c503c94e572e035556998bb3a83d78b99ad80ae1a97d2",
        "scaffold.yaml": "6f2ce02ad5e314b5c971491d54b10f83c69178491adfa523cf83a3965c6582d0",
        "target.cif": "11b82b2633793e6799f1d56c19a88fd52828bec5d26d9366801753dfa72d2d53",
    },
    "evaluation_scope": "STRICT_FULL_ASSIGNED_CANONICAL_HEAVY_ATOMS",
    "legacy_binding_site_filter": False,
    "primary_descriptors": ["generated_his_ala_contact", "free_fold_his_ala_contact", "candidate_all_five_his_ala_contact"],
    "seed_interpretation": "EQUAL_BUDGET_STOCHASTIC_DRAWS_NOT_COMMON_RANDOM_NUMBER_PAIRS",
    "automatic_retry": False, "automatic_downstream_gpu": False,
    "old_pilot_status": "BLOCKED_UNCHANGED", "biological_pass": False,
}
EXPECTED_BINDING = {"A": [0]*30, "B": [1, 1]+[0]*28, "C": [1, 1]+[2]*28}
IMPLEMENTATIONS = (
    "run_vhh_terminal_controls.py", "prepare_vhh_terminal_controls.py",
    "preflight_vhh_evaluator_compatibility.py", "summarize_vhh_terminal_controls.py",
    "evaluate_vhh_epitope.py", "evaluate_vhh_pose_v2.py",
    "summarize_vhh_pilot.py", "summarize_vhh_controlled_expansion.py",
    "run_vhh_controlled_expansion.py", "run_vhh_diversified_pilot.py",
    "run_owner_exploratory_cell.sh", "run_vhh_terminal_cell.py", "run_owner_t12_split_template.py",
    "vhh_stage_gate.py", "check_vhh_generation_conditioning.py",
)
TERMINAL_FIELDS = ("exit_code", "timed_out", "attempt_root", "receipt_sha256",
                   "terminal_status_sha256", "legacy_filter_pass_count", "strict_output_check")


def small_binding(path):
    """Bind only a bounded, regular source/config/feature file, never model weights."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 8*1024**2:
        raise ValueError("small nonsymlink absolute source file required")
    return {"path": str(path.resolve(strict=True)), "sha256": sha256_file(path)}


def verify_small_binding(binding):
    """Recheck a direct small source binding without scanning a directory tree."""
    if small_binding(binding["path"]) != binding:
        raise ValueError("small source binding changed")


def condition_record(spec_path, condition_id, cell_id):
    """Bind exact files without confusing a condition folder with scaffold identity."""
    if condition_id not in EXPECTED_BINDING:
        raise ValueError("unknown condition")
    spec_path = Path(spec_path).resolve(strict=True)
    if spec_path.name != "design.yaml":
        raise ValueError("expected design.yaml")
    row = spec_record(spec_path.parent.parent, spec_path.parent.name, cell_id)
    row.update(scaffold_id=RULE["scaffold_id"], condition_id=condition_id,
               expected_target_binding_types=EXPECTED_BINDING[condition_id])
    if [end-start+1 for start, end in row["cdr_ranges_one_based"]] != RULE["source_cdr_lengths"]:
        raise ValueError("unexpected CDR lengths")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", cell_id):
        raise ValueError("unsafe cell id")
    return row


def validate_controlled_specs(cells):
    """Only target binding labels may differ across the three source specifications."""
    if [c["condition_id"] for c in cells] != RULE["condition_ids"]:
        raise ValueError("exactly A/B/C in frozen order required")
    if len({c["cell_id"] for c in cells}) != 3:
        raise ValueError("unique cell ids required")
    canonical = []
    for cell in cells:
        current = condition_record(cell["spec_path"], cell["condition_id"], cell["cell_id"])
        if current != cell:
            raise ValueError("source, condition or CDR definition changed")
        data = yaml.safe_load(Path(cell["spec_path"]).read_text())
        target = data["entities"][0]["file"]
        labels = target.pop("binding_types", None)
        expected = None if cell["condition_id"] == "A" else [{"chain": {
            "id": "E", "binding": "1..2",
            **({"not_binding": "3..30"} if cell["condition_id"] == "C" else {})}}]
        if labels != expected:
            raise ValueError("target binding specification differs from fixed condition")
        canonical.append(data)
    if canonical[1:] != canonical[:1]*2:
        raise ValueError("non-binding specification differences are forbidden")
    for name in ("target.cif", "scaffold.cif", "scaffold.yaml"):
        if len({c["spec_hashes"][name] for c in cells}) != 1:
            raise ValueError("all arms must share " + name)
        if cells[0]["spec_hashes"][name] != RULE["source_file_sha256"][name]:
            raise ValueError("source is not the frozen complete 7XL0 input: " + name)


def require_compatible_inputs(cells, design_config):
    """Run actual CPU parser/evaluator checks, never trust a ready label alone."""
    from preflight_vhh_evaluator_compatibility import inspect_spec
    rows = []
    for cell in cells:
        result = inspect_spec(Path(cell["spec_path"]), Path(design_config))
        if result.get("status") != "PASS" or result.get("ready") is not True:
            raise ValueError("input/evaluator incompatibility in " + cell["condition_id"])
        rows.append({"condition_id": cell["condition_id"], "result": result})
    return rows


def check_preparation(preparation):
    """Require actual checked preparation, not a hand-written pass boolean."""
    if (preparation.get("schema") != "VHH_TERMINAL_CONTROLS_PREPARATION_V1"
            or preparation.get("status") != "CPU_PREPARATION_COMPLETE"
            or preparation.get("gpu_started") is not False):
        raise ValueError("terminal control CPU preparation is incomplete")
    if [c["condition_id"] for c in preparation.get("cells", [])] != RULE["condition_ids"]:
        raise ValueError("CPU preparation lacks three conditions")
    sources = preparation["source_spec_binding"]
    if set(sources) != {"design.yaml", "scaffold.yaml", "scaffold.cif", "target.cif"}:
        raise ValueError("four original source bindings required")
    for binding in [*sources.values(), preparation["source_design_config_binding"],
                    *preparation["source_implementation"].values()]:
        verify_small_binding(binding)
    for name, expected in RULE["source_file_sha256"].items():
        if sources[name]["sha256"] != expected:
            raise ValueError("CPU preparation source identity changed")
    source_design = yaml.safe_load(Path(sources["design.yaml"]["path"]).read_text())
    source_design["entities"][0]["file"].pop("binding_types", None)
    metadata = preparation["checkpoint_diagnostic"]
    if (metadata.get("checkpoint_name") != "boltzgen1_adherence.ckpt"
            or metadata.get("add_binding_specification") is not True
            or metadata.get("binding_embedding_present") is not True):
        raise ValueError("checkpoint-backed conditioning evidence is incomplete")
    baselines = {}
    for row in preparation["cells"]:
        expected = EXPECTED_BINDING[row["condition_id"]]
        if row["expected_target_binding_types"] != expected:
            raise ValueError("CPU binding interpretation differs from rule")
        current = yaml.safe_load(Path(row["spec_path"]).read_text())
        current["entities"][0]["file"].pop("binding_types", None)
        if current != source_design:
            raise ValueError("condition changed non-binding source content")
        if [s["sample_index"] for s in row.get("samples", [])] != [0, 1]:
            raise ValueError("both CPU sample checks required")
        for sample in row["samples"]:
            verify_small_binding(sample["feature_snapshot"])
            for phase in ("parsed", "masked"):
                record = sample[phase]
                if (record["target_binding_types"] != expected
                        or record["vhh_binding_types_all_unspecified"] is not True
                        or record["cdr_token_count"] != 30):
                    raise ValueError("CPU parsed/masked binding check failed")
            with np.load(sample["feature_snapshot"]["path"], allow_pickle=False) as data:
                arrays = {name: data[name] for name in data.files}
            for name in ("binding_type", "masked_binding_type"):
                actual = arrays[name]
                if actual.ndim != 1 or not np.array_equal(actual[:30], expected) or np.any(actual[30:] != 0):
                    raise ValueError("actual CPU feature binding labels differ")
            other = {name: value for name, value in arrays.items()
                     if name not in ("binding_type", "masked_binding_type")}
            number = sample["sample_index"]
            if number not in baselines:
                baselines[number] = other
            elif set(other) != set(baselines[number]) or any(
                    not np.array_equal(value, baselines[number][name], equal_nan=True)
                    for name, value in other.items()):
                raise ValueError("CPU controlled non-binding features differ")


def prepare(args):
    """Create a new immutable plan after source compatibility and software checks."""
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("preparation output must be new")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,45}", args.prefix):
        raise ValueError("safe fresh prefix required")
    preparation = json_object(args.preparation)
    check_preparation(preparation)
    config_binding = small_binding(args.design_config)
    if config_binding != preparation["source_design_config_binding"]:
        raise ValueError("generation config differs from actual CPU preparation")
    tests = json_object(args.test_summary)
    if tests.get("failed") != 0 or tests.get("passed", 0) < 281:
        raise ValueError("full software regression receipt required")
    cells = [condition_record(row["spec_path"], row["condition_id"],
             args.prefix+"_"+row["condition_id"].lower()+"_n2") for row in preparation["cells"]]
    for cell, source in zip(cells, preparation["cells"]):
        if cell["spec_hashes"] != source["spec_hashes"]:
            raise ValueError("prepared source has changed")
    validate_controlled_specs(cells)
    compatibility = require_compatible_inputs(cells, args.design_config)
    check_native(json_object(args.native_contract), json_object(args.native_bindings), json_object(args.native_receipt))
    prerequisites = {name: bound_json(getattr(args, name)) for name in (
        "preparation", "test_summary", "native_contract", "native_bindings", "native_receipt")}
    # The accepted real generation config is YAML, bound separately from JSON receipts.
    plan = {"schema": "VHH_TERMINAL_CONTROL_PLAN_V1", "rule": RULE,
            "created_at_utc": utc_now(), "cells": cells,
            "registration": "PROSPECTIVE_STOCHASTIC_CONDITION_CONTROL_NOT_PAIRED_MOLECULES",
            "prerequisites": prerequisites, "generation_config_binding": config_binding,
            "input_compatibility": compatibility,
            "implementation_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in IMPLEMENTATIONS},
            "biological_pass": False, "automatic_downstream_gpu": False}
    write_new(output / "PLAN.json", plan)
    print(json.dumps({"plan": str(output / "PLAN.json"), "sha256": sha256_file(output / "PLAN.json"),
                      "candidate_count": 6, "fold_sample_count": 30}))
    return 0


def validate_plan(plan, recheck_inputs=False):
    """Verify the frozen plan; actual input parsing is mandatory on GPU launch."""
    if plan.get("schema") != "VHH_TERMINAL_CONTROL_PLAN_V1" or plan.get("rule") != RULE:
        raise ValueError("plan schema or rules differ")
    if timestamp(plan["created_at_utc"]) > timestamp(utc_now()):
        raise ValueError("plan must predate execution")
    for name in IMPLEMENTATIONS:
        if plan["implementation_sha256"].get(name) != sha256_file(Path(__file__).with_name(name)):
            raise ValueError("implementation changed after freeze: " + name)
    issues = validate_artifacts(list(plan["prerequisites"].values()))
    if issues:
        raise ValueError("; ".join(issues))
    prep = json_object(Path(plan["prerequisites"]["preparation"]["path"]))
    check_preparation(prep)
    verify_small_binding(plan["generation_config_binding"])
    if plan["generation_config_binding"] != prep["source_design_config_binding"]:
        raise ValueError("generation config differs from CPU preparation")
    cells = plan["cells"]
    validate_controlled_specs(cells)
    for cell, row in zip(cells, prep["cells"]):
        if cell["spec_hashes"] != row["spec_hashes"] or cell["spec_path"] != row["spec_path"]:
            raise ValueError("plan differs from CPU-prepared inputs")
    native = [json_object(Path(plan["prerequisites"][key]["path"])) for key in (
        "native_contract", "native_bindings", "native_receipt")]
    check_native(*native)
    if recheck_inputs:
        require_compatible_inputs(cells, plan["generation_config_binding"]["path"])


def strict_cell_check(cell):
    """Stop before further GPU work if a newly generated full structure cannot be evaluated."""
    import evaluate_vhh_epitope as E
    import summarize_vhh_pilot as P
    from summarize_vhh_terminal_controls import evaluate_candidate
    from summarize_vhh_controlled_expansion import source_reference
    source = Path(cell["spec_path"]).parent
    ref = source_reference(source/"target.cif", yaml.safe_load((source/"design.yaml").read_text()))
    cdr_tokens, _ = P.cdr_annotation(yaml.safe_load((source/"scaffold.yaml").read_text()))
    root, _ = P.fold_design_root(cell["attempt_root"])
    paths = sorted(root.glob("design_*.npz"))
    if len(paths) != 2 or {p.name for p in paths} != {p.name for p in (root/"fold_out_npz").glob("design_*.npz")}:
        raise ValueError("candidate output closure mismatch")
    for path in paths:
        design, _ = E._load_npz(path)
        fold, _ = E._load_npz(root/"fold_out_npz"/path.name)
        if not np.array_equal(np.flatnonzero(design["design_mask"]), cdr_tokens) or len(fold["coords"]) != 5:
            raise ValueError("output CDR or fold count differs")
        evaluate_candidate(design, fold, ref,
            expected_target_binding_types=cell["expected_target_binding_types"], cdr_tokens=cdr_tokens)
    return {"status": "PASS", "candidate_count": 2, "free_fold_sample_count": 10,
            "scope": RULE["evaluation_scope"], "biological_pass": False}


def run(args):
    """Execute three protected serial cells once with a whole-workflow deadline."""
    started, started_utc = time.monotonic(), utc_now()
    repo, workspace = args.repo_root.resolve(strict=True), args.workspace.resolve(strict=True)
    ensure_clean_repository(repo)
    plan = json_object(args.plan)
    validate_plan(plan, recheck_inputs=True)
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("run output must be new")
    for cell in plan["cells"]:
        if (workspace/"gpu_work/owner_mode/t8_exploratory_inference"/cell["cell_id"]).exists():
            raise ValueError("cell ID already used; no retry/reuse")
    output.mkdir(parents=True, mode=0o700)
    index = {**plan, "plan_sha256": sha256_file(args.plan), "started_at_utc": started_utc,
             "frozen_cells": plan["cells"], "cells": [], "status": "RUNNING", "biological_pass": False}
    write_new(output/"RUN_STARTED.json", {"started_at_utc": started_utc, "plan_sha256": index["plan_sha256"]})
    code = 1
    try:
        for pos, cell in enumerate(plan["cells"]):
            remaining = RULE["hard_timeout_seconds"]-(time.monotonic()-started)-75
            if remaining <= 1:
                raise RuntimeError("outer budget exhausted before next cell")
            print(f"CONDITION_START {cell['condition_id']} remaining_seconds={remaining:.1f}", flush=True)
            command = [sys.executable, str(Path(__file__).with_name("run_vhh_terminal_cell.py")),
                       str(workspace), cell["cell_id"], cell["spec_path"], "adherence", "2"]
            exit_code, timed_out = bounded_cell(command, output/f"condition_{cell['condition_id']}.log", repo, remaining)
            attempts = sorted((workspace/"gpu_work/owner_mode/t8_exploratory_inference"/cell["cell_id"]).glob("attempt_*"))
            row = {**cell, "exit_code": exit_code, "timed_out": timed_out,
                   "attempt_root": str(attempts[0]) if len(attempts) == 1 else None}
            index["cells"].append(row)
            if exit_code != 0 or timed_out or len(attempts) != 1:
                raise RuntimeError("cell execution failed; preserve partial outputs")
            logs = attempts[0]/"operator_logs"
            receipt = json_object(logs/"EXPLORATORY_INFERENCE.json")
            if (receipt.get("status") != "EXPLORATORY_INFERENCE_COMPLETE" or receipt.get("exit_code") != 0
                    or receipt.get("observed_designs") != 2 or receipt.get("fold_samples_per_candidate") != 5
                    or receipt.get("cuda_oom_detected") is not False
                    or receipt.get("output_validation", {}).get("status") != "PASS"
                    or (logs/"STATUS.txt").read_text().strip() != "EXPLORATORY_INFERENCE_COMPLETE"):
                raise RuntimeError("terminal validation or counts failed")
            row.update(receipt_sha256=sha256_file(logs/"EXPLORATORY_INFERENCE.json"),
                       terminal_status_sha256=sha256_file(logs/"STATUS.txt"),
                       legacy_filter_pass_count=receipt["filter_pass_count"])
            row["strict_output_check"] = strict_cell_check(row)
            write_new(output/f"CONDITION_{cell['condition_id']}_COMPLETED.json", row)
        index["status"], code = "GPU_COMPLETE_PENDING_STRICT_ANALYSIS", 0
    except Exception as exc:
        index["status"], index["error"] = "EXECUTION_FAILED", f"{type(exc).__name__}: {exc}"
        try:
            runtime = workspace/"boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
            manifest = dict(parse_manifest(runtime/"SHA256SUMS"))
            names = ("boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt", "boltz2_conf_final.ckpt", "mols.zip")
            index["failure_path_runtime_assets_after"] = verify_runtime(runtime, {name: manifest[name] for name in names})
        except Exception as error:
            index["failure_path_runtime_verification_error"] = str(error)
    finally:
        index.update(wall_seconds=time.monotonic()-started, finished_at_utc=utc_now())
        if index["wall_seconds"] > RULE["hard_timeout_seconds"]:
            index["status"], index["error"], code = "EXECUTION_FAILED", "outer wall budget exceeded", 1
        write_new(output/"INDEX.json", index)
    print(json.dumps({key: index[key] for key in ("status", "wall_seconds")}))
    return code


def main():
    """Expose separate CPU freeze and bounded GPU actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    for name in ("preparation", "design-config", "test-summary", "native-contract", "native-bindings", "native-receipt", "output"):
        p.add_argument("--"+name, type=Path, required=True)
    p.add_argument("--prefix", required=True)
    r = sub.add_parser("run")
    for name in ("workspace", "repo-root", "plan", "output"):
        r.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    return prepare(args) if args.action == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
