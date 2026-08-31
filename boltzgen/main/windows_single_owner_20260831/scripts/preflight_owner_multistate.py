#!/usr/bin/env python3
"""CPU preflight for a materialized Windows-owner multi-state fold panel."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Mapping

import hydra
from omegaconf import OmegaConf
import torch


# The production runner deliberately invokes this file with ``python -I``.
# Isolated mode does not add the script directory to ``sys.path``, so load the
# sibling validator by its bound absolute path instead of relying on an ambient
# import path.
_SUMMARY_PATH = Path(__file__).resolve().with_name("summarize_owner_multistate.py")
_SUMMARY_SPEC = importlib.util.spec_from_file_location(
    "owner_multistate_summary_contract", _SUMMARY_PATH
)
if _SUMMARY_SPEC is None or _SUMMARY_SPEC.loader is None:
    raise RuntimeError(f"cannot load sibling validator: {_SUMMARY_PATH}")
_SUMMARY = importlib.util.module_from_spec(_SUMMARY_SPEC)
_SUMMARY_SPEC.loader.exec_module(_SUMMARY)
ValidationError = _SUMMARY.ValidationError
load_json_object = _SUMMARY.load_json_object
validate_tasks = _SUMMARY.validate_tasks
target_pairwise_distance_vector = _SUMMARY.target_pairwise_distance_vector


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_coordinate_contract(path: Path, geometry: Mapping[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ValidationError(f"coordinate contract already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise ValidationError(f"coordinate contract temporary exists: {temporary}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            import numpy as np

            np.savez_compressed(stream, **geometry)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def require_equal(checks: dict[str, object], label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ValidationError(
            f"folding config mismatch: {label} expected={expected!r} actual={actual!r}"
        )
    checks[label] = actual


def validate_config(
    root: Path,
    config: Mapping[str, object],
    *,
    runtime_root: Path,
    samples_per_task: int,
) -> dict[str, object]:
    design_dir = root / "design_inputs"
    folding_checkpoint = runtime_root / "boltz2_conf_final.ckpt"
    mols = runtime_root / "mols.zip"
    data = config.get("data")
    trainer = config.get("trainer")
    writer = config.get("writer")
    override = config.get("override")
    if not all(isinstance(value, Mapping) for value in (data, trainer, writer, override)):
        raise ValidationError("folding config is missing data/trainer/writer/override objects")
    data = dict(data)
    trainer = dict(trainer)
    writer = dict(writer)
    override = dict(override)
    cfg = data.get("cfg")
    if not isinstance(cfg, Mapping):
        raise ValidationError("folding config data.cfg must be an object")
    cfg = dict(cfg)
    checks: dict[str, object] = {}
    require_equal(checks, "task", config.get("_target_"), "boltzgen.task.predict.predict.Predict")
    require_equal(
        checks,
        "data.task",
        data.get("_target_"),
        "boltzgen.task.predict.data_from_generated.FromGeneratedDataModule",
    )
    require_equal(checks, "data.design_dir", str(data.get("design_dir")), str(design_dir))
    require_equal(checks, "data.target_templates", data.get("target_templates"), True)
    require_equal(checks, "data.return_native", data.get("return_native"), False)
    require_equal(checks, "data.fail_if_no_designs", data.get("fail_if_no_designs"), True)
    require_equal(checks, "data.skip_existing", data.get("skip_existing"), False)
    require_equal(checks, "data.cfg.batch_size", cfg.get("batch_size"), 1)
    workers = cfg.get("num_workers")
    if not isinstance(workers, int) or isinstance(workers, bool) or not 0 <= workers <= 4:
        raise ValidationError("folding data.cfg.num_workers must be an integer from 0 through 4")
    checks["data.cfg.num_workers"] = workers
    require_equal(checks, "data.cfg.moldir", str(cfg.get("moldir")), str(mols))
    require_equal(checks, "writer.design_dir", str(writer.get("design_dir")), str(design_dir))
    require_equal(checks, "trainer.accelerator", trainer.get("accelerator"), "gpu")
    require_equal(checks, "trainer.devices", trainer.get("devices"), 1)
    require_equal(checks, "trainer.precision", trainer.get("precision"), "bf16-mixed")
    require_equal(checks, "checkpoint", str(config.get("checkpoint")), str(folding_checkpoint))
    require_equal(checks, "recycling_steps", config.get("recycling_steps"), 3)
    require_equal(checks, "sampling_steps", config.get("sampling_steps"), 200)
    require_equal(checks, "diffusion_samples", config.get("diffusion_samples"), samples_per_task)
    require_equal(checks, "override.use_kernels", override.get("use_kernels"), True)
    return checks


def execute(run_root: Path, runtime_root: Path, coordinate_contract: Path) -> dict:
    root = run_root.resolve(strict=True)
    runtime = runtime_root.resolve(strict=True)
    tasks_payload = load_json_object(root / "tasks.json")
    tasks, samples = validate_tasks(root, tasks_payload)
    steps = load_json_object_from_yaml(root / "steps.yaml")
    if steps != {"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}:
        raise ValidationError("steps.yaml must contain only the folding step")
    config_path = root / "config" / "folding.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        raise ValidationError("missing or unsafe folding config")
    omega = OmegaConf.load(config_path)
    resolved = OmegaConf.to_container(omega, resolve=True)
    if not isinstance(resolved, dict):
        raise ValidationError("resolved folding config must be an object")
    checks = validate_config(
        root,
        resolved,
        runtime_root=runtime,
        samples_per_task=samples,
    )
    with contextlib.redirect_stdout(sys.stderr):
        data_module = hydra.utils.instantiate(omega.data)
    dataset = data_module.predict_set
    expected_by_id = {task["task_id"]: task for task in tasks}
    generated = list(dataset.generated_paths)
    metadata = list(dataset.metadata_paths)
    native = list(dataset.native_paths)
    if len(generated) != len(tasks) or len(metadata) != len(tasks):
        raise ValidationError("data module did not discover exactly the declared task count")
    if {path.stem for path in generated} != set(expected_by_id):
        raise ValidationError("data module task IDs disagree with tasks.json")
    sample_checks: list[dict] = []
    geometry_vectors: dict[str, object] = {}
    for generated_path, metadata_path, native_path in zip(generated, metadata, native, strict=True):
        task = expected_by_id[generated_path.stem]
        with contextlib.redirect_stdout(sys.stderr):
            feature = dataset.getitem_from_paths(metadata_path, generated_path, native_path)
        if feature.get("exception") is not False:
            raise ValidationError(f"BoltzGen data preflight failed for {generated_path.stem}")
        design_count = int(feature["design_mask"].sum().item())
        chain_design_count = int(feature["chain_design_mask"].sum().item())
        template_count = int(feature["template_mask"].sum().item())
        expected_target = len(task["target_sequence"])
        expected_vhh = len(task["vhh_sequence"])
        if design_count != task["design_mask_count"]:
            raise ValidationError(f"design mask changed during parsing: {generated_path.stem}")
        if chain_design_count != expected_vhh:
            raise ValidationError(f"chain design mask mismatch: {generated_path.stem}")
        if template_count != expected_target:
            raise ValidationError(f"target template mask mismatch: {generated_path.stem}")
        for key in ("coords", "design_mask", "chain_design_mask", "binding_type", "template_mask"):
            tensor = feature.get(key)
            if isinstance(tensor, torch.Tensor) and not torch.isfinite(tensor.float()).all():
                raise ValidationError(f"non-finite preflight feature {key}: {generated_path.stem}")
        for geometry_key in ("coords", "atom_to_token", "atom_resolved_mask"):
            if geometry_key not in feature:
                raise ValidationError(
                    f"missing preflight geometry feature {geometry_key}: {generated_path.stem}"
                )
        geometry_vector = target_pairwise_distance_vector(
            feature["coords"].detach().cpu().numpy(),
            feature["atom_to_token"].detach().cpu().numpy(),
            feature["atom_resolved_mask"].detach().cpu().numpy(),
            expected_target,
        )
        geometry_vectors[generated_path.stem] = geometry_vector
        sample_checks.append(
            {
                "task_id": generated_path.stem,
                "token_count": expected_target + expected_vhh,
                "design_mask_count": design_count,
                "chain_design_mask_count": chain_design_count,
                "target_template_count": template_count,
                "target_geometry_pair_count": int(geometry_vector.size),
            }
        )
    expected_contract = (root / "operator_logs" / "preflight_target_geometry.npz").absolute()
    requested_contract = coordinate_contract.absolute()
    if requested_contract != expected_contract:
        raise ValidationError(
            f"coordinate contract must be written to {expected_contract}, got {requested_contract}"
        )
    write_coordinate_contract(requested_contract, geometry_vectors)
    contract_relative = requested_contract.relative_to(root).as_posix()
    return {
        "schema_version": "WINDOWS_OWNER_MULTISTATE_PREFLIGHT_V1",
        "status": "PASS",
        "logical_task_count": len(tasks),
        "samples_per_task": samples,
        "expected_sample_rows": len(tasks) * samples,
        "resolved_config_checks": checks,
        "task_checks": sorted(sample_checks, key=lambda row: row["task_id"]),
        "coordinate_contract_relative_path": contract_relative,
        "coordinate_contract_sha256": sha256_file(requested_contract),
        "coordinate_identity": "TARGET_RESOLVED_ATOM_PAIRWISE_DISTANCES_FLOAT32",
        "training_performed": False,
    }


def load_json_object_from_yaml(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"missing or unsafe YAML: {path}")
    payload = OmegaConf.to_container(OmegaConf.load(path), resolve=True)
    if not isinstance(payload, dict):
        raise ValidationError(f"expected YAML object: {path}")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--coordinate-contract", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        contract = execute(args.run_root, args.runtime_root, args.coordinate_contract)
    except (ValidationError, OSError, ValueError) as exc:
        raise SystemExit(f"MULTISTATE_PREFLIGHT_FAILED: {exc}") from exc
    if not all(
        math.isfinite(float(row["token_count"])) for row in contract["task_checks"]
    ):
        raise SystemExit("MULTISTATE_PREFLIGHT_FAILED: invalid task count")
    print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
