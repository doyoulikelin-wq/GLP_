#!/usr/bin/env python3
"""构建双路线资源索引和 BindCraft 靶标面板清单。

这是 2026-08-26 仓库整理使用的一次性脚本，不应被正式设计流水线导入。
它只读取原工作区与当前仓库，输出可审计的 CSV；不会移动、删除或下载数据。

输入
----
``--source-root`` 指向原项目工作区（默认是本 Git 仓库的父目录）。

输出
----
* ``shared/resources/manifests/all_resources_20260826.csv``
* ``boltzgen/resources/manifests/boltzgen_resources_20260826.csv``
* ``bindcraft/resources/manifests/bindcraft_resources_20260826.csv``
* ``bindcraft/resources/manifests/bindcraft_glp1_target_panel_20260825.csv``

目录只统计字节数和文件数，不重新哈希数 GiB 的全部内容；需要逐文件校验时，
索引会指向原数据包已有的 SHA-256 manifest。单个文件则直接计算 SHA-256。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ResourceSpec:
    """描述一个本地资源以及它在仓库中的公开策略。"""

    resource_id: str
    route: str
    purpose: str
    asset_class: str
    local_path: str
    source_name: str
    source_uri: str
    source_revision: str
    created_at: str
    data_format: str
    record_count: str
    license_name: str
    git_policy: str
    repository_path: str
    sha256_manifest: str
    validation_status: str
    limitations: str


RESOURCE_SPECS = (
    ResourceSpec(
        "bg_runtime_v032",
        "boltzgen",
        "BoltzGen v0.3.2 推理权重与化学组分字典",
        "model_runtime",
        "data/boltzgen_data/mvp_assets_v0.3.2/runtime_cache",
        "BoltzGen release assets",
        "https://huggingface.co/boltzgen/boltzgen-1/tree/c1be29e1f82ffcc72264f64b993c43fb4e0d17f0",
        "c1be29e1f82ffcc72264f64b993c43fb4e0d17f0",
        "2026-08-19T02:09:33+08:00",
        "Lightning checkpoint + ZIP",
        "5 runtime assets",
        "MIT per pinned model/dataset metadata; keep upstream notices",
        "EXTERNAL_ONLY",
        "",
        "data/boltzgen_data/mvp_assets_v0.3.2/metadata/raw_sha256.json",
        "VERIFIED_5_OF_5",
        "约 6.35 GB；属于推理输入，不是项目训练数据；禁止提交 Git。",
    ),
    ResourceSpec(
        "bg_mvp_curated_inputs",
        "boltzgen",
        "GLP-1 与早期 VHH 冒烟测试清理输入",
        "curated_input",
        "data/boltzgen_data/mvp_assets_v0.3.2/curated_project_inputs",
        "RCSB PDB, UniProt, PubChem and BoltzGen examples",
        "https://github.com/HannesStark/boltzgen/tree/v0.3.2/example",
        "BoltzGen 31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0",
        "2026-08-19",
        "mmCIF + YAML + JSON + TSV",
        "20 curated files",
        "mixed; see per-source manifest",
        "MANIFEST_AND_SMALL_TABLES_ONLY",
        "boltzgen/resources/data/GLP1_VHH推理输入_20260818",
        "data/boltzgen_data/mvp_assets_v0.3.2/curation_manifest.json",
        "PASS_WITH_DECLARED_LIMITATIONS",
        "7EOW/7XL0 是协议示例；GLP-1 C 端酰胺尚未完成原子级闭环验证。",
    ),
    ResourceSpec(
        "bg_sabdab2_vhh_v1",
        "boltzgen",
        "从 SAbDab2 SD-H 快照筛选 VHH 基线骨架",
        "scaffold_database",
        "data/boltzgen_data/sabdab2_vhh_scaffolds_v1",
        "SAbDab2-nano",
        "https://sabdab.opig.stats.ox.ac.uk/api/download/all-sd-h-summary",
        "API 2.0.10; snapshot updated 2026-08-06",
        "2026-08-19",
        "TGZ + mmCIF + SQLite + TSV + YAML + JSON",
        "4,508 SD-H instances; 10 PRIMARY + 2 RESERVE",
        "CC BY 4.0 for SAbDab2 snapshot; derived-code terms separate",
        "REPORT_MANIFEST_AND_SELECTED_TABLES_ONLY",
        "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819",
        "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/raw_snapshot/raw_manifest.json",
        "12_OF_12_BOLTZGEN_CHECK_PASS",
        "通过只表示输入合同可解析，不表示结合、亲和力、选择性或可开发性。",
    ),
    ResourceSpec(
        "bg_mps_smoke",
        "boltzgen",
        "Apple Silicon 上的 nanobody-anything 冒烟测试",
        "run_evidence",
        "data/boltzgen_data/mvp_run_001",
        "Project-generated run",
        "https://github.com/HannesStark/boltzgen/tree/v0.3.2",
        "experimental MPS commit 592317f0f5582730b28c144267a15631c07fcb94",
        "2026-08-19",
        "YAML + CIF + NPZ + CSV + JSON + logs",
        "2 candidates; 0 passed",
        "project-authored code; upstream components retain their licenses",
        "CODE_SUMMARY_AND_REPORT_ONLY",
        "boltzgen/main/mvp_mac_20260818",
        "data/boltzgen_data/mvp_run_001/provenance/SHA256SUMS",
        "PIPELINE_COMPLETE_0_OF_2_PASS",
        "实验性 MPS 分支；低预算工程验证，不能替代 Linux + NVIDIA 基线。",
    ),
    ResourceSpec(
        "bg_old12_round1",
        "boltzgen",
        "旧 12 个 VHH 骨架对单一 GLP-1 几何的第一轮推理",
        "run_evidence",
        "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819",
        "Project-generated run",
        "https://github.com/HannesStark/boltzgen/tree/v0.3.2",
        "BoltzGen v0.3.2 + experimental MPS branch",
        "2026-08-19",
        "YAML + CIF + NPZ + CSV + JSON + logs",
        "24 candidates; 0 passed",
        "project-authored code; upstream components retain their licenses",
        "CODE_SUMMARY_AND_REPORT_ONLY",
        "boltzgen/main/round1_old12_mac_20260819",
        "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/provenance/SHA256SUMS",
        "12_OF_12_TASKS_COMPLETE_0_OF_24_PASS",
        "只有 GLP-1(7–36) 单状态正靶；不能评价型态选择性。",
    ),
    ResourceSpec(
        "bg_old12_mac_enhanced",
        "boltzgen",
        "旧 12 骨架在 Mac 上的 diverse/adherence 分支增强筛选",
        "run_evidence",
        "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820",
        "Project-generated run",
        "https://github.com/HannesStark/boltzgen/tree/v0.3.2",
        "BoltzGen v0.3.2 + experimental MPS branch",
        "2026-08-20",
        "YAML + CIF + NPZ + CSV + JSON + JSONL + logs",
        "48 main candidates + 4 deep-probe candidates; 0 passed",
        "project-authored code; upstream components retain their licenses",
        "CODE_SUMMARY_AND_REPORT_ONLY",
        "boltzgen/main/enhanced_old12_mac_20260820",
        "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/provenance/SHA256SUMS",
        "24_OF_24_MAIN_TASKS_COMPLETE_0_OF_48_PASS",
        "双 checkpoint 同进程压力尝试被安全停止；计算代理不能解释为 K_D。",
    ),
    ResourceSpec(
        "bg_ai_validation_registry",
        "boltzgen",
        "对 112 个结构资产做来源、重复和使用边界登记",
        "validation_registry",
        "data/boltzgen_data/ai_validation_assets_v1",
        "Project-generated registry",
        "",
        "v1",
        "2026-08-26T15:52:06+08:00",
        "TSV + JSON + Markdown",
        "112 structures",
        "mixed; inherit each source asset license",
        "INCLUDE_SMALL_TABLES",
        "boltzgen/resources/data/AI结构资产验证登记册_20260826",
        "",
        "VALIDATED",
        "登记结果不能把结构来源标签改写成结合标签。",
    ),
    ResourceSpec(
        "bg_positive_conformer_ensemble",
        "boltzgen",
        "GLP-1 1D0R 的 20 个正靶构象规范副本",
        "conformer_panel",
        "data/样本数据/binding-多构象/all_conformers",
        "RCSB PDB 1D0R",
        "https://www.rcsb.org/structure/1D0R",
        "PDB 1D0R",
        "2026-08-19",
        "mmCIF",
        "20 models from one NMR deposition",
        "CC0-1.0 archive policy; cite structure authors",
        "EXTERNAL_ONLY_INDEXED",
        "",
        "",
        "CANONICAL_SOURCE_SELECTED",
        "单一 deposition 的模型不是 20 个独立实验样本；当前首轮暂不使用多构象。",
    ),
    ResourceSpec(
        "bg_challenger_scaffolds",
        "boltzgen",
        "待准入的新 17 个 VHH 骨架挑战库",
        "scaffold_challenger",
        "data/样本数据/boltzgen_vhh_scaffolds",
        "Project-curated challenger set",
        "",
        "unfrozen",
        "2026-08-19T17:34:52+08:00",
        "mmCIF + YAML + TSV",
        "17 scaffolds",
        "mixed; per-structure provenance required",
        "EXTERNAL_ONLY_INDEXED",
        "",
        "",
        "ADMISSION_PENDING",
        "必须 canonicalize、解决 INSTANCE 冲突并逐项运行 target-containing check 后才能入场。",
    ),
    ResourceSpec(
        "bc_glp1_target_panel",
        "bindcraft",
        "活性 GLP-1、截短体和同源肽的选择性计算面板",
        "target_panel",
        "bindcraft/glp1_target_panel",
        "RCSB PDB-derived peptide chains",
        "https://www.rcsb.org/",
        "1D0R, 6X18, 7DTY, 6LMK, 7LLY",
        "2026-08-25T09:33:28+08:00",
        "PDB",
        "8 peptide structures",
        "CC0-1.0 archive policy; cite structure authors",
        "INCLUDE_CURATED_SMALL_INPUTS",
        "bindcraft/resources/data/GLP1选择性靶标面板_20260825",
        "bindcraft/resources/manifests/bindcraft_glp1_target_panel_20260825.csv",
        "AUDITED_NEEDS_REVISION",
        "GIP 与 oxyntomodulin 坐标不完整；9–36 是坐标删除派生对照；不是独立实验结构。",
    ),
    ResourceSpec(
        "bc_prototype_notebook",
        "bindcraft",
        "GLP-1 选择性 de novo miniprotein BindCraft 原型",
        "prototype_code",
        "bindcraft/BindCraft-0823.ipynb",
        "Project-authored notebook based on BindCraft",
        "https://github.com/martinpacesa/BindCraft",
        "local prototype; upstream was not pinned",
        "2026-08-23",
        "Jupyter Notebook",
        "12 code cells; no execution outputs",
        "project-authored changes; upstream dependencies have separate terms",
        "INCLUDE_WITH_NOT_READY_WARNING",
        "bindcraft/main/active_glp1_selectivity_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb",
        "",
        "NOT_EXECUTED_NEEDS_REVISION",
        "正负靶统计口径、冗余阈值、N 端接触排序和依赖固定均需修订。",
    ),
)


THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def sha256_file(path: Path) -> str:
    """以流式读取方式计算文件哈希，避免把大文件整体读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure_path(path: Path) -> tuple[int, int, str]:
    """返回 ``(文件数, 总字节数, 单文件 SHA-256)``。"""

    if not path.exists():
        return 0, 0, ""
    if path.is_file():
        return 1, path.stat().st_size, sha256_file(path)
    file_count = 0
    size_bytes = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            file_count += 1
            size_bytes += child.stat().st_size
    return file_count, size_bytes, ""


def write_csv(path: Path, rows: Iterable[dict[str, object]], fields: list[str]) -> None:
    """用 UTF-8 BOM 写 CSV，兼顾命令行和常见表格软件的中文显示。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_resource_rows(source_root: Path) -> list[dict[str, object]]:
    """把静态资源合同与当前磁盘统计合并。"""

    rows: list[dict[str, object]] = []
    for spec in RESOURCE_SPECS:
        path = source_root / spec.local_path
        file_count, size_bytes, sha256 = measure_path(path)
        rows.append(
            {
                "resource_id": spec.resource_id,
                "route": spec.route,
                "purpose": spec.purpose,
                "asset_class": spec.asset_class,
                "local_workspace_path": spec.local_path,
                "source_name": spec.source_name,
                "source_uri": spec.source_uri,
                "source_revision": spec.source_revision,
                "created_at": spec.created_at,
                "format": spec.data_format,
                "record_count": spec.record_count,
                "file_count": file_count,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "sha256_manifest": spec.sha256_manifest,
                "license": spec.license_name,
                "git_policy": spec.git_policy,
                "repository_path": spec.repository_path,
                "validation_status": spec.validation_status,
                "limitations": spec.limitations,
            }
        )
    return rows


def pdb_summary(path: Path) -> tuple[str, int, int, str]:
    """从单模型肽 PDB 的 ATOM 行提取链、残基数、重原子数和序列。"""

    chains: list[str] = []
    residues: list[tuple[str, str, str]] = []
    residue_seen: set[tuple[str, str, str]] = set()
    heavy_atoms = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("ATOM") or len(line) < 54:
            continue
        chain = line[21].strip() or "_"
        if chain not in chains:
            chains.append(chain)
        residue_key = (chain, line[22:26].strip(), line[26].strip())
        if residue_key not in residue_seen:
            residue_seen.add(residue_key)
            residues.append(residue_key)
        element = line[76:78].strip() if len(line) >= 78 else line[12:16].strip()[:1]
        if element.upper() != "H":
            heavy_atoms += 1

    names: dict[tuple[str, str, str], str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ATOM") and len(line) >= 27:
            key = (line[21].strip() or "_", line[22:26].strip(), line[26].strip())
            names.setdefault(key, line[17:20].strip().upper())
    sequence = "".join(THREE_TO_ONE.get(names.get(key, ""), "X") for key in residues)
    return ",".join(chains), len(residues), heavy_atoms, sequence


def build_bindcraft_panel_rows(repository_root: Path) -> list[dict[str, object]]:
    """为仓库中已纳入的 8 个 BindCraft 肽结构建立逐文件清单。"""

    panel_dir = repository_root / "bindcraft/resources/data/GLP1选择性靶标面板_20260825"
    metadata = {
        "GLP1_7_36_6X18.pdb": ("positive", "6X18 chain P", 30, "受体结合态；C 端酰胺未原子级确认"),
        "GLP1_7_36_1D0R_model1.pdb": ("positive", "1D0R chain A model 1", 30, "游离肽 NMR 模型；同一 deposition"),
        "GLP1_7_36_1D0R_model10.pdb": ("positive", "1D0R chain A model 10", 30, "游离肽 NMR 模型；同一 deposition"),
        "GLP1_9_36_6X18.pdb": ("negative_derived", "6X18 chain P minus first 2 residues", 28, "坐标删除派生对照，不是独立实验结构"),
        "GLP1_9_36_1D0R_model1.pdb": ("negative_derived", "1D0R model 1 minus first 2 residues", 28, "坐标删除派生对照，不是独立实验结构"),
        "GIP_7DTY.pdb": ("negative_homolog", "7DTY chain P", 42, "当前坐标 30/42 残基；不完整"),
        "Glucagon_6LMK.pdb": ("negative_homolog", "6LMK chain E", 29, "同源肽反靶"),
        "Oxyntomodulin_7LLY.pdb": ("negative_homolog", "7LLY chain P", 37, "结构不完整：当前坐标 26/37 残基；与 glucagon 段高度重合"),
    }
    rows: list[dict[str, object]] = []
    for path in sorted(panel_dir.glob("*.pdb")):
        chains, residue_count, heavy_atoms, sequence = pdb_summary(path)
        role, source, expected_residue_count, limitation = metadata[path.name]
        rows.append(
            {
                "file_name": path.name,
                "role": role,
                "source_derivation": source,
                "chain": chains,
                "residue_count": residue_count,
                "expected_residue_count": expected_residue_count,
                "coordinate_coverage": round(residue_count / expected_residue_count, 4),
                "heavy_atom_count": heavy_atoms,
                "sequence": sequence,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "validation_status": "NEEDS_REVISION" if "不完整" in limitation or "派生" in limitation else "STRUCTURE_PARSED",
                "limitations": limitation,
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=REPOSITORY_ROOT.parent,
        help="原项目工作区根目录；默认是当前 Git 仓库的父目录。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source_root.resolve()
    rows = build_resource_rows(source_root)
    fields = list(rows[0])
    write_csv(REPOSITORY_ROOT / "shared/resources/manifests/all_resources_20260826.csv", rows, fields)
    write_csv(
        REPOSITORY_ROOT / "boltzgen/resources/manifests/boltzgen_resources_20260826.csv",
        (row for row in rows if row["route"] == "boltzgen"),
        fields,
    )
    write_csv(
        REPOSITORY_ROOT / "bindcraft/resources/manifests/bindcraft_resources_20260826.csv",
        (row for row in rows if row["route"] == "bindcraft"),
        fields,
    )

    panel_rows = build_bindcraft_panel_rows(REPOSITORY_ROOT)
    write_csv(
        REPOSITORY_ROOT / "bindcraft/resources/manifests/bindcraft_glp1_target_panel_20260825.csv",
        panel_rows,
        list(panel_rows[0]),
    )
    print(f"Wrote {len(rows)} resource rows and {len(panel_rows)} BindCraft panel rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
