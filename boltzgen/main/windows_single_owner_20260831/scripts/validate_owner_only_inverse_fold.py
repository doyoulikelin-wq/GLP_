#!/usr/bin/env python3
"""Validate a fixed-pose, inverse-fold-only BoltzGen owner run.

This is deliberately independent from the frozen T8 cell validator: native
``--only_inverse_fold`` has no design stage and writes its candidates directly
to ``intermediate_designs``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import stat
import sys
import zlib
from pathlib import Path
from typing import Any, Iterable, Sequence

import gemmi
import numpy as np
import yaml

_BUILDER_PATH = Path(__file__).with_name("build_owner_pose_anchored_spec.py")
_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "owner_pose_builder_for_only_inverse_validation", _BUILDER_PATH
)
if _BUILDER_SPEC is None or _BUILDER_SPEC.loader is None:
    raise RuntimeError(f"cannot load pose-builder dependency: {_BUILDER_PATH}")
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)

DESIGN_INDICES = _BUILDER.DESIGN_INDICES
_require_bound_bytes = _BUILDER._require_bound_bytes
chain_sequence = _BUILDER.chain_sequence
load_structure_from_bound = _BUILDER.load_structure_from_bound
protein_residues = _BUILDER.protein_residues
read_bound_file = _BUILDER.read_bound_file
resolve_spec_chain = _BUILDER.resolve_spec_chain
validate_chain_inventory = _BUILDER.validate_chain_inventory
validate_vhh_disulfide = _BUILDER.validate_vhh_disulfide
verify_spec_bundle_manifest_strict = _BUILDER.verify_spec_bundle_manifest_strict
verify_top_manifest_strict = _BUILDER.verify_top_manifest_strict
parse_manifest_bytes = _BUILDER.parse_manifest_bytes


SCHEMA = "WINDOWS_OWNER_ONLY_INVERSE_FOLD_VALIDATION_V1"
GENERATION_MODE = "ONLY_INVERSE_FOLD_FROM_POSE_SPEC"
EXPECTED_STEPS = ("inverse_folding", "folding", "analysis", "filtering")
EXPECTED_SPEC_FILES = {"design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"}
DESIGN_SET = frozenset(DESIGN_INDICES)
DESIGN_RANGE_TEXT = "26..33,51..57,96..110"
EXPECTED_DESIGN_DOCUMENT = {
    "entities": [
        {
            "file": {
                "path": "target.cif",
                "include": [
                    {"chain": {"id": "E", "res_index": "1..30"}},
                ],
                "binding_types": [
                    {"chain": {"id": "E", "binding": "1..2"}},
                ],
                "structure_groups": [
                    {"group": {"id": "E", "visibility": 1}},
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
        {"chain": {"id": "A", "res_index": DESIGN_RANGE_TEXT}},
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
EXPECTED_PUBLICATION_BINDING_KEYS = {
    "copied_target_sha256",
    "source_target_sha256",
    "spec_bundle_manifest_sha256",
    "spec_manifest_target_sha256",
}
BACKBONE_ATOMS = ("N", "CA", "C", "O")
COORDINATE_ATOL = 5.1e-5
REQUIRED_METADATA = {
    "design_mask", "mol_type", "ss_type", "token_resolved_mask", "binding_type"
}
PER_SAMPLE_KEYS = (
    "iptm", "ptm", "design_to_target_iptm", "design_ptm",
    "min_design_to_target_pae", "min_interaction_pae",
)


def _regular_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def secure_bound(path: Path):
    """Capture a regular, single-link file and bind bytes to its identity."""
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"required member is not a regular file: {path}")
    if info.st_nlink != 1:
        raise ValueError(f"hard-linked input/output is forbidden: {path}")
    bound = read_bound_file(path)
    current = path.lstat()
    if _regular_identity(info) != _regular_identity(current):
        raise ValueError(f"file identity changed while captured: {path}")
    return bound


def secure_tree_snapshot(root: Path) -> dict[str, tuple[int, ...]]:
    """Reject links/special members and snapshot every entry for terminal replay."""
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError(f"tree root must be absolute, canonical, and non-symlink: {root}")
    snapshot: dict[str, tuple[int, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            raise ValueError(f"symlink is forbidden: {path}")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise ValueError(f"hard-linked member is forbidden: {path}")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"special tree member is forbidden: {path}")
        snapshot[relative] = _regular_identity(info)
    return snapshot


def stable_sha256(path: Path) -> str:
    return secure_bound(path).sha256


def _json(bound) -> dict[str, Any]:
    value = json.loads(_require_bound_bytes(bound).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {bound.path}")
    return value


def _yaml(bound) -> dict[str, Any]:
    value = yaml.safe_load(_require_bound_bytes(bound).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"YAML root is not an object: {bound.path}")
    return value


def _chain_backbone(residues: Sequence[gemmi.Residue], role: str) -> dict[tuple[int, str], np.ndarray]:
    result: dict[tuple[int, str], np.ndarray] = {}
    for position, residue in enumerate(residues, 1):
        for name in BACKBONE_ATOMS:
            atoms = [atom for atom in residue if atom.name.strip() == name]
            if len(atoms) != 1:
                raise ValueError(f"{role} {position} requires exactly one {name} atom")
            atom = atoms[0]
            result[(position, name)] = np.asarray(
                [atom.pos.x, atom.pos.y, atom.pos.z], dtype=np.float64
            )
    return result


def _source_contract(spec_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    spec_path = spec_path.expanduser()
    if not spec_path.is_absolute() or spec_path.name != "design.yaml":
        raise ValueError("SPEC must be an absolute spec_bundle/design.yaml path")
    if spec_path.is_symlink() or spec_path.resolve(strict=True) != spec_path:
        raise ValueError("SPEC must be canonical and non-symlink")
    bundle = spec_path.parent
    pose_root = bundle.parent
    before = secure_tree_snapshot(pose_root)
    if bundle.name != "spec_bundle":
        raise ValueError("SPEC parent must be named spec_bundle")
    observed_bundle = {member.name for member in bundle.iterdir()}
    if observed_bundle != EXPECTED_SPEC_FILES:
        raise ValueError("sealed spec bundle is not the exact four-file closure")

    top_manifest_bound = secure_bound(pose_root / "SHA256SUMS")
    spec_manifest_bound = secure_bound(pose_root / "SPEC_BUNDLE.SHA256SUMS")
    top_rows = verify_top_manifest_strict(pose_root)
    spec_rows = verify_spec_bundle_manifest_strict(pose_root)
    captured_top_rows = parse_manifest_bytes(
        _require_bound_bytes(top_manifest_bound), str(top_manifest_bound.path)
    )
    captured_spec_rows = parse_manifest_bytes(
        _require_bound_bytes(spec_manifest_bound), str(spec_manifest_bound.path)
    )
    if top_rows != captured_top_rows:
        raise ValueError("top manifest changed while replayed")
    if spec_rows != captured_spec_rows:
        raise ValueError("SPEC_BUNDLE manifest changed while replayed")
    receipt_bound = secure_bound(pose_root / "POSE_ANCHORED_SPEC.json")
    receipt = _json(receipt_bound)
    required = {
        "schema_version": "WINDOWS_OWNER_POSE_ANCHORED_SPEC_V1",
        "status": "POSE_ANCHORED_SPEC_READY",
        "generation_mode": "RUNNABLE_SPEC",
        "runner_input": "spec_bundle/design.yaml",
        "source_candidate_id": "design_3",
        "formal_gate_claimed": False,
        "training_performed": False,
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
    }
    for key, expected in required.items():
        if receipt.get(key) != expected:
            raise ValueError(f"sealed pose receipt mismatch: {key}")
    if receipt.get("scope") != "DEVELOPMENT_ONLY":
        raise ValueError("sealed pose receipt is not development-only")
    if receipt.get("pose_search", {}).get("mode") not in {"external", "internal"}:
        raise ValueError("sealed pose receipt has no accepted deterministic pose search")
    if receipt.get("pose_gates", {}).get("all_hard_gates_passed") is not True:
        raise ValueError("sealed pose hard gates did not pass")
    runner_input = receipt["runner_input"]
    runner_path = pose_root / runner_input
    if runner_path != spec_path or runner_path.resolve(strict=True) != spec_path:
        raise ValueError("sealed pose runner_input does not bind the requested spec")

    design_bound = secure_bound(spec_path)
    scaffold_yaml_bound = secure_bound(bundle / "scaffold.yaml")
    target_bound = secure_bound(bundle / "target.cif")
    scaffold_bound = secure_bound(bundle / "scaffold.cif")
    if top_rows.get("POSE_ANCHORED_SPEC.json") != receipt_bound.sha256:
        raise ValueError("pose receipt/top manifest cross-bind failed")
    if top_rows.get("SPEC_BUNDLE.SHA256SUMS") != spec_manifest_bound.sha256:
        raise ValueError("SPEC_BUNDLE/top manifest digest cross-bind failed")
    for name, bound in {
        "design.yaml": design_bound, "scaffold.yaml": scaffold_yaml_bound,
        "target.cif": target_bound, "scaffold.cif": scaffold_bound,
    }.items():
        relative = f"spec_bundle/{name}"
        if not (bound.sha256 == spec_rows.get(relative) == top_rows.get(relative)):
            raise ValueError(f"spec/top manifest digest cross-bind failed: {relative}")

    design_doc = _yaml(design_bound)
    scaffold_doc = _yaml(scaffold_yaml_bound)
    if design_doc != EXPECTED_DESIGN_DOCUMENT:
        raise ValueError("design.yaml fixed-document semantics drift")
    if scaffold_doc != EXPECTED_SCAFFOLD_DOCUMENT:
        raise ValueError("scaffold.yaml fixed-document semantics drift")

    target_structure = load_structure_from_bound(target_bound)
    scaffold_structure = load_structure_from_bound(scaffold_bound)
    target_chain, target_residues, _ = resolve_spec_chain(target_structure, "E", "pose target")
    scaffold_chain, scaffold_residues, _ = resolve_spec_chain(scaffold_structure, "A", "pose VHH")
    validate_chain_inventory(target_residues, 30, "pose target")
    validate_chain_inventory(scaffold_residues, 121, "pose VHH")
    disulfide = validate_vhh_disulfide(
        scaffold_structure, scaffold_chain, scaffold_residues, "pose VHH"
    )
    target_sequence = chain_sequence(target_residues)
    vhh_sequence = chain_sequence(scaffold_residues)
    sequence_contract = receipt.get("sequence_contract", {})
    if sequence_contract.get("target_sequence") != target_sequence:
        raise ValueError("receipt/target sequence cross-bind failed")
    if sequence_contract.get("sealed_design_3_raw_vhh_sequence") != vhh_sequence:
        raise ValueError("receipt/VHH sequence cross-bind failed")
    if sequence_contract.get("design_residue_indices_1based") != list(DESIGN_INDICES):
        raise ValueError("receipt CDR design indices drift")
    bindings = receipt.get("publication_bindings")
    if not isinstance(bindings, dict) or set(bindings) != EXPECTED_PUBLICATION_BINDING_KEYS:
        raise ValueError("sealed pose publication_bindings key set drift")
    if bindings["spec_bundle_manifest_sha256"] != spec_manifest_bound.sha256:
        raise ValueError("receipt/SPEC_BUNDLE manifest SHA cross-bind failed")
    target_sha = target_bound.sha256
    if not (
        target_sha == bindings.get("copied_target_sha256")
        == bindings.get("source_target_sha256")
        == bindings.get("spec_manifest_target_sha256")
    ):
        raise ValueError("receipt/source/copied target SHA cross-bind failed")
    terminal_bounds = {
        "top manifest": top_manifest_bound,
        "spec manifest": spec_manifest_bound,
        "pose receipt": receipt_bound,
        "design.yaml": design_bound,
        "scaffold.yaml": scaffold_yaml_bound,
        "target.cif": target_bound,
        "scaffold.cif": scaffold_bound,
    }
    for role, bound in terminal_bounds.items():
        current = secure_bound(bound.path)
        if current.identity != bound.identity or current.sha256 != bound.sha256:
            raise ValueError(f"sealed pose {role} changed during source validation")
    if before != secure_tree_snapshot(pose_root):
        raise ValueError("sealed pose tree changed during preflight")
    evidence = {
        "schema_version": SCHEMA,
        "status": "POSE_INPUT_PASS",
        "pose_root": str(pose_root),
        "spec_path": str(spec_path),
        "pose_receipt_sha256": receipt_bound.sha256,
        "top_manifest_sha256": top_manifest_bound.sha256,
        "spec_manifest_sha256": spec_manifest_bound.sha256,
        "receipt_publication_bindings": dict(bindings),
        "runner_input": runner_input,
        "spec_file_sha256": design_bound.sha256,
        "target_sha256": target_bound.sha256,
        "scaffold_sha256": scaffold_bound.sha256,
        "target_sequence": target_sequence,
        "vhh_sequence": vhh_sequence,
        "design_indices_1based": list(DESIGN_INDICES),
        "disulfide": disulfide,
        "tree_identity_sha256": hashlib.sha256(
            json.dumps(before, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    source = {
        "target_sequence": target_sequence,
        "vhh_sequence": vhh_sequence,
        "target_backbone": _chain_backbone(target_residues, "pose target"),
        "vhh_backbone": _chain_backbone(scaffold_residues, "pose VHH"),
        "pose_root": pose_root,
        "snapshot": before,
    }
    return evidence, source


def preflight_spec(spec_path: str) -> dict[str, Any]:
    evidence, _ = _source_contract(Path(spec_path))
    return evidence


def _load_npz(bound) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(_require_bound_bytes(bound)), allow_pickle=False) as archive:
        if not archive.files or len(archive.files) != len(set(archive.files)):
            raise ValueError(f"empty or duplicate-key NPZ: {bound.path}")
        return {name: np.asarray(archive[name]) for name in archive.files}


def _resolve_candidate_roles(
    structure: gemmi.Structure, target_sequence: str, role: str
) -> tuple[gemmi.Chain, list[gemmi.Residue], gemmi.Chain, list[gemmi.Residue]]:
    """Resolve native/check candidate roles by protein semantics, never chain IDs."""
    chains = list(structure[0])
    if len(chains) != 2:
        raise ValueError(f"{role} must contain exactly two protein chains")
    observed: list[tuple[gemmi.Chain, list[gemmi.Residue], str]] = []
    for chain in chains:
        residues = protein_residues(chain)
        observed.append((chain, residues, chain_sequence(residues)))
    target_matches = [
        row for row in observed if len(row[1]) == 30 and row[2] == target_sequence
    ]
    if len(target_matches) != 1:
        summary = [(row[0].name, len(row[1]), row[2] == target_sequence) for row in observed]
        raise ValueError(
            f"{role} target must resolve uniquely by exact 30-residue sequence; "
            f"observed={summary}"
        )
    target_chain, target_residues, _ = target_matches[0]
    vhh_matches = [row for row in observed if row is not target_matches[0]]
    if len(vhh_matches) != 1 or len(vhh_matches[0][1]) != 121:
        raise ValueError(f"{role} non-target chain must be the unique 121-residue VHH")
    vhh_chain, vhh_residues, _ = vhh_matches[0]
    return target_chain, target_residues, vhh_chain, vhh_residues


def _candidate_structure(bound, source: dict[str, Any], *, require_backbone: bool) -> tuple[str, str]:
    structure = load_structure_from_bound(bound)
    target_chain, target_residues, vhh_chain, vhh_residues = _resolve_candidate_roles(
        structure, source["target_sequence"], f"candidate {bound.path}"
    )
    validate_chain_inventory(target_residues, 30, "candidate target")
    validate_chain_inventory(vhh_residues, 121, "candidate VHH")
    target_sequence = chain_sequence(target_residues)
    vhh_sequence = chain_sequence(vhh_residues)
    if target_sequence != source["target_sequence"]:
        raise ValueError(f"target sequence changed: {bound.path}")
    for position, (expected, observed) in enumerate(
        zip(source["vhh_sequence"], vhh_sequence, strict=True), 1
    ):
        if position not in DESIGN_SET and expected != observed:
            raise ValueError(f"non-CDR VHH sequence changed at {position}: {bound.path}")
    if vhh_sequence[21] != "C" or vhh_sequence[94] != "C":
        raise ValueError(f"CYS22/CYS95 changed: {bound.path}")
    if any(vhh_sequence[position - 1] == "C" for position in DESIGN_INDICES):
        raise ValueError(f"inverse fold generated CYS inside the CDR mask: {bound.path}")
    validate_vhh_disulfide(structure, vhh_chain, vhh_residues, "candidate VHH")
    if require_backbone:
        observed_target = _chain_backbone(target_residues, "candidate target")
        observed_vhh = _chain_backbone(vhh_residues, "candidate VHH")
        for role, observed, expected in (
            ("target", observed_target, source["target_backbone"]),
            ("VHH", observed_vhh, source["vhh_backbone"]),
        ):
            if set(observed) != set(expected):
                raise ValueError(f"{role} backbone atom identity drift: {bound.path}")
        # BoltzGen's input featurizer may apply one global rigid augmentation.
        # A single proper transform must close *all* target+VHH backbone atoms;
        # separate chain fits would incorrectly permit interface-pose drift.
        moving = np.stack(
            [observed_target[key] for key in sorted(observed_target)]
            + [observed_vhh[key] for key in sorted(observed_vhh)]
        )
        fixed = np.stack(
            [source["target_backbone"][key] for key in sorted(source["target_backbone"])]
            + [source["vhh_backbone"][key] for key in sorted(source["vhh_backbone"])]
        )
        moving_center = moving.mean(axis=0)
        fixed_center = fixed.mean(axis=0)
        u, _, vt = np.linalg.svd((moving - moving_center).T @ (fixed - fixed_center))
        rotation = vt.T @ u.T
        if np.linalg.det(rotation) < 0:
            vt[-1, :] *= -1
            rotation = vt.T @ u.T
        transformed = (rotation @ (moving - moving_center).T).T + fixed_center
        residuals = np.linalg.norm(transformed - fixed, axis=1)
        maximum = float(residuals.max())
        if maximum > COORDINATE_ATOL:
            raise ValueError(
                f"target/VHH backbone coordinates changed beyond one rigid transform "
                f"({maximum:.6g} A): {bound.path}"
            )
    return target_sequence, vhh_sequence


def _metadata(bound) -> None:
    arrays = _load_npz(bound)
    if not REQUIRED_METADATA <= set(arrays):
        raise ValueError(f"candidate metadata keys missing: {bound.path}")
    for key in REQUIRED_METADATA:
        if arrays[key].shape != (151,) or not np.isfinite(arrays[key]).all():
            raise ValueError(f"candidate metadata shape/value drift: {bound.path}:{key}")
    expected_design = np.zeros(151, dtype=np.int8)
    expected_design[[30 + position - 1 for position in DESIGN_INDICES]] = 1
    if not np.array_equal(arrays["design_mask"].astype(np.int8), expected_design):
        raise ValueError(f"candidate design mask is not the exact 30 CDR positions: {bound.path}")
    expected_binding = np.zeros(151, dtype=np.int8)
    expected_binding[:2] = 1
    if not np.array_equal(arrays["binding_type"].astype(np.int8), expected_binding):
        raise ValueError(f"candidate binding_type drift: {bound.path}")


def _fold_npz(bound, samples: int) -> None:
    arrays = _load_npz(bound)
    coords = arrays.get("coords")
    if coords is None or coords.ndim != 3 or coords.shape[0] != samples or coords.shape[2] != 3:
        raise ValueError(f"fold coordinate topology drift: {bound.path}")
    if not np.isfinite(coords).all():
        raise ValueError(f"non-finite fold coordinates: {bound.path}")
    for key in PER_SAMPLE_KEYS:
        value = arrays.get(key)
        if value is None or value.shape != (samples,) or not np.isfinite(value).all():
            raise ValueError(f"fold per-sample array drift: {bound.path}:{key}")


def _opaque_gzip(bound) -> int:
    data = _require_bound_bytes(bound)
    if not (0 < len(data) <= 16 * 1024 * 1024):
        raise ValueError("opaque gzip compressed size is unsafe")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = decoder.decompress(data, 16 * 1024 * 1024 + 1)
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("opaque artifact is not exactly one complete gzip member")
    if not (0 < len(output) <= 16 * 1024 * 1024):
        raise ValueError("opaque gzip uncompressed size is unsafe")
    return len(output)


def _private_runtime_from_spec(spec: Path) -> tuple[Path, Path]:
    """Derive the one accepted private runtime root from the private pose copy."""
    if (
        not spec.is_absolute()
        or spec.name != "design.yaml"
        or spec.parent.name != "spec_bundle"
        or spec.parent.parent.name != "pose"
    ):
        raise ValueError("resolved only-inverse spec is not private_root/pose/spec_bundle/design.yaml")
    private_root = spec.parent.parent.parent
    if not private_root.name.startswith(".only_ifold_private.attempt_"):
        raise ValueError("resolved only-inverse spec is outside an owner private transaction")
    if private_root.is_symlink() or private_root.resolve(strict=True) != private_root:
        raise ValueError("owner private transaction root is non-canonical or symlinked")
    runtime = private_root / "runtime"
    info = runtime.lstat()
    if runtime.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ValueError("owner private runtime is missing, symlinked, or non-directory")
    if runtime.resolve(strict=True) != runtime:
        raise ValueError("owner private runtime is non-canonical")
    return private_root, runtime


def _require_exact_private_asset(value: Any, expected: Path, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"resolved {role} path is missing or non-string")
    observed = Path(value)
    if not observed.is_absolute() or observed != expected:
        raise ValueError(
            f"resolved {role} role/path mismatch: {observed} != {expected}"
        )
    info = observed.lstat()
    if (
        observed.is_symlink()
        or observed.resolve(strict=True) != observed
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise ValueError(f"resolved {role} asset is not canonical regular single-link bytes")
    return str(observed)


def _checkpoint_references(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    references: list[tuple[tuple[str, ...], str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            references.extend(_checkpoint_references(child, (*path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            references.extend(_checkpoint_references(child, (*path, str(index))))
    elif isinstance(value, str) and Path(value).suffix == ".ckpt":
        references.append((path, value))
    return references


def _validate_config(root: Path, spec: Path, count: int, fold_samples: int) -> dict[str, Any]:
    config = root / "config"
    if {path.name for path in config.iterdir()} != {
        "inverse_folding.yaml", "folding.yaml", "analysis.yaml", "filtering.yaml"
    }:
        raise ValueError("only-inverse config closure drift (design.yaml is forbidden)")
    steps = _yaml(secure_bound(root / "steps.yaml"))
    observed_steps = tuple(row.get("name") for row in steps.get("steps", []))
    if observed_steps != EXPECTED_STEPS:
        raise ValueError(f"steps.yaml is not the exact only-inverse topology: {observed_steps}")
    inverse = _yaml(secure_bound(config / "inverse_folding.yaml"))
    folding = _yaml(secure_bound(config / "folding.yaml"))
    analysis = _yaml(secure_bound(config / "analysis.yaml"))
    filtering = _yaml(secure_bound(config / "filtering.yaml"))
    private_root, private_runtime = _private_runtime_from_spec(spec)
    inverse_checkpoint = _require_exact_private_asset(
        inverse.get("checkpoint"),
        private_runtime / "boltzgen1_ifold.ckpt",
        "inverse-fold checkpoint",
    )
    folding_checkpoint = _require_exact_private_asset(
        folding.get("checkpoint"),
        private_runtime / "boltz2_conf_final.ckpt",
        "folding checkpoint",
    )
    inverse_moldir = _require_exact_private_asset(
        inverse.get("data", {}).get("cfg", {}).get("moldir"),
        private_runtime / "mols.zip",
        "inverse-fold moldir",
    )
    folding_moldir = _require_exact_private_asset(
        folding.get("data", {}).get("cfg", {}).get("moldir"),
        private_runtime / "mols.zip",
        "folding moldir",
    )
    expected_checkpoint_references = {
        "inverse_folding": [(('checkpoint',), inverse_checkpoint)],
        "folding": [(('checkpoint',), folding_checkpoint)],
        "analysis": [],
        "filtering": [],
    }
    for role, document in {
        "inverse_folding": inverse,
        "folding": folding,
        "analysis": analysis,
        "filtering": filtering,
    }.items():
        observed = _checkpoint_references(document)
        if observed != expected_checkpoint_references[role]:
            raise ValueError(
                f"resolved {role} checkpoint references drift; design checkpoints are forbidden"
            )
    checks = {
        "private.root": (str(private_root), str(private_root)),
        "private.runtime": (str(private_runtime), str(private_runtime)),
        "inverse.checkpoint": (inverse_checkpoint, str(private_runtime / "boltzgen1_ifold.ckpt")),
        "inverse.moldir": (inverse_moldir, str(private_runtime / "mols.zip")),
        "inverse.name": (inverse.get("name"), "inverse_fold_only"),
        "inverse.yaml_path": (inverse["data"]["cfg"].get("yaml_path"), [str(spec)]),
        "inverse.multiplicity": (inverse["data"]["cfg"].get("multiplicity"), count),
        "inverse.diffusion_samples": (inverse.get("diffusion_samples"), 1),
        "inverse.skip_existing": (inverse["data"]["cfg"].get("skip_existing"), False),
        "inverse.output": (inverse.get("output"), str(root / "intermediate_designs")),
        "folding.checkpoint": (folding_checkpoint, str(private_runtime / "boltz2_conf_final.ckpt")),
        "folding.moldir": (folding_moldir, str(private_runtime / "mols.zip")),
        "folding.design_dir": (folding["data"].get("design_dir"), str(root / "intermediate_designs")),
        "folding.diffusion_samples": (folding.get("diffusion_samples"), fold_samples),
        "folding.skip_existing": (folding["data"].get("skip_existing"), False),
        "analysis.design_dir": (analysis.get("design_dir"), str(root / "intermediate_designs")),
        "filtering.design_dir": (filtering.get("design_dir"), str(root / "intermediate_designs")),
        "filtering.budget": (filtering.get("budget"), count),
    }
    for label, (actual, expected) in checks.items():
        if actual != expected:
            raise ValueError(f"resolved config mismatch {label}: {actual!r} != {expected!r}")
    return {label: actual for label, (actual, _) in checks.items()}


def validate_inverse_stage(root_text: str, spec_text: str, count: int) -> dict[str, Any]:
    """Gate sequence diversity and fixed-backbone closure before spending folds."""
    if count < 6 or count > 10:
        raise ValueError("owner pilot requires 6..10 inverse-fold sequences")
    root = Path(root_text).expanduser()
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("run root must be absolute, canonical, and non-symlink")
    before = secure_tree_snapshot(root)
    spec = Path(spec_text).expanduser().resolve(strict=True)
    pose_evidence, source = _source_contract(spec)
    _validate_config(root, spec, count, 5)
    expected_ids = {f"design_{index}" for index in range(count)}
    design_dir = root / "intermediate_designs"
    cifs = {
        path.stem: secure_bound(path)
        for path in design_dir.iterdir()
        if path.is_file() and path.suffix == ".cif" and not path.name.endswith("_native.cif")
    }
    npzs = {
        path.stem: secure_bound(path)
        for path in design_dir.iterdir()
        if path.is_file() and path.suffix == ".npz" and not path.name.endswith("_native.npz")
    }
    if set(cifs) != expected_ids or set(npzs) != expected_ids:
        raise ValueError("inverse-fold candidate IDs are not exact design_0..design_N-1")
    sequences: dict[str, str] = {}
    bounds = []
    for candidate_id in sorted(expected_ids):
        _, sequence = _candidate_structure(cifs[candidate_id], source, require_backbone=True)
        _metadata(npzs[candidate_id])
        sequences[candidate_id] = "".join(sequence[index - 1] for index in DESIGN_INDICES)
        bounds.extend((cifs[candidate_id], npzs[candidate_id]))
    unique = len(set(sequences.values()))
    if unique < 4:
        raise ValueError(f"inverse-fold diversity gate failed: unique CDR sequences={unique} < 4")
    for bound in bounds:
        current = secure_bound(bound.path)
        if current.identity != bound.identity or current.sha256 != bound.sha256:
            raise ValueError(f"inverse output changed during gate: {bound.path}")
    if source["snapshot"] != secure_tree_snapshot(source["pose_root"]):
        raise ValueError("sealed pose input changed during inverse gate")
    if before != secure_tree_snapshot(root):
        raise ValueError("run tree changed during inverse gate")
    return {
        "schema_version": SCHEMA,
        "status": "INVERSE_FOLD_PASS",
        "generation_mode": GENERATION_MODE,
        "design_diffusion_performed": False,
        "source_pose": pose_evidence,
        "candidate_ids": sorted(expected_ids),
        "candidate_count": count,
        "unique_cdr_sequence_count": unique,
        "minimum_unique_cdr_sequence_count": 4,
        "backbone_coordinate_closure_count": count,
    }


def validate_run(root_text: str, spec_text: str, count: int, fold_samples: int = 5) -> dict[str, Any]:
    if count < 6 or count > 10 or fold_samples != 5:
        raise ValueError("owner pilot requires 6..10 sequences and exactly 5 folds each")
    root = Path(root_text).expanduser()
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("run root must be absolute, canonical, and non-symlink")
    tree_before = secure_tree_snapshot(root)
    spec = Path(spec_text).expanduser().resolve(strict=True)
    pose_evidence, source = _source_contract(spec)
    config_evidence = _validate_config(root, spec, count, fold_samples)
    expected_ids = {f"design_{index}" for index in range(count)}
    design_dir = root / "intermediate_designs"

    def exact_files(directory: Path, suffix: str) -> dict[str, Any]:
        files = {
            path.stem: secure_bound(path)
            for path in directory.iterdir()
            if path.is_file() and not path.name.endswith(f"_native{suffix}") and path.suffix == suffix
        }
        if set(files) != expected_ids:
            raise ValueError(f"candidate ID mismatch in {directory}: {sorted(files)}")
        return files

    candidate_cifs = exact_files(design_dir, ".cif")
    candidate_npzs = exact_files(design_dir, ".npz")
    fold_npzs = exact_files(design_dir / "fold_out_npz", ".npz")
    refold_cifs = exact_files(design_dir / "refold_cif", ".cif")
    sequences: dict[str, str] = {}
    semantic_bounds = []
    for candidate_id in sorted(expected_ids):
        _, sequence = _candidate_structure(candidate_cifs[candidate_id], source, require_backbone=True)
        _candidate_structure(refold_cifs[candidate_id], source, require_backbone=False)
        _metadata(candidate_npzs[candidate_id])
        _fold_npz(fold_npzs[candidate_id], fold_samples)
        sequences[candidate_id] = sequence
        semantic_bounds.extend((candidate_cifs[candidate_id], candidate_npzs[candidate_id], fold_npzs[candidate_id], refold_cifs[candidate_id]))

    analysis_bound = secure_bound(design_dir / "aggregate_metrics_analyze.csv")
    rows = list(csv.DictReader(io.StringIO(_require_bound_bytes(analysis_bound).decode("utf-8"))))
    if {row.get("id") for row in rows} != expected_ids or len(rows) != count:
        raise ValueError("analysis CSV candidate IDs/count drift")
    for row in rows:
        candidate_id = str(row["id"])
        if row.get("file_name") != f"{candidate_id}.cif":
            raise ValueError("analysis id/file_name drift")
        if str(row.get("designed_chain_sequence", "")).strip().upper() != sequences[candidate_id]:
            raise ValueError("analysis/CIF sequence drift")
    opaque_bound = secure_bound(design_dir / "ca_coords_sequences.pkl.gz")
    opaque_size = _opaque_gzip(opaque_bound)
    filtered_bound = secure_bound(root / "final_ranked_designs/all_designs_metrics.csv")
    final_bound = secure_bound(
        root / f"final_ranked_designs/final_designs_metrics_{count}.csv"
    )
    filtered_rows = list(
        csv.DictReader(io.StringIO(_require_bound_bytes(filtered_bound).decode("utf-8")))
    )
    filtered_ids = [str(row.get("id", "")) for row in filtered_rows]
    if (
        not filtered_ids
        or len(filtered_ids) != len(set(filtered_ids))
        or not set(filtered_ids) <= expected_ids
    ):
        raise ValueError("filtering output IDs are empty, duplicated, or unknown")
    for row in filtered_rows:
        if str(row.get("designed_chain_sequence", "")).strip().upper() != sequences[str(row["id"])]:
            raise ValueError("filtering/CIF sequence drift")
    final_rows = list(
        csv.DictReader(io.StringIO(_require_bound_bytes(final_bound).decode("utf-8")))
    )
    final_ids = [str(row.get("id", "")) for row in final_rows]
    if len(final_ids) != len(set(final_ids)) or not set(final_ids) <= set(filtered_ids):
        raise ValueError("final filtering IDs are duplicated or not a filtered subset")
    semantic_bounds.extend(
        (analysis_bound, opaque_bound, filtered_bound, final_bound)
    )

    for bound in semantic_bounds:
        current = secure_bound(bound.path)
        if current.sha256 != bound.sha256 or current.identity != bound.identity:
            raise ValueError(f"semantic output changed during validation: {bound.path}")
    if source["snapshot"] != secure_tree_snapshot(source["pose_root"]):
        raise ValueError("sealed pose input changed during output validation")
    if tree_before != secure_tree_snapshot(root):
        raise ValueError("run output tree changed during validation")

    records = sorted(
        ({"path": bound.path.relative_to(root).as_posix(), "sha256": bound.sha256}
         for bound in semantic_bounds),
        key=lambda row: row["path"].encode("utf-8"),
    )
    manifest_bytes = "".join(f"{row['sha256']}  ./{row['path']}\n" for row in records).encode()
    return {
        "schema_version": SCHEMA,
        "status": "PASS",
        "generation_mode": GENERATION_MODE,
        "design_diffusion_performed": False,
        "source_pose": pose_evidence,
        "resolved_config_contract": config_evidence,
        "candidate_ids": sorted(expected_ids),
        "observed_sequence_candidates": count,
        "unique_designed_sequence_count": len(set(sequences.values())),
        "fold_samples_per_candidate": fold_samples,
        "observed_fold_sample_count": count * fold_samples,
        "backbone_coordinate_closure_count": count,
        "disulfide_closure_count": count * 2,
        "semantic_payload_files": records,
        "semantic_payload_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "opaque_artifact_uncompressed_bytes": opaque_size,
        "pickle_deserialization_performed": False,
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight-spec")
    preflight.add_argument("spec")
    validate = sub.add_parser("validate-run")
    validate.add_argument("run_root")
    validate.add_argument("spec")
    validate.add_argument("--sequences", type=int, required=True)
    validate.add_argument("--fold-samples", type=int, default=5)
    inverse = sub.add_parser("validate-inverse")
    inverse.add_argument("run_root")
    inverse.add_argument("spec")
    inverse.add_argument("--sequences", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight-spec":
            payload = preflight_spec(args.spec)
        elif args.command == "validate-inverse":
            payload = validate_inverse_stage(args.run_root, args.spec, args.sequences)
        else:
            payload = validate_run(args.run_root, args.spec, args.sequences, args.fold_samples)
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
