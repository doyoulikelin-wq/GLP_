#!/usr/bin/env python3
"""Prepare three terminal-label controls and inspect CPU features, never infer.

CPU RNG isolation is a diagnostic device, not a change to BoltzGen or a claim
that future GPU cells share random draws. Public outputs contain no sequences.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from unittest.mock import patch

import numpy as np
import yaml

from check_vhh_generation_conditioning import checkpoint_metadata, file_digest

BUNDLE = ("design.yaml", "scaffold.yaml", "scaffold.cif", "target.cif")
CONDITIONS = {
    "A": [0] * 30,
    "B": [1, 1] + [0] * 28,
    "C": [1, 1] + [2] * 28,
}
ACTIVE_IDS = [2 + "ARNDCQEGHILKMFPSTWYV".index(letter) for letter in "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR"]


def binding(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": file_digest(path)}


def make_conditions(source):
    if not isinstance(source, dict) or len(source.get("entities", [])) != 2:
        raise ValueError("expected target and scaffold entities")
    if [entity.get("file", {}).get("path") for entity in source["entities"]] != ["target.cif", "scaffold.yaml"]:
        raise ValueError("unexpected source file references")
    target = source["entities"][0]["file"]
    if target.get("include") != [{"chain": {"id": "E", "res_index": "1..30"}}]:
        raise ValueError("expected complete active target chain E residues 1..30")
    result = {}
    for condition in CONDITIONS:
        spec = copy.deepcopy(source)
        entry = spec["entities"][0]["file"]
        entry.pop("binding_types", None)
        if condition != "A":
            chain = {"id": "E", "binding": "1..2"}
            if condition == "C":
                chain["not_binding"] = "3..30"
            entry["binding_types"] = [{"chain": chain}]
        result[condition] = spec
    normalized = []
    for spec in result.values():
        spec = copy.deepcopy(spec)
        spec["entities"][0]["file"].pop("binding_types", None)
        normalized.append(spec)
    if not all(value == normalized[0] for value in normalized):
        raise ValueError("non-binding spec content differs")
    return result


def validate_binding(arrays, expected):
    labels = np.asarray(arrays["binding_type"])
    residues = np.asarray(arrays["res_type"])
    design = np.asarray(arrays["design_mask"])
    n = len(labels)
    if n <= 30 or labels.shape != (n,) or residues.shape != (n, 33) or design.shape != (n,):
        raise ValueError("unexpected target/VHH feature shapes")
    if not np.isin(labels, [0, 1, 2]).all() or labels[:30].tolist() != expected or np.any(labels[30:] != 0):
        raise ValueError("parsed or masked binding labels do not match condition")
    if not np.isin(residues, [0, 1]).all() or not np.all(residues.sum(axis=1) == 1):
        raise ValueError("invalid residue encoding")
    if residues.argmax(axis=1)[:30].tolist() != ACTIVE_IDS:
        raise ValueError("target must be full active GLP1, starting His/Ala")
    if not np.isin(design, [0, 1]).all() or design[:30].any() or not design[30:].any():
        raise ValueError("target must be fixed and VHH must contain designed CDRs")
    groups, mask = arrays["structure_group"], arrays["token_distance_mask"]
    expected_mask = (groups[:, None] == groups[None, :]) & (groups[:, None] > 0)
    if not np.array_equal(mask, expected_mask) or np.any(mask[:30, 30:]):
        raise ValueError("unexpected cross-group geometry conditioning")
    return {"target_binding_types": labels[:30].tolist(), "vhh_binding_types_all_unspecified": True,
            "target_token_count": 30, "vhh_token_count": n - 30,
            "cdr_token_count": int(design.sum()), "target_vhh_visible_distance_pair_count": 0}


def differing_arrays(first, second, exclude=("binding_type",)):
    if set(first) != set(second):
        raise ValueError("CPU feature tensor keys differ")
    return sorted(key for key in first if key not in exclude and not np.array_equal(first[key], second[key], equal_nan=True))


@contextmanager
def cpu_rng(seed):
    """Scoped CPU-only RNG control; all global RNG state is restored."""
    import torch
    python_state, numpy_state = random.getstate(), np.random.get_state()
    original_default_rng = np.random.default_rng
    try:
        with torch.random.fork_rng(devices=[]):
            random.seed(seed)
            np.random.seed(seed)
            torch.random.default_generator.manual_seed(seed)
            with patch.object(np.random, "default_rng", lambda value=None: original_default_rng(seed if value is None else value)):
                yield
    finally:
        random.setstate(python_state)
        np.random.set_state(numpy_state)


def cpu_features(config, spec_path, sample_index, seed, masker_args):
    import torch
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    from boltzgen.model.modules.masker import BoltzMasker
    config = OmegaConf.create(OmegaConf.to_container(config, resolve=True))
    config.data.cfg.yaml_path = [str(spec_path)]
    # Instantiate only the data module, never Predict/model/Trainer or CUDA.
    with cpu_rng(seed):
        module = instantiate(config.data)
        sample = module.predict_set[sample_index]
        arrays = {key: value.detach().cpu().numpy().copy() for key, value in sample.items() if isinstance(value, torch.Tensor)}
        masked = BoltzMasker(**masker_args)(module.collate([sample]))
        masked_arrays = {key: masked[key][0].detach().cpu().numpy().copy() for key in
                         ("binding_type", "res_type", "design_mask", "structure_group", "token_distance_mask")}
    return arrays, masked_arrays, sorted(set(sample) - set(arrays))


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def prepare(source_spec, design_config, output, seed=20260911):
    import boltzgen
    from omegaconf import OmegaConf
    output = Path(output).absolute()
    source_spec, design_config = Path(source_spec).resolve(strict=True), Path(design_config).resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new private directory")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 2:
        raise ValueError("CPU seed must be an integer from 0 through 2**32-2")
    if source_spec.name != "design.yaml" or set(path.name for path in source_spec.parent.iterdir()) != set(BUNDLE):
        raise ValueError("source must be a closed four-file spec bundle")
    if any((source_spec.parent / name).is_symlink() or not (source_spec.parent / name).is_file() for name in BUNDLE):
        raise ValueError("source bundle must contain ordinary files")
    source = yaml.safe_load(source_spec.read_text())
    variants = make_conditions(source)
    scaffold = yaml.safe_load((source_spec.parent / "scaffold.yaml").read_text())
    if scaffold.get("path") != "scaffold.cif":
        raise ValueError("scaffold must reference local scaffold.cif")
    config = OmegaConf.load(design_config)
    if config.data._target_ != "boltzgen.task.predict.data_from_yaml.FromYamlDataModule":
        raise ValueError("unsupported generation data path")
    if config.data.cfg.skip_existing or config.data.cfg.multiplicity != 2 or config.data.batch_size != 1:
        raise ValueError("expected fresh batch-1 two-candidate generation configuration")
    checkpoint = Path(config.checkpoint).resolve(strict=True)
    if checkpoint.name != "boltzgen1_adherence.ckpt":
        raise ValueError("controls require the same existing adherence checkpoint")
    metadata = checkpoint_metadata(checkpoint)
    effective = config.get("override", {}).get("embedder_args", metadata["embedder_args"])
    if not effective.get("add_binding_specification", False) or not metadata["binding_embedding_present"]:
        raise ValueError("trained binding-conditioning branch is not enabled")
    masker_args = dict(config.get("override", {}).get("masker_args", metadata["checkpoint_masker_args"]))
    inputs = {name: binding(source_spec.parent / name) for name in BUNDLE}
    source_root = Path(boltzgen.__file__).parent
    runtime_sources = ["data/parse/schema.py", "task/predict/data_from_yaml.py", "data/feature/featurizer.py",
                       "model/modules/masker.py", "model/modules/trunk.py", "task/predict/predict.py", "model/modules/diffusion.py"]
    implementations = {name: binding(source_root / name) for name in runtime_sources}
    implementations["prepare_vhh_terminal_controls.py"] = binding(__file__)
    implementations["check_vhh_generation_conditioning.py"] = binding(Path(__file__).with_name("check_vhh_generation_conditioning.py"))
    output.mkdir(parents=True, exist_ok=False)
    (output / "features").mkdir()
    cells, baselines = [], {}
    for condition, spec in variants.items():
        root = output / "specs" / condition
        root.mkdir(parents=True)
        for name in BUNDLE:
            payload = yaml.safe_dump(spec, sort_keys=False).encode() if name == "design.yaml" else (source_spec.parent / name).read_bytes()
            with (root / name).open("xb") as handle:
                handle.write(payload)
        samples = []
        for sample_index in range(2):
            arrays, masked, ignored = cpu_features(config, root / "design.yaml", sample_index, seed + sample_index, masker_args)
            parsed_check = validate_binding(arrays, CONDITIONS[condition])
            masked_check = validate_binding(masked, CONDITIONS[condition])
            if sample_index not in baselines:
                baselines[sample_index] = arrays
            different = differing_arrays(baselines[sample_index], arrays)
            if different:
                raise ValueError(f"condition {condition} has additional CPU-controlled tensor differences: {different}")
            snapshot = output / "features" / f"{condition}_sample{sample_index}.npz"
            with snapshot.open("xb") as handle:
                np.savez_compressed(handle, **arrays, masked_binding_type=masked["binding_type"])
            samples.append({"sample_index": sample_index, "cpu_diagnostic_seed": seed + sample_index,
                            "parsed": parsed_check, "masked": masked_check, "feature_snapshot": binding(snapshot),
                            "non_tensor_keys_not_compared": ignored, "non_binding_tensor_differences_under_cpu_rng_isolation": different,
                            "compared_tensor_key_count": len(arrays) - 1})
        cells.append({"condition_id": condition, "spec_path": str(root / "design.yaml"),
                      "spec_hashes": {name: file_digest(root / name) for name in BUNDLE},
                      "expected_target_binding_types": CONDITIONS[condition], "samples": samples})
    if any(file_digest(source_spec.parent / name) != inputs[name]["sha256"] for name in BUNDLE):
        raise ValueError("source inputs changed during preparation")
    seed_policy = {"cpu_diagnostic_seed": seed, "cpu_rng_isolation_only": True,
                   "gpu_seed": None, "gpu_seed_mode": "UPSTREAM_UNSEEDED_ENTROPY_NOT_RECORDED",
                   "gpu_common_random_numbers_guaranteed": False,
                   "reason": "PredictionDataset uses np.random.default_rng(None); DataLoader has no explicit generator/worker_init_fn; Predict has no seed parameter.",
                   "interpretation": "Equal-budget stochastic exploratory groups, not biological pairs or a causal estimate from shared random draws.",
                   "cpu_feature_comparison_scope": "All tensor features compared with diagnostic-only common CPU RNG; actual unseeded GPU data features need not be identical."}
    public = {"schema": "VHH_TERMINAL_CONTROLS_PREPARATION_V1", "status": "CPU_PREPARATION_COMPLETE",
              "created_at_utc": datetime.now(timezone.utc).isoformat(), "condition_count": 3,
              "conditions": [{"condition_id": cell["condition_id"], "expected_target_binding_types": cell["expected_target_binding_types"],
                              "parsed_and_masked_binding_verified": True, "source_content_only_binding_types_differ": True,
                              "cpu_controlled_non_binding_tensor_equality_verified": True} for cell in cells],
              "budget": {"candidates_per_condition": 2, "folds_per_candidate": 5, "total_candidates": 6, "total_folds": 30, "gpu_hard_cap_seconds": 1800},
              "checkpoint_name": checkpoint.name, "seed_policy": seed_policy,
              "gpu_started": False, "training_performed": False, "model_weights_modified": False,
              "biological_pass": False, "automatic_paired_evaluation": False,
              "claim_boundary": "Model conditioning-response exploration only; no binding, affinity, selectivity or causal efficacy claim.",
              "limits": ["BINDING/NOT_BINDING are soft learned labels, not guaranteed geometric constraints.",
                         "Native GLP1 terminal chemistry is not atomically established by these geometry inputs.",
                         "Only CPU feature construction is RNG-isolated; the production generator is unchanged."]}
    private = {**public, "cells": cells, "source_spec_binding": inputs,
               "source_design_config_binding": binding(design_config), "source_implementation": implementations,
               "checkpoint_path": str(checkpoint), "checkpoint_diagnostic": metadata,
               "effective_masker_args": masker_args}
    write_new(output / "private.json", private)
    write_new(output / "public.json", public)
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-spec", type=Path, required=True)
    parser.add_argument("--design-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260911)
    args = parser.parse_args()
    result = prepare(args.source_spec, args.design_config, args.output, args.seed)
    print(json.dumps({"status": result["status"], "condition_count": 3, "gpu_started": False}))


if __name__ == "__main__":
    main()
