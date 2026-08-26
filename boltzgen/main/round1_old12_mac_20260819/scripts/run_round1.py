#!/usr/bin/env python3
"""顺序运行旧 12 个 VHH 骨架的第一轮 BoltzGen 候选生成。

每个骨架是一个独立任务：先做输入检查，再冻结配置，最后执行
``design -> inverse_folding -> folding -> analysis -> filtering``。脚本不会并行加载
多个大模型，适合当前 18 GB 统一内存的 Apple Silicon 机器。每个尝试都有独立目录，
失败不会覆盖旧日志；再次执行时会跳过已经完整完成且结果表可解析的骨架。

注意：这里调用预训练权重进行推理，不更新模型参数，不属于基础模型再训练。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RUN_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = RUN_ROOT.parent
MANIFEST_PATH = RUN_ROOT / "provenance" / "input_manifest.json"

# 可执行文件来自已经验证的 MPS 环境；实际导入的 BoltzGen 源码由 PYTHONPATH 指向本轮快照。
MVP_ROOT = DATA_ROOT / "mvp_run_001"
PYTHON = MVP_ROOT / "env" / "bin" / "python"
BOLTZGEN = MVP_ROOT / "env" / "bin" / "boltzgen"
VENDOR_SRC = RUN_ROOT / "vendor" / "boltzgen_mps_pr145" / "src"

RUNTIME_CACHE = DATA_ROOT / "mvp_assets_v0.3.2" / "runtime_cache"
RUNTIME_FILES = {
    "design_diverse": RUNTIME_CACHE / "boltzgen1_diverse.ckpt",
    "inverse_fold": RUNTIME_CACHE / "boltzgen1_ifold.ckpt",
    "folding": RUNTIME_CACHE / "boltz2_conf_final.ckpt",
    "molecule_dictionary": RUNTIME_CACHE / "mols.zip",
}

GLOBAL_STATUS_PATH = RUN_ROOT / "runs" / "round1_status.json"


def utc_now() -> str:
    """返回带时区的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    """稳定写出状态 JSON；先写临时文件再替换，减少中断时的半文件风险。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算输入或模型文件的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_display(command: Iterable[str]) -> str:
    """把安全参数列表转换为便于复制阅读的 shell 命令。"""

    return " ".join(shlex.quote(str(part)) for part in command)


def make_environment() -> dict[str, str]:
    """构造离线、单任务、实验性 MPS 运行环境。"""

    environment = os.environ.copy()

    # 让 console script 优先导入本轮文件夹内冻结的 MPS 源码，而非外部可编辑路径。
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(VENDOR_SRC) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )

    # MPS 尚不支持的个别 PyTorch 算子允许回退到 CPU；日志会保留相应警告。
    environment["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
    environment["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    environment["OMP_NUM_THREADS"] = "4"
    environment["MKL_NUM_THREADS"] = "4"
    environment["PYTHONUNBUFFERED"] = "1"

    # 所有权重和化学组分字典已下载；强制离线可证明运行不会静默拉取新版本。
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["HF_DATASETS_OFFLINE"] = "1"
    environment["WANDB_MODE"] = "offline"
    return environment


def load_manifest() -> dict[str, Any]:
    """载入准备阶段清单，并检查关键文件仍存在。"""

    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"缺少 {MANIFEST_PATH}；请先运行 scripts/prepare_round1.py"
        )
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    records = manifest["scaffold_population"]["records"]
    if len(records) != 12:
        raise ValueError("输入清单中的 scaffold records 不是 12 条")
    if manifest["generation_budget"]["requested_total_designs"] != 24:
        raise ValueError("输入清单的请求候选总数不是 24")
    return manifest


def validate_runtime(manifest: dict[str, Any]) -> None:
    """运行开始前重新核对代码入口、MPS 可用性和四个运行资产哈希。"""

    required = [PYTHON, BOLTZGEN, VENDOR_SRC, *RUNTIME_FILES.values()]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少运行时文件：\n" + "\n".join(missing))

    expected = {row["asset"]: row["sha256"] for row in manifest["runtime"]["assets"]}
    for name, path in RUNTIME_FILES.items():
        observed = sha256_file(path)
        if observed != expected[name]:
            raise ValueError(f"运行资产 {name} 的 SHA-256 在准备后发生变化")

    # 单独子进程核对实际导入路径和 MPS 状态，防止 PYTHONPATH 没有生效。
    probe = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "import boltzgen, json, torch; "
                "print(json.dumps({'boltzgen': boltzgen.__file__, "
                "'torch': torch.__version__, "
                "'mps_built': torch.backends.mps.is_built(), "
                "'mps_available': torch.backends.mps.is_available()}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=make_environment(),
    )
    payload = json.loads(probe.stdout.strip())
    if not payload["mps_built"] or not payload["mps_available"]:
        raise RuntimeError(f"当前 PyTorch MPS 不可用：{payload}")
    if str(VENDOR_SRC) not in payload["boltzgen"]:
        raise RuntimeError(f"BoltzGen 未从本轮冻结源码导入：{payload['boltzgen']}")
    write_json(RUN_ROOT / "provenance" / "runtime_preflight.json", payload)


def read_ranked_rows(path: Path) -> list[dict[str, str]]:
    """读取最终排名表；不存在或损坏时返回空列表。"""

    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError):
        return []
    return rows if all(row.get("id") for row in rows) else []


def summarize_pipeline_outputs(pipeline: Path) -> dict[str, int]:
    """统计每个主阶段真实落盘的候选数量。

    ``all_designs_metrics.csv`` 可能只剩 1 行，因为 v0.3.2 会按设计位点序列去重；
    这不应被误报为模型没生成第 2 个结构。因此完成门同时检查上游两两配对文件。
    """

    design_dir = pipeline / "intermediate_designs"
    inverse_dir = pipeline / "intermediate_designs_inverse_folded"

    def paired_stem_count(directory: Path) -> int:
        cif_stems = {path.stem for path in directory.glob("*.cif")}
        npz_stems = {path.stem for path in directory.glob("*.npz")}
        return len(cif_stems & npz_stems)

    ranked_rows = read_ranked_rows(
        pipeline / "final_ranked_designs" / "all_designs_metrics.csv"
    )
    return {
        "design_cif_npz_pairs": paired_stem_count(design_dir),
        "inverse_folded_cif_npz_pairs": paired_stem_count(inverse_dir),
        "fold_output_npz": len(list((inverse_dir / "fold_out_npz").glob("*.npz"))),
        "refold_cif": len(list((inverse_dir / "refold_cif").glob("*.cif"))),
        "ranked_unique_rows": len(ranked_rows),
    }


def pipeline_outputs_are_complete(pipeline: Path) -> bool:
    """确认两个请求候选均完成主阶段，最终排名表至少保留一个唯一序列。"""

    counts = summarize_pipeline_outputs(pipeline)
    return (
        counts["design_cif_npz_pairs"] == 2
        and counts["inverse_folded_cif_npz_pairs"] == 2
        and counts["fold_output_npz"] == 2
        and counts["refold_cif"] == 2
        and 1 <= counts["ranked_unique_rows"] <= 2
    )


def latest_complete_attempt(task_root: Path) -> Path | None:
    """查找最近一个真正包含两行结果且状态为完成的 attempt。"""

    for attempt in sorted(task_root.glob("attempt_*"), reverse=True):
        status_path = attempt / "run_status.json"
        results_path = attempt / "pipeline" / "final_ranked_designs" / "all_designs_metrics.csv"
        if not status_path.exists() or not pipeline_outputs_are_complete(attempt / "pipeline"):
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if status.get("status") == "PIPELINE_COMPLETE":
            return attempt
    return None


def next_attempt_dir(task_root: Path) -> Path:
    """为失败重跑分配新 attempt 编号，永不覆盖旧过程证据。"""

    existing = []
    for path in task_root.glob("attempt_*"):
        try:
            existing.append(int(path.name.split("_")[-1]))
        except ValueError:
            continue
    number = max(existing, default=0) + 1
    attempt = task_root / f"attempt_{number:03d}"
    attempt.mkdir(parents=True, exist_ok=False)
    return attempt


def run_logged(
    stage: str,
    command: list[str],
    attempt: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    """执行一个阶段，同时把合并日志实时显示并完整落盘。"""

    log_dir = attempt / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{stage}.log"
    started_at = utc_now()
    start_clock = time.monotonic()

    print(f"\n[{attempt.parent.name} / {stage}] {command_display(command)}", flush=True)
    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"stage: {stage}\n")
        log_handle.write(f"started_at_utc: {started_at}\n")
        log_handle.write(f"working_directory: {RUN_ROOT}\n")
        log_handle.write(f"command: {command_display(command)}\n\n")
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
        "command_display": command_display(command),
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "return_code": return_code,
        "log_path": log_path.relative_to(RUN_ROOT).as_posix(),
    }
    if return_code != 0:
        raise RuntimeError(f"{stage} 失败，退出码 {return_code}；详见 {log_path}")
    return result


def check_command(spec: Path, attempt: Path) -> list[str]:
    """构造不加载神经网络权重的输入合同检查命令。"""

    return [
        str(BOLTZGEN),
        "check",
        str(spec),
        "--output",
        str(attempt / "input_check"),
        "--moldir",
        str(RUNTIME_FILES["molecule_dictionary"]),
    ]


def configure_command(spec: Path, attempt: Path) -> list[str]:
    """构造第一轮快速筛查配置命令。

    这里显式使用 antibody 责任基序与过滤口径，并开启 His7/Ala8 结合位点覆盖过滤。
    50/30/50 步和单个复折叠样本是成本受限的首轮设置，最终报告必须披露。
    """

    return [
        str(BOLTZGEN),
        "configure",
        str(spec),
        "--output",
        str(attempt / "pipeline"),
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


def execute_command(attempt: Path) -> list[str]:
    """执行已经冻结到 attempt/pipeline 的五步任务图。"""

    return [str(BOLTZGEN), "execute", str(attempt / "pipeline")]


def parse_args() -> argparse.Namespace:
    """解析可恢复、可分段的运行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-rank", type=int, default=1, help="首个骨架排名，默认 1")
    parser.add_argument("--end-rank", type=int, default=12, help="末个骨架排名，默认 12")
    parser.add_argument(
        "--force-new-attempt",
        action="store_true",
        help="即使已有完整结果也创建新 attempt；默认安全跳过已完成任务。",
    )
    return parser.parse_args()


def main() -> int:
    """顺序执行所有请求骨架，并持续写出全局状态。"""

    args = parse_args()
    if not (1 <= args.start_rank <= args.end_rank <= 12):
        raise ValueError("排名范围必须满足 1 <= start <= end <= 12")

    manifest = load_manifest()
    validate_runtime(manifest)
    environment = make_environment()
    records = manifest["scaffold_population"]["records"]
    selected_records = [
        row
        for row in records
        if args.start_rank <= int(row["selection_rank"]) <= args.end_rank
    ]

    global_status: dict[str, Any] = {
        "schema_version": "1.0.0",
        "campaign_id": manifest["campaign_id"],
        "execution_semantics": manifest["execution_semantics"],
        "started_at_utc": utc_now(),
        "requested_rank_range": [args.start_rank, args.end_rank],
        "status": "RUNNING",
        "tasks": [],
    }
    write_json(GLOBAL_STATUS_PATH, global_status)

    campaign_start = time.monotonic()
    for row in selected_records:
        rank = int(row["selection_rank"])
        task_name = f"{rank:02d}_{row['candidate_id']}"
        task_root = RUN_ROOT / "runs" / task_name
        task_root.mkdir(parents=True, exist_ok=True)

        complete_attempt = latest_complete_attempt(task_root)
        if complete_attempt is not None and not args.force_new_attempt:
            task_record = {
                "selection_rank": rank,
                "candidate_id": row["candidate_id"],
                "role": row["role"],
                "status": "SKIPPED_ALREADY_COMPLETE",
                "attempt": complete_attempt.relative_to(RUN_ROOT).as_posix(),
                "results_csv": (
                    complete_attempt
                    / "pipeline"
                    / "final_ranked_designs"
                    / "all_designs_metrics.csv"
                ).relative_to(RUN_ROOT).as_posix(),
            }
            global_status["tasks"].append(task_record)
            write_json(GLOBAL_STATUS_PATH, global_status)
            print(f"[{task_name}] 已有完整两候选结果，安全跳过。", flush=True)
            continue

        attempt = next_attempt_dir(task_root)
        spec = RUN_ROOT / row["design_spec"]
        status_path = attempt / "run_status.json"
        task_status: dict[str, Any] = {
            "schema_version": "1.0.0",
            "selection_rank": rank,
            "candidate_id": row["candidate_id"],
            "role": row["role"],
            "pdb_code": row["pdb_code"],
            "design_spec": spec.relative_to(RUN_ROOT).as_posix(),
            "design_spec_sha256": sha256_file(spec),
            "attempt": attempt.relative_to(RUN_ROOT).as_posix(),
            "requested_designs": 2,
            "final_display_budget": 1,
            "started_at_utc": utc_now(),
            "status": "RUNNING",
            "stages": [],
        }
        write_json(status_path, task_status)
        task_start = time.monotonic()

        try:
            task_status["stages"].append(
                run_logged("01_check", check_command(spec, attempt), attempt, environment)
            )
            write_json(status_path, task_status)

            task_status["stages"].append(
                run_logged(
                    "02_configure",
                    configure_command(spec, attempt),
                    attempt,
                    environment,
                )
            )
            write_json(status_path, task_status)

            task_status["stages"].append(
                run_logged("03_execute", execute_command(attempt), attempt, environment)
            )

            results_path = (
                attempt / "pipeline" / "final_ranked_designs" / "all_designs_metrics.csv"
            )
            output_counts = summarize_pipeline_outputs(attempt / "pipeline")
            if not pipeline_outputs_are_complete(attempt / "pipeline"):
                raise RuntimeError(
                    "管线退出码为 0，但主阶段输出数量不满足完成合同："
                    + json.dumps(output_counts, ensure_ascii=False, sort_keys=True)
                )

            task_status.update(
                status="PIPELINE_COMPLETE",
                finished_at_utc=utc_now(),
                elapsed_seconds=round(time.monotonic() - task_start, 3),
                results_csv=results_path.relative_to(RUN_ROOT).as_posix(),
                results_csv_sha256=sha256_file(results_path),
                output_counts=output_counts,
                ranked_row_note=(
                    "ranked_unique_rows 可能小于 2，因为 v0.3.2 会按设计位点序列去重"
                ),
            )
            write_json(status_path, task_status)
            print(f"[{task_name}] 完成：2 个候选已进入权威结果表。", flush=True)
        except Exception as exc:
            task_status.update(
                status="FAILED",
                finished_at_utc=utc_now(),
                elapsed_seconds=round(time.monotonic() - task_start, 3),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            write_json(status_path, task_status)
            global_status["tasks"].append(task_status)
            global_status.update(
                status="PARTIAL_FAILURE",
                finished_at_utc=utc_now(),
                elapsed_seconds=round(time.monotonic() - campaign_start, 3),
            )
            write_json(GLOBAL_STATUS_PATH, global_status)
            raise

        global_status["tasks"].append(task_status)
        write_json(GLOBAL_STATUS_PATH, global_status)

    completed = sum(
        row["status"] in {"PIPELINE_COMPLETE", "SKIPPED_ALREADY_COMPLETE"}
        for row in global_status["tasks"]
    )
    global_status.update(
        status="PIPELINE_COMPLETE" if completed == len(selected_records) else "PARTIAL",
        completed_task_count=completed,
        requested_task_count=len(selected_records),
        expected_candidate_count=completed * 2,
        finished_at_utc=utc_now(),
        elapsed_seconds=round(time.monotonic() - campaign_start, 3),
    )
    write_json(GLOBAL_STATUS_PATH, global_status)
    return 0 if completed == len(selected_records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
