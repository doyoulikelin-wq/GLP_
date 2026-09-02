"""CPU tests for independent T12 split-template output validation."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import yaml


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_t12_split_template_gpu.py"
SPEC = importlib.util.spec_from_file_location("validate_t12_split_template_gpu", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {SCRIPT}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ValidateT12SplitTemplateGpuTests(unittest.TestCase):
    def write_input_npz(self, path: Path) -> None:
        design_mask = np.zeros(151, dtype=np.float32)
        design_mask[30:60] = 1
        np.savez(
            path,
            design_mask=design_mask,
            mol_type=np.zeros(151, dtype=np.int64),
            ss_type=np.zeros(151, dtype=np.int64),
            token_resolved_mask=np.ones(151, dtype=np.float32),
            binding_type=np.zeros(151, dtype=np.float32),
        )

    def write_fold_npz(
        self,
        path: Path,
        *,
        samples: int = 5,
        nonfinite: bool = False,
    ) -> None:
        atom_count = 151
        coords = np.zeros((samples, atom_count, 3), dtype=np.float32)
        if nonfinite:
            coords[0, 0, 0] = np.nan
        atom_to_token = np.eye(151, dtype=bool)[None, :, :]
        arrays = {
            "coords": coords,
            "input_coords": np.zeros((1, 1, atom_count, 3), dtype=np.float32),
            "token_index": np.arange(151, dtype=np.int64)[None, :],
            "mol_type": np.zeros((1, 151), dtype=np.int64),
            "res_type": np.zeros((1, 151, 33), dtype=np.int64),
            "atom_to_token": atom_to_token,
            "atom_resolved_mask": np.ones((1, atom_count), dtype=bool),
            "backbone_mask": np.ones((1, atom_count), dtype=np.float32),
        }
        arrays.update(
            {
                name: np.linspace(0.1, 0.5, samples, dtype=np.float32)
                for name in MODULE.REQUIRED_SAMPLE_METRICS
            }
        )
        np.savez(path, **arrays)

    def make_run(self, temporary: str) -> tuple[Path, Path]:
        base = Path(temporary).resolve()
        source_attempt = base / "source_t11_attempt"
        source_designs = source_attempt / "intermediate_designs"
        run_root = base / "t12_run"
        design_dir = run_root / "intermediate_designs"
        fold_dir = design_dir / "fold_out_npz"
        refold_dir = design_dir / "refold_cif"
        logs = run_root / "operator_logs"
        config_dir = run_root / "config"
        for directory in (
            source_designs,
            design_dir,
            fold_dir,
            refold_dir,
            logs,
            config_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        for candidate_id in MODULE.EXPECTED_IDS:
            cif = source_designs / f"{candidate_id}.cif"
            cif.write_text(f"data_{candidate_id}\n_atom_site.id 1\n", encoding="utf-8")
            self.write_input_npz(source_designs / f"{candidate_id}.npz")
            shutil.copy2(cif, design_dir / cif.name)
            shutil.copy2(
                source_designs / f"{candidate_id}.npz",
                design_dir / f"{candidate_id}.npz",
            )
            self.write_fold_npz(fold_dir / f"{candidate_id}.npz")
            (refold_dir / f"{candidate_id}.cif").write_text(
                f"data_refold_{candidate_id}\n_atom_site.id 1\n",
                encoding="utf-8",
            )

        files = {
            name: {
                "sha256": sha256(source_designs / name),
                "size_bytes": (source_designs / name).stat().st_size,
            }
            for name in sorted(MODULE.EXPECTED_INPUT_NAMES)
        }
        manifest = {
            "schema_version": MODULE.SOURCE_SCHEMA,
            "source_t11_attempt": str(source_attempt),
            "files": files,
        }
        for name in ("SOURCE_INPUTS_BEFORE.json", "SOURCE_INPUTS_AFTER.json"):
            (logs / name).write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        folding = {
            "_target_": "boltzgen.task.predict.predict.Predict",
            "data": {
                "_target_": MODULE.DATA_MODULE_TARGET,
                "target_templates": True,
                "design_mask_templates": False,
                "expected_target_tokens": 30,
                "expected_cdr_tokens": 30,
                "expected_framework_tokens": 91,
                "skip_existing": False,
            },
            "diffusion_samples": 5,
        }
        (config_dir / "folding.yaml").write_text(
            yaml.safe_dump(folding, sort_keys=False), encoding="utf-8"
        )
        (run_root / "steps.yaml").write_text(
            yaml.safe_dump(
                {
                    "steps": [
                        {"name": "folding", "config_file": "config/folding.yaml"}
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return run_root, logs / "SOURCE_INPUTS_AFTER.json"

    def test_valid_run_emits_small_complete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root, manifest = self.make_run(temporary)
            payload = MODULE.validate_run(
                run_root,
                manifest.relative_to(run_root),
                fold_samples=5,
            )
            self.assertEqual(payload["schema_version"], MODULE.SCHEMA)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["candidate_ids"], list(MODULE.EXPECTED_IDS))
            self.assertEqual(payload["observed_fold_sample_count"], 30)
            self.assertEqual(payload["source_input_manifest"]["replayed_file_count"], 12)
            self.assertEqual(
                payload["resolved_execution_contract"]["expected_total_tokens"], 151
            )
            self.assertEqual(set(payload["per_candidate"]), set(MODULE.EXPECTED_IDS))
            self.assertEqual(payload["per_candidate"]["design_0"]["fold_samples"], 5)
            self.assertEqual(payload["per_candidate"]["design_0"]["atom_count"], 151)
            self.assertRegex(payload["semantic_payload_manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(payload["semantic_payload_files"]), 28)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = MODULE.main(
                    [
                        "validate-run",
                        str(run_root),
                        "--source-input-manifest",
                        "operator_logs/SOURCE_INPUTS_AFTER.json",
                        "--resolved-config",
                        "config/folding.yaml",
                        "--fold-samples",
                        "5",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")

    def test_rejects_before_after_manifest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root, manifest = self.make_run(temporary)
            before_path = manifest.with_name("SOURCE_INPUTS_BEFORE.json")
            before = json.loads(before_path.read_text(encoding="utf-8"))
            before["files"]["design_0.cif"]["sha256"] = "0" * 64
            before_path.write_text(json.dumps(before), encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.ValidationError, "BEFORE/AFTER manifests do not match"
            ):
                MODULE.validate_run(run_root, manifest)

    def test_rejects_wrong_fold_sample_count_and_nonfinite_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root, manifest = self.make_run(temporary)
            fold = run_root / "intermediate_designs/fold_out_npz/design_3.npz"
            self.write_fold_npz(fold, samples=4)
            with self.assertRaisesRegex(MODULE.ValidationError, "coords shape must be"):
                MODULE.validate_run(run_root, manifest)
            self.write_fold_npz(fold, nonfinite=True)
            with self.assertRaisesRegex(MODULE.ValidationError, "NaN/Inf is forbidden"):
                MODULE.validate_run(run_root, manifest)

    def test_rejects_candidate_closure_and_pose_coupled_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root, manifest = self.make_run(temporary)
            (run_root / "intermediate_designs/design_6.npz").write_bytes(b"extra")
            with self.assertRaisesRegex(MODULE.ValidationError, "input NPZ closure mismatch"):
                MODULE.validate_run(run_root, manifest)
            (run_root / "intermediate_designs/design_6.npz").unlink()
            config_path = run_root / "config/folding.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["data"]["design_mask_templates"] = True
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.ValidationError, "data.design_mask_templates"
            ):
                MODULE.validate_run(run_root, manifest)


if __name__ == "__main__":
    unittest.main()
