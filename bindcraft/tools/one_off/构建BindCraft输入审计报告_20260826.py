#!/usr/bin/env python3
"""构建 BindCraft GLP-1 选择性原型的输入审计 artifact。

脚本读取原型 Notebook、已生成的 8 文件 PDB manifest 和输入审计 Notebook，
把可验证事实与修订建议写入 canonical report artifact。它不运行 BindCraft、
AlphaFold2、ProteinMPNN 或 PyRosetta，也不生成候选。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GENERATED_AT = "2026-08-26T16:35:00Z"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def source(source_id: str, label: str, path: str, description: str, sql: str) -> dict[str, object]:
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


def table(table_id: str, title: str, subtitle: str, dataset: str, source_object: dict[str, object], columns: list[tuple[str, str, str]], sort_field: str) -> dict[str, object]:
    return {
        "id": table_id,
        "title": title,
        "subtitle": subtitle,
        "dataset": dataset,
        "source": source_object,
        "density": "compact",
        "layout": "full",
        "defaultSort": {"field": sort_field, "direction": "asc"},
        "columns": [{"field": field, "label": label, "type": kind} for field, label, kind in columns],
    }


def main() -> int:
    prototype_path = ROOT / "bindcraft/main/active_glp1_selectivity_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb"
    prototype = json.loads(prototype_path.read_text(encoding="utf-8"))
    code_cells = [cell for cell in prototype.get("cells", []) if cell.get("cell_type") == "code"]
    executed_cells = [cell for cell in code_cells if cell.get("execution_count") is not None or cell.get("outputs")]

    panel_path = ROOT / "bindcraft/resources/manifests/bindcraft_glp1_target_panel_20260825.csv"
    panel_rows = read_csv(panel_path)
    panel_dataset = [
        {
            "file_name": row["file_name"], "role": row["role"], "source_derivation": row["source_derivation"],
            "residue_count": int(row["residue_count"]),
            "expected_residue_count": int(row["expected_residue_count"]),
            "coordinate_coverage": float(row["coordinate_coverage"]),
            "sequence": row["sequence"],
            "validation_status": row["validation_status"], "limitations": row["limitations"],
        }
        for row in panel_rows
    ]

    status_rows = [
        {"item": "原型代码", "observed": f"{len(code_cells)} code cells", "result": f"{len(executed_cells)} cells with execution evidence", "status": "NOT_EXECUTED", "meaning": "只能审计代码与输入，不能评价候选质量"},
        {"item": "靶标面板", "observed": f"{len(panel_rows)} PDB files", "result": "3 positive + 2 derived 9–36 + 3 homolog countertargets", "status": "NEEDS_REVISION", "meaning": "含不完整结构和非独立派生对照"},
        {"item": "运行产物", "observed": "0 candidate PDB / 0 result CSV / 0 logs", "result": "No generated evidence", "status": "ABSENT", "meaning": "当前不能下单或估计命中率"},
    ]
    findings_rows = [
        {"priority": "P0", "issue": "跨靶模型数不一致", "evidence": "主正靶使用两个模型平均；其余正靶和负靶默认只用一个模型", "consequence": "正负分数不可严格比较", "correction": "所有靶标固定相同模型集合、recycle、seed 状态和汇总规则"},
        {"priority": "P0", "issue": "GIP 与 oxyntomodulin 坐标不完整", "evidence": "GIP 30/42；oxyntomodulin 26/37", "consequence": "负靶表面和界面分数可能偏置", "correction": "补全、替换或明确采用统一建模策略，并冻结来源/哈希"},
        {"priority": "P0", "issue": "上游和依赖未固定", "evidence": "Notebook 动态克隆未固定 main 并下载外部资产", "consequence": "无法重建同一环境与结果", "correction": "固定 BindCraft tag/commit、AlphaFold2、ProteinMPNN、PyRosetta、模型资产和输入"},
        {"priority": "P1", "issue": "9–36 不是独立实验结构", "evidence": "由对应 7–36 PDB 删除前两个残基", "consequence": "只能当 matched computational challenge，不能当独立生物学重复", "correction": "保留 derived 标签，并在实验阶段使用真实 9–36 标准品"},
        {"priority": "P1", "issue": "默认 margin 门冗余", "evidence": "positive≥0.50 且 negative≤0.35 已保证 margin≥0.15", "consequence": "门看似更多，实际没有新增约束", "correction": "删除冗余门或预注册能增加信息的独立阈值"},
        {"priority": "P1", "issue": "N 端接触加分无排序信息", "evidence": "contacts∈{0,1,2}，pass 要求≥2，score 加 0.025×contacts", "consequence": "所有通过者都固定 +0.05", "correction": "改为连续距离/接触几何评分，或只作为硬门不参与排序"},
        {"priority": "P1", "issue": "1D0R 模型不独立", "evidence": "models 1 与 10 来自同一 NMR deposition", "consequence": "不能将构象数当生物学样本数", "correction": "按 deposition 分层；构象只作重复测量/鲁棒性挑战"},
        {"priority": "P1", "issue": "末端化学不一致", "evidence": "标准 PDB 聚合物记录未无歧义编码 C 端酰胺", "consequence": "不能声称模型已识别 NH₂ 化学选择性", "correction": "建立原子级化学注册表，并与实验标准品对齐"},
    ]
    metrics_rows = [
        {"metric": "positive_min_i_pTM", "current_definition": "所有正靶 iPTM 的最小值", "default_gate": ">=0.50", "problem": "模型数不一致导致不可比", "recommended_role": "相同模型/seed 合同下的鲁棒性硬门"},
        {"metric": "negative_max_i_pTM", "current_definition": "所有负靶 iPTM 的最大值", "default_gate": "<=0.35", "problem": "缺少统一 iPAE/pLDDT 与不完整结构处理", "recommended_role": "带负靶置信度的硬门和风险排序"},
        {"metric": "selectivity_margin", "current_definition": "positive_min - negative_max", "default_gate": ">=0.15", "problem": "在另外两个默认门下冗余", "recommended_role": "修订阈值后作为连续排序特征，不单独制造重复通过门"},
        {"metric": "N_terminal_contacts", "current_definition": "前两个目标残基中有重原子距 binder <=5 Å 的残基数", "default_gate": ">=2", "problem": "通过者恒为 2，+0.05 加分无区分力", "recommended_role": "硬门；另用连续最小距离/几何质量排序"},
    ]
    next_rows = [
        {"order": 1, "step": "修复输入", "deliverable": "完整同源肽、末端化学注册表、派生/独立结构标签", "gate": "全部文件来源与哈希冻结"},
        {"order": 2, "step": "固定环境", "deliverable": "BindCraft v.1.5.3 或批准 commit、依赖 lock、模型资产 manifest", "gate": "清洁重建和 GPU 预检通过"},
        {"order": 3, "step": "修复评价", "deliverable": "相同模型集合、seed 状态、recycle 和跨靶汇总；新评分合同", "gate": "合成单元测试和手工复核通过"},
        {"order": 4, "step": "小规模试跑", "deliverable": "每个正构象 30–50 trajectories 的日志、CSV、PDB 与失败原因", "gate": "至少工程完整且口径可审计；不预设必须有命中"},
        {"order": 5, "step": "实验候选门", "deliverable": "去重、多样性、表达风险和跨靶计算审阅", "gate": "通过后才选择 5–20 条实验候选"},
    ]

    findings_path = ROOT / "bindcraft/resources/manifests/bindcraft_input_audit_findings_20260826.csv"
    metrics_path = ROOT / "bindcraft/resources/manifests/bindcraft_evaluation_contract_review_20260826.csv"
    write_csv(findings_path, findings_rows)
    write_csv(metrics_path, metrics_rows)

    prototype_source = source(
        "prototype_source", "BindCraft GLP-1 原型 Notebook",
        "bindcraft/main/active_glp1_selectivity_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb",
        "原型代码单元、输入构建、阈值和选择性评价实现。",
        "SELECT 12 AS code_cells, 0 AS executed_cells, 0 AS output_cells",
    )
    panel_source = source(
        "panel_source", "BindCraft 靶标面板 manifest",
        "bindcraft/resources/manifests/bindcraft_glp1_target_panel_20260825.csv",
        "8 个 PDB 的角色、派生来源、残基数、序列、哈希、状态和限制。",
        "SELECT * FROM read_csv_auto('bindcraft/resources/manifests/bindcraft_glp1_target_panel_20260825.csv', header=true)",
    )
    findings_source = source(
        "findings_source", "BindCraft 输入审计发现",
        "bindcraft/resources/manifests/bindcraft_input_audit_findings_20260826.csv",
        "静态代码与输入审计后的优先级、证据、后果和修订。",
        "SELECT * FROM read_csv_auto('bindcraft/resources/manifests/bindcraft_input_audit_findings_20260826.csv', header=true)",
    )
    metrics_source = source(
        "metrics_source", "BindCraft 评价合同复核",
        "bindcraft/resources/manifests/bindcraft_evaluation_contract_review_20260826.csv",
        "当前选择性指标、默认门、逻辑问题和推荐角色。",
        "SELECT * FROM read_csv_auto('bindcraft/resources/manifests/bindcraft_evaluation_contract_review_20260826.csv', header=true)",
    )
    coverage_chart = {
        "id": "panel_coverage_chart",
        "title": "BindCraft 8 个输入 PDB 的序列坐标覆盖率",
        "subtitle": "分母为各肽声明长度；GIP 为 30/42，oxyntomodulin 为 26/37。",
        "type": "bar",
        "intent": "comparison",
        "question": "每个靶标文件覆盖了其声明肽序列的多少比例？",
        "rationale": "八个文件共享 0–1 完整度尺度，按角色着色可同时看见不完整同源肽与完整派生对照。",
        "dataset": "panel",
        "source": panel_source,
        "palette": {"kind": "categorical"},
        "legend": {"position": "bottom", "title": "输入角色"},
        "labels": {"values": "all"},
        "referenceLines": [{"axis": "y", "value": 1.0, "label": "声明序列完整", "lineStyle": "dashed"}],
        "encodings": {
            "x": {"field": "file_name", "type": "nominal", "label": "输入 PDB"},
            "y": {"field": "coordinate_coverage", "type": "quantitative", "label": "坐标覆盖率"},
            "color": {"field": "role", "type": "nominal"},
        },
    }

    tables = [
        table("status_table", "现有材料只能支持静态审计", "没有任何生成或实验结果。", "status", prototype_source,
              [("item", "对象", "text"), ("observed", "观察", "text"), ("result", "结果", "text"), ("status", "状态", "text"), ("meaning", "含义", "text")], "item"),
        table("panel_table", "8 个输入不等于 8 个独立实验结构", "正靶构象、派生对照和同源肽必须分角色解释。", "panel", panel_source,
              [("file_name", "文件", "text"), ("role", "角色", "text"), ("source_derivation", "来源/派生", "text"), ("residue_count", "已解析残基", "number"), ("expected_residue_count", "声明残基", "number"), ("coordinate_coverage", "坐标覆盖率", "number"), ("sequence", "序列", "text"), ("validation_status", "状态", "text"), ("limitations", "限制", "text")], "role"),
        table("findings_table", "三个 P0 问题会阻断可比性与复现", "P1 问题在小试前也应关闭。", "findings", findings_source,
              [("priority", "优先级", "text"), ("issue", "问题", "text"), ("evidence", "证据", "text"), ("consequence", "后果", "text"), ("correction", "修订", "text")], "priority"),
        table("metrics_table", "当前四个项目级指标需要重新分工", "硬门和排序特征不应重复表达同一约束。", "metrics", metrics_source,
              [("metric", "指标", "text"), ("current_definition", "当前定义", "text"), ("default_gate", "默认门", "text"), ("problem", "问题", "text"), ("recommended_role", "推荐角色", "text")], "metric"),
        table("next_table", "先修合同，再做 30–50 trajectories/正构象", "小试应保留完整失败证据，不以必须命中为成功条件。", "next", findings_source,
              [("order", "顺序", "number"), ("step", "步骤", "text"), ("deliverable", "交付物", "text"), ("gate", "进入下一步的门", "text")], "order"),
    ]
    blocks = [
        {"id": "title", "type": "markdown", "body": "# BindCraft × 活性 GLP-1：输入与选择性原型审计"},
        {"id": "summary", "type": "markdown", "sourceId": "prototype_source", "body": f"## 技术摘要\n\n**当前 BindCraft 线不能评价候选质量：原型有 {len(code_cells)} 个代码单元，但 {len(executed_cells)} 个具有执行证据；没有候选 PDB、结果 CSV、日志或实验结果。** 8 个 PDB 输入中包含两个坐标删除派生的 9–36 对照，以及 GIP 30/42 和 oxyntomodulin 26/37 两个不完整同源肽结构。\n\n结论是 `NEEDS_REVISION`，不是模型失败。先修跨靶可比性、输入完整性、依赖固定和评分合同，再做小规模轨迹。"},
        {"id": "status_intro", "type": "markdown", "body": "## 静态审计回答了“能否开始跑”，没有回答“是否会命中”\n\n本报告没有执行模型；所有发现来自 Notebook 源码、PDB 逐文件解析和输入审计。"},
        {"id": "status_block", "type": "table", "tableId": "status_table"},
        {"id": "scope", "type": "markdown", "body": "## 范围、数据和定义\n\n正靶是 GLP-1(7–36) 的受体结合态与两个 1D0R NMR 模型；负靶包括两个 matched 9–36 派生结构和三种同源肽。NMR model 是同一 deposition 内的重复构象，不是独立生物学重复。interface predicted template modeling score（iPTM）等指标只作计算代理。"},
        {"id": "panel_block", "type": "table", "tableId": "panel_table"},
        {"id": "coverage_chart_block", "type": "chart", "chartId": "panel_coverage_chart"},
        {"id": "method", "type": "markdown", "body": "## 方法：只读、逐文件、按逻辑合同复核\n\n审计统计 Notebook 执行状态，解析每个 PDB 的 ATOM 行、链、残基数、序列和 SHA-256，再检查正负靶是否使用同一模型集合、默认阈值是否独立、N 端接触项是否提供排序信息，以及上游/依赖是否固定。没有使用未运行代码产生的假结果。"},
        {"id": "findings_block", "type": "table", "tableId": "findings_table"},
        {"id": "metric_heading", "type": "markdown", "body": "## 损失/评价需要把生成目标、通过门和排序分开\n\nBindCraft 原生设计损失用于生成；项目级跨靶评价用于筛选。两者都不能替代实验选择性。默认参数下 margin 门与正负绝对门重复，N 端接触加分对所有通过者恒定。"},
        {"id": "metrics_block", "type": "table", "tableId": "metrics_table"},
        {"id": "limitations", "type": "markdown", "body": "## 限制与不确定性\n\n- 审计未运行 BindCraft，所以不能测量显存、耗时、稳定性或候选通过率。\n- PDB 残基完整性按当前 ATOM 行计数；不等于解析所有实验电子密度或末端化学。\n- 上游 BindCraft 当前参考 release 是 v.1.5.3，但现有 Notebook 未固定 revision，不能倒填该版本。\n- 所有计算选择性仍需真实 GLP-1(7–36)NH₂ 与 9–36NH₂ 配对实验确认。"},
        {"id": "next_block", "type": "table", "tableId": "next_table"},
        {"id": "questions", "type": "markdown", "body": "## 进一步问题\n\n- GIP 与 oxyntomodulin 采用完整实验结构、统一预测结构，还是排除出第一版面板？\n- 小试使用哪一固定 BindCraft commit 和哪种 NVIDIA GPU？\n- 选择性排序是否只针对 9–36，还是把同源肽作为独立安全门？\n- 计算小试到实验下单的去重、多样性和可表达性门由谁批准？"},
    ]

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1, "surface": "report",
            "title": "BindCraft × 活性 GLP-1：输入与选择性原型审计",
            "description": "对未执行 BindCraft 原型、8 个 PDB 输入和项目级选择性指标的只读技术审计。",
            "generatedAt": GENERATED_AT,
            "sources": [prototype_source, panel_source, findings_source, metrics_source],
            "blocks": blocks, "cards": [], "charts": [coverage_chart], "tables": tables,
        },
        "snapshot": {
            "version": 1, "generatedAt": GENERATED_AT, "status": "ready", "accessIssues": [],
            "datasets": {"status": status_rows, "panel": panel_dataset, "findings": findings_rows, "metrics": metrics_rows, "next": next_rows},
        },
        "sources": [prototype_source, panel_source, findings_source, metrics_source],
        "package_info": {"mode": "portable_html", "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False}},
    }
    output = ROOT / "bindcraft/reports/manifests/bindcraft_glp1_selectivity_input_audit_20260826.artifact.json"
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output.relative_to(ROOT)} with {len(blocks)} blocks and {len(tables)} tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
