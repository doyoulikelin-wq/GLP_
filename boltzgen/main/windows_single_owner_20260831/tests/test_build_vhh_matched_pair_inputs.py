"""Tests for identity-preserving active and matched-deletion input construction."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import gemmi
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("build_vhh_matched_pair_inputs", ROOT / "scripts/build_vhh_matched_pair_inputs.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
NAMES = dict(zip("ACDEFGHIKLMNPQRSTVWY", "ALA CYS ASP GLU PHE GLY HIS ILE LYS LEU MET ASN PRO GLN ARG SER THR VAL TRP TYR".split()))


def fixture(vhh="ACDEFGHIKLMNPQRSTVWY"):
    """Synthetic short test chain; not a scientific candidate or inference input."""
    structure, model = gemmi.Structure(), gemmi.Model("1")
    structure.name = "synthetic_unit_test"
    for chain_id, sequence, entity_id in (("A", MODULE.ACTIVE_GLP1, "1"), ("B", vhh, "2")):
        chain = gemmi.Chain(chain_id)
        for i, letter in enumerate(sequence):
            residue = gemmi.Residue()
            residue.name, residue.seqid = NAMES[letter], gemmi.SeqId(i + 1, " ")
            residue.entity_type = gemmi.EntityType.Polymer
            residue.subchain, residue.entity_id = chain_id, entity_id
            for name, element, dx in (("N", "N", 0), ("CA", "C", 1), ("C", "C", 2), ("O", "O", 3)):
                atom = gemmi.Atom()
                atom.name, atom.element = name, gemmi.Element(element)
                atom.pos = gemmi.Position(i * 3.8 + dx, (i % 3) * 0.2, 10 if chain_id == "B" else 0)
                residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
        entity = gemmi.Entity(entity_id)
        entity.entity_type, entity.polymer_type = gemmi.EntityType.Polymer, gemmi.PolymerType.PeptideL
        entity.full_sequence, entity.subchains = [NAMES[letter] for letter in sequence], [chain_id]
        structure.entities.append(entity)
    structure.add_model(model)
    structure.assign_label_seq_id(force=True)
    n = 30 + len(vhh)
    metadata = {name: np.zeros(n, dtype=np.float32) for name in MODULE.load_metadata.__globals__["METADATA_KEYS"]}
    metadata["token_resolved_mask"][:] = 1
    metadata["token_resolved_mask"][34] = 0
    metadata["design_mask"][[32, 34, n - 1]] = 1
    metadata["binding_type"][:2] = 1
    return structure, metadata


class MatchedPairInputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        for name in ("boltz2_conf_final.ckpt", "mols.zip"):
            (self.runtime / name).write_bytes(b"test_fixture_not_real_runtime")
        (self.runtime / "SHA256SUMS").write_text("\n".join("0" * 64 + "  " + name for name in ("boltz2_conf_final.ckpt", "mols.zip")) + "\n")

    def write_candidate(self, name="cell", vhh="ACDEFGHIKLMNPQRSTVWY"):
        directory = self.root / name
        directory.mkdir()
        structure, metadata = fixture(vhh)
        (directory / "design_0.cif").write_text(structure.make_mmcif_document().as_string())
        MODULE.write_npz(directory / "design_0.npz", metadata)
        return directory

    def test_exact_deletion_and_metadata_index_remap(self):
        structure, metadata = fixture()
        text, result, mapping = MODULE.paired_state(structure, metadata, 2, "test_truncated")
        self.assertEqual(mapping["target_token_count"], 28)
        self.assertEqual(mapping["deleted_source_token_indices"], [0, 1])
        self.assertEqual(mapping["output_to_source_token_indices"], list(range(2, 50)))
        self.assertEqual(mapping["designed_token_indices"], [30, 32, 47])
        self.assertEqual(mapping["binding_token_indices"], [])
        self.assertEqual(mapping["target_biological_residue_numbers"], list(range(9, 37)))
        for key in metadata:
            np.testing.assert_array_equal(result[key], metadata[key][2:])
        parsed = gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block())
        self.assertEqual(parsed[0][0][0].name, "GLU")
        self.assertEqual(str(parsed[0][0][0].seqid), "1")
        self.assertEqual(len(parsed.get_entity("1").full_sequence), 28)

    def test_active_and_truncated_preserve_same_vhh_sequence(self):
        structure, metadata = fixture()
        active = MODULE.paired_state(structure, metadata, 0, "test_active")
        truncated = MODULE.paired_state(structure, metadata, 2, "test_truncated")
        self.assertEqual(active[2]["vhh_sequence_sha256"], truncated[2]["vhh_sequence_sha256"])
        self.assertEqual(active[2]["binding_token_indices"], [0, 1])
        self.assertEqual(len(structure[0][0]), 30)
        np.testing.assert_array_equal(metadata["binding_type"][:2], [1, 1])

    def test_wrong_his_ala_identity_rejected(self):
        structure, metadata = fixture()
        structure[0][0][0].name = "ARG"
        with self.assertRaisesRegex(ValueError, "His7/Ala8/Glu9"):
            MODULE.paired_state(structure, metadata, 2, "bad")

    def test_mismatched_metadata_lengths_rejected(self):
        structure, metadata = fixture()
        metadata["binding_type"] = metadata["binding_type"][:-1]
        with self.assertRaisesRegex(ValueError, "token lengths"):
            MODULE.paired_state(structure, metadata, 2, "bad")

    def test_wrong_deletion_rejected(self):
        structure, metadata = fixture()
        with self.assertRaisesRegex(ValueError, "exact His7/Ala8"):
            MODULE.paired_state(structure, metadata, 1, "bad")

    def test_end_to_end_two_states_two_folds_without_gpu(self):
        source = self.write_candidate()
        output = self.root / "out"
        before = {path.name: MODULE.stable_digest(path) for path in source.iterdir()}
        report = MODULE.build_inputs([source], output, self.runtime)
        self.assertEqual(report["expected_fold_samples"], 4)
        self.assertFalse(report["gpu_started"])
        self.assertFalse(report["lockbox_read"])
        self.assertEqual(report["terminal_chemistry_status"], "NOT_ATOMICALLY_VERIFIED")
        config = yaml.safe_load((output / "folding.yaml").read_text())
        self.assertEqual(config["diffusion_samples"], 2)
        self.assertTrue(config["data"]["target_templates"])
        self.assertFalse(config["data"]["design_mask_templates"])
        self.assertEqual(before, {path.name: MODULE.stable_digest(path) for path in source.iterdir()})
        saved = json.loads((output / "MATCHED_PAIR_INPUTS.json").read_text())
        self.assertEqual(saved["candidate_set_sha256"], MODULE.candidate_set_digest(saved["candidates"]))

    def test_multiple_cells_get_unambiguous_global_ids(self):
        first = self.write_candidate("first")
        second = self.write_candidate("second", "ACDEFGHIKLMNPQRSTVWA")
        result = MODULE.build_inputs([first, second], self.root / "out", self.runtime, ["7xl0", "6apo"])
        self.assertEqual(result["expected_fold_samples"], 8)
        self.assertEqual([row["candidate_id"] for row in result["candidates"]], ["6apo_design_0", "7xl0_design_0"])

    def test_existing_output_not_reused(self):
        source = self.write_candidate()
        output = self.root / "out"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "new directory"):
            MODULE.build_inputs([source], output, self.runtime)

    def test_output_inside_source_rejected(self):
        source = self.write_candidate()
        with self.assertRaisesRegex(ValueError, "inside"):
            MODULE.build_inputs([source], source / "out", self.runtime)

    def test_duplicate_sequences_not_counted_as_diverse_candidates(self):
        first, second = self.write_candidate("first"), self.write_candidate("second")
        with self.assertRaisesRegex(ValueError, "unique"):
            MODULE.build_inputs([first, second], self.root / "out", self.runtime, ["first", "second"])
        self.assertFalse((self.root / "out").exists())

    def test_multiple_roots_require_explicit_prefixes(self):
        first, second = self.write_candidate("first"), self.write_candidate("second")
        with self.assertRaisesRegex(ValueError, "prefix"):
            MODULE.build_inputs([first, second], self.root / "out", self.runtime)


if __name__ == "__main__":
    unittest.main()
