from __future__ import annotations

import csv
from pathlib import Path

import yaml

from conftest import run_python, sha256


MANIFEST_FIELDS = [
    "spec_id", "scaffold_id", "scaffold_role", "target_id", "target_chain",
    "binding_label_seq_ids", "cdr1_range", "cdr2_range", "cdr3_range",
    "cdr1_length", "cdr2_length", "cdr3_length", "spec_path",
    "spec_sha256", "scaffold_sha256", "target_sha256",
]


def write_tsv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    from test_verify_specs import TARGET_SEQUENCE, mmcif_bytes

    target = root / "target.cif"
    target.write_bytes(mmcif_bytes([("E", TARGET_SEQUENCE)]))
    scaffold_root = root / "scaffolds"
    selected_rows: list[dict[str, object]] = []
    export_rows: list[dict[str, object]] = []
    for index in range(1, 13):
        candidate = f"pdb_fixture_{index:02d}-A"
        package = f"{index:02d}_{candidate}"
        package_dir = scaffold_root / package
        package_dir.mkdir(parents=True)
        scaffold = package_dir / "scaffold.cif"
        scaffold.write_bytes(mmcif_bytes([("A", "A" * 120)]))
        scaffold_yaml = package_dir / "scaffold.yaml"
        scaffold_yaml.write_text(
            "path: scaffold.cif\n"
            "include:\n- chain:\n    id: A\n"
            "design:\n- chain:\n    id: A\n    res_index: 26..33,51..57,96..106\n"
            "structure_groups:\n"
            "- group:\n    id: A\n    visibility: 2\n"
            "- group:\n    id: A\n    visibility: 0\n"
            "    res_index: 26..33,51..57,96..106\n"
            "reset_res_index:\n- chain:\n    id: A\n",
            encoding="utf-8",
        )
        selected_rows.append({
            "selection_rank": index,
            "role": "PRIMARY" if index <= 10 else "RESERVE",
            "candidate_id": candidate,
            "cdr1_length_aa": 8,
            "cdr2_length_aa": 7,
            "cdr3_length_aa": 11,
            "package_path": f"selected/{package}",
            "boltzgen_check_status": "PASS",
        })
        export_rows.append({
            "candidate_id": candidate,
            "normalized_cif_path": f"selected/{package}/scaffold.cif",
            "normalized_cif_sha256": sha256(scaffold),
            "scaffold_yaml_path": f"selected/{package}/scaffold.yaml",
            "scaffold_yaml_sha256": sha256(scaffold_yaml),
            "target_cif_sha256": sha256(target),
            "boltzgen_check_status": "PASS",
        })
    selected = root / "selected_scaffolds.tsv"
    exports = root / "export_artifacts.tsv"
    write_tsv(selected, list(selected_rows[0]), selected_rows)
    write_tsv(exports, list(export_rows[0]), export_rows)
    return target, scaffold_root, selected, exports


def invoke(root: Path):
    target, scaffold_root, selected, exports = build_fixture(root)
    output_root = root / "specs"
    manifest = root / "spec_manifest.tsv"
    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", output_root,
        "--manifest", manifest,
    )
    return result, target, output_root, manifest


def test_builds_twelve_deterministic_safe_specs(tmp_path: Path) -> None:
    result, target, output_root, manifest = invoke(tmp_path)
    assert result.returncode == 0, result.stderr
    with manifest.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = list(reader)
        assert reader.fieldnames == MANIFEST_FIELDS
    assert len(rows) == 12
    assert [row["scaffold_role"] for row in rows].count("PRIMARY") == 10
    assert [row["scaffold_role"] for row in rows].count("RESERVE") == 2
    assert all(row["binding_label_seq_ids"] == "1,2" for row in rows)
    assert all(row["spec_path"] == f"specs/{row['spec_id']}/design.yaml" for row in rows)
    for row in rows:
        directory = output_root / row["spec_id"]
        assert {item.name for item in directory.iterdir()} == {
            "design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"
        }
        assert sha256(directory / "design.yaml") == row["spec_sha256"]
        assert sha256(directory / "scaffold.cif") == row["scaffold_sha256"]
        assert sha256(directory / "target.cif") == row["target_sha256"] == sha256(target)
        payload = yaml.safe_load((directory / "design.yaml").read_text(encoding="utf-8"))
        target_entity = payload["entities"][0]["file"]
        assert target_entity["path"] == "target.cif"
        assert target_entity["include"] == [
            {"chain": {"id": "E", "res_index": "1..30"}}
        ]
        assert target_entity["binding_types"][0]["chain"]["binding"] == "1..2"
        scaffold_payload = yaml.safe_load(
            (directory / "scaffold.yaml").read_text(encoding="utf-8")
        )
        assert scaffold_payload["structure_groups"] == [
            {"group": {"id": "A", "visibility": 2}},
            {
                "group": {
                    "id": "A",
                    "visibility": 0,
                    "res_index": "26..33,51..57,96..106",
                }
            },
        ]
        assert scaffold_payload["reset_res_index"] == [{"chain": {"id": "A"}}]
        assert payload["entities"][1]["file"]["path"] == "scaffold.yaml"

    second = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", tmp_path / "scaffolds",
        "--selected-scaffolds", tmp_path / "selected_scaffolds.tsv",
        "--export-artifacts", tmp_path / "export_artifacts.tsv",
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", output_root,
        "--manifest", manifest,
    )
    assert second.returncode == 0, second.stderr

    frozen_manifest = manifest.read_bytes()
    manifest.unlink()
    recovered = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", tmp_path / "scaffolds",
        "--selected-scaffolds", tmp_path / "selected_scaffolds.tsv",
        "--export-artifacts", tmp_path / "export_artifacts.tsv",
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", output_root,
        "--manifest", manifest,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert manifest.read_bytes() == frozen_manifest


def test_rejects_registry_range_drift_and_unsafe_binding_ids(tmp_path: Path) -> None:
    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    yaml_path = scaffold_root / "01_pdb_fixture_01-A" / "scaffold.yaml"
    yaml_path.write_text(yaml_path.read_text(encoding="utf-8").replace("96..106", "96..107"), encoding="utf-8")
    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "7,8",
        "--output-root", tmp_path / "bad_specs",
        "--manifest", tmp_path / "bad.tsv",
    )
    assert result.returncode != 0
    assert not (tmp_path / "bad.tsv").exists()


def test_rejects_export_path_that_only_matches_by_basename(tmp_path: Path) -> None:
    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    with exports.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    rows[0]["normalized_cif_path"] = rows[0]["normalized_cif_path"].replace(
        "selected/", "untrusted/", 1
    )
    rows[0]["scaffold_yaml_path"] = rows[0]["scaffold_yaml_path"].replace(
        "selected/", "untrusted/", 1
    )
    write_tsv(exports, list(rows[0]), rows)
    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", tmp_path / "specs",
        "--manifest", tmp_path / "spec_manifest.tsv",
    )
    assert result.returncode != 0
    assert not (tmp_path / "spec_manifest.tsv").exists()


def test_requires_export_target_hash_binding(tmp_path: Path) -> None:
    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    with exports.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    fields = [field for field in rows[0] if field != "target_cif_sha256"]
    write_tsv(exports, fields, [{field: row[field] for field in fields} for row in rows])

    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", tmp_path / "specs",
        "--manifest", tmp_path / "spec_manifest.tsv",
    )
    assert result.returncode != 0
    assert not (tmp_path / "specs").exists()
    assert not (tmp_path / "spec_manifest.tsv").exists()


def test_conflicting_manifest_preflight_leaves_specs_unpublished(tmp_path: Path) -> None:
    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    manifest = tmp_path / "spec_manifest.tsv"
    manifest.write_bytes(b"foreign immutable manifest\n")

    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", tmp_path / "specs",
        "--manifest", manifest,
    )
    assert result.returncode != 0
    assert manifest.read_bytes() == b"foreign immutable manifest\n"
    assert not (tmp_path / "specs").exists()


def test_rejects_nonfrozen_target_identity_before_publication(tmp_path: Path) -> None:
    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_9-36",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", tmp_path / "specs",
        "--manifest", tmp_path / "spec_manifest.tsv",
    )
    assert result.returncode != 0
    assert not (tmp_path / "specs").exists()
    assert not (tmp_path / "spec_manifest.tsv").exists()


def test_rejects_scaffold_visibility_or_reset_contract_drift(tmp_path: Path) -> None:
    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    yaml_path = scaffold_root / "01_pdb_fixture_01-A" / "scaffold.yaml"
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    payload["structure_groups"] = payload["structure_groups"][:1]
    payload.pop("reset_res_index")
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    export_rows = list(csv.DictReader(exports.open(newline="", encoding="utf-8"), delimiter="\t"))
    export_rows[0]["scaffold_yaml_sha256"] = sha256(yaml_path)
    write_tsv(exports, list(export_rows[0]), export_rows)

    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", tmp_path / "specs",
        "--manifest", tmp_path / "spec_manifest.tsv",
    )
    assert result.returncode != 0
    assert not (tmp_path / "specs").exists()


def test_rejects_target_cif_chain_or_residue_drift(tmp_path: Path) -> None:
    from test_verify_specs import TARGET_SEQUENCE, mmcif_bytes

    target, scaffold_root, selected, exports = build_fixture(tmp_path)
    target.write_bytes(mmcif_bytes([("E", TARGET_SEQUENCE[:-1])]))
    export_rows = list(csv.DictReader(exports.open(newline="", encoding="utf-8"), delimiter="\t"))
    for item in export_rows:
        item["target_cif_sha256"] = sha256(target)
    write_tsv(exports, list(export_rows[0]), export_rows)

    result = run_python(
        "build_design_specs.py",
        "--target", target,
        "--scaffold-root", scaffold_root,
        "--selected-scaffolds", selected,
        "--export-artifacts", exports,
        "--target-id", "GLP1_7-36_NH2",
        "--target-chain", "E",
        "--binding-label-seq-ids", "1,2",
        "--output-root", tmp_path / "specs",
        "--manifest", tmp_path / "spec_manifest.tsv",
    )
    assert result.returncode != 0
    assert not (tmp_path / "spec_manifest.tsv").exists()
