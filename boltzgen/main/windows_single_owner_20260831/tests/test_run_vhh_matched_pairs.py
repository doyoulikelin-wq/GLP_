"""Tests for matched-state identity, stage prerequisites and result boundaries."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_vhh_matched_pair_inputs import fixture
import build_vhh_matched_pair_inputs as BUILDER
SPEC = importlib.util.spec_from_file_location("run_vhh_matched_pairs", ROOT / "scripts/run_vhh_matched_pairs.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class MatchedRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        source, runtime = self.base / "source", self.base / "runtime"
        source.mkdir(); runtime.mkdir()
        structure, metadata = fixture()
        (source / "design_0.cif").write_text(structure.make_mmcif_document().as_string())
        BUILDER.write_npz(source / "design_0.npz", metadata)
        for name in ("boltz2_conf_final.ckpt", "mols.zip"):
            (runtime / name).write_bytes(b"synthetic_test_asset_not_model")
        (runtime / "SHA256SUMS").write_text("\n".join("0"*64+"  "+name for name in ("boltz2_conf_final.ckpt", "mols.zip"))+"\n")
        self.prepared = self.base / "prepared"
        self.preparation = BUILDER.build_inputs([source], self.prepared, runtime)
        self.attempt = self.base / "attempt"
        designs = self.attempt / "intermediate_designs"
        shutil.copytree(self.prepared / "design_inputs", designs)
        (designs / "fold_out_npz").mkdir(); (designs / "refold_cif").mkdir()
        for state in self.preparation["candidates"][0]["states"]:
            count, task = state["target_token_count"], state["task_id"]
            sequence = (M.ACTIVE_GLP1 if count == 30 else M.ACTIVE_GLP1[2:])+"ACDEFGHIKLMNPQRSTVWY"
            ids = np.array([M.ONE_LETTER.index(letter)+2 for letter in sequence])
            counts = M.geometry.HEAVY_COUNTS[ids-2]
            token = np.repeat(np.arange(len(ids)), counts)
            mapping = np.zeros((1,len(token),len(ids)), dtype=bool)
            mapping[0,np.arange(len(token)),token] = True
            centres = np.array([[2*(i+(7 if count==30 else 9)),0,0] if i<count else [18+(i-count)%2,3,0] for i in range(len(ids))], dtype=float)
            res_type = np.zeros((1,len(ids),33), dtype=int)
            res_type[0,np.arange(len(ids)),ids] = 1
            fold = {"coords": np.stack([centres[token],centres[token]]), "atom_to_token": mapping,
                    "atom_resolved_mask": mapping.any(axis=2), "res_type":res_type,
                    "token_index":np.arange(len(ids))[None,:], "mol_type":np.zeros((1,len(ids)),dtype=int)}
            fold.update({metric:np.array([.7,.8]) for metric in ("iptm","ptm","design_to_target_iptm","design_ptm")})
            np.savez_compressed(designs / "fold_out_npz" / f"{task}.npz", **fold)
            shutil.copyfile(designs / f"{task}.cif", designs / "refold_cif" / f"{task}.cif")
        self.bindings = {M.STAGE:{"candidate_set_sha256":self.preparation["candidate_set_sha256"]}}
        self.prerequisites = [{"stage_id":"diversified_pilot","actual_candidate_set_sha256":self.preparation["candidate_set_sha256"]}]

    def mutate_fold(self, action, task="design_0_truncated"):
        path = self.attempt / "intermediate_designs/fold_out_npz" / f"{task}.npz"
        with np.load(path, allow_pickle=False) as data:
            fold = {key:data[key] for key in data.files}
        action(fold)
        np.savez_compressed(path, **fold)

    def test_real_geometry_two_states_have_explicit_epitope_applicability(self):
        private, public = M.score_outputs(self.attempt,self.preparation)
        active,truncated = private["candidates"][0]["states"]
        self.assertEqual(active["epitope_status"],"HIS7_ALA8_PRESENT")
        self.assertEqual(truncated["epitope_status"],"ABSENT_NOT_APPLICABLE")
        self.assertIsNone(truncated["summary"]["counts"]["his_contact"])
        self.assertIsNone(truncated["samples"][0]["ala_min_cdr_distance_angstrom"])
        self.assertEqual(public["fold_sample_count"],4)
        self.assertFalse(public["biological_pass"])
        self.assertEqual(private["actual_candidate_set_sha256"],self.preparation["candidate_set_sha256"])

    def test_shared_contacts_use_biological_and_vhh_local_indices(self):
        private,_ = M.score_outputs(self.attempt,self.preparation)
        candidate = private["candidates"][0]
        a,t = candidate["states"]
        self.assertEqual(a["samples"][0]["shared_segment_contact_pairs"],t["samples"][0]["shared_segment_contact_pairs"])
        self.assertEqual(candidate["shared_segment_cross_state_jaccard"]["median"],1.0)
        self.assertEqual(candidate["shared_segment_contact_change_truncated_minus_active"],0)
        self.assertTrue(all(pair[0]>=9 for pair in a["samples"][0]["shared_segment_contact_pairs"]))

    def test_public_summary_has_no_candidate_ids_hashes_or_paths(self):
        _,public = M.score_outputs(self.attempt,self.preparation)
        text = json.dumps(public,allow_nan=False)
        for secret in ("design_0",str(self.base),self.preparation["candidate_set_sha256"],"ACDEFGHIKLMNPQRSTVWY"):
            self.assertNotIn(secret,text)
        self.assertNotIn("confidence_descriptive_only",text)

    def test_wrong_output_sequence_rejected(self):
        self.preparation["candidates"][0]["vhh_sequence_sha256"] = "a"*64
        with self.assertRaisesRegex(M.RunFailure,"sequence"):
            M.score_outputs(self.attempt,self.preparation)

    def test_nonfinite_coordinate_rejected(self):
        self.mutate_fold(lambda f:f["coords"].__setitem__((0,0,0),np.nan))
        with self.assertRaises(ValueError): M.score_outputs(self.attempt,self.preparation)

    def test_incomplete_repeats_rejected(self):
        self.mutate_fold(lambda f:f.__setitem__("coords",f["coords"][:1]))
        with self.assertRaisesRegex(M.RunFailure,"exactly two"):
            M.score_outputs(self.attempt,self.preparation)

    def test_invalid_confidence_rejected_without_selectivity_calculation(self):
        self.mutate_fold(lambda f:f.__setitem__("iptm",np.array([np.nan,.4])))
        with self.assertRaisesRegex(M.RunFailure,"confidence"):
            M.score_outputs(self.attempt,self.preparation)

    def test_output_closure_rejected(self):
        (self.attempt / "intermediate_designs/refold_cif/design_0_active.cif").unlink()
        with self.assertRaisesRegex(M.RunFailure,"closure"):
            M.score_outputs(self.attempt,self.preparation)

    def test_stage_blocked_cannot_start_pairing(self):
        with patch.object(M,"evaluate",return_value={"status":"BLOCKED","next_stage":"diversified_pilot"}):
            with self.assertRaisesRegex(M.RunFailure,"stage gate"):
                M.require_ready({},self.bindings,self.prerequisites,self.preparation)

    def test_actual_pilot_output_identity_required(self):
        self.bindings[M.STAGE]["candidate_set_sha256"] = "a"*64
        with patch.object(M,"evaluate",return_value={"status":"READY","next_stage":M.STAGE}):
            with self.assertRaisesRegex(M.RunFailure,"pilot actual output"):
                M.require_ready({},self.bindings,self.prerequisites,self.preparation)

    def test_ready_checks_full_task_count(self):
        with patch.object(M,"evaluate",return_value={"status":"READY","next_stage":M.STAGE}):
            self.assertEqual(M.require_ready({},self.bindings,self.prerequisites,self.preparation)["next_stage"],M.STAGE)
            self.preparation["expected_fold_samples"]=2
            with self.assertRaisesRegex(M.RunFailure,"sample counts"):
                M.require_ready({},self.bindings,self.prerequisites,self.preparation)

    def test_changed_prepared_input_rejected(self):
        M.verify_preparation(self.prepared,self.preparation)
        (self.prepared / "design_inputs/design_0_active.cif").write_text("changed")
        with self.assertRaisesRegex(M.RunFailure,"hash mismatch"):
            M.verify_preparation(self.prepared,self.preparation)


if __name__ == "__main__": unittest.main()
