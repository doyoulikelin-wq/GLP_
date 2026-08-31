from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import torch

from conftest import implementation, sha256


PIP_CHECK_NAMES = (
    "pip_check.production.before_smoke.txt",
    "pip_check.production.after_smoke.txt",
    "pip_check.clean_rebuild.before_smoke.txt",
    "pip_check.clean_rebuild.after_smoke.txt",
)
RUNTIME_MEMBERS = tuple(
    sorted(
        (
            "run_local_cell.sh",
            "software/finalize_local_attempt.py",
            "software/validate_cell_output.py",
            "status_local_cell.sh",
            "submit_local_once.sh",
            "verify_gpu_env_stage.sh",
        ),
        key=lambda value: value.encode("utf-8"),
    )
)


def live_driver() -> str:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.splitlines()[0].strip()


def live_freeze() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--all"],
        text=True,
        capture_output=True,
        check=True,
    )
    return "\n".join(sorted(result.stdout.splitlines())) + "\n"


def write_manifest(base: Path, output: Path, paths: list[Path]) -> None:
    output.write_text(
        "".join(
            f"{sha256(path)}  {path.relative_to(base).as_posix()}\n"
            for path in sorted(paths, key=lambda value: value.relative_to(base).as_posix())
        ),
        encoding="utf-8",
    )


def make_fixture(
    root: Path,
    *,
    stage_class: str = "ENGINEERING",
    clean_freeze_drift: bool = False,
    bad_pip_check: bool = False,
    inventory_overrides: dict[str, object] | None = None,
) -> Path:
    bg_work = root / "bg_work"
    contract_dir = bg_work / "contract"
    contract_dir.mkdir(parents=True)
    for relative in RUNTIME_MEMBERS:
        target = bg_work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == "verify_gpu_env_stage.sh":
            target.write_bytes(implementation("verify_gpu_env_stage.sh").read_bytes())
        else:
            target.write_text(f"# fixture runtime member: {relative}\n", encoding="utf-8")
        target.chmod(0o755)
    runtime_manifest = bg_work / "gpu_runtime_scripts_SHA256SUMS"
    runtime_manifest.write_text(
        "".join(f"{sha256(bg_work / relative)}  ./{relative}\n" for relative in RUNTIME_MEMBERS),
        encoding="utf-8",
    )
    attempt = root / "attempt_004"
    env_bin = attempt / "env" / "bin"
    env_bin.mkdir(parents=True)
    environment_python = env_bin / "python"
    environment_python.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = -I ] && [ \"${2:-}\" = - ]; then\n"
        "  shift 2\n"
        f"  exec {shlex.quote(sys.executable)} -I -c "
        + shlex.quote(
            "import sys; prefix=sys.argv[1]; rest=sys.argv[2:]; "
            "sys.prefix=prefix; sys.argv=['-', *rest]; "
            "exec(compile(sys.stdin.read(), '<stdin>', 'exec'))"
        )
        + f" {shlex.quote(str((attempt / 'env').resolve()))} \"$@\"\n"
        "fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    environment_python.chmod(0o755)
    launcher = env_bin / "boltzgen-wsl-sm120"
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    wheelhouse = attempt / "wheelhouse"
    source_distributions = attempt / "source_distributions"
    wheelhouse.mkdir()
    source_distributions.mkdir()
    wheel = wheelhouse / "fixture-1.0-py3-none-any.whl"
    wheel.write_bytes(b"fixture wheel payload\n")
    sdist = source_distributions / "fixture-1.0.tar.gz"
    sdist.write_bytes(b"fixture source distribution payload\n")
    wheel_manifest = attempt / "wheelhouse.SHA256SUMS"
    source_manifest = attempt / "source_distributions.SHA256SUMS"
    write_manifest(wheelhouse, wheel_manifest, [wheel])
    write_manifest(source_distributions, source_manifest, [sdist])

    freeze = attempt / "pip_freeze.production.txt"
    freeze.write_text(live_freeze(), encoding="utf-8")
    clean_freeze = attempt / "pip_freeze.clean_rebuild.txt"
    clean_freeze.write_text(
        freeze.read_text(encoding="utf-8")
        + ("fixture-drift==1\n" if clean_freeze_drift else ""),
        encoding="utf-8",
    )
    pip_check_text = "fixture dependency conflict\n" if bad_pip_check else "No broken requirements found.\n"
    pip_checks: list[Path] = []
    for name in PIP_CHECK_NAMES:
        path = attempt / name
        path.write_text(pip_check_text, encoding="utf-8")
        pip_checks.append(path)

    inventory = {
        "schema_version": "WSL2_CU128_BLACKWELL_GPU_INVENTORY_V4",
        "environment_status": "ENGINEERING_COMPATIBILITY_ONLY",
        "formal_g1": False,
        "os_id": "ubuntu",
        "os_version_id": "24.04",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "kernel_release": platform.release(),
        "gpu": torch.cuda.get_device_name(0),
        "driver_version": live_driver(),
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "compute_capability": list(torch.cuda.get_device_capability(0)),
        "multiprocessor_count": torch.cuda.get_device_properties(0).multi_processor_count,
        "torch_arch_list": torch.cuda.get_arch_list(),
        "bf16_supported": torch.cuda.is_bf16_supported(),
        "platform_class": "LINUX_NVIDIA",
        "virtualization_class": "WSL2",
    }
    if inventory_overrides:
        inventory.update(inventory_overrides)
    inventory_path = attempt / "gpu_inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    recursive = attempt / "recursive_payload.SHA256SUMS"
    recursive_payload = [
        inventory_path,
        freeze,
        clean_freeze,
        *pip_checks,
        wheel_manifest,
        source_manifest,
        wheel,
        sdist,
    ]
    write_manifest(attempt, recursive, recursive_payload)
    outputs = attempt / "outputs.SHA256SUMS"
    output_payload = [
        inventory_path,
        freeze,
        clean_freeze,
        *pip_checks,
        wheel_manifest,
        source_manifest,
        recursive,
    ]
    write_manifest(attempt, outputs, output_payload)

    artifact_sha256 = {
        "gpu_inventory.json": sha256(inventory_path),
        "pip_freeze.production.txt": sha256(freeze),
        "pip_freeze.clean_rebuild.txt": sha256(clean_freeze),
        "wheelhouse.SHA256SUMS": sha256(wheel_manifest),
        "source_distributions.SHA256SUMS": sha256(source_manifest),
        "recursive_payload.SHA256SUMS": sha256(recursive),
        **{path.name: sha256(path) for path in pip_checks},
    }
    receipt = attempt / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4",
                "attempt_id": "attempt_004",
                "status": "ENGINEERING_COMPATIBILITY_ONLY",
                "exit_code": 0,
                "failure_codes": [],
                "failure_stage": None,
                "formal_g1": False,
                "environment_contract_revision_required": True,
                "compatibility_activation": "EXPLICIT_PROCESS_LOCAL_ONLY",
                "outputs_manifest_sha256": sha256(outputs),
                "recursive_payload_manifest_sha256": sha256(recursive),
                "official_contract": {
                    "boltzgen": "0.3.2",
                    "cuequivariance": "0.6.1",
                    "torch": "2.8.0+cu128",
                    "torch_cuda": "12.8",
                    "triton": "3.4.0",
                },
                "artifact_sha256": artifact_sha256,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    bindings = {
        "outputs_manifest": {"path": str(outputs.resolve()), "sha256": sha256(outputs)},
        "recursive_payload_manifest": {
            "path": str(recursive.resolve()), "sha256": sha256(recursive),
        },
        "wheelhouse_manifest": {
            "path": str(wheel_manifest.resolve()), "sha256": sha256(wheel_manifest),
        },
        "source_distributions_manifest": {
            "path": str(source_manifest.resolve()), "sha256": sha256(source_manifest),
        },
        "production_freeze": {"path": str(freeze.resolve()), "sha256": sha256(freeze)},
        "clean_rebuild_freeze": {
            "path": str(clean_freeze.resolve()), "sha256": sha256(clean_freeze),
        },
        "gpu_inventory": {
            "path": str(inventory_path.resolve()), "sha256": sha256(inventory_path),
        },
        **{
            path.name.removesuffix(".txt"): {
                "path": str(path.resolve()), "sha256": sha256(path),
            }
            for path in pip_checks
        },
    }
    contract = contract_dir / "environment_contract.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": "WSL2_GPU_STAGE_ENVIRONMENT_CONTRACT_V1",
                "contract_id": "fixture-cu128-engineering-v1",
                "stage_class": stage_class,
                "executor_uid": os.getuid(),
                "environment_attempt_root": str(attempt.resolve()),
                "environment_subdir": "env",
                "environment_receipt_path": str(receipt.resolve()),
                "environment_receipt_sha256": sha256(receipt),
                "expected_status": "ENGINEERING_COMPATIBILITY_ONLY",
                "expected_formal_g1": False,
                "expected_inventory": {
                    "os_id": "ubuntu",
                    "os_version_id": "24.04",
                    "machine": platform.machine(),
                    "gpu": torch.cuda.get_device_name(0),
                    "torch": "2.8.0+cu128",
                    "torch_cuda": "12.8",
                    "compute_capability": [12, 0],
                    "bf16_supported": True,
                },
                "artifact_bindings": bindings,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return bg_work


def run_guard(bg_work: Path, stage: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "CUDA_VISIBLE_DEVICES"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        ["bash", str(bg_work / "verify_gpu_env_stage.sh"), str(bg_work), stage],
        text=True, capture_output=True, env=env, check=False,
    )


def load_contract(bg_work: Path) -> tuple[Path, dict[str, object]]:
    path = bg_work / "contract" / "environment_contract.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def write_contract(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def promote_fixture_to_formal(bg_work: Path) -> tuple[Path, Path]:
    contract_path, contract = load_contract(bg_work)
    attempt = Path(contract["environment_attempt_root"])
    inventory_path = attempt / "gpu_inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["environment_status"] = "G1_PASS"
    inventory["formal_g1"] = True
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    recursive = attempt / "recursive_payload.SHA256SUMS"
    recursive_members = [
        attempt / line.split("  ", 1)[1]
        for line in recursive.read_text(encoding="utf-8").splitlines()
    ]
    write_manifest(attempt, recursive, recursive_members)
    outputs = attempt / "outputs.SHA256SUMS"
    output_members = [
        attempt / line.split("  ", 1)[1]
        for line in outputs.read_text(encoding="utf-8").splitlines()
    ]
    write_manifest(attempt, outputs, output_members)

    receipt = attempt / "receipt.json"
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_payload.update(
        {
            "schema_version": "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1",
            "status": "G1_PASS",
            "formal_g1": True,
            "environment_contract_revision": (
                "WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V1"
            ),
            "environment_contract_revision_required": False,
            "environment_manifest_sha256": sha256(recursive),
            "outputs_manifest_sha256": sha256(outputs),
            "recursive_payload_manifest_sha256": sha256(recursive),
        }
    )
    for name in tuple(receipt_payload["artifact_sha256"]):
        path = attempt / name
        if path.is_file():
            receipt_payload["artifact_sha256"][name] = sha256(path)
    receipt.write_text(
        json.dumps(receipt_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    contract.update(
        {
            "contract_id": "fixture-cu128-formal-v1",
            "stage_class": "FORMAL",
            "environment_receipt_sha256": sha256(receipt),
            "expected_status": "G1_PASS",
            "expected_formal_g1": True,
        }
    )
    for label, path in (
        ("gpu_inventory", inventory_path),
        ("recursive_payload_manifest", recursive),
        ("outputs_manifest", outputs),
    ):
        contract["artifact_bindings"][label]["sha256"] = sha256(path)
    write_contract(contract_path, contract)
    return receipt, contract_path


def test_engineering_stage_revalidates_and_atomically_reuses_audit(tmp_path: Path) -> None:
    bg_work = make_fixture(tmp_path)
    result = run_guard(bg_work, "engineering_smoke_fixture")
    assert result.returncode == 0, result.stderr
    audit = bg_work / "stage_audits" / "engineering_smoke_fixture"
    manifest = audit / "stage_environment.SHA256SUMS"
    assert manifest.is_file()
    verification = json.loads((audit / "verification.json").read_text(encoding="utf-8"))
    assert verification["status"] == "PASS"
    assert verification["environment_status"] == "ENGINEERING_COMPATIBILITY_ONLY"
    assert verification["formal_g1"] is False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        assert sha256(audit / relative) == digest
        assert relative != "stage_environment.SHA256SUMS"
    first = {
        path.relative_to(audit): (path.stat().st_mtime_ns, sha256(path))
        for path in audit.rglob("*") if path.is_file()
    }
    result = run_guard(bg_work, "engineering_smoke_fixture")
    assert result.returncode == 0, result.stderr
    second = {
        path.relative_to(audit): (path.stat().st_mtime_ns, sha256(path))
        for path in audit.rglob("*") if path.is_file()
    }
    assert first == second
    assert not list((bg_work / "stage_audits").glob(".engineering_smoke_fixture.tmp.*"))


def test_rejects_formal_stage_on_candidate_receipt_and_unsafe_stage_id(tmp_path: Path) -> None:
    bg_work = make_fixture(tmp_path, stage_class="FORMAL")
    result = run_guard(bg_work, "formal_g2_fixture")
    assert result.returncode != 0
    assert "formal_g1" in result.stderr or "FORMAL" in result.stderr
    assert not (bg_work / "stage_audits" / "formal_g2_fixture").exists()
    result = run_guard(bg_work, "../escape")
    assert result.returncode != 0


def test_verify_rejects_pseudoformal_revision_before_stage_execution(tmp_path: Path) -> None:
    bg_work = make_fixture(tmp_path)
    receipt, contract_path = promote_fixture_to_formal(bg_work)
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_payload["environment_contract_revision"] = "ENGINEERING_CANDIDATE_V4"
    receipt.write_text(
        json.dumps(receipt_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["environment_receipt_sha256"] = sha256(receipt)
    write_contract(contract_path, contract)

    result = run_guard(bg_work, "pseudoformal_revision")

    assert result.returncode != 0
    assert "revision" in result.stderr
    assert not (bg_work / "stage_audits/pseudoformal_revision").exists()


def test_contract_binds_exact_receipt_path_hash_and_required_manifests(tmp_path: Path) -> None:
    bg_work = make_fixture(tmp_path)
    contract_path, contract = load_contract(bg_work)
    contract["environment_receipt_sha256"] = "0" * 64
    write_contract(contract_path, contract)
    result = run_guard(bg_work, "bad_receipt_hash")
    assert result.returncode != 0
    assert not (bg_work / "stage_audits" / "bad_receipt_hash").exists()

    bg_work = make_fixture(tmp_path / "copied")
    contract_path, contract = load_contract(bg_work)
    source = Path(str(contract["environment_receipt_path"]))
    copied = source.with_name("receipt.copy.json")
    copied.write_bytes(source.read_bytes())
    contract["environment_receipt_path"] = str(copied)
    write_contract(contract_path, contract)
    result = run_guard(bg_work, "copied_receipt")
    assert result.returncode != 0

    bg_work = make_fixture(tmp_path / "manifest")
    contract_path, contract = load_contract(bg_work)
    bindings = contract["artifact_bindings"]
    assert isinstance(bindings, dict)
    bindings.pop("source_distributions_manifest")
    write_contract(contract_path, contract)
    result = run_guard(bg_work, "missing_manifest_binding")
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("fixture_kwargs", "stage_id"),
    [
        ({"clean_freeze_drift": True}, "freeze_drift"),
        ({"bad_pip_check": True}, "pip_check_drift"),
        ({"inventory_overrides": {"torch_cuda": "0.0"}}, "inventory_live_drift"),
    ],
)
def test_rejects_freeze_pip_check_or_live_inventory_drift(
    tmp_path: Path,
    fixture_kwargs: dict[str, object],
    stage_id: str,
) -> None:
    bg_work = make_fixture(tmp_path, **fixture_kwargs)
    result = run_guard(bg_work, stage_id)
    assert result.returncode != 0
    assert not (bg_work / "stage_audits" / stage_id).exists()


def test_manifest_payload_tamper_fails_and_existing_difference_is_never_overwritten(
    tmp_path: Path,
) -> None:
    bg_work = make_fixture(tmp_path)
    _, contract = load_contract(bg_work)
    attempt = Path(str(contract["environment_attempt_root"]))
    (attempt / "wheelhouse" / "fixture-1.0-py3-none-any.whl").write_bytes(b"tampered\n")
    result = run_guard(bg_work, "wheel_tamper")
    assert result.returncode != 0
    assert not (bg_work / "stage_audits" / "wheel_tamper").exists()

    bg_work = make_fixture(tmp_path / "existing")
    audit = bg_work / "stage_audits" / "preexisting_difference"
    audit.mkdir(parents=True)
    marker = audit / "marker.txt"
    marker.write_text("do not overwrite\n", encoding="utf-8")
    before = marker.read_bytes()
    result = run_guard(bg_work, "preexisting_difference")
    assert result.returncode != 0
    assert marker.read_bytes() == before
    assert sorted(path.name for path in audit.iterdir()) == ["marker.txt"]


def test_runtime_script_tamper_is_rejected_before_stage_audit(tmp_path: Path) -> None:
    bg_work = make_fixture(tmp_path)
    with (bg_work / "status_local_cell.sh").open("ab") as handle:
        handle.write(b"tampered\n")

    result = run_guard(bg_work, "runtime_tamper")

    assert result.returncode != 0
    assert not (bg_work / "stage_audits" / "runtime_tamper").exists()


def test_rejects_polluted_cuda_visible_devices(tmp_path: Path) -> None:
    bg_work = make_fixture(tmp_path)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
        env.pop(key, None)
    env["CUDA_VISIBLE_DEVICES"] = "0"

    result = subprocess.run(
        ["bash", str(bg_work / "verify_gpu_env_stage.sh"), str(bg_work), "polluted_cuda"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert not (bg_work / "stage_audits" / "polluted_cuda").exists()
