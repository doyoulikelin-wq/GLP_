#!/usr/bin/env python3
"""只读汇总旧 12 骨架 × 单一 GLP-1 的第一轮 BoltzGen 真实输出。

本脚本不加载模型，也不改变 ``inputs/``、``configs/``、``runs/`` 或 ``vendor/``。
它只把派生表、图和验证记录写入 ``analysis/``。分析明确区分三件事：

1. 工程管线是否完成；
2. 结果文件与候选谱系是否完整；
3. 候选是否通过 BoltzGen 默认计算过滤。

所有界面指标都是结构计算代理，不会被解释为实验平衡解离常数、结合概率、
GLP-1(7–36)/GLP-1(9–36) 型态选择性或 C 端酰胺识别。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import gemmi
import matplotlib

# 使用无窗口后端，保证 Notebook 重放、终端和自动化验证都能输出图片。
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from Bio import Align


RUN_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = RUN_ROOT / "inputs"
CONFIG_DIR = RUN_ROOT / "configs"
RUNS_DIR = RUN_ROOT / "runs"
ANALYSIS_DIR = RUN_ROOT / "analysis"
FIGURE_DIR = ANALYSIS_DIR / "figures"
INPUT_MANIFEST = RUN_ROOT / "provenance" / "input_manifest.json"

RMSD_THRESHOLD_A = 2.5
HOTSPOT_DISTANCE_THRESHOLD_A = 8.0

COLORS = {
    "navy": "#12304A",
    "blue": "#277DA1",
    "teal": "#2A9D8F",
    "amber": "#E69F00",
    "red": "#C75B4B",
    "purple": "#7B61A8",
    "gray": "#8796A5",
    "light": "#E8EEF3",
    "ink": "#1D2A35",
}

# 当前 filtering 配置实际启用的 10 个条件。字段名来自 v0.3.2 结果表；
# 阈值由每个任务的执行日志与冻结配置共同确认。
FILTER_DEFINITIONS = [
    {
        "order": 1,
        "pass_column": "pass_has_x_filter",
        "value_column": "has_x",
        "label_cn": "未知残基 X",
        "operator": "<=",
        "threshold": 0.0,
        "unit": "count",
    },
    {
        "order": 2,
        "pass_column": "pass_filter_rmsd_filter",
        "value_column": "filter_rmsd",
        "label_cn": "复合物骨架均方根偏差",
        "operator": "<=",
        "threshold": 2.5,
        "unit": "Å",
    },
    {
        "order": 3,
        "pass_column": "pass_filter_rmsd_design_filter",
        "value_column": "filter_rmsd_design",
        "label_cn": "VHH设计区骨架均方根偏差",
        "operator": "<=",
        "threshold": 2.5,
        "unit": "Å",
    },
    {
        "order": 4,
        "pass_column": "pass_bindsite_under_8rmsd_filter",
        "value_column": "bindsite_under_8rmsd",
        "label_cn": "复折叠前 His7/Ala8 提示位点8 Å覆盖",
        "operator": ">=",
        "threshold": 0.0001,
        "unit": "fraction",
    },
    {
        "order": 5,
        "pass_column": "pass_CYS_fraction_filter",
        "value_column": "CYS_fraction",
        "label_cn": "设计区半胱氨酸比例",
        "operator": "<=",
        "threshold": 0.0,
        "unit": "fraction",
    },
    {
        "order": 6,
        "pass_column": "pass_ALA_fraction_filter",
        "value_column": "ALA_fraction",
        "label_cn": "设计区丙氨酸比例",
        "operator": "<=",
        "threshold": 0.3,
        "unit": "fraction",
    },
    {
        "order": 7,
        "pass_column": "pass_GLY_fraction_filter",
        "value_column": "GLY_fraction",
        "label_cn": "设计区甘氨酸比例",
        "operator": "<=",
        "threshold": 0.3,
        "unit": "fraction",
    },
    {
        "order": 8,
        "pass_column": "pass_GLU_fraction_filter",
        "value_column": "GLU_fraction",
        "label_cn": "设计区谷氨酸比例",
        "operator": "<=",
        "threshold": 0.3,
        "unit": "fraction",
    },
    {
        "order": 9,
        "pass_column": "pass_LEU_fraction_filter",
        "value_column": "LEU_fraction",
        "label_cn": "设计区亮氨酸比例",
        "operator": "<=",
        "threshold": 0.3,
        "unit": "fraction",
    },
    {
        "order": 10,
        "pass_column": "pass_VAL_fraction_filter",
        "value_column": "VAL_fraction",
        "label_cn": "设计区缬氨酸比例",
        "operator": "<=",
        "threshold": 0.3,
        "unit": "fraction",
    },
]


def utc_now() -> str:
    """返回带时区的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 2 * 1024 * 1024) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """写出可读且稳定的 UTF-8 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def rel(path: Path) -> str:
    """把路径转换为相对本轮根目录的 POSIX 表示。"""

    return path.resolve().relative_to(RUN_ROOT).as_posix()


def to_bool(value: Any) -> bool:
    """稳健解析 CSV 中的布尔值。"""

    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def safe_float(value: Any) -> float | None:
    """把字符串/标量转换为有限浮点数；空值、NaN 和 Inf 返回 None。"""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def passes_threshold(value: float, operator: str, threshold: float) -> bool:
    """按冻结过滤合同重算一个条件，拒绝未知运算符。"""

    if operator == "<=":
        return value <= threshold
    if operator == ">=":
        return value >= threshold
    raise ValueError(f"不支持的过滤运算符：{operator}")


def json_value(value: Any) -> Any:
    """把 NumPy/Pandas 标量变成标准 JSON 类型。"""

    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def configure_plot_style() -> None:
    """配置跨 macOS/Notebook 可读的中文图表样式。"""

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
            "font.size": 10.5,
            "axes.titlesize": 15,
            "axes.labelsize": 11,
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
    """以高分辨率 PNG 保存图，并释放图对象。"""

    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE_DIR / filename, dpi=210, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def protected_source_snapshot() -> dict[str, str]:
    """对分析不应修改的核心文件计算哈希快照。

    覆盖冻结规格、真实目标、每个骨架输入，以及各任务“最近完整 attempt”的权威
    CSV、refold CIF 与 fold NPZ。这样既能发现分析阶段的意外写入，也不会把仍在运行
    或历史未选中的 attempt 混入前后快照。
    """

    manifest = json.loads(INPUT_MANIFEST.read_text(encoding="utf-8"))
    paths = [
        INPUT_MANIFEST,
        RUN_ROOT / manifest["target"]["path"],
        *sorted(CONFIG_DIR.glob("*.yaml")),
    ]
    for scaffold in manifest["scaffold_population"]["records"]:
        package = RUN_ROOT / scaffold["input_package"]
        paths.extend([package / "scaffold.yaml", package / "scaffold.cif"])

        rank = int(scaffold["selection_rank"])
        task_name = f"{rank:02d}_{scaffold['candidate_id']}"
        attempt, _ = latest_complete_attempt(RUNS_DIR / task_name)
        pipeline = attempt / "pipeline"
        paths.append(attempt / "run_status.json")
        paths.extend(sorted((pipeline / "final_ranked_designs").glob("*.csv")))
        paths.extend(
            sorted(
                (
                    pipeline
                    / "intermediate_designs_inverse_folded"
                    / "refold_cif"
                ).glob("*.cif")
            )
        )
        paths.extend(
            sorted(
                (
                    pipeline
                    / "intermediate_designs_inverse_folded"
                    / "fold_out_npz"
                ).glob("*.npz")
            )
        )

    unique_paths = sorted({path.resolve() for path in paths if path.is_file()})
    return {rel(path): sha256_file(path) for path in unique_paths}


def latest_complete_attempt(task_root: Path) -> tuple[Path, dict[str, Any]]:
    """返回一个骨架最近的完整 attempt 和状态记录。"""

    for attempt in sorted(task_root.glob("attempt_*"), reverse=True):
        status_path = attempt / "run_status.json"
        if not status_path.exists():
            continue
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") == "PIPELINE_COMPLETE":
            return attempt, status
    raise FileNotFoundError(f"{task_root.name} 没有 PIPELINE_COMPLETE attempt")


def parse_design_regions(scaffold_yaml: Path) -> list[list[int]]:
    """把 VHH YAML 严格解析为按链顺序排列的三个设计区域。

    本轮把三个逗号分隔区域依次解释为 CDR1、CDR2 和 CDR3；因此不能先把所有位置
    展平、排序后再按长度切片，否则 YAML 区域边界错误会被静默掩盖。
    """

    payload = yaml.safe_load(scaffold_yaml.read_text(encoding="utf-8"))
    design_items = payload.get("design", [])
    if not isinstance(design_items, list) or len(design_items) != 1:
        raise ValueError(f"本轮每个骨架必须恰有一个design链：{scaffold_yaml}")
    try:
        text = str(design_items[0]["chain"]["res_index"])
    except (KeyError, TypeError) as error:
        raise ValueError(f"design.chain.res_index合同缺失：{scaffold_yaml}") from error

    tokens = [token.strip() for token in text.split(",")]
    if len(tokens) != 3 or any(not token for token in tokens):
        raise ValueError(f"本轮design.res_index必须包含三个非空区域：{scaffold_yaml}")

    regions: list[list[int]] = []
    for token in tokens:
        match = re.fullmatch(r"([1-9][0-9]*)(?:\.\.([1-9][0-9]*))?", token)
        if match is None:
            raise ValueError(f"非法design.res_index区域 {token!r}：{scaffold_yaml}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            raise ValueError(f"design区域终点小于起点 {token!r}：{scaffold_yaml}")
        regions.append(list(range(start, end + 1)))

    if any(regions[index][-1] >= regions[index + 1][0] for index in range(2)):
        raise ValueError(f"三个design区域必须按链顺序排列且互不重叠：{scaffold_yaml}")
    return regions


def parse_design_positions(scaffold_yaml: Path) -> list[int]:
    """返回三个设计区域的展平位置，供接触计算和绘图复用。"""

    return [position for region in parse_design_regions(scaffold_yaml) for position in region]


def chain_sequence(chain: gemmi.Chain) -> str:
    """将 Gemmi 链转换为标准单字母蛋白序列。"""

    letters = []
    for residue in chain:
        info = gemmi.find_tabulated_residue(residue.name)
        letter = info.one_letter_code if info is not None else "X"
        letters.append(letter if letter else "X")
    return "".join(letters)


def choose_target_and_binder(model: gemmi.Model) -> tuple[gemmi.Chain, gemmi.Chain]:
    """按已知30残基目标序列识别输出中的目标链和VHH链。

    BoltzGen 在 refold CIF 中通常把输入 E/A 重命名为 A/B，所以不能沿用输入链名。
    """

    target_sequence = "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR"
    chains = list(model)
    target_matches = [chain for chain in chains if chain_sequence(chain) == target_sequence]
    if len(target_matches) != 1:
        raise ValueError(f"refold CIF 中不能唯一识别30残基GLP-1链：{[chain_sequence(c) for c in chains]}")
    target = target_matches[0]
    binders = [chain for chain in chains if chain.name != target.name]
    if len(binders) != 1:
        raise ValueError(f"refold CIF 中VHH链数量不是1：{[chain.name for chain in chains]}")
    return target, binders[0]


def heavy_atoms(residue: gemmi.Residue) -> list[gemmi.Atom]:
    """返回一个残基中非氢、占有率大于0的原子。"""

    return [
        atom
        for atom in residue
        if not atom.element.is_hydrogen and float(atom.occ) > 0.0
    ]


def min_atom_distance(residue_a: gemmi.Residue, residues_b: Iterable[gemmi.Residue]) -> float:
    """计算两个残基集合间最小重原子距离，单位为埃。"""

    atoms_a = heavy_atoms(residue_a)
    atoms_b = [atom for residue in residues_b for atom in heavy_atoms(residue)]
    if not atoms_a or not atoms_b:
        raise ValueError("计算重原子距离时发现空原子集合")
    return min(atom_a.pos.dist(atom_b.pos) for atom_a in atoms_a for atom_b in atoms_b)


def min_ca_distance(residue_a: gemmi.Residue, residues_b: Iterable[gemmi.Residue]) -> float:
    """计算目标残基 Cα 到设计残基 Cα 的最小距离。"""

    ca_a = residue_a.find_atom("CA", "*")
    ca_b = [residue.find_atom("CA", "*") for residue in residues_b]
    ca_b = [atom for atom in ca_b if atom]
    if not ca_a or not ca_b:
        raise ValueError("计算 Cα 距离时缺少 Cα 原子")
    return min(ca_a.pos.dist(atom.pos) for atom in ca_b)


def independent_hotspot_contacts(
    refold_cif: Path,
    design_positions: list[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """独立重算 His7/Ala8 到 VHH 设计区的几何距离与覆盖比例。"""

    structure = gemmi.read_structure(str(refold_cif))
    if len(structure) != 1:
        raise ValueError(f"refold CIF 模型数不是1：{refold_cif}")
    target, binder = choose_target_and_binder(structure[0])
    # YAML 的 res_index 使用输入链的 1-based canonical/label 顺序。refold CIF 当前虽恰好
    # 把 auth 编号重置为 1..N，但这里仍按链内顺序建索引，避免将来 auth 编号变化时静默错位。
    binder_by_position = {
        position: residue for position, residue in enumerate(binder, start=1)
    }
    missing = sorted(set(design_positions) - set(binder_by_position))
    if missing:
        raise ValueError(f"VHH设计位置在refold CIF中缺失：{missing}")
    design_residues = [binder_by_position[position] for position in design_positions]
    target_by_position = {
        position: residue for position, residue in enumerate(target, start=1)
    }

    rows = []
    for label_seq_id, biological_name in ((1, "His7"), (2, "Ala8")):
        residue = target_by_position[label_seq_id]
        heavy_distance = min_atom_distance(residue, design_residues)
        ca_distance = min_ca_distance(residue, design_residues)
        rows.append(
            {
                "hotspot_label_seq_id": label_seq_id,
                "hotspot_biological_name": biological_name,
                "min_heavy_atom_distance_a": round(heavy_distance, 5),
                "min_ca_distance_a": round(ca_distance, 5),
                "heavy_atom_covered_lt8a": heavy_distance < HOTSPOT_DISTANCE_THRESHOLD_A,
                "ca_covered_lt8a": ca_distance < HOTSPOT_DISTANCE_THRESHOLD_A,
            }
        )

    summary = {
        "refold_target_chain": target.name,
        "refold_vhh_chain": binder.name,
        "target_sequence_verified": chain_sequence(target),
        "vhh_sequence_verified": chain_sequence(binder),
        "vhh_residue_count": len(binder),
        "design_residue_count": len(design_positions),
        "independent_hotspot_coverage_heavy_lt8a": sum(
            row["heavy_atom_covered_lt8a"] for row in rows
        )
        / 2.0,
        "independent_hotspot_coverage_ca_lt8a": sum(row["ca_covered_lt8a"] for row in rows)
        / 2.0,
    }
    return rows, summary


def global_alignment_identity(sequence_a: str, sequence_b: str) -> float:
    """以全局比对后相同字符数/对齐长度定义序列一致性。"""

    aligner = Align.PairwiseAligner()
    aligner.mode = "global"
    aligner.match_score = 1.0
    aligner.mismatch_score = 0.0
    aligner.open_gap_score = -1.0
    aligner.extend_gap_score = -0.1
    alignment = aligner.align(sequence_a, sequence_b)[0]
    matrix = np.asarray(alignment).astype("U1")
    matches = int(np.sum(matrix[0] == matrix[1]))
    return matches / matrix.shape[1] if matrix.shape[1] else 1.0


def csv_candidate_ids(path: Path, label: str) -> tuple[set[str], int]:
    """读取候选表 ID，并拒绝缺列、空 ID 和重复 ID。"""

    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(f"缺少或为空的{label}：{path}")
    frame = pd.read_csv(path)
    if "id" not in frame.columns:
        raise ValueError(f"{label}缺少id列：{path}")
    ids = frame["id"].astype(str).str.strip()
    if ids.empty or (ids == "").any() or ids.duplicated().any():
        raise ValueError(f"{label}存在空ID、零行或重复ID：{path}")
    return set(ids), len(ids)


def validate_output_contract(pipeline: Path, task_name: str) -> dict[str, Any]:
    """验证一个骨架从设计到预算目录的候选 ID 谱系合同。

    本轮固定 ``num_designs=2``、``budget=1``。前四个模型阶段必须保留两个原始
    candidate stem；序列去重后的排序表可以只有1或2个，但只能是这两个ID的子集；
    最终指标表和 ``final_1_designs`` 目录必须指向同一个唯一候选。
    """

    design_dir = pipeline / "intermediate_designs"
    inverse_dir = pipeline / "intermediate_designs_inverse_folded"
    final_dir = pipeline / "final_ranked_designs"
    expected_ids = {f"{task_name}_0", f"{task_name}_1"}

    stage_ids = {
        "raw_design_cif": {path.stem for path in design_dir.glob("*.cif")},
        "raw_design_npz": {path.stem for path in design_dir.glob("*.npz")},
        "inverse_fold_cif": {path.stem for path in inverse_dir.glob("*.cif")},
        "inverse_fold_npz": {path.stem for path in inverse_dir.glob("*.npz")},
        "fold_npz": {path.stem for path in (inverse_dir / "fold_out_npz").glob("*.npz")},
        "refold_cif": {path.stem for path in (inverse_dir / "refold_cif").glob("*.cif")},
    }
    aggregate_ids, analyzed_rows = csv_candidate_ids(
        inverse_dir / "aggregate_metrics_analyze.csv", "分析聚合表"
    )
    stage_ids["analyzed_csv"] = aggregate_ids
    for stage, observed_ids in stage_ids.items():
        if observed_ids != expected_ids:
            raise ValueError(
                f"{task_name} 的{stage}候选ID合同不成立："
                f"expected={sorted(expected_ids)}, observed={sorted(observed_ids)}"
            )

    ranked_ids, ranked_rows = csv_candidate_ids(
        final_dir / "all_designs_metrics.csv", "骨架内去重排序表"
    )
    if not ranked_ids.issubset(expected_ids) or not 1 <= ranked_rows <= 2:
        raise ValueError(
            f"{task_name} 的排序候选必须是原始两个ID的非空子集：{sorted(ranked_ids)}"
        )

    selected_ids, selected_rows = csv_candidate_ids(
        final_dir / "final_designs_metrics_1.csv", "预算1最终指标表"
    )
    if selected_rows != 1 or not selected_ids.issubset(ranked_ids):
        raise ValueError(
            f"{task_name} 的预算1最终ID必须是唯一排序候选：{sorted(selected_ids)}"
        )
    selected_id = next(iter(selected_ids))

    budget_dir = final_dir / "final_1_designs"
    budget_cifs = sorted(budget_dir.glob("*.cif"))
    if len(budget_cifs) != 1 or budget_cifs[0].name != f"rank1_{selected_id}.cif":
        raise ValueError(
            f"{task_name} 的final_1_designs目录必须恰含rank1_{selected_id}.cif："
            f"{[path.name for path in budget_cifs]}"
        )

    return {
        "expected_ids": expected_ids,
        "ranked_ids": ranked_ids,
        "selected_ids": selected_ids,
        "counts": {
            "raw_design_pairs": len(expected_ids),
            "inverse_folded_pairs": len(expected_ids),
            "fold_npz": len(expected_ids),
            "refold_cif": len(expected_ids),
            "analyzed_rows": analyzed_rows,
            "ranked_unique_rows": ranked_rows,
            "final_budget_rows": selected_rows,
            "final_budget_cif": len(budget_cifs),
        },
    }


def inspect_fold_npz(path: Path, expected_samples: int) -> dict[str, int]:
    """验证 fold NPZ 的样本、原子、坐标和 atom-to-token 轴合同。"""

    required = {
        "iptm",
        "ptm",
        "design_to_target_iptm",
        "design_ptm",
        "coords",
        "atom_to_token",
        "atom_resolved_mask",
        "token_index",
    }
    with np.load(path, allow_pickle=False) as arrays:
        missing = sorted(required - set(arrays.files))
        if missing:
            raise ValueError(f"fold NPZ缺少必需数组 {missing}：{path}")

        score_arrays = {
            key: np.asarray(arrays[key])
            for key in ("iptm", "ptm", "design_to_target_iptm", "design_ptm")
        }
        if score_arrays["iptm"].ndim != 1:
            raise ValueError(f"iptm必须是一维样本向量：{path}")
        sample_count = int(score_arrays["iptm"].shape[0])
        if sample_count != expected_samples:
            raise ValueError(
                f"fold NPZ样本数与冻结配置不一致：expected={expected_samples}, "
                f"observed={sample_count}, path={path}"
            )
        for key, array in score_arrays.items():
            if array.shape != (sample_count,) or not np.isfinite(array).all():
                raise ValueError(f"{key}必须是有限的一维样本向量({sample_count},)：{path}")

        coords = np.asarray(arrays["coords"])
        atom_to_token = np.asarray(arrays["atom_to_token"])
        resolved = np.asarray(arrays["atom_resolved_mask"])
        token_index = np.asarray(arrays["token_index"])
        if coords.ndim != 3 or coords.shape[0] != sample_count or coords.shape[2] != 3:
            raise ValueError(f"coords必须为[sample, atom_slot, xyz=3]：{path} {coords.shape}")
        if not np.isfinite(coords).all():
            raise ValueError(f"coords包含NaN或Inf：{path}")
        if atom_to_token.ndim != 3 or atom_to_token.shape[:2] != coords.shape[:2]:
            raise ValueError(
                f"atom_to_token必须为[sample, atom_slot, token]且前两轴匹配coords："
                f"{path} {atom_to_token.shape} vs {coords.shape}"
            )
        token_count = int(atom_to_token.shape[2])
        if token_index.shape != (sample_count, token_count):
            raise ValueError(
                f"token_index必须为[sample, token]并匹配atom_to_token末轴："
                f"{path} {token_index.shape}"
            )
        if resolved.shape != coords.shape[:2]:
            raise ValueError(
                f"atom_resolved_mask必须为[sample, atom_slot]：{path} {resolved.shape}"
            )
        if not np.isin(atom_to_token, (0, 1)).all():
            raise ValueError(f"atom_to_token必须是二值映射数组：{path}")
        if not np.isin(resolved, (0, 1)).all():
            raise ValueError(f"atom_resolved_mask必须是二值掩码：{path}")
        mapped_token_count = atom_to_token.astype(bool).sum(axis=2)
        if not np.all(mapped_token_count <= 1):
            raise ValueError(f"每个atom slot最多只能映射到一个token：{path}")
        if not np.all(mapped_token_count[resolved.astype(bool)] == 1):
            raise ValueError(f"每个resolved atom slot必须恰映射到一个token：{path}")
        # unresolved并不等同于padding：化学上存在但当前未解析的原子仍可能保留token归属。

        analysis_score = (
            0.8 * score_arrays["design_to_target_iptm"]
            + 0.2 * score_arrays["design_ptm"]
        )
        writer_score = 0.8 * score_arrays["iptm"] + 0.2 * score_arrays["ptm"]
        return {
            "sample_count": sample_count,
            "atom_slot_count": int(coords.shape[1]),
            "token_count": token_count,
            "analysis_best_sample_index": int(np.argmax(analysis_score)),
            "writer_best_sample_index": int(np.argmax(writer_score)),
        }


def build_tables(manifest: dict[str, Any]) -> dict[str, pd.DataFrame]:
    """合并12个独立任务，生成规范化候选、过滤、谱系和运行表。"""

    raw_frames = []
    candidate_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    lineage_rows: list[dict[str, Any]] = []
    filter_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()

    for scaffold in manifest["scaffold_population"]["records"]:
        rank = int(scaffold["selection_rank"])
        task_name = f"{rank:02d}_{scaffold['candidate_id']}"
        task_root = RUNS_DIR / task_name
        attempt, status = latest_complete_attempt(task_root)
        pipeline = attempt / "pipeline"
        output_contract = validate_output_contract(pipeline, task_name)
        metrics_path = pipeline / "final_ranked_designs" / "all_designs_metrics.csv"
        final_path = pipeline / "final_ranked_designs" / "final_designs_metrics_1.csv"
        aggregate_path = (
            pipeline / "intermediate_designs_inverse_folded" / "aggregate_metrics_analyze.csv"
        )
        for required in (metrics_path, final_path, aggregate_path):
            if not required.exists():
                raise FileNotFoundError(f"缺少权威输出：{required}")

        raw = pd.read_csv(metrics_path)
        if not 1 <= len(raw) <= 2:
            raise ValueError(f"{task_name} 的骨架内去重结果行数异常：{len(raw)}")
        if raw["id"].astype(str).duplicated().any():
            raise ValueError(f"{task_name} 的权威结果表存在重复id")
        raw_candidate_ids = set(raw["id"].astype(str))
        if raw_candidate_ids != output_contract["ranked_ids"]:
            raise ValueError(f"{task_name} 的排序表ID在两次读取间发生变化")
        collisions = sorted(raw_candidate_ids & seen_candidate_ids)
        if collisions:
            raise ValueError(f"候选ID在跨骨架合并前已经碰撞：{collisions}")
        seen_candidate_ids.update(raw_candidate_ids)
        # 先验证字段合同，防止缺列被 row.get(None) 静默解释为过滤失败。
        required_columns = {
            "id",
            "designed_sequence",
            "designed_chain_sequence",
            "pass_filters",
            *(definition["pass_column"] for definition in FILTER_DEFINITIONS),
            *(definition["value_column"] for definition in FILTER_DEFINITIONS),
        }
        missing_columns = sorted(required_columns - set(raw.columns))
        if missing_columns:
            raise ValueError(f"{task_name} 的结果表缺少必需列：{missing_columns}")
        # all_designs_metrics 本身列很多；一次性拼接元数据，避免逐列 insert 造成
        # DataFrame 碎片化和无意义的性能警告。
        metadata = pd.DataFrame(
            {
                "scaffold_selection_rank": [rank] * len(raw),
                "scaffold_id": [scaffold["candidate_id"]] * len(raw),
                "scaffold_role": [scaffold["role"]] * len(raw),
                "scaffold_pdb_code": [scaffold["pdb_code"]] * len(raw),
            },
            index=raw.index,
        )
        raw = pd.concat([metadata, raw], axis=1).copy()
        raw_frames.append(raw)

        selected_ids = output_contract["selected_ids"]
        scaffold_package = RUN_ROOT / scaffold["input_package"]
        design_regions = parse_design_regions(scaffold_package / "scaffold.yaml")
        design_positions = [
            position for region in design_regions for position in region
        ]
        design_position_set = set(design_positions)
        cdr_lengths = [
            int(scaffold["cdr1_length_aa"]),
            int(scaffold["cdr2_length_aa"]),
            int(scaffold["cdr3_length_aa"]),
        ]
        if [len(region) for region in design_regions] != cdr_lengths:
            raise ValueError(
                f"{task_name} 的三个design区域长度与CDR1/2/3合同不一致："
                f"regions={[len(region) for region in design_regions]}, cdr={cdr_lengths}"
            )

        input_structure = gemmi.read_structure(str(scaffold_package / "scaffold.cif"))
        if len(input_structure) != 1 or len(list(input_structure[0])) != 1:
            raise ValueError(f"{task_name} 的输入骨架必须恰有一个模型和一条VHH链")
        input_chain = list(input_structure[0])[0]
        input_sequence = chain_sequence(input_chain)
        if not input_sequence or design_positions[-1] > len(input_sequence):
            raise ValueError(f"{task_name} 的design位置超出输入VHH完整链")
        input_framework = "".join(
            residue
            for position, residue in enumerate(input_sequence, start=1)
            if position not in design_position_set
        )

        expected_fold_samples = int(
            manifest["compute_profile"]["folding_samples_per_candidate"]
        )
        if expected_fold_samples <= 0:
            raise ValueError("folding_samples_per_candidate必须是正整数")

        counts = output_contract["counts"]
        execute_seconds = sum(
            float(stage["elapsed_seconds"])
            for stage in status.get("stages", [])
            if stage["stage"] == "03_execute"
        )
        run_rows.append(
            {
                "selection_rank": rank,
                "scaffold_id": scaffold["candidate_id"],
                "pdb_code": scaffold["pdb_code"],
                "role": scaffold["role"],
                "status": status["status"],
                "attempt": rel(attempt),
                "requested_designs": 2,
                **counts,
                "execute_seconds": round(execute_seconds, 3),
                "elapsed_seconds": float(status.get("elapsed_seconds", 0.0)),
                "design_residue_count": len(design_positions),
            }
        )

        # 包装阶段耗时来自状态JSON；五个模型阶段耗时再从execute日志逐项提取。
        for stage in status.get("stages", []):
            stage_rows.append(
                {
                    "selection_rank": rank,
                    "scaffold_id": scaffold["candidate_id"],
                    "pdb_code": scaffold["pdb_code"],
                    "scope": "wrapper",
                    "stage": stage["stage"],
                    "stage_label_cn": {
                        "01_check": "输入检查",
                        "02_configure": "配置冻结",
                        "03_execute": "五步模型管线总计",
                    }.get(stage["stage"], stage["stage"]),
                    "elapsed_seconds": float(stage["elapsed_seconds"]),
                    "source_log": stage["log_path"],
                }
            )
        execute_log = attempt / "logs" / "03_execute.log"
        log_text = execute_log.read_text(encoding="utf-8", errors="replace")
        for model_stage, seconds in re.findall(
            r"✓ Step ([a-z_]+) completed successfully in ([0-9.]+)s", log_text
        ):
            stage_rows.append(
                {
                    "selection_rank": rank,
                    "scaffold_id": scaffold["candidate_id"],
                    "pdb_code": scaffold["pdb_code"],
                    "scope": "model_step",
                    "stage": model_stage,
                    "stage_label_cn": {
                        "design": "扩散设计",
                        "inverse_folding": "逆折叠",
                        "folding": "复合物复折叠",
                        "analysis": "指标分析",
                        "filtering": "过滤与排序",
                    }.get(model_stage, model_stage),
                    "elapsed_seconds": float(seconds),
                    "source_log": rel(execute_log),
                }
            )

        for _, raw_row in raw.iterrows():
            row = raw_row.to_dict()
            candidate_id = str(row["id"])
            design_sequence = str(row.get("designed_sequence", ""))
            full_sequence = str(row.get("designed_chain_sequence", ""))
            if len(full_sequence) != len(input_sequence):
                raise ValueError(
                    f"{candidate_id} 的完整VHH长度与输入骨架不一致："
                    f"{len(full_sequence)} != {len(input_sequence)}"
                )
            cdr_sequences = [
                "".join(full_sequence[position - 1] for position in region)
                for region in design_regions
            ]
            if design_sequence != "".join(cdr_sequences):
                raise ValueError(
                    f"{candidate_id} 的designed_sequence不是完整链三个design区域的顺序拼接"
                )
            cdr1, cdr2, cdr3 = cdr_sequences
            framework_sequence = "".join(
                residue
                for position, residue in enumerate(full_sequence, start=1)
                if position not in design_position_set
            )
            framework_unchanged = framework_sequence == input_framework

            refold_cif = (
                pipeline
                / "intermediate_designs_inverse_folded"
                / "refold_cif"
                / f"{candidate_id}.cif"
            )
            contact_detail, contact_summary = independent_hotspot_contacts(
                refold_cif, design_positions
            )
            if contact_summary["vhh_sequence_verified"] != full_sequence:
                raise ValueError(f"{candidate_id} 的refold VHH序列与权威完整链不一致")
            for contact in contact_detail:
                contact_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "scaffold_id": scaffold["candidate_id"],
                        "pdb_code": scaffold["pdb_code"],
                        **contact,
                        "source_refold_cif": rel(refold_cif),
                        "source_refold_cif_sha256": sha256_file(refold_cif),
                    }
                )

            pass_values: dict[str, bool] = {}
            for definition in FILTER_DEFINITIONS:
                value = safe_float(row[definition["value_column"]])
                if value is None:
                    raise ValueError(
                        f"{candidate_id} 的过滤值不是有限数：{definition['value_column']}"
                    )
                reported_pass = to_bool(row[definition["pass_column"]])
                recomputed_pass = passes_threshold(
                    value, definition["operator"], float(definition["threshold"])
                )
                if reported_pass != recomputed_pass:
                    raise ValueError(
                        f"{candidate_id} 的 {definition['pass_column']} 与值/阈值不一致"
                    )
                pass_values[definition["pass_column"]] = reported_pass
            computed_all = all(pass_values.values())
            official_all = to_bool(row.get("pass_filters"))
            if computed_all != official_all:
                raise ValueError(f"{candidate_id} 的逐项过滤与pass_filters不一致")

            failed_labels = []
            for definition in FILTER_DEFINITIONS:
                passed = pass_values[definition["pass_column"]]
                value = safe_float(row.get(definition["value_column"]))
                if not passed:
                    failed_labels.append(definition["label_cn"])
                filter_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "scaffold_id": scaffold["candidate_id"],
                        "pdb_code": scaffold["pdb_code"],
                        "filter_order": definition["order"],
                        "filter_label_cn": definition["label_cn"],
                        "pass_column": definition["pass_column"],
                        "value_column": definition["value_column"],
                        "observed_value": value,
                        "operator": definition["operator"],
                        "threshold": definition["threshold"],
                        "unit": definition["unit"],
                        "passed": passed,
                    }
                )

            fold_npz = (
                pipeline
                / "intermediate_designs_inverse_folded"
                / "fold_out_npz"
                / f"{candidate_id}.npz"
            )
            fold_contract = inspect_fold_npz(fold_npz, expected_fold_samples)

            normalized = {
                "candidate_id": candidate_id,
                "candidate_label": f"{scaffold['pdb_code']}-{candidate_id.rsplit('_', 1)[-1]}",
                "local_candidate_index": int(candidate_id.rsplit("_", 1)[-1]),
                "scaffold_selection_rank": rank,
                "scaffold_id": scaffold["candidate_id"],
                "scaffold_pdb_code": scaffold["pdb_code"],
                "scaffold_role": scaffold["role"],
                "scaffold_resolution_a": float(scaffold["resolution_a"]),
                "scaffold_r_free": float(scaffold["r_free"]),
                "design_residue_count": len(design_positions),
                "cdr1_length_aa": cdr_lengths[0],
                "cdr2_length_aa": cdr_lengths[1],
                "cdr3_length_aa": cdr_lengths[2],
                "cdr1_sequence": cdr1,
                "cdr2_sequence": cdr2,
                "cdr3_sequence": cdr3,
                "designed_sequence": design_sequence,
                "designed_chain_sequence": full_sequence,
                "framework_sequence": framework_sequence,
                "framework_sequence_unchanged": framework_unchanged,
                "filter_rmsd_a": safe_float(row.get("filter_rmsd")),
                "filter_rmsd_design_a": safe_float(row.get("filter_rmsd_design")),
                "bb_target_aligned_rmsd_design_a": safe_float(
                    row.get("bb_target_aligned_rmsd_design")
                ),
                "design_to_target_iptm": safe_float(row.get("design_to_target_iptm")),
                "design_ptm": safe_float(row.get("design_ptm")),
                "design_ipsae_min": safe_float(row.get("design_ipsae_min")),
                "min_design_to_target_pae_a": safe_float(
                    row.get("min_design_to_target_pae")
                ),
                "complex_plddt": safe_float(row.get("complex_plddt")),
                "complex_iplddt": safe_float(row.get("complex_iplddt")),
                # BoltzGen 的 bindsite_under_8rmsd 在 Analyze 阶段使用逆折叠后、
                # 复折叠前结构的 token center 坐标。它不能与下面从 refold CIF 独立
                # 重算的重原子/Cα距离混为同一阶段的指标。
                "prerefold_hotspot_coverage_fraction_lt8a": safe_float(
                    row.get("bindsite_under_8rmsd")
                ),
                "independent_hotspot_coverage_heavy_lt8a": contact_summary[
                    "independent_hotspot_coverage_heavy_lt8a"
                ],
                "independent_hotspot_coverage_ca_lt8a": contact_summary[
                    "independent_hotspot_coverage_ca_lt8a"
                ],
                "his7_min_heavy_atom_distance_a": contact_detail[0][
                    "min_heavy_atom_distance_a"
                ],
                "ala8_min_heavy_atom_distance_a": contact_detail[1][
                    "min_heavy_atom_distance_a"
                ],
                "his7_min_ca_distance_a": contact_detail[0]["min_ca_distance_a"],
                "ala8_min_ca_distance_a": contact_detail[1]["min_ca_distance_a"],
                "target_delta_sasa_refolded_a2": safe_float(
                    row.get("delta_sasa_refolded")
                ),
                "geometric_hbond_count_refolded": safe_float(
                    row.get("plip_hbonds_refolded")
                ),
                "charged_atom_pair_count_refolded": safe_float(
                    row.get("plip_saltbridge_refolded")
                ),
                "liability_score": safe_float(row.get("liability_score")),
                "liability_num_violations": safe_float(
                    row.get("liability_num_violations")
                ),
                "liability_high_severity_violations": safe_float(
                    row.get("liability_high_severity_violations")
                ),
                "liability_summary": str(row.get("liability_violations_summary", "")),
                "computed_filter_pass_count": sum(pass_values.values()),
                "computed_filter_total": len(pass_values),
                "failed_filter_count": len(failed_labels),
                "failed_filters_cn": "；".join(failed_labels),
                "pass_all_default_filters": official_all,
                "selected_by_budget": candidate_id in selected_ids,
                "boltzgen_internal_prefix_pass_score": safe_float(
                    row.get("num_filters_passed")
                ),
                "final_rank_within_scaffold": safe_float(row.get("final_rank")),
                "quality_score_within_scaffold_only": safe_float(row.get("quality_score")),
                "fold_sample_count": fold_contract["sample_count"],
                "fold_atom_slot_count": fold_contract["atom_slot_count"],
                "fold_token_count": fold_contract["token_count"],
                "analysis_best_sample_index": fold_contract[
                    "analysis_best_sample_index"
                ],
                "writer_best_sample_index": fold_contract[
                    "writer_best_sample_index"
                ],
                "same_best_sample": fold_contract["analysis_best_sample_index"]
                == fold_contract["writer_best_sample_index"],
                "source_metrics_csv": rel(metrics_path),
                "source_refold_cif": rel(refold_cif),
            }
            candidate_rows.append(normalized)

            path_map = {
                "design_cif": pipeline / "intermediate_designs" / f"{candidate_id}.cif",
                "design_npz": pipeline / "intermediate_designs" / f"{candidate_id}.npz",
                "inverse_fold_cif": pipeline
                / "intermediate_designs_inverse_folded"
                / f"{candidate_id}.cif",
                "inverse_fold_npz": pipeline
                / "intermediate_designs_inverse_folded"
                / f"{candidate_id}.npz",
                "fold_npz": fold_npz,
                "refold_cif": refold_cif,
            }
            for artifact_role, path in path_map.items():
                if not path.exists() or path.stat().st_size == 0:
                    raise FileNotFoundError(f"{candidate_id} 缺少 {artifact_role}: {path}")
                lineage_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "scaffold_id": scaffold["candidate_id"],
                        "artifact_role": artifact_role,
                        "path": rel(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )

    raw_all = pd.concat(raw_frames, ignore_index=True, sort=False)
    candidates = pd.DataFrame(candidate_rows).sort_values(
        ["scaffold_selection_rank", "local_candidate_index"]
    )
    if candidates["candidate_id"].duplicated().any():
        raise ValueError("合并后的候选ID碰撞")

    contacts = pd.DataFrame(contact_rows).sort_values(
        ["scaffold_id", "candidate_id", "hotspot_label_seq_id"]
    )
    filters = pd.DataFrame(filter_rows).sort_values(["candidate_id", "filter_order"])
    lineages = pd.DataFrame(lineage_rows).sort_values(["candidate_id", "artifact_role"])
    runs = pd.DataFrame(run_rows).sort_values("selection_rank")
    stages = pd.DataFrame(stage_rows).sort_values(
        ["selection_rank", "scope", "stage"]
    )

    # 过滤失败汇总的分母是骨架内去重后的唯一候选数，而不是24个原始请求。
    filter_summary = (
        filters.groupby(
            [
                "filter_order",
                "filter_label_cn",
                "pass_column",
                "value_column",
                "operator",
                "threshold",
                "unit",
            ],
            as_index=False,
        )
        .agg(candidate_count=("candidate_id", "count"), passed_count=("passed", "sum"))
        .sort_values("filter_order")
    )
    filter_summary["failed_count"] = (
        filter_summary["candidate_count"] - filter_summary["passed_count"]
    )
    filter_summary["failure_rate"] = (
        filter_summary["failed_count"] / filter_summary["candidate_count"]
    )

    per_scaffold = (
        candidates.groupby(
            [
                "scaffold_selection_rank",
                "scaffold_id",
                "scaffold_pdb_code",
                "scaffold_role",
            ],
            as_index=False,
        )
        .agg(
            ranked_unique_candidates=("candidate_id", "count"),
            default_filter_survivors=("pass_all_default_filters", "sum"),
            candidates_with_any_hotspot=(
                "prerefold_hotspot_coverage_fraction_lt8a",
                lambda values: int(sum(float(value) > 0 for value in values)),
            ),
            candidates_passing_complex_rmsd=(
                "filter_rmsd_a",
                lambda values: int(sum(float(value) <= RMSD_THRESHOLD_A for value in values)),
            ),
            median_design_to_target_iptm=("design_to_target_iptm", "median"),
            median_min_design_to_target_pae_a=("min_design_to_target_pae_a", "median"),
            best_computed_filter_pass_count=("computed_filter_pass_count", "max"),
        )
        .sort_values("scaffold_selection_rank")
    )

    return {
        "raw_all": raw_all,
        "candidates": candidates,
        "contacts": contacts,
        "filters": filters,
        "filter_summary": filter_summary,
        "lineages": lineages,
        "runs": runs,
        "stages": stages,
        "per_scaffold": per_scaffold,
    }


def build_sequence_pairs(candidates: pd.DataFrame) -> pd.DataFrame:
    """计算24个候选两两的设计区、完整链、框架和CDR3序列一致性。"""

    rows = []
    records = candidates.to_dict("records")
    for left_index, left in enumerate(records):
        for right_index in range(left_index, len(records)):
            right = records[right_index]
            rows.append(
                {
                    "candidate_id_a": left["candidate_id"],
                    "candidate_id_b": right["candidate_id"],
                    "candidate_label_a": left["candidate_label"],
                    "candidate_label_b": right["candidate_label"],
                    "same_scaffold": left["scaffold_id"] == right["scaffold_id"],
                    "design_sequence_identity": global_alignment_identity(
                        left["designed_sequence"], right["designed_sequence"]
                    ),
                    "full_vhh_sequence_identity": global_alignment_identity(
                        left["designed_chain_sequence"], right["designed_chain_sequence"]
                    ),
                    "framework_sequence_identity": global_alignment_identity(
                        left["framework_sequence"], right["framework_sequence"]
                    ),
                    "cdr3_sequence_identity": global_alignment_identity(
                        left["cdr3_sequence"], right["cdr3_sequence"]
                    ),
                }
            )
    return pd.DataFrame(rows)


def build_funnel(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """构建全局阶段漏斗；骨架内去重后再合并，不跨骨架去重。"""

    runs = tables["runs"]
    candidates = tables["candidates"]
    stages = [
        (1, "scaffolds", "冻结并运行的骨架", len(runs), "scaffold"),
        (2, "requested", "请求候选", int(runs["requested_designs"].sum()), "candidate"),
        (3, "raw_design", "原始设计CIF+NPZ配对", int(runs["raw_design_pairs"].sum()), "candidate"),
        (
            4,
            "inverse_folded",
            "逆折叠CIF+NPZ配对",
            int(runs["inverse_folded_pairs"].sum()),
            "candidate",
        ),
        (5, "fold_npz", "复折叠NPZ", int(runs["fold_npz"].sum()), "candidate"),
        (6, "refold_cif", "复折叠CIF", int(runs["refold_cif"].sum()), "candidate"),
        (7, "analyzed", "分析完成", int(runs["analyzed_rows"].sum()), "candidate"),
        (
            8,
            "ranked_unique",
            "骨架内序列去重后候选",
            len(candidates),
            "candidate",
        ),
        (
            9,
            "pass_filters",
            "通过全部默认计算过滤",
            int(candidates["pass_all_default_filters"].sum()),
            "candidate",
        ),
        (
            10,
            "selected_budget",
            "预算目录展示候选（不等于通过）",
            int(candidates["selected_by_budget"].sum()),
            "candidate",
        ),
    ]
    rows = []
    for order, key, label, count, unit in stages:
        denominator = 12 if unit == "scaffold" else 24
        rows.append(
            {
                "order": order,
                "stage_key": key,
                "stage_label_cn": label,
                "count": count,
                "unit": unit,
                "fraction_of_requested_candidates": (
                    None if unit == "scaffold" else count / denominator
                ),
            }
        )
    return pd.DataFrame(rows)


def selected_attempt_paths(runs: pd.DataFrame) -> list[Path]:
    """从运行表恢复并验证本次分析实际选择的最近完整 attempts。"""

    if "attempt" not in runs.columns or runs.empty:
        raise ValueError("运行表缺少非空attempt列")
    attempts: list[Path] = []
    runs_root = RUNS_DIR.resolve()
    for value in runs["attempt"].astype(str):
        attempt = (RUN_ROOT / value).resolve()
        if runs_root not in attempt.parents or not attempt.is_dir():
            raise ValueError(f"attempt路径越界或不存在：{value}")
        latest, status = latest_complete_attempt(attempt.parent)
        if attempt != latest.resolve() or status.get("status") != "PIPELINE_COMPLETE":
            raise ValueError(f"attempt不是该任务最近的完整运行：{value}")
        attempts.append(attempt)
    if len({path for path in attempts}) != len(attempts):
        raise ValueError("运行表包含重复attempt路径")
    return sorted(attempts)


def build_npz_schema(attempts: Iterable[Path]) -> dict[str, Any]:
    """仅统计本次所选完整 attempts 的NPZ字段、形状与类型。"""

    def role(path: Path) -> str:
        text = path.as_posix()
        if "/fold_out_npz/" in text:
            return "boltz2_refold_prediction_arrays"
        if "/metrics_tmp/" in text:
            return "analysis_cache_arrays"
        if "/intermediate_designs_inverse_folded/" in text:
            return "inverse_fold_metadata_arrays"
        if "/intermediate_designs/" in text:
            return "design_condition_metadata_arrays"
        return "other_npz"

    by_role: dict[str, list[Path]] = defaultdict(list)
    for attempt in sorted({path.resolve() for path in attempts}):
        for path in sorted((attempt / "pipeline").glob("**/*.npz")):
            by_role[role(path)].append(path)

    roles = []
    total = 0
    for role_name, paths in sorted(by_role.items()):
        total += len(paths)
        schema_counter: Counter[str] = Counter()
        schema_examples: dict[str, dict[str, Any]] = {}
        for path in paths:
            with np.load(path, allow_pickle=True) as arrays:
                fields = []
                for key in arrays.files:
                    array = np.asarray(arrays[key])
                    fields.append(
                        {
                            "name": key,
                            "shape": list(array.shape),
                            "dtype": str(array.dtype),
                            "sample": [json_value(value) for value in array.reshape(-1)[:4]],
                        }
                    )
                signature = json.dumps(
                    [(field["name"], field["shape"], field["dtype"]) for field in fields],
                    sort_keys=True,
                )
                schema_counter[signature] += 1
                schema_examples.setdefault(
                    signature,
                    {"example_path": rel(path), "fields": fields},
                )
        variants = []
        for signature, count in schema_counter.items():
            variants.append({"file_count": count, **schema_examples[signature]})
        roles.append(
            {
                "role": role_name,
                "file_count": len(paths),
                "schema_variant_count": len(variants),
                "variants": variants,
            }
        )
    return {
        "schema_version": "1.0.0",
        "generated_at_utc": utc_now(),
        "npz_file_count": total,
        "roles": roles,
        "axis_explanation": {
            "fold_out_token_axis": "一个复合物中的残基/分子令牌；本项目主要是30残基GLP-1加完整VHH",
            "fold_out_atom_axis": "模型原子槽位；可含有效原子/虚拟槽和padding，必须结合atom_resolved_mask与atom_to_token读取",
            "fold_out_coordinate_axis": "x、y、z三维笛卡尔坐标，单位为埃",
            "fold_out_sample_axis": "本轮每候选只有1个Boltz-2复折叠样本",
        },
        "important_limit": "fold_out_npz没有完整二维PAE矩阵，因此报告不绘制PAE热图。",
    }


def build_output_inventory(attempts: Iterable[Path]) -> pd.DataFrame:
    """仅列出本次所选完整 attempts 的产物、格式、大小与SHA-256。"""

    rows = []
    for attempt in sorted({path.resolve() for path in attempts}):
        for path in sorted(attempt.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            text = path.as_posix()
            if "/final_ranked_designs/" in text:
                stage = "filtering_and_ranking"
            elif "/fold_out_npz/" in text or "/refold_cif/" in text:
                stage = "folding"
            elif "/metrics_tmp/" in text or "aggregate_metrics" in text:
                stage = "analysis"
            elif "/intermediate_designs_inverse_folded/" in text:
                stage = "inverse_folding"
            elif "/intermediate_designs/" in text:
                stage = "design"
            elif "/input_check/" in text:
                stage = "input_check"
            elif "/config/" in text or path.name == "steps.yaml":
                stage = "configuration"
            else:
                stage = "run_metadata"
            rows.append(
                {
                    "path": rel(path),
                    "stage": stage,
                    "format": path.suffix.lower().lstrip(".") or "none",
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return pd.DataFrame(rows)


def build_resource_summary() -> dict[str, Any]:
    """汇总运行期间采样到的CPU/RSS；明确MPS内存未测量。"""

    path = ANALYSIS_DIR / "runtime_resource_samples.csv"
    if not path.exists() or path.stat().st_size == 0:
        return {
            "status": "not_available",
            "reason": "资源监控文件不存在；不从checkpoint大小推断内存。",
        }
    samples = pd.read_csv(path)
    if samples.empty:
        return {"status": "not_available", "reason": "资源监控文件没有采样行。"}
    return {
        "status": "partial_observed",
        "sample_count": len(samples),
        "sampling_interval_seconds": 2.0,
        "monitoring_duration_seconds": float(samples["elapsed_seconds"].max()),
        "peak_process_tree_rss_gib": float(samples["rss_gib_sum"].max()),
        "median_process_tree_rss_gib": float(samples["rss_gib_sum"].median()),
        "peak_process_tree_cpu_percent_sum": float(samples["cpu_percent_sum"].max()),
        "note": "监控从第2个骨架运行中途开始；RSS为进程树常驻内存，MPS统一内存未单独测量。",
    }


def build_validation(
    manifest: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    protected_before: dict[str, str],
    protected_after: dict[str, str],
) -> dict[str, Any]:
    """执行阻断性一致性检查并给出可分享状态。"""

    runs = tables["runs"]
    candidates = tables["candidates"]
    filters = tables["filters"]
    lineage = tables["lineages"]
    contacts = tables["contacts"]
    expected_fold_samples = int(
        manifest["compute_profile"]["folding_samples_per_candidate"]
    )
    target_path = (RUN_ROOT / manifest["target"]["path"]).resolve()
    if RUN_ROOT.resolve() not in target_path.parents or not target_path.is_file():
        raise ValueError(f"目标文件路径越界或不存在：{target_path}")
    observed_target_sha256 = sha256_file(target_path)
    per_scaffold_selected = candidates.groupby("scaffold_id")[
        "selected_by_budget"
    ].sum()

    checks = {
        "twelve_unique_scaffolds": runs["scaffold_id"].nunique() == 12 and len(runs) == 12,
        "all_tasks_pipeline_complete": bool((runs["status"] == "PIPELINE_COMPLETE").all()),
        "each_scaffold_has_two_ids_at_every_pre_ranking_stage": bool(
            (
                runs[
                    [
                        "raw_design_pairs",
                        "inverse_folded_pairs",
                        "fold_npz",
                        "refold_cif",
                        "analyzed_rows",
                    ]
                ]
                == 2
            ).all(axis=None)
        ),
        "each_scaffold_has_exact_budget_one_contract": bool(
            (runs[["final_budget_rows", "final_budget_cif"]] == 1).all(axis=None)
            and (per_scaffold_selected == 1).all()
        ),
        "twenty_four_raw_design_pairs": int(runs["raw_design_pairs"].sum()) == 24,
        "twenty_four_inverse_fold_pairs": int(runs["inverse_folded_pairs"].sum()) == 24,
        "twenty_four_fold_npz": int(runs["fold_npz"].sum()) == 24,
        "twenty_four_refold_cif": int(runs["refold_cif"].sum()) == 24,
        "twenty_four_analyzed_rows": int(runs["analyzed_rows"].sum()) == 24,
        "candidate_ids_unique_after_per_scaffold_dedup": not candidates[
            "candidate_id"
        ].duplicated().any(),
        "each_candidate_has_six_lineage_artifacts": bool(
            (lineage.groupby("candidate_id").size() == 6).all()
        ),
        "each_candidate_has_two_hotspot_rows": bool(
            (contacts.groupby("candidate_id").size() == 2).all()
        ),
        "framework_sequence_unchanged_outside_design_mask": bool(
            candidates["framework_sequence_unchanged"].all()
        ),
        "prerefold_hotspot_coverage_is_fraction": set(
            candidates["prerefold_hotspot_coverage_fraction_lt8a"].dropna().astype(float)
        ).issubset({0.0, 0.5, 1.0}),
        "ten_filter_rows_per_candidate": bool(
            (filters.groupby("candidate_id").size() == len(FILTER_DEFINITIONS)).all()
        ),
        "fold_sample_axis_matches_frozen_config": bool(
            (candidates["fold_sample_count"] == expected_fold_samples).all()
        ),
        "fold_best_sample_indexes_are_in_bounds": bool(
            (
                (candidates["analysis_best_sample_index"] >= 0)
                & (
                    candidates["analysis_best_sample_index"]
                    < candidates["fold_sample_count"]
                )
                & (candidates["writer_best_sample_index"] >= 0)
                & (
                    candidates["writer_best_sample_index"]
                    < candidates["fold_sample_count"]
                )
            ).all()
        ),
        "source_files_unchanged_by_analysis": protected_before == protected_after,
        "actual_target_file_sha_matches_manifest": observed_target_sha256
        == manifest["target"]["sha256"],
        "target_manifest_sha_matches_frozen_expected": manifest["target"]["sha256"]
        == "11b82b2633793e6799f1d56c19a88fd52828bec5d26d9366801753dfa72d2d53",
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": "1.0.0",
        "validated_at_utc": utc_now(),
        "assessment": "READY_TO_SHARE_WITH_SCIENTIFIC_CAVEATS" if not failed else "NEEDS_REVISION",
        "checks": checks,
        "failed_checks": failed,
        "target_path": rel(target_path),
        "target_observed_sha256": observed_target_sha256,
        "target_manifest_sha256": manifest["target"]["sha256"],
        "scientific_caveats": [
            "本轮是预训练模型推理，不是模型权重训练。",
            "只有单一6X18受体结合态几何，没有GLP-1(9-36)或多构象反筛。",
            "目标C端酰胺未在当前标准聚合物CIF中完成原子级验证。",
            "iPTM、PAE、RMSD、SASA和接触数都是计算代理，不是Kd或实验命中。",
            "每骨架n=2，只能做流程与候选级描述，不能估计骨架因果效应或模型命中率。",
            "实验性MPS结果不等同于官方Linux+NVIDIA基线。",
        ],
    }


def plot_funnel(funnel: pd.DataFrame) -> None:
    """绘制候选阶段条形漏斗，避免狭窄漏斗形状遮挡文字。"""

    candidate_rows = funnel[funnel["unit"] == "candidate"].copy()
    fig, ax = plt.subplots(figsize=(11, 6.5))
    colors = [COLORS["red"] if key == "pass_filters" else COLORS["blue"] for key in candidate_rows["stage_key"]]
    ax.barh(candidate_rows["stage_label_cn"], candidate_rows["count"], color=colors)
    ax.invert_yaxis()
    ax.set_xlim(0, 26)
    ax.set_xlabel("候选数")
    ax.set_title("第一轮候选流程漏斗", loc="left", fontweight="bold")
    for index, value in enumerate(candidate_rows["count"]):
        ax.text(value + 0.35, index, f"{int(value)}", va="center", fontweight="bold")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    fig.text(
        0.01,
        0.01,
        "分母为24个请求候选；去重只在各骨架内部进行。预算目录展示不等于通过过滤。",
        fontsize=9,
        color=COLORS["gray"],
    )
    save_figure(fig, "01_process_funnel.png")


def plot_filter_failures(summary: pd.DataFrame) -> None:
    """绘制每个启用过滤条件的失败候选数与失败率。"""

    ordered = summary.sort_values("failed_count", ascending=True)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.barh(ordered["filter_label_cn"], ordered["failed_count"], color=COLORS["amber"])
    ax.set_xlim(0, max(1, int(ordered["candidate_count"].max())) + 2)
    ax.set_xlabel("失败的骨架内去重候选数")
    ax.set_title("默认过滤失败分布", loc="left", fontweight="bold")
    for index, row in enumerate(ordered.itertuples()):
        ax.text(
            row.failed_count + 0.25,
            index,
            f"{int(row.failed_count)}/{int(row.candidate_count)} ({row.failure_rate:.0%})",
            va="center",
        )
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    save_figure(fig, "02_filter_failures.png")


def plot_rmsd(candidates: pd.DataFrame) -> None:
    """按骨架展示两个RMSD维度，保留2.5 Å硬门槛。"""

    labels = [
        f"{rank:02d} {pdb}"
        for rank, pdb in zip(
            candidates["scaffold_selection_rank"], candidates["scaffold_pdb_code"]
        )
    ]
    x = np.arange(len(candidates))
    fig, axes = plt.subplots(2, 1, figsize=(14, 8.5), sharex=True)
    for ax, column, title, color in (
        (
            axes[0],
            "filter_rmsd_a",
            "复合物骨架均方根偏差",
            COLORS["blue"],
        ),
        (
            axes[1],
            "filter_rmsd_design_a",
            "VHH设计区骨架均方根偏差",
            COLORS["teal"],
        ),
    ):
        ax.scatter(x, candidates[column], c=color, s=45, edgecolor=COLORS["navy"], linewidth=0.5)
        ax.axhline(RMSD_THRESHOLD_A, color=COLORS["red"], linestyle="--", linewidth=1.4)
        ax.set_ylabel("Å（越低越好）")
        ax.set_title(title, loc="left", fontweight="bold")
        ax.grid(axis="y")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(labels, rotation=65, ha="right", fontsize=8)
    fig.suptitle(
        f"{len(candidates)}个骨架内去重候选的重折叠自洽性",
        x=0.01,
        ha="left",
        fontsize=17,
        fontweight="bold",
    )
    fig.text(
        0.01,
        0.01,
        "虚线为官方默认2.5 Å门槛；横轴按骨架筛选顺序及本地候选编号排列。",
        fontsize=9,
    )
    save_figure(fig, "03_rmsd_by_scaffold.png")


def plot_interface_proxies(candidates: pd.DataFrame) -> None:
    """绘制界面置信代理关系图，不合成为亲和力分数。"""

    fig, ax = plt.subplots(figsize=(10, 7.5))
    coverage = candidates["prerefold_hotspot_coverage_fraction_lt8a"].astype(float)
    scatter = ax.scatter(
        candidates["design_to_target_iptm"],
        candidates["min_design_to_target_pae_a"],
        c=coverage,
        cmap="viridis",
        vmin=0,
        vmax=1,
        s=70,
        edgecolor=COLORS["navy"],
        linewidth=0.6,
    )
    for _, row in candidates.nlargest(5, "computed_filter_pass_count").iterrows():
        ax.annotate(
            row["candidate_label"],
            (row["design_to_target_iptm"], row["min_design_to_target_pae_a"]),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=8,
        )
    colorbar = fig.colorbar(scatter, ax=ax)
    colorbar.set_label("BoltzGen复折叠前 His7/Ala8 token-center覆盖比例（8 Å）")
    ax.set_xlabel("设计残基→目标 iPTM（高更好，仅结构代理）")
    ax.set_ylabel("最小设计残基→目标 PAE，Å（低更好）")
    ax.set_title("界面置信代理", loc="left", fontweight="bold")
    ax.grid(True)
    save_figure(fig, "04_interface_proxies.png")


def plot_hotspot_distances(candidates: pd.DataFrame) -> None:
    """显示独立重算的His7与Ala8最小重原子距离。"""

    x = np.arange(len(candidates))
    labels = candidates["candidate_label"].tolist()
    fig, ax = plt.subplots(figsize=(14, 6.5))
    ax.scatter(x - 0.1, candidates["his7_min_heavy_atom_distance_a"], label="His7", color=COLORS["purple"], s=48)
    ax.scatter(x + 0.1, candidates["ala8_min_heavy_atom_distance_a"], label="Ala8", color=COLORS["amber"], marker="s", s=44)
    ax.axhline(HOTSPOT_DISTANCE_THRESHOLD_A, color=COLORS["red"], linestyle="--")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=65, ha="right", fontsize=8)
    ax.set_ylabel("目标热点到设计区最小重原子距离，Å")
    ax.set_title(
        "从复折叠CIF独立重算的His7/Ala8几何距离",
        loc="left",
        fontweight="bold",
    )
    ax.legend(ncol=2, frameon=False)
    ax.grid(axis="y")
    save_figure(fig, "05_hotspot_distances.png")


def plot_sequence_identity(candidates: pd.DataFrame, pairs: pd.DataFrame) -> None:
    """绘制设计区序列全局比对一致性热图。"""

    labels = candidates["candidate_label"].tolist()
    index = {candidate_id: position for position, candidate_id in enumerate(candidates["candidate_id"])}
    matrix = np.eye(len(candidates))
    for row in pairs.itertuples():
        i = index[row.candidate_id_a]
        j = index[row.candidate_id_b]
        matrix[i, j] = row.design_sequence_identity
        matrix[j, i] = row.design_sequence_identity
    fig, ax = plt.subplots(figsize=(12.5, 11))
    image = ax.imshow(matrix, vmin=0, vmax=1, cmap="Blues")
    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=70, ha="right", fontsize=7.5)
    ax.set_yticklabels(labels, fontsize=7.5)
    ax.set_title("设计区序列一致性矩阵", loc="left", fontweight="bold")
    colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    colorbar.set_label("全局比对一致性")
    save_figure(fig, "06_design_sequence_identity.png")


def plot_stage_timings(stages: pd.DataFrame) -> None:
    """按骨架绘制五个模型阶段的堆叠耗时。"""

    model = stages[stages["scope"] == "model_step"].copy()
    order = ["design", "inverse_folding", "folding", "analysis", "filtering"]
    pivot = model.pivot_table(
        index=["selection_rank", "pdb_code"],
        columns="stage",
        values="elapsed_seconds",
        aggfunc="sum",
        fill_value=0,
    ).sort_index()
    fig, ax = plt.subplots(figsize=(12, 7))
    left = np.zeros(len(pivot))
    colors = [COLORS["blue"], COLORS["teal"], COLORS["purple"], COLORS["amber"], COLORS["gray"]]
    labels_cn = ["扩散设计", "逆折叠", "复合物复折叠", "指标分析", "过滤排序"]
    for stage, label, color in zip(order, labels_cn, colors):
        values = pivot[stage].to_numpy() if stage in pivot else np.zeros(len(pivot))
        ax.barh(range(len(pivot)), values, left=left, label=label, color=color)
        left += values
    ax.set_yticks(range(len(pivot)))
    ax.set_yticklabels([f"{rank:02d} {pdb}" for rank, pdb in pivot.index])
    ax.invert_yaxis()
    ax.set_xlabel("秒")
    ax.set_title("每个骨架的五步模型耗时", loc="left", fontweight="bold")
    ax.legend(ncol=3, frameon=False, loc="lower right")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    save_figure(fig, "07_stage_timings.png")


def plot_resource_usage() -> None:
    """绘制进程树CPU和RSS采样；不使用双轴以避免量纲混淆。"""

    path = ANALYSIS_DIR / "runtime_resource_samples.csv"
    if not path.exists():
        return
    samples = pd.read_csv(path)
    if samples.empty:
        return
    minutes = samples["elapsed_seconds"] / 60.0
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    axes[0].plot(minutes, samples["rss_gib_sum"], color=COLORS["blue"])
    axes[0].set_ylabel("进程树RSS，GiB")
    axes[0].set_title("常驻内存采样", loc="left", fontweight="bold")
    axes[0].grid(True)
    axes[1].plot(minutes, samples["cpu_percent_sum"], color=COLORS["amber"])
    axes[1].set_ylabel("进程树CPU%，可超过100")
    axes[1].set_xlabel("监控启动后分钟")
    axes[1].set_title("CPU使用率采样", loc="left", fontweight="bold")
    axes[1].grid(True)
    fig.text(
        0.01,
        0.01,
        "监控从第2个骨架运行中途开始；MPS统一内存未单独测量。",
        fontsize=9,
        color=COLORS["gray"],
    )
    save_figure(fig, "08_resource_usage.png")


def plot_interface_geometry(candidates: pd.DataFrame) -> None:
    """对每个骨架预算展示候选画出目标侧ΔSASA和几何氢键，量纲分面。"""

    selected = candidates[candidates["selected_by_budget"]].sort_values(
        "scaffold_selection_rank"
    )
    x = np.arange(len(selected))
    labels = selected["scaffold_pdb_code"].tolist()
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    axes[0].bar(x, selected["target_delta_sasa_refolded_a2"], color=COLORS["teal"])
    axes[0].set_ylabel("目标侧ΔSASA，Å²")
    axes[0].set_title("目标侧结合前后溶剂可接触面积减少", loc="left", fontweight="bold")
    axes[0].grid(axis="y")
    axes[1].bar(x, selected["geometric_hbond_count_refolded"], color=COLORS["purple"])
    axes[1].set_ylabel("几何氢键计数")
    axes[1].set_title("复折叠界面几何氢键", loc="left", fontweight="bold")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(labels, rotation=45, ha="right")
    axes[1].grid(axis="y")
    fig.text(
        0.01,
        0.01,
        "每个骨架仅展示预算排序第1名；该目录即使无人通过过滤也会保留候选。",
        fontsize=9,
    )
    save_figure(fig, "09_interface_geometry.png")


def plot_budget_representative_structures(candidates: pd.DataFrame) -> None:
    """绘制每个骨架的预算代表复折叠结构 Cα 轨迹。

    图中“预算代表”只表示 BoltzGen 在该骨架内部排序后复制到 budget=1 目录的候选，
    绝不暗示它通过了全部过滤。这里只画 Cα 轨迹，目的是展示候选总体构型、目标相对
    位置和设计区分布；它不是原子级界面验证图。
    """

    selected = candidates[candidates["selected_by_budget"]].sort_values(
        "scaffold_selection_rank"
    )
    if selected.empty:
        return

    # 采用四列小多图；行数由真实骨架数决定，不把 12 或 24 写死到绘图逻辑中。
    columns = min(4, len(selected))
    rows = math.ceil(len(selected) / columns)
    fig = plt.figure(figsize=(4.2 * columns, 3.7 * rows))

    for panel_index, row in enumerate(selected.itertuples(), start=1):
        axis = fig.add_subplot(rows, columns, panel_index, projection="3d")
        structure = gemmi.read_structure(str(RUN_ROOT / row.source_refold_cif))
        target, binder = choose_target_and_binder(structure[0])
        scaffold_yaml = (
            INPUT_DIR
            / "scaffolds"
            / f"{int(row.scaffold_selection_rank):02d}_{row.scaffold_id}"
            / "scaffold.yaml"
        )
        design_positions = set(parse_design_positions(scaffold_yaml))

        def ca_coordinates(chain: gemmi.Chain, positions: set[int] | None = None) -> np.ndarray:
            """提取一条链或指定位置集合的 Cα 坐标。"""

            coordinates = []
            for canonical_position, residue in enumerate(chain, start=1):
                # 与输入 YAML 相同，按链内 canonical 顺序解释设计位置，不依赖 auth 编号。
                if positions is not None and canonical_position not in positions:
                    continue
                atom = residue.find_atom("CA", "*")
                if atom:
                    coordinates.append([atom.pos.x, atom.pos.y, atom.pos.z])
            return np.asarray(coordinates, dtype=float)

        target_ca = ca_coordinates(target)
        binder_ca = ca_coordinates(binder)
        design_ca = ca_coordinates(binder, design_positions)
        hotspot_ca = target_ca[:2]

        axis.plot(
            binder_ca[:, 0], binder_ca[:, 1], binder_ca[:, 2],
            color=COLORS["navy"], linewidth=1.8, alpha=0.88,
        )
        axis.scatter(
            design_ca[:, 0], design_ca[:, 1], design_ca[:, 2],
            color=COLORS["teal"], s=9, alpha=0.9,
        )
        axis.plot(
            target_ca[:, 0], target_ca[:, 1], target_ca[:, 2],
            color=COLORS["amber"], linewidth=3.0,
        )
        axis.scatter(
            hotspot_ca[:, 0], hotspot_ca[:, 1], hotspot_ca[:, 2],
            color=COLORS["red"], s=36, depthshade=False,
        )

        # 使用全部坐标的统一跨度，防止某个轴被压扁而造成形状误读。
        combined = np.vstack([target_ca, binder_ca])
        center = combined.mean(axis=0)
        span = max(float(np.ptp(combined, axis=0).max()), 1.0)
        half = span * 0.56
        axis.set_xlim(center[0] - half, center[0] + half)
        axis.set_ylim(center[1] - half, center[1] + half)
        axis.set_zlim(center[2] - half, center[2] + half)
        axis.set_box_aspect((1, 1, 1))
        axis.view_init(elev=18, azim=-58)
        axis.set_axis_off()
        state = "全过滤通过" if row.pass_all_default_filters else "未全通过"
        axis.set_title(
            f"{row.scaffold_pdb_code} · {row.candidate_id.rsplit('_', 1)[-1]}\n{state}",
            fontsize=10.5,
            pad=3,
        )

    # 空白网格位不创建坐标轴；用全局文字解释颜色与科学边界。
    fig.suptitle(
        "每个骨架的预算代表：复折叠 Cα 轨迹（代表不等于通过）",
        fontsize=16,
        y=0.99,
    )
    fig.text(
        0.5,
        0.012,
        "深蓝=VHH主链；青色点=设计位点；橙色=GLP-1；红色=His7/Ala8。仅作几何概览。",
        ha="center",
        fontsize=10.5,
        color=COLORS["ink"],
    )
    fig.tight_layout(rect=(0, 0.035, 1, 0.97))
    save_figure(fig, "10_budget_representative_ca_traces.png")


def plot_hotspot_stage_comparison(candidates: pd.DataFrame) -> None:
    """比较复折叠前官方 token-center 覆盖与复折叠后独立重原子覆盖。

    两个值来自不同结构阶段、不同几何定义，因此本图只用于发现“设计阶段提示位点信号
    是否在复折叠后仍存在”，不用于宣称两种实现应该逐点相等。
    """

    ordered = candidates.sort_values(
        ["scaffold_selection_rank", "local_candidate_index"]
    ).reset_index(drop=True)
    before = ordered["prerefold_hotspot_coverage_fraction_lt8a"].astype(float)
    after = ordered["independent_hotspot_coverage_heavy_lt8a"].astype(float)
    x = np.arange(len(ordered))
    fig, ax = plt.subplots(figsize=(14, 6.5))
    for index in x:
        color = COLORS["red"] if before.iloc[index] != after.iloc[index] else COLORS["gray"]
        ax.plot(
            [index, index],
            [before.iloc[index], after.iloc[index]],
            color=color,
            linewidth=1.2,
            alpha=0.75,
        )
    ax.scatter(
        x - 0.07,
        before,
        color=COLORS["purple"],
        marker="s",
        s=46,
        label="复折叠前：BoltzGen token-center覆盖",
        zorder=3,
    )
    ax.scatter(
        x + 0.07,
        after,
        color=COLORS["teal"],
        marker="o",
        s=48,
        label="复折叠后：独立重原子覆盖",
        zorder=3,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(ordered["candidate_label"], rotation=65, ha="right", fontsize=8)
    ax.set_yticks([0, 0.5, 1.0], ["0/2", "1/2", "2/2"])
    ax.set_ylim(-0.08, 1.08)
    ax.set_ylabel("His7/Ala8 被覆盖的位点数")
    ax.set_title(
        "提示位点覆盖的阶段稳定性：红色连线表示前后定义结果不同",
        loc="left",
        fontweight="bold",
    )
    ax.legend(frameon=False, ncol=2, loc="upper center")
    ax.grid(axis="y")
    save_figure(fig, "11_hotspot_stage_comparison.png")


def build_summary(
    manifest: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    funnel: pd.DataFrame,
    resource_summary: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    """生成报告与Notebook共用的机器可读核心结论。"""

    candidates = tables["candidates"]
    runs = tables["runs"]
    filter_summary = tables["filter_summary"]
    survivor_count = int(candidates["pass_all_default_filters"].sum())
    closest = candidates.sort_values(
        [
            "computed_filter_pass_count",
            "design_to_target_iptm",
            "min_design_to_target_pae_a",
        ],
        ascending=[False, False, True],
    ).head(5)
    top_failures = filter_summary.sort_values(
        ["failed_count", "filter_order"], ascending=[False, True]
    ).head(3)
    return {
        "schema_version": "1.0.0",
        "generated_at_utc": utc_now(),
        "campaign_id": manifest["campaign_id"],
        "result_classification": (
            "PIPELINE_COMPLETE_WITH_DEFAULT_FILTER_SURVIVORS"
            if survivor_count > 0
            else "PIPELINE_COMPLETE_BUT_ZERO_DEFAULT_FILTER_SURVIVORS"
        ),
        "engineering_status": {
            "scaffold_tasks_complete": int((runs["status"] == "PIPELINE_COMPLETE").sum()),
            "scaffold_tasks_total": len(runs),
            "raw_candidates_requested": 24,
            "raw_design_pairs_complete": int(runs["raw_design_pairs"].sum()),
            "ranked_unique_candidates": len(candidates),
            "default_filter_survivors": survivor_count,
            "budget_display_candidates": int(candidates["selected_by_budget"].sum()),
        },
        "compute": {
            "sum_execute_seconds": float(runs["execute_seconds"].sum()),
            "median_execute_seconds_per_scaffold": float(runs["execute_seconds"].median()),
            "min_execute_seconds_per_scaffold": float(runs["execute_seconds"].min()),
            "max_execute_seconds_per_scaffold": float(runs["execute_seconds"].max()),
            "resource_observation": resource_summary,
        },
        "leading_failure_modes": [
            {
                "filter": row.filter_label_cn,
                "failed_count": int(row.failed_count),
                "candidate_count": int(row.candidate_count),
                "failure_rate": float(row.failure_rate),
            }
            for row in top_failures.itertuples()
        ],
        "manual_review_priority_not_binders": [
            {
                "candidate_id": row.candidate_id,
                "candidate_label": row.candidate_label,
                "passed_filter_count": int(row.computed_filter_pass_count),
                "failed_filters_cn": row.failed_filters_cn,
                "design_to_target_iptm": float(row.design_to_target_iptm),
                "min_design_to_target_pae_a": float(row.min_design_to_target_pae_a),
                "reason": "仅按通过条件数、iPTM和PAE排序供人工复盘；不是亲和力或结合概率",
            }
            for row in closest.itertuples()
        ],
        "target_boundary": {
            "target_role": manifest["target"]["role"],
            "terminal_amide_atomically_verified": False,
            "off_target_or_multiconformation_evaluated": False,
            "selectivity_claim_allowed": False,
        },
        "validation_assessment": validation["assessment"],
        "validation_failed_checks": validation["failed_checks"],
        "funnel_table": "analysis/process_funnel.csv",
        "candidate_table": "analysis/candidate_metrics.csv",
        "notebook": "notebooks/BoltzGen_旧12骨架_GL P1_第一轮复盘.ipynb".replace(" ", ""),
    }


def main() -> int:
    """执行只读分析、验证、绘图和机器可读输出。"""

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    configure_plot_style()
    protected_before = protected_source_snapshot()
    manifest = json.loads(INPUT_MANIFEST.read_text(encoding="utf-8"))

    tables = build_tables(manifest)
    sequence_pairs = build_sequence_pairs(tables["candidates"])
    funnel = build_funnel(tables)
    analyzed_attempts = selected_attempt_paths(tables["runs"])
    npz_schema = build_npz_schema(analyzed_attempts)
    output_inventory = build_output_inventory(analyzed_attempts)
    resource_summary = build_resource_summary()

    # 先写所有规范表；原始269列合并表也完整保留，方便以后复核未展示字段。
    tables["raw_all"].to_csv(ANALYSIS_DIR / "raw_all_designs_metrics.csv", index=False)
    tables["candidates"].to_csv(ANALYSIS_DIR / "candidate_metrics.csv", index=False)
    tables["contacts"].to_csv(
        ANALYSIS_DIR / "interface_contacts_independent.csv", index=False
    )
    tables["filters"].to_csv(ANALYSIS_DIR / "candidate_filter_results.csv", index=False)
    tables["filter_summary"].to_csv(ANALYSIS_DIR / "filter_summary.csv", index=False)
    tables["lineages"].to_csv(ANALYSIS_DIR / "candidate_lineage.csv", index=False)
    tables["runs"].to_csv(ANALYSIS_DIR / "run_manifest.csv", index=False)
    tables["stages"].to_csv(ANALYSIS_DIR / "stage_timings.csv", index=False)
    tables["per_scaffold"].to_csv(ANALYSIS_DIR / "per_scaffold_summary.csv", index=False)
    sequence_pairs.to_csv(ANALYSIS_DIR / "sequence_pairs.csv", index=False)
    funnel.to_csv(ANALYSIS_DIR / "process_funnel.csv", index=False)
    output_inventory.to_csv(ANALYSIS_DIR / "output_inventory.tsv", sep="\t", index=False)
    write_json(ANALYSIS_DIR / "npz_schema.json", npz_schema)
    write_json(ANALYSIS_DIR / "resource_summary.json", resource_summary)

    # 图表全部由真实规范表驱动，不硬编码候选1/2或固定结果数量。
    plot_funnel(funnel)
    plot_filter_failures(tables["filter_summary"])
    plot_rmsd(tables["candidates"])
    plot_interface_proxies(tables["candidates"])
    plot_hotspot_distances(tables["candidates"])
    plot_sequence_identity(tables["candidates"], sequence_pairs)
    plot_stage_timings(tables["stages"])
    plot_resource_usage()
    plot_interface_geometry(tables["candidates"])
    plot_budget_representative_structures(tables["candidates"])
    plot_hotspot_stage_comparison(tables["candidates"])

    protected_after = protected_source_snapshot()
    validation = build_validation(
        manifest, tables, protected_before, protected_after
    )
    write_json(ANALYSIS_DIR / "validation_report.json", validation)
    if validation["failed_checks"]:
        raise RuntimeError(
            "分析验证存在阻断项：" + ", ".join(validation["failed_checks"])
        )

    summary = build_summary(
        manifest, tables, funnel, resource_summary, validation
    )
    write_json(ANALYSIS_DIR / "run_summary.json", summary)

    print("分析完成：")
    print("  骨架任务：", summary["engineering_status"]["scaffold_tasks_complete"], "/12")
    print("  原始候选：", summary["engineering_status"]["raw_design_pairs_complete"], "/24")
    print("  骨架内去重候选：", summary["engineering_status"]["ranked_unique_candidates"])
    print("  通过全部默认过滤：", summary["engineering_status"]["default_filter_survivors"])
    print("  验证状态：", summary["validation_assessment"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
