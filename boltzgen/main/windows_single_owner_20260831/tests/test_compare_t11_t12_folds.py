from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "compare_t11_t12_folds.py"
SPEC = importlib.util.spec_from_file_location("compare_t11_t12_folds", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CompareT11T12FoldsTest(unittest.TestCase):
    def make_method(
        self, root: Path, *, cdr_offset: float, historical_gate_pattern: bool = False
    ) -> None:
        fold_root = root / "fold_out_npz"
        fold_root.mkdir(parents=True)

        design_mask = np.zeros(MODULE.TOTAL_TOKENS, dtype=np.float32)
        design_mask[list(MODULE.CDR_TOKEN_INDICES)] = 1.0

        atom_count = MODULE.TOTAL_TOKENS * 4
        token_for_atom = np.repeat(np.arange(MODULE.TOTAL_TOKENS), 4)
        atom_to_token = np.zeros((1, atom_count, MODULE.TOTAL_TOKENS), dtype=bool)
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
        metrics = {
            name: np.linspace(0.1, 0.5, MODULE.SAMPLES_PER_DESIGN, dtype=np.float32)
            for name in MODULE.REQUIRED_SAMPLE_METRICS
        }
        metrics.update(
            {
                "min_design_to_target_pae": np.linspace(
                    8.0, 10.0, MODULE.SAMPLES_PER_DESIGN, dtype=np.float32
                ),
                "min_interaction_pae": np.linspace(
                    7.0, 9.0, MODULE.SAMPLES_PER_DESIGN, dtype=np.float32
                ),
            }
        )
        for index in range(MODULE.EXPECTED_CANDIDATE_COUNT):
            (root / f"design_{index}.cif").write_bytes(
                f"data_design_{index}\n#\n".encode("utf-8")
            )
            np.savez_compressed(root / f"design_{index}.npz", design_mask=design_mask)
            varied_metrics = {
                name: values + np.float32(index * 0.01)
                for name, values in metrics.items()
            }
            predictions = np.repeat(
                reference[None, :, :], MODULE.SAMPLES_PER_DESIGN, axis=0
            )
            predictions += np.asarray([7.0, -3.0, 2.0], dtype=np.float32)
            vhh_atoms = token_for_atom >= 30
            predictions[:, vhh_atoms, 0] += 30.0
            for sample_index in range(MODULE.SAMPLES_PER_DESIGN):
                flat_index = index * MODULE.SAMPLES_PER_DESIGN + sample_index
                offset = 0.0 if historical_gate_pattern and flat_index < 7 else cdr_offset
                predictions[
                    sample_index, design_mask[token_for_atom].astype(bool), 2
                ] += offset
            np.savez_compressed(
                fold_root / f"design_{index}.npz",
                coords=predictions,
                input_coords=reference[None, None, :, :],
                atom_to_token=atom_to_token,
                atom_resolved_mask=np.ones((1, atom_count), dtype=bool),
                backbone_mask=np.ones((1, atom_count), dtype=np.float32),
                token_index=np.arange(MODULE.TOTAL_TOKENS, dtype=np.int64)[None, :],
                mol_type=np.zeros((1, MODULE.TOTAL_TOKENS), dtype=np.int64),
                res_type=np.arange(MODULE.TOTAL_TOKENS, dtype=np.int64)[None, :] % 20,
                **varied_metrics,
            )

    def make_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        t11 = root / "t11"
        t12 = root / "t12"
        self.make_method(t11, cdr_offset=5.0, historical_gate_pattern=True)
        self.make_method(t12, cdr_offset=7.0)
        for index in range(MODULE.EXPECTED_CANDIDATE_COUNT):
            shutil.copyfile(t11 / f"design_{index}.cif", t12 / f"design_{index}.cif")
            shutil.copyfile(t11 / f"design_{index}.npz", t12 / f"design_{index}.npz")

        rows, _, _ = MODULE._load_method(
            MODULE.METHODS[0],
            t11,
            MODULE.EXPECTED_CANDIDATE_COUNT,
            MODULE.FRAMEWORK_THRESHOLD_ANGSTROM,
            MODULE.TARGET_THRESHOLD_ANGSTROM,
        )
        summary = MODULE._method_summary(rows, MODULE.EXPECTED_CANDIDATE_COUNT)
        candidate_summaries = {}
        for index in range(MODULE.EXPECTED_CANDIDATE_COUNT):
            candidate_id = f"design_{index}"
            selected = [row for row in rows if row["candidate_id"] == candidate_id]
            candidate_summaries[candidate_id] = {
                "sample_count": MODULE.SAMPLES_PER_DESIGN,
                "framework_le_threshold_count": sum(
                    bool(row["framework_le_threshold"]) for row in selected
                ),
                MODULE.FRAMEWORK_METRIC: MODULE._metric_summary(
                    [float(row[MODULE.FRAMEWORK_METRIC]) for row in selected]
                ),
                MODULE.PRIMARY_METRIC: MODULE._metric_summary(
                    [float(row[MODULE.PRIMARY_METRIC]) for row in selected]
                ),
            }
        historical = root / "historical.json"
        historical.write_text(
            json.dumps(
                {
                    "schema_version": MODULE.HISTORICAL_AUDIT_SCHEMA,
                    "status": "FAIL",
                    "exit_code": 42,
                    "gpu_performed": False,
                    "total_sample_count": 155,
                    "gate": {
                        "arm": "fixed_ifold",
                        "denominator": 30,
                        "failure_action": "DO_NOT_START_T12_GPU",
                        "metric": MODULE.FRAMEWORK_METRIC,
                        "minimum_pass_count": 10,
                        "observed_pass_count": 7,
                        "operator": "<=",
                        "passed": False,
                        "threshold_angstrom": MODULE.FRAMEWORK_THRESHOLD_ANGSTROM,
                    },
                    "inputs": {"fixed_ifold": str(t11)},
                    "arm_summaries": {
                        "fixed_ifold": {
                            "design_count": MODULE.EXPECTED_CANDIDATE_COUNT,
                            "sample_count": summary["sample_count"],
                            "framework_le_threshold_count": summary["threshold_counts"][
                                "framework_aligned_cdr_rmsd_le_4_angstrom"
                            ],
                            "framework_le_8_angstrom_count": summary["threshold_counts"][
                                "framework_aligned_cdr_rmsd_le_8_angstrom"
                            ],
                            MODULE.FRAMEWORK_METRIC: summary["metrics"][
                                MODULE.FRAMEWORK_METRIC
                            ],
                            MODULE.PRIMARY_METRIC: summary["metrics"][MODULE.PRIMARY_METRIC],
                        }
                    },
                    "candidate_summaries": {"fixed_ifold": candidate_summaries},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return t11, t12, historical

    def test_compare_uses_candidate_level_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            t11, t12, historical = self.make_inputs(root)
            payload, samples, candidates, paired = MODULE.compare(
                t11, t12, historical
            )
            self.assertEqual(payload["sample_count"], 60)
            self.assertEqual(len(samples), 60)
            self.assertEqual(len(candidates), 12)
            self.assertEqual(
                len(paired),
                MODULE.EXPECTED_CANDIDATE_COUNT * len(MODULE.METRIC_DIRECTIONS),
            )
            self.assertEqual(payload["source_read_replay"]["status"], "PASS")
            self.assertEqual(payload["source_read_replay"]["files_replayed"], 38)
            self.assertFalse(
                payload["sample_grain"]["sample_index_paired_across_methods"]
            )
            primary = payload["paired_candidate_comparisons"][MODULE.PRIMARY_METRIC]
            self.assertEqual(
                primary["candidate_direction_counts"]["worsened"],
                MODULE.EXPECTED_CANDIDATE_COUNT,
            )
            self.assertFalse(primary["inference_test_performed"])

    def test_rejects_cross_method_input_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            t11, t12, historical = self.make_inputs(root)
            (t12 / "design_0.cif").write_bytes(b"changed\n")
            with self.assertRaisesRegex(MODULE.ValidationError, "input_cif_sha256 mismatch"):
                MODULE.compare(t11, t12, historical)

    def test_rejects_historical_baseline_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            t11, t12, historical = self.make_inputs(root)
            value = json.loads(historical.read_text(encoding="utf-8"))
            value["arm_summaries"]["fixed_ifold"][MODULE.PRIMARY_METRIC]["median"] += 1.0
            historical.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ValidationError, "baseline reproduction failed"):
                MODULE.compare(t11, t12, historical)

    def test_main_writes_closed_attempt_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            t11, t12, historical = self.make_inputs(root)
            payload, samples, candidates, paired = MODULE.compare(t11, t12, historical)
            payload["code_provenance"] = {
                "comparator_sha256": "e" * 64,
                "audit_dependency_sha256": payload["audit_dependency_sha256"],
            }
            parent = root / "attempts"
            parent.mkdir()
            output = parent / "attempt_20260903T010203Z"
            write_kwargs = {
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "t11_receipt_sha256": "c" * 64,
                "t12_receipt_sha256": "d" * 64,
                "started_at_utc": "2026-09-03T01:02:03Z",
                "analysis_duration_seconds": 0.1,
                "command": ["python", "comparator.py", "--path", "value with space"],
            }
            self.assertEqual(
                MODULE.write_attempt(
                    output, payload, samples, candidates, paired, **write_kwargs
                ),
                output,
            )
            self.assertEqual(
                (output / "STATUS.txt").read_text(encoding="utf-8").strip(),
                MODULE.STATUS,
            )
            self.assertEqual(
                len(
                    (output / "reports" / "POST_T12_SAMPLE_METRICS.tsv")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
                61,
            )
            for line in (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
                expected, relative = line.split("  ./", maxsplit=1)
                observed = hashlib.sha256((output / relative).read_bytes()).hexdigest()
                self.assertEqual(observed, expected)
            before = hashlib.sha256((output / "SHA256SUMS").read_bytes()).hexdigest()
            with self.assertRaisesRegex(MODULE.ValidationError, "refusing to overwrite"):
                MODULE.write_attempt(
                    output, payload, samples, candidates, paired, **write_kwargs
                )
            after = hashlib.sha256((output / "SHA256SUMS").read_bytes()).hexdigest()
            self.assertEqual(after, before)

    def test_protocol_dimensions_are_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            t11, t12, historical = self.make_inputs(root)
            with self.assertRaisesRegex(MODULE.ValidationError, "candidate count is fixed"):
                MODULE.compare(t11, t12, historical, candidate_count=5)
            with self.assertRaisesRegex(MODULE.ValidationError, "target threshold is fixed"):
                MODULE.compare(t11, t12, historical, target_threshold=7.0)

    def test_rejects_extra_candidate_cif(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            t11, t12, historical = self.make_inputs(root)
            (t12 / "design_6.cif").write_bytes(b"extra\n")
            with self.assertRaisesRegex(MODULE.ValidationError, "CIF candidate closure"):
                MODULE.compare(t11, t12, historical)

    def test_isolated_cli_help(self) -> None:
        process = subprocess.run(
            [sys.executable, "-B", "-I", str(SCRIPT), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--t11-design-root", process.stdout)


if __name__ == "__main__":
    unittest.main()
