"""CPU tests: strict full-atom compatibility, placeholders and fail-closed inputs."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/preflight_vhh_evaluator_compatibility.py"
SPEC = importlib.util.spec_from_file_location("preflight_vhh_evaluator_compatibility", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def features():
    """Create a complete canonical synthetic GLP1 plus three-residue test binder."""
    residue_ids = np.r_[MODULE.EXPECTED_TARGET_IDS, [2, 9, 13]]
    n = len(residue_ids)
    atom_tokens = np.repeat(np.arange(n), MODULE.E.HEAVY_COUNTS[residue_ids-2])
    a = len(atom_tokens) + 2
    mapping = np.zeros((a, n), dtype=np.int8)
    mapping[np.arange(len(atom_tokens)), atom_tokens] = 1
    active = mapping.sum(axis=1).astype(bool)
    elements = np.zeros((a, 128), dtype=np.int8)
    elements[active, 6] = 1
    design = np.zeros(n, dtype=np.int8)
    design[30] = 1
    return {"coords": np.random.default_rng(4).normal(size=(1, a, 3)),
            "atom_to_token": mapping, "atom_resolved_mask": active.copy(),
            "atom_pad_mask": active, "token_index": np.arange(n),
            "res_type": np.eye(33, dtype=np.int8)[residue_ids], "mol_type": np.zeros(n, dtype=np.int8),
            "design_mask": design, "ref_element": elements,
            "ref_atom_name_chars": np.zeros((a, 4, 64), dtype=np.int8)}


def test_complete_source_passes_actual_strict_evaluator():
    result = MODULE.inspect_features(features())
    assert result["status"] == "PASS"
    assert result["strict_evaluator_source_pass"] is True
    assert result["unresolved_fixed_heavy_atoms"] == 0


@pytest.mark.parametrize("token", [0, 10, 30, 31])
def test_any_unresolved_atom_rejects_full_source_including_cdr_and_backbone(token):
    data = features()
    atom = np.flatnonzero(data["atom_to_token"][:, token])[0]
    data["atom_resolved_mask"][atom] = False
    result = MODULE.inspect_features(data)
    assert result["status"] == "REJECT"
    assert "assigned/resolved mismatch" in result["strict_evaluator_source_error"]
    if token == 31:
        assert result["unresolved_framework_heavy_atoms"] == 1
    elif token < 30:
        assert result["unresolved_target_heavy_atoms"] == 1


def test_generation_cdr_placeholders_are_not_treated_as_completed_candidate_atoms():
    data = features()
    atom = np.flatnonzero(data["atom_to_token"][:, 30])[-1]
    data["atom_resolved_mask"][atom] = False
    generation = MODULE.inspect_features(data, representation="generation_atom14")
    assert generation["status"] == "PASS"
    assert generation["generation_cdr_placeholders_excluded_from_fixed_atom_diagnosis"] is True
    assert generation["strict_evaluator_source_pass"] is False
    assert MODULE.inspect_features(data)["status"] == "REJECT"


def test_generation_still_blocks_unresolved_framework_atoms():
    data = features()
    atom = np.flatnonzero(data["atom_to_token"][:, 32])[-1]
    data["atom_resolved_mask"][atom] = False
    result = MODULE.inspect_features(data, representation="generation_atom14")
    assert result["status"] == "REJECT"
    assert result["unresolved_framework_heavy_atoms"] == 1


@pytest.mark.parametrize("token", [0, 1, 12, 29])
def test_entire_target_identity_checked_not_just_terminal_pair(token):
    data = features()
    data["res_type"][token] = np.eye(33, dtype=np.int8)[21]
    with pytest.raises(MODULE.E.ValidationError, match="complete canonical active GLP1"):
        MODULE.inspect_features(data)


def test_ambiguous_atom_mapping_rejected():
    data = features()
    data["atom_to_token"][0, 1] = 1
    with pytest.raises(MODULE.E.ValidationError, match="multiple atom-token"):
        MODULE.inspect_features(data)


def test_removed_atom_mapping_cannot_hide_unresolved_atom():
    data = features()
    data["atom_to_token"][0] = 0
    with pytest.raises(MODULE.E.ValidationError, match="padding disagrees"):
        MODULE.inspect_features(data)


@pytest.mark.parametrize("change", ["empty", "target", "nonbinary"])
def test_invalid_cdr_mask_rejected(change):
    data = features()
    if change == "empty":
        data["design_mask"][:] = 0
    elif change == "target":
        data["design_mask"][0] = 1
    else:
        data["design_mask"][30] = 2
    with pytest.raises(MODULE.E.ValidationError):
        MODULE.inspect_features(data)


def test_nan_in_padding_also_rejected():
    data = features()
    data["coords"][0, -1, 0] = np.nan
    with pytest.raises(MODULE.E.ValidationError, match="finite singleton"):
        MODULE.inspect_features(data)


def test_real_input_masks_and_coordinates_never_modified():
    data = features()
    before = {key: value.copy() for key, value in data.items()}
    MODULE.inspect_features(data)
    assert all(np.array_equal(value, before[key]) for key, value in data.items())


def test_unknown_representation_rejected():
    with pytest.raises(ValueError):
        MODULE.inspect_features(features(), representation="relaxed_local")


def test_unknown_fixed_element_rejected():
    data = features()
    data["ref_element"][0] = np.eye(128, dtype=np.int8)[0]
    with pytest.raises(MODULE.E.ValidationError, match="actual heavy-element identity"):
        MODULE.inspect_features(data)
