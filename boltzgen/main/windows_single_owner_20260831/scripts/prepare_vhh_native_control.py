#!/usr/bin/env python3
"""Prepare a small, non-blind native VHH recovery control; never run a GPU.

6JB8 is an experimental D3-L11/hen-egg-white-lysozyme complex. Only modeled
canonical polymer residues are retained. Reference coordinates are scoring-only;
the folding input displaces the VHH by 100 A and exposes target-only templates.
This checks an existing complex, not prospective GLP-1 binding or selectivity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

import gemmi
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_owner_t12_split_template import build_folding_config, atomic_write, sha256_file

SOURCE_URL = "https://files.rcsb.org/download/6JB8.cif"
REFERENCE_URL = "https://www.rcsb.org/structure/6JB8"
CONTROL_ID = "native6jb8"
TRANSLATION = np.array([100.0, 0.0, 0.0])
RECOVERY_RMSD_ANGSTROM = 5.0
CALIBRATION_THRESHOLDS = {
    "target_aligned_binder_ca_rmsd_angstrom": {"direction": "max", "value": 5.0},
    "native_heavy_residue_contact_recall": {"direction": "min", "value": 0.3},
}


def heavy_residue_contacts(coords: np.ndarray, token: np.ndarray, heavy: np.ndarray,
                           target_count: int, cutoff: float = 4.5) -> set[tuple[int, int]]:
    """Return interchain residue pairs using eligible heavy atoms; no atom-count metric."""
    coords, token, heavy = np.asarray(coords), np.asarray(token), np.asarray(heavy, dtype=bool)
    if coords.shape != (len(token), 3) or heavy.shape != token.shape or not np.isfinite(coords[heavy]).all():
        raise ValueError("invalid contact inputs")
    left = np.flatnonzero(heavy & (token < target_count))
    right = np.flatnonzero(heavy & (token >= target_count))
    if not len(left) or not len(right):
        raise ValueError("empty contact chain")
    pairs = np.argwhere(np.sum((coords[left, None] - coords[right]) ** 2, axis=-1) <= cutoff ** 2)
    return {(int(token[left[i]]), int(token[right[j]])) for i, j in pairs}


def aligned_binder_rmsd(predicted: np.ndarray, reference: np.ndarray, target_count: int) -> float:
    """CA RMSD of binder after target-CA Kabsch alignment (not DockQ)."""
    predicted = np.asarray(predicted, dtype=float)
    reference = np.asarray(reference, dtype=float)
    if predicted.shape != reference.shape or predicted.ndim != 2 or predicted.shape[1] != 3:
        raise ValueError("matching N x 3 CA coordinates required")
    if not (3 <= target_count < len(predicted)) or not np.isfinite([predicted, reference]).all():
        raise ValueError("invalid target count or nonfinite coordinates")
    mobile, fixed = predicted[:target_count], reference[:target_count]
    cm, cf = mobile.mean(0), fixed.mean(0)
    if np.linalg.matrix_rank(mobile - cm) < 2 or np.linalg.matrix_rank(fixed - cf) < 2:
        raise ValueError("degenerate target alignment")
    u, _, vt = np.linalg.svd((mobile - cm).T @ (fixed - cf))
    correction = np.eye(3)
    correction[2, 2] = np.linalg.det(u @ vt)
    rotation = u @ correction @ vt
    delta = (predicted[target_count:] - cm) @ rotation + cf - reference[target_count:]
    return float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))


def control_config(design_dir: Path, runtime_root: Path) -> dict:
    config = build_folding_config(design_dir, runtime_root)
    data = config["data"]
    data["_target_"] = "boltzgen.task.predict.data_from_generated.FromGeneratedDataModule"
    for key in ("expected_target_tokens", "expected_cdr_tokens", "expected_framework_tokens"):
        del data[key]
    data["cfg"]["num_workers"] = 0
    config["diffusion_samples"] = 2
    return config


def extract_control(raw: Path) -> tuple[gemmi.Structure, np.ndarray, dict]:
    block = gemmi.cif.read_file(str(raw)).sole_block()
    desc = dict(zip(block.find_values("_entity.id"), block.find_values("_entity.pdbx_description")))
    structure = gemmi.make_structure_from_block(block)
    if len(structure) != 1:
        raise ValueError("6JB8 must have one experimental model")
    output = gemmi.Structure()
    output.name = "6JB8_modeled_polymer_control"
    model = gemmi.Model("1")
    mapping, ca_coordinates, residue_addresses = [], [], {}
    for source_id, output_id, role, expected_length in (
        ("B", "A", "target_lysozyme", 129),
        ("A", "B", "binder_D3_L11", 125),
    ):
        source = structure[0][source_id]
        residues = [r for r in source if r.entity_type == gemmi.EntityType.Polymer]
        if len(residues) != expected_length:
            raise ValueError(f"unexpected modeled residue count for {source_id}: {len(residues)}")
        if len({r.entity_id for r in residues}) != 1:
            raise ValueError("ambiguous polymer entity")
        entity_id = residues[0].entity_id
        description = str(desc.get(entity_id, ""))
        keyword = "lysozyme" if source_id == "B" else "D3-L11"
        if keyword.lower() not in description.lower():
            raise ValueError(f"unexpected entity identity for {source_id}: {description}")
        entity = structure.get_entity(entity_id)
        declared = list(entity.full_sequence)
        observed = [r.name for r in residues]
        starts = [i for i in range(len(declared) - len(observed) + 1) if declared[i:i+len(observed)] == observed]
        if len(starts) != 1:
            raise ValueError("modeled polymer must be one unambiguous contiguous declared-sequence slice")
        chain = gemmi.Chain(output_id)
        for index, residue in enumerate(residues, 1):
            if not gemmi.find_tabulated_residue(residue.name).is_amino_acid():
                raise ValueError("non-amino-acid residue in polymer")
            cleaned = gemmi.Residue()
            cleaned.name, cleaned.seqid = residue.name, gemmi.SeqId(index, " ")
            cleaned.entity_type = gemmi.EntityType.Polymer
            cleaned.subchain = output_id
            cleaned.entity_id = "1" if output_id == "A" else "2"
            residue_addresses[(source_id, str(residue.seqid))] = (output_id, cleaned.seqid)
            atoms = {}
            for atom in residue:
                if atom.element.is_hydrogen:
                    continue
                if atom.name not in atoms or atom.occ > atoms[atom.name].occ:
                    atoms[atom.name] = atom
            if not {"N", "CA", "C", "O"}.issubset(atoms):
                raise ValueError("modeled residue lacks complete backbone")
            for atom in atoms.values():
                clone = atom.clone()
                clone.altloc = "\x00"
                cleaned.add_atom(clone)
            ca_coordinates.append(list(atoms["CA"].pos))
            chain.add_residue(cleaned)
        model.add_chain(chain)
        output_entity = gemmi.Entity("1" if output_id == "A" else "2")
        output_entity.entity_type = gemmi.EntityType.Polymer
        output_entity.polymer_type = gemmi.PolymerType.PeptideL
        output_entity.full_sequence = observed
        output_entity.subchains = [output_id]
        output.entities.append(output_entity)
        mapping.append({"source_auth_chain": source_id, "input_chain": output_id, "role": role,
                        "source_entity": entity_id, "description": description,
                        "modeled_residue_count": len(residues), "deposited_residue_count": len(declared),
                        "omitted_prefix_residues": starts[0],
                        "omitted_suffix_residues": len(declared)-starts[0]-len(observed),
                        "source_auth_residue_ids": [str(r.seqid) for r in residues]})
    output.add_model(model)
    for connection in structure.connections:
        keys = [(p.chain_name, str(p.res_id.seqid)) for p in (connection.partner1, connection.partner2)]
        if all(key in residue_addresses for key in keys) and connection.type == gemmi.ConnectionType.Disulf:
            copied = gemmi.Connection()
            copied.name, copied.type = connection.name, connection.type
            for source_partner, target_partner, key in zip((connection.partner1, connection.partner2),
                                                           (copied.partner1, copied.partner2), keys):
                target_partner.chain_name, target_partner.res_id.seqid = residue_addresses[key]
                target_partner.res_id.name = source_partner.res_id.name
                target_partner.atom_name = source_partner.atom_name
            output.connections.append(copied)
    output.assign_label_seq_id(force=True)
    return output, np.array(ca_coordinates), {"chains": mapping, "target_tokens": 129, "binder_tokens": 125,
                                            "preserved_disulfide_connections": len(output.connections)}


def preflight(config: dict, design_dir: Path, reference_ca: np.ndarray) -> dict:
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    module = instantiate(OmegaConf.create(config).data)
    if len(module.predict_set) != 1:
        raise ValueError("expected one calibration complex")
    sample = module.predict_set[0]
    mask = sample["template_mask"].numpy()
    if mask.shape != (1, 254) or not np.all(mask[0, :129] == 1) or np.any(mask[0, 129:]):
        raise ValueError("target-only template visibility failed")
    atom_indices = sample["token_to_rep_atom"].numpy().argmax(axis=1)
    # For these canonical protein tokens the representative atom must be CA.
    names = sample["ref_atom_name_chars"].numpy().argmax(axis=-1)
    decoded = ["".join(chr(int(v)+32) for v in row).strip() for row in names]
    if any(decoded[index] != "CA" for index in atom_indices):
        raise ValueError("representative atom mapping is not all CA")
    atom_pad = sample["atom_pad_mask"].numpy().astype(bool)
    resolved = sample["atom_resolved_mask"].numpy().astype(bool)
    if not np.all(atom_pad[atom_indices] & resolved[atom_indices]):
        raise ValueError("missing CA atom mapping")
    input_ca = sample["coords"].numpy().reshape(-1, len(atom_pad), 3)[0, atom_indices]
    # Featurization may globally center/rotate coordinates; rigid-invariant score checks displacement.
    displaced_score = aligned_binder_rmsd(input_ca, reference_ca, 129)
    if not np.isclose(displaced_score, 100.0, atol=0.05):
        raise ValueError(f"input does not retain deliberate 100 A displacement: {displaced_score}")
    native = module.predict_set.get_feat(design_dir.parent / "scoring_only" / "reference.cif",
                                        sample["design_mask"].numpy())
    for key in ("atom_to_token", "atom_pad_mask", "ref_atom_name_chars", "ref_element"):
        if not np.array_equal(sample[key].numpy(), native[key].numpy()):
            raise ValueError(f"reference/input atom identity differs: {key}")
    template_keys = [key for key in sample if key.startswith("template_") or key == "visibility_ids"]
    for key in template_keys:
        if not np.allclose(sample[key].numpy(), native[key].numpy(), atol=1e-5):
            raise ValueError(f"native/displaced template differs: {key}")
    atom_to_token = sample["atom_to_token"].numpy()
    token = atom_to_token.argmax(axis=1)
    heavy = atom_pad & resolved & (sample["ref_element"].numpy().argmax(axis=-1) > 1)
    native_coords = native["coords"].numpy().reshape(-1, len(atom_pad), 3)[0]
    native_pairs = heavy_residue_contacts(native_coords, token, heavy, 129)
    if not native_pairs:
        raise ValueError("native experimental complex has no eligible interface contacts")
    np.savez_compressed(design_dir.parent / "scoring_only" / "atom_mapping.npz",
                        ca_atom_indices=atom_indices, reference_ca=reference_ca,
                        atom_to_token=atom_to_token, atom_pad_mask=atom_pad,
                        contact_heavy_mask=heavy, native_residue_contact_pairs=np.array(sorted(native_pairs)))
    return {"status": "PASS", "template_slots": 1, "visible_target_tokens": 129,
            "visible_binder_tokens": 0, "native_features_returned": False,
            "mapping_atom_count": len(atom_pad), "ca_count": len(atom_indices),
            "deliberate_input_displacement_rmsd_angstrom": displaced_score,
            "relative_complex_template_exposed": False,
            "template_features_native_displaced_equal": True,
            "native_heavy_residue_contact_pairs": len(native_pairs),
            "contact_reference_policy": "canonical heavy atoms present in experimental structure; same mapping for predictions"}


def prepare(output: Path, runtime_root: Path) -> dict:
    started = time.monotonic()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("refusing to reuse or overwrite control preparation directory")
    output.mkdir(parents=True, mode=0o700)
    for folder in ("source", "scoring_only", "intermediate_designs", "config"):
        (output / folder).mkdir(mode=0o700)
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "GLP-VHH-calibration/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        raw_bytes = response.read(5 * 1024 * 1024 + 1)
    if len(raw_bytes) > 5 * 1024 * 1024 or not raw_bytes.startswith(b"data_"):
        raise ValueError("invalid or oversized RCSB mmCIF download")
    raw = output / "source" / "6JB8.cif"
    raw.write_bytes(raw_bytes)
    structure, reference_ca, metadata = extract_control(raw)
    structure.make_mmcif_document().write_file(str(output / "scoring_only" / "reference.cif"))
    for residue in structure[0]["B"]:
        for atom in residue:
            atom.pos = gemmi.Position(*(np.array(list(atom.pos)) + TRANSLATION))
    designs = output / "intermediate_designs"
    structure.make_mmcif_document().write_file(str(designs / f"{CONTROL_ID}.cif"))
    n = len(reference_ca)
    np.savez_compressed(designs / f"{CONTROL_ID}.npz", design_mask=np.r_[np.zeros(129), np.ones(125)].astype(np.float32),
                        mol_type=np.zeros(n, dtype=np.int64), ss_type=np.zeros(n, dtype=np.int64),
                        token_resolved_mask=np.ones(n, dtype=np.float32), binding_type=np.zeros(n, dtype=np.int64))
    config = control_config(designs, runtime_root)
    atomic_write(output / "config" / "folding.yaml", yaml.safe_dump(config, sort_keys=False))
    atomic_write(output / "steps.yaml", yaml.safe_dump({"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}))
    displaced = reference_ca.copy()
    displaced[129:] += TRANSLATION
    self_score = aligned_binder_rmsd(reference_ca, reference_ca, 129)
    displaced_score = aligned_binder_rmsd(displaced, reference_ca, 129)
    if self_score > 1e-6 or not np.isclose(displaced_score, 100.0):
        raise ValueError("CPU score self/translated control failed")
    receipt = {"schema": "VHH_NATIVE_CONTROL_PREPARATION_V1", "status": "CPU_READY",
               "source_url": SOURCE_URL, "reference_url": REFERENCE_URL, "source_sha256": sha256_file(raw),
               "control_id": CONTROL_ID, "inference_status": "NOT_STARTED", "fold_samples": 2,
               "sequence_design_or_training": False, "binder_mask_semantics": "whole fixed-sequence VHH; not CDR annotation",
               "terminal_modeling": "modeled polymer slice only; omitted residues recorded; no affinity inference",
               "source_is_experimental_complex": True, "prospective_or_training_holdout": False,
               "calibration_claim_boundary": "existing protein-antigen pipeline check, not GLP1-selectivity validation",
               "diagnostic_rule": {"thresholds": CALIBRATION_THRESHOLDS,
                                   "minimum_recovered_samples_of_two": 2,
                                   "purpose": "preregistered coarse pose recovery diagnostic; not a binding acceptance threshold"},
               "cpu_metric_controls": {"self_rmsd": self_score, "displaced_rmsd": displaced_score,
                                       "displaced_is_geometric_sanity_control_not_biological_negative": True},
               **metadata, "preflight": preflight(config, designs, reference_ca)}
    receipt["elapsed_seconds"] = time.monotonic() - started
    atomic_write(output / "PREPARATION.json", json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    files = sorted(path for path in output.rglob("*") if path.is_file())
    atomic_write(output / "INPUT_SHA256SUMS", "".join(f"{sha256_file(path)}  ./{path.relative_to(output).as_posix()}\n" for path in files))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    receipt = prepare(args.output, args.runtime_root.resolve(strict=True))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
