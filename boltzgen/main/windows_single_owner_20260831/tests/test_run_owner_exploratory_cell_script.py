import json
from pathlib import Path
import subprocess
import sys


SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "run_owner_exploratory_cell.sh"
)
PERSONAL_SCRIPT = (
    Path(__file__).parents[2]
    / "windows_gpu_handoff_20260829"
    / "scripts"
    / "personal"
    / "start_personal_vhh_inference.sh"
)


def test_script_syntax_and_help() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "WORKSPACE_ROOT CELL_ID SPEC CHECKPOINT NUM_DESIGNS" in result.stdout


def test_script_rejects_invalid_checkpoint_before_creating_a_run() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "/tmp", "safe_cell", "/tmp/spec", "invalid", "2"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert "CHECKPOINT must be adherence or diverse" in result.stderr


def test_script_keeps_owner_mode_execution_safety_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--reuse" not in text
    assert 'exec 9<"/run/user/$(id -u)"' in text
    assert text.index("flock -n 9") < text.index("gpu_compute_processes_before.csv")
    assert "runtime_assets_before.txt" in text
    assert "spec_bundle_before.SHA256SUMS" in text
    assert "resolved_config.SHA256SUMS" in text
    finalizer = text[text.index("finalize() {") :]
    assert finalizer.index("terminal_input_code=0") < finalizer.index(
        "if mark_terminal_success; then"
    )
    assert finalizer.index("if mark_terminal_success; then") < finalizer.index(
        "if ! seal_output_manifest; then"
    )
    sealer = text[text.index("seal_output_manifest() {") : text.index("write_terminal_files() {")]
    assert sealer.index(".OUTPUT_SHA256SUMS.tmp >/dev/null") < sealer.index(
        'mv -fT -- "$temporary" "$manifest"'
    )
    assert "TERMINAL_INPUT_REVALIDATION_FAILED" in text
    assert "OUTPUT_MANIFEST_SEAL_FAILED" in text
    assert "validate_cell_output.py" in text
    assert "EXPECTED_FOLD_SAMPLES=5" in text
    assert "resolved_config_contract.json" in text
    assert 'design_paths != ["target.cif", "scaffold.yaml"]' in text
    assert 'scaffold.get("path") != "scaffold.cif"' in text
    for contract_item in (
        "design.yaml_path",
        "design.use_kernels",
        "inverse.use_kernels",
        "folding.use_kernels",
        "inverse.devices",
        "design.num_workers",
        "inverse.num_workers",
        "folding.num_workers",
        "analysis.num_workers",
        "analysis.liability_modality",
        "filtering.modality",
    ):
        assert contract_item in text
    for stage in ("design", "inverse_folding", "folding", "analysis", "filtering"):
        assert stage in text


def test_all_gpu_entry_points_share_the_canonical_lock() -> None:
    subprocess.run(["bash", "-n", str(PERSONAL_SCRIPT)], check=True)
    text = PERSONAL_SCRIPT.read_text(encoding="utf-8")
    assert 'exec 8<"/run/user/$(id -u)"' in text
    assert text.index("flock -n 8") < text.index("personal_inference.lock")


def _manifest_validator_source() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    marker = 'if ! python3 -I -S - "$attempt_root" "$temporary" <<\'PY\'\n'
    start = text.index(marker) + len(marker)
    return text[start : text.index("\nPY\n", start)]


def _write_complete_attempt(root: Path) -> Path:
    logs = root / "operator_logs"
    logs.mkdir(parents=True)
    plain_files = {
        "STATUS.txt": "EXPLORATORY_INFERENCE_COMPLETE\n",
        "exit_code.txt": "0\n",
        "started_at_utc.txt": "2026-08-31T00:00:00Z\n",
        "ended_at_utc.txt": "2026-08-31T00:01:00Z\n",
        "runtime_assets_used.SHA256SUMS": "placeholder\n",
        "spec_bundle_before.SHA256SUMS": "placeholder\n",
        "gpu_monitor_wait_exit_code.txt": "143\n",
        "gpu_after.exit_code.txt": "0\n",
        "gpu_compute_processes_after.exit_code.txt": "0\n",
        "gpu_compute_processes_after.csv": "",
        "disk_after.exit_code.txt": "0\n",
        "cuda_oom_detected.txt": "false\n",
    }
    for name, content in plain_files.items():
        (logs / name).write_text(content, encoding="utf-8")
    for stage in ("design", "inverse_folding", "folding", "analysis", "filtering", "validation"):
        (logs / f"{stage}.exit_code.txt").write_text("0\n", encoding="utf-8")
    receipt = {
        "status": "EXPLORATORY_INFERENCE_COMPLETE",
        "exit_code": 0,
        "cuda_oom_detected": False,
        "expected_designs": 2,
        "observed_designs": 2,
        "fold_samples_per_candidate": 5,
        "output_validation": {"status": "PASS"},
        "resolved_config_contract": {"status": "PASS"},
    }
    (logs / "EXPLORATORY_INFERENCE.json").write_text(
        json.dumps(receipt) + "\n", encoding="utf-8"
    )
    return logs


def _run_manifest_validator(root: Path) -> subprocess.CompletedProcess[str]:
    output = root / "operator_logs/.OUTPUT_SHA256SUMS.tmp"
    return subprocess.run(
        [sys.executable, "-I", "-S", "-", str(root), str(output)],
        input=_manifest_validator_source(),
        capture_output=True,
        text=True,
    )


def test_manifest_validator_accepts_only_semantically_complete_receipt(tmp_path: Path) -> None:
    logs = _write_complete_attempt(tmp_path)
    result = _run_manifest_validator(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (logs / ".OUTPUT_SHA256SUMS.tmp").is_file()


def test_manifest_validator_rejects_missing_or_non_file_terminal_member(tmp_path: Path) -> None:
    logs = _write_complete_attempt(tmp_path)
    (logs / "exit_code.txt").unlink()
    (logs / "exit_code.txt").mkdir()
    result = _run_manifest_validator(tmp_path)
    assert result.returncode != 0
    assert "required output members missing" in result.stderr
    assert not (logs / ".OUTPUT_SHA256SUMS.tmp").exists()
