#!/usr/bin/env python3
"""从会话/资源索引构建项目总览的 canonical report artifact。

这是一次性报告准备脚本。它读取仓库内的 CSV 和报告目录，输出：

* ``shared/resources/manifests/project_status_20260826.csv``；
* ``shared/reports/manifests/glp1_project_session_resource_overview_20260826.artifact.json``。

HTML 不在这里手写；必须再用 Data Analytics 的 portable artifact builder 打包，
从而让可见报告、来源弹窗和无脚本语义回退共享同一份已验证 payload。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GENERATED_AT = "2026-08-26T16:30:00Z"


def read_csv(path: Path) -> list[dict[str, str]]:
    """读取带或不带 UTF-8 BOM 的 CSV。"""

    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """写入供报告与人工审计共同使用的中间表。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def source(source_id: str, label: str, path: str, description: str, sql: str) -> dict[str, object]:
    """生成 portable report 使用的规范来源对象。"""

    return {
        "id": source_id,
        "label": label,
        "path": path,
        "description": description,
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": sql,
            "description": description,
            "tables_used": [path],
            "executed_at": GENERATED_AT,
        },
    }


def table(
    table_id: str,
    title: str,
    subtitle: str,
    dataset: str,
    source_object: dict[str, object],
    columns: list[tuple[str, str, str]],
    sort_field: str,
) -> dict[str, object]:
    """建立紧凑、可排序的报告原生表格定义。"""

    return {
        "id": table_id,
        "title": title,
        "subtitle": subtitle,
        "dataset": dataset,
        "source": source_object,
        "density": "compact",
        "layout": "full",
        "defaultSort": {"field": sort_field, "direction": "asc"},
        "columns": [
            {"field": field, "label": label, "type": column_type}
            for field, label, column_type in columns
        ],
    }


def main() -> int:
    session_path = ROOT / "shared/resources/manifests/session_index_20260826.csv"
    resource_path = ROOT / "shared/resources/manifests/all_resources_20260826.csv"
    sessions = read_csv(session_path)
    resources = read_csv(resource_path)

    status_rows: list[dict[str, object]] = [
        {
            "route": "BoltzGen / VHH",
            "decision": "工程主线",
            "available_input": "旧 12 个已检查 VHH 骨架 + GLP-1 数据资产",
            "latest_result": "0/2；0/24；0/48；深度探针 0/4 严格通过",
            "current_state": "Mac 工程闭环完成；Linux/NVIDIA 正式 campaign 未启动",
            "next_gate": "实现并运行 AIV1，按 10→240→2,400→12,000 门控",
        },
        {
            "route": "BindCraft / de novo miniprotein",
            "decision": "探索线",
            "available_input": "1 个原型 Notebook + 8 个 PDB + 1 个审计 Notebook",
            "latest_result": "12 个代码单元未执行；无候选、CSV、日志或实验结果",
            "current_state": "输入与评价合同 NEEDS_REVISION",
            "next_gate": "修复后按每个正构象 30–50 条轨迹小试",
        },
        {
            "route": "共享治理",
            "decision": "两线共同合同",
            "available_input": "8 个相关会话条目 + 11 个资源包索引",
            "latest_result": "代码、报告、小型数据与外置大资产边界已分离",
            "current_state": "报告与资源可审计；无项目整体许可证声明",
            "next_gate": "PR 审阅、CI 通过后合并；再保护 main",
        },
    ]
    status_path = ROOT / "shared/resources/manifests/project_status_20260826.csv"
    write_csv(status_path, status_rows)

    # 同一尝试内用相同候选分母比较“生成”和“严格通过”；深度探针保持独立行。
    attempt_rows = [
        {"attempt": "Mac MVP", "outcome": "生成候选", "candidate_count": 2, "date": "2026-08-19"},
        {"attempt": "Mac MVP", "outcome": "严格通过", "candidate_count": 0, "date": "2026-08-19"},
        {"attempt": "旧12第一轮", "outcome": "生成候选", "candidate_count": 24, "date": "2026-08-19"},
        {"attempt": "旧12第一轮", "outcome": "严格通过", "candidate_count": 0, "date": "2026-08-19"},
        {"attempt": "Mac增强主筛", "outcome": "生成候选", "candidate_count": 48, "date": "2026-08-20"},
        {"attempt": "Mac增强主筛", "outcome": "严格通过", "candidate_count": 0, "date": "2026-08-20"},
        {"attempt": "7XL0深度探针", "outcome": "生成候选", "candidate_count": 4, "date": "2026-08-20"},
        {"attempt": "7XL0深度探针", "outcome": "严格通过", "candidate_count": 0, "date": "2026-08-20"},
    ]
    attempt_path = ROOT / "boltzgen/resources/manifests/boltzgen_attempt_outcomes_20260826.csv"
    write_csv(attempt_path, attempt_rows)

    session_rows = [
        {
            "route": row["route"],
            "started": row["started_at_asia_shanghai"] or "未返回",
            "updated": row["updated_at_asia_shanghai"],
            "title": row["title"],
            "turns": row["turns"] or "未返回",
            "status": row["status"],
            "summary": row["content_summary"],
        }
        for row in sessions
    ]
    resource_rows = [
        {
            "route": row["route"],
            "purpose": row["purpose"],
            "asset_class": row["asset_class"],
            "record_count": row["record_count"],
            "size_bytes": int(row["size_bytes"] or 0),
            "git_policy": row["git_policy"],
            "validation_status": row["validation_status"],
            "repository_path": row["repository_path"] or "仅外置索引",
            "limitations": row["limitations"],
        }
        for row in resources
    ]
    naming_rows = [
        {"object_type": "执行入口/尝试包", "pattern": "<尝试内容>_<YYYYMMDD>", "location": "<route>/main/", "example": "round1_old12_mac_20260819", "reason": "一眼识别目的与时间"},
        {"object_type": "数据包", "pattern": "<用处>_<YYYYMMDD[_HHMMSS]>", "location": "<route>/resources/data/", "example": "GLP1选择性靶标面板_20260825", "reason": "避免 latest/final 漂移"},
        {"object_type": "一次性代码", "pattern": "<动作><对象>_<YYYYMMDD>.py", "location": "<route>/tools/one_off/", "example": "构建项目总览报告_20260826.py", "reason": "不混入正式流水线"},
        {"object_type": "HTML/Notebook", "pattern": "<主题>_<YYYYMMDD>.<ext>", "location": "reports/ 或 notebooks/", "example": "bindcraft_glp1_selectivity_input_audit_20260826.html", "reason": "报告版本可追溯"},
    ]
    report_rows = [
        {"route": "shared", "title": "GLP-1 AI 设计知识图谱", "date": "2026-08-19", "path": "shared/reports/html/glp1_ai_design_knowledge_graph_20260819.html", "claim": "共同概念、数据流和术语"},
        {"route": "boltzgen", "title": "BoltzGen MVP 数据资产", "date": "2026-08-18", "path": "boltzgen/reports/html/boltzgen_mvp_data_assets_20260818.html", "claim": "数据来源、格式、角色与样例"},
        {"route": "boltzgen", "title": "SAbDab2 VHH 骨架筛选", "date": "2026-08-19", "path": "boltzgen/reports/html/sabdab2_vhh_scaffold_screening_20260819.html", "claim": "筛选漏斗、质量控制和旧 12 骨架"},
        {"route": "boltzgen", "title": "BoltzGen 数据流与算法", "date": "2026-08-19", "path": "boltzgen/reports/html/boltzgen_vhh_glp1_algorithm_20260819.html", "claim": "输入、生成、逆折叠、复折叠与过滤"},
        {"route": "boltzgen", "title": "旧 12 骨架第一轮", "date": "2026-08-19", "path": "boltzgen/reports/html/boltzgen_old12_glp1_round1_20260819.html", "claim": "24/24 完成，0/24 严格通过"},
        {"route": "boltzgen", "title": "旧 12 骨架 Mac 增强", "date": "2026-08-20", "path": "boltzgen/reports/html/boltzgen_old12_glp1_mac_enhanced_20260820.html", "claim": "48 主候选，0/48 严格通过"},
        {"route": "bindcraft", "title": "BindCraft 输入与原型审计", "date": "2026-08-26", "path": "bindcraft/reports/html/bindcraft_glp1_selectivity_input_audit_20260826.html", "claim": "未执行；输入与评价合同需修订"},
    ]

    status_source = source(
        "status_source", "双路线状态表", "shared/resources/manifests/project_status_20260826.csv",
        "截至 2026-08-26 的路线决策、已有输入、最新结果、当前状态和下一门。",
        "SELECT * FROM read_csv_auto('shared/resources/manifests/project_status_20260826.csv', header=true)",
    )
    session_source = source(
        "session_source", "项目会话索引", "shared/resources/manifests/session_index_20260826.csv",
        "7 个直接相关任务和 1 个背景引用任务的时间、turn、状态、摘要和限制。",
        "SELECT * FROM read_csv_auto('shared/resources/manifests/session_index_20260826.csv', header=true)",
    )
    resource_source = source(
        "resource_source", "项目资源索引", "shared/resources/manifests/all_resources_20260826.csv",
        "BoltzGen 与 BindCraft 关键资源的来源、大小、Git 策略、验证状态和限制。",
        "SELECT * FROM read_csv_auto('shared/resources/manifests/all_resources_20260826.csv', header=true)",
    )
    policy_source = source(
        "policy_source", "仓库命名与数据政策", "CONTRIBUTING.md",
        "执行、数据、报告和一次性代码的目录与命名合同。",
        "SELECT 'CONTRIBUTING.md' AS document, 'repository naming and data policy' AS scope",
    )
    attempt_source = source(
        "attempt_source", "BoltzGen 历史尝试候选结果",
        "boltzgen/resources/manifests/boltzgen_attempt_outcomes_20260826.csv",
        "四个互不合并的历史尝试中生成候选数与严格通过数。",
        "SELECT * FROM read_csv_auto('boltzgen/resources/manifests/boltzgen_attempt_outcomes_20260826.csv', header=true)",
    )

    attempt_chart = {
        "id": "attempt_outcomes_chart",
        "title": "BoltzGen 历史尝试的候选数与严格通过数",
        "subtitle": "四个独立 Mac 统计域；严格通过均为 0，深度探针不并入 48 条主筛选分母。",
        "type": "bar",
        "intent": "comparison",
        "question": "每个历史尝试生成了多少候选，其中多少通过冻结的全部计算过滤？",
        "rationale": "分组条形图保留同一尝试内相同候选单位，同时明确零通过结果。",
        "dataset": "attempt_outcomes",
        "source": attempt_source,
        "palette": {"kind": "categorical"},
        "legend": {"position": "bottom", "title": "结果状态"},
        "labels": {"values": "all"},
        "encodings": {
            "x": {"field": "attempt", "type": "nominal", "label": "尝试"},
            "y": {"field": "candidate_count", "type": "quantitative", "label": "候选数"},
            "color": {"field": "outcome", "type": "nominal"},
        },
    }

    tables = [
        table("route_status_table", "两条路线不能用同一种成功定义", "工程完成、计算通过和实验验证分开记录。", "route_status", status_source,
              [("route", "路线", "text"), ("decision", "角色", "text"), ("available_input", "已有输入", "text"), ("latest_result", "最新结果", "text"), ("current_state", "当前状态", "text"), ("next_gate", "下一门", "text")], "route"),
        table("session_table", "会话覆盖与产物", "摘要而非原始逐字 transcript；保留状态和限制。", "sessions", session_source,
              [("route", "路线", "text"), ("started", "开始", "text"), ("updated", "更新", "text"), ("title", "会话", "text"), ("turns", "Turns", "text"), ("status", "状态", "text"), ("summary", "覆盖内容", "text")], "started"),
        table("resource_table", "资源与 Git 边界", "精确资源查找使用表格，不用面积或比例图。", "resources", resource_source,
              [("route", "路线", "text"), ("purpose", "用途", "text"), ("asset_class", "类别", "text"), ("record_count", "记录", "text"), ("size_bytes", "字节", "number"), ("git_policy", "Git 策略", "text"), ("validation_status", "验证状态", "text"), ("repository_path", "仓库位置", "text"), ("limitations", "限制", "text")], "route"),
        table("naming_table", "命名与目录合同", "真实路径使用下划线，不使用操作系统保留字符 *。", "naming", policy_source,
              [("object_type", "对象", "text"), ("pattern", "规则", "text"), ("location", "位置", "text"), ("example", "示例", "text"), ("reason", "原因", "text")], "object_type"),
        table("report_table", "HTML 报告索引", "所有报告均按路线和日期命名；大型原始数据不嵌入仓库。", "reports", policy_source,
              [("route", "路线", "text"), ("title", "报告", "text"), ("date", "日期", "text"), ("path", "仓库路径", "text"), ("claim", "可支持的结论", "text")], "route"),
    ]

    blocks = [
        {"id": "title", "type": "markdown", "body": "# GLP-1 AI 捕获蛋白项目：会话、进展与资源总览"},
        {"id": "summary", "type": "markdown", "sourceId": "status_source", "body": "## 技术摘要\n\n**BoltzGen/VHH 已完成数据、骨架库和三批 Mac 工程闭环，但严格过滤仍为 0/2、0/24、0/48（独立深度探针 0/4）；正式 Linux/NVIDIA campaign 尚未启动。BindCraft 只有未执行的原型和已审计输入，不能评价候选质量。**\n\n仓库已按两条线拆分主代码、一次性工具、报告、Notebook 和资源索引。模型权重、SAbDab2 原始库、环境与完整运行产物只保留可恢复 manifest，不进入公开 Git。"},
        {"id": "status_heading", "type": "markdown", "body": "## 当前状态：一条线等待正式算力，一条线等待协议修订\n\n“工程完成”“计算过滤通过”“实验验证”是三种状态。当前两条线都没有实验命中。"},
        {"id": "status_table_block", "type": "table", "tableId": "route_status_table"},
        {"id": "attempt_chart_block", "type": "chart", "chartId": "attempt_outcomes_chart"},
        {"id": "scope", "type": "markdown", "body": "## 审计范围与方法\n\n本次读取 Codex 任务列表并分页核对主任务的全部 35 turns，同时检查本地项目树、运行摘要、校验清单和 GitHub 空仓库状态。会话输出采用事实摘要，不提交原始 rollout；资源按用途、来源、revision、体积、Git 策略和限制登记。\n\n图表在这里被有意省略：本任务的核心是精确查找和状态审计，表格比比例图更能保留路径、口径和限制。"},
        {"id": "sessions_heading", "type": "markdown", "body": "## 八个会话条目覆盖了项目定义、双轨决策、数据、运行与审计\n\n7 个任务直接属于该项目，另 1 个是早期文档引用的背景会话。中断 turn 不被写成已完成。"},
        {"id": "sessions_table_block", "type": "table", "tableId": "session_table"},
        {"id": "boltzgen_findings", "type": "markdown", "sourceId": "status_source", "body": "## BoltzGen 的负结果已经改变下一步\n\nMac 三批推理说明流程可以落盘并暴露失败模式，但没有候选跨过冻结结构门。下一步不是把 Mac 批量放大，而是在 Linux/NVIDIA 环境实现 AIV1，以 10→240→2,400→12,000 逐级门控；只有得到配对实验标签后，才训练项目级重排序器。当前不重训基础模型。"},
        {"id": "bindcraft_findings", "type": "markdown", "sourceId": "status_source", "body": "## BindCraft 仍处于协议修订，不是运行阶段\n\n现有 Notebook 未执行；正负靶模型数不一致，两个同源肽结构不完整，9–36 是派生坐标，对默认阈值和 N 端接触加分的逻辑也需修正。修复后先做每个正构象 30–50 条轨迹的小试。"},
        {"id": "resources_heading", "type": "markdown", "body": "## 资源保留可恢复性，但不把 8.4 GiB 工作区塞进 Git\n\n资源表中的字节数来自 2026-08-26 本地快照；目录行不重新哈希全部内容，而指向原包逐文件校验清单。"},
        {"id": "resource_table_block", "type": "table", "tableId": "resource_table"},
        {"id": "naming_heading", "type": "markdown", "body": "## 目录从现在起区分正式流程与一次性工作\n\n`main/` 保存持续使用的实现；`tools/one_off/` 保存报告构建、迁移、剖析和封存脚本。冻结历史包内部实现名可保留，但公开入口必须带尝试和日期。"},
        {"id": "naming_table_block", "type": "table", "tableId": "naming_table"},
        {"id": "reports_heading", "type": "markdown", "body": "## 报告均有日期与路线归属\n\nHTML 是只读快照；规范 artifact 保存在相邻 manifests 目录。"},
        {"id": "report_table_block", "type": "table", "tableId": "report_table"},
        {"id": "limitations", "type": "markdown", "body": "## 限制、不确定性与稳健性检查\n\n- 会话是摘要，不是逐字 transcript；ChatGPT connector 的一条会话没有 startedAt。\n- Mac 结果来自实验性 MPS + CPU fallback，不能视为官方 CUDA 等价复现。\n- 所有结构分数都是计算代理；当前没有结合或选择性的实验真值。\n- 资源大小反映 2026-08-26 本地快照；外置目录未来变化必须新增 manifest 版本。\n- 仓库未声明项目整体许可证，公开可见不等于授权复用。"},
        {"id": "next_steps", "type": "markdown", "body": "## 建议下一步\n\n1. 审阅并合并本次仓库重组 PR，再为 `main` 启用至少 1 次审阅和必需 CI。\n2. BoltzGen：实现 AIV1 并在 Linux/NVIDIA 上跑 10 条门控，不越级放大。\n3. BindCraft：修完输入和评价合同，冻结上游 release/依赖，再做 30–50 轨迹/正构象的小试。\n4. 两线都通过计算门后，统一进入配对 SPR/BLI、表达 QC、交叉反应和捕获 LC–MS 验证。"},
        {"id": "questions", "type": "markdown", "body": "## 仍需回答的问题\n\n- 生产算力的 GPU 型号、队列限制和对象存储位置何时冻结？\n- GLP-1 C 端酰胺如何在每个结构输入与实验标准品中原子级对齐？\n- 96–192 条实验面板的预算、盲法分层和停止规则由谁批准？\n- BindCraft 同源肽的完整结构从何处获取，或是否改用统一的预测/建模面板？"},
    ]

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "GLP-1 AI 捕获蛋白项目：会话、进展与资源总览",
            "description": "截至 2026-08-26 的 BoltzGen/BindCraft 双路线会话、结果、资源和仓库治理审计。",
            "generatedAt": GENERATED_AT,
            "sources": [status_source, session_source, resource_source, policy_source, attempt_source],
            "blocks": blocks,
            "cards": [],
            "charts": [attempt_chart],
            "tables": tables,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": GENERATED_AT,
            "status": "ready",
            "accessIssues": [],
            "datasets": {
                "route_status": status_rows,
                "attempt_outcomes": attempt_rows,
                "sessions": session_rows,
                "resources": resource_rows,
                "naming": naming_rows,
                "reports": report_rows,
            },
        },
        "sources": [status_source, session_source, resource_source, policy_source, attempt_source],
        "package_info": {
            "mode": "portable_html",
            "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False},
        },
    }
    output = ROOT / "shared/reports/manifests/glp1_project_session_resource_overview_20260826.artifact.json"
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output.relative_to(ROOT)} with {len(blocks)} blocks and {len(tables)} tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
