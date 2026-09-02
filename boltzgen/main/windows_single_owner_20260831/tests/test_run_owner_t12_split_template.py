"""CPU tests for the bounded T12 split-template GPU runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_owner_t12_split_template.py"
SPEC = importlib.util.spec_from_file_location("run_owner_t12_split_template", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write_candidate(root: Path, candidate: str) -> None:
    """Write a minimal valid 151-token/604-atom synthetic fold result."""
    (root / f"{candidate}.cif").write_text("data_test\n", encoding="utf-8")
    design_mask = np.zeros(151, dtype=np.int8)
    design_mask[55:63] = 1
    design_mask[80:87] = 1
    design_mask[125:140] = 1
    np.savez(root / f"{candidate}.npz", design_mask=design_mask)

    token = np.repeat(np.arange(151), 4)
    atom_count = token.size + 4
    atom_to_token = np.zeros((1, atom_count, 151), dtype=np.int8)
    atom_to_token[0, np.arange(token.size), token] = 1
    coords = np.zeros((5, atom_count, 3), dtype=np.float32)
    active = np.zeros((1, atom_count), dtype=np.int8)
    active[:, : token.size] = 1
    payload = {
        "coords": coords,
        "input_coords": np.zeros((1, 1, atom_count, 3), dtype=np.float32),
        "atom_to_token": atom_to_token,
        "atom_resolved_mask": active,
        "backbone_mask": active,
    }
    for key in MODULE.METRIC_KEYS:
        payload[key] = np.zeros(5, dtype=np.float32)
    np.savez(root / "fold_out_npz" / f"{candidate}.npz", **payload)
    (root / "refold_cif" / f"{candidate}.cif").write_text(
        "data_refold\n", encoding="utf-8"
    )


def _valid_tree(tmp_path: Path) -> Path:
    root = tmp_path / "intermediate_designs"
    root.mkdir()
    (root / "fold_out_npz").mkdir()
    (root / "refold_cif").mkdir()
    for candidate in MODULE.CANDIDATE_IDS:
        _write_candidate(root, candidate)
    return root


def test_folding_config_is_split_template_only(tmp_path: Path) -> None:
    """The generated config binds the two-slot adapter and frozen budget."""
    config = MODULE.build_folding_config(tmp_path / "designs", tmp_path / "runtime")
    checks = MODULE.validate_config(config)
    assert all(checks.values())
    assert config["data"]["_target_"] == (
        "owner_split_template_data.SplitTemplateFromGeneratedDataModule"
    )
    assert config["diffusion_samples"] == 5
    assert config["data"]["design_mask_templates"] is False


def test_validate_fold_outputs_accepts_exact_6_by_5(tmp_path: Path) -> None:
    """The output validator accepts exactly six candidates and thirty samples."""
    result = MODULE.validate_fold_outputs(_valid_tree(tmp_path))
    assert result["status"] == "PASS"
    assert result["candidate_count"] == 6
    assert result["fold_sample_count"] == 30
    assert all(row["fold_samples"] == 5 for row in result["candidates"])


def test_validate_fold_outputs_rejects_missing_candidate(tmp_path: Path) -> None:
    """A missing fold NPZ is a terminal closure failure."""
    root = _valid_tree(tmp_path)
    (root / "fold_out_npz" / "design_5.npz").unlink()
    with pytest.raises(MODULE.RunFailure, match="closure mismatch"):
        MODULE.validate_fold_outputs(root)


def test_validate_fold_outputs_rejects_nonfinite_metric(tmp_path: Path) -> None:
    """NaN in any required per-sample metric fails closed."""
    root = _valid_tree(tmp_path)
    path = root / "fold_out_npz" / "design_0.npz"
    with np.load(path, allow_pickle=False) as source:
        payload = {key: np.asarray(source[key]) for key in source.files}
    payload["ptm"][0] = np.nan
    np.savez(path, **payload)
    with pytest.raises(MODULE.RunFailure, match="metric contract mismatch"):
        MODULE.validate_fold_outputs(root)
