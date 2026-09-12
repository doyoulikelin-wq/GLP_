"""Synthetic, CPU-only checks separating edit masks from original-CDR scoring."""
import copy
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

PATH=Path(__file__).resolve().parents[1]/"scripts/score_vhh_local_edit_20260913.py"
SPEC=importlib.util.spec_from_file_location("score_vhh_local_edit_20260913",PATH)
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)


def fixture(arm="LOCAL_INPAINT",mutate=False):
    source=np.array([*M.TARGET_IDS,*([2]*121)],dtype=int);actual=source.copy()
    window=list(range(125,130))
    if mutate:actual[125]=M.E.RESIDUES.index("LYS")+2
    xyz=[];tokens=[];names=[];backbone=[]
    for token,residue in enumerate(actual):
        atoms=M.const.ref_atoms[M.E.RESIDUES[int(residue)-2]]
        centre=np.array([100.+10*token,50.,20.])
        if token==1:centre=np.array([101.,50.,20.])
        if token==55:centre=np.array([102.,50.,20.])
        for j,name in enumerate(atoms):
            xyz.append(centre+[j*.1,(j%3)*.2,(j%2)*.3]);tokens.append(token);names.append(name)
            backbone.append(name in ("N","CA","C","O"))
    xyz=np.array(xyz);mapping=np.eye(151,dtype=bool)[tokens]
    chars=np.zeros((len(names),4,64),dtype=int)
    for i,name in enumerate(names):
        for j,ch in enumerate(name.ljust(4)):chars[i,j,ord(ch)-32]=1
    residues=np.eye(33,dtype=int)[actual]
    bound={"atom_to_token":mapping,"atom_pad_mask":np.ones(len(xyz),dtype=bool),"ref_atom_name_chars":chars,"res_type":residues}
    fold={"coords":np.repeat(xyz[None],3,axis=0),"atom_to_token":mapping[None],
        "atom_resolved_mask":np.ones((1,len(xyz)),dtype=bool),"token_index":np.arange(151)[None],
        "mol_type":np.zeros((1,151),int),"res_type":residues[None],"backbone_mask":np.array(backbone)[None]}
    fold.update({name:np.full(3,.5) for name in ("iptm","ptm","design_to_target_iptm","design_ptm")})
    design={"design_mask":np.isin(np.arange(151),window),"binding_type":np.zeros(151,int)}
    task={"task_id":"safe_task","arm":arm,"source_draw":0,"full_cdr_tokens":list(M.FULL_CDR_TOKENS),
        "edit_window_tokens":window,"source_residue_ids":source.tolist(),"expected_residue_ids":actual.tolist()}
    return design,fold,task,bound


def test_full_cdr_contact_outside_edit_window_is_primary_not_lost():
    result=M.evaluate_candidate(*fixture())
    assert result["metadata_design_mask_token_count"]==5
    assert result["scoring_cdr_token_count"]==30
    assert all(r["both_epitope_contacts"] for r in result["full_cdr_samples"])
    assert not any(r["any_target_cdr_contact"] for r in result["edit_window_samples"])


def test_allowed_window_sequence_change_preserves_outside_and_framework():
    result=M.evaluate_candidate(*fixture(mutate=True))
    assert result["window_substitution_count"]==1
    assert result["source_sequence_preserved_outside_window"] is True


@pytest.mark.parametrize("problem",["outside_window","original_mutation","changed_target","bad_window","scoring_window_only","unexpected_fixed_sequence","binding","missing_fold","nan","missing_atom","atom_mapping","backbone_mask"])
def test_scope_identity_or_atom_invalidity_fails_closed(problem):
    design,fold,task,bound=fixture(mutate=True)
    if problem=="outside_window":task["expected_residue_ids"][40]=3
    elif problem=="original_mutation":task["arm"]="ORIGINAL"
    elif problem=="changed_target":task["source_residue_ids"][0]=2
    elif problem=="bad_window":task["edit_window_tokens"]=[125,126,128,129,130]
    elif problem=="scoring_window_only":task["full_cdr_tokens"]=task["edit_window_tokens"]
    elif problem=="unexpected_fixed_sequence":task["expected_residue_ids"][125]=2
    elif problem=="binding":design["binding_type"][0]=1
    elif problem=="missing_fold":fold["coords"]=fold["coords"][:2]
    elif problem=="nan":fold["coords"][0,0,0]=np.nan
    elif problem=="missing_atom":fold["atom_resolved_mask"][0,-1]=False
    elif problem=="atom_mapping":bound["atom_to_token"]=bound["atom_to_token"].copy();bound["atom_to_token"][[0,20]]=bound["atom_to_token"][[20,0]]
    elif problem=="backbone_mask":fold["backbone_mask"][0,4]=True
    with pytest.raises(ValueError):M.evaluate_candidate(design,fold,task,bound)


@pytest.mark.parametrize("kind",["origin","translated_coincidence"])
def test_if_dummy_sidechains_are_rejected_even_after_translation(kind):
    design,fold,task,bound=fixture(mutate=True)
    token=fold["atom_to_token"][0].argmax(axis=-1)
    side=np.flatnonzero((token==125)&~fold["backbone_mask"][0])
    if kind=="origin":fold["coords"][0,side[0]]=0
    else:fold["coords"][1,side]=[200.,30.,10.]
    with pytest.raises(ValueError,match="placeholder"):M.evaluate_candidate(design,fold,task,bound)


@pytest.mark.parametrize("token_index",[0,40])
def test_target_and_framework_sidechain_placeholders_are_also_rejected(token_index):
    design,fold,task,bound=fixture()
    token=fold["atom_to_token"][0].argmax(axis=-1)
    atom=np.flatnonzero((token==token_index)&~fold["backbone_mask"][0])[0]
    fold["coords"][0,atom]=0
    with pytest.raises(ValueError,match="placeholder"):M.evaluate_candidate(design,fold,task,bound)


def six_results():
    result=M.evaluate_candidate(*fixture())
    candidates=[]
    for draw in range(2):
        for arm in M.ARMS:
            row=copy.deepcopy(result);row.update(source_draw=draw,arm=arm,task_id=f"private_{draw}_{arm}")
            candidates.append(row)
    return candidates


def test_nested_denominators_and_public_allowlist():
    candidates=six_results();public=M.public_summary(candidates)
    assert public["free_fold_sample_count"]==18
    arm=public["by_arm"]["ORIGINAL"]
    assert arm["primary_full_cdr"]["source_candidate_count"]==2
    assert arm["primary_full_cdr"]["free_fold_sample_count"]==6
    assert arm["primary_full_cdr"]["candidate_all_three_contact_counts"]["both_epitope_contacts"]==2
    assert arm["diagnostic_edit_window"]["sample_contact_counts"]["both_epitope_contacts"]==0
    assert arm["unique_sequence_count"]==1  # repeated sequences retained, not 1 draw
    text=json.dumps(public,allow_nan=False)
    assert "private_" not in text and candidates[0]["vhh_sequence_sha256"] not in text


def test_one_bad_fold_removes_all_three_stability_not_entire_candidate():
    candidates=six_results();candidates[0]["full_cdr_samples"][-1]["both_epitope_contacts"]=False
    public=M.public_summary(candidates)
    counts=public["by_arm"]["ORIGINAL"]["primary_full_cdr"]
    assert counts["sample_contact_counts"]["both_epitope_contacts"]==5
    assert counts["candidate_all_three_contact_counts"]["both_epitope_contacts"]==1
    assert counts["source_candidate_count"]==2


def test_missing_arm_is_not_silently_aggregated():
    with pytest.raises(ValueError):M.public_summary(six_results()[:-1])


@pytest.mark.parametrize("metric",["iptm","ptm","design_to_target_iptm","design_ptm"])
@pytest.mark.parametrize("bad",["shape","nan"])
def test_all_confidence_arrays_are_three_finite_values(metric,bad):
    design,fold,task,bound=fixture()
    if bad=="shape":fold[metric]=np.array([.5,.5])
    else:fold[metric][0]=np.nan
    with pytest.raises(ValueError,match="confidence"):M.evaluate_candidate(design,fold,task,bound)


def test_real_runtime_zero_encoded_padding_is_not_an_assigned_atom():
    design,fold,task,bound=fixture()
    bound["atom_to_token"]=np.r_[bound["atom_to_token"],np.zeros((1,151),bool)]
    bound["atom_pad_mask"]=np.r_[bound["atom_pad_mask"],False]
    bound["ref_atom_name_chars"]=np.concatenate([bound["ref_atom_name_chars"],np.zeros((1,4,64),int)])
    fold["atom_to_token"]=bound["atom_to_token"][None]
    fold["atom_resolved_mask"]=bound["atom_pad_mask"][None]
    fold["backbone_mask"]=np.c_[fold["backbone_mask"],False]
    fold["coords"]=np.concatenate([fold["coords"],np.zeros((3,1,3))],axis=1)
    result=M.evaluate_candidate(design,fold,task,bound)
    assert result["strict_full_atom_validation"] is True
