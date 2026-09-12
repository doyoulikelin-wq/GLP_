#!/usr/bin/env python3
"""Independently recalculate final local-edit contacts without project scorers.

Reads the private fixed-input manifest and predicted arrays; prints only counts,
distances and sequence-scope checks. No inference or filesystem writes.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from boltzgen.data import const


def verify(attempt):
    """Verify all 18 samples and reconcile the published whole-CDR counts."""
    attempt = Path(attempt)
    fixed = json.loads((attempt / "FINAL_INPUTS.json").read_text())
    public = json.loads((attempt / "PUBLIC_LOCAL_EDIT_RESULT.json").read_text())
    assert len(fixed["tasks"]) == 6
    for name, digest in fixed["sha256"].items():
        assert hashlib.sha256((attempt / name).read_bytes()).hexdigest() == digest
    results = []
    for task in fixed["tasks"]:
        task_id = task["task_id"]
        with np.load(attempt / "intermediate_designs/fold_out_npz" / (task_id + ".npz"), allow_pickle=False) as z:
            xyz = np.asarray(z["coords"], dtype=float)
            mapping = z["atom_to_token"][0]
            ids = z["res_type"][0].argmax(axis=-1)
            resolved = z["atom_resolved_mask"][0].astype(bool)
        assert xyz.shape[0] == 3 and xyz.shape[2] == 3 and np.isfinite(xyz).all()
        assert np.isin(mapping, [0, 1]).all() and np.all(mapping.sum(axis=1) <= 1)
        valid = mapping.sum(axis=1) == 1
        token = mapping.argmax(axis=1)
        assert np.array_equal(valid, resolved)
        assert ids.tolist() == task["expected_residue_ids"]
        source = np.asarray(task["source_residue_ids"])
        outside = ~np.isin(np.arange(151), task["edit_window_tokens"])
        assert np.array_equal(ids[outside], source[outside])
        with np.load(attempt / "atom_mappings" / (task_id + ".npz"), allow_pickle=False) as z:
            assert np.array_equal(mapping, z["atom_to_token"])
            chars = z["ref_atom_name_chars"].argmax(axis=-1)
            names = np.array(["".join(chr(int(c) + 32) for c in row).strip() for row in chars])
        for i in range(151):
            expected_names = const.ref_atoms[const.tokens[int(ids[i])]]
            observed = names[valid & (token == i)].tolist()
            assert len(observed) == len(set(observed)) and set(observed) == set(expected_names)
        # Padding is excluded, including its argmax(zeros)==0 token assignment.
        cdr = valid & np.isin(token, task["full_cdr_tokens"])
        target = valid & (token < 30)
        assert not np.any(np.all(xyz[:, valid] == 0, axis=-1))
        samples = []
        for sample in xyz:
            cdr_xyz = sample[cdr]
            distances = []
            for terminal in (0, 1):
                distances.append(float(np.sqrt(np.sum((cdr_xyz[:, None] - sample[valid & (token == terminal)][None]) ** 2, axis=-1)).min()))
            any_distance = float(np.sqrt(np.sum((cdr_xyz[:, None] - sample[target][None]) ** 2, axis=-1)).min())
            samples.append({"his_angstrom": distances[0], "ala_angstrom": distances[1],
                "both": max(distances) <= 4.5, "any": any_distance <= 4.5})
        results.append({"task_id": task_id, "arm": task["arm"], "samples": samples,
            "substitutions": int(np.count_nonzero(ids != source))})
    by_arm = {}
    for arm in ("ORIGINAL", "SEQUENCE_ONLY", "LOCAL_INPAINT"):
        candidates = [r for r in results if r["arm"] == arm]
        rows = [r for c in candidates for r in c["samples"]]
        assert len(candidates) == 2 and len(rows) == 6
        counts = {"both": sum(r["both"] for r in rows), "any": sum(r["any"] for r in rows),
            "all_three_both": sum(all(r["both"] for r in c["samples"]) for c in candidates)}
        reported = public["by_arm"][arm]["primary_full_cdr"]
        assert counts["both"] == reported["sample_contact_counts"]["both_epitope_contacts"]
        assert counts["any"] == reported["sample_contact_counts"]["any_target_cdr_contact"]
        assert counts["all_three_both"] == reported["candidate_all_three_contact_counts"]["both_epitope_contacts"]
        by_arm[arm] = counts
    return {"status": "INDEPENDENT_RECALCULATION_PASS", "samples_checked": 18,
        "all_atom_names_and_outside_window_identities_checked": True,
        "by_arm": by_arm, "tasks": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempt", type=Path)
    print(json.dumps(verify(parser.parse_args().attempt), indent=2, allow_nan=False))
