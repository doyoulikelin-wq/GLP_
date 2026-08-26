#!/usr/bin/env python3
"""调用结构资产验证实现，并转发可选命令行参数。

验证仅说明文件可解析、来源登记和重复关系符合合同，不表示候选结合或选择性通过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    return subprocess.run(
        [sys.executable, str(ROOT / "validate_assets.py"), *sys.argv[1:]],
        cwd=ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
