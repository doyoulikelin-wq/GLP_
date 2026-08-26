#!/usr/bin/env python3
"""Validate and inventory the local BoltzGen AI-validation assets.

Run with the frozen project interpreter that already contains Gemmi/PyYAML:

    data/boltzgen_data/mvp_run_001/env/bin/python -I \
      data/boltzgen_data/ai_validation_assets_v1/validate_assets.py --write

The script never edits source data.  ``--write`` atomically refreshes only the
derived TSV/JSON files beside this script; ``--check`` compares a fresh in-memory
render with the committed derived files.
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
from pathlib import Path
from typing import Any, Iterable

import gemmi
import yaml


SCHEMA_VERSION = "AI_VALIDATION_ASSET_REGISTRY_V1"
SOURCE_ROOTS = (
    "data/样本数据",
    "data/not_binding",
    "data/多构象-1",
    "data/sd-h骨架",
)
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


def relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


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
    project_root: Path,
    cohort_rows: list[dict[str, str]],
    overrides: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    structures: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    for cohort in cohort_rows:
        matches = [
            Path(path)
            for path in sorted(glob.glob(str(project_root / cohort["canonical_glob"])))
            if Path(path).is_file()
        ]
        expected_count = int(cohort["expected_file_count"])
        if len(matches) != expected_count:
            errors.append(
                f"{cohort['cohort_id']}: expected {expected_count} files, found {len(matches)}"
            )

        status_counts: Counter[str] = Counter()
        parse_pass = 0
        complete_count = 0
        for path in matches:
            relative = relpath(path, project_root)
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


def inventory_source_files(project_root: Path) -> list[dict[str, Any]]:
    paths: set[Path] = set()
    for relative_root in SOURCE_ROOTS:
        source_root = project_root / relative_root
        if source_root.is_dir():
            paths.update(path for path in source_root.rglob("*") if path.is_file())
    extra_paths = (
        project_root
        / "data/boltzgen_data/mvp_assets_v0.3.2/raw_sources/rcsb_structures/1D0R.cif",
        project_root
        / "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/inputs/target/6X18_GLP1_7-36_geometry.cif",
    )
    paths.update(path for path in extra_paths if path.is_file())

    rows: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: relpath(item, project_root)):
        relative = relpath(path, project_root)
        digest = sha256_file(path)
        rows.append(
            {
                "relative_path": relative,
                "bytes": path.stat().st_size,
                "suffix": path.suffix.lower(),
                "sha256": digest,
                "inventory_scope": next(
                    (
                        source_root
                        for source_root in SOURCE_ROOTS
                        if relative == source_root or relative.startswith(source_root + "/")
                    ),
                    "explicit_baseline_or_raw_reference",
                ),
                "excluded_from_model_input": str(
                    path.name == ".DS_Store" or "原始文件" in path.parts
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
    project_root: Path, structures: list[dict[str, Any]], errors: list[str]
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

    raw_path = (
        project_root
        / "data/boltzgen_data/mvp_assets_v0.3.2/raw_sources/rcsb_structures/1D0R.cif"
    )
    expected_raw_hash = "efa9eefd43129b2b2c1124a5da3099c8abb315f017560d1e678fa354b2a2bcf8"
    raw_hash_match = raw_path.is_file() and sha256_file(raw_path) == expected_raw_hash
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
    project_root: Path, structures: list[dict[str, Any]], errors: list[str]
) -> dict[str, Any]:
    manifest_path = (
        project_root
        / "data/样本数据/not_binding/not_binding/structure_manifest.csv"
    )
    run_summary_path = (
        project_root / "data/样本数据/not_binding/not_binding/run_summary.json"
    )
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

        raw = project_root / "data/样本数据/not_binding/原始文件" / f"{manifest['pdb_id']}.cif"
        if raw.is_file() and sha256_file(raw) == manifest["raw_sha256"]:
            raw_hash_matches += 1

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


def verify_scaffolds(
    project_root: Path, structures: list[dict[str, Any]], errors: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    library_root = project_root / "data/样本数据/boltzgen_vhh_scaffolds"
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
        relative_cif = relpath(cif_path, project_root)
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

    checksum_entries = parse_checksum_manifest(library_root / "checksums.sha256")
    checksum_verified = 0
    for expected, relative in checksum_entries:
        path = library_root / relative
        if not path.is_file() or sha256_file(path) != expected:
            errors.append(f"new scaffold checksums.sha256 mismatch: {relative}")
        else:
            checksum_verified += 1

    old_root = project_root / "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/selected"
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
        "checksum_entries_total": len(checksum_entries),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args()

    output_dir = Path(__file__).resolve().parent
    project_root = (
        args.project_root.resolve()
        if args.project_root
        else output_dir.parents[2].resolve()
    )
    if not (project_root / "data").is_dir():
        raise SystemExit(f"invalid project root: {project_root}")
    cohort_path = output_dir / "cohort_registry.tsv"
    override_path = output_dir / "file_overrides.tsv"
    cohorts = read_tsv(cohort_path)
    overrides = {
        row["relative_path"]: row for row in read_tsv(override_path)
    }

    structures, cohort_summaries, errors, warnings = expand_cohorts(
        project_root, cohorts, overrides
    )
    source_inventory = inventory_source_files(project_root)
    duplicate_groups = build_duplicate_groups(source_inventory)
    positive = verify_positive_aliases(project_root, structures, errors)
    negative = verify_negative_manifest(project_root, structures, errors)
    scaffold_comparison, scaffolds = verify_scaffolds(
        project_root, structures, errors
    )

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

    overall_status = "PASS" if not errors else "FAIL"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "overall_status": overall_status,
        "source_file_count": len(source_inventory),
        "source_bytes": sum(int(row["bytes"]) for row in source_inventory),
        "structure_path_count": len(structures),
        "structure_parse_pass": sum(row["parse_status"] == "PASS" for row in structures),
        "cohort_count": len(cohorts),
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
        "validator_sha256": sha256_file(Path(__file__).resolve()),
    }
    outputs[output_dir / "validation_summary.json"] = render_json(summary)

    mode_name = "write" if args.write else "check"
    for path, text in outputs.items():
        publish_or_check(path, text, mode_name)

    print(render_json(summary), end="")
    return 0 if overall_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
