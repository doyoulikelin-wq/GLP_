#!/usr/bin/env python3
"""运行或重放旧 12 个 VHH 骨架的第一轮 Mac 尝试。

``prepare`` 冻结输入，``run`` 执行模型，``analyze`` 只读已有产物；``all``
按该顺序执行。额外参数通过 ``--`` 之后转发给模型运行脚本。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def execute(script: str, arguments: list[str] | None = None) -> None:
    """在尝试包内执行阶段，避免依赖调用者当前目录。"""

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *(arguments or [])],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "analyze", "all"))
    parser.add_argument("run_args", nargs=argparse.REMAINDER, help="转发给 run_round1.py 的参数。")
    args = parser.parse_args()
    forwarded = args.run_args[1:] if args.run_args[:1] == ["--"] else args.run_args

    if args.action in {"prepare", "all"}:
        execute("prepare_round1.py")
    if args.action in {"run", "all"}:
        execute("run_round1.py", forwarded)
    if args.action in {"analyze", "all"}:
        execute("analyze_round1.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
