#!/usr/bin/env python3
"""CPU-only integrity/diversity summary of a bounded VHH design pilot.

Generated-design pose diversity is distinct from N/M/C epitope coverage and
from free-fold repeat stability. None of these descriptors is biological PASS.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E
from vhh_stage_gate import candidate_set_digest

POSE_RULE = {
    "geometry_source": "fold_npz_input_coords_generated_design",
    "alignment": "target_per_residue_backbone_centroid_kabsch",
    "target_reference_rmsd_tolerance_angstrom": 0.001,
    "position_separation_angstrom": 8.0,
    "direction_separation_degrees": 45.0,
    "direction": "framework_backbone_centroid_to_cdr_backbone_centroid",
    "clustering": "deterministic_complete_link_greedy",
    "fold_assignment": "nearest_generated_class_representative_within_both_thresholds_else_unassigned",
    "classification_role": "exploratory_relative_geometry_not_biological_pass",
}
TARGET_COUNT = 30
CANONICAL_ONE_LETTER = "ARNDCQEGHILKMFPSTWYV"


def read_bound(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("bound source must be a nonsymlink regular file")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("bound source hash mismatch")
    return data, {"path": str(path.resolve()), "sha256": digest, "size_bytes": len(data)}


def cdr_annotation(scaffold_yaml):
    """Fixed-length source label_seq_id ranges, in CDR1/CDR2/CDR3 order."""
    if any(scaffold_yaml.get(key) for key in ("exclude", "design_insertions")):
        raise ValueError("insertions/exclusions require a separate explicit CDR remapping")
    entries = scaffold_yaml.get("design", [])
    if len(entries) != 1:
        raise ValueError("one source VHH CDR chain annotation required")
    text = entries[0]["chain"]["res_index"]
    if not isinstance(text, str) or not re.fullmatch(r"\d+\.\.\d+,\d+\.\.\d+,\d+\.\.\d+", text):
        raise ValueError("source must identify three ordered fixed CDR ranges")
    ranges = [tuple(map(int, part.split(".."))) for part in text.split(",")]
    if any(start < 1 or end < start for start, end in ranges) or any(ranges[i][1] >= ranges[i+1][0] for i in (0, 1)):
        raise ValueError("CDR source ranges must be positive, ordered and disjoint")
    tokens = [TARGET_COUNT + index - 1 for start, end in ranges for index in range(start, end + 1)]
    return tokens, ranges[2][1] - ranges[2][0] + 1


def backbone_geometry(fold, design_mask):
    mapping = np.asarray(fold["atom_to_token"])[0]
    valid = mapping.sum(axis=1) == 1
    backbone = E._binary(fold["backbone_mask"], (1, len(valid)), "backbone_mask")[0]
    if np.any(backbone & ~valid):
        raise ValueError("backbone contains padding")
    token = mapping.argmax(axis=1)
    if not np.all(np.bincount(token[backbone], minlength=len(design_mask)) == 4):
        raise ValueError("four modeled backbone atoms per residue required")
    cdr = backbone & np.asarray(design_mask, dtype=bool)[token]
    framework = backbone & (token >= TARGET_COUNT) & ~cdr
    if not cdr.any() or not framework.any():
        raise ValueError("CDR and framework backbone must both be nonempty")
    return token, backbone, cdr, framework


def target_centres(coords, token, backbone):
    return np.stack([coords[backbone & (token == index)].mean(axis=0) for index in range(TARGET_COUNT)])


def describe_pose(coords, geometry, reference, require_same_target=False):
    token, backbone, cdr, framework = geometry
    xyz = np.asarray(coords, dtype=float)
    if not np.isfinite(xyz).all():
        raise ValueError("nonfinite pose coordinates")
    target = target_centres(xyz, token, backbone)
    mobile, fixed = target - target.mean(axis=0), reference - reference.mean(axis=0)
    if np.linalg.matrix_rank(mobile) < 2 or np.linalg.matrix_rank(fixed) < 2:
        raise ValueError("degenerate target alignment")
    u, _, vt = np.linalg.svd(mobile.T @ fixed)
    correction = np.eye(3)
    correction[-1, -1] = np.linalg.det(u @ vt)
    rotation = u @ correction @ vt
    rmsd = float(np.sqrt(np.mean(np.sum((mobile @ rotation - fixed) ** 2, axis=1))))
    if require_same_target and rmsd > POSE_RULE["target_reference_rmsd_tolerance_angstrom"]:
        raise ValueError("generated target conformations differ; cannot directly compare poses")
    centre = (xyz[cdr].mean(axis=0) - target.mean(axis=0)) @ rotation
    direction = (xyz[cdr].mean(axis=0) - xyz[framework].mean(axis=0)) @ rotation
    length = np.linalg.norm(direction)
    if not np.isfinite(length) or length < 1e-6:
        raise ValueError("framework-to-CDR direction is undefined")
    return {"cdr_backbone_centre_angstrom": centre.tolist(), "framework_to_cdr_unit_direction": (direction / length).tolist(), "target_alignment_rmsd_angstrom": rmsd}


def separation(left, right):
    distance = float(np.linalg.norm(np.asarray(left["cdr_backbone_centre_angstrom"]) - right["cdr_backbone_centre_angstrom"]))
    dot = np.dot(left["framework_to_cdr_unit_direction"], right["framework_to_cdr_unit_direction"])
    angle = math.degrees(math.acos(float(np.clip(dot, -1.0, 1.0))))
    return distance, angle


def same_pose(left, right):
    distance, angle = separation(left, right)
    return distance < POSE_RULE["position_separation_angstrom"] and angle < POSE_RULE["direction_separation_degrees"]


def cluster_poses(poses):
    """Stable input order; every member of a cluster must be mutually close."""
    clusters = []
    for index, pose in enumerate(poses):
        for cluster in clusters:
            if all(same_pose(pose, poses[member]) for member in cluster):
                cluster.append(index)
                break
        else:
            clusters.append([index])
    return clusters


def assign_fold(pose, representatives):
    options = []
    for index, representative in enumerate(representatives):
        distance, angle = separation(pose, representative)
        if same_pose(pose, representative):
            options.append((distance / 8.0 + angle / 45.0, index))
    return f"P{min(options)[1] + 1}" if options else "unassigned"


def footprint(row):
    counts = Counter("N" if target < 10 else "M" if target < 20 else "C" for target, _ in row["contact_residue_pairs_zero_based"])
    if not counts:
        return "none"
    maximum = max(counts.values())
    winners = [key for key, value in counts.items() if value == maximum]
    return winners[0] if len(winners) == 1 else "mixed_tie"


def summarize_index(index_path: Path, output_dir: Path):
    started = time.perf_counter()
    output = output_dir.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output directory must be new")
    raw_index, index_receipt = read_bound(index_path)
    index = json.loads(raw_index)
    rule = index.get("pose_classification", {})
    if any(rule.get(key) != value for key, value in POSE_RULE.items()):
        raise ValueError("pose classification was not bound to the fixed declared rule")
    cells = index.get("cells", [])
    if not cells or len({cell["cell_id"] for cell in cells}) != len(cells):
        raise ValueError("nonempty uniquely named cells required")
    expected_folds = index.get("expected_folds_per_candidate", index.get("folds_per_candidate", 5))
    if expected_folds != 5:
        raise ValueError("current pilot requires five free folds per candidate")
    inputs, candidates, target_hashes = [index_receipt], [], set()
    reference, target_identity = None, None
    for cell in sorted(cells, key=lambda row: row["cell_id"]):
        if cell.get("exitcode", cell.get("exit_code")) != 0:
            raise ValueError("pilot cell did not complete successfully")
        scaffold = cell["scaffold_id"]
        if not re.fullmatch(r"\d{2}_pdb_[0-9a-z]{8}-[A-Za-z0-9]+", scaffold):
            raise ValueError("public scaffold label must identify a curated PDB scaffold")
        source = Path(cell["spec_path"])
        source = source.parent if source.is_file() else source
        bound_sources = {}
        for name in ("design.yaml", "scaffold.yaml", "target.cif", "scaffold.cif"):
            payload, receipt = read_bound(source / name, cell["spec_hashes"][name])
            bound_sources[name] = payload
            inputs.append(receipt)
        target_hashes.add(hashlib.sha256(bound_sources["target.cif"]).hexdigest())
        if len(target_hashes) > 1:
            raise ValueError("pilot cells do not share the same source target")
        cdr_tokens, cdr3_length = cdr_annotation(yaml.safe_load(bound_sources["scaffold.yaml"]))
        if cell.get("cdr3_length") != cdr3_length:
            raise ValueError("declared CDR3 class differs from source YAML annotation")
        root = Path(cell["attempt_root"]) / "intermediate_designs"
        if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
            raise ValueError("summary cannot be inside a source design directory")
        designs = sorted(root.glob("design_*.npz"), key=lambda path: path.name)
        if len(designs) != cell["num_designs"] or not designs:
            raise ValueError("generated candidate count differs from cell binding")
        if {p.name for p in designs} != {p.name for p in (root / "fold_out_npz").glob("design_*.npz")}:
            raise ValueError("pilot design/fold closure mismatch")
        for path in designs:
            design, design_receipt = E._load_npz(path)
            fold, fold_receipt = E._load_npz(root / "fold_out_npz" / path.name)
            inputs.extend((design_receipt, fold_receipt))
            rows = E.evaluate_arrays(design, fold, target_tokens=list(range(TARGET_COUNT)))
            if len(rows) != expected_folds or not np.array_equal(np.flatnonzero(design["design_mask"]), cdr_tokens):
                raise ValueError("fold count or actual candidate CDR positions disagree with source annotation")
            res_ids = np.asarray(fold["res_type"])[0].argmax(axis=-1)
            if target_identity is None:
                target_identity = res_ids[:TARGET_COUNT]
            if not np.array_equal(target_identity, res_ids[:TARGET_COUNT]):
                raise ValueError("target residue identities differ between cells")
            sequence = "".join(CANONICAL_ONE_LETTER[int(residue_id) - 2] for residue_id in res_ids[TARGET_COUNT:])
            sequence_hash = hashlib.sha256(sequence.encode("ascii")).hexdigest()
            geometry = backbone_geometry(fold, design["design_mask"])
            original = np.asarray(fold["input_coords"])
            if original.shape != (1, 1, fold["coords"].shape[1], 3) or not np.isfinite(original).all():
                raise ValueError("invalid generated input coordinate reference")
            original = original[0, 0]
            if reference is None:
                reference = target_centres(original, geometry[0], geometry[1])
            generated_pose = describe_pose(original, geometry, reference, require_same_target=True)
            generated_fold = dict(fold, coords=original[None])
            generated_row = E.evaluate_arrays(design, generated_fold, target_tokens=list(range(TARGET_COUNT)))[0]
            candidates.append({"candidate_id": f"{cell['cell_id']}/{path.stem}", "scaffold_id": scaffold,
                "vhh_sequence_sha256": sequence_hash, "cdr3_length": cdr3_length,
                "generated_pose": generated_pose, "generated_epitope_footprint": footprint(generated_row),
                "generated_metrics": generated_row,
                "free_fold_poses": [describe_pose(xyz, geometry, reference) for xyz in fold["coords"]],
                "free_fold_metrics": rows, "free_fold_summary": E.candidate_summary(rows),
                "developability_descriptors": E.developability_descriptors(design, fold)})
    # Deduplicate before diversity counts; keep duplicate identities private.
    seen, unique, duplicates = {}, [], []
    for candidate in candidates:
        digest = candidate["vhh_sequence_sha256"]
        if digest in seen:
            duplicates.append({"candidate_id": candidate["candidate_id"], "same_sequence_as": seen[digest]})
        else:
            seen[digest] = candidate["candidate_id"]
            unique.append(candidate)
    clusters = cluster_poses([row["generated_pose"] for row in unique])
    representatives = [unique[group[0]]["generated_pose"] for group in clusters]
    for class_index, members in enumerate(clusters, 1):
        for member in members:
            candidate = unique[member]
            candidate["generated_pose_class"] = f"P{class_index}"
            assignments = [assign_fold(pose, representatives) for pose in candidate["free_fold_poses"]]
            counts = Counter(assignments)
            largest = max(counts.values())
            candidate["free_fold_pose_class_counts"] = dict(counts)
            candidate["free_fold_modal_pose_classes"] = sorted(key for key, value in counts.items() if value == largest)
            candidate["free_fold_modal_fraction"] = largest / len(assignments)
    scaffold_counts = dict(sorted(Counter(row["scaffold_id"] for row in unique).items()))
    cdr3_counts = dict(sorted(Counter(str(row["cdr3_length"]) for row in unique).items()))
    set_digest = candidate_set_digest(unique)
    public = {"schema": "VHH_DIVERSE_PILOT_SUMMARY_V1", "status": "COMPUTATIONAL_SUMMARY_COMPLETE",
        "biological_pass": False, "claim_boundary": "EXPLORATORY_DESIGN_DIVERSITY_NOT_BINDING_OR_SELECTIVITY",
        "candidate_count": len(unique), "generated_candidate_count": len(candidates), "duplicate_sequence_count": len(duplicates),
        "fold_sample_count": len(candidates) * expected_folds, "scaffold_count": len(scaffold_counts),
        "cdr3_length_class_count": len(cdr3_counts), "generated_pose_class_count": len(clusters),
        "scaffold_candidate_counts": scaffold_counts, "cdr3_length_class_candidate_counts": cdr3_counts,
        "generated_pose_class_sizes": [len(group) for group in clusters],
        "generated_epitope_footprint_counts_descriptive_only": dict(Counter(row["generated_epitope_footprint"] for row in unique)),
        "free_fold_modal_fraction_distribution": E._summary([row["free_fold_modal_fraction"] for row in unique]),
        "free_fold_unassigned_sample_count": sum(row["free_fold_pose_class_counts"].get("unassigned", 0) for row in unique),
        "candidate_developability_distributions": {name: E._summary([row["developability_descriptors"][name] for row in unique]) for name in E.DEVELOPABILITY_METRICS},
        "pose_classification": POSE_RULE,
        "source_file_count": len(inputs),
        "implementation_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ("summarize_vhh_pilot.py", "evaluate_vhh_epitope.py", "vhh_stage_gate.py")},
        "checks": {"source_identity_valid": True, "atom_mapping_valid": True, "finite_coordinates": True, "outputs_complete": True,
            "candidate_sequences_unique": not duplicates, "diversity_axes_reported": True, "developability_risks_reported": True,
            "same_source_target_verified": True, "cdr3_classes_verified_from_source_yaml_and_actual_mask": True},
        "diversity_requirements_satisfied": len(scaffold_counts) >= 2 and len(cdr3_counts) >= 2 and len(clusters) >= 2 and not duplicates,
        "limitations": ["Generated-pose classes are exploratory relative-geometry bins, not different biological binding modes.",
            "N/M/C contact footprint does not determine pose diversity; all candidates may appropriately share the N-terminal footprint.",
            "CDR3 classes are fixed source-label ranges verified against the actual design mask, not inferred from scaffold names.",
            "Free-fold modal fraction can be dominated by unassigned structures; no stability acceptance threshold is applied.",
            "Sequence deduplication uses the full modeled VHH canonical residue identity; no public sequence or per-candidate hash is emitted."],
        "analysis_seconds": time.perf_counter() - started}
    private = {**public, "actual_candidate_set_sha256": set_digest, "sequence_hash_encoding": "SHA256 of ASCII one-letter sequence of full modeled VHH, without newline", "inputs": inputs, "candidates": unique, "duplicate_candidates": duplicates}
    encoded = {"PUBLIC_PILOT_SUMMARY.json": json.dumps(public, indent=2, sort_keys=True, allow_nan=False), "PRIVATE_PILOT_SUMMARY.json": json.dumps(private, indent=2, sort_keys=True, allow_nan=False)}
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in encoded.items():
        with (output / name).open("x", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = summarize_index(args.index, args.output_dir)
    print(json.dumps({key: result[key] for key in ("status", "candidate_count", "scaffold_count", "cdr3_length_class_count", "generated_pose_class_count", "diversity_requirements_satisfied")}))


if __name__ == "__main__":
    main()
