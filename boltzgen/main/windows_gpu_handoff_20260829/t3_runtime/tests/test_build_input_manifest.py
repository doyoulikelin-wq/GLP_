from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from conftest import run_python, sha256
from test_build_design_specs import write_tsv


OUTPUT_FIELDS = [
    "asset_id", "asset_role", "source_url", "source_snapshot", "local_source_path",
    "run_copy_path", "bytes", "records", "format", "sha256", "license",
    "chemistry_status", "model_role", "allowed_in_current_run", "limitation",
    "independence_group", "target_identity", "conformer_id", "data_partition",
    "label_status", "experimental_label",
]


def fake_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def assert_derived_lineage(rows: list[dict[str, str]]) -> None:
    derived = [row for row in rows if row["license"] == "DERIVED_SEE_SOURCE_MANIFEST"]
    assert derived
    for item in derived:
        limitation = item["limitation"]
        assert re.search(r"(?:^|; )source_asset_id=[^;]+", limitation), item["asset_id"]
        assert re.search(r"(?:^|; )source_sha256=[0-9a-f]{64}(?:;|$)", limitation), item["asset_id"]
        assert (
            re.search(r"(?:^|; )transform_code_sha256=[0-9a-f]{64}(?:;|$)", limitation)
            or re.search(r"(?:^|; )frozen_validator_sha256=[0-9a-f]{64}(?:;|$)", limitation)
        ), item["asset_id"]
        assert re.search(r"(?:^|; )upstream_license=[^;]+", limitation), item["asset_id"]


def make_fixture(root: Path) -> dict[str, Path]:
    target_transform = root / "GLP_/boltzgen/main/mvp_data_assets_20260818/scripts/curate_small_sources.py"
    target_transform.parent.mkdir(parents=True)
    target_transform.write_text("# frozen fixture target curator\n", encoding="utf-8")
    scaffold_transform = root / "GLP_/boltzgen/main/sabdab2_scaffold_curation_20260819/scripts/build_scaffold_database.py"
    scaffold_transform.parent.mkdir(parents=True)
    scaffold_transform.write_text("# frozen fixture scaffold curator\n", encoding="utf-8")
    ai_validator = root / "GLP_/boltzgen/main/asset_validation_20260820/validate_assets.py"
    ai_validator.parent.mkdir(parents=True)
    ai_validator.write_text("# frozen fixture AI validator\n", encoding="utf-8")

    assets = root / "assets"
    allowlist = assets / "curated_project_inputs" / "project_input_allowlist.tsv"
    target = assets / "curated_project_inputs" / "target.cif"
    target.parent.mkdir(parents=True)
    target.write_text("data_target\n_atom_site.id\n1\n", encoding="utf-8")
    allow_rows = [
        {
            "asset": "6X18_glp1_7-36NH2_labelE_authP.cif",
            "path": "curated_project_inputs/target.cif",
            "role": "positive-target geometry",
            "status": "project_input_geometry_with_chemistry_caveat",
            "use_level": "conditional_geometry_only",
            "conditions": "terminal amide geometry only",
            "sha256": sha256(target),
        },
        {
            "asset": "9IVG_glp1_9-36_labelA_authP_observed.cif",
            "path": "curated_project_inputs/incomplete.cif",
            "role": "challenge-state geometry reference",
            "status": "blocked",
            "use_level": "blocked_until_missing_geometry_and_terminal_chemistry_are_resolved",
            "conditions": "incomplete",
            "sha256": fake_hash("incomplete"),
        },
    ]
    write_tsv(allowlist, list(allow_rows[0]), allow_rows)
    curation = assets / "curation_manifest.json"
    raw_target_sha = fake_hash("raw 6X18 fixture")
    curation.write_text(json.dumps({
        "schema_version": "1.0",
        "dataset_release_context": "fixture",
        "raw_files": [{
            "path": "raw_sources/rcsb_structures/6X18.cif",
            "source_url": "https://files.rcsb.org/download/6X18.cif",
            "format": "PDBx/mmCIF",
            "sha256": raw_target_sha,
        }],
        "curated_structure_records": [{
            "artifact_id": "GLP1_7-36NH2_6X18_peptide_only",
            "status": "project_input_geometry_with_chemistry_caveat",
            "project_role": "receptor-bound positive-target geometry",
            "source_pdb_id": "6X18",
            "source_path": "raw_sources/rcsb_structures/6X18.cif",
            "curated_path": "curated_project_inputs/target.cif",
            "source_sha256": raw_target_sha,
            "curated_sha256": sha256(target),
            "model_count": 1,
        }],
        "curated_files": [{
            "path": "curated_project_inputs/target.cif",
            "source_snapshot": "fixture-snapshot",
            "format": "mmCIF",
            "record_count": 1,
            "license": "fixture-license",
            "sha256": sha256(target),
        }],
    }, indent=2) + "\n", encoding="utf-8")

    runtime_dir = assets / "runtime_cache"
    runtime_dir.mkdir()
    runtime_files = [
        ("boltzgen1_diverse.ckpt", "generative design checkpoint; diverse design mode"),
        ("boltzgen1_adherence.ckpt", "generative design checkpoint; adherence design mode"),
        ("boltzgen1_ifold.ckpt", "inverse-folding checkpoint"),
        ("boltz2_conf_final.ckpt", "folding checkpoint"),
        ("mols.zip", "chemical dictionary"),
    ]
    runtime_rows = []
    for filename, role in runtime_files:
        path = runtime_dir / filename
        path.write_bytes((filename + "\n").encode())
        runtime_rows.append({
            "filename": filename, "role": role, "format": "fixture", "bytes": path.stat().st_size,
            "sha256": sha256(path), "source_url": f"https://example.invalid/{filename}",
        })
    runtime_manifest = runtime_dir / "runtime_manifest.json"
    runtime_manifest.write_text(json.dumps({
        "schema_version": "1.0", "asset_set": "fixture", "boltzgen_release": "v0.3.2",
        "pinned_sources": {"model_repository": {"revision": "fixture-model"},
                           "chemical_dictionary_repository": {"revision": "fixture-mols"}},
        "files": runtime_rows,
    }, indent=2) + "\n", encoding="utf-8")

    scaffold_root = root / "scaffold_db"
    database_summary = scaffold_root / "registry" / "database_summary.json"
    database_summary.parent.mkdir(parents=True)
    database_summary.write_text(json.dumps({
        "schema_version": "1.0.0",
        "source_release": {
            "release_id": "sabdab2_sd_h_fixture",
            "license": "CC BY 4.0",
        },
    }) + "\n", encoding="utf-8")
    selected_rows = []
    export_rows = []
    for index in range(1, 13):
        candidate = f"pdb_fixture_{index:02d}-A"
        package = f"{index:02d}_{candidate}"
        directory = scaffold_root / "selected" / package
        directory.mkdir(parents=True)
        cif = directory / "scaffold.cif"
        yml = directory / "scaffold.yaml"
        source = directory / "source_rcsb_original.cif"
        curation_record = directory / "curation.json"
        cif.write_text(f"data_scaffold_{index}\n_atom_site.id\n1\n", encoding="utf-8")
        yml.write_text("path: scaffold.cif\n", encoding="utf-8")
        source.write_text(f"data_source_scaffold_{index}\n", encoding="utf-8")
        curation_record.write_text(json.dumps({
            "candidate_id": candidate,
            "source": {"sabdab2_archive_member": f"raw/{candidate}.cif"},
        }) + "\n", encoding="utf-8")
        selected_rows.append({
            "selection_rank": index, "role": "PRIMARY" if index <= 10 else "RESERVE",
            "candidate_id": candidate, "cdr1_length_aa": 8, "cdr2_length_aa": 7,
            "cdr3_length_aa": 11, "package_path": f"selected/{package}",
            "boltzgen_check_status": "PASS",
        })
        export_rows.append({
            "candidate_id": candidate,
            "normalized_cif_path": f"selected/{package}/scaffold.cif",
            "normalized_cif_sha256": sha256(cif),
            "scaffold_yaml_path": f"selected/{package}/scaffold.yaml",
            "scaffold_yaml_sha256": sha256(yml),
            "curation_json_path": f"selected/{package}/curation.json",
            "boltzgen_check_status": "PASS",
        })
    selected = scaffold_root / "registry" / "selected_scaffolds.tsv"
    exports = scaffold_root / "registry" / "export_artifacts.tsv"
    write_tsv(selected, list(selected_rows[0]), selected_rows)
    write_tsv(exports, list(export_rows[0]), export_rows)
    criteria = scaffold_root / "criteria" / "scaffold_screening_v1.json"
    criteria.parent.mkdir()
    criteria.write_text(json.dumps({"schema_version": "1.0", "fixture": True}) + "\n", encoding="utf-8")

    registry = root / "ai_contract"
    cohort_rows = []
    cohort_specs = [
        ("positive_1d0r_all", "1D0R", "data/positive/*.cif", "positive_geometry_sensitivity_ensemble", "USE_SENSITIVITY", "geometry_only"),
        ("countertarget_glp1_9_36_9ivm", "9IVM", "data/tuning/9IVM_*.cif", "primary_truncation_challenge", "USE_TUNING_CHALLENGE", "not_explicit"),
        ("challenge_glp2_2l63", "2L63", "data/tuning/2L63_*.cif", "secondary_family_tuning_challenge", "USE_TUNING_CHALLENGE", "not_explicit"),
        ("challenge_gip_2b4n", "2B4N_OR_7DTY", "data/*/2B4N_*.cif", "family_offtarget_lockbox_challenge", "USE_LOCKBOX_CHALLENGE", "not_explicit"),
        ("challenge_glucagon", "6LMK_OR_6PHI", "data/*/6LMK_*.cif", "family_offtarget_lockbox_challenge", "USE_LOCKBOX_CHALLENGE", "not_explicit"),
        ("countertarget_glp1_9_36_9ivg_incomplete", "9IVG", "data/excluded/9IVG.cif", "incomplete_audit_only", "EXCLUDE_INCOMPLETE", "not_observed"),
        ("challenge_oxyntomodulin_incomplete", "9N0E", "data/excluded/9N0E.cif", "incomplete_audit_only", "EXCLUDE_INCOMPLETE", "not_observed"),
        ("scaffolds_new17", "SAbDab2_stage2_17", "data/scaffolds/new/[0-9][0-9]_*/scaffold.cif", "challenger_scaffold_library", "PENDING_CANONICALIZATION_AND_CHECK", "not_applicable"),
    ]
    for cohort_id, source_id, canonical_glob, ai_role, default_status, chemistry in cohort_specs:
        cohort_rows.append({
            "cohort_id": cohort_id, "asset_class": "peptide_target", "source_id": source_id,
            "canonical_glob": canonical_glob,
            "ai_role": ai_role, "default_status": default_status,
            "terminal_chemistry_claim": chemistry, "limitations": "fixture limitation",
        })
    cohorts = registry / "cohort_registry.tsv"
    write_tsv(cohorts, list(cohort_rows[0]), cohort_rows)

    inventory_rows = []
    override_rows = []
    for model in (10, 12, 19, 20):
        relative = f"data/positive/1D0R_model{model}.cif"
        local = root / relative
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(f"data_positive_{model}\n", encoding="utf-8")
        compact_status = "USE_POSITIVE_FIXED_CONTROL" if model == 10 else "USE_POSITIVE_COMPACT"
        inventory_rows.append({
            "cohort_id": "positive_1d0r_all", "asset_class": "peptide_target_ensemble",
            "source_id": "1D0R", "relative_path": relative, "bytes": local.stat().st_size,
            "sha256": sha256(local), "ai_role": "positive_geometry_sensitivity_ensemble",
            "status": compact_status, "active_for_ai": "true", "experimental_negative": "false",
            "binding_label": "unknown_or_not_applicable",
            "independence_group": "PDB:1D0R", "terminal_chemistry_claim": "geometry_only",
            "parse_status": "PASS", "validation_status": "PASS", "geometry_complete": "true",
            "model_count": "1", "model_numbers": str(model), "source_pdb": "1D0R",
            "limitation_or_reason": "fixture",
        })
        override_rows.append({
            "relative_path": relative,
            "status": compact_status,
            "reason": "fixture compact panel",
        })
    challenge_groups = [
        ("countertarget_glp1_9_36_9ivm", "9IVM", "9IVM", "primary_truncation_challenge", 1, "PDB:9IVM", "tuning"),
        ("challenge_glp2_2l63", "2L63", "2L63", "secondary_family_tuning_challenge", 10, "PDB:2L63", "tuning"),
        ("challenge_gip_2b4n", "2B4N_OR_7DTY", "2B4N", "family_offtarget_lockbox_challenge", 20, "PDB:2B4N", "lockbox"),
        ("challenge_glucagon", "6LMK_OR_6PHI", "6LMK", "family_offtarget_lockbox_challenge", 1, "PDB:6LMK", "lockbox"),
    ]
    for cohort_id, cohort_source, source_pdb, ai_role, count, group, partition in challenge_groups:
        for index in range(1, count + 1):
            relative = f"data/{partition}/{source_pdb}_conf{index:02d}.cif"
            local = root / relative
            if partition == "tuning":
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(f"data_tuning_{source_pdb}_{index}\n", encoding="utf-8")
                byte_count = local.stat().st_size
                file_sha = sha256(local)
            else:
                byte_count = 200 + index
                file_sha = fake_hash(relative)
            inventory_rows.append({
                "cohort_id": cohort_id, "asset_class": "peptide_challenge", "source_id": cohort_source,
                "relative_path": relative, "bytes": byte_count, "sha256": file_sha,
                "ai_role": ai_role, "status": "USE_TUNING_CHALLENGE" if partition == "tuning" else "USE_LOCKBOX_CHALLENGE",
                "active_for_ai": "true", "experimental_negative": "false",
                "binding_label": "unknown_or_not_applicable", "independence_group": group,
                "terminal_chemistry_claim": "not_explicit", "parse_status": "PASS",
                "validation_status": "PASS", "geometry_complete": "true", "model_count": "1",
                "model_numbers": str(index), "source_pdb": source_pdb, "limitation_or_reason": "fixture",
            })
    for source_id in ("9IVG", "9N0E", "6PHI", "7DTY"):
        incomplete_contract = {
            "9IVG": (
                "countertarget_glp1_9_36_9ivg_incomplete",
                "9IVG",
                "incomplete_audit_only",
                "not_observed",
                "9IVG.cif",
            ),
            "9N0E": (
                "challenge_oxyntomodulin_incomplete",
                "9N0E",
                "incomplete_audit_only",
                "not_observed",
                "9N0E.cif",
            ),
            "6PHI": (
                "challenge_glucagon",
                "6LMK_OR_6PHI",
                "family_offtarget_lockbox_challenge",
                "not_explicit",
                "6LMK_6PHI.cif",
            ),
            "7DTY": (
                "challenge_gip_2b4n",
                "2B4N_OR_7DTY",
                "family_offtarget_lockbox_challenge",
                "not_explicit",
                "2B4N_7DTY.cif",
            ),
        }[source_id]
        relative = f"data/excluded/{incomplete_contract[4]}"
        inventory_rows.append({
            "cohort_id": incomplete_contract[0], "asset_class": "peptide_challenge",
            "source_id": incomplete_contract[1], "relative_path": relative, "bytes": 1,
            "sha256": fake_hash(relative), "ai_role": incomplete_contract[2],
            "status": "EXCLUDE_INCOMPLETE", "active_for_ai": "false", "experimental_negative": "false",
            "binding_label": "unknown_or_not_applicable",
            "independence_group": f"PDB:{source_id}", "terminal_chemistry_claim": incomplete_contract[3],
            "parse_status": "PASS", "validation_status": "PASS", "geometry_complete": "false",
            "model_count": "1", "model_numbers": "1", "source_pdb": source_id,
            "limitation_or_reason": "incomplete fixture",
        })
        override_rows.append({"relative_path": relative, "status": "EXCLUDE_INCOMPLETE", "reason": "fixture"})
    overrides = registry / "file_overrides.tsv"

    ai_outputs = root / "ai_outputs"
    structures = ai_outputs / "structure_inventory.tsv"
    comparison_rows = []
    overlap_indices = {2, 6, 9, 14}
    for index in range(1, 18):
        overlap = index in overlap_indices
        comparison_rows.append({
            "instance": f"pdb_new_{index:02d}-A", "new_rank": index,
            "new_folder": f"{index:02d}_pdb_new_{index:02d}-A", "new_qc_status": "pass",
            "new_warning": "", "ai_status": "DUPLICATE_ALIAS_USE_OLD_CANONICAL" if overlap else "PENDING_CANONICALIZATION_AND_CHECK",
            "old12_overlap": "true" if overlap else "false",
            "old12_folder": f"{index:02d}_old" if overlap else "",
        })
        relative = f"data/scaffolds/new/{index:02d}_pdb_new_{index:02d}-A/scaffold.cif"
        inventory_rows.append({
            "cohort_id": "scaffolds_new17", "asset_class": "vhh_scaffold",
            "source_id": "SAbDab2_stage2_17", "relative_path": relative,
            "bytes": 300 + index, "sha256": fake_hash(relative),
            "ai_role": "challenger_scaffold_library", "status": comparison_rows[-1]["ai_status"],
            "active_for_ai": "false", "experimental_negative": "false",
            "binding_label": "unknown_or_not_applicable",
            "independence_group": f"SCAFFOLD:pdb_new_{index:02d}-A",
            "terminal_chemistry_claim": "not_applicable", "parse_status": "PASS",
            "validation_status": "PASS", "geometry_complete": "true", "model_count": "1",
            "model_numbers": "1", "source_pdb": f"NEW{index:02d}",
            "limitation_or_reason": "fixture challenger is inactive",
        })
        if comparison_rows[-1]["ai_status"] != "PENDING_CANONICALIZATION_AND_CHECK":
            override_rows.append(
                {
                    "relative_path": relative,
                    "status": comparison_rows[-1]["ai_status"],
                    "reason": "fixture challenger overlap",
                }
            )
    write_tsv(overrides, list(override_rows[0]), override_rows)
    write_tsv(structures, list(inventory_rows[0]), inventory_rows)
    scaffolds = ai_outputs / "scaffold_comparison.tsv"
    write_tsv(scaffolds, list(comparison_rows[0]), comparison_rows)
    historical = registry / "historical_output_hashes.tsv"
    historical_rows = [
        {"filename": "source_file_inventory.tsv", "sha256": fake_hash("source inventory"), "contract": "fixture"},
        {"filename": "structure_inventory.tsv", "sha256": sha256(structures), "contract": "fixture"},
        {"filename": "duplicate_groups.tsv", "sha256": fake_hash("duplicates"), "contract": "fixture"},
        {"filename": "cohort_summary.tsv", "sha256": fake_hash("cohort summary"), "contract": "fixture"},
        {"filename": "scaffold_comparison.tsv", "sha256": sha256(scaffolds), "contract": "fixture"},
    ]
    write_tsv(historical, list(historical_rows[0]), historical_rows)
    validation = ai_outputs / "validation_summary.json"
    validation.write_text(json.dumps({
        "schema_version": "AI_VALIDATION_ASSET_REGISTRY_V2", "overall_status": "PASS",
        "source_file_count": len(inventory_rows), "source_bytes": sum(int(row["bytes"]) for row in inventory_rows),
        "source_count_semantics": {
            "historical_logical_files": len(inventory_rows),
            "non_system_metadata_logical_files": len(inventory_rows),
            "system_metadata_files_excluded_from_model_input": 0,
            "intentional_positive_mirror_logical_files": 0,
        },
        "structure_path_count": len(inventory_rows),
        "structure_parse_pass": sum(row["parse_status"] == "PASS" for row in inventory_rows),
        "cohort_count": len(cohort_rows), "asset_mount_count": 1,
        "compatibility_aliases_verified": 0, "historical_output_hashes_verified": 5,
        "input_registry_sha256": sha256(cohorts), "override_registry_sha256": sha256(overrides),
        "asset_mount_registry_sha256": fake_hash("asset mounts"),
        "compatibility_alias_registry_sha256": fake_hash("aliases"),
        "historical_output_hash_registry_sha256": sha256(historical),
        "validator_sha256": sha256(ai_validator), "errors": [], "warnings": [],
        "positive_ensemble": {"compact_panel_models": [10, 12, 19, 20], "compact_panel_paths_verified": 4,
                              "active_representative_aliases": 0},
        "challenge_panel": {"usable_challenge_conformers": 32, "usable_target_source_groups": 4,
                            "experimental_negative_labels": 0},
        "scaffold_libraries": {"new_scaffold_packages": 17, "old12_instance_overlaps": 4,
                               "overlap_use_old_canonical": 4, "production_active_from_new17": 0},
        "semantic_contract": {"experimental_negative_labels": 0, "challenge_target_source_groups": 4,
                              "no_binding_directory_is_label": False},
    }, indent=2) + "\n", encoding="utf-8")
    return {
        "allowlist": allowlist, "curation": curation, "runtime": runtime_manifest,
        "selected": selected, "exports": exports, "criteria": criteria, "cohorts": cohorts,
        "overrides": overrides, "structures": structures, "scaffolds": scaffolds,
        "validation": validation, "historical": historical,
    }


def invoke(paths: dict[str, Path], output: Path):
    return run_python(
        "build_input_manifest.py",
        "--allowlist", paths["allowlist"], "--curation-manifest", paths["curation"],
        "--runtime-manifest", paths["runtime"], "--selected-scaffolds", paths["selected"],
        "--export-artifacts", paths["exports"], "--screening-criteria", paths["criteria"],
        "--ai-cohorts", paths["cohorts"], "--ai-overrides", paths["overrides"],
        "--ai-structures", paths["structures"], "--ai-scaffolds", paths["scaffolds"],
        "--ai-validation-summary", paths["validation"], "--output", output,
    )


def test_builds_allowlist_only_manifest_with_frozen_counts(tmp_path: Path) -> None:
    paths = make_fixture(tmp_path)
    output = tmp_path / "source_manifest.tsv"
    result = invoke(paths, output)
    assert result.returncode == 0, result.stderr
    with output.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = list(reader)
        assert reader.fieldnames == OUTPUT_FIELDS
    assert len([row for row in rows if row["model_role"] == "primary_target"]) == 1
    assert len([row for row in rows if row["model_role"] == "baseline_scaffold"]) == 12
    assert [row["asset_role"] for row in rows].count("design_checkpoint") == 2
    assert [row["asset_role"] for row in rows].count("inverse_fold_checkpoint") == 1
    assert [row["asset_role"] for row in rows].count("folding_checkpoint") == 1
    assert len([row for row in rows if row["data_partition"] == "positive_compact"]) == 4
    assert len([row for row in rows if row["data_partition"] == "tuning_challenge"]) == 11
    assert len([row for row in rows if row["data_partition"] == "lockbox"]) == 21
    assert all(row["allowed_in_current_run"] == "false" for row in rows if row["data_partition"] == "lockbox")
    assert all(row["local_source_path"] == "" for row in rows if row["data_partition"] == "lockbox")
    assert all(row["experimental_label"] == "" for row in rows)
    assert not any("raw_sources/" in row["local_source_path"] for row in rows)
    assert not any(row["data_partition"] == "challenger_scaffold" and row["allowed_in_current_run"] == "true" for row in rows)
    assert_derived_lineage(rows)
    assert any(
        "upstream_license=UNSPECIFIED_IN_SUPPLIED_SOURCE_CONTRACT" in row["limitation"]
        for row in rows
        if row["data_partition"] in {"positive_compact", "tuning_challenge", "lockbox"}
    )


def test_rejects_any_drift_in_exact_6x18_allowlist_and_curation_contract(tmp_path: Path) -> None:
    mutations = (
        ("allowlist", "asset", "6X18_other.cif"),
        ("allowlist", "role", "challenge-state geometry reference"),
        ("allowlist", "status", "blocked"),
        ("allowlist", "use_level", "blocked"),
        ("curation", "project_role", "blocked role"),
    )
    for index, (location, field, value) in enumerate(mutations):
        case = tmp_path / f"case_{index}"
        paths = make_fixture(case)
        if location == "allowlist":
            rows = read_tsv(paths["allowlist"])
            rows[0][field] = value
            write_tsv(paths["allowlist"], list(rows[0]), rows)
        else:
            payload = json.loads(paths["curation"].read_text(encoding="utf-8"))
            payload["curated_structure_records"][0][field] = value
            paths["curation"].write_text(json.dumps(payload) + "\n", encoding="utf-8")
        output = case / "blocked.tsv"
        result = invoke(paths, output)
        assert result.returncode != 0, (field, result.stdout, result.stderr)
        assert not output.exists()


def test_validation_summary_hashes_counts_and_historical_output_bindings_are_mandatory(
    tmp_path: Path,
) -> None:
    for index, field in enumerate((
        "source_file_count", "structure_path_count", "cohort_count",
        "input_registry_sha256", "override_registry_sha256",
        "historical_output_hash_registry_sha256", "validator_sha256",
    )):
        case = tmp_path / f"missing_{index}"
        paths = make_fixture(case)
        summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
        summary.pop(field)
        paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
        result = invoke(paths, case / "blocked.tsv")
        assert result.returncode != 0, field

    for index, field in enumerate((
        "input_registry_sha256", "override_registry_sha256",
        "historical_output_hash_registry_sha256", "validator_sha256",
    )):
        case = tmp_path / f"hash_{index}"
        paths = make_fixture(case)
        summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
        summary[field] = fake_hash(f"wrong {field}")
        paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
        result = invoke(paths, case / "blocked.tsv")
        assert result.returncode != 0, field

    case = tmp_path / "historical_entry"
    paths = make_fixture(case)
    historical = read_tsv(paths["historical"])
    next(row for row in historical if row["filename"] == "structure_inventory.tsv")["sha256"] = fake_hash("wrong")
    write_tsv(paths["historical"], list(historical[0]), historical)
    summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
    summary["historical_output_hash_registry_sha256"] = sha256(paths["historical"])
    paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
    result = invoke(paths, case / "blocked.tsv")
    assert result.returncode != 0


def test_allowed_ai_files_and_compact_override_status_active_and_label_are_closed(tmp_path: Path) -> None:
    mutations = (
        ("structure", "status", "USE_SENSITIVITY"),
        ("structure", "active_for_ai", "false"),
        ("structure", "binding_label", "experimental_binder"),
        ("override", "status", "USE_POSITIVE_COMPACT"),
    )
    for index, (where, field, value) in enumerate(mutations):
        case = tmp_path / f"contract_{index}"
        paths = make_fixture(case)
        path = paths["structures"] if where == "structure" else paths["overrides"]
        rows = read_tsv(path)
        target = next(row for row in rows if row["relative_path"].endswith("1D0R_model10.cif"))
        target[field] = value
        write_tsv(path, list(rows[0]), rows)
        summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
        if where == "structure":
            historical = read_tsv(paths["historical"])
            next(row for row in historical if row["filename"] == "structure_inventory.tsv")["sha256"] = sha256(path)
            write_tsv(paths["historical"], list(historical[0]), historical)
            summary["historical_output_hash_registry_sha256"] = sha256(paths["historical"])
        else:
            summary["override_registry_sha256"] = sha256(path)
        paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
        result = invoke(paths, case / "blocked.tsv")
        assert result.returncode != 0, (where, field, result.stderr)

    case = tmp_path / "file_drift"
    paths = make_fixture(case)
    (case / "data/positive/1D0R_model12.cif").write_text("tampered\n", encoding="utf-8")
    result = invoke(paths, case / "blocked.tsv")
    assert result.returncode != 0


def test_publication_is_no_replace_and_rejects_parent_or_leaf_symlinks(tmp_path: Path) -> None:
    paths = make_fixture(tmp_path / "fixture")
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias_parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    through_parent = invoke(paths, alias_parent / "source_manifest.tsv")
    assert through_parent.returncode != 0
    assert not (real_parent / "source_manifest.tsv").exists()

    leaf_target = tmp_path / "leaf_target.tsv"
    leaf_target.write_text("do not overwrite\n", encoding="utf-8")
    leaf = tmp_path / "leaf.tsv"
    leaf.symlink_to(leaf_target)
    through_leaf = invoke(paths, leaf)
    assert through_leaf.returncode != 0
    assert leaf_target.read_text(encoding="utf-8") == "do not overwrite\n"
    assert "os.replace(" not in (Path(__file__).resolve().parents[1] / "build_input_manifest.py").read_text(encoding="utf-8")


def test_missing_frozen_transformation_code_fails_closed(tmp_path: Path) -> None:
    paths = make_fixture(tmp_path)
    (tmp_path / "GLP_/boltzgen/main/mvp_data_assets_20260818/scripts/curate_small_sources.py").unlink()
    result = invoke(paths, tmp_path / "blocked.tsv")
    assert result.returncode != 0


def test_rejects_registry_failure_or_raw_allowlist_path(tmp_path: Path) -> None:
    paths = make_fixture(tmp_path)
    summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
    summary["overall_status"] = "FAIL"
    paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
    output = tmp_path / "bad.tsv"
    result = invoke(paths, output)
    assert result.returncode != 0
    assert not output.exists()

    paths = make_fixture(tmp_path / "raw_case")
    rows = list(csv.DictReader(paths["allowlist"].open(newline="", encoding="utf-8"), delimiter="\t"))
    rows[0]["path"] = "raw_sources/forbidden.cif"
    write_tsv(paths["allowlist"], list(rows[0]), rows)
    output = tmp_path / "raw.tsv"
    result = invoke(paths, output)
    assert result.returncode != 0
    assert not output.exists()


def test_rejects_incomplete_cohort_cross_checks_and_noncanonical_paths(
    tmp_path: Path,
) -> None:
    case = tmp_path / "cohort_role"
    paths = make_fixture(case)
    cohorts = read_tsv(paths["cohorts"])
    next(
        item for item in cohorts if item["cohort_id"] == "challenge_glp2_2l63"
    )["ai_role"] = "wrong_but_safe_role"
    write_tsv(paths["cohorts"], list(cohorts[0]), cohorts)
    summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
    summary["input_registry_sha256"] = sha256(paths["cohorts"])
    paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
    result = invoke(paths, case / "blocked.tsv")
    assert result.returncode != 0

    case = tmp_path / "unrepresented_cohort"
    paths = make_fixture(case)
    cohorts = read_tsv(paths["cohorts"])
    extra = dict(cohorts[0])
    extra.update(
        cohort_id="unused_safe_cohort",
        source_id="UNUSED",
        canonical_glob="data/unused/*.cif",
        ai_role="unused_role",
        default_status="EXCLUDE_INCOMPLETE",
    )
    cohorts.append(extra)
    write_tsv(paths["cohorts"], list(cohorts[0]), cohorts)
    summary = json.loads(paths["validation"].read_text(encoding="utf-8"))
    summary["input_registry_sha256"] = sha256(paths["cohorts"])
    summary["cohort_count"] = len(cohorts)
    paths["validation"].write_text(json.dumps(summary) + "\n", encoding="utf-8")
    result = invoke(paths, case / "blocked.tsv")
    assert result.returncode != 0


def test_rejects_intermediate_source_symlink_hops(tmp_path: Path) -> None:
    project = tmp_path / "project"
    paths = make_fixture(project)
    tuning = project / "data/tuning"
    real_tuning = tmp_path / "outside_tuning"
    tuning.rename(real_tuning)
    tuning.symlink_to(real_tuning, target_is_directory=True)

    result = invoke(paths, project / "blocked.tsv")
    assert result.returncode != 0
    assert not (project / "blocked.tsv").exists()


def test_real_frozen_registries_match_the_declared_input_contract(tmp_path: Path) -> None:
    """Exercise the production schemas; the synthetic curation schema is richer."""
    creator_root = Path(__file__).resolve().parents[6]
    asset_root = creator_root / "data/boltzgen_data/mvp_assets_v0.3.2"
    scaffold_root = creator_root / "data/boltzgen_data/sabdab2_vhh_scaffolds_v1"
    ai_contract = creator_root / "GLP_/boltzgen/resources/data/AI结构资产验证登记册_20260828"
    ai_output = creator_root / "boltzgen/data/ai_structure_asset_validation_registry_20260828_211504"
    paths = {
        "allowlist": asset_root / "curated_project_inputs/project_input_allowlist.tsv",
        "curation": asset_root / "curation_manifest.json",
        "runtime": asset_root / "runtime_cache/runtime_manifest.json",
        "selected": scaffold_root / "registry/selected_scaffolds.tsv",
        "exports": scaffold_root / "registry/export_artifacts.tsv",
        "criteria": scaffold_root / "criteria/scaffold_screening_v1.json",
        "cohorts": ai_contract / "cohort_registry.tsv",
        "overrides": ai_contract / "file_overrides.tsv",
        "structures": ai_output / "structure_inventory.tsv",
        "scaffolds": ai_output / "scaffold_comparison.tsv",
        "validation": ai_output / "validation_summary.json",
    }
    assert all(path.is_file() for path in paths.values())
    output = tmp_path / "real_source_manifest.tsv"
    result = invoke(paths, output)
    assert result.returncode == 0, result.stderr
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    target = [row for row in rows if row["model_role"] == "primary_target"]
    assert [(row["asset_id"], row["chemistry_status"]) for row in target] == [
        ("GLP1_7-36_NH2", "geometry_only")
    ]
    assert len([row for row in rows if row["model_role"] == "baseline_scaffold"]) == 12
    assert len([row for row in rows if row["data_partition"] == "positive_compact"]) == 4
    assert len([row for row in rows if row["data_partition"] == "tuning_challenge"]) == 11
    assert len([row for row in rows if row["data_partition"] == "lockbox"]) == 21
    assert len([row for row in rows if row["data_partition"] == "incomplete_quarantine"]) == 4
    assert len([row for row in rows if row["data_partition"] == "challenger_scaffold"]) == 17
    assert sum(row["experimental_label"] != "" for row in rows) == 0
    assert {row["license"] for row in rows if row["model_role"] == "runtime_asset"} == {"MIT"}
    assert all(
        row["license"] == "DERIVED_SEE_SOURCE_MANIFEST"
        for row in rows
        if row["model_role"] != "runtime_asset"
    )
    assert all(
        row["allowed_in_current_run"] == "false"
        for row in rows
        if row["data_partition"] in {"lockbox", "incomplete_quarantine", "challenger_scaffold"}
    )
    assert not any("raw_sources/" in row["local_source_path"] for row in rows)
    assert_derived_lineage(rows)
