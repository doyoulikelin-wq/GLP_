#!/usr/bin/env bash
# Lightweight Windows-owner T7 replacement. No Mac receipts or environment signing.
set -euo pipefail
umask 077

workspace_input="${1:?usage: verify_local_env.sh WORKSPACE_ROOT [PYTHON_BIN]}"
python_input="${2:-}"

workspace_root="$(realpath "$workspace_input")"
test "$workspace_root" != "/" && test -d "$workspace_root/GLP_"
case "$workspace_root" in
  /home/*) ;;
  *) printf 'workspace must be under WSL /home: %s\n' "$workspace_root" >&2; exit 64 ;;
esac

for command_name in nvidia-smi python3 sha256sum df find sort stat; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command: %s\n' "$command_name" >&2
    exit 69
  }
done

if [ -n "$python_input" ]; then
  python_bin="$(realpath "$python_input")"
else
  if [ -d "$workspace_root/gpu_work/environments" ]; then
    python_bin="$(
      find "$workspace_root/gpu_work/environments" -path '*/attempt_*/env/bin/python' \
        \( -type f -o -type l \) 2>/dev/null | sort -V | tail -1
    )"
  else
    python_bin=""
  fi
fi
test -n "$python_bin" && test -x "$python_bin" || {
  printf 'cannot find a runnable T2 production Python; pass it as argument 2\n' >&2
  exit 66
}

attempt_stamp="$(date -u +'%Y%m%dT%H%M%SZ')"
attempt_root="$workspace_root/gpu_work/owner_mode/local_env_acceptance/$attempt_stamp"
test ! -e "$attempt_root"
mkdir -p "$attempt_root"
printf '%q ' "$0" "$@" > "$attempt_root/command.txt"
printf '\n' >> "$attempt_root/command.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/started_at_utc.txt"

exec 3>&1 4>&2
exec >"$attempt_root/stdout.log" 2>"$attempt_root/stderr.log"

finalize() {
  local exit_code="$?"
  trap - EXIT INT TERM
  exec 1>&3 2>&4
  printf '%s\n' "$exit_code" > "$attempt_root/exit_code.txt"
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/ended_at_utc.txt"
  if [ "$exit_code" -eq 0 ]; then
    printf 'LOCAL_ENV_READY\n' > "$attempt_root/STATUS.txt"
  else
    printf 'LOCAL_ENV_NOT_READY\n' > "$attempt_root/STATUS.txt"
  fi
  python3 -I - "$attempt_root" "$exit_code" "$python_bin" <<'PY'
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
exit_code = int(sys.argv[2])
files = {}
for path in sorted(root.iterdir()):
    if path.is_file() and path.name not in {"LOCAL_ENV_ACCEPTANCE.json", "SHA256SUMS"}:
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
payload = {
    "schema_version": "WINDOWS_OWNER_LOCAL_ENV_ACCEPTANCE_V1",
    "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "status": "LOCAL_ENV_READY" if exit_code == 0 else "LOCAL_ENV_NOT_READY",
    "exit_code": exit_code,
    "python_bin": sys.argv[3],
    "mac_review_required": False,
    "environment_contract_required": False,
    "artifacts": files,
}
(root / "LOCAL_ENV_ACCEPTANCE.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  (
    cd "$attempt_root"
    find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 \
      | sort -z | xargs -0 sha256sum > SHA256SUMS
  )
  printf '%s path=%s\n' \
    "$([ "$exit_code" -eq 0 ] && printf LOCAL_ENV_READY || printf LOCAL_ENV_NOT_READY)" \
    "$attempt_root"
  exec 3>&- 4>&-
  exit "$exit_code"
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free,temperature.gpu \
  --format=csv,noheader,nounits > "$attempt_root/nvidia_smi.csv"
test "$(wc -l < "$attempt_root/nvidia_smi.csv" | tr -d ' ')" = "1"
df -h "$workspace_root" > "$attempt_root/disk_free.txt"
"$python_bin" -m pip check > "$attempt_root/pip_check.txt"
"$python_bin" -m pip freeze --all | LC_ALL=C sort > "$attempt_root/pip_freeze.txt"

"$python_bin" -I - "$attempt_root/gpu_kernel_smoke.json" <<'PY'
import importlib.metadata
import json
import platform
import sys

from wsl_blackwell_nvml_compat import activate, get_state

activation_state = activate()
assert activation_state["active"] is True
assert activation_state["activation_scope"] == "CURRENT_PROCESS_ONLY"

import torch
from boltzgen.model.layers.triangular import TriangleMultiplicationOutgoing

assert torch.cuda.is_available(), "CUDA is not available"
assert torch.cuda.device_count() == 1, torch.cuda.device_count()
assert torch.cuda.is_bf16_supported(), "BF16 is not supported"
versions = {}
for package in (
    "boltzgen",
    "cuequivariance",
    "cuequivariance-torch",
    "cuequivariance-ops-cu12",
    "cuequivariance-ops-torch-cu12",
):
    versions[package] = importlib.metadata.version(package)

torch.manual_seed(0)
module = TriangleMultiplicationOutgoing(dim=128).cuda().to(torch.bfloat16)
x = torch.randn(1, 32, 32, 128, device="cuda", dtype=torch.bfloat16, requires_grad=True)
mask = torch.ones(1, 32, 32, device="cuda", dtype=torch.bfloat16)
y = module(x, mask, use_kernels=True)
assert y.shape == x.shape and torch.isfinite(y).all()
y.float().square().mean().backward()
assert x.grad is not None and torch.isfinite(x.grad).all()
torch.cuda.synchronize()
compatibility_state = get_state()
assert compatibility_state["active"] is True

payload = {
    "status": "LOCAL_GPU_NATIVE_KERNEL_PASS",
    "python": platform.python_version(),
    "torch": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "gpu": torch.cuda.get_device_name(0),
    "compute_capability": list(torch.cuda.get_device_capability(0)),
    "bf16_supported": torch.cuda.is_bf16_supported(),
    "packages": versions,
    "wsl_blackwell_nvml_compat": importlib.metadata.version(
        "wsl-blackwell-nvml-compat"
    ),
    "compatibility_state": compatibility_state,
}
open(sys.argv[1], "w", encoding="utf-8").write(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
)
PY

runtime_root="$workspace_root/boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
test -f "$runtime_root/SHA256SUMS"
cp "$runtime_root/SHA256SUMS" "$attempt_root/runtime_expected_sha256.txt"
( cd "$runtime_root" && sha256sum -c SHA256SUMS ) \
  > "$attempt_root/runtime_sha256_check.txt"
printf 'expected_sha256\tsize_bytes\trelative_path\n' \
  > "$attempt_root/runtime_assets.tsv"
while IFS= read -r row; do
  expected_sha256="${row%% *}"
  relative="${row#*  }"
  test -f "$runtime_root/$relative" || {
    printf 'missing runtime asset: %s\n' "$relative" >&2
    exit 66
  }
  printf '%s\t%s\t%s\n' \
    "$expected_sha256" "$(stat -c '%s' "$runtime_root/$relative")" "$relative" \
    >> "$attempt_root/runtime_assets.tsv"
done < "$runtime_root/SHA256SUMS"
