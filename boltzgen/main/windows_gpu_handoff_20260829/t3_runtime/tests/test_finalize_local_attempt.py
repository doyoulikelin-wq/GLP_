from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from test_validate_cell_output import make_fixture as make_output_fixture


T3_ROOT = Path(__file__).resolve().parents[1]
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
GPU_MONITOR_HEADER = (
    "timestamp, index, name, memory.total [MiB], memory.used [MiB], "
    "utilization.gpu [%], power.draw [W]\n"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_asset(path: Path, payload: bytes) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path.resolve()), sha256(path)


def write_probe_telemetry(
    output: Path,
    *,
    memory_used_mib: float = 95.0,
    memory_total_mib: float = 100.0,
) -> tuple[Path, Path, float]:
    logs = output / "operator_logs"
    monitor = logs / "gpu_monitor.csv"
    monitor.write_text(
        GPU_MONITOR_HEADER
        + (
            "2026/08/30 00:00:30.000, 0, NVIDIA GeForce RTX 5070 Ti, "
            f"{memory_total_mib:g} MiB, {memory_used_mib:g} MiB, 1 %, 42 W\n"
        ),
        encoding="utf-8",
    )
    peak = memory_used_mib / memory_total_mib
    peak_path = logs / "peak_memory_fraction.txt"
    peak_path.write_text(f"{peak:.17g}\n", encoding="ascii")
    return monitor, peak_path, peak


def write_environment_manifest(attempt: Path, path: Path) -> None:
    members: list[Path] = []
    for tree_name in ("env", "env_clean_rebuild"):
        for directory, _, files in os.walk(attempt / tree_name, followlinks=False):
            for name in files:
                member = Path(directory) / name
                if member.is_file() and not member.is_symlink():
                    members.append(member)
    members.sort(key=lambda member: member.relative_to(attempt).as_posix().encode("utf-8"))
    path.write_text(
        "".join(
            f"{sha256(member)}  ./{member.relative_to(attempt).as_posix()}\n"
            for member in members
        ),
        encoding="utf-8",
    )


def exec_start_sha256(
    bg_work: Path, contract: Path, submission_token: str, executor_uid: int
) -> str:
    account = pwd.getpwuid(executor_uid)
    argv = [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        TRAMPOLINE_CODE,
        submission_token,
        str(bg_work / "run_local_cell.sh"),
        str(bg_work),
        str(contract),
        EXEC_START_PATH,
        account.pw_dir,
        account.pw_name,
        str(executor_uid),
    ]
    encoded = json.dumps(
        argv, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def copy_runtime(bg_work: Path) -> Path:
    (bg_work / "software").mkdir(parents=True, exist_ok=True)
    for relative in RUNTIME_MEMBERS:
        source = T3_ROOT / Path(relative).name
        destination = bg_work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    manifest = bg_work / "gpu_runtime_scripts_SHA256SUMS"
    manifest.write_text(
        "".join(
            f"{sha256(bg_work / relative)}  ./{relative}\n"
            for relative in sorted(RUNTIME_MEMBERS, key=lambda value: value.encode("utf-8"))
        ),
        encoding="utf-8",
    )
    return manifest


def load_copied_finalizer(bg_work: Path):
    path = bg_work / "software/finalize_local_attempt.py"
    spec = importlib.util.spec_from_file_location(
        f"copied_finalizer_{hash(path)}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_fixture(
    root: Path,
    *,
    formal: bool = False,
    failure: bool = False,
    engineering_probe: bool = False,
):
    assert not (formal and engineering_probe)
    bg_work = root / "bg_work"
    bg_work.mkdir(parents=True)
    runtime_manifest = copy_runtime(bg_work)
    generated = make_output_fixture(root / "generated")
    if engineering_probe:
        cell_id = "6xym_diverse_batch1_engineering"
    else:
        cell_id = "7xl0_adherence_batch1" if formal else "engineering_smoke_7xl0"
    output = bg_work / "runs" / cell_id / "attempt_001"
    output.parent.mkdir(parents=True)
    generated.rename(output)
    logs = output / "operator_logs"
    logs.mkdir()
    for stage in ("configure", "design", "inverse_folding", "folding", "analysis", "filtering"):
        (logs / f"{stage}.stdout.txt").write_text(f"{stage} complete\n", encoding="utf-8")
        (logs / f"{stage}.stderr.txt").write_text("", encoding="utf-8")
        (logs / f"{stage}.exit_code.txt").write_text("0\n", encoding="utf-8")
    if engineering_probe:
        write_probe_telemetry(output)

    environment_attempt = bg_work / "environment" / "attempt_001"
    python_path = environment_attempt / "env" / "bin" / "python"
    if formal:
        system_python = Path("/usr/bin/python3")
        assert system_python.is_file() and system_python.is_symlink()
        for tree_name in ("env", "env_clean_rebuild"):
            tree = environment_attempt / tree_name
            (tree / "bin").mkdir(parents=True)
            (tree / "lib").mkdir()
            (tree / "lib/site-packages.txt").write_text(
                f"{tree_name} packages\n", encoding="utf-8"
            )
            (tree / "bin/environment-tool").write_text(
                f"{tree_name} tool\n", encoding="utf-8"
            )
            (tree / "lib64").symlink_to("lib", target_is_directory=True)
            (tree / "bin/python3").symlink_to(system_python)
            (tree / "bin/python").symlink_to("python3")
            (tree / "bin/python3.12").symlink_to("python3")
    else:
        python_path.parent.mkdir(parents=True)
        python_path.write_text(
            f"#!/bin/sh\nexec {sys.executable} \"$@\"\n", encoding="utf-8"
        )
        python_path.chmod(0o755)
    environment_manifest = environment_attempt / "environment_provenance_SHA256SUMS"
    if formal:
        write_environment_manifest(environment_attempt, environment_manifest)
    else:
        environment_manifest.write_text("environment closure\n", encoding="utf-8")
    environment = environment_attempt / "receipt.json"
    common_environment: dict[str, object] = {
        "attempt_id": "attempt_001",
        "exit_code": 0,
        "failure_codes": [],
        "failure_stage": None,
        "compatibility_activation": "EXPLICIT_PROCESS_LOCAL_ONLY",
    }
    if formal:
        common_environment.update(
            {
                "schema_version": "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1",
                "environment_contract_revision": "WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V1",
                "status": "G1_PASS",
                "formal_g1": True,
                "environment_contract_revision_required": False,
                "official_contract": {
                    "boltzgen": "0.3.2",
                    "cuequivariance": "0.6.1",
                    "torch": "2.8.0+cu128",
                    "torch_cuda": "12.8",
                    "triton": "3.4.0",
                },
                "environment_manifest_sha256": sha256(environment_manifest),
            }
        )
    else:
        common_environment.update(
            {
                "schema_version": "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4",
                "status": "ENGINEERING_COMPATIBILITY_ONLY",
                "formal_g1": False,
                "environment_contract_revision_required": True,
            }
        )
    write_json(environment, common_environment)

    monitor = logs / "monitor.stopped.json"
    write_json(
        monitor,
        {
            "schema_version": "WSL2_LOCAL_GPU_MONITOR_STOP_V1",
            "status": "STOPPED",
            "wait_completed": True,
            "monitor_healthy": True,
            "monitor_started": True,
            "monitor_pid": 12345,
            "stopped_at_utc": "2026-08-30T00:01:00Z",
        },
    )
    resolved = logs / "resolved_config_SHA256SUMS"
    resolved.write_text(
        "".join(
            f"{sha256(path)}  ./{path.relative_to(output).as_posix()}\n"
            for path in sorted((output / "config").iterdir())
        ),
        encoding="utf-8",
    )

    assets = bg_work / "assets"
    bindings: dict[str, str] = {}
    for path_field, sha_field, name in (
        ("spec_path", "spec_sha256", "design.yaml"),
        ("design_checkpoint", "design_checkpoint_sha256", "design.ckpt"),
        ("inverse_fold_checkpoint", "inverse_fold_checkpoint_sha256", "inverse.ckpt"),
        ("folding_checkpoint", "folding_checkpoint_sha256", "fold.ckpt"),
        ("mols_path", "mols_sha256", "mols.zip"),
        ("model_inputs_manifest_path", "model_inputs_manifest_sha256", "model_inputs_SHA256SUMS"),
        ("spec_gate_bundle_path", "spec_gate_bundle_sha256", "spec_gate_bundle.tar"),
        ("input_and_model_manifest_path", "input_and_model_manifest_sha256", "input_and_model_SHA256SUMS"),
    ):
        if engineering_probe and path_field == "spec_path":
            name = "project_input/specs/08_pdb_00006xym-A/design.yaml"
        elif engineering_probe and path_field == "design_checkpoint":
            name = "boltzgen1_diverse.ckpt"
        path_value, digest = write_asset(assets / name, (name + "\n").encode())
        bindings[path_field] = path_value
        bindings[sha_field] = digest

    contract = bg_work / "contracts" / "cell_execution_contract.json"
    contract_payload: dict[str, object] = {
        "schema_version": "WSL2_BOLTZGEN_LOCAL_CELL_V1",
        "cell_id": cell_id,
        "attempt_id": "attempt_001",
        "run_kind": "FORMAL_G2_ACCEPTANCE" if formal else "ENGINEERING_SMOKE",
        "success_status": "G2_PASS" if formal else "ENGINEERING_SMOKE_PASS_NOT_G2",
        "stage_class": "FORMAL" if formal else "ENGINEERING",
        "expected_designs": 10 if formal else 1,
        "expected_fold_samples": 5,
        **bindings,
        "runtime_scripts_manifest_path": str(runtime_manifest.resolve()),
        "runtime_scripts_manifest_sha256": sha256(runtime_manifest),
        "resolved_config_manifest_path": str(resolved.resolve()),
        "resolved_config_manifest_sha256": sha256(resolved),
        "environment_receipt": str(environment.resolve()),
        "environment_receipt_sha256": sha256(environment),
    }
    if engineering_probe:
        peak_path = output / "operator_logs/peak_memory_fraction.txt"
        contract_payload.update(
            {
                "run_kind": "ENGINEERING_MEMORY_PROBE",
                "success_status": "ENGINEERING_MEMORY_PROBE_ONLY",
                "stage_class": "ENGINEERING",
                "probe_id": cell_id,
                "checkpoint_name": "diverse",
                "checkpoint_sha256": bindings["design_checkpoint_sha256"],
                "budget": 1,
                "diffusion_batch_size": 1,
                "inverse_fold_num_sequences": 1,
                "devices": 1,
                "peak_memory_fraction_path": str(peak_path.resolve()),
            }
        )
    if formal:
        contract_payload.update(
            {
                "environment_provenance_manifest_path": str(environment_manifest.resolve()),
                "environment_provenance_manifest_sha256": sha256(environment_manifest),
            }
        )
    write_json(contract, contract_payload)

    contract_sha = sha256(contract)
    submission_token = "1" * 32
    executor_uid = os.geteuid()
    submission = (
        bg_work
        / "local_submissions"
        / f"{cell_id}.attempt_001.receipt.json"
    )
    write_json(
        submission,
        {
            "schema_version": "WSL2_LOCAL_SUBMISSION_RECEIPT_V1",
            "status": "SUBMITTED",
            "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
            "cell_id": cell_id,
            "attempt_id": "attempt_001",
            "cell_contract_path": str(contract.resolve()),
            "cell_contract_sha256": contract_sha,
            "unit": f"boltzgen-local-{contract_sha}.service",
            "active_state_at_receipt": "active",
            "sub_state_at_receipt": "running",
            "unit_result_at_receipt": "success",
            "invocation_id": "2" * 32,
            "runner_path": str((bg_work / "run_local_cell.sh").resolve()),
            "submission_token": submission_token,
            "submitted_at_utc": "2026-08-30T00:00:30Z",
            "executor_uid": executor_uid,
            "exec_start_sha256": exec_start_sha256(
                bg_work, contract, submission_token, executor_uid
            ),
        },
    )

    validation = logs / "cell_contract.json"
    if not failure:
        completed = subprocess.run(
            [str(python_path), "-I", str(bg_work / "software/validate_cell_output.py"), str(output)],
            env={
                "EXPECTED_DESIGNS": "1",
                "EXPECTED_FOLD_SAMPLES": "5",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode()
        validation.write_bytes(completed.stdout)
    else:
        resolved.unlink()
    return bg_work, output, contract, environment, monitor


def rebind_submission(bg_work: Path, contract: Path) -> Path:
    contract_payload = json.loads(contract.read_text(encoding="utf-8"))
    path = (
        bg_work
        / "local_submissions"
        / f"{contract_payload['cell_id']}.{contract_payload['attempt_id']}.receipt.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    contract_sha = sha256(contract)
    payload["cell_contract_sha256"] = contract_sha
    payload["unit"] = f"boltzgen-local-{contract_sha}.service"
    write_json(path, payload)
    return path


def rebind_formal_environment_manifest(
    bg_work: Path, contract: Path, environment: Path, manifest: Path
) -> None:
    environment_payload = json.loads(environment.read_text(encoding="utf-8"))
    environment_payload["environment_manifest_sha256"] = sha256(manifest)
    write_json(environment, environment_payload)
    contract_payload = json.loads(contract.read_text(encoding="utf-8"))
    contract_payload["environment_provenance_manifest_sha256"] = sha256(manifest)
    contract_payload["environment_receipt_sha256"] = sha256(environment)
    write_json(contract, contract_payload)
    rebind_submission(bg_work, contract)


def invoke(
    bg_work: Path,
    output: Path,
    contract: Path,
    environment: Path,
    monitor: Path,
    *,
    status: str,
    exit_code: int,
    submission_receipt: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    contract_payload = json.loads(contract.read_text(encoding="utf-8"))
    submission = (
        bg_work
        / "local_submissions"
        / f"{contract_payload['cell_id']}.{contract_payload['attempt_id']}.receipt.json"
    )
    if submission_receipt is not None:
        submission = submission_receipt
    return subprocess.run(
        [
            sys.executable,
            str(bg_work / "software/finalize_local_attempt.py"),
            "--attempt-root", str(output),
            "--cell-contract", str(contract),
            "--environment-receipt", str(environment),
            "--monitor-stopped", str(monitor),
            "--submission-receipt", str(submission),
            "--terminal-status", status,
            "--pipeline-exit-code", str(exit_code),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def finalizer_arguments(fixture, *, status: str, exit_code: int) -> Namespace:
    bg_work, output, contract, environment, monitor = fixture
    contract_payload = json.loads(contract.read_text(encoding="utf-8"))
    submission = (
        bg_work
        / "local_submissions"
        / f"{contract_payload['cell_id']}.{contract_payload['attempt_id']}.receipt.json"
    )
    return Namespace(
        attempt_root=str(output),
        cell_contract=str(contract),
        environment_receipt=str(environment),
        monitor_stopped=str(monitor),
        submission_receipt=str(submission),
        terminal_status=status,
        pipeline_exit_code=exit_code,
    )


def thaw_output(output: Path) -> None:
    for path in output.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o644)
    for path in sorted(
        (item for item in output.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(0o755)


def test_success_reruns_bound_validator_and_publishes_immutable_marker(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    result = invoke(*fixture, status="ENGINEERING_SMOKE_PASS_NOT_G2", exit_code=0)
    assert result.returncode == 0, result.stderr
    _, output, contract, environment, _ = fixture
    marker_path = output / "operator_logs/cell.SUCCESS.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == "WSL2_BOLTZGEN_LOCAL_SUCCESS_V1"
    assert marker["formal_g1_receipt_sha256"] is None
    assert marker["environment_manifest_sha256"] is None
    validation = json.loads((output / "operator_logs/cell_contract.json").read_text())
    assert marker["validator_sha256"] == validation["validator_sha256"]
    assert marker["environment_receipt_sha256"] == sha256(environment)
    assert marker["execution_contract_sha256"] == sha256(contract)
    submission = rebind_submission(fixture[0], contract)
    submission_payload = json.loads(submission.read_text(encoding="utf-8"))
    assert marker["submission_receipt_sha256"] == sha256(submission)
    assert marker["systemd_unit"] == submission_payload["unit"]
    assert marker["submission_token_sha256"] == hashlib.sha256(
        submission_payload["submission_token"].encode("ascii")
    ).hexdigest()
    assert marker["invocation_id"] == submission_payload["invocation_id"]
    assert marker["executor_uid"] == submission_payload["executor_uid"]
    assert marker["exec_start_sha256"] == submission_payload["exec_start_sha256"]
    assert marker["monitor_healthy"] is True
    assert all(not (path.stat().st_mode & 0o222) for path in output.rglob("*") if path.is_file())


def test_engineering_memory_probe_exact_contract_accepts_canonical_95_percent_peak(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True)

    result = invoke(
        *fixture,
        status="ENGINEERING_MEMORY_PROBE_ONLY",
        exit_code=0,
    )

    assert result.returncode == 0, result.stderr
    marker_path = fixture[1] / "operator_logs/probe.SUCCESS.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["terminal_status"] == "ENGINEERING_MEMORY_PROBE_ONLY"
    assert marker["run_kind"] == "ENGINEERING_MEMORY_PROBE"
    assert marker["probe_id"] == "6xym_diverse_batch1_engineering"
    assert marker["checkpoint_name"] == "diverse"
    assert marker["num_designs"] == 1
    assert marker["diffusion_batch_size"] == 1
    assert marker["fold_samples"] == 5
    assert marker["peak_memory_fraction"] == pytest.approx(0.95)
    assert not (fixture[1] / "operator_logs/cell.SUCCESS.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "batch",
        "status",
        "id",
        "spec",
        "checkpoint_name",
        "checkpoint_path",
        "checkpoint_sha",
        "budget",
        "inverse_fold_num_sequences",
    ],
)
def test_engineering_memory_probe_rejects_near_miss_contracts(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True)
    bg_work, output, contract, _, _ = fixture
    payload = json.loads(contract.read_text(encoding="utf-8"))
    if fault == "batch":
        payload["diffusion_batch_size"] = 2
    elif fault == "status":
        payload["success_status"] = "ENGINEERING_MEMORY_PROBE_PASS"
    elif fault == "id":
        payload["probe_id"] = "6xym_diverse_batch1_engineering_v2"
    elif fault == "spec":
        path, digest = write_asset(
            bg_work / "assets/specs/08_pdb_00006xym-B/design.yaml",
            b"near-miss 6XYM spec\n",
        )
        payload["spec_path"] = path
        payload["spec_sha256"] = digest
    elif fault == "checkpoint_name":
        payload["checkpoint_name"] = "adherence"
    elif fault == "checkpoint_path":
        path, digest = write_asset(
            bg_work / "assets/boltzgen1_diverse_candidate.ckpt",
            b"near-miss checkpoint\n",
        )
        payload["design_checkpoint"] = path
        payload["design_checkpoint_sha256"] = digest
        payload["checkpoint_sha256"] = digest
    elif fault == "checkpoint_sha":
        payload["checkpoint_sha256"] = "0" * 64
    elif fault == "budget":
        payload["budget"] = 2
    else:
        payload["inverse_fold_num_sequences"] = 2
    write_json(contract, payload)
    rebind_submission(bg_work, contract)

    result = invoke(
        *fixture,
        status=str(payload["success_status"]),
        exit_code=0,
    )

    assert result.returncode != 0
    assert not (output / "operator_logs/probe.SUCCESS.json").exists()
    assert not (output / "operator_logs/cell.SUCCESS.json").exists()


@pytest.mark.parametrize("fault", ["peak_mismatch", "bad_monitor_header"])
def test_engineering_memory_probe_rejects_unreconciled_peak_evidence(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True)
    output = fixture[1]
    logs = output / "operator_logs"
    if fault == "peak_mismatch":
        (logs / "peak_memory_fraction.txt").write_text(
            f"{0.94:.17g}\n",
            encoding="ascii",
        )
    else:
        monitor = logs / "gpu_monitor.csv"
        lines = monitor.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("memory.used [MiB]", "memory.free [MiB]")
        monitor.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = invoke(
        *fixture,
        status="ENGINEERING_MEMORY_PROBE_ONLY",
        exit_code=0,
    )

    assert result.returncode != 0
    assert not (output / "operator_logs/probe.SUCCESS.json").exists()


def test_formal_probe_peak_bound_remains_90_percent_with_canonical_telemetry(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True)
    finalizer = load_copied_finalizer(fixture[0])
    write_probe_telemetry(
        fixture[1],
        memory_used_mib=90.0,
        memory_total_mib=100.0,
    )
    assert finalizer.load_peak_fraction(
        fixture[1],
        {},
        formal_probe=True,
    ) == pytest.approx(0.90)

    write_probe_telemetry(
        fixture[1],
        memory_used_mib=95.0,
        memory_total_mib=100.0,
    )
    with pytest.raises(finalizer.FinalizationError):
        finalizer.load_peak_fraction(fixture[1], {}, formal_probe=True)


@pytest.mark.parametrize(
    ("gpu_exit_code", "gpu_stderr", "status", "accepted", "failure_class"),
    [
        (
            137,
            "RuntimeError: CUDA out of memory\n",
            "BLOCKED_GPU_MEMORY",
            True,
            "BLOCKED_GPU_MEMORY",
        ),
        (
            137,
            "RuntimeError: CUDA out of memory\n",
            "LOCAL_CELL_FAILED",
            False,
            None,
        ),
        (7, "generic design failure\n", "LOCAL_CELL_FAILED", True, "PIPELINE_EXIT_NONZERO"),
        (7, "generic design failure\n", "BLOCKED_GPU_MEMORY", False, None),
        (
            0,
            "RuntimeError: CUDA out of memory\n",
            "LOCAL_CELL_FAILED",
            True,
            "PIPELINE_EXIT_NONZERO",
        ),
    ],
)
def test_engineering_memory_probe_failure_status_is_bound_to_gpu_oom_evidence(
    tmp_path: Path,
    gpu_exit_code: int,
    gpu_stderr: str,
    status: str,
    accepted: bool,
    failure_class: str | None,
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True, failure=True)
    output = fixture[1]
    logs = output / "operator_logs"
    (logs / "design.exit_code.txt").write_text(f"{gpu_exit_code}\n", encoding="ascii")
    (logs / "design.stderr.txt").write_text(gpu_stderr, encoding="utf-8")

    result = invoke(*fixture, status=status, exit_code=17)

    marker_path = output / "operator_logs/probe.FAILURE.json"
    if accepted:
        assert result.returncode == 0, result.stderr
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["terminal_status"] == status
        assert marker["failure_class"] == failure_class
    else:
        assert result.returncode != 0
        assert not marker_path.exists()


def test_probe_peak_change_before_manifest_blocks_terminal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True)
    finalizer = load_copied_finalizer(fixture[0])
    original = finalizer.build_manifest
    mutated = False

    def mutate_then_build(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            write_probe_telemetry(fixture[1], memory_used_mib=90.0)
            mutated = True
        return original(*args, **kwargs)

    monkeypatch.setattr(finalizer, "build_manifest", mutate_then_build)

    with pytest.raises(finalizer.FinalizationError):
        finalizer.finalize(
            finalizer_arguments(
                fixture,
                status="ENGINEERING_MEMORY_PROBE_ONLY",
                exit_code=0,
            )
        )
    assert mutated
    assert not (fixture[1] / "operator_logs/probe.SUCCESS.json").exists()


def test_probe_oom_change_before_manifest_blocks_terminal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path, engineering_probe=True, failure=True)
    logs = fixture[1] / "operator_logs"
    (logs / "design.exit_code.txt").write_text("7\n", encoding="ascii")
    stderr = logs / "design.stderr.txt"
    stderr.write_text("generic design failure\n", encoding="utf-8")
    finalizer = load_copied_finalizer(fixture[0])
    original = finalizer.build_manifest
    mutated = False

    def mutate_then_build(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            stderr.write_text("RuntimeError: CUDA out of memory\n", encoding="utf-8")
            mutated = True
        return original(*args, **kwargs)

    monkeypatch.setattr(finalizer, "build_manifest", mutate_then_build)

    with pytest.raises(finalizer.FinalizationError):
        finalizer.finalize(
            finalizer_arguments(fixture, status="LOCAL_CELL_FAILED", exit_code=17)
        )
    assert mutated
    assert not (fixture[1] / "operator_logs/probe.FAILURE.json").exists()


@pytest.mark.parametrize("tamper", ["semantic_file", "receipt"])
def test_stored_validation_must_exactly_match_live_semantics(tmp_path: Path, tamper: str) -> None:
    fixture = make_fixture(tmp_path)
    _, output, _, _, _ = fixture
    if tamper == "semantic_file":
        path = output / "config/design.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    else:
        path = output / "operator_logs/cell_contract.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["filter_final_rows"] += 1
        write_json(path, payload)
    result = invoke(*fixture, status="ENGINEERING_SMOKE_PASS_NOT_G2", exit_code=0)
    assert result.returncode != 0
    assert not (output / "operator_logs/cell.SUCCESS.json").exists()


@pytest.mark.parametrize("fault", ["missing", "extra", "unsorted", "digest"])
def test_runtime_manifest_is_exact_bg_work_code_identity(tmp_path: Path, fault: str) -> None:
    fixture = make_fixture(tmp_path)
    bg_work, output, contract, _, _ = fixture
    manifest = bg_work / "gpu_runtime_scripts_SHA256SUMS"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if fault == "missing":
        lines.pop()
    elif fault == "extra":
        extra = bg_work / "extra.sh"
        extra.write_text("extra\n", encoding="utf-8")
        lines.append(f"{sha256(extra)}  ./extra.sh")
    elif fault == "unsorted":
        lines[0], lines[1] = lines[1], lines[0]
    else:
        lines[0] = "0" * 64 + lines[0][64:]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["runtime_scripts_manifest_sha256"] = sha256(manifest)
    write_json(contract, payload)
    rebind_submission(bg_work, contract)
    result = invoke(*fixture, status="ENGINEERING_SMOKE_PASS_NOT_G2", exit_code=0)
    assert result.returncode != 0
    assert not (output / "operator_logs/cell.SUCCESS.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executor_uid", 999999),
        ("unit", "boltzgen-local-wrong.service"),
        ("runner_path", "/tmp/run_local_cell.sh"),
        ("submission_token", "not-a-token"),
        ("invocation_id", "not-an-invocation"),
        ("exec_start_sha256", "0" * 64),
        ("unexpected_field", True),
    ],
)
def test_submission_receipt_fixed_identity_is_required(
    tmp_path: Path, field: str, value: object
) -> None:
    fixture = make_fixture(tmp_path, failure=True)
    bg_work, output, contract, _, _ = fixture
    submission = rebind_submission(bg_work, contract)
    payload = json.loads(submission.read_text(encoding="utf-8"))
    payload[field] = value
    write_json(submission, payload)

    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=4)

    assert result.returncode != 0
    assert not (output / "operator_logs/cell.FAILURE.json").exists()


def test_submission_receipt_path_must_be_canonical_bg_work_member(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, failure=True)
    bg_work, output, contract, _, _ = fixture
    submission = rebind_submission(bg_work, contract)
    wrong = bg_work / "wrong.receipt.json"
    shutil.copy2(submission, wrong)

    result = invoke(
        *fixture,
        status="LOCAL_CELL_FAILED",
        exit_code=4,
        submission_receipt=wrong,
    )

    assert result.returncode != 0
    assert not (output / "operator_logs/cell.FAILURE.json").exists()


def test_nonzero_pipeline_seals_failure_without_validation_receipt(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, failure=True)
    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=17)
    assert result.returncode == 0, result.stderr
    marker = json.loads((fixture[1] / "operator_logs/cell.FAILURE.json").read_text())
    assert marker["schema_version"] == "WSL2_BOLTZGEN_LOCAL_FAILURE_V1"
    assert marker["status"] == "FAILURE"
    assert marker["pipeline_exit_code"] == 17


def test_unhealthy_stopped_monitor_can_seal_failure_and_is_recorded(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, failure=True)
    monitor = fixture[4]
    payload = json.loads(monitor.read_text(encoding="utf-8"))
    payload["monitor_healthy"] = False
    payload["monitor_started"] = False
    payload["monitor_pid"] = None
    write_json(monitor, payload)

    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=23)

    assert result.returncode == 0, result.stderr
    marker = json.loads((fixture[1] / "operator_logs/cell.FAILURE.json").read_text())
    assert marker["monitor_healthy"] is False


def test_unhealthy_monitor_cannot_seal_success(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    monitor = fixture[4]
    payload = json.loads(monitor.read_text(encoding="utf-8"))
    payload["monitor_healthy"] = False
    write_json(monitor, payload)

    result = invoke(*fixture, status="ENGINEERING_SMOKE_PASS_NOT_G2", exit_code=0)

    assert result.returncode != 0
    assert not (fixture[1] / "operator_logs/cell.SUCCESS.json").exists()


def test_formal_receipt_and_environment_manifest_are_strictly_bound(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, formal=True, failure=True)
    for tree_name in ("env", "env_clean_rebuild"):
        tree = fixture[3].parent / tree_name
        assert os.readlink(tree / "lib64") == "lib"
        assert os.readlink(tree / "bin/python") == "python3"
        assert os.readlink(tree / "bin/python3") == "/usr/bin/python3"
        assert os.readlink(tree / "bin/python3.12") == "python3"
    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=9)
    assert result.returncode == 0, result.stderr
    _, output, _, environment, _ = fixture
    marker = json.loads((output / "operator_logs/cell.FAILURE.json").read_text())
    receipt = json.loads(environment.read_text(encoding="utf-8"))
    assert marker["formal_g1_receipt_sha256"] == sha256(environment)
    assert marker["environment_manifest_sha256"] == receipt["environment_manifest_sha256"]


@pytest.mark.parametrize(
    "fault", ["malformed", "member_tamper", "missing", "extra", "symlink"]
)
def test_formal_environment_manifest_rejects_open_or_invalid_closure(
    tmp_path: Path, fault: str
) -> None:
    fixture = make_fixture(tmp_path, formal=True, failure=True)
    bg_work, output, contract, environment, _ = fixture
    attempt = environment.parent
    manifest = attempt / "environment_provenance_SHA256SUMS"
    if fault == "malformed":
        manifest.write_text("not a canonical SHA256SUMS\n", encoding="utf-8")
        rebind_formal_environment_manifest(bg_work, contract, environment, manifest)
    elif fault == "member_tamper":
        member = attempt / "env/lib/site-packages.txt"
        member.write_text("tampered packages\n", encoding="utf-8")
    elif fault == "missing":
        lines = manifest.read_text(encoding="utf-8").splitlines()
        manifest.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        rebind_formal_environment_manifest(bg_work, contract, environment, manifest)
    elif fault == "extra":
        (attempt / "env/unlisted.txt").write_text("unlisted\n", encoding="utf-8")
    else:
        (attempt / "env/unexpected_link").symlink_to("lib")

    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=9)

    assert result.returncode != 0
    assert not (output / "operator_logs/cell.FAILURE.json").exists()


@pytest.mark.parametrize(
    "relative_link", ["lib64", "bin/python", "bin/python3", "bin/python3.12"]
)
def test_formal_environment_requires_each_fixed_venv_symlink(
    tmp_path: Path, relative_link: str
) -> None:
    fixture = make_fixture(tmp_path, formal=True, failure=True)
    _, output, _, environment, _ = fixture
    (environment.parent / "env" / relative_link).unlink()

    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=9)

    assert result.returncode != 0
    assert not (output / "operator_logs/cell.FAILURE.json").exists()


def test_formal_environment_member_drift_after_initial_validation_blocks_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path, formal=True, failure=True)
    finalizer = load_copied_finalizer(fixture[0])
    original = finalizer.build_manifest
    member = fixture[3].parent / "env/lib/site-packages.txt"
    mutated = False

    def mutate_then_build(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            member.write_text("drift after initial validation\n", encoding="utf-8")
            mutated = True
        return original(*args, **kwargs)

    monkeypatch.setattr(finalizer, "build_manifest", mutate_then_build)

    with pytest.raises(finalizer.FinalizationError):
        finalizer.finalize(
            finalizer_arguments(fixture, status="LOCAL_CELL_FAILED", exit_code=9)
        )
    assert not (fixture[1] / "operator_logs/cell.FAILURE.json").exists()


@pytest.mark.parametrize("target", ["receipt", "manifest"])
def test_formal_environment_binding_drift_at_terminal_prelink_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    fixture = make_fixture(tmp_path, formal=True, failure=True)
    finalizer = load_copied_finalizer(fixture[0])
    original = finalizer.verify_evidence_frozen
    external = (
        fixture[3]
        if target == "receipt"
        else fixture[3].parent / "environment_provenance_SHA256SUMS"
    )

    def mutate_after_freeze(*args, **kwargs):
        result = original(*args, **kwargs)
        external.write_text(
            external.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(finalizer, "verify_evidence_frozen", mutate_after_freeze)
    try:
        with pytest.raises(finalizer.FinalizationError):
            finalizer.finalize(
                finalizer_arguments(fixture, status="LOCAL_CELL_FAILED", exit_code=9)
            )
        assert not (fixture[1] / "operator_logs/cell.FAILURE.json").exists()
    finally:
        thaw_output(fixture[1])


def test_runs_ancestor_rename_and_symlink_at_terminal_prelink_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path, failure=True)
    bg_work, output, _, _, _ = fixture
    finalizer = load_copied_finalizer(bg_work)
    original = finalizer.verify_evidence_frozen
    runs = bg_work / "runs"
    moved = bg_work / "runs.held-original"
    mutated = False

    def replace_runs_after_freeze(*args, **kwargs):
        nonlocal mutated
        result = original(*args, **kwargs)
        runs.rename(moved)
        runs.symlink_to(moved, target_is_directory=True)
        mutated = True
        return result

    monkeypatch.setattr(finalizer, "verify_evidence_frozen", replace_runs_after_freeze)
    try:
        with pytest.raises(finalizer.FinalizationError):
            finalizer.finalize(
                finalizer_arguments(fixture, status="LOCAL_CELL_FAILED", exit_code=9)
            )
        assert mutated
        assert not (output / "operator_logs/cell.FAILURE.json").exists()
    finally:
        if runs.is_symlink():
            runs.unlink()
        if moved.exists():
            moved.rename(runs)
        thaw_output(output)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4"),
        ("environment_contract_revision", "ENGINEERING_CANDIDATE_V4"),
        ("failure_codes", ["drift"]),
        ("environment_contract_revision_required", True),
        ("official_contract", {}),
        ("environment_manifest_sha256", "0" * 64),
    ],
)
def test_formal_receipt_rejects_nonformal_or_incomplete_contract(tmp_path: Path, field: str, value: object) -> None:
    fixture = make_fixture(tmp_path, formal=True, failure=True)
    _, output, contract, environment, _ = fixture
    payload = json.loads(environment.read_text(encoding="utf-8"))
    payload[field] = value
    write_json(environment, payload)
    contract_payload = json.loads(contract.read_text(encoding="utf-8"))
    contract_payload["environment_receipt_sha256"] = sha256(environment)
    write_json(contract, contract_payload)
    rebind_submission(fixture[0], contract)
    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=9)
    assert result.returncode != 0
    assert not (output / "operator_logs/cell.FAILURE.json").exists()


@pytest.mark.parametrize("stage_class", ["FORMAL_G2", "engineering", "FORMAL "])
def test_stage_class_is_canonical_enum(tmp_path: Path, stage_class: str) -> None:
    fixture = make_fixture(tmp_path, failure=True)
    _, output, contract, _, _ = fixture
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["stage_class"] = stage_class
    write_json(contract, payload)
    rebind_submission(fixture[0], contract)
    result = invoke(*fixture, status="LOCAL_CELL_FAILED", exit_code=1)
    assert result.returncode != 0
    assert not (output / "operator_logs/cell.FAILURE.json").exists()
