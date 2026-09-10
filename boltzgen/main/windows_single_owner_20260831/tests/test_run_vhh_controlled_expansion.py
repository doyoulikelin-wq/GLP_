"""CPU-only safeguards for the independently frozen three-framework experiment."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_vhh_controlled_expansion.py"
SPEC = importlib.util.spec_from_file_location("controlled_runner_test", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def ready(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"actual":true}')
    return {"schema": "VHH_CONTROLLED_EXPANSION_CPU_V1", "status": "READY",
        "experiment_rule_sha256": M.digest(M.RULE), "checks": dict.fromkeys(M.CPU_CHECKS, True),
        "result_artifacts": [M.bound_json(evidence)]}


def test_real_small_result_is_required(tmp_path):
    receipt = ready(tmp_path)
    M.check_cpu_receipt(receipt)
    (tmp_path / "evidence.json").write_text('{"actual":false}')
    with pytest.raises(ValueError, match="hash mismatch"):
        M.check_cpu_receipt(receipt)


@pytest.mark.parametrize("key", M.CPU_CHECKS)
def test_each_missing_cpu_prerequisite_blocks(tmp_path, key):
    receipt = ready(tmp_path)
    receipt["checks"][key] = False
    with pytest.raises(ValueError, match="not passed"):
        M.check_cpu_receipt(receipt)


def test_rule_change_not_silently_accepted(tmp_path):
    receipt = ready(tmp_path)
    receipt["experiment_rule_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="different experiment"):
        M.check_cpu_receipt(receipt)


def test_exclusive_output_preserves_old_results(tmp_path):
    path = tmp_path / "receipt.json"
    M.write_new(path, {"first": True})
    with pytest.raises(FileExistsError):
        M.write_new(path, {"first": False})
    assert json.loads(path.read_text()) == {"first": True}


def test_dirty_tree_blocks_before_attempt_creation(monkeypatch, tmp_path):
    def dirty(repo):
        raise ValueError("dirty repository")
    monkeypatch.setattr(M, "ensure_clean_repository", dirty)
    output = tmp_path / "attempt"
    with pytest.raises(ValueError, match="dirty"):
        M.run(SimpleNamespace(repo_root=tmp_path, workspace=tmp_path, output=output))
    assert not output.exists()


def records(monkeypatch, mutate=None):
    def record(root, label, cell):
        ranges = [[26, 33], [51, 57], [96, 110]] if label != M.RULE["scaffolds"][1] else [[24, 31], [50, 56], [95, 105]]
        row = {"cell_id": cell, "scaffold_id": label, "cdr_ranges_one_based": ranges,
            "spec_hashes": {"target.cif": "same", "design.yaml": "same"}}
        if mutate:
            mutate(row)
        return row
    monkeypatch.setattr(M, "spec_record", record)


def test_exact_three_frameworks_and_matched_loops(monkeypatch, tmp_path):
    records(monkeypatch)
    rows = M.controlled_cells(tmp_path, "controlled_01")
    assert [row["scaffold_id"] for row in rows] == M.RULE["scaffolds"]
    assert len({row["cell_id"] for row in rows}) == 3


def test_mismatched_third_cdr_length_fails(monkeypatch, tmp_path):
    def mutate(row):
        if row["scaffold_id"] == M.RULE["scaffolds"][2]:
            row["cdr_ranges_one_based"][-1][-1] += 1
    records(monkeypatch, mutate)
    with pytest.raises(ValueError, match="8/7/15"):
        M.controlled_cells(tmp_path, "controlled_01")


def test_different_target_conditioning_fails(monkeypatch, tmp_path):
    records(monkeypatch, lambda row: row["spec_hashes"].update({"design.yaml": row["scaffold_id"]}))
    with pytest.raises(ValueError, match="same target conditioning"):
        M.controlled_cells(tmp_path, "controlled_01")


def test_new_experiment_does_not_unlock_old_pilot():
    assert M.RULE["old_pilot_status"] == "BLOCKED_UNCHANGED"
    assert M.RULE["pose_class_count_role"] == "DESCRIPTIVE_NOT_REQUIRED_SUCCESS_GATE"
    assert M.RULE["automatic_retry"] is False
    assert M.RULE["biological_pass"] is False


def test_direct_safety_dependencies_are_frozen():
    assert {"run_owner_t12_split_template.py", "vhh_stage_gate.py"}.issubset(M.IMPLEMENTATIONS)


def test_frozen_safety_dependency_hash_change_rejected(monkeypatch):
    import evaluate_vhh_pose_v2 as V
    plan = {"schema": "VHH_CONTROLLED_EXPANSION_PLAN_V1", "rule": copy.deepcopy(M.RULE),
        "pose_rule": V.POSE_V2_RULE, "created_at_utc": "2026-09-10T00:00:00Z",
        "implementation_sha256": dict.fromkeys(M.IMPLEMENTATIONS, "a"*64)}
    monkeypatch.setattr(M, "sha256_file", lambda path: "b"*64 if path.name == "vhh_stage_gate.py" else "a"*64)
    with pytest.raises(ValueError, match="vhh_stage_gate.py"):
        M.validate_plan(plan)
