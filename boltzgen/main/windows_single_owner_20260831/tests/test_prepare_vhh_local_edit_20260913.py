"""CPU-only tests for explicit local edit selection, masks, and input contracts."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import prepare_vhh_local_edit_20260913 as P


class LocalEditingPreparationTests(unittest.TestCase):
    """Exercise deterministic selection and reject accidental broader editing."""

    def setUp(self):
        """Create a small synthetic feature contract, not a biological structure."""
        self.window = {"vhh_start_one_based": 51, "vhh_end_one_based": 55}
        self.edit = list(range(80, 85))
        self.ids = np.full(151, 2)
        self.mask = np.isin(np.arange(151), self.edit)

    def arrays(self, arm):
        """Construct the exact required tensor subset for mask tests."""
        groups = np.ones(151)
        if arm == "LOCAL_INPAINT":
            groups[self.mask] = 0
        atom_tokens = np.repeat(np.arange(151), 4)
        arrays = {"res_type": np.eye(33)[self.ids], "design_mask": self.mask,
            "binding_type": np.zeros(151), "structure_group": groups,
            "token_distance_mask": (groups[:, None] == groups[None, :]) & (groups[:, None] > 0),
            "atom_to_token": np.eye(151)[atom_tokens], "backbone_mask": np.ones(604)}
        return arrays, {key: arrays[key].copy() for key in ("design_mask", "binding_type")}

    def test_inpaint_only_hides_window(self):
        """The editable local segment, and no other region, loses visibility."""
        spec = P.edit_spec(self.window, "LOCAL_INPAINT")["entities"][0]["file"]
        self.assertEqual(spec["design"], [{"chain": {"id": "B", "res_index": "51..55"}}])
        self.assertEqual(spec["structure_groups"][-1]["group"]["visibility"], 0)
        self.assertNotIn("design_insertions", spec)
        self.assertNotIn("binding_types", spec)

    def test_sequence_only_keeps_backbone_context(self):
        """Sequence-only input retains every residue's structural visibility."""
        spec = P.edit_spec(self.window, "SEQUENCE_ONLY")["entities"][0]["file"]
        self.assertEqual(len(spec["structure_groups"]), 2)
        self.assertTrue(all(x["group"]["visibility"] == 1 for x in spec["structure_groups"]))

    def test_reject_wider_window(self):
        """A wider window cannot quietly change the registered experiment."""
        with self.assertRaises(ValueError):
            P.edit_spec({"vhh_start_one_based": 51, "vhh_end_one_based": 57}, "LOCAL_INPAINT")

    def test_both_cpu_contracts_pass(self):
        """Both intended mechanisms preserve the fixed sequence and token count."""
        for arm in P.ARMS[1:]:
            arrays, masked = self.arrays(arm)
            self.assertEqual(P.validate_cpu(arrays, masked, self.ids, self.edit, arm)["status"], "PASS")

    def test_reject_framework_identity_change(self):
        """An unintended framework mutation blocks GPU execution."""
        arrays, masked = self.arrays("LOCAL_INPAINT")
        arrays["res_type"][40] = np.eye(33)[3]
        with self.assertRaisesRegex(ValueError, "fixed sequence"):
            P.validate_cpu(arrays, masked, self.ids, self.edit, "LOCAL_INPAINT")

    def test_reject_full_cdr_design_mask(self):
        """A full-CDR annotation cannot be confused with the five-token edit mask."""
        arrays, masked = self.arrays("LOCAL_INPAINT")
        arrays["design_mask"][70] = True
        with self.assertRaisesRegex(ValueError, "five residues"):
            P.validate_cpu(arrays, masked, self.ids, self.edit, "LOCAL_INPAINT")

    def test_reject_binding_labels(self):
        """No arm may silently add terminal hotspots."""
        arrays, masked = self.arrays("SEQUENCE_ONLY")
        arrays["binding_type"][0] = 1
        with self.assertRaisesRegex(ValueError, "binding labels"):
            P.validate_cpu(arrays, masked, self.ids, self.edit, "SEQUENCE_ONLY")

    def test_reject_inpaint_visible_window(self):
        """Inpainting must not use sequence-only structural visibility."""
        arrays, masked = self.arrays("SEQUENCE_ONLY")
        with self.assertRaisesRegex(ValueError, "conditioning group"):
            P.validate_cpu(arrays, masked, self.ids, self.edit, "LOCAL_INPAINT")

    def test_selection_is_cdr_agnostic_and_deterministic(self):
        """Nearest windows may be CDR1; no CDR3 prior or outcome selection is used."""
        coords = np.array([[1.+i*4., 1., 1.] for i in range(151)])
        coords[30:35, 0] = 3
        fold = {"coords": coords[None], "atom_to_token": np.eye(151)[None]}
        selected = P.select_window([fold, fold], [[1, 8], [20, 26], [96, 110]])
        self.assertEqual(selected["selected"]["cdr_index"], 1)
        self.assertEqual(selected["selected"]["vhh_start_one_based"], 1)
        self.assertEqual(selected["candidate_window_count"], 18)
        self.assertEqual(selected, P.select_window([fold, fold], [[1, 8], [20, 26], [96, 110]]))

    def test_selection_rejects_placeholder_atoms(self):
        """Dummy atom coordinates must not influence geometric window selection."""
        fold = {"coords": np.zeros((1, 151, 3)), "atom_to_token": np.eye(151)[None]}
        with self.assertRaisesRegex(ValueError, "non-placeholder"):
            P.select_window([fold, fold], [[1, 8]])


if __name__ == "__main__":
    unittest.main()
