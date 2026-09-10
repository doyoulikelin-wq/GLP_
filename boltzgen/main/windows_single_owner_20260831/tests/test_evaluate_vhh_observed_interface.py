"""Local observed-interface repair tests; no original mask modifications."""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_evaluate_vhh_pose_v2 import fold_fixture

SPEC = importlib.util.spec_from_file_location("evaluate_vhh_observed_interface", ROOT/"scripts/evaluate_vhh_observed_interface.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def partial_fixture():
    design, fold, ref = fold_fixture()
    token = fold["atom_to_token"][0].argmax(axis=1)
    missing = np.flatnonzero(token == 35)[-1]  # framework ALA CB, not backbone
    fold["atom_resolved_mask"][0, missing] = False
    fold["input_coords"][0, 0, missing] = 0
    return design, fold, ref, missing


def test_real_projection_preserves_masks_and_original_indices():
    design, fold, ref, missing = partial_fixture()
    before = fold["atom_resolved_mask"].copy()
    result = M.evaluate_observed_interface(design, fold, ref)
    assert np.array_equal(before, fold["atom_resolved_mask"])
    assert not fold["atom_resolved_mask"][0,missing]
    assert result["missing_framework_sidechain_atom_count"] == 1
    assert result["full_structure_validated"] is False
    assert result["original_strict_result_replacement"] is False
    assert result["analysis_classification"] == "RETROSPECTIVE_PARTIAL_OBSERVED_INTERFACE"
    assert result["samples"][0]["pose"]["usage"] == "RETROSPECTIVE_METHOD_DEVELOPMENT"
    for row in result["samples"]:
        assert row["contacts"]["min_target_vhh_distance_angstrom"] is None
        assert all(c in (30,31) for t,c in row["contacts"]["contact_residue_pairs_zero_based"])
    assert result["contact_summary"]["metrics"]["min_target_vhh_distance_angstrom"]["median"] is None


@pytest.mark.parametrize("region", ["target", "cdr", "framework_backbone"])
def test_missing_atoms_outside_permitted_region_fail(region):
    design, fold, ref, _ = partial_fixture()
    token = fold["atom_to_token"][0].argmax(axis=1)
    wanted = {"target":0,"cdr":30,"framework_backbone":35}[region]
    atom = np.flatnonzero(token==wanted)[0 if region == "framework_backbone" else -1]
    fold["atom_resolved_mask"][0,atom] = False
    with pytest.raises(ValueError): M.evaluate_observed_interface(design, fold, ref)


def test_finite_dummy_prediction_does_not_validate_missing_atoms():
    design, fold, ref, missing = partial_fixture()
    fold["coords"][:,missing] = [999,998,997]
    result = M.evaluate_observed_interface(design,fold,ref,geometry_source="free_fold_samples")
    assert len(result["samples"]) == 2
    assert result["missing_framework_sidechain_atom_count"] == 1
    assert result["original_strict_evaluation_status"] == "NA_MISSING_FRAMEWORK_SIDECHAINS_PRESERVED"


def test_projection_does_not_change_complete_target_cdr_metric():
    design, fold, ref = fold_fixture()
    expected = M.E.evaluate_arrays(design,fold,target_tokens=list(range(30)))
    result = M.evaluate_observed_interface(design,fold,ref,geometry_source="free_fold_samples")
    for original, current in zip(expected,result["samples"]):
        assert original["contact_residue_pairs_zero_based"] == current["contacts"]["contact_residue_pairs_zero_based"]
        assert original["min_target_cdr_distance_angstrom"] == current["contacts"]["min_target_cdr_distance_angstrom"]


def test_noncontiguous_projected_cdr_indices_map_back_to_original():
    design,fold,ref,_ = partial_fixture()
    design["design_mask"][31] = 0
    design["design_mask"][33] = 1
    token = fold["atom_to_token"][0].argmax(axis=1)
    fold["input_coords"][0,0,token==33] = [4.,0.,1.5]
    result = M.evaluate_observed_interface(design,fold,ref)
    partners = {cdr for target,cdr in result["samples"][0]["contacts"]["contact_residue_pairs_zero_based"]}
    assert 33 in partners
    assert 31 not in partners


def test_malformed_assignment_and_nonfinite_coordinates_fail():
    design, fold, ref, _ = partial_fixture()
    fold["atom_to_token"][0,0,1] = True
    with pytest.raises(ValueError): M.evaluate_observed_interface(design,fold,ref)
    design, fold, ref, _ = partial_fixture()
    fold["coords"][0,0,0] = np.nan
    with pytest.raises(ValueError): M.evaluate_observed_interface(design,fold,ref)
