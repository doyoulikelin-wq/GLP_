#!/usr/bin/env python3
"""Validate and summarize one Windows-owner multi-state folding run.

The input materializer owns ``tasks.json`` and the paired CIF/NPZ inputs.  This
script owns the terminal 5-fold accounting: every declared task must have one
fold NPZ with exactly the declared number of samples and one parseable refolded
CIF.  It deliberately reports all folds instead of selecting a best fold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import gemmi
import numpy as np


TASK_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REQUIRED_METRICS = (
    "iptm",
    "ptm",
    "design_to_target_iptm",
    "design_ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
)
HIGHER_IS_BETTER = {
    "iptm": True,
    "ptm": True,
    "design_to_target_iptm": True,
    "design_ptm": True,
    "min_design_to_target_pae": False,
    "min_interaction_pae": False,
}
THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


class ValidationError(ValueError):
    """A fail-closed multi-state output validation error."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"expected JSON object: {path}")
    return payload


def safe_member(root: Path, relative: str, *, suffix: str | None = None) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValidationError(f"invalid relative path: {relative!r}")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValidationError(f"unsafe relative path: {relative!r}")
    path = root / candidate
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"missing run member: {relative}") from exc
    if resolved.parent == root.resolve() or root.resolve() in resolved.parents:
        pass
    else:
        raise ValidationError(f"run member escapes root: {relative}")
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValidationError(f"unsafe or empty run member: {relative}")
    if suffix is not None and path.suffix != suffix:
        raise ValidationError(f"unexpected suffix for {relative}: expected {suffix}")
    return path


def cif_polymer_sequences(path: Path) -> list[str]:
    try:
        structure = gemmi.read_structure(str(path))
    except Exception as exc:  # gemmi uses multiple exception types
        raise ValidationError(f"unparseable CIF {path}: {exc}") from exc
    if len(structure) != 1:
        raise ValidationError(f"CIF must contain exactly one model: {path}")
    sequences: list[str] = []
    for chain in structure[0]:
        sequence: list[str] = []
        for residue in chain:
            for atom in residue:
                if not all(math.isfinite(value) for value in (atom.pos.x, atom.pos.y, atom.pos.z)):
                    raise ValidationError(f"CIF contains non-finite coordinates: {path}")
            if residue.entity_type != gemmi.EntityType.Polymer:
                continue
            one = THREE_TO_ONE.get(residue.name.upper())
            if one is None:
                one = gemmi.find_tabulated_residue(residue.name).one_letter_code
                if len(one) != 1 or one == " ":
                    one = "X"
            sequence.append(one)
        if sequence:
            sequences.append("".join(sequence))
    if not sequences:
        raise ValidationError(f"CIF has no polymer sequence: {path}")
    return sequences


def validate_sequence_pair(path: Path, target_sequence: str, vhh_sequence: str) -> None:
    observed = cif_polymer_sequences(path)
    if len(observed) != 2:
        raise ValidationError(f"expected two polymer chains in {path}, observed {len(observed)}")
    if observed.count(target_sequence) != 1 or observed.count(vhh_sequence) != 1:
        raise ValidationError(
            f"target/VHH identity mismatch in {path}: observed lengths="
            f"{[len(value) for value in observed]}"
        )


def validate_tasks(run_root: Path, payload: Mapping[str, object]) -> tuple[list[dict], int]:
    if payload.get("status") != "INPUTS_READY":
        raise ValidationError("tasks.json status must be INPUTS_READY")
    samples = payload.get("samples_per_task")
    if not isinstance(samples, int) or isinstance(samples, bool) or samples <= 0:
        raise ValidationError("samples_per_task must be a positive integer")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValidationError("tasks must be a non-empty list")
    tasks: list[dict] = []
    seen: set[str] = set()
    for number, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise ValidationError(f"task {number} is not an object")
        task = dict(raw)
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or TASK_ID_RE.fullmatch(task_id) is None:
            raise ValidationError(f"unsafe task_id: {task_id!r}")
        if task_id in seen:
            raise ValidationError(f"duplicate task_id: {task_id}")
        seen.add(task_id)
        for field in (
            "candidate_id", "target_state_id", "panel_role", "target_identity",
            "target_sequence", "vhh_sequence", "input_cif_relative_path",
            "input_npz_relative_path", "target_source_path", "target_source_sha256",
        ):
            value = task.get(field)
            if not isinstance(value, str) or not value:
                raise ValidationError(f"task {task_id} missing non-empty {field}")
        if SHA256_RE.fullmatch(task["target_source_sha256"]) is None:
            raise ValidationError(f"task {task_id} has invalid target SHA-256")
        source = Path(task["target_source_path"])
        if not source.is_absolute() or source.is_symlink() or not source.is_file():
            raise ValidationError(f"task {task_id} has unsafe target source")
        if sha256_file(source) != task["target_source_sha256"]:
            raise ValidationError(f"task {task_id} target source SHA-256 changed")
        input_cif = safe_member(run_root, task["input_cif_relative_path"], suffix=".cif")
        input_npz = safe_member(run_root, task["input_npz_relative_path"], suffix=".npz")
        validate_sequence_pair(input_cif, task["target_sequence"], task["vhh_sequence"])
        with np.load(input_npz, allow_pickle=False) as metadata:
            required = {"design_mask", "mol_type", "ss_type", "token_resolved_mask", "binding_type"}
            if set(metadata.files) != required:
                raise ValidationError(
                    f"task {task_id} metadata keys mismatch: {sorted(metadata.files)}"
                )
            length = len(task["target_sequence"]) + len(task["vhh_sequence"])
            arrays = {name: np.asarray(metadata[name]) for name in required}
        for name, array in arrays.items():
            if array.shape != (length,) or not np.isfinite(array).all():
                raise ValidationError(f"task {task_id} invalid metadata array: {name}")
        design_mask = arrays["design_mask"].astype(bool)
        target_length = len(task["target_sequence"])
        expected_design_count = task.get("design_mask_count")
        if (
            not isinstance(expected_design_count, int)
            or isinstance(expected_design_count, bool)
            or expected_design_count <= 0
            or expected_design_count > len(task["vhh_sequence"])
        ):
            raise ValidationError(f"task {task_id} has invalid design_mask_count")
        if (
            design_mask[:target_length].any()
            or int(design_mask[target_length:].sum()) != expected_design_count
        ):
            raise ValidationError(
                f"task {task_id} design_mask must preserve the declared VHH design residues"
            )
        binding = arrays["binding_type"]
        expected_binding = np.zeros(length, dtype=binding.dtype)
        expected_binding[: min(2, target_length)] = 1
        if not np.array_equal(binding, expected_binding):
            raise ValidationError(f"task {task_id} binding_type contract mismatch")
        tasks.append(task)
    declared_candidates = payload.get("candidate_ids")
    declared_states = payload.get("state_ids")
    if not isinstance(declared_candidates, list) or not isinstance(declared_states, list):
        raise ValidationError("candidate_ids and state_ids must be lists")
    candidate_ids = [str(value) for value in declared_candidates]
    state_ids = [str(value) for value in declared_states]
    if len(set(candidate_ids)) != len(candidate_ids) or len(set(state_ids)) != len(state_ids):
        raise ValidationError("declared candidate/state IDs must be unique")
    observed_pairs = {(task["candidate_id"], task["target_state_id"]) for task in tasks}
    expected_pairs = {(candidate, state) for candidate in candidate_ids for state in state_ids}
    if observed_pairs != expected_pairs or len(tasks) != len(expected_pairs):
        raise ValidationError("tasks do not form the declared candidate-by-state Cartesian product")
    return tasks, samples


def numeric_sample_array(archive: Mapping[str, np.ndarray], key: str, samples: int, task_id: str) -> np.ndarray:
    if key not in archive:
        raise ValidationError(f"task {task_id} missing fold metric: {key}")
    value = np.asarray(archive[key])
    if value.shape != (samples,) or not np.issubdtype(value.dtype, np.number):
        raise ValidationError(f"task {task_id} metric {key} must have shape ({samples},)")
    value = value.astype(np.float64)
    if not np.isfinite(value).all():
        raise ValidationError(f"task {task_id} metric {key} contains NaN/Inf")
    return value


def _squeeze_leading_singletons(value: np.ndarray, final_dimensions: int, label: str) -> np.ndarray:
    array = np.asarray(value)
    while array.ndim > final_dimensions and array.shape[0] == 1:
        array = array[0]
    if array.ndim != final_dimensions:
        raise ValidationError(f"unexpected {label} shape: {np.asarray(value).shape}")
    return array


def target_pairwise_distance_vector(
    coords: np.ndarray,
    atom_to_token: np.ndarray,
    atom_resolved_mask: np.ndarray,
    target_token_count: int,
) -> np.ndarray:
    """Return a rigid-transform-invariant target-geometry identity vector."""
    coordinates = _squeeze_leading_singletons(coords, 2, "input coords")
    mapping = _squeeze_leading_singletons(atom_to_token, 2, "atom_to_token")
    resolved = _squeeze_leading_singletons(
        atom_resolved_mask, 1, "atom_resolved_mask"
    ).astype(bool)
    if coordinates.shape[1] != 3 or mapping.shape[0] != coordinates.shape[0]:
        raise ValidationError("coordinate/atom mapping shape mismatch")
    if resolved.shape != (coordinates.shape[0],):
        raise ValidationError("resolved atom mask shape mismatch")
    if not isinstance(target_token_count, int) or not 0 < target_token_count < mapping.shape[1]:
        raise ValidationError("invalid target token count for geometry identity")
    if not np.isfinite(coordinates).all() or not np.isin(mapping, [0, 1]).all():
        raise ValidationError("invalid coordinate/atom mapping values")
    target_atoms = resolved & mapping[:, :target_token_count].astype(bool).any(axis=1)
    selected = coordinates[target_atoms].astype(np.float64)
    if selected.shape[0] < 3:
        raise ValidationError("too few resolved target atoms for geometry identity")
    deltas = selected[:, None, :] - selected[None, :, :]
    distances = np.sqrt(np.sum(deltas * deltas, axis=-1))
    upper = distances[np.triu_indices(selected.shape[0], 1)]
    if not np.isfinite(upper).all() or upper.size < 3:
        raise ValidationError("invalid target pairwise-distance vector")
    return upper.astype(np.float32)


def load_preflight_geometry(run_root: Path, tasks: Sequence[Mapping[str, str]]) -> dict[str, np.ndarray]:
    contract_path = safe_member(
        run_root, "operator_logs/preflight_contract.json", suffix=".json"
    )
    contract = load_json_object(contract_path)
    if contract.get("status") != "PASS":
        raise ValidationError("preflight geometry contract is not PASS")
    relative = contract.get("coordinate_contract_relative_path")
    expected_sha = contract.get("coordinate_contract_sha256")
    if not isinstance(relative, str) or SHA256_RE.fullmatch(str(expected_sha)) is None:
        raise ValidationError("preflight geometry contract binding is incomplete")
    path = safe_member(run_root, relative, suffix=".npz")
    if sha256_file(path) != expected_sha:
        raise ValidationError("preflight geometry contract SHA-256 changed")
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {task["task_id"] for task in tasks}:
            raise ValidationError("preflight geometry task closure mismatch")
        geometry = {name: np.asarray(archive[name], dtype=np.float32) for name in archive.files}
    for task_id, vector in geometry.items():
        if vector.ndim != 1 or vector.size < 3 or not np.isfinite(vector).all():
            raise ValidationError(f"invalid preflight geometry vector: {task_id}")
    return geometry


def validate_outputs(
    run_root: Path,
    tasks: Sequence[Mapping[str, str]],
    samples: int,
    expected_geometry: Mapping[str, np.ndarray],
) -> list[dict]:
    fold_dir = run_root / "design_inputs" / "fold_out_npz"
    cif_dir = run_root / "design_inputs" / "refold_cif"
    if not fold_dir.is_dir() or fold_dir.is_symlink() or not cif_dir.is_dir() or cif_dir.is_symlink():
        raise ValidationError("fold_out_npz/refold_cif directories are missing or unsafe")
    expected = {task["task_id"] for task in tasks}
    for directory, suffix in ((fold_dir, ".npz"), (cif_dir, ".cif")):
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix != suffix:
                raise ValidationError(f"unexpected fold output member: {path}")
    fold_files = {path.stem: path for path in fold_dir.iterdir() if path.is_file() and path.suffix == ".npz"}
    cif_files = {path.stem: path for path in cif_dir.iterdir() if path.is_file() and path.suffix == ".cif"}
    if set(fold_files) != expected or set(cif_files) != expected:
        raise ValidationError(
            "fold output closure mismatch: "
            f"npz_missing={sorted(expected - set(fold_files))} "
            f"npz_extra={sorted(set(fold_files) - expected)} "
            f"cif_missing={sorted(expected - set(cif_files))} "
            f"cif_extra={sorted(set(cif_files) - expected)}"
        )
    rows: list[dict] = []
    for task in tasks:
        task_id = task["task_id"]
        fold_path = fold_files[task_id]
        cif_path = cif_files[task_id]
        if fold_path.is_symlink() or cif_path.is_symlink() or not fold_path.stat().st_size or not cif_path.stat().st_size:
            raise ValidationError(f"task {task_id} has unsafe/empty fold output")
        validate_sequence_pair(cif_path, task["target_sequence"], task["vhh_sequence"])
        try:
            archive_context = np.load(fold_path, allow_pickle=False)
        except Exception as exc:
            raise ValidationError(f"task {task_id} has unreadable fold NPZ: {exc}") from exc
        with archive_context as archive:
            for name in archive.files:
                raw = np.asarray(archive[name])
                if raw.dtype.hasobject or not (
                    np.issubdtype(raw.dtype, np.number)
                    or np.issubdtype(raw.dtype, np.bool_)
                ):
                    raise ValidationError(f"task {task_id} has unsafe fold array: {name}")
                if not np.isfinite(raw).all():
                    raise ValidationError(f"task {task_id} fold array contains NaN/Inf: {name}")
            metric_arrays = {
                metric: numeric_sample_array(archive, metric, samples, task_id)
                for metric in REQUIRED_METRICS
            }
            if "coords" not in archive:
                raise ValidationError(f"task {task_id} fold NPZ has no coords")
            coords = np.asarray(archive["coords"])
            if coords.ndim != 3 or coords.shape[0] != samples or coords.shape[2] != 3:
                raise ValidationError(f"task {task_id} coords shape mismatch: {coords.shape}")
            if not np.issubdtype(coords.dtype, np.number) or not np.isfinite(coords).all():
                raise ValidationError(f"task {task_id} coords contain invalid values")
            for required_input_key in ("input_coords", "atom_to_token", "atom_resolved_mask"):
                if required_input_key not in archive:
                    raise ValidationError(
                        f"task {task_id} fold NPZ has no {required_input_key}"
                    )
            observed_geometry = target_pairwise_distance_vector(
                archive["input_coords"],
                archive["atom_to_token"],
                archive["atom_resolved_mask"],
                len(task["target_sequence"]),
            )
            expected_vector = np.asarray(expected_geometry[task_id], dtype=np.float32)
            if observed_geometry.shape != expected_vector.shape or not np.allclose(
                observed_geometry,
                expected_vector,
                rtol=1e-5,
                atol=2e-4,
            ):
                raise ValidationError(
                    f"task {task_id} fold input geometry does not match its declared state"
                )
        for sample_index in range(samples):
            row = {
                "task_id": task_id,
                "candidate_id": task["candidate_id"],
                "target_state_id": task["target_state_id"],
                "panel_role": task["panel_role"],
                "target_identity": task["target_identity"],
                "sample_index": sample_index,
            }
            row.update({metric: float(values[sample_index]) for metric, values in metric_arrays.items()})
            rows.append(row)
    return rows


def metric_summary(values: Iterable[float], metric: str) -> dict[str, float]:
    data = [float(value) for value in values]
    if not data or not all(math.isfinite(value) for value in data):
        raise ValidationError(f"cannot summarize invalid metric values: {metric}")
    minimum = min(data)
    maximum = max(data)
    return {
        "mean": statistics.fmean(data),
        "median": statistics.median(data),
        "std_population": statistics.pstdev(data),
        "min": minimum,
        "max": maximum,
        "worst": minimum if HIGHER_IS_BETTER[metric] else maximum,
    }


def summarize_groups(rows: Sequence[Mapping[str, object]], group_fields: Sequence[str]) -> list[dict]:
    groups: dict[tuple[str, ...], list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row[field]) for field in group_fields)].append(row)
    output: list[dict] = []
    for group_key in sorted(groups):
        group = groups[group_key]
        record = dict(zip(group_fields, group_key, strict=True))
        record["sample_count"] = len(group)
        for metric in REQUIRED_METRICS:
            summary = metric_summary((float(row[metric]) for row in group), metric)
            for statistic_name, value in summary.items():
                record[f"{metric}_{statistic_name}"] = value
        output.append(record)
    return output


def build_contrasts(task_summary: Sequence[Mapping[str, object]], baseline_state: str) -> list[dict]:
    by_pair = {
        (str(row["candidate_id"]), str(row["target_state_id"])): row
        for row in task_summary
    }
    candidates = sorted({candidate for candidate, _ in by_pair})
    states = sorted({state for _, state in by_pair})
    output: list[dict] = []
    for candidate in candidates:
        baseline = by_pair.get((candidate, baseline_state))
        if baseline is None:
            raise ValidationError(f"missing baseline {baseline_state} for {candidate}")
        for state in states:
            current = by_pair.get((candidate, state))
            if current is None:
                raise ValidationError(f"missing state {state} for {candidate}")
            for metric in REQUIRED_METRICS:
                current_mean = float(current[f"{metric}_mean"])
                baseline_mean = float(baseline[f"{metric}_mean"])
                raw_delta = current_mean - baseline_mean
                output.append(
                    {
                        "candidate_id": candidate,
                        "target_state_id": state,
                        "baseline_state_id": baseline_state,
                        "metric": metric,
                        "higher_is_better": HIGHER_IS_BETTER[metric],
                        "state_mean": current_mean,
                        "baseline_mean": baseline_mean,
                        "raw_delta": raw_delta,
                        "single_complex_quality_direction_delta": (
                            raw_delta if HIGHER_IS_BETTER[metric] else -raw_delta
                        ),
                        "direction_interpretation": (
                            "positive means higher single-complex model quality; "
                            "it is not a binding or selectivity-favorable direction"
                        ),
                    }
                )
    return output


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if path.exists() or temporary.exists():
        raise ValidationError(f"refusing to overwrite report: {path}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def csv_text(rows: Sequence[Mapping[str, object]]) -> str:
    if not rows:
        raise ValidationError("refusing to write empty CSV")
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def execute(run_root: Path, baseline_state: str) -> dict:
    root = run_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("run root must be a real directory")
    tasks_path = safe_member(root, "tasks.json", suffix=".json")
    tasks_payload = load_json_object(tasks_path)
    tasks, samples = validate_tasks(root, tasks_payload)
    expected_geometry = load_preflight_geometry(root, tasks)
    fold_rows = validate_outputs(root, tasks, samples, expected_geometry)
    task_rows = summarize_groups(
        fold_rows,
        ("candidate_id", "target_state_id", "panel_role", "target_identity", "task_id"),
    )
    state_rows = summarize_groups(
        fold_rows,
        ("target_state_id", "panel_role", "target_identity"),
    )
    contrast_rows = build_contrasts(task_rows, baseline_state)
    report_paths = {
        "fold_metrics": root / "reports" / "fold_metrics.csv",
        "task_summary": root / "reports" / "task_summary.csv",
        "state_summary": root / "reports" / "state_summary.csv",
        "candidate_state_contrasts": root / "reports" / "candidate_state_contrasts.csv",
    }
    for name, path in report_paths.items():
        rows = {
            "fold_metrics": fold_rows,
            "task_summary": task_rows,
            "state_summary": state_rows,
            "candidate_state_contrasts": contrast_rows,
        }[name]
        atomic_write_text(path, csv_text(rows))
    contract = {
        "schema_version": "WINDOWS_OWNER_MULTISTATE_OUTPUT_CONTRACT_V1",
        "status": "PASS",
        "logical_task_count": len(tasks),
        "samples_per_task": samples,
        "sample_row_count": len(fold_rows),
        "candidate_count": len({task["candidate_id"] for task in tasks}),
        "state_count": len({task["target_state_id"] for task in tasks}),
        "failed_task_count": 0,
        "baseline_state_id": baseline_state,
        "aggregation_policy": "ALL_FOLDS_MEAN_MEDIAN_POPULATION_SD_MIN_MAX_AND_DIRECTIONAL_WORST",
        "best_fold_only": False,
        "training_performed": False,
        "formal_gate_claimed": False,
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
        "reports": {
            name: {
                "relative_path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
            for name, path in report_paths.items()
        },
    }
    contract_path = root / "operator_logs" / "multistate_contract.json"
    atomic_write_text(
        contract_path,
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return contract


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--baseline-state", default="DEV_00")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        contract = execute(args.run_root, args.baseline_state)
    except (ValidationError, OSError, ValueError) as exc:
        raise SystemExit(f"MULTISTATE_VALIDATION_FAILED: {exc}") from exc
    print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
