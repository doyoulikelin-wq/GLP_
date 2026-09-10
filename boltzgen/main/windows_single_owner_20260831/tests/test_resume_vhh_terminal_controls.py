"""CPU-only failure-mode tests for the single authorized A/B/C continuation."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/resume_vhh_terminal_controls.py"
SPEC = importlib.util.spec_from_file_location("resume_vhh_terminal_controls", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def failure_fixture(tmp_path):
    root = tmp_path / "a_attempt"
    logs = root / "operator_logs"
    logs.mkdir(parents=True)
    cells = [{"condition_id": c, "cell_id": "cell_" + c.lower(), "spec_path": str(tmp_path / (c + ".yaml"))}
             for c in "ABC"]
    row = {**cells[0], "exit_code": 1, "timed_out": False, "attempt_root": str(root)}
    original = {"rule": M.R.RULE, "cells": cells}
    failed = {"status": "EXECUTION_FAILED", "rule": M.R.RULE, "frozen_cells": cells,
              "plan_sha256": "abc", "cells": [row], "started_at_utc": "2026-09-10T00:00:00Z"}
    dump(logs / "EXPLORATORY_INFERENCE.json", {"status": M.FAILED, "exit_code": 1,
         "cell_id": row["cell_id"], "run_root": str(root), "expected_designs": 2,
         "cuda_oom_detected": False, "resolved_config_contract": {"status": "PASS"}})
    (logs / "STATUS.txt").write_text(M.FAILED)
    for stage in ("design", "inverse_folding", "folding", "analysis", "filtering", "validation"):
        (logs / (stage + ".exit_code.txt")).write_text("1" if stage == "validation" else "0")
    (logs / "validation.stderr.txt").write_text(M.FAILURE + "\n")
    (logs / "validation.stdout.txt").write_text("")
    (root / "output.dat").write_text("original unmodified data")
    (logs / "OUTPUT_SHA256SUMS").write_text(M.R.sha256_file(root / "output.dat") + "  ./output.dat\n")
    return original, failed, logs


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_only_exact_single_stream_failure_is_eligible(tmp_path, stream):
    original, failed, logs = failure_fixture(tmp_path)
    (logs / "validation.stderr.txt").write_text("")
    (logs / ("validation." + stream + ".txt")).write_text(M.FAILURE)
    result = M.original_failure(original, failed, "abc")
    assert result["error_stream"] == stream
    assert result["source_bindings"]["OUTPUT_SHA256SUMS"]["sha256"]


@pytest.mark.parametrize("mutation", ["stage", "other_error", "both_streams", "two_cells", "exit_zero", "changed_output"])
def test_other_failures_or_mutated_outputs_are_not_recoverable(tmp_path, mutation):
    original, failed, logs = failure_fixture(tmp_path)
    if mutation == "stage":
        (logs / "folding.exit_code.txt").write_text("1")
    elif mutation == "other_error":
        (logs / "validation.stderr.txt").write_text("missing coordinates")
    elif mutation == "both_streams":
        (logs / "validation.stdout.txt").write_text("another warning")
    elif mutation == "two_cells":
        failed["cells"].append(copy.deepcopy(failed["cells"][0]))
    elif mutation == "exit_zero":
        failed["cells"][0]["exit_code"] = 0
    else:
        (logs.parent / "output.dat").write_text("changed data")
    with pytest.raises(ValueError):
        M.original_failure(original, failed, "abc")


def resume_fixture(tmp_path, monkeypatch):
    original, failed, _ = failure_fixture(tmp_path)
    original_path = dump(tmp_path / "ORIGINAL.json", original)
    failed_path = dump(tmp_path / "FAILED.json", failed)
    plan = {"schema": M.PLAN_SCHEMA, "rule": M.R.RULE, "implementation_sha256": {"new.py": "hash"},
            "automatic_retry": False, "biological_pass": False,
            "original_plan": M.R.small_binding(original_path), "failed_index": M.R.small_binding(failed_path),
            "a_cpu_validation": {"path": str(tmp_path / "cpu.json"), "sha256": "proof"},
            "a_strict_check": {"status": "PASS"}, "created_at_utc": "2026-09-10T00:10:00Z",
            "original_started_at_utc": failed["started_at_utc"], "deadline_at_utc": "2026-09-10T00:30:00Z"}
    path = dump(tmp_path / "RESUME_PLAN.json", plan)
    monkeypatch.setattr(M.R, "utc_now", lambda: "2026-09-10T00:10:00Z")
    monkeypatch.setattr(M.R, "ensure_clean_repository", lambda root: None)
    monkeypatch.setattr(M, "validate_resume_plan", lambda plan, recheck_inputs: None)
    args = SimpleNamespace(resume_plan=path, repo_root=tmp_path, workspace=tmp_path, output=tmp_path / "new_run")
    return args, plan, original


def test_dirty_repository_blocks_before_output_creation(tmp_path, monkeypatch):
    args, _, _ = resume_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(M.R, "ensure_clean_repository", lambda root: (_ for _ in ()).throw(ValueError("dirty")))
    with pytest.raises(ValueError, match="dirty"):
        M.run(args)
    assert not args.output.exists()


def test_incompatible_inputs_block_before_output_creation(tmp_path, monkeypatch):
    args, _, _ = resume_fixture(tmp_path, monkeypatch)
    def reject(plan, recheck_inputs):
        assert recheck_inputs is True
        raise ValueError("incompatible")
    monkeypatch.setattr(M, "validate_resume_plan", reject)
    with pytest.raises(ValueError, match="incompatible"):
        M.run(args)
    assert not args.output.exists()


@pytest.mark.parametrize("reason", ["used", "expired"])
def test_no_reuse_or_renewed_budget(tmp_path, monkeypatch, reason):
    args, _, _ = resume_fixture(tmp_path, monkeypatch)
    if reason == "used":
        (tmp_path / "gpu_work/owner_mode/t8_exploratory_inference/cell_b").mkdir(parents=True)
    else:
        monkeypatch.setattr(M.R, "utc_now", lambda: "2026-09-10T00:30:00Z")
    with pytest.raises(ValueError):
        M.run(args)
    assert not args.output.exists()


@pytest.mark.parametrize("strict_failure", [False, True])
def test_only_bc_execute_and_strict_failure_stops_before_c(tmp_path, monkeypatch, strict_failure):
    args, _, _ = resume_fixture(tmp_path, monkeypatch)
    called = []
    def cell(command, logfile, repo, seconds):
        assert command[1].endswith("run_vhh_terminal_cell_v2.py")
        assert seconds == 1125  # Remaining original 20 minutes minus bounded cleanup reserve.
        called.append(command[3])
        logs = tmp_path / "gpu_work/owner_mode/t8_exploratory_inference" / command[3] / "attempt_new/operator_logs"
        dump(logs / "EXPLORATORY_INFERENCE.json", {"status": "EXPLORATORY_INFERENCE_COMPLETE", "exit_code": 0,
             "observed_designs": 2, "fold_samples_per_candidate": 5, "cuda_oom_detected": False,
             "output_validation": {"status": "PASS"}, "filter_pass_count": 0})
        (logs / "STATUS.txt").write_text("EXPLORATORY_INFERENCE_COMPLETE")
        return 0, False
    def strict(row):
        if strict_failure:
            raise ValueError("missing assigned heavy atom")
        return {"status": "PASS"}
    monkeypatch.setattr(M.R, "bounded_cell", cell)
    monkeypatch.setattr(M.R, "strict_cell_check", strict)
    assert M.run(args) == (1 if strict_failure else 0)
    index = json.loads((args.output / "INDEX.json").read_text())
    assert called == (["cell_b"] if strict_failure else ["cell_b", "cell_c"])
    assert index["cells"][0]["exit_code"] == 1
    assert index["cells"][0]["reused_original_gpu_outputs"] is True
    assert index["overall_elapsed_seconds"] == 600
    assert index["automatic_retry"] is False


def test_new_implementation_change_rejected_before_original_reads(monkeypatch):
    monkeypatch.setattr(M, "implementation_hashes", lambda: {"new.py": "changed"})
    with pytest.raises(ValueError, match="implementation"):
        M.validate_resume_plan({"schema": M.PLAN_SCHEMA, "rule": M.R.RULE,
            "implementation_sha256": {"new.py": "frozen"}, "automatic_retry": False, "biological_pass": False})


def test_cpu_validation_requires_strict_e_and_restores_environment(monkeypatch):
    import validate_vhh_terminal_cell as V
    monkeypatch.setenv("EXPECTED_DESIGNS", "previous")
    monkeypatch.setattr(V, "validate", lambda root: {"status": "PASS", "observed_unique_ids": 2, "fold_samples_per_candidate": 5})
    monkeypatch.setattr(M.R, "strict_cell_check", lambda row: {"status": "REJECT"})
    with pytest.raises(ValueError, match="strict"):
        M.cpu_validate_a({"attempt_root": "/unused"})
    assert M.os.environ["EXPECTED_DESIGNS"] == "previous"
