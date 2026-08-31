#!/usr/bin/env python3
"""Mechanically release the ten formal G2 anchors after strict AIV1 validation.

Code source: project_original.  The actual G2 evidence and release artifacts are
validated by the frozen ``aiv1_technical_gate_20260828`` implementation; this
entrypoint only supplies the formal-G1 guard, derives the deterministic rows,
and publishes the two production files without replacement.
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
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


AIV1_DIRECTORY = (
    Path(__file__).resolve().parents[2] / "aiv1_technical_gate_20260828"
)
if not (AIV1_DIRECTORY / "build_ai_validation_matrix.py").is_file():
    raise RuntimeError(f"missing frozen AIV1 validator: {AIV1_DIRECTORY}")
sys.path.insert(0, str(AIV1_DIRECTORY))

from build_ai_validation_matrix import (  # noqa: E402
    ANCHOR_FIELDS,
    CANONICAL_AIV0_SUMMARY_RELATIVE,
    CANONICAL_INPUT_CONTRACT_RELATIVE,
    CANONICAL_REGISTRY_SCHEMA_RELATIVE,
    CANONICAL_STATE_CONTRACT_RELATIVE,
    ContractViolation,
    G2_RECEIPT_FIELDS,
    canonical_json,
    candidate_id_set_sha256,
    load_input_contract,
    sha256_file,
    sha256_text,
    validate_anchors_and_g2,
)


PRODUCTION_ANCHOR_RELATIVE = Path(
    "data/boltzgen_data/glp1_vhh_production_v1/07_analysis/ai_validation/"
    "anchor_candidate_set_v1.tsv"
)
PRODUCTION_RECEIPT_RELATIVE = Path(
    "data/boltzgen_data/glp1_vhh_production_v1/04_pilot/g2/"
    "G2_anchor_release.receipt.json"
)
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEQUENCE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORMAL_G1_SCHEMA = "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1"
FORMAL_ENVIRONMENT_REVISION = re.compile(
    r"^WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V[1-9][0-9]*$"
)
FORMAL_G1_OFFICIAL_CONTRACT = {
    "boltzgen": "0.3.2",
    "cuequivariance": "0.6.1",
    "torch": "2.8.0+cu128",
    "torch_cuda": "12.8",
    "triton": "3.4.0",
}
RELEASE_SCHEMA = "AIV1_G2_ANCHOR_RELEASE_V2"
RELEASE_TRACE_FIELDS = frozenset(
    {
        "formal_g1_receipt_uri",
        "formal_g1_receipt_path",
        "formal_g1_receipt_sha256",
        "environment_manifest_uri",
        "environment_manifest_path",
        "environment_manifest_sha256",
    }
)


@dataclass
class HeldReleaseFile:
    """A release path held by parent and file descriptors across publication."""

    path: Path
    parent_fd: int
    file_fd: int
    identity: tuple[int, int]
    content_sha256: str
    created_by_run: bool


def parse_args() -> argparse.Namespace:
    """Parse the frozen local release interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--acceptance-root", required=True, type=Path)
    parser.add_argument("--g1-receipt", required=True, type=Path)
    parser.add_argument(
        "--g1-receipt-sha256",
        required=True,
        help="independently transported SHA-256 of the exact formal G1 receipt bytes",
    )
    parser.add_argument("--aiv0-final-receipt", required=True, type=Path)
    parser.add_argument("--platform-evidence", required=True, type=Path)
    parser.add_argument("--environment-manifest", required=True, type=Path)
    parser.add_argument("--runtime-scripts-manifest", required=True, type=Path)
    parser.add_argument("--anchor-output", required=True, type=Path)
    parser.add_argument("--receipt-output", required=True, type=Path)
    return parser.parse_args()


def require_directory(path: Path, label: str) -> Path:
    """Return an absolute existing non-symlink directory."""

    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise ValueError(f"{label} must be an absolute non-symlink directory: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")
    return resolved


def require_file(path: Path, label: str) -> Path:
    """Return an absolute existing regular file without accepting a symlink."""

    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise ValueError(f"{label} must be an absolute non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    return resolved


def require_canonical_file(path: Path, label: str) -> Path:
    """Require an absolute regular file whose lexical path is already canonical."""

    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise ValueError(
            f"{label} must be an absolute canonical non-symlink file: {path}"
        )
    resolved = path.resolve(strict=True)
    if path != resolved or not resolved.is_file():
        raise ValueError(
            f"{label} path contains a symlink hop or is non-canonical: {path}"
        )
    return resolved


def require_sha256(value: object, label: str) -> str:
    """Return one exact lowercase SHA-256 digest."""

    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def read_stable_file(path: Path, label: str) -> tuple[bytes, str]:
    """Read canonical file bytes once and reject path or content races."""

    canonical = require_canonical_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(canonical, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file")
        digest = hashlib.sha256()
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError(f"{label} changed while it was being hashed")
        canonical_after = require_canonical_file(path, label)
        current = canonical_after.stat()
        if file_identity(current) != file_identity(after):
            raise ValueError(f"{label} path identity changed while it was being read")
        return b"".join(blocks), digest.hexdigest()
    finally:
        os.close(descriptor)


def sha256_stable_file(path: Path, label: str) -> str:
    """Hash a canonical non-symlink file with identity checks."""

    _, digest = read_stable_file(path, label)
    return digest


def json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting ambiguous duplicate names."""

    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def load_json(path: Path, label: str) -> Mapping[str, object]:
    """Load one stable canonical UTF-8 JSON object without duplicate keys."""

    content, _ = read_stable_file(path, label)
    payload = json.loads(
        content.decode("utf-8"), object_pairs_hook=json_no_duplicates
    )
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def require_exact_output(path: Path, workspace: Path, relative: Path, label: str) -> Path:
    """Require the exact production-relative output, including filename case."""

    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute canonical path")
    expected = workspace / relative
    if path != expected:
        raise ValueError(f"{label} must be exactly {expected}, got {path}")
    return path


def workspace_uri(path: Path, workspace: Path, label: str) -> str:
    """Render a canonical workspace URI for an existing file."""

    resolved = require_file(path, label)
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as error:
        raise ValueError(f"{label} is outside the workspace: {resolved}") from error
    return f"workspace://{relative.as_posix()}"


def verify_formal_g1(
    receipt_path: Path,
    environment_manifest: Path,
    expected_receipt_sha256: str | None = None,
) -> tuple[str, str]:
    """Validate the supported formal-G1 receipt and both immutable bindings."""

    receipt_bytes, receipt_sha = read_stable_file(receipt_path, "formal G1 receipt")
    if expected_receipt_sha256 is not None:
        expected_sha = require_sha256(
            expected_receipt_sha256, "formal G1 expected receipt SHA-256"
        )
        if receipt_sha != expected_sha:
            raise ValueError(
                "formal G1 receipt SHA-256 does not match the expected digest"
            )
    receipt = json.loads(
        receipt_bytes.decode("utf-8"), object_pairs_hook=json_no_duplicates
    )
    if not isinstance(receipt, dict):
        raise ValueError("formal G1 receipt must be a JSON object")
    if receipt.get("schema_version") != FORMAL_G1_SCHEMA:
        raise ValueError("unsupported formal G1 receipt schema_version")
    attempt_id = receipt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("formal G1 receipt lacks a non-empty attempt_id")
    revision = receipt.get("environment_contract_revision")
    if (
        not isinstance(revision, str)
        or FORMAL_ENVIRONMENT_REVISION.fullmatch(revision) is None
    ):
        raise ValueError(
            "formal G1 receipt lacks a versioned formal environment revision"
        )
    exit_code = receipt.get("exit_code")
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code != 0
    ):
        raise ValueError("formal G1 receipt exit_code must be integer zero")
    if receipt.get("status") != "G1_PASS" or receipt.get("formal_g1") is not True:
        raise ValueError("formal G1 receipt is not G1_PASS/formal_g1=true")
    if receipt.get("failure_codes") != [] or receipt.get("failure_stage") is not None:
        raise ValueError("formal G1 receipt contains failure evidence")
    if receipt.get("environment_contract_revision_required") is not False:
        raise ValueError(
            "formal G1 receipt still requires an environment contract revision"
        )
    if receipt.get("compatibility_activation") != "EXPLICIT_PROCESS_LOCAL_ONLY":
        raise ValueError(
            "formal G1 receipt has unsafe compatibility activation semantics"
        )
    if receipt.get("official_contract") != FORMAL_G1_OFFICIAL_CONTRACT:
        raise ValueError(
            "formal G1 receipt official_contract is not the frozen contract"
        )
    environment_sha = sha256_stable_file(
        environment_manifest, "environment manifest"
    )
    if receipt.get("environment_manifest_sha256") != environment_sha:
        raise ValueError("formal G1 receipt does not bind the environment manifest")
    return receipt_sha, environment_sha


def verify_input_contract_hashes(
    repo: Path, input_contract: Mapping[str, object]
) -> None:
    """Recompute the two semantic/schema files named by the AIV1 contract."""

    state_contract = require_canonical_file(
        repo / CANONICAL_STATE_CONTRACT_RELATIVE,
        "AIV1 development-state semantic contract",
    )
    registry_schema = require_canonical_file(
        repo / CANONICAL_REGISTRY_SCHEMA_RELATIVE,
        "AIV1 experience-registry schema",
    )
    expected = {
        "development_state_contract_sha256": sha256_stable_file(
            state_contract, "AIV1 development-state semantic contract"
        ),
        "experience_registry_schema_sha256": sha256_stable_file(
            registry_schema, "AIV1 experience-registry schema"
        ),
    }
    for field, observed in expected.items():
        if input_contract.get(field) != observed:
            raise ValueError(f"AIV1 input contract {field} does not match its file")


def verify_strict_acceptance_json(acceptance_root: Path) -> None:
    """Reject duplicate names in every JSON object consumed below G2."""

    for path in sorted(acceptance_root.rglob("*.json")):
        load_json(path, f"G2 JSON evidence {path.relative_to(acceptance_root)}")


def verify_g2_marker_bindings(
    *,
    acceptance_root: Path,
    generation_cell: str,
    formal_g1_sha: str,
    environment_sha: str,
) -> None:
    """Bind the acceptance marker and both resource probes to one formal G1."""

    markers = (
        (
            acceptance_root / generation_cell / "operator_logs/cell.SUCCESS.json",
            "G2 7XL0 acceptance SUCCESS marker",
        ),
        (
            acceptance_root
            / "6xym_diverse_batch5__attempt_001/operator_logs/probe.SUCCESS.json",
            "G2 6XYM diverse SUCCESS marker",
        ),
        (
            acceptance_root
            / "6xym_adherence_batch5__attempt_001/operator_logs/probe.SUCCESS.json",
            "G2 6XYM adherence SUCCESS marker",
        ),
    )
    expected = {
        "formal_g1_receipt_sha256": formal_g1_sha,
        "environment_manifest_sha256": environment_sha,
    }
    for path, label in markers:
        marker = load_json(path, label)
        for field, value in expected.items():
            if marker.get(field) != value:
                raise ValueError(f"{label} does not bind the formal G1 {field}")


def verify_aiv0_binding(repo: Path, receipt_path: Path) -> str:
    """Bind the supplied AIV0 receipt to the canonical repository summary."""

    summary_path = require_file(
        repo / CANONICAL_AIV0_SUMMARY_RELATIVE, "canonical AIV0 summary"
    )
    summary = load_json(summary_path, "canonical AIV0 summary")
    receipt = load_json(receipt_path, "AIV0 final receipt")
    if (
        summary.get("schema_version") != "AIV0_M0_REPOSITORY_SUMMARY_V1"
        or summary.get("status") != "M0_PASS_ASSET_AND_SEMANTIC_READINESS"
    ):
        raise ValueError("canonical AIV0 summary is not the frozen PASS summary")
    if (
        receipt.get("schema_version") != "AIV0_STAGE_RECEIPT_V1"
        or receipt.get("status") != "PASS"
        or receipt.get("validator_mode") != "check"
        or receipt.get("exit_code") != 0
    ):
        raise ValueError("AIV0 final receipt is not a successful read-only check")
    evidence = summary.get("authoritative_evidence")
    actual_sha = sha256_file(receipt_path)
    if not isinstance(evidence, dict) or evidence.get("final_check_receipt_sha256") != actual_sha:
        raise ValueError("AIV0 final receipt SHA-256 is not bound by canonical summary")
    return actual_sha


def verify_release_traceability(
    release_receipt: Path,
    *,
    g1_receipt: Path,
    g1_receipt_sha: str,
    environment_manifest: Path,
    environment_sha: str,
    workspace: Path,
) -> None:
    """Require an existing release to preserve the current formal-G1 chain."""

    payload = load_json(
        require_canonical_file(release_receipt, "existing G2 release receipt"),
        "existing G2 release receipt",
    )
    expected = {
        "formal_g1_receipt_uri": workspace_uri(
            g1_receipt, workspace, "formal G1 receipt"
        ),
        "formal_g1_receipt_path": str(g1_receipt),
        "formal_g1_receipt_sha256": g1_receipt_sha,
        "environment_manifest_uri": workspace_uri(
            environment_manifest, workspace, "environment manifest"
        ),
        "environment_manifest_path": str(environment_manifest),
        "environment_manifest_sha256": environment_sha,
        "environment_sha256": environment_sha,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ValueError(f"existing G2 release receipt has stale {field}")


def read_aggregate(path: Path) -> list[dict[str, str]]:
    """Read the exact ten-candidate G2 aggregate index."""

    resolved = require_file(path, "G2 aggregate metrics")
    with resolved.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        required = {"id", "file_name", "designed_chain_sequence"}
        if len(fields) != len(set(fields)) or not required.issubset(fields):
            raise ValueError("G2 aggregate lacks unique id/file_name/sequence columns")
        rows = [dict(row) for row in reader]
    candidate_ids = [row["id"] for row in rows]
    if len(rows) != 10 or len(set(candidate_ids)) != 10:
        raise ValueError("G2 aggregate must contain exactly ten unique candidates")
    return sorted(rows, key=lambda row: row["id"])


def render_anchors(
    *,
    rows: list[dict[str, str]],
    generation: Mapping[str, object],
    acceptance_output: Path,
    workspace: Path,
    config_sha: str,
    code_sha: str,
    environment_sha: str,
) -> str:
    """Derive the deterministic AIV1 anchor TSV from G2 aggregate rows."""

    anchors: list[dict[str, str]] = []
    refold_root = acceptance_output / "intermediate_designs_inverse_folded/refold_cif"
    for order, row in enumerate(rows):
        candidate_id = row["id"]
        filename = row["file_name"]
        if SAFE_FILENAME.fullmatch(filename) is None or Path(filename).name != filename:
            raise ValueError(f"unsafe candidate file_name: {filename!r}")
        sequence = re.sub(r"\s+", "", row["designed_chain_sequence"]).upper()
        if SEQUENCE.fullmatch(sequence) is None:
            raise ValueError(f"invalid full sequence for {candidate_id}")
        artifact = require_file(refold_root / filename, f"refold artifact {candidate_id}")
        anchors.append(
            {
                "anchor_order": str(order),
                "candidate_id": candidate_id,
                "full_sequence": sequence,
                "full_sequence_sha256": sha256_text(sequence),
                "generation_cell_id": str(generation["generation_cell_id"]),
                "shard_id": str(generation["shard_id"]),
                "scaffold_id": str(generation["scaffold_id"]),
                "checkpoint_id": str(generation["checkpoint_id"]),
                "candidate_artifact_uri": workspace_uri(
                    artifact, workspace, f"refold artifact {candidate_id}"
                ),
                "candidate_artifact_sha256": sha256_file(artifact),
                "config_sha256": config_sha,
                "code_sha256": code_sha,
                "environment_sha256": environment_sha,
                "rng_seed_status": "NOT_EXPOSED_BY_CLI",
                "rng_seed": "",
            }
        )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=ANCHOR_FIELDS, delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(anchors)
    return buffer.getvalue()


def write_temporary(workspace: Path, prefix: str, payload: bytes) -> Path:
    """Write and fsync a temporary regular file on the workspace filesystem."""

    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, dir=workspace)
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def validate_release_with_frozen_aiv1(
    *,
    anchor_manifest_path: Path,
    g2_receipt_path: Path,
    input_contract: Mapping[str, object],
    aiv0_handoff: Mapping[str, object],
    repo_root: Path,
    workspace_root: Path,
) -> None:
    """Validate V2 plus its exact V1 projection with the frozen AIV1 gate."""

    receipt = load_json(g2_receipt_path, "G2 anchor release receipt V2")
    expected_fields = G2_RECEIPT_FIELDS | RELEASE_TRACE_FIELDS
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version") != RELEASE_SCHEMA
    ):
        raise ValueError("G2 V2 release receipt keyset/schema mismatch")
    legacy = {field: receipt[field] for field in G2_RECEIPT_FIELDS}
    legacy["schema_version"] = "AIV1_G2_ANCHOR_RELEASE_V1"
    projection = write_temporary(
        workspace_root,
        ".G2_anchor_release.v1_projection.",
        canonical_json(legacy).encode("utf-8"),
    )
    try:
        validate_anchors_and_g2(
            anchor_manifest_path=anchor_manifest_path,
            g2_receipt_path=projection,
            input_contract=input_contract,
            aiv0_handoff=aiv0_handoff,
            repo_root=repo_root,
            workspace_root=workspace_root,
        )
    finally:
        projection.unlink(missing_ok=True)


def open_output_parent(path: Path, workspace: Path, *, create: bool) -> int:
    """Open the output parent beneath workspace using no-follow directory FDs."""

    try:
        relative_parent = path.parent.relative_to(workspace)
    except ValueError as error:
        raise ValueError(
            f"output parent is outside the workspace: {path.parent}"
        ) from error
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(workspace, flags)
    try:
        for part in relative_parent.parts:
            if part in {"", ".", ".."}:
                raise ValueError(f"unsafe output parent component: {part!r}")
            if create:
                try:
                    os.mkdir(part, mode=0o750, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def ensure_output_parent(path: Path, workspace: Path) -> None:
    """Create an output parent below workspace without following symlink hops."""

    descriptor = open_output_parent(path, workspace, create=True)
    os.close(descriptor)


def file_identity(value: os.stat_result) -> tuple[int, int]:
    """Return the filesystem identity used for race detection."""

    return value.st_dev, value.st_ino


def verify_published_path(
    destination: Path,
    workspace: Path,
    parent_descriptor: int,
    published_identity: tuple[int, int],
) -> None:
    """Require the canonical destination still names the just-linked inode."""

    current_parent = open_output_parent(destination, workspace, create=False)
    try:
        if file_identity(os.fstat(current_parent)) != file_identity(
            os.fstat(parent_descriptor)
        ):
            raise ValueError("output parent identity changed during publication")
        current = os.stat(
            destination.name, dir_fd=current_parent, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or file_identity(current) != published_identity
        ):
            raise ValueError(
                "canonical output path does not identify the published file"
            )
    finally:
        os.close(current_parent)
    canonical = require_canonical_file(destination, "published release output")
    if file_identity(canonical.stat()) != published_identity:
        raise ValueError("published release output identity changed after publication")


def descriptor_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return identity and content-change metadata for one open file."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def sha256_descriptor(descriptor: int, label: str) -> str:
    """Hash a held regular-file descriptor without changing its offset."""

    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} is not a held regular file")
    digest = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(descriptor, 1024 * 1024, offset)
        if not block:
            break
        digest.update(block)
        offset += len(block)
    after = os.fstat(descriptor)
    if descriptor_signature(before) != descriptor_signature(after):
        raise ValueError(f"{label} changed while its held descriptor was hashed")
    return digest.hexdigest()


def verify_held_release_file(held: HeldReleaseFile, workspace: Path) -> None:
    """Recheck held content and require the canonical path to name that inode."""

    opened = os.fstat(held.file_fd)
    if file_identity(opened) != held.identity:
        raise ValueError(f"held release file identity changed: {held.path}")
    if sha256_descriptor(held.file_fd, str(held.path)) != held.content_sha256:
        raise ValueError(f"held release file content changed: {held.path}")
    verify_published_path(held.path, workspace, held.parent_fd, held.identity)


def hold_existing_release_file(
    path: Path,
    workspace: Path,
    *,
    expected_sha256: str | None = None,
) -> HeldReleaseFile:
    """Open and retain an existing canonical release file and its parent."""

    parent_fd = open_output_parent(path, workspace, create=False)
    file_fd: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_fd = os.open(path.name, flags, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"release path is not a regular file: {path}")
        digest = sha256_descriptor(file_fd, str(path))
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError(f"release path content differs from derived bytes: {path}")
        held = HeldReleaseFile(
            path=path,
            parent_fd=parent_fd,
            file_fd=file_fd,
            identity=file_identity(opened),
            content_sha256=digest,
            created_by_run=False,
        )
        verify_held_release_file(held, workspace)
        return held
    except BaseException:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)
        raise


def close_held_release_file(held: HeldReleaseFile | None) -> None:
    """Close a held release file without mutating its directory entry."""

    if held is not None:
        os.close(held.file_fd)
        os.close(held.parent_fd)


def unlink_held_if_current(held: HeldReleaseFile) -> bool:
    """Remove only the directory entry that still names the held inode."""

    try:
        current = os.stat(
            held.path.name, dir_fd=held.parent_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return False
    if file_identity(current) != held.identity:
        return False
    os.unlink(held.path.name, dir_fd=held.parent_fd)
    os.fsync(held.parent_fd)
    return True


def publish_no_replace_held(
    temporary: Path, destination: Path, workspace: Path
) -> HeldReleaseFile:
    """Publish through a parent FD and retain both parent and file identity."""

    ensure_output_parent(destination, workspace)
    parent_fd = open_output_parent(destination, workspace, create=False)
    linked_identity: tuple[int, int] | None = None
    file_fd: int | None = None
    held: HeldReleaseFile | None = None
    try:
        try:
            existing = os.stat(
                destination.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            raise FileExistsError(
                f"immutable release output already exists: {destination}"
            )
        os.chmod(temporary, 0o444, follow_symlinks=False)
        source = os.stat(temporary, follow_symlinks=False)
        if not stat.S_ISREG(source.st_mode):
            raise ValueError(f"release temporary is not a regular file: {temporary}")
        source_sha = sha256_stable_file(temporary, "release temporary")
        os.link(
            temporary,
            destination.name,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        linked = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        linked_identity = file_identity(linked)
        if not stat.S_ISREG(linked.st_mode) or linked_identity != file_identity(source):
            raise ValueError("published output is not the immutable temporary inode")
        os.fsync(parent_fd)
        verify_published_path(destination, workspace, parent_fd, linked_identity)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_fd = os.open(destination.name, flags, dir_fd=parent_fd)
        if file_identity(os.fstat(file_fd)) != linked_identity:
            raise ValueError("published output changed before its inode was held")
        held = HeldReleaseFile(
            path=destination,
            parent_fd=parent_fd,
            file_fd=file_fd,
            identity=linked_identity,
            content_sha256=source_sha,
            created_by_run=True,
        )
        verify_held_release_file(held, workspace)
        temporary.unlink()
    except BaseException:
        if file_fd is not None:
            os.close(file_fd)
        if linked_identity is not None:
            try:
                current = os.stat(
                    destination.name, dir_fd=parent_fd, follow_symlinks=False
                )
                if file_identity(current) == linked_identity:
                    os.unlink(destination.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
        raise
    if held is None:
        raise RuntimeError("publication completed without a held release file")
    return held


def publish_no_replace(temporary: Path, destination: Path, workspace: Path) -> None:
    """Compatibility wrapper for one safely published standalone file."""

    held = publish_no_replace_held(temporary, destination, workspace)
    close_held_release_file(held)


def main() -> int:
    """Validate the full evidence chain, then publish anchor and receipt last."""

    os.umask(0o077)
    args = parse_args()
    workspace = require_directory(args.workspace_root, "workspace root")
    repo = require_directory(args.repo_root, "repo root")
    acceptance_root = require_directory(args.acceptance_root, "G2 acceptance root")
    g1_receipt = require_canonical_file(args.g1_receipt, "formal G1 receipt")
    g1_expected_sha = require_sha256(
        args.g1_receipt_sha256, "formal G1 expected receipt SHA-256"
    )
    aiv0_receipt = require_file(args.aiv0_final_receipt, "AIV0 final receipt")
    platform = require_file(args.platform_evidence, "platform evidence")
    environment_manifest = require_file(
        args.environment_manifest, "environment manifest"
    )
    runtime_manifest = require_file(
        args.runtime_scripts_manifest, "runtime scripts manifest"
    )
    anchor_output = require_exact_output(
        args.anchor_output,
        workspace,
        PRODUCTION_ANCHOR_RELATIVE,
        "anchor output",
    )
    receipt_output = require_exact_output(
        args.receipt_output,
        workspace,
        PRODUCTION_RECEIPT_RELATIVE,
        "G2 receipt output",
    )

    formal_g1_sha, environment_sha = verify_formal_g1(
        g1_receipt, environment_manifest, g1_expected_sha
    )
    aiv0_sha = verify_aiv0_binding(repo, aiv0_receipt)
    load_json(platform, "platform evidence")

    expected_runtime = require_file(
        acceptance_root.parent.parent
        / "provenance/gpu_runtime_scripts_SHA256SUMS",
        "canonical runtime scripts manifest",
    )
    expected_environment = require_file(
        acceptance_root.parent.parent / "environment_provenance.SHA256SUMS",
        "canonical environment manifest",
    )
    if runtime_manifest != expected_runtime or environment_manifest != expected_environment:
        raise ValueError("supplied runtime/environment manifest is not the executed one")

    contract_path = require_file(
        repo / CANONICAL_INPUT_CONTRACT_RELATIVE, "canonical AIV1 input contract"
    )
    strict_input_contract = load_json(contract_path, "canonical AIV1 input contract")
    input_contract = load_input_contract(contract_path)
    if input_contract != strict_input_contract:
        raise ValueError("AIV1 input contract changed while it was being validated")
    verify_input_contract_hashes(repo, input_contract)
    generation = input_contract.get("generation_contract")
    if not isinstance(generation, dict):
        raise ValueError("AIV1 input contract lacks generation_contract")

    generation_cell = str(generation["generation_cell_id"])
    acceptance_output = require_directory(
        acceptance_root / generation_cell, "7XL0 adherence acceptance output"
    )
    acceptance_log = require_directory(
        acceptance_output / "operator_logs", "7XL0 adherence operator logs"
    )
    aggregate = require_file(
        acceptance_output
        / "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv",
        "G2 aggregate metrics",
    )
    gate = require_file(acceptance_root / "G2_acceptance_gate.json", "G2 gate")
    resource_status = require_file(
        acceptance_root / "G2_resource_probe.status.txt", "G2 resource status"
    )
    output_manifest = require_file(
        acceptance_log / "output_SHA256SUMS", "7XL0 output manifest"
    )
    config_manifest = require_file(
        acceptance_log / "resolved_config_SHA256SUMS", "7XL0 config manifest"
    )
    verify_strict_acceptance_json(acceptance_root)
    verify_g2_marker_bindings(
        acceptance_root=acceptance_root,
        generation_cell=generation_cell,
        formal_g1_sha=formal_g1_sha,
        environment_sha=environment_sha,
    )

    anchor_text = render_anchors(
        rows=read_aggregate(aggregate),
        generation=generation,
        acceptance_output=acceptance_output,
        workspace=workspace,
        config_sha=sha256_file(config_manifest),
        code_sha=sha256_file(runtime_manifest),
        environment_sha=sha256_file(environment_manifest),
    )
    anchor_bytes = anchor_text.encode("utf-8")
    anchor_sha = hashlib.sha256(anchor_bytes).hexdigest()
    candidate_ids = [
        row["id"] for row in read_aggregate(aggregate)
    ]
    receipt_payload = {
        "schema_version": RELEASE_SCHEMA,
        "gate_id": "G2",
        "status": "PASS",
        **generation,
        "platform_evidence_uri": workspace_uri(
            platform, workspace, "platform evidence"
        ),
        "platform_evidence_sha256": sha256_file(platform),
        "g2_acceptance_gate_uri": workspace_uri(gate, workspace, "G2 gate"),
        "g2_acceptance_gate_sha256": sha256_file(gate),
        "g2_resource_probe_status_uri": workspace_uri(
            resource_status, workspace, "G2 resource status"
        ),
        "g2_resource_probe_status_sha256": sha256_file(resource_status),
        "aggregate_metrics_uri": workspace_uri(aggregate, workspace, "G2 aggregate"),
        "aggregate_metrics_sha256": sha256_file(aggregate),
        "candidate_count": 10,
        "candidate_id_set_sha256": candidate_id_set_sha256(candidate_ids),
        "anchor_manifest_sha256": anchor_sha,
        "aiv0_final_check_receipt_sha256": aiv0_sha,
        "formal_g1_receipt_uri": workspace_uri(
            g1_receipt, workspace, "formal G1 receipt"
        ),
        "formal_g1_receipt_path": str(g1_receipt),
        "formal_g1_receipt_sha256": formal_g1_sha,
        "environment_manifest_uri": workspace_uri(
            environment_manifest, workspace, "environment manifest"
        ),
        "environment_manifest_path": str(environment_manifest),
        "environment_manifest_sha256": environment_sha,
        "config_sha256": sha256_file(config_manifest),
        "code_sha256": sha256_file(runtime_manifest),
        "environment_sha256": environment_sha,
        "output_manifest_uri": workspace_uri(
            output_manifest, workspace, "7XL0 output manifest"
        ),
        "output_manifest_sha256": sha256_file(output_manifest),
        "completed_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    aiv0_handoff = {"aiv0_final_check_receipt_sha256": aiv0_sha}

    # A complete existing pair is a read-only replay, never a rewrite.
    if anchor_output.is_symlink() or receipt_output.is_symlink():
        raise ValueError("release outputs may not be symbolic links")
    if anchor_output.exists() and receipt_output.exists():
        existing_anchor: HeldReleaseFile | None = None
        existing_receipt: HeldReleaseFile | None = None
        try:
            existing_anchor = hold_existing_release_file(
                anchor_output, workspace, expected_sha256=anchor_sha
            )
            existing_receipt = hold_existing_release_file(
                receipt_output, workspace
            )
            verify_release_traceability(
                receipt_output,
                g1_receipt=g1_receipt,
                g1_receipt_sha=formal_g1_sha,
                environment_manifest=environment_manifest,
                environment_sha=environment_sha,
                workspace=workspace,
            )
            validate_release_with_frozen_aiv1(
                anchor_manifest_path=anchor_output,
                g2_receipt_path=receipt_output,
                input_contract=input_contract,
                aiv0_handoff=aiv0_handoff,
                repo_root=repo,
                workspace_root=workspace,
            )
            verify_held_release_file(existing_anchor, workspace)
            verify_held_release_file(existing_receipt, workspace)
            print("G2_ANCHOR_RELEASE_PASS existing_valid_release=true")
            return 0
        finally:
            close_held_release_file(existing_receipt)
            close_held_release_file(existing_anchor)
    if receipt_output.exists() or receipt_output.is_symlink():
        raise FileExistsError("G2 receipt exists without a valid complete release")

    anchor_temporary: Path | None = None
    receipt_temporary: Path | None = None
    held_anchor: HeldReleaseFile | None = None
    held_receipt: HeldReleaseFile | None = None
    try:
        if anchor_output.exists() or anchor_output.is_symlink():
            held_anchor = hold_existing_release_file(
                anchor_output, workspace, expected_sha256=anchor_sha
            )
            validation_anchor = anchor_output
        else:
            anchor_temporary = write_temporary(
                workspace, ".anchor_candidate_set_v1.", anchor_bytes
            )
            validation_anchor = anchor_temporary

        receipt_temporary = write_temporary(
            workspace,
            ".G2_anchor_release.receipt.",
            canonical_json(receipt_payload).encode("utf-8"),
        )
        # The frozen downstream gate validates exact-ten sets, five-sample NPZs,
        # canonical mmCIF sequence, checkpoint hashes, ./ manifests, platform,
        # G2 probes, artifact paths, and every receipt binding before publication.
        validate_release_with_frozen_aiv1(
            anchor_manifest_path=validation_anchor,
            g2_receipt_path=receipt_temporary,
            input_contract=input_contract,
            aiv0_handoff=aiv0_handoff,
            repo_root=repo,
            workspace_root=workspace,
        )

        if held_anchor is not None:
            verify_held_release_file(held_anchor, workspace)
        if anchor_temporary is not None:
            held_anchor = publish_no_replace_held(
                anchor_temporary, anchor_output, workspace
            )
            anchor_temporary = None
        if held_anchor is None:
            raise RuntimeError("anchor publication completed without a held inode")
        verify_held_release_file(held_anchor, workspace)

        # The two parents cannot be committed atomically. The immutable receipt
        # is deliberately published last; any failure below precisely removes
        # only entries whose held inode was created by this run.
        held_receipt = publish_no_replace_held(
            receipt_temporary, receipt_output, workspace
        )
        receipt_temporary = None
        validate_release_with_frozen_aiv1(
            anchor_manifest_path=anchor_output,
            g2_receipt_path=receipt_output,
            input_contract=input_contract,
            aiv0_handoff=aiv0_handoff,
            repo_root=repo,
            workspace_root=workspace,
        )
        verify_held_release_file(held_anchor, workspace)
        verify_held_release_file(held_receipt, workspace)
    except BaseException:
        if held_receipt is not None and held_receipt.created_by_run:
            unlink_held_if_current(held_receipt)
        if held_anchor is not None and held_anchor.created_by_run:
            unlink_held_if_current(held_anchor)
        raise
    finally:
        if anchor_temporary is not None:
            anchor_temporary.unlink(missing_ok=True)
        if receipt_temporary is not None:
            receipt_temporary.unlink(missing_ok=True)
        close_held_release_file(held_receipt)
        close_held_release_file(held_anchor)

    print("G2_ANCHOR_RELEASE_PASS candidate_count=10")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ContractViolation,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        csv.Error,
    ) as error:
        print(f"BLOCKED_G2_ANCHOR_RELEASE: {error}", file=sys.stderr)
        raise SystemExit(65) from error
