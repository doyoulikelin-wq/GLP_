"""CPU-only fail-closed tests for frozen serial terminal-condition experiments."""
import copy
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_vhh_terminal_controls.py"
SPEC = importlib.util.spec_from_file_location("run_vhh_terminal_controls", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_cells(root):
    """Build three tiny actual YAML bundles; no protein model or GPU is needed."""
    cells = []
    scaffold = {"path": "scaffold.cif", "design": [{"chain": {"id": "A", "res_index": "26..33,51..57,96..110"}}]}
    for condition in ("A", "B", "C"):
        folder = root / condition
        folder.mkdir(parents=True)
        target = {"path": "target.cif", "include": [{"chain": {"id": "E", "res_index": "1..30"}}]}
        if condition != "A":
            target["binding_types"] = [{"chain": {"id": "E", "binding": "1..2", **({"not_binding": "3..30"} if condition == "C" else {})}}]
        (folder / "design.yaml").write_text(yaml.safe_dump({"entities": [{"file": target}, {"file": {"path": "scaffold.yaml"}}]}))
        (folder / "scaffold.yaml").write_text(yaml.safe_dump(scaffold))
        (folder / "scaffold.cif").write_text("dummy_identical_scaffold\n")
        (folder / "target.cif").write_text("dummy_identical_target\n")
        cells.append(MODULE.condition_record(folder / "design.yaml", condition, "control_" + condition.lower()))
    return cells


def bind_fixture_source(monkeypatch, cells):
    """Bind the synthetic source hashes in this unit test, never production input."""
    rule = copy.deepcopy(MODULE.RULE)
    rule["source_file_sha256"] = {name: cells[0]["spec_hashes"][name] for name in ("target.cif", "scaffold.cif", "scaffold.yaml")}
    monkeypatch.setattr(MODULE, "RULE", rule)


@pytest.mark.parametrize("result", [{"status": "REJECT", "ready": False}, {"status": "PASS", "ready": False},
                                    {"status": "REJECT", "ready": True}, {"status": "PASS", "ready": 1}])
def test_require_compatible_inputs_needs_pass_and_boolean_ready(monkeypatch, result):
    preflight = importlib.import_module("preflight_vhh_evaluator_compatibility")
    monkeypatch.setattr(preflight, "inspect_spec", lambda *args: result)
    with pytest.raises(ValueError, match="incompatibility"):
        MODULE.require_compatible_inputs([{"condition_id": "A", "spec_path": "unused.yaml"}], "unused_config")


def test_require_compatible_inputs_uses_actual_parser_each_condition(monkeypatch):
    preflight = importlib.import_module("preflight_vhh_evaluator_compatibility")
    called = []
    def check(spec, config):
        called.append((spec, config))
        return {"status": "PASS", "ready": True}
    monkeypatch.setattr(preflight, "inspect_spec", check)
    cells = [{"condition_id": name, "spec_path": name + ".yaml"} for name in ("A", "B", "C")]
    assert len(MODULE.require_compatible_inputs(cells, "config.yaml")) == len(called) == 3


def test_only_expected_binding_differences_are_allowed(tmp_path, monkeypatch):
    cells = make_cells(tmp_path)
    bind_fixture_source(monkeypatch, cells)
    MODULE.validate_controlled_specs(cells)


@pytest.mark.parametrize("member", ["target.cif", "scaffold.cif", "scaffold.yaml"])
def test_arms_cannot_change_target_framework_or_scaffold_annotation(tmp_path, member, monkeypatch):
    cells = make_cells(tmp_path)
    bind_fixture_source(monkeypatch, cells)
    path = Path(cells[2]["spec_path"]).parent / member
    path.write_text(path.read_text() + "\n# altered source\n")
    cells[2] = MODULE.condition_record(Path(cells[2]["spec_path"]), "C", cells[2]["cell_id"])
    with pytest.raises(ValueError, match="all arms must share"):
        MODULE.validate_controlled_specs(cells)


def test_nonbinding_design_yaml_difference_rejected(tmp_path):
    cells = make_cells(tmp_path)
    path = Path(cells[2]["spec_path"])
    design = yaml.safe_load(path.read_text())
    design["entities"][0]["file"]["include"][0]["chain"]["res_index"] = "1..29"
    path.write_text(yaml.safe_dump(design))
    cells[2] = MODULE.condition_record(path, "C", cells[2]["cell_id"])
    with pytest.raises(ValueError, match="non-binding specification"):
        MODULE.validate_controlled_specs(cells)


def test_source_mutation_after_binding_rejected(tmp_path):
    cells = make_cells(tmp_path)
    path = Path(cells[0]["spec_path"]).parent / "target.cif"
    path.write_text("different_source\n")
    with pytest.raises(ValueError, match="source, condition or CDR"):
        MODULE.validate_controlled_specs(cells)


def test_identical_but_wrong_scaffold_source_still_rejected(tmp_path):
    with pytest.raises(ValueError, match="not the frozen complete 7XL0"):
        MODULE.validate_controlled_specs(make_cells(tmp_path))


def test_generation_yaml_small_binding_detects_change(tmp_path):
    path = tmp_path / "generation.yaml"
    path.write_text("sampling_steps: 500\n")
    bound = MODULE.small_binding(path)
    MODULE.verify_small_binding(bound)
    path.write_text("sampling_steps: 501\n")
    with pytest.raises(ValueError, match="binding changed"):
        MODULE.verify_small_binding(bound)


def test_small_binding_rejects_relative_path():
    with pytest.raises(ValueError, match="absolute source file"):
        MODULE.small_binding(Path("relative_generation.yaml"))


def test_changed_code_after_freeze_rejected(monkeypatch):
    monkeypatch.setattr(MODULE, "sha256_file", lambda path: "actual")
    plan = {"schema": "VHH_TERMINAL_CONTROL_PLAN_V1", "rule": copy.deepcopy(MODULE.RULE),
            "created_at_utc": "2026-01-01T00:00:00Z", "implementation_sha256": {name: "actual" for name in MODULE.IMPLEMENTATIONS}}
    plan["implementation_sha256"][MODULE.IMPLEMENTATIONS[0]] = "old"
    with pytest.raises(ValueError, match="implementation changed after freeze"):
        MODULE.validate_plan(plan)


@pytest.mark.parametrize("failure", ["dirty", "incompatible"])
def test_prelaunch_failure_occurs_before_attempt_or_child(monkeypatch, tmp_path, failure):
    args = SimpleNamespace(workspace=tmp_path, repo_root=tmp_path, plan=tmp_path/"plan.json", output=tmp_path/"attempt")
    calls = []
    monkeypatch.setattr(MODULE, "bounded_cell", lambda *args: calls.append(args))
    def dirty(repo):
        if failure == "dirty":
            raise ValueError("dirty repository")
    def invalid(plan, recheck_inputs=False):
        assert recheck_inputs is True
        raise ValueError("input/evaluator incompatibility")
    monkeypatch.setattr(MODULE, "ensure_clean_repository", dirty)
    monkeypatch.setattr(MODULE, "json_object", lambda path: {})
    monkeypatch.setattr(MODULE, "validate_plan", invalid)
    with pytest.raises(ValueError):
        MODULE.run(args)
    assert not args.output.exists()
    assert calls == []


def test_budget_and_retry_boundary_is_frozen():
    assert MODULE.RULE["hard_timeout_seconds"] == 1800
    assert MODULE.RULE["candidates_per_condition"] == 2
    assert MODULE.RULE["free_folds_per_candidate"] == 5
    assert MODULE.RULE["automatic_retry"] is False
    assert MODULE.RULE["automatic_downstream_gpu"] is False
    assert MODULE.RULE["evaluation_scope"] == "STRICT_FULL_ASSIGNED_CANONICAL_HEAVY_ATOMS"


def test_strict_missing_atoms_stop_before_next_condition(monkeypatch, tmp_path):
    """An execution-complete first cell cannot authorize a second invalid-atom cell."""
    cells = make_cells(tmp_path / "sources")
    plan = {"cells": cells, "rule": MODULE.RULE}
    args = SimpleNamespace(workspace=tmp_path, repo_root=tmp_path, plan=tmp_path/"plan.json", output=tmp_path/"attempt")
    receipt = {"status": "EXPLORATORY_INFERENCE_COMPLETE", "exit_code": 0, "observed_designs": 2,
               "fold_samples_per_candidate": 5, "cuda_oom_detected": False,
               "output_validation": {"status": "PASS"}, "filter_pass_count": 0}
    monkeypatch.setattr(MODULE, "ensure_clean_repository", lambda repo: None)
    monkeypatch.setattr(MODULE, "validate_plan", lambda plan, recheck_inputs=False: None)
    monkeypatch.setattr(MODULE, "json_object", lambda path: plan if Path(path) == args.plan else receipt)
    monkeypatch.setattr(MODULE, "sha256_file", lambda path: "a"*64)
    written, child_calls = {}, []
    monkeypatch.setattr(MODULE, "write_new", lambda path, value: written.update({path.name: copy.deepcopy(value)}))
    def child(command, log, cwd, timeout):
        child_calls.append(command)
        folder = tmp_path / "gpu_work/owner_mode/t8_exploratory_inference" / cells[0]["cell_id"] / "attempt_test"
        logs = folder / "operator_logs"
        logs.mkdir(parents=True)
        (logs/"STATUS.txt").write_text("EXPLORATORY_INFERENCE_COMPLETE\n")
        return 0, False
    monkeypatch.setattr(MODULE, "bounded_cell", child)
    E = importlib.import_module("evaluate_vhh_epitope")
    T = importlib.import_module("summarize_vhh_terminal_controls")
    P = importlib.import_module("summarize_vhh_pilot")
    S = importlib.import_module("summarize_vhh_controlled_expansion")
    dummy_root = tmp_path / "fold_fixture"
    (dummy_root/"fold_out_npz").mkdir(parents=True)
    for name in ("design_0.npz", "design_1.npz"):
        (dummy_root/name).touch()
        (dummy_root/"fold_out_npz"/name).touch()
    monkeypatch.setattr(P, "fold_design_root", lambda *args: (dummy_root, None))
    monkeypatch.setattr(P, "cdr_annotation", lambda *args: ([30], 1))
    monkeypatch.setattr(S, "source_reference", lambda *args: None)
    mask = np.zeros(31, dtype=bool)
    mask[30] = True
    monkeypatch.setattr(E, "_load_npz", lambda path: ({"coords": np.zeros((5, 1, 3))} if path.parent.name == "fold_out_npz" else {"design_mask": mask}, {}))
    strict_calls = []
    def strict_fail(*args, **kwargs):
        assert kwargs["expected_target_binding_types"] == MODULE.EXPECTED_BINDING["A"]
        assert kwargs["cdr_tokens"] == [30]
        strict_calls.append("strict_full_candidate")
        raise E.ValidationError("assigned/resolved mismatch: missing atoms")
    monkeypatch.setattr(T, "evaluate_candidate", strict_fail)
    monkeypatch.setattr(MODULE, "parse_manifest", lambda path: [(name, "a"*64) for name in ("boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt", "boltz2_conf_final.ckpt", "mols.zip")])
    monkeypatch.setattr(MODULE, "verify_runtime", lambda *args: {})
    assert MODULE.run(args) == 1
    assert len(child_calls) == 1
    assert strict_calls == ["strict_full_candidate"], written["INDEX.json"].get("error")
    assert written["INDEX.json"]["status"] == "EXECUTION_FAILED"
    assert "missing atoms" in written["INDEX.json"]["error"]
    assert "CONDITION_A_COMPLETED.json" not in written
