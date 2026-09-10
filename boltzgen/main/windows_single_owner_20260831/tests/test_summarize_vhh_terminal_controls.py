"""Strict terminal-control aggregation checks without inference or network."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_evaluate_vhh_pose_v2 import fold_fixture

SCRIPT = Path(__file__).resolve().parents[1]/"scripts/summarize_vhh_terminal_controls.py"
SPEC = importlib.util.spec_from_file_location("summarize_vhh_terminal_controls", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture(condition="B"):
    design, fold, ref = fold_fixture()
    expected = {"A": [0]*30, "B": [1,1]+[0]*28, "C": [1,1]+[2]*28}[condition]
    design["binding_type"] = np.r_[expected, np.zeros(8)].astype(int)
    fold["coords"] = np.repeat(fold["coords"][:1],5,axis=0)
    return design, fold, ref, expected


def evaluate(data):
    design, fold, ref, expected = data
    return M.evaluate_candidate(design,fold,ref,expected_target_binding_types=expected,cdr_tokens=[30,31])


@pytest.mark.parametrize("condition",["A","B","C"])
def test_each_explicit_condition_uses_strict_generated_and_five_free_folds(condition):
    result = evaluate(fixture(condition))
    assert result["strict_all_atom_validation"] is True
    assert result["partial_fallback_used"] is False
    assert len(result["generated"]["samples"]) == 1
    assert len(result["free_folds"]["samples"]) == 5


def test_missing_framework_sidechain_remains_fatal_not_partial():
    data = fixture()
    fold = data[1]
    token = fold["atom_to_token"][0].argmax(axis=-1)
    fold["atom_resolved_mask"][0,np.flatnonzero(token==35)[-1]] = False
    with pytest.raises(ValueError, match="assigned/resolved mismatch"):
        evaluate(data)


@pytest.mark.parametrize("problem",["wrong_condition","binder_condition","missing_repeat","cdr_annotation","nan"])
def test_incorrect_condition_or_structure_is_rejected(problem):
    data = fixture()
    design,fold,ref,expected = data
    if problem == "wrong_condition": design["binding_type"][0] = 0
    elif problem == "binder_condition": design["binding_type"][32] = 1
    elif problem == "missing_repeat": fold["coords"] = fold["coords"][:4]
    elif problem == "cdr_annotation": design["design_mask"][32] = 1
    elif problem == "nan": fold["coords"][0,0,0] = np.nan
    with pytest.raises(ValueError): evaluate(data)


def test_nested_denominators_and_no_sequence_publication():
    candidate = {"candidate_id":"secret_candidate",**evaluate(fixture())}
    result = M.condition_aggregate([candidate,candidate])
    assert result["denominators"] == {"generated_candidates":2,"free_fold_samples":10,"candidates_for_five_of_five_terminal_contact":2,"folds_nested_within_each_candidate":5}
    assert result["unique_vhh_sequence_count"] == 1
    assert result["generated_contacts"]["sample_count"] == 2
    assert result["free_fold_contacts"]["sample_count"] == 10
    text = json.dumps(result,allow_nan=False)
    assert "secret_candidate" not in text
    assert candidate["vhh_sequence_sha256"] not in text


def test_one_failed_repeat_prevents_five_of_five_description():
    left = evaluate(fixture())
    right = evaluate(fixture())
    for row in (left,right):
        row["generated"]["samples"][0]["contacts"]["both_epitope_contacts"] = True
        for sample in row["free_folds"]["samples"]:
            sample["contacts"]["both_epitope_contacts"] = True
    right["free_folds"]["samples"][-1]["contacts"]["both_epitope_contacts"] = False
    result = M.condition_aggregate([left,right])
    assert result["generated_both_terminal_contact_candidate_count"] == 2
    assert result["candidate_five_of_five_both_terminal_contact_count"] == 1


def test_pose_outside_domain_does_not_discard_strict_contact_data():
    data = fixture()
    fold = data[1]
    token = fold["atom_to_token"][0].argmax(axis=-1)
    fold["input_coords"][0,0,token==0] += [3.,0.,0.]
    candidate = evaluate(data)
    assert candidate["generated"]["pose_applicable_sample_count"] == 0
    result = M.condition_aggregate([candidate,candidate])
    assert result["generated_pose_class_count_descriptive_only"] is None
    assert result["generated_contacts"]["sample_count"] == 2


def test_wrong_condition_denominators_fail():
    with pytest.raises(ValueError): M.condition_aggregate([evaluate(fixture())])


def test_frozen_plan_kept_separate_from_terminal_fields(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules,"run_vhh_terminal_controls",SimpleNamespace(validate_plan=lambda plan,**kwargs:calls.append((plan,kwargs))))
    frozen = [{"condition_id":condition,"cell_id":"cell_"+condition,"expected_target_binding_types":[0]*30} for condition in M.CONDITIONS]
    index = {"schema":"VHH_TERMINAL_CONTROL_PLAN_V1","status":"GPU_COMPLETE_PENDING_STRICT_ANALYSIS",
        "frozen_cells":frozen,"cells":[{**row,"exit_code":0} for row in frozen]}
    M.validate_frozen_plan(index)
    assert calls[0][0]["cells"] == frozen
    assert calls[0][1] == {"recheck_inputs":False}
    index["cells"][1]["expected_target_binding_types"] = [1]*30
    with pytest.raises(ValueError,match="frozen input"):
        M.validate_frozen_plan(index)


def test_failed_execution_cannot_be_summarized_as_complete(monkeypatch):
    monkeypatch.setitem(sys.modules,"run_vhh_terminal_controls",SimpleNamespace(validate_plan=lambda *args,**kwargs:None))
    with pytest.raises(ValueError,match="completed"):
        M.validate_frozen_plan({"schema":"VHH_TERMINAL_CONTROL_PLAN_V1","status":"FAILED"})


def test_terminal_strict_output_check_matches_actual_runner_schema():
    scope = "STRICT_FULL_ASSIGNED_CANONICAL_HEAVY_ATOMS"
    check = {"status":"PASS","candidate_count":2,"free_fold_sample_count":10,"scope":scope,"biological_pass":False}
    M.validate_strict_output_check({"strict_output_check":check},scope)
    for field,value in (("status","FAIL"),("candidate_count",1),("free_fold_sample_count",9),("scope","PARTIAL"),("biological_pass",True)):
        with pytest.raises(ValueError):
            M.validate_strict_output_check({"strict_output_check":{**check,field:value}},scope)
    with pytest.raises(ValueError): M.validate_strict_output_check({},scope)
