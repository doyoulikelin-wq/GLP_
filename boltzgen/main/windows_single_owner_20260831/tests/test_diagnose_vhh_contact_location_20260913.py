"""Direct contact-location tests; one-atom geometry fixtures are not protein validation."""
import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import diagnose_vhh_contact_location_20260913 as D


def fixture():
    coords = np.column_stack((np.arange(151) * 100.0 + 10.0, np.zeros(151), np.zeros(151)))
    return coords, np.eye(151, dtype=int), tuple(range(135, 140))


@pytest.mark.parametrize("native,region", [(7,"terminal"),(8,"terminal"),(9,"near_N"),(14,"near_N"),
                                          (15,"middle"),(28,"middle"),(29,"C_region"),(36,"C_region")])
def test_native_region_boundaries(native, region):
    xyz, mapping, window = fixture()
    xyz[55] = xyz[native - 7] + [0, 4.5, 0]
    row = D.describe_sample(xyz, mapping, window)
    assert row["by_component"]["CDR1"]["target_native_residues"] == [native]
    assert [key for key, value in row["by_component"]["CDR1"]["region_contact"].items() if value] == [region]


def test_framework_cannot_substitute_for_cdr_endpoint():
    xyz, mapping, window = fixture()
    xyz[30] = xyz[0] + [0, 2, 0]
    xyz[31] = xyz[1] + [0, 2, 0]
    row = D.describe_sample(xyz, mapping, window)
    assert row["by_component"]["framework"]["both_terminal_contact"]
    assert not row["full_CDR_both_terminal_contact"]
    assert row["terminal_negative_location_class"] == "framework_only_contact"


def test_endpoint_different_cdr_and_window_overlay_not_double_counted():
    xyz, mapping, window = fixture()
    xyz[55] = xyz[0] + [0, 2, 0]
    xyz[135] = xyz[1] + [0, 3, 0]
    row = D.describe_sample(xyz, mapping, window)
    assert row["full_CDR_both_terminal_contact"]
    assert row["terminal_contributing_CDRs"] == {"His7": ["CDR1"], "Ala8": ["CDR3"]}
    assert row["edit_window_terminal_contribution"] == {"His7": False, "Ala8": True}
    assert row["by_component"]["full_CDR"]["residue_pair_count"] == 2
    assert row["by_component"]["edit_window"]["residue_pair_count"] == 1


def test_no_contact_and_noncontact_threshold_and_padding():
    xyz, mapping, window = fixture()
    xyz[55] = xyz[0] + [0, 4.500001, 0]
    xyz = np.vstack((xyz, xyz[0]))
    mapping = np.vstack((mapping, np.zeros(151)))
    row = D.describe_sample(xyz, mapping, window)
    assert row["terminal_negative_location_class"] == "no_VHH_contact_at_cutoff"
    json.dumps(row, allow_nan=False)


@pytest.mark.parametrize("malformed", ["nonfinite", "ambiguous_map", "missing_target", "bad_window"])
def test_reject_malformed_geometry(malformed):
    xyz, mapping, window = fixture()
    if malformed == "nonfinite": xyz[3, 0] = np.nan
    if malformed == "ambiguous_map": mapping[0, 1] = 1
    if malformed == "missing_target": mapping[0] = 0
    if malformed == "bad_window": window = (135, 136, 137, 138, 138)
    with pytest.raises(ValueError): D.describe_sample(xyz, mapping, window)


def test_negative_classes_disjoint_and_public_redaction():
    xyz, mapping, window = fixture()
    xyz[125] = xyz[8] + [0, 2, 0]
    row = D.describe_sample(xyz, mapping, window)
    assert row["terminal_negative_location_class"] == "CDR_middle_or_C_contact"
    out = D.aggregate([{"task_id": "PRIVATE_ID", "sequence": "PRIVATE_SEQUENCE", "samples": [row] * 3}])
    assert sum(out["terminal_negative_location_counts_disjoint"].values()) == 3
    assert "PRIVATE" not in json.dumps(out)
