#!/usr/bin/env python3
"""把一次真实 BoltzGen MVP 运行整理成可验证的技术报告 artifact。

这个脚本只读取已经冻结的输入、日志和模型输出，不会重新运行模型，也不会修改
BoltzGen 产生的原始结果。它输出 canonical Data Analytics artifact JSON；随后由共享
portable builder 将同一份数据封装为一个完全离线、自包含的 HTML 文件。
"""

from __future__ import annotations

import csv
import base64
import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# 本脚本位于 RUN_ROOT/scripts/，所有路径从脚本位置推导，避免依赖调用时的工作目录。
RUN_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = RUN_ROOT / "outputs" / "02_mps_run"
PIPELINE_ROOT = OUTPUT_ROOT / "pipeline"
FINAL_ROOT = PIPELINE_ROOT / "final_ranked_designs"
ANALYSIS_ROOT = RUN_ROOT / "analysis"
REPORT_ROOT = RUN_ROOT / "report"


def read_json(path: Path) -> object:
    """读取 UTF-8 JSON，并让缺失或语法错误直接中止构建。"""

    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    """按表头读取 CSV/TSV；返回的每一项对应一条数据行。"""

    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def escape(value: object) -> str:
    """对嵌入 HTML 的原始文本做实体转义，防止代码样例被当成标签执行。"""

    return html.escape(str(value), quote=True)


def preformatted(text: str) -> str:
    """生成可横向滚动、可键盘聚焦的代码块。"""

    return f'<pre tabindex="0"><code>{escape(text)}</code></pre>'


def png_data_uri(path: Path) -> str:
    """把分析脚本生成的PNG内嵌进最终HTML，保证报告离线打开也能显示。"""

    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{payload}"


def portable_command() -> str:
    """返回不含本机绝对路径的复现命令；真实绝对命令仍保存在 status JSON 中。"""

    return """# 进入本次运行目录后执行；脚本会先验证4项运行资产的SHA-256。
./env/bin/python scripts/run_mvp.py --through execute

# 只重建分析表与图片，不重新运行模型。
./env/bin/python scripts/analyze_results.py

# 只重建并执行复盘Notebook，不重新运行模型。
./env/bin/python scripts/build_notebook.py"""


def parse_pipeline_stage_timings(log_text: str) -> list[dict[str, object]]:
    """从官方 execute 日志提取五个模型阶段的实际耗时。

    这里不用进度条估算时间，而只读取 BoltzGen 在每个步骤结束时写出的
    ``completed successfully in ...s`` 记录。
    """

    pattern = re.compile(r"Step ([a-z_]+) completed successfully in ([0-9.]+)s")
    labels = {
        "design": "1 结构生成",
        "inverse_folding": "2 逆折叠",
        "folding": "3 复折叠",
        "analysis": "4 指标分析",
        "filtering": "5 过滤排名",
    }
    rows = []
    for stage, seconds in pattern.findall(log_text):
        rows.append({"stage": labels.get(stage, stage), "stage_key": stage, "seconds": float(seconds)})
    if len(rows) != 5:
        raise ValueError(f"期望从执行日志读到5个阶段耗时，实际为{len(rows)}个")
    return rows


def bool_value(value: object) -> bool:
    """把 pandas/CSV 常见的 True/False 字符串稳定转换为布尔值。"""

    return str(value).strip().lower() in {"true", "1", "yes"}


def make_source(
    source_id: str,
    label: str,
    path: str,
    sql: str,
    description: str,
    tables_used: list[str],
) -> dict[str, object]:
    """创建统一的来源对象，使卡片、图表和表格都能打开精确来源说明。"""

    return {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": sql,
            "description": description,
            "tables_used": tables_used,
        },
    }


# 点击式详情模块的样式。所有颜色在浅色/深色模式下均保持足够对比度。
DETAIL_STYLE = """
<style>
  .mvp-guide{font:17px/1.68 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#17344b}
  .mvp-guide *{box-sizing:border-box}.mvp-guide h2{margin:0 0 8px}.mvp-guide .intro{margin:0 0 14px;color:#536c80}
  .mvp-guide details{margin:10px 0;border:1px solid #c8d8e2;border-radius:14px;background:#fff;overflow:clip}
  .mvp-guide summary{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:15px 17px;cursor:pointer;list-style:none;background:#f4f8fa;color:#103a58;font-weight:760}
  .mvp-guide summary::-webkit-details-marker{display:none}.mvp-guide summary::after{content:"＋";font-size:22px;color:#076c64}.mvp-guide details[open] summary::after{content:"－"}
  .mvp-guide summary:focus-visible{outline:3px solid #076c64;outline-offset:-3px}.mvp-guide .body{padding:16px 18px 19px}
  .mvp-guide .facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:8px;margin:10px 0 15px}
  .mvp-guide .fact{padding:9px 11px;border-radius:10px;background:#f0f5f7;overflow-wrap:anywhere}.mvp-guide .fact b{display:block;color:#61798b;font-size:12px;letter-spacing:.03em}
  .mvp-guide .note{margin:12px 0 0;padding:11px 13px;border-left:4px solid #a85c00;background:#fff5e6;border-radius:0 9px 9px 0}
  .mvp-guide .stop{border-left-color:#a7322b;background:#fff0ee}.mvp-guide pre{max-height:520px;margin:10px 0;overflow:auto;white-space:pre;padding:14px;border-radius:10px;background:#0f2638;color:#edf5f7;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  .mvp-guide code{overflow-wrap:anywhere}.mvp-guide ul{padding-left:23px}.mvp-guide li{margin:5px 0}.mvp-guide .flow{display:grid;grid-template-columns:repeat(5,minmax(118px,1fr));gap:8px;margin:14px 0}
  .mvp-guide .flow-step{padding:12px 10px;text-align:center;border-radius:11px;background:#e9f4f3;border:1px solid #a9cfca;font-weight:700}.mvp-guide .flow-step span{display:block;margin-top:3px;font-size:12px;font-weight:500;color:#526a7c}
  .mvp-guide .skipped{background:#f3f3f3;border-color:#d3d3d3;color:#68737c;text-decoration:line-through}.mvp-guide .badge{display:inline-block;margin-left:7px;padding:2px 8px;border-radius:99px;background:#ffe8c4;color:#764500;font-size:12px;vertical-align:2px}
  @media(prefers-color-scheme:dark){.mvp-guide{color:#dceaf1}.mvp-guide .intro{color:#b3c7d2}.mvp-guide details{background:#172b39;border-color:#3a586a}.mvp-guide summary{background:#1e3747;color:#f0f7fa}.mvp-guide .body{background:#172b39}.mvp-guide .fact{background:#213b49}.mvp-guide .fact b{color:#b3c8d3}.mvp-guide .note{background:#48361c}.mvp-guide .stop{background:#4a2928}.mvp-guide .flow-step{background:#193c3b;border-color:#417c77}.mvp-guide .skipped{background:#30373c;border-color:#56616a}.mvp-guide .badge{background:#533c17;color:#ffe2ad}}
  @media(max-width:760px){.mvp-guide{font-size:16px}.mvp-guide .flow{grid-template-columns:1fr}.mvp-guide summary,.mvp-guide .body{padding:13px}.mvp-guide .facts{grid-template-columns:1fr}}
</style>
"""


def fact_grid(items: list[tuple[str, str]]) -> str:
    """把短事实列表排成响应式网格。"""

    cells = "".join(
        f'<div class="fact"><b>{escape(label)}</b>{escape(value)}</div>' for label, value in items
    )
    return f'<div class="facts">{cells}</div>'


def detail(title: str, body: str, badge: str = "") -> str:
    """创建一个不依赖 JavaScript 的可点击详情。"""

    badge_html = f'<span class="badge">{escape(badge)}</span>' if badge else ""
    return f'<details><summary><span>{escape(title)}{badge_html}</span></summary><div class="body">{body}</div></details>'


def main() -> None:
    """加载真实结果、构造快照与报告清单，并写出 artifact JSON。"""

    status = read_json(OUTPUT_ROOT / "mvp_run_status.json")
    input_manifest = read_json(OUTPUT_ROOT / "input_manifest.json")
    metrics_rows = read_csv(FINAL_ROOT / "all_designs_metrics.csv")
    if not isinstance(status, dict) or not isinstance(input_manifest, dict):
        raise TypeError("状态和输入manifest必须是JSON对象")
    if status.get("status") != "PIPELINE_COMPLETE":
        raise ValueError("模型管线尚未完成，拒绝生成成功报告")
    if len(metrics_rows) != 2:
        raise ValueError(f"本次MVP应有2个候选，实际读到{len(metrics_rows)}个")

    # 以final_rank排序，避免CSV物理顺序改变时影响阅读顺序。
    metrics_rows.sort(key=lambda row: int(float(row["final_rank"])))
    passed_count = sum(bool_value(row["pass_filters"]) for row in metrics_rows)
    execute_seconds = float(status["stages"][-1]["elapsed_seconds"])
    stage_rows = parse_pipeline_stage_timings((RUN_ROOT / "logs" / "03_execute.log").read_text(encoding="utf-8"))

    # 漏斗保持真实流程顺序；最后一行是硬过滤通过数，不是final目录文件数。
    funnel_rows = [
        {"order": 1, "stage": "请求生成", "count": int(input_manifest["requested_designs"])},
        {"order": 2, "stage": "结构生成完成", "count": 2},
        {"order": 3, "stage": "逆折叠完成", "count": 2},
        {"order": 4, "stage": "复折叠完成", "count": 2},
        {"order": 5, "stage": "分析并去重", "count": len(metrics_rows)},
        {"order": 6, "stage": "通过全部过滤", "count": passed_count},
    ]

    # 候选表只选可解释、真实存在的字段；不显示native_rmsd占位值。
    candidates = []
    rmsd_rows = []
    for display_index, row in enumerate(metrics_rows, start=1):
        # 面向读者使用1起算的“候选1/2”，同时保留BoltzGen原始0起算ID，避免和
        # 文件名_suffix_0/_1混淆。
        candidate_id = f"候选{display_index}"
        candidate = {
            "candidate": candidate_id,
            "raw_id": row["id"],
            "rank": int(float(row["final_rank"])),
            "pass_filters": bool_value(row["pass_filters"]),
            "full_vhh_length": len(row["designed_chain_sequence"]),
            "designed_region_length": len(row["designed_sequence"]),
            "design_to_target_iptm": round(float(row["design_to_target_iptm"]), 5),
            "min_design_to_target_pae_A": round(float(row["min_design_to_target_pae"]), 5),
            "complex_rmsd_A": round(float(row["filter_rmsd"]), 5),
            "design_rmsd_A": round(float(row["filter_rmsd_design"]), 5),
            "hotspot_contacts_under_8A": int(float(row["bindsite_under_8rmsd"])),
            "hbonds": int(float(row["plip_hbonds_refolded"])),
            "salt_bridges": int(float(row["plip_saltbridge_refolded"])),
            "target_delta_sasa_A2": round(float(row["delta_sasa_refolded"]), 3),
            "designed_sequence": row["designed_sequence"],
            "full_vhh_sequence": row["designed_chain_sequence"],
        }
        candidates.append(candidate)
        rmsd_rows.extend(
            [
                {"candidate": candidate_id, "metric": "复合物骨架RMSD", "angstrom": candidate["complex_rmsd_A"]},
                {"candidate": candidate_id, "metric": "设计区骨架RMSD", "angstrom": candidate["design_rmsd_A"]},
            ]
        )

    # 读取分析脚本生成的输出清单；它将目录文件计数和总字节纳入可复核表格。
    inventory_path = ANALYSIS_ROOT / "output_inventory.tsv"
    if not inventory_path.exists():
        raise FileNotFoundError("请先运行 scripts/analyze_results.py 生成 analysis/output_inventory.tsv")
    raw_output_inventory = read_csv(inventory_path, delimiter="\t")

    # 分析脚本写的是逐文件审计表。报告把它按“角色”汇总，以免40行文件路径淹没
    # 输入/输出关系；原始逐文件表仍可在来源弹窗中完整核验。
    role_metadata = {
        "run_manifest": (1, "运行记录", "JSON", "冻结输入、状态和环境；用于复现与审计"),
        "resolved_configuration": (2, "冻结配置", "YAML", "五步管线实际读取的解析后配置"),
        "raw_generated_structure": (3, "结构生成", "CIF", "扩散产生的候选骨架/复合物"),
        "raw_design_metadata": (3, "结构生成", "NPZ", "设计掩码、热点和token元数据"),
        "inverse_folded_backbone_structure": (4, "逆折叠", "CIF", "序列化后的骨架；侧链坐标不可作最终结构"),
        "inverse_folded_metadata": (4, "逆折叠", "NPZ", "逆折叠阶段的设计元数据"),
        "folding_prediction_npz": (5, "复折叠", "NPZ", "原子坐标、token映射与界面置信汇总"),
        "refolded_complex_structure": (5, "复折叠", "CIF", "Boltz-2复折叠后的候选—目标复合物"),
        "analysis_intermediate_npz": (6, "指标分析", "NPZ", "RMSD、接触与几何分析的中间数组"),
        "analysis_metrics": (6, "指标分析", "CSV", "逐候选和逐目标汇总指标"),
        "final_metrics": (7, "过滤排名", "CSV", "最终排序与pass_filters权威表"),
        "final_refolded_structure": (7, "过滤排名", "CIF", "按排名复制的复折叠结构；不等于通过"),
        "boltzgen_summary_pdf": (7, "过滤排名", "PDF", "BoltzGen自动生成的指标概览"),
        "auxiliary_output": (8, "辅助产物", "混合", "检查快照、日志参数或重复展示文件"),
    }
    grouped_inventory: dict[str, dict[str, object]] = defaultdict(
        lambda: {"file_count": 0, "bytes": 0, "extensions": set()}
    )
    for row in raw_output_inventory:
        role = row["role"]
        grouped_inventory[role]["file_count"] += 1
        grouped_inventory[role]["bytes"] += int(row["size_bytes"])
        grouped_inventory[role]["extensions"].add(row["extension"].lstrip(".").upper() or "无扩展名")

    output_inventory = []
    for role, totals in grouped_inventory.items():
        order, stage, default_format, interpretation = role_metadata.get(
            role, (99, "其他", "混合", "详见逐文件审计表")
        )
        observed_format = "/".join(sorted(totals["extensions"])) or default_format
        output_inventory.append(
            {
                "stage_order": order,
                "stage": stage,
                "role": role,
                "format": observed_format,
                "file_count": totals["file_count"],
                "bytes": totals["bytes"],
                "interpretation": interpretation,
            }
        )
    output_inventory.sort(key=lambda row: (row["stage_order"], row["role"]))

    # NPZ schema来自实际两个fold_out_npz文件的shape/dtype检查，而不是文档猜测。
    npz_schema = read_json(ANALYSIS_ROOT / "npz_schema.json")
    npz_rows = []
    for file_record in npz_schema["files"]:
        # 这里只展示fold_out_npz，因为它是最终复折叠结构与analysis之间的关键桥梁。
        if file_record["role"] != "folding_output_and_analysis_input":
            continue
        candidate_name = Path(file_record["path"]).stem
        for array_record in file_record["arrays"]:
            npz_rows.append(
                {
                    "candidate": candidate_name,
                    "array": array_record["key"],
                    "shape": str(array_record["shape"]),
                    "dtype": array_record["dtype"],
                    "meaning": f"{array_record['axis_semantics']}；{array_record['scientific_meaning']}",
                }
            )
    if len({row["candidate"] for row in npz_rows}) != 2:
        raise ValueError("期望在npz_schema中找到两个fold_out_npz候选")

    summary_rows = [
        {
            "requested": int(input_manifest["requested_designs"]),
            "completed": len(metrics_rows),
            "passed": passed_count,
            "execute_seconds": round(execute_seconds, 3),
            "experimental_mps": 1,
        }
    ]

    # 来源查询展示“如何从原始文件复现报告数据”，不把来源说明伪装成SQL。
    summary_source = make_source(
        "run_summary_source",
        "已冻结运行状态与候选结果",
        "analysis/run_summary.json",
        """SELECT counts.requested AS requested,
       counts.unique_ranked AS completed,
       counts.pass_default_filters AS passed,
       execute_wrapper_elapsed_seconds AS execute_seconds
FROM read_json_auto('analysis/run_summary.json')""",
        "从经源文件哈希保护的分析摘要读取请求数、完成数、全部过滤通过数和execute总耗时。",
        ["analysis/run_summary.json"],
    )
    candidate_source = make_source(
        "candidate_source",
        "BoltzGen最终候选指标CSV",
        "outputs/02_mps_run/pipeline/final_ranked_designs/all_designs_metrics.csv",
        """SELECT id, final_rank, pass_filters, designed_sequence, designed_chain_sequence,
       design_to_target_iptm, min_design_to_target_pae,
       filter_rmsd, filter_rmsd_design, bindsite_under_8rmsd,
       plip_hbonds_refolded, plip_saltbridge_refolded, delta_sasa_refolded
FROM read_csv_auto('outputs/02_mps_run/pipeline/final_ranked_designs/all_designs_metrics.csv')
ORDER BY final_rank""",
        "提取本报告使用的候选序列、结构自洽、界面代理和官方过滤结果。",
        ["outputs/02_mps_run/pipeline/final_ranked_designs/all_designs_metrics.csv"],
    )
    stage_source = make_source(
        "stage_source",
        "BoltzGen execute阶段日志",
        "logs/03_execute.log",
        """SELECT display_name_cn AS stage,
       CAST(elapsed_seconds AS DOUBLE) AS seconds
FROM read_csv_auto('analysis/stage_timings.csv')
WHERE category = 'pipeline' AND status = 'completed'
ORDER BY "order";""",
        "从日志中的每个completed successfully记录提取五步管线耗时。",
        ["logs/03_execute.log", "analysis/stage_timings.csv"],
    )
    funnel_source = make_source(
        "funnel_source",
        "逐阶段文件与结果计数",
        "analysis/process_funnel.csv",
        """SELECT stage_label_cn AS stage, count
FROM read_csv_auto('analysis/process_funnel.csv')
ORDER BY "order";""",
        "按运行顺序展示请求、生成、逆折叠、复折叠、分析和硬过滤通过数量。",
        ["analysis/process_funnel.csv"],
    )
    inventory_source = make_source(
        "inventory_source",
        "模型输出文件清单",
        "analysis/output_inventory.tsv",
        """SELECT role, extension,
       count(*) AS file_count,
       sum(size_bytes) AS bytes
FROM read_csv_auto('analysis/output_inventory.tsv', delim='\\t', header=true)
GROUP BY role, extension
ORDER BY role, extension""",
        "从逐文件审计表按输出角色和扩展名汇总文件数量与总字节；阶段名和正确用法由报告中的固定角色字典补充。",
        ["analysis/output_inventory.tsv"],
    )

    # 输入详情直接嵌入真实YAML，便于读者核对目标链、热点与scaffold索引。
    top_yaml = (RUN_ROOT / "inputs" / "glp1_7_36_nanobody_mvp.yaml").read_text(encoding="utf-8")
    scaffold_yaml = (RUN_ROOT / "inputs" / "scaffold" / "7xl0_mvp_scaffold.yaml").read_text(encoding="utf-8")
    runner_code = (RUN_ROOT / "scripts" / "run_mvp.py").read_text(encoding="utf-8")

    input_details = DETAIL_STYLE + '<section class="mvp-guide"><h2>输入：点击查看真实内容</h2><p class="intro">以下内容来自本次运行的冻结文件，不是示例占位符。</p>'
    input_details += detail(
        "顶层设计YAML：目标、热点和VHH入口",
        fact_grid(
            [
                ("目标", "6X18来源GLP-1(7–36)几何，label链E，30 aa"),
                ("热点", "E:1..2，即生物学编号His7/Ala8"),
                ("VHH", "只使用7XL0官方example链A"),
                ("用途", "正靶冒烟测试，不含9–36反靶"),
            ]
        )
        + preformatted(top_yaml)
        + '<div class="note stop"><b>化学限制：</b>标准聚合物CIF没有原子级证明C端–CONH₂被模型往返保留，所以本次只能称作7–36几何输入。</div>',
        "真实输入",
    )
    input_details += detail(
        "7XL0 VHH scaffold 配方",
        fact_grid(
            [
                ("结构来源", "RCSB 7XL0 / BoltzGen v0.3.2官方示例快照"),
                ("设计区", "CDR样范围26..34、52..59、98..118"),
                ("框架二硫键", "原始CIF保留Cys22–Cys95 _struct_conn"),
                ("状态", "provisional example，不是生产批准scaffold"),
            ]
        )
        + preformatted(scaffold_yaml)
        + '<div class="note"><b>为什么不是清理后的7XL0：</b>旧清理副本丢失了跨残基二硫键记录；本次改用与官方示例字节级相同的原始CIF，YAML只include链A。</div>',
        "真实输入",
    )
    runtime_facts = [
        (item["asset"], f"{item['size_bytes']:,} B｜SHA {item['sha256'][:12]}…")
        for item in input_manifest["runtime_assets"]
    ]
    input_details += detail(
        "预训练权重与化学字典",
        fact_grid(runtime_facts)
        + '<p>四项均为<strong>推理输入</strong>：diverse设计权重、inverse-fold权重、Boltz-2复折叠/置信度权重、mols.zip化学组分字典。本次没有训练BoltzGen本体，也没有调用只适用于protein–small_molecule的affinity权重。</p>',
        "SHA已验证",
    )
    input_details += "</section>"

    process_details = DETAIL_STYLE + '<section class="mvp-guide"><h2>过程：从输入到最终表</h2><p class="intro">五步均实际完成；灰色步骤是nanobody-anything协议明确跳过的功能。</p>'
    process_details += '<div class="flow">' + "".join(
        [
            '<div class="flow-step">结构生成<span>design</span></div>',
            '<div class="flow-step">序列逆折叠<span>inverse_folding</span></div>',
            '<div class="flow-step">复合物复折叠<span>folding</span></div>',
            '<div class="flow-step">指标计算<span>analysis</span></div>',
            '<div class="flow-step">硬过滤与排名<span>filtering</span></div>',
        ]
    ) + '</div><div class="flow"><div class="flow-step skipped">单体折叠<span>design_folding：跳过</span></div><div class="flow-step skipped">亲和力头<span>affinity：跳过</span></div></div>'
    process_details += detail(
        "完整运行入口代码（全部带中文注释）",
        '<p><code>scripts/run_mvp.py</code>负责哈希校验、输入冻结、命令日志、实验性MPS环境设置和阶段状态记录。</p>'
        + preformatted(runner_code),
        "可复现代码",
    )
    process_details += detail(
        "最短复现命令",
        preformatted(portable_command())
        + '<div class="note"><b>不要直接重复执行已有output目录：</b>若要重复推理，应创建新的run_id/output目录，保留本次原始结果。</div>',
        "不含绝对路径",
    )
    process_details += "</section>"

    # 每个候选单独给完整序列和“为什么失败”，避免把长序列塞进主表破坏可读性。
    candidate_details = DETAIL_STYLE + '<section class="mvp-guide"><h2>候选：点击查看完整序列与判读</h2><p class="intro">“排名1”只表示本批两个候选中的相对顺序；两个候选均未通过默认硬过滤。</p>'
    for candidate in candidates:
        failure_text = (
            f"复合物RMSD {candidate['complex_rmsd_A']:.2f} Å、设计区RMSD {candidate['design_rmsd_A']:.2f} Å，"
            f"均高于2.5 Å阈值；His7/Ala8热点8 Å内接触数为{candidate['hotspot_contacts_under_8A']}。"
        )
        candidate_details += detail(
            f"{candidate['candidate']}｜本批排名{candidate['rank']}｜未通过",
            fact_grid(
                [
                    ("BoltzGen原始ID", candidate["raw_id"]),
                    ("完整VHH长度", f"{candidate['full_vhh_length']} aa"),
                    ("设计区长度", f"{candidate['designed_region_length']} aa"),
                    ("CDR→目标 iPTM", f"{candidate['design_to_target_iptm']:.5f}"),
                    ("最小设计区→目标 PAE", f"{candidate['min_design_to_target_pae_A']:.2f} Å"),
                    ("界面氢键/盐桥", f"{candidate['hbonds']} / {candidate['salt_bridges']}"),
                    ("目标ΔSASA", f"{candidate['target_delta_sasa_A2']:.1f} Å²"),
                ]
            )
            + '<h3>设计区拼接序列</h3>'
            + preformatted(candidate["designed_sequence"])
            + '<h3>完整VHH序列</h3>'
            + preformatted(candidate["full_vhh_sequence"])
            + f'<div class="note stop"><b>失败原因：</b>{escape(failure_text)} 因此不进入合成清单。</div>',
            "未通过",
        )
    candidate_details += "</section>"

    npz_detail_rows = "".join(
        f"<tr><td>{escape(row['array'])}</td><td>{escape(row['shape'])}</td><td>{escape(row['dtype'])}</td><td>{escape(row['meaning'])}</td></tr>"
        for row in npz_rows
        if row["candidate"] == "glp1_7_36_nanobody_mvp_0"
    )
    output_details = DETAIL_STYLE + '<section class="mvp-guide"><h2>输出：文件、数组和正确用法</h2><p class="intro">模型输出既包含结构文件，也包含数组与汇总表；不同文件不能混作同一种结果。</p>'
    output_details += detail(
        "fold_out_npz：数组的行、列和向量含义",
        '<div style="overflow:auto"><table style="width:100%;border-collapse:collapse;min-width:720px"><thead><tr><th>数组</th><th>shape</th><th>dtype</th><th>每个轴代表什么</th></tr></thead><tbody>'
        + npz_detail_rows
        + '</tbody></table></div><ul><li><code>coords [S,N,3]</code>：S是结构采样数，N是原子数，最后三列是x/y/z坐标（Å）。本次S=1。</li><li><code>res_type [B,T,33]</code>：B是batch，T是token/残基位置，33是模型的残基类别通道；不能当作33维连续物化性质。</li><li><code>atom_to_token [B,N,T]</code>：布尔映射矩阵；第n个原子属于第t个token时为真。</li><li>本NPZ只保存PAE汇总标量，没有完整PAE矩阵，因此报告没有伪造PAE热图。</li></ul>',
        "真实shape",
    )
    output_details += detail(
        "哪个CIF才是最终结构",
        '<ul><li><code>intermediate_designs/*.cif</code>：扩散生成的结构。</li><li><code>intermediate_designs_inverse_folded/*.cif</code>：逆折叠骨架；设计侧链坐标可能为0，不能作最终原子模型。</li><li><code>refold_cif/*.cif</code>：Boltz-2复折叠后的复合物，是本报告结构图的来源。</li><li><code>final_1_designs/*.cif</code>：按批内排名复制的结构；本次即使写入该目录仍<strong>没有通过过滤</strong>。</li></ul>',
        "防误用",
    )
    output_details += "</section>"

    # 结构图不是普通业务图表：它以Cα轨迹表达三维几何，因此使用分析脚本生成的
    # 科学静态图，而不是把三维坐标强行塞进二维原生柱状图。
    ca_trace_uri = png_data_uri(ANALYSIS_ROOT / "figures" / "final_cif_ca_trace_3d.png")
    interface_proxy_uri = png_data_uri(ANALYSIS_ROOT / "figures" / "interface_proxies.png")
    structure_visual = DETAIL_STYLE + f'''<section class="mvp-guide">
      <h2>排名1候选的复折叠Cα轨迹</h2>
      <p class="intro">来源：<code>final_1_designs/rank1_glp1_7_36_nanobody_mvp_0.cif</code>。图中只连结每个残基的Cα原子，用于观察整体相对位置；它不是实验结构、电子密度或原子碰撞验证。</p>
      <figure style="margin:12px 0">
        <img src="{ca_trace_uri}" alt="排名1候选与GLP-1目标的三维Cα轨迹；目标N端His7和Ala8被强调" style="display:block;width:100%;height:auto;max-height:720px;object-fit:contain;border-radius:14px;border:1px solid #c8d8e2;background:#fff">
        <figcaption style="margin-top:8px;color:#536c80">目标链A为30 aa GLP-1；VHH链B为125 aa。目标位置1/2对应His7/Ala8。该候选仍因RMSD与热点接触过滤失败。</figcaption>
      </figure>
    </section>'''
    proxy_visual = DETAIL_STYLE + f'''<section class="mvp-guide">
      <h2>界面计算代理：只能在本批内部比较</h2>
      <p class="intro">左图是0–1区间的结构/界面置信代理，右图是Å单位的PAE汇总标量。两类量纲分开绘制，避免把不同尺度混成一个总分。</p>
      <figure style="margin:12px 0">
        <img src="{interface_proxy_uri}" alt="两个候选的iPTM、pTM、ipSAE与PAE汇总标量对比" style="display:block;width:100%;height:auto;max-height:760px;object-fit:contain;border-radius:14px;border:1px solid #c8d8e2;background:#fff">
        <figcaption style="margin-top:8px;color:#536c80">候选1的CDR→目标iPTM略高、最小PAE略低，但它仍未通过RMSD与热点接触过滤；这些数值不能换算为Kd或结合概率。</figcaption>
      </figure>
    </section>'''

    generated_at = datetime.now(timezone.utc).isoformat()
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "BoltzGen nanobody-anything MVP：真实运行、结果与评价",
            "description": "Apple Silicon实验性MPS冒烟测试的输入、五步过程、真实输出、过滤失败与下一轮决策。",
            "generatedAt": generated_at,
            "cards": [
                {"id": "requested_card", "description": "顶层运行配置请求生成的候选数。", "dataset": "summary", "source": summary_source, "metrics": [{"label": "请求候选", "field": "requested", "unit": "个"}]},
                {"id": "completed_card", "description": "完成生成、逆折叠、复折叠和分析的候选数。", "dataset": "summary", "source": summary_source, "metrics": [{"label": "完整计算", "field": "completed", "unit": "个"}]},
                {"id": "passed_card", "description": "同时通过全部默认硬过滤的候选数。", "dataset": "summary", "source": summary_source, "metrics": [{"label": "全部过滤通过", "field": "passed", "unit": "个"}]},
                {"id": "runtime_card", "description": "仅BoltzGen execute五步管线的实际墙钟时间。", "dataset": "summary", "source": summary_source, "metrics": [{"label": "execute耗时", "field": "execute_seconds", "unit": "秒"}]},
            ],
            "charts": [
                {
                    "id": "stage_timing_chart",
                    "title": "五步模型管线实际耗时",
                    "subtitle": "结构生成和复折叠占用最多时间；总和与execute墙钟时间的差异来自阶段启动与文件处理。",
                    "type": "bar",
                    "intent": "comparison",
                    "question": "本次MVP的时间主要消耗在哪些阶段？",
                    "rationale": "五个按流程排序的阶段共享相同秒数单位，条形图适合比较阶段耗时。",
                    "dataset": "stage_timings",
                    "source": stage_source,
                    "labels": {"values": "all"},
                    "encodings": {"x": {"field": "stage", "type": "nominal", "label": "阶段"}, "y": {"field": "seconds", "type": "quantitative", "label": "秒"}},
                },
                {
                    "id": "funnel_chart",
                    "title": "候选数量沿管线的变化",
                    "subtitle": "两个候选完成全部计算，但在硬过滤后归零。",
                    "type": "bar",
                    "intent": "comparison",
                    "question": "候选在哪个阶段被淘汰？",
                    "rationale": "按固定流程顺序显示每一阶段保留数量，能直接暴露计算完成与质量通过的差别。",
                    "dataset": "funnel",
                    "source": funnel_source,
                    "labels": {"values": "all"},
                    "encodings": {"x": {"field": "stage", "type": "nominal", "label": "阶段"}, "y": {"field": "count", "type": "quantitative", "label": "候选数"}},
                },
                {
                    "id": "rmsd_chart",
                    "title": "候选复折叠自洽RMSD",
                    "subtitle": "四个观测值均高于官方默认2.5 Å硬阈值；RMSD是自洽性，不是与实验真值的误差。",
                    "type": "bar",
                    "intent": "comparison",
                    "question": "两个候选的结构自洽性是否通过默认过滤？",
                    "rationale": "两个候选各有相同量纲的复合物与设计区RMSD，分组条形图可直接对照统一阈值。",
                    "dataset": "rmsd",
                    "source": candidate_source,
                    "palette": {"kind": "categorical"},
                    "legend": {"position": "bottom", "title": "RMSD类型"},
                    "labels": {"values": "all"},
                    "referenceLines": [{"axis": "y", "value": 2.5, "label": "默认上限2.5 Å", "color": "neutral", "lineStyle": "dashed"}],
                    "encodings": {
                        "x": {"field": "candidate", "type": "nominal", "label": "候选"},
                        "y": {"field": "angstrom", "type": "quantitative", "label": "Å"},
                        "color": {"field": "metric", "type": "nominal"},
                    },
                    "combinationRationale": "颜色区分同一候选的两类RMSD，而不是重复编码候选名称。",
                },
            ],
            "tables": [
                {
                    "id": "candidate_table",
                    "title": "两个候选的可解释结果",
                    "subtitle": "iPTM/PAE/ΔSASA是结构代理，不能换算成Kd；pass_filters才是本次默认硬过滤结论。",
                    "dataset": "candidates",
                    "source": candidate_source,
                    "density": "compact",
                    "layout": "full",
                    "defaultSort": {"field": "rank", "direction": "asc"},
                    "columns": [
                        {"field": "candidate", "label": "候选", "type": "text"},
                        {"field": "raw_id", "label": "BoltzGen原始ID", "type": "text"},
                        {"field": "rank", "label": "批内排名"},
                        {"field": "pass_filters", "label": "全部通过", "type": "boolean"},
                        {"field": "full_vhh_length", "label": "VHH aa"},
                        {"field": "design_to_target_iptm", "label": "CDR→目标 iPTM"},
                        {"field": "min_design_to_target_pae_A", "label": "最小PAE Å"},
                        {"field": "complex_rmsd_A", "label": "复合物RMSD Å"},
                        {"field": "design_rmsd_A", "label": "设计区RMSD Å"},
                        {"field": "hotspot_contacts_under_8A", "label": "热点8Å内接触"},
                        {"field": "hbonds", "label": "氢键"},
                        {"field": "salt_bridges", "label": "盐桥"},
                        {"field": "target_delta_sasa_A2", "label": "目标ΔSASA Å²"},
                    ],
                },
                {
                    "id": "output_table",
                    "title": "模型输出文件清单",
                    "subtitle": "按阶段汇总文件数、格式与正确用途；字节数是本次本地快照。",
                    "dataset": "output_inventory",
                    "source": inventory_source,
                    "density": "compact",
                    "layout": "full",
                    "defaultSort": {"field": "stage_order", "direction": "asc"},
                    "columns": [
                        {"field": "stage_order", "label": "顺序"},
                        {"field": "stage", "label": "阶段", "type": "text"},
                        {"field": "role", "label": "输入/输出角色", "type": "text"},
                        {"field": "format", "label": "格式", "type": "text"},
                        {"field": "file_count", "label": "文件数"},
                        {"field": "bytes", "label": "字节"},
                        {"field": "interpretation", "label": "正确用法", "type": "text"},
                    ],
                },
            ],
            "sources": [
                summary_source,
                candidate_source,
                stage_source,
                funnel_source,
                inventory_source,
                {"id": "input_manifest", "label": "冻结输入manifest", "path": "outputs/02_mps_run/input_manifest.json"},
                {"id": "boltzgen_release", "label": "BoltzGen v0.3.2", "href": "https://github.com/HannesStark/boltzgen/releases/tag/v0.3.2"},
                {"id": "mps_pr", "label": "实验性Apple Silicon MPS PR #145", "href": "https://github.com/HannesStark/boltzgen/pull/145"},
            ],
            "blocks": [
                {
                    "id": "title",
                    "type": "markdown",
                    "body": "# BoltzGen nanobody-anything MVP：真实运行、结果与评价\n\n**结论先行：推理链路在本机实验性MPS环境中完整跑通，但2个候选均未通过默认结构过滤。本次成功证明的是工程流程可运行，不是蛋白已经可用。**",
                },
                {"id": "headline_metrics", "type": "metric-strip", "cardIds": ["requested_card", "completed_card", "passed_card", "runtime_card"]},
                {
                    "id": "key_findings",
                    "type": "markdown",
                    "sourceId": "candidate_source",
                    "body": "## 关键发现\n\n- **2/2完成计算，0/2通过过滤。**两者都在复合物骨架RMSD、设计区骨架RMSD和His7/Ala8热点接触三项失败。\n- 候选1（BoltzGen原始ID后缀`_0`）的批内排名为1，但其复合物RMSD为**10.82 Å**、设计区RMSD为**3.79 Å**，不能因为被复制到`final_1_designs`目录就称为通过。\n- 本次只跑了7–36正靶，且没有原子级确认C端酰胺；因此不能评价7–36/9–36选择性，也不能报告Kd、结合概率或倍数选择性。",
                },
                {"id": "input_details", "type": "html", "body": input_details},
                {
                    "id": "method_intro",
                    "type": "markdown",
                    "body": "## 方法与模型设置\n\n官方v0.3.2在macOS上有CUDA专用依赖并会调用CUDA设备能力；本次使用尚未合并的MPS PR #145，属于**实验性兼容性测试**。为适配18 GB统一内存，仅生成2个候选，design/folding各50步、inverse folding 30步、每个候选1个复折叠样本，全部使用FP32。",
                },
                {"id": "process_details", "type": "html", "body": process_details},
                {"id": "stage_chart", "type": "chart", "chartId": "stage_timing_chart"},
                {"id": "funnel_chart_block", "type": "chart", "chartId": "funnel_chart"},
                {
                    "id": "result_heading",
                    "type": "markdown",
                    "body": "## 结果与候选判读\n\n管线的文件产出完整；质量问题集中在复折叠后几何不自洽以及没有贴近His7/Ala8热点。小样本只适合逐候选核对，不适合做分布、相关性或成功率推断。",
                },
                {"id": "rmsd_chart_block", "type": "chart", "chartId": "rmsd_chart"},
                {"id": "candidate_table_block", "type": "table", "tableId": "candidate_table", "layout": "full"},
                {"id": "proxy_visual", "type": "html", "body": proxy_visual, "sourceId": "candidate_source"},
                {"id": "candidate_details", "type": "html", "body": candidate_details},
                {"id": "structure_visual", "type": "html", "body": structure_visual, "sourceId": "candidate_source"},
                {
                    "id": "evaluation_contract",
                    "type": "markdown",
                    "body": "## 如何评价这次尝试\n\n1. **工程成功**：`check → configure → design → inverse_folding → folding → analysis → filtering`全部返回成功，输入哈希和日志齐全。\n2. **计算质量未达标**：官方默认要求两类RMSD≤2.5 Å；本批四个值全部超阈值，而且两个候选在His7/Ala8热点8 Å内的设计残基接触数均为0。\n3. **不把代理指标当实验量**：iPTM、PAE、几何氢键、盐桥和ΔSASA仅用于同一计算设置内排序，不能换算成Kd或结合概率。\n4. **不声称型态选择性**：只有7–36正靶，无完整9–36匹配构象与成对重预测。\n5. **不进入合成**：本批最合理处置是保留为流程验证记录，修正计算设置后重跑，而不是放宽阈值包装成命中。",
                },
                {"id": "output_table_block", "type": "table", "tableId": "output_table", "layout": "full"},
                {"id": "output_details", "type": "html", "body": output_details},
                {
                    "id": "limitations",
                    "type": "markdown",
                    "body": "## 限制与稳健性\n\n- MPS代码来自未合并PR，无官方稳定性保证；本次结果不等价于Linux+NVIDIA官方基线。\n- 采样步数、recycling和复折叠样本数均大幅缩减，目的是验证链路而非最大化候选质量。\n- BoltzGen CLI未暴露统一随机种子，本次输出不能保证逐字节重复。\n- 7XL0只是官方示例scaffold，尚未经过项目级表达、稳定性和家族覆盖审核。\n- GLP-1 C端酰胺未在本次标准聚合物CIF中原子级闭环；9–36反靶未运行。\n- `nanobody-anything`跳过affinity和VHH单体design_folding；本次没有亲和力或独立折叠验证。",
                },
                {
                    "id": "next_steps",
                    "type": "markdown",
                    "body": "## 下一步决策\n\n1. 先把同一输入迁移到Linux+NVIDIA环境，恢复更充分的sampling/recycling，并生成至少50个候选；仍保留默认硬过滤。\n2. 建立项目审核后的多VHH框架库，不把7XL0示例当生产库。\n3. 对7–36和匹配构象的9–36分别做多构象、同设置复折叠；用正靶最差值与反靶最好值做保守计算代理。\n4. 单独解决C端酰胺的原子级输入与输出往返验证。\n5. 只有通过结构门槛和可开发性门槛的候选才进入表达；随后用SPR/BLI和混合样本捕获LC–MS产生真实Kd/回收率/选择性标签。\n\n### 进一步问题\n\n- 恢复完整采样后，His7/Ala8热点接触是否仍为0？\n- 不同VHH框架是否改变对肽N端的几何可达性？\n- 端酰胺被显式建模后，候选排序是否稳定？",
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "summary": summary_rows,
                "stage_timings": stage_rows,
                "funnel": funnel_rows,
                "rmsd": rmsd_rows,
                "candidates": candidates,
                "output_inventory": output_inventory,
            },
        },
        "package_info": {
            "mode": "portable_html",
            "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False},
        },
    }

    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    output_path = REPORT_ROOT / "report_artifact.json"
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(output_path),
                "candidate_rows": len(candidates),
                "stage_rows": len(stage_rows),
                "passed": passed_count,
                "blocks": len(artifact["manifest"]["blocks"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
