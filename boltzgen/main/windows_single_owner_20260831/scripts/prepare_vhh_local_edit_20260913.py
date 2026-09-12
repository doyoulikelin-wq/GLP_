#!/usr/bin/env python3
"""Prepare a bounded local sequence-edit/inpainting control, without GPU.

Inputs: completed historical C-arm raw-generation CIF/metadata, existing runtime,
and measured short-peptide calibration receipt. Outputs: a fresh private bundle,
source bindings, deterministic five-residue window selection, and CPU contracts.
No input overwrite, model inference, training, upload, or biological PASS occurs.
The edit mask is NOT the full-CDR evaluation mask; both are retained explicitly.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import gemmi
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnose_vhh_terminal_interface as D
from prepare_vhh_terminal_controls import cpu_features

ARMS = ("ORIGINAL", "SEQUENCE_ONLY", "LOCAL_INPAINT")
SCHEMA = "VHH_LOCAL_EDIT_PREPARATION_V1"
IMPLEMENTATIONS = (
    "prepare_vhh_local_edit_20260913.py", "run_vhh_local_edit_20260913.py", "score_vhh_local_edit_20260913.py",
    "prepare_vhh_terminal_controls.py", "diagnose_vhh_terminal_interface.py",
    "prepare_vhh_sequence_retention.py", "prepare_vhh_native_control.py",
    "run_vhh_sequence_retention.py", "run_vhh_short_peptide_control.py",
    "evaluate_vhh_epitope.py", "run_owner_t12_split_template.py",
)


def sha(path):
    """Hash one known small input or implementation file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def binding(path):
    """Bind an existing ordinary source without silently resolving a symlink."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("ordinary existing source file required")
    return {"path": str(path.resolve()), "sha256": sha(path)}


def implementation_hashes():
    """Freeze authored implementations after all participating files exist."""
    return {name: sha(Path(__file__).with_name(name)) for name in IMPLEMENTATIONS}


def write_json(path, value):
    """Create a new JSON receipt without replacing prior outcomes."""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def select_window(folds, cdr_ranges, width=5):
    """Enumerate same-CDR windows and minimize mean residue-to-terminal distance.

For each source and each residue, compute separate minimum heavy-atom distances
to target His7 and Ala8. Average over both sources, both terminals, and all five
window residues. Ties resolve by CDR index then start. No new GPU result is read.
"""
    if len(folds) != 2 or width != 5:
        raise ValueError("exactly two source draws and a fixed five-residue window required")
    profiles = []
    for fold in folds:
        xyz = np.asarray(fold["coords"])[0]
        token = np.asarray(fold["atom_to_token"])[0].argmax(axis=-1)
        if not np.isfinite(xyz).all() or np.any(np.all(xyz == 0, axis=-1)):
            raise ValueError("source heavy atoms must be finite and non-placeholder")
        profiles.append({r: [float(np.linalg.norm(
            xyz[token == r, None, :] - xyz[None, token == end, :], axis=-1).min())
            for end in (0, 1)] for start, stop in cdr_ranges for r in range(30+start-1, 30+stop)})
    candidates = []
    for cdr_index, (start, stop) in enumerate(cdr_ranges, 1):
        if stop < start or start < 1 or stop > 121:
            raise ValueError("invalid one-based VHH CDR ranges")
        for window_start in range(start, stop-width+2):
            tokens = list(range(30+window_start-1, 30+window_start-1+width))
            per_source = [float(np.mean([profile[t] for t in tokens])) for profile in profiles]
            candidates.append({"cdr_index": cdr_index, "vhh_start_one_based": window_start,
                "vhh_end_one_based": window_start+width-1, "tokens_zero_based": tokens,
                "mean_distance_to_both_terminal_residues_angstrom": float(np.mean(per_source)),
                "source_mean_distances_angstrom": per_source})
    if not candidates:
        raise ValueError("no within-CDR five-residue window available")
    ordered = sorted(candidates, key=lambda r: (r["mean_distance_to_both_terminal_residues_angstrom"],
        r["cdr_index"], r["vhh_start_one_based"]))
    return {"rule": "MIN_MEAN_PER_RESIDUE_HEAVY_ATOM_DISTANCE_TO_HIS7_AND_ALA8_ACROSS_BOTH_RAW_C_SOURCES",
        "prospective_to_new_gpu_results": True, "width": width, "candidate_window_count": len(ordered),
        "selected": ordered[0], "all_windows_ranked": ordered,
        "limitation": "Uses historical generated geometry, not binding truth or an independently validated epitope."}


def edit_spec(window, arm):
    """Build two explicit local editing modes with equal, unspecified binding labels."""
    if arm not in ARMS[1:]:
        raise ValueError("spec only exists for the two editing arms")
    start, end = window["vhh_start_one_based"], window["vhh_end_one_based"]
    if end-start != 4:
        raise ValueError("edit window must have exactly five consecutive residues")
    groups = [{"group": {"id": "A", "visibility": 1}},
              {"group": {"id": "B", "visibility": 1}}]
    if arm == "LOCAL_INPAINT":
        groups.append({"group": {"id": "B", "visibility": 0, "res_index": f"{start}..{end}"}})
    return {"entities": [{"file": {"path": "input.cif", "include": [
        {"chain": {"id": "A", "res_index": "1..30"}}, {"chain": {"id": "B", "res_index": "1..121"}}],
        "design": [{"chain": {"id": "B", "res_index": f"{start}..{end}"}}],
        "structure_groups": groups}}]}


def make_config(source_config, inverse_template, runtime, spec, output, arm):
    """Configure one fresh upstream Predict task; never initialize its model."""
    if arm not in ARMS[1:]:
        raise ValueError("unsupported editing arm")
    config = copy.deepcopy(inverse_template if arm == "SEQUENCE_ONLY" else source_config)
    config["checkpoint"] = str(runtime/("boltzgen1_ifold.ckpt" if arm == "SEQUENCE_ONLY" else "boltzgen1_adherence.ckpt"))
    config["output"] = str(output)
    config["name"] = arm.lower()
    config["diffusion_samples"] = 1
    config["data"]["cfg"].update(yaml_path=[str(spec)], moldir=str(runtime/"mols.zip"),
        multiplicity=1, skip_existing=False, output_dir="${output}")
    config["data"]["num_workers"] = 0
    config["data"]["batch_size"] = 1
    config["override"]["use_kernels"] = True
    if arm == "SEQUENCE_ONLY":
        config["override"]["inverse_fold_args"]["inverse_fold_restriction"] = ["CYS"]
    return config


def validate_cpu(arrays, masked, expected_ids, edit_tokens, arm):
    """Require exact sequence/length/edit masks and explicit structural visibility."""
    edit = np.isin(np.arange(151), edit_tokens)
    ids = np.asarray(arrays["res_type"]).argmax(axis=-1)
    if ids.shape != (151,) or not np.array_equal(ids[~edit], np.asarray(expected_ids)[~edit]):
        raise ValueError("CPU parsing changed fixed sequence or chain length")
    if not np.array_equal(np.asarray(arrays["design_mask"], dtype=bool), edit):
        raise ValueError("only the registered five residues may be designed")
    if not np.array_equal(np.asarray(masked["design_mask"], dtype=bool), edit):
        raise ValueError("model masking changed editable residue selection")
    if np.any(arrays["binding_type"] != 0) or np.any(masked["binding_type"] != 0):
        raise ValueError("both editing arms require unspecified binding labels")
    expected_groups = np.ones(151)
    if arm == "LOCAL_INPAINT":
        expected_groups[edit] = 0
    elif arm != "SEQUENCE_ONLY":
        raise ValueError("unexpected arm")
    if not np.array_equal(arrays["structure_group"], expected_groups):
        raise ValueError("structural conditioning group differs from registered edit mechanism")
    dist_mask = np.asarray(arrays["token_distance_mask"])
    expected_dist = (expected_groups[:, None] == expected_groups[None, :]) & (expected_groups[:, None] > 0)
    if not np.array_equal(dist_mask, expected_dist):
        raise ValueError("distance visibility disagrees with structure groups")
    atom_token = np.asarray(arrays["atom_to_token"]).argmax(axis=-1)
    bb = np.asarray(arrays["backbone_mask"], dtype=bool)
    if any(np.sum(bb & (atom_token == token)) != 4 for token in range(151)):
        raise ValueError("exact four backbone atoms per residue required")
    return {"status": "PASS", "token_count": 151, "target_tokens": 30, "vhh_tokens": 121,
        "edit_token_count": 5, "fixed_token_count": 146, "target_not_designed": True,
        "outside_window_identity_preserved": True, "binding_types_all_unspecified": True,
        "cross_target_framework_relative_geometry_conditioned": True,
        "window_backbone_conditioned": arm == "SEQUENCE_ONLY",
        "full_cdr_mask_reserved_for_final_freefold": True}


def prepare(index_path, output, runtime, calibration):
    """Create and CPU-validate every source/arm without selecting successful draws."""
    import boltzgen
    from omegaconf import OmegaConf
    from run_vhh_short_peptide_control import validate_calibration_receipt

    output, runtime = Path(output).absolute(), Path(runtime).resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise ValueError("preparation output must be new")
    validate_calibration_receipt(calibration)
    index = json.loads(Path(index_path).read_text())
    if index.get("status") != "COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY":
        raise ValueError("historical terminal control must be complete")
    cells = [cell for cell in index["cells"] if cell["condition_id"] == "C"]
    if len(cells) != 1 or cells[0].get("strict_output_check", {}).get("status") != "PASS":
        raise ValueError("exactly the complete and strictly checked C arm required")
    cell = cells[0]
    source, attempt = Path(cell["spec_path"]).parent, Path(cell["attempt_root"])
    sources = {str(Path(index_path).resolve()): sha(index_path)}
    for name, digest in cell["spec_hashes"].items():
        if sha(source/name) != digest:
            raise ValueError("historical source changed")
        sources[str(source/name)] = digest
    cdr_ranges = cell["cdr_ranges_one_based"]
    cdr, _ = D.cdr_annotation(yaml.safe_load((source/"scaffold.yaml").read_text()))
    if cdr != [30+i-1 for start, end in cdr_ranges for i in range(start, end+1)]:
        raise ValueError("full CDR annotation does not match source ranges")
    directory = attempt/"intermediate_designs"
    if {p.stem for p in directory.glob("design_*.cif")} != {"design_0", "design_1"}:
        raise ValueError("exactly both original C source draws required")
    inputs, folds = [], []
    for number in range(2):
        cif, meta = directory/f"design_{number}.cif", directory/f"design_{number}.npz"
        _, fold = D.cif_arrays(cif.read_bytes(), cdr)
        with np.load(meta, allow_pickle=False) as z:
            if np.flatnonzero(z["design_mask"]).tolist() != cdr:
                raise ValueError("source full CDR metadata mismatch")
        inputs.append((cif, meta)); folds.append(fold)
        sources.update({str(cif): sha(cif), str(meta): sha(meta)})
    selection = select_window(folds, cdr_ranges)
    selected = selection["selected"]
    upstream = Path(boltzgen.__file__).parent
    inverse_path = upstream/"resources/config/inverse_fold_only.yaml"
    source_config_path = attempt/"config/design.yaml"
    sources.update({str(inverse_path): sha(inverse_path), str(source_config_path): sha(source_config_path)})
    source_config = yaml.safe_load(source_config_path.read_text())
    inverse_template = yaml.safe_load(inverse_path.read_text())
    output.mkdir(parents=True, mode=0o700)
    tasks = []
    for number, ((cif, meta), fold) in enumerate(zip(inputs, folds)):
        ids = fold["res_type"][0].argmax(axis=-1).tolist()
        for arm in ARMS:
            task_id = f"c{number}_{arm.lower()}"
            task = {"task_id": task_id, "source_id": f"c{number}", "source_draw": number,
                "arm": arm, "source_cif": binding(cif), "source_npz": binding(meta),
                "spec": None, "config": None, "edit_tokens": selected["tokens_zero_based"],
                "edit_window_tokens": selected["tokens_zero_based"], "source_residue_ids": ids,
                "full_cdr_tokens": cdr, "expected_residue_ids": ids, "fold_samples": 3}
            if arm != "ORIGINAL":
                folder = output/"specs"/task_id
                folder.mkdir(parents=True)
                with (folder/"input.cif").open("xb") as stream:
                    stream.write(cif.read_bytes())
                with (folder/"spec.yaml").open("x") as stream:
                    yaml.safe_dump(edit_spec(selected, arm), stream, sort_keys=False)
                config = make_config(source_config, inverse_template, runtime, folder/"spec.yaml",
                    output/"stage_outputs"/task_id, arm)
                with (folder/"config.yaml").open("x") as stream:
                    yaml.safe_dump(config, stream, sort_keys=False)
                arrays, masked, _ = cpu_features(OmegaConf.create(config), folder/"spec.yaml", 0,
                    20260913+number, config["override"]["masker_args"])
                task["cpu_preflight"] = validate_cpu(arrays, masked, ids, selected["tokens_zero_based"], arm)
                mapping = {key: arrays[key] for key in ("atom_to_token", "atom_pad_mask", "backbone_mask",
                    "ref_atom_name_chars", "design_mask", "structure_group", "token_distance_mask", "res_type")}
                np.savez_compressed(folder/"cpu_atom_mapping.npz", **mapping)
                task.update(spec=binding(folder/"spec.yaml"), config=binding(folder/"config.yaml"),
                    cpu_atom_mapping=binding(folder/"cpu_atom_mapping.npz"))
            tasks.append(task)
    outputs = {str(p.relative_to(output)): sha(p) for p in output.rglob("*") if p.is_file()}
    implementation = implementation_hashes()
    plan = {"schema": SCHEMA, "status": "CPU_READY", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(runtime), "tasks": tasks, "selection": selection,
        "hard_timeout_seconds": 1200, "expected_fold_samples": 18,
        "folds_per_task": 3, "automatic_retry": False,
        "full_cdr_ranges_one_based": cdr_ranges, "full_cdr_tokens": cdr,
        "target_reference": {**binding(source/"target.cif"), "target_chain_id": "E", "res_index": "1..30"},
        "calibration_private_binding": binding(calibration), "source_files": sources,
        "output_sha256": outputs, "implementation_sha256": implementation,
        "budget": {"hard_timeout_seconds": 1200, "candidate_count": 6, "editing_jobs": 4,
            "folds_per_candidate": 3, "total_freefold_samples": 18, "automatic_retry": False},
        "cpu_preflight": {"status": "PASS", "edited_task_count": 4, "original_task_count": 2},
        "gpu_started": False, "biological_pass": False,
        "input_scope": "ALL_TWO_ORIGINAL_C_DRAWS_NOT_IF_PLACEHOLDER_STRUCTURES",
        "sequence_only_checkpoint": "boltzgen1_ifold.ckpt", "inpainting_checkpoint": "boltzgen1_adherence.ckpt",
        "generation_hints": "Original target/VHH outside-window relative geometry retained in both editing contexts; no binding labels. IF window backbone retained; inpainting window distance visibility removed.",
        "evaluation_contract": "All six candidates use fixed sequence, identical original target-only template, no VHH template, no binding labels; score all original CDRs, never only edit mask.",
        "randomness": "Two source-paired candidates per arm; stochastic predictions are not common-random-number pairs.",
        "limits": ["One edit per source and three nested free predictions are a feasibility pilot, not a powered efficacy test.",
            "Local generation uses geometry context; free evaluation must remove target/VHH relative-pose conditioning.",
            "Terminal chemical state remains unverified; no affinity, selectivity or biological binding claims.",
            "Sequence-only and inpainting use different pretrained mechanisms, not a controlled single-variable model comparison."]}
    write_json(output/"PRIVATE_PLAN.json", plan)
    return plan


def main():
    """Command-line CPU preparation; failure leaves its new directory for audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("index", "output", "runtime-root", "calibration"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.index, args.output, args.runtime_root, args.calibration)
    print(json.dumps({"status": result["status"], "task_count": len(result["tasks"]),
        "window": result["selection"]["selected"], "gpu_started": False}))


if __name__ == "__main__":
    main()
