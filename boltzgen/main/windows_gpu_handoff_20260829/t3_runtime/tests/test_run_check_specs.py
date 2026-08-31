from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import implementation


MANIFEST_FIELDS = [
    "spec_id",
    "scaffold_id",
    "scaffold_role",
    "target_id",
    "target_chain",
    "binding_label_seq_ids",
    "cdr1_range",
    "cdr2_range",
    "cdr3_range",
    "cdr1_length",
    "cdr2_length",
    "cdr3_length",
    "spec_path",
    "spec_sha256",
    "scaffold_sha256",
    "target_sha256",
]
EVIDENCE_FIELDS = {
    "schema_version",
    "spec_id",
    "spec_sha256",
    "checker_executable_path",
    "checker_executable_sha256",
    "checker_version",
    "moldir_sha256",
    "runner_sha256",
    "environment_receipt_sha256",
    "argv",
    "exit_code",
    "stdout_sha256",
    "stderr_sha256",
    "check_cif_sha256",
}
POLLUTION_KEYS = {
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONOPTIMIZE",
    "BASH_ENV",
    "ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
}


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_checker(
    path: Path,
    *,
    check_exit: int = 0,
    call_log: Path | None = None,
    tamper_prior_on_check: int = 0,
    cif_name: str = "design.cif",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        f"call_log = pathlib.Path({str(call_log)!r}) if {call_log is not None!r} else None\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('boltzgen 0.3.2')\n"
        "    raise SystemExit(0)\n"
        "if len(sys.argv) != 7 or sys.argv[1] != 'check' or sys.argv[3] != '--output' or sys.argv[5] != '--moldir':\n"
        "    print('bad command', file=sys.stderr)\n"
        "    raise SystemExit(64)\n"
        "if call_log is not None:\n"
        "    with call_log.open('a', encoding='utf-8') as handle:\n"
        "        handle.write(sys.argv[2] + '\\n')\n"
        "    call_count = len(call_log.read_text(encoding='utf-8').splitlines())\n"
        "else:\n"
        "    call_count = 0\n"
        f"if {check_exit}:\n"
        "    print('fixture check failure', file=sys.stderr)\n"
        f"    raise SystemExit({check_exit})\n"
        "output = pathlib.Path(sys.argv[4])\n"
        "output.mkdir(parents=True, exist_ok=False)\n"
        f"(output / {cif_name!r}).write_text('data_fixture\\n#\\n', encoding='utf-8')\n"
        f"if {tamper_prior_on_check} and call_count == {tamper_prior_on_check}:\n"
        "    first = output.parent / '01_pdb_fixture_01-A'\n"
        "    (first / 'late_symlink').symlink_to('/etc/passwd')\n"
        "print('Total designed residues: 3')\n"
        "print('Design specification visualization is written')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def make_campaign(
    root: Path,
    *,
    check_exit: int = 0,
    tamper_prior_on_check: int = 0,
    cif_name: str = "design.cif",
) -> dict[str, Path]:
    campaign = root / "attempt_001"
    project_input = campaign / "project_input"
    specs = project_input / "specs"
    provenance = campaign / "provenance"
    runtime = campaign / "runtime_cache"
    environment = campaign / "environment"
    software = campaign / "software"
    specs.mkdir(parents=True)
    provenance.mkdir()
    runtime.mkdir()
    environment.mkdir()
    software.mkdir()

    runner = software / "run_check_specs.sh"
    runner.write_bytes(implementation("run_check_specs.sh").read_bytes())
    runner.chmod(0o755)
    call_log = campaign / "checker_calls.txt"
    checker = campaign / "env/bin/boltzgen"
    write_checker(
        checker,
        check_exit=check_exit,
        call_log=call_log,
        tamper_prior_on_check=tamper_prior_on_check,
        cif_name=cif_name,
    )
    moldir = runtime / "mols.zip"
    moldir.write_bytes(b"fixture molecule dictionary\n")
    receipt = environment / "t2_receipt.json"
    receipt.write_text(
        '{"status":"ENGINEERING_COMPATIBILITY_ONLY","formal_g1":false}\n',
        encoding="utf-8",
    )

    target_content = b"data_target\n#\n"
    target_sha = hashlib.sha256(target_content).hexdigest()
    rows = []
    for index in range(1, 13):
        spec_id = f"{index:02d}_pdb_fixture_{index:02d}-A"
        spec_dir = specs / spec_id
        spec_dir.mkdir()
        design = spec_dir / "design.yaml"
        design.write_text(f"entities: []\nfixture_index: {index}\n", encoding="utf-8")
        scaffold = spec_dir / "scaffold.cif"
        scaffold.write_text(f"data_scaffold_{index}\n#\n", encoding="utf-8")
        (spec_dir / "scaffold.yaml").write_text(
            "path: scaffold.cif\ndesign:\n- chain:\n    id: A\n    res_index: 26..33,51..57,96..106\n",
            encoding="utf-8",
        )
        (spec_dir / "target.cif").write_bytes(target_content)
        rows.append(
            {
                "spec_id": spec_id,
                "scaffold_id": f"pdb_fixture_{index:02d}-A",
                "scaffold_role": "PRIMARY" if index <= 10 else "RESERVE",
                "target_id": "GLP1_7-36_NH2",
                "target_chain": "E",
                "binding_label_seq_ids": "1,2",
                "cdr1_range": "26..33",
                "cdr2_range": "51..57",
                "cdr3_range": "96..106",
                "cdr1_length": "8",
                "cdr2_length": "7",
                "cdr3_length": "11",
                "spec_path": f"specs/{spec_id}/design.yaml",
                "spec_sha256": sha256(design),
                "scaffold_sha256": sha256(scaffold),
                "target_sha256": target_sha,
            }
        )
    manifest = project_input / "spec_manifest.tsv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=MANIFEST_FIELDS, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    return {
        "campaign": campaign,
        "checker": checker,
        "moldir": moldir,
        "receipt": receipt,
        "manifest": manifest,
        "runner": runner,
        "call_log": call_log,
    }


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in POLLUTION_KEYS:
        environment.pop(key, None)
    environment["PATH"] = "/usr/bin:/bin"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def expected_hashes(fixture: dict[str, Path]) -> dict[str, str]:
    return {
        "checker": sha256(fixture["checker"]),
        "moldir": sha256(fixture["moldir"]),
        "receipt": sha256(fixture["receipt"]),
        "runner": sha256(fixture["runner"]),
    }


def run_runner(
    fixture: dict[str, Path],
    *,
    environment: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    hashes = expected_hashes(fixture)
    hashes.update(overrides or {})
    command = [
        str(fixture["runner"]),
        str(fixture["campaign"]),
        str(fixture["checker"]),
        hashes["checker"],
        str(fixture["moldir"]),
        hashes["moldir"],
        str(fixture["receipt"]),
        hashes["receipt"],
        hashes["runner"],
    ]
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=environment or clean_environment(),
        check=False,
    )


def verify_digest_manifest(root: Path, manifest: Path) -> None:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        assert sha256(root / relative) == expected


def check_call_count(fixture: dict[str, Path]) -> int:
    path = fixture["call_log"]
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def test_runs_exact_twelve_checks_and_emits_replayable_evidence(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path)
    result = run_runner(fixture)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "BOLTZGEN_CHECK_12_OF_12_PASS\n"
    assert check_call_count(fixture) == 12
    campaign = fixture["campaign"]
    output_root = campaign / "project_input/check_outputs"
    log_root = campaign / "provenance/check_logs"
    assert len(list(output_root.iterdir())) == len(list(log_root.iterdir())) == 12
    runner_sha = sha256(fixture["runner"])
    for spec_dir in sorted(output_root.iterdir()):
        assert {path.name for path in spec_dir.iterdir()} == {"design.cif"}
        log = log_root / spec_dir.name
        assert {path.name for path in log.iterdir()} == {
            "check.stdout.log",
            "check.stderr.log",
            "check.exit_code.txt",
            "check.execution.json",
        }
        evidence = json.loads((log / "check.execution.json").read_text(encoding="utf-8"))
        assert set(evidence) == EVIDENCE_FIELDS
        assert evidence["schema_version"] == "BOLTZGEN_CHECK_EXECUTION_V1"
        assert evidence["spec_id"] == spec_dir.name
        assert evidence["runner_sha256"] == runner_sha
        assert evidence["checker_executable_path"] == str(fixture["checker"])
        assert evidence["argv"] == [
            str(fixture["checker"]),
            "check",
            str(campaign / f"project_input/specs/{spec_dir.name}/design.yaml"),
            "--output",
            str(output_root / spec_dir.name),
            "--moldir",
            str(fixture["moldir"]),
        ]
        assert evidence["exit_code"] == 0
        assert (log / "check.exit_code.txt").read_bytes() == b"0\n"
        assert evidence["check_cif_sha256"] == sha256(spec_dir / "design.cif")
    verify_digest_manifest(
        output_root, campaign / "provenance/check_outputs_SHA256SUMS"
    )
    verify_digest_manifest(log_root, campaign / "provenance/check_logs_SHA256SUMS")
    for root in (output_root, log_root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o500
        for path in root.rglob("*"):
            expected_mode = 0o500 if path.is_dir() else 0o400
            assert stat.S_IMODE(path.stat().st_mode) == expected_mode
    assert stat.S_IMODE(
        (campaign / "provenance/check_outputs_SHA256SUMS").stat().st_mode
    ) == 0o400
    assert stat.S_IMODE(
        (campaign / "provenance/check_logs_SHA256SUMS").stat().st_mode
    ) == 0o400


def test_clean_shebang_neutralizes_hostile_bash_env_before_bash_starts(
    tmp_path: Path,
) -> None:
    fixture = make_campaign(tmp_path)
    marker = tmp_path / "bash_env_executed"
    hook = tmp_path / "hostile_bash_env.sh"
    hook.write_text(
        f"unset BASH_ENV\nprintf compromised > {marker}\n", encoding="utf-8"
    )
    environment = clean_environment()
    environment["BASH_ENV"] = str(hook)
    environment["PYTHONPATH"] = "/untrusted"
    environment["PYTHONHOME"] = "/untrusted"

    result = run_runner(fixture, environment=environment)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert check_call_count(fixture) == 12


def test_refuses_rerun_without_overwriting_existing_evidence(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path)
    first = run_runner(fixture)
    assert first.returncode == 0, first.stderr
    evidence = (
        fixture["campaign"]
        / "provenance/check_logs/01_pdb_fixture_01-A/check.execution.json"
    )
    before = evidence.read_bytes()

    second = run_runner(fixture)

    assert second.returncode != 0
    assert "already exists" in second.stderr
    assert evidence.read_bytes() == before
    assert check_call_count(fixture) == 12


def test_rejects_late_spec_hash_drift_before_any_check_execution(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path)
    design = fixture["campaign"] / "project_input/specs/12_pdb_fixture_12-A/design.yaml"
    design.write_text("entities: [tampered]\n", encoding="utf-8")

    result = run_runner(fixture)

    assert result.returncode != 0
    assert "hash differs" in result.stderr
    assert not (fixture["campaign"] / "project_input/check_outputs").exists()
    assert not (fixture["campaign"] / "provenance/check_logs").exists()
    assert check_call_count(fixture) == 0


def test_preserves_nonzero_checker_failure_without_success_evidence(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path, check_exit=17)

    result = run_runner(fixture)

    assert result.returncode == 17
    log = fixture["campaign"] / "provenance/check_logs/01_pdb_fixture_01-A"
    assert (log / "check.exit_code.txt").read_bytes() == b"17\n"
    assert b"fixture check failure" in (log / "check.stderr.log").read_bytes()
    assert not (log / "check.execution.json").exists()
    assert not (fixture["campaign"] / "provenance/check_outputs_SHA256SUMS").exists()


def test_rejects_provenance_symlink_without_external_writes(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path / "campaign")
    provenance = fixture["campaign"] / "provenance"
    provenance.rmdir()
    external = tmp_path / "external"
    external.mkdir()
    provenance.symlink_to(external, target_is_directory=True)

    result = run_runner(fixture)

    assert result.returncode != 0
    assert "provenance root" in result.stderr
    assert list(external.iterdir()) == []
    assert check_call_count(fixture) == 0


def test_rejects_intermediate_spec_symlink_before_checker(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path / "campaign")
    spec_dir = fixture["campaign"] / "project_input/specs/12_pdb_fixture_12-A"
    external = tmp_path / "external_spec"
    spec_dir.rename(external)
    spec_dir.symlink_to(external, target_is_directory=True)

    result = run_runner(fixture)

    assert result.returncode != 0
    assert "spec directory 12_pdb_fixture_12-A" in result.stderr
    assert not (fixture["campaign"] / "project_input/check_outputs").exists()
    assert check_call_count(fixture) == 0


@pytest.mark.parametrize(
    "sentinel_name", ["check_outputs_SHA256SUMS", "check_logs_SHA256SUMS"]
)
def test_preexisting_digest_manifest_is_never_overwritten(
    tmp_path: Path, sentinel_name: str
) -> None:
    fixture = make_campaign(tmp_path)
    sentinel = fixture["campaign"] / "provenance" / sentinel_name
    sentinel.write_bytes(b"do not overwrite\n")

    result = run_runner(fixture)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert sentinel.read_bytes() == b"do not overwrite\n"
    assert not (fixture["campaign"] / "project_input/check_outputs").exists()
    assert not (fixture["campaign"] / "provenance/check_logs").exists()
    assert check_call_count(fixture) == 0


@pytest.mark.parametrize("hash_name", ["checker", "moldir", "receipt", "runner"])
def test_rejects_wrong_trusted_hash_before_writes(
    tmp_path: Path, hash_name: str
) -> None:
    fixture = make_campaign(tmp_path)

    result = run_runner(fixture, overrides={hash_name: "0" * 64})

    assert result.returncode != 0
    assert "hash differs" in result.stderr
    assert not (fixture["campaign"] / "project_input/check_outputs").exists()
    assert not (fixture["campaign"] / "provenance/check_logs").exists()
    assert check_call_count(fixture) == 0


def test_ignores_untrusted_path_tools(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path / "campaign")
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    for name in ("python3", "sha256sum", "readlink", "stat"):
        tool = fake_bin / name
        tool.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        tool.chmod(0o755)
    environment = clean_environment()
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"

    result = run_runner(fixture, environment=environment)

    assert result.returncode == 0, result.stderr
    assert check_call_count(fixture) == 12


def test_final_tree_rejects_late_tampering_of_prior_output(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path, tamper_prior_on_check=12)

    result = run_runner(fixture)

    assert result.returncode != 0
    assert "symlink forbidden in final evidence tree" in result.stderr
    assert check_call_count(fixture) == 12
    assert not (fixture["campaign"] / "provenance/check_outputs_SHA256SUMS").exists()
    assert not (fixture["campaign"] / "provenance/check_logs_SHA256SUMS").exists()


def test_manifest_publication_never_exposes_a_writable_success_tree(
    tmp_path: Path,
) -> None:
    fixture = make_campaign(tmp_path)
    hashes = expected_hashes(fixture)
    command = [
        str(fixture["runner"]),
        str(fixture["campaign"]),
        str(fixture["checker"]),
        hashes["checker"],
        str(fixture["moldir"]),
        hashes["moldir"],
        str(fixture["receipt"]),
        hashes["receipt"],
        hashes["runner"],
    ]
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_environment(),
    )
    state = {"attempted": False, "succeeded": False}
    output_manifest = fixture["campaign"] / "provenance/check_outputs_SHA256SUMS"
    first_cif = (
        fixture["campaign"]
        / "project_input/check_outputs/01_pdb_fixture_01-A/design.cif"
    )

    def attack_after_publication() -> None:
        while True:
            if output_manifest.exists():
                state["attempted"] = True
                try:
                    with first_cif.open("ab") as handle:
                        handle.write(b"late mutation\n")
                    state["succeeded"] = True
                except PermissionError:
                    pass
                return
            if process.poll() is not None:
                return
            time.sleep(0.0005)

    attacker = threading.Thread(target=attack_after_publication)
    attacker.start()
    stdout, stderr = process.communicate(timeout=60)
    attacker.join(timeout=5)

    assert process.returncode == 0, stderr
    assert stdout == "BOLTZGEN_CHECK_12_OF_12_PASS\n"
    assert state == {"attempted": True, "succeeded": False}
    verify_digest_manifest(
        fixture["campaign"] / "project_input/check_outputs", output_manifest
    )


def test_rejects_non_replayable_cif_filename_before_manifest_publication(
    tmp_path: Path,
) -> None:
    fixture = make_campaign(tmp_path, cif_name="design\ninjected.cif")

    result = run_runner(fixture)

    assert result.returncode != 0
    assert "unsafe check output name" in result.stderr
    assert check_call_count(fixture) == 1
    assert not (fixture["campaign"] / "provenance/check_outputs_SHA256SUMS").exists()
    assert not (fixture["campaign"] / "provenance/check_logs_SHA256SUMS").exists()


def test_rejects_symlink_campaign_and_checker_outside_env_layout(tmp_path: Path) -> None:
    fixture = make_campaign(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(fixture["campaign"], target_is_directory=True)
    aliased = dict(fixture)
    aliased["campaign"] = alias
    result = run_runner(aliased)
    assert result.returncode != 0
    assert "campaign root" in result.stderr

    separate = make_campaign(tmp_path / "separate")
    external_checker = tmp_path / "external_checker"
    write_checker(external_checker)
    separate["checker"] = external_checker
    result = run_runner(separate)
    assert result.returncode != 0
    assert "outside the canonical campaign env layout" in result.stderr
    assert not (separate["campaign"] / "project_input/check_outputs").exists()


def test_source_invokes_only_version_and_non_inference_check_commands() -> None:
    source = implementation("run_check_specs.sh").read_text(encoding="utf-8")
    checker_lines = [line.strip() for line in source.splitlines() if '"$CHECKER"' in line]

    assert source.splitlines()[0] == (
        "#!/usr/bin/env -S -i PATH=/usr/bin:/bin "
        "BOLTZGEN_CLEAN_LAUNCH=1 /bin/bash"
    )
    assert any(line.startswith('CHECKER_VERSION=$("$CHECKER" --version)') for line in checker_lines)
    assert any(line.startswith('"$CHECKER" check "$SPEC"') for line in checker_lines)
    command_text = "\n".join(checker_lines)
    for forbidden in (" configure ", " execute ", "--reuse", "checkpoint", "train", "lockbox"):
        assert forbidden not in command_text.lower()
