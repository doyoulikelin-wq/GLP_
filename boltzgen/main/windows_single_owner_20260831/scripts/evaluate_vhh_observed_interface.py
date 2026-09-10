#!/usr/bin/env python3
"""Post-run partial-interface method; does not replace frozen strict results.

Only missing framework sidechains are tolerated. No atom is invented and no
resolved bit is changed. The actual target+CDR substructure is projected into
the frozen evaluator, then residue indices are mapped back to the full input.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E
import evaluate_vhh_pose_v2 as V2

CLASSIFICATION = "RETROSPECTIVE_PARTIAL_OBSERVED_INTERFACE"


def evaluate_observed_interface(design, fold, reference, *, geometry_source="generated_input"):
    """Return V2-shaped contact/pose rows, preserving whole-structure NA.

    Supports active GLP1 tokens 0..29 only. All full-input canonical assignments
    and backbone atoms are checked before a real target/CDR-only projection.
    """
    mask = np.asarray(design["design_mask"])
    if mask.ndim != 1 or len(mask) <= 30:
        raise ValueError("active target plus VHH token vector required")
    token_count = len(mask)
    cdr_tokens_mask = E._binary(mask, (token_count,), "design_mask")
    if cdr_tokens_mask[:30].any() or not cdr_tokens_mask[30:].any():
        raise ValueError("target must be fixed and CDR annotation nonempty")
    if not np.array_equal(fold["token_index"], np.arange(token_count)[None]):
        raise ValueError("full token order mismatch")
    if np.asarray(fold["mol_type"]).shape != (1, token_count) or np.any(fold["mol_type"] != 0):
        raise ValueError("canonical protein tokens only")
    residues = E._binary(fold["res_type"], (1, token_count, 33), "res_type")[0]
    ids = residues.argmax(axis=-1)
    if not np.all(residues.sum(axis=-1) == 1) or np.any(ids < 2) or np.any(ids > 21):
        raise ValueError("canonical one-hot residue identity required")
    names = tuple(E.RESIDUES[int(index)-2] for index in ids[:30])
    if names != V2.ACTIVE_SEQUENCE_NAMES or names != tuple(reference["residue_names"]) or tuple(reference["biological_numbers"]) != V2.BIOLOGICAL_NUMBERS:
        raise ValueError("active target and original-reference identity mismatch")
    coords = np.asarray(fold["coords"])
    if coords.ndim != 3 or coords.shape[0] < 1 or coords.shape[2] != 3 or coords.dtype.kind not in "fiu" or not np.isfinite(coords).all():
        raise ValueError("invalid full fold coordinate array")
    atoms = coords.shape[1]
    mapping = E._binary(fold["atom_to_token"], (1, atoms, token_count), "atom_to_token")[0]
    assignment_count = mapping.sum(axis=1)
    if not np.isin(assignment_count, (0, 1)).all():
        raise ValueError("nonunique atom assignment")
    assigned = assignment_count == 1
    resolved = E._binary(fold["atom_resolved_mask"], (1, atoms), "resolved")[0]
    if np.any(resolved & ~assigned) or not np.array_equal(mapping.sum(axis=0), E.HEAVY_COUNTS[ids-2]):
        raise ValueError("resolved padding or canonical assigned-slot count mismatch")
    token = mapping.argmax(axis=1)
    backbone = E._binary(fold["backbone_mask"], (1, atoms), "backbone")[0]
    if np.any(backbone & (~assigned | ~resolved)) or not np.all(np.bincount(token[backbone], minlength=token_count) == 4):
        raise ValueError("every token requires four resolved backbone atoms")
    target = assigned & (token < 30)
    cdr = assigned & cdr_tokens_mask[token]
    framework = assigned & (token >= 30) & ~cdr
    missing = assigned & ~resolved
    if np.any(missing & (target | cdr | backbone)) or np.any(missing & ~framework):
        raise ValueError("missing atoms must be framework sidechains only")
    if not np.any(framework & backbone):
        raise ValueError("framework backbone required for separate pose geometry")
    if geometry_source == "generated_input":
        original = np.asarray(fold["input_coords"])
        if original.shape != (1, 1, atoms, 3) or original.dtype.kind not in "fiu" or not np.isfinite(original).all():
            raise ValueError("invalid generated input coordinate array")
        selected_coords = original[0]
    elif geometry_source == "free_fold_samples":
        selected_coords = coords
    else:
        raise ValueError("unknown coordinate source")
    # Remove complete framework tokens and their atoms. Keep only real,
    # resolved atoms; preserve the original mask values without filling bits.
    selected_tokens = np.r_[np.arange(30), np.flatnonzero(cdr_tokens_mask)]
    selected_atoms = np.flatnonzero(target | cdr)
    projected_design = {"design_mask": mask[selected_tokens]}
    projected_fold = {
        "coords": selected_coords[:, selected_atoms],
        "token_index": np.arange(len(selected_tokens))[None],
        "mol_type": np.asarray(fold["mol_type"])[:, selected_tokens],
        "res_type": np.asarray(fold["res_type"])[:, selected_tokens],
        "atom_to_token": mapping[np.ix_(selected_atoms, selected_tokens)][None],
        "atom_resolved_mask": resolved[selected_atoms][None],
    }
    contact_rows = E.evaluate_arrays(projected_design, projected_fold, target_tokens=list(range(30)))
    rows = []
    for index, (xyz, contact) in enumerate(zip(selected_coords, contact_rows)):
        contact["contact_residue_pairs_zero_based"] = [[int(selected_tokens[t]), int(selected_tokens[c])] for t, c in contact["contact_residue_pairs_zero_based"]]
        for item in contact["per_target_residue"]:
            item["token_index"] = int(selected_tokens[item["token_index"]])
        # The projected evaluator's non-target region is CDR only. Its result
        # must never be mislabeled as an all-VHH heavy-atom minimum distance.
        contact["min_target_vhh_distance_angstrom"] = None
        contact["all_vhh_heavy_atom_distance_status"] = "NOT_ASSESSED"
        centres = np.stack([xyz[backbone & (token == number)].mean(axis=0) for number in range(30)])
        pose = V2.evaluate_geometry(centres, xyz[cdr & backbone].mean(axis=0), xyz[framework & backbone].mean(axis=0), reference["centroids"],
            biological_numbers=V2.BIOLOGICAL_NUMBERS, usage="RETROSPECTIVE_METHOD_DEVELOPMENT")
        rows.append({"sample_index": index, "contacts": contact, "pose": pose})
    return {"schema": "VHH_PARTIAL_OBSERVED_INTERFACE_V1", "analysis_classification": CLASSIFICATION,
        "usage": CLASSIFICATION, "geometry_source": geometry_source,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_target_sha256": reference["source_sha256"], "rule": V2.POSE_V2_RULE,
        "missing_framework_sidechain_atom_count": int(missing.sum()),
        "missing_framework_sidechain_token_indices": np.unique(token[missing]).tolist(),
        "resolved_target_heavy_atom_count": int(target.sum()), "resolved_cdr_heavy_atom_count": int(cdr.sum()),
        "resolved_framework_backbone_atom_count": int((framework & backbone).sum()),
        "all_vhh_heavy_atom_distance_status": "NOT_ASSESSED",
        "full_structure_validated": False, "original_strict_result_replacement": False,
        "original_strict_evaluation_status": "NA_MISSING_FRAMEWORK_SIDECHAINS_PRESERVED" if missing.any() else "NOT_REDETERMINED_BY_PARTIAL_METHOD",
        "biological_pass": False, "old_gate_unlock_allowed": False,
        "scope": "complete observed target-CDR heavy atoms and separately complete resolved backbone; framework sidechains excluded",
        "samples": rows, "contact_summary": E.candidate_summary(contact_rows),
        "pose_applicable_sample_count": sum(row["pose"]["engineering_domain"] == "WITHIN" for row in rows)}
