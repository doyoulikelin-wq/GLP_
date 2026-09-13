#!/usr/bin/env python3
"""Independent SciPy-based pose decomposition and alignment-window sensitivity.

Reads the completed local-edit inputs and predictions. Does not import project
fit/scoring helpers, run inference, or write files. Prints no sequence/coordinates.
"""
import argparse
import json
from pathlib import Path

import gemmi
import numpy as np
from scipy.spatial.transform import Rotation


def ca_from_cif(path, target_only=False):
    """Read explicit canonical CA identities in the known source chain scheme."""
    block = gemmi.cif.read_file(str(path)).sole_block()
    atoms = {}
    for chain, number, atom, x, y, z in block.find("_atom_site.",
            ["label_asym_id", "label_seq_id", "label_atom_id", "Cartn_x", "Cartn_y", "Cartn_z"]):
        if atom != "CA" or number in (".", "?"):
            continue
        if target_only:
            if chain != "E" or not 1 <= int(number) <= 30:
                continue
            token = int(number) - 1
        else:
            if chain not in ("A", "B"):
                raise ValueError("unexpected source chain")
            token = int(number) - 1 + (30 if chain == "B" else 0)
        if token in atoms:
            raise ValueError("duplicate CA identity")
        atoms[token] = [float(x), float(y), float(z)]
    count = 30 if target_only else 151
    if set(atoms) != set(range(count)):
        raise ValueError("complete CA mapping required")
    return np.array([atoms[i] for i in range(count)])


def align_all(mobile, fixed, fit):
    """Align using independent scipy proper rotations, with equal CA weights."""
    a, b = mobile[fit], fixed[fit]
    cm, cf = a.mean(axis=0), b.mean(axis=0)
    rotation, _ = Rotation.align_vectors(b - cf, a - cm)
    return rotation.apply(mobile - cm) + cf


def rmsd(a, b):
    """Root mean squared Euclidean displacement, in angstroms."""
    return float(np.sqrt(np.mean(np.sum((a - b) ** 2, axis=-1))))


def terminal_reference_atoms(path):
    """Read original His7/Ala8 heavy atoms for a geometric, not physical, replay."""
    block = gemmi.cif.read_file(str(path)).sole_block()
    result = {0: [], 1: []}
    for chain, number, element, x, y, z in block.find("_atom_site.",
            ["label_asym_id", "label_seq_id", "type_symbol", "Cartn_x", "Cartn_y", "Cartn_z"]):
        if chain == "E" and number in ("1", "2") and element not in ("H", "D"):
            result[int(number) - 1].append([float(x), float(y), float(z)])
    if any(not values for values in result.values()):
        raise ValueError("terminal reference atoms unavailable")
    return {i: np.array(values) for i, values in result.items()}


def verify(attempt, prepared):
    """Check all predictions with primary and shorter central alignment windows."""
    attempt, prepared = Path(attempt), Path(prepared)
    inputs = json.loads((attempt / "FINAL_INPUTS.json").read_text())
    plan = json.loads((prepared / "PRIVATE_PLAN.json").read_text())
    reference = ca_from_cif(plan["target_reference"]["path"], target_only=True)
    terminal_atoms = terminal_reference_atoms(plan["target_reference"]["path"])
    rows = []
    for task in inputs["tasks"]:
        name = task["task_id"]
        raw = ca_from_cif(task["source_cif"]["path"])
        cdr = task["full_cdr_tokens"]
        framework = [i for i in range(30, 151) if i not in cdr]
        with np.load(attempt / "atom_mappings" / (name + ".npz"), allow_pickle=False) as z:
            tokens = z["atom_to_token"].argmax(axis=-1)
            valid = z["atom_to_token"].sum(axis=-1) == 1
            names = ["".join(chr(int(c) + 32) for c in row).strip()
                     for row in z["ref_atom_name_chars"].argmax(axis=-1)]
        indices = [np.flatnonzero(valid & (tokens == i) & (np.array(names) == "CA")) for i in range(151)]
        if any(len(i) != 1 for i in indices):
            raise ValueError("one CA per residue required")
        indices = np.array([i[0] for i in indices])
        with np.load(attempt / "intermediate_designs/fold_out_npz" / (name + ".npz"), allow_pickle=False) as z:
            all_atoms = np.asarray(z["coords"], dtype=float)
            predictions = all_atoms[:, indices, :]
            iptm = z["iptm"]
        if predictions.shape != (3, 151, 3) or not np.isfinite(predictions).all():
            raise ValueError("exactly three finite structures required")
        for i, predicted in enumerate(predictions):
            individual = align_all(predicted, raw, framework)
            row = {"task_id": name, "arm": task["arm"], "sample_index": i,
                "framework_self_rmsd": rmsd(individual[framework], raw[framework]),
                "cdr_after_framework_fit_rmsd": rmsd(individual[cdr], raw[cdr]),
                "iptm": float(iptm[i]), "alignments": {}}
            for label, fit in (("primary_2_27", np.arange(2, 28)), ("sensitivity_8_23", np.arange(8, 24))):
                target_aligned = align_all(predicted, reference, fit)
                source_aligned = align_all(raw, reference, fit)
                row["alignments"][label] = {
                    "target_all_ca_rmsd": rmsd(target_aligned[:30], reference),
                    "terminal_ca_rmsd": rmsd(target_aligned[:2], reference[:2]),
                    "target_aligned_framework_rmsd": rmsd(target_aligned[framework], source_aligned[framework])}
                # Keep actual predicted VHH atoms; only superpose original target
                # termini. This is an artificial geometry probe, not a new fold.
                cm, cf = reference[fit].mean(0), predicted[fit].mean(0)
                rotation, _ = Rotation.align_vectors(predicted[fit] - cf, reference[fit] - cm)
                cdr_xyz = all_atoms[i, valid & np.isin(tokens, cdr)]
                distances = []
                for terminal in (0, 1):
                    placed = rotation.apply(terminal_atoms[terminal] - cm) + cf
                    distances.append(float(np.sqrt(np.sum((cdr_xyz[:, None] - placed[None]) ** 2, axis=-1)).min()))
                row["alignments"][label].update(
                    hypothetical_replacement_his_distance=distances[0],
                    hypothetical_replacement_ala_distance=distances[1],
                    hypothetical_replacement_both_contact=max(distances) <= 4.5)
            rows.append(row)
    if len(rows) != 18:
        raise ValueError("all eighteen predictions required")
    return {"schema": "VHH_POSE_INDEPENDENT_SCIPY_RECALCULATION_V1", "sample_count": 18,
        "method": "Scipy Rotation.align_vectors; no project alignment functions",
        "replacement_status": "ARTIFICIAL_GEOMETRY_NOT_A_PREDICTION_OR_PHYSICAL_VALIDATION", "rows": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.attempt, args.prepared), indent=2, allow_nan=False))
