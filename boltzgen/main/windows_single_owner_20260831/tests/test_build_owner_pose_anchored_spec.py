import importlib.util
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path

import gemmi
import numpy as np
import pytest
import yaml


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "build_owner_pose_anchored_spec.py"
)
SPEC = importlib.util.spec_from_file_location("build_owner_pose_anchored_spec", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


RESIDUE_NAMES = {
    "A": "ALA",
    "C": "CYS",
    "D": "ASP",
    "E": "GLU",
    "H": "HIS",
}


def make_residues(sequence: str, coordinates: np.ndarray) -> list[gemmi.Residue]:
    residues: list[gemmi.Residue] = []
    for index, (code, coordinate) in enumerate(zip(sequence, coordinates), 1):
        residue = gemmi.Residue()
        residue.name = RESIDUE_NAMES[code]
        residue.entity_type = gemmi.EntityType.Polymer
        residue.seqid = gemmi.SeqId(index, " ")
        residue.label_seq = index
        residue.subchain = "E"
        atom = gemmi.Atom()
        atom.name = "CA"
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(*(float(value) for value in coordinate))
        residue.add_atom(atom)
        residues.append(residue)
    return residues


def make_structure(
    auth_chain_id: str, label_chain_id: str, coordinate: tuple[float, float, float]
) -> gemmi.Structure:
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain(auth_chain_id)
    residue = make_residues("A", np.asarray([coordinate]))[0]
    residue.subchain = label_chain_id
    chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    return structure


def write_closed_yaml_fixture(root: Path) -> None:
    root.mkdir()
    (root / "design.yaml").write_text(
        yaml.safe_dump(MODULE.EXPECTED_DESIGN_DOCUMENT, sort_keys=False),
        encoding="utf-8",
    )
    (root / "scaffold.yaml").write_text(
        yaml.safe_dump(MODULE.EXPECTED_SCAFFOLD_DOCUMENT, sort_keys=False),
        encoding="utf-8",
    )
    (root / "target.cif").write_text("target\n", encoding="utf-8")
    (root / "scaffold.cif").write_text("scaffold\n", encoding="utf-8")


def test_chain_mapping_resolves_frozen_label_e_to_auth_p() -> None:
    structure = make_structure("P", "E", (0.0, 0.0, 0.0))

    chain, residues, evidence = MODULE.resolve_spec_chain(
        structure, "E", "frozen_target"
    )

    assert chain.name == "P"
    assert len(residues) == 1
    assert evidence == {
        "role": "frozen_target",
        "spec_chain_id": "E",
        "match_kind": "label_asym_id",
        "auth_asym_id": "P",
        "label_asym_ids": ["E"],
    }


def test_chain_mapping_rejects_ambiguous_label_id() -> None:
    structure = make_structure("P", "E", (0.0, 0.0, 0.0))
    second = gemmi.Chain("Q")
    residue = make_residues("A", np.asarray([[1.0, 0.0, 0.0]]))[0]
    residue.subchain = "E"
    second.add_residue(residue)
    structure[0].add_chain(second)

    with pytest.raises(ValueError, match="must resolve exactly once"):
        MODULE.resolve_spec_chain(structure, "E", "frozen_target")


def test_alignment_diagnostic_recovers_rigid_pose_and_reports_max_residual() -> None:
    moving_coordinates = np.asarray(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.5, 0.5, 1.5]]
    )
    angle = np.deg2rad(31.0)
    expected_rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    expected_translation = np.asarray([4.0, -3.0, 2.0])
    fixed_coordinates = (
        expected_rotation @ moving_coordinates.T
    ).T + expected_translation
    moving = make_residues("ACDE", moving_coordinates)
    fixed = make_residues("ACDE", fixed_coordinates)

    rotation, translation, evidence = MODULE.build_alignment_diagnostic(
        moving, fixed, minimum_aligned_ca=4
    )

    assert np.allclose(rotation, expected_rotation, atol=1e-12)
    assert np.allclose(translation, expected_translation, atol=1e-12)
    assert evidence["rmsd_angstrom"] < 1e-12
    assert evidence["ca_residual_max_angstrom"] < 1e-12
    assert evidence["rotation_determinant"] == pytest.approx(1.0)


def test_default_safety_rejects_high_residual_and_cross_chain_clash() -> None:
    alignment = {"rmsd_angstrom": 11.875, "ca_residual_max_angstrom": 25.852}
    clashes = {"atom_pair_count": 116}

    decision = MODULE.evaluate_safety(
        alignment,
        clashes,
        max_ca_rmsd_angstrom=MODULE.DEFAULT_MAX_CA_RMSD_ANGSTROM,
        max_ca_residual_angstrom=MODULE.DEFAULT_MAX_CA_RESIDUAL_ANGSTROM,
        max_heavy_atom_clash_count=MODULE.DEFAULT_MAX_HEAVY_ATOM_CLASH_COUNT,
    )

    assert decision["safe_for_full_chain_rigid_transfer"] is False
    assert decision["runnable_spec_generation_authorized"] is False
    assert decision["runnable_spec_generated"] is False
    assert decision["failed_checks"] == [
        "ca_kabsch_rmsd",
        "maximum_ca_residual",
        "cross_chain_heavy_atom_clash_count",
    ]


def test_cross_chain_clash_counter_uses_strict_heavy_atom_cutoff() -> None:
    target = make_residues("A", np.asarray([[0.0, 0.0, 0.0]]))
    vhh = make_residues("A", np.asarray([[1.5, 0.0, 0.0]]))

    diagnostic = MODULE.diagnose_cross_chain_clashes(target, vhh, 2.0)

    assert diagnostic["atom_pair_count"] == 1
    assert diagnostic["residue_pair_count"] == 1
    assert diagnostic["minimum_distance_angstrom"] == pytest.approx(1.5)


def test_frozen_group_contract_rejects_wrong_framework_visibility(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    write_closed_yaml_fixture(root)
    scaffold = deepcopy(MODULE.EXPECTED_SCAFFOLD_DOCUMENT)
    scaffold["structure_groups"][0]["group"]["visibility"] = 1
    (root / "scaffold.yaml").write_text(
        yaml.safe_dump(scaffold, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="scaffold.yaml.*group contract"):
        MODULE.validate_frozen_spec_contract(root)


def test_four_file_closure_symlink_and_overwrite_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    write_closed_yaml_fixture(root)
    hashes = MODULE.validate_bundle_closure(root)
    assert set(hashes) == set(MODULE.FROZEN_BUNDLE_FILES)

    (root / "extra.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(ValueError, match="closure mismatch"):
        MODULE.validate_bundle_closure(root)
    (root / "extra.txt").unlink()

    (root / "target.cif").unlink()
    (root / "target.cif").symlink_to(root / "scaffold.cif")
    with pytest.raises(ValueError, match="symlink"):
        MODULE.validate_bundle_closure(root)

    output = tmp_path / "existing-output"
    output.mkdir()
    with pytest.raises(FileExistsError, match="overwrite is forbidden"):
        MODULE.ensure_output_absent(output)


def test_chain_inventory_rejects_duplicate_atom_identity() -> None:
    residues = make_residues("A", np.asarray([[0.0, 0.0, 0.0]]))
    duplicate = gemmi.Atom()
    duplicate.name = "CA"
    duplicate.element = gemmi.Element("C")
    duplicate.pos = gemmi.Position(1.0, 0.0, 0.0)
    residues[0].add_atom(duplicate)

    with pytest.raises(ValueError, match="duplicate atom identities"):
        MODULE.validate_chain_inventory(residues, 1, "synthetic chain")


def test_bound_bytes_survive_replacement_but_terminal_revalidation_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"captured-input\n")
    bound = MODULE.read_bound_file(source)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement-input\n")
    os.replace(replacement, source)

    assert MODULE._require_bound_bytes(bound) == b"captured-input\n"
    with pytest.raises(ValueError, match="terminal input identity/digest changed"):
        MODULE.revalidate_bound_file(bound)


def test_top_seal_rejects_file_injected_after_manifest_write(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    (root / "receipt.json").write_text("{}\n", encoding="utf-8")

    def inject_late_file(sealed_root: Path) -> None:
        (sealed_root / "late.txt").write_text("late\n", encoding="utf-8")

    with pytest.raises(ValueError, match="closure replay mismatch"):
        MODULE.seal_and_verify_output(root, after_manifest_write=inject_late_file)


def test_spec_manifest_replay_rejects_post_manifest_mutation(tmp_path: Path) -> None:
    root = tmp_path / "output"
    bundle = root / "spec_bundle"
    bundle.mkdir(parents=True)
    for name in MODULE.FROZEN_BUNDLE_FILES:
        (bundle / name).write_text(f"{name}\n", encoding="utf-8")
    hashes = {
        name: MODULE.stable_digest(bundle / name)
        for name in MODULE.FROZEN_BUNDLE_FILES
    }
    MODULE.write_spec_bundle_manifest(root, hashes)
    MODULE.verify_spec_bundle_manifest_strict(root)
    (bundle / "target.cif").write_text("mutated\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest replay failed"):
        MODULE.verify_spec_bundle_manifest_strict(root)


def test_source_receipt_target_and_spec_manifest_are_cross_bound(tmp_path: Path) -> None:
    source = tmp_path / "source-target.cif"
    source.write_bytes(b"target-bytes\n")
    bound = MODULE.read_bound_file(source)
    root = tmp_path / "output"
    bundle = root / "spec_bundle"
    bundle.mkdir(parents=True)
    (bundle / "target.cif").write_bytes(MODULE._require_bound_bytes(bound))
    rows = {"spec_bundle/target.cif": bound.sha256}
    receipt = {"source_files": [MODULE._source_file_row(bound)]}
    MODULE.assert_source_target_cross_binding(receipt, bound, root, rows)

    receipt["source_files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="cross-bind failed"):
        MODULE.assert_source_target_cross_binding(receipt, bound, root, rows)


def test_policy_rejects_any_source_candidate_other_than_exact_design_3() -> None:
    arguments = MODULE.parse_args(
        [
            "--spec-bundle",
            "/unused/spec",
            "--anchor-set",
            "/unused/anchor",
            "--candidate-id",
            "design_1",
            "--output",
            "/unused/output",
            "--boltzgen-launcher",
            "/unused/boltzgen",
            "--moldir",
            "/unused/mols.zip",
        ]
    )

    with pytest.raises(ValueError, match="exact T9 source candidate design_3"):
        MODULE.validate_policy(arguments)


REAL_T9 = Path(
    "/home/lin/creator/gpu_work/owner_mode/t9_local_anchor_set/7xl0_top3/"
    "attempt_20260831T141149Z"
)
REAL_LAUNCHER = Path(
    "/home/lin/creator/gpu_work/environments/cu128_blackwell_candidate/"
    "attempt_004/env/bin/boltzgen"
)
REAL_MOLDIR = Path(
    "/home/lin/creator/gpu_work/runs/t4_spec_gate/attempt_003/runtime_cache/mols.zip"
)


@pytest.mark.skipif(
    not (REAL_T9.is_dir() and REAL_LAUNCHER.is_file() and REAL_MOLDIR.is_file()),
    reason="owner-mode sealed T9/check runtime absent",
)
def test_real_internal_grid_check_terminal_tamper_and_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = MODULE.parse_args(
        [
            "--spec-bundle",
            str(REAL_T9 / "inputs/spec_bundle"),
            "--anchor-set",
            str(REAL_T9),
            "--candidate-id",
            "design_3",
            "--pose-search-mode",
            "internal",
            "--output",
            str(tmp_path / "published"),
            "--boltzgen-launcher",
            str(REAL_LAUNCHER),
            "--moldir",
            str(REAL_MOLDIR),
        ]
    )

    receipt, context = MODULE.collect_diagnostic(arguments)
    rejected_receipt = deepcopy(receipt)
    chosen = receipt["pose_search"]["chosen_pose"]
    geometry = receipt["pose_geometry"]

    assert receipt["status"] == "POSE_ANCHORED_SPEC_CANDIDATE"
    assert context["materialize"] is True
    assert receipt["pose_search"]["evaluated_pose_count"] == 30_600
    assert receipt["pose_search"]["feasible_pose_count"] == 2
    assert (
        chosen["separation_angstrom"],
        chosen["roll_degrees"],
        chosen["tilt_e1_degrees"],
        chosen["tilt_e2_degrees"],
    ) == (8.0, 15, 20, 20)
    assert chosen["hotspot_cdr_ca_distances_angstrom"] == pytest.approx(
        [4.4562471976, 6.9009841196], abs=1e-8
    )
    assert geometry["minimum_target_vhh_heavy_atom_distance_angstrom"] == pytest.approx(
        2.0394417922, abs=1e-8
    )
    assert geometry["minimum_target_vhh_ca_distance_angstrom"] == pytest.approx(
        4.4562471976, abs=1e-8
    )
    assert geometry["cdr_contact_residue_count_within_5_angstrom"] == 5
    assert geometry["cdr_target_heavy_atom_pairs_2_to_5_angstrom"] == 158
    assert geometry["framework_target_heavy_atom_pairs_2_to_5_angstrom"] == 0
    assert receipt["pose_gates"]["all_hard_gates_passed"] is True

    output = tmp_path / "published"
    MODULE.publish_diagnostic(output, receipt, context)
    published = json.loads((output / "POSE_ANCHORED_SPEC.json").read_text())
    assert published["status"] == "POSE_ANCHORED_SPEC_READY"
    assert published["runner_input"] == "spec_bundle/design.yaml"
    assert published["boltzgen_check"]["exit_code"] == 0
    semantic = published["boltzgen_check"]["semantic_validation"]
    assert semantic["label_chain_ids"] == ["E", "A"]
    assert semantic["binding_b_factor"] == 80.0
    assert semantic["design_b_factor"] == 100.0
    assert semantic["all_other_b_factors"] == 0.0
    assert semantic["disulfide"]["label_seq_positions"] == [22, 95]
    top_rows = MODULE.verify_top_manifest_strict(output)
    MODULE.verify_spec_bundle_manifest_strict(output)
    assert published["boltzgen_check"]["exit_code_relative_path"] == (
        "boltzgen_check/check.exit_code.txt"
    )
    MODULE.validate_terminal_boltzgen_check_artifacts(
        output,
        published,
        top_rows,
        context["target_sequence"],
        context["vhh_sequence"],
    )

    for index, relative in enumerate(
        (
            "boltzgen_check/output/design.cif",
            "boltzgen_check/check.stdout.log",
            "boltzgen_check/check.stderr.log",
            "boltzgen_check/check.exit_code.txt",
        )
    ):
        tampered = tmp_path / f"terminal-artifact-{index}"
        shutil.copytree(output, tampered)
        (tampered / "SHA256SUMS").unlink()
        with (tampered / relative).open("ab") as stream:
            stream.write(b"injected-terminal-mutation\n")
        tampered_rows = MODULE.seal_and_verify_output(tampered)
        tampered_receipt = json.loads(
            (tampered / "POSE_ANCHORED_SPEC.json").read_text()
        )
        with pytest.raises(ValueError, match="artifact SHA cross-bind failed"):
            MODULE.validate_terminal_boltzgen_check_artifacts(
                tampered,
                tampered_receipt,
                tampered_rows,
                context["target_sequence"],
                context["vhh_sequence"],
            )

    bad_check = tmp_path / "bad-check.cif"
    check_structure = MODULE.load_structure(
        output / "boltzgen_check/output/design.cif"
    )
    check_structure[0].find_chain("E")[0][0].b_iso = 0.0
    bad_check.write_text(
        check_structure.make_mmcif_document().as_string(), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="B-factor flag mismatch"):
        MODULE.validate_boltzgen_check_cif(
            MODULE.read_bound_file(bad_check),
            context["target_sequence"],
            context["vhh_sequence"],
        )

    original_seal = MODULE.seal_and_verify_output

    def tamper_check_cif_before_seal(staging: Path, **kwargs):
        with (staging / "boltzgen_check/output/design.cif").open("ab") as stream:
            stream.write(b"injected-after-check-before-seal\n")
        return original_seal(staging, **kwargs)

    monkeypatch.setattr(
        MODULE, "seal_and_verify_output", tamper_check_cif_before_seal
    )
    tampered_output = tmp_path / "tampered-publication"
    with pytest.raises(ValueError, match="artifact SHA cross-bind failed"):
        MODULE.publish_diagnostic(
            tampered_output, deepcopy(rejected_receipt), context
        )
    assert not tampered_output.exists()
    assert not list(tmp_path.glob(".tampered-publication.staging.*"))
    monkeypatch.setattr(MODULE, "seal_and_verify_output", original_seal)

    def fail_check(*_args, **_kwargs):
        raise ValueError("injected check failure")

    monkeypatch.setattr(MODULE, "run_mandatory_boltzgen_check", fail_check)
    failed_output = tmp_path / "failed-publication"
    with pytest.raises(ValueError, match="injected check failure"):
        MODULE.publish_diagnostic(failed_output, rejected_receipt, context)
    assert not failed_output.exists()
    assert not list(tmp_path.glob(".failed-publication.staging.*"))
