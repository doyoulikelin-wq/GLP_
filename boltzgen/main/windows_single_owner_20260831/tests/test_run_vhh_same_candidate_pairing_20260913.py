"""Test independently calibrated matched-deletion execution gates."""

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_vhh_same_candidate_pairing_20260913 as R


def valid_plan(monkeypatch):
    monkeypatch.setattr(R, "implementations", lambda: {"test": "hash"})
    return {"schema": R.SCHEMA, "created_at_utc": "2026-09-01T00:00:00Z",
        "implementation_sha256": {"test": "hash"}, "candidate_count": 6,
        "fold_sample_count": 24, "hard_timeout_seconds": 1800,
        "old_stage_gate_status": "BLOCKED_UNCHANGED", "automatic_retry": False,
        "biological_pass": False, "pairing_plan": {"path": "/private/pair"},
        "calibration_receipt": {"path": "/private/calibration"}, "candidate_set_sha256": "current"}


def test_requires_real_calibration_validation_not_old_gate(monkeypatch):
    plan = valid_plan(monkeypatch)
    monkeypatch.setattr(R, "bound_json", lambda value: {})
    monkeypatch.setattr(R, "check_blueprint", lambda value: {"candidate_set_sha256": "current"})
    seen = []
    def rejected(path):
        seen.append(path)
        raise ValueError("native sample 1 failed")
    monkeypatch.setattr(R, "validate_calibration_receipt", rejected)
    with pytest.raises(ValueError, match="sample 1"):
        R.check_plan(plan)
    assert seen == [Path("/private/calibration")]


def test_old_candidate_identity_cannot_be_reused(monkeypatch):
    plan = valid_plan(monkeypatch)
    monkeypatch.setattr(R, "bound_json", lambda value: {})
    monkeypatch.setattr(R, "check_blueprint", lambda value: {"candidate_set_sha256": "different"})
    monkeypatch.setattr(R, "validate_calibration_receipt", lambda path: {"status": "COMPLETED"})
    with pytest.raises(ValueError, match="candidate identity"):
        R.check_plan(plan)


@pytest.mark.parametrize("key,value", [("candidate_count", 7), ("fold_sample_count", 25),
    ("hard_timeout_seconds", 1801), ("old_stage_gate_status", "PASS"),
    ("automatic_retry", True), ("biological_pass", True)])
def test_contract_changes_rejected_before_calibration(monkeypatch, key, value):
    plan = valid_plan(monkeypatch)
    plan[key] = value
    with pytest.raises(ValueError, match="execution plan changed"):
        R.check_plan(plan)


def test_no_old_gate_function_import_or_patch():
    source = Path(R.__file__).read_text()
    assert "M.require_ready" not in source
    assert "M.run(" not in source
    assert "setattr" not in source
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in source
    assert "M.score_outputs" in source
