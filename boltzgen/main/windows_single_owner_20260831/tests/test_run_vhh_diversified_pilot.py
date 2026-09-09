"""CPU-only tests for the bounded two-scaffold pilot wrapper."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_vhh_diversified_pilot.py"
SPEC = importlib.util.spec_from_file_location("run_vhh_diversified_pilot", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture_spec(root, ranges="26..33,51..57,96..110"):
    """Create a synthetic four-file source bundle, without coordinates or a GPU."""
    directory = root / "scaffold"
    directory.mkdir()
    for name in MODULE.SPEC_MEMBERS:
        (directory / name).write_text("dummy")
    (directory / "scaffold.yaml").write_text(yaml.safe_dump({"design": [{"chain": {"res_index": ranges}}]}))
    return directory


def test_actual_cdr3_length_not_scaffold_label(tmp_path):
    fixture_spec(tmp_path)
    result = MODULE.spec_record(tmp_path, "scaffold", "pilot01")
    assert result["cdr3_length"] == 15
    assert result["num_designs"] == 2
    assert len(result["spec_hashes"]) == 4


@pytest.mark.parametrize("ranges", ["26..33,51..57", "26..33,51..57,110..96", "26..33,51..57,foo"])
def test_ambiguous_or_reversed_annotation_rejected(tmp_path, ranges):
    fixture_spec(tmp_path, ranges)
    with pytest.raises(ValueError):
        MODULE.spec_record(tmp_path, "scaffold", "pilot01")


def test_unexpected_bundle_file_rejected(tmp_path):
    directory = fixture_spec(tmp_path)
    (directory / "unexpected.txt").write_text("x")
    with pytest.raises(ValueError):
        MODULE.spec_record(tmp_path, "scaffold", "pilot01")


@pytest.mark.parametrize("gate", [{"status": "BLOCKED", "next_stage": "native_free_refold"},
                                  {"status": "READY", "next_stage": "native_free_refold"},
                                  {"status": "COMPLETE", "next_stage": None}])
def test_gate_blocks_launch_without_native_pass(monkeypatch, gate):
    monkeypatch.setattr(MODULE, "evaluate", lambda *args: gate)
    with pytest.raises(ValueError):
        MODULE.require_ready({}, {}, [])


def test_gate_allows_only_ready_pilot(monkeypatch):
    gate = {"status": "READY", "next_stage": "diversified_pilot"}
    monkeypatch.setattr(MODULE, "evaluate", lambda *args: gate)
    assert MODULE.require_ready({}, {}, []) == gate


@pytest.mark.parametrize("status", [" M tracked.py\n", "?? new_script.py\n"])
def test_dirty_repository_rejected_before_any_output(monkeypatch, tmp_path, status):
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=status, stderr=""))
    output = tmp_path / "must_not_be_created"
    args = SimpleNamespace(workspace=tmp_path, repo_root=tmp_path, output=output)
    with pytest.raises(ValueError, match="repository must be clean"):
        MODULE.run(args)
    assert not output.exists()


def test_git_status_failure_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=128, stdout="", stderr="not a repository"))
    with pytest.raises(ValueError, match="cannot verify"):
        MODULE.ensure_clean_repository(tmp_path)


def test_clean_git_status_accepted(monkeypatch, tmp_path):
    def mocked_run(command, **kwargs):
        assert command == ["git", "status", "--porcelain"]
        assert kwargs["cwd"] == tmp_path
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(MODULE.subprocess, "run", mocked_run)
    assert MODULE.ensure_clean_repository(tmp_path) is None
