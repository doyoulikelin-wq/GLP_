#!/usr/bin/env python3
"""准备并冻结 Mac 增强版 BoltzGen 候选生成的输入合同。

本脚本只复制、核对和记录文件，绝不会加载模型权重或生成候选。数据来源固定为
已经完成且封存的 ``boltzgen_round1_old12_glp1_20260819``：同一份 GLP-1(7–36)
几何、同一批旧 12 个 VHH 骨架、同一份实验性 Metal Performance Shaders（MPS）
源码快照。这样增强尝试与第一轮之间只改变采样深度和设计 checkpoint 组合，不会
悄悄更换输入。

“训练”边界：本项目调用已经训练好的 BoltzGen 权重执行推理和候选生成，不更新
基础模型参数；本脚本更不执行神经网络。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = RUN_ROOT.parent
SOURCE_ROOT = DATA_ROOT / "boltzgen_round1_old12_glp1_20260819"
SOURCE_MANIFEST = SOURCE_ROOT / "provenance" / "input_manifest.json"

# 大型权重只引用经过校验的共享缓存，不在本目录重复占用约 6.35 GB。
RUNTIME_CACHE = DATA_ROOT / "mvp_assets_v0.3.2" / "runtime_cache"
RUNTIME_ASSETS = {
    "design_diverse": RUNTIME_CACHE / "boltzgen1_diverse.ckpt",
    "design_adherence": RUNTIME_CACHE / "boltzgen1_adherence.ckpt",
    "inverse_fold": RUNTIME_CACHE / "boltzgen1_ifold.ckpt",
    "folding": RUNTIME_CACHE / "boltz2_conf_final.ckpt",
    "molecule_dictionary": RUNTIME_CACHE / "mols.zip",
}
EXPECTED_RUNTIME_SHA256 = {
    "design_diverse": "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c",
    "design_adherence": "ac7078b3dc13064c68e0c3fd542e5bc538c33558bf6607f65e499eb336ca5e5d",
    "inverse_fold": "dd4cf108c94471bdc3a326b7b180fa3854dc019110fae780208c30b50bd56578",
    "folding": "525a51ef306da7282a54d23a4a5b91212fc60d0ff6b23b56dd6351de3b387530",
    "molecule_dictionary": "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53",
}

# 所有运行档位都是这台机器可承受范围内的显式合同，而不是隐藏默认值。
PROFILES: dict[str, dict[str, Any]] = {
    "balanced_all12": {
        "description": "旧 12 骨架的均衡增强筛查；每个 checkpoint 精确分配 2/4 个设计。",
        "selection_ranks": list(range(1, 13)),
        "num_designs": 4,
        "budget": 2,
        "design_checkpoints": ["design_diverse", "design_adherence"],
        "design_sampling_steps": 100,
        "design_recycling_steps": 2,
        "inverse_fold_sampling_steps": 60,
        "inverse_fold_recycling_steps": 2,
        "folding_sampling_steps": 100,
        "folding_recycling_steps": 2,
        "folding_diffusion_samples": 2,
        "precision": "32",
        "minimum_free_disk_gib": 25,
        "prerequisite_profile": None,
    },
    "balanced_diverse_all12": {
        "description": (
            "旧 12 骨架的 diverse 单检查点支路；从双检查点压力探针中拆分，"
            "避免同一 MPS 进程切换两个大型设计权重。"
        ),
        "selection_ranks": list(range(1, 13)),
        "num_designs": 2,
        "budget": 1,
        "design_checkpoints": ["design_diverse"],
        "design_sampling_steps": 100,
        "design_recycling_steps": 2,
        "inverse_fold_sampling_steps": 60,
        "inverse_fold_recycling_steps": 2,
        "folding_sampling_steps": 100,
        "folding_recycling_steps": 2,
        "folding_diffusion_samples": 2,
        "precision": "32",
        "minimum_free_disk_gib": 25,
        "prerequisite_profile": None,
        "parent_stress_profile": "balanced_all12",
    },
    "balanced_adherence_all12": {
        "description": (
            "旧 12 骨架的 adherence 单检查点支路；与 diverse 支路使用独立进程、"
            "独立目录和独立日志，分析阶段才合并。"
        ),
        "selection_ranks": list(range(1, 13)),
        "num_designs": 2,
        "budget": 1,
        "design_checkpoints": ["design_adherence"],
        "design_sampling_steps": 100,
        "design_recycling_steps": 2,
        "inverse_fold_sampling_steps": 60,
        "inverse_fold_recycling_steps": 2,
        "folding_sampling_steps": 100,
        "folding_recycling_steps": 2,
        "folding_diffusion_samples": 2,
        "precision": "32",
        "minimum_free_disk_gib": 25,
        "prerequisite_profile": None,
        "parent_stress_profile": "balanced_all12",
    },
    "near_official_adherence_7xl0": {
        "description": (
            "只用 7XL0 和 adherence 单一设计 checkpoint 做近官方采样深度探针；"
            "严禁在同一 MPS 进程装载或切换第二个设计 checkpoint。"
        ),
        "selection_ranks": [1],
        "num_designs": 4,
        "budget": 1,
        "design_checkpoints": ["design_adherence"],
        "design_sampling_steps": 500,
        "design_recycling_steps": 3,
        "inverse_fold_sampling_steps": 200,
        "inverse_fold_recycling_steps": 3,
        "inverse_fold_diffusion_samples": 1,
        "folding_sampling_steps": 200,
        "folding_recycling_steps": 3,
        "folding_diffusion_samples": 1,
        "precision": "32",
        "minimum_free_disk_gib": 15,
        # 先确认同一 checkpoint 的浅层 7XL0 支路已经端到端成功，再允许加深采样。
        "prerequisite_profile": "balanced_adherence_all12",
        "parent_stress_profile": "balanced_adherence_all12",
        "single_checkpoint_required": True,
        "official_like_scope": (
            "采样步数接近官方默认；候选量、硬件、MPS代码与统计规模仍不是官方生产基线"
        ),
    },
    "full_depth_probe": {
        "description": "只用 7XL0 骨架做接近官方采样步数的深度探针；先只复折叠 1 个样本。",
        "selection_ranks": [1],
        "num_designs": 4,
        "budget": 1,
        "design_checkpoints": ["design_diverse", "design_adherence"],
        "design_sampling_steps": 500,
        "design_recycling_steps": 3,
        "inverse_fold_sampling_steps": 200,
        "inverse_fold_recycling_steps": 3,
        "folding_sampling_steps": 200,
        "folding_recycling_steps": 3,
        "folding_diffusion_samples": 1,
        "precision": "32",
        "minimum_free_disk_gib": 12,
        "prerequisite_profile": None,
    },
    "full_depth_probe_samples2": {
        "description": "7XL0 深度探针的复折叠样本数 2 复核；仅在 samples1 全流程成功后允许执行。",
        "selection_ranks": [1],
        "num_designs": 4,
        "budget": 1,
        "design_checkpoints": ["design_diverse", "design_adherence"],
        "design_sampling_steps": 500,
        "design_recycling_steps": 3,
        "inverse_fold_sampling_steps": 200,
        "inverse_fold_recycling_steps": 3,
        "folding_sampling_steps": 200,
        "folding_recycling_steps": 3,
        "folding_diffusion_samples": 2,
        "precision": "32",
        "minimum_free_disk_gib": 15,
        "prerequisite_profile": "full_depth_probe",
    },
}


def utc_now() -> str:
    """返回可以跨机器比较的 UTC ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算 SHA-256，避免一次把大型文件读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    """先写临时文件再原子替换，避免中断留下半个 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def copy_if_identical_or_missing(source: Path, destination: Path, expected_sha: str) -> dict[str, Any]:
    """复制一个冻结文件；目的地已存在时只接受字节完全相同的文件。

    这里故意没有“强制覆盖”选项：若用户在增强目录修改过输入，脚本会停止并指出
    冲突，而不是销毁用户内容。
    """

    observed_source = sha256_file(source)
    if observed_source != expected_sha:
        raise ValueError(f"源文件哈希偏离封存清单：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        observed_destination = sha256_file(destination)
        if observed_destination != expected_sha:
            raise FileExistsError(
                f"目的地已存在但内容不同，拒绝覆盖：{destination}"
            )
    else:
        shutil.copy2(source, destination)
    return {
        "path": destination.relative_to(RUN_ROOT).as_posix(),
        "size_bytes": destination.stat().st_size,
        "sha256": expected_sha,
        "source_path": str(source),
    }


def source_records(source_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """汇总需要复制的输入、顶层规格和 MPS 源码文件。"""

    records: dict[str, dict[str, Any]] = {
        row["path"]: row for row in source_manifest["prepared_files"]
    }
    vendor = source_manifest["runtime"]["vendor_code"]["files"]
    for row in vendor:
        records[row["path"]] = row
    allowed_prefixes = ("inputs/", "configs/", "vendor/")
    selected = [row for path, row in records.items() if path.startswith(allowed_prefixes)]
    return sorted(selected, key=lambda row: row["path"])


def validate_profiles() -> None:
    """在落盘前检查档位内部逻辑，避免配置名与数值不一致。"""

    for name, profile in PROFILES.items():
        if profile["num_designs"] % len(profile["design_checkpoints"]) != 0:
            raise ValueError(f"{name}: num_designs 不能在所选 checkpoint 间等分")
        if profile["precision"] != "32":
            raise ValueError(f"{name}: 当前实验性 MPS 只批准 precision=32")
        if not 1 <= profile["budget"] <= profile["num_designs"]:
            raise ValueError(f"{name}: budget 必须在 1..num_designs 内")
        if any(rank not in range(1, 13) for rank in profile["selection_ranks"]):
            raise ValueError(f"{name}: selection_ranks 越界")
    prerequisite = PROFILES["full_depth_probe_samples2"]["prerequisite_profile"]
    if prerequisite != "full_depth_probe":
        raise ValueError("samples2 档位必须以前置 samples1 成功为门控")
    near_official = PROFILES["near_official_adherence_7xl0"]
    if near_official["selection_ranks"] != [1]:
        raise ValueError("near_official_adherence_7xl0 只能包含 rank 1 的 7XL0")
    if near_official["design_checkpoints"] != ["design_adherence"]:
        raise ValueError("近官方深度档位必须且只能使用 adherence checkpoint")
    if not near_official.get("single_checkpoint_required"):
        raise ValueError("近官方深度档位缺少单 checkpoint 硬门")
    if near_official["inverse_fold_diffusion_samples"] != 1:
        raise ValueError("近官方深度档位的逆折叠 samples 必须为 1")


def parse_args() -> argparse.Namespace:
    """解析准备脚本参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只核对当前目录，不复制缺失文件，也不重写增强清单。",
    )
    return parser.parse_args()


def main() -> int:
    """冻结输入、运行档位、权重指纹和软件环境。"""

    args = parse_args()
    validate_profiles()
    if not SOURCE_MANIFEST.exists():
        raise FileNotFoundError(f"缺少第一轮封存清单：{SOURCE_MANIFEST}")
    source_manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    if source_manifest.get("scaffold_population", {}).get("count") != 12:
        raise ValueError("第一轮封存清单不是旧 12 骨架集合")

    copied_records: list[dict[str, Any]] = []
    for row in source_records(source_manifest):
        source = SOURCE_ROOT / row["path"]
        destination = RUN_ROOT / row["path"]
        if args.verify_only and not destination.exists():
            raise FileNotFoundError(f"验证模式发现缺失文件：{destination}")
        copied_records.append(
            copy_if_identical_or_missing(source, destination, row["sha256"])
        )

    runtime_records = []
    for name, path in RUNTIME_ASSETS.items():
        if not path.exists():
            raise FileNotFoundError(f"缺少共享运行资产：{path}")
        observed = sha256_file(path)
        if observed != EXPECTED_RUNTIME_SHA256[name]:
            raise ValueError(f"共享运行资产 {name} 的 SHA-256 不匹配")
        runtime_records.append(
            {
                "asset": name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": observed,
                "storage_policy": "external_validated_cache_not_duplicated",
            }
        )

    # verify-only 不改变任何文件；上面的哈希核对完成即返回。
    if args.verify_only:
        print(f"验证通过：{len(copied_records)} 个冻结文件、5 个共享运行资产。")
        return 0

    manifest = {
        "schema_version": "2.0.0",
        "campaign_id": RUN_ROOT.name,
        "created_at_utc": utc_now(),
        "execution_semantics": "pretrained_inference_candidate_generation_not_weight_training",
        "source_round": {
            "path": str(SOURCE_ROOT),
            "manifest_path": str(SOURCE_MANIFEST),
            "manifest_sha256": sha256_file(SOURCE_MANIFEST),
            "campaign_id": source_manifest["campaign_id"],
        },
        "target": source_manifest["target"],
        "scaffold_population": source_manifest["scaffold_population"],
        "profiles": PROFILES,
        "fixed_execution_contract": {
            "protocol": "nanobody-anything",
            "device": "single Apple MPS task",
            "devices": 1,
            "num_workers": 1,
            "diffusion_batch_size": 1,
            "use_kernels": False,
            "analysis_liability_modality": "antibody",
            "filtering_modality": "antibody",
            "filter_bindingsite": True,
            "offline": True,
            "pipeline_steps": [
                "design",
                "inverse_folding",
                "folding",
                "analysis",
                "filtering",
            ],
        },
        "runtime": {
            "python_executable": str(DATA_ROOT / "mvp_run_001" / "env" / "bin" / "python"),
            "boltzgen_executable": str(DATA_ROOT / "mvp_run_001" / "env" / "bin" / "boltzgen"),
            "experimental_mps_source_commit": source_manifest["runtime"]["vendor_code"][
                "experimental_mps_source_commit"
            ],
            "official_release_baseline": "BoltzGen v0.3.2",
            "platform_at_prepare": platform.platform(),
            "python_at_prepare": sys.version,
            "assets": runtime_records,
        },
        "frozen_files": copied_records,
        "known_limits": [
            "只输入一个 GLP-1(7–36) 正靶几何，不评价型态选择性或反靶排斥",
            "C 端酰胺尚未在标准聚合物 CIF 中完成原子级验证",
            "MPS 分支未合并，不能等同于官方 Linux + NVIDIA CUDA 基线",
            "BoltzGen CLI 没有为整条管线暴露统一随机种子，序列不能保证逐字节重现",
            "结构分数是计算代理，不能换算为解离常数、亲和力或实验成功率",
        ],
    }
    atomic_write_json(RUN_ROOT / "provenance" / "enhanced_input_manifest.json", manifest)
    atomic_write_json(RUN_ROOT / "provenance" / "profiles.json", PROFILES)

    python_executable = Path(manifest["runtime"]["python_executable"])
    freeze = subprocess.run(
        [str(python_executable), "-m", "pip", "freeze"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (RUN_ROOT / "provenance" / "pip_freeze.txt").write_text(freeze, encoding="utf-8")
    print(
        "准备完成：旧 12 骨架与单一 GLP-1 输入已冻结；"
        f"{len(copied_records)} 个文件、{len(PROFILES)} 个运行档位、"
        "5 个共享资产均通过 SHA-256。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
