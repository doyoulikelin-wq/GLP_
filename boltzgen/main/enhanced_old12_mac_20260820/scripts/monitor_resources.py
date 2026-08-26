#!/usr/bin/env python3
"""监控一个 BoltzGen 阶段进程树的 CPU、常驻内存、系统内存和磁盘余量。

Apple MPS 使用统一内存，macOS 没有稳定的公开“逐进程显存”接口。本脚本只记录
可验证的进程常驻内存（Resident Set Size，RSS）和整机虚拟内存页，不伪造显存。
CSV 便于后续画图，JSONL 保留逐次采样事件和字段语义。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STOP_SIGNAL: int | None = None


def utc_now() -> str:
    """返回 UTC ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def signal_handler(signum: int, frame: Any) -> None:
    """请求监控器在当前采样完成后刷新文件并正常退出。"""

    del frame
    global _STOP_SIGNAL
    _STOP_SIGNAL = signum


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写出监控摘要，避免终止时产生半文件。"""

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def process_snapshot() -> list[dict[str, Any]]:
    """用 macOS/Linux 都支持的 ``ps`` 字段读取当前进程表。"""

    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,%cpu=,rss=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) != 5:
            continue
        try:
            rows.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "cpu_percent": float(parts[2]),
                    "rss_kib": int(parts[3]),
                    "command": parts[4],
                }
            )
        except ValueError:
            continue
    return rows


def descendants(root_pid: int, rows: list[dict[str, Any]]) -> set[int]:
    """递归取得阶段根进程及全部子进程 PID。"""

    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["ppid"] in selected and row["pid"] not in selected:
                selected.add(row["pid"])
                changed = True
    return selected


def mac_memory_snapshot() -> dict[str, int | None]:
    """解析 ``vm_stat``；非 macOS 或解析失败时返回空值。"""

    try:
        output = subprocess.run(
            ["vm_stat"], check=True, capture_output=True, text=True
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {
            "system_free_bytes": None,
            "system_active_bytes": None,
            "swap_used_bytes": None,
        }
    page_size = 4096
    first = output.splitlines()[0] if output else ""
    for token in first.replace(".", "").split():
        if token.isdigit():
            page_size = int(token)
            break
    values: dict[str, int] = {}
    for line in output.splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        digits = value.strip().rstrip(".")
        if digits.isdigit():
            values[key] = int(digits) * page_size
    free_bytes = sum(
        values.get(key, 0)
        for key in ("Pages free", "Pages speculative", "Pages purgeable")
    )
    active_bytes = sum(
        values.get(key, 0)
        for key in ("Pages active", "Pages wired down", "Pages occupied by compressor")
    )
    swap_used: int | None = None
    try:
        swap_text = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # macOS 形式："total = ...M  used = 10627.56M  free = ...M"。
        tokens = swap_text.replace("=", " ").split()
        used_index = tokens.index("used") + 1
        used_text = tokens[used_index]
        if used_text.endswith("M"):
            swap_used = int(float(used_text[:-1]) * 1024 * 1024)
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError, IndexError):
        swap_used = None
    return {
        "system_free_bytes": free_bytes,
        "system_active_bytes": active_bytes,
        "swap_used_bytes": swap_used,
    }


def parse_args() -> argparse.Namespace:
    """解析监控目标、输出路径和采样频率。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-pid", required=True, type=int)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    """持续采样直到阶段根进程退出。"""

    args = parse_args()
    if args.interval < 0.25:
        raise ValueError("采样间隔不得小于 0.25 秒")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_prefix.with_suffix(".csv")
    jsonl_path = args.output_prefix.with_suffix(".jsonl")
    summary_path = args.output_prefix.with_suffix(".summary.json")
    fields = [
        "sample_index",
        "sampled_at_utc",
        "elapsed_seconds",
        "launch_id",
        "attempt",
        "stage",
        "root_pid",
        "process_count",
        "cpu_percent_sum",
        "rss_kib_sum",
        "rss_gib_sum",
        "system_free_bytes",
        "system_active_bytes",
        "swap_used_bytes",
        "disk_free_bytes",
        "mps_process_memory_note",
    ]
    start = time.monotonic()
    started_at = utc_now()
    sample_index = 0
    first_sample_at: str | None = None
    last_sample_at: str | None = None
    exit_reason = "root_process_exited"
    with csv_path.open("w", newline="", encoding="utf-8") as csv_handle, jsonl_path.open(
        "w", encoding="utf-8"
    ) as json_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=fields)
        writer.writeheader()
        while True:
            if _STOP_SIGNAL is not None:
                exit_reason = "termination_signal_requested"
                break
            rows = process_snapshot()
            live = {row["pid"] for row in rows}
            if args.root_pid not in live:
                break
            tree = descendants(args.root_pid, rows)
            tree_rows = [row for row in rows if row["pid"] in tree]
            rss_kib = sum(row["rss_kib"] for row in tree_rows)
            memory = mac_memory_snapshot()
            disk_free = shutil.disk_usage(args.output_prefix.parent).free
            sampled_at = utc_now()
            first_sample_at = first_sample_at or sampled_at
            last_sample_at = sampled_at
            record: dict[str, Any] = {
                "sample_index": sample_index,
                "sampled_at_utc": sampled_at,
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "launch_id": args.launch_id,
                "attempt": args.attempt,
                "stage": args.stage,
                "root_pid": args.root_pid,
                "process_count": len(tree_rows),
                "cpu_percent_sum": round(sum(row["cpu_percent"] for row in tree_rows), 3),
                "rss_kib_sum": rss_kib,
                "rss_gib_sum": round(rss_kib / 1024 / 1024, 6),
                "system_free_bytes": memory["system_free_bytes"],
                "system_active_bytes": memory["system_active_bytes"],
                "swap_used_bytes": memory["swap_used_bytes"],
                "disk_free_bytes": disk_free,
                "mps_process_memory_note": "not_measured_no_stable_public_process_level_api",
            }
            writer.writerow(record)
            json_handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            csv_handle.flush()
            json_handle.flush()
            sample_index += 1
            time.sleep(args.interval)
    signal_name = None
    if _STOP_SIGNAL is not None:
        try:
            signal_name = signal.Signals(_STOP_SIGNAL).name
        except ValueError:
            signal_name = f"SIGNAL_{_STOP_SIGNAL}"
    atomic_write_json(
        summary_path,
        {
            "schema_version": "1.0.0",
            "monitor_pid": os.getpid(),
            "root_pid": args.root_pid,
            "launch_id": args.launch_id,
            "attempt": args.attempt,
            "stage": args.stage,
            "started_at_utc": started_at,
            "finished_at_utc": utc_now(),
            "first_sample_at_utc": first_sample_at,
            "last_sample_at_utc": last_sample_at,
            "sample_count": sample_index,
            "exit_reason": exit_reason,
            "termination_signal": _STOP_SIGNAL,
            "signal_name": signal_name,
            "status": "COMPLETE" if _STOP_SIGNAL is None else "GRACEFUL_TERMINATION",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
