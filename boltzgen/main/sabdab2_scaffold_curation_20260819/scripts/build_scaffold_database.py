#!/usr/bin/env python3
"""从 SAbDab2 SD-H 快照建立可审计的 BoltzGen VHH scaffold 数据库。

本脚本故意把三个概念分开：

* ``SD-H``：SAbDab2 的结构类型；它可以包含 camelid VHH、人源化 VHH，
  也可能包含普通人源单域 VH。
* ``primary VHH pool``：本项目首轮只使用有 camelid 来源证据、X-ray
  分辨率不高于 2.5 Å 的结构实例。
* ``selected scaffold``：结构、编号和二硫键通过 QC，且在框架聚类后被选为
  多样化代表。入选不表示已经结合 GLP-1，也不表示已经具有选择性。

输入是冻结的 SAbDab2 summary CSV 与 full IMGT-numbered mmCIF tar.gz。
输出包括 TSV、SQLite、逐残基映射、QC/排除日志、聚类、12 个代表 scaffold
包，以及用于报告的统计数据。所有阈值来自版本化 JSON 政策文件。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import tarfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import gemmi
import numpy as np
import pandas as pd
import requests
import yaml
from Bio.SeqUtils.ProtParam import ProteinAnalysis
from sklearn.cluster import AgglomerativeClustering


AA3_TO_1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}

BACKBONE_ATOMS = {"N", "CA", "C", "O"}
MISSING_TOKENS = {"", ".", "?", "NA", "N/A", "NULL", "NONE"}


@dataclass
class CandidateProfile:
    """一个 antibody-instance 的结构摘要与聚类所需内存对象。"""

    record: dict[str, Any]
    residues: list[dict[str, Any]]
    ca_by_imgt: dict[str, np.ndarray]
    anchor_ca: dict[int, np.ndarray]
    disulfides: list[dict[str, Any]]
    hard_reasons: list[str]
    soft_flags: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_text(value: Any) -> str:
    """把 SAbDab2/mmCIF 的各种缺失标记统一为空字符串。"""

    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.upper() in MISSING_TOKENS else text


def safe_float(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def parse_imgt_position(auth_seq_id: Any, insertion_code: Any) -> tuple[int, str] | None:
    """解析 SAbDab2 重写后的 IMGT author residue id。

    常规位置由整数 ``auth_seq_id`` 和可选 ``pdbx_PDB_ins_code`` 组成；
    少数文件可能把字母直接附在数字后。返回 ``(base, insertion)``。
    """

    auth = clean_text(auth_seq_id)
    ins = clean_text(insertion_code).upper()
    match = re.fullmatch(r"(-?\d+)([A-Za-z]*)", auth)
    if not match:
        return None
    base = int(match.group(1))
    suffix = (match.group(2) or ins).upper()
    return base, suffix


def imgt_key(base: int, insertion: str) -> str:
    return f"{base}{insertion}" if insertion else str(base)


def imgt_region(base: int, config: dict[str, Any]) -> str:
    for name, (start, end) in config["imgt_regions"].items():
        if start <= base <= end:
            return name
    return "OUTSIDE_VARIABLE"


def is_framework(region: str) -> bool:
    return region.startswith("FR")


def cif_table(block: gemmi.cif.Block, category: str) -> tuple[Any, dict[str, int]]:
    table = block.find_mmcif_category(category)
    columns = {tag.split(".")[-1]: index for index, tag in enumerate(table.tags)}
    return table, columns


def row_value(row: Any, columns: dict[str, int], name: str, default: str = "") -> str:
    index = columns.get(name)
    return default if index is None else str(row[index])


def choose_residue_conformer(atom_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, bool]:
    """按残基而不是逐原子选择一个 altloc 构象。

    无 altloc 的原子属于所有构象。候选 altloc 首先按主链完整性排序，再按总
    occupancy 排序，最后稳定优先 A/字典序。这样不会拼出实验中不存在的混合
    构象。相同 atom name 若仍重复，只保留 occupancy 较高的一条并记录风险。
    """

    common = [row for row in atom_rows if clean_text(row["alt_id"]) == ""]
    alt_ids = sorted({clean_text(row["alt_id"]) for row in atom_rows if clean_text(row["alt_id"])})
    candidates = alt_ids or [""]

    scored: list[tuple[tuple[int, float, int, str], str, list[dict[str, Any]]]] = []
    for alt in candidates:
        selected = common + ([row for row in atom_rows if clean_text(row["alt_id"]) == alt] if alt else [])
        names = {row["atom_name"] for row in selected}
        backbone_complete = int(BACKBONE_ATOMS.issubset(names))
        occupancy = sum(float(row["occupancy"]) for row in selected)
        # A 是最常见的主构象；空 altloc 只在没有分支时出现。
        preference = 0 if alt == "A" else 1
        scored.append(((backbone_complete, occupancy, -preference, alt), alt, selected))
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen_alt, chosen_rows = scored[0][1], scored[0][2]

    by_name: dict[str, dict[str, Any]] = {}
    duplicate = False
    for atom in chosen_rows:
        name = atom["atom_name"]
        if name in by_name:
            duplicate = True
            if float(atom["occupancy"]) <= float(by_name[name]["occupancy"]):
                continue
        by_name[name] = atom
    return list(by_name.values()), chosen_alt, duplicate


def extract_profile(
    block: gemmi.cif.Block,
    metadata: dict[str, Any],
    config: dict[str, Any],
    archive_member: str,
) -> CandidateProfile:
    """从一个 SAbDab2 full mmCIF 中提取指定 SD-H 实例并执行结构 QC。"""

    instance_id = str(metadata["INSTANCE"])
    target_hchain = clean_text(metadata.get("Hchain"))
    requested_model = int(float(metadata.get("model", 0))) + 1  # CSV 0-based；mmCIF 1-based。
    hard: list[str] = []
    soft: list[str] = []

    atom_table, columns = cif_table(block, "_atom_site.")
    required_columns = {
        "group_PDB", "type_symbol", "label_atom_id", "label_alt_id", "label_comp_id",
        "label_asym_id", "label_entity_id", "label_seq_id", "Cartn_x",
        "Cartn_y", "Cartn_z", "occupancy", "B_iso_or_equiv", "auth_seq_id",
        "auth_asym_id", "pdbx_PDB_model_num",
    }
    missing_columns = sorted(required_columns - set(columns))
    if missing_columns:
        record = {"candidate_id": instance_id, "hard_status": "FAIL", "hard_reason": "missing_atom_site_columns"}
        return CandidateProfile(record, [], {}, {}, [], [f"missing_atom_site_columns:{','.join(missing_columns)}"], [])

    chain_groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in atom_table:
        if row_value(row, columns, "group_PDB") != "ATOM":
            continue
        model_text = clean_text(row_value(row, columns, "pdbx_PDB_model_num"))
        if model_text and int(float(model_text)) != requested_model:
            continue
        label_seq = clean_text(row_value(row, columns, "label_seq_id"))
        if not label_seq or not re.fullmatch(r"\d+", label_seq):
            continue
        label_asym = clean_text(row_value(row, columns, "label_asym_id"))
        auth_asym = clean_text(row_value(row, columns, "auth_asym_id"))
        entity = clean_text(row_value(row, columns, "label_entity_id"))
        if target_hchain in {label_asym, auth_asym}:
            chain_groups[(label_asym, auth_asym, entity)].append(row)

    if not chain_groups:
        record = {"candidate_id": instance_id, "hard_status": "FAIL", "hard_reason": "hchain_not_found"}
        return CandidateProfile(record, [], {}, {}, [], ["hchain_not_found"], [])

    scored_groups: list[tuple[tuple[int, int, int], tuple[str, str, str], list[Any]]] = []
    for key, rows in chain_groups.items():
        bases = set()
        label_seq_ids = set()
        for row in rows:
            parsed = parse_imgt_position(
                row_value(row, columns, "auth_seq_id"),
                row_value(row, columns, "pdbx_PDB_ins_code"),
            )
            if parsed and 1 <= parsed[0] <= 128:
                bases.add(parsed[0])
                label_seq_ids.add(int(row_value(row, columns, "label_seq_id")))
        anchor_count = len(set(config["required_imgt_anchors"]) & bases)
        exact_auth = int(key[1] == target_hchain)
        scored_groups.append(((exact_auth, anchor_count, len(label_seq_ids)), key, rows))
    scored_groups.sort(key=lambda item: item[0], reverse=True)
    best_score = scored_groups[0][0]
    tied = [item for item in scored_groups if item[0] == best_score]
    if len(tied) > 1:
        record = {"candidate_id": instance_id, "hard_status": "FAIL", "hard_reason": "ambiguous_hchain_mapping"}
        return CandidateProfile(record, [], {}, {}, [], ["ambiguous_hchain_mapping"], [])

    (_, (label_asym, auth_asym, entity_id), selected_chain_rows) = scored_groups[0]
    raw_residue_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_chain_rows:
        label_seq = int(row_value(row, columns, "label_seq_id"))
        xyz = tuple(float(row_value(row, columns, name)) for name in ("Cartn_x", "Cartn_y", "Cartn_z"))
        occupancy = safe_float(row_value(row, columns, "occupancy"))
        b_factor = safe_float(row_value(row, columns, "B_iso_or_equiv"))
        raw_residue_groups[label_seq].append(
            {
                "label_seq_source": label_seq,
                "label_asym_source": label_asym,
                "auth_asym_source": auth_asym,
                "entity_id_source": entity_id,
                "auth_seq_id": row_value(row, columns, "auth_seq_id"),
                "ins_code": row_value(row, columns, "pdbx_PDB_ins_code"),
                "comp_id": row_value(row, columns, "label_comp_id").upper(),
                "atom_name": row_value(row, columns, "label_atom_id").strip(),
                "element": row_value(row, columns, "type_symbol").upper(),
                "alt_id": row_value(row, columns, "label_alt_id"),
                "xyz": xyz,
                "occupancy": 1.0 if occupancy is None else occupancy,
                "occupancy_missing": occupancy is None,
                "b_factor": 0.0 if b_factor is None else b_factor,
            }
        )

    residues: list[dict[str, Any]] = []
    coordinate_error = False
    duplicate_atom_error = False
    for label_seq in sorted(raw_residue_groups):
        raw_atoms = raw_residue_groups[label_seq]
        comp_ids = {atom["comp_id"] for atom in raw_atoms}
        parsed_positions = {
            parse_imgt_position(atom["auth_seq_id"], atom["ins_code"]) for atom in raw_atoms
        }
        parsed_positions.discard(None)
        if len(comp_ids) != 1 or len(parsed_positions) != 1:
            hard.append(f"residue_identity_ambiguous:label_seq={label_seq}")
            continue
        base, insertion = next(iter(parsed_positions))
        if not 1 <= base <= 128:
            # SAbDab2 会把标签/常数区继续编号；它们不属于 VHH variable scaffold。
            continue
        atoms, selected_altloc, duplicate = choose_residue_conformer(raw_atoms)
        duplicate_atom_error |= duplicate
        for atom in atoms:
            xyz = np.asarray(atom["xyz"], dtype=float)
            if not np.isfinite(xyz).all() or np.allclose(xyz, 0.0):
                coordinate_error = True
        comp_id = next(iter(comp_ids))
        region = imgt_region(base, config)
        atom_names = {atom["atom_name"] for atom in atoms if atom["element"] != "H"}
        backbone_complete = BACKBONE_ATOMS.issubset(atom_names)
        residues.append(
            {
                "candidate_id": instance_id,
                "ordinal": 0,  # 在完成排序后赋值。
                "source_label_asym_id": label_asym,
                "source_label_seq_id": label_seq,
                "source_auth_asym_id": auth_asym,
                "source_auth_seq_id": clean_text(raw_atoms[0]["auth_seq_id"]),
                "source_ins_code": insertion,
                "imgt_position": imgt_key(base, insertion),
                "imgt_base": base,
                "imgt_insertion": insertion,
                "region": region,
                "normalized_label_asym_id": "A",
                "normalized_label_seq_id": 0,
                "comp_id": comp_id,
                "one_letter": AA3_TO_1.get(comp_id, "X"),
                "observed": True,
                "backbone_complete": backbone_complete,
                "selected_altloc": selected_altloc,
                "mean_occupancy": float(np.mean([atom["occupancy"] for atom in atoms])),
                "occupancy_missing": any(atom["occupancy_missing"] for atom in atoms),
                "mean_b_factor": float(np.mean([atom["b_factor"] for atom in atoms])),
                "atoms": atoms,
            }
        )

    residues.sort(key=lambda item: item["source_label_seq_id"])
    for ordinal, residue in enumerate(residues, start=1):
        residue["ordinal"] = ordinal
        residue["normalized_label_seq_id"] = ordinal

    # 一个候选中每个 IMGT 位置必须只映射到一个结构残基。若两个 label_seq_id
    # 落到同一个 IMGT 位点，后续字典会静默覆盖其中一个，既会污染聚类，也会
    # 使导出的 auth 编号含义不唯一，所以在这里直接硬失败。
    imgt_position_counts = Counter(residue["imgt_position"] for residue in residues)
    duplicate_imgt_positions = sorted(
        (position for position, count in imgt_position_counts.items() if count > 1),
        key=lambda key: (int(re.match(r"\d+", key).group()), key),
    )
    if duplicate_imgt_positions:
        hard.append(
            f"duplicate_imgt_position:{','.join(duplicate_imgt_positions[:12])}"
        )

    if not residues:
        hard.append("no_variable_domain_residues")
    if coordinate_error:
        hard.append("invalid_or_zero_coordinates")
    if duplicate_atom_error:
        soft.append("duplicate_atom_name_resolved_by_occupancy")

    allowed = set(config["allowed_residues"])
    nonstandard = sorted({residue["comp_id"] for residue in residues if residue["comp_id"] not in allowed})
    if nonstandard:
        hard.append(f"nonstandard_variable_residue:{','.join(nonstandard)}")

    length = len(residues)
    if length and not config["scope"]["min_variable_length_aa"] <= length <= config["scope"]["max_variable_length_aa"]:
        hard.append(f"variable_length_out_of_range:{length}")
    bases = {residue["imgt_base"] for residue in residues}
    if bases:
        if min(bases) > config["scope"]["max_imgt_n_terminal_position"]:
            hard.append(f"n_terminal_framework_truncated:first_imgt={min(bases)}")
        if max(bases) < config["scope"]["min_imgt_c_terminal_position"]:
            hard.append(f"c_terminal_framework_truncated:last_imgt={max(bases)}")

    missing_anchors = sorted(set(config["required_imgt_anchors"]) - bases)
    if missing_anchors:
        hard.append(f"missing_imgt_anchors:{','.join(map(str, missing_anchors))}")

    incomplete_framework = [
        residue["imgt_position"] for residue in residues
        if is_framework(residue["region"]) and not residue["backbone_complete"]
    ]
    if incomplete_framework:
        hard.append(f"framework_backbone_incomplete:{','.join(incomplete_framework[:12])}")

    # CDR 是导出 YAML 中真正会被 BoltzGen 重新设计的区域。即便框架完整，
    # 只要某个 CDR 残基缺少 N/CA/C/O，生成模型看到的骨架条件就不完整，
    # 因而必须作为硬失败，而不能留到导出后再碰运气。
    incomplete_design = [
        residue["imgt_position"] for residue in residues
        if residue["region"] in {"CDR1", "CDR2", "CDR3"} and not residue["backbone_complete"]
    ]
    if incomplete_design:
        hard.append(f"design_region_backbone_incomplete:{','.join(incomplete_design[:12])}")

    # 低 occupancy 表示该残基坐标只由较小比例的晶体状态支持。小于0.5时不再
    # 把它作为生成骨架；0.5–0.7保留但标软风险，便于优先选电子密度更明确者。
    hard_low_occupancy = [
        residue["imgt_position"] for residue in residues
        if residue["mean_occupancy"] < config["min_residue_mean_occupancy_hard"]
    ]
    soft_low_occupancy = [
        residue["imgt_position"] for residue in residues
        if config["min_residue_mean_occupancy_hard"]
        <= residue["mean_occupancy"]
        < config["min_residue_mean_occupancy_soft"]
    ]
    if hard_low_occupancy:
        hard.append(f"residue_mean_occupancy_below_0.5:{','.join(hard_low_occupancy[:12])}")
    if soft_low_occupancy:
        soft.append(f"residue_mean_occupancy_below_0.7:{','.join(soft_low_occupancy[:12])}")
    if any(residue["occupancy_missing"] for residue in residues):
        soft.append("atom_occupancy_missing_assumed_1.0")

    # 检查按 entity sequence 相邻的残基是否发生坐标断裂。IMGT 号码本身有规则间隙，
    # 所以连续性必须按 label_seq，而不是拿 38→39 等 author 编号直接相减。
    sequence_gaps: list[str] = []
    peptide_breaks: list[str] = []
    for left, right in zip(residues, residues[1:]):
        if right["source_label_seq_id"] != left["source_label_seq_id"] + 1:
            sequence_gaps.append(f"{left['imgt_position']}->{right['imgt_position']}")
            continue
        left_atoms = {atom["atom_name"]: atom for atom in left["atoms"]}
        right_atoms = {atom["atom_name"]: atom for atom in right["atoms"]}
        if "C" in left_atoms and "N" in right_atoms:
            distance = float(np.linalg.norm(np.asarray(left_atoms["C"]["xyz"]) - np.asarray(right_atoms["N"]["xyz"])))
            if not (
                config["min_peptide_bond_distance_a"]
                <= distance
                <= config["max_peptide_bond_distance_a"]
            ):
                peptide_breaks.append(f"{left['imgt_position']}->{right['imgt_position']}:{distance:.3f}")
    if sequence_gaps:
        hard.append(f"unresolved_sequence_gap:{','.join(sequence_gaps[:8])}")
    if peptide_breaks:
        hard.append(f"peptide_bond_break:{','.join(peptide_breaks[:8])}")

    by_base = {residue["imgt_base"]: residue for residue in residues if not residue["imgt_insertion"]}
    cys_positions = [residue for residue in residues if residue["comp_id"] == "CYS"]
    disulfides: list[dict[str, Any]] = []
    for index, left in enumerate(cys_positions):
        left_atoms = {atom["atom_name"]: atom for atom in left["atoms"]}
        if "SG" not in left_atoms:
            continue
        for right in cys_positions[index + 1 :]:
            right_atoms = {atom["atom_name"]: atom for atom in right["atoms"]}
            if "SG" not in right_atoms:
                continue
            distance = float(np.linalg.norm(np.asarray(left_atoms["SG"]["xyz"]) - np.asarray(right_atoms["SG"]["xyz"])))
            if distance <= config["canonical_disulfide_distance_a"][1]:
                disulfides.append(
                    {
                        "candidate_id": instance_id,
                        "conn_id": f"disulf{len(disulfides) + 1}",
                        "conn_type": "disulf",
                        "p1_label_seq_id": left["normalized_label_seq_id"],
                        "p1_imgt_position": left["imgt_position"],
                        "p2_label_seq_id": right["normalized_label_seq_id"],
                        "p2_imgt_position": right["imgt_position"],
                        "distance_a": distance,
                        "connection_source": "geometry_reconstructed_from_sabdab2",
                        "retained": True,
                    }
                )

    canonical_pair = None
    if 23 in by_base and 104 in by_base:
        r23, r104 = by_base[23], by_base[104]
        if r23["comp_id"] != "CYS" or r104["comp_id"] != "CYS":
            hard.append("canonical_imgt_23_104_not_cysteine")
        else:
            a23 = {atom["atom_name"]: atom for atom in r23["atoms"]}
            a104 = {atom["atom_name"]: atom for atom in r104["atoms"]}
            if "SG" not in a23 or "SG" not in a104:
                hard.append("canonical_disulfide_sg_missing")
            else:
                canonical_distance = float(np.linalg.norm(np.asarray(a23["SG"]["xyz"]) - np.asarray(a104["SG"]["xyz"])))
                low, high = config["canonical_disulfide_distance_a"]
                if not low <= canonical_distance <= high:
                    hard.append(f"canonical_disulfide_distance_invalid:{canonical_distance:.3f}")
                else:
                    canonical_pair = (r23["imgt_position"], r104["imgt_position"], canonical_distance)
    else:
        hard.append("canonical_disulfide_positions_missing")

    for connection in disulfides:
        pair = {connection["p1_imgt_position"], connection["p2_imgt_position"]}
        if pair != {"23", "104"}:
            involved = [
                residue for residue in residues
                if residue["imgt_position"] in pair and residue["region"].startswith("CDR")
            ]
            if involved:
                hard.append(f"extra_disulfide_involves_design_region:{'|'.join(sorted(pair))}")
            else:
                soft.append(f"extra_framework_disulfide:{'|'.join(sorted(pair))}")

    if any(residue["selected_altloc"] for residue in residues):
        soft.append("altloc_residue_conformer_selected")
    soft.append("struct_conn_reconstructed_from_geometry")

    sequence = "".join(residue["one_letter"] for residue in residues)
    framework_sequence = "".join(residue["one_letter"] for residue in residues if is_framework(residue["region"]))
    # 精确框架去重必须同时编码“IMGT位置”和氨基酸。只哈希无编号字符串会把
    # 插入位置不同、但字符序列碰巧相同的框架错误折叠成同一条记录。
    framework_numbered_sequence = "|".join(
        f"{residue['imgt_position']}:{residue['one_letter']}"
        for residue in residues
        if is_framework(residue["region"])
    )
    cdr_sequences = {
        region: "".join(residue["one_letter"] for residue in residues if residue["region"] == region)
        for region in ("CDR1", "CDR2", "CDR3")
    }
    try:
        analysis = ProteinAnalysis(framework_sequence)
        predicted_pi = float(analysis.isoelectric_point())
        framework_gravy = float(analysis.gravy())
    except Exception:
        predicted_pi = float("nan")
        framework_gravy = float("nan")
        soft.append("developability_sequence_metrics_failed")

    liability_count = sum(
        len(re.findall(pattern, framework_sequence))
        for pattern in (r"N[GST]", r"D[GST]", r"M", r"W")
    )
    if math.isfinite(predicted_pi) and (predicted_pi < 5.5 or predicted_pi > 9.5):
        soft.append(f"framework_predicted_pi_extreme:{predicted_pi:.2f}")
    if math.isfinite(framework_gravy) and framework_gravy > 0.2:
        soft.append(f"framework_gravy_high:{framework_gravy:.3f}")
    if liability_count >= 5:
        soft.append(f"framework_liability_motif_count:{liability_count}")

    # 透明的项目内排序分数。它只比较已经通过硬 QC 的模板，不能解释成成功概率。
    resolution = safe_float(metadata.get("resolution"))
    r_free = safe_float(metadata.get("r_free"))
    resolution_score = 0.0 if resolution is None else float(np.clip((3.0 - resolution) / 2.0, 0.0, 1.0))
    rfree_score = 0.5 if r_free is None else float(np.clip((0.35 - r_free) / 0.20, 0.0, 1.0))
    structure_quality = 0.7 * resolution_score + 0.3 * rfree_score
    framework_integrity = 1.0 if canonical_pair and not incomplete_framework and not sequence_gaps and not peptide_breaks else 0.0
    mapping_confidence = 1.0 if not duplicate_atom_error and not coordinate_error else 0.5
    developability = float(np.clip(1.0 - 0.05 * liability_count - max(0.0, abs(predicted_pi - 7.4) - 2.0) * 0.08, 0.0, 1.0)) if math.isfinite(predicted_pi) else 0.5
    quality_score = 0.35 * structure_quality + 0.30 * framework_integrity + 0.20 * developability + 0.15 * mapping_confidence

    ca_by_imgt: dict[str, np.ndarray] = {}
    anchor_ca: dict[int, np.ndarray] = {}
    for residue in residues:
        atom_map = {atom["atom_name"]: atom for atom in residue["atoms"]}
        if "CA" in atom_map:
            coordinate = np.asarray(atom_map["CA"]["xyz"], dtype=float)
            ca_by_imgt[residue["imgt_position"]] = coordinate
            if residue["imgt_base"] in set(config["required_imgt_anchors"]) and not residue["imgt_insertion"]:
                anchor_ca[residue["imgt_base"]] = coordinate

    hard = sorted(set(hard))
    soft = sorted(set(soft))
    record = {
        "candidate_id": instance_id,
        "instance_id": instance_id,
        "pdb_id": str(metadata["PDB"]),
        # SAbDab2 使用扩展 PDBx 形式（如 pdb_00007xl0）；传统 RCSB 代码是末4位。
        "pdb_code": str(metadata["PDB"])[-4:].upper(),
        "sabdab_id": str(metadata["SABDAB_ID"]),
        "source_model_zero_based": int(float(metadata.get("model", 0))),
        "source_hchain": target_hchain,
        "source_label_asym_id": label_asym,
        "source_auth_asym_id": auth_asym,
        "archive_member": archive_member,
        "sequence": sequence,
        "variable_length_aa": length,
        "framework_sequence": framework_sequence,
        "framework_sha256": hashlib.sha256(framework_numbered_sequence.encode()).hexdigest(),
        "cdr1_sequence": cdr_sequences["CDR1"],
        "cdr2_sequence": cdr_sequences["CDR2"],
        "cdr3_sequence": cdr_sequences["CDR3"],
        "cdr1_length_aa": len(cdr_sequences["CDR1"]),
        "cdr2_length_aa": len(cdr_sequences["CDR2"]),
        "cdr3_length_aa": len(cdr_sequences["CDR3"]),
        "first_imgt_base": min(bases) if bases else None,
        "last_imgt_base": max(bases) if bases else None,
        "framework_backbone_complete_fraction": (
            sum(residue["backbone_complete"] for residue in residues if is_framework(residue["region"]))
            / max(1, sum(is_framework(residue["region"]) for residue in residues))
        ),
        "minimum_residue_mean_occupancy": (
            min(residue["mean_occupancy"] for residue in residues) if residues else None
        ),
        "residues_below_0_7_occupancy": len(hard_low_occupancy) + len(soft_low_occupancy),
        "canonical_ss_distance_a": canonical_pair[2] if canonical_pair else None,
        "extra_disulfide_count": max(0, len(disulfides) - int(canonical_pair is not None)),
        "predicted_framework_pi": predicted_pi if math.isfinite(predicted_pi) else None,
        "framework_gravy": framework_gravy if math.isfinite(framework_gravy) else None,
        "framework_liability_count": liability_count,
        "resolution_a": resolution,
        "r_free": r_free,
        "quality_score": quality_score,
        "hard_status": "PASS" if not hard else "FAIL",
        "hard_reason_count": len(hard),
        "hard_reasons": " | ".join(hard),
        "soft_flag_count": len(soft),
        "soft_flags": " | ".join(soft),
        "original_antigen_provenance_only": clean_text(metadata.get("antigen_name")),
    }
    return CandidateProfile(record, residues, ca_by_imgt, anchor_ca, disulfides, hard, soft)


def kabsch_metrics(left: CandidateProfile, right: CandidateProfile, config: dict[str, Any]) -> tuple[float, float, float, int]:
    """返回框架 identity、框架 Cα RMSD、锚点 RMSD 和共同位置数。"""

    left_res = {res["imgt_position"]: res for res in left.residues if is_framework(res["region"])}
    right_res = {res["imgt_position"]: res for res in right.residues if is_framework(res["region"])}
    common = sorted(set(left_res) & set(right_res), key=lambda key: (int(re.match(r"\d+", key).group()), key))
    common = [key for key in common if key in left.ca_by_imgt and key in right.ca_by_imgt]
    if len(common) < config["clustering"]["minimum_common_framework_positions"]:
        return 0.0, float("inf"), float("inf"), len(common)

    # 共同位置用于坐标叠合；序列 identity 则以 framework 位置并集为分母，
    # 让只在一侧出现的 IMGT 插入/缺口计为不匹配，而不是被忽略后虚高。
    framework_union = set(left_res) | set(right_res)
    identity = (
        sum(left_res[key]["one_letter"] == right_res[key]["one_letter"] for key in common)
        / len(framework_union)
    )
    mobile = np.vstack([left.ca_by_imgt[key] for key in common])
    fixed = np.vstack([right.ca_by_imgt[key] for key in common])
    mobile_center = mobile.mean(axis=0)
    fixed_center = fixed.mean(axis=0)
    covariance = (mobile - mobile_center).T @ (fixed - fixed_center)
    u, _, vt = np.linalg.svd(covariance)

    # 这里的每个坐标点按“行向量”存放，因此最小化
    # ``||(mobile-mobile_center) @ R - (fixed-fixed_center)||`` 的 Kabsch
    # 旋转应为 U @ Vt。若照列向量公式写成 V @ Ut，RMSD 会错误依赖
    # 两个 mmCIF 在全局坐标系中的朝向，进而污染框架聚类结果。
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1
        rotation = u @ vt
    aligned = (mobile - mobile_center) @ rotation + fixed_center
    framework_rmsd = float(np.sqrt(np.mean(np.sum((aligned - fixed) ** 2, axis=1))))

    anchors = [base for base in config["required_imgt_anchors"] if base in left.anchor_ca and base in right.anchor_ca]
    if len(anchors) != len(config["required_imgt_anchors"]):
        anchor_rmsd = float("inf")
    else:
        left_anchor = np.vstack([left.anchor_ca[base] for base in anchors])
        right_anchor = np.vstack([right.anchor_ca[base] for base in anchors])
        left_anchor_aligned = (left_anchor - mobile_center) @ rotation + fixed_center
        anchor_rmsd = float(np.sqrt(np.mean(np.sum((left_anchor_aligned - right_anchor) ** 2, axis=1))))
    return identity, framework_rmsd, anchor_rmsd, len(common)


def normalized_pair_distance(metrics: tuple[float, float, float, int], config: dict[str, Any]) -> float:
    identity, framework_rmsd, anchor_rmsd, _ = metrics
    if not math.isfinite(framework_rmsd) or not math.isfinite(anchor_rmsd):
        return 999.0
    cluster = config["clustering"]
    return max(
        (1.0 - identity) / cluster["framework_identity_scale"],
        framework_rmsd / cluster["framework_ca_rmsd_scale_a"],
        anchor_rmsd / cluster["anchor_rmsd_scale_a"],
    )


def compress_indices(values: Iterable[int]) -> str:
    """把 [1,2,3,7] 压缩成 BoltzGen 接受的 ``1..3,7``。"""

    ordered = sorted(set(int(value) for value in values))
    if not ordered:
        return ""
    chunks: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        chunks.append(str(start) if start == previous else f"{start}..{previous}")
        start = previous = value
    chunks.append(str(start) if start == previous else f"{start}..{previous}")
    return ",".join(chunks)


def build_normalized_structure(profile: CandidateProfile, output: Path) -> None:
    """写出只含一个 VHH variable domain 且保留二硫键的 mmCIF。"""

    structure = gemmi.Structure()
    structure.name = profile.record["candidate_id"].replace("-", "_")
    model = gemmi.Model(1)
    chain = gemmi.Chain("A")

    for residue_data in profile.residues:
        residue = gemmi.Residue()
        residue.name = residue_data["comp_id"]
        insertion = residue_data["imgt_insertion"] or " "
        residue.seqid = gemmi.SeqId(int(residue_data["imgt_base"]), insertion)
        residue.label_seq = int(residue_data["normalized_label_seq_id"])
        residue.entity_id = "1"
        residue.entity_type = gemmi.EntityType.Polymer
        residue.subchain = "A"
        residue.het_flag = "A"
        for atom_data in residue_data["atoms"]:
            if atom_data["element"] == "H":
                continue
            atom = gemmi.Atom()
            atom.name = atom_data["atom_name"]
            atom.element = gemmi.Element(atom_data["element"])
            atom.pos = gemmi.Position(*map(float, atom_data["xyz"]))
            atom.occ = float(atom_data["occupancy"])
            atom.b_iso = float(atom_data["b_factor"])
            atom.altloc = "\0"
            residue.add_atom(atom)
        chain.add_residue(residue)

    # Gemmi 的 add_chain/add_model 会复制传入对象；因此必须等链构建完成后再加入，
    # 否则后续写入原 ``chain`` 变量的残基不会出现在 Structure 内。
    model.add_chain(chain)
    structure.add_model(model)

    entity = gemmi.Entity("1")
    entity.entity_type = gemmi.EntityType.Polymer
    entity.polymer_type = gemmi.PolymerType.PeptideL
    entity.full_sequence = [residue["comp_id"] for residue in profile.residues]
    entity.subchains = ["A"]
    structure.entities.append(entity)

    by_norm = {residue["normalized_label_seq_id"]: residue for residue in profile.residues}
    for index, connection_data in enumerate(profile.disulfides, start=1):
        left = by_norm[connection_data["p1_label_seq_id"]]
        right = by_norm[connection_data["p2_label_seq_id"]]
        connection = gemmi.Connection()
        connection.name = f"disulf{index}"
        connection.type = gemmi.ConnectionType.Disulf
        connection.asu = gemmi.Asu.Same
        connection.reported_distance = float(connection_data["distance_a"])
        connection.partner1 = gemmi.AtomAddress(
            "A", gemmi.SeqId(int(left["imgt_base"]), left["imgt_insertion"] or " "), "CYS", "SG"
        )
        connection.partner2 = gemmi.AtomAddress(
            "A", gemmi.SeqId(int(right["imgt_base"]), right["imgt_insertion"] or " "), "CYS", "SG"
        )
        structure.connections.append(connection)

    output.parent.mkdir(parents=True, exist_ok=True)
    structure.make_mmcif_document().write_file(str(output))


def crosscheck_rcsb_disulfide(raw_cif: Path, profile: CandidateProfile, tolerance_a: float = 0.25) -> bool:
    """用原始 RCSB ``_struct_conn`` 的 SG 坐标交叉核对规范二硫键。

    RCSB author residue 编号与 SAbDab2 IMGT 编号通常不同，因此不能直接比较
    22/95 与 23/104。这里比较连接两端 SG 的实验坐标，避免编号转换误判。
    """

    try:
        block = gemmi.cif.read_file(str(raw_cif)).sole_block()
        conn, conn_cols = cif_table(block, "_struct_conn.")
        atoms, atom_cols = cif_table(block, "_atom_site.")
    except Exception:
        return False

    canonical = [conn for conn in profile.disulfides if {conn["p1_imgt_position"], conn["p2_imgt_position"]} == {"23", "104"}]
    if not canonical:
        return False
    by_norm = {residue["normalized_label_seq_id"]: residue for residue in profile.residues}
    target_coords = []
    for norm in (canonical[0]["p1_label_seq_id"], canonical[0]["p2_label_seq_id"]):
        residue = by_norm[norm]
        sg = next(atom for atom in residue["atoms"] if atom["atom_name"] == "SG")
        target_coords.append(np.asarray(sg["xyz"], dtype=float))

    # 原始 PDBx/mmCIF 的 ``_struct_conn`` 有时只填 label 编号，有时只填
    # author 编号；altloc 还可能让同一端点出现多组 SG 坐标。因此分别建立
    # 两套多值索引，不能要求四个字段同时精确相等、也不能静默覆盖 altloc。
    label_lookup: dict[tuple[str, str], list[np.ndarray]] = defaultdict(list)
    auth_lookup: dict[tuple[str, str], list[np.ndarray]] = defaultdict(list)
    for row in atoms:
        if row_value(row, atom_cols, "label_atom_id").strip() != "SG":
            continue
        coordinate = np.asarray(
            [float(row_value(row, atom_cols, axis)) for axis in ("Cartn_x", "Cartn_y", "Cartn_z")],
            dtype=float,
        )
        label_key = (
            clean_text(row_value(row, atom_cols, "label_asym_id")),
            clean_text(row_value(row, atom_cols, "label_seq_id")),
        )
        auth_key = (
            clean_text(row_value(row, atom_cols, "auth_asym_id")),
            clean_text(row_value(row, atom_cols, "auth_seq_id")),
        )
        if all(label_key):
            label_lookup[label_key].append(coordinate)
        if all(auth_key):
            auth_lookup[auth_key].append(coordinate)

    for row in conn:
        if clean_text(row_value(row, conn_cols, "conn_type_id")).lower() != "disulf":
            continue
        endpoint_options: list[list[np.ndarray]] = []
        for side in ("ptnr1", "ptnr2"):
            label_key = (
                clean_text(row_value(row, conn_cols, f"{side}_label_asym_id")),
                clean_text(row_value(row, conn_cols, f"{side}_label_seq_id")),
            )
            auth_key = (
                clean_text(row_value(row, conn_cols, f"{side}_auth_asym_id")),
                clean_text(row_value(row, conn_cols, f"{side}_auth_seq_id")),
            )
            options: list[np.ndarray] = []
            if all(label_key):
                options.extend(label_lookup.get(label_key, []))
            if all(auth_key):
                options.extend(auth_lookup.get(auth_key, []))
            endpoint_options.append(options)
        if not endpoint_options[0] or not endpoint_options[1]:
            continue
        for first in endpoint_options[0]:
            for second in endpoint_options[1]:
                direct = np.linalg.norm(first - target_coords[0]) + np.linalg.norm(second - target_coords[1])
                swapped = np.linalg.norm(first - target_coords[1]) + np.linalg.norm(second - target_coords[0])
                if min(direct, swapped) <= 2 * tolerance_a:
                    return True
    return False


def write_tsv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    if columns:
        for column in columns:
            if column not in frame:
                frame[column] = None
        frame = frame[columns]
    frame.to_csv(path, sep="\t", index=False, quoting=csv.QUOTE_MINIMAL)


def write_sqlite(database: Path, tables: dict[str, pd.DataFrame], metadata: dict[str, Any]) -> None:
    """把审计表写入 SQLite，并显式建立可复核的唯一键与查询索引。"""

    if database.exists():
        database.unlink()
    with sqlite3.connect(database) as connection:
        for name, frame in tables.items():
            frame.to_sql(name, connection, index=False, if_exists="replace")
        pd.DataFrame([{"key": key, "value_json": json.dumps(value, ensure_ascii=False)} for key, value in metadata.items()]).to_sql(
            "database_metadata", connection, index=False, if_exists="replace"
        )
        connection.executescript(
            """
            CREATE UNIQUE INDEX idx_instance_pk ON antibody_instance(INSTANCE);
            CREATE INDEX idx_instance_sabdab ON antibody_instance(SABDAB_ID);
            CREATE UNIQUE INDEX idx_candidate_pk ON scaffold_candidate(candidate_id);
            CREATE INDEX idx_candidate_status ON scaffold_candidate(hard_status);
            CREATE UNIQUE INDEX idx_residue_pk ON residue_map(candidate_id, ordinal);
            CREATE UNIQUE INDEX idx_connection_pk ON structure_connection(candidate_id, conn_id);
            CREATE UNIQUE INDEX idx_qc_pk ON qc_result(candidate_id, rule_id, severity, status, detail);
            CREATE UNIQUE INDEX idx_cluster_member_pk ON cluster_member(candidate_id);
            CREATE UNIQUE INDEX idx_selection_rank ON selection_member(selection_rank);
            CREATE UNIQUE INDEX idx_selection_candidate ON selection_member(candidate_id);
            CREATE UNIQUE INDEX idx_export_candidate ON export_artifact(candidate_id);
            CREATE UNIQUE INDEX idx_exclusion_instance ON exclusion_log(instance_id);
            CREATE UNIQUE INDEX idx_funnel_order ON screening_funnel(stage_order);
            CREATE UNIQUE INDEX idx_exclusion_reason_order ON exclusion_reason_summary(reason_order);
            CREATE UNIQUE INDEX idx_database_metadata_key ON database_metadata(key);
            """
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--skip-rcsb-crosscheck", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    config_path = args.config or root / "criteria" / "scaffold_screening_v1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    summary_path = root / "raw_snapshot" / "sabdab_summary_all_sd_h.csv"
    archive_path = root / "raw_snapshot" / "sabdab_all_sd_h_structures.tgz"
    if not summary_path.is_file() or not archive_path.is_file():
        raise SystemExit("缺少冻结的 summary CSV 或 structure tgz")

    # 原始数据先做文件级验证，防止后续把半截下载误当数据质量问题。
    with tarfile.open(archive_path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile() and member.name.endswith("_sabdab.cif")]
    if not members:
        raise RuntimeError("结构归档没有 *_sabdab.cif")

    raw = pd.read_csv(summary_path, dtype=str, keep_default_na=False)
    required_csv = {
        "INSTANCE", "PDB", "SABDAB_ID", "Hchain", "model", "method", "resolution",
        "r_free", "r_factor", "type", "heavy_species", "heavy_taxid",
        "chainsharing_construct", "non_chainsharing_construct", "antigen_name",
    }
    missing_csv = sorted(required_csv - set(raw.columns))
    if missing_csv:
        raise RuntimeError(f"summary CSV 缺列：{missing_csv}")
    if raw["INSTANCE"].duplicated().any():
        duplicates = raw.loc[raw["INSTANCE"].duplicated(False), "INSTANCE"].tolist()[:10]
        raise RuntimeError(f"INSTANCE 不唯一：{duplicates}")

    raw["resolution_a"] = pd.to_numeric(raw["resolution"], errors="coerce")
    raw["r_free_num"] = pd.to_numeric(raw["r_free"], errors="coerce")
    species_pattern = "|".join(re.escape(value) for value in config["scope"]["primary_vhh_provenance_patterns"])
    raw["is_camelid_vhh_scope"] = raw["heavy_species"].str.contains(species_pattern, case=False, regex=True, na=False)
    raw["is_primary_method"] = raw["method"].eq(config["scope"]["primary_method"])
    raw["is_primary_resolution"] = raw["resolution_a"].le(config["scope"]["max_primary_resolution_a"])
    raw["is_primary_rfree"] = raw["r_free_num"].isna() | raw["r_free_num"].le(config["scope"]["max_primary_r_free"])
    raw["is_unshared_construct"] = raw["chainsharing_construct"].map(clean_text).eq("") & raw["non_chainsharing_construct"].map(clean_text).eq("")

    primary_mask = (
        raw["type"].eq("SD-H")
        & raw["is_camelid_vhh_scope"]
        & raw["is_primary_method"]
        & raw["is_primary_resolution"]
        & raw["is_primary_rfree"]
        & raw["is_unshared_construct"]
    )
    primary = raw.loc[primary_mask].copy()
    primary_by_pdb: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in primary.to_dict("records"):
        primary_by_pdb[row["PDB"]].append(row)

    member_by_pdb: dict[str, tarfile.TarInfo] = {}
    for member in members:
        match = re.search(r"(pdb_[A-Za-z0-9]{8})_sabdab\.cif$", member.name)
        if match:
            member_by_pdb[match.group(1)] = member

    profiles: dict[str, CandidateProfile] = {}
    residue_rows: list[dict[str, Any]] = []
    connection_rows: list[dict[str, Any]] = []
    qc_rows: list[dict[str, Any]] = []
    missing_pdbs = sorted(set(primary_by_pdb) - set(member_by_pdb))
    if missing_pdbs:
        raise RuntimeError(f"归档缺少 {len(missing_pdbs)} 个 metadata-qualified PDB，例如 {missing_pdbs[:5]}")

    with tarfile.open(archive_path, "r:gz") as archive:
        needed_members = {member_by_pdb[pdb].name: pdb for pdb in primary_by_pdb}
        for member in archive:
            pdb_id = needed_members.get(member.name)
            if not pdb_id:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            text = handle.read().decode("utf-8")
            block = gemmi.cif.read_string(text).sole_block()
            for metadata in primary_by_pdb[pdb_id]:
                profile = extract_profile(block, metadata, config, member.name)
                profiles[profile.record["candidate_id"]] = profile
                for residue in profile.residues:
                    residue_rows.append({key: value for key, value in residue.items() if key != "atoms"})
                connection_rows.extend(profile.disulfides)
                if profile.hard_reasons:
                    for reason in profile.hard_reasons:
                        qc_rows.append({"candidate_id": profile.record["candidate_id"], "rule_id": reason.split(":", 1)[0], "severity": "HARD", "status": "FAIL", "detail": reason})
                else:
                    qc_rows.append({"candidate_id": profile.record["candidate_id"], "rule_id": "all_hard_rules", "severity": "HARD", "status": "PASS", "detail": ""})
                for flag in profile.soft_flags:
                    qc_rows.append({"candidate_id": profile.record["candidate_id"], "rule_id": flag.split(":", 1)[0], "severity": "SOFT", "status": "FLAG", "detail": flag})

    candidate_frame = pd.DataFrame([profile.record for profile in profiles.values()])
    pass_profiles = [profile for profile in profiles.values() if profile.record["hard_status"] == "PASS"]
    if not pass_profiles:
        raise RuntimeError("没有结构通过硬 QC；不能继续聚类或凑数")

    # 同一 SAbDab ID 可有多个坐标实例。先按确定性质量顺序选最佳实例。
    pass_profiles.sort(
        key=lambda profile: (
            profile.record["sabdab_id"],
            profile.record["soft_flag_count"],
            -profile.record["quality_score"],
            profile.record["resolution_a"] if profile.record["resolution_a"] is not None else 999.0,
            profile.record["candidate_id"],
        )
    )
    best_by_sabdab: dict[str, CandidateProfile] = {}
    for profile in pass_profiles:
        best_by_sabdab.setdefault(profile.record["sabdab_id"], profile)
    unique_antibodies = list(best_by_sabdab.values())

    # 完全相同 framework sequence 只保留质量更高的一个实例。
    unique_antibodies.sort(
        key=lambda profile: (
            profile.record["soft_flag_count"],
            -profile.record["quality_score"],
            profile.record["candidate_id"],
        )
    )
    best_by_framework: dict[str, CandidateProfile] = {}
    for profile in unique_antibodies:
        best_by_framework.setdefault(profile.record["framework_sha256"], profile)
    representatives = list(best_by_framework.values())
    representatives.sort(key=lambda profile: profile.record["candidate_id"])

    size = len(representatives)
    distance_matrix = np.zeros((size, size), dtype=float)
    pair_metrics: dict[tuple[int, int], tuple[float, float, float, int]] = {}
    for left_index in range(size):
        for right_index in range(left_index + 1, size):
            metrics = kabsch_metrics(representatives[left_index], representatives[right_index], config)
            pair_metrics[(left_index, right_index)] = metrics
            distance = normalized_pair_distance(metrics, config)
            distance_matrix[left_index, right_index] = distance_matrix[right_index, left_index] = distance

    if size == 1:
        labels = np.zeros(1, dtype=int)
    else:
        clusterer = AgglomerativeClustering(
            n_clusters=None,
            metric="precomputed",
            linkage="complete",
            distance_threshold=config["clustering"]["complete_linkage_distance_threshold"],
            compute_full_tree=True,
        )
        labels = clusterer.fit_predict(distance_matrix)

    raw_clusters: dict[int, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        raw_clusters[int(label)].append(index)
    ordered_clusters = sorted(raw_clusters.values(), key=lambda members_: min(representatives[index].record["candidate_id"] for index in members_))
    cluster_id_by_index: dict[int, str] = {}
    cluster_rows: list[dict[str, Any]] = []
    cluster_reps: list[CandidateProfile] = []
    for ordinal, members_ in enumerate(ordered_clusters, start=1):
        cluster_id = f"FWC{ordinal:04d}"
        # 簇代表通常以软风险更少、质量更高者优先。若本簇包含当前 MVP 已用的
        # 7XL0-A 且政策要求保留，则在这里就把它设为代表；若等到簇后再查找，
        # 它可能早已被同簇另一个成员替换，导致“retain when eligible”形同虚设。
        members_sorted = sorted(
            members_,
            key=lambda index: (
                representatives[index].record["soft_flag_count"],
                -representatives[index].record["quality_score"],
                representatives[index].record["candidate_id"],
            ),
        )
        benchmark_indices = [
            index for index in members_
            if representatives[index].record["pdb_code"] == "7XL0"
            and representatives[index].record["source_hchain"] == "A"
        ]
        benchmark_override = bool(
            benchmark_indices
            and config["selection"]["retain_current_7xl0_benchmark_when_eligible"]
        )
        rep_index = benchmark_indices[0] if benchmark_override else members_sorted[0]
        cluster_reps.append(representatives[rep_index])
        for index in members_:
            cluster_id_by_index[index] = cluster_id
            metrics = (1.0, 0.0, 0.0, len(representatives[index].ca_by_imgt)) if index == rep_index else pair_metrics.get((min(index, rep_index), max(index, rep_index)))
            cluster_rows.append(
                {
                    "cluster_id": cluster_id,
                    "candidate_id": representatives[index].record["candidate_id"],
                    "is_cluster_representative": index == rep_index,
                    "cluster_representative_reason": (
                        "benchmark_7xl0_override" if benchmark_override else "quality_and_soft_flags"
                    ),
                    "identity_to_representative": metrics[0] if metrics else None,
                    "framework_rmsd_to_representative_a": metrics[1] if metrics else None,
                    "anchor_rmsd_to_representative_a": metrics[2] if metrics else None,
                    "cluster_size": len(members_),
                }
            )

    # 选择 10 primary + 2 reserve。7XL0 若通过，会作为当前 MVP 的明确 benchmark
    # 被保留；这是一项项目连续性约束，不伪装成质量排名。
    desired = config["selection"]["primary_count"] + config["selection"]["reserve_count"]
    selected: list[CandidateProfile] = []
    benchmark = next((profile for profile in cluster_reps if profile.record["pdb_code"] == "7XL0" and profile.record["source_hchain"] == "A"), None)
    if benchmark and config["selection"]["retain_current_7xl0_benchmark_when_eligible"]:
        selected.append(benchmark)
    if not selected:
        selected.append(
            max(
                cluster_reps,
                key=lambda profile: (
                    profile.record["quality_score"],
                    -profile.record["soft_flag_count"],
                    profile.record["candidate_id"],
                ),
            )
        )

    def pair_to_selected(candidate: CandidateProfile, chosen: CandidateProfile) -> tuple[float, float]:
        metrics = kabsch_metrics(candidate, chosen, config)
        sequence_distance = 1.0 - metrics[0]
        anchor_distance = 1.0 if not math.isfinite(metrics[2]) else min(1.0, metrics[2] / 2.0)
        return sequence_distance, anchor_distance

    selection_utilities: dict[str, dict[str, float]] = {}
    while len(selected) < min(desired, len(cluster_reps)):
        best_candidate = None
        best_tuple = None
        for candidate in cluster_reps:
            if candidate in selected:
                continue
            distances = [pair_to_selected(candidate, chosen) for chosen in selected]
            min_seq = min(item[0] for item in distances)
            min_anchor = min(item[1] for item in distances)
            utility = (
                config["selection"]["quality_weight"] * candidate.record["quality_score"]
                + config["selection"]["framework_sequence_diversity_weight"] * min_seq
                + config["selection"]["anchor_geometry_diversity_weight"] * min_anchor
            )
            key = (utility, candidate.record["quality_score"], candidate.record["candidate_id"])
            if best_tuple is None or key > best_tuple:
                best_tuple = key
                best_candidate = candidate
                selection_utilities[candidate.record["candidate_id"]] = {
                    "utility": utility,
                    "min_framework_sequence_distance": min_seq,
                    "min_anchor_geometry_distance_scaled": min_anchor,
                }
        if best_candidate is None:
            break
        selected.append(best_candidate)

    selected_rows: list[dict[str, Any]] = []
    export_rows: list[dict[str, Any]] = []
    metadata_by_instance = {row["INSTANCE"]: row for row in primary.to_dict("records")}
    session = requests.Session()
    session.headers["User-Agent"] = "GLP1-scaffold-research/1.0"
    selected_ids = {profile.record["candidate_id"] for profile in selected}

    # ``selected`` 是完全由本脚本生成、可重建的目录。每轮选择集合可能变化；
    # 若只覆盖同名 package，上一轮不再入选的目录会残留并被 SHA 清单误收。
    # 这里在路径安全断言后整体重建，使磁盘目录与 selection_member 严格一一对应。
    selected_root = root / "selected"
    if selected_root.parent != root or selected_root.name != "selected":
        raise RuntimeError(f"拒绝清理非预期生成目录：{selected_root}")
    if selected_root.exists():
        shutil.rmtree(selected_root)
    selected_root.mkdir(parents=True)

    # 第二次流式读取只重新提取最终少量入选结构，用于写出含原子坐标的 scaffold 包。
    selected_by_pdb: dict[str, list[CandidateProfile]] = defaultdict(list)
    for profile in selected:
        selected_by_pdb[profile.record["pdb_id"]].append(profile)
    with tarfile.open(archive_path, "r:gz") as archive:
        needed = {member_by_pdb[pdb].name: pdb for pdb in selected_by_pdb}
        for member in archive:
            pdb_id = needed.get(member.name)
            if not pdb_id:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            block = gemmi.cif.read_string(handle.read().decode("utf-8")).sole_block()
            for old_profile in selected_by_pdb[pdb_id]:
                # 重新提取原子，随后断言摘要完全一致，防止两次遍历漂移。
                profile = extract_profile(block, metadata_by_instance[old_profile.record["candidate_id"]], config, member.name)
                if profile.record["sequence"] != old_profile.record["sequence"]:
                    raise RuntimeError(f"二次提取序列漂移：{profile.record['candidate_id']}")
                old_profile.residues = profile.residues
                old_profile.disulfides = profile.disulfides

    for rank, profile in enumerate(selected, start=1):
        candidate_id = profile.record["candidate_id"]
        package_dir = selected_root / f"{rank:02d}_{candidate_id}"
        package_dir.mkdir(parents=True)
        cif_path = package_dir / "scaffold.cif"
        yaml_path = package_dir / "scaffold.yaml"
        map_path = package_dir / "residue_mapping.tsv"
        qc_path = package_dir / "qc.json"
        curation_path = package_dir / "curation.json"
        build_normalized_structure(profile, cif_path)

        design_indices = [
            residue["normalized_label_seq_id"] for residue in profile.residues
            if residue["region"] in {"CDR1", "CDR2", "CDR3"}
        ]
        design_spec = compress_indices(design_indices)
        scaffold_yaml = {
            "path": "scaffold.cif",
            "include": [{"chain": {"id": "A"}}],
            "design": [{"chain": {"id": "A", "res_index": design_spec}}],
            "structure_groups": [
                {"group": {"id": "A", "visibility": 2}},
                {"group": {"id": "A", "visibility": 0, "res_index": design_spec}},
            ],
            "reset_res_index": [{"chain": {"id": "A"}}],
        }
        yaml_path.write_text(
            "# 固定 CDR 长度的 BoltzGen VHH scaffold。\n"
            "# 编号来自本文件 scaffold.cif 的 1-based label_seq_id，不是 IMGT/auth 编号。\n"
            + yaml.safe_dump(scaffold_yaml, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        map_rows = [{key: value for key, value in residue.items() if key != "atoms"} for residue in profile.residues]
        write_tsv(map_path, map_rows)

        rcsb_path = package_dir / "source_rcsb_original.cif"
        rcsb_url = f"https://files.rcsb.org/download/{profile.record['pdb_code']}.cif"
        rcsb_crosschecked = False
        rcsb_error = ""
        if not args.skip_rcsb_crosscheck:
            try:
                response = session.get(rcsb_url, timeout=120)
                response.raise_for_status()
                rcsb_path.write_bytes(response.content)
                rcsb_crosschecked = crosscheck_rcsb_disulfide(rcsb_path, profile)
                time.sleep(0.15)
            except Exception as exc:
                rcsb_error = f"{type(exc).__name__}: {exc}"

        for connection in profile.disulfides:
            if {connection["p1_imgt_position"], connection["p2_imgt_position"]} == {"23", "104"} and rcsb_crosschecked:
                connection["connection_source"] = "geometry_reconstructed_and_rcsb_struct_conn_crosschecked"

        qc_payload = {
            "schema_version": "1.0.0",
            "candidate_id": candidate_id,
            "hard_status": profile.record["hard_status"],
            "hard_reasons": profile.hard_reasons,
            "soft_flags": profile.soft_flags,
            "canonical_disulfide_rcsb_crosschecked": rcsb_crosschecked,
            "rcsb_crosscheck_error": rcsb_error,
            "boltzgen_check_status": "PENDING",
            "interpretation": "结构模板 QC 通过不等于 GLP-1 结合或选择性通过。",
        }
        qc_path.write_text(json.dumps(qc_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        curation_payload = {
            "schema_version": "1.0.0",
            "candidate_id": candidate_id,
            "source": {
                "sabdab2_archive_member": profile.record["archive_member"],
                "sabdab2_instance": candidate_id,
                "pdb_code": profile.record["pdb_code"],
                "source_hchain": profile.record["source_hchain"],
                "source_label_asym_id": profile.record["source_label_asym_id"],
                "source_auth_asym_id": profile.record["source_auth_asym_id"],
            },
            "transformations": [
                "选择 CSV 指定的模型和 SD-H 重链",
                "只保留 IMGT 1..128 的 VHH variable domain",
                "按残基选择单一 altloc，移除氢、抗原、水、离子、配体与标签",
                "重新建立 label_asym_id=A 和连续 label_seq_id=1..N",
                "依据 SG 几何重建二硫键；最终代表另用 RCSB 原始 _struct_conn 交叉核对",
            ],
            "sequence": profile.record["sequence"],
            "framework_sequence": profile.record["framework_sequence"],
            "cdr_sequences": {
                "CDR1": profile.record["cdr1_sequence"],
                "CDR2": profile.record["cdr2_sequence"],
                "CDR3": profile.record["cdr3_sequence"],
            },
            "design_res_index": design_spec,
            "connections": profile.disulfides,
        }
        curation_path.write_text(json.dumps(curation_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        role = "PRIMARY" if rank <= config["selection"]["primary_count"] else "RESERVE"
        utility = selection_utilities.get(candidate_id, {"utility": profile.record["quality_score"], "min_framework_sequence_distance": None, "min_anchor_geometry_distance_scaled": None})
        cluster_id = next(row["cluster_id"] for row in cluster_rows if row["candidate_id"] == candidate_id)
        selected_row = {
            "selection_rank": rank,
            "role": role,
            "candidate_id": candidate_id,
            "pdb_code": profile.record["pdb_code"],
            "sabdab_id": profile.record["sabdab_id"],
            "source_hchain": profile.record["source_hchain"],
            "heavy_species": metadata_by_instance.get(candidate_id, {}).get("heavy_species", ""),
            "method": metadata_by_instance.get(candidate_id, {}).get("method", ""),
            "resolution_a": profile.record["resolution_a"],
            "r_free": profile.record["r_free"],
            "variable_length_aa": profile.record["variable_length_aa"],
            "cdr1_length_aa": profile.record["cdr1_length_aa"],
            "cdr2_length_aa": profile.record["cdr2_length_aa"],
            "cdr3_length_aa": profile.record["cdr3_length_aa"],
            "quality_score": profile.record["quality_score"],
            "selection_utility": utility["utility"],
            "min_framework_sequence_distance": utility["min_framework_sequence_distance"],
            "min_anchor_geometry_distance_scaled": utility["min_anchor_geometry_distance_scaled"],
            "framework_cluster_id": cluster_id,
            "soft_flag_count": profile.record["soft_flag_count"],
            "canonical_disulfide_rcsb_crosschecked": rcsb_crosschecked,
            "benchmark_7xl0": profile.record["pdb_code"] == "7XL0" and profile.record["source_hchain"] == "A",
            "package_path": str(package_dir.relative_to(root)),
            "selection_interpretation": "结构质量与框架多样性代表；未证明 GLP-1 结合或选择性",
        }
        selected_rows.append(selected_row)
        export_rows.append(
            {
                "candidate_id": candidate_id,
                "normalized_cif_path": str(cif_path.relative_to(root)),
                "normalized_cif_sha256": sha256_file(cif_path),
                "scaffold_yaml_path": str(yaml_path.relative_to(root)),
                "scaffold_yaml_sha256": sha256_file(yaml_path),
                "residue_mapping_path": str(map_path.relative_to(root)),
                "residue_mapping_sha256": sha256_file(map_path),
                "curation_json_path": str(curation_path.relative_to(root)),
                "qc_json_path": str(qc_path.relative_to(root)),
                "boltzgen_version": "0.3.2",
                "boltzgen_check_status": "PENDING",
            }
        )

    # 给所有原始行分配一个互斥的“首个未进入下一阶段原因”，用于对账图表。
    structural_status = candidate_frame.set_index("candidate_id")["hard_status"].to_dict()
    best_ids = {profile.record["candidate_id"] for profile in unique_antibodies}
    exact_ids = {profile.record["candidate_id"] for profile in representatives}
    cluster_rep_ids = {profile.record["candidate_id"] for profile in cluster_reps}
    exclusion_rows: list[dict[str, Any]] = []
    for metadata in raw.to_dict("records"):
        instance_id = metadata["INSTANCE"]
        if metadata["type"] != "SD-H":
            reason = "类型不是 SD-H"
        elif not bool(metadata["is_camelid_vhh_scope"]):
            reason = "不在 camelid-origin VHH 主面板范围"
        elif not bool(metadata["is_primary_method"]):
            reason = "首轮只采用 X-ray 结构"
        elif not bool(metadata["is_primary_resolution"]):
            reason = "分辨率缺失或高于 2.5 Å"
        elif not bool(metadata["is_primary_rfree"]):
            reason = "Rfree 高于 0.30"
        elif not bool(metadata["is_unshared_construct"]):
            reason = "链共享/融合构建体不进入首轮"
        elif structural_status.get(instance_id) != "PASS":
            reason = "结构/编号/二硫键硬 QC 未通过"
        elif instance_id not in best_ids:
            reason = "同一 SAbDab ID 有质量更优实例"
        elif instance_id not in exact_ids:
            reason = "完全相同 framework 已有质量更优代表"
        elif instance_id not in cluster_rep_ids:
            reason = "同一 framework 结构簇已有代表"
        elif instance_id not in selected_ids:
            reason = "通过但未进入 12 个多样化代表面板"
        else:
            reason = "SELECTED"
        exclusion_rows.append({"instance_id": instance_id, "first_exclusion_reason": reason})

    funnel = [
        (1, "SAbDab2 SD-H antibody instances", len(raw)),
        (2, "camelid-origin VHH scope", int(raw["is_camelid_vhh_scope"].sum())),
        (3, "X-ray", int((raw["is_camelid_vhh_scope"] & raw["is_primary_method"]).sum())),
        (4, "resolution ≤2.5 Å", int((raw["is_camelid_vhh_scope"] & raw["is_primary_method"] & raw["is_primary_resolution"]).sum())),
        (5, "metadata-qualified for structure QC", len(primary)),
        (6, "hard structure QC pass", len(pass_profiles)),
        (7, "best instance per SAbDab ID", len(unique_antibodies)),
        (8, "unique exact framework", len(representatives)),
        (9, "framework cluster representatives", len(cluster_reps)),
        (10, "selected primary + reserve", len(selected_rows)),
    ]
    funnel_rows = [{"stage_order": order, "stage": stage, "remaining_count": count} for order, stage, count in funnel]
    reason_counts = Counter(row["first_exclusion_reason"] for row in exclusion_rows if row["first_exclusion_reason"] != "SELECTED")
    exclusion_summary = [
        {"reason_order": index, "reason": reason, "excluded_count": count}
        for index, (reason, count) in enumerate(reason_counts.most_common(), start=1)
    ]

    # 入选项经过 RCSB 交叉核对后，连接来源字段可能从“仅几何重建”升级；
    # 因此此处从当前 profile 对象重新汇总，而不是沿用第一次遍历的旧副本。
    connection_rows = [connection for profile in profiles.values() for connection in profile.disulfides]

    # 保存审计表。
    registry = root / "registry"
    qc_dir = root / "qc"
    write_tsv(registry / "antibody_instances.tsv", raw.to_dict("records"))
    write_tsv(registry / "scaffold_candidates.tsv", candidate_frame.to_dict("records"))
    write_tsv(registry / "residue_map.tsv", residue_rows)
    write_tsv(registry / "structure_connections.tsv", connection_rows)
    write_tsv(registry / "framework_clusters.tsv", cluster_rows)
    write_tsv(registry / "selected_scaffolds.tsv", selected_rows)
    write_tsv(registry / "export_artifacts.tsv", export_rows)
    write_tsv(qc_dir / "qc_results.tsv", qc_rows)
    write_tsv(qc_dir / "exclusion_log.tsv", exclusion_rows)
    write_tsv(qc_dir / "screening_funnel.tsv", funnel_rows)
    write_tsv(qc_dir / "exclusion_reason_summary.tsv", exclusion_summary)

    # 即使没有排除项，也固定 SQLite 空表的列类型，保证 fixture 与正式全库
    # 使用同一份可执行 SQL 和相同的字段合同。
    exclusion_summary_frame = pd.DataFrame(
        exclusion_summary,
        columns=["reason_order", "reason", "excluded_count"],
    ).astype(
        {
            "reason_order": "int64",
            "reason": "string",
            "excluded_count": "int64",
        }
    )

    sqlite_tables = {
        "antibody_instance": raw,
        "scaffold_candidate": candidate_frame,
        "residue_map": pd.DataFrame(residue_rows),
        "structure_connection": pd.DataFrame(connection_rows),
        "qc_result": pd.DataFrame(qc_rows),
        "cluster_member": pd.DataFrame(cluster_rows),
        "selection_member": pd.DataFrame(selected_rows),
        "export_artifact": pd.DataFrame(export_rows),
        "exclusion_log": pd.DataFrame(exclusion_rows),
        "screening_funnel": pd.DataFrame(funnel_rows),
        # 显式列避免 pandas 为零列 DataFrame 生成无效的 ``CREATE TABLE ()``。
        "exclusion_reason_summary": exclusion_summary_frame,
    }
    source_release = {
        "release_id": "sabdab2_sd_h_20260806",
        "source_name": "SAbDab2-nano SD-H bulk",
        "api_version": "2.0.10",
        "snapshot_last_modified": "2026-08-06",
        "downloaded_at": utc_now(),
        "license": "CC BY 4.0",
        "summary_sha256": sha256_file(summary_path),
        "archive_sha256": sha256_file(archive_path),
        "policy_id": config["policy_id"],
        "policy_sha256": sha256_file(config_path),
    }
    write_sqlite(registry / "scaffold_database.sqlite", sqlite_tables, source_release)

    summary = {
        "schema_version": "1.0.0",
        "generated_at": utc_now(),
        "source_release": source_release,
        "counts": {
            "raw_instances": len(raw),
            "unique_pdb": int(raw["PDB"].nunique()),
            "unique_sabdab_id": int(raw["SABDAB_ID"].nunique()),
            "archive_cif_files": len(members),
            "metadata_qualified_instances": len(primary),
            "hard_qc_pass_instances": len(pass_profiles),
            "best_instance_per_sabdab_id": len(unique_antibodies),
            "unique_exact_framework": len(representatives),
            "framework_clusters": len(cluster_reps),
            "selected_primary": sum(row["role"] == "PRIMARY" for row in selected_rows),
            "selected_reserve": sum(row["role"] == "RESERVE" for row in selected_rows),
        },
        "funnel": funnel_rows,
        "exclusion_reasons": exclusion_summary,
        "interpretation": {
            "positive_claim": "入选项是可追溯、结构完整且彼此多样的 VHH 生成起点。",
            "not_established": ["GLP-1 结合", "GLP-1(7-36)NH2 对 9-36NH2 选择性", "表达量", "热稳定性", "免疫原性", "知识产权自由实施"],
        },
    }
    (registry / "database_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 最后生成整个交付目录的 SHA 清单。Python 字节码缓存会因一次 import 就
    # 改写，既不是源数据也不是交付物，必须排除，否则复核会无故漂移。
    checksum_path = root / "SHA256SUMS"
    checksum_rows = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path == checksum_path
            or path.name.endswith(("-wal", "-shm"))
            or path.suffix == ".pyc"
            or "__pycache__" in path.parts
        ):
            continue
        checksum_rows.append(f"{sha256_file(path)}  {path.relative_to(root)}")
    checksum_path.write_text("\n".join(checksum_rows) + "\n", encoding="utf-8")

    print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
