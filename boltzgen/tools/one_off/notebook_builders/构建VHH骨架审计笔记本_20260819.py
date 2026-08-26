#!/usr/bin/env python3
"""生成并执行 SAbDab2 VHH scaffold 筛选复盘 Notebook。

Notebook 不重新下载数据，也不重新运行结构筛选。它读取已经保存的 TSV/SQLite
结果，复算关键计数、主键、漏斗、排除原因和入选面板对账，作为可查看的审计
伴随文件。真正的结构解析逻辑在 ``build_scaffold_database.py`` 中。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import nbformat as nbf
from nbclient import NotebookClient


def markdown(text: str):
    return nbf.v4.new_markdown_cell(text)


def code(text: str):
    return nbf.v4.new_code_cell(text)


def build_notebook(root: Path):
    notebook = nbf.v4.new_notebook()
    notebook["metadata"] = {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
    }
    notebook["cells"] = [
        markdown(
            "# SAbDab2 VHH scaffold 筛选复盘\n\n"
            "本 Notebook 读取冻结后的筛选结果，复算统计与一致性检查。"
            "它不把‘结构模板合格’解释成‘已经结合 GLP‑1’。"
        ),
        code(
            "# 所有路径相对于数据库根目录；避免把个人机器的绝对路径写入结果表。\n"
            "from pathlib import Path\n"
            "import json, sqlite3\n"
            "import pandas as pd\n\n"
            "ROOT = Path.cwd()\n"
            "summary = json.loads((ROOT/'registry/database_summary.json').read_text())\n"
            "instances = pd.read_csv(ROOT/'registry/antibody_instances.tsv', sep='\\t', low_memory=False)\n"
            "candidates = pd.read_csv(ROOT/'registry/scaffold_candidates.tsv', sep='\\t')\n"
            "residues = pd.read_csv(ROOT/'registry/residue_map.tsv', sep='\\t', low_memory=False)\n"
            "selected = pd.read_csv(ROOT/'registry/selected_scaffolds.tsv', sep='\\t')\n"
            "validation = pd.read_csv(ROOT/'registry/boltzgen_export_validation.tsv', sep='\\t')\n"
            "funnel = pd.read_csv(ROOT/'qc/screening_funnel.tsv', sep='\\t')\n"
            "exclusions = pd.read_csv(ROOT/'qc/exclusion_log.tsv', sep='\\t')\n"
            "summary['counts']"
        ),
        markdown("## 原始数据粒度与主键"),
        code(
            "# INSTANCE 应是一行一个 antibody instance；SABDAB_ID 允许重复，代表同一变量域的多次结构观测。\n"
            "pd.DataFrame({\n"
            "    'metric':['rows','unique INSTANCE','unique PDB','unique SABDAB_ID'],\n"
            "    'value':[len(instances), instances.INSTANCE.nunique(), instances.PDB.nunique(), instances.SABDAB_ID.nunique()]\n"
            "})"
        ),
        code(
            "# 主键和筛选漏斗必须满足确定性对账条件。\n"
            "assert instances.INSTANCE.is_unique\n"
            "assert candidates.candidate_id.is_unique\n"
            "assert selected.candidate_id.is_unique\n"
            "assert selected.selection_rank.is_unique\n"
            "assert not residues.duplicated(['candidate_id','imgt_position']).any()\n"
            "assert funnel.remaining_count.is_monotonic_decreasing\n"
            "assert int(funnel.iloc[-1].remaining_count) == len(selected)\n"
            "print('主键与漏斗对账：PASS')"
        ),
        markdown("## 筛选漏斗"),
        code("funnel.style.format({'remaining_count':'{:,}'})"),
        markdown("## 首个排除原因"),
        code(
            "reason_counts = (exclusions.query(\"first_exclusion_reason != 'SELECTED'\")\n"
            "                 .first_exclusion_reason.value_counts().rename_axis('reason').reset_index(name='count'))\n"
            "assert reason_counts['count'].sum() + (exclusions.first_exclusion_reason=='SELECTED').sum() == len(instances)\n"
            "reason_counts"
        ),
        markdown("## 结构 QC 结果"),
        code(
            "# 这里只统计进入 metadata-qualified pool 的结构实例。\n"
            "candidates.groupby('hard_status', dropna=False).size().rename('count').reset_index()"
        ),
        code(
            "# 展示最常见硬失败类别；完整逐条证据保存在 qc/qc_results.tsv。\n"
            "failed = candidates.query(\"hard_status == 'FAIL'\").copy()\n"
            "(failed.hard_reasons.fillna('').str.split(' | ', regex=False).explode()\n"
            " .str.split(':').str[0].value_counts().head(15).rename_axis('rule').reset_index(name='count'))"
        ),
        markdown("## 最终 scaffold 面板"),
        code(
            "columns = ['selection_rank','role','candidate_id','pdb_code','sabdab_id','heavy_species',\n"
            "           'resolution_a','variable_length_aa','cdr3_length_aa','quality_score',\n"
            "           'framework_cluster_id','canonical_disulfide_rcsb_crosschecked','benchmark_7xl0',\n"
            "           'boltzgen_check_status']\n"
            "selected[columns].sort_values('selection_rank')"
        ),
        code(
            "# 入选面板的结构包必须完整存在。\n"
            "required_files = ['scaffold.cif','scaffold.yaml','residue_mapping.tsv','curation.json','qc.json']\n"
            "missing = []\n"
            "for package in selected.package_path:\n"
            "    for name in required_files:\n"
            "        if not (ROOT/package/name).is_file(): missing.append(f'{package}/{name}')\n"
            "assert not missing, missing\n"
            "actual_packages = {str(path.relative_to(ROOT)) for path in (ROOT/'selected').iterdir() if path.is_dir()}\n"
            "expected_packages = set(selected.package_path)\n"
            "assert actual_packages == expected_packages\n"
            "assert set(validation.candidate_id) == set(selected.candidate_id)\n"
            "assert (validation.boltzgen_check_status == 'PASS').all()\n"
            "assert (validation.target_residue_count == 30).all()\n"
            "assert (validation.target_role == 'geometry_only').all()\n"
            "assert (~validation.terminal_amide_atomically_verified).all()\n"
            "print(f'{len(selected)} 个 scaffold 包文件完整且 BoltzGen check 全通过：PASS')"
        ),
        markdown(
            "## 结论边界\n\n"
            "- 该库证明的是数据来源、编号、坐标、规范二硫键和框架多样性可追溯。\n"
            "- BoltzGen check PASS 只证明 30 残基 target 几何与 scaffold 输入合同可解析；C 端酰胺尚未原子级验证。\n"
            "- 它没有提供 GLP‑1 结合标签，也没有提供 7–36NH₂ 对 9–36NH₂ 的选择性标签。\n"
            "- 下一步必须让每个骨架获得相同生成预算，并用统一正/负靶重预测与实验闭环比较。"
        ),
    ]
    return notebook


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    output = root / "notebooks" / "SAbDab2_VHH骨架筛选复盘.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    notebook = build_notebook(root)
    client = NotebookClient(notebook, timeout=300, kernel_name="python3", resources={"metadata": {"path": str(root)}})
    executed = client.execute()
    nbf.write(executed, output)
    print(output)


if __name__ == "__main__":
    main()
