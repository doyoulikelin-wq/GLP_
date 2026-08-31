#!/usr/bin/env python3
"""Run one single-GPU Windows-owner multi-state folding evaluation.

This orchestrator intentionally has a small, frozen first panel: two reliable
development anchors by five declared development states, with five fold samples
per task.  It creates a new attempt, never uses reuse semantics, holds the same
global GPU lock as the exploratory runner, and publishes a terminal manifest.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PANEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DEFAULT_CANDIDATES = ("design_1", "design_3")
DEFAULT_STATES = ("DEV_00", "DEV_01", "DEV_05", "DEV_06", "DEV_15")
SAMPLES_PER_TASK = 5
MIN_FREE_BYTES = 1024 * 1024 * 1024
OOM_RE = re.compile(
    r"CUDA[^\n]{0,80}out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED",
    re.IGNORECASE,
)


class RunFailure(RuntimeError):
    """A controlled terminal failure that must remain in the run evidence."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunFailure(f"invalid JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunFailure(f"expected JSON object: {path}")
    return value


def atomic_write(path: Path, content: str, *, replace_input_status: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not replace_input_status or path.read_text(encoding="utf-8").strip() != "INPUTS_READY":
            raise RunFailure(f"refusing to overwrite terminal file: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def replace_owned_terminal(path: Path, content: str) -> None:
    """Atomically replace a terminal file owned exclusively by this runner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.replace-{os.getpid()}")
    if temporary.exists():
        raise RunFailure(f"terminal replacement temporary already exists: {temporary}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def parse_sha256_manifest(path: Path) -> list[tuple[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise RunFailure(f"missing or unsafe SHA-256 manifest: {path}")
    records: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (?:\./)?([^\x00\r\n]+)", line)
        if match is None:
            raise RunFailure(f"invalid SHA-256 manifest row in {path}: {line!r}")
        digest, relative = match.groups()
        member = Path(relative)
        if (
            member.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in member.parts)
            or relative in seen
        ):
            raise RunFailure(f"unsafe or duplicate manifest path: {relative!r}")
        seen.add(relative)
        records.append((relative, digest))
    if not records:
        raise RunFailure(f"empty SHA-256 manifest: {path}")
    return records


def verify_manifest(base: Path, manifest: Path) -> dict[str, str]:
    resolved_base = base.resolve(strict=True)
    verified: dict[str, str] = {}
    for relative, expected in parse_sha256_manifest(manifest):
        member = base / relative
        try:
            resolved = member.resolve(strict=True)
        except OSError as exc:
            raise RunFailure(f"manifest member missing: {member}") from exc
        if not (resolved.parent == resolved_base or resolved_base in resolved.parents):
            raise RunFailure(f"manifest member escapes base: {relative}")
        if member.is_symlink() or not member.is_file():
            raise RunFailure(f"manifest member is not a regular file: {relative}")
        observed = sha256_file(member)
        if observed != expected:
            raise RunFailure(f"SHA-256 mismatch: {member}")
        verified[relative] = observed
    return verified


def expected_runtime_hashes(runtime_root: Path) -> dict[str, str]:
    manifest = runtime_root / "SHA256SUMS"
    records = dict(parse_sha256_manifest(manifest))
    wanted = {"boltz2_conf_final.ckpt", "mols.zip"}
    missing = wanted - set(records)
    if missing:
        raise RunFailure(f"runtime manifest is missing {sorted(missing)}")
    return {name: records[name] for name in sorted(wanted)}


def verify_runtime(runtime_root: Path, expected: Mapping[str, str]) -> dict[str, str]:
    observed: dict[str, str] = {}
    for name, digest in expected.items():
        path = runtime_root / name
        if path.is_symlink() or not path.is_file():
            raise RunFailure(f"missing or unsafe runtime asset: {path}")
        value = sha256_file(path)
        if value != digest:
            raise RunFailure(f"runtime asset SHA-256 mismatch: {path}")
        observed[name] = value
    return observed


def command_output(command: Sequence[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RunFailure(
            f"command failed ({result.returncode}): {shlex.join(command)}: {result.stderr.strip()}"
        )
    return result.stdout


def run_logged(
    label: str,
    command: Sequence[str],
    logs: Path,
    *,
    cwd: Path | None = None,
) -> float:
    stdout_path = logs / f"{label}.stdout.txt"
    stderr_path = logs / f"{label}.stderr.txt"
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run(list(command), cwd=cwd, stdout=stdout, stderr=stderr, check=False)
    duration = time.monotonic() - started
    atomic_write(logs / f"{label}.exit_code.txt", f"{result.returncode}\n")
    atomic_write(logs / f"{label}.duration_seconds.txt", f"{duration:.6f}\n")
    if result.returncode != 0:
        raise RunFailure(f"stage {label} failed with exit code {result.returncode}")
    return duration


class GPUMonitor:
    def __init__(self, output: Path, errors: Path) -> None:
        self.output = output
        self.errors = errors
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.row_count = 0
        self.peak_used_mib: int | None = None
        self.total_mib: int | None = None
        self.failed: str | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise RunFailure("GPU monitor already started")
        self.thread = threading.Thread(target=self._run, name="owner-gpu-monitor", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while self.row_count < 1 and self.failed is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.failed is not None or self.row_count < 1:
            raise RunFailure(f"GPU monitor failed to start: {self.failed or 'no samples'}")

    def _run(self) -> None:
        header = [
            "observed_at_utc", "index", "name", "memory.total_mib",
            "memory.used_mib", "utilization.gpu_percent", "power.draw_w",
        ]
        try:
            with self.output.open("w", encoding="utf-8", newline="") as stream, self.errors.open(
                "w", encoding="utf-8"
            ) as error_stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                stream.flush()
                while not self.stop_event.is_set():
                    result = subprocess.run(
                        [
                            "nvidia-smi",
                            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,power.draw",
                            "--format=csv,noheader,nounits",
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                        check=False,
                    )
                    if result.returncode != 0:
                        error_stream.write(result.stderr)
                        error_stream.flush()
                        self.failed = f"nvidia-smi exit code {result.returncode}"
                        return
                    rows = list(csv.reader(result.stdout.splitlines()))
                    if len(rows) != 1 or len(rows[0]) != 6:
                        self.failed = f"unexpected nvidia-smi row count/shape: {rows!r}"
                        return
                    values = [value.strip() for value in rows[0]]
                    total = int(float(values[2]))
                    used = int(float(values[3]))
                    writer.writerow([utc_now(), *values])
                    stream.flush()
                    self.row_count += 1
                    self.total_mib = total
                    self.peak_used_mib = used if self.peak_used_mib is None else max(self.peak_used_mib, used)
                    self.stop_event.wait(1.0)
        except BaseException as exc:  # preserve monitor failures for the terminal receipt
            self.failed = repr(exc)

    def stop(self) -> None:
        if self.thread is None:
            return
        self.stop_event.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RunFailure("GPU monitor did not stop")


def read_peak_from_monitor(monitor: GPUMonitor) -> tuple[int | None, int | None, float | None]:
    peak, total = monitor.peak_used_mib, monitor.total_mib
    fraction = peak / total if peak is not None and total else None
    return peak, total, fraction


def gpu_snapshot(path: Path, *, compute: bool = False) -> str:
    if compute:
        command = [
            "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ]
    else:
        command = [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,memory.free,utilization.gpu,temperature.gpu",
            "--format=csv",
        ]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    atomic_write(path, result.stdout)
    atomic_write(path.with_suffix(path.suffix + ".stderr.txt"), result.stderr)
    atomic_write(path.with_suffix(path.suffix + ".exit_code.txt"), f"{result.returncode}\n")
    if result.returncode != 0:
        raise RunFailure(f"nvidia-smi snapshot failed: {path.name}")
    return result.stdout


def logs_contain_oom(logs: Path) -> bool:
    for path in logs.glob("*.txt"):
        if path.is_file() and path.stat().st_size <= 100 * 1024 * 1024:
            try:
                if OOM_RE.search(path.read_text(encoding="utf-8", errors="replace")):
                    return True
            except OSError:
                continue
    return False


def seal_output_manifest(run_root: Path) -> str:
    manifest = run_root / "operator_logs" / "OUTPUT_SHA256SUMS"
    if manifest.exists():
        raise RunFailure("refusing to replace an output manifest")
    records: list[tuple[str, str]] = []
    for path in run_root.rglob("*"):
        relative = path.relative_to(run_root).as_posix()
        if relative in {"operator_logs/OUTPUT_SHA256SUMS"}:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise RunFailure(f"unsafe output member: {relative}")
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        digest = sha256_file(path)
        after_info = path.stat()
        after = (
            after_info.st_dev, after_info.st_ino, after_info.st_size,
            after_info.st_mtime_ns, after_info.st_ctime_ns,
        )
        if before != after:
            raise RunFailure(f"output changed while hashing: {relative}")
        records.append((relative, digest))
    records.sort(key=lambda item: item[0].encode("utf-8"))
    common_required = {
        "STATUS.txt",
        "operator_logs/AI_EVALUATION.json",
        "operator_logs/experience_event.json",
    }
    status = (run_root / "STATUS.txt").read_text(encoding="utf-8").strip()
    success_required = {
        "operator_logs/multistate_contract.json",
        "operator_logs/preflight_contract.json",
        "operator_logs/preflight_target_geometry.npz",
        "reports/fold_metrics.csv",
        "reports/task_summary.csv",
        "reports/state_summary.csv",
        "reports/candidate_state_contrasts.csv",
    }
    failure_required = {"operator_logs/terminal_failure_reason.txt"}
    if status == "AI_EVALUATION_COMPLETE":
        required = common_required | success_required
    elif status == "AI_EVALUATION_FAILED":
        required = common_required | failure_required
    else:
        raise RunFailure(f"refusing to seal non-terminal status: {status!r}")
    observed = {relative for relative, _ in records}
    if not required <= observed:
        raise RunFailure(f"required terminal outputs missing: {sorted(required - observed)}")
    content = "".join(f"{digest}  ./{relative}\n" for relative, digest in records)
    atomic_write(manifest, content)
    verify_manifest(run_root, manifest)
    return sha256_file(manifest)


def append_experience_external(
    *,
    python_bin: Path,
    append_script: Path,
    registry: Path,
    event_path: Path,
) -> None:
    command = [
        str(python_bin), "-I", str(append_script),
        "--registry", str(registry), "--event", str(event_path),
    ]
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RunFailure(
            f"experience append failed ({result.returncode}): {result.stderr.strip()}"
        )


def publish_failure_terminal(
    *,
    run_root: Path,
    panel_id: str,
    started_at: str,
    failure_reason: str,
) -> tuple[Path, str]:
    """Correct any provisional success evidence and seal an explicit failure."""
    logs = run_root / "operator_logs"
    logs.mkdir(exist_ok=True)
    ended_at = utc_now()
    receipt = {
        "schema_version": "WINDOWS_OWNER_AI_EVALUATION_V1",
        "status": "AI_EVALUATION_FAILED",
        "exit_code": 1,
        "authority": "WINDOWS_CODEX",
        "panel_id": panel_id,
        "attempt_id": run_root.name,
        "run_root": str(run_root),
        "failure_reason": failure_reason,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "training_performed": False,
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
    }
    event = {
        "event_id": (
            f"t10-multistate-{panel_id}-"
            f"{run_root.name.removeprefix('attempt_')}-failure"
        ),
        "iteration_id": run_root.name,
        "stage": "T10_MULTI_STATE_AI_EVALUATION",
        "outcome": "FAILURE",
        "summary": failure_reason,
        "run_root": str(run_root),
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
    }
    replace_owned_terminal(
        logs / "AI_EVALUATION.json",
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    replace_owned_terminal(logs / "ended_at_utc.txt", ended_at + "\n")
    replace_owned_terminal(logs / "terminal_failure_reason.txt", failure_reason + "\n")
    replace_owned_terminal(
        logs / "experience_event.json",
        json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    replace_owned_terminal(run_root / "STATUS.txt", "AI_EVALUATION_FAILED\n")
    output_manifest = logs / "OUTPUT_SHA256SUMS"
    if output_manifest.exists():
        # This exact runner-owned manifest may describe a provisional success.
        # Remove it before sealing the corrected failure terminal state.
        output_manifest.unlink()
    return logs / "experience_event.json", seal_output_manifest(run_root)


def locate_acceptance(workspace: Path) -> tuple[Path, dict]:
    root = workspace / "gpu_work" / "owner_mode" / "local_env_acceptance"
    receipts = sorted(root.glob("*/LOCAL_ENV_ACCEPTANCE.json"), key=lambda path: path.parent.name)
    if not receipts:
        raise RunFailure("no local environment acceptance receipt found")
    receipt = receipts[-1]
    verify_manifest(receipt.parent, receipt.parent / "SHA256SUMS")
    payload = json_object(receipt)
    if payload.get("status") != "LOCAL_ENV_READY" or payload.get("exit_code") != 0:
        raise RunFailure("latest local environment acceptance is not ready")
    if payload.get("mac_review_required") is not False:
        raise RunFailure("local environment receipt unexpectedly requires Mac review")
    return receipt, payload


def validate_owner(marker: Path) -> None:
    payload = json_object(marker)
    required = {
        "status": "ACTIVE",
        "authority": "WINDOWS_CODEX",
        "mac_review_required": False,
        "environment_contract_required": False,
        "training_allowed": False,
        "model_weights_mutable": False,
    }
    for field, expected in required.items():
        if payload.get(field) != expected:
            raise RunFailure(f"owner marker mismatch: {field}")


def validate_repo(repo: Path) -> tuple[str, str]:
    if not (repo / ".git").exists() or repo.is_symlink():
        raise RunFailure(f"invalid repository root: {repo}")
    status = command_output(["git", "status", "--short"], cwd=repo)
    if status:
        raise RunFailure("repository must be clean before multi-state evaluation")
    commit = command_output(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    tree = command_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).strip()
    return commit, tree


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--panel-id", required=True)
    parser.add_argument("--anchor-set", required=True, type=Path)
    parser.add_argument("--candidate-ids", nargs="+", default=list(DEFAULT_CANDIDATES))
    parser.add_argument("--state-ids", nargs="+", default=list(DEFAULT_STATES))
    parser.add_argument("--baseline-state", default="DEV_00")
    return parser.parse_args()


def validated_ids(values: Iterable[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result or len(set(result)) != len(result):
        raise RunFailure(f"{label} must be a non-empty unique list")
    if any(REQUEST_ID_RE.fullmatch(value) is None for value in result):
        raise RunFailure(f"{label} contains an unsafe ID")
    return result


def main() -> int:  # noqa: PLR0915
    args = parse_args()
    if PANEL_ID_RE.fullmatch(args.panel_id) is None:
        raise SystemExit(f"unsafe panel ID: {args.panel_id!r}")
    try:
        candidate_ids = validated_ids(args.candidate_ids, "candidate IDs")
        state_ids = validated_ids(args.state_ids, "state IDs")
    except RunFailure as exc:
        raise SystemExit(str(exc)) from exc
    if args.baseline_state not in state_ids:
        raise SystemExit("baseline state must be included in state IDs")
    workspace = args.workspace_root.resolve(strict=True)
    if not str(workspace).startswith("/home/"):
        raise SystemExit(f"workspace root must be under /home: {workspace}")
    repo = workspace / "GLP_"
    marker = workspace / "WINDOWS_OWNER_MODE.json"
    run_root: Path | None = None
    logs: Path | None = None
    monitor: GPUMonitor | None = None
    lock_descriptor: int | None = None
    experience_python: Path | None = None
    experience_script = (
        repo
        / "boltzgen"
        / "main"
        / "windows_single_owner_20260831"
        / "scripts"
        / "append_owner_experience.py"
    )
    experience_registry = (
        workspace / "gpu_work" / "experience" / "windows_engineering_events.jsonl"
    )
    started_at = utc_now()
    started_monotonic = time.monotonic()
    failure_reason: str | None = None
    exit_code = 1
    try:
        validate_owner(marker)
        commit, tree = validate_repo(repo)
        acceptance_path, acceptance = locate_acceptance(workspace)
        python_bin = Path(str(acceptance["python_bin"]))
        experience_python = python_bin
        launcher = python_bin.parent / "boltzgen-wsl-sm120"
        # A venv's ``bin/python`` is normally a symlink; its exact path is bound
        # by the accepted receipt.  The project launcher itself must remain a
        # regular, non-symlink executable.
        if not python_bin.is_file() or launcher.is_symlink() or not os.access(python_bin, os.X_OK) or not os.access(launcher, os.X_OK):
            raise RunFailure("accepted Python/BoltzGen launcher is missing or unsafe")
        runtime_root = workspace / "boltzgen" / "data" / "boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819" / "runtime_cache"
        if runtime_root.is_symlink() or not runtime_root.is_dir():
            raise RunFailure("runtime root is missing or unsafe")
        runtime_expected = expected_runtime_hashes(runtime_root)
        runtime_before = verify_runtime(runtime_root, runtime_expected)
        anchor_set = args.anchor_set.resolve(strict=True)
        if anchor_set.is_symlink() or not anchor_set.is_dir():
            raise RunFailure("anchor set is missing or unsafe")
        verify_manifest(anchor_set, anchor_set / "SHA256SUMS")
        anchor_payload = json_object(anchor_set / "ANCHOR_SET.json")
        if anchor_payload.get("status") != "LOCAL_ANCHOR_SET_READY":
            raise RunFailure("anchor set is not ready")
        if anchor_payload.get("formal_gate_claimed") is not False:
            raise RunFailure("anchor set unexpectedly claims a formal gate")
        if shutil.disk_usage(workspace).free < MIN_FREE_BYTES:
            raise RunFailure("less than 1 GiB free space remains on the workspace disk")
        lock_path = Path(f"/run/user/{os.getuid()}")
        lock_descriptor = os.open(lock_path, os.O_RDONLY)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunFailure("the shared single-GPU lock is already held") from exc
        compute_before = command_output(
            [
                "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ]
        )
        if compute_before.strip():
            raise RunFailure("another GPU compute process is active")

        panel_root = workspace / "gpu_work" / "owner_mode" / "t10_multistate_ai_evaluation" / args.panel_id
        panel_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        attempt_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_root = panel_root / f"attempt_{attempt_stamp}"
        if run_root.exists() or run_root.is_symlink():
            raise RunFailure(f"attempt path already exists: {run_root}")
        builder = repo / "boltzgen" / "main" / "windows_single_owner_20260831" / "scripts" / "build_owner_multistate_inputs.py"
        preflight = builder.with_name("preflight_owner_multistate.py")
        summarizer = builder.with_name("summarize_owner_multistate.py")
        append_script = builder.with_name("append_owner_experience.py")
        for script in (builder, preflight, summarizer, append_script):
            if script.is_symlink() or not script.is_file():
                raise RunFailure(f"required owner script missing or unsafe: {script}")
        materialize_stdout = panel_root / f".{run_root.name}.materialize.stdout.tmp"
        materialize_stderr = panel_root / f".{run_root.name}.materialize.stderr.tmp"
        materialize_command = [
            str(python_bin), "-I", str(builder),
            "--repo-root", str(repo),
            "--anchor-set", str(anchor_set),
            "--output", str(run_root),
            "--runtime-root", str(runtime_root),
            "--candidate-ids", *candidate_ids,
            "--state-ids", *state_ids,
        ]
        materialize_started = time.monotonic()
        with materialize_stdout.open("wb") as stdout, materialize_stderr.open("wb") as stderr:
            materialize_result = subprocess.run(
                materialize_command, cwd=repo, stdout=stdout, stderr=stderr, check=False
            )
        materialize_duration = time.monotonic() - materialize_started
        if materialize_result.returncode != 0:
            if not run_root.exists():
                run_root.mkdir(mode=0o700)
            logs = run_root / "operator_logs"
            logs.mkdir(exist_ok=True)
            shutil.move(materialize_stdout, logs / "materialize.stdout.txt")
            shutil.move(materialize_stderr, logs / "materialize.stderr.txt")
            atomic_write(logs / "materialize.exit_code.txt", f"{materialize_result.returncode}\n")
            atomic_write(logs / "materialize.duration_seconds.txt", f"{materialize_duration:.6f}\n")
            raise RunFailure(f"input materializer failed with exit code {materialize_result.returncode}")
        if not run_root.is_dir() or run_root.is_symlink():
            raise RunFailure("input materializer did not publish a safe run root")
        logs = run_root / "operator_logs"
        logs.mkdir(exist_ok=False)
        shutil.move(materialize_stdout, logs / "materialize.stdout.txt")
        shutil.move(materialize_stderr, logs / "materialize.stderr.txt")
        atomic_write(logs / "materialize.exit_code.txt", "0\n")
        atomic_write(logs / "materialize.duration_seconds.txt", f"{materialize_duration:.6f}\n")
        atomic_write(logs / "started_at_utc.txt", started_at + "\n")
        atomic_write(logs / "command.txt", shlex.join([sys.executable, *sys.argv]) + "\n")
        atomic_write(logs / "source_commit.txt", commit + "\n")
        atomic_write(logs / "source_tree.txt", tree + "\n")
        shutil.copy2(acceptance_path, logs / "LOCAL_ENV_ACCEPTANCE.json")
        atomic_write(
            logs / "runtime_assets_used.SHA256SUMS",
            "".join(f"{digest}  {name}\n" for name, digest in runtime_expected.items()),
        )
        atomic_write(logs / "input_bindings.json", json.dumps(
            {
                "owner_marker": {"path": str(marker), "sha256": sha256_file(marker)},
                "anchor_set": {"path": str(anchor_set), "manifest_sha256": sha256_file(anchor_set / "SHA256SUMS")},
                "local_env_acceptance": {"path": str(acceptance_path), "sha256": sha256_file(acceptance_path)},
                "builder": {"path": str(builder), "sha256": sha256_file(builder)},
                "preflight": {"path": str(preflight), "sha256": sha256_file(preflight)},
                "summarizer": {"path": str(summarizer), "sha256": sha256_file(summarizer)},
            },
            indent=2, sort_keys=True,
        ) + "\n")
        gpu_snapshot(logs / "gpu_before.csv")
        atomic_write(logs / "gpu_compute_processes_before.csv", compute_before)
        atomic_write(logs / "disk_before.txt", command_output(["df", "-B1", str(workspace)]))
        input_hashes_before = verify_manifest(run_root, run_root / "INPUT_SHA256SUMS")

        preflight_duration = run_logged(
            "preflight",
            [
                str(python_bin), "-I", str(preflight), "--run-root", str(run_root),
                "--runtime-root", str(runtime_root),
                "--coordinate-contract", str(logs / "preflight_target_geometry.npz"),
            ],
            logs,
            cwd=repo,
        )
        preflight_contract = json_object(logs / "preflight.stdout.txt")
        if preflight_contract.get("status") != "PASS":
            raise RunFailure("CPU preflight did not pass")
        atomic_write(
            logs / "preflight_contract.json",
            json.dumps(preflight_contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

        monitor = GPUMonitor(logs / "gpu_monitor.csv", logs / "gpu_monitor.stderr.txt")
        monitor.start()
        folding_duration = run_logged(
            "folding",
            [str(launcher), "execute", str(run_root), "--no_subprocess", "--steps", "folding"],
            logs,
            cwd=repo,
        )
        monitor.stop()
        if monitor.failed is not None or monitor.row_count < 2:
            raise RunFailure(f"GPU monitor contract failed: {monitor.failed or 'too few samples'}")
        if logs_contain_oom(logs):
            raise RunFailure("CUDA OOM signature detected")

        summary_duration = run_logged(
            "summary",
            [
                str(python_bin), "-I", str(summarizer), "--run-root", str(run_root),
                "--baseline-state", args.baseline_state,
            ],
            logs,
            cwd=repo,
        )
        output_contract = json_object(logs / "multistate_contract.json")
        if output_contract.get("status") != "PASS":
            raise RunFailure("multi-state output validator did not pass")
        expected_tasks = len(candidate_ids) * len(state_ids)
        if (
            output_contract.get("logical_task_count") != expected_tasks
            or output_contract.get("samples_per_task") != SAMPLES_PER_TASK
            or output_contract.get("sample_row_count") != expected_tasks * SAMPLES_PER_TASK
        ):
            raise RunFailure("multi-state output denominator mismatch")

        runtime_after = verify_runtime(runtime_root, runtime_expected)
        input_hashes_after = verify_manifest(run_root, run_root / "INPUT_SHA256SUMS")
        if input_hashes_before != input_hashes_after or runtime_before != runtime_after:
            raise RunFailure("terminal input identity changed")
        terminal_commit, terminal_tree = validate_repo(repo)
        if (terminal_commit, terminal_tree) != (commit, tree):
            raise RunFailure("repository identity changed during evaluation")
        gpu_after = gpu_snapshot(logs / "gpu_after.csv")
        compute_after = gpu_snapshot(logs / "gpu_compute_processes_after.csv", compute=True)
        if compute_after.strip():
            raise RunFailure("GPU compute process remained after folding")
        atomic_write(logs / "disk_after.txt", command_output(["df", "-B1", str(workspace)]))
        atomic_write(logs / "cuda_oom_detected.txt", "false\n")
        peak, total, fraction = read_peak_from_monitor(monitor)
        ended_at = utc_now()
        duration_total = time.monotonic() - started_monotonic
        receipt = {
            "schema_version": "WINDOWS_OWNER_AI_EVALUATION_V1",
            "status": "AI_EVALUATION_COMPLETE",
            "exit_code": 0,
            "authority": "WINDOWS_CODEX",
            "mac_review_required": False,
            "environment_contract_required": False,
            "formal_gate_claimed": False,
            "training_performed": False,
            "panel_id": args.panel_id,
            "attempt_id": run_root.name,
            "run_root": str(run_root),
            "candidate_ids": list(candidate_ids),
            "state_ids": list(state_ids),
            "baseline_state_id": args.baseline_state,
            "logical_task_count": expected_tasks,
            "samples_per_task": SAMPLES_PER_TASK,
            "sample_row_count": expected_tasks * SAMPLES_PER_TASK,
            "aggregation_policy": output_contract["aggregation_policy"],
            "best_fold_only": False,
            "source_commit": commit,
            "source_tree": tree,
            "anchor_set_path": str(anchor_set),
            "anchor_set_manifest_sha256": sha256_file(anchor_set / "SHA256SUMS"),
            "local_env_acceptance_path": str(acceptance_path),
            "local_env_acceptance_sha256": sha256_file(acceptance_path),
            "runtime_assets_sha256": runtime_expected,
            "input_manifest_sha256": sha256_file(run_root / "INPUT_SHA256SUMS"),
            "preflight_contract_sha256": sha256_file(logs / "preflight_contract.json"),
            "preflight_target_geometry_sha256": sha256_file(
                logs / "preflight_target_geometry.npz"
            ),
            "output_contract_sha256": sha256_file(logs / "multistate_contract.json"),
            "gpu_peak_memory_used_mib": peak,
            "gpu_total_memory_mib": total,
            "gpu_peak_memory_fraction": fraction,
            "cuda_oom_detected": False,
            "durations_seconds": {
                "materialize": materialize_duration,
                "preflight": preflight_duration,
                "folding": folding_duration,
                "summary": summary_duration,
                "total": duration_total,
            },
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "experience_event_id": (
                f"t10-multistate-{args.panel_id}-"
                f"{run_root.name.removeprefix('attempt_')}"
            ),
            "experience_registry": str(experience_registry),
            "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
        }
        atomic_write(
            logs / "AI_EVALUATION.json",
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        atomic_write(logs / "ended_at_utc.txt", ended_at + "\n")
        atomic_write(run_root / "STATUS.txt", "AI_EVALUATION_COMPLETE\n", replace_input_status=True)
        event = {
            "event_id": (
                f"t10-multistate-{args.panel_id}-"
                f"{run_root.name.removeprefix('attempt_')}"
            ),
            "iteration_id": run_root.name,
            "stage": "T10_MULTI_STATE_AI_EVALUATION",
            "outcome": "SUCCESS",
            "summary": f"{expected_tasks} logical tasks and {expected_tasks * SAMPLES_PER_TASK} fold rows validated; no OOM",
            "run_root": str(run_root),
            "candidate_ids": list(candidate_ids),
            "state_ids": list(state_ids),
            "output_contract_sha256": sha256_file(logs / "multistate_contract.json"),
            "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
        }
        event_path = logs / "experience_event.json"
        atomic_write(event_path, json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        # Seal the run first.  The registry append writes only outside run_root;
        # if it fails, the exception handler replaces all provisional COMPLETE
        # evidence with FAILED evidence and reseals the run.
        manifest_sha = seal_output_manifest(run_root)
        append_experience_external(
            python_bin=python_bin,
            append_script=append_script,
            registry=experience_registry,
            event_path=event_path,
        )
        exit_code = 0
        try:
            print(
                f"AI_EVALUATION_COMPLETE path={run_root} output_manifest_sha256={manifest_sha}",
                flush=True,
            )
        except OSError:
            # Terminal output is not part of the committed result transaction.
            pass
    except (RunFailure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        failure_reason = str(exc)
        if monitor is not None:
            try:
                monitor.stop()
            except Exception as monitor_exc:  # keep both failures
                failure_reason += f"; monitor_stop={monitor_exc}"
        if run_root is not None and run_root.exists():
            try:
                failure_event, _ = publish_failure_terminal(
                    run_root=run_root,
                    panel_id=args.panel_id,
                    started_at=started_at,
                    failure_reason=failure_reason,
                )
                if experience_python is not None and experience_script.is_file():
                    try:
                        append_experience_external(
                            python_bin=experience_python,
                            append_script=experience_script,
                            registry=experience_registry,
                            event_path=failure_event,
                        )
                    except Exception as append_failure_exc:
                        failure_reason += f"; failure_experience_append={append_failure_exc}"
            except Exception as finalize_exc:
                failure_reason += f"; failure_finalize={finalize_exc}"
        print(f"AI_EVALUATION_FAILED: {failure_reason}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
