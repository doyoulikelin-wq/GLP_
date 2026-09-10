#!/usr/bin/env python3
"""CPU-only input/evaluator compatibility checks; no model or GPU is loaded.

The actual FromYaml parser is used twice: generation atom14 representation and
canonical-source atom representation. Source CDR identities are NOT predictions;
atom14 design placeholders are never treated as completed candidate atoms. The
unchanged strict E evaluator checks the canonical source globally. After real
generation/ifold/folding, the real full candidate must still pass E globally.
No coordinates, resolved masks or source structures are repaired or overwritten.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E

TARGET_SEQUENCE = "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR"
ONE_LETTER = "ARNDCQEGHILKMFPSTWYV"
EXPECTED_TARGET_IDS = np.array([ONE_LETTER.index(aa)+2 for aa in TARGET_SEQUENCE])
KEYS = ("coords", "atom_to_token", "atom_resolved_mask", "token_index", "res_type", "mol_type",
        "design_mask", "atom_pad_mask", "ref_element", "ref_atom_name_chars")


def arrays(features: dict) -> dict:
    """Extract CPU arrays without changing their masks, identities or coordinates."""
    return {key: value.detach().cpu().numpy() if hasattr(value, "detach") else np.asarray(value)
            for key, value in features.items() if key in KEYS}


def inspect_features(features: dict, *, representation: str = "canonical_source") -> dict:
    """Validate actual unbatched features; return diagnostics, never biological PASS."""
    if representation not in ("canonical_source", "generation_atom14"):
        raise ValueError("unsupported feature representation")
    data = arrays(features)
    if set(KEYS) - data.keys():
        raise E.ValidationError("required actual atom-identity features are missing")
    design = np.asarray(data["design_mask"])
    if design.ndim != 1 or len(design) <= 30:
        raise E.ValidationError("expected target30 followed by VHH")
    n = len(design)
    design = E._binary(design, (n,), "design_mask")
    if design[:30].any() or not design[30:].any():
        raise E.ValidationError("CDR mask must be nonempty and exclude all target tokens")
    residues = E._binary(data["res_type"], (n, 33), "res_type")
    if not np.all(residues.sum(axis=1) == 1):
        raise E.ValidationError("res_type must be one-hot")
    residue_ids = residues.argmax(axis=1)
    if not np.array_equal(residue_ids[:30], EXPECTED_TARGET_IDS):
        raise E.ValidationError("target identity is not the complete canonical active GLP1 sequence")
    if np.any(residue_ids < 2) or np.any(residue_ids > 21):
        raise E.ValidationError("noncanonical source residues unsupported")
    xyz = data["coords"]
    if xyz.ndim == 2:
        xyz = xyz[None]
    if xyz.ndim != 3 or xyz.shape[0] != 1 or xyz.shape[2] != 3 or not np.isfinite(xyz).all():
        raise E.ValidationError("source coordinates must be finite singleton Nx3, including padding")
    atom_count = xyz.shape[1]
    mapping = E._binary(data["atom_to_token"], (atom_count, n), "atom_to_token")
    counts = mapping.sum(axis=1)
    if np.any(counts > 1):
        raise E.ValidationError("multiple atom-token assignment")
    assigned = counts == 1
    pad = E._binary(data["atom_pad_mask"], (atom_count,), "atom_pad_mask")
    resolved = E._binary(data["atom_resolved_mask"], (atom_count,), "atom_resolved_mask")
    if not np.array_equal(assigned, pad):
        raise E.ValidationError("actual atom padding disagrees with assignment")
    if not np.array_equal(data["token_index"], np.arange(n)) or data["mol_type"].shape != (n,) or np.any(data["mol_type"] != 0):
        raise E.ValidationError("canonical protein token indexing/type contract failed")
    elements = np.asarray(data["ref_element"])
    if elements.ndim != 2 or elements.shape[0] != atom_count or not np.isin(elements, (0, 1)).all():
        raise E.ValidationError("invalid element one-hot representation")
    if not np.all(elements.sum(axis=1)[assigned] == 1) or not np.isin(elements.sum(axis=1)[~assigned], (0, 1)).all():
        raise E.ValidationError("assigned element identity must be one-hot; padding may be all zero")
    atomic_numbers = elements.argmax(axis=1)
    token = mapping.argmax(axis=1)
    fixed = assigned & ~design[token]
    heavy = assigned & (atomic_numbers > 1) & (atomic_numbers <= 118)
    if np.any(fixed & ~heavy):
        raise E.ValidationError("fixed source atoms must have actual heavy-element identity")
    missing_fixed = fixed & heavy & ~resolved
    summary = {"representation": representation, "token_count": n, "cdr_token_count": int(design.sum()),
               "assigned_atoms": int(assigned.sum()), "fixed_heavy_atoms": int((fixed & heavy).sum()),
               "unresolved_fixed_heavy_atoms": int(missing_fixed.sum()),
               "unresolved_target_heavy_atoms": int((missing_fixed & (token < 30)).sum()),
               "unresolved_framework_heavy_atoms": int((missing_fixed & (token >= 30)).sum()),
               "unresolved_framework_token_indices": sorted(set(token[missing_fixed & (token >= 30)].tolist())),
               "source_target_identity_valid": True, "finite_coordinates": True,
               "generation_cdr_placeholders_excluded_from_fixed_atom_diagnosis": representation == "generation_atom14",
               "strict_evaluator_source_pass": False}
    if representation == "canonical_source":
        fold = {key: data[key][None] for key in ("atom_to_token", "atom_resolved_mask", "token_index", "res_type", "mol_type")}
        fold["coords"] = xyz
        try:
            # No cropped/masked or synthetic replacement coordinates are supplied.
            E.evaluate_arrays({"design_mask": design}, fold, target_tokens=list(range(30)))
            if not np.array_equal(heavy, assigned):
                raise E.ValidationError("canonical source assigned atoms include unknown/non-heavy atoms")
            summary["strict_evaluator_source_pass"] = True
        except E.ValidationError as exc:
            summary["strict_evaluator_source_error"] = str(exc)
    summary["status"] = "PASS" if not missing_fixed.any() and (representation != "canonical_source" or summary["strict_evaluator_source_pass"]) else "REJECT"
    return summary


def fixed_signatures(features: dict) -> list[tuple]:
    """Compare real fixed atom identities/resolution across source representations."""
    data = arrays(features)
    mapping = data["atom_to_token"]
    assigned = mapping.sum(axis=1) == 1
    token = mapping.argmax(axis=1)
    fixed = assigned & ~data["design_mask"].astype(bool)[token]
    chars = data["ref_atom_name_chars"].argmax(axis=-1)
    names = ["".join(chr(int(c)+32) for c in row).strip() for row in chars]
    elements = data["ref_element"].argmax(axis=-1)
    return sorted((int(token[i]), names[i], int(elements[i]), bool(data["atom_resolved_mask"][i])) for i in np.flatnonzero(fixed))


def inspect_spec(spec_path: Path, design_config: Path, *, return_arrays: bool = False) -> dict:
    """Parse one actual source spec; canonical settings apply only to CPU diagnosis."""
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    config = OmegaConf.load(design_config)
    if config.data._target_ != "boltzgen.task.predict.data_from_yaml.FromYamlDataModule":
        raise ValueError("must reuse actual FromYaml generation data path")
    config.data.cfg.yaml_path = [str(spec_path.resolve(strict=True))]
    config.data.cfg.skip_existing = False
    config.data.cfg.multiplicity = 1
    config.data.num_workers = 0
    config.data.cfg.output_dir = None
    actual = instantiate(config.data).predict_set[0]
    generation = inspect_features(actual, representation="generation_atom14")
    # The second CPU parse is not a runtime model/config override: it exposes the
    # same source CCD atoms without replacing CDR positions by atom14 placeholders.
    source_config = OmegaConf.create(OmegaConf.to_container(config, resolve=True))
    source_config.data.cfg.atom14 = False
    source_config.data.cfg.atom37 = False
    source_config.data.cfg.backbone_only = False
    canonical = instantiate(source_config.data).predict_set[0]
    source = inspect_features(canonical, representation="canonical_source")
    equal = fixed_signatures(actual) == fixed_signatures(canonical)
    if not equal:
        raise E.ValidationError("fixed target/framework atom identities differ between generation and canonical parse")
    label = spec_path.parent.name
    if not re.fullmatch(r"\d{2}_pdb_[a-z0-9]{8}-[A-Za-z0-9]+", label):
        label = "controlled_spec"
    result = {"source_spec_label": label, "status": "PASS" if generation["status"] == source["status"] == "PASS" else "REJECT",
              "fixed_atom_identity_resolution_equivalent": equal,
              "generation_features": generation, "canonical_source_features": source,
              "postgeneration_full_candidate_evaluation_still_required": True,
              "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED"}
    result["ready"] = result["status"] == "PASS"
    if return_arrays:
        result["private_arrays"] = {"generation": arrays(actual), "canonical_source": arrays(canonical)}
    return result


def execute(design_config: Path, specs: list[Path], output: Path) -> dict:
    """Write fresh private feature snapshots and an allowlisted public diagnostic."""
    started = time.monotonic()
    if output.exists() or output.is_symlink() or any((p / ".git").exists() for p in (output, *output.parents)):
        raise ValueError("new private output outside Git required")
    rows, snapshots, inputs = [], {}, []
    for index, spec in enumerate(specs):
        result = inspect_spec(spec, design_config, return_arrays=True)
        snapshots[index] = result.pop("private_arrays")
        rows.append(result)
        inputs.append({"spec_path": str(spec.resolve()), "source_hashes": {name: hashlib.sha256((spec.parent/name).read_bytes()).hexdigest()
            for name in ("design.yaml", "scaffold.yaml", "scaffold.cif", "target.cif")}})
    public = {"schema": "VHH_EVALUATOR_COMPATIBILITY_V1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "CPU_PREFLIGHT_COMPLETE", "gpu_started": False, "weights_loaded": False,
              "source_coordinates_modified": False, "resolved_mask_modified": False, "local_evaluator_substitution": False,
              "strict_evaluator_sha256": hashlib.sha256(Path(E.__file__).read_bytes()).hexdigest(),
              "spec_count": len(rows), "passed_spec_count": sum(row["status"] == "PASS" for row in rows),
              "rejected_spec_count": sum(row["status"] == "REJECT" for row in rows), "specs": rows,
              "scope": "Input-source necessary compatibility screen; not sufficient proof for not-yet-generated candidate atoms",
              "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED",
              "required_runtime_check": "Run the unchanged full-atom E evaluator on every actual completed candidate/fold; never substitute a local subset",
              "biological_pass": False, "elapsed_seconds": time.monotonic()-started}
    private = {**public, "design_config_path": str(design_config.resolve()),
               "design_config_sha256": hashlib.sha256(design_config.read_bytes()).hexdigest(), "inputs": inputs}
    output.mkdir(parents=True, exist_ok=False)
    for index, stages in snapshots.items():
        for stage, data in stages.items():
            with (output/f"spec_{index}_{stage}.npz").open("xb") as stream:
                np.savez_compressed(stream, **data)
    for name, result in (("PUBLIC_COMPATIBILITY.json", public), ("PRIVATE_COMPATIBILITY.json", private)):
        with (output/name).open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    return public


def main() -> int:
    """CLI returns completion of the diagnostic, not acceptance of rejected specs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design-config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = execute(args.design_config, args.spec, args.output.absolute())
    print(json.dumps({key: result[key] for key in ("status", "passed_spec_count", "rejected_spec_count", "elapsed_seconds")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
