#!/usr/bin/env python3
"""Finalize one local BoltzGen output root under the frozen AIV1 contract."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


EXECUTOR_KIND = "WSL2_SYSTEMD_SINGLE_GPU"
SUCCESS_SCHEMA = "WSL2_BOLTZGEN_LOCAL_SUCCESS_V1"
CONTRACT_SCHEMA = "WSL2_BOLTZGEN_LOCAL_CELL_V1"
ENGINEERING_ENVIRONMENT_SCHEMA = "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4"
FORMAL_G1_SCHEMA = "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1"
VALIDATION_SCHEMA = "WSL2_BOLTZGEN_CELL_VALIDATION_V2"
OPAQUE_ARTIFACT_VALIDATION = "SINGLE_GZIP_MEMBER_CRC_EOF_BOUNDED_NO_TRAILING_V1"
OPAQUE_ARTIFACT_SEMANTIC_SOURCE = (
    "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv"
)
MAX_OPAQUE_GZIP_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
FAILURE_SCHEMA = "WSL2_BOLTZGEN_LOCAL_FAILURE_V1"
MONITOR_SCHEMA = "WSL2_LOCAL_GPU_MONITOR_STOP_V1"
SUBMISSION_SCHEMA = "WSL2_LOCAL_SUBMISSION_RECEIPT_V1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
FORMAL_ENVIRONMENT_REVISION = re.compile(
    r"WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V[1-9][0-9]*"
)
FORMAL_G1_OFFICIAL_CONTRACT = {
    "boltzgen": "0.3.2",
    "cuequivariance": "0.6.1",
    "torch": "2.8.0+cu128",
    "torch_cuda": "12.8",
    "triton": "3.4.0",
}
RUNTIME_MANIFEST_NAME = "gpu_runtime_scripts_SHA256SUMS"
RUNTIME_MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  \./([^\n\r\0]+)")
RUNTIME_MEMBERS = (
    "run_local_cell.sh",
    "software/finalize_local_attempt.py",
    "software/validate_cell_output.py",
    "status_local_cell.sh",
    "submit_local_once.sh",
    "verify_gpu_env_stage.sh",
)
EXEC_START_PATH = (
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/wsl/lib"
)
TRAMPOLINE_CODE = (
    'import os,re,sys;token,runner,bg_work,contract,path,home,user,uid=sys.argv[1:];'
    'invocation=os.environ.get("INVOCATION_ID","");'
    're.fullmatch(r"[0-9a-f]{32}",invocation) or sys.exit(75);'
    'environment={"PATH":path,"HOME":home,"USER":user,"LOGNAME":user,'
    '"XDG_RUNTIME_DIR":f"/run/user/{uid}","BG_SUBMISSION_TOKEN":token,'
    '"INVOCATION_ID":invocation};'
    'os.execve(runner,[runner,bg_work,contract],environment)'
)
FORMAL_SUCCESS_PATTERN = re.compile(r"(?:^|_)G[12](?:_[A-Z0-9]+)*_PASS(?:_|$)")
ENGINEERING_MEMORY_PROBE_RUN_KIND = "ENGINEERING_MEMORY_PROBE"
ENGINEERING_MEMORY_PROBE_STATUS = "ENGINEERING_MEMORY_PROBE_ONLY"
BLOCKED_GPU_MEMORY_STATUS = "BLOCKED_GPU_MEMORY"
ENGINEERING_MEMORY_PROBE_ID = re.compile(
    r"6xym_(diverse|adherence)_batch1_engineering"
)
ENGINEERING_6XYM_SPEC_SUFFIX = (
    "project_input",
    "specs",
    "08_pdb_00006xym-A",
    "design.yaml",
)
GPU_MONITOR_HEADER = (
    "timestamp",
    "index",
    "name",
    "memory.total [MiB]",
    "memory.used [MiB]",
    "utilization.gpu [%]",
    "power.draw [W]",
)
GPU_MEMORY_VALUE = re.compile(r"([0-9]+(?:\.[0-9]+)?) MiB")
GPU_STAGE_NAMES = ("design", "inverse_folding", "folding", "analysis", "filtering")
GPU_OOM_MARKERS = (
    b"cuda out of memory",
    b"torch.cuda.outofmemoryerror",
    b"cuda error: out of memory",
    b"cudnn_status_alloc_failed",
)
MAX_GPU_MONITOR_BYTES = 16 * 1024 * 1024
MAX_GPU_STAGE_STDERR_BYTES = 8 * 1024 * 1024
OUTPUT_MANIFEST_RELATIVE = "operator_logs/output_SHA256SUMS"
CELL_SUCCESS_RELATIVE = "operator_logs/cell.SUCCESS.json"
PROBE_SUCCESS_RELATIVE = "operator_logs/probe.SUCCESS.json"
LEGACY_ROOT_OUTPUTS = frozenset({"outputs.SHA256SUMS", "receipt.json", "STATUS.txt"})
TERMINAL_RELATIVES = frozenset(
    {
        CELL_SUCCESS_RELATIVE,
        PROBE_SUCCESS_RELATIVE,
        "operator_logs/cell.FAILURE.json",
        "operator_logs/probe.FAILURE.json",
    }
)


class FinalizationError(RuntimeError):
    """Raised when finalization cannot preserve the immutable evidence contract."""


def parse_gpu_monitor_peak_fraction(path: Path) -> float:
    """Recompute the peak used/total GPU-memory ratio from canonical telemetry."""

    if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
        raise FinalizationError("GPU monitor CSV is missing or unsafe")
    if path.stat(follow_symlinks=False).st_size > MAX_GPU_MONITOR_BYTES:
        raise FinalizationError("GPU monitor CSV exceeds the safe size bound")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FinalizationError("GPU monitor CSV is not readable UTF-8") from exc
    if not raw or not raw.endswith("\n") or "\r" in raw or "\0" in raw:
        raise FinalizationError("GPU monitor CSV framing is invalid")
    rows = csv.reader(raw.splitlines(), skipinitialspace=True, strict=True)
    try:
        header = tuple(next(rows))
    except (StopIteration, csv.Error) as exc:
        raise FinalizationError("GPU monitor CSV lacks a valid header") from exc
    if header != GPU_MONITOR_HEADER:
        raise FinalizationError("GPU monitor CSV header differs from the frozen schema")

    identity: tuple[str, str, float] | None = None
    peak = -1.0
    count = 0
    try:
        for row in rows:
            if len(row) != len(GPU_MONITOR_HEADER):
                raise FinalizationError("GPU monitor CSV row width is invalid")
            timestamp, index, name, total_text, used_text, utilization, power = (
                value.strip() for value in row
            )
            if not timestamp or not index.isdecimal() or not name or not utilization or not power:
                raise FinalizationError("GPU monitor CSV row identity is invalid")
            total_match = GPU_MEMORY_VALUE.fullmatch(total_text)
            used_match = GPU_MEMORY_VALUE.fullmatch(used_text)
            if total_match is None or used_match is None:
                raise FinalizationError("GPU monitor memory value is invalid")
            total = float(total_match.group(1))
            used = float(used_match.group(1))
            if (
                not math.isfinite(total)
                or not math.isfinite(used)
                or total <= 0
                or used < 0
                or used > total
            ):
                raise FinalizationError("GPU monitor memory range is invalid")
            current_identity = (index, name, total)
            if identity is None:
                identity = current_identity
            elif current_identity != identity:
                raise FinalizationError("GPU monitor contains multiple or drifting GPUs")
            peak = max(peak, used / total)
            count += 1
    except csv.Error as exc:
        raise FinalizationError("GPU monitor CSV parsing failed") from exc
    if count < 1 or not math.isfinite(peak) or not 0 < peak <= 1:
        raise FinalizationError("GPU monitor contains no valid positive memory sample")
    return peak


def write_peak_memory_fraction(monitor_path: Path, output_path: Path) -> float:
    """Publish the derived peak ratio once; finalization will recompute it."""

    expected = monitor_path.parent / "peak_memory_fraction.txt"
    if output_path != expected or output_path.is_symlink():
        raise FinalizationError("peak-memory output path is not canonical")
    value = parse_gpu_monitor_peak_fraction(monitor_path)
    payload = f"{value:.17g}\n".encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except FileExistsError as exc:
        raise FinalizationError("peak-memory output already exists") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(output_path.parent)
    return value


def detect_gpu_oom(root: Path) -> bool:
    """Require a non-zero GPU-stage exit and a matching CUDA OOM traceback."""

    logs = root / "operator_logs"
    if logs.is_symlink() or not logs.is_dir() or logs.resolve(strict=True) != logs:
        raise FinalizationError("operator-logs directory is missing or unsafe")
    for stage in GPU_STAGE_NAMES:
        exit_path = logs / f"{stage}.exit_code.txt"
        stderr_path = logs / f"{stage}.stderr.txt"
        if exit_path.is_symlink() or stderr_path.is_symlink():
            raise FinalizationError("GPU-stage OOM evidence is unsafe")
        if not exit_path.is_file() or not stderr_path.is_file():
            continue
        if (
            exit_path.resolve(strict=True) != exit_path
            or stderr_path.resolve(strict=True) != stderr_path
        ):
            raise FinalizationError("GPU-stage OOM evidence traverses a symlink")
        try:
            exit_text = exit_path.read_text(encoding="ascii")
            if not re.fullmatch(r"(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\n", exit_text):
                continue
            if (
                stderr_path.stat(follow_symlinks=False).st_size
                > MAX_GPU_STAGE_STDERR_BYTES
            ):
                raise FinalizationError("GPU-stage stderr exceeds the safe size bound")
            stderr = stderr_path.read_bytes().lower()
        except (OSError, UnicodeError) as exc:
            raise FinalizationError("GPU-stage OOM evidence is unreadable") from exc
        if any(marker in stderr for marker in GPU_OOM_MARKERS):
            return True
    return False


class DirectoryIdentityGuard:
    """Hold and revalidate one non-symlink directory inode across finalization."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        self.descriptor = os.open(path, flags)
        opened = os.fstat(self.descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            os.close(self.descriptor)
            raise FinalizationError(f"{label} is not a directory")
        self.identity = (opened.st_dev, opened.st_ino)
        self.verify()

    def verify(self) -> None:
        opened = os.fstat(self.descriptor)
        current = os.stat(self.path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or self.identity != (opened.st_dev, opened.st_ino)
            or self.identity != (current.st_dev, current.st_ino)
        ):
            raise FinalizationError(f"{self.label} inode changed during finalization")

    def __del__(self) -> None:
        descriptor = getattr(self, "descriptor", -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self.descriptor = -1


HIERARCHY_FD_ENV = (
    "BG_HIERARCHY_BG_FD",
    "BG_HIERARCHY_RUNS_FD",
    "BG_HIERARCHY_CELL_FD",
    "BG_HIERARCHY_ATTEMPT_FD",
    "BG_HIERARCHY_LOGS_FD",
)


def require_canonical_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise FinalizationError(f"{label} is missing, non-directory, or a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FinalizationError(f"cannot resolve {label}: {path}: {exc}") from exc
    if resolved != path:
        raise FinalizationError(f"{label} is not canonical or traverses a symlink")


def verify_hierarchy_closure(
    guards: tuple[DirectoryIdentityGuard, ...],
) -> None:
    for guard in guards:
        guard.verify()
    ready = os.environ.get("BG_HIERARCHY_READY")
    raw_descriptors = [os.environ.get(name) for name in HIERARCHY_FD_ENV]
    if ready is None and all(value is None for value in raw_descriptors):
        return
    if ready != "1" or any(value is None for value in raw_descriptors):
        raise FinalizationError("inherited hierarchy descriptor set is incomplete")
    for name, raw, guard in zip(
        HIERARCHY_FD_ENV, raw_descriptors, guards, strict=True
    ):
        if raw is None or not raw.isdecimal():
            raise FinalizationError(f"{name} must be a decimal descriptor")
        try:
            inherited = os.fstat(int(raw))
        except OSError as exc:
            raise FinalizationError(f"{name} is not an open inherited descriptor") from exc
        if (
            not stat.S_ISDIR(inherited.st_mode)
            or (inherited.st_dev, inherited.st_ino) != guard.identity
        ):
            raise FinalizationError(f"{name} identifies another hierarchy inode")


def json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FinalizationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinalizationError(f"{label} is missing, non-regular, or a symlink: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=json_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"cannot parse {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"{label} must contain one JSON object: {path}")
    return value


def require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise FinalizationError(f"{label} must be a JSON boolean")
    return value


def require_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinalizationError(f"{label} must be a JSON integer")
    if minimum is not None and value < minimum:
        raise FinalizationError(f"{label} must be >= {minimum}")
    return value


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinalizationError(f"{label} must be a non-empty string")
    text = value.strip()
    if "\n" in text or "\r" in text or "\0" in text:
        raise FinalizationError(f"{label} contains an unsafe character")
    return text


def require_sha256(value: object, label: str) -> str:
    text = require_string(value, label)
    if SHA256_RE.fullmatch(text) is None:
        raise FinalizationError(f"{label} must be a lowercase SHA-256 digest")
    return text


def require_utc_timestamp(value: object, label: str) -> str:
    text = require_string(value, label)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise FinalizationError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FinalizationError(f"{label} must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise FinalizationError(f"{label} must be UTC")
    return text


def canonical_existing_file(argument: str, label: str) -> Path:
    unresolved = Path(argument).expanduser()
    if not unresolved.is_absolute():
        raise FinalizationError(f"{label} path must be absolute: {unresolved}")
    if unresolved.is_symlink():
        raise FinalizationError(f"{label} may not be a symlink: {unresolved}")
    try:
        path = unresolved.resolve(strict=True)
    except OSError as exc:
        raise FinalizationError(f"cannot resolve {label}: {argument}: {exc}") from exc
    if unresolved != path:
        raise FinalizationError(f"{label} path must be canonical and traverse no symlinks: {unresolved}")
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise FinalizationError(f"{label} must be a regular non-symlink file: {path}")
    return path


def canonical_attempt_root(argument: str) -> Path:
    unresolved = Path(argument).expanduser()
    if not unresolved.is_absolute():
        raise FinalizationError(f"attempt root must be absolute: {unresolved}")
    if unresolved.is_symlink():
        raise FinalizationError(f"attempt root may not be a symlink: {unresolved}")
    try:
        root = unresolved.resolve(strict=True)
    except OSError as exc:
        raise FinalizationError(f"cannot resolve attempt root: {argument}: {exc}") from exc
    if unresolved != root:
        raise FinalizationError("attempt root must be canonical and traverse no symlinks")
    if root.is_symlink() or not root.is_dir():
        raise FinalizationError(f"attempt root must be a regular directory: {root}")
    return root


def sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FinalizationError(f"cannot open regular file for hashing: {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FinalizationError(f"cannot hash a non-regular file: {path}")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise FinalizationError(f"file changed while it was being hashed: {path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def is_formal_success_claim(status_value: str) -> bool:
    return FORMAL_SUCCESS_PATTERN.search(status_value.upper()) is not None


def contract_bound_file(
    contract: dict[str, Any],
    *,
    path_field: str,
    sha_field: str,
    label: str,
    expected_path: Path | None = None,
) -> tuple[Path, str]:
    path = canonical_existing_file(require_string(contract.get(path_field), path_field), label)
    if expected_path is not None and path != expected_path:
        raise FinalizationError(f"{path_field} does not identify the supplied {label}")
    expected_sha = require_sha256(contract.get(sha_field), sha_field)
    if sha256_file(path) != expected_sha:
        raise FinalizationError(f"{label} hash disagrees with {sha_field}")
    return path, expected_sha


def optional_contract_bound_file(
    contract: dict[str, Any],
    *,
    path_field: str,
    sha_field: str,
    label: str,
) -> tuple[Path, str] | None:
    path_value = contract.get(path_field)
    sha_value = contract.get(sha_field)
    if path_value is None and sha_value is None:
        return None
    if path_value is None or sha_value is None:
        raise FinalizationError(f"{path_field} and {sha_field} must be supplied together")
    return contract_bound_file(
        contract,
        path_field=path_field,
        sha_field=sha_field,
        label=label,
    )


def validate_runtime_manifest(
    contract: dict[str, Any],
) -> tuple[Path, dict[str, str], Path, str, str]:
    """Validate the exact BG_WORK-rooted runtime closure, including this process."""
    manifest, manifest_sha = contract_bound_file(
        contract,
        path_field="runtime_scripts_manifest_path",
        sha_field="runtime_scripts_manifest_sha256",
        label="runtime-scripts manifest",
    )
    if manifest.name != RUNTIME_MANIFEST_NAME:
        raise FinalizationError("runtime scripts manifest is not at the BG_WORK root")
    bg_work = manifest.parent
    try:
        raw = manifest.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise FinalizationError("cannot read runtime scripts manifest") from exc
    if not text or not text.endswith("\n") or "\r" in text or "\0" in text:
        raise FinalizationError("runtime scripts manifest framing is invalid")
    records: dict[str, str] = {}
    ordered: list[str] = []
    for line in text.splitlines():
        match = RUNTIME_MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise FinalizationError("runtime scripts manifest line is invalid")
        digest, relative = match.groups()
        relative_path = Path(relative)
        if (
            relative.startswith("/")
            or "\\" in relative
            or relative_path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or any(ord(character) < 32 for character in relative)
        ):
            raise FinalizationError("runtime scripts manifest path is unsafe")
        if relative in records:
            raise FinalizationError("runtime scripts manifest has duplicate entries")
        target = bg_work / relative_path
        if target.is_symlink() or not target.is_file() or target.resolve(strict=True) != target:
            raise FinalizationError(f"runtime script is missing or unsafe: {relative}")
        if sha256_file(target) != digest:
            raise FinalizationError(f"runtime script SHA-256 mismatch: {relative}")
        records[relative] = digest
        ordered.append(relative)
    expected = list(sorted(RUNTIME_MEMBERS, key=lambda value: value.encode("utf-8")))
    if ordered != expected:
        raise FinalizationError("runtime scripts manifest does not contain the exact required set")
    finalizer_path = (bg_work / "software/finalize_local_attempt.py").resolve(strict=True)
    if Path(__file__).resolve(strict=True) != finalizer_path:
        raise FinalizationError("executing finalizer is not the BG_WORK manifest member")
    validator_path = (bg_work / "software/validate_cell_output.py").resolve(strict=True)
    return (
        validator_path,
        records,
        manifest,
        manifest_sha,
        records["software/finalize_local_attempt.py"],
    )


def validate_submission_receipt(
    *,
    path: Path,
    bg_work: Path,
    contract_path: Path,
    contract_sha256: str,
    contract: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Bind terminal publication to the unique systemd submission identity."""
    cell_id = require_string(contract.get("cell_id"), "cell contract cell_id")
    attempt_id = require_string(contract.get("attempt_id"), "cell contract attempt_id")
    expected_path = bg_work / "local_submissions" / f"{cell_id}.{attempt_id}.receipt.json"
    if path != expected_path:
        raise FinalizationError("submission receipt is not the canonical BG_WORK receipt")
    before_sha = sha256_file(path)
    receipt = load_json(path, "submission receipt")
    required_fields = {
        "schema_version",
        "status",
        "executor_kind",
        "cell_id",
        "attempt_id",
        "cell_contract_path",
        "cell_contract_sha256",
        "unit",
        "active_state_at_receipt",
        "sub_state_at_receipt",
        "unit_result_at_receipt",
        "invocation_id",
        "runner_path",
        "submission_token",
        "submitted_at_utc",
        "executor_uid",
        "exec_start_sha256",
    }
    if set(receipt) != required_fields:
        raise FinalizationError("submission receipt does not use the fixed schema")
    expected_unit = f"boltzgen-local-{contract_sha256}.service"
    expected_runner = bg_work / "run_local_cell.sh"
    expected_values = {
        "schema_version": SUBMISSION_SCHEMA,
        "status": "SUBMITTED",
        "executor_kind": EXECUTOR_KIND,
        "cell_id": cell_id,
        "attempt_id": attempt_id,
        "cell_contract_path": str(contract_path),
        "cell_contract_sha256": contract_sha256,
        "unit": expected_unit,
        "runner_path": str(expected_runner),
    }
    for field, expected in expected_values.items():
        if receipt.get(field) != expected:
            raise FinalizationError(f"submission receipt binding mismatch: {field}")
    for field in (
        "active_state_at_receipt",
        "sub_state_at_receipt",
        "unit_result_at_receipt",
    ):
        require_string(receipt.get(field), f"submission receipt {field}")
    token = receipt.get("submission_token")
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise FinalizationError("submission receipt token is invalid")
    invocation_id = receipt.get("invocation_id")
    if not isinstance(invocation_id, str) or TOKEN_RE.fullmatch(invocation_id) is None:
        raise FinalizationError("submission receipt InvocationID is invalid")
    executor_uid = require_int(
        receipt.get("executor_uid"), "submission receipt executor_uid", minimum=0
    )
    if executor_uid != os.geteuid():
        raise FinalizationError("submission receipt executor_uid is not this executor")
    account = pwd.getpwuid(executor_uid)
    runner = canonical_existing_file(str(expected_runner), "submission runner")
    if runner != expected_runner:
        raise FinalizationError("submission runner path is not canonical")
    argv = [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        TRAMPOLINE_CODE,
        token,
        str(expected_runner),
        str(bg_work),
        str(contract_path),
        EXEC_START_PATH,
        account.pw_dir,
        account.pw_name,
        str(executor_uid),
    ]
    encoded_argv = json.dumps(
        argv, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    expected_exec_start_sha = hashlib.sha256(encoded_argv).hexdigest()
    if require_sha256(
        receipt.get("exec_start_sha256"), "submission receipt exec_start_sha256"
    ) != expected_exec_start_sha:
        raise FinalizationError("submission receipt ExecStart digest mismatch")
    require_utc_timestamp(receipt.get("submitted_at_utc"), "submission submitted_at_utc")
    if path.read_bytes() != canonical_json_bytes(receipt):
        raise FinalizationError("submission receipt is not canonical JSON")
    if sha256_file(path) != before_sha:
        raise FinalizationError("submission receipt changed during validation")
    return receipt, before_sha


def validate_monitor(
    root: Path, monitor_path: Path, *, require_healthy: bool
) -> tuple[dict[str, Any], str]:
    expected_path = (root / "operator_logs" / "monitor.stopped.json").resolve(strict=True)
    if monitor_path != expected_path:
        raise FinalizationError("monitor receipt must be output/operator_logs/monitor.stopped.json")
    monitor = load_json(monitor_path, "stopped-monitor receipt")
    if monitor.get("schema_version") != MONITOR_SCHEMA:
        raise FinalizationError("unsupported or missing monitor-stop schema_version")
    if monitor.get("status") != "STOPPED":
        raise FinalizationError("GPU monitor is not STOPPED")
    if not require_bool(monitor.get("wait_completed"), "monitor wait_completed"):
        raise FinalizationError("GPU monitor wait did not complete")
    monitor_healthy = require_bool(
        monitor.get("monitor_healthy"), "monitor monitor_healthy"
    )
    monitor_started = require_bool(
        monitor.get("monitor_started"), "monitor monitor_started"
    )
    if require_healthy and not monitor_healthy:
        raise FinalizationError("GPU monitor evidence is incomplete or unhealthy")
    if require_healthy and not monitor_started:
        raise FinalizationError("successful pipeline lacks a started GPU monitor")
    if monitor_started:
        require_int(monitor.get("monitor_pid"), "monitor monitor_pid", minimum=1)
    elif monitor.get("monitor_pid") is not None:
        raise FinalizationError("monitor_pid must be null when monitor_started=false")
    stopped_at = require_utc_timestamp(monitor.get("stopped_at_utc"), "monitor stopped_at_utc")
    return monitor, stopped_at


def validate_environment_provenance_manifest(
    manifest: Path, environment_attempt: Path
) -> None:
    """Validate a canonical formal-environment SHA256SUMS and both venv closures."""
    if manifest.parent != environment_attempt:
        raise FinalizationError(
            "formal environment provenance manifest must be at the environment-attempt root"
        )
    try:
        text = manifest.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise FinalizationError("cannot read formal environment provenance manifest") from exc
    if not text or not text.endswith("\n") or "\r" in text or "\0" in text:
        raise FinalizationError("formal environment provenance manifest framing is invalid")
    records: dict[str, str] = {}
    ordered: list[str] = []
    for line in text.splitlines():
        match = RUNTIME_MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise FinalizationError("formal environment provenance manifest line is invalid")
        digest, relative = match.groups()
        pure = Path(relative)
        if (
            relative.startswith("/")
            or "\\" in relative
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any(ord(character) < 32 for character in relative)
        ):
            raise FinalizationError("formal environment provenance path is unsafe")
        if relative in records:
            raise FinalizationError("formal environment provenance manifest has duplicates")
        target = environment_attempt / pure
        if target.is_symlink() or not target.is_file() or target.resolve(strict=True) != target:
            raise FinalizationError(
                f"formal environment provenance member is missing or unsafe: {relative}"
            )
        if sha256_file(target) != digest:
            raise FinalizationError(
                f"formal environment provenance member hash mismatch: {relative}"
            )
        records[relative] = digest
        ordered.append(relative)
    expected_order = sorted(records, key=lambda value: value.encode("utf-8"))
    if ordered != expected_order:
        raise FinalizationError(
            "formal environment provenance manifest is not unique bytewise-sorted"
        )

    tree_names = ("env", "env_clean_rebuild")
    allowed_links = {
        "lib64": "lib",
        "bin/python": "python3",
        "bin/python3": None,
        "bin/python3.12": "python3",
    }
    observed_regular: set[str] = set()
    interpreter_targets: list[Path] = []

    for tree_name in tree_names:
        tree = environment_attempt / tree_name
        if tree.is_symlink() or not tree.is_dir() or tree.resolve(strict=True) != tree:
            raise FinalizationError(f"formal environment tree is missing or unsafe: {tree_name}")
        observed_links: dict[str, Path] = {}

        def visit(directory: Path) -> None:
            try:
                entries = sorted(
                    os.scandir(directory), key=lambda entry: entry.name.encode("utf-8")
                )
            except OSError as exc:
                raise FinalizationError(
                    f"cannot enumerate formal environment tree: {directory}"
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                relative_to_tree = path.relative_to(tree).as_posix()
                relative = f"{tree_name}/{relative_to_tree}"
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                except OSError as exc:
                    raise FinalizationError(
                        f"cannot inspect formal environment member: {relative}"
                    ) from exc
                if stat.S_ISDIR(mode):
                    visit(path)
                    continue
                if stat.S_ISREG(mode):
                    observed_regular.add(relative)
                    continue
                if stat.S_ISLNK(mode):
                    if relative_to_tree not in allowed_links:
                        raise FinalizationError(
                            f"unexpected formal environment symlink: {relative}"
                        )
                    raw_target = os.readlink(path)
                    expected_target = allowed_links[relative_to_tree]
                    if expected_target is None:
                        target_path = Path(raw_target)
                        if not target_path.is_absolute():
                            raise FinalizationError(
                                f"formal environment python3 is not an absolute system link: {relative}"
                            )
                        if target_path.parent != Path("/usr/bin"):
                            raise FinalizationError(
                                f"formal environment python3 target is outside /usr/bin: {relative}"
                            )
                        try:
                            resolved = target_path.resolve(strict=True)
                        except OSError as exc:
                            raise FinalizationError(
                                f"formal environment python3 target is missing: {relative}"
                            ) from exc
                        target_stat = resolved.stat()
                        if (
                            not resolved.is_file()
                            or resolved.parent != Path("/usr/bin")
                            or not resolved.name.startswith("python3")
                            or target_stat.st_uid != 0
                            or target_stat.st_mode & 0o022
                        ):
                            raise FinalizationError(
                                f"formal environment python3 target is not a canonical system interpreter: {relative}"
                            )
                    else:
                        if raw_target != expected_target:
                            raise FinalizationError(
                                f"formal environment symlink target mismatch: {relative}"
                            )
                        try:
                            resolved = path.resolve(strict=True)
                        except OSError as exc:
                            raise FinalizationError(
                                f"formal environment symlink target is missing: {relative}"
                            ) from exc
                    observed_links[relative_to_tree] = resolved
                    continue
                raise FinalizationError(
                    f"formal environment tree contains a special file: {relative}"
                )

        visit(tree)
        if set(observed_links) != set(allowed_links):
            missing_links = sorted(set(allowed_links) - set(observed_links))
            extra_links = sorted(set(observed_links) - set(allowed_links))
            raise FinalizationError(
                f"{tree_name} fixed symlink set mismatch: "
                f"missing={missing_links}, extra={extra_links}"
            )
        if "lib64" in observed_links:
            if observed_links["lib64"] != (tree / "lib").resolve(strict=True):
                raise FinalizationError(f"{tree_name}/lib64 does not resolve to lib")
        python_targets = [
            observed_links[name]
            for name in ("bin/python", "bin/python3", "bin/python3.12")
            if name in observed_links
        ]
        if python_targets and any(target != python_targets[0] for target in python_targets[1:]):
            raise FinalizationError(
                f"{tree_name} Python symlinks do not resolve to one interpreter"
            )
        if "bin/python3" in observed_links:
            interpreter_targets.append(observed_links["bin/python3"])

    if interpreter_targets and any(
        target != interpreter_targets[0] for target in interpreter_targets[1:]
    ):
        raise FinalizationError(
            "formal production and clean-rebuild Python links resolve differently"
        )
    listed_regular = {
        relative
        for relative in records
        if relative.split("/", 1)[0] in tree_names
    }
    if listed_regular != observed_regular:
        missing = sorted(observed_regular - listed_regular)[:10]
        extra = sorted(listed_regular - observed_regular)[:10]
        raise FinalizationError(
            f"formal environment manifest/tree closure mismatch: missing={missing}, extra={extra}"
        )


def validate_environment(
    environment: dict[str, Any],
    contract: dict[str, Any],
    stage_class: str,
    environment_path: Path,
) -> tuple[bool, str | None, Path | None]:
    if require_int(environment.get("exit_code"), "environment exit_code", minimum=0) != 0:
        raise FinalizationError("environment receipt is not successful")
    if environment.get("failure_codes") != [] or environment.get("failure_stage") is not None:
        raise FinalizationError("environment receipt contains failure evidence")
    require_string(environment.get("attempt_id"), "environment attempt_id")
    if environment.get("compatibility_activation") != "EXPLICIT_PROCESS_LOCAL_ONLY":
        raise FinalizationError("environment compatibility activation is unsafe")
    formal_g1 = require_bool(environment.get("formal_g1"), "environment formal_g1")
    status_value = require_string(environment.get("status"), "environment status")
    if stage_class == "ENGINEERING":
        if environment.get("schema_version") != ENGINEERING_ENVIRONMENT_SCHEMA:
            raise FinalizationError("ENGINEERING requires the V4 engineering receipt schema")
        if formal_g1 is not False or status_value != "ENGINEERING_COMPATIBILITY_ONLY":
            raise FinalizationError("ENGINEERING V4 may only carry formal_g1=false")
        if require_bool(
            environment.get("environment_contract_revision_required"),
            "environment environment_contract_revision_required",
        ) is not True:
            raise FinalizationError("engineering environment must require a contract revision")
        return False, None, None
    if environment.get("schema_version") != FORMAL_G1_SCHEMA:
        raise FinalizationError("FORMAL requires the formal G1 V1 receipt schema")
    if formal_g1 is not True or status_value != "G1_PASS":
        raise FinalizationError("FORMAL requires G1_PASS/formal_g1=true")
    revision = environment.get("environment_contract_revision")
    if not isinstance(revision, str) or FORMAL_ENVIRONMENT_REVISION.fullmatch(revision) is None:
        raise FinalizationError("formal G1 receipt has an invalid environment revision")
    if require_bool(
        environment.get("environment_contract_revision_required"),
        "environment environment_contract_revision_required",
    ) is not False:
        raise FinalizationError("formal G1 receipt still requires a contract revision")
    if environment.get("official_contract") != FORMAL_G1_OFFICIAL_CONTRACT:
        raise FinalizationError("formal G1 receipt official_contract mismatch")
    bound = contract_bound_file(
        contract,
        path_field="environment_provenance_manifest_path",
        sha_field="environment_provenance_manifest_sha256",
        label="environment provenance manifest",
    )
    validate_environment_provenance_manifest(bound[0], environment_path.parent)
    if environment.get("environment_manifest_sha256") != bound[1]:
        raise FinalizationError("formal G1 receipt does not bind the environment manifest")
    return True, bound[1], bound[0]


def revalidate_formal_environment_evidence(evidence: dict[str, Any]) -> None:
    """Close live FORMAL G1 and both environment trees at publication boundaries."""
    if not evidence["formal_run"]:
        return
    environment_path = evidence["environment_path"]
    manifest_path = evidence["environment_manifest_path"]
    if not isinstance(environment_path, Path) or not isinstance(manifest_path, Path):
        raise FinalizationError("formal environment evidence paths are unavailable")
    if sha256_file(environment_path) != evidence["environment_sha256"]:
        raise FinalizationError("formal environment receipt changed before publication")
    if sha256_file(manifest_path) != evidence["environment_manifest_sha256"]:
        raise FinalizationError("formal environment provenance manifest changed before publication")
    validate_environment_provenance_manifest(manifest_path, environment_path.parent)


def validate_validation_receipt(
    root: Path,
    contract: dict[str, Any],
    *,
    probe: bool,
    formal_run: bool,
    validator_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    path = canonical_existing_file(
        str(root / "operator_logs" / "cell_contract.json"),
        "cell-output validation receipt",
    )
    validation = load_json(path, "cell-output validation receipt")
    required_fields = {
        "schema_version",
        "status",
        "output",
        "validator_sha256",
        "semantic_payload_files",
        "semantic_payload_file_count",
        "semantic_payload_manifest_sha256",
        "pickle_deserialization_performed",
        "opaque_artifact_validation",
        "opaque_artifact_semantic_source",
        "opaque_artifact_uncompressed_bytes",
        "expected_designs",
        "observed_unique_ids",
        "fold_samples_per_candidate",
        "resolved_design_multiplicity",
        "resolved_design_diffusion_samples",
        "resolved_inverse_fold_multiplicity",
        "filter_rows_after_cdr_dedup",
        "filter_final_rows",
        "filter_budget",
        "writer_coordinate_closure_count",
    }
    if set(validation) != required_fields:
        raise FinalizationError("cell-output validation receipt does not use the fixed schema")
    if validation.get("schema_version") != VALIDATION_SCHEMA:
        raise FinalizationError("unsupported cell-output validation schema_version")
    if validation.get("status") != "PASS":
        raise FinalizationError("cell-output validation receipt is not PASS")
    if validation.get("pickle_deserialization_performed") is not False:
        raise FinalizationError("validator receipt may not claim pickle deserialization")
    if validation.get("opaque_artifact_validation") != OPAQUE_ARTIFACT_VALIDATION:
        raise FinalizationError("validator receipt has an unsupported opaque-artifact check")
    if validation.get("opaque_artifact_semantic_source") != OPAQUE_ARTIFACT_SEMANTIC_SOURCE:
        raise FinalizationError("validator receipt has an unsafe opaque semantic source")
    opaque_size = require_int(
        validation.get("opaque_artifact_uncompressed_bytes"),
        "validation opaque_artifact_uncompressed_bytes",
        minimum=1,
    )
    if opaque_size > MAX_OPAQUE_GZIP_UNCOMPRESSED_BYTES:
        raise FinalizationError("validator receipt opaque artifact exceeds the size bound")
    if validation.get("output") != str(root):
        raise FinalizationError("cell-output validation receipt names another output root")
    if require_sha256(validation.get("validator_sha256"), "validation validator_sha256") != validator_sha256:
        raise FinalizationError("validation receipt was produced by another validator")
    expected_designs = require_int(
        contract.get("expected_designs"), "cell contract expected_designs", minimum=1
    )
    expected_fold_samples = require_int(
        contract.get("expected_fold_samples"),
        "cell contract expected_fold_samples",
        minimum=1,
    )
    if expected_fold_samples != 5:
        raise FinalizationError("the frozen cell contract requires exactly five fold samples")
    for field, expected in {
        "expected_designs": expected_designs,
        "observed_unique_ids": expected_designs,
        "fold_samples_per_candidate": expected_fold_samples,
    }.items():
        if require_int(validation.get(field), f"validation {field}", minimum=1) != expected:
            raise FinalizationError(f"validation {field} disagrees with the execution contract")
    if formal_run:
        if expected_designs != 10:
            raise FinalizationError("the frozen G2 acceptance/probe contract requires ten designs")
        expected_diffusion = 5 if probe else 1
        expected_multiplicity = 2 if probe else 10
        for field, expected in {
            "resolved_design_diffusion_samples": expected_diffusion,
            "resolved_design_multiplicity": expected_multiplicity,
        }.items():
            if require_int(validation.get(field), f"validation {field}", minimum=1) != expected:
                raise FinalizationError(f"formal G2 validation {field} != {expected}")
    semantic = validation.get("semantic_payload_files")
    if not isinstance(semantic, list) or not semantic:
        raise FinalizationError("validation semantic_payload_files must be a non-empty list")
    if require_int(
        validation.get("semantic_payload_file_count"),
        "validation semantic_payload_file_count",
        minimum=1,
    ) != len(semantic):
        raise FinalizationError("validation semantic payload count mismatch")
    manifest_lines: list[str] = []
    ordered_paths: list[str] = []
    for index, record in enumerate(semantic):
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise FinalizationError(f"invalid semantic payload record {index}")
        relative = require_string(record.get("path"), f"semantic payload path {index}")
        pure = Path(relative)
        if (
            relative.startswith("/")
            or "\\" in relative
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise FinalizationError("validation semantic payload path is unsafe")
        digest = require_sha256(record.get("sha256"), f"semantic payload SHA {index}")
        target = root / pure
        if target.is_symlink() or not target.is_file() or target.resolve(strict=True) != target:
            raise FinalizationError("validation semantic payload member is unsafe")
        if sha256_file(target) != digest:
            raise FinalizationError("validation semantic payload member hash mismatch")
        ordered_paths.append(relative)
        manifest_lines.append(f"{digest}  ./{relative}\n")
    if ordered_paths != sorted(set(ordered_paths), key=lambda value: value.encode("utf-8")):
        raise FinalizationError("validation semantic payload is not unique bytewise-sorted")
    manifest_sha = hashlib.sha256("".join(manifest_lines).encode("utf-8")).hexdigest()
    if require_sha256(
        validation.get("semantic_payload_manifest_sha256"),
        "validation semantic_payload_manifest_sha256",
    ) != manifest_sha:
        raise FinalizationError("validation semantic payload manifest hash mismatch")
    if path.read_bytes() != canonical_json_bytes(validation):
        raise FinalizationError("cell-output validation receipt is not canonical JSON")
    return path, validation


def rerun_validator_exact(
    *,
    root: Path,
    contract: dict[str, Any],
    environment_path: Path,
    validator_path: Path,
    validation_path: Path,
) -> None:
    python_path = environment_path.parent / "env" / "bin" / "python"
    if not python_path.exists() or not python_path.is_file() or not os.access(python_path, os.X_OK):
        raise FinalizationError("environment receipt sibling env/bin/python is unavailable")
    expected = validation_path.read_bytes()
    run_environment = {
        "EXPECTED_DESIGNS": str(
            require_int(contract.get("expected_designs"), "cell contract expected_designs", minimum=1)
        ),
        "EXPECTED_FOLD_SAMPLES": str(
            require_int(
                contract.get("expected_fold_samples"),
                "cell contract expected_fold_samples",
                minimum=1,
            )
        ),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(
            [str(python_path), "-I", str(validator_path), str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_environment,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FinalizationError(f"cannot rerun the bound validator: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FinalizationError(f"bound validator rerun failed: {detail[:500]}")
    if completed.stdout != expected:
        raise FinalizationError("bound validator rerun does not exactly match its receipt")


def validate_resolved_config(
    root: Path, contract: dict[str, Any]
) -> tuple[Path, str]:
    expected = canonical_existing_file(
        str(root / "operator_logs" / "resolved_config_SHA256SUMS"),
        "resolved config manifest",
    )
    path_value = contract.get("resolved_config_manifest_path")
    sha_value = contract.get("resolved_config_manifest_sha256")
    if path_value is not None or sha_value is not None:
        if path_value is None or sha_value is None:
            raise FinalizationError(
                "resolved_config_manifest_path and resolved_config_manifest_sha256 must be paired"
            )
        bound_path, bound_sha = contract_bound_file(
            contract,
            path_field="resolved_config_manifest_path",
            sha_field="resolved_config_manifest_sha256",
            label="resolved config manifest",
            expected_path=expected,
        )
        return bound_path, bound_sha
    return expected, sha256_file(expected)


def classify_probe_contract(
    contract: dict[str, Any],
    *,
    run_kind: str,
    stage_class: str,
    success_status: str,
) -> str | None:
    """Separate the one engineering T6 probe from the frozen formal probes."""

    if run_kind == ENGINEERING_MEMORY_PROBE_RUN_KIND:
        if stage_class != "ENGINEERING":
            raise FinalizationError("the engineering memory probe must use stage_class=ENGINEERING")
        if success_status != ENGINEERING_MEMORY_PROBE_STATUS:
            raise FinalizationError(
                "the engineering memory probe has a non-canonical success_status"
            )
        cell_id = require_string(contract.get("cell_id"), "cell contract cell_id")
        probe_id = require_string(contract.get("probe_id"), "probe_id")
        match = ENGINEERING_MEMORY_PROBE_ID.fullmatch(cell_id)
        if match is None or probe_id != cell_id:
            raise FinalizationError("the engineering memory probe has a non-canonical probe_id")
        checkpoint_name = require_string(contract.get("checkpoint_name"), "checkpoint_name")
        if checkpoint_name != match.group(1):
            raise FinalizationError("engineering probe_id/checkpoint_name mismatch")
        checkpoint_sha = require_sha256(contract.get("checkpoint_sha256"), "checkpoint_sha256")
        design_sha = require_sha256(
            contract.get("design_checkpoint_sha256"), "design_checkpoint_sha256"
        )
        if checkpoint_sha != design_sha:
            raise FinalizationError("engineering probe checkpoint SHA is not the design checkpoint")
        design_path = Path(
            require_string(contract.get("design_checkpoint"), "design_checkpoint")
        )
        if design_path.name != f"boltzgen1_{checkpoint_name}.ckpt":
            raise FinalizationError("engineering probe checkpoint path/name mismatch")
        for field, expected in {
            "expected_designs": 1,
            "budget": 1,
            "diffusion_batch_size": 1,
            "inverse_fold_num_sequences": 1,
            "expected_fold_samples": 5,
            "devices": 1,
        }.items():
            if require_int(contract.get(field), field, minimum=1) != expected:
                raise FinalizationError(
                    f"engineering memory probe requires {field}={expected}"
                )
        spec_path = Path(require_string(contract.get("spec_path"), "spec_path"))
        if tuple(spec_path.parts[-4:]) != ENGINEERING_6XYM_SPEC_SUFFIX:
            raise FinalizationError("engineering memory probe must use the frozen 6XYM spec")
        return "ENGINEERING"

    if stage_class == "ENGINEERING":
        cell_id = require_string(contract.get("cell_id"), "cell contract cell_id")
        spec_path = Path(require_string(contract.get("spec_path"), "spec_path"))
        if (
            "PROBE" in run_kind.upper()
            or "PROBE" in success_status.upper()
            or cell_id.startswith("6xym_")
            or tuple(spec_path.parts[-4:]) == ENGINEERING_6XYM_SPEC_SUFFIX
            or any(
                field in contract
                for field in ("probe_id", "checkpoint_name", "checkpoint_sha256")
            )
        ):
            raise FinalizationError("approximate engineering memory-probe contract")

    if "PROBE" in run_kind.upper():
        if stage_class != "FORMAL":
            raise FinalizationError("unsupported non-formal probe run_kind")
        return "FORMAL"

    if success_status == ENGINEERING_MEMORY_PROBE_STATUS or any(
        field in contract for field in ("probe_id", "peak_memory_fraction_path")
    ):
        raise FinalizationError("probe-only fields require a canonical probe run_kind")
    return None


def load_peak_fraction(
    root: Path,
    contract: dict[str, Any],
    *,
    formal_probe: bool,
) -> float:
    expected = canonical_existing_file(
        str(root / "operator_logs" / "peak_memory_fraction.txt"),
        "probe peak-memory fraction",
    )
    declared_path = contract.get("peak_memory_fraction_path")
    if declared_path is not None:
        resolved = canonical_existing_file(
            require_string(declared_path, "peak_memory_fraction_path"),
            "probe peak-memory fraction",
        )
        if resolved != expected:
            raise FinalizationError("peak_memory_fraction_path is not the output probe receipt")
    monitor_path = canonical_existing_file(
        str(root / "operator_logs" / "gpu_monitor.csv"),
        "probe GPU monitor CSV",
    )
    recomputed = parse_gpu_monitor_peak_fraction(monitor_path)
    try:
        payload = expected.read_bytes()
        value = float(payload.decode("ascii").removesuffix("\n"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise FinalizationError("probe peak-memory fraction is not numeric") from exc
    canonical = f"{recomputed:.17g}\n".encode("ascii")
    if payload != canonical or value != recomputed:
        raise FinalizationError("probe peak-memory receipt disagrees with canonical telemetry")
    maximum = 0.90 if formal_probe else 1.0
    if not math.isfinite(value) or not 0 < value <= maximum:
        bound = "0.90" if formal_probe else "1.0"
        raise FinalizationError(f"probe peak-memory fraction must be in (0, {bound}]")
    return value


def validate_contract_and_inputs(
    *,
    root: Path,
    contract_path: Path,
    environment_path: Path,
    monitor_path: Path,
    submission_path: Path,
    terminal_status: str,
    pipeline_exit_code: int,
) -> dict[str, Any]:
    execution_contract_sha = sha256_file(contract_path)
    contract = load_json(contract_path, "cell execution contract")
    if sha256_file(contract_path) != execution_contract_sha:
        raise FinalizationError("cell execution contract changed while it was parsed")
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        raise FinalizationError("unsupported or missing cell-contract schema_version")
    attempt_id = require_string(contract.get("attempt_id"), "cell contract attempt_id")
    cell_id = require_string(contract.get("cell_id"), "cell contract cell_id")
    if SAFE_ID_RE.fullmatch(attempt_id) is None or SAFE_ID_RE.fullmatch(cell_id) is None:
        raise FinalizationError("cell contract cell_id/attempt_id are not safe identifiers")
    if root.name not in {attempt_id, f"{cell_id}__{attempt_id}"}:
        raise FinalizationError("cell contract attempt_id/cell_id do not identify the output root")
    run_kind = require_string(contract.get("run_kind"), "cell contract run_kind")
    stage_class = require_string(contract.get("stage_class"), "cell contract stage_class")
    if stage_class not in {"ENGINEERING", "FORMAL"}:
        raise FinalizationError("cell contract stage_class must be ENGINEERING or FORMAL")
    success_status = require_string(
        contract.get("success_status"), "cell contract success_status"
    )
    probe_class = classify_probe_contract(
        contract,
        run_kind=run_kind,
        stage_class=stage_class,
        success_status=success_status,
    )
    probe = probe_class is not None
    formal_run = stage_class == "FORMAL"
    formal_claim = "G2" in run_kind.upper() or is_formal_success_claim(success_status)
    if formal_claim != formal_run:
        raise FinalizationError("stage_class and formal/G2 execution claims disagree")

    failed = pipeline_exit_code != 0
    failure_class: str | None = None
    if failed:
        gpu_oom = probe_class == "ENGINEERING" and detect_gpu_oom(root)
        expected_failure = BLOCKED_GPU_MEMORY_STATUS if gpu_oom else "LOCAL_CELL_FAILED"
        if terminal_status != expected_failure:
            raise FinalizationError(
                f"failed pipeline evidence requires {expected_failure}"
            )
        failure_class = (
            BLOCKED_GPU_MEMORY_STATUS if gpu_oom else "PIPELINE_EXIT_NONZERO"
        )
    elif terminal_status != success_status:
        raise FinalizationError("terminal status disagrees with cell contract success_status")

    bound_environment_path, environment_sha = contract_bound_file(
        contract,
        path_field="environment_receipt",
        sha_field="environment_receipt_sha256",
        label="environment receipt",
        expected_path=environment_path,
    )
    environment = load_json(bound_environment_path, "environment receipt")
    formal_g1, environment_manifest_sha, environment_manifest_path = validate_environment(
        environment, contract, stage_class, bound_environment_path
    )

    monitor, stopped_at = validate_monitor(
        root, monitor_path, require_healthy=not failed
    )
    (
        validator_path,
        runtime_records,
        runtime_manifest_path,
        runtime_manifest_sha,
        finalizer_sha,
    ) = validate_runtime_manifest(contract)
    submission, submission_sha = validate_submission_receipt(
        path=submission_path,
        bg_work=runtime_manifest_path.parent,
        contract_path=contract_path,
        contract_sha256=execution_contract_sha,
        contract=contract,
    )
    validation_path: Path | None = None
    validation: dict[str, Any] | None = None
    resolved_config_path: Path | None = None
    resolved_config_sha: str | None = None
    if not failed:
        validation_path, validation = validate_validation_receipt(
            root,
            contract,
            probe=probe,
            formal_run=formal_run,
            validator_sha256=runtime_records["software/validate_cell_output.py"],
        )
        resolved_config_path, resolved_config_sha = validate_resolved_config(root, contract)
        rerun_validator_exact(
            root=root,
            contract=contract,
            environment_path=bound_environment_path,
            validator_path=validator_path,
            validation_path=validation_path,
        )

    bindings: dict[str, str] = {}
    binding_paths: dict[str, Path] = {}
    for path_field, sha_field, label in (
        ("spec_path", "spec_sha256", "design specification"),
        ("design_checkpoint", "design_checkpoint_sha256", "design checkpoint"),
        (
            "inverse_fold_checkpoint",
            "inverse_fold_checkpoint_sha256",
            "inverse-fold checkpoint",
        ),
        ("folding_checkpoint", "folding_checkpoint_sha256", "folding checkpoint"),
        ("mols_path", "mols_sha256", "molecular reference archive"),
        (
            "model_inputs_manifest_path",
            "model_inputs_manifest_sha256",
            "model-inputs manifest",
        ),
        ("spec_gate_bundle_path", "spec_gate_bundle_sha256", "spec-gate bundle"),
    ):
        bound_path, digest = contract_bound_file(
            contract,
            path_field=path_field,
            sha_field=sha_field,
            label=label,
        )
        binding_paths[path_field] = bound_path
        bindings[sha_field] = digest
    bindings["runtime_scripts_manifest_sha256"] = runtime_manifest_sha
    combined = optional_contract_bound_file(
        contract,
        path_field="input_and_model_manifest_path",
        sha_field="input_and_model_manifest_sha256",
        label="input-and-model manifest",
    )
    if combined is not None:
        bindings["input_and_model_manifest_sha256"] = combined[1]

    probe_fields: dict[str, Any] = {}
    if probe and not failed:
        probe_id = require_string(contract.get("probe_id"), "probe_id")
        checkpoint_name = require_string(contract.get("checkpoint_name"), "checkpoint_name")
        if checkpoint_name not in {"diverse", "adherence"}:
            raise FinalizationError("probe checkpoint_name must be diverse or adherence")
        checkpoint_sha = require_sha256(
            contract.get("checkpoint_sha256", bindings["design_checkpoint_sha256"]),
            "checkpoint_sha256",
        )
        if checkpoint_sha != bindings["design_checkpoint_sha256"]:
            raise FinalizationError("probe checkpoint SHA is not the executed design checkpoint")
        if probe_class == "ENGINEERING":
            match = ENGINEERING_MEMORY_PROBE_ID.fullmatch(probe_id)
            if match is None or probe_id != cell_id or checkpoint_name != match.group(1):
                raise FinalizationError("engineering probe identity changed after binding")
            if (
                tuple(binding_paths["spec_path"].parts[-4:])
                != ENGINEERING_6XYM_SPEC_SUFFIX
            ):
                raise FinalizationError("engineering probe is not bound to the frozen 6XYM spec")
            if binding_paths["design_checkpoint"].name != f"boltzgen1_{checkpoint_name}.ckpt":
                raise FinalizationError("engineering probe checkpoint path/name mismatch")
            diffusion_batch_size = 1
        else:
            if not probe_id.startswith("6xym_") or probe_id != cell_id:
                raise FinalizationError(
                    "formal resource probe must use its canonical 6xym probe_id"
                )
            if f"_{checkpoint_name}_" not in f"_{probe_id}_":
                raise FinalizationError("probe_id/checkpoint_name mismatch")
            if (
                require_int(
                    contract.get("diffusion_batch_size"),
                    "diffusion_batch_size",
                    minimum=1,
                )
                != 5
            ):
                raise FinalizationError("formal 6XYM probe requires diffusion_batch_size=5")
            diffusion_batch_size = 5
        probe_fields = {
            "probe_id": probe_id,
            "checkpoint_name": checkpoint_name,
            "checkpoint_sha256": checkpoint_sha,
            "num_designs": require_int(contract.get("expected_designs"), "expected_designs"),
            "diffusion_batch_size": diffusion_batch_size,
            "fold_samples": require_int(
                contract.get("expected_fold_samples"), "expected_fold_samples"
            ),
            "peak_memory_fraction": load_peak_fraction(
                root,
                contract,
                formal_probe=probe_class == "FORMAL",
            ),
        }

    if sha256_file(contract_path) != execution_contract_sha:
        raise FinalizationError("cell execution contract changed during input validation")
    return {
        "contract": contract,
        "execution_contract_sha256": execution_contract_sha,
        "environment": environment,
        "monitor": monitor,
        "submission": submission,
        "submission_path": submission_path,
        "submission_receipt_sha256": submission_sha,
        "validation": validation,
        "validation_path": validation_path,
        "resolved_config_path": resolved_config_path,
        "resolved_config_sha256": resolved_config_sha,
        "environment_sha256": environment_sha,
        "environment_manifest_sha256": environment_manifest_sha,
        "environment_manifest_path": environment_manifest_path,
        "formal_g1": formal_g1,
        "formal_run": formal_run,
        "failed": failed,
        "pipeline_exit_code": pipeline_exit_code,
        "probe": probe,
        "probe_class": probe_class,
        "failure_class": failure_class,
        "probe_fields": probe_fields,
        "bindings": bindings,
        "runtime_manifest_path": runtime_manifest_path,
        "runtime_records": runtime_records,
        "finalizer_sha256": finalizer_sha,
        "validator_path": validator_path,
        "validator_sha256": runtime_records["software/validate_cell_output.py"],
        "environment_path": bound_environment_path,
        "stopped_at_utc": stopped_at,
    }


def revalidate_probe_semantics(root: Path, evidence: dict[str, Any]) -> None:
    """Keep derived peak/OOM semantics coherent with the bytes being sealed."""

    if not evidence["probe"]:
        return
    if evidence["failed"]:
        if evidence["probe_class"] != "ENGINEERING":
            return
        observed_oom = detect_gpu_oom(root)
        expected_oom = evidence["failure_class"] == BLOCKED_GPU_MEMORY_STATUS
        if observed_oom != expected_oom:
            raise FinalizationError("GPU OOM evidence changed during finalization")
        return
    observed_peak = load_peak_fraction(
        root,
        evidence["contract"],
        formal_probe=evidence["probe_class"] == "FORMAL",
    )
    if observed_peak != evidence["probe_fields"]["peak_memory_fraction"]:
        raise FinalizationError("probe peak-memory evidence changed during finalization")


def _walk_regular_files(
    root: Path,
    directory: Path,
    *,
    excluded: frozenset[str],
) -> list[tuple[str, Path]]:
    payload: list[tuple[str, Path]] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name.encode("utf-8"))
    except OSError as exc:
        raise FinalizationError(f"cannot enumerate output directory {directory}: {exc}") from exc
    for entry in entries:
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix()
        if "\n" in relative or "\r" in relative or "\0" in relative:
            raise FinalizationError(f"output contains an unsafe pathname: {relative!r}")
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            raise FinalizationError(f"cannot inspect output member: {relative}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise FinalizationError(f"output tree contains a symlink: {relative}")
        if stat.S_ISDIR(mode):
            payload.extend(_walk_regular_files(root, path, excluded=excluded))
            continue
        if not stat.S_ISREG(mode):
            raise FinalizationError(f"output tree contains a special file: {relative}")
        if relative in LEGACY_ROOT_OUTPUTS:
            raise FinalizationError(f"legacy root finalization file is forbidden: {relative}")
        if relative in TERMINAL_RELATIVES and relative not in excluded:
            raise FinalizationError(f"conflicting terminal marker exists: {relative}")
        if relative in excluded:
            continue
        payload.append((relative, path))
    return payload


def build_manifest(root: Path, marker_relative: str) -> tuple[bytes, int]:
    excluded = frozenset({OUTPUT_MANIFEST_RELATIVE, marker_relative})
    payload = _walk_regular_files(root, root, excluded=excluded)
    payload.sort(key=lambda item: item[0].encode("utf-8"))
    records = [f"{sha256_file(path)}  ./{relative}\n" for relative, path in payload]
    if not records:
        raise FinalizationError("output manifest may not be empty")
    return "".join(records).encode("utf-8"), len(records)


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_existing(path: Path, expected: bytes, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise FinalizationError(f"{label} exists with an unsafe type: {path}")
    if path.read_bytes() != expected:
        raise FinalizationError(f"existing {label} differs from the frozen evidence")


def freeze_evidence_tree(root: Path, marker_relative: str) -> None:
    """Freeze every evidence leaf and every non-publication directory before SUCCESS."""
    marker_path = root / marker_relative
    directories: list[Path] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in names:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise FinalizationError(f"unsafe directory while freezing evidence: {path}")
            directories.append(path)
        for name in files:
            path = base / name
            if path == marker_path:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise FinalizationError(f"unsafe file while freezing evidence: {path}")
            os.chmod(path, (stat.S_IMODE(mode) & 0o555) | 0o400, follow_symlinks=False)
            if path.stat(follow_symlinks=False).st_mode & 0o222:
                raise FinalizationError(f"evidence file remains writable after freeze: {path}")
    publication_directories = {root, root / "operator_logs"}
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        if path in publication_directories:
            continue
        os.chmod(path, 0o555, follow_symlinks=False)
        if path.stat(follow_symlinks=False).st_mode & 0o222:
            raise FinalizationError(f"evidence directory remains writable after freeze: {path}")


def verify_evidence_frozen(root: Path, marker_relative: str) -> None:
    marker_path = root / marker_relative
    publication_directories = {root, root / "operator_logs"}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise FinalizationError(f"frozen evidence contains a symlink: {path}")
        if stat.S_ISREG(mode) and path != marker_path and mode & 0o222:
            raise FinalizationError(f"evidence file is writable: {path}")
        if stat.S_ISDIR(mode) and path not in publication_directories and mode & 0o222:
            raise FinalizationError(f"evidence directory is writable: {path}")


def stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def digest_descriptor(descriptor: int) -> str:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise FinalizationError("cannot hash a non-regular evidence descriptor")
    digest = hashlib.sha256()
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    after = os.fstat(descriptor)
    if stat_fingerprint(before) != stat_fingerprint(after):
        raise FinalizationError("evidence changed while its closure digest was computed")
    return digest.hexdigest()


def capture_evidence_snapshot(
    root_descriptor: int,
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], str | None]]:
    snapshot: dict[
        str, tuple[tuple[int, int, int, int, int, int, int], str | None]
    ] = {"": (stat_fingerprint(os.fstat(root_descriptor)), None)}
    base_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

    def visit(directory: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory), key=lambda item: item.encode("utf-8")):
            if not name or "/" in name or any(ord(character) < 32 for character in name):
                raise FinalizationError("unsafe evidence pathname during snapshot")
            relative = f"{prefix}/{name}" if prefix else name
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise FinalizationError(f"evidence snapshot contains a symlink: {relative}")
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, base_flags | os.O_DIRECTORY, dir_fd=directory)
                try:
                    opened = os.fstat(child)
                    if stat_fingerprint(before) != stat_fingerprint(opened):
                        raise FinalizationError(f"directory changed before snapshot: {relative}")
                    visit(child, relative)
                    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat_fingerprint(opened) != stat_fingerprint(after):
                        raise FinalizationError(f"directory changed during snapshot: {relative}")
                    snapshot[relative] = (stat_fingerprint(after), None)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise FinalizationError(f"evidence snapshot contains a special file: {relative}")
            descriptor = os.open(name, base_flags, dir_fd=directory)
            try:
                opened = os.fstat(descriptor)
                if stat_fingerprint(before) != stat_fingerprint(opened):
                    raise FinalizationError(f"file changed before snapshot: {relative}")
                digest = digest_descriptor(descriptor)
                after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat_fingerprint(opened) != stat_fingerprint(after):
                    raise FinalizationError(f"file changed during snapshot: {relative}")
                snapshot[relative] = (stat_fingerprint(after), digest)
            finally:
                os.close(descriptor)

    visit(root_descriptor, "")
    root_after = os.fstat(root_descriptor)
    if snapshot[""][0] != stat_fingerprint(root_after):
        raise FinalizationError("attempt root changed during evidence snapshot")
    return snapshot


def close_evidence_snapshot(
    root_descriptor: int,
    snapshot: dict[str, tuple[tuple[int, int, int, int, int, int, int], str | None]],
) -> None:
    if stat_fingerprint(os.fstat(root_descriptor)) != snapshot[""][0]:
        raise FinalizationError("attempt root changed before terminal publication")
    base_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for relative in sorted(
        (item for item in snapshot if item), key=lambda item: item.encode("utf-8")
    ):
        expected, expected_digest = snapshot[relative]
        components = relative.split("/")
        directory = os.dup(root_descriptor)
        try:
            for component in components[:-1]:
                following = os.open(component, base_flags | os.O_DIRECTORY, dir_fd=directory)
                os.close(directory)
                directory = following
            flags = base_flags | (os.O_DIRECTORY if stat.S_ISDIR(expected[2]) else 0)
            descriptor = os.open(components[-1], flags, dir_fd=directory)
            current_path = os.stat(
                components[-1], dir_fd=directory, follow_symlinks=False
            )
        finally:
            os.close(directory)
        try:
            current_descriptor = os.fstat(descriptor)
            if (
                stat_fingerprint(current_path) != expected
                or stat_fingerprint(current_descriptor) != expected
            ):
                raise FinalizationError(
                    f"evidence member identity changed before terminal publication: {relative}"
                )
            if expected_digest is not None and digest_descriptor(descriptor) != expected_digest:
                raise FinalizationError(
                    f"evidence member content changed before terminal publication: {relative}"
                )
        finally:
            os.close(descriptor)
    if stat_fingerprint(os.fstat(root_descriptor)) != snapshot[""][0]:
        raise FinalizationError("attempt root changed during terminal closure")


def publish_no_replace(
    path: Path,
    payload: bytes,
    label: str,
    *,
    terminal_publication: bool = False,
    expected_parent_identity: tuple[int, int] | None = None,
    pre_link_check: Callable[[], None] | None = None,
) -> bool:
    """Publish through a same-filesystem hard link without an overwrite window."""
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(path.parent, parent_flags)
    parent_identity = os.fstat(parent_descriptor)
    if expected_parent_identity is not None and expected_parent_identity != (
        parent_identity.st_dev,
        parent_identity.st_ino,
    ):
        os.close(parent_descriptor)
        raise FinalizationError(f"{label} parent is not the guarded evidence directory")
    parent_now = os.stat(path.parent, follow_symlinks=False)
    if (parent_identity.st_dev, parent_identity.st_ino) != (
        parent_now.st_dev,
        parent_now.st_ino,
    ):
        os.close(parent_descriptor)
        raise FinalizationError(f"{label} parent directory changed before publication")
    if path.exists() or path.is_symlink():
        try:
            verify_existing(path, payload, label)
            return False
        finally:
            os.close(parent_descriptor)
    output_root = path.parents[1]
    temporary_directory = output_root.parent
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_root.name}.{path.name}.",
        suffix=".tmp",
        dir=temporary_directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        if pre_link_check is not None:
            pre_link_check()
        try:
            os.link(
                temporary,
                path.name,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            verify_existing(path, payload, label)
            return False
        try:
            os.fsync(parent_descriptor)
        except OSError:
            # Once the terminal link exists there must be no failing step which
            # reports an unsealed attempt. The marker itself records that its
            # two publication parent directories intentionally remain mutable.
            if not terminal_publication:
                raise
        return True
    except OSError as exc:
        raise FinalizationError(f"cannot atomically publish {label}: {path}: {exc}") from exc
    finally:
        os.close(parent_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def success_payload(
    *,
    evidence: dict[str, Any],
    monitor_path: Path,
    terminal_status: str,
    manifest_sha256: str,
    manifest_entries: int,
) -> dict[str, Any]:
    contract = evidence["contract"]
    if evidence["validation_path"] is None or evidence["resolved_config_sha256"] is None:
        raise FinalizationError("successful finalization lacks validation evidence")
    submission = evidence["submission"]
    marker: dict[str, Any] = {
        "schema_version": SUCCESS_SCHEMA,
        "status": "SUCCESS",
        "terminal_status": terminal_status,
        "pipeline_exit_code": 0,
        "executor_kind": EXECUTOR_KIND,
        "cell_id": contract["cell_id"],
        "attempt_id": contract["attempt_id"],
        "run_kind": contract["run_kind"],
        "formal_g1": evidence["formal_g1"],
        "formal_g1_receipt_sha256": (
            evidence["environment_sha256"] if evidence["formal_run"] else None
        ),
        "environment_manifest_sha256": evidence["environment_manifest_sha256"],
        "completed_at_utc": evidence["stopped_at_utc"],
        "execution_contract_sha256": evidence["execution_contract_sha256"],
        "environment_receipt_sha256": evidence["environment_sha256"],
        "monitor_stopped_sha256": sha256_file(monitor_path),
        "monitor_healthy": evidence["monitor"]["monitor_healthy"],
        "submission_receipt_sha256": evidence["submission_receipt_sha256"],
        "systemd_unit": submission["unit"],
        "submission_token_sha256": hashlib.sha256(
            submission["submission_token"].encode("ascii")
        ).hexdigest(),
        "invocation_id": submission["invocation_id"],
        "executor_uid": submission["executor_uid"],
        "exec_start_sha256": submission["exec_start_sha256"],
        "cell_contract_sha256": sha256_file(evidence["validation_path"]),
        "validation_sha256": sha256_file(evidence["validation_path"]),
        "resolved_config_manifest_sha256": evidence["resolved_config_sha256"],
        "validator_sha256": evidence["validator_sha256"],
        "finalizer_sha256": evidence["finalizer_sha256"],
        "output_manifest_sha256": manifest_sha256,
        "output_manifest_entry_count": manifest_entries,
        "evidence_freeze_schema_version": "WSL2_OUTPUT_EVIDENCE_FREEZE_V1",
        "evidence_files_read_only": True,
        "evidence_directories_read_only_except_terminal_parents": True,
        "terminal_publication_parents_mutable": True,
        **evidence["bindings"],
    }
    if evidence["probe"]:
        marker.update(evidence["probe_fields"])
    return marker


def failure_payload(
    *,
    evidence: dict[str, Any],
    monitor_path: Path,
    terminal_status: str,
    manifest_sha256: str,
    manifest_entries: int,
) -> dict[str, Any]:
    contract = evidence["contract"]
    submission = evidence["submission"]
    return {
        "schema_version": FAILURE_SCHEMA,
        "status": "FAILURE",
        "terminal_status": terminal_status,
        "pipeline_exit_code": evidence["pipeline_exit_code"],
        "failure_class": evidence["failure_class"],
        "executor_kind": EXECUTOR_KIND,
        "cell_id": contract["cell_id"],
        "attempt_id": contract["attempt_id"],
        "run_kind": contract["run_kind"],
        "formal_g1": evidence["formal_g1"],
        "formal_g1_receipt_sha256": (
            evidence["environment_sha256"] if evidence["formal_run"] else None
        ),
        "environment_manifest_sha256": evidence["environment_manifest_sha256"],
        "completed_at_utc": evidence["stopped_at_utc"],
        "execution_contract_sha256": evidence["execution_contract_sha256"],
        "environment_receipt_sha256": evidence["environment_sha256"],
        "monitor_stopped_sha256": sha256_file(monitor_path),
        "monitor_healthy": evidence["monitor"]["monitor_healthy"],
        "submission_receipt_sha256": evidence["submission_receipt_sha256"],
        "systemd_unit": submission["unit"],
        "submission_token_sha256": hashlib.sha256(
            submission["submission_token"].encode("ascii")
        ).hexdigest(),
        "invocation_id": submission["invocation_id"],
        "executor_uid": submission["executor_uid"],
        "exec_start_sha256": submission["exec_start_sha256"],
        "validator_sha256": evidence["validator_sha256"],
        "finalizer_sha256": evidence["finalizer_sha256"],
        "output_manifest_sha256": manifest_sha256,
        "output_manifest_entry_count": manifest_entries,
        "evidence_freeze_schema_version": "WSL2_OUTPUT_EVIDENCE_FREEZE_V1",
        "evidence_files_read_only": True,
        "evidence_directories_read_only_except_terminal_parents": True,
        "terminal_publication_parents_mutable": True,
        **evidence["bindings"],
    }


def finalize(arguments: argparse.Namespace) -> dict[str, Any]:
    root = canonical_attempt_root(arguments.attempt_root)
    root_guard = DirectoryIdentityGuard(root, "attempt root")
    logs_guard = DirectoryIdentityGuard(root / "operator_logs", "operator-logs directory")
    contract_path = canonical_existing_file(arguments.cell_contract, "cell execution contract")
    environment_path = canonical_existing_file(
        arguments.environment_receipt, "environment receipt"
    )
    monitor_path = canonical_existing_file(arguments.monitor_stopped, "stopped-monitor receipt")
    submission_path = canonical_existing_file(
        arguments.submission_receipt, "submission receipt"
    )
    terminal_status = require_string(arguments.terminal_status, "terminal status")
    if not 0 <= arguments.pipeline_exit_code <= 255:
        raise FinalizationError("pipeline exit code must be in the POSIX range 0..255")

    evidence = validate_contract_and_inputs(
        root=root,
        contract_path=contract_path,
        environment_path=environment_path,
        monitor_path=monitor_path,
        submission_path=submission_path,
        terminal_status=terminal_status,
        pipeline_exit_code=arguments.pipeline_exit_code,
    )
    contract = evidence["contract"]
    bg_work = evidence["runtime_manifest_path"].parent
    runs_path = bg_work / "runs"
    cell_path = runs_path / contract["cell_id"]
    expected_root = cell_path / contract["attempt_id"]
    expected_logs = expected_root / "operator_logs"
    if root != expected_root or logs_guard.path != expected_logs:
        raise FinalizationError(
            "attempt root must be BG_WORK/runs/{cell_id}/{attempt_id}"
        )
    for path, label in (
        (bg_work, "BG_WORK directory"),
        (runs_path, "BG_WORK runs directory"),
        (cell_path, "cell directory"),
        (root, "attempt directory"),
        (expected_logs, "operator-logs directory"),
    ):
        require_canonical_directory(path, label)
    bg_guard = DirectoryIdentityGuard(bg_work, "BG_WORK directory")
    runs_guard = DirectoryIdentityGuard(runs_path, "BG_WORK runs directory")
    cell_guard = DirectoryIdentityGuard(cell_path, "cell directory")
    hierarchy_guards = (bg_guard, runs_guard, cell_guard, root_guard, logs_guard)
    verify_hierarchy_closure(hierarchy_guards)
    if evidence["failed"]:
        marker_relative = (
            "operator_logs/probe.FAILURE.json"
            if evidence["probe"]
            else "operator_logs/cell.FAILURE.json"
        )
    else:
        marker_relative = PROBE_SUCCESS_RELATIVE if evidence["probe"] else CELL_SUCCESS_RELATIVE
    manifest_path = root / OUTPUT_MANIFEST_RELATIVE
    marker_path = root / marker_relative
    for other_relative in TERMINAL_RELATIVES - {marker_relative}:
        other_marker = root / other_relative
        if other_marker.exists() or other_marker.is_symlink():
            raise FinalizationError(f"conflicting terminal marker already exists: {other_marker}")

    manifest_bytes, manifest_entries = build_manifest(root, marker_relative)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    revalidate_probe_semantics(root, evidence)
    payload_builder = failure_payload if evidence["failed"] else success_payload
    marker = payload_builder(
        evidence=evidence,
        monitor_path=monitor_path,
        terminal_status=terminal_status,
        manifest_sha256=manifest_sha,
        manifest_entries=manifest_entries,
    )
    marker_bytes = canonical_json_bytes(marker)
    marker_label = "FAILURE marker" if evidence["failed"] else "SUCCESS marker"

    marker_exists = marker_path.exists() or marker_path.is_symlink()
    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    if marker_exists:
        if not manifest_exists:
            raise FinalizationError("terminal marker exists without its output manifest")
        verify_existing(manifest_path, manifest_bytes, "output manifest")
        verify_existing(marker_path, marker_bytes, marker_label)
        verify_evidence_frozen(root, marker_relative)
        return {
            "status": terminal_status,
            "attempt_root": str(root),
            "marker_path": marker_relative,
            "marker_sha256": sha256_file(marker_path),
            "output_manifest_sha256": manifest_sha,
            "idempotent_reentry": True,
        }

    if manifest_exists:
        verify_existing(manifest_path, manifest_bytes, "output manifest")
    else:
        publish_no_replace(
            manifest_path,
            manifest_bytes,
            "output manifest",
            expected_parent_identity=logs_guard.identity,
        )

    # A payload mutation between hashing and terminal publication must block SUCCESS.
    post_manifest, post_count = build_manifest(root, marker_relative)
    if post_manifest != manifest_bytes or post_count != manifest_entries:
        raise FinalizationError("output changed while its manifest was being published")
    verify_existing(manifest_path, manifest_bytes, "output manifest")
    verify_hierarchy_closure(hierarchy_guards)
    if sha256_file(evidence["submission_path"]) != evidence["submission_receipt_sha256"]:
        raise FinalizationError("submission receipt changed before evidence freeze")
    revalidate_formal_environment_evidence(evidence)
    if not evidence["failed"]:
        rerun_validator_exact(
            root=root,
            contract=evidence["contract"],
            environment_path=evidence["environment_path"],
            validator_path=evidence["validator_path"],
            validation_path=evidence["validation_path"],
        )
    freeze_evidence_tree(root, marker_relative)
    verify_hierarchy_closure(hierarchy_guards)
    frozen_manifest, frozen_count = build_manifest(root, marker_relative)
    if frozen_manifest != manifest_bytes or frozen_count != manifest_entries:
        raise FinalizationError("output changed while evidence was being frozen")
    frozen_snapshot = capture_evidence_snapshot(root_guard.descriptor)
    verify_evidence_frozen(root, marker_relative)

    def close_before_terminal_link() -> None:
        if sha256_file(contract_path) != evidence["execution_contract_sha256"]:
            raise FinalizationError("cell execution contract changed before terminal publication")
        if sha256_file(evidence["submission_path"]) != evidence["submission_receipt_sha256"]:
            raise FinalizationError("submission receipt changed before terminal publication")
        revalidate_formal_environment_evidence(evidence)
        if not evidence["failed"]:
            rerun_validator_exact(
                root=root,
                contract=evidence["contract"],
                environment_path=evidence["environment_path"],
                validator_path=evidence["validator_path"],
                validation_path=evidence["validation_path"],
            )
        revalidate_probe_semantics(root, evidence)
        close_evidence_snapshot(root_guard.descriptor, frozen_snapshot)
        verify_hierarchy_closure(hierarchy_guards)

    published = publish_no_replace(
        marker_path,
        marker_bytes,
        marker_label,
        terminal_publication=True,
        expected_parent_identity=logs_guard.identity,
        pre_link_check=close_before_terminal_link,
    )

    return {
        "status": terminal_status,
        "attempt_root": str(root),
        "marker_path": marker_relative,
        "marker_sha256": hashlib.sha256(marker_bytes).hexdigest(),
        "output_manifest_sha256": manifest_sha,
        "idempotent_reentry": not published,
    }


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-root", required=True)
    parser.add_argument("--cell-contract", required=True)
    parser.add_argument("--environment-receipt", required=True)
    parser.add_argument("--monitor-stopped", required=True)
    parser.add_argument("--submission-receipt", required=True)
    parser.add_argument("--terminal-status", required=True)
    parser.add_argument("--pipeline-exit-code", required=True, type=int)
    return parser.parse_args(argv)


def compute_peak_memory_cli(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        prog="finalize_local_attempt.py compute-peak-memory",
        description="Derive the immutable peak-memory receipt from canonical GPU telemetry.",
    )
    parser.add_argument("--gpu-monitor", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    monitor_path = canonical_existing_file(arguments.gpu_monitor, "GPU monitor CSV")
    output_path = Path(arguments.output)
    if not output_path.is_absolute():
        raise FinalizationError("peak-memory output path must be absolute")
    value = write_peak_memory_fraction(monitor_path, output_path)
    return {
        "status": "PEAK_MEMORY_RECORDED",
        "peak_memory_fraction": value,
        "output": str(output_path),
    }


def detect_gpu_oom_cli(argv: list[str]) -> tuple[dict[str, Any], int]:
    parser = argparse.ArgumentParser(
        prog="finalize_local_attempt.py detect-gpu-oom",
        description="Classify a failed engineering probe from frozen stage evidence.",
    )
    parser.add_argument("--attempt-root", required=True)
    arguments = parser.parse_args(argv)
    root = canonical_attempt_root(arguments.attempt_root)
    detected = detect_gpu_oom(root)
    return {
        "status": "GPU_OOM_DETECTED" if detected else "GPU_OOM_NOT_DETECTED",
        "attempt_root": str(root),
        "gpu_oom": detected,
    }, (0 if detected else 2)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["compute-peak-memory"]:
            result = compute_peak_memory_cli(arguments[1:])
            exit_code = 0
        elif arguments[:1] == ["detect-gpu-oom"]:
            result, exit_code = detect_gpu_oom_cli(arguments[1:])
        else:
            result = finalize(parse_arguments(arguments))
            exit_code = 0
    except (FinalizationError, OSError, ValueError, KeyError) as exc:
        print(f"finalization failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
