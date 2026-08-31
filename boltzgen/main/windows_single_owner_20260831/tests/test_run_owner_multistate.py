from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_owner_multistate.py"
SPEC = importlib.util.spec_from_file_location("run_owner_multistate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RunOwnerMultistateTest(unittest.TestCase):
    def test_panel_is_frozen_to_ten_tasks_and_fifty_rows(self) -> None:
        self.assertEqual(MODULE.DEFAULT_CANDIDATES, ("design_1", "design_3"))
        self.assertEqual(
            MODULE.DEFAULT_STATES,
            ("DEV_00", "DEV_01", "DEV_05", "DEV_06", "DEV_15"),
        )
        self.assertEqual(
            len(MODULE.DEFAULT_CANDIDATES)
            * len(MODULE.DEFAULT_STATES)
            * MODULE.SAMPLES_PER_TASK,
            50,
        )

    def test_manifest_verification_rejects_escape_and_detects_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            member = root / "input.txt"
            member.write_text("frozen\n", encoding="utf-8")
            manifest = root / "SHA256SUMS"
            manifest.write_text(
                f"{MODULE.sha256_file(member)}  input.txt\n", encoding="utf-8"
            )
            self.assertEqual(
                MODULE.verify_manifest(root, manifest)["input.txt"],
                MODULE.sha256_file(member),
            )
            member.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.RunFailure, "mismatch"):
                MODULE.verify_manifest(root, manifest)
            manifest.write_text(f"{'0' * 64}  ../outside\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.RunFailure, "unsafe"):
                MODULE.parse_sha256_manifest(manifest)

    def test_input_status_can_only_transition_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "STATUS.txt"
            MODULE.atomic_write(status, "INPUTS_READY\n")
            MODULE.atomic_write(
                status, "AI_EVALUATION_COMPLETE\n", replace_input_status=True
            )
            self.assertEqual(status.read_text(encoding="utf-8"), "AI_EVALUATION_COMPLETE\n")
            with self.assertRaisesRegex(MODULE.RunFailure, "overwrite"):
                MODULE.atomic_write(
                    status, "AI_EVALUATION_FAILED\n", replace_input_status=True
                )

    def test_append_or_seal_failure_corrects_provisional_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "attempt_20260831T000000Z"
            logs = run_root / "operator_logs"
            logs.mkdir(parents=True)
            (run_root / "STATUS.txt").write_text(
                "AI_EVALUATION_COMPLETE\n", encoding="utf-8"
            )
            (logs / "AI_EVALUATION.json").write_text(
                json.dumps({"status": "AI_EVALUATION_COMPLETE"}), encoding="utf-8"
            )
            (logs / "experience_event.json").write_text(
                json.dumps({"outcome": "SUCCESS"}), encoding="utf-8"
            )
            (logs / "OUTPUT_SHA256SUMS").write_text(
                "provisional manifest\n", encoding="utf-8"
            )

            event_path, manifest_sha = MODULE.publish_failure_terminal(
                run_root=run_root,
                panel_id="fixture",
                started_at="2026-08-31T00:00:00Z",
                failure_reason="injected experience append failure",
            )

            self.assertEqual(
                (run_root / "STATUS.txt").read_text(encoding="utf-8").strip(),
                "AI_EVALUATION_FAILED",
            )
            self.assertEqual(
                json.loads((logs / "AI_EVALUATION.json").read_text(encoding="utf-8"))[
                    "status"
                ],
                "AI_EVALUATION_FAILED",
            )
            self.assertEqual(json.loads(event_path.read_text(encoding="utf-8"))["outcome"], "FAILURE")
            self.assertEqual(
                manifest_sha, MODULE.sha256_file(logs / "OUTPUT_SHA256SUMS")
            )
            MODULE.verify_manifest(run_root, logs / "OUTPUT_SHA256SUMS")


if __name__ == "__main__":
    unittest.main()
