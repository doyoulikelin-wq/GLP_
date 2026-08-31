from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import ctypes
import select
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import implementation, sha256


ASSET_BINDINGS = (
    ("spec_path", "spec_sha256", "design.yaml"),
    ("design_checkpoint", "design_checkpoint_sha256", "design.ckpt"),
    ("inverse_fold_checkpoint", "inverse_fold_checkpoint_sha256", "inverse.ckpt"),
    ("folding_checkpoint", "folding_checkpoint_sha256", "fold.ckpt"),
    ("mols_path", "mols_sha256", "mols.zip"),
    ("model_inputs_manifest_path", "model_inputs_manifest_sha256", "model_inputs_SHA256SUMS"),
    ("runtime_scripts_manifest_path", "runtime_scripts_manifest_sha256", "gpu_runtime_scripts_SHA256SUMS"),
    ("spec_gate_bundle_path", "spec_gate_bundle_sha256", "spec_gate_bundle.tar"),
    ("environment_receipt", "environment_receipt_sha256", "environment.receipt.json"),
    (
        "environment_provenance_manifest_path",
        "environment_provenance_manifest_sha256",
        "environment_provenance_SHA256SUMS",
    ),
)

RUNTIME_MEMBERS = tuple(
    sorted(
        (
            "run_local_cell.sh",
            "software/finalize_local_attempt.py",
            "software/validate_cell_output.py",
            "status_local_cell.sh",
            "submit_local_once.sh",
            "verify_gpu_env_stage.sh",
        ),
        key=lambda value: value.encode("utf-8"),
    )
)


def executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def snapshot_tree(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, sha256(path))
        for path in root.rglob("*")
        if path.is_file()
    }


def make_contract(root: Path, bg_work: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    for relative in RUNTIME_MEMBERS:
        target = bg_work / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = implementation(Path(relative).name)
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)
    contents = {
        "design.yaml": b"entities: []\n",
        "design.ckpt": b"frozen design checkpoint\n",
        "inverse.ckpt": b"frozen inverse-fold checkpoint\n",
        "fold.ckpt": b"frozen folding checkpoint\n",
        "mols.zip": b"frozen mols archive\n",
        "model_inputs_SHA256SUMS": b"model input manifest\n",
        "spec_gate_bundle.tar": b"frozen spec gate bundle\n",
        "environment_provenance_SHA256SUMS": b"environment provenance manifest\n",
    }
    paths: dict[str, Path] = {}
    for name, content in contents.items():
        path = assets / name
        path.write_bytes(content)
        paths[name] = path
    environment_attempt = root / "environment_attempt"
    env_bin = environment_attempt / "env" / "bin"
    env_bin.mkdir(parents=True, exist_ok=True)
    environment_python = env_bin / "python"
    if not environment_python.exists():
        environment_python.symlink_to(sys.executable)
    launcher = env_bin / "boltzgen-wsl-sm120"
    if not launcher.exists():
        executable(launcher, "#!/usr/bin/env bash\nexit 0\n")
    receipt = environment_attempt / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4",
                "status": "ENGINEERING_COMPATIBILITY_ONLY",
                "formal_g1": False,
                "environment_contract_revision_required": True,
                "compatibility_activation": "EXPLICIT_PROCESS_LOCAL_ONLY",
                "exit_code": 0,
                "attempt_id": "attempt_004",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    paths["environment.receipt.json"] = receipt
    runtime_manifest = bg_work / "gpu_runtime_scripts_SHA256SUMS"
    runtime_manifest.write_text(
        "".join(f"{sha256(bg_work / relative)}  ./{relative}\n" for relative in RUNTIME_MEMBERS),
        encoding="utf-8",
    )
    paths[runtime_manifest.name] = runtime_manifest
    contract_root = bg_work / "contract"
    contract_root.mkdir(exist_ok=True)
    (contract_root / "environment_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "WSL2_GPU_STAGE_ENVIRONMENT_CONTRACT_V1",
                "contract_id": "fixture-engineering-environment",
                "stage_class": "ENGINEERING",
                "executor_uid": os.getuid(),
                "environment_attempt_root": str(environment_attempt.resolve()),
                "environment_receipt_path": str(receipt.resolve()),
                "environment_receipt_sha256": sha256(receipt),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    payload: dict[str, object] = {
        "schema_version": "WSL2_BOLTZGEN_LOCAL_CELL_V1",
        "cell_id": "engineering_smoke_7xl0",
        "attempt_id": "attempt_001",
        "run_kind": "ENGINEERING_SMOKE",
        "success_status": "ENGINEERING_SMOKE_PASS_NOT_G2",
        "stage_class": "ENGINEERING",
        "expected_designs": 1,
        "expected_fold_samples": 5,
        "budget": 1,
        "diffusion_batch_size": 1,
        "inverse_fold_num_sequences": 1,
        "devices": 1,
        "num_workers": 1,
        "use_kernels": "auto",
        "protocol": "nanobody-anything",
        "analysis_modality": "antibody",
        "filtering_modality": "antibody",
        "filter_bindingsite": True,
    }
    for path_field, hash_field, name in ASSET_BINDINGS:
        payload[path_field] = str(paths[name].resolve())
        payload[hash_field] = sha256(paths[name])
    contract = root / "cell_contract.json"
    contract.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return contract


def promote_contract_to_formal(root: Path, bg_work: Path, contract: Path) -> tuple[Path, Path]:
    environment_attempt = root / "environment_attempt"
    recursive = environment_attempt / "recursive_payload.SHA256SUMS"
    recursive.write_text(
        f"{sha256(environment_attempt / 'receipt.json')}  ./receipt.before-formal.json\n",
        encoding="utf-8",
    )
    receipt = environment_attempt / "receipt.json"
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_payload.update(
        {
            "schema_version": "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1",
            "status": "G1_PASS",
            "formal_g1": True,
            "environment_contract_revision": (
                "WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V1"
            ),
            "environment_contract_revision_required": False,
            "failure_codes": [],
            "failure_stage": None,
            "environment_manifest_sha256": sha256(recursive),
            "recursive_payload_manifest_sha256": sha256(recursive),
            "official_contract": {
                "boltzgen": "0.3.2",
                "cuequivariance": "0.6.1",
                "torch": "2.8.0+cu128",
                "torch_cuda": "12.8",
                "triton": "3.4.0",
            },
        }
    )
    receipt.write_text(
        json.dumps(receipt_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    environment_contract = bg_work / "contract/environment_contract.json"
    environment_payload = json.loads(environment_contract.read_text(encoding="utf-8"))
    environment_payload.update(
        {
            "contract_id": "fixture-formal-environment-v1",
            "stage_class": "FORMAL",
            "environment_receipt_sha256": sha256(receipt),
            "artifact_bindings": {
                "recursive_payload_manifest": {
                    "path": str(recursive.resolve()),
                    "sha256": sha256(recursive),
                }
            },
        }
    )
    environment_contract.write_text(
        json.dumps(environment_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    cell_payload = json.loads(contract.read_text(encoding="utf-8"))
    cell_payload.update(
        {
            "stage_class": "FORMAL",
            "run_kind": "G2_ACCEPTANCE",
            "success_status": "G2_ACCEPTANCE_PASS",
            "expected_designs": 10,
            "budget": 10,
            "environment_receipt_sha256": sha256(receipt),
            "environment_provenance_manifest_path": str(recursive.resolve()),
            "environment_provenance_manifest_sha256": sha256(recursive),
        }
    )
    contract.write_text(
        json.dumps(cell_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return receipt, environment_contract


def fake_systemd(root: Path) -> tuple[Path, dict[str, str]]:
    fake_bin = root / "fake_systemd_bin"
    log = root / "systemd-run.log"
    state = root / "systemd.state"
    exec_state = root / "systemd.exec.json"
    executable(
        fake_bin / "systemd-run",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\t' "$@" >> "$FAKE_SYSTEMD_LOG"
printf '\\n' >> "$FAKE_SYSTEMD_LOG"
unit=''
description=''
exec_args=()
after_separator=0
while [ "$#" -gt 0 ]; do
  if [ "$after_separator" -eq 1 ]; then exec_args+=("$1"); shift; continue; fi
  case "$1" in
    --unit=*) unit=${1#--unit=} ;;
    --unit) shift; unit=${1:-} ;;
    --description=*) description=${1#--description=} ;;
    --description) shift; description=${1:-} ;;
    --) after_separator=1 ;;
  esac
  shift || true
done
[ -n "$unit" ] && [ -n "$description" ] || exit 64
case "${FAKE_SYSTEMD_RUN_MODE:-normal}" in
  crash_before_unit) exit 70 ;;
esac
printf 'Id=%s\\nLoadState=loaded\\nActiveState=active\\nSubState=running\\nResult=success\\nDescription=%s\\nRestart=no\\nType=exec\\nInvocationID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\nKillMode=control-group\\nUMask=0077\\n' "$unit" "$description" > "$FAKE_SYSTEMD_STATE"
python3 -I -S - "$FAKE_SYSTEMD_EXEC_STATE" "${exec_args[@]}" <<'PY'
import json, sys
path, *argv = sys.argv[1:]
with open(path, 'w', encoding='utf-8') as handle:
    json.dump({'type':'a(sasbttttuii)','data':[[argv[0],argv,False,0,0,0,0,123,0,0]]}, handle, separators=(',', ':'))
PY
if [ -n "${FAKE_SYSTEMD_SLEEP:-}" ]; then sleep "$FAKE_SYSTEMD_SLEEP"; fi
case "${FAKE_SYSTEMD_RUN_MODE:-normal}" in
  crash_after_unit) exit 71 ;;
esac
exit 0
""",
    )
    executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
mode=${FAKE_SYSTEMCTL_MODE:-normal}
if [[ " $* " == *' --json=short '* ]]; then
  [ -f "$FAKE_SYSTEMD_EXEC_STATE" ] || exit 4
  if [ "$mode" = exec_tamper ]; then
    python3 -I -S - "$FAKE_SYSTEMD_EXEC_STATE" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding='utf-8'))
value['ExecStart'][0]['argv'].append('--injected')
print(json.dumps(value, separators=(',', ':')))
PY
    exit 0
  fi
  cat "$FAKE_SYSTEMD_EXEC_STATE"
  exit 0
fi
case "$mode" in
  disappeared) exit 4 ;;
  ambiguous)
    [ -f "$FAKE_SYSTEMD_STATE" ] || exit 4
    cat "$FAKE_SYSTEMD_STATE"
    sed -n 's/^Id=/Id=/p' "$FAKE_SYSTEMD_STATE"
    exit 0
    ;;
  inactive)
    [ -f "$FAKE_SYSTEMD_STATE" ] || exit 4
    sed -e 's/^ActiveState=.*/ActiveState=inactive/' -e 's/^SubState=.*/SubState=dead/' "$FAKE_SYSTEMD_STATE"
    exit 0
    ;;
  bad_properties)
    [ -f "$FAKE_SYSTEMD_STATE" ] || exit 4
    sed 's/^KillMode=.*/KillMode=process/' "$FAKE_SYSTEMD_STATE"
    exit 0
    ;;
  normal)
    [ -f "$FAKE_SYSTEMD_STATE" ] || exit 4
    cat "$FAKE_SYSTEMD_STATE"
    exit 0
    ;;
  *) exit 64 ;;
esac
""",
    )
    executable(
        fake_bin / "busctl",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *' GetUnit '* ]]; then
  printf 'o "/org/freedesktop/systemd1/unit/fake"\n'
  exit 0
fi
[ -f "$FAKE_SYSTEMD_EXEC_STATE" ] || exit 4
if [ "${FAKE_SYSTEMCTL_MODE:-normal}" = exec_tamper ]; then
  python3 -I -S - "$FAKE_SYSTEMD_EXEC_STATE" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding='utf-8'))
value['data'][0][1].append('--injected')
print(json.dumps(value, separators=(',', ':')))
PY
  exit 0
fi
cat "$FAKE_SYSTEMD_EXEC_STATE"
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_SYSTEMD_LOG"] = str(log)
    env["FAKE_SYSTEMD_STATE"] = str(state)
    env["FAKE_SYSTEMD_EXEC_STATE"] = str(exec_state)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONOPTIMIZE",
        "CUDA_VISIBLE_DEVICES",
        "EXPECTED_DESIGNS",
        "EXPECTED_FOLD_SAMPLES",
    ):
        env.pop(key, None)
    return log, env


def submit(bg_work: Path, contract: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(implementation("submit_local_once.sh")), str(bg_work), str(contract)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=20,
    )


def submission_paths(bg_work: Path) -> tuple[Path, Path]:
    base = bg_work / "local_submissions" / "engineering_smoke_7xl0.attempt_001"
    return Path(f"{base}.intent.json"), Path(f"{base}.receipt.json")


@pytest.mark.parametrize("path_field,hash_field,name", ASSET_BINDINGS)
def test_submit_rejects_every_changed_immutable_asset_before_intent(
    tmp_path: Path, path_field: str, hash_field: str, name: str
) -> None:
    del hash_field, name
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    runner_env = install_runner_fakes(tmp_path, bg_work)
    contract = make_contract(tmp_path, bg_work)
    log, env = fake_systemd(tmp_path)
    bound = Path(json.loads(contract.read_text(encoding="utf-8"))[path_field])
    with bound.open("ab") as handle:
        handle.write(b"tampered\n")

    result = submit(bg_work, contract, env)

    assert result.returncode != 0
    intent, receipt = submission_paths(bg_work)
    assert not intent.exists()
    assert not receipt.exists()
    assert not log.exists()


def test_submit_is_idempotent_and_uses_nonrestarting_no_block_systemd(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    log, env = fake_systemd(tmp_path)

    first = submit(bg_work, contract, env)
    assert first.returncode == 0, first.stderr
    args = log.read_text(encoding="utf-8")
    assert "--user" in args
    assert "--no-block" in args
    assert "--property=Restart=no" in args
    assert "--unit=" in args
    assert str(bg_work / "run_local_cell.sh") in args
    intent_path, receipt_path = submission_paths(bg_work)
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert intent["status"] == "SUBMISSION_INTENT"
    assert receipt["status"] == "SUBMITTED"
    assert receipt["executor_kind"] == "WSL2_SYSTEMD_SINGLE_GPU"
    assert receipt["cell_contract_sha256"] == sha256(contract)
    assert receipt["unit"] == intent["unit"]
    assert receipt["submission_token"] == intent["submission_token"]
    assert receipt["invocation_id"] == "a" * 32

    before = log.read_bytes()
    second = submit(bg_work, contract, env)
    assert second.returncode == 0, second.stderr
    assert log.read_bytes() == before


def test_intent_written_then_systemd_crash_before_unit_is_never_relaunched(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    log, env = fake_systemd(tmp_path)
    env["FAKE_SYSTEMD_RUN_MODE"] = "crash_before_unit"

    first = submit(bg_work, contract, env)
    assert first.returncode != 0
    intent, receipt = submission_paths(bg_work)
    assert intent.is_file()
    assert not receipt.exists()
    before = log.read_bytes()

    env["FAKE_SYSTEMD_RUN_MODE"] = "normal"
    second = submit(bg_work, contract, env)
    assert second.returncode != 0
    assert log.read_bytes() == before
    assert not receipt.exists()


def test_systemd_transport_crash_after_unit_is_reconciled_to_receipt(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    _, env = fake_systemd(tmp_path)
    env["FAKE_SYSTEMD_RUN_MODE"] = "crash_after_unit"

    result = submit(bg_work, contract, env)

    assert result.returncode == 0, result.stderr
    _, receipt = submission_paths(bg_work)
    assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "SUBMITTED"


@pytest.mark.parametrize("mode", ("exec_tamper", "bad_properties"))
def test_submit_refuses_unit_with_changed_execstart_or_service_properties(
    tmp_path: Path, mode: str
) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    _, env = fake_systemd(tmp_path)
    env["FAKE_SYSTEMCTL_MODE"] = mode

    result = submit(bg_work, contract, env)

    assert result.returncode != 0
    intent, receipt = submission_paths(bg_work)
    assert intent.is_file()
    assert not receipt.exists()


def test_missing_receipt_status_is_read_only_and_submit_reconciles_without_relaunch(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    log, env = fake_systemd(tmp_path)
    assert submit(bg_work, contract, env).returncode == 0
    _, receipt = submission_paths(bg_work)
    receipt.unlink()
    before_status = snapshot_tree(bg_work)

    status = subprocess.run(
        ["bash", str(implementation("status_local_cell.sh")), str(bg_work), "engineering_smoke_7xl0", "attempt_001"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode != 0
    assert json.loads(status.stdout)["state"] == "BLOCKED_SUBMISSION_RECEIPT_MISSING"
    assert snapshot_tree(bg_work) == before_status

    before_log = log.read_bytes()
    recovered = submit(bg_work, contract, env)
    assert recovered.returncode == 0, recovered.stderr
    assert log.read_bytes() == before_log
    assert receipt.is_file()


@pytest.mark.parametrize(
    "systemctl_mode,expected_state",
    (("disappeared", "BLOCKED_UNIT_DISAPPEARED"), ("ambiguous", "BLOCKED_UNIT_AMBIGUOUS")),
)
def test_missing_receipt_and_untrustworthy_unit_fail_closed_without_relaunch(
    tmp_path: Path, systemctl_mode: str, expected_state: str
) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    log, env = fake_systemd(tmp_path)
    assert submit(bg_work, contract, env).returncode == 0
    _, receipt = submission_paths(bg_work)
    receipt.unlink()
    env["FAKE_SYSTEMCTL_MODE"] = systemctl_mode
    before_log = log.read_bytes()

    retry = submit(bg_work, contract, env)
    assert retry.returncode != 0
    assert log.read_bytes() == before_log
    assert not receipt.exists()
    before_status = snapshot_tree(bg_work)
    status = subprocess.run(
        ["bash", str(implementation("status_local_cell.sh")), str(bg_work), "engineering_smoke_7xl0", "attempt_001"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode != 0
    assert json.loads(status.stdout)["state"] == expected_state
    assert snapshot_tree(bg_work) == before_status


def test_concurrent_identical_submissions_launch_exactly_once(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    log, env = fake_systemd(tmp_path)
    env["FAKE_SYSTEMD_SLEEP"] = "0.3"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(bg_work, contract, env), range(2)))

    assert [result.returncode for result in results] == [0, 0], [result.stderr for result in results]
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def install_runner_fakes(root: Path, bg_work: Path) -> dict[str, str]:
    software = bg_work / "software"
    env_bin = root / "environment_attempt" / "env" / "bin"
    software.mkdir(parents=True, exist_ok=True)
    env_bin.mkdir(parents=True, exist_ok=True)
    if not (env_bin / "python").exists():
        (env_bin / "python").symlink_to(sys.executable)
    executable(
        bg_work / "verify_gpu_env_stage.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_VERIFY_LOG"
bg_work=$1
stage_id=$2
audit="$bg_work/stage_audits/$stage_id"
mkdir -p "$audit"
python3 -I -S - "$bg_work" "$stage_id" "$audit" <<'PY'
import hashlib, json, sys
from pathlib import Path
bg, stage_id, audit = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
environment_contract = json.loads((bg / 'contract/environment_contract.json').read_text())
receipt = Path(environment_contract['environment_receipt_path'])
root = Path(environment_contract['environment_attempt_root']) / 'env'
launcher = root / 'bin/boltzgen-wsl-sm120'
runtime = bg / 'gpu_runtime_scripts_SHA256SUMS'
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
binding = {
    'schema_version': 'WSL2_GPU_STAGE_CONTRACT_BINDING_V1',
    'stage_id': stage_id,
    'stage_class': environment_contract['stage_class'],
    'executor_uid': environment_contract['executor_uid'],
    'environment_root': str(root),
    'environment_python': str(root / 'bin/python'),
    'environment_launcher': str(launcher),
    'environment_launcher_sha256': digest(launcher),
    'receipt_path': str(receipt),
    'receipt_sha256': digest(receipt),
    'runtime_scripts_manifest_path': str(runtime),
    'runtime_scripts_manifest_sha256': digest(runtime),
}
(audit / 'contract_binding.json').write_text(json.dumps(binding, sort_keys=True) + '\\n')
members = sorted((p for p in audit.iterdir() if p.is_file()), key=lambda p: p.name.encode())
(audit / 'stage_environment.SHA256SUMS').write_text(
    ''.join(f'{digest(path)}  ./{path.name}\\n' for path in members)
)
PY
printf '%s\\n' "$audit"
""",
    )
    executable(
        env_bin / "boltzgen-wsl-sm120",
        """#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[1:]
with open(os.environ['FAKE_BOLTZGEN_LOG'], 'a', encoding='utf-8') as handle:
    handle.write('\\t'.join(arguments) + '\\n')

child_program = r'''\
import os
import sys
from pathlib import Path

action, root_text, stage = sys.argv[1:]
root = Path(root_text)
if action == 'configure':
    (root / 'config').mkdir(parents=True)
    (root / 'results').mkdir()
    (root / 'config/design.yaml').write_text(
        'data: {cfg: {multiplicity: 1}}\\ndiffusion_samples: 1\\n'
    )
    (root / 'config/inverse_folding.yaml').write_text(
        'data: {cfg: {multiplicity: 1}}\\n'
    )
    (root / 'config/folding.yaml').write_text('diffusion_samples: 5\\n')
    if os.environ.get('FAKE_CONFIG_OMIT_ANALYSIS', '0') != '1':
        (root / 'config/analysis.yaml').write_text(
            'liability_modality: antibody\\n'
        )
    (root / 'config/filtering.yaml').write_text('budget: 1\\n')
elif action == 'execute':
    config_name = {
        'design': 'design.yaml',
        'inverse_folding': 'inverse_folding.yaml',
        'folding': 'folding.yaml',
        'analysis': 'analysis.yaml',
        'filtering': 'filtering.yaml',
    }.get(stage)
    if config_name is None:
        raise SystemExit(64)
    config = root / 'config' / config_name
    if not config.read_text(encoding='utf-8'):
        raise SystemExit(65)
    if stage == os.environ.get('FAKE_FAIL_STAGE'):
        raise SystemExit(23)
    (root / 'results' / f'{stage}.done').write_text(
        f'{stage}:{config_name}\\n', encoding='utf-8'
    )
else:
    raise SystemExit(64)
'''

if not arguments:
    raise SystemExit(64)
if arguments[0] == 'configure':
    try:
        root = arguments[arguments.index('--output') + 1]
    except (ValueError, IndexError):
        raise SystemExit(64)
    stage = ''
elif arguments[0] == 'execute' and len(arguments) >= 2:
    root = arguments[1]
    if arguments.count('--no_subprocess') != 1:
        raise SystemExit(64)
    try:
        stage = arguments[arguments.index('--steps') + 1]
    except (ValueError, IndexError):
        raise SystemExit(64)
else:
    raise SystemExit(64)

child = subprocess.Popen(
    [sys.executable, '-I', '-S', '-c', child_program, arguments[0], root, stage],
    stdin=subprocess.DEVNULL,
    env=os.environ.copy(),
    close_fds=True,
)
raise SystemExit(child.wait())
""",
    )
    executable(
        software / "validate_cell_output.py",
        """#!/usr/bin/env python3
import json, os
assert os.environ.get('EXPECTED_DESIGNS') == '1'
assert os.environ.get('EXPECTED_FOLD_SAMPLES') == '5'
print(json.dumps({
    'status':'PASS', 'expected_designs':1, 'observed_unique_ids':1,
    'fold_samples_per_candidate':5, 'resolved_design_diffusion_samples':1,
    'resolved_design_multiplicity':1,
}, sort_keys=True))
""",
    )
    executable(
        software / "finalize_local_attempt.py",
        """#!/usr/bin/env python3
import argparse, errno, hashlib, json, os, stat
from datetime import datetime, timezone
from pathlib import Path

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

p=argparse.ArgumentParser()
p.add_argument('--attempt-root', required=True)
p.add_argument('--cell-contract', required=True)
p.add_argument('--environment-receipt', required=True)
p.add_argument('--submission-receipt', required=True)
p.add_argument('--monitor-stopped', required=True)
p.add_argument('--terminal-status', required=True)
p.add_argument('--pipeline-exit-code', required=True, type=int)
a=p.parse_args()
contract=json.loads(Path(a.cell_contract).read_text())
hierarchy_keys = (
    'BG_HIERARCHY_BG_FD',
    'BG_HIERARCHY_RUNS_FD',
    'BG_HIERARCHY_CELL_FD',
    'BG_HIERARCHY_ATTEMPT_FD',
    'BG_HIERARCHY_LOGS_FD',
)
hierarchy_text = [os.environ.get(key, '') for key in hierarchy_keys]
if any(not value.isdecimal() for value in hierarchy_text):
    raise SystemExit(91)
hierarchy = tuple(map(int, hierarchy_text))
if len(set(hierarchy)) != 5:
    raise SystemExit(91)
held = [os.fstat(value) for value in hierarchy]
for index, info in enumerate(held):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink < 2
        or (index == 0 and stat.S_IMODE(info.st_mode) & 0o022)
        or (index > 0 and stat.S_IMODE(info.st_mode) != 0o700)
    ):
        raise SystemExit(91)
bg, runs, cell, attempt, logs = hierarchy
expected_root = Path(a.attempt_root)
if (
    not expected_root.is_absolute()
    or expected_root.is_symlink()
    or expected_root.resolve() != expected_root
    or expected_root.name != contract['attempt_id']
    or expected_root.parent.name != contract['cell_id']
    or expected_root.parent.parent.name != 'runs'
):
    raise SystemExit(91)
bg_path = expected_root.parents[2]
current = (
    os.stat(bg_path, follow_symlinks=False),
    os.stat('runs', dir_fd=bg, follow_symlinks=False),
    os.stat(contract['cell_id'], dir_fd=runs, follow_symlinks=False),
    os.stat(contract['attempt_id'], dir_fd=cell, follow_symlinks=False),
    os.stat('operator_logs', dir_fd=attempt, follow_symlinks=False),
)
if any(
    (expected.st_dev, expected.st_ino) != (observed.st_dev, observed.st_ino)
    for expected, observed in zip(held, current)
):
    raise SystemExit(91)
expected_logs = expected_root / 'operator_logs'
if Path(a.monitor_stopped).parent != expected_logs:
    raise SystemExit(91)
if (
    (os.stat(a.attempt_root).st_dev, os.stat(a.attempt_root).st_ino)
    != (held[3].st_dev, held[3].st_ino)
    or (os.stat(expected_logs).st_dev, os.stat(expected_logs).st_ino)
    != (held[4].st_dev, held[4].st_ino)
):
    raise SystemExit(91)
for key in (
    'BG_HIERARCHY_SUBMISSIONS_FD',
    'BG_EXECUTOR_LOCK_PARENT_FD',
    'BG_EXECUTOR_LOCK_FD',
    'FAKE_FINALIZER_SENTINEL_FD',
):
    value = os.environ.get(key)
    if value is None:
        continue
    if not value.isdecimal():
        raise SystemExit(91)
    try:
        os.fstat(int(value))
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise
    else:
        raise SystemExit(91)
counter = os.environ.get('FAKE_FINALIZER_COUNTER')
if counter:
    with open(counter, 'ab') as handle:
        handle.write(b'1\\n')
if os.environ.get('FAKE_FINALIZER_BLOCK') == '1':
    os.write(2, b'blocking finalizer\\n')
    Path(os.environ['FAKE_FINALIZER_STARTED']).write_text('started\\n')
    while True:
        __import__('time').sleep(1)
if os.environ.get('FAKE_FINALIZER_PRECREATE_INVALID_TERMINAL') == '1':
    invalid_logs = Path(a.attempt_root) / 'operator_logs'
    invalid_manifest = invalid_logs / 'output_SHA256SUMS'
    invalid_marker = invalid_logs / 'cell.SUCCESS.json'
    invalid_manifest.write_bytes(b'')
    invalid_marker.write_text('{}\\n')
    invalid_manifest.chmod(0o444)
    invalid_marker.chmod(0o444)
    raise SystemExit(33)
if os.environ.get('FAKE_FINALIZER_FAIL') == '1':
    os.write(2, b'injected finalizer failure\\n\\n')
    raise SystemExit(31)
root=Path(a.attempt_root)
logs=root/'operator_logs'
monitor=json.loads(Path(a.monitor_stopped).read_text())
if monitor.get('status') != 'STOPPED' or monitor.get('wait_completed') is not True:
    raise SystemExit(2)
submission=json.loads(Path(a.submission_receipt).read_text())
success=a.pipeline_exit_code == 0 and a.terminal_status == contract['success_status']
if success:
    marker=logs/'cell.SUCCESS.json'
    schema='WSL2_BOLTZGEN_LOCAL_SUCCESS_V1'
else:
    if not (1 <= a.pipeline_exit_code <= 255 and a.terminal_status == 'LOCAL_CELL_FAILED'):
        raise SystemExit(1)
    marker=logs/'cell.FAILURE.json'
    schema='WSL2_BOLTZGEN_LOCAL_FAILURE_V1'
manifest=logs/'output_SHA256SUMS'
excluded={manifest.resolve(), marker.resolve()}
files=[]
for path in root.rglob('*'):
    if path.is_symlink(): raise SystemExit(3)
    if path.is_file() and path.resolve() not in excluded:
        files.append(path)
files.sort(key=lambda item: item.relative_to(root).as_posix().encode())
manifest.write_text(''.join(f'{digest(path)}  ./{path.relative_to(root).as_posix()}\\n' for path in files))
payload={
    'schema_version':schema,
    'status':'SUCCESS' if success else 'FAILURE',
    'terminal_status':a.terminal_status,
    'pipeline_exit_code':a.pipeline_exit_code,
    'executor_kind':'WSL2_SYSTEMD_SINGLE_GPU',
    'execution_contract_sha256':digest(Path(a.cell_contract)),
    'cell_id':contract['cell_id'],
    'attempt_id':contract['attempt_id'],
    'run_kind':contract['run_kind'],
    'formal_g1':False,
    'formal_g1_receipt_sha256':None,
    'environment_manifest_sha256':None,
    'completed_at_utc':monitor['stopped_at_utc'],
    **({
        'cell_contract_sha256':digest(logs/'cell_contract.json'),
        'validation_sha256':digest(logs/'cell_contract.json'),
        'resolved_config_manifest_sha256':digest(logs/'resolved_config_SHA256SUMS'),
    } if success else {'failure_class':'PIPELINE_EXIT_NONZERO'}),
    'environment_receipt_sha256':digest(Path(a.environment_receipt)),
    'monitor_stopped_sha256':digest(Path(a.monitor_stopped)),
    'monitor_healthy':monitor['monitor_healthy'],
    'submission_receipt_sha256':digest(Path(a.submission_receipt)),
    'systemd_unit':submission['unit'],
    'submission_token_sha256':hashlib.sha256(submission['submission_token'].encode('ascii')).hexdigest(),
    'invocation_id':submission['invocation_id'],
    'executor_uid':submission['executor_uid'],
    'exec_start_sha256':submission['exec_start_sha256'],
    'output_manifest_sha256':digest(manifest),
    'model_inputs_manifest_sha256':contract['model_inputs_manifest_sha256'],
    'spec_sha256':contract['spec_sha256'],
    'design_checkpoint_sha256':contract['design_checkpoint_sha256'],
    'inverse_fold_checkpoint_sha256':contract['inverse_fold_checkpoint_sha256'],
    'folding_checkpoint_sha256':contract['folding_checkpoint_sha256'],
    'mols_sha256':contract['mols_sha256'],
    'runtime_scripts_manifest_sha256':contract['runtime_scripts_manifest_sha256'],
    'spec_gate_bundle_sha256':contract['spec_gate_bundle_sha256'],
    'output_manifest_entry_count':len(files),
    'validator_sha256':digest(Path(__file__).with_name('validate_cell_output.py')),
    'finalizer_sha256':digest(Path(__file__)),
    'evidence_freeze_schema_version':'WSL2_OUTPUT_EVIDENCE_FREEZE_V1',
    'evidence_files_read_only':True,
    'evidence_directories_read_only_except_terminal_parents':True,
    'terminal_publication_parents_mutable':True,
}
marker.write_text(json.dumps(payload, sort_keys=True)+'\\n')
for path in root.rglob('*'):
    if path.is_file(): path.chmod(0o444)
for path in sorted((p for p in root.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
    if path != logs: path.chmod(0o555)
if os.environ.get('FAKE_FINALIZER_FAIL_AFTER_MARKER') == '1':
    os.write(2, b'injected post-marker failure\\n')
    raise SystemExit(32)
print(json.dumps({'terminal_status': a.terminal_status, 'marker_path': str(marker)}, sort_keys=True))
""",
    )
    fake_bin = root / "runner_fake_bin"
    executable(
        fake_bin / "nvidia-smi",
        """#!/usr/bin/env bash
if [ -n "${FAKE_PROBE_SLEEP:-}" ] && [[ " $* " != *' --loop='* ]] && [[ " $* " != *' --loop '* ]]; then
  : > "$FAKE_PROBE_STARTED"
  sleep "$FAKE_PROBE_SLEEP"
fi
if [ "${FAKE_MONITOR_FAIL:-0}" = 1 ]; then exit 42; fi
if [[ " $* " == *' --loop='* ]] || [[ " $* " == *' --loop '* ]]; then
  trap 'if [ -n "${FAKE_MONITOR_STOP_DELAY:-}" ]; then : > "$FAKE_MONITOR_STOP_STARTED"; sleep "$FAKE_MONITOR_STOP_DELAY"; fi; exit 0' TERM INT
  printf 'timestamp, index, name, memory.total [MiB], memory.used [MiB], utilization.gpu [%%], power.draw [W]\\n'
  while true; do
    printf '2026-08-30, 0, Fixture GPU, 10000 MiB, 100 MiB, 1 %%, 10 W\\n'
    sleep 0.1
  done
else
  printf 'GPU 0: Fixture GPU\\n'
fi
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_BOLTZGEN_LOG"] = str(root / "boltzgen.log")
    env["FAKE_VERIFY_LOG"] = str(root / "verify.log")
    env["FAKE_PROBE_STARTED"] = str(root / "probe.started")
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "CUDA_VISIBLE_DEVICES"):
        env.pop(key, None)
    return env


def prepare_runner(tmp_path: Path) -> tuple[Path, Path, dict[str, str], dict[str, str]]:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    runner_env = install_runner_fakes(tmp_path, bg_work)
    contract = make_contract(tmp_path, bg_work)
    _, systemd_env = fake_systemd(tmp_path)
    submitted = submit(bg_work, contract, systemd_env)
    assert submitted.returncode == 0, submitted.stderr
    runner_env.update(
        {
            key: value
            for key, value in systemd_env.items()
            if key.startswith("FAKE_SYSTEMD") or key == "FAKE_SYSTEMCTL_MODE"
        }
    )
    attempt = bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001"
    runner_env["FAKE_ATTEMPT_ROOT"] = str(attempt)
    intent, _ = submission_paths(bg_work)
    runner_env["BG_SUBMISSION_TOKEN"] = json.loads(intent.read_text(encoding="utf-8"))[
        "submission_token"
    ]
    runner_env["INVOCATION_ID"] = "a" * 32
    return bg_work, contract, systemd_env, runner_env


def run_cell(
    bg_work: Path,
    contract: Path,
    env: dict[str, str],
    *,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(bg_work / "run_local_cell.sh"), str(bg_work), str(contract)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        pass_fds=pass_fds,
        timeout=30,
    )


def test_runner_runs_five_stages_stops_and_waits_monitor_then_publishes_canonical_success(
    tmp_path: Path,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)

    result = run_cell(bg_work, contract, env)

    assert result.returncode == 0, result.stderr
    attempt = bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001"
    calls = (tmp_path / "boltzgen.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 6
    assert calls[0].startswith("configure\t")
    assert "--no_subprocess" not in calls[0].split("\t")
    pinned_roots = []
    for call in calls:
        arguments = call.split("\t")
        if arguments[0] == "configure":
            pinned_root = arguments[arguments.index("--output") + 1]
        else:
            assert arguments[0] == "execute"
            pinned_root = arguments[1]
        parts = Path(pinned_root).parts
        assert len(parts) == 5
        assert parts[:2] == ("/", "proc")
        assert parts[2].isdecimal()
        assert parts[2] != "self"
        assert parts[3] == "fd"
        assert parts[4].isdecimal()
        assert "/proc/self/" not in pinned_root
        pinned_roots.append(pinned_root)
    assert len(set(pinned_roots)) == 1
    for stage, call in zip(("design", "inverse_folding", "folding", "analysis", "filtering"), calls[1:]):
        assert call.startswith("execute\t")
        assert call.split("\t").count("--no_subprocess") == 1
        assert f"--steps\t{stage}" in call
        assert (attempt / "operator_logs" / f"{stage}.exit_code.txt").read_text().strip() == "0"
        expected_config = f"{stage}.yaml"
        assert (attempt / "results" / f"{stage}.done").read_text().strip() == (
            f"{stage}:{expected_config}"
        )
    configure = calls[0]
    assert "--protocol\tnanobody-anything" in configure
    assert "analysis\tliability_modality=antibody" in configure
    assert "filtering\tmodality=antibody\tfilter_bindingsite=true" in configure
    monitor = json.loads((attempt / "operator_logs" / "monitor.stopped.json").read_text())
    assert monitor["status"] == "STOPPED"
    assert monitor["wait_completed"] is True
    marker_path = attempt / "operator_logs" / "cell.SUCCESS.json"
    marker = json.loads(marker_path.read_text())
    assert marker["status"] == "SUCCESS"
    assert marker["pipeline_exit_code"] == 0
    manifest_path = attempt / "operator_logs" / "output_SHA256SUMS"
    records = manifest_path.read_text(encoding="utf-8").splitlines()
    assert records
    assert all(len(line.split("  ", 1)[0]) == 64 and line.split("  ", 1)[1].startswith("./") for line in records)
    names = {line.split("  ", 1)[1] for line in records}
    assert "./operator_logs/output_SHA256SUMS" not in names
    assert "./operator_logs/cell.SUCCESS.json" not in names
    assert "./operator_logs/finalizer.log.txt" not in names
    assert not (attempt / "operator_logs" / "finalizer.log.txt").exists()
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))
    resolved = (attempt / "operator_logs" / "resolved_config_SHA256SUMS").read_text(
        encoding="utf-8"
    ).splitlines()
    assert {line.split("  ", 1)[1] for line in resolved} == {
        "./config/analysis.yaml",
        "./config/design.yaml",
        "./config/filtering.yaml",
        "./config/folding.yaml",
        "./config/inverse_folding.yaml",
    }
    assert not (attempt / "receipt.json").exists()
    assert not (attempt / "outputs.SHA256SUMS").exists()

    before = snapshot_tree(bg_work)
    status = subprocess.run(
        ["bash", str(implementation("status_local_cell.sh")), str(bg_work), "engineering_smoke_7xl0", "attempt_001"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["state"] == "SUCCEEDED"
    assert payload["executor_kind"] == "WSL2_SYSTEMD_SINGLE_GPU"
    assert snapshot_tree(bg_work) == before


def test_runner_supplies_contract_counts_to_validator_from_clean_executor_environment(
    tmp_path: Path,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    assert "EXPECTED_DESIGNS" not in env
    assert "EXPECTED_FOLD_SAMPLES" not in env

    result = run_cell(bg_work, contract, env)

    assert result.returncode == 0, result.stderr
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    validation = json.loads(
        (attempt / "operator_logs/cell_contract.json").read_text(encoding="utf-8")
    )
    assert validation["expected_designs"] == 1
    assert validation["fold_samples_per_candidate"] == 5


def test_finalizer_inherits_only_five_bound_hierarchy_descriptors(
    tmp_path: Path,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    sentinel_path = tmp_path / "inheritable.sentinel"
    sentinel_path.write_bytes(b"must not reach finalizer\n")
    sentinel = os.open(sentinel_path, os.O_RDONLY)
    os.set_inheritable(sentinel, True)
    env["FAKE_FINALIZER_SENTINEL_FD"] = str(sentinel)
    try:
        result = run_cell(bg_work, contract, env, pass_fds=(sentinel,))
    finally:
        os.close(sentinel)

    assert result.returncode == 0, result.stderr
    marker = bg_work / "runs/engineering_smoke_7xl0/attempt_001/operator_logs/cell.SUCCESS.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "SUCCESS"


def test_stage_failure_still_stops_and_waits_monitor_and_never_writes_success(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_FAIL_STAGE"] = "folding"

    result = run_cell(bg_work, contract, env)

    assert result.returncode == 23
    attempt = bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001"
    monitor = json.loads((attempt / "operator_logs" / "monitor.stopped.json").read_text())
    assert monitor["status"] == "STOPPED"
    assert monitor["wait_completed"] is True
    assert not (attempt / "operator_logs" / "cell.SUCCESS.json").exists()
    failure = json.loads((attempt / "operator_logs" / "cell.FAILURE.json").read_text())
    assert failure["status"] == "FAILURE"
    assert failure["pipeline_exit_code"] == 23
    assert not (attempt / "operator_logs/finalizer.log.txt").exists()
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))
    calls = (tmp_path / "boltzgen.log").read_text(encoding="utf-8")
    assert "--steps\tanalysis" not in calls
    assert "--steps\tfiltering" not in calls
    status = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 1, status.stderr
    assert json.loads(status.stdout)["state"] == "FAILED"


def test_runner_rejects_missing_analysis_config_before_any_gpu_stage(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_CONFIG_OMIT_ANALYSIS"] = "1"

    result = run_cell(bg_work, contract, env)

    assert result.returncode != 0
    attempt = bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001"
    assert not (attempt / "operator_logs" / "cell.SUCCESS.json").exists()
    failure = json.loads((attempt / "operator_logs" / "cell.FAILURE.json").read_text())
    assert failure["status"] == "FAILURE"
    calls = (tmp_path / "boltzgen.log").read_text(encoding="utf-8")
    assert "execute\t" not in calls


def test_runner_rejects_failed_gpu_monitor_and_never_writes_success(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_MONITOR_FAIL"] = "1"

    result = run_cell(bg_work, contract, env)

    assert result.returncode != 0
    attempt = bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001"
    assert not (attempt / "operator_logs" / "cell.SUCCESS.json").exists()
    monitor = json.loads((attempt / "operator_logs" / "monitor.stopped.json").read_text())
    assert monitor["wait_completed"] is True
    assert monitor["monitor_healthy"] is False
    failure = json.loads((attempt / "operator_logs" / "cell.FAILURE.json").read_text())
    assert failure["status"] == "FAILURE"


def test_finalizer_failure_is_sealed_as_distinct_emergency_terminal(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_FINALIZER_FAIL"] = "1"

    result = run_cell(bg_work, contract, env)

    assert result.returncode == 31
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    marker = attempt / "operator_logs/cell.EMERGENCY_FAILURE.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "WSL2_BOLTZGEN_LOCAL_EMERGENCY_FAILURE_V1"
    assert payload["finalizer_exit_code"] == 31
    finalizer_log = attempt / "operator_logs/finalizer.log.txt"
    assert finalizer_log.read_bytes() == b"injected finalizer failure\n\n"
    assert payload["finalizer_log_sha256"] == sha256(finalizer_log)
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))
    assert not (attempt / "operator_logs/cell.FAILURE.json").exists()
    status = subprocess.run(
        ["bash", str(implementation("status_local_cell.sh")), str(bg_work), "engineering_smoke_7xl0", "attempt_001"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 1, status.stderr
    assert json.loads(status.stdout)["state"] == "FAILED"


def test_post_marker_finalizer_failure_never_mutates_canonical_success(
    tmp_path: Path,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_FINALIZER_FAIL_AFTER_MARKER"] = "1"

    result = run_cell(bg_work, contract, env)

    assert result.returncode == 0, result.stderr
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    logs = attempt / "operator_logs"
    assert (logs / "cell.SUCCESS.json").is_file()
    assert not (logs / "finalizer.log.txt").exists()
    records = (logs / "output_SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert "./operator_logs/finalizer.log.txt" not in {
        line.split("  ", 1)[1] for line in records
    }
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))
    status = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["state"] == "SUCCEEDED"


def test_invalid_preexisting_terminal_never_masks_finalizer_failure(
    tmp_path: Path,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_FINALIZER_PRECREATE_INVALID_TERMINAL"] = "1"

    result = run_cell(bg_work, contract, env)

    assert result.returncode == 75, result.stderr
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    logs = attempt / "operator_logs"
    assert not (logs / "finalizer.log.txt").exists()
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))
    status = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 3, status.stderr
    assert json.loads(status.stdout)["state"] == "BLOCKED_TERMINAL_MARKER_INVALID"


@pytest.mark.parametrize("signal_group", [False, True])
def test_term_during_finalizer_runs_once_and_seals_exact_emergency_failure(
    tmp_path: Path, signal_group: bool,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    started = tmp_path / "finalizer.started"
    counter = tmp_path / "finalizer.invocations"
    env.update(
        {
            "FAKE_FINALIZER_BLOCK": "1",
            "FAKE_FINALIZER_STARTED": str(started),
            "FAKE_FINALIZER_COUNTER": str(counter),
        }
    )
    process = subprocess.Popen(
        ["bash", str(bg_work / "run_local_cell.sh"), str(bg_work), str(contract)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    for _ in range(200):
        if started.exists():
            break
        __import__("time").sleep(0.02)
    assert started.exists()
    if signal_group:
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    stdout, stderr = process.communicate(timeout=15)
    del stdout

    assert process.returncode == 143, stderr
    assert counter.read_bytes() == b"1\n"
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    logs = attempt / "operator_logs"
    emergency = logs / "cell.EMERGENCY_FAILURE.json"
    payload = json.loads(emergency.read_text(encoding="utf-8"))
    assert payload["finalizer_exit_code"] == 143
    assert payload["pipeline_exit_code"] == 143
    finalizer_log = logs / "finalizer.log.txt"
    assert finalizer_log.read_bytes() == b"blocking finalizer\n"
    assert payload["finalizer_log_sha256"] == sha256(finalizer_log)
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))
    assert not (logs / "cell.SUCCESS.json").exists()
    assert not (logs / "cell.FAILURE.json").exists()
    status = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert status.returncode == 1, status.stderr
    assert json.loads(status.stdout)["state"] == "FAILED"


@pytest.mark.parametrize("signal_group", [False, True])
def test_term_before_finalizer_spawn_cannot_publish_success(
    tmp_path: Path, signal_group: bool,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    monitor_stopping = tmp_path / "monitor.stopping"
    counter = tmp_path / "finalizer.invocations"
    env.update(
        {
            "FAKE_MONITOR_STOP_DELAY": "1",
            "FAKE_MONITOR_STOP_STARTED": str(monitor_stopping),
            "FAKE_FINALIZER_COUNTER": str(counter),
        }
    )
    process = subprocess.Popen(
        ["bash", str(bg_work / "run_local_cell.sh"), str(bg_work), str(contract)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    for _ in range(200):
        if monitor_stopping.exists():
            break
        __import__("time").sleep(0.02)
    assert monitor_stopping.exists()
    if signal_group:
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    stdout, stderr = process.communicate(timeout=15)
    del stdout

    assert process.returncode == 143, stderr
    assert counter.read_bytes() == b"1\n"
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    logs = attempt / "operator_logs"
    failure = json.loads((logs / "cell.FAILURE.json").read_text(encoding="utf-8"))
    assert failure["pipeline_exit_code"] == 143
    assert not (logs / "cell.SUCCESS.json").exists()
    assert not (logs / "finalizer.log.txt").exists()
    assert not list((bg_work / "local_submissions").glob("*.finalizer.capture.tmp"))


def test_signal_before_monitor_start_seals_null_pid_monitor_receipt(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_PROBE_SLEEP"] = "1"
    process = subprocess.Popen(
        ["bash", str(bg_work / "run_local_cell.sh"), str(bg_work), str(contract)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    started = Path(env["FAKE_PROBE_STARTED"])
    for _ in range(100):
        if started.exists():
            break
        __import__("time").sleep(0.02)
    assert started.exists()
    process.terminate()
    stdout, stderr = process.communicate(timeout=10)
    del stdout

    assert process.returncode == 143, stderr
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    monitor = json.loads((attempt / "operator_logs/monitor.stopped.json").read_text())
    assert monitor["monitor_started"] is False
    assert monitor["monitor_pid"] is None
    assert (attempt / "operator_logs/cell.FAILURE.json").is_file()


def test_runner_seals_original_attempt_when_runs_name_drifts_during_gpu_probe(
    tmp_path: Path,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["FAKE_PROBE_SLEEP"] = "1"
    process = subprocess.Popen(
        ["bash", str(bg_work / "run_local_cell.sh"), str(bg_work), str(contract)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    started = Path(env["FAKE_PROBE_STARTED"])
    for _ in range(100):
        if started.exists():
            break
        __import__("time").sleep(0.02)
    assert started.exists()

    runs = bg_work / "runs"
    original_runs = bg_work / "runs.original"
    decoy = tmp_path / "runs.decoy"
    decoy.mkdir()
    runs.rename(original_runs)
    runs.symlink_to(decoy, target_is_directory=True)

    stdout, stderr = process.communicate(timeout=15)
    del stdout
    assert process.returncode != 0, stderr
    original_attempt = original_runs / "engineering_smoke_7xl0/attempt_001"
    logs = original_attempt / "operator_logs"
    assert not (logs / "cell.SUCCESS.json").exists()
    assert not (logs / "cell.FAILURE.json").exists()
    marker = logs / "cell.EMERGENCY_FAILURE.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "WSL2_BOLTZGEN_LOCAL_EMERGENCY_FAILURE_V1"
    assert payload["failure_class"] == "ANCESTOR_IDENTITY_DRIFT"
    assert payload["pipeline_exit_code"] != 0
    assert (logs / "emergency_output_SHA256SUMS").is_file()
    assert not list(decoy.iterdir())


def test_engineering_environment_cannot_submit_formal_or_g2_contract(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload.update(
        {
            "stage_class": "FORMAL",
            "run_kind": "G2_ACCEPTANCE",
            "success_status": "G2_ACCEPTANCE_PASS",
            "expected_designs": 10,
            "budget": 10,
        }
    )
    contract.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    log, env = fake_systemd(tmp_path)

    result = submit(bg_work, contract, env)

    assert result.returncode != 0
    assert not log.exists() or not log.read_bytes()
    assert not (bg_work / "local_submissions").exists()


def test_submit_rejects_pseudoformal_environment_revision_before_intent(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    receipt, environment_contract = promote_contract_to_formal(tmp_path, bg_work, contract)
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_payload["environment_contract_revision"] = "ENGINEERING_CANDIDATE_V4"
    receipt.write_text(
        json.dumps(receipt_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    cell_payload = json.loads(contract.read_text(encoding="utf-8"))
    cell_payload["environment_receipt_sha256"] = sha256(receipt)
    contract.write_text(
        json.dumps(cell_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    environment_payload = json.loads(environment_contract.read_text(encoding="utf-8"))
    environment_payload["environment_receipt_sha256"] = sha256(receipt)
    environment_contract.write_text(
        json.dumps(environment_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    log, env = fake_systemd(tmp_path)

    result = submit(bg_work, contract, env)

    assert result.returncode != 0
    assert not log.exists() or not log.read_bytes()
    assert not (bg_work / "local_submissions").exists()


def test_runner_rejects_pseudoformal_recursive_binding_before_attempt_or_gpu(
    tmp_path: Path,
) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    runner_env = install_runner_fakes(tmp_path, bg_work)
    contract = make_contract(tmp_path, bg_work)
    _, environment_contract = promote_contract_to_formal(tmp_path, bg_work, contract)
    _, systemd_env = fake_systemd(tmp_path)
    submitted = submit(bg_work, contract, systemd_env)
    assert submitted.returncode == 0, submitted.stderr
    runner_env.update(
        {
            key: value
            for key, value in systemd_env.items()
            if key.startswith("FAKE_SYSTEMD") or key == "FAKE_SYSTEMCTL_MODE"
        }
    )
    intent, _ = submission_paths(bg_work)
    runner_env["BG_SUBMISSION_TOKEN"] = json.loads(intent.read_text(encoding="utf-8"))[
        "submission_token"
    ]
    runner_env["INVOCATION_ID"] = "a" * 32

    environment_payload = json.loads(environment_contract.read_text(encoding="utf-8"))
    environment_payload["artifact_bindings"]["recursive_payload_manifest"]["sha256"] = "0" * 64
    environment_contract.write_text(
        json.dumps(environment_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    result = run_cell(bg_work, contract, runner_env)

    assert result.returncode != 0
    assert not (bg_work / "runs/engineering_smoke_7xl0/attempt_001").exists()
    assert not Path(runner_env["FAKE_PROBE_STARTED"]).exists()


def test_submit_rejects_environment_contract_for_another_executor_uid(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    environment_contract = bg_work / "contract/environment_contract.json"
    payload = json.loads(environment_contract.read_text(encoding="utf-8"))
    payload["executor_uid"] = os.getuid() + 1
    environment_contract.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    log, env = fake_systemd(tmp_path)

    result = submit(bg_work, contract, env)

    assert result.returncode != 0
    assert not log.exists()
    assert not (bg_work / "local_submissions").exists()


def test_runner_revalidates_contract_against_submission_intent(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["budget"] = 2
    contract.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    result = run_cell(bg_work, contract, env)

    assert result.returncode != 0
    assert not (bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001").exists()


def test_runner_requires_receipt_bound_invocation_id_before_attempt(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["INVOCATION_ID"] = "b" * 32

    result = run_cell(bg_work, contract, env)

    assert result.returncode != 0
    assert not (bg_work / "runs/engineering_smoke_7xl0/attempt_001").exists()


def test_runner_rejects_symlinked_runs_hierarchy_without_touching_target(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    (bg_work / "runs").symlink_to(outside, target_is_directory=True)

    result = run_cell(bg_work, contract, env)

    assert result.returncode != 0
    assert not list(outside.iterdir())


def test_runner_refuses_when_global_gpu_lock_is_held(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    runtime_root = Path(f"/run/user/{os.getuid()}")
    assert runtime_root.is_dir(), "systemd user runtime directory is required"
    descriptor = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_cell(bg_work, contract, env)
    finally:
        os.close(descriptor)
    assert result.returncode == 75
    assert not (bg_work / "runs" / "engineering_smoke_7xl0" / "attempt_001").exists()


def test_machine_gpu_lock_has_one_fixed_non_replaceable_inode(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    result = run_cell(bg_work, contract, env)
    assert result.returncode == 0, result.stderr

    runtime_root = Path(f"/run/user/{os.getuid()}")
    parent = runtime_root.parent
    before = runtime_root.stat()
    assert not runtime_root.is_symlink()
    assert before.st_uid == os.getuid()
    assert before.st_mode & 0o777 == 0o700
    assert parent.stat().st_uid == 0
    assert parent.stat().st_mode & 0o022 == 0
    descriptor = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        held = os.fstat(descriptor)
        assert (held.st_dev, held.st_ino) == (before.st_dev, before.st_ino)
    finally:
        os.close(descriptor)


def test_status_reports_running_without_writing_anything(tmp_path: Path) -> None:
    bg_work = tmp_path / "bg_work"
    bg_work.mkdir()
    contract = make_contract(tmp_path, bg_work)
    _, env = fake_systemd(tmp_path)
    assert submit(bg_work, contract, env).returncode == 0
    before = snapshot_tree(bg_work)

    result = subprocess.run(
        ["bash", str(implementation("status_local_cell.sh")), str(bg_work), "engineering_smoke_7xl0", "attempt_001"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "RUNNING"
    assert snapshot_tree(bg_work) == before


@pytest.mark.parametrize(
    "tamper",
    (
        "payload_changed",
        "payload_missing",
        "manifest_duplicate",
        "manifest_noncanonical",
        "manifest_missing_entry",
        "manifest_extra_entry",
        "tree_symlink",
        "tree_fifo",
    ),
)
def test_status_recomputes_exact_terminal_manifest_and_rejects_tree_drift(
    tmp_path: Path, tamper: str
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    assert run_cell(bg_work, contract, env).returncode == 0
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    manifest = attempt / "operator_logs/output_SHA256SUMS"
    payload = attempt / "results/design.done"
    for path in attempt.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
        elif not path.is_symlink():
            path.chmod(0o600)

    if tamper == "payload_changed":
        payload.write_text("tampered\n", encoding="utf-8")
    elif tamper == "payload_missing":
        payload.unlink()
    elif tamper == "manifest_duplicate":
        first = manifest.read_text(encoding="utf-8").splitlines()[0]
        manifest.write_text(manifest.read_text(encoding="utf-8") + first + "\n", encoding="utf-8")
    elif tamper == "manifest_noncanonical":
        body = manifest.read_text(encoding="utf-8")
        manifest.write_text(body.replace("./config/design.yaml", "./config/./design.yaml"), encoding="utf-8")
    elif tamper == "manifest_missing_entry":
        lines = manifest.read_text(encoding="utf-8").splitlines()
        manifest.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    elif tamper == "manifest_extra_entry":
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"{'0' * 64}  ./does-not-exist\n",
            encoding="utf-8",
        )
    elif tamper == "tree_symlink":
        (attempt / "unsafe-link").symlink_to(payload)
    else:
        os.mkfifo(attempt / "unsafe-fifo")

    for path in attempt.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir() and path != attempt / "operator_logs":
            path.chmod(0o555)

    result = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["state"] == "BLOCKED_TERMINAL_MARKER_INVALID"


def test_status_requires_canonical_live_contract_matching_intent_and_marker(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    assert run_cell(bg_work, contract, env).returncode == 0
    original = contract.read_bytes()
    contract.write_bytes(original + b" ")

    result = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["state"] == "BLOCKED_TERMINAL_MARKER_INVALID"


@pytest.mark.parametrize("mutation", ("wrong_schema", "extra_field", "missing_field"))
def test_status_rejects_terminal_marker_with_wrong_fixed_field_set(
    tmp_path: Path, mutation: str
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    assert run_cell(bg_work, contract, env).returncode == 0
    marker = bg_work / "runs/engineering_smoke_7xl0/attempt_001/operator_logs/cell.SUCCESS.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    if mutation == "wrong_schema":
        payload["schema_version"] = "WSL2_LOCAL_CELL_TERMINAL_V1"
    elif mutation == "extra_field":
        payload["unexpected"] = True
    else:
        payload.pop("exec_start_sha256")
    marker.chmod(0o600)
    marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    marker.chmod(0o444)

    result = subprocess.run(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 3
    assert json.loads(result.stdout)["state"] == "BLOCKED_TERMINAL_MARKER_INVALID"


def test_runner_rejects_polluted_cuda_visible_devices_before_attempt(tmp_path: Path) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    env["CUDA_VISIBLE_DEVICES"] = "0"

    result = run_cell(bg_work, contract, env)

    assert result.returncode != 0
    assert not (bg_work / "runs/engineering_smoke_7xl0/attempt_001").exists()


@pytest.mark.parametrize("mutation", ("replace_inode", "chmod_restore"))
def test_status_rejects_early_payload_mutated_after_late_hash_has_started(
    tmp_path: Path, mutation: str,
) -> None:
    bg_work, contract, _, env = prepare_runner(tmp_path)
    assert run_cell(bg_work, contract, env).returncode == 0
    attempt = bg_work / "runs/engineering_smoke_7xl0/attempt_001"
    logs = attempt / "operator_logs"
    manifest = logs / "output_SHA256SUMS"
    marker = logs / "cell.SUCCESS.json"
    late = attempt / "zzz_large_payload.bin"
    with late.open("wb") as handle:
        handle.truncate(256 * 1024 * 1024)
    manifest.chmod(0o600)
    marker.chmod(0o600)

    members = sorted(
        (
            path
            for path in attempt.rglob("*")
            if path.is_file() and not path.is_symlink() and path not in {manifest, marker}
        ),
        key=lambda path: path.relative_to(attempt).as_posix().encode("utf-8"),
    )
    manifest.write_text(
        "".join(
            f"{sha256(path)}  ./{path.relative_to(attempt).as_posix()}\n" for path in members
        ),
        encoding="utf-8",
    )
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    marker_payload["output_manifest_sha256"] = sha256(manifest)
    marker.write_text(json.dumps(marker_payload, sort_keys=True) + "\n", encoding="utf-8")
    manifest.chmod(0o444)
    marker.chmod(0o444)

    libc = ctypes.CDLL(None, use_errno=True)
    inotify_fd = libc.inotify_init1(os.O_CLOEXEC)
    assert inotify_fd >= 0, os.strerror(ctypes.get_errno())
    watch = libc.inotify_add_watch(inotify_fd, os.fsencode(late), 0x00000020)  # IN_OPEN
    assert watch >= 0, os.strerror(ctypes.get_errno())
    process = subprocess.Popen(
        [
            "bash",
            str(implementation("status_local_cell.sh")),
            str(bg_work),
            "engineering_smoke_7xl0",
            "attempt_001",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        readable, _, _ = select.select([inotify_fd], [], [], 10)
        assert readable, "status never opened the late payload"
        os.read(inotify_fd, 4096)
        early = attempt / "config/analysis.yaml"
        if mutation == "replace_inode":
            early.parent.chmod(0o700)
            replacement = attempt / "config/.analysis.yaml.replacement"
            replacement.write_bytes(early.read_bytes())
            os.replace(replacement, early)
            early.chmod(0o444)
            early.parent.chmod(0o555)
        else:
            original_mode = early.stat().st_mode & 0o777
            early.chmod(0o600)
            early.chmod(original_mode)
        stdout, stderr = process.communicate(timeout=20)
    finally:
        os.close(inotify_fd)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 3, stderr
    assert json.loads(stdout)["state"] == "BLOCKED_TERMINAL_MARKER_INVALID"


def test_all_shell_entrypoints_parse_and_forbid_reuse_and_scheduler_impersonation() -> None:
    forbidden_scheduler_token = "SLURM" + "_JOB_ID"
    for name in (
        "verify_gpu_env_stage.sh",
        "submit_local_once.sh",
        "run_local_cell.sh",
        "status_local_cell.sh",
    ):
        path = implementation(name)
        parsed = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
        assert parsed.returncode == 0, f"{name}: {parsed.stderr}"
        assert forbidden_scheduler_token not in path.read_text(encoding="utf-8")
    assert "--" + "reuse" not in implementation("run_local_cell.sh").read_text(encoding="utf-8")
