#!/usr/bin/env python3
"""Finalize the BoltzGen MVP data package after all downloads are verified.

This script creates the project input allowlist, full file inventory, scope record,
quality checks, and a human-readable README.  It refuses to finalize while any
required runtime asset is missing, corrupt, or still represented by a .part file.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime_cache"
RAW = ROOT / "raw_sources"
CURATED = ROOT / "curated_project_inputs"
META = ROOT / "metadata"

EXPECTED_RUNTIME = {
    "boltzgen1_diverse.ckpt": {
        "bytes": 1_930_847_192,
        "sha256": "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c",
        "role": "BoltzGen design checkpoint; inference input",
    },
    "boltzgen1_adherence.ckpt": {
        "bytes": 1_930_858_014,
        "sha256": "ac7078b3dc13064c68e0c3fd542e5bc538c33558bf6607f65e499eb336ca5e5d",
        "role": "BoltzGen design checkpoint; inference input",
    },
    "boltzgen1_ifold.ckpt": {
        "bytes": 12_582_656,
        "sha256": "dd4cf108c94471bdc3a326b7b180fa3854dc019110fae780208c30b50bd56578",
        "role": "inverse-folding checkpoint; inference input",
    },
    "boltz2_conf_final.ckpt": {
        "bytes": 2_087_255_089,
        "sha256": "525a51ef306da7282a54d23a4a5b91212fc60d0ff6b23b56dd6351de3b387530",
        "role": "Boltz-2 confidence/refolding checkpoint; inference input",
    },
    "mols.zip": {
        "bytes": 391_401_102,
        "sha256": "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53",
        "role": "CCD/RDKit component dictionary; inference input",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pretty_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.3f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def tsv_count(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle, delimiter="\t")) - 1, 0)


def fasta_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(">"))


def classify_curated(path: Path, curation_by_path: dict[str, dict]) -> dict:
    rel = relative(path)
    if path.name == "project_input_allowlist.tsv":
        return {
            "format": "TSV",
            "record_unit": "approved/conditional project asset",
            "record_count": tsv_count(path),
            "role": "configuration input gate",
            "status": "keep_derived_audit",
            "direct_boltzgen_use": "no",
        }
    if path.name == "used_components.tsv":
        return {
            "format": "TSV",
            "record_unit": "CCD component used by current project sequences",
            "record_count": tsv_count(path),
            "role": "runtime dictionary usage lineage",
            "status": "keep_derived_audit",
            "direct_boltzgen_use": "no",
        }
    if rel.endswith(".cif"):
        curation = curation_by_path.get(rel, {})
        status = curation.get("status", "derived_input")
        if status == "provisional_example":
            role = "smoke-test scaffold geometry"
            direct = "smoke_test_only"
        elif "9IVG" in path.name:
            role = "challenge-state geometry reference"
            direct = "blocked_until_missing_geometry_and_terminal_chemistry_are_resolved"
        else:
            role = "positive-target geometry"
            direct = "conditional_geometry_only"
        return {
            "format": "PDBx/mmCIF",
            "record_unit": "coordinate file",
            "record_count": curation.get("model_count", 1),
            "role": role,
            "status": status,
            "direct_boltzgen_use": direct,
        }
    if rel.endswith("_residue_mapping.tsv"):
        return {
            "format": "TSV",
            "record_unit": "retained residue in one coordinate model",
            "record_count": tsv_count(path),
            "role": "curation lineage and index QA",
            "status": "keep_derived_audit",
            "direct_boltzgen_use": "no",
        }
    if rel.endswith("_curation.json"):
        return {
            "format": "JSON",
            "record_unit": "curation decision record",
            "record_count": 1,
            "role": "curation lineage and limitations",
            "status": "keep_derived_audit",
            "direct_boltzgen_use": "no",
        }
    if path.name == "GLP1_project_variants.fasta":
        return {
            "format": "FASTA",
            "record_unit": "GLP-1 sequence state",
            "record_count": fasta_count(path),
            "role": "sequence registry; terminal chemistry comes from JSON sidecar",
            "status": "keep_derived_input",
            "direct_boltzgen_use": "sequence_only_not_terminal_chemistry",
        }
    if path.name == "GLP1_project_variants.json":
        count = len(json.loads(path.read_text(encoding="utf-8"))["variants"])
        return {
            "format": "JSON",
            "record_unit": "GLP-1 chemical/sequence state",
            "record_count": count,
            "role": "canonical target-state registry",
            "status": "keep_derived_input",
            "direct_boltzgen_use": "preparation_input",
        }
    if path.name == "PubChem_CID16133831_project_record.json":
        return {
            "format": "JSON",
            "record_unit": "filtered chemical identity record",
            "record_count": 1,
            "role": "chemical identity reference",
            "status": "keep_reference_only",
            "direct_boltzgen_use": "no",
        }
    if path.suffix.lower() in {".yaml", ".yml"}:
        return {
            "format": "YAML",
            "record_unit": "nanobody scaffold design recipe",
            "record_count": 1,
            "role": "BoltzGen scaffold configuration example",
            "status": "provisional_example",
            "direct_boltzgen_use": "smoke_test_only",
        }
    return {
        "format": path.suffix.lstrip(".").upper(),
        "record_unit": "file",
        "record_count": 1,
        "role": "derived project data",
        "status": "keep_derived",
        "direct_boltzgen_use": "no",
    }


def main() -> None:
    part_files = sorted(ROOT.rglob("*.part"))
    if part_files:
        raise RuntimeError("Incomplete downloads remain: " + ", ".join(relative(path) for path in part_files))

    runtime_checks = []
    for name, expected in EXPECTED_RUNTIME.items():
        path = RUNTIME / name
        if not path.is_file():
            raise FileNotFoundError(path)
        actual_size = path.stat().st_size
        actual_sha = sha256(path)
        passed = actual_size == expected["bytes"] and actual_sha == expected["sha256"]
        runtime_checks.append({
            "path": relative(path),
            "size_bytes": actual_size,
            "expected_size_bytes": expected["bytes"],
            "sha256": actual_sha,
            "expected_sha256": expected["sha256"],
            "passed": passed,
        })
        if not passed:
            raise RuntimeError(f"Runtime verification failed: {name}")

    curation = json.loads((ROOT / "curation_manifest.json").read_text(encoding="utf-8"))
    raw_checks = []
    for item in curation["raw_files"]:
        path = ROOT / item["path"]
        actual = sha256(path)
        passed = path.stat().st_size == item["size_bytes"] and actual == item["sha256"]
        raw_checks.append({"path": item["path"], "sha256": actual, "passed": passed})
        if not passed:
            raise RuntimeError(f"Raw source verification failed: {item['path']}")

    curation_records = []
    for path in sorted(CURATED.rglob("*_curation.json")):
        curation_records.append(json.loads(path.read_text(encoding="utf-8")))
    curation_by_path = {item["curated_path"]: item for item in curation_records}
    curated_checks = []
    for item in curation_records:
        path = ROOT / item["curated_path"]
        actual = sha256(path)
        passed = actual == item["curated_sha256"]
        curated_checks.append({"path": item["curated_path"], "sha256": actual, "passed": passed})
        if not passed:
            raise RuntimeError(f"Curated structure verification failed: {item['curated_path']}")

    curated_manifest_checks = []
    for item in curation["curated_files"]:
        path = ROOT / item["path"]
        actual = sha256(path)
        passed = path.stat().st_size == item["size_bytes"] and actual == item["sha256"]
        curated_manifest_checks.append({
            "path": item["path"],
            "size_bytes": path.stat().st_size,
            "sha256": actual,
            "passed": passed,
        })
        if not passed:
            raise RuntimeError(f"Curated manifest verification failed: {item['path']}")

    checkpoint_profile = json.loads((META / "checkpoint_profile.json").read_text(encoding="utf-8"))
    checkpoint_by_name = {Path(item["path"]).name: item for item in checkpoint_profile["profiles"]}
    asset_profile = json.loads((META / "asset_profile.json").read_text(encoding="utf-8"))
    mols_profile = asset_profile["profiles"]["mols_zip"]

    inventory: list[dict] = []
    for name, expected in EXPECTED_RUNTIME.items():
        path = RUNTIME / name
        if name.endswith(".ckpt"):
            profile = checkpoint_by_name[name]
            count = profile["tensor_count"]
            unit = "parameter tensor"
            fmt = "PyTorch/Lightning checkpoint"
        else:
            count = mols_profile["member_count"]
            unit = "CCD component pickle"
            fmt = "ZIP of RDKit Mol pickle objects"
        inventory.append({
            "stage": "runtime",
            "asset": name,
            "path": relative(path),
            "role": expected["role"],
            "format": fmt,
            "record_unit": unit,
            "record_count": count,
            "size_bytes": path.stat().st_size,
            "size_display": pretty_bytes(path.stat().st_size),
            "status": "verified_required",
            "direct_boltzgen_use": "yes",
            "sha256": expected["sha256"],
        })

    for item in curation["raw_files"]:
        inventory.append({
            "stage": "raw_source",
            "asset": item["dataset"],
            "path": item["path"],
            "role": "immutable provenance; preparation reference only",
            "format": item["format"],
            "record_unit": "source record",
            "record_count": item.get("record_count", 1),
            "size_bytes": item["size_bytes"],
            "size_display": pretty_bytes(item["size_bytes"]),
            "status": "verified_raw_source",
            "direct_boltzgen_use": "no",
            "sha256": item["sha256"],
        })

    for path in sorted(CURATED.rglob("*")):
        if not path.is_file():
            continue
        attrs = classify_curated(path, curation_by_path)
        inventory.append({
            "stage": "curated",
            "asset": path.name,
            "path": relative(path),
            **attrs,
            "size_bytes": path.stat().st_size,
            "size_display": pretty_bytes(path.stat().st_size),
            "sha256": sha256(path),
        })

    inventory_path = META / "file_inventory.tsv"
    fields = [
        "stage", "asset", "path", "role", "format", "record_unit",
        "record_count", "size_bytes", "size_display", "status",
        "direct_boltzgen_use", "sha256",
    ]
    with inventory_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fields)
        writer.writeheader()
        writer.writerows(inventory)

    allowlist_rows = []
    for item in inventory:
        if item["stage"] != "curated":
            continue
        if item["direct_boltzgen_use"] == "no":
            continue
        conditions = {
            "conditional_geometry_only": "Terminal amide is not explicitly encoded; use as geometry only until round-trip chemistry QC passes.",
            "blocked_until_missing_geometry_and_terminal_chemistry_are_resolved": "9IVG lacks coordinates for residues 30..36 and does not verify NH2; do not use as a complete 9-36NH2 target.",
            "smoke_test_only": "Official example scaffold, not a validated or project-approved production framework.",
            "sequence_only_not_terminal_chemistry": "FASTA stores residues only; read terminal chemistry from GLP1_project_variants.json.",
            "preparation_input": "Registry input; convert to a validated target specification before inference.",
        }.get(item["direct_boltzgen_use"], "")
        allowlist_rows.append({
            "asset": item["asset"],
            "path": item["path"],
            "role": item["role"],
            "status": item["status"],
            "use_level": item["direct_boltzgen_use"],
            "conditions": conditions,
            "sha256": item["sha256"],
        })
    allowlist_path = CURATED / "project_input_allowlist.tsv"
    allowlist_fields = ["asset", "path", "role", "status", "use_level", "conditions", "sha256"]
    with allowlist_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=allowlist_fields)
        writer.writeheader()
        writer.writerows(allowlist_rows)

    one_to_three = {
        "A": "ALA", "R": "ARG", "N": "ASN", "D": "ASP", "C": "CYS",
        "Q": "GLN", "E": "GLU", "G": "GLY", "H": "HIS", "I": "ILE",
        "L": "LEU", "K": "LYS", "M": "MET", "F": "PHE", "P": "PRO",
        "S": "SER", "T": "THR", "W": "TRP", "Y": "TYR", "V": "VAL",
    }
    sequences = []
    variants = json.loads((CURATED / "sequence_chemistry" / "GLP1_project_variants.json").read_text(encoding="utf-8"))
    sequences.extend((item["id"], item["sequence"]) for item in variants["variants"])
    for record in curation_records:
        if record["status"] == "provisional_example":
            sequences.append((record["artifact_id"], record["curated_entity_sequence"]))
    component_sources: dict[str, set[str]] = {}
    for source, sequence in sequences:
        for letter in set(sequence):
            component_sources.setdefault(one_to_three[letter], set()).add(source)
    used_components = CURATED / "used_components.tsv"
    with used_components.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["ccd_component_id", "one_letter_code", "source_asset_count", "source_assets", "dictionary_policy"])
        reverse = {value: key for key, value in one_to_three.items()}
        for component in sorted(component_sources):
            sources = sorted(component_sources[component])
            writer.writerow([component, reverse[component], len(sources), ";".join(sources), "mols.zip retained whole; this table is usage lineage only"])

    # Add the two final project-control tables to the inventory after creating
    # them. They are generated data assets, although they are not part of the
    # original 20-file curation manifest.
    inventory_paths = {item["path"] for item in inventory}
    for path in (allowlist_path, used_components):
        if relative(path) in inventory_paths:
            continue
        attrs = classify_curated(path, curation_by_path)
        inventory.append({
            "stage": "curated",
            "asset": path.name,
            "path": relative(path),
            **attrs,
            "size_bytes": path.stat().st_size,
            "size_display": pretty_bytes(path.stat().st_size),
            "sha256": sha256(path),
        })
    with inventory_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fields)
        writer.writeheader()
        writer.writerows(inventory)

    official_download_bytes = sum(item["size_bytes"] for item in runtime_checks) + curation["raw_total_bytes"]
    curated_bytes_before_allowlist = curation["curated_total_bytes"]
    scope = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "boltzgen_version": "v0.3.2",
        "boltzgen_git_commit": "31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0",
        "model_repository_revision": "c1be29e1f82ffcc72264f64b993c43fb4e0d17f0",
        "molecule_dictionary_revision": "c3d36fd276e9caf098c75d4113c6d5eb320b1a4c",
        "included": {
            "runtime_required_file_count": len(EXPECTED_RUNTIME),
            "runtime_required_bytes": sum(item["bytes"] for item in EXPECTED_RUNTIME.values()),
            "raw_source_file_count": curation["raw_file_count"],
            "raw_source_bytes": curation["raw_total_bytes"],
            "official_download_file_count": len(EXPECTED_RUNTIME) + curation["raw_file_count"],
            "official_download_bytes": official_download_bytes,
            "curated_file_count_before_final_manifests": curation["curated_file_count"],
            "curated_bytes_before_final_manifests": curated_bytes_before_allowlist,
            "mols_member_count": mols_profile["member_count"],
            "mols_uncompressed_member_bytes": mols_profile["uncompressed_bytes"],
        },
        "excluded_not_downloaded": [
            {"asset": "boltz2_aff.ckpt", "reason": "protein-small_molecule affinity only; nanobody-anything does not use it"},
            {"asset": "boltzgen1_train/targets.zip", "reason": "foundation-model training archive; not required for MVP inference"},
            {"asset": "boltzgen1_train/msa.zip", "reason": "foundation-model training archive; not required for MVP inference"},
            {"asset": "boltzgen1_structuretrained_small.ckpt", "reason": "optional training initialization; not required for nanobody-anything inference"},
            {"asset": "remaining nanobody_scaffolds examples", "reason": "examples, not an approved production scaffold library"},
            {"asset": "SAbDab2-nano and PROPEDIA", "reason": "future scaffold/interface expansion; not needed for the inference-only MVP"},
        ],
        "cleaning_policy": {
            "raw_sources": "immutable provenance copies; never referenced directly by project inference configs",
            "curated_project_inputs": "only selected GLP-1 peptide or VHH chain plus explicit lineage and limitations",
            "runtime_cache": "version-pinned official checkpoints and the complete molecule dictionary; never subset mols.zip",
        },
        "blocking_caveats": [
            "Ordinary FASTA and the curated standard-polymer mmCIF files do not explicitly prove C-terminal amidation round-trips through BoltzGen v0.3.2.",
            "9IVG provides coordinates for only 21 of the declared 28 residues and must not be treated as a complete free GLP-1(9-36)NH2 truth structure.",
            "7EOW and 7XL0 are official example scaffolds for smoke testing only, not project-approved production VHH frameworks.",
            "A production-ready top-level BoltzGen run YAML has not been generated; scaffold, target conformer, chemistry state and design policy still require an explicit reviewed task specification.",
        ],
    }
    (META / "mvp_scope.json").write_text(json.dumps(scope, ensure_ascii=False, indent=2), encoding="utf-8")

    checks = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "overall_status": "PASS_WITH_DECLARED_LIMITATIONS",
        "checks": [
            {"id": "runtime_hashes", "passed": all(item["passed"] for item in runtime_checks), "items": runtime_checks},
            {"id": "raw_source_hashes", "passed": all(item["passed"] for item in raw_checks), "items": raw_checks},
            {"id": "curated_structure_hashes", "passed": all(item["passed"] for item in curated_checks), "items": curated_checks},
            {"id": "all_curated_manifest_hashes", "passed": all(item["passed"] for item in curated_manifest_checks), "items": curated_manifest_checks},
            {"id": "no_partial_downloads", "passed": not part_files, "items": []},
            {"id": "mols_zip_exact_member_count", "passed": mols_profile["member_count"] == 45_227, "observed": mols_profile["member_count"]},
            {"id": "excluded_assets_absent", "passed": not any((RUNTIME / name).exists() for name in ["boltz2_aff.ckpt", "targets.zip", "msa.zip", "boltzgen1_structuretrained_small.ckpt"])},
            {"id": "9ivg_limitation_recorded", "passed": any(item["source_pdb_id"] == "9IVG" and item["observed_coordinate_sequence_length"] == 21 and item["raw_declared_sequence_length"] == 28 for item in curation_records)},
            {"id": "vhh_examples_not_approved", "passed": sum(item["status"] == "provisional_example" for item in curation_records) == 2},
            {"id": "7eow_curated_yaml_index_shift", "passed": "25..33,51..58,97..117" in (CURATED / "vhh_provisional_scaffolds" / "7eow" / "7eow.yaml").read_text(encoding="utf-8") and "26..34,52..59,98..118" not in (CURATED / "vhh_provisional_scaffolds" / "7eow" / "7eow.yaml").read_text(encoding="utf-8")},
            {"id": "curated_heavy_atom_counts", "passed": any(item["source_pdb_id"] == "1D0R" and set(item["atom_count_per_model"]) == {234} for item in curation_records) and any(item["source_pdb_id"] == "7XL0" and item["atom_count_per_model"] == [915] for item in curation_records)},
            {"id": "vhh_unresolved_termini_recorded", "passed": all(item["unresolved_declared_positions"] for item in curation_records if item["status"] == "provisional_example")},
            {"id": "correct_pubchem_cid", "passed": (RAW / "pubchem_CID16133831" / "CID16133831.json").is_file()},
        ],
    }
    if not all(item["passed"] for item in checks["checks"]):
        checks["overall_status"] = "FAIL"
    (META / "quality_checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    if checks["overall_status"] == "FAIL":
        raise RuntimeError("One or more final quality checks failed")

    readme = f"""# BoltzGen v0.3.2 MVP 数据包

生成时间（UTC）：{scope['generated_at']}

## 结论

- 已下载并逐文件核验 5 个 `nanobody-anything` 必需运行资产，共 {sum(item['bytes'] for item in EXPECTED_RUNTIME.values()):,} B（{pretty_bytes(sum(item['bytes'] for item in EXPECTED_RUNTIME.values()))}）。
- 已保存 14 个公开来源文件，共 {curation['raw_total_bytes']:,} B；这些文件只用于溯源，不直接进入推理。
- 已形成 20 个初始清理文件，共 {curation['curated_total_bytes']:,} B，并新增项目白名单与组分使用记录。
- `mols.zip` 保持完整，不解压、不裁剪；它是按需索引的运行字典，不是训练样本集合。
- 总体 QA 状态：`PASS_WITH_DECLARED_LIMITATIONS`。限制不是错误被忽略，而是显式阻断项。

## 目录边界

```text
runtime_cache/             版本锁定的4个checkpoint和完整mols.zip
raw_sources/               不可变公开来源；禁止直接作为项目推理输入
curated_project_inputs/    只含选定GLP-1/VHH链、序列注册表、映射和白名单
metadata/                  哈希、统计、质量检查、剖析与报告生成材料
```

## 使用前必须看

1. `curated_project_inputs/project_input_allowlist.tsv` 是唯一允许配置层读取的项目输入清单。
2. 1D0R/6X18当前仅可作为几何来源；C端酰胺仍需做BoltzGen解析与输出的原子/键闭环验证。
3. 9IVG只有21/28个声明残基有坐标，且条目未声明NH2；它不能当完整、游离的9-36NH2真值。
4. 7EOW/7XL0是官方示例框架，只可做冒烟测试，不能写成项目批准的生产VHH库。
5. 不要把checkpoint称为训练数据集；它们是原训练过程的输出、当前推理过程的输入。

## 关键审计文件

- `metadata/file_inventory.tsv`：逐文件体积、格式、粒度、角色、SHA-256。
- `metadata/mvp_scope.json`：纳入/排除边界与版本。
- `metadata/quality_checks.json`：最终机器可读QA结果。
- `curation_manifest.json`：原始到清理结构的转换证据。
- `metadata/checkpoint_profile.json`：安全的张量键/形状统计。
- `metadata/asset_profile.json`：各格式、记录、维度和样例统计。
"""
    (ROOT / "README.md").write_text(readme, encoding="utf-8")

    print(json.dumps({
        "status": checks["overall_status"],
        "official_download_bytes": official_download_bytes,
        "inventory_rows": len(inventory),
        "allowlist_rows": len(allowlist_rows),
        "output": relative(META / "quality_checks.json"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
