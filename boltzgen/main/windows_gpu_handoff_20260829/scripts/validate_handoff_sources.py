#!/usr/bin/env python3
"""Validate the exact open GPU handoff sources and emit compact inventories."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path


BUNDLE_SCHEMA = "WINDOWS_CODEX_GPU_HANDOFF_SOURCE_VALIDATION_V1"
EXPECTED_RUNTIME_BYTES = 6_352_944_053
EXPECTED_RUNTIME_NAMES = {
    "boltzgen1_diverse.ckpt",
    "boltzgen1_adherence.ckpt",
    "boltzgen1_ifold.ckpt",
    "boltz2_conf_final.ckpt",
    "mols.zip",
}
FORBIDDEN_MARKERS = ("LOCKBOX", "GIP", "2B4N", "6LMK", "GLUCAGON")


def sha256(path: Path) -> str:
    """Return a file's SHA-256 digest."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_regular_file(path: Path) -> None:
    """Require a non-symlink regular file."""

    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"required regular file missing or symlinked: {path}")


def canonical_source(workspace: Path, relative_path: str) -> Path:
    """Map a contract compatibility path to its canonical source path."""

    mappings = (
        (
            "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/",
            "boltzgen/runs/old12_glp1_mac_enhanced_20260820/",
        ),
        (
            "data/样本数据/binding-多构象/",
            "shared/data/glp1_positive_conformer_ensemble_20260819/",
        ),
        (
            "data/not_binding/",
            "shared/data/glp2_tuning_countertargets_20260824/",
        ),
    )
    for compatibility_prefix, canonical_prefix in mappings:
        if relative_path.startswith(compatibility_prefix):
            suffix = relative_path[len(compatibility_prefix) :]
            return workspace / canonical_prefix / suffix
    raise SystemExit(f"development state uses an unapproved path: {relative_path}")


def validate_runtime(workspace: Path, output: Path) -> dict[str, object]:
    """Validate the five frozen runtime assets against their original manifest."""

    runtime = (
        workspace
        / "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
    )
    manifest = runtime / "SHA256SUMS"
    require_regular_file(manifest)
    rows: list[dict[str, object]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, name = line.split(None, 1)
        name = name.lstrip("*")
        path = runtime / name
        require_regular_file(path)
        observed = sha256(path)
        if observed != expected:
            raise SystemExit(f"runtime SHA-256 mismatch: {path}")
        rows.append(
            {
                "relative_path": path.relative_to(workspace).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": observed,
            }
        )
    names = {Path(str(row["relative_path"])).name for row in rows}
    total = sum(int(row["size_bytes"]) for row in rows)
    if names != EXPECTED_RUNTIME_NAMES or total != EXPECTED_RUNTIME_BYTES:
        raise SystemExit(
            f"runtime identity mismatch: names={sorted(names)!r}, total={total}"
        )
    write_tsv(output / "runtime_assets.tsv", rows)
    return {"file_count": len(rows), "total_bytes": total, "status": "PASS"}


def validate_development_states(
    workspace: Path, repo: Path, output: Path
) -> dict[str, object]:
    """Validate exactly the 16 open AIV1 development-state structures."""

    contract = (
        repo
        / "boltzgen/resources/data/AIV1技术门合同_20260828/development_state_contract.tsv"
    )
    require_regular_file(contract)
    rows_out: list[dict[str, object]] = []
    with contract.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if len(rows) != 16:
        raise SystemExit(f"expected 16 development states, observed {len(rows)}")
    for expected_order, row in enumerate(rows):
        if int(row["state_order"]) != expected_order:
            raise SystemExit("development state order is not contiguous")
        relative_path = row["relative_path"]
        marker_text = " ".join(
            (
                relative_path,
                row["target_state_id"],
                row["source_id"],
                row["cohort_id"],
            )
        ).upper()
        if any(marker in marker_text for marker in FORBIDDEN_MARKERS):
            raise SystemExit(f"forbidden lockbox marker in development state: {row}")
        path = canonical_source(workspace, relative_path)
        require_regular_file(path)
        observed = sha256(path)
        if observed != row["sha256"]:
            raise SystemExit(f"development-state SHA-256 mismatch: {path}")
        rows_out.append(
            {
                "state_order": expected_order,
                "target_state_id": row["target_state_id"],
                "contract_relative_path": relative_path,
                "canonical_relative_path": path.relative_to(workspace).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": observed,
            }
        )
    write_tsv(output / "aiv1_development_states.tsv", rows_out)
    return {
        "file_count": len(rows_out),
        "total_bytes": sum(int(row["size_bytes"]) for row in rows_out),
        "status": "PASS",
    }


def validate_scaffolds(workspace: Path, output: Path) -> dict[str, object]:
    """Validate the 12 selected baseline scaffold packages."""

    scaffold_root = workspace / "boltzgen/data/vhh_scaffold_database_20260819"
    selected = scaffold_root / "selected"
    registry = scaffold_root / "registry/export_artifacts.tsv"
    require_regular_file(registry)
    with registry.open("r", encoding="utf-8", newline="") as handle:
        registry_rows = list(csv.DictReader(handle, delimiter="\t"))
    if len(registry_rows) != 12:
        raise SystemExit(
            f"expected 12 scaffold registry rows, observed {len(registry_rows)}"
        )
    registry_by_package = {
        Path(row["normalized_cif_path"]).parent.name: row for row in registry_rows
    }
    if len(registry_by_package) != 12:
        raise SystemExit("scaffold registry package IDs are not unique")
    packages = sorted(path for path in selected.iterdir() if path.is_dir())
    if len(packages) != 12:
        raise SystemExit(f"expected 12 scaffold packages, observed {len(packages)}")
    if {path.name for path in packages} != set(registry_by_package):
        raise SystemExit("selected package set does not match export registry")
    rows: list[dict[str, object]] = []
    for package in packages:
        scaffold = package / "scaffold.cif"
        spec = package / "scaffold.yaml"
        require_regular_file(scaffold)
        require_regular_file(spec)
        expected = registry_by_package[package.name]
        observed_scaffold_sha = sha256(scaffold)
        observed_spec_sha = sha256(spec)
        if expected["boltzgen_check_status"] != "PASS":
            raise SystemExit(f"scaffold registry is not PASS: {package.name}")
        if (
            expected["normalized_cif_path"]
            != f"selected/{package.name}/scaffold.cif"
            or expected["scaffold_yaml_path"]
            != f"selected/{package.name}/scaffold.yaml"
            or expected["normalized_cif_sha256"] != observed_scaffold_sha
            or expected["scaffold_yaml_sha256"] != observed_spec_sha
        ):
            raise SystemExit(f"scaffold registry identity mismatch: {package.name}")
        rows.append(
            {
                "package_id": package.name,
                "candidate_id": expected["candidate_id"],
                "scaffold_relative_path": scaffold.relative_to(workspace).as_posix(),
                "scaffold_size_bytes": scaffold.stat().st_size,
                "scaffold_sha256": observed_scaffold_sha,
                "spec_relative_path": spec.relative_to(workspace).as_posix(),
                "spec_size_bytes": spec.stat().st_size,
                "spec_sha256": observed_spec_sha,
            }
        )
    write_tsv(output / "selected_scaffolds.tsv", rows)
    return {"package_count": len(rows), "status": "PASS"}


def validate_lockbox_identity_metadata(
    workspace: Path, output: Path
) -> dict[str, object]:
    """Freeze the 25 known lockbox-file hashes as a payload byte denylist.

    This reads only AIV0 inventory metadata. It deliberately does not resolve or
    open any sealed structure path.
    """

    inventory = (
        workspace
        / "boltzgen/data/ai_structure_asset_validation_registry_20260828_211504"
        / "source_file_inventory.tsv"
    )
    require_regular_file(inventory)
    with inventory.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    lockbox_rows = []
    for row in rows:
        relative = row["relative_path"]
        if (
            "/GIP_1_42/" in relative
            or "/glucagon_1_29/" in relative
            or relative.endswith("/原始文件/2B4N.cif")
            or relative.endswith("/原始文件/6LMK.cif")
        ):
            lockbox_rows.append(row)
    if len(lockbox_rows) != 25:
        raise SystemExit(
            f"expected 25 lockbox identity-metadata rows, observed {len(lockbox_rows)}"
        )
    digests = sorted(row["sha256"] for row in lockbox_rows)
    if len(set(digests)) != 25 or any(
        len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        for value in digests
    ):
        raise SystemExit("lockbox metadata SHA-256 set is malformed or duplicated")
    if any(row["suffix"].lower() != ".cif" for row in lockbox_rows):
        raise SystemExit("lockbox structure denylist contains a non-CIF file")
    write_tsv(
        output / "lockbox_structure_sha256_denylist.tsv",
        [{"sha256": value} for value in digests],
    )
    return {
        "identity_metadata_row_count": len(lockbox_rows),
        "structure_files_included": False,
        "candidate_results_included": False,
        "access_count": 0,
        "status": "PASS",
    }


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write deterministic UTF-8 TSV rows."""

    if not rows:
        raise SystemExit(f"refusing to write empty inventory: {path}")
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    """Run all handoff source checks."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    workspace = args.workspace_root.resolve(strict=True)
    repo = args.repo_root.resolve(strict=True)
    output = args.output_dir
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    output.mkdir(parents=True)

    if repo.parent != workspace or repo.name != "GLP_":
        raise SystemExit("repo root must be <workspace>/GLP_")
    if (repo / "private").exists():
        raise SystemExit("private data must not be stored inside the project repository")

    result = {
        "schema_version": BUNDLE_SCHEMA,
        "runtime_assets": validate_runtime(workspace, output),
        "aiv1_development_states": validate_development_states(
            workspace, repo, output
        ),
        "selected_scaffolds": validate_scaffolds(workspace, output),
        "lockbox": validate_lockbox_identity_metadata(workspace, output),
        "lockbox_identity_metadata_included": True,
        "formal_g1_status": "NOT_RUN",
        "formal_g2_status": "NOT_RUN",
        "formal_aiv1_status": "NOT_RUN",
        "status": "PASS",
    }
    (output / "source_validation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
