from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import run_python, sha256


AIV1_DIRECTORY = (
    Path(__file__).resolve().parents[3] / "aiv1_technical_gate_20260828"
)
sys.path.insert(0, str(AIV1_DIRECTORY))

from build_ai_validation_matrix import (  # noqa: E402
    ANCHOR_FIELDS,
    canonical_json,
    extract_mmcif_canonical_sequences,
)
from test_build_ai_validation_matrix import SyntheticAIV1Fixture  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_g2_anchors as release  # noqa: E402


PRODUCTION_ANCHOR_RELATIVE = Path(
    "data/boltzgen_data/glp1_vhh_production_v1/07_analysis/ai_validation/"
    "anchor_candidate_set_v1.tsv"
)
PRODUCTION_RECEIPT_RELATIVE = Path(
    "data/boltzgen_data/glp1_vhh_production_v1/04_pilot/g2/"
    "G2_anchor_release.receipt.json"
)


def bind_g2_markers_to_formal_g1(
    fixture: SyntheticAIV1Fixture, g1_receipt: Path
) -> None:
    formal_sha = sha256(g1_receipt)
    environment_sha = sha256(fixture.environment_manifest_path)
    marker_paths = (
        fixture.acceptance_success_path,
        fixture.probe_paths["diverse"]["success"],
        fixture.probe_paths["adherence"]["success"],
    )
    for marker in marker_paths:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["formal_g1_receipt_sha256"] = formal_sha
        payload["environment_manifest_sha256"] = environment_sha
        marker.write_text(canonical_json(payload), encoding="utf-8")
    gate = json.loads(fixture.g2_gate_path.read_text(encoding="utf-8"))
    gate["acceptance_success_sha256"] = sha256(fixture.acceptance_success_path)
    gate["probe_success_sha256"] = {
        name: sha256(fixture.probe_paths[name]["success"])
        for name in ("diverse", "adherence")
    }
    fixture.g2_gate_path.write_text(canonical_json(gate), encoding="utf-8")


def make_fixture(root: Path) -> tuple[SyntheticAIV1Fixture, Path, Path, Path]:
    fixture = SyntheticAIV1Fixture(root)
    g1_receipt = fixture.provenance_directory / "G1.receipt.json"
    g1_receipt.write_text(
        canonical_json(
            {
                "schema_version": "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1",
                "attempt_id": "formal_g1_attempt_001",
                "environment_contract_revision": (
                    "WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V1"
                ),
                "exit_code": 0,
                "status": "G1_PASS",
                "formal_g1": True,
                "failure_codes": [],
                "failure_stage": None,
                "environment_contract_revision_required": False,
                "compatibility_activation": "EXPLICIT_PROCESS_LOCAL_ONLY",
                "official_contract": {
                    "boltzgen": "0.3.2",
                    "cuequivariance": "0.6.1",
                    "torch": "2.8.0+cu128",
                    "torch_cuda": "12.8",
                    "triton": "3.4.0",
                },
                "environment_manifest_sha256": sha256(
                    fixture.environment_manifest_path
                ),
            }
        ),
        encoding="utf-8",
    )
    original_rebind = fixture.rebind_g2

    def rebind_with_formal_g1(*args, **kwargs) -> None:
        original_rebind(*args, **kwargs)
        bind_g2_markers_to_formal_g1(fixture, g1_receipt)

    fixture.rebind_g2 = rebind_with_formal_g1  # type: ignore[method-assign]
    bind_g2_markers_to_formal_g1(fixture, g1_receipt)
    anchor_output = fixture.workspace / PRODUCTION_ANCHOR_RELATIVE
    receipt_output = fixture.workspace / PRODUCTION_RECEIPT_RELATIVE
    return fixture, g1_receipt, anchor_output, receipt_output


def invoke(
    fixture: SyntheticAIV1Fixture,
    g1_receipt: Path,
    anchor_output: Path,
    receipt_output: Path,
):
    return run_python(
        "release_g2_anchors.py",
        *release_arguments(fixture, g1_receipt, anchor_output, receipt_output),
    )


def release_arguments(
    fixture: SyntheticAIV1Fixture,
    g1_receipt: Path,
    anchor_output: Path,
    receipt_output: Path,
) -> list[object]:
    return [
        "--workspace-root",
        fixture.workspace,
        "--repo-root",
        fixture.repo,
        "--acceptance-root",
        fixture.acceptance_root,
        "--g1-receipt",
        g1_receipt,
        "--g1-receipt-sha256",
        sha256(g1_receipt),
        "--aiv0-final-receipt",
        fixture.aiv0_receipt_path,
        "--platform-evidence",
        fixture.platform_path,
        "--environment-manifest",
        fixture.environment_manifest_path,
        "--runtime-scripts-manifest",
        fixture.runtime_scripts_manifest_path,
        "--anchor-output",
        anchor_output,
        "--receipt-output",
        receipt_output,
    ]


def assert_strict_aiv1_release(
    fixture: SyntheticAIV1Fixture, anchor_output: Path, receipt_output: Path
) -> None:
    input_contract = json.loads(fixture.contract_path.read_text(encoding="utf-8"))
    release.validate_release_with_frozen_aiv1(
        anchor_manifest_path=anchor_output,
        g2_receipt_path=receipt_output,
        input_contract=input_contract,
        aiv0_handoff={
            "aiv0_final_check_receipt_sha256": sha256(fixture.aiv0_receipt_path)
        },
        repo_root=fixture.repo,
        workspace_root=fixture.workspace,
    )


def test_release_passes_the_real_aiv1_g2_validator(tmp_path: Path) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)

    # The fixture carries every strict dependency that the earlier shallow
    # release test omitted.
    platform = json.loads(fixture.platform_path.read_text(encoding="utf-8"))
    assert platform["gpu_compute_capability"] == "8.0"
    for cell in (
        fixture.acceptance_directory,
        fixture.probe_paths["diverse"]["root"],
        fixture.probe_paths["adherence"]["root"],
    ):
        inverse = cell / "intermediate_designs_inverse_folded"
        assert len(list((cell / "intermediate_designs").glob("*.cif"))) == 10
        assert len(list((cell / "intermediate_designs").glob("*.npz"))) == 10
        assert len(list(inverse.glob("*.cif"))) == 10
        assert len(list(inverse.glob("*.npz"))) == 10
        folds = sorted((inverse / "fold_out_npz").glob("*.npz"))
        assert len(folds) == 10
        assert len(list((inverse / "refold_cif").glob("*.cif"))) == 10
        with np.load(folds[0], allow_pickle=False) as arrays:
            assert arrays["coords"].shape[0] == 5

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode == 0, result.stderr
    assert anchors.relative_to(fixture.workspace) == PRODUCTION_ANCHOR_RELATIVE
    assert receipt.relative_to(fixture.workspace) == PRODUCTION_RECEIPT_RELATIVE
    assert receipt.name == "G2_anchor_release.receipt.json"

    # Invoke the production downstream validator, not a test-local
    # approximation. It rechecks the complete G2 evidence chain.
    assert_strict_aiv1_release(fixture, anchors, receipt)

    with anchors.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = list(reader)
        assert tuple(reader.fieldnames or ()) == ANCHOR_FIELDS
    assert len(rows) == 10
    assert [row["anchor_order"] for row in rows] == [str(index) for index in range(10)]
    assert [row["candidate_id"] for row in rows] == sorted(
        row["candidate_id"] for row in rows
    )
    for row in rows:
        assert "/refold_cif/" in row["candidate_artifact_uri"]
        artifact = fixture.workspace / row["candidate_artifact_uri"].removeprefix(
            "workspace://"
        )
        assert row["full_sequence"] in extract_mmcif_canonical_sequences(artifact)

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "AIV1_G2_ANCHOR_RELEASE_V2"
    assert payload["checkpoint_sha256"] == fixture.generation["checkpoint_sha256"]
    assert payload["aiv0_final_check_receipt_sha256"] == sha256(
        fixture.aiv0_receipt_path
    )
    assert payload["formal_g1_receipt_uri"] == (
        f"workspace://{g1.relative_to(fixture.workspace).as_posix()}"
    )
    assert payload["formal_g1_receipt_path"] == str(g1)
    assert payload["formal_g1_receipt_sha256"] == sha256(g1)
    assert payload["environment_manifest_sha256"] == sha256(
        fixture.environment_manifest_path
    )
    for manifest in (
        fixture.output_manifest_path,
        fixture.probe_paths["diverse"]["output_manifest"],
        fixture.probe_paths["adherence"]["output_manifest"],
    ):
        assert all("  ./" in line for line in manifest.read_text().splitlines())


def test_formal_g1_false_is_a_hard_block(tmp_path: Path) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    payload = json.loads(g1.read_text(encoding="utf-8"))
    payload.update(status="ENGINEERING_COMPATIBILITY_ONLY", formal_g1=False)
    g1.write_text(canonical_json(payload), encoding="utf-8")

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "FORMAL_G1_RECEIPT_V1"),
        ("schema_version", "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4"),
        ("environment_contract_revision", "ENGINEERING_CANDIDATE_V4"),
        ("exit_code", 9),
        ("exit_code", False),
        ("failure_codes", ["BLOCKED_UNSAFE_FORMAL_RECEIPT"]),
        ("failure_stage", "native_kernel_smoke"),
        ("environment_contract_revision_required", True),
        ("compatibility_activation", "GLOBAL_SHELL_MUTATION"),
        ("official_contract", {"torch": "2.8.0+cu128"}),
    ],
)
def test_formal_g1_receipt_requires_the_supported_success_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    fixture, g1, _, _ = make_fixture(tmp_path)
    payload = json.loads(g1.read_text(encoding="utf-8"))
    payload[field] = value
    g1.write_text(canonical_json(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        release.verify_formal_g1(g1, fixture.environment_manifest_path)


def test_formal_g1_receipt_path_must_be_canonical_without_symlink_hops(
    tmp_path: Path,
) -> None:
    fixture, g1, _, _ = make_fixture(tmp_path)
    alias = tmp_path / "provenance-alias"
    alias.symlink_to(g1.parent, target_is_directory=True)
    aliased_receipt = alias / g1.name

    with pytest.raises(ValueError):
        release.verify_formal_g1(aliased_receipt, fixture.environment_manifest_path)


def test_formal_g1_receipt_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    fixture, g1, _, _ = make_fixture(tmp_path)
    original = g1.read_text(encoding="utf-8").lstrip()
    g1.write_text(
        '{"status":"G1_PASS",' + original[1:],
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        release.verify_formal_g1(g1, fixture.environment_manifest_path)


@pytest.mark.parametrize("marker_name", ["acceptance", "diverse", "adherence"])
def test_all_three_g2_markers_must_bind_the_same_formal_g1_and_environment(
    tmp_path: Path, marker_name: str
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    marker = (
        fixture.acceptance_success_path
        if marker_name == "acceptance"
        else fixture.probe_paths[marker_name]["success"]
    )
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["formal_g1_receipt_sha256"] = "0" * 64
    marker.write_text(canonical_json(payload), encoding="utf-8")

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


@pytest.mark.parametrize("json_name", ["platform", "g2_marker", "input_contract"])
def test_release_rejects_duplicate_keys_in_every_json_layer(
    tmp_path: Path, json_name: str
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    if json_name == "platform":
        path = fixture.platform_path
        duplicate = '"schema_version":"AIV1_PLATFORM_EVIDENCE_V1",'
    elif json_name == "g2_marker":
        path = fixture.acceptance_success_path
        duplicate = '"status":"SUCCESS",'
    else:
        path = fixture.contract_path
        duplicate = '"schema_version":"AIV1_INPUT_CONTRACT_V1",'
    original = path.read_text(encoding="utf-8").lstrip()
    path.write_text("{" + duplicate + original[1:], encoding="utf-8")

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


@pytest.mark.parametrize("contract_name", ["state", "schema"])
def test_aiv1_semantic_and_schema_hashes_are_recomputed(
    tmp_path: Path, contract_name: str
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    path = (
        fixture.state_path
        if contract_name == "state"
        else fixture.registry_schema_path
    )
    path.write_text(path.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


def test_formal_g1_receipt_sha256_is_an_independent_cli_binding(tmp_path: Path) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    expected_sha = sha256(g1)
    payload = json.loads(g1.read_text(encoding="utf-8"))
    payload["unbound_mutation"] = True
    g1.write_text(canonical_json(payload), encoding="utf-8")

    result = run_python(
        "release_g2_anchors.py",
        "--workspace-root",
        fixture.workspace,
        "--repo-root",
        fixture.repo,
        "--acceptance-root",
        fixture.acceptance_root,
        "--g1-receipt",
        g1,
        "--g1-receipt-sha256",
        expected_sha,
        "--aiv0-final-receipt",
        fixture.aiv0_receipt_path,
        "--platform-evidence",
        fixture.platform_path,
        "--environment-manifest",
        fixture.environment_manifest_path,
        "--runtime-scripts-manifest",
        fixture.runtime_scripts_manifest_path,
        "--anchor-output",
        anchors,
        "--receipt-output",
        receipt,
    )
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


def test_publish_race_cannot_follow_replaced_parent_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    parent = workspace / "release"
    outside = tmp_path / "outside"
    displaced = workspace / "release.displaced"
    parent.mkdir(parents=True)
    outside.mkdir()
    temporary = workspace / ".temporary"
    temporary.write_bytes(b"immutable release\n")
    destination = parent / "receipt.json"
    real_link = os.link
    raced = False

    def replace_parent_then_link(source, target, *args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            parent.rename(displaced)
            parent.symlink_to(outside, target_is_directory=True)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(release.os, "link", replace_parent_then_link)
    with pytest.raises((OSError, ValueError)):
        release.publish_no_replace(temporary, destination, workspace)

    assert not (outside / destination.name).exists()
    assert not (displaced / destination.name).exists()


def mutate_bad_canonical_sequence(fixture: SyntheticAIV1Fixture) -> None:
    artifact = fixture.candidate_paths[0]
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace(
            "_entity_poly.pdbx_seq_one_letter_code_can ",
            "_entity_poly.pdbx_seq_one_letter_code_can G",
        ),
        encoding="utf-8",
    )
    fixture.rebind_g2(rewrite_acceptance_manifest=True)


def mutate_missing_refold_cif(fixture: SyntheticAIV1Fixture) -> None:
    fixture.candidate_paths[0].unlink()
    fixture.rebind_g2(rewrite_acceptance_manifest=True)


def mutate_missing_fold_npz(fixture: SyntheticAIV1Fixture) -> None:
    fold = next(
        (
            fixture.acceptance_directory
            / "intermediate_designs_inverse_folded/fold_out_npz"
        ).glob("*.npz")
    )
    fold.unlink()
    fixture.rebind_g2(rewrite_acceptance_manifest=True)


def mutate_bad_checkpoint_sha(fixture: SyntheticAIV1Fixture) -> None:
    success = fixture.probe_paths["diverse"]["success"]
    payload = json.loads(success.read_text(encoding="utf-8"))
    payload["checkpoint_sha256"] = "0" * 64
    success.write_text(canonical_json(payload), encoding="utf-8")
    fixture.rebind_g2()


def mutate_array_compute_capability(fixture: SyntheticAIV1Fixture) -> None:
    payload = json.loads(fixture.platform_path.read_text(encoding="utf-8"))
    payload["gpu_compute_capability"] = [8, 0]
    fixture.platform_path.write_text(canonical_json(payload), encoding="utf-8")


def mutate_noncanonical_output_manifest(fixture: SyntheticAIV1Fixture) -> None:
    lines = fixture.output_manifest_path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("  ./", "  ", 1)
    fixture.output_manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fixture.rebind_g2()


@pytest.mark.parametrize(
    "mutator",
    [
        mutate_bad_canonical_sequence,
        mutate_missing_refold_cif,
        mutate_missing_fold_npz,
        mutate_bad_checkpoint_sha,
        mutate_array_compute_capability,
        mutate_noncanonical_output_manifest,
    ],
    ids=[
        "canonical-sequence",
        "refold-cif-path-and-exact-ten",
        "fold-npz-exact-ten-and-five-samples",
        "checkpoint-sha256",
        "compute-capability-must-be-string",
        "manifest-paths-require-dot-slash",
    ],
)
def test_real_aiv1_contract_drift_is_rejected(tmp_path: Path, mutator) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    mutator(fixture)

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


def test_aiv0_receipt_must_match_the_canonical_summary(tmp_path: Path) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    payload = json.loads(fixture.aiv0_receipt_path.read_text(encoding="utf-8"))
    payload["unexpected_drift"] = True
    fixture.aiv0_receipt_path.write_text(canonical_json(payload), encoding="utf-8")

    result = invoke(fixture, g1, anchors, receipt)
    assert result.returncode != 0
    assert not anchors.exists() and not receipt.exists()


def test_lowercase_production_receipt_name_is_rejected(tmp_path: Path) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    lowercase = receipt.with_name("g2_anchor_release_receipt.json")

    result = invoke(fixture, g1, anchors, lowercase)
    assert result.returncode != 0
    assert not anchors.exists() and not lowercase.exists()


def test_complete_valid_release_is_a_read_only_replay(tmp_path: Path) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    first = invoke(fixture, g1, anchors, receipt)
    assert first.returncode == 0, first.stderr
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_mode, sha256(path))
        for path in (anchors, receipt)
    }
    second = invoke(fixture, g1, anchors, receipt)
    assert second.returncode == 0, second.stderr
    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_mode, sha256(path))
        for path in (anchors, receipt)
    }
    assert before == after
    assert_strict_aiv1_release(fixture, anchors, receipt)


def test_existing_release_rejects_a_different_formal_g1_receipt_body(
    tmp_path: Path,
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    first = invoke(fixture, g1, anchors, receipt)
    assert first.returncode == 0, first.stderr
    before = {path: sha256(path) for path in (anchors, receipt)}
    payload = json.loads(g1.read_text(encoding="utf-8"))
    payload["completed_at_utc"] = "2026-08-30T00:00:00Z"
    g1.write_text(canonical_json(payload), encoding="utf-8")

    replay = invoke(fixture, g1, anchors, receipt)
    assert replay.returncode != 0
    assert {path: sha256(path) for path in (anchors, receipt)} == before


def test_partial_existing_anchor_replacement_after_validation_blocks_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    first = invoke(fixture, g1, anchors, receipt)
    assert first.returncode == 0, first.stderr
    receipt.unlink()
    tampered_bytes = b"externally replaced anchor\n"
    real_validate = release.validate_release_with_frozen_aiv1
    replaced = False

    def validate_then_replace(**kwargs) -> None:
        nonlocal replaced
        real_validate(**kwargs)
        if not replaced:
            replaced = True
            replacement = fixture.workspace / ".external-anchor-replacement"
            replacement.write_bytes(tampered_bytes)
            os.replace(replacement, anchors)

    monkeypatch.setattr(
        release, "validate_release_with_frozen_aiv1", validate_then_replace
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_g2_anchors.py",
            *map(str, release_arguments(fixture, g1, anchors, receipt)),
        ],
    )

    with pytest.raises((OSError, ValueError)):
        release.main()
    assert anchors.read_bytes() == tampered_bytes
    assert not receipt.exists()


def test_new_anchor_replacement_after_final_validation_cleans_only_own_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    tampered_bytes = b"external anchor replacement after final validation\n"
    real_validate = release.validate_release_with_frozen_aiv1
    validations = 0

    def replace_anchor_after_final_validation(**kwargs) -> None:
        nonlocal validations
        real_validate(**kwargs)
        validations += 1
        if validations == 2:
            replacement = fixture.workspace / ".external-final-anchor"
            replacement.write_bytes(tampered_bytes)
            os.replace(replacement, anchors)

    monkeypatch.setattr(
        release,
        "validate_release_with_frozen_aiv1",
        replace_anchor_after_final_validation,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_g2_anchors.py",
            *map(str, release_arguments(fixture, g1, anchors, receipt)),
        ],
    )

    with pytest.raises((OSError, ValueError)):
        release.main()
    assert anchors.read_bytes() == tampered_bytes
    assert not receipt.exists()
    assert "G2_ANCHOR_RELEASE_PASS" not in capsys.readouterr().out


def test_external_receipt_replacement_is_never_deleted_on_final_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    tampered_bytes = b"external receipt replacement\n"
    real_validate = release.validate_release_with_frozen_aiv1
    validations = 0

    def replace_receipt_after_final_validation(**kwargs) -> None:
        nonlocal validations
        real_validate(**kwargs)
        validations += 1
        if validations == 2:
            replacement = fixture.workspace / ".external-final-receipt"
            replacement.write_bytes(tampered_bytes)
            os.replace(replacement, receipt)

    monkeypatch.setattr(
        release,
        "validate_release_with_frozen_aiv1",
        replace_receipt_after_final_validation,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_g2_anchors.py",
            *map(str, release_arguments(fixture, g1, anchors, receipt)),
        ],
    )

    with pytest.raises((OSError, ValueError)):
        release.main()
    assert not anchors.exists()
    assert receipt.read_bytes() == tampered_bytes
    assert "G2_ANCHOR_RELEASE_PASS" not in capsys.readouterr().out


def test_identical_partial_anchor_can_recover_receipt_without_rewriting_anchor(
    tmp_path: Path,
) -> None:
    fixture, g1, anchors, receipt = make_fixture(tmp_path)
    first = invoke(fixture, g1, anchors, receipt)
    assert first.returncode == 0, first.stderr
    anchor_before = (anchors.stat().st_mtime_ns, sha256(anchors))
    receipt.unlink()
    recovered = invoke(fixture, g1, anchors, receipt)
    assert recovered.returncode == 0, recovered.stderr
    assert (anchors.stat().st_mtime_ns, sha256(anchors)) == anchor_before
    assert_strict_aiv1_release(fixture, anchors, receipt)
