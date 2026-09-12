"""Test exact shared-atom and residue mapping for matched deletion inputs."""

from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_vhh_same_candidate_pairing_20260913 as P
import build_vhh_matched_pair_inputs as B


def test_arg_nh2_is_sidechain_not_extra_amide():
    from boltzgen.data import const
    assert const.ref_atoms["ARG"] == ["N", "CA", "C", "O", "CB", "CG", "CD", "NE", "CZ", "NH1", "NH2"]
    assert len(const.ref_atoms["ARG"]) == 11
    assert "OXT" not in const.ref_atoms["ARG"]


def test_negative_labels_are_not_positive_hotspots():
    original = np.array([1, 1] + [2]*28 + [0]*121)
    truncated = original[2:]
    assert np.flatnonzero(truncated == 1).tolist() == []
    assert np.flatnonzero(truncated == 2).tolist() == list(range(28))


def test_deletion_source_identity_and_shared_sequence():
    assert B.ACTIVE_GLP1[:3] == "HAE"
    assert len(B.ACTIVE_GLP1) == 30 and len(B.ACTIVE_GLP1[2:]) == 28
    assert list(range(9, 37)) == [i + 2 + 7 for i in range(28)]


def test_new_directory_only(tmp_path):
    with pytest.raises(ValueError, match="new private"):
        P.prepare(tmp_path / "missing-index.json", tmp_path, tmp_path)


def test_truncated_label_seq_repaired_without_moving_atoms():
    import gemmi
    sys.path.insert(0, str(Path(__file__).parent))
    from test_build_vhh_matched_pair_inputs import fixture
    structure, metadata = fixture()
    text, _, _ = B.paired_state(structure, metadata, 2, "synthetic_truncated")
    original = gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block())
    assert original[0][0][0].label_seq == 3
    fixed = gemmi.make_structure_from_block(gemmi.cif.read_string(P.canonicalize_label_sequence(text)).sole_block())
    assert [r.label_seq for r in fixed[0][0]] == list(range(1, 29))
    assert fixed[0][0][0].name == "GLU"
    B.check_same_atoms(list(original[0][0]), list(fixed[0][0]))
    B.check_same_atoms(list(original[0][1]), list(fixed[0][1]))
