#!/usr/bin/env python3
"""Post-run contact-location description of all 18 local-edit free folds.

Frozen scorer checks source/atom integrity; contact locations are independently
recomputed here from heavy-atom distances, not its scores. No GPU or new gate.
The target regions are retrospective descriptive bins, not revised endpoints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_vhh_local_edit_20260913 as S

CUTOFF = 4.5
CDRS = {"CDR1": tuple(range(55, 63)), "CDR2": tuple(range(80, 87)),
        "CDR3": tuple(range(125, 140))}
REGIONS = {"terminal": (7, 8), "near_N": (9, 14),
           "middle": (15, 28), "C_region": (29, 36)}
NEGATIVE_CLASSES = ("CDR_middle_or_C_contact", "CDR_other_region_only",
                    "framework_only_contact", "no_VHH_contact_at_cutoff")


def describe_sample(coords, mapping, window):
    """Geometry-only helper; caller must validate full canonical atom identities.

    CDR1/2/3/framework partition the binder. Full_CDR is their CDR union;
    edit_window is an overlapping diagnostic subset and is never added to it.
    """
    xyz = np.asarray(coords, dtype=float)
    mapping = np.asarray(mapping)
    if (xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all()
            or mapping.shape != (len(xyz), 151) or not np.isin(mapping, [0, 1]).all()
            or np.any(mapping.sum(axis=1) > 1)):
        raise ValueError("finite coordinates and unambiguous 151-token atom mapping required")
    window = tuple(window)
    if (len(window) != 5 or any(type(i) not in (int, np.int64, np.int32) for i in window)
            or any(b - a != 1 for a, b in zip(window, window[1:]))
            or not set(window).issubset(S.FULL_CDR_TOKENS)):
        raise ValueError("five consecutive original-CDR window tokens required")
    valid = mapping.sum(axis=1) == 1
    token = mapping.argmax(axis=1)
    if not np.all(np.bincount(token[valid], minlength=151) > 0):
        raise ValueError("all target and binder residues must have assigned atoms")
    target = valid & (token < 30)
    binder = valid & (token >= 30)
    distances2 = np.sum((xyz[target, None] - xyz[None, binder]) ** 2, axis=-1)
    residue_min2 = np.full((30, 121), np.inf)
    np.minimum.at(residue_min2, (token[target, None], token[None, binder] - 30), distances2)
    contacts = residue_min2 <= CUTOFF ** 2
    components = {**CDRS, "framework": tuple(i for i in range(30, 151) if i not in S.FULL_CDR_TOKENS),
                  "full_CDR": S.FULL_CDR_TOKENS, "edit_window": window}
    by_component = {}
    for name, tokens in components.items():
        positions = np.asarray(tokens) - 30
        relevant = contacts[:, positions]
        target_indices = np.flatnonzero(relevant.any(axis=1))
        target_native = (target_indices + 7).tolist()
        a, b = np.nonzero(relevant)
        pairs = [{"target_native_residue": int(t + 7), "vhh_label_seq_id": int(tokens[v] - 29),
                  "vhh_original_token": int(tokens[v]),
                  "minimum_distance_angstrom": float(np.sqrt(residue_min2[t, tokens[v] - 30]))}
                 for t, v in zip(a, b)]
        by_component[name] = {"target_native_residues": target_native,
            "contacting_target_residue_count": len(target_native), "residue_pair_count": len(pairs),
            "contact_pairs": pairs, "both_terminal_contact": bool(relevant[0].any() and relevant[1].any()),
            "terminal_min_distances_angstrom": {"His7": float(np.sqrt(residue_min2[0, positions].min())),
                                                "Ala8": float(np.sqrt(residue_min2[1, positions].min()))},
            "region_contact": {region: any(lo <= t <= hi for t in target_native)
                               for region, (lo, hi) in REGIONS.items()}}
    full = by_component["full_CDR"]
    negative_class = None
    if not full["both_terminal_contact"]:
        if full["region_contact"]["middle"] or full["region_contact"]["C_region"]:
            negative_class = NEGATIVE_CLASSES[0]
        elif full["target_native_residues"]:
            negative_class = NEGATIVE_CLASSES[1]
        elif by_component["framework"]["target_native_residues"]:
            negative_class = NEGATIVE_CLASSES[2]
        else:
            negative_class = NEGATIVE_CLASSES[3]
    terminal_contributors = {endpoint: [name for name in CDRS if native in by_component[name]["target_native_residues"]]
                             for endpoint, native in (("His7", 7), ("Ala8", 8))}
    return {"full_CDR_both_terminal_contact": full["both_terminal_contact"],
            "terminal_contributing_CDRs": terminal_contributors,
            "edit_window_terminal_contribution": {name: native in by_component["edit_window"]["target_native_residues"]
                                                  for name, native in (("His7", 7), ("Ala8", 8))},
            "terminal_negative_location_class": negative_class, "by_component": by_component}


def aggregate(candidates):
    """Public allowlist; no candidate identities, sequences, coordinates or paths."""
    rows = [r for candidate in candidates for r in candidate["samples"]]
    positives = [r for r in rows if r["full_CDR_both_terminal_contact"]]
    return {"candidate_draw_count": len(candidates), "free_fold_sample_count": len(rows),
        "folds_nested_per_candidate": 3, "full_CDR_both_terminal_count": len(positives),
        "candidate_all_three_both_terminal_count": sum(all(r["full_CDR_both_terminal_contact"] for r in c["samples"]) for c in candidates),
        "any_full_CDR_contact_count": sum(bool(r["by_component"]["full_CDR"]["target_native_residues"]) for r in rows),
        "any_framework_contact_count": sum(bool(r["by_component"]["framework"]["target_native_residues"]) for r in rows),
        "both_terminal_positive_CDR_contributor_counts": {
            name: {ep: sum(name in r["terminal_contributing_CDRs"][ep] for r in positives) for ep in ("His7", "Ala8")}
            for name in CDRS},
        "both_terminal_positive_edit_window_contribution_counts": {
            ep: sum(r["edit_window_terminal_contribution"][ep] for r in positives) for ep in ("His7", "Ala8")},
        "terminal_negative_location_counts_disjoint": {name: sum(r["terminal_negative_location_class"] == name for r in rows) for name in NEGATIVE_CLASSES},
        "region_contact_fold_counts_nonexclusive": {
            component: {region: sum(r["by_component"][component]["region_contact"][region] for r in rows) for region in REGIONS}
            for component in (*CDRS, "full_CDR", "edit_window", "framework")}}


def diagnose(attempt):
    """Validate all six predetermined tasks and independently locate 18 contacts."""
    started = time.monotonic()
    attempt = Path(attempt).resolve()
    manifest_path = attempt / "FINAL_INPUTS.json"
    manifest_raw = manifest_path.read_bytes()
    final = json.loads(manifest_raw)
    # Frozen CPU validator checks resolved/canonical heavy atoms, finite arrays,
    # padding, side-chain placeholders, confidence shape, source CIF identities,
    # fixed full sequences, all framework and outside-window residues, and closure.
    validated, _ = S.score_outputs(attempt, {"tasks": final["tasks"]})
    inputs = {str(Path(row["path"]).resolve()): row["sha256"] for row in validated["inputs"]}
    inputs[str(manifest_path)] = hashlib.sha256(manifest_raw).hexdigest()
    for relative, expected in final["sha256"].items():
        path = (attempt / relative).resolve()
        if not path.is_relative_to(attempt) or not path.is_file() or path.is_symlink():
            raise ValueError("final input binding escapes attempt or is not a regular file")
        key = str(path)
        if key not in inputs:
            inputs[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        if inputs[key] != expected:
            raise ValueError("fixed final input binding changed")
    candidates = []
    for task in final["tasks"]:
        path = attempt / "intermediate_designs/fold_out_npz" / (task["task_id"] + ".npz")
        with np.load(path, allow_pickle=False) as fold:
            samples = [dict(fold_index=i, **describe_sample(xyz, fold["atom_to_token"][0], task["edit_window_tokens"]))
                       for i, xyz in enumerate(fold["coords"])]
        candidates.append({"task_id": task["task_id"], "source_draw": task["source_draw"], "arm": task["arm"], "samples": samples})
    public = {"schema": "VHH_CONTACT_LOCATION_DIAGNOSIS_V1",
        "status": "RETROSPECTIVE_DESCRIPTIVE_ANALYSIS_COMPLETE", "contact_threshold_angstrom": CUTOFF,
        "geometry_source": "FINAL_FREE_FOLD_PREDICTIONS_ONLY", "target_numbering": "native_GLP1_token_plus_7",
        "regions_inclusive_native_numbering": REGIONS, "source_draw_count": 2,
        "all_18_predetermined_samples_retained": True, "source_and_full_atom_validation": True,
        "all_framework_and_outside_window_identities_preserved": True,
        "original_terminal_endpoint_changed": False, "biological_pass": False,
        "causal_editing_benefit_established": False, "new_downstream_gpu_authorized": False,
        "overall": aggregate(candidates),
        "by_arm": {arm: aggregate([c for c in candidates if c["arm"] == arm]) for arm in S.ARMS},
        "limitations": ["Region bins and component locations are post-run descriptions, not new success criteria.",
            "CDR1/2/3/framework are disjoint; full_CDR is their CDR union and edit_window overlaps CDR3.",
            "A target residue may contact several components or regions; those counts must not be summed as independent samples.",
            "Three predictions are nested within each sequence; two matched source draws per arm are not six independent molecules.",
            "Prediction indices are not random-number paired; unchanged sequences and every original draw are retained.",
            "Contact location does not establish experimental binding, specificity, affinity or the cause of failure.",
            "No_VHH_contact means no heavy-atom pair within 4.5 angstrom, not proof of physical dissociation."]}
    dependencies = (Path(__file__), Path(S.__file__), Path(S.E.__file__))
    private = {**public, "candidates": candidates,
        "input_sha256": inputs,
        "implementation_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dependencies},
        "elapsed_seconds": time.monotonic() - started}
    # Reconcile only after independent calculations; no expected result is used
    # to select structures or define the descriptive location classes.
    for arm in S.ARMS:
        original = validated["by_arm"][arm]["primary_full_cdr"]
        current = public["by_arm"][arm]
        if (current["full_CDR_both_terminal_count"] != original["sample_contact_counts"]["both_epitope_contacts"]
                or current["any_full_CDR_contact_count"] != original["sample_contact_counts"]["any_target_cdr_contact"]):
            raise ValueError("independent recomputation differs from frozen endpoint calculations")
    json.dumps(private, allow_nan=False)
    return private, public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("new output directory required; previous results are never overwritten")
    private, public = diagnose(args.attempt)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, data in (("PRIVATE_CONTACT_LOCATION.json", private), ("PUBLIC_CONTACT_LOCATION.json", public)):
        with (args.output_dir / name).open("x") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
    print(json.dumps({"status": public["status"], "overall": public["overall"], "elapsed_seconds": private["elapsed_seconds"]}, allow_nan=False))


if __name__ == "__main__":
    main()
