#!/usr/bin/env python3
"""Build a small, sanitized public summary from a sealed T12 GPU attempt.

Input is a local attempt containing the terminal T12 receipt, validator JSON,
and exact output manifest. Output is a new date-suffixed directory inside the
Git repository. The builder verifies the complete private manifest but never
copies candidate sequences, CIF/NPZ payloads, logs, weights, environment data,
absolute private paths, or the full per-file manifest into the public bundle.

This command performs no model inference and does not modify its source attempt.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


RUN_SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_GPU_RUN_V1"
RUN_COMPLETE = "T12_SPLIT_TEMPLATE_COMPLETE"
VALIDATION_SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_VALIDATION_V1"
PUBLIC_RECEIPT_SCHEMA = "WINDOWS_OWNER_T12_GPU_PUBLIC_RECEIPT_V1"
PUBLIC_VALIDATION_SCHEMA = "WINDOWS_OWNER_T12_GPU_PUBLIC_VALIDATION_SUMMARY_V1"
PUBLIC_CONFIG_SCHEMA = "WINDOWS_OWNER_T12_GPU_PUBLIC_CONFIG_V1"
CLAIM_BOUNDARY = "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE"

RECEIPT_RELATIVE = "operator_logs/T12_SPLIT_TEMPLATE_GPU.json"
VALIDATION_RELATIVE = "operator_logs/T12_VALIDATION.json"
MANIFEST_RELATIVE = "operator_logs/OUTPUT_SHA256SUMS"
DIRECTORIES_RELATIVE = "operator_logs/OUTPUT_DIRECTORIES.txt"

CANDIDATE_COUNT = 6
FOLD_SAMPLES_PER_CANDIDATE = 5
FOLD_SAMPLE_COUNT = 30
HARD_TIMEOUT_SECONDS = 5_400
EXPECTED_CANDIDATE_IDS = tuple(f"design_{index}" for index in range(CANDIDATE_COUNT))
EXPECTED_SOURCE_INPUT_NAMES = frozenset(
    f"design_{index}.{suffix}"
    for index in range(CANDIDATE_COUNT)
    for suffix in ("cif", "npz")
)
EXPECTED_RUNTIME_NAMES = frozenset({"boltz2_conf_final.ckpt", "mols.zip"})
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
DATE_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*_20[0-9]{6}")
MANIFEST_ROW = re.compile(r"([0-9a-f]{64})  \./([^\x00\r\n]+)")
AMINO_ACID_SEQUENCE = re.compile(r"(?<![A-Z])[ACDEFGHIKLMNPQRSTVWY]{20,}(?![A-Z])")


class PublicationError(ValueError):
    """Raised when a private attempt is unsafe or not terminally complete."""


@dataclass(frozen=True)
class BoundFile:
    """A regular file bound to its bytes, digest, and filesystem identity."""

    path: Path
    data: bytes
    sha256: str
    identity: tuple[int, ...]


@dataclass(frozen=True)
class ValidatedSource:
    """Validated private evidence reduced to values safe for public rendering."""

    receipt: Mapping[str, Any]
    validation: Mapping[str, Any]
    receipt_sha256: str
    validation_sha256: str
    manifest_sha256: str
    manifest_file_count: int
    source_commit: str
    source_tree: str
    started_at_utc: str
    ended_at_utc: str
    duration_seconds: float
    adapter_contract: Mapping[str, Any]
    resolved_config: Mapping[str, Any]
    bound_identities: Mapping[Path, tuple[int, ...]]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected-source-commit", required=True)
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


def _regular_bound(path: Path) -> BoundFile:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PublicationError(f"required file is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise PublicationError(f"required path is not a regular non-symlink file: {path}")
    if before.st_nlink != 1:
        raise PublicationError(f"required file must have one hard link: {path}")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            chunks.append(chunk)
    after = path.lstat()
    if _identity(before) != _identity(after):
        raise PublicationError(f"file changed while being read: {path}")
    return BoundFile(path, b"".join(chunks), digest.hexdigest(), _identity(after))


def _regular_digest(path: Path) -> tuple[str, tuple[int, ...]]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PublicationError(f"manifest member is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise PublicationError(f"manifest member is not a regular non-symlink file: {path}")
    if before.st_nlink != 1:
        raise PublicationError(f"manifest member must have one hard link: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if _identity(before) != _identity(after):
        raise PublicationError(f"manifest member changed while hashing: {path}")
    return digest.hexdigest(), _identity(after)


def _canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise PublicationError(f"{label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as exc:
        raise PublicationError(f"{label} is unavailable: {exc}") from exc
    if resolved != path or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PublicationError(f"{label} must be a canonical non-symlink directory")
    return path


def _prepare_output_path(repo_root: Path, output: Path) -> Path:
    if not output.is_absolute():
        raise PublicationError("output directory must be absolute")
    if output.exists() or output.is_symlink():
        raise PublicationError(f"refusing to overwrite existing output: {output}")
    if output.resolve(strict=False) != output:
        raise PublicationError("output directory must be canonical")
    parent = _canonical_directory(output.parent, "output parent")
    try:
        output.relative_to(repo_root)
    except ValueError as exc:
        raise PublicationError("output directory must be inside the repository") from exc
    if not DATE_DIRECTORY.fullmatch(output.name):
        raise PublicationError("output directory name must end in a YYYYMMDD date")
    if parent == repo_root:
        raise PublicationError("public bundle must be placed below a repository subdirectory")
    return output


def _canonical_relative(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PublicationError(f"{label} must be a string")
    if value.startswith("./"):
        value = value[2:]
    if not value or "\\" in value or value.startswith("/"):
        raise PublicationError(f"unsafe {label}: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PublicationError(f"noncanonical {label}: {value!r}")
    if pure.as_posix() != value:
        raise PublicationError(f"noncanonical {label}: {value!r}")
    return value


def _parse_manifest(bound: BoundFile) -> dict[str, str]:
    try:
        text = bound.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError("output manifest is not UTF-8") from exc
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = MANIFEST_ROW.fullmatch(line)
        if match is None:
            raise PublicationError(f"invalid output manifest row: {line!r}")
        relative = _canonical_relative(match.group(2), "manifest path")
        if relative in rows:
            raise PublicationError(f"duplicate output manifest path: {relative}")
        rows[relative] = match.group(1)
    if not rows:
        raise PublicationError("output manifest is empty")
    if MANIFEST_RELATIVE in rows:
        raise PublicationError("output manifest must exclude itself")
    return rows


def _verify_manifest_closure(
    attempt: Path,
) -> tuple[BoundFile, dict[str, str], dict[Path, tuple[int, ...]]]:
    manifest = _regular_bound(attempt / MANIFEST_RELATIVE)
    rows = _parse_manifest(manifest)
    required = {RECEIPT_RELATIVE, VALIDATION_RELATIVE, DIRECTORIES_RELATIVE}
    if not required <= set(rows):
        raise PublicationError(f"terminal manifest members missing: {sorted(required - set(rows))}")

    observed: set[str] = set()
    for path in attempt.rglob("*"):
        relative = path.relative_to(attempt).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PublicationError(f"symlink is forbidden in sealed attempt: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise PublicationError(f"special file is forbidden in sealed attempt: {relative}")
        if relative != MANIFEST_RELATIVE:
            observed.add(relative)
    if observed != set(rows):
        raise PublicationError(
            "output manifest closure mismatch: "
            f"missing={sorted(observed - set(rows))} unexpected={sorted(set(rows) - observed)}"
        )

    identities: dict[Path, tuple[int, ...]] = {manifest.path: manifest.identity}
    for relative, expected in rows.items():
        path = attempt / relative
        actual, identity = _regular_digest(path)
        if actual != expected:
            raise PublicationError(f"output manifest digest mismatch: {relative}")
        identities[path] = identity
    return manifest, rows, identities


def _json_object(bound: BoundFile, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(bound.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be a JSON object")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise PublicationError(f"{label} must be a lowercase SHA-256")
    return value


def _git_oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX40.fullmatch(value) is None:
        raise PublicationError(f"{label} must be a lowercase 40-hex Git object ID")
    return value


def _integer(value: Any, expected: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise PublicationError(f"{label} must equal {expected}")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicationError(f"{label} must be a positive integer")
    return value


def _timestamp(value: Any, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PublicationError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PublicationError(f"invalid {label}: {value!r}") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PublicationError(f"{label} must use UTC")
    return value, parsed


def _validate_hash_records(
    value: Any,
    expected_names: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_names:
        raise PublicationError(f"{label} path set is not the frozen contract")
    for name, record in value.items():
        if isinstance(record, str):
            _sha256(record, f"{label}/{name}")
        elif isinstance(record, dict) and set(record) == {"sha256", "size_bytes"}:
            _sha256(record["sha256"], f"{label}/{name}/sha256")
            _positive_integer(record["size_bytes"], f"{label}/{name}/size_bytes")
        else:
            raise PublicationError(
                f"{label}/{name} must be a SHA-256 or sha256/size_bytes record"
            )
    return value


def _token_count(contract: Mapping[str, Any], stem: str) -> int:
    aliases = (stem, f"{stem}_tokens", f"{stem}_token_count")
    present = [name for name in aliases if name in contract]
    if len(present) != 1:
        raise PublicationError(f"token contract needs exactly one {stem} count")
    value = contract[present[0]]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PublicationError(f"token contract {stem} count must be an integer")
    return value


def _validate_adapter_preflight(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PublicationError("adapter_preflight must be an object")
    if value.get("status") != "PASS" or value.get("sample_count") != CANDIDATE_COUNT:
        raise PublicationError("adapter_preflight is not PASS for all six candidates")
    samples = value.get("samples")
    if not isinstance(samples, list) or len(samples) != CANDIDATE_COUNT:
        raise PublicationError("adapter_preflight sample closure is invalid")
    for index, row in enumerate(samples):
        expected_id = EXPECTED_CANDIDATE_IDS[index]
        if not isinstance(row, dict) or row.get("id") != expected_id:
            raise PublicationError("adapter_preflight candidate identity drift")
        if row.get("template_shape") != [2, 151] or row.get("slot_sums") != [30, 91]:
            raise PublicationError("adapter_preflight template shape/count drift")
        if row.get("cdr_visible") != 0:
            raise PublicationError("adapter_preflight exposes CDR template tokens")
    return value


def _validate_resolved_config(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PublicationError("resolved_config_contract must be an object")
    expected_checks = {
        "data_target", "target_templates", "design_mask_templates",
        "expected_target_tokens", "expected_cdr_tokens", "expected_framework_tokens",
        "skip_existing", "diffusion_samples", "sampling_steps", "recycling_steps",
        "batch_size", "devices", "precision", "kernels",
    }
    if not expected_checks <= set(value) or any(value[key] is not True for key in expected_checks):
        raise PublicationError("resolved_config_contract does not pass every frozen check")
    return value


def _validate_execution_contract(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PublicationError("validation resolved_execution_contract must be an object")
    expected = {
        "steps": ["folding"],
        "data_module_target": "owner_split_template_data.SplitTemplateFromGeneratedDataModule",
        "target_templates": True,
        "design_mask_templates": False,
        "expected_target_tokens": 30,
        "expected_cdr_tokens": 30,
        "expected_framework_tokens": 91,
        "expected_total_tokens": 151,
        "diffusion_samples": 5,
        "skip_existing": False,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value or type(value.get(key)) is not type(expected_value):
            raise PublicationError(f"resolved_execution_contract/{key} drift")
    return value


def _verify_source_commit(repo_root: Path, commit: str, tree: str, expected: str) -> None:
    if HEX40.fullmatch(expected) is None:
        raise PublicationError("--expected-source-commit must be lowercase 40-hex")
    if commit != expected:
        raise PublicationError("source commit does not match the operator-provided expectation")
    process = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{commit}^{{tree}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0 or process.stdout.strip() != tree:
        raise PublicationError("source commit/tree binding does not resolve in this repository")


def _validate_semantic_payload(value: Any, manifest_rows: Mapping[str, str]) -> int:
    if not isinstance(value, list) or not value:
        raise PublicationError("validation semantic_payload_files must be a non-empty list")
    seen: set[str] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict) or not {"path", "sha256"} <= set(record):
            raise PublicationError(f"semantic payload record {index} has an invalid shape")
        relative = _canonical_relative(record["path"], "semantic payload path")
        digest = _sha256(record["sha256"], f"semantic payload {relative}")
        if relative in seen or manifest_rows.get(relative) != digest:
            raise PublicationError(f"semantic payload is not uniquely bound by manifest: {relative}")
        seen.add(relative)
    return len(seen)


def _revalidate_identities(identities: Mapping[Path, tuple[int, ...]]) -> None:
    for path, expected in identities.items():
        try:
            current = path.lstat()
        except OSError as exc:
            raise PublicationError(f"source evidence disappeared before publication: {path}") from exc
        if _identity(current) != expected:
            raise PublicationError(f"source evidence changed before publication: {path}")


def validate_source(
    attempt_root: Path,
    repo_root: Path,
    expected_source_commit: str,
) -> ValidatedSource:
    """Validate a sealed T12 attempt and return only publication-safe values."""

    attempt = _canonical_directory(attempt_root, "attempt root")
    repo = _canonical_directory(repo_root, "repository root")
    manifest, manifest_rows, identities = _verify_manifest_closure(attempt)

    receipt_bound = _regular_bound(attempt / RECEIPT_RELATIVE)
    validation_bound = _regular_bound(attempt / VALIDATION_RELATIVE)
    if manifest_rows[RECEIPT_RELATIVE] != receipt_bound.sha256:
        raise PublicationError("terminal receipt is not bound by OUTPUT_SHA256SUMS")
    if manifest_rows[VALIDATION_RELATIVE] != validation_bound.sha256:
        raise PublicationError("validation JSON is not bound by OUTPUT_SHA256SUMS")
    receipt = _json_object(receipt_bound, "T12 receipt")
    validation = _json_object(validation_bound, "T12 validation")

    if receipt.get("schema_version") != RUN_SCHEMA or receipt.get("status") != RUN_COMPLETE:
        raise PublicationError("attempt receipt is not a complete T12 GPU run")
    _integer(receipt.get("exit_code"), 0, "receipt exit_code")
    if receipt.get("authority") != "WINDOWS_CODEX":
        raise PublicationError("receipt authority is not WINDOWS_CODEX")
    scope = receipt.get("scope")
    if (
        not isinstance(scope, str)
        or "T12" not in scope.upper()
        or "EXPLORATORY" not in scope.upper()
    ):
        raise PublicationError("receipt scope does not record the explicit exploratory T12 override")
    if receipt.get("stages_executed") != ["folding"]:
        raise PublicationError("receipt must record folding as the only executed stage")
    _integer(receipt.get("candidate_count"), CANDIDATE_COUNT, "candidate_count")
    _integer(
        receipt.get("fold_samples_per_candidate"),
        FOLD_SAMPLES_PER_CANDIDATE,
        "fold_samples_per_candidate",
    )
    _integer(receipt.get("fold_sample_count"), FOLD_SAMPLE_COUNT, "fold_sample_count")
    candidate_ids = receipt.get("candidate_ids")
    if not isinstance(candidate_ids, list) or tuple(candidate_ids) != EXPECTED_CANDIDATE_IDS:
        raise PublicationError("receipt candidate IDs are not the frozen six-member closure")
    exact_flags = {
        "forbidden_stages_started": False,
        "no_auto_retry": True,
        "retry_count": 0,
        "bindcraft_started": False,
        "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
        "oom_detected": False,
        "hard_timeout_respected": True,
        "timed_out": False,
        "training_performed": False,
    }
    for key, expected in exact_flags.items():
        if receipt.get(key) != expected or type(receipt.get(key)) is not type(expected):
            raise PublicationError(f"receipt/{key} must equal {expected!r}")

    source_before = _validate_hash_records(
        receipt.get("source_input_hashes_before"),
        EXPECTED_SOURCE_INPUT_NAMES,
        "source inputs before",
    )
    source_after = _validate_hash_records(
        receipt.get("source_input_hashes_after"),
        EXPECTED_SOURCE_INPUT_NAMES,
        "source inputs after",
    )
    if source_before != source_after:
        raise PublicationError("source input hashes changed during the attempt")
    if receipt.get("requested_stages") != ["folding"]:
        raise PublicationError("receipt requested_stages must be folding only")
    _integer(
        receipt.get("requested_fold_sample_count"),
        FOLD_SAMPLE_COUNT,
        "requested_fold_sample_count",
    )
    if receipt.get("cpu_gate_preserved") != {
        "status": "FAIL", "pass_count": 7, "denominator": 30,
    }:
        raise PublicationError("historical CPU gate FAIL 7/30 was not preserved")
    if receipt.get("user_authorization") != "EXPLICIT_T12_GPU_OVERRIDE_IN_CURRENT_TASK":
        raise PublicationError("explicit exploratory override evidence is missing")
    if receipt.get("folding_exit_code") != 0 or receipt.get("fatal_log_matches") != []:
        raise PublicationError("folding did not close without fatal signatures")
    if not isinstance(receipt.get("gpu_monitor_rows"), int) or receipt["gpu_monitor_rows"] < 2:
        raise PublicationError("GPU monitor evidence is insufficient")

    copied_before = _validate_hash_records(
        receipt.get("copied_input_hashes_before"),
        EXPECTED_SOURCE_INPUT_NAMES,
        "copied inputs before",
    )
    copied_after = _validate_hash_records(
        receipt.get("copied_input_hashes_after"),
        EXPECTED_SOURCE_INPUT_NAMES,
        "copied inputs after",
    )
    if copied_before != copied_after or copied_before != source_before:
        raise PublicationError("copied/source input hashes changed during the attempt")
    runtime_before = _validate_hash_records(
        receipt.get("runtime_assets_before"),
        EXPECTED_RUNTIME_NAMES,
        "runtime before",
    )
    runtime_after = _validate_hash_records(
        receipt.get("runtime_assets_after"),
        EXPECTED_RUNTIME_NAMES,
        "runtime after",
    )
    if runtime_before != runtime_after:
        raise PublicationError("runtime hashes changed during the attempt")
    for key in (
        "source_t11_receipt_sha256", "adapter_sha256", "validator_sha256",
        "resolved_config_sha256", "local_env_acceptance_sha256",
    ):
        _sha256(receipt.get(key), f"receipt/{key}")

    source_commit = _git_oid(receipt.get("source_commit"), "source_commit")
    source_tree = _git_oid(receipt.get("source_tree"), "source_tree")
    _verify_source_commit(repo, source_commit, source_tree, expected_source_commit)
    started_text, started = _timestamp(receipt.get("started_at_utc"), "started_at_utc")
    ended_text, ended = _timestamp(receipt.get("ended_at_utc"), "ended_at_utc")
    duration = (ended - started).total_seconds()
    if duration < 0 or duration > HARD_TIMEOUT_SECONDS:
        raise PublicationError("attempt duration is negative or exceeds the hard timeout")
    monotonic_duration = receipt.get("total_duration_seconds")
    if (
        isinstance(monotonic_duration, bool)
        or not isinstance(monotonic_duration, (int, float))
        or monotonic_duration < 0
        or monotonic_duration > HARD_TIMEOUT_SECONDS
    ):
        raise PublicationError("receipt total_duration_seconds violates the hard timeout")

    adapter_preflight = _validate_adapter_preflight(receipt.get("adapter_preflight"))
    resolved_checks = _validate_resolved_config(receipt.get("resolved_config_contract"))
    if validation.get("schema_version") != VALIDATION_SCHEMA or validation.get("status") != "PASS":
        raise PublicationError("T12 validation is not PASS")
    if receipt.get("output_validation") != validation:
        raise PublicationError("receipt output_validation differs from terminal validation JSON")
    if validation.get("candidate_ids") != candidate_ids:
        raise PublicationError("receipt and validation candidate identities differ")
    _integer(validation.get("candidate_count"), CANDIDATE_COUNT, "validation candidate_count")
    _integer(
        validation.get("fold_samples_per_candidate"),
        FOLD_SAMPLES_PER_CANDIDATE,
        "validation fold_samples_per_candidate",
    )
    _integer(
        validation.get("observed_fold_sample_count"),
        FOLD_SAMPLE_COUNT,
        "validation observed_fold_sample_count",
    )
    execution_contract = _validate_execution_contract(
        validation.get("resolved_execution_contract")
    )
    source_manifest = validation.get("source_input_manifest")
    if not isinstance(source_manifest, dict):
        raise PublicationError("validation source_input_manifest must be an object")
    if source_manifest.get("before_path") != "operator_logs/SOURCE_INPUTS_BEFORE.json":
        raise PublicationError("source input before-manifest path drift")
    if source_manifest.get("after_path") != "operator_logs/SOURCE_INPUTS_AFTER.json":
        raise PublicationError("source input after-manifest path drift")
    before_sha = _sha256(source_manifest.get("before_sha256"), "source input before-manifest")
    after_sha = _sha256(source_manifest.get("after_sha256"), "source input after-manifest")
    if before_sha != after_sha:
        raise PublicationError("source input before/after manifests changed")
    if manifest_rows.get(source_manifest["before_path"]) != before_sha:
        raise PublicationError("source input before-manifest is not terminally bound")
    if manifest_rows.get(source_manifest["after_path"]) != after_sha:
        raise PublicationError("source input after-manifest is not terminally bound")
    _integer(source_manifest.get("replayed_file_count"), 12, "source manifest replayed_file_count")
    per_candidate = validation.get("per_candidate")
    if not isinstance(per_candidate, dict) or tuple(per_candidate) != EXPECTED_CANDIDATE_IDS:
        raise PublicationError("validation per_candidate closure drift")
    for candidate_id, candidate in per_candidate.items():
        if not isinstance(candidate, dict):
            raise PublicationError(f"validation {candidate_id} evidence is invalid")
        _integer(candidate.get("fold_samples"), 5, f"{candidate_id}/fold_samples")
        _integer(candidate.get("token_count"), 151, f"{candidate_id}/token_count")
        if not isinstance(candidate.get("sample_metric_keys"), list) or not candidate["sample_metric_keys"]:
            raise PublicationError(f"{candidate_id} has no finite sample-metric evidence")
    _validate_semantic_payload(
        validation.get("semantic_payload_files"),
        manifest_rows,
    )
    internal_validation = receipt.get("internal_output_validation")
    if not isinstance(internal_validation, dict) or internal_validation.get("status") != "PASS":
        raise PublicationError("internal output validation is not PASS")
    if internal_validation.get("finite_arrays") is not True:
        raise PublicationError("internal output validation does not prove finite arrays")
    _integer(internal_validation.get("fold_sample_count"), 30, "internal fold_sample_count")
    token_contract = internal_validation.get("token_contract")
    if not isinstance(token_contract, dict):
        raise PublicationError("internal token contract is missing")
    for stem, expected in {"target": 30, "cdr": 30, "framework": 91}.items():
        if _token_count(token_contract, stem) != expected:
            raise PublicationError(f"internal token count drift: {stem}")
    if validation.get("input_hashes_unchanged") is not True:
        raise PublicationError("validation input hashes are not unchanged")
    if validation.get("source_t11_hashes_unchanged") is not True:
        raise PublicationError("validation T11 source hashes are not unchanged")
    if validation.get("runtime_hashes_unchanged") is not True:
        raise PublicationError("validation runtime hashes are not unchanged")
    if validation.get("repository_identity_unchanged") is not True:
        raise PublicationError("validation repository identity changed")
    if validation.get("gpu_compute_processes_after") != 0:
        raise PublicationError("GPU compute process remained after T12")
    if validation.get("oom_detected") is not False:
        raise PublicationError("validation detected OOM")
    if receipt.get("scientific_claim_boundary") != CLAIM_BOUNDARY:
        raise PublicationError("receipt scientific claim boundary is missing or changed")
    if validation.get("scientific_claim_boundary") != CLAIM_BOUNDARY:
        raise PublicationError("validation scientific claim boundary is missing or changed")

    identities[receipt_bound.path] = receipt_bound.identity
    identities[validation_bound.path] = validation_bound.identity
    _revalidate_identities(identities)
    return ValidatedSource(
        receipt=receipt,
        validation=validation,
        receipt_sha256=receipt_bound.sha256,
        validation_sha256=validation_bound.sha256,
        manifest_sha256=manifest.sha256,
        manifest_file_count=len(manifest_rows),
        source_commit=source_commit,
        source_tree=source_tree,
        started_at_utc=started_text,
        ended_at_utc=ended_text,
        duration_seconds=duration,
        adapter_contract={
            "hydra_target": execution_contract["data_module_target"],
            "target_templates": True, "design_mask_templates": False,
            "expected_target_tokens": 30, "expected_cdr_tokens": 30,
            "expected_framework_tokens": 91, "template_slots": 2,
            "cdr_visible_slots": 0, "preflight_sample_count": adapter_preflight["sample_count"],
        },
        resolved_config={
            "diffusion_samples": execution_contract["diffusion_samples"],
            "sampling_steps": 200, "recycling_steps": 3,
            "checks": resolved_checks,
        },
        bound_identities=identities,
    )


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _public_config_bytes(source: ValidatedSource) -> bytes:
    adapter = source.adapter_contract
    resolved = source.resolved_config
    lines = [
        f"schema_version: {_yaml_string(PUBLIC_CONFIG_SCHEMA)}",
        "execution:",
        "  classification: \"EXPLORATORY_T12_GPU_OVERRIDE\"",
        "  stages:",
        "    - \"folding\"",
        f"  candidate_count: {CANDIDATE_COUNT}",
        f"  fold_samples_per_candidate: {FOLD_SAMPLES_PER_CANDIDATE}",
        f"  fold_sample_count: {FOLD_SAMPLE_COUNT}",
        f"  hard_timeout_seconds: {HARD_TIMEOUT_SECONDS}",
        "  auto_retry: false",
        "  bindcraft_fallback: false",
        "split_template:",
        f"  hydra_target: {_yaml_string(str(adapter['hydra_target']))}",
        "  target_templates: true",
        "  design_mask_templates: false",
        "  template_slots: 2",
        "  target_tokens: 30",
        "  cdr_tokens: 30",
        "  framework_tokens: 91",
        "  cdr_visible_slots: 0",
        "folding:",
        f"  diffusion_samples: {resolved['diffusion_samples']}",
        f"  sampling_steps: {resolved['sampling_steps']}",
        f"  recycling_steps: {resolved['recycling_steps']}",
        "  skip_existing: false",
        "publication_exclusions:",
        "  candidate_sequences: true",
        "  structures_and_arrays: true",
        "  original_logs_and_full_manifest: true",
        "  weights_and_environment: true",
        f"scientific_claim_boundary: {_yaml_string(CLAIM_BOUNDARY)}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_public_files(source: ValidatedSource) -> dict[str, bytes]:
    receipt = {
        "schema_version": PUBLIC_RECEIPT_SCHEMA,
        "status": "COMPLETE",
        "publication_kind": "SANITIZED_PUBLIC_SUMMARY",
        "source": {
            "commit": source.source_commit,
            "tree": source.source_tree,
            "private_attempt_receipt_sha256": source.receipt_sha256,
            "private_validation_sha256": source.validation_sha256,
            "private_output_manifest_sha256": source.manifest_sha256,
            "source_t11_receipt_sha256": source.receipt["source_t11_receipt_sha256"],
        },
        "process": {
            "started_at_utc": source.started_at_utc,
            "ended_at_utc": source.ended_at_utc,
            "duration_seconds": source.duration_seconds,
            "classification": "EXPLORATORY_T12_GPU_OVERRIDE",
            "stages_executed": ["folding"],
            "candidate_count": CANDIDATE_COUNT,
            "fold_samples_per_candidate": FOLD_SAMPLES_PER_CANDIDATE,
            "fold_sample_count": FOLD_SAMPLE_COUNT,
            "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
            "oom_detected": False,
            "retry_count": 0,
            "bindcraft_started": False,
            "source_inputs_unchanged": True,
            "runtime_assets_unchanged": True,
        },
        "historical_cpu_gate": {
            "status": "FAIL",
            "observed_pass_count": 7,
            "required_pass_count": 10,
            "denominator": 30,
            "reclassified_as_pass": False,
        },
        "exploratory_override": {
            "explicitly_authorized": True,
            "effect": "ALLOWED_THIS_BOUNDED_EXPLORATORY_T12_GPU_RUN_ONLY",
            "changes_historical_gate_status": False,
        },
        "result": {
            "engineering_status": "COMPLETE",
            "validation_status": "PASS",
            "observed_fold_sample_count": FOLD_SAMPLE_COUNT,
            "finite_coordinates": True,
            "experimental_validation": "NOT_PERFORMED",
        },
        "scientific_claim_boundary": CLAIM_BOUNDARY,
    }
    validation = {
        "schema_version": PUBLIC_VALIDATION_SCHEMA,
        "status": "PASS",
        "candidate_count": CANDIDATE_COUNT,
        "fold_samples_per_candidate": FOLD_SAMPLES_PER_CANDIDATE,
        "observed_fold_sample_count": FOLD_SAMPLE_COUNT,
        "finite_coordinates": True,
        "token_contract": {
            "total_tokens": 151,
            "target_tokens": 30,
            "cdr_tokens": 30,
            "framework_tokens": 91,
            "template_slots": 2,
            "cdr_visible_slots": 0,
        },
        "integrity_checks": {
            "private_output_manifest_replayed": True,
            "private_output_manifest_file_count": source.manifest_file_count,
            "source_inputs_unchanged": True,
            "runtime_assets_unchanged": True,
            "source_commit_tree_bound": True,
            "oom_absent": True,
        },
        "historical_cpu_gate_status": "FAIL_7_OF_30_BELOW_10_OF_30",
        "run_classification": "EXPLICIT_EXPLORATORY_OVERRIDE",
        "scientific_claim_boundary": CLAIM_BOUNDARY,
    }
    readme = f"""# T12 GPU 探索运行公开摘要

本目录是封存 T12 GPU attempt 的小型脱敏公开包。私有输出 manifest 已逐项复核，工程运行与独立输出验证均完成，共有 6 个候选、每个 5 个折叠样本，合计 30 个有限坐标样本；本目录不包含候选序列、CIF/NPZ、原始日志、完整逐文件 manifest、模型权重或环境信息。

历史 CPU 门保持 **FAIL：7/30 < 10/30**。负责人随后作出的明确指令只构成本次有界探索性 GPU 运行的 override，不能把历史门改写为 PASS。本轮仅执行 folding，硬上限 5400 秒，不自动重试，不启动 BindCraft；运行终态记录无 OOM。

这些是现成模型权重的计算推理结果和工程完整性记录，不是实验结合、亲和力、选择性、安全性或成药性结论。未进行湿实验验证。

## 文件

- `T12_PUBLIC_RECEIPT.json`：脱敏过程、终态、失败门历史和探索性 override。
- `T12_VALIDATION_SUMMARY.json`：30 样本、有限数、token/template 合同和完整性检查摘要。
- `T12_PUBLIC_CONFIG.yaml`：可公开的 folding 与 split-template 配置；不含本机环境或权重细节。
- `ARTIFACT_INDEX.csv`：公开文件与私有源证据的内容哈希索引；不展开完整私有 manifest。
- `SHA256SUMS`：本目录上述五个文件的 SHA-256。

源代码提交：`{source.source_commit}`。源 attempt receipt、validation 和完整运行目录仅留在本地封存区，未复制到 Git。
"""
    return {
        "README.md": readme.encode("utf-8"),
        "T12_PUBLIC_RECEIPT.json": _json_bytes(receipt),
        "T12_VALIDATION_SUMMARY.json": _json_bytes(validation),
        "T12_PUBLIC_CONFIG.yaml": _public_config_bytes(source),
    }


def _artifact_index_bytes(
    source: ValidatedSource,
    public_files: Mapping[str, bytes],
) -> bytes:
    output = io.StringIO(newline="")
    fields = (
        "artifact_id",
        "repository_filename",
        "disposition",
        "media_type",
        "record_count",
        "size_bytes",
        "sha256",
        "limitations",
    )
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for filename in sorted(public_files, key=lambda value: value.encode("utf-8")):
        data = public_files[filename]
        writer.writerow(
            {
                "artifact_id": f"public_{Path(filename).stem.lower()}",
                "repository_filename": filename,
                "disposition": "INCLUDED_PUBLIC_SUMMARY",
                "media_type": (
                    "text/markdown"
                    if filename.endswith(".md")
                    else "application/json"
                    if filename.endswith(".json")
                    else "application/yaml"
                ),
                "record_count": 1,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "limitations": "sanitized aggregate only",
            }
        )
    private_rows = (
        ("private_t12_receipt", source.receipt_sha256, "terminal process receipt; not copied"),
        ("private_t12_validation", source.validation_sha256, "terminal validation; not copied"),
        ("private_output_manifest", source.manifest_sha256, "full per-file manifest; not copied"),
        (
            "private_source_t11_receipt",
            str(source.receipt["source_t11_receipt_sha256"]),
            "source T11 receipt; not copied",
        ),
    )
    for artifact_id, digest, limitation in private_rows:
        writer.writerow(
            {
                "artifact_id": artifact_id,
                "repository_filename": "",
                "disposition": "INDEX_ONLY_LOCAL_PRIVATE",
                "media_type": "application/octet-stream",
                "record_count": "",
                "size_bytes": "",
                "sha256": digest,
                "limitations": limitation,
            }
        )
    return output.getvalue().encode("utf-8")


def _privacy_scan(files: Mapping[str, bytes]) -> None:
    forbidden = (
        re.compile(r"/home/[^\s\"'`]+"),
        re.compile(r"[A-Za-z]:\\Users\\[^\s\"'`]+", re.IGNORECASE),
        re.compile(r"(?:ghp_|github_pat_|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"),
        AMINO_ACID_SEQUENCE,
    )
    for filename, data in files.items():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicationError(f"public file is not UTF-8 text: {filename}") from exc
        for pattern in forbidden:
            if pattern.search(text) is not None:
                raise PublicationError(f"sensitive-looking content in public file {filename}")
        if "boltz2_conf_final.ckpt" in text or "mols.zip" in text:
            raise PublicationError(f"private runtime asset leaked into public file {filename}")


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def build_bundle(
    attempt_root: Path,
    repo_root: Path,
    output_dir: Path,
    expected_source_commit: str,
) -> Path:
    """Validate private evidence and create one new six-file public bundle."""

    repo = _canonical_directory(repo_root, "repository root")
    output = _prepare_output_path(repo, output_dir)
    source = validate_source(attempt_root, repo, expected_source_commit)
    files = _render_public_files(source)
    files["ARTIFACT_INDEX.csv"] = _artifact_index_bytes(source, files)
    _privacy_scan(files)
    checksums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  ./{filename}\n"
        for filename, data in sorted(
            files.items(),
            key=lambda item: item[0].encode("utf-8"),
        )
    ).encode("utf-8")
    files["SHA256SUMS"] = checksums
    _privacy_scan(files)

    _revalidate_identities(source.bound_identities)
    output.mkdir(mode=0o755)
    for filename, data in files.items():
        _write_new(output / filename, data)
    os.sync()
    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = build_bundle(
            args.attempt_root,
            args.repo_root,
            args.output_dir,
            args.expected_source_commit,
        )
    except (OSError, PublicationError) as exc:
        print(f"T12 public bundle refused: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
