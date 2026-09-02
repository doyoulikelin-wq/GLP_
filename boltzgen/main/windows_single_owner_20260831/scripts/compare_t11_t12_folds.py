#!/usr/bin/env python3
"""Compare sealed T11 and T12 fold outputs without running a model.

The same six candidate inputs were folded by the T11 default-template path and
the T12 split-template path.  This script validates that the inputs and scoring
reference arrays are identical, recomputes geometric metrics for every fold,
and compares methods at candidate level.  Cross-run sample indices are not
treated as paired random repeats because no shared seed contract was recorded.

The command writes a new private analysis attempt.  It never modifies either
source run and never performs GPU inference, training, filtering, or BindCraft.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import yaml

_AUDIT_PATH = Path(__file__).resolve().with_name("audit_t12_framework_aligned_cdr.py")
_AUDIT_SPEC = importlib.util.spec_from_file_location(
    "post_t12_framework_audit_dependency", _AUDIT_PATH
)
if _AUDIT_SPEC is None or _AUDIT_SPEC.loader is None:
    raise RuntimeError(f"cannot load sibling audit dependency: {_AUDIT_PATH}")
_AUDIT = importlib.util.module_from_spec(_AUDIT_SPEC)
_AUDIT_SPEC.loader.exec_module(_AUDIT)

CDR_TOKEN_INDICES = _AUDIT.CDR_TOKEN_INDICES
REQUIRED_SAMPLE_METRICS = _AUDIT.REQUIRED_SAMPLE_METRICS
SAMPLES_PER_DESIGN = _AUDIT.SAMPLES_PER_DESIGN
TOTAL_TOKENS = _AUDIT.TOTAL_TOKENS
ValidationError = _AUDIT.ValidationError
_arm_root = _AUDIT._arm_root
_candidate_rows = _AUDIT._candidate_rows
_design_files = _AUDIT._design_files
_load_npz = _AUDIT._load_npz
_metric_summary = _AUDIT._metric_summary
_read_regular = _AUDIT._read_regular


SCHEMA = "WINDOWS_OWNER_POST_T12_READONLY_COMPARISON_V1"
RECEIPT_SCHEMA = "WINDOWS_OWNER_POST_T12_READONLY_COMPARISON_RECEIPT_V1"
STATUS = "POST_T12_READONLY_ANALYSIS_COMPLETE"
CLAIM_BOUNDARY = "COMPUTATIONAL_PROXY_COMPARISON_ONLY_NOT_EXPERIMENTAL_EVIDENCE"
METHODS = ("t11_default_template", "t12_split_template")
T11_RUN_SCHEMA = "WINDOWS_OWNER_ONLY_INVERSE_FOLD_RUN_V1"
T11_RUN_STATUS = "ONLY_INVERSE_FOLD_COMPLETE"
T11_VALIDATION_SCHEMA = "WINDOWS_OWNER_ONLY_INVERSE_FOLD_VALIDATION_V1"
T12_RUN_SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_GPU_RUN_V1"
T12_RUN_STATUS = "T12_SPLIT_TEMPLATE_COMPLETE"
T12_VALIDATION_SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_VALIDATION_V1"
HISTORICAL_AUDIT_SCHEMA = "WINDOWS_OWNER_T12_FRAMEWORK_ALIGNED_CDR_AUDIT_V1"
EXPECTED_CANDIDATE_COUNT = 6
FRAMEWORK_THRESHOLD_ANGSTROM = 4.0
TARGET_THRESHOLD_ANGSTROM = 8.0
ATTEMPT_NAME = re.compile(r"attempt_20[0-9]{6}T[0-9]{6}Z")
DESIGN_NAME = re.compile(r"design_[0-9]+")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")

PRIMARY_METRIC = "target_aligned_cdr_rmsd_angstrom"
FRAMEWORK_METRIC = "framework_aligned_cdr_rmsd_angstrom"
EXTRA_SAMPLE_METRICS = (
    "min_design_to_target_pae",
    "min_interaction_pae",
)
METRIC_DIRECTIONS = {
    PRIMARY_METRIC: "lower",
    FRAMEWORK_METRIC: "lower",
    "design_to_target_iptm": "higher",
    "design_ptm": "higher",
    "iptm": "higher",
    "ptm": "higher",
    "min_design_to_target_pae": "lower",
    "min_interaction_pae": "lower",
}
REFERENCE_ARRAYS = (
    "input_coords",
    "atom_to_token",
    "atom_resolved_mask",
    "backbone_mask",
    "token_index",
    "mol_type",
    "res_type",
)

SAMPLE_FIELDS = (
    "method",
    "candidate_id",
    "sample_index",
    PRIMARY_METRIC,
    FRAMEWORK_METRIC,
    "target_le_threshold",
    "framework_le_threshold",
    "framework_le_8_angstrom",
    *tuple(metric for metric in METRIC_DIRECTIONS if metric not in {PRIMARY_METRIC, FRAMEWORK_METRIC}),
    "fold_npz_sha256",
)

CANDIDATE_FIELDS = (
    "method",
    "candidate_id",
    "sample_count",
    "target_le_threshold_count",
    "framework_le_threshold_count",
    "framework_le_8_angstrom_count",
    *tuple(
        f"{metric}_{statistic}"
        for metric in METRIC_DIRECTIONS
        for statistic in ("min", "median", "max")
    ),
)

PAIRED_FIELDS = (
    "candidate_id",
    "metric",
    "direction",
    "t11_median",
    "t12_median",
    "delta_t12_minus_t11",
    "favorable_direction",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--t11-design-root", required=True, type=Path)
    parser.add_argument("--t12-design-root", required=True, type=Path)
    parser.add_argument("--t11-run-receipt", required=True, type=Path)
    parser.add_argument("--t11-validation-json", required=True, type=Path)
    parser.add_argument("--t12-run-receipt", required=True, type=Path)
    parser.add_argument("--t12-validation-json", required=True, type=Path)
    parser.add_argument("--historical-audit-json", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def _sha256_text(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_digest(value: str, label: str) -> str:
    if HEX64.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256")
    return value


def _validate_git_oid(value: str, label: str) -> str:
    if HEX40.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a lowercase 40-hex Git object ID")
    return value


def _load_json_file(path: Path, label: str) -> tuple[Mapping[str, Any], str]:
    data, digest = _read_regular(path)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain a JSON object")
    return value, digest


def _load_yaml_file(path: Path, label: str) -> tuple[Mapping[str, Any], str]:
    data, digest = _read_regular(path)
    try:
        value = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain a YAML mapping")
    return value, digest


def _run_git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise ValidationError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout.strip()


def _git_identity(repo_root: Path) -> tuple[dict[str, Any], dict[Path, str]]:
    repo = _arm_root(repo_root, "repository")
    top = Path(_run_git(repo, "rev-parse", "--show-toplevel"))
    if top != repo:
        raise ValidationError("repo root does not match git top level")
    dirty = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise ValidationError("repository must be clean before comparison")
    commit = _validate_git_oid(_run_git(repo, "rev-parse", "HEAD"), "source commit")
    tree = _validate_git_oid(_run_git(repo, "rev-parse", "HEAD^{tree}"), "source tree")
    if _run_git(repo, "rev-parse", f"{commit}^{{tree}}") != tree:
        raise ValidationError("source commit/tree binding failed")

    evidence: dict[Path, str] = {}
    source_hashes: dict[str, str] = {}
    for role, path in (("comparator", Path(__file__).resolve()), ("audit_dependency", _AUDIT_PATH)):
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError as exc:
            raise ValidationError(f"{role} is outside repository") from exc
        _run_git(repo, "ls-files", "--error-unmatch", "--", relative)
        _, digest = _read_regular(path)
        evidence[path] = digest
        source_hashes[f"{role}_sha256"] = digest
    return {
        "source_commit": commit,
        "source_tree": tree,
        **source_hashes,
        "repository_clean": True,
    }, evidence


def _expected_ids() -> list[str]:
    return [f"design_{index}" for index in range(EXPECTED_CANDIDATE_COUNT)]


def _expect_equal(value: Any, expected: Any, label: str) -> None:
    if value != expected:
        raise ValidationError(f"{label} must equal {expected!r}, got {value!r}")


def _nested(mapping: Mapping[str, Any], keys: Sequence[str], label: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValidationError(f"{label} missing {'.'.join(keys)}")
        value = value[key]
    return value


def _validate_run_evidence(
    t11_design_root: Path,
    t12_design_root: Path,
    t11_receipt_path: Path,
    t11_validation_path: Path,
    t12_receipt_path: Path,
    t12_validation_path: Path,
) -> tuple[dict[str, Any], dict[Path, str]]:
    t11_root = _arm_root(t11_design_root, "T11 design")
    t12_root = _arm_root(t12_design_root, "T12 design")
    t11_attempt = _arm_root(t11_root.parent, "T11 attempt")
    t12_attempt = _arm_root(t12_root.parent, "T12 attempt")
    if t11_root != t11_attempt / "intermediate_designs":
        raise ValidationError("T11 design root must be the attempt intermediate_designs")
    if t12_root != t12_attempt / "intermediate_designs":
        raise ValidationError("T12 design root must be the attempt intermediate_designs")

    expected_paths = {
        "T11 run receipt": t11_attempt / "operator_logs" / "ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json",
        "T11 validation": t11_attempt / "operator_logs" / "output_validation.json",
        "T12 run receipt": t12_attempt / "operator_logs" / "T12_SPLIT_TEMPLATE_GPU.json",
        "T12 validation": t12_attempt / "operator_logs" / "T12_VALIDATION.json",
    }
    provided = {
        "T11 run receipt": t11_receipt_path,
        "T11 validation": t11_validation_path,
        "T12 run receipt": t12_receipt_path,
        "T12 validation": t12_validation_path,
    }
    for label, expected in expected_paths.items():
        if provided[label].resolve(strict=True) != expected:
            raise ValidationError(f"{label} path does not belong to the selected attempt")

    t11_receipt, t11_receipt_sha = _load_json_file(t11_receipt_path, "T11 run receipt")
    t11_validation, t11_validation_sha = _load_json_file(
        t11_validation_path, "T11 validation"
    )
    t12_receipt, t12_receipt_sha = _load_json_file(t12_receipt_path, "T12 run receipt")
    t12_validation, t12_validation_sha = _load_json_file(
        t12_validation_path, "T12 validation"
    )
    expected_ids = _expected_ids()
    checks = (
        (t11_receipt, T11_RUN_SCHEMA, T11_RUN_STATUS, "T11 run"),
        (t11_validation, T11_VALIDATION_SCHEMA, "PASS", "T11 validation"),
        (t12_receipt, T12_RUN_SCHEMA, T12_RUN_STATUS, "T12 run"),
        (t12_validation, T12_VALIDATION_SCHEMA, "PASS", "T12 validation"),
    )
    for document, schema, status, label in checks:
        _expect_equal(document.get("schema_version"), schema, f"{label} schema")
        _expect_equal(document.get("status"), status, f"{label} status")
        _expect_equal(document.get("candidate_ids"), expected_ids, f"{label} candidate IDs")
    _expect_equal(t11_receipt.get("candidate_count"), EXPECTED_CANDIDATE_COUNT, "T11 run candidate count")
    _expect_equal(
        t11_validation.get("observed_sequence_candidates"),
        EXPECTED_CANDIDATE_COUNT,
        "T11 validation candidate count",
    )
    _expect_equal(t12_receipt.get("candidate_count"), EXPECTED_CANDIDATE_COUNT, "T12 run candidate count")
    _expect_equal(t12_validation.get("candidate_count"), EXPECTED_CANDIDATE_COUNT, "T12 validation candidate count")
    _expect_equal(t11_receipt.get("attempt_id"), t11_attempt.name, "T11 attempt ID")
    _expect_equal(t12_receipt.get("attempt_id"), t12_attempt.name, "T12 attempt ID")
    _expect_equal(t11_receipt.get("exit_code"), 0, "T11 exit code")
    _expect_equal(t12_receipt.get("exit_code"), 0, "T12 exit code")
    _expect_equal(t11_receipt.get("fold_samples_per_candidate"), 5, "T11 folds per candidate")
    _expect_equal(t12_receipt.get("fold_samples_per_candidate"), 5, "T12 folds per candidate")
    _expect_equal(t11_receipt.get("fold_sample_count"), 30, "T11 fold count")
    _expect_equal(t12_receipt.get("fold_sample_count"), 30, "T12 fold count")
    _expect_equal(t11_validation.get("fold_samples_per_candidate"), 5, "T11 validation folds")
    _expect_equal(t12_validation.get("fold_samples_per_candidate"), 5, "T12 validation folds")
    _expect_equal(t11_validation.get("observed_fold_sample_count"), 30, "T11 validation sample count")
    _expect_equal(t12_validation.get("observed_fold_sample_count"), 30, "T12 validation sample count")
    _expect_equal(t11_receipt.get("output_validation"), t11_validation, "T11 embedded validation")
    _expect_equal(t12_receipt.get("output_validation"), t12_validation, "T12 embedded validation")
    _expect_equal(t11_receipt.get("design_diffusion_performed"), False, "T11 design diffusion")
    _expect_equal(t12_receipt.get("training_performed"), False, "T12 training")
    _expect_equal(t12_receipt.get("bindcraft_started"), False, "T12 BindCraft")
    _expect_equal(t12_receipt.get("stages_executed"), ["folding"], "T12 stages")
    if Path(str(t12_receipt.get("source_t11_attempt"))).resolve(strict=True) != t11_attempt:
        raise ValidationError("T12 source T11 attempt binding mismatch")
    _expect_equal(
        t12_receipt.get("source_t11_receipt_sha256"),
        t11_receipt_sha,
        "T12 source T11 receipt digest",
    )

    config_paths = {
        "t11_config": t11_attempt / "config" / "folding.yaml",
        "t12_config": t12_attempt / "config" / "folding.yaml",
    }
    t11_config, t11_config_sha = _load_yaml_file(config_paths["t11_config"], "T11 folding config")
    t12_config, t12_config_sha = _load_yaml_file(config_paths["t12_config"], "T12 folding config")
    common_paths = {
        "diffusion_samples": ("diffusion_samples",),
        "sampling_steps": ("sampling_steps",),
        "recycling_steps": ("recycling_steps",),
        "batch_size": ("data", "cfg", "batch_size"),
        "devices": ("trainer", "devices"),
        "precision": ("trainer", "precision"),
        "use_kernels": ("override", "use_kernels"),
        "target_templates": ("data", "target_templates"),
        "skip_existing": ("data", "skip_existing"),
    }
    common: dict[str, Any] = {}
    for label, key_path in common_paths.items():
        left = _nested(t11_config, key_path, "T11 config")
        right = _nested(t12_config, key_path, "T12 config")
        _expect_equal(right, left, f"common folding parameter {label}")
        common[label] = left
    _expect_equal(
        common,
        {
            "diffusion_samples": 5,
            "sampling_steps": 200,
            "recycling_steps": 3,
            "batch_size": 1,
            "devices": 1,
            "precision": "bf16-mixed",
            "use_kernels": True,
            "target_templates": True,
            "skip_existing": False,
        },
        "common folding contract",
    )
    _expect_equal(
        _nested(t11_config, ("data", "_target_"), "T11 config"),
        "boltzgen.task.predict.data_from_generated.FromGeneratedDataModule",
        "T11 data module",
    )
    _expect_equal(
        _nested(t12_config, ("data", "_target_"), "T12 config"),
        "owner_split_template_data.SplitTemplateFromGeneratedDataModule",
        "T12 data module",
    )
    for key, expected in (
        ("design_mask_templates", False),
        ("expected_target_tokens", 30),
        ("expected_cdr_tokens", 30),
        ("expected_framework_tokens", 91),
    ):
        _expect_equal(_nested(t12_config, ("data", key), "T12 config"), expected, f"T12 {key}")

    t11_assets = t11_receipt.get("checkpoint_hashes_and_sizes")
    t12_before = t12_receipt.get("runtime_assets_before")
    t12_after = t12_receipt.get("runtime_assets_after")
    if not isinstance(t11_assets, dict) or not isinstance(t12_before, dict) or not isinstance(t12_after, dict):
        raise ValidationError("runtime asset evidence is missing")
    asset_hashes: dict[str, str] = {}
    for name in ("boltz2_conf_final.ckpt", "mols.zip"):
        t11_hash = _validate_digest(
            str(_nested(t11_assets, (name, "accepted_sha256"), "T11 assets")),
            f"T11 {name}",
        )
        before_hash = _validate_digest(str(t12_before.get(name)), f"T12 before {name}")
        after_hash = _validate_digest(str(t12_after.get(name)), f"T12 after {name}")
        if len({t11_hash, before_hash, after_hash}) != 1:
            raise ValidationError(f"cross-run runtime asset mismatch: {name}")
        asset_hashes[name] = t11_hash

    evidence = {
        t11_receipt_path: t11_receipt_sha,
        t11_validation_path: t11_validation_sha,
        t12_receipt_path: t12_receipt_sha,
        t12_validation_path: t12_validation_sha,
        config_paths["t11_config"]: t11_config_sha,
        config_paths["t12_config"]: t12_config_sha,
    }
    return {
        "status": "PASS",
        "t11_run_receipt_sha256": t11_receipt_sha,
        "t11_validation_sha256": t11_validation_sha,
        "t12_run_receipt_sha256": t12_receipt_sha,
        "t12_validation_sha256": t12_validation_sha,
        "t11_folding_config_sha256": t11_config_sha,
        "t12_folding_config_sha256": t12_config_sha,
        "candidate_count": EXPECTED_CANDIDATE_COUNT,
        "folds_per_candidate_per_method": SAMPLES_PER_DESIGN,
        "common_folding_contract": common,
        "runtime_asset_sha256": asset_hashes,
        "controlled_method_difference": {
            "t11": "default_template_data_module",
            "t12": "split_template_data_module",
        },
    }, evidence


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_output_location(
    output_dir: Path,
    repo_root: Path,
    t11_design_root: Path,
    t12_design_root: Path,
) -> None:
    destination, staging = _prepare_output(output_dir)
    protected = (
        _arm_root(repo_root, "repository"),
        _arm_root(t11_design_root.parent, "T11 attempt"),
        _arm_root(t12_design_root.parent, "T12 attempt"),
    )
    for root in protected:
        if _is_within(destination, root) or _is_within(staging, root):
            raise ValidationError(f"output must not be inside protected source: {root}")


def _prepare_output(path: Path) -> tuple[Path, Path]:
    if not path.is_absolute():
        raise ValidationError("output directory must be absolute")
    if path.exists() or path.is_symlink():
        raise ValidationError(f"refusing to overwrite existing output: {path}")
    if ATTEMPT_NAME.fullmatch(path.name) is None:
        raise ValidationError("output directory must use attempt_YYYYMMDDTHHMMSSZ")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve(strict=True) != parent:
        raise ValidationError("output parent must be a canonical non-symlink directory")
    if path.resolve(strict=False) != path:
        raise ValidationError("output directory must be canonical")
    staging = parent / f".{path.name}.staging-{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        raise ValidationError(f"staging path already exists: {staging}")
    return path, staging


def _format_float(value: Any) -> str:
    return f"{float(value):.9f}"


def _rows_to_tsv(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        serial = dict(row)
        for key, value in tuple(serial.items()):
            if isinstance(value, (float, np.floating)):
                serial[key] = _format_float(value)
            elif isinstance(value, (bool, np.bool_)):
                serial[key] = str(bool(value)).lower()
        writer.writerow(serial)
    return output.getvalue().encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _manifest_bytes(root: Path) -> bytes:
    rows: list[str] = []
    for path in sorted(
        (member for member in root.rglob("*") if member.is_file()),
        key=lambda member: member.relative_to(root).as_posix().encode("utf-8"),
    ):
        relative = path.relative_to(root).as_posix()
        if relative == "SHA256SUMS":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  ./{relative}\n")
    return "".join(rows).encode("utf-8")


def _candidate_id_sort(value: str) -> int:
    return int(value.split("_")[1])


def _load_method(
    method: str,
    root: Path,
    candidate_count: int,
    framework_threshold: float,
    target_threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, np.ndarray]]]:
    canonical = _arm_root(root, method)
    expected_ids = {f"design_{index}" for index in range(candidate_count)}
    observed_cifs = {
        path.stem
        for path in canonical.iterdir()
        if path.suffix == ".cif" and DESIGN_NAME.fullmatch(path.stem)
    }
    if observed_cifs != expected_ids:
        raise ValidationError(
            f"{method} CIF candidate closure mismatch: "
            f"observed={sorted(observed_cifs)} expected={sorted(expected_ids)}"
        )
    rows: list[dict[str, Any]] = []
    files: dict[str, Any] = {}
    arrays: dict[str, dict[str, np.ndarray]] = {}
    for candidate_id, design_path, fold_path in _design_files(
        canonical, candidate_count, method
    ):
        cif_path = canonical / f"{candidate_id}.cif"
        _, cif_digest = _read_regular(cif_path)
        _, design_digest = _read_regular(design_path)
        fold, fold_digest = _load_npz(fold_path)
        for metric in EXTRA_SAMPLE_METRICS:
            if metric not in fold or fold[metric].shape != (SAMPLES_PER_DESIGN,):
                raise ValidationError(
                    f"{method}/{candidate_id} metric {metric} must have shape "
                    f"({SAMPLES_PER_DESIGN},)"
                )
        for key in REFERENCE_ARRAYS:
            if key not in fold:
                raise ValidationError(f"{method}/{candidate_id} missing reference array {key}")
        candidate_rows = _candidate_rows(
            method,
            candidate_id,
            design_path,
            fold_path,
            framework_threshold,
        )
        for row in candidate_rows:
            sample_index = int(row["sample_index"])
            row = dict(row)
            row["method"] = row.pop("arm")
            if row.pop("design_npz_sha256") != design_digest:
                raise ValidationError(
                    f"{method}/{candidate_id} design NPZ changed between reads"
                )
            if row["fold_npz_sha256"] != fold_digest:
                raise ValidationError(
                    f"{method}/{candidate_id} fold NPZ changed between reads"
                )
            row["target_le_threshold"] = (
                float(row[PRIMARY_METRIC]) <= target_threshold
            )
            row["framework_le_threshold"] = bool(row.pop("framework_gate_pass"))
            row["framework_le_8_angstrom"] = float(row[FRAMEWORK_METRIC]) <= 8.0
            for metric in EXTRA_SAMPLE_METRICS:
                row[metric] = float(fold[metric][sample_index])
            rows.append(row)
        files[candidate_id] = {
            "input_cif_sha256": cif_digest,
            "input_npz_sha256": design_digest,
            "fold_npz_sha256": fold_digest,
        }
        arrays[candidate_id] = {key: np.asarray(fold[key]) for key in REFERENCE_ARRAYS}
    return rows, {"root": str(canonical), "files": files}, arrays


def _validate_cross_method_binding(
    bindings: Mapping[str, Mapping[str, Any]],
    arrays: Mapping[str, Mapping[str, Mapping[str, np.ndarray]]],
    candidate_count: int,
) -> dict[str, Any]:
    expected = {f"design_{index}" for index in range(candidate_count)}
    left_files = bindings[METHODS[0]]["files"]
    right_files = bindings[METHODS[1]]["files"]
    if set(left_files) != expected or set(right_files) != expected:
        raise ValidationError("cross-method candidate identity mismatch")
    for candidate_id in sorted(expected, key=_candidate_id_sort):
        for key in ("input_cif_sha256", "input_npz_sha256"):
            if left_files[candidate_id][key] != right_files[candidate_id][key]:
                raise ValidationError(f"{candidate_id} cross-method {key} mismatch")
        for key in REFERENCE_ARRAYS:
            if not np.array_equal(
                arrays[METHODS[0]][candidate_id][key],
                arrays[METHODS[1]][candidate_id][key],
            ):
                raise ValidationError(f"{candidate_id} cross-method reference array {key} mismatch")
    public_binding = {
        candidate_id: {
            key: left_files[candidate_id][key]
            for key in ("input_cif_sha256", "input_npz_sha256")
        }
        for candidate_id in sorted(expected, key=_candidate_id_sort)
    }
    return {
        "candidate_ids_match": True,
        "candidate_input_files_byte_identical": True,
        "reference_arrays_identical": True,
        "candidate_binding_sha256": _sha256_text(public_binding),
    }


def _replay_source_bindings(
    bindings: Mapping[str, Mapping[str, Any]],
    historical_audit_path: Path,
    historical_audit_sha256: str,
    additional_evidence: Mapping[Path, str] | None = None,
) -> dict[str, Any]:
    """Re-read every bound source file before writing an analysis receipt."""
    checked = 0
    for method in METHODS:
        root = _arm_root(Path(str(bindings[method]["root"])), method)
        for candidate_id, expected in bindings[method]["files"].items():
            paths = {
                "input_cif_sha256": root / f"{candidate_id}.cif",
                "input_npz_sha256": root / f"{candidate_id}.npz",
                "fold_npz_sha256": root / "fold_out_npz" / f"{candidate_id}.npz",
            }
            for key, path in paths.items():
                _, observed = _read_regular(path)
                if observed != expected[key]:
                    raise ValidationError(
                        f"source changed during comparison: {method}/{candidate_id}/{key}"
                    )
                checked += 1
    _, observed_audit = _read_regular(historical_audit_path)
    if observed_audit != historical_audit_sha256:
        raise ValidationError("historical audit changed during comparison")
    checked += 1
    for path, expected in (additional_evidence or {}).items():
        _, observed = _read_regular(path)
        if observed != expected:
            raise ValidationError(f"bound evidence changed during comparison: {path.name}")
        checked += 1
    return {
        "status": "PASS",
        "files_replayed": checked,
        "source_inputs_modified": False,
    }


def _method_summary(
    method_rows: Sequence[Mapping[str, Any]],
    candidate_count: int,
) -> dict[str, Any]:
    expected = candidate_count * SAMPLES_PER_DESIGN
    identities = {
        (str(row["candidate_id"]), int(row["sample_index"])) for row in method_rows
    }
    if len(method_rows) != expected or len(identities) != expected:
        raise ValidationError("method sample closure mismatch")
    return {
        "candidate_count": candidate_count,
        "sample_count": expected,
        "metrics": {
            metric: _metric_summary([float(row[metric]) for row in method_rows])
            for metric in METRIC_DIRECTIONS
        },
        "threshold_counts": {
            "target_aligned_cdr_rmsd_le_8_angstrom": sum(
                bool(row["target_le_threshold"]) for row in method_rows
            ),
            "framework_aligned_cdr_rmsd_le_4_angstrom": sum(
                bool(row["framework_le_threshold"]) for row in method_rows
            ),
            "framework_aligned_cdr_rmsd_le_8_angstrom": sum(
                bool(row["framework_le_8_angstrom"]) for row in method_rows
            ),
        },
    }


def _candidate_summaries(
    rows: Sequence[Mapping[str, Any]], candidate_count: int
) -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    flat: list[dict[str, Any]] = []
    nested: dict[str, dict[str, dict[str, Any]]] = {method: {} for method in METHODS}
    for method in METHODS:
        method_rows = [row for row in rows if row["method"] == method]
        for index in range(candidate_count):
            candidate_id = f"design_{index}"
            selected = [row for row in method_rows if row["candidate_id"] == candidate_id]
            if len(selected) != SAMPLES_PER_DESIGN:
                raise ValidationError(f"{method}/{candidate_id} sample closure mismatch")
            metric_summaries = {
                metric: _metric_summary([float(row[metric]) for row in selected])
                for metric in METRIC_DIRECTIONS
            }
            record: dict[str, Any] = {
                "method": method,
                "candidate_id": candidate_id,
                "sample_count": len(selected),
                "target_le_threshold_count": sum(
                    bool(row["target_le_threshold"]) for row in selected
                ),
                "framework_le_threshold_count": sum(
                    bool(row["framework_le_threshold"]) for row in selected
                ),
                "framework_le_8_angstrom_count": sum(
                    bool(row["framework_le_8_angstrom"]) for row in selected
                ),
            }
            for metric, summary in metric_summaries.items():
                for statistic in ("min", "median", "max"):
                    record[f"{metric}_{statistic}"] = summary[statistic]
            flat.append(record)
            nested[method][candidate_id] = {
                "sample_count": len(selected),
                "threshold_counts": {
                    "target_aligned_cdr_rmsd_le_8_angstrom": record[
                        "target_le_threshold_count"
                    ],
                    "framework_aligned_cdr_rmsd_le_4_angstrom": record[
                        "framework_le_threshold_count"
                    ],
                    "framework_aligned_cdr_rmsd_le_8_angstrom": record[
                        "framework_le_8_angstrom_count"
                    ],
                },
                "metrics": metric_summaries,
            }
    return flat, nested


def _paired_comparison(
    nested: Mapping[str, Mapping[str, Mapping[str, Any]]], candidate_count: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for metric, direction in METRIC_DIRECTIONS.items():
        deltas: list[float] = []
        favorable = unchanged = unfavorable = 0
        for index in range(candidate_count):
            candidate_id = f"design_{index}"
            t11 = float(nested[METHODS[0]][candidate_id]["metrics"][metric]["median"])
            t12 = float(nested[METHODS[1]][candidate_id]["metrics"][metric]["median"])
            delta = t12 - t11
            directional = -delta if direction == "lower" else delta
            if math.isclose(directional, 0.0, rel_tol=0.0, abs_tol=1e-12):
                label = "unchanged"
                unchanged += 1
            elif directional > 0:
                label = "improved"
                favorable += 1
            else:
                label = "worsened"
                unfavorable += 1
            deltas.append(delta)
            flat.append(
                {
                    "candidate_id": candidate_id,
                    "metric": metric,
                    "direction": direction,
                    "t11_median": t11,
                    "t12_median": t12,
                    "delta_t12_minus_t11": delta,
                    "favorable_direction": label,
                }
            )
        summary[metric] = {
            "direction": direction,
            "candidate_median_delta_t12_minus_t11": _metric_summary(deltas),
            "candidate_direction_counts": {
                "improved": favorable,
                "unchanged": unchanged,
                "worsened": unfavorable,
            },
            "inference_test_performed": False,
            "inference_test_reason": (
                "Only six candidates and no cross-run seed pairing; candidate-level "
                "effects are descriptive."
            ),
        }
    return flat, summary


def _validate_historical_baseline(
    path: Path,
    t11_root: Path,
    t11_summary: Mapping[str, Any],
    t11_candidates: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    historical, digest = _load_json_file(path, "historical audit JSON")
    _expect_equal(historical.get("schema_version"), HISTORICAL_AUDIT_SCHEMA, "historical schema")
    _expect_equal(historical.get("status"), "FAIL", "historical status")
    _expect_equal(historical.get("exit_code"), 42, "historical exit code")
    _expect_equal(historical.get("gpu_performed"), False, "historical GPU flag")
    _expect_equal(historical.get("total_sample_count"), 155, "historical total sample count")
    gate = historical.get("gate")
    if not isinstance(gate, dict):
        raise ValidationError("historical audit lacks gate")
    expected_gate = {
        "arm": "fixed_ifold",
        "denominator": 30,
        "failure_action": "DO_NOT_START_T12_GPU",
        "metric": FRAMEWORK_METRIC,
        "minimum_pass_count": 10,
        "observed_pass_count": 7,
        "operator": "<=",
        "passed": False,
        "threshold_angstrom": FRAMEWORK_THRESHOLD_ANGSTROM,
    }
    _expect_equal(gate, expected_gate, "historical CPU gate")
    historical_input = historical.get("inputs", {}).get("fixed_ifold")
    if not isinstance(historical_input, str) or Path(historical_input).resolve(strict=True) != t11_root:
        raise ValidationError("historical fixed_ifold input binding mismatch")
    fixed = historical.get("arm_summaries", {}).get("fixed_ifold")
    if not isinstance(fixed, dict):
        raise ValidationError("historical audit lacks fixed_ifold summary")
    checks: dict[str, bool] = {
        "design_count": int(fixed.get("design_count", -1)) == EXPECTED_CANDIDATE_COUNT,
        "sample_count": int(t11_summary["sample_count"]) == int(fixed.get("sample_count", -1)),
        "framework_le_4_count": int(
            t11_summary["threshold_counts"]["framework_aligned_cdr_rmsd_le_4_angstrom"]
        ) == int(fixed.get("framework_le_threshold_count", -1)),
        "framework_le_8_count": int(
            t11_summary["threshold_counts"]["framework_aligned_cdr_rmsd_le_8_angstrom"]
        ) == int(fixed.get("framework_le_8_angstrom_count", -1)),
        "gate_count_matches_summary": int(fixed.get("framework_le_threshold_count", -1))
        == int(gate["observed_pass_count"]),
    }
    for metric in (PRIMARY_METRIC, FRAMEWORK_METRIC):
        for statistic in ("min", "mean", "median", "max"):
            checks[f"{metric}_{statistic}"] = math.isclose(
                float(t11_summary["metrics"][metric][statistic]),
                float(fixed.get(metric, {}).get(statistic, math.nan)),
                rel_tol=0.0,
                abs_tol=1e-8,
            )
    historical_candidates = historical.get("candidate_summaries", {}).get("fixed_ifold")
    if not isinstance(historical_candidates, dict) or set(historical_candidates) != set(t11_candidates):
        raise ValidationError("historical fixed_ifold candidate closure mismatch")
    for candidate_id, current in t11_candidates.items():
        previous = historical_candidates[candidate_id]
        if not isinstance(previous, dict):
            raise ValidationError(f"historical candidate summary is invalid: {candidate_id}")
        checks[f"{candidate_id}_sample_count"] = int(current["sample_count"]) == int(
            previous.get("sample_count", -1)
        )
        checks[f"{candidate_id}_framework_count"] = int(
            current["threshold_counts"]["framework_aligned_cdr_rmsd_le_4_angstrom"]
        ) == int(previous.get("framework_le_threshold_count", -1))
        for metric in (PRIMARY_METRIC, FRAMEWORK_METRIC):
            for statistic in ("min", "mean", "median", "max"):
                checks[f"{candidate_id}_{metric}_{statistic}"] = math.isclose(
                    float(current["metrics"][metric][statistic]),
                    float(previous.get(metric, {}).get(statistic, math.nan)),
                    rel_tol=0.0,
                    abs_tol=1e-8,
                )
    if not all(checks.values()):
        failed = sorted(key for key, passed in checks.items() if not passed)
        raise ValidationError(f"T11 historical baseline reproduction failed: {failed}")
    return {
        "status": "PASS",
        "check_count": len(checks),
        "historical_audit_sha256": digest,
    }


def compare(
    t11_root: Path,
    t12_root: Path,
    historical_audit_json: Path,
    candidate_count: int = 6,
    framework_threshold: float = 4.0,
    target_threshold: float = 8.0,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if candidate_count != EXPECTED_CANDIDATE_COUNT:
        raise ValidationError(f"candidate count is fixed at {EXPECTED_CANDIDATE_COUNT}")
    if framework_threshold != FRAMEWORK_THRESHOLD_ANGSTROM:
        raise ValidationError(f"framework threshold is fixed at {FRAMEWORK_THRESHOLD_ANGSTROM}")
    if target_threshold != TARGET_THRESHOLD_ANGSTROM:
        raise ValidationError(f"target threshold is fixed at {TARGET_THRESHOLD_ANGSTROM}")
    if t11_root.resolve(strict=True) == t12_root.resolve(strict=True):
        raise ValidationError("T11 and T12 roots must be different")
    _, audit_dependency_sha256 = _read_regular(_AUDIT_PATH)

    all_rows: list[dict[str, Any]] = []
    bindings: dict[str, Mapping[str, Any]] = {}
    reference_arrays: dict[str, Mapping[str, Mapping[str, np.ndarray]]] = {}
    for method, root in zip(METHODS, (t11_root, t12_root)):
        method_rows, method_bindings, arrays = _load_method(
            method,
            root,
            candidate_count,
            framework_threshold,
            target_threshold,
        )
        all_rows.extend(method_rows)
        bindings[method] = method_bindings
        reference_arrays[method] = arrays

    expected_rows = len(METHODS) * candidate_count * SAMPLES_PER_DESIGN
    identities = {
        (str(row["method"]), str(row["candidate_id"]), int(row["sample_index"]))
        for row in all_rows
    }
    if len(all_rows) != expected_rows or len(identities) != expected_rows:
        raise ValidationError("full comparison sample closure mismatch")

    input_contract = _validate_cross_method_binding(
        bindings, reference_arrays, candidate_count
    )
    method_summaries = {
        method: _method_summary(
            [row for row in all_rows if row["method"] == method], candidate_count
        )
        for method in METHODS
    }
    candidate_rows, nested = _candidate_summaries(all_rows, candidate_count)
    paired_rows, paired = _paired_comparison(nested, candidate_count)
    baseline = _validate_historical_baseline(
        historical_audit_json,
        t11_root,
        method_summaries[METHODS[0]],
        nested[METHODS[0]],
    )

    primary_directions = paired[PRIMARY_METRIC]["candidate_direction_counts"]
    framework_directions = paired[FRAMEWORK_METRIC]["candidate_direction_counts"]

    payload = {
        "schema_version": SCHEMA,
        "status": "ANALYSIS_COMPLETE",
        "classification": "POSTHOC_READ_ONLY_DESCRIPTIVE_COMPARISON",
        "scientific_claim_boundary": CLAIM_BOUNDARY,
        "gpu_performed": False,
        "training_performed": False,
        "bindcraft_performed": False,
        "wet_lab_performed": False,
        "sample_grain": {
            "within_method": "(method, candidate_id, zero-based sample_index)",
            "cross_method_pairing_unit": "candidate_id after five-fold within-candidate summary",
            "sample_index_paired_across_methods": False,
            "reason": "No cross-run random-seed binding was recorded.",
        },
        "thresholds_are_descriptive_not_new_gates": True,
        "thresholds_angstrom": {
            "framework_aligned_cdr": framework_threshold,
            "target_aligned_cdr": target_threshold,
        },
        "inputs": bindings,
        "input_contract": input_contract,
        "sample_count": len(all_rows),
        "method_summaries": method_summaries,
        "candidate_summaries": nested,
        "paired_candidate_comparisons": paired,
        "historical_t11_baseline_reproduction": baseline,
        "audit_dependency_sha256": audit_dependency_sha256,
        "interpretation": {
            "analysis_classification": "EXPLORATORY_DESCRIPTIVE",
            "decision_rule_predeclared": False,
            "overall_comparison_outcome": "INCONCLUSIVE",
            "primary_target_pose_observation": {
                "t11_le_8_angstrom_count": method_summaries[METHODS[0]]["threshold_counts"][
                    "target_aligned_cdr_rmsd_le_8_angstrom"
                ],
                "t12_le_8_angstrom_count": method_summaries[METHODS[1]]["threshold_counts"][
                    "target_aligned_cdr_rmsd_le_8_angstrom"
                ],
                "candidate_direction_counts": primary_directions,
            },
            "secondary_framework_observation": {
                "t11_le_4_angstrom_count": method_summaries[METHODS[0]]["threshold_counts"][
                    "framework_aligned_cdr_rmsd_le_4_angstrom"
                ],
                "t12_le_4_angstrom_count": method_summaries[METHODS[1]]["threshold_counts"][
                    "framework_aligned_cdr_rmsd_le_4_angstrom"
                ],
                "candidate_direction_counts": framework_directions,
            },
            "historical_cpu_gate_reclassified": False,
            "escalation_supported_by_this_analysis": False,
            "causal_effect_claimed": False,
            "experimental_binding_claimed": False,
        },
    }
    payload["source_read_replay"] = _replay_source_bindings(
        bindings,
        historical_audit_json,
        baseline["historical_audit_sha256"],
        {_AUDIT_PATH: audit_dependency_sha256},
    )
    return payload, all_rows, candidate_rows, paired_rows


def write_attempt(
    output_dir: Path,
    payload: Mapping[str, Any],
    sample_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    paired_rows: Sequence[Mapping[str, Any]],
    *,
    source_commit: str,
    source_tree: str,
    t11_receipt_sha256: str,
    t12_receipt_sha256: str,
    started_at_utc: str,
    analysis_duration_seconds: float,
    command: Sequence[str],
) -> Path:
    destination, staging = _prepare_output(output_dir)
    source_commit = _validate_git_oid(source_commit, "source commit")
    source_tree = _validate_git_oid(source_tree, "source tree")
    t11_receipt_sha256 = _validate_digest(t11_receipt_sha256, "T11 receipt")
    t12_receipt_sha256 = _validate_digest(t12_receipt_sha256, "T12 receipt")
    try:
        staging.mkdir(mode=0o700)
        reports = staging / "reports"
        logs = staging / "operator_logs"
        reports.mkdir()
        logs.mkdir()

        comparison_path = reports / "POST_T12_COMPARISON.json"
        sample_path = reports / "POST_T12_SAMPLE_METRICS.tsv"
        candidate_path = reports / "POST_T12_CANDIDATE_SUMMARY.tsv"
        paired_path = reports / "POST_T12_PAIRED_CANDIDATE_DELTAS.tsv"
        _write_bytes(
            comparison_path,
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        _write_bytes(sample_path, _rows_to_tsv(sample_rows, SAMPLE_FIELDS))
        _write_bytes(candidate_path, _rows_to_tsv(candidate_rows, CANDIDATE_FIELDS))
        _write_bytes(paired_path, _rows_to_tsv(paired_rows, PAIRED_FIELDS))

        output_hashes = {
            path.relative_to(staging).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (comparison_path, sample_path, candidate_path, paired_path)
        }
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "status": STATUS,
            "classification": "CPU_ONLY_READ_ONLY_ANALYSIS",
            "started_at_utc": started_at_utc,
            "ended_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "analysis_duration_seconds": analysis_duration_seconds,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "comparator_sha256": payload["code_provenance"]["comparator_sha256"],
            "audit_dependency_sha256": payload["code_provenance"][
                "audit_dependency_sha256"
            ],
            "t11_receipt_sha256": t11_receipt_sha256,
            "t12_receipt_sha256": t12_receipt_sha256,
            "historical_audit_sha256": payload["historical_t11_baseline_reproduction"][
                "historical_audit_sha256"
            ],
            "candidate_count": payload["method_summaries"][METHODS[0]]["candidate_count"],
            "samples_per_candidate": SAMPLES_PER_DESIGN,
            "total_sample_rows": payload["sample_count"],
            "input_contract": payload["input_contract"],
            "output_sha256": output_hashes,
            "gpu_performed": False,
            "training_performed": False,
            "bindcraft_performed": False,
            "wet_lab_performed": False,
            "source_read_replay": payload["source_read_replay"],
            "source_inputs_modified": payload["source_read_replay"][
                "source_inputs_modified"
            ],
            "scientific_claim_boundary": CLAIM_BOUNDARY,
        }
        _write_bytes(
            logs / "COMPARISON_RECEIPT.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        _write_bytes(
            logs / "argv.json",
            (json.dumps(list(command), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        _write_bytes(staging / "STATUS.txt", (STATUS + "\n").encode("utf-8"))
        _write_bytes(staging / "SHA256SUMS", _manifest_bytes(staging))
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    clock = time.monotonic()
    try:
        _validate_output_location(
            args.output_dir,
            args.repo_root,
            args.t11_design_root,
            args.t12_design_root,
        )
        code_provenance, code_evidence = _git_identity(args.repo_root)
        historical = args.historical_audit_json.resolve(strict=True)
        try:
            historical_relative = historical.relative_to(args.repo_root).as_posix()
        except ValueError as exc:
            raise ValidationError("historical audit must be inside repository") from exc
        _run_git(args.repo_root, "ls-files", "--error-unmatch", "--", historical_relative)
        run_contract, run_evidence = _validate_run_evidence(
            args.t11_design_root,
            args.t12_design_root,
            args.t11_run_receipt,
            args.t11_validation_json,
            args.t12_run_receipt,
            args.t12_validation_json,
        )
        payload, samples, candidates, paired = compare(
            args.t11_design_root,
            args.t12_design_root,
            args.historical_audit_json,
        )
        payload["run_evidence"] = run_contract
        payload["code_provenance"] = code_provenance
        replay_evidence = {**code_evidence, **run_evidence}
        payload["source_read_replay"] = _replay_source_bindings(
            payload["inputs"],
            args.historical_audit_json,
            payload["historical_t11_baseline_reproduction"]["historical_audit_sha256"],
            replay_evidence,
        )
        analysis_duration = time.monotonic() - clock
        output = write_attempt(
            args.output_dir,
            payload,
            samples,
            candidates,
            paired,
            source_commit=code_provenance["source_commit"],
            source_tree=code_provenance["source_tree"],
            t11_receipt_sha256=run_contract["t11_run_receipt_sha256"],
            t12_receipt_sha256=run_contract["t12_run_receipt_sha256"],
            started_at_utc=started,
            analysis_duration_seconds=analysis_duration,
            command=[sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
        )
    except (OSError, ValidationError, ValueError) as exc:
        print(f"post-T12 comparison failed: {exc}", file=sys.stderr)
        return 2
    t11 = payload["method_summaries"][METHODS[0]]
    t12 = payload["method_summaries"][METHODS[1]]
    print(
        "post-T12 read-only analysis complete: "
        f"target<=8A {t11['threshold_counts']['target_aligned_cdr_rmsd_le_8_angstrom']}"
        f"/{t11['sample_count']} vs "
        f"{t12['threshold_counts']['target_aligned_cdr_rmsd_le_8_angstrom']}"
        f"/{t12['sample_count']}; "
        f"output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
