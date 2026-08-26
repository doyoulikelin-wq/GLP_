#!/usr/bin/env python3
"""进入历史 Mac MVP 实现并转发命令行参数。

此入口对应 2026-08-19 的实验性 Apple Metal Performance Shaders 冒烟测试。
它不是官方 Linux/NVIDIA 生产入口，也不会把计算代理解释为实验结合结果。
完整模型资产、目标 CIF、骨架 CIF 和环境需按 README 从外部存储恢复。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    command = [sys.executable, str(ROOT / "scripts" / "run_mvp.py"), *sys.argv[1:]]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
