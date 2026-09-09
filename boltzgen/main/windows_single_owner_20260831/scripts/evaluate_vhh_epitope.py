#!/usr/bin/env python3
"""Re-evaluate modeled VHH contacts on CPU; never infer biological PASS.

This bounded reader supports canonical-protein BoltzGen fold NPZ files with a
33-class residue encoding and complete modeled heavy-atom slots. Fold NPZ does
not contain atom_pad_mask/ref_element, so unique assignment, resolved-mask
agreement and canonical heavy-atom counts are required. It does NOT use the
original design's unresolved-sidechain mask to discard predicted CDR atoms.
Noncanonical/chemically modified targets require a separate atom-aware reader;
passing this reader does not establish terminal amidation or protonation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

SCHEMA = "VHH_EPITOPE_GEOMETRY_V1"
CLAIM_BOUNDARY = "EXPLORATORY_MODELED_GEOMETRY_ONLY_NOT_BINDING_OR_SELECTIVITY_EVIDENCE"
# BoltzGen canonical residue IDs 2..21; counts exclude hydrogens.
RESIDUES = ("ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL")
HEAVY_COUNTS = np.asarray((5, 11, 8, 8, 6, 9, 9, 4, 10, 8, 8, 9, 8, 11, 7, 6, 7, 14, 12, 7))
PUBLIC_METHODS = {"t11_default_template", "t12_split_template", "native_positive_control", "exploratory"}
NUMERIC_METRICS = (
    "min_target_cdr_distance_angstrom", "min_target_vhh_distance_angstrom",
    "his_min_cdr_distance_angstrom", "ala_min_cdr_distance_angstrom",
    "target_cdr_residue_pair_count", "participating_target_residue_count",
    "participating_cdr_residue_count", "participating_cdr_fraction",
)
BOOLEAN_METRICS = ("any_target_cdr_contact", "his_contact", "ala_contact", "both_epitope_contacts", "severe_close_contact_diagnostic")
DEVELOPABILITY_METRICS = ("cdr_residue_count", "cdr_cysteine_count", "cdr_hydrophobic_residue_fraction", "cdr_kr_minus_de_charge_proxy")


class ValidationError(ValueError):
    """Input is outside the explicitly supported geometry contract."""


def _binary(value: Any, shape: tuple[int, ...], label: str) -> np.ndarray:
    array = np.asarray(value)
    if array.shape != shape or not np.isin(array, (0, 1)).all():
        raise ValidationError(f"{label}: expected binary array of shape {shape}")
    return array.astype(bool)


def _summary(values: Sequence[float]) -> dict[str, float | None]:
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {"min": None, "median": None, "max": None}
    if not np.isfinite(array).all():
        raise ValidationError("nonfinite summary values")
    return {"min": float(array.min()), "median": float(np.median(array)), "max": float(array.max())}


def _config(target_tokens: Sequence[int], his_token: int, ala_token: int, token_count: int, threshold: float) -> np.ndarray:
    raw = np.asarray(target_tokens)
    if raw.ndim != 1 or raw.size == 0 or not np.issubdtype(raw.dtype, np.integer):
        raise ValidationError("target tokens must be an explicit nonempty integer list")
    if len(set(raw.tolist())) != raw.size or np.any(raw < 0) or np.any(raw >= token_count):
        raise ValidationError("target tokens must be unique and in range")
    if isinstance(his_token, bool) or isinstance(ala_token, bool) or not isinstance(his_token, (int, np.integer)) or not isinstance(ala_token, (int, np.integer)):
        raise ValidationError("epitope indices must be integers")
    if his_token == ala_token or his_token not in raw or ala_token not in raw:
        raise ValidationError("distinct His/Ala indices must belong to target")
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValidationError("contact threshold must be finite and positive")
    return np.sort(raw.astype(int))


def evaluate_arrays(
    design: Mapping[str, np.ndarray], fold: Mapping[str, np.ndarray], *,
    target_tokens: Sequence[int], his_token: int = 0, ala_token: int = 1,
    threshold: float = 4.5,
) -> list[dict[str, Any]]:
    """Return one private geometry row per fold, without writing or inference."""
    required = {"coords", "atom_to_token", "atom_resolved_mask", "token_index", "res_type", "mol_type"}
    if "design_mask" not in design or required - set(fold):
        raise ValidationError("missing design_mask or required fold arrays")
    mask = np.asarray(design["design_mask"])
    if mask.ndim != 1 or not mask.size:
        raise ValidationError("design_mask must be a nonempty token vector")
    token_count = mask.size
    cdr_mask = _binary(mask, (token_count,), "design_mask")
    target_tokens = _config(target_tokens, his_token, ala_token, token_count, threshold)
    target_mask = np.zeros(token_count, dtype=bool)
    target_mask[target_tokens] = True
    if not cdr_mask.any() or np.any(cdr_mask & target_mask):
        raise ValidationError("nonempty designed CDR mask must be disjoint from target")
    raw_coords = np.asarray(fold["coords"])
    if raw_coords.dtype.kind not in "fiu":
        raise ValidationError("coords must be a real numeric array")
    coords = raw_coords.astype(float)
    if coords.ndim != 3 or coords.shape[0] < 1 or coords.shape[1] < 1 or coords.shape[2] != 3:
        raise ValidationError("coords must be (positive samples, positive atoms, 3)")
    if not np.isfinite(coords).all():
        raise ValidationError("coords contain NaN/Inf, including padding")
    atom_count = coords.shape[1]
    if not np.array_equal(fold["token_index"], np.arange(token_count)[None, :]):
        raise ValidationError("token_index does not match contiguous zero-based tokens")
    mol_type = np.asarray(fold["mol_type"])
    if mol_type.shape != (1, token_count) or not np.all(mol_type == 0):
        raise ValidationError("only the protein mol_type=0 encoding is supported")
    residues = _binary(fold["res_type"], (1, token_count, 33), "res_type")[0]
    if not np.all(residues.sum(axis=1) == 1):
        raise ValidationError("res_type must be one-hot")
    residue_ids = residues.argmax(axis=1)
    if np.any(residue_ids < 2) or np.any(residue_ids > 21):
        raise ValidationError("only canonical amino acids are supported")
    if residue_ids[his_token] != 10 or residue_ids[ala_token] != 2:
        raise ValidationError("configured epitope does not identify HIS and ALA")
    mapping = _binary(fold["atom_to_token"], (1, atom_count, token_count), "atom_to_token")[0]
    assigned_count = mapping.sum(axis=1)
    if np.any(assigned_count > 1):
        raise ValidationError("atom_to_token contains multiple assignments")
    assigned = assigned_count == 1
    resolved = _binary(fold["atom_resolved_mask"], (1, atom_count), "atom_resolved_mask")[0]
    if not np.array_equal(assigned, resolved):
        raise ValidationError("assigned/resolved mismatch: cannot establish modeled atom validity")
    if not np.array_equal(mapping.sum(axis=0), HEAVY_COUNTS[residue_ids - 2]):
        raise ValidationError("canonical heavy-atom counts mismatch; missing/extra atoms unsupported")
    # Do not infer a padding atom's identity from argmax(zeros)==0.
    atom_tokens = np.full(atom_count, -1, dtype=int)
    atom_tokens[assigned] = mapping[assigned].argmax(axis=1)
    target_atoms = np.flatnonzero(assigned & np.isin(atom_tokens, target_tokens))
    cdr_tokens = np.flatnonzero(cdr_mask)
    cdr_atoms = np.flatnonzero(assigned & np.isin(atom_tokens, cdr_tokens))
    vhh_atoms = np.flatnonzero(assigned & ~np.isin(atom_tokens, target_tokens))
    token_to_target = {int(token): index for index, token in enumerate(target_tokens)}
    token_to_cdr = {int(token): index for index, token in enumerate(cdr_tokens)}
    target_atom_rows = np.asarray([token_to_target[int(atom_tokens[a])] for a in target_atoms])
    cdr_atom_columns = np.asarray([token_to_cdr[int(atom_tokens[a])] for a in cdr_atoms])
    rows = []
    for sample_index, sample in enumerate(coords):
        distances = np.linalg.norm(sample[target_atoms, None, :] - sample[None, cdr_atoms, :], axis=-1)
        if not np.isfinite(distances).all():
            raise ValidationError("pairwise distances overflowed")
        residue_min = np.full((len(target_tokens), len(cdr_tokens)), np.inf)
        np.minimum.at(residue_min, (target_atom_rows[:, None], cdr_atom_columns[None, :]), distances)
        contacts = residue_min <= threshold
        his_min = float(residue_min[token_to_target[his_token]].min())
        ala_min = float(residue_min[token_to_target[ala_token]].min())
        vhh_min = float(np.linalg.norm(sample[target_atoms, None, :] - sample[None, vhh_atoms, :], axis=-1).min())
        if not math.isfinite(vhh_min):
            raise ValidationError("target-VHH distances overflowed")
        pairs = [[int(target_tokens[t]), int(cdr_tokens[c])] for t, c in np.argwhere(contacts)]
        rows.append({
            "sample_index": sample_index,
            "min_target_cdr_distance_angstrom": float(distances.min()),
            "min_target_vhh_distance_angstrom": vhh_min,
            "his_min_cdr_distance_angstrom": his_min,
            "ala_min_cdr_distance_angstrom": ala_min,
            "any_target_cdr_contact": bool(contacts.any()),
            "his_contact": his_min <= threshold,
            "ala_contact": ala_min <= threshold,
            "both_epitope_contacts": his_min <= threshold and ala_min <= threshold,
            "target_cdr_residue_pair_count": len(pairs),
            "participating_target_residue_count": int(contacts.any(axis=1).sum()),
            "participating_cdr_residue_count": int(contacts.any(axis=0).sum()),
            "participating_cdr_fraction": float(contacts.any(axis=0).mean()),
            "severe_close_contact_diagnostic": bool(distances.min() < 1.5),
            "contact_residue_pairs_zero_based": pairs,
            "per_target_residue": [
                {"token_index": int(token), "min_cdr_distance_angstrom": float(residue_min[i].min()),
                 "contacting_cdr_residue_count": int(contacts[i].sum())}
                for i, token in enumerate(target_tokens)
            ],
        })
    return rows


def candidate_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValidationError("candidate has no samples")
    maps = [{tuple(pair) for pair in row["contact_residue_pairs_zero_based"]} for row in rows]
    similarities = []
    empty_pairs = 0
    for left, right in itertools.combinations(maps, 2):
        union = left | right
        if not union:
            empty_pairs += 1
        else:
            similarities.append(len(left & right) / len(union))
    return {
        "sample_count": len(rows),
        "counts": {name: sum(bool(row[name]) for row in rows) for name in BOOLEAN_METRICS},
        "metrics": {name: _summary([row[name] for row in rows]) for name in NUMERIC_METRICS},
        "within_candidate_contact_map_jaccard": {
            "pair_count": len(rows) * (len(rows) - 1) // 2,
            "defined_pair_count": len(similarities),
            "both_empty_undefined_pair_count": empty_pairs,
            "values_summary": _summary(similarities),
            "empty_map_rule": "both_empty=null_excluded; one_empty=0; fewer_than_two_samples=no_pairs",
        },
    }


def developability_descriptors(design: Mapping[str, np.ndarray], fold: Mapping[str, np.ndarray]) -> dict[str, int | float]:
    """Composition-only descriptors after evaluate_arrays validates identity.

    No sequence is returned; KR-DE omits His/protonation and is NOT net charge.
    Hydrophobic set is the declared convention A,V,I,L,M,F,W,Y, not surface
    exposure. No expression, aggregation or biological gate is asserted.
    """
    cdr_ids = np.asarray(fold["res_type"])[0].argmax(axis=1)[np.asarray(design["design_mask"], dtype=bool)]
    names = [RESIDUES[int(index) - 2] for index in cdr_ids]
    hydrophobic = {"ALA", "VAL", "ILE", "LEU", "MET", "PHE", "TRP", "TYR"}
    return {
        "cdr_residue_count": len(names),
        "cdr_cysteine_count": names.count("CYS"),
        "cdr_hydrophobic_residue_fraction": sum(name in hydrophobic for name in names) / len(names),
        "cdr_kr_minus_de_charge_proxy": names.count("LYS") + names.count("ARG") - names.count("ASP") - names.count("GLU"),
    }


def _load_npz(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"input must be a regular nonsymlink file: {path}")
    payload = path.read_bytes()  # One read, one hash; no expensive replay audit.
    try:
        with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
            arrays = {key: archive[key] for key in archive.files}
    except (ValueError, OSError, EOFError) as exc:
        raise ValidationError(f"invalid NPZ: {path}") from exc
    return arrays, {"path": str(path.resolve()), "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def execute(design_root: Path, output_dir: Path, method: str, *, target_tokens: Sequence[int] = tuple(range(30)), his_token: int = 0, ala_token: int = 1, threshold: float = 4.5, expected_candidates: int | None = None, expected_samples: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    root = design_root.expanduser().resolve(strict=True)
    output = output_dir.expanduser().absolute()
    if not root.is_dir() or output.exists() or output.is_symlink():
        raise ValidationError("design root must exist; output directory must be new")
    resolved_output = output.resolve()
    if resolved_output == root or root in resolved_output.parents:
        raise ValidationError("outputs must not be written inside source design root")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", method):
        raise ValidationError("method must be a short lowercase machine label")
    if (expected_candidates is not None and expected_candidates < 1) or (expected_samples is not None and expected_samples < 1):
        raise ValidationError("expected counts must be positive")
    designs = sorted(root.glob("design_*.npz"), key=lambda p: p.name)
    if not designs or any(not re.fullmatch(r"design_[0-9]+\.npz", p.name) for p in designs):
        raise ValidationError("no valid design_N.npz files or malformed candidate filename")
    folds = list((root / "fold_out_npz").glob("design_*.npz"))
    if {p.name for p in folds} != {p.name for p in designs}:
        raise ValidationError("design/fold file set mismatch")
    if expected_candidates is not None and len(designs) != expected_candidates:
        raise ValidationError("unexpected candidate count")
    candidates, all_rows, inputs = {}, [], []
    for path in designs:
        design, design_receipt = _load_npz(path)
        fold, fold_receipt = _load_npz(root / "fold_out_npz" / path.name)
        rows = evaluate_arrays(design, fold, target_tokens=target_tokens, his_token=his_token, ala_token=ala_token, threshold=threshold)
        if expected_samples is not None and len(rows) != expected_samples:
            raise ValidationError("unexpected per-candidate sample count")
        candidates[path.stem] = {"summary": candidate_summary(rows), "developability_descriptors": developability_descriptors(design, fold), "samples": rows}
        all_rows.extend(rows)
        inputs.extend((design_receipt, fold_receipt))
    counts = {name: sum(bool(row[name]) for row in all_rows) for name in BOOLEAN_METRICS}
    elapsed = time.perf_counter() - started
    common = {
        "schema_version": SCHEMA, "status": "COMPUTATIONAL_REEVALUATION_COMPLETE",
        "analysis_classification": "RETROSPECTIVE_EXPLORATORY",
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "claim_boundary": CLAIM_BOUNDARY, "gpu_performed": False,
        "contact_threshold_angstrom": threshold, "contact_operator": "<=",
        "severe_close_contact_threshold_angstrom": 1.5,
        "severe_close_contact_interpretation": "target-CDR distance<1.5 diagnostic only; not all target-VHH pairs, atom-type-aware vdW clash or a pass/fail gate",
        "target_token_indices_zero_based": [int(x) for x in target_tokens],
        "epitope_token_indices_zero_based": {"HIS": his_token, "ALA": ala_token},
        "atom_contract": "canonical_protein_heavy_atom_counts; unique_mapping==fold_resolved; unmapped_padding_excluded; design_sidechain_resolution_not_used",
        "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED",
        "candidate_count": len(candidates), "sample_count": len(all_rows),
        "counts": counts, "source_file_count": len(inputs),
        "analysis_seconds_including_single_input_read_and_hash": elapsed,
        "sample_interpretation": "Repeated folds are nested within candidates, not independent molecules; no cross-method seed pairing is assumed.",
        "developability_interpretation": "CDR composition only, no gate: hydrophobic set=A,V,I,L,M,F,W,Y; charge proxy=(K+R)-(D+E), excluding histidine/protonation. Not expression, aggregation, surface exposure or net-charge measurements.",
    }
    # Public report is assembled from an explicit aggregate allowlist. Never
    # serialize private candidate dictionaries and then try to redact strings.
    public = {
        **common, "method": method if method in PUBLIC_METHODS else "custom_method",
        "sample_metric_distributions_descriptive_only": {name: _summary([row[name] for row in all_rows]) for name in NUMERIC_METRICS},
        "candidate_median_distributions": {name: _summary([candidate["summary"]["metrics"][name]["median"] for candidate in candidates.values()]) for name in NUMERIC_METRICS},
        "candidate_developability_distributions": {name: _summary([candidate["developability_descriptors"][name] for candidate in candidates.values()]) for name in DEVELOPABILITY_METRICS},
        "candidate_all_samples_contact_counts": {name: sum(c["summary"]["counts"][name] == c["summary"]["sample_count"] for c in candidates.values()) for name in BOOLEAN_METRICS},
        "candidate_jaccard_median_distribution": _summary([c["summary"]["within_candidate_contact_map_jaccard"]["values_summary"]["median"] for c in candidates.values() if c["summary"]["within_candidate_contact_map_jaccard"]["values_summary"]["median"] is not None]),
        "candidate_jaccard_undefined_median_count": sum(c["summary"]["within_candidate_contact_map_jaccard"]["values_summary"]["median"] is None for c in candidates.values()),
        "per_target_contact_sample_counts": [{"token_index": int(token), "contact_sample_count": sum(row["per_target_residue"][i]["contacting_cdr_residue_count"] > 0 for row in all_rows)} for i, token in enumerate(sorted(target_tokens))],
        "limitations": ["Contacts are predicted geometry, not binding/affinity/selectivity.", "His/Ala contacts are exploratory mechanism diagnostics, not a biological success gate.", "No buried-surface, atom-type-aware clash, terminal chemistry or experimental calibration is established here.", "Reference-pose RMSD remains a separate secondary diagnostic; it is not recomputed or used as truth here."],
    }
    private = {**common, "method": method, "created_utc": datetime.now(timezone.utc).isoformat(), "inputs": inputs, "candidates": candidates}
    # Validate complete serialization before creating the destination.
    encoded = {"PRIVATE_EPITOPE_EVALUATION.json": json.dumps(private, indent=2, sort_keys=True, allow_nan=False) + "\n", "PUBLIC_EPITOPE_SUMMARY.json": json.dumps(public, indent=2, sort_keys=True, allow_nan=False) + "\n"}
    output.mkdir(parents=True, exist_ok=False)
    for name, value in encoded.items():
        with (output / name).open("x", encoding="utf-8") as stream:
            stream.write(value)
    return public


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--method", required=True)
    parser.add_argument("--target-token-indices", default=",".join(map(str, range(30))), help="Explicit comma-separated zero-based target indices; default GLP-1 tokens 0..29")
    parser.add_argument("--his-token-index", type=int, default=0)
    parser.add_argument("--ala-token-index", type=int, default=1)
    parser.add_argument("--contact-threshold", type=float, default=4.5)
    parser.add_argument("--expected-candidates", type=int)
    parser.add_argument("--expected-samples", type=int)
    args = parser.parse_args(argv)
    try:
        target_tokens = tuple(int(value) for value in args.target_token_indices.split(","))
        result = execute(args.design_root, args.output_dir, args.method, target_tokens=target_tokens, his_token=args.his_token_index, ala_token=args.ala_token_index, threshold=args.contact_threshold, expected_candidates=args.expected_candidates, expected_samples=args.expected_samples)
    except (ValidationError, OSError, ValueError) as exc:
        parser.exit(2, f"validation failed: {exc}\n")
    print(json.dumps({"status": result["status"], "candidate_count": result["candidate_count"], "sample_count": result["sample_count"], "counts": result["counts"]}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
