#!/usr/bin/env python3
"""严格、只读地汇总 Mac 上两条 BoltzGen 单检查点支路。

本脚本只读取 ``runs/``、``inputs/`` 与 ``provenance/``，只写 ``analysis/``
与 ``figures/``。它不会加载模型、启动推理、修改检查点，也不会触碰旧 round1。

分析对象固定为：

* ``balanced_diverse_all12``：12 个骨架，每个骨架 2 个候选；
* ``balanced_adherence_all12``：12 个骨架，每个骨架 2 个候选；
* 每个候选恰有 2 个复折叠样本。

因此主分析必须恰有 48 个候选、96 个复折叠样本。任何支路尚未完成、文件缺失、
样本轴错误、逐项过滤不一致或候选谱系断裂都会阻断输出，绝不以占位值补齐。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import gemmi
import matplotlib

# 使用无窗口绘图后端，保证终端、Notebook 与自动化重放都能稳定产图。
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml


项目根目录 = Path(__file__).resolve().parent.parent
输入目录 = 项目根目录 / "inputs"
运行目录 = 项目根目录 / "runs"
溯源目录 = 项目根目录 / "provenance"
分析目录 = 项目根目录 / "analysis"
图片目录 = 项目根目录 / "figures"
输入清单路径 = 溯源目录 / "enhanced_input_manifest.json"

主支路 = (
    "balanced_diverse_all12",
    "balanced_adherence_all12",
)
深度探针支路 = "near_official_adherence_7xl0"
支路中文名 = {
    "balanced_diverse_all12": "多样性检查点",
    "balanced_adherence_all12": "骨架遵循检查点",
}
支路检查点 = {
    "balanced_diverse_all12": "design_diverse",
    "balanced_adherence_all12": "design_adherence",
}
预期阶段 = (
    "00_check",
    "00_configure",
    "01_design",
    "02_inverse_folding",
    "03_folding",
    "04_analysis",
    "05_filtering",
)
预期模型阶段 = ("design", "inverse_folding", "folding", "analysis", "filtering")
目标序列 = "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR"
重原子距离阈值埃 = 8.0
复合物均方根偏差阈值埃 = 2.5
每骨架候选数 = 2
每候选样本数 = 2
预期骨架数 = 12
预期候选数 = 48
预期样本数 = 96
吉字节 = 1024**3

# 这些数组在 fold_out_npz 中以复折叠样本为第一轴；每个数组必须恰有 2 项。
样本标量数组 = (
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
)

# 字段和阈值来自本次冻结的 antibody filtering 结果表。
# ``num_filters_passed`` 是 BoltzGen 内部前缀分数，不是下面十项的通过数。
过滤定义 = (
    (1, "pass_has_x_filter", "has_x", "未知残基 X", "<=", 0.0, "flag"),
    (2, "pass_filter_rmsd_filter", "filter_rmsd", "复合物骨架均方根偏差", "<=", 2.5, "Å"),
    (3, "pass_filter_rmsd_design_filter", "filter_rmsd_design", "VHH 设计区骨架均方根偏差", "<=", 2.5, "Å"),
    (
        4,
        "pass_bindsite_under_8rmsd_filter",
        "bindsite_under_8rmsd",
        "复折叠前 His7/Ala8 提示位点 8 Å 覆盖",
        ">=",
        0.0001,
        "fraction",
    ),
    (5, "pass_CYS_fraction_filter", "CYS_fraction", "设计区半胱氨酸比例", "<=", 0.0, "fraction"),
    (6, "pass_ALA_fraction_filter", "ALA_fraction", "设计区丙氨酸比例", "<=", 0.3, "fraction"),
    (7, "pass_GLY_fraction_filter", "GLY_fraction", "设计区甘氨酸比例", "<=", 0.3, "fraction"),
    (8, "pass_GLU_fraction_filter", "GLU_fraction", "设计区谷氨酸比例", "<=", 0.3, "fraction"),
    (9, "pass_LEU_fraction_filter", "LEU_fraction", "设计区亮氨酸比例", "<=", 0.3, "fraction"),
    (10, "pass_VAL_fraction_filter", "VAL_fraction", "设计区缬氨酸比例", "<=", 0.3, "fraction"),
)

颜色 = {
    "深蓝": "#12304A",
    "蓝": "#277DA1",
    "青": "#2A9D8F",
    "橙": "#E69F00",
    "红": "#C75B4B",
    "紫": "#7B61A8",
    "灰": "#8796A5",
}


class 数据尚未就绪(RuntimeError):
    """表示两条主支路尚未全部完成，调用方应等待而不是生成占位结果。"""


def 当前世界时() -> str:
    """返回带时区的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def 相对路径(path: Path) -> str:
    """把根目录内路径转换为稳定的 POSIX 相对路径，并拒绝越界。"""

    resolved = path.resolve()
    return resolved.relative_to(项目根目录.resolve()).as_posix()


def 读取JSON(path: Path) -> Any:
    """读取 UTF-8 JSON；缺失或空文件立即报错。"""

    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"缺少或为空的 JSON：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def 文件哈希(path: Path, 块大小: int = 2 * 1024 * 1024) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(块大小), b""):
            digest.update(chunk)
    return digest.hexdigest()


def 有限浮点(value: Any, 字段: str) -> float:
    """把值严格转换为有限浮点数。"""

    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{字段} 不是浮点数：{value!r}") from error
    if not math.isfinite(result):
        raise ValueError(f"{字段} 不是有限数：{value!r}")
    return result


def 布尔值(value: Any, 字段: str) -> bool:
    """严格解析 CSV/JSON 布尔值，不把任意非空字符串当作真。"""

    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"{字段} 不是合法布尔值：{value!r}")


def 阈值通过(value: float, operator: str, threshold: float) -> bool:
    """按冻结运算符重算过滤结果。"""

    if operator == "<=":
        return value <= threshold
    if operator == ">=":
        return value >= threshold
    raise ValueError(f"不支持的过滤运算符：{operator}")


def 原子写文本(path: Path, text: str) -> None:
    """仅在两个授权输出目录中原子写文件。"""

    resolved_parent = path.resolve().parent
    if 分析目录.resolve() != resolved_parent and 图片目录.resolve() != resolved_parent:
        raise ValueError(f"拒绝写入授权目录之外：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def 写JSON(path: Path, payload: Any) -> None:
    """写出带缩进的稳定 JSON。"""

    原子写文本(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def 写表(path: Path, frame: pd.DataFrame) -> None:
    """原子写 UTF-8 CSV，保留布尔和缺失值的显式表示。"""

    if path.parent.resolve() != 分析目录.resolve():
        raise ValueError(f"CSV 只能写入 analysis：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8", lineterminator="\n")
    temporary.replace(path)


def 最新尝试(task_root: Path) -> Path | None:
    """返回任务目录按编号排序的最新 attempt；没有则返回空。"""

    attempts = sorted(path for path in task_root.glob("attempt_*") if path.is_dir())
    return attempts[-1] if attempts else None


def 就绪审计(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any], Path, dict[str, Any]]]]:
    """检查两条支路的 24 个最新尝试是否全部完整。

    返回值中的尝试列表只在 ``ready=true`` 时可供后续分析使用。
    """

    records = manifest.get("scaffold_population", {}).get("records", [])
    if len(records) != 预期骨架数:
        raise ValueError(f"冻结清单骨架数不是 {预期骨架数}：{len(records)}")
    ranks = [int(record["selection_rank"]) for record in records]
    if sorted(ranks) != list(range(1, 预期骨架数 + 1)):
        raise ValueError(f"骨架 selection_rank 不连续：{sorted(ranks)}")

    details: list[dict[str, Any]] = []
    selected: list[tuple[str, dict[str, Any], Path, dict[str, Any]]] = []
    for profile in 主支路:
        profile_contract = manifest.get("profiles", {}).get(profile)
        if not isinstance(profile_contract, dict):
            raise ValueError(f"冻结清单缺少主支路：{profile}")
        if int(profile_contract.get("num_designs", -1)) != 每骨架候选数:
            raise ValueError(f"{profile} 的 num_designs 不是 {每骨架候选数}")
        if int(profile_contract.get("folding_diffusion_samples", -1)) != 每候选样本数:
            raise ValueError(f"{profile} 的 folding_diffusion_samples 不是 {每候选样本数}")
        if profile_contract.get("design_checkpoints") != [支路检查点[profile]]:
            raise ValueError(f"{profile} 不是预期单检查点配置")

        for scaffold in records:
            rank = int(scaffold["selection_rank"])
            task_name = f"{rank:02d}_{scaffold['candidate_id']}"
            attempt = 最新尝试(运行目录 / profile / task_name)
            if attempt is None:
                details.append(
                    {
                        "profile": profile,
                        "selection_rank": rank,
                        "scaffold_id": scaffold["candidate_id"],
                        "task": task_name,
                        "latest_attempt": None,
                        "status": "MISSING",
                        "ready": False,
                    }
                )
                continue
            status_path = attempt / "run_status.json"
            status = 读取JSON(status_path) if status_path.is_file() else {"status": "RUNNING_OR_UNRECORDED"}
            complete = status.get("status") == "PIPELINE_COMPLETE"
            details.append(
                {
                    "profile": profile,
                    "selection_rank": rank,
                    "scaffold_id": scaffold["candidate_id"],
                    "task": task_name,
                    "latest_attempt": 相对路径(attempt),
                    "status": status.get("status"),
                    "ready": complete,
                }
            )
            if complete:
                selected.append((profile, scaffold, attempt, status))

    main_ready = len(selected) == len(主支路) * 预期骨架数

    # near-official 探针是独立分析域：若冻结 provenance 声明了该 profile，正式输出
    # 必须等待它完整，但它永远不加入 48 个主候选的分母。
    deep_contract = manifest.get("profiles", {}).get(深度探针支路)
    deep_detail: dict[str, Any]
    if deep_contract is None:
        deep_detail = {
            "profile": 深度探针支路,
            "required_by_current_provenance": False,
            "ready": True,
            "status": "NOT_DECLARED",
            "latest_attempt": None,
        }
    else:
        if not (
            int(deep_contract.get("num_designs", -1)) == 4
            and int(deep_contract.get("folding_diffusion_samples", -1)) == 1
            and deep_contract.get("design_checkpoints") == ["design_adherence"]
            and deep_contract.get("selection_ranks") == [1]
        ):
            raise ValueError("near_official_adherence_7xl0 冻结合同不是 7XL0/4候选/1样本/单adherence")
        deep_scaffold = next(record for record in records if int(record["selection_rank"]) == 1)
        deep_task = f"01_{deep_scaffold['candidate_id']}"
        deep_attempt = 最新尝试(运行目录 / 深度探针支路 / deep_task)
        if deep_attempt is None:
            deep_detail = {
                "profile": 深度探针支路,
                "required_by_current_provenance": True,
                "ready": False,
                "status": "MISSING",
                "latest_attempt": None,
                "task": deep_task,
            }
        else:
            deep_status_path = deep_attempt / "run_status.json"
            deep_status = (
                读取JSON(deep_status_path)
                if deep_status_path.is_file()
                else {"status": "RUNNING_OR_UNRECORDED"}
            )
            deep_detail = {
                "profile": 深度探针支路,
                "required_by_current_provenance": True,
                "ready": deep_status.get("status") == "PIPELINE_COMPLETE",
                "status": deep_status.get("status"),
                "latest_attempt": 相对路径(deep_attempt),
                "task": deep_task,
            }

    counts = Counter(row["status"] for row in details)
    audit = {
        "schema_version": "1.0.0",
        "checked_at_utc": 当前世界时(),
        "expected_attempts": len(主支路) * 预期骨架数,
        "complete_attempts": sum(row["ready"] for row in details),
        "main_ready": main_ready,
        "deep_probe_ready": bool(deep_detail["ready"]),
        "ready": main_ready and bool(deep_detail["ready"]),
        "status_counts": dict(sorted(counts.items())),
        "details": details,
        "deep_probe": deep_detail,
        "not_ready_action": (
            "等待未完成主任务或独立 near-official 探针；不得生成占位候选、样本或指标，"
            "且探针永不并入48主候选。"
        ),
    }
    return audit, selected


def 解析设计区域(scaffold_yaml: Path) -> list[list[int]]:
    """严格解析三个 1-based 设计区域，并保留区域边界。"""

    payload = yaml.safe_load(scaffold_yaml.read_text(encoding="utf-8"))
    designs = payload.get("design", [])
    if not isinstance(designs, list) or len(designs) != 1:
        raise ValueError(f"每个骨架必须恰有一个 design 链：{scaffold_yaml}")
    try:
        text = str(designs[0]["chain"]["res_index"])
    except (KeyError, TypeError) as error:
        raise ValueError(f"缺少 design.chain.res_index：{scaffold_yaml}") from error
    tokens = [token.strip() for token in text.split(",")]
    if len(tokens) != 3:
        raise ValueError(f"设计掩码必须恰有三个区域：{scaffold_yaml}")
    regions: list[list[int]] = []
    for token in tokens:
        match = re.fullmatch(r"([1-9][0-9]*)(?:\.\.([1-9][0-9]*))?", token)
        if match is None:
            raise ValueError(f"非法设计区域 {token!r}：{scaffold_yaml}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            raise ValueError(f"设计区域反向：{token!r}")
        regions.append(list(range(start, end + 1)))
    if any(regions[index][-1] >= regions[index + 1][0] for index in range(2)):
        raise ValueError(f"三个设计区域必须按链顺序且不重叠：{scaffold_yaml}")
    return regions


def 链序列(chain: gemmi.Chain) -> str:
    """把 Gemmi 蛋白链转换为标准单字母序列。"""

    letters = []
    for residue in chain:
        info = gemmi.find_tabulated_residue(residue.name)
        letter = info.one_letter_code if info is not None else "X"
        letters.append(letter if letter else "X")
    return "".join(letters)


def 唯一蛋白链(path: Path) -> gemmi.Chain:
    """读取仅含一条蛋白链的骨架 CIF。"""

    structure = gemmi.read_structure(str(path))
    if len(structure) != 1 or len(structure[0]) != 1:
        raise ValueError(f"骨架 CIF 必须恰有一个模型和一条链：{path}")
    return structure[0][0]


def 识别目标与VHH(model: gemmi.Model) -> tuple[gemmi.Chain, gemmi.Chain]:
    """按 30 残基目标序列识别复折叠输出中的目标链和 VHH 链。"""

    chains = list(model)
    targets = [chain for chain in chains if 链序列(chain) == 目标序列]
    if len(targets) != 1:
        raise ValueError(f"不能唯一识别 GLP-1 链：{[链序列(chain) for chain in chains]}")
    binders = [chain for chain in chains if chain.name != targets[0].name]
    if len(binders) != 1:
        raise ValueError(f"不能唯一识别 VHH 链：{[chain.name for chain in chains]}")
    return targets[0], binders[0]


def 重原子(residue: gemmi.Residue) -> list[gemmi.Atom]:
    """返回非氢且占有率大于零的原子。"""

    return [atom for atom in residue if not atom.element.is_hydrogen and float(atom.occ) > 0]


def 最小重原子距离(residue: gemmi.Residue, partners: Iterable[gemmi.Residue]) -> float:
    """计算一个目标残基到一组 VHH 设计残基的最小重原子距离。"""

    left = 重原子(residue)
    right = [atom for partner in partners for atom in 重原子(partner)]
    if not left or not right:
        raise ValueError("重原子距离计算遇到空原子集合")
    return min(atom_a.pos.dist(atom_b.pos) for atom_a in left for atom_b in right)


def 最小碳阿尔法距离(residue: gemmi.Residue, partners: Iterable[gemmi.Residue]) -> float:
    """计算目标残基 Cα 到 VHH 设计残基 Cα 的最小距离。"""

    left = residue.find_atom("CA", "*")
    right = [partner.find_atom("CA", "*") for partner in partners]
    right = [atom for atom in right if atom]
    if not left or not right:
        raise ValueError("Cα 距离计算缺少 Cα 原子")
    return min(left.pos.dist(atom.pos) for atom in right)


def 复折叠热点距离(path: Path, design_positions: list[int]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """独立重算复折叠后 His7/Ala8 到 VHH 设计区的几何距离。"""

    structure = gemmi.read_structure(str(path))
    if len(structure) != 1:
        raise ValueError(f"复折叠 CIF 模型数不是 1：{path}")
    target, binder = 识别目标与VHH(structure[0])
    binder_by_position = {index: residue for index, residue in enumerate(binder, start=1)}
    missing = sorted(set(design_positions) - set(binder_by_position))
    if missing:
        raise ValueError(f"复折叠 VHH 缁少设计位置：{missing}")
    partners = [binder_by_position[position] for position in design_positions]
    target_by_position = {index: residue for index, residue in enumerate(target, start=1)}
    rows: list[dict[str, Any]] = []
    for position, name in ((1, "His7"), (2, "Ala8")):
        heavy = 最小重原子距离(target_by_position[position], partners)
        ca = 最小碳阿尔法距离(target_by_position[position], partners)
        rows.append(
            {
                "hotspot_label_seq_id": position,
                "hotspot_biological_name": name,
                "min_heavy_atom_distance_a": round(heavy, 6),
                "min_ca_distance_a": round(ca, 6),
                "heavy_atom_covered_lt8a": heavy < 重原子距离阈值埃,
                "ca_covered_lt8a": ca < 重原子距离阈值埃,
            }
        )
    summary = {
        "refold_target_chain": target.name,
        "refold_vhh_chain": binder.name,
        "target_sequence": 链序列(target),
        "vhh_sequence": 链序列(binder),
        "vhh_residue_count": len(binder),
        "design_residue_count": len(design_positions),
        "refold_hotspot_coverage_heavy_fraction_lt8a": sum(
            row["heavy_atom_covered_lt8a"] for row in rows
        )
        / 2.0,
        "refold_hotspot_coverage_ca_fraction_lt8a": sum(row["ca_covered_lt8a"] for row in rows)
        / 2.0,
    }
    return rows, summary


def 读取候选表(path: Path, label: str) -> pd.DataFrame:
    """读取非空候选 CSV，并拒绝空 ID 或重复 ID。"""

    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"缺少或为空的{label}：{path}")
    frame = pd.read_csv(path)
    if "id" not in frame.columns or frame.empty:
        raise ValueError(f"{label}缺少 id 或没有数据行：{path}")
    ids = frame["id"].astype(str).str.strip()
    if (ids == "").any() or ids.duplicated().any():
        raise ValueError(f"{label}包含空 ID 或重复 ID：{path}")
    frame["id"] = ids
    return frame


def 验证候选谱系(
    pipeline: Path,
    task_name: str,
    expected_designs: int = 每骨架候选数,
    budget: int = 1,
) -> tuple[set[str], set[str]]:
    """验证从设计到复折叠、分析、排序与预算目录的候选集合。"""

    expected = {f"{task_name}_{index}" for index in range(expected_designs)}
    design = pipeline / "intermediate_designs"
    inverse = pipeline / "intermediate_designs_inverse_folded"
    observed = {
        "design_cif": {path.stem for path in design.glob("*.cif")},
        "design_npz": {path.stem for path in design.glob("*.npz")},
        "inverse_cif": {path.stem for path in inverse.glob("*.cif")},
        "inverse_npz": {path.stem for path in inverse.glob("*.npz")},
        "fold_npz": {path.stem for path in (inverse / "fold_out_npz").glob("*.npz")},
        "refold_cif": {path.stem for path in (inverse / "refold_cif").glob("*.cif")},
    }
    analyzed = 读取候选表(inverse / "aggregate_metrics_analyze.csv", "分析聚合表")
    observed["analyzed_csv"] = set(analyzed["id"])
    for stage, ids in observed.items():
        if ids != expected:
            raise ValueError(f"{task_name} 的 {stage} 候选集合错误：{sorted(ids)}")

    final = pipeline / "final_ranked_designs"
    ranked = 读取候选表(final / "all_designs_metrics.csv", "全候选过滤表")
    ranked_ids = set(ranked["id"])
    if ranked_ids != expected:
        raise ValueError(f"{task_name} 的过滤表没有保留两个候选：{sorted(ranked_ids)}")
    budget_frame = 读取候选表(
        final / f"final_designs_metrics_{budget}.csv", f"预算 {budget} 指标表"
    )
    budget_ids = set(budget_frame["id"])
    if len(budget_ids) != budget or not budget_ids.issubset(expected):
        raise ValueError(
            f"{task_name} 的预算目录不是原始候选中的 {budget} 项：{sorted(budget_ids)}"
        )
    budget_cifs = sorted((final / f"final_{budget}_designs").glob("*.cif"))
    expected_names = {
        f"rank{rank}_{candidate_id}.cif"
        for rank, candidate_id in enumerate(
            budget_frame.sort_values("final_rank")["id"].astype(str), start=1
        )
    }
    if len(budget_cifs) != budget or {path.name for path in budget_cifs} != expected_names:
        raise ValueError(f"{task_name} 的预算 CIF 合同错误：{[path.name for path in budget_cifs]}")
    return expected, budget_ids


def 复折叠CIF坐标(path: Path) -> np.ndarray:
    """按 CIF 原子行顺序提取所有已解析原子坐标。"""

    structure = gemmi.read_structure(str(path))
    if len(structure) != 1:
        raise ValueError(f"CIF 模型数不是 1：{path}")
    coords = [
        (atom.pos.x, atom.pos.y, atom.pos.z)
        for chain in structure[0]
        for residue in chain
        for atom in residue
        if float(atom.occ) > 0
    ]
    return np.asarray(coords, dtype=np.float64)


def 检查复折叠NPZ(
    path: Path,
    refold_cif: Path,
    analysis_row: dict[str, Any],
    expected_samples: int = 每候选样本数,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """验证 2 样本轴、最佳样本公式、分析行与 writer CIF 坐标。"""

    required = set(样本标量数组) | {
        "coords",
        "atom_resolved_mask",
        "atom_to_token",
        "token_index",
        "input_coords",
    }
    with np.load(path, allow_pickle=False) as arrays:
        missing = sorted(required - set(arrays.files))
        if missing:
            raise ValueError(f"fold NPZ 缺少数组 {missing}：{path}")
        coords = np.asarray(arrays["coords"])
        if coords.ndim != 3 or coords.shape[0] != expected_samples or coords.shape[2] != 3:
            raise ValueError(
                f"coords 形状必须是 ({expected_samples}, 原子槽, 3)：{coords.shape}，{path}"
            )
        if not np.isfinite(coords).all():
            raise ValueError(f"coords 含 NaN/Inf：{path}")
        atom_slots = int(coords.shape[1])

        # token_index 必须是单批次、0..T-1 连续索引；它定义 token 轴的唯一长度。
        token_index = np.asarray(arrays["token_index"])
        if token_index.ndim != 2 or token_index.shape[0] != 1 or token_index.shape[1] == 0:
            raise ValueError(f"token_index 形状必须是 (1, token数)：{token_index.shape}，{path}")
        if not np.issubdtype(token_index.dtype, np.integer):
            raise ValueError(f"token_index 必须是整数：{token_index.dtype}，{path}")
        token_count = int(token_index.shape[1])
        if not np.array_equal(token_index[0], np.arange(token_count, dtype=token_index.dtype)):
            raise ValueError(f"token_index 不是从 0 开始的连续索引：{path}")

        # atom_to_token 是 [批次=1, 原子槽, token] 的布尔 one-hot/空行映射。
        atom_to_token = np.asarray(arrays["atom_to_token"])
        if atom_to_token.shape != (1, atom_slots, token_count) or atom_to_token.dtype != np.bool_:
            raise ValueError(
                f"atom_to_token 必须是 bool (1,{atom_slots},{token_count})："
                f"{atom_to_token.shape}/{atom_to_token.dtype}，{path}"
            )
        mapping_counts = atom_to_token[0].sum(axis=1)
        if not np.isin(mapping_counts, [0, 1]).all():
            raise ValueError(f"atom_to_token 每个原子槽只能映射 0 或 1 个 token：{path}")
        if (atom_to_token[0].sum(axis=0) == 0).any():
            raise ValueError(f"atom_to_token 存在没有任何原子槽的 token：{path}")

        # resolved mask 与原子槽共轴；已解析原子必须拥有唯一 token 映射。
        resolved = np.asarray(arrays["atom_resolved_mask"])
        if resolved.shape != (1, atom_slots) or resolved.dtype != np.bool_:
            raise ValueError(
                f"atom_resolved_mask 必须是 bool (1,{atom_slots})："
                f"{resolved.shape}/{resolved.dtype}，{path}"
            )
        if not (mapping_counts[resolved[0]] == 1).all():
            raise ValueError(f"存在已解析但未唯一映射 token 的原子槽：{path}")

        # input_coords 是单批次、单输入构象，与预测 coords 共享原子槽和 xyz 轴。
        input_coords = np.asarray(arrays["input_coords"])
        if input_coords.shape != (1, 1, atom_slots, 3) or not np.isfinite(input_coords).all():
            raise ValueError(
                f"input_coords 必须是有限 (1,1,{atom_slots},3)：{input_coords.shape}，{path}"
            )
        score_arrays: dict[str, np.ndarray] = {}
        for key in 样本标量数组:
            array = np.asarray(arrays[key])
            if array.shape != (expected_samples,) or not np.isfinite(array).all():
                raise ValueError(
                    f"{key} 必须是有限 ({expected_samples},) 数组：{array.shape}，{path}"
                )
            score_arrays[key] = array.astype(np.float64)

        analysis_score = 0.8 * score_arrays["design_to_target_iptm"] + 0.2 * score_arrays["design_ptm"]
        writer_score = 0.8 * score_arrays["iptm"] + 0.2 * score_arrays["ptm"]
        analysis_best = int(np.argmax(analysis_score))
        writer_best = int(np.argmax(writer_score))

        # Analyze 使用 design-to-target 指标选样本；CSV 必须等于这个样本而非 writer 样本。
        matched_columns = []
        for key in 样本标量数组:
            if key not in analysis_row or pd.isna(analysis_row[key]):
                continue
            observed = 有限浮点(analysis_row[key], f"analysis CSV {key}")
            expected = float(score_arrays[key][analysis_best])
            if not math.isclose(observed, expected, abs_tol=5.1e-5, rel_tol=1e-5):
                raise ValueError(
                    f"分析 CSV 的 {key} 未对应 analysis best 样本：{observed} != {expected}，{path}"
                )
            matched_columns.append(key)

        # writer 使用全复合物 0.8*iPTM+0.2*pTM 选坐标；独立比对 CIF 中的原子坐标。
        expected_cif_coords = coords[writer_best][resolved[0].astype(bool)]
        observed_cif_coords = 复折叠CIF坐标(refold_cif)
        if observed_cif_coords.shape != expected_cif_coords.shape:
            raise ValueError(
                f"writer CIF 原子数与 resolved mask 不一致：{observed_cif_coords.shape} != "
                f"{expected_cif_coords.shape}，{path}"
            )
        max_error = float(np.max(np.abs(observed_cif_coords - expected_cif_coords)))
        if max_error > 5e-4:
            raise ValueError(f"writer CIF 坐标未对应 writer best 样本；最大误差 {max_error} Å：{path}")

        sample_rows = []
        for sample_index in range(expected_samples):
            sample_rows.append(
                {
                    "sample_index": sample_index,
                    "analysis_selection_score": float(analysis_score[sample_index]),
                    "writer_selection_score": float(writer_score[sample_index]),
                    "selected_by_analysis": sample_index == analysis_best,
                    "selected_by_writer": sample_index == writer_best,
                    **{key: float(values[sample_index]) for key, values in score_arrays.items()},
                }
            )
        schema = {
            "path": 相对路径(path),
            "keys": list(arrays.files),
            "fields": [
                {"name": key, "shape": list(np.asarray(arrays[key]).shape), "dtype": str(np.asarray(arrays[key]).dtype)}
                for key in arrays.files
            ],
        }

    contract = {
        "fold_sample_count": expected_samples,
        "fold_atom_slot_count": atom_slots,
        "fold_token_count": token_count,
        "mapped_atom_slot_count": int((mapping_counts == 1).sum()),
        "unmapped_atom_slot_count": int((mapping_counts == 0).sum()),
        "resolved_atom_slot_count": int(resolved.sum()),
        "analysis_best_sample_index": analysis_best,
        "writer_best_sample_index": writer_best,
        "same_best_sample": analysis_best == writer_best,
        "writer_cif_max_abs_coordinate_error_a": max_error,
        "analysis_csv_matched_sample_columns": len(matched_columns),
    }
    return contract, sample_rows, schema


def 检查阶段合同(
    profile: str,
    checkpoint: str,
    scaffold: dict[str, Any],
    attempt: Path,
    status: dict[str, Any],
    runtime_hashes: dict[str, str],
    expected_designs: int = 每骨架候选数,
    expected_samples: int = 每候选样本数,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """验证七个阶段状态、检查点哈希与资源采样，并返回明细表。"""

    if status.get("profile") != profile or status.get("candidate_id") != scaffold["candidate_id"]:
        raise ValueError(f"run_status 与任务身份不一致：{attempt}")
    if status.get("completed_pipeline_stages") != list(预期模型阶段):
        raise ValueError(f"模型阶段列表不完整：{attempt}")
    contracts = status.get("output_contracts", [])
    if len(contracts) != len(预期模型阶段) or any(item.get("status") != "PASS" for item in contracts):
        raise ValueError(f"run_status 输出合同未全部通过：{attempt}")
    if [item.get("stage") for item in contracts] != list(预期模型阶段):
        raise ValueError(f"run_status 输出合同阶段顺序错误：{attempt}")
    if any(int(item.get("expected_designs", -1)) != expected_designs for item in contracts):
        raise ValueError(f"run_status 输出合同候选数不是 {expected_designs}：{attempt}")
    folding_contract = next(item for item in contracts if item.get("stage") == "folding")
    if folding_contract.get("fold_sample_counts") != [expected_samples] * expected_designs:
        raise ValueError(
            f"run_status 折叠样本合同不是 {[expected_samples] * expected_designs}：{attempt}"
        )
    resolved_contract = status.get("resolved_config_contract", {})
    if resolved_contract.get("status") != "PASS" or not all(
        bool(value) for value in resolved_contract.get("checks", {}).values()
    ):
        raise ValueError(f"冻结配置解析合同未全部通过：{attempt}")

    stage_by_name = {row.get("stage"): row for row in status.get("stage_records", [])}
    if set(stage_by_name) != set(预期阶段):
        raise ValueError(f"阶段记录集合错误：{sorted(stage_by_name)}，{attempt}")

    switch = 读取JSON(attempt / "stage_status" / "01_design_checkpoint_switch_contract.json")
    if not (
        switch.get("status") == "PASS"
        and int(switch.get("expected_switch_count", -1)) == 0
        and int(switch.get("observed_switch_count", -1)) == 0
        and switch.get("design_checkpoints") == [checkpoint]
    ):
        raise ValueError(f"单检查点切换合同失败：{attempt}")

    stage_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    resource_summary_rows: list[dict[str, Any]] = []
    for stage_name in 预期阶段:
        row = stage_by_name[stage_name]
        if not (
            row.get("status") == "COMPLETE"
            and int(row.get("return_code", -999)) == 0
            and row.get("contract_status") == "PASS"
            and row.get("monitor_status") == "COMPLETE"
            and int(row.get("monitor_return_code", -999)) == 0
        ):
            raise ValueError(f"阶段状态/输出/监控合同失败：{stage_name}，{attempt}")
        pre = row.get("checkpoint_pre_sha256", [])
        post = row.get("checkpoint_post_sha256", [])
        if pre != post:
            raise ValueError(f"检查点在阶段前后哈希变化：{stage_name}，{attempt}")

        # 只有三个模型阶段读取权重；其余阶段不得伪称使用了检查点。
        expected_asset = {
            "01_design": checkpoint,
            "02_inverse_folding": "inverse_fold",
            "03_folding": "folding",
        }.get(stage_name)
        if expected_asset is None and pre:
            raise ValueError(f"非权重阶段意外记录检查点：{stage_name}，{attempt}")
        if expected_asset is not None:
            if len(pre) != 1 or pre[0].get("asset") != expected_asset:
                raise ValueError(f"阶段检查点身份错误：{stage_name}，{attempt}")
            if expected_asset not in runtime_hashes or pre[0].get("sha256") != runtime_hashes[expected_asset]:
                raise ValueError(f"阶段检查点哈希与冻结 provenance 不一致：{stage_name}，{attempt}")

        base = {
            "profile": profile,
            "checkpoint": checkpoint,
            "selection_rank": int(scaffold["selection_rank"]),
            "scaffold_id": scaffold["candidate_id"],
            "pdb_code": scaffold["pdb_code"],
            "attempt": 相对路径(attempt),
            "stage": stage_name,
        }
        stage_rows.append(
            {
                **base,
                "elapsed_seconds": 有限浮点(row["elapsed_seconds"], "stage elapsed_seconds"),
                "return_code": int(row["return_code"]),
                "contract_status": row["contract_status"],
                "monitor_status": row["monitor_status"],
                "monitor_sample_count": int(row["monitor_sample_count"]),
                "checkpoint_assets": ";".join(item["asset"] for item in pre),
                "checkpoint_sha256": ";".join(item["sha256"] for item in pre),
                "started_at_utc": row.get("started_at_utc"),
                "finished_at_utc": row.get("finished_at_utc"),
                "resource_csv": row.get("resource_csv"),
                "stdout_log": f"{相对路径(attempt)}/logs/{stage_name}/stdout.log",
                "stderr_log": f"{相对路径(attempt)}/logs/{stage_name}/stderr.log",
            }
        )

        resource_path = 项目根目录 / str(row["resource_csv"])
        if not resource_path.is_file():
            raise FileNotFoundError(f"缺少资源 CSV：{resource_path}")
        resource = pd.read_csv(resource_path)
        if len(resource) != int(row["monitor_sample_count"]):
            raise ValueError(f"资源采样行数与监控合同不一致：{resource_path}")
        required_resource = {
            "sample_index",
            "sampled_at_utc",
            "elapsed_seconds",
            "cpu_percent_sum",
            "rss_gib_sum",
            "system_free_bytes",
            "system_active_bytes",
            "swap_used_bytes",
            "disk_free_bytes",
        }
        if not required_resource.issubset(resource.columns) or resource.empty:
            raise ValueError(f"资源 CSV 字段不全或为空：{resource_path}")
        for record in resource.to_dict("records"):
            resource_rows.append({**base, **record, "resource_source": 相对路径(resource_path)})
        first_swap = 有限浮点(resource.iloc[0]["swap_used_bytes"], "first swap")
        last_swap = 有限浮点(resource.iloc[-1]["swap_used_bytes"], "last swap")
        resource_summary_rows.append(
            {
                **base,
                "sample_count": len(resource),
                "sampled_duration_seconds": float(resource["elapsed_seconds"].max()),
                "peak_process_tree_rss_gib": float(resource["rss_gib_sum"].max()),
                "peak_process_tree_cpu_percent_sum": float(resource["cpu_percent_sum"].max()),
                "minimum_system_free_gib": float(resource["system_free_bytes"].min()) / 吉字节,
                "peak_system_active_gib": float(resource["system_active_bytes"].max()) / 吉字节,
                "swap_first_gib": first_swap / 吉字节,
                "swap_last_gib": last_swap / 吉字节,
                "swap_stage_delta_gib": (last_swap - first_swap) / 吉字节,
                "swap_stage_range_gib": (
                    float(resource["swap_used_bytes"].max()) - float(resource["swap_used_bytes"].min())
                )
                / 吉字节,
                "minimum_disk_free_gib": float(resource["disk_free_bytes"].min()) / 吉字节,
                "mps_process_memory_measured": False,
            }
        )
    return stage_rows, resource_rows, resource_summary_rows


def 输入与运行保护快照(selected: Iterable[tuple[str, dict[str, Any], Path, dict[str, Any]]]) -> dict[str, str]:
    """哈希所有授权源目录中的文件，用于确认分析没有写回源数据。"""

    paths = []
    for root in (输入目录, 溯源目录):
        paths.extend(path for path in root.rglob("*") if path.is_file())
    for _, _, attempt, _ in selected:
        paths.extend(path for path in attempt.rglob("*") if path.is_file())
    unique = sorted({path.resolve() for path in paths if "__pycache__" not in path.parts and path.suffix != ".pyc"})
    return {相对路径(path): 文件哈希(path) for path in unique}


def 构建主分析(
    manifest: dict[str, Any],
    selected: list[tuple[str, dict[str, Any], Path, dict[str, Any]]],
) -> dict[str, Any]:
    """在内存中构建全部明细表；任何错误发生时不写派生文件。"""

    candidates: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    lineages: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    resource_summary_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    npz_schemas: list[dict[str, Any]] = []
    runtime_hashes = {
        str(record["asset"]): str(record["sha256"])
        for record in manifest.get("runtime", {}).get("assets", [])
    }
    required_assets = {"design_diverse", "design_adherence", "inverse_fold", "folding"}
    if not required_assets.issubset(runtime_hashes):
        raise ValueError(f"冻结 runtime 清单缺少检查点：{sorted(required_assets - set(runtime_hashes))}")

    for profile, scaffold, attempt, status in selected:
        rank = int(scaffold["selection_rank"])
        task_name = f"{rank:02d}_{scaffold['candidate_id']}"
        if status.get("profile_contract") != manifest["profiles"][profile]:
            raise ValueError(f"run_status 的 profile_contract 与冻结 provenance 不一致：{attempt}")
        expected_ids, budget_ids = 验证候选谱系(attempt / "pipeline", task_name)
        stages, resources, resource_summaries = 检查阶段合同(
            profile, 支路检查点[profile], scaffold, attempt, status, runtime_hashes
        )
        stage_rows.extend(stages)
        resource_rows.extend(resources)
        resource_summary_rows.extend(resource_summaries)

        scaffold_package = 项目根目录 / scaffold["input_package"]
        design_regions = 解析设计区域(scaffold_package / "scaffold.yaml")
        design_positions = [position for region in design_regions for position in region]
        design_position_set = set(design_positions)
        input_sequence = 链序列(唯一蛋白链(scaffold_package / "scaffold.cif"))
        input_framework = "".join(
            residue for index, residue in enumerate(input_sequence, start=1) if index not in design_position_set
        )

        pipeline = attempt / "pipeline"
        inverse = pipeline / "intermediate_designs_inverse_folded"
        analysis_frame = 读取候选表(inverse / "aggregate_metrics_analyze.csv", "分析聚合表").set_index("id")
        filter_frame = 读取候选表(
            pipeline / "final_ranked_designs" / "all_designs_metrics.csv", "过滤结果表"
        ).set_index("id")
        if set(analysis_frame.index) != expected_ids or set(filter_frame.index) != expected_ids:
            raise ValueError(f"候选表集合在验证后发生变化：{attempt}")

        run_rows.append(
            {
                "profile": profile,
                "checkpoint": 支路检查点[profile],
                "selection_rank": rank,
                "scaffold_id": scaffold["candidate_id"],
                "pdb_code": scaffold["pdb_code"],
                "role": scaffold["role"],
                "attempt": 相对路径(attempt),
                "launch_id": status["launch_id"],
                "status": status["status"],
                "elapsed_seconds": 有限浮点(status["elapsed_seconds"], "run elapsed_seconds"),
                "requested_candidates": 每骨架候选数,
                "folding_samples_per_candidate": 每候选样本数,
                "budget": int(status["profile_contract"]["budget"]),
                "budget_semantics": "仅展示排序候选，不等于通过全部过滤",
            }
        )

        for original_id in sorted(expected_ids):
            global_id = f"{支路检查点[profile]}::{original_id}"
            analysis_row = analysis_frame.loc[original_id].to_dict()
            filter_row = filter_frame.loc[original_id].to_dict()
            full_sequence = str(filter_row["designed_chain_sequence"])
            design_sequence = str(filter_row["designed_sequence"])
            if len(full_sequence) != len(input_sequence):
                raise ValueError(f"{global_id} 完整链长度与骨架不同")
            cdr_sequences = [
                "".join(full_sequence[position - 1] for position in region) for region in design_regions
            ]
            if design_sequence != "".join(cdr_sequences):
                raise ValueError(f"{global_id} 的 designed_sequence 不是三个设计区域的顺序拼接")
            framework = "".join(
                residue for index, residue in enumerate(full_sequence, start=1) if index not in design_position_set
            )
            if framework != input_framework:
                raise ValueError(f"{global_id} 的固定框架在设计掩码外发生变化")

            fold_npz = inverse / "fold_out_npz" / f"{original_id}.npz"
            refold_cif = inverse / "refold_cif" / f"{original_id}.cif"
            fold_contract, sample_detail, npz_schema = 检查复折叠NPZ(fold_npz, refold_cif, analysis_row)
            npz_schema["candidate_id"] = global_id
            npz_schemas.append(npz_schema)
            for detail in sample_detail:
                samples.append(
                    {
                        "candidate_id": global_id,
                        "original_candidate_id": original_id,
                        "profile": profile,
                        "checkpoint": 支路检查点[profile],
                        "selection_rank": rank,
                        "scaffold_id": scaffold["candidate_id"],
                        "pdb_code": scaffold["pdb_code"],
                        **detail,
                        "source_fold_npz": 相对路径(fold_npz),
                    }
                )

            contact_detail, contact_summary = 复折叠热点距离(refold_cif, design_positions)
            if contact_summary["vhh_sequence"] != full_sequence:
                raise ValueError(f"{global_id} 的复折叠 VHH 序列与候选完整链不一致")
            for contact in contact_detail:
                contacts.append(
                    {
                        "candidate_id": global_id,
                        "original_candidate_id": original_id,
                        "profile": profile,
                        "checkpoint": 支路检查点[profile],
                        "selection_rank": rank,
                        "scaffold_id": scaffold["candidate_id"],
                        "pdb_code": scaffold["pdb_code"],
                        **contact,
                        "source_refold_cif": 相对路径(refold_cif),
                        "source_refold_cif_sha256": 文件哈希(refold_cif),
                    }
                )

            prerefold_coverage = 有限浮点(filter_row["bindsite_under_8rmsd"], "bindsite_under_8rmsd")
            if prerefold_coverage not in {0.0, 0.5, 1.0}:
                raise ValueError(f"{global_id} 的提示位点覆盖不是 0/0.5/1：{prerefold_coverage}")

            pass_values: dict[str, bool] = {}
            failed_labels = []
            for order, pass_col, value_col, label, operator, threshold, unit in 过滤定义:
                value = 有限浮点(filter_row[value_col], f"{global_id} {value_col}")
                reported = 布尔值(filter_row[pass_col], f"{global_id} {pass_col}")
                recomputed = 阈值通过(value, operator, threshold)
                if reported != recomputed:
                    raise ValueError(f"{global_id} 的 {pass_col} 与值/阈值不一致")
                pass_values[pass_col] = reported
                if not reported:
                    failed_labels.append(label)
                filters.append(
                    {
                        "candidate_id": global_id,
                        "original_candidate_id": original_id,
                        "profile": profile,
                        "checkpoint": 支路检查点[profile],
                        "selection_rank": rank,
                        "scaffold_id": scaffold["candidate_id"],
                        "pdb_code": scaffold["pdb_code"],
                        "filter_order": order,
                        "filter_label_cn": label,
                        "pass_column": pass_col,
                        "value_column": value_col,
                        "observed_value": value,
                        "operator": operator,
                        "threshold": threshold,
                        "unit": unit,
                        "passed": reported,
                    }
                )
            pass_all = all(pass_values.values())
            if pass_all != 布尔值(filter_row["pass_filters"], f"{global_id} pass_filters"):
                raise ValueError(f"{global_id} 的逐项过滤与 pass_filters 不一致")

            candidates.append(
                {
                    "candidate_id": global_id,
                    "original_candidate_id": original_id,
                    "local_candidate_index": int(original_id.rsplit("_", 1)[-1]),
                    "profile": profile,
                    "checkpoint": 支路检查点[profile],
                    "checkpoint_label_cn": 支路中文名[profile],
                    "selection_rank": rank,
                    "scaffold_id": scaffold["candidate_id"],
                    "pdb_code": scaffold["pdb_code"],
                    "scaffold_role": scaffold["role"],
                    "scaffold_resolution_a": scaffold["resolution_a"],
                    "scaffold_r_free": scaffold["r_free"],
                    "design_residue_count": len(design_positions),
                    "cdr1_sequence": cdr_sequences[0],
                    "cdr2_sequence": cdr_sequences[1],
                    "cdr3_sequence": cdr_sequences[2],
                    "designed_sequence": design_sequence,
                    "designed_chain_sequence": full_sequence,
                    "framework_sequence_unchanged": True,
                    "analysis_best_sample_index": fold_contract["analysis_best_sample_index"],
                    "writer_best_sample_index": fold_contract["writer_best_sample_index"],
                    "same_best_sample": fold_contract["same_best_sample"],
                    "writer_cif_max_abs_coordinate_error_a": fold_contract[
                        "writer_cif_max_abs_coordinate_error_a"
                    ],
                    "design_to_target_iptm": 有限浮点(filter_row["design_to_target_iptm"], "design_to_target_iptm"),
                    "design_ptm": 有限浮点(filter_row["design_ptm"], "design_ptm"),
                    "iptm": 有限浮点(filter_row["iptm"], "iptm"),
                    "ptm": 有限浮点(filter_row["ptm"], "ptm"),
                    "min_design_to_target_pae_a": 有限浮点(
                        analysis_row["min_design_to_target_pae"], "min_design_to_target_pae"
                    ),
                    "filter_rmsd_a": 有限浮点(filter_row["filter_rmsd"], "filter_rmsd"),
                    "filter_rmsd_design_a": 有限浮点(
                        filter_row["filter_rmsd_design"], "filter_rmsd_design"
                    ),
                    "prerefold_hotspot_coverage_fraction_lt8a": prerefold_coverage,
                    "refold_hotspot_coverage_heavy_fraction_lt8a": contact_summary[
                        "refold_hotspot_coverage_heavy_fraction_lt8a"
                    ],
                    "refold_hotspot_coverage_ca_fraction_lt8a": contact_summary[
                        "refold_hotspot_coverage_ca_fraction_lt8a"
                    ],
                    "his7_min_heavy_atom_distance_a": contact_detail[0]["min_heavy_atom_distance_a"],
                    "ala8_min_heavy_atom_distance_a": contact_detail[1]["min_heavy_atom_distance_a"],
                    "his7_min_ca_distance_a": contact_detail[0]["min_ca_distance_a"],
                    "ala8_min_ca_distance_a": contact_detail[1]["min_ca_distance_a"],
                    "delta_sasa_refolded_a2": 有限浮点(filter_row["delta_sasa_refolded"], "delta_sasa_refolded"),
                    "plip_hbonds_refolded": 有限浮点(filter_row["plip_hbonds_refolded"], "plip_hbonds_refolded"),
                    "plip_saltbridge_refolded": 有限浮点(
                        filter_row["plip_saltbridge_refolded"], "plip_saltbridge_refolded"
                    ),
                    "liability_score": 有限浮点(filter_row["liability_score"], "liability_score"),
                    "liability_num_violations": 有限浮点(
                        filter_row["liability_num_violations"], "liability_num_violations"
                    ),
                    "computed_filter_pass_count": sum(pass_values.values()),
                    "computed_filter_total": len(pass_values),
                    "pass_all_default_filters": pass_all,
                    "failed_filter_count": len(failed_labels),
                    "failed_filters_cn": "；".join(failed_labels),
                    "boltzgen_internal_prefix_pass_score": 有限浮点(
                        filter_row["num_filters_passed"], "num_filters_passed"
                    ),
                    "selected_by_budget": original_id in budget_ids,
                    "budget_semantics": "展示/排序项；与 pass_all_default_filters 是独立字段",
                    "source_attempt": 相对路径(attempt),
                    "source_metrics_csv": 相对路径(
                        pipeline / "final_ranked_designs" / "all_designs_metrics.csv"
                    ),
                    "source_fold_npz": 相对路径(fold_npz),
                    "source_refold_cif": 相对路径(refold_cif),
                }
            )

            artifact_map = {
                "design_cif": pipeline / "intermediate_designs" / f"{original_id}.cif",
                "design_npz": pipeline / "intermediate_designs" / f"{original_id}.npz",
                "inverse_fold_cif": inverse / f"{original_id}.cif",
                "inverse_fold_npz": inverse / f"{original_id}.npz",
                "fold_npz": fold_npz,
                "refold_cif": refold_cif,
                "analysis_metrics_csv": inverse / "aggregate_metrics_analyze.csv",
                "filter_metrics_csv": pipeline / "final_ranked_designs" / "all_designs_metrics.csv",
            }
            for role, path in artifact_map.items():
                if not path.is_file() or path.stat().st_size == 0:
                    raise FileNotFoundError(f"{global_id} 缺少谱系产物 {role}：{path}")
                lineages.append(
                    {
                        "candidate_id": global_id,
                        "original_candidate_id": original_id,
                        "profile": profile,
                        "checkpoint": 支路检查点[profile],
                        "selection_rank": rank,
                        "scaffold_id": scaffold["candidate_id"],
                        "artifact_role": role,
                        "path": 相对路径(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": 文件哈希(path),
                    }
                )

    frames = {
        "candidates": pd.DataFrame(candidates).sort_values(["selection_rank", "profile", "local_candidate_index"]),
        "samples": pd.DataFrame(samples).sort_values(["selection_rank", "profile", "original_candidate_id", "sample_index"]),
        "contacts": pd.DataFrame(contacts).sort_values(["candidate_id", "hotspot_label_seq_id"]),
        "filters": pd.DataFrame(filters).sort_values(["candidate_id", "filter_order"]),
        "lineages": pd.DataFrame(lineages).sort_values(["candidate_id", "artifact_role"]),
        "stages": pd.DataFrame(stage_rows).sort_values(["selection_rank", "profile", "stage"]),
        "resources": pd.DataFrame(resource_rows).sort_values(
            ["selection_rank", "profile", "stage", "sample_index"]
        ),
        "resource_summary": pd.DataFrame(resource_summary_rows).sort_values(
            ["selection_rank", "profile", "stage"]
        ),
        "runs": pd.DataFrame(run_rows).sort_values(["selection_rank", "profile"]),
    }
    return {"frames": frames, "npz_schemas": npz_schemas}


def 构建独立深度探针(
    manifest: dict[str, Any],
    readiness: dict[str, Any],
) -> dict[str, Any]:
    """严格汇总 near-official 7XL0 探针，且保持独立主键与独立分母。"""

    detail = readiness.get("deep_probe", {})
    if not detail.get("required_by_current_provenance"):
        return {"status": "NOT_DECLARED", "frames": {}, "npz_schemas": []}
    if not detail.get("ready"):
        raise 数据尚未就绪("near_official_adherence_7xl0 尚未 PIPELINE_COMPLETE")

    profile = 深度探针支路
    checkpoint = "design_adherence"
    attempt = 项目根目录 / str(detail["latest_attempt"])
    status = 读取JSON(attempt / "run_status.json")
    contract = manifest["profiles"][profile]
    if status.get("profile_contract") != contract:
        raise ValueError("深度探针 run_status 合同与当前 provenance 不一致")
    scaffold = next(
        record for record in manifest["scaffold_population"]["records"] if int(record["selection_rank"]) == 1
    )
    task_name = f"01_{scaffold['candidate_id']}"
    expected_designs = int(contract["num_designs"])
    expected_samples = int(contract["folding_diffusion_samples"])
    budget = int(contract["budget"])
    expected_ids, budget_ids = 验证候选谱系(
        attempt / "pipeline", task_name, expected_designs=expected_designs, budget=budget
    )
    runtime_hashes = {
        str(record["asset"]): str(record["sha256"])
        for record in manifest["runtime"]["assets"]
    }
    stages, resources, resource_summaries = 检查阶段合同(
        profile,
        checkpoint,
        scaffold,
        attempt,
        status,
        runtime_hashes,
        expected_designs=expected_designs,
        expected_samples=expected_samples,
    )

    scaffold_package = 项目根目录 / scaffold["input_package"]
    design_regions = 解析设计区域(scaffold_package / "scaffold.yaml")
    design_positions = [position for region in design_regions for position in region]
    design_position_set = set(design_positions)
    input_sequence = 链序列(唯一蛋白链(scaffold_package / "scaffold.cif"))
    input_framework = "".join(
        residue for index, residue in enumerate(input_sequence, start=1) if index not in design_position_set
    )
    pipeline = attempt / "pipeline"
    inverse = pipeline / "intermediate_designs_inverse_folded"
    analysis_frame = 读取候选表(
        inverse / "aggregate_metrics_analyze.csv", "深度探针分析聚合表"
    ).set_index("id")
    filter_frame = 读取候选表(
        pipeline / "final_ranked_designs" / "all_designs_metrics.csv", "深度探针过滤表"
    ).set_index("id")
    if set(analysis_frame.index) != expected_ids or set(filter_frame.index) != expected_ids:
        raise ValueError("深度探针候选集合与 4 候选合同不一致")

    candidates: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    lineages: list[dict[str, Any]] = []
    npz_schemas: list[dict[str, Any]] = []
    for original_id in sorted(expected_ids):
        candidate_id = f"deep_probe::{original_id}"
        analysis_row = analysis_frame.loc[original_id].to_dict()
        filter_row = filter_frame.loc[original_id].to_dict()
        full_sequence = str(filter_row["designed_chain_sequence"])
        design_sequence = str(filter_row["designed_sequence"])
        cdr_sequences = [
            "".join(full_sequence[position - 1] for position in region) for region in design_regions
        ]
        if len(full_sequence) != len(input_sequence) or design_sequence != "".join(cdr_sequences):
            raise ValueError(f"{candidate_id} 的完整链或设计区序列合同失败")
        framework = "".join(
            residue for index, residue in enumerate(full_sequence, start=1) if index not in design_position_set
        )
        if framework != input_framework:
            raise ValueError(f"{candidate_id} 的固定框架发生变化")

        fold_npz = inverse / "fold_out_npz" / f"{original_id}.npz"
        refold_cif = inverse / "refold_cif" / f"{original_id}.cif"
        fold_contract, sample_detail, npz_schema = 检查复折叠NPZ(
            fold_npz, refold_cif, analysis_row, expected_samples=expected_samples
        )
        npz_schema["candidate_id"] = candidate_id
        npz_schemas.append(npz_schema)
        for sample in sample_detail:
            samples.append(
                {
                    "candidate_id": candidate_id,
                    "original_candidate_id": original_id,
                    "profile": profile,
                    "checkpoint": checkpoint,
                    **sample,
                    "source_fold_npz": 相对路径(fold_npz),
                }
            )

        contact_detail, contact_summary = 复折叠热点距离(refold_cif, design_positions)
        if contact_summary["vhh_sequence"] != full_sequence:
            raise ValueError(f"{candidate_id} 的复折叠 VHH 序列合同失败")
        for contact in contact_detail:
            contacts.append(
                {
                    "candidate_id": candidate_id,
                    "original_candidate_id": original_id,
                    "profile": profile,
                    "checkpoint": checkpoint,
                    **contact,
                    "source_refold_cif": 相对路径(refold_cif),
                    "source_refold_cif_sha256": 文件哈希(refold_cif),
                }
            )

        pass_values: dict[str, bool] = {}
        failed_labels = []
        for order, pass_col, value_col, label, operator, threshold, unit in 过滤定义:
            value = 有限浮点(filter_row[value_col], f"{candidate_id} {value_col}")
            reported = 布尔值(filter_row[pass_col], f"{candidate_id} {pass_col}")
            if reported != 阈值通过(value, operator, threshold):
                raise ValueError(f"{candidate_id} 的 {pass_col} 与冻结阈值不一致")
            pass_values[pass_col] = reported
            if not reported:
                failed_labels.append(label)
            filters.append(
                {
                    "candidate_id": candidate_id,
                    "original_candidate_id": original_id,
                    "profile": profile,
                    "checkpoint": checkpoint,
                    "filter_order": order,
                    "filter_label_cn": label,
                    "pass_column": pass_col,
                    "value_column": value_col,
                    "observed_value": value,
                    "operator": operator,
                    "threshold": threshold,
                    "unit": unit,
                    "passed": reported,
                }
            )
        pass_all = all(pass_values.values())
        if pass_all != 布尔值(filter_row["pass_filters"], f"{candidate_id} pass_filters"):
            raise ValueError(f"{candidate_id} 的逐项过滤与 pass_filters 不一致")
        prerefold = 有限浮点(filter_row["bindsite_under_8rmsd"], "bindsite_under_8rmsd")
        if prerefold not in {0.0, 0.5, 1.0}:
            raise ValueError(f"{candidate_id} 的提示位点覆盖不是 0/0.5/1")

        candidates.append(
            {
                "candidate_id": candidate_id,
                "original_candidate_id": original_id,
                "local_candidate_index": int(original_id.rsplit("_", 1)[-1]),
                "profile": profile,
                "checkpoint": checkpoint,
                "scaffold_id": scaffold["candidate_id"],
                "pdb_code": scaffold["pdb_code"],
                "designed_sequence": design_sequence,
                "designed_chain_sequence": full_sequence,
                "framework_sequence_unchanged": True,
                "analysis_best_sample_index": fold_contract["analysis_best_sample_index"],
                "writer_best_sample_index": fold_contract["writer_best_sample_index"],
                "same_best_sample": fold_contract["same_best_sample"],
                "design_to_target_iptm": 有限浮点(filter_row["design_to_target_iptm"], "design_to_target_iptm"),
                "design_ptm": 有限浮点(filter_row["design_ptm"], "design_ptm"),
                "filter_rmsd_a": 有限浮点(filter_row["filter_rmsd"], "filter_rmsd"),
                "filter_rmsd_design_a": 有限浮点(
                    filter_row["filter_rmsd_design"], "filter_rmsd_design"
                ),
                "prerefold_hotspot_coverage_fraction_lt8a": prerefold,
                "refold_hotspot_coverage_heavy_fraction_lt8a": contact_summary[
                    "refold_hotspot_coverage_heavy_fraction_lt8a"
                ],
                "his7_min_heavy_atom_distance_a": contact_detail[0]["min_heavy_atom_distance_a"],
                "ala8_min_heavy_atom_distance_a": contact_detail[1]["min_heavy_atom_distance_a"],
                "computed_filter_pass_count": sum(pass_values.values()),
                "pass_all_default_filters": pass_all,
                "failed_filters_cn": "；".join(failed_labels),
                "selected_by_budget": original_id in budget_ids,
                "budget_semantics": "独立探针排序展示；不等于通过全部过滤",
                "source_attempt": 相对路径(attempt),
                "source_fold_npz": 相对路径(fold_npz),
                "source_refold_cif": 相对路径(refold_cif),
            }
        )

        artifact_map = {
            "design_cif": pipeline / "intermediate_designs" / f"{original_id}.cif",
            "design_npz": pipeline / "intermediate_designs" / f"{original_id}.npz",
            "inverse_fold_cif": inverse / f"{original_id}.cif",
            "inverse_fold_npz": inverse / f"{original_id}.npz",
            "fold_npz": fold_npz,
            "refold_cif": refold_cif,
            "analysis_metrics_csv": inverse / "aggregate_metrics_analyze.csv",
            "filter_metrics_csv": pipeline / "final_ranked_designs" / "all_designs_metrics.csv",
        }
        for role, path in artifact_map.items():
            if not path.is_file() or path.stat().st_size == 0:
                raise FileNotFoundError(f"{candidate_id} 缺少 {role}：{path}")
            lineages.append(
                {
                    "candidate_id": candidate_id,
                    "artifact_role": role,
                    "path": 相对路径(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": 文件哈希(path),
                }
            )

    inventory_rows = []
    for path in sorted(attempt.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        inventory_rows.append(
            {
                "profile": profile,
                "attempt": 相对路径(attempt),
                "path": 相对路径(path),
                "format": path.suffix.lower().lstrip(".") or "none",
                "size_bytes": path.stat().st_size,
                "sha256": 文件哈希(path),
            }
        )

    frames = {
        "runs": pd.DataFrame(
            [
                {
                    "profile": profile,
                    "checkpoint": checkpoint,
                    "selection_rank": 1,
                    "scaffold_id": scaffold["candidate_id"],
                    "pdb_code": scaffold["pdb_code"],
                    "attempt": 相对路径(attempt),
                    "launch_id": status["launch_id"],
                    "status": status["status"],
                    "elapsed_seconds": 有限浮点(status["elapsed_seconds"], "deep elapsed_seconds"),
                    "candidate_count": expected_designs,
                    "folding_samples_per_candidate": expected_samples,
                    "scope": "independent_deep_probe_not_in_main_48",
                }
            ]
        ),
        "candidates": pd.DataFrame(candidates).sort_values("local_candidate_index"),
        "samples": pd.DataFrame(samples).sort_values(["original_candidate_id", "sample_index"]),
        "contacts": pd.DataFrame(contacts).sort_values(["candidate_id", "hotspot_label_seq_id"]),
        "filters": pd.DataFrame(filters).sort_values(["candidate_id", "filter_order"]),
        "lineages": pd.DataFrame(lineages).sort_values(["candidate_id", "artifact_role"]),
        "stages": pd.DataFrame(stages).sort_values("stage"),
        "resources": pd.DataFrame(resources).sort_values(["stage", "sample_index"]),
        "resource_summary": pd.DataFrame(resource_summaries).sort_values("stage"),
        "inventory": pd.DataFrame(inventory_rows).sort_values("path"),
    }
    checks = {
        "profile_complete": status.get("status") == "PIPELINE_COMPLETE",
        "exactly_four_candidates": len(frames["candidates"]) == 4,
        "exactly_four_single_folding_samples": len(frames["samples"]) == 4
        and (frames["samples"].groupby("candidate_id").size() == 1).all(),
        "exactly_forty_filter_rows": len(frames["filters"]) == 40,
        "exactly_eight_hotspot_rows": len(frames["contacts"]) == 8,
        "exactly_thirty_two_lineage_rows": len(frames["lineages"]) == 32,
        "exactly_seven_stages": len(frames["stages"]) == 7,
        "single_checkpoint_no_switch": True,
        "not_part_of_main_48_candidate_namespace": all(
            str(value).startswith("deep_probe::") for value in frames["candidates"]["candidate_id"]
        ),
    }
    checks = {name: bool(value) for name, value in checks.items()}
    failed = [name for name, value in checks.items() if not value]
    if failed:
        raise ValueError(f"深度探针验证失败：{failed}")
    return {
        "status": "COMPLETE",
        "attempt": 相对路径(attempt),
        "profile_contract": contract,
        "frames": frames,
        "npz_schemas": npz_schemas,
        "validation": {
            "schema_version": "1.0.0",
            "validated_at_utc": 当前世界时(),
            "checks": checks,
            "failed_checks": [],
            "scope_note": "4个候选与4个单样本仅属于独立深度探针，不进入48/96主分析分母。",
        },
    }


def 构建深度探针对照(
    main_candidates: pd.DataFrame,
    deep: dict[str, Any],
    manifest: dict[str, Any],
) -> pd.DataFrame:
    """把 7XL0 轻量 adherence 与 near-official 探针做描述性对照。"""

    baseline = main_candidates[
        (main_candidates["profile"] == "balanced_adherence_all12")
        & (main_candidates["selection_rank"] == 1)
    ]
    probe = deep["frames"]["candidates"]
    if len(baseline) != 2 or len(probe) != 4:
        raise ValueError("深度探针对照分组不是 baseline n=2 / probe n=4")
    rows = []
    for cohort, label, frame, contract in (
        (
            "balanced_adherence_7xl0",
            "轻量 adherence：7XL0",
            baseline,
            manifest["profiles"]["balanced_adherence_all12"],
        ),
        (
            深度探针支路,
            "近官方采样深度：7XL0",
            probe,
            manifest["profiles"][深度探针支路],
        ),
    ):
        rows.append(
            {
                "cohort": cohort,
                "cohort_label_cn": label,
                "candidate_count": len(frame),
                "folding_samples_per_candidate": int(contract["folding_diffusion_samples"]),
                "design_sampling_steps": int(contract["design_sampling_steps"]),
                "design_recycling_steps": int(contract["design_recycling_steps"]),
                "inverse_fold_sampling_steps": int(contract["inverse_fold_sampling_steps"]),
                "inverse_fold_recycling_steps": int(contract["inverse_fold_recycling_steps"]),
                "folding_sampling_steps": int(contract["folding_sampling_steps"]),
                "folding_recycling_steps": int(contract["folding_recycling_steps"]),
                "strict_filter_survivors": int(frame["pass_all_default_filters"].sum()),
                "median_design_to_target_iptm": float(frame["design_to_target_iptm"].median()),
                "median_design_ptm": float(frame["design_ptm"].median()),
                "median_filter_rmsd_a": float(frame["filter_rmsd_a"].median()),
                "prerefold_hotspot_positive": int(
                    (frame["prerefold_hotspot_coverage_fraction_lt8a"] > 0).sum()
                ),
                "interpretation": "不同候选数与复折叠样本数的描述性探针；不作显著性或因果推断。",
            }
        )
    return pd.DataFrame(rows)


def 构建汇总表(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """生成过滤、骨架、检查点和阶段层级汇总。"""

    candidates = frames["candidates"]
    filters = frames["filters"]
    filter_summary = (
        filters.groupby(
            ["filter_order", "filter_label_cn", "pass_column", "value_column", "operator", "threshold", "unit"],
            as_index=False,
        )
        .agg(candidate_count=("candidate_id", "count"), passed_count=("passed", "sum"))
        .sort_values("filter_order")
    )
    filter_summary["failed_count"] = filter_summary["candidate_count"] - filter_summary["passed_count"]
    filter_summary["failure_rate"] = filter_summary["failed_count"] / filter_summary["candidate_count"]

    scaffold_checkpoint = (
        candidates.groupby(
            ["selection_rank", "scaffold_id", "pdb_code", "scaffold_role", "profile", "checkpoint"],
            as_index=False,
        )
        .agg(
            candidate_count=("candidate_id", "count"),
            folding_sample_count=("candidate_id", lambda values: len(values) * 每候选样本数),
            filter_survivors=("pass_all_default_filters", "sum"),
            budget_items=("selected_by_budget", "sum"),
            median_design_to_target_iptm=("design_to_target_iptm", "median"),
            median_design_ptm=("design_ptm", "median"),
            median_filter_rmsd_a=("filter_rmsd_a", "median"),
            prerefold_hotspot_positive=(
                "prerefold_hotspot_coverage_fraction_lt8a",
                lambda values: int(sum(float(value) > 0 for value in values)),
            ),
            writer_analysis_best_match=("same_best_sample", "sum"),
        )
        .sort_values(["selection_rank", "profile"])
    )
    scaffold = (
        candidates.groupby(["selection_rank", "scaffold_id", "pdb_code", "scaffold_role"], as_index=False)
        .agg(
            candidate_count=("candidate_id", "count"),
            folding_sample_count=("candidate_id", lambda values: len(values) * 每候选样本数),
            filter_survivors=("pass_all_default_filters", "sum"),
            budget_items=("selected_by_budget", "sum"),
            median_design_to_target_iptm=("design_to_target_iptm", "median"),
            median_design_ptm=("design_ptm", "median"),
            median_filter_rmsd_a=("filter_rmsd_a", "median"),
            prerefold_hotspot_positive=(
                "prerefold_hotspot_coverage_fraction_lt8a",
                lambda values: int(sum(float(value) > 0 for value in values)),
            ),
        )
        .sort_values("selection_rank")
    )
    checkpoint = (
        candidates.groupby(["profile", "checkpoint", "checkpoint_label_cn"], as_index=False)
        .agg(
            scaffold_count=("scaffold_id", "nunique"),
            candidate_count=("candidate_id", "count"),
            folding_sample_count=("candidate_id", lambda values: len(values) * 每候选样本数),
            filter_survivors=("pass_all_default_filters", "sum"),
            budget_items=("selected_by_budget", "sum"),
            median_design_to_target_iptm=("design_to_target_iptm", "median"),
            median_design_ptm=("design_ptm", "median"),
            median_filter_rmsd_a=("filter_rmsd_a", "median"),
            prerefold_hotspot_positive=(
                "prerefold_hotspot_coverage_fraction_lt8a",
                lambda values: int(sum(float(value) > 0 for value in values)),
            ),
            writer_analysis_best_match=("same_best_sample", "sum"),
        )
        .sort_values("profile")
    )
    stage_summary = (
        frames["stages"].groupby(["profile", "checkpoint", "stage"], as_index=False)
        .agg(
            attempt_count=("attempt", "nunique"),
            total_elapsed_seconds=("elapsed_seconds", "sum"),
            median_elapsed_seconds=("elapsed_seconds", "median"),
            maximum_elapsed_seconds=("elapsed_seconds", "max"),
        )
        .sort_values(["profile", "stage"])
    )
    return {
        "filter_summary": filter_summary,
        "scaffold_checkpoint_summary": scaffold_checkpoint,
        "scaffold_summary": scaffold,
        "checkpoint_summary": checkpoint,
        "stage_summary": stage_summary,
    }


def 构建压力尝试() -> dict[str, pd.DataFrame]:
    """汇总双检查点压力尝试；它不进入 48 候选主分析。"""

    attempt_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    root = 运行目录 / "balanced_all12"
    for status_path in sorted(root.glob("*/attempt_*/run_status.json")):
        attempt = status_path.parent
        status = 读取JSON(status_path)
        stderr_path = attempt / "logs" / "01_design" / "stderr.log"
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
        design_stage_path = attempt / "stage_status" / "01_design.json"
        design_stage = 读取JSON(design_stage_path) if design_stage_path.is_file() else {}
        interruption_evidence = (
            "KeyboardInterrupt" in stderr
            and int(design_stage.get("return_code", 0)) == -2
            and design_stage.get("stage") == "01_design"
            and design_stage.get("status") == "FAILED"
        )
        attempt_rows.append(
            {
                "attempt": 相对路径(attempt),
                "profile": status.get("profile"),
                "selection_rank": status.get("selection_rank"),
                "candidate_id": status.get("candidate_id"),
                "status": status.get("status"),
                "elapsed_seconds": status.get("elapsed_seconds"),
                "completed_pipeline_stage_count": len(status.get("completed_pipeline_stages", [])),
                "partial_design_cif_count": len(list((attempt / "pipeline" / "intermediate_designs").glob("*.cif"))),
                "error_type": status.get("error_type"),
                "error_message": status.get("error_message"),
                "keyboard_interrupt_evidence": interruption_evidence,
                "keyboard_interrupt_stage_status_path": (
                    相对路径(design_stage_path) if design_stage_path.is_file() else None
                ),
                "keyboard_interrupt_stage_return_code": design_stage.get("return_code"),
                "keyboard_interrupt_stderr_path": 相对路径(stderr_path) if stderr_path.is_file() else None,
                "keyboard_interrupt_stderr_sha256": 文件哈希(stderr_path) if stderr_path.is_file() else None,
                "keyboard_interrupt_stderr_contains_trace": "KeyboardInterrupt" in stderr,
                "interpretation": (
                    "有 KeyboardInterrupt 与返回码 -2 的文件证据；这是受控中断的压力尝试，"
                    "不是完整主结果，也不据此判定模型算法失败。"
                    if interruption_evidence
                    else "历史压力尝试；不进入主候选统计。"
                ),
            }
        )

        # 失败阶段不会被旧版 runner 追加到 run_status.stage_records，必须直接读取
        # stage_status/01_design.json，才能保留导致安全停止的 swap 与返回码证据。
        observed_stages = []
        for stage_name in 预期阶段:
            stage_path = attempt / "stage_status" / f"{stage_name}.json"
            if stage_path.is_file():
                observed_stages.append(读取JSON(stage_path))
        for stage in observed_stages:
            stage_rows.append(
                {
                    "attempt": 相对路径(attempt),
                    "stage": stage.get("stage"),
                    "status": stage.get("status"),
                    "return_code": stage.get("return_code"),
                    "elapsed_seconds": stage.get("elapsed_seconds"),
                    "checkpoint_assets": ";".join(
                        item.get("asset", "") for item in stage.get("checkpoint_pre_sha256", [])
                    ),
                    "resource_csv": stage.get("resource_csv"),
                }
            )
            resource_value = stage.get("resource_csv")
            resource_path = 项目根目录 / str(resource_value) if resource_value else None
            if resource_path is not None and resource_path.is_file():
                frame = pd.read_csv(resource_path)
                if not frame.empty:
                    resource_rows.append(
                        {
                            "attempt": 相对路径(attempt),
                            "stage": stage.get("stage"),
                            "sample_count": len(frame),
                            "peak_process_tree_rss_gib": float(frame["rss_gib_sum"].max()),
                            "minimum_system_free_gib": float(frame["system_free_bytes"].min()) / 吉字节,
                            "swap_first_gib": float(frame.iloc[0]["swap_used_bytes"]) / 吉字节,
                            "swap_last_gib": float(frame.iloc[-1]["swap_used_bytes"]) / 吉字节,
                            "swap_stage_delta_gib": (
                                float(frame.iloc[-1]["swap_used_bytes"])
                                - float(frame.iloc[0]["swap_used_bytes"])
                            )
                            / 吉字节,
                            "swap_stage_range_gib": (
                                float(frame["swap_used_bytes"].max())
                                - float(frame["swap_used_bytes"].min())
                            )
                            / 吉字节,
                            "source": 相对路径(resource_path),
                        }
                    )
        for path in sorted(attempt.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            inventory_rows.append(
                {
                    "attempt": 相对路径(attempt),
                    "path": 相对路径(path),
                    "format": path.suffix.lower().lstrip(".") or "none",
                    "size_bytes": path.stat().st_size,
                    "sha256": 文件哈希(path),
                }
            )
    return {
        "stress_attempts": pd.DataFrame(attempt_rows),
        "stress_stage_timing": pd.DataFrame(stage_rows),
        "stress_resource_summary": pd.DataFrame(resource_rows),
        "stress_output_inventory": pd.DataFrame(inventory_rows),
    }


def 构建完整清单(
    selected: Iterable[tuple[str, dict[str, Any], Path, dict[str, Any]]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """列出主尝试及输入/溯源目录的每个文件、大小和 SHA-256。"""

    output_rows = []
    for profile, scaffold, attempt, _ in selected:
        for path in sorted(attempt.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            output_rows.append(
                {
                    "profile": profile,
                    "checkpoint": 支路检查点[profile],
                    "selection_rank": scaffold["selection_rank"],
                    "scaffold_id": scaffold["candidate_id"],
                    "attempt": 相对路径(attempt),
                    "path": 相对路径(path),
                    "format": path.suffix.lower().lstrip(".") or "none",
                    "size_bytes": path.stat().st_size,
                    "sha256": 文件哈希(path),
                }
            )
    source_rows = []
    for scope, root in (("inputs", 输入目录), ("provenance", 溯源目录)):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            source_rows.append(
                {
                    "scope": scope,
                    "path": 相对路径(path),
                    "format": path.suffix.lower().lstrip(".") or "none",
                    "size_bytes": path.stat().st_size,
                    "sha256": 文件哈希(path),
                }
            )
    return pd.DataFrame(output_rows).sort_values(["selection_rank", "profile", "path"]), pd.DataFrame(
        source_rows
    ).sort_values(["scope", "path"])


def 当前溯源哈希() -> dict[str, Any]:
    """每次执行都从当前 provenance 文件字节重算哈希，禁止复用旧分析值。"""

    records = []
    for path in sorted(溯源目录.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        records.append(
            {
                "path": 相对路径(path),
                "size_bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256_recomputed_from_current_bytes": 文件哈希(path),
            }
        )
    if not records:
        raise ValueError("provenance 目录没有可哈希文件")
    return {
        "schema_version": "1.0.0",
        "recomputed_at_utc": 当前世界时(),
        "reuse_cached_hashes": False,
        "file_count": len(records),
        "records": records,
    }


def 验证全部(
    frames: dict[str, pd.DataFrame],
    summaries: dict[str, pd.DataFrame],
    deep: dict[str, Any],
    protected_before: dict[str, str],
    protected_after: dict[str, str],
) -> dict[str, Any]:
    """执行最终阻断性检查，并返回可直接写入报告的验证结果。"""

    candidates = frames["candidates"]
    samples = frames["samples"]
    filters = frames["filters"]
    contacts = frames["contacts"]
    checks = {
        "exactly_24_complete_attempts": len(frames["runs"]) == 24
        and (frames["runs"]["status"] == "PIPELINE_COMPLETE").all(),
        "exactly_48_unique_candidates": len(candidates) == 预期候选数
        and candidates["candidate_id"].nunique() == 预期候选数,
        "exactly_96_folding_samples": len(samples) == 预期样本数,
        "each_candidate_has_two_samples": bool((samples.groupby("candidate_id").size() == 2).all()),
        "sample_indexes_are_0_and_1": bool(
            samples.groupby("candidate_id")["sample_index"].apply(lambda values: set(values) == {0, 1}).all()
        ),
        "each_candidate_has_ten_filters": bool(
            (filters.groupby("candidate_id").size() == len(过滤定义)).all()
        ),
        "each_candidate_has_two_hotspot_rows": bool((contacts.groupby("candidate_id").size() == 2).all()),
        "each_candidate_has_eight_lineage_rows": bool(
            (frames["lineages"].groupby("candidate_id").size() == 8).all()
        ),
        "all_frameworks_unchanged_outside_design_mask": bool(
            candidates["framework_sequence_unchanged"].all()
        ),
        "prerefold_metric_is_fraction": set(
            candidates["prerefold_hotspot_coverage_fraction_lt8a"].astype(float)
        ).issubset({0.0, 0.5, 1.0}),
        "writer_cif_matches_writer_best_coordinates": bool(
            (candidates["writer_cif_max_abs_coordinate_error_a"] <= 5e-4).all()
        ),
        "analysis_and_writer_indexes_in_bounds": bool(
            candidates["analysis_best_sample_index"].isin([0, 1]).all()
            and candidates["writer_best_sample_index"].isin([0, 1]).all()
        ),
        "observed_41_of_48_same_best_and_7_disagree": int(candidates["same_best_sample"].sum()) == 41
        and int((~candidates["same_best_sample"]).sum()) == 7,
        "exactly_24_budget_items": int(candidates["selected_by_budget"].sum()) == 24,
        "budget_and_filter_are_distinct_columns": "selected_by_budget" in candidates.columns
        and "pass_all_default_filters" in candidates.columns,
        "two_checkpoint_summaries": len(summaries["checkpoint_summary"]) == 2,
        "twenty_four_scaffold_checkpoint_rows": len(summaries["scaffold_checkpoint_summary"]) == 24,
        "twelve_scaffold_rows": len(summaries["scaffold_summary"]) == 12,
        "deep_probe_complete_but_not_merged_into_main": deep.get("status") == "COMPLETE"
        and len(deep["frames"]["candidates"]) == 4
        and len(candidates) == 48,
        "deep_probe_candidate_namespace_disjoint": set(deep["frames"]["candidates"]["candidate_id"]).isdisjoint(
            set(candidates["candidate_id"])
        ),
        "source_files_unchanged_by_analysis": protected_before == protected_after,
    }
    # Pandas/NumPy 的布尔标量不能稳定直接序列化为 JSON，统一转换为 Python bool。
    checks = {name: bool(passed) for name, passed in checks.items()}
    failed = [name for name, passed in checks.items() if not bool(passed)]
    if failed:
        raise ValueError(f"最终验证失败，拒绝形成正式分析：{failed}")
    same_best_count = int(candidates["same_best_sample"].sum())
    different_best_count = int((~candidates["same_best_sample"]).sum())
    return {
        "schema_version": "1.0.0",
        "validated_at_utc": 当前世界时(),
        "assessment": "READY_FOR_TECHNICAL_REVIEW_WITH_SCIENTIFIC_LIMITS",
        "checks": checks,
        "failed_checks": [],
        "scientific_limits": [
            "这是预训练模型推理与候选生成，不是模型权重训练。",
            "只使用单一 6X18 受体结合态 GLP-1(7–36) 正靶几何。",
            "没有 GLP-1(9–36)、其他反靶或多构象反筛，因此不支持型态选择性结论。",
            "目标 C 端酰胺未完成原子级验证。",
            "所有分数与距离都是计算代理，不支持解离常数、亲和力或实验成功率结论。",
            "实验性 Apple Metal Performance Shaders 分支不等同于官方 Linux + NVIDIA CUDA 基线。",
            (
                "本轮使用 Apple Metal Performance Shaders，且 PYTORCH_ENABLE_MPS_FALLBACK=1 允许"
                "不受支持的算子回退到中央处理器；日志未逐算子归因，不能把耗时或数值差异只归因于一种设备。"
            ),
            (
                f"两种最佳样本公式只在 {same_best_count}/48 个候选上一致；另有 "
                f"{different_best_count} 个候选选择了不同样本，writer CIF 与 Analyze 指标必须分别解释。"
            ),
            "每个骨架每个检查点只有 2 个候选，只适合流程复盘和候选描述。",
        ],
        "metric_semantics": {
            "bindsite_under_8rmsd": (
                "BoltzGen 在复折叠前结构上计算的 His7/Ala8 token-center 覆盖比例；"
                "取值为 0、0.5 或 1，不是接触数，也不是复折叠后的原子距离。"
            ),
            "refold_hotspot_distances": (
                "本脚本从 writer 输出 CIF 独立重算 His7/Ala8 到 VHH 设计残基的最小重原子和 Cα 距离。"
            ),
            "analysis_best_sample": "0.8×design_to_target_iPTM + 0.2×design_pTM 的最大值索引。",
            "writer_best_sample": "0.8×iPTM + 0.2×pTM 的最大值索引；writer CIF 坐标来自此样本。",
            "observed_best_index_agreement": (
                f"主分析中 {same_best_count}/48 一致，{different_best_count}/48 不一致。"
            ),
            "budget_item": "排序/展示预算项；它与是否通过全部十项过滤是两个独立事实。",
        },
    }


def 配置绘图() -> None:
    """配置 Mac 与无窗口环境都可读的中文图表字体。"""

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["PingFang SC", "Hiragino Sans GB", "Arial Unicode MS", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "font.size": 10.5,
            "axes.titlesize": 14,
            "figure.facecolor": "white",
            "axes.facecolor": "#F8FAFC",
            "axes.edgecolor": "#CAD5DF",
            "grid.color": "#DDE5EC",
        }
    )


def 保存图片(fig: plt.Figure, name: str) -> None:
    """将图片原子保存到 figures，并关闭图对象。"""

    图片目录.mkdir(parents=True, exist_ok=True)
    path = 图片目录 / name
    temporary = path.with_name(path.stem + ".tmp" + path.suffix)
    fig.savefig(temporary, dpi=210, bbox_inches="tight", facecolor="white", format="png")
    temporary.replace(path)
    plt.close(fig)


def 绘图(frames: dict[str, pd.DataFrame], summaries: dict[str, pd.DataFrame]) -> None:
    """生成六张直接服务于复盘的定量图。"""

    配置绘图()
    filter_summary = summaries["filter_summary"]
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.barh(filter_summary["filter_label_cn"], filter_summary["failed_count"], color=颜色["红"])
    ax.invert_yaxis()
    ax.set_xlabel("失败候选数（总计 48）")
    ax.set_title("十项冻结过滤的失败分布", loc="left", fontweight="bold")
    for index, value in enumerate(filter_summary["failed_count"]):
        ax.text(float(value) + 0.35, index, str(int(value)), va="center")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    保存图片(fig, "01_filter_failures.png")

    checkpoint = summaries["checkpoint_summary"].copy()
    x = np.arange(len(checkpoint))
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))
    axes[0].bar(x, checkpoint["median_design_to_target_iptm"], color=[颜色["蓝"], 颜色["紫"]])
    axes[0].set_xticks(x, checkpoint["checkpoint_label_cn"])
    axes[0].set_ylabel("中位 design-to-target iPTM")
    axes[0].set_ylim(0, 1)
    axes[1].bar(x, checkpoint["median_filter_rmsd_a"], color=[颜色["蓝"], 颜色["紫"]])
    axes[1].axhline(复合物均方根偏差阈值埃, color=颜色["红"], linestyle="--", label="2.5 Å 阈值")
    axes[1].set_xticks(x, checkpoint["checkpoint_label_cn"])
    axes[1].set_ylabel("中位复合物 RMSD（Å）")
    axes[1].legend()
    fig.suptitle("两个设计检查点的候选级描述比较", fontweight="bold")
    fig.text(
        0.5,
        -0.01,
        "每个骨架×检查点单元仅 n=2；柱为跨12骨架共24候选的描述性中位数，不作显著性或因果推断。",
        ha="center",
        fontsize=9,
    )
    保存图片(fig, "02_checkpoint_comparison.png")

    candidates = frames["candidates"].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(12, 6.5))
    index = np.arange(len(candidates))
    ax.scatter(
        index - 0.12,
        candidates["his7_min_heavy_atom_distance_a"],
        s=28,
        marker="o",
        color=颜色["橙"],
        label="His7 重原子（圆点）",
    )
    ax.scatter(
        index + 0.12,
        candidates["ala8_min_heavy_atom_distance_a"],
        s=30,
        marker="s",
        color=颜色["紫"],
        label="Ala8 重原子（方块）",
    )
    ax.axhline(重原子距离阈值埃, color=颜色["红"], linestyle="--", label="8 Å")
    ax.set_xlabel("48 个候选（按骨架、检查点、局部编号排列）")
    ax.set_ylabel("到 VHH 设计区的最小距离（Å）")
    ax.set_title(
        "writer 选中样本 CIF：复折叠后 His7/Ala8 独立重原子距离",
        loc="left",
        fontweight="bold",
    )
    ax.legend(ncol=3)
    ax.grid(axis="y")
    保存图片(fig, "03_refold_hotspot_distances.png")

    stage = summaries["stage_summary"]
    pivot = stage.pivot(index="stage", columns="checkpoint", values="median_elapsed_seconds").reindex(预期阶段)
    fig, ax = plt.subplots(figsize=(11, 6))
    pivot.plot(kind="bar", ax=ax, color=[颜色["紫"], 颜色["蓝"]])
    ax.set_ylabel("每骨架阶段中位耗时（秒）")
    ax.set_xlabel("阶段")
    ax.set_title("主流程阶段耗时", loc="left", fontweight="bold")
    ax.tick_params(axis="x", rotation=30)
    ax.grid(axis="y")
    保存图片(fig, "04_stage_timing.png")

    resources = frames["resource_summary"].reset_index(drop=True)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.2))
    sample_index = np.arange(len(resources))
    axes[0].scatter(
        sample_index,
        resources["swap_last_gib"],
        s=19,
        alpha=0.7,
        color=颜色["青"],
        label="阶段结束绝对 swap",
    )
    axes[0].set_xlabel("168 个骨架×检查点×阶段记录")
    axes[0].set_ylabel("绝对 swap used（GiB）")
    axes[0].set_title("绝对 swap 水位")
    axes[0].legend()
    axes[1].hist(resources["swap_stage_delta_gib"], bins=20, color=颜色["橙"])
    axes[1].axvline(0, color=颜色["深蓝"], linewidth=1)
    axes[1].set_xlabel("阶段首末 swap 变化（GiB）")
    axes[1].set_ylabel("阶段数")
    axes[1].set_title("单阶段 swap 增量（可正可负）")
    fig.suptitle("资源复盘：绝对 swap 与阶段增量；不含独立 MPS 进程内存", fontweight="bold")
    保存图片(fig, "05_resource_envelope.png")

    heat = summaries["scaffold_checkpoint_summary"].pivot(
        index="pdb_code", columns="checkpoint", values="median_design_to_target_iptm"
    )
    heat = heat.reindex(summaries["scaffold_summary"]["pdb_code"])
    fig, ax = plt.subplots(figsize=(7.5, 7))
    image = ax.imshow(heat.to_numpy(), aspect="auto", cmap="viridis", vmin=0, vmax=1)
    ax.set_xticks(np.arange(len(heat.columns)), heat.columns, rotation=20)
    ax.set_yticks(np.arange(len(heat.index)), heat.index)
    ax.set_title(
        "骨架 × 检查点的中位 design-to-target iPTM（每格 n=2，仅描述性）",
        loc="left",
        fontweight="bold",
    )
    fig.colorbar(image, ax=ax, label="中位计算分数")
    保存图片(fig, "06_scaffold_checkpoint_heatmap.png")


def 绘制深度探针对照(comparison: pd.DataFrame) -> None:
    """绘制轻量 7XL0 与独立 near-official 探针的描述性对照。"""

    if len(comparison) != 2:
        raise ValueError("深度探针对照图要求恰有两个队列")
    配置绘图()
    # 横轴采用短标签并显式换行，避免完整队列名在三联图的窄面板中相互遮挡。
    # 完整比较范围已经写在总标题与图下注释中，因此短标签不会丢失语义。
    label_map = {
        "轻量 adherence：7XL0": "轻量",
        "近官方采样深度：7XL0": "近官方深度",
    }
    labels = [
        f"{label_map.get(row.cohort_label_cn, row.cohort_label_cn)} n={int(row.candidate_count)}"
        for row in comparison.itertuples(index=False)
    ]
    x = np.arange(2)
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.8))
    metrics = (
        ("median_design_to_target_iptm", "中位 design-to-target iPTM", (0, 1)),
        ("median_design_ptm", "中位 design pTM", (0, 1)),
        ("median_filter_rmsd_a", "中位复合物 RMSD（Å）", None),
    )
    for ax, (column, title, ylim) in zip(axes, metrics):
        values = comparison[column].astype(float).to_numpy()
        ax.bar(x, values, color=[颜色["蓝"], 颜色["紫"]])
        ax.set_xticks(x, labels)
        ax.tick_params(axis="x", labelsize=9)
        ax.set_title(title)
        if ylim is not None:
            ax.set_ylim(*ylim)
        for index, value in enumerate(values):
            ax.text(index, value, f"{value:.3f}", ha="center", va="bottom")
        ax.grid(axis="y")
    axes[2].axhline(
        复合物均方根偏差阈值埃,
        color=颜色["红"],
        linestyle="--",
        linewidth=1,
        label="2.5 Å 阈值",
    )
    axes[2].legend()
    fig.suptitle(
        "独立 near-official 7XL0 深度探针：与轻量 adherence 的描述性对照",
        fontweight="bold",
    )
    # 为横轴短标签与两行口径说明预留固定底边距，避免 bbox_inches="tight"
    # 导出时将说明挤入刻度标签区域。
    fig.subplots_adjust(bottom=0.20, top=0.78, wspace=0.20)
    fig.text(
        0.5,
        0.035,
        (
            "轻量＝balanced_adherence_all12 的 7XL0（n=2）；近官方深度＝独立 "
            "near_official_adherence_7xl0（n=4）。\n"
            "候选数、复折叠样本数和采样步数不同；不作显著性、因果、亲和力或选择性推断。"
        ),
        ha="center",
        fontsize=9,
    )
    保存图片(fig, "07_deep_probe_comparison.png")


def 数据字典() -> dict[str, Any]:
    """返回报告与 Notebook 可复用的字段解释。"""

    return {
        "schema_version": "1.0.0",
        "generated_at_utc": 当前世界时(),
        "tables": {
            "candidates.csv": "48 个候选；候选主键加检查点前缀，避免两条支路的原始 ID 碰撞。",
            "folding_samples.csv": "96 个复折叠样本；每候选 2 行，包含 writer 与 Analyze 两套选样本分数。",
            "filter_long.csv": "48×10 条逐项过滤记录；每项都按冻结阈值独立复算。",
            "hotspot_contacts.csv": "每候选 His7、Ala8 各一行的复折叠后独立距离。",
            "stage_timing.csv": "24 个主尝试×7 个阶段的进程、监控、检查点与耗时合同。",
            "resource_samples.csv": "每 2 秒左右采样的进程树 RSS、CPU、系统 free、swap 与磁盘；无独立 MPS 内存。",
            "candidate_lineage.csv": "每候选 8 个关键谱系角色，含路径、大小与 SHA-256。",
            "output_inventory.csv": "24 个主尝试中所有非 pyc 文件的完整清单。",
            "stress_attempts.csv": "双检查点压力尝试；不进入 48 候选主统计。",
            "deep_probe_runs.csv": "独立 near-official 7XL0 探针的一条运行记录。",
            "deep_probe_candidates.csv": "独立探针 4 个候选；使用 deep_probe:: 主键且不进入主候选表。",
            "deep_probe_folding_samples.csv": "独立探针 4 个单样本复折叠记录。",
            "deep_probe_filter_long.csv": "独立探针 4×10 条过滤记录。",
            "deep_probe_summary.json": "独立探针合同、计数、验证和与轻量7XL0的描述性对照。",
        },
        "axes": {
            "fold_coords": "[复折叠样本, 原子槽, x/y/z]，本轮第一轴必须是 2。",
            "atom_resolved_mask": "[批次=1, 原子槽]；真值槽位按原顺序写入 CIF。",
            "atom_to_token": "[批次=1, 原子槽, token] 的归属映射。",
            "score_arrays": "[复折叠样本]；本轮每个分数字段都必须是 (2,)。",
        },
        "important_distinctions": {
            "prerefold_bindsite_fraction": "复折叠前 token-center 覆盖比例，不是距离矩阵、接触数或复折叠几何。",
            "refold_heavy_atom_and_ca_distance": "从 writer CIF 独立计算的候选几何；重原子与 Cα 是两种不同定义。",
            "designed_sequence": "三个设计区域按链顺序拼接；不是完整 VHH 链。",
            "designed_chain_sequence": "包含固定框架与三个设计区域的完整 VHH 链。",
            "num_filters_passed": "BoltzGen 内部前缀分数；本脚本另有 computed_filter_pass_count 表示十项总通过数。",
            "budget": "预算目录是排序展示，不等于通过 pass_filters。",
            "device": (
                "Apple Metal Performance Shaders 运行且启用中央处理器 fallback；"
                "资源表不含可独立归因的 MPS 进程内存。"
            ),
        },
        "forbidden_inference": "这些计算结果不得换算为解离常数、亲和力、实验成功率或型态选择性。",
    }


def 输出派生文件(
    readiness: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    summaries: dict[str, pd.DataFrame],
    stress: dict[str, pd.DataFrame],
    inventory: pd.DataFrame,
    sources: pd.DataFrame,
    npz_schemas: list[dict[str, Any]],
    validation: dict[str, Any],
    deep: dict[str, Any],
    deep_comparison: pd.DataFrame,
) -> None:
    """在全部合同通过后一次性写出表、JSON 与图片。"""

    table_map = {
        "runs.csv": frames["runs"],
        "candidates.csv": frames["candidates"],
        "folding_samples.csv": frames["samples"],
        "hotspot_contacts.csv": frames["contacts"],
        "filter_long.csv": frames["filters"],
        "candidate_lineage.csv": frames["lineages"],
        "stage_timing.csv": frames["stages"],
        "resource_samples.csv": frames["resources"],
        "stage_resource_summary.csv": frames["resource_summary"],
        "output_inventory.csv": inventory,
        "input_provenance_inventory.csv": sources,
        "deep_probe_runs.csv": deep["frames"]["runs"],
        "deep_probe_candidates.csv": deep["frames"]["candidates"],
        "deep_probe_folding_samples.csv": deep["frames"]["samples"],
        "deep_probe_filter_long.csv": deep["frames"]["filters"],
        "deep_probe_hotspot_contacts.csv": deep["frames"]["contacts"],
        "deep_probe_stage_timing.csv": deep["frames"]["stages"],
        "deep_probe_resource_samples.csv": deep["frames"]["resources"],
        "deep_probe_resource_summary.csv": deep["frames"]["resource_summary"],
        "deep_probe_lineage.csv": deep["frames"]["lineages"],
        "deep_probe_output_inventory.csv": deep["frames"]["inventory"],
        "deep_probe_comparison.csv": deep_comparison,
        **{f"{name}.csv": frame for name, frame in summaries.items()},
        **{f"{name}.csv": frame for name, frame in stress.items()},
    }
    for name, frame in table_map.items():
        写表(分析目录 / name, frame)
    写JSON(分析目录 / "readiness.json", readiness)
    写JSON(分析目录 / "current_provenance_hashes.json", 当前溯源哈希())
    写JSON(
        分析目录 / "npz_schema.json",
        {
            "schema_version": "1.0.0",
            "generated_at_utc": 当前世界时(),
            "file_count": len(npz_schemas),
            "expected_sample_axis": 每候选样本数,
            "records": npz_schemas,
            "important_limit": "fold NPZ 不含可供本报告绘制的完整二维 PAE 矩阵。",
        },
    )
    写JSON(分析目录 / "data_dictionary.json", 数据字典())
    写JSON(分析目录 / "validation.json", validation)
    写JSON(
        分析目录 / "deep_probe_npz_schema.json",
        {
            "schema_version": "1.0.0",
            "generated_at_utc": 当前世界时(),
            "file_count": len(deep["npz_schemas"]),
            "expected_sample_axis": 1,
            "records": deep["npz_schemas"],
        },
    )
    写JSON(
        分析目录 / "deep_probe_summary.json",
        {
            "schema_version": "1.0.0",
            "generated_at_utc": 当前世界时(),
            "status": deep["status"],
            "attempt": deep["attempt"],
            "profile_contract": deep["profile_contract"],
            "counts": {
                "runs": len(deep["frames"]["runs"]),
                "candidates": len(deep["frames"]["candidates"]),
                "folding_samples": len(deep["frames"]["samples"]),
                "filter_rows": len(deep["frames"]["filters"]),
                "strict_filter_survivors": int(
                    deep["frames"]["candidates"]["pass_all_default_filters"].sum()
                ),
            },
            "validation": deep["validation"],
            "comparison_to_lightweight_7xl0": deep_comparison.to_dict("records"),
            "scope_note": "独立探针不进入主分析48候选、96复折叠样本或其通过率分母。",
        },
    )
    run_summary = {
        "schema_version": "1.0.0",
        "generated_at_utc": 当前世界时(),
        "execution_semantics": "pretrained_inference_candidate_generation_not_weight_training",
        "counts": {
            "complete_attempts": len(frames["runs"]),
            "scaffolds": frames["runs"]["scaffold_id"].nunique(),
            "checkpoints": frames["runs"]["checkpoint"].nunique(),
            "candidates": len(frames["candidates"]),
            "folding_samples": len(frames["samples"]),
            "filter_rows": len(frames["filters"]),
            "strict_filter_survivors": int(frames["candidates"]["pass_all_default_filters"].sum()),
            "budget_items": int(frames["candidates"]["selected_by_budget"].sum()),
            "writer_analysis_best_index_matches": int(frames["candidates"]["same_best_sample"].sum()),
        },
        "best_index_formulas": validation["metric_semantics"],
        "scientific_limits": validation["scientific_limits"],
        "stress_attempt_count": len(stress["stress_attempts"]),
        "independent_deep_probe": {
            "status": deep["status"],
            "candidates": len(deep["frames"]["candidates"]),
            "folding_samples": len(deep["frames"]["samples"]),
            "included_in_main_counts": False,
        },
    }
    写JSON(分析目录 / "run_summary.json", run_summary)
    绘图(frames, summaries)
    绘制深度探针对照(deep_comparison)

    # 清单最后生成，避免自引用；清单自身不写入自己的哈希集合。
    artifact_rows = []
    for root in (分析目录, 图片目录):
        for path in sorted(root.glob("*")):
            if not path.is_file() or path.name == "analysis_manifest.json":
                continue
            artifact_rows.append(
                {
                    "path": 相对路径(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": 文件哈希(path),
                }
            )
    写JSON(
        分析目录 / "analysis_manifest.json",
        {
            "schema_version": "1.0.0",
            "generated_at_utc": 当前世界时(),
            "artifact_count_excluding_this_manifest": len(artifact_rows),
            "artifacts": artifact_rows,
        },
    )


def main() -> int:
    """命令行入口：先就绪检查，再严格分析。"""

    parser = argparse.ArgumentParser(description="严格汇总 Mac BoltzGen 两条单检查点主支路")
    parser.add_argument(
        "--check-ready",
        action="store_true",
        help="只检查 24 个主尝试是否全部完成，不创建 analysis/ 或 figures/",
    )
    args = parser.parse_args()

    manifest = 读取JSON(输入清单路径)
    readiness, selected = 就绪审计(manifest)
    if args.check_ready:
        print(json.dumps(readiness, ensure_ascii=False, indent=2))
        return 0 if readiness["ready"] else 3
    if not readiness["ready"]:
        print(json.dumps(readiness, ensure_ascii=False, indent=2), file=sys.stderr)
        raise 数据尚未就绪(
            f"仅 {readiness['complete_attempts']}/{readiness['expected_attempts']} 个主尝试完整；"
            f"独立探针状态={readiness['deep_probe']['status']}；等待后重跑，未写任何派生分析。"
        )

    deep_attempt = 项目根目录 / readiness["deep_probe"]["latest_attempt"]
    deep_scaffold = next(
        record
        for record in manifest["scaffold_population"]["records"]
        if int(record["selection_rank"]) == 1
    )
    protected_selection = [
        *selected,
        (
            深度探针支路,
            deep_scaffold,
            deep_attempt,
            读取JSON(deep_attempt / "run_status.json"),
        ),
    ]
    protected_before = 输入与运行保护快照(protected_selection)
    built = 构建主分析(manifest, selected)
    frames = built["frames"]
    deep = 构建独立深度探针(manifest, readiness)
    deep_comparison = 构建深度探针对照(frames["candidates"], deep, manifest)
    summaries = 构建汇总表(frames)
    stress = 构建压力尝试()
    inventory, sources = 构建完整清单(selected)
    protected_after = 输入与运行保护快照(protected_selection)
    validation = 验证全部(frames, summaries, deep, protected_before, protected_after)
    输出派生文件(
        readiness,
        frames,
        summaries,
        stress,
        inventory,
        sources,
        built["npz_schemas"],
        validation,
        deep,
        deep_comparison,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "candidates": len(frames["candidates"]),
                "folding_samples": len(frames["samples"]),
                "deep_probe_candidates_excluded_from_main": len(deep["frames"]["candidates"]),
                "deep_probe_folding_samples_excluded_from_main": len(deep["frames"]["samples"]),
                "analysis_dir": str(分析目录),
                "figures_dir": str(图片目录),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except 数据尚未就绪 as error:
        print(f"WAITING: {error}", file=sys.stderr)
        raise SystemExit(3)
