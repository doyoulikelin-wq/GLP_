from __future__ import annotations

import gzip
import json
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from conftest import run_python


REQUIRED_NUMERIC = [
    "bb_rmsd",
    "bb_rmsd_design",
    "bindsite_under_8rmsd",
    "design_to_target_iptm",
    "design_ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
    "plip_hbonds_refolded",
    "plip_saltbridge_refolded",
    "delta_sasa_refolded",
    "CYS_fraction",
    "ALA_fraction",
    "GLY_fraction",
    "GLU_fraction",
    "LEU_fraction",
    "VAL_fraction",
]

THREE_LETTER = {
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

WRITER_SEQUENCE_TAG = "_entity_poly.pdbx_seq_one_letter_code"
CANONICAL_SEQUENCE_TAG = "_entity_poly.pdbx_seq_one_letter_code_can"


def write_mmcif(
    path: Path,
    *,
    designed_sequence: str = "ACDEFGHIK",
    coordinates: np.ndarray | None = None,
    materialize_designed_entity: bool = True,
    sequence_tag: str = WRITER_SEQUENCE_TAG,
) -> None:
    """Write the subset emitted by BoltzGen's Gemmi mmCIF writer."""
    assert sequence_tag in {WRITER_SEQUENCE_TAG, CANONICAL_SEQUENCE_TAG}
    target_sequence = "LMNP"
    residue_count = len(target_sequence) + len(designed_sequence)
    if coordinates is None:
        coordinates = np.arange(residue_count * 9, dtype=float).reshape(
            residue_count * 3, 3
        )
    coordinates = np.asarray(coordinates, dtype=float)
    assert coordinates.shape == (residue_count * 3, 3)
    lines = [
        "data_candidate",
        "loop_",
        "_entity_poly.entity_id",
        "_entity_poly.type",
        sequence_tag,
        f"1 polypeptide(L) {target_sequence}",
        f"2 polypeptide(L) {designed_sequence}",
        "#",
        "loop_",
        "_entity_poly_seq.entity_id",
        "_entity_poly_seq.num",
        "_entity_poly_seq.mon_id",
    ]
    for entity_id, sequence in (("1", target_sequence), ("2", designed_sequence)):
        lines.extend(
            f"{entity_id} {index} {THREE_LETTER[residue]}"
            for index, residue in enumerate(sequence, start=1)
        )
    lines.extend(
        [
            "#",
            "loop_",
            "_struct_asym.id",
            "_struct_asym.entity_id",
            "A 1",
        ]
    )
    if materialize_designed_entity:
        lines.append("B 2")
    lines.extend(
        [
            "#",
            "loop_",
            "_atom_site.id",
            "_atom_site.group_PDB",
            "_atom_site.label_atom_id",
            "_atom_site.label_comp_id",
            "_atom_site.label_asym_id",
            "_atom_site.label_entity_id",
            "_atom_site.label_seq_id",
            "_atom_site.Cartn_x",
            "_atom_site.Cartn_y",
            "_atom_site.Cartn_z",
            "_atom_site.pdbx_PDB_model_num",
        ]
    )
    atom_id = 0
    coordinate_index = 0
    entities = [("1", "A", target_sequence)]
    if materialize_designed_entity:
        entities.append(("2", "B", designed_sequence))
    for entity_id, chain_id, sequence in entities:
        for residue_number, residue in enumerate(sequence, start=1):
            for atom_name in ("N", "CA", "C"):
                atom_id += 1
                xyz = coordinates[coordinate_index]
                coordinate_index += 1
                lines.append(
                    f"{atom_id} ATOM {atom_name} {THREE_LETTER[residue]} "
                    f"{chain_id} {entity_id} {residue_number} "
                    f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f} 1"
                )
    lines.extend(["#", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_metadata_npz(path: Path, *, inverse: bool = False) -> None:
    """Mirror DesignWriter's real token-metadata NPZ schema."""
    token_count = 13
    arrays: dict[str, np.ndarray] = {
        "design_mask": np.asarray([0.0] * 4 + [1.0] * 9, dtype=np.float32),
        "mol_type": np.zeros(token_count, dtype=np.int64),
        "ss_type": np.zeros(token_count, dtype=np.int64),
        "token_resolved_mask": np.ones(token_count, dtype=np.float32),
        "binding_type": np.zeros(token_count, dtype=np.float32),
    }
    if inverse:
        arrays["inverse_fold_design_mask"] = np.asarray(
            [0.0] * 4 + [1.0] * 9, dtype=np.float32
        )
    np.savez(path, **arrays)


def fold_arrays(*, nonfinite: bool = False) -> dict[str, np.ndarray]:
    atom_count = 39
    token_count = 13
    coords = np.arange(5 * atom_count * 3, dtype=np.float32).reshape(
        5, atom_count, 3
    )
    if nonfinite:
        coords[0, 0, 0] = np.nan
    return {
        "coords": coords,
        # Writer selects index 1; Analysis intentionally selects index 4.
        "iptm": np.asarray([0.1, 0.9, 0.2, 0.3, 0.4], dtype=np.float32),
        "ptm": np.asarray([0.1, 0.8, 0.2, 0.3, 0.4], dtype=np.float32),
        "design_to_target_iptm": np.linspace(0.1, 0.5, 5, dtype=np.float32),
        "design_ptm": np.linspace(0.2, 0.4, 5, dtype=np.float32),
        "min_design_to_target_pae": np.linspace(5.0, 1.0, 5, dtype=np.float32),
        "min_interaction_pae": np.linspace(6.0, 2.0, 5, dtype=np.float32),
        "atom_resolved_mask": np.ones((1, atom_count), dtype=np.int8),
        "backbone_mask": np.ones((1, atom_count), dtype=np.int8),
        "atom_to_token": np.repeat(
            np.eye(token_count, dtype=np.int8), 3, axis=0
        )[None, :, :],
        "token_index": np.arange(token_count, dtype=np.int64)[None, :],
        "mol_type": np.zeros((1, token_count), dtype=np.int64),
        "res_type": np.tile(
            np.asarray([[[1, 0, 0]]], dtype=np.int8), (1, token_count, 1)
        ),
        "input_coords": np.zeros((1, 1, atom_count, 3), dtype=np.float32),
    }


def write_fold_npz(path: Path, *, nonfinite: bool = False) -> dict[str, np.ndarray]:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = fold_arrays(nonfinite=nonfinite)
    np.savez(path, **arrays)
    return arrays


def rewrite_npz(path: Path, field: str, value: np.ndarray) -> None:
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    arrays[field] = value
    np.savez(path, **arrays)


def make_fixture(
    root: Path,
    *,
    nonfinite: bool = False,
    sequence_tag: str = WRITER_SEQUENCE_TAG,
    final_rank: float | str = 1,
    quality_score: float | str = float("nan"),
) -> Path:
    output = root / "cell_output"
    config = output / "config"
    config.mkdir(parents=True)
    configs = {
        "design.yaml": {"data": {"cfg": {"multiplicity": 1}}, "diffusion_samples": 1},
        "inverse_folding.yaml": {"data": {"cfg": {"multiplicity": 1}}},
        "folding.yaml": {"diffusion_samples": 5},
        "filtering.yaml": {"budget": 1},
    }
    for name, payload in configs.items():
        (config / name).write_text(
            yaml.safe_dump(payload, sort_keys=True), encoding="utf-8"
        )
    design = output / "intermediate_designs"
    inverse = output / "intermediate_designs_inverse_folded"
    (inverse / "fold_out_npz").mkdir(parents=True)
    (inverse / "refold_cif").mkdir()
    design.mkdir()
    candidate = "candidate_000"
    arrays = write_fold_npz(
        inverse / "fold_out_npz" / f"{candidate}.npz", nonfinite=nonfinite
    )
    writer_index = int(np.argmax(0.8 * arrays["iptm"] + 0.2 * arrays["ptm"]))
    writer_coordinates = arrays["coords"][writer_index]
    for path in (
        design / f"{candidate}.cif",
        inverse / f"{candidate}.cif",
        inverse / "refold_cif" / f"{candidate}.cif",
    ):
        write_mmcif(
            path,
            coordinates=writer_coordinates,
            sequence_tag=sequence_tag,
        )
    write_metadata_npz(design / f"{candidate}.npz")
    write_metadata_npz(inverse / f"{candidate}.npz", inverse=True)

    row = {column: 0.25 for column in REQUIRED_NUMERIC}
    row.update(
        {
            "id": candidate,
            "file_name": f"{candidate}.cif",
            "designed_chain_sequence": "ACDEFGHIK",
            "design_to_target_iptm": 0.5,
            "design_ptm": 0.4,
            "min_design_to_target_pae": 1.0,
        }
    )
    pd.DataFrame([row]).to_csv(inverse / "aggregate_metrics_analyze.csv", index=False)
    pd.DataFrame(
        [{"id": candidate, "designed_chain_sequence": "ACDEFGHIK"}]
    ).to_pickle(inverse / "ca_coords_sequences.pkl.gz")
    final = output / "final_ranked_designs"
    final.mkdir()
    pd.DataFrame(
        [
            {
                "id": candidate,
                "designed_chain_sequence": "ACDEFGHIK",
                "pass_filters": True,
            }
        ]
    ).to_csv(final / "all_designs_metrics.csv", index=False)
    pd.DataFrame(
        [
            {
                "id": candidate,
                "designed_chain_sequence": "ACDEFGHIK",
                "final_rank": final_rank,
                "quality_score": quality_score,
            }
        ]
    ).to_csv(final / "final_designs_metrics_1.csv", index=False)
    return output


def invoke(output: Path):
    return run_python(
        "validate_cell_output.py",
        output,
        env={"EXPECTED_DESIGNS": "1", "EXPECTED_FOLD_SAMPLES": "5"},
    )


def test_accepts_real_writer_metadata_entity_and_selected_sample_contract(
    tmp_path: Path,
) -> None:
    output = make_fixture(tmp_path)
    result = invoke(output)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "WSL2_BOLTZGEN_CELL_VALIDATION_V2"
    assert payload["status"] == "PASS"
    assert payload["observed_unique_ids"] == 1
    assert payload["fold_samples_per_candidate"] == 5
    assert payload["writer_coordinate_closure_count"] == 1
    assert payload["pickle_deserialization_performed"] is False
    assert payload["opaque_artifact_validation"] == (
        "SINGLE_GZIP_MEMBER_CRC_EOF_BOUNDED_NO_TRAILING_V1"
    )
    assert payload["opaque_artifact_semantic_source"].endswith(
        "aggregate_metrics_analyze.csv"
    )
    assert len(payload["validator_sha256"]) == 64
    assert payload["semantic_payload_file_count"] == len(
        payload["semantic_payload_files"]
    )
    assert [entry["path"] for entry in payload["semantic_payload_files"]] == sorted(
        entry["path"] for entry in payload["semantic_payload_files"]
    )
    assert invoke(output).stdout == result.stdout


def test_accepts_legacy_canonical_sequence_tag_and_finite_quality_score(
    tmp_path: Path,
) -> None:
    output = make_fixture(
        tmp_path,
        sequence_tag=CANONICAL_SEQUENCE_TAG,
        quality_score=0.8,
    )
    result = invoke(output)
    assert result.returncode == 0, result.stderr


def add_canonical_sequence_column(path: Path, *, conflict: bool) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = lines.index(WRITER_SEQUENCE_TAG)
    lines.insert(header_index + 1, CANONICAL_SEQUENCE_TAG)
    for index, line in enumerate(lines):
        fields = line.split()
        if len(fields) == 3 and fields[1] == "polypeptide(L)":
            sequence = fields[2]
            if conflict and fields[0] == "2":
                sequence = sequence[:-1] + ("L" if sequence[-1] != "L" else "K")
            lines[index] = f"{line} {sequence}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_accepts_consistent_dual_sequence_tags_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    consistent = make_fixture(tmp_path / "consistent")
    add_canonical_sequence_column(
        consistent / "intermediate_designs/candidate_000.cif",
        conflict=False,
    )
    result = invoke(consistent)
    assert result.returncode == 0, result.stderr

    conflicting = make_fixture(tmp_path / "conflicting")
    add_canonical_sequence_column(
        conflicting
        / "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif",
        conflict=True,
    )
    result = invoke(conflicting)
    assert result.returncode != 0
    assert "conflicting canonical/native entity sequence" in result.stderr


@pytest.mark.parametrize(
    ("final_rank", "quality_score"),
    [
        (2, float("nan")),
        (1, float("inf")),
        (1, float("-inf")),
        (float("nan"), 0.8),
        (1, "garbage"),
    ],
)
def test_rejects_non_native_singleton_nonfinite_quality_score(
    tmp_path: Path,
    final_rank: float | str,
    quality_score: float | str,
) -> None:
    output = make_fixture(
        tmp_path,
        final_rank=final_rank,
        quality_score=quality_score,
    )
    result = invoke(output)
    assert result.returncode != 0
    assert "final filtering metric" in result.stderr


def test_rejects_nonfinite_fold_npz_or_candidate_count_drift(tmp_path: Path) -> None:
    output = make_fixture(tmp_path / "nonfinite", nonfinite=True)
    assert invoke(output).returncode != 0

    clean = make_fixture(tmp_path / "count")
    result = run_python(
        "validate_cell_output.py",
        clean,
        env={"EXPECTED_DESIGNS": "2", "EXPECTED_FOLD_SAMPLES": "5"},
    )
    assert result.returncode != 0


def test_rejects_raw_design_metadata_npz_nan(tmp_path: Path) -> None:
    output = make_fixture(tmp_path)
    path = output / "intermediate_designs/candidate_000.npz"
    rewrite_npz(path, "design_mask", np.asarray([np.nan, 1.0], dtype=np.float32))
    result = invoke(output)
    assert result.returncode != 0
    assert "metadata" in result.stderr or "non-finite" in result.stderr


@pytest.mark.parametrize("fault", ["nan", "shape", "dtype"])
def test_rejects_inverse_metadata_npz_nan_shape_or_dtype(
    tmp_path: Path,
    fault: str,
) -> None:
    output = make_fixture(tmp_path)
    path = output / "intermediate_designs_inverse_folded/candidate_000.npz"
    if fault == "nan":
        rewrite_npz(path, "binding_type", np.asarray([1.0, np.nan], dtype=np.float32))
    elif fault == "shape":
        rewrite_npz(path, "ss_type", np.asarray([[0], [1]], dtype=np.int64))
    else:
        rewrite_npz(path, "mol_type", np.asarray(["PROTEIN", "PROTEIN"]))
    result = invoke(output)
    assert result.returncode != 0


def test_rejects_decoy_canonical_sequence_not_bound_to_materialized_design_entity(
    tmp_path: Path,
) -> None:
    output = make_fixture(tmp_path)
    refold = output / (
        "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif"
    )
    with np.load(
        output / "intermediate_designs_inverse_folded/fold_out_npz/candidate_000.npz",
        allow_pickle=False,
    ) as archive:
        writer_index = int(np.argmax(0.8 * archive["iptm"] + 0.2 * archive["ptm"]))
        coordinates = np.asarray(archive["coords"][writer_index])
    write_mmcif(
        refold,
        designed_sequence="ACDEFGHIK",
        coordinates=coordinates,
        materialize_designed_entity=False,
    )
    result = invoke(output)
    assert result.returncode != 0
    assert "entity" in result.stderr or "chain" in result.stderr


def test_rejects_refold_coordinates_from_non_writer_selected_sample(tmp_path: Path) -> None:
    output = make_fixture(tmp_path)
    fold = output / (
        "intermediate_designs_inverse_folded/fold_out_npz/candidate_000.npz"
    )
    with np.load(fold, allow_pickle=False) as archive:
        writer_index = int(np.argmax(0.8 * archive["iptm"] + 0.2 * archive["ptm"]))
        analysis_index = int(
            np.argmax(
                0.8 * archive["design_to_target_iptm"] + 0.2 * archive["design_ptm"]
            )
        )
        assert writer_index != analysis_index
        wrong_coordinates = np.asarray(archive["coords"][analysis_index])
    write_mmcif(
        output
        / "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif",
        coordinates=wrong_coordinates,
    )
    result = invoke(output)
    assert result.returncode != 0
    assert "Writer" in result.stderr or "coordinate" in result.stderr


def test_rejects_mmcif_sequence_or_nonfinite_coordinate(tmp_path: Path) -> None:
    sequence_drift = make_fixture(tmp_path / "sequence")
    write_mmcif(
        sequence_drift
        / "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif",
        designed_sequence="ACDEFGHIL",
    )
    assert invoke(sequence_drift).returncode != 0

    bad_coordinate = make_fixture(tmp_path / "coordinate")
    refold = bad_coordinate / (
        "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif"
    )
    text = refold.read_text(encoding="utf-8").replace("117.000000", "nan", 1)
    refold.write_text(text, encoding="utf-8")
    result = invoke(bad_coordinate)
    assert result.returncode != 0
    assert "non-finite" in result.stderr


@pytest.mark.parametrize(
    "fault",
    ["truncated_residue", "missing_backbone", "duplicate_atom", "extra_model", "seq_gap"],
)
def test_rejects_incomplete_or_ambiguous_canonical_mmcif(
    tmp_path: Path, fault: str
) -> None:
    output = make_fixture(tmp_path)
    path = output / "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif"
    lines = path.read_text(encoding="utf-8").splitlines()
    atom_rows = [
        index
        for index, line in enumerate(lines)
        if len(line.split()) == 11 and line.split()[1:2] == ["ATOM"]
    ]
    assert len(atom_rows) == 39
    if fault == "truncated_residue":
        del lines[atom_rows[-3] : atom_rows[-1] + 1]
    elif fault == "missing_backbone":
        del lines[atom_rows[2]]
    elif fault == "duplicate_atom":
        lines.insert(atom_rows[-1] + 1, lines[atom_rows[0]])
    elif fault == "extra_model":
        fields = lines[atom_rows[0]].split()
        fields[-1] = "2"
        lines[atom_rows[0]] = " ".join(fields)
    else:
        lines.remove("2 9 LYS")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = invoke(output)

    assert result.returncode != 0


@pytest.mark.parametrize("directive", ["data_second", "global_", "save_split"])
def test_rejects_split_mmcif_top_level_scope(tmp_path: Path, directive: str) -> None:
    output = make_fixture(tmp_path)
    path = output / "intermediate_designs_inverse_folded/refold_cif/candidate_000.cif"
    path.write_text(
        path.read_text(encoding="utf-8") + f"{directive}\n#\n",
        encoding="utf-8",
    )

    result = invoke(output)

    assert result.returncode != 0


def test_opaque_pickle_opcodes_are_never_executed(tmp_path: Path) -> None:
    output = make_fixture(tmp_path)
    artifact = output / "intermediate_designs_inverse_folded/ca_coords_sequences.pkl.gz"
    sentinel = tmp_path / "pickle_reduce_executed"

    class Malicious:
        def __reduce__(self):
            return (os.system, (f"/usr/bin/touch -- {sentinel}",))

    artifact.write_bytes(gzip.compress(pickle.dumps(Malicious()), mtime=0))

    result = invoke(output)

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert json.loads(result.stdout)["pickle_deserialization_performed"] is False


@pytest.mark.parametrize(
    "fault", ["truncated", "crc", "trailing", "second_member", "gzip_bomb"]
)
def test_rejects_invalid_or_unbounded_opaque_gzip(tmp_path: Path, fault: str) -> None:
    output = make_fixture(tmp_path)
    artifact = output / "intermediate_designs_inverse_folded/ca_coords_sequences.pkl.gz"
    raw = artifact.read_bytes()
    if fault == "truncated":
        raw = raw[:-4]
    elif fault == "crc":
        changed = bytearray(raw)
        changed[-8] ^= 0x01
        raw = bytes(changed)
    elif fault == "trailing":
        raw += b"trailing-data"
    elif fault == "second_member":
        raw += gzip.compress(b"second member", mtime=0)
    else:
        raw = gzip.compress(b"A" * (16 * 1024 * 1024 + 1), compresslevel=9, mtime=0)
    artifact.write_bytes(raw)

    result = invoke(output)

    assert result.returncode != 0


@pytest.mark.parametrize("fault", ["empty_deflate", "oversize"])
def test_rejects_oversized_compressed_opaque_artifact(
    tmp_path: Path, fault: str
) -> None:
    output = make_fixture(tmp_path)
    artifact = output / "intermediate_designs_inverse_folded/ca_coords_sequences.pkl.gz"
    limit = 16 * 1024 * 1024
    if fault == "empty_deflate":
        header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
        empty_nonfinal = b"\x00\x00\x00\xff\xff"
        empty_final = b"\x01\x00\x00\xff\xff"
        footer = b"\x00" * 8
        raw = header + empty_nonfinal * (limit // 5 + 1) + empty_final + footer
        assert gzip.decompress(raw) == b""
    else:
        raw = b"x" * (limit + 1)
    assert len(raw) > limit
    artifact.write_bytes(raw)

    result = invoke(output)

    assert result.returncode != 0


def test_rejects_symlink_or_noncanonical_output_root(tmp_path: Path) -> None:
    output = make_fixture(tmp_path / "real")
    alias = tmp_path / "output_alias"
    alias.symlink_to(output, target_is_directory=True)

    result = invoke(alias)

    assert result.returncode != 0
    assert "OUTPUT_PATH" in result.stderr or "canonical" in result.stderr
