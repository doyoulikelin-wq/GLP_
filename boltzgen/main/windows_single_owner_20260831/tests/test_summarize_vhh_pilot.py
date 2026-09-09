"""Small geometry/annotation unit tests, without inference or network."""
import importlib.util
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "summarize_vhh_pilot.py"
SPEC = importlib.util.spec_from_file_location("summarize_vhh_pilot", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def pose(x=0, angle=0):
    theta = np.deg2rad(angle)
    return {"cdr_backbone_centre_angstrom": [x, 0, 0], "framework_to_cdr_unit_direction": [np.cos(theta), np.sin(theta), 0]}


def test_two_orientations_can_share_same_epitope():
    row = {"contact_residue_pairs_zero_based": [[0, 45], [1, 46]]}
    assert M.footprint(row) == "N"
    assert len(M.cluster_poses([pose(angle=0), pose(angle=90)])) == 2


def test_position_and_direction_thresholds():
    assert M.same_pose(pose(), pose(x=7.999, angle=44.999))
    assert not M.same_pose(pose(), pose(x=8))
    assert not M.same_pose(pose(), pose(angle=45.001))


def test_complete_link_does_not_chain_distant_poses():
    assert M.cluster_poses([pose(x=0), pose(x=7), pose(x=14)]) == [[0, 1], [2]]


def test_free_fold_assignment_unassigned_and_closest():
    representatives = [pose(x=0), pose(x=20)]
    assert M.assign_fold(pose(x=1), representatives) == "P1"
    assert M.assign_fold(pose(x=19), representatives) == "P2"
    assert M.assign_fold(pose(x=10), representatives) == "unassigned"


def test_footprint_ties_and_empty_are_explicit():
    assert M.footprint({"contact_residue_pairs_zero_based": []}) == "none"
    assert M.footprint({"contact_residue_pairs_zero_based": [[0, 31], [10, 32]]}) == "mixed_tie"


def test_cdr3_source_annotation_not_scaffold_name():
    scaffold = {"design": [{"chain": {"id": "A", "res_index": "26..33,51..57,96..110"}}]}
    tokens, length = M.cdr_annotation(scaffold)
    assert length == 15
    assert tokens[0] == 55 and tokens[-1] == 139
    scaffold["design"][0]["chain"]["res_index"] = "24..31,50..56,95..105"
    assert M.cdr_annotation(scaffold)[1] == 11


@pytest.mark.parametrize("ranges", ["1..3,2..5,8..9", "0..3,5..6,8..9", "1..3,5..6", "1..3,5..6,9..8"])
def test_ambiguous_cdr_ranges_rejected(ranges):
    with pytest.raises(ValueError):
        M.cdr_annotation({"design": [{"chain": {"res_index": ranges}}]})


def test_cdr_insertions_require_explicit_remapping():
    with pytest.raises(ValueError):
        M.cdr_annotation({"design_insertions": [{"insertion": {}}]})


def geometry_fixture():
    token = np.repeat(np.arange(32), 4)
    backbone = np.ones(len(token), dtype=bool)
    cdr = token == 30
    framework = token == 31
    centers = np.random.default_rng(55).normal(size=(32, 3))
    centers[30] = [8, 2, 3]
    centers[31] = [12, 5, 1]
    xyz = np.repeat(centers, 4, axis=0)
    return xyz, (token, backbone, cdr, framework), centers[:30]


def test_target_alignment_is_globally_rigid_invariant():
    xyz, geometry, reference = geometry_fixture()
    before = M.describe_pose(xyz, geometry, reference, require_same_target=True)
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    after = M.describe_pose(xyz @ rotation + 120, geometry, reference, require_same_target=True)
    assert M.separation(before, after)[0] < 1e-10
    assert M.separation(before, after)[1] < 1e-5


def test_changed_target_reference_fails_comparability_check():
    xyz, geometry, reference = geometry_fixture()
    xyz[:4] += 3
    with pytest.raises(ValueError):
        M.describe_pose(xyz, geometry, reference, require_same_target=True)


def test_undefined_axis_and_nan_fail():
    xyz, geometry, reference = geometry_fixture()
    xyz[geometry[3]] = xyz[geometry[2]]
    with pytest.raises(ValueError):
        M.describe_pose(xyz, geometry, reference)
    xyz[0, 0] = np.nan
    with pytest.raises(ValueError):
        M.describe_pose(xyz, geometry, reference)


def pilot_fixture(base, duplicate=False):
    cells = []
    target = np.random.default_rng(42).normal(size=(30, 3)) * 3
    for number, scaffold in enumerate(("01_pdb_00007xl0-A", "02_pdb_00006apo-A")):
        cell = base / f"cell_{number}"
        source, attempt = cell / "source", cell / "attempt"
        source.mkdir(parents=True)
        root = attempt / "intermediate_designs"
        (root / "fold_out_npz").mkdir(parents=True)
        cdr_text = "1..1,3..3,5..6" if number == 0 else "1..1,3..3,5..7"
        scaffold_yaml = {"design": [{"chain": {"id": "A", "res_index": cdr_text}}]}
        (source / "scaffold.yaml").write_text(json.dumps(scaffold_yaml))
        for name in ("design.yaml", "scaffold.cif", "target.cif"):
            (source / name).write_text("synthetic test source\n")
        cdr_indices, length = M.cdr_annotation(scaffold_yaml)
        design_mask = np.zeros(38)
        design_mask[cdr_indices] = 1
        residue_ids = np.full(38, 2)
        residue_ids[0] = 10
        if number and not duplicate:
            residue_ids[-1] = 9
        counts = M.E.HEAVY_COUNTS[residue_ids - 2]
        token = np.repeat(np.arange(38), counts)
        atom_count = len(token)
        mapping = np.zeros((1, atom_count, 38), dtype=bool)
        mapping[0, np.arange(atom_count), token] = True
        backbone = np.concatenate([np.r_[np.ones(4), np.zeros(count-4)] for count in counts])[None]
        centres = np.r_[target, np.array([[10+number*20+i, i%3, i%2] for i in range(8)])]
        coords = centres[token]
        res_type = np.zeros((1, 38, 33), dtype=int)
        res_type[0, np.arange(38), residue_ids] = 1
        np.savez(root / "design_0.npz", design_mask=design_mask)
        np.savez(root / "fold_out_npz" / "design_0.npz", coords=np.repeat(coords[None], 5, axis=0), input_coords=coords[None, None],
                 atom_to_token=mapping, atom_resolved_mask=mapping.any(axis=2), backbone_mask=backbone,
                 token_index=np.arange(38)[None], res_type=res_type, mol_type=np.zeros((1, 38), dtype=int))
        cells.append({"cell_id": f"cell_{number}", "scaffold_id": scaffold, "spec_path": str(source),
            "spec_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()},
            "cdr3_length": length, "num_designs": 1, "attempt_root": str(attempt), "exit_code": 0})
    index = base / "INDEX.json"
    index.write_text(json.dumps({"cells": cells, "pose_classification": M.POSE_RULE, "expected_folds_per_candidate": 5}))
    return index


def test_end_to_end_identity_and_public_allowlist(tmp_path):
    index = pilot_fixture(tmp_path)
    output = tmp_path / "summary"
    public = M.summarize_index(index, output)
    assert public["candidate_count"] == 2
    assert public["scaffold_count"] == 2
    assert public["cdr3_length_class_count"] == 2
    assert public["generated_pose_class_count"] == 2
    assert public["diversity_requirements_satisfied"] is True
    private = json.loads((output / "PRIVATE_PILOT_SUMMARY.json").read_text())
    assert private["actual_candidate_set_sha256"] == M.candidate_set_digest(private["candidates"])
    assert private["candidates"][0]["vhh_sequence_sha256"] == hashlib.sha256(b"AAAAAAAA").hexdigest()
    public_text = (output / "PUBLIC_PILOT_SUMMARY.json").read_text()
    for secret in (str(tmp_path), "cell_0", "design_0", "vhh_sequence_sha256", "actual_candidate_set_sha256"):
        assert secret not in public_text
    with pytest.raises(ValueError):
        M.summarize_index(index, output)


def test_duplicate_sequences_do_not_inflate_diversity(tmp_path):
    index = pilot_fixture(tmp_path, duplicate=True)
    public = M.summarize_index(index, tmp_path / "summary")
    assert public["generated_candidate_count"] == 2
    assert public["candidate_count"] == 1
    assert public["duplicate_sequence_count"] == 1
    assert public["checks"]["candidate_sequences_unique"] is False
    assert public["diversity_requirements_satisfied"] is False


def test_source_mutation_and_unbound_rule_rejected(tmp_path):
    index = pilot_fixture(tmp_path)
    original = index.read_text()
    parsed = json.loads(original)
    parsed["pose_classification"]["position_separation_angstrom"] = 7.9
    index.write_text(json.dumps(parsed))
    with pytest.raises(ValueError):
        M.summarize_index(index, tmp_path / "summary")
    index.write_text(original)
    (tmp_path / "cell_0" / "source" / "target.cif").write_text("mutated")
    with pytest.raises(ValueError):
        M.summarize_index(index, tmp_path / "summary")
