"""CPU condition isolation, exact binding labels and preparation safeguards."""
import copy
from pathlib import Path
import random
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_vhh_terminal_controls as P


def source():
    return {"entities": [{"file": {"path": "target.cif", "include": [{"chain": {"id": "E", "res_index": "1..30"}}],
                                      "binding_types": [{"chain": {"id": "E", "binding": "1..2"}}],
                                      "structure_groups": [{"group": {"id": "E", "visibility": 1}}]}},
                         {"file": {"path": "scaffold.yaml"}}]}


def features(condition):
    residues = np.zeros((32, 33), int)
    residues[np.arange(32), P.ACTIVE_IDS + [2, 2]] = 1
    groups = np.array([1] * 30 + [2, 0])
    return {"binding_type": np.array(P.CONDITIONS[condition] + [0, 0]), "res_type": residues,
            "design_mask": np.array([False] * 31 + [True]), "structure_group": groups,
            "token_distance_mask": (groups[:, None] == groups[None, :]) & (groups[:, None] > 0)}


def test_exact_yaml_and_no_mutation():
    original = source()
    retained = copy.deepcopy(original)
    variants = P.make_conditions(original)
    assert original == retained
    assert "binding_types" not in variants["A"]["entities"][0]["file"]
    assert variants["B"] == original
    assert variants["C"]["entities"][0]["file"]["binding_types"] == [{"chain": {"id": "E", "binding": "1..2", "not_binding": "3..30"}}]
    for value in variants.values():
        value["entities"][0]["file"].pop("binding_types", None)
    assert variants["A"] == variants["B"] == variants["C"]


@pytest.mark.parametrize("condition", ["A", "B", "C"])
def test_all_actual_labels(condition):
    assert P.validate_binding(features(condition), P.CONDITIONS[condition])["target_token_count"] == 30


@pytest.mark.parametrize("change", ["wrong_nterminus", "target_design", "vhh_binding", "wrong_negative_labels", "cross_group"])
def test_rejects_misrepresented_features(change):
    values = features("C")
    if change == "wrong_nterminus":
        values["res_type"][[0, 1]] = values["res_type"][[1, 0]]
    elif change == "target_design":
        values["design_mask"][0] = True
    elif change == "vhh_binding":
        values["binding_type"][30] = 1
    elif change == "wrong_negative_labels":
        values["binding_type"][2] = 0
    else:
        values["token_distance_mask"][0, 30] = True
    with pytest.raises(ValueError):
        P.validate_binding(values, P.CONDITIONS["C"])


def test_rejects_any_non_binding_tensor_change():
    first, second = features("A"), features("C")
    assert P.differing_arrays(first, second) == []
    second["design_mask"][30] = True
    assert P.differing_arrays(first, second) == ["design_mask"]


def test_cpu_rng_scoped_and_reproducible():
    import torch
    python_state, numpy_state, torch_state = random.getstate(), np.random.get_state(), torch.get_rng_state().clone()
    original = np.random.default_rng
    with P.cpu_rng(20260911):
        first = (random.random(), np.random.random(), np.random.default_rng(None).random(), torch.rand(3))
    with P.cpu_rng(20260911):
        second = (random.random(), np.random.random(), np.random.default_rng(None).random(), torch.rand(3))
    assert first[:3] == second[:3]
    assert torch.equal(first[3], second[3])
    assert random.getstate() == python_state
    assert np.array_equal(np.random.get_state()[1], numpy_state[1])
    assert torch.equal(torch.get_rng_state(), torch_state)
    assert np.random.default_rng is original


def test_output_no_overwrite(tmp_path):
    path = tmp_path / "private.json"
    P.write_new(path, {"status": "test"})
    with pytest.raises(FileExistsError):
        P.write_new(path, {"status": "replacement"})
