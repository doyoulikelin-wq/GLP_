#!/usr/bin/env python3
"""封存当前 BoltzGen Mac campaign，并对路径集合与 SHA-256 做双重验证。

脚本只扫描 campaign 根目录内的常规文件，只写：

* ``provenance/SHA256SUMS``；
* ``provenance/checksum_verification.json``。

外部运行权重位于 campaign 目录之外，只在输入 manifest 中记录来源和哈希，不会
被本脚本重复纳入。SHA 清单自身、验证 JSON、Python 字节码、缓存目录、临时文件
和运行锁被明确排除，避免自引用或动态状态让封存结果不可重复。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 以脚本所在 campaign 为唯一根；不扫描同级旧 round1 或外部模型资产目录。
项目根目录 = Path(__file__).resolve().parent.parent
溯源目录 = 项目根目录 / "provenance"
清单路径 = 溯源目录 / "SHA256SUMS"
验证路径 = 溯源目录 / "checksum_verification.json"

# 这些文件若进入自己的清单会形成自引用；因此必须同时从生成和复核中排除。
自引用排除 = {
    "provenance/SHA256SUMS",
    "provenance/checksum_verification.json",
}


def 当前世界时() -> str:
    """返回带 UTC 时区的 ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


def 相对路径(path: Path) -> str:
    """返回 campaign 内稳定 POSIX 相对路径，并拒绝越界。"""

    relative = path.resolve().relative_to(项目根目录.resolve()).as_posix()
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError(f"非法封存相对路径：{relative!r}")
    if "\n" in relative or "\r" in relative:
        raise ValueError(f"文件名含换行，无法安全写入 SHA256SUMS：{relative!r}")
    return relative


def 应排除(relative: str) -> tuple[bool, str | None]:
    """按固定规则判断文件是否属于动态、缓存或自引用产物。"""

    parts = Path(relative).parts
    name = Path(relative).name
    if relative in 自引用排除:
        return True, "self_referential_checksum_artifact"
    if "__pycache__" in parts or name.endswith((".pyc", ".pyo")):
        return True, "python_bytecode_or_cache"
    if ".locks" in parts:
        return True, "dynamic_runtime_lock"
    if name.endswith((".tmp", ".swp", "~")):
        return True, "temporary_file"
    if name == ".DS_Store":
        return True, "macos_directory_metadata"
    return False, None


def 文件哈希(path: Path, chunk_size: int = 2 * 1024 * 1024) -> str:
    """流式计算常规文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def 枚举封存文件() -> tuple[list[Path], dict[str, list[str]]]:
    """枚举根目录内的常规文件，并记录每类排除路径。"""

    included: list[Path] = []
    excluded: dict[str, list[str]] = {}
    for path in sorted(项目根目录.rglob("*"), key=lambda item: item.as_posix()):
        # 符号链接可能越过 campaign 边界；为保证封存集合封闭，直接拒绝。
        if path.is_symlink():
            raise ValueError(f"封存目录含符号链接，拒绝继续：{path}")
        if not path.is_file():
            continue
        relative = 相对路径(path)
        skip, reason = 应排除(relative)
        if skip:
            excluded.setdefault(str(reason), []).append(relative)
        else:
            included.append(path)
    return included, excluded


def 原子写文本(path: Path, text: str) -> None:
    """仅在 provenance 内原子替换封存产物。"""

    if path.resolve().parent != 溯源目录.resolve():
        raise ValueError(f"封存脚本只允许写 provenance/：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def 解析清单(path: Path) -> dict[str, str]:
    """严格解析本脚本格式的 SHA256SUMS。"""

    parsed: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            continue
        if len(line) < 67 or line[64:66] != "  ":
            raise ValueError(f"SHA256SUMS 第 {line_number} 行格式错误")
        digest = line[:64]
        relative = line[66:]
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"SHA256SUMS 第 {line_number} 行哈希非法")
        if relative in parsed:
            raise ValueError(f"SHA256SUMS 路径重复：{relative}")
        parsed[relative] = digest
    return parsed


def 生成并验证() -> dict[str, Any]:
    """生成清单后重新枚举文件，并同时验证路径集合与每个文件哈希。"""

    files_before, excluded_before = 枚举封存文件()
    records: list[dict[str, Any]] = []
    for path in files_before:
        records.append(
            {
                "path": 相对路径(path),
                "size_bytes": path.stat().st_size,
                "sha256": 文件哈希(path),
            }
        )
    records.sort(key=lambda row: row["path"])

    # GNU/BSD sha256sum 兼容格式：64 位小写哈希、两个空格、相对路径。
    manifest_text = "".join(f"{row['sha256']}  {row['path']}\n" for row in records)
    原子写文本(清单路径, manifest_text)

    # 第二次重新枚举，防止清单生成过程中路径集合发生增加、删除或替换。
    files_after, excluded_after = 枚举封存文件()
    actual_paths = {相对路径(path) for path in files_after}
    manifest = 解析清单(清单路径)
    expected_paths = set(manifest)
    missing_paths = sorted(expected_paths - actual_paths)
    unexpected_paths = sorted(actual_paths - expected_paths)
    path_set_match = not missing_paths and not unexpected_paths

    # 只有路径集合先对齐，逐文件哈希才具有完整封存意义；仍对交集全部复算。
    mismatches: list[dict[str, Any]] = []
    verified_bytes = 0
    for relative in sorted(expected_paths & actual_paths):
        path = 项目根目录 / relative
        current_hash = 文件哈希(path)
        current_size = path.stat().st_size
        verified_bytes += current_size
        if current_hash != manifest[relative]:
            mismatches.append(
                {
                    "path": relative,
                    "expected_sha256": manifest[relative],
                    "actual_sha256": current_hash,
                    "size_bytes": current_size,
                }
            )
    hashes_match = not mismatches

    # 首次运行会新建 SHA 清单，因此“自引用排除”集合允许在第一次与第二次枚举间
    # 增加清单自身；其他缓存、锁和临时文件排除必须完全稳定。
    stable_excluded_before = {
        key: value for key, value in excluded_before.items()
        if key != "self_referential_checksum_artifact"
    }
    stable_excluded_after = {
        key: value for key, value in excluded_after.items()
        if key != "self_referential_checksum_artifact"
    }
    exclusions_stable = stable_excluded_before == stable_excluded_after
    status = "PASS" if path_set_match and hashes_match and exclusions_stable else "FAIL"
    verification = {
        "schema_version": "1.0.0",
        "verified_at_utc": 当前世界时(),
        "status": status,
        "campaign_root": ".",
        "manifest": "provenance/SHA256SUMS",
        "manifest_sha256": 文件哈希(清单路径),
        "manifest_entry_count": len(manifest),
        "verified_file_count": len(expected_paths & actual_paths),
        "verified_total_bytes": verified_bytes,
        "checks": {
            "path_set_match": path_set_match,
            "all_hashes_match": hashes_match,
            "exclusion_set_stable": exclusions_stable,
            "manifest_excludes_itself": "provenance/SHA256SUMS" not in manifest,
            "verification_json_excluded": "provenance/checksum_verification.json" not in manifest,
            "no_python_bytecode_in_manifest": not any(
                "__pycache__" in Path(path).parts or path.endswith((".pyc", ".pyo"))
                for path in manifest
            ),
            "no_runtime_locks_in_manifest": not any(".locks" in Path(path).parts for path in manifest),
        },
        "missing_paths": missing_paths,
        "unexpected_paths": unexpected_paths,
        "hash_mismatches": mismatches,
        "excluded": excluded_after,
        "external_runtime_asset_policy": (
            "模型 checkpoint 与 mols.zip 位于 campaign 根目录之外；只在 frozen provenance "
            "中保存其绝对来源、大小与 SHA-256，不重复复制或纳入本目录 SHA 清单。"
        ),
        "self_reference_policy": (
            "SHA256SUMS 与 checksum_verification.json 均排除；验证 JSON 记录清单自身哈希，"
            "但不会被清单反向包含。"
        ),
    }
    原子写文本(验证路径, json.dumps(verification, ensure_ascii=False, indent=2) + "\n")
    if status != "PASS":
        raise RuntimeError(json.dumps(verification, ensure_ascii=False, indent=2))
    return verification


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="生成并双重验证 campaign SHA-256 封存清单")
    parser.parse_args()
    result = 生成并验证()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
