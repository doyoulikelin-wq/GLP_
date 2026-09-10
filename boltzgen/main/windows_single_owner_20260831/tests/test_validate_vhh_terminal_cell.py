"""Optional legacy-column handling preserves all other structural checks."""
import csv
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_vhh_terminal_cell as V


def setup_case(tmp_path, monkeypatch, optional=None, omit_other=False):
    monkeypatch.setenv("EXPECTED_DESIGNS", "2")
    monkeypatch.setenv("EXPECTED_FOLD_SAMPLES", "5")
    root = tmp_path / "cell"
    directory = root / "intermediate_designs_inverse_folded"
    directory.mkdir(parents=True)
    data = {} if omit_other else {"design_ptm": [0.5, 0.6]}
    if optional is not None:
        data[V.OPTIONAL_METRIC] = optional
    pd.DataFrame(data).to_csv(directory / "aggregate_metrics_analyze.csv", index=False)
    module = SimpleNamespace(REQUIRED_ANALYSIS_NUMERIC=("design_ptm", V.OPTIONAL_METRIC),
                             load_yaml_mapping=lambda path: {"filter_bindingsite": False})
    def validate(path):
        df = pd.read_csv(Path(path) / "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv")
        assert set(module.REQUIRED_ANALYSIS_NUMERIC).issubset(df.columns), "missing required metric"
        for name in module.REQUIRED_ANALYSIS_NUMERIC:
            assert np.isfinite(pd.to_numeric(df[name], errors="coerce")).all(), "non-finite metric"
        return {"schema_version": "BASE", "validator_sha256": V.BASE_SHA256, "status": "PASS",
                "observed_unique_ids": 2, "fold_samples_per_candidate": 5}
    module.validate = validate
    monkeypatch.setattr(V, "load_base_validator", lambda: module)
    return root, module


def test_absent_metric_optional_without_filling_zero(tmp_path, monkeypatch):
    root, module = setup_case(tmp_path, monkeypatch)
    before = (root / "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv").read_bytes()
    result = V.validate(str(root))
    assert result["status"] == "PASS"
    assert result["schema_version"] == V.SCHEMA
    assert result["base_validator_sha256"] == V.BASE_SHA256
    assert result["optional_analysis_metric"]["present"] is False
    assert module.REQUIRED_ANALYSIS_NUMERIC == ("design_ptm",)
    assert (root / "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv").read_bytes() == before


def test_present_metric_retains_original_numeric_checks(tmp_path, monkeypatch):
    root, module = setup_case(tmp_path, monkeypatch, [0, 1])
    assert V.validate(str(root))["optional_analysis_metric"]["present"] is True
    assert V.OPTIONAL_METRIC in module.REQUIRED_ANALYSIS_NUMERIC


@pytest.mark.parametrize("values", [[float("nan"), 0], [float("inf"), 0], ["invalid", "0"]])
def test_present_bad_metric_still_fails(tmp_path, monkeypatch, values):
    root, _ = setup_case(tmp_path, monkeypatch, values)
    with pytest.raises(AssertionError, match="non-finite"):
        V.validate(str(root))


def test_other_missing_metric_still_fails(tmp_path, monkeypatch):
    root, _ = setup_case(tmp_path, monkeypatch, [0, 1], omit_other=True)
    with pytest.raises(AssertionError, match="missing required"):
        V.validate(str(root))


def test_common_disabled_filter_required(tmp_path, monkeypatch):
    root, module = setup_case(tmp_path, monkeypatch)
    module.load_yaml_mapping = lambda path: {"filter_bindingsite": True}
    with pytest.raises(ValueError, match="disabled legacy filter"):
        V.validate(str(root))


def test_real_module_fresh_and_hash_bound(monkeypatch):
    first = V.load_base_validator()
    first.REQUIRED_ANALYSIS_NUMERIC = ()
    second = V.load_base_validator()
    assert second.REQUIRED_ANALYSIS_NUMERIC.count(V.OPTIONAL_METRIC) == 1
    monkeypatch.setattr(V, "BASE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="SHA256"):
        V.load_base_validator()


def test_frozen_two_candidates_and_five_folds_only(tmp_path, monkeypatch):
    root, _ = setup_case(tmp_path, monkeypatch)
    monkeypatch.setenv("EXPECTED_DESIGNS", "1")
    with pytest.raises(ValueError, match="EXPECTED_DESIGNS"):
        V.validate(str(root))
