#!/usr/bin/env bash
# Build a reproducible CUDA 12.8 engineering candidate for RTX 50/Blackwell.
# This script cannot issue a formal G1 PASS; it creates evidence for contract revision.
set -euo pipefail
umask 077

workspace_input="${1:?usage: bootstrap_cu128_engineering_env.sh WORKSPACE_ROOT WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT WSL_PROBE_RECEIPT}"
work_input="${2:?usage: bootstrap_cu128_engineering_env.sh WORKSPACE_ROOT WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT WSL_PROBE_RECEIPT}"
attempt_id="${3:?usage: bootstrap_cu128_engineering_env.sh WORKSPACE_ROOT WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT WSL_PROBE_RECEIPT}"
t0_receipt="${4:?usage: bootstrap_cu128_engineering_env.sh WORKSPACE_ROOT WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT WSL_PROBE_RECEIPT}"
windows_receipt="${5:?usage: bootstrap_cu128_engineering_env.sh WORKSPACE_ROOT WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT WSL_PROBE_RECEIPT}"
wsl_probe_receipt="${6:?usage: bootstrap_cu128_engineering_env.sh WORKSPACE_ROOT WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT WSL_PROBE_RECEIPT}"

[[ "$attempt_id" =~ ^attempt_[0-9]{3}$ ]] || {
  printf 'invalid attempt ID: %s\n' "$attempt_id" >&2
  exit 64
}
for command_name in python3 realpath; do
  command -v "$command_name" >/dev/null || {
    printf 'BLOCKED_MISSING_COMMAND: %s\n' "$command_name" >&2
    exit 69
  }
done
canonical_roots_text="$(python3 -I - "$workspace_input" "$work_input" <<'PY'
import sys
from pathlib import Path

home = Path("/home").resolve(strict=True)
for index, value in enumerate(sys.argv[1:]):
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise SystemExit("workspace/work roots must be absolute normalized paths")
    if raw.exists() and raw.is_symlink():
        raise SystemExit(f"root must not be a symlink: {raw}")
    if index == 0 and (not raw.is_dir() or raw.is_symlink()):
        raise SystemExit(f"workspace root is not a regular directory: {raw}")
    resolved = raw.resolve(strict=True) if raw.exists() else raw.parent.resolve(strict=True) / raw.name
    try:
        relative = resolved.relative_to(home)
    except ValueError as exc:
        raise SystemExit(f"root resolves outside /home: {raw}") from exc
    if len(relative.parts) < 2:
        raise SystemExit(f"root must be below /home/<user>: {raw}")
    print(resolved)
PY
)"
readarray -t canonical_roots <<< "$canonical_roots_text"
workspace_root="${canonical_roots[0]}"
work_root="${canonical_roots[1]}"
grep -Eqi '(microsoft-standard-WSL2|WSL2)' /proc/sys/kernel/osrelease || {
  printf 'BLOCKED_NOT_WSL2\n' >&2
  exit 65
}
test "$(cat /proc/1/comm)" = "systemd" || {
  printf 'BLOCKED_SYSTEMD_NOT_PID1\n' >&2
  exit 65
}

if [ ! -e "$work_root" ]; then
  mkdir "$work_root"
fi
test -d "$work_root" && test ! -L "$work_root"
ensure_subdirectory() {
  local path="$1"
  if [ -L "$path" ]; then
    printf 'BLOCKED_SYMLINKED_WORK_SUBDIRECTORY: %s\n' "$path" >&2
    return 65
  fi
  if [ ! -e "$path" ]; then
    mkdir "$path"
  fi
  test -d "$path" && test ! -L "$path"
  case "$(realpath "$path")" in
    "$work_root"/*) ;;
    *) printf 'BLOCKED_WORK_SUBDIRECTORY_ESCAPE: %s\n' "$path" >&2; return 65 ;;
  esac
}
ensure_subdirectory "$work_root/environments"
stage_root="$work_root/environments/cu128_blackwell_candidate"
ensure_subdirectory "$stage_root"
attempt_root="$stage_root/$attempt_id"
mkdir "$attempt_root" || {
  printf 'attempt already exists: %s\n' "$attempt_root" >&2
  exit 73
}
chmod 0750 "$attempt_root"
stage_complete=0

stdout_log="$attempt_root/stdout.log"
stderr_log="$attempt_root/stderr.log"
exec >"$stdout_log" 2>"$stderr_log"

printf '%q ' "$0" "$@" > "$attempt_root/command.txt"
printf '\n' >> "$attempt_root/command.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/started_at_utc.txt"

finalize() {
  local exit_code="$?"
  trap - EXIT INT TERM
  set +e
  if [ "$exit_code" -eq 0 ] && [ "$stage_complete" -eq 1 ]; then
    printf 'ENGINEERING_COMPATIBILITY_ONLY\n' > "$attempt_root/STATUS.txt" || exit 75
  else
    if [ "$exit_code" -eq 0 ]; then
      exit_code=75
    fi
    printf 'ENGINEERING_ENV_BUILD_FAIL\n' > "$attempt_root/STATUS.txt" || exit 75
  fi
  printf '%s\n' "$exit_code" > "$attempt_root/exit_code.txt" || exit 75
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/ended_at_utc.txt" || exit 75
  if [ "$exit_code" -eq 0 ]; then
    : > "$attempt_root/failure_codes.txt"
  else
    printf 'BLOCKED_ENGINEERING_ENV_EXIT_%s\n' "$exit_code" \
      > "$attempt_root/failure_codes.txt"
  fi
  (
    cd "$attempt_root" || exit 1
    find . -maxdepth 1 -type f \
      ! -name 'outputs.SHA256SUMS' ! -name 'receipt.json' \
      -print0 | sort -z | xargs -0 sha256sum
  ) > "$attempt_root/outputs.SHA256SUMS" || exit 75
  python3 -I - "$attempt_root" "$exit_code" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
exit_code = int(sys.argv[2])
manifest = root / "outputs.SHA256SUMS"
chain_path = root / "predecessor_receipts.json"
chain = json.loads(chain_path.read_text(encoding="utf-8")) if chain_path.is_file() else {}
artifacts = {}
for name in (
    "requirements.resolved.in",
    "requirements.production.lock.txt",
    "requirements.boltzgen-wheel.lock.txt",
    "wheelhouse.SHA256SUMS",
    "pip_freeze.production.txt",
    "pip_freeze.clean_rebuild.txt",
    "gpu_inventory.json",
    "cuequivariance_kernel_smoke.production.txt",
    "cuequivariance_kernel_smoke.clean_rebuild.txt",
):
    path = root / name
    if path.is_file():
        artifacts[name] = hashlib.sha256(path.read_bytes()).hexdigest()
payload = {
    "schema_version": "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V1",
    "attempt_id": root.name,
    "exit_code": exit_code,
    "formal_g1": False,
    "environment_contract_revision_required": True,
    "status": "ENGINEERING_COMPATIBILITY_ONLY" if exit_code == 0 else "ENGINEERING_ENV_BUILD_FAIL",
    "artifact_sha256": artifacts,
    "outputs_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "failure_codes": [] if exit_code == 0 else [f"BLOCKED_ENGINEERING_ENV_EXIT_{exit_code}"],
    "t0_receipt_sha256": chain.get("t0_receipt_sha256"),
    "windows_receipt_sha256": chain.get("windows_receipt_sha256"),
    "wsl_probe_receipt_sha256": chain.get("wsl_probe_receipt_sha256"),
}
(root / "receipt.json").write_text(
    json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
PY
  test "$?" -eq 0 || exit 75
  find "$attempt_root" -maxdepth 1 -type f -exec chmod a-w {} + || exit 75
  exit "$exit_code"
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for command_name in python3 git nvidia-smi sha256sum uname find sort xargs; do
  command -v "$command_name" >/dev/null || {
    printf 'BLOCKED_MISSING_COMMAND: %s\n' "$command_name" >&2
    exit 69
  }
done

receipt_validator="$(realpath "$(dirname "$0")/../validate_engineering_receipt_chain.py")"
python3 -I "$receipt_validator" \
  --stage t2 \
  --t0-receipt "$t0_receipt" \
  --windows-receipt "$windows_receipt" \
  --wsl-probe-receipt "$wsl_probe_receipt" \
  --handoff-root "$workspace_root/handoff" \
  --output "$attempt_root/predecessor_receipts.json"

python3 -I - <<'PY'
import platform
import sys

if sys.version_info[:2] not in {(3, 11), (3, 12)}:
    raise SystemExit(f"BLOCKED_PYTHON_VERSION: {platform.python_version()}")
if platform.machine() != "x86_64":
    raise SystemExit(f"BLOCKED_MACHINE: {platform.machine()}")
PY

bg_src="$workspace_root/software/boltzgen"
runtime="$workspace_root/boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
test -f "$bg_src/pyproject.toml"
test -f "$runtime/SHA256SUMS"
test "$(sha256sum "$bg_src/Dockerfile" | cut -d' ' -f1)" = \
  "8ec8ea5441b95d033a8d689d758f6e971e157f02a77cf02de7b527bb550f868d"
test "$(sha256sum "$bg_src/pyproject.toml" | cut -d' ' -f1)" = \
  "f1260cddbafb6b83f31951481ccc1602120f36979dc0ffc315f89d19bd62428d"
( cd "$runtime" && sha256sum -c SHA256SUMS )
( cd "$workspace_root" && sha256sum -c \
    "$workspace_root/handoff/package_manifests/contracts/runtime_tree.SHA256SUMS" )
( cd "$workspace_root" && sha256sum -c \
    "$workspace_root/handoff/package_manifests/contracts/boltzgen_upstream_tree.SHA256SUMS" )

driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits | head -1 | tr -d ' ')"
gpu_rows="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits)"
python3 -I - "$gpu_rows" <<'PY'
import sys

rows = [line.strip() for line in sys.argv[1].splitlines() if line.strip()]
if len(rows) != 1:
    raise SystemExit(f"BLOCKED_EXPECTED_ONE_GPU: {len(rows)}")
name, memory_mib = [item.strip() for item in rows[0].rsplit(",", 1)]
if "RTX 5070 TI" not in name.upper():
    raise SystemExit(f"BLOCKED_GPU_NOT_RTX_5070_TI: {name}")
if int(memory_mib) < 11 * 1024:
    raise SystemExit(f"BLOCKED_GPU_MEMORY_LT_11_GIB: {memory_mib}")
PY
python3 -I - "$driver_version" <<'PY'
import re
import sys

value = sys.argv[1]
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
    raise SystemExit(f"BLOCKED_INVALID_DRIVER: {value}")
if tuple(int(part) for part in value.split(".")) < (570, 65):
    raise SystemExit(f"BLOCKED_BLACKWELL_DRIVER_LT_R570_65: {value}")
PY

export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
for variable_name in \
  PYTHONPATH PYTHONHOME PYTHONOPTIMIZE \
  LD_PRELOAD LD_LIBRARY_PATH CUDA_VISIBLE_DEVICES CUDA_HOME CUDA_PATH; do
  if declare -p "$variable_name" >/dev/null 2>&1; then
    printf 'BLOCKED_ENVIRONMENT_INJECTION: %s\n' "$variable_name" >&2
    exit 70
  fi
done
for variable_name in ${!PIP_@}; do
  unset "$variable_name"
done
export PIP_CONFIG_FILE=/dev/null
export PIP_INDEX_URL=https://pypi.org/simple
export PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cu128

resolver="$attempt_root/env_resolver"
production="$attempt_root/env"
rebuild="$attempt_root/env_clean_rebuild"
wheelhouse="$attempt_root/wheelhouse"

python3 -m venv "$resolver"
"$resolver/bin/python" -I -c 'import sys; sys.exit(0 if __debug__ else 70)'
"$resolver/bin/python" -m pip install --no-cache-dir --upgrade \
  --index-url "$PIP_INDEX_URL" \
  'pip==24.3.1' 'setuptools==75.6.0' 'wheel==0.45.1'
"$resolver/bin/pip" install --no-cache-dir --index-url "$PIP_INDEX_URL" \
  'pip-tools==7.4.1'
"$resolver/bin/pip" install --no-cache-dir \
  --index-url "$PIP_INDEX_URL" --extra-index-url "$PIP_EXTRA_INDEX_URL" \
  'torch==2.7.0+cu128' \
  'cuequivariance==0.5.1' \
  'cuequivariance-torch==0.5.1' \
  'cuequivariance-ops-cu12==0.5.1' \
  'cuequivariance-ops-torch-cu12==0.5.1' \
  'pytest==8.3.4' 'pyarrow==18.1.0' "$bg_src"

"$resolver/bin/pip" list --format=freeze \
  | LC_ALL=C sort \
  | grep -viE '^boltzgen([=@]|[[:space:]])' \
  > "$attempt_root/requirements.resolved.in"
"$resolver/bin/pip-compile" \
  --resolver=backtracking --generate-hashes --allow-unsafe --strip-extras \
  --index-url "$PIP_INDEX_URL" --extra-index-url "$PIP_EXTRA_INDEX_URL" \
  --output-file "$attempt_root/requirements.production.lock.txt" \
  "$attempt_root/requirements.resolved.in"

mkdir "$wheelhouse"
"$resolver/bin/pip" download --only-binary=:all: --require-hashes \
  --requirement "$attempt_root/requirements.production.lock.txt" \
  --dest "$wheelhouse"
"$resolver/bin/pip" wheel --no-deps --no-build-isolation \
  --wheel-dir "$wheelhouse" "$bg_src"

boltzgen_wheel_count="$(find "$wheelhouse" -maxdepth 1 -type f -name 'boltzgen-0.3.2-*.whl' | wc -l | tr -d ' ')"
test "$boltzgen_wheel_count" = 1
boltzgen_wheel="$(find "$wheelhouse" -maxdepth 1 -type f -name 'boltzgen-0.3.2-*.whl')"
boltzgen_wheel_sha256="$(sha256sum "$boltzgen_wheel" | cut -d' ' -f1)"
printf 'boltzgen==0.3.2 --hash=sha256:%s\n' "$boltzgen_wheel_sha256" \
  > "$attempt_root/requirements.boltzgen-wheel.lock.txt"
(
  cd "$wheelhouse"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
) > "$attempt_root/wheelhouse.SHA256SUMS"
( cd "$wheelhouse" && sha256sum -c "$attempt_root/wheelhouse.SHA256SUMS" )

for environment in "$production" "$rebuild"; do
  python3 -m venv "$environment"
  "$environment/bin/pip" install --force-reinstall --no-index --no-compile \
    --find-links "$wheelhouse" --require-hashes \
    --requirement "$attempt_root/requirements.production.lock.txt"
  "$environment/bin/pip" install --force-reinstall --no-index --no-deps --no-compile \
    --find-links "$wheelhouse" --require-hashes \
    --requirement "$attempt_root/requirements.boltzgen-wheel.lock.txt"
done

"$production/bin/pip" freeze --all | LC_ALL=C sort > "$attempt_root/pip_freeze.production.txt"
"$rebuild/bin/pip" freeze --all | LC_ALL=C sort > "$attempt_root/pip_freeze.clean_rebuild.txt"
cmp "$attempt_root/pip_freeze.production.txt" "$attempt_root/pip_freeze.clean_rebuild.txt"

for item in "production:$production" "clean_rebuild:$rebuild"; do
  label="${item%%:*}"
  environment="${item#*:}"
  "$environment/bin/pip" check > "$attempt_root/pip_check.$label.txt"
  "$environment/bin/python" -I - "$attempt_root/cuequivariance_kernel_smoke.$label.txt" <<'PY'
import importlib.metadata
import json
import sys

import torch
from boltzgen.model.layers.triangular import TriangleMultiplicationOutgoing

assert torch.__version__ == "2.7.0+cu128", torch.__version__
assert torch.version.cuda == "12.8", torch.version.cuda
assert torch.cuda.is_available() and torch.cuda.device_count() == 1
assert torch.cuda.is_bf16_supported()
for package in (
    "cuequivariance",
    "cuequivariance-torch",
    "cuequivariance-ops-cu12",
    "cuequivariance-ops-torch-cu12",
):
    assert importlib.metadata.version(package) == "0.5.1", package

torch.manual_seed(0)
module = TriangleMultiplicationOutgoing(dim=128).cuda().to(torch.bfloat16)
x = torch.randn(
    1, 32, 32, 128, device="cuda", dtype=torch.bfloat16, requires_grad=True
)
mask = torch.ones(1, 32, 32, device="cuda", dtype=torch.bfloat16)
y = module(x, mask, use_kernels=True)
assert y.shape == x.shape and torch.isfinite(y).all()
y.float().square().mean().backward()
assert x.grad is not None and torch.isfinite(x.grad).all()
torch.cuda.synchronize()
payload = {
    "status": "CUEQUIVARIANCE_NATIVE_KERNEL_SMOKE_PASS",
    "torch": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "gpu": torch.cuda.get_device_name(0),
    "compute_capability": list(torch.cuda.get_device_capability(0)),
    "bf16_supported": torch.cuda.is_bf16_supported(),
}
open(sys.argv[1], "w", encoding="utf-8").write(
    json.dumps(payload, sort_keys=True, indent=2) + "\n"
)
PY
done

"$production/bin/python" -I - "$driver_version" "$attempt_root/gpu_inventory.json" <<'PY'
import json
import os
import platform
import sys
from pathlib import Path

import torch

os_release = {}
for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        os_release[key] = value.strip('"')
payload = {
    "schema_version": "WSL2_CU128_BLACKWELL_GPU_INVENTORY_V1",
    "environment_status": "ENGINEERING_COMPATIBILITY_ONLY",
    "formal_g1": False,
    "platform_class": "LINUX_NVIDIA",
    "virtualization_class": "WSL2",
    "os_id": os_release.get("ID"),
    "os_version_id": os_release.get("VERSION_ID"),
    "python": platform.python_version(),
    "machine": platform.machine(),
    "driver_version": sys.argv[1],
    "torch": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "gpu": torch.cuda.get_device_name(0),
    "compute_capability": list(torch.cuda.get_device_capability(0)),
    "bf16_supported": torch.cuda.is_bf16_supported(),
}
Path(sys.argv[2]).write_text(
    json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
PY

stage_complete=1
