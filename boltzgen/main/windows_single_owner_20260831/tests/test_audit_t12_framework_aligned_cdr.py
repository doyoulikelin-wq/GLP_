from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit_t12_framework_aligned_cdr.py"
)
SPEC = importlib.util.spec_from_file_location("audit_t12_framework_aligned_cdr", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AuditT12FrameworkAlignedCdrTest(unittest.TestCase):
    def make_arm(
        self,
        root: Path,
        *,
        cdr_offset: float = 0.0,
        sample_count: int = 5,
        bad_metric: str | None = None,
    ) -> None:
        fold_root = root / "fold_out_npz"
        fold_root.mkdir(parents=True)
        design_mask = np.zeros(MODULE.TOTAL_TOKENS, dtype=np.float32)
        design_mask[list(MODULE.CDR_TOKEN_INDICES)] = 1.0
        np.savez_compressed(root / "design_0.npz", design_mask=design_mask)

        atom_count = MODULE.TOTAL_TOKENS * 4
        token_for_atom = np.repeat(np.arange(MODULE.TOTAL_TOKENS), 4)
        atom_to_token = np.zeros(
            (1, atom_count, MODULE.TOTAL_TOKENS), dtype=bool
        )
        atom_to_token[0, np.arange(atom_count), token_for_atom] = True
        reference = np.zeros((atom_count, 3), dtype=np.float32)
        offsets = np.asarray(
            [[0.0, 0.0, 0.0], [1.1, 0.2, 0.0], [2.0, 0.8, 0.3], [2.8, 1.2, 0.9]],
            dtype=np.float32,
        )
        for token in range(MODULE.TOTAL_TOKENS):
            base = np.asarray(
                [3.7 * (token % 17), 4.1 * ((token // 17) % 5), 2.3 * (token // 85)],
                dtype=np.float32,
            )
            reference[token * 4 : token * 4 + 4] = base + offsets
        predictions = np.repeat(reference[None, :, :], sample_count, axis=0)
        predictions += np.asarray([7.0, -3.0, 2.0], dtype=np.float32)
        cdr_atoms = design_mask[token_for_atom].astype(bool)
        predictions[:, cdr_atoms, 2] += cdr_offset

        metrics = {}
        for name in MODULE.REQUIRED_SAMPLE_METRICS:
            size = sample_count - 1 if name == bad_metric else sample_count
            metrics[name] = np.linspace(0.1, 0.5, size, dtype=np.float32)
        np.savez_compressed(
            fold_root / "design_0.npz",
            coords=predictions,
            input_coords=reference[None, None, :, :],
            atom_to_token=atom_to_token,
            atom_resolved_mask=np.ones((1, atom_count), dtype=bool),
            backbone_mask=np.ones((1, atom_count), dtype=np.float32),
            token_index=np.arange(MODULE.TOTAL_TOKENS, dtype=np.int64)[None, :],
            **metrics,
        )

    def make_four_arms(
        self,
        parent: Path,
        *,
        fixed_offset: float = 0.0,
        bad_sample_arm: str | None = None,
        bad_metric_arm: str | None = None,
    ) -> dict[str, Path]:
        roots = {}
        for arm in MODULE.ARM_ORDER:
            root = parent / arm
            self.make_arm(
                root,
                cdr_offset=fixed_offset if arm == "fixed_ifold" else 0.0,
                sample_count=4 if arm == bad_sample_arm else 5,
                bad_metric="iptm" if arm == bad_metric_arm else None,
            )
            roots[arm] = root
        return roots

    @staticmethod
    def cli_args(roots: dict[str, Path], output_json: Path, output_tsv: Path) -> list[str]:
        return [
            "--internal-root",
            str(roots["internal"]),
            "--high-contact-root",
            str(roots["high_contact"]),
            "--diverse-root",
            str(roots["diverse"]),
            "--fixed-ifold-root",
            str(roots["fixed_ifold"]),
            "--output-json",
            str(output_json),
            "--output-tsv",
            str(output_tsv),
            "--internal-design-count",
            "1",
            "--high-contact-design-count",
            "1",
            "--diverse-design-count",
            "1",
            "--fixed-ifold-design-count",
            "1",
            "--fixed-ifold-min-pass",
            "3",
        ]

    def test_success_writes_all_sample_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = self.make_four_arms(root)
            output_json = root / "audit.json"
            output_tsv = root / "samples.tsv"
            self.assertEqual(MODULE.main(self.cli_args(roots, output_json, output_tsv)), 0)
            payload = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["total_sample_count"], 20)
            self.assertEqual(payload["gate"]["observed_pass_count"], 5)
            self.assertEqual(
                payload["candidate_summaries"]["fixed_ifold"]["design_0"][
                    "framework_le_threshold_count"
                ],
                5,
            )
            lines = output_tsv.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 21)
            identities = {(line.split("\t")[0], line.split("\t")[1], line.split("\t")[2]) for line in lines[1:]}
            self.assertEqual(len(identities), 20)

    def test_rejects_wrong_sample_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = self.make_four_arms(root, bad_sample_arm="diverse")
            with self.assertRaisesRegex(MODULE.ValidationError, "coords shape"):
                MODULE.audit(
                    roots,
                    {arm: 1 for arm in MODULE.ARM_ORDER},
                    fixed_ifold_min_pass=3,
                )

    def test_rejects_wrong_metric_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = self.make_four_arms(root, bad_metric_arm="high_contact")
            with self.assertRaisesRegex(MODULE.ValidationError, "metric iptm shape"):
                MODULE.audit(
                    roots,
                    {arm: 1 for arm in MODULE.ARM_ORDER},
                    fixed_ifold_min_pass=3,
                )

    def test_gate_failure_writes_complete_results_before_exit_42(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = self.make_four_arms(root, fixed_offset=5.0)
            output_json = root / "audit.json"
            output_tsv = root / "samples.tsv"
            self.assertEqual(MODULE.main(self.cli_args(roots, output_json, output_tsv)), 42)
            self.assertTrue(output_json.is_file())
            self.assertTrue(output_tsv.is_file())
            payload = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["exit_code"], 42)
            self.assertEqual(payload["total_sample_count"], 20)
            self.assertEqual(payload["gate"]["observed_pass_count"], 0)
            self.assertEqual(
                payload["candidate_summaries"]["fixed_ifold"]["design_0"][
                    "framework_le_threshold_count"
                ],
                0,
            )
            self.assertEqual(payload["gate"]["failure_action"], "DO_NOT_START_T12_GPU")
            self.assertEqual(len(output_tsv.read_text(encoding="utf-8").splitlines()), 21)

    def test_rejects_existing_output_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = self.make_four_arms(root)
            output_json = root / "audit.json"
            output_tsv = root / "samples.tsv"
            output_json.write_text("owner-data\n", encoding="utf-8")
            self.assertEqual(MODULE.main(self.cli_args(roots, output_json, output_tsv)), 2)
            self.assertEqual(output_json.read_text(encoding="utf-8"), "owner-data\n")
            self.assertFalse(output_tsv.exists())


if __name__ == "__main__":
    unittest.main()
