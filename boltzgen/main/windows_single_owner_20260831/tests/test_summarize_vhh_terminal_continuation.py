"""Continuation provenance, unchanged metrics and source-framework integrity."""
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1]/"scripts/summarize_vhh_terminal_continuation.py"
SPEC = importlib.util.spec_from_file_location("summarize_vhh_terminal_continuation", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def strict():
    return {"status":"PASS", "candidate_count":2, "free_fold_sample_count":10,
        "scope":"STRICT_FULL_ASSIGNED_CANONICAL_HEAVY_ATOMS", "biological_pass":False}


def a_args():
    return [{"exit_code":1,"timed_out":False,"reused_original_gpu_outputs":True},
        {"validator_result":{"status":"PASS","observed_unique_ids":2,"fold_samples_per_candidate":5},
         "strict_check":strict(),"historical_terminal_status":"EXPLORATORY_INFERENCE_FAILED",
         "original_failure":"legacy optional column absent"},
        {"status":"EXPLORATORY_INFERENCE_FAILED"},"EXPLORATORY_INFERENCE_FAILED",strict()["scope"],strict()]


def test_a_failure_is_preserved_and_cpu_pass_is_not_gpu_rerun():
    M.check_a_administration(*a_args())


@pytest.mark.parametrize("problem",["rewrite_exit","new_gpu","historical_success","missing_failure","short_folds","partial"])
def test_a_requires_truthful_complete_cpu_recovery(problem):
    args=a_args()
    if problem=="rewrite_exit": args[0]["exit_code"]=0
    elif problem=="new_gpu": args[0]["reused_original_gpu_outputs"]=False
    elif problem=="historical_success": args[2]["status"]="EXPLORATORY_INFERENCE_COMPLETE"
    elif problem=="missing_failure": args[1].pop("original_failure")
    elif problem=="short_folds": args[1]["validator_result"]["fold_samples_per_candidate"]=4
    elif problem=="partial": args[1]["strict_check"]["scope"]="PARTIAL"
    with pytest.raises(ValueError): M.check_a_administration(*args)


def test_bc_cannot_reuse_a_failed_administration():
    cell={"exit_code":0,"timed_out":False,"strict_output_check":strict()}
    receipt={"status":"EXPLORATORY_INFERENCE_COMPLETE","exit_code":0,"cuda_oom_detected":False,
        "output_validation":{"status":"PASS"},"observed_designs":2,"fold_samples_per_candidate":5}
    M.check_complete_administration(cell,receipt,"EXPLORATORY_INFERENCE_COMPLETE",strict()["scope"])
    cell["exit_code"]=1
    with pytest.raises(ValueError): M.check_complete_administration(cell,receipt,"EXPLORATORY_INFERENCE_COMPLETE",strict()["scope"])


def identity_fixture():
    annotation={"include":[{"chain":{"id":"A"}}],"design":[{"chain":{"id":"A","res_index":"2..2,4..4,6..6"}}]}
    cif="data_test\nloop_\n_atom_site.label_asym_id\n_atom_site.label_seq_id\n_atom_site.label_comp_id\n"+"".join(f"A {i} ALA\n" for i in range(1,9))
    ids=np.full(38,2); onehot=np.eye(33,dtype=int)[ids][None]
    return {"res_type":onehot},[31,33,35],cif.encode(),annotation


def test_framework_sequence_is_verified_not_inferred_from_scaffold_label():
    args=identity_fixture()
    result=M.framework_identity(*args)
    assert result=={"all_framework_identities_match_source":True,"framework_residue_count":5,"source_vhh_residue_count":8}
    args[0]["res_type"][0,31]=np.eye(33,dtype=int)[3]
    M.framework_identity(*args)  # designed CDR identity may differ
    args[0]["res_type"][0,30]=np.eye(33,dtype=int)[3]
    with pytest.raises(ValueError,match="framework residue"): M.framework_identity(*args)


@pytest.mark.parametrize("problem",["source_gap","wrong_length","ambiguous","wrong_cdr","partial_source"])
def test_ambiguous_source_or_mapping_is_rejected(problem):
    fold,cdr,cif,annotation=identity_fixture()
    if problem=="source_gap": cif=cif.replace(b"A 3 ALA\n",b"")
    elif problem=="wrong_length": fold["res_type"]=fold["res_type"][:,:-1]
    elif problem=="ambiguous": cif+=b"A 3 ARG\n"
    elif problem=="wrong_cdr": cdr=[31,33,36]
    elif problem=="partial_source": annotation["include"][0]["chain"]["res_index"]="1..8"
    with pytest.raises(ValueError): M.framework_identity(fold,cdr,cif,annotation)


def test_original_rules_cells_and_deadline_cannot_change(monkeypatch):
    monkeypatch.setitem(sys.modules,"resume_vhh_terminal_controls",SimpleNamespace(validate_resume_plan=lambda *a,**k:None))
    cells=[{"condition_id":c,"attempt_root":"old" if c=="A" else c} for c in "ABC"]
    plan={"rule":M.R.RULE,"cells":copy.deepcopy(cells)}
    failed={"status":"EXECUTION_FAILED","cells":[copy.deepcopy(cells[0])]}
    index={"schema":"VHH_TERMINAL_CONTROL_CONTINUATION_INDEX_V1","status":"COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY",
        "rule":M.R.RULE,"cells":copy.deepcopy(cells),"resume_plan_sha256":"digest","resume_plan":{"sha256":"digest"},
        "original_started_at_utc":"2026-09-10T17:00:00Z","finished_at_utc":"2026-09-10T17:20:00Z",
        "deadline_at_utc":"2026-09-10T17:30:00Z","overall_elapsed_seconds":1200}
    M.validate_index(index,plan,failed,{})
    index["deadline_at_utc"]="2026-09-10T17:31:00Z"
    with pytest.raises(ValueError,match="deadline"): M.validate_index(index,plan,failed,{})
