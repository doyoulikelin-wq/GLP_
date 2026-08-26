#!/usr/bin/env python3
"""非破坏式整理 Git 仓库外的 GLP-1 本地工作区。

本脚本把大型 BoltzGen 资产、冻结运行、BindCraft 原型和共享研究输入移动到
``boltzgen/``、``bindcraft/``、``shared/``、``private/`` 与 ``archive/``。
所有大资产都在同一文件系统内原子改名，不复制、不删除；历史路径改为相对软
链接，历史 JSON、日志和 manifest 保持原字节。运行脚本依赖的旧 sibling 名也会
在新父目录中建立兼容别名。

默认只执行 dry-run。只有显式提供 ``--apply``，且迁移前完整树清单存在、目标
均不存在、重复多构象清单逐行一致时才会修改工作区。迁移记录不写用户名或绝对
路径；含个人信息的材料进入权限收紧的 ``private/``，不进入 Git。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Iterable


DEFAULT_WORKSPACE = Path(__file__).resolve().parents[4]
TREE_SCRIPT = Path(__file__).with_name("构建本地资产树清单_20260826.py")
MANIFEST_ROOT = Path("manifests/local_assets_20260826")


@dataclass(frozen=True)
class Move:
    """一条同卷移动及其可选旧路径别名。"""

    resource_id: str
    source: str
    destination: str
    alias_target: str | None
    classification: str
    reason: str


@dataclass(frozen=True)
class Link:
    """一条新建兼容软链接。"""

    path: str
    target: str
    role: str


def load_tree_module() -> ModuleType:
    """从同一 one_off 目录加载唯一资产映射，避免两份合同漂移。"""

    specification = importlib.util.spec_from_file_location(
        "glp_local_asset_tree_manifest_20260826", TREE_SCRIPT
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"无法加载资产清单脚本：{TREE_SCRIPT}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def root_identity(path: Path) -> tuple[int, int]:
    """返回根节点设备号和 inode，用于证明移动没有发生复制替换。"""

    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """写出具有稳定字段顺序的 UTF-8 CSV。"""

    if not rows:
        raise ValueError(f"拒绝写空清单：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def relative_symlink(link: Path, target: Path) -> None:
    """创建可随整个工作区移动的相对软链接，并立即验证解析目标。"""

    if link.exists() or link.is_symlink():
        raise FileExistsError(link)
    if not target.exists() and not target.is_symlink():
        raise FileNotFoundError(target)
    link.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(target, start=link.parent)
    link.symlink_to(relative_target, target_is_directory=target.is_dir())
    if link.resolve(strict=True) != target.resolve(strict=True):
        raise RuntimeError(f"软链接验证失败：{link} -> {target}")


def git_head(repository: Path) -> str:
    """读取当前仓库提交，不修改 Git 状态。"""

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def tighten_private_permissions(root: Path) -> None:
    """将私有目录设为 0700、普通文件设为 0600，跳过软链接。"""

    if not root.is_dir():
        return
    root.chmod(0o700)
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        if not current.is_symlink():
            current.chmod(0o700)
        for name in directory_names:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
        for name in file_names:
            child = current / name
            if not child.is_symlink() and stat.S_ISREG(child.lstat().st_mode):
                child.chmod(0o600)


def public_asset_moves(tree_module: ModuleType) -> list[Move]:
    """把树清单合同转为同卷移动合同。"""

    no_legacy_alias = {"shared_project_brief", "shared_competition_template"}
    positive_canonical = "shared/data/glp1_positive_conformer_ensemble_20260819"
    moves: list[Move] = []
    for asset in tree_module.ASSETS:
        if asset.resource_id == "shared_positive_conformers_legacy_mirror":
            alias_target = positive_canonical
        elif asset.resource_id in no_legacy_alias:
            alias_target = None
        else:
            alias_target = asset.after
        moves.append(
            Move(
                resource_id=asset.resource_id,
                source=asset.before,
                destination=asset.after,
                alias_target=alias_target,
                classification="public_or_restricted_research_asset",
                reason=asset.purpose,
            )
        )
    return moves


def fixed_moves(workspace: Path) -> tuple[list[Move], list[dict[str, str]]]:
    """生成报告副本、临时材料和私有交付物的显式移动合同。"""

    workbook_matches = sorted(
        path
        for path in workspace.glob("*智创胎界.xlsx")
        if path.is_file() and not path.name.startswith(".~")
    )
    if len(workbook_matches) != 1:
        raise RuntimeError(
            "根目录应恰有一个待私有化的项目工作簿，实际为 "
            f"{len(workbook_matches)}"
        )
    lock_matches = sorted(path for path in workspace.glob(".~*.xlsx") if path.is_file())
    if len(lock_matches) != 1:
        raise RuntimeError(
            f"根目录应恰有一个 Office 锁文件，实际为 {len(lock_matches)}"
        )

    moves = [
        Move(
            "shared_knowledge_graph_root_copy",
            "AI创制活性GLP-1知识图谱.html",
            "archive/shared/root_report_duplicates_20260826/AI创制活性GLP-1知识图谱.html",
            "shared/reports/glp1_ai_design_knowledge_graph_20260819.html",
            "byte_identical_report_duplicate",
            "根目录旧文件归档；旧书签改指 Git 规范报告",
        ),
        Move(
            "shared_blueprint_root_copy",
            "ai相关.html",
            "archive/shared/root_report_duplicates_20260826/ai相关.html",
            "shared/reports/glp1_ai_implementation_blueprint_20260818.html",
            "byte_identical_report_duplicate",
            "根目录旧文件归档；旧书签改指 Git 规范报告",
        ),
        Move(
            "private_competition_render_artifacts",
            ".codex_artifacts",
            "private/competition_submission_20260819/render_and_validation_artifacts",
            None,
            "private",
            "含检查脚本、预览图和潜在个人信息",
        ),
        Move(
            "private_competition_filled_output",
            "outputs",
            "private/competition_submission_20260819/filled_form_and_inspection",
            None,
            "private",
            "含填写后的表格和展开检查数据",
        ),
        Move(
            "private_competition_project_workbook",
            workbook_matches[0].relative_to(workspace).as_posix(),
            "private/competition_submission_20260824/project_workbook_20260824.xlsx",
            None,
            "private",
            "含个人姓名的赛事工作簿",
        ),
        Move(
            "temporary_office_lock",
            lock_matches[0].relative_to(workspace).as_posix(),
            "archive/temporary_files_20260826/root_office_lock_file.xlsx",
            None,
            "temporary",
            "保留而不删除的 Office 锁文件",
        ),
        Move(
            "temporary_boltzgen_office_lock",
            "data/boltzgen_data/.~BoltzGen_MVP_数据来源核验版_20260819.xlsx",
            "archive/temporary_files_20260826/boltzgen_source_review_lock.xlsx",
            None,
            "temporary",
            "保留而不删除的 Office 锁文件",
        ),
        Move(
            "empty_unnamed_directory",
            "data/boltzgen_data/未命名文件夹",
            "archive/empty_directories_20260826/data_boltzgen_data_unnamed",
            None,
            "empty_legacy_directory",
            "空目录移入归档而不永久删除",
        ),
        Move(
            "macos_metadata_root",
            ".DS_Store",
            "archive/macos_metadata_20260826/root.DS_Store",
            None,
            "temporary",
            "Finder 元数据不属于项目资产",
        ),
        Move(
            "macos_metadata_data",
            "data/.DS_Store",
            "archive/macos_metadata_20260826/data.DS_Store",
            None,
            "temporary",
            "Finder 元数据不属于项目资产",
        ),
        Move(
            "macos_metadata_sample_data",
            "data/样本数据/.DS_Store",
            "archive/macos_metadata_20260826/sample_data.DS_Store",
            None,
            "temporary",
            "Finder 元数据不属于项目资产",
        ),
        Move(
            "macos_metadata_boltzgen_data",
            "data/boltzgen_data/.DS_Store",
            "archive/macos_metadata_20260826/boltzgen_data.DS_Store",
            None,
            "temporary",
            "Finder 元数据不属于项目资产",
        ),
    ]
    private_rows = [
        {
            "resource_id": move.resource_id,
            "destination_uri": f"private://{Path(move.destination).relative_to('private').as_posix()}",
            "classification": move.classification,
            "reason": move.reason,
        }
        for move in moves
        if move.classification == "private"
    ]
    return moves, private_rows


def compatibility_links() -> list[Link]:
    """建立历史脚本经 ``Path.resolve`` 后仍需要的第二层 sibling 别名。"""

    return [
        Link(
            "boltzgen/runs/mvp_assets_v0.3.2",
            "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819",
            "run_sibling_compatibility",
        ),
        Link(
            "boltzgen/runs/mvp_run_001",
            "boltzgen/runs/nanobody_mps_smoke_20260819",
            "run_sibling_compatibility",
        ),
        Link(
            "boltzgen/runs/sabdab2_vhh_scaffolds_v1",
            "boltzgen/data/vhh_scaffold_database_20260819",
            "run_sibling_compatibility",
        ),
        Link(
            "boltzgen/runs/boltzgen_round1_old12_glp1_20260819",
            "boltzgen/runs/old12_glp1_round1_20260819",
            "run_sibling_compatibility",
        ),
        Link(
            "boltzgen/runs/boltzgen_mac_enhanced_old12_glp1_20260820",
            "boltzgen/runs/old12_glp1_mac_enhanced_20260820",
            "run_sibling_compatibility",
        ),
        Link(
            "boltzgen/data/mvp_assets_v0.3.2",
            "boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819",
            "data_sibling_compatibility",
        ),
        Link(
            "boltzgen/data/mvp_run_001",
            "boltzgen/runs/nanobody_mps_smoke_20260819",
            "data_sibling_compatibility",
        ),
        Link(
            "boltzgen/data/sabdab2_vhh_scaffolds_v1",
            "boltzgen/data/vhh_scaffold_database_20260819",
            "data_sibling_compatibility",
        ),
        Link(
            "boltzgen/data/boltzgen_round1_old12_glp1_20260819",
            "boltzgen/runs/old12_glp1_round1_20260819",
            "data_sibling_compatibility",
        ),
        Link(
            "boltzgen/data/boltzgen_mac_enhanced_old12_glp1_20260820",
            "boltzgen/runs/old12_glp1_mac_enhanced_20260820",
            "data_sibling_compatibility",
        ),
        Link(
            "boltzgen/data/ai_validation_assets_v1",
            "boltzgen/data/ai_structure_asset_validation_registry_20260826",
            "data_sibling_compatibility",
        ),
        Link(
            "shared/reports/glp1_ai_design_knowledge_graph_20260819.html",
            "GLP_/shared/reports/html/glp1_ai_design_knowledge_graph_20260819.html",
            "git_canonical_report",
        ),
        Link(
            "shared/reports/glp1_ai_implementation_blueprint_20260818.html",
            "GLP_/shared/reports/html/glp1_ai_implementation_blueprint_20260818.html",
            "git_canonical_report",
        ),
        Link(
            "bindcraft/runs/active_glp1_selectivity_prototype_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb",
            "bindcraft/runs/active_glp1_selectivity_prototype_20260823/BindCraft-0823.ipynb",
            "dated_public_entry",
        ),
    ]


def verify_preconditions(
    workspace: Path,
    moves: list[Move],
    links: list[Link],
) -> None:
    """在任何修改前完成存在性、冲突、清单和重复数据验证。"""

    receipt_path = workspace / MANIFEST_ROOT / "asset_tree_receipt_pre_20260826.json"
    if not receipt_path.is_file():
        raise RuntimeError("缺少迁移前完整树清单 receipt")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("phase") != "pre" or receipt.get("mismatches"):
        raise RuntimeError("迁移前树清单 receipt 状态异常")

    duplicate_a = (
        workspace
        / MANIFEST_ROOT
        / "tree_manifests/pre/shared_positive_conformers.csv"
    )
    duplicate_b = (
        workspace
        / MANIFEST_ROOT
        / "tree_manifests/pre/shared_positive_conformers_legacy_mirror.csv"
    )
    if duplicate_a.read_bytes() != duplicate_b.read_bytes():
        raise RuntimeError("两个正靶多构象目录的逐文件清单不一致，拒绝迁移")

    seen_destinations: set[Path] = set()
    for move in moves:
        source = workspace / move.source
        destination = workspace / move.destination
        if not source.exists() or source.is_symlink():
            raise RuntimeError(f"迁移源缺失或已是链接：{move.source}")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(f"迁移目标已存在：{move.destination}")
        if destination in seen_destinations:
            raise RuntimeError(f"重复迁移目标：{move.destination}")
        seen_destinations.add(destination)
        if source.stat().st_dev != workspace.stat().st_dev:
            raise RuntimeError(f"迁移源不在工作区同一文件系统：{move.source}")

    for link in links:
        path = workspace / link.path
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"兼容链接路径已存在：{link.path}")

    expected_reports = (
        (
            workspace / "AI创制活性GLP-1知识图谱.html",
            workspace / "GLP_/shared/reports/html/glp1_ai_design_knowledge_graph_20260819.html",
        ),
        (
            workspace / "ai相关.html",
            workspace / "GLP_/shared/reports/html/glp1_ai_implementation_blueprint_20260818.html",
        ),
    )
    for legacy, canonical in expected_reports:
        if sha256_file(legacy) != sha256_file(canonical):
            raise RuntimeError(f"根目录报告与 Git 规范副本不一致：{legacy.name}")


def apply_moves(workspace: Path, moves: list[Move]) -> list[dict[str, object]]:
    """逐项执行同卷原子改名，并记录 inode 守恒。"""

    rows: list[dict[str, object]] = []
    for move in moves:
        source = workspace / move.source
        destination = workspace / move.destination
        before_device, before_inode = root_identity(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        after_device, after_inode = root_identity(destination)
        if (before_device, before_inode) != (after_device, after_inode):
            raise RuntimeError(f"移动后根 inode 变化：{move.resource_id}")
        rows.append(
            {
                "resource_id": move.resource_id,
                "classification": move.classification,
                "legacy_uri": f"workspace://{move.source}",
                "canonical_uri": f"workspace://{move.destination}",
                "action": "same_filesystem_rename",
                "root_device": before_device,
                "root_inode": before_inode,
                "legacy_alias_target_uri": (
                    f"workspace://{move.alias_target}" if move.alias_target else ""
                ),
                "reason": move.reason,
            }
        )
        print(f"MOVED {move.source} -> {move.destination}")
    return rows


def apply_links(
    workspace: Path,
    moves: Iterable[Move],
    fixed_links: Iterable[Link],
) -> list[dict[str, object]]:
    """建立旧路径别名、运行 sibling 别名和 Git 报告入口。"""

    # 先建立 Git 规范报告与新父目录 sibling 别名；根目录旧报告的别名以这些
    # 固定入口为目标，因此必须排在普通 legacy alias 之前。
    links: list[Link] = list(fixed_links)
    links.extend(
        [
            Link(move.source, move.alias_target, "legacy_path")
            for move in moves
            if move.alias_target
        ]
    )
    rows: list[dict[str, object]] = []
    for link in links:
        path = workspace / link.path
        target = workspace / link.target
        relative_symlink(path, target)
        rows.append(
            {
                "legacy_uri": f"workspace://{link.path}",
                "target_uri": f"workspace://{link.target}",
                "role": link.role,
                "relative_link_text": os.readlink(path),
                "target_exists": "true",
                "resolved_target_matches": "true",
            }
        )
        print(f"LINKED {link.path} -> {os.readlink(path)}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    repository = workspace / "GLP_"
    if not (repository / ".git").is_dir():
        raise SystemExit(f"拒绝处理非 GLP-1 工作区：{workspace}")

    tree_module = load_tree_module()
    asset_moves = public_asset_moves(tree_module)
    additional_moves, private_rows = fixed_moves(workspace)
    moves = asset_moves + additional_moves
    fixed_links = compatibility_links()
    verify_preconditions(workspace, moves, fixed_links)

    print(f"PRECHECK PASS: {len(moves)} moves, {len(fixed_links)} fixed links")
    for move in moves:
        alias = f"; alias -> {move.alias_target}" if move.alias_target else ""
        print(f"PLAN {move.source} -> {move.destination}{alias}")
    if not args.apply:
        print("DRY RUN ONLY: 使用 --apply 才会执行迁移。")
        return 0

    started_at = datetime.now(timezone.utc).isoformat()
    migration_rows = apply_moves(workspace, moves)
    link_rows = apply_links(workspace, moves, fixed_links)
    tighten_private_permissions(workspace / "private")

    manifest_directory = workspace / MANIFEST_ROOT
    migration_path = manifest_directory / "local_workspace_migration_20260826.csv"
    compatibility_path = manifest_directory / "compatibility_aliases_20260826.csv"
    write_csv(migration_path, migration_rows)
    write_csv(compatibility_path, link_rows)

    private_manifest = workspace / "private/manifests/private_assets_20260826.csv"
    write_csv(private_manifest, private_rows)
    private_manifest.chmod(0o600)
    tighten_private_permissions(workspace / "private")

    receipt = {
        "schema_version": "LOCAL_WORKSPACE_MIGRATION_V1",
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_head(repository),
        "move_count": len(migration_rows),
        "compatibility_link_count": len(link_rows),
        "method": "same_filesystem_rename_without_delete_or_copy",
        "pre_tree_receipt_uri": (
            "workspace://manifests/local_assets_20260826/"
            "asset_tree_receipt_pre_20260826.json"
        ),
        "migration_manifest_uri": (
            "workspace://manifests/local_assets_20260826/"
            "local_workspace_migration_20260826.csv"
        ),
        "compatibility_manifest_uri": (
            "workspace://manifests/local_assets_20260826/"
            "compatibility_aliases_20260826.csv"
        ),
        "permanent_deletions": 0,
    }
    receipt_path = manifest_directory / "local_workspace_migration_receipt_20260826.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"WROTE {receipt_path.relative_to(workspace)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
