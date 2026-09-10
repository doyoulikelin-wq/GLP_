#!/usr/bin/env python3
"""Preserve strict failures and separately describe the complete observed interface.

Input: frozen 20260910 INDEX and six-scaffold public registry. Output: new private
directory with private evidence, public aggregates and ordinary stage timings.
This post-run method repair does not modify old rules, atoms or GPU results.
Exits on source mismatch, incomplete CDR/target atoms or changed framework.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import yaml

OWNER = Path(__file__).resolve().parents[2] / "main/windows_single_owner_20260831"
sys.path.insert(0, str(OWNER / "scripts"))
import evaluate_vhh_epitope as E
import evaluate_vhh_pose_v2 as V
import summarize_vhh_pilot as P
import summarize_vhh_controlled_expansion as S
from evaluate_vhh_observed_interface import evaluate_observed_interface
from run_vhh_controlled_expansion import RULE, validate_plan, write_new


def execute(index_path, library_path, output):
    """Use the frozen strict evaluator unchanged; local diagnostics are separate."""
    started = time.perf_counter()
    if output.exists() or output.is_symlink():
        raise ValueError("output directory must be new")
    raw, binding = P.read_bound(index_path)
    index = json.loads(raw)
    frozen = dict(index)
    extra = {"exit_code", "timed_out", "attempt_root", "receipt_sha256", "terminal_status_sha256", "legacy_filter_pass_count"}
    frozen["cells"] = [{key: value for key, value in cell.items() if key not in extra} for cell in index["cells"]]
    validate_plan(frozen)
    if index["status"] != "GPU_COMPLETE_PENDING_DESCRIPTIVE_ANALYSIS":
        raise ValueError("requires a completed GPU run, not failed/incomplete execution")
    payload, library_binding = P.read_bound(library_path)
    library = {row["scaffold_id"]: row for row in json.loads(payload)["records"]}
    evidence, strict, local, failures, timings = [binding, library_binding], [], [], [], []
    for cell in index["cells"]:
        source, attempt = Path(cell["spec_path"]).parent, Path(cell["attempt_root"])
        for name, expected in cell["spec_hashes"].items():
            _, row = P.read_bound(source / name, expected)
            evidence.append(row)
        payload, row = P.read_bound(attempt / "operator_logs/EXPLORATORY_INFERENCE.json", cell["receipt_sha256"])
        evidence.append(row)
        terminal = json.loads(payload)
        payload, row = P.read_bound(attempt / "operator_logs/STATUS.txt", cell["terminal_status_sha256"])
        evidence.append(row)
        if (cell["exit_code"] != 0 or cell["timed_out"] or terminal["status"] != "EXPLORATORY_INFERENCE_COMPLETE"
                or terminal["exit_code"] != 0 or terminal["cuda_oom_detected"] or terminal["output_validation"]["status"] != "PASS"
                or payload.decode().strip() != "EXPLORATORY_INFERENCE_COMPLETE"):
            raise ValueError("terminal execution and used-weight validation failed")
        stage_seconds = {}
        for name in ("configure", "resolved_config_validation", "design", "inverse_folding", "folding", "analysis", "filtering", "validation"):
            data, row = P.read_bound(attempt / "operator_logs" / f"{name}.duration_seconds.txt")
            value = float(data.decode())
            if not np.isfinite(value) or value < 0:
                raise ValueError("invalid recorded stage duration")
            evidence.append(row)
            stage_seconds[name] = value
        timings.append({"scaffold_id": cell["scaffold_id"], "recorded_stage_seconds": stage_seconds})
        spec = yaml.safe_load((source / "design.yaml").read_text())
        reference = S.source_reference(source / "target.cif", spec)
        cdr_tokens, cdr3 = P.cdr_annotation(yaml.safe_load((source / "scaffold.yaml").read_text()))
        root, row = P.fold_design_root(attempt)
        evidence.append(row)
        designs = sorted(root.glob("design_*.npz"))
        if len(designs) != 2 or {p.name for p in designs} != {p.name for p in (root / "fold_out_npz").glob("design_*.npz")}:
            raise ValueError("candidate/fold closure differs from frozen count")
        for path in designs:
            design, row = E._load_npz(path)
            evidence.append(row)
            fold, row = E._load_npz(root / "fold_out_npz" / path.name)
            evidence.append(row)
            if len(fold["coords"]) != 5 or not np.array_equal(np.flatnonzero(design["design_mask"]), cdr_tokens):
                raise ValueError("actual repeat/CDR mask mismatch")
            ids = np.asarray(fold["res_type"])[0].argmax(axis=-1)
            sequence = "".join(P.CANONICAL_ONE_LETTER[int(residue)-2] for residue in ids[30:])
            framework = "".join(letter for token, letter in enumerate(sequence, 30) if not design["design_mask"][token])
            if hashlib.sha256(framework.encode()).hexdigest() != library[cell["scaffold_id"]]["framework_sequence_sha256"]:
                raise ValueError("actual framework sequence differs from selected source")
            base = {"candidate_id": cell["cell_id"]+"/"+path.stem, "scaffold_id": cell["scaffold_id"], "cdr3_length": cdr3,
                "vhh_sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                "framework_sequence_preserved": True, "developability_descriptors": E.developability_descriptors(design, fold)}
            try:
                strict.append({**base,
                    "generated": V.evaluate_fold(design, fold, reference, usage="PROSPECTIVE_EXPLORATORY"),
                    "free_folds": V.evaluate_fold(design, fold, reference, usage="PROSPECTIVE_EXPLORATORY", geometry_source="free_fold_samples")})
            except E.ValidationError as exc:
                failures.append({"candidate_id": base["candidate_id"], "scaffold_id": base["scaffold_id"],
                    "strict_status": "NOT_ASSESSABLE", "reason": str(exc)})
            local.append({**base, "generated": evaluate_observed_interface(design, fold, reference),
                "free_folds": evaluate_observed_interface(design, fold, reference, geometry_source="free_fold_samples")})
    if len(local) != 6:
        raise ValueError("expected six generated candidates")
    all_unique = len({row["vhh_sequence_sha256"] for row in local}) == 6
    by_scaffold = lambda rows: {label: S.aggregate([row for row in rows if row["scaffold_id"] == label]) if any(row["scaffold_id"] == label for row in rows) else None for label in RULE["scaffolds"]}
    public = {"schema": "VHH_EXPANSION_PARTIAL_DIAGNOSTIC_V1", "status": "COMPUTATION_COMPLETE_STRICT_COMPARISON_INCOMPLETE",
        "biological_pass": False, "candidate_count": 6, "fold_sample_count": 30,
        "all_framework_sequences_preserved": True, "all_candidate_sequences_unique": all_unique,
        "strict_evaluation": {"implementation_unchanged": True, "assessable_candidates": len(strict), "not_assessable_candidates": len(failures),
            "by_scaffold": by_scaffold(strict), "failure_counts_by_scaffold": {label: sum(row["scaffold_id"] == label for row in failures) for label in RULE["scaffolds"]}},
        "supplementary_observed_interface": {"classification": "RETROSPECTIVE_PARTIAL_OBSERVED_INTERFACE",
            "not_replacement_for_strict_results": True, "overall": S.aggregate(local), "by_scaffold": by_scaffold(local),
            "missing_framework_sidechain_atoms_per_candidate": sorted(row["generated"]["missing_framework_sidechain_atom_count"] for row in local),
            "all_vhh_heavy_atom_distance_status": "NOT_ASSESSED"},
        "execution": {"wall_seconds": index["wall_seconds"], "started_at_utc": index["started_at_utc"], "finished_at_utc": index["finished_at_utc"],
            "legacy_filter_pass_count": sum(row["legacy_filter_pass_count"] for row in index["cells"]), "legacy_filter_candidate_count": 6,
            "weights_unchanged_by_successful_terminal_verification": True, "stage_timings": timings,
            "timing_note": "Recorded stage durations are not the whole wall clock; remainder includes orchestration and input/output checks and is not attributed entirely to hashing."},
        "old_pilot_status": "BLOCKED_UNCHANGED", "automatic_additional_gpu": False,
        "next_action": "FIX_PREFLIGHT_EVALUATOR_SCOPE_MISMATCH_BEFORE_ANY_FURTHER_GPU",
        "post_run_implementation_sha256": {"exporter": P.read_bound(Path(__file__))[1]["sha256"],
            "observed_interface": P.read_bound(OWNER / "scripts/evaluate_vhh_observed_interface.py")[1]["sha256"]},
        "limitations": ["Scaffold input readiness tolerated missing framework sidechains, while the frozen full-atom evaluator did not; this preparation/evaluation compatibility gap was discovered after execution.",
            "8IM0 is not restored to full-atom validity. Only complete target/CDR atoms and resolved backbone enter the supplementary diagnostic.",
            "No missing atom is marked resolved or imputed; full-VHH heavy-atom distance and missing framework-sidechain contacts are not assessed.",
            "Two candidates per scaffold cannot establish scaffold superiority, affinity or selectivity.",
            "All 30 folds remain nested within six candidates. Supplementary post-run method repair is not preregistered validation.",
            "Soft conditioning does not guarantee terminal contacts; terminal chemistry remains NOT_ATOMICALLY_VERIFIED."],
        "analysis_seconds": time.perf_counter()-started}
    private = {**public, "inputs": evidence, "strict_candidates": strict, "strict_failures": failures, "local_candidates": local}
    json.dumps(private, allow_nan=False)
    write_new(output / "PRIVATE_DIAGNOSTIC.json", private)
    write_new(output / "PUBLIC_DIAGNOSTIC.json", public)
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = execute(args.index, args.library, args.output)
    print(json.dumps({"status": result["status"], "strict_assessable": result["strict_evaluation"]["assessable_candidates"],
        "local_counts": result["supplementary_observed_interface"]["overall"]["free_fold_contacts"]["counts"]}))


if __name__ == "__main__":
    main()
