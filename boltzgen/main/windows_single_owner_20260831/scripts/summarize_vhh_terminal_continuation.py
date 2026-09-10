#!/usr/bin/env python3
"""Combine unchanged terminal-contact evaluation with truthful continuation receipts.

A retains its original failed administrative receipt and original GPU outputs;
B/C are new executions. This is not a clean first-pass run or a biological gate.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

import gemmi
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize_vhh_terminal_controls as S
import run_vhh_terminal_controls as R


def bound_json(binding, inputs):
    raw, receipt = S.P.read_bound(binding["path"], binding["sha256"])
    inputs.append(receipt)
    return json.loads(raw)


def framework_identity(fold, cdr_tokens, scaffold_bytes, annotation):
    """Check actual canonical identities against source label_seq_id, not a name."""
    include = annotation.get("include")
    if not isinstance(include, list) or len(include) != 1:
        raise ValueError("one complete source scaffold chain required")
    chain = include[0].get("chain", {})
    if set(chain) != {"id"}:
        raise ValueError("partial source scaffold needs an explicit identity mapping")
    block = gemmi.cif.read_string(scaffold_bytes.decode("utf-8")).sole_block()
    residues = {}
    for label, number, name in block.find("_atom_site.", ["label_asym_id", "label_seq_id", "label_comp_id"]):
        if label != chain["id"] or number in (".", "?"):
            continue
        number = int(number)
        if name not in S.E.RESIDUES or residues.get(number, name) != name:
            raise ValueError("ambiguous or noncanonical source scaffold residue")
        residues[number] = name
    if not residues or set(residues) != set(range(1, len(residues)+1)):
        raise ValueError("source scaffold label_seq_id must be complete and contiguous")
    encoded = np.asarray(fold["res_type"])
    if (encoded.shape != (1, 30+len(residues), 33) or not np.isin(encoded, [0, 1]).all()
            or not np.all(encoded.sum(axis=-1) == 1)):
        raise ValueError("actual scaffold length or residue encoding mismatch")
    ids = encoded[0].argmax(axis=-1)
    if np.any(ids < 2) or np.any(ids > 21):
        raise ValueError("actual residues must be canonical")
    expected_cdr, _ = S.P.cdr_annotation(annotation)
    if list(cdr_tokens) != expected_cdr:
        raise ValueError("source CDR mapping mismatch")
    framework = [i for i in range(30, len(ids)) if i not in set(cdr_tokens)]
    if not framework or any(S.E.RESIDUES[int(ids[i])-2] != residues[i-29] for i in framework):
        raise ValueError("actual framework residue identity differs from frozen source")
    return {"all_framework_identities_match_source": True,
        "framework_residue_count": len(framework), "source_vhh_residue_count": len(residues)}


def check_a_administration(cell, cpu, historical, status, scope, strict_check):
    if (cell.get("exit_code") != 1 or cell.get("timed_out") is not False
            or cell.get("reused_original_gpu_outputs") is not True):
        raise ValueError("A must retain its original failed exit and reused outputs")
    if (historical.get("status") != "EXPLORATORY_INFERENCE_FAILED"
            or status.strip() != "EXPLORATORY_INFERENCE_FAILED"
            or cpu.get("historical_terminal_status") != "EXPLORATORY_INFERENCE_FAILED"
            or not cpu.get("original_failure")):
        raise ValueError("original A failure must remain explicit")
    validation = cpu.get("validator_result", {})
    if (validation.get("status") != "PASS" or validation.get("observed_unique_ids") != 2
            or validation.get("fold_samples_per_candidate") != 5):
        raise ValueError("A CPU output revalidation did not establish two by five")
    S.validate_strict_output_check({"strict_output_check": cpu.get("strict_check")}, scope)
    if cpu["strict_check"] != strict_check:
        raise ValueError("A strict receipt changed between CPU validation and INDEX")


def check_complete_administration(cell, receipt, status, scope):
    if (cell.get("exit_code") != 0 or cell.get("timed_out") is not False
            or cell.get("reused_original_gpu_outputs", False)):
        raise ValueError("B/C must be newly completed executions")
    S.validate_strict_output_check(cell, scope)
    if (receipt.get("status") != "EXPLORATORY_INFERENCE_COMPLETE"
            or receipt.get("exit_code") != 0 or receipt.get("cuda_oom_detected") is not False
            or receipt.get("output_validation", {}).get("status") != "PASS"
            or receipt.get("observed_designs") != 2 or receipt.get("fold_samples_per_candidate") != 5
            or status.strip() != "EXPLORATORY_INFERENCE_COMPLETE"):
        raise ValueError("new terminal cell output validation failed")


def validate_index(index, plan, failed, resume):
    from resume_vhh_terminal_controls import validate_resume_plan
    if (index.get("schema") != "VHH_TERMINAL_CONTROL_CONTINUATION_INDEX_V1"
            or index.get("status") != "COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY"):
        raise ValueError("only a completed continuation may be summarized")
    validate_resume_plan(resume, recheck_inputs=False)
    if index["rule"] != R.RULE or plan["rule"] != R.RULE or index["resume_plan_sha256"] != index["resume_plan"]["sha256"]:
        raise ValueError("frozen scientific rule or resume binding changed")
    if failed.get("status") != "EXECUTION_FAILED" or len(failed.get("cells", [])) != 1:
        raise ValueError("historical run must retain its A-only execution failure")
    cells = index.get("cells", [])
    if [c.get("condition_id") for c in cells] != list(S.CONDITIONS):
        raise ValueError("exactly A/B/C in original order required")
    for frozen, cell in zip(plan["cells"], cells):
        if any(cell.get(key) != value for key, value in frozen.items()):
            raise ValueError("continuation changed a frozen scientific input")
    if cells[0]["attempt_root"] != failed["cells"][0]["attempt_root"]:
        raise ValueError("A may not be rerun or replaced")
    for name in ("original_plan", "failed_index", "a_cpu_validation"):
        if name in resume and index[name] != resume[name]:
            raise ValueError("continuation provenance differs from resume plan")
    start = datetime.fromisoformat(index["original_started_at_utc"].replace("Z", "+00:00"))
    finish = datetime.fromisoformat(index["finished_at_utc"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(index["deadline_at_utc"].replace("Z", "+00:00"))
    elapsed = (finish-start).total_seconds()
    if (elapsed < 0 or finish > deadline or (deadline-start).total_seconds() > R.RULE["hard_timeout_seconds"]
            or abs(elapsed-index["overall_elapsed_seconds"]) > 2):
        raise ValueError("original whole-workflow deadline must not be extended")


def summarize(index_path: Path, output_dir: Path):
    started = time.perf_counter()
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("summary output directory must be new")
    raw, binding = S.P.read_bound(index_path)
    index, inputs = json.loads(raw), [binding]
    plan = bound_json(index["original_plan"], inputs)
    failed = bound_json(index["failed_index"], inputs)
    resume = bound_json(index["resume_plan"], inputs)
    cpu = bound_json(index["a_cpu_validation"], inputs)
    validate_index(index, plan, failed, resume)
    # Verify old scientific implementation hashes as well as new administration.
    if not S.REQUIRED_IMPLEMENTATIONS.issubset(plan["implementation_sha256"]):
        raise ValueError("original implementation closure missing")
    implementations = dict(plan["implementation_sha256"])
    for name, digest in index["implementation_sha256"].items():
        if name in implementations and implementations[name] != digest:
            raise ValueError("continuation changed an original implementation")
        implementations[name] = digest
    if Path(__file__).name not in index["implementation_sha256"]:
        raise ValueError("continuation summary implementation must be bound")
    for name, digest in implementations.items():
        if Path(name).name != name:
            raise ValueError("implementation path must be a sibling filename")
        _, binding = S.P.read_bound(Path(__file__).with_name(name), digest)
        inputs.append(binding)
    candidates = []
    for cell in index["cells"]:
        if cell["num_designs"] != 2:
            raise ValueError("all two generation draws must be retained")
        source = Path(cell["spec_path"]).parent
        bound = {}
        for name in ("design.yaml", "target.cif", "scaffold.yaml", "scaffold.cif"):
            bound[name], binding = S.P.read_bound(source/name, cell["spec_hashes"][name])
            inputs.append(binding)
        annotation = yaml.safe_load(bound["scaffold.yaml"])
        cdr, cdr3 = S.P.cdr_annotation(annotation)
        reference = S.S.source_reference(source/"target.cif", yaml.safe_load(bound["design.yaml"]))
        root, binding = S.P.fold_design_root(cell["attempt_root"])
        inputs.append(binding)
        if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
            raise ValueError("output must be separate from candidate sources")
        logs = Path(cell["attempt_root"])/"operator_logs"
        receipt_raw, rb = S.P.read_bound(logs/"EXPLORATORY_INFERENCE.json", cell.get("receipt_sha256"))
        status_raw, sb = S.P.read_bound(logs/"STATUS.txt", cell.get("terminal_status_sha256"))
        inputs.extend((rb, sb))
        receipt, status = json.loads(receipt_raw), status_raw.decode()
        if cell["condition_id"] == "A":
            if cell.get("cpu_revalidation_sha256") != index["a_cpu_validation"]["sha256"]:
                raise ValueError("A CPU receipt binding mismatch")
            check_a_administration(cell, cpu, receipt, status, index["rule"]["evaluation_scope"], index["a_strict_check"])
        else:
            check_complete_administration(cell, receipt, status, index["rule"]["evaluation_scope"])
        paths = sorted(root.glob("design_*.npz"))
        if len(paths) != 2 or {p.name for p in paths} != {p.name for p in (root/"fold_out_npz").glob("design_*.npz")}:
            raise ValueError("unfiltered candidate/fold closure must be two by five")
        for path in paths:
            design, db = S.E._load_npz(path)
            fold, fb = S.E._load_npz(root/"fold_out_npz"/path.name)
            inputs.extend((db, fb))
            result = S.evaluate_candidate(design, fold, reference,
                expected_target_binding_types=cell["expected_target_binding_types"], cdr_tokens=cdr)
            identity = framework_identity(fold, cdr, bound["scaffold.cif"], annotation)
            candidates.append({"candidate_id": cell["cell_id"]+"/"+path.stem,
                "condition_id": cell["condition_id"], "cdr3_length": cdr3, **result, **identity})
    unique = len({row["vhh_sequence_sha256"] for row in candidates})
    public = {"schema": "VHH_TERMINAL_CONTINUATION_SUMMARY_V1",
        "status": "STRICT_ALL_ATOM_DESCRIPTIVE_ANALYSIS_COMPLETE_AFTER_ADMINISTRATIVE_CONTINUATION",
        "historical_original_attempt_status": "EXECUTION_FAILED",
        "administrative_recovery": "A_ORIGINAL_GPU_OUTPUTS_CPU_REVALIDATED_B_C_NEW_EXECUTIONS",
        "clean_first_pass_execution": False, "original_a_gpu_rerun": False,
        "scientific_evaluation_unchanged": True, "original_30_minute_deadline_extended": False,
        "strict_all_atom_validation": True, "partial_fallback_used": False,
        "all_framework_identities_match_source": all(row["all_framework_identities_match_source"] for row in candidates),
        "biological_pass": False, "new_downstream_gpu_authorized_by_summary": False,
        "condition_ids": list(S.CONDITIONS), "candidate_count": len(candidates), "fold_sample_count": 30,
        "unique_sequence_count": unique, "duplicate_sequence_count": len(candidates)-unique,
        "contact_threshold_angstrom": 4.5, "pose_v2_role": "DIAGNOSTIC_ONLY_NOT_A_CONTINUATION_GATE",
        "by_condition": {condition: S.condition_aggregate([r for r in candidates if r["condition_id"] == condition]) for condition in S.CONDITIONS},
        "continuation_wall_seconds": index["continuation_wall_seconds"], "overall_elapsed_seconds": index["overall_elapsed_seconds"],
        "limitations": ["The original A workflow failed on an absent legacy optional binding-site column after GPU stages completed; its failed receipt is preserved and CPU output validation was corrected.",
            "All six generation draws are retained, including repeated sequences; five free folds are nested repeats, not independent molecules.",
            "Two draws per condition do not establish statistical sufficiency or a biological success rate; no common-random-number pairing is assumed.",
            "His/Ala geometric contact does not establish binding, active-versus-truncated selectivity, or terminal chemistry; close-contact diagnostics are not atom-type-aware clashes.",
            "Original contact thresholds and pose applicability rules are unchanged; pose is diagnostic only, never a continuation gate."],
        "analysis_seconds": time.perf_counter()-started}
    private = {**public, "inputs": inputs, "implementation_sha256": implementations, "candidates": candidates}
    payloads = {"PUBLIC_TERMINAL_CONTINUATION_SUMMARY.json": public, "PRIVATE_TERMINAL_CONTINUATION_SUMMARY.json": private}
    encoded = {name: json.dumps(value, indent=2, sort_keys=True, allow_nan=False) for name, value in payloads.items()}
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in encoded.items():
        with (output/name).open("x", encoding="utf-8") as stream:
            stream.write(payload+"\n")
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.index, args.output)
    print(json.dumps({k: result[k] for k in ("status", "candidate_count", "fold_sample_count")}))


if __name__ == "__main__":
    main()
