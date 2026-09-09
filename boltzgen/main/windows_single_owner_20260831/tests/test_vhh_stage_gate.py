"""Regression checks for evidence-bound VHH workflow stage transitions."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vhh_stage_gate", ROOT / "scripts/vhh_stage_gate.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class VHHStageGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.result = Path(self.tmp.name) / "result.json"
        self.candidates = [{"candidate_id": f"test_{i}", "vhh_sequence_sha256": hashlib.sha256(f"synthetic_sequence_identity_{i}".encode()).hexdigest()} for i in range(12)]
        self.actual_candidate_digest = GATE.candidate_set_digest(self.candidates)
        self.result.write_text(json.dumps({"kind": "synthetic_test_fixture_not_scientific_evidence",
                                          "candidates": self.candidates,
                                          "actual_candidate_set_sha256": self.actual_candidate_digest}), encoding="utf-8")
        self.contract = json.loads((ROOT / "configs/vhh_revision_20260909.json").read_text())
        self.now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
        self.bindings = {stage["id"]: {"source_sha256": "a" * 64} for stage in self.contract["stages"]}
        self.bindings["native_free_refold"]["calibration_thresholds"] = self.contract["stages"][1]["calibration_thresholds"]
        self.bindings["active_truncated_pairing"]["candidate_set_sha256"] = self.actual_candidate_digest
        self.receipts = []
        for stage in self.contract["stages"]:
            actual = {"execution_device": "cuda"}
            for key, value in stage["budget"].items():
                if key.startswith("max_"):
                    actual[key[4:]] = value
            actual["wall_seconds"] = 100
            actual.update(scaffolds=2, cdr3_classes=2, pose_classes=2)
            self.receipts.append({
                "schema": GATE.SCHEMA, "protocol_id": self.contract["protocol_id"],
                "protocol_sha256": GATE.digest(self.contract), "stage_id": stage["id"],
                "input_binding_sha256": GATE.digest(self.bindings[stage["id"]]),
                "status": "COMPLETED", "evidence_kind": stage["evidence_kind"],
                "registration": stage["registration"], "created_at_utc": "2026-09-09T11:00:00Z",
                "preregistered_at_utc": "2026-09-09T09:00:00Z", "started_at_utc": "2026-09-09T10:00:00Z",
                "checks": {key: True for key in stage["required_checks"]},
                "actual": actual, "biological_pass": False,
                "result_artifacts": [{"path": str(self.result), "sha256": hashlib.sha256(self.result.read_bytes()).hexdigest()}]
            })
        thresholds = self.contract["stages"][1]["calibration_thresholds"]
        self.receipts[1]["calibration"] = {"thresholds": thresholds, "samples": [
            {"sample_index": i, "target_aligned_binder_ca_rmsd_angstrom": 2.0,
             "native_heavy_residue_contact_recall": 0.7} for i in range(2)]}
        self.receipts[3]["target_states"] = self.contract["pairing"]["primary_states"]
        self.receipts[3]["terminal_chemistry_status"] = "NOT_ATOMICALLY_VERIFIED"
        self.receipts[2]["actual_candidate_set_sha256"] = self.actual_candidate_digest

    def evaluate(self, receipts=None):
        return GATE.evaluate(self.contract, self.bindings, self.receipts if receipts is None else receipts, self.now)

    def assert_blocked(self, stage):
        result = self.evaluate()
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["next_stage"], stage)
        self.assertFalse(result["gpu_started"])

    def test_no_receipt_only_first_stage_ready(self):
        result = self.evaluate([])
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["next_stage"], "existing_fold_reassessment")
        self.assertEqual(result["stages"][1]["state"], "NOT_EVALUATED")

    def test_cpu_complete_does_not_complete_native(self):
        result = self.evaluate(self.receipts[:1])
        self.assertEqual(result["next_stage"], "native_free_refold")
        self.assertEqual(result["status"], "READY")

    def test_all_technical_complete_is_never_biological_pass(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertFalse(result["biological_pass"])
        self.assertFalse(result["selectivity_claim_allowed"])

    def test_cpu_self_control_is_not_native_gpu(self):
        self.receipts[1]["evidence_kind"] = "cpu_native_self_check"
        self.assert_blocked("native_free_refold")

    def test_actual_cpu_device_cannot_claim_gpu(self):
        self.receipts[1]["actual"]["execution_device"] = "cpu"
        self.assert_blocked("native_free_refold")

    def test_native_cross_chain_template_leak_blocks(self):
        self.receipts[1]["checks"]["no_native_cross_chain_template"] = False
        self.assert_blocked("native_free_refold")

    def test_native_two_samples_required(self):
        self.receipts[1]["actual"]["fold_samples"] = 1
        self.assert_blocked("native_free_refold")

    def test_cpu_all_sixty_samples_required(self):
        self.receipts[0]["actual"]["samples"] = 30
        self.assert_blocked("existing_fold_reassessment")

    def test_native_boolean_cannot_hide_failed_coordinate_metric(self):
        self.receipts[1]["calibration"]["samples"][1]["target_aligned_binder_ca_rmsd_angstrom"] = 12.0
        self.assert_blocked("native_free_refold")

    def test_native_nan_metric_blocks(self):
        self.receipts[1]["calibration"]["samples"][0]["native_heavy_residue_contact_recall"] = float("nan")
        self.assert_blocked("native_free_refold")

    def test_native_changed_threshold_blocks(self):
        self.receipts[1]["calibration"] = copy.deepcopy(self.receipts[1]["calibration"])
        self.receipts[1]["calibration"]["thresholds"]["target_aligned_binder_ca_rmsd_angstrom"]["value"] = 20.0
        self.assert_blocked("native_free_refold")

    def test_unmatched_new_candidates_cannot_inherit_old_evidence(self):
        self.bindings["active_truncated_pairing"]["candidate_set_sha256"] = "c" * 64
        self.receipts[3]["input_binding_sha256"] = GATE.digest(self.bindings["active_truncated_pairing"])
        self.assert_blocked("active_truncated_pairing")

    def test_pilot_does_not_require_unknown_generated_sequences_in_input(self):
        self.assertNotIn("candidate_set_sha256", self.bindings["diversified_pilot"])
        self.assertEqual(self.evaluate()["status"], "COMPLETE")

    def test_pilot_ids_without_sequence_identity_are_not_evidence(self):
        with self.assertRaisesRegex(ValueError, "sequence_sha256"):
            GATE.candidate_set_digest([{"candidate_id": "design_0"}])

    def test_pilot_output_digest_must_match_actual_artifact(self):
        self.receipts[2]["actual_candidate_set_sha256"] = "d" * 64
        self.assert_blocked("diversified_pilot")

    def test_candidate_digest_is_sequence_based_not_name_based(self):
        renamed = copy.deepcopy(self.candidates)
        renamed[0]["candidate_id"] = "arbitrary_new_label"
        self.assertEqual(GATE.candidate_set_digest(renamed), self.actual_candidate_digest)
        renamed[0]["vhh_sequence_sha256"] = "c" * 64
        self.assertNotEqual(GATE.candidate_set_digest(renamed), self.actual_candidate_digest)

    def test_changed_input_invalidates_receipt(self):
        self.bindings["existing_fold_reassessment"]["source_sha256"] = "c" * 64
        self.assert_blocked("existing_fold_reassessment")

    def test_changed_protocol_invalidates_receipt(self):
        self.contract["metrics"]["heavy_atom_contact_cutoff_angstrom"] = 6.0
        self.assert_blocked("existing_fold_reassessment")

    def test_old_unchanged_evidence_is_not_pointlessly_rerun(self):
        self.receipts[0]["created_at_utc"] = "2026-07-01T11:00:00Z"
        result = self.evaluate()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(result["stages"][0]["advisories"])

    def test_future_receipt_blocks(self):
        self.receipts[0]["created_at_utc"] = "2027-01-01T11:00:00Z"
        self.assert_blocked("existing_fold_reassessment")

    def test_posthoc_native_cannot_be_called_preregistered(self):
        self.receipts[1]["preregistered_at_utc"] = "2026-09-09T10:30:00Z"
        self.assert_blocked("native_free_refold")

    def test_timeout_and_failed_receipt_block(self):
        self.receipts[1]["status"] = "TIMEOUT"
        self.receipts[1]["actual"]["wall_seconds"] = 901
        self.assert_blocked("native_free_refold")

    def test_boolean_budget_is_not_a_valid_count(self):
        self.receipts[0]["actual"]["samples"] = True
        self.assert_blocked("existing_fold_reassessment")

    def test_incomplete_pairing_blocks(self):
        self.receipts[3]["actual"]["fold_samples"] = 47
        self.assert_blocked("active_truncated_pairing")

    def test_duplicate_receipts_rejected(self):
        with self.assertRaisesRegex(ValueError, "multiple receipts"):
            self.evaluate(self.receipts + self.receipts[:1])

    def test_tampered_actual_result_blocks(self):
        self.result.write_text('{"changed":true}', encoding="utf-8")
        self.assert_blocked("existing_fold_reassessment")

    def test_missing_actual_result_blocks(self):
        self.result.unlink()
        self.assert_blocked("existing_fold_reassessment")

    def test_biological_pass_cannot_be_true(self):
        self.receipts[0]["biological_pass"] = True
        self.assert_blocked("existing_fold_reassessment")


if __name__ == "__main__":
    unittest.main()
