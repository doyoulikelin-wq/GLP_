"""Fast CPU tests for structural annotation and genuine framework difference."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/screen_vhh_scaffold_library.py"
SPEC = importlib.util.spec_from_file_location("screen_vhh_scaffold_library", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def scaffold_annotation(ranges="26..33,51..57,96..110"):
    """Return a small synthetic annotation in the supported fixed-loop schema."""
    return {"design": [{"chain": {"id": "A", "res_index": ranges}}]}


def test_edit_distance_handles_substitutions_and_insertions():
    assert MODULE.edit_distance("ABCD", "ABCD") == 0
    assert MODULE.edit_distance("ABCD", "ABXD") == 1
    assert MODULE.edit_distance("ABCD", "ABCDE") == 1
    assert MODULE.edit_distance("", "AB") == 2


def test_annotations_are_label_indexed_fixed_loop_lengths():
    ranges = MODULE.annotation(scaffold_annotation(), 121)
    assert [end-start+1 for start, end in ranges] == [8, 7, 15]


@pytest.mark.parametrize("value", ["0..3,51..57,96..110", "26..33,30..57,96..110",
                                   "26..33,51..57,96..122", "26..33,51..57", "26..33,51..57,110..96"])
def test_invalid_cdr_ranges_rejected(value):
    with pytest.raises(ValueError):
        MODULE.annotation(scaffold_annotation(value), 121)


def record(identifier, lengths, framework, ready=True):
    """Create an in-memory record sufficient for the selection rule."""
    return {"scaffold_id": identifier, "cdr_lengths": lengths, "framework_sequence": framework,
            "scaffold_input_ready": ready, "resolution_angstrom": 1.5}


def test_matching_lengths_need_actual_framework_difference():
    old = record(MODULE.BASELINES[0], [8, 7, 15], "ACDE")
    duplicate = record("new", [8, 7, 15], "ACDE")
    assert MODULE.choose_addition([old, duplicate]) is None
    new = record("new", [8, 7, 15], "ACDF")
    assert MODULE.choose_addition([old, new])["framework_edit_distance"] == 1


def test_different_length_or_incomplete_scaffold_not_declared_matched():
    old = record(MODULE.BASELINES[0], [8, 7, 15], "ACDE")
    assert MODULE.choose_addition([old, record("new", [8, 8, 15], "ACDF")]) is None
    assert MODULE.choose_addition([old, record("new", [8, 7, 15], "ACDF", False)]) is None


def test_refuse_raw_library_inside_git_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside a Git checkout"):
        MODULE.screen(tmp_path / "source", tmp_path / "output")


def test_refuse_existing_private_library(tmp_path):
    with pytest.raises(ValueError, match="refusing to reuse"):
        MODULE.screen(tmp_path / "source", tmp_path)
