#!/usr/bin/env python3
"""CPU-only decomposition of target shape, VHH shape and relative pose.

Inputs are the complete frozen 18-sample local-edit run and its preparation.
Outputs are fresh private/public JSON diagnostic receipts, never new predictions.
Replacing target coordinates is an explicitly hypothetical geometry operation:
no relaxation, energy calculation, binding inference or causal claim is made.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import gemmi
import numpy as np
from boltzgen.data import const

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnose_vhh_terminal_interface as D
import score_vhh_local_edit_20260913 as S

CENTRAL = np.arange(2, 28)
CDR = np.asarray(S.FULL_CDR_TOKENS)
FRAMEWORK = np.array([i for i in range(30, 151) if i not in set(CDR)])
CONTACT_KEYS = ("both_epitope_contacts", "his_contact", "ala_contact", "any_target_cdr_contact",
    "his_min_cdr_distance_angstrom", "ala_min_cdr_distance_angstrom",
    "min_target_cdr_distance_angstrom", "severe_close_contact_diagnostic")
GEOMETRY_KEYS = ("target_central_ca_rmsd_angstrom", "target_all_ca_rmsd_after_central_fit_angstrom",
    "target_his_ala_ca_rmsd_after_central_fit_angstrom", "target_his_ca_displacement_angstrom",
    "target_ala_ca_displacement_angstrom", "target_c_terminal_two_ca_rmsd_after_central_fit_angstrom",
    "vhh_framework_ca_rmsd_after_framework_fit_angstrom", "vhh_full_cdr_ca_rmsd_after_framework_fit_angstrom",
    "vhh_framework_ca_rmsd_after_target_central_fit_angstrom",
    "vhh_framework_centroid_displacement_after_target_fit_angstrom",
    "vhh_framework_relative_rotation_degrees")


def read_bound(path, inputs, expected=None):
    """Read one ordinary immutable file and retain exact content provenance."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("ordinary existing input required")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("frozen input changed")
    inputs[str(path.resolve())] = {"path": str(path.resolve()), "sha256": digest, "bytes": len(raw)}
    return raw


def rmsd(first, second):
    """Return RMS atom displacement with exact finite shape checks."""
    first, second = np.asarray(first, float), np.asarray(second, float)
    if first.shape != second.shape or first.ndim != 2 or first.shape[1] != 3 or not len(first):
        raise ValueError("matching nonempty Nx3 coordinate arrays required")
    if not np.isfinite(first).all() or not np.isfinite(second).all():
        raise ValueError("finite coordinates required")
    return float(np.sqrt(np.mean(np.sum((first-second)**2, axis=-1))))


def fitted(points, mobile, fixed):
    """Apply a proper row-vector Kabsch transform inferred from matched atoms."""
    rotation, centre, target = D.rigid_fit(mobile, fixed)
    return (np.asarray(points)-centre)@rotation+target


def geometry(pred_ca, target_ref_ca, source_ca):
    """Keep three distinct alignments; metrics are descriptive, not additive causes."""
    pred_ca, target_ref_ca, source_ca = (np.asarray(value, float) for value in (pred_ca, target_ref_ca, source_ca))
    if pred_ca.shape != (151, 3) or source_ca.shape != (151, 3) or target_ref_ca.shape != (30, 3):
        raise ValueError("exact 151-token complex and 30-token target CA mapping required")
    pred_ref = fitted(pred_ca, pred_ca[CENTRAL], target_ref_ca[CENTRAL])
    source_ref = fitted(source_ca, source_ca[CENTRAL], target_ref_ca[CENTRAL])
    pred_source = fitted(pred_ca, pred_ca[FRAMEWORK], source_ca[FRAMEWORK])
    rotation, _, _ = D.rigid_fit(pred_ref[FRAMEWORK], source_ref[FRAMEWORK])
    return {
        "target_central_ca_rmsd_angstrom": rmsd(pred_ref[CENTRAL], target_ref_ca[CENTRAL]),
        "target_all_ca_rmsd_after_central_fit_angstrom": rmsd(pred_ref[:30], target_ref_ca),
        "target_his_ala_ca_rmsd_after_central_fit_angstrom": rmsd(pred_ref[:2], target_ref_ca[:2]),
        "target_his_ca_displacement_angstrom": rmsd(pred_ref[:1], target_ref_ca[:1]),
        "target_ala_ca_displacement_angstrom": rmsd(pred_ref[1:2], target_ref_ca[1:2]),
        "target_c_terminal_two_ca_rmsd_after_central_fit_angstrom": rmsd(pred_ref[28:30], target_ref_ca[28:30]),
        "vhh_framework_ca_rmsd_after_framework_fit_angstrom": rmsd(pred_source[FRAMEWORK], source_ca[FRAMEWORK]),
        "vhh_full_cdr_ca_rmsd_after_framework_fit_angstrom": rmsd(pred_source[CDR], source_ca[CDR]),
        "vhh_framework_ca_rmsd_after_target_central_fit_angstrom": rmsd(pred_ref[FRAMEWORK], source_ref[FRAMEWORK]),
        "vhh_framework_centroid_displacement_after_target_fit_angstrom": rmsd(pred_ref[FRAMEWORK].mean(axis=0, keepdims=True), source_ref[FRAMEWORK].mean(axis=0, keepdims=True)),
        "vhh_framework_relative_rotation_degrees": float(np.degrees(np.arccos(np.clip((np.trace(rotation)-1)/2, -1, 1)))),
    }


def target_reference(raw, chain_id):
    """Parse exact canonical target atom names without inferring terminal chemistry."""
    block = gemmi.cif.read_string(raw.decode()).sole_block()
    atoms, residues = {}, {}
    tags = ["label_asym_id", "label_seq_id", "label_comp_id", "label_atom_id", "type_symbol",
        "label_alt_id", "pdbx_PDB_model_num", "Cartn_x", "Cartn_y", "Cartn_z"]
    for chain, number, name, atom, element, alt, model, x, y, z in block.find("_atom_site.", tags):
        if chain != chain_id or number in (".", "?") or not 1 <= int(number) <= 30:
            continue
        token = int(number)-1
        if element in ("H", "D") or alt not in (".", "?") or model != "1" or name not in S.E.RESIDUES:
            raise ValueError("target must be one canonical heavy-atom model without alternates")
        if (token, atom) in atoms or residues.get(token, name) != name:
            raise ValueError("ambiguous target atom or residue")
        atoms[token, atom] = np.array([float(x), float(y), float(z)])
        residues[token] = name
    if set(residues) != set(range(30)):
        raise ValueError("all thirty target residues required")
    ids = [S.E.RESIDUES.index(residues[i])+2 for i in range(30)]
    if tuple(ids) != S.TARGET_IDS:
        raise ValueError("target sequence differs from frozen full active identity")
    for token in range(30):
        names = {atom for t, atom in atoms if t == token}
        if names != set(const.ref_atoms[residues[token]]):
            raise ValueError("complete canonical target heavy atoms required")
    if not np.isfinite(np.asarray(list(atoms.values()))).all():
        raise ValueError("target reference coordinates must be finite")
    return atoms, np.stack([atoms[i, "CA"] for i in range(30)])


def atom_names(bound):
    """Decode CPU-bound names only after strict score validation has accepted them."""
    chars = np.asarray(bound["ref_atom_name_chars"])
    return np.array(["".join(chr(int(v)+32) for v in row).strip() for row in chars.argmax(axis=-1)])


def source_ca(raw):
    """Read exactly one canonical raw target/VHH CIF and map all C-alpha atoms."""
    _, checked = D.cif_arrays(raw, CDR)
    block = gemmi.cif.read_string(raw.decode()).sole_block()
    positions = {}
    for chain, number, atom, x, y, z in block.find("_atom_site.", ["label_asym_id", "label_seq_id", "label_atom_id", "Cartn_x", "Cartn_y", "Cartn_z"]):
        if atom == "CA":
            token = int(number)-1+(30 if chain == "B" else 0)
            if token in positions:
                raise ValueError("duplicate source CA")
            positions[token] = [float(x), float(y), float(z)]
    if set(positions) != set(range(151)):
        raise ValueError("complete canonical source CA mapping required")
    return np.asarray([positions[i] for i in range(151)]), checked["res_type"][0].argmax(axis=-1)


def replace_target(xyz, valid, tokens, names, reference_atoms, reference_ca):
    """Hypothetical target replacement; VHH atoms remain bitwise unchanged."""
    xyz = np.asarray(xyz, float)
    pred_ca = np.stack([xyz[np.flatnonzero(valid & (tokens == i) & (names == "CA"))[0]] for i in range(30)])
    rotation, centre, target = D.rigid_fit(reference_ca[CENTRAL], pred_ca[CENTRAL])
    result = xyz.copy()
    selected = np.flatnonzero(valid & (tokens < 30))
    for index in selected:
        key = int(tokens[index]), str(names[index])
        if key not in reference_atoms:
            raise ValueError("canonical target replacement atom missing")
        result[index] = (reference_atoms[key]-centre)@rotation+target
    if not np.array_equal(result[valid & (tokens >= 30)], xyz[valid & (tokens >= 30)]):
        raise ValueError("hypothetical replacement unexpectedly moved VHH atoms")
    return result


def summarize(rows):
    """Summarize all samples with unchanged denominators and explicit no-cause labels."""
    observed = [r["observed_contacts"]["both_epitope_contacts"] for r in rows]
    replaced = [r["hypothetical_target_replacement_contacts"]["both_epitope_contacts"] for r in rows]
    return {"sample_count": len(rows), "source_candidate_count": len({r["source_draw"] for r in rows}),
        "observed_double_contact_count": sum(observed), "hypothetical_double_contact_count": sum(replaced),
        "hypothetical_restored_double_contact_count": sum(not a and b for a, b in zip(observed, replaced)),
        "hypothetical_lost_double_contact_count": sum(a and not b for a, b in zip(observed, replaced)),
        "geometry_descriptors": {key: S.E._summary([r["geometry"][key] for r in rows]) for key in GEOMETRY_KEYS}}


def diagnose(attempt, prepared, output):
    """Validate existing outputs then diagnose all eighteen samples without inference."""
    started = time.perf_counter()
    attempt, prepared, output = Path(attempt).resolve(strict=True), Path(prepared).resolve(strict=True), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("diagnostic output must be a new directory")
    inputs = {}
    run = json.loads(read_bound(attempt/"LOCAL_EDIT_RUN.json", inputs))
    if run.get("status") != "COMPLETED" or run.get("actual", {}).get("fold_samples") != 18 or run.get("weights_unchanged") is not True:
        raise ValueError("the completed eighteen-sample local edit run is required")
    if Path(run["plan"]["path"]).resolve() != prepared/"PRIVATE_PLAN.json":
        raise ValueError("preparation must be the actual run-bound plan")
    plan = json.loads(read_bound(prepared/"PRIVATE_PLAN.json", inputs, run["plan"]["sha256"]))
    final = json.loads(read_bound(attempt/"FINAL_INPUTS.json", inputs))
    for relative, digest in final["sha256"].items():
        source = attempt/relative
        if attempt not in source.resolve().parents:
            raise ValueError("invalid final input manifest path")
        read_bound(source, inputs, digest)
    historical = json.loads(read_bound(attempt/"PUBLIC_LOCAL_EDIT_RESULT.json", inputs, run["result"]["sha256"]))
    strict, score = S.score_outputs(attempt, final)
    for record in strict["inputs"]:
        read_bound(record["path"], inputs, record["sha256"])
    expected_counts = {"ORIGINAL": 1, "SEQUENCE_ONLY": 0, "LOCAL_INPAINT": 1}
    for arm, count in expected_counts.items():
        key = "primary_full_cdr"
        actual = score["by_arm"][arm][key]["sample_contact_counts"]["both_epitope_contacts"]
        old = historical["by_arm"][arm][key]["sample_contact_counts"]["both_epitope_contacts"]
        if actual != count or old != count:
            raise ValueError("existing contact counts do not reconcile with the frozen local-edit result")
    target = plan["target_reference"]
    ref_atoms, ref_ca = target_reference(read_bound(target["path"], inputs, target["sha256"]), target["target_chain_id"])
    source_cache, source_baselines, rows = {}, [], []
    candidate_scores = {r["task_id"]: r for r in strict["candidates"]}
    for task in final["tasks"]:
        name = task["task_id"]
        if task["source_draw"] not in source_cache:
            source = task["source_cif"]
            ca, ids = source_ca(read_bound(source["path"], inputs, source["sha256"]))
            if ids.tolist() != task["source_residue_ids"]:
                raise ValueError("source CA reference identity differs")
            source_cache[task["source_draw"]] = ca
            source_baselines.append({"source_draw": task["source_draw"], "geometry_to_self_and_common_target": geometry(ca, ref_ca, ca)})
        fold, _ = S.E._load_npz(attempt/"intermediate_designs/fold_out_npz"/(name+".npz"))
        bound, _ = S.E._load_npz(attempt/"atom_mappings"/(name+".npz"))
        valid, tokens, _ = S.validated_backbone(fold, bound, np.asarray(task["expected_residue_ids"]))
        names = atom_names(bound)
        ca_indices = []
        for token in range(151):
            index = np.flatnonzero(valid & (tokens == token) & (names == "CA"))
            if len(index) != 1:
                raise ValueError("exactly one named CA required per residue")
            ca_indices.append(index[0])
        counterfactual = np.stack([replace_target(xyz, valid, tokens, names, ref_atoms, ref_ca) for xyz in fold["coords"]])
        hypothetical_rows = S.E.evaluate_arrays({"design_mask": np.isin(np.arange(151), CDR)},
            dict(fold, coords=counterfactual), target_tokens=list(range(30)))
        for sample_index, xyz in enumerate(fold["coords"]):
            observed = candidate_scores[name]["full_cdr_samples"][sample_index]
            hypothetical = hypothetical_rows[sample_index]
            rows.append({"task_id": name, "source_draw": task["source_draw"], "arm": task["arm"],
                "sample_index": sample_index, "geometry": geometry(xyz[ca_indices], ref_ca, source_cache[task["source_draw"]]),
                "observed_contacts": {key: observed[key] for key in CONTACT_KEYS},
                "hypothetical_target_replacement_contacts": {key: hypothetical[key] for key in CONTACT_KEYS},
                "replacement_classification": "COUNTERFACTUAL_GEOMETRY_NOT_PREDICTION",
                "replacement_keeps_predicted_vhh_coordinates_unchanged": True})
    if len(rows) != 18:
        raise ValueError("all eighteen registered predictions must be retained")
    implementations = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in (
        Path(__file__).name, "diagnose_vhh_terminal_interface.py", "score_vhh_local_edit_20260913.py", "evaluate_vhh_epitope.py")}
    public = {"schema": "VHH_TERMINAL_POSE_DIAGNOSIS_V1", "status": "CPU_DIAGNOSIS_COMPLETE",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "gpu_started": False,
        "biological_pass": False, "causal_failure_mechanism_established": False,
        "sample_count": 18, "candidate_count": 6, "source_draw_count": 2,
        "central_target_tokens_zero_based": CENTRAL.tolist(), "his_ala_tokens_zero_based": [0, 1],
        "framework_token_count": len(FRAMEWORK), "full_cdr_token_count": len(CDR),
        "source_baselines": source_baselines, "samples": rows,
        "by_arm": {arm: summarize([r for r in rows if r["arm"] == arm]) for arm in S.ARMS},
        "overall": summarize(rows), "original_contact_counts_reconciled": expected_counts,
        "implementation_sha256": implementations,
        "input_content_sha256": sorted({r["sha256"] for r in inputs.values()}),
        "limitations": [
            "All metrics are retrospective geometry descriptors, not independent additive causal contributions.",
            "Target central fit uses tokens 2..27; terminal metrics concern His7/Ala8 tokens 0/1, with the C-terminal pair reported separately.",
            "Relative-pose framework RMSD includes residual framework deformation; independent framework-fit RMSD, centroid displacement and rotation are supplied separately.",
            "Both source and predicted complexes are independently aligned by target central CA to the same original target before relative-pose comparison.",
            "The coordinate replacement substitutes the frozen original target conformation into each prediction while retaining predicted VHH atoms; it is not a model prediction or relaxed structure.",
            "Hypothetical contact recovery cannot prove target deformation caused failure, or that a physically valid bound state exists.",
            "Six samples per arm are nested within two source candidates; ORIGINAL and SEQUENCE_ONLY have identical sequences in this completed run.",
            "Canonical atom mapping does not verify GLP1 terminal chemistry, affinity, selectivity or biological activity."],
        "analysis_seconds": time.perf_counter()-started}
    private = {**public, "inputs": list(inputs.values())}
    output.mkdir(parents=True, mode=0o700)
    for name, payload in (("PRIVATE_TERMINAL_POSE_DIAGNOSIS.json", private), ("PUBLIC_TERMINAL_POSE_DIAGNOSIS.json", public)):
        with (output/name).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    return public


def main():
    """Run the bounded CPU diagnostic once and summarize only sanitized counts."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("attempt", "prepared", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.attempt, args.prepared, args.output)
    print(json.dumps({"status": result["status"], "sample_count": result["sample_count"],
        "analysis_seconds": result["analysis_seconds"], "by_arm": result["by_arm"]}))


if __name__ == "__main__":
    main()
