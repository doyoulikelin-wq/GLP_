"""Synthetic coordinate tests distinguish shape change from relative rigid pose."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import diagnose_vhh_terminal_pose_20260913 as P


class TerminalPoseDiagnosticTests(unittest.TestCase):
    """Use complete CA arrays but no biological conclusions from fixtures."""

    def setUp(self):
        """Create a nondegenerate reference in a reproducible coordinate frame."""
        self.source = np.random.default_rng(173).normal(size=(151, 3))*5+20
        self.target = self.source[:30].copy()

    def test_global_rigid_transform_changes_no_descriptor(self):
        """An arbitrary complex rotation and translation must disappear."""
        rotation = np.array([[0., 1, 0], [-1, 0, 0], [0, 0, 1]])
        pred = self.source@rotation+[200., -31., 10.]
        metrics = P.geometry(pred, self.target, self.source)
        for key, value in metrics.items():
            self.assertLess(value, 2e-5, key)

    def test_relative_vhh_translation_is_not_shape_deformation(self):
        """Translating only VHH changes its relative pose, not its fold."""
        pred = self.source.copy()
        pred[30:] += [3., 4., 0.]
        metrics = P.geometry(pred, self.target, self.source)
        self.assertAlmostEqual(metrics["vhh_framework_ca_rmsd_after_target_central_fit_angstrom"], 5.)
        self.assertAlmostEqual(metrics["vhh_framework_centroid_displacement_after_target_fit_angstrom"], 5.)
        self.assertLess(metrics["vhh_framework_ca_rmsd_after_framework_fit_angstrom"], 1e-10)
        self.assertLess(metrics["vhh_full_cdr_ca_rmsd_after_framework_fit_angstrom"], 1e-10)
        self.assertLess(metrics["target_all_ca_rmsd_after_central_fit_angstrom"], 1e-10)

    def test_terminal_shape_change_is_separate(self):
        """Moving His/Ala while the central target stays fixed affects terminal metrics."""
        pred = self.source.copy()
        pred[:2] += [0., 0., 7.]
        metrics = P.geometry(pred, self.target, self.source)
        self.assertAlmostEqual(metrics["target_his_ala_ca_rmsd_after_central_fit_angstrom"], 7.)
        self.assertLess(metrics["target_central_ca_rmsd_angstrom"], 1e-10)
        self.assertLess(metrics["vhh_framework_ca_rmsd_after_target_central_fit_angstrom"], 1e-10)

    def test_cdr_deformation_not_framework_pose(self):
        """A CDR-only displacement remains after independent framework alignment."""
        pred = self.source.copy()
        pred[P.CDR] += [2., 0., 0.]
        metrics = P.geometry(pred, self.target, self.source)
        self.assertAlmostEqual(metrics["vhh_full_cdr_ca_rmsd_after_framework_fit_angstrom"], 2.)
        self.assertLess(metrics["vhh_framework_ca_rmsd_after_target_central_fit_angstrom"], 1e-10)
        self.assertLess(metrics["vhh_framework_ca_rmsd_after_framework_fit_angstrom"], 1e-10)

    def test_replacement_preserves_vhh_and_original_arrays(self):
        """The hypothetical target operation must not move or overwrite VHH atoms."""
        pred = self.source.copy()
        pred[:2] += [0., 0., 7.]
        before = pred.copy()
        atoms = {(i, "CA"): self.target[i] for i in range(30)}
        changed = P.replace_target(pred, np.ones(151, bool), np.arange(151),
            np.array(["CA"]*151), atoms, self.target)
        np.testing.assert_array_equal(pred, before)
        np.testing.assert_array_equal(changed[30:], pred[30:])
        np.testing.assert_allclose(changed[:30], self.target, atol=1e-12)

    def test_replacement_requires_each_named_reference_atom(self):
        """Missing atom identities cannot be filled by guessed coordinates."""
        atoms = {(i, "CA"): self.target[i] for i in range(1, 30)}
        with self.assertRaisesRegex(ValueError, "atom missing"):
            P.replace_target(self.source, np.ones(151, bool), np.arange(151),
                np.array(["CA"]*151), atoms, self.target)

    def test_degenerate_alignment_rejected(self):
        """Collinear points cannot establish a complete rigid orientation."""
        values = np.tile([1., 0., 0.], (151, 1))
        with self.assertRaisesRegex(ValueError, "degenerate"):
            P.geometry(values, values[:30], values)

    def test_all_rows_preserved_in_contact_counterfactual_summary(self):
        """Restored and lost hypothetical contacts retain their actual denominators."""
        metrics = {key: 0. for key in P.GEOMETRY_KEYS}
        rows = [{"source_draw": 0, "geometry": metrics,
            "observed_contacts": {"both_epitope_contacts": a},
            "hypothetical_target_replacement_contacts": {"both_epitope_contacts": b}}
            for a, b in ((True, False), (False, True), (False, False))]
        summary = P.summarize(rows)
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["observed_double_contact_count"], 1)
        self.assertEqual(summary["hypothetical_double_contact_count"], 1)
        self.assertEqual(summary["hypothetical_restored_double_contact_count"], 1)
        self.assertEqual(summary["hypothetical_lost_double_contact_count"], 1)


if __name__ == "__main__":
    unittest.main()
