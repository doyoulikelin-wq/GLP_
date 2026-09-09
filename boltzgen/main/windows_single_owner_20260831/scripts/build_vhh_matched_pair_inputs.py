#!/usr/bin/env python3
"""Build paired active/His-Ala-deleted GLP-1 inputs for fixed new VHH sequences.

This is a CPU-only input builder, not a selection or binding experiment. It does
not read a lockbox, invent sequences, align a different target structure, or run
inference. The truncated state is a matched geometric deletion, not a claim of
physiological truncated-state geometry or atomically verified terminal chemistry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import gemmi
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_owner_multistate_inputs import (atomic_write, chain_sequence, load_metadata,
    load_structure, parse_runtime_manifest, select_candidate_chains, stable_digest,
    write_json, write_npz)
from prepare_vhh_native_control import control_config
from vhh_stage_gate import candidate_set_digest

ACTIVE_GLP1 = "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR"
MAX_CANDIDATES = 6
STATES = (("active", "GLP1_7_36_NH2", 0), ("truncated", "GLP1_9_36_NH2", 2))
SAFE_PREFIX = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_]{0,40}")
DESIGN_ID = re.compile(r"design_[0-9]+")


def sequence_digest(sequence: str) -> str:
    return hashlib.sha256(sequence.encode("ascii")).hexdigest()


def atom_records(residues):
    return [(i, atom.name, str(atom.altloc), atom.element.name,
             (atom.pos.x, atom.pos.y, atom.pos.z))
            for i, residue in enumerate(residues) for atom in residue]


def check_same_atoms(first, second):
    before, after = atom_records(first), atom_records(second)
    if len(before) != len(after) or any(a[:4] != b[:4] for a, b in zip(before, after)):
        raise ValueError("atom identity changed during matched-state serialization")
    if not np.allclose([row[4] for row in before], [row[4] for row in after], rtol=0, atol=1e-5):
        raise ValueError("matched-state coordinates changed beyond mmCIF roundtrip precision")


def paired_state(structure, metadata, deleted_count: int, task_id: str):
    if deleted_count not in (0, 2):
        raise ValueError("only intact and exact His7/Ala8 deletion are permitted")
    target_chain, target, _, vhh = select_candidate_chains(structure)
    if [chain.name for chain in structure[0]] != ["A", "B"]:
        raise ValueError("metadata requires CIF chain order A target then B VHH")
    if chain_sequence(target) != ACTIVE_GLP1 or [res.name for res in target[:3]] != ["HIS", "ALA", "GLU"]:
        raise ValueError("source target must be canonical active GLP-1 with His7/Ala8/Glu9 identity")
    vhh_sequence = chain_sequence(vhh)
    if not vhh_sequence or any(letter not in "ACDEFGHIKLMNPQRSTVWY" for letter in vhh_sequence):
        raise ValueError("fixed VHH sequence contains unsupported residues")
    target_entity_ids, vhh_entity_ids = {r.entity_id for r in target}, {r.entity_id for r in vhh}
    if len(target_entity_ids) != 1 or len(vhh_entity_ids) != 1 or target_entity_ids == vhh_entity_ids:
        raise ValueError("target and VHH require distinct complete polymer entities")
    for residues, entity_id in ((target, next(iter(target_entity_ids))), (vhh, next(iter(vhh_entity_ids)))):
        entity = structure.get_entity(entity_id)
        if entity is None or list(entity.full_sequence) != [residue.name for residue in residues]:
            raise ValueError("source observed residues must exactly match declared polymer sequence")
    n = len(target) + len(vhh)
    if any(values.shape != (n,) for values in metadata.values()):
        raise ValueError("source CIF/metadata token lengths disagree")
    if np.any(metadata["design_mask"][:30]) or not np.any(metadata["design_mask"][30:]):
        raise ValueError("require fixed target and nonempty VHH design/CDR annotation")
    if np.any(metadata["mol_type"] != 0):
        raise ValueError("only canonical protein token metadata is supported")
    coords = np.array([row[4] for row in atom_records(target + vhh)])
    if not len(coords) or not np.isfinite(coords).all():
        raise ValueError("source structure contains empty or nonfinite coordinates")
    # Explicit target covalent links require chemistry-aware reconstruction, not
    # blind index editing. VHH disulfide connections are preserved unchanged.
    if any(connection.partner1.chain_name == "A" or connection.partner2.chain_name == "A"
           for connection in structure.connections):
        raise ValueError("target has explicit covalent connections requiring separate chemistry review")
    keep = np.arange(deleted_count, n)
    remapped = {name: np.array(values[keep], copy=True) for name, values in metadata.items()}
    combined = structure.clone()
    combined.name = task_id
    changed_target = combined[0].find_chain("A")
    for _ in range(deleted_count):
        del changed_target[0]
    for index, residue in enumerate(changed_target, 1):
        residue.seqid = gemmi.SeqId(index, " ")
    entity_ids = {residue.entity_id for residue in target_chain}
    if len(entity_ids) != 1:
        raise ValueError("source target must have one complete polymer entity")
    entity = combined.get_entity(next(iter(entity_ids)))
    if entity is None:
        raise ValueError("source target polymer entity missing")
    entity.full_sequence = [residue.name for residue in changed_target]
    combined.assign_label_seq_id(force=True)
    text = combined.make_mmcif_document().as_string()
    reparsed = gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block())
    _, observed_target, _, observed_vhh = select_candidate_chains(reparsed)
    if chain_sequence(observed_target) != ACTIVE_GLP1[deleted_count:] or chain_sequence(observed_vhh) != vhh_sequence:
        raise ValueError("source sequence changed beyond the requested target deletion")
    check_same_atoms(target[deleted_count:], observed_target)
    check_same_atoms(vhh, observed_vhh)
    if len(reparsed.connections) != len(structure.connections):
        raise ValueError("VHH covalent connections changed during serialization")
    for name, values in metadata.items():
        if not np.array_equal(remapped[name][30 - deleted_count:], values[30:]):
            raise ValueError(f"VHH metadata changed: {name}")
    return text, remapped, {
        "vhh_sequence_sha256": sequence_digest(vhh_sequence),
        "target_sequence_sha256": sequence_digest(ACTIVE_GLP1[deleted_count:]),
        "target_token_count": 30 - deleted_count, "vhh_token_count": len(vhh),
        "designed_token_indices": np.flatnonzero(remapped["design_mask"]).tolist(),
        "binding_token_indices": np.flatnonzero(remapped["binding_type"]).tolist(),
        "output_to_source_token_indices": keep.tolist(),
        "target_biological_residue_numbers": list(range(7 + deleted_count, 37)),
        "deleted_source_token_indices": list(range(deleted_count)),
        "shared_coordinates_preserved": True,
    }


def build_inputs(candidate_roots, output: Path, runtime_root: Path, prefixes=None):
    roots = [Path(root).resolve(strict=True) for root in candidate_roots]
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory; no overwrite or reuse")
    output = output.resolve(strict=False)
    if any(root == output or root in output.parents for root in roots):
        raise ValueError("output must not be inside any candidate source directory")
    if len(set(roots)) != len(roots):
        raise ValueError("duplicate candidate roots")
    prefixes = list(prefixes) if prefixes is not None else ([""] if len(roots) == 1 else [])
    if len(prefixes) != len(roots) or len(set(prefixes)) != len(prefixes):
        raise ValueError("supply one unique --candidate-prefix per root for multiple roots")
    if any(prefix and not SAFE_PREFIX.fullmatch(prefix) for prefix in prefixes):
        raise ValueError("unsafe candidate prefix")
    runtime = parse_runtime_manifest(runtime_root.resolve(strict=True))
    prepared, candidates, source_files = [], [], []
    for root, prefix in zip(roots, prefixes):
        files = sorted(path for path in root.glob("design_*.cif") if DESIGN_ID.fullmatch(path.stem))
        if not files:
            raise ValueError(f"no design_<number>.cif inputs under {root}")
        for cif in files:
            npz = cif.with_suffix(".npz")
            hashes = {str(path): stable_digest(path) for path in (cif, npz)}
            candidate_id = f"{prefix}_{cif.stem}" if prefix else cif.stem
            structure, metadata = load_structure(cif), load_metadata(npz)
            states = []
            for label, state_id, deleted in STATES:
                task_id = f"{candidate_id}_{label}"
                content, arrays, mapping = paired_state(structure, metadata, deleted, task_id)
                prepared.append((task_id, content, arrays))
                states.append(dict(mapping, task_id=task_id, state_id=state_id,
                                   cif=f"design_inputs/{task_id}.cif", metadata=f"design_inputs/{task_id}.npz",
                                   folds=2, conformation_source_group=f"matched_deletion:{candidate_id}"))
            if states[0]["vhh_sequence_sha256"] != states[1]["vhh_sequence_sha256"]:
                raise ValueError("paired states changed fixed candidate sequence")
            candidates.append({"candidate_id": candidate_id, "source_design_id": cif.stem,
                               "source_root": str(root), "source_files": hashes,
                               "vhh_sequence_sha256": states[0]["vhh_sequence_sha256"], "states": states})
            source_files.extend(hashes.items())
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError(f"bounded builder requires 1–{MAX_CANDIDATES} unique candidates")
    candidates.sort(key=lambda row: row["candidate_id"])
    sequence_set = candidate_set_digest(candidates)
    config = control_config(output / "design_inputs", runtime_root.resolve(strict=True))
    output.mkdir(parents=True, exist_ok=False)
    (output / "design_inputs").mkdir()
    output_hashes = {}
    for task_id, content, arrays in prepared:
        cif, npz = output / "design_inputs" / f"{task_id}.cif", output / "design_inputs" / f"{task_id}.npz"
        atomic_write(cif, content.encode())
        write_npz(npz, arrays)
        output_hashes.update({str(path.relative_to(output)): stable_digest(path) for path in (cif, npz)})
    atomic_write(output / "folding.yaml", yaml.safe_dump(config, sort_keys=False).encode())
    output_hashes["folding.yaml"] = stable_digest(output / "folding.yaml")
    for path, digest in source_files:
        if stable_digest(Path(path)) != digest:
            raise ValueError("source changed during input preparation; do not execute this output")
    report = {
        "schema": "VHH_MATCHED_PAIR_INPUTS_V1", "status": "INPUTS_PREPARED_GPU_NOT_RUN",
        "candidate_count": len(candidates), "task_count": len(prepared), "expected_fold_samples": len(prepared) * 2,
        "candidate_set_sha256": sequence_set, "actual_candidate_set_sha256": sequence_set,
        "candidates": candidates, "output_sha256": output_hashes, "runtime_assets_from_manifest": runtime,
        "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED", "truncated_new_n_terminus": "Glu9_geometry_only",
        "paired_effect": "matched_deletion_of_His7_Ala8_from_same_active_geometry",
        "natural_truncated_conformer_test": "NOT_INCLUDED_SEPARATE_SOURCE_STRESS_TEST_REQUIRED",
        "binding_metadata_policy": "slice_original_flags_do_not_reassign_deleted_His_Ala_flags_to_Glu9",
        "free_evaluation": "target_only_template_no_cross_chain_relative_template_geometry",
        "candidate_sequences_unchanged": True, "lockbox_read": False, "gpu_started": False,
        "biological_pass": False, "claim_boundary": "COMPUTATIONAL_MATCHED_DELETION_DESCRIPTOR_ONLY_NOT_SELECTIVITY",
        "builder_sha256": stable_digest(Path(__file__)),
    }
    write_json(output / "MATCHED_PAIR_INPUTS.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", action="append", type=Path, required=True)
    parser.add_argument("--candidate-prefix", action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_inputs(args.candidate_root, args.output, args.runtime_root, args.candidate_prefix)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "PREPARATION_FAILED", "error": str(exc), "gpu_started": False}))
        return 2
    print(json.dumps({key: report[key] for key in ("status", "candidate_count", "task_count", "expected_fold_samples", "candidate_set_sha256", "gpu_started")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
