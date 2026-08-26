#!/usr/bin/env python3
"""冻结第一轮 BoltzGen 候选生成所需的全部输入与来源证据。

本脚本只做数据准备，不运行神经网络。它从已经完成质量控制的旧版 12 个
SD-H/VHH 骨架数据库中复制正式入选包，复制单一 GLP-1(7–36) 几何目标，
为每个骨架生成一份独立的 BoltzGen 顶层 YAML，并记录模型资产、代码版本、
输入文件与既有 ``boltzgen check`` 证据的 SHA-256。

术语说明：这里的“第一轮”是使用预训练 BoltzGen 权重进行候选生成（推理），
不会更新任何模型权重，因此不是重新训练基础模型。
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 脚本位于 RUN_ROOT/scripts/；所有本轮自编代码、输入、过程和结果都写入 RUN_ROOT。
RUN_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = RUN_ROOT.parent

# 旧 12 骨架的正式数据库；selected_scaffolds.tsv 是入选身份与角色的权威真源。
SOURCE_DB_ROOT = DATA_ROOT / "sabdab2_vhh_scaffolds_v1"
SOURCE_SELECTED = SOURCE_DB_ROOT / "registry" / "selected_scaffolds.tsv"

# 单一正靶采用已经清理并在上一轮验证过的 6X18 GLP-1 链 E 几何。
TARGET_SOURCE_DIR = (
    DATA_ROOT
    / "mvp_assets_v0.3.2"
    / "curated_project_inputs"
    / "glp1_complex_peptides"
)
TARGET_SOURCE = TARGET_SOURCE_DIR / "6X18_glp1_7-36NH2_labelE_authP.cif"
TARGET_CURATION_SOURCE = (
    TARGET_SOURCE_DIR / "6X18_glp1_7-36NH2_labelE_authP_curation.json"
)
TARGET_MAPPING_SOURCE = (
    TARGET_SOURCE_DIR / "6X18_glp1_7-36NH2_labelE_authP_residue_mapping.tsv"
)

# 运行时权重保留在已校验缓存中，避免在本轮文件夹内重复占用约 6.35 GB。
RUNTIME_CACHE = DATA_ROOT / "mvp_assets_v0.3.2" / "runtime_cache"
RUNTIME_FILES = {
    "design_diverse": RUNTIME_CACHE / "boltzgen1_diverse.ckpt",
    "inverse_fold": RUNTIME_CACHE / "boltzgen1_ifold.ckpt",
    "folding": RUNTIME_CACHE / "boltz2_conf_final.ckpt",
    "molecule_dictionary": RUNTIME_CACHE / "mols.zip",
}
EXPECTED_RUNTIME_SHA256 = {
    "design_diverse": "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c",
    "inverse_fold": "dd4cf108c94471bdc3a326b7b180fa3854dc019110fae780208c30b50bd56578",
    "folding": "525a51ef306da7282a54d23a4a5b91212fc60d0ff6b23b56dd6351de3b387530",
    "molecule_dictionary": "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53",
}

# 这台 Mac 实际使用的是上一轮已验证的实验性 MPS 环境与源代码。
MVP_ROOT = DATA_ROOT / "mvp_run_001"
PYTHON = MVP_ROOT / "env" / "bin" / "python"
BOLTZGEN = MVP_ROOT / "env" / "bin" / "boltzgen"
MPS_SOURCE_REPO = MVP_ROOT / "vendor" / "boltzgen_mps_pr145"

# 6X18 清理目标的已知指纹；若变化则停止，避免把不同目标混入同一批次。
EXPECTED_TARGET_SHA256 = (
    "11b82b2633793e6799f1d56c19a88fd52828bec5d26d9366801753dfa72d2d53"
)


def utc_now() -> str:
    """返回可跨机器比较的 UTC ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算文件 SHA-256，避免把大 checkpoint 一次载入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """以 UTF-8、稳定键顺序和换行写出机器可读 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def copy_file(source: Path, destination: Path) -> dict[str, Any]:
    """复制单个文件，并返回复制后可审计的大小与哈希。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "path": destination.relative_to(RUN_ROOT).as_posix(),
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def load_selected_rows() -> list[dict[str, str]]:
    """读取并严格验证旧版 12 骨架的正式入选表。"""

    with SOURCE_SELECTED.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    if len(rows) != 12:
        raise ValueError(f"旧版正式入选表应为 12 行，实际为 {len(rows)} 行")

    ranks = [int(row["selection_rank"]) for row in rows]
    if ranks != list(range(1, 13)):
        raise ValueError(f"selection_rank 不是连续的 1..12：{ranks}")

    candidate_ids = [row["candidate_id"] for row in rows]
    if len(set(candidate_ids)) != 12:
        raise ValueError("旧版正式入选表存在重复 candidate_id")

    for row in rows:
        if row["boltzgen_check_status"] != "PASS":
            raise ValueError(f"{row['candidate_id']} 的既有 BoltzGen 输入检查不是 PASS")
        if row["role"] not in {"PRIMARY", "RESERVE"}:
            raise ValueError(f"{row['candidate_id']} 的 role 非法：{row['role']}")
    return rows


def snapshot_vendor_code() -> dict[str, Any]:
    """复制本次实际执行的 MPS 源码快照，不复制大型示例和 Git 工作区元数据。"""

    destination = RUN_ROOT / "vendor" / "boltzgen_mps_pr145"
    if destination.exists():
        # 准备脚本可重复执行；已存在时不覆盖，稍后仍会重新计算哈希清单。
        pass
    else:
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copytree(
            MPS_SOURCE_REPO / "src",
            destination / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in ("README.md", "LICENSE", "pyproject.toml"):
            source = MPS_SOURCE_REPO / name
            if source.exists():
                shutil.copy2(source, destination / name)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=MPS_SOURCE_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=MPS_SOURCE_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise RuntimeError("实验性 MPS 源仓库存在未提交修改，拒绝冻结不明确代码")

    code_files = []
    for path in sorted(destination.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            code_files.append(
                {
                    "path": path.relative_to(RUN_ROOT).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "official_release_baseline": "BoltzGen v0.3.2",
        "experimental_mps_source_commit": commit,
        "source_repo_clean": True,
        "execution_notice": "未合并的实验性 MPS 代码，不代表官方 macOS 支持",
        "files": code_files,
    }


def build_design_spec(package_name: str) -> str:
    """为一个固定骨架生成独立顶层设计规格。

    目标链 E 的位置 1、2 分别对应生物编号 His7、Ala8。``binding`` 只是正向
    结合位点提示，不是对 GLP-1(9–36) 的反向选择性损失。
    """

    return f"""# 第一轮 BoltzGen nanobody-anything 顶层设计规格。
# 本文件只包含一个 GLP-1 正靶和一个固定 VHH 骨架，确保每个骨架得到相同候选预算。

entities:
  - file:
      path: ../inputs/target/6X18_GLP1_7-36_geometry.cif
      include:
        - chain:
            id: E
      binding_types:
        - chain:
            id: E
            binding: 1,2
      structure_groups: all

  - file:
      path: ../inputs/scaffolds/{package_name}/scaffold.yaml

# 科学边界：本轮没有输入 GLP-1(9–36) 或多构象集合，因此不能评价型态选择性。
"""


def main() -> int:
    """冻结输入、生成 12 份规格，并写出第一轮总清单。"""

    required = [
        SOURCE_SELECTED,
        TARGET_SOURCE,
        TARGET_CURATION_SOURCE,
        TARGET_MAPPING_SOURCE,
        PYTHON,
        BOLTZGEN,
        MPS_SOURCE_REPO,
        *RUNTIME_FILES.values(),
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少准备第一轮所需文件：\n" + "\n".join(missing))

    rows = load_selected_rows()
    prepared_files: list[dict[str, Any]] = []

    # 保存旧筛选政策、入选身份、验证证据和来源快照元数据；不复制 519 MB 原始 TGZ。
    evidence_files = [
        SOURCE_SELECTED,
        SOURCE_DB_ROOT / "registry" / "boltzgen_export_validation.tsv",
        SOURCE_DB_ROOT / "registry" / "database_summary.json",
        SOURCE_DB_ROOT / "criteria" / "scaffold_screening_v1.json",
        SOURCE_DB_ROOT / "raw_snapshot" / "raw_manifest.json",
        SOURCE_DB_ROOT / "README.md",
        SOURCE_DB_ROOT / "SHA256SUMS",
    ]
    for source in evidence_files:
        category = "source_registry"
        if source.parent.name == "criteria":
            category = "source_criteria"
        elif source.parent.name == "raw_snapshot":
            category = "source_snapshot_metadata"
        destination = RUN_ROOT / "inputs" / category / source.name
        prepared_files.append(copy_file(source, destination))

    # 冻结单一 GLP-1 几何目标及其清理说明和残基映射。
    target_destination = RUN_ROOT / "inputs" / "target" / "6X18_GLP1_7-36_geometry.cif"
    target_record = copy_file(TARGET_SOURCE, target_destination)
    prepared_files.append(target_record)
    prepared_files.append(
        copy_file(
            TARGET_CURATION_SOURCE,
            RUN_ROOT / "inputs" / "target" / "6X18_GLP1_7-36_geometry_curation.json",
        )
    )
    prepared_files.append(
        copy_file(
            TARGET_MAPPING_SOURCE,
            RUN_ROOT / "inputs" / "target" / "6X18_GLP1_7-36_geometry_residue_mapping.tsv",
        )
    )
    if target_record["sha256"] != EXPECTED_TARGET_SHA256:
        raise ValueError("冻结后的 GLP-1 目标 SHA-256 与已验证指纹不一致")

    scaffold_manifest_rows: list[dict[str, Any]] = []
    for row in rows:
        rank = int(row["selection_rank"])
        source_package = SOURCE_DB_ROOT / row["package_path"]
        package_name = source_package.name
        destination_package = RUN_ROOT / "inputs" / "scaffolds" / package_name

        # 12 个包总计仅约 7.5 MB；完整复制可保留旧 check 日志与来源坐标证据。
        if destination_package.exists():
            shutil.rmtree(destination_package)
        shutil.copytree(
            source_package,
            destination_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        required_package_files = [
            destination_package / "scaffold.cif",
            destination_package / "scaffold.yaml",
            destination_package / "curation.json",
            destination_package / "qc.json",
            destination_package / "residue_mapping.tsv",
            destination_package / "source_rcsb_original.cif",
            destination_package / "check_spec.yaml",
            destination_package / "boltzgen_check" / "output" / "check_spec.cif",
        ]
        absent = [str(path) for path in required_package_files if not path.exists()]
        if absent:
            raise FileNotFoundError(
                f"{row['candidate_id']} 的正式骨架包不完整：\n" + "\n".join(absent)
            )

        package_files = []
        for path in sorted(destination_package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                record = {
                    "path": path.relative_to(RUN_ROOT).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
                package_files.append(record)
                prepared_files.append(record)

        spec_name = f"{rank:02d}_{row['candidate_id']}.yaml"
        spec_path = RUN_ROOT / "configs" / spec_name
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(build_design_spec(package_name), encoding="utf-8")
        spec_record = {
            "path": spec_path.relative_to(RUN_ROOT).as_posix(),
            "size_bytes": spec_path.stat().st_size,
            "sha256": sha256_file(spec_path),
        }
        prepared_files.append(spec_record)

        scaffold_manifest_rows.append(
            {
                "selection_rank": rank,
                "role": row["role"],
                "candidate_id": row["candidate_id"],
                "pdb_code": row["pdb_code"],
                "sabdab_id": row["sabdab_id"],
                "source_hchain": row["source_hchain"],
                "heavy_species": row["heavy_species"],
                "method": row["method"],
                "resolution_a": float(row["resolution_a"]),
                "r_free": float(row["r_free"]),
                "variable_length_aa": int(row["variable_length_aa"]),
                "cdr1_length_aa": int(row["cdr1_length_aa"]),
                "cdr2_length_aa": int(row["cdr2_length_aa"]),
                "cdr3_length_aa": int(row["cdr3_length_aa"]),
                "framework_cluster_id": row["framework_cluster_id"],
                "quality_score": float(row["quality_score"]),
                "soft_flag_count": int(row["soft_flag_count"]),
                "benchmark_7xl0": row["benchmark_7xl0"] == "True",
                "prior_boltzgen_check_status": row["boltzgen_check_status"],
                "prior_boltzgen_check_output_sha256": row[
                    "boltzgen_check_output_sha256"
                ],
                "input_package": destination_package.relative_to(RUN_ROOT).as_posix(),
                "design_spec": spec_path.relative_to(RUN_ROOT).as_posix(),
                "design_spec_sha256": spec_record["sha256"],
                "package_files": package_files,
            }
        )

    vendor_manifest = snapshot_vendor_code()

    runtime_assets = []
    for name, path in RUNTIME_FILES.items():
        observed = sha256_file(path)
        if observed != EXPECTED_RUNTIME_SHA256[name]:
            raise ValueError(f"运行资产 {name} 的 SHA-256 不匹配")
        runtime_assets.append(
            {
                "asset": name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": observed,
                "storage_policy": "external_validated_cache_not_duplicated",
            }
        )

    manifest = {
        "schema_version": "1.0.0",
        "created_at_utc": utc_now(),
        "campaign_id": "boltzgen_round1_old12_glp1_20260819",
        "execution_semantics": "pretrained_inference_candidate_generation_not_weight_training",
        "target": {
            "name": "GLP-1(7-36) receptor-bound geometry from PDB 6X18",
            "sequence": "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR",
            "label_asym_id": "E",
            "residue_count": 30,
            "binding_hint_label_seq_id": [1, 2],
            "binding_hint_biological_identity": ["His7", "Ala8"],
            "role": "positive_target_geometry_only",
            "terminal_amide_atomically_verified": False,
            "path": target_record["path"],
            "sha256": target_record["sha256"],
        },
        "scaffold_population": {
            "count": 12,
            "primary_count": sum(row["role"] == "PRIMARY" for row in rows),
            "reserve_count": sum(row["role"] == "RESERVE" for row in rows),
            "source_registry": "inputs/source_registry/selected_scaffolds.tsv",
            "records": scaffold_manifest_rows,
        },
        "generation_budget": {
            "designs_per_scaffold": 2,
            "requested_total_designs": 24,
            "final_display_budget_per_scaffold": 1,
            "run_order": "selection_rank ascending, strictly sequential",
            "reason": "2 避免 v0.3.2 单候选 quality_score 除零；顺序运行降低 18 GB MPS 内存风险",
        },
        "compute_profile": {
            "design_sampling_steps": 50,
            "inverse_fold_sampling_steps": 30,
            "folding_sampling_steps": 50,
            "folding_samples_per_candidate": 1,
            "recycling_steps": 1,
            "precision": "32",
            "diffusion_batch_size": 1,
            "analysis_liability_modality": "antibody",
            "filtering_modality": "antibody",
            "filter_bindingsite": True,
            "class": "first_round_fast_screen",
        },
        "runtime": {
            "python_executable": str(PYTHON),
            "boltzgen_executable": str(BOLTZGEN),
            "platform": platform.platform(),
            "python_version_used_for_preparation": sys.version,
            "vendor_code": vendor_manifest,
            "assets": runtime_assets,
        },
        "known_limits": [
            "只使用一个 GLP-1 正靶几何，不评价 GLP-1(9-36) 或多构象鲁棒性",
            "C 端酰胺没有在当前标准聚合物 CIF 中完成原子级验证",
            "使用未合并的实验性 MPS 代码，不代表官方 v0.3.2 支持 macOS",
            "低采样步数和每候选单个复折叠样本适合首轮流程筛查，不适合最终定论",
            "结构置信度、界面几何和过滤状态都不是实验 Kd、亲和力或选择性",
            "BoltzGen CLI 没有为整条管线暴露统一随机种子，精确复现序列不保证",
        ],
        "prepared_files": sorted(prepared_files, key=lambda item: item["path"]),
    }
    write_json(RUN_ROOT / "provenance" / "input_manifest.json", manifest)

    # 保存环境包清单，便于以后在另一台机器重建；这一步不改变当前环境。
    freeze = subprocess.run(
        [str(PYTHON), "-m", "pip", "freeze"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (RUN_ROOT / "provenance" / "pip_freeze.txt").write_text(freeze, encoding="utf-8")

    print(
        f"准备完成：12 个骨架、12 份独立规格、请求总候选 24；目标 SHA={target_record['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
