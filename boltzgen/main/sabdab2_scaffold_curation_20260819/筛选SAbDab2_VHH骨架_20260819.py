#!/usr/bin/env python3
"""编排 SAbDab2 VHH 骨架下载、数据库构建和 BoltzGen 输入验证。

默认不联网，只执行数据库构建和输入验证；只有显式传入 ``--download`` 才会
调用批量下载脚本。原始 SAbDab2 快照体积约 0.5 GB，留在外部数据目录。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def execute(script: str, *arguments: str) -> None:
    """执行一个阶段并在非零退出时立即阻断后续阶段。"""

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *arguments],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="先下载官方 SD-H 结构快照。")
    parser.add_argument("--skip-validation", action="store_true", help="只构建数据库，不运行 BoltzGen check。")
    args = parser.parse_args()

    if args.download:
        execute(
            "download_sabdab_range.py",
            "--output",
            "raw_snapshot/sabdab_all_sd_h_structures.tgz",
            "--workers",
            "4",
            "--tail-workers",
            "4",
        )
    execute("build_scaffold_database.py")
    if not args.skip_validation:
        execute("validate_boltzgen_exports.py", "--root", ".", "--timeout", "120")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
