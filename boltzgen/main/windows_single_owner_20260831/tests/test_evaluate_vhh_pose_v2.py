"""Synthetic method-development checks, not protein-binding calibration."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest
import gemmi

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_vhh_pose_v2.py"
SPEC = importlib.util.spec_from_file_location("evaluate_vhh_pose_v2", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def reference():
    angle = np.arange(30) * 1.7
    return np.stack([2.3*np.cos(angle), 2.3*np.sin(angle), np.arange(30)*1.5], axis=-1)


def evaluate(target, ref=None, cdr=(6., 2., 8.), framework=(15., 3., 11.), usage="RETROSPECTIVE_METHOD_DEVELOPMENT"):
    return M.evaluate_geometry(target, cdr, framework, reference() if ref is None else ref,
        biological_numbers=tuple(range(7, 37)), usage=usage)


def test_rigid_transform_is_invariant():
    ref = reference()
    before = evaluate(ref)
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    after = evaluate(ref@rotation+72, cdr=np.array([6.,2.,8.])@rotation+72, framework=np.array([15.,3.,11.])@rotation+72)
    assert before["engineering_domain"] == after["engineering_domain"] == "WITHIN"
    for key in before["pose_descriptor"]:
        assert np.allclose(before["pose_descriptor"][key], after["pose_descriptor"][key], atol=1e-10)


def test_small_deformation_is_within_engineering_domain_not_biological_pass():
    target = reference()+np.random.default_rng(4).normal(scale=.05,size=(30,3))
    result = evaluate(target)
    assert result["engineering_domain"] == "WITHIN"
    assert result["biological_pass"] is False
    assert result["old_gate_unlock_allowed"] is False
    assert result["usage"] == "RETROSPECTIVE_METHOD_DEVELOPMENT"


def test_large_global_deformation_is_not_comparable():
    result = evaluate(reference()+np.random.default_rng(4).normal(scale=2,size=(30,3)))
    assert result["engineering_domain"] == "OUTSIDE"
    assert result["pose_descriptor"] is None
    assert any("global" in reason or "core" in reason for reason in result["reasons"])


def test_terminal_local_deformation_cannot_hide_in_global_average():
    target = reference()
    target[0] += [3.,0.,0.]
    result = evaluate(target)
    assert result["diagnostics"]["global_rmsd_angstrom"] < 1.0
    assert result["diagnostics"]["core_rmsd_angstrom"] < 1e-10
    assert result["engineering_domain"] == "OUTSIDE"
    assert result["reasons"] == ["local_epitope_max_displacement_angstrom_outside_engineering_domain"]


def test_terminal_displacement_boundary_is_engineering_only():
    target = reference()
    target[0] += [1.001,0.,0.]
    assert evaluate(target)["engineering_domain"] == "OUTSIDE"
    target[0] -= [.002,0.,0.]
    assert evaluate(target)["engineering_domain"] == "WITHIN"


def test_degenerate_candidate_returns_pose_na():
    target = reference()
    target[6:27] = np.stack([np.arange(21),np.zeros(21),np.zeros(21)],axis=-1)
    result = evaluate(target)
    assert result["engineering_domain"] == "OUTSIDE"
    assert result["diagnostics"] is None


def test_degenerate_reference_is_invalid_input():
    ref = np.stack([np.arange(30),np.zeros(30),np.zeros(30)],axis=-1)
    with pytest.raises(ValueError,match="reference alignment"):
        evaluate(reference(),ref=ref)


def test_nearly_collinear_core_is_rejected_not_numerically_arbitrary():
    target = reference()
    target[6:27,:2] *= .00001
    assert evaluate(target)["engineering_domain"] == "OUTSIDE"


def test_nonfinite_and_mapping_errors_fail_closed():
    target = reference()
    target[0,0] = np.nan
    with pytest.raises(ValueError): evaluate(target)
    with pytest.raises(ValueError):
        M.evaluate_geometry(reference(),[1.,0.,0.],[2.,0.,0.],reference(),biological_numbers=list(range(8,38)),usage="PROSPECTIVE_EXPLORATORY")


def test_unknown_usage_and_undefined_direction_rejected():
    with pytest.raises(ValueError): evaluate(reference(),usage="OLD_GATE_APPROVED")
    result = evaluate(reference(),cdr=(1.,1.,1.),framework=(1.,1.,1.))
    assert result["pose_descriptor"] is None


def test_diversity_is_descriptive_and_na_is_not_zero_classes():
    one = evaluate(reference())
    result = M.descriptive_clusters([one,one])
    assert result["class_count"] == 1
    assert result["minimum_class_count_required"] is None
    bad = evaluate(reference()+np.random.default_rng(8).normal(scale=4,size=(30,3)))
    assert M.descriptive_clusters([bad])["class_count"] is None
    assert M.descriptive_clusters([one,bad])["not_applicable_count"] == 1


def test_different_binding_directions_are_descriptive_classes():
    one = evaluate(reference())
    two = evaluate(reference(),cdr=(30.,2.,8.),framework=(15.,3.,11.))
    assert M.descriptive_clusters([one,two])["class_count"] == 2


def test_cluster_cannot_mix_original_reference_coordinate_frames():
    one = evaluate(reference())
    two = evaluate(reference()+1,ref=reference()+1)
    with pytest.raises(ValueError,match="same original reference"):
        M.descriptive_clusters([one,two])


def fold_fixture(deform=False):
    names = list(M.ACTIVE_SEQUENCE_NAMES)+["ALA"]*8
    ids = np.asarray([M.E.RESIDUES.index(name)+2 for name in names])
    counts = M.E.HEAVY_COUNTS[ids-2]
    token = np.repeat(np.arange(38),counts)
    mapping = np.zeros((1,len(token),38),dtype=bool)
    mapping[0,np.arange(len(token)),token] = True
    backbone = np.concatenate([np.r_[np.ones(4),np.zeros(count-4)] for count in counts])[None]
    centres = np.r_[reference(),np.array([[6.+i*2,2.,8.] for i in range(8)])]
    if deform:
        centres[0] += [3.,0.,0.]
    coords = centres[token]
    res_type = np.zeros((1,38,33),dtype=int)
    res_type[0,np.arange(38),ids] = 1
    mask = np.zeros(38)
    mask[30:32] = 1
    design = {"design_mask":mask}
    fold = {"coords":np.repeat(coords[None],2,axis=0),"input_coords":coords[None,None],
        "atom_to_token":mapping,"atom_resolved_mask":mapping.any(axis=2),"backbone_mask":backbone,
        "token_index":np.arange(38)[None],"res_type":res_type,"mol_type":np.zeros((1,38),dtype=int)}
    ref = {"centroids":reference(),"biological_numbers":M.BIOLOGICAL_NUMBERS,
        "residue_names":M.ACTIVE_SEQUENCE_NAMES,"source_sha256":"0"*64}
    return design,fold,ref


def test_pose_outside_domain_does_not_remove_contact_evaluation():
    design,fold,ref = fold_fixture(deform=True)
    result = M.evaluate_fold(design,fold,ref,usage="PROSPECTIVE_EXPLORATORY")
    assert result["pose_applicable_sample_count"] == 0
    assert result["samples"][0]["pose"]["pose_descriptor"] is None
    assert result["contact_summary"]["sample_count"] == 1
    assert isinstance(result["samples"][0]["contacts"]["any_target_cdr_contact"],bool)
    assert result["biological_pass"] is False


def test_free_folds_are_assessed_separately_and_identity_mismatch_rejected():
    design,fold,ref = fold_fixture()
    result = M.evaluate_fold(design,fold,ref,usage="PROSPECTIVE_EXPLORATORY",geometry_source="free_fold_samples")
    assert result["pose_applicable_sample_count"] == 2
    ref["residue_names"] = tuple(reversed(M.ACTIVE_SEQUENCE_NAMES))
    with pytest.raises(ValueError,match="identity"):
        M.evaluate_fold(design,fold,ref,usage="PROSPECTIVE_EXPLORATORY")


def test_original_reference_has_explicit_numbering_and_sequence(tmp_path):
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("A")
    for index,(name,point) in enumerate(zip(M.ACTIVE_SEQUENCE_NAMES,reference())):
        residue = gemmi.Residue()
        residue.name,residue.seqid = name,gemmi.SeqId(index+101," ")
        for atom_name in ("N","CA","C","O"):
            atom = gemmi.Atom()
            atom.name,atom.element = atom_name,gemmi.Element(atom_name[0])
            atom.pos = gemmi.Position(*point)
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    path = tmp_path/"reference.cif"
    structure.make_mmcif_document().write_file(str(path))
    result = M.load_reference(path,"A")
    assert result["biological_numbers"] == tuple(range(7,37))
    assert result["source_auth_residue_ids"][0] == "101"
    assert np.allclose(result["centroids"],reference(),atol=.001)
    with pytest.raises(ValueError): M.load_reference(path,"B")
    with pytest.raises(ValueError): M.load_reference(path,"A",biological_numbers=range(101,131))
