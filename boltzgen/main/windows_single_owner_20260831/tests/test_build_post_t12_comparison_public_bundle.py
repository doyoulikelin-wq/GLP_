from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_post_t12_comparison_public_bundle.py"
BUILDER_SPEC = importlib.util.spec_from_file_location(
    "build_post_t12_comparison_public_bundle", BUILDER_PATH
)
assert BUILDER_SPEC is not None and BUILDER_SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = BUILDER
BUILDER_SPEC.loader.exec_module(BUILDER)

HELPER_PATH = Path(__file__).with_name("test_compare_t11_t12_folds.py")
HELPER_SPEC = importlib.util.spec_from_file_location(
    "post_t12_comparator_fixture", HELPER_PATH
)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
HELPER = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER)
COMPARE = HELPER.MODULE


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class PublicFixture:
    def __init__(self, parent: Path) -> None:
        self.repo = parent / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "source.txt").write_text("sealed source\n", encoding="utf-8")
        git(self.repo, "add", "source.txt")
        git(self.repo, "commit", "-q", "-m", "fixture")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        (self.repo / "reports").mkdir()

        helper = HELPER.CompareT11T12FoldsTest("runTest")
        t11, t12, historical = helper.make_inputs(parent / "inputs")
        payload, samples, candidates, paired = COMPARE.compare(t11, t12, historical)
        payload["run_evidence"] = {"status": "PASS"}
        payload["code_provenance"] = {
            "source_commit": self.commit,
            "source_tree": self.tree,
            "comparator_sha256": "e" * 64,
            "audit_dependency_sha256": payload["audit_dependency_sha256"],
            "repository_clean": True,
        }
        private_parent = parent / "private_analysis"
        private_parent.mkdir()
        self.attempt = private_parent / "attempt_20260903T010203Z"
        COMPARE.write_attempt(
            self.attempt,
            payload,
            samples,
            candidates,
            paired,
            source_commit=self.commit,
            source_tree=self.tree,
            t11_receipt_sha256="c" * 64,
            t12_receipt_sha256="d" * 64,
            started_at_utc="2026-09-03T01:02:03Z",
            analysis_duration_seconds=0.25,
            command=["python", "comparator.py"],
        )
        self.output = (
            self.repo
            / "reports"
            / "post_t12_readonly_comparison_public_20260903"
        )


class BuildPostT12ComparisonPublicBundleTest(unittest.TestCase):
    def test_success_is_aggregate_only_and_manifest_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicFixture(Path(temporary))
            self.assertEqual(
                BUILDER.build_bundle(fixture.attempt, fixture.repo, fixture.output),
                fixture.output,
            )
            self.assertEqual(
                {path.name for path in fixture.output.iterdir()},
                set(BUILDER.PUBLIC_FILES) | {"SHA256SUMS"},
            )
            combined = b"\n".join(path.read_bytes() for path in fixture.output.iterdir())
            self.assertNotIn(str(fixture.attempt).encode("utf-8"), combined)
            self.assertNotIn(b"design_0", combined)
            self.assertNotIn(b"sample_index", combined)
            summary = json.loads(
                (fixture.output / "POST_T12_COMPARISON_SUMMARY.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["comparison_outcome"], "INCONCLUSIVE")
            self.assertFalse(summary["scope"]["cross_method_fold_pairing"])
            index_rows = list(
                csv.DictReader(
                    io.StringIO(
                        (fixture.output / "ARTIFACT_INDEX.csv").read_text(
                            encoding="utf-8"
                        )
                    )
                )
            )
            sealed = [
                row
                for row in index_rows
                if row["publication_scope"] == "SEALED_SOURCE_DIGEST_ONLY"
            ]
            self.assertTrue(sealed)
            self.assertTrue(all(row["path"] == "" for row in sealed))
            for line in (fixture.output / "SHA256SUMS").read_text(
                encoding="utf-8"
            ).splitlines():
                expected, relative = line.split("  ./", maxsplit=1)
                observed = hashlib.sha256(
                    (fixture.output / relative).read_bytes()
                ).hexdigest()
                self.assertEqual(observed, expected)

    def test_rejects_private_manifest_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicFixture(Path(temporary))
            report = fixture.attempt / "reports" / "POST_T12_COMPARISON.json"
            report.write_bytes(report.read_bytes() + b" ")
            with self.assertRaisesRegex(BUILDER.PublicationError, "digest mismatch"):
                BUILDER.build_bundle(fixture.attempt, fixture.repo, fixture.output)
            self.assertFalse(fixture.output.exists())

    def test_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicFixture(Path(temporary))
            BUILDER.build_bundle(fixture.attempt, fixture.repo, fixture.output)
            before = hashlib.sha256(
                (fixture.output / "SHA256SUMS").read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(BUILDER.PublicationError, "refusing to overwrite"):
                BUILDER.build_bundle(fixture.attempt, fixture.repo, fixture.output)
            after = hashlib.sha256(
                (fixture.output / "SHA256SUMS").read_bytes()
            ).hexdigest()
            self.assertEqual(after, before)

    def test_privacy_scanner_rejects_paths_sequences_and_candidate_ids(self) -> None:
        for data in (
            b"/home/person/project",
            b"C:\\Users\\person\\project",
            b"design_3",
            b"ACDEFGHIKLMNPQRSTVWY",
        ):
            with self.subTest(data=data):
                with self.assertRaises(BUILDER.PublicationError):
                    BUILDER._privacy_check("fixture.txt", data)

    def test_isolated_cli_help(self) -> None:
        process = subprocess.run(
            [sys.executable, "-B", "-I", str(BUILDER_PATH), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--comparison-attempt", process.stdout)


if __name__ == "__main__":
    unittest.main()
