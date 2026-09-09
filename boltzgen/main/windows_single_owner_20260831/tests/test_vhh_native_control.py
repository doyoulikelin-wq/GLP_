"""Fast CPU-only native calibration tests; no network or inference in tests."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREP = load("prepare_vhh_native_control")
RUN = load("run_vhh_native_control")


def reference():
    return np.random.default_rng(8).normal(size=(254, 3))


def test_kabsch_self_and_global_rigid_transform():
    ref = reference()
    rotation = np.array([[0., 1., 0.], [-1., 0., 0.], [0., 0., 1.]])
    assert PREP.aligned_binder_rmsd(ref, ref, 129) < 1e-10
    assert PREP.aligned_binder_rmsd(ref @ rotation + 20, ref, 129) < 1e-10


def test_kabsch_detects_binder_only_displacement():
    ref = reference()
    shifted = ref.copy()
    shifted[129:] += [100, 0, 0]
    assert PREP.aligned_binder_rmsd(shifted, ref, 129) == pytest.approx(100)


@pytest.mark.parametrize("n", [0, 2, 254])
def test_bad_target_alignment_count(n):
    with pytest.raises(ValueError):
        PREP.aligned_binder_rmsd(reference(), reference(), n)


def test_nan_and_degenerate_fail_closed():
    bad = reference()
    bad[0, 0] = np.nan
    with pytest.raises(ValueError):
        PREP.aligned_binder_rmsd(bad, reference(), 129)
    with pytest.raises(ValueError):
        PREP.aligned_binder_rmsd(np.zeros((254, 3)), reference(), 129)


def test_contacts_deduplicate_atom_pairs_and_exclude_ineligible_atoms():
    xyz = np.array([[0, 0, 0], [0, 0, .1], [3, 0, 0], [0, 1, 0], [50, 0, 0]])
    token = np.array([0, 0, 129, 130, 131])
    assert PREP.heavy_residue_contacts(xyz, token, np.array([1, 1, 1, 0, 1]), 129) == {(0, 129)}


def test_control_config_is_free_refold_not_design(tmp_path):
    config = PREP.control_config(tmp_path / "input", tmp_path / "runtime")
    assert config["diffusion_samples"] == 2
    assert config["data"]["_target_"].endswith(".FromGeneratedDataModule")
    assert config["data"]["target_templates"] is True
    assert config["data"]["design_mask_templates"] is False
    assert config["data"]["return_native"] is False
    assert "expected_cdr_tokens" not in config["data"]
    assert config["override"] == {"validators": None, "use_kernels": True}


def fixture_outputs(root, shifted_sample=False, nan=False, remap=False):
    ref = reference()
    designs = root / "intermediate_designs"
    folds, cifs = designs / "fold_out_npz", designs / "refold_cif"
    folds.mkdir(parents=True)
    cifs.mkdir()
    (cifs / "native6jb8.cif").write_text("data_synthetic_test\n")
    (root / "scoring_only").mkdir()
    mapping = np.eye(254, dtype=np.int8)
    pairs = PREP.heavy_residue_contacts(ref, np.arange(254), np.ones(254), 129)
    np.savez(root / "scoring_only" / "atom_mapping.npz", ca_atom_indices=np.arange(254),
             reference_ca=ref, atom_to_token=mapping, atom_pad_mask=np.ones(254),
             contact_heavy_mask=np.ones(254), native_residue_contact_pairs=np.array(sorted(pairs)))
    coords = np.stack([ref, ref])
    if shifted_sample:
        coords[1, 129:] += [100, 0, 0]
    if nan:
        coords[0, 0, 0] = np.nan
    if remap:
        mapping[[0, 1]] = mapping[[1, 0]]
    np.savez(folds / "native6jb8.npz", coords=coords, atom_to_token=mapping[None],
             **{key: np.ones(2) for key in ("iptm", "ptm", "design_to_target_iptm", "design_ptm")})


def test_native_recovery_requires_both_samples(tmp_path):
    fixture_outputs(tmp_path, shifted_sample=True)
    result = RUN.score_outputs(tmp_path)
    assert result["samples"][0]["native_heavy_residue_contact_recall"] == 1
    assert result["samples"][1]["native_heavy_residue_contact_recall"] == 0
    assert result["native_interface_recovered"] is False


def test_two_native_samples_recover(tmp_path):
    fixture_outputs(tmp_path)
    assert RUN.score_outputs(tmp_path)["native_interface_recovered"] is True


@pytest.mark.parametrize("issue", ["nan", "remap"])
def test_invalid_fold_rejected(tmp_path, issue):
    fixture_outputs(tmp_path, **{issue: True})
    with pytest.raises(RUN.RunFailure):
        RUN.score_outputs(tmp_path)


def test_digest_order_independent():
    assert RUN.canonical_digest({"a": 1, "b": 2}) == RUN.canonical_digest({"b": 2, "a": 1})


def test_preparation_refuses_existing_output_without_network(tmp_path):
    with pytest.raises(ValueError):
        PREP.prepare(tmp_path, tmp_path)


@pytest.mark.parametrize("gate", [{"status": "BLOCKED", "next_stage": "existing_fold_reassessment"},
                                  {"status": "READY", "next_stage": "existing_fold_reassessment"},
                                  {"status": "COMPLETE", "next_stage": None}])
def test_native_launch_rejects_invalid_prerequisite_gate(monkeypatch, gate):
    monkeypatch.setattr(RUN, "evaluate_stage_gate", lambda *args: gate)
    with pytest.raises(RUN.RunFailure):
        RUN.require_native_ready({}, {}, {})


def test_native_launch_accepts_only_next_ready_stage(monkeypatch):
    gate = {"status": "READY", "next_stage": "native_free_refold"}
    monkeypatch.setattr(RUN, "evaluate_stage_gate", lambda *args: gate)
    assert RUN.require_native_ready({}, {}, {}) == gate
