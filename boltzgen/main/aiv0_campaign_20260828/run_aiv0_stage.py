#!/usr/bin/env python3
"""Run the AIV0 validator with an immutable, external audit trail.

Code source: project_original.

The runner never edits the validator or source inputs. It requires explicit
versioned contract and external output roots, creates exactly one new attempt
directory, fingerprints the runtime, revalidates inputs after execution, proves
that check mode did not mutate derived outputs, and publishes ``receipt.json``
last via an atomic, no-replace link.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


STAGE_ID = "aiv0_asset_validation"
ATTEMPT_PATTERN = re.compile(r"attempt_[0-9]{3}")
ENVIRONMENT_ALLOWLIST = (
    "LANG",
    "LC_ALL",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED",
    "PYTHONNOUSERSITE",
    "TZ",
)
CONTRACT_FILENAMES = (
    "asset_mounts.tsv",
    "cohort_registry.tsv",
    "compatibility_aliases.tsv",
    "file_overrides.tsv",
    "historical_output_hashes.tsv",
)
DEFAULT_RUNTIME_MODULES = ("gemmi", "yaml")


class StageConfigurationError(ValueError):
    """Raised before an attempt directory is created for an invalid request."""


class AttemptAlreadyExistsError(FileExistsError):
    """Raised when an immutable attempt directory already exists."""


@dataclass(frozen=True)
class StageResult:
    """Outcome and evidence location for one completed runner attempt."""

    attempt_dir: Path
    exit_code: int
    status: str


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    """Hash one regular file without loading it wholly into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(payload: object) -> str:
    """Render stable UTF-8 JSON for hashing and receipt replay."""

    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def write_text_new(path: Path, text: str) -> None:
    """Create a UTF-8 text file and refuse to replace an existing path."""

    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def write_json_new(path: Path, payload: object) -> None:
    """Create a stable JSON file without overwriting an existing file."""

    write_text_new(path, canonical_json(payload))


def atomic_publish_receipt(path: Path, payload: object) -> None:
    """Publish the final receipt atomically and never replace an old receipt."""

    if path.exists():
        raise FileExistsError(f"receipt already exists: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write_text_new(temporary, canonical_json(payload))
        # Linking a complete, fsynced file is an atomic no-replace publication.
        # Unlike os.replace(), it cannot silently overwrite a competing receipt.
        os.link(temporary, path)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def require_existing_directory(path: Path, label: str) -> Path:
    """Resolve and validate an existing directory."""

    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise StageConfigurationError(f"{label} does not exist: {path}") from error
    if not resolved.is_dir():
        raise StageConfigurationError(f"{label} is not a directory: {resolved}")
    return resolved


def require_existing_file(path: Path, label: str) -> Path:
    """Resolve and validate an existing regular file."""

    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise StageConfigurationError(f"{label} does not exist: {path}") from error
    if not resolved.is_file():
        raise StageConfigurationError(f"{label} is not a file: {resolved}")
    if any(character in str(resolved) for character in ("\n", "\r", "\0")):
        raise StageConfigurationError(f"{label} contains a control character")
    return resolved


def is_within(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is equal to or nested below ``parent``."""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def create_stage_root(run_root: Path, repo_root: Path) -> Path:
    """Create fixed stage parents without following a symlinked log path."""

    current = run_root
    for name in ("logs", "stages", STAGE_ID):
        current = current / name
        if current.is_symlink():
            raise StageConfigurationError(
                f"stage path component must not be a symlink: {current}"
            )
        if current.exists():
            if not current.is_dir():
                raise StageConfigurationError(
                    f"stage path component is not a directory: {current}"
                )
        else:
            current.mkdir(mode=0o750)
    resolved = current.resolve(strict=True)
    if not is_within(resolved, run_root) or is_within(resolved, repo_root):
        raise StageConfigurationError(f"stage root escaped external run root: {resolved}")
    return resolved


def render_sha256sums(paths: Iterable[Path], *, relative_to: Path | None = None) -> str:
    """Render a deterministic SHA-256 manifest for unique regular files."""

    resolved_paths = {path.resolve(strict=True) for path in paths}
    rows: list[str] = []
    for path in sorted(resolved_paths, key=lambda item: os.fsencode(str(item))):
        if not path.is_file():
            raise StageConfigurationError(f"manifest member is not a file: {path}")
        display = path.relative_to(relative_to) if relative_to is not None else path
        rows.append(f"{sha256_file(path)}  {display.as_posix()}")
    return "" if not rows else "\n".join(rows) + "\n"


def canonical_evidence_uri(path: Path, repo_root: Path, workspace_root: Path) -> str:
    """Represent an evidence path without persisting a machine-specific prefix."""

    resolved = path.resolve(strict=True)
    if is_within(resolved, repo_root):
        return f"repo://{resolved.relative_to(repo_root).as_posix()}"
    if is_within(resolved, workspace_root):
        return f"workspace://{resolved.relative_to(workspace_root).as_posix()}"
    raise StageConfigurationError(
        f"input evidence must be inside repo_root or project_root: {resolved}"
    )


def render_input_sha256sums(
    paths: Iterable[Path], *, repo_root: Path, workspace_root: Path
) -> str:
    """Render canonical URI hashes for inputs, deduplicated by resolved file."""

    resolved_paths = {path.resolve(strict=True) for path in paths}
    rows = [
        f"{sha256_file(path)}  {canonical_evidence_uri(path, repo_root, workspace_root)}"
        for path in sorted(
            resolved_paths,
            key=lambda item: canonical_evidence_uri(item, repo_root, workspace_root),
        )
    ]
    return "" if not rows else "\n".join(rows) + "\n"


def snapshot_output_tree(output_root: Path) -> str:
    """Hash an external derived tree and reject symlinks or path escapes."""

    members: list[Path] = []
    for path in output_root.rglob("*"):
        if path.is_symlink():
            raise StageConfigurationError(f"derived output must not be a symlink: {path}")
        if path.is_file():
            resolved = path.resolve(strict=True)
            if not is_within(resolved, output_root):
                raise StageConfigurationError(
                    f"derived output escaped output_root: {resolved}"
                )
            members.append(resolved)
    return render_sha256sums(members, relative_to=output_root)


def collect_runtime_fingerprint(
    validator_python: Path,
    runtime_modules: Sequence[str],
    environment: Mapping[str, str],
) -> dict[str, object]:
    """Fingerprint Python plus requested imported package trees."""

    probe = r'''
import hashlib
import importlib
import json
import pathlib
import platform
import sys

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def package_tree(root):
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return {"file_count": len(files), "sha256": digest.hexdigest()}

modules = []
for name in json.loads(sys.argv[1]):
    module = importlib.import_module(name)
    module_file = pathlib.Path(module.__file__).resolve(strict=True)
    root = module_file.parent if hasattr(module, "__path__") else module_file
    item = {
        "module": name,
        "module_file": str(module_file),
        "module_file_sha256": sha256(module_file),
        "version": str(getattr(module, "__version__", "unknown")),
    }
    if root.is_dir():
        item["package_tree"] = package_tree(root)
    modules.append(item)

print(json.dumps({
    "implementation": platform.python_implementation(),
    "modules": modules,
    "python_executable": sys.executable,
    "python_version": sys.version,
}, ensure_ascii=True, sort_keys=True))
'''
    completed = subprocess.run(
        [
            str(validator_python),
            "-B",
            "-I",
            "-c",
            probe,
            json.dumps(list(runtime_modules)),
        ],
        capture_output=True,
        check=False,
        env=dict(environment),
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise StageConfigurationError(
            f"runtime fingerprint probe failed ({completed.returncode}): {detail}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise StageConfigurationError("runtime fingerprint returned invalid JSON") from error
    payload["schema_version"] = "AIV0_RUNTIME_FINGERPRINT_V1"
    payload["python_executable_sha256"] = sha256_file(validator_python)
    return payload


def paths_overlap(first: Path, second: Path) -> bool:
    """Return whether either resolved directory contains the other."""

    return is_within(first, second) or is_within(second, first)


def run_stage(
    *,
    repo_root: Path,
    run_root: Path,
    attempt_id: str,
    validator_python: Path,
    validator: Path,
    project_root: Path,
    mode: str = "check",
    contract_root: Path | None = None,
    output_root: Path | None = None,
    runtime_modules: Sequence[str] = (),
    enforce_canonical_layout: bool = False,
    input_paths: Sequence[Path] = (),
    environment: Mapping[str, str] | None = None,
) -> StageResult:
    """Execute one AIV0 attempt and publish a closed evidence bundle.

    Args:
        repo_root: Existing Git checkout root.  The run root must resolve outside it.
        run_root: Existing external campaign root where evidence will be written.
        attempt_id: Immutable identifier matching ``attempt_NNN``.
        validator_python: Explicit Python interpreter used for the validator.
        validator: Existing validator script; it is executed with ``--check``.
        project_root: Workspace root passed to the validator.
        mode: Validator mode, ``check`` or the explicitly logged ``write``.
        contract_root: Required static contract directory passed to the validator.
        output_root: Required external derived-artifact directory; its files are
            hashed before and after the validator exits.
        runtime_modules: Import names whose package trees enter the runtime
            fingerprint. The CLI defaults to Gemmi and PyYAML.
        enforce_canonical_layout: Require the repository and workspace AIV0
            dated roots used by the formal CLI.
        input_paths: Additional files whose bytes must be bound by the receipt.
        environment: Optional complete child environment, primarily for tests.

    Returns:
        A :class:`StageResult` after a PASS or FAIL receipt has been published.

    Raises:
        StageConfigurationError: If paths or identifiers are invalid.
        AttemptAlreadyExistsError: If the attempt directory already exists.
    """

    if ATTEMPT_PATTERN.fullmatch(attempt_id) is None:
        raise StageConfigurationError(
            f"attempt_id must match {ATTEMPT_PATTERN.pattern!r}: {attempt_id!r}"
        )
    if mode not in {"check", "write"}:
        raise StageConfigurationError(f"mode must be check or write: {mode!r}")

    resolved_repo_root = require_existing_directory(repo_root, "repo_root")
    resolved_run_root = require_existing_directory(run_root, "run_root")
    resolved_project_root = require_existing_directory(project_root, "project_root")
    if contract_root is None:
        raise StageConfigurationError("contract_root is required for every AIV0 mode")
    if output_root is None:
        raise StageConfigurationError("output_root is required for every AIV0 mode")
    resolved_contract_root = require_existing_directory(contract_root, "contract_root")
    resolved_output_root = require_existing_directory(output_root, "output_root")
    if is_within(resolved_run_root, resolved_repo_root):
        raise StageConfigurationError(
            f"run_root must be outside repo_root: {resolved_run_root}"
        )
    if not is_within(resolved_contract_root, resolved_repo_root):
        raise StageConfigurationError(
            f"contract_root must be inside repo_root: {resolved_contract_root}"
        )
    if not is_within(resolved_output_root, resolved_project_root):
        raise StageConfigurationError(
            f"output_root must be inside project_root: {resolved_output_root}"
        )
    for label, protected in (
        ("repo_root", resolved_repo_root),
        ("run_root", resolved_run_root),
        ("contract_root", resolved_contract_root),
    ):
        if paths_overlap(resolved_output_root, protected):
            raise StageConfigurationError(
                f"output_root must not overlap {label}: {resolved_output_root}"
            )
    if enforce_canonical_layout:
        expected_contract_parent = (
            resolved_repo_root / "boltzgen/resources/data"
        ).resolve(strict=True)
        expected_output_parent = (
            resolved_project_root / "boltzgen/data"
        ).resolve(strict=True)
        contract_match = re.fullmatch(
            r"AI结构资产验证登记册_(20[0-9]{6})", resolved_contract_root.name
        )
        output_match = re.fullmatch(
            r"ai_structure_asset_validation_registry_(20[0-9]{6})(?:_[0-9]{6})?",
            resolved_output_root.name,
        )
        if (
            resolved_contract_root.parent != expected_contract_parent
            or contract_match is None
        ):
            raise StageConfigurationError(
                f"non-canonical AIV0 contract_root: {resolved_contract_root}"
            )
        if resolved_output_root.parent != expected_output_parent or output_match is None:
            raise StageConfigurationError(
                f"non-canonical AIV0 output_root: {resolved_output_root}"
            )
        if contract_match.group(1) != output_match.group(1):
            raise StageConfigurationError("contract_root and output_root dates differ")
        if contract_match.group(1) == "20260826":
            raise StageConfigurationError("historical 20260826 registry is immutable")

    resolved_python = require_existing_file(validator_python, "validator_python")
    if not os.access(resolved_python, os.X_OK):
        raise StageConfigurationError(
            f"validator_python is not executable: {resolved_python}"
        )
    resolved_validator = require_existing_file(validator, "validator")
    source_environment = dict(os.environ if environment is None else environment)
    child_environment = {
        key: source_environment[key]
        for key in ENVIRONMENT_ALLOWLIST
        if key in source_environment
    }
    child_environment["PYTHONNOUSERSITE"] = "1"
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    resolved_inputs = [
        require_existing_file(Path(path), "input") for path in input_paths
    ]
    contract_inputs = [
        require_existing_file(resolved_contract_root / name, "contract input")
        for name in CONTRACT_FILENAMES
    ]
    resolved_inputs.extend(
        (
            Path(__file__).resolve(strict=True),
            resolved_validator,
            resolved_python,
            *contract_inputs,
        )
    )
    runtime_fingerprint = collect_runtime_fingerprint(
        resolved_python, runtime_modules, child_environment
    )
    inputs_before = render_input_sha256sums(
        resolved_inputs,
        repo_root=resolved_repo_root,
        workspace_root=resolved_project_root,
    )
    derived_before = snapshot_output_tree(resolved_output_root)

    stage_root = create_stage_root(resolved_run_root, resolved_repo_root)
    attempt_dir = stage_root / attempt_id
    try:
        attempt_dir.mkdir(mode=0o750)
    except FileExistsError as error:
        raise AttemptAlreadyExistsError(
            f"immutable attempt already exists: {attempt_dir}"
        ) from error

    command = [
        str(resolved_python),
        "-B",
        "-I",
        str(resolved_validator),
        f"--{mode}",
        "--workspace-root",
        str(resolved_project_root),
    ]
    command.extend(("--contract-root", str(resolved_contract_root)))
    command.extend(("--output-root", str(resolved_output_root)))
    recorded_environment = {
        key: child_environment[key]
        for key in ENVIRONMENT_ALLOWLIST
        if key in child_environment
    }

    started_at = utc_now()
    write_json_new(
        attempt_dir / "command.json",
        {
            "argv": command,
            "attempt_id": attempt_id,
            "cwd": str(resolved_project_root),
            "schema_version": "AIV0_COMMAND_V1",
            "stage_id": STAGE_ID,
        },
    )
    write_json_new(
        attempt_dir / "environment_allowlist.json",
        {
            "schema_version": "AIV0_ENVIRONMENT_ALLOWLIST_V1",
            "variables": recorded_environment,
        },
    )
    write_json_new(attempt_dir / "runtime_fingerprint.json", runtime_fingerprint)
    write_text_new(attempt_dir / "started_at_utc.txt", started_at + "\n")
    write_text_new(attempt_dir / "inputs.SHA256SUMS", inputs_before)
    write_text_new(
        attempt_dir / "derived_outputs_before.SHA256SUMS",
        derived_before,
    )

    launch_error: str | None = None
    with (attempt_dir / "stdout.log").open("xb") as stdout_handle, (
        attempt_dir / "stderr.log"
    ).open("xb") as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                cwd=resolved_project_root,
                env=child_environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
            )
            exit_code = int(completed.returncode)
        except OSError as error:
            exit_code = 127
            launch_error = f"{type(error).__name__}: {error}"
            stderr_handle.write((launch_error + "\n").encode("utf-8"))
        stdout_handle.flush()
        stderr_handle.flush()
        os.fsync(stdout_handle.fileno())
        os.fsync(stderr_handle.fileno())

    evidence_errors: list[str] = []
    try:
        runtime_fingerprint_after = collect_runtime_fingerprint(
            resolved_python, runtime_modules, child_environment
        )
    except (OSError, RuntimeError, StageConfigurationError, ValueError) as error:
        runtime_fingerprint_after = {
            "schema_version": "AIV0_RUNTIME_FINGERPRINT_V1",
            "error": str(error),
        }
        evidence_errors.append(f"runtime revalidation failed: {error}")
    write_json_new(
        attempt_dir / "runtime_fingerprint_after.json",
        runtime_fingerprint_after,
    )
    if runtime_fingerprint_after != runtime_fingerprint:
        evidence_errors.append("runtime bytes changed during validator execution")

    try:
        inputs_after = render_input_sha256sums(
            resolved_inputs,
            repo_root=resolved_repo_root,
            workspace_root=resolved_project_root,
        )
    except (OSError, RuntimeError, StageConfigurationError, ValueError) as error:
        inputs_after = ""
        evidence_errors.append(f"input revalidation failed: {error}")
    write_text_new(attempt_dir / "inputs_after.SHA256SUMS", inputs_after)
    if inputs_after != inputs_before:
        evidence_errors.append("input bytes changed during validator execution")

    try:
        derived_after = snapshot_output_tree(resolved_output_root)
    except (OSError, RuntimeError, StageConfigurationError, ValueError) as error:
        derived_after = ""
        evidence_errors.append(f"derived output snapshot failed: {error}")
    derived_manifest_path = attempt_dir / "derived_outputs.SHA256SUMS"
    write_text_new(derived_manifest_path, derived_after)
    if mode == "check" and derived_after != derived_before:
        evidence_errors.append("check mode changed the derived output tree")

    if evidence_errors:
        exit_code = 70
        with (attempt_dir / "stderr.log").open("ab") as stderr_handle:
            stderr_handle.write(
                ("RUNNER_EVIDENCE_ERROR: " + "; ".join(evidence_errors) + "\n").encode(
                    "utf-8"
                )
            )
            stderr_handle.flush()
            os.fsync(stderr_handle.fileno())

    ended_at = utc_now()
    status = "PASS" if exit_code == 0 else "FAIL"
    failure_kind = (
        "RUNNER_EVIDENCE_ERROR"
        if evidence_errors
        else "RUNNER_LAUNCH_ERROR"
        if launch_error is not None
        else "VALIDATOR_NONZERO"
        if exit_code != 0
        else None
    )
    write_text_new(attempt_dir / "ended_at_utc.txt", ended_at + "\n")
    write_text_new(attempt_dir / "exit_code.txt", f"{exit_code}\n")
    status_payload = {
        "attempt_id": attempt_id,
        "derived_outputs_unchanged": derived_after == derived_before,
        "ended_at_utc": ended_at,
        "exit_code": exit_code,
        "failure_kind": failure_kind,
        "inputs_reverified_after_execution": inputs_after == inputs_before,
        "runtime_reverified_after_execution": (
            runtime_fingerprint_after == runtime_fingerprint
        ),
        "schema_version": "AIV0_STAGE_STATUS_V1",
        "stage_id": STAGE_ID,
        "started_at_utc": started_at,
        "status": status,
        "validator_mode": mode,
    }
    write_json_new(attempt_dir / "status.json", status_payload)

    output_members = [
        path
        for path in attempt_dir.iterdir()
        if path.is_file()
        and path.name not in {"outputs.SHA256SUMS", "receipt.json"}
    ]
    write_text_new(
        attempt_dir / "outputs.SHA256SUMS",
        render_sha256sums(output_members, relative_to=attempt_dir),
    )

    receipt_payload = {
        "attempt_id": attempt_id,
        "command_sha256": sha256_file(attempt_dir / "command.json"),
        "ended_at_utc": ended_at,
        "environment_allowlist_sha256": sha256_file(
            attempt_dir / "environment_allowlist.json"
        ),
        "exit_code": exit_code,
        "inputs_manifest_sha256": sha256_file(
            attempt_dir / "inputs.SHA256SUMS"
        ),
        "inputs_after_manifest_sha256": sha256_file(
            attempt_dir / "inputs_after.SHA256SUMS"
        ),
        "outputs_manifest_sha256": sha256_file(
            attempt_dir / "outputs.SHA256SUMS"
        ),
        "runtime_fingerprint_sha256": sha256_file(
            attempt_dir / "runtime_fingerprint.json"
        ),
        "runtime_fingerprint_after_sha256": sha256_file(
            attempt_dir / "runtime_fingerprint_after.json"
        ),
        "schema_version": "AIV0_STAGE_RECEIPT_V1",
        "stage_id": STAGE_ID,
        "started_at_utc": started_at,
        "status": status,
        "validator_mode": mode,
    }
    receipt_payload["derived_outputs_before_manifest_sha256"] = sha256_file(
        attempt_dir / "derived_outputs_before.SHA256SUMS"
    )
    receipt_payload["derived_outputs_manifest_sha256"] = sha256_file(
        derived_manifest_path
    )
    atomic_publish_receipt(attempt_dir / "receipt.json", receipt_payload)
    return StageResult(attempt_dir=attempt_dir, exit_code=exit_code, status=status)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the AIV0 runner."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--validator-python", required=True, type=Path)
    parser.add_argument("--validator", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--mode", choices=("check", "write"), default="check")
    parser.add_argument("--contract-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--migration-receipt", required=True, type=Path)
    parser.add_argument("--asset-root-manifest", required=True, type=Path)
    parser.add_argument("--asset-root-summary", required=True, type=Path)
    parser.add_argument(
        "--runtime-module",
        action="append",
        dest="runtime_modules",
        help=(
            "Imported package tree to fingerprint; defaults to gemmi and yaml. "
            "Repeat for multiple modules."
        ),
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        dest="input_paths",
        type=Path,
        help="Additional input file to bind; repeat for multiple files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; return the validator exit code when execution completes."""

    args = build_argument_parser().parse_args(argv)
    try:
        result = run_stage(
            repo_root=args.repo_root,
            run_root=args.run_root,
            attempt_id=args.attempt_id,
            validator_python=args.validator_python,
            validator=args.validator,
            project_root=args.project_root,
            mode=args.mode,
            contract_root=args.contract_root,
            output_root=args.output_root,
            runtime_modules=tuple(args.runtime_modules or DEFAULT_RUNTIME_MODULES),
            enforce_canonical_layout=True,
            input_paths=(
                args.migration_receipt,
                args.asset_root_manifest,
                args.asset_root_summary,
                *args.input_paths,
            ),
        )
    except AttemptAlreadyExistsError as error:
        print(f"BLOCKED_ATTEMPT_ALREADY_EXISTS: {error}", file=sys.stderr)
        return 73
    except StageConfigurationError as error:
        print(f"BLOCKED_AIV0_RUNNER_CONFIGURATION: {error}", file=sys.stderr)
        return 64
    print(result.attempt_dir / "receipt.json")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
