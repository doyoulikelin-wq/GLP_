"""Shared helpers for the Windows/WSL single-GPU T3 runtime tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path


T3_ROOT = Path(__file__).resolve().parents[1]


def implementation(name: str) -> Path:
    path = T3_ROOT / name
    assert path.is_file(), f"missing T3 implementation: {name}"
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_python(name: str, *arguments: object, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(implementation(name)), *(str(value) for value in arguments)]
    run_env = os.environ.copy()
    run_env.update({
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if env:
        run_env.update(env)
    return subprocess.run(command, text=True, capture_output=True, env=run_env, check=False)
