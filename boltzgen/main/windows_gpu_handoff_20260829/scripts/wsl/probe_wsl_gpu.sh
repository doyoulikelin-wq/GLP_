#!/usr/bin/env bash
# Record a fail-closed WSL2/NVIDIA engineering probe without running BoltzGen.
set -euo pipefail
umask 077

work_input="${1:?usage: probe_wsl_gpu.sh WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT}"
attempt_id="${2:?usage: probe_wsl_gpu.sh WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT}"
t0_receipt="${3:?usage: probe_wsl_gpu.sh WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT}"
windows_receipt="${4:?usage: probe_wsl_gpu.sh WORK_ROOT ATTEMPT_ID T0_RECEIPT WINDOWS_RECEIPT}"
[[ "$attempt_id" =~ ^attempt_[0-9]{3}$ ]] || {
  printf 'invalid attempt ID: %s\n' "$attempt_id" >&2
  exit 64
}

for command_name in nvidia-smi python3 sha256sum lscpu free df uname realpath; do
  command -v "$command_name" >/dev/null || {
    printf 'BLOCKED_MISSING_COMMAND: %s\n' "$command_name" >&2
    exit 69
  }
done
work_root="$(python3 -I - "$work_input" <<'PY'
import sys
from pathlib import Path

raw = Path(sys.argv[1])
if not raw.is_absolute() or ".." in raw.parts:
    raise SystemExit("WORK_ROOT must be an absolute normalized path")
if raw.exists() and raw.is_symlink():
    raise SystemExit("WORK_ROOT must not be a symlink")
resolved = raw.resolve(strict=True) if raw.exists() else raw.parent.resolve(strict=True) / raw.name
home = Path("/home").resolve(strict=True)
try:
    relative = resolved.relative_to(home)
except ValueError as exc:
    raise SystemExit("WORK_ROOT resolves outside /home") from exc
if len(relative.parts) < 2:
    raise SystemExit("WORK_ROOT must be below /home/<user>")
print(resolved)
PY
)"

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
ensure_subdirectory "$work_root/runs"
stage_root="$work_root/runs/wsl_gpu_probe"
ensure_subdirectory "$stage_root"
attempt_root="$stage_root/$attempt_id"
mkdir "$attempt_root" || {
  printf 'attempt already exists: %s\n' "$attempt_root" >&2
  exit 73
}
chmod 0750 "$attempt_root"
stage_complete=0

finalize() {
  local exit_code="$?"
  trap - EXIT INT TERM
  set +e
  if [ "$exit_code" -eq 0 ] && [ "$stage_complete" -eq 1 ]; then
    printf 'ENGINEERING_GPU_PROBE_PASS\n' > "$attempt_root/STATUS.txt" || exit 75
  else
    if [ "$exit_code" -eq 0 ]; then
      exit_code=75
    fi
    printf 'ENGINEERING_GPU_PROBE_FAIL\n' > "$attempt_root/STATUS.txt" || exit 75
  fi
  printf '%s\n' "$exit_code" > "$attempt_root/exit_code.txt" || exit 75
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/ended_at_utc.txt" || exit 75
  if [ "$exit_code" -eq 0 ]; then
    : > "$attempt_root/failure_codes.txt"
  else
    printf 'BLOCKED_ENGINEERING_GPU_PROBE_EXIT_%s\n' "$exit_code" \
      > "$attempt_root/failure_codes.txt"
  fi
  (
    cd "$attempt_root" || exit 1
    find . -type f \
      ! -name 'outputs.SHA256SUMS' \
      ! -name 'receipt.json' \
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
payload = {
    "schema_version": "WSL2_GPU_ENGINEERING_PROBE_RECEIPT_V1",
    "attempt_id": root.name,
    "exit_code": exit_code,
    "formal_g1": False,
    "status": "ENGINEERING_GPU_PROBE_PASS" if exit_code == 0 else "ENGINEERING_GPU_PROBE_FAIL",
    "outputs_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "t0_receipt_sha256": chain.get("t0_receipt_sha256"),
    "windows_receipt_sha256": chain.get("windows_receipt_sha256"),
}
(root / "receipt.json").write_text(
    json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
PY
  test "$?" -eq 0 || exit 75
  chmod -R a-w "$attempt_root" || exit 75
  exit "$exit_code"
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Direct files avoid a process-substitution race between tee and the final hash pass.
exec >"$attempt_root/stdout.log" 2>"$attempt_root/stderr.log"

grep -Eqi '(microsoft-standard-WSL2|WSL2)' /proc/sys/kernel/osrelease || {
  printf 'BLOCKED_NOT_WSL2\n' >&2
  exit 65
}
test "$(cat /proc/1/comm)" = "systemd" || {
  printf 'BLOCKED_SYSTEMD_NOT_PID1\n' >&2
  exit 65
}

printf '%q ' "$0" "$@" > "$attempt_root/command.txt"
printf '\n' >> "$attempt_root/command.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/started_at_utc.txt"
receipt_validator="$(realpath "$(dirname "$0")/../validate_engineering_receipt_chain.py")"
handoff_root="$(realpath "$(dirname "$0")/../..")"
python3 -I "$receipt_validator" \
  --stage t1 \
  --t0-receipt "$t0_receipt" \
  --windows-receipt "$windows_receipt" \
  --handoff-root "$handoff_root" \
  --output "$attempt_root/predecessor_receipts.json"
uname -a > "$attempt_root/uname.txt"
cp /etc/os-release "$attempt_root/os-release.txt"
cp /proc/version "$attempt_root/proc_version.txt"
lscpu > "$attempt_root/lscpu.txt"
free -h > "$attempt_root/memory.txt"
df -hT "$work_root" > "$attempt_root/disk.txt"
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used \
  --format=csv,noheader > "$attempt_root/nvidia_smi_inventory.csv"
if nvidia-smi -q > "$attempt_root/nvidia_smi_q.txt" 2> "$attempt_root/nvidia_smi_q.stderr.txt"; then
  printf 'AVAILABLE\n' > "$attempt_root/nvidia_smi_extended_status.txt"
else
  printf 'UNAVAILABLE_IN_WSL\n' > "$attempt_root/nvidia_smi_extended_status.txt"
fi
if nvidia-smi --query-gpu=temperature.gpu,power.limit --format=csv,noheader \
  > "$attempt_root/nvidia_smi_thermal_power.csv" \
  2> "$attempt_root/nvidia_smi_thermal_power.stderr.txt"; then
  printf 'AVAILABLE\n' > "$attempt_root/nvidia_smi_thermal_power_status.txt"
else
  printf 'UNAVAILABLE_IN_WSL\n' > "$attempt_root/nvidia_smi_thermal_power_status.txt"
fi

python3 -I - "$work_root" "$attempt_root/platform_probe.json" <<'PY'
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

work_root = Path(sys.argv[1])
output = Path(sys.argv[2])

os_release = {}
for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        os_release[key] = value.strip('"')

query = subprocess.check_output(
    [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    ],
    text=True,
).strip().splitlines()
if len(query) != 1:
    raise SystemExit(f"BLOCKED_EXPECTED_ONE_GPU: {len(query)}")
name, driver, memory_mib = [item.strip() for item in query[0].split(",")]
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", driver):
    raise SystemExit(f"BLOCKED_INVALID_DRIVER_VERSION: {driver}")
driver_parts = tuple(int(part) for part in driver.split("."))
memory_mib_int = int(memory_mib)
disk = shutil.disk_usage(work_root)
minimum_free = 80 * 1024**3
formal_minimum_free = 250 * 1024**3

blocked = []
if "RTX 5070 TI" not in name.upper():
    blocked.append("BLOCKED_GPU_NOT_RTX_5070_TI")
if "RTX 50" in name.upper() and driver_parts < (570, 65):
    blocked.append("BLOCKED_BLACKWELL_DRIVER_LT_R570_65")
if memory_mib_int < 11 * 1024:
    blocked.append("BLOCKED_GPU_MEMORY_LT_11_GIB")
if disk.free < minimum_free:
    blocked.append("BLOCKED_DISK_FREE_LT_80_GIB")

payload = {
    "schema_version": "WSL2_GPU_PLATFORM_PROBE_V1",
    "platform_class": "LINUX_NVIDIA",
    "virtualization_class": "WSL2",
    "formal_g1": False,
    "os_id": os_release.get("ID"),
    "os_version_id": os_release.get("VERSION_ID"),
    "machine": os.uname().machine,
    "gpu_name": name,
    "driver_version": driver,
    "memory_total_mib": memory_mib_int,
    "disk_free_bytes": disk.free,
    "minimum_disk_free_bytes": minimum_free,
    "formal_minimum_disk_free_bytes": formal_minimum_free,
    "formal_scratch_ready": disk.free >= formal_minimum_free,
    "systemd_pid1": Path("/proc/1/comm").read_text(encoding="utf-8").strip() == "systemd",
    "blocked_reasons": blocked,
    "status": "ENGINEERING_GPU_PROBE_PASS" if not blocked else "BLOCKED",
}
output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
if blocked:
    raise SystemExit(";".join(blocked))
PY

stage_complete=1
printf 'ENGINEERING_GPU_PROBE_PASS (not G1)\n'
