import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest
import yaml


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "run_owner_only_inverse_fold.sh"
VALIDATOR = ROOT / "scripts" / "validate_owner_only_inverse_fold.py"
RUNTIME_PYTHON = Path(
    "/home/lin/creator/gpu_work/environments/cu128_blackwell_candidate/attempt_004/env/bin/python"
)
SEALED_POSE = Path(
    "/home/lin/creator/gpu_work/owner_mode/t10_pose_anchored_spec/"
    "7xl0_design_3_high_contact/attempt_20260831T183556Z"
)
SPEC_PATH = SEALED_POSE / "spec_bundle/design.yaml"


def _load_validator():
    spec = importlib.util.spec_from_file_location("owner_only_inverse_validator", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rebuild_spec_manifest(root: Path) -> str:
    manifest = root / "SPEC_BUNDLE.SHA256SUMS"
    names = ("design.yaml", "target.cif", "scaffold.cif", "scaffold.yaml")
    manifest.write_text(
        "".join(
            f"{_sha256(root / 'spec_bundle' / name)}  ./spec_bundle/{name}\n"
            for name in names
        ),
        encoding="utf-8",
    )
    return _sha256(manifest)


def _rebuild_top_manifest(root: Path) -> None:
    manifest = root / "SHA256SUMS"
    members = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path != manifest
        ),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    manifest.write_text(
        "".join(
            f"{_sha256(path)}  ./{path.relative_to(root).as_posix()}\n"
            for path in members
        ),
        encoding="utf-8",
    )


def _bind_receipt_to_current_spec_manifest(root: Path) -> None:
    spec_manifest_sha = _rebuild_spec_manifest(root)
    receipt_path = root / "POSE_ANCHORED_SPEC.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["publication_bindings"]["spec_bundle_manifest_sha256"] = spec_manifest_sha
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _rebuild_top_manifest(root)


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _make_resolved_only_inverse_config(
    tmp_path: Path, count: int = 6
) -> tuple[Path, Path, Path]:
    private_root = tmp_path / ".only_ifold_private.attempt_test.abcdef"
    spec = private_root / "pose/spec_bundle/design.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("entities: []\n", encoding="utf-8")
    runtime = private_root / "runtime"
    runtime.mkdir()
    for name in (
        "boltzgen1_ifold.ckpt",
        "boltz2_conf_final.ckpt",
        "boltzgen1_adherence.ckpt",
        "mols.zip",
    ):
        (runtime / name).write_bytes(name.encode("ascii"))

    root = tmp_path / "run"
    config = root / "config"
    config.mkdir(parents=True)
    design_dir = root / "intermediate_designs"
    _write_yaml(
        root / "steps.yaml",
        {
            "steps": [
                {"name": "inverse_folding"},
                {"name": "folding"},
                {"name": "analysis"},
                {"name": "filtering"},
            ]
        },
    )
    _write_yaml(
        config / "inverse_folding.yaml",
        {
            "name": "inverse_fold_only",
            "data": {
                "cfg": {
                    "yaml_path": [str(spec)],
                    "multiplicity": count,
                    "skip_existing": False,
                    "moldir": str(runtime / "mols.zip"),
                }
            },
            "diffusion_samples": 1,
            "output": str(design_dir),
            "checkpoint": str(runtime / "boltzgen1_ifold.ckpt"),
        },
    )
    _write_yaml(
        config / "folding.yaml",
        {
            "data": {
                "cfg": {"moldir": str(runtime / "mols.zip")},
                "design_dir": str(design_dir),
                "skip_existing": False,
            },
            "diffusion_samples": 5,
            "checkpoint": str(runtime / "boltz2_conf_final.ckpt"),
        },
    )
    _write_yaml(config / "analysis.yaml", {"design_dir": str(design_dir)})
    _write_yaml(
        config / "filtering.yaml",
        {"design_dir": str(design_dir), "budget": count},
    )
    return root, spec, runtime


def test_runner_syntax_help_and_only_inverse_topology() -> None:
    subprocess.run(["bash", "-n", str(RUNNER)], check=True)
    help_result = subprocess.run(
        ["bash", str(RUNNER), "--help"], check=True, capture_output=True, text=True
    )
    assert "SEALED_SPEC NUM_SEQUENCES" in help_result.stdout
    text = RUNNER.read_text(encoding="utf-8")
    assert "--only_inverse_fold" in text
    assert '--inverse_fold_num_sequences "$num_sequences"' in text
    assert "diffusion_samples=5" in text
    assert "--reuse" not in text
    assert "validate_owner_only_inverse_fold.py" in text
    assert "design_diffusion_performed\":False" in text
    assert 'exec 9<"/run/user/$(id -u)"' in text
    assert text.index("flock -n 9") < text.index("gpu_compute_processes_before.csv")
    execution = text[text.index("run_logged inverse_folding") :]
    assert "for stage in design " not in execution
    assert execution.index("validate-inverse") < execution.index("for stage in folding analysis filtering")
    assert "unique CDR sequences={unique} < 4" in VALIDATOR.read_text(encoding="utf-8")
    assert execution.index("spec_preflight_terminal.json") < execution.index(
        'value["status"]="ONLY_INVERSE_FOLD_COMPLETE"'
    )
    assert "info.st_nlink != 1" in text
    assert "semantic payload changed after validation" in text
    assert "code_bindings.SHA256SUMS" in text
    assert text.rindex("code_bindings.SHA256SUMS") < text.index(
        'value["status"]="ONLY_INVERSE_FOLD_COMPLETE"'
    )


def test_forged_resume_paths_are_rejected_before_trap_or_token_consumption() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    resume = text[
        text.index('if [ "$private_resume" -eq 1 ]') : text.index("else\n  attempt_stamp")
    ]
    assert resume.index("unsafe resumed staging transaction") < resume.index(
        "trap emergency_finalize EXIT"
    ) < resume.index('"$private_root/resume.token"')

    workspace = Path(tempfile.mkdtemp(prefix="only-ifold-resume-negative-", dir="/home/lin"))
    try:
        (workspace / "GLP_/.git").mkdir(parents=True)
        owner = workspace / "gpu_work/owner_mode"
        run_id = "resume-path-negative"
        (owner / "t11_only_inverse_fold_from_pose_spec" / run_id).mkdir(parents=True)
        (workspace / "WINDOWS_OWNER_MODE.json").write_text("{}\n", encoding="utf-8")
        attempt_id = "attempt_20260901T000000Z"
        victim = workspace / "untrusted"
        staging = victim / "staging"
        staging.mkdir(parents=True)
        operator_logs = staging / "operator_logs"
        operator_logs.mkdir()
        sentinel = staging / "sentinel.txt"
        sentinel.write_text("UNCHANGED\n", encoding="utf-8")
        private = owner / f".only_ifold_private.{attempt_id}.ABC123"
        private.mkdir()
        token = os.urandom(64)
        token_path = private / "resume.token"
        token_path.write_bytes(token)
        token_path.chmod(0o600)
        attempt = victim / attempt_id
        env = os.environ.copy()
        env.update(
            {
                "OWNER_ONLY_IFOLD_PRIVATE_RESUME": "1",
                "OWNER_ONLY_IFOLD_STAGING_ROOT": str(staging),
                "OWNER_ONLY_IFOLD_PRIVATE_ROOT": str(private),
                "OWNER_ONLY_IFOLD_ATTEMPT_ROOT": str(attempt),
                "OWNER_ONLY_IFOLD_ATTEMPT_ID": attempt_id,
                "OWNER_ONLY_IFOLD_RESUME_TOKEN_SHA256": hashlib.sha256(token).hexdigest(),
            }
        )
        result = subprocess.run(
            ["bash", str(RUNNER), str(workspace), run_id, "/tmp/nonexistent/design.yaml", "6"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert "unsafe resumed staging transaction" in result.stderr
        assert sentinel.read_text(encoding="utf-8") == "UNCHANGED\n"
        assert token_path.read_bytes() == token
        assert not attempt.exists()
        assert list(operator_logs.iterdir()) == []
    finally:
        shutil.rmtree(workspace)


@pytest.mark.skipif(not RUNTIME_PYTHON.is_file() or not SPEC_PATH.is_file(), reason="owner runtime absent")
def test_real_sealed_pose_preflight_cross_binds_manifests_and_receipt() -> None:
    result = subprocess.run(
        [str(RUNTIME_PYTHON), "-I", str(VALIDATOR), "preflight-spec", str(SPEC_PATH)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "POSE_INPUT_PASS"
    assert payload["spec_path"] == str(SPEC_PATH)
    assert payload["design_indices_1based"] == [
        *range(26, 34), *range(51, 58), *range(96, 111)
    ]
    assert payload["disulfide"]["label_seq_positions"] == [22, 95]
    assert len(payload["target_sequence"]) == 30
    assert len(payload["vhh_sequence"]) == 121
    assert payload["runner_input"] == "spec_bundle/design.yaml"
    assert (
        payload["receipt_publication_bindings"]["spec_bundle_manifest_sha256"]
        == payload["spec_manifest_sha256"]
    )


@pytest.mark.skipif(not SPEC_PATH.is_file(), reason="sealed pose absent")
@pytest.mark.parametrize(
    "binding_key",
    [
        "spec_bundle_manifest_sha256",
        "copied_target_sha256",
        "source_target_sha256",
        "spec_manifest_target_sha256",
    ],
)
def test_preflight_rejects_tampered_receipt_publication_binding(
    tmp_path: Path, binding_key: str
) -> None:
    module = _load_validator()
    root = tmp_path / binding_key
    shutil.copytree(SEALED_POSE, root)
    receipt_path = root / "POSE_ANCHORED_SPEC.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["publication_bindings"][binding_key] = "0" * 64
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _rebuild_top_manifest(root)
    with pytest.raises(ValueError, match="cross-bind"):
        module.preflight_spec(str(root / "spec_bundle/design.yaml"))


@pytest.mark.skipif(not SPEC_PATH.is_file(), reason="sealed pose absent")
def test_preflight_rejects_design_yaml_extra_key_after_valid_reseal(
    tmp_path: Path,
) -> None:
    module = _load_validator()
    root = tmp_path / "design-extra"
    shutil.copytree(SEALED_POSE, root)
    path = root / "spec_bundle/design.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["unexpected"] = True
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    _bind_receipt_to_current_spec_manifest(root)
    with pytest.raises(ValueError, match="design.yaml fixed-document semantics drift"):
        module.preflight_spec(str(path))


@pytest.mark.skipif(not SPEC_PATH.is_file(), reason="sealed pose absent")
def test_preflight_rejects_scaffold_yaml_group_drift_after_valid_reseal(
    tmp_path: Path,
) -> None:
    module = _load_validator()
    root = tmp_path / "scaffold-drift"
    shutil.copytree(SEALED_POSE, root)
    path = root / "spec_bundle/scaffold.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["structure_groups"][1]["group"]["res_index"] = "26..33"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    _bind_receipt_to_current_spec_manifest(root)
    with pytest.raises(ValueError, match="scaffold.yaml fixed-document semantics drift"):
        module.preflight_spec(str(root / "spec_bundle/design.yaml"))


@pytest.mark.skipif(not SPEC_PATH.is_file(), reason="sealed pose absent")
def test_preflight_rejects_hardlink_and_symlink(tmp_path: Path) -> None:
    module = _load_validator()
    hard_root = tmp_path / "hard"
    shutil.copytree(SEALED_POSE, hard_root)
    design = hard_root / "spec_bundle/design.yaml"
    twin = hard_root / "design.twin"
    os.link(design, twin)
    with pytest.raises(ValueError, match="hard-linked"):
        module.preflight_spec(str(design))

    link_root = tmp_path / "link"
    shutil.copytree(SEALED_POSE, link_root)
    target = link_root / "spec_bundle/target.cif"
    saved = link_root / "target.saved"
    target.rename(saved)
    target.symlink_to(saved)
    with pytest.raises(ValueError, match="symlink"):
        module.preflight_spec(str(link_root / "spec_bundle/design.yaml"))


@pytest.mark.skipif(not SPEC_PATH.is_file(), reason="sealed pose absent")
def test_candidate_backbone_gate_detects_coordinate_drift() -> None:
    module = _load_validator()
    _, source = module._source_contract(SPEC_PATH)
    candidate = module.secure_bound(SEALED_POSE / "boltzgen_check/output/design.cif")
    target, vhh = module._candidate_structure(candidate, source, require_backbone=True)
    assert target == source["target_sequence"]
    assert vhh == source["vhh_sequence"]

    changed = dict(source)
    changed["vhh_backbone"] = dict(source["vhh_backbone"])
    changed["vhh_backbone"][(1, "CA")] = changed["vhh_backbone"][(1, "CA")] + 0.01
    with pytest.raises(ValueError, match="backbone coordinates changed"):
        module._candidate_structure(candidate, changed, require_backbone=True)


def test_sequence_count_contract_is_six_through_ten() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    for value in ("5", "11"):
        result = subprocess.run(
            ["bash", str(RUNNER), "/tmp", "safe", "/tmp/spec", value],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 64
        assert "6..10" in result.stderr
    assert "candidate_ids\":validation[\"candidate_ids\"]" in text
    assert '"fold_sample_count":validation["observed_fold_sample_count"]' in text


def test_validate_config_binds_private_runtime_asset_roles(tmp_path: Path) -> None:
    module = _load_validator()
    root, spec, runtime = _make_resolved_only_inverse_config(tmp_path)
    evidence = module._validate_config(root, spec, 6, 5)
    assert evidence["private.runtime"] == str(runtime)
    assert evidence["inverse.checkpoint"] == str(runtime / "boltzgen1_ifold.ckpt")
    assert evidence["folding.checkpoint"] == str(runtime / "boltz2_conf_final.ckpt")
    assert evidence["inverse.moldir"] == evidence["folding.moldir"] == str(
        runtime / "mols.zip"
    )


def test_validate_config_rejects_inverse_and_folding_checkpoint_role_swap(
    tmp_path: Path,
) -> None:
    module = _load_validator()
    root, spec, _ = _make_resolved_only_inverse_config(tmp_path)
    inverse_path = root / "config/inverse_folding.yaml"
    folding_path = root / "config/folding.yaml"
    inverse = yaml.safe_load(inverse_path.read_text(encoding="utf-8"))
    folding = yaml.safe_load(folding_path.read_text(encoding="utf-8"))
    inverse["checkpoint"], folding["checkpoint"] = (
        folding["checkpoint"],
        inverse["checkpoint"],
    )
    _write_yaml(inverse_path, inverse)
    _write_yaml(folding_path, folding)
    with pytest.raises(ValueError, match="role/path mismatch"):
        module._validate_config(root, spec, 6, 5)


def test_validate_config_rejects_external_same_basename_checkpoint(
    tmp_path: Path,
) -> None:
    module = _load_validator()
    root, spec, _ = _make_resolved_only_inverse_config(tmp_path)
    external = tmp_path / "external/boltzgen1_ifold.ckpt"
    external.parent.mkdir()
    external.write_bytes(b"external")
    inverse_path = root / "config/inverse_folding.yaml"
    inverse = yaml.safe_load(inverse_path.read_text(encoding="utf-8"))
    inverse["checkpoint"] = str(external)
    _write_yaml(inverse_path, inverse)
    with pytest.raises(ValueError, match="role/path mismatch"):
        module._validate_config(root, spec, 6, 5)


def test_validate_config_rejects_design_checkpoint_reference(
    tmp_path: Path,
) -> None:
    module = _load_validator()
    root, spec, runtime = _make_resolved_only_inverse_config(tmp_path)
    analysis_path = root / "config/analysis.yaml"
    analysis = yaml.safe_load(analysis_path.read_text(encoding="utf-8"))
    analysis["design_checkpoint"] = str(runtime / "boltzgen1_adherence.ckpt")
    _write_yaml(analysis_path, analysis)
    with pytest.raises(ValueError, match="design checkpoints are forbidden"):
        module._validate_config(root, spec, 6, 5)
