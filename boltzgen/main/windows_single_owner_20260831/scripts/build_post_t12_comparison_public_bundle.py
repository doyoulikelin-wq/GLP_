#!/usr/bin/env python3
"""Build a sanitized public bundle from a sealed post-T12 comparison attempt.

The private attempt is verified as a closed SHA-256 manifest.  Public files are
rendered from an explicit aggregate whitelist; candidate identities, per-sample
rows, structures, sequences, logs, commands, model assets, and local paths are
never copied.  This command performs no model inference.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import statistics
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


COMPARISON_SCHEMA = "WINDOWS_OWNER_POST_T12_READONLY_COMPARISON_V1"
COMPARISON_RECEIPT_SCHEMA = "WINDOWS_OWNER_POST_T12_READONLY_COMPARISON_RECEIPT_V1"
COMPARISON_STATUS = "POST_T12_READONLY_ANALYSIS_COMPLETE"
PUBLIC_RECEIPT_SCHEMA = "WINDOWS_OWNER_POST_T12_PUBLIC_RECEIPT_V1"
PUBLIC_SUMMARY_SCHEMA = "WINDOWS_OWNER_POST_T12_PUBLIC_SUMMARY_V1"
PUBLIC_METHOD_SCHEMA = "WINDOWS_OWNER_POST_T12_PUBLIC_METHOD_V1"
CLAIM_BOUNDARY = "COMPUTATIONAL_PROXY_COMPARISON_ONLY_NOT_EXPERIMENTAL_EVIDENCE"
METHODS = ("t11_default_template", "t12_split_template")
PRIMARY_METRIC = "target_aligned_cdr_rmsd_angstrom"
FRAMEWORK_METRIC = "framework_aligned_cdr_rmsd_angstrom"
EXPECTED_CANDIDATES = tuple(f"design_{index}" for index in range(6))
EXPECTED_MEMBERS = frozenset(
    {
        "STATUS.txt",
        "operator_logs/COMPARISON_RECEIPT.json",
        "operator_logs/argv.json",
        "reports/POST_T12_COMPARISON.json",
        "reports/POST_T12_SAMPLE_METRICS.tsv",
        "reports/POST_T12_CANDIDATE_SUMMARY.tsv",
        "reports/POST_T12_PAIRED_CANDIDATE_DELTAS.tsv",
    }
)
PUBLIC_FILES = (
    "README.md",
    "POST_T12_COMPARISON_PUBLIC_RECEIPT.json",
    "POST_T12_COMPARISON_SUMMARY.json",
    "POST_T12_COMPARISON_METHOD.json",
    "ARTIFACT_INDEX.csv",
)
DATE_DIRECTORY = re.compile(r"post_t12_readonly_comparison_public_20[0-9]{6}")
MANIFEST_ROW = re.compile(r"([0-9a-f]{64})  \./([^\x00\r\n]+)")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
AMINO_ACID_SEQUENCE = re.compile(r"(?<![A-Z])[ACDEFGHIKLMNPQRSTVWY]{20,}(?![A-Z])")
SENSITIVE_PATTERNS = (
    (re.compile(r"/(?:home|root|Users|mnt|tmp|opt)/"), "absolute POSIX path"),
    (re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]"), "absolute Windows path"),
    (re.compile(r"(?i)\\\\(?:wsl(?:\.localhost|\$)?|[^\\\s]+)\\"), "UNC path"),
    (re.compile(r"(?i)(?:file|workspace)://"), "local URI"),
    (re.compile(r"attempt_20[0-9]{6}T[0-9]{6}Z"), "private attempt ID"),
    (re.compile(r"design_[0-9]+"), "candidate identifier"),
    (re.compile(r"sample_index"), "sample identifier field"),
    (re.compile(r"(?i)(?:ghp_|github_pat_|sk-[A-Za-z0-9_-]{16,})"), "credential token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)(?:boltz2_conf_final\.ckpt|mols\.zip)"), "private runtime asset"),
    (re.compile(r"(?i)\b[^\s]+\.(?:cif|mmcif|pdb|npz|npy|pt|ckpt|safetensors)\b"), "private data filename"),
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
    "design_to_target_iptm",
    "design_ptm",
    "iptm",
    "ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
    "fold_npz_sha256",
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


class PublicationError(ValueError):
    """Raised when private evidence or public output violates the contract."""


@dataclass(frozen=True)
class BoundFile:
    path: Path
    data: bytes
    sha256: str
    identity: tuple[int, ...]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-attempt", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
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


def _bound(path: Path) -> BoundFile:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PublicationError(f"required file is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise PublicationError(f"required file is not regular/non-symlink: {path}")
    if before.st_nlink != 1:
        raise PublicationError(f"required file has multiple hard links: {path}")
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


def _canonical_relative(value: str) -> str:
    if value.startswith("./"):
        value = value[2:]
    pure = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise PublicationError(f"unsafe manifest path: {value!r}")
    return value


def _parse_manifest(bound: BoundFile) -> dict[str, str]:
    try:
        text = bound.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError("private manifest is not UTF-8") from exc
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = MANIFEST_ROW.fullmatch(line)
        if match is None:
            raise PublicationError(f"invalid private manifest row: {line!r}")
        relative = _canonical_relative(match.group(2))
        if relative in rows:
            raise PublicationError(f"duplicate private manifest member: {relative}")
        rows[relative] = match.group(1)
    if set(rows) != EXPECTED_MEMBERS:
        raise PublicationError(
            "private manifest closure mismatch: "
            f"missing={sorted(EXPECTED_MEMBERS - set(rows))} "
            f"unexpected={sorted(set(rows) - EXPECTED_MEMBERS)}"
        )
    return rows


def _verify_private_attempt(
    attempt: Path,
) -> tuple[BoundFile, dict[str, BoundFile], dict[Path, tuple[int, ...]]]:
    root = _canonical_directory(attempt, "comparison attempt")
    manifest = _bound(root / "SHA256SUMS")
    expected = _parse_manifest(manifest)
    observed: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PublicationError(f"symlink in private attempt: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise PublicationError(f"special file in private attempt: {relative}")
        if relative != "SHA256SUMS":
            observed.add(relative)
    if observed != set(expected):
        raise PublicationError("filesystem/private manifest closure mismatch")
    files: dict[str, BoundFile] = {}
    identities = {manifest.path: manifest.identity}
    for relative, digest in expected.items():
        member = _bound(root / relative)
        if member.sha256 != digest:
            raise PublicationError(f"private manifest digest mismatch: {relative}")
        files[relative] = member
        identities[member.path] = member.identity
    return manifest, files, identities


def _json_object(bound: BoundFile, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(bound.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be a JSON object")
    return value


def _expect(value: Any, expected: Any, label: str) -> None:
    if value != expected:
        raise PublicationError(f"{label} must equal {expected!r}, got {value!r}")


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise PublicationError(f"{label} must be a lowercase SHA-256")
    return value


def _oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX40.fullmatch(value) is None:
        raise PublicationError(f"{label} must be a lowercase Git object ID")
    return value


def _tsv(bound: BoundFile, expected_fields: Sequence[str], label: str) -> list[dict[str, str]]:
    try:
        text = bound.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError(f"{label} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if tuple(reader.fieldnames or ()) != tuple(expected_fields):
        raise PublicationError(f"{label} header mismatch")
    rows = list(reader)
    if any(None in row for row in rows):
        raise PublicationError(f"{label} has extra columns")
    return rows


def _finite(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise PublicationError(f"{label} is not numeric") from exc
    if not math.isfinite(parsed):
        raise PublicationError(f"{label} is not finite")
    return parsed


def _direction_counts(
    paired_rows: Sequence[Mapping[str, str]], metric: str
) -> dict[str, int]:
    selected = [row for row in paired_rows if row["metric"] == metric]
    if {row["candidate_id"] for row in selected} != set(EXPECTED_CANDIDATES):
        raise PublicationError(f"paired candidate closure mismatch for {metric}")
    counts = {"improved": 0, "unchanged": 0, "worsened": 0}
    for row in selected:
        label = row["favorable_direction"]
        if label not in counts:
            raise PublicationError(f"invalid paired direction for {metric}: {label}")
        counts[label] += 1
        for key in ("t11_median", "t12_median", "delta_t12_minus_t11"):
            _finite(row[key], f"{metric}/{key}")
    return counts


def _validate_private_source(
    attempt: Path,
) -> tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any], dict[Path, tuple[int, ...]]]:
    manifest, files, identities = _verify_private_attempt(attempt)
    status = files["STATUS.txt"].data.decode("utf-8", errors="strict").strip()
    _expect(status, COMPARISON_STATUS, "private terminal status")
    receipt = _json_object(files["operator_logs/COMPARISON_RECEIPT.json"], "comparison receipt")
    comparison = _json_object(files["reports/POST_T12_COMPARISON.json"], "comparison report")
    _expect(receipt.get("schema_version"), COMPARISON_RECEIPT_SCHEMA, "receipt schema")
    _expect(receipt.get("status"), COMPARISON_STATUS, "receipt status")
    _expect(comparison.get("schema_version"), COMPARISON_SCHEMA, "comparison schema")
    _expect(comparison.get("status"), "ANALYSIS_COMPLETE", "comparison status")
    for document, label in ((receipt, "receipt"), (comparison, "comparison")):
        for key in ("gpu_performed", "training_performed", "bindcraft_performed", "wet_lab_performed"):
            _expect(document.get(key), False, f"{label} {key}")
        _expect(document.get("scientific_claim_boundary"), CLAIM_BOUNDARY, f"{label} claim boundary")
    _expect(receipt.get("candidate_count"), 6, "receipt candidate count")
    _expect(receipt.get("samples_per_candidate"), 5, "receipt folds per candidate")
    _expect(receipt.get("total_sample_rows"), 60, "receipt sample rows")
    _expect(comparison.get("sample_count"), 60, "comparison sample rows")
    replay = receipt.get("source_read_replay")
    if not isinstance(replay, dict):
        raise PublicationError("receipt source replay is missing")
    _expect(replay.get("status"), "PASS", "source replay status")
    _expect(replay.get("source_inputs_modified"), False, "source modification flag")
    if not isinstance(replay.get("files_replayed"), int) or replay["files_replayed"] < 38:
        raise PublicationError("source replay count is incomplete")
    _expect(receipt.get("source_inputs_modified"), False, "receipt source modification flag")
    _expect(comparison.get("input_contract", {}).get("candidate_ids_match"), True, "candidate identity contract")
    _expect(
        comparison.get("input_contract", {}).get("candidate_input_files_byte_identical"),
        True,
        "input byte identity contract",
    )
    _expect(
        comparison.get("input_contract", {}).get("reference_arrays_identical"),
        True,
        "reference array identity contract",
    )
    _expect(comparison.get("historical_t11_baseline_reproduction", {}).get("status"), "PASS", "historical baseline")
    _expect(comparison.get("run_evidence", {}).get("status"), "PASS", "run evidence")
    _expect(comparison.get("code_provenance", {}).get("source_commit"), receipt.get("source_commit"), "source commit binding")
    _expect(comparison.get("code_provenance", {}).get("source_tree"), receipt.get("source_tree"), "source tree binding")
    _expect(comparison.get("sample_grain", {}).get("sample_index_paired_across_methods"), False, "sample pairing rule")
    _expect(comparison.get("interpretation", {}).get("decision_rule_predeclared"), False, "decision rule classification")
    _expect(comparison.get("interpretation", {}).get("overall_comparison_outcome"), "INCONCLUSIVE", "comparison outcome")
    _expect(comparison.get("interpretation", {}).get("historical_cpu_gate_reclassified"), False, "historical gate preservation")

    output_hashes = receipt.get("output_sha256")
    if not isinstance(output_hashes, dict):
        raise PublicationError("receipt output hashes are missing")
    expected_reports = {relative for relative in EXPECTED_MEMBERS if relative.startswith("reports/")}
    if set(output_hashes) != expected_reports:
        raise PublicationError("receipt report hash closure mismatch")
    for relative in expected_reports:
        _expect(_sha(output_hashes[relative], f"output hash {relative}"), files[relative].sha256, f"bound output hash {relative}")

    sample_rows = _tsv(files["reports/POST_T12_SAMPLE_METRICS.tsv"], SAMPLE_FIELDS, "sample metrics")
    if len(sample_rows) != 60:
        raise PublicationError("sample table must contain 60 rows")
    identities_seen: set[tuple[str, str, int]] = set()
    method_values: dict[str, dict[str, list[float]]] = {
        method: {PRIMARY_METRIC: [], FRAMEWORK_METRIC: []} for method in METHODS
    }
    threshold_counts = {
        method: {"target": 0, "framework": 0} for method in METHODS
    }
    for row in sample_rows:
        method = row["method"]
        candidate = row["candidate_id"]
        if method not in METHODS or candidate not in EXPECTED_CANDIDATES:
            raise PublicationError("unknown sample method/candidate")
        try:
            sample_index = int(row["sample_index"])
        except ValueError as exc:
            raise PublicationError("sample index is not an integer") from exc
        if sample_index not in range(5):
            raise PublicationError("sample index is outside 0..4")
        identity = (method, candidate, sample_index)
        if identity in identities_seen:
            raise PublicationError("duplicate sample identity")
        identities_seen.add(identity)
        method_values[method][PRIMARY_METRIC].append(_finite(row[PRIMARY_METRIC], "primary metric"))
        method_values[method][FRAMEWORK_METRIC].append(_finite(row[FRAMEWORK_METRIC], "framework metric"))
        for key, short in (("target_le_threshold", "target"), ("framework_le_threshold", "framework")):
            if row[key] not in {"true", "false"}:
                raise PublicationError(f"invalid boolean value in {key}")
            threshold_counts[method][short] += row[key] == "true"
        _sha(row["fold_npz_sha256"], "private fold digest")
    expected_identities = {
        (method, candidate, sample)
        for method in METHODS
        for candidate in EXPECTED_CANDIDATES
        for sample in range(5)
    }
    if identities_seen != expected_identities:
        raise PublicationError("sample identity closure mismatch")

    candidate_bound = files["reports/POST_T12_CANDIDATE_SUMMARY.tsv"]
    candidate_text = candidate_bound.data.decode("utf-8", errors="strict")
    candidate_reader = csv.DictReader(io.StringIO(candidate_text), delimiter="\t")
    candidate_rows = list(candidate_reader)
    if len(candidate_rows) != 12 or {
        (row.get("method"), row.get("candidate_id")) for row in candidate_rows
    } != {(method, candidate) for method in METHODS for candidate in EXPECTED_CANDIDATES}:
        raise PublicationError("candidate summary closure mismatch")

    paired_rows = _tsv(
        files["reports/POST_T12_PAIRED_CANDIDATE_DELTAS.tsv"],
        PAIRED_FIELDS,
        "paired candidate deltas",
    )
    if len(paired_rows) != 48:
        raise PublicationError("paired candidate table must contain 48 rows")

    metric_public: dict[str, Any] = {}
    for metric, threshold_key, threshold_value in (
        (PRIMARY_METRIC, "target", 8.0),
        (FRAMEWORK_METRIC, "framework", 4.0),
    ):
        methods_public: dict[str, Any] = {}
        for method in METHODS:
            values = method_values[method][metric]
            if len(values) != 30:
                raise PublicationError(f"{method}/{metric} must contain 30 values")
            observed_median = statistics.median(values)
            recorded = comparison.get("method_summaries", {}).get(method, {})
            recorded_median = float(recorded.get("metrics", {}).get(metric, {}).get("median", math.nan))
            if not math.isclose(observed_median, recorded_median, rel_tol=0.0, abs_tol=2e-8):
                raise PublicationError(f"{method}/{metric} median mismatch")
            recorded_key = (
                "target_aligned_cdr_rmsd_le_8_angstrom"
                if metric == PRIMARY_METRIC
                else "framework_aligned_cdr_rmsd_le_4_angstrom"
            )
            _expect(
                recorded.get("threshold_counts", {}).get(recorded_key),
                threshold_counts[method][threshold_key],
                f"{method}/{metric} threshold count",
            )
            methods_public[method] = {
                "sample_count": 30,
                "median_angstrom": recorded_median,
                "at_or_below_threshold_count": threshold_counts[method][threshold_key],
                "threshold_denominator": 30,
            }
        directions = _direction_counts(paired_rows, metric)
        _expect(
            comparison.get("paired_candidate_comparisons", {}).get(metric, {}).get("candidate_direction_counts"),
            directions,
            f"{metric} direction counts",
        )
        metric_public[metric] = {
            "unit": "angstrom",
            "direction": "lower_is_better",
            "descriptive_threshold_angstrom": threshold_value,
            "threshold_is_a_new_success_gate": False,
            "t11": methods_public[METHODS[0]],
            "t12": methods_public[METHODS[1]],
            "t12_minus_t11_median_angstrom": (
                methods_public[METHODS[1]]["median_angstrom"]
                - methods_public[METHODS[0]]["median_angstrom"]
            ),
            "candidate_level_direction_counts": directions,
        }

    source = {
        "manifest_sha256": manifest.sha256,
        "comparison_receipt_sha256": files["operator_logs/COMPARISON_RECEIPT.json"].sha256,
        "comparison_report_sha256": files["reports/POST_T12_COMPARISON.json"].sha256,
        "t11_run_receipt_sha256": _sha(receipt.get("t11_receipt_sha256"), "T11 receipt digest"),
        "t12_run_receipt_sha256": _sha(receipt.get("t12_receipt_sha256"), "T12 receipt digest"),
        "historical_audit_sha256": _sha(receipt.get("historical_audit_sha256"), "historical audit digest"),
        "source_commit": _oid(receipt.get("source_commit"), "source commit"),
        "source_tree": _oid(receipt.get("source_tree"), "source tree"),
    }
    return source, comparison, metric_public, identities


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise PublicationError(process.stderr.strip() or f"git {' '.join(args)} failed")
    return process.stdout.strip()


def _validate_git_identity(repo: Path, source: Mapping[str, Any]) -> None:
    _expect(Path(_git(repo, "rev-parse", "--show-toplevel")), repo, "git top level")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PublicationError("repository must be clean before publication")
    _expect(_git(repo, "rev-parse", "HEAD"), source["source_commit"], "repository HEAD")
    _expect(_git(repo, "rev-parse", "HEAD^{tree}"), source["source_tree"], "repository tree")


def _prepare_output(repo: Path, output: Path) -> tuple[Path, Path]:
    if not output.is_absolute() or output.resolve(strict=False) != output:
        raise PublicationError("output directory must be absolute and canonical")
    if output.exists() or output.is_symlink():
        raise PublicationError(f"refusing to overwrite public output: {output}")
    parent = _canonical_directory(output.parent, "public output parent")
    try:
        output.relative_to(repo)
    except ValueError as exc:
        raise PublicationError("public output must be inside repository") from exc
    if parent == repo or DATE_DIRECTORY.fullmatch(output.name) is None:
        raise PublicationError("public output must be below a subdirectory and use the dated name")
    staging = parent / f".{output.name}.staging-{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        raise PublicationError(f"public staging path already exists: {staging}")
    return output, staging


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _csv_bytes(fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _privacy_check(name: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError(f"public file is not UTF-8: {name}") from exc
    normalized = unicodedata.normalize("NFKC", text)
    for character in normalized:
        code = ord(character)
        if character not in "\n\r\t" and (code < 32 or 0x7F <= code <= 0x9F):
            raise PublicationError(f"control character in public file: {name}")
        if code in {0x200B, 0x200C, 0x200D, 0x2060} or 0x202A <= code <= 0x202E:
            raise PublicationError(f"hidden/bidirectional character in public file: {name}")
    for pattern, label in SENSITIVE_PATTERNS:
        if pattern.search(normalized):
            raise PublicationError(f"{label} found in public file: {name}")
    if AMINO_ACID_SEQUENCE.search(normalized):
        raise PublicationError(f"possible amino-acid sequence in public file: {name}")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _manifest_bytes(root: Path) -> bytes:
    rows = []
    for path in sorted(
        (path for path in root.iterdir() if path.is_file() and path.name != "SHA256SUMS"),
        key=lambda path: path.name.encode("utf-8"),
    ):
        rows.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.name}\n")
    return "".join(rows).encode("utf-8")


def _replay_identities(identities: Mapping[Path, tuple[int, ...]]) -> None:
    for path, expected in identities.items():
        try:
            observed = _identity(path.lstat())
        except OSError as exc:
            raise PublicationError(f"private evidence disappeared: {path}") from exc
        if observed != expected:
            raise PublicationError(f"private evidence changed during publication: {path.name}")


def _render_public_files(
    source: Mapping[str, Any], comparison: Mapping[str, Any], metrics: Mapping[str, Any]
) -> dict[str, bytes]:
    primary = metrics[PRIMARY_METRIC]
    framework = metrics[FRAMEWORK_METRIC]
    primary_directions = primary["candidate_level_direction_counts"]
    framework_directions = framework["candidate_level_direction_counts"]
    if (
        primary_directions["improved"] == primary_directions["worsened"]
        and primary["t11"]["at_or_below_threshold_count"]
        == primary["t12"]["at_or_below_threshold_count"]
    ):
        primary_finding = "目标相对位姿未显示明确改善，候选层面的方向相互抵消。"
        primary_short = "未见明确改善"
    elif primary_directions["improved"] > primary_directions["worsened"]:
        primary_finding = "目标相对位姿出现描述性改善方向，但不足以形成确认性结论。"
        primary_short = "出现描述性改善方向"
    else:
        primary_finding = "目标相对位姿出现描述性变差方向。"
        primary_short = "出现描述性变差方向"
    if (
        framework["t12_minus_t11_median_angstrom"] < 0
        and framework_directions["improved"] > framework_directions["worsened"]
    ):
        framework_finding = "分子内部的 CDR 保持指标出现描述性改善，但不能替代目标相对位姿结果。"
        framework_short = "有描述性改善"
    elif framework["t12_minus_t11_median_angstrom"] > 0:
        framework_finding = "分子内部的 CDR 保持指标出现描述性变差。"
        framework_short = "出现描述性变差"
    else:
        framework_finding = "分子内部的 CDR 保持指标结果不一致。"
        framework_short = "结果不一致"
    summary = {
        "schema_version": PUBLIC_SUMMARY_SCHEMA,
        "status": "COMPLETE",
        "analysis_classification": "EXPLORATORY_DESCRIPTIVE",
        "decision_rule_predeclared": False,
        "comparison_outcome": "INCONCLUSIVE",
        "scope": {
            "same_candidate_count": 6,
            "folds_per_candidate_per_method": 5,
            "sample_count_per_method": 30,
            "independent_comparison_unit": "candidate",
            "cross_method_fold_pairing": False,
        },
        "primary_metric": {"name": PRIMARY_METRIC, **primary},
        "secondary_metric": {"name": FRAMEWORK_METRIC, **framework},
        "interpretation_zh": {
            "primary": primary_finding,
            "secondary": framework_finding,
            "decision": "当前结果不足以支持继续追加计算；若继续，应先修改约束或方法并预先登记评价规则。",
        },
        "limitations": [
            "只有六个候选；每个候选的五次折叠不是五个独立候选。",
            "两轮没有可验证的共同随机种子，因此样本序号不能跨方法配对。",
            "判定规则未在读取本轮结果前预先登记，整体结论只能是探索性且不确定。",
            "所有指标均为计算代理，未进行湿实验。",
        ],
        "scientific_claim_boundary": CLAIM_BOUNDARY,
    }
    method = {
        "schema_version": PUBLIC_METHOD_SCHEMA,
        "method_id": "POST_T12_READONLY_COMPARISON",
        "method_version": 1,
        "analysis_classification": "EXPLORATORY_DESCRIPTIVE",
        "decision_rule_predeclared": False,
        "population": "同一组六个候选在两种模板组织方式下的既有折叠结果",
        "exclusion_rule": "排除未完成的首次 T12 运行，仅使用封存且验证通过的完整运行。",
        "alignment": {
            "primary": "以目标区域对齐后评估 CDR 骨架偏差",
            "secondary": "以框架区域对齐后评估 CDR 骨架偏差",
        },
        "aggregation_order": "先在每个候选内汇总五次折叠，再按六个候选比较方向；不进行逐样本跨方法配对。",
        "descriptive_thresholds_angstrom": {"primary": 8.0, "secondary": 4.0},
        "thresholds_are_new_success_gates": False,
        "missing_or_nonfinite_values_allowed": False,
        "inference_test_performed": False,
        "source_commit": source["source_commit"],
        "source_tree": source["source_tree"],
        "scientific_claim_boundary": CLAIM_BOUNDARY,
    }
    receipt = {
        "schema_version": PUBLIC_RECEIPT_SCHEMA,
        "status": "COMPLETE",
        "publication_kind": "SANITIZED_AGGREGATE_COMPARISON",
        "source": {
            "commit": source["source_commit"],
            "tree": source["source_tree"],
            "sealed_source_manifest_sha256": source["manifest_sha256"],
            "sealed_comparison_receipt_sha256": source["comparison_receipt_sha256"],
            "sealed_comparison_report_sha256": source["comparison_report_sha256"],
            "t11_run_receipt_sha256": source["t11_run_receipt_sha256"],
            "t12_run_receipt_sha256": source["t12_run_receipt_sha256"],
            "historical_audit_sha256": source["historical_audit_sha256"],
        },
        "process": {
            "cpu_only": True,
            "new_model_inference_performed": False,
            "gpu_started": False,
            "training_performed": False,
            "bindcraft_started": False,
            "wet_lab_performed": False,
            "source_files_modified": False,
        },
        "scope": {
            "candidate_count": 6,
            "folds_per_candidate_per_method": 5,
            "sample_count_per_method": 30,
            "comparison_unit": "candidate",
            "sample_pairing": "UNPAIRED_ACROSS_METHODS",
        },
        "integrity_checks": {
            "input_closure_verified": True,
            "sealed_manifest_replayed": True,
            "source_hashes_unchanged": True,
            "metric_values_finite": True,
            "missing_record_count": 0,
            "duplicate_record_count": 0,
            "method_contract_bound": True,
        },
        "historical_cpu_gate": {
            "status": "FAIL",
            "observed_pass_count": 7,
            "required_pass_count": 10,
            "denominator": 30,
            "reclassified_as_pass": False,
        },
        "result": {
            "engineering_status": "COMPLETE",
            "package_validation_status": "PASS",
            "comparison_outcome": "INCONCLUSIVE",
            "experimental_validation": "NOT_PERFORMED",
        },
        "scientific_claim_boundary": CLAIM_BOUNDARY,
    }
    readme = f"""# T12 与 T11 只读对比公开摘要

本次只读对比已完整核验：在同一组 6 个候选上，T12 的目标相对位姿指标{primary_short}，分子内部 CDR 保持指标{framework_short}。由于评价规则未在读取结果前预先登记，整体结论只能是探索性且不确定；当前不支持直接追加计算，若继续应先修改约束或方法。

## 领导可直接读取的结论

- 主指标：中位数从 {primary['t11']['median_angstrom']:.3f} Å 变为 {primary['t12']['median_angstrom']:.3f} Å；达到既有 8 Å 描述阈值的数量仍为 {primary['t11']['at_or_below_threshold_count']}/30 与 {primary['t12']['at_or_below_threshold_count']}/30；候选方向为 {primary['candidate_level_direction_counts']['improved']} 个改善、{primary['candidate_level_direction_counts']['worsened']} 个变差。
- 辅助指标：中位数从 {framework['t11']['median_angstrom']:.3f} Å 降至 {framework['t12']['median_angstrom']:.3f} Å；达到既有 4 Å 描述阈值的数量从 {framework['t11']['at_or_below_threshold_count']}/30 变为 {framework['t12']['at_or_below_threshold_count']}/30；候选方向为 {framework['candidate_level_direction_counts']['improved']} 个改善、{framework['candidate_level_direction_counts']['worsened']} 个变差。
- 正确解释：{primary_finding}{framework_finding}
- 决策边界：这是已有结果的 CPU 只读分析，没有新增模型推理、GPU 运行、训练、BindCraft 或湿实验。

本目录只包含聚合指标、方法和哈希收据，不包含候选身份、逐候选或逐样本结果、序列、结构坐标、原始日志、完整命令、权重、运行环境或本机路径。这些计算代理不能证明结合、亲和力、选择性、安全性或成药性。

## 文件

- `POST_T12_COMPARISON_SUMMARY.json`：两项几何指标的聚合结果与限制。
- `POST_T12_COMPARISON_METHOD.json`：比较单位、对齐方式、聚合顺序和解释边界。
- `POST_T12_COMPARISON_PUBLIC_RECEIPT.json`：来源哈希、工程完整性和历史门状态。
- `ARTIFACT_INDEX.csv`：公开文件及封存证据的内容哈希索引。
- `SHA256SUMS`：公开包完整性清单。
"""
    return {
        "README.md": readme.encode("utf-8"),
        "POST_T12_COMPARISON_PUBLIC_RECEIPT.json": _json_bytes(receipt),
        "POST_T12_COMPARISON_SUMMARY.json": _json_bytes(summary),
        "POST_T12_COMPARISON_METHOD.json": _json_bytes(method),
    }


def build_bundle(comparison_attempt: Path, repo_root: Path, output_dir: Path) -> Path:
    repo = _canonical_directory(repo_root, "repository")
    attempt = _canonical_directory(comparison_attempt, "comparison attempt")
    try:
        attempt.relative_to(repo)
    except ValueError:
        pass
    else:
        raise PublicationError("private comparison attempt must remain outside repository")
    destination, staging = _prepare_output(repo, output_dir)
    source, comparison, metrics, identities = _validate_private_source(attempt)
    _validate_git_identity(repo, source)
    rendered = _render_public_files(source, comparison, metrics)
    for name, data in rendered.items():
        _privacy_check(name, data)

    try:
        staging.mkdir(mode=0o700)
        for name, data in rendered.items():
            _write(staging / name, data)
        index_rows = [
            {
                "artifact_id": name,
                "publication_scope": "PUBLIC_FILE",
                "path": name,
                "sha256": hashlib.sha256(rendered[name]).hexdigest(),
            }
            for name in rendered
        ]
        index_rows.extend(
            {
                "artifact_id": artifact_id,
                "publication_scope": "SEALED_SOURCE_DIGEST_ONLY",
                "path": "",
                "sha256": source[key],
            }
            for artifact_id, key in (
                ("source_manifest", "manifest_sha256"),
                ("comparison_receipt", "comparison_receipt_sha256"),
                ("comparison_report", "comparison_report_sha256"),
                ("t11_run_receipt", "t11_run_receipt_sha256"),
                ("t12_run_receipt", "t12_run_receipt_sha256"),
                ("historical_audit", "historical_audit_sha256"),
            )
        )
        index = _csv_bytes(
            ("artifact_id", "publication_scope", "path", "sha256"), index_rows
        )
        _privacy_check("ARTIFACT_INDEX.csv", index)
        _write(staging / "ARTIFACT_INDEX.csv", index)
        manifest = _manifest_bytes(staging)
        _privacy_check("SHA256SUMS", manifest)
        _write(staging / "SHA256SUMS", manifest)
        if {path.name for path in staging.iterdir() if path.is_file()} != set(PUBLIC_FILES) | {"SHA256SUMS"}:
            raise PublicationError("public file closure mismatch")
        for path in staging.iterdir():
            if path.is_file():
                _privacy_check(path.name, path.read_bytes())
        _replay_identities(identities)
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = build_bundle(args.comparison_attempt, args.repo_root, args.output_dir)
    except (OSError, PublicationError, ValueError) as exc:
        print(f"post-T12 public bundle failed: {exc}", file=sys.stderr)
        return 2
    print(f"post-T12 public bundle complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
