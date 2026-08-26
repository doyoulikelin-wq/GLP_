#!/usr/bin/env python3
"""把筛选数据库结果转换为 Data Analytics portable report artifact。

脚本只负责从已审计的 SQLite/JSON 生成 ``artifact.json``；真正的自包含 HTML
由官方 ``report:deliver`` 构建器生成。这样图表、表格、来源弹窗、窄屏布局和
无脚本语义后备都走同一套经过校验的运行时，而不是另写一套 HTML 图表代码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd


TITLE = "SAbDab2 VHH 骨架筛选与数据库统计"
SQLITE_SOURCE_PATH = "registry/scaffold_database.sqlite"


# 报告快照和来源弹窗共用下列 SQL。这样 source.query.sql 不是对 TSV
# 处理过程的事后描述，而是实际生成图表/表格行的可执行 SQLite 查询。
SCREENING_FUNNEL_SQL = """
SELECT
    stage_order,
    CASE stage_order
        WHEN 1 THEN 'SAbDab2 SD-H 原始实例'
        WHEN 2 THEN '具有骆驼科来源证据的 VHH'
        WHEN 3 THEN 'X-ray 结构'
        WHEN 4 THEN '分辨率 ≤ 2.5 Å'
        WHEN 5 THEN '进入结构质量控制（QC）'
        WHEN 6 THEN '通过结构质量控制（QC）'
        WHEN 7 THEN '每个 SAbDab ID 保留最佳实例'
        WHEN 8 THEN '去除完全相同的框架'
        WHEN 9 THEN '框架结构簇代表'
        WHEN 10 THEN '最终入选主用与备用骨架'
        ELSE stage
    END AS stage,
    remaining_count
FROM screening_funnel
ORDER BY stage_order ASC;
""".strip()

EXCLUSION_REASON_SQL = """
SELECT
    reason_order,
    reason,
    excluded_count
FROM exclusion_reason_summary
ORDER BY reason_order ASC;
""".strip()

RESOLUTION_VALUES_SQL = """
SELECT
    candidate_id,
    pdb_code,
    sabdab_id,
    resolution_a,
    r_free,
    hard_status,
    quality_score,
    soft_flag_count,
    variable_length_aa,
    cdr3_length_aa,
    'XRAY' AS method
FROM scaffold_candidate
WHERE resolution_a IS NOT NULL
ORDER BY resolution_a ASC, candidate_id ASC;
""".strip()

CLUSTER_SIZES_SQL = """
SELECT
    cluster_id,
    COUNT(*) AS cluster_size,
    COALESCE(
        MAX(CASE WHEN is_cluster_representative = 1 THEN candidate_id END),
        MIN(candidate_id)
    ) AS representative
FROM cluster_member
GROUP BY cluster_id
ORDER BY cluster_size DESC, cluster_id ASC
LIMIT 20;
""".strip()

SELECTED_SCAFFOLDS_SQL = """
SELECT
    s.selection_rank,
    s.role,
    s.candidate_id,
    s.pdb_code,
    s.sabdab_id,
    s.source_hchain,
    s.heavy_species,
    s.method,
    s.resolution_a,
    s.r_free,
    s.variable_length_aa,
    s.cdr1_length_aa,
    s.cdr2_length_aa,
    s.cdr3_length_aa,
    s.quality_score,
    s.selection_utility,
    s.min_framework_sequence_distance,
    s.min_anchor_geometry_distance_scaled,
    s.framework_cluster_id,
    s.soft_flag_count,
    CASE
        WHEN s.canonical_disulfide_rcsb_crosschecked = 1 THEN '是'
        ELSE '否'
    END AS canonical_disulfide_rcsb_crosschecked,
    s.benchmark_7xl0,
    s.boltzgen_check_status,
    e.target_residue_count,
    e.target_role,
    CASE
        WHEN LOWER(CAST(e.terminal_amide_atomically_verified AS TEXT)) IN ('true','1','yes') THEN '是'
        ELSE '否'
    END AS terminal_amide_atomically_verified,
    s.package_path,
    s.selection_interpretation
FROM selection_member AS s
LEFT JOIN export_artifact AS e ON e.candidate_id = s.candidate_id
ORDER BY s.selection_rank ASC;
""".strip()

DATABASE_INVENTORY_SQL = """
SELECT 1 AS table_order, 'antibody_instance' AS table_name, COUNT(*) AS row_count,
       '一行 = 一个 SAbDab2 SD-H antibody instance' AS row_grain FROM antibody_instance
UNION ALL SELECT 2, 'scaffold_candidate', COUNT(*),
       '一行 = 一个进入结构 QC 的候选实例' FROM scaffold_candidate
UNION ALL SELECT 3, 'residue_map', COUNT(*),
       '一行 = 一个候选中的一个保留残基' FROM residue_map
UNION ALL SELECT 4, 'structure_connection', COUNT(*),
       '一行 = 一个候选中的一条保留共价连接' FROM structure_connection
UNION ALL SELECT 5, 'qc_result', COUNT(*),
       '一行 = 一个候选的一项硬规则或软风险结果' FROM qc_result
UNION ALL SELECT 6, 'cluster_member', COUNT(*),
       '一行 = 一个去重 framework 在一个结构簇中的成员资格' FROM cluster_member
UNION ALL SELECT 7, 'selection_member', COUNT(*),
       '一行 = 一个最终 PRIMARY 或 RESERVE scaffold' FROM selection_member
UNION ALL SELECT 8, 'export_artifact', COUNT(*),
       '一行 = 一个入选 scaffold 的导出文件与 BoltzGen check 状态' FROM export_artifact
UNION ALL SELECT 9, 'exclusion_log', COUNT(*),
       '一行 = 一个原始 instance 的首个排除原因' FROM exclusion_log
UNION ALL SELECT 10, 'screening_funnel', COUNT(*),
       '一行 = 一个严格嵌套筛选阶段' FROM screening_funnel
UNION ALL SELECT 11, 'exclusion_reason_summary', COUNT(*),
       '一行 = 一类首个排除原因的汇总' FROM exclusion_reason_summary
ORDER BY table_order ASC;
""".strip()


def json_rows(frame: pd.DataFrame) -> list[dict]:
    """把 NaN 转成 JSON null，并返回普通 Python 行对象。"""

    return json.loads(frame.to_json(orient="records", force_ascii=False))


def sqlite_source(
    *,
    source_id: str,
    label: str,
    sql: str,
    description: str,
    tables_used: list[str],
) -> dict:
    """构造符合 build-report v0.2.8 的可审计 SQLite 来源对象。"""

    return {
        "id": source_id,
        "label": label,
        "path": SQLITE_SOURCE_PATH,
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "sql": sql,
            "description": description,
            "tables_used": tables_used,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()

    summary = json.loads((root / "registry" / "database_summary.json").read_text(encoding="utf-8"))
    database_path = root / SQLITE_SOURCE_PATH
    if not database_path.is_file():
        raise FileNotFoundError(f"缺少筛选数据库：{database_path}")

    # 直接从交付数据库执行与来源弹窗完全一致的 SQL，避免快照数据来自 TSV、
    # 但 provenance 却声称来自另一个查询的来源漂移。
    with sqlite3.connect(database_path) as connection:
        funnel = pd.read_sql_query(SCREENING_FUNNEL_SQL, connection)
        exclusions = pd.read_sql_query(EXCLUSION_REASON_SQL, connection)
        resolution_values = pd.read_sql_query(RESOLUTION_VALUES_SQL, connection)
        cluster_sizes = pd.read_sql_query(CLUSTER_SIZES_SQL, connection)
        selected = pd.read_sql_query(SELECTED_SCAFFOLDS_SQL, connection)
        database_inventory = pd.read_sql_query(DATABASE_INVENTORY_SQL, connection)

    counts = summary["counts"]
    selected_primary = int(counts["selected_primary"])
    selected_reserve = int(counts["selected_reserve"])
    hard_pass = int(counts["hard_qc_pass_instances"])
    metadata_pool = int(counts["metadata_qualified_instances"])
    boltzgen_pass = int(counts.get("boltzgen_check_pass", 0))
    boltzgen_total = int(counts.get("boltzgen_check_total", 0))

    sources = [
        {
            "id": "database_summary_source",
            "label": "Scaffold database reconciled summary",
            "path": "registry/database_summary.json",
        },
        {
            "id": "source_release",
            "label": "SAbDab2-nano SD-H bulk snapshot (CC BY 4.0)",
            "href": "https://sabdab.opig.stats.ox.ac.uk/search-nanobodies",
            "path": "raw_snapshot/sabdab_summary_all_sd_h.csv + sabdab_all_sd_h_structures.tgz",
        },
        sqlite_source(
            source_id="screening_funnel_source",
            label="Nested scaffold screening funnel",
            sql=SCREENING_FUNNEL_SQL,
            description="按阶段序号读取严格嵌套的累计保留数。",
            tables_used=["screening_funnel"],
        ),
        sqlite_source(
            source_id="exclusion_source",
            label="First exclusion reason per antibody instance",
            sql=EXCLUSION_REASON_SQL,
            description="读取每个 antibody instance 的首个排除原因汇总。",
            tables_used=["exclusion_reason_summary"],
        ),
        sqlite_source(
            source_id="candidate_source",
            label="Structure-profiled scaffold candidates",
            sql=RESOLUTION_VALUES_SQL,
            description="读取具有数值分辨率的 scaffold 候选及审计上下文。",
            tables_used=["scaffold_candidate"],
        ),
        sqlite_source(
            source_id="selected_source",
            label="Selected VHH scaffold panel",
            sql=SELECTED_SCAFFOLDS_SQL,
            description="按选择排名读取最终 PRIMARY/RESERVE scaffold 面板。",
            tables_used=["selection_member", "export_artifact"],
        ),
        sqlite_source(
            source_id="cluster_source",
            label="Framework sequence and geometry clusters",
            sql=CLUSTER_SIZES_SQL,
            description="统计成员最多的前 20 个 framework 结构簇及其代表。",
            tables_used=["cluster_member"],
        ),
        sqlite_source(
            source_id="database_inventory_source",
            label="SQLite table inventory and row grains",
            sql=DATABASE_INVENTORY_SQL,
            description="逐表读取交付数据库的行数，并给出每行所代表的数据粒度。",
            tables_used=[
                "antibody_instance", "scaffold_candidate", "residue_map",
                "structure_connection", "qc_result", "cluster_member",
                "selection_member", "export_artifact", "exclusion_log",
                "screening_funnel", "exclusion_reason_summary",
            ],
        ),
        {
            "id": "policy_source",
            "label": "Versioned scaffold screening policy",
            "path": "criteria/scaffold_screening_v1.json",
        },
    ]

    charts = [
        {
            "id": "screening_funnel_chart",
            "title": "VHH 骨架筛选各阶段保留数量",
            "subtitle": "严格嵌套的累计保留集；条越长，表示该阶段保留的实例越多。",
            "intent": "comparison",
            "type": "horizontalBar",
            "dataset": "screening_funnel",
            "sourceId": "screening_funnel_source",
            "encodings": {
                "x": {"field": "stage", "type": "ordinal", "label": "筛选阶段"},
                "y": {"field": "remaining_count", "type": "quantitative", "aggregate": "sum", "label": "保留实例数", "unit": "条"},
                "tooltip": [
                    {"field": "stage_order", "type": "quantitative", "label": "阶段序号"},
                    {"field": "remaining_count", "type": "quantitative", "label": "保留数"},
                ],
            },
            "valueFormat": "number",
            "unit": "条",
            "layout": "full",
            "settings": {"showValues": True, "categoryLabelPolicy": "wrap"},
        },
        {
            "id": "exclusion_reason_chart",
            "title": "首个排除原因分布",
            "subtitle": "范围排除与结构失败分开命名；每个原始实例只计入一项。",
            "intent": "comparison",
            "type": "horizontalBar",
            "dataset": "exclusion_reasons",
            "sourceId": "exclusion_source",
            "encodings": {
                "x": {"field": "reason", "type": "nominal", "label": "首个排除原因"},
                "y": {"field": "excluded_count", "type": "quantitative", "aggregate": "sum", "label": "实例数", "unit": "条"},
            },
            "valueFormat": "number",
            "unit": "条",
            "layout": "full",
            "settings": {"sort": "descending", "showValues": True, "categoryLabelPolicy": "wrap"},
        },
        {
            "id": "resolution_histogram",
            "title": "进入结构 QC 的 X-ray 分辨率分布",
            "subtitle": f"metadata-qualified pool，n={metadata_pool}；单位 Å，数值越小通常细节越清楚。",
            "intent": "distribution",
            "type": "histogram",
            "dataset": "resolution_values",
            "sourceId": "candidate_source",
            "encodings": {
                "x": {"field": "resolution_a", "type": "quantitative", "label": "分辨率", "unit": "Å"},
                "y": {"field": "resolution_a", "type": "quantitative", "aggregate": "none", "label": "分辨率", "unit": "Å"},
                "tooltip": [
                    {"field": "candidate_id", "type": "text", "label": "INSTANCE"},
                    {"field": "hard_status", "type": "text", "label": "硬 QC"},
                    {"field": "quality_score", "type": "quantitative", "label": "项目内质量分数"},
                ],
            },
            "valueFormat": "number",
            "unit": "Å",
            "layout": "full",
            "settings": {"bins": 12},
        },
        {
            "id": "cluster_size_chart",
            "title": "最大的 framework 结构簇",
            "subtitle": "显示前20个簇；簇大小反映数据库冗余，不等于框架更优。",
            "intent": "ranking",
            "type": "horizontalBar",
            "dataset": "cluster_sizes",
            "sourceId": "cluster_source",
            "encodings": {
                "x": {"field": "cluster_id", "type": "nominal", "label": "框架簇"},
                "y": {"field": "cluster_size", "type": "quantitative", "aggregate": "sum", "label": "成员数", "unit": "条"},
                "tooltip": [{"field": "representative", "type": "text", "label": "示例成员"}],
            },
            "valueFormat": "number",
            "unit": "条",
            "layout": "full",
            "settings": {"sort": "descending", "showValues": True},
        },
    ]

    tables = [
        {
            "id": "selected_scaffold_table",
            "title": "最终入选 scaffold 面板",
            "subtitle": "一行对应一个结构包；PRIMARY 用于首轮并行生成，RESERVE 用作替补。",
            "dataset": "selected_scaffolds",
            "sourceId": "selected_source",
            "density": "spacious",
            "layout": "full",
            "defaultSort": {"field": "selection_rank", "direction": "asc"},
            "columns": [
                {"field": "selection_rank", "label": "排序", "type": "number"},
                {"field": "role", "label": "角色", "type": "text"},
                {"field": "pdb_code", "label": "PDB", "type": "text"},
                {"field": "source_hchain", "label": "源链", "type": "text"},
                {"field": "sabdab_id", "label": "SAbDab ID", "type": "text"},
                {"field": "heavy_species", "label": "来源物种", "type": "text"},
                {"field": "resolution_a", "label": "分辨率", "type": "number", "unit": "Å"},
                {"field": "variable_length_aa", "label": "VHH长度", "type": "number", "unit": "aa"},
                {"field": "cdr3_length_aa", "label": "CDR3长度", "type": "number", "unit": "aa"},
                {"field": "quality_score", "label": "项目内质量分数", "type": "number"},
                {"field": "framework_cluster_id", "label": "框架簇", "type": "text"},
                {"field": "canonical_disulfide_rcsb_crosschecked", "label": "二硫键交叉核对", "type": "text"},
                {"field": "boltzgen_check_status", "label": "BoltzGen check", "type": "text"},
                {"field": "target_role", "label": "Target角色", "type": "text"},
                {"field": "terminal_amide_atomically_verified", "label": "C端酰胺原子级验证", "type": "text"},
                {"field": "package_path", "label": "结构包", "type": "text"},
            ],
        }
    ]

    tables.append(
        {
            "id": "database_inventory_table",
            "title": "SQLite 数据表、行数与行粒度",
            "subtitle": "行数说明数据库规模；行粒度说明每一行究竟代表什么。",
            "dataset": "database_inventory",
            "sourceId": "database_inventory_source",
            "density": "comfortable",
            "layout": "full",
            "defaultSort": {"field": "table_order", "direction": "asc"},
            "columns": [
                {"field": "table_order", "label": "序号", "type": "number"},
                {"field": "table_name", "label": "数据表", "type": "text"},
                {"field": "row_count", "label": "行数", "type": "number"},
                {"field": "row_grain", "label": "每一行代表什么", "type": "text"},
            ],
        }
    )

    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {TITLE}"},
        {
            "id": "technical_summary",
            "type": "markdown",
            "sourceId": "database_summary_source",
            "body": (
                "## 技术摘要\n\n"
                f"本次冻结 **{counts['raw_instances']:,}** 个 SAbDab2 SD‑H antibody instances，"
                f"其中 **{metadata_pool:,}** 个进入首轮结构 QC，**{hard_pass:,}** 个通过硬规则。"
                f"去除同一抗体重复观测、完全相同框架并完成结构聚类后，最终建立 "
                f"**{selected_primary} 个主骨架 + {selected_reserve} 个备选骨架**；"
                f"其中 **{boltzgen_pass}/{boltzgen_total}** 已通过离线 BoltzGen 0.3.2 输入合同检查。\n\n"
                "入选只表示来源、IMGT映射、主链、规范二硫键和框架多样性可追溯；"
                "**不表示已经结合 GLP‑1，也不表示已经区分 7–36NH₂ 与 9–36NH₂。**"
            ),
        },
        {
            "id": "funnel_narrative",
            "type": "markdown",
            "sourceId": "screening_funnel_source",
            "body": (
                "## 数据准备先缩小范围，再判断结构是否可用\n\n"
                "前四步是项目范围控制：只保留 camelid-origin、X-ray 且分辨率不高于 2.5 Å 的 VHH。"
                "后续才是结构真实性检查、同一抗体最佳实例选择、框架去冗余与聚类。"
                "因此，早期被排除不等于原结构错误；它可能只是未进入本轮主面板。\n\n"
                "### QC 是什么\n\n"
                "**QC（Quality Control）就是“质量控制”**。这里不是给抗体打一个笼统的好坏分数，"
                "而是逐项检查结构能不能安全地作为生成模型的输入：\n\n"
                "- VHH 链、IMGT 编号和项目内残基映射是否唯一且一致；\n"
                "- 框架区以及准备重新设计的 CDR 区，N、CA、C、O 主链原子是否完整；\n"
                "- 相邻残基的 C–N 肽键距离是否位于 1.15–1.8 Å；\n"
                "- 每个保留残基的平均 occupancy（坐标占据率）是否至少为 0.5；\n"
                "- VHH 的规范 Cys23–Cys104 二硫键是否存在，SG–SG 距离是否位于 1.8–2.3 Å；\n"
                "- 是否存在零坐标、非标准残基、链断裂，或涉及设计区的额外二硫键。\n\n"
                "**通过 QC 只表示“结构输入合格”，不表示它已经能结合 GLP‑1，也不表示具有型态选择性。**"
            ),
        },
        {"id": "funnel_block", "type": "chart", "chartId": "screening_funnel_chart", "layout": "full"},
        {
            "id": "exclusion_narrative",
            "type": "markdown",
            "sourceId": "exclusion_source",
            "body": (
                "## 首个排除原因让全部原始行能够对账\n\n"
                "每个 INSTANCE 只记录最先触发的一项原因，避免同一条目被多个失败标签重复计数。"
                "完整软风险仍保存在 QC 长表中；本图只回答‘数据在哪一步离开主流程’。"
            ),
        },
        {"id": "exclusion_block", "type": "chart", "chartId": "exclusion_reason_chart", "layout": "full"},
        {
            "id": "resolution_narrative",
            "type": "markdown",
            "sourceId": "candidate_source",
            "body": (
                "## 分辨率用于控制起始坐标质量，不用于预测 GLP‑1 亲和力\n\n"
                "这里的 Å 是实验结构分辨率。较小数值通常意味着原子细节更清楚，但它不能替代主链完整性、"
                "二硫键和编号检查，也不能转换成 K_D、结合概率或选择性倍数。"
            ),
        },
        {"id": "resolution_block", "type": "chart", "chartId": "resolution_histogram", "layout": "full"},
        {
            "id": "cluster_narrative",
            "type": "markdown",
            "sourceId": "cluster_source",
            "body": (
                "## 聚类防止把许多近乎相同的框架误当成多样性\n\n"
                "聚类只使用 framework 的共同 IMGT 位置、Cα RMSD 与六个锚点几何。"
                "原抗原、原界面和是否为药物都不参与加分；簇越大只表示数据库中重复观测越多。"
            ),
        },
        {"id": "cluster_block", "type": "chart", "chartId": "cluster_size_chart", "layout": "full"},
        {
            "id": "selected_narrative",
            "type": "markdown",
            "sourceId": "selected_source",
            "body": (
                "## 最终面板平衡了结构质量与框架差异\n\n"
                "每个入选目录包含单链 scaffold.cif、固定 CDR 长度 scaffold.yaml、逐残基映射、"
                "清理说明和 QC JSON。7XL0 若通过会保留为当前 MVP 的连续性 benchmark；"
                "它被标记为 benchmark，不会被误写成质量最优。"
            ),
        },
        {"id": "selected_block", "type": "table", "tableId": "selected_scaffold_table", "layout": "full"},
        {
            "id": "database_inventory_narrative",
            "type": "markdown",
            "sourceId": "database_inventory_source",
            "body": (
                "## 数据库不是一张宽表，而是一组相互对账的规范化表\n\n"
                "原始实例、候选结构、逐残基映射、QC、聚类、选择和导出分别存表，"
                "避免把一个抗体、一个残基和一个筛选阶段混成同一种行。SQLite 对关键复合键建立唯一索引；"
                "TSV 是便于查看的镜像，SQLite 是查询与连接的主数据库。"
            ),
        },
        {"id": "database_inventory_block", "type": "table", "tableId": "database_inventory_table", "layout": "full"},
        {
            "id": "scope_definitions",
            "type": "markdown",
            "sourceId": "policy_source",
            "body": (
                "## 范围与关键定义\n\n"
                "- **原始粒度**：一行是一个 SAbDab2 antibody instance；同一 SAbDab ID 可以有多个结构观测。\n"
                "- **主面板 VHH**：SAbDab2 类型为 SD‑H，并有 camelid 物种或类群来源证据。人源或 synthetic SD‑H 保留在原始库，但不混入首轮主面板。\n"
                "- **硬 QC**：链映射与 IMGT 位置唯一、变量域端部和六个锚点存在、框架与待设计 CDR 主链完整、相邻 C–N 为 1.15–1.8 Å、残基平均 occupancy 不低于 0.5、Cys23–Cys104 SG 距离为 1.8–2.3 Å。\n"
                "- **软风险**：残基平均 occupancy 为 0.5–0.7、存在 altloc、序列责任基序或连接由几何重建；软风险用于排序但不伪装成实验失败。\n"
                "- **项目内质量分数**：只用于硬通过模板之间排序，不是概率，也没有通用生物学阈值。"
            ),
        },
        {
            "id": "methodology",
            "type": "markdown",
            "sourceId": "policy_source",
            "body": (
                "## 方法由版本化规则和逐残基证据共同约束\n\n"
                "流程为：冻结官方 CSV/tar.gz 与 SHA‑256 → 以 CSV Hchain/model 定位逻辑 VHH → "
                "按残基选择 altloc → 建立 auth/IMGT/label_seq 映射 → 检查主链、锚点与二硫键 → "
                "按 SAbDab ID 选最佳实例 → 折叠相同 framework → complete-linkage 聚类 → "
                "贪心选择质量与差异兼顾的 10+2 面板。\n\n"
                "输出 scaffold 统一使用 label_asym_id=A 与连续的 1-based label_seq_id；"
                "BoltzGen YAML 的 res_index 来自每个骨架自己的映射，绝不机械复制 7XL0 的编号。"
            ),
        },
        {
            "id": "limitations",
            "type": "markdown",
            "body": (
                "## 限制、未确定事项与稳健性边界\n\n"
                "当前筛选没有实验表达量、熔解温度、聚集、免疫原性或知识产权标签；"
                "序列可开发性只作为软风险，不能替代实测。SAbDab2 的处理文件不保留全部原始 _struct_conn，"
                "所以规范二硫键先由 SG 几何重建，再对最终代表用 RCSB 原始连接表做坐标交叉核对。\n\n"
                "离线 BoltzGen check 使用的是 6X18 派生的 30 残基 GLP‑1(7–36) 几何文件。"
                "该标准聚合物 CIF **没有完成 C 端酰胺 –CONH₂ 的原子级 round-trip 验证**；"
                "因此 check PASS 只证明 target/scaffold 输入合同能解析，不能证明末端酰胺已被模型读入。\n\n"
                "最重要的是：SAbDab2 原抗原不是 GLP‑1 训练标签，原抗原名称、原亲和力和原界面未进入评分。"
            ),
        },
        {
            "id": "next_steps",
            "type": "markdown",
            "body": (
                "## 下一步按相同预算比较骨架，而不是继续扩大数据库\n\n"
                "1. 为每个 PRIMARY 骨架分别运行相同数量、相同 seed 设计；不要把多个 scaffold 放入随机 path 列表。\n"
                "2. 对同一完整 VHH 序列分别重预测 7–36NH₂ 正靶和 9–36NH₂ 反靶的多构象集合。\n"
                "3. 用统一结构门槛、可开发性检查和失败原因比较各骨架的候选存活率。\n"
                "4. 用 SPR/BLI 和混合样本捕获 LC–MS 验证 K_D、动力学与真实选择性。"
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## 后续问题\n\n"
                "- 哪些框架在相同生成预算下更容易得到同时满足结构自洽与 N 端接触的候选？\n"
                "- 框架差异是否改变 CDR3 长度偏好、表达和非特异吸附？\n"
                "- 7–36NH₂ 末端化学在所用结构工具链中是否完成原子级 round-trip 验证？"
            ),
        },
    ]

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": TITLE,
            "description": "SAbDab2 SD-H 快照的 VHH scaffold 范围筛选、结构 QC、聚类、代表选择与可追溯数据库说明。",
            "sources": sources,
            "charts": charts,
            "tables": tables,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "status": "ready",
            "generatedAt": summary["generated_at"],
            "datasets": {
                "screening_funnel": json_rows(funnel),
                "exclusion_reasons": json_rows(exclusions),
                "resolution_values": json_rows(resolution_values),
                "cluster_sizes": json_rows(cluster_sizes),
                "selected_scaffolds": json_rows(selected),
                "database_inventory": json_rows(database_inventory),
            },
        },
    }
    output = root / "artifact_source" / "artifact.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
