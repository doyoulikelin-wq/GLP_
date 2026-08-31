#!/usr/bin/env bash
# Revalidate the bound CUDA/Blackwell environment before every GPU business stage.

set -euo pipefail
umask 077

die() {
  local message=${1:-"environment stage verification failed"}
  local code=${2:-70}
  printf 'verify_gpu_env_stage: %s\n' "$message" >&2
  exit "$code"
}

if [ "${PYTHONPATH+x}" = x ] || [ "${PYTHONHOME+x}" = x ] || \
   [ "${PYTHONOPTIMIZE+x}" = x ] || [ "${CUDA_VISIBLE_DEVICES+x}" = x ]; then
  die "PYTHONPATH, PYTHONHOME, PYTHONOPTIMIZE, and CUDA_VISIBLE_DEVICES must all be unset" 70
fi
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export LC_ALL=C

BG_WORK=${1:?usage: verify_gpu_env_stage.sh BG_WORK STAGE_ID}
STAGE_ID=${2:?usage: verify_gpu_env_stage.sh BG_WORK STAGE_ID}
[[ "$STAGE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || \
  die "unsafe stage id: $STAGE_ID" 64

test -d "$BG_WORK" || die "BG_WORK is not a directory: $BG_WORK" 66
test ! -L "$BG_WORK" || die "BG_WORK may not be a symlink" 66
BG_WORK=$(readlink -f -- "$BG_WORK")
python3 -I -S - "$BG_WORK" <<'PY' || die "BG_WORK ownership/mode is unsafe" 66
import os
import stat
import sys
info = os.stat(sys.argv[1], follow_symlinks=False)
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022):
    raise SystemExit(1)
PY
CONTRACT_PATH="$BG_WORK/contract/environment_contract.json"
[ -d "$BG_WORK/contract" ] && [ ! -L "$BG_WORK/contract" ] || \
  die "contract directory is missing or unsafe" 66
test -f "$CONTRACT_PATH" && test ! -L "$CONTRACT_PATH" || \
  die "missing or unsafe environment contract: $CONTRACT_PATH" 66
RUNTIME_MANIFEST="$BG_WORK/gpu_runtime_scripts_SHA256SUMS"
test -f "$RUNTIME_MANIFEST" && test ! -L "$RUNTIME_MANIFEST" || \
  die "missing or unsafe runtime scripts manifest: $RUNTIME_MANIFEST" 66

AUDIT_PARENT="$BG_WORK/stage_audits"
AUDIT_FINAL="$AUDIT_PARENT/$STAGE_ID"
mkdir -p -- "$AUDIT_PARENT"
test ! -L "$AUDIT_PARENT" || die "stage_audits may not be a symlink" 66
if [ -e "$AUDIT_FINAL" ] || [ -L "$AUDIT_FINAL" ]; then
  test -d "$AUDIT_FINAL" && test ! -L "$AUDIT_FINAL" || \
    die "existing stage audit is not a regular directory: $AUDIT_FINAL" 73
  test -f "$AUDIT_FINAL/stage_environment.SHA256SUMS" && \
    test ! -L "$AUDIT_FINAL/stage_environment.SHA256SUMS" || \
    die "existing stage audit lacks its manifest; refusing overwrite" 73
  ( cd "$AUDIT_FINAL" && sha256sum --strict -c stage_environment.SHA256SUMS >/dev/null ) || \
    die "existing stage audit manifest does not verify; refusing overwrite" 73
fi

AUDIT_TMP=$(mktemp -d "$AUDIT_PARENT/.${STAGE_ID}.tmp.XXXXXX")
cleanup() {
  if [ -n "${AUDIT_TMP:-}" ] && [ -d "$AUDIT_TMP" ]; then
    chmod -R u+w -- "$AUDIT_TMP" 2>/dev/null || true
    rm -rf -- "$AUDIT_TMP"
  fi
}
trap cleanup EXIT HUP INT TERM

# Validate the production contract and receipt before trusting any path from them.
python3 -I -S - "$CONTRACT_PATH" "$RUNTIME_MANIFEST" "$BG_WORK" "$STAGE_ID" \
  "$AUDIT_TMP/contract_binding.json" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

contract_path = Path(sys.argv[1])
runtime_manifest_path = Path(sys.argv[2])
bg_work = Path(sys.argv[3])
stage_id = sys.argv[4]
output_path = Path(sys.argv[5])
sha_pattern = re.compile(r"[0-9a-f]{64}")
formal_revision_pattern = re.compile(
    r"WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V[1-9][0-9]*"
)
manifest_line = re.compile(r"([0-9a-f]{64})  \./([^\n\r\0]+)")
runtime_members = (
    "run_local_cell.sh",
    "software/finalize_local_attempt.py",
    "software/validate_cell_output.py",
    "status_local_cell.sh",
    "submit_local_once.sh",
    "verify_gpu_env_stage.sh",
)


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path: Path, label: str):
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"missing or unsafe {label}: {path}")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return value


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def absolute_path(raw, label: str, *, directory: bool = False) -> Path:
    if not isinstance(raw, str) or not raw:
        raise SystemExit(f"missing {label} path")
    unresolved = Path(raw)
    if not unresolved.is_absolute() or unresolved.is_symlink():
        raise SystemExit(f"{label} path must be absolute and non-symlink: {raw!r}")
    path = unresolved.resolve(strict=True)
    expected_type = path.is_dir() if directory else path.is_file()
    if not expected_type:
        raise SystemExit(f"{label} has wrong filesystem type: {path}")
    return path


def required_sha(raw, label: str) -> str:
    if not isinstance(raw, str) or sha_pattern.fullmatch(raw) is None:
        raise SystemExit(f"{label} must be a lowercase SHA-256")
    return raw


def validate_runtime_manifest():
    if runtime_manifest_path != bg_work / "gpu_runtime_scripts_SHA256SUMS":
        raise SystemExit("runtime scripts manifest is not at the BG_WORK root")
    if runtime_manifest_path.is_symlink() or not runtime_manifest_path.is_file():
        raise SystemExit("runtime scripts manifest is missing or unsafe")
    raw = runtime_manifest_path.read_text(encoding="utf-8")
    if not raw or not raw.endswith("\n") or "\r" in raw or "\0" in raw:
        raise SystemExit("runtime scripts manifest framing is invalid")
    records = []
    for line in raw.splitlines():
        match = manifest_line.fullmatch(line)
        if match is None:
            raise SystemExit("runtime scripts manifest line is invalid")
        expected_sha, relative = match.groups()
        if relative.startswith("/") or "\\" in relative:
            raise SystemExit("runtime scripts manifest path is unsafe")
        pure = Path(relative)
        if pure.as_posix() != relative or any(part in {"", ".", ".."} for part in pure.parts):
            raise SystemExit("runtime scripts manifest path is non-canonical")
        target = bg_work / pure
        if target.is_symlink() or not target.is_file() or target.resolve() != target:
            raise SystemExit(f"runtime script is missing or unsafe: {relative}")
        if digest(target) != expected_sha:
            raise SystemExit(f"runtime script SHA-256 mismatch: {relative}")
        records.append((relative, expected_sha))
    names = tuple(relative for relative, _ in records)
    expected_names = tuple(sorted(runtime_members, key=lambda value: value.encode("utf-8")))
    if names != expected_names:
        raise SystemExit("runtime scripts manifest does not contain the exact required set")
    return {relative: value for relative, value in records}


contract = load(contract_path, "environment contract")
runtime_script_sha256 = validate_runtime_manifest()
if contract.get("schema_version") != "WSL2_GPU_STAGE_ENVIRONMENT_CONTRACT_V1":
    raise SystemExit("unsupported environment contract schema")
contract_id = contract.get("contract_id")
if not isinstance(contract_id, str) or not contract_id:
    raise SystemExit("missing contract_id")
stage_class = contract.get("stage_class")
if stage_class not in {"ENGINEERING", "FORMAL"}:
    raise SystemExit("stage_class must be ENGINEERING or FORMAL")
executor_uid = contract.get("executor_uid")
if type(executor_uid) is not int or executor_uid != os.getuid():
    raise SystemExit("environment contract executor_uid does not match the current executor")
if contract.get("environment_subdir") != "env":
    raise SystemExit("environment_subdir must be exactly env")

attempt_root = absolute_path(
    contract.get("environment_attempt_root"), "environment_attempt_root", directory=True
)
receipt_path = absolute_path(contract.get("environment_receipt_path"), "environment receipt")
if receipt_path != attempt_root / "receipt.json":
    raise SystemExit("environment_receipt_path must be attempt_root/receipt.json")
receipt_expected_sha = required_sha(
    contract.get("environment_receipt_sha256"), "environment_receipt_sha256"
)
receipt_observed_sha = digest(receipt_path)
if receipt_observed_sha != receipt_expected_sha:
    raise SystemExit("environment receipt SHA-256 mismatch")

receipt = load(receipt_path, "environment receipt")
if receipt.get("attempt_id") != attempt_root.name:
    raise SystemExit("environment receipt attempt_id/root mismatch")
if receipt.get("exit_code") != 0 or receipt.get("failure_codes") != []:
    raise SystemExit("environment receipt is not successful")
if receipt.get("failure_stage") is not None:
    raise SystemExit("environment receipt contains a failure stage")
if receipt.get("compatibility_activation") != "EXPLICIT_PROCESS_LOCAL_ONLY":
    raise SystemExit("environment compatibility activation is not process-local")

expected_status = contract.get("expected_status")
expected_formal = contract.get("expected_formal_g1")
if type(expected_formal) is not bool or not isinstance(expected_status, str):
    raise SystemExit("contract expected_status/expected_formal_g1 are invalid")
if receipt.get("status") != expected_status or receipt.get("formal_g1") is not expected_formal:
    raise SystemExit("contract and receipt status/formal_g1 binding mismatch")
if stage_class == "ENGINEERING":
    if receipt.get("schema_version") != "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4":
        raise SystemExit("ENGINEERING requires the V4 engineering receipt schema")
    if expected_status != "ENGINEERING_COMPATIBILITY_ONLY" or expected_formal is not False:
        raise SystemExit("ENGINEERING stage contract has unsafe expected status")
    if receipt.get("environment_contract_revision_required") is not True:
        raise SystemExit("engineering receipt must require a contract revision")
else:
    if receipt.get("schema_version") != "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1":
        raise SystemExit("FORMAL requires the formal G1 receipt schema")
    if receipt.get("formal_g1") is not True:
        raise SystemExit("FORMAL stage is blocked because formal_g1 is not true")
    if receipt.get("status") != "G1_PASS":
        raise SystemExit("FORMAL stage requires receipt status G1_PASS")
    revision = receipt.get("environment_contract_revision")
    if not isinstance(revision, str) or formal_revision_pattern.fullmatch(revision) is None:
        raise SystemExit("FORMAL receipt has an invalid environment contract revision")
    if receipt.get("environment_contract_revision_required") is not False:
        raise SystemExit("FORMAL receipt must not require another contract revision")

expected_inventory = contract.get("expected_inventory")
if not isinstance(expected_inventory, dict):
    raise SystemExit("missing expected_inventory contract")
required_expected = {
    "os_id", "os_version_id", "machine", "gpu", "torch", "torch_cuda",
    "compute_capability", "bf16_supported",
}
if set(expected_inventory) != required_expected:
    raise SystemExit("expected_inventory keys do not match the frozen schema")
if expected_inventory.get("torch") != "2.8.0+cu128":
    raise SystemExit("frozen contract requires torch 2.8.0+cu128")
if expected_inventory.get("torch_cuda") != "12.8":
    raise SystemExit("frozen contract requires Torch CUDA 12.8")
if expected_inventory.get("compute_capability") != [12, 0]:
    raise SystemExit("frozen contract requires SM120")
if expected_inventory.get("bf16_supported") is not True:
    raise SystemExit("frozen contract requires BF16")

official = receipt.get("official_contract")
expected_official = {
    "boltzgen": "0.3.2",
    "cuequivariance": "0.6.1",
    "torch": "2.8.0+cu128",
    "torch_cuda": "12.8",
    "triton": "3.4.0",
}
if official != expected_official:
    raise SystemExit("environment receipt official_contract mismatch")

fixed_paths = {
    "outputs_manifest": attempt_root / "outputs.SHA256SUMS",
    "recursive_payload_manifest": attempt_root / "recursive_payload.SHA256SUMS",
    "wheelhouse_manifest": attempt_root / "wheelhouse.SHA256SUMS",
    "source_distributions_manifest": attempt_root / "source_distributions.SHA256SUMS",
    "production_freeze": attempt_root / "pip_freeze.production.txt",
    "clean_rebuild_freeze": attempt_root / "pip_freeze.clean_rebuild.txt",
    "gpu_inventory": attempt_root / "gpu_inventory.json",
    "pip_check.production.before_smoke": attempt_root / "pip_check.production.before_smoke.txt",
    "pip_check.production.after_smoke": attempt_root / "pip_check.production.after_smoke.txt",
    "pip_check.clean_rebuild.before_smoke": attempt_root / "pip_check.clean_rebuild.before_smoke.txt",
    "pip_check.clean_rebuild.after_smoke": attempt_root / "pip_check.clean_rebuild.after_smoke.txt",
}
bindings = contract.get("artifact_bindings")
if not isinstance(bindings, dict) or set(bindings) != set(fixed_paths):
    raise SystemExit("artifact_bindings do not match the required production set")

receipt_artifacts = receipt.get("artifact_sha256")
if not isinstance(receipt_artifacts, dict):
    raise SystemExit("environment receipt lacks artifact_sha256")
bound = {}
for label, fixed_path in fixed_paths.items():
    entry = bindings.get(label)
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        raise SystemExit(f"invalid artifact binding: {label}")
    path = absolute_path(entry.get("path"), f"artifact {label}")
    if path != fixed_path:
        raise SystemExit(f"artifact binding path mismatch: {label}")
    expected_sha = required_sha(entry.get("sha256"), f"artifact {label} sha256")
    observed_sha = digest(path)
    if observed_sha != expected_sha:
        raise SystemExit(f"artifact binding SHA-256 mismatch: {label}")
    if label == "outputs_manifest":
        receipt_sha = receipt.get("outputs_manifest_sha256")
    elif label == "recursive_payload_manifest":
        receipt_sha = receipt.get("recursive_payload_manifest_sha256")
    else:
        receipt_sha = receipt_artifacts.get(path.name)
    if receipt_sha != expected_sha:
        raise SystemExit(f"receipt/artifact binding SHA-256 mismatch: {label}")
    bound[label] = {"path": str(path), "sha256": expected_sha}

if stage_class == "FORMAL":
    recursive_binding = bound["recursive_payload_manifest"]
    if receipt.get("environment_manifest_sha256") != recursive_binding["sha256"]:
        raise SystemExit("FORMAL receipt does not bind the recursive environment manifest")

environment_root = attempt_root / "env"
environment_python = environment_root / "bin" / "python"
environment_launcher = environment_root / "bin" / "boltzgen-wsl-sm120"
if environment_root.is_symlink() or not environment_root.is_dir():
    raise SystemExit("bound environment directory is missing or unsafe")
# Standard venv entry points are symlinks by design.  The lexical path must stay
# under the receipt-bound env, while its resolved target must be a regular,
# executable interpreter.
try:
    resolved_environment_python = environment_python.resolve(strict=True)
except OSError as exc:
    raise SystemExit(f"bound environment Python cannot be resolved: {exc}")
if not resolved_environment_python.is_file() or not os.access(environment_python, os.X_OK):
    raise SystemExit("bound environment Python is missing or non-executable")
if (
    environment_launcher.is_symlink()
    or not environment_launcher.is_file()
    or not os.access(environment_launcher, os.X_OK)
):
    raise SystemExit("bound compatibility launcher is missing or unsafe")

payload = {
    "schema_version": "WSL2_GPU_STAGE_CONTRACT_BINDING_V1",
    "stage_id": stage_id,
    "stage_class": stage_class,
    "executor_uid": executor_uid,
    "contract_id": contract_id,
    "contract_path": str(contract_path.resolve()),
    "contract_sha256": digest(contract_path),
    "attempt_root": str(attempt_root),
    "environment_root": str(environment_root),
    "environment_python": str(environment_python),
    "environment_launcher": str(environment_launcher),
    "environment_launcher_sha256": digest(environment_launcher),
    "receipt_path": str(receipt_path),
    "receipt_sha256": receipt_observed_sha,
    "environment_status": receipt["status"],
    "formal_g1": receipt["formal_g1"],
    "expected_inventory": expected_inventory,
    "artifact_bindings": bound,
    "runtime_scripts_manifest_path": str(runtime_manifest_path),
    "runtime_scripts_manifest_sha256": digest(runtime_manifest_path),
    "runtime_script_sha256": runtime_script_sha256,
}
output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

mapfile -t BINDING_VALUES < <(
  python3 -I -S - "$AUDIT_TMP/contract_binding.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in ("attempt_root", "environment_python", "environment_launcher", "environment_status"):
    print(payload[key])
print("true" if payload["formal_g1"] else "false")
PY
)
test "${#BINDING_VALUES[@]}" -eq 5 || die "could not read validated contract binding" 70
ATTEMPT_ROOT=${BINDING_VALUES[0]}
ENV_PYTHON=${BINDING_VALUES[1]}
ENV_LAUNCHER=${BINDING_VALUES[2]}
ENVIRONMENT_STATUS=${BINDING_VALUES[3]}
FORMAL_G1_TEXT=${BINDING_VALUES[4]}

# Recompute every bound manifest. Only successful checks are summarized in the audit.
( cd "$ATTEMPT_ROOT" && sha256sum --strict -c outputs.SHA256SUMS >/dev/null )
( cd "$ATTEMPT_ROOT" && sha256sum --strict -c recursive_payload.SHA256SUMS >/dev/null )
( cd "$ATTEMPT_ROOT/wheelhouse" && sha256sum --strict -c ../wheelhouse.SHA256SUMS >/dev/null )
( cd "$ATTEMPT_ROOT/source_distributions" && \
  sha256sum --strict -c ../source_distributions.SHA256SUMS >/dev/null )

cmp "$ATTEMPT_ROOT/pip_freeze.production.txt" \
    "$ATTEMPT_ROOT/pip_freeze.clean_rebuild.txt" >/dev/null
"$ENV_PYTHON" -I -m pip freeze --all | LC_ALL=C sort > "$AUDIT_TMP/pip_freeze.live.txt"
cmp "$ATTEMPT_ROOT/pip_freeze.production.txt" "$AUDIT_TMP/pip_freeze.live.txt" >/dev/null
"$ENV_PYTHON" -I -m pip check > "$AUDIT_TMP/pip_check.live.txt"
for recorded_check in \
  "$ATTEMPT_ROOT/pip_check.production.before_smoke.txt" \
  "$ATTEMPT_ROOT/pip_check.production.after_smoke.txt" \
  "$ATTEMPT_ROOT/pip_check.clean_rebuild.before_smoke.txt" \
  "$ATTEMPT_ROOT/pip_check.clean_rebuild.after_smoke.txt"; do
  cmp "$recorded_check" "$AUDIT_TMP/pip_check.live.txt" >/dev/null
done

DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader \
  | head -n 1 | tr -d '[:space:]')
test -n "$DRIVER_VERSION" || die "nvidia-smi returned no driver version" 70
"$ENV_PYTHON" -I - "$DRIVER_VERSION" "$AUDIT_TMP/live_inventory.json" <<'PY'
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

from wsl_blackwell_nvml_compat import activate, get_state

compatibility_state = activate()
if compatibility_state != get_state() or compatibility_state.get("active") is not True:
    raise SystemExit("process-local WSL compatibility activation failed")
if compatibility_state.get("activation_scope") != "CURRENT_PROCESS_ONLY":
    raise SystemExit("unexpected compatibility activation scope")
import torch

if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
    raise SystemExit("live environment must expose exactly one CUDA GPU")
os_release = {}
for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        os_release[key] = value.strip('"')
payload = {
    "schema_version": "WSL2_GPU_STAGE_LIVE_INVENTORY_V1",
    "os_id": os_release.get("ID"),
    "os_version_id": os_release.get("VERSION_ID"),
    "machine": platform.machine(),
    "python": platform.python_version(),
    "python_prefix": sys.prefix,
    "kernel_release": platform.release(),
    "gpu": torch.cuda.get_device_name(0),
    "driver_version": sys.argv[1],
    "torch": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "compute_capability": list(torch.cuda.get_device_capability(0)),
    "multiprocessor_count": torch.cuda.get_device_properties(0).multi_processor_count,
    "torch_arch_list": torch.cuda.get_arch_list(),
    "bf16_supported": torch.cuda.is_bf16_supported(),
    "cuda_available": torch.cuda.is_available(),
    "device_count": torch.cuda.device_count(),
    "boltzgen": importlib.metadata.version("boltzgen"),
    "cuequivariance": importlib.metadata.version("cuequivariance"),
    "triton": importlib.metadata.version("triton"),
    "wsl_blackwell_nvml_compat": importlib.metadata.version("wsl-blackwell-nvml-compat"),
    "compatibility_state": compatibility_state,
}
Path(sys.argv[2]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

python3 -I -S - \
  "$AUDIT_TMP/contract_binding.json" \
  "$ATTEMPT_ROOT/gpu_inventory.json" \
  "$AUDIT_TMP/live_inventory.json" \
  "$AUDIT_TMP/pip_freeze.live.txt" \
  "$AUDIT_TMP/pip_check.live.txt" \
  "$AUDIT_TMP/verification.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

binding_path, recorded_path, live_path, freeze_path, check_path, output_path = map(Path, sys.argv[1:])
binding = json.loads(binding_path.read_text(encoding="utf-8"))
recorded = json.loads(recorded_path.read_text(encoding="utf-8"))
live = json.loads(live_path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if recorded.get("schema_version") != "WSL2_CU128_BLACKWELL_GPU_INVENTORY_V4":
    raise SystemExit("unsupported recorded GPU inventory schema")
if recorded.get("environment_status") != binding["environment_status"]:
    raise SystemExit("inventory/receipt environment status mismatch")
if recorded.get("formal_g1") is not binding["formal_g1"]:
    raise SystemExit("inventory/receipt formal_g1 mismatch")

expected = binding["expected_inventory"]
for key, value in expected.items():
    if recorded.get(key) != value:
        raise SystemExit(f"recorded inventory/contract mismatch: {key}")

live_equal = (
    "os_id", "os_version_id", "machine", "python", "kernel_release", "gpu",
    "driver_version", "torch", "torch_cuda", "compute_capability",
    "multiprocessor_count", "torch_arch_list", "bf16_supported",
)
for key in live_equal:
    if recorded.get(key) != live.get(key):
        raise SystemExit(f"recorded/live inventory mismatch: {key}")
if live.get("cuda_available") is not True or live.get("device_count") != 1:
    raise SystemExit("live CUDA device contract failed")
if live.get("python_prefix") != binding["environment_root"]:
    raise SystemExit("live Python prefix is not the receipt-bound environment")
if live.get("compute_capability") != [12, 0] or "sm_120" not in live.get("torch_arch_list", []):
    raise SystemExit("live GPU is not usable SM120")
if live.get("bf16_supported") is not True:
    raise SystemExit("live GPU does not support BF16")
if live.get("torch") != "2.8.0+cu128" or live.get("torch_cuda") != "12.8":
    raise SystemExit("live Torch/CUDA coordinated version mismatch")
if live.get("boltzgen") != "0.3.2" or live.get("cuequivariance") != "0.6.1":
    raise SystemExit("live BoltzGen/cuEquivariance version mismatch")
if live.get("triton") != "3.4.0":
    raise SystemExit("live Triton version mismatch")
if live.get("wsl_blackwell_nvml_compat") != "0.1.0":
    raise SystemExit("live WSL compatibility package version mismatch")
compatibility_state = live.get("compatibility_state")
if not isinstance(compatibility_state, dict) or compatibility_state.get("active") is not True:
    raise SystemExit("live WSL compatibility layer is not active")
if compatibility_state.get("activation_scope") != "CURRENT_PROCESS_ONLY":
    raise SystemExit("live WSL compatibility activation scope mismatch")

payload = {
    "schema_version": "WSL2_GPU_STAGE_VERIFICATION_V1",
    "status": "PASS",
    "stage_id": binding["stage_id"],
    "stage_class": binding["stage_class"],
    "executor_uid": binding["executor_uid"],
    "contract_id": binding["contract_id"],
    "contract_sha256": binding["contract_sha256"],
    "environment_attempt_root": binding["attempt_root"],
    "environment_receipt_path": binding["receipt_path"],
    "environment_receipt_sha256": binding["receipt_sha256"],
    "environment_status": binding["environment_status"],
    "formal_g1": binding["formal_g1"],
    "artifact_sha256": {
        label: value["sha256"] for label, value in binding["artifact_bindings"].items()
    },
    "environment_launcher_path": binding["environment_launcher"],
    "environment_launcher_sha256": binding["environment_launcher_sha256"],
    "runtime_scripts_manifest_path": binding["runtime_scripts_manifest_path"],
    "runtime_scripts_manifest_sha256": binding["runtime_scripts_manifest_sha256"],
    "runtime_script_sha256": binding["runtime_script_sha256"],
    "recorded_inventory_sha256": digest(recorded_path),
    "live_inventory_sha256": digest(live_path),
    "live_freeze_sha256": digest(freeze_path),
    "live_pip_check_sha256": digest(check_path),
}
output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

cat > "$AUDIT_TMP/manifest_checks.json" <<EOF
{
  "outputs": "PASS",
  "recursive_payload": "PASS",
  "source_distributions": "PASS",
  "wheelhouse": "PASS"
}
EOF

( cd "$AUDIT_TMP"
  find . -maxdepth 1 -type f ! -name stage_environment.SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum
) > "$AUDIT_TMP/stage_environment.SHA256SUMS"
( cd "$AUDIT_TMP" && sha256sum --strict -c stage_environment.SHA256SUMS >/dev/null )

if [ -e "$AUDIT_FINAL" ] || [ -L "$AUDIT_FINAL" ]; then
  diff -qr --no-dereference "$AUDIT_TMP" "$AUDIT_FINAL" >/dev/null || \
    die "existing stage audit differs; refusing overwrite" 73
  cleanup
  AUDIT_TMP=""
  trap - EXIT HUP INT TERM
  printf '%s\n' "$AUDIT_FINAL"
  return 0 2>/dev/null || exit 0
fi

find "$AUDIT_TMP" -type f -exec chmod 0400 {} +
chmod 0500 "$AUDIT_TMP"
if python3 -I -S - "$AUDIT_TMP" "$AUDIT_FINAL" <<'PY'
import os
import sys

os.rename(sys.argv[1], sys.argv[2])
PY
then
  AUDIT_TMP=""
else
  if [ -d "$AUDIT_FINAL" ] && [ ! -L "$AUDIT_FINAL" ] && \
     [ -f "$AUDIT_FINAL/stage_environment.SHA256SUMS" ] && \
     ( cd "$AUDIT_FINAL" && sha256sum --strict -c stage_environment.SHA256SUMS >/dev/null ) && \
     diff -qr --no-dereference "$AUDIT_TMP" "$AUDIT_FINAL" >/dev/null; then
    cleanup
    AUDIT_TMP=""
  else
    die "atomic stage-audit publication collided with a different result" 73
  fi
fi
trap - EXIT HUP INT TERM
( cd "$AUDIT_FINAL" && sha256sum --strict -c stage_environment.SHA256SUMS >/dev/null )
printf '%s\n' "$AUDIT_FINAL"
return 0 2>/dev/null || exit 0
