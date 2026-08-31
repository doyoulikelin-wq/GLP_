from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

import yaml

from conftest import run_python, sha256
from test_build_design_specs import MANIFEST_FIELDS, write_tsv


ONE_TO_THREE = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
    "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
    "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
    "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
}
TARGET_SEQUENCE = "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR"
SCAFFOLD_SEQUENCE = "ACDEFG"
DESIGN_POSITIONS = {2, 4, 6}


def mmcif_bytes(
    chains: list[tuple[str, str]], *, annotate_check: bool = False,
) -> bytes:
    lines = ["data_model", "loop_", "_entity_poly_seq.entity_id", "_entity_poly_seq.num", "_entity_poly_seq.mon_id"]
    for entity_index, (_, sequence) in enumerate(chains, start=1):
        for number, residue in enumerate(sequence, start=1):
            lines.append(f"{entity_index} {number} {ONE_TO_THREE[residue]}")
    lines.extend(["#", "loop_", "_struct_asym.id", "_struct_asym.entity_id"])
    for entity_index, (chain, _) in enumerate(chains, start=1):
        lines.append(f"{chain} {entity_index}")
    lines.extend([
        "#", "loop_", "_atom_site.group_PDB", "_atom_site.label_asym_id",
        "_atom_site.label_entity_id", "_atom_site.label_seq_id",
        "_atom_site.label_atom_id", "_atom_site.label_comp_id",
        "_atom_site.Cartn_x", "_atom_site.Cartn_y", "_atom_site.Cartn_z",
        "_atom_site.occupancy", "_atom_site.B_iso_or_equiv",
        "_atom_site.pdbx_PDB_model_num",
    ])
    atom_id = 0
    for entity_index, (chain, sequence) in enumerate(chains, start=1):
        for number, residue in enumerate(sequence, start=1):
            if annotate_check and chain == "E":
                b_factor = 80 if number in {1, 2} else 0
            elif annotate_check and chain == "A":
                b_factor = 100 if number in DESIGN_POSITIONS else 0
            else:
                b_factor = 0
            for atom_name, offset in (("N", 0.0), ("CA", 0.2), ("C", 0.4)):
                atom_id += 1
                x = entity_index * 100 + number + offset
                lines.append(
                    f"ATOM {chain} {entity_index} {number} {atom_name} "
                    f"{ONE_TO_THREE[residue]} {x:.1f} {number:.1f} {offset:.1f} 1 {b_factor} 1"
                )
    lines.extend(["#", ""])
    return "\n".join(lines).encode("utf-8")


def write_fake_checker(path: Path, check_cif: bytes) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        f"CHECK_CIF = {check_cif!r}\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('boltzgen 0.3.2')\n"
        "    raise SystemExit(0)\n"
        "if len(sys.argv) != 7 or sys.argv[1] != 'check' or sys.argv[3] != '--output' or sys.argv[5] != '--moldir':\n"
        "    print('bad command', file=sys.stderr)\n"
        "    raise SystemExit(64)\n"
        "output = pathlib.Path(sys.argv[4])\n"
        "output.mkdir(parents=True, exist_ok=False)\n"
        "(output / 'design.cif').write_bytes(CHECK_CIF)\n"
        "print('Total designed residues: 3')\n"
        "print('Design specification visualization is written')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def make_fixture(root: Path) -> dict[str, object]:
    inputs = root / "02_inputs"
    specs = inputs / "specs"
    checks = inputs / "check_outputs"
    logs = root / "logs" / "check_logs"
    screenshots = root / "logs" / "check_screenshots"
    screenshots.mkdir(parents=True)
    checker = root / "formal_env" / "bin" / "boltzgen"
    checker.parent.mkdir(parents=True)
    combined_cif = mmcif_bytes([("E", TARGET_SEQUENCE), ("A", SCAFFOLD_SEQUENCE)], annotate_check=True)
    write_fake_checker(checker, combined_cif)
    moldir = root / "runtime_cache" / "mols.zip"
    moldir.parent.mkdir()
    moldir.write_bytes(b"fixture frozen chemical dictionary\n")
    runner = root / "software" / "run_check_specs.sh"
    runner.parent.mkdir()
    runner.write_bytes(b"#!/bin/sh\n# frozen T4 runner fixture\n")
    environment_receipt = root / "formal_env" / "receipt.json"
    environment_receipt.write_text('{"status":"PASS","formal_g1":true}\n', encoding="utf-8")

    manifest_rows: list[dict[str, object]] = []
    review_rows: list[dict[str, object]] = []
    target_sha = ""
    for index in range(1, 13):
        spec_id = f"{index:02d}_pdb_fixture_{index:02d}-A"
        spec_dir = specs / spec_id
        spec_dir.mkdir(parents=True)
        design = spec_dir / "design.yaml"
        design.write_text(
            yaml.safe_dump({
                "entities": [
                    {"file": {
                        "path": "target.cif",
                        "include": [{"chain": {"id": "E", "res_index": "1..30"}}],
                        "binding_types": [{"chain": {"id": "E", "binding": "1..2"}}],
                        "structure_groups": [{"group": {"id": "E", "visibility": 1}}],
                    }},
                    {"file": {"path": "scaffold.yaml"}},
                ]
            }, sort_keys=False),
            encoding="utf-8",
        )
        scaffold_yaml = spec_dir / "scaffold.yaml"
        scaffold_yaml.write_text(
            yaml.safe_dump({
                "path": "scaffold.cif",
                "include": [{"chain": {"id": "A"}}],
                "design": [{"chain": {"id": "A", "res_index": "2..2,4..4,6..6"}}],
                "structure_groups": [
                    {"group": {"id": "A", "visibility": 2}},
                    {"group": {"id": "A", "visibility": 0, "res_index": "2..2,4..4,6..6"}},
                ],
                "reset_res_index": [{"chain": {"id": "A"}}],
            }, sort_keys=False),
            encoding="utf-8",
        )
        scaffold = spec_dir / "scaffold.cif"
        target = spec_dir / "target.cif"
        scaffold.write_bytes(mmcif_bytes([("A", SCAFFOLD_SEQUENCE)]))
        target.write_bytes(mmcif_bytes([("E", TARGET_SEQUENCE)]))
        target_sha = sha256(target)
        manifest_rows.append(dict(zip(MANIFEST_FIELDS, [
            spec_id, f"pdb_fixture_{index:02d}-A",
            "PRIMARY" if index <= 10 else "RESERVE", "GLP1_7-36_NH2", "E", "1,2",
            "2..2", "4..4", "6..6", "1", "1", "1",
            f"specs/{spec_id}/design.yaml", sha256(design), sha256(scaffold), target_sha,
        ])))

        check_dir = checks / spec_id
        command = [
            str(checker), "check", str(design), "--output", str(check_dir),
            "--moldir", str(moldir),
        ]
        completed = subprocess.run(command, capture_output=True, check=False)
        assert completed.returncode == 0
        log_dir = logs / spec_id
        log_dir.mkdir(parents=True)
        stdout_path = log_dir / "check.stdout.log"
        stderr_path = log_dir / "check.stderr.log"
        stdout_path.write_bytes(completed.stdout)
        stderr_path.write_bytes(completed.stderr)
        (log_dir / "check.exit_code.txt").write_bytes(b"0\n")
        remote_executable = "/gpu/boltzgen_glp1_v1/env/bin/boltzgen"
        evidence = {
            "schema_version": "BOLTZGEN_CHECK_EXECUTION_V1",
            "spec_id": spec_id,
            "spec_sha256": sha256(design),
            "checker_executable_path": remote_executable,
            "checker_executable_sha256": sha256(checker),
            "checker_version": "boltzgen 0.3.2",
            "moldir_sha256": sha256(moldir),
            "runner_sha256": sha256(runner),
            "environment_receipt_sha256": sha256(environment_receipt),
            "argv": [
                remote_executable, "check",
                f"/gpu/boltzgen_glp1_v1/project_input/specs/{spec_id}/design.yaml",
                "--output",
                f"/gpu/boltzgen_glp1_v1/project_input/check_outputs/{spec_id}",
                "--moldir", "/gpu/boltzgen_glp1_v1/runtime_cache/mols.zip",
            ],
            "exit_code": 0,
            "stdout_sha256": sha256(stdout_path),
            "stderr_sha256": sha256(stderr_path),
            "check_cif_sha256": sha256(check_dir / "design.cif"),
        }
        (log_dir / "check.execution.json").write_text(
            json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8"
        )
        screenshot = screenshots / f"{spec_id}.png"
        screenshot.write_bytes(b"fixture screenshot supplement")
        review_rows.append({
            "spec_id": spec_id, "machine_status": "PASS", "manual_status": "PASS",
            "reviewer": "fixture-reviewer", "reviewed_at_utc": "2026-08-30T00:00:00Z",
            "screenshot_path": str(screenshot.relative_to(root)), "notes": "fixture",
        })

    manifest = inputs / "spec_manifest.tsv"
    review = inputs / "check_review.tsv"
    write_tsv(manifest, MANIFEST_FIELDS, manifest_rows)
    write_tsv(review, list(review_rows[0]), review_rows)
    machine_args = [
        "--check-log-root", logs,
        "--boltzgen-executable", checker,
        "--expected-boltzgen-sha256", sha256(checker),
        "--moldir", moldir,
        "--expected-moldir-sha256", sha256(moldir),
        "--check-runner", runner,
        "--expected-check-runner-sha256", sha256(runner),
        "--environment-receipt", environment_receipt,
        "--expected-environment-receipt-sha256", sha256(environment_receipt),
    ]
    return {
        "manifest": manifest, "checks": checks, "logs": logs, "review": review,
        "target_sha": target_sha, "machine_args": machine_args,
    }


def invoke(fixture: dict[str, object], output: Path, *, include_machine: bool = True):
    arguments: list[object] = [
        "--spec-manifest", fixture["manifest"],
        "--check-root", fixture["checks"],
        "--manual-review", fixture["review"],
        "--expected-target-sha256", fixture["target_sha"],
    ]
    if include_machine:
        arguments.extend(fixture["machine_args"])
    arguments.extend(["--output", output])
    return run_python("verify_specs.py", *arguments)


def test_verifies_machine_manual_hash_and_replay_contract(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    output = tmp_path / "01_provenance" / "spec_verification.json"
    output.parent.mkdir()
    result = invoke(fixture, output)
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "t3_spec_verification_v2"
    assert payload["status"] == "PASS"
    assert payload["spec_count"] == payload["machine_pass_count"] == payload["manual_pass_count"] == 12
    assert len(payload["specs"]) == 12
    assert all(record["check_cif_atom_count"] > 0 for record in payload["specs"])
    assert all(record["machine_evidence_sha256"] for record in payload["specs"])
    first_bytes = output.read_bytes()
    second = invoke(fixture, output)
    assert second.returncode == 0, second.stderr
    assert output.read_bytes() == first_bytes


def test_rejects_tampering_or_manual_failure_without_overwriting(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    review = fixture["review"]
    rows = list(csv.DictReader(review.open(newline="", encoding="utf-8"), delimiter="\t"))
    rows[0]["manual_status"] = "FAIL"
    write_tsv(review, list(rows[0]), rows)
    output = tmp_path / "result.json"
    result = invoke(fixture, output)
    assert result.returncode != 0
    assert not output.exists()

    rows[0]["manual_status"] = "PASS"
    write_tsv(review, list(rows[0]), rows)
    first_spec = next((tmp_path / "02_inputs" / "specs").iterdir()) / "design.yaml"
    first_spec.write_text("entities: []\n", encoding="utf-8")
    result = invoke(fixture, output)
    assert result.returncode != 0
    assert not output.exists()


def test_rejects_self_consistent_invalid_yaml_and_fake_check_cif(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path / "fake_cif")
    first_check = next(Path(fixture["checks"]).iterdir()) / "design.cif"
    first_check.write_text(
        "data_fake\nloop_\n_atom_site.id\n_atom_site.Cartn_x\n1 0.0\n",
        encoding="utf-8",
    )
    output = tmp_path / "invalid_but_self_consistent.json"
    result = invoke(fixture, output)
    assert result.returncode != 0
    assert not output.exists()

    yaml_fixture = make_fixture(tmp_path / "invalid_yaml")
    manifest = Path(yaml_fixture["manifest"])
    manifest_rows = list(csv.DictReader(manifest.open(newline="", encoding="utf-8"), delimiter="\t"))
    spec_id = manifest_rows[0]["spec_id"]
    design = manifest.parent / "specs" / spec_id / "design.yaml"
    design.write_text("entities: []\n", encoding="utf-8")
    manifest_rows[0]["spec_sha256"] = sha256(design)
    write_tsv(manifest, MANIFEST_FIELDS, manifest_rows)
    evidence_path = Path(yaml_fixture["logs"]) / spec_id / "check.execution.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["spec_sha256"] = sha256(design)
    evidence_path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    yaml_output = tmp_path / "invalid_yaml.json"
    result = invoke(yaml_fixture, yaml_output)
    assert result.returncode != 0
    assert not yaml_output.exists()


def test_fails_closed_without_t4_execution_evidence(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    output = tmp_path / "missing_evidence.json"
    result = invoke(fixture, output, include_machine=False)
    assert result.returncode != 0
    assert "T4 runner must provide" in result.stderr
    assert not output.exists()


def test_rejects_machine_evidence_or_exit_log_drift(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    first_log = next(Path(fixture["logs"]).iterdir())
    (first_log / "check.exit_code.txt").write_bytes(b"1\n")
    output = tmp_path / "drift.json"
    result = invoke(fixture, output)
    assert result.returncode != 0
    assert not output.exists()


def test_rejects_noncanonical_parent_paths(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path / "fixture")
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias_parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    result = invoke(fixture, alias_parent / "verification.json")
    assert result.returncode != 0
    assert not (real_parent / "verification.json").exists()

    inputs_alias = tmp_path / "inputs_alias"
    inputs_alias.symlink_to(Path(fixture["manifest"]).parent, target_is_directory=True)
    aliased_fixture = dict(fixture)
    aliased_fixture["manifest"] = inputs_alias / "spec_manifest.tsv"
    result = invoke(aliased_fixture, tmp_path / "verification.json")
    assert result.returncode != 0
    assert not (tmp_path / "verification.json").exists()


def test_rejects_suffix_only_or_noncanonical_execution_argv(tmp_path: Path) -> None:
    for case_name, replacement in (
        (
            "wrong_remote_root",
            "/foreign/project_input/specs/{spec_id}/design.yaml",
        ),
        (
            "dotdot",
            "/gpu/boltzgen_glp1_v1/project_input/other/../specs/{spec_id}/design.yaml",
        ),
    ):
        case = tmp_path / case_name
        fixture = make_fixture(case)
        first_log = sorted(Path(fixture["logs"]).iterdir())[0]
        evidence_path = first_log / "check.execution.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["argv"][2] = replacement.format(spec_id=evidence["spec_id"])
        evidence_path.write_text(
            json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8"
        )
        result = invoke(fixture, case / "blocked.json")
        assert result.returncode != 0
        assert not (case / "blocked.json").exists()
