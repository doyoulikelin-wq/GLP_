#!/usr/bin/env python3
"""按固定顺序运行 BoltzGen MVP 小数据清理与数据包封装。

该日期入口只负责编排；实现保留在 ``scripts/``，以维持历史相对路径合同。
运行前必须按本目录 README 将原始公开来源和运行资产恢复到约定位置。
模型权重不会被写入 Git。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def run_script(name: str) -> None:
    """在尝试包根目录执行一个实现脚本，并原样传播失败退出码。"""

    subprocess.run([sys.executable, str(ROOT / "scripts" / name)], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--step",
        choices=("curate", "finalize", "all"),
        default="all",
        help="只运行小数据清理、只封装数据包，或按顺序执行两步。",
    )
    args = parser.parse_args()

    if args.step in {"curate", "all"}:
        run_script("curate_small_sources.py")
    if args.step in {"finalize", "all"}:
        run_script("finalize_dataset_package.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
