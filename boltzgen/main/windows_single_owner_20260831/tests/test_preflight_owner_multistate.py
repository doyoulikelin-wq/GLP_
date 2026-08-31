from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "preflight_owner_multistate.py"
SPEC = importlib.util.spec_from_file_location("preflight_owner_multistate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PreflightOwnerMultistateTest(unittest.TestCase):
    def test_production_isolated_cli_can_load_sibling_validator(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--run-root", result.stdout)

    def config(self, root: Path, runtime: Path) -> dict:
        design_dir = root / "design_inputs"
        return {
            "_target_": "boltzgen.task.predict.predict.Predict",
            "data": {
                "_target_": "boltzgen.task.predict.data_from_generated.FromGeneratedDataModule",
                "cfg": {
                    "batch_size": 1,
                    "num_workers": 1,
                    "moldir": str(runtime / "mols.zip"),
                },
                "design_dir": str(design_dir),
                "target_templates": True,
                "return_native": False,
                "fail_if_no_designs": True,
                "skip_existing": False,
            },
            "writer": {"design_dir": str(design_dir)},
            "trainer": {
                "accelerator": "gpu",
                "devices": 1,
                "precision": "bf16-mixed",
            },
            "checkpoint": str(runtime / "boltz2_conf_final.ckpt"),
            "recycling_steps": 3,
            "sampling_steps": 200,
            "diffusion_samples": 5,
            "override": {"use_kernels": True},
        }

    def test_accepts_frozen_single_gpu_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "run"
            runtime = base / "runtime"
            root.mkdir()
            runtime.mkdir()
            checks = MODULE.validate_config(
                root, self.config(root, runtime), runtime_root=runtime, samples_per_task=5
            )
            self.assertEqual(checks["trainer.devices"], 1)
            self.assertEqual(checks["diffusion_samples"], 5)

    def test_rejects_reuse_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "run"
            runtime = base / "runtime"
            root.mkdir()
            runtime.mkdir()
            config = self.config(root, runtime)
            config["data"]["skip_existing"] = True
            with self.assertRaisesRegex(MODULE.ValidationError, "skip_existing"):
                MODULE.validate_config(
                    root, config, runtime_root=runtime, samples_per_task=5
                )


if __name__ == "__main__":
    unittest.main()
