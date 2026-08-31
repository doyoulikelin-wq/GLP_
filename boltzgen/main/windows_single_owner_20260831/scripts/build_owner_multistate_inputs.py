#!/usr/bin/env python3
"""Stage verified owner-mode multi-state folding inputs without running a GPU job."""

from __future__ import annotations

import argparse
import csv
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Sequence

import gemmi
import numpy as np
import yaml
from Bio.Align import PairwiseAligner


DEFAULT_CANDIDATES = ("design_1", "design_3")
DEFAULT_STATES = ("DEV_00", "DEV_01", "DEV_05", "DEV_06", "DEV_15")
STATE_CATALOG_RELATIVE = Path(
    "boltzgen/resources/data/AIV1技术门合同_20260828/development_state_contract.tsv"
)
FOLDING_CONFIG_RELATIVE = Path("source_evidence/resolved_configs/folding.yaml")
METADATA_KEYS = {
    "design_mask",
    "mol_type",
    "ss_type",
    "token_resolved_mask",
    "binding_type",
}
MANIFEST_ROW = re.compile(r"([0-9a-f]{64})  (?:\./)?([^\x00\r\n]+)")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
BOLTZGEN_TARGET_ID_REGEX = re.compile(
    r"^(?:(?:sample\d+_|batch\d+_|rank\d+_)+)?"
    r"([^_]+)(?:_[^_]+)*?(?:_(?:gen))*$"
)
SCHEMA_VERSION = "WINDOWS_OWNER_MULTISTATE_INPUTS_V1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build verified T10 multi-state folding inputs from a T9 anchor set."
    )
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--anchor-set", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--candidate-ids", nargs="+", default=list(DEFAULT_CANDIDATES)
    )
    parser.add_argument("--state-ids", nargs="+", default=list(DEFAULT_STATES))
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--minimum-aligned-ca", type=int, default=8)
    return parser.parse_args()


def stable_digest(path: Path) -> str:
    """Hash one immutable regular file while detecting concurrent replacement/change."""
    if path.is_symlink():
        raise ValueError(f"symlink is not allowed for hashed file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"regular file required: {path}")
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity:
        raise ValueError(f"file changed while hashing: {path}")
    return digest.hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    """Create a file once; never replace an existing path."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, payload: dict) -> None:
    atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def write_text(path: Path, value: str) -> None:
    atomic_write(path, value.encode("utf-8"))


def parse_manifest(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = MANIFEST_ROW.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid manifest row in {path}: {line!r}")
        relative = Path(match.group(2))
        key = relative.as_posix()
        if relative.is_absolute() or ".." in relative.parts or key in rows:
            raise ValueError(f"unsafe or duplicate manifest path: {relative}")
        rows[key] = match.group(1)
    if not rows:
        raise ValueError(f"empty manifest: {path}")
    return rows


def regular_file_closure(root: Path, excluded: set[str]) -> set[str]:
    observed: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"unsafe tree member: {relative}")
        observed.add(relative)
    return observed


def verify_manifest(root: Path, manifest: Path, excluded: set[str]) -> dict[str, str]:
    rows = parse_manifest(manifest)
    observed = regular_file_closure(root, excluded)
    if set(rows) != observed:
        raise ValueError(
            f"manifest closure mismatch: missing={sorted(observed - set(rows))} "
            f"unexpected={sorted(set(rows) - observed)}"
        )
    for relative, expected in rows.items():
        if stable_digest(root / relative) != expected:
            raise ValueError(f"manifest digest mismatch: {relative}")
    return rows


def seal_input_snapshot(root: Path) -> None:
    """Seal the initial input tree; later folding outputs may add new files."""
    manifest = root / "INPUT_SHA256SUMS"
    if manifest.exists() or manifest.is_symlink():
        raise ValueError(f"input manifest already exists: {manifest}")
    # STATUS is terminal bookkeeping and may be rewritten by the GPU runner.
    # Folding output directories do not exist yet and are intentionally outside
    # this immutable, row-addressed input snapshot.
    observed = regular_file_closure(root, {"INPUT_SHA256SUMS", "STATUS.txt"})
    records = sorted(
        ((relative, stable_digest(root / relative)) for relative in observed),
        key=lambda item: item[0].encode("utf-8"),
    )
    content = "".join(
        f"{digest}  ./{relative}\n" for relative, digest in records
    ).encode("utf-8")
    atomic_write(manifest, content)
    rows = parse_manifest(manifest)
    if set(rows) != observed:
        raise ValueError("input tree changed while sealing")
    for relative, expected in rows.items():
        if stable_digest(root / relative) != expected:
            raise ValueError(f"input changed while sealing: {relative}")


def publish_directory_no_replace(staging: Path, destination: Path) -> None:
    """Atomically publish a directory with Linux renameat2(RENAME_NOREPLACE)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(staging),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def validate_requested_ids(values: Sequence[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result or len(set(result)) != len(result):
        raise ValueError(f"{label} must be nonempty and unique")
    invalid = [value for value in result if SAFE_ID.fullmatch(value) is None]
    if invalid:
        raise ValueError(f"unsafe {label}: {invalid}")
    return result


def make_task_id(candidate_id: str, state_id: str) -> str:
    # The owner validator deliberately uses a lowercase filesystem-safe ID
    # contract even though catalog state IDs are uppercase.
    task_id = f"{candidate_id}_{state_id}".lower()
    if "__" in task_id or BOLTZGEN_TARGET_ID_REGEX.fullmatch(task_id) is None:
        raise ValueError(f"task ID is incompatible with BoltzGen: {task_id}")
    return task_id


def resolve_catalog_source(repo_root: Path, relative_value: str) -> tuple[Path, Path]:
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"unsafe state relative_path: {relative_value!r}")
    candidates = (repo_root / relative, repo_root.parent / relative)
    existing = [candidate for candidate in candidates if candidate.exists()]
    if len(existing) != 1:
        raise ValueError(
            f"state path must resolve through exactly one project root: {relative_value!r}; "
            f"found={existing}"
        )
    alias = existing[0].absolute()
    resolved = alias.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"state source must resolve to a regular file: {alias}")
    return alias, resolved


def load_selected_states(
    repo_root: Path, state_ids: Sequence[str]
) -> tuple[list[dict[str, str]], Path, str]:
    """Select, policy-check, path-resolve, and hash-check catalog states."""
    requested = validate_requested_ids(state_ids, "state IDs")
    catalog = (repo_root / STATE_CATALOG_RELATIVE).resolve(strict=True)
    if catalog.is_symlink() or not catalog.is_file():
        raise ValueError(f"unsafe state catalog: {catalog}")
    with catalog.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    required_columns = {
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
    }
    if not rows or not required_columns.issubset(rows[0]):
        raise ValueError(
            f"state catalog missing columns: {sorted(required_columns - set(rows[0] if rows else {}))}"
        )
    by_id: dict[str, dict[str, str]] = {}
    orders: set[int] = set()
    for raw in rows:
        state_id = raw["target_state_id"]
        try:
            order = int(raw["state_order"])
        except ValueError as exc:
            raise ValueError(f"invalid state_order for {state_id}") from exc
        if state_id in by_id or order in orders:
            raise ValueError("duplicate state ID or state_order in catalog")
        by_id[state_id] = raw
        orders.add(order)
    missing = [state_id for state_id in requested if state_id not in by_id]
    if missing:
        raise ValueError(f"requested states are absent from catalog: {missing}")

    selected: list[dict[str, str]] = []
    for state_id in requested:
        row = dict(by_id[state_id])
        if row["required_active_for_ai"].lower() != "true":
            raise ValueError(f"state is not active for AI: {state_id}")
        if row["required_parse_status"] != "PASS":
            raise ValueError(f"state parse status is not PASS: {state_id}")
        if row["required_geometry_complete"].lower() != "true":
            raise ValueError(f"state geometry is incomplete: {state_id}")
        if not row["required_status"]:
            raise ValueError(f"state required_status is empty: {state_id}")
        expected = row["sha256"]
        if HEX_SHA256.fullmatch(expected) is None:
            raise ValueError(f"invalid state sha256 in catalog: {state_id}")
        alias, resolved = resolve_catalog_source(repo_root, row["relative_path"])
        actual = stable_digest(resolved)
        if actual != expected:
            raise ValueError(
                f"state source digest mismatch: {state_id} expected={expected} actual={actual}"
            )
        row["source_alias_path"] = str(alias)
        row["source_resolved_path"] = str(resolved)
        selected.append(row)
    return selected, catalog, stable_digest(catalog)


def load_anchor_selection(
    anchor_root: Path, candidate_ids: Sequence[str]
) -> tuple[dict, list[dict], dict[str, str]]:
    """Verify the complete T9 snapshot and return requested anchors in CLI order."""
    requested = validate_requested_ids(candidate_ids, "candidate IDs")
    manifest = anchor_root / "SHA256SUMS"
    rows = verify_manifest(anchor_root, manifest, {"SHA256SUMS"})
    payload_path = anchor_root / "ANCHOR_SET.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "WINDOWS_OWNER_LOCAL_ANCHOR_SET_V1"
        or payload.get("status") != "LOCAL_ANCHOR_SET_READY"
        or payload.get("training_performed") is not False
        or payload.get("selection", {}).get("scope") != "DEVELOPMENT_ONLY"
    ):
        raise ValueError("anchor set is not a ready development-only T9 snapshot")
    anchors = payload.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("anchor set contains no anchors")
    by_id: dict[str, dict] = {}
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError("invalid anchor row")
        candidate_id = anchor.get("candidate_id")
        if not isinstance(candidate_id, str) or SAFE_ID.fullmatch(candidate_id) is None:
            raise ValueError(f"invalid anchor candidate ID: {candidate_id!r}")
        if candidate_id in by_id:
            raise ValueError(f"duplicate anchor candidate ID: {candidate_id}")
        rank = anchor.get("final_rank")
        if not isinstance(rank, int) or rank < 1:
            raise ValueError(f"invalid final rank for {candidate_id}")
        by_id[candidate_id] = anchor
    missing = [candidate_id for candidate_id in requested if candidate_id not in by_id]
    if missing:
        raise ValueError(f"requested candidates are absent from T9: {missing}")

    selected: list[dict] = []
    for candidate_id in requested:
        anchor = by_id[candidate_id]
        rank = anchor["final_rank"]
        base = f"anchors/rank{rank:02d}_{candidate_id}"
        files = anchor.get("files")
        if not isinstance(files, dict):
            raise ValueError(f"missing anchor file evidence: {candidate_id}")
        for logical_name in ("inverse_folded.cif", "inverse_metadata.npz"):
            relative = f"{base}/{logical_name}"
            declared = files.get(logical_name, {}).get("sha256")
            if rows.get(relative) is None or declared != rows[relative]:
                raise ValueError(
                    f"anchor JSON/manifest binding mismatch: {candidate_id}:{logical_name}"
                )
        item = dict(anchor)
        item["anchor_relative_root"] = base
        selected.append(item)
    return payload, selected, rows


def load_structure(path: Path) -> gemmi.Structure:
    try:
        document = gemmi.cif.read(str(path))
        structure = gemmi.make_structure_from_block(document.sole_block())
    except Exception as exc:
        raise ValueError(f"cannot parse mmCIF: {path}: {exc}") from exc
    if len(structure) != 1:
        raise ValueError(f"exactly one model is required: {path}")
    return structure


def protein_residues(chain: gemmi.Chain) -> list[gemmi.Residue]:
    residues = [
        residue
        for residue in chain
        if residue.entity_type == gemmi.EntityType.Polymer
    ]
    if not residues or len(residues) != len(chain):
        raise ValueError(f"chain {chain.name!r} must contain only polymer residues")
    return residues


def residue_one_letter(residue: gemmi.Residue) -> str:
    code = gemmi.find_tabulated_residue(residue.name).one_letter_code
    if not code or len(code) != 1 or not code.isalpha():
        raise ValueError(f"unsupported protein residue: {residue.name}")
    return code.upper()


def chain_sequence(residues: Sequence[gemmi.Residue]) -> str:
    return "".join(residue_one_letter(residue) for residue in residues)


def residue_ca(residue: gemmi.Residue) -> np.ndarray | None:
    atoms = [atom for atom in residue if atom.name.strip() == "CA"]
    if not atoms:
        return None
    preferred = [atom for atom in atoms if str(atom.altloc) in {"\x00", " ", "A"}]
    atom = preferred[0] if preferred else atoms[0]
    return np.asarray([atom.pos.x, atom.pos.y, atom.pos.z], dtype=np.float64)


def global_sequence_pairs(moving_sequence: str, fixed_sequence: str) -> list[tuple[int, int]]:
    """Return deterministic residue-index pairs from a scored global alignment."""
    aligner = PairwiseAligner()
    aligner.mode = "global"
    aligner.match_score = 2.0
    aligner.mismatch_score = -1.0
    aligner.open_gap_score = -2.0
    aligner.extend_gap_score = -0.5
    alignments = aligner.align(moving_sequence, fixed_sequence)
    if len(alignments) == 0:
        raise ValueError("global sequence alignment produced no result")
    coordinates = np.asarray(alignments[0].coordinates, dtype=np.int64)
    pairs: list[tuple[int, int]] = []
    for index in range(coordinates.shape[1] - 1):
        moving_start, moving_end = coordinates[0, index : index + 2]
        fixed_start, fixed_end = coordinates[1, index : index + 2]
        moving_step = int(moving_end - moving_start)
        fixed_step = int(fixed_end - fixed_start)
        if moving_step and fixed_step:
            if moving_step != fixed_step:
                raise ValueError("unexpected unequal diagonal alignment segment")
            pairs.extend(
                (int(moving_start + offset), int(fixed_start + offset))
                for offset in range(moving_step)
            )
    return pairs


def kabsch_transform(
    moving: np.ndarray, fixed: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    """Return proper rotation/translation mapping moving points onto fixed points."""
    moving = np.asarray(moving, dtype=np.float64)
    fixed = np.asarray(fixed, dtype=np.float64)
    if moving.shape != fixed.shape or moving.ndim != 2 or moving.shape[1] != 3:
        raise ValueError("Kabsch inputs must have identical (N, 3) shapes")
    if moving.shape[0] < 3:
        raise ValueError("Kabsch alignment requires at least three point pairs")
    if not np.isfinite(moving).all() or not np.isfinite(fixed).all():
        raise ValueError("Kabsch inputs must be finite")
    moving_center = moving.mean(axis=0)
    fixed_center = fixed.mean(axis=0)
    moving_centered = moving - moving_center
    fixed_centered = fixed - fixed_center
    if np.linalg.matrix_rank(moving_centered) < 2 or np.linalg.matrix_rank(fixed_centered) < 2:
        raise ValueError("Kabsch points are geometrically degenerate")
    covariance = moving_centered.T @ fixed_centered
    left, _, right_transposed = np.linalg.svd(covariance)
    rotation = right_transposed.T @ left.T
    if np.linalg.det(rotation) < 0:
        right_transposed[-1, :] *= -1
        rotation = right_transposed.T @ left.T
    translation = fixed_center - rotation @ moving_center
    transformed = (rotation @ moving.T).T + translation
    rmsd = float(np.sqrt(np.mean(np.sum((transformed - fixed) ** 2, axis=1))))
    if not math.isfinite(rmsd) or not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-7):
        raise ValueError("invalid proper Kabsch transform")
    return rotation, translation, rmsd


def align_target_chain(
    moving_residues: Sequence[gemmi.Residue],
    fixed_residues: Sequence[gemmi.Residue],
    minimum_aligned_ca: int,
) -> tuple[np.ndarray, np.ndarray, dict]:
    moving_sequence = chain_sequence(moving_residues)
    fixed_sequence = chain_sequence(fixed_residues)
    residue_pairs = global_sequence_pairs(moving_sequence, fixed_sequence)
    ca_rows: list[tuple[int, int, np.ndarray, np.ndarray]] = []
    for moving_index, fixed_index in residue_pairs:
        moving_ca = residue_ca(moving_residues[moving_index])
        fixed_ca = residue_ca(fixed_residues[fixed_index])
        if moving_ca is not None and fixed_ca is not None:
            ca_rows.append((moving_index, fixed_index, moving_ca, fixed_ca))
    if len(ca_rows) < minimum_aligned_ca:
        raise ValueError(
            f"insufficient aligned C-alpha pairs: observed={len(ca_rows)} "
            f"required={minimum_aligned_ca}"
        )
    moving_coordinates = np.asarray([row[2] for row in ca_rows], dtype=np.float64)
    fixed_coordinates = np.asarray([row[3] for row in ca_rows], dtype=np.float64)
    pre_rmsd = float(
        np.sqrt(np.mean(np.sum((moving_coordinates - fixed_coordinates) ** 2, axis=1)))
    )
    rotation, translation, rmsd = kabsch_transform(
        moving_coordinates, fixed_coordinates
    )
    identical = sum(
        moving_sequence[moving_index] == fixed_sequence[fixed_index]
        for moving_index, fixed_index, _, _ in ca_rows
    )
    evidence = {
        "algorithm": "global_sequence_alignment_then_CA_Kabsch",
        "alignment_scoring": {
            "match": 2.0,
            "mismatch": -1.0,
            "gap_open": -2.0,
            "gap_extend": -0.5,
        },
        "state_target_sequence": moving_sequence,
        "reference_candidate_target_sequence": fixed_sequence,
        "aligned_residue_pair_count": len(residue_pairs),
        "matched_ca_count": len(ca_rows),
        "identical_residue_count": int(identical),
        "sequence_identity_over_matched_ca": float(identical / len(ca_rows)),
        "pre_alignment_rmsd_angstrom": pre_rmsd,
        "rmsd_angstrom": rmsd,
        "rotation_matrix": rotation.tolist(),
        "translation_vector_angstrom": translation.tolist(),
        "residue_pairs": [
            {
                "state_index_1based": moving_index + 1,
                "reference_index_1based": fixed_index + 1,
                "state_residue": moving_sequence[moving_index],
                "reference_residue": fixed_sequence[fixed_index],
            }
            for moving_index, fixed_index, _, _ in ca_rows
        ],
    }
    return rotation, translation, evidence


def apply_transform(
    chain: gemmi.Chain, rotation: np.ndarray, translation: np.ndarray
) -> None:
    for residue in chain:
        for atom in residue:
            coordinate = np.asarray(
                [atom.pos.x, atom.pos.y, atom.pos.z], dtype=np.float64
            )
            transformed = rotation @ coordinate + translation
            atom.pos = gemmi.Position(*(float(value) for value in transformed))


def select_candidate_chains(
    structure: gemmi.Structure,
) -> tuple[gemmi.Chain, list[gemmi.Residue], gemmi.Chain, list[gemmi.Residue]]:
    model = structure[0]
    if len(model) != 2 or {chain.name for chain in model} != {"A", "B"}:
        raise ValueError("candidate CIF must contain exactly target chain A and VHH chain B")
    target = model.find_chain("A")
    vhh = model.find_chain("B")
    if target is None or vhh is None:
        raise ValueError("candidate target/VHH chain lookup failed")
    return target, protein_residues(target), vhh, protein_residues(vhh)


def select_state_chain(structure: gemmi.Structure) -> tuple[gemmi.Chain, list[gemmi.Residue]]:
    model = structure[0]
    if len(model) != 1:
        raise ValueError("state CIF must contain exactly one target chain")
    chain = model[0]
    return chain, protein_residues(chain)


def build_combined_structure(
    candidate: gemmi.Structure,
    state: gemmi.Structure,
    task_id: str,
    minimum_aligned_ca: int,
) -> tuple[str, dict, str, str, int, int]:
    candidate_target, candidate_target_residues, _, vhh_residues = (
        select_candidate_chains(candidate)
    )
    state_chain, state_residues = select_state_chain(state)
    rotation, translation, alignment = align_target_chain(
        state_residues, candidate_target_residues, minimum_aligned_ca
    )
    transformed_target = state_chain.clone()
    apply_transform(transformed_target, rotation, translation)
    transformed_target.name = "A"
    for index, residue in enumerate(transformed_target, 1):
        residue.subchain = "A"
        residue.entity_id = ""
        residue.seqid = gemmi.SeqId(index, " ")

    combined = candidate.clone()
    combined.name = task_id
    model = combined[0]
    target_entity_ids = {residue.entity_id for residue in candidate_target_residues}
    vhh_entity_ids = {residue.entity_id for residue in vhh_residues}
    if len(target_entity_ids) != 1 or len(vhh_entity_ids) != 1:
        raise ValueError("candidate chains do not each bind exactly one polymer entity")
    target_entity_id = next(iter(target_entity_ids))
    vhh_entity_id = next(iter(vhh_entity_ids))
    if target_entity_id == vhh_entity_id:
        raise ValueError("candidate target and VHH unexpectedly share one entity")
    model.remove_chain(candidate_target.name)
    for residue in transformed_target:
        residue.entity_id = target_entity_id
    model.add_chain(transformed_target, pos=0)
    target_entity = combined.get_entity(target_entity_id)
    vhh_entity = combined.get_entity(vhh_entity_id)
    if target_entity is None or vhh_entity is None:
        raise ValueError("candidate polymer entity lookup failed")
    target_entity.entity_type = gemmi.EntityType.Polymer
    target_entity.polymer_type = gemmi.PolymerType.PeptideL
    target_entity.full_sequence = [residue.name for residue in transformed_target]
    if len(vhh_entity.full_sequence) != len(vhh_residues):
        raise ValueError("candidate VHH entity sequence is incomplete")
    combined.assign_label_seq_id(force=True)
    document = combined.make_mmcif_document()
    content = document.as_string()

    reparsed = gemmi.make_structure_from_block(gemmi.cif.read_string(content).sole_block())
    _, reparsed_target, _, reparsed_vhh = select_candidate_chains(reparsed)
    target_sequence = chain_sequence(reparsed_target)
    vhh_sequence = chain_sequence(reparsed_vhh)
    expected_target = chain_sequence(state_residues)
    expected_vhh = chain_sequence(vhh_residues)
    if target_sequence != expected_target or vhh_sequence != expected_vhh:
        raise ValueError("combined mmCIF sequence changed during serialization")
    reparsed_target_entity = reparsed.get_entity(reparsed_target[0].entity_id)
    reparsed_vhh_entity = reparsed.get_entity(reparsed_vhh[0].entity_id)
    if (
        reparsed_target_entity is None
        or len(reparsed_target_entity.full_sequence) != len(reparsed_target)
        or reparsed_vhh_entity is None
        or len(reparsed_vhh_entity.full_sequence) != len(reparsed_vhh)
    ):
        raise ValueError("combined mmCIF lacks complete polymer entity sequences")
    if len(reparsed.connections) != len(candidate.connections):
        raise ValueError("candidate covalent connections were not preserved")
    return (
        content,
        alignment,
        target_sequence,
        vhh_sequence,
        len(candidate_target_residues),
        len(vhh_residues),
    )


def load_metadata(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != METADATA_KEYS:
            raise ValueError(
                f"unexpected inverse metadata schema: observed={sorted(archive.files)}"
            )
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    token_count = arrays["design_mask"].shape[0]
    if token_count <= 0:
        raise ValueError("empty inverse metadata")
    for name, values in arrays.items():
        if values.shape != (token_count,):
            raise ValueError(f"metadata shape mismatch: {name}:{values.shape}")
        if values.dtype.hasobject or not np.issubdtype(values.dtype, np.number):
            raise ValueError(f"metadata must be a numeric array: {name}")
        if not np.isfinite(values).all():
            raise ValueError(f"metadata contains NaN or Inf: {name}")
    for name in ("design_mask", "token_resolved_mask"):
        if not np.isin(arrays[name], [0, 1]).all():
            raise ValueError(f"metadata mask is not binary: {name}")
    for name, allowed in (
        ("mol_type", [0, 1, 2, 3]),
        ("ss_type", [0, 1, 2, 3]),
        ("binding_type", [0, 1, 2]),
    ):
        if not np.equal(arrays[name], np.floor(arrays[name])).all() or not np.isin(
            arrays[name], allowed
        ).all():
            raise ValueError(f"metadata enum is invalid: {name}")
    return arrays


def build_multistate_metadata(
    source: dict[str, np.ndarray],
    source_target_length: int,
    vhh_length: int,
    new_target_length: int,
) -> dict[str, np.ndarray]:
    """Replace target metadata and preserve the candidate VHH token slice exactly."""
    if set(source) != METADATA_KEYS:
        raise ValueError("source metadata keys do not match the frozen folding schema")
    token_count = source["design_mask"].shape[0]
    if token_count != source_target_length + vhh_length:
        raise ValueError("candidate CIF/metadata token lengths disagree")
    if source_target_length < 2 or new_target_length < 2:
        raise ValueError("target must contain at least two residues for binding_type")
    if np.any(source["design_mask"][:source_target_length] != 0):
        raise ValueError("source target unexpectedly contains design-mask residues")

    vhh_slices = {
        name: np.array(values[source_target_length:], copy=True)
        for name, values in source.items()
    }
    if any(values.shape != (vhh_length,) for values in vhh_slices.values()):
        raise ValueError("source VHH metadata slice length mismatch")
    vhh_design_count = int(np.count_nonzero(vhh_slices["design_mask"]))
    if vhh_design_count != 30:
        raise ValueError(
            f"expected exactly 30 VHH CDR design-mask residues, got {vhh_design_count}"
        )

    result: dict[str, np.ndarray] = {}
    for name in sorted(METADATA_KEYS):
        dtype = source[name].dtype
        if name == "token_resolved_mask":
            target = np.ones(new_target_length, dtype=dtype)
        else:
            target = np.zeros(new_target_length, dtype=dtype)
        if name == "binding_type":
            target[:2] = np.asarray(1, dtype=dtype)
        result[name] = np.concatenate((target, vhh_slices[name]))

    expected_length = new_target_length + vhh_length
    if any(values.shape != (expected_length,) for values in result.values()):
        raise ValueError("constructed metadata has inconsistent token lengths")
    if int(np.count_nonzero(result["design_mask"])) != 30:
        raise ValueError("constructed design mask did not preserve 30 CDR residues")
    if not np.array_equal(
        result["design_mask"][new_target_length:], vhh_slices["design_mask"]
    ):
        raise ValueError("constructed metadata changed the VHH design mask")
    if not np.array_equal(
        result["binding_type"][:new_target_length],
        np.r_[np.ones(2, dtype=result["binding_type"].dtype), np.zeros(new_target_length - 2, dtype=result["binding_type"].dtype)],
    ):
        raise ValueError("constructed target binding_type is not first-two-only")
    return result


def write_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.savez_compressed(stream, **{name: arrays[name] for name in sorted(arrays)})
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    observed = load_metadata(path)
    for name, expected in arrays.items():
        if not np.array_equal(observed[name], expected):
            raise ValueError(f"NPZ serialization changed metadata: {path}:{name}")


def parse_runtime_manifest(runtime_root: Path) -> dict[str, dict[str, object]]:
    manifest = runtime_root / "SHA256SUMS"
    rows = parse_manifest(manifest)
    result: dict[str, dict[str, object]] = {}
    for name in ("boltz2_conf_final.ckpt", "mols.zip"):
        expected = rows.get(name)
        path = runtime_root / name
        if expected is None or not path.is_file() or path.is_symlink():
            raise ValueError(f"runtime manifest/file missing or unsafe: {name}")
        result[name] = {
            "path": str(path),
            "sha256_from_runtime_manifest": expected,
            "size_bytes": path.stat().st_size,
        }
    return result


def set_nested(mapping: dict, keys: Sequence[str], value: object) -> None:
    cursor: object = mapping
    for key in keys[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            raise ValueError(f"folding config missing path: {'.'.join(keys)}")
        cursor = cursor[key]
    if not isinstance(cursor, dict) or keys[-1] not in cursor:
        raise ValueError(f"folding config missing path: {'.'.join(keys)}")
    cursor[keys[-1]] = value


def build_folding_config(
    source_path: Path, output_root: Path, runtime_root: Path
) -> dict:
    source = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise ValueError("source folding config must be a mapping")
    expected = {
        ("data", "target_templates"): True,
        ("data", "return_native"): False,
        ("data", "fail_if_no_designs"): True,
        ("data", "skip_existing"): False,
        ("data", "cfg", "batch_size"): 1,
        ("data", "cfg", "num_workers"): 4,
        ("trainer", "accelerator"): "gpu",
        ("trainer", "devices"): 1,
        ("trainer", "precision"): "bf16-mixed",
        ("sampling_steps",): 200,
        ("recycling_steps",): 3,
        ("diffusion_samples",): 5,
        ("override", "use_kernels"): True,
    }
    for keys, expected_value in expected.items():
        cursor: object = source
        for key in keys:
            if not isinstance(cursor, dict) or key not in cursor:
                raise ValueError(f"source folding config missing {'.'.join(keys)}")
            cursor = cursor[key]
        if cursor != expected_value:
            raise ValueError(
                f"source folding config drift: {'.'.join(keys)} "
                f"expected={expected_value!r} actual={cursor!r}"
            )

    config = deepcopy(source)
    design_dir = str(output_root / "design_inputs")
    set_nested(config, ("data", "design_dir"), design_dir)
    set_nested(config, ("data", "cfg", "moldir"), str(runtime_root / "mols.zip"))
    set_nested(config, ("writer", "design_dir"), design_dir)
    set_nested(config, ("output",), design_dir)
    set_nested(config, ("checkpoint",), str(runtime_root / "boltz2_conf_final.ckpt"))
    return config


def builder_code_identity(repo_root: Path) -> dict[str, object]:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    status = git("status", "--short")
    script = Path(__file__).resolve()
    return {
        "repo_root": str(repo_root),
        "repo_commit": git("rev-parse", "HEAD"),
        "repo_tree": git("rev-parse", "HEAD^{tree}"),
        "worktree_clean": not bool(status),
        "builder_path": str(script),
        "builder_sha256": stable_digest(script),
    }


def format_tasks_tsv(tasks: Sequence[dict]) -> str:
    fields = (
        "task_id",
        "candidate_id",
        "target_state_id",
        "panel_role",
        "target_identity",
        "target_sequence",
        "vhh_sequence",
        "design_mask_count",
        "matched_ca_count",
        "alignment_rmsd_angstrom",
        "target_source_sha256",
        "input_cif_relative_path",
        "input_npz_relative_path",
    )
    lines = ["\t".join(fields)]
    for task in tasks:
        values = {
            **task,
            "matched_ca_count": task["alignment"]["matched_ca_count"],
            "alignment_rmsd_angstrom": task["alignment"]["rmsd_angstrom"],
        }
        lines.append("\t".join(str(values[field]) for field in fields))
    return "\n".join(lines) + "\n"


def build_inputs(
    repo_root: Path,
    anchor_root: Path,
    output: Path,
    candidate_ids: Sequence[str],
    state_ids: Sequence[str],
    runtime_root: Path,
    minimum_aligned_ca: int,
) -> None:
    if minimum_aligned_ca < 3:
        raise ValueError("minimum aligned C-alpha count must be at least 3")
    anchor_payload, anchors, anchor_manifest = load_anchor_selection(
        anchor_root, candidate_ids
    )
    states, state_catalog, state_catalog_sha = load_selected_states(
        repo_root, state_ids
    )
    runtime_assets = parse_runtime_manifest(runtime_root)

    design_dir = output / "design_inputs"
    evidence_dir = output / "alignment_evidence"
    config_dir = output / "config"
    source_evidence_dir = output / "source_evidence"
    for directory in (design_dir, evidence_dir, config_dir, source_evidence_dir):
        directory.mkdir(mode=0o700)

    atomic_write(
        source_evidence_dir / "ANCHOR_SET.json",
        (anchor_root / "ANCHOR_SET.json").read_bytes(),
    )
    atomic_write(
        source_evidence_dir / "ANCHOR_SHA256SUMS",
        (anchor_root / "SHA256SUMS").read_bytes(),
    )
    atomic_write(
        source_evidence_dir / "development_state_contract.tsv",
        state_catalog.read_bytes(),
    )

    tasks: list[dict] = []
    candidate_summaries: list[dict] = []
    for anchor in anchors:
        candidate_id = anchor["candidate_id"]
        anchor_relative = anchor["anchor_relative_root"]
        candidate_cif_relative = f"{anchor_relative}/inverse_folded.cif"
        candidate_npz_relative = f"{anchor_relative}/inverse_metadata.npz"
        candidate_cif = anchor_root / candidate_cif_relative
        candidate_npz = anchor_root / candidate_npz_relative
        candidate_structure = load_structure(candidate_cif)
        _, candidate_target_residues, _, candidate_vhh_residues = select_candidate_chains(
            candidate_structure
        )
        candidate_target_sequence = chain_sequence(candidate_target_residues)
        vhh_sequence = chain_sequence(candidate_vhh_residues)
        metric_sequence = anchor.get("metrics", {}).get("designed_chain_sequence")
        if metric_sequence != vhh_sequence:
            raise ValueError(f"anchor metric/CIF VHH sequence mismatch: {candidate_id}")
        metadata = load_metadata(candidate_npz)
        candidate_summaries.append(
            {
                "candidate_id": candidate_id,
                "source_final_rank": anchor["final_rank"],
                "source_strict_filters_passed": not anchor.get(
                    "selected_despite_strict_filter_failure", True
                ),
                "candidate_target_sequence": candidate_target_sequence,
                "vhh_sequence": vhh_sequence,
                "design_mask_count": int(np.count_nonzero(metadata["design_mask"])),
                "source_cif_sha256": anchor_manifest[candidate_cif_relative],
                "source_npz_sha256": anchor_manifest[candidate_npz_relative],
            }
        )

        for state_row in states:
            state_id = state_row["target_state_id"]
            # FromGeneratedDataModule's frozen default target_id_regex rejects
            # empty underscore-delimited segments, so never use "__" here.
            task_id = make_task_id(candidate_id, state_id)
            state_path = Path(state_row["source_resolved_path"])
            state_structure = load_structure(state_path)
            (
                cif_content,
                alignment,
                target_sequence,
                combined_vhh_sequence,
                source_target_length,
                vhh_length,
            ) = build_combined_structure(
                candidate_structure,
                state_structure,
                task_id,
                minimum_aligned_ca,
            )
            if combined_vhh_sequence != vhh_sequence:
                raise ValueError(f"VHH changed while building task: {task_id}")
            combined_metadata = build_multistate_metadata(
                metadata,
                source_target_length,
                vhh_length,
                len(target_sequence),
            )
            cif_relative = f"design_inputs/{task_id}.cif"
            npz_relative = f"design_inputs/{task_id}.npz"
            atomic_write(output / cif_relative, cif_content.encode("utf-8"))
            write_npz(output / npz_relative, combined_metadata)

            alignment_payload = {
                "schema_version": "WINDOWS_OWNER_TARGET_ALIGNMENT_V1",
                "status": "PASS",
                "task_id": task_id,
                "candidate_id": candidate_id,
                "target_state_id": state_id,
                **alignment,
            }
            alignment_relative = f"alignment_evidence/{task_id}.json"
            write_json(output / alignment_relative, alignment_payload)
            task = {
                "task_id": task_id,
                "candidate_id": candidate_id,
                "source_final_rank": anchor["final_rank"],
                "source_strict_filters_passed": not anchor.get(
                    "selected_despite_strict_filter_failure", True
                ),
                "target_state_id": state_id,
                "state_order": int(state_row["state_order"]),
                "panel_role": state_row["panel_role"],
                "target_identity": state_row["target_identity"],
                "target_sequence": target_sequence,
                "vhh_sequence": vhh_sequence,
                "design_mask_count": int(
                    np.count_nonzero(combined_metadata["design_mask"])
                ),
                "input_token_count": int(combined_metadata["design_mask"].shape[0]),
                "input_cif_relative_path": cif_relative,
                "input_npz_relative_path": npz_relative,
                "alignment_evidence_relative_path": alignment_relative,
                "target_source_path": state_row["source_resolved_path"],
                "target_source_alias_path": state_row["source_alias_path"],
                "target_source_resolved_path": state_row["source_resolved_path"],
                "target_source_relative_path": state_row["relative_path"],
                "target_source_sha256": state_row["sha256"],
                "alignment": {
                    "matched_ca_count": alignment["matched_ca_count"],
                    "rmsd_angstrom": alignment["rmsd_angstrom"],
                    "sequence_identity_over_matched_ca": alignment[
                        "sequence_identity_over_matched_ca"
                    ],
                },
            }
            tasks.append(task)

    expected_task_count = len(candidate_ids) * len(state_ids)
    if len(tasks) != expected_task_count or len({task["task_id"] for task in tasks}) != len(
        tasks
    ):
        raise ValueError("constructed task matrix is incomplete or duplicate")
    if any(task["design_mask_count"] != 30 for task in tasks):
        raise ValueError("task matrix did not preserve exactly 30 CDR design residues")

    tasks_payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "INPUTS_READY",
        "scope": "DEVELOPMENT_ONLY",
        "samples_per_task": 5,
        "candidate_ids": list(candidate_ids),
        "state_ids": list(state_ids),
        "task_count": len(tasks),
        "tasks": tasks,
    }
    write_json(output / "tasks.json", tasks_payload)
    write_text(output / "tasks.tsv", format_tasks_tsv(tasks))

    folding_source = anchor_root / FOLDING_CONFIG_RELATIVE
    if FOLDING_CONFIG_RELATIVE.as_posix() not in anchor_manifest:
        raise ValueError("T9 manifest does not bind the source folding config")
    folding_config = build_folding_config(folding_source, output, runtime_root)
    write_text(
        config_dir / "folding.yaml",
        yaml.safe_dump(folding_config, sort_keys=False, allow_unicode=True),
    )
    write_text(
        output / "steps.yaml",
        yaml.safe_dump(
            {"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]},
            sort_keys=False,
        ),
    )

    inputs_payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "INPUTS_READY",
        "authority": "WINDOWS_CODEX",
        "scope": "DEVELOPMENT_ONLY",
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
        "training_performed": False,
        "weights_modified": False,
        "formal_gate_claimed": False,
        "candidate_ids": list(candidate_ids),
        "state_ids": list(state_ids),
        "candidate_count": len(candidate_ids),
        "state_count": len(state_ids),
        "task_count": len(tasks),
        "samples_per_task": 5,
        "fold_rows_expected": len(tasks) * 5,
        "minimum_aligned_ca": minimum_aligned_ca,
        "design_mask_semantics": (
            "target tokens are fixed; the exact 30-residue VHH CDR mask is preserved "
            "from each T9 inverse_metadata NPZ"
        ),
        "target_binding_type_semantics": "only target residues 1 and 2 are set to 1",
        "source_anchor_set": {
            "path": str(anchor_root),
            "schema_version": anchor_payload["schema_version"],
            "anchor_set_sha256": stable_digest(anchor_root / "ANCHOR_SET.json"),
            "manifest_sha256": stable_digest(anchor_root / "SHA256SUMS"),
        },
        "state_catalog": {
            "path": str(state_catalog),
            "sha256": state_catalog_sha,
        },
        "runtime": {
            "root": str(runtime_root),
            "manifest_sha256": stable_digest(runtime_root / "SHA256SUMS"),
            "assets": runtime_assets,
            "note": "large runtime assets are bound to their pre-existing runtime manifest; this input builder does not redundantly rehash them",
        },
        "folding_contract": {
            "batch_size": 1,
            "num_workers": 4,
            "devices": 1,
            "precision": "bf16-mixed",
            "sampling_steps": 200,
            "recycling_steps": 3,
            "diffusion_samples": 5,
            "target_templates": True,
            "use_kernels": True,
            "skip_existing": False,
            "design_dir": str(output / "design_inputs"),
        },
        "candidates": candidate_summaries,
        "states": [
            {
                key: row[key]
                for key in (
                    "state_order",
                    "target_state_id",
                    "panel_role",
                    "target_identity",
                    "source_alias_path",
                    "source_resolved_path",
                    "relative_path",
                    "sha256",
                )
            }
            for row in states
        ],
        "builder_code_identity": builder_code_identity(repo_root),
    }
    write_json(output / "INPUTS.json", inputs_payload)
    write_text(output / "STATUS.txt", "INPUTS_READY\n")
    seal_input_snapshot(output)


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve(strict=True)
    anchor_root = args.anchor_set.resolve(strict=True)
    runtime_root = args.runtime_root.resolve(strict=True)
    if repo_root.is_symlink() or not (repo_root / ".git").is_dir():
        raise SystemExit(f"repo root is not a regular Git worktree: {repo_root}")
    for label, root in (("anchor set", anchor_root), ("runtime root", runtime_root)):
        if root.is_symlink() or not root.is_dir():
            raise SystemExit(f"unsafe {label}: {root}")
    candidate_ids = validate_requested_ids(args.candidate_ids, "candidate IDs")
    state_ids = validate_requested_ids(args.state_ids, "state IDs")

    requested = args.output.absolute()
    requested.parent.mkdir(parents=True, exist_ok=True)
    requested = requested.parent.resolve(strict=True) / requested.name
    if requested.exists() or requested.is_symlink():
        raise SystemExit(f"output already exists: {requested}")
    staging = requested.with_name(f".{requested.name}.BUILDING_{os.getpid()}")
    if staging.exists() or staging.is_symlink():
        raise SystemExit(f"staging output already exists: {staging}")
    staging.mkdir(mode=0o700)
    write_text(staging / "STATUS.txt", "INPUTS_BUILDING\n")

    try:
        # The final absolute path is intentionally embedded in folding.yaml before
        # atomic publication, so `boltzgen execute <output> --steps folding` works.
        (staging / "STATUS.txt").unlink()
        build_inputs(
            repo_root,
            anchor_root,
            staging,
            candidate_ids,
            state_ids,
            runtime_root,
            args.minimum_aligned_ca,
        )
        # Rebind staging paths in the already validated configuration/receipt to the
        # final no-replace destination, then reseal the complete input snapshot.
        config_path = staging / "config/folding.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        staging_design = str(staging / "design_inputs")
        final_design = str(requested / "design_inputs")
        serialized = yaml.safe_dump(config, sort_keys=False, allow_unicode=True).replace(
            staging_design, final_design
        )
        config_path.unlink()
        write_text(config_path, serialized)
        inputs_path = staging / "INPUTS.json"
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        inputs["folding_contract"]["design_dir"] = final_design
        inputs_path.unlink()
        write_json(inputs_path, inputs)
        (staging / "INPUT_SHA256SUMS").unlink()
        seal_input_snapshot(staging)
        publish_directory_no_replace(staging, requested)
    except BaseException as exc:
        if staging.exists():
            for name in ("INPUT_SHA256SUMS", "STATUS.txt", "ERROR.json"):
                try:
                    (staging / name).unlink()
                except FileNotFoundError:
                    pass
            write_text(staging / "STATUS.txt", "INPUTS_FAILED\n")
            write_json(
                staging / "ERROR.json",
                {
                    "status": "INPUTS_FAILED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            try:
                seal_input_snapshot(staging)
            except BaseException:
                pass
            failed = requested.with_name(f"{requested.name}.FAILED_{os.getpid()}")
            if not failed.exists():
                try:
                    publish_directory_no_replace(staging, failed)
                except BaseException:
                    pass
        raise

    print(f"INPUTS_READY path={requested} tasks={len(candidate_ids) * len(state_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
