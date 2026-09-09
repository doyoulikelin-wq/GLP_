"""Synthetic geometry, atom mapping and publication regression checks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_vhh_epitope.py"
SPEC = importlib.util.spec_from_file_location("evaluate_vhh_epitope", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture(cdr_x=3.0, samples=2, padding=3):
    # HIS, ALA, GLY target; ALA designed CDR; GLY framework. All canonical
    # heavy slots are present; coincident within-residue atoms simplify tests.
    ids = np.asarray([10, 2, 9, 2, 9])
    counts = M.HEAVY_COUNTS[ids - 2]
    token = np.repeat(np.arange(5), counts)
    n = len(token) + padding
    mapping = np.zeros((1, n, 5), dtype=bool)
    mapping[0, np.arange(len(token)), token] = True
    centres = np.asarray([[0, 0, 0], [0, 2, 0], [20, 0, 0], [cdr_x, 0, 0], [40, 0, 0]], dtype=float)
    coords = np.zeros((samples, n, 3))
    coords[:, :len(token), :] = centres[token]
    coords[:, len(token):, :] = [1, 0, 0]  # padding close to HIS, must be ignored
    res_type = np.zeros((1, 5, 33), dtype=int)
    res_type[0, np.arange(5), ids] = 1
    design = {"design_mask": np.asarray([0, 0, 0, 1, 0]), "token_resolved_mask": np.asarray([1, 1, 1, 0, 1])}
    fold = {"coords": coords, "atom_to_token": mapping, "atom_resolved_mask": mapping.any(axis=2), "token_index": np.arange(5)[None, :], "res_type": res_type, "mol_type": np.zeros((1, 5), dtype=int)}
    return design, fold


def evaluate(design, fold, **kwargs):
    return M.evaluate_arrays(design, fold, target_tokens=[0, 1, 2], **kwargs)


class EpitopeTests(unittest.TestCase):
    def test_contacts_and_residue_pair_definition(self):
        rows = evaluate(*fixture())
        self.assertTrue(rows[0]["both_epitope_contacts"])
        self.assertEqual(rows[0]["target_cdr_residue_pair_count"], 2)
        self.assertEqual(rows[0]["participating_cdr_residue_count"], 1)
        self.assertFalse(rows[0]["severe_close_contact_diagnostic"])

    def test_developability_descriptors_are_composition_only(self):
        design, fold = fixture()
        evaluate(design, fold)
        values = M.developability_descriptors(design, fold)
        self.assertEqual(values, {"cdr_residue_count": 1, "cdr_cysteine_count": 0, "cdr_hydrophobic_residue_fraction": 1.0, "cdr_kr_minus_de_charge_proxy": 0})

    def test_wrong_epitope_still_has_anywhere_contact(self):
        row = evaluate(*fixture(cdr_x=18))[0]
        self.assertTrue(row["any_target_cdr_contact"])
        self.assertFalse(row["his_contact"])
        self.assertFalse(row["ala_contact"])

    def test_rigid_rotation_translation_invariance(self):
        design, fold = fixture()
        original = evaluate(design, fold)
        rotation = np.asarray([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        fold["coords"] = fold["coords"] @ rotation + [70, -2, 19]
        transformed = evaluate(design, fold)
        for left, right in zip(original, transformed):
            for field in M.NUMERIC_METRICS:
                self.assertAlmostEqual(left[field], right[field])
            self.assertEqual(left["contact_residue_pairs_zero_based"], right["contact_residue_pairs_zero_based"])

    def test_padding_is_not_a_his_atom(self):
        row = evaluate(*fixture(cdr_x=10))[0]
        self.assertEqual(row["min_target_cdr_distance_angstrom"], 10)
        self.assertFalse(row["any_target_cdr_contact"])

    def test_predicted_cdr_sidechain_not_discarded(self):
        design, fold = fixture(cdr_x=10)
        cdr_atoms = np.flatnonzero(fold["atom_to_token"][0, :, 3])
        fold["coords"][:, cdr_atoms[-1], :] = [3, 0, 0]
        self.assertEqual(design["token_resolved_mask"][3], 0)
        self.assertTrue(evaluate(design, fold)[0]["his_contact"])

    def test_contact_includes_threshold_boundary(self):
        self.assertTrue(evaluate(*fixture(cdr_x=4.5))[0]["his_contact"])
        self.assertFalse(evaluate(*fixture(cdr_x=4.500001))[0]["his_contact"])

    def test_severe_distance_is_strict_diagnostic(self):
        self.assertFalse(evaluate(*fixture(cdr_x=1.5))[0]["severe_close_contact_diagnostic"])
        self.assertTrue(evaluate(*fixture(cdr_x=1.499))[0]["severe_close_contact_diagnostic"])

    def test_empty_contact_jaccard_is_undefined_not_perfect(self):
        summary = M.candidate_summary(evaluate(*fixture(cdr_x=10)))
        jaccard = summary["within_candidate_contact_map_jaccard"]
        self.assertEqual(jaccard["both_empty_undefined_pair_count"], 1)
        self.assertIsNone(jaccard["values_summary"]["median"])
        self.assertNotIn("NaN", json.dumps(summary, allow_nan=False))

    def test_one_empty_map_has_zero_jaccard(self):
        rows = [evaluate(*fixture())[0], evaluate(*fixture(cdr_x=10))[0]]
        self.assertEqual(M.candidate_summary(rows)["within_candidate_contact_map_jaccard"]["values_summary"]["median"], 0)

    def test_single_sample_has_no_pairs(self):
        result = M.candidate_summary(evaluate(*fixture(samples=1)))
        self.assertEqual(result["within_candidate_contact_map_jaccard"]["pair_count"], 0)

    def test_invalid_masks_and_shapes_fail_closed(self):
        changes = [
            ("atom_to_token", lambda a: a.__setitem__((0, 0, 1), True)),
            ("atom_resolved_mask", lambda a: a.__setitem__((0, 0), False)),
            ("atom_resolved_mask", lambda a: a.__setitem__((0, -1), True)),
            ("coords", lambda a: a.__setitem__((0, 0, 0), np.nan)),
            ("coords", lambda a: a.__setitem__((0, -1, 0), np.inf)),
            ("token_index", lambda a: a.__setitem__((0, 0), 9)),
            ("mol_type", lambda a: a.__setitem__((0, 0), 1)),
            ("res_type", lambda a: a.__setitem__((0, 0, 10), 0)),
        ]
        for field, change in changes:
            with self.subTest(field=field):
                design, fold = fixture()
                change(fold[field])
                with self.assertRaises(M.ValidationError):
                    evaluate(design, fold)
        design, fold = fixture()
        fold["coords"] = fold["coords"][0]
        with self.assertRaises(M.ValidationError):
            evaluate(design, fold)
        design, fold = fixture()
        fold["coords"] = fold["coords"].astype(complex)
        with self.assertRaises(M.ValidationError):
            evaluate(design, fold)

    def test_incomplete_heavy_atoms_and_wrong_epitope_fail(self):
        design, fold = fixture()
        fold["atom_to_token"][0, 0] = False
        fold["atom_resolved_mask"][0, 0] = False
        with self.assertRaises(M.ValidationError):
            evaluate(design, fold)
        with self.assertRaises(M.ValidationError):
            evaluate(*fixture(), his_token=2)

    def test_invalid_target_and_threshold_fail(self):
        design, fold = fixture()
        for tokens in ([], [0, 0, 1], [0, 1, 3], [0, 1, 999], [0.0, 1.0]):
            with self.subTest(tokens=tokens), self.assertRaises(M.ValidationError):
                M.evaluate_arrays(design, fold, target_tokens=tokens)
        for threshold in (0, -1, np.nan, np.inf):
            with self.assertRaises(M.ValidationError):
                evaluate(design, fold, threshold=threshold)

    def write_fixture(self, root):
        root.mkdir()
        (root / "fold_out_npz").mkdir()
        design, fold = fixture()
        np.savez(root / "design_0.npz", **design)
        np.savez(root / "fold_out_npz" / "design_0.npz", **fold)

    def test_new_outputs_public_allowlist_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, output = base / "private_input", base / "result"
            self.write_fixture(root)
            result = M.execute(root, output, "sensitive_candidate_label", target_tokens=[0, 1, 2], expected_candidates=1, expected_samples=2)
            public = (output / "PUBLIC_EPITOPE_SUMMARY.json").read_text()
            for secret in ("private_input", "design_0", "sensitive_candidate_label", str(root)):
                self.assertNotIn(secret, public)
            self.assertEqual(result["analysis_classification"], "RETROSPECTIVE_EXPLORATORY")
            self.assertEqual(result["evaluator_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
            self.assertEqual(result["method"], "custom_method")
            private = json.loads((output / "PRIVATE_EPITOPE_EVALUATION.json").read_text())
            self.assertEqual(len(private["inputs"]), 2)
            self.assertEqual(len(private["inputs"][0]["sha256"]), 64)
            for item in private["inputs"]:
                self.assertNotIn(item["sha256"], public)
            with self.assertRaises(M.ValidationError):
                M.execute(root, output, "exploratory", target_tokens=[0, 1, 2])

    def test_missing_fold_wrong_count_and_source_output_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "input"
            self.write_fixture(root)
            for kwargs in ({"expected_samples": 5}, {"expected_candidates": 6}):
                with self.assertRaises(M.ValidationError):
                    M.execute(root, base / "output", "exploratory", target_tokens=[0, 1, 2], **kwargs)
            with self.assertRaises(M.ValidationError):
                M.execute(root, root / "new", "exploratory", target_tokens=[0, 1, 2])
            (root / "fold_out_npz" / "design_0.npz").unlink()
            with self.assertRaises(M.ValidationError):
                M.execute(root, base / "output", "exploratory", target_tokens=[0, 1, 2])
            self.assertFalse((base / "output").exists())


if __name__ == "__main__":
    unittest.main()
