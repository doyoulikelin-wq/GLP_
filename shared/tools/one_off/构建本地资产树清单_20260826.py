#!/usr/bin/env python3
"""为 Git 仓库外的 GLP-1 本地资产生成完整、可复核的树清单。

本脚本只读资产内容，输出到工作区 ``manifests/local_assets_20260826``。
它在本地目录整理前后分别运行，逐项记录相对路径、文件类型、逻辑字节数、
SHA-256、权限和软链接目标；``post`` 阶段还能与 ``pre`` 清单逐行比较。

大模型权重、历史环境、运行日志和结构数据不进入 Git，清单也不会记录用户名
或绝对路径。脚本不会跟随目录软链接，避免兼容别名造成重复遍历或循环。
既有清单一律不覆盖；需要重新生成时必须指定一个新的相对输出目录。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DEFAULT_WORKSPACE = Path(__file__).resolve().parents[4]
MANIFEST_DIRECTORY = "manifests/local_assets_20260826"


@dataclass(frozen=True)
class Asset:
    """描述同一资产在整理前后的工作区相对位置。"""

    resource_id: str
    route: str
    before: str
    after: str
    purpose: str


ASSETS = (
    Asset(
        "bg_mvp_assets_v032",
        "boltzgen",
        "data/boltzgen_data/mvp_assets_v0.3.2",
        "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819",
        "BoltzGen v0.3.2 运行资产和 MVP 清理输入",
    ),
    Asset(
        "bg_nanobody_mps_smoke",
        "boltzgen",
        "data/boltzgen_data/mvp_run_001",
        "boltzgen/runs/nanobody_mps_smoke_20260819",
        "Apple Silicon MPS 纳米抗体最小链路冻结现场",
    ),
    Asset(
        "bg_vhh_scaffold_database",
        "boltzgen",
        "data/boltzgen_data/sabdab2_vhh_scaffolds_v1",
        "boltzgen/data/vhh_scaffold_database_20260819",
        "SAbDab2 VHH 骨架数据库、原始快照与精选 12 骨架",
    ),
    Asset(
        "bg_old12_round1",
        "boltzgen",
        "data/boltzgen_data/boltzgen_round1_old12_glp1_20260819",
        "boltzgen/runs/old12_glp1_round1_20260819",
        "旧 12 骨架第一轮冻结运行现场",
    ),
    Asset(
        "bg_old12_mac_enhanced",
        "boltzgen",
        "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820",
        "boltzgen/runs/old12_glp1_mac_enhanced_20260820",
        "旧 12 骨架 Mac 增强轮冻结运行现场",
    ),
    Asset(
        "bg_ai_validation_registry",
        "boltzgen",
        "data/boltzgen_data/ai_validation_assets_v1",
        "boltzgen/data/ai_structure_asset_validation_registry_20260826",
        "本地结构资产验证与使用边界登记册",
    ),
    Asset(
        "bg_algorithm_explainer_sources",
        "boltzgen",
        "data/boltzgen_data/boltzgen_vhh_glp1_explainer",
        "boltzgen/reports/vhh_glp1_algorithm_explainer_20260819",
        "BoltzGen VHH 与 GLP-1 算法说明报告的生成材料",
    ),
    Asset(
        "bg_execution_plan",
        "boltzgen",
        "data/boltzgen_data/BoltzGen_GLP1_VHH_无上下文执行实施方案.md",
        "boltzgen/plans/glp1_vhh_execution_plan_20260826.md",
        "BoltzGen 零上下文实施合同",
    ),
    Asset(
        "bg_algorithm_html",
        "boltzgen",
        "data/boltzgen_data/BoltzGen_VHH_GLP-1_数据流与算法原理.html",
        "boltzgen/reports/vhh_glp1_dataflow_and_algorithm_20260819.html",
        "BoltzGen 数据流与算法原理 HTML",
    ),
    Asset(
        "bg_source_review_workbook",
        "boltzgen",
        "data/boltzgen_data/BoltzGen_MVP_数据来源核验版_20260819.xlsx",
        "boltzgen/reports/mvp_data_source_review_20260819.xlsx",
        "MVP 数据来源核验工作簿",
    ),
    Asset(
        "bg_storage_budget_workbook",
        "boltzgen",
        "data/boltzgen_data/BoltzGen_MVP_数据来源与存储预算_20260819.xlsx",
        "boltzgen/reports/mvp_data_source_and_storage_budget_20260819.xlsx",
        "MVP 数据来源与存储预算工作簿",
    ),
    Asset(
        "shared_positive_conformers",
        "shared",
        "data/样本数据/binding-多构象",
        "shared/data/glp1_positive_conformer_ensemble_20260819",
        "GLP-1 正靶 1D0R 多构象集合",
    ),
    Asset(
        "shared_positive_conformers_legacy_mirror",
        "archive",
        "data/多构象-1",
        "archive/shared/glp1_positive_conformer_mirror_20260819",
        "与规范正靶集合字节一致的旧镜像，暂存一个验证周期",
    ),
    Asset(
        "shared_peptide_lockbox",
        "shared",
        "data/样本数据/not_binding",
        "shared/data/peptide_lockbox_countertargets_20260823",
        "同源肽和 GLP-1(9–36) 反靶 lockbox",
    ),
    Asset(
        "shared_glp2_tuning_countertargets",
        "shared",
        "data/not_binding",
        "shared/data/glp2_tuning_countertargets_20260824",
        "GLP-2 与 GLP-1(9–36) 调参挑战面板",
    ),
    Asset(
        "shared_vhh_challenger_scaffolds",
        "shared",
        "data/样本数据/boltzgen_vhh_scaffolds",
        "shared/data/vhh_challenger_scaffolds_20260823",
        "新 17 个 VHH challenger 骨架展开包",
    ),
    Asset(
        "shared_vhh_challenger_source_package",
        "archive",
        "data/sd-h骨架",
        "archive/shared/vhh_challenger_source_package_20260819",
        "新 17 骨架的来源压缩包与说明",
    ),
    Asset(
        "bc_active_glp1_prototype",
        "bindcraft",
        "bindcraft/BindCraft-0823.ipynb",
        "bindcraft/runs/active_glp1_selectivity_prototype_20260823/BindCraft-0823.ipynb",
        "BindCraft 活性 GLP-1 选择性研究原型",
    ),
    Asset(
        "bc_target_panel",
        "bindcraft",
        "bindcraft/glp1_target_panel",
        "bindcraft/data/glp1_selectivity_target_panel_20260825",
        "BindCraft GLP-1 选择性正负靶 PDB 面板",
    ),
    Asset(
        "bc_input_audit",
        "bindcraft",
        "bindcraft/review_artifacts",
        "bindcraft/reports/input_audit_20260826",
        "BindCraft 输入静态审计产物",
    ),
    Asset(
        "shared_project_brief",
        "shared",
        "AI创制的活性GLP-1型态选择性捕获蛋白及其质谱增敏试剂_项目简介.docx",
        "shared/documents/glp1_capture_protein_project_brief_20260815.docx",
        "GLP-1 捕获蛋白项目简介",
    ),
    Asset(
        "shared_competition_template",
        "shared",
        "AI造物大赛_复赛平台需求收集模板.xlsx",
        "shared/templates/ai_creation_semifinal_requirement_template_20260817.xlsx",
        "未填写的复赛平台需求模板",
    ),
)


@dataclass(frozen=True)
class TreeRecord:
    """单个文件或软链接在资产根内的稳定记录。"""

    relative_path: str
    file_type: str
    size_bytes: int
    sha256: str
    mode_octal: str
    symlink_target: str


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算大文件 SHA-256，避免一次载入模型权重。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_for_path(path: Path, root: Path) -> TreeRecord:
    """构造一个不跟随软链接的树记录。"""

    metadata = path.lstat()
    relative = "." if path == root else path.relative_to(root).as_posix()
    mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
    if path.is_symlink():
        return TreeRecord(relative, "symlink", 0, "", mode, os.readlink(path))
    if path.is_file():
        return TreeRecord(
            relative,
            "file",
            metadata.st_size,
            sha256_file(path),
            mode,
            "",
        )
    raise ValueError(f"不支持的资产节点类型：{path}")


def iter_tree(root: Path) -> Iterator[TreeRecord]:
    """按 UTF-8 路径稳定顺序遍历文件和软链接，不跟随目录软链接。"""

    if root.is_symlink() or root.is_file():
        yield record_for_path(root, root)
        return
    if not root.is_dir():
        raise FileNotFoundError(root)

    records: list[TreeRecord] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        directory_names.sort()
        file_names.sort()

        retained_directories: list[str] = []
        for name in directory_names:
            child = current / name
            if child.is_symlink():
                records.append(record_for_path(child, root))
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            records.append(record_for_path(current / name, root))

    yield from sorted(records, key=lambda item: item.relative_path.encode("utf-8"))


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    """在尚未发布的 staging 目录中写 CSV，并把内容刷新到磁盘。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, payload: dict[str, object]) -> None:
    """在 staging 目录写稳定 JSON，并把内容刷新到磁盘。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def load_records(path: Path) -> list[dict[str, str]]:
    """读取既有树清单，用于迁移前后严格比较。"""

    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--manifest-directory",
        type=Path,
        default=Path(MANIFEST_DIRECTORY),
        help=(
            "工作区内的相对输出目录；既有 generation 不会被覆盖，"
            "重新生成时请换一个带日期或 generation 的目录"
        ),
    )
    parser.add_argument("--phase", choices=("pre", "post"), required=True)
    parser.add_argument(
        "--verify-pre",
        action="store_true",
        help="在 post 阶段将每个新清单与对应 pre 清单逐行比较",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not (workspace / "GLP_/.git").is_dir():
        raise SystemExit(f"拒绝处理非 GLP-1 工作区：{workspace}")
    if args.verify_pre != (args.phase == "post"):
        raise SystemExit("post 阶段必须提供 --verify-pre；pre 阶段不得提供该参数")
    if args.manifest_directory.is_absolute() or ".." in args.manifest_directory.parts:
        raise SystemExit("--manifest-directory 必须是工作区内且不含 '..' 的相对路径")

    output_root = (workspace / args.manifest_directory).resolve()
    try:
        output_root.relative_to(workspace)
    except ValueError as exc:
        raise SystemExit("清单输出目录必须位于工作区内") from exc
    phase_root = output_root / "tree_manifests" / args.phase
    summary_path = output_root / f"asset_tree_summary_{args.phase}_20260826.csv"
    receipt_path = output_root / f"asset_tree_receipt_{args.phase}_20260826.json"

    # Receipt 是一次 generation 的提交标志。任何目标已存在都拒绝执行，避免
    # 迁移后误把旧路径软链接写回 pre 证据，也避免把已验证 post 降级为未验证。
    publish_targets = (phase_root, summary_path, receipt_path)
    existing = [
        path
        for path in publish_targets
        if path.exists() or path.is_symlink()
    ]
    if existing:
        names = ", ".join(path.relative_to(workspace).as_posix() for path in existing)
        raise SystemExit(
            "拒绝覆盖既有清单 generation："
            f"{names}；请通过 --manifest-directory 指定新目录"
        )

    output_root.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, object]] = []
    mismatches: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix=f".{args.phase}_generation_", dir=output_root
    ) as temporary_directory:
        staging_root = Path(temporary_directory)
        staging_phase_root = staging_root / "tree_manifests" / args.phase

        for asset in ASSETS:
            relative_root = asset.before if args.phase == "pre" else asset.after
            root = workspace / relative_root
            if not root.exists() and not root.is_symlink():
                raise SystemExit(f"缺少 {args.phase} 资产：{relative_root}")
            if args.phase == "pre" and root.is_symlink():
                raise SystemExit(
                    f"pre 资产已是兼容软链接，说明迁移已经发生：{relative_root}"
                )

            records = list(iter_tree(root))
            final_manifest_path = phase_root / f"{asset.resource_id}.csv"
            staged_manifest_path = staging_phase_root / final_manifest_path.name
            write_csv(
                staged_manifest_path,
                [asdict(record) for record in records],
                list(TreeRecord.__dataclass_fields__),
            )
            manifest_sha256 = sha256_file(staged_manifest_path)
            summary_rows.append(
                {
                    "resource_id": asset.resource_id,
                    "route": asset.route,
                    "phase": args.phase,
                    "root_uri": f"workspace://{relative_root}",
                    "record_count": len(records),
                    "file_count": sum(
                        record.file_type == "file" for record in records
                    ),
                    "symlink_count": sum(
                        record.file_type == "symlink" for record in records
                    ),
                    "size_bytes": sum(record.size_bytes for record in records),
                    "manifest_uri": (
                        "workspace://"
                        + final_manifest_path.relative_to(workspace).as_posix()
                    ),
                    "manifest_sha256": manifest_sha256,
                    "purpose": asset.purpose,
                }
            )

            if args.phase == "post":
                pre_path = output_root / "tree_manifests/pre" / final_manifest_path.name
                if not pre_path.is_file():
                    mismatches.append(f"{asset.resource_id}: 缺少 pre 清单")
                elif load_records(pre_path) != load_records(staged_manifest_path):
                    mismatches.append(f"{asset.resource_id}: pre/post 树记录不一致")

            print(
                f"{asset.resource_id}: {len(records)} records, "
                f"{sum(record.size_bytes for record in records)} bytes"
            )

        staged_summary_path = staging_root / summary_path.name
        write_csv(staged_summary_path, summary_rows, list(summary_rows[0]))
        receipt = {
            "schema_version": "LOCAL_ASSET_TREE_MANIFEST_V1",
            "phase": args.phase,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "asset_count": len(ASSETS),
            "summary_uri": (
                "workspace://" + summary_path.relative_to(workspace).as_posix()
            ),
            "summary_sha256": sha256_file(staged_summary_path),
            "pre_post_verified": args.phase == "post",
            "mismatches": mismatches,
        }
        staged_receipt_path = staging_root / receipt_path.name
        write_json(staged_receipt_path, receipt)

        if mismatches:
            for mismatch in mismatches:
                print(f"MISMATCH: {mismatch}")
            return 1

        # 再次检查发布目标，receipt 最后发布；只有 receipt 存在才代表本次
        # generation 完整提交。所有内容先在同卷 staging 中写完并 fsync。
        if any(path.exists() or path.is_symlink() for path in publish_targets):
            raise SystemExit("发布前发现清单目标冲突，拒绝覆盖")
        phase_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_phase_root, phase_root)
        os.replace(staged_summary_path, summary_path)
        os.replace(staged_receipt_path, receipt_path)

    print(f"Wrote {summary_path.relative_to(workspace)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
