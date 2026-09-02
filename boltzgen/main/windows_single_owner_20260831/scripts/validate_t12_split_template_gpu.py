#!/usr/bin/env python3
"""Fail-closed validation for one owner-mode T12 split-template folding run.

The validator does not run a model and does not interpret scientific quality.  It
binds the six copied T11 inputs to their before/after manifests, verifies the
resolved folding-only configuration, and checks the complete 6 x 5 NPZ output
topology before emitting a small JSON receipt on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import yaml


SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_VALIDATION_V1"
SOURCE_SCHEMA = "WINDOWS_OWNER_T12_SOURCE_INPUTS_V1"
DATA_MODULE_TARGET = "owner_split_template_data.SplitTemplateFromGeneratedDataModule"
EXPECTED_IDS = tuple(f"design_{index}" for index in range(6))
EXPECTED_INPUT_NAMES = frozenset(
    f"{candidate_id}{suffix}"
    for candidate_id in EXPECTED_IDS
    for suffix in (".cif", ".npz")
)
EXPECTED_FOLD_NAMES = frozenset(f"{candidate_id}.npz" for candidate_id in EXPECTED_IDS)
EXPECTED_REFOLD_NAMES = frozenset(f"{candidate_id}.cif" for candidate_id in EXPECTED_IDS)
TOTAL_TOKENS = 151
TARGET_TOKENS = 30
CDR_TOKENS = 30
FRAMEWORK_TOKENS = 91
REQUIRED_INPUT_ARRAYS = (
    "design_mask",
    "mol_type",
    "ss_type",
    "token_resolved_mask",
    "binding_type",
)
REQUIRED_SAMPLE_METRICS = (
    "iptm",
    "ptm",
    "design_to_target_iptm",
    "design_ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
)
STRUCTURAL_FOLD_ARRAYS = frozenset(
    {
        "coords",
        "input_coords",
        "token_index",
        "mol_type",
        "res_type",
        "atom_to_token",
        "atom_resolved_mask",
        "backbone_mask",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ValidationError(ValueError):
    """Raised when run bytes do not satisfy the frozen T12 contract."""


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _canonical_directory(path: Path, role: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValidationError(f"{role} must be absolute: {path}")
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{role} is unavailable: {path}: {exc}") from exc
    if path.is_symlink() or resolved != path or not stat.S_ISDIR(before.st_mode):
        raise ValidationError(f"{role} must be a canonical non-symlink directory: {path}")
    return path


def _read_regular(path: Path, role: str) -> tuple[bytes, str, int, tuple[int, ...]]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValidationError(f"{role} is unavailable: {path}: {exc}") from exc
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValidationError(f"{role} must be a regular non-symlink file: {path}")
    if before.st_nlink != 1:
        raise ValidationError(f"{role} must not be hard-linked: {path}")
    data = path.read_bytes()
    after = path.lstat()
    identity = _identity(before)
    if identity != _identity(after):
        raise ValidationError(f"{role} changed while being read: {path}")
    if not data:
        raise ValidationError(f"{role} is empty: {path}")
    return data, hashlib.sha256(data).hexdigest(), len(data), identity


def _json_file(path: Path, role: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data, digest, size, identity = _read_regular(path, role)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{role} is not valid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{role} JSON root must be an object: {path}")
    return value, {
        "path": path,
        "sha256": digest,
        "size_bytes": size,
        "identity": identity,
    }


def _yaml_file(path: Path, role: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data, digest, size, identity = _read_regular(path, role)
    try:
        value = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValidationError(f"{role} is not valid UTF-8 YAML: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{role} YAML root must be an object: {path}")
    return value, {
        "path": path,
        "sha256": digest,
        "size_bytes": size,
        "identity": identity,
    }


def _load_npz(path: Path, role: str) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    data, digest, size, identity = _read_regular(path, role)
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as archive:
            if not archive.files or len(archive.files) != len(set(archive.files)):
                raise ValidationError(f"{role} NPZ is empty or has duplicate keys: {path}")
            arrays = {name: np.asarray(archive[name]) for name in archive.files}
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"{role} NPZ is unreadable: {path}: {exc}") from exc
    for name, value in arrays.items():
        if value.dtype.hasobject:
            raise ValidationError(f"object arrays are forbidden: {path}:{name}")
        if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
            raise ValidationError(f"NaN/Inf is forbidden: {path}:{name}")
    return arrays, {
        "path": path,
        "sha256": digest,
        "size_bytes": size,
        "identity": identity,
    }


def _manifest_entry(value: Any, name: str, role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"sha256", "size_bytes"}:
        raise ValidationError(f"{role} files[{name!r}] must contain only sha256/size_bytes")
    digest = value.get("sha256")
    size = value.get("size_bytes")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ValidationError(f"{role} files[{name!r}] has an invalid sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValidationError(f"{role} files[{name!r}] has an invalid size_bytes")
    return {"sha256": digest, "size_bytes": size}


def _validate_source_manifest(
    path: Path, role: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    document, bound = _json_file(path, role)
    if set(document) != {"schema_version", "source_t11_attempt", "files"}:
        raise ValidationError(f"{role} has an unexpected top-level key set")
    if document.get("schema_version") != SOURCE_SCHEMA:
        raise ValidationError(f"{role} schema_version mismatch")
    source_text = document.get("source_t11_attempt")
    if not isinstance(source_text, str) or not source_text:
        raise ValidationError(f"{role} source_t11_attempt must be a non-empty string")
    source = _canonical_directory(Path(source_text), f"{role} source_t11_attempt")
    files = document.get("files")
    if not isinstance(files, dict) or set(files) != EXPECTED_INPUT_NAMES:
        observed = sorted(files) if isinstance(files, dict) else type(files).__name__
        raise ValidationError(
            f"{role} input closure mismatch: observed={observed}"
        )
    normalized = {
        name: _manifest_entry(files[name], name, role)
        for name in sorted(EXPECTED_INPUT_NAMES)
    }
    return {
        "schema_version": SOURCE_SCHEMA,
        "source_t11_attempt": str(source),
        "files": normalized,
    }, bound


def _resolve_child(root: Path, value: Path, role: str) -> Path:
    path = value.expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{role} is unavailable: {path}: {exc}") from exc
    if path.is_symlink() or resolved != path:
        raise ValidationError(f"{role} must be canonical and non-symlink: {path}")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{role} must be inside run root: {path}") from exc
    return path


def _exact_suffix_closure(
    directory: Path,
    expected: frozenset[str],
    suffix: str,
    role: str,
) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError(f"{role} directory is missing or unsafe: {directory}")
    observed = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix == suffix
    }
    if observed != expected:
        raise ValidationError(
            f"{role} closure mismatch: observed={sorted(observed)} expected={sorted(expected)}"
        )


def _validate_input_npz(arrays: Mapping[str, np.ndarray], candidate_id: str) -> None:
    missing = set(REQUIRED_INPUT_ARRAYS) - set(arrays)
    if missing:
        raise ValidationError(f"{candidate_id} input NPZ is missing {sorted(missing)}")
    for name in REQUIRED_INPUT_ARRAYS:
        value = arrays[name]
        if value.shape != (TOTAL_TOKENS,):
            raise ValidationError(
                f"{candidate_id} input {name} shape must be ({TOTAL_TOKENS},), got {value.shape}"
            )
    design_mask = arrays["design_mask"]
    if not (
        np.issubdtype(design_mask.dtype, np.number)
        or np.issubdtype(design_mask.dtype, np.bool_)
    ):
        raise ValidationError(f"{candidate_id} design_mask must be numeric/bool")
    if not np.isin(design_mask, (0, 1)).all():
        raise ValidationError(f"{candidate_id} design_mask must be binary")
    design = design_mask.astype(bool)
    if int(design.sum()) != CDR_TOKENS or bool(design[:TARGET_TOKENS].any()):
        raise ValidationError(
            f"{candidate_id} token split must be target/CDR/framework=30/30/91"
        )


def _validate_fold_npz(
    arrays: Mapping[str, np.ndarray], candidate_id: str, fold_samples: int
) -> tuple[int, list[str]]:
    required = {*STRUCTURAL_FOLD_ARRAYS, *REQUIRED_SAMPLE_METRICS}
    missing = required - set(arrays)
    if missing:
        raise ValidationError(f"{candidate_id} fold NPZ is missing {sorted(missing)}")
    coords = arrays["coords"]
    if coords.ndim != 3 or coords.shape[0] != fold_samples or coords.shape[2] != 3:
        raise ValidationError(
            f"{candidate_id} coords shape must be ({fold_samples}, Natom, 3), got {coords.shape}"
        )
    atom_count = int(coords.shape[1])
    if atom_count <= 0:
        raise ValidationError(f"{candidate_id} coords has no atoms")
    expected_shapes = {
        "input_coords": (1, 1, atom_count, 3),
        "token_index": (1, TOTAL_TOKENS),
        "mol_type": (1, TOTAL_TOKENS),
        "res_type": (1, TOTAL_TOKENS, 33),
        "atom_to_token": (1, atom_count, TOTAL_TOKENS),
        "atom_resolved_mask": (1, atom_count),
        "backbone_mask": (1, atom_count),
    }
    for name, expected in expected_shapes.items():
        if arrays[name].shape != expected:
            raise ValidationError(
                f"{candidate_id} {name} shape must be {expected}, got {arrays[name].shape}"
            )
    if not np.array_equal(arrays["token_index"][0], np.arange(TOTAL_TOKENS)):
        raise ValidationError(f"{candidate_id} token_index must be exact 0..150")
    atom_to_token = arrays["atom_to_token"]
    if not (
        np.issubdtype(atom_to_token.dtype, np.number)
        or np.issubdtype(atom_to_token.dtype, np.bool_)
    ) or not np.isin(atom_to_token, (0, 1)).all():
        raise ValidationError(f"{candidate_id} atom_to_token must be binary")
    assignment_count = atom_to_token.astype(bool).sum(axis=2)[0]
    if not np.isin(assignment_count, (0, 1)).all():
        raise ValidationError(f"{candidate_id} atom_to_token must be zero/one-hot")
    resolved = arrays["atom_resolved_mask"]
    backbone = arrays["backbone_mask"]
    for name, mask in (("atom_resolved_mask", resolved), ("backbone_mask", backbone)):
        if not (
            np.issubdtype(mask.dtype, np.number)
            or np.issubdtype(mask.dtype, np.bool_)
        ) or not np.isin(mask, (0, 1)).all():
            raise ValidationError(f"{candidate_id} {name} must be binary")
    required_assignment = resolved.astype(bool)[0] | backbone.astype(bool)[0]
    if not np.all(assignment_count[required_assignment] == 1):
        raise ValidationError(
            f"{candidate_id} every resolved/backbone atom must map to one token"
        )
    metric_keys = sorted(set(arrays) - STRUCTURAL_FOLD_ARRAYS)
    for name in metric_keys:
        value = arrays[name]
        if not np.issubdtype(value.dtype, np.number):
            raise ValidationError(f"{candidate_id} sample metric {name} must be numeric")
        if value.shape != (fold_samples,):
            raise ValidationError(
                f"{candidate_id} sample metric {name} shape must be "
                f"({fold_samples},), got {value.shape}"
            )
    return atom_count, metric_keys


def _validate_resolved_config(
    root: Path, resolved_config: Path, fold_samples: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected_config = root / "config" / "folding.yaml"
    if resolved_config != expected_config:
        raise ValidationError(
            f"resolved config must be RUN_ROOT/config/folding.yaml: {resolved_config}"
        )
    config, config_bound = _yaml_file(resolved_config, "resolved folding config")
    steps_path = root / "steps.yaml"
    steps, steps_bound = _yaml_file(steps_path, "resolved steps")
    expected_steps = {"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}
    if steps != expected_steps:
        raise ValidationError("steps.yaml must contain exactly one folding step")
    data = config.get("data")
    if not isinstance(data, dict):
        raise ValidationError("resolved folding config data must be an object")
    checks = {
        "data._target_": (data.get("_target_"), DATA_MODULE_TARGET),
        "data.target_templates": (data.get("target_templates"), True),
        "data.design_mask_templates": (data.get("design_mask_templates"), False),
        "data.expected_target_tokens": (data.get("expected_target_tokens"), TARGET_TOKENS),
        "data.expected_cdr_tokens": (data.get("expected_cdr_tokens"), CDR_TOKENS),
        "data.expected_framework_tokens": (
            data.get("expected_framework_tokens"),
            FRAMEWORK_TOKENS,
        ),
        "data.skip_existing": (data.get("skip_existing"), False),
        "diffusion_samples": (config.get("diffusion_samples"), fold_samples),
    }
    for label, (observed, expected) in checks.items():
        if observed != expected:
            raise ValidationError(
                f"resolved config mismatch {label}: {observed!r} != {expected!r}"
            )
    if sum(
        int(data[name])
        for name in (
            "expected_target_tokens",
            "expected_cdr_tokens",
            "expected_framework_tokens",
        )
    ) != TOTAL_TOKENS:
        raise ValidationError("resolved split-template token counts do not total 151")
    evidence = {
        "steps": ["folding"],
        "data_module_target": DATA_MODULE_TARGET,
        "target_templates": True,
        "design_mask_templates": False,
        "expected_target_tokens": TARGET_TOKENS,
        "expected_cdr_tokens": CDR_TOKENS,
        "expected_framework_tokens": FRAMEWORK_TOKENS,
        "expected_total_tokens": TOTAL_TOKENS,
        "diffusion_samples": fold_samples,
        "skip_existing": False,
    }
    return evidence, [config_bound, steps_bound]


def _semantic_record(root: Path, bound: Mapping[str, Any], role: str) -> dict[str, Any]:
    path = Path(bound["path"])
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = str(path)
    return {
        "path": relative,
        "role": role,
        "sha256": str(bound["sha256"]),
        "size_bytes": int(bound["size_bytes"]),
    }


def _replay_bound(bound: Mapping[str, Any], role: str) -> None:
    _, digest, size, identity = _read_regular(Path(bound["path"]), role)
    if (
        digest != bound["sha256"]
        or size != bound["size_bytes"]
        or identity != bound["identity"]
    ):
        raise ValidationError(f"{role} changed during validation: {bound['path']}")


def validate_run(
    run_root: str | Path,
    source_input_manifest: str | Path,
    *,
    resolved_config: str | Path | None = None,
    fold_samples: int = 5,
) -> dict[str, Any]:
    """Validate a completed six-candidate T12 split-template folding run."""
    if fold_samples != 5:
        raise ValidationError("T12 requires exactly 5 fold samples per candidate")
    root = _canonical_directory(Path(run_root), "run root")
    manifest_path = _resolve_child(
        root, Path(source_input_manifest), "source input after-manifest"
    )
    if manifest_path.name != "SOURCE_INPUTS_AFTER.json":
        raise ValidationError("source input manifest must be SOURCE_INPUTS_AFTER.json")
    after, after_bound = _validate_source_manifest(
        manifest_path, "source input after-manifest"
    )
    before_path = manifest_path.with_name("SOURCE_INPUTS_BEFORE.json")
    before, before_bound = _validate_source_manifest(
        before_path, "source input before-manifest"
    )
    if before != after:
        raise ValidationError("SOURCE_INPUTS_BEFORE/AFTER manifests do not match exactly")

    config_path = _resolve_child(
        root,
        Path(resolved_config) if resolved_config is not None else Path("config/folding.yaml"),
        "resolved folding config",
    )
    config_evidence, config_bounds = _validate_resolved_config(
        root, config_path, fold_samples
    )

    design_dir = root / "intermediate_designs"
    expected_input_cifs = frozenset(f"{value}.cif" for value in EXPECTED_IDS)
    expected_input_npzs = frozenset(f"{value}.npz" for value in EXPECTED_IDS)
    _exact_suffix_closure(design_dir, expected_input_cifs, ".cif", "input CIF")
    _exact_suffix_closure(design_dir, expected_input_npzs, ".npz", "input NPZ")
    fold_dir = design_dir / "fold_out_npz"
    refold_dir = design_dir / "refold_cif"
    _exact_suffix_closure(fold_dir, EXPECTED_FOLD_NAMES, ".npz", "fold NPZ")
    _exact_suffix_closure(refold_dir, EXPECTED_REFOLD_NAMES, ".cif", "refold CIF")

    source_dir = Path(after["source_t11_attempt"]) / "intermediate_designs"
    _canonical_directory(source_dir, "source T11 intermediate_designs")
    semantic: list[tuple[dict[str, Any], str]] = [
        (after_bound, "source_input_after_manifest"),
        (before_bound, "source_input_before_manifest"),
        *((bound, "resolved_configuration") for bound in config_bounds),
    ]
    per_candidate: dict[str, Any] = {}
    all_bounds: list[dict[str, Any]] = [after_bound, before_bound, *config_bounds]

    for candidate_id in EXPECTED_IDS:
        candidate: dict[str, Any] = {}
        for suffix, key, role in (
            (".cif", "input_cif", "input_cif"),
            (".npz", "input_npz", "input_npz"),
        ):
            name = f"{candidate_id}{suffix}"
            source_bytes, source_sha, source_size, source_identity = _read_regular(
                source_dir / name, f"source T11 {name}"
            )
            del source_bytes
            source_bound = {
                "path": source_dir / name,
                "sha256": source_sha,
                "size_bytes": source_size,
                "identity": source_identity,
            }
            all_bounds.append(source_bound)
            declared = after["files"][name]
            if source_sha != declared["sha256"] or source_size != declared["size_bytes"]:
                raise ValidationError(f"source T11 bytes do not replay manifest: {name}")
            run_path = design_dir / name
            if suffix == ".npz":
                arrays, bound = _load_npz(run_path, f"{candidate_id} input NPZ")
                _validate_input_npz(arrays, candidate_id)
            else:
                _, digest, size, identity = _read_regular(
                    run_path, f"{candidate_id} input CIF"
                )
                bound = {
                    "path": run_path,
                    "sha256": digest,
                    "size_bytes": size,
                    "identity": identity,
                }
            if (
                bound["sha256"] != declared["sha256"]
                or bound["size_bytes"] != declared["size_bytes"]
            ):
                raise ValidationError(f"copied run input does not replay manifest: {name}")
            candidate[f"{key}_sha256"] = bound["sha256"]
            candidate[f"{key}_size_bytes"] = bound["size_bytes"]
            semantic.append((bound, role))
            all_bounds.append(bound)

        fold_arrays, fold_bound = _load_npz(
            fold_dir / f"{candidate_id}.npz", f"{candidate_id} fold NPZ"
        )
        atom_count, metric_keys = _validate_fold_npz(
            fold_arrays, candidate_id, fold_samples
        )
        _, refold_sha, refold_size, refold_identity = _read_regular(
            refold_dir / f"{candidate_id}.cif", f"{candidate_id} refold CIF"
        )
        refold_bound = {
            "path": refold_dir / f"{candidate_id}.cif",
            "sha256": refold_sha,
            "size_bytes": refold_size,
            "identity": refold_identity,
        }
        candidate.update(
            {
                "fold_npz_sha256": fold_bound["sha256"],
                "fold_npz_size_bytes": fold_bound["size_bytes"],
                "refold_cif_sha256": refold_bound["sha256"],
                "refold_cif_size_bytes": refold_bound["size_bytes"],
                "fold_samples": fold_samples,
                "atom_count": atom_count,
                "token_count": TOTAL_TOKENS,
                "sample_metric_keys": metric_keys,
            }
        )
        semantic.extend(((fold_bound, "fold_npz"), (refold_bound, "refold_cif")))
        all_bounds.extend((fold_bound, refold_bound))
        per_candidate[candidate_id] = candidate

    for bound in all_bounds:
        _replay_bound(bound, "semantic payload")
    records = sorted(
        (_semantic_record(root, bound, role) for bound, role in semantic),
        key=lambda row: (row["path"].encode("utf-8"), row["role"]),
    )
    manifest_bytes = "".join(
        f"{row['sha256']}  {row['path']}  {row['role']}\n" for row in records
    ).encode("utf-8")
    return {
        "schema_version": SCHEMA,
        "status": "PASS",
        "run_root": str(root),
        "candidate_ids": list(EXPECTED_IDS),
        "candidate_count": len(EXPECTED_IDS),
        "fold_samples_per_candidate": fold_samples,
        "observed_fold_sample_count": len(EXPECTED_IDS) * fold_samples,
        "source_input_manifest": {
            "after_path": manifest_path.relative_to(root).as_posix(),
            "after_sha256": after_bound["sha256"],
            "before_path": before_path.relative_to(root).as_posix(),
            "before_sha256": before_bound["sha256"],
            "source_t11_attempt": after["source_t11_attempt"],
            "replayed_file_count": len(EXPECTED_INPUT_NAMES),
        },
        "resolved_execution_contract": config_evidence,
        "per_candidate": per_candidate,
        "semantic_payload_files": records,
        "semantic_payload_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-run")
    validate.add_argument("run_root")
    validate.add_argument("--source-input-manifest", required=True)
    validate.add_argument("--resolved-config")
    validate.add_argument("--fold-samples", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = validate_run(
            args.run_root,
            args.source_input_manifest,
            resolved_config=args.resolved_config,
            fold_samples=args.fold_samples,
        )
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
