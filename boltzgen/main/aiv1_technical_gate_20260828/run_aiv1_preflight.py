#!/usr/bin/env python3
"""Run an immutable AIV1 readiness preflight without performing inference.

The preflight is allowed on macOS because it only validates contracts and
records blockers.  It can never issue an AIV1 PASS receipt.  Formal AIV1
execution remains a separate Linux/NVIDIA stage.

Code source: project_original.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from build_ai_validation_matrix import (
    CANONICAL_AIV0_SUMMARY_RELATIVE,
    CANONICAL_INPUT_CONTRACT_RELATIVE,
    CANONICAL_REGISTRY_SCHEMA_RELATIVE,
    CANONICAL_STATE_CONTRACT_RELATIVE,
    ContractViolation,
    canonical_json,
    canonical_uri,
    load_input_contract,
    require_directory,
    require_canonical_repo_file,
    require_file,
    resolve_uri,
    sha256_file,
    validate_aiv0_handoff,
    validate_anchors_and_g2,
    validate_g2_evidence_chain,
    validate_states,
)


STAGE_ID = "aiv1_readiness_preflight"
ATTEMPT_PATTERN = re.compile(r"attempt_[0-9]{3}")

FORMAL_IMPLEMENTATION_FILES = (
    "compute_project_metrics.py",
    "run_multistate_ai_validation.py",
    "update_ai_experience_registry.py",
    "freeze_ai_eval_spec.py",
    "validate_aiv1_campaign.py",
    "run_aiv1_stage.py",
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def write_new(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_receipt(path: Path, payload: object) -> None:
    if path.exists():
        raise FileExistsError(f"receipt already exists: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write_new(temporary, canonical_json(payload))
        os.link(temporary, path)
        temporary.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def probe_torch() -> Mapping[str, object]:
    script = (
        "import json,torch;"
        "print(json.dumps({'version':torch.__version__,"
        "'cuda_available':torch.cuda.is_available(),"
        "'mps_available':bool(getattr(torch.backends,'mps',None) and "
        "torch.backends.mps.is_available())},sort_keys=True))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return {
            "probe_status": "UNAVAILABLE",
            "exit_code": completed.returncode,
            "stderr": completed.stderr[-1000:],
        }
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"probe_status": "INVALID_OUTPUT", "exit_code": 0}
    result["probe_status"] = "PASS"
    return result


def probe_platform(path_for_disk: Path) -> Mapping[str, object]:
    nvidia_smi = shutil.which("nvidia-smi")
    nvidia_result: dict[str, object]
    if nvidia_smi is None:
        nvidia_result = {"available": False, "exit_code": None, "output": ""}
    else:
        completed = subprocess.run(
            [nvidia_smi, "-L"], capture_output=True, check=False, text=True, timeout=30
        )
        nvidia_result = {
            "available": completed.returncode == 0,
            "exit_code": completed.returncode,
            "output": completed.stdout.strip()[:4000],
        }
    disk = shutil.disk_usage(path_for_disk)
    torch_probe = probe_torch()
    linux = platform.system() == "Linux"
    x86_64 = platform.machine() in {"x86_64", "amd64"}
    nvidia_ready = nvidia_result["available"] is True
    cuda_ready = torch_probe.get("cuda_available") is True
    formal_ready = linux and x86_64 and nvidia_ready and cuda_ready
    return {
        "schema_version": "AIV1_PREFLIGHT_PLATFORM_PROBE_V1",
        "os_family": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable_sha256": sha256_file(
            Path(sys.executable).resolve(strict=True)
        ),
        "nvidia_smi": nvidia_result,
        "torch": torch_probe,
        "disk_free_bytes": disk.free,
        "recommended_scratch_min_bytes": 250 * 1024**3,
        "scratch_recommendation_met": disk.free >= 250 * 1024**3,
        "gpu_host_configured": bool(os.environ.get("GPU_HOST")),
        "gpu_project_root_configured": bool(os.environ.get("GPU_PROJECT_ROOT")),
        "formal_linux_nvidia_ready": formal_ready,
    }


def implementation_inventory(code_root: Path) -> Mapping[str, object]:
    rows: list[dict[str, object]] = []
    for name in FORMAL_IMPLEMENTATION_FILES:
        path = code_root / name
        test_path = code_root / f"test_{Path(name).stem}.py"
        source_present = (
            path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        )
        test_present = (
            test_path.is_file()
            and not test_path.is_symlink()
            and test_path.stat().st_size > 0
        )
        source_syntax_valid = False
        test_syntax_valid = False
        if source_present:
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (UnicodeDecodeError, SyntaxError):
                pass
            else:
                source_syntax_valid = True
        if test_present:
            try:
                ast.parse(
                    test_path.read_text(encoding="utf-8"), filename=str(test_path)
                )
            except (UnicodeDecodeError, SyntaxError):
                pass
            else:
                test_syntax_valid = True
        rows.append(
            {
                "filename": name,
                "present_nonempty_regular_file": source_present,
                "syntax_valid": source_syntax_valid,
                "sha256": sha256_file(path) if source_present else None,
                "test_filename": test_path.name,
                "test_present_nonempty_regular_file": test_present,
                "test_syntax_valid": test_syntax_valid,
                "test_sha256": sha256_file(test_path) if test_present else None,
                "implementation_unit_ready": (
                    source_present
                    and source_syntax_valid
                    and test_present
                    and test_syntax_valid
                ),
            }
        )
    return {
        "schema_version": "AIV1_IMPLEMENTATION_INVENTORY_V1",
        "formal_execution_files": rows,
        "present_count": sum(
            bool(row["present_nonempty_regular_file"]) for row in rows
        ),
        "ready_unit_count": sum(bool(row["implementation_unit_ready"]) for row in rows),
        "required_count": len(rows),
        "formal_implementation_complete": all(
            bool(row["implementation_unit_ready"]) for row in rows
        ),
        "note": "presence plus syntax-valid source/test pairs is only the preflight minimum; formal stage validation remains required",
    }


def create_attempt(
    run_root: Path, repo_root: Path, workspace_root: Path, attempt_id: str
) -> Path:
    if ATTEMPT_PATTERN.fullmatch(attempt_id) is None:
        raise ContractViolation(
            "BLOCKED_ATTEMPT_ID", "attempt ID must match attempt_NNN"
        )
    resolved_run = require_directory(run_root, "AIV1 preflight run root")
    resolved_repo = require_directory(repo_root, "repository root")
    resolved_workspace = require_directory(workspace_root, "workspace root")
    expected_run = (
        resolved_workspace / "boltzgen/runs/glp1_vhh_aiv1_preflight_20260828"
    ).resolve(strict=True)
    frozen_aiv0 = resolved_workspace / "boltzgen/runs/glp1_vhh_formal_campaign_20260828"
    if resolved_run != expected_run:
        raise ContractViolation(
            "BLOCKED_RUN_ROOT",
            "AIV1 preflight run root must be "
            "workspace://boltzgen/runs/glp1_vhh_aiv1_preflight_20260828/",
        )
    try:
        resolved_run.relative_to(frozen_aiv0.resolve(strict=False))
    except ValueError:
        pass
    else:
        raise ContractViolation(
            "BLOCKED_RUN_ROOT", "AIV1 preflight cannot write into the frozen AIV0 root"
        )
    try:
        resolved_run.relative_to(resolved_repo)
    except ValueError:
        pass
    else:
        raise ContractViolation(
            "BLOCKED_RUN_ROOT", "preflight run root must be outside the repository"
        )
    current = resolved_run
    for name in ("logs", "stages", STAGE_ID):
        current = current / name
        if current.is_symlink():
            raise ContractViolation(
                "BLOCKED_RUN_ROOT", f"symlinked stage path: {current}"
            )
        current.mkdir(mode=0o750, exist_ok=True)
    attempt = current / attempt_id
    try:
        attempt.mkdir(mode=0o750)
    except FileExistsError as error:
        raise ContractViolation(
            "BLOCKED_IMMUTABLE_OUTPUT_EXISTS", f"attempt exists: {attempt}"
        ) from error
    return attempt


def run_contract_tests(code_root: Path) -> tuple[Mapping[str, object], str, Path]:
    test_path = require_file(
        code_root / "test_build_ai_validation_matrix.py", "AIV1 contract tests"
    )
    command = [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "-v",
        test_path.name,
    ]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        command,
        cwd=code_root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    log = (
        f"command={json.dumps(command, ensure_ascii=True)}\n"
        f"cwd={code_root}\n"
        "--- stdout ---\n"
        f"{completed.stdout}"
        "--- stderr ---\n"
        f"{completed.stderr}"
    )
    match = re.search(r"Ran ([0-9]+) tests?", completed.stdout + completed.stderr)
    test_count = int(match.group(1)) if match is not None else None
    combined_output = completed.stdout + completed.stderr
    failure_count = sum(
        int(value)
        for value in re.findall(
            r"(?:failures|errors|unexpected successes)=([0-9]+)", combined_output
        )
    )
    skipped_match = re.search(r"skipped=([0-9]+)", combined_output)
    skipped_count = int(skipped_match.group(1)) if skipped_match is not None else 0
    passed_count = (
        test_count - failure_count - skipped_count if test_count is not None else None
    )
    tests_pass = (
        completed.returncode == 0
        and test_count is not None
        and test_count > 0
        and failure_count == 0
        and skipped_count == 0
    )
    result = {
        "schema_version": "AIV1_CONTRACT_TEST_RESULT_V1",
        "status": "PASS" if tests_pass else "FAIL",
        "exit_code": completed.returncode,
        "test_count": test_count,
        "test_passed": passed_count,
        "test_failed": failure_count,
        "test_skipped": skipped_count,
        "test_script_sha256": sha256_file(test_path),
        "python_executable_sha256": sha256_file(
            Path(sys.executable).resolve(strict=True)
        ),
        "python_version": platform.python_version(),
        "command": command,
    }
    return result, log, test_path


def command_snapshot(
    args: argparse.Namespace, *, repo_root: Path, workspace_root: Path
) -> Mapping[str, object]:
    rendered: dict[str, object] = {}
    for key, value in vars(args).items():
        if isinstance(value, Path):
            candidate = value.resolve(strict=False)
            for prefix, root in (
                ("repo", repo_root),
                ("workspace", workspace_root),
            ):
                try:
                    relative = candidate.relative_to(root)
                except ValueError:
                    continue
                rendered[key] = f"{prefix}://{relative.as_posix()}"
                break
            else:
                rendered[key] = str(candidate)
        else:
            rendered[key] = value
    return {
        "schema_version": "AIV1_PREFLIGHT_COMMAND_V1",
        "entrypoint": "run_aiv1_preflight.py",
        "arguments": rendered,
    }


def run_preflight(args: argparse.Namespace) -> tuple[int, Path, Mapping[str, object]]:
    repo_root = require_directory(args.repo_root, "repository root")
    workspace_root = require_directory(args.workspace_root, "workspace root")
    input_contract_path = require_canonical_repo_file(
        args.input_contract,
        repo_root=repo_root,
        relative=CANONICAL_INPUT_CONTRACT_RELATIVE,
        label="AIV1 input contract",
    )
    state_contract_path = require_canonical_repo_file(
        args.state_contract,
        repo_root=repo_root,
        relative=CANONICAL_STATE_CONTRACT_RELATIVE,
        label="state contract",
    )
    inventory_path = require_file(args.inventory, "AIV0 inventory")
    aiv0_summary_path = require_canonical_repo_file(
        args.aiv0_summary,
        repo_root=repo_root,
        relative=CANONICAL_AIV0_SUMMARY_RELATIVE,
        label="AIV0 summary",
    )
    aiv0_receipt_path = require_file(args.aiv0_receipt, "AIV0 receipt")
    aiv0_derived_manifest_path = require_file(
        args.aiv0_derived_manifest, "AIV0 derived manifest"
    )
    sql_schema_path = require_canonical_repo_file(
        args.registry_schema,
        repo_root=repo_root,
        relative=CANONICAL_REGISTRY_SCHEMA_RELATIVE,
        label="AIV1 registry schema",
    )
    attempt_dir = create_attempt(
        args.run_root, repo_root, workspace_root, args.attempt_id
    )
    started = utc_now()
    write_new(attempt_dir / "started_at_utc.txt", started + "\n")
    write_new(
        attempt_dir / "command.json",
        canonical_json(
            command_snapshot(args, repo_root=repo_root, workspace_root=workspace_root)
        ),
    )
    code_root = Path(__file__).resolve(strict=True).parent
    contract_tests, contract_test_log, contract_test_path = run_contract_tests(
        code_root
    )
    write_new(attempt_dir / "contract_tests.log", contract_test_log)
    write_new(attempt_dir / "contract_tests.json", canonical_json(contract_tests))
    input_contract = load_input_contract(input_contract_path)
    checks: list[dict[str, object]] = [
        {
            "check": "local_contract_tests",
            "status": contract_tests["status"],
            "test_count": contract_tests["test_count"],
        }
    ]
    fatal_contract_error: ContractViolation | None = None
    if contract_tests["status"] != "PASS":
        fatal_contract_error = ContractViolation(
            "BLOCKED_CONTRACT_TESTS", "local AIV1 contract tests failed"
        )
    if sha256_file(sql_schema_path) != input_contract.get(
        "experience_registry_schema_sha256"
    ):
        schema_error = ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "experience registry schema hash differs from AIV1 input contract",
        )
        if fatal_contract_error is None:
            fatal_contract_error = schema_error
        checks.append(
            {
                "check": "experience_registry_schema",
                "status": "FAIL",
                "code": schema_error.code,
                "detail": schema_error.message,
            }
        )
    else:
        checks.append({"check": "experience_registry_schema", "status": "PASS"})
    try:
        aiv0_handoff = validate_aiv0_handoff(
            summary_path=aiv0_summary_path,
            receipt_path=aiv0_receipt_path,
            derived_manifest_path=aiv0_derived_manifest_path,
            inventory_path=inventory_path,
        )
        checks.append({"check": "aiv0_handoff", "status": "PASS"})
    except ContractViolation as error:
        aiv0_handoff = {}
        if fatal_contract_error is None:
            fatal_contract_error = error
        checks.append(
            {
                "check": "aiv0_handoff",
                "status": "FAIL",
                "code": error.code,
                "detail": error.message,
            }
        )

    try:
        states = validate_states(
            contract_path=state_contract_path,
            inventory_path=inventory_path,
            input_contract=input_contract,
            workspace_root=workspace_root,
        )
        checks.append(
            {
                "check": "development_state_contract",
                "status": "PASS",
                "state_count": len(states),
                "lockbox_state_count": 0,
            }
        )
    except ContractViolation as error:
        states = ()
        if fatal_contract_error is None:
            fatal_contract_error = error
        checks.append(
            {
                "check": "development_state_contract",
                "status": "FAIL",
                "code": error.code,
                "detail": error.message,
            }
        )

    platform_probe = probe_platform(workspace_root)
    implementations = implementation_inventory(
        Path(__file__).resolve(strict=True).parent
    )
    anchor_present = args.anchor_manifest.is_file() and args.g2_receipt.is_file()
    anchors: Sequence[Mapping[str, str]] = ()
    validated_g2_receipt: Mapping[str, object] | None = None
    anchor_validation_error: ContractViolation | None = None
    anchor_skipped_upstream = anchor_present and fatal_contract_error is not None
    if anchor_present and fatal_contract_error is None:
        try:
            anchors, validated_g2_receipt = validate_anchors_and_g2(
                anchor_manifest_path=args.anchor_manifest,
                g2_receipt_path=args.g2_receipt,
                input_contract=input_contract,
                aiv0_handoff=aiv0_handoff,
                repo_root=repo_root,
                workspace_root=workspace_root,
            )
        except ContractViolation as error:
            anchor_validation_error = error
            fatal_contract_error = error
    blockers: list[str] = []
    if not platform_probe["formal_linux_nvidia_ready"]:
        blockers.append("BLOCKED_EXTERNAL_INFRASTRUCTURE")
    if not platform_probe["scratch_recommendation_met"]:
        blockers.append("BLOCKED_INSUFFICIENT_SCRATCH")
    if not anchor_present:
        blockers.append("BLOCKED_MISSING_G2_ANCHORS")
    if not implementations["formal_implementation_complete"]:
        blockers.append("BLOCKED_MISSING_AIV1_IMPLEMENTATION")

    anchor_status = {
        "anchor_manifest_uri": (
            canonical_uri(
                args.anchor_manifest.resolve(strict=True),
                repo_root=repo_root,
                workspace_root=workspace_root,
            )
            if args.anchor_manifest.is_file()
            else None
        ),
        "g2_receipt_uri": (
            canonical_uri(
                args.g2_receipt.resolve(strict=True),
                repo_root=repo_root,
                workspace_root=workspace_root,
            )
            if args.g2_receipt.is_file()
            else None
        ),
        "anchor_manifest_present": args.anchor_manifest.is_file(),
        "g2_receipt_present": args.g2_receipt.is_file(),
        "formal_anchor_validation_status": (
            "NOT_RUN_MISSING_INPUT"
            if not anchor_present
            else (
                "NOT_RUN_UPSTREAM_FAILURE"
                if anchor_skipped_upstream
                else "FAIL" if anchor_validation_error is not None else "PASS"
            )
        ),
        "formal_anchor_count": len(anchors),
        "validation_code": (
            anchor_validation_error.code
            if anchor_validation_error is not None
            else None
        ),
    }
    checks.extend(
        (
            {
                "check": "formal_linux_nvidia_runtime",
                "status": (
                    "PASS" if platform_probe["formal_linux_nvidia_ready"] else "BLOCKED"
                ),
            },
            {
                "check": "g2_anchor_release",
                "status": anchor_status["formal_anchor_validation_status"],
                "anchor_count": len(anchors),
            },
            {
                "check": "formal_aiv1_implementation",
                "status": (
                    "PASS"
                    if implementations["formal_implementation_complete"]
                    else "BLOCKED"
                ),
            },
        )
    )

    if fatal_contract_error is not None:
        status = "FAIL_INPUT_CONTRACT"
        decision_code = fatal_contract_error.code
        exit_code = 2
    elif blockers:
        status = "BLOCKED_PREREQUISITES"
        decision_code = (
            "BLOCKED_EXTERNAL_INFRASTRUCTURE"
            if "BLOCKED_EXTERNAL_INFRASTRUCTURE" in blockers
            else blockers[0]
        )
        exit_code = 3
    else:
        status = "READY_FOR_FORMAL_AIV1_INPUT_VALIDATION"
        decision_code = "READY_FOR_FORMAL_AIV1_INPUT_VALIDATION"
        exit_code = 0

    write_new(attempt_dir / "checks.json", canonical_json(checks))
    write_new(attempt_dir / "platform_probe.json", canonical_json(platform_probe))
    write_new(
        attempt_dir / "implementation_inventory.json", canonical_json(implementations)
    )
    write_new(attempt_dir / "anchor_readiness.json", canonical_json(anchor_status))
    write_new(attempt_dir / "aiv0_handoff.json", canonical_json(aiv0_handoff))
    state_report = {
        "schema_version": "AIV1_STATE_VALIDATION_V1",
        "status": "PASS" if len(states) == 16 else "FAIL",
        "state_count": len(states),
        "expected_state_count": 16,
        "expected_candidate_count": 10,
        "expected_logical_task_count": 160,
        "expected_sample_result_rows": 800,
        "lockbox_state_count": 0 if len(states) == 16 else None,
        "state_ids": [row["target_state_id"] for row in states],
    }
    write_new(attempt_dir / "state_validation.json", canonical_json(state_report))
    input_files: list[Path] = [
        Path(__file__).resolve(strict=True),
        (Path(__file__).resolve(strict=True).parent / "build_ai_validation_matrix.py"),
        contract_test_path,
        input_contract_path,
        state_contract_path,
        inventory_path,
        aiv0_summary_path,
        aiv0_receipt_path,
        aiv0_derived_manifest_path,
        sql_schema_path,
    ]
    for optional_input in (args.anchor_manifest, args.g2_receipt):
        if optional_input.is_file():
            input_files.append(require_file(optional_input, "optional G2 input"))
    if validated_g2_receipt is not None:
        _, _, g2_paths = validate_g2_evidence_chain(
            receipt=validated_g2_receipt,
            repo_root=repo_root,
            workspace_root=workspace_root,
        )
        input_files.extend(g2_paths)
        for uri_field in ("platform_evidence_uri", "aggregate_metrics_uri"):
            input_files.append(
                resolve_uri(
                    str(validated_g2_receipt[uri_field]),
                    repo_root=repo_root,
                    workspace_root=workspace_root,
                    label=uri_field,
                )
            )
        input_files.extend(
            resolve_uri(
                str(anchor["candidate_artifact_uri"]),
                repo_root=repo_root,
                workspace_root=workspace_root,
                label=f"candidate artifact {anchor['candidate_id']}",
            )
            for anchor in anchors
        )
    input_rows = [
        f"{sha256_file(path)}  {canonical_uri(path, repo_root=repo_root, workspace_root=workspace_root)}"
        for path in sorted(set(input_files))
    ]
    write_new(attempt_dir / "inputs.SHA256SUMS", "\n".join(sorted(input_rows)) + "\n")
    report = {
        "schema_version": "AIV1_PREFLIGHT_REPORT_V1",
        "status": status,
        "decision_code": decision_code,
        "blockers": sorted(set(blockers)),
        "contract_test_status": contract_tests["status"],
        "contract_test_count": contract_tests["test_count"],
        "contract_test_passed": contract_tests["test_passed"],
        "contract_test_failed": contract_tests["test_failed"],
        "contract_test_skipped": contract_tests["test_skipped"],
        "aiv0_handoff_status": next(
            item["status"] for item in checks if item["check"] == "aiv0_handoff"
        ),
        "development_state_status": next(
            item["status"]
            for item in checks
            if item["check"] == "development_state_contract"
        ),
        "development_state_count": len(states),
        "formal_anchor_count": len(anchors),
        "task_matrix_materialized": False,
        "inference_started": False,
        "lockbox_access_count": 0,
        "model_training_started": False,
        "scientific_boundary": input_contract["scientific_boundary"],
        "next_required_action": "complete Linux/NVIDIA G1 and G2, freeze the exact ten official anchors, then finish and validate the formal AIV1 execution stack",
    }
    write_new(attempt_dir / "preflight_report.json", canonical_json(report))
    ended = utc_now()
    output_members = sorted(
        path
        for path in attempt_dir.iterdir()
        if path.is_file() and path.name != "receipt.json"
    )
    output_manifest = (
        "\n".join(f"{sha256_file(path)}  {path.name}" for path in output_members) + "\n"
    )
    write_new(attempt_dir / "outputs.SHA256SUMS", output_manifest)
    receipt = {
        "schema_version": "AIV1_PREFLIGHT_RECEIPT_V1",
        "stage_id": STAGE_ID,
        "attempt_id": args.attempt_id,
        "status": status,
        "decision_code": decision_code,
        "exit_code": exit_code,
        "started_at_utc": started,
        "ended_at_utc": ended,
        "inputs_manifest_sha256": sha256_file(attempt_dir / "inputs.SHA256SUMS"),
        "outputs_manifest_sha256": sha256_file(attempt_dir / "outputs.SHA256SUMS"),
        "preflight_report_sha256": sha256_file(attempt_dir / "preflight_report.json"),
        "contract_test_status": contract_tests["status"],
        "contract_test_count": contract_tests["test_count"],
        "contract_test_passed": contract_tests["test_passed"],
        "contract_test_failed": contract_tests["test_failed"],
        "contract_test_skipped": contract_tests["test_skipped"],
        "contract_test_script_sha256": contract_tests["test_script_sha256"],
        "contract_test_log_sha256": sha256_file(attempt_dir / "contract_tests.log"),
        "aiv0_final_check_receipt_sha256": aiv0_handoff.get(
            "aiv0_final_check_receipt_sha256"
        ),
        "development_state_count": len(states),
        "formal_anchor_count": len(anchors),
        "task_matrix_count": 0,
        "sample_result_count": 0,
        "lockbox_access_count": 0,
        "inference_started": False,
        "model_training_started": False,
        "is_aiv1_pass": False,
    }
    atomic_receipt(attempt_dir / "receipt.json", receipt)
    return exit_code, attempt_dir, receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--input-contract", type=Path, required=True)
    parser.add_argument("--state-contract", type=Path, required=True)
    parser.add_argument("--registry-schema", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--aiv0-summary", type=Path, required=True)
    parser.add_argument("--aiv0-receipt", type=Path, required=True)
    parser.add_argument("--aiv0-derived-manifest", type=Path, required=True)
    parser.add_argument("--anchor-manifest", type=Path, required=True)
    parser.add_argument("--g2-receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, attempt_dir, receipt = run_preflight(args)
    except ContractViolation as error:
        print(
            canonical_json(
                {"status": "BLOCKED", "code": error.code, "detail": error.message}
            ),
            end="",
        )
        return 2
    print(
        canonical_json(
            {
                "attempt_dir": str(attempt_dir),
                "receipt": receipt,
            }
        ),
        end="",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
