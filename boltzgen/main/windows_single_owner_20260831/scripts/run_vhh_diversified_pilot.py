#!/usr/bin/env python3
"""Prepare/run a two-scaffold, four-candidate pilot using the existing cell runner.

This thin wrapper checks the real native prerequisite gate and imposes one
1800-second outer budget. It does not substitute execution success for scientific
diversity: INDEX.json is consumed by the separate structural summarizer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_owner_t12_split_template import (atomic_write, json_object, parse_manifest,
                                        sha256_file, utc_now, verify_manifest, verify_runtime)
from vhh_stage_gate import digest, evaluate

SCAFFOLDS = ("01_pdb_00007xl0-A", "02_pdb_00006apo-A")
SPEC_MEMBERS = ("design.yaml", "scaffold.yaml", "target.cif", "scaffold.cif")


def spec_record(root: Path, scaffold_id: str, cell_id: str) -> dict:
    """Bind the four existing spec files and actual third designed loop length."""
    source = root / scaffold_id
    if {p.name for p in source.iterdir()} != set(SPEC_MEMBERS):
        raise ValueError("spec bundle must contain exactly four expected files")
    if any((source / name).is_symlink() for name in SPEC_MEMBERS):
        raise ValueError("symlink spec member forbidden")
    spec = yaml.safe_load((source / "scaffold.yaml").read_text())
    regions = spec["design"][0]["chain"]["res_index"].split(",")
    if len(regions) != 3 or any(not re.fullmatch(r"\d+\.\.\d+", item) for item in regions):
        raise ValueError("expected three explicitly annotated CDR ranges")
    bounds = [list(map(int, item.split(".."))) for item in regions]
    if any(start > end for start, end in bounds):
        raise ValueError("reversed CDR range")
    return {"cell_id": cell_id, "scaffold_id": scaffold_id, "spec_path": str(source / "design.yaml"),
            "checkpoint": "adherence", "num_designs": 2, "cdr3_length": bounds[-1][1]-bounds[-1][0]+1,
            "cdr_ranges_one_based": bounds, "spec_hashes": {name: sha256_file(source / name) for name in SPEC_MEMBERS}}


def prepare(args: argparse.Namespace) -> int:
    """Write an immutable, private plan; no GPU or model weights are loaded."""
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("refusing to overwrite an existing plan directory")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,45}", args.prefix):
        raise ValueError("safe short lowercase cell prefix required")
    cells = [spec_record(args.spec_root.resolve(strict=True), label,
                         f"{args.prefix}_{label.split('_')[2].lower()}_n2") for label in SCAFFOLDS]
    if len({cell["cdr3_length"] for cell in cells}) != 2:
        raise ValueError("pilot requires two observed CDR3 length classes")
    if len({cell["spec_hashes"]["target.cif"] for cell in cells}) != 1:
        raise ValueError("pilot must use the same active target structure")
    from summarize_vhh_pilot import POSE_RULE
    pose_rule = json_object(args.pose_rule) if args.pose_rule else POSE_RULE
    if pose_rule != POSE_RULE:
        raise ValueError("pose classification must match the preregistered structural summarizer")
    plan = {"schema": "VHH_DIVERSE_PILOT_PLAN_V1", "created_at_utc": utc_now(), "cells": cells,
            "hard_timeout_seconds": 1800, "candidate_count": 4, "folds_per_candidate": 5,
            "pose_classification": pose_rule, "automatic_retry": False,
            "scientific_claim": "COMPUTATIONAL_EXPLORATION_NOT_BINDING_EVIDENCE",
            "legacy_runner_sha256": sha256_file(Path(__file__).with_name("run_owner_exploratory_cell.sh")),
            "wrapper_sha256": sha256_file(Path(__file__)),
            "summarizer_sha256": sha256_file(Path(__file__).with_name("summarize_vhh_pilot.py"))}
    output.mkdir(parents=True, mode=0o700)
    atomic_write(output / "PILOT_PLAN.json", json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"plan": str(output / "PILOT_PLAN.json"), "plan_sha256": sha256_file(output / "PILOT_PLAN.json"),
                      "candidates": 4, "cdr3_lengths": [cell["cdr3_length"] for cell in cells]}))
    return 0


def require_ready(contract: dict, bindings: dict, receipts: list[dict]) -> dict:
    """Only an input-bound CPU and native pass permits the pilot."""
    gate = evaluate(contract, bindings, receipts)
    if gate.get("status") != "READY" or gate.get("next_stage") != "diversified_pilot":
        raise ValueError("real prerequisite gate does not permit diversified_pilot")
    return gate


def ensure_clean_repository(repo: Path) -> None:
    """Fail before creating pilot/cell output when tracked or untracked work exists."""
    result = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise ValueError("cannot verify pilot repository status")
    if result.stdout.strip():
        raise ValueError("pilot repository must be clean before creating any output")


def bounded_cell(command: list[str], log: Path, cwd: Path, timeout: float) -> tuple[int, bool]:
    """Kill the whole legacy process group within20seconds after a deadline."""
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            return 124, True


def run(args: argparse.Namespace) -> int:
    """Execute two cells once; leave scientific stage completion to the summarizer."""
    started, started_utc = time.monotonic(), utc_now()
    workspace, repo = args.workspace.resolve(strict=True), args.repo_root.resolve(strict=True)
    ensure_clean_repository(repo)
    plan, contract, bindings = json_object(args.plan), json_object(args.contract), json_object(args.bindings)
    prerequisite = [json_object(path) for path in args.prerequisite_receipts]
    prior_gate = require_ready(contract, bindings, prerequisite)
    binding = bindings["diversified_pilot"]
    if binding.get("pilot_plan_sha256") != sha256_file(args.plan):
        raise ValueError("pilot input binding identifies a different plan")
    if plan.get("schema") != "VHH_DIVERSE_PILOT_PLAN_V1" or plan["candidate_count"] != 4 or len(plan["cells"]) != 2:
        raise ValueError("expected a two-cell/four-candidate pilot")
    legacy = Path(__file__).with_name("run_owner_exploratory_cell.sh")
    if plan["legacy_runner_sha256"] != sha256_file(legacy):
        raise ValueError("legacy runner changed after registration")
    if plan["wrapper_sha256"] != sha256_file(Path(__file__)) or plan["summarizer_sha256"] != sha256_file(Path(__file__).with_name("summarize_vhh_pilot.py")):
        raise ValueError("pilot execution/scoring code changed after registration")
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("refusing to overwrite pilot attempt")
    # Revalidate only the eight direct files, never the workspace tree.
    for cell in plan["cells"]:
        current = spec_record(Path(cell["spec_path"]).parent.parent, cell["scaffold_id"], cell["cell_id"])
        if current != cell or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", cell["cell_id"]):
            raise ValueError("pilot source spec/annotation changed")
        if (workspace / "gpu_work/owner_mode/t8_exploratory_inference" / cell["cell_id"]).exists():
            raise ValueError("pilot cell_id already exists; no automatic rerun")
    output.mkdir(parents=True, mode=0o700)
    atomic_write(output / "PREREQUISITE_GATE.json", json.dumps(prior_gate, indent=2, sort_keys=True) + "\n")
    index = {**plan, "status": "RUNNING", "started_at_utc": started_utc, "cells": [],
             "input_binding_sha256": digest(binding), "protocol_sha256": digest(contract),
             "biological_pass": False, "scientific_stage_receipt_created": False}
    code = 1
    try:
        for position, cell in enumerate(plan["cells"]):
            remaining = 1800 - (time.monotonic() - started) - 75
            if remaining <= 1:
                raise RuntimeError("pilot total wall budget exhausted")
            print(f"PILOT_CELL_START index={position} cell={cell['cell_id']} remaining_seconds={remaining:.1f}", flush=True)
            command = ["bash", str(legacy), str(workspace), cell["cell_id"], cell["spec_path"], cell["checkpoint"], "2"]
            exit_code, timed_out = bounded_cell(command, output / f"cell_{position}.log", repo, remaining)
            attempts = sorted((workspace / "gpu_work/owner_mode/t8_exploratory_inference" / cell["cell_id"]).glob("attempt_*"))
            row = {**cell, "exit_code": exit_code, "timed_out": timed_out,
                   "attempt_root": str(attempts[0]) if len(attempts) == 1 else None}
            index["cells"].append(row)
            if exit_code != 0 or len(attempts) != 1:
                raise RuntimeError("pilot cell failed; preserve partial outputs, do not expand/retry")
            logs = attempts[0] / "operator_logs"
            receipt = json_object(logs / "EXPLORATORY_INFERENCE.json")
            if receipt.get("status") != "EXPLORATORY_INFERENCE_COMPLETE" or receipt.get("exit_code") != 0:
                raise RuntimeError("legacy terminal receipt not complete")
            verify_manifest(attempts[0], logs / "OUTPUT_SHA256SUMS")
            row["receipt_sha256"] = sha256_file(logs / "EXPLORATORY_INFERENCE.json")
        index["status"], code = "EXECUTION_COMPLETE_DIVERSITY_NOT_YET_ASSESSED", 0
    except Exception as exc:
        index["status"], index["error"] = "EXECUTION_FAILED", f"{type(exc).__name__}: {exc}"
        # A hard-killed legacy finalizer may not finish its after-run asset check.
        # On this failure path only, replay the four potentially used assets;
        # successful cells already verified them both before and after.
        try:
            runtime = workspace / "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
            manifest = dict(parse_manifest(runtime / "SHA256SUMS"))
            names = ("boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt", "boltz2_conf_final.ckpt", "mols.zip")
            index["failure_path_runtime_assets_after"] = verify_runtime(runtime, {name: manifest[name] for name in names})
        except Exception as verification_error:
            index["failure_path_runtime_verification_error"] = str(verification_error)
    finally:
        index["wall_seconds"] = time.monotonic() - started
        index["finished_at_utc"] = utc_now()
        if index["wall_seconds"] > 1800:
            index["status"], index["error"], code = "EXECUTION_FAILED", "outer wall budget exceeded", 1
        atomic_write(output / "INDEX.json", json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": index["status"], "index": str(output / "INDEX.json"), "wall_seconds": index["wall_seconds"]}))
    return code


def main() -> int:
    """CLI entrypoint with separate CPU preparation and explicit GPU execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    prepare_parser = subs.add_parser("prepare")
    for name in ("spec-root", "output"):
        prepare_parser.add_argument(f"--{name}", type=Path, required=True)
    prepare_parser.add_argument("--pose-rule", type=Path)
    prepare_parser.add_argument("--prefix", required=True)
    run_parser = subs.add_parser("run")
    for name in ("workspace", "repo-root", "plan", "output", "contract", "bindings"):
        run_parser.add_argument(f"--{name}", type=Path, required=True)
    run_parser.add_argument("--prerequisite-receipts", type=Path, nargs=2, required=True)
    args = parser.parse_args()
    return prepare(args) if args.action == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
