#!/usr/bin/env python3
"""Validate one BoltzGen v0.3.2 cell against the frozen output contract."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


VALIDATION_SCHEMA = "WSL2_BOLTZGEN_CELL_VALIDATION_V2"
MAX_OPAQUE_GZIP_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_OPAQUE_GZIP_COMPRESSED_BYTES = 16 * 1024 * 1024
OPAQUE_ARTIFACT_VALIDATION = "SINGLE_GZIP_MEMBER_CRC_EOF_BOUNDED_NO_TRAILING_V1"


if not __debug__:
    raise RuntimeError("must run without python -O")


REQUIRED_ANALYSIS_NUMERIC = (
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
)

PER_SAMPLE_KEYS = (
    "iptm",
    "ptm",
    "design_to_target_iptm",
    "design_ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
)

MAPPING_AND_FEATURE_KEYS = (
    "atom_resolved_mask",
    "atom_to_token",
    "token_index",
    "mol_type",
    "res_type",
    "backbone_mask",
    "input_coords",
)
DESIGN_METADATA_REQUIRED = frozenset(
    {
        "design_mask",
        "mol_type",
        "ss_type",
        "token_resolved_mask",
        "binding_type",
    }
)
DESIGN_METADATA_OPTIONAL = frozenset(
    {"inverse_fold_design_mask", "aa_constraint_mask"}
)
# Gemmi 0.7.x writes Cartn_* with six decimal places; cover only that rounding.
WRITER_COORDINATE_ATOL = 5.1e-7
SEQUENCE_RE = re.compile(r"[ACDEFGHIKLMNPQRSTVWY]+")
THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def top_level(directory: Path, suffix: str, *, prefix: str = "") -> list[Path]:
    """Return only first-level materialized files, excluding native sidecars."""
    assert directory.is_dir() and not directory.is_symlink(), (
        "missing or unsafe output directory",
        directory,
    )
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.name.endswith(suffix)
        and not path.name.endswith(f"_native{suffix}")
    )
    for path in paths:
        assert not path.is_symlink(), ("output material must not be a symlink", path)
    return paths


def sha256_stable_file(path: Path) -> str:
    """Hash one canonical regular file while detecting substitution or mutation."""
    assert not path.is_symlink(), ("semantic payload file is a symlink", path)
    assert path.resolve(strict=True) == path, ("semantic payload path is not canonical", path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        assert stat.S_ISREG(before.st_mode), ("semantic payload is not a regular file", path)
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        identity = lambda value: (  # noqa: E731 - compact immutable identity tuple
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        assert identity(before) == identity(after) == identity(current), (
            "semantic payload changed while hashing",
            path,
        )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def assert_opaque_single_gzip(path: Path) -> int:
    """Validate one bounded gzip member without interpreting its payload."""
    assert not path.is_symlink() and path.is_file(), (
        "opaque artifact is missing, non-regular, or a symlink",
        path,
    )
    assert path.resolve(strict=True) == path, ("opaque artifact path is not canonical", path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        assert stat.S_ISREG(before.st_mode), ("opaque artifact is not regular", path)
        assert 0 < before.st_size <= MAX_OPAQUE_GZIP_COMPRESSED_BYTES, (
            "opaque gzip compressed size is outside the frozen limit",
            path,
            before.st_size,
            MAX_OPAQUE_GZIP_COMPRESSED_BYTES,
        )
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        uncompressed_size = 0
        compressed_size = 0
        reached_eof = False
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            compressed_size += len(block)
            assert compressed_size <= MAX_OPAQUE_GZIP_COMPRESSED_BYTES, (
                "opaque gzip exceeded the compressed-byte read limit",
                path,
            )
            pending = block
            while pending:
                remaining = MAX_OPAQUE_GZIP_UNCOMPRESSED_BYTES - uncompressed_size + 1
                try:
                    output = decompressor.decompress(pending, remaining)
                except zlib.error as exc:
                    raise AssertionError(("opaque gzip CRC/stream validation failed", path)) from exc
                uncompressed_size += len(output)
                assert uncompressed_size <= MAX_OPAQUE_GZIP_UNCOMPRESSED_BYTES, (
                    "opaque gzip exceeds the decompression limit",
                    path,
                    MAX_OPAQUE_GZIP_UNCOMPRESSED_BYTES,
                )
                if decompressor.eof:
                    trailing = decompressor.unused_data + os.read(descriptor, 1)
                    assert not trailing, (
                        "opaque gzip has a second member or trailing bytes",
                        path,
                    )
                    reached_eof = True
                    pending = b""
                    break
                next_pending = decompressor.unconsumed_tail
                assert next_pending != pending, ("opaque gzip decoder made no progress", path)
                pending = next_pending
            if reached_eof:
                break
        assert reached_eof and decompressor.eof, (
            "opaque gzip is truncated or lacks a complete CRC/EOF",
            path,
        )
        assert uncompressed_size > 0, ("opaque gzip payload is empty", path)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        identity = lambda value: (  # noqa: E731
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        assert identity(before) == identity(after) == identity(current), (
            "opaque gzip changed during validation",
            path,
        )
        return uncompressed_size
    finally:
        os.close(descriptor)


def semantic_payload_contract(
    root: Path, paths: list[Path]
) -> tuple[list[dict[str, str]], str]:
    """Return deterministic path/digest records and their canonical manifest digest."""
    records: list[dict[str, str]] = []
    observed: set[str] = set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        assert relative not in observed, ("duplicate semantic payload path", relative)
        assert relative not in {"", ".", ".."} and not relative.startswith("../"), relative
        assert "\\" not in relative and not any(ord(character) < 32 for character in relative), (
            "unsafe semantic payload path",
            relative,
        )
        observed.add(relative)
        records.append({"path": relative, "sha256": sha256_stable_file(path)})
    records.sort(key=lambda record: record["path"].encode("utf-8"))
    manifest = "".join(
        f"{record['sha256']}  ./{record['path']}\n" for record in records
    ).encode("utf-8")
    assert manifest, "semantic payload manifest may not be empty"
    return records, hashlib.sha256(manifest).hexdigest()


def assert_exact_ids(label: str, observed: set[str], expected: set[str]) -> None:
    if observed != expected:
        raise AssertionError(
            f"{label} ID mismatch: missing={sorted(expected - observed)[:10]}, "
            f"extra={sorted(observed - expected)[:10]}"
        )


def mmcif_tokens(text: str) -> list[str]:
    """Tokenize the STAR/mmCIF subset used by sequence and atom-site checks."""
    tokens: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(";"):
            block = [line[1:]]
            index += 1
            while index < len(lines) and not lines[index].startswith(";"):
                block.append(lines[index])
                index += 1
            assert index < len(lines), "unterminated mmCIF text block"
            tokens.append("\n".join(block))
            index += 1
            continue
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens.extend(list(lexer))
        index += 1
    return tokens


def parse_mmcif_records(
    tokens: list[str], path: Path
) -> tuple[dict[str, str], list[tuple[tuple[str, ...], list[dict[str, str]]]]]:
    """Parse scalar items and loops without silently discarding entity identity."""
    scalars: dict[str, str] = {}
    loops: list[tuple[tuple[str, ...], list[dict[str, str]]]] = []
    controls = {"loop_", "stop_", "global_"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if lowered == "loop_":
            index += 1
            headers: list[str] = []
            while index < len(tokens) and tokens[index].startswith("_"):
                headers.append(tokens[index])
                index += 1
            assert headers and len(headers) == len(set(headers)), (
                "invalid/duplicate mmCIF loop headers",
                path,
            )
            values: list[str] = []
            while index < len(tokens):
                current = tokens[index]
                current_lower = current.casefold()
                if (
                    current.startswith("_")
                    or current_lower in controls
                    or current_lower.startswith("data_")
                    or current_lower.startswith("save_")
                ):
                    break
                values.append(current)
                index += 1
            assert len(values) % len(headers) == 0, (
                "incomplete mmCIF loop row",
                path,
            )
            rows = [
                dict(zip(headers, values[offset : offset + len(headers)], strict=True))
                for offset in range(0, len(values), len(headers))
            ]
            assert rows, ("empty mmCIF loop", path, headers)
            loops.append((tuple(headers), rows))
            continue
        if token.startswith("_"):
            assert token not in scalars, ("duplicate mmCIF scalar", path, token)
            assert index + 1 < len(tokens), ("missing mmCIF scalar value", path, token)
            value = tokens[index + 1]
            value_lower = value.casefold()
            assert not (
                value.startswith("_")
                or value_lower in controls
                or value_lower.startswith("data_")
                or value_lower.startswith("save_")
            ), ("missing mmCIF scalar value", path, token)
            scalars[token] = value
            index += 2
            continue
        index += 1
    return scalars, loops


def normalized_protein_sequence(value: str, path: Path, label: str) -> str:
    sequence = re.sub(r"\s+", "", value).upper()
    assert SEQUENCE_RE.fullmatch(sequence), (label, path, value)
    return sequence


def assert_mmcif(path: Path) -> dict[str, object]:
    """Validate entity/chain-bound protein sequences and materialized atoms."""
    assert path.is_file() and not path.is_symlink(), ("missing or unsafe mmCIF", path)
    text = path.read_text(encoding="utf-8")
    assert text.strip(), ("empty mmCIF", path)
    meaningful = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert meaningful and meaningful[0].lower().startswith("data_"), (
        "mmCIF missing data_ block",
        path,
    )
    data_blocks: list[str] = []
    for line in meaningful:
        if line[0] in {"'", '"'}:
            continue
        directive = line.split(maxsplit=1)[0].casefold()
        if directive.startswith("data_"):
            data_blocks.append(directive)
        assert directive != "global_", ("mmCIF global_ scope is forbidden", path)
        assert not directive.startswith("save_"), (
            "mmCIF save frame is forbidden",
            path,
        )
    assert len(data_blocks) == 1, (
        "mmCIF must contain exactly one top-level data_ block",
        path,
        data_blocks,
    )
    scalars, loops = parse_mmcif_records(mmcif_tokens(text), path)
    sequence_keys = (
        "_entity_poly.pdbx_seq_one_letter_code_can",
        "_entity_poly.pdbx_seq_one_letter_code",
    )
    canonical_entity_key = "_entity_poly.entity_id"
    residue_keys = (
        "_entity_poly_seq.entity_id",
        "_entity_poly_seq.num",
        "_entity_poly_seq.mon_id",
    )
    coordinate_keys = {
        "_atom_site.group_PDB",
        "_atom_site.label_atom_id",
        "_atom_site.Cartn_x",
        "_atom_site.Cartn_y",
        "_atom_site.Cartn_z",
        "_atom_site.label_asym_id",
        "_atom_site.label_entity_id",
        "_atom_site.label_seq_id",
        "_atom_site.label_comp_id",
        "_atom_site.pdbx_PDB_model_num",
    }
    entity_sequences: dict[str, str] = {}
    residue_sequences: dict[str, dict[int, str]] = {}
    scalar_sequence_keys = [key for key in sequence_keys if key in scalars]
    if scalar_sequence_keys or canonical_entity_key in scalars:
        assert scalar_sequence_keys and canonical_entity_key in scalars, (
            "scalar polymer sequence lacks entity identity",
            path,
        )
        entity = scalars[canonical_entity_key]
        scalar_sequences = [
            normalized_protein_sequence(
                scalars[key], path, "invalid polymer sequence"
            )
            for key in scalar_sequence_keys
        ]
        assert len(set(scalar_sequences)) == 1, (
            "conflicting canonical/native entity sequence",
            path,
            entity,
        )
        entity_sequences[entity] = scalar_sequences[0]

    chain_entities: dict[str, str] = {}
    atom_coordinates: list[np.ndarray] = []
    materialized_entity_chains: dict[str, set[str]] = {}
    atom_rows: list[dict[str, str]] = []
    residue_atoms: dict[tuple[str, int], set[str]] = {}
    atom_identities: set[tuple[str, int, str]] = set()
    for headers, rows in loops:
        header_set = set(headers)
        if any(header.startswith("_entity_poly_seq.") for header in headers):
            assert set(residue_keys).issubset(header_set), (
                "incomplete entity_poly_seq loop",
                path,
            )
        loop_sequence_keys = [key for key in sequence_keys if key in header_set]
        if loop_sequence_keys:
            assert canonical_entity_key in header_set, (
                "polymer sequence loop lacks entity identity",
                path,
            )
            for row in rows:
                entity = row[canonical_entity_key]
                loop_sequences = [
                    normalized_protein_sequence(
                        row[key], path, "invalid polymer sequence"
                    )
                    for key in loop_sequence_keys
                ]
                assert len(set(loop_sequences)) == 1, (
                    "conflicting canonical/native entity sequence",
                    path,
                    entity,
                )
                sequence = loop_sequences[0]
                assert entity not in entity_sequences or entity_sequences[entity] == sequence, (
                    "conflicting polymer entity sequence",
                    path,
                    entity,
                )
                entity_sequences[entity] = sequence
        if set(residue_keys).issubset(header_set):
            for row in rows:
                entity = row[residue_keys[0]]
                number = int(row[residue_keys[1]])
                monomer = row[residue_keys[2]].upper()
                assert number > 0 and monomer in THREE_TO_ONE, ("invalid entity_poly_seq row", path)
                positions = residue_sequences.setdefault(entity, {})
                one_letter = THREE_TO_ONE[monomer]
                assert number not in positions, (
                    "duplicate entity_poly_seq position",
                    path,
                    entity,
                    number,
                )
                positions[number] = one_letter
        if {"_struct_asym.id", "_struct_asym.entity_id"}.issubset(header_set):
            for row in rows:
                chain = row["_struct_asym.id"]
                entity = row["_struct_asym.entity_id"]
                assert chain not in chain_entities, ("duplicate struct_asym chain", path, chain)
                chain_entities[chain] = entity
        if any(header.startswith("_atom_site.") for header in headers):
            assert coordinate_keys.issubset(header_set), (
                "atom_site loop lacks required identity/backbone fields",
                path,
                sorted(coordinate_keys - header_set),
            )
            for row in rows:
                model = row["_atom_site.pdbx_PDB_model_num"]
                assert model == "1", ("mmCIF contains an extra/non-primary model", path, model)
                coordinates = np.asarray(
                    [
                        float(row["_atom_site.Cartn_x"]),
                        float(row["_atom_site.Cartn_y"]),
                        float(row["_atom_site.Cartn_z"]),
                    ],
                    dtype=np.float64,
                )
                assert np.isfinite(coordinates).all(), ("non-finite mmCIF coordinates", path)
                atom_coordinates.append(coordinates)
                atom_rows.append(row)

    assert entity_sequences, ("mmCIF lacks a canonical polymer entity", path)
    for entity, expected_sequence in entity_sequences.items():
        assert entity in residue_sequences, (
            "canonical entity lacks entity_poly_seq",
            path,
            entity,
        )
        positions = residue_sequences[entity]
        expected_positions = list(range(1, len(expected_sequence) + 1))
        assert sorted(positions) == expected_positions, (
            "entity_poly_seq is not continuous and complete",
            path,
            entity,
            sorted(positions),
            expected_positions,
        )
        sequence = "".join(positions[number] for number in expected_positions)
        assert expected_sequence == sequence, (
            "canonical/entity_poly_seq mismatch",
            path,
            entity,
        )
    assert not (set(residue_sequences) - set(entity_sequences)), (
        "entity_poly_seq refers to a non-canonical entity",
        path,
        sorted(set(residue_sequences) - set(entity_sequences)),
    )
    canonical_chains = {
        chain: entity
        for chain, entity in chain_entities.items()
        if entity in entity_sequences
    }
    assert set(canonical_chains.values()) == set(entity_sequences), (
        "canonical entities and struct_asym chains disagree",
        path,
    )
    for row in atom_rows:
        chain = row["_atom_site.label_asym_id"]
        entity = row["_atom_site.label_entity_id"]
        assert chain in chain_entities, ("atom references unknown chain", path, chain)
        assert chain_entities[chain] == entity, (
            "atom chain/entity binding mismatch",
            path,
            chain,
            entity,
        )
        materialized_entity_chains.setdefault(entity, set()).add(chain)
        if entity not in entity_sequences:
            continue
        assert row["_atom_site.group_PDB"].upper() == "ATOM", (
            "canonical polymer atom must use group_PDB ATOM",
            path,
            chain,
            entity,
        )
        sequence_number = int(row["_atom_site.label_seq_id"])
        assert sequence_number > 0, (
            "canonical atom has invalid label_seq_id",
            path,
            chain,
            sequence_number,
        )
        monomer = row["_atom_site.label_comp_id"].upper()
        assert monomer in THREE_TO_ONE, (
            "non-canonical polymer atom residue",
            path,
            monomer,
        )
        assert residue_sequences[entity].get(sequence_number) == THREE_TO_ONE[monomer], (
            "atom residue/entity sequence mismatch",
            path,
            entity,
            sequence_number,
        )
        atom_name = row["_atom_site.label_atom_id"].upper()
        assert atom_name not in {"", ".", "?"}, (
            "canonical atom has no label_atom_id",
            path,
        )
        identity = (chain, sequence_number, atom_name)
        assert identity not in atom_identities, (
            "duplicate canonical atom identity",
            path,
            identity,
        )
        atom_identities.add(identity)
        residue_atoms.setdefault((chain, sequence_number), set()).add(atom_name)
    assert atom_coordinates, ("mmCIF has no materialized finite atom_site rows", path)
    for chain, entity in canonical_chains.items():
        expected_positions = set(range(1, len(entity_sequences[entity]) + 1))
        observed_positions = {
            sequence_number
            for observed_chain, sequence_number in residue_atoms
            if observed_chain == chain
        }
        assert observed_positions == expected_positions, (
            "canonical chain is truncated or has extra residues",
            path,
            chain,
            sorted(expected_positions - observed_positions),
            sorted(observed_positions - expected_positions),
        )
        for sequence_number in sorted(expected_positions):
            atoms = residue_atoms[(chain, sequence_number)]
            missing_backbone = {"N", "CA", "C"} - atoms
            assert not missing_backbone, (
                "canonical residue lacks a unique complete N/CA/C backbone",
                path,
                chain,
                sequence_number,
                sorted(missing_backbone),
            )
    return {
        "entity_sequences": entity_sequences,
        "chain_entities": chain_entities,
        "materialized_entity_chains": materialized_entity_chains,
        "atom_coordinates": np.stack(atom_coordinates),
        "atom_rows": atom_rows,
    }


def assert_designed_entity_binding(
    evidence: dict[str, object], expected_sequence: str, path: Path
) -> tuple[str, tuple[str, ...]]:
    entity_sequences = evidence["entity_sequences"]
    assert isinstance(entity_sequences, dict)
    matching = [
        str(entity)
        for entity, sequence in entity_sequences.items()
        if sequence == expected_sequence
    ]
    assert len(matching) == 1, (
        "designed canonical sequence does not identify exactly one entity",
        path,
        expected_sequence,
        matching,
    )
    entity = matching[0]
    materialized = evidence["materialized_entity_chains"]
    assert isinstance(materialized, dict)
    chains = tuple(sorted(str(value) for value in materialized.get(entity, set())))
    assert len(chains) == 1, (
        "designed entity must bind exactly one materialized chain",
        path,
        entity,
        chains,
    )
    return entity, chains


def load_yaml_mapping(path: Path) -> dict[str, object]:
    assert path.is_file() and not path.is_symlink(), ("missing or unsafe resolved config", path)
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    assert isinstance(payload, dict), ("resolved config is not a mapping", path)
    return payload


def assert_design_metadata_npz(path: Path) -> int:
    """Validate the frozen v0.3.2 DesignWriter token-metadata schema."""
    assert path.is_file() and not path.is_symlink(), ("missing or unsafe NPZ", path)
    with np.load(path, allow_pickle=False) as arrays:
        assert arrays.files and len(arrays.files) == len(set(arrays.files)), (
            "empty or duplicate-key NPZ",
            path,
        )
        observed = set(arrays.files)
        assert DESIGN_METADATA_REQUIRED.issubset(observed), (
            "missing Writer metadata keys",
            path,
            sorted(DESIGN_METADATA_REQUIRED - observed),
        )
        assert observed.issubset(DESIGN_METADATA_REQUIRED | DESIGN_METADATA_OPTIONAL), (
            "unexpected Writer metadata keys",
            path,
            sorted(observed - DESIGN_METADATA_REQUIRED - DESIGN_METADATA_OPTIONAL),
        )
        materialized = {name: np.asarray(arrays[name]) for name in arrays.files}

    token_count = int(materialized["design_mask"].shape[0])
    assert token_count > 0, ("empty Writer metadata token axis", path)
    for key in DESIGN_METADATA_REQUIRED:
        values = materialized[key]
        assert values.shape == (token_count,), (
            "Writer metadata shape mismatch",
            path,
            key,
            values.shape,
        )
        assert np.issubdtype(values.dtype, np.number) or values.dtype == np.bool_, (
            "Writer metadata dtype is not numeric/bool",
            path,
            key,
            values.dtype,
        )
        assert np.isfinite(values).all(), ("non-finite Writer metadata", path, key)

    for key in ("design_mask", "token_resolved_mask"):
        assert np.isin(materialized[key], [0, 1]).all(), (
            "non-binary Writer metadata mask",
            path,
            key,
        )
    for key, allowed in (
        ("mol_type", [0, 1, 2, 3]),
        ("ss_type", [0, 1, 2, 3]),
        ("binding_type", [0, 1, 2]),
    ):
        values = materialized[key]
        assert np.equal(values, np.floor(values)).all() and np.isin(values, allowed).all(), (
            "Writer metadata enum out of range",
            path,
            key,
        )
    if "inverse_fold_design_mask" in materialized:
        mask = materialized["inverse_fold_design_mask"]
        assert mask.shape == (token_count,), (
            "inverse_fold_design_mask shape mismatch",
            path,
            mask.shape,
        )
        assert (np.issubdtype(mask.dtype, np.number) or mask.dtype == np.bool_) and np.isfinite(
            mask
        ).all(), ("invalid inverse_fold_design_mask dtype/value", path, mask.dtype)
        assert np.isin(mask, [0, 1]).all(), (
            "non-binary inverse_fold_design_mask",
            path,
        )
    if "aa_constraint_mask" in materialized:
        mask = materialized["aa_constraint_mask"]
        assert mask.shape == (token_count, 20), (
            "aa_constraint_mask shape mismatch",
            path,
            mask.shape,
        )
        assert np.issubdtype(mask.dtype, np.number) and np.isfinite(mask).all(), (
            "invalid aa_constraint_mask dtype/value",
            path,
            mask.dtype,
        )
        assert np.isin(mask, [0, 1]).all(), ("non-binary aa_constraint_mask", path)
    return token_count


def validate(root_argument: str) -> dict[str, object]:
    requested_root = Path(root_argument).expanduser()
    assert requested_root.is_absolute(), (
        "OUTPUT_PATH must be an absolute canonical path",
        requested_root,
    )
    assert not requested_root.is_symlink(), (
        "OUTPUT_PATH must not be a symlink",
        requested_root,
    )
    root = requested_root.resolve(strict=True)
    assert requested_root == root, (
        "OUTPUT_PATH must not traverse symlinks or use a noncanonical spelling",
        requested_root,
        root,
    )
    assert root.is_dir(), ("OUTPUT_PATH is not a directory", root)

    try:
        expected_n = int(os.environ["EXPECTED_DESIGNS"])
        expected_fold_samples = int(os.environ.get("EXPECTED_FOLD_SAMPLES", "5"))
    except (KeyError, ValueError) as exc:
        raise AssertionError("EXPECTED_DESIGNS and EXPECTED_FOLD_SAMPLES must be integers") from exc
    assert expected_n > 0, ("EXPECTED_DESIGNS must be positive", expected_n)
    assert expected_fold_samples == 5, (
        "the frozen folding contract requires exactly five samples",
        expected_fold_samples,
    )

    design_dir = root / "intermediate_designs"
    inverse_dir = root / "intermediate_designs_inverse_folded"
    config_dir = root / "config"

    design_config = load_yaml_mapping(config_dir / "design.yaml")
    inverse_config = load_yaml_mapping(config_dir / "inverse_folding.yaml")
    folding_config = load_yaml_mapping(config_dir / "folding.yaml")
    filtering_config = load_yaml_mapping(config_dir / "filtering.yaml")

    design_total = int(design_config["data"]["cfg"]["multiplicity"]) * int(
        design_config["diffusion_samples"]
    )
    assert design_total == expected_n, ("resolved design total", design_total, expected_n)
    assert int(inverse_config["data"]["cfg"]["multiplicity"]) == 1
    assert int(folding_config["diffusion_samples"]) == expected_fold_samples

    design_cif = top_level(design_dir, ".cif")
    design_npz = top_level(design_dir, ".npz")
    inverse_cif = top_level(inverse_dir, ".cif")
    inverse_npz = top_level(inverse_dir, ".npz")
    fold_npz = top_level(inverse_dir / "fold_out_npz", ".npz")
    refold_cif = top_level(inverse_dir / "refold_cif", ".cif")

    stage_files = {
        "raw design CIF": design_cif,
        "raw design NPZ": design_npz,
        "inverse-folded CIF": inverse_cif,
        "inverse-folded NPZ": inverse_npz,
        "fold metadata NPZ": fold_npz,
        "refold CIF": refold_cif,
    }
    for label, paths in stage_files.items():
        assert len(paths) == expected_n, (label, len(paths), expected_n)
    mmcif_evidence = {
        path: assert_mmcif(path) for path in (*design_cif, *inverse_cif, *refold_cif)
    }
    metadata_token_counts = {
        path: assert_design_metadata_npz(path) for path in (*design_npz, *inverse_npz)
    }

    csv_path = inverse_dir / "aggregate_metrics_analyze.csv"
    assert csv_path.is_file() and not csv_path.is_symlink(), "missing analysis manifest"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames, "analysis manifest has no header"
        assert len(reader.fieldnames) == len(set(reader.fieldnames)), "duplicate analysis columns"
        rows = list(reader)
    assert len(rows) == expected_n, ("analysis rows", len(rows), expected_n)
    analysis = pd.DataFrame(rows)

    missing_numeric = set(REQUIRED_ANALYSIS_NUMERIC) - set(analysis.columns)
    assert not missing_numeric, ("missing required analysis metrics", missing_numeric)
    for column in REQUIRED_ANALYSIS_NUMERIC:
        values = pd.to_numeric(analysis[column], errors="coerce").to_numpy(dtype=float)
        assert values.shape == (expected_n,), ("bad analysis metric shape", column, values.shape)
        assert np.isfinite(values).all(), ("non-finite analysis metric", column)
        analysis[column] = values

    assert "designed_chain_sequence" in analysis.columns, "missing designed_chain_sequence"
    sequences = analysis["designed_chain_sequence"].astype(str).str.strip().str.upper()
    assert sequences.str.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+").all(), (
        "invalid designed_chain_sequence"
    )

    ids = [row.get("id", "").strip() for row in rows]
    file_names = [row.get("file_name", "").strip() for row in rows]
    assert all(ids) and len(set(ids)) == expected_n, "analysis id is missing or duplicated"
    assert all(file_names) and len(set(file_names)) == expected_n, (
        "analysis file_name is missing or duplicated"
    )
    for candidate_id, file_name in zip(ids, file_names, strict=True):
        assert (
            candidate_id not in {".", ".."}
            and Path(candidate_id).name == candidate_id
            and "\\" not in candidate_id
        ), (
            "unsafe candidate id",
            candidate_id,
        )
        assert file_name == f"{candidate_id}.cif", (
            "analysis id/file_name mismatch",
            candidate_id,
            file_name,
        )
    authoritative_ids = set(ids)

    assert_exact_ids("raw design CIF", {path.stem for path in design_cif}, authoritative_ids)
    assert_exact_ids("raw design NPZ", {path.stem for path in design_npz}, authoritative_ids)
    assert_exact_ids("inverse CIF", {path.stem for path in inverse_cif}, authoritative_ids)
    assert_exact_ids("inverse NPZ", {path.stem for path in inverse_npz}, authoritative_ids)
    assert set(file_names) == {path.name for path in inverse_cif}
    assert set(file_names) == {path.name for path in refold_cif}
    assert {f"{candidate_id}.npz" for candidate_id in authoritative_ids} == {
        path.name for path in fold_npz
    }

    filtered_path = root / "final_ranked_designs" / "all_designs_metrics.csv"
    assert filtered_path.is_file() and not filtered_path.is_symlink(), (
        "missing filtering output table"
    )
    filtered = pd.read_csv(
        filtered_path,
        dtype={"id": "string", "designed_chain_sequence": "string"},
    )
    required_filter_columns = {"id", "designed_chain_sequence", "pass_filters"}
    assert required_filter_columns.issubset(filtered.columns), (
        "missing filtering columns",
        required_filter_columns - set(filtered.columns),
    )
    assert 0 < len(filtered) <= expected_n
    assert filtered["id"].notna().all() and filtered["id"].astype(str).is_unique
    filtered_ids = set(filtered["id"].astype(str))
    assert filtered_ids.issubset(authoritative_ids)
    filter_boolean = filtered["pass_filters"].astype(str).str.strip().str.lower()
    assert filter_boolean.isin({"true", "false", "1", "0"}).all()
    authoritative_sequence = dict(zip(ids, sequences, strict=True))
    for candidate_id, expected_sequence in authoritative_sequence.items():
        inverse_path = inverse_dir / f"{candidate_id}.cif"
        refold_path = inverse_dir / "refold_cif" / f"{candidate_id}.cif"
        inverse_binding = assert_designed_entity_binding(
            mmcif_evidence[inverse_path], expected_sequence, inverse_path
        )
        refold_binding = assert_designed_entity_binding(
            mmcif_evidence[refold_path], expected_sequence, refold_path
        )
        assert inverse_binding == refold_binding, (
            "inverse/refold designed entity-chain binding drift",
            candidate_id,
            inverse_binding,
            refold_binding,
        )
        assert metadata_token_counts[design_dir / f"{candidate_id}.npz"] == (
            metadata_token_counts[inverse_dir / f"{candidate_id}.npz"]
        ), ("design/inverse Writer metadata token-count drift", candidate_id)
    for row in filtered.itertuples(index=False):
        assert str(row.designed_chain_sequence).strip().upper() == authoritative_sequence[str(row.id)]

    filter_budget = int(filtering_config["budget"])
    assert filter_budget >= 0, ("negative filtering budget", filter_budget)
    final_path = root / "final_ranked_designs" / f"final_designs_metrics_{filter_budget}.csv"
    assert final_path.is_file() and not final_path.is_symlink(), (
        "missing final filtering ranking table"
    )
    final = pd.read_csv(final_path, dtype={"id": "string"})
    assert 0 <= len(final) <= filter_budget
    required_final_columns = {"id", "designed_chain_sequence", "final_rank", "quality_score"}
    assert required_final_columns.issubset(final.columns)
    assert final["id"].notna().all() and final["id"].astype(str).is_unique
    assert set(final["id"].astype(str)).issubset(filtered_ids)
    final_observed = final["designed_chain_sequence"].astype(str).str.strip().str.upper()
    final_expected = (
        final["id"].astype(str).map(authoritative_sequence).astype(str).str.strip().str.upper()
    )
    assert final_observed.equals(final_expected)
    try:
        final_ranks = pd.to_numeric(
            final["final_rank"], errors="raise"
        ).to_numpy(dtype=float)
    except (TypeError, ValueError) as error:
        raise AssertionError(
            ("invalid final filtering metric", "final_rank")
        ) from error
    assert np.isfinite(final_ranks).all(), (
        "non-finite final filtering metric",
        "final_rank",
    )
    try:
        quality_scores = pd.to_numeric(
            final["quality_score"], errors="raise"
        ).to_numpy(dtype=float)
    except (TypeError, ValueError) as error:
        raise AssertionError(
            ("invalid final filtering metric", "quality_score")
        ) from error
    if not np.isfinite(quality_scores).all():
        native_singleton_nan = (
            expected_n == 1
            and filter_budget == 1
            and len(filtered) == 1
            and len(final) == 1
            and final_ranks[0] == 1.0
            and np.isnan(quality_scores[0])
        )
        assert native_singleton_nan, (
            "non-finite final filtering metric",
            "quality_score",
        )

    sequence_path = inverse_dir / "ca_coords_sequences.pkl.gz"
    opaque_uncompressed_bytes = assert_opaque_single_gzip(sequence_path)

    analysis_by_id = analysis.assign(id=analysis["id"].astype(str)).set_index("id")
    writer_coordinate_closures = 0
    for path in fold_npz:
        with np.load(path, allow_pickle=False) as arrays:
            assert len(arrays.files) == len(set(arrays.files)), ("duplicate-key NPZ", path)
            assert "coords" in arrays.files, (path, "coords")
            coords = np.asarray(arrays["coords"])
            assert (
                coords.ndim == 3
                and coords.shape[0] == expected_fold_samples
                and coords.shape[2] == 3
            ), (path, "coords", coords.shape)
            assert coords.shape[1] > 0, (path, "empty atom axis")
            assert np.issubdtype(coords.dtype, np.number), (path, "coords", coords.dtype)
            assert np.isfinite(coords).all(), ("non-finite fold coordinates", path)

            for key in PER_SAMPLE_KEYS:
                assert key in arrays.files, (path, key)
                values = np.asarray(arrays[key])
                assert values.shape == (expected_fold_samples,), (
                    path,
                    key,
                    values.shape,
                    expected_fold_samples,
                )
                assert np.issubdtype(values.dtype, np.number), (path, key, values.dtype)
                assert np.isfinite(values).all(), ("non-finite fold array", path, key)

            atom_count = coords.shape[1]
            for key in MAPPING_AND_FEATURE_KEYS:
                assert key in arrays.files, (path, key)
            assert arrays["atom_resolved_mask"].shape == (1, atom_count)
            assert arrays["backbone_mask"].shape == (1, atom_count)
            assert arrays["atom_to_token"].ndim == 3
            assert arrays["atom_to_token"].shape[:2] == (1, atom_count)
            token_count = arrays["atom_to_token"].shape[2]
            assert token_count > 0, (path, "empty token axis")
            assert arrays["token_index"].shape == (1, token_count)
            assert arrays["mol_type"].shape == (1, token_count)
            assert arrays["res_type"].shape[:2] == (1, token_count)
            assert arrays["input_coords"].shape == (1, 1, atom_count, 3)

            for key in MAPPING_AND_FEATURE_KEYS:
                values = np.asarray(arrays[key])
                assert np.issubdtype(values.dtype, np.number) or values.dtype == np.bool_, (
                    path,
                    key,
                    values.dtype,
                )
                assert np.isfinite(values).all(), (
                    "non-finite mapping/feature array",
                    path,
                    key,
                )
            for key in ("atom_resolved_mask", "atom_to_token", "backbone_mask", "res_type"):
                values = np.asarray(arrays[key])
                assert np.isin(values, [0, 1]).all(), (
                    "non-binary mask/one-hot",
                    path,
                    key,
                )

            token_index = np.asarray(arrays["token_index"])[0]
            assert np.equal(token_index, np.floor(token_index)).all(), (
                "fractional token_index",
                path,
            )
            assert np.array_equal(token_index.astype(np.int64), np.arange(token_count)), (
                "token_index must be contiguous 0..T-1",
                path,
            )
            mol_type = np.asarray(arrays["mol_type"])[0]
            assert np.equal(mol_type, np.floor(mol_type)).all(), (
                "fractional mol_type",
                path,
            )
            assert np.isin(mol_type, [0, 1, 2, 3]).all(), (
                "mol_type out of frozen range",
                path,
            )
            res_type = np.asarray(arrays["res_type"])[0]
            assert (res_type.sum(axis=-1) == 1).all(), ("res_type is not one-hot", path)
            atom_to_token = np.asarray(arrays["atom_to_token"])[0].astype(bool)
            atom_token_counts = atom_to_token.sum(axis=1)
            assert np.isin(atom_token_counts, [0, 1]).all(), ("bad atom_to_token", path)
            resolved = np.asarray(arrays["atom_resolved_mask"])[0].astype(bool)
            assert (atom_token_counts[resolved] == 1).all(), (
                "unmapped resolved atom",
                path,
            )

            analysis_index = int(
                np.argmax(
                    0.8 * arrays["design_to_target_iptm"]
                    + 0.2 * arrays["design_ptm"]
                )
            )
            writer_index = int(np.argmax(0.8 * arrays["iptm"] + 0.2 * arrays["ptm"]))
            candidate_id = path.stem
            row = analysis_by_id.loc[candidate_id]
            for column, key in (
                ("design_to_target_iptm", "design_to_target_iptm"),
                ("design_ptm", "design_ptm"),
                ("min_design_to_target_pae", "min_design_to_target_pae"),
            ):
                observed = float(pd.to_numeric(row[column], errors="raise"))
                expected = float(arrays[key][analysis_index])
                assert np.isclose(observed, expected, rtol=0.0, atol=5.1e-6), (
                    "aggregate/NPZ selected-sample mismatch",
                    path,
                    column,
                    observed,
                    expected,
                )
            serialized_atom_mask = resolved & (atom_token_counts == 1)
            expected_writer_coordinates = coords[writer_index][serialized_atom_mask]
            refold_path = inverse_dir / "refold_cif" / f"{candidate_id}.cif"
            observed_writer_coordinates = np.asarray(
                mmcif_evidence[refold_path]["atom_coordinates"], dtype=np.float64
            )
            assert observed_writer_coordinates.shape == expected_writer_coordinates.shape, (
                "Writer-selected refold coordinate count mismatch",
                path,
                observed_writer_coordinates.shape,
                expected_writer_coordinates.shape,
            )
            assert np.allclose(
                observed_writer_coordinates,
                expected_writer_coordinates,
                rtol=0.0,
                atol=WRITER_COORDINATE_ATOL,
            ), ("refold CIF is not the Writer-selected NPZ sample", path, writer_index)
            writer_coordinate_closures += 1
            assert 0 <= analysis_index < expected_fold_samples
            assert 0 <= writer_index < expected_fold_samples

    semantic_paths = [
        config_dir / "design.yaml",
        config_dir / "inverse_folding.yaml",
        config_dir / "folding.yaml",
        config_dir / "filtering.yaml",
        *design_cif,
        *design_npz,
        *inverse_cif,
        *inverse_npz,
        *fold_npz,
        *refold_cif,
        csv_path,
        filtered_path,
        final_path,
        sequence_path,
    ]
    semantic_files, semantic_manifest_sha256 = semantic_payload_contract(
        root, semantic_paths
    )
    return {
        "schema_version": VALIDATION_SCHEMA,
        "status": "PASS",
        "output": str(root),
        "validator_sha256": sha256_stable_file(Path(__file__).resolve(strict=True)),
        "semantic_payload_files": semantic_files,
        "semantic_payload_file_count": len(semantic_files),
        "semantic_payload_manifest_sha256": semantic_manifest_sha256,
        "pickle_deserialization_performed": False,
        "opaque_artifact_validation": OPAQUE_ARTIFACT_VALIDATION,
        "opaque_artifact_semantic_source": (
            "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv"
        ),
        "opaque_artifact_uncompressed_bytes": opaque_uncompressed_bytes,
        "expected_designs": expected_n,
        "observed_unique_ids": len(authoritative_ids),
        "fold_samples_per_candidate": expected_fold_samples,
        "resolved_design_multiplicity": int(design_config["data"]["cfg"]["multiplicity"]),
        "resolved_design_diffusion_samples": int(design_config["diffusion_samples"]),
        "resolved_inverse_fold_multiplicity": int(
            inverse_config["data"]["cfg"]["multiplicity"]
        ),
        "filter_rows_after_cdr_dedup": len(filtered),
        "filter_final_rows": len(final),
        "filter_budget": filter_budget,
        "writer_coordinate_closure_count": writer_coordinate_closures,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: validate_cell_output.py OUTPUT_PATH", file=sys.stderr)
        return 64
    try:
        summary = validate(arguments[0])
    except Exception as exc:  # noqa: BLE001 - emit one stable CLI failure path
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
