#!/usr/bin/env python3
"""阻断不应进入公开 GLP_ 仓库的文件和命名。

该检查是持续使用的仓库治理代码，不是模型流水线。它只读扫描当前工作树，检查：

* 单文件大小和禁止扩展；
* 环境、缓存、运行目录和 vendor 路径；
* PDB 仅位于已登记的 BindCraft 小型公开输入目录；
* ``tools/one_off`` 脚本和 ``resources/data`` 目录含创建日期；
* Python 模块具有文档字符串；
* 绝对本机路径和常见凭据模式没有被提交。

它不能证明科学结论正确，也不能替代人工许可、隐私和 secret 审查。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MAX_FILE_BYTES = 5 * 1024 * 1024

BLOCKED_SUFFIXES = {
    ".ckpt", ".pt", ".pth", ".safetensors", ".npy", ".npz", ".pkl",
    ".pickle", ".parquet", ".h5", ".hdf5", ".tgz", ".sqlite", ".db",
    ".so", ".dylib", ".pem", ".key", ".p12", ".pfx",
}
BLOCKED_SEGMENTS = {
    "env", ".venv", "venv", "site-packages", "node_modules", "runtime_cache",
    "checkpoints", "runs", "snapshots", "outputs", "logs", "vendor",
    "__pycache__", ".ipynb_checkpoints", ".codex_artifacts",
}
TEXT_SUFFIXES = {
    ".py", ".md", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml",
    ".html", ".ipynb", ".txt", ".cff",
}
DATE_TOKEN = re.compile(r"20\d{6}")
DATA_DIRECTORY = re.compile(r".+_20\d{6}(?:_\d{6})?$")
SENSITIVE_PATTERNS = {
    "macOS absolute home path": re.compile(r"/Users/[^/\s<]+/"),
    "Windows absolute home path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s<]+\\"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "OpenAI-style secret": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def repository_files() -> list[Path]:
    """列出工作树中需要检查的常规文件，忽略 Git 自身对象。"""

    return sorted(
        path for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(ROOT).parts
    )


def check_python_docstring(path: Path) -> str | None:
    """主代码和一次性 Python 若无模块说明，返回可读错误。"""

    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return f"Python parse failed: {exc}"
    if not ast.get_docstring(module):
        return "Python module has no module docstring"
    return None


def check_file(path: Path) -> list[str]:
    """对单个文件应用大小、路径、语法和敏感内容检查。"""

    relative = path.relative_to(ROOT)
    errors: list[str] = []
    parts = set(relative.parts)

    if path.stat().st_size > MAX_FILE_BYTES:
        errors.append(f"file exceeds {MAX_FILE_BYTES} bytes")
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        errors.append(f"blocked suffix {path.suffix.lower()}")
    blocked = sorted(parts & BLOCKED_SEGMENTS)
    if blocked:
        errors.append(f"blocked path segment(s): {', '.join(blocked)}")
    if path.suffix.lower() == ".pdb":
        allowed = relative.parts[:4] == (
            "bindcraft", "resources", "data", "GLP1选择性靶标面板_20260825"
        )
        if not allowed:
            errors.append("PDB is outside the registered BindCraft target panel")

    if "tools" in parts and "one_off" in parts and path.name != "README.md":
        if not DATE_TOKEN.search(path.stem):
            errors.append("one_off file name has no YYYYMMDD date")

    if path.suffix.lower() == ".py" and ({"main", "one_off"} & parts):
        docstring_error = check_python_docstring(path)
        if docstring_error:
            errors.append(docstring_error)

    # 正则表达式本身会包含被检测的字面量，因此政策脚本不扫描自己的源文本。
    if (
        path.resolve() != Path(__file__).resolve()
        and path.suffix.lower() in TEXT_SUFFIXES
        and path.stat().st_size <= MAX_FILE_BYTES
    ):
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"possible sensitive content: {label}")
    return errors


def check_data_directories() -> list[str]:
    """要求每个资源数据包的直接子目录按用途和创建日期命名。"""

    errors: list[str] = []
    for route in ("boltzgen", "bindcraft"):
        data_root = ROOT / route / "resources" / "data"
        if not data_root.exists():
            continue
        for child in sorted(data_root.iterdir()):
            if child.is_dir() and not DATA_DIRECTORY.fullmatch(child.name):
                errors.append(f"{child.relative_to(ROOT)}: data directory lacks date suffix")
    return errors


def main() -> int:
    failures: list[str] = []
    for path in repository_files():
        for message in check_file(path):
            failures.append(f"{path.relative_to(ROOT)}: {message}")
    failures.extend(check_data_directories())

    if failures:
        print("Repository policy FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Repository policy PASS: checked {len(repository_files())} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
