#!/usr/bin/env python3
"""在 Apple Silicon 上执行 BoltzGen nanobody-anything 的最小可审计 MVP。

这不是对 BoltzGen 模型本体的再训练；它调用已经下载的官方模型权重进行推理。
脚本将每条命令、开始/结束时间、退出码和输入哈希写入运行目录，避免只留下
无法追溯的模型文件。所有路径都从本脚本所在项目目录推导，不依赖当前终端位置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


# 本脚本位于 RUN_ROOT/scripts/，因此父目录的父目录就是本次运行根目录。
RUN_ROOT = Path(__file__).resolve().parent.parent

# 数据资产与本次运行目录相邻；这里不复制 6.35 GB 模型，避免无意义的重复占用。
ASSET_ROOT = RUN_ROOT.parent / "mvp_assets_v0.3.2"
RUNTIME_CACHE = ASSET_ROOT / "runtime_cache"

# 固定使用本次实验性 MPS 环境中的可执行文件，避免误调用系统 Python。
PYTHON = RUN_ROOT / "env" / "bin" / "python"
BOLTZGEN = RUN_ROOT / "env" / "bin" / "boltzgen"

# 输入、输出和日志目录各自分离；原始输入不会在模型运行过程中被覆盖。
DESIGN_SPEC = RUN_ROOT / "inputs" / "glp1_7_36_nanobody_mvp.yaml"
RUN_OUTPUT_ROOT = RUN_ROOT / "outputs" / "02_mps_run"
CHECK_OUTPUT = RUN_OUTPUT_ROOT / "input_check"
PIPELINE_OUTPUT = RUN_OUTPUT_ROOT / "pipeline"
LOG_DIR = RUN_ROOT / "logs"
STATUS_PATH = RUN_OUTPUT_ROOT / "mvp_run_status.json"

# 这四项是本协议真正读取的运行资产。affinity checkpoint 不在清单中，
# 因为 nanobody-anything 协议不会运行 protein-small_molecule affinity head。
RUNTIME_FILES = {
    "design_diverse": RUNTIME_CACHE / "boltzgen1_diverse.ckpt",
    "inverse_fold": RUNTIME_CACHE / "boltzgen1_ifold.ckpt",
    "folding": RUNTIME_CACHE / "boltz2_conf_final.ckpt",
    "molecule_dictionary": RUNTIME_CACHE / "mols.zip",
}

# 预先核对过的 SHA-256。运行前再次计算，防止下载中断或文件被意外改写。
EXPECTED_SHA256 = {
    "design_diverse": "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c",
    "inverse_fold": "dd4cf108c94471bdc3a326b7b180fa3854dc019110fae780208c30b50bd56578",
    "folding": "525a51ef306da7282a54d23a4a5b91212fc60d0ff6b23b56dd6351de3b387530",
    "molecule_dictionary": "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53",
}


def utc_now() -> str:
    """返回带时区的 ISO 8601 时间，便于跨机器比较运行记录。"""

    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算大文件 SHA-256，避免一次把 2 GB checkpoint 读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    """以稳定、可读的 UTF-8 JSON 写出机器可读状态。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def quote_command(command: Iterable[str]) -> str:
    """把参数列表转成适合阅读的命令字符串；实际执行仍使用安全的列表形式。"""

    import shlex

    return " ".join(shlex.quote(str(part)) for part in command)


def run_logged(stage: str, command: list[str], environment: dict[str, str]) -> dict[str, object]:
    """实时显示并保存一个子进程的合并标准输出/错误输出。

    返回值只记录客观运行事实，不把退出码 0 自动解释成“候选有效”。模型质量要在
    后续分析阶段依据 CSV 中的 `pass_filters` 和各项代理指标分别判断。
    """

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{stage}.log"
    started_at = utc_now()
    start_clock = time.monotonic()

    print(f"\n[{stage}] {quote_command(command)}", flush=True)
    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"stage: {stage}\n")
        log_handle.write(f"started_at_utc: {started_at}\n")
        log_handle.write(f"working_directory: {RUN_ROOT}\n")
        log_handle.write(f"command: {quote_command(command)}\n\n")
        log_handle.flush()

        process = subprocess.Popen(
            command,
            cwd=RUN_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log_handle.write(line)
        return_code = process.wait()

        elapsed_seconds = time.monotonic() - start_clock
        finished_at = utc_now()
        log_handle.write("\n")
        log_handle.write(f"finished_at_utc: {finished_at}\n")
        log_handle.write(f"elapsed_seconds: {elapsed_seconds:.3f}\n")
        log_handle.write(f"return_code: {return_code}\n")

    result = {
        "stage": stage,
        "command": command,
        "command_display": quote_command(command),
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "return_code": return_code,
        "log_path": str(log_path.relative_to(RUN_ROOT)),
    }
    if return_code != 0:
        raise RuntimeError(f"阶段 {stage} 失败，退出码 {return_code}；详见 {log_path}")
    return result


def make_environment() -> dict[str, str]:
    """构造子进程环境，并明确标出实验性 MPS 回退策略。"""

    environment = os.environ.copy()

    # MPS 尚未实现的个别 PyTorch 算子允许回退到 CPU；日志仍会保留相应警告。
    environment["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

    # MPS PR 的 README 要求允许 Conda OpenMP 与某些 wheel 的 OpenMP 共存。
    environment["KMP_DUPLICATE_LIB_OK"] = "TRUE"

    # 限制 CPU 线程数，避免 18 GB 统一内存机器上分析进程过度并行。
    environment["OMP_NUM_THREADS"] = "4"
    environment["MKL_NUM_THREADS"] = "4"
    return environment


def validate_inputs() -> dict[str, object]:
    """检查必需文件、模型哈希、代码版本和当前硬件。"""

    required_paths = [PYTHON, BOLTZGEN, DESIGN_SPEC, *RUNTIME_FILES.values()]
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少 MVP 必需文件：\n" + "\n".join(missing))

    asset_rows = []
    for asset_name, path in RUNTIME_FILES.items():
        observed_sha = sha256_file(path)
        expected_sha = EXPECTED_SHA256[asset_name]
        if observed_sha != expected_sha:
            raise ValueError(f"{asset_name} 的 SHA-256 不匹配")
        asset_rows.append(
            {
                "asset": asset_name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": observed_sha,
            }
        )

    # 保存输入文本自身的指纹；日后即使同名 YAML 被修改，也能识别这次实际输入。
    input_rows = []
    for path in (
        DESIGN_SPEC,
        RUN_ROOT / "inputs" / "target" / "6X18_GLP1_7-36_geometry.cif",
        RUN_ROOT / "inputs" / "scaffold" / "7xl0_mvp_scaffold.yaml",
        RUN_ROOT / "inputs" / "scaffold" / "7XL0_official_example.cif",
    ):
        input_rows.append(
            {
                "path": str(path.relative_to(RUN_ROOT)),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=RUN_ROOT / "vendor" / "boltzgen_mps_pr145",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    manifest = {
        "schema_version": 1,
        "created_at_utc": utc_now(),
        "run_id": "boltzgen_nanobody_mps_smoke_001",
        "purpose": "验证 GLP-1(7–36) × VHH nanobody-anything 的本地推理链路",
        "execution_class": "experimental_mps_smoke_test",
        "official_release_baseline": "BoltzGen v0.3.2",
        "experimental_mps_pr_commit": git_commit,
        "platform": platform.platform(),
        "python": sys.version,
        "design_spec": str(DESIGN_SPEC.relative_to(RUN_ROOT)),
        "requested_designs": 2,
        "final_budget": 1,
        "fast_smoke_settings": {
            "design_sampling_steps": 50,
            "inverse_fold_sampling_steps": 30,
            "folding_sampling_steps": 50,
            "folding_samples_per_candidate": 1,
            "recycling_steps": 1,
            "precision": "32",
        },
        "runtime_assets": asset_rows,
        "input_files": input_rows,
        "known_limits": [
            "使用未合并的 MPS PR，不代表官方 v0.3.2 原生支持 macOS",
            "目标标准聚合物 CIF 未原子级验证 C 端酰胺",
            "只运行正靶 7–36，不能评价对 9–36 的选择性",
            "降低采样步数与折叠样本数，结果只用于链路冒烟测试",
            "BoltzGen 推理 CLI 未暴露统一随机种子，精确重复结果不保证一致",
        ],
    }
    write_json(RUN_OUTPUT_ROOT / "input_manifest.json", manifest)
    return manifest


def check_command() -> list[str]:
    """构造只解析输入、不加载模型权重的结构检查命令。"""

    return [
        str(BOLTZGEN),
        "check",
        str(DESIGN_SPEC),
        "--output",
        str(CHECK_OUTPUT),
        "--moldir",
        str(RUNTIME_FILES["molecule_dictionary"]),
    ]


def configure_command() -> list[str]:
    """构造低预算 MPS 管线配置命令。

    `--config` 后的参数是对官方默认配置的最小覆盖：减少计算量、关闭混合精度，
    并把责任基序/过滤规则明确切换为 antibody 模式。
    """

    return [
        str(BOLTZGEN),
        "configure",
        str(DESIGN_SPEC),
        "--output",
        str(PIPELINE_OUTPUT),
        "--protocol",
        "nanobody-anything",
        "--num_designs",
        "2",
        "--budget",
        "1",
        "--diffusion_batch_size",
        "1",
        "--devices",
        "1",
        "--num_workers",
        "1",
        "--use_kernels",
        "false",
        "--moldir",
        str(RUNTIME_FILES["molecule_dictionary"]),
        "--design_checkpoints",
        str(RUNTIME_FILES["design_diverse"]),
        "--inverse_fold_checkpoint",
        str(RUNTIME_FILES["inverse_fold"]),
        "--folding_checkpoint",
        str(RUNTIME_FILES["folding"]),
        "--config",
        "design",
        "sampling_steps=50",
        "recycling_steps=1",
        "trainer.precision=32",
        "--config",
        "inverse_folding",
        "sampling_steps=30",
        "recycling_steps=1",
        "trainer.precision=32",
        "--config",
        "folding",
        "sampling_steps=50",
        "recycling_steps=1",
        "diffusion_samples=1",
        "trainer.precision=32",
        "--config",
        "analysis",
        "liability_modality=antibody",
        "num_processes=1",
        "--config",
        "filtering",
        "modality=antibody",
        "filter_bindingsite=true",
    ]


def execute_command() -> list[str]:
    """执行已冻结的五步管线，而不是在执行时重新生成配置。"""

    return [str(BOLTZGEN), "execute", str(PIPELINE_OUTPUT)]


def parse_args() -> argparse.Namespace:
    """允许逐阶段调试，同时默认完成整个 MVP。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--through",
        choices=("preflight", "check", "configure", "execute"),
        default="execute",
        help="运行到哪个阶段为止；默认执行完整推理管线。",
    )
    return parser.parse_args()


def main() -> int:
    """按固定顺序执行预检、结构检查、配置和真实模型推理。"""

    args = parse_args()
    RUN_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    environment = make_environment()
    stages: list[dict[str, object]] = []

    # 阶段 0：任何模型计算前先证明输入与权重没有损坏。
    manifest = validate_inputs()
    status: dict[str, object] = {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "started_at_utc": utc_now(),
        "requested_through": args.through,
        "status": "RUNNING",
        "stages": stages,
    }
    write_json(STATUS_PATH, status)
    if args.through == "preflight":
        status.update(status="PREFLIGHT_COMPLETE", finished_at_utc=utc_now())
        write_json(STATUS_PATH, status)
        return 0

    try:
        stages.append(run_logged("01_check", check_command(), environment))
        write_json(STATUS_PATH, status)
        if args.through == "check":
            status.update(status="CHECK_COMPLETE", finished_at_utc=utc_now())
            write_json(STATUS_PATH, status)
            return 0

        stages.append(run_logged("02_configure", configure_command(), environment))
        write_json(STATUS_PATH, status)
        if args.through == "configure":
            status.update(status="CONFIGURE_COMPLETE", finished_at_utc=utc_now())
            write_json(STATUS_PATH, status)
            return 0

        stages.append(run_logged("03_execute", execute_command(), environment))
        status.update(status="PIPELINE_COMPLETE", finished_at_utc=utc_now())
        write_json(STATUS_PATH, status)
        return 0
    except Exception as exc:
        # 失败也属于真实实验结果；写明类型和消息，便于移植到 CUDA 机器后续跑。
        status.update(
            status="FAILED",
            finished_at_utc=utc_now(),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        write_json(STATUS_PATH, status)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
