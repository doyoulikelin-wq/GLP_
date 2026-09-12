#!/usr/bin/env python3
"""Prepare new same-candidate deletion pairs and audit terminal chemistry on CPU.

The old stage gate is not advanced. This independent batch preserves every one
of the six terminal-control draws, without generating or editing any sequence.
Chemical labels in historical state IDs are not proof of terminal amidation.
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
import build_vhh_matched_pair_inputs as B
import run_vhh_matched_pairs as M
import summarize_vhh_terminal_continuation as T

CLAIM = "SAME_CANDIDATE_MATCHED_GEOMETRIC_DELETION_ONLY_NOT_SELECTIVITY"


def bound(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": B.stable_digest(path)}


def terminal_atoms(path, target_sequence=B.ACTIVE_GLP1):
    """Inspect serialized polymer atoms, not the NH2 substring in an ARG name."""
    structure = gemmi.read_structure(str(path))
    matches = [list(chain) for chain in structure[0]
               if B.chain_sequence(list(chain)) == target_sequence]
    if len(matches) != 1:
        raise ValueError("one complete target sequence required for chemistry inspection")
    residues = matches[0]
    first, last = residues[0], residues[-1]
    names = [[atom.name for atom in residue] for residue in (first, last)]
    if any(len(row) != len(set(row)) for row in names):
        raise ValueError("ambiguous terminal atom identities")
    from boltzgen.data import const
    canonical_last = set(const.ref_atoms[last.name])
    extra_last = sorted(set(names[1]) - canonical_last)
    canonical_only = set(names[1]) == canonical_last
    return {"source": bound(path), "first_residue_name": first.name,
        "last_residue_name": last.name, "first_residue_atom_names": names[0],
        "last_residue_atom_names": names[1],
        "first_residue_hydrogen_count": sum(atom.element.name == "H" for atom in first),
        "last_residue_extra_noncanonical_atom_names": extra_last,
        "last_residue_exactly_canonical_ARG_atoms": last.name == "ARG" and canonical_only,
        "ARG_sidechain_NH2_is_not_terminal_amide": last.name == "ARG" and "NH2" in names[1],
        "C_terminal_amide_nitrogen_represented": False if last.name == "ARG" and canonical_only else None,
        "free_N_terminus_charge_and_protonation_verified": False,
        "explicit_connection_count": len(structure.connections),
        "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED"}


def verify_pair(candidate, prepared):
    """Recheck identity, shared atoms, CDR mapping and actual positive labels."""
    states = candidate["states"]
    if len(states) != 2 or [s["target_token_count"] for s in states] != [30, 28]:
        raise ValueError("exact active30/deletion28 pair required")
    structures = [B.load_structure(prepared / state["cif"]) for state in states]
    metadata = [B.load_metadata(prepared / state["metadata"]) for state in states]
    chains = [B.select_candidate_chains(structure) for structure in structures]
    active_target, truncated_target = chains[0][1], chains[1][1]
    if B.chain_sequence(active_target) != B.ACTIVE_GLP1 or B.chain_sequence(truncated_target) != B.ACTIVE_GLP1[2:]:
        raise ValueError("target deletion identity mismatch")
    if [r.name for r in active_target[:3]] != ["HIS", "ALA", "GLU"]:
        raise ValueError("deletion must remove precisely His7/Ala8, leaving Glu9")
    if B.chain_sequence(chains[0][3]) != B.chain_sequence(chains[1][3]):
        raise ValueError("candidate sequence differs between paired states")
    if B.sequence_digest(B.chain_sequence(chains[0][3])) != candidate["vhh_sequence_sha256"]:
        raise ValueError("candidate digest mismatch")
    B.check_same_atoms(active_target[2:], truncated_target)
    B.check_same_atoms(chains[0][3], chains[1][3])
    for key in metadata[0]:
        if not np.array_equal(metadata[0][key][2:], metadata[1][key]):
            raise ValueError("deletion metadata did not retain exact original indices")
    if np.any(metadata[1]["binding_type"][:28] == 1):
        raise ValueError("deleted His/Ala positive hotspot must not move to Glu9")
    rows = []
    for state, arrays in zip(states, metadata):
        count = state["target_token_count"]
        rows.append({"task_id": state["task_id"], "positive_binding_token_indices": np.flatnonzero(arrays["binding_type"] == 1).tolist(),
            "negative_binding_token_indices": np.flatnonzero(arrays["binding_type"] == 2).tolist(),
            "cdr_token_indices": np.flatnonzero(arrays["design_mask"]).tolist(),
            "target_biological_residue_numbers": list(range(37-count, 37)),
            "his_ala_epitope_applicability": "applicable" if count == 30 else "not_applicable",
            "N_terminal_chemistry": "His7_geometry_only" if count == 30 else "Glu9_geometry_only"})
    return {"candidate_id": candidate["candidate_id"], "candidate_sequence_sha256": candidate["vhh_sequence_sha256"],
        "vhh_atoms_and_coordinates_same_in_both_states": True, "shared_target_atoms_and_coordinates_same": True,
        "entire_metadata_exact_deletion_slice": True, "deleted_positive_hotspot_not_reassigned": True, "states": rows}


def canonicalize_label_sequence(content):
    """Repair only target label_seq numbering, preserving all actual atoms."""
    structure = gemmi.make_structure_from_block(gemmi.cif.read_string(content).sole_block())
    before = structure.clone()
    target, residues, _, vhh = B.select_candidate_chains(structure)
    for number, residue in enumerate(target, 1):
        residue.label_seq = number
    encoded = structure.make_mmcif_document().as_string()
    parsed = gemmi.make_structure_from_block(gemmi.cif.read_string(encoded).sole_block())
    _, after_target, _, after_vhh = B.select_candidate_chains(parsed)
    if [r.label_seq for r in after_target] != list(range(1, len(after_target)+1)):
        raise ValueError("canonical label_seq_id mapping failed")
    _, before_target, _, before_vhh = B.select_candidate_chains(before)
    B.check_same_atoms(before_target, after_target)
    B.check_same_atoms(before_vhh, after_vhh)
    return encoded


def materialize_fixed_numbering(legacy, report, prepared, runtime):
    """Keep legacy output intact; write corrected derived inputs in a new folder."""
    result = copy.deepcopy(report)
    prepared.mkdir()
    (prepared / "design_inputs").mkdir()
    for relative in report["output_sha256"]:
        if relative == "folding.yaml":
            content = yaml.safe_dump(M.control_config(prepared / "design_inputs", runtime), sort_keys=False).encode()
        elif relative.endswith(".cif"):
            content = canonicalize_label_sequence((legacy / relative).read_text()).encode()
        else:
            content = (legacy / relative).read_bytes()
        B.atomic_write(prepared / relative, content)
    result["output_sha256"] = {name: B.stable_digest(prepared / name) for name in report["output_sha256"]}
    for candidate in result["candidates"]:
        for state in candidate["states"]:
            labels = B.load_metadata(prepared / state["metadata"])["binding_type"]
            state["binding_token_indices"] = np.flatnonzero(labels == 1).tolist()
            state["not_binding_token_indices"] = np.flatnonzero(labels == 2).tolist()
    result["target_label_seq_id_normalization"] = "1..target_count; atoms/coordinates/metadata unchanged"
    result["legacy_builder_intermediate"] = bound(legacy / "MATCHED_PAIR_INPUTS.json")
    result["adapter_sha256"] = B.stable_digest(Path(__file__))
    B.write_json(prepared / "MATCHED_PAIR_INPUTS.json", result)
    return result


def prepare(index_path, output, runtime_root):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or any((p / ".git").exists() for p in (output, *output.parents)):
        raise ValueError("new private output outside Git required")
    index_binding = bound(index_path)
    index = json.loads(Path(index_path).read_text())
    # Reuse full existing source identity/admin/strict checks. No old receipts or
    # candidate files are rewritten; a new CPU audit is recorded under output.
    T.summarize(Path(index_path), output / "source_reassessment")
    reassessment = json.loads((output / "source_reassessment/PRIVATE_TERMINAL_CONTINUATION_SUMMARY.json").read_text())
    if reassessment["candidate_count"] != 6 or reassessment["fold_sample_count"] != 30:
        raise ValueError("source must be all six real candidates and thirty source folds")
    roots = [Path(cell["attempt_root"]) / "intermediate_designs_inverse_folded" for cell in index["cells"]]
    prefixes = [cell["condition_id"] for cell in index["cells"]]
    if prefixes != ["A", "B", "C"]:
        raise ValueError("must preserve all A/B/C draws")
    prepared = output / "paired_inputs"
    legacy = output / "legacy_builder_intermediate"
    report = B.build_inputs(roots, legacy, Path(runtime_root), prefixes)
    report = materialize_fixed_numbering(legacy, report, prepared, Path(runtime_root))
    if report["candidate_count"] != 6 or report["expected_fold_samples"] != 24:
        raise ValueError("pairing must retain six candidates, twelve tasks, twenty-four folds")
    original_digests = sorted(c["vhh_sequence_sha256"] for c in reassessment["candidates"])
    if sorted(c["vhh_sequence_sha256"] for c in report["candidates"]) != original_digests:
        raise ValueError("paired inputs do not identify original actual candidate sequence multiset")
    mapping = [verify_pair(candidate, prepared) for candidate in report["candidates"]]
    config = yaml.safe_load((prepared / "folding.yaml").read_text())
    feature_check = M.preflight(config, report)
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    data = instantiate(OmegaConf.create(config["data"]))
    states = {s["task_id"]: s for c in report["candidates"] for s in c["states"]}
    cpu_rows = []
    for i in range(len(data.predict_set)):
        sample = data.predict_set[i]
        state = states[str(sample["id"])]
        count = state["target_token_count"]
        original = B.load_metadata(prepared / state["metadata"])
        if not np.array_equal(sample["binding_type"].numpy(), original["binding_type"]):
            raise ValueError("actual freefold features changed binding flags")
        if not np.array_equal(sample["design_mask"].numpy(), original["design_mask"]):
            raise ValueError("actual freefold features changed CDR masks")
        cpu_rows.append({"task_id": str(sample["id"]), "target_tokens": count,
            "actual_positive_binding_tokens": np.flatnonzero(sample["binding_type"].numpy() == 1).tolist(),
            "actual_negative_binding_tokens": np.flatnonzero(sample["binding_type"].numpy() == 2).tolist(),
            "first_target_residue_id": int(sample["res_type"][0].argmax()),
            "last_target_residue_id": int(sample["res_type"][count-1].argmax())})
    chemistry = []
    for cell, root in zip(index["cells"], roots):
        source = terminal_atoms(Path(cell["spec_path"]).parent / "target.cif")
        for cif in sorted(root.glob("design_*.cif")):
            cid = cell["condition_id"] + "_" + cif.stem
            stages = {"source_target": source,
                "generated": terminal_atoms(root.parent / "intermediate_designs" / cif.name),
                "inverse_folded": terminal_atoms(cif), "refolded": terminal_atoms(root / "refold_cif" / cif.name)}
            with np.load(root / "fold_out_npz" / (cif.stem + ".npz"), allow_pickle=False) as fold:
                ids = fold["res_type"][0].argmax(axis=1)
                mapping_array = fold["atom_to_token"][0]
                if ids[0] != 10 or ids[29] != 3 or mapping_array[:, 29].sum() != 11:
                    raise ValueError("actual freefold terminal tokens/ARG atom slots changed")
                slots = {"first_token_type": "HIS", "last_token_type": "ARG",
                    "first_token_atom_slot_count": int(mapping_array[:, 0].sum()),
                    "last_token_atom_slot_count": int(mapping_array[:, 29].sum()),
                    "explicit_extra_terminal_amide_N_slot": False}
            chemistry.append({"candidate_id": cid, "stages": stages, "actual_fold_npz_terminal_slots": slots})
    pair_chemistry = [terminal_atoms(prepared / s["cif"], B.ACTIVE_GLP1 if s["target_token_count"] == 30 else B.ACTIVE_GLP1[2:])
                     for c in report["candidates"] for s in c["states"]]
    import boltzgen
    source_root = Path(boltzgen.__file__).parent
    evidence = {name: bound(source_root / name) for name in ("data/parse/mmcif.py", "data/const.py", "task/predict/data_from_generated.py")}
    public = {"schema": "VHH_SAME_CANDIDATE_PAIRING_PREPARATION_20260913_V1", "status": "CPU_PREPARATION_COMPLETE",
        "candidate_count": 6, "state_count_per_candidate": 2, "folds_per_state": 2, "planned_fold_count": 24,
        "all_candidate_sequences_and_shared_coordinates_preserved": True, "deleted_hotspot_not_reassigned": True,
        "deleted_target_label_seq_id_normalization_applied": True,
        "actual_freefold_CPU_tasks_verified": len(cpu_rows), "target_only_template_verified": True,
        "source_groups": {"A": 2, "B": 2, "C": 2}, "source_candidate_count_includes_duplicates": True,
        "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED",
        "C_terminal_amide_nitrogen_absent_in_canonical_representation": all(row["stages"]["inverse_folded"]["C_terminal_amide_nitrogen_represented"] is False for row in chemistry),
        "ARG_sidechain_NH2_is_not_C_terminal_amide": True,
        "free_N_terminal_protonation_and_charge_unverified": True,
        "model_support_assessment": "Current canonical protein path strips H and selects fixed reference atom lists; no verified terminal amidation/protonation protocol was established.",
        "chemical_fix_applied": False, "dummy_atoms_added": False,
        "old_stage_gate_status": "BLOCKED_UNCHANGED", "gpu_started": False, "biological_pass": False,
        "claim_boundary": CLAIM,
        "limits": ["Deleting His/Ala changes geometric sequence length, not a validated physiologic active/truncated chemistry model.",
            "Historical NH2 state IDs are nominal labels only; no chemically verified NH2 state is claimed.",
            "Candidate/state is the pairing unit; two stochastic folds are nested repeats, not independent molecules.",
            "No new natural truncated source or lockbox was read; shared-target geometry is deliberately matched."]}
    private = {**public, "source_index": index_binding, "paired_preparation": bound(prepared / "MATCHED_PAIR_INPUTS.json"),
        "source_reassessment": bound(output / "source_reassessment/PRIVATE_TERMINAL_CONTINUATION_SUMMARY.json"),
        "candidate_set_sha256": report["candidate_set_sha256"], "pair_mapping_checks": mapping,
        "cpu_freefold_checks": cpu_rows, "cpu_template_preflight": feature_check,
        "terminal_chemistry_by_candidate": chemistry, "paired_terminal_chemistry": pair_chemistry,
        "runtime_implementation": evidence}
    B.write_json(output / "PRIVATE_PAIRING_PREPARATION.json", private)
    B.write_json(output / "PUBLIC_PAIRING_PREPARATION.json", public)
    plan = {"schema": "VHH_INDEPENDENT_MATCHED_DELETION_PLAN_20260913_V1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "INPUTS_READY_FOR_SEPARATE_NEW_BATCH_EXECUTOR", "source_index": index_binding,
        "prepared": str(prepared), "preparation": bound(prepared / "MATCHED_PAIR_INPUTS.json"),
        "cpu_audit": bound(output / "PRIVATE_PAIRING_PREPARATION.json"), "candidate_set_sha256": report["candidate_set_sha256"],
        "candidate_count": 6, "task_count": 12, "fold_sample_count": 24, "hard_timeout_seconds": 1800,
        "contact_cutoff_angstrom": 4.5, "folding_config": bound(prepared / "folding.yaml"),
        "allowed_reused_functions": ["run_vhh_matched_pairs.verify_preparation", "run_vhh_matched_pairs.preflight", "run_vhh_matched_pairs.score_outputs"],
        "old_run_entrypoint_not_authorized_by_this_plan": True, "old_stage_gate_status": "BLOCKED_UNCHANGED",
        "required_runtime_controls": ["separate fresh output", "accepted Python/launcher", "single shared GPU lock", "used weights before/after SHA", "24 finite folds and exact candidate identity", "no retries or new sequence design"],
        "executor_status": "PARENT_MUST_BIND_NEW_INDEPENDENT_RUNNER_NO_OLD_GATE_BYPASS",
        "terminal_chemistry_status": "NOT_ATOMICALLY_VERIFIED", "claim_boundary": CLAIM,
        "implementation_sha256": {name: B.stable_digest(Path(__file__).with_name(name)) for name in
            (Path(__file__).name, "build_vhh_matched_pair_inputs.py", "run_vhh_matched_pairs.py", "evaluate_vhh_epitope.py", "prepare_vhh_native_control.py")},
        "gpu_started": False, "automatic_gpu": False, "biological_pass": False}
    B.write_json(output / "PAIRING_PLAN.json", plan)
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-index", "output", "runtime-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.source_index, args.output, args.runtime_root)
    print(json.dumps({k: result[k] for k in ("status", "candidate_count", "planned_fold_count", "terminal_chemistry_status", "gpu_started")}))


if __name__ == "__main__":
    main()
