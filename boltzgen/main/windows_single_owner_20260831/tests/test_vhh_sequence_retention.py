"""CPU-only contracts for the entire C-arm raw/IF sequence comparison."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
import numpy as np

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import prepare_vhh_sequence_retention as P
import run_vhh_sequence_retention as R


def preparation():
    return {'schema':'VHH_SEQUENCE_RETENTION_PREPARATION_V1','task_count':4,'expected_fold_samples':8,
        'hard_timeout_seconds':900,'implementation_sha256':P.implementations(),'cpu_preflight':{'status':'PASS'},
        'tasks':[{'source_draw':i,'sequence_source':s,'task_id':f'{i}_{s}'} for i in range(2) for s in P.SOURCES],
        'source_files':{},'output_sha256':{}}


def test_exact_entire_c_arm_four_tasks(tmp_path):R.verify_prepared(tmp_path,preparation())


@pytest.mark.parametrize('problem',['one_draw','same_source','folds','code','cpu'])
def test_modified_scope_cannot_run(tmp_path,problem):
    r=preparation()
    if problem=='one_draw':r['tasks']=r['tasks'][:2]
    elif problem=='same_source':r['tasks'][0]['sequence_source']='INVERSE_FOLDED'
    elif problem=='folds':r['expected_fold_samples']=10
    elif problem=='code':r['implementation_sha256']={}
    else:r['cpu_preflight']['status']='FAIL'
    with pytest.raises(ValueError):R.verify_prepared(tmp_path,r)


def test_input_hash_change_rejected(tmp_path):
    p=tmp_path/'input';p.write_text('old');r=preparation();r['output_sha256']={'input':P.sha(p)}
    p.write_text('new')
    with pytest.raises(ValueError,match='changed'):R.verify_prepared(tmp_path,r)


def test_free_config_is_exact_two_folds_and_no_binder_template(tmp_path):
    c=P.control_config(tmp_path/'inputs',tmp_path/'runtime')
    assert c['diffusion_samples']==2
    assert c['data']['target_templates'] is True
    assert c['data']['design_mask_templates'] is False
    assert c['data']['return_native'] is False
    assert c['trainer']['devices']==1


def test_missing_new_calibration_blocks_before_creation_or_gpu(tmp_path,monkeypatch):
    prepared=tmp_path/'prepared';prepared.mkdir();P.write_json(prepared/'PREPARATION.json',preparation())
    def denied(path):raise ValueError('new 9NK9 recovery missing')
    monkeypatch.setattr(R,'validate_calibration_receipt',denied)
    args=SimpleNamespace(workspace=tmp_path,repo_root=tmp_path,prepared=prepared,output=tmp_path/'attempt',
        hard_timeout_seconds=900,calibration_receipt=tmp_path/'missing',runtime_root=tmp_path)
    with pytest.raises(ValueError,match='9NK9'):R.run(args)
    assert not args.output.exists()


def test_backbone_mask_must_match_cpu_named_atoms():
    names=['N','CA','C','O','CB']
    chars=np.zeros((5,4,64),dtype=int)
    for i,name in enumerate(names):
        for j,ch in enumerate(name.ljust(4)):chars[i,j,ord(ch)-32]=1
    bound={'ref_atom_name_chars':chars,'atom_pad_mask':np.ones(5,dtype=bool)}
    fold={'atom_to_token':np.ones((1,5,1),dtype=bool),'backbone_mask':np.array([[1,1,1,1,0]])}
    assert R.validated_backbone(fold,bound).tolist()==[True,True,True,True,False]
    fold['backbone_mask'][0,-1]=1
    with pytest.raises(ValueError,match='atom names'):R.validated_backbone(fold,bound)
