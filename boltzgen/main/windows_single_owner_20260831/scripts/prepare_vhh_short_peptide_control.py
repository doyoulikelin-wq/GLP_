#!/usr/bin/env python3
"""Prepare/score a real 9NK9 short-peptide control on CPU; never launch a GPU.

The complete modeled canonical 10-aa peptide and contiguous 118-aa modeled VHH
slice are retained. Unmodeled VHH terminal residues are disclosed, not invented.
Whole-VHH masks mean a fixed sequence binder, not a claimed CDR annotation.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.request

import gemmi
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E
from prepare_vhh_native_control import CALIBRATION_THRESHOLDS, aligned_binder_rmsd, control_config, heavy_residue_contacts
from run_owner_t12_split_template import atomic_write, sha256_file, utc_now

CONTROL_ID = "native9nk9"
TARGET_COUNT, BINDER_COUNT = 10, 118
SOURCE_URL = "https://files.rcsb.org/download/9NK9.cif"
PROTOCOL_ID = "vhh-short-peptide-9nk9-20260913"
IMPLEMENTATIONS = ("prepare_vhh_short_peptide_control.py", "run_vhh_short_peptide_control.py",
                   "prepare_vhh_native_control.py", "run_owner_t12_split_template.py", "evaluate_vhh_epitope.py")
LIMITATIONS = [
    "Known experimental Nb127/10-aa peptide positive control; not GLP1 selectivity or affinity evidence.",
    "One positive complex and two stochastic folds do not establish sensitivity or specificity.",
    "No validated biological negative is included; a rigid displacement is only a geometric sanity control.",
    "Training-set overlap is unknown; this is not a certified holdout.",
    "Only the contiguous experimentally modeled VHH slice is used; omitted terminal residues are recorded.",
    "Canonical residue heavy-atom representation does not verify terminal protonation or amidation.",
    "Only peptide internal conformation is templated; native relative VHH-peptide pose is not templated.",
]


def implementation_hashes():
    return {name: sha256_file(Path(__file__).with_name(name)) for name in IMPLEMENTATIONS}


def extract_control(raw):
    """Reject noncanonical/missing atoms and retain author/label residue provenance."""
    from boltzgen.data.const import ref_atoms
    block = gemmi.cif.read_file(str(raw)).sole_block()
    if block.find_value("_entry.id").upper() != "9NK9" or "X-RAY" not in block.find_value("_exptl.method"):
        raise ValueError("expected the experimental 9NK9 entry")
    descriptions = dict(zip(block.find_values("_entity.id"), block.find_values("_entity.pdbx_description")))
    source = gemmi.make_structure_from_block(block)
    if len(source) != 1:
        raise ValueError("exactly one experimental model required")
    output, model, ca, rows = gemmi.Structure(), gemmi.Model("1"), [], []
    addresses = {}
    output.name = CONTROL_ID
    for auth, out, role, expected in (("B", "A", "short_peptide", TARGET_COUNT), ("A", "B", "VHH_Nb127", BINDER_COUNT)):
        residues = [r for r in source[0][auth] if r.entity_type == gemmi.EntityType.Polymer]
        if len(residues) != expected or len({r.entity_id for r in residues}) != 1:
            raise ValueError("unexpected source chain/model length")
        declared = list(source.get_entity(residues[0].entity_id).full_sequence)
        observed = [r.name for r in residues]
        starts = [i for i in range(len(declared)-len(observed)+1) if declared[i:i+len(observed)] == observed]
        if len(starts) != 1 or (auth == "B" and (starts[0] or len(declared) != TARGET_COUNT)):
            raise ValueError("modeled residues must be an unambiguous complete peptide/contiguous VHH slice")
        labels = [r.label_seq for r in residues]
        if labels != list(range(starts[0]+1, starts[0]+1+len(observed))):
            raise ValueError("source label sequence has an internal gap")
        chain, omitted_atoms = gemmi.Chain(out), []
        for index, residue in enumerate(residues, 1):
            if residue.name not in E.RESIDUES:
                raise ValueError("noncanonical residue cannot be silently normalized")
            choices = {}
            for atom in residue:
                if atom.element.is_hydrogen or atom.occ <= 0:
                    continue
                if atom.name not in choices or atom.occ > choices[atom.name].occ:
                    choices[atom.name] = atom
            if set(ref_atoms[residue.name]) - choices.keys():
                raise ValueError("missing canonical heavy atoms")
            clean = gemmi.Residue()
            clean.name, clean.seqid = residue.name, gemmi.SeqId(index, " ")
            clean.entity_type, clean.subchain, clean.entity_id = gemmi.EntityType.Polymer, out, str(len(rows)+1)
            addresses[(auth, str(residue.seqid))] = (out, clean.seqid)
            for name in ref_atoms[residue.name]:
                atom = choices[name].clone()
                if not np.isfinite(list(atom.pos)).all():
                    raise ValueError("nonfinite source coordinates")
                atom.altloc = "\x00"
                clean.add_atom(atom)
            omitted_atoms += [{"source_auth_residue": str(residue.seqid), "atom": name} for name in choices.keys()-set(ref_atoms[residue.name])]
            ca.append(list(choices["CA"].pos))
            chain.add_residue(clean)
        model.add_chain(chain)
        entity = gemmi.Entity(str(len(rows)+1))
        entity.entity_type, entity.polymer_type = gemmi.EntityType.Polymer, gemmi.PolymerType.PeptideL
        entity.full_sequence, entity.subchains = observed, [out]
        output.entities.append(entity)
        rows.append({"source_auth_chain": auth, "input_chain": out, "role": role,
                     "source_description": str(descriptions[residues[0].entity_id]),
                     "modeled_residues": len(observed), "deposited_residues": len(declared),
                     "omitted_prefix_residues": starts[0], "omitted_suffix_residues": len(declared)-starts[0]-len(observed),
                     "source_auth_residue_ids": [str(r.seqid) for r in residues], "source_label_seq_ids": labels,
                     "omitted_noncanonical_model_atoms": omitted_atoms})
    output.add_model(model)
    for connection in source.connections:
        keys = [(p.chain_name, str(p.res_id.seqid)) for p in (connection.partner1, connection.partner2)]
        if connection.type == gemmi.ConnectionType.Disulf and all(key in addresses for key in keys):
            copied = gemmi.Connection()
            copied.name, copied.type = connection.name, connection.type
            for old, new, key in zip((connection.partner1, connection.partner2), (copied.partner1, copied.partner2), keys):
                new.chain_name, new.res_id.seqid = addresses[key]
                new.res_id.name, new.atom_name = old.res_id.name, old.atom_name
            output.connections.append(copied)
    output.assign_label_seq_id(force=True)
    return output, np.asarray(ca), rows


def evaluator_arrays(sample):
    """Use the unchanged E atom contract with explicit generic peptide identities."""
    arrays = {name: sample[name].numpy()[None] for name in (
        "atom_to_token", "atom_resolved_mask", "token_index", "res_type", "mol_type")}
    arrays["coords"] = sample["coords"].numpy().reshape(1, -1, 3)
    design = {"design_mask": sample["design_mask"].numpy()}
    E.evaluate_arrays(design, arrays, target_tokens=list(range(TARGET_COUNT)), his_token=None, ala_token=None)
    return design, arrays


def preflight(config, output, reference_ca, write_mapping=True):
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    module = instantiate(OmegaConf.create(config).data)
    if len(module.predict_set) != 1:
        raise ValueError("one short-peptide control required")
    sample = module.predict_set[0]
    design, arrays = evaluator_arrays(sample)
    expected_mask = np.r_[np.zeros(TARGET_COUNT), np.ones(BINDER_COUNT)]
    if not np.array_equal(design["design_mask"], expected_mask):
        raise ValueError("fixed whole-VHH mask differs")
    mask = sample["template_mask"].numpy()
    if mask.shape != (1, TARGET_COUNT+BINDER_COUNT) or not np.array_equal(mask[0], 1-expected_mask):
        raise ValueError("target-only template failed")
    native = module.predict_set.get_feat(output/"scoring_only/reference.cif", expected_mask)
    _, native_arrays = evaluator_arrays(native)
    for key in ("atom_to_token", "atom_resolved_mask", "ref_atom_name_chars", "ref_element", "res_type"):
        if not np.array_equal(sample[key].numpy(), native[key].numpy()):
            raise ValueError("input/reference identity mapping differs")
    for key in [k for k in sample if k.startswith("template_") or k == "visibility_ids"]:
        if not np.allclose(sample[key].numpy(), native[key].numpy(), atol=1e-5):
            raise ValueError("relative pose leaked into template features")
    ca = sample["token_to_rep_atom"].numpy().argmax(axis=1)
    names = sample["ref_atom_name_chars"].numpy().argmax(axis=-1)
    if any("".join(chr(int(v)+32) for v in names[i]).strip() != "CA" for i in ca):
        raise ValueError("representative atom is not CA")
    atom_map = arrays["atom_to_token"][0]
    assigned = atom_map.sum(axis=1) == 1
    token = atom_map.argmax(axis=1)
    pairs = heavy_residue_contacts(native_arrays["coords"][0], token, assigned, TARGET_COUNT)
    shifted = heavy_residue_contacts(arrays["coords"][0], token, assigned, TARGET_COUNT)
    displacement = aligned_binder_rmsd(arrays["coords"][0, ca], reference_ca, TARGET_COUNT)
    if not pairs or shifted or not np.isclose(displacement, 100, atol=0.05):
        raise ValueError("native/100-A geometric controls failed")
    if write_mapping:
        np.savez_compressed(output/"scoring_only/atom_mapping.npz", ca_atom_indices=ca, reference_ca=reference_ca,
                            atom_to_token=atom_map, atom_resolved_mask=assigned,
                            native_residue_contact_pairs=np.asarray(sorted(pairs)), res_type=arrays["res_type"])
    return {"status": "PASS", "strict_full_canonical_heavy_atom_validation": True,
            "all_assigned_atoms_resolved": True, "assigned_heavy_atoms": int(assigned.sum()),
            "target_tokens": TARGET_COUNT, "binder_tokens": BINDER_COUNT,
            "visible_target_tokens": TARGET_COUNT, "visible_binder_tokens": 0,
            "template_features_native_displaced_equal": True, "native_contact_pairs": len(pairs),
            "cpu_self_rmsd_angstrom": aligned_binder_rmsd(reference_ca, reference_ca, TARGET_COUNT),
            "cpu_displaced_rmsd_angstrom": displacement, "cpu_displaced_contact_pairs": len(shifted)}


def prepare(output, runtime_root):
    started, output = time.monotonic(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("fresh private preparation directory required")
    output.mkdir(parents=True, mode=0o700)
    for folder in ("source", "scoring_only", "intermediate_designs", "config"):
        (output/folder).mkdir(mode=0o700)
    with urllib.request.urlopen(SOURCE_URL, timeout=45) as response:
        raw = response.read(5*1024**2+1)
    if len(raw) > 5*1024**2 or not raw.startswith(b"data_"):
        raise ValueError("invalid bounded source download")
    source = output/"source/9NK9.cif"
    source.write_bytes(raw)
    structure, ca, chains = extract_control(source)
    structure.make_mmcif_document().write_file(str(output/"scoring_only/reference.cif"))
    for residue in structure[0]["B"]:
        for atom in residue:
            atom.pos.x += 100
    designs = output/"intermediate_designs"
    structure.make_mmcif_document().write_file(str(designs/f"{CONTROL_ID}.cif"))
    n = TARGET_COUNT+BINDER_COUNT
    np.savez_compressed(designs/f"{CONTROL_ID}.npz", design_mask=np.r_[np.zeros(TARGET_COUNT), np.ones(BINDER_COUNT)].astype(np.float32),
                        mol_type=np.zeros(n, dtype=np.int64), ss_type=np.zeros(n, dtype=np.int64),
                        token_resolved_mask=np.ones(n, dtype=np.float32), binding_type=np.zeros(n, dtype=np.int64))
    config = control_config(designs, runtime_root)
    atomic_write(output/"config/folding.yaml", yaml.safe_dump(config, sort_keys=False))
    atomic_write(output/"steps.yaml", yaml.safe_dump({"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]}))
    checks = preflight(config, output, ca)
    public = {"schema": "VHH_SHORT_PEPTIDE_CONTROL_PREPARATION_V1", "protocol_id": PROTOCOL_ID, "status": "CPU_READY",
              "prepared_at_utc": utc_now(), "source_pdb": "9NK9", "source_url": SOURCE_URL,
              "source_sha256": sha256_file(source), "reference_url": "https://www.rcsb.org/structure/9NK9",
              "primary_paper_url": "https://pubmed.ncbi.nlm.nih.gov/40409557/",
              "primary_paper_doi": "10.1016/j.jbc.2025.110268", "resolution_angstrom": 2.10,
              "target_tokens": TARGET_COUNT, "binder_tokens": BINDER_COUNT, "fold_samples": 2,
              "hard_timeout_seconds": 900, "minimum_recovered_samples_of_two": 2,
              "calibration_thresholds": CALIBRATION_THRESHOLDS, "contact_cutoff_angstrom": 4.5,
              "implementation_sha256": implementation_hashes(), "preflight": checks,
              "chains": [{k: v for k, v in row.items() if k not in ("source_description", "source_auth_residue_ids", "source_label_seq_ids")} for row in chains],
              "negative_control_status": "NO_VALIDATED_BIOLOGICAL_NEGATIVE", "limitations": LIMITATIONS,
              "gpu_started": False, "automatic_retry": False, "biological_pass": False,
              "elapsed_seconds": time.monotonic()-started}
    atomic_write(output/"PUBLIC_PREPARATION.json", json.dumps(public, indent=2, sort_keys=True)+"\n")
    atomic_write(output/"PREPARATION.json", json.dumps({**public, "chains": chains}, indent=2, sort_keys=True)+"\n")
    files = sorted(p for p in output.rglob("*") if p.is_file())
    atomic_write(output/"INPUT_SHA256SUMS", "".join(f"{sha256_file(p)}  ./{p.relative_to(output).as_posix()}\n" for p in files))
    return public


def score_outputs(attempt):
    outputs = list((attempt/"intermediate_designs/fold_out_npz").glob("*.npz"))
    cif = list((attempt/"intermediate_designs/refold_cif").glob("*.cif"))
    if [p.name for p in outputs] != [CONTROL_ID+".npz"] or [p.name for p in cif] != [CONTROL_ID+".cif"] or not cif[0].stat().st_size:
        raise ValueError("short-peptide output closure failed")
    with np.load(outputs[0], allow_pickle=False) as data:
        fold = {k: data[k] for k in data.files}
    with np.load(attempt/"scoring_only/atom_mapping.npz", allow_pickle=False) as data:
        reference = {k: data[k] for k in data.files}
    design = {"design_mask": np.r_[np.zeros(TARGET_COUNT), np.ones(BINDER_COUNT)]}
    if fold["coords"].shape != (2, len(reference["atom_to_token"]), 3):
        raise ValueError("exactly two complete fold samples required")
    E.evaluate_arrays(design, fold, target_tokens=list(range(TARGET_COUNT)), his_token=None, ala_token=None)
    for key in ("iptm", "ptm", "design_to_target_iptm", "design_ptm"):
        if np.asarray(fold[key]).shape != (2,) or not np.isfinite(fold[key]).all():
            raise ValueError("incomplete or nonfinite confidence outputs")
    if not np.array_equal(fold["atom_to_token"][0], reference["atom_to_token"]) or not np.array_equal(fold["res_type"], reference["res_type"]):
        raise ValueError("predicted atom/residue identity differs from reference")
    native = {tuple(map(int, row)) for row in reference["native_residue_contact_pairs"]}
    rows = []
    for index, coords in enumerate(fold["coords"]):
        pairs = heavy_residue_contacts(coords, reference["atom_to_token"].argmax(axis=1), reference["atom_resolved_mask"], TARGET_COUNT)
        rows.append({"sample_index": index,
            "target_aligned_binder_ca_rmsd_angstrom": aligned_binder_rmsd(coords[reference["ca_atom_indices"]], reference["reference_ca"], TARGET_COUNT),
            "native_heavy_residue_contact_recall": len(native & pairs)/len(native),
            "native_contact_pairs": len(native), "recovered_native_contact_pairs": len(native & pairs), "predicted_contact_pairs": len(pairs)})
    recovered = all(all(r[k] <= t["value"] if t["direction"] == "max" else r[k] >= t["value"] for k, t in CALIBRATION_THRESHOLDS.items()) for r in rows)
    return {"protocol_id": PROTOCOL_ID, "thresholds": CALIBRATION_THRESHOLDS, "samples": rows,
            "native_interface_recovered": recovered, "strict_full_canonical_heavy_atom_validation": True,
            "target_tokens": TARGET_COUNT, "binder_tokens": BINDER_COUNT, "biological_pass": False, "limitations": LIMITATIONS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.runtime_root.resolve(strict=True)), indent=2))


if __name__ == "__main__":
    main()
