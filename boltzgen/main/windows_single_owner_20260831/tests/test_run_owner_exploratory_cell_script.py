from pathlib import Path
import subprocess


SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "run_owner_exploratory_cell.sh"
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
    assert "runtime_assets_after.txt" in text
    assert "spec_bundle_before.SHA256SUMS" in text
    assert "spec_bundle_after.txt" in text
    assert "validate_cell_output.py" in text
    assert "EXPECTED_FOLD_SAMPLES=5" in text
    assert "resolved_config_contract.json" in text
    for stage in ("design", "inverse_folding", "folding", "analysis", "filtering"):
        assert stage in text
