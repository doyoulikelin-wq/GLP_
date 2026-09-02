"""CPU tests for the privacy-preserving T12 public bundle builder."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "build_t12_gpu_public_bundle.py"
)
SPEC = importlib.util.spec_from_file_location("build_t12_gpu_public_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_label(label: str) -> str:
    return digest_bytes(label.encode("utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class PublicBundleFixture:
    def __init__(self, parent: Path) -> None:
        self.repo = parent / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.repo), "config", "user.email",
                "fixture" + "@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Fixture"],
            check=True,
        )
        (self.repo / "source.txt").write_text("sealed source\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "fixture"],
            check=True,
        )
        self.commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.tree = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.output_parent = self.repo / "reports"
        self.output_parent.mkdir()
        self.output = self.output_parent / "t12_gpu_public_20260902"
        self.attempt = parent / "private_attempt"
        self.private_t11 = "/" + "home/private-user/local/t11_attempt"
        (self.attempt / "operator_logs").mkdir(parents=True)
        (self.attempt / "semantic").mkdir()
        self._build_attempt()

    @property
    def receipt_path(self) -> Path:
        return self.attempt / MODULE.RECEIPT_RELATIVE

    @property
    def validation_path(self) -> Path:
        return self.attempt / MODULE.VALIDATION_RELATIVE

    def _hash_records(self, names: set[str]) -> dict[str, str]:
        return {name: digest_label(name) for name in sorted(names)}

    def _build_attempt(self) -> None:
        adapter = {
            "hydra_target": "owner_split_template_data.SplitTemplateFromGeneratedDataModule",
            "target_templates": True,
            "design_mask_templates": False,
            "expected_target_tokens": 30,
            "expected_cdr_tokens": 30,
            "expected_framework_tokens": 91,
            "template_slots": 2,
            "cdr_visible_slots": 0,
        }
        resolved = {
            name: True for name in (
                "data_target", "target_templates", "design_mask_templates",
                "expected_target_tokens", "expected_cdr_tokens",
                "expected_framework_tokens", "skip_existing", "diffusion_samples",
                "sampling_steps", "recycling_steps", "batch_size", "devices",
                "precision", "kernels",
            )
        }
        execution = {
            "steps": ["folding"],
            "data_module_target": adapter["hydra_target"],
            "target_templates": True,
            "design_mask_templates": False,
            "expected_target_tokens": 30,
            "expected_cdr_tokens": 30,
            "expected_framework_tokens": 91,
            "expected_total_tokens": 151,
            "diffusion_samples": 5,
            "skip_existing": False,
        }
        semantic_records = []
        for candidate in range(6):
            for sample in range(5):
                relative = f"semantic/design_{candidate}_sample_{sample}.summary"
                data = f"candidate={candidate} sample={sample} finite=true\n".encode("utf-8")
                (self.attempt / relative).write_bytes(data)
                semantic_records.append({"path": relative, "sha256": digest_bytes(data)})
        source_manifest_payload = {
            "schema_version": "WINDOWS_OWNER_T12_SOURCE_INPUTS_V1",
            "source_t11_attempt": self.private_t11,
            "files": {},
        }
        source_manifest_data = (
            json.dumps(source_manifest_payload, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        before_relative = "operator_logs/SOURCE_INPUTS_BEFORE.json"
        after_relative = "operator_logs/SOURCE_INPUTS_AFTER.json"
        (self.attempt / before_relative).write_bytes(source_manifest_data)
        (self.attempt / after_relative).write_bytes(source_manifest_data)
        source_manifest_sha = digest_bytes(source_manifest_data)
        semantic_records.extend(
            {"path": relative, "sha256": source_manifest_sha, "role": "source_manifest", "size_bytes": len(source_manifest_data)}
            for relative in (before_relative, after_relative)
        )
        per_candidate = {
            candidate_id: {
                "fold_samples": 5,
                "token_count": 151,
                "sample_metric_keys": ["iptm"],
            }
            for candidate_id in MODULE.EXPECTED_CANDIDATE_IDS
        }
        validation = {
            "schema_version": MODULE.VALIDATION_SCHEMA,
            "status": "PASS",
            "candidate_ids": list(MODULE.EXPECTED_CANDIDATE_IDS),
            "candidate_count": 6,
            "fold_samples_per_candidate": 5,
            "observed_fold_sample_count": 30,
            "source_input_manifest": {
                "before_path": before_relative,
                "before_sha256": source_manifest_sha,
                "after_path": after_relative,
                "after_sha256": source_manifest_sha,
                "source_t11_attempt": self.private_t11,
                "replayed_file_count": 12,
            },
            "resolved_execution_contract": execution,
            "per_candidate": per_candidate,
            "semantic_payload_files": semantic_records,
            "input_hashes_unchanged": True,
            "source_t11_hashes_unchanged": True,
            "runtime_hashes_unchanged": True,
            "repository_identity_unchanged": True,
            "gpu_compute_processes_after": 0,
            "oom_detected": False,
            "scientific_claim_boundary": MODULE.CLAIM_BOUNDARY,
        }
        source_hashes = self._hash_records(set(MODULE.EXPECTED_SOURCE_INPUT_NAMES))
        runtime_hashes = self._hash_records(set(MODULE.EXPECTED_RUNTIME_NAMES))
        receipt = {
            "schema_version": MODULE.RUN_SCHEMA,
            "status": MODULE.RUN_COMPLETE,
            "exit_code": 0,
            "authority": "WINDOWS_CODEX",
            "scope": "EXPLORATORY_OVERRIDE_AFTER_CPU_GATE_FAIL",
            "cpu_gate_preserved": {"status": "FAIL", "pass_count": 7, "denominator": 30},
            "user_authorization": "EXPLICIT_T12_GPU_OVERRIDE_IN_CURRENT_TASK",
            "run_id": "t12_split_template_gpu",
            "attempt_id": "attempt_001",
            "started_at_utc": "2026-09-02T01:00:00Z",
            "ended_at_utc": "2026-09-02T01:03:00Z",
            "total_duration_seconds": 180.0,
            "source_commit": self.commit,
            "source_tree": self.tree,
            "source_t11_attempt": self.private_t11,
            "source_t11_receipt_sha256": digest_label("t11-receipt"),
            "candidate_ids": list(MODULE.EXPECTED_CANDIDATE_IDS),
            "candidate_count": 6,
            "fold_samples_per_candidate": 5,
            "fold_sample_count": 30,
            "requested_fold_sample_count": 30,
            "requested_stages": ["folding"],
            "stages_executed": ["folding"],
            "forbidden_stages_started": False,
            "no_auto_retry": True,
            "retry_count": 0,
            "bindcraft_started": False,
            "hard_timeout_seconds": 5400,
            "hard_timeout_respected": True,
            "oom_detected": False,
            "timed_out": False,
            "training_performed": False,
            "folding_exit_code": 0,
            "fatal_log_matches": [],
            "gpu_monitor_rows": 4,
            "source_input_hashes_before": source_hashes,
            "source_input_hashes_after": source_hashes,
            "copied_input_hashes_before": source_hashes,
            "copied_input_hashes_after": source_hashes,
            "runtime_assets_before": runtime_hashes,
            "runtime_assets_after": runtime_hashes,
            "source_t11_receipt_sha256": digest_label("t11-receipt"),
            "adapter_sha256": digest_label("adapter"),
            "validator_sha256": digest_label("validator"),
            "resolved_config_sha256": digest_label("config"),
            "local_env_acceptance_sha256": digest_label("env"),
            "adapter_preflight": {
                "status": "PASS",
                "sample_count": 6,
                "samples": [
                    {"id": candidate_id, "template_shape": [2, 151], "slot_sums": [30, 91], "cdr_visible": 0}
                    for candidate_id in MODULE.EXPECTED_CANDIDATE_IDS
                ],
            },
            "resolved_config_contract": resolved,
            "output_validation": validation,
            "internal_output_validation": {
                "schema_version": MODULE.VALIDATION_SCHEMA,
                "status": "PASS",
                "candidate_count": 6,
                "fold_samples_per_candidate": 5,
                "fold_sample_count": 30,
                "finite_arrays": True,
                "token_contract": {"target": 30, "cdr": 30, "framework": 91},
            },
            "candidate_sequences": ["ACDEFGHIKLMNPQRSTVWYACDEFGHIK"],
            "scientific_claim_boundary": MODULE.CLAIM_BOUNDARY,
        }
        write_json(self.validation_path, validation)
        write_json(self.receipt_path, receipt)
        (self.attempt / MODULE.DIRECTORIES_RELATIVE).write_text(
            "./operator_logs\n./semantic\n",
            encoding="utf-8",
        )
        self.reseal()

    def reseal(self) -> None:
        manifest = self.attempt / MODULE.MANIFEST_RELATIVE
        manifest.unlink(missing_ok=True)
        rows = []
        for path in self.attempt.rglob("*"):
            if path.is_file() and path != manifest:
                relative = path.relative_to(self.attempt).as_posix()
                rows.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        rows.sort(key=lambda item: item[0].encode("utf-8"))
        manifest.write_text(
            "".join(f"{digest}  ./{relative}\n" for relative, digest in rows),
            encoding="utf-8",
        )

    def mutate_receipt(self, key: str, value: object) -> None:
        payload = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        payload[key] = value
        write_json(self.receipt_path, payload)
        self.reseal()


class BuildT12GpuPublicBundleTest(unittest.TestCase):
    def test_success_writes_exact_sanitized_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicBundleFixture(Path(temporary))
            result = MODULE.build_bundle(
                fixture.attempt,
                fixture.repo,
                fixture.output,
                fixture.commit,
            )
            self.assertEqual(result, fixture.output)
            expected = {
                "README.md",
                "T12_PUBLIC_RECEIPT.json",
                "T12_VALIDATION_SUMMARY.json",
                "T12_PUBLIC_CONFIG.yaml",
                "ARTIFACT_INDEX.csv",
                "SHA256SUMS",
            }
            self.assertEqual({path.name for path in result.iterdir()}, expected)

            public_receipt = json.loads(
                (result / "T12_PUBLIC_RECEIPT.json").read_text(encoding="utf-8")
            )
            self.assertEqual(public_receipt["historical_cpu_gate"]["status"], "FAIL")
            self.assertEqual(public_receipt["historical_cpu_gate"]["observed_pass_count"], 7)
            self.assertFalse(public_receipt["historical_cpu_gate"]["reclassified_as_pass"])
            self.assertTrue(public_receipt["exploratory_override"]["explicitly_authorized"])
            self.assertEqual(public_receipt["process"]["fold_sample_count"], 30)
            self.assertFalse(public_receipt["process"]["oom_detected"])

            combined = b"\n".join(path.read_bytes() for path in result.iterdir())
            self.assertNotIn(b"/" + b"home/private-user", combined)
            self.assertNotIn(b"ACDEFGHIKLMNPQRSTVWYACDEFGHIK", combined)
            self.assertNotIn(b"boltz2_conf_final.ckpt", combined)
            self.assertNotIn(b"mols.zip", combined)

            for line in (result / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
                expected_sha, relative = line.split("  ./", 1)
                self.assertEqual(
                    hashlib.sha256((result / relative).read_bytes()).hexdigest(),
                    expected_sha,
                )

    def test_refuses_failed_incomplete_or_oom_attempts(self) -> None:
        cases = (
            ("status", "T12_SPLIT_TEMPLATE_FAILED", "complete T12 GPU run"),
            ("fold_sample_count", 29, "fold_sample_count"),
            ("oom_detected", True, "oom_detected"),
            ("retry_count", 1, "retry_count"),
            ("bindcraft_started", True, "bindcraft_started"),
        )
        for key, value, message in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                fixture = PublicBundleFixture(Path(temporary))
                fixture.mutate_receipt(key, value)
                with self.assertRaisesRegex(MODULE.PublicationError, message):
                    MODULE.build_bundle(
                        fixture.attempt,
                        fixture.repo,
                        fixture.output,
                        fixture.commit,
                    )
                self.assertFalse(fixture.output.exists())

    def test_refuses_input_or_runtime_hash_drift(self) -> None:
        for key in ("source_input_hashes_after", "runtime_assets_after"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                fixture = PublicBundleFixture(Path(temporary))
                receipt = json.loads(fixture.receipt_path.read_text(encoding="utf-8"))
                first = sorted(receipt[key])[0]
                receipt[key][first] = digest_label(f"changed-{key}")
                write_json(fixture.receipt_path, receipt)
                fixture.reseal()
                with self.assertRaisesRegex(MODULE.PublicationError, "changed during the attempt"):
                    MODULE.build_bundle(
                        fixture.attempt,
                        fixture.repo,
                        fixture.output,
                        fixture.commit,
                    )

    def test_refuses_manifest_tamper_and_source_commit_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicBundleFixture(Path(temporary))
            semantic = fixture.attempt / "semantic/design_0_sample_0.summary"
            semantic.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.PublicationError, "digest mismatch"):
                MODULE.build_bundle(
                    fixture.attempt,
                    fixture.repo,
                    fixture.output,
                    fixture.commit,
                )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicBundleFixture(Path(temporary))
            with self.assertRaisesRegex(MODULE.PublicationError, "operator-provided expectation"):
                MODULE.build_bundle(
                    fixture.attempt,
                    fixture.repo,
                    fixture.output,
                    "0" * 40,
                )

    def test_refuses_existing_output_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicBundleFixture(Path(temporary))
            fixture.output.mkdir()
            marker = fixture.output / "owner-data.txt"
            marker.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.PublicationError, "overwrite"):
                MODULE.build_bundle(
                    fixture.attempt,
                    fixture.repo,
                    fixture.output,
                    fixture.commit,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
