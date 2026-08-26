#!/usr/bin/env python3
"""从第一轮 BoltzGen 规范分析表构建 canonical 技术报告 artifact。

本脚本是一个“失败即停止”的报告生成器，而不是模型运行脚本。它只读取：

1. ``provenance/input_manifest.json`` 中冻结的输入、运行设置与已知边界；
2. ``analysis/`` 中由 ``scripts/analyze_round1.py`` 生成并验证过的规范表；
3. ``analysis/validation_report.json`` 与 ``analysis/run_summary.json`` 中的审计结论。

只有分析产物齐全、12 个骨架任务全部完成、24 个原始候选链路完整、规范表
互相一致，而且 validation assessment 为
``READY_TO_SHARE_WITH_SCIENTIFIC_CAVEATS`` 时，脚本才会原子写入
``report/report_artifact.json``。因此，在模型运行或分析尚未完成时执行本脚本，
只会得到明确错误，不会生成带有占位数值或伪结果的报告。

报告中的“通过”只指通过本轮十项默认计算过滤；``selected_by_budget`` 始终被解释
为预算目录展示项，不等于通过。任何结构置信度、界面几何、距离或面积指标都不会
被表述为实验解离常数、实验亲和力、真实结合概率或型态选择性。
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


# 本脚本位于 RUN_ROOT/scripts/；所有路径都从脚本位置推导，避免依赖当前工作目录。
RUN_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_ROOT = RUN_ROOT / "analysis"
PROVENANCE_ROOT = RUN_ROOT / "provenance"
REPORT_ROOT = RUN_ROOT / "report"
OUTPUT_PATH = REPORT_ROOT / "report_artifact.json"

# 只有这个评估值允许生成“可分享但必须保留科学限制”的报告。
READY_ASSESSMENT = "READY_TO_SHARE_WITH_SCIENTIFIC_CAVEATS"

# 本轮冻结的结构门槛。它们是计算过滤阈值，不是实验成功阈值。
RMSD_THRESHOLD_A = 2.5
HOTSPOT_DISTANCE_THRESHOLD_A = 8.0

# 五个模型步骤按真实执行逻辑排序；CSV 的物理顺序不参与语义排序。
MODEL_STAGE_ORDER = {
    "design": 1,
    "inverse_folding": 2,
    "folding": 3,
    "analysis": 4,
    "filtering": 5,
}


# 报告使用的规范表。把所有表集中列出，可以在构建前一次性阻断缺失产物。
REQUIRED_ANALYSIS_FILES = (
    "raw_all_designs_metrics.csv",
    "candidate_metrics.csv",
    "interface_contacts_independent.csv",
    "candidate_filter_results.csv",
    "filter_summary.csv",
    "candidate_lineage.csv",
    "run_manifest.csv",
    "stage_timings.csv",
    "per_scaffold_summary.csv",
    "sequence_pairs.csv",
    "process_funnel.csv",
    "output_inventory.tsv",
    "npz_schema.json",
    "resource_summary.json",
    "validation_report.json",
    "run_summary.json",
)


# 每个 SQL 来源会显式 SELECT 这些真实列。构建器先核对表头，再把 SQL 写进来源弹窗，
# 从机制上避免报告查询引用不存在的列或旧版别名。
QUERY_COLUMNS: dict[str, tuple[str, ...]] = {
    "candidate_metrics.csv": (
        "candidate_id",
        "candidate_label",
        "local_candidate_index",
        "scaffold_selection_rank",
        "scaffold_id",
        "scaffold_pdb_code",
        "scaffold_role",
        "scaffold_resolution_a",
        "scaffold_r_free",
        "design_residue_count",
        "cdr1_length_aa",
        "cdr2_length_aa",
        "cdr3_length_aa",
        "cdr1_sequence",
        "cdr2_sequence",
        "cdr3_sequence",
        "designed_sequence",
        "designed_chain_sequence",
        "framework_sequence_unchanged",
        "filter_rmsd_a",
        "filter_rmsd_design_a",
        "bb_target_aligned_rmsd_design_a",
        "design_to_target_iptm",
        "design_ptm",
        "design_ipsae_min",
        "min_design_to_target_pae_a",
        "complex_plddt",
        "complex_iplddt",
        "prerefold_hotspot_coverage_fraction_lt8a",
        "independent_hotspot_coverage_heavy_lt8a",
        "independent_hotspot_coverage_ca_lt8a",
        "his7_min_heavy_atom_distance_a",
        "ala8_min_heavy_atom_distance_a",
        "target_delta_sasa_refolded_a2",
        "geometric_hbond_count_refolded",
        "charged_atom_pair_count_refolded",
        "liability_score",
        "liability_num_violations",
        "liability_high_severity_violations",
        "liability_summary",
        "computed_filter_pass_count",
        "computed_filter_total",
        "failed_filter_count",
        "failed_filters_cn",
        "pass_all_default_filters",
        "selected_by_budget",
        "boltzgen_internal_prefix_pass_score",
        "final_rank_within_scaffold",
        "quality_score_within_scaffold_only",
        "fold_sample_count",
        "analysis_best_sample_index",
        "writer_best_sample_index",
        "same_best_sample",
        "source_metrics_csv",
        "source_refold_cif",
    ),
    "filter_summary.csv": (
        "filter_order",
        "filter_label_cn",
        "pass_column",
        "value_column",
        "operator",
        "threshold",
        "unit",
        "candidate_count",
        "passed_count",
        "failed_count",
        "failure_rate",
    ),
    "candidate_filter_results.csv": (
        "candidate_id",
        "scaffold_id",
        "pdb_code",
        "filter_order",
        "filter_label_cn",
        "pass_column",
        "value_column",
        "observed_value",
        "operator",
        "threshold",
        "unit",
        "passed",
    ),
    "process_funnel.csv": (
        "order",
        "stage_key",
        "stage_label_cn",
        "count",
        "unit",
        "fraction_of_requested_candidates",
    ),
    "per_scaffold_summary.csv": (
        "scaffold_selection_rank",
        "scaffold_id",
        "scaffold_pdb_code",
        "scaffold_role",
        "ranked_unique_candidates",
        "default_filter_survivors",
        "candidates_with_any_hotspot",
        "candidates_passing_complex_rmsd",
        "median_design_to_target_iptm",
        "median_min_design_to_target_pae_a",
        "best_computed_filter_pass_count",
    ),
    "run_manifest.csv": (
        "selection_rank",
        "scaffold_id",
        "pdb_code",
        "role",
        "status",
        "attempt",
        "requested_designs",
        "raw_design_pairs",
        "inverse_folded_pairs",
        "fold_npz",
        "refold_cif",
        "analyzed_rows",
        "ranked_unique_rows",
        "execute_seconds",
        "elapsed_seconds",
        "design_residue_count",
    ),
    "stage_timings.csv": (
        "selection_rank",
        "scaffold_id",
        "pdb_code",
        "scope",
        "stage",
        "stage_label_cn",
        "elapsed_seconds",
        "source_log",
    ),
}


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON；缺失、语法错误或类型错误都由调用方显式处理。"""

    return json.loads(path.read_text(encoding="utf-8"))


def read_delimited(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    """按表头读取 CSV/TSV，并拒绝没有表头的文件。"""

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError(f"规范表缺少表头：{path.relative_to(RUN_ROOT)}")
        return list(reader)


def csv_header(path: Path, delimiter: str = ",") -> tuple[str, ...]:
    """只读取表头，用来核验来源 SQL 的列名。"""

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            return tuple(next(reader))
        except StopIteration as error:
            raise ValueError(f"规范表为空：{path.relative_to(RUN_ROOT)}") from error


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    """把 JSON 对象收窄为字典，否则给出可定位的阻断错误。"""

    if not isinstance(value, dict):
        raise TypeError(f"{label} 必须是 JSON object")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    """把 JSON 数组收窄为列表，否则给出可定位的阻断错误。"""

    if not isinstance(value, list):
        raise TypeError(f"{label} 必须是 JSON array")
    return value


def as_bool(value: Any, label: str) -> bool:
    """严格转换 CSV/JSON 布尔值，避免把任意非空字符串误判为 True。"""

    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{label} 不是可识别的布尔值：{value!r}")


def as_int(value: Any, label: str) -> int:
    """严格转换整数；拒绝 1.5 之类会被静默截断的值。"""

    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label} 必须是有限整数：{value!r}")
    return int(number)


def as_float(value: Any, label: str, *, allow_missing: bool = False) -> float | None:
    """转换有限浮点数；允许缺失时返回 None，绝不把 NaN 写进 JSON。"""

    if value is None or str(value).strip() == "":
        if allow_missing:
            return None
        raise ValueError(f"{label} 缺失")
    number = float(value)
    if not math.isfinite(number):
        if allow_missing:
            return None
        raise ValueError(f"{label} 不是有限数：{value!r}")
    return number


def round_or_none(value: float | None, digits: int = 5) -> float | None:
    """报告展示使用稳定小数位；缺失值保持为 JSON null。"""

    return None if value is None else round(value, digits)


def exact_threshold_text(value: float) -> str:
    """展示过滤阈值且不把 0.0001 之类的小数舍入成 0。"""

    rendered = format(value, ".10g")
    # 便携表格会把“看起来像数值”的文本再次按通用两位格式化；给小数
    # 加上明确审计标记，使 0.0001 保持逐字显示。
    return f"{rendered}（精确值）" if 0 < abs(value) < 0.01 else rendered


def relative(path: Path) -> str:
    """来源路径统一写成相对 RUN_ROOT 的 POSIX 形式，便于报告迁移。"""

    return path.relative_to(RUN_ROOT).as_posix()


def select_sql(columns: Iterable[str], relative_csv_path: str, *, suffix: str = "") -> str:
    """为 DuckDB 来源生成显式列查询；保留 ``order`` 等保留字的双引号。"""

    quoted = [f'"{column}"' if column == "order" else column for column in columns]
    sql = f"SELECT {', '.join(quoted)} FROM read_csv_auto('{relative_csv_path}')"
    return f"{sql} {suffix.strip()}".strip()


def csv_source(
    source_id: str,
    label: str,
    filename: str,
    *,
    description: str,
    sql: str | None = None,
) -> dict[str, Any]:
    """创建带可执行 DuckDB 查询的规范表来源对象。"""

    path = f"analysis/{filename}"
    columns = QUERY_COLUMNS[filename]
    return {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": sql or select_sql(columns, path),
            "description": description,
            "tables_used": [path],
        },
    }


def file_source(source_id: str, label: str, path: str, description: str) -> dict[str, Any]:
    """创建 JSON/许可/代码来源对象。

    便携报告合同要求所有可见指标卡、图表和表格都携带真实 SQL。
    JSON 来源因此用 DuckDB ``read_json_auto`` 暴露原始机器记录；报告中的
    Python 类型化步骤再把嵌套字段整理成 snapshot 表。纯文本许可文件只作
    叙事出处，不伪造 SQL 查询。
    """

    source: dict[str, Any] = {
        "id": source_id,
        "label": label,
        "path": path,
        "description": description,
    }
    if path.lower().endswith(".json"):
        source["query"] = {
            "engine": "duckdb",
            "language": "sql",
            "sql": f"SELECT * FROM read_json_auto('{path}')",
            "description": f"读取 {path} 的完整机器可读记录；报告生成器随后类型化嵌套字段。",
            "tables_used": [path],
        }
    return source


def assert_required_files() -> None:
    """确认分析规范表与 provenance 完整；本函数不会创建任何目录。"""

    required = [
        PROVENANCE_ROOT / "input_manifest.json",
        PROVENANCE_ROOT / "runtime_preflight.json",
        RUN_ROOT / "vendor" / "boltzgen_mps_pr145" / "LICENSE",
    ]
    required.extend(ANALYSIS_ROOT / name for name in REQUIRED_ANALYSIS_FILES)
    missing = [relative(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "分析尚未完成，拒绝生成报告。缺失文件：" + ", ".join(missing)
        )


def assert_query_columns() -> None:
    """逐表核对 SQL 选择列；若分析脚本变更 schema，报告必须同步更新。"""

    errors: list[str] = []
    for filename, requested_columns in QUERY_COLUMNS.items():
        header = set(csv_header(ANALYSIS_ROOT / filename))
        missing = [column for column in requested_columns if column not in header]
        if missing:
            errors.append(f"{filename}: {', '.join(missing)}")
    if errors:
        raise ValueError("报告来源查询引用了不存在的 CSV 列：" + "；".join(errors))


def normalize_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    """把候选规范表一行转换为类型稳定、可直接放入 snapshot 的记录。"""

    candidate_id = str(row["candidate_id"])
    label = f"candidate_metrics[{candidate_id}]"
    return {
        "candidate_id": candidate_id,
        "candidate_label": str(row["candidate_label"]),
        "local_candidate_index": as_int(row["local_candidate_index"], f"{label}.local_candidate_index"),
        "scaffold_selection_rank": as_int(row["scaffold_selection_rank"], f"{label}.scaffold_selection_rank"),
        "scaffold_id": str(row["scaffold_id"]),
        "scaffold_pdb_code": str(row["scaffold_pdb_code"]),
        "scaffold_role": str(row["scaffold_role"]),
        "scaffold_resolution_a": round_or_none(as_float(row["scaffold_resolution_a"], f"{label}.scaffold_resolution_a", allow_missing=True)),
        "scaffold_r_free": round_or_none(as_float(row["scaffold_r_free"], f"{label}.scaffold_r_free", allow_missing=True)),
        "design_residue_count": as_int(row["design_residue_count"], f"{label}.design_residue_count"),
        "cdr1_length_aa": as_int(row["cdr1_length_aa"], f"{label}.cdr1_length_aa"),
        "cdr2_length_aa": as_int(row["cdr2_length_aa"], f"{label}.cdr2_length_aa"),
        "cdr3_length_aa": as_int(row["cdr3_length_aa"], f"{label}.cdr3_length_aa"),
        "cdr1_sequence": str(row["cdr1_sequence"]),
        "cdr2_sequence": str(row["cdr2_sequence"]),
        "cdr3_sequence": str(row["cdr3_sequence"]),
        "designed_sequence": str(row["designed_sequence"]),
        "designed_chain_sequence": str(row["designed_chain_sequence"]),
        "framework_sequence_unchanged": as_bool(row["framework_sequence_unchanged"], f"{label}.framework_sequence_unchanged"),
        "filter_rmsd_a": round_or_none(as_float(row["filter_rmsd_a"], f"{label}.filter_rmsd_a")),
        "filter_rmsd_design_a": round_or_none(as_float(row["filter_rmsd_design_a"], f"{label}.filter_rmsd_design_a")),
        "bb_target_aligned_rmsd_design_a": round_or_none(as_float(row["bb_target_aligned_rmsd_design_a"], f"{label}.bb_target_aligned_rmsd_design_a", allow_missing=True)),
        "design_to_target_iptm": round_or_none(as_float(row["design_to_target_iptm"], f"{label}.design_to_target_iptm")),
        "design_ptm": round_or_none(as_float(row["design_ptm"], f"{label}.design_ptm", allow_missing=True)),
        "design_ipsae_min": round_or_none(as_float(row["design_ipsae_min"], f"{label}.design_ipsae_min", allow_missing=True)),
        "min_design_to_target_pae_a": round_or_none(as_float(row["min_design_to_target_pae_a"], f"{label}.min_design_to_target_pae_a")),
        "complex_plddt": round_or_none(as_float(row["complex_plddt"], f"{label}.complex_plddt", allow_missing=True)),
        "complex_iplddt": round_or_none(as_float(row["complex_iplddt"], f"{label}.complex_iplddt", allow_missing=True)),
        "prerefold_hotspot_coverage_fraction_lt8a": round_or_none(as_float(row["prerefold_hotspot_coverage_fraction_lt8a"], f"{label}.prerefold_hotspot_coverage_fraction_lt8a")),
        "independent_hotspot_coverage_heavy_lt8a": round_or_none(as_float(row["independent_hotspot_coverage_heavy_lt8a"], f"{label}.independent_hotspot_coverage_heavy_lt8a")),
        "independent_hotspot_coverage_ca_lt8a": round_or_none(as_float(row["independent_hotspot_coverage_ca_lt8a"], f"{label}.independent_hotspot_coverage_ca_lt8a")),
        "his7_min_heavy_atom_distance_a": round_or_none(as_float(row["his7_min_heavy_atom_distance_a"], f"{label}.his7_min_heavy_atom_distance_a")),
        "ala8_min_heavy_atom_distance_a": round_or_none(as_float(row["ala8_min_heavy_atom_distance_a"], f"{label}.ala8_min_heavy_atom_distance_a")),
        "target_delta_sasa_refolded_a2": round_or_none(as_float(row["target_delta_sasa_refolded_a2"], f"{label}.target_delta_sasa_refolded_a2", allow_missing=True), 3),
        "geometric_hbond_count_refolded": as_int(row["geometric_hbond_count_refolded"], f"{label}.geometric_hbond_count_refolded"),
        "charged_atom_pair_count_refolded": as_int(row["charged_atom_pair_count_refolded"], f"{label}.charged_atom_pair_count_refolded"),
        "liability_score": round_or_none(as_float(row["liability_score"], f"{label}.liability_score", allow_missing=True)),
        "liability_num_violations": as_int(row["liability_num_violations"], f"{label}.liability_num_violations"),
        "liability_high_severity_violations": as_int(row["liability_high_severity_violations"], f"{label}.liability_high_severity_violations"),
        "liability_summary": str(row["liability_summary"]),
        "computed_filter_pass_count": as_int(row["computed_filter_pass_count"], f"{label}.computed_filter_pass_count"),
        "computed_filter_total": as_int(row["computed_filter_total"], f"{label}.computed_filter_total"),
        "failed_filter_count": as_int(row["failed_filter_count"], f"{label}.failed_filter_count"),
        "failed_filters_cn": str(row["failed_filters_cn"]),
        "pass_all_default_filters": as_bool(row["pass_all_default_filters"], f"{label}.pass_all_default_filters"),
        "selected_by_budget": as_bool(row["selected_by_budget"], f"{label}.selected_by_budget"),
        "boltzgen_internal_prefix_pass_score": round_or_none(as_float(row["boltzgen_internal_prefix_pass_score"], f"{label}.boltzgen_internal_prefix_pass_score", allow_missing=True)),
        "final_rank_within_scaffold": as_int(row["final_rank_within_scaffold"], f"{label}.final_rank_within_scaffold"),
        "quality_score_within_scaffold_only": round_or_none(as_float(row["quality_score_within_scaffold_only"], f"{label}.quality_score_within_scaffold_only", allow_missing=True)),
        "fold_sample_count": as_int(row["fold_sample_count"], f"{label}.fold_sample_count"),
        "analysis_best_sample_index": as_int(row["analysis_best_sample_index"], f"{label}.analysis_best_sample_index"),
        "writer_best_sample_index": as_int(row["writer_best_sample_index"], f"{label}.writer_best_sample_index"),
        "same_best_sample": as_bool(row["same_best_sample"], f"{label}.same_best_sample"),
        "source_metrics_csv": str(row["source_metrics_csv"]),
        "source_refold_cif": str(row["source_refold_cif"]),
    }


def normalize_filter_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """类型化一条过滤汇总记录。"""

    order = as_int(row["filter_order"], "filter_summary.filter_order")
    threshold = as_float(row["threshold"], f"filter_summary[{order}].threshold")
    return {
        "filter_order": order,
        "filter_label_cn": str(row["filter_label_cn"]),
        "pass_column": str(row["pass_column"]),
        "value_column": str(row["value_column"]),
        "operator": str(row["operator"]),
        "threshold": round_or_none(threshold),
        "threshold_display": exact_threshold_text(threshold),
        "unit": str(row["unit"]),
        "candidate_count": as_int(row["candidate_count"], f"filter_summary[{order}].candidate_count"),
        "passed_count": as_int(row["passed_count"], f"filter_summary[{order}].passed_count"),
        "failed_count": as_int(row["failed_count"], f"filter_summary[{order}].failed_count"),
        "failure_rate": round_or_none(as_float(row["failure_rate"], f"filter_summary[{order}].failure_rate")),
    }


def normalize_filter_result(row: Mapping[str, Any]) -> dict[str, Any]:
    """类型化候选×过滤条件明细，保留观测值和阈值以支持逐项复核。"""

    candidate_id = str(row["candidate_id"])
    order = as_int(row["filter_order"], f"candidate_filter_results[{candidate_id}].filter_order")
    threshold = as_float(
        row["threshold"],
        f"candidate_filter_results[{candidate_id},{order}].threshold",
    )
    return {
        "candidate_id": candidate_id,
        "scaffold_id": str(row["scaffold_id"]),
        "pdb_code": str(row["pdb_code"]),
        "filter_order": order,
        "filter_label_cn": str(row["filter_label_cn"]),
        "pass_column": str(row["pass_column"]),
        "value_column": str(row["value_column"]),
        "observed_value": round_or_none(as_float(row["observed_value"], f"candidate_filter_results[{candidate_id},{order}].observed_value", allow_missing=True)),
        "operator": str(row["operator"]),
        "threshold": round_or_none(threshold),
        "threshold_display": exact_threshold_text(threshold),
        "unit": str(row["unit"]),
        "passed": as_bool(row["passed"], f"candidate_filter_results[{candidate_id},{order}].passed"),
    }


def normalize_funnel(row: Mapping[str, Any]) -> dict[str, Any]:
    """类型化流程漏斗；stage_key 保留 pass 与预算展示的语义差别。"""

    order = as_int(row["order"], "process_funnel.order")
    return {
        "order": order,
        "stage_key": str(row["stage_key"]),
        "stage_label_cn": str(row["stage_label_cn"]),
        "count": as_int(row["count"], f"process_funnel[{order}].count"),
        "unit": str(row["unit"]),
        "fraction_of_requested_candidates": round_or_none(as_float(row["fraction_of_requested_candidates"], f"process_funnel[{order}].fraction", allow_missing=True)),
    }


def normalize_per_scaffold(row: Mapping[str, Any]) -> dict[str, Any]:
    """类型化一个骨架的第一轮结果摘要。"""

    scaffold_id = str(row["scaffold_id"])
    label = f"per_scaffold_summary[{scaffold_id}]"
    return {
        "scaffold_selection_rank": as_int(row["scaffold_selection_rank"], f"{label}.rank"),
        "scaffold_id": scaffold_id,
        "scaffold_pdb_code": str(row["scaffold_pdb_code"]),
        "scaffold_role": str(row["scaffold_role"]),
        "ranked_unique_candidates": as_int(row["ranked_unique_candidates"], f"{label}.ranked_unique_candidates"),
        "default_filter_survivors": as_int(row["default_filter_survivors"], f"{label}.default_filter_survivors"),
        "candidates_with_any_hotspot": as_int(row["candidates_with_any_hotspot"], f"{label}.candidates_with_any_hotspot"),
        "candidates_passing_complex_rmsd": as_int(row["candidates_passing_complex_rmsd"], f"{label}.candidates_passing_complex_rmsd"),
        "median_design_to_target_iptm": round_or_none(as_float(row["median_design_to_target_iptm"], f"{label}.median_design_to_target_iptm", allow_missing=True)),
        "median_min_design_to_target_pae_a": round_or_none(as_float(row["median_min_design_to_target_pae_a"], f"{label}.median_min_design_to_target_pae_a", allow_missing=True)),
        "best_computed_filter_pass_count": as_int(row["best_computed_filter_pass_count"], f"{label}.best_computed_filter_pass_count"),
    }


def normalize_run(row: Mapping[str, Any]) -> dict[str, Any]:
    """类型化每骨架运行清单，用于工程完成性与原始产物计数复核。"""

    scaffold_id = str(row["scaffold_id"])
    label = f"run_manifest[{scaffold_id}]"
    return {
        "selection_rank": as_int(row["selection_rank"], f"{label}.selection_rank"),
        "scaffold_id": scaffold_id,
        "pdb_code": str(row["pdb_code"]),
        "role": str(row["role"]),
        "status": str(row["status"]),
        # analysis/run_manifest.csv 的 attempt 是可追溯的运行目录，例如
        # runs/01_pdb_00007xl0-A/attempt_001；它不是数值尝试序号。
        "attempt": str(row["attempt"]),
        "requested_designs": as_int(row["requested_designs"], f"{label}.requested_designs"),
        "raw_design_pairs": as_int(row["raw_design_pairs"], f"{label}.raw_design_pairs"),
        "inverse_folded_pairs": as_int(row["inverse_folded_pairs"], f"{label}.inverse_folded_pairs"),
        "fold_npz": as_int(row["fold_npz"], f"{label}.fold_npz"),
        "refold_cif": as_int(row["refold_cif"], f"{label}.refold_cif"),
        "analyzed_rows": as_int(row["analyzed_rows"], f"{label}.analyzed_rows"),
        "ranked_unique_rows": as_int(row["ranked_unique_rows"], f"{label}.ranked_unique_rows"),
        "execute_seconds": round_or_none(as_float(row["execute_seconds"], f"{label}.execute_seconds"), 3),
        "elapsed_seconds": round_or_none(as_float(row["elapsed_seconds"], f"{label}.elapsed_seconds"), 3),
        "design_residue_count": as_int(row["design_residue_count"], f"{label}.design_residue_count"),
    }


def normalize_scaffold_registry(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从冻结 provenance 提取 12 条骨架真源、角色和结构质量信息。"""

    population = require_mapping(manifest.get("scaffold_population"), "input_manifest.scaffold_population")
    records = require_list(population.get("records"), "input_manifest.scaffold_population.records")
    normalized: list[dict[str, Any]] = []
    for raw in records:
        row = require_mapping(raw, "input_manifest.scaffold_population.records[]")
        scaffold_id = str(row["candidate_id"])
        normalized.append(
            {
                "selection_rank": as_int(row["selection_rank"], f"registry[{scaffold_id}].selection_rank"),
                "scaffold_id": scaffold_id,
                "pdb_code": str(row["pdb_code"]),
                "source_chain": str(row["source_hchain"]),
                "sabdab_id": str(row["sabdab_id"]),
                "role": str(row["role"]),
                "framework_cluster_id": str(row["framework_cluster_id"]),
                "method": str(row["method"]),
                "resolution_a": round_or_none(as_float(row["resolution_a"], f"registry[{scaffold_id}].resolution_a")),
                "r_free": round_or_none(as_float(row["r_free"], f"registry[{scaffold_id}].r_free")),
                "variable_length_aa": as_int(row["variable_length_aa"], f"registry[{scaffold_id}].variable_length_aa"),
                "cdr1_length_aa": as_int(row["cdr1_length_aa"], f"registry[{scaffold_id}].cdr1_length_aa"),
                "cdr2_length_aa": as_int(row["cdr2_length_aa"], f"registry[{scaffold_id}].cdr2_length_aa"),
                "cdr3_length_aa": as_int(row["cdr3_length_aa"], f"registry[{scaffold_id}].cdr3_length_aa"),
                "prior_boltzgen_check_status": str(row["prior_boltzgen_check_status"]),
                "prior_boltzgen_check_output_sha256": str(row["prior_boltzgen_check_output_sha256"]),
                "input_package": str(row["input_package"]),
            }
        )
    return sorted(normalized, key=lambda row: row["selection_rank"])


def build_stage_totals(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """按模型步骤汇总 12 个骨架耗时；wrapper 墙钟记录不混入模型步骤总和。"""

    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in raw_rows:
        if row["scope"] != "model_step":
            continue
        stage = row["stage"]
        if stage not in MODEL_STAGE_ORDER:
            raise ValueError(f"stage_timings.csv 出现未知模型步骤：{stage}")
        elapsed = as_float(row["elapsed_seconds"], f"stage_timings[{row['scaffold_id']},{stage}].elapsed_seconds")
        assert elapsed is not None
        grouped[(stage, row["stage_label_cn"])].append(elapsed)

    rows: list[dict[str, Any]] = []
    for (stage, label), values in grouped.items():
        rows.append(
            {
                "stage_order": MODEL_STAGE_ORDER[stage],
                "stage": stage,
                "stage_label_cn": label,
                "total_seconds": round(sum(values), 3),
                "median_seconds": round(statistics.median(values), 3),
                "scaffold_runs": len(values),
            }
        )
    return sorted(rows, key=lambda row: row["stage_order"])


def build_validation_rows(validation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 validation checks 转成原生表格可显示的行。"""

    checks = require_mapping(validation.get("checks"), "validation_report.checks")
    return [
        {"check": name, "passed": as_bool(passed, f"validation_report.checks.{name}")}
        for name, passed in checks.items()
    ]


def build_priority_rows(summary: Mapping[str, Any], candidate_ids: set[str]) -> list[dict[str, Any]]:
    """读取人工复盘顺序；字段名本身强调这些记录不是 binder 结论。"""

    raw_rows = require_list(
        summary.get("manual_review_priority_not_binders"),
        "run_summary.manual_review_priority_not_binders",
    )
    rows: list[dict[str, Any]] = []
    for rank, raw in enumerate(raw_rows, start=1):
        row = require_mapping(raw, "run_summary.manual_review_priority_not_binders[]")
        candidate_id = str(row["candidate_id"])
        if candidate_id not in candidate_ids:
            raise ValueError(f"人工复盘清单引用未知候选：{candidate_id}")
        rows.append(
            {
                "review_order": rank,
                "candidate_id": candidate_id,
                "candidate_label": str(row["candidate_label"]),
                "passed_filter_count": as_int(row["passed_filter_count"], f"priority[{candidate_id}].passed_filter_count"),
                "failed_filters_cn": str(row["failed_filters_cn"]),
                "design_to_target_iptm": round_or_none(as_float(row["design_to_target_iptm"], f"priority[{candidate_id}].design_to_target_iptm")),
                "min_design_to_target_pae_a": round_or_none(as_float(row["min_design_to_target_pae_a"], f"priority[{candidate_id}].min_design_to_target_pae_a")),
                "interpretation": "仅供人工复盘；不是实验命中、亲和力或结合概率结论",
            }
        )
    return rows


def validate_consistency(
    manifest: Mapping[str, Any],
    validation: Mapping[str, Any],
    summary: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    filter_summary: list[dict[str, Any]],
    filter_results: list[dict[str, Any]],
    funnel: list[dict[str, Any]],
    scaffolds: list[dict[str, Any]],
    scaffold_results: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    stage_totals: list[dict[str, Any]],
) -> None:
    """执行生成报告前的阻断性一致性检查。

    这里重复验证关键数量不是多余工作：``analyze_round1.py`` 验证分析过程，
    本函数验证“即将写入报告的 snapshot”仍与这些结论一致。
    """

    errors: list[str] = []

    # 只有没有失败检查的 READY assessment 才能进入报告。
    if validation.get("assessment") != READY_ASSESSMENT:
        errors.append(f"validation assessment={validation.get('assessment')!r}")
    failed_checks = require_list(validation.get("failed_checks"), "validation_report.failed_checks")
    if failed_checks:
        errors.append("validation failed_checks 非空：" + ", ".join(map(str, failed_checks)))
    checks = require_mapping(validation.get("checks"), "validation_report.checks")
    false_checks = [name for name, passed in checks.items() if not as_bool(passed, f"validation.{name}")]
    if false_checks:
        errors.append("validation checks 中仍有 false：" + ", ".join(false_checks))

    # 骨架来源、运行清单与结果表必须是同一组 12 个唯一 ID。
    if len(scaffolds) != 12 or len({row["scaffold_id"] for row in scaffolds}) != 12:
        errors.append("冻结骨架 registry 不是 12 条唯一记录")
    if [row["selection_rank"] for row in scaffolds] != list(range(1, 13)):
        errors.append("冻结骨架 selection_rank 不是 1–12")
    if sum(row["role"] == "PRIMARY" for row in scaffolds) != 10 or sum(row["role"] == "RESERVE" for row in scaffolds) != 2:
        errors.append("骨架角色不是 10 PRIMARY + 2 RESERVE")
    if any(row["prior_boltzgen_check_status"] != "PASS" for row in scaffolds):
        errors.append("存在 prior BoltzGen check 未通过的骨架")

    registry_ids = {row["scaffold_id"] for row in scaffolds}
    run_ids = {row["scaffold_id"] for row in runs}
    result_ids = {row["scaffold_id"] for row in scaffold_results}
    if len(runs) != 12 or run_ids != registry_ids:
        errors.append("run_manifest 与 12 条冻结骨架不一致")
    if len(scaffold_results) != 12 or result_ids != registry_ids:
        errors.append("per_scaffold_summary 与 12 条冻结骨架不一致")

    # 工程链路必须完成；这里验证的是文件/步骤完整性，不是候选生物学可用性。
    if any(row["status"] != "PIPELINE_COMPLETE" for row in runs):
        errors.append("至少一个骨架任务不是 PIPELINE_COMPLETE")
    for field in ("raw_design_pairs", "inverse_folded_pairs", "fold_npz", "refold_cif"):
        total = sum(row[field] for row in runs)
        if total != 24:
            errors.append(f"{field} 总数为 {total}，不是 24")

    # 骨架内去重后可能保留 12–24 条；每条候选仍必须有唯一 ID 和完整 10 项过滤。
    candidate_ids = [row["candidate_id"] for row in candidates]
    if not 12 <= len(candidates) <= 24:
        errors.append(f"骨架内去重候选数 {len(candidates)} 不在 12–24 范围")
    if len(candidate_ids) != len(set(candidate_ids)):
        errors.append("candidate_id 不唯一")
    if {row["scaffold_id"] for row in candidates} != registry_ids:
        errors.append("候选没有覆盖全部 12 个骨架")
    if any(row["computed_filter_total"] != 10 for row in candidates):
        errors.append("候选 computed_filter_total 不是 10")
    if any(row["computed_filter_pass_count"] + row["failed_filter_count"] != 10 for row in candidates):
        errors.append("候选通过数 + 失败数不等于 10")
    if any(not row["framework_sequence_unchanged"] for row in candidates):
        errors.append("至少一个候选的设计掩码外框架序列发生变化")
    if any(not row["same_best_sample"] for row in candidates):
        errors.append("analysis 与 writer 使用的最佳复折叠样本不一致")

    expected_filter_rows = len(candidates) * 10
    if len(filter_summary) != 10:
        errors.append(f"过滤汇总不是 10 条，而是 {len(filter_summary)} 条")
    if [row["filter_order"] for row in filter_summary] != list(range(1, 11)):
        errors.append("过滤汇总顺序不是 1–10")
    if len(filter_results) != expected_filter_rows:
        errors.append(f"候选过滤明细为 {len(filter_results)} 条，期望 {expected_filter_rows} 条")
    filter_counts: dict[str, int] = defaultdict(int)
    for row in filter_results:
        filter_counts[row["candidate_id"]] += 1
    if any(filter_counts[candidate_id] != 10 for candidate_id in candidate_ids):
        errors.append("至少一个候选没有恰好 10 条过滤明细")
    if any(row["candidate_count"] != len(candidates) for row in filter_summary):
        errors.append("filter_summary 的候选分母与 candidate_metrics 行数不一致")
    if any(row["passed_count"] + row["failed_count"] != row["candidate_count"] for row in filter_summary):
        errors.append("filter_summary 通过数 + 失败数与分母不一致")

    # 逐候选布尔结论必须与 10 条明细重新聚合后一致。
    passed_by_candidate: dict[str, int] = defaultdict(int)
    for row in filter_results:
        passed_by_candidate[row["candidate_id"]] += int(row["passed"])
    for row in candidates:
        passed_count = passed_by_candidate[row["candidate_id"]]
        if passed_count != row["computed_filter_pass_count"]:
            errors.append(f"{row['candidate_id']} 的过滤明细通过数与候选表不一致")
        if (passed_count == 10) != row["pass_all_default_filters"]:
            errors.append(f"{row['candidate_id']} 的全通过布尔值与 10 条明细不一致")

    # 两种覆盖比例分别属于复折叠前 token-center 和复折叠后独立重原子阶段；
    # 这里只检查各自取值域，不要求二者相等。
    allowed_coverages = {0.0, 0.5, 1.0}
    for field in (
        "prerefold_hotspot_coverage_fraction_lt8a",
        "independent_hotspot_coverage_heavy_lt8a",
        "independent_hotspot_coverage_ca_lt8a",
    ):
        observed = {float(row[field]) for row in candidates}
        if not observed.issubset(allowed_coverages):
            errors.append(f"{field} 出现 0、0.5、1 以外的值：{sorted(observed)}")

    # 冻结预算是每骨架展示 1 条；这是目录展示完整性检查，不是质量通过检查。
    budget_per_scaffold: dict[str, int] = defaultdict(int)
    for row in candidates:
        budget_per_scaffold[row["scaffold_id"]] += int(row["selected_by_budget"])
    if any(budget_per_scaffold[scaffold_id] != 1 for scaffold_id in registry_ids):
        errors.append("selected_by_budget 不是每个骨架恰好 1 条（该检查不代表通过）")

    # 五个步骤每步都应有 12 条骨架记录；wrapper 墙钟时间不在这张汇总表中。
    if [row["stage"] for row in stage_totals] != list(MODEL_STAGE_ORDER):
        errors.append("stage_timings 没有形成完整且有序的五步模型阶段")
    if any(row["scaffold_runs"] != 12 for row in stage_totals):
        errors.append("至少一个模型步骤不是 12 条骨架耗时记录")

    # 报告 headline 必须和规范候选表、run summary 互相核对。
    engineering = require_mapping(summary.get("engineering_status"), "run_summary.engineering_status")
    actual_survivors = sum(row["pass_all_default_filters"] for row in candidates)
    actual_budget = sum(row["selected_by_budget"] for row in candidates)
    expected_values = {
        "scaffold_tasks_complete": 12,
        "scaffold_tasks_total": 12,
        "raw_candidates_requested": 24,
        "raw_design_pairs_complete": 24,
        "ranked_unique_candidates": len(candidates),
        "default_filter_survivors": actual_survivors,
        "budget_display_candidates": actual_budget,
    }
    for field, expected in expected_values.items():
        observed = as_int(engineering.get(field), f"run_summary.engineering_status.{field}")
        if observed != expected:
            errors.append(f"run_summary.{field}={observed}，期望 {expected}")

    # 漏斗的最终两个概念必须分别存在，且预算项永远不能替代过滤通过项。
    funnel_by_key = {row["stage_key"]: row for row in funnel}
    if funnel_by_key.get("pass_filters", {}).get("count") != actual_survivors:
        errors.append("process_funnel.pass_filters 与候选通过数不一致")
    if funnel_by_key.get("selected_budget", {}).get("count") != actual_budget:
        errors.append("process_funnel.selected_budget 与预算展示数不一致")

    # 单一正靶、端酰胺未闭环、未运行反靶，是本报告不可越过的科学边界。
    target = require_mapping(manifest.get("target"), "input_manifest.target")
    boundary = require_mapping(summary.get("target_boundary"), "run_summary.target_boundary")
    if target.get("role") != "positive_target_geometry_only":
        errors.append("target role 不是 positive_target_geometry_only")
    if as_bool(target.get("terminal_amide_atomically_verified"), "target.terminal_amide_atomically_verified"):
        errors.append("manifest 意外声称端酰胺已完成原子级验证")
    if as_bool(boundary.get("terminal_amide_atomically_verified"), "target_boundary.terminal_amide_atomically_verified"):
        errors.append("run_summary 意外声称端酰胺已完成原子级验证")
    if as_bool(boundary.get("off_target_or_multiconformation_evaluated"), "target_boundary.off_target_or_multiconformation_evaluated"):
        errors.append("run_summary 意外声称已评价反靶或多构象")
    if as_bool(boundary.get("selectivity_claim_allowed"), "target_boundary.selectivity_claim_allowed"):
        errors.append("run_summary 意外允许型态选择性结论")

    if errors:
        raise ValueError("报告前一致性验证失败：\n- " + "\n- ".join(errors))


def build_scope_rows(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    """生成范围、分析单位和关键术语定义表。"""

    target = require_mapping(manifest["target"], "input_manifest.target")
    budget = require_mapping(manifest["generation_budget"], "input_manifest.generation_budget")
    return [
        {
            "term": "本轮操作",
            "definition": "固定预训练权重的推理与候选生成，不是模型权重训练。",
            "project_interpretation": str(manifest["execution_semantics"]),
        },
        {
            "term": "正靶输入",
            "definition": "GLP-1(7–36) 的单一受体结合态几何，只提供正靶空间条件。",
            "project_interpretation": f"{target['name']}；30 个残基；热点提示为 His7/Ala8。",
        },
        {
            "term": "原始候选",
            "definition": "结构生成、逆折叠和复折叠链路中按请求产生的候选记录。",
            "project_interpretation": f"每骨架 {budget['designs_per_scaffold']} 个，共 {budget['requested_total_designs']} 个请求。",
        },
        {
            "term": "骨架内去重候选",
            "definition": "在每个 VHH 骨架内部去重后进入统一分析的候选；分母可能小于原始请求数。",
            "project_interpretation": "报告候选表、过滤分母和散点图均使用这个分析单位。",
        },
        {
            "term": "通过全部默认计算过滤",
            "definition": "同一候选在本轮十项计算条件上全部为真。",
            "project_interpretation": "这是结构与序列代理门槛，不是实验结合或可开发性结论。",
        },
        {
            "term": "预算展示项",
            "definition": "每个骨架按批内顺序保留一个目录展示项。",
            "project_interpretation": "selected_by_budget 不等于通过过滤；即使无人通过也可以存在。",
        },
        {
            "term": "PRIMARY / RESERVE",
            "definition": "骨架库中预先冻结的主用与备用角色。",
            "project_interpretation": "10 条 PRIMARY、2 条 RESERVE；角色不是本轮结果标签。",
        },
    ]


def source_definitions() -> dict[str, dict[str, Any]]:
    """集中创建 artifact 的来源对象，避免卡片、图表和表格各自漂移。"""

    candidate_source = csv_source(
        "candidate_metrics_source",
        "骨架内去重候选规范表",
        "candidate_metrics.csv",
        description="逐候选结构、自洽性、热点、界面代理、可开发性与过滤结论。",
    )
    filter_source = csv_source(
        "filter_summary_source",
        "十项默认过滤汇总",
        "filter_summary.csv",
        description="每一项过滤的真实阈值、分母、通过数、失败数与失败率。",
        sql=select_sql(QUERY_COLUMNS["filter_summary.csv"], "analysis/filter_summary.csv", suffix="ORDER BY filter_order"),
    )
    filter_detail_source = csv_source(
        "candidate_filter_results_source",
        "候选×过滤条件明细",
        "candidate_filter_results.csv",
        description="每个候选在每个过滤条件下的观测值、运算符、阈值和布尔结论。",
        sql=select_sql(
            QUERY_COLUMNS["candidate_filter_results.csv"],
            "analysis/candidate_filter_results.csv",
            suffix="ORDER BY pdb_code, candidate_id, filter_order",
        ),
    )
    funnel_source = csv_source(
        "process_funnel_source",
        "流程漏斗规范表",
        "process_funnel.csv",
        description="按执行阶段记录样本/候选数量；pass_filters 与 selected_budget 是两个不同概念。",
        sql=select_sql(
            QUERY_COLUMNS["process_funnel.csv"],
            "analysis/process_funnel.csv",
            suffix='WHERE unit = \'candidate\' ORDER BY "order"',
        ),
    )
    scaffold_result_source = csv_source(
        "per_scaffold_source",
        "12 骨架结果摘要",
        "per_scaffold_summary.csv",
        description="每个冻结骨架的去重候选数、计算过滤通过数、复折叠前提示位点覆盖、RMSD状态和批内最佳过滤项数。",
        sql=select_sql(
            QUERY_COLUMNS["per_scaffold_summary.csv"],
            "analysis/per_scaffold_summary.csv",
            suffix="ORDER BY scaffold_selection_rank",
        ),
    )
    run_source = csv_source(
        "run_manifest_source",
        "12 骨架运行清单",
        "run_manifest.csv",
        description="每个骨架任务的状态、产物计数与墙钟耗时。",
        sql=select_sql(QUERY_COLUMNS["run_manifest.csv"], "analysis/run_manifest.csv", suffix="ORDER BY selection_rank"),
    )
    stage_sql = (
        "SELECT stage, stage_label_cn, SUM(elapsed_seconds) AS total_seconds, "
        "MEDIAN(elapsed_seconds) AS median_seconds, COUNT(*) AS scaffold_runs "
        "FROM read_csv_auto('analysis/stage_timings.csv') "
        "WHERE scope = 'model_step' GROUP BY stage, stage_label_cn"
    )
    stage_source = csv_source(
        "stage_timings_source",
        "五步模型耗时规范表",
        "stage_timings.csv",
        description="只汇总 scope=model_step 的五步模型耗时；不把 wrapper 墙钟记录重复计入。",
        sql=stage_sql,
    )
    hotspot_stage_sql = (
        "SELECT candidate_id, candidate_label, scaffold_pdb_code, 1 AS stage_order, "
        "'复折叠前 token-center' AS stage_definition, "
        "prerefold_hotspot_coverage_fraction_lt8a AS coverage_fraction_lt8a "
        "FROM read_csv_auto('analysis/candidate_metrics.csv') UNION ALL "
        "SELECT candidate_id, candidate_label, scaffold_pdb_code, 2 AS stage_order, "
        "'复折叠后重原子' AS stage_definition, "
        "independent_hotspot_coverage_heavy_lt8a AS coverage_fraction_lt8a "
        "FROM read_csv_auto('analysis/candidate_metrics.csv')"
    )
    hotspot_stage_source = csv_source(
        "hotspot_stage_source",
        "复折叠前后提示位点覆盖阶段对照",
        "candidate_metrics.csv",
        description="同一候选的复折叠前 token-center 覆盖与复折叠后 refold CIF 独立重原子覆盖；两者不是同一几何定义。",
        sql=hotspot_stage_sql,
    )
    return {
        "candidate": candidate_source,
        "filter": filter_source,
        "filter_detail": filter_detail_source,
        "funnel": funnel_source,
        "scaffold_result": scaffold_result_source,
        "run": run_source,
        "stage": stage_source,
        "hotspot_stage": hotspot_stage_source,
        "input_manifest": file_source(
            "input_manifest_source",
            "冻结输入与运行 provenance",
            "provenance/input_manifest.json",
            "骨架真源、主备角色、目标、运行设置、checkpoint 哈希与已知限制。",
        ),
        "runtime_preflight": file_source(
            "runtime_preflight_source",
            "运行前环境检查",
            "provenance/runtime_preflight.json",
            "PyTorch、Metal Performance Shaders 可用性和 BoltzGen 代码位置。",
        ),
        "validation": file_source(
            "validation_report_source",
            "分析一致性验证",
            "analysis/validation_report.json",
            "阻断性检查、分享状态和科学限制。",
        ),
        "summary": file_source(
            "run_summary_source",
            "机器可读运行摘要",
            "analysis/run_summary.json",
            "工程完成性、候选数量、失败模式、人工复盘顺序与目标边界。",
        ),
        "resource": file_source(
            "resource_summary_source",
            "资源观测摘要",
            "analysis/resource_summary.json",
            "进程树 CPU/RSS 的部分观测；不代表单独测量的 Metal Performance Shaders 统一内存。",
        ),
        "license": file_source(
            "boltzgen_license_source",
            "冻结 BoltzGen 代码许可",
            "vendor/boltzgen_mps_pr145/LICENSE",
            "本地冻结实验性代码快照附带的许可文本。",
        ),
    }


def make_cards(summary_source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """建立 answer-first 指标卡；预算展示卡明确标注“非通过”。"""

    definitions = (
        ("scaffold_card", "完成骨架任务", "scaffolds_complete", "个"),
        ("raw_card", "原始设计链路", "raw_design_pairs", "个"),
        ("candidate_card", "骨架内去重候选", "ranked_unique_candidates", "个"),
        ("survivor_card", "默认计算过滤通过", "default_filter_survivors", "个"),
        ("budget_card", "预算展示项（非通过）", "budget_display_candidates", "个"),
        ("runtime_card", "12 骨架 execute 总耗时", "sum_execute_seconds", "秒"),
    )
    return [
        {
            "id": card_id,
            "description": label,
            "dataset": "headline",
            "source": summary_source,
            "metrics": [{"label": label, "field": field, "unit": unit}],
        }
        for card_id, label, field, unit in definitions
    ]


def make_charts(sources: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """定义仅由 snapshot 数据驱动的原生图表；不内嵌预渲染图片。"""

    return [
        {
            "id": "stage_timing_chart",
            "title": "五步模型阶段累计耗时",
            "subtitle": "按 12 个骨架汇总；仅统计 model_step，不与 wrapper 墙钟重复相加。",
            "type": "horizontalBar",
            "intent": "comparison",
            "question": "第一轮计算时间主要消耗在哪个模型步骤？",
            "rationale": "五个步骤共享秒单位，按执行顺序比较累计耗时最直接。",
            "dataset": "stage_totals",
            "source": sources["stage"],
            "labels": {"values": "all"},
            "encodings": {
                "x": {"field": "stage_label_cn", "type": "nominal", "label": "模型步骤"},
                "y": {"field": "total_seconds", "type": "quantitative", "label": "累计秒数"},
            },
        },
        {
            "id": "process_funnel_chart",
            "title": "第一轮数据流与候选数量",
            "subtitle": "最后两行分别是计算过滤通过数与预算展示数；两者不可互换。",
            "type": "bar",
            "intent": "comparison",
            "question": "候选在生成、分析、过滤和预算展示各阶段保留多少？",
            "rationale": "按固定流程顺序比较数量，能区分工程产出、计算过滤与目录展示。",
            "dataset": "funnel_candidates",
            "source": sources["funnel"],
            "labels": {"values": "all"},
            "encodings": {
                "x": {"field": "stage_label_cn", "type": "nominal", "label": "流程阶段"},
                "y": {"field": "count", "type": "quantitative", "label": "候选数"},
            },
        },
        {
            "id": "filter_failure_chart",
            "title": "十项默认计算过滤的失败分布",
            "subtitle": "每条柱表示骨架内去重候选中未通过该项的数量。",
            "type": "bar",
            "intent": "comparison",
            "question": "本轮最常见的计算失败模式是什么？",
            "rationale": "横向类别比较适合十个长中文标签，并保留真实失败分母。",
            "dataset": "filter_summary_by_failure",
            "source": sources["filter"],
            "labels": {"values": "all"},
            "encodings": {
                # 便携报告的原生图表合同固定要求 y 为数值度量；horizontalBar
                # 会在渲染层把类别放到纵轴、度量放到横轴。
                "x": {"field": "filter_label_cn", "type": "nominal", "label": "过滤条件"},
                "y": {"field": "failed_count", "type": "quantitative", "label": "失败候选数"},
            },
        },
        {
            "id": "rmsd_scatter_chart",
            "title": "复合物与 VHH 设计区的复折叠自洽性",
            "subtitle": "虚线为 2.5 Å 默认门槛；均方根偏差不是对实验结构的误差。",
            "type": "scatter",
            "intent": "relationship",
            "question": "候选是否同时满足两个复折叠自洽性门槛？",
            "rationale": "二维散点同时呈现两个不同范围的结构偏差，并按骨架角色着色。",
            "dataset": "candidates",
            "source": sources["candidate"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "骨架角色"},
            "referenceLines": [
                {"axis": "x", "value": RMSD_THRESHOLD_A, "label": "复合物门槛 2.5 Å", "lineStyle": "dashed"},
                {"axis": "y", "value": RMSD_THRESHOLD_A, "label": "设计区门槛 2.5 Å", "lineStyle": "dashed"},
            ],
            "encodings": {
                "x": {"field": "filter_rmsd_a", "type": "quantitative", "label": "复合物骨架均方根偏差（Å）"},
                "y": {"field": "filter_rmsd_design_a", "type": "quantitative", "label": "VHH 设计区骨架均方根偏差（Å）"},
                "color": {"field": "scaffold_role", "type": "nominal"},
            },
        },
        {
            "id": "hotspot_stage_chart",
            "title": "提示位点覆盖在复折叠前后的阶段对照",
            "subtitle": "方形概念为复折叠前 token-center 覆盖；圆形概念为复折叠后 CIF 独立重原子覆盖。",
            "type": "bar",
            "intent": "comparison",
            "question": "设计阶段的提示位点覆盖信号在复折叠后是否仍能由独立几何计算观察到？",
            "rationale": "将同一候选的两个阶段并列显示，避免把不同结构阶段和不同距离定义误当为同一字段。",
            "dataset": "hotspot_stage_coverage",
            "source": sources["hotspot_stage"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "结构阶段与几何定义"},
            "encodings": {
                "x": {"field": "candidate_label", "type": "nominal", "label": "候选"},
                "y": {"field": "coverage_fraction_lt8a", "type": "quantitative", "label": "His7/Ala8 覆盖比例（0、0.5 或 1）"},
                "color": {"field": "stage_definition", "type": "nominal"},
            },
        },
        {
            "id": "hotspot_distance_chart",
            "title": "His7/Ala8 提示位点的独立重原子距离",
            "subtitle": "仅从复折叠后 refold CIF 独立重算；小于 8 Å 只表示几何接近。",
            "type": "scatter",
            "intent": "relationship",
            "question": "候选设计区是否同时接近两个目标 N 端提示位点？",
            "rationale": "二维距离图能区分只覆盖一个提示位点与同时覆盖两个提示位点。",
            "dataset": "candidates",
            "source": sources["candidate"],
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "title": "骨架角色"},
            "referenceLines": [
                {"axis": "x", "value": HOTSPOT_DISTANCE_THRESHOLD_A, "label": "His7 8 Å", "lineStyle": "dashed"},
                {"axis": "y", "value": HOTSPOT_DISTANCE_THRESHOLD_A, "label": "Ala8 8 Å", "lineStyle": "dashed"},
            ],
            "encodings": {
                "x": {"field": "his7_min_heavy_atom_distance_a", "type": "quantitative", "label": "His7 最小重原子距离（Å）"},
                "y": {"field": "ala8_min_heavy_atom_distance_a", "type": "quantitative", "label": "Ala8 最小重原子距离（Å）"},
                "color": {"field": "scaffold_role", "type": "nominal"},
            },
        },
        {
            "id": "scaffold_filter_chart",
            "title": "12 个骨架的批内最佳过滤项数",
            "subtitle": "纵轴最大值为 10；这是本轮小样本描述，不是骨架成功率或因果比较。",
            "type": "bar",
            "intent": "comparison",
            "question": "每个骨架下最接近默认计算门槛的候选通过了几项？",
            "rationale": "每骨架一个固定摘要值，适合检查主用和备用骨架的首轮覆盖。",
            "dataset": "scaffold_results",
            "source": sources["scaffold_result"],
            "labels": {"values": "all"},
            "referenceLines": [{"axis": "y", "value": 10, "label": "全部 10 项", "lineStyle": "dashed"}],
            "encodings": {
                "x": {"field": "scaffold_pdb_code", "type": "nominal", "label": "PDB 骨架"},
                "y": {"field": "best_computed_filter_pass_count", "type": "quantitative", "label": "批内最佳通过项数（/10）"},
                "color": {"field": "scaffold_role", "type": "nominal"},
            },
        },
    ]


def make_tables(sources: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """定义报告中的原生表格；长序列保留在候选表中以便逐条审计。"""

    return [
        {
            "id": "scope_table",
            "title": "范围、分析单位与术语定义",
            "subtitle": "先统一分母和“通过/展示”的含义，再解释结果。",
            "dataset": "scope_definitions",
            "source": sources["input_manifest"],
            "density": "comfortable",
            "layout": "full",
            "columns": [
                {"field": "term", "label": "术语", "type": "text"},
                {"field": "definition", "label": "定义", "type": "text"},
                {"field": "project_interpretation", "label": "本轮解释", "type": "text"},
            ],
        },
        {
            "id": "scaffold_registry_table",
            "title": "冻结的 12 条 VHH 骨架真源",
            "subtitle": "10 条主用、2 条备用；PDB/SAbDab2/链、结构质量和 prior check 均来自 input manifest。",
            "dataset": "scaffold_registry",
            "source": sources["input_manifest"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "selection_rank", "direction": "asc"},
            "columns": [
                {"field": "selection_rank", "label": "顺序"},
                {"field": "role", "label": "角色", "type": "text"},
                {"field": "pdb_code", "label": "PDB", "type": "text"},
                {"field": "source_chain", "label": "链", "type": "text"},
                {"field": "sabdab_id", "label": "SAbDab2 ID", "type": "text"},
                {"field": "framework_cluster_id", "label": "框架簇", "type": "text"},
                {"field": "resolution_a", "label": "分辨率 Å"},
                {"field": "r_free", "label": "R-free"},
                {"field": "cdr1_length_aa", "label": "CDR1 aa"},
                {"field": "cdr2_length_aa", "label": "CDR2 aa"},
                {"field": "cdr3_length_aa", "label": "CDR3 aa"},
                {"field": "prior_boltzgen_check_status", "label": "prior check", "type": "text"},
            ],
        },
        {
            "id": "run_manifest_table",
            "title": "12 个骨架任务的工程完成性",
            "subtitle": "PIPELINE_COMPLETE 只表示链路和产物完成，不表示候选通过质量门槛。",
            "dataset": "runs",
            "source": sources["run"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "selection_rank", "direction": "asc"},
            "columns": [
                {"field": "selection_rank", "label": "顺序"},
                {"field": "pdb_code", "label": "PDB", "type": "text"},
                {"field": "role", "label": "角色", "type": "text"},
                {"field": "status", "label": "工程状态", "type": "text"},
                {"field": "attempt", "label": "尝试目录", "type": "text"},
                {"field": "raw_design_pairs", "label": "原始设计"},
                {"field": "inverse_folded_pairs", "label": "逆折叠"},
                {"field": "fold_npz", "label": "复折叠 NPZ"},
                {"field": "refold_cif", "label": "复折叠 CIF"},
                {"field": "ranked_unique_rows", "label": "骨架内去重"},
                {"field": "execute_seconds", "label": "execute 秒"},
            ],
        },
        {
            "id": "scaffold_results_table",
            "title": "12 个骨架的首轮结果",
            "subtitle": "每骨架 n=2 请求；只做候选级描述，不估计骨架命中率。",
            "dataset": "scaffold_results",
            "source": sources["scaffold_result"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "scaffold_selection_rank", "direction": "asc"},
            "columns": [
                {"field": "scaffold_selection_rank", "label": "顺序"},
                {"field": "scaffold_pdb_code", "label": "PDB", "type": "text"},
                {"field": "scaffold_role", "label": "角色", "type": "text"},
                {"field": "ranked_unique_candidates", "label": "去重候选"},
                {"field": "default_filter_survivors", "label": "计算过滤通过"},
                {"field": "candidates_with_any_hotspot", "label": "复折叠前至少一提示位点覆盖"},
                {"field": "candidates_passing_complex_rmsd", "label": "复合物 RMSD 通过"},
                {"field": "best_computed_filter_pass_count", "label": "批内最佳 /10"},
                {"field": "median_design_to_target_iptm", "label": "中位 iPTM"},
                {"field": "median_min_design_to_target_pae_a", "label": "中位最小 PAE Å"},
            ],
        },
        {
            "id": "filter_summary_table",
            "title": "十项默认计算过滤及真实分母",
            "subtitle": "阈值来自本轮冻结配置和运行日志；过滤结论不是实验测量。",
            "dataset": "filter_summary",
            "source": sources["filter"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "filter_order", "direction": "asc"},
            "columns": [
                {"field": "filter_order", "label": "顺序"},
                {"field": "filter_label_cn", "label": "过滤条件", "type": "text"},
                {"field": "value_column", "label": "观测列", "type": "text"},
                {"field": "operator", "label": "运算符", "type": "text"},
                {"field": "threshold_display", "label": "阈值", "type": "text"},
                {"field": "unit", "label": "单位", "type": "text"},
                {"field": "candidate_count", "label": "候选分母"},
                {"field": "passed_count", "label": "通过"},
                {"field": "failed_count", "label": "失败"},
                {"field": "failure_rate", "label": "失败率"},
            ],
        },
        {
            "id": "candidate_table",
            "title": "骨架内去重候选的关键计算指标",
            "subtitle": "selected_by_budget 只是每骨架预算展示；pass_all_default_filters 才是十项计算过滤结论。",
            "dataset": "candidates",
            "source": sources["candidate"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "scaffold_selection_rank", "direction": "asc"},
            "columns": [
                {"field": "scaffold_selection_rank", "label": "骨架顺序"},
                {"field": "candidate_label", "label": "候选", "type": "text"},
                {"field": "scaffold_pdb_code", "label": "PDB", "type": "text"},
                {"field": "scaffold_role", "label": "角色", "type": "text"},
                {"field": "pass_all_default_filters", "label": "十项全通过", "type": "boolean"},
                {"field": "selected_by_budget", "label": "预算展示（非通过）", "type": "boolean"},
                {"field": "computed_filter_pass_count", "label": "通过项 /10"},
                {"field": "failed_filters_cn", "label": "失败条件", "type": "text"},
                {"field": "filter_rmsd_a", "label": "复合物 RMSD Å"},
                {"field": "filter_rmsd_design_a", "label": "设计区 RMSD Å"},
                {"field": "his7_min_heavy_atom_distance_a", "label": "His7 距离 Å"},
                {"field": "ala8_min_heavy_atom_distance_a", "label": "Ala8 距离 Å"},
                {"field": "prerefold_hotspot_coverage_fraction_lt8a", "label": "复折叠前 token-center 覆盖"},
                {"field": "independent_hotspot_coverage_heavy_lt8a", "label": "复折叠后独立重原子覆盖"},
                {"field": "design_to_target_iptm", "label": "设计区→目标 iPTM"},
                {"field": "min_design_to_target_pae_a", "label": "最小 PAE Å"},
                {"field": "liability_num_violations", "label": "可开发性规则违反"},
                {"field": "liability_high_severity_violations", "label": "高严重度违反"},
                {"field": "cdr3_sequence", "label": "CDR3 序列", "type": "text"},
                {"field": "source_refold_cif", "label": "复折叠结构真源", "type": "text"},
            ],
        },
        {
            "id": "filter_detail_table",
            "title": "逐候选、逐条件过滤明细",
            "subtitle": "每个候选固定 10 行；可核对观测值、运算符、阈值和通过状态。",
            "dataset": "filter_results",
            "source": sources["filter_detail"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "candidate_id", "direction": "asc"},
            "columns": [
                {"field": "candidate_id", "label": "候选 ID", "type": "text"},
                {"field": "pdb_code", "label": "PDB", "type": "text"},
                {"field": "filter_order", "label": "条件顺序"},
                {"field": "filter_label_cn", "label": "条件", "type": "text"},
                {"field": "observed_value", "label": "观测值"},
                {"field": "operator", "label": "运算符", "type": "text"},
                {"field": "threshold_display", "label": "阈值", "type": "text"},
                {"field": "unit", "label": "单位", "type": "text"},
                {"field": "passed", "label": "通过", "type": "boolean"},
            ],
        },
        {
            "id": "priority_table",
            "title": "人工复盘优先项（不是 binder 清单）",
            "subtitle": "只按通过条件数、界面置信代理和误差代理排序，不能解释为实验亲和力或结合概率。",
            "dataset": "priority_review",
            "source": sources["summary"],
            "density": "compact",
            "layout": "full",
            "defaultSort": {"field": "review_order", "direction": "asc"},
            "columns": [
                {"field": "review_order", "label": "复盘顺序"},
                {"field": "candidate_label", "label": "候选", "type": "text"},
                {"field": "passed_filter_count", "label": "通过项 /10"},
                {"field": "failed_filters_cn", "label": "失败条件", "type": "text"},
                {"field": "design_to_target_iptm", "label": "设计区→目标 iPTM"},
                {"field": "min_design_to_target_pae_a", "label": "最小 PAE Å"},
                {"field": "interpretation", "label": "正确解释", "type": "text"},
            ],
        },
        {
            "id": "validation_table",
            "title": "报告生成前的阻断性验证",
            "subtitle": "只有全部检查为 true，生成器才会写出 artifact。",
            "dataset": "validation_checks",
            "source": sources["validation"],
            "density": "compact",
            "layout": "full",
            "columns": [
                {"field": "check", "label": "检查项", "type": "text"},
                {"field": "passed", "label": "通过", "type": "boolean"},
            ],
        },
    ]


def make_blocks(
    *,
    survivors: int,
    candidates: int,
    budget_display: int,
    leading_failures: list[Mapping[str, Any]],
    known_limits: list[str],
) -> list[dict[str, Any]]:
    """按技术报告规范生成 answer-first 叙事，并为每张图配置相邻解释。"""

    if survivors:
        result_sentence = (
            f"{survivors}/{candidates} 条骨架内去重候选通过本轮十项默认计算过滤；"
            "这只是结构与序列代理门槛，尚不能作为实验命中结论。"
        )
    else:
        result_sentence = (
            f"{candidates} 条骨架内去重候选中，0 条通过全部十项默认计算过滤。"
            "工程链路完成与候选质量通过必须分开表述。"
        )

    failure_bullets = []
    for row in leading_failures:
        failure_bullets.append(
            f"- **{row['filter']}**：{as_int(row['failed_count'], 'leading_failure.failed_count')}/"
            f"{as_int(row['candidate_count'], 'leading_failure.candidate_count')} 条失败"
            f"（{as_float(row['failure_rate'], 'leading_failure.failure_rate'):.1%}）。"
        )
    failure_text = "\n".join(failure_bullets) or "- run_summary 未列出领先失败模式。"
    limits_text = "\n".join(f"- {item}" for item in known_limits)

    return [
        {
            "id": "title",
            "type": "markdown",
            "body": "# BoltzGen 旧 12 条 VHH 骨架 × GLP-1(7–36)：第一轮技术报告",
        },
        {
            "id": "technical_summary",
            "type": "markdown",
            "sourceId": "run_summary_source",
            "body": (
                "## 技术摘要\n\n"
                f"**{result_sentence}** 12 个冻结骨架任务、24 个原始设计链路均在本轮预训练模型推理中完成。"
                f"每骨架预算目录保留 1 条，共 {budget_display} 条预算展示项；这些条目不因进入目录而变成过滤通过项。\n\n"
                "本轮只输入一个 GLP-1(7–36) 正靶几何，未运行 GLP-1(9–36) 或多构象反筛，且 C 端酰胺没有完成原子级闭环验证。"
                "因此报告只描述工程完成性、结构自洽性、热点几何、模型置信代理与序列规则；不报告实验解离常数，也不作型态选择性结论。"
            ),
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": [
                "scaffold_card",
                "raw_card",
                "candidate_card",
                "survivor_card",
                "budget_card",
                "runtime_card",
            ],
        },
        {
            "id": "key_findings",
            "type": "markdown",
            "sourceId": "run_summary_source",
            "body": (
                "## 关键发现与图证\n\n"
                f"- **工程产出完整**：12/12 骨架任务完成，24/24 原始设计、逆折叠与复折叠产物链路齐全。\n"
                f"- **候选过滤结论**：{result_sentence}\n"
                f"- **预算展示不等于通过**：{budget_display} 条预算展示项是每骨架固定展示预算，不是额外的通过候选。\n"
                "- **科学边界**：所有指标均为计算代理；单一正靶和未闭环端酰胺不足以评价型态选择性。"
            ),
        },
        {
            "id": "funnel_chart_block",
            "type": "chart",
            "chartId": "process_funnel_chart",
        },
        {
            "id": "funnel_interpretation",
            "type": "markdown",
            "sourceId": "process_funnel_source",
            "body": (
                "**如何读图：**“通过全部默认计算过滤”和“预算目录展示候选”是两条并列统计。"
                "前者回答质量门槛，后者回答每个骨架保留多少条用于查看；后者不能回填为成功数。"
            ),
        },
        {
            "id": "scope_heading",
            "type": "markdown",
            "body": (
                "## 范围、数据与定义\n\n"
                "分析总体是冻结的 12 条 SAbDab2 单域重链骨架；每骨架请求 2 条设计。"
                "候选过滤分母使用骨架内去重后的行数，而不是无条件使用 24。"
            ),
        },
        {"id": "scope_table_block", "type": "table", "tableId": "scope_table", "layout": "full"},
        {"id": "registry_table_block", "type": "table", "tableId": "scaffold_registry_table", "layout": "full"},
        {
            "id": "methodology",
            "type": "markdown",
            "sourceId": "input_manifest_source",
            "body": (
                "## 模型与方法\n\n"
                "本轮不训练权重，而是依次运行五步预训练推理：\n\n"
                "1. **结构生成（design）**：以固定 VHH 框架、可设计的互补决定区掩码、GLP-1 正靶坐标和 His7/Ala8 提示位点为条件，通过扩散采样产生候选复合物几何。\n"
                "2. **逆折叠（inverse folding）**：在候选主链几何上为可设计位置采样氨基酸序列；掩码外框架序列必须保持不变。\n"
                "3. **复折叠（folding）**：用候选序列和目标重新预测复合物，检查生成几何与序列支持的几何是否自洽。\n"
                "4. **指标分析（analysis）**：分别保留逆折叠后、复折叠前结构上的 BoltzGen token-center 提示位点覆盖，"
                "以及从复折叠后 refold CIF 独立重算的 His7/Ala8 重原子/Cα 距离；同时计算复合物/设计区均方根偏差、界面置信代理、预测对齐误差代理、溶剂可接触面积变化和序列可开发性规则。\n"
                "5. **过滤排序（filtering）**：逐候选应用本轮冻结的十项计算条件，并在每个骨架内部排序和选择预算展示项。\n\n"
                "配置为 design 50 步、inverse folding 30 步、folding 50 步、每候选 1 个复折叠样本、1 次 recycling、FP32、batch size 1。"
                "这些是首轮快速筛查设置；本轮没有新的训练损失函数，也没有更新基础模型参数。\n\n"
                "**方法与输入来源：**"
                "[BoltzGen v0.3.2 源码](https://github.com/HannesStark/boltzgen/tree/v0.3.2)、"
                "[BoltzGen 预印本](https://www.biorxiv.org/content/10.1101/2025.11.20.689494v2)、"
                "[PDB 6X18](https://www.rcsb.org/structure/6X18)、"
                "[SAbDab2-nano](https://sabdab.opig.stats.ox.ac.uk/search-nanobodies)。"
                "本目录同时保存实际输入、代码快照和逐文件哈希，外部链接用于核对原始方法与数据条目。"
            ),
        },
        {"id": "stage_chart_block", "type": "chart", "chartId": "stage_timing_chart"},
        {
            "id": "stage_interpretation",
            "type": "markdown",
            "sourceId": "stage_timings_source",
            "body": (
                "**如何读图：**柱高是 12 个骨架在同一步骤的累计模型时间。"
                "它用于容量规划；不应与 execute 墙钟卡片逐项强行相等，因为 wrapper、进程启动和文件处理不在该汇总中。"
            ),
        },
        {"id": "run_manifest_block", "type": "table", "tableId": "run_manifest_table", "layout": "full"},
        {
            "id": "scaffold_results_heading",
            "type": "markdown",
            "body": (
                "## 12 条骨架结果\n\n"
                "下面先按骨架汇总，再进入逐候选表。每个骨架只有 2 条请求，样本过小，"
                "所以这些结果不能用来估计框架的真实命中率，也不能据此推断 PRIMARY 与 RESERVE 的因果差异。"
            ),
        },
        {"id": "scaffold_chart_block", "type": "chart", "chartId": "scaffold_filter_chart"},
        {
            "id": "scaffold_chart_interpretation",
            "type": "markdown",
            "sourceId": "per_scaffold_source",
            "body": (
                "**如何读图：**每根柱只取该骨架候选中通过条件最多的一条，满分为 10。"
                "柱高可帮助定位要复盘的框架与候选，但不是跨骨架归一化质量分数。"
            ),
        },
        {"id": "scaffold_results_table_block", "type": "table", "tableId": "scaffold_results_table", "layout": "full"},
        {
            "id": "filters_heading",
            "type": "markdown",
            "body": "## 过滤、候选与失败模式\n\n领先失败模式由实际过滤汇总表计算：\n\n" + failure_text,
        },
        {"id": "filter_chart_block", "type": "chart", "chartId": "filter_failure_chart"},
        {
            "id": "filter_chart_interpretation",
            "type": "markdown",
            "sourceId": "filter_summary_source",
            "body": (
                "**如何读图：**同一候选可能同时在多个条件失败，因此各柱不能相加为独立淘汰人数。"
                "失败率的分母始终是骨架内去重候选数。"
            ),
        },
        {"id": "filter_table_block", "type": "table", "tableId": "filter_summary_table", "layout": "full"},
        {"id": "rmsd_chart_block", "type": "chart", "chartId": "rmsd_scatter_chart"},
        {
            "id": "rmsd_interpretation",
            "type": "markdown",
            "sourceId": "candidate_metrics_source",
            "body": (
                "**如何读图：**左下象限表示两个均方根偏差都不超过 2.5 Å。"
                "这里比较的是生成结构与复折叠结构之间的自洽性，不是候选相对未知实验复合物的结构精度。"
            ),
        },
        {"id": "hotspot_stage_chart_block", "type": "chart", "chartId": "hotspot_stage_chart"},
        {
            "id": "hotspot_stage_interpretation",
            "type": "markdown",
            "sourceId": "candidate_metrics_source",
            "body": (
                "**两个阶段不能混用：**“复折叠前 token-center 覆盖”是 BoltzGen 在 analysis 过滤所用的 `bindsite_under_8rmsd` 语义；"
                "“复折叠后独立重原子覆盖”来自最终 refold CIF 的独立坐标计算。两者结构阶段和距离定义都不同，"
                "所以只用于检查信号是否在复折叠后仍可观察，不要求逐候选相等，也不能用一个字段替代另一个。"
            ),
        },
        {"id": "hotspot_chart_block", "type": "chart", "chartId": "hotspot_distance_chart"},
        {
            "id": "hotspot_interpretation",
            "type": "markdown",
            "sourceId": "candidate_metrics_source",
            "body": (
                "**如何读图：**这张图只使用复折叠后 refold CIF。左下象限表示候选设计区与 His7、Ala8 两个提示位点的独立最小重原子距离都小于 8 Å。"
                "这是局部几何覆盖，不等于形成特定相互作用，更不能单独证明实验结合。"
            ),
        },
        {"id": "candidate_table_block", "type": "table", "tableId": "candidate_table", "layout": "full"},
        {"id": "priority_table_block", "type": "table", "tableId": "priority_table", "layout": "full"},
        {"id": "filter_detail_block", "type": "table", "tableId": "filter_detail_table", "layout": "full"},
        {
            "id": "limitations",
            "type": "markdown",
            "sourceId": "validation_report_source",
            "body": (
                "## 限制、不确定性与稳健性\n\n"
                + limits_text
                + "\n- 每骨架仅 2 条请求，不能估计候选生成成功率，也不足以比较骨架优劣。"
                "\n- Metal Performance Shaders 代码来自未合并的实验性快照，不能视为官方 Linux + NVIDIA 基线。"
                "\n- 资源监控从第 2 个骨架中途开始，进程树 RSS 也不是对统一内存的独立测量。"
                "\n- 稳健性目前只覆盖单次低预算推理；没有统一随机种子、重复批次、更多复折叠样本、反靶或多构象重预测。"
            ),
        },
        {"id": "validation_table_block", "type": "table", "tableId": "validation_table", "layout": "full"},
        {
            "id": "retrospective",
            "type": "markdown",
            "body": (
                "## 复盘\n\n"
                "**本轮证明了什么：**冻结的 12 条骨架可以按统一配置顺序运行；24 条候选均可追溯到配置、生成结构、逆折叠、复折叠和指标表，"
                "12 条预算展示项另有最终复制结构；框架序列在设计掩码外保持不变。由于每个候选仅生成 1 个复折叠样本，分析与写出选择同一索引只证明文件血缘一致，不构成独立的一致性证据。\n\n"
                "**本轮没有证明什么：**未证明候选在体外结合 GLP-1，未测量实验亲和力、回收率、表达量或稳定性，也未比较 7–36 与 9–36。"
                "预算展示目录是审阅入口，不是命中目录。\n\n"
                "**流程改进点：**下一轮应把更多计算投入到采样重复、复折叠样本、目标多构象与反靶几何，而不是通过放宽阈值把失败候选包装为命中。"
            ),
        },
        {
            "id": "next_steps",
            "type": "markdown",
            "body": (
                "## 下一步\n\n"
                "1. 在官方支持的 Linux + NVIDIA 环境复现同一冻结输入，恢复更充分的结构采样、recycling 和每候选多复折叠样本。\n"
                "2. 在不改变默认门槛的前提下扩大每骨架候选数，并保留独立重复批次，用于评估排序稳定性。\n"
                "3. 原子级闭环验证 GLP-1(7–36) C 端酰胺，再建立匹配的 GLP-1(9–36) 与多构象反筛集合。\n"
                "4. 对通过计算门槛的候选继续做聚集倾向、表达、热稳定性和非特异性等可开发性检查。\n"
                "5. 只有计算门槛和人工结构复核均合格后才进入表达；实验阶段再用表面等离子体共振或生物层干涉法、混合样本捕获液相色谱–质谱建立真实标签。"
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## 进一步问题\n\n"
                "- 增加扩散采样和复折叠样本后，两个均方根偏差门槛与 His7/Ala8 几何覆盖是否在重复批次中稳定？\n"
                "- 失败模式是由某些框架几何可达性主导，还是主要来自低预算采样？\n"
                "- 显式建模并验证端酰胺后，候选排序和界面几何是否发生系统性变化？\n"
                "- 在同一计算设置下加入 GLP-1(9–36) 与多构象反筛后，哪些候选仍保留正靶结构优势？\n"
                "- 计算代理与后续表达、稳定性、捕获回收率和实验结合数据之间的相关性是否足以支持下一轮模型或排序器校准？"
            ),
        },
    ]


def main() -> int:
    """加载真实分析产物、验证一致性并原子写入 canonical artifact。"""

    # 缺少任何分析文件时立即停止；此步骤不会创建 report/ 或占位 JSON。
    assert_required_files()
    assert_query_columns()

    manifest = require_mapping(read_json(PROVENANCE_ROOT / "input_manifest.json"), "input_manifest")
    validation = require_mapping(read_json(ANALYSIS_ROOT / "validation_report.json"), "validation_report")
    summary = require_mapping(read_json(ANALYSIS_ROOT / "run_summary.json"), "run_summary")

    # 规范表先全部类型化，再执行跨表一致性验证。
    candidates = [normalize_candidate(row) for row in read_delimited(ANALYSIS_ROOT / "candidate_metrics.csv")]
    candidates.sort(key=lambda row: (row["scaffold_selection_rank"], row["final_rank_within_scaffold"], row["candidate_id"]))
    filter_summary = [normalize_filter_summary(row) for row in read_delimited(ANALYSIS_ROOT / "filter_summary.csv")]
    filter_summary.sort(key=lambda row: row["filter_order"])
    filter_results = [normalize_filter_result(row) for row in read_delimited(ANALYSIS_ROOT / "candidate_filter_results.csv")]
    filter_results.sort(key=lambda row: (row["pdb_code"], row["candidate_id"], row["filter_order"]))
    funnel = [normalize_funnel(row) for row in read_delimited(ANALYSIS_ROOT / "process_funnel.csv")]
    funnel.sort(key=lambda row: row["order"])
    scaffold_results = [normalize_per_scaffold(row) for row in read_delimited(ANALYSIS_ROOT / "per_scaffold_summary.csv")]
    scaffold_results.sort(key=lambda row: row["scaffold_selection_rank"])
    runs = [normalize_run(row) for row in read_delimited(ANALYSIS_ROOT / "run_manifest.csv")]
    runs.sort(key=lambda row: row["selection_rank"])
    scaffolds = normalize_scaffold_registry(manifest)
    stage_totals = build_stage_totals(read_delimited(ANALYSIS_ROOT / "stage_timings.csv"))
    validation_rows = build_validation_rows(validation)
    priority_rows = build_priority_rows(summary, {row["candidate_id"] for row in candidates})

    validate_consistency(
        manifest,
        validation,
        summary,
        candidates,
        filter_summary,
        filter_results,
        funnel,
        scaffolds,
        scaffold_results,
        runs,
        stage_totals,
    )

    engineering = require_mapping(summary["engineering_status"], "run_summary.engineering_status")
    compute = require_mapping(summary["compute"], "run_summary.compute")
    survivor_count = as_int(engineering["default_filter_survivors"], "engineering.default_filter_survivors")
    candidate_count = as_int(engineering["ranked_unique_candidates"], "engineering.ranked_unique_candidates")
    budget_count = as_int(engineering["budget_display_candidates"], "engineering.budget_display_candidates")
    headline = [
        {
            "scaffolds_complete": as_int(engineering["scaffold_tasks_complete"], "engineering.scaffold_tasks_complete"),
            "raw_design_pairs": as_int(engineering["raw_design_pairs_complete"], "engineering.raw_design_pairs_complete"),
            "ranked_unique_candidates": candidate_count,
            "default_filter_survivors": survivor_count,
            "budget_display_candidates": budget_count,
            "sum_execute_seconds": round_or_none(as_float(compute["sum_execute_seconds"], "compute.sum_execute_seconds"), 3),
        }
    ]

    # 漏斗图只显示 candidate 单位；样本/骨架单位仍保留在完整 funnel 数据集中。
    funnel_candidates = [row for row in funnel if row["unit"] == "candidate"]
    # 同一候选的两个提示位点覆盖值来自不同结构阶段和不同距离定义；转换成长表后
    # 并列展示，但绝不把两列相互替代或据此要求数值相等。
    hotspot_stage_coverage: list[dict[str, Any]] = []
    for candidate in candidates:
        hotspot_stage_coverage.extend(
            [
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_label": candidate["candidate_label"],
                    "scaffold_pdb_code": candidate["scaffold_pdb_code"],
                    "stage_order": 1,
                    "stage_definition": "复折叠前 token-center",
                    "coverage_fraction_lt8a": candidate["prerefold_hotspot_coverage_fraction_lt8a"],
                },
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_label": candidate["candidate_label"],
                    "scaffold_pdb_code": candidate["scaffold_pdb_code"],
                    "stage_order": 2,
                    "stage_definition": "复折叠后重原子",
                    "coverage_fraction_lt8a": candidate["independent_hotspot_coverage_heavy_lt8a"],
                },
            ]
        )
    # 失败图按失败数降序，平手时按冻结过滤顺序，避免视觉顺序随 CSV 写出变化。
    filter_summary_by_failure = sorted(
        filter_summary,
        key=lambda row: (-row["failed_count"], row["filter_order"]),
    )
    scope_rows = build_scope_rows(manifest)
    sources = source_definitions()

    leading_failures_raw = require_list(summary.get("leading_failure_modes"), "run_summary.leading_failure_modes")
    leading_failures = [require_mapping(row, "run_summary.leading_failure_modes[]") for row in leading_failures_raw]
    known_limits = [str(item) for item in require_list(manifest.get("known_limits"), "input_manifest.known_limits")]

    generated_at = datetime.now(timezone.utc).isoformat()
    title = "BoltzGen 旧 12 条 VHH 骨架 × GLP-1(7–36)：第一轮技术报告"
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "冻结旧 12 条 VHH 骨架在单一 GLP-1(7–36) 正靶上的预训练推理、规范分析、默认计算过滤、限制与下一步。",
            "generatedAt": generated_at,
            "cards": make_cards(sources["summary"]),
            "charts": make_charts(sources),
            "tables": make_tables(sources),
            "sources": list(sources.values()),
            "blocks": make_blocks(
                survivors=survivor_count,
                candidates=candidate_count,
                budget_display=budget_count,
                leading_failures=leading_failures,
                known_limits=known_limits,
            ),
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "headline": headline,
                "scope_definitions": scope_rows,
                "scaffold_registry": scaffolds,
                "runs": runs,
                "stage_totals": stage_totals,
                "funnel": funnel,
                "funnel_candidates": funnel_candidates,
                "filter_summary": filter_summary,
                "filter_summary_by_failure": filter_summary_by_failure,
                "scaffold_results": scaffold_results,
                "candidates": candidates,
                "hotspot_stage_coverage": hotspot_stage_coverage,
                "filter_results": filter_results,
                "priority_review": priority_rows,
                "validation_checks": validation_rows,
            },
        },
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

    # 只有上面的所有验证都完成后才创建目录并原子替换报告 JSON。
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(OUTPUT_PATH)
    print(
        json.dumps(
            {
                "artifact": relative(OUTPUT_PATH),
                "scaffolds": len(scaffolds),
                "raw_design_pairs": headline[0]["raw_design_pairs"],
                "ranked_unique_candidates": candidate_count,
                "default_filter_survivors": survivor_count,
                "budget_display_candidates_not_passes": budget_count,
                "validation": validation["assessment"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
