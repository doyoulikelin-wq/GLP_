from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import gemmi
import numpy as np


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "summarize_owner_multistate.py"
SPEC = importlib.util.spec_from_file_location("summarize_owner_multistate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_cif(path: Path, chains: list[tuple[str, list[str]]]) -> None:
    structure = gemmi.Structure()
    structure.name = "fixture"
    model = gemmi.Model("1")
    three = {"A": "ALA", "G": "GLY", "S": "SER"}
    atom_serial = 1
    for chain_name, sequence in chains:
        chain = gemmi.Chain(chain_name)
        for index, one in enumerate(sequence, 1):
            residue = gemmi.Residue()
            residue.name = three[one]
            residue.seqid = gemmi.SeqId(index, " ")
            residue.entity_type = gemmi.EntityType.Polymer
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.serial = atom_serial
            atom.pos = gemmi.Position(float(index), float(ord(chain_name)), 0.0)
            atom_serial += 1
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    path.parent.mkdir(parents=True, exist_ok=True)
    structure.make_mmcif_document().write_file(str(path))


class SummarizeOwnerMultistateTest(unittest.TestCase):
    def make_run(self, root: Path, *, bad_sample_count: bool = False) -> None:
        design_dir = root / "design_inputs"
        fold_dir = design_dir / "fold_out_npz"
        cif_dir = design_dir / "refold_cif"
        fold_dir.mkdir(parents=True)
        cif_dir.mkdir()
        source = root / "source_target.cif"
        write_cif(source, [("A", ["A"])])
        tasks = []
        geometry_vectors = {}
        for state in ("DEV_00", "DEV_01"):
            task_id = f"design_1_{state.lower()}"
            input_cif = design_dir / f"{task_id}.cif"
            input_npz = design_dir / f"{task_id}.npz"
            write_cif(input_cif, [("A", ["A"]), ("B", ["G"])])
            np.savez_compressed(
                input_npz,
                design_mask=np.asarray([0, 1], dtype=np.float32),
                mol_type=np.zeros(2, dtype=np.int64),
                ss_type=np.zeros(2, dtype=np.int64),
                token_resolved_mask=np.ones(2, dtype=np.float32),
                binding_type=np.asarray([1, 0], dtype=np.float32),
            )
            count = 4 if bad_sample_count and state == "DEV_01" else 5
            metrics = {
                name: np.linspace(0.1, 0.5, count, dtype=np.float32)
                for name in MODULE.REQUIRED_METRICS
            }
            state_scale = 1.0 if state == "DEV_00" else 2.0
            input_coords = np.asarray(
                [[[[0.0, 0.0, 0.0], [state_scale, 0.0, 0.0], [0.0, 1.0, 0.0], [4.0, 0.0, 0.0]]]],
                dtype=np.float32,
            )
            atom_to_token = np.asarray(
                [[[1, 0], [1, 0], [1, 0], [0, 1]]], dtype=bool
            )
            atom_resolved_mask = np.ones((1, 4), dtype=bool)
            np.savez_compressed(
                fold_dir / f"{task_id}.npz",
                coords=np.zeros((count, 4, 3), dtype=np.float32),
                input_coords=input_coords,
                atom_to_token=atom_to_token,
                atom_resolved_mask=atom_resolved_mask,
                **metrics,
            )
            geometry_vectors[task_id] = MODULE.target_pairwise_distance_vector(
                input_coords, atom_to_token, atom_resolved_mask, 1
            )
            write_cif(cif_dir / f"{task_id}.cif", [("A", ["A"]), ("B", ["G"])])
            tasks.append(
                {
                    "task_id": task_id,
                    "candidate_id": "design_1",
                    "target_state_id": state,
                    "panel_role": "positive_primary" if state == "DEV_00" else "control",
                    "target_identity": state,
                    "target_sequence": "A",
                    "vhh_sequence": "G",
                    "design_mask_count": 1,
                    "input_cif_relative_path": input_cif.relative_to(root).as_posix(),
                    "input_npz_relative_path": input_npz.relative_to(root).as_posix(),
                    "target_source_path": str(source),
                    "target_source_sha256": sha256(source),
                }
            )
        (root / "tasks.json").write_text(
            json.dumps(
                {
                    "schema_version": "TEST",
                    "status": "INPUTS_READY",
                    "samples_per_task": 5,
                    "candidate_ids": ["design_1"],
                    "state_ids": ["DEV_00", "DEV_01"],
                    "tasks": tasks,
                }
            ),
            encoding="utf-8",
        )
        logs = root / "operator_logs"
        logs.mkdir()
        geometry_path = logs / "preflight_target_geometry.npz"
        np.savez_compressed(geometry_path, **geometry_vectors)
        (logs / "preflight_contract.json").write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "coordinate_contract_relative_path": geometry_path.relative_to(
                        root
                    ).as_posix(),
                    "coordinate_contract_sha256": sha256(geometry_path),
                }
            ),
            encoding="utf-8",
        )

    def test_validates_all_folds_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root)
            contract = MODULE.execute(root, "DEV_00")
            self.assertEqual(contract["status"], "PASS")
            self.assertEqual(contract["logical_task_count"], 2)
            self.assertEqual(contract["sample_row_count"], 10)
            self.assertFalse(contract["best_fold_only"])
            fold_rows = (root / "reports" / "fold_metrics.csv").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(fold_rows), 11)

    def test_rejects_wrong_fold_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root, bad_sample_count=True)
            with self.assertRaisesRegex(MODULE.ValidationError, "shape"):
                MODULE.execute(root, "DEV_00")

    def test_rejects_same_sequence_state_output_swap_by_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root)
            first = root / "design_inputs" / "fold_out_npz" / "design_1_dev_00.npz"
            second = root / "design_inputs" / "fold_out_npz" / "design_1_dev_01.npz"
            first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
            first.write_bytes(second_bytes)
            second.write_bytes(first_bytes)
            with self.assertRaisesRegex(MODULE.ValidationError, "declared state"):
                MODULE.execute(root, "DEV_00")


if __name__ == "__main__":
    unittest.main()
