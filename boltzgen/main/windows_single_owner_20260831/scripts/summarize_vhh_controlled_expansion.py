#!/usr/bin/env python3
"""Aggregate one frozen controlled expansion without exposing candidate sequences.

Reads each small source once, computes generated/free contact descriptors, and
uses the independent source-anchored pose-v2 method. No GPU or biological gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import yaml
import gemmi

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E
import evaluate_vhh_pose_v2 as V
import summarize_vhh_pilot as P
from run_vhh_controlled_expansion import RULE, validate_plan, write_new
from vhh_stage_gate import candidate_set_digest


def source_reference(source, spec):
    """Resolve YAML label_asym_id to mmCIF author chain for Gemmi lookup."""
    selection = spec["entities"][0]["file"]["include"][0]["chain"]
    if selection.get("res_index") != "1..30":
        raise ValueError("source target selection must be the complete active chain")
    block = gemmi.cif.read_file(str(source)).sole_block()
    aliases = {row[1] for row in block.find("_atom_site.", ["label_asym_id", "auth_asym_id"]) if row[0] == selection["id"]}
    if len(aliases) != 1:
        raise ValueError("ambiguous or absent label-to-author target chain mapping")
    return V.load_reference(source, aliases.pop())


def aggregate(candidates):
    """Descriptive nested-fold aggregates; never cross-candidate repeat maps."""
    generated = [row["generated"]["samples"][0]["contacts"] for row in candidates]
    folds = [sample["contacts"] for row in candidates for sample in row["free_folds"]["samples"]]
    poses = [row["generated"]["samples"][0]["pose"] for row in candidates]
    clusters = V.descriptive_clusters(poses)
    return {"candidate_count": len(candidates), "fold_sample_count": len(folds),
        "generated_contacts": P.pooled_epitope_descriptors(generated),
        "free_fold_contacts": P.pooled_epitope_descriptors(folds),
        "candidate_all_free_folds_contact_counts": {name: sum(all(sample["contacts"][name] is True for sample in row["free_folds"]["samples"]) for row in candidates) for name in E.BOOLEAN_METRICS},
        "candidate_contact_map_jaccard_median_distribution": E._summary([row["free_folds"]["contact_summary"]["within_candidate_contact_map_jaccard"]["values_summary"]["median"] for row in candidates if row["free_folds"]["contact_summary"]["within_candidate_contact_map_jaccard"]["values_summary"]["median"] is not None]),
        "generated_pose_class_count_descriptive_only": clusters["class_count"],
        "generated_pose_class_sizes": [len(group) for group in clusters["groups"]],
        "generated_pose_applicable_count": clusters["applicable_count"],
        "generated_pose_not_applicable_count": clusters["not_applicable_count"],
        "free_fold_pose_applicable_count": sum(row["free_folds"]["pose_applicable_sample_count"] for row in candidates),
        "free_fold_pose_not_applicable_count": len(folds)-sum(row["free_folds"]["pose_applicable_sample_count"] for row in candidates),
        "generated_epitope_footprint_descriptive_counts": dict(Counter(P.footprint(row) for row in generated)),
        "candidate_developability_distributions": {name: E._summary([row["developability_descriptors"][name] for row in candidates]) for name in E.DEVELOPABILITY_METRICS}}


def summarize(index_path, output_dir):
    """Produce independently serialized private and aggregate-only public reports."""
    started = time.perf_counter()
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("summary output must be new")
    payload, index_binding = P.read_bound(index_path)
    index = json.loads(payload)
    # Run INDEX adds terminal fields to frozen cells; validate original plan
    # cells reconstructed with only their original, source-bound fields.
    frozen = dict(index)
    frozen["cells"] = [{key: value for key, value in cell.items() if key not in
        {"exit_code", "timed_out", "attempt_root", "receipt_sha256", "terminal_status_sha256", "legacy_filter_pass_count"}}
        for cell in index["cells"]]
    validate_plan(frozen)
    if index["status"] != "GPU_COMPLETE_PENDING_DESCRIPTIVE_ANALYSIS":
        raise ValueError("cannot summarize incomplete/failed GPU execution as complete")
    inputs, candidates, target_hashes = [index_binding], [], set()
    for cell in index["cells"]:
        if cell["exit_code"] != 0 or cell["timed_out"] is not False:
            raise ValueError("cell did not complete")
        source = Path(cell["spec_path"]).parent
        bound = {}
        for name, expected in cell["spec_hashes"].items():
            content, receipt = P.read_bound(source / name, expected)
            inputs.append(receipt)
            bound[name] = content
        target_hashes.add(cell["spec_hashes"]["target.cif"])
        spec = yaml.safe_load(bound["design.yaml"])
        reference = source_reference(source / "target.cif", spec)
        cdr_tokens, cdr3_length = P.cdr_annotation(yaml.safe_load(bound["scaffold.yaml"]))
        root, receipt = P.fold_design_root(cell["attempt_root"])
        inputs.append(receipt)
        if root == output_dir or root in output_dir.parents:
            raise ValueError("summary may not be written into raw candidate directory")
        receipt_payload, receipt_binding = P.read_bound(Path(cell["attempt_root"]) / "operator_logs/EXPLORATORY_INFERENCE.json", cell["receipt_sha256"])
        inputs.append(receipt_binding)
        terminal = json.loads(receipt_payload)
        status_payload, status_binding = P.read_bound(Path(cell["attempt_root"]) / "operator_logs/STATUS.txt", cell["terminal_status_sha256"])
        inputs.append(status_binding)
        if (terminal.get("status") != "EXPLORATORY_INFERENCE_COMPLETE" or terminal.get("exit_code") != 0
                or terminal.get("cuda_oom_detected") is not False
                or terminal.get("output_validation", {}).get("status") != "PASS"
                or status_payload.decode().strip() != "EXPLORATORY_INFERENCE_COMPLETE"
                or terminal["observed_designs"] != 2 or terminal["fold_samples_per_candidate"] != 5):
            raise ValueError("terminal validation/status/counts differ from frozen rule")
        designs = sorted(root.glob("design_*.npz"))
        if len(designs) != 2 or {p.name for p in designs} != {p.name for p in (root / "fold_out_npz").glob("design_*.npz")}:
            raise ValueError("candidate/fold output closure mismatch")
        for path in designs:
            design, db = E._load_npz(path)
            fold, fb = E._load_npz(root / "fold_out_npz" / path.name)
            inputs.extend((db, fb))
            if not np.array_equal(np.flatnonzero(design["design_mask"]), cdr_tokens) or len(fold["coords"]) != 5:
                raise ValueError("actual CDR annotation or repeats differ from plan")
            generated = V.evaluate_fold(design, fold, reference, usage="PROSPECTIVE_EXPLORATORY", geometry_source="generated_input")
            free = V.evaluate_fold(design, fold, reference, usage="PROSPECTIVE_EXPLORATORY", geometry_source="free_fold_samples")
            residue_ids = np.asarray(fold["res_type"])[0].argmax(axis=-1)
            sequence = "".join(P.CANONICAL_ONE_LETTER[int(residue_id)-2] for residue_id in residue_ids[30:])
            candidates.append({"candidate_id": cell["cell_id"] + "/" + path.stem,
                "vhh_sequence_sha256": hashlib.sha256(sequence.encode("ascii")).hexdigest(),
                "scaffold_id": cell["scaffold_id"], "cdr3_length": cdr3_length,
                "generated": generated, "free_folds": free,
                "developability_descriptors": E.developability_descriptors(design, fold)})
    if len(target_hashes) != 1 or len(candidates) != 6:
        raise ValueError("source target identity or total candidate count inconsistent")
    unique_count = len({row["vhh_sequence_sha256"] for row in candidates})
    public = {"schema": "VHH_CONTROLLED_EXPANSION_SUMMARY_V1", "status": "DESCRIPTIVE_ANALYSIS_COMPLETE",
        "experiment_rule": RULE, "pose_rule": V.POSE_V2_RULE,
        "registration": "PROSPECTIVE_EXPLORATORY_NEW_EXPERIMENT", "biological_pass": False,
        "source_target_sha256": next(iter(target_hashes)), "unique_sequence_count": unique_count,
        "duplicate_sequence_count": 6-unique_count, "all_candidates_sequences_unique": unique_count == 6,
        "overall": aggregate(candidates),
        "by_scaffold": {label: aggregate([row for row in candidates if row["scaffold_id"] == label]) for label in RULE["scaffolds"]},
        "gpu_wall_seconds": index["wall_seconds"],
        "legacy_filter_pass_count": sum(row["legacy_filter_pass_count"] for row in index["cells"]),
        "weights_verified_unchanged_by_each_cell_terminal_finalizer": True,
        "old_pilot_status": "BLOCKED_UNCHANGED", "paired_gpu_executed": False,
        "next_action": "REVIEW_MECHANISTIC_DESCRIPTORS_NO_AUTOMATIC_DOWNSTREAM_GPU",
        "implementation_sha256": index["implementation_sha256"],
        "limitations": ["Two candidates per framework are not statistically sufficient to rank frameworks or estimate a binding success rate.",
            "All six generated candidates are retained; duplicate sequences, if present, are reported and not independent evidence.",
            "7XL0 and 8IM0 match source-defined CDR lengths; framework sequence, loop stems and structural context still differ.",
            "6APO has a different CDR3 length and is an exploratory reference, not a length-matched control.",
            "Binding metadata are a trained soft condition, not a guaranteed contact constraint.",
            "Pose-v2 applicability and class counts are engineering descriptors, not biological pass/fail.",
            "Hydrophobic composition is not exposed surface area; KR-DE is not net charge; short distance diagnostics are not atom-type-aware clash checks.",
            "Terminal chemical modifications remain NOT_ATOMICALLY_VERIFIED; no affinity or selectivity claim is permitted."],
        "analysis_seconds": time.perf_counter()-started}
    private = {**public, "inputs": inputs, "candidates": candidates,
        "actual_candidate_set_sha256": candidate_set_digest(candidates) if unique_count == 6 else None}
    # Validate both objects before creating the output directory.
    json.dumps(private, allow_nan=False)
    write_new(output_dir / "PRIVATE_SUMMARY.json", private)
    write_new(output_dir / "PUBLIC_SUMMARY.json", public)
    return public


def main():
    """CPU-only new-directory summary entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.index, args.output)
    print(json.dumps({"status": result["status"], "overall": result["overall"], "by_scaffold": result["by_scaffold"]}))


if __name__ == "__main__":
    main()
