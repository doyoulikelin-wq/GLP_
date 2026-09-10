#!/usr/bin/env python3
"""Source-anchored, per-structure pose descriptors; never unlock the old gate.

Coordinate comparability is an engineering applicability flag, not a binding
threshold. Contact metrics remain available when a valid target conformation
is outside the pose method's domain. This module does not run inference.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import gemmi
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E

BIOLOGICAL_NUMBERS = tuple(range(7, 37))
ACTIVE_SEQUENCE_NAMES = tuple(E.RESIDUES["ARNDCQEGHILKMFPSTWYV".index(letter)] for letter in "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR")
USAGES = {"RETROSPECTIVE_METHOD_DEVELOPMENT", "PROSPECTIVE_EXPLORATORY"}
POSE_V2_RULE = {
    "schema": "VHH_POSE_METHOD_V2",
    "reference": "one_original_target_cif_shared_by_all_candidates_not_first_generated_candidate",
    "mapping": "explicit_GLP1_biological_residue_numbers_7_through_36",
    "point": "per_residue_N_CA_C_O_centroid",
    "alignment_core_biological_numbers": list(range(13, 34)),
    "local_epitope_biological_numbers": list(range(7, 13)),
    "maximum_core_rmsd_angstrom": 0.5,
    "maximum_global_rmsd_angstrom": 1.0,
    "maximum_local_epitope_residue_displacement_angstrom": 1.0,
    "minimum_core_second_to_first_singular_value_ratio": 0.02,
    "domain_role": "engineering_coordinate_comparability_not_biological_threshold",
    "pose_diversity_required_for_progress": False,
    "descriptive_cluster_position_separation_angstrom": 8.0,
    "descriptive_cluster_direction_separation_degrees": 45.0,
    "outside_domain_action": "pose_NA_contacts_still_reported_no_candidate_biological_failure",
    "old_gate_unlock_allowed": False,
}


def rule_digest():
    return hashlib.sha256(json.dumps(POSE_V2_RULE, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _points(value, shape, label):
    array = np.asarray(value)
    if array.shape != shape or array.dtype.kind not in "fiu" or not np.isfinite(array).all():
        raise ValueError(f"invalid {label}: require finite real {shape}")
    return array.astype(float)


def _conditioning(points):
    singular = np.linalg.svd(points - points.mean(axis=0), compute_uv=False)
    return float(singular[1] / singular[0]) if singular[0] > 1e-12 else 0.0


def load_reference(path: Path, chain_id: str, biological_numbers=BIOLOGICAL_NUMBERS):
    """Read the original canonical target, with explicit sequential bio mapping.

    The chain must contain exactly the complete 30-residue active sequence;
    author numbering is recorded but never silently treated as biological IDs.
    """
    if tuple(biological_numbers) != BIOLOGICAL_NUMBERS or path.is_symlink():
        raise ValueError("reference needs explicit ordered biological mapping 7..36")
    data = path.read_bytes()
    structure = gemmi.make_structure_from_block(gemmi.cif.read_string(data.decode()).sole_block())
    if len(structure) != 1:
        raise ValueError("one source target model required")
    chain = structure[0].find_chain(chain_id)
    if chain is None or tuple(residue.name for residue in chain) != ACTIVE_SEQUENCE_NAMES:
        raise ValueError("reference chain does not identify the complete active GLP1 sequence")
    points = []
    for residue in chain:
        atoms = []
        for name in ("N", "CA", "C", "O"):
            matches = [atom for atom in residue if atom.name == name]
            if len(matches) != 1:
                raise ValueError("reference backbone missing or altloc-ambiguous")
            atoms.append(list(matches[0].pos))
        points.append(np.mean(atoms, axis=0))
    return {"centroids": _points(points, (30, 3), "reference"),
        "biological_numbers": BIOLOGICAL_NUMBERS, "residue_names": ACTIVE_SEQUENCE_NAMES,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_auth_residue_ids": [str(residue.seqid) for residue in chain]}


def evaluate_geometry(target_centroids, cdr_centroid, framework_centroid, reference_centroids,
                      *, biological_numbers, usage):
    """Fixed-core Kabsch, followed by global AND local applicability checks."""
    if usage not in USAGES or tuple(biological_numbers) != BIOLOGICAL_NUMBERS:
        raise ValueError("explicit usage and fixed biological residue mapping required")
    target = _points(target_centroids, (30, 3), "target centroids")
    reference = _points(reference_centroids, (30, 3), "reference centroids")
    cdr = _points(cdr_centroid, (3,), "CDR centroid")
    framework = _points(framework_centroid, (3,), "framework centroid")
    core = np.asarray([number - 7 for number in POSE_V2_RULE["alignment_core_biological_numbers"]])
    epitope = np.asarray([number - 7 for number in POSE_V2_RULE["local_epitope_biological_numbers"]])
    minimum = POSE_V2_RULE["minimum_core_second_to_first_singular_value_ratio"]
    reference_condition = _conditioning(reference[core])
    if reference_condition < minimum:
        raise ValueError("original reference alignment is degenerate/ill-conditioned")
    target_condition = _conditioning(target[core])
    result = {"method": "VHH_POSE_METHOD_V2", "usage": usage, "rule_sha256": rule_digest(),
        "reference_centroids_sha256": hashlib.sha256(reference.astype("<f8").tobytes()).hexdigest(),
        "biological_pass": False, "old_gate_unlock_allowed": False,
        "pose_diversity_required_for_progress": False,
        "engineering_domain": "OUTSIDE", "pose_descriptor": None,
        "target_core_conditioning_ratio": target_condition,
        "reference_core_conditioning_ratio": reference_condition,
        "diagnostics": None, "reasons": []}
    if target_condition < minimum:
        result["reasons"] = ["target_core_alignment_degenerate_or_ill_conditioned"]
        return result
    mobile_centre, fixed_centre = target[core].mean(axis=0), reference[core].mean(axis=0)
    u, _, vt = np.linalg.svd((target[core] - mobile_centre).T @ (reference[core] - fixed_centre))
    correction = np.eye(3)
    correction[-1, -1] = np.linalg.det(u @ vt)
    rotation = u @ correction @ vt
    aligned = (target - mobile_centre) @ rotation + fixed_centre
    displacement = np.linalg.norm(aligned - reference, axis=1)
    diagnostics = {"core_rmsd_angstrom": float(np.sqrt(np.mean(displacement[core] ** 2))),
        "global_rmsd_angstrom": float(np.sqrt(np.mean(displacement ** 2))),
        "local_epitope_max_displacement_angstrom": float(displacement[epitope].max()),
        "per_residue_displacement_angstrom": displacement.tolist()}
    result["diagnostics"] = diagnostics
    for metric, limit_name in (("core_rmsd_angstrom", "maximum_core_rmsd_angstrom"),
                              ("global_rmsd_angstrom", "maximum_global_rmsd_angstrom"),
                              ("local_epitope_max_displacement_angstrom", "maximum_local_epitope_residue_displacement_angstrom")):
        if diagnostics[metric] > POSE_V2_RULE[limit_name]:
            result["reasons"].append(metric + "_outside_engineering_domain")
    direction = (cdr - framework) @ rotation
    if np.linalg.norm(direction) < 1e-6:
        result["reasons"].append("framework_to_cdr_direction_undefined")
    if result["reasons"]:
        return result
    result["engineering_domain"] = "WITHIN"
    result["pose_descriptor"] = {
        "cdr_backbone_centroid_relative_to_source_target_centroid_angstrom": ((cdr-mobile_centre) @ rotation + fixed_centre-reference.mean(axis=0)).tolist(),
        "framework_to_cdr_unit_direction": (direction/np.linalg.norm(direction)).tolist()}
    return result


def evaluate_fold(design, fold, reference, *, usage, geometry_source="generated_input"):
    """Return contacts independently of per-structure pose applicability.

    Protein identities/heavy atoms use the existing validated evaluator. This
    function only supports the explicitly mapped active target at tokens 0..29.
    Reference dictionaries should come from load_reference, not generated CIFs.
    """
    if tuple(reference["biological_numbers"]) != BIOLOGICAL_NUMBERS:
        raise ValueError("reference biological map changed")
    if geometry_source == "generated_input":
        array = np.asarray(fold["input_coords"])
        if array.shape != (1, 1, np.asarray(fold["coords"]).shape[1], 3):
            raise ValueError("invalid generated input coordinate shape")
        selected = dict(fold, coords=array[0])
    elif geometry_source == "free_fold_samples":
        selected = fold
    else:
        raise ValueError("explicit generated_input or free_fold_samples source required")
    contacts = E.evaluate_arrays(design, selected, target_tokens=list(range(30)))
    names = tuple(E.RESIDUES[int(index)-2] for index in np.asarray(fold["res_type"])[0, :30].argmax(axis=-1))
    if names != tuple(reference["residue_names"]) or names != ACTIVE_SEQUENCE_NAMES:
        raise ValueError("candidate/reference target identity mapping mismatch")
    mapping = np.asarray(fold["atom_to_token"])[0]
    valid = mapping.sum(axis=1) == 1
    token = mapping.argmax(axis=1)
    backbone = E._binary(fold["backbone_mask"], (1, len(token)), "backbone_mask")[0]
    if np.any(backbone & ~valid) or not np.all(np.bincount(token[backbone], minlength=mapping.shape[1]) == 4):
        raise ValueError("require exactly N/CA/C/O modeled backbone slots per token")
    cdr = backbone & np.asarray(design["design_mask"], dtype=bool)[token]
    framework = backbone & (token >= 30) & ~cdr
    if not framework.any():
        raise ValueError("nonempty framework required for orientation")
    rows = []
    for index, coords in enumerate(selected["coords"]):
        centres = np.stack([coords[backbone & (token == number)].mean(axis=0) for number in range(30)])
        pose = evaluate_geometry(centres, coords[cdr].mean(axis=0), coords[framework].mean(axis=0),
            reference["centroids"], biological_numbers=BIOLOGICAL_NUMBERS, usage=usage)
        rows.append({"sample_index": index, "contacts": contacts[index], "pose": pose})
    return {"schema": "VHH_POSE_V2_EVALUATION", "usage": usage, "geometry_source": geometry_source,
        "source_target_sha256": reference["source_sha256"], "rule": POSE_V2_RULE,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "old_gate_unlock_allowed": False, "biological_pass": False, "samples": rows,
        "pose_applicable_sample_count": sum(row["pose"]["engineering_domain"] == "WITHIN" for row in rows),
        "contact_summary": E.candidate_summary(contacts)}


def descriptive_clusters(evaluations):
    """Complete-link, input-order clustering of applicable generated poses only.

    No minimum class count is required. Omitted poses are NA, not an extra class.
    """
    if evaluations and (len({row["reference_centroids_sha256"] for row in evaluations}) != 1 or any(row["rule_sha256"] != rule_digest() for row in evaluations)):
        raise ValueError("all clustered poses require the same original reference and V2 rule")
    descriptors = [(index, row["pose_descriptor"]) for index, row in enumerate(evaluations) if row["engineering_domain"] == "WITHIN"]
    def near(left, right):
        position = np.linalg.norm(np.asarray(left["cdr_backbone_centroid_relative_to_source_target_centroid_angstrom"])-right["cdr_backbone_centroid_relative_to_source_target_centroid_angstrom"])
        angle = np.degrees(np.arccos(np.clip(np.dot(left["framework_to_cdr_unit_direction"], right["framework_to_cdr_unit_direction"]), -1, 1)))
        return position < POSE_V2_RULE["descriptive_cluster_position_separation_angstrom"] and angle < POSE_V2_RULE["descriptive_cluster_direction_separation_degrees"]
    groups = []
    for index, descriptor in descriptors:
        for group in groups:
            if all(near(descriptor, evaluations[member]["pose_descriptor"]) for member in group):
                group.append(index)
                break
        else:
            groups.append([index])
    return {"class_count": len(groups) if descriptors else None, "groups": groups,
        "applicable_count": len(descriptors), "not_applicable_count": len(evaluations)-len(descriptors),
        "descriptive_only": True, "minimum_class_count_required": None, "old_gate_unlock_allowed": False}
