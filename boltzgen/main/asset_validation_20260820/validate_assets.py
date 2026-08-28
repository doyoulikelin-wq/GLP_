#!/usr/bin/env python3
"""Validate and inventory the local BoltzGen AI-validation assets.

The 2026-08-26 workspace migration moved large assets out of the Git repository
and retained compatibility symlinks.  This validator therefore keeps two path
identities for every asset: a stable *logical* path used by registries and a
canonical *physical* path used only for reads and hashing.  It never derives a
logical identity by resolving a symlink.

The script never edits source data. ``--write`` atomically refreshes only the
derived TSV/JSON files in an explicitly selected registry directory; ``--check``
compares a fresh in-memory render with those files.  Neither mode performs model
training or claims experimental binding truth.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import gemmi
import yaml


SCHEMA_VERSION = "AI_VALIDATION_ASSET_REGISTRY_V2"
HISTORICAL_SOURCE_FILE_COUNT = 177
HISTORICAL_SOURCE_BYTES = 14_884_156
EXPECTED_STRUCTURE_PATH_COUNT = 112
EXPECTED_STRUCTURE_PARSE_PASS = 112
EXPECTED_ASSET_MOUNT_COUNT = 10
EXPECTED_COHORT_COUNT = 13
EXPECTED_FILE_OVERRIDE_COUNT = 18
EXPECTED_COMPATIBILITY_ALIAS_COUNT = 9
EXPECTED_HISTORICAL_OUTPUT_HASH_COUNT = 5
EXPECTED_NEW_SCAFFOLD_CHECKSUM_COUNT = 72
ACTIVE_STATUSES = {
    "USE_PRIMARY",
    "USE_SENSITIVITY",
    "USE_POSITIVE_FIXED_CONTROL",
    "USE_POSITIVE_COMPACT",
    "USE_TUNING_CHALLENGE",
    "USE_LOCKBOX_CHALLENGE",
    "USE_SCAFFOLD_ADMISSION_PROBE",
    "USE_BASELINE_SCAFFOLD",
}
PEPTIDE_CLASSES = {
    "peptide_target",
    "peptide_target_ensemble",
    "peptide_target_representatives",
    "peptide_challenge",
    "peptide_challenge_ensemble",
    "peptide_challenge_quarantine",
    "duplicate_alias",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AssetRef:
    """One stable logical asset identity backed by one physical file."""

    logical_relative_path: str
    physical_path: Path


@dataclass(frozen=True)
class AssetMount:
    """A declared mapping from a logical path prefix to a canonical asset."""

    mount_id: str
    logical_path: str
    canonical_uri: str
    asset_kind: str
    inventory_scope: str
    include_in_source_inventory: bool
    expected_file_count: int
    expected_bytes: int


def normalize_logical_path(value: str) -> str:
    """Return a canonical POSIX relative path without touching the filesystem."""

    if not value or "\\" in value:
        raise ValueError(f"invalid logical path: {value!r}")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"logical path must be a normalized relative POSIX path: {value!r}")
    normalized = candidate.as_posix()
    if normalized != value:
        raise ValueError(f"logical path is not canonical: {value!r}")
    return normalized


def workspace_uri_to_lexical_path(uri: str, workspace_root: Path) -> Path:
    """Map ``workspace://`` to a lexical path without following symlinks."""

    prefix = "workspace://"
    if not uri.startswith(prefix):
        raise ValueError(f"only workspace:// URIs are allowed: {uri!r}")
    relative = normalize_logical_path(uri[len(prefix) :])
    return workspace_root.absolute() / PurePosixPath(relative)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_workspace_uri(uri: str, workspace_root: Path) -> Path:
    """Resolve an existing workspace URI and reject workspace escapes."""

    root = workspace_root.resolve(strict=True)
    lexical = workspace_uri_to_lexical_path(uri, root)
    resolved = lexical.resolve(strict=True)
    if not _is_within(resolved, root):
        raise ValueError(f"workspace URI resolves outside the workspace: {uri!r}")
    return resolved


def relpath(path: Path, root: Path) -> str:
    """Return a lexical path; retained for callers that must not resolve aliases."""

    return path.absolute().relative_to(root.absolute()).as_posix()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def read_asset_mounts(path: Path) -> list[AssetMount]:
    """Load and validate the explicit logical-to-physical mount contract."""

    mounts: list[AssetMount] = []
    seen_ids: set[str] = set()
    seen_logical: set[str] = set()
    for row in read_tsv(path):
        mount_id = row["mount_id"]
        logical_path = normalize_logical_path(row["logical_path"])
        if mount_id in seen_ids:
            raise ValueError(f"duplicate mount_id: {mount_id}")
        if logical_path in seen_logical:
            raise ValueError(f"duplicate logical mount: {logical_path}")
        if row["asset_kind"] not in {"tree", "file"}:
            raise ValueError(f"invalid asset_kind for {mount_id}: {row['asset_kind']}")
        if not row["canonical_uri"].startswith("workspace://"):
            raise ValueError(f"canonical_uri must use workspace:// for {mount_id}")
        include_value = row["include_in_source_inventory"].lower()
        if include_value not in {"true", "false"}:
            raise ValueError(
                f"include_in_source_inventory must be true or false for {mount_id}"
            )
        mounts.append(
            AssetMount(
                mount_id=mount_id,
                logical_path=logical_path,
                canonical_uri=row["canonical_uri"],
                asset_kind=row["asset_kind"],
                inventory_scope=row["inventory_scope"],
                include_in_source_inventory=include_value == "true",
                expected_file_count=int(row["expected_file_count"] or 0),
                expected_bytes=int(row["expected_bytes"] or 0),
            )
        )
        seen_ids.add(mount_id)
        seen_logical.add(logical_path)
    return mounts


def list_mount_files(
    mount: AssetMount, workspace_root: Path
) -> tuple[Path, list[Path]]:
    """Return regular files for a mount and reject undeclared nested symlinks."""

    physical_root = resolve_workspace_uri(mount.canonical_uri, workspace_root)
    if mount.asset_kind == "file":
        if not physical_root.is_file():
            raise ValueError(f"file mount is not a regular file: {mount.mount_id}")
        return physical_root, [physical_root]

    if not physical_root.is_dir():
        raise ValueError(f"tree mount is not a directory: {mount.mount_id}")
    entries = sorted(physical_root.rglob("*"), key=lambda item: item.as_posix())
    nested_symlinks = [entry for entry in entries if entry.is_symlink()]
    if nested_symlinks:
        relative = nested_symlinks[0].relative_to(physical_root).as_posix()
        raise ValueError(
            f"undeclared nested symlink in {mount.mount_id}: {relative}"
        )
    files = [entry.resolve(strict=True) for entry in entries if entry.is_file()]
    if any(not _is_within(path, physical_root) for path in files):
        raise ValueError(f"file escaped canonical mount: {mount.mount_id}")
    return physical_root, files


def validate_asset_mount_contract(
    workspace_root: Path, mounts: list[AssetMount], errors: list[str]
) -> None:
    """Validate every mount, including non-inventory execution dependencies."""

    if len(mounts) != EXPECTED_ASSET_MOUNT_COUNT:
        errors.append(
            f"expected {EXPECTED_ASSET_MOUNT_COUNT} asset mounts, found {len(mounts)}"
        )
    for mount in mounts:
        try:
            _, files = list_mount_files(mount, workspace_root)
            observed_bytes = sum(path.stat().st_size for path in files)
            if len(files) != mount.expected_file_count:
                errors.append(
                    f"{mount.mount_id}: expected {mount.expected_file_count} files, "
                    f"found {len(files)}"
                )
            if observed_bytes != mount.expected_bytes:
                errors.append(
                    f"{mount.mount_id}: expected {mount.expected_bytes} bytes, "
                    f"found {observed_bytes}"
                )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{mount.mount_id}: mount validation failed: {exc}")


def _mount_for_logical_path(logical_path: str, mounts: list[AssetMount]) -> AssetMount:
    logical = normalize_logical_path(logical_path)
    candidates = [
        mount
        for mount in mounts
        if logical == mount.logical_path
        or logical.startswith(mount.logical_path.rstrip("/") + "/")
    ]
    if not candidates:
        raise ValueError(f"no declared asset mount for logical path: {logical}")
    return max(candidates, key=lambda item: len(item.logical_path))


def resolve_logical_path(
    logical_path: str, workspace_root: Path, mounts: list[AssetMount]
) -> AssetRef:
    """Resolve a logical file through its declared mount without identity drift."""

    logical = normalize_logical_path(logical_path)
    mount = _mount_for_logical_path(logical, mounts)
    physical_root = resolve_workspace_uri(mount.canonical_uri, workspace_root)
    if mount.asset_kind == "file":
        if logical != mount.logical_path:
            raise ValueError(f"file mount cannot contain child path: {logical}")
        physical = physical_root
    else:
        suffix = PurePosixPath(logical).relative_to(PurePosixPath(mount.logical_path))
        candidate = physical_root / suffix
        cursor = physical_root
        for part in suffix.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"undeclared nested symlink in asset path: {logical}")
        physical = candidate.resolve(strict=True)
        if not _is_within(physical, physical_root):
            raise ValueError(f"asset path escapes declared mount: {logical}")
    if not physical.is_file():
        raise ValueError(f"logical asset is not a file: {logical}")
    return AssetRef(logical, physical)


def expand_logical_glob(
    logical_glob: str, workspace_root: Path, mounts: list[AssetMount]
) -> list[AssetRef]:
    """Expand one logical glob only inside its longest declared mount."""

    if "\\" in logical_glob or logical_glob.startswith("/") or ".." in PurePosixPath(logical_glob).parts:
        raise ValueError(f"invalid logical glob: {logical_glob!r}")
    static_prefix = logical_glob
    for marker in ("*", "?", "["):
        static_prefix = static_prefix.split(marker, 1)[0]
    static_prefix = static_prefix.rstrip("/")
    mount = _mount_for_logical_path(static_prefix, mounts)
    physical_root = resolve_workspace_uri(mount.canonical_uri, workspace_root)
    if mount.asset_kind == "file":
        if logical_glob != mount.logical_path:
            return []
        return [AssetRef(mount.logical_path, physical_root)] if physical_root.is_file() else []
    pattern_suffix = PurePosixPath(logical_glob).relative_to(
        PurePosixPath(mount.logical_path)
    ).as_posix()
    matches: list[AssetRef] = []
    for value in sorted(glob.glob(str(physical_root / pattern_suffix))):
        candidate = Path(value)
        relative_candidate = candidate.relative_to(physical_root)
        cursor = physical_root
        for part in relative_candidate.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(
                    f"undeclared nested symlink in cohort glob: "
                    f"{relative_candidate.as_posix()}"
                )
        physical = candidate.resolve(strict=True)
        if not physical.is_file() or not _is_within(physical, physical_root):
            continue
        suffix = physical.relative_to(physical_root).as_posix()
        logical = f"{mount.logical_path}/{suffix}"
        matches.append(AssetRef(normalize_logical_path(logical), physical))
    return matches


def validate_compatibility_aliases(
    path: Path, workspace_root: Path, errors: list[str]
) -> int:
    """Verify declared compatibility symlinks without trusting their targets."""

    if not path.is_file():
        errors.append(f"missing compatibility alias contract: {path.name}")
        return 0
    verified = 0
    for row in read_tsv(path):
        legacy_uri = row["legacy_uri"]
        target_uri = row["target_uri"]
        expected_link = row["relative_link_text"]
        try:
            legacy = workspace_uri_to_lexical_path(legacy_uri, workspace_root)
            target = resolve_workspace_uri(target_uri, workspace_root)
            if not legacy.is_symlink():
                raise ValueError("legacy path is not a symlink")
            observed_link = os.readlink(legacy)
            if os.path.isabs(observed_link):
                raise ValueError("absolute symlink is forbidden")
            if observed_link != expected_link:
                raise ValueError(
                    f"link text differs: observed={observed_link!r} expected={expected_link!r}"
                )
            observed_target = legacy.resolve(strict=True)
            if observed_target != target:
                raise ValueError("resolved target differs from declared target")
            verified += 1
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"compatibility alias invalid ({legacy_uri}): {exc}")
    return verified


def render_tsv(rows: Iterable[dict[str, Any]], fields: list[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return buffer.getvalue()


def render_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def publish_or_check(path: Path, text: str, mode: str) -> None:
    if mode == "check":
        if not path.is_file():
            raise SystemExit(f"MISSING_DERIVED_ASSET: {path}")
        observed = path.read_text(encoding="utf-8")
        if observed != text:
            raise SystemExit(f"STALE_DERIVED_ASSET: {path}")
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def clean_sequence(value: str | None) -> str:
    if not value or value in {"?", "."}:
        return ""
    return "".join(value.replace(";", "").split()).upper()


def cif_value(block: gemmi.cif.Block, tag: str) -> str:
    value = block.find_value(tag)
    if value is None or value in {"?", "."}:
        return ""
    return str(value)


def coordinate_fingerprint(block: gemmi.cif.Block) -> str:
    tags = [
        "_atom_site.group_PDB",
        "_atom_site.type_symbol",
        "_atom_site.label_atom_id",
        "_atom_site.label_alt_id",
        "_atom_site.label_comp_id",
        "_atom_site.label_asym_id",
        "_atom_site.label_seq_id",
        "_atom_site.Cartn_x",
        "_atom_site.Cartn_y",
        "_atom_site.Cartn_z",
        "_atom_site.pdbx_PDB_model_num",
    ]
    table = block.find(tags)
    material = "\n".join("\t".join(str(value) for value in row) for row in table)
    return sha256_text(material + ("\n" if material else ""))


def parse_structure(path: Path) -> dict[str, Any]:
    document = gemmi.cif.read(str(path))
    if len(document) != 1:
        raise ValueError(f"expected one CIF data block, found {len(document)}")
    block = document.sole_block()
    structure = gemmi.read_structure(str(path))
    if len(structure) < 1:
        raise ValueError("no coordinate model")

    atom_tags = [
        "_atom_site.id",
        "_atom_site.label_asym_id",
        "_atom_site.auth_asym_id",
        "_atom_site.label_seq_id",
        "_atom_site.label_atom_id",
        "_atom_site.Cartn_x",
        "_atom_site.Cartn_y",
        "_atom_site.Cartn_z",
        "_atom_site.pdbx_PDB_model_num",
    ]
    atom_table = block.find(atom_tags)
    atom_ids: list[str] = []
    label_chains: set[str] = set()
    auth_chains: set[str] = set()
    model_numbers: set[str] = set()
    finite_coordinates = True
    for row in atom_table:
        atom_id, label_chain, auth_chain, _, _, x, y, z, model = map(str, row)
        atom_ids.append(atom_id)
        label_chains.add(label_chain)
        auth_chains.add(auth_chain)
        model_numbers.add(model)
        try:
            finite_coordinates = finite_coordinates and all(
                math.isfinite(float(value)) for value in (x, y, z)
            )
        except ValueError:
            finite_coordinates = False

    declared_sequences = sorted(
        {
            sequence
            for sequence in (
                clean_sequence(str(value))
                for value in block.find_values(
                    "_entity_poly.pdbx_seq_one_letter_code_can"
                )
            )
            if sequence
        }
    )
    observed_chains: list[dict[str, Any]] = []
    missing_backbone_residues = 0
    oxt_count = 0
    first_model = structure[0]
    for chain in first_model:
        polymer = chain.get_polymer()
        if not polymer:
            continue
        sequence = "".join(
            gemmi.find_tabulated_residue(residue.name).one_letter_code
            for residue in polymer
        )
        observed_chains.append(
            {"chain": chain.name, "length": len(polymer), "sequence": sequence}
        )
        for residue in polymer:
            names = {atom.name.strip() for atom in residue}
            if not {"N", "CA", "C", "O"}.issubset(names):
                missing_backbone_residues += 1
            oxt_count += sum(atom.name.strip() == "OXT" for atom in residue)

    return {
        "data_block": block.name,
        "model_count": len(structure),
        "model_numbers": ",".join(sorted(model_numbers, key=lambda x: int(x))),
        "atom_count": len(atom_table),
        "atom_id_unique": len(atom_ids) == len(set(atom_ids)),
        "finite_coordinates": finite_coordinates,
        "label_chains": ",".join(sorted(label_chains)),
        "auth_chains": ",".join(sorted(auth_chains)),
        "polymer_chain_count": len(observed_chains),
        "observed_lengths": ",".join(str(item["length"]) for item in observed_chains),
        "observed_sequences": "|".join(item["sequence"] for item in observed_chains),
        "declared_sequences": "|".join(declared_sequences),
        "missing_backbone_residues": missing_backbone_residues,
        "oxt_count": oxt_count,
        "source_pdb": cif_value(block, "_boltzgen_prep.source_pdb_id"),
        "prep_suitability": cif_value(block, "_boltzgen_prep.suitability"),
        "prep_warning": cif_value(block, "_boltzgen_prep.warning"),
        "prep_terminal_state": cif_value(block, "_boltzgen_prep.c_terminal_state"),
        "coordinate_sha256": coordinate_fingerprint(block),
    }


def expand_cohorts(
    workspace_root: Path,
    mounts: list[AssetMount],
    cohort_rows: list[dict[str, str]],
    overrides: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    structures: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    for cohort in cohort_rows:
        try:
            matches = expand_logical_glob(
                cohort["canonical_glob"], workspace_root, mounts
            )
        except (OSError, RuntimeError, ValueError) as exc:
            matches = []
            errors.append(f"{cohort['cohort_id']}: logical glob failed: {exc}")
        expected_count = int(cohort["expected_file_count"])
        if len(matches) != expected_count:
            errors.append(
                f"{cohort['cohort_id']}: expected {expected_count} files, found {len(matches)}"
            )

        status_counts: Counter[str] = Counter()
        parse_pass = 0
        complete_count = 0
        for asset in matches:
            path = asset.physical_path
            relative = asset.logical_relative_path
            override = overrides.get(relative, {})
            status = override.get("status", cohort["default_status"])
            reason = override.get("reason", cohort["limitations"])
            status_counts[status] += 1
            row: dict[str, Any] = {
                "cohort_id": cohort["cohort_id"],
                "asset_class": cohort["asset_class"],
                "source_id": cohort["source_id"],
                "relative_path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "ai_role": cohort["ai_role"],
                "status": status,
                "active_for_ai": str(status in ACTIVE_STATUSES).lower(),
                "experimental_negative": "false",
                "binding_label": "unknown_or_not_applicable",
                "independence_group": f"PDB:{cohort['source_id']}",
                "terminal_chemistry_claim": cohort["terminal_chemistry_claim"],
                "limitation_or_reason": reason,
                "parse_status": "PASS",
                "validation_status": "PASS",
            }
            try:
                parsed = parse_structure(path)
                row.update(parsed)
                parse_pass += 1
            except Exception as exc:  # report every bad asset before exiting
                row["parse_status"] = "FAIL"
                row["validation_status"] = "FAIL_PARSE"
                row["limitation_or_reason"] = f"{reason}; parse error: {exc}"
                errors.append(f"{relative}: CIF parse failed: {exc}")
                structures.append(row)
                continue

            expected_models = cohort["expected_models_per_file"]
            expected_sequence = cohort["expected_sequence"]
            expected_length = cohort["expected_declared_length"]
            geometry_complete = True
            checks: list[str] = []
            if expected_models and row["model_count"] != int(expected_models):
                geometry_complete = False
                checks.append(
                    f"model_count={row['model_count']} expected={expected_models}"
                )
            if expected_sequence:
                declared = row["declared_sequences"].split("|") if row["declared_sequences"] else []
                observed = row["observed_sequences"].split("|") if row["observed_sequences"] else []
                if declared and expected_sequence not in declared:
                    geometry_complete = False
                    checks.append("expected sequence absent from declared entity sequence")
                if expected_sequence not in observed:
                    geometry_complete = False
                    checks.append("expected sequence not fully observed in coordinates")
            if expected_length:
                observed_lengths = {
                    int(value) for value in row["observed_lengths"].split(",") if value
                }
                if int(expected_length) not in observed_lengths:
                    geometry_complete = False
                    checks.append(
                        f"expected observed length {expected_length} absent"
                    )
            if row["missing_backbone_residues"]:
                geometry_complete = False
                checks.append(
                    f"{row['missing_backbone_residues']} observed residues lack N/CA/C/O"
                )
            if not row["atom_id_unique"]:
                geometry_complete = False
                checks.append("atom_site.id values are not unique")
            if not row["finite_coordinates"]:
                geometry_complete = False
                checks.append("non-finite coordinate")

            row["geometry_complete"] = str(geometry_complete).lower()
            if geometry_complete:
                complete_count += 1
            elif status in ACTIVE_STATUSES:
                row["validation_status"] = "FAIL_ACTIVE_ASSET_INCOMPLETE"
                errors.append(f"{relative}: active asset incomplete: {'; '.join(checks)}")
            else:
                row["validation_status"] = "EXPECTED_EXCLUSION"
                row["limitation_or_reason"] = f"{reason}; {'; '.join(checks)}"
            structures.append(row)

        summaries.append(
            {
                "cohort_id": cohort["cohort_id"],
                "asset_class": cohort["asset_class"],
                "source_id": cohort["source_id"],
                "file_count": len(matches),
                "parse_pass": parse_pass,
                "geometry_complete": complete_count,
                "active_file_count": sum(
                    count for status, count in status_counts.items() if status in ACTIVE_STATUSES
                ),
                "biological_units": cohort["biological_units"],
                "status_counts": ";".join(
                    f"{status}:{count}" for status, count in sorted(status_counts.items())
                ),
                "ai_role": cohort["ai_role"],
                "limitations": cohort["limitations"],
            }
        )
    return structures, summaries, errors, warnings


def inventory_source_files(
    workspace_root: Path, mounts: list[AssetMount], errors: list[str]
) -> list[dict[str, Any]]:
    """Inventory each declared logical mount, preserving intentional mirrors."""

    assets: list[tuple[AssetMount, AssetRef]] = []
    seen_logical: set[str] = set()
    for mount in mounts:
        if not mount.include_in_source_inventory:
            continue
        try:
            physical_root, physical_paths = list_mount_files(mount, workspace_root)
            observed_bytes = sum(path.stat().st_size for path in physical_paths)
            if len(physical_paths) != mount.expected_file_count:
                errors.append(
                    f"{mount.mount_id}: expected {mount.expected_file_count} source files, "
                    f"found {len(physical_paths)}"
                )
            if observed_bytes != mount.expected_bytes:
                errors.append(
                    f"{mount.mount_id}: expected {mount.expected_bytes} bytes, "
                    f"found {observed_bytes}"
                )
            for physical in physical_paths:
                if mount.asset_kind == "file":
                    logical = mount.logical_path
                else:
                    suffix = physical.relative_to(physical_root).as_posix()
                    logical = normalize_logical_path(f"{mount.logical_path}/{suffix}")
                if logical in seen_logical:
                    errors.append(f"duplicate logical source identity: {logical}")
                    continue
                seen_logical.add(logical)
                assets.append((mount, AssetRef(logical, physical)))
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{mount.mount_id}: source inventory failed: {exc}")

    rows: list[dict[str, Any]] = []
    for mount, asset in sorted(assets, key=lambda item: item[1].logical_relative_path):
        path = asset.physical_path
        relative = asset.logical_relative_path
        logical_path = PurePosixPath(relative)
        digest = sha256_file(path)
        rows.append(
            {
                "relative_path": relative,
                "bytes": path.stat().st_size,
                "suffix": logical_path.suffix.lower(),
                "sha256": digest,
                "inventory_scope": mount.inventory_scope,
                "excluded_from_model_input": str(
                    logical_path.name == ".DS_Store"
                    or "原始文件" in logical_path.parts
                ).lower(),
            }
        )
    return rows


def build_duplicate_groups(
    source_inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_inventory:
        by_hash[row["sha256"]].append(row)
    output: list[dict[str, Any]] = []
    group_number = 0
    for digest, members in sorted(by_hash.items()):
        if len(members) < 2:
            continue
        group_number += 1
        group_id = f"DUP{group_number:04d}"
        for index, member in enumerate(
            sorted(members, key=lambda item: item["relative_path"]), start=1
        ):
            output.append(
                {
                    "duplicate_group_id": group_id,
                    "sha256": digest,
                    "bytes": member["bytes"],
                    "member_count": len(members),
                    "member_index": index,
                    "relative_path": member["relative_path"],
                }
            )
    return output


def verify_positive_aliases(
    workspace_root: Path,
    mounts: list[AssetMount],
    structures: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    by_path = {row["relative_path"]: row for row in structures}
    canonical_root = "data/样本数据/binding-多构象"
    mirror_root = "data/多构象-1"
    canonical_paths = sorted(
        path
        for path in by_path
        if path.startswith(canonical_root + "/") and path.endswith(".cif")
    )
    identical_mirror_pairs = 0
    for canonical in canonical_paths:
        mirror = mirror_root + canonical[len(canonical_root) :]
        if mirror not in by_path:
            errors.append(f"missing positive mirror: {mirror}")
            continue
        if by_path[canonical]["sha256"] != by_path[mirror]["sha256"]:
            errors.append(f"positive mirror differs: {canonical} vs {mirror}")
            continue
        identical_mirror_pairs += 1

    representative_map = {
        "data/样本数据/binding-多构象/筛选(根据RMSD聚类)有代表性的3个/GLP1_conf01-model12.cif":
            "data/样本数据/binding-多构象/all_conformers/1D0R_model12.cif",
        "data/样本数据/binding-多构象/筛选(根据RMSD聚类)有代表性的3个/GLP1_conf02-model19.cif":
            "data/样本数据/binding-多构象/all_conformers/1D0R_model19.cif",
        "data/样本数据/binding-多构象/筛选(根据RMSD聚类)有代表性的3个/GLP1_conf03-model20.cif":
            "data/样本数据/binding-多构象/all_conformers/1D0R_model20.cif",
    }
    representative_matches = 0
    for representative, source_model in representative_map.items():
        if representative not in by_path or source_model not in by_path:
            errors.append(f"missing representative/source mapping: {representative}")
            continue
        if (
            by_path[representative]["coordinate_sha256"]
            != by_path[source_model]["coordinate_sha256"]
        ):
            errors.append(
                f"representative coordinates differ: {representative} vs {source_model}"
            )
            continue
        representative_matches += 1

    active_representative_aliases = 0
    for representative in representative_map:
        row = by_path.get(representative)
        if row is None:
            continue
        if row["status"] != "DUPLICATE_ALIAS" or row["active_for_ai"] != "false":
            errors.append(
                f"representative alias must be inactive DUPLICATE_ALIAS: {representative}"
            )
        else:
            continue
        active_representative_aliases += 1

    compact_statuses = {
        10: "USE_POSITIVE_FIXED_CONTROL",
        12: "USE_POSITIVE_COMPACT",
        19: "USE_POSITIVE_COMPACT",
        20: "USE_POSITIVE_COMPACT",
    }
    compact_panel_paths_verified = 0
    for model_id, expected_status in compact_statuses.items():
        source_model = (
            "data/样本数据/binding-多构象/all_conformers/"
            f"1D0R_model{model_id:02d}.cif"
        )
        row = by_path.get(source_model)
        if row is None:
            errors.append(f"missing canonical compact model: {source_model}")
            continue
        if row["status"] != expected_status or row["active_for_ai"] != "true":
            errors.append(
                f"bad compact-model status: {source_model} "
                f"status={row['status']} active={row['active_for_ai']}"
            )
            continue
        compact_panel_paths_verified += 1

    split_model_paths = [
        row
        for path, row in by_path.items()
        if path.startswith(canonical_root + "/all_conformers/1D0R_model")
        and path.endswith(".cif")
    ]
    if len(split_model_paths) != 20:
        errors.append(
            f"expected 20 canonical split 1D0R models, found {len(split_model_paths)}"
        )

    raw_logical = (
        "data/boltzgen_data/mvp_assets_v0.3.2/"
        "raw_sources/rcsb_structures/1D0R.cif"
    )
    expected_raw_hash = "efa9eefd43129b2b2c1124a5da3099c8abb315f017560d1e678fa354b2a2bcf8"
    try:
        raw_path = resolve_logical_path(raw_logical, workspace_root, mounts).physical_path
        raw_hash_match = sha256_file(raw_path) == expected_raw_hash
    except (OSError, RuntimeError, ValueError):
        raw_hash_match = False
    if not raw_hash_match:
        errors.append("1D0R raw source missing or SHA-256 mismatch")
    return {
        "canonical_cif_paths": len(canonical_paths),
        "byte_identical_mirror_pairs": identical_mirror_pairs,
        "representative_coordinate_matches": representative_matches,
        "representative_expected": len(representative_map),
        "active_representative_aliases": active_representative_aliases,
        "compact_panel_paths_verified": compact_panel_paths_verified,
        "full_sensitivity_split_files_verified": len(split_model_paths),
        "multi_model_cif_allowed_for_execution": False,
        "raw_1d0r_sha256_match": raw_hash_match,
        "independence_group": "PDB:1D0R",
        "independent_biological_samples": 1,
        "submitted_conformers": 20,
        "derived_representative_aliases": 3,
        "compact_panel_models": [10, 12, 19, 20],
    }


def verify_negative_manifest(
    workspace_root: Path,
    mounts: list[AssetMount],
    structures: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    manifest_path = resolve_logical_path(
        "data/样本数据/not_binding/not_binding/structure_manifest.csv",
        workspace_root,
        mounts,
    ).physical_path
    run_summary_path = resolve_logical_path(
        "data/样本数据/not_binding/not_binding/run_summary.json",
        workspace_root,
        mounts,
    ).physical_path
    with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))
    summary = json.loads(run_summary_path.read_text(encoding="utf-8-sig"))
    if len(manifest_rows) != 25:
        errors.append(f"negative structure_manifest expected 25 rows, found {len(manifest_rows)}")
    if summary.get("prepared_files") != 25:
        errors.append("negative run_summary prepared_files is not 25")

    actual_by_name = {
        Path(row["relative_path"]).name: row
        for row in structures
        if row["relative_path"].startswith(
            "data/样本数据/not_binding/not_binding/"
        )
        and row["relative_path"].endswith(".cif")
    }
    manifest_matches = 0
    raw_hash_matches = 0
    for manifest in manifest_rows:
        name = Path(manifest["prepared_file"].replace("\\", "/")).name
        actual = actual_by_name.get(name)
        if actual is None:
            errors.append(f"manifest prepared file missing locally: {name}")
            continue
        if clean_sequence(manifest["sequence"]) not in actual.get(
            "declared_sequences", ""
        ).split("|"):
            errors.append(f"manifest sequence mismatch: {name}")
            continue
        expected_complete = manifest["suitability"] != "NOT_RECOMMENDED"
        if (actual.get("geometry_complete") == "true") != expected_complete:
            errors.append(f"manifest suitability/completeness mismatch: {name}")
            continue
        manifest_matches += 1

        raw_logical = (
            "data/样本数据/not_binding/原始文件/"
            f"{manifest['pdb_id']}.cif"
        )
        try:
            raw = resolve_logical_path(raw_logical, workspace_root, mounts).physical_path
            if sha256_file(raw) == manifest["raw_sha256"]:
                raw_hash_matches += 1
            else:
                errors.append(f"raw source SHA-256 mismatch: {manifest['pdb_id']}")
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(
                f"raw source could not be verified: {manifest['pdb_id']}: {error}"
            )

    challenge_rows = [
        row
        for row in structures
        if row["asset_class"].startswith("peptide_challenge")
    ]
    active = [row for row in challenge_rows if row["status"] in ACTIVE_STATUSES]
    excluded = [row for row in challenge_rows if row["status"] == "EXCLUDE_INCOMPLETE"]
    active_groups = sorted(
        {
            row["cohort_id"]
            for row in active
        }
    )
    if len(active) != 32:
        errors.append(f"expected 32 usable challenge conformers, found {len(active)}")
    if len(excluded) != 4:
        errors.append(f"expected 4 excluded incomplete challenge conformers, found {len(excluded)}")
    if len(active_groups) != 4:
        errors.append(f"expected 4 usable target/source groups, found {len(active_groups)}")
    return {
        "prepared_cif_total": len(challenge_rows),
        "usable_challenge_conformers": len(active),
        "quarantined_incomplete_conformers": len(excluded),
        "usable_target_source_groups": len(active_groups),
        "usable_cohorts": active_groups,
        "experimental_negative_labels": 0,
        "required_label_status": "computational_challenge_unvalidated",
        "manifest_rows_verified": manifest_matches,
        "raw_source_hashes_verified": raw_hash_matches,
        "manifest_windows_paths_resolve_locally": 0,
        "weighting_rule": (
            "aggregate within each source/target ensemble; keep tuning and lockbox "
            "partitions separate; macro-average only pre-standardized comparable metrics"
        ),
    }


def parse_checksum_manifest(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split(maxsplit=1)
        entries.append((digest, relative.lstrip("*")))
    return entries


def verify_checksum_manifest(
    manifest_path: Path,
    library_root: Path,
    errors: list[str],
    *,
    expected_count: int = EXPECTED_NEW_SCAFFOLD_CHECKSUM_COUNT,
) -> tuple[int, int]:
    """Verify an exact-size checksum contract without accepting duplicate paths."""

    entries = parse_checksum_manifest(manifest_path)
    if len(entries) != expected_count:
        errors.append(
            f"new scaffold checksums.sha256 expected {expected_count} entries, "
            f"found {len(entries)}"
        )

    verified = 0
    seen_paths: set[str] = set()
    canonical_root = library_root.resolve(strict=True)
    for expected, relative in entries:
        try:
            normalized = normalize_logical_path(relative)
        except ValueError as error:
            errors.append(f"invalid new scaffold checksum path {relative!r}: {error}")
            continue
        if normalized in seen_paths:
            errors.append(f"duplicate new scaffold checksum path: {normalized}")
            continue
        seen_paths.add(normalized)
        path = library_root / PurePosixPath(normalized)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            errors.append(f"new scaffold checksums.sha256 file missing: {normalized}")
            continue
        if not _is_within(resolved, canonical_root) or not resolved.is_file():
            errors.append(f"new scaffold checksum path escapes its package: {normalized}")
            continue
        if sha256_file(resolved) != expected:
            errors.append(f"new scaffold checksums.sha256 mismatch: {normalized}")
            continue
        verified += 1

    if verified != expected_count:
        errors.append(
            f"new scaffold checksums.sha256 expected {expected_count} verified entries, "
            f"found {verified}"
        )
    return len(entries), verified


def verify_scaffolds(
    workspace_root: Path,
    mounts: list[AssetMount],
    structures: list[dict[str, Any]],
    errors: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    library_mount = _mount_for_logical_path(
        "data/样本数据/boltzgen_vhh_scaffolds", mounts
    )
    library_root = resolve_workspace_uri(library_mount.canonical_uri, workspace_root)
    manifest_path = library_root / "manifest.csv"
    with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))
    if len(manifest_rows) != 17:
        errors.append(f"new scaffold manifest expected 17 rows, found {len(manifest_rows)}")

    structure_by_path = {row["relative_path"]: row for row in structures}
    comparison: list[dict[str, Any]] = []
    manifest_verified = 0
    yaml_references_verified = 0
    for item in manifest_rows:
        folder = library_root / item["folder"]
        cif_path = folder / item["cif_filename"]
        yaml_path = folder / item["yaml_filename"]
        metadata_path = folder / item["metadata_filename"]
        required = (cif_path, yaml_path, metadata_path, folder / "README.md")
        if not all(path.is_file() for path in required):
            errors.append(f"new scaffold package incomplete: {item['folder']}")
            continue
        actual_hashes = (
            sha256_file(cif_path),
            sha256_file(yaml_path),
            sha256_file(metadata_path),
        )
        expected_hashes = (
            item["cif_sha256"],
            item["yaml_sha256"],
            item["metadata_sha256"],
        )
        if actual_hashes != expected_hashes:
            errors.append(f"new scaffold manifest hash mismatch: {item['folder']}")
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        yaml_data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if metadata.get("instance") != item["INSTANCE"]:
            errors.append(f"new scaffold metadata INSTANCE mismatch: {item['folder']}")
            continue
        if yaml_data.get("path") != item["cif_filename"]:
            errors.append(f"new scaffold YAML path mismatch: {item['folder']}")
            continue
        yaml_references_verified += 1
        relative_cif = (
            f"{library_mount.logical_path}/"
            f"{cif_path.resolve(strict=True).relative_to(library_root).as_posix()}"
        )
        structure_row = structure_by_path.get(relative_cif)
        if structure_row is None or structure_row.get("parse_status") != "PASS":
            errors.append(f"new scaffold CIF not parsed: {item['folder']}")
            continue
        manifest_verified += 1
        comparison.append(
            {
                "instance": item["INSTANCE"],
                "new_rank": item["second_stage_rank"],
                "new_folder": item["folder"],
                "new_qc_status": item["qc_status"],
                "new_warning": item["warnings"],
                "ai_status": structure_row["status"],
                "old12_overlap": "false",
                "old12_folder": "",
            }
        )

    checksum_total, checksum_verified = verify_checksum_manifest(
        library_root / "checksums.sha256",
        library_root,
        errors,
    )

    old_mount = _mount_for_logical_path(
        "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/selected", mounts
    )
    old_root = resolve_workspace_uri(old_mount.canonical_uri, workspace_root)
    old_folders = {
        folder.name.split("_", 1)[1]: folder.name
        for folder in old_root.iterdir()
        if folder.is_dir() and "_" in folder.name
    }
    for row in comparison:
        if row["instance"] in old_folders:
            row["old12_overlap"] = "true"
            row["old12_folder"] = old_folders[row["instance"]]
    overlaps = sum(row["old12_overlap"] == "true" for row in comparison)
    if overlaps != 4:
        errors.append(f"expected four INSTANCE overlaps between new17 and old12, found {overlaps}")

    status_counts = Counter(row["ai_status"] for row in comparison)
    expected_status_counts = {
        "DUPLICATE_ALIAS_USE_OLD_CANONICAL": 3,
        "STRESS_ONLY_USE_OLD_CANONICAL": 1,
        "QUARANTINE_PENDING_REDESIGN": 1,
        "QUARANTINE_MISSING_CDR_COORDINATES": 1,
        "NEEDS_REPAIR_OR_ACCEPTANCE": 4,
        "PENDING_CANONICALIZATION_AND_CHECK": 7,
    }
    if dict(status_counts) != expected_status_counts:
        errors.append(
            "new17 scaffold status partition differs: "
            f"observed={dict(status_counts)} expected={expected_status_counts}"
        )
    return comparison, {
        "new_scaffold_packages": len(manifest_rows),
        "manifest_rows_verified": manifest_verified,
        "yaml_relative_references_verified": yaml_references_verified,
        "checksum_entries_verified": checksum_verified,
        "checksum_entries_total": checksum_total,
        "unique_instances": len({row["INSTANCE"] for row in manifest_rows}),
        "old12_instance_overlaps": overlaps,
        "union_unique_instances": 12 + len(manifest_rows) - overlaps,
        "production_active_from_new17": sum(
            row["ai_status"] == "USE_PRODUCTION_SCAFFOLD" for row in comparison
        ),
        "overlap_use_old_canonical": 4,
        "quarantined_new_scaffolds": 2,
        "repair_or_accept_new_scaffolds": 4,
        "pending_canonicalization_new_scaffolds": 7,
        "ai_status_counts": dict(sorted(status_counts.items())),
        "boltzgen_cli_check": "NOT_RUN_REQUIRES_TARGET_CONTAINING_SPEC",
    }


def assert_inventory_hard_gates(
    *,
    source_inventory: list[dict[str, Any]],
    structures: list[dict[str, Any]],
) -> None:
    """Raise when frozen row, identity, or parse gates do not close."""

    logical_source_paths = [row["relative_path"] for row in source_inventory]
    logical_structure_paths = [row["relative_path"] for row in structures]
    failures: list[str] = []
    if len(source_inventory) != HISTORICAL_SOURCE_FILE_COUNT:
        failures.append(
            f"expected {HISTORICAL_SOURCE_FILE_COUNT} historical logical source files, "
            f"found {len(source_inventory)}"
        )
    if len(set(logical_source_paths)) != HISTORICAL_SOURCE_FILE_COUNT:
        failures.append("historical source logical paths are not unique")
    if len(structures) != EXPECTED_STRUCTURE_PATH_COUNT:
        failures.append(
            f"expected {EXPECTED_STRUCTURE_PATH_COUNT} structure paths, "
            f"found {len(structures)}"
        )
    if len(set(logical_structure_paths)) != EXPECTED_STRUCTURE_PATH_COUNT:
        failures.append("structure logical paths are not unique")
    parse_pass = sum(row.get("parse_status") == "PASS" for row in structures)
    if parse_pass != EXPECTED_STRUCTURE_PARSE_PASS:
        failures.append(
            f"expected {EXPECTED_STRUCTURE_PARSE_PASS} parsed structures, found {parse_pass}"
        )
    if failures:
        raise ValueError("; ".join(failures))


def validate_frozen_invariants(
    source_inventory: list[dict[str, Any]],
    structures: list[dict[str, Any]],
    errors: list[str],
) -> None:
    """Apply the migration-regression gates before derived assets can publish."""

    try:
        assert_inventory_hard_gates(
            source_inventory=source_inventory,
            structures=structures,
        )
    except ValueError as exc:
        errors.append(str(exc))
    observed_bytes = sum(int(row["bytes"]) for row in source_inventory)
    if observed_bytes != HISTORICAL_SOURCE_BYTES:
        errors.append(
            f"expected {HISTORICAL_SOURCE_BYTES} historical logical source bytes, "
            f"found {observed_bytes}"
        )


def discover_workspace_root(script_path: Path) -> Path:
    """Find the workspace by its migration manifest, without machine paths."""

    for candidate in script_path.resolve().parents:
        if (
            candidate
            / "manifests/local_assets_20260826/local_workspace_migration_20260826.csv"
        ).is_file():
            return candidate
    raise SystemExit("workspace root not found; pass --workspace-root")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument(
        "--project-root",
        type=Path,
        help="deprecated alias for --workspace-root",
    )
    parser.add_argument(
        "--registry-root",
        type=Path,
        help="backward-compatible shorthand setting both contract and output root",
    )
    parser.add_argument(
        "--contract-root",
        type=Path,
        help="directory containing versioned static registry contracts",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="external directory containing derived registry files",
    )
    args = parser.parse_args()

    if args.workspace_root and args.project_root:
        raise SystemExit("pass only one of --workspace-root or --project-root")
    requested_workspace = args.workspace_root or args.project_root
    workspace_root = (
        requested_workspace.resolve(strict=True)
        if requested_workspace
        else discover_workspace_root(Path(__file__))
    )
    if not (workspace_root / "manifests/local_assets_20260826").is_dir():
        raise SystemExit("invalid workspace root: migration manifest directory is absent")
    if args.registry_root and (args.contract_root or args.output_root):
        raise SystemExit(
            "--registry-root cannot be combined with --contract-root or --output-root"
        )
    default_contract = (
        Path(__file__).resolve().parents[2]
        / "resources/data/AI结构资产验证登记册_20260828"
    )
    contract_dir = (
        args.registry_root.resolve(strict=True)
        if args.registry_root
        else args.contract_root.resolve(strict=True)
        if args.contract_root
        else default_contract.resolve(strict=True)
    )
    output_dir = (
        args.registry_root.resolve(strict=True)
        if args.registry_root
        else args.output_root.resolve(strict=True)
        if args.output_root
        else (
            workspace_root
            / "boltzgen/data/ai_structure_asset_validation_registry_20260828"
        ).resolve(strict=True)
    )
    if not contract_dir.is_dir():
        raise SystemExit("contract root does not exist")
    if not output_dir.is_dir():
        raise SystemExit("output root does not exist")
    if args.write and output_dir.name == "ai_structure_asset_validation_registry_20260826":
        raise SystemExit("historical 20260826 registry is immutable; select a new registry root")

    cohort_path = contract_dir / "cohort_registry.tsv"
    override_path = contract_dir / "file_overrides.tsv"
    mount_path = contract_dir / "asset_mounts.tsv"
    alias_path = contract_dir / "compatibility_aliases.tsv"
    historical_hash_path = contract_dir / "historical_output_hashes.tsv"
    cohorts = read_tsv(cohort_path)
    mounts = read_asset_mounts(mount_path)
    contract_errors: list[str] = []
    if len(cohorts) != EXPECTED_COHORT_COUNT:
        contract_errors.append(
            f"expected {EXPECTED_COHORT_COUNT} cohorts, found {len(cohorts)}"
        )
    cohort_ids = [row["cohort_id"] for row in cohorts]
    if len(set(cohort_ids)) != len(cohort_ids):
        contract_errors.append("cohort_id values are not unique")
    validate_asset_mount_contract(workspace_root, mounts, contract_errors)

    override_rows = read_tsv(override_path)
    if len(override_rows) != EXPECTED_FILE_OVERRIDE_COUNT:
        contract_errors.append(
            f"expected {EXPECTED_FILE_OVERRIDE_COUNT} file overrides, "
            f"found {len(override_rows)}"
        )
    override_paths = [
        normalize_logical_path(row["relative_path"]) for row in override_rows
    ]
    if len(set(override_paths)) != len(override_paths):
        contract_errors.append("file override logical paths are not unique")
    overrides = {path: row for path, row in zip(override_paths, override_rows)}

    alias_rows = read_tsv(alias_path)
    if len(alias_rows) != EXPECTED_COMPATIBILITY_ALIAS_COUNT:
        contract_errors.append(
            f"expected {EXPECTED_COMPATIBILITY_ALIAS_COUNT} compatibility aliases, "
            f"found {len(alias_rows)}"
        )
    alias_legacy_uris = [row["legacy_uri"] for row in alias_rows]
    if len(set(alias_legacy_uris)) != len(alias_legacy_uris):
        contract_errors.append("compatibility alias legacy_uri values are not unique")

    structures, cohort_summaries, errors, warnings = expand_cohorts(
        workspace_root, mounts, cohorts, overrides
    )
    errors = contract_errors + errors
    structure_paths = {row["relative_path"] for row in structures}
    unmatched_overrides = sorted(set(overrides) - structure_paths)
    if unmatched_overrides:
        errors.append(
            "file overrides did not match structure inventory: "
            + ", ".join(unmatched_overrides)
        )
    source_inventory = inventory_source_files(workspace_root, mounts, errors)
    duplicate_groups = build_duplicate_groups(source_inventory)
    compatibility_aliases_verified = validate_compatibility_aliases(
        alias_path, workspace_root, errors
    )
    if compatibility_aliases_verified != EXPECTED_COMPATIBILITY_ALIAS_COUNT:
        errors.append(
            f"expected {EXPECTED_COMPATIBILITY_ALIAS_COUNT} verified compatibility "
            f"aliases, found {compatibility_aliases_verified}"
        )
    positive = verify_positive_aliases(workspace_root, mounts, structures, errors)
    negative = verify_negative_manifest(workspace_root, mounts, structures, errors)
    scaffold_comparison, scaffolds = verify_scaffolds(
        workspace_root, mounts, structures, errors
    )
    validate_frozen_invariants(source_inventory, structures, errors)

    # The folder name is not evidence.  These values are deliberately hard-coded
    # to zero until a future assay import supplies a measured VHH/target outcome.
    if any(row["experimental_negative"] != "false" for row in structures):
        errors.append("a structure asset was incorrectly marked experimental_negative")

    outputs: dict[Path, str] = {}
    outputs[output_dir / "source_file_inventory.tsv"] = render_tsv(
        source_inventory,
        [
            "relative_path",
            "bytes",
            "suffix",
            "sha256",
            "inventory_scope",
            "excluded_from_model_input",
        ],
    )
    outputs[output_dir / "duplicate_groups.tsv"] = render_tsv(
        duplicate_groups,
        [
            "duplicate_group_id",
            "sha256",
            "bytes",
            "member_count",
            "member_index",
            "relative_path",
        ],
    )
    structure_fields = [
        "cohort_id",
        "asset_class",
        "source_id",
        "relative_path",
        "bytes",
        "sha256",
        "coordinate_sha256",
        "ai_role",
        "status",
        "active_for_ai",
        "experimental_negative",
        "binding_label",
        "independence_group",
        "terminal_chemistry_claim",
        "parse_status",
        "validation_status",
        "geometry_complete",
        "model_count",
        "model_numbers",
        "polymer_chain_count",
        "label_chains",
        "auth_chains",
        "declared_sequences",
        "observed_sequences",
        "observed_lengths",
        "atom_count",
        "atom_id_unique",
        "finite_coordinates",
        "missing_backbone_residues",
        "oxt_count",
        "source_pdb",
        "prep_suitability",
        "prep_terminal_state",
        "prep_warning",
        "limitation_or_reason",
    ]
    outputs[output_dir / "structure_inventory.tsv"] = render_tsv(
        sorted(structures, key=lambda row: row["relative_path"]), structure_fields
    )
    outputs[output_dir / "cohort_summary.tsv"] = render_tsv(
        cohort_summaries,
        [
            "cohort_id",
            "asset_class",
            "source_id",
            "file_count",
            "parse_pass",
            "geometry_complete",
            "active_file_count",
            "biological_units",
            "status_counts",
            "ai_role",
            "limitations",
        ],
    )
    outputs[output_dir / "scaffold_comparison.tsv"] = render_tsv(
        scaffold_comparison,
        [
            "instance",
            "new_rank",
            "new_folder",
            "new_qc_status",
            "new_warning",
            "ai_status",
            "old12_overlap",
            "old12_folder",
        ],
    )

    historical_hash_rows = read_tsv(historical_hash_path)
    if len(historical_hash_rows) != EXPECTED_HISTORICAL_OUTPUT_HASH_COUNT:
        errors.append(
            f"expected {EXPECTED_HISTORICAL_OUTPUT_HASH_COUNT} historical output "
            f"hashes, found {len(historical_hash_rows)}"
        )
    historical_filenames = [row["filename"] for row in historical_hash_rows]
    if len(set(historical_filenames)) != len(historical_filenames):
        errors.append("historical output hash filenames are not unique")
    historical_hashes_verified = 0
    for row in historical_hash_rows:
        filename = PurePosixPath(row["filename"])
        if len(filename.parts) != 1 or filename.name != row["filename"]:
            errors.append(f"invalid historical output filename: {row['filename']!r}")
            continue
        rendered = outputs.get(output_dir / filename.name)
        if rendered is None:
            errors.append(f"historical output contract names unknown file: {filename.name}")
            continue
        observed_hash = sha256_text(rendered)
        if observed_hash != row["sha256"]:
            errors.append(
                f"historical output differs after migration: {filename.name} "
                f"observed={observed_hash} expected={row['sha256']}"
            )
            continue
        historical_hashes_verified += 1
    if historical_hashes_verified != EXPECTED_HISTORICAL_OUTPUT_HASH_COUNT:
        errors.append(
            f"expected {EXPECTED_HISTORICAL_OUTPUT_HASH_COUNT} verified historical "
            f"output hashes, found {historical_hashes_verified}"
        )

    overall_status = "PASS" if not errors else "FAIL"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "overall_status": overall_status,
        "source_file_count": len(source_inventory),
        "source_bytes": sum(int(row["bytes"]) for row in source_inventory),
        "source_count_semantics": {
            "historical_logical_files": len(source_inventory),
            "non_system_metadata_logical_files": sum(
                Path(row["relative_path"]).name != ".DS_Store"
                for row in source_inventory
            ),
            "system_metadata_files_excluded_from_model_input": sum(
                Path(row["relative_path"]).name == ".DS_Store"
                for row in source_inventory
            ),
            "intentional_positive_mirror_logical_files": sum(
                row["relative_path"].startswith("data/多构象-1/")
                for row in source_inventory
            ),
            "note": (
                "177 is a reproducibility count of logical inventory rows, not "
                "177 independent scientific samples; it includes one archived mirror "
                "tree and two Finder metadata files"
            ),
        },
        "structure_path_count": len(structures),
        "structure_parse_pass": sum(row["parse_status"] == "PASS" for row in structures),
        "cohort_count": len(cohorts),
        "asset_mount_count": len(mounts),
        "compatibility_aliases_verified": compatibility_aliases_verified,
        "historical_output_hashes_verified": historical_hashes_verified,
        "positive_ensemble": positive,
        "challenge_panel": negative,
        "scaffold_libraries": scaffolds,
        "errors": errors,
        "warnings": warnings,
        "semantic_contract": {
            "no_binding_directory_is_label": False,
            "experimental_negative_labels": 0,
            "positive_1d0r_independent_samples": 1,
            "challenge_target_source_groups": 4,
            "terminal_amide_atomically_proven": False,
            "allowed_claim": "computational robustness and off-target-risk proxy only",
            "forbidden_claim": "binding, non-binding, KD, or selectivity truth",
        },
        "input_registry_sha256": sha256_file(cohort_path),
        "override_registry_sha256": sha256_file(override_path),
        "asset_mount_registry_sha256": sha256_file(mount_path),
        "compatibility_alias_registry_sha256": sha256_file(alias_path),
        "historical_output_hash_registry_sha256": sha256_file(
            historical_hash_path
        ),
        "validator_sha256": sha256_file(Path(__file__).resolve()),
    }
    outputs[output_dir / "validation_summary.json"] = render_json(summary)

    if overall_status != "PASS":
        print(render_json(summary), end="")
        return 1

    mode_name = "write" if args.write else "check"
    for path, text in outputs.items():
        publish_or_check(path, text, mode_name)

    print(render_json(summary), end="")
    return 0 if overall_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
