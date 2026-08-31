import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_local_anchor_set.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_source_manifest(source: Path) -> None:
    manifest = source / "operator_logs/OUTPUT_SHA256SUMS"
    rows = []
    for path in source.rglob("*"):
        if path.is_file() and path != manifest:
            rows.append((path.relative_to(source).as_posix(), digest(path)))
    rows.sort()
    manifest.write_text(
        "".join(f"{value}  ./{relative}\n" for relative, value in rows),
        encoding="utf-8",
    )


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    logs = source / "operator_logs"
    logs.mkdir(parents=True)
    spec = tmp_path / "spec"
    spec.mkdir()
    for name in ("design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"):
        (spec / name).write_text(f"fixture {name}\n", encoding="utf-8")
    (logs / "spec_bundle_before.SHA256SUMS").write_text(
        "".join(
            f"{digest(spec / name)}  ./{name}\n"
            for name in sorted(path.name for path in spec.iterdir())
        ),
        encoding="utf-8",
    )

    metrics_dir = source / "final_ranked_designs"
    final_dir = metrics_dir / "final_3_designs"
    final_dir.mkdir(parents=True)
    inverse = source / "intermediate_designs_inverse_folded"
    (inverse / "fold_out_npz").mkdir(parents=True)
    (inverse / "refold_cif").mkdir()
    raw = source / "intermediate_designs"
    raw.mkdir()
    fields = [
        "id",
        "final_rank",
        "designed_sequence",
        "designed_chain_sequence",
        "pass_filters",
        "pass_filter_rmsd_filter",
        "pass_filter_rmsd_design_filter",
        "pass_bindsite_under_8rmsd_filter",
        "filter_rmsd",
        "filter_rmsd_design",
        "bindsite_under_8rmsd",
        "design_to_target_iptm",
        "design_ptm",
        "min_design_to_target_pae",
        "plip_hbonds_refolded",
        "liability_score",
        "quality_score",
    ]
    rows = []
    for rank, candidate in enumerate(("design_b", "design_a", "design_c"), 1):
        rows.append(
            {
                "id": candidate,
                "final_rank": str(rank),
                "designed_sequence": f"SEQ{rank}",
                "designed_chain_sequence": f"CHAIN{rank}",
                "pass_filters": "False",
                "pass_filter_rmsd_filter": "False",
                "pass_filter_rmsd_design_filter": "True",
                "pass_bindsite_under_8rmsd_filter": "False",
                "filter_rmsd": str(6 + rank),
                "filter_rmsd_design": "0.7",
                "bindsite_under_8rmsd": "0.0",
                "design_to_target_iptm": "0.4",
                "design_ptm": "0.72",
                "min_design_to_target_pae": "7.0",
                "plip_hbonds_refolded": "4",
                "liability_score": "140",
                "quality_score": str(1 - (rank - 1) / 2),
            }
        )
        (final_dir / f"rank{rank:02d}_{candidate}.cif").write_bytes(
            f"refold {candidate}".encode()
        )
        (inverse / f"{candidate}.cif").write_bytes(f"design {candidate}".encode())
        (inverse / "refold_cif" / f"{candidate}.cif").write_bytes(
            f"refold {candidate}".encode()
        )
        (raw / f"{candidate}.cif").write_bytes(f"raw {candidate}".encode())
        np.savez(raw / f"{candidate}.npz", design_mask=np.ones(3, dtype=np.int8))
        np.savez(inverse / f"{candidate}.npz", design_mask=np.ones(3, dtype=np.int8))
        np.savez(
            inverse / "fold_out_npz" / f"{candidate}.npz",
            coords=np.ones((5, 3, 3), dtype=np.float32),
            design_ptm=np.ones(5, dtype=np.float32),
        )
    with (metrics_dir / "all_designs_metrics.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    receipt = {
        "status": "EXPLORATORY_INFERENCE_COMPLETE",
        "exit_code": 0,
        "expected_designs": 3,
        "observed_designs": 3,
        "fold_samples_per_candidate": 5,
        "filter_pass_count": 0,
        "cuda_oom_detected": False,
        "output_validation": {"status": "PASS"},
        "resolved_config_contract": {
            "status": "PASS",
            "checks": {"design.yaml_path": [str(spec / "design.yaml")]},
        },
    }
    (logs / "EXPLORATORY_INFERENCE.json").write_text(
        json.dumps(receipt) + "\n", encoding="utf-8"
    )
    evidence = {
        "cell_contract.json": {"status": "PASS"},
        "resolved_config_contract.json": receipt["resolved_config_contract"],
        "LOCAL_ENV_ACCEPTANCE.json": {"status": "LOCAL_ENV_READY"},
    }
    for name, payload in evidence.items():
        (logs / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    (logs / "runtime_assets_used.SHA256SUMS").write_text("fixture\n", encoding="utf-8")
    (logs / "source_commit.txt").write_text("a" * 40 + "\n", encoding="utf-8")
    (logs / "source_tree.txt").write_text("b" * 40 + "\n", encoding="utf-8")
    config = source / "config"
    config.mkdir()
    for name in ("design.yaml", "inverse_folding.yaml", "folding.yaml", "analysis.yaml", "filtering.yaml"):
        (config / name).write_text(f"fixture: {name}\n", encoding="utf-8")
    (logs / "resolved_config.SHA256SUMS").write_text(
        "".join(
            f"{digest(config / name)}  config/{name}\n"
            for name in sorted(path.name for path in config.iterdir())
        ),
        encoding="utf-8",
    )
    (metrics_dir / "final_designs_metrics_3.csv").write_text(
        (metrics_dir / "all_designs_metrics.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (inverse / "aggregate_metrics_analyze.csv").write_text(
        (metrics_dir / "all_designs_metrics.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    write_source_manifest(source)
    return source


def test_builds_ranked_self_contained_anchor_set(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    output = tmp_path / "anchors"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-run",
            str(source),
            "--output",
            str(output),
            "--anchor-count",
            "2",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads((output / "ANCHOR_SET.json").read_text(encoding="utf-8"))
    assert payload["status"] == "LOCAL_ANCHOR_SET_READY"
    assert payload["selection"]["denominator"] == 3
    assert [row["candidate_id"] for row in payload["anchors"]] == [
        "design_b",
        "design_a",
    ]
    assert payload["anchors"][0]["metrics"]["filter_gate_map"] == {
        "pass_bindsite_under_8rmsd_filter": False,
        "pass_filter_rmsd_design_filter": True,
        "pass_filter_rmsd_filter": False,
    }
    assert (output / "inputs/spec_bundle/target.cif").is_file()
    assert (output / "anchors/rank01_design_b/refold_samples.npz").is_file()
    assert (output / "anchors/rank01_design_b/raw_design.cif").is_file()
    manifest = output / "SHA256SUMS"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ./", 1)
        assert digest(output / relative) == expected


def test_rejects_tampered_source_before_publishing_ready(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (source / "final_ranked_designs/all_designs_metrics.csv").write_text(
        "tampered\n", encoding="utf-8"
    )
    output = tmp_path / "anchors"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-run", str(source), "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not output.exists()
    failures = list(tmp_path.glob("anchors.FAILED_*"))
    assert len(failures) == 1
    assert (failures[0] / "STATUS.txt").read_text(encoding="utf-8").strip() == (
        "LOCAL_ANCHOR_SET_FAILED"
    )
    assert not (failures[0] / "ANCHOR_SET.json").exists()


def test_rejects_non_boolean_filter_gate_even_when_source_manifest_matches(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    metrics = source / "final_ranked_designs/all_designs_metrics.csv"
    text = metrics.read_text(encoding="utf-8").replace(",False,False,True,False,", ",False,unknown,True,False,", 1)
    metrics.write_text(text, encoding="utf-8")
    write_source_manifest(source)
    output = tmp_path / "anchors"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-run", str(source), "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "strict boolean required" in result.stderr
    assert not output.exists()
