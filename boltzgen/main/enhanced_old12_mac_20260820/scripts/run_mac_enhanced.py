#!/usr/bin/env python3
"""在 Apple Silicon Mac 上顺序执行可审计的 BoltzGen 增强候选生成。

核心保证：

* 每次启动有唯一 ``launch_id``，每次任务使用永不覆盖的 ``attempt_NNN``；
* 全局文件锁禁止两份大型 MPS 管线同时争用统一内存；
* ``design → inverse_folding → folding → analysis → filtering`` 五阶段分别启动，
  每阶段都有独立 stdout、stderr、合并 JSONL、状态、资源曲线和产物哈希；
* 新增或改变的阶段产物复制进按 SHA-256 寻址的只读快照仓库（CAS）；
* 每个用到的 checkpoint 在阶段前后重新计算 SHA-256，改变即判失败；
* 强制 Hugging Face、Transformers、Datasets 和 Weights & Biases 离线；
* SIGINT/SIGTERM 会转发给当前阶段进程组，保留失败状态，不把半成品冒充完成结果。

本脚本运行的是预训练权重推理，不更新模型权重。实验性 MPS 分支不等同于官方
Linux + NVIDIA CUDA 基线。
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import platform
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

import yaml
import numpy as np


RUN_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = RUN_ROOT.parent
MANIFEST_PATH = RUN_ROOT / "provenance" / "enhanced_input_manifest.json"
MVP_ROOT = DATA_ROOT / "mvp_run_001"
PYTHON = MVP_ROOT / "env" / "bin" / "python"
BOLTZGEN = MVP_ROOT / "env" / "bin" / "boltzgen"
VENDOR_SRC = RUN_ROOT / "vendor" / "boltzgen_mps_pr145" / "src"
MONITOR_SCRIPT = RUN_ROOT / "scripts" / "monitor_resources.py"
CAS_ROOT = RUN_ROOT / "snapshots" / "cas" / "sha256"

RUNTIME_CACHE = DATA_ROOT / "mvp_assets_v0.3.2" / "runtime_cache"
RUNTIME_FILES = {
    "design_diverse": RUNTIME_CACHE / "boltzgen1_diverse.ckpt",
    "design_adherence": RUNTIME_CACHE / "boltzgen1_adherence.ckpt",
    "inverse_fold": RUNTIME_CACHE / "boltzgen1_ifold.ckpt",
    "folding": RUNTIME_CACHE / "boltz2_conf_final.ckpt",
    "molecule_dictionary": RUNTIME_CACHE / "mols.zip",
}

PIPELINE_STAGES = ["design", "inverse_folding", "folding", "analysis", "filtering"]
STAGE_ASSETS = {
    "inverse_folding": ["inverse_fold"],
    "folding": ["folding"],
    "analysis": [],
    "filtering": [],
}

_CURRENT_PROCESS: subprocess.Popen[str] | None = None
_STOP_SIGNAL: int | None = None
_SIGNAL_COUNT = 0

# 只允许这些非敏感运行参数进入日志。绝不序列化完整 os.environ，因为其中可能有
# Hugging Face token、代理口令、云凭据或其他与本任务无关的秘密。
ENVIRONMENT_LOG_KEYS = (
    "PYTORCH_ENABLE_MPS_FALLBACK",
    "KMP_DUPLICATE_LIB_OK",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "PYTHONUNBUFFERED",
    "PYTHONPATH",
    "HF_HUB_OFFLINE",
    "HF_DATASETS_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "WANDB_MODE",
    "WANDB_DISABLED",
    "DO_NOT_TRACK",
    "TOKENIZERS_PARALLELISM",
)


def utc_now() -> str:
    """返回 UTC ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def new_launch_id() -> str:
    """生成可读且全局唯一的启动编号。"""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"mac-{stamp}-p{os.getpid()}-{uuid.uuid4().hex[:8]}"


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """分块计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON，避免突然中断产生截断文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def command_display(command: Iterable[str]) -> str:
    """把参数列表安全转换为便于阅读的 shell 形式。"""

    return " ".join(shlex.quote(str(part)) for part in command)


class EventWriter:
    """把结构化事件同时写入启动总日志和当前 attempt 日志。"""

    def __init__(self, launch_id: str, launch_dir: Path) -> None:
        self.launch_id = launch_id
        self.path = launch_dir / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: str, payload: dict[str, Any] | None = None, attempt: Path | None = None) -> None:
        """追加一个带时间和 launch_id 的 JSONL 事件。"""

        record = {
            "timestamp_utc": utc_now(),
            "launch_id": self.launch_id,
            "event": event,
            **(payload or {}),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            if attempt is not None:
                attempt_path = attempt / "events.jsonl"
                with attempt_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()


class CampaignLock:
    """对整个增强目录持有非阻塞排他锁。"""

    def __init__(self, launch_id: str) -> None:
        self.launch_id = launch_id
        self.path = RUN_ROOT / ".locks" / "run_mac_enhanced.lock"
        self.handle: TextIO | None = None

    def __enter__(self) -> "CampaignLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.seek(0)
            owner = self.handle.read().strip() or "未知持有者"
            raise RuntimeError(f"已有增强管线持有运行锁：{owner}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(
            json.dumps(
                {
                    "launch_id": self.launch_id,
                    "pid": os.getpid(),
                    "hostname": platform.node(),
                    "acquired_at_utc": utc_now(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.flush()
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def signal_handler(signum: int, frame: Any) -> None:
    """转发终止信号；第二次信号强制杀死当前阶段进程组。"""

    del frame
    global _STOP_SIGNAL, _SIGNAL_COUNT
    _STOP_SIGNAL = signum
    _SIGNAL_COUNT += 1
    process = _CURRENT_PROCESS
    if process is not None and process.poll() is None:
        forwarded = signal.SIGKILL if _SIGNAL_COUNT >= 2 else signum
        try:
            os.killpg(process.pid, forwarded)
        except ProcessLookupError:
            pass


def signal_metadata(signum: int | None = None) -> dict[str, Any]:
    """把终止信号转换为稳定、可读的状态字段。"""

    value = _STOP_SIGNAL if signum is None else signum
    if value is None:
        return {"termination_signal": None, "signal_name": None}
    try:
        name = signal.Signals(value).name
    except ValueError:
        name = f"SIGNAL_{value}"
    return {"termination_signal": int(value), "signal_name": name}


def make_environment() -> dict[str, str]:
    """构造冻结源码、严格离线、单任务 MPS 环境。"""

    environment = os.environ.copy()
    previous_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(VENDOR_SRC) + (
        os.pathsep + previous_pythonpath if previous_pythonpath else ""
    )
    environment.update(
        {
            "PYTORCH_ENABLE_MPS_FALLBACK": "1",
            "KMP_DUPLICATE_LIB_OK": "TRUE",
            "OMP_NUM_THREADS": "4",
            "MKL_NUM_THREADS": "4",
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "WANDB_MODE": "offline",
            "WANDB_DISABLED": "true",
            "DO_NOT_TRACK": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    return environment


def environment_log_snapshot(environment: dict[str, str]) -> dict[str, str | None]:
    """返回严格白名单环境快照；缺失键显式记为 null。"""

    return {key: environment.get(key) for key in ENVIRONMENT_LOG_KEYS}


def load_manifest() -> dict[str, Any]:
    """读取增强输入清单并检查基本结构。"""

    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"缺少 {MANIFEST_PATH}；请先运行 scripts/prepare_mac_enhanced.py"
        )
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if len(manifest["scaffold_population"]["records"]) != 12:
        raise ValueError("增强输入清单不是旧 12 骨架")
    if set(manifest["profiles"]) != {
        "balanced_all12",
        "balanced_diverse_all12",
        "balanced_adherence_all12",
        "near_official_adherence_7xl0",
        "full_depth_probe",
        "full_depth_probe_samples2",
    }:
        raise ValueError("增强输入清单的 profile 集合不完整")
    return manifest


def asset_expected_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    """从冻结清单提取运行资产的权威哈希。"""

    return {row["asset"]: row["sha256"] for row in manifest["runtime"]["assets"]}


def hash_assets(names: Iterable[str], expected: dict[str, str]) -> list[dict[str, Any]]:
    """实际重读指定 checkpoint，并拒绝任何前后变化。"""

    records = []
    for name in names:
        path = RUNTIME_FILES[name]
        observed = sha256_file(path)
        if observed != expected[name]:
            raise ValueError(f"运行资产 {name} 的 SHA-256 与冻结合同不符")
        records.append(
            {
                "asset": name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": observed,
            }
        )
    return records


def command_output(command: list[str]) -> str | None:
    """读取非关键系统信息；命令不可用时返回空值而非伪造。"""

    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def runtime_preflight(
    manifest: dict[str, Any], profile_name: str, launch_id: str, launch_dir: Path
) -> dict[str, Any]:
    """执行不加载模型权重的运行前检查，并写出完整证据。"""

    profile = manifest["profiles"][profile_name]
    if profile.get("single_checkpoint_required") and profile["design_checkpoints"] != [
        "design_adherence"
    ]:
        raise RuntimeError(
            "单 checkpoint 安全门失败：近官方 7XL0 档位只能使用 design_adherence"
        )
    required = [PYTHON, BOLTZGEN, VENDOR_SRC, MONITOR_SCRIPT, *RUNTIME_FILES.values()]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少运行时文件：\n" + "\n".join(missing))

    # 冻结输入全部重算哈希；总量很小，不依赖修改时间等弱证据。
    frozen_checks = []
    for row in manifest["frozen_files"]:
        path = RUN_ROOT / row["path"]
        if not path.exists():
            raise FileNotFoundError(f"冻结文件缺失：{path}")
        observed = sha256_file(path)
        if observed != row["sha256"]:
            raise ValueError(f"冻结文件在准备后变化：{path}")
        frozen_checks.append({"path": row["path"], "sha256": observed})

    expected = asset_expected_hashes(manifest)
    runtime_checks = hash_assets(RUNTIME_FILES.keys(), expected)
    environment = make_environment()
    probe = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "import boltzgen,json,torch;"
                "print(json.dumps({'boltzgen':boltzgen.__file__,'torch':torch.__version__,"
                "'mps_built':torch.backends.mps.is_built(),"
                "'mps_available':torch.backends.mps.is_available()}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    torch_probe = json.loads(probe.stdout.strip())
    if not torch_probe["mps_built"] or not torch_probe["mps_available"]:
        raise RuntimeError(f"当前 PyTorch MPS 不可用：{torch_probe}")
    if str(VENDOR_SRC) not in torch_probe["boltzgen"]:
        raise RuntimeError(f"BoltzGen 未从冻结源码导入：{torch_probe['boltzgen']}")

    disk = shutil.disk_usage(RUN_ROOT)
    free_gib = disk.free / 1024**3
    if free_gib < profile["minimum_free_disk_gib"]:
        raise RuntimeError(
            f"可用空间 {free_gib:.2f} GiB 低于 {profile['minimum_free_disk_gib']} GiB 安全门"
        )
    memory_bytes_text = command_output(["sysctl", "-n", "hw.memsize"])
    # 运行代码本身不是模型输入，但它决定日志、恢复和完成门语义；因此每次 launch
    # 都保存真实字节指纹。最终封版还会再生成覆盖全部交付物的 SHA256SUMS。
    execution_code = []
    for path in sorted((RUN_ROOT / "scripts").glob("*.py")):
        execution_code.append(
            {
                "path": path.relative_to(RUN_ROOT).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    preflight = {
        "schema_version": "2.0.0",
        "launch_id": launch_id,
        "checked_at_utc": utc_now(),
        "profile": profile_name,
        "no_model_weights_loaded": True,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "macos_product_version": command_output(["sw_vers", "-productVersion"]),
        "hardware_memory_bytes": int(memory_bytes_text) if memory_bytes_text else None,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "disk_free_gib": round(free_gib, 3),
        "minimum_free_disk_gib": profile["minimum_free_disk_gib"],
        "torch_probe": torch_probe,
        "runtime_assets": runtime_checks,
        "execution_code": execution_code,
        "frozen_file_count": len(frozen_checks),
        "frozen_files": frozen_checks,
        "execution_environment": environment_log_snapshot(environment),
        "environment_logging_policy": (
            "strict_allowlist_only; full process environment and secret-bearing variables are never persisted"
        ),
        "offline_environment": {
            key: environment[key]
            for key in (
                "HF_HUB_OFFLINE",
                "HF_DATASETS_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "WANDB_MODE",
                "WANDB_DISABLED",
                "HF_HUB_DISABLE_TELEMETRY",
            )
        },
        "status": "PASS",
    }
    path = RUN_ROOT / "provenance" / "preflight" / f"{launch_id}.json"
    atomic_write_json(path, preflight)
    atomic_write_json(launch_dir / "preflight.json", preflight)
    return preflight


def next_attempt_dir(task_root: Path) -> Path:
    """创建下一个 attempt 目录，永不覆盖任何旧尝试。"""

    numbers = []
    for path in task_root.glob("attempt_*"):
        try:
            numbers.append(int(path.name.split("_")[-1]))
        except ValueError:
            continue
    attempt = task_root / f"attempt_{max(numbers, default=0) + 1:03d}"
    attempt.mkdir(parents=True, exist_ok=False)
    return attempt


def latest_complete_attempt(task_root: Path) -> Path | None:
    """返回最近一个真正完成五阶段的尝试。"""

    for attempt in sorted(task_root.glob("attempt_*"), reverse=True):
        status_path = attempt / "run_status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if status.get("status") == "PIPELINE_COMPLETE" and status.get(
            "completed_pipeline_stages"
        ) == PIPELINE_STAGES:
            return attempt
    return None


def collect_file_manifest(root: Path) -> dict[str, dict[str, Any]]:
    """对管线目录的全部普通文件建立强哈希清单。"""

    records: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return records
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        records[relative] = {
            "relative_path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return records


def put_cas(source: Path, sha256: str) -> Path:
    """把一个阶段产物复制到 SHA-256 内容寻址、只读的快照仓库。"""

    destination = CAS_ROOT / sha256[:2] / sha256
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != sha256:
            raise RuntimeError(f"CAS 中同名对象内容错误：{destination}")
        return destination
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    shutil.copy2(source, temporary)
    if sha256_file(temporary) != sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"CAS 复制后哈希不一致：{source}")
    temporary.replace(destination)
    destination.chmod(0o444)
    return destination


def snapshot_stage_delta(
    pipeline: Path,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    attempt: Path,
    stage: str,
    launch_id: str,
) -> dict[str, Any]:
    """保存本阶段新增/修改文件的清单与内容寻址快照。"""

    changed = []
    for relative, record in after.items():
        if relative in before and before[relative]["sha256"] == record["sha256"]:
            continue
        source = pipeline / relative
        cas_path = put_cas(source, record["sha256"])
        changed.append(
            {
                **record,
                "change": "modified" if relative in before else "created",
                "cas_path": cas_path.relative_to(RUN_ROOT).as_posix(),
            }
        )
    removed = sorted(set(before) - set(after))
    manifest = {
        "schema_version": "1.0.0",
        "launch_id": launch_id,
        "attempt": attempt.relative_to(RUN_ROOT).as_posix(),
        "stage": stage,
        "created_at_utc": utc_now(),
        "pipeline_file_count_before": len(before),
        "pipeline_file_count_after": len(after),
        "changed_file_count": len(changed),
        "changed_total_bytes": sum(row["size_bytes"] for row in changed),
        "removed_paths": removed,
        "files": changed,
        "cas_semantics": "immutable byte copy keyed by SHA-256; original path retained in this manifest",
    }
    path = attempt / "manifests" / f"{stage}.json"
    atomic_write_json(path, manifest)
    return {**manifest, "manifest_path": path.relative_to(RUN_ROOT).as_posix()}


def start_stream_thread(
    stream: TextIO, stream_name: str, output_queue: queue.Queue[tuple[int, str, str | None]]
) -> threading.Thread:
    """后台逐行读取 stdout 或 stderr，防止任一管道写满造成死锁。"""

    def pump() -> None:
        try:
            for line in stream:
                output_queue.put((time.monotonic_ns(), stream_name, line))
        finally:
            output_queue.put((time.monotonic_ns(), stream_name, None))

    thread = threading.Thread(target=pump, name=f"stream-{stream_name}", daemon=True)
    thread.start()
    return thread


def read_monitor_summary(stage_dir: Path) -> dict[str, Any] | None:
    """读取监控器主动刷新的摘要；损坏时返回空值并由调用者降级记录。"""

    path = stage_dir / "resources.summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def finalize_monitor_process(
    monitor: subprocess.Popen[str] | None,
    stage_dir: Path,
    *,
    graceful_timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """等待隔离的监控器刷新；超时才发 SIGTERM，并记录可靠退出状态。"""

    if monitor is None:
        return {
            "monitor_pid": None,
            "monitor_return_code": None,
            "monitor_status": "NOT_STARTED",
            "monitor_first_sample_at_utc": None,
            "monitor_last_sample_at_utc": None,
            "monitor_sample_count": 0,
        }
    termination_requested = False
    force_killed = False
    try:
        return_code = monitor.wait(timeout=graceful_timeout_seconds)
    except subprocess.TimeoutExpired:
        termination_requested = True
        # monitor_resources.py 捕获 SIGTERM，只在当前采样完成后刷盘并返回 0。
        monitor.terminate()
        try:
            return_code = monitor.wait(timeout=4)
        except subprocess.TimeoutExpired:
            force_killed = True
            monitor.kill()
            return_code = monitor.wait(timeout=3)
    summary = read_monitor_summary(stage_dir)
    if force_killed:
        monitor_status = "FORCE_KILLED"
    elif return_code != 0:
        monitor_status = "FAILED"
    elif summary is None:
        monitor_status = "COMPLETE_WITHOUT_SUMMARY"
    elif termination_requested:
        monitor_status = "GRACEFUL_TERMINATION_AFTER_TIMEOUT"
    elif int(summary.get("sample_count", 0)) == 0:
        monitor_status = "COMPLETE_NO_SAMPLES"
    else:
        monitor_status = "COMPLETE"
    return {
        "monitor_pid": monitor.pid,
        "monitor_return_code": return_code,
        "monitor_status": monitor_status,
        "monitor_first_sample_at_utc": (
            summary.get("first_sample_at_utc") if summary else None
        ),
        "monitor_last_sample_at_utc": (
            summary.get("last_sample_at_utc") if summary else None
        ),
        "monitor_sample_count": int(summary.get("sample_count", 0)) if summary else None,
        "monitor_exit_reason": summary.get("exit_reason") if summary else None,
        "monitor_summary_path": (
            (stage_dir / "resources.summary.json").relative_to(RUN_ROOT).as_posix()
            if summary
            else None
        ),
        "monitor_signal_isolation": "separate_process_group_start_new_session",
    }


def run_logged_command(
    *,
    stage: str,
    command: list[str],
    attempt: Path,
    environment: dict[str, str],
    events: EventWriter,
    manifest: dict[str, Any],
    pipeline: Path | None = None,
    checkpoint_names: list[str] | None = None,
) -> dict[str, Any]:
    """执行一个命令并完整分流日志、资源、状态、checkpoint 与产物快照。"""

    global _CURRENT_PROCESS
    checkpoint_names = checkpoint_names or []
    expected_hashes = asset_expected_hashes(manifest)
    stage_dir = attempt / "logs" / stage
    stage_dir.mkdir(parents=True, exist_ok=False)
    status_path = attempt / "stage_status" / f"{stage}.json"
    status: dict[str, Any] = {
        "schema_version": "2.0.0",
        "launch_id": events.launch_id,
        "attempt": attempt.relative_to(RUN_ROOT).as_posix(),
        "stage": stage,
        "status": "RUNNING",
        "started_at_utc": utc_now(),
        "command": command,
        "command_display": command_display(command),
        "working_directory": str(RUN_ROOT),
        "log_directory": stage_dir.relative_to(RUN_ROOT).as_posix(),
        "execution_environment": environment_log_snapshot(environment),
        "environment_logging_policy": "strict_allowlist_only_no_secrets",
        "checkpoint_pre_sha256": hash_assets(checkpoint_names, expected_hashes),
        "checkpoint_note": (
            "not_applicable_no_model_checkpoint_consumed"
            if not checkpoint_names
            else "rehashed_from_bytes_before_and_after_stage"
        ),
    }
    atomic_write_json(status_path, status)
    atomic_write_json(stage_dir / "command.json", status)
    before = collect_file_manifest(pipeline) if pipeline is not None else {}
    events.emit("stage_started", {"stage": stage, "command": command}, attempt)
    start_clock = time.monotonic()

    stdout_raw = (stage_dir / "stdout.log").open("w", encoding="utf-8")
    stderr_raw = (stage_dir / "stderr.log").open("w", encoding="utf-8")
    stdout_json = (stage_dir / "stdout.jsonl").open("w", encoding="utf-8")
    stderr_json = (stage_dir / "stderr.jsonl").open("w", encoding="utf-8")
    combined_json = (stage_dir / "combined.jsonl").open("w", encoding="utf-8")
    monitor: subprocess.Popen[str] | None = None
    monitor_record: dict[str, Any] | None = None
    monitor_stderr_handle: TextIO | None = None
    process: subprocess.Popen[str] | None = None
    return_code: int | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=RUN_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        _CURRENT_PROCESS = process
        status["pid"] = process.pid
        atomic_write_json(status_path, status)
        monitor_stderr_handle = (stage_dir / "resource_monitor.stderr.log").open(
            "w", encoding="utf-8"
        )
        monitor = subprocess.Popen(
            [
                sys.executable,
                str(MONITOR_SCRIPT),
                "--root-pid",
                str(process.pid),
                "--launch-id",
                events.launch_id,
                "--attempt",
                attempt.relative_to(RUN_ROOT).as_posix(),
                "--stage",
                stage,
                "--output-prefix",
                str(stage_dir / "resources"),
            ],
            cwd=RUN_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=monitor_stderr_handle,
            text=True,
            # 与主终端和模型阶段使用不同进程组。用户按 Control-C 时只有运行器收到
            # 终端信号；运行器转发给模型阶段，监控器则继续刷新到根进程消失。
            start_new_session=True,
        )
        status.update(
            monitor_pid=monitor.pid,
            monitor_status="RUNNING",
            monitor_signal_isolation="separate_process_group_start_new_session",
        )
        atomic_write_json(status_path, status)
        assert process.stdout is not None and process.stderr is not None
        output_queue: queue.Queue[tuple[int, str, str | None]] = queue.Queue()
        threads = [
            start_stream_thread(process.stdout, "stdout", output_queue),
            start_stream_thread(process.stderr, "stderr", output_queue),
        ]
        closed_streams: set[str] = set()
        sequence = 0
        while len(closed_streams) < 2:
            try:
                monotonic_ns, stream_name, line = output_queue.get(timeout=0.5)
            except queue.Empty:
                if _STOP_SIGNAL is not None and process.poll() is not None:
                    continue
                continue
            if line is None:
                closed_streams.add(stream_name)
                continue
            sequence += 1
            record = {
                "sequence": sequence,
                "timestamp_utc": utc_now(),
                "monotonic_ns": monotonic_ns,
                "launch_id": events.launch_id,
                "stage": stage,
                "stream": stream_name,
                "text": line.rstrip("\r\n"),
            }
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            combined_json.write(encoded)
            if stream_name == "stdout":
                stdout_raw.write(line)
                stdout_json.write(encoded)
                print(line, end="", flush=True)
            else:
                stderr_raw.write(line)
                stderr_json.write(encoded)
                print(line, end="", file=sys.stderr, flush=True)
            stdout_raw.flush()
            stderr_raw.flush()
            stdout_json.flush()
            stderr_json.flush()
            combined_json.flush()
        for thread in threads:
            thread.join(timeout=2)
        return_code = process.wait()
        monitor_record = finalize_monitor_process(monitor, stage_dir)
        status.update(monitor_record)
        _CURRENT_PROCESS = None

        status.update(
            return_code=return_code,
            process_finished_at_utc=utc_now(),
            elapsed_seconds=round(time.monotonic() - start_clock, 3),
        )
        if _STOP_SIGNAL is not None:
            raise InterruptedError(f"收到 {signal_metadata()['signal_name']}，阶段已停止")
        if return_code != 0:
            raise RuntimeError(f"{stage} 退出码 {return_code}；详见 {stage_dir}")

        checkpoint_post = hash_assets(checkpoint_names, expected_hashes)
        if [row["sha256"] for row in status["checkpoint_pre_sha256"]] != [
            row["sha256"] for row in checkpoint_post
        ]:
            raise RuntimeError(f"{stage}: checkpoint 在阶段执行期间发生变化")
        after = collect_file_manifest(pipeline) if pipeline is not None else {}
        artifact_manifest = (
            snapshot_stage_delta(
                pipeline, before, after, attempt, stage, events.launch_id
            )
            if pipeline is not None
            else None
        )
        status.update(
            # 退出码 0 只证明子进程结束。外层仍必须通过产物合同；design 还必须
            # 通过 checkpoint switch 合同，之后才能由 finalize_stage_status 升级。
            status="PROCESS_COMPLETE",
            finished_at_utc=utc_now(),
            elapsed_seconds=round(time.monotonic() - start_clock, 3),
            checkpoint_post_sha256=checkpoint_post,
            artifact_manifest=artifact_manifest,
            resource_csv=(stage_dir / "resources.csv").relative_to(RUN_ROOT).as_posix(),
            resource_jsonl=(stage_dir / "resources.jsonl").relative_to(RUN_ROOT).as_posix(),
        )
        atomic_write_json(status_path, status)
        atomic_write_json(stage_dir / "status.json", status)
        events.emit(
            "stage_process_finished",
            {
                "stage": stage,
                "return_code": return_code,
                "elapsed_seconds": status["elapsed_seconds"],
                "status": status["status"],
            },
            attempt,
        )
        return status
    except BaseException as exc:
        _CURRENT_PROCESS = None
        if monitor_record is None:
            monitor_record = finalize_monitor_process(monitor, stage_dir)
        interrupted = _STOP_SIGNAL is not None or isinstance(exc, InterruptedError)
        status.update(
            status="INTERRUPTED" if interrupted else "FAILED",
            finished_at_utc=utc_now(),
            elapsed_seconds=round(time.monotonic() - start_clock, 3),
            error_type=type(exc).__name__,
            error_message=str(exc),
            return_code=return_code,
            **monitor_record,
            **(signal_metadata() if interrupted else {}),
        )
        atomic_write_json(status_path, status)
        atomic_write_json(stage_dir / "status.json", status)
        events.emit(
            "stage_interrupted" if interrupted else "stage_failed",
            {
                "stage": stage,
                "status": status["status"],
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                **(signal_metadata() if interrupted else {}),
            },
            attempt,
        )
        raise
    finally:
        for handle in (stdout_raw, stderr_raw, stdout_json, stderr_json, combined_json):
            handle.close()
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if monitor is not None and monitor.poll() is None:
            finalize_monitor_process(monitor, stage_dir, graceful_timeout_seconds=0.5)
        if monitor_stderr_handle is not None:
            monitor_stderr_handle.close()


def finalize_stage_status(
    attempt: Path,
    stage: str,
    contracts: dict[str, dict[str, Any]],
    events: EventWriter,
) -> dict[str, Any]:
    """仅在全部外层合同 PASS 后把 PROCESS_COMPLETE 升级为 COMPLETE。"""

    status_path = attempt / "stage_status" / f"{stage}.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("status") != "PROCESS_COMPLETE":
        raise RuntimeError(
            f"{stage}: 只能从 PROCESS_COMPLETE 升级，实际为 {status.get('status')}"
        )
    failed = [name for name, contract in contracts.items() if contract.get("status") != "PASS"]
    if failed:
        raise RuntimeError(f"{stage}: 外层合同未通过：{failed}")
    status.update(
        status="COMPLETE",
        contract_status="PASS",
        contracts=contracts,
        contract_validated_at_utc=utc_now(),
    )
    atomic_write_json(status_path, status)
    atomic_write_json(attempt / "logs" / stage / "status.json", status)
    events.emit(
        "stage_contracts_passed",
        {"stage": stage, "status": "COMPLETE", "contracts": sorted(contracts)},
        attempt,
    )
    return status


def mark_stage_contract_failed(
    attempt: Path,
    stage: str,
    exc: BaseException,
    events: EventWriter,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> None:
    """把退出码 0 但合同失败的阶段明确标为 CONTRACT_FAILED。"""

    status_path = attempt / "stage_status" / f"{stage}.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    # 如果阶段在合同检查前已被中断/失败，保留更准确的原状态。
    if status.get("status") == "PROCESS_COMPLETE":
        status.update(
            status="CONTRACT_FAILED",
            contract_status="FAIL",
            contracts=contracts or {},
            contract_error_type=type(exc).__name__,
            contract_error_message=str(exc),
            contract_validated_at_utc=utc_now(),
        )
        atomic_write_json(status_path, status)
        atomic_write_json(attempt / "logs" / stage / "status.json", status)
    events.emit(
        "stage_contract_failed",
        {
            "stage": stage,
            "status": status.get("status"),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        },
        attempt,
    )


def validate_check_outputs(attempt: Path) -> dict[str, Any]:
    """确认 check 真的写出至少一个非空结构文件。"""

    files = [path for path in (attempt / "input_check").glob("*.cif") if path.stat().st_size > 0]
    if not files:
        raise RuntimeError("boltzgen check 退出码为0但没有非空 CIF 输出")
    return {
        "status": "PASS",
        "kind": "input_check_output_contract",
        "nonempty_cif_count": len(files),
        "files": [path.relative_to(RUN_ROOT).as_posix() for path in files],
    }


def check_command(spec: Path, attempt: Path) -> list[str]:
    """构造不加载权重的输入检查命令。"""

    return [
        str(BOLTZGEN),
        "check",
        str(spec),
        "--output",
        str(attempt / "input_check"),
        "--moldir",
        str(RUNTIME_FILES["molecule_dictionary"]),
    ]


def configure_command(spec: Path, attempt: Path, profile: dict[str, Any]) -> list[str]:
    """把 profile 全部显式展开为 BoltzGen 配置命令。"""

    command = [
        str(BOLTZGEN),
        "configure",
        str(spec),
        "--output",
        str(attempt / "pipeline"),
        "--protocol",
        "nanobody-anything",
        "--num_designs",
        str(profile["num_designs"]),
        "--budget",
        str(profile["budget"]),
        "--diffusion_batch_size",
        "1",
        "--inverse_fold_num_sequences",
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
        *[str(RUNTIME_FILES[name]) for name in profile["design_checkpoints"]],
        "--inverse_fold_checkpoint",
        str(RUNTIME_FILES["inverse_fold"]),
        "--folding_checkpoint",
        str(RUNTIME_FILES["folding"]),
        "--config",
        "design",
        f"sampling_steps={profile['design_sampling_steps']}",
        f"recycling_steps={profile['design_recycling_steps']}",
        f"trainer.precision={profile['precision']}",
        "--config",
        "inverse_folding",
        f"sampling_steps={profile['inverse_fold_sampling_steps']}",
        f"recycling_steps={profile['inverse_fold_recycling_steps']}",
        f"diffusion_samples={profile.get('inverse_fold_diffusion_samples', 1)}",
        f"trainer.precision={profile['precision']}",
        "--config",
        "folding",
        f"sampling_steps={profile['folding_sampling_steps']}",
        f"recycling_steps={profile['folding_recycling_steps']}",
        f"diffusion_samples={profile['folding_diffusion_samples']}",
        f"trainer.precision={profile['precision']}",
        "--config",
        "analysis",
        "liability_modality=antibody",
        "num_processes=1",
        "--config",
        "filtering",
        "modality=antibody",
        "filter_bindingsite=true",
    ]
    return command


def execute_stage_command(pipeline: Path, stage: str) -> list[str]:
    """只执行五阶段中的一个，确保日志和内存边界清楚。"""

    return [str(BOLTZGEN), "execute", str(pipeline), "--steps", stage]


def validate_resolved_config(attempt: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """逐字段核对 ``configure`` 的解析结果，防止命令行参数被忽略。"""

    config_dir = attempt / "pipeline" / "config"
    configs = {
        name: yaml.safe_load((config_dir / f"{name}.yaml").read_text(encoding="utf-8"))
        for name in PIPELINE_STAGES
    }
    design = configs["design"]
    inverse = configs["inverse_folding"]
    folding = configs["folding"]
    expected_first_checkpoint = RUNTIME_FILES[profile["design_checkpoints"][0]]
    checks = {
        "design_sampling_steps": design["sampling_steps"] == profile["design_sampling_steps"],
        "design_recycling_steps": design["recycling_steps"] == profile["design_recycling_steps"],
        "design_precision": str(design["trainer"]["precision"]) == profile["precision"],
        "design_multiplicity": design["data"]["cfg"]["multiplicity"] == profile["num_designs"],
        "design_batch_size": design["diffusion_samples"] == 1,
        "first_checkpoint": Path(design["checkpoint"]) == expected_first_checkpoint,
        "inverse_sampling_steps": inverse["sampling_steps"]
        == profile["inverse_fold_sampling_steps"],
        "inverse_recycling_steps": inverse["recycling_steps"]
        == profile["inverse_fold_recycling_steps"],
        "inverse_diffusion_samples": inverse["diffusion_samples"]
        == profile.get("inverse_fold_diffusion_samples", 1),
        "inverse_precision": str(inverse["trainer"]["precision"]) == profile["precision"],
        "folding_sampling_steps": folding["sampling_steps"] == profile["folding_sampling_steps"],
        "folding_recycling_steps": folding["recycling_steps"]
        == profile["folding_recycling_steps"],
        "folding_diffusion_samples": folding["diffusion_samples"]
        == profile["folding_diffusion_samples"],
        "folding_precision": str(folding["trainer"]["precision"]) == profile["precision"],
        "analysis_antibody": configs["analysis"]["liability_modality"] == "antibody",
        "filtering_antibody": configs["filtering"]["modality"] == "antibody",
        "filter_binding_site": configs["filtering"]["filter_bindingsite"] is True,
    }
    checkpoint_assignment: dict[str, str]
    if len(profile["design_checkpoints"]) == 2:
        checkpoint_cfg = design["override"]["checkpoints"]
        checks.update(
            first_checkpoint_fraction=checkpoint_cfg["first_checkpoint_num_samples"] == 0.5,
            second_checkpoint_fraction=(
                checkpoint_cfg["checkpoint_list"][0]["checkpoint"]["num_samples"] == 0.5
            ),
            second_checkpoint=(
                Path(checkpoint_cfg["checkpoint_list"][0]["checkpoint"]["path"])
                == RUNTIME_FILES[profile["design_checkpoints"][1]]
            ),
        )
        checkpoint_assignment = {
            "design_indices_0_1": profile["design_checkpoints"][0],
            "design_indices_2_3": profile["design_checkpoints"][1],
            "basis": "4 designs, diffusion_batch_size=1, two 0.5 checkpoint fractions",
        }
    else:
        checks["no_checkpoint_switch_configured"] = "checkpoints" not in design["override"]
        checkpoint_assignment = {
            "design_indices_0_1": profile["design_checkpoints"][0],
            "basis": "2 designs, diffusion_batch_size=1, one isolated checkpoint process",
        }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(f"解析配置合同失败：{failed}")
    contract = {
        "status": "PASS",
        "checked_at_utc": utc_now(),
        "checks": checks,
        "expected_checkpoint_assignment": checkpoint_assignment,
    }
    atomic_write_json(attempt / "provenance" / "resolved_config_contract.json", contract)
    return contract


def paired_count(directory: Path) -> int:
    """统计同时具有 CIF 坐标和 NPZ 元数据的候选 stem 数。"""

    return len(
        {path.stem for path in directory.glob("*.cif")}
        & {path.stem for path in directory.glob("*.npz")}
    )


def csv_row_count(path: Path) -> int:
    """读取 CSV 数据行数；缺失或空文件返回 0。"""

    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return len(list(csv.DictReader(handle)))


def validate_stage_outputs(stage: str, pipeline: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """对每阶段实施最低完整性门，不以退出码 0 代替真实产物检查。"""

    expected = profile["num_designs"]
    inverse_dir = pipeline / "intermediate_designs_inverse_folded"
    counts = {
        "design_pairs": paired_count(pipeline / "intermediate_designs"),
        "inverse_fold_pairs": paired_count(inverse_dir),
        "fold_npz": len(list((inverse_dir / "fold_out_npz").glob("*.npz"))),
        "refold_cif": len(list((inverse_dir / "refold_cif").glob("*.cif"))),
        # aggregate_metrics_analyze.csv 是逐候选表；per_target_metrics_analyze.csv
        # 按 target_id 聚合。同一骨架只有一个 target_id 时，后者通常只有 1 行，
        # 绝不能把它误当成候选数。
        "analysis_candidate_rows": csv_row_count(
            inverse_dir / "aggregate_metrics_analyze.csv"
        ),
        "analysis_target_aggregate_rows": csv_row_count(
            inverse_dir / "per_target_metrics_analyze.csv"
        ),
        "ranked_unique_rows": csv_row_count(
            pipeline / "final_ranked_designs" / "all_designs_metrics.csv"
        ),
    }
    if stage == "design" and counts["design_pairs"] != expected:
        raise RuntimeError(f"design 产物合同失败：{counts}")
    if stage == "inverse_folding" and counts["inverse_fold_pairs"] != expected:
        raise RuntimeError(f"inverse_folding 产物合同失败：{counts}")
    fold_sample_counts: list[int] = []
    if stage == "folding":
        if counts["fold_npz"] != expected or counts["refold_cif"] != expected:
            raise RuntimeError(f"folding 产物合同失败：{counts}")
        # writer 只把择优样本写成一个 refold CIF；全部样本仍在 NPZ 的
        # 第一轴。这里逐文件确认样本数、坐标和评分都与冻结配置一致。
        required_arrays = {
            "coords",
            "iptm",
            "ptm",
            "design_to_target_iptm",
            "design_ptm",
            "atom_to_token",
            "atom_resolved_mask",
        }
        for npz_path in sorted((inverse_dir / "fold_out_npz").glob("*.npz")):
            with np.load(npz_path, allow_pickle=False) as arrays:
                missing = sorted(required_arrays - set(arrays.files))
                if missing:
                    raise RuntimeError(f"fold NPZ 缺少数组 {missing}：{npz_path}")
                sample_count = int(np.asarray(arrays["iptm"]).shape[0])
                fold_sample_counts.append(sample_count)
                if sample_count != int(profile["folding_diffusion_samples"]):
                    raise RuntimeError(
                        f"fold NPZ 样本数不符：expected="
                        f"{profile['folding_diffusion_samples']}, observed={sample_count}, "
                        f"path={npz_path}"
                    )
                coords = np.asarray(arrays["coords"])
                if coords.ndim != 3 or coords.shape[0] != sample_count or coords.shape[2] != 3:
                    raise RuntimeError(f"fold coords 轴合同错误：{npz_path} {coords.shape}")
                if not np.isfinite(coords).all():
                    raise RuntimeError(f"fold coords 含 NaN/Inf：{npz_path}")
                for key in ("iptm", "ptm", "design_to_target_iptm", "design_ptm"):
                    array = np.asarray(arrays[key])
                    if array.shape != (sample_count,) or not np.isfinite(array).all():
                        raise RuntimeError(f"{key} 样本轴/数值错误：{npz_path}")
    if stage == "analysis" and (
        counts["analysis_candidate_rows"] != expected
        or counts["analysis_target_aggregate_rows"] < 1
    ):
        raise RuntimeError(f"analysis 产物合同失败：{counts}")
    if stage == "filtering" and not 1 <= counts["ranked_unique_rows"] <= expected:
        raise RuntimeError(f"filtering 产物合同失败：{counts}")
    return {
        "stage": stage,
        "status": "PASS",
        "counts": counts,
        "expected_designs": expected,
        "fold_sample_counts": fold_sample_counts,
    }


def prerequisite_complete(profile_name: str, manifest: dict[str, Any]) -> Path | None:
    """检查 samples2 深度探针的 samples1 前置全流程是否成功。"""

    prerequisite = manifest["profiles"][profile_name].get("prerequisite_profile")
    if not prerequisite:
        return None
    rank_one = manifest["scaffold_population"]["records"][0]
    task_name = f"01_{rank_one['candidate_id']}"
    complete = latest_complete_attempt(RUN_ROOT / "runs" / prerequisite / task_name)
    if complete is None:
        raise RuntimeError(
            f"profile={profile_name} 只能在 {prerequisite} 的 7XL0 五阶段成功后运行"
        )
    return complete


def parse_args() -> argparse.Namespace:
    """解析运行档位、骨架范围和恢复策略。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        required=True,
        choices=[
            "balanced_all12",
            "balanced_diverse_all12",
            "balanced_adherence_all12",
            "near_official_adherence_7xl0",
            "full_depth_probe",
            "full_depth_probe_samples2",
        ],
    )
    parser.add_argument("--start-rank", type=int, default=1)
    parser.add_argument("--end-rank", type=int, default=12)
    parser.add_argument(
        "--force-new-attempt",
        action="store_true",
        help="已有完整结果时仍新建 attempt；任何情况下都不会覆盖旧 attempt。",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="首个骨架失败后停止；默认记录失败并继续其余骨架。",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="只做输入、哈希、磁盘、MPS 和离线环境检查，不启动模型。",
    )
    return parser.parse_args()


def run_task(
    record: dict[str, Any],
    profile_name: str,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    launch_id: str,
    events: EventWriter,
) -> dict[str, Any]:
    """执行单个骨架的 check、configure 和五个彼此隔离的阶段。"""

    rank = int(record["selection_rank"])
    task_name = f"{rank:02d}_{record['candidate_id']}"
    task_root = RUN_ROOT / "runs" / profile_name / task_name
    task_root.mkdir(parents=True, exist_ok=True)
    attempt = next_attempt_dir(task_root)
    spec = RUN_ROOT / record["design_spec"]
    pipeline = attempt / "pipeline"
    environment = make_environment()
    status_path = attempt / "run_status.json"
    task_status: dict[str, Any] = {
        "schema_version": "2.0.0",
        "launch_id": launch_id,
        "profile": profile_name,
        "profile_contract": profile,
        "selection_rank": rank,
        "candidate_id": record["candidate_id"],
        "role": record["role"],
        "attempt": attempt.relative_to(RUN_ROOT).as_posix(),
        "design_spec": spec.relative_to(RUN_ROOT).as_posix(),
        "design_spec_sha256": sha256_file(spec),
        "started_at_utc": utc_now(),
        "status": "RUNNING",
        "completed_pipeline_stages": [],
        "stage_records": [],
    }
    atomic_write_json(status_path, task_status)
    events.emit("task_started", {"task": task_name, "profile": profile_name}, attempt)
    start = time.monotonic()
    try:
        run_logged_command(
                stage="00_check",
                command=check_command(spec, attempt),
                attempt=attempt,
                environment=environment,
                events=events,
                manifest=manifest,
            )
        try:
            check_contract = validate_check_outputs(attempt)
            task_status["stage_records"].append(
                finalize_stage_status(
                    attempt, "00_check", {"output_contract": check_contract}, events
                )
            )
        except BaseException as exc:
            mark_stage_contract_failed(attempt, "00_check", exc, events)
            raise

        run_logged_command(
                stage="00_configure",
                command=configure_command(spec, attempt, profile),
                attempt=attempt,
                environment=environment,
                events=events,
                manifest=manifest,
            )
        try:
            task_status["resolved_config_contract"] = validate_resolved_config(
                attempt, profile
            )
            task_status["stage_records"].append(
                finalize_stage_status(
                    attempt,
                    "00_configure",
                    {"resolved_config_contract": task_status["resolved_config_contract"]},
                    events,
                )
            )
        except BaseException as exc:
            mark_stage_contract_failed(attempt, "00_configure", exc, events)
            raise
        atomic_write_json(status_path, task_status)

        for index, stage in enumerate(PIPELINE_STAGES, start=1):
            numbered = f"{index:02d}_{stage}"
            run_logged_command(
                    stage=numbered,
                    command=execute_stage_command(pipeline, stage),
                    attempt=attempt,
                    environment=environment,
                    events=events,
                    manifest=manifest,
                    pipeline=pipeline,
                    checkpoint_names=(
                        profile["design_checkpoints"]
                        if stage == "design"
                        else STAGE_ASSETS[stage]
                    ),
                )
            contracts: dict[str, dict[str, Any]] = {}
            try:
                contract = validate_stage_outputs(stage, pipeline, profile)
                contracts["output_contract"] = contract
                if stage == "design":
                    design_stdout = attempt / "logs" / numbered / "stdout.log"
                    switch_count = design_stdout.read_text(
                        encoding="utf-8", errors="replace"
                    ).count("Switched checkpoint.")
                    expected_switches = 1 if len(profile["design_checkpoints"]) == 2 else 0
                    switch_contract = {
                        "status": "PASS" if switch_count == expected_switches else "FAIL",
                        "kind": "checkpoint_switch_contract",
                        "expected_switch_count": expected_switches,
                        "observed_switch_count": switch_count,
                        "design_checkpoints": profile["design_checkpoints"],
                        "execution": (
                            "dual_checkpoint_equal_split"
                            if expected_switches == 1
                            else "single_checkpoint_isolated_process"
                        ),
                    }
                    contracts["checkpoint_switch_contract"] = switch_contract
                    atomic_write_json(
                        attempt
                        / "stage_status"
                        / f"{numbered}_checkpoint_switch_contract.json",
                        switch_contract,
                    )
                    if switch_contract["status"] != "PASS":
                        raise RuntimeError(
                            "design checkpoint 切换合同失败："
                            f"expected={expected_switches}, observed={switch_count}"
                        )
                atomic_write_json(
                    attempt / "stage_status" / f"{numbered}_output_contract.json",
                    contract,
                )
                finalized = finalize_stage_status(attempt, numbered, contracts, events)
                task_status["stage_records"].append(finalized)
                task_status["completed_pipeline_stages"].append(stage)
                task_status.setdefault("output_contracts", []).append(contract)
                atomic_write_json(status_path, task_status)
            except BaseException as exc:
                mark_stage_contract_failed(attempt, numbered, exc, events, contracts)
                raise

        results = pipeline / "final_ranked_designs" / "all_designs_metrics.csv"
        task_status.update(
            status="PIPELINE_COMPLETE",
            finished_at_utc=utc_now(),
            elapsed_seconds=round(time.monotonic() - start, 3),
            results_csv=results.relative_to(RUN_ROOT).as_posix(),
            results_csv_sha256=sha256_file(results),
        )
        atomic_write_json(status_path, task_status)
        events.emit("task_finished", {"task": task_name, "status": "PIPELINE_COMPLETE"}, attempt)
        return task_status
    except BaseException as exc:
        interrupted = _STOP_SIGNAL is not None or isinstance(exc, InterruptedError)
        task_status.update(
            status="INTERRUPTED" if interrupted else "FAILED",
            finished_at_utc=utc_now(),
            elapsed_seconds=round(time.monotonic() - start, 3),
            error_type=type(exc).__name__,
            error_message=str(exc),
            **(signal_metadata() if interrupted else {}),
        )
        atomic_write_json(status_path, task_status)
        events.emit(
            "task_interrupted" if interrupted else "task_failed",
            {
                "task": task_name,
                "status": task_status["status"],
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                **(signal_metadata() if interrupted else {}),
            },
            attempt,
        )
        raise


def main() -> int:
    """取得全局锁、预检，并按选择范围严格串行执行任务。"""

    args = parse_args()
    manifest = load_manifest()
    profile = manifest["profiles"][args.profile]
    if not (1 <= args.start_rank <= args.end_rank <= 12):
        raise ValueError("排名范围必须满足 1 <= start <= end <= 12")
    if args.profile not in {
        "balanced_all12",
        "balanced_diverse_all12",
        "balanced_adherence_all12",
    } and (args.start_rank, args.end_rank) != (1, 12):
        raise ValueError("深度探针固定为 7XL0；不要再指定排名范围")

    launch_id = new_launch_id()
    launch_dir = RUN_ROOT / "runs" / "launches" / launch_id
    launch_dir.mkdir(parents=True, exist_ok=False)
    events = EventWriter(launch_id, launch_dir)
    launch_status: dict[str, Any] = {
        "schema_version": "2.0.0",
        "launch_id": launch_id,
        "profile": args.profile,
        "pid": os.getpid(),
        "argv": sys.argv,
        "working_directory": str(Path.cwd()),
        "execution_semantics": "pretrained_inference_candidate_generation_not_weight_training",
        "started_at_utc": utc_now(),
        "status": "PREFLIGHT",
        "tasks": [],
    }
    atomic_write_json(launch_dir / "launch_status.json", launch_status)
    events.emit("launch_created", {"profile": args.profile, "pid": os.getpid()})
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    with CampaignLock(launch_id):
        events.emit("campaign_lock_acquired")
        preflight = runtime_preflight(manifest, args.profile, launch_id, launch_dir)
        launch_status["preflight"] = preflight
        # 前置 profile 门属于启动安全合同，preflight-only 也必须验证；否则预检通过
        # 之后正式启动仍可能立即被门控拒绝。
        prerequisite = prerequisite_complete(args.profile, manifest)
        if prerequisite is not None:
            launch_status["prerequisite_attempt"] = prerequisite.relative_to(
                RUN_ROOT
            ).as_posix()
        if _STOP_SIGNAL is not None:
            launch_status.update(
                status="INTERRUPTED",
                finished_at_utc=utc_now(),
                **signal_metadata(),
            )
            atomic_write_json(launch_dir / "launch_status.json", launch_status)
            events.emit("launch_interrupted", signal_metadata())
            return 128 + int(_STOP_SIGNAL)
        if args.preflight_only:
            launch_status.update(status="PREFLIGHT_ONLY_COMPLETE", finished_at_utc=utc_now())
            atomic_write_json(launch_dir / "launch_status.json", launch_status)
            events.emit("preflight_only_complete")
            print(f"预检通过；未启动模型。launch_id={launch_id}")
            return 0

        records = [
            row
            for row in manifest["scaffold_population"]["records"]
            if int(row["selection_rank"]) in profile["selection_ranks"]
            and args.start_rank <= int(row["selection_rank"]) <= args.end_rank
        ]
        launch_status["status"] = "RUNNING"
        launch_status["requested_task_count"] = len(records)
        atomic_write_json(launch_dir / "launch_status.json", launch_status)
        start = time.monotonic()
        failures = 0
        interruptions = 0
        for record in records:
            task_name = f"{int(record['selection_rank']):02d}_{record['candidate_id']}"
            task_root = RUN_ROOT / "runs" / args.profile / task_name
            complete = latest_complete_attempt(task_root)
            if complete is not None and not args.force_new_attempt:
                skipped = {
                    "task": task_name,
                    "status": "SKIPPED_ALREADY_COMPLETE",
                    "attempt": complete.relative_to(RUN_ROOT).as_posix(),
                }
                launch_status["tasks"].append(skipped)
                atomic_write_json(launch_dir / "launch_status.json", launch_status)
                events.emit("task_skipped_complete", skipped)
                continue
            try:
                result = run_task(
                    record, args.profile, profile, manifest, launch_id, events
                )
                launch_status["tasks"].append(
                    {
                        "task": task_name,
                        "status": result["status"],
                        "attempt": result["attempt"],
                        "elapsed_seconds": result["elapsed_seconds"],
                    }
                )
            except BaseException as exc:
                interrupted = _STOP_SIGNAL is not None or isinstance(exc, InterruptedError)
                failures += 0 if interrupted else 1
                interruptions += 1 if interrupted else 0
                launch_status["tasks"].append(
                    {
                        "task": task_name,
                        "status": "INTERRUPTED" if interrupted else "FAILED",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        **(signal_metadata() if interrupted else {}),
                    }
                )
                if args.stop_on_error or _STOP_SIGNAL is not None:
                    break
            atomic_write_json(launch_dir / "launch_status.json", launch_status)

        completed = sum(
            row["status"] in {"PIPELINE_COMPLETE", "SKIPPED_ALREADY_COMPLETE"}
            for row in launch_status["tasks"]
        )
        final_status = (
            "INTERRUPTED"
            if _STOP_SIGNAL is not None
            else (
                "PIPELINE_COMPLETE"
                if failures == 0 and completed == len(records)
                else "PARTIAL_FAILURE"
            )
        )
        launch_status.update(
            status=final_status,
            completed_task_count=completed,
            failed_task_count=failures,
            interrupted_task_count=interruptions,
            finished_at_utc=utc_now(),
            elapsed_seconds=round(time.monotonic() - start, 3),
            **signal_metadata(),
        )
        atomic_write_json(launch_dir / "launch_status.json", launch_status)
        events.emit(
            "launch_interrupted" if final_status == "INTERRUPTED" else "launch_finished",
            {
                "status": final_status,
                "failures": failures,
                "interruptions": interruptions,
                **(signal_metadata() if final_status == "INTERRUPTED" else {}),
            },
        )
        if final_status == "INTERRUPTED":
            return 128 + int(_STOP_SIGNAL or signal.SIGTERM)
        return 0 if final_status == "PIPELINE_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
