#!/usr/bin/env python3
"""生成 Git 仓库文件到原工作区资源的逐文件映射。

输出 ``shared/resources/manifests/repository_file_map_20260826.csv``。每行记录
仓库路径、路线、类别、字节数、SHA-256、是否为公开带日期入口、已知旧路径和
迁移变换。映射输出本身不递归写入自己的清单。

该脚本只读仓库与原工作区，不移动或删除文件。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "shared/resources/manifests/repository_file_map_20260826.csv"


# 保持原文件名的目录可用前缀映射；重命名文件在下面的显式映射中列出。
PREFIX_MAP = {
    "boltzgen/main/mvp_data_assets_20260818/scripts/": "data/boltzgen_data/mvp_assets_v0.3.2/metadata/",
    "boltzgen/main/mvp_mac_20260818/scripts/": "data/boltzgen_data/mvp_run_001/scripts/",
    "boltzgen/main/sabdab2_scaffold_curation_20260819/scripts/": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/scripts/",
    "boltzgen/main/round1_old12_mac_20260819/scripts/": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/scripts/",
    "boltzgen/main/enhanced_old12_mac_20260820/scripts/": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/scripts/",
    "boltzgen/main/round1_old12_mac_20260819/configs/": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/configs/",
    "boltzgen/main/enhanced_old12_mac_20260820/configs/": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/configs/",
    "bindcraft/resources/data/GLP1选择性靶标面板_20260825/": "bindcraft/glp1_target_panel/",
}

EXPLICIT_MAP = {
    "boltzgen/main/asset_validation_20260820/validate_assets.py": "data/boltzgen_data/ai_validation_assets_v1/validate_assets.py",
    "boltzgen/main/mvp_mac_20260818/inputs/glp1_7_36_nanobody_mvp.yaml": "data/boltzgen_data/mvp_run_001/inputs/glp1_7_36_nanobody_mvp.yaml",
    "boltzgen/main/mvp_mac_20260818/inputs/scaffold/7xl0_mvp_scaffold.yaml": "data/boltzgen_data/mvp_run_001/inputs/scaffold/7xl0_mvp_scaffold.yaml",
    "boltzgen/plans/boltzgen_glp1_vhh_execution_plan_20260826.md": "data/boltzgen_data/BoltzGen_GLP1_VHH_无上下文执行实施方案.md",
    "boltzgen/reports/html/boltzgen_mvp_data_assets_20260818.html": "data/boltzgen_data/mvp_assets_v0.3.2/BoltzGen_MVP_数据资产说明与样例.html",
    "boltzgen/reports/html/boltzgen_nanobody_mps_smoke_20260819.html": "data/boltzgen_data/mvp_run_001/BoltzGen_MVP_运行结果与评价.html",
    "boltzgen/reports/html/sabdab2_vhh_scaffold_screening_20260819.html": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/reports/SAbDab2_VHH骨架筛选与数据库统计.html",
    "boltzgen/reports/html/boltzgen_vhh_glp1_algorithm_20260819.html": "data/boltzgen_data/BoltzGen_VHH_GLP-1_数据流与算法原理.html",
    "boltzgen/reports/html/boltzgen_old12_glp1_round1_20260819.html": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/BoltzGen_旧12骨架_GLP1_第一轮候选生成与复盘.html",
    "boltzgen/reports/html/boltzgen_old12_glp1_mac_enhanced_20260820.html": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/BoltzGen_Mac_旧12骨架_增强筛选与复盘.html",
    "shared/reports/html/glp1_ai_design_knowledge_graph_20260819.html": "AI创制活性GLP-1知识图谱.html",
    "shared/reports/html/glp1_ai_implementation_blueprint_20260818.html": "ai相关.html",
    "boltzgen/reports/manifests/boltzgen_mvp_data_assets_20260818.artifact.json": "data/boltzgen_data/mvp_assets_v0.3.2/metadata/report_artifact.json",
    "boltzgen/reports/manifests/boltzgen_nanobody_mps_smoke_20260819.artifact.json": "data/boltzgen_data/mvp_run_001/report/report_artifact.json",
    "boltzgen/reports/manifests/sabdab2_vhh_scaffold_screening_20260819.artifact.json": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/artifact_source/artifact.json",
    "boltzgen/reports/manifests/boltzgen_vhh_glp1_algorithm_20260819.artifact.json": "data/boltzgen_data/boltzgen_vhh_glp1_explainer/artifact.json",
    "boltzgen/reports/manifests/boltzgen_old12_glp1_round1_20260819.artifact.json": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/report/report_artifact.json",
    "boltzgen/reports/manifests/boltzgen_old12_glp1_mac_enhanced_20260820.artifact.json": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/report/report_artifact.json",
    "boltzgen/notebooks/boltzgen_nanobody_mps_smoke_review_20260819.ipynb": "data/boltzgen_data/mvp_run_001/notebooks/BoltzGen_MVP_运行复盘.ipynb",
    "boltzgen/notebooks/sabdab2_vhh_scaffold_screening_review_20260819.ipynb": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/notebooks/SAbDab2_VHH骨架筛选复盘.ipynb",
    "boltzgen/notebooks/boltzgen_old12_glp1_round1_review_20260819.ipynb": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/notebooks/BoltzGen_旧12骨架_GLP1_第一轮复盘.ipynb",
    "boltzgen/notebooks/boltzgen_old12_glp1_mac_enhanced_review_20260820.ipynb": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/notebooks/BoltzGen_Mac_旧12骨架_增强筛选复盘.ipynb",
    "bindcraft/main/active_glp1_selectivity_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb": "bindcraft/BindCraft-0823.ipynb",
    "bindcraft/notebooks/bindcraft_input_audit_20260826.ipynb": "bindcraft/review_artifacts/bindcraft_input_audit.ipynb",
    "boltzgen/resources/data/GLP1_VHH推理输入_20260818/推理输入白名单_20260818.tsv": "data/boltzgen_data/mvp_assets_v0.3.2/curated_project_inputs/project_input_allowlist.tsv",
    "boltzgen/resources/data/GLP1_VHH推理输入_20260818/推理使用组件_20260818.tsv": "data/boltzgen_data/mvp_assets_v0.3.2/curated_project_inputs/used_components.tsv",
    "boltzgen/resources/data/GLP1_VHH推理输入_20260818/GLP1化学状态注册表_20260818.json": "data/boltzgen_data/mvp_assets_v0.3.2/curated_project_inputs/sequence_chemistry/GLP1_project_variants.json",
    "boltzgen/resources/data/GLP1_VHH推理输入_20260818/GLP1_6X18几何清理说明_20260818.json": "data/boltzgen_data/mvp_assets_v0.3.2/curated_project_inputs/glp1_complex_peptides/6X18_glp1_7-36NH2_labelE_authP_curation.json",
    "boltzgen/resources/data/GLP1_VHH推理输入_20260818/GLP1_6X18残基映射_20260818.tsv": "data/boltzgen_data/mvp_assets_v0.3.2/curated_project_inputs/glp1_complex_peptides/6X18_glp1_7-36NH2_labelE_authP_residue_mapping.tsv",
    "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819/VHH骨架筛选规则_20260819.json": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/criteria/scaffold_screening_v1.json",
    "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819/旧12骨架登记表_20260819.tsv": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/registry/selected_scaffolds.tsv",
    "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819/BoltzGen输入验证_20260819.tsv": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/registry/boltzgen_export_validation.tsv",
    "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819/骨架数据库摘要_20260819.json": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/registry/database_summary.json",
    "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819/骨架筛选漏斗_20260819.tsv": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/qc/screening_funnel.tsv",
    "boltzgen/resources/data/SAbDab2_VHH骨架登记表_20260819/骨架排除原因摘要_20260819.tsv": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/qc/exclusion_reason_summary.tsv",
    "boltzgen/resources/data/BoltzGen旧12骨架第一轮摘要_20260819/候选指标_20260819.csv": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/analysis/candidate_metrics.csv",
    "boltzgen/resources/data/BoltzGen旧12骨架第一轮摘要_20260819/过滤摘要_20260819.csv": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/analysis/filter_summary.csv",
    "boltzgen/resources/data/BoltzGen旧12骨架第一轮摘要_20260819/一致性验证_20260819.json": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/analysis/validation_report.json",
    "boltzgen/resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/候选指标_20260820.csv": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/analysis/candidates.csv",
    "boltzgen/resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/过滤摘要_20260820.csv": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/analysis/filter_summary.csv",
    "boltzgen/resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/运行摘要_20260820.json": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/analysis/run_summary.json",
    "boltzgen/resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/深度探针摘要_20260820.json": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/analysis/deep_probe_summary.json",
    "boltzgen/resources/data/AI结构资产验证登记册_20260826/结构队列登记册_20260826.tsv": "data/boltzgen_data/ai_validation_assets_v1/cohort_registry.tsv",
    "boltzgen/resources/data/AI结构资产验证登记册_20260826/结构队列摘要_20260826.tsv": "data/boltzgen_data/ai_validation_assets_v1/cohort_summary.tsv",
    "boltzgen/resources/data/AI结构资产验证登记册_20260826/骨架集合对比_20260826.tsv": "data/boltzgen_data/ai_validation_assets_v1/scaffold_comparison.tsv",
    "boltzgen/resources/data/AI结构资产验证登记册_20260826/验证摘要_20260826.json": "data/boltzgen_data/ai_validation_assets_v1/validation_summary.json",
    "boltzgen/tools/one_off/report_builders/构建MVP数据资产报告_20260818.py": "data/boltzgen_data/mvp_assets_v0.3.2/metadata/build_report_artifact.py",
    "boltzgen/tools/one_off/report_builders/构建MVP运行报告_20260818.py": "data/boltzgen_data/mvp_run_001/scripts/build_report_artifact.py",
    "boltzgen/tools/one_off/report_builders/构建VHH骨架统计报告_20260819.py": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/scripts/build_report_artifact.py",
    "boltzgen/tools/one_off/report_builders/构建旧12骨架第一轮报告_20260819.py": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/scripts/build_report_artifact.py",
    "boltzgen/tools/one_off/report_builders/构建旧12骨架Mac增强报告_20260820.py": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/scripts/build_report_artifact.py",
    "boltzgen/tools/one_off/report_builders/构建BoltzGen数据流原理报告_20260819.py": "data/boltzgen_data/boltzgen_vhh_glp1_explainer/build_report.py",
    "boltzgen/tools/one_off/notebook_builders/构建MVP复盘笔记本_20260818.py": "data/boltzgen_data/mvp_run_001/scripts/build_notebook.py",
    "boltzgen/tools/one_off/notebook_builders/构建VHH骨架审计笔记本_20260819.py": "data/boltzgen_data/sabdab2_vhh_scaffolds_v1/scripts/build_audit_notebook.py",
    "boltzgen/tools/one_off/notebook_builders/构建旧12骨架第一轮笔记本_20260819.py": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/scripts/build_notebook.py",
    "boltzgen/tools/one_off/notebook_builders/构建旧12骨架Mac增强笔记本_20260820.py": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/scripts/build_notebook.py",
    "boltzgen/tools/one_off/profiling/剖析MVP数据资产_20260818.py": "data/boltzgen_data/mvp_assets_v0.3.2/metadata/profile_assets.py",
    "boltzgen/tools/one_off/profiling/剖析BoltzGen检查点_20260818.py": "data/boltzgen_data/mvp_assets_v0.3.2/metadata/profile_checkpoints.py",
    "boltzgen/tools/one_off/release/封存旧12骨架第一轮交付_20260819.py": "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819/scripts/finalize_release.py",
    "boltzgen/tools/one_off/release/封存旧12骨架Mac增强交付_20260820.py": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/scripts/finalize_release.py",
}


def sha256(path: Path) -> str:
    """流式计算单文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_path(repo_path: str) -> str:
    """根据显式映射或同名目录映射解析旧工作区路径。"""

    if repo_path in EXPLICIT_MAP:
        return EXPLICIT_MAP[repo_path]
    for destination_prefix, source_prefix in PREFIX_MAP.items():
        if repo_path.startswith(destination_prefix):
            return source_prefix + repo_path.removeprefix(destination_prefix)
    return ""


def category(path: Path) -> str:
    """按目录和扩展名给仓库文件标注用途类别。"""

    parts = path.parts
    if path.suffix == ".html":
        return "report_html"
    if path.suffix == ".ipynb":
        return "notebook"
    if path.suffix == ".py" and "one_off" in parts:
        return "code_one_off"
    if path.suffix == ".py":
        return "code_main_or_policy"
    if "resources" in parts and "data" in parts:
        return "curated_data"
    if "manifests" in parts:
        return "manifest"
    return "documentation_or_configuration"


def naming_role(path: Path) -> str:
    """说明日期位于文件、父尝试包，还是该文件只是内部实现。"""

    text = path.as_posix()
    if any(part.startswith(("mvp_", "round1_", "enhanced_", "sabdab2_", "asset_", "active_")) and "20" in part for part in path.parts):
        if path.name.startswith(("运行", "准备", "筛选", "验证")):
            return "dated_public_entry"
        if "/scripts/" in f"/{text}/" or path.name == "validate_assets.py":
            return "internal_implementation_in_dated_attempt"
    if "tools/one_off" in text:
        return "dated_one_off"
    if any(char.isdigit() for char in path.stem) and "2026" in path.stem:
        return "dated_artifact"
    return "stable_repository_file"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT.parent)
    args = parser.parse_args()
    source_root = args.source_root.resolve()

    rows: list[dict[str, object]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(ROOT).parts or path == OUTPUT:
            continue
        relative = path.relative_to(ROOT).as_posix()
        old_relative = legacy_path(relative)
        old_path = source_root / old_relative if old_relative else None
        old_exists = bool(old_path and old_path.is_file())
        repo_sha = sha256(path)
        source_sha = sha256(old_path) if old_exists and old_path is not None else ""
        if old_exists:
            transformation = "copied_preserving_bytes" if source_sha == repo_sha else "copied_renamed_and_sanitized"
        elif path.suffix == ".html" and "20260826" in path.name:
            transformation = "generated_from_canonical_artifact"
        else:
            transformation = "repository_authored_or_generated_20260826"
        rows.append(
            {
                "repository_path": relative,
                "route": relative.split("/", 1)[0] if "/" in relative else "root",
                "category": category(Path(relative)),
                "naming_role": naming_role(Path(relative)),
                "size_bytes": path.stat().st_size,
                "sha256": repo_sha,
                "legacy_workspace_path": old_relative,
                "legacy_sha256": source_sha,
                "transformation": transformation,
            }
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} repository file mappings to {OUTPUT.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
