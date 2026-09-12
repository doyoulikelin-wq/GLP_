"""CPU-only atom-contract and fail-closed prerequisite tests for 9NK9 control."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1]/"scripts/run_vhh_short_peptide_control.py"
SPEC = importlib.util.spec_from_file_location("run_vhh_short_peptide_control", SCRIPT)
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
P = R.P


def dump(path, data):
    path.write_text(json.dumps(data))
    return R.binding(path)


def receipt_fixture(tmp_path, monkeypatch):
    prep = {"calibration_thresholds": P.CALIBRATION_THRESHOLDS, "prepared_at_utc": "2026-09-12T12:00:00Z"}
    result = {"protocol_id": P.PROTOCOL_ID, "thresholds": P.CALIBRATION_THRESHOLDS,
              "strict_full_canonical_heavy_atom_validation": True, "target_tokens": 10, "binder_tokens": 118,
              "samples": [{"sample_index": i, "target_aligned_binder_ca_rmsd_angstrom": 3.0,
                           "native_heavy_residue_contact_recall": 0.7} for i in range(2)]}
    receipt = {"schema": "VHH_SHORT_PEPTIDE_CONTROL_RUN_V1", "protocol_id": P.PROTOCOL_ID,
               "status": "COMPLETED", "calibration_status": "PASSED_TWO_OF_TWO", "biological_pass": False,
               "started_at_utc": "2026-09-12T12:01:00Z", "finished_at_utc": "2026-09-12T12:02:00Z",
               "checks": {"weights_unchanged": True}, "actual": {"fold_samples": 2, "wall_seconds": 60, "execution_device": "cuda"},
               "preparation": dump(tmp_path/"PREPARATION.json", prep),
               "calibration_result": dump(tmp_path/"CALIBRATION_RESULT.json", result)}
    monkeypatch.setattr(R, "check_preparation", lambda path: (prep, {}))
    path = tmp_path/"RUN.json"
    dump(path, receipt)
    return path, receipt, result


def test_measured_two_sample_prerequisite_passes(tmp_path, monkeypatch):
    path, _, _ = receipt_fixture(tmp_path, monkeypatch)
    assert R.validate_calibration_receipt(path)["status"] == "COMPLETED"


@pytest.mark.parametrize("change", ["one_sample", "rmsd", "recall", "nan", "bool", "different_threshold", "different_target", "unresolved"])
def test_label_cannot_override_missing_or_failed_measurements(tmp_path, monkeypatch, change):
    path, receipt, result = receipt_fixture(tmp_path, monkeypatch)
    if change == "one_sample": result["samples"].pop()
    elif change in ("rmsd", "nan", "bool"):
        result["samples"][1]["target_aligned_binder_ca_rmsd_angstrom"] = {"rmsd": 5.1, "nan": float("nan"), "bool": True}[change]
    elif change == "recall": result["samples"][1]["native_heavy_residue_contact_recall"] = 0.29
    elif change == "different_threshold": result["thresholds"] = {}
    elif change == "different_target": result["target_tokens"] = 9
    else: result["strict_full_canonical_heavy_atom_validation"] = False
    receipt["calibration_result"] = dump(tmp_path/"CALIBRATION_RESULT.json", result)
    dump(path, receipt)
    with pytest.raises(ValueError): R.validate_calibration_receipt(path)


@pytest.mark.parametrize("change", ["changed_artifact", "weights", "overtime", "cpu"])
def test_provenance_and_execution_contract_fail_closed(tmp_path, monkeypatch, change):
    path, receipt, result = receipt_fixture(tmp_path, monkeypatch)
    if change == "changed_artifact": dump(tmp_path/"CALIBRATION_RESULT.json", {})
    elif change == "weights": receipt["checks"]["weights_unchanged"] = False
    elif change == "overtime": receipt["actual"]["wall_seconds"] = 900.001
    else: receipt["actual"]["execution_device"] = "cpu"
    dump(path, receipt)
    with pytest.raises(ValueError): R.validate_calibration_receipt(path)


def feature_fixture():
    n = 128
    data = {"atom_to_token": np.repeat(np.eye(n), 5, axis=0),
            "atom_resolved_mask": np.ones(n*5), "token_index": np.arange(n),
            "res_type": np.eye(33)[np.full(n, 2)], "mol_type": np.zeros(n),
            "coords": np.random.default_rng(2).normal(size=(1, n*5, 3)),
            "design_mask": np.r_[np.zeros(10), np.ones(118)]}
    return data


@pytest.mark.parametrize("change", [None, "missing_atom", "bad_token", "nan"])
def test_actual_feature_atom_contract(change):
    data = feature_fixture()
    if change == "missing_atom": data["atom_resolved_mask"][70] = 0
    if change == "bad_token": data["token_index"][4] = 5
    if change == "nan": data["coords"][0, 3, 0] = np.nan
    sample = {k: SimpleNamespace(numpy=lambda v=v: v) for k, v in data.items()}
    if change:
        with pytest.raises(P.E.ValidationError): P.evaluator_arrays(sample)
    else:
        _, arrays = P.evaluator_arrays(sample)
        assert arrays["coords"].shape == (1, 640, 3)


def test_dirty_repository_blocks_before_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ensure_clean_repository", lambda root: (_ for _ in ()).throw(ValueError("dirty")))
    args = SimpleNamespace(workspace=tmp_path, repo_root=tmp_path, prepared=tmp_path,
                           output=tmp_path/"new", hard_timeout_seconds=900)
    with pytest.raises(ValueError, match="dirty"): R.run(args)
    assert not args.output.exists()
