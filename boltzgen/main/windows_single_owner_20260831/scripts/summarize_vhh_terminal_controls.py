#!/usr/bin/env python3
"""Strict all-atom summary of three predeclared terminal-conditioning controls.

One complete scaffold, two generated candidates per condition, five nested free
folds per candidate. No partial-atom fallback, GPU, sequence publication or
biological acceptance decision. Pose V2 is descriptive only.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E
import evaluate_vhh_pose_v2 as V
import summarize_vhh_pilot as P
import summarize_vhh_controlled_expansion as S
from vhh_stage_gate import candidate_set_digest

CONDITIONS = ("A", "B", "C")
REQUIRED_IMPLEMENTATIONS = {
    "summarize_vhh_terminal_controls.py", "run_vhh_terminal_controls.py",
    "evaluate_vhh_epitope.py", "evaluate_vhh_pose_v2.py", "summarize_vhh_pilot.py",
    "summarize_vhh_controlled_expansion.py", "vhh_stage_gate.py",
}


def validate_frozen_plan(index):
    # Deferred import lets runner/summary be prepared independently without a
    # circular import. The runner owns the exact protocol and CPU-evidence rule.
    from run_vhh_terminal_controls import validate_plan
    if index.get("schema") != "VHH_TERMINAL_CONTROL_PLAN_V1" or index.get("status") != "GPU_COMPLETE_PENDING_STRICT_ANALYSIS":
        raise ValueError("only the completed terminal-control execution can be summarized")
    frozen_cells = index.get("frozen_cells")
    if not isinstance(frozen_cells, list) or len(frozen_cells) != 3:
        raise ValueError("INDEX must retain the three original frozen cells")
    validate_plan({**index, "cells": frozen_cells}, recheck_inputs=False)
    if len(index["cells"]) != 3:
        raise ValueError("exactly three terminal cells required")
    for frozen, terminal in zip(frozen_cells, index["cells"]):
        if any(terminal.get(key) != value for key, value in frozen.items()):
            raise ValueError("terminal cell changed a frozen input field")
    if tuple(cell["condition_id"] for cell in index["cells"]) != CONDITIONS:
        raise ValueError("conditions must be exactly A, B, C in frozen order")


def evaluate_candidate(design, fold, reference, *, expected_target_binding_types, cdr_tokens):
    """Strict candidate evaluation; any missing atom remains a fatal error."""
    mask = np.asarray(design["design_mask"])
    if not np.array_equal(np.flatnonzero(mask), cdr_tokens):
        raise ValueError("actual CDR annotation differs from the shared scaffold source")
    binding = np.asarray(design["binding_type"])
    expected = np.asarray(expected_target_binding_types)
    if expected.shape != (30,) or not np.issubdtype(expected.dtype, np.integer):
        raise ValueError("frozen target binding types must be thirty verified integers")
    if binding.shape != mask.shape or not np.array_equal(binding[:30], expected) or np.any(binding[30:] != 0):
        raise ValueError("actual candidate binding metadata differs from the frozen condition")
    if np.asarray(fold["coords"]).shape[0] != 5:
        raise ValueError("exactly five free folds per candidate required")
    # V.evaluate_fold calls frozen E.evaluate_arrays for BOTH coordinate sets.
    # There is deliberately no except/fallback to an observed/partial method.
    generated = V.evaluate_fold(design, fold, reference, usage="PROSPECTIVE_EXPLORATORY", geometry_source="generated_input")
    free = V.evaluate_fold(design, fold, reference, usage="PROSPECTIVE_EXPLORATORY", geometry_source="free_fold_samples")
    ids = np.asarray(fold["res_type"])[0].argmax(axis=-1)
    sequence = "".join(P.CANONICAL_ONE_LETTER[int(index)-2] for index in ids[30:])
    return {"vhh_sequence_sha256": hashlib.sha256(sequence.encode("ascii")).hexdigest(),
        "generated": generated, "free_folds": free,
        "developability_descriptors": E.developability_descriptors(design, fold),
        "strict_all_atom_validation": True, "partial_fallback_used": False,
        "condition_metadata_matches_frozen_input": True}


def condition_aggregate(candidates):
    if len(candidates) != 2 or any(len(row["free_folds"]["samples"]) != 5 for row in candidates):
        raise ValueError("condition requires two candidates, each with five free folds")
    result = S.aggregate(candidates)
    result["denominators"] = {"generated_candidates": 2, "free_fold_samples": 10,
        "candidates_for_five_of_five_terminal_contact": 2, "folds_nested_within_each_candidate": 5}
    result["generated_both_terminal_contact_candidate_count"] = sum(row["generated"]["samples"][0]["contacts"]["both_epitope_contacts"] is True for row in candidates)
    result["candidate_five_of_five_both_terminal_contact_count"] = sum(all(sample["contacts"]["both_epitope_contacts"] is True for sample in row["free_folds"]["samples"]) for row in candidates)
    result["unique_vhh_sequence_count"] = len({row["vhh_sequence_sha256"] for row in candidates})
    result["candidate_jaccard_undefined_median_count"] = sum(row["free_folds"]["contact_summary"]["within_candidate_contact_map_jaccard"]["values_summary"]["median"] is None for row in candidates)
    return result


def validate_strict_output_check(cell, expected_scope):
    check = cell.get("strict_output_check", {})
    if (check.get("status") != "PASS" or check.get("candidate_count") != 2
            or check.get("free_fold_sample_count") != 10 or check.get("scope") != expected_scope
            or check.get("biological_pass") is not False):
        raise ValueError("terminal strict all-atom check is absent or differs from frozen scope")


def summarize(index_path: Path, output_dir: Path):
    started = time.perf_counter()
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("summary output directory must be new")
    raw, index_binding = P.read_bound(index_path)
    index = json.loads(raw)
    validate_frozen_plan(index)
    implementations = index["implementation_sha256"]
    if not REQUIRED_IMPLEMENTATIONS.issubset(implementations):
        raise ValueError("frozen implementation manifest omits a strict-summary dependency")
    inputs = [index_binding]
    for name, digest in implementations.items():
        if Path(name).name != name:
            raise ValueError("implementation binding must use sibling filenames")
        _, receipt = P.read_bound(Path(__file__).with_name(name), digest)
        inputs.append(receipt)
    candidates, target_hashes, scaffold_hashes = [], set(), set()
    for cell in index["cells"]:
        if cell["exit_code"] != 0 or cell["timed_out"] is not False or cell["num_designs"] != 2:
            raise ValueError("condition cell failed, timed out or changed generation count")
        validate_strict_output_check(cell, index["rule"]["evaluation_scope"])
        if cell["scaffold_id"] != "01_pdb_00007xl0-A":
            raise ValueError("all conditions require the same complete 7XL0 scaffold")
        source = Path(cell["spec_path"]).parent
        bound = {}
        for name, expected in cell["spec_hashes"].items():
            data, receipt = P.read_bound(source/name, expected)
            inputs.append(receipt)
            bound[name] = data
        if not {"design.yaml", "target.cif", "scaffold.yaml", "scaffold.cif"}.issubset(bound):
            raise ValueError("source binding closure is incomplete")
        target_hashes.add(cell["spec_hashes"]["target.cif"])
        scaffold_hashes.add((cell["spec_hashes"]["scaffold.cif"], cell["spec_hashes"]["scaffold.yaml"]))
        reference = S.source_reference(source/"target.cif", yaml.safe_load(bound["design.yaml"]))
        cdr_tokens, cdr3_length = P.cdr_annotation(yaml.safe_load(bound["scaffold.yaml"]))
        root, receipt = P.fold_design_root(cell["attempt_root"])
        inputs.append(receipt)
        if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
            raise ValueError("output may not be placed inside source candidates")
        data, receipt = P.read_bound(Path(cell["attempt_root"])/"operator_logs/EXPLORATORY_INFERENCE.json", cell["receipt_sha256"])
        inputs.append(receipt)
        terminal = json.loads(data)
        status, receipt = P.read_bound(Path(cell["attempt_root"])/"operator_logs/STATUS.txt", cell["terminal_status_sha256"])
        inputs.append(receipt)
        if (terminal.get("status") != "EXPLORATORY_INFERENCE_COMPLETE" or terminal.get("exit_code") != 0
                or terminal.get("cuda_oom_detected") is not False or terminal.get("output_validation", {}).get("status") != "PASS"
                or terminal.get("observed_designs") != 2 or terminal.get("fold_samples_per_candidate") != 5
                or status.decode().strip() != "EXPLORATORY_INFERENCE_COMPLETE"):
            raise ValueError("terminal status or actual candidate/fold count failed verification")
        designs = sorted(root.glob("design_*.npz"))
        if len(designs) != 2 or {path.name for path in designs} != {path.name for path in (root/"fold_out_npz").glob("design_*.npz")}:
            raise ValueError("strict candidate/fold file closure mismatch")
        for path in designs:
            design, db = E._load_npz(path)
            fold, fb = E._load_npz(root/"fold_out_npz"/path.name)
            inputs.extend((db, fb))
            result = evaluate_candidate(design, fold, reference,
                expected_target_binding_types=cell["expected_target_binding_types"], cdr_tokens=cdr_tokens)
            candidates.append({"candidate_id": cell["cell_id"]+"/"+path.stem,
                "condition_id": cell["condition_id"], "scaffold_id": cell["scaffold_id"], "cdr3_length": cdr3_length, **result})
    if len(candidates) != 6 or len(target_hashes) != 1 or len(scaffold_hashes) != 1:
        raise ValueError("total count, shared source target or shared scaffold identity mismatch")
    unique_count = len({row["vhh_sequence_sha256"] for row in candidates})
    # Build the public object explicitly; never publish candidate objects and
    # attempt to redact sequences/paths afterward.
    public = {"schema": "VHH_TERMINAL_CONTROL_SUMMARY_V1", "status": "STRICT_ALL_ATOM_DESCRIPTIVE_ANALYSIS_COMPLETE",
        "registration": "PROSPECTIVE_EXPLORATORY_TERMINAL_CONDITION_CONTROLS",
        "biological_pass": False, "partial_fallback_used": False, "strict_all_atom_validation": True,
        "condition_ids": list(CONDITIONS), "scaffold_id": "01_pdb_00007xl0-A",
        "candidate_count": 6, "fold_sample_count": 30, "unique_sequence_count": unique_count,
        "duplicate_sequence_count": 6-unique_count, "source_target_sha256": next(iter(target_hashes)),
        "by_condition": {condition: condition_aggregate([row for row in candidates if row["condition_id"] == condition]) for condition in CONDITIONS},
        "contact_threshold_angstrom": 4.5, "pose_v2_role": "DIAGNOSTIC_ONLY_NOT_A_CONTINUATION_GATE",
        "new_downstream_gpu_authorized_by_summary": False,
        "gpu_wall_seconds": index["wall_seconds"], "legacy_filter_pass_count": sum(cell["legacy_filter_pass_count"] for cell in index["cells"]),
        "implementation_sha256": implementations,
        "limitations": ["Two generated candidates per condition are a small exploration budget, not statistical sufficiency or a binding success-rate estimate.",
            "Five free folds are nested repeats of one candidate, not five independent molecules; no cross-condition seed pairing is assumed.",
            "All generation draws are retained; repeated sequences are reported and are not independent sequence evidence.",
            "Generated conditioning is a trained soft signal only if the separate CPU preflight establishes support; metadata alone does not prove an effect.",
            "His/Ala contact and five-of-five contact are descriptive geometry, not binding or selectivity validation.",
            "The below-1.5-Angstrom diagnostic covers target-CDR heavy atoms only, not atom-type-aware clashes or all-VHH overlap.",
            "Pose applicability and class counts are diagnostic only; failed pose comparability does not discard valid contact evaluation.",
            "Terminal chemistry remains NOT_ATOMICALLY_VERIFIED; this experiment does not compare active and truncated GLP1 states."],
        "analysis_seconds": time.perf_counter()-started}
    private = {**public, "inputs": inputs, "candidates": candidates,
        "actual_candidate_set_sha256": candidate_set_digest(candidates) if unique_count == 6 else None}
    encoded = {"PRIVATE_TERMINAL_CONTROL_SUMMARY.json": json.dumps(private, indent=2, sort_keys=True, allow_nan=False),
        "PUBLIC_TERMINAL_CONTROL_SUMMARY.json": json.dumps(public, indent=2, sort_keys=True, allow_nan=False)}
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in encoded.items():
        with (output/name).open("x", encoding="utf-8") as stream:
            stream.write(payload+"\n")
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = summarize(args.index, args.output)
    print(json.dumps({key: result[key] for key in ("status", "candidate_count", "fold_sample_count", "strict_all_atom_validation")}))


if __name__ == "__main__":
    main()
