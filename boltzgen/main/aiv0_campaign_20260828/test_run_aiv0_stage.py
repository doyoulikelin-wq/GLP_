"""Tests for the external, immutable AIV0 stage runner.

Code source: project_original.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from run_aiv0_stage import (
    AttemptAlreadyExistsError,
    StageConfigurationError,
    run_stage,
)


def digest(path: Path) -> str:
    """Return the SHA-256 of a small test artifact."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


class Aiv0StageRunnerTests(unittest.TestCase):
    """Exercise success, failure, collision, and external-root contracts."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo_root = self.root / "repo"
        self.run_root = self.root / "external_run"
        self.project_root = Path(
            os.path.commonpath((self.root.resolve(), Path(sys.executable).resolve()))
        )
        self.repo_root.mkdir()
        self.run_root.mkdir()
        self.contract_root = self.repo_root / "contracts"
        self.contract_root.mkdir()
        for name in (
            "asset_mounts.tsv",
            "cohort_registry.tsv",
            "compatibility_aliases.tsv",
            "file_overrides.tsv",
            "historical_output_hashes.tsv",
        ):
            (self.contract_root / name).write_text(f"fixture:{name}\n", encoding="utf-8")
        self.output_root = self.root / "derived_registry"
        self.output_root.mkdir()
        self.input_file = self.root / "source_manifest.tsv"
        self.input_file.write_text("asset_id\tsha256\nasset-1\tabcd\n", encoding="utf-8")

    def make_validator(self, exit_code: int) -> Path:
        """Create a fake validator that accepts arbitrary CLI arguments."""

        validator = self.root / f"fake_validator_{exit_code}.py"
        validator.write_text(
            "import sys\n"
            "print('fake validator stdout')\n"
            "print('fake validator stderr', file=sys.stderr)\n"
            f"raise SystemExit({exit_code})\n",
            encoding="utf-8",
        )
        return validator

    def make_writing_validator(self) -> Path:
        """Create a fake validator that writes one declared derived artifact."""

        validator = self.root / "fake_writing_validator.py"
        validator.write_text(
            "import argparse\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--output-root', required=True)\n"
            "args, _ = parser.parse_known_args()\n"
            "Path(args.output_root, 'derived.tsv').write_text('row\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        return validator

    def make_mutating_validator(self, path: Path, replacement: str) -> Path:
        """Create a fake validator that mutates one evidence path."""

        validator = self.root / f"mutate_{path.name}.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"Path({str(path)!r}).write_text({replacement!r}, encoding='utf-8')\n",
            encoding="utf-8",
        )
        return validator

    def run_fake(self, *, attempt_id: str, exit_code: int):
        """Run one fake validator attempt with an explicit input file."""

        return run_stage(
            repo_root=self.repo_root,
            run_root=self.run_root,
            attempt_id=attempt_id,
            validator_python=Path(sys.executable),
            validator=self.make_validator(exit_code),
            project_root=self.project_root,
            contract_root=self.contract_root,
            output_root=self.output_root,
            input_paths=[self.input_file],
            environment={"SECRET_TOKEN": "must-not-be-recorded", "LANG": "C"},
        )

    def test_success_closes_manifests_and_publishes_receipt_last(self) -> None:
        result = self.run_fake(attempt_id="attempt_001", exit_code=0)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.exit_code, 0)
        required = {
            "command.json",
            "derived_outputs.SHA256SUMS",
            "derived_outputs_before.SHA256SUMS",
            "environment_allowlist.json",
            "started_at_utc.txt",
            "ended_at_utc.txt",
            "stdout.log",
            "stderr.log",
            "exit_code.txt",
            "status.json",
            "inputs.SHA256SUMS",
            "inputs_after.SHA256SUMS",
            "outputs.SHA256SUMS",
            "receipt.json",
            "runtime_fingerprint.json",
            "runtime_fingerprint_after.json",
        }
        self.assertEqual({path.name for path in result.attempt_dir.iterdir()}, required)
        self.assertIn("fake validator stdout", (result.attempt_dir / "stdout.log").read_text())
        self.assertIn("fake validator stderr", (result.attempt_dir / "stderr.log").read_text())

        recorded_environment = json.loads(
            (result.attempt_dir / "environment_allowlist.json").read_text()
        )
        self.assertNotIn("SECRET_TOKEN", recorded_environment["variables"])
        self.assertEqual(recorded_environment["variables"]["LANG"], "C")
        self.assertEqual(recorded_environment["variables"]["PYTHONNOUSERSITE"], "1")

        receipt_path = result.attempt_dir / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["schema_version"], "AIV0_STAGE_RECEIPT_V1")
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(
            receipt["outputs_manifest_sha256"],
            digest(result.attempt_dir / "outputs.SHA256SUMS"),
        )
        output_manifest = (result.attempt_dir / "outputs.SHA256SUMS").read_text()
        self.assertNotIn("receipt.json", output_manifest)
        self.assertFalse(
            any(
                line.endswith("  outputs.SHA256SUMS")
                for line in output_manifest.splitlines()
            )
        )
        self.assertGreaterEqual(
            receipt_path.stat().st_mtime_ns,
            (result.attempt_dir / "outputs.SHA256SUMS").stat().st_mtime_ns,
        )
        self.assertFalse(any(path.name.endswith(".tmp") for path in result.attempt_dir.iterdir()))
        command = json.loads((result.attempt_dir / "command.json").read_text())
        self.assertIn("-B", command["argv"])
        input_manifest = (result.attempt_dir / "inputs.SHA256SUMS").read_text()
        self.assertIn("repo://contracts/asset_mounts.tsv", input_manifest)
        self.assertNotIn("  /", input_manifest)
        runtime = json.loads(
            (result.attempt_dir / "runtime_fingerprint.json").read_text()
        )
        self.assertEqual(runtime["schema_version"], "AIV0_RUNTIME_FINGERPRINT_V1")
        self.assertEqual(
            receipt["runtime_fingerprint_sha256"],
            digest(result.attempt_dir / "runtime_fingerprint.json"),
        )
        self.assertEqual(
            (result.attempt_dir / "runtime_fingerprint.json").read_text(),
            (result.attempt_dir / "runtime_fingerprint_after.json").read_text(),
        )
        self.assertEqual(
            (result.attempt_dir / "inputs.SHA256SUMS").read_text(),
            (result.attempt_dir / "inputs_after.SHA256SUMS").read_text(),
        )
        self.assertEqual(
            (result.attempt_dir / "derived_outputs_before.SHA256SUMS").read_bytes(),
            b"",
        )

    def test_nonzero_validator_is_a_closed_fail_attempt(self) -> None:
        result = self.run_fake(attempt_id="attempt_002", exit_code=7)

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.exit_code, 7)
        status = json.loads((result.attempt_dir / "status.json").read_text())
        receipt = json.loads((result.attempt_dir / "receipt.json").read_text())
        self.assertEqual(status["failure_kind"], "VALIDATOR_NONZERO")
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual((result.attempt_dir / "exit_code.txt").read_text(), "7\n")

    def test_existing_attempt_is_never_overwritten(self) -> None:
        result = self.run_fake(attempt_id="attempt_003", exit_code=0)
        before = {
            path.name: digest(path)
            for path in result.attempt_dir.iterdir()
            if path.is_file()
        }

        with self.assertRaises(AttemptAlreadyExistsError):
            self.run_fake(attempt_id="attempt_003", exit_code=0)

        after = {
            path.name: digest(path)
            for path in result.attempt_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_run_root_inside_repo_is_rejected_before_writing(self) -> None:
        internal_run_root = self.repo_root / "runs" / "campaign"
        internal_run_root.mkdir(parents=True)

        with self.assertRaises(StageConfigurationError):
            run_stage(
                repo_root=self.repo_root,
                run_root=internal_run_root,
                attempt_id="attempt_004",
                validator_python=Path(sys.executable),
                validator=self.make_validator(0),
                project_root=self.project_root,
                contract_root=self.contract_root,
                output_root=self.output_root,
            )

        self.assertFalse((internal_run_root / "logs").exists())

    def test_symlinked_log_root_cannot_escape_external_run_root(self) -> None:
        escaped_log_root = self.repo_root / "escaped_logs"
        escaped_log_root.mkdir()
        (self.run_root / "logs").symlink_to(escaped_log_root, target_is_directory=True)

        with self.assertRaises(StageConfigurationError):
            run_stage(
                repo_root=self.repo_root,
                run_root=self.run_root,
                attempt_id="attempt_005",
                validator_python=Path(sys.executable),
                validator=self.make_validator(0),
                project_root=self.project_root,
                contract_root=self.contract_root,
                output_root=self.output_root,
            )

        self.assertEqual(list(escaped_log_root.iterdir()), [])

    def test_every_mode_requires_external_output_root(self) -> None:
        with self.assertRaises(StageConfigurationError):
            run_stage(
                repo_root=self.repo_root,
                run_root=self.run_root,
                attempt_id="attempt_006",
                validator_python=Path(sys.executable),
                validator=self.make_validator(0),
                project_root=self.project_root,
                mode="write",
                contract_root=self.contract_root,
            )

        self.assertFalse((self.run_root / "logs").exists())

    def test_write_mode_hashes_external_derived_outputs(self) -> None:
        output_root = self.output_root

        result = run_stage(
            repo_root=self.repo_root,
            run_root=self.run_root,
            attempt_id="attempt_007",
            validator_python=Path(sys.executable),
            validator=self.make_writing_validator(),
            project_root=self.project_root,
            mode="write",
            contract_root=self.contract_root,
            output_root=output_root,
        )

        self.assertEqual(result.status, "PASS")
        derived_manifest = (
            result.attempt_dir / "derived_outputs.SHA256SUMS"
        ).read_text()
        self.assertIn("derived.tsv", derived_manifest)
        receipt = json.loads((result.attempt_dir / "receipt.json").read_text())
        self.assertEqual(receipt["validator_mode"], "write")
        self.assertEqual(
            receipt["derived_outputs_manifest_sha256"],
            digest(result.attempt_dir / "derived_outputs.SHA256SUMS"),
        )

    def test_input_change_during_execution_closes_fail_receipt(self) -> None:
        result = run_stage(
            repo_root=self.repo_root,
            run_root=self.run_root,
            attempt_id="attempt_008",
            validator_python=Path(sys.executable),
            validator=self.make_mutating_validator(self.input_file, "changed\n"),
            project_root=self.project_root,
            contract_root=self.contract_root,
            output_root=self.output_root,
            input_paths=[self.input_file],
        )

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.exit_code, 70)
        status = json.loads((result.attempt_dir / "status.json").read_text())
        self.assertEqual(status["failure_kind"], "RUNNER_EVIDENCE_ERROR")
        self.assertFalse(status["inputs_reverified_after_execution"])
        self.assertTrue((result.attempt_dir / "receipt.json").is_file())

    def test_check_mode_output_change_closes_fail_receipt(self) -> None:
        derived = self.output_root / "derived.tsv"
        derived.write_text("before\n", encoding="utf-8")
        result = run_stage(
            repo_root=self.repo_root,
            run_root=self.run_root,
            attempt_id="attempt_009",
            validator_python=Path(sys.executable),
            validator=self.make_mutating_validator(derived, "after\n"),
            project_root=self.project_root,
            contract_root=self.contract_root,
            output_root=self.output_root,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.exit_code, 70)
        status = json.loads((result.attempt_dir / "status.json").read_text())
        self.assertFalse(status["derived_outputs_unchanged"])
        self.assertEqual(status["failure_kind"], "RUNNER_EVIDENCE_ERROR")
        self.assertTrue((result.attempt_dir / "receipt.json").is_file())


if __name__ == "__main__":
    unittest.main()
