#!/usr/bin/env python3
"""用项目 6X18 靶标逐包验证最终 VHH scaffold 的 BoltzGen 输入合同。

本脚本是导出后的独立验证步骤，不参与 scaffold 筛选，也不会重新排序候选。
它对 ``registry/selected_scaffolds.tsv`` 中的每个包执行以下操作：

1. 把项目冻结的 6X18 GLP-1(7-36)NH2 target 复制为 ``target.cif``；
2. 生成只引用 ``target.cif`` 与 ``scaffold.yaml`` 的 ``check_spec.yaml``；
3. 显式使用本地 ``mols.zip``，并在 Hugging Face/代理离线环境运行
   ``boltzgen check``；
4. 保存标准输出、标准错误、可视化 mmCIF 与逐候选结果；
5. 把 PASS/FAIL 和输出摘要回写 JSON、TSV、SQLite 与数据库摘要；
6. 最后重建整个交付目录的 ``SHA256SUMS``。

这里的 PASS 只表示“YAML、mmCIF、残基设计掩码能被当前 BoltzGen 0.3.2
解析，且检查输出同时含 scaffold 链和 30 残基 target 几何链”。target 在本步骤
是 ``geometry_only``；本检查没有原子级验证 C 端 ``CONH2``。它也不证明结合、
亲和力、选择性、表达量或可开发性。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import gemmi


# 以下扩展列同时写入 export_artifacts.tsv 与 SQLite export_artifact。
# 保留原有列，避免破坏 build_scaffold_database.py 已建立的字段合同。
EXPORT_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("target_cif_path", "TEXT"),
    ("target_cif_sha256", "TEXT"),
    ("check_spec_yaml_path", "TEXT"),
    ("check_spec_yaml_sha256", "TEXT"),
    ("boltzgen_check_output_path", "TEXT"),
    ("boltzgen_check_output_sha256", "TEXT"),
    ("boltzgen_check_stdout_path", "TEXT"),
    ("boltzgen_check_stdout_sha256", "TEXT"),
    ("boltzgen_check_stderr_path", "TEXT"),
    ("boltzgen_check_stderr_sha256", "TEXT"),
    ("boltzgen_check_exit_code", "INTEGER"),
    ("boltzgen_check_elapsed_seconds", "REAL"),
    ("boltzgen_check_status", "TEXT"),
    ("target_residue_count", "INTEGER"),
    ("target_role", "TEXT"),
    ("terminal_amide_atomically_verified", "TEXT"),
    ("boltzgen_check_message", "TEXT"),
]

# selection_member 只保存筛选成员与最终检查结论，不复制全部文件审计字段。
SELECTION_EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("boltzgen_check_status", "TEXT"),
    ("boltzgen_check_output_path", "TEXT"),
    ("boltzgen_check_output_sha256", "TEXT"),
]

RESULT_COLUMNS = [
    "candidate_id",
    "selection_rank",
    "role",
    "package_path",
    "boltzgen_check_status",
    "boltzgen_check_exit_code",
    "boltzgen_check_elapsed_seconds",
    "target_cif_path",
    "target_cif_sha256",
    "check_spec_yaml_path",
    "check_spec_yaml_sha256",
    "boltzgen_check_output_path",
    "boltzgen_check_output_sha256",
    "boltzgen_check_stdout_path",
    "boltzgen_check_stdout_sha256",
    "boltzgen_check_stderr_path",
    "boltzgen_check_stderr_sha256",
    "atom_site_rows",
    "label_asym_ids",
    "target_residue_count",
    "target_role",
    "terminal_amide_atomically_verified",
    "validated_at",
    "boltzgen_check_message",
    "command_json",
]


def utc_now() -> str:
    """返回带时区且可排序的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """流式计算文件摘要，避免把 373 MiB 的 mols.zip 一次读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def installed_boltzgen_version() -> str:
    """记录当前验证 Python 环境实际加载的 BoltzGen 版本。"""

    try:
        return version("boltzgen")
    except PackageNotFoundError:
        return "UNKNOWN"


def portable_project_path(path: Path) -> str:
    """把项目内外部资产写成可移植逻辑路径，不泄露个人主目录绝对路径。"""

    boltzgen_data = Path(__file__).resolve().parents[2]
    try:
        return str(Path("boltzgen_data") / path.relative_to(boltzgen_data))
    except ValueError:
        return f"external:{path.name}"


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件写完后原子替换，避免中断留下半截 JSON/TSV。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_copy(source: Path, destination: Path) -> None:
    """原子复制冻结 target；不直接覆盖正在被其他程序读取的文件。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """读取 TSV 并保留原始列顺序。"""

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"TSV 缺少表头：{path}")
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames), rows


def render_tsv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """把行渲染成稳定 TSV 文本，None 统一为空单元格。"""

    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=columns,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: "" if row.get(column) is None else row.get(column, "") for column in columns})
    return buffer.getvalue()


def resolve_package(root: Path, relative_path: str) -> Path:
    """解析 registry 中的相对路径，并拒绝越出交付根目录的路径。"""

    package = (root / relative_path).resolve()
    if not package.is_relative_to(root):
        raise ValueError(f"package_path 越出根目录：{relative_path}")
    if not package.is_dir():
        raise FileNotFoundError(f"候选包不存在：{package}")
    return package


def build_check_spec(target_chain: str) -> str:
    """生成最小、确定性的 BoltzGen check YAML。

    第二个 entity 引用 builder 已导出的 scaffold.yaml；该文件继续负责固定框架
    和开放三段 CDR。这里不复制其设计范围，避免两个规范发生漂移。
    """

    return (
        "entities:\n"
        "  - file:\n"
        "      path: target.cif\n"
        "      include:\n"
        "        - chain:\n"
        f"            id: {target_chain}\n"
        "  - file:\n"
        "      path: scaffold.yaml\n"
    )


def clean_process_text(value: str | bytes | None) -> str:
    """兼容 TimeoutExpired 在不同 Python 版本返回 bytes 或 str。"""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def count_chain_residues(block: gemmi.cif.Block, chain_id: str) -> int:
    """按 ``label_asym_id + label_seq_id`` 统计一个聚合物链的唯一残基数。"""

    asym_values = [str(value) for value in block.find_values("_atom_site.label_asym_id")]
    seq_values = [str(value) for value in block.find_values("_atom_site.label_seq_id")]
    if len(asym_values) != len(seq_values):
        raise ValueError("_atom_site.label_asym_id 与 label_seq_id 行数不一致")
    residue_ids = {
        seq_id
        for asym_id, seq_id in zip(asym_values, seq_values, strict=True)
        if asym_id == chain_id and seq_id not in {"", ".", "?"}
    }
    return len(residue_ids)


def inspect_target_source(path: Path, target_chain: str) -> int:
    """预检冻结 target：项目几何模板必须含指定链的 30 个聚合物残基。"""

    block = gemmi.cif.read_file(str(path)).sole_block()
    residue_count = count_chain_residues(block, target_chain)
    if residue_count != 30:
        raise ValueError(f"target 链 {target_chain} 应为 30 残基，实际为 {residue_count}")
    return residue_count


def inspect_check_output(path: Path, target_chain: str) -> tuple[int, list[str], int]:
    """独立解析 check 输出，确认输出不是空壳且合并了 A 与 30 残基 E 链。"""

    document = gemmi.cif.read_file(str(path))
    block = document.sole_block()
    asym_values = [str(value) for value in block.find_values("_atom_site.label_asym_id")]
    atom_rows = len(asym_values)
    chains = sorted(set(asym_values))
    if atom_rows == 0:
        raise ValueError("check 输出没有 _atom_site 行")
    expected = {"A", target_chain}
    missing = sorted(expected - set(chains))
    if missing:
        raise ValueError(f"check 输出缺少链：{','.join(missing)}；实际链：{','.join(chains)}")
    target_residue_count = count_chain_residues(block, target_chain)
    if target_residue_count != 30:
        raise ValueError(
            f"check 输出 target 链 {target_chain} 应为 30 残基，实际为 {target_residue_count}"
        )
    return atom_rows, chains, target_residue_count


def prepare_offline_environment() -> dict[str, str]:
    """构造子进程环境，双重阻断自动下载。

    显式本地 ``--moldir`` 已足以绕过 hf_hub_download；离线变量与不可达代理
    是第二层防护，用于防止未来版本出现隐式下载。清空 NO_PROXY，避免远端域名
    绕过不可达代理。
    """

    env = os.environ.copy()
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "WANDB_MODE": "offline",
            "PYTHONUNBUFFERED": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "all_proxy": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "no_proxy": "",
        }
    )
    return env


def run_one_candidate(
    *,
    root: Path,
    row: dict[str, str],
    target_source: Path,
    target_chain: str,
    boltzgen: Path,
    mols_zip: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """准备并检查一个候选；所有异常都转成可审计 FAIL 记录。"""

    candidate_id = row["candidate_id"]
    package = resolve_package(root, row["package_path"])
    scaffold_yaml = package / "scaffold.yaml"
    scaffold_cif = package / "scaffold.cif"
    if not scaffold_yaml.is_file() or not scaffold_cif.is_file():
        raise FileNotFoundError(f"{candidate_id} 缺少 scaffold.yaml 或 scaffold.cif")

    target_path = package / "target.cif"
    spec_path = package / "check_spec.yaml"
    check_root = package / "boltzgen_check"
    output_dir = check_root / "output"
    stdout_path = check_root / "stdout.log"
    stderr_path = check_root / "stderr.log"
    output_path = output_dir / "check_spec.cif"

    # 输出目录是本脚本唯一会清理的目录；拒绝符号链接，防止误删包外内容。
    if check_root.exists():
        if check_root.is_symlink() or not check_root.is_dir():
            raise ValueError(f"拒绝清理非普通目录：{check_root}")
        shutil.rmtree(check_root)
    check_root.mkdir(parents=True)

    atomic_copy(target_source, target_path)
    atomic_write_text(spec_path, build_check_spec(target_chain))

    command = [
        str(boltzgen),
        "check",
        spec_path.name,
        "--moldir",
        str(mols_zip),
        "--output",
        str(output_dir.relative_to(package)),
    ]
    started = time.monotonic()
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    message = ""
    try:
        completed = subprocess.run(
            command,
            cwd=package,
            env=prepare_offline_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        if exit_code != 0:
            message = f"boltzgen check 退出码 {exit_code}"
    except subprocess.TimeoutExpired as exc:
        # subprocess.run 在超时后会等待被终止的子进程，避免留下 check 残留。
        exit_code = 124
        stdout = clean_process_text(exc.stdout)
        stderr = clean_process_text(exc.stderr)
        message = f"boltzgen check 超过 {timeout_seconds:g} 秒"
    except Exception as exc:  # 保留每个候选的失败证据，不中断其余候选。
        exit_code = 125
        message = f"{type(exc).__name__}: {exc}"
        stderr = message + "\n"
    elapsed = time.monotonic() - started

    atomic_write_text(stdout_path, stdout)
    atomic_write_text(stderr_path, stderr)

    status = "FAIL"
    output_sha = ""
    atom_rows = 0
    chains: list[str] = []
    target_residue_count = 0
    if exit_code == 0:
        try:
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise FileNotFoundError("boltzgen check 未生成非空 check_spec.cif")
            atom_rows, chains, target_residue_count = inspect_check_output(output_path, target_chain)
            output_sha = sha256_file(output_path)
            status = "PASS"
            message = (
                "BoltzGen 输入合同通过：target 为30残基 geometry-only；"
                "C端CONH2未完成原子级验证，且不代表结合、亲和力或选择性通过。"
            )
        except Exception as exc:
            message = f"输出验证失败：{type(exc).__name__}: {exc}"

    def relative(path: Path) -> str:
        return str(path.relative_to(root))

    return {
        "candidate_id": candidate_id,
        "selection_rank": row.get("selection_rank", ""),
        "role": row.get("role", ""),
        "package_path": row["package_path"],
        "boltzgen_check_status": status,
        "boltzgen_check_exit_code": exit_code,
        "boltzgen_check_elapsed_seconds": f"{elapsed:.3f}",
        "target_cif_path": relative(target_path),
        "target_cif_sha256": sha256_file(target_path),
        "check_spec_yaml_path": relative(spec_path),
        "check_spec_yaml_sha256": sha256_file(spec_path),
        "boltzgen_check_output_path": relative(output_path) if output_path.is_file() else "",
        "boltzgen_check_output_sha256": output_sha,
        "boltzgen_check_stdout_path": relative(stdout_path),
        "boltzgen_check_stdout_sha256": sha256_file(stdout_path),
        "boltzgen_check_stderr_path": relative(stderr_path),
        "boltzgen_check_stderr_sha256": sha256_file(stderr_path),
        "atom_site_rows": atom_rows,
        "label_asym_ids": ",".join(chains),
        "target_residue_count": target_residue_count,
        "target_role": "geometry_only",
        "terminal_amide_atomically_verified": "false",
        "validated_at": utc_now(),
        "boltzgen_check_message": message,
        "command_json": json.dumps(command, ensure_ascii=False),
    }


def add_missing_columns(connection: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    """用 ALTER TABLE 扩展表，而不是 pandas replace，以保留全部索引。"""

    existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    for name, sql_type in columns:
        if name not in existing:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')


def unique_index_snapshot(connection: sqlite3.Connection, tables: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """记录目标表所有显式唯一索引，供事务前后逐项核对。"""

    placeholders = ",".join("?" for _ in tables)
    rows = connection.execute(
        f"""
        SELECT tbl_name, name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name IN ({placeholders})
          AND sql LIKE 'CREATE UNIQUE INDEX%'
        ORDER BY tbl_name, name
        """,
        tables,
    ).fetchall()
    return [(str(table), str(name), str(sql)) for table, name, sql in rows]


def update_sqlite(database: Path, results: list[dict[str, Any]]) -> None:
    """在单一事务内回写两个表，并断言唯一索引完全保留。"""

    with sqlite3.connect(database) as connection:
        before = unique_index_snapshot(connection, ("export_artifact", "selection_member"))
        expected_names = {"idx_export_candidate", "idx_selection_candidate", "idx_selection_rank"}
        if not expected_names.issubset({name for _, name, _ in before}):
            raise RuntimeError(f"SQLite 缺少预期唯一索引：{sorted(expected_names)}")

        connection.execute("BEGIN IMMEDIATE")
        try:
            add_missing_columns(connection, "export_artifact", EXPORT_EXTRA_COLUMNS)
            add_missing_columns(connection, "selection_member", SELECTION_EXTRA_COLUMNS)
            for result in results:
                candidate_id = result["candidate_id"]
                export_values = {
                    name: result.get(name, "") for name, _ in EXPORT_EXTRA_COLUMNS
                }
                assignments = ", ".join(f'"{name}" = ?' for name in export_values)
                cursor = connection.execute(
                    f'UPDATE export_artifact SET {assignments} WHERE candidate_id = ?',
                    [*export_values.values(), candidate_id],
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"export_artifact 未唯一命中：{candidate_id}")

                selection_values = {
                    name: result.get(name, "") for name, _ in SELECTION_EXTRA_COLUMNS
                }
                assignments = ", ".join(f'"{name}" = ?' for name in selection_values)
                cursor = connection.execute(
                    f'UPDATE selection_member SET {assignments} WHERE candidate_id = ?',
                    [*selection_values.values(), candidate_id],
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"selection_member 未唯一命中：{candidate_id}")

            after = unique_index_snapshot(connection, ("export_artifact", "selection_member"))
            if after != before:
                raise RuntimeError("SQLite 唯一索引在回写过程中发生变化")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def update_qc_json(root: Path, result: dict[str, Any]) -> None:
    """把机器可读平面字段和完整嵌套证据同时写回逐候选 QC。"""

    qc_path = root / result["package_path"] / "qc.json"
    payload = json.loads(qc_path.read_text(encoding="utf-8"))
    if payload.get("candidate_id") != result["candidate_id"]:
        raise ValueError(f"qc.json candidate_id 不一致：{qc_path}")
    payload["boltzgen_check_status"] = result["boltzgen_check_status"]
    payload["boltzgen_check_output_sha256"] = result["boltzgen_check_output_sha256"]
    payload["boltzgen_check"] = {
        key: result[key]
        for key in RESULT_COLUMNS
        if key not in {"selection_rank", "role", "package_path", "command_json"}
    }
    payload["boltzgen_check"]["command"] = json.loads(result["command_json"])
    atomic_write_text(qc_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def update_export_tsv(path: Path, results: list[dict[str, Any]]) -> None:
    """按 candidate_id 更新导出登记，不改变原始行序。"""

    columns, rows = read_tsv(path)
    by_candidate = {result["candidate_id"]: result for result in results}
    if len(by_candidate) != len(results):
        raise ValueError("验证结果中 candidate_id 重复")
    seen: set[str] = set()
    for row in rows:
        candidate_id = row.get("candidate_id", "")
        if candidate_id in by_candidate:
            seen.add(candidate_id)
            for name, _ in EXPORT_EXTRA_COLUMNS:
                row[name] = by_candidate[candidate_id].get(name, "")
    missing = sorted(set(by_candidate) - seen)
    if missing:
        raise ValueError(f"export_artifacts.tsv 缺少候选：{missing}")
    for name, _ in EXPORT_EXTRA_COLUMNS:
        if name not in columns:
            columns.append(name)
    atomic_write_text(path, render_tsv(columns, rows))


def update_selection_tsv(path: Path, results: list[dict[str, Any]]) -> None:
    """同步扩展 selected_scaffolds.tsv，维持其与 selection_member 的镜像关系。

    builder 最初用同一组行同时生成 TSV 与 SQLite 表；若只更新 SQLite，后续审计
    会把字段差异误判成数据漂移。因此这里只追加与 selection_member 相同的三个
    字段，既不改变排序，也不改变任何筛选结论。
    """

    columns, rows = read_tsv(path)
    by_candidate = {result["candidate_id"]: result for result in results}
    seen: set[str] = set()
    for row in rows:
        candidate_id = row.get("candidate_id", "")
        if candidate_id in by_candidate:
            seen.add(candidate_id)
            for name, _ in SELECTION_EXTRA_COLUMNS:
                row[name] = by_candidate[candidate_id].get(name, "")
    missing = sorted(set(by_candidate) - seen)
    if missing:
        raise ValueError(f"selected_scaffolds.tsv 缺少候选：{missing}")
    for name, _ in SELECTION_EXTRA_COLUMNS:
        if name not in columns:
            columns.append(name)
    atomic_write_text(path, render_tsv(columns, rows))


def update_database_summary(
    path: Path,
    results: list[dict[str, Any]],
    target_source: Path,
    mols_zip: Path,
    boltzgen_version: str,
    target_residue_count: int,
) -> None:
    """补充检查计数及运行合同；不改写原始筛选漏斗。"""

    summary = json.loads(path.read_text(encoding="utf-8"))
    pass_count = sum(result["boltzgen_check_status"] == "PASS" for result in results)
    fail_count = len(results) - pass_count
    counts = summary.setdefault("counts", {})
    counts.update(
        {
            "boltzgen_check_total": len(results),
            "boltzgen_check_pass": pass_count,
            "boltzgen_check_fail": fail_count,
            "boltzgen_check_pending": 0,
        }
    )
    summary["boltzgen_export_validation"] = {
        "validated_at": utc_now(),
        "status": "PASS" if fail_count == 0 else "FAIL",
        "boltzgen_version": boltzgen_version,
        "target_source": portable_project_path(target_source),
        "target_sha256": sha256_file(target_source),
        "target_residue_count": target_residue_count,
        "target_role": "geometry_only",
        "terminal_amide_atomically_verified": False,
        "mols_zip": portable_project_path(mols_zip),
        "mols_zip_sha256": sha256_file(mols_zip),
        "offline_mode": True,
        "result_table": "registry/boltzgen_export_validation.tsv",
        "interpretation": (
            "PASS 仅证明 6X18 target 的 30 残基几何与 scaffold 输入合同可解析；"
            "不证明 C 端 CONH2 被原子级保留，也不证明结合、亲和力或选择性。"
        ),
    }
    atomic_write_text(path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")


def rebuild_checksums(root: Path) -> None:
    """重建并复核清单：既验证摘要，也验证没有未登记普通文件。"""

    checksum_path = root / "SHA256SUMS"
    rows: list[str] = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path == checksum_path
            or path.name.endswith(("-wal", "-shm"))
            or path.suffix == ".pyc"
            or "__pycache__" in path.parts
        ):
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(root)}")
    atomic_write_text(checksum_path, "\n".join(rows) + "\n")

    # ``shasum -c`` 只能发现已登记文件损坏，不能发现 unlisted 文件；这里同时
    # 比较路径集合，覆盖“清单完整性”这一审计维度。
    expected: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"SHA256SUMS 行格式无效：{line}")
        if relative in expected:
            raise ValueError(f"SHA256SUMS 路径重复：{relative}")
        expected[relative] = digest
    actual = {
        str(path.relative_to(root)): path
        for path in sorted(root.rglob("*"))
        if (
            path.is_file()
            and path != checksum_path
            and not path.name.endswith(("-wal", "-shm"))
            and path.suffix != ".pyc"
            and "__pycache__" not in path.parts
        )
    }
    if set(expected) != set(actual):
        missing = sorted(set(expected) - set(actual))
        unlisted = sorted(set(actual) - set(expected))
        raise RuntimeError(f"SHA256SUMS 路径集合不完整；missing={missing}, unlisted={unlisted}")
    mismatched = [relative for relative, file_path in actual.items() if sha256_file(file_path) != expected[relative]]
    if mismatched:
        raise RuntimeError(f"SHA256SUMS 摘要复核失败：{mismatched}")


def preflight_sqlite(database: Path, candidate_ids: list[str]) -> None:
    """在耗时检查前确认 DB 与 TSV 候选集合一致，减少跨文件部分更新风险。"""

    with sqlite3.connect(database) as connection:
        for table in ("export_artifact", "selection_member"):
            placeholders = ",".join("?" for _ in candidate_ids)
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE candidate_id IN ({placeholders})",
                candidate_ids,
            ).fetchone()[0]
            if count != len(candidate_ids):
                raise RuntimeError(f"{table} 只命中 {count}/{len(candidate_ids)} 个候选")


def preflight_packages_and_export_registry(
    root: Path,
    selected: list[dict[str, str]],
    export_path: Path,
) -> None:
    """在任何 package 写入前核对导出包与 export registry 的完整性。

    特别把 qc/curation 放在预检阶段，是为了避免 ``boltzgen check`` 已运行且
    SQLite 已提交后才发现 JSON 缺失。若 builder 尚在并行重建 selected 目录，
    本检查也会尽早拒绝在不稳定快照上继续。
    """

    _, export_rows = read_tsv(export_path)
    export_ids = [row.get("candidate_id", "") for row in export_rows]
    selected_ids = [row["candidate_id"] for row in selected]
    if len(set(export_ids)) != len(export_ids):
        raise ValueError("export_artifacts.tsv 存在重复 candidate_id")
    if set(export_ids) != set(selected_ids):
        missing = sorted(set(selected_ids) - set(export_ids))
        extra = sorted(set(export_ids) - set(selected_ids))
        raise ValueError(f"selected/export 候选集合不一致；missing={missing}, extra={extra}")

    required_files = (
        "scaffold.cif",
        "scaffold.yaml",
        "residue_mapping.tsv",
        "qc.json",
        "curation.json",
    )
    for row in selected:
        package = resolve_package(root, row["package_path"])
        missing = [name for name in required_files if not (package / name).is_file()]
        if missing:
            raise FileNotFoundError(f"{row['candidate_id']} 导出包不完整：{','.join(missing)}")
        for json_name in ("qc.json", "curation.json"):
            payload = json.loads((package / json_name).read_text(encoding="utf-8"))
            if payload.get("candidate_id") != row["candidate_id"]:
                raise ValueError(
                    f"{row['candidate_id']} 与 {package / json_name} 的 candidate_id 不一致"
                )


def parse_args() -> argparse.Namespace:
    script_root = Path(__file__).resolve().parents[1]
    boltzgen_data = script_root.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=script_root, help="scaffold 数据库交付根目录")
    parser.add_argument(
        "--target",
        type=Path,
        default=boltzgen_data / "mvp_run_001/inputs/target/6X18_GLP1_7-36_geometry.cif",
        help="项目冻结的 6X18 target mmCIF",
    )
    parser.add_argument(
        "--mols-zip",
        type=Path,
        default=boltzgen_data / "mvp_assets_v0.3.2/runtime_cache/mols.zip",
        help="BoltzGen 本地 molecule archive；不会自动下载",
    )
    parser.add_argument(
        "--boltzgen",
        type=Path,
        default=boltzgen_data / "mvp_run_001/env/bin/boltzgen",
        help="BoltzGen 0.3.2 CLI 路径",
    )
    parser.add_argument("--target-chain", default="E", help="6X18 文件中 GLP-1 target 链，默认 E")
    parser.add_argument("--timeout", type=float, default=300.0, help="每个候选的 check 超时秒数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    target_source = args.target.resolve()
    mols_zip = args.mols_zip.resolve()
    boltzgen = args.boltzgen.resolve()
    boltzgen_version = installed_boltzgen_version()

    if not re.fullmatch(r"[A-Za-z0-9]+", args.target_chain):
        raise SystemExit("--target-chain 只允许字母或数字")
    if args.timeout <= 0:
        raise SystemExit("--timeout 必须大于 0")
    for path, label in ((target_source, "target"), (mols_zip, "mols.zip"), (boltzgen, "boltzgen")):
        if not path.is_file():
            raise SystemExit(f"缺少 {label}：{path}")
    if mols_zip.suffix.lower() != ".zip":
        raise SystemExit(f"--mols-zip 必须指向 .zip 文件：{mols_zip}")
    target_residue_count = inspect_target_source(target_source, args.target_chain)

    selected_path = root / "registry/selected_scaffolds.tsv"
    export_path = root / "registry/export_artifacts.tsv"
    database = root / "registry/scaffold_database.sqlite"
    summary_path = root / "registry/database_summary.json"
    for path in (selected_path, export_path, database, summary_path):
        if not path.is_file():
            raise SystemExit(f"缺少数据库交付文件：{path}")

    _, selected = read_tsv(selected_path)
    if not selected:
        raise SystemExit("selected_scaffolds.tsv 没有候选")
    candidate_ids = [row.get("candidate_id", "") for row in selected]
    if any(not candidate_id for candidate_id in candidate_ids):
        raise SystemExit("selected_scaffolds.tsv 存在空 candidate_id")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise SystemExit("selected_scaffolds.tsv 存在重复 candidate_id")

    # 完整预检后才运行 check，避免在候选登记或导出包损坏时写 package。
    preflight_packages_and_export_registry(root, selected, export_path)
    preflight_sqlite(database, candidate_ids)

    results: list[dict[str, Any]] = []
    for index, row in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] 检查 {row['candidate_id']} ...", flush=True)
        try:
            result = run_one_candidate(
                root=root,
                row=row,
                target_source=target_source,
                target_chain=args.target_chain,
                boltzgen=boltzgen,
                mols_zip=mols_zip,
                timeout_seconds=args.timeout,
            )
        except Exception as exc:
            # 准备阶段错误通常代表导出包不完整；保持候选级 FAIL，而不是无记录退出。
            package = resolve_package(root, row["package_path"])
            check_root = package / "boltzgen_check"
            check_root.mkdir(parents=True, exist_ok=True)
            stdout_path = check_root / "stdout.log"
            stderr_path = check_root / "stderr.log"
            atomic_write_text(stdout_path, "")
            atomic_write_text(stderr_path, f"{type(exc).__name__}: {exc}\n")
            result = {
                column: "" for column in RESULT_COLUMNS
            }
            result.update(
                {
                    "candidate_id": row["candidate_id"],
                    "selection_rank": row.get("selection_rank", ""),
                    "role": row.get("role", ""),
                    "package_path": row["package_path"],
                    "boltzgen_check_status": "FAIL",
                    "boltzgen_check_exit_code": 125,
                    "boltzgen_check_elapsed_seconds": "0.000",
                    "boltzgen_check_stdout_path": str(stdout_path.relative_to(root)),
                    "boltzgen_check_stdout_sha256": sha256_file(stdout_path),
                    "boltzgen_check_stderr_path": str(stderr_path.relative_to(root)),
                    "boltzgen_check_stderr_sha256": sha256_file(stderr_path),
                    "validated_at": utc_now(),
                    "boltzgen_check_message": f"{type(exc).__name__}: {exc}",
                    "command_json": "[]",
                    "target_residue_count": target_residue_count,
                    "target_role": "geometry_only",
                    "terminal_amide_atomically_verified": "false",
                }
            )
        results.append(result)
        print(f"    {result['boltzgen_check_status']}: {result['boltzgen_check_message']}", flush=True)

    # 先用事务更新 SQLite；其他文本文件随后采用原子替换。
    update_sqlite(database, results)
    update_export_tsv(export_path, results)
    update_selection_tsv(selected_path, results)
    for result in results:
        update_qc_json(root, result)

    result_path = root / "registry/boltzgen_export_validation.tsv"
    atomic_write_text(result_path, render_tsv(RESULT_COLUMNS, results))
    update_database_summary(
        summary_path,
        results,
        target_source,
        mols_zip,
        boltzgen_version,
        target_residue_count,
    )
    rebuild_checksums(root)

    passed = sum(result["boltzgen_check_status"] == "PASS" for result in results)
    failed = len(results) - passed
    print(json.dumps({"total": len(results), "pass": passed, "fail": failed}, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("用户中断；当前正在运行的 check 子进程会由 subprocess.run 回收。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"验证中止：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
