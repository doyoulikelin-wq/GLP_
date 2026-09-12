"""Test original-generation versus dummy-sidechain interface semantics."""

import importlib.util
from pathlib import Path
import numpy as np
import pytest

path=Path(__file__).resolve().parents[1]/"scripts/diagnose_vhh_terminal_interface.py"
spec=importlib.util.spec_from_file_location("diagnose_vhh_terminal_interface",path)
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


def test_rigid_motion_preserves_shape_and_relative_direction():
    a=np.random.default_rng(9).normal(size=(12,3)); rot=np.array([[0.,-1,0],[1,0,0],[0,0,1]])
    b=a@rot+[8.,-3.,4.]
    result=M.compare_geometry(a,b,np.arange(4),np.arange(4,12))
    assert result['cdr_centroid_rmsd_after_framework_fit_angstrom']<1e-12
    assert result['cdr_internal_distance_rms_change_angstrom']<1e-12
    assert result['framework_relative_cdr_direction_change_degrees']<1e-5


def test_local_cdr_change_detected_without_framework_change():
    a=np.random.default_rng(9).normal(size=(12,3)); b=a.copy();b[0]+=[2.,0.,0.]
    result=M.compare_geometry(a,b,np.arange(4),np.arange(4,12))
    assert result['framework_fit_rmsd_angstrom']<1e-12
    assert result['cdr_centroid_rmsd_after_framework_fit_angstrom']>0.9
    assert result['cdr_internal_distance_rms_change_angstrom']>0


def test_missing_stage_stays_not_assessable_not_zero():
    result=M.transition({'status':'MISSING'},{'status':'OBSERVED'},np.arange(3))
    assert result['cdr_identity_change_count'] is None and result['samples']==[]


@pytest.mark.parametrize('bad',['nan','degenerate','wrongshape'])
def test_invalid_alignment_rejected(bad):
    a=np.random.default_rng(9).normal(size=(8,3));b=a.copy()
    if bad=='nan': b[0,0]=np.nan
    elif bad=='degenerate': b[:]=0
    else: b=b[:-1]
    with pytest.raises(ValueError):M.rigid_fit(a,b)


def test_incomplete_cif_is_not_silently_evaluated_as_full():
    cif=b'data_x\nloop_\n_atom_site.label_asym_id\n_atom_site.label_seq_id\n_atom_site.label_comp_id\n_atom_site.label_atom_id\n_atom_site.type_symbol\n_atom_site.label_alt_id\n_atom_site.pdbx_PDB_model_num\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\nA 1 HIS N N . 1 0 0 0\n'
    with pytest.raises(ValueError,match='incomplete'):M.cif_arrays(cif,np.array([30]))


def test_all_atom_linkage_accepts_reordering_but_detects_changed_sidechain():
    a=np.array([[0.,0,0],[1,0,0],[0,1,0],[4,0,0]])
    mapping=np.array([[1,0],[1,0],[1,0],[0,1]],dtype=bool)[None]
    left={'coords':a[None],'atom_to_token':mapping,'res_type':np.array([1,2])}
    order=[2,0,1,3]
    right={'input_coords':a[order][None,None],'atom_to_token':mapping[:,order], 'res_type':np.array([1,2])}
    assert M.input_atom_linkage(left,right)==0
    right['input_coords'][0,0,0,0]+=0.5
    with pytest.raises(ValueError,match='coordinates differ'):M.input_atom_linkage(left,right)


def test_placeholder_contacts_aggregate_as_na_not_zero():
    row={k:None for k in (*M.NUMERIC,*M.BOOLEAN)}
    row.update(zero_coordinate_cdr_sidechain_atom_count=90,pose={'engineering_domain':'WITHIN'})
    missing={'status':'NOT_ASSESSABLE_MISSING_STAGE','samples':[],'cdr_identity_change_count':None}
    candidate={'stages':{s:{'status':'OBSERVED','samples':[row]} for s in M.STAGES},
        'transitions':{'generation_to_inverse_folding':missing,'inverse_to_free_folding':missing}}
    result=M.aggregate([candidate])
    assert result['inverse_folding']['both_epitope_contacts_count'] is None
    assert result['inverse_folding']['contact_metric_applicable_sample_counts']['both_epitope_contacts']==0
    assert result['inverse_folding']['his_min_cdr_distance_angstrom']['median'] is None
