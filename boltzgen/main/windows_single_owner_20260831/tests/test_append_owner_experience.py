from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "append_owner_experience.py"


class AppendOwnerExperienceTest(unittest.TestCase):
    def test_appends_local_final_event_and_rejects_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            registry_path = root / "events.jsonl"
            event_path.write_text(
                json.dumps(
                    {
                        "event_id": "iteration-001-env-ready",
                        "iteration_id": "iteration-001",
                        "stage": "LOCAL_ENV_ACCEPTANCE",
                        "outcome": "SUCCESS",
                        "summary": "GPU smoke passed",
                    }
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SCRIPT),
                "--registry",
                str(registry_path),
                "--event",
                str(event_path),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            row = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(row["authority"], "WINDOWS_CODEX")
            self.assertEqual(row["review_state"], "LOCAL_FINAL")
            self.assertFalse(row["mac_review_required"])
            self.assertRegex(row["event_sha256"], r"^[0-9a-f]{64}$")

            duplicate = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("duplicate event_id", duplicate.stderr)


if __name__ == "__main__":
    unittest.main()
