#!/usr/bin/env bash
# Run one Windows-owner exploratory BoltzGen cell with ordinary reproducibility evidence.
set -euo pipefail
umask 077

usage() {
  printf '%s\n' \
    'usage: run_owner_exploratory_cell.sh WORKSPACE_ROOT CELL_ID SPEC CHECKPOINT NUM_DESIGNS' \
    '  CHECKPOINT must be adherence or diverse; batch size is fixed at 1.'
}

if [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi
[ "$#" -eq 5 ] || { usage >&2; exit 64; }

workspace_input=$1
cell_id=$2
spec_input=$3
checkpoint_name=$4
num_designs=$5

[[ "$cell_id" =~ ^[a-z0-9][a-z0-9_.-]{0,95}$ ]] || {
  printf 'unsafe CELL_ID: %s\n' "$cell_id" >&2
  exit 64
}
case "$checkpoint_name" in
  adherence|diverse) ;;
  *) printf 'CHECKPOINT must be adherence or diverse\n' >&2; exit 64 ;;
esac
case "$num_designs" in
  ''|*[!0-9]*) printf 'NUM_DESIGNS must be an integer from 1 through 10\n' >&2; exit 64 ;;
esac
[ "$num_designs" -ge 1 ] && [ "$num_designs" -le 10 ] || {
  printf 'NUM_DESIGNS must be an integer from 1 through 10\n' >&2
  exit 64
}

for command_name in \
  cp date df env find flock git grep id mkdir mv nvidia-smi python3 readlink realpath \
  rm sed sha256sum sleep sort stat tail tr wc xargs; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command: %s\n' "$command_name" >&2
    exit 69
  }
done

workspace_root="$(realpath "$workspace_input")"
case "$workspace_root" in
  /home/*) ;;
  *) printf 'workspace must be under WSL /home: %s\n' "$workspace_root" >&2; exit 64 ;;
esac
repo_root="$workspace_root/GLP_"
owner_marker="$workspace_root/WINDOWS_OWNER_MODE.json"
test -d "$repo_root/.git" && test ! -L "$repo_root"
test -f "$owner_marker" && test ! -L "$owner_marker"

python3 -I -S - "$owner_marker" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
required = {
    "status": "ACTIVE",
    "authority": "WINDOWS_CODEX",
    "mac_review_required": False,
    "environment_contract_required": False,
    "training_allowed": False,
    "model_weights_mutable": False,
}
for key, value in required.items():
    if payload.get(key) != value:
        raise SystemExit(f"owner marker mismatch: {key}")
PY

acceptance_root="$workspace_root/gpu_work/owner_mode/local_env_acceptance"
acceptance_receipt="$(
  find "$acceptance_root" -mindepth 2 -maxdepth 2 -type f \
    -name LOCAL_ENV_ACCEPTANCE.json -print 2>/dev/null | sort -V | tail -1
)"
test -n "$acceptance_receipt" && test -f "$acceptance_receipt" && test ! -L "$acceptance_receipt" || {
  printf 'no LOCAL_ENV_ACCEPTANCE receipt found\n' >&2
  exit 66
}
acceptance_attempt="$(dirname "$acceptance_receipt")"
( cd "$acceptance_attempt" && sha256sum --strict -c SHA256SUMS >/dev/null )
python_bin="$(python3 -I -S - "$acceptance_receipt" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "LOCAL_ENV_READY" or payload.get("exit_code") != 0:
    raise SystemExit("latest local environment acceptance is not ready")
if payload.get("mac_review_required") is not False:
    raise SystemExit("local environment receipt unexpectedly requires Mac review")
print(payload["python_bin"])
PY
)"
test -x "$python_bin"
environment_bin="$(dirname "$python_bin")"
boltzgen_launcher="$environment_bin/boltzgen-wsl-sm120"
test -x "$boltzgen_launcher" && test ! -L "$boltzgen_launcher"

spec_path="$(readlink -f -- "$spec_input")"
test -f "$spec_path" && test ! -L "$spec_path"
spec_root="$(dirname "$spec_path")"
$python_bin -I - "$spec_root" "$spec_path" <<'PY'
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
spec = Path(sys.argv[2])
if root.is_symlink() or not root.is_dir() or spec.parent != root:
    raise SystemExit("unsafe spec bundle root")
expected = {"design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"}
observed = {path.name for path in root.iterdir() if path.is_file() and not path.is_symlink()}
if observed != expected or any((root / name).is_symlink() for name in expected):
    raise SystemExit(
        f"spec bundle closure mismatch: missing={sorted(expected - observed)} "
        f"unexpected={sorted(observed - expected)}"
    )

design = yaml.safe_load((root / "design.yaml").read_text(encoding="utf-8"))
scaffold = yaml.safe_load((root / "scaffold.yaml").read_text(encoding="utf-8"))
if not isinstance(design, dict) or not isinstance(scaffold, dict):
    raise SystemExit("spec YAML roots must be objects")
entities = design.get("entities")
if not isinstance(entities, list):
    raise SystemExit("design.yaml entities must be a list")
design_paths = []
for entity in entities:
    if not isinstance(entity, dict) or not isinstance(entity.get("file"), dict):
        raise SystemExit("design.yaml entities must contain only file objects")
    design_paths.append(entity["file"].get("path"))
if design_paths != ["target.cif", "scaffold.yaml"]:
    raise SystemExit(f"unexpected design.yaml file references: {design_paths!r}")
if scaffold.get("path") != "scaffold.cif":
    raise SystemExit(f"unexpected scaffold.yaml path: {scaffold.get('path')!r}")
PY
runtime_root="$workspace_root/boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819/runtime_cache"
test -d "$runtime_root" && test ! -L "$runtime_root"
design_checkpoint="$runtime_root/boltzgen1_${checkpoint_name}.ckpt"
inverse_checkpoint="$runtime_root/boltzgen1_ifold.ckpt"
folding_checkpoint="$runtime_root/boltz2_conf_final.ckpt"
mols_path="$runtime_root/mols.zip"
for required_file in \
  "$design_checkpoint" "$inverse_checkpoint" "$folding_checkpoint" "$mols_path" \
  "$runtime_root/SHA256SUMS"; do
  test -f "$required_file" && test ! -L "$required_file" || {
    printf 'missing or unsafe runtime asset: %s\n' "$required_file" >&2
    exit 66
  }
done

validator="$repo_root/boltzgen/main/windows_gpu_handoff_20260829/t3_runtime/validate_cell_output.py"
test -f "$validator" && test ! -L "$validator"

attempt_stamp="$(date -u +'%Y%m%dT%H%M%SZ')"
attempt_id="attempt_$attempt_stamp"
cell_root="$workspace_root/gpu_work/owner_mode/t8_exploratory_inference/$cell_id"
attempt_root="$cell_root/$attempt_id"
test ! -e "$attempt_root" && test ! -L "$attempt_root"
mkdir -p "$cell_root"
python3 -I -S - "$workspace_root/gpu_work/owner_mode/t8_exploratory_inference" "$cell_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

base, cell = map(Path, sys.argv[1:])
base = base.resolve(strict=True)
cell = cell.resolve(strict=True)
if cell.parent != base or cell.is_symlink():
    raise SystemExit("unsafe exploratory cell directory")
for path in (base, cell):
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SystemExit(f"unsafe owner/mode: {path}")
PY
mkdir "$attempt_root"
operator_logs="$attempt_root/operator_logs"
mkdir "$operator_logs"

full_finalizer_ready=0
early_finalize() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [ "$full_finalizer_ready" -eq 1 ]; then
    return "$exit_code"
  fi
  set +e
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$operator_logs/ended_at_utc.txt"
  printf '%s\n' EXPLORATORY_INFERENCE_FAILED > "$operator_logs/STATUS.txt"
  printf '%s\n' "$exit_code" > "$operator_logs/exit_code.txt"
  (
    cd "$attempt_root"
    find . -type f ! -path './operator_logs/OUTPUT_SHA256SUMS' -print0 \
      | sort -z | xargs -0 sha256sum > operator_logs/OUTPUT_SHA256SUMS
  )
  printf 'EXPLORATORY_INFERENCE_FAILED path=%s\n' "$attempt_root" >&2
  exit "$exit_code"
}
trap early_finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '%q ' "$0" "$@" > "$operator_logs/command.txt"
printf '\n' >> "$operator_logs/command.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$operator_logs/started_at_utc.txt"
git -C "$repo_root" rev-parse HEAD > "$operator_logs/source_commit.txt"
git -C "$repo_root" rev-parse HEAD^{tree} > "$operator_logs/source_tree.txt"
git -C "$repo_root" status --short > "$operator_logs/source_status.txt"
test ! -s "$operator_logs/source_status.txt" || {
  printf 'repository must be clean before exploratory inference\n' >&2
  exit 74
}
cp "$acceptance_receipt" "$operator_logs/LOCAL_ENV_ACCEPTANCE.json"
sha256sum "$owner_marker" "$acceptance_receipt" "$spec_path" "$validator" \
  > "$operator_logs/input_bindings.sha256"
(
  cd "$spec_root"
  find . -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum \
    > "$operator_logs/spec_bundle_before.SHA256SUMS"
)
spec_prechecked=1
df -B1 "$workspace_root" > "$operator_logs/disk_before.txt"
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free,utilization.gpu,temperature.gpu \
  --format=csv > "$operator_logs/gpu_before.csv"
exec 9<"/run/user/$(id -u)"
flock -n 9 || {
  printf 'the shared single-GPU lock is already held\n' >&2
  exit 75
}
nvidia-smi --query-compute-apps=pid,process_name,used_memory \
  --format=csv,noheader,nounits > "$operator_logs/gpu_compute_processes_before.csv"
test ! -s "$operator_logs/gpu_compute_processes_before.csv" || {
  printf 'another GPU compute process is active\n' >&2
  exit 75
}

python3 -I -S - \
  "$runtime_root/SHA256SUMS" "$operator_logs/runtime_assets_used.SHA256SUMS" \
  "$(basename "$design_checkpoint")" "$(basename "$inverse_checkpoint")" \
  "$(basename "$folding_checkpoint")" "$(basename "$mols_path")" <<'PY'
import re
import sys
from pathlib import Path

source, output = map(Path, sys.argv[1:3])
wanted = set(sys.argv[3:])
rows = {}
for line in source.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/][^\x00]*)", line)
    if match:
        rows[match.group(2)] = match.group(1)
if set(rows) & wanted != wanted:
    raise SystemExit(f"runtime manifest is missing: {sorted(wanted - set(rows))}")
output.write_text(
    "".join(f"{rows[name]}  {name}\n" for name in sorted(wanted)),
    encoding="utf-8",
)
PY
( cd "$runtime_root" && sha256sum --strict -c "$operator_logs/runtime_assets_used.SHA256SUMS" ) \
  > "$operator_logs/runtime_assets_before.txt"
runtime_prechecked=1

monitor_pid=''
monitor_stopped=0
monitor_was_running=0
monitor_wait_code=125
pipeline_success=0
stop_monitor() {
  if [ "$monitor_stopped" -eq 1 ]; then
    return 0
  fi
  if [ -n "$monitor_pid" ]; then
    if kill -0 "$monitor_pid" 2>/dev/null; then
      monitor_was_running=1
      kill "$monitor_pid" 2>/dev/null || true
    fi
    if wait "$monitor_pid"; then
      monitor_wait_code=0
    else
      monitor_wait_code=$?
    fi
  fi
  printf '%s\n' "$monitor_wait_code" > "$operator_logs/gpu_monitor_wait_exit_code.txt"
  monitor_stopped=1
  return 0
}

seal_output_manifest() {
  local manifest="$operator_logs/OUTPUT_SHA256SUMS"
  local temporary="$operator_logs/.OUTPUT_SHA256SUMS.tmp"
  if ! rm -f -- "$manifest" "$temporary"; then
    return 1
  fi
  if ! python3 -I -S - "$attempt_root" "$temporary" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
output = Path(sys.argv[2])
excluded = {
    "operator_logs/OUTPUT_SHA256SUMS",
    "operator_logs/.OUTPUT_SHA256SUMS.tmp",
}
records = []
for path in root.rglob("*"):
    relative = path.relative_to(root).as_posix()
    if relative in excluded:
        continue
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        continue
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"unsafe output member: {relative}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise SystemExit(f"output changed while hashing: {relative}")
    records.append((relative, digest.hexdigest()))
records.sort(key=lambda item: item[0].encode("utf-8"))
if not records:
    raise SystemExit("refusing an empty output manifest")

required = {
    "operator_logs/STATUS.txt",
    "operator_logs/exit_code.txt",
    "operator_logs/EXPLORATORY_INFERENCE.json",
    "operator_logs/started_at_utc.txt",
    "operator_logs/ended_at_utc.txt",
    "operator_logs/runtime_assets_used.SHA256SUMS",
    "operator_logs/spec_bundle_before.SHA256SUMS",
    "operator_logs/gpu_monitor_wait_exit_code.txt",
    "operator_logs/gpu_after.exit_code.txt",
    "operator_logs/gpu_compute_processes_after.exit_code.txt",
    "operator_logs/gpu_compute_processes_after.csv",
    "operator_logs/disk_after.exit_code.txt",
    "operator_logs/cuda_oom_detected.txt",
}
observed = {relative for relative, _ in records}
if not required <= observed:
    raise SystemExit(f"required output members missing: {sorted(required - observed)}")
status = (root / "operator_logs/STATUS.txt").read_text(encoding="utf-8").strip()
try:
    exit_code = int((root / "operator_logs/exit_code.txt").read_text(encoding="utf-8").strip())
    receipt = json.loads(
        (root / "operator_logs/EXPLORATORY_INFERENCE.json").read_text(encoding="utf-8")
    )
except (OSError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid terminal receipt: {exc}") from exc
if receipt.get("status") != status or receipt.get("exit_code") != exit_code:
    raise SystemExit("terminal status/exit code disagree with JSON receipt")
if status == "EXPLORATORY_INFERENCE_COMPLETE":
    validation = receipt.get("output_validation")
    config = receipt.get("resolved_config_contract")
    if (
        exit_code != 0
        or receipt.get("cuda_oom_detected") is not False
        or not isinstance(validation, dict)
        or validation.get("status") != "PASS"
        or not isinstance(config, dict)
        or config.get("status") != "PASS"
        or receipt.get("observed_designs") != receipt.get("expected_designs")
        or receipt.get("fold_samples_per_candidate") != 5
        or (root / "operator_logs/gpu_monitor_wait_exit_code.txt").read_text(
            encoding="utf-8"
        ).strip() != "143"
        or (root / "operator_logs/gpu_after.exit_code.txt").read_text(
            encoding="utf-8"
        ).strip() != "0"
        or (root / "operator_logs/gpu_compute_processes_after.exit_code.txt").read_text(
            encoding="utf-8"
        ).strip() != "0"
        or (root / "operator_logs/gpu_compute_processes_after.csv").stat().st_size != 0
        or (root / "operator_logs/disk_after.exit_code.txt").read_text(
            encoding="utf-8"
        ).strip() != "0"
        or (root / "operator_logs/cuda_oom_detected.txt").read_text(
            encoding="utf-8"
        ).strip() != "false"
    ):
        raise SystemExit("complete receipt does not satisfy the execution contract")
    for stage in ("design", "inverse_folding", "folding", "analysis", "filtering", "validation"):
        stage_exit = root / f"operator_logs/{stage}.exit_code.txt"
        if not stage_exit.is_file() or stage_exit.read_text(encoding="utf-8").strip() != "0":
            raise SystemExit(f"complete receipt has nonzero or missing stage: {stage}")
elif status == "EXPLORATORY_INFERENCE_FAILED":
    if exit_code == 0:
        raise SystemExit("failed receipt cannot have exit code zero")
else:
    raise SystemExit(f"invalid terminal status: {status!r}")
content = "".join(f"{digest}  ./{relative}\n" for relative, digest in records).encode()
descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as stream:
    stream.write(content)
    stream.flush()
    os.fsync(stream.fileno())
PY
  then
    rm -f -- "$temporary"
    return 1
  fi
  if ! ( cd "$attempt_root" && sha256sum --strict -c \
      operator_logs/.OUTPUT_SHA256SUMS.tmp >/dev/null ); then
    rm -f -- "$temporary"
    return 1
  fi
  if ! python3 -I -S - "$attempt_root" "$temporary" <<'PY'
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
manifest = Path(sys.argv[2])
pattern = re.compile(r"[0-9a-f]{64}  \./([^\x00\r\n]+)")
listed = set()
for line in manifest.read_text(encoding="utf-8").splitlines():
    match = pattern.fullmatch(line)
    if match is None or match.group(1) in listed:
        raise SystemExit("invalid or duplicate output manifest record")
    listed.add(match.group(1))
observed = set()
for path in root.rglob("*"):
    relative = path.relative_to(root).as_posix()
    if relative in {
        "operator_logs/OUTPUT_SHA256SUMS",
        "operator_logs/.OUTPUT_SHA256SUMS.tmp",
    }:
        continue
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        continue
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"unsafe terminal output member: {relative}")
    observed.add(relative)
if listed != observed:
    raise SystemExit(
        f"output manifest closure mismatch: missing={sorted(observed - listed)} "
        f"unexpected={sorted(listed - observed)}"
    )
PY
  then
    rm -f -- "$temporary"
    return 1
  fi
  if ! mv -fT -- "$temporary" "$manifest"; then
    rm -f -- "$temporary"
    return 1
  fi
}

write_terminal_files() {
  local status_text=$1
  local exit_text=$2
  local status_temporary="$operator_logs/.STATUS.txt.tmp"
  local exit_temporary="$operator_logs/.exit_code.txt.tmp"
  rm -f -- "$status_temporary" "$exit_temporary" || return 1
  printf '%s\n' "$status_text" > "$status_temporary" || return 1
  printf '%s\n' "$exit_text" > "$exit_temporary" || return 1
  mv -fT -- "$status_temporary" "$operator_logs/STATUS.txt" || return 1
  mv -fT -- "$exit_temporary" "$operator_logs/exit_code.txt" || return 1
}

mark_terminal_failure() {
  local failure_code=$1
  local failure_reason=$2
  write_terminal_files EXPLORATORY_INFERENCE_FAILED "$failure_code" || return 1
  python3 -I -S - \
    "$operator_logs/EXPLORATORY_INFERENCE.json" "$failure_code" "$failure_reason" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_file():
    payload = json.loads(path.read_text(encoding="utf-8"))
else:
    payload = {"schema_version": "WINDOWS_OWNER_EXPLORATORY_INFERENCE_V1"}
payload["status"] = "EXPLORATORY_INFERENCE_FAILED"
payload["exit_code"] = int(sys.argv[2])
payload["terminal_failure_reason"] = sys.argv[3]
path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

mark_terminal_success() {
  write_terminal_files EXPLORATORY_INFERENCE_COMPLETE 0 || return 1
  python3 -I -S - "$operator_logs/EXPLORATORY_INFERENCE.json" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
temporary = path.with_name(".EXPLORATORY_INFERENCE.json.tmp")
payload = json.loads(path.read_text(encoding="utf-8"))
payload["status"] = "EXPLORATORY_INFERENCE_COMPLETE"
payload["exit_code"] = 0
payload.pop("terminal_failure_reason", None)
with temporary.open("w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
PY
}

finalize() {
  local exit_code=$?
  trap - EXIT INT TERM
  set +e
  stop_monitor
  monitor_line_count="$(wc -l < "$operator_logs/gpu_monitor.csv" | tr -d ' ')"
  if [ "$monitor_was_running" -ne 1 ] || [ "$monitor_wait_code" -ne 143 ] || \
      [ "$monitor_line_count" -lt 2 ] || [ -s "$operator_logs/gpu_monitor.stderr.txt" ]; then
    exit_code=74
  fi
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$operator_logs/ended_at_utc.txt"
  nvidia-smi --query-gpu=name,memory.used,memory.free,utilization.gpu,temperature.gpu \
    --format=csv > "$operator_logs/gpu_after.csv" 2> "$operator_logs/gpu_after.stderr.txt"
  gpu_after_code=$?
  printf '%s\n' "$gpu_after_code" > "$operator_logs/gpu_after.exit_code.txt"
  nvidia-smi --query-compute-apps=pid,process_name,used_memory \
    --format=csv,noheader,nounits > "$operator_logs/gpu_compute_processes_after.csv" \
    2> "$operator_logs/gpu_compute_processes_after.stderr.txt"
  gpu_process_after_code=$?
  printf '%s\n' "$gpu_process_after_code" \
    > "$operator_logs/gpu_compute_processes_after.exit_code.txt"
  df -B1 "$workspace_root" > "$operator_logs/disk_after.txt"
  disk_after_code=$?
  printf '%s\n' "$disk_after_code" > "$operator_logs/disk_after.exit_code.txt"
  if [ "$gpu_after_code" -ne 0 ] || [ "$gpu_process_after_code" -ne 0 ] || \
      [ "$disk_after_code" -ne 0 ] || \
      [ -s "$operator_logs/gpu_compute_processes_after.csv" ]; then
    exit_code=74
  fi

  oom_detected=false
  if grep -Eiq 'CUDA([^\n]{0,80})out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED' \
      "$operator_logs"/*.stderr.txt 2>/dev/null; then
    oom_detected=true
  fi
  printf '%s\n' "$oom_detected" > "$operator_logs/cuda_oom_detected.txt"
  if [ "$oom_detected" = true ]; then
    exit_code=76
  fi

  local status=EXPLORATORY_INFERENCE_FAILED
  if [ "$exit_code" -eq 0 ] && [ "$pipeline_success" -eq 1 ]; then
    status=EXPLORATORY_INFERENCE_FINALIZING
  elif [ "$exit_code" -eq 0 ]; then
    exit_code=75
  fi
  if ! write_terminal_files "$status" "$exit_code"; then
    exit_code=74
    status=EXPLORATORY_INFERENCE_FAILED
    mark_terminal_failure "$exit_code" TERMINAL_STATUS_WRITE_FAILED || true
  fi

  "$python_bin" -I - \
    "$attempt_root" "$status" "$exit_code" "$cell_id" "$attempt_id" \
    "$checkpoint_name" "$num_designs" "$spec_path" "$design_checkpoint" \
    "$inverse_checkpoint" "$folding_checkpoint" "$mols_path" \
    "$acceptance_receipt" "$oom_detected" <<'PY'
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

(
    root_text, status, exit_text, cell_id, attempt_id, checkpoint_name,
    expected_text, spec_text, design_text, inverse_text, folding_text,
    mols_text, acceptance_text, oom_text,
) = sys.argv[1:]
root = Path(root_text)

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

validator = root / "operator_logs/cell_contract.json"
validation = json.loads(validator.read_text(encoding="utf-8")) if validator.is_file() else None
resolved_path = root / "operator_logs/resolved_config_contract.json"
resolved = json.loads(resolved_path.read_text(encoding="utf-8")) if resolved_path.is_file() else None
checks = resolved.get("checks", {}) if isinstance(resolved, dict) else {}
metrics_path = root / "final_ranked_designs/all_designs_metrics.csv"
rows = []
if metrics_path.is_file():
    with metrics_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
pass_filters = sum(str(row.get("pass_filters", "")).lower() == "true" for row in rows)

peak_used = None
total_memory = None
monitor = root / "operator_logs/gpu_monitor.csv"
if monitor.is_file():
    with monitor.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            used_match = re.search(r"[0-9]+", row.get(" memory.used [MiB]", ""))
            total_match = re.search(r"[0-9]+", row.get(" memory.total [MiB]", ""))
            if used_match and total_match:
                used = int(used_match.group())
                total = int(total_match.group())
                peak_used = used if peak_used is None else max(peak_used, used)
                total_memory = total

def read_manifest(path: Path) -> dict[str, str]:
    rows = {}
    pattern = re.compile(r"([0-9a-f]{64})  (?:\./)?([^/\x00\r\n]+)")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(line)
        if match is None or match.group(2) in rows:
            raise SystemExit(f"invalid or duplicate input manifest row: {line!r}")
        rows[match.group(2)] = match.group(1)
    return rows

paths = [Path(spec_text), Path(design_text), Path(inverse_text), Path(folding_text), Path(mols_text)]
spec_manifest = root / "operator_logs/spec_bundle_before.SHA256SUMS"
runtime_manifest = root / "operator_logs/runtime_assets_used.SHA256SUMS"
spec_hashes = read_manifest(spec_manifest)
runtime_hashes = read_manifest(runtime_manifest)
input_sha256 = {spec_text: spec_hashes[Path(spec_text).name]}
for path in paths[1:]:
    input_sha256[str(path)] = runtime_hashes[path.name]
payload = {
    "schema_version": "WINDOWS_OWNER_EXPLORATORY_INFERENCE_V1",
    "status": status,
    "exit_code": int(exit_text),
    "authority": "WINDOWS_CODEX",
    "mac_review_required": False,
    "environment_contract_required": False,
    "formal_g1": False,
    "formal_g2": False,
    "cell_id": cell_id,
    "attempt_id": attempt_id,
    "run_root": str(root),
    "checkpoint_name": checkpoint_name,
    "expected_designs": int(expected_text),
    "observed_designs": validation.get("observed_unique_ids") if validation else None,
    "fold_samples_per_candidate": validation.get("fold_samples_per_candidate") if validation else None,
    "filter_pass_count": pass_filters,
    "batch_size": checks.get("design.batch_size"),
    "design_sampling_steps": checks.get("design.sampling_steps"),
    "design_recycling_steps": checks.get("design.recycling_steps"),
    "inverse_fold_sampling_steps": checks.get("inverse.sampling_steps"),
    "inverse_fold_recycling_steps": checks.get("inverse.recycling_steps"),
    "folding_sampling_steps": checks.get("folding.sampling_steps"),
    "folding_recycling_steps": checks.get("folding.recycling_steps"),
    "cuda_oom_detected": oom_text == "true",
    "gpu_peak_memory_used_mib": peak_used,
    "gpu_total_memory_mib": total_memory,
    "gpu_peak_memory_fraction": (
        peak_used / total_memory if peak_used is not None and total_memory else None
    ),
    "local_env_acceptance_path": acceptance_text,
    "local_env_acceptance_sha256": digest(Path(acceptance_text)),
    "input_sha256": input_sha256,
    "spec_bundle_manifest_sha256": digest(spec_manifest) if spec_manifest.is_file() else None,
    "resolved_config_contract": resolved,
    "output_validation": validation,
    "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
}
(root / "operator_logs/EXPLORATORY_INFERENCE.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  summary_code=$?
  if [ "$summary_code" -ne 0 ]; then
    [ "$exit_code" -ne 0 ] || exit_code=$summary_code
    status=EXPLORATORY_INFERENCE_FAILED
    mark_terminal_failure "$exit_code" SUMMARY_GENERATION_FAILED
  fi

  # Revalidate all external inputs before publishing COMPLETE.  Until this
  # succeeds there is no output manifest and the status remains FINALIZING.
  terminal_input_code=0
  if [ "${runtime_prechecked:-0}" -ne 1 ] || \
      ! ( cd "$runtime_root" && sha256sum --strict -c \
        "$operator_logs/runtime_assets_used.SHA256SUMS" >/dev/null ); then
    terminal_input_code=1
  fi
  if [ "${spec_prechecked:-0}" -ne 1 ] || \
      ! ( cd "$spec_root" && sha256sum --strict -c \
        "$operator_logs/spec_bundle_before.SHA256SUMS" >/dev/null ); then
    terminal_input_code=1
  fi
  if [ "${config_prechecked:-0}" -ne 1 ] || \
      ! ( cd "$attempt_root" && sha256sum --strict -c \
        operator_logs/resolved_config.SHA256SUMS >/dev/null ); then
    terminal_input_code=1
  fi
  if [ "$terminal_input_code" -ne 0 ]; then
    exit_code=74
    status=EXPLORATORY_INFERENCE_FAILED
    mark_terminal_failure "$exit_code" TERMINAL_INPUT_REVALIDATION_FAILED || true
  elif [ "$status" = EXPLORATORY_INFERENCE_FINALIZING ]; then
    if mark_terminal_success; then
      status=EXPLORATORY_INFERENCE_COMPLETE
    else
      exit_code=74
      status=EXPLORATORY_INFERENCE_FAILED
      mark_terminal_failure "$exit_code" TERMINAL_SUCCESS_PUBLISH_FAILED || true
    fi
  fi
  if ! seal_output_manifest; then
    exit_code=74
    status=EXPLORATORY_INFERENCE_FAILED
    mark_terminal_failure "$exit_code" OUTPUT_MANIFEST_SEAL_FAILED || true
    if ! seal_output_manifest; then
      printf 'EXPLORATORY_INFERENCE_FAILED manifest_seal_failed path=%s\n' \
        "$attempt_root" >&2
      exit "$exit_code"
    fi
  fi
  printf '%s path=%s\n' "$(sed -n '1p' "$operator_logs/STATUS.txt")" "$attempt_root"
  exit "$exit_code"
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
full_finalizer_ready=1

(
  nvidia-smi --query-gpu=timestamp,index,name,memory.total,memory.used,utilization.gpu,power.draw \
    --format=csv
  while sleep 1; do
    nvidia-smi --query-gpu=timestamp,index,name,memory.total,memory.used,utilization.gpu,power.draw \
      --format=csv,noheader
  done
) > "$operator_logs/gpu_monitor.csv" 2> "$operator_logs/gpu_monitor.stderr.txt" &
monitor_pid=$!
sleep 1
kill -0 "$monitor_pid"

run_logged() {
  local label=$1
  shift
  local started ended result
  started="$(date +%s)"
  set +e
  "$@" > "$operator_logs/$label.stdout.txt" 2> "$operator_logs/$label.stderr.txt"
  result=$?
  set -e
  ended="$(date +%s)"
  printf '%s\n' "$result" > "$operator_logs/$label.exit_code.txt"
  printf '%s\n' "$((ended - started))" > "$operator_logs/$label.duration_seconds.txt"
  return "$result"
}

run_logged configure "$boltzgen_launcher" configure "$spec_path" \
  --output "$attempt_root" \
  --protocol nanobody-anything \
  --num_designs "$num_designs" \
  --budget "$num_designs" \
  --diffusion_batch_size 1 \
  --inverse_fold_num_sequences 1 \
  --design_checkpoints "$design_checkpoint" \
  --inverse_fold_checkpoint "$inverse_checkpoint" \
  --folding_checkpoint "$folding_checkpoint" \
  --moldir "$mols_path" \
  --devices 1 \
  --num_workers 4 \
  --use_kernels auto \
  --config analysis 'liability_modality=antibody' \
  --config filtering 'modality=antibody' 'filter_bindingsite=true'

run_logged resolved_config_validation "$python_bin" -I - \
  "$attempt_root" "$num_designs" "$spec_path" "$design_checkpoint" \
  "$inverse_checkpoint" "$folding_checkpoint" "$mols_path" <<'PY'
import json
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
expected_designs = int(sys.argv[2])
spec_path, design_checkpoint, inverse_checkpoint, folding_checkpoint, mols_path = sys.argv[3:]

def load(name: str) -> dict:
    value = yaml.safe_load((root / "config" / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"resolved config is not an object: {name}")
    return value

design = load("design.yaml")
inverse = load("inverse_folding.yaml")
folding = load("folding.yaml")
analysis = load("analysis.yaml")
filtering = load("filtering.yaml")

checks = {
    "design.checkpoint": (design.get("checkpoint"), design_checkpoint),
    "design.sampling_steps": (design.get("sampling_steps"), 500),
    "design.recycling_steps": (design.get("recycling_steps"), 3),
    # BoltzGen stores the per-batch diffusion sample count here.  The total
    # candidate count is represented by the number of batch iterations.
    "design.diffusion_samples": (design.get("diffusion_samples"), 1),
    "design.batch_size": (design["data"].get("batch_size"), 1),
    "design.num_workers": (design["data"].get("num_workers"), 4),
    "design.devices": (design["trainer"].get("devices"), 1),
    "design.yaml_path": (design["data"]["cfg"].get("yaml_path"), [spec_path]),
    "design.multiplicity": (
        design["data"]["cfg"].get("multiplicity"), expected_designs
    ),
    "design.skip_existing": (design["data"]["cfg"].get("skip_existing"), False),
    "design.mols": (design["data"]["cfg"].get("moldir"), mols_path),
    "design.use_kernels": (design.get("override", {}).get("use_kernels"), True),
    "inverse.checkpoint": (inverse.get("checkpoint"), inverse_checkpoint),
    "inverse.sampling_steps": (inverse.get("sampling_steps"), 200),
    "inverse.recycling_steps": (inverse.get("recycling_steps"), 3),
    "inverse.diffusion_samples": (inverse.get("diffusion_samples"), 1),
    "inverse.batch_size": (inverse["data"]["cfg"].get("batch_size"), 1),
    "inverse.num_workers": (inverse["data"]["cfg"].get("num_workers"), 4),
    "inverse.devices": (inverse["trainer"].get("devices"), 1),
    "inverse.max_seqs": (inverse["data"]["cfg"].get("max_seqs"), 1),
    "inverse.multiplicity": (inverse["data"]["cfg"].get("multiplicity"), 1),
    "inverse.skip_existing": (inverse["data"].get("skip_existing"), False),
    "inverse.mols": (inverse["data"]["cfg"].get("moldir"), mols_path),
    "inverse.use_kernels": (inverse.get("override", {}).get("use_kernels"), True),
    "folding.checkpoint": (folding.get("checkpoint"), folding_checkpoint),
    "folding.sampling_steps": (folding.get("sampling_steps"), 200),
    "folding.recycling_steps": (folding.get("recycling_steps"), 3),
    "folding.diffusion_samples": (folding.get("diffusion_samples"), 5),
    "folding.batch_size": (folding["data"]["cfg"].get("batch_size"), 1),
    "folding.num_workers": (folding["data"]["cfg"].get("num_workers"), 4),
    "folding.devices": (folding["trainer"].get("devices"), 1),
    "folding.skip_existing": (folding["data"].get("skip_existing"), False),
    "folding.mols": (folding["data"]["cfg"].get("moldir"), mols_path),
    "folding.use_kernels": (folding.get("override", {}).get("use_kernels"), True),
    "analysis.skip_existing": (analysis["data"].get("skip_existing"), False),
    "analysis.num_workers": (analysis["data"]["cfg"].get("num_workers"), 4),
    "analysis.mols": (analysis["data"]["cfg"].get("moldir"), mols_path),
    "analysis.liability_modality": (analysis.get("liability_modality"), "antibody"),
    "filtering.budget": (filtering.get("budget"), expected_designs),
    "filtering.filter_bindingsite": (filtering.get("filter_bindingsite"), True),
    "filtering.modality": (filtering.get("modality"), "antibody"),
}
for label, (actual, expected) in checks.items():
    if actual != expected:
        raise SystemExit(f"resolved config mismatch: {label} expected={expected!r} actual={actual!r}")

payload = {
    "schema_version": "WINDOWS_OWNER_RESOLVED_CONFIG_CONTRACT_V1",
    "status": "PASS",
    "expected_designs": expected_designs,
    "batch_size": 1,
    "fold_samples_per_candidate": 5,
    "checks": {label: actual for label, (actual, _) in checks.items()},
}
print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
PY
mv "$operator_logs/resolved_config_validation.stdout.txt" \
  "$operator_logs/resolved_config_contract.json"
(
  cd "$attempt_root"
  find config -type f -print0 | sort -z | xargs -0 sha256sum \
    > operator_logs/resolved_config.SHA256SUMS
)
config_prechecked=1

for stage in design inverse_folding folding analysis filtering; do
  run_logged "$stage" "$boltzgen_launcher" execute "$attempt_root" \
    --no_subprocess --steps "$stage"
done

run_logged validation env EXPECTED_DESIGNS="$num_designs" EXPECTED_FOLD_SAMPLES=5 \
  "$python_bin" -I "$validator" "$attempt_root"
mv "$operator_logs/validation.stdout.txt" "$operator_logs/cell_contract.json"
python3 -I -S - "$operator_logs/cell_contract.json" "$num_designs" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "PASS":
    raise SystemExit("output validator did not pass")
if payload.get("observed_unique_ids") != int(sys.argv[2]):
    raise SystemExit("candidate count mismatch")
if payload.get("fold_samples_per_candidate") != 5:
    raise SystemExit("fold sample count mismatch")
PY

kill -0 "$monitor_pid"
stop_monitor
pipeline_success=1
