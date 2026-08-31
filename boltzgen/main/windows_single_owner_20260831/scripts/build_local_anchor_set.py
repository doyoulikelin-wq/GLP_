#!/usr/bin/env python3
"""Build a self-contained Windows-owner local anchor set from one T8 run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import traceback
from pathlib import Path

import numpy as np


MANIFEST_ROW = re.compile(r"([0-9a-f]{64})  (?:\./)?([^\x00\r\n]+)")
SPEC_MEMBERS = {"design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--anchor-count", type=int, default=3)
    return parser.parse_args()


def stable_digest(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"symlink is not allowed: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"regular file required: {path}")
        value = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            value.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise ValueError(f"file changed while hashing: {path}")
    return value.hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_json(path: Path, payload: dict) -> None:
    atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


def write_text(path: Path, value: str) -> None:
    atomic_write(path, value.encode("utf-8"))


def parse_manifest(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = MANIFEST_ROW.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid manifest row in {path}: {line!r}")
        relative = Path(match.group(2))
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in rows:
            raise ValueError(f"unsafe or duplicate manifest path: {relative}")
        rows[relative.as_posix()] = match.group(1)
    if not rows:
        raise ValueError(f"empty manifest: {path}")
    return rows


def regular_file_closure(root: Path, excluded: set[str]) -> set[str]:
    observed: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"unsafe tree member: {relative}")
        observed.add(relative)
    return observed


def verify_manifest(root: Path, manifest: Path, excluded: set[str]) -> dict[str, str]:
    rows = parse_manifest(manifest)
    observed = regular_file_closure(root, excluded)
    if set(rows) != observed:
        raise ValueError(
            f"manifest closure mismatch: missing={sorted(observed - set(rows))} "
            f"unexpected={sorted(set(rows) - observed)}"
        )
    for relative, expected in rows.items():
        actual = stable_digest(root / relative)
        if actual != expected:
            raise ValueError(f"manifest digest mismatch: {relative}")
    return rows


def copy_verified(source: Path, destination: Path, expected: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination already exists: {destination}")
    with source.open("rb") as input_stream:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    actual = stable_digest(destination)
    if actual != expected:
        raise ValueError(f"copied digest mismatch: {destination}")
    return actual


def as_bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def builder_code_identity() -> dict[str, object]:
    repo = Path(__file__).resolve().parents[4]

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    status = git("status", "--short")
    return {
        "repo_root": str(repo),
        "repo_commit": git("rev-parse", "HEAD"),
        "repo_tree": git("rev-parse", "HEAD^{tree}"),
        "worktree_clean": not bool(status),
        "builder_path": str(Path(__file__).resolve()),
        "builder_sha256": stable_digest(Path(__file__).resolve()),
    }


def selected_metric(row: dict[str, str]) -> dict:
    numeric_float = {
        "filter_rmsd",
        "filter_rmsd_design",
        "bindsite_under_8rmsd",
        "design_to_target_iptm",
        "design_ptm",
        "min_design_to_target_pae",
        "quality_score",
    }
    numeric_int = {"final_rank", "plip_hbonds_refolded", "liability_score"}
    wanted = {
        "id",
        "designed_sequence",
        "designed_chain_sequence",
        "pass_filters",
        "pass_filter_rmsd_filter",
        "pass_filter_rmsd_design_filter",
        "pass_bindsite_under_8rmsd_filter",
        *numeric_float,
        *numeric_int,
    }
    payload: dict[str, object] = {}
    for key in wanted:
        value = row.get(key)
        if value in (None, ""):
            payload[key] = None
        elif key.startswith("pass_"):
            payload[key] = as_bool(value)
        elif key in numeric_float:
            payload[key] = float(value)
        elif key in numeric_int:
            payload[key] = int(float(value))
        else:
            payload[key] = value
    payload["unmet_filters"] = sorted(
        key for key, value in row.items()
        if key.startswith("pass_") and key.endswith("_filter") and not as_bool(value)
    )
    payload["designed_sequence_length"] = len(row.get("designed_sequence", ""))
    payload["designed_chain_sequence_length"] = len(
        row.get("designed_chain_sequence", "")
    )
    return payload


def verify_npz(path: Path, expected_folds: int | None = None) -> None:
    with np.load(path, allow_pickle=False) as archive:
        if not archive.files:
            raise ValueError(f"empty NPZ archive: {path}")
        arrays = {name: archive[name] for name in archive.files}
    for name, array in arrays.items():
        if array.dtype.hasobject:
            raise ValueError(f"object array is not allowed: {path}:{name}")
        if np.issubdtype(array.dtype, np.inexact) and not np.isfinite(array).all():
            raise ValueError(f"NaN or Inf in anchor NPZ: {path}:{name}")
    if expected_folds is not None:
        coords = arrays.get("coords")
        if coords is None or coords.ndim != 3 or coords.shape[0] != expected_folds:
            raise ValueError(f"anchor NPZ does not contain {expected_folds} folds: {path}")


def seal_output(root: Path) -> None:
    manifest = root / "SHA256SUMS"
    temporary = root / ".SHA256SUMS.tmp"
    for path in (manifest, temporary):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    observed = regular_file_closure(root, {"SHA256SUMS", ".SHA256SUMS.tmp"})
    records = [(relative, stable_digest(root / relative)) for relative in observed]
    records.sort(key=lambda item: item[0].encode("utf-8"))
    content = "".join(f"{digest}  ./{relative}\n" for relative, digest in records).encode()
    atomic_write(temporary, content)
    rows = parse_manifest(temporary)
    if set(rows) != observed:
        raise ValueError("output manifest closure changed before publication")
    for relative, expected in rows.items():
        if stable_digest(root / relative) != expected:
            raise ValueError(f"output changed before manifest publication: {relative}")
    os.replace(temporary, manifest)


def main() -> int:
    args = parse_args()
    if not 1 <= args.anchor_count <= 10:
        raise SystemExit("--anchor-count must be from 1 through 10")
    source = args.source_run.resolve(strict=True)
    if source.is_symlink() or not source.is_dir():
        raise SystemExit("source run must be a regular directory")
    requested_output = args.output.absolute()
    requested_output.parent.mkdir(parents=True, exist_ok=True)
    if requested_output.exists() or requested_output.is_symlink():
        raise SystemExit(f"output already exists: {requested_output}")
    output = requested_output.with_name(
        f".{requested_output.name}.BUILDING_{os.getpid()}"
    )
    if output.exists() or output.is_symlink():
        raise SystemExit(f"staging output already exists: {output}")
    output.mkdir(mode=0o700)
    write_text(output / "STATUS.txt", "LOCAL_ANCHOR_SET_BUILDING\n")

    try:
        source_manifest_path = source / "operator_logs/OUTPUT_SHA256SUMS"
        source_rows = verify_manifest(
            source,
            source_manifest_path,
            {"operator_logs/OUTPUT_SHA256SUMS"},
        )
        receipt_path = source / "operator_logs/EXPLORATORY_INFERENCE.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("status") != "EXPLORATORY_INFERENCE_COMPLETE"
            or receipt.get("exit_code") != 0
            or receipt.get("output_validation", {}).get("status") != "PASS"
            or receipt.get("cuda_oom_detected") is not False
        ):
            raise ValueError("source T8 receipt is not a complete validated run")
        denominator = int(receipt["observed_designs"])
        if denominator != int(receipt["expected_designs"]) or args.anchor_count > denominator:
            raise ValueError("source denominator does not support requested anchor count")

        metrics_relative = "final_ranked_designs/all_designs_metrics.csv"
        metrics_path = source / metrics_relative
        if metrics_relative not in source_rows:
            raise ValueError("source manifest does not bind the metrics table")
        with metrics_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != denominator or len({row.get("id") for row in rows}) != denominator:
            raise ValueError("metrics denominator or candidate IDs do not match receipt")
        designed_sequences = [row.get("designed_sequence", "") for row in rows]
        chain_sequences = [row.get("designed_chain_sequence", "") for row in rows]
        if (
            any(not value for value in designed_sequences + chain_sequences)
            or len(set(designed_sequences)) != denominator
            or len(set(chain_sequences)) != denominator
            or any(len(full) <= len(design) for design, full in zip(designed_sequences, chain_sequences))
        ):
            raise ValueError("candidate sequence denominator is empty, duplicate, or truncated")
        ranks = [int(row["final_rank"]) for row in rows]
        if sorted(ranks) != list(range(1, denominator + 1)):
            raise ValueError("final ranks are not a complete unique denominator")
        selected = sorted(rows, key=lambda row: int(row["final_rank"]))[: args.anchor_count]

        checks = receipt.get("resolved_config_contract", {}).get("checks", {})
        yaml_paths = checks.get("design.yaml_path")
        if not isinstance(yaml_paths, list) or len(yaml_paths) != 1:
            raise ValueError("source receipt does not bind exactly one spec YAML")
        spec_root = Path(yaml_paths[0]).resolve(strict=True).parent
        spec_manifest_path = source / "operator_logs/spec_bundle_before.SHA256SUMS"
        spec_rows = verify_manifest(spec_root, spec_manifest_path, set())
        if set(spec_rows) != SPEC_MEMBERS:
            raise ValueError("source spec bundle is not the canonical four-file closure")

        source_evidence = output / "source_evidence"
        source_evidence.mkdir(mode=0o700)
        evidence_files = {
            "EXPLORATORY_INFERENCE.json": "operator_logs/EXPLORATORY_INFERENCE.json",
            "cell_contract.json": "operator_logs/cell_contract.json",
            "resolved_config_contract.json": "operator_logs/resolved_config_contract.json",
            "runtime_assets_used.SHA256SUMS": "operator_logs/runtime_assets_used.SHA256SUMS",
            "LOCAL_ENV_ACCEPTANCE.json": "operator_logs/LOCAL_ENV_ACCEPTANCE.json",
            "source_commit.txt": "operator_logs/source_commit.txt",
            "source_tree.txt": "operator_logs/source_tree.txt",
            "spec_bundle_before.SHA256SUMS": "operator_logs/spec_bundle_before.SHA256SUMS",
            "all_designs_metrics.csv": metrics_relative,
            "final_designs_metrics.csv": (
                f"final_ranked_designs/final_designs_metrics_{denominator}.csv"
            ),
            "aggregate_metrics_analyze.csv": (
                "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv"
            ),
        }
        for destination_name, relative in evidence_files.items():
            expected = source_rows.get(relative)
            if expected is None:
                raise ValueError(f"source manifest does not bind evidence file: {relative}")
            copy_verified(source / relative, source_evidence / destination_name, expected)
        copy_verified(
            source_manifest_path,
            source_evidence / "OUTPUT_SHA256SUMS",
            stable_digest(source_manifest_path),
        )
        spec_output = output / "inputs/spec_bundle"
        for name in sorted(SPEC_MEMBERS):
            copy_verified(spec_root / name, spec_output / name, spec_rows[name])

        anchors: list[dict] = []
        for row in selected:
            candidate_id = row["id"]
            rank = int(row["final_rank"])
            source_files = {
                "raw_design.cif": f"intermediate_designs/{candidate_id}.cif",
                "raw_design_metadata.npz": f"intermediate_designs/{candidate_id}.npz",
                "inverse_folded.cif": (
                    f"intermediate_designs_inverse_folded/{candidate_id}.cif"
                ),
                "inverse_metadata.npz": f"intermediate_designs_inverse_folded/{candidate_id}.npz",
                "refold_samples.npz": (
                    f"intermediate_designs_inverse_folded/fold_out_npz/{candidate_id}.npz"
                ),
                "refolded.cif": (
                    f"intermediate_designs_inverse_folded/refold_cif/{candidate_id}.cif"
                ),
            }
            anchor_root = output / f"anchors/rank{rank:02d}_{candidate_id}"
            copied: dict[str, dict[str, object]] = {}
            for destination_name, relative in source_files.items():
                expected = source_rows.get(relative)
                if expected is None:
                    raise ValueError(f"source manifest does not bind anchor file: {relative}")
                destination = anchor_root / destination_name
                copied[destination_name] = {
                    "sha256": copy_verified(source / relative, destination, expected),
                    "size_bytes": destination.stat().st_size,
                    "source_relative_path": relative,
                }
                if destination.suffix == ".npz":
                    verify_npz(
                        destination,
                        expected_folds=5 if destination_name == "refold_samples.npz" else None,
                    )
            metric = selected_metric(row)
            anchors.append(
                {
                    "candidate_id": candidate_id,
                    "final_rank": rank,
                    "selected_despite_strict_filter_failure": not as_bool(row["pass_filters"]),
                    "metrics": metric,
                    "files": copied,
                }
            )

        anchor_set = {
            "schema_version": "WINDOWS_OWNER_LOCAL_ANCHOR_SET_V1",
            "status": "LOCAL_ANCHOR_SET_READY",
            "authority": "WINDOWS_CODEX",
            "mac_review_required": False,
            "formal_gate_claimed": False,
            "training_performed": False,
            "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
            "source_run": str(source),
            "source_receipt_sha256": stable_digest(receipt_path),
            "source_output_manifest_sha256": stable_digest(source_manifest_path),
            "builder_code_identity": builder_code_identity(),
            "selection": {
                "scope": "DEVELOPMENT_ONLY",
                "rule": (
                    "ascending integer final_rank from one canonical T8 run; "
                    "UTF-8 candidate ID is the deterministic future tie-break"
                ),
                "denominator": denominator,
                "selected_count": len(anchors),
                "source_strict_filter_pass_count": sum(
                    as_bool(row.get("pass_filters")) for row in rows
                ),
                "strict_filter_qualified_anchor_count": sum(
                    not anchor["selected_despite_strict_filter_failure"]
                    for anchor in anchors
                ),
                "nonpass_anchor_count": sum(
                    anchor["selected_despite_strict_filter_failure"] for anchor in anchors
                ),
                "eligibility_requires_pass_filters": False,
                "strict_filter_status_preserved": True,
                "interpretation": (
                    "Failed filters remain explicit; selection permits local multi-state AI "
                    "evaluation but is not a binding or experimental success claim."
                ),
            },
            "anchors": anchors,
        }
        write_json(output / "ANCHOR_SET.json", anchor_set)
        write_text(output / "STATUS.txt", "LOCAL_ANCHOR_SET_READY\n")
        seal_output(output)
        os.replace(output, requested_output)
    except BaseException as exc:
        write_text(output / "STATUS.txt", "LOCAL_ANCHOR_SET_FAILED\n")
        write_json(
            output / "ERROR.json",
            {
                "status": "LOCAL_ANCHOR_SET_FAILED",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        try:
            seal_output(output)
        except BaseException:
            pass
        failed_output = requested_output.with_name(
            f"{requested_output.name}.FAILED_{os.getpid()}"
        )
        if output.exists() and not failed_output.exists():
            os.replace(output, failed_output)
        raise

    print(f"LOCAL_ANCHOR_SET_READY path={requested_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
