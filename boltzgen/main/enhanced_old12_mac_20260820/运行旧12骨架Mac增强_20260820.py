#!/usr/bin/env python3
"""运行旧 12 个 VHH 骨架的 Mac 增强尝试或只读分析。

模型运行参数通过 ``--`` 转发给 ``run_mac_enhanced.py``。历史结果表明不要在
18 GB 统一内存机器上使用同进程双 checkpoint 压力档位；推荐按 README 分开
运行 diverse 和 adherence 支路。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def execute(script: str, arguments: list[str] | None = None) -> None:
    """执行单个阶段，并让失败阻止错误结果继续进入分析。"""

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *(arguments or [])],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "analyze"))
    parser.add_argument("run_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    forwarded = args.run_args[1:] if args.run_args[:1] == ["--"] else args.run_args

    if args.action == "prepare":
        execute("prepare_mac_enhanced.py")
    elif args.action == "run":
        execute("run_mac_enhanced.py", forwarded)
    else:
        execute("analyze_mac_enhanced.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
