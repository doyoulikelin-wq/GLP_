#!/usr/bin/env python3
"""Append one receipt-bound Windows engineering event to a staging JSONL registry."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "ENGINEERING_EXPERIENCE_EVENT_V1"
EXPECTED_KEYS = {
    "schema_version",
    "event_id",
    "operation_id",
    "task_id",
    "attempt_id",
    "created_at_utc",
    "agent_role",
    "review_state",
    "outcome",
    "terminal_status",
    "failure_codes",
    "summary_zh",
    "lessons_zh",
    "next_adjustments_zh",
    "source_receipt_uri",
    "source_receipt_sha256",
    "code_identity",
    "input_manifest_sha256",
    "environment_manifest_sha256",
    "output_manifest_sha256",
    "supersedes_event_id",
    "formal_gate_claimed",
    "training_performed",
    "model_weights_modified",
    "lockbox_access_count",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
TASK_ID = re.compile(r"^T(?:-1|[0-9]|1[01])$")
ATTEMPT_ID = re.compile(r"^attempt_[0-9]{3}$")
TERMINAL_STATUS = re.compile(r"^[A-Z][A-Z0-9_:-]{2,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_URI = re.compile(r"^(?:workspace|windows|wsl)://.+")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def require_regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute normalized path")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing regular non-symlink file: {path}")
    return path.resolve(strict=True)


def require_registry_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.suffix != ".jsonl":
        raise ValueError("registry must be an absolute normalized .jsonl path")
    parent = path.parent.resolve(strict=True)
    home = Path("/home").resolve(strict=True)
    try:
        relative = parent.relative_to(home)
    except ValueError as exc:
        raise ValueError("registry parent must resolve below /home") from exc
    if len(relative.parts) < 2:
        raise ValueError("registry parent must be below /home/<user>")
    resolved = parent / path.name
    if resolved.is_symlink() or (resolved.exists() and not resolved.is_file()):
        raise ValueError("existing registry must be a regular non-symlink file")
    return resolved


def require_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be non-empty text no longer than {maximum}")
    return value


def require_string_list(value: Any, label: str, minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{label} must be a list with at least {minimum} item(s)")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in value):
        raise ValueError(f"{label} contains an invalid item")
    return value


def validate_event(event: Any, source_receipt_sha256: str) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("event JSON must be an object")
    observed = set(event)
    if observed != EXPECTED_KEYS:
        raise ValueError(
            f"event fields mismatch; missing={sorted(EXPECTED_KEYS - observed)}; "
            f"extra={sorted(observed - EXPECTED_KEYS)}"
        )
    if event["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    for label, pattern in (
        ("event_id", SAFE_ID),
        ("operation_id", OPERATION_ID),
        ("task_id", TASK_ID),
        ("attempt_id", ATTEMPT_ID),
        ("terminal_status", TERMINAL_STATUS),
    ):
        if not isinstance(event[label], str) or not pattern.fullmatch(event[label]):
            raise ValueError(f"invalid {label}: {event[label]!r}")
    if event["agent_role"] != "WINDOWS_EXECUTOR":
        raise ValueError("engineering staging events must use agent_role=WINDOWS_EXECUTOR")
    if event["review_state"] != "PENDING_MAC_REVIEW":
        raise ValueError("Windows cannot issue a reviewed experience decision")
    if event["outcome"] not in {"SUCCESS", "FAILURE", "BLOCKED"}:
        raise ValueError("outcome must be SUCCESS, FAILURE, or BLOCKED")
    failure_codes = require_string_list(event["failure_codes"], "failure_codes")
    if len(set(failure_codes)) != len(failure_codes):
        raise ValueError("failure_codes must be unique")
    if any(not TERMINAL_STATUS.fullmatch(item) for item in failure_codes):
        raise ValueError("failure_codes contains an invalid code")
    if event["outcome"] == "SUCCESS" and failure_codes:
        raise ValueError("SUCCESS must have an empty failure_codes list")
    if event["outcome"] != "SUCCESS" and not failure_codes:
        raise ValueError("FAILURE/BLOCKED must include at least one failure code")
    require_text(event["summary_zh"], "summary_zh", 2000)
    require_string_list(event["lessons_zh"], "lessons_zh", minimum=1)
    require_string_list(event["next_adjustments_zh"], "next_adjustments_zh")
    if not isinstance(event["source_receipt_uri"], str) or not SOURCE_URI.fullmatch(
        event["source_receipt_uri"]
    ):
        raise ValueError("source_receipt_uri must use workspace://, windows://, or wsl://")
    if event["source_receipt_sha256"] != source_receipt_sha256:
        raise ValueError("source_receipt_sha256 does not match --source-receipt bytes")
    require_text(event["code_identity"], "code_identity", 256)
    for label in (
        "source_receipt_sha256",
        "input_manifest_sha256",
        "environment_manifest_sha256",
        "output_manifest_sha256",
    ):
        value = event[label]
        if value is not None and (not isinstance(value, str) or not SHA256.fullmatch(value)):
            raise ValueError(f"{label} must be null or a lowercase SHA-256")
    supersedes = event["supersedes_event_id"]
    if supersedes is not None and (not isinstance(supersedes, str) or not SAFE_ID.fullmatch(supersedes)):
        raise ValueError("invalid supersedes_event_id")
    try:
        timestamp = dt.datetime.strptime(event["created_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise ValueError("created_at_utc must be a real UTC second timestamp") from exc
    if timestamp.year < 2026:
        raise ValueError("created_at_utc is outside the project time range")
    for label, expected in (
        ("formal_gate_claimed", False),
        ("training_performed", False),
        ("model_weights_modified", False),
        ("lockbox_access_count", 0),
    ):
        if type(event[label]) is not type(expected) or event[label] != expected:
            raise ValueError(f"{label} must equal {expected!r}")
    return event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--event", required=True, type=Path)
    parser.add_argument("--source-receipt", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    event_path = require_regular_file(args.event, "event")
    source_receipt = require_regular_file(args.source_receipt, "source receipt")
    registry = require_registry_path(args.registry)
    schema = require_regular_file(
        Path(__file__).resolve(strict=True).parent.parent / "ENGINEERING_EXPERIENCE_EVENT_SCHEMA.json",
        "event schema",
    )
    event = validate_event(
        json.loads(event_path.read_text(encoding="utf-8")), digest(source_receipt)
    )
    canonical_line = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    canonical_bytes = canonical_line.encode("utf-8")

    receipt_dir = registry.parent / "append_receipts"
    if receipt_dir.is_symlink() or (receipt_dir.exists() and not receipt_dir.is_dir()):
        raise ValueError("append_receipts must be a regular directory")
    receipt_dir.mkdir(mode=0o750, exist_ok=True)
    append_receipt = receipt_dir / f"{event['event_id']}.json"
    if append_receipt.exists() or append_receipt.is_symlink():
        raise ValueError(f"append receipt already exists: {append_receipt}")

    lock_path = registry.with_suffix(registry.suffix + ".lock")
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError("registry lock must be a regular file")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    with os.fdopen(lock_fd, "r+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        before_sha256 = (
            digest(registry) if registry.exists() else hashlib.sha256(b"").hexdigest()
        )
        if registry.exists():
            with registry.open("r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"invalid registry JSON at line {number}") from exc
                    if existing.get("event_id") == event["event_id"]:
                        raise ValueError(f"duplicate event_id: {event['event_id']}")
        fd = os.open(registry, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o640)
        with os.fdopen(fd, "ab") as registry_handle:
            registry_handle.write(canonical_bytes)
            registry_handle.flush()
            os.fsync(registry_handle.fileno())
        after_sha256 = digest(registry)

        append_payload = {
            "schema_version": "ENGINEERING_EXPERIENCE_APPEND_RECEIPT_V1",
            "status": "ENGINEERING_EXPERIENCE_STAGED_PENDING_MAC_REVIEW",
            "event_id": event["event_id"],
            "event_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
            "source_receipt_sha256": event["source_receipt_sha256"],
            "schema_sha256": digest(schema),
            "registry_sha256_before": before_sha256,
            "registry_sha256_after": after_sha256,
            "authoritative_experience_registry_updated": False,
        }
        with append_receipt.open("x", encoding="utf-8") as handle:
            json.dump(append_payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(append_receipt, 0o440)
    print(
        "ENGINEERING_EXPERIENCE_STAGED_PENDING_MAC_REVIEW "
        f"event_id={event['event_id']} registry={registry}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BLOCKED_ENGINEERING_EXPERIENCE_APPEND: {exc}", file=sys.stderr)
        raise SystemExit(65) from exc
