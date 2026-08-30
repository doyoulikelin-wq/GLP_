#!/usr/bin/env bash
# Trusted personal-computer entry: existing weights only, real VHH inference pipeline.
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "$0")" && pwd -P)"
overlay_root="$(cd "$script_dir/../.." && pwd -P)"

if [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: bash start_personal_vhh_inference.sh

Loads the existing BoltzGen weights and runs a fixed 7XL0 VHH generation and
screening smoke test. The base package and its .TRANSFER.SHA256 sidecar must be
siblings of the personal inference overlay.
EOF
  exit 0
fi
test "$#" -eq 0 || {
  printf 'this fixed personal inference entry accepts no arguments\n' >&2
  exit 64
}

for command_name in bash python3 sha256sum realpath tee flock; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command: %s\n' "$command_name" >&2
    exit 69
  }
done

personal_root="$HOME/boltzgen_personal"
mkdir -p "$personal_root/logs" "$personal_root/evidence"
run_id="run_$(date -u +'%Y%m%dT%H%M%SZ')_${RANDOM}_$$"
log_root="$personal_root/logs/$run_id"
mkdir "$log_root"
chmod 0700 "$log_root"
quick_log="$log_root/quickstart.log"
current_stage="START"
success=0
env_stage=""
env_root=""
env_marker=""
new_env=0
gpu_monitor_pid=""
workspace_root=""
weights_used_manifest=""
inference_root=""
inference_started=0
weights_final_checked=0
exec > >(tee -a "$quick_log") 2>&1

finalize() {
  local exit_code="$?"
  trap - EXIT INT TERM
  set +e
  if [ -n "$gpu_monitor_pid" ]; then
    kill "$gpu_monitor_pid" >/dev/null 2>&1
    wait "$gpu_monitor_pid" >/dev/null 2>&1
  fi
  if [ "$inference_started" -eq 1 ] && [ "$weights_final_checked" -eq 0 ] && \
      [ -n "$workspace_root" ] && [ -n "$weights_used_manifest" ] && \
      [ -f "$weights_used_manifest" ]; then
    (
      cd "$workspace_root" || exit 1
      sha256sum -c "$weights_used_manifest"
    ) > "$log_root/weights_failure_path_recheck.log" 2>&1
    printf '%s\n' "$?" > "$log_root/weights_failure_path_recheck_exit_code.txt"
  fi
  if { [ "$exit_code" -ne 0 ] || [ "$success" -ne 1 ]; } && \
      [ -n "$inference_root" ] && [ -d "$inference_root" ]; then
    for result_name in RESULT_SUMMARY.json RESULT_SUMMARY_ZH.txt RESULT_COMMITTED.json; do
      if [ -e "$inference_root/$result_name" ] || [ -L "$inference_root/$result_name" ]; then
        mv -f -- "$inference_root/$result_name" \
          "$inference_root/${result_name}.INVALID_${run_id}"
      fi
    done
  fi
  if { [ "$current_stage" = "PREPARE_CUDA128_INFERENCE_ENVIRONMENT" ] || \
       [ "$current_stage" = "CUDA_NATIVE_KERNEL_SMOKE" ]; } && \
      [ "$new_env" -eq 0 ] && [ -n "$env_marker" ] && \
      { [ -e "$env_marker" ] || [ -L "$env_marker" ]; }; then
    mv -f -- "$env_marker" "${env_marker}.FAILED_${run_id}"
  fi
  if [ -n "$env_stage" ] && [ -d "$env_stage" ]; then
    rm -rf -- "$env_stage"
  fi
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$log_root/ended_at_utc.txt"
  printf '%s\n' "$exit_code" > "$log_root/exit_code.txt"
  printf '%s\n' "$current_stage" > "$log_root/last_stage.txt"
  if [ "$exit_code" -eq 0 ] && [ "$success" -eq 1 ]; then
    printf 'PERSONAL_VHH_SMOKE_PASS_NOT_G1_NOT_G2_NOT_AIV1\n' > "$log_root/STATUS.txt"
  else
    printf 'PERSONAL_VHH_SMOKE_FAILED\n' > "$log_root/STATUS.txt"
    if [ "$exit_code" -eq 0 ]; then
      exit_code=75
      printf '%s\n' "$exit_code" > "$log_root/exit_code.txt"
    fi
  fi
  printf '\nstatus=%s stage=%s log=%s\n' "$(cat "$log_root/STATUS.txt")" "$current_stage" "$log_root"
  exit "$exit_code"
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '使用既有模型权重，只进行 VHH 推理、候选生成和筛选；权重文件保持只读。\n'
printf 'run_id=%s\noverlay=%s\n' "$run_id" "$overlay_root"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$log_root/started_at_utc.txt"
printf '%q ' "$0" > "$log_root/command.txt"
printf '\n' >> "$log_root/command.txt"

current_stage="SINGLE_INSTANCE_LOCK"
exec 9> "$personal_root/personal_inference.lock"
flock -n 9 || {
  printf '已有一个个人 VHH 推理任务正在运行；本次没有并发启动。\n' >&2
  exit 73
}

current_stage="READ_BASE_BINDING"
binding_path="$overlay_root/BASE_BINDING.json"
test -f "$binding_path" && test ! -L "$binding_path" || {
  printf 'missing BASE_BINDING.json in overlay: %s\n' "$overlay_root" >&2
  exit 66
}
binding_text="$(python3 -I - "$binding_path" <<'PY'
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
required = {
    "base_package_name",
    "base_transfer_sha256",
    "base_payload_manifest_sha256",
    "base_package_receipt_sha256",
    "base_verifier_sha256",
    "base_upstream_manifest_sha256",
    "base_active_manifest_sha256",
    "base_runtime_manifest_sha256",
}
missing = sorted(required - payload.keys())
if missing:
    raise SystemExit(f"BASE_BINDING missing fields: {missing}")
if payload.get("schema_version") != "PERSONAL_INFERENCE_OVERLAY_BASE_BINDING_V1":
    raise SystemExit("unexpected BASE_BINDING schema_version")
if payload.get("base_package_name") != "WINDOWS_CODEX_GPU_HANDOFF_20260829_V1":
    raise SystemExit("unexpected base package name in BASE_BINDING")
for name in (
    "base_transfer_sha256",
    "base_payload_manifest_sha256",
    "base_package_receipt_sha256",
    "base_verifier_sha256",
    "base_upstream_manifest_sha256",
    "base_active_manifest_sha256",
    "base_runtime_manifest_sha256",
):
    if not re.fullmatch(r"[0-9a-f]{64}", payload[name]):
        raise SystemExit(f"invalid {name}")
if payload.get("standalone") is not False or payload.get("base_assets_copied") is not False:
    raise SystemExit("invalid overlay/base relationship")
if payload.get("operation") != "EXISTING_WEIGHT_VHH_INFERENCE_GENERATION_SCREENING":
    raise SystemExit("unexpected operation in BASE_BINDING")
print(payload["base_package_name"])
print(payload["base_transfer_sha256"])
print(payload["base_payload_manifest_sha256"])
print(payload["base_package_receipt_sha256"])
print(payload["base_verifier_sha256"])
print(payload["base_upstream_manifest_sha256"])
print(payload["base_active_manifest_sha256"])
print(payload["base_runtime_manifest_sha256"])
PY
)"
readarray -t binding_fields <<< "$binding_text"
base_name="${binding_fields[0]}"
base_transfer_sha256="${binding_fields[1]}"
base_payload_manifest_sha256="${binding_fields[2]}"
base_receipt_sha256="${binding_fields[3]}"
base_verifier_sha256="${binding_fields[4]}"
base_upstream_manifest_sha256="${binding_fields[5]}"
base_active_manifest_sha256="${binding_fields[6]}"
base_runtime_manifest_sha256="${binding_fields[7]}"
test -d "$overlay_root/../$base_name" && test ! -L "$overlay_root/../$base_name" || {
  printf 'base package must be beside overlay: %s\n' "$overlay_root/../$base_name" >&2
  exit 66
}
base_root="$(realpath "$overlay_root/../$base_name")"
test -d "$base_root" && test ! -L "$base_root"
printf 'base=%s\n' "$base_root"

current_stage="VERIFY_WSL2_UBUNTU"
grep -Eqi '(microsoft-standard-WSL2|WSL2)' /proc/sys/kernel/osrelease || {
  printf 'this entry must run inside WSL2 Ubuntu\n' >&2
  exit 65
}
python3 -I - <<'PY'
import platform
from pathlib import Path

values = {}
for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value.strip('"')
if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "24.04":
    raise SystemExit(
        f"Ubuntu 24.04 is required; observed {values.get('ID')} {values.get('VERSION_ID')}"
    )
if platform.machine() != "x86_64":
    raise SystemExit(f"x86_64 WSL2 is required; observed {platform.machine()}")
PY

current_stage="VERIFY_OVERLAY_AND_BASE_BINDING"
test -f "${overlay_root}.TRANSFER.SHA256" && \
  test ! -L "${overlay_root}.TRANSFER.SHA256" || {
  printf 'missing overlay transfer sidecar: %s\n' "${overlay_root}.TRANSFER.SHA256" >&2
  exit 66
}
(
  cd "$(dirname "$overlay_root")"
  sha256sum -c "$(basename "${overlay_root}.TRANSFER.SHA256")"
)
(
  cd "$overlay_root"
  sha256sum -c OVERLAY_TRANSFER.SHA256SUMS
  sha256sum -c OVERLAY_PAYLOAD.SHA256SUMS
)
test "$(sha256sum "$base_root/TRANSFER.SHA256SUMS" | awk '{print $1}')" = \
  "$base_transfer_sha256"
test "$(sha256sum "$base_root/PAYLOAD.SHA256SUMS" | awk '{print $1}')" = \
  "$base_payload_manifest_sha256"
test "$(sha256sum "$base_root/PACKAGE_RECEIPT.json" | awk '{print $1}')" = \
  "$base_receipt_sha256"
(
  cd "$base_root"
  sha256sum -c TRANSFER.SHA256SUMS
)
test -f "${base_root}.TRANSFER.SHA256" && \
  test ! -L "${base_root}.TRANSFER.SHA256" || {
  printf 'missing base transfer sidecar: %s\n' "${base_root}.TRANSFER.SHA256" >&2
  exit 66
}
(
  cd "$(dirname "$base_root")"
  sha256sum -c "$(basename "${base_root}.TRANSFER.SHA256")"
)

payload_hash_for() {
  python3 -I - "$base_root/PAYLOAD.SHA256SUMS" "$1" <<'PY'
import re
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
expected_path = sys.argv[2]
matches = []
for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
    if "  " not in line:
        raise SystemExit(f"invalid payload manifest row {line_number}")
    digest, relative = line.split("  ", 1)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(f"invalid payload digest at row {line_number}")
    if relative == expected_path:
        matches.append(digest)
if len(matches) != 1:
    raise SystemExit(f"expected exactly one payload row for {expected_path}, observed {len(matches)}")
print(matches[0])
PY
}
verifier_rel="scripts/wsl/verify_and_extract_in_wsl.sh"
upstream_manifest_rel="manifests/contracts/boltzgen_upstream_tree.SHA256SUMS"
active_manifest_rel="manifests/active_data_tree.SHA256SUMS"
runtime_manifest_rel="manifests/contracts/runtime_tree.SHA256SUMS"
for item in \
  "$verifier_rel:$base_verifier_sha256" \
  "$upstream_manifest_rel:$base_upstream_manifest_sha256" \
  "$active_manifest_rel:$base_active_manifest_sha256" \
  "$runtime_manifest_rel:$base_runtime_manifest_sha256"; do
  relative="${item%%:*}"
  binding_hash="${item##*:}"
  manifest_hash="$(payload_hash_for "$relative")"
  test "$manifest_hash" = "$binding_hash"
  test -f "$base_root/$relative" && test ! -L "$base_root/$relative"
  test "$(sha256sum "$base_root/$relative" | awk '{print $1}')" = "$binding_hash"
done

current_stage="INSTALL_SYSTEM_PREREQUISITES"
missing_system=0
for command_name in git zstd; do
  command -v "$command_name" >/dev/null || missing_system=1
done
dpkg-query -W -f='${Status}\n' python3-venv 2>/dev/null \
  | grep -qx 'install ok installed' || missing_system=1
if [ "$missing_system" -ne 0 ]; then
  printf '安装 Ubuntu 的基础推理工具（不会在 WSL2 内安装 NVIDIA 驱动）...\n'
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      git zstd python3 python3-venv ca-certificates
  else
    command -v sudo >/dev/null || {
      printf 'sudo is required to install Ubuntu prerequisites\n' >&2
      exit 69
    }
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
      git zstd python3 python3-venv ca-certificates
  fi
fi
if [ -x /usr/lib/wsl/lib/nvidia-smi ] && ! command -v nvidia-smi >/dev/null; then
  export PATH="/usr/lib/wsl/lib:$PATH"
fi
for command_name in git zstd python3; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command after prerequisite install: %s\n' "$command_name" >&2
    exit 69
  }
done
command -v nvidia-smi >/dev/null || {
  printf 'WSL2 cannot see nvidia-smi; update the Windows NVIDIA driver, then restart WSL2\n' >&2
  exit 69
}
python3 -I - <<'PY'
import platform
import sys

if sys.version_info[:2] not in {(3, 11), (3, 12)}:
    raise SystemExit(
        f"Python 3.11 or 3.12 is required; Ubuntu 24.04 provides 3.12, observed {platform.python_version()}"
    )
PY

current_stage="GPU_AND_DISK_PREFLIGHT"
nvidia-smi > "$log_root/nvidia_smi.txt"
nvidia-smi --query-gpu=name,driver_version,memory.total \
  --format=csv,noheader,nounits > "$log_root/gpu_inventory.csv"
nvidia-smi --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu \
  --format=csv,noheader,nounits -l 5 > "$log_root/gpu_monitor.csv" &
gpu_monitor_pid="$!"
python3 -I - "$log_root/gpu_inventory.csv" <<'PY'
import re
import sys
from pathlib import Path

rows = [row.strip() for row in Path(sys.argv[1]).read_text().splitlines() if row.strip()]
if len(rows) != 1:
    raise SystemExit(f"expected exactly one NVIDIA GPU, observed {len(rows)}")
name, driver, memory = [item.strip() for item in rows[0].rsplit(",", 2)]
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", driver):
    raise SystemExit(f"unrecognized NVIDIA driver version: {driver}")
if tuple(int(part) for part in driver.split(".")) < (570, 65):
    raise SystemExit(f"NVIDIA driver must be >= 570.65 for this RTX 50 setup: {driver}")
if int(memory) < 10 * 1024:
    raise SystemExit(f"GPU memory is below 10 GiB: {memory} MiB")
print(f"GPU preflight PASS: {name}; driver={driver}; memory={memory} MiB")
PY
free_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
test -n "$free_kib"
if [ "$free_kib" -lt $((80 * 1024 * 1024)) ]; then
  printf 'WSL2 Linux storage needs at least 80 GiB free; observed_kib=%s\n' "$free_kib" >&2
  exit 70
fi

workspace_root="$personal_root/workspace"
current_stage="VERIFY_AND_EXTRACT_BASE_PACKAGE"
if [ -d "$workspace_root" ]; then
  python3 -I - "$workspace_root/handoff/T0_RECEIPT.json" "$base_transfer_sha256" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"existing workspace has no successful extraction receipt: {path}")
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "TRANSFER_AND_SOURCE_VALIDATION_PASS":
    raise SystemExit("existing workspace extraction receipt is not PASS")
if payload.get("expected_transfer_sha256") != sys.argv[2]:
    raise SystemExit("existing workspace was extracted from a different base package")
if payload.get("lockbox_access_count") != 0:
    raise SystemExit("unexpected lockbox access in existing workspace")
print("Reusing previously verified WSL2 workspace")
PY
else
  next_t0="$(python3 -I - "$personal_root/evidence/t0_transfer" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
used = []
if root.is_dir():
    for path in root.iterdir():
        match = re.fullmatch(r"attempt_([0-9]{3})", path.name)
        if match:
            used.append(int(match.group(1)))
print(f"attempt_{max(used, default=0) + 1:03d}")
PY
)"
  bash "$base_root/scripts/wsl/verify_and_extract_in_wsl.sh" \
    "$base_root" "$workspace_root" "$personal_root/evidence" \
    "$next_t0" "$base_transfer_sha256"
fi

current_stage="REVALIDATE_REUSED_SOURCE_AND_INPUTS"
workspace_upstream_manifest="$workspace_root/handoff/package_manifests/contracts/boltzgen_upstream_tree.SHA256SUMS"
workspace_active_manifest="$workspace_root/handoff/package_manifests/active_data_tree.SHA256SUMS"
workspace_runtime_manifest="$workspace_root/handoff/package_manifests/contracts/runtime_tree.SHA256SUMS"
for anchored_manifest in \
  "$workspace_upstream_manifest:$base_upstream_manifest_sha256" \
  "$workspace_active_manifest:$base_active_manifest_sha256" \
  "$workspace_runtime_manifest:$base_runtime_manifest_sha256"; do
  manifest_path="${anchored_manifest%:*}"
  expected_manifest_sha256="${anchored_manifest##*:}"
  test -f "$manifest_path" && test ! -L "$manifest_path"
  test "$(sha256sum "$manifest_path" | awk '{print $1}')" = \
    "$expected_manifest_sha256"
done
(
  cd "$workspace_root"
  sha256sum -c "$workspace_upstream_manifest"
  sha256sum -c "$workspace_active_manifest"
  sha256sum -c "$workspace_runtime_manifest"
)

current_stage="VERIFY_FIXED_INPUTS_AND_WEIGHTS"
boltzgen_source="$workspace_root/software/boltzgen"
runtime_root="$workspace_root/boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
spec="$workspace_root/boltzgen/runs/old12_glp1_mac_enhanced_20260820/configs/01_pdb_00007xl0-A.yaml"
test -f "$boltzgen_source/pyproject.toml"
test -f "$spec"
test -f "$runtime_root/SHA256SUMS"
test "$(sha256sum "$spec" | awk '{print $1}')" = \
  "c1c3c9927e2d8e705ccfe265d6dd40541953183790743c5438ab0f9c15bf8609"
test "$(sha256sum "$boltzgen_source/pyproject.toml" | awk '{print $1}')" = \
  "f1260cddbafb6b83f31951481ccc1602120f36979dc0ffc315f89d19bd62428d"
runtime_contract="$workspace_runtime_manifest"
test -f "$runtime_contract"
weights_used_manifest="$log_root/weights_used.SHA256SUMS"
grep -E '/runtime_cache/(boltzgen1_adherence\.ckpt|boltzgen1_ifold\.ckpt|boltz2_conf_final\.ckpt|mols\.zip)$' \
  "$runtime_contract" > "$weights_used_manifest"
test "$(wc -l < "$weights_used_manifest" | tr -d ' ')" = 4
(
  cd "$workspace_root"
  sha256sum -c "$weights_used_manifest"
)
for asset in \
  boltzgen1_adherence.ckpt boltzgen1_ifold.ckpt boltz2_conf_final.ckpt \
  boltzgen1_diverse.ckpt mols.zip; do
  chmod a-w "$runtime_root/$asset"
  test ! -w "$runtime_root/$asset"
done

current_stage="PREPARE_CUDA128_INFERENCE_ENVIRONMENT"
unset PYTHONPATH PYTHONHOME PYTHONOPTIMIZE LD_PRELOAD || true
for variable_name in ${!PIP_@}; do
  unset "$variable_name"
done
export PIP_CONFIG_FILE=/dev/null
env_parent="$personal_root/envs"
mkdir -p "$env_parent"
generate_boltzgen_tree_manifest() {
  local target_env="$1"
  local output_manifest="$2"
  "$target_env/bin/python" -I - "$target_env" "$output_manifest" <<'PY'
import hashlib
import importlib.metadata
import sys
from pathlib import Path

env_root = Path(sys.argv[1]).resolve(strict=True)
output = Path(sys.argv[2])
distribution = importlib.metadata.distribution("boltzgen")
files = distribution.files
if not files:
    raise SystemExit("BoltzGen distribution has no installed file inventory")

def add_file(path: Path, rows: dict[str, str]) -> None:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"unexpected non-regular BoltzGen installed file: {path}")
    resolved = path.resolve(strict=True)
    try:
        env_relative = resolved.relative_to(env_root).as_posix()
    except ValueError as exc:
        raise SystemExit(f"BoltzGen installed file escapes environment: {resolved}") from exc
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    previous = rows.setdefault(env_relative, digest)
    if previous != digest:
        raise SystemExit(f"conflicting installed file identity: {env_relative}")

rows = {}
scan_roots = {Path(distribution.locate_file("boltzgen")).resolve(strict=True)}
for relative in files:
    relative_text = str(relative)
    if "__pycache__" in relative.parts or relative_text.endswith((".pyc", ".pyo")):
        continue
    located = Path(distribution.locate_file(relative))
    add_file(located, rows)
    if relative.name == "METADATA" and located.parent.name.endswith(".dist-info"):
        scan_roots.add(located.parent.resolve(strict=True))
for scan_root in scan_roots:
    try:
        scan_root.relative_to(env_root)
    except ValueError as exc:
        raise SystemExit(f"BoltzGen installed tree escapes environment: {scan_root}") from exc
    if scan_root.is_symlink() or not scan_root.is_dir():
        raise SystemExit(f"invalid BoltzGen installed tree root: {scan_root}")
    for path in scan_root.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"symlink forbidden in BoltzGen installed tree: {path}")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        add_file(path, rows)
if not rows:
    raise SystemExit("BoltzGen installed tree manifest would be empty")
temporary = output.with_name(f".{output.name}.tmp")
temporary.write_text(
    "".join(f"{rows[name]}  {name}\n" for name in sorted(rows)),
    encoding="utf-8",
)
temporary.replace(output)
PY
}
env_root="$(python3 -I - "$env_parent" "$base_transfer_sha256" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve(strict=True)
expected_base = sys.argv[2]

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def validate_tree_manifest(env_root: Path, manifest: Path) -> bool:
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines:
        return False
    seen = set()
    for line in lines:
        if "  " not in line:
            return False
        digest, relative_text = line.split("  ", 1)
        relative = PurePosixPath(relative_text)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative_text in seen
        ):
            return False
        seen.add(relative_text)
        candidate = env_root.joinpath(*relative.parts)
        cursor = env_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(env_root)
        except (OSError, ValueError):
            return False
        if not resolved.is_file() or file_sha256(resolved) != digest:
            return False
    return True

for marker in sorted(root.glob("boltzgen-cu128-*/PERSONAL_INFERENCE_ENV_READY.json"), reverse=True):
    if marker.is_symlink() or marker.parent.is_symlink():
        continue
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if (
        payload.get("schema_version") == "PERSONAL_BOLTZGEN_CU128_ENV_V2"
        and payload.get("status") == "READY_FOR_EXISTING_WEIGHT_INFERENCE"
        and payload.get("base_transfer_sha256") == expected_base
        and payload.get("boltzgen_version") == "0.3.2"
        and payload.get("cuda_native_kernel_smoke") == "PASS"
        and payload.get("weights_write_allowed") is False
        and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("pip_freeze_lock_sha256", "")))
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(payload.get("boltzgen_installed_tree_manifest_sha256", "")),
        )
    ):
        env_root = marker.parent.resolve(strict=True)
        lock = env_root / "PIP_FREEZE.LOCK.txt"
        tree_manifest = env_root / "BOLTZGEN_INSTALLED_TREE.SHA256SUMS"
        if any(path.is_symlink() or not path.is_file() for path in (lock, tree_manifest)):
            continue
        if file_sha256(lock) != payload["pip_freeze_lock_sha256"]:
            continue
        if file_sha256(tree_manifest) != payload["boltzgen_installed_tree_manifest_sha256"]:
            continue
        if not validate_tree_manifest(env_root, tree_manifest):
            continue
        print(env_root)
        break
PY
)"
if [ -z "$env_root" ]; then
  new_env=1
  env_root="$env_parent/boltzgen-cu128-$run_id"
  env_marker="$env_root/PERSONAL_INFERENCE_ENV_READY.json"
  test ! -e "$env_root"
  env_stage="$env_root"
  python3 -m venv "$env_root"
  "$env_root/bin/python" -m pip install --no-cache-dir --upgrade \
    --index-url https://pypi.org/simple \
    'pip==24.3.1' 'setuptools==75.6.0' 'wheel==0.45.1'
  "$env_root/bin/python" -m pip install --no-cache-dir \
    --index-url https://pypi.org/simple \
    --extra-index-url https://download.pytorch.org/whl/cu128 \
    'torch==2.7.0+cu128' \
    'cuequivariance==0.5.1' \
    'cuequivariance-torch==0.5.1' \
    'cuequivariance-ops-cu12==0.5.1' \
    'cuequivariance-ops-torch-cu12==0.5.1' \
    'pyarrow==18.1.0' \
    "$boltzgen_source"
  "$env_root/bin/python" -m pip check
  test "$("$env_root/bin/boltzgen" --version)" = "boltzgen 0.3.2"
  "$env_root/bin/python" -m pip freeze --all | LC_ALL=C sort \
    > "$env_root/.PIP_FREEZE.LOCK.txt.tmp"
  mv "$env_root/.PIP_FREEZE.LOCK.txt.tmp" "$env_root/PIP_FREEZE.LOCK.txt"
  generate_boltzgen_tree_manifest \
    "$env_root" "$env_root/BOLTZGEN_INSTALLED_TREE.SHA256SUMS"
else
  env_marker="$env_root/PERSONAL_INFERENCE_ENV_READY.json"
fi
test -d "$env_root" && test ! -L "$env_root"
env_lock="$env_root/PIP_FREEZE.LOCK.txt"
env_tree_manifest="$env_root/BOLTZGEN_INSTALLED_TREE.SHA256SUMS"
test -f "$env_lock" && test ! -L "$env_lock"
test -f "$env_tree_manifest" && test ! -L "$env_tree_manifest"
if [ "$new_env" -eq 0 ]; then
  marker_hash_text="$("$env_root/bin/python" -I - "$env_marker" <<'PY'
import json
import re
import sys
from pathlib import Path

marker = Path(sys.argv[1])
if marker.is_symlink() or not marker.is_file():
    raise SystemExit("reused environment marker is not a regular file")
payload = json.loads(marker.read_text(encoding="utf-8"))
for name in ("pip_freeze_lock_sha256", "boltzgen_installed_tree_manifest_sha256"):
    value = payload.get(name)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SystemExit(f"invalid environment marker field: {name}")
    print(value)
PY
)"
  readarray -t marker_hashes <<< "$marker_hash_text"
  test "$(sha256sum "$env_lock" | awk '{print $1}')" = "${marker_hashes[0]}"
  test "$(sha256sum "$env_tree_manifest" | awk '{print $1}')" = "${marker_hashes[1]}"
fi
"$env_root/bin/python" -m pip check
"$env_root/bin/python" -m pip freeze --all | LC_ALL=C sort > "$log_root/pip_freeze.txt"
test "$(sha256sum "$log_root/pip_freeze.txt" | awk '{print $1}')" = \
  "$(sha256sum "$env_lock" | awk '{print $1}')"
current_env_tree_manifest="$log_root/BOLTZGEN_INSTALLED_TREE.current.SHA256SUMS"
generate_boltzgen_tree_manifest "$env_root" "$current_env_tree_manifest"
test "$(sha256sum "$current_env_tree_manifest" | awk '{print $1}')" = \
  "$(sha256sum "$env_tree_manifest" | awk '{print $1}')"
(
  cd "$env_root"
  sha256sum -c BOLTZGEN_INSTALLED_TREE.SHA256SUMS \
    > "$log_root/boltzgen_installed_tree_check.log"
)
"$env_root/bin/boltzgen" --version > "$log_root/boltzgen_version.txt"
test "$(cat "$log_root/boltzgen_version.txt")" = "boltzgen 0.3.2"

current_stage="CUDA_NATIVE_KERNEL_SMOKE"
"$env_root/bin/python" -I - "$log_root/gpu_smoke.json" <<'PY'
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
with torch.inference_mode():
    module = TriangleMultiplicationOutgoing(dim=128).cuda().to(torch.bfloat16).eval()
    x = torch.randn(1, 32, 32, 128, device="cuda", dtype=torch.bfloat16)
    mask = torch.ones(1, 32, 32, device="cuda", dtype=torch.bfloat16)
    y = module(x, mask, use_kernels=True)
    assert y.shape == x.shape and torch.isfinite(y).all()
torch.cuda.synchronize()
payload = {
    "status": "CUDA128_NATIVE_KERNEL_PASS",
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

if [ "$new_env" -eq 1 ]; then
  current_stage="PUBLISH_VERIFIED_INFERENCE_ENVIRONMENT"
  "$env_root/bin/python" -I - \
    "$env_marker" "$base_transfer_sha256" "$env_lock" "$env_tree_manifest" <<'PY'
import datetime as dt
import hashlib
import json
import platform
import sys
from pathlib import Path

marker = Path(sys.argv[1])
payload = {
    "schema_version": "PERSONAL_BOLTZGEN_CU128_ENV_V2",
    "status": "READY_FOR_EXISTING_WEIGHT_INFERENCE",
    "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "python": platform.python_version(),
    "base_transfer_sha256": sys.argv[2],
    "boltzgen_version": "0.3.2",
    "pip_freeze_lock_sha256": hashlib.sha256(Path(sys.argv[3]).read_bytes()).hexdigest(),
    "boltzgen_installed_tree_manifest_sha256": hashlib.sha256(
        Path(sys.argv[4]).read_bytes()
    ).hexdigest(),
    "cuda_native_kernel_smoke": "PASS",
    "weights_write_allowed": False,
}
temporary = marker.with_name(f".{marker.name}.tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(marker)
PY
  env_stage=""
fi

current_stage="RUN_REAL_VHH_INFERENCE_GENERATION_SCREENING"
campaign_root="$workspace_root/boltzgen/runs/personal_vhh_smoke_20260830"
mkdir -p "$campaign_root"
inference_id="attempt_$(date -u +'%Y%m%dT%H%M%SZ')_${RANDOM}_$$"
inference_root="$campaign_root/$inference_id"
pipeline_root="$inference_root/pipeline"
mkdir "$inference_root"
mkdir "$inference_root/cache" "$inference_root/cache/matplotlib" "$inference_root/tmp"
cp "$log_root/gpu_smoke.json" "$inference_root/"
cp "$log_root/pip_freeze.txt" "$inference_root/"
printf '%s\n' "$run_id" > "$inference_root/quickstart_run_id.txt"

export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export WANDB_MODE=offline
export WANDB_DISABLED=true
export DO_NOT_TRACK=1
export XDG_CACHE_HOME="$inference_root/cache"
export MPLCONFIGDIR="$inference_root/cache/matplotlib"
export TMPDIR="$inference_root/tmp"

check_command=(
  "$env_root/bin/boltzgen" check "$spec"
  --output "$inference_root/input_check"
  --moldir "$runtime_root/mols.zip"
)
printf '%q ' "${check_command[@]}" > "$inference_root/check_command.txt"
printf '\n' >> "$inference_root/check_command.txt"
"${check_command[@]}"
checked_cif="$(find "$inference_root/input_check" -maxdepth 1 -type f \
  -name '*.cif' -size +0c -print -quit)"
test -n "$checked_cif"

inference_command=(
  "$env_root/bin/boltzgen" run "$spec"
  --output "$pipeline_root"
  --protocol nanobody-anything
  --num_designs 2
  --budget 1
  --diffusion_batch_size 1
  --inverse_fold_num_sequences 1
  --devices 1
  --num_workers 1
  --use_kernels auto
  --moldir "$runtime_root/mols.zip"
  --design_checkpoints "$runtime_root/boltzgen1_adherence.ckpt"
  --inverse_fold_checkpoint "$runtime_root/boltzgen1_ifold.ckpt"
  --folding_checkpoint "$runtime_root/boltz2_conf_final.ckpt"
  --config design sampling_steps=50 recycling_steps=1 trainer.precision=bf16-mixed
  --config inverse_folding sampling_steps=30 recycling_steps=1 diffusion_samples=1 trainer.precision=32
  --config folding sampling_steps=50 recycling_steps=1 diffusion_samples=1 trainer.precision=bf16-mixed
  --config analysis liability_modality=antibody num_processes=1
  --config filtering modality=antibody filter_bindingsite=true
)
printf '%q ' "${inference_command[@]}" > "$inference_root/inference_command.txt"
printf '\n' >> "$inference_root/inference_command.txt"
inference_started=1
"${inference_command[@]}"
kill "$gpu_monitor_pid" >/dev/null 2>&1 || true
wait "$gpu_monitor_pid" >/dev/null 2>&1 || true
gpu_monitor_pid=""

current_stage="VALIDATE_AND_SUMMARIZE_RESULTS"
metrics="$pipeline_root/final_ranked_designs/all_designs_metrics.csv"
test -s "$metrics"
"$env_root/bin/python" -I - "$inference_root" "$spec" "$runtime_contract" <<'PY'
import csv
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

root = Path(sys.argv[1]).resolve(strict=True)
spec = Path(sys.argv[2]).resolve(strict=True)
runtime_contract = Path(sys.argv[3]).resolve(strict=True)
pipeline = root / "pipeline"
metrics = pipeline / "final_ranked_designs" / "all_designs_metrics.csv"

for output_path in root.rglob("*"):
    if output_path.is_symlink():
        raise SystemExit(f"symlink forbidden anywhere in inference output: {output_path}")

def require_regular_nonempty(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"{label} must be a nonempty regular file: {path}")

def validate_cif(path: Path, label: str) -> None:
    require_regular_nonempty(path, label)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{label} is not UTF-8 mmCIF text: {path}") from exc
    stripped = text.lstrip()
    if (
        not stripped.startswith("data_")
        or "_atom_site." not in text
        or not re.search(r"(?m)^(?:ATOM|HETATM)\s+", text)
    ):
        raise SystemExit(f"{label} lacks the required mmCIF atom table: {path}")

with metrics.open("r", encoding="utf-8", newline="") as handle:
    reader = csv.DictReader(handle)
    filter_columns = reader.fieldnames
    rows = list(reader)
if not filter_columns or len(filter_columns) != len(set(filter_columns)):
    raise SystemExit("filtering metrics require a nonempty unique-column schema")
if len(rows) != 2:
    raise SystemExit(f"filtering metrics expected 2 unique candidates, observed {len(rows)}")
canonical_sequence = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")
scientific_metric_columns = (
    "bb_rmsd",
    "bb_rmsd_design",
    "bindsite_under_8rmsd",
    "design_to_target_iptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
    "design_ptm",
    "plip_hbonds_refolded",
    "plip_saltbridge_refolded",
    "delta_sasa_refolded",
    "CYS_fraction",
    "ALA_fraction",
    "GLY_fraction",
    "GLU_fraction",
    "LEU_fraction",
    "VAL_fraction",
)
candidate_ids_list = [row.get("id", "") for row in rows]
if any(not candidate_id for candidate_id in candidate_ids_list) or len(set(candidate_ids_list)) != 2:
    raise SystemExit(f"filtering metrics do not contain two unique nonempty IDs: {candidate_ids_list}")
sequences = [row.get("designed_sequence", "") for row in rows]
if len(set(sequences)) != 2 or any(not canonical_sequence.fullmatch(sequence) for sequence in sequences):
    raise SystemExit("filtering metrics require two unique nonempty canonical amino-acid sequences")
for index, row in enumerate(rows, 1):
    for column in ("final_rank", "quality_score", *scientific_metric_columns):
        if column not in row:
            raise SystemExit(f"filtering metrics missing required column: {column}")
        try:
            value = float(row[column])
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"invalid {column} in candidate row {index}: {row[column]!r}") from exc
        if not math.isfinite(value):
            raise SystemExit(
                f"non-finite {column} in candidate row {index}; "
                "BoltzGen v0.3.2 may have collapsed the two generated candidates to one unique row"
            )
quality_scores = [float(row["quality_score"]) for row in rows]
final_ranks = [float(row["final_rank"]) for row in rows]
if any(value < 0.0 or value > 1.0 for value in quality_scores):
    raise SystemExit(f"quality_score outside [0,1]: {quality_scores}")
if sorted(final_ranks) != [1.0, 2.0]:
    raise SystemExit(f"final ranks are not exactly 1 and 2: {final_ranks}")

def parse_bool(value: str, field: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise SystemExit(f"invalid boolean {field}: {value!r}")

def reject_nonfinite_parseable_values(row: dict[str, str], label: str) -> None:
    for column, raw_value in row.items():
        value = "" if raw_value is None else str(raw_value).strip()
        if value == "" or value.lower() in {"true", "false"}:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if not math.isfinite(number):
            raise SystemExit(f"non-finite numeric value in {label}.{column}: {raw_value!r}")

def assert_semantic_field_equal(
    left_value: str | None,
    right_value: str | None,
    field: str,
    label: str,
) -> None:
    if left_value is None or right_value is None:
        raise SystemExit(f"missing field in {label}: {field}")
    left = str(left_value)
    right = str(right_value)
    if left == "" or right == "":
        if left != right:
            raise SystemExit(f"empty-field mismatch in {label}: {field}")
        return
    left_number = right_number = None
    left_is_number = right_is_number = False
    try:
        left_number = float(left)
        left_is_number = True
    except ValueError:
        pass
    try:
        right_number = float(right)
        right_is_number = True
    except ValueError:
        pass
    if left_is_number != right_is_number:
        raise SystemExit(f"field type mismatch in {label}: {field}")
    if left_is_number:
        if (
            not math.isfinite(left_number)
            or not math.isfinite(right_number)
            or not math.isclose(left_number, right_number, rel_tol=1e-7, abs_tol=1e-5)
        ):
            raise SystemExit(f"numeric mismatch or non-finite value in {label}: {field}")
    elif left != right:
        raise SystemExit(f"text mismatch in {label}: {field}")

expected_component_filter_columns = {
    "pass_has_x_filter",
    "pass_filter_rmsd_filter",
    "pass_filter_rmsd_design_filter",
    "pass_bindsite_under_8rmsd_filter",
    "pass_CYS_fraction_filter",
    "pass_ALA_fraction_filter",
    "pass_GLY_fraction_filter",
    "pass_GLU_fraction_filter",
    "pass_LEU_fraction_filter",
    "pass_VAL_fraction_filter",
}
observed_component_filter_columns = {
    column
    for column in filter_columns
    if column.startswith("pass_") and column.endswith("_filter")
}
if observed_component_filter_columns != expected_component_filter_columns:
    raise SystemExit(
        "hard-filter component schema mismatch: "
        f"expected={sorted(expected_component_filter_columns)} "
        f"observed={sorted(observed_component_filter_columns)}"
    )
hard_filter_pass_count = 0
for row in rows:
    reject_nonfinite_parseable_values(row, f"filtering[{row['id']}]")
    reported_pass = parse_bool(row.get("pass_filters"), f"{row['id']}.pass_filters")
    component_pass = all(
        parse_bool(row.get(column), f"{row['id']}.{column}")
        for column in sorted(expected_component_filter_columns)
    )
    if reported_pass != component_pass:
        raise SystemExit(
            f"pass_filters disagrees with component hard filters for {row['id']}: "
            f"reported={reported_pass} component_and={component_pass}"
        )
    hard_filter_pass_count += int(reported_pass)

intermediate_cif = sorted((pipeline / "intermediate_designs").glob("*.cif"))
intermediate_npz = sorted((pipeline / "intermediate_designs").glob("*.npz"))
inverse_cif = sorted((pipeline / "intermediate_designs_inverse_folded").glob("*.cif"))
inverse_npz = sorted((pipeline / "intermediate_designs_inverse_folded").glob("*.npz"))
refold_cif = sorted((pipeline / "intermediate_designs_inverse_folded" / "refold_cif").glob("*.cif"))
fold_npz = sorted((pipeline / "intermediate_designs_inverse_folded" / "fold_out_npz").glob("*.npz"))
if any(
    len(paths) != 2
    for paths in (intermediate_cif, intermediate_npz, inverse_cif, inverse_npz, refold_cif, fold_npz)
):
    raise SystemExit(
        "candidate output contract failed: "
        f"design_cif={len(intermediate_cif)} design_npz={len(intermediate_npz)} "
        f"inverse_cif={len(inverse_cif)} inverse_npz={len(inverse_npz)} "
        f"refold_cif={len(refold_cif)} fold_npz={len(fold_npz)}"
    )
for label, paths in (
    ("design CIF", intermediate_cif),
    ("inverse-folded CIF", inverse_cif),
    ("refolded CIF", refold_cif),
):
    for path in paths:
        validate_cif(path, label)
for label, paths in (
    ("design NPZ", intermediate_npz),
    ("inverse-folded NPZ", inverse_npz),
    ("fold NPZ", fold_npz),
):
    for path in paths:
        require_regular_nonempty(path, label)

candidate_ids = set(candidate_ids_list)
rows_by_id = {row["id"]: row for row in rows}
stage_id_sets = {
    "design_cif": {path.stem for path in intermediate_cif},
    "design_npz": {path.stem for path in intermediate_npz},
    "inverse_cif": {path.stem for path in inverse_cif},
    "inverse_npz": {path.stem for path in inverse_npz},
    "refold_cif": {path.stem for path in refold_cif},
    "fold_npz": {path.stem for path in fold_npz},
}
for label, observed_ids in stage_id_sets.items():
    if observed_ids != candidate_ids:
        raise SystemExit(
            f"candidate identity mismatch at {label}: expected={sorted(candidate_ids)} "
            f"observed={sorted(observed_ids)}"
        )

for path in intermediate_npz + inverse_npz:
    with np.load(path, allow_pickle=False) as arrays:
        if not arrays.files:
            raise SystemExit(f"empty candidate NPZ: {path}")
        for name in arrays.files:
            value = np.asarray(arrays[name])
            if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
                raise SystemExit(f"non-finite array {name} in {path}")

scalar_fold_arrays = (
    "min_interaction_pae",
    "min_design_to_target_pae",
    "interaction_pae",
    "ligand_iptm",
    "protein_iptm",
    "iptm",
    "design_iptm",
    "design_iiptm",
    "design_to_target_iptm",
    "design_residue_iptm",
    "design_ptm",
    "target_ptm",
    "ptm",
    "complex_plddt",
    "complex_iplddt",
    "complex_pde",
    "complex_ipde",
    "design_ipsae_min",
    "design_to_target_ipsae",
    "target_to_design_ipsae",
)
expected_fold_arrays = {
    "token_index",
    "mol_type",
    "res_type",
    "atom_resolved_mask",
    "coords",
    "atom_to_token",
    "backbone_mask",
    "input_coords",
    *scalar_fold_arrays,
}
fold_metrics_by_id = {}
for path in fold_npz:
    with np.load(path, allow_pickle=False) as arrays:
        observed_arrays = set(arrays.files)
        if observed_arrays != expected_fold_arrays:
            raise SystemExit(
                f"fold NPZ schema mismatch: missing={sorted(expected_fold_arrays - observed_arrays)} "
                f"extra={sorted(observed_arrays - expected_fold_arrays)} path={path}"
            )
        coords = np.asarray(arrays["coords"])
        if coords.ndim != 3 or coords.shape[0] != 1 or coords.shape[1] == 0 or coords.shape[2] != 3:
            raise SystemExit(f"fold NPZ has invalid coordinate shape {coords.shape}: {path}")
        for name in ("coords", *scalar_fold_arrays):
            value = np.asarray(arrays[name])
            if name != "coords" and value.shape != (1,):
                raise SystemExit(f"fold NPZ scalar {name} has invalid shape {value.shape}: {path}")
            if not np.issubdtype(value.dtype, np.number):
                raise SystemExit(f"fold NPZ array {name} must be numeric: {path}")
            if not np.isfinite(value).all():
                raise SystemExit(f"non-finite fold array {name} in {path}")
        token_index = np.asarray(arrays["token_index"])
        mol_type = np.asarray(arrays["mol_type"])
        res_type = np.asarray(arrays["res_type"])
        token_count = token_index.shape[1] if token_index.ndim == 2 else 0
        if (
            token_index.dtype != np.dtype(np.int64)
            or token_index.shape != (1, token_count)
            or token_count == 0
            or not np.array_equal(token_index[0], np.arange(token_count, dtype=np.int64))
        ):
            raise SystemExit(f"fold NPZ token_index has invalid dtype/content/shape: {path}")
        if mol_type.dtype != np.dtype(np.int64) or mol_type.shape != (1, token_count):
            raise SystemExit(f"fold NPZ mol_type has invalid dtype/shape: {path}")
        if res_type.dtype != np.dtype(np.int64) or res_type.shape != (1, token_count, 33):
            raise SystemExit(f"fold NPZ res_type has invalid dtype/shape: {path}")
        if not np.isin(res_type, (0, 1)).all() or not np.array_equal(
            res_type.sum(axis=2), np.ones((1, token_count), dtype=np.int64)
        ):
            raise SystemExit(f"fold NPZ res_type is not one-hot by token: {path}")
        atom_resolved_mask = np.asarray(arrays["atom_resolved_mask"])
        atom_to_token = np.asarray(arrays["atom_to_token"])
        if (
            atom_resolved_mask.dtype != np.dtype(np.bool_)
            or atom_resolved_mask.ndim != 2
            or atom_resolved_mask.shape != (1, coords.shape[1])
        ):
            raise SystemExit(
                f"fold NPZ atom_resolved_mask has invalid dtype/shape "
                f"{atom_resolved_mask.dtype}/{atom_resolved_mask.shape}: {path}"
            )
        if (
            atom_to_token.dtype != np.dtype(np.bool_)
            or atom_to_token.ndim != 3
            or atom_to_token.shape[0] != 1
            or atom_to_token.shape[1] != coords.shape[1]
            or atom_to_token.shape[2] != token_count
        ):
            raise SystemExit(
                f"fold NPZ atom_to_token has invalid dtype/shape "
                f"{atom_to_token.dtype}/{atom_to_token.shape}: {path}"
            )
        if not np.array_equal(atom_to_token.sum(axis=2), atom_resolved_mask.astype(np.int64)):
            raise SystemExit(f"fold NPZ atom/token assignment disagrees with resolved mask: {path}")
        backbone_mask = np.asarray(arrays["backbone_mask"])
        input_coords = np.asarray(arrays["input_coords"])
        if (
            backbone_mask.shape != (1, coords.shape[1])
            or not np.issubdtype(backbone_mask.dtype, np.number)
            or not np.isfinite(backbone_mask).all()
        ):
            raise SystemExit(f"fold NPZ backbone_mask has invalid shape/value: {path}")
        if (
            input_coords.shape != (1, 1, coords.shape[1], 3)
            or not np.issubdtype(input_coords.dtype, np.number)
            or not np.isfinite(input_coords).all()
        ):
            raise SystemExit(f"fold NPZ input_coords has invalid shape/value: {path}")
        fold_metrics_by_id[path.stem] = {
            name: float(np.asarray(arrays[name])[0]) for name in scalar_fold_arrays
        }

analysis_metrics = pipeline / "intermediate_designs_inverse_folded" / "aggregate_metrics_analyze.csv"
with analysis_metrics.open("r", encoding="utf-8", newline="") as handle:
    reader = csv.DictReader(handle)
    analysis_columns = reader.fieldnames
    analysis_rows = list(reader)
if not analysis_columns or len(analysis_columns) != len(set(analysis_columns)):
    raise SystemExit("analysis metrics require a nonempty unique-column schema")
if not set(analysis_columns).issubset(filter_columns):
    raise SystemExit("filtering metrics must preserve every analysis field")
if len(analysis_rows) != 2:
    raise SystemExit(f"analysis metrics expected 2 candidate rows, observed {len(analysis_rows)}")
if {row.get("id") for row in analysis_rows} != candidate_ids:
    raise SystemExit("analysis candidate IDs differ from filtering candidate IDs")
for index, row in enumerate(analysis_rows, 1):
    candidate_id = row["id"]
    reject_nonfinite_parseable_values(row, f"analysis[{candidate_id}]")
    sequence = row.get("designed_sequence", "")
    if not canonical_sequence.fullmatch(sequence):
        raise SystemExit(f"invalid analysis designed_sequence in row {index}")
    if sequence != rows_by_id[candidate_id]["designed_sequence"]:
        raise SystemExit(f"analysis/filter designed_sequence mismatch for {candidate_id}")
    for column in analysis_columns:
        assert_semantic_field_equal(
            row.get(column),
            rows_by_id[candidate_id].get(column),
            column,
            f"analysis/filter[{candidate_id}]",
        )
    for column in scientific_metric_columns:
        try:
            value = float(row[column])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"invalid analysis metric {column} in row {index}") from exc
        if not math.isfinite(value):
            raise SystemExit(f"non-finite analysis metric {column} in row {index}")
        filter_value = float(rows_by_id[candidate_id][column])
        if not math.isclose(value, filter_value, rel_tol=1e-7, abs_tol=1e-5):
            raise SystemExit(
                f"analysis/filter metric mismatch for {candidate_id} {column}: "
                f"analysis={value} filtering={filter_value}"
            )
    for column, fold_value in fold_metrics_by_id[candidate_id].items():
        try:
            analysis_value = float(row[column])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(
                f"analysis metrics missing fold scalar {column} for {candidate_id}"
            ) from exc
        if not math.isfinite(analysis_value) or not math.isclose(
            fold_value, analysis_value, rel_tol=1e-7, abs_tol=1e-5
        ):
            raise SystemExit(
                f"fold/analysis metric mismatch for {candidate_id} {column}: "
                f"fold={fold_value} analysis={analysis_value}"
            )

selected_cif = sorted(
    (pipeline / "final_ranked_designs" / "final_1_designs").glob("*.cif")
)
selected_metrics = pipeline / "final_ranked_designs" / "final_designs_metrics_1.csv"
with selected_metrics.open("r", encoding="utf-8", newline="") as handle:
    reader = csv.DictReader(handle)
    selected_columns = reader.fieldnames
    selected_rows = list(reader)
if len(selected_cif) != 1 or len(selected_rows) != 1:
    raise SystemExit(
        f"budget selection expected one structure/row: cif={len(selected_cif)} rows={len(selected_rows)}"
    )
selected_id = selected_rows[0]["id"]
if selected_id not in candidate_ids:
    raise SystemExit(f"budget-selected candidate is not in candidate set: {selected_id}")
if selected_cif[0].name != f"rank1_{selected_id}.cif":
    raise SystemExit(f"budget-selected structure identity mismatch: {selected_cif[0].name}")
validate_cif(selected_cif[0], "budget-selected CIF")
selected_row = selected_rows[0]
parent_row = rows_by_id[selected_id]
if selected_columns != [*filter_columns, "sequence"]:
    raise SystemExit("selected metrics schema must equal filtering schema plus final sequence")
if selected_row.get("sequence") != selected_row.get("designed_sequence"):
    raise SystemExit("selected final sequence differs from designed_sequence")
try:
    selected_rank = float(selected_row["final_rank"])
    selected_quality = float(selected_row["quality_score"])
    parent_quality = float(parent_row["quality_score"])
    parent_rank = float(parent_row["final_rank"])
except (KeyError, TypeError, ValueError) as exc:
    raise SystemExit("invalid selected candidate rank or quality score") from exc
if selected_rank != 1.0 or parent_rank != 1.0:
    raise SystemExit(
        f"budget-selected candidate and parent must both have rank 1: "
        f"selected={selected_rank} parent={parent_rank}"
    )
if selected_row.get("designed_sequence") != parent_row["designed_sequence"]:
    raise SystemExit("selected candidate sequence differs from parent filtering row")
if not math.isclose(selected_quality, parent_quality, rel_tol=1e-12, abs_tol=1e-12):
    raise SystemExit("selected candidate quality score differs from parent filtering row")
selected_passes_hard_filters = parse_bool(selected_row.get("pass_filters"), "selected.pass_filters")
parent_passes_hard_filters = parse_bool(parent_row.get("pass_filters"), "parent.pass_filters")
if selected_passes_hard_filters != parent_passes_hard_filters:
    raise SystemExit("selected candidate hard-filter result differs from parent filtering row")
boolean_columns = {
    column for column in filter_columns if column.startswith("pass_")
} | {"pass_filters", "has_x"}
for column in filter_columns:
    selected_value = selected_row.get(column)
    parent_value = parent_row.get(column)
    if selected_value is None or parent_value is None:
        raise SystemExit(f"missing shared selected/filtering field: {column}")
    if column in boolean_columns:
        if parse_bool(selected_value, f"selected.{column}") != parse_bool(
            parent_value, f"parent.{column}"
        ):
            raise SystemExit(f"selected/filtering boolean mismatch: {column}")
        continue
    if selected_value == "" or parent_value == "":
        if selected_value != parent_value:
            raise SystemExit(f"selected/filtering empty-field mismatch: {column}")
        continue
    selected_number = parent_number = None
    selected_is_number = parent_is_number = False
    try:
        selected_number = float(selected_value)
        selected_is_number = True
    except ValueError:
        pass
    try:
        parent_number = float(parent_value)
        parent_is_number = True
    except ValueError:
        pass
    if selected_is_number != parent_is_number:
        raise SystemExit(f"selected/filtering field type mismatch: {column}")
    if selected_is_number:
        if (
            not math.isfinite(selected_number)
            or not math.isfinite(parent_number)
            or not math.isclose(selected_number, parent_number, rel_tol=1e-7, abs_tol=1e-5)
        ):
            raise SystemExit(f"selected/filtering numeric mismatch or non-finite value: {column}")
    elif selected_value != parent_value:
        raise SystemExit(f"selected/filtering text mismatch: {column}")
refold_selected = pipeline / "intermediate_designs_inverse_folded" / "refold_cif" / f"{selected_id}.cif"
if hashlib.sha256(selected_cif[0].read_bytes()).digest() != hashlib.sha256(
    refold_selected.read_bytes()
).digest():
    raise SystemExit("budget-selected CIF bytes differ from its candidate refolded CIF")
created_ckpt = sorted(root.rglob("*.ckpt"))
if created_ckpt:
    raise SystemExit(f"unexpected checkpoint written to inference output: {created_ckpt}")

weights = {}
for line in runtime_contract.read_text(encoding="utf-8").splitlines():
    digest, relative = line.split("  ", 1)
    name = Path(relative).name
    if name in {"boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt", "boltz2_conf_final.ckpt"}:
        weights[name] = digest
if len(weights) != 3:
    raise SystemExit("runtime manifest does not contain the three required checkpoints")

payload = {
    "schema_version": "PERSONAL_VHH_OUTPUT_CONTRACT_VALIDATION_V1",
    "status": "OUTPUT_CONTRACT_VALIDATION_PASS_PENDING_POST_WEIGHT_HASH",
    "validated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "operation": "EXISTING_WEIGHT_VHH_INFERENCE_GENERATION_SCREENING",
    "formal_g1": False,
    "formal_g2": False,
    "formal_aiv1": False,
    "experimental_validation": False,
    "input_spec": str(spec),
    "intermediate_candidate_count": len(intermediate_cif),
    "analysis_candidate_count": len(analysis_rows),
    "screened_candidate_row_count": len(rows),
    "hard_filter_pass_count": hard_filter_pass_count,
    "budget_selected_candidate_structure_count": len(selected_cif),
    "budget_selected_candidate_id": selected_id,
    "budget_selected_candidate_passes_hard_filters": selected_passes_hard_filters,
    "metrics_csv": str(metrics),
    "selected_structures": [str(path) for path in selected_cif],
    "checkpoint_sha256": weights,
    "scientific_boundary": (
        "Computational smoke only; does not establish binding, affinity, selectivity, "
        "safety, developability, or experimental success."
    ),
}
(root / "RESULT_VALIDATION.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

(
  cd "$workspace_root"
  sha256sum -c "$weights_used_manifest"
)
weights_final_checked=1
(
  cd "$inference_root"
  find . -type f ! -name OUTPUTS_PRECOMMIT.SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum
) > "$inference_root/OUTPUTS_PRECOMMIT.SHA256SUMS"
(
  cd "$inference_root"
  sha256sum -c OUTPUTS_PRECOMMIT.SHA256SUMS
)
printf '%s\n' "$inference_root" > "$log_root/inference_output_path.txt"

current_stage="PUBLISH_FINAL_RESULT_SUMMARY"
"$env_root/bin/python" -I - \
  "$inference_root/RESULT_VALIDATION.json" \
  "$inference_root/OUTPUTS_PRECOMMIT.SHA256SUMS" <<'PY'
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

validation_path = Path(sys.argv[1]).resolve(strict=True)
manifest_path = Path(sys.argv[2]).resolve(strict=True)
root = validation_path.parent
payload = json.loads(validation_path.read_text(encoding="utf-8"))
if payload.get("status") != "OUTPUT_CONTRACT_VALIDATION_PASS_PENDING_POST_WEIGHT_HASH":
    raise SystemExit("unexpected precommit result validation status")
payload.update(
    schema_version="PERSONAL_VHH_INFERENCE_RESULT_V1",
    status="PERSONAL_VHH_SMOKE_PASS_NOT_G1_NOT_G2_NOT_AIV1",
    completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
    weights_rechecked_after_inference=True,
    publication_commit_required=True,
    publication_commit_file="RESULT_COMMITTED.json",
    precommit_outputs_manifest=str(manifest_path),
    precommit_outputs_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
)
selected_filter_text = (
    "是" if payload["budget_selected_candidate_passes_hard_filters"] else "否"
)
text = "\n".join(
    [
        "状态：既有权重 VHH 小规模推理流程完成",
        f"生成并完成分析的候选：{payload['analysis_candidate_count']}",
        f"通过全部硬过滤条件的候选：{payload['hard_filter_pass_count']}",
        f"按预算相对排序选出的候选：{payload['budget_selected_candidate_id']}",
        f"该预算候选是否通过全部硬过滤条件：{selected_filter_text}",
        f"指标表：{payload['metrics_csv']}",
        "有效性：只有同目录 RESULT_COMMITTED.json 存在时，这两份摘要才是正式完成结果。",
        "说明：预算排序选出不等于通过硬过滤，更不等于实验验证成功。",
        "边界：这是计算工程结果，不代表实验结合、亲和力、选择性或成药性成立。",
    ]
) + "\n"

text_temp = root / ".RESULT_SUMMARY_ZH.txt.tmp"
text_temp.write_text(text, encoding="utf-8")
text_temp.replace(root / "RESULT_SUMMARY_ZH.txt")
json_temp = root / ".RESULT_SUMMARY.json.tmp"
json_temp.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
json_temp.replace(root / "RESULT_SUMMARY.json")
print(text)
PY

(
  cd "$inference_root"
  sha256sum RESULT_SUMMARY.json RESULT_SUMMARY_ZH.txt
) > "$log_root/final_summary.SHA256SUMS"
(
  cd "$inference_root"
  sha256sum -c "$log_root/final_summary.SHA256SUMS"
)
current_stage="COMMIT_FINAL_RESULT"
"$env_root/bin/python" -I - \
  "$inference_root/RESULT_COMMITTED.json" \
  "$log_root/final_summary.SHA256SUMS" \
  "$inference_root/OUTPUTS_PRECOMMIT.SHA256SUMS" \
  "$inference_root/RESULT_VALIDATION.json" \
  "$run_id" <<'PY'
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

marker = Path(sys.argv[1])
summary_manifest = Path(sys.argv[2]).resolve(strict=True)
outputs_manifest = Path(sys.argv[3]).resolve(strict=True)
validation = Path(sys.argv[4]).resolve(strict=True)
root = marker.parent.resolve(strict=True)
expected_names = {"RESULT_SUMMARY.json", "RESULT_SUMMARY_ZH.txt"}
summary_sha256 = {}
for line in summary_manifest.read_text(encoding="utf-8").splitlines():
    if "  " not in line:
        raise SystemExit("invalid final summary hash row")
    digest, name = line.split("  ", 1)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or name not in expected_names:
        raise SystemExit("unexpected final summary hash row")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"final summary is not a regular file: {name}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"final summary hash mismatch: {name}")
    if name in summary_sha256:
        raise SystemExit(f"duplicate final summary hash row: {name}")
    summary_sha256[name] = digest
if set(summary_sha256) != expected_names:
    raise SystemExit("final summary hash manifest is incomplete")
payload = {
    "schema_version": "PERSONAL_VHH_RESULT_COMMIT_V1",
    "status": "PERSONAL_VHH_SMOKE_PASS_NOT_G1_NOT_G2_NOT_AIV1",
    "committed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "quickstart_run_id": sys.argv[5],
    "summary_sha256": summary_sha256,
    "precommit_outputs_manifest_sha256": hashlib.sha256(
        outputs_manifest.read_bytes()
    ).hexdigest(),
    "result_validation_sha256": hashlib.sha256(validation.read_bytes()).hexdigest(),
    "formal_g1": False,
    "formal_g2": False,
    "formal_aiv1": False,
    "experimental_validation": False,
}
temporary = marker.with_name(f".{marker.name}.tmp")
temporary.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.replace(marker)
PY
sha256sum "$inference_root/RESULT_COMMITTED.json" > "$log_root/result_commit.SHA256"
"$env_root/bin/python" -I - "$inference_root/RESULT_COMMITTED.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "PERSONAL_VHH_SMOKE_PASS_NOT_G1_NOT_G2_NOT_AIV1":
    raise SystemExit("final result commit marker is not PASS")
PY
current_stage="COMPLETE"
success=1
