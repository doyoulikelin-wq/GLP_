#!/usr/bin/env python3
"""BoltzGen nanobody MVP 真实输出的只读审计、汇总与可视化。

本脚本有三个不可妥协的约束：

1. 只读取 ``inputs/``、``logs/`` 和 ``outputs/02_mps_run/`` 中已经存在的真实文件；
2. 只把新文件写入 ``analysis/``，绝不重算、覆盖或“修饰”BoltzGen 原始结果；
3. 把计算代理与实验事实严格区分，不把 iPTM、PAE、RMSD、接触数或 ΔSASA
   解释为 Kd、结合概率、实验命中或 7–36/9–36 选择性。

运行方式（使用本次 MVP 已安装好依赖的 Python 环境）：

    ./env/bin/python scripts/analyze_results.py

脚本刻意不导入 BoltzGen 项目代码。这样可以避免分析动作触发模型加载、缓存写入或对
原始目录的隐式修改；所有统计均来自落盘的 CSV、NPZ、CIF、JSON、YAML 与日志。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib

# 使用无窗口后端，保证在终端、CI 或没有图形桌面的环境中也能稳定生成图片。
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml


# ---------------------------------------------------------------------------
# 固定路径与审计常量
# ---------------------------------------------------------------------------

# 脚本位于 RUN_ROOT/scripts/；由脚本位置推导根目录，避免依赖当前工作目录。
RUN_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = RUN_ROOT / "inputs"
LOG_DIR = RUN_ROOT / "logs"
RUN_OUTPUT_DIR = RUN_ROOT / "outputs" / "02_mps_run"
PIPELINE_DIR = RUN_OUTPUT_DIR / "pipeline"
INTERMEDIATE_DIR = PIPELINE_DIR / "intermediate_designs"
INVERSE_DIR = PIPELINE_DIR / "intermediate_designs_inverse_folded"
FOLD_NPZ_DIR = INVERSE_DIR / "fold_out_npz"
REFOLD_CIF_DIR = INVERSE_DIR / "refold_cif"
FINAL_DIR = PIPELINE_DIR / "final_ranked_designs"

# 所有自编分析产物只能出现在该目录；该目录不属于原始运行输出目录。
ANALYSIS_DIR = RUN_ROOT / "analysis"
FIGURE_DIR = ANALYSIS_DIR / "figures"

# v0.3.2 默认的两个骨架 RMSD 硬过滤阈值。此次 resolved filtering.yaml 没有显式
# 写出构造函数默认值，但执行日志明确显示阈值为 2.5 Å。
RMSD_THRESHOLD_ANGSTROM = 2.5

# 图表统一配色。红色只表示“未通过默认过滤”，不表示实验失败。
COLORS = {
    "navy": "#12304A",
    "blue": "#277DA1",
    "teal": "#2A9D8F",
    "green": "#4D908E",
    "amber": "#F4A261",
    "red": "#D1495B",
    "purple": "#7B61A8",
    "gray": "#8796A5",
    "light": "#E8EEF3",
    "ink": "#1D2A35",
}


# ---------------------------------------------------------------------------
# 通用读写与类型清理工具
# ---------------------------------------------------------------------------

def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 SHA-256；不会把大文件一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """以可读、稳定的 UTF-8 JSON 格式写入分析目录。"""

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def json_scalar(value: Any) -> Any:
    """把 NumPy/Pandas 标量转换为标准 JSON 类型，并把 NaN/Inf 写成 null。"""

    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def to_bool(value: Any) -> bool:
    """稳健解析 CSV 中可能以 bool、0/1 或字符串形式出现的布尔值。"""

    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def rel(path: Path) -> str:
    """返回相对于运行根目录的 POSIX 路径，方便 HTML/JSON 跨机器阅读。"""

    return path.resolve().relative_to(RUN_ROOT).as_posix()


def configure_plot_style() -> None:
    """配置支持中文且适合报告阅读的 Matplotlib 全局样式。"""

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [
                "PingFang SC",
                "Hiragino Sans GB",
                "Heiti SC",
                "Arial Unicode MS",
                "DejaVu Sans",
            ],
            "axes.unicode_minus": False,
            "font.size": 11,
            "axes.titlesize": 16,
            "axes.labelsize": 12,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "figure.facecolor": "white",
            "axes.facecolor": "#F8FAFC",
            "axes.edgecolor": "#CAD5DF",
            "axes.labelcolor": COLORS["ink"],
            "text.color": COLORS["ink"],
            "grid.color": "#DDE5EC",
            "grid.linewidth": 0.8,
            "grid.alpha": 0.8,
        }
    )


def save_figure(fig: plt.Figure, filename: str) -> None:
    """统一以高分辨率 PNG 保存，并立即关闭图对象释放内存。"""

    fig.savefig(FIGURE_DIR / filename, dpi=210, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def required_file(path: Path) -> Path:
    """声明并检查关键输入；缺失时立即失败，避免生成看似完整的空报告。"""

    if not path.is_file():
        raise FileNotFoundError(f"缺少必需的真实运行文件：{path}")
    return path


def snapshot_protected_sources() -> dict[str, str]:
    """对所有受保护原始文件做哈希快照，用于运行后验证“只读”承诺。"""

    protected: dict[str, str] = {}
    for root in (INPUT_DIR, LOG_DIR, RUN_OUTPUT_DIR):
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            protected[rel(path)] = sha256_file(path)
    return protected


# ---------------------------------------------------------------------------
# 原始输出清点与 NPZ 数据契约
# ---------------------------------------------------------------------------

def classify_output(path: Path) -> tuple[str, bool]:
    """根据真实相对路径给运行输出分类，并标记是否属于主要科学结果。"""

    p = path.relative_to(RUN_OUTPUT_DIR).as_posix()
    if p in {"input_manifest.json", "mvp_run_status.json"}:
        return "run_manifest", True
    if "/config/" in f"/{p}" or p.endswith("steps.yaml"):
        return "resolved_configuration", True
    if "final_ranked_designs" in p and p.endswith(".csv"):
        return "final_metrics", True
    if "final_ranked_designs" in p and p.endswith(".pdf"):
        return "boltzgen_summary_pdf", True
    if "final_" in p and p.endswith(".cif") and "before_refolding" not in p:
        return "final_refolded_structure", True
    if "refold_cif" in p and p.endswith(".cif"):
        return "refolded_complex_structure", True
    if "fold_out_npz" in p and p.endswith(".npz"):
        return "folding_prediction_npz", True
    if "metrics_tmp" in p and p.endswith(".npz"):
        return "analysis_intermediate_npz", False
    if "aggregate_metrics" in p or "per_target_metrics" in p:
        return "analysis_metrics", True
    if "intermediate_designs_inverse_folded" in p and p.endswith(".cif"):
        return "inverse_folded_backbone_structure", False
    if "intermediate_designs_inverse_folded" in p and p.endswith(".npz"):
        return "inverse_folded_metadata", False
    if "intermediate_designs" in p and p.endswith(".cif"):
        return "raw_generated_structure", False
    if "intermediate_designs" in p and p.endswith(".npz"):
        return "raw_design_metadata", False
    return "auxiliary_output", False


def build_output_inventory() -> pd.DataFrame:
    """逐文件记录真实运行输出的大小、哈希、角色和修改时间。"""

    rows: list[dict[str, Any]] = []
    for path in sorted(p for p in RUN_OUTPUT_DIR.rglob("*") if p.is_file()):
        role, primary = classify_output(path)
        stat = path.stat()
        rows.append(
            {
                "relative_path": rel(path),
                "role": role,
                "is_primary_result": primary,
                "extension": path.suffix.lower() or "[none]",
                "size_bytes": stat.st_size,
                "modified_at_utc": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "sha256": sha256_file(path),
            }
        )
    return pd.DataFrame(rows)


# 每个 NPZ 键的轴和科学含义。未列出的动态键仍会被完整记录，只是语义标为“见源字段名”。
NPZ_KEY_SEMANTICS: dict[str, dict[str, str]] = {
    "design_mask": {
        "axes": "[token]；1=设计位点，0=固定/目标位点",
        "meaning": "逐 token 的设计掩码；在本项目中主要标记三段 CDR 设计区。",
    },
    "mol_type": {
        "axes": "[token] 或 [fold_sample, token]",
        "meaning": "逐 token 分子类型编码；本次两个实体均为蛋白质。",
    },
    "ss_type": {
        "axes": "[token]",
        "meaning": "逐 token 二级结构条件编码；是条件/元数据，不是坐标矩阵。",
    },
    "token_resolved_mask": {
        "axes": "[token]",
        "meaning": "逐 token 是否具有可用坐标。",
    },
    "binding_type": {
        "axes": "[token]",
        "meaning": "逐 token 结合位点提示；本输入中 GLP-1 的 His7/Ala8 被设为正向提示。",
    },
    "token_index": {
        "axes": "[fold_sample, token]",
        "meaning": "每个折叠样本中的 token 顺序编号。",
    },
    "res_type": {
        "axes": "[fold_sample, token, residue_category]",
        "meaning": "残基类别 one-hot/类别向量；最后一轴不是三维坐标。",
    },
    "atom_resolved_mask": {
        "axes": "[fold_sample, atom]",
        "meaning": "逐原子是否在该折叠样本中有效。",
    },
    "coords": {
        "axes": "[fold_sample, atom, xyz]",
        "meaning": "折叠输出的原子笛卡尔坐标；xyz 三列单位为 Å。",
    },
    "input_coords": {
        "axes": "[batch_or_sample, coordinate_copy, atom, xyz]",
        "meaning": "送入折叠步骤的条件坐标副本；最后一轴为 x/y/z（Å）。",
    },
    "atom_to_token": {
        "axes": "[fold_sample, atom, token]",
        "meaning": "布尔映射矩阵；某原子属于某 token 时元素为 True。行=原子，列=token。",
    },
    "backbone_mask": {
        "axes": "[fold_sample, atom]",
        "meaning": "逐原子骨架掩码，用于骨架 RMSD。",
    },
    "design_seq": {
        "axes": "[designed_residue]",
        "meaning": "设计位点残基的整数 token；三段 CDR 被顺序拼接。",
    },
    "ca_coords": {
        "axes": "[designed_residue, xyz]",
        "meaning": "生成结构中设计残基 Cα 坐标，xyz 单位为 Å。",
    },
    "ca_coords_refolded": {
        "axes": "[designed_residue, xyz]",
        "meaning": "重折叠结构中相同设计残基的 Cα 坐标，xyz 单位为 Å。",
    },
    "designed_sequence": {
        "axes": "标量字符串",
        "meaning": "三个设计区按序拼接的氨基酸序列，不含 CDR 分隔符。",
    },
    "designed_chain_sequence": {
        "axes": "标量字符串",
        "meaning": "完整 VHH 链序列。",
    },
}


SCALAR_CONFIDENCE_KEYS = {
    "min_interaction_pae",
    "min_design_to_target_pae",
    "interaction_pae",
    "ligand_iptm",
    "protein_iptm",
    "iptm",
    "design_iptm",
    "design_iiptm",
    "design_to_target_iptm",
    "design_residue_iptm",
    "design_ptm",
    "target_ptm",
    "ptm",
    "complex_plddt",
    "complex_iplddt",
    "complex_pde",
    "complex_ipde",
    "design_ipsae_min",
    "design_to_target_ipsae",
    "target_to_design_ipsae",
}


def role_for_npz(path: Path) -> str:
    """给 NPZ 文件标记它在管线中的输入/输出角色。"""

    p = path.as_posix()
    if "fold_out_npz" in p:
        return "folding_output_and_analysis_input"
    if "metrics_tmp/data_" in p:
        return "analysis_coordinate_cache"
    if "metrics_tmp/metrics_" in p:
        return "analysis_per_candidate_metrics_cache"
    if "intermediate_designs_inverse_folded" in p:
        return "inverse_folding_output_and_folding_input"
    return "design_output_and_inverse_folding_input"


def array_sample(array: np.ndarray, max_values: int = 6) -> list[Any]:
    """只截取少量元素作为 schema 样例，避免把大型矩阵复制进 JSON。"""

    flat = array.reshape(-1)
    return [json_scalar(v) for v in flat[:max_values]]


def build_npz_schema() -> dict[str, Any]:
    """逐个读取真实 NPZ，记录键、dtype、shape、轴语义与少量样例值。"""

    files: list[dict[str, Any]] = []
    for path in sorted(PIPELINE_DIR.rglob("*.npz")):
        entry: dict[str, Any] = {
            "path": rel(path),
            "role": role_for_npz(path),
            "size_bytes": path.stat().st_size,
            "arrays": [],
        }
        try:
            # allow_pickle=False 是安全边界：本分析不执行 NPZ 内潜在 Python 对象。
            with np.load(path, allow_pickle=False) as archive:
                for key in archive.files:
                    array = np.asarray(archive[key])
                    semantics = NPZ_KEY_SEMANTICS.get(key, {})
                    if key in SCALAR_CONFIDENCE_KEYS:
                        semantics = {
                            "axes": "[fold_sample] 或聚合后的标量",
                            "meaning": (
                                "Boltz-2 结构/界面置信代理；不是亲和力、Kd 或实验结合概率。"
                            ),
                        }
                    entry["arrays"].append(
                        {
                            "key": key,
                            "dtype": str(array.dtype),
                            "shape": list(array.shape),
                            "element_count": int(array.size),
                            "nbytes_uncompressed": int(array.nbytes),
                            "axis_semantics": semantics.get(
                                "axes", "动态标量或数组；按字段名与源配置解释"
                            ),
                            "scientific_meaning": semantics.get(
                                "meaning", "BoltzGen 中间/分析字段；本 JSON 不赋予额外实验含义。"
                            ),
                            "sample_values": array_sample(array),
                        }
                    )
        except Exception as exc:  # pragma: no cover - 仅用于异常产物的可审计降级
            entry["read_error"] = f"{type(exc).__name__}: {exc}"
        files.append(entry)

    fold_keys = {
        array["key"]
        for file_entry in files
        if file_entry["role"] == "folding_output_and_analysis_input"
        for array in file_entry.get("arrays", [])
    }
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "npz_file_count": len(files),
        "files": files,
        "important_absence": {
            "full_pae_matrix_present": False,
            "basis": (
                "fold_out_npz 仅观察到 interaction_pae、min_interaction_pae 和 "
                "min_design_to_target_pae 等汇总标量；未观察到二维 PAE 矩阵键。"
            ),
            "observed_fold_keys": sorted(fold_keys),
            "visualization_rule": "禁止据此伪造 PAE 热图。",
        },
    }


# ---------------------------------------------------------------------------
# 候选评价、序列相似度与阶段计时
# ---------------------------------------------------------------------------

def find_final_cif(candidate_id: str) -> Path | None:
    """按候选 ID 定位预算选择目录中的重折叠 CIF，不解析 rank 前缀语义。"""

    matches = sorted(
        p
        for p in FINAL_DIR.glob("final_*_designs/*.cif")
        if p.name.endswith(f"{candidate_id}.cif") or candidate_id in p.name
    )
    return matches[0] if matches else None


def fold_sample_audit(candidate_id: str) -> dict[str, Any]:
    """核对 v0.3.2 中 Analysis 与 CIF Writer 是否选择了同一个折叠样本。"""

    path = FOLD_NPZ_DIR / f"{candidate_id}.npz"
    if not path.is_file():
        return {
            "fold_sample_count": 0,
            "analysis_best_sample_index": None,
            "writer_best_sample_index": None,
            "same_best_sample": None,
            "note": "缺少 fold_out_npz，无法审计。",
        }

    with np.load(path, allow_pickle=False) as z:
        required = {"design_to_target_iptm", "design_ptm", "iptm", "ptm"}
        missing = sorted(required - set(z.files))
        if missing:
            return {
                "fold_sample_count": None,
                "analysis_best_sample_index": None,
                "writer_best_sample_index": None,
                "same_best_sample": None,
                "note": f"缺少字段：{', '.join(missing)}",
            }
        analysis_score = 0.8 * z["design_to_target_iptm"] + 0.2 * z["design_ptm"]
        writer_score = 0.8 * z["iptm"] + 0.2 * z["ptm"]
        analysis_idx = int(np.argmax(analysis_score))
        writer_idx = int(np.argmax(writer_score))
        return {
            "fold_sample_count": int(len(np.asarray(z["iptm"]))),
            "analysis_best_sample_index": analysis_idx,
            "writer_best_sample_index": writer_idx,
            "same_best_sample": analysis_idx == writer_idx,
            "analysis_best_score": float(np.asarray(analysis_score)[analysis_idx]),
            "writer_best_score": float(np.asarray(writer_score)[writer_idx]),
            "note": (
                "本次只有一个折叠样本，因此两个选择规则必然一致。"
                if len(np.asarray(z["iptm"])) == 1
                else "两个选择规则一致。"
                if analysis_idx == writer_idx
                else "两个选择规则不一致；CIF/ΔSASA 与部分 CSV 指标可能不对应。"
            ),
        }


def build_candidate_metrics() -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    """从 all_designs_metrics 提取小而清晰的候选评价表，并保留科学边界。"""

    all_metrics_path = required_file(FINAL_DIR / "all_designs_metrics.csv")
    selected_files = sorted(FINAL_DIR.glob("final_designs_metrics_*.csv"))
    if not selected_files:
        raise FileNotFoundError("缺少 final_designs_metrics_<budget>.csv")

    all_df = pd.read_csv(all_metrics_path)
    selected_df = pd.read_csv(selected_files[0])
    selected_ids = set(selected_df["id"].astype(str))

    # 这些 pass_* 列才是真正的逐条件布尔结果。官方 num_filters_passed 在 v0.3.2
    # 的实现中是“连续前缀累计”而不是简单的通过项总数，因此另行计算清晰计数。
    pass_columns = [
        c
        for c in all_df.columns
        if c.startswith("pass_") and c.endswith("_filter")
    ]
    filter_name_cn = {
        "pass_has_x_filter": "未知残基 X",
        "pass_filter_rmsd_filter": "复合物骨架 RMSD≤2.5Å",
        "pass_filter_rmsd_design_filter": "VHH 设计部分骨架 RMSD≤2.5Å",
        "pass_bindsite_under_8rmsd_filter": "提示位点 8Å 内存在设计残基",
        "pass_CYS_fraction_filter": "设计区 Cys 比例=0",
        "pass_ALA_fraction_filter": "设计区 Ala 比例≤0.3",
        "pass_GLY_fraction_filter": "设计区 Gly 比例≤0.3",
        "pass_GLU_fraction_filter": "设计区 Glu 比例≤0.3",
        "pass_LEU_fraction_filter": "设计区 Leu 比例≤0.3",
        "pass_VAL_fraction_filter": "设计区 Val 比例≤0.3",
    }

    records: list[dict[str, Any]] = []
    for _, row in all_df.sort_values("final_rank").iterrows():
        candidate_id = str(row["id"])
        pass_values = {column: to_bool(row.get(column)) for column in pass_columns}
        failed = [filter_name_cn.get(c, c) for c, passed in pass_values.items() if not passed]
        selected = candidate_id in selected_ids
        final_cif = find_final_cif(candidate_id) if selected else None
        refold_cif = REFOLD_CIF_DIR / f"{candidate_id}.cif"
        sample_audit = fold_sample_audit(candidate_id)

        record = {
            "candidate_id": candidate_id,
            "source_file_name": row.get("file_name"),
            "final_rank_within_this_run": int(row["final_rank"]),
            "selected_by_budget": selected,
            "pass_all_default_filters": to_bool(row.get("pass_filters")),
            "computed_filters_passed": sum(pass_values.values()),
            "computed_filters_total": len(pass_values),
            "failed_filter_count": len(failed),
            "failed_filters": "；".join(failed),
            "boltzgen_internal_num_filters_passed": row.get("num_filters_passed"),
            "interpretation": (
                "通过默认计算过滤"
                if to_bool(row.get("pass_filters"))
                else "预算排序输出，但未通过默认过滤"
                if selected
                else "未通过默认过滤，且未进入最终预算集合"
            ),
            "designed_sequence_concatenated": row.get("designed_sequence"),
            "full_vhh_sequence": row.get("designed_chain_sequence"),
            "designed_residue_count": row.get("num_design"),
            "quality_score_relative_rank_only": row.get("quality_score"),
            "filter_rmsd_angstrom": row.get("filter_rmsd"),
            "filter_rmsd_design_angstrom": row.get("filter_rmsd_design"),
            "rmsd_threshold_angstrom": RMSD_THRESHOLD_ANGSTROM,
            "binding_site_residues_within_8A": row.get("bindsite_under_8rmsd"),
            "design_to_target_iptm": row.get("design_to_target_iptm"),
            "design_iptm": row.get("design_iptm"),
            "design_ptm": row.get("design_ptm"),
            "design_ipsae_min": row.get("design_ipsae_min"),
            "min_design_to_target_pae_angstrom": row.get("min_design_to_target_pae"),
            "interaction_pae_angstrom": row.get("interaction_pae"),
            "complex_plddt": row.get("complex_plddt"),
            "delta_sasa_refolded_angstrom2": row.get("delta_sasa_refolded"),
            "interface_hbond_count": row.get("plip_hbonds_refolded"),
            "interface_saltbridge_atom_pair_count": row.get(
                "plip_saltbridge_refolded"
            ),
            "liability_score_proxy": row.get("liability_score"),
            "liability_violation_count": row.get("liability_num_violations"),
            "designed_region_loop_fraction": row.get("loop"),
            "designed_region_helix_fraction": row.get("helix"),
            "designed_region_sheet_fraction": row.get("sheet"),
            "refolded_cif": rel(refold_cif) if refold_cif.is_file() else "",
            "selected_final_cif": rel(final_cif) if final_cif else "",
            **sample_audit,
        }
        # 把每一个原始 pass 标志也带入精简表，便于复核失败原因。
        for column, passed in pass_values.items():
            record[column] = passed
        records.append(record)

    return pd.DataFrame(records), selected_df, selected_ids


def lcs_similarity(seq_a: str, seq_b: str) -> float:
    """复现 BoltzGen v0.3.2 过滤器所用的默认对齐分数/max(length)代理。

    默认 PairwiseAligner 对匹配计 1、错配和缺口计 0，最优分数等价于最长公共子序列
    长度。该值用于本批设计区多样性展示，但不是严格的逐位点序列一致率。
    """

    if not seq_a and not seq_b:
        return 1.0
    if not seq_a or not seq_b:
        return 0.0
    previous = [0] * (len(seq_b) + 1)
    for aa in seq_a:
        current = [0]
        for j, bb in enumerate(seq_b, start=1):
            if aa == bb:
                current.append(previous[j - 1] + 1)
            else:
                current.append(max(previous[j], current[-1]))
        previous = current
    return previous[-1] / max(len(seq_a), len(seq_b))


def build_sequence_identity(candidate_df: pd.DataFrame) -> pd.DataFrame:
    """构建设计位点拼接序列的两两 BoltzGen 内部相似度代理矩阵。"""

    ids = candidate_df["candidate_id"].astype(str).tolist()
    seqs = candidate_df["designed_sequence_concatenated"].fillna("").astype(str).tolist()
    matrix = np.zeros((len(ids), len(ids)), dtype=float)
    for i, seq_i in enumerate(seqs):
        for j, seq_j in enumerate(seqs):
            matrix[i, j] = lcs_similarity(seq_i, seq_j)
    result = pd.DataFrame(matrix, index=ids, columns=ids)
    result.index.name = "candidate_id"
    return result


def build_stage_timings(status: dict[str, Any]) -> pd.DataFrame:
    """合并 wrapper JSON 和执行日志中的逐阶段耗时，并显式记录协议跳过步骤。"""

    rows: list[dict[str, Any]] = []
    for stage in status.get("stages", []):
        if stage.get("stage") == "03_execute":
            continue
        rows.append(
            {
                "order": len(rows) + 1,
                "category": "preparation",
                "stage": stage.get("stage"),
                "display_name_cn": {
                    "01_check": "输入检查",
                    "02_configure": "配置解析",
                }.get(stage.get("stage"), stage.get("stage")),
                "elapsed_seconds": stage.get("elapsed_seconds"),
                "status": "completed" if stage.get("return_code") == 0 else "failed",
                "source": "outputs/02_mps_run/mvp_run_status.json",
            }
        )

    log_path = required_file(LOG_DIR / "03_execute.log")
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    observed = {
        match.group(1): float(match.group(2))
        for match in re.finditer(
            r"✓ Step ([a-z_]+) completed successfully in ([0-9.]+)s", log_text
        )
    }
    pipeline_order = [
        ("design", "扩散生成"),
        ("inverse_folding", "逆折叠赋序列"),
        ("folding", "复合物重折叠"),
        ("design_folding", "VHH 单独重折叠"),
        ("affinity", "亲和力预测"),
        ("analysis", "指标分析"),
        ("filtering", "过滤与排序"),
    ]
    for stage_name, display in pipeline_order:
        skipped = stage_name in {"design_folding", "affinity"}
        rows.append(
            {
                "order": len(rows) + 1,
                "category": "pipeline",
                "stage": stage_name,
                "display_name_cn": display,
                "elapsed_seconds": observed.get(stage_name),
                "status": "skipped_by_nanobody_protocol" if skipped else "completed",
                "source": "logs/03_execute.log",
            }
        )

    execute = next(
        (s for s in status.get("stages", []) if s.get("stage") == "03_execute"), None
    )
    if execute:
        rows.append(
            {
                "order": len(rows) + 1,
                "category": "wrapper_total",
                "stage": "03_execute_total",
                "display_name_cn": "execute 包装器总耗时",
                "elapsed_seconds": execute.get("elapsed_seconds"),
                "status": "completed" if execute.get("return_code") == 0 else "failed",
                "source": "outputs/02_mps_run/mvp_run_status.json",
            }
        )
    return pd.DataFrame(rows)


def paired_count(directory: Path) -> int:
    """统计同 stem 的 CIF+NPZ 成对产物，避免只看到半成品就算成功。"""

    cif_stems = {p.stem for p in directory.glob("*.cif")}
    npz_stems = {p.stem for p in directory.glob("*.npz")}
    return len(cif_stems & npz_stems)


def build_process_funnel(
    manifest: dict[str, Any], aggregate_df: pd.DataFrame, candidate_df: pd.DataFrame,
    selected_ids: set[str]
) -> pd.DataFrame:
    """按落盘证据构建过程数量表；最终预算输出与过滤通过数故意分开。"""

    rows = [
        (1, "requested", "请求生成", int(manifest["requested_designs"]), "input_manifest.json"),
        (2, "raw_design_pairs", "原始 CIF+NPZ 成对", paired_count(INTERMEDIATE_DIR), "intermediate_designs"),
        (3, "inverse_fold_pairs", "逆折叠 CIF+NPZ 成对", paired_count(INVERSE_DIR), "intermediate_designs_inverse_folded"),
        (4, "fold_npz", "复合物重折叠 NPZ", len(list(FOLD_NPZ_DIR.glob("*.npz"))), "fold_out_npz"),
        (5, "refold_cif", "重折叠复合物 CIF", len(list(REFOLD_CIF_DIR.glob("*.cif"))), "refold_cif"),
        (6, "analyzed", "成功分析", len(aggregate_df), "aggregate_metrics_analyze.csv"),
        (7, "unique_ranked", "按设计序列去重后", len(candidate_df), "all_designs_metrics.csv"),
        (
            8,
            "pass_default_filters",
            "通过全部默认过滤",
            int(candidate_df["pass_all_default_filters"].sum()),
            "all_designs_metrics.csv/pass_filters",
        ),
        (
            9,
            "selected_by_budget",
            "预算排序输出（非通过数）",
            len(selected_ids),
            "final_designs_metrics_1.csv",
        ),
    ]
    records = []
    for order, key, label, count, source in rows:
        note = ""
        if key == "pass_default_filters":
            note = "科学筛选门：只有 pass_filters=True 才能称通过默认计算过滤。"
        elif key == "selected_by_budget":
            note = (
                "BoltzGen 在通过数不足预算时仍会输出排序候选；本次 1 个输出候选未通过过滤。"
            )
        records.append(
            {
                "order": order,
                "stage_key": key,
                "stage_label_cn": label,
                "count": count,
                "source": source,
                "note": note,
            }
        )
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# CIF 解析和绘图
# ---------------------------------------------------------------------------

def parse_atom_site_ca(cif_path: Path) -> dict[str, np.ndarray]:
    """从 mmCIF 的 _atom_site loop 读取模型 1 的 Cα 轨迹。

    这里只读取 ATOM 行的 label_atom_id、label_asym_id 与 xyz，不修改结构，也不把
    B-factor 当成实验 B 因子。返回值为 ``链ID -> [residue, xyz]`` 的坐标矩阵。
    """

    lines = cif_path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() != "loop_":
            i += 1
            continue
        j = i + 1
        headers: list[str] = []
        while j < len(lines) and lines[j].strip().startswith("_"):
            headers.append(lines[j].strip())
            j += 1
        if "_atom_site.group_PDB" not in headers:
            i = j
            continue

        index = {name: idx for idx, name in enumerate(headers)}
        required = [
            "_atom_site.group_PDB",
            "_atom_site.label_atom_id",
            "_atom_site.label_asym_id",
            "_atom_site.Cartn_x",
            "_atom_site.Cartn_y",
            "_atom_site.Cartn_z",
        ]
        missing = [name for name in required if name not in index]
        if missing:
            raise ValueError(f"{cif_path} 的 atom_site 缺少列：{missing}")

        chain_coords: dict[str, list[list[float]]] = {}
        while j < len(lines):
            text = lines[j].strip()
            if not text or text == "#" or text == "loop_" or text.startswith("_"):
                break
            values = shlex.split(text)
            if len(values) < len(headers):
                raise ValueError(f"无法解析 atom_site 行 {j + 1}：字段数不足")
            if values[index["_atom_site.group_PDB"]] != "ATOM":
                j += 1
                continue
            if values[index["_atom_site.label_atom_id"]] != "CA":
                j += 1
                continue
            if "_atom_site.pdbx_PDB_model_num" in index:
                if values[index["_atom_site.pdbx_PDB_model_num"]] != "1":
                    j += 1
                    continue
            chain = values[index["_atom_site.label_asym_id"]]
            xyz = [
                float(values[index["_atom_site.Cartn_x"]]),
                float(values[index["_atom_site.Cartn_y"]]),
                float(values[index["_atom_site.Cartn_z"]]),
            ]
            chain_coords.setdefault(chain, []).append(xyz)
            j += 1
        return {chain: np.asarray(coords, dtype=float) for chain, coords in chain_coords.items()}
    raise ValueError(f"{cif_path} 中未找到 _atom_site loop")


def plot_process_funnel(funnel_df: pd.DataFrame) -> None:
    """画数量审计条形图；不用会掩盖“0 通过但 1 输出”的传统漏斗面积图。"""

    fig, ax = plt.subplots(figsize=(11, 6.8))
    labels = funnel_df["stage_label_cn"].tolist()[::-1]
    counts = funnel_df["count"].tolist()[::-1]
    keys = funnel_df["stage_key"].tolist()[::-1]
    colors = [
        COLORS["red"] if k == "pass_default_filters" else COLORS["amber"]
        if k == "selected_by_budget" else COLORS["teal"]
        for k in keys
    ]
    bars = ax.barh(labels, counts, color=colors, height=0.62)
    ax.set_xlim(0, max(max(counts), 1) + 0.65)
    ax.set_xlabel("候选数量")
    ax.set_title("MVP 过程数量审计：最终预算输出不等于过滤通过")
    ax.xaxis.grid(True)
    ax.yaxis.grid(False)
    for bar, count in zip(bars, counts):
        ax.text(
            bar.get_width() + 0.06,
            bar.get_y() + bar.get_height() / 2,
            str(count),
            va="center",
            # macOS 的 PingFang 没有名为 ``bold`` 的独立字体文件；直接指定
            # 数值字重 600，既保留强调效果，也避免 Matplotlib 的字体回退警告。
            fontweight=600,
        )
    ax.text(
        0.99,
        0.02,
        "红色=默认过滤通过数；橙色=预算机制仍写出的排序候选",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        color=COLORS["gray"],
        fontsize=9,
    )
    save_figure(fig, "process_funnel.png")


def plot_stage_timings(timing_df: pd.DataFrame) -> None:
    """画真实逐阶段耗时，并把 nanobody 协议跳过步骤显示为灰色。"""

    plot_df = timing_df[timing_df["category"] == "pipeline"].copy()
    plot_df["plot_seconds"] = plot_df["elapsed_seconds"].fillna(0.0)
    fig, ax = plt.subplots(figsize=(10.5, 6.2))
    colors = [
        COLORS["gray"] if str(status).startswith("skipped") else COLORS["blue"]
        for status in plot_df["status"]
    ]
    bars = ax.barh(
        plot_df["display_name_cn"][::-1],
        plot_df["plot_seconds"][::-1],
        color=colors[::-1],
        height=0.62,
    )
    ax.set_xlabel("耗时（秒）")
    ax.set_title("BoltzGen nanobody-anything 各阶段真实耗时")
    ax.xaxis.grid(True)
    ax.yaxis.grid(False)
    reversed_rows = list(plot_df.iloc[::-1].itertuples(index=False))
    for bar, row in zip(bars, reversed_rows):
        label = "协议跳过" if str(row.status).startswith("skipped") else f"{row.plot_seconds:.1f}s"
        ax.text(
            bar.get_width() + 1.0,
            bar.get_y() + bar.get_height() / 2,
            label,
            va="center",
            color=COLORS["gray"] if "跳过" in label else COLORS["ink"],
        )
    ax.set_xlim(0, max(plot_df["plot_seconds"].max(), 1) * 1.22)
    save_figure(fig, "stage_timings.png")


def plot_rmsd_threshold(candidate_df: pd.DataFrame) -> None:
    """对比两个骨架自洽 RMSD 与官方 2.5 Å 默认阈值。"""

    ids = [f"候选 {rank}" for rank in candidate_df["final_rank_within_this_run"]]
    x = np.arange(len(ids))
    width = 0.34
    fig, ax = plt.subplots(figsize=(9.5, 5.8))
    bars1 = ax.bar(
        x - width / 2,
        candidate_df["filter_rmsd_angstrom"],
        width,
        label="复合物骨架 RMSD",
        color=COLORS["blue"],
    )
    bars2 = ax.bar(
        x + width / 2,
        candidate_df["filter_rmsd_design_angstrom"],
        width,
        label="VHH 设计部分骨架 RMSD",
        color=COLORS["amber"],
    )
    ax.axhline(
        RMSD_THRESHOLD_ANGSTROM,
        color=COLORS["red"],
        linestyle="--",
        linewidth=2,
        label="默认过滤阈值 2.5 Å",
    )
    ax.set_xticks(x, ids)
    ax.set_ylabel("RMSD（Å，低更好）")
    ax.set_title("结构自洽性：两个候选均未通过默认 RMSD 门槛")
    # 图例放到横轴下方，避免遮挡 10–12 Å 的柱顶数值；三列并排可在不压缩
    # 主绘图区的前提下保留“两个指标 + 一条阈值线”的完整对应关系。
    ax.legend(
        frameon=False,
        ncol=3,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
    )
    fig.subplots_adjust(bottom=0.23)
    ax.yaxis.grid(True)
    ax.xaxis.grid(False)
    for bars in (bars1, bars2):
        ax.bar_label(bars, fmt="%.2f", padding=3, fontsize=9)
    save_figure(fig, "rmsd_threshold.png")


def plot_interface_proxies(candidate_df: pd.DataFrame) -> None:
    """分面展示无统一阈值的置信代理和 PAE，避免不同量纲被强行合成。"""

    labels = [f"候选 {rank}" for rank in candidate_df["final_rank_within_this_run"]]
    x = np.arange(len(labels))
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.7))

    confidence_metrics = [
        ("design_to_target_iptm", "CDR-目标 iPTM", COLORS["teal"]),
        ("design_ptm", "VHH pTM", COLORS["blue"]),
        ("design_ipsae_min", "界面 ipSAE(min)", COLORS["purple"]),
    ]
    width = 0.23
    for offset, (column, label, color) in enumerate(confidence_metrics):
        bars = axes[0].bar(
            x + (offset - 1) * width,
            candidate_df[column],
            width,
            label=label,
            color=color,
        )
        axes[0].bar_label(bars, fmt="%.3f", padding=2, fontsize=8, rotation=90)
    axes[0].set_xticks(x, labels)
    axes[0].set_ylim(0, 0.78)
    axes[0].set_ylabel("分数（0–1，高更好）")
    axes[0].set_title("结构/界面置信代理")
    axes[0].legend(frameon=False, fontsize=9)
    axes[0].yaxis.grid(True)
    axes[0].xaxis.grid(False)

    pae_metrics = [
        ("min_design_to_target_pae_angstrom", "最小 CDR→目标 PAE", COLORS["amber"]),
        ("interaction_pae_angstrom", "平均界面 PAE", COLORS["red"]),
    ]
    width = 0.34
    for offset, (column, label, color) in enumerate(pae_metrics):
        bars = axes[1].bar(
            x + (offset - 0.5) * width,
            candidate_df[column],
            width,
            label=label,
            color=color,
        )
        axes[1].bar_label(bars, fmt="%.2f", padding=2, fontsize=9)
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("PAE（Å，低更好）")
    axes[1].set_title("PAE 汇总标量（不是 PAE 矩阵）")
    axes[1].legend(frameon=False, fontsize=9)
    axes[1].yaxis.grid(True)
    axes[1].xaxis.grid(False)

    fig.suptitle("候选界面计算代理：只用于本批相对比较，不代表亲和力", y=1.03)
    fig.tight_layout()
    save_figure(fig, "interface_proxies.png")


def plot_interface_geometry(candidate_df: pd.DataFrame) -> None:
    """分别展示目标侧 ΔSASA 与界面几何接触计数。"""

    labels = [f"候选 {rank}" for rank in candidate_df["final_rank_within_this_run"]]
    x = np.arange(len(labels))
    fig, axes = plt.subplots(1, 2, figsize=(12.2, 5.5))

    bars = axes[0].bar(
        x,
        candidate_df["delta_sasa_refolded_angstrom2"],
        color=COLORS["teal"],
        width=0.58,
    )
    axes[0].bar_label(bars, fmt="%.0f Å²", padding=4)
    axes[0].set_xticks(x, labels)
    axes[0].set_ylabel("目标侧 ΔSASA（Å²，高表示更多遮蔽）")
    axes[0].set_title("目标因 VHH 存在而减少的 SASA")
    axes[0].yaxis.grid(True)
    axes[0].xaxis.grid(False)

    width = 0.34
    hb = axes[1].bar(
        x - width / 2,
        candidate_df["interface_hbond_count"],
        width,
        label="氢键几何计数",
        color=COLORS["blue"],
    )
    sb = axes[1].bar(
        x + width / 2,
        candidate_df["interface_saltbridge_atom_pair_count"],
        width,
        label="盐桥带电原子对计数",
        color=COLORS["amber"],
    )
    axes[1].bar_label(hb, fmt="%.0f", padding=3)
    axes[1].bar_label(sb, fmt="%.0f", padding=3)
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("计数（高表示几何接触更多）")
    axes[1].set_title("界面非共价接触代理")
    axes[1].legend(frameon=False)
    axes[1].yaxis.grid(True)
    axes[1].xaxis.grid(False)

    fig.suptitle("界面几何代理不是结合能、Kd 或实验结合证据", y=1.03)
    fig.tight_layout()
    save_figure(fig, "interface_geometry.png")


def plot_sequence_identity(identity_df: pd.DataFrame, candidate_df: pd.DataFrame) -> None:
    """画设计区拼接序列的两两内部相似度代理热图。"""

    labels = [
        f"候选 {rank}" for rank in candidate_df["final_rank_within_this_run"].tolist()
    ]
    matrix = identity_df.to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(6.4, 5.7))
    image = ax.imshow(matrix, vmin=0, vmax=1, cmap="Blues")
    ax.set_xticks(np.arange(len(labels)), labels)
    ax.set_yticks(np.arange(len(labels)), labels)
    ax.set_title("设计区序列相似度代理")
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            ax.text(
                j,
                i,
                f"{matrix[i, j]:.3f}",
                ha="center",
                va="center",
                color="white" if matrix[i, j] > 0.6 else COLORS["ink"],
                fontweight=600,
            )
    cbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("BoltzGen 内部对齐分数 / 较长序列长度")
    fig.text(
        0.5,
        0.01,
        "基于拼接设计位点；不是完整 VHH 的严格逐位点 identity",
        ha="center",
        color=COLORS["gray"],
        fontsize=9,
    )
    save_figure(fig, "sequence_identity.png")


def set_axes_equal_3d(ax: Any, arrays: Iterable[np.ndarray]) -> None:
    """让三维坐标轴使用相同比例，避免蛋白轨迹被视觉拉伸。"""

    all_coords = np.concatenate([a for a in arrays if len(a)], axis=0)
    mins = all_coords.min(axis=0)
    maxs = all_coords.max(axis=0)
    centers = (mins + maxs) / 2
    radius = max(maxs - mins) / 2
    ax.set_xlim(centers[0] - radius, centers[0] + radius)
    ax.set_ylim(centers[1] - radius, centers[1] + radius)
    ax.set_zlim(centers[2] - radius, centers[2] + radius)
    ax.set_box_aspect((1, 1, 1))


def plot_final_cif_ca_trace(candidate_df: pd.DataFrame) -> dict[str, Any]:
    """从预算输出的最终 CIF 画目标与 VHH 的三维 Cα 轨迹。"""

    selected = candidate_df[candidate_df["selected_by_budget"]]
    if selected.empty:
        return {"generated": False, "reason": "没有预算选择 CIF"}
    row = selected.sort_values("final_rank_within_this_run").iloc[0]
    cif_path = RUN_ROOT / str(row["selected_final_cif"])
    chains = parse_atom_site_ca(cif_path)
    if len(chains) < 2:
        return {"generated": False, "reason": "最终 CIF 少于两条含 Cα 的链"}

    # 本次 CIF 中 GLP-1 是较短的 30 aa 链，VHH 是较长的约 125 aa 链。
    chain_order = sorted(chains, key=lambda chain: len(chains[chain]))
    target_chain, vhh_chain = chain_order[0], chain_order[-1]
    target = chains[target_chain]
    vhh = chains[vhh_chain]

    fig = plt.figure(figsize=(9.2, 8.0))
    ax = fig.add_subplot(111, projection="3d")
    ax.plot(
        target[:, 0], target[:, 1], target[:, 2],
        color=COLORS["blue"], linewidth=3.2, label=f"GLP-1 目标（链 {target_chain}）"
    )
    ax.scatter(target[:, 0], target[:, 1], target[:, 2], color=COLORS["blue"], s=14)
    ax.plot(
        vhh[:, 0], vhh[:, 1], vhh[:, 2],
        color=COLORS["amber"], linewidth=2.2, label=f"VHH（链 {vhh_chain}）"
    )
    ax.scatter(vhh[:, 0], vhh[:, 1], vhh[:, 2], color=COLORS["amber"], s=8, alpha=0.8)

    # 输入把 GLP-1 的前两个残基 His7/Ala8 设为结合提示，单独用红点显示。
    hotspot_count = min(2, len(target))
    ax.scatter(
        target[:hotspot_count, 0],
        target[:hotspot_count, 1],
        target[:hotspot_count, 2],
        color=COLORS["red"], s=70, edgecolor="white", linewidth=0.9,
        label="输入提示位点 His7/Ala8",
    )
    set_axes_equal_3d(ax, [target, vhh])
    ax.set_xlabel("x（Å）")
    ax.set_ylabel("y（Å）")
    ax.set_zlabel("z（Å）")
    ax.view_init(elev=22, azim=-58)
    ax.legend(loc="upper left", frameon=False)
    ax.set_title(
        "最终预算输出 CIF 的 Cα 轨迹\n"
        f"{row['candidate_id']}；注意：该候选 pass_filters=False"
    )
    fig.text(
        0.5,
        0.02,
        "轨迹仅展示预测几何，不代表已证实结合；本候选未满足默认 RMSD/提示位点过滤。",
        ha="center",
        color=COLORS["gray"],
        fontsize=9,
    )
    save_figure(fig, "final_cif_ca_trace_3d.png")
    return {
        "generated": True,
        "candidate_id": row["candidate_id"],
        "source_cif": rel(cif_path),
        "target_chain": target_chain,
        "target_ca_count": len(target),
        "vhh_chain": vhh_chain,
        "vhh_ca_count": len(vhh),
    }


# ---------------------------------------------------------------------------
# 科学评价契约与总摘要
# ---------------------------------------------------------------------------

def build_evaluation_contract(
    manifest: dict[str, Any], candidate_df: pd.DataFrame
) -> dict[str, Any]:
    """把本次结果允许/禁止的解释写成机器可读契约。"""

    return {
        "schema_version": 1,
        "scope": "BoltzGen nanobody-anything 的 macOS MPS 冒烟运行结果审计",
        "input_role": {
            "design_spec": "GLP-1(7–36) 正靶结构 + 7XL0 VHH scaffold + 三段 CDR 设计",
            "binding_hint": "目标前两个残基 His7/Ala8；这是生成提示，不是选择性损失或实验标签。",
            "requested_designs": manifest.get("requested_designs"),
        },
        "protocol_steps": {
            "executed": ["design", "inverse_folding", "folding", "analysis", "filtering"],
            "skipped_by_protocol": ["design_folding", "affinity"],
            "consequence": [
                "没有 VHH 脱离目标后独立折叠的直接代理。",
                "没有亲和力预测字段，不能推断 Kd/IC50/Ki/ΔG。",
            ],
        },
        "hard_filter_contract": {
            "pass_field": "pass_filters",
            "required_for_claim": "只有 True 才可称‘通过默认计算过滤’。",
            "rmsd_threshold_angstrom": RMSD_THRESHOLD_ANGSTROM,
            "binding_site_rule": "bindsite_under_8rmsd >= 0.0001",
            "designed_cys_fraction_max": 0.0,
            "composition_fraction_max": {
                "ALA": 0.3,
                "GLY": 0.3,
                "GLU": 0.3,
                "LEU": 0.3,
                "VAL": 0.3,
            },
            "selection_warning": (
                "final_<budget>_designs 是预算排序输出；当通过数不足预算时仍可能含 "
                "pass_filters=False 的候选。"
            ),
        },
        "metric_semantics": {
            "filter_rmsd": "生成复合物与重折叠复合物的骨架自洽 RMSD（Å，低好），不是实验真值误差。",
            "filter_rmsd_design": "VHH 设计部分骨架自洽 RMSD（Å，低好）。",
            "design_to_target_iptm": "设计残基/CDR 与目标的结构界面置信代理（高好）。",
            "design_ptm": "VHH 链内部结构置信代理（高好）。",
            "min_design_to_target_pae": "最小跨界面 PAE（Å，低好）；可能由单一最自信接触主导。",
            "design_ipsae_min": "基于 PAE 的界面代理（高好），不是结合概率。",
            "delta_sasa_refolded": (
                "目标在 VHH 存在时减少的 SASA（Å²）；v0.3.2 该实现不是对称总埋藏面积。"
            ),
            "plip_hbonds_refolded": "Biotite/hydride 几何氢键计数；字段名含 plip 但不是结合能。",
            "plip_saltbridge_refolded": "0.5–5.5 Å 带电原子对计数，可能一个残基对贡献多个原子对。",
            "quality_score": "由本批 final_rank 线性映射的相对分数，不是成功率或概率。",
            "native_rmsd_fields": "native=False 时的 0 是占位值，禁止解释为对实验结构 RMSD=0。",
        },
        "allowed_claims": [
            "本地 experimental MPS 管线是否完成。",
            "每一阶段实际生成/分析的候选数量。",
            "候选是否通过本次配置中的默认计算过滤。",
            "候选在这 2 个样本中的相对排序和原始代理指标。",
            "最终 CIF 的预测 Cα 几何。",
        ],
        "prohibited_claims": [
            "Kd、Ki、IC50、ΔG、nM/µM 亲和力或结合概率。",
            "已证实结合、实验命中或具有捕获功能。",
            "对 GLP-1(9–36) 的选择性、选择倍数或排他性。",
            "对 C 端酰胺的原子级识别；当前标准 polymer CIF 未明确编码该化学差异。",
            "用两个候选估计总体分布、相关性、命中率或统计显著性。",
        ],
        "run_specific_limits": manifest.get("known_limits", []),
        "sample_size_rule": "n=2 只适合逐候选描述，不适合相关性、分布或校准结论。",
        "current_result": {
            "candidate_count": len(candidate_df),
            "default_filter_survivors": int(
                candidate_df["pass_all_default_filters"].sum()
            ),
            "classification": "PIPELINE_COMPLETE_BUT_ZERO_DEFAULT_FILTER_SURVIVORS",
        },
        "future_selectivity_evaluation": {
            "required": (
                "同一完整 VHH 序列、同一预测设置分别对 7–36 与 9–36 多构象重预测。"
            ),
            "paired_computational_proxies": [
                "ΔiPTM = on-target − off-target",
                "ΔPAE = off-target − on-target",
                "ΔSASA = on-target − off-target",
            ],
            "remaining_boundary": (
                "即使完成上述比较也只能称计算选择性代理；最终仍需 SPR/BLI 等实验。"
            ),
        },
    }


def build_run_summary(
    manifest: dict[str, Any],
    status: dict[str, Any],
    funnel_df: pd.DataFrame,
    timing_df: pd.DataFrame,
    candidate_df: pd.DataFrame,
    identity_df: pd.DataFrame,
    inventory_df: pd.DataFrame,
    cif_plot_meta: dict[str, Any],
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    """生成面向人和后续 HTML 的答案优先总摘要。"""

    selected = candidate_df[candidate_df["selected_by_budget"]]
    selected_records = [
        {key: json_scalar(value) for key, value in row.items()}
        for row in selected.to_dict(orient="records")
    ]
    pipeline_timings = timing_df[
        (timing_df["category"] == "pipeline")
        & (timing_df["status"] == "completed")
    ]
    funnel_counts = dict(zip(funnel_df["stage_key"], funnel_df["count"]))
    off_diag = identity_df.to_numpy(dtype=float)
    diversity_value = float(off_diag[0, 1]) if off_diag.shape == (2, 2) else None

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": manifest.get("run_id"),
        "run_status": status.get("status"),
        "evaluation_classification": "PIPELINE_COMPLETE_BUT_ZERO_DEFAULT_FILTER_SURVIVORS",
        "headline_cn": "MVP 推理链路完整跑通，但 2 个候选均未通过默认计算过滤。",
        "interpretation_cn": (
            "这是一次成功的软件/流程冒烟测试，不是一次成功的候选发现。"
            "最终目录中的候选 1 是预算排序机制写出的相对优先项；它仍然 "
            "pass_filters=False。"
        ),
        "counts": {key: int(value) for key, value in funnel_counts.items()},
        "pipeline_elapsed_seconds": {
            row["stage"]: json_scalar(row["elapsed_seconds"])
            for _, row in pipeline_timings.iterrows()
        },
        "execute_wrapper_elapsed_seconds": next(
            (
                json_scalar(row["elapsed_seconds"])
                for _, row in timing_df.iterrows()
                if row["category"] == "wrapper_total"
            ),
            None,
        ),
        "selected_candidates": selected_records,
        "candidate_table": rel(ANALYSIS_DIR / "candidate_metrics.csv"),
        "design_region_pair_similarity_proxy": diversity_value,
        "fold_sample_consistency": [
            {
                "candidate_id": row["candidate_id"],
                "fold_sample_count": json_scalar(row["fold_sample_count"]),
                "analysis_best_sample_index": json_scalar(
                    row["analysis_best_sample_index"]
                ),
                "writer_best_sample_index": json_scalar(row["writer_best_sample_index"]),
                "same_best_sample": json_scalar(row["same_best_sample"]),
                "note": row["note"],
            }
            for _, row in candidate_df.iterrows()
        ],
        "input_fingerprint": {
            "design_spec": manifest.get("design_spec"),
            "requested_designs": manifest.get("requested_designs"),
            "final_budget": manifest.get("final_budget"),
            "execution_class": manifest.get("execution_class"),
            "official_release_baseline": manifest.get("official_release_baseline"),
            "experimental_mps_pr_commit": manifest.get("experimental_mps_pr_commit"),
            "fast_smoke_settings": manifest.get("fast_smoke_settings"),
            "input_files": manifest.get("input_files"),
            "runtime_assets": manifest.get("runtime_assets"),
        },
        "scientific_boundaries": [
            "无 affinity 步骤；不能报告 Kd/IC50/Ki/ΔG 或结合概率。",
            "只运行正靶 7–36；不能报告 7–36/9–36 选择性。",
            "标准 polymer CIF 未明确原子级编码 C 端酰胺。",
            "nanobody 协议跳过 design_folding；没有 VHH 独立折叠代理。",
            "n=2 且降低采样；不能外推总体命中率或统计分布。",
        ],
        "figures": {
            "process_funnel": "analysis/figures/process_funnel.png",
            "stage_timings": "analysis/figures/stage_timings.png",
            "rmsd_threshold": "analysis/figures/rmsd_threshold.png",
            "interface_proxies": "analysis/figures/interface_proxies.png",
            "interface_geometry": "analysis/figures/interface_geometry.png",
            "sequence_identity": "analysis/figures/sequence_identity.png",
            "final_cif_ca_trace_3d": "analysis/figures/final_cif_ca_trace_3d.png",
        },
        "final_cif_visualization": cif_plot_meta,
        "output_inventory": {
            "file_count": len(inventory_df),
            "total_size_bytes": int(inventory_df["size_bytes"].sum()),
            "primary_result_file_count": int(inventory_df["is_primary_result"].sum()),
        },
        "source_read_only_check": {
            "protected_file_count": len(source_hashes),
            "status": "PASS",
            "meaning": "分析前后 inputs/logs/outputs 中全部文件 SHA-256 保持不变。",
        },
        "generated_analysis_files": [
            "analysis/run_summary.json",
            "analysis/candidate_metrics.csv",
            "analysis/process_funnel.csv",
            "analysis/stage_timings.csv",
            "analysis/npz_schema.json",
            "analysis/output_inventory.tsv",
            "analysis/sequence_identity.csv",
            "analysis/evaluation_contract.json",
            "analysis/figures/process_funnel.png",
            "analysis/figures/stage_timings.png",
            "analysis/figures/rmsd_threshold.png",
            "analysis/figures/interface_proxies.png",
            "analysis/figures/interface_geometry.png",
            "analysis/figures/sequence_identity.png",
            "analysis/figures/final_cif_ca_trace_3d.png",
        ],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    """执行只读分析、产物写入、完整性验证和简要终端汇报。"""

    # 在任何写入发生前完成受保护源文件哈希快照。
    source_hashes_before = snapshot_protected_sources()

    manifest_path = required_file(RUN_OUTPUT_DIR / "input_manifest.json")
    status_path = required_file(RUN_OUTPUT_DIR / "mvp_run_status.json")
    aggregate_path = required_file(INVERSE_DIR / "aggregate_metrics_analyze.csv")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    aggregate_df = pd.read_csv(aggregate_path)

    # analysis/ 是本脚本唯一允许创建/更新的目录。
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    configure_plot_style()

    inventory_df = build_output_inventory()
    candidate_df, _, selected_ids = build_candidate_metrics()
    identity_df = build_sequence_identity(candidate_df)
    timing_df = build_stage_timings(status)
    funnel_df = build_process_funnel(
        manifest, aggregate_df, candidate_df, selected_ids
    )
    npz_schema = build_npz_schema()
    evaluation_contract = build_evaluation_contract(manifest, candidate_df)

    # 先写结构化表格和契约；CSV 均保留原始数值精度，不只保留绘图四舍五入值。
    candidate_df.to_csv(ANALYSIS_DIR / "candidate_metrics.csv", index=False)
    funnel_df.to_csv(ANALYSIS_DIR / "process_funnel.csv", index=False)
    timing_df.to_csv(ANALYSIS_DIR / "stage_timings.csv", index=False)
    identity_df.to_csv(ANALYSIS_DIR / "sequence_identity.csv", float_format="%.6f")
    inventory_df.to_csv(
        ANALYSIS_DIR / "output_inventory.tsv", sep="\t", index=False, quoting=csv.QUOTE_MINIMAL
    )
    write_json(ANALYSIS_DIR / "npz_schema.json", npz_schema)
    write_json(ANALYSIS_DIR / "evaluation_contract.json", evaluation_contract)

    # 所有图都来自上面已经落盘的同一组真实统计，避免“图和表来源不一致”。
    plot_process_funnel(funnel_df)
    plot_stage_timings(timing_df)
    plot_rmsd_threshold(candidate_df)
    plot_interface_proxies(candidate_df)
    plot_interface_geometry(candidate_df)
    plot_sequence_identity(identity_df, candidate_df)
    cif_plot_meta = plot_final_cif_ca_trace(candidate_df)
    if not cif_plot_meta.get("generated"):
        raise RuntimeError(f"最终 CIF 三维图生成失败：{cif_plot_meta}")

    # 写总摘要前再次哈希全部受保护文件。任何变化都直接报错，不生成 PASS 摘要。
    source_hashes_after = snapshot_protected_sources()
    if source_hashes_after != source_hashes_before:
        changed = sorted(
            key
            for key in set(source_hashes_before) | set(source_hashes_after)
            if source_hashes_before.get(key) != source_hashes_after.get(key)
        )
        raise RuntimeError(f"只读保护失败，检测到原始文件变化：{changed}")

    summary = build_run_summary(
        manifest,
        status,
        funnel_df,
        timing_df,
        candidate_df,
        identity_df,
        inventory_df,
        cif_plot_meta,
        source_hashes_before,
    )
    write_json(ANALYSIS_DIR / "run_summary.json", summary)

    print("分析完成：", ANALYSIS_DIR)
    print("真实候选数：", len(candidate_df))
    print("通过默认过滤：", int(candidate_df["pass_all_default_filters"].sum()))
    print("预算排序输出：", len(selected_ids), "（不等于过滤通过）")
    print("只读源文件校验：PASS（", len(source_hashes_before), "个文件）")


if __name__ == "__main__":
    main()
