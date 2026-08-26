#!/usr/bin/env python3
"""构建 BoltzGen Mac 增强筛选的规范 Data Analytics 技术报告 artifact。

这个脚本只读取当前 campaign 的 ``analysis/``、``provenance/`` 和 ``runs/``，
并且只写 ``report/``。它不会加载模型、不会重新执行 BoltzGen，也不会修改旧
round1。最终 HTML 必须再由 Data Analytics 插件附带的
``deliver_portable_artifact.mjs`` 从本脚本写出的规范 JSON 统一打包；不要为报告
另写一套 HTML/JavaScript 图表运行时。

报告把两个统计域严格分开：

* 主增强筛选：12 个旧 VHH 骨架 × 2 个单 checkpoint 支路 × 每支路 2 个候选，
  合计 48 个候选、96 个复折叠样本；
* 近官方深度探针：仅 7XL0、adherence checkpoint、4 个候选、每候选 1 个复折叠
  样本；这个探针只作独立描述，绝不并入主筛选分母。

本脚本生成的内容是预训练模型推理与候选生成报告，不是模型权重训练报告。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# 以脚本所在 campaign 为唯一工作根，避免意外读写同级旧 round1。
项目根目录 = Path(__file__).resolve().parent.parent
分析目录 = 项目根目录 / "analysis"
溯源目录 = 项目根目录 / "provenance"
运行目录 = 项目根目录 / "runs"
报告目录 = 项目根目录 / "report"

# 用户最终打开的 HTML 标题与报告 JSON 中的标题必须完全一致。
报告标题 = "BoltzGen Mac：旧 12 条 VHH 骨架增强筛选与复盘"

# 主分析的冻结统计合同。任何数量漂移都应阻断报告，而不是静默改写分母。
主骨架数 = 12
主检查点数 = 2
主候选数 = 48
主复折叠样本数 = 96
每候选过滤项数 = 10
深度探针候选数 = 4
深度探针样本数 = 4

# 两条主 checkpoint 的机器字段与读者字段分开，避免把 profile 名当结论。
检查点中文名 = {
    "design_diverse": "多样性检查点",
    "design_adherence": "骨架遵循检查点",
}

# 七个实际进程阶段的中文名称；前两个是输入检查与配置，不加载设计模型。
阶段中文名 = {
    "00_check": "输入检查",
    "00_configure": "配置展开",
    "01_design": "结构扩散设计",
    "02_inverse_folding": "逆折叠序列采样",
    "03_folding": "复合物复折叠",
    "04_analysis": "指标分析",
    "05_filtering": "过滤与排序",
}


def 当前世界时() -> str:
    """返回带 UTC 时区的 ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def 相对路径(path: Path) -> str:
    """把 campaign 内路径转换为安全、稳定的 POSIX 相对路径。"""

    return path.resolve().relative_to(项目根目录.resolve()).as_posix()


def 读取JSON(path: Path) -> Any:
    """严格读取非空 UTF-8 JSON。"""

    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"缺少或为空的 JSON：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def 写JSON(path: Path, payload: Any) -> None:
    """只在 report 目录内原子写 JSON，避免出现半文件。"""

    if path.resolve().parent != 报告目录.resolve():
        raise ValueError(f"报告生成器只允许写 report/：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def 文件哈希(path: Path, chunk_size: int = 2 * 1024 * 1024) -> str:
    """流式计算 SHA-256，供构建记录核对输入与输出。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def 转布尔(value: Any, field: str) -> bool:
    """严格解析布尔值；不把任意非空字符串当作真。"""

    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"{field} 不是合法布尔值：{value!r}")


def 转数字(value: Any, field: str) -> int | float | None:
    """把 CSV 文本转换为有限数；空值保留为 ``None``。"""

    text = str(value).strip()
    if not text:
        return None
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{field} 不是有限数：{value!r}")
    return int(number) if number.is_integer() else number


def 读取CSV(
    path: Path,
    *,
    numeric: Iterable[str] = (),
    boolean: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """读取 CSV 并按显式字段合同转换类型。"""

    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"缺少或为空的 CSV：{path}")
    numeric_fields = set(numeric)
    boolean_fields = set(boolean)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict[str, Any]] = []
        for raw in reader:
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if key in numeric_fields:
                    row[key] = 转数字(value, key)
                elif key in boolean_fields:
                    row[key] = 转布尔(value, key)
                else:
                    row[key] = value
            rows.append(row)
    return rows


def 中位数(values: Iterable[Any]) -> float | None:
    """忽略空值后返回中位数；没有有限值时返回 ``None``。"""

    cleaned = [float(value) for value in values if value is not None]
    return statistics.median(cleaned) if cleaned else None


def 四舍五入(value: Any, digits: int = 4) -> Any:
    """只对数值做展示级四舍五入，布尔与空值保持原样。"""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(value, digits)
    return value


def 来源(
    source_id: str,
    label: str,
    path: str | None = None,
    *,
    description: str | None = None,
    sql: str | None = None,
    tables: Iterable[str] = (),
    href: str | None = None,
) -> dict[str, Any]:
    """创建 portable artifact 可用于来源弹窗的规范来源对象。"""

    source: dict[str, Any] = {"id": source_id, "label": label}
    if path:
        source["path"] = path
    if href:
        source["href"] = href
    if description:
        source["description"] = description
    if sql:
        source["query"] = {
            "engine": "duckdb",
            "language": "sql",
            "sql": sql,
            "description": description or label,
            "tables_used": list(tables),
        }
    return source


def 卡片(
    card_id: str,
    label: str,
    field: str,
    source: dict[str, Any],
    *,
    unit: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """创建只有一个主指标的 metric card。"""

    metric: dict[str, Any] = {"label": label, "field": field}
    if unit:
        metric["unit"] = unit
    return {
        "id": card_id,
        "description": description or label,
        "dataset": "headline",
        "source": source,
        "metrics": [metric],
    }


def 表格(
    table_id: str,
    title: str,
    subtitle: str,
    dataset: str,
    source: dict[str, Any],
    columns: list[dict[str, Any]],
    *,
    sort_field: str,
    sort_direction: str = "asc",
    density: str = "compact",
) -> dict[str, Any]:
    """创建全宽、可排序的审计表。"""

    return {
        "id": table_id,
        "title": title,
        "subtitle": subtitle,
        "dataset": dataset,
        "source": source,
        "density": density,
        "layout": "full",
        "defaultSort": {"field": sort_field, "direction": sort_direction},
        "columns": columns,
    }


def 最新预检() -> tuple[Path, dict[str, Any]]:
    """选择与近官方探针对应的最新成功 preflight。"""

    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((溯源目录 / "preflight").glob("*.json")):
        payload = 读取JSON(path)
        if payload.get("status") != "PASS":
            continue
        if payload.get("profile") == "near_official_adherence_7xl0":
            return path, payload
        candidates.append((path, payload))
    if not candidates:
        raise FileNotFoundError("provenance/preflight 中没有成功记录")
    return candidates[-1]


def 验证合同(
    summary: dict[str, Any],
    validation: dict[str, Any],
    candidates: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    deep_summary: dict[str, Any],
    deep_candidates: list[dict[str, Any]],
    deep_samples: list[dict[str, Any]],
) -> None:
    """在写报告前再次执行关键分母和域隔离检查。"""

    counts = summary.get("counts", {})
    expected = {
        "scaffolds": 主骨架数,
        "checkpoints": 主检查点数,
        "candidates": 主候选数,
        "folding_samples": 主复折叠样本数,
        "filter_rows": 主候选数 * 每候选过滤项数,
    }
    for key, value in expected.items():
        if int(counts.get(key, -1)) != value:
            raise ValueError(f"主统计合同漂移：{key}={counts.get(key)!r}，预期 {value}")
    if len(candidates) != 主候选数 or len(samples) != 主复折叠样本数:
        raise ValueError("主候选或复折叠样本 CSV 行数不符合 48/96 合同")
    if len(filters) != 主候选数 * 每候选过滤项数:
        raise ValueError("主过滤长表不是 48×10")
    if validation.get("failed_checks"):
        raise ValueError(f"analysis/validation.json 存在失败检查：{validation['failed_checks']}")

    # 深度探针必须是独立命名空间；任何主候选键出现在深度表都阻断报告。
    deep_counts = deep_summary.get("counts", deep_summary)
    deep_candidate_count = int(
        deep_counts.get("candidates", deep_counts.get("candidate_count", len(deep_candidates)))
    )
    deep_sample_count = int(
        deep_counts.get("folding_samples", deep_counts.get("folding_sample_count", len(deep_samples)))
    )
    if deep_candidate_count != 深度探针候选数 or len(deep_candidates) != 深度探针候选数:
        raise ValueError("near-official 深度探针候选数必须恰为 4")
    if deep_sample_count != 深度探针样本数 or len(deep_samples) != 深度探针样本数:
        raise ValueError("near-official 深度探针样本数必须恰为 4")
    main_ids = {row["candidate_id"] for row in candidates}
    deep_ids = {row["candidate_id"] for row in deep_candidates}
    if main_ids & deep_ids:
        raise ValueError("主筛选与深度探针候选主键发生碰撞")
    if any(not candidate_id.startswith("deep_probe::") for candidate_id in deep_ids):
        raise ValueError("深度探针候选必须使用 deep_probe:: 命名空间")


def 构建报告() -> tuple[dict[str, Any], dict[str, Any]]:
    """读取规范分析产物并构建完整 report artifact 与构建注记。"""

    # 先读取机器可读摘要、验证报告和冻结输入合同。
    summary = 读取JSON(分析目录 / "run_summary.json")
    validation = 读取JSON(分析目录 / "validation.json")
    input_manifest = 读取JSON(溯源目录 / "enhanced_input_manifest.json")
    profiles = 读取JSON(溯源目录 / "profiles.json")
    preflight_path, preflight = 最新预检()

    # 主候选表的字段类型是后续图表和表格的唯一事实来源。
    candidates = 读取CSV(
        分析目录 / "candidates.csv",
        numeric=(
            "local_candidate_index", "selection_rank", "scaffold_resolution_a",
            "scaffold_r_free", "design_residue_count", "analysis_best_sample_index",
            "writer_best_sample_index", "writer_cif_max_abs_coordinate_error_a",
            "design_to_target_iptm", "design_ptm", "iptm", "ptm",
            "min_design_to_target_pae_a", "filter_rmsd_a", "filter_rmsd_design_a",
            "prerefold_hotspot_coverage_fraction_lt8a",
            "refold_hotspot_coverage_heavy_fraction_lt8a",
            "refold_hotspot_coverage_ca_fraction_lt8a",
            "his7_min_heavy_atom_distance_a", "ala8_min_heavy_atom_distance_a",
            "his7_min_ca_distance_a", "ala8_min_ca_distance_a",
            "delta_sasa_refolded_a2", "plip_hbonds_refolded",
            "plip_saltbridge_refolded", "liability_score",
            "liability_num_violations", "computed_filter_pass_count",
            "computed_filter_total", "failed_filter_count",
            "boltzgen_internal_prefix_pass_score",
        ),
        boolean=(
            "framework_sequence_unchanged", "same_best_sample",
            "pass_all_default_filters", "selected_by_budget",
        ),
    )
    samples = 读取CSV(
        分析目录 / "folding_samples.csv",
        numeric=(
            "selection_rank", "sample_index", "analysis_selection_score",
            "writer_selection_score", "min_interaction_pae",
            "min_design_to_target_pae", "interaction_pae", "ligand_iptm",
            "protein_iptm", "iptm", "design_iptm", "design_iiptm",
            "design_to_target_iptm", "design_residue_iptm", "design_ptm",
            "target_ptm", "ptm", "complex_plddt", "complex_iplddt",
            "complex_pde", "complex_ipde", "design_ipsae_min",
            "design_to_target_ipsae", "target_to_design_ipsae",
        ),
        boolean=("selected_by_analysis", "selected_by_writer"),
    )
    filters = 读取CSV(
        分析目录 / "filter_long.csv",
        numeric=("selection_rank", "filter_order", "observed_value", "threshold"),
        boolean=("passed",),
    )
    filter_summary = 读取CSV(
        分析目录 / "filter_summary.csv",
        numeric=(
            "filter_order", "threshold", "candidate_count", "passed_count",
            "failed_count", "failure_rate",
        ),
    )
    checkpoints = 读取CSV(
        分析目录 / "checkpoint_summary.csv",
        numeric=(
            "scaffold_count", "candidate_count", "folding_sample_count",
            "filter_survivors", "budget_items", "median_design_to_target_iptm",
            "median_design_ptm", "median_filter_rmsd_a",
            "prerefold_hotspot_positive", "writer_analysis_best_match",
        ),
    )
    scaffold_checkpoints = 读取CSV(
        分析目录 / "scaffold_checkpoint_summary.csv",
        numeric=(
            "selection_rank", "candidate_count", "folding_sample_count",
            "filter_survivors", "budget_items", "median_design_to_target_iptm",
            "median_design_ptm", "median_filter_rmsd_a",
            "prerefold_hotspot_positive", "writer_analysis_best_match",
        ),
    )
    runs = 读取CSV(
        分析目录 / "runs.csv",
        numeric=(
            "selection_rank", "elapsed_seconds", "requested_candidates",
            "folding_samples_per_candidate", "budget",
        ),
    )
    stage_summary = 读取CSV(
        分析目录 / "stage_summary.csv",
        numeric=(
            "attempt_count", "total_elapsed_seconds", "median_elapsed_seconds",
            "maximum_elapsed_seconds",
        ),
    )
    resources = 读取CSV(
        分析目录 / "stage_resource_summary.csv",
        numeric=(
            "selection_rank", "sample_count", "sampled_duration_seconds",
            "peak_process_tree_rss_gib", "peak_process_tree_cpu_percent_sum",
            "minimum_system_free_gib", "peak_system_active_gib", "swap_first_gib",
            "swap_last_gib", "swap_stage_delta_gib", "swap_stage_range_gib",
            "minimum_disk_free_gib",
        ),
        boolean=("mps_process_memory_measured",),
    )
    stress_attempts = 读取CSV(
        分析目录 / "stress_attempts.csv",
        numeric=(
            "selection_rank", "elapsed_seconds", "completed_pipeline_stage_count",
            "partial_design_cif_count",
        ),
        boolean=("keyboard_interrupt_evidence",),
    )
    stress_resources = 读取CSV(
        分析目录 / "stress_resource_summary.csv",
        numeric=(
            "sample_count", "peak_process_tree_rss_gib", "minimum_system_free_gib",
            "swap_first_gib", "swap_last_gib", "swap_stage_delta_gib",
            "swap_stage_range_gib",
        ),
    )
    inventory = 读取CSV(
        分析目录 / "output_inventory.csv",
        numeric=("selection_rank", "size_bytes"),
    )
    lineages = 读取CSV(
        分析目录 / "candidate_lineage.csv",
        numeric=("selection_rank", "size_bytes"),
    )

    # 深度探针产物由分析脚本独立写出，绝不从主表筛选或拼接生成。
    deep_summary = 读取JSON(分析目录 / "deep_probe_summary.json")
    deep_candidates = 读取CSV(
        分析目录 / "deep_probe_candidates.csv",
        numeric=(
            "local_candidate_index", "selection_rank", "analysis_best_sample_index",
            "writer_best_sample_index", "design_to_target_iptm", "design_ptm", "iptm",
            "ptm", "min_design_to_target_pae_a", "filter_rmsd_a",
            "filter_rmsd_design_a", "prerefold_hotspot_coverage_fraction_lt8a",
            "refold_hotspot_coverage_heavy_fraction_lt8a",
            "refold_hotspot_coverage_ca_fraction_lt8a",
            "his7_min_heavy_atom_distance_a", "ala8_min_heavy_atom_distance_a",
            "his7_min_ca_distance_a", "ala8_min_ca_distance_a",
            "delta_sasa_refolded_a2", "plip_hbonds_refolded",
            "plip_saltbridge_refolded", "computed_filter_pass_count",
            "computed_filter_total", "failed_filter_count",
        ),
        boolean=(
            "framework_sequence_unchanged", "same_best_sample",
            "pass_all_default_filters", "selected_by_budget",
        ),
    )
    deep_samples = 读取CSV(
        分析目录 / "deep_probe_folding_samples.csv",
        numeric=(
            "selection_rank", "sample_index", "analysis_selection_score",
            "writer_selection_score", "min_interaction_pae",
            "min_design_to_target_pae", "interaction_pae", "ligand_iptm",
            "protein_iptm", "iptm", "design_iptm", "design_iiptm",
            "design_to_target_iptm", "design_residue_iptm", "design_ptm",
            "target_ptm", "ptm", "complex_plddt", "complex_iplddt",
            "complex_pde", "complex_ipde", "design_ipsae_min",
            "design_to_target_ipsae", "target_to_design_ipsae",
        ),
        boolean=("selected_by_analysis", "selected_by_writer"),
    )
    deep_filters = 读取CSV(
        分析目录 / "deep_probe_filter_long.csv",
        numeric=("selection_rank", "filter_order", "observed_value", "threshold"),
        boolean=("passed",),
    )
    deep_runs = 读取CSV(
        分析目录 / "deep_probe_runs.csv",
        numeric=(
            "selection_rank", "elapsed_seconds", "requested_candidates",
            "folding_samples_per_candidate", "budget",
        ),
    )
    deep_stage = 读取CSV(
        分析目录 / "deep_probe_stage_timing.csv",
        numeric=("selection_rank", "elapsed_seconds", "return_code", "monitor_sample_count"),
    )
    deep_resources = 读取CSV(
        分析目录 / "deep_probe_resource_summary.csv",
        numeric=(
            "selection_rank", "sample_count", "sampled_duration_seconds",
            "peak_process_tree_rss_gib", "peak_process_tree_cpu_percent_sum",
            "minimum_system_free_gib", "peak_system_active_gib", "swap_first_gib",
            "swap_last_gib", "swap_stage_delta_gib", "swap_stage_range_gib",
            "minimum_disk_free_gib",
        ),
        boolean=("mps_process_memory_measured",),
    )

    # 构建报告前再次验证主统计和独立探针合同。
    验证合同(
        summary, validation, candidates, samples, filters,
        deep_summary, deep_candidates, deep_samples,
    )
    if len(deep_filters) != 深度探针候选数 * 每候选过滤项数:
        raise ValueError("near-official 深度探针过滤长表不是 4×10")
    if len(deep_runs) != 1:
        raise ValueError("near-official 深度探针必须恰有一个完整 run")

    # 规范化读者字段，原始机器字段仍保留在来源 CSV 中。
    for row in candidates:
        row["checkpoint_cn"] = 检查点中文名.get(row["checkpoint"], row["checkpoint"])
        row["candidate_label"] = f"{row['pdb_code']} · {row['checkpoint_cn']} · #{int(row['local_candidate_index']) + 1}"
        row["best_index_relation"] = "一致" if row["same_best_sample"] else "不一致"
        for field in (
            "design_to_target_iptm", "design_ptm", "iptm", "ptm",
            "min_design_to_target_pae_a", "filter_rmsd_a", "filter_rmsd_design_a",
            "prerefold_hotspot_coverage_fraction_lt8a",
            "refold_hotspot_coverage_heavy_fraction_lt8a",
            "refold_hotspot_coverage_ca_fraction_lt8a",
            "his7_min_heavy_atom_distance_a", "ala8_min_heavy_atom_distance_a",
            "his7_min_ca_distance_a", "ala8_min_ca_distance_a",
            "delta_sasa_refolded_a2",
        ):
            row[field] = 四舍五入(row.get(field), 4)
    for row in checkpoints:
        row["checkpoint_cn"] = 检查点中文名.get(row["checkpoint"], row["checkpoint"])
        row["writer_analysis_mismatch"] = int(row["candidate_count"] - row["writer_analysis_best_match"])
        row["best_match_rate"] = row["writer_analysis_best_match"] / row["candidate_count"]
        for field in ("median_design_to_target_iptm", "median_design_ptm", "median_filter_rmsd_a", "best_match_rate"):
            row[field] = 四舍五入(row[field], 4)
    for row in scaffold_checkpoints:
        row["checkpoint_cn"] = 检查点中文名.get(row["checkpoint"], row["checkpoint"])
        row["scaffold_checkpoint_label"] = f"{row['pdb_code']} · {row['checkpoint_cn']}"
        for field in ("median_design_to_target_iptm", "median_design_ptm", "median_filter_rmsd_a"):
            row[field] = 四舍五入(row[field], 4)
    for row in filter_summary:
        row["failure_percent"] = round(100 * float(row["failure_rate"]), 1)
        row["threshold_display"] = f"{row['operator']} {row['threshold']} {row['unit']}"
    for row in stage_summary:
        row["checkpoint_cn"] = 检查点中文名.get(row["checkpoint"], row["checkpoint"])
        row["stage_cn"] = 阶段中文名.get(row["stage"], row["stage"])
        row["stage_checkpoint"] = f"{row['stage_cn']} · {row['checkpoint_cn']}"
        for field in ("total_elapsed_seconds", "median_elapsed_seconds", "maximum_elapsed_seconds"):
            row[field] = 四舍五入(row[field], 3)

    # 复折叠前/后热点覆盖使用不同几何语义，因此构建成长表而不覆盖原字段。
    hotspot_stage: list[dict[str, Any]] = []
    for row in candidates:
        hotspot_stage.extend(
            [
                {
                    "candidate_id": row["candidate_id"],
                    "candidate_label": row["candidate_label"],
                    "checkpoint_cn": row["checkpoint_cn"],
                    "stage_definition": "复折叠前 token-center 覆盖",
                    "coverage_fraction": row["prerefold_hotspot_coverage_fraction_lt8a"],
                },
                {
                    "candidate_id": row["candidate_id"],
                    "candidate_label": row["candidate_label"],
                    "checkpoint_cn": row["checkpoint_cn"],
                    "stage_definition": "复折叠后独立重原子覆盖",
                    "coverage_fraction": row["refold_hotspot_coverage_heavy_fraction_lt8a"],
                },
            ]
        )

    # 候选流只比较同一“候选”单位；96 个复折叠样本另放在主指标卡中。
    candidate_flow = [
        {"order": 1, "stage": "生成并完成分析", "candidate_count": 主候选数},
        {"order": 2, "stage": "进入十项过滤", "candidate_count": 主候选数},
        {
            "order": 3,
            "stage": "通过全部十项过滤",
            "candidate_count": int(summary["counts"]["strict_filter_survivors"]),
        },
        {
            "order": 4,
            "stage": "预算目录展示（非通过）",
            "candidate_count": int(summary["counts"]["budget_items"]),
        },
    ]

    # 分析选样与 writer 选样的 41/7 一致性按候选计数。
    agreement = Counter(row["best_index_relation"] for row in candidates)
    best_index_agreement = [
        {"relation": "一致", "candidate_count": agreement.get("一致", 0)},
        {"relation": "不一致", "candidate_count": agreement.get("不一致", 0)},
    ]

    # 资源图只画可直接解释的进程树 RSS；统一内存中的独立 MPS 占用未被测得。
    resource_by_stage: list[dict[str, Any]] = []
    grouped_resources: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in resources:
        grouped_resources[(row["stage"], row["checkpoint"])].append(row)
    for (stage, checkpoint), rows_for_group in grouped_resources.items():
        resource_by_stage.append(
            {
                "stage": stage,
                "stage_cn": 阶段中文名.get(stage, stage),
                "checkpoint": checkpoint,
                "checkpoint_cn": 检查点中文名.get(checkpoint, checkpoint),
                "peak_process_tree_rss_gib": round(
                    max(float(row["peak_process_tree_rss_gib"]) for row in rows_for_group), 3
                ),
                "max_swap_stage_delta_gib": round(
                    max(float(row["swap_stage_delta_gib"]) for row in rows_for_group), 3
                ),
                "minimum_system_free_gib": round(
                    min(float(row["minimum_system_free_gib"]) for row in rows_for_group), 3
                ),
                "minimum_disk_free_gib": round(
                    min(float(row["minimum_disk_free_gib"]) for row in rows_for_group), 3
                ),
            }
        )
    resource_by_stage.sort(key=lambda row: (list(阶段中文名).index(row["stage"]), row["checkpoint"]))

    # 完整清单行数很大；报告正文按格式聚合，逐文件哈希仍保留在 source CSV。
    inventory_grouped: dict[str, dict[str, Any]] = {}
    for row in inventory:
        bucket = inventory_grouped.setdefault(
            row["format"], {"format": row["format"], "file_count": 0, "total_bytes": 0}
        )
        bucket["file_count"] += 1
        bucket["total_bytes"] += int(row["size_bytes"])
    output_formats = sorted(
        (
            {
                **row,
                "total_mib": round(row["total_bytes"] / (1024**2), 3),
            }
            for row in inventory_grouped.values()
        ),
        key=lambda row: (-row["total_bytes"], row["format"]),
    )

    # 八个候选血缘角色都应恰有 48 项；正文只展示角色计数和用途。
    lineage_counts = Counter(row.get("artifact_role", row.get("role", "")) for row in lineages)
    lineage_meanings = {
        "design_cif": "结构扩散设计坐标",
        "design_npz": "结构扩散中间数组",
        "inverse_fold_cif": "逆折叠后的候选序列与坐标",
        "inverse_fold_npz": "逆折叠中间数组",
        "fold_npz": "每候选两个复折叠样本及标量分数",
        "refold_cif": "writer 选中样本写出的复折叠坐标",
        "analysis_metrics_csv": "候选级分析代理指标",
        "filter_metrics_csv": "十项过滤与批内排序结果",
    }
    lineage_summary = [
        {"artifact_role": role, "file_count": count, "meaning": lineage_meanings.get(role, "")}
        for role, count in sorted(lineage_counts.items())
    ]

    # 环境表只使用 frozen provenance；不在报告构建时重新探测或修改系统。
    environment = [
        {"item": "硬件架构", "value": preflight.get("machine", "arm64"), "meaning": "Apple Silicon 单机"},
        {
            "item": "统一内存",
            "value": f"{preflight['hardware_memory_bytes'] / (1024**3):.0f} GiB",
            "meaning": "中央处理器与图形处理器共享；进程树 RSS 不是完整图形内存计量",
        },
        {"item": "操作系统", "value": preflight.get("platform", "macOS arm64"), "meaning": "本次实际运行平台"},
        {"item": "PyTorch", "value": preflight["torch_probe"]["torch"], "meaning": "MPS built=true、available=true"},
        {"item": "BoltzGen 基线", "value": input_manifest["runtime"]["official_release_baseline"], "meaning": "官方版本基线"},
        {
            "item": "实验性 MPS 代码",
            "value": input_manifest["runtime"]["experimental_mps_source_commit"][:12],
            "meaning": "未合并 Apple Metal Performance Shaders 分支快照",
        },
        {"item": "计算设备策略", "value": "MPS + CPU fallback", "meaning": "PYTORCH_ENABLE_MPS_FALLBACK=1；不等同于 NVIDIA CUDA"},
        {"item": "精度/并发", "value": "FP32；batch size 1；worker 1", "meaning": "降低统一内存峰值"},
        {"item": "离线策略", "value": "离线", "meaning": "模型权重和分子字典均来自已校验本地缓存"},
    ]

    # 输入清单把目标和 12 条骨架分列，保留来源主键、结构质量和设计区长度。
    target = input_manifest["target"]
    scope_rows = [
        {
            "object": "正靶",
            "identity": "PDB 6X18 · GLP-1(7–36)",
            "role": "唯一正靶几何",
            "count": target["residue_count"],
            "unit": "残基",
            "detail": f"序列 {target['sequence']}；提示位点 His7/Ala8；C 端酰胺未原子级确认",
        },
        {
            "object": "VHH 骨架",
            "identity": "旧 12 条 SAbDab2 单域重链可变区骨架",
            "role": "固定框架；三个互补决定区可设计",
            "count": 主骨架数,
            "unit": "骨架",
            "detail": "10 条 PRIMARY、2 条 RESERVE；角色是冻结输入标签，不是本轮结果",
        },
        {
            "object": "主筛选候选",
            "identity": "diverse 24 + adherence 24",
            "role": "主分析总体",
            "count": 主候选数,
            "unit": "候选",
            "detail": "每候选 2 个复折叠样本；十项过滤分母固定为 48",
        },
        {
            "object": "近官方探针",
            "identity": "仅 7XL0 · adherence",
            "role": "独立描述性探针",
            "count": 深度探针候选数,
            "unit": "候选",
            "detail": "每候选 1 个复折叠样本；绝不并入 48/96 主分母",
        },
    ]
    scaffold_registry: list[dict[str, Any]] = []
    for record in input_manifest["scaffold_population"]["records"]:
        scaffold_registry.append(
            {
                "selection_rank": record["selection_rank"],
                "pdb_code": record["pdb_code"],
                "source_chain": record["source_hchain"],
                "sabdab_id": record["sabdab_id"],
                "role": record["role"],
                "framework_cluster_id": record["framework_cluster_id"],
                "method": record["method"],
                "resolution_a": record["resolution_a"],
                "r_free": record["r_free"],
                "variable_length_aa": record["variable_length_aa"],
                "cdr1_length_aa": record["cdr1_length_aa"],
                "cdr2_length_aa": record["cdr2_length_aa"],
                "cdr3_length_aa": record["cdr3_length_aa"],
                "prior_check": record["prior_boltzgen_check_status"],
                "design_spec": record["design_spec"],
            }
        )

    # 模型步骤的输入/输出合同。这里描述运行时数据流，不暗示重新训练损失。
    stage_io = [
        {"order": 1, "stage": "输入检查", "input": "骨架 CIF、目标 CIF、YAML 设计掩码", "operation": "结构与索引合同检查", "output": "规范化 check_spec.cif", "format": "CIF / YAML"},
        {"order": 2, "stage": "结构扩散设计", "input": "固定 VHH 框架 + 可设计区掩码 + GLP-1 坐标 + His7/Ala8 提示", "operation": "条件扩散去噪采样", "output": "候选复合物几何与中间数组", "format": "CIF + NPZ"},
        {"order": 3, "stage": "逆折叠序列采样", "input": "候选主链几何 + 可设计区掩码", "operation": "按几何条件采样氨基酸；框架不变", "output": "三个设计区序列与完整 VHH 链", "format": "CIF + NPZ"},
        {"order": 4, "stage": "复合物复折叠", "input": "候选完整 VHH 序列 + GLP-1", "operation": "重新预测复合物并产生多个样本", "output": "2 个样本坐标/分数；writer 写 1 个 CIF", "format": "NPZ + CIF"},
        {"order": 5, "stage": "指标分析", "input": "生成结构、复折叠结构、序列", "operation": "自洽性、界面代理、热点与序列规则", "output": "候选级指标与逐目标指标", "format": "CSV + NPZ"},
        {"order": 6, "stage": "十项过滤与排序", "input": "候选指标", "operation": "逐项按冻结阈值重算；每骨架预算展示", "output": "all_designs_metrics.csv 与排名结构", "format": "CSV + CIF + PDF"},
    ]

    # 折叠 NPZ 的轴说明来自分析脚本逐文件合同；报告不伪造不存在的完整 PAE 矩阵。
    npz_axes = [
        {"field": "coords", "shape": "[复折叠样本, 原子槽, x/y/z]", "row_axis": "第一轴为样本；主筛选恒为 2", "column_axis": "第二轴为原子槽；第三轴为三维笛卡尔坐标", "meaning": "每个候选两个复折叠结构样本"},
        {"field": "atom_resolved_mask", "shape": "[批次=1, 原子槽]", "row_axis": "唯一输入批次", "column_axis": "与 coords 原子槽同序", "meaning": "真值表示该原子槽可写入坐标文件"},
        {"field": "atom_to_token", "shape": "[批次=1, 原子槽, token]", "row_axis": "唯一输入批次", "column_axis": "原子槽 × 残基/token 归属", "meaning": "布尔归属映射，不是距离矩阵"},
        {"field": "design_to_target_iptm 等标量", "shape": "[复折叠样本]", "row_axis": "每个样本一个值", "column_axis": "无第二维", "meaning": "用于样本选择的计算置信代理"},
        {"field": "完整二维 PAE", "shape": "未保存", "row_axis": "不适用", "column_axis": "不适用", "meaning": "当前 NPZ 只有汇总 PAE 标量，不能绘制完整 PAE 热图"},
    ]

    # 日志与中间产物说明，使使用者能从 stage 状态追到原始输出。
    log_artifacts = [
        {"order": 1, "artifact": "events.jsonl", "grain": "每尝试事件流", "meaning": "启动、阶段开始、阶段完成、错误与结束状态；按写入顺序追加"},
        {"order": 2, "artifact": "logs/<stage>/stdout.log", "grain": "每阶段标准输出", "meaning": "模型进度、步骤耗时和普通运行信息"},
        {"order": 3, "artifact": "logs/<stage>/stderr.log", "grain": "每阶段标准错误", "meaning": "警告、异常堆栈与中断证据；空文件也是有效记录"},
        {"order": 4, "artifact": "logs/<stage>/resources.csv/jsonl", "grain": "约每 2 秒采样", "meaning": "进程树 RSS/CPU、系统 free、swap、磁盘；不含独立 MPS 内存"},
        {"order": 5, "artifact": "stage_status/<stage>.json", "grain": "每阶段一个状态", "meaning": "命令、返回码、耗时、监控状态、检查点哈希与合同结论"},
        {"order": 6, "artifact": "manifests/<stage>.json", "grain": "每阶段文件变化", "meaning": "新增/变化文件、大小、SHA-256 与内容寻址快照位置"},
        {"order": 7, "artifact": "run_status.json", "grain": "每尝试总状态", "meaning": "完整阶段、输出合同、结果 CSV 哈希、开始/结束时间"},
        {"order": 8, "artifact": "analysis/candidate_lineage.csv", "grain": "每候选 8 个角色", "meaning": "把配置、CIF、NPZ、分析表和过滤表连成可审计血缘"},
    ]

    # 验证报告的每项布尔检查转成长表，便于在报告内逐项核对。
    validation_checks = [
        {
            "check": key,
            "passed": bool(value),
            "interpretation": "通过" if value else "失败；不得封装报告",
        }
        for key, value in validation.get("checks", {}).items()
    ]

    # 深度探针表保留 4 条实际候选，明确它不是第二个主筛选批次。
    for row in deep_candidates:
        row["candidate_label"] = f"7XL0 深度探针 · #{int(row['local_candidate_index']) + 1}"
        for field in (
            "design_to_target_iptm", "design_ptm", "min_design_to_target_pae_a",
            "filter_rmsd_a", "filter_rmsd_design_a",
            "prerefold_hotspot_coverage_fraction_lt8a",
            "his7_min_heavy_atom_distance_a", "ala8_min_heavy_atom_distance_a",
        ):
            row[field] = 四舍五入(row.get(field), 4)
    shallow_7xl0 = [
        row for row in candidates
        if row["pdb_code"] == "7XL0" and row["checkpoint"] == "design_adherence"
    ]
    probe_comparison = [
        {
            "scope": "平衡增强筛选：7XL0 adherence",
            "candidate_count": len(shallow_7xl0),
            "folding_samples_per_candidate": 2,
            "design_steps": input_manifest["profiles"]["balanced_adherence_all12"]["design_sampling_steps"],
            "inverse_steps": input_manifest["profiles"]["balanced_adherence_all12"]["inverse_fold_sampling_steps"],
            "folding_steps": input_manifest["profiles"]["balanced_adherence_all12"]["folding_sampling_steps"],
            "median_design_to_target_iptm": 四舍五入(中位数(row["design_to_target_iptm"] for row in shallow_7xl0), 6),
            "median_filter_rmsd_a": 四舍五入(中位数(row["filter_rmsd_a"] for row in shallow_7xl0), 6),
            "strict_survivors": sum(bool(row["pass_all_default_filters"]) for row in shallow_7xl0),
        },
        {
            "scope": "近官方深度探针：7XL0 adherence",
            "candidate_count": len(deep_candidates),
            "folding_samples_per_candidate": 1,
            "design_steps": input_manifest["profiles"]["near_official_adherence_7xl0"]["design_sampling_steps"],
            "inverse_steps": input_manifest["profiles"]["near_official_adherence_7xl0"]["inverse_fold_sampling_steps"],
            "folding_steps": input_manifest["profiles"]["near_official_adherence_7xl0"]["folding_sampling_steps"],
            "median_design_to_target_iptm": 四舍五入(中位数(row["design_to_target_iptm"] for row in deep_candidates), 6),
            "median_filter_rmsd_a": 四舍五入(中位数(row["filter_rmsd_a"] for row in deep_candidates), 6),
            "strict_survivors": sum(bool(row["pass_all_default_filters"]) for row in deep_candidates),
        },
    ]

    # 深度探针资源摘要使用与主表相同字段，但保持独立 dataset。
    deep_resource_table = []
    for row in deep_resources:
        deep_resource_table.append(
            {
                "stage": row.get("stage", ""),
                "stage_cn": 阶段中文名.get(row.get("stage", ""), row.get("stage", "")),
                "peak_process_tree_rss_gib": 四舍五入(row.get("peak_process_tree_rss_gib"), 3),
                "minimum_system_free_gib": 四舍五入(row.get("minimum_system_free_gib"), 3),
                "swap_stage_delta_gib": 四舍五入(row.get("swap_stage_delta_gib"), 3),
                "minimum_disk_free_gib": 四舍五入(row.get("minimum_disk_free_gib"), 3),
                "mps_process_memory_measured": row.get("mps_process_memory_measured", False),
            }
        )

    # 汇总压力中断的最大资源变化；原始逐阶段记录仍保存在 source 表。
    stress_max_swap = max((float(row["swap_stage_delta_gib"]) for row in stress_resources), default=0.0)
    stress_min_free = min((float(row["minimum_system_free_gib"]) for row in stress_resources), default=0.0)
    for row in stress_attempts:
        row["max_swap_stage_delta_gib"] = round(stress_max_swap, 3)
        row["minimum_system_free_gib"] = round(stress_min_free, 3)

    # 机器可读来源对象会同时出现在每个卡片/图/表和 manifest.sources 中。
    preflight_rel = 相对路径(preflight_path)
    sources = {
        "summary": 来源(
            "summary_source", "主筛选机器可读摘要", "analysis/run_summary.json",
            description="48 个主候选、96 个复折叠样本、过滤通过数与最佳样本一致性。",
            sql="SELECT * FROM read_json_auto('analysis/run_summary.json')",
            tables=("analysis/run_summary.json",),
        ),
        "candidates": 来源(
            "candidate_source", "48 个主候选规范表", "analysis/candidates.csv",
            description="两个单 checkpoint 支路的候选级结构、热点、序列与过滤结果；不含深度探针。",
            sql="SELECT * FROM read_csv_auto('analysis/candidates.csv')",
            tables=("analysis/candidates.csv",),
        ),
        "samples": 来源(
            "sample_source", "96 个复折叠样本规范表", "analysis/folding_samples.csv",
            description="每主候选两个复折叠样本及 analysis/writer 两套选样分数。",
            sql="SELECT * FROM read_csv_auto('analysis/folding_samples.csv')",
            tables=("analysis/folding_samples.csv",),
        ),
        "filters": 来源(
            "filter_source", "十项过滤规范表", "analysis/filter_summary.csv",
            description="十项冻结过滤的运算符、阈值、48 候选分母、通过数和失败数。",
            sql="SELECT * FROM read_csv_auto('analysis/filter_summary.csv') ORDER BY filter_order",
            tables=("analysis/filter_summary.csv", "analysis/filter_long.csv"),
        ),
        "checkpoints": 来源(
            "checkpoint_source", "两个 checkpoint 汇总", "analysis/checkpoint_summary.csv",
            description="多样性与骨架遵循支路各 24 个候选的描述性中位数与选样一致性。",
            sql="SELECT * FROM read_csv_auto('analysis/checkpoint_summary.csv') ORDER BY checkpoint",
            tables=("analysis/checkpoint_summary.csv",),
        ),
        "scaffolds": 来源(
            "scaffold_checkpoint_source", "骨架 × checkpoint 汇总", "analysis/scaffold_checkpoint_summary.csv",
            description="12 个骨架在两个 checkpoint 下各 2 个候选的描述性统计。",
            sql="SELECT * FROM read_csv_auto('analysis/scaffold_checkpoint_summary.csv') ORDER BY selection_rank, checkpoint",
            tables=("analysis/scaffold_checkpoint_summary.csv",),
        ),
        "stages": 来源(
            "stage_source", "阶段耗时汇总", "analysis/stage_summary.csv",
            description="24 个主尝试的七阶段累计、中位和最大墙钟时间。",
            sql="SELECT * FROM read_csv_auto('analysis/stage_summary.csv')",
            tables=("analysis/stage_summary.csv", "analysis/stage_timing.csv"),
        ),
        "resources": 来源(
            "resource_source", "主筛选阶段资源摘要", "analysis/stage_resource_summary.csv",
            description="每阶段进程树 RSS、系统 free、swap 变化与磁盘；无独立 MPS 进程内存。",
            sql="SELECT * FROM read_csv_auto('analysis/stage_resource_summary.csv')",
            tables=("analysis/stage_resource_summary.csv", "analysis/resource_samples.csv"),
        ),
        "stress": 来源(
            "stress_source", "双 checkpoint 压力尝试", "analysis/stress_attempts.csv",
            description="历史压力尝试及资源护栏触发证据；不进入 48 个主候选统计。",
            sql="SELECT * FROM read_csv_auto('analysis/stress_attempts.csv')",
            tables=("analysis/stress_attempts.csv", "analysis/stress_resource_summary.csv", "analysis/stress_stage_timing.csv"),
        ),
        "inventory": 来源(
            "inventory_source", "主筛选逐文件哈希清单", "analysis/output_inventory.csv",
            description="24 个主尝试所有非 pyc 输出的格式、大小和 SHA-256。",
            sql="SELECT format, COUNT(*) AS file_count, SUM(size_bytes) AS total_bytes FROM read_csv_auto('analysis/output_inventory.csv') GROUP BY format",
            tables=("analysis/output_inventory.csv",),
        ),
        "lineage": 来源(
            "lineage_source", "候选血缘规范表", "analysis/candidate_lineage.csv",
            description="每主候选八个关键输入/中间/输出角色及 SHA-256。",
            sql="SELECT artifact_role, COUNT(*) AS file_count FROM read_csv_auto('analysis/candidate_lineage.csv') GROUP BY artifact_role",
            tables=("analysis/candidate_lineage.csv",),
        ),
        "input": 来源(
            "input_source", "冻结输入与运行合同", "provenance/enhanced_input_manifest.json",
            description="GLP-1 正靶、旧 12 骨架、模型资产哈希、profile 配置和已知限制。",
            sql="SELECT * FROM read_json_auto('provenance/enhanced_input_manifest.json')",
            tables=("provenance/enhanced_input_manifest.json", "provenance/profiles.json"),
        ),
        "preflight": 来源(
            "preflight_source", "Mac 运行前置检查", preflight_rel,
            description="硬件内存、MPS 可用性、PyTorch 版本、磁盘和本地模型资产哈希检查。",
            sql=f"SELECT * FROM read_json_auto('{preflight_rel}')",
            tables=(preflight_rel,),
        ),
        "validation": 来源(
            "validation_source", "主分析验证报告", "analysis/validation.json",
            description="行数、样本轴、过滤、血缘、框架不变性与源文件未改变检查。",
            sql="SELECT * FROM read_json_auto('analysis/validation.json')",
            tables=("analysis/validation.json",),
        ),
        "deep": 来源(
            "deep_source", "近官方 7XL0 adherence 独立探针", "analysis/deep_probe_summary.json",
            description="4 个深度探针候选和 4 个单样本结果；绝不并入主筛选 48/96。",
            sql="SELECT * FROM read_csv_auto('analysis/deep_probe_candidates.csv')",
            tables=(
                "analysis/deep_probe_summary.json", "analysis/deep_probe_candidates.csv",
                "analysis/deep_probe_folding_samples.csv", "analysis/deep_probe_filter_long.csv",
                "analysis/deep_probe_stage_timing.csv", "analysis/deep_probe_resource_summary.csv",
            ),
        ),
        "runner": 来源(
            "runner_source", "Mac 执行与日志代码", "scripts/run_mac_enhanced.py",
            description="单检查点隔离、逐阶段执行、资源监控、输出合同、内容寻址快照与中断记录。",
        ),
        "analyzer": 来源(
            "analyzer_source", "只读分析代码", "scripts/analyze_mac_enhanced.py",
            description="从 runs/provenance 构建候选、样本、过滤、热点、资源和深度探针规范表。",
        ),
        "boltzgen_release": 来源(
            "boltzgen_release", "BoltzGen v0.3.2 官方发布", href="https://github.com/HannesStark/boltzgen/releases/tag/v0.3.2",
            description="本 campaign 声明的官方版本基线。",
        ),
        "mps_branch": 来源(
            "mps_branch", "BoltzGen Apple Silicon MPS PR #145", href="https://github.com/HannesStark/boltzgen/pull/145",
            description="本次 vendor 快照所依据的未合并实验性 MPS 改动。",
        ),
    }

    # 头部指标只包含不同维度的决策信息，不为固定卡片数量凑数。
    headline = [
        {
            "scaffolds": 主骨架数,
            "main_candidates": 主候选数,
            "folding_samples": 主复折叠样本数,
            "strict_survivors": int(summary["counts"]["strict_filter_survivors"]),
            "best_index_matches": int(summary["counts"]["writer_analysis_best_index_matches"]),
            "best_index_mismatches": 主候选数 - int(summary["counts"]["writer_analysis_best_index_matches"]),
            "deep_candidates": 深度探针候选数,
        }
    ]
    cards = [
        卡片("scaffolds_card", "冻结 VHH 骨架", "scaffolds", sources["input"], unit="条"),
        卡片("candidate_card", "主筛选候选", "main_candidates", sources["summary"], unit="个"),
        卡片("sample_card", "主筛选复折叠样本", "folding_samples", sources["samples"], unit="个"),
        卡片("survivor_card", "十项全通过", "strict_survivors", sources["filters"], unit="个"),
        卡片("best_match_card", "两种最佳样本索引一致", "best_index_matches", sources["samples"], unit="/ 48"),
        卡片("deep_card", "独立近官方探针", "deep_candidates", sources["deep"], unit="个候选"),
    ]

    # 原生图表均由 portable artifact reader 渲染；不嵌入图片或外部图表库。
    charts = [
        {
            "id": "candidate_flow_chart",
            "title": "主筛选候选在过滤与预算展示中的数量",
            "subtitle": "同一候选单位；预算展示项不是过滤通过项。",
            "type": "bar",
            "intent": "comparison",
            "question": "48 个主候选完成计算后，多少通过十项过滤，多少仅进入预算展示？",
            "rationale": "四个离散阶段共享候选单位，顺序条形图能避免把 96 个样本混入候选分母。",
            "dataset": "candidate_flow",
            "source": sources["summary"],
            "labels": {"values": "all"},
            "encodings": {
                "x": {"field": "stage", "type": "nominal", "label": "阶段"},
                "y": {"field": "candidate_count", "type": "quantitative", "label": "候选数"},
            },
        },
        {
            "id": "filter_failure_chart",
            "title": "十项默认计算过滤的失败数量",
            "subtitle": "分母为 48 个主候选；同一候选可以同时失败多项。",
            "type": "horizontalBar",
            "intent": "comparison",
            "question": "哪些冻结过滤条件是本轮最常见失败点？",
            "rationale": "过滤标签较长，横向条形图更易读；失败数量按降序展示。",
            "dataset": "filter_summary",
            "source": sources["filters"],
            "labels": {"values": "all"},
            "encodings": {
                "x": {"field": "filter_label_cn", "type": "nominal", "label": "过滤条件"},
                "y": {"field": "failed_count", "type": "quantitative", "label": "失败候选数"},
            },
        },
        {
            "id": "checkpoint_iptm_chart",
            "title": "两个设计 checkpoint 的中位界面置信代理",
            "subtitle": "每个 checkpoint n=24；仅描述性比较，不是亲和力或成功概率。",
            "type": "bar",
            "intent": "comparison",
            "question": "两个 checkpoint 的主筛选候选在 design-to-target iPTM 中位数上是否有明显描述性差异？",
            "rationale": "只有两个同量纲中位数，简单类别条形图比趋势图更诚实。",
            "dataset": "checkpoint_summary",
            "source": sources["checkpoints"],
            "labels": {"values": "all"},
            "encodings": {
                "x": {"field": "checkpoint_cn", "type": "nominal", "label": "设计 checkpoint"},
                "y": {"field": "median_design_to_target_iptm", "type": "quantitative", "label": "中位 design-to-target iPTM"},
            },
        },
        {
            "id": "scaffold_checkpoint_chart",
            "title": "12 个骨架在两个 checkpoint 下的中位界面置信代理",
            "subtitle": "每个骨架×checkpoint 仅 n=2；用于发现异质性，不用于骨架优劣定论。",
            "type": "bar",
            "intent": "comparison",
            "question": "不同骨架与 checkpoint 组合的描述性中位数如何分布？",
            "rationale": "按 PDB 骨架分组、checkpoint 着色，保留真实 n=2 的比较粒度。",
            "dataset": "scaffold_checkpoint_summary",
            "source": sources["scaffolds"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "设计 checkpoint"},
            "encodings": {
                "x": {"field": "pdb_code", "type": "nominal", "label": "VHH 骨架 PDB"},
                "y": {"field": "median_design_to_target_iptm", "type": "quantitative", "label": "中位 design-to-target iPTM"},
                "color": {"field": "checkpoint_cn", "type": "nominal"},
            },
        },
        {
            "id": "hotspot_distance_chart",
            "title": "复折叠后 His7 与 Ala8 的独立最小重原子距离",
            "subtitle": "48 个主候选；8 Å 虚线只表示局部几何接近。",
            "type": "scatter",
            "intent": "relationship",
            "question": "复折叠后的候选是否同时靠近 GLP-1 的 His7 与 Ala8？",
            "rationale": "二维散点能区分两个提示位点都接近、只接近一个和都不接近。",
            "dataset": "candidates",
            "source": sources["candidates"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "设计 checkpoint"},
            "referenceLines": [
                {"axis": "x", "value": 8.0, "label": "His7 8 Å", "lineStyle": "dashed"},
                {"axis": "y", "value": 8.0, "label": "Ala8 8 Å", "lineStyle": "dashed"},
            ],
            "encodings": {
                "x": {"field": "his7_min_heavy_atom_distance_a", "type": "quantitative", "label": "His7 最小重原子距离（Å）"},
                "y": {"field": "ala8_min_heavy_atom_distance_a", "type": "quantitative", "label": "Ala8 最小重原子距离（Å）"},
                "color": {"field": "checkpoint_cn", "type": "nominal"},
            },
        },
        {
            "id": "best_index_agreement_chart",
            "title": "analysis 与 writer 的最佳复折叠样本索引",
            "subtitle": "每候选两个样本；两套加权公式在 41/48 个候选上选择同一索引。",
            "type": "bar",
            "intent": "comparison",
            "question": "两种合法但不同的样本加权公式有多少次选择同一坐标样本？",
            "rationale": "二分类计数用简单条形图最直接，且保留 48 的分母。",
            "dataset": "best_index_agreement",
            "source": sources["samples"],
            "labels": {"values": "all"},
            "encodings": {
                "x": {"field": "relation", "type": "nominal", "label": "最佳样本索引关系"},
                "y": {"field": "candidate_count", "type": "quantitative", "label": "候选数"},
            },
        },
        {
            "id": "stage_timing_chart",
            "title": "七个执行阶段的累计墙钟时间",
            "subtitle": "每个 checkpoint 12 个骨架；两条支路独立进程运行。",
            "type": "bar",
            "intent": "comparison",
            "question": "Mac 运行时间主要花在哪些阶段？",
            "rationale": "七个阶段共享秒单位，按 checkpoint 分组可比较结构设计、逆折叠和复折叠成本。",
            "dataset": "stage_summary",
            "source": sources["stages"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "设计 checkpoint"},
            "encodings": {
                "x": {"field": "stage_cn", "type": "nominal", "label": "执行阶段"},
                "y": {"field": "total_elapsed_seconds", "type": "quantitative", "label": "累计墙钟秒数"},
                "color": {"field": "checkpoint_cn", "type": "nominal"},
            },
        },
        {
            "id": "resource_rss_chart",
            "title": "各阶段观测到的进程树 RSS 峰值",
            "subtitle": "按阶段×checkpoint 取 12 个骨架最大值；不是独立 MPS 图形内存。",
            "type": "bar",
            "intent": "comparison",
            "question": "单进程分支中哪个阶段的可观测进程树内存最高？",
            "rationale": "同为 GiB 的进程树 RSS 可按阶段和 checkpoint 直接比较；不把系统 free 或 swap 混在同一轴。",
            "dataset": "resource_by_stage",
            "source": sources["resources"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "设计 checkpoint"},
            "encodings": {
                "x": {"field": "stage_cn", "type": "nominal", "label": "执行阶段"},
                "y": {"field": "peak_process_tree_rss_gib", "type": "quantitative", "label": "峰值进程树 RSS（GiB）"},
                "color": {"field": "checkpoint_cn", "type": "nominal"},
            },
        },
    ]

    # 审计表在 artifact 中使用原生 table contract；长序列允许横向滚动而不压缩字体。
    tables = [
        表格(
            "scope_table", "统计域与分母", "主筛选和近官方探针是两个不可合并的统计域。",
            "scope_rows", sources["input"],
            [
                {"field": "object", "label": "对象", "type": "text"},
                {"field": "identity", "label": "身份", "type": "text"},
                {"field": "role", "label": "统计角色", "type": "text"},
                {"field": "count", "label": "数量"},
                {"field": "unit", "label": "单位", "type": "text"},
                {"field": "detail", "label": "边界说明", "type": "text"},
            ], sort_field="object",
        ),
        表格(
            "environment_table", "实际 Mac 环境", "来自成功 preflight 和冻结 manifest；没有在报告构建时重新探测。",
            "environment", sources["preflight"],
            [
                {"field": "item", "label": "项目", "type": "text"},
                {"field": "value", "label": "本次值", "type": "text"},
                {"field": "meaning", "label": "解释", "type": "text"},
            ], sort_field="item", density="spacious",
        ),
        表格(
            "scaffold_registry_table", "旧 12 条 VHH 骨架输入", "所有骨架在运行前已通过既有 BoltzGen 输入检查。",
            "scaffold_registry", sources["input"],
            [
                {"field": "selection_rank", "label": "序号"},
                {"field": "pdb_code", "label": "PDB", "type": "text"},
                {"field": "source_chain", "label": "原链", "type": "text"},
                {"field": "sabdab_id", "label": "SAbDab2 ID", "type": "text"},
                {"field": "role", "label": "冻结角色", "type": "text"},
                {"field": "framework_cluster_id", "label": "框架簇", "type": "text"},
                {"field": "resolution_a", "label": "分辨率 Å"},
                {"field": "r_free", "label": "R-free"},
                {"field": "variable_length_aa", "label": "VHH aa"},
                {"field": "cdr1_length_aa", "label": "CDR1 aa"},
                {"field": "cdr2_length_aa", "label": "CDR2 aa"},
                {"field": "cdr3_length_aa", "label": "CDR3 aa"},
                {"field": "prior_check", "label": "既有检查", "type": "text"},
            ], sort_field="selection_rank",
        ),
        表格(
            "stage_io_table", "模型数据流：每一步输入、变换与输出", "这是预训练推理的数据流，不是本次训练数据管线。",
            "stage_io", sources["input"],
            [
                {"field": "order", "label": "顺序"},
                {"field": "stage", "label": "阶段", "type": "text"},
                {"field": "input", "label": "输入", "type": "text"},
                {"field": "operation", "label": "核心操作", "type": "text"},
                {"field": "output", "label": "输出", "type": "text"},
                {"field": "format", "label": "格式", "type": "text"},
            ], sort_field="order", density="spacious",
        ),
        表格(
            "npz_axes_table", "复折叠 NPZ 数组与坐标轴", "解释数组每一维的含义，并明确本轮没有完整二维 PAE 矩阵。",
            "npz_axes", sources["samples"],
            [
                {"field": "field", "label": "字段", "type": "text"},
                {"field": "shape", "label": "形状", "type": "text"},
                {"field": "row_axis", "label": "行/第一轴", "type": "text"},
                {"field": "column_axis", "label": "列/后续轴", "type": "text"},
                {"field": "meaning", "label": "用途", "type": "text"},
            ], sort_field="field", density="spacious",
        ),
        表格(
            "checkpoint_table", "两个设计 checkpoint 的主筛选汇总", "每支路 12 个骨架、24 个候选、48 个复折叠样本。",
            "checkpoint_summary", sources["checkpoints"],
            [
                {"field": "checkpoint_cn", "label": "checkpoint", "type": "text"},
                {"field": "candidate_count", "label": "候选数"},
                {"field": "folding_sample_count", "label": "复折叠样本"},
                {"field": "filter_survivors", "label": "十项全通过"},
                {"field": "median_design_to_target_iptm", "label": "中位 design→target iPTM"},
                {"field": "median_design_ptm", "label": "中位 design pTM"},
                {"field": "median_filter_rmsd_a", "label": "中位复合物 RMSD Å"},
                {"field": "prerefold_hotspot_positive", "label": "复折叠前热点阳性"},
                {"field": "writer_analysis_best_match", "label": "最佳索引一致"},
                {"field": "writer_analysis_mismatch", "label": "最佳索引不一致"},
            ], sort_field="checkpoint_cn",
        ),
        表格(
            "filter_table", "十项冻结过滤的阈值与结果", "每项分母固定为 48；同一候选可以在多项失败。",
            "filter_summary", sources["filters"],
            [
                {"field": "filter_order", "label": "顺序"},
                {"field": "filter_label_cn", "label": "过滤条件", "type": "text"},
                {"field": "value_column", "label": "原字段", "type": "text"},
                {"field": "threshold_display", "label": "阈值", "type": "text"},
                {"field": "candidate_count", "label": "分母"},
                {"field": "passed_count", "label": "通过"},
                {"field": "failed_count", "label": "失败"},
                {"field": "failure_percent", "label": "失败率 %"},
            ], sort_field="filter_order",
        ),
        表格(
            "candidate_table", "48 个主筛选候选的关键字段", "全部是计算代理；预算展示、过滤通过和最佳样本索引是三个独立字段。",
            "candidates", sources["candidates"],
            [
                {"field": "candidate_label", "label": "候选", "type": "text"},
                {"field": "candidate_id", "label": "候选主键", "type": "text"},
                {"field": "designed_sequence", "label": "三个设计区拼接序列", "type": "text"},
                {"field": "design_to_target_iptm", "label": "design→target iPTM"},
                {"field": "design_ptm", "label": "design pTM"},
                {"field": "min_design_to_target_pae_a", "label": "最小 PAE Å"},
                {"field": "filter_rmsd_a", "label": "复合物 RMSD Å"},
                {"field": "filter_rmsd_design_a", "label": "设计区 RMSD Å"},
                {"field": "prerefold_hotspot_coverage_fraction_lt8a", "label": "复折叠前热点覆盖"},
                {"field": "his7_min_heavy_atom_distance_a", "label": "His7 重原子距 Å"},
                {"field": "ala8_min_heavy_atom_distance_a", "label": "Ala8 重原子距 Å"},
                {"field": "computed_filter_pass_count", "label": "十项通过数"},
                {"field": "failed_filters_cn", "label": "失败项", "type": "text"},
                {"field": "pass_all_default_filters", "label": "十项全通过", "type": "boolean"},
                {"field": "selected_by_budget", "label": "预算展示", "type": "boolean"},
                {"field": "analysis_best_sample_index", "label": "analysis 样本"},
                {"field": "writer_best_sample_index", "label": "writer 样本"},
                {"field": "same_best_sample", "label": "索引一致", "type": "boolean"},
            ], sort_field="candidate_label",
        ),
        表格(
            "mismatch_table", "7 个最佳样本索引不一致的候选", "两套公式权重不同；writer CIF 始终对应 writer 索引。",
            "best_index_mismatches", sources["samples"],
            [
                {"field": "candidate_label", "label": "候选", "type": "text"},
                {"field": "analysis_best_sample_index", "label": "analysis 索引"},
                {"field": "writer_best_sample_index", "label": "writer 索引"},
                {"field": "source_fold_npz", "label": "复折叠 NPZ", "type": "text"},
                {"field": "source_refold_cif", "label": "writer CIF", "type": "text"},
            ], sort_field="candidate_label",
        ),
        表格(
            "probe_comparison_table", "7XL0 adherence：平衡筛选与近官方深度探针对照", "n=2 与 n=4 的单次描述性对照；不能归因于采样步数，也不能合并。",
            "probe_comparison", sources["deep"],
            [
                {"field": "scope", "label": "统计域", "type": "text"},
                {"field": "candidate_count", "label": "候选 n"},
                {"field": "folding_samples_per_candidate", "label": "每候选复折叠样本"},
                {"field": "design_steps", "label": "设计步数"},
                {"field": "inverse_steps", "label": "逆折叠步数"},
                {"field": "folding_steps", "label": "复折叠步数"},
                {"field": "median_design_to_target_iptm", "label": "中位 design→target iPTM"},
                {"field": "median_filter_rmsd_a", "label": "中位复合物 RMSD Å"},
                {"field": "strict_survivors", "label": "十项全通过"},
            ], sort_field="candidate_count",
        ),
        表格(
            "deep_candidate_table", "近官方 7XL0 adherence 探针的 4 个候选", "独立统计域；每候选只有 1 个复折叠样本。",
            "deep_candidates", sources["deep"],
            [
                {"field": "candidate_label", "label": "候选", "type": "text"},
                {"field": "designed_sequence", "label": "三个设计区拼接序列", "type": "text"},
                {"field": "design_to_target_iptm", "label": "design→target iPTM"},
                {"field": "design_ptm", "label": "design pTM"},
                {"field": "filter_rmsd_a", "label": "复合物 RMSD Å"},
                {"field": "filter_rmsd_design_a", "label": "设计区 RMSD Å"},
                {"field": "prerefold_hotspot_coverage_fraction_lt8a", "label": "复折叠前热点覆盖"},
                {"field": "computed_filter_pass_count", "label": "十项通过数"},
                {"field": "failed_filters_cn", "label": "失败项", "type": "text"},
                {"field": "pass_all_default_filters", "label": "十项全通过", "type": "boolean"},
            ], sort_field="candidate_label",
        ),
        表格(
            "stress_table", "双 checkpoint 压力尝试与安全停止", "历史压力尝试不进入主候选统计；中间产物和日志完整保留。",
            "stress_attempts", sources["stress"],
            [
                {"field": "attempt", "label": "尝试", "type": "text"},
                {"field": "status", "label": "记录状态", "type": "text"},
                {"field": "partial_design_cif_count", "label": "已生成设计 CIF"},
                {"field": "elapsed_seconds", "label": "墙钟秒"},
                {"field": "max_swap_stage_delta_gib", "label": "最大阶段 swap 增量 GiB"},
                {"field": "minimum_system_free_gib", "label": "最小 system free GiB"},
                {"field": "interpretation", "label": "解释", "type": "text"},
            ], sort_field="attempt",
        ),
        表格(
            "resource_table", "主筛选资源护栏摘要", "每行是阶段×checkpoint 的 12 骨架包络；无独立 MPS 内存。",
            "resource_by_stage", sources["resources"],
            [
                {"field": "stage_cn", "label": "阶段", "type": "text"},
                {"field": "checkpoint_cn", "label": "checkpoint", "type": "text"},
                {"field": "peak_process_tree_rss_gib", "label": "进程树 RSS 峰值 GiB"},
                {"field": "max_swap_stage_delta_gib", "label": "最大阶段 swap 增量 GiB"},
                {"field": "minimum_system_free_gib", "label": "最小 system free GiB"},
                {"field": "minimum_disk_free_gib", "label": "最小磁盘可用 GiB"},
            ], sort_field="stage_cn",
        ),
        表格(
            "log_table", "日志、中间状态与血缘文件", "每个阶段都有独立标准输出、标准错误、资源采样、状态和文件变化清单。",
            "log_artifacts", sources["inventory"],
            [
                {"field": "order", "label": "顺序"},
                {"field": "artifact", "label": "文件/目录", "type": "text"},
                {"field": "grain", "label": "粒度", "type": "text"},
                {"field": "meaning", "label": "内容与用途", "type": "text"},
            ], sort_field="order", density="spacious",
        ),
        表格(
            "lineage_table", "48 个主候选的八角色血缘", "每个角色应有 48 项；逐项路径、大小和哈希在 candidate_lineage.csv。",
            "lineage_summary", sources["lineage"],
            [
                {"field": "artifact_role", "label": "血缘角色", "type": "text"},
                {"field": "file_count", "label": "条目数"},
                {"field": "meaning", "label": "用途", "type": "text"},
            ], sort_field="artifact_role",
        ),
        表格(
            "output_format_table", "主筛选输出格式与体量", "正文按格式聚合；逐文件 SHA-256 仍在完整清单中。",
            "output_formats", sources["inventory"],
            [
                {"field": "format", "label": "格式", "type": "text"},
                {"field": "file_count", "label": "文件数"},
                {"field": "total_mib", "label": "合计 MiB"},
                {"field": "total_bytes", "label": "精确字节"},
            ], sort_field="total_bytes", sort_direction="desc",
        ),
        表格(
            "validation_table", "规范分析的逐项验证", "所有检查必须为真，报告才允许封装。",
            "validation_checks", sources["validation"],
            [
                {"field": "check", "label": "检查", "type": "text"},
                {"field": "passed", "label": "通过", "type": "boolean"},
                {"field": "interpretation", "label": "解释", "type": "text"},
            ], sort_field="check",
        ),
    ]

    # 纯 HTML block 只用于流程示意；图表仍全部走规范原生 chart contract。
    flow_html = """
<style>
  .mac-flow{font:16px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:inherit}
  .mac-flow h2{margin:0 0 8px}.mac-flow p{margin:0 0 14px;color:color-mix(in srgb,currentColor 70%,transparent)}
  .mac-flow-grid{display:grid;grid-template-columns:repeat(6,minmax(150px,1fr));gap:10px;align-items:stretch;overflow-x:auto;padding:4px 2px 8px}
  .mac-flow-step{min-width:150px;padding:14px;border:1px solid color-mix(in srgb,#277da1 42%,transparent);border-radius:14px;background:color-mix(in srgb,#277da1 9%,transparent);position:relative}
  .mac-flow-step:not(:last-child)::after{content:"→";position:absolute;right:-10px;top:42%;z-index:2;font-weight:800;color:#277da1;background:Canvas;padding:0 2px}
  .mac-flow-step b{display:block;margin-bottom:5px;color:#12304a}.mac-flow-step small{display:block;margin-top:6px;opacity:.78}
  .mac-flow-output{border-color:color-mix(in srgb,#e69f00 48%,transparent);background:color-mix(in srgb,#e69f00 10%,transparent)}
  @media(prefers-color-scheme:dark){.mac-flow-step b{color:#dcecf6}.mac-flow-step:not(:last-child)::after{background:Canvas}}
  @media(max-width:760px){.mac-flow-grid{grid-template-columns:1fr;overflow:visible}.mac-flow-step{min-width:0}.mac-flow-step:not(:last-child)::after{content:"↓";right:auto;left:50%;top:auto;bottom:-16px}}
</style>
<section class="mac-flow" aria-labelledby="mac-flow-title">
  <h2 id="mac-flow-title">从输入到候选：实际推理链路</h2>
  <p>固定框架只提供起点；三个互补决定区的几何与序列由预训练模型逐步采样，再用复折叠和十项过滤检查。此链路没有更新模型权重。</p>
  <div class="mac-flow-grid" role="list">
    <div class="mac-flow-step" role="listitem"><b>1 · 条件输入</b>旧 VHH 骨架、GLP-1(7–36) 坐标、His7/Ala8 提示、三个可设计区<small>CIF + YAML</small></div>
    <div class="mac-flow-step" role="listitem"><b>2 · 结构扩散设计</b>从噪声复合物几何逐步去噪，产生满足条件的候选主链<small>design checkpoint</small></div>
    <div class="mac-flow-step" role="listitem"><b>3 · 逆折叠</b>在候选几何上采样三个设计区的氨基酸，固定框架保持不变<small>inverse-fold checkpoint</small></div>
    <div class="mac-flow-step" role="listitem"><b>4 · 复折叠</b>把完整 VHH 序列和 GLP-1 重新预测成复合物；主筛选每候选 2 个样本<small>folding checkpoint</small></div>
    <div class="mac-flow-step" role="listitem"><b>5 · 分析与过滤</b>结构自洽、热点几何、序列规则和界面置信代理；逐项应用 10 个阈值<small>48 × 10 记录</small></div>
    <div class="mac-flow-step mac-flow-output" role="listitem"><b>6 · 可审计输出</b>候选表、CIF/NPZ、阶段日志、资源采样、哈希血缘与预算展示目录<small>不是实验命中</small></div>
  </div>
</section>
"""

    # 报告正文遵循“结论 → 证据 → 定义/方法 → 限制 → 下一步”的技术阅读路径。
    failure_leaders = sorted(filter_summary, key=lambda row: (-row["failed_count"], row["filter_order"]))[:3]
    failure_text = "；".join(
        f"{row['filter_label_cn']} {int(row['failed_count'])}/48"
        for row in failure_leaders
    )
    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {报告标题}"},
        {
            "id": "technical_summary",
            "type": "markdown",
            "sourceId": "summary_source",
            "body": (
                "## 技术摘要\n\n"
                "**Mac 上的完整预训练推理链路已跑通：24/24 个单 checkpoint 主尝试完成，"
                "得到 48 个候选和 96 个真实复折叠样本，但 0/48 通过全部十项冻结计算过滤。** "
                "工程可运行与候选质量通过是两个不同结论。双 checkpoint 同进程的压力尝试因 swap "
                "快速增加而被安全停止，随后拆成 diverse 与 adherence 两个独立进程支路；中间产物、"
                "阶段日志和资源采样均保留。\n\n"
                "这次执行是固定预训练权重的候选生成，不是训练，也没有设计新的训练损失函数。所有"
                "界面置信、预测对齐误差、均方根偏差和热点距离都是计算代理：不能换算成解离常数，"
                "不能声称亲和力，也不能在没有 GLP-1(9–36)、反靶和多构象反筛时声称选择性。"
            ),
        },
        {"id": "headline_metrics", "type": "metric-strip", "cardIds": [card["id"] for card in cards]},
        {
            "id": "key_findings",
            "type": "markdown",
            "sourceId": "filter_source",
            "body": (
                "## 关键证据指向结构自洽与热点覆盖，而不是序列组成\n\n"
                f"- **0/48 严格通过。**领先失败项为：{failure_text}。同一候选可同时失败多项，所以这些失败数不能相加成独立淘汰人数。\n"
                "- **两个 checkpoint 都没有严格通过者。**它们的 24 条候选只支持描述性比较；每骨架×checkpoint 只有 n=2，不能估计命中率或框架优劣。\n"
                "- **最佳复折叠样本并非总是一致。**analysis 公式与 writer 公式在 41/48 个候选选中相同索引，另 7 个不同；writer 输出 CIF 始终来自 writer 公式选择的样本。\n"
                "- **近官方 7XL0 adherence 探针仍为 0/4 严格通过。**它使用更深步数但每候选仅 1 个复折叠样本，和主筛选 n=2 的批次不同，因此只能独立描述，不能据此归因。"
            ),
        },
        {"id": "candidate_flow_block", "type": "chart", "chartId": "candidate_flow_chart"},
        {
            "id": "candidate_flow_note", "type": "markdown", "sourceId": "summary_source",
            "body": "**如何读图：**48 个候选全部完成生成与分析；0 个通过十项门槛。24 个预算展示项只是两个 checkpoint 各为 12 个骨架保留 1 个审阅入口，不是 24 个成功候选。",
        },
        {"id": "filter_chart_block", "type": "chart", "chartId": "filter_failure_chart"},
        {
            "id": "filter_chart_note", "type": "markdown", "sourceId": "filter_source",
            "body": "**解释：**复合物骨架均方根偏差衡量生成几何与候选序列复折叠几何之间的自洽性，并不是候选相对未知实验真值的误差。序列组成过滤大多通过，并不能抵消结构自洽或提示位点覆盖失败。",
        },
        {"id": "scope_heading", "type": "markdown", "body": "## 输入、统计域与实际 Mac 环境\n\n先固定分母，再解释结果。主筛选、压力尝试和近官方探针有不同目的，不能拼成一个成功率。"},
        {"id": "scope_table_block", "type": "table", "tableId": "scope_table", "layout": "full"},
        {"id": "environment_table_block", "type": "table", "tableId": "environment_table", "layout": "full"},
        {
            "id": "environment_note", "type": "markdown", "sourceId": "preflight_source",
            "body": "**MPS 与 CPU fallback：**Apple Metal Performance Shaders（MPS）为 Apple 图形处理器后端；`PYTORCH_ENABLE_MPS_FALLBACK=1` 允许不受 MPS 支持的算子回退到中央处理器。这个实验性分支能运行，不代表与官方 Linux + NVIDIA CUDA 的数值、速度或稳定性等价。",
        },
        {"id": "scaffold_registry_block", "type": "table", "tableId": "scaffold_registry_table", "layout": "full"},
        {"id": "flow_block", "type": "html", "body": flow_html},
        {
            "id": "algorithm_principle",
            "type": "markdown",
            "sourceId": "input_source",
            "body": (
                "## 算法原理与本次推理公式\n\n"
                "**变量定义。**VHH 指骆驼科重链抗体的单个可变结构域；GLP-1 指胰高血糖素样肽-1。"
                "扩散设计在每个去噪步将带噪结构 `x_t`、条件 `c`（骨架、目标、设计掩码和提示位点）"
                "送入预训练网络：\n\n"
                "`x_(t-1) = μ_θ(x_t, c, t) + σ_t · ε`\n\n"
                "其中 `μ_θ` 由已训练权重给出，`ε` 是采样噪声。逆折叠随后按候选几何采样可设计区序列：\n\n"
                "`s* ~ p_θ(s | x_0, design_mask)`\n\n"
                "复折叠模型再用 `s*` 与 GLP-1 重新预测结构 `x̂_j`。这一步不是把生成结构原样抄回，"
                "而是检验序列是否支持相近几何。对齐后的均方根偏差（RMSD）为：\n\n"
                "`RMSD = sqrt[(1/N) · Σ_i ||R·x_i + t − x̂_i||²]`\n\n"
                "`R` 与 `t` 是最佳刚体对齐，`N` 是比较的骨架原子数。本轮没有通过反向传播更新 `θ`，"
                "所以这些公式描述采样与评价，不是本次训练损失。"
            ),
        },
        {"id": "stage_io_block", "type": "table", "tableId": "stage_io_table", "layout": "full"},
        {"id": "npz_axes_block", "type": "table", "tableId": "npz_axes_table", "layout": "full"},
        {
            "id": "checkpoint_heading", "type": "markdown",
            "body": "## 两个设计 checkpoint 提供互补采样，但本轮都没有严格通过者\n\n多样性检查点和骨架遵循检查点使用相同骨架、目标、逆折叠、复折叠与过滤配置，仅结构设计权重不同。比较只反映本次 24 对 24 个候选。",
        },
        {"id": "checkpoint_chart_block", "type": "chart", "chartId": "checkpoint_iptm_chart"},
        {
            "id": "checkpoint_chart_note", "type": "markdown", "sourceId": "checkpoint_source",
            "body": "**如何读图：**design-to-target interface predicted template modeling score（设计区到目标界面预测模板建模分数，iPTM）是 0–1 的模型置信代理；数值较高不等于更强实验结合，更不能换算为解离常数。",
        },
        {"id": "checkpoint_table_block", "type": "table", "tableId": "checkpoint_table", "layout": "full"},
        {"id": "scaffold_chart_block", "type": "chart", "chartId": "scaffold_checkpoint_chart"},
        {
            "id": "scaffold_chart_note", "type": "markdown", "sourceId": "scaffold_checkpoint_source",
            "body": "**限制：**每格仅 2 条候选。可用来选择要人工复核的骨架×checkpoint 组合，不能把中位数差异解释为框架的稳定优势。",
        },
        {
            "id": "evaluation_heading", "type": "markdown",
            "body": "## 十项过滤、热点语义与最佳样本选择\n\n评价分三层：复折叠前过滤字段、复折叠后独立几何、以及两个复折叠样本的选择公式。三层字段相关但不可互换。",
        },
        {"id": "filter_table_block", "type": "table", "tableId": "filter_table", "layout": "full"},
        {"id": "hotspot_chart_block", "type": "chart", "chartId": "hotspot_distance_chart"},
        {
            "id": "hotspot_semantics",
            "type": "markdown",
            "sourceId": "candidate_source",
            "body": (
                "**复折叠前与复折叠后必须分开。**BoltzGen 的 `bindsite_under_8rmsd` 是在复折叠前结构上"
                "计算 His7/Ala8 两个提示 token-center 的覆盖比例，只可能是 0、0.5 或 1；它不是接触数、"
                "不是 RMSD、也不是原子距离。报告另从 writer 输出的复折叠后 CIF 独立重算 His7/Ala8 到"
                "VHH 设计残基的最小重原子与 Cα 距离。复折叠后重原子覆盖的描述式为：\n\n"
                "`coverage = [I(d(His7,D)<8 Å) + I(d(Ala8,D)<8 Å)] / 2`\n\n"
                "其中 `D` 是三个可设计区域中的重原子集合。距离小于 8 Å 只表示局部几何接近，不证明"
                "形成特定化学相互作用，也不证明实验结合。"
            ),
        },
        {"id": "best_index_chart_block", "type": "chart", "chartId": "best_index_agreement_chart"},
        {
            "id": "best_index_formula",
            "type": "markdown",
            "sourceId": "sample_source",
            "body": (
                "**两套最佳样本公式。**analysis 选择：\n\n"
                "`j_analysis = argmax_j [0.8·iPTM_(design→target,j) + 0.2·pTM_(design,j)]`\n\n"
                "writer 选择并写入 CIF：\n\n"
                "`j_writer = argmax_j [0.8·iPTM_(complex,j) + 0.2·pTM_(complex,j)]`\n\n"
                "41/48 一致，7/48 不一致。两者回答的问题不同；不能用 analysis 索引的标量配 writer 索引"
                "的坐标。坐标血缘检查确认每个 writer CIF 与其 writer 样本的最大绝对坐标误差处于浮点写出误差范围。"
            ),
        },
        {"id": "mismatch_table_block", "type": "table", "tableId": "mismatch_table", "layout": "full"},
        {"id": "candidate_table_block", "type": "table", "tableId": "candidate_table", "layout": "full"},
        {
            "id": "deep_probe_heading",
            "type": "markdown",
            "sourceId": "deep_source",
            "body": (
                "## 近官方 7XL0 adherence 探针没有改变严格结论\n\n"
                "探针将结构设计从 100 提高到 500 步、逆折叠从 60 提高到 200 步、复折叠从 100 提高到"
                "200 步，并将 recycling 从 2 提高到 3；为控制 Mac 内存，只使用 adherence 单 checkpoint、"
                "4 个候选且每候选 1 个复折叠样本。4/4 完成，0/4 十项全通过。"
            ),
        },
        {"id": "probe_comparison_block", "type": "table", "tableId": "probe_comparison_table", "layout": "full"},
        {
            "id": "probe_comparison_note", "type": "markdown", "sourceId": "deep_source",
            "body": "**不要作因果解释：**浅层 n=2、深层 n=4，且每候选复折叠样本数分别为 2 与 1；随机采样也没有统一全管线种子。中位 iPTM 与均方根偏差的变化只能描述，不能归因于步数，更不能据此声称模型改进。",
        },
        {"id": "deep_candidate_block", "type": "table", "tableId": "deep_candidate_table", "layout": "full"},
        {
            "id": "resource_heading", "type": "markdown",
            "body": "## 压力中断、资源护栏与逐阶段日志使 Mac 尝试可复盘\n\n最初双 checkpoint 同进程方案会在结构设计阶段切换两个约 1.8 GiB 的设计权重；监控观察到 swap 快速上升和系统 free 极低后主动安全停止。随后每个进程只加载一个设计 checkpoint，24 个主尝试全部完成。",
        },
        {"id": "stress_table_block", "type": "table", "tableId": "stress_table", "layout": "full"},
        {"id": "stage_chart_block", "type": "chart", "chartId": "stage_timing_chart"},
        {
            "id": "stage_chart_note", "type": "markdown", "sourceId": "stage_source",
            "body": "**如何读图：**这是 12 个骨架在同一 checkpoint 支路的阶段墙钟累计。结构扩散设计和复折叠通常占主要时间；check/configure/analysis/filtering 仍作为独立进程保留，方便定位异常。",
        },
        {"id": "rss_chart_block", "type": "chart", "chartId": "resource_rss_chart"},
        {
            "id": "rss_chart_note", "type": "markdown", "sourceId": "resource_source",
            "body": "**资源口径：**进程树 RSS 只覆盖可由操作系统归到进程树的驻留内存；Apple 统一内存中的 MPS 占用没有独立测量。因此图适合比较阶段包络，不适合作为“图形处理器显存峰值”。swap 的绝对水位受前序系统状态影响，报告同时保留阶段首末增量。",
        },
        {"id": "resource_table_block", "type": "table", "tableId": "resource_table", "layout": "full"},
        {"id": "log_table_block", "type": "table", "tableId": "log_table", "layout": "full"},
        {"id": "lineage_table_block", "type": "table", "tableId": "lineage_table", "layout": "full"},
        {"id": "output_format_block", "type": "table", "tableId": "output_format_table", "layout": "full"},
        {
            "id": "validation_heading", "type": "markdown", "sourceId": "validation_source",
            "body": "## 规范验证全部通过，但科学结论仍受实验设计限制\n\n验证证明的是文件与计算合同：48/96/480 行数、每候选两个样本、writer 坐标血缘、十项过滤复算、设计掩码外框架不变、预算与过滤分离，以及分析未改写输入/运行源文件。它不把计算代理提升为实验真值。",
        },
        {"id": "validation_table_block", "type": "table", "tableId": "validation_table", "layout": "full"},
        {
            "id": "reproduction",
            "type": "markdown",
            "sourceId": "runner_source",
            "body": (
                "## 代码、命令与复现边界\n\n"
                "所有执行代码均在 `scripts/`，运行记录在 `runs/`，规范派生表在 `analysis/`。下列命令是"
                "从冻结输入重放各统计域的入口；再次运行会创建新的 `attempt_NNN`，不会覆盖既有尝试：\n\n"
                "```bash\n"
                "python scripts/run_mac_enhanced.py --profile balanced_diverse_all12 --start-rank 1 --end-rank 12 --stop-on-error\n"
                "python scripts/run_mac_enhanced.py --profile balanced_adherence_all12 --start-rank 1 --end-rank 12 --stop-on-error\n"
                "python scripts/run_mac_enhanced.py --profile near_official_adherence_7xl0 --start-rank 1 --end-rank 1 --stop-on-error\n"
                "python scripts/analyze_mac_enhanced.py\n"
                "python scripts/build_notebook.py\n"
                "python scripts/build_report_artifact.py\n"
                "node <DATA_ANALYTICS_PLUGIN_ROOT>/skills/build-report/scripts/deliver_portable_artifact.mjs \\\n"
                "  --input report/report_artifact.json \\\n"
                "  --output BoltzGen_Mac_旧12骨架_增强筛选与复盘.html\n"
                "```\n\n"
                "冻结模型资产、输入、配置和每阶段输出均有 SHA-256。BoltzGen 命令行没有为整条管线暴露"
                "统一随机种子，所以重放应验证合同、分布与失败模式，不承诺逐字节生成相同序列。"
            ),
        },
        {
            "id": "limitations",
            "type": "markdown",
            "sourceId": "validation_source",
            "body": (
                "## 限制、不确定性与禁止外推\n\n"
                "- **不是训练。**没有新训练集、梯度、损失优化或权重更新；这是固定预训练权重推理。\n"
                "- **只有单一正靶。**仅使用 PDB 6X18 的 GLP-1(7–36) 受体结合态几何，没有 GLP-1(9–36)、其他反靶或多构象反筛，不能声称选择性。\n"
                "- **末端化学未闭环。**目标 C 端酰胺没有完成原子级确认。\n"
                "- **计算代理不是实验量。**iPTM、pTM、PAE、RMSD、ΔSASA、氢键/盐桥计数和热点距离不能换算为解离常数、亲和力或实验成功率。\n"
                "- **硬件与代码分支受限。**未合并 MPS 快照、CPU fallback、FP32 和 Apple 统一内存不等同于官方 Linux + NVIDIA CUDA 基线。\n"
                "- **样本仍小。**主筛选每骨架×checkpoint 只有 2 个候选；深度探针只有 4 个候选且每候选 1 个复折叠样本。\n"
                "- **最佳样本公式不同。**7 个候选的 analysis 与 writer 索引不同，比较指标时必须与对应坐标样本配对。\n"
                "- **没有完整 PAE 矩阵。**当前 fold NPZ 只保存汇总标量，不能补画或推断二维 PAE 热图。"
            ),
        },
        {
            "id": "retrospective",
            "type": "markdown",
            "body": (
                "## 复盘：Mac 适合验证工程链路，不适合把低样本批次包装成命中\n\n"
                "**本轮做成了什么：**两条单 checkpoint 支路在 Mac 上稳定完成，真实保存每候选两个复折叠"
                "样本，逐阶段日志和资源监控可定位压力点，候选可沿八个文件角色追溯，十项过滤由独立"
                "分析再次复算。近官方 7XL0 单 checkpoint 探针也完整结束。\n\n"
                "**本轮没有做成什么：**没有出现十项全通过候选；没有实验结合、表达、热稳定性、非特异性"
                "或捕获性能数据；没有选择性设计。放宽阈值或把预算目录当成功目录都会扭曲结论。\n\n"
                "**工程决策：**Mac 路线保留作可复现小批量筛查和调试。更大候选量、重复批次、多反靶与"
                "多构象计算应转到官方支持的 Linux + NVIDIA 环境，并继续沿用单 checkpoint 进程隔离、"
                "输出合同和资源护栏。"
            ),
        },
        {
            "id": "next_steps",
            "type": "markdown",
            "body": (
                "## 下一步\n\n"
                "1. 在 Linux + NVIDIA 环境用同一冻结输入复现两个 checkpoint，并扩大每骨架候选数和独立随机重复。\n"
                "2. 保持十项门槛不变，优先增加每候选复折叠样本，报告 analysis/writer 两套选样以及坐标配对。\n"
                "3. 原子级确认 GLP-1(7–36) C 端酰胺，再建立 GLP-1(9–36)、多构象和其他反靶集合。\n"
                "4. 只有计算门槛与人工结构检查都合格的候选才进入表达、稳定性和非特异性评估。\n"
                "5. 实验阶段再用表面等离子体共振或生物层干涉法测量真实结合，并以捕获液相色谱–质谱建立任务标签；不要用计算代理填充实验值。"
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## 进一步问题\n\n"
                "- 在更多候选和独立重复中，复合物 RMSD 与提示位点覆盖失败是否仍是主要瓶颈？\n"
                "- 7 个最佳样本索引不一致候选在增加复折叠样本后是否更不稳定？\n"
                "- 不同骨架的设计区长度与复折叠自洽性是否存在可重复关系？\n"
                "- 加入 GLP-1(9–36) 和多构象反筛后，哪些候选仍保留正靶结构优势？\n"
                "- 哪些计算代理与后续表达、稳定性、捕获回收率和实验结合数据真正相关？"
            ),
        },
    ]

    # Snapshot 只保存经过审阅且有明确分析粒度的数据集；不嵌入 3,264 行逐文件清单。
    snapshot = {
        "version": 1,
        "generatedAt": 当前世界时(),
        "status": "ready",
        "datasets": {
            "headline": headline,
            "candidate_flow": candidate_flow,
            "filter_summary": filter_summary,
            "checkpoint_summary": checkpoints,
            "scaffold_checkpoint_summary": scaffold_checkpoints,
            "candidates": candidates,
            "best_index_mismatches": [row for row in candidates if not row["same_best_sample"]],
            "hotspot_stage": hotspot_stage,
            "best_index_agreement": best_index_agreement,
            "stage_summary": stage_summary,
            "resource_by_stage": resource_by_stage,
            "scope_rows": scope_rows,
            "environment": environment,
            "scaffold_registry": scaffold_registry,
            "stage_io": stage_io,
            "npz_axes": npz_axes,
            "stress_attempts": stress_attempts,
            "probe_comparison": probe_comparison,
            "deep_candidates": deep_candidates,
            "deep_resource_table": deep_resource_table,
            "log_artifacts": log_artifacts,
            "lineage_summary": lineage_summary,
            "output_formats": output_formats,
            "validation_checks": validation_checks,
        },
        "accessIssues": [],
    }

    generated_at = 当前世界时()
    manifest = {
        "version": 1,
        "surface": "report",
        "title": 报告标题,
        "description": "Mac MPS+CPU fallback 上旧 12 条 VHH 骨架的双 checkpoint 增强筛选、独立近官方探针、过滤、资源与日志复盘。",
        "generatedAt": generated_at,
        "cards": cards,
        "charts": charts,
        "tables": tables,
        "sources": list(sources.values()),
        "blocks": blocks,
    }
    artifact = {
        "surface": "report",
        "manifest": manifest,
        "snapshot": snapshot,
        "sources": list(sources.values()),
        "package_info": {
            "mode": "portable_html",
            "controls": {
                "edit": False,
                "refresh": False,
                "persistence": False,
                "copyAsImage": False,
            },
        },
    }

    # 构建注记保存受众结构映射、图表合同和未纳入可视 snapshot 的原因。
    notes = {
        "schema_version": "1.0.0",
        "generated_at_utc": generated_at,
        "audience": "technical",
        "delivery_mode": "html",
        "required_structure_mapping": {
            "Title": "title",
            "Technical summary": "technical_summary",
            "Key findings with visual evidence": ["key_findings", "candidate_flow_chart", "filter_failure_chart"],
            "Scope, data, and metric definitions": ["scope_table", "environment_table", "scaffold_registry_table"],
            "Methodology and model specification": ["flow_block", "algorithm_principle", "stage_io_table", "npz_axes_table"],
            "Limitations, uncertainty, robustness": ["validation_heading", "limitations", "retrospective"],
            "Recommended next steps": "next_steps",
            "Further questions": "further_questions",
        },
        "chart_map": [
            {
                "chart_id": chart["id"],
                "section": next(
                    (block["id"] for block in blocks if block.get("chartId") == chart["id"]),
                    "unknown",
                ),
                "question": chart["question"],
                "type": chart["type"],
                "dataset": chart["dataset"],
                "claim": chart["subtitle"],
                "palette_policy": "single-root preferred" if "color" not in chart.get("encodings", {}) else "hard two-root cap",
            }
            for chart in charts
        ],
        "snapshot_omissions": [
            "analysis/output_inventory.csv 的 3,264 行逐文件清单只按格式聚合进入 snapshot；完整路径和 SHA-256 保留在 source 文件。",
            "analysis/filter_long.csv 的 480 行逐项记录不重复嵌入正文；十项汇总和 48 候选失败项进入 snapshot，完整长表保留在 source 文件。",
            "analysis/resource_samples.csv 的高频时间序列不嵌入；阶段包络进入 snapshot，原始约 2 秒采样留在 source 文件。",
            "复折叠 NPZ 没有完整二维 PAE 矩阵，因此没有绘制 PAE 热图。",
        ],
        "scientific_claim_boundary": "预训练推理与候选生成；禁止解离常数、亲和力、实验成功率或选择性声称。",
        "source_artifact_sha256": {
            "analysis/run_summary.json": 文件哈希(分析目录 / "run_summary.json"),
            "analysis/validation.json": 文件哈希(分析目录 / "validation.json"),
            "analysis/deep_probe_summary.json": 文件哈希(分析目录 / "deep_probe_summary.json"),
            "provenance/enhanced_input_manifest.json": 文件哈希(溯源目录 / "enhanced_input_manifest.json"),
        },
    }
    return artifact, notes


def main() -> int:
    """命令行入口：构建规范 artifact 与构建注记。"""

    parser = argparse.ArgumentParser(description="构建 BoltzGen Mac 增强筛选技术报告 artifact")
    parser.add_argument(
        "--output",
        type=Path,
        default=报告目录 / "report_artifact.json",
        help="规范 artifact JSON；必须位于 report/",
    )
    parser.add_argument(
        "--notes-output",
        type=Path,
        default=报告目录 / "report_build_notes.json",
        help="受众结构、图表合同和 snapshot 省略说明；必须位于 report/",
    )
    args = parser.parse_args()
    artifact, notes = 构建报告()
    写JSON(args.output, artifact)
    写JSON(args.notes_output, notes)
    print(
        json.dumps(
            {
                "status": "REPORT_ARTIFACT_READY",
                "artifact": 相对路径(args.output),
                "artifact_sha256": 文件哈希(args.output),
                "notes": 相对路径(args.notes_output),
                "title": 报告标题,
                "main_population": {"candidates": 主候选数, "folding_samples": 主复折叠样本数},
                "deep_probe_population": {"candidates": 深度探针候选数, "folding_samples": 深度探针样本数},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
