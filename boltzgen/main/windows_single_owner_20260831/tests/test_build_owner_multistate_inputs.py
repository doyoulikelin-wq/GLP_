import hashlib
import importlib.util
from pathlib import Path

import gemmi
import numpy as np
import pytest


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "build_owner_multistate_inputs.py"
)
SPEC = importlib.util.spec_from_file_location("build_owner_multistate_inputs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_residues(sequence: str, ca_count: int | None = None) -> list[gemmi.Residue]:
    names = {
        "A": "ALA",
        "C": "CYS",
        "D": "ASP",
        "E": "GLU",
        "F": "PHE",
        "G": "GLY",
        "H": "HIS",
        "I": "ILE",
        "K": "LYS",
        "L": "LEU",
        "M": "MET",
        "N": "ASN",
        "P": "PRO",
        "Q": "GLN",
        "R": "ARG",
        "S": "SER",
        "T": "THR",
        "V": "VAL",
        "W": "TRP",
        "Y": "TYR",
    }
    residues: list[gemmi.Residue] = []
    limit = len(sequence) if ca_count is None else ca_count
    for index, code in enumerate(sequence):
        residue = gemmi.Residue()
        residue.name = names[code]
        residue.entity_type = gemmi.EntityType.Polymer
        residue.seqid = gemmi.SeqId(index + 1, " ")
        if index < limit:
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            # A non-collinear deterministic trace.
            atom.pos = gemmi.Position(float(index), float(index * index), float(index % 2))
            residue.add_atom(atom)
        residues.append(residue)
    return residues


def test_kabsch_recovers_known_rigid_transform() -> None:
    moving = np.asarray(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.5, 0.5, 1.5]]
    )
    angle = np.deg2rad(37.0)
    expected_rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    expected_translation = np.asarray([4.0, -2.5, 7.0])
    fixed = (expected_rotation @ moving.T).T + expected_translation

    rotation, translation, rmsd = MODULE.kabsch_transform(moving, fixed)

    assert rmsd < 1e-12
    assert np.allclose(rotation, expected_rotation, atol=1e-12)
    assert np.allclose(translation, expected_translation, atol=1e-12)
    assert np.linalg.det(rotation) == pytest.approx(1.0)


def test_metadata_replaces_target_and_preserves_exact_vhh_cdr_mask() -> None:
    source_target_length = 4
    vhh_length = 35
    token_count = source_target_length + vhh_length
    design_mask = np.zeros(token_count, dtype=np.float32)
    design_mask[source_target_length : source_target_length + 30] = 1
    source = {
        "design_mask": design_mask,
        "mol_type": np.zeros(token_count, dtype=np.int64),
        "ss_type": np.zeros(token_count, dtype=np.int64),
        "token_resolved_mask": np.ones(token_count, dtype=np.float32),
        "binding_type": np.r_[
            np.asarray([1, 1, 0, 0], dtype=np.float32),
            np.zeros(vhh_length, dtype=np.float32),
        ],
    }

    result = MODULE.build_multistate_metadata(
        source, source_target_length, vhh_length, new_target_length=7
    )

    assert all(array.shape == (42,) for array in result.values())
    assert np.array_equal(result["design_mask"][:7], np.zeros(7))
    assert np.array_equal(result["design_mask"][7:], design_mask[4:])
    assert int(result["design_mask"].sum()) == 30
    assert np.array_equal(result["binding_type"][:7], [1, 1, 0, 0, 0, 0, 0])
    assert np.array_equal(result["binding_type"][7:], source["binding_type"][4:])
    assert np.array_equal(result["token_resolved_mask"][:7], np.ones(7))
    assert result["mol_type"].dtype == source["mol_type"].dtype


def write_catalog(repo: Path, sources: dict[str, bytes]) -> Path:
    catalog = repo / MODULE.STATE_CATALOG_RELATIVE
    catalog.parent.mkdir(parents=True)
    fields = [
        "state_order",
        "target_state_id",
        "panel_role",
        "target_identity",
        "relative_path",
        "sha256",
        "required_status",
        "required_active_for_ai",
        "required_parse_status",
        "required_geometry_complete",
    ]
    rows = []
    for order, (state_id, content) in enumerate(sources.items()):
        relative = Path("data/states") / f"{state_id}.cif"
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        rows.append(
            {
                "state_order": str(order),
                "target_state_id": state_id,
                "panel_role": f"role_{order}",
                "target_identity": f"identity_{order}",
                "relative_path": relative.as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "required_status": "USE_TUNING_CHALLENGE",
                "required_active_for_ai": "true",
                "required_parse_status": "PASS",
                "required_geometry_complete": "true",
            }
        )
    catalog.write_text(
        "\t".join(fields)
        + "\n"
        + "".join("\t".join(row[field] for field in fields) + "\n" for row in rows),
        encoding="utf-8",
    )
    return catalog


def test_state_selection_preserves_request_order_and_rejects_hash_drift(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_catalog(repo, {"DEV_A": b"state-a\n", "DEV_B": b"state-b\n"})

    selected, catalog, catalog_digest = MODULE.load_selected_states(
        repo, ["DEV_B", "DEV_A"]
    )

    assert [row["target_state_id"] for row in selected] == ["DEV_B", "DEV_A"]
    assert catalog == (repo / MODULE.STATE_CATALOG_RELATIVE).resolve()
    assert catalog_digest == hashlib.sha256(catalog.read_bytes()).hexdigest()
    assert selected[0]["source_resolved_path"] == str(
        (repo / "data/states/DEV_B.cif").resolve()
    )

    (repo / "data/states/DEV_B.cif").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        MODULE.load_selected_states(repo, ["DEV_B"])


def test_alignment_rejects_insufficient_ca_correspondence() -> None:
    moving = make_residues("HAEGTF", ca_count=3)
    fixed = make_residues("HAEGTF", ca_count=6)

    with pytest.raises(ValueError, match="insufficient aligned C-alpha pairs"):
        MODULE.align_target_chain(moving, fixed, minimum_aligned_ca=4)


def test_task_id_shape_matches_frozen_boltzgen_target_regex() -> None:
    task_id = MODULE.make_task_id("design_1", "DEV_00")

    assert MODULE.BOLTZGEN_TARGET_ID_REGEX.fullmatch(task_id) is not None
    assert task_id == "design_1_dev_00"
    assert "__" not in task_id
