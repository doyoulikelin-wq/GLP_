#!/usr/bin/env python3
"""Audit all historical T12 fold samples without running a model."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


SCHEMA = "WINDOWS_OWNER_T12_FRAMEWORK_ALIGNED_CDR_AUDIT_V1"
SAMPLES_PER_DESIGN = 5
TOTAL_TOKENS = 151
TARGET_TOKEN_COUNT = 30
VHH_TOKEN_COUNT = 121
CDR_TOKEN_INDICES = tuple(range(55, 63)) + tuple(range(80, 87)) + tuple(range(125, 140))
REQUIRED_SAMPLE_METRICS = ("design_to_target_iptm", "design_ptm", "iptm", "ptm")
ARM_ORDER = ("internal", "high_contact", "diverse", "fixed_ifold")
DEFAULT_DESIGN_COUNTS = {
    "internal": 10,
    "high_contact": 10,
    "diverse": 5,
    "fixed_ifold": 6,
}
DESIGN_NAME = re.compile(r"design_(0|[1-9][0-9]*)")
TSV_FIELDS = (
    "arm",
    "candidate_id",
    "sample_index",
    "framework_aligned_cdr_rmsd_angstrom",
    "target_aligned_cdr_rmsd_angstrom",
    "framework_gate_pass",
    *REQUIRED_SAMPLE_METRICS,
    "design_npz_sha256",
    "fold_npz_sha256",
)


class ValidationError(ValueError):
    """Raised when an input cannot be bound to the declared sample semantics."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--internal-root", required=True, type=Path)
    parser.add_argument("--high-contact-root", required=True, type=Path)
    parser.add_argument("--diverse-root", required=True, type=Path)
    parser.add_argument("--fixed-ifold-root", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-tsv", required=True, type=Path)
    parser.add_argument("--framework-threshold-angstrom", type=float, default=4.0)
    parser.add_argument("--fixed-ifold-min-pass", type=int, default=10)
    parser.add_argument("--internal-design-count", type=int, default=10)
    parser.add_argument("--high-contact-design-count", type=int, default=10)
    parser.add_argument("--diverse-design-count", type=int, default=5)
    parser.add_argument("--fixed-ifold-design-count", type=int, default=6)
    return parser.parse_args(argv)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_regular(path: Path) -> tuple[bytes, str]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValidationError(f"required file is not readable: {path}: {exc}") from exc
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"required path is not a regular non-symlink file: {path}")
    data = path.read_bytes()
    after = path.lstat()
    if _identity(before) != _identity(after):
        raise ValidationError(f"file changed while being read: {path}")
    return data, hashlib.sha256(data).hexdigest()


def _load_npz(path: Path) -> tuple[dict[str, np.ndarray], str]:
    data, digest = _read_regular(path)
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as archive:
            arrays = {name: np.asarray(archive[name]) for name in archive.files}
    except Exception as exc:
        raise ValidationError(f"unreadable NPZ {path}: {exc}") from exc
    if not arrays:
        raise ValidationError(f"empty NPZ: {path}")
    for name, value in arrays.items():
        if value.dtype.hasobject:
            raise ValidationError(f"object array is forbidden: {path}:{name}")
        if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
            raise ValidationError(f"NaN/Inf is forbidden: {path}:{name}")
    return arrays, digest


def _binary_mask(value: np.ndarray, shape: tuple[int, ...], label: str) -> np.ndarray:
    if value.shape != shape:
        raise ValidationError(f"{label} shape must be {shape}, got {value.shape}")
    if not (np.issubdtype(value.dtype, np.number) or np.issubdtype(value.dtype, np.bool_)):
        raise ValidationError(f"{label} must be numeric/bool")
    if not np.isfinite(value).all() or not np.isin(value, (0, 1)).all():
        raise ValidationError(f"{label} must be finite and binary")
    return value.astype(bool)


def _arm_root(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValidationError(f"{label} root must be absolute: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{label} root is unavailable: {path}: {exc}") from exc
    if path.is_symlink() or resolved != path or not path.is_dir():
        raise ValidationError(f"{label} root must be a canonical non-symlink directory: {path}")
    return path


def _design_files(root: Path, expected_count: int, arm: str) -> list[tuple[str, Path, Path]]:
    if expected_count <= 0:
        raise ValidationError(f"{arm} expected design count must be positive")
    fold_root = root / "fold_out_npz"
    if not fold_root.is_dir() or fold_root.is_symlink():
        raise ValidationError(f"{arm} fold_out_npz directory is missing or unsafe")
    observed_designs: dict[str, Path] = {}
    observed_folds: dict[str, Path] = {}
    for directory, destination in ((root, observed_designs), (fold_root, observed_folds)):
        for path in directory.iterdir():
            if not path.is_file() or path.suffix != ".npz":
                continue
            if DESIGN_NAME.fullmatch(path.stem):
                destination[path.stem] = path
    expected_ids = {f"design_{index}" for index in range(expected_count)}
    if set(observed_designs) != expected_ids or set(observed_folds) != expected_ids:
        raise ValidationError(
            f"{arm} candidate closure mismatch: "
            f"design={sorted(observed_designs)} fold={sorted(observed_folds)} "
            f"expected={sorted(expected_ids)}"
        )
    return [
        (candidate_id, observed_designs[candidate_id], observed_folds[candidate_id])
        for candidate_id in sorted(expected_ids, key=lambda value: int(value.split("_")[1]))
    ]


def _align_reference(
    reference: np.ndarray, prediction: np.ndarray, mask: np.ndarray, label: str
) -> np.ndarray:
    if mask.shape != (reference.shape[0],) or mask.sum() < 3:
        raise ValidationError(f"{label} alignment mask is invalid")
    source = reference[mask].astype(np.float64)
    target = prediction[mask].astype(np.float64)
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    try:
        left, _, right_t = np.linalg.svd(
            (source - source_center).T @ (target - target_center)
        )
    except np.linalg.LinAlgError as exc:
        raise ValidationError(f"{label} Kabsch SVD failed: {exc}") from exc
    rotation = left @ right_t
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right_t
    aligned = (reference.astype(np.float64) - source_center) @ rotation + target_center
    if not np.isfinite(aligned).all():
        raise ValidationError(f"{label} alignment produced NaN/Inf")
    return aligned


def _subset_rmsd(
    reference: np.ndarray,
    prediction: np.ndarray,
    align_mask: np.ndarray,
    score_mask: np.ndarray,
    label: str,
) -> float:
    if score_mask.shape != (reference.shape[0],) or not score_mask.any():
        raise ValidationError(f"{label} score mask is invalid")
    aligned = _align_reference(reference, prediction, align_mask, label)
    delta = prediction[score_mask].astype(np.float64) - aligned[score_mask]
    value = float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))
    if not math.isfinite(value):
        raise ValidationError(f"{label} RMSD is not finite")
    return value


def _candidate_rows(
    arm: str,
    candidate_id: str,
    design_path: Path,
    fold_path: Path,
    framework_threshold: float,
) -> list[dict[str, Any]]:
    design, design_sha = _load_npz(design_path)
    fold, fold_sha = _load_npz(fold_path)
    if "design_mask" not in design:
        raise ValidationError(f"{arm}/{candidate_id} design NPZ has no design_mask")
    design_mask = _binary_mask(
        design["design_mask"], (TOTAL_TOKENS,), f"{arm}/{candidate_id} design_mask"
    )
    if tuple(np.flatnonzero(design_mask)) != CDR_TOKEN_INDICES:
        raise ValidationError(f"{arm}/{candidate_id} CDR token mask drift")

    required = {
        "coords",
        "input_coords",
        "atom_to_token",
        "atom_resolved_mask",
        "backbone_mask",
        "token_index",
        *REQUIRED_SAMPLE_METRICS,
    }
    missing = required - set(fold)
    if missing:
        raise ValidationError(f"{arm}/{candidate_id} fold NPZ missing {sorted(missing)}")
    coords = fold["coords"]
    if coords.ndim != 3 or coords.shape[0] != SAMPLES_PER_DESIGN or coords.shape[2] != 3:
        raise ValidationError(
            f"{arm}/{candidate_id} coords shape must be (5, atoms, 3), got {coords.shape}"
        )
    atom_count = coords.shape[1]
    if fold["input_coords"].shape != (1, 1, atom_count, 3):
        raise ValidationError(
            f"{arm}/{candidate_id} input_coords shape mismatch: {fold['input_coords'].shape}"
        )
    for metric in REQUIRED_SAMPLE_METRICS:
        if fold[metric].shape != (SAMPLES_PER_DESIGN,):
            raise ValidationError(
                f"{arm}/{candidate_id} metric {metric} shape must be (5,), got {fold[metric].shape}"
            )
    token_index = fold["token_index"]
    if token_index.shape != (1, TOTAL_TOKENS) or not np.array_equal(
        token_index[0], np.arange(TOTAL_TOKENS)
    ):
        raise ValidationError(f"{arm}/{candidate_id} token_index drift")
    atom_to_token = _binary_mask(
        fold["atom_to_token"],
        (1, atom_count, TOTAL_TOKENS),
        f"{arm}/{candidate_id} atom_to_token",
    )[0]
    atom_assignment_count = atom_to_token.sum(axis=1)
    if not np.isin(atom_assignment_count, (0, 1)).all():
        raise ValidationError(f"{arm}/{candidate_id} atom_to_token is not zero/one-hot")
    resolved = _binary_mask(
        fold["atom_resolved_mask"], (1, atom_count), f"{arm}/{candidate_id} resolved"
    )[0]
    backbone = _binary_mask(
        fold["backbone_mask"], (1, atom_count), f"{arm}/{candidate_id} backbone"
    )[0]
    if np.any((resolved | backbone) & (atom_assignment_count != 1)):
        raise ValidationError(
            f"{arm}/{candidate_id} resolved/backbone atom has no unique token"
        )
    token = atom_to_token.argmax(axis=1)
    target = token < TARGET_TOKEN_COUNT
    cdr = design_mask[token]
    framework = (token >= TARGET_TOKEN_COUNT) & ~cdr
    resolved_backbone = resolved & backbone
    per_token_backbone = np.bincount(token[resolved_backbone], minlength=TOTAL_TOKENS)
    if per_token_backbone.shape != (TOTAL_TOKENS,) or not np.all(per_token_backbone == 4):
        raise ValidationError(f"{arm}/{candidate_id} requires four resolved backbone atoms per token")
    if int((resolved_backbone & target).sum()) != 120:
        raise ValidationError(f"{arm}/{candidate_id} target backbone mask drift")
    if int((resolved_backbone & framework).sum()) != 364:
        raise ValidationError(f"{arm}/{candidate_id} framework backbone mask drift")
    if int((resolved_backbone & cdr).sum()) != 120:
        raise ValidationError(f"{arm}/{candidate_id} CDR backbone mask drift")

    reference = fold["input_coords"][0, 0]
    rows: list[dict[str, Any]] = []
    for sample_index in range(SAMPLES_PER_DESIGN):
        prediction = coords[sample_index]
        identity = f"{arm}/{candidate_id}/sample_{sample_index}"
        framework_rmsd = _subset_rmsd(
            reference,
            prediction,
            resolved_backbone & framework,
            resolved_backbone & cdr,
            f"{identity} framework-aligned",
        )
        target_rmsd = _subset_rmsd(
            reference,
            prediction,
            resolved_backbone & target,
            resolved_backbone & cdr,
            f"{identity} target-aligned",
        )
        row: dict[str, Any] = {
            "arm": arm,
            "candidate_id": candidate_id,
            "sample_index": sample_index,
            "framework_aligned_cdr_rmsd_angstrom": framework_rmsd,
            "target_aligned_cdr_rmsd_angstrom": target_rmsd,
            "framework_gate_pass": framework_rmsd <= framework_threshold,
            "design_npz_sha256": design_sha,
            "fold_npz_sha256": fold_sha,
        }
        for metric in REQUIRED_SAMPLE_METRICS:
            row[metric] = float(fold[metric][sample_index])
        rows.append(row)
    return rows


def _metric_summary(values: Sequence[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "min": float(array.min()),
        "median": float(np.median(array)),
        "mean": float(array.mean()),
        "max": float(array.max()),
    }


def audit(
    arm_roots: Mapping[str, Path],
    design_counts: Mapping[str, int],
    framework_threshold: float = 4.0,
    fixed_ifold_min_pass: int = 10,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if set(arm_roots) != set(ARM_ORDER) or set(design_counts) != set(ARM_ORDER):
        raise ValidationError("exactly four named T12 arms are required")
    if not math.isfinite(framework_threshold) or framework_threshold <= 0:
        raise ValidationError("framework threshold must be finite and positive")
    fixed_samples = design_counts["fixed_ifold"] * SAMPLES_PER_DESIGN
    if fixed_ifold_min_pass <= 0 or fixed_ifold_min_pass > fixed_samples:
        raise ValidationError("fixed-ifold minimum pass must be within its sample count")

    rows: list[dict[str, Any]] = []
    resolved_roots: dict[str, str] = {}
    for arm in ARM_ORDER:
        root = _arm_root(arm_roots[arm], arm)
        resolved_roots[arm] = str(root)
        for candidate_id, design_path, fold_path in _design_files(
            root, design_counts[arm], arm
        ):
            rows.extend(
                _candidate_rows(
                    arm,
                    candidate_id,
                    design_path,
                    fold_path,
                    framework_threshold,
                )
            )
    identities = {(r["arm"], r["candidate_id"], r["sample_index"]) for r in rows}
    expected_rows = sum(design_counts.values()) * SAMPLES_PER_DESIGN
    if len(rows) != expected_rows or len(identities) != expected_rows:
        raise ValidationError("(arm, design_i, sample_index) sample closure mismatch")

    arm_summaries: dict[str, Any] = {}
    candidate_summaries: dict[str, dict[str, Any]] = {}
    for arm in ARM_ORDER:
        arm_rows = [row for row in rows if row["arm"] == arm]
        framework_values = [row["framework_aligned_cdr_rmsd_angstrom"] for row in arm_rows]
        target_values = [row["target_aligned_cdr_rmsd_angstrom"] for row in arm_rows]
        arm_summaries[arm] = {
            "design_count": design_counts[arm],
            "sample_count": len(arm_rows),
            "framework_aligned_cdr_rmsd_angstrom": _metric_summary(framework_values),
            "target_aligned_cdr_rmsd_angstrom": _metric_summary(target_values),
            "framework_le_threshold_count": sum(bool(row["framework_gate_pass"]) for row in arm_rows),
            "framework_le_8_angstrom_count": sum(value <= 8.0 for value in framework_values),
        }
        candidate_summaries[arm] = {}
        for candidate_id in sorted(
            {str(row["candidate_id"]) for row in arm_rows},
            key=lambda value: int(value.split("_")[1]),
        ):
            candidate_rows = [
                row for row in arm_rows if row["candidate_id"] == candidate_id
            ]
            candidate_framework = [
                row["framework_aligned_cdr_rmsd_angstrom"] for row in candidate_rows
            ]
            candidate_target = [
                row["target_aligned_cdr_rmsd_angstrom"] for row in candidate_rows
            ]
            candidate_summaries[arm][candidate_id] = {
                "sample_count": len(candidate_rows),
                "framework_le_threshold_count": sum(
                    bool(row["framework_gate_pass"]) for row in candidate_rows
                ),
                "framework_aligned_cdr_rmsd_angstrom": _metric_summary(
                    candidate_framework
                ),
                "target_aligned_cdr_rmsd_angstrom": _metric_summary(candidate_target),
            }
    observed_pass = int(arm_summaries["fixed_ifold"]["framework_le_threshold_count"])
    gate_passed = observed_pass >= fixed_ifold_min_pass
    payload = {
        "schema_version": SCHEMA,
        "status": "PASS" if gate_passed else "FAIL",
        "exit_code": 0 if gate_passed else 42,
        "scientific_claim_boundary": "THEORETICAL_COMPUTATIONAL_AUDIT_ONLY",
        "gpu_performed": False,
        "sample_binding": "Each row is bound to one (arm, design_i, zero-based sample_index); coords and all metric arrays use that same index.",
        "method": {
            "samples_per_design": SAMPLES_PER_DESIGN,
            "total_tokens": TOTAL_TOKENS,
            "target_token_indices_zero_based": [0, TARGET_TOKEN_COUNT - 1],
            "vhh_token_indices_zero_based": [TARGET_TOKEN_COUNT, TOTAL_TOKENS - 1],
            "cdr_token_indices_zero_based": list(CDR_TOKEN_INDICES),
            "framework_residue_count": VHH_TOKEN_COUNT - len(CDR_TOKEN_INDICES),
            "framework_backbone_atom_count": 364,
            "cdr_backbone_atom_count": 120,
            "alignment": "No-reflection Kabsch fit of input_coords to coords[sample_index].",
        },
        "inputs": resolved_roots,
        "design_counts": dict(design_counts),
        "total_sample_count": len(rows),
        "arm_summaries": arm_summaries,
        "candidate_summaries": candidate_summaries,
        "gate": {
            "arm": "fixed_ifold",
            "metric": "framework_aligned_cdr_rmsd_angstrom",
            "operator": "<=",
            "threshold_angstrom": framework_threshold,
            "minimum_pass_count": fixed_ifold_min_pass,
            "denominator": fixed_samples,
            "observed_pass_count": observed_pass,
            "passed": gate_passed,
            "failure_action": "DO_NOT_START_T12_GPU" if not gate_passed else "GPU_PILOT_MAY_PROCEED_SEPARATELY",
        },
    }
    return payload, rows


def _atomic_write(path: Path, data: bytes) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValidationError(f"refusing to overwrite existing output: {path}") from exc
        temporary.unlink()
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_outputs(
    output_json: Path,
    output_tsv: Path,
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    output_json = output_json.expanduser()
    output_tsv = output_tsv.expanduser()
    if output_json.resolve() == output_tsv.resolve():
        raise ValidationError("JSON and TSV outputs must be different paths")
    for output in (output_json, output_tsv):
        if output.exists() or output.is_symlink():
            raise ValidationError(f"refusing to overwrite existing output: {output}")
    tsv = io.StringIO(newline="")
    writer = csv.DictWriter(tsv, fieldnames=TSV_FIELDS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        serial = dict(row)
        for field in (
            "framework_aligned_cdr_rmsd_angstrom",
            "target_aligned_cdr_rmsd_angstrom",
            *REQUIRED_SAMPLE_METRICS,
        ):
            serial[field] = f"{float(serial[field]):.9f}"
        serial["framework_gate_pass"] = str(bool(serial["framework_gate_pass"])).lower()
        writer.writerow(serial)
    _atomic_write(output_tsv, tsv.getvalue().encode("utf-8"))
    _atomic_write(
        output_json,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def execute(
    arm_roots: Mapping[str, Path],
    output_json: Path,
    output_tsv: Path,
    design_counts: Mapping[str, int] | None = None,
    framework_threshold: float = 4.0,
    fixed_ifold_min_pass: int = 10,
) -> dict[str, Any]:
    payload, rows = audit(
        arm_roots,
        DEFAULT_DESIGN_COUNTS if design_counts is None else design_counts,
        framework_threshold,
        fixed_ifold_min_pass,
    )
    write_outputs(output_json, output_tsv, payload, rows)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    roots = {
        "internal": args.internal_root,
        "high_contact": args.high_contact_root,
        "diverse": args.diverse_root,
        "fixed_ifold": args.fixed_ifold_root,
    }
    counts = {
        "internal": args.internal_design_count,
        "high_contact": args.high_contact_design_count,
        "diverse": args.diverse_design_count,
        "fixed_ifold": args.fixed_ifold_design_count,
    }
    try:
        payload = execute(
            roots,
            args.output_json,
            args.output_tsv,
            counts,
            args.framework_threshold_angstrom,
            args.fixed_ifold_min_pass,
        )
    except ValidationError as exc:
        print(f"T12 CPU audit validation failed: {exc}", file=sys.stderr)
        return 2
    gate = payload["gate"]
    print(
        f"T12 CPU gate {payload['status']}: {gate['observed_pass_count']}/"
        f"{gate['denominator']} <= {gate['threshold_angstrom']} A; "
        f"required {gate['minimum_pass_count']}"
    )
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
