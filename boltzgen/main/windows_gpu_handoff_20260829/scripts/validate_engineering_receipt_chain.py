#!/usr/bin/env python3
"""Validate T0/Windows/T1 engineering receipts before the next GPU stage."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def regular(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an absolute regular non-symlink file: {path}")
    return path.resolve(strict=True)


def regular_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be an absolute regular non-symlink directory: {path}")
    return path.resolve(strict=True)


def load_receipt(path: Path, label: str) -> tuple[dict[str, Any], str]:
    path = regular(path, label)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is not a JSON object")
    return payload, digest(path)


def safe_member(root: Path, relative: str) -> Path:
    key = Path(relative)
    if key.is_absolute() or ".." in key.parts or key.name in {"", ".", ".."}:
        raise ValueError(f"unsafe evidence path: {relative!r}")
    path = root / key
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing evidence file: {relative}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"evidence path escapes attempt: {relative}") from exc
    return resolved


def verify_sha256sum_manifest(receipt_path: Path, payload: dict[str, Any]) -> None:
    root = receipt_path.parent
    manifest = regular(root / "outputs.SHA256SUMS", "outputs manifest")
    if digest(manifest) != payload.get("outputs_manifest_sha256"):
        raise ValueError("outputs manifest hash does not match receipt")
    verify_manifest_members(root, manifest)


def verify_manifest_members(root: Path, manifest: Path) -> None:
    rows = manifest.read_text(encoding="utf-8").splitlines()
    if not rows:
        raise ValueError("empty outputs manifest")
    observed: set[str] = set()
    for line in rows:
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError("malformed sha256sum manifest line") from exc
        relative = relative.lstrip("*")
        if relative in observed:
            raise ValueError(f"duplicate outputs manifest path: {relative}")
        observed.add(relative)
        path = safe_member(root, relative)
        if digest(path) != expected:
            raise ValueError(f"evidence SHA-256 mismatch: {relative}")


def verify_windows_manifest(receipt_path: Path, payload: dict[str, Any]) -> None:
    root = receipt_path.parent
    manifest = regular(root / "EVIDENCE.SHA256.tsv", "Windows evidence manifest")
    if digest(manifest) != payload.get("evidence_manifest_sha256"):
        raise ValueError("Windows evidence manifest hash does not match receipt")
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != ["sha256", "bytes", "name"]:
            raise ValueError(f"unexpected Windows evidence header: {reader.fieldnames}")
        rows = list(reader)
    if not rows:
        raise ValueError("empty Windows evidence manifest")
    observed: set[str] = set()
    for row in rows:
        relative = row["name"]
        if relative in observed:
            raise ValueError(f"duplicate Windows evidence path: {relative}")
        observed.add(relative)
        path = safe_member(root, relative)
        if path.stat().st_size != int(row["bytes"]) or digest(path) != row["sha256"]:
            raise ValueError(f"Windows evidence identity mismatch: {relative}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=("t1", "t2"))
    parser.add_argument("--t0-receipt", required=True, type=Path)
    parser.add_argument("--windows-receipt", required=True, type=Path)
    parser.add_argument("--handoff-root", required=True, type=Path)
    parser.add_argument("--wsl-probe-receipt", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    output = args.output
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("output must be a new file in an existing directory")

    t0_path = regular(args.t0_receipt, "T0 receipt")
    windows_path = regular(args.windows_receipt, "Windows receipt")
    t0, t0_sha256 = load_receipt(t0_path, "T0 receipt")
    windows, windows_sha256 = load_receipt(windows_path, "Windows receipt")
    if (
        t0.get("schema_version") != "T0_TRANSFER_ATTEMPT_RECEIPT_V1"
        or t0.get("status") != "TRANSFER_AND_SOURCE_VALIDATION_PASS"
        or t0.get("exit_code") != 0
    ):
        raise ValueError("T0 receipt is not a successful transfer attempt")
    verify_sha256sum_manifest(t0_path, t0)
    internal_receipt_path = regular(
        t0_path.parent / "internal_T0_RECEIPT.json", "internal T0 receipt"
    )
    internal_manifest = regular(
        t0_path.parent / "internal_T0_OUTPUTS.SHA256SUMS", "internal T0 manifest"
    )
    internal_receipt = json.loads(internal_receipt_path.read_text(encoding="utf-8"))
    if (
        internal_receipt.get("schema_version") != "WINDOWS_GPU_HANDOFF_T0_RECEIPT_V1"
        or internal_receipt.get("status") != "TRANSFER_AND_SOURCE_VALIDATION_PASS"
        or internal_receipt.get("exit_code") != 0
        or internal_receipt.get("expected_transfer_sha256")
        != t0.get("expected_transfer_sha256")
        or digest(internal_manifest) != internal_receipt.get("outputs_manifest_sha256")
    ):
        raise ValueError("internal T0 handoff receipt is not closed")
    handoff_root = regular_directory(args.handoff_root, "current handoff root")
    current_internal_receipt = regular(
        handoff_root / "T0_RECEIPT.json", "current handoff T0 receipt"
    )
    current_internal_manifest = regular(
        handoff_root / "T0_OUTPUTS.SHA256SUMS", "current handoff T0 manifest"
    )
    if (
        digest(current_internal_receipt) != digest(internal_receipt_path)
        or digest(current_internal_manifest) != digest(internal_manifest)
    ):
        raise ValueError("current handoff does not match the successful T0 attempt")
    verify_manifest_members(handoff_root, current_internal_manifest)
    if (
        windows.get("schema_version") != "WINDOWS_HOST_ENGINEERING_PROBE_RECEIPT_V1"
        or windows.get("status") != "WINDOWS_HOST_PROBE_PASS_NOT_G1"
        or windows.get("exit_code") != 0
    ):
        raise ValueError("Windows host receipt is not a successful engineering probe")
    verify_windows_manifest(windows_path, windows)

    result: dict[str, Any] = {
        "schema_version": "ENGINEERING_RECEIPT_CHAIN_VALIDATION_V1",
        "stage": args.stage,
        "status": "PASS",
        "t0_receipt_sha256": t0_sha256,
        "windows_receipt_sha256": windows_sha256,
        "handoff_t0_receipt_sha256": digest(current_internal_receipt),
        "handoff_t0_manifest_sha256": digest(current_internal_manifest),
    }
    if args.stage == "t2":
        if args.wsl_probe_receipt is None:
            raise ValueError("--wsl-probe-receipt is required for t2")
        wsl_path = regular(args.wsl_probe_receipt, "WSL probe receipt")
        wsl, wsl_sha256 = load_receipt(wsl_path, "WSL probe receipt")
        if (
            wsl.get("schema_version") != "WSL2_GPU_ENGINEERING_PROBE_RECEIPT_V1"
            or wsl.get("status") != "ENGINEERING_GPU_PROBE_PASS"
            or wsl.get("exit_code") != 0
        ):
            raise ValueError("WSL probe receipt is not successful")
        verify_sha256sum_manifest(wsl_path, wsl)
        if (
            wsl.get("t0_receipt_sha256") != t0_sha256
            or wsl.get("windows_receipt_sha256") != windows_sha256
        ):
            raise ValueError("WSL probe does not bind the supplied predecessor receipts")
        result["wsl_probe_receipt_sha256"] = wsl_sha256
    elif args.wsl_probe_receipt is not None:
        raise ValueError("--wsl-probe-receipt is only valid for t2")

    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"ENGINEERING_RECEIPT_CHAIN_PASS stage={args.stage}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BLOCKED_ENGINEERING_RECEIPT_CHAIN: {exc}", file=sys.stderr)
        raise SystemExit(65) from exc
