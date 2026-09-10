"""Synthetic end-to-end controlled summary and source-chain alias regression."""
import importlib.util
import json
from pathlib import Path

import gemmi
import numpy as np
import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/summarize_vhh_controlled_expansion.py"
SPEC = importlib.util.spec_from_file_location("controlled_summary_test", SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def target_file(path):
    angle = np.arange(30)*1.7
    points = np.stack([2.3*np.cos(angle), 2.3*np.sin(angle), np.arange(30)*1.5], axis=1)
    structure, model, chain = gemmi.Structure(), gemmi.Model("1"), gemmi.Chain("P")
    for index, (name, point) in enumerate(zip(M.V.ACTIVE_SEQUENCE_NAMES, points)):
        residue = gemmi.Residue()
        residue.name, residue.seqid, residue.subchain = name, gemmi.SeqId(index+1, " "), "E"
        residue.label_seq = index+1
        for name in ("N", "CA", "C", "O"):
            atom = gemmi.Atom()
            atom.name, atom.element, atom.pos = name, gemmi.Element(name[0]), gemmi.Position(*point)
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    structure.make_mmcif_document().write_file(str(path))
    return points


def test_source_label_author_chain_alias(tmp_path):
    path = tmp_path / "target.cif"
    points = target_file(path)
    spec = {"entities": [{"file": {"include": [{"chain": {"id": "E", "res_index": "1..30"}}]}}]}
    result = M.source_reference(path, spec)
    assert np.allclose(result["centroids"], points, atol=.001)
    spec["entities"][0]["file"]["include"][0]["chain"]["id"] = "missing"
    with pytest.raises(ValueError, match="chain mapping"):
        M.source_reference(path, spec)


def fixture(tmp_path):
    target = tmp_path / "target.cif"
    points = target_file(target)
    cells = []
    for scaffold_index, label in enumerate(M.RULE["scaffolds"]):
        source, attempt = tmp_path / label, tmp_path / f"attempt_{scaffold_index}"
        source.mkdir()
        (source / "target.cif").write_bytes(target.read_bytes())
        (source / "scaffold.cif").write_text("synthetic source only")
        ranges = "26..33,51..57,96..110" if scaffold_index != 1 else "24..31,50..56,95..105"
        scaffold = {"design": [{"chain": {"res_index": ranges}}]}
        (source / "scaffold.yaml").write_text(yaml.safe_dump(scaffold))
        spec = {"entities": [{"file": {"include": [{"chain": {"id": "E", "res_index": "1..30"}}]}}]}
        (source / "design.yaml").write_text(yaml.safe_dump(spec))
        root = attempt / "intermediate_designs_inverse_folded"
        (root / "fold_out_npz").mkdir(parents=True)
        (attempt / "config").mkdir()
        (attempt / "config/folding.yaml").write_text(json.dumps({"data": {"design_dir": str(root)}}))
        logs = attempt / "operator_logs"
        logs.mkdir()
        (logs / "EXPLORATORY_INFERENCE.json").write_text(json.dumps({"observed_designs": 2, "fold_samples_per_candidate": 5,
            "status": "EXPLORATORY_INFERENCE_COMPLETE", "exit_code": 0, "cuda_oom_detected": False, "output_validation": {"status": "PASS"}}))
        (logs / "STATUS.txt").write_text("EXPLORATORY_INFERENCE_COMPLETE\n")
        cdr_tokens, cdr3 = M.P.cdr_annotation(scaffold)
        for number in range(2):
            ids = np.array([M.E.RESIDUES.index(name)+2 for name in M.V.ACTIVE_SEQUENCE_NAMES]+[2]*121)
            ids[-1] = 3+scaffold_index*2+number
            counts = M.E.HEAVY_COUNTS[ids-2]
            token = np.repeat(np.arange(len(ids)), counts)
            mapping = np.zeros((1, len(token), len(ids)), dtype=bool)
            mapping[0, np.arange(len(token)), token] = True
            backbone = np.concatenate([np.r_[np.ones(4), np.zeros(count-4)] for count in counts])[None]
            centres = np.r_[points, np.stack([np.arange(121)*.5+10, np.arange(121)%7, np.arange(121)%5], axis=1)]
            coords = centres[token]
            res_type = np.zeros((1, len(ids), 33))
            res_type[0, np.arange(len(ids)), ids] = 1
            mask = np.zeros(len(ids))
            mask[cdr_tokens] = 1
            np.savez(root / f"design_{number}.npz", design_mask=mask)
            np.savez(root / "fold_out_npz" / f"design_{number}.npz", coords=np.repeat(coords[None], 5, axis=0), input_coords=coords[None, None],
                atom_to_token=mapping, atom_resolved_mask=mapping.any(axis=2), backbone_mask=backbone,
                token_index=np.arange(len(ids))[None], res_type=res_type, mol_type=np.zeros((1, len(ids)), dtype=int))
        cells.append({"scaffold_id": label, "cell_id": f"private_cell_{scaffold_index}", "spec_path": str(source / "design.yaml"),
            "spec_hashes": {p.name: M.P.read_bound(p)[1]["sha256"] for p in source.iterdir()},
            "attempt_root": str(attempt), "exit_code": 0, "timed_out": False,
            "receipt_sha256": M.P.read_bound(logs / "EXPLORATORY_INFERENCE.json")[1]["sha256"],
            "terminal_status_sha256": M.P.read_bound(logs / "STATUS.txt")[1]["sha256"], "legacy_filter_pass_count": 0})
    path = tmp_path / "INDEX.json"
    path.write_text(json.dumps({"status": "GPU_COMPLETE_PENDING_DESCRIPTIVE_ANALYSIS", "cells": cells, "wall_seconds": 1.0, "implementation_sha256": {}}))
    return path


def test_complete_summary_no_candidate_data_leak(monkeypatch, tmp_path):
    index = fixture(tmp_path)
    monkeypatch.setattr(M, "validate_plan", lambda plan: None)
    output = tmp_path / "summary"
    result = M.summarize(index, output)
    assert result["overall"]["candidate_count"] == result["unique_sequence_count"] == 6
    assert result["overall"]["fold_sample_count"] == 30
    assert set(result["by_scaffold"]) == set(M.RULE["scaffolds"])
    assert result["old_pilot_status"] == "BLOCKED_UNCHANGED"
    public = (output / "PUBLIC_SUMMARY.json").read_text()
    for secret in (str(tmp_path), "private_cell_", "vhh_sequence_sha256", "actual_candidate_set_sha256"):
        assert secret not in public
    assert result["biological_pass"] is False
    with pytest.raises(ValueError, match="must be new"):
        M.summarize(index, output)


def test_missing_repeat_rejected(monkeypatch, tmp_path):
    index = fixture(tmp_path)
    monkeypatch.setattr(M, "validate_plan", lambda plan: None)
    path = tmp_path / "attempt_0/intermediate_designs_inverse_folded/fold_out_npz/design_0.npz"
    with np.load(path) as data:
        arrays = {key: data[key].copy() for key in data.files}
    arrays["coords"] = arrays["coords"][:4]
    np.savez(path, **arrays)
    with pytest.raises(ValueError, match="repeats"):
        M.summarize(index, tmp_path / "summary")
    assert not (tmp_path / "summary").exists()


def test_failure_receipt_cannot_claim_weights_verified(monkeypatch, tmp_path):
    index = fixture(tmp_path)
    monkeypatch.setattr(M, "validate_plan", lambda plan: None)
    data = json.loads(index.read_text())
    path = tmp_path / "attempt_0/operator_logs/EXPLORATORY_INFERENCE.json"
    receipt = json.loads(path.read_text())
    receipt["status"] = "EXPLORATORY_INFERENCE_FAILED"
    path.write_text(json.dumps(receipt))
    data["cells"][0]["receipt_sha256"] = M.P.read_bound(path)[1]["sha256"]
    index.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="terminal validation"):
        M.summarize(index, tmp_path / "summary")
    assert not (tmp_path / "summary").exists()
