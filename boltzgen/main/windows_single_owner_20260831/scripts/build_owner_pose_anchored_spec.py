#!/usr/bin/env python3
"""Build or reject a frozen-target/T9-VHH pose-anchored specification.

The geometry source is the sealed T9 ``design_3/raw_design.cif``.  Its target is
used only to place its VHH in the frozen T4 target frame.  The T9 refold route is
measured and explicitly disabled because its target fit is unsafe.  A runnable
four-file bundle is emitted only when the frozen internal 30,600-pose search or
an explicitly supplied external rigid transform passes every interface-geometry
gate and the materialized bundle passes a real ``boltzgen check`` replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

import gemmi
import numpy as np
import yaml
from scipy.spatial import cKDTree


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_owner_multistate_inputs import (  # noqa: E402
    align_target_chain,
    apply_transform,
    atomic_write,
    chain_sequence,
    kabsch_transform,
    load_structure,
    parse_manifest,
    protein_residues,
    publish_directory_no_replace,
    regular_file_closure,
    residue_ca,
    stable_digest,
)


SCHEMA_VERSION = "WINDOWS_OWNER_POSE_ANCHORED_SPEC_V1"
ALLOWED_CANDIDATE_ID = "design_3"
FROZEN_BUNDLE_FILES = (
    "design.yaml",
    "target.cif",
    "scaffold.cif",
    "scaffold.yaml",
)
DESIGN_RANGE_TEXT = "26..33,51..57,96..110"
DESIGN_INDICES = tuple(
    [*range(26, 34), *range(51, 58), *range(96, 111)]
)
HOTSPOTS = ((1, "His7", "HIS"), (2, "Ala8", "ALA"))
DEFAULT_MAX_CA_RMSD_ANGSTROM = 2.0
DEFAULT_MAX_CA_RESIDUAL_ANGSTROM = 5.0
DEFAULT_HEAVY_ATOM_CLASH_CUTOFF_ANGSTROM = 2.0
DEFAULT_MAX_HEAVY_ATOM_CLASH_COUNT = 0
MIN_TARGET_VHH_CA_DISTANCE_ANGSTROM = 3.6
MIN_HOTSPOT_CDR_CA_DISTANCE_ANGSTROM = 4.0
MAX_HOTSPOT_CDR_CA_DISTANCE_ANGSTROM = 8.0
INTERFACE_HEAVY_CONTACT_CUTOFF_ANGSTROM = 4.0
CDR_CONTACT_CUTOFF_ANGSTROM = 5.0
MIN_CDR_CONTACT_RESIDUE_COUNT = 3
EXTERNAL_TRANSFORM_SCHEMA = "WINDOWS_OWNER_RIGID_POSE_TRANSFORM_V1"

EXPECTED_DESIGN_DOCUMENT = {
    "entities": [
        {
            "file": {
                "path": "target.cif",
                "include": [{"chain": {"id": "E", "res_index": "1..30"}}],
                "binding_types": [
                    {"chain": {"id": "E", "binding": "1..2"}}
                ],
                "structure_groups": [
                    {"group": {"id": "E", "visibility": 1}}
                ],
            }
        },
        {"file": {"path": "scaffold.yaml"}},
    ]
}
EXPECTED_SCAFFOLD_DOCUMENT = {
    "path": "scaffold.cif",
    "include": [{"chain": {"id": "A"}}],
    "design": [
        {"chain": {"id": "A", "res_index": DESIGN_RANGE_TEXT}}
    ],
    "structure_groups": [
        {"group": {"id": "A", "visibility": 2}},
        {
            "group": {
                "id": "A",
                "visibility": 0,
                "res_index": DESIGN_RANGE_TEXT,
            }
        },
    ],
    "reset_res_index": [{"chain": {"id": "A"}}],
}


class BoundFile:
    __slots__ = ("path", "sha256", "size_bytes", "identity", "data")

    def __init__(
        self,
        path: Path,
        sha256: str,
        size_bytes: int,
        identity: tuple[int, int, int, int, int],
        data: bytes | None,
    ) -> None:
        self.path = path
        self.sha256 = sha256
        self.size_bytes = size_bytes
        self.identity = identity
        self.data = data


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_bound_file(path: Path, *, retain_bytes: bool = True) -> BoundFile:
    """Read/hash one fd once and bind content to its before/after identity."""
    if path.is_symlink():
        raise ValueError(f"bound input may not be a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    chunks: list[bytes] | None = [] if retain_bytes else None
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"bound input must be a regular file: {path}")
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _stat_identity(before) != _stat_identity(after):
        raise ValueError(f"input changed while being captured: {path}")
    return BoundFile(
        path=path.resolve(strict=True),
        sha256=digest.hexdigest(),
        size_bytes=before.st_size,
        identity=_stat_identity(before),
        data=b"".join(chunks) if chunks is not None else None,
    )


def revalidate_bound_file(bound: BoundFile) -> None:
    current = read_bound_file(bound.path, retain_bytes=False)
    if current.identity != bound.identity or current.sha256 != bound.sha256:
        raise ValueError(f"terminal input identity/digest changed: {bound.path}")


def revalidate_bound_inputs(bounds: Sequence[BoundFile]) -> None:
    seen: set[Path] = set()
    for bound in bounds:
        if bound.path in seen:
            continue
        seen.add(bound.path)
        revalidate_bound_file(bound)


def revalidate_input_closures(
    contracts: Sequence[tuple[Path, set[str], set[str]]]
) -> None:
    for root, excluded, expected in contracts:
        observed = regular_file_closure(root, excluded)
        if observed != expected:
            raise ValueError(f"terminal input closure changed: {root}")


def _require_bound_bytes(bound: BoundFile) -> bytes:
    if bound.data is None:
        raise ValueError(f"captured bytes were not retained: {bound.path}")
    return bound.data


def parse_manifest_bytes(content: bytes, label: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"manifest is not UTF-8: {label}") from exc
    for line in lines:
        match = re.fullmatch(
            r"([0-9a-f]{64})  (?:\./)?([^\x00\r\n]+)", line
        )
        if match is None:
            raise ValueError(f"invalid manifest row in {label}: {line!r}")
        relative = Path(match.group(2))
        canonical = relative.as_posix()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in match.group(2)
            or canonical in rows
            or canonical != match.group(2).removeprefix("./")
        ):
            raise ValueError(f"unsafe/noncanonical manifest path: {match.group(2)!r}")
        rows[canonical] = match.group(1)
    if not rows:
        raise ValueError(f"empty manifest: {label}")
    return rows


def load_structure_from_bound(bound: BoundFile) -> gemmi.Structure:
    try:
        text = _require_bound_bytes(bound).decode("utf-8")
        document = gemmi.cif.read_string(text)
        structure = gemmi.make_structure_from_block(document.sole_block())
    except Exception as exc:
        raise ValueError(f"cannot parse captured mmCIF {bound.path}: {exc}") from exc
    if len(structure) != 1:
        raise ValueError(f"captured mmCIF requires exactly one model: {bound.path}")
    return structure


def capture_manifest_tree(
    root: Path, manifest_name: str = "SHA256SUMS"
) -> tuple[dict[str, str], dict[str, BoundFile], BoundFile]:
    root = require_input_directory(root, "manifest tree")
    manifest = read_bound_file(root / manifest_name)
    rows = parse_manifest_bytes(_require_bound_bytes(manifest), str(manifest.path))
    observed = regular_file_closure(root, {manifest_name})
    if observed != set(rows):
        raise ValueError(
            f"manifest tree closure mismatch: missing={sorted(observed-set(rows))} "
            f"unexpected={sorted(set(rows)-observed)}"
        )
    bounds: dict[str, BoundFile] = {}
    for relative, expected in rows.items():
        bound = read_bound_file(root / relative)
        if bound.sha256 != expected:
            raise ValueError(f"manifest digest mismatch: {relative}")
        bounds[relative] = bound
    if regular_file_closure(root, {manifest_name}) != set(rows):
        raise ValueError("manifest tree closure changed while being captured")
    revalidate_bound_file(manifest)
    return rows, bounds, manifest


def select_design3_anchor(
    anchor_json: BoundFile, manifest_rows: dict[str, str]
) -> tuple[dict[str, object], dict[str, object]]:
    payload = json.loads(_require_bound_bytes(anchor_json).decode("utf-8"))
    if (
        payload.get("schema_version") != "WINDOWS_OWNER_LOCAL_ANCHOR_SET_V1"
        or payload.get("status") != "LOCAL_ANCHOR_SET_READY"
        or payload.get("training_performed") is not False
        or payload.get("selection", {}).get("scope") != "DEVELOPMENT_ONLY"
    ):
        raise ValueError("anchor set is not a ready development-only T9 snapshot")
    anchors = payload.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError("T9 anchors must be a list")
    selected = [
        row
        for row in anchors
        if isinstance(row, dict) and row.get("candidate_id") == ALLOWED_CANDIDATE_ID
    ]
    if len(selected) != 1:
        raise ValueError("T9 must contain exactly one design_3 anchor")
    anchor = selected[0]
    if anchor.get("metrics", {}).get("id") != ALLOWED_CANDIDATE_ID:
        raise ValueError("T9 metric source candidate ID does not exactly match design_3")
    rank = anchor.get("final_rank")
    if not isinstance(rank, int) or rank < 1:
        raise ValueError("T9 design_3 final rank is invalid")
    files = anchor.get("files")
    if not isinstance(files, dict):
        raise ValueError("T9 design_3 file evidence is missing")
    base = f"anchors/rank{rank:02d}_{ALLOWED_CANDIDATE_ID}"
    for logical_name in ("raw_design.cif", "refolded.cif"):
        evidence = files.get(logical_name)
        if not isinstance(evidence, dict):
            raise ValueError(f"T9 design_3 {logical_name} evidence is missing")
        relative = f"{base}/{logical_name}"
        if (
            evidence.get("sha256") != manifest_rows.get(relative)
            or Path(str(evidence.get("source_relative_path", ""))).stem
            != ALLOWED_CANDIDATE_ID
        ):
            raise ValueError(
                f"T9 design_3 {logical_name} JSON/manifest/source binding mismatch"
            )
    return payload, anchor


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a hard-gated pose-anchored spec from sealed T9 design_3 raw "
            "geometry and the frozen T4 target. Internal mode deterministically "
            "enumerates 30,600 rigid poses; unsafe runs publish no spec bundle."
        )
    )
    parser.add_argument("--spec-bundle", required=True, type=Path)
    parser.add_argument("--anchor-set", required=True, type=Path)
    parser.add_argument("--candidate-id", default=ALLOWED_CANDIDATE_ID)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--boltzgen-launcher",
        required=True,
        type=Path,
        help="Explicit boltzgen executable used for the mandatory pre-READY check.",
    )
    parser.add_argument(
        "--moldir",
        required=True,
        type=Path,
        help="Explicit molecule dictionary archive passed to boltzgen check.",
    )
    parser.add_argument("--check-timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--pose-search-mode",
        choices=("none", "external", "internal"),
        default="none",
    )
    parser.add_argument("--external-transform-json", type=Path)
    parser.add_argument("--minimum-aligned-ca", type=int, default=8)
    parser.add_argument(
        "--max-ca-rmsd-angstrom",
        type=float,
        default=DEFAULT_MAX_CA_RMSD_ANGSTROM,
    )
    parser.add_argument(
        "--max-ca-residual-angstrom",
        type=float,
        default=DEFAULT_MAX_CA_RESIDUAL_ANGSTROM,
    )
    parser.add_argument(
        "--heavy-atom-clash-cutoff-angstrom",
        type=float,
        default=DEFAULT_HEAVY_ATOM_CLASH_CUTOFF_ANGSTROM,
    )
    parser.add_argument(
        "--max-heavy-atom-clash-count",
        type=int,
        default=DEFAULT_MAX_HEAVY_ATOM_CLASH_COUNT,
    )
    return parser.parse_args(argv)


def validate_policy(arguments: argparse.Namespace) -> None:
    if arguments.candidate_id != ALLOWED_CANDIDATE_ID:
        raise ValueError(
            "pose builder only accepts the exact T9 source candidate design_3"
        )
    if arguments.minimum_aligned_ca < 3:
        raise ValueError("minimum aligned C-alpha count must be at least 3")
    for name in (
        "max_ca_rmsd_angstrom",
        "max_ca_residual_angstrom",
        "heavy_atom_clash_cutoff_angstrom",
    ):
        value = getattr(arguments, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if arguments.max_heavy_atom_clash_count < 0:
        raise ValueError("max_heavy_atom_clash_count must be nonnegative")
    if (
        not math.isfinite(arguments.check_timeout_seconds)
        or arguments.check_timeout_seconds <= 0
    ):
        raise ValueError("check_timeout_seconds must be finite and positive")
    if arguments.pose_search_mode == "external":
        if arguments.external_transform_json is None:
            raise ValueError(
                "external pose-search mode requires --external-transform-json"
            )
    elif arguments.external_transform_json is not None:
        raise ValueError(
            "--external-transform-json is only valid with --pose-search-mode external"
        )


def require_input_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symlink: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory: {path}")
    return resolved


def ensure_output_absent(output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists; overwrite is forbidden: {output}")
    if not output.name or output.name in {".", ".."}:
        raise ValueError(f"unsafe output path: {output}")


def validate_bundle_closure(root: Path) -> dict[str, str]:
    """Require the frozen bundle to contain exactly four regular files."""
    observed: set[str] = set()
    for member in root.iterdir():
        info = member.lstat()
        if member.is_symlink():
            raise ValueError(f"frozen spec bundle contains a symlink: {member.name}")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(
                f"frozen spec bundle contains a non-regular member: {member.name}"
            )
        observed.add(member.name)
    expected = set(FROZEN_BUNDLE_FILES)
    if observed != expected:
        raise ValueError(
            "frozen four-file bundle closure mismatch: "
            f"missing={sorted(expected - observed)} extra={sorted(observed - expected)}"
        )
    return {name: stable_digest(root / name) for name in FROZEN_BUNDLE_FILES}


def capture_bundle(root: Path) -> dict[str, BoundFile]:
    observed: set[str] = set()
    for member in root.iterdir():
        info = member.lstat()
        if member.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"unsafe member in captured four-file bundle: {member.name}")
        observed.add(member.name)
    if observed != set(FROZEN_BUNDLE_FILES):
        raise ValueError("captured four-file bundle closure mismatch")
    bounds = {name: read_bound_file(root / name) for name in FROZEN_BUNDLE_FILES}
    if set(member.name for member in root.iterdir()) != set(FROZEN_BUNDLE_FILES):
        raise ValueError("four-file bundle closure changed while being captured")
    return bounds


def validate_frozen_spec_contract(
    root: Path, bounds: dict[str, BoundFile] | None = None
) -> dict[str, object]:
    if bounds is None:
        bounds = capture_bundle(root)
    design = yaml.safe_load(_require_bound_bytes(bounds["design.yaml"]).decode("utf-8"))
    scaffold = yaml.safe_load(
        _require_bound_bytes(bounds["scaffold.yaml"]).decode("utf-8")
    )
    if design != EXPECTED_DESIGN_DOCUMENT:
        raise ValueError(
            "frozen design.yaml target/group contract differs from the sealed T4 input"
        )
    if scaffold != EXPECTED_SCAFFOLD_DOCUMENT:
        raise ValueError(
            "frozen scaffold.yaml include/design/group contract differs from the sealed T4 input"
        )
    return {
        "target_spec_chain_id": "E",
        "target_binding_residue_range": "1..2",
        "target_structure_group_visibility": 1,
        "scaffold_spec_chain_id": "A",
        "source_framework_structure_group_visibility": 2,
        "source_design_structure_group_visibility": 0,
        "design_residue_range": DESIGN_RANGE_TEXT,
        "design_residue_count": len(DESIGN_INDICES),
    }


def resolve_spec_chain(
    structure: gemmi.Structure, spec_chain_id: str, role: str
) -> tuple[gemmi.Chain, list[gemmi.Residue], dict[str, object]]:
    """Resolve a YAML chain ID against both label_asym_id and auth_asym_id."""
    model = structure[0]
    matches: list[tuple[gemmi.Chain, list[gemmi.Residue], list[str], str]] = []
    for chain in model:
        residues = protein_residues(chain)
        label_ids = sorted({residue.subchain for residue in residues})
        if not label_ids or any(not value for value in label_ids):
            raise ValueError(f"{role} chain {chain.name!r} has no label_asym_id")
        auth_match = chain.name == spec_chain_id
        label_match = spec_chain_id in label_ids
        if auth_match or label_match:
            if auth_match and label_match:
                kind = "auth_and_label_asym_id"
            elif label_match:
                kind = "label_asym_id"
            else:
                kind = "auth_asym_id"
            matches.append((chain, residues, label_ids, kind))
    if len(matches) != 1:
        raise ValueError(
            f"{role} spec chain {spec_chain_id!r} must resolve exactly once; "
            f"matches={[(item[0].name, item[2]) for item in matches]}"
        )
    chain, residues, label_ids, kind = matches[0]
    return chain, residues, {
        "role": role,
        "spec_chain_id": spec_chain_id,
        "match_kind": kind,
        "auth_asym_id": chain.name,
        "label_asym_ids": label_ids,
    }


def atom_key(atom: gemmi.Atom) -> tuple[str, str, str]:
    return atom.name.strip(), atom.element.name, str(atom.altloc)


def validate_chain_inventory(
    residues: Sequence[gemmi.Residue], expected_count: int, role: str
) -> list[tuple[tuple[str, str, str], ...]]:
    if len(residues) != expected_count:
        raise ValueError(
            f"{role} residue count mismatch: expected={expected_count} observed={len(residues)}"
        )
    labels = [residue.label_seq for residue in residues]
    if labels != list(range(1, expected_count + 1)):
        raise ValueError(f"{role} label_seq_id values are not exactly 1..{expected_count}")
    inventories: list[tuple[tuple[str, str, str], ...]] = []
    for index, residue in enumerate(residues, 1):
        if len(residue) == 0:
            raise ValueError(f"{role} residue {index} has no atoms")
        keys = [atom_key(atom) for atom in residue]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{role} residue {index} has duplicate atom identities")
        for atom in residue:
            coordinate = (atom.pos.x, atom.pos.y, atom.pos.z)
            if not all(math.isfinite(value) for value in coordinate):
                raise ValueError(f"{role} residue {index} has non-finite coordinates")
            if not atom.name.strip() or atom.element.name == "X":
                raise ValueError(f"{role} residue {index} has an invalid atom identity")
        inventories.append(tuple(sorted(keys)))
    return inventories


def _find_residue_for_connection(
    residues: Sequence[gemmi.Residue], partner: gemmi.AtomAddress, role: str
) -> tuple[int, gemmi.Residue, gemmi.Atom]:
    matches = [
        residue
        for residue in residues
        if residue.seqid == partner.res_id.seqid and residue.name == partner.res_id.name
    ]
    if len(matches) != 1:
        raise ValueError(f"{role} disulfide endpoint does not resolve exactly once")
    residue = matches[0]
    atoms = [atom for atom in residue if atom.name.strip() == partner.atom_name.strip()]
    if len(atoms) != 1:
        raise ValueError(f"{role} disulfide atom does not resolve exactly once")
    if residue.label_seq is None:
        raise ValueError(f"{role} disulfide residue lacks label_seq_id")
    return int(residue.label_seq), residue, atoms[0]


def validate_vhh_disulfide(
    structure: gemmi.Structure,
    chain: gemmi.Chain,
    residues: Sequence[gemmi.Residue],
    role: str,
) -> dict[str, object]:
    disulfides = [
        connection
        for connection in structure.connections
        if connection.type == gemmi.ConnectionType.Disulf
    ]
    if len(disulfides) != 1 or len(structure.connections) != 1:
        raise ValueError(f"{role} must contain exactly one disulfide connection")
    connection = disulfides[0]
    endpoints = []
    atoms = []
    for partner in (connection.partner1, connection.partner2):
        if partner.chain_name != chain.name:
            raise ValueError(f"{role} disulfide is not wholly inside chain {chain.name}")
        label_index, residue, atom = _find_residue_for_connection(
            residues, partner, role
        )
        if residue.name != "CYS" or partner.atom_name.strip() != "SG":
            raise ValueError(f"{role} disulfide must connect two CYS SG atoms")
        endpoints.append(label_index)
        atoms.append(atom)
    if tuple(sorted(endpoints)) != (22, 95):
        raise ValueError(
            f"{role} disulfide label positions must be 22 and 95; observed={endpoints}"
        )
    distance = float(atoms[0].pos.dist(atoms[1].pos))
    if not 1.5 <= distance <= 2.5:
        raise ValueError(f"{role} disulfide geometry is implausible: {distance:.6f} A")
    return {
        "connection_name": connection.name,
        "label_seq_positions": sorted(endpoints),
        "atom_name": "SG",
        "distance_angstrom": distance,
    }


def _transformed_alignment_residuals(
    moving_residues: Sequence[gemmi.Residue],
    fixed_residues: Sequence[gemmi.Residue],
    rotation: np.ndarray,
    translation: np.ndarray,
    residue_pairs: Sequence[dict[str, object]],
) -> list[float]:
    residuals: list[float] = []
    for row in residue_pairs:
        moving_index = int(row["state_index_1based"]) - 1
        fixed_index = int(row["reference_index_1based"]) - 1
        moving = residue_ca(moving_residues[moving_index])
        fixed = residue_ca(fixed_residues[fixed_index])
        if moving is None or fixed is None:
            raise ValueError("alignment evidence references a missing C-alpha atom")
        residuals.append(float(np.linalg.norm(rotation @ moving + translation - fixed)))
    return residuals


def build_alignment_diagnostic(
    moving_residues: Sequence[gemmi.Residue],
    fixed_residues: Sequence[gemmi.Residue],
    minimum_aligned_ca: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    rotation, translation, evidence = align_target_chain(
        moving_residues, fixed_residues, minimum_aligned_ca
    )
    residuals = _transformed_alignment_residuals(
        moving_residues,
        fixed_residues,
        rotation,
        translation,
        evidence["residue_pairs"],
    )
    evidence = dict(evidence)
    evidence.update(
        {
            "moving_role": "sealed_t9_refold_target",
            "fixed_role": "frozen_t4_target",
            "ca_residual_max_angstrom": max(residuals),
            "ca_residual_median_angstrom": float(np.median(residuals)),
            "ca_residual_p95_angstrom": float(np.percentile(residuals, 95)),
            "ca_residuals_angstrom": residuals,
            "rotation_determinant": float(np.linalg.det(rotation)),
            "rotation_orthogonality_max_abs_error": float(
                np.max(np.abs(rotation.T @ rotation - np.eye(3)))
            ),
        }
    )
    return rotation, translation, evidence


def _required_atom_coordinate(residue: gemmi.Residue, name: str) -> np.ndarray:
    atoms = [atom for atom in residue if atom.name.strip() == name]
    if len(atoms) != 1:
        raise ValueError(
            f"residue {residue.name} {residue.label_seq} must contain exactly one {name}"
        )
    atom = atoms[0]
    return np.asarray([atom.pos.x, atom.pos.y, atom.pos.z], dtype=np.float64)


def build_backbone_alignment_diagnostic(
    moving_residues: Sequence[gemmi.Residue],
    fixed_residues: Sequence[gemmi.Residue],
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    """Fit exact-sequence target residues with ordered N/CA/C correspondences."""
    if chain_sequence(moving_residues) != chain_sequence(fixed_residues):
        raise ValueError("raw/frozen target sequences differ for backbone alignment")
    if len(moving_residues) != 30 or len(fixed_residues) != 30:
        raise ValueError("raw/frozen backbone alignment requires exactly 30 residues")
    moving_rows = np.asarray(
        [
            _required_atom_coordinate(residue, atom_name)
            for residue in moving_residues
            for atom_name in ("N", "CA", "C")
        ]
    )
    fixed_rows = np.asarray(
        [
            _required_atom_coordinate(residue, atom_name)
            for residue in fixed_residues
            for atom_name in ("N", "CA", "C")
        ]
    )
    pre_rmsd = float(
        np.sqrt(np.mean(np.sum((moving_rows - fixed_rows) ** 2, axis=1)))
    )
    rotation, translation, rmsd = kabsch_transform(moving_rows, fixed_rows)
    transformed = (rotation @ moving_rows.T).T + translation
    residuals = np.linalg.norm(transformed - fixed_rows, axis=1)
    ca_residuals = residuals.reshape(30, 3)[:, 1]
    return rotation, translation, {
        "algorithm": "exact_sequence_30_residue_N_CA_C_Kabsch",
        "moving_role": "sealed_t9_raw_design_target",
        "fixed_role": "frozen_t4_target",
        "matched_residue_count": 30,
        "matched_backbone_atom_count": 90,
        "atom_order_per_residue": ["N", "CA", "C"],
        "pre_alignment_rmsd_angstrom": pre_rmsd,
        "rmsd_angstrom": rmsd,
        "backbone_residual_max_angstrom": float(np.max(residuals)),
        "ca_residual_max_angstrom": float(np.max(ca_residuals)),
        "ca_residual_median_angstrom": float(np.median(ca_residuals)),
        "rotation_matrix": rotation.tolist(),
        "translation_vector_angstrom": translation.tolist(),
        "rotation_determinant": float(np.linalg.det(rotation)),
    }


def rotation_about_axis(axis: np.ndarray, degrees: float) -> np.ndarray:
    axis = np.asarray(axis, dtype=np.float64)
    norm = float(np.linalg.norm(axis))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("rotation axis is degenerate")
    unit = axis / norm
    angle = math.radians(degrees)
    cross = np.asarray(
        [
            [0.0, -unit[2], unit[1]],
            [unit[2], 0.0, -unit[0]],
            [-unit[1], unit[0], 0.0],
        ]
    )
    return np.eye(3) + math.sin(angle) * cross + (1.0 - math.cos(angle)) * (
        cross @ cross
    )


def shortest_proper_rotation(moving_axis: np.ndarray, fixed_axis: np.ndarray) -> np.ndarray:
    moving = np.asarray(moving_axis, dtype=np.float64)
    fixed = np.asarray(fixed_axis, dtype=np.float64)
    moving /= np.linalg.norm(moving)
    fixed /= np.linalg.norm(fixed)
    cross_vector = np.cross(moving, fixed)
    sine = float(np.linalg.norm(cross_vector))
    cosine = float(np.clip(np.dot(moving, fixed), -1.0, 1.0))
    if sine <= 1e-12:
        if cosine > 0:
            return np.eye(3)
        reference = np.eye(3)[int(np.argmin(np.abs(moving)))]
        axis = np.cross(moving, reference)
        axis /= np.linalg.norm(axis)
        return rotation_about_axis(axis, 180.0)
    cross = np.asarray(
        [
            [0.0, -cross_vector[2], cross_vector[1]],
            [cross_vector[2], 0.0, -cross_vector[0]],
            [-cross_vector[1], cross_vector[0], 0.0],
        ]
    )
    return np.eye(3) + cross + cross @ cross * ((1.0 - cosine) / sine**2)


def run_internal_pose_search(
    target_residues: Sequence[gemmi.Residue],
    raw_vhh_residues: Sequence[gemmi.Residue],
    base_rotation: np.ndarray,
    base_translation: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    """Enumerate the frozen 30,600-pose owner-mode rigid search exactly."""
    design_set = set(DESIGN_INDICES)
    raw_ca = _ca_coordinates(raw_vhh_residues)
    base_ca = (base_rotation @ raw_ca.T).T + base_translation
    cdr_centroid = np.mean(base_ca[np.asarray(DESIGN_INDICES) - 1], axis=0)
    framework_indices = np.asarray(
        [index - 1 for index in range(1, 122) if index not in design_set]
    )
    framework_centroid = np.mean(base_ca[framework_indices], axis=0)
    binding_axis = cdr_centroid - framework_centroid
    binding_axis /= np.linalg.norm(binding_axis)

    target_ca = _ca_coordinates(target_residues)
    hotspot_ca = target_ca[:2]
    hotspot_centroid = np.mean(hotspot_ca, axis=0)
    outward_axis = hotspot_centroid - np.mean(target_ca, axis=0)
    outward_axis /= np.linalg.norm(outward_axis)
    align_rotation = shortest_proper_rotation(binding_axis, -outward_axis)
    projected_e1 = hotspot_ca[1] - hotspot_ca[0]
    projected_e1 -= np.dot(projected_e1, outward_axis) * outward_axis
    e1 = projected_e1 / np.linalg.norm(projected_e1)
    e2 = np.cross(outward_axis, e1)
    e2 /= np.linalg.norm(e2)

    raw_heavy_rows = _heavy_atoms(raw_vhh_residues)
    raw_heavy = np.asarray([row["coordinate"] for row in raw_heavy_rows])
    base_heavy = (base_rotation @ raw_heavy.T).T + base_translation
    cdr_heavy_indices = np.asarray(
        [
            index
            for index, row in enumerate(raw_heavy_rows)
            if int(row["label_seq_id"]) in design_set
        ]
    )
    framework_heavy_indices = np.asarray(
        [
            index
            for index, row in enumerate(raw_heavy_rows)
            if int(row["label_seq_id"]) not in design_set
        ]
    )
    cdr_heavy_residue_indices = np.asarray(
        [
            int(raw_heavy_rows[index]["label_seq_id"])
            for index in cdr_heavy_indices
        ]
    )
    target_heavy = np.asarray(
        [row["coordinate"] for row in _heavy_atoms(target_residues)]
    )
    target_heavy_tree = cKDTree(target_heavy)
    target_ca_tree = cKDTree(target_ca)

    separation_values = [4.0 + 0.25 * index for index in range(17)]
    roll_values = list(range(0, 360, 5))
    tilt_values = [-20, -10, 0, 10, 20]
    evaluated_count = 0
    feasible: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for separation in separation_values:
        destination = hotspot_centroid + outward_axis * separation
        for roll in roll_values:
            roll_rotation = rotation_about_axis(-outward_axis, roll)
            for tilt_e1 in tilt_values:
                e1_rotation = rotation_about_axis(e1, tilt_e1)
                for tilt_e2 in tilt_values:
                    evaluated_count += 1
                    q_rotation = (
                        rotation_about_axis(e2, tilt_e2)
                        @ e1_rotation
                        @ roll_rotation
                        @ align_rotation
                    )
                    posed_heavy = (
                        q_rotation @ (base_heavy - cdr_centroid).T
                    ).T + destination
                    heavy_nearest = np.asarray(
                        target_heavy_tree.query(posed_heavy, k=1)[0]
                    )
                    if np.any(heavy_nearest < 2.0):
                        continue
                    posed_ca = (
                        q_rotation @ (base_ca - cdr_centroid).T
                    ).T + destination
                    ca_minimum = float(
                        np.min(target_ca_tree.query(posed_ca, k=1)[0])
                    )
                    if ca_minimum < MIN_TARGET_VHH_CA_DISTANCE_ANGSTROM:
                        continue
                    posed_cdr_ca = posed_ca[np.asarray(DESIGN_INDICES) - 1]
                    hotspot_distance_matrix = np.linalg.norm(
                        hotspot_ca[:, None, :] - posed_cdr_ca[None, :, :], axis=2
                    )
                    hotspot_minima = np.min(hotspot_distance_matrix, axis=1)
                    if not np.all(
                        (hotspot_minima >= MIN_HOTSPOT_CDR_CA_DISTANCE_ANGSTROM)
                        & (hotspot_minima < MAX_HOTSPOT_CDR_CA_DISTANCE_ANGSTROM)
                    ):
                        continue
                    heavy_minimum = float(np.min(heavy_nearest))
                    if heavy_minimum > INTERFACE_HEAVY_CONTACT_CUTOFF_ANGSTROM:
                        continue
                    posed_cdr_heavy = posed_heavy[cdr_heavy_indices]
                    cdr_neighbors = target_heavy_tree.query_ball_point(
                        posed_cdr_heavy, r=CDR_CONTACT_CUTOFF_ANGSTROM
                    )
                    cdr_contact_residues = {
                        int(cdr_heavy_residue_indices[index])
                        for index, neighbors in enumerate(cdr_neighbors)
                        if neighbors
                    }
                    if len(cdr_contact_residues) < MIN_CDR_CONTACT_RESIDUE_COUNT:
                        continue
                    posed_framework_heavy = posed_heavy[framework_heavy_indices]
                    framework_neighbors = target_heavy_tree.query_ball_point(
                        posed_framework_heavy, r=CDR_CONTACT_CUTOFF_ANGSTROM
                    )
                    framework_pair_count = sum(
                        len(neighbors) for neighbors in framework_neighbors
                    )
                    if framework_pair_count != 0:
                        continue
                    cdr_pair_count = sum(len(neighbors) for neighbors in cdr_neighbors)
                    ranking_key = (
                        float(np.max(hotspot_minima)),
                        float(np.sum(hotspot_minima)),
                        -len(cdr_contact_residues),
                        -cdr_pair_count,
                        abs(tilt_e1) + abs(tilt_e2),
                        max(abs(tilt_e1), abs(tilt_e2)),
                        abs(tilt_e1),
                        abs(tilt_e2),
                        roll,
                        separation,
                    )
                    overall_rotation = q_rotation @ base_rotation
                    overall_translation = (
                        q_rotation @ base_translation
                        - q_rotation @ cdr_centroid
                        + destination
                    )
                    feasible.append(
                        (
                            ranking_key,
                            {
                                "separation_angstrom": separation,
                                "roll_degrees": roll,
                                "tilt_e1_degrees": tilt_e1,
                                "tilt_e2_degrees": tilt_e2,
                                "hotspot_cdr_ca_distances_angstrom": (
                                    hotspot_minima.tolist()
                                ),
                                "minimum_target_vhh_heavy_atom_distance_angstrom": (
                                    heavy_minimum
                                ),
                                "minimum_target_vhh_ca_distance_angstrom": ca_minimum,
                                "cdr_contact_residue_count": len(
                                    cdr_contact_residues
                                ),
                                "cdr_contact_residue_indices_1based": sorted(
                                    cdr_contact_residues
                                ),
                                "cdr_target_heavy_atom_pairs_2_to_5_angstrom": (
                                    cdr_pair_count
                                ),
                                "framework_target_heavy_atom_pairs_2_to_5_angstrom": 0,
                                "ranking_key": list(ranking_key),
                                "overall_rotation_matrix": overall_rotation.tolist(),
                                "overall_translation_vector_angstrom": (
                                    overall_translation.tolist()
                                ),
                            },
                        )
                    )
    if evaluated_count != 30_600:
        raise ValueError(f"internal pose grid size drifted: {evaluated_count}")
    if not feasible:
        raise ValueError("frozen internal pose grid produced no hard-gate-feasible pose")
    feasible.sort(key=lambda row: row[0])
    chosen = feasible[0][1]
    rotation = np.asarray(chosen["overall_rotation_matrix"], dtype=np.float64)
    translation = np.asarray(
        chosen["overall_translation_vector_angstrom"], dtype=np.float64
    )
    receipt = {
        "mode": "internal",
        "interface_status": "FROZEN_DETERMINISTIC_GRID_COMPLETE",
        "rng_used": False,
        "evaluated_pose_count": evaluated_count,
        "feasible_pose_count": len(feasible),
        "geometry_definition": {
            "binding_axis": "CDR_CA_centroid_minus_framework_CA_centroid",
            "outward_axis": "E1_E2_CA_centroid_minus_target30_CA_centroid",
            "axis_alignment": "shortest_proper_rotation(binding_axis,-outward_axis)",
            "e1": "unit projection of E2_CA-E1_CA onto outward-orthogonal plane",
            "e2": "unit cross(outward_axis,e1)",
            "pivot": "CDR_CA_centroid",
            "destination": "E1_E2_CA_centroid + outward_axis * separation",
            "rotation_order": "Rot(e2,t2) @ Rot(e1,t1) @ Rot(-outward,roll) @ Ra",
        },
        "grid": {
            "separation_angstrom": separation_values,
            "roll_degrees": roll_values,
            "tilt_e1_degrees": tilt_values,
            "tilt_e2_degrees": tilt_values,
            "loop_order": ["separation", "roll", "tilt_e1", "tilt_e2"],
        },
        "ranking": [
            "minimize_max_hotspot_CDR_CA_distance",
            "minimize_sum_hotspot_CDR_CA_distance",
            "maximize_CDR_contact_residue_count",
            "maximize_CDR_target_heavy_atom_pair_count_2_to_5A",
            "minimize_abs_t1_plus_abs_t2",
            "minimize_max_abs_tilt",
            "minimize_abs_t1",
            "minimize_abs_t2",
            "minimize_roll",
            "minimize_separation",
            "stable_first_in_fixed_traversal_for_any_remaining_exact_tie",
        ],
        "frame_vectors": {
            "cdr_ca_centroid": cdr_centroid.tolist(),
            "framework_ca_centroid": framework_centroid.tolist(),
            "binding_axis": binding_axis.tolist(),
            "hotspot_ca_centroid": hotspot_centroid.tolist(),
            "outward_axis": outward_axis.tolist(),
            "e1": e1.tolist(),
            "e2": e2.tolist(),
            "axis_alignment_rotation": align_rotation.tolist(),
        },
        "chosen_pose": chosen,
    }
    return rotation, translation, receipt


def _heavy_atoms(
    residues: Sequence[gemmi.Residue], selected_indices: set[int] | None = None
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index, residue in enumerate(residues, 1):
        if selected_indices is not None and index not in selected_indices:
            continue
        for atom in residue:
            if atom.element.name.upper() in {"H", "D"}:
                continue
            result.append(
                {
                    "label_seq_id": index,
                    "auth_seq_id": residue.seqid.num,
                    "residue_name": residue.name,
                    "atom_name": atom.name.strip(),
                    "coordinate": np.asarray(
                        [atom.pos.x, atom.pos.y, atom.pos.z], dtype=np.float64
                    ),
                }
            )
    if not result:
        raise ValueError("heavy-atom selection is empty")
    return result


def diagnose_cross_chain_clashes(
    target_residues: Sequence[gemmi.Residue],
    vhh_residues: Sequence[gemmi.Residue],
    cutoff_angstrom: float,
) -> dict[str, object]:
    target_atoms = _heavy_atoms(target_residues)
    vhh_atoms = _heavy_atoms(vhh_residues)
    target_coordinates = np.asarray(
        [row["coordinate"] for row in target_atoms], dtype=np.float64
    )
    vhh_coordinates = np.asarray(
        [row["coordinate"] for row in vhh_atoms], dtype=np.float64
    )
    distances = np.linalg.norm(
        target_coordinates[:, None, :] - vhh_coordinates[None, :, :], axis=2
    )
    clash_indices = np.argwhere(distances < cutoff_angstrom)
    rows: list[dict[str, object]] = []
    residue_pairs: set[tuple[int, int]] = set()
    for target_index, vhh_index in clash_indices:
        target = target_atoms[int(target_index)]
        vhh = vhh_atoms[int(vhh_index)]
        residue_pairs.add(
            (int(target["label_seq_id"]), int(vhh["label_seq_id"]))
        )
        rows.append(
            {
                "distance_angstrom": float(distances[target_index, vhh_index]),
                "target_label_seq_id": int(target["label_seq_id"]),
                "target_residue_name": target["residue_name"],
                "target_atom_name": target["atom_name"],
                "vhh_label_seq_id": int(vhh["label_seq_id"]),
                "vhh_residue_name": vhh["residue_name"],
                "vhh_atom_name": vhh["atom_name"],
            }
        )
    rows.sort(
        key=lambda row: (
            row["distance_angstrom"],
            row["target_label_seq_id"],
            row["vhh_label_seq_id"],
            row["target_atom_name"],
            row["vhh_atom_name"],
        )
    )
    return {
        "definition": "cross-chain non-hydrogen atom pairs with distance < cutoff",
        "cutoff_angstrom": cutoff_angstrom,
        "target_heavy_atom_count": len(target_atoms),
        "vhh_heavy_atom_count": len(vhh_atoms),
        "atom_pair_count": len(rows),
        "residue_pair_count": len(residue_pairs),
        "minimum_distance_angstrom": float(np.min(distances)),
        "closest_clash_pairs": rows[:25],
    }


def _closest_atom_pair(
    first: Sequence[dict[str, object]], second: Sequence[dict[str, object]]
) -> tuple[float, dict[str, object], dict[str, object]]:
    first_coordinates = np.asarray([row["coordinate"] for row in first])
    second_coordinates = np.asarray([row["coordinate"] for row in second])
    distances = np.linalg.norm(
        first_coordinates[:, None, :] - second_coordinates[None, :, :], axis=2
    )
    flat_index = int(np.argmin(distances))
    first_index, second_index = np.unravel_index(flat_index, distances.shape)
    return (
        float(distances[first_index, second_index]),
        first[int(first_index)],
        second[int(second_index)],
    )


def diagnose_hotspot_distances(
    target_residues: Sequence[gemmi.Residue],
    vhh_residues: Sequence[gemmi.Residue],
) -> dict[str, object]:
    design_set = set(DESIGN_INDICES)
    design_heavy_atoms = _heavy_atoms(vhh_residues, design_set)
    design_ca_rows = [
        (index, residue_ca(vhh_residues[index - 1])) for index in DESIGN_INDICES
    ]
    if any(coordinate is None for _, coordinate in design_ca_rows):
        raise ValueError("VHH design region lacks a C-alpha atom")
    rows: list[dict[str, object]] = []
    for target_index, name, expected_residue_name in HOTSPOTS:
        target_residue = target_residues[target_index - 1]
        if target_residue.name != expected_residue_name:
            raise ValueError(
                f"hotspot {name} residue mismatch: observed={target_residue.name}"
            )
        target_heavy_atoms = _heavy_atoms(target_residues, {target_index})
        heavy_distance, target_atom, vhh_atom = _closest_atom_pair(
            target_heavy_atoms, design_heavy_atoms
        )
        target_ca = residue_ca(target_residue)
        if target_ca is None:
            raise ValueError(f"hotspot {name} lacks a C-alpha atom")
        ca_distance, vhh_ca_index = min(
            (
                float(np.linalg.norm(target_ca - coordinate)),
                index,
            )
            for index, coordinate in design_ca_rows
            if coordinate is not None
        )
        rows.append(
            {
                "hotspot": name,
                "target_label_seq_id": target_index,
                "target_residue_name": target_residue.name,
                "minimum_heavy_atom_distance_to_vhh_design_angstrom": heavy_distance,
                "target_closest_atom_name": target_atom["atom_name"],
                "vhh_closest_label_seq_id": int(vhh_atom["label_seq_id"]),
                "vhh_closest_residue_name": vhh_atom["residue_name"],
                "vhh_closest_atom_name": vhh_atom["atom_name"],
                "minimum_ca_distance_to_vhh_design_angstrom": ca_distance,
                "vhh_closest_ca_label_seq_id": vhh_ca_index,
            }
        )
    return {
        "definition": (
            "frozen target His7/Ala8 (label positions 1/2) to transformed "
            "VHH design residues only"
        ),
        "contact_reporting_cutoff_angstrom": 8.0,
        "heavy_atom_coverage_fraction_lt8": sum(
            row["minimum_heavy_atom_distance_to_vhh_design_angstrom"] < 8.0
            for row in rows
        )
        / len(rows),
        "rows": rows,
    }


def load_external_transform(
    path: Path,
) -> tuple[np.ndarray, np.ndarray, dict[str, object], BoundFile]:
    bound = read_bound_file(path)
    payload = json.loads(_require_bound_bytes(bound).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("external rigid transform must be a JSON object")
    if payload.get("schema_version") != EXTERNAL_TRANSFORM_SCHEMA:
        raise ValueError("external rigid transform schema mismatch")
    if payload.get("source_candidate_id") != ALLOWED_CANDIDATE_ID:
        raise ValueError("external rigid transform source candidate must be design_3")
    if payload.get("coordinate_frame") != "FROZEN_TARGET_RAW_BASE_VHH_DELTA":
        raise ValueError("external rigid transform coordinate_frame mismatch")
    rotation = np.asarray(payload.get("rotation_matrix"), dtype=np.float64)
    translation = np.asarray(
        payload.get("translation_vector_angstrom"), dtype=np.float64
    )
    if rotation.shape != (3, 3) or translation.shape != (3,):
        raise ValueError("external rigid transform must contain a 3x3 rotation and 3-vector")
    if not np.isfinite(rotation).all() or not np.isfinite(translation).all():
        raise ValueError("external rigid transform contains NaN or Inf")
    determinant = float(np.linalg.det(rotation))
    orthogonality_error = float(
        np.max(np.abs(rotation.T @ rotation - np.eye(3)))
    )
    if not np.isclose(determinant, 1.0, atol=1e-7) or orthogonality_error > 1e-7:
        raise ValueError("external transform rotation is not a proper rigid rotation")
    return rotation, translation, {
        "mode": "external",
        "interface_status": "EXTERNAL_TRANSFORM_LOADED",
        "path": str(bound.path),
        "sha256": bound.sha256,
        "schema_version": payload["schema_version"],
        "coordinate_frame": payload["coordinate_frame"],
        "rotation_matrix": rotation.tolist(),
        "translation_vector_angstrom": translation.tolist(),
        "rotation_determinant": determinant,
        "rotation_orthogonality_max_abs_error": orthogonality_error,
        "producer": payload.get("producer"),
        "search_receipt": payload.get("search_receipt"),
    }, bound


def _ca_coordinates(residues: Sequence[gemmi.Residue]) -> np.ndarray:
    coordinates = [residue_ca(residue) for residue in residues]
    if any(coordinate is None for coordinate in coordinates):
        raise ValueError("pose geometry requires one C-alpha atom per residue")
    return np.asarray(coordinates, dtype=np.float64)


def diagnose_pose_geometry(
    target_residues: Sequence[gemmi.Residue],
    vhh_residues: Sequence[gemmi.Residue],
    reference_vhh_residues: Sequence[gemmi.Residue],
) -> dict[str, object]:
    """Evaluate every frozen interface gate for one VHH pose."""
    design_set = set(DESIGN_INDICES)
    framework_set = set(range(1, len(vhh_residues) + 1)) - design_set
    target_atoms = _heavy_atoms(target_residues)
    vhh_atoms = _heavy_atoms(vhh_residues)
    cdr_atoms = _heavy_atoms(vhh_residues, design_set)
    framework_atoms = _heavy_atoms(vhh_residues, framework_set)
    target_coordinates = np.asarray([row["coordinate"] for row in target_atoms])
    vhh_coordinates = np.asarray([row["coordinate"] for row in vhh_atoms])
    cdr_coordinates = np.asarray([row["coordinate"] for row in cdr_atoms])
    framework_coordinates = np.asarray(
        [row["coordinate"] for row in framework_atoms]
    )
    target_tree = cKDTree(target_coordinates)

    all_heavy_nearest = np.asarray(target_tree.query(vhh_coordinates, k=1)[0])
    clash_neighbors = target_tree.query_ball_point(
        vhh_coordinates, r=DEFAULT_HEAVY_ATOM_CLASH_CUTOFF_ANGSTROM
    )
    clash_count = sum(len(neighbors) for neighbors in clash_neighbors)

    target_ca = _ca_coordinates(target_residues)
    vhh_ca = _ca_coordinates(vhh_residues)
    target_vhh_ca_distances = np.linalg.norm(
        target_ca[:, None, :] - vhh_ca[None, :, :], axis=2
    )
    hotspot_ca = target_ca[:2]
    cdr_ca = vhh_ca[np.asarray(DESIGN_INDICES) - 1]
    hotspot_cdr_distances = np.linalg.norm(
        hotspot_ca[:, None, :] - cdr_ca[None, :, :], axis=2
    )
    hotspot_minima = np.min(hotspot_cdr_distances, axis=1)
    hotspot_closest = np.argmin(hotspot_cdr_distances, axis=1)

    cdr_distance_matrix = np.linalg.norm(
        cdr_coordinates[:, None, :] - target_coordinates[None, :, :], axis=2
    )
    framework_distance_matrix = np.linalg.norm(
        framework_coordinates[:, None, :] - target_coordinates[None, :, :], axis=2
    )
    cdr_pair_mask = (
        (cdr_distance_matrix >= DEFAULT_HEAVY_ATOM_CLASH_CUTOFF_ANGSTROM)
        & (cdr_distance_matrix <= CDR_CONTACT_CUTOFF_ANGSTROM)
    )
    framework_pair_mask = (
        (framework_distance_matrix >= DEFAULT_HEAVY_ATOM_CLASH_CUTOFF_ANGSTROM)
        & (framework_distance_matrix <= CDR_CONTACT_CUTOFF_ANGSTROM)
    )
    cdr_contact_residues = sorted(
        {
            int(cdr_atoms[index]["label_seq_id"])
            for index in np.flatnonzero(np.any(cdr_pair_mask, axis=1))
        }
    )

    reference_ca = _ca_coordinates(reference_vhh_residues)
    reference_internal = np.linalg.norm(
        reference_ca[:, None, :] - reference_ca[None, :, :], axis=2
    )
    observed_internal = np.linalg.norm(
        vhh_ca[:, None, :] - vhh_ca[None, :, :], axis=2
    )
    internal_error = float(np.max(np.abs(reference_internal - observed_internal)))

    hotspot_rows = []
    for row_index, (_, hotspot_name, _) in enumerate(HOTSPOTS):
        cdr_offset = int(hotspot_closest[row_index])
        hotspot_rows.append(
            {
                "hotspot": hotspot_name,
                "target_label_seq_id": row_index + 1,
                "minimum_cdr_ca_distance_angstrom": float(
                    hotspot_minima[row_index]
                ),
                "closest_cdr_label_seq_id": DESIGN_INDICES[cdr_offset],
            }
        )
    return {
        "gate_definition": {
            "heavy_atom_clash": "target-VHH non-hydrogen atom pairs <2.0 A",
            "ca_separation": "minimum target-VHH CA distance >=3.6 A",
            "hotspot": "each E1/E2-to-CDR CA minimum is >=4.0 and <8.0 A",
            "interface": "at least one target-VHH heavy-atom distance <=4.0 A",
            "cdr_contacts": "at least three distinct CDR residues have a heavy atom <=5.0 A from target",
            "framework_exclusion": "framework-target heavy atom pairs in [2.0,5.0] A equal zero",
        },
        "target_vhh_heavy_atom_clash_count_lt2": int(clash_count),
        "minimum_target_vhh_heavy_atom_distance_angstrom": float(
            np.min(all_heavy_nearest)
        ),
        "minimum_target_vhh_ca_distance_angstrom": float(
            np.min(target_vhh_ca_distances)
        ),
        "hotspot_to_cdr_ca": hotspot_rows,
        "maximum_hotspot_to_cdr_ca_distance_angstrom": float(
            np.max(hotspot_minima)
        ),
        "sum_hotspot_to_cdr_ca_distance_angstrom": float(
            np.sum(hotspot_minima)
        ),
        "cdr_target_heavy_atom_pairs_2_to_5_angstrom": int(
            np.count_nonzero(cdr_pair_mask)
        ),
        "framework_target_heavy_atom_pairs_2_to_5_angstrom": int(
            np.count_nonzero(framework_pair_mask)
        ),
        "cdr_contact_residue_count_within_5_angstrom": len(cdr_contact_residues),
        "cdr_contact_residue_indices_1based": cdr_contact_residues,
        "vhh_internal_ca_distance_max_abs_error_angstrom": internal_error,
    }


def evaluate_pose_gates(
    base_alignment: dict[str, object], geometry: dict[str, object]
) -> dict[str, object]:
    hotspot_distances = [
        float(row["minimum_cdr_ca_distance_angstrom"])
        for row in geometry["hotspot_to_cdr_ca"]
    ]
    checks = [
        {
            "check": "raw_target_ca_kabsch_rmsd",
            "passed": float(base_alignment["rmsd_angstrom"])
            <= DEFAULT_MAX_CA_RMSD_ANGSTROM,
            "observed": float(base_alignment["rmsd_angstrom"]),
            "requirement": "<=2.0 A",
        },
        {
            "check": "raw_target_maximum_ca_residual",
            "passed": float(base_alignment["ca_residual_max_angstrom"])
            <= DEFAULT_MAX_CA_RESIDUAL_ANGSTROM,
            "observed": float(base_alignment["ca_residual_max_angstrom"]),
            "requirement": "<=5.0 A",
        },
        {
            "check": "target_vhh_heavy_atom_clashes",
            "passed": geometry["target_vhh_heavy_atom_clash_count_lt2"] == 0,
            "observed": geometry["target_vhh_heavy_atom_clash_count_lt2"],
            "requirement": "=0 pairs <2.0 A",
        },
        {
            "check": "target_vhh_ca_minimum",
            "passed": geometry["minimum_target_vhh_ca_distance_angstrom"]
            >= MIN_TARGET_VHH_CA_DISTANCE_ANGSTROM,
            "observed": geometry["minimum_target_vhh_ca_distance_angstrom"],
            "requirement": ">=3.6 A",
        },
        {
            "check": "each_hotspot_to_cdr_ca_window",
            "passed": all(
                MIN_HOTSPOT_CDR_CA_DISTANCE_ANGSTROM <= distance
                < MAX_HOTSPOT_CDR_CA_DISTANCE_ANGSTROM
                for distance in hotspot_distances
            ),
            "observed": hotspot_distances,
            "requirement": "each >=4.0 A and <8.0 A",
        },
        {
            "check": "interface_heavy_atom_contact",
            "passed": geometry[
                "minimum_target_vhh_heavy_atom_distance_angstrom"
            ]
            <= INTERFACE_HEAVY_CONTACT_CUTOFF_ANGSTROM,
            "observed": geometry[
                "minimum_target_vhh_heavy_atom_distance_angstrom"
            ],
            "requirement": "<=4.0 A",
        },
        {
            "check": "minimum_distinct_cdr_contact_residues",
            "passed": geometry["cdr_contact_residue_count_within_5_angstrom"]
            >= MIN_CDR_CONTACT_RESIDUE_COUNT,
            "observed": geometry["cdr_contact_residue_count_within_5_angstrom"],
            "requirement": ">=3 residues",
        },
        {
            "check": "framework_target_contacts_excluded",
            "passed": geometry[
                "framework_target_heavy_atom_pairs_2_to_5_angstrom"
            ]
            == 0,
            "observed": geometry[
                "framework_target_heavy_atom_pairs_2_to_5_angstrom"
            ],
            "requirement": "=0 pairs in [2.0,5.0] A",
        },
        {
            "check": "vhh_internal_ca_geometry_preserved",
            "passed": geometry[
                "vhh_internal_ca_distance_max_abs_error_angstrom"
            ]
            <= 1e-6,
            "observed": geometry[
                "vhh_internal_ca_distance_max_abs_error_angstrom"
            ],
            "requirement": "<=1e-6 A",
        },
    ]
    passed = all(bool(check["passed"]) for check in checks)
    return {
        "all_hard_gates_passed": passed,
        "checks": checks,
        "failed_checks": [
            check["check"] for check in checks if not bool(check["passed"])
        ],
    }


def output_scaffold_document() -> dict[str, object]:
    return {
        "path": "scaffold.cif",
        "include": [{"chain": {"id": "A"}}],
        "design": [
            {"chain": {"id": "A", "res_index": DESIGN_RANGE_TEXT}}
        ],
        "structure_groups": [
            {"group": {"id": "A", "visibility": 1}},
            {
                "group": {
                    "id": "A",
                    "visibility": 0,
                    "res_index": DESIGN_RANGE_TEXT,
                }
            },
        ],
        "reset_res_index": [{"chain": {"id": "A"}}],
    }


def build_scaffold_cif(
    raw_structure: gemmi.Structure,
    rotation: np.ndarray,
    translation: np.ndarray,
) -> bytes:
    scaffold = raw_structure.clone()
    source_entity = scaffold.get_entity("2")
    if source_entity is None:
        raise ValueError("raw design VHH entity 2 is missing")
    entity_type = source_entity.entity_type
    polymer_type = source_entity.polymer_type
    full_sequence = list(source_entity.full_sequence)
    model = scaffold[0]
    model.remove_chain("A")
    vhh = model.find_chain("B")
    if vhh is None or len(model) != 1:
        raise ValueError("cannot isolate raw design VHH chain B")
    apply_transform(vhh, rotation, translation)
    vhh.name = "A"
    for index, residue in enumerate(vhh, 1):
        residue.subchain = "A"
        residue.entity_id = "1"
        residue.seqid = gemmi.SeqId(index, " ")
        residue.label_seq = index
    for connection in scaffold.connections:
        connection.partner1.chain_name = "A"
        connection.partner2.chain_name = "A"
    scaffold.entities.clear()
    entity = gemmi.Entity("1")
    entity.subchains = ["A"]
    entity.entity_type = entity_type
    entity.polymer_type = polymer_type
    entity.full_sequence = full_sequence
    scaffold.entities.append(entity)
    scaffold.name = "owner_pose_anchored_design_3"
    content = scaffold.make_mmcif_document().as_string().encode("utf-8")

    reparsed = gemmi.make_structure_from_block(
        gemmi.cif.read_string(content.decode("utf-8")).sole_block()
    )
    if len(reparsed) != 1 or len(reparsed[0]) != 1 or reparsed[0][0].name != "A":
        raise ValueError("serialized scaffold is not exactly one chain A")
    residues = protein_residues(reparsed[0][0])
    validate_chain_inventory(residues, 121, "serialized pose scaffold")
    validate_vhh_disulfide(reparsed, reparsed[0][0], residues, "serialized pose scaffold")
    return content


def write_spec_bundle(
    bundle_root: Path,
    target_source: BoundFile,
    raw_structure: gemmi.Structure,
    rotation: np.ndarray,
    translation: np.ndarray,
    expected_vhh_sequence: str,
) -> dict[str, str]:
    bundle_root.mkdir(mode=0o700)
    atomic_write(bundle_root / "target.cif", _require_bound_bytes(target_source))
    atomic_write(
        bundle_root / "design.yaml",
        yaml.safe_dump(
            EXPECTED_DESIGN_DOCUMENT, sort_keys=False, allow_unicode=True
        ).encode("utf-8"),
    )
    atomic_write(
        bundle_root / "scaffold.yaml",
        yaml.safe_dump(
            output_scaffold_document(), sort_keys=False, allow_unicode=True
        ).encode("utf-8"),
    )
    atomic_write(
        bundle_root / "scaffold.cif",
        build_scaffold_cif(raw_structure, rotation, translation),
    )
    hashes = validate_bundle_closure(bundle_root)
    if stable_digest(bundle_root / "target.cif") != target_source.sha256:
        raise ValueError("materialized target.cif bytes changed")
    design = yaml.safe_load((bundle_root / "design.yaml").read_text(encoding="utf-8"))
    scaffold = yaml.safe_load(
        (bundle_root / "scaffold.yaml").read_text(encoding="utf-8")
    )
    if design != EXPECTED_DESIGN_DOCUMENT or scaffold != output_scaffold_document():
        raise ValueError("materialized YAML closure/group contract mismatch")
    scaffold_structure = load_structure(bundle_root / "scaffold.cif")
    chain, residues, mapping = resolve_spec_chain(
        scaffold_structure, "A", "materialized_scaffold"
    )
    if mapping["auth_asym_id"] != "A" or chain_sequence(residues) != expected_vhh_sequence:
        raise ValueError("materialized scaffold chain/sequence changed")
    validate_vhh_disulfide(
        scaffold_structure, chain, residues, "materialized scaffold"
    )
    return hashes


def write_spec_bundle_manifest(root: Path, hashes: dict[str, str]) -> None:
    atomic_write(
        root / "SPEC_BUNDLE.SHA256SUMS",
        "".join(
            f"{hashes[name]}  ./spec_bundle/{name}\n"
            for name in FROZEN_BUNDLE_FILES
        ).encode("utf-8"),
    )


def _strict_regular_file_closure(root: Path, excluded: set[str]) -> set[str]:
    files = regular_file_closure(root, excluded)
    for directory in [root, *[path for path in root.rglob("*") if path.is_dir()]]:
        if directory.is_symlink():
            raise ValueError(f"symlink directory is forbidden in sealed tree: {directory}")
        if directory != root and not any(directory.iterdir()):
            raise ValueError(f"empty directory is forbidden in sealed tree: {directory}")
    return files


def verify_spec_bundle_manifest_strict(root: Path) -> dict[str, str]:
    bundle = root / "spec_bundle"
    expected_paths = {f"spec_bundle/{name}" for name in FROZEN_BUNDLE_FILES}
    if set(member.name for member in bundle.iterdir()) != set(FROZEN_BUNDLE_FILES):
        raise ValueError("spec_bundle closure differs during manifest replay")
    manifest = read_bound_file(root / "SPEC_BUNDLE.SHA256SUMS")
    rows = parse_manifest_bytes(_require_bound_bytes(manifest), str(manifest.path))
    if set(rows) != expected_paths:
        raise ValueError("SPEC_BUNDLE manifest path set differs from four-file closure")
    for relative, expected in rows.items():
        current = read_bound_file(root / relative, retain_bytes=False)
        if current.sha256 != expected:
            raise ValueError(f"SPEC_BUNDLE digest replay failed: {relative}")
    if set(member.name for member in bundle.iterdir()) != set(FROZEN_BUNDLE_FILES):
        raise ValueError("spec_bundle closure changed during manifest replay")
    revalidate_bound_file(manifest)
    return rows


def verify_top_manifest_strict(root: Path) -> dict[str, str]:
    manifest = read_bound_file(root / "SHA256SUMS")
    rows = parse_manifest_bytes(_require_bound_bytes(manifest), str(manifest.path))
    observed = _strict_regular_file_closure(root, {"SHA256SUMS"})
    if set(rows) != observed:
        raise ValueError("top-level manifest closure replay mismatch")
    for relative, expected in rows.items():
        current = read_bound_file(root / relative, retain_bytes=False)
        if current.sha256 != expected:
            raise ValueError(f"top-level manifest digest replay failed: {relative}")
    if _strict_regular_file_closure(root, {"SHA256SUMS"}) != set(rows):
        raise ValueError("top-level closure changed during manifest replay")
    revalidate_bound_file(manifest)
    has_bundle = (root / "spec_bundle").exists()
    has_spec_manifest = (root / "SPEC_BUNDLE.SHA256SUMS").exists()
    if has_bundle != has_spec_manifest:
        raise ValueError("spec bundle and SPEC_BUNDLE manifest must appear together")
    if has_bundle:
        verify_spec_bundle_manifest_strict(root)
    return rows


def seal_and_verify_output(
    root: Path, *, after_manifest_write: Callable[[Path], None] | None = None
) -> dict[str, str]:
    manifest = root / "SHA256SUMS"
    if manifest.exists() or manifest.is_symlink():
        raise ValueError(f"top-level manifest already exists: {manifest}")
    observed = _strict_regular_file_closure(root, {"SHA256SUMS"})
    rows = sorted(
        ((relative, stable_digest(root / relative)) for relative in observed),
        key=lambda item: item[0].encode("utf-8"),
    )
    atomic_write(
        manifest,
        "".join(
            f"{digest}  ./{relative}\n" for relative, digest in rows
        ).encode("utf-8"),
    )
    if after_manifest_write is not None:
        after_manifest_write(root)
    return verify_top_manifest_strict(root)


def validate_boltzgen_check_cif(
    check_cif: BoundFile, target_sequence: str, vhh_sequence: str
) -> dict[str, object]:
    structure = load_structure_from_bound(check_cif)
    if len(structure[0]) != 2 or {chain.name for chain in structure[0]} != {"E", "A"}:
        raise ValueError("boltzgen check CIF must contain exactly label chains E and A")
    target = structure[0].find_chain("E")
    vhh = structure[0].find_chain("A")
    if target is None or vhh is None:
        raise ValueError("boltzgen check CIF E/A chain lookup failed")
    target_residues = protein_residues(target)
    vhh_residues = protein_residues(vhh)
    if chain_sequence(target_residues) != target_sequence or len(target_residues) != 30:
        raise ValueError("boltzgen check CIF target sequence/length differs")
    if chain_sequence(vhh_residues) != vhh_sequence or len(vhh_residues) != 121:
        raise ValueError("boltzgen check CIF VHH sequence/length differs")
    for chain_name, residues in (("E", target_residues), ("A", vhh_residues)):
        for index, residue in enumerate(residues, 1):
            if chain_name == "E":
                expected = 80.0 if index in {1, 2} else 0.0
            else:
                expected = 100.0 if index in set(DESIGN_INDICES) else 0.0
            for atom in residue:
                if not math.isclose(atom.b_iso, expected, rel_tol=0.0, abs_tol=1e-6):
                    raise ValueError(
                        f"boltzgen check B-factor flag mismatch: {chain_name}:{index}:"
                        f"{atom.name.strip()} expected={expected} observed={atom.b_iso}"
                    )
    disulfide = validate_vhh_disulfide(
        structure, vhh, vhh_residues, "boltzgen check VHH"
    )
    return {
        "check_cif_relative_path": "boltzgen_check/output/design.cif",
        "check_cif_sha256": check_cif.sha256,
        "check_cif_size_bytes": check_cif.size_bytes,
        "label_chain_ids": ["E", "A"],
        "target_length": 30,
        "vhh_length": 121,
        "binding_b_factor": 80.0,
        "binding_label_seq_ids": [1, 2],
        "design_b_factor": 100.0,
        "design_label_seq_ids": list(DESIGN_INDICES),
        "all_other_b_factors": 0.0,
        "disulfide": disulfide,
        "status": "PASS",
    }


def run_mandatory_boltzgen_check(
    staging: Path,
    launcher: BoundFile,
    moldir: BoundFile,
    timeout_seconds: float,
    target_sequence: str,
    vhh_sequence: str,
) -> dict[str, object]:
    check_root = staging / "boltzgen_check"
    check_root.mkdir(mode=0o700)
    output = check_root / "output"
    design = staging / "spec_bundle" / "design.yaml"
    command = [
        str(launcher.path),
        "check",
        str(design),
        "--output",
        str(output),
        "--moldir",
        str(moldir.path),
    ]
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
        environment.pop(name, None)
    environment.update(
        {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=staging / "spec_bundle",
            env=environment,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        atomic_write(check_root / "check.stdout.log", stdout)
        atomic_write(check_root / "check.stderr.log", stderr)
        atomic_write(check_root / "check.exit_code.txt", b"124\n")
        raise ValueError("mandatory boltzgen check timed out") from exc
    elapsed = time.monotonic() - started
    atomic_write(check_root / "check.stdout.log", result.stdout)
    atomic_write(check_root / "check.stderr.log", result.stderr)
    atomic_write(
        check_root / "check.exit_code.txt", f"{result.returncode}\n".encode("ascii")
    )
    if result.returncode != 0:
        raise ValueError(f"mandatory boltzgen check failed with exit {result.returncode}")
    if not output.is_dir() or output.is_symlink():
        raise ValueError("mandatory boltzgen check did not create a regular output directory")
    output_files = []
    for item in output.rglob("*"):
        if item.is_symlink() or (not item.is_file() and not item.is_dir()):
            raise ValueError(f"unsafe member in boltzgen check output: {item}")
        if item.is_file():
            output_files.append(item)
    if len(output_files) != 1 or output_files[0].suffix.lower() not in {".cif", ".mmcif"}:
        raise ValueError("boltzgen check output must contain exactly one CIF/mmCIF")
    if output_files[0].relative_to(output).as_posix() != "design.cif":
        raise ValueError("boltzgen check output CIF must be canonical output/design.cif")
    check_cif = read_bound_file(output_files[0])
    semantic = validate_boltzgen_check_cif(
        check_cif, target_sequence, vhh_sequence
    )
    stdout_bound = read_bound_file(check_root / "check.stdout.log")
    stderr_bound = read_bound_file(check_root / "check.stderr.log")
    exit_code_bound = read_bound_file(check_root / "check.exit_code.txt")
    evidence = {
        "schema_version": "WINDOWS_OWNER_BOLTZGEN_CHECK_V1",
        "status": "PASS",
        "argv": command,
        "cwd": str(staging / "spec_bundle"),
        "exit_code": 0,
        "elapsed_seconds": elapsed,
        "stdout_relative_path": "boltzgen_check/check.stdout.log",
        "stdout_sha256": stdout_bound.sha256,
        "stderr_relative_path": "boltzgen_check/check.stderr.log",
        "stderr_sha256": stderr_bound.sha256,
        "exit_code_relative_path": "boltzgen_check/check.exit_code.txt",
        "exit_code_sha256": exit_code_bound.sha256,
        "launcher_sha256": launcher.sha256,
        "moldir_sha256": moldir.sha256,
        "semantic_validation": semantic,
    }
    atomic_write(
        check_root / "check.execution.json",
        (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return evidence


def validate_terminal_boltzgen_check_artifacts(
    root: Path,
    receipt: dict[str, object],
    top_manifest_rows: dict[str, str],
    target_sequence: str,
    vhh_sequence: str,
) -> dict[str, object]:
    """Re-bind every sealed check artifact to both evidence copies and SHA256SUMS."""
    receipt_check = receipt.get("boltzgen_check")
    if not isinstance(receipt_check, dict):
        raise ValueError("sealed receipt lacks boltzgen check evidence")

    execution_path = "boltzgen_check/check.execution.json"
    execution_bound = read_bound_file(root / execution_path)
    if execution_bound.sha256 != top_manifest_rows.get(execution_path):
        raise ValueError("terminal check.execution.json/top-manifest SHA mismatch")
    try:
        execution = json.loads(_require_bound_bytes(execution_bound).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("sealed check.execution.json is not canonical JSON") from exc
    if not isinstance(execution, dict) or execution != receipt_check:
        raise ValueError("sealed check.execution.json differs from receipt evidence")

    receipt_semantic = receipt_check.get("semantic_validation")
    execution_semantic = execution.get("semantic_validation")
    if not isinstance(receipt_semantic, dict) or not isinstance(
        execution_semantic, dict
    ):
        raise ValueError("sealed boltzgen check semantic evidence is missing")

    artifact_contracts = (
        (
            "check CIF",
            receipt_semantic,
            execution_semantic,
            "check_cif_relative_path",
            "check_cif_sha256",
            "boltzgen_check/output/design.cif",
        ),
        (
            "stdout",
            receipt_check,
            execution,
            "stdout_relative_path",
            "stdout_sha256",
            "boltzgen_check/check.stdout.log",
        ),
        (
            "stderr",
            receipt_check,
            execution,
            "stderr_relative_path",
            "stderr_sha256",
            "boltzgen_check/check.stderr.log",
        ),
        (
            "exit code",
            receipt_check,
            execution,
            "exit_code_relative_path",
            "exit_code_sha256",
            "boltzgen_check/check.exit_code.txt",
        ),
    )
    artifact_bounds: dict[str, BoundFile] = {}
    for (
        label,
        receipt_holder,
        execution_holder,
        path_key,
        sha_key,
        canonical_path,
    ) in artifact_contracts:
        if (
            receipt_holder.get(path_key) != canonical_path
            or execution_holder.get(path_key) != canonical_path
        ):
            raise ValueError(f"terminal {label} evidence path is not canonical")
        bound = read_bound_file(root / canonical_path)
        artifact_bounds[canonical_path] = bound
        if not (
            bound.sha256
            == receipt_holder.get(sha_key)
            == execution_holder.get(sha_key)
            == top_manifest_rows.get(canonical_path)
        ):
            raise ValueError(
                f"terminal check artifact SHA cross-bind failed: {canonical_path}"
            )

    exit_bound = artifact_bounds["boltzgen_check/check.exit_code.txt"]
    if (
        receipt_check.get("exit_code") != 0
        or execution.get("exit_code") != 0
        or _require_bound_bytes(exit_bound) != b"0\n"
    ):
        raise ValueError("terminal boltzgen check exit code is not canonical zero")

    fresh_semantic = validate_boltzgen_check_cif(
        artifact_bounds["boltzgen_check/output/design.cif"],
        target_sequence,
        vhh_sequence,
    )
    if fresh_semantic != receipt_semantic or fresh_semantic != execution_semantic:
        raise ValueError("terminal check CIF semantic replay differs from evidence")

    replayed_rows = verify_top_manifest_strict(root)
    if replayed_rows != top_manifest_rows:
        raise ValueError("top manifest changed during terminal check validation")
    revalidate_bound_inputs([execution_bound, *artifact_bounds.values()])
    return fresh_semantic


def assert_source_target_cross_binding(
    receipt: dict[str, object],
    target_source: BoundFile,
    root: Path,
    spec_manifest_rows: dict[str, str],
) -> None:
    actual = stable_digest(root / "spec_bundle" / "target.cif")
    manifest_sha = spec_manifest_rows.get("spec_bundle/target.cif")
    source_rows = [
        row
        for row in receipt.get("source_files", [])
        if isinstance(row, dict) and row.get("path") == str(target_source.path)
    ]
    if len(source_rows) != 1:
        raise ValueError("receipt must contain exactly one frozen target source row")
    receipt_sha = source_rows[0].get("sha256")
    if not (
        actual == manifest_sha == receipt_sha == target_source.sha256
    ):
        raise ValueError("source/receipt/copied-target/SPEC manifest SHA cross-bind failed")


def evaluate_safety(
    alignment: dict[str, object],
    clashes: dict[str, object],
    *,
    max_ca_rmsd_angstrom: float,
    max_ca_residual_angstrom: float,
    max_heavy_atom_clash_count: int,
) -> dict[str, object]:
    checks = [
        {
            "check": "ca_kabsch_rmsd",
            "observed": float(alignment["rmsd_angstrom"]),
            "operator": "<=",
            "threshold": max_ca_rmsd_angstrom,
        },
        {
            "check": "maximum_ca_residual",
            "observed": float(alignment["ca_residual_max_angstrom"]),
            "operator": "<=",
            "threshold": max_ca_residual_angstrom,
        },
        {
            "check": "cross_chain_heavy_atom_clash_count",
            "observed": int(clashes["atom_pair_count"]),
            "operator": "<=",
            "threshold": max_heavy_atom_clash_count,
        },
    ]
    for check in checks:
        check["passed"] = check["observed"] <= check["threshold"]
    safe = all(bool(check["passed"]) for check in checks)
    return {
        "safe_for_full_chain_rigid_transfer": safe,
        "runnable_spec_generation_authorized": False,
        "runnable_spec_generated": False,
        "decision": (
            "DIAGNOSTIC_PASS_BUT_BUNDLE_GENERATION_PAUSED"
            if safe
            else "REJECT_UNSAFE_FULL_CHAIN_TRANSFER"
        ),
        "checks": checks,
        "failed_checks": [
            check["check"] for check in checks if not bool(check["passed"])
        ],
        "next_method_boundary": (
            "Use a separately reviewed local or search-based pose method; this "
            "receipt does not authorize threshold overrides or BoltzGen execution."
        ),
    }


def _git_identity(repo_root: Path) -> dict[str, object]:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    return {
        "repo_root": str(repo_root),
        "repo_commit": git("rev-parse", "HEAD"),
        "repo_tree": git("rev-parse", "HEAD^{tree}"),
        "worktree_clean": not bool(git("status", "--short")),
        "builder_path": str(Path(__file__).resolve()),
        "builder_sha256": stable_digest(Path(__file__).resolve()),
    }


def _source_file_row(bound: BoundFile) -> dict[str, object]:
    return {
        "path": str(bound.path),
        "sha256": bound.sha256,
        "size_bytes": bound.size_bytes,
        "stat_identity": list(bound.identity),
    }


def collect_diagnostic(
    arguments: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, object]]:
    validate_policy(arguments)
    spec_root = require_input_directory(arguments.spec_bundle, "frozen spec bundle")
    anchor_root = require_input_directory(arguments.anchor_set, "T9 anchor set")
    launcher_bound = read_bound_file(arguments.boltzgen_launcher, retain_bytes=False)
    if not os.access(launcher_bound.path, os.X_OK):
        raise ValueError(f"boltzgen launcher is not executable: {launcher_bound.path}")
    moldir_bound = read_bound_file(arguments.moldir, retain_bytes=False)
    spec_bounds = capture_bundle(spec_root)
    bundle_hashes = {name: bound.sha256 for name, bound in spec_bounds.items()}
    spec_contract = validate_frozen_spec_contract(spec_root, spec_bounds)

    anchor_manifest, anchor_bounds, anchor_manifest_bound = capture_manifest_tree(
        anchor_root
    )
    anchor_payload, anchor = select_design3_anchor(
        anchor_bounds["ANCHOR_SET.json"], anchor_manifest
    )
    rank = anchor.get("final_rank")
    source_bounds: dict[str, BoundFile] = {}
    for logical_name in ("raw_design.cif", "refolded.cif"):
        relative = f"anchors/rank{rank:02d}_{ALLOWED_CANDIDATE_ID}/{logical_name}"
        source_bounds[logical_name] = anchor_bounds[relative]

    for name, actual in bundle_hashes.items():
        expected = anchor_manifest.get(f"inputs/spec_bundle/{name}")
        if expected is None or actual != expected:
            raise ValueError(f"frozen spec file does not match the sealed T9 snapshot: {name}")

    target_structure = load_structure_from_bound(spec_bounds["target.cif"])
    scaffold_structure = load_structure_from_bound(spec_bounds["scaffold.cif"])
    raw_structure = load_structure_from_bound(source_bounds["raw_design.cif"])
    refold_structure = load_structure_from_bound(source_bounds["refolded.cif"])
    if len(target_structure[0]) != 1 or len(scaffold_structure[0]) != 1:
        raise ValueError("frozen target and scaffold must each contain exactly one chain")

    frozen_target_chain, frozen_target_residues, target_mapping = resolve_spec_chain(
        target_structure, "E", "frozen_target"
    )
    frozen_scaffold_chain, frozen_scaffold_residues, scaffold_mapping = (
        resolve_spec_chain(scaffold_structure, "A", "frozen_scaffold")
    )
    for role, structure in (("raw design", raw_structure), ("refold", refold_structure)):
        if len(structure[0]) != 2 or {chain.name for chain in structure[0]} != {
            "A",
            "B",
        }:
            raise ValueError(f"sealed T9 {role} must contain exactly target A and VHH B")
    raw_target_chain = raw_structure[0].find_chain("A")
    raw_vhh_chain = raw_structure[0].find_chain("B")
    refold_target_chain = refold_structure[0].find_chain("A")
    refold_vhh_chain = refold_structure[0].find_chain("B")
    if any(
        chain is None
        for chain in (
            raw_target_chain,
            raw_vhh_chain,
            refold_target_chain,
            refold_vhh_chain,
        )
    ):
        raise ValueError("sealed T9 target/VHH chain lookup failed")
    assert raw_target_chain is not None and raw_vhh_chain is not None
    assert refold_target_chain is not None and refold_vhh_chain is not None
    raw_target_residues = protein_residues(raw_target_chain)
    raw_vhh_residues = protein_residues(raw_vhh_chain)
    refold_target_residues = protein_residues(refold_target_chain)
    refold_vhh_residues = protein_residues(refold_vhh_chain)

    frozen_target_atoms = validate_chain_inventory(
        frozen_target_residues, 30, "frozen target"
    )
    raw_target_atoms = validate_chain_inventory(
        raw_target_residues, 30, "T9 raw target"
    )
    frozen_scaffold_atoms = validate_chain_inventory(
        frozen_scaffold_residues, 121, "frozen scaffold"
    )
    raw_vhh_atoms = validate_chain_inventory(
        raw_vhh_residues, 121, "T9 raw VHH"
    )

    frozen_target_sequence = chain_sequence(frozen_target_residues)
    raw_target_sequence = chain_sequence(raw_target_residues)
    frozen_scaffold_sequence = chain_sequence(frozen_scaffold_residues)
    raw_vhh_sequence = chain_sequence(raw_vhh_residues)
    refold_vhh_sequence = chain_sequence(refold_vhh_residues)
    if frozen_target_sequence != raw_target_sequence:
        raise ValueError("frozen/T9 raw target sequences differ")
    if frozen_target_atoms != raw_target_atoms:
        raise ValueError("frozen/T9 raw target residue atom inventories differ")
    design_set = set(DESIGN_INDICES)
    for index in range(1, 122):
        if index in design_set:
            continue
        if (
            frozen_scaffold_residues[index - 1].name
            != raw_vhh_residues[index - 1].name
            or frozen_scaffold_atoms[index - 1] != raw_vhh_atoms[index - 1]
        ):
            raise ValueError(
                f"T9 VHH framework residue/atom inventory changed at label position {index}"
            )
    if anchor.get("metrics", {}).get("designed_chain_sequence") != refold_vhh_sequence:
        raise ValueError("T9 anchor metric/VHH sequence mismatch")

    frozen_disulfide = validate_vhh_disulfide(
        scaffold_structure,
        frozen_scaffold_chain,
        frozen_scaffold_residues,
        "frozen scaffold",
    )
    raw_disulfide = validate_vhh_disulfide(
        raw_structure, raw_vhh_chain, raw_vhh_residues, "T9 raw VHH"
    )
    refold_disulfide = validate_vhh_disulfide(
        refold_structure, refold_vhh_chain, refold_vhh_residues, "T9 refold VHH"
    )

    base_rotation, base_translation, alignment = build_backbone_alignment_diagnostic(
        raw_target_residues, frozen_target_residues
    )
    base_vhh_chain = raw_vhh_chain.clone()
    apply_transform(base_vhh_chain, base_rotation, base_translation)
    base_vhh_residues = protein_residues(base_vhh_chain)

    refold_rotation, refold_translation, refold_alignment = build_alignment_diagnostic(
        refold_target_residues,
        frozen_target_residues,
        arguments.minimum_aligned_ca,
    )
    transformed_refold_vhh = refold_vhh_chain.clone()
    apply_transform(transformed_refold_vhh, refold_rotation, refold_translation)
    refold_clashes = diagnose_cross_chain_clashes(
        frozen_target_residues,
        protein_residues(transformed_refold_vhh),
        arguments.heavy_atom_clash_cutoff_angstrom,
    )
    refold_safety = evaluate_safety(
        refold_alignment,
        refold_clashes,
        max_ca_rmsd_angstrom=arguments.max_ca_rmsd_angstrom,
        max_ca_residual_angstrom=arguments.max_ca_residual_angstrom,
        max_heavy_atom_clash_count=arguments.max_heavy_atom_clash_count,
    )
    additional_input_bounds: list[BoundFile] = []
    if arguments.pose_search_mode == "internal":
        overall_rotation, overall_translation, search_receipt = (
            run_internal_pose_search(
                frozen_target_residues,
                raw_vhh_residues,
                base_rotation,
                base_translation,
            )
        )
    elif arguments.pose_search_mode == "external":
        (
            delta_rotation,
            delta_translation,
            search_receipt,
            external_transform_bound,
        ) = load_external_transform(arguments.external_transform_json)
        additional_input_bounds.append(external_transform_bound)
        overall_rotation = delta_rotation @ base_rotation
        overall_translation = delta_rotation @ base_translation + delta_translation
    else:
        overall_rotation = base_rotation
        overall_translation = base_translation
        search_receipt = {
            "mode": "none",
            "interface_status": "RAW_BASE_POSE_ONLY",
            "rng_used": False,
        }
    posed_vhh_chain = raw_vhh_chain.clone()
    apply_transform(posed_vhh_chain, overall_rotation, overall_translation)
    posed_vhh_residues = protein_residues(posed_vhh_chain)
    pose_geometry = diagnose_pose_geometry(
        frozen_target_residues, posed_vhh_residues, base_vhh_residues
    )
    pose_gates = evaluate_pose_gates(alignment, pose_geometry)
    materialize = (
        arguments.pose_search_mode in {"internal", "external"}
        and pose_gates["all_hard_gates_passed"]
    )
    if materialize:
        status = "POSE_ANCHORED_SPEC_CANDIDATE"
    elif arguments.pose_search_mode == "none":
        status = "RAW_BASE_POSE_REJECTED_SEARCH_REQUIRED"
    else:
        status = "POSE_TRANSFER_REJECTED_UNSAFE"

    source_files = [_source_file_row(spec_bounds[name]) for name in FROZEN_BUNDLE_FILES]
    source_files.extend(
        [
            _source_file_row(source_bounds["raw_design.cif"]),
            _source_file_row(source_bounds["refolded.cif"]),
            _source_file_row(anchor_bounds["ANCHOR_SET.json"]),
            _source_file_row(anchor_manifest_bound),
        ]
    )
    repo_root = Path(__file__).resolve().parents[4]
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "authority": "WINDOWS_CODEX",
        "scope": "DEVELOPMENT_ONLY",
        "formal_gate_claimed": False,
        "training_performed": False,
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
        "generation_mode": "STAGED_CANDIDATE" if materialize else "DIAGNOSTIC_ONLY",
        "source_candidate_id": ALLOWED_CANDIDATE_ID,
        "source_anchor_set_schema_version": anchor_payload.get("schema_version"),
        "source_files": source_files,
        "frozen_spec_contract": spec_contract,
        "chain_id_mappings": {
            "frozen_target": target_mapping,
            "frozen_scaffold": scaffold_mapping,
            "sealed_raw_target": {
                "auth_asym_id": raw_target_chain.name,
                "label_asym_ids": sorted(
                    {residue.subchain for residue in raw_target_residues}
                ),
            },
            "sealed_raw_vhh": {
                "auth_asym_id": raw_vhh_chain.name,
                "label_asym_ids": sorted(
                    {residue.subchain for residue in raw_vhh_residues}
                ),
            },
            "disabled_refold_target": {
                "auth_asym_id": refold_target_chain.name,
                "label_asym_ids": sorted(
                    {residue.subchain for residue in refold_target_residues}
                ),
            },
            "disabled_refold_vhh": {
                "auth_asym_id": refold_vhh_chain.name,
                "label_asym_ids": sorted(
                    {residue.subchain for residue in refold_vhh_residues}
                ),
            },
        },
        "sequence_contract": {
            "target_sequence": frozen_target_sequence,
            "target_length": len(frozen_target_sequence),
            "frozen_scaffold_sequence": frozen_scaffold_sequence,
            "sealed_design_3_raw_vhh_sequence": raw_vhh_sequence,
            "vhh_length": len(raw_vhh_sequence),
            "framework_identity_outside_design_region": True,
            "design_residue_indices_1based": list(DESIGN_INDICES),
            "design_residue_count": len(DESIGN_INDICES),
        },
        "disulfide_contract": {
            "frozen_scaffold": frozen_disulfide,
            "sealed_design_3_raw": raw_disulfide,
            "disabled_design_3_refold": refold_disulfide,
        },
        "alignment": alignment,
        "refold_route": {
            "enabled": False,
            "reason": "unsafe target fit and cross-chain clashes after full-chain transfer",
            "alignment": refold_alignment,
            "cross_chain_heavy_atom_clashes": refold_clashes,
            "safety_decision": refold_safety,
        },
        "pose_search": search_receipt,
        "pose_geometry": pose_geometry,
        "pose_gates": pose_gates,
        "overall_raw_to_frozen_pose_transform": {
            "rotation_matrix": overall_rotation.tolist(),
            "translation_vector_angstrom": overall_translation.tolist(),
        },
        "target_bytes_unchanged_required": True,
        "runner_input": None,
        "runtime_check_contract": {
            "boltzgen_launcher": _source_file_row(launcher_bound),
            "moldir": _source_file_row(moldir_bound),
            "check_timeout_seconds": arguments.check_timeout_seconds,
            "implicit_launcher_or_moldir_forbidden": True,
        },
        "builder_code_identity": _git_identity(repo_root),
        "replay": {
            "program": str(Path(__file__).resolve()),
            "arguments": {
                "spec_bundle": str(spec_root),
                "anchor_set": str(anchor_root),
                "candidate_id": ALLOWED_CANDIDATE_ID,
                "pose_search_mode": arguments.pose_search_mode,
                "boltzgen_launcher": str(launcher_bound.path),
                "moldir": str(moldir_bound.path),
                "check_timeout_seconds": arguments.check_timeout_seconds,
                "external_transform_json": (
                    str(arguments.external_transform_json.resolve())
                    if arguments.external_transform_json is not None
                    else None
                ),
                "minimum_aligned_ca": arguments.minimum_aligned_ca,
                "max_ca_rmsd_angstrom": arguments.max_ca_rmsd_angstrom,
                "max_ca_residual_angstrom": arguments.max_ca_residual_angstrom,
                "heavy_atom_clash_cutoff_angstrom": (
                    arguments.heavy_atom_clash_cutoff_angstrom
                ),
                "max_heavy_atom_clash_count": (
                    arguments.max_heavy_atom_clash_count
                ),
            },
            "output_argument_must_be_a_new_path": True,
        },
    }
    context = {
        "materialize": materialize,
        "target_source": spec_bounds["target.cif"],
        "raw_structure": raw_structure,
        "rotation": overall_rotation,
        "translation": overall_translation,
        "vhh_sequence": raw_vhh_sequence,
        "target_sequence": frozen_target_sequence,
        "launcher": launcher_bound,
        "moldir": moldir_bound,
        "check_timeout_seconds": arguments.check_timeout_seconds,
        "input_bounds": [
            *spec_bounds.values(),
            *anchor_bounds.values(),
            anchor_manifest_bound,
            launcher_bound,
            moldir_bound,
            *additional_input_bounds,
        ],
        "input_closures": [
            (spec_root, set(), set(FROZEN_BUNDLE_FILES)),
            (anchor_root, {"SHA256SUMS"}, set(anchor_manifest)),
        ],
    }
    return receipt, context


def cleanup_owned_staging(staging: Path, output_parent: Path) -> None:
    if not staging.exists() and not staging.is_symlink():
        return
    parent = output_parent.resolve(strict=True)
    if staging.parent.resolve(strict=True) != parent:
        raise ValueError(f"refusing to clean staging outside output parent: {staging}")
    if not staging.name.startswith(".") or ".staging." not in staging.name:
        raise ValueError(f"refusing to clean non-staging path: {staging}")
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError(f"refusing to clean unsafe staging path: {staging}")
    shutil.rmtree(staging)


def publish_diagnostic(
    output: Path, receipt: dict[str, object], context: dict[str, object]
) -> None:
    ensure_output_absent(output)
    output_parent = output.absolute().parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging = output_parent / f".{output.name}.staging.{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        raise FileExistsError(f"staging path already exists: {staging}")
    staging.mkdir(mode=0o700)
    try:
        if context["materialize"]:
            bundle_hashes = write_spec_bundle(
                staging / "spec_bundle",
                context["target_source"],
                context["raw_structure"],
                context["rotation"],
                context["translation"],
                context["vhh_sequence"],
            )
            write_spec_bundle_manifest(staging, bundle_hashes)
            spec_rows = verify_spec_bundle_manifest_strict(staging)
            check_evidence = run_mandatory_boltzgen_check(
                staging,
                context["launcher"],
                context["moldir"],
                context["check_timeout_seconds"],
                context["target_sequence"],
                context["vhh_sequence"],
            )
            # The external checker is not trusted to leave its inputs untouched.
            spec_rows = verify_spec_bundle_manifest_strict(staging)
            receipt["boltzgen_check"] = check_evidence
            receipt["publication_bindings"] = {
                "source_target_sha256": context["target_source"].sha256,
                "copied_target_sha256": stable_digest(
                    staging / "spec_bundle" / "target.cif"
                ),
                "spec_manifest_target_sha256": spec_rows[
                    "spec_bundle/target.cif"
                ],
                "spec_bundle_manifest_sha256": stable_digest(
                    staging / "SPEC_BUNDLE.SHA256SUMS"
                ),
            }
            assert_source_target_cross_binding(
                receipt, context["target_source"], staging, spec_rows
            )
            receipt["status"] = "POSE_ANCHORED_SPEC_READY"
            receipt["generation_mode"] = "RUNNABLE_SPEC"
            receipt["runner_input"] = "spec_bundle/design.yaml"
        atomic_write(
            staging / "POSE_ANCHORED_SPEC.json",
            (
                json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8"),
        )
        atomic_write(
            staging / "STATUS.txt", (str(receipt["status"]) + "\n").encode()
        )
        seal_and_verify_output(staging)
        # Terminal validation is deliberately after all output bytes/manifests exist.
        revalidate_bound_inputs(context["input_bounds"])
        revalidate_input_closures(context["input_closures"])
        final_top_rows = verify_top_manifest_strict(staging)
        if context["materialize"]:
            final_spec_rows = verify_spec_bundle_manifest_strict(staging)
            receipt_bound = read_bound_file(staging / "POSE_ANCHORED_SPEC.json")
            disk_receipt = json.loads(
                _require_bound_bytes(receipt_bound).decode("utf-8")
            )
            if disk_receipt != receipt:
                raise ValueError("sealed receipt bytes differ from in-memory receipt")
            disk_check = json.loads(
                _require_bound_bytes(
                    read_bound_file(staging / "boltzgen_check/check.execution.json")
                ).decode("utf-8")
            )
            if disk_check != disk_receipt.get("boltzgen_check"):
                raise ValueError("sealed check evidence differs from receipt")
            validate_terminal_boltzgen_check_artifacts(
                staging,
                disk_receipt,
                final_top_rows,
                context["target_sequence"],
                context["vhh_sequence"],
            )
            if _require_bound_bytes(read_bound_file(staging / "STATUS.txt")) != (
                str(disk_receipt["status"]) + "\n"
            ).encode("utf-8"):
                raise ValueError("sealed STATUS does not match sealed receipt")
            assert_source_target_cross_binding(
                disk_receipt, context["target_source"], staging, final_spec_rows
            )
            revalidate_bound_file(receipt_bound)
            if verify_top_manifest_strict(staging) != final_top_rows:
                raise ValueError("top manifest changed before atomic publication")
        publish_directory_no_replace(staging, output.absolute())
    except BaseException:
        cleanup_owned_staging(staging, output_parent)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        ensure_output_absent(arguments.output)
        receipt, context = collect_diagnostic(arguments)
        publish_diagnostic(arguments.output, receipt, context)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    summary = {
        "status": receipt["status"],
        "output": str(arguments.output.absolute()),
        "runnable_spec_generated": receipt["runner_input"] is not None,
        "backbone_rmsd_angstrom": receipt["alignment"]["rmsd_angstrom"],
        "ca_max_residual_angstrom": receipt["alignment"][
            "ca_residual_max_angstrom"
        ],
        "heavy_atom_clash_count": receipt["pose_geometry"][
            "target_vhh_heavy_atom_clash_count_lt2"
        ],
        "hard_gates_passed": receipt["pose_gates"]["all_hard_gates_passed"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "POSE_ANCHORED_SPEC_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
