#!/usr/bin/env python3
"""生成稳定的交付哈希清单，并执行“摘要 + 路径集合”双重验证。

清单刻意排除自身、验证回执、Python 字节码缓存与操作系统临时文件。验证回执会在
清单生成后写入；如果把回执本身纳入清单，就会形成无法稳定收敛的自引用哈希。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "provenance" / "SHA256SUMS"
RECEIPT = ROOT / "provenance" / "checksum_verification.json"


def utc_now() -> str:
    """返回带时区的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免把大文件一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_delivery_file(path: Path) -> bool:
    """判断文件是否应纳入稳定交付清单。"""

    relative = path.relative_to(ROOT)
    if path in {MANIFEST, RECEIPT}:
        return False
    if "__pycache__" in relative.parts or path.suffix == ".pyc":
        return False
    if path.name in {".DS_Store", ".localized"}:
        return False
    if path.name.endswith((".part", ".tmp", "~")):
        return False
    return path.is_file() and not path.is_symlink()


def delivery_files() -> list[Path]:
    """以相对路径稳定排序列出全部交付文件。"""

    return sorted(
        (path for path in ROOT.rglob("*") if is_delivery_file(path)),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def write_manifest(files: list[Path]) -> None:
    """采用 shasum 可直接验证的两空格格式写出清单。"""

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{sha256_file(path)}  {path.relative_to(ROOT).as_posix()}" for path in files
    ]
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_manifest() -> dict[str, str]:
    """读取刚生成的清单，并拒绝格式异常或重复路径。"""

    records: dict[str, str] = {}
    for line_number, line in enumerate(
        MANIFEST.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if "  " not in line:
            raise ValueError(f"SHA256SUMS 第 {line_number} 行格式错误")
        digest, relative = line.split("  ", 1)
        if relative in records:
            raise ValueError(f"SHA256SUMS 重复路径：{relative}")
        records[relative] = digest
    return records


def verify_manifest(records: dict[str, str]) -> dict[str, object]:
    """同时验证路径集合、摘要、文件数与总字节数。"""

    actual_paths = {
        path.relative_to(ROOT).as_posix(): path for path in delivery_files()
    }
    manifest_paths = set(records)
    actual_names = set(actual_paths)
    missing = sorted(manifest_paths - actual_names)
    unlisted = sorted(actual_names - manifest_paths)
    mismatched = []
    for relative in sorted(manifest_paths & actual_names):
        observed = sha256_file(actual_paths[relative])
        if observed != records[relative]:
            mismatched.append(
                {
                    "path": relative,
                    "expected": records[relative],
                    "observed": observed,
                }
            )
    return {
        "schema_version": "1.0.0",
        "verified_at_utc": utc_now(),
        "status": "PASS" if not (missing or unlisted or mismatched) else "FAIL",
        "manifest_path": MANIFEST.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256_file(MANIFEST),
        "listed_file_count": len(records),
        "listed_total_bytes": sum(
            actual_paths[path].stat().st_size
            for path in manifest_paths & actual_names
        ),
        "missing_paths": missing,
        "unlisted_paths": unlisted,
        "digest_mismatches": mismatched,
        "exclusions": [
            "provenance/SHA256SUMS",
            "provenance/checksum_verification.json",
            "**/__pycache__/**",
            "**/*.pyc",
            ".DS_Store/.localized/临时文件",
        ],
    }


def main() -> int:
    """重建清单、立即复核并保存机器可读回执。"""

    files = delivery_files()
    write_manifest(files)
    receipt = verify_manifest(parse_manifest())
    RECEIPT.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"checksum={receipt['status']} files={receipt['listed_file_count']} "
        f"bytes={receipt['listed_total_bytes']}"
    )
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
