#!/usr/bin/env python3
"""按固定时间间隔记录第一轮运行进程树的 CPU 与常驻内存。

本脚本只读取 macOS ``ps`` 输出，不注入模型进程。它把 ``run_round1.py`` 主进程
及其所有子进程视为同一个任务树，记录合计 CPU 使用率、合计常驻内存和进程数。
Apple MPS 统一内存没有稳定的进程级公开统计接口，因此本表不会伪造“显存”字段。
"""

from __future__ import annotations

import csv
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = RUN_ROOT / "analysis" / "runtime_resource_samples.csv"
SAMPLE_INTERVAL_SECONDS = 2.0


def utc_now() -> str:
    """返回带 UTC 时区的 ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def process_snapshot() -> list[dict[str, object]]:
    """读取当前进程表并转换为结构化记录。"""

    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,%cpu=,rss=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) != 5:
            continue
        pid, ppid, cpu, rss, command = parts
        try:
            rows.append(
                {
                    "pid": int(pid),
                    "ppid": int(ppid),
                    "cpu_percent": float(cpu),
                    "rss_kib": int(rss),
                    "command": command,
                }
            )
        except ValueError:
            continue
    return rows


def find_runner_pid(rows: list[dict[str, object]]) -> int | None:
    """定位当前的 run_round1.py 主进程，排除本监控脚本自身。"""

    candidates = [
        int(row["pid"])
        for row in rows
        if "scripts/run_round1.py" in str(row["command"])
        and "monitor_resources.py" not in str(row["command"])
    ]
    return min(candidates) if candidates else None


def descendants(root_pid: int, rows: list[dict[str, object]]) -> set[int]:
    """递归求一个进程的全部后代 PID。"""

    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if int(row["ppid"]) in selected and int(row["pid"]) not in selected:
                selected.add(int(row["pid"]))
                changed = True
    return selected


def main() -> int:
    """等待运行器出现，随后采样到运行器退出。"""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "sample_index",
                "sampled_at_utc",
                "elapsed_seconds",
                "runner_pid",
                "process_count",
                "cpu_percent_sum",
                "rss_kib_sum",
                "rss_gib_sum",
                "mps_memory_note",
            ],
        )
        writer.writeheader()
        handle.flush()

        # 最多等待 60 秒，避免在没有运行任务时无限驻留。
        runner_pid = None
        for _ in range(30):
            rows = process_snapshot()
            runner_pid = find_runner_pid(rows)
            if runner_pid is not None:
                break
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        if runner_pid is None:
            return 2

        start = time.monotonic()
        sample_index = 0
        while True:
            rows = process_snapshot()
            live_pids = {int(row["pid"]) for row in rows}
            if runner_pid not in live_pids:
                break

            tree_pids = descendants(runner_pid, rows)
            tree_rows = [row for row in rows if int(row["pid"]) in tree_pids]
            rss_kib = sum(int(row["rss_kib"]) for row in tree_rows)
            writer.writerow(
                {
                    "sample_index": sample_index,
                    "sampled_at_utc": utc_now(),
                    "elapsed_seconds": round(time.monotonic() - start, 3),
                    "runner_pid": runner_pid,
                    "process_count": len(tree_rows),
                    "cpu_percent_sum": round(
                        sum(float(row["cpu_percent"]) for row in tree_rows), 3
                    ),
                    "rss_kib_sum": rss_kib,
                    "rss_gib_sum": round(rss_kib / 1024 / 1024, 6),
                    "mps_memory_note": "not_measured_no_stable_process_level_api",
                }
            )
            handle.flush()
            sample_index += 1
            time.sleep(SAMPLE_INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
