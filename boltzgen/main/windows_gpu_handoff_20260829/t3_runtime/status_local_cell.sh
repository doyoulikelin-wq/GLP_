#!/usr/bin/env bash
# Read-only status projection for one local systemd single-GPU cell.

set -uo pipefail
umask 077

emit_state() {
  local state=$1
  local unit=${2:-}
  local detail=${3:-}
  python3 -I -S - "$CELL_ID" "$ATTEMPT_ID" "$state" "$unit" "$detail" <<'PY'
import json
import sys
payload = {
    "schema_version": "WSL2_LOCAL_CELL_STATUS_V1",
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": sys.argv[1],
    "attempt_id": sys.argv[2],
    "state": sys.argv[3],
}
if sys.argv[4]:
    payload["unit"] = sys.argv[4]
if sys.argv[5]:
    payload["detail"] = sys.argv[5]
print(json.dumps(payload, sort_keys=True))
PY
}

if [ "$#" -ne 3 ]; then
  printf 'status_local_cell: usage: status_local_cell.sh BG_WORK CELL_ID ATTEMPT_ID\n' >&2
  exit 64
fi
BG_WORK_INPUT=$1
CELL_ID=$2
ATTEMPT_ID=$3
if [[ ! "$CELL_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  emit_state BLOCKED_UNSAFE_IDENTIFIER "" "unsafe cell id"; exit 64
fi
if [[ ! "$ATTEMPT_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  emit_state BLOCKED_UNSAFE_IDENTIFIER "" "unsafe attempt id"; exit 64
fi
if [ ! -d "$BG_WORK_INPUT" ] || [ -L "$BG_WORK_INPUT" ]; then
  emit_state BLOCKED_BG_WORK_INVALID "" "BG_WORK is missing or unsafe"
  exit 66
fi
BG_WORK=$(readlink -f -- "$BG_WORK_INPUT")
if ! EXECUTOR_UID=$(python3 -I -S - "$BG_WORK" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path
bg = Path(sys.argv[1])
info = bg.stat(follow_symlinks=False)
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022):
    raise SystemExit(1)
contract = bg / "contract" / "environment_contract.json"
if contract.is_symlink() or not contract.is_file():
    raise SystemExit(1)
value = json.loads(contract.read_text(encoding="utf-8"))
uid = value.get("executor_uid")
if type(uid) is not int or uid != os.getuid():
    raise SystemExit(1)
print(uid)
PY
); then
  emit_state BLOCKED_BG_WORK_INVALID "" "BG_WORK/executor ownership contract is unsafe"
  exit 66
fi
BASE="$BG_WORK/local_submissions/$CELL_ID.$ATTEMPT_ID"
INTENT="$BASE.intent.json"
RECEIPT="$BASE.receipt.json"
ATTEMPT="$BG_WORK/runs/$CELL_ID/$ATTEMPT_ID"

if [ ! -f "$INTENT" ] || [ -L "$INTENT" ]; then
  emit_state BLOCKED_SUBMISSION_INTENT_MISSING "" "submission intent is missing or unsafe"
  exit 3
fi
set +e
INTENT_VALUES=$(python3 -I -S - "$INTENT" "$CELL_ID" "$ATTEMPT_ID" "$BG_WORK/run_local_cell.sh" "$EXECUTOR_UID" <<'PY'
import json
import re
import sys
from pathlib import Path

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result

path = Path(sys.argv[1])
with path.open(encoding="utf-8") as handle:
    value = json.load(handle, object_pairs_hook=no_duplicates)
expected = {
    "schema_version": "WSL2_LOCAL_SUBMISSION_INTENT_V1",
    "status": "SUBMISSION_INTENT",
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": sys.argv[2],
    "attempt_id": sys.argv[3],
    "executor_uid": int(sys.argv[5]),
}
for key, item in expected.items():
    if value.get(key) != item:
        raise ValueError(f"intent mismatch: {key}")
unit = value.get("unit")
contract_sha = value.get("cell_contract_sha256")
contract_path = value.get("cell_contract_path")
if not isinstance(unit, str) or re.fullmatch(r"boltzgen-local-[0-9a-f]{64}\.service", unit) is None:
    raise ValueError("invalid unit")
if not isinstance(contract_sha, str) or re.fullmatch(r"[0-9a-f]{64}", contract_sha) is None:
    raise ValueError("invalid contract SHA")
if not isinstance(contract_path, str) or not contract_path.startswith("/"):
    raise ValueError("invalid contract path")
token = value.get("submission_token")
if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
    raise ValueError("invalid submission token")
if value.get("runner_path") != sys.argv[4]:
    raise ValueError("unexpected runner path")
exec_start_sha = value.get("exec_start_sha256")
if not isinstance(exec_start_sha, str) or re.fullmatch(r"[0-9a-f]{64}", exec_start_sha) is None:
    raise ValueError("invalid ExecStart SHA")
if set(value) != set(expected) | {
    "unit", "cell_contract_sha256", "cell_contract_path", "submission_token",
    "runner_path", "exec_start_sha256", "intent_at_utc",
}:
    raise ValueError("intent field set differs")
print(unit)
print(contract_sha)
print(contract_path)
print(token)
print(value["runner_path"])
print(exec_start_sha)
PY
)
INTENT_RC=$?
set -e
if [ "$INTENT_RC" -ne 0 ]; then
  emit_state BLOCKED_SUBMISSION_INTENT_INVALID "" "submission intent cannot be trusted"
  exit 3
fi
mapfile -t INTENT_FIELDS <<< "$INTENT_VALUES"
if [ "${#INTENT_FIELDS[@]}" -ne 6 ]; then
  emit_state BLOCKED_SUBMISSION_INTENT_INVALID "" "submission intent fields are incomplete"
  exit 3
fi
UNIT=${INTENT_FIELDS[0]}
CONTRACT_SHA=${INTENT_FIELDS[1]}
CONTRACT=${INTENT_FIELDS[2]}
SUBMISSION_TOKEN=${INTENT_FIELDS[3]}
RUNNER=${INTENT_FIELDS[4]}
EXEC_START_SHA=${INTENT_FIELDS[5]}
SERVICE_USER=$(id -un)
SERVICE_HOME=$(getent passwd "$EXECUTOR_UID" | cut -d: -f6)
SERVICE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/wsl/lib
TRAMPOLINE_CODE='import os,re,sys;token,runner,bg_work,contract,path,home,user,uid=sys.argv[1:];invocation=os.environ.get("INVOCATION_ID","");re.fullmatch(r"[0-9a-f]{32}",invocation) or sys.exit(75);environment={"PATH":path,"HOME":home,"USER":user,"LOGNAME":user,"XDG_RUNTIME_DIR":f"/run/user/{uid}","BG_SUBMISSION_TOKEN":token,"INVOCATION_ID":invocation};os.execve(runner,[runner,bg_work,contract],environment)'
if ! EXPECTED_EXEC_JSON=$(python3 -I -S - \
    "$TRAMPOLINE_CODE" "$SERVICE_PATH" "$SERVICE_HOME" "$SERVICE_USER" "$EXECUTOR_UID" \
    "$SUBMISSION_TOKEN" "$RUNNER" "$BG_WORK" "$CONTRACT" "$EXEC_START_SHA" <<'PY'
import hashlib
import json
import sys
code, path, home, user, uid, token, runner, bg_work, contract, expected_sha = sys.argv[1:]
argv = [
    "/usr/bin/python3", "-I", "-S", "-c", code, token, runner, bg_work,
    contract, path, home, user, uid,
]
canonical = json.dumps(argv, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
if hashlib.sha256(canonical.encode()).hexdigest() != expected_sha:
    raise SystemExit(1)
print(canonical)
PY
); then
  emit_state BLOCKED_SUBMISSION_INTENT_INVALID "$UNIT" "intent ExecStart binding is inconsistent"
  exit 3
fi

QUERY_ACTIVE_STATE=
QUERY_SUB_STATE=
QUERY_RESULT=
QUERY_INVOCATION_ID=
QUERY_EXEC_START_SHA=
query_exact_unit() {
  local raw object_raw object_path exec_raw show_rc object_rc exec_rc parsed parse_rc
  raw=$(systemctl --user show "$UNIT" \
    --property=Id --property=LoadState --property=ActiveState \
    --property=SubState --property=Result --property=Description \
    --property=Restart --property=Type --property=InvocationID \
    --property=KillMode --property=UMask --no-pager 2>/dev/null)
  show_rc=$?
  object_raw=$(busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
    org.freedesktop.systemd1.Manager GetUnit s "$UNIT" 2>/dev/null)
  object_rc=$?
  object_path=$(python3 -I -S -c 'import json,sys; raw=sys.argv[1]; raw.startswith("o ") or sys.exit(2); value=json.loads(raw[2:]); isinstance(value,str) and value.startswith("/org/freedesktop/systemd1/unit/") or sys.exit(2); print(value)' "$object_raw" 2>/dev/null)
  exec_raw=$(busctl --json=short --user get-property org.freedesktop.systemd1 "$object_path" \
    org.freedesktop.systemd1.Service ExecStart 2>/dev/null)
  exec_rc=$?
  [ "$show_rc" -eq 0 ] && [ "$object_rc" -eq 0 ] && [ -n "$object_path" ] \
    && [ "$exec_rc" -eq 0 ] || return 10
  parsed=$(printf '%s\n' "$raw" | python3 -I -S -c '
import hashlib
import json
import sys
unit = sys.argv[1]
description = sys.argv[2]
expected_argv = json.loads(sys.argv[3])
exec_payload = json.loads(sys.argv[4])
keys = {"Id", "LoadState", "ActiveState", "SubState", "Result", "Description", "Restart", "Type", "InvocationID", "KillMode", "UMask"}
values = {}
for raw in sys.stdin:
    line = raw.rstrip("\n")
    if not line or "=" not in line:
        raise SystemExit(2)
    key, value = line.split("=", 1)
    if key not in keys or key in values or not value:
        raise SystemExit(2)
    values[key] = value
if set(values) != keys or values["Id"] != unit or values["LoadState"] != "loaded":
    raise SystemExit(2)
if (values["Description"] != description or values["Restart"] != "no" or values["Type"] != "exec"
        or values["KillMode"] != "control-group" or values["UMask"] != "0077"):
    raise SystemExit(2)
if len(values["InvocationID"]) != 32 or any(ch not in "0123456789abcdef" for ch in values["InvocationID"]):
    raise SystemExit(2)
entries = exec_payload.get("data") if isinstance(exec_payload, dict) else None
if (exec_payload.get("type") != "a(sasbttttuii)" or not isinstance(entries, list)
        or len(entries) != 1 or not isinstance(entries[0], list) or len(entries[0]) != 10):
    raise SystemExit(2)
entry = entries[0]
if entry[0] != "/usr/bin/python3" or entry[1] != expected_argv or entry[2] is not False:
    raise SystemExit(2)
canonical = json.dumps(entry[1], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
print(values["ActiveState"])
print(values["SubState"])
print(values["Result"])
print(values["InvocationID"])
print(hashlib.sha256(canonical.encode()).hexdigest())
' "$UNIT" "boltzgen-local:$CONTRACT_SHA:$SUBMISSION_TOKEN" "$EXPECTED_EXEC_JSON" "$exec_raw")
  parse_rc=$?
  [ "$parse_rc" -eq 0 ] || return 11
  mapfile -t QUERY_FIELDS <<< "$parsed"
  [ "${#QUERY_FIELDS[@]}" -eq 5 ] || return 11
  QUERY_ACTIVE_STATE=${QUERY_FIELDS[0]}
  QUERY_SUB_STATE=${QUERY_FIELDS[1]}
  QUERY_RESULT=${QUERY_FIELDS[2]}
  QUERY_INVOCATION_ID=${QUERY_FIELDS[3]}
  QUERY_EXEC_START_SHA=${QUERY_FIELDS[4]}
  [ "$QUERY_EXEC_START_SHA" = "$EXEC_START_SHA" ] || return 11
  return 0
}

if [ ! -f "$RECEIPT" ] || [ -L "$RECEIPT" ]; then
  set +e
  query_exact_unit
  QUERY_RC=$?
  set -e
  if [ "$QUERY_RC" -eq 10 ]; then
    emit_state BLOCKED_UNIT_DISAPPEARED "$UNIT" "intent exists, receipt is absent, and unit cannot be found"
  elif [ "$QUERY_RC" -eq 11 ]; then
    emit_state BLOCKED_UNIT_AMBIGUOUS "$UNIT" "intent exists, receipt is absent, and unit identity is ambiguous"
  else
    emit_state BLOCKED_SUBMISSION_RECEIPT_MISSING "$UNIT" "unit exists but submission receipt is absent"
  fi
  exit 3
fi

set +e
RECEIPT_VALUES=$(python3 -I -S - "$RECEIPT" "$CELL_ID" "$ATTEMPT_ID" "$CONTRACT" "$CONTRACT_SHA" "$UNIT" "$RUNNER" "$SUBMISSION_TOKEN" "$EXECUTOR_UID" "$EXEC_START_SHA" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result

path = Path(sys.argv[1])
with path.open(encoding="utf-8") as handle:
    value = json.load(handle, object_pairs_hook=no_duplicates)
expected = {
    "schema_version": "WSL2_LOCAL_SUBMISSION_RECEIPT_V1",
    "status": "SUBMITTED",
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": sys.argv[2],
    "attempt_id": sys.argv[3],
    "cell_contract_path": sys.argv[4],
    "cell_contract_sha256": sys.argv[5],
    "unit": sys.argv[6],
    "runner_path": sys.argv[7],
    "submission_token": sys.argv[8],
    "executor_uid": int(sys.argv[9]),
    "exec_start_sha256": sys.argv[10],
}
for key, item in expected.items():
    if value.get(key) != item:
        raise ValueError(f"receipt mismatch: {key}")
if set(value) != set(expected) | {
    "active_state_at_receipt", "sub_state_at_receipt", "unit_result_at_receipt",
    "invocation_id", "submitted_at_utc",
}:
    raise ValueError("receipt field set differs")
invocation = value.get("invocation_id")
if not isinstance(invocation, str) or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
    raise ValueError("invalid receipt InvocationID")
print(invocation)
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
)
RECEIPT_RC=$?
set -e
if [ "$RECEIPT_RC" -ne 0 ]; then
  emit_state BLOCKED_SUBMISSION_RECEIPT_INVALID "$UNIT" "submission receipt cannot be trusted"
  exit 3
fi
mapfile -t RECEIPT_FIELDS <<< "$RECEIPT_VALUES"
if [ "${#RECEIPT_FIELDS[@]}" -ne 2 ]; then
  emit_state BLOCKED_SUBMISSION_RECEIPT_INVALID "$UNIT" "submission receipt fields are incomplete"
  exit 3
fi
RECEIPT_INVOCATION=${RECEIPT_FIELDS[0]}
SUBMISSION_RECEIPT_SHA=${RECEIPT_FIELDS[1]}

SUCCESS_CELL="$ATTEMPT/operator_logs/cell.SUCCESS.json"
SUCCESS_PROBE="$ATTEMPT/operator_logs/probe.SUCCESS.json"
FAILURE_CELL="$ATTEMPT/operator_logs/cell.FAILURE.json"
FAILURE_PROBE="$ATTEMPT/operator_logs/probe.FAILURE.json"
EMERGENCY_CELL="$ATTEMPT/operator_logs/cell.EMERGENCY_FAILURE.json"
EMERGENCY_PROBE="$ATTEMPT/operator_logs/probe.EMERGENCY_FAILURE.json"
TERMINAL_MARKER=
TERMINAL_COUNT=0
for candidate in "$SUCCESS_CELL" "$SUCCESS_PROBE" "$FAILURE_CELL" "$FAILURE_PROBE" "$EMERGENCY_CELL" "$EMERGENCY_PROBE"; do
  if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    TERMINAL_MARKER=$candidate
    TERMINAL_COUNT=$((TERMINAL_COUNT + 1))
  fi
done
if [ "$TERMINAL_COUNT" -gt 1 ]; then
  emit_state BLOCKED_TERMINAL_MARKER_AMBIGUOUS "$UNIT" "multiple terminal markers exist"
  exit 3
fi
if [ "$TERMINAL_COUNT" -eq 1 ]; then
  set +e
  TERMINAL_KIND=$(python3 -I -S - "$TERMINAL_MARKER" "$ATTEMPT" "$CONTRACT" "$CONTRACT_SHA" \
    "$RECEIPT" "$SUBMISSION_RECEIPT_SHA" "$UNIT" "$SUBMISSION_TOKEN" \
    "$RECEIPT_INVOCATION" "$EXECUTOR_UID" "$EXEC_START_SHA" "$BG_WORK" <<'PY'
import csv
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

ENGINEERING_MEMORY_PROBE_RUN_KIND = "ENGINEERING_MEMORY_PROBE"
ENGINEERING_MEMORY_PROBE_STATUS = "ENGINEERING_MEMORY_PROBE_ONLY"
BLOCKED_GPU_MEMORY_STATUS = "BLOCKED_GPU_MEMORY"
ENGINEERING_MEMORY_PROBE_ID = re.compile(
    r"6xym_(diverse|adherence)_batch1_engineering"
)
ENGINEERING_6XYM_SPEC_SUFFIX = (
    "project_input", "specs", "08_pdb_00006xym-A", "design.yaml"
)
ENGINEERING_PROBE_CHECKPOINTS = {
    "diverse": "boltzgen1_diverse.ckpt",
    "adherence": "boltzgen1_adherence.ckpt",
}
ENGINEERING_PROBE_FIELDS = (
    "probe_id", "checkpoint_name", "checkpoint_sha256"
)
GPU_MONITOR_HEADER = (
    "timestamp", "index", "name", "memory.total [MiB]", "memory.used [MiB]",
    "utilization.gpu [%]", "power.draw [W]",
)
GPU_MEMORY_VALUE = re.compile(r"([0-9]+(?:\.[0-9]+)?) MiB")
GPU_STAGE_NAMES = ("design", "inverse_folding", "folding", "analysis", "filtering")
GPU_OOM_MARKERS = (
    b"cuda out of memory",
    b"torch.cuda.outofmemoryerror",
    b"cuda error: out of memory",
    b"cudnn_status_alloc_failed",
)
PROBE_EVIDENCE_RELATIVES = frozenset(
    {
        "operator_logs/peak_memory_fraction.txt",
        "operator_logs/gpu_monitor.csv",
        *(
            f"operator_logs/{stage}.{suffix}"
            for stage in GPU_STAGE_NAMES
            for suffix in ("exit_code.txt", "stderr.txt")
        ),
    }
)
TERMINAL_CAPTURE_LIMIT = 16 * 1024 * 1024
PROBE_MONITOR_CAPTURE_LIMIT = 16 * 1024 * 1024
PROBE_STDERR_CAPTURE_LIMIT = 8 * 1024 * 1024
PROBE_SMALL_CAPTURE_LIMIT = 64 * 1024

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def classify_engineering_probe(contract):
    """Return exact T6 status; reject every approximate ENGINEERING probe."""

    if contract.get("stage_class") != "ENGINEERING":
        return False
    run_kind = contract.get("run_kind")
    success_status = contract.get("success_status")
    cell_id = contract.get("cell_id")
    spec_value = contract.get("spec_path")
    if not all(isinstance(item, str) for item in (run_kind, success_status, cell_id, spec_value)):
        raise ValueError("engineering contract classification fields are invalid")
    spec_path = Path(spec_value)
    is_6xym_spec = tuple(spec_path.parts[-4:]) == ENGINEERING_6XYM_SPEC_SUFFIX
    probe_hint = (
        "PROBE" in run_kind.upper()
        or "PROBE" in success_status.upper()
        or cell_id.startswith("6xym_")
        or is_6xym_spec
        or any(field in contract for field in ENGINEERING_PROBE_FIELDS)
    )
    if not probe_hint:
        return False

    match = ENGINEERING_MEMORY_PROBE_ID.fullmatch(cell_id)
    if (
        run_kind != ENGINEERING_MEMORY_PROBE_RUN_KIND
        or success_status != ENGINEERING_MEMORY_PROBE_STATUS
        or match is None
        or contract.get("probe_id") != cell_id
        or contract.get("checkpoint_name") != match.group(1)
    ):
        raise ValueError("ENGINEERING probe is not the exact T6 memory-probe contract")
    checkpoint_name = match.group(1)
    expected_name = ENGINEERING_PROBE_CHECKPOINTS[checkpoint_name]
    checkpoint_path = contract.get("design_checkpoint")
    checkpoint_sha = contract.get("checkpoint_sha256")
    if (
        not isinstance(checkpoint_path, str)
        or Path(checkpoint_path).name != expected_name
        or not isinstance(checkpoint_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha) is None
        or checkpoint_sha != contract.get("design_checkpoint_sha256")
    ):
        raise ValueError("engineering probe design checkpoint name/SHA mismatch")
    if not is_6xym_spec:
        raise ValueError("engineering probe does not bind the frozen 6XYM specification")
    for field, expected in {
        "expected_designs": 1,
        "budget": 1,
        "diffusion_batch_size": 1,
        "inverse_fold_num_sequences": 1,
        "expected_fold_samples": 5,
    }.items():
        value = contract.get(field)
        if type(value) is not int or value != expected:
            raise ValueError(f"engineering probe {field} must equal {expected}")
    return True


def recompute_peak_memory_fraction(captures):
    """Bind the probe marker to its derived receipt and canonical GPU telemetry."""

    try:
        peak_bytes = captures["operator_logs/peak_memory_fraction.txt"]
        monitor_bytes = captures["operator_logs/gpu_monitor.csv"]
    except KeyError as exc:
        raise ValueError("probe peak-memory evidence is missing or unsafe")
    try:
        peak_text = peak_bytes.decode("ascii")
        raw = monitor_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("probe peak-memory evidence encoding is invalid") from exc
    if not peak_text.endswith("\n") or peak_text.count("\n") != 1 or "\r" in peak_text:
        raise ValueError("probe peak-memory receipt framing is invalid")
    try:
        declared = float(peak_text[:-1])
    except ValueError as exc:
        raise ValueError("probe peak-memory receipt is not numeric") from exc
    if not raw or not raw.endswith("\n") or "\r" in raw or "\0" in raw:
        raise ValueError("GPU monitor CSV framing is invalid")
    rows = csv.reader(raw.splitlines(), skipinitialspace=True, strict=True)
    try:
        header = tuple(next(rows))
    except (StopIteration, csv.Error) as exc:
        raise ValueError("GPU monitor CSV lacks a valid header") from exc
    if header != GPU_MONITOR_HEADER:
        raise ValueError("GPU monitor CSV header differs from the frozen schema")
    identity = None
    observed_peak = -1.0
    count = 0
    try:
        for row in rows:
            if len(row) != len(GPU_MONITOR_HEADER):
                raise ValueError("GPU monitor CSV row width is invalid")
            timestamp, index, name, total_text, used_text, utilization, power = (
                item.strip() for item in row
            )
            if not timestamp or not index.isdecimal() or not name or not utilization or not power:
                raise ValueError("GPU monitor CSV row identity is invalid")
            total_match = GPU_MEMORY_VALUE.fullmatch(total_text)
            used_match = GPU_MEMORY_VALUE.fullmatch(used_text)
            if total_match is None or used_match is None:
                raise ValueError("GPU monitor CSV memory value is invalid")
            total = float(total_match.group(1))
            used = float(used_match.group(1))
            if (
                not math.isfinite(total)
                or not math.isfinite(used)
                or total <= 0
                or used < 0
                or used > total
            ):
                raise ValueError("GPU monitor CSV memory range is invalid")
            current_identity = (index, name, total)
            if identity is None:
                identity = current_identity
            elif current_identity != identity:
                raise ValueError("GPU monitor contains multiple or drifting GPUs")
            observed_peak = max(observed_peak, used / total)
            count += 1
    except csv.Error as exc:
        raise ValueError("GPU monitor CSV parsing failed") from exc
    if (
        count < 1
        or not math.isfinite(declared)
        or not math.isfinite(observed_peak)
        or peak_text != f"{observed_peak:.17g}\n"
    ):
        raise ValueError("probe peak-memory receipt/telemetry mismatch")
    return declared


def recompute_engineering_gpu_oom(captures):
    """Reclassify T6 failure solely from canonical GPU-stage evidence."""

    for stage in GPU_STAGE_NAMES:
        exit_key = f"operator_logs/{stage}.exit_code.txt"
        stderr_key = f"operator_logs/{stage}.stderr.txt"
        if exit_key not in captures or stderr_key not in captures:
            continue
        try:
            exit_text = captures[exit_key].decode("ascii")
        except UnicodeError as exc:
            raise ValueError("GPU-stage OOM exit evidence encoding is invalid") from exc
        if re.fullmatch(r"(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\n", exit_text) is None:
            continue
        stderr = captures[stderr_key].lower()
        if any(marker in stderr for marker in GPU_OOM_MARKERS):
            return True
    return False

def digest_fd(descriptor):
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("non-regular file")
    value = hashlib.sha256()
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        value.update(block)
    after = os.fstat(descriptor)
    identity = lambda item: (
        item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns
    )
    if identity(before) != identity(after):
        raise ValueError("file changed while hashing")
    return value.hexdigest()

def identity(item):
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )

def open_canonical_file(raw):
    if not raw.startswith("/") or os.path.normpath(raw) != raw or os.path.realpath(raw) != raw:
        raise ValueError("contract path is not canonical")
    components = PurePosixPath(raw).parts[1:]
    if not components or any(item in {"", ".", ".."} for item in components):
        raise ValueError("invalid contract path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | os.O_DIRECTORY
    directory = os.open("/", directory_flags)
    try:
        for component in components[:-1]:
            following = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(components[-1], flags, dir_fd=directory)
    finally:
        os.close(directory)
    return descriptor

def read_member(directory_fd, name, *, capture_limit=0):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("member identity changed before open")
        chunks = []
        captured_size = 0
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            if capture_limit:
                captured_size += len(block)
                if captured_size > capture_limit:
                    raise ValueError("captured evidence exceeds its safe size bound")
                chunks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
        if identity(opened) != identity(after):
            raise ValueError("member changed while reading")
        return digest.hexdigest(), b"".join(chunks)
    finally:
        os.close(descriptor)

def walk(directory_fd, prefix=""):
    files = {}
    captures = {}
    identities = {}
    for name in sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8")):
        if not name or "/" in name or any(ord(ch) < 32 for ch in name):
            raise ValueError("unsafe output member name")
        relative = f"{prefix}/{name}" if prefix else name
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise ValueError(f"output symlink: {relative}")
        if stat.S_ISDIR(before.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("directory identity changed")
                child_files, child_captures, child_identities = walk(child, relative)
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if identity(opened) != identity(after):
                    raise ValueError("directory replaced during walk")
                files.update(child_files)
                captures.update(child_captures)
                identities[relative] = identity(after)
                identities.update(child_identities)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"output special file: {relative}")
        terminal_capture = relative in {
            "operator_logs/output_SHA256SUMS",
            "operator_logs/emergency_output_SHA256SUMS",
        } or relative.endswith(
            (".SUCCESS.json", ".FAILURE.json", ".EMERGENCY_FAILURE.json")
        )
        if terminal_capture:
            capture_limit = TERMINAL_CAPTURE_LIMIT
        elif relative == "operator_logs/gpu_monitor.csv":
            capture_limit = PROBE_MONITOR_CAPTURE_LIMIT
        elif relative.endswith(".stderr.txt") and relative in PROBE_EVIDENCE_RELATIVES:
            capture_limit = PROBE_STDERR_CAPTURE_LIMIT
        elif relative in PROBE_EVIDENCE_RELATIVES:
            capture_limit = PROBE_SMALL_CAPTURE_LIMIT
        else:
            capture_limit = 0
        digest, content = read_member(
            directory_fd,
            name,
            capture_limit=capture_limit,
        )
        files[relative] = digest
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if identity(before) != identity(after):
            raise ValueError("file path changed during read")
        identities[relative] = identity(after)
        if capture_limit:
            captures[relative] = content
    return files, captures, identities

def reopen_snapshot_member(root_fd, relative, expected):
    components = relative.split("/")
    if not components or any(item in {"", ".", ".."} for item in components):
        raise ValueError("invalid snapshot path")
    base_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory = os.dup(root_fd)
    try:
        for component in components[:-1]:
            following = os.open(component, base_flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = following
        expected_is_directory = stat.S_ISDIR(expected[2])
        flags = base_flags | (os.O_DIRECTORY if expected_is_directory else 0)
        descriptor = os.open(components[-1], flags, dir_fd=directory)
        current_path = os.stat(components[-1], dir_fd=directory, follow_symlinks=False)
    finally:
        os.close(directory)
    current_fd = os.fstat(descriptor)
    if identity(current_path) != expected or identity(current_fd) != expected:
        os.close(descriptor)
        raise ValueError(f"output member identity changed after hashing: {relative}")
    return descriptor

def close_output_snapshot(root_fd, identities, terminal_relatives):
    terminal_digests = {}
    for relative in sorted(identities, key=lambda item: item.encode("utf-8")):
        descriptor = reopen_snapshot_member(root_fd, relative, identities[relative])
        try:
            if relative in terminal_relatives:
                terminal_digests[relative] = digest_fd(descriptor)
        finally:
            os.close(descriptor)
    return terminal_digests

marker = Path(sys.argv[1])
root = Path(sys.argv[2])
contract_path = sys.argv[3]
intent_contract_sha = sys.argv[4]
submission_path = Path(sys.argv[5])
submission_sha = sys.argv[6]
expected_unit = sys.argv[7]
raw_token = sys.argv[8]
expected_invocation = sys.argv[9]
expected_uid = int(sys.argv[10])
expected_exec_sha = sys.argv[11]
bg_work = Path(sys.argv[12])
if marker.parent != root / "operator_logs":
    raise ValueError("terminal marker is outside the output root")
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
root_identity = identity(os.fstat(root_fd))
observed, captures, output_identities = walk(root_fd)
root_after = os.stat(root, follow_symlinks=False)
if root_identity != identity(root_after):
    raise ValueError("output root replaced during verification")
marker_relative = f"operator_logs/{marker.name}"
emergency = marker.name.endswith("EMERGENCY_FAILURE.json")
manifest_relative = (
    "operator_logs/emergency_output_SHA256SUMS"
    if emergency else "operator_logs/output_SHA256SUMS"
)
try:
    marker_bytes = captures[marker_relative]
    manifest_bytes = captures[manifest_relative]
except KeyError as exc:
    raise ValueError("missing terminal evidence") from exc
try:
    value = json.loads(marker_bytes.decode("utf-8"), object_pairs_hook=no_duplicates)
except (UnicodeError, json.JSONDecodeError) as exc:
    raise ValueError("invalid terminal marker JSON") from exc
if value.get("executor_kind") != "WSL2_SYSTEMD_SINGLE_GPU":
    raise ValueError("wrong executor")
execution_contract_sha = value.get("execution_contract_sha256")
if execution_contract_sha != intent_contract_sha:
    raise ValueError("terminal marker contract mismatch")
manifest_sha_key = "emergency_output_manifest_sha256" if emergency else "output_manifest_sha256"
manifest_count_key = "emergency_output_manifest_entry_count" if emergency else "output_manifest_entry_count"
expected_manifest_sha = value.get(manifest_sha_key)
if not isinstance(expected_manifest_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha) is None:
    raise ValueError("invalid output manifest SHA")
if observed[manifest_relative] != expected_manifest_sha:
    raise ValueError("output manifest mismatch")
contract_fd = open_canonical_file(contract_path)
try:
    actual_contract_sha = digest_fd(contract_fd)
    os.lseek(contract_fd, 0, os.SEEK_SET)
    contract_bytes = b""
    while True:
        block = os.read(contract_fd, 1024 * 1024)
        if not block:
            break
        contract_bytes += block
    contract_identity = identity(os.fstat(contract_fd))
finally:
    os.close(contract_fd)
contract_now = os.stat(contract_path, follow_symlinks=False)
if contract_identity != identity(contract_now):
    raise ValueError("contract path changed during verification")
if actual_contract_sha != intent_contract_sha or actual_contract_sha != execution_contract_sha:
    raise ValueError("live contract does not match intent and marker")
try:
    contract = json.loads(contract_bytes.decode("utf-8"), object_pairs_hook=no_duplicates)
except (UnicodeError, json.JSONDecodeError) as exc:
    raise ValueError("invalid live cell contract") from exc
for key in ("cell_id", "attempt_id", "run_kind"):
    if value.get(key) != contract.get(key):
        raise ValueError(f"terminal marker/cell contract mismatch: {key}")
engineering_probe = classify_engineering_probe(contract)
formal_probe = (
    contract.get("stage_class") == "FORMAL"
    and "PROBE" in str(contract.get("run_kind", "")).upper()
)
probe = engineering_probe or formal_probe
expected_stem = "probe" if probe else "cell"
expected_name = (
    f"{expected_stem}.EMERGENCY_FAILURE.json" if emergency else
    f"{expected_stem}.SUCCESS.json" if marker.name.endswith("SUCCESS.json") else
    f"{expected_stem}.FAILURE.json"
)
if marker.name != expected_name:
    raise ValueError("terminal marker filename/run-kind mismatch")
for marker_key, contract_key in (
    ("environment_receipt_sha256", "environment_receipt_sha256"),
    ("model_inputs_manifest_sha256", "model_inputs_manifest_sha256"),
    ("runtime_scripts_manifest_sha256", "runtime_scripts_manifest_sha256"),
    ("spec_gate_bundle_sha256", "spec_gate_bundle_sha256"),
):
    if value.get(marker_key) != contract.get(contract_key):
        raise ValueError(f"terminal marker binding mismatch: {marker_key}")
monitor_sha = value.get("monitor_stopped_sha256")
if not isinstance(monitor_sha, str) or re.fullmatch(r"[0-9a-f]{64}", monitor_sha) is None:
    raise ValueError("invalid stopped-monitor binding")

sha_fields = {
    "spec_sha256", "design_checkpoint_sha256", "inverse_fold_checkpoint_sha256",
    "folding_checkpoint_sha256", "mols_sha256", "model_inputs_manifest_sha256",
    "runtime_scripts_manifest_sha256", "spec_gate_bundle_sha256",
}
if "input_and_model_manifest_sha256" in contract:
    sha_fields.add("input_and_model_manifest_sha256")
checked_binding_fields = (
    {"runtime_scripts_manifest_sha256", "model_inputs_manifest_sha256", "spec_gate_bundle_sha256"}
    if emergency else sha_fields
)
for key in checked_binding_fields:
    if value.get(key) != contract.get(key):
        raise ValueError(f"terminal marker frozen-input mismatch: {key}")
normal_common = {
    "schema_version", "status", "terminal_status", "pipeline_exit_code", "executor_kind",
    "cell_id", "attempt_id", "run_kind", "formal_g1", "formal_g1_receipt_sha256",
    "environment_manifest_sha256", "completed_at_utc", "execution_contract_sha256",
    "environment_receipt_sha256", "monitor_stopped_sha256", "monitor_healthy",
    "submission_receipt_sha256", "systemd_unit", "submission_token_sha256",
    "invocation_id", "executor_uid", "exec_start_sha256", "validator_sha256",
    "finalizer_sha256", "output_manifest_sha256", "output_manifest_entry_count",
    "evidence_freeze_schema_version", "evidence_files_read_only",
    "evidence_directories_read_only_except_terminal_parents",
    "terminal_publication_parents_mutable",
} | sha_fields
success_only = {
    "cell_contract_sha256", "validation_sha256", "resolved_config_manifest_sha256",
}
probe_only = {
    "probe_id", "checkpoint_name", "checkpoint_sha256", "num_designs",
    "diffusion_batch_size", "fold_samples", "peak_memory_fraction",
}
failure_only = {"failure_class"}
emergency_keys = {
    "schema_version", "status", "terminal_status", "pipeline_exit_code", "failure_class",
    "executor_kind", "cell_id", "attempt_id", "run_kind", "formal_g1",
    "formal_g1_receipt_sha256", "environment_manifest_sha256", "completed_at_utc", "execution_contract_sha256",
    "environment_receipt_sha256", "monitor_stopped_sha256", "monitor_healthy",
    "submission_receipt_sha256", "systemd_unit", "submission_token_sha256",
    "invocation_id", "executor_uid", "exec_start_sha256", "finalizer_exit_code",
    "finalizer_log_sha256", "finalizer_sha256", "runtime_scripts_manifest_sha256",
    "model_inputs_manifest_sha256", "spec_gate_bundle_sha256",
    "emergency_output_manifest_sha256", "emergency_output_manifest_entry_count",
    "evidence_freeze_schema_version", "evidence_files_read_only",
    "evidence_directories_read_only",
}
if emergency:
    required_keys = emergency_keys
elif marker.name.endswith("SUCCESS.json"):
    required_keys = normal_common | success_only | (probe_only if probe else set())
else:
    required_keys = normal_common | failure_only
if set(value) != required_keys:
    raise ValueError(
        f"terminal marker field set differs: missing={sorted(required_keys - set(value))} "
        f"extra={sorted(set(value) - required_keys)}"
    )
if not emergency and marker.name.endswith("SUCCESS.json") and probe:
    expected_checkpoint_sha = contract.get(
        "checkpoint_sha256", contract.get("design_checkpoint_sha256")
    )
    expected_probe_values = {
        "probe_id": contract.get("probe_id"),
        "checkpoint_name": contract.get("checkpoint_name"),
        "checkpoint_sha256": expected_checkpoint_sha,
        "num_designs": contract.get("expected_designs"),
        "diffusion_batch_size": contract.get("diffusion_batch_size"),
        "fold_samples": contract.get("expected_fold_samples"),
    }
    for key, expected in expected_probe_values.items():
        if value.get(key) != expected:
            raise ValueError(f"successful probe marker mismatch: {key}")
    peak = value.get("peak_memory_fraction")
    peak_limit = 1.0 if engineering_probe else 0.90
    if (
        isinstance(peak, bool)
        or not isinstance(peak, (int, float))
        or not math.isfinite(peak)
        or not 0 < peak <= peak_limit
        or peak != recompute_peak_memory_fraction(captures)
    ):
        raise ValueError("successful probe marker peak-memory value is invalid")
if (submission_path.is_symlink() or not submission_path.is_file()
        or value.get("submission_receipt_sha256") != submission_sha
        or hashlib.sha256(submission_path.read_bytes()).hexdigest() != submission_sha):
    raise ValueError("submission receipt binding mismatch")
if (value.get("systemd_unit") != expected_unit
        or value.get("submission_token_sha256") != hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        or value.get("invocation_id") != expected_invocation
        or value.get("executor_uid") != expected_uid
        or value.get("exec_start_sha256") != expected_exec_sha):
    raise ValueError("terminal systemd/executor binding mismatch")
environment_path = Path(contract["environment_receipt"])
if (environment_path.is_symlink() or not environment_path.is_file()
        or observed.get("operator_logs/monitor.stopped.json") != monitor_sha
        or hashlib.sha256(environment_path.read_bytes()).hexdigest() != value["environment_receipt_sha256"]):
    raise ValueError("terminal environment/monitor live binding mismatch")
monitor = json.loads((root / "operator_logs/monitor.stopped.json").read_text(encoding="utf-8"))
if (monitor.get("schema_version") != "WSL2_LOCAL_GPU_MONITOR_STOP_V1"
        or monitor.get("status") != "STOPPED" or monitor.get("wait_completed") is not True
        or type(monitor.get("monitor_started")) is not bool
        or type(monitor.get("monitor_healthy")) is not bool
        or value.get("monitor_healthy") is not monitor.get("monitor_healthy")):
    raise ValueError("stopped-monitor schema/binding mismatch")
if set(monitor) != {
    "schema_version", "status", "wait_completed", "monitor_started", "monitor_pid",
    "monitor_probe_exit_code", "wait_exit_code", "monitor_healthy", "stopped_at_utc",
}:
    raise ValueError("stopped-monitor field set differs")
if monitor["monitor_started"]:
    if type(monitor.get("monitor_pid")) is not int or monitor["monitor_pid"] < 1:
        raise ValueError("started monitor lacks PID")
elif monitor.get("monitor_pid") is not None:
    raise ValueError("non-started monitor must have null PID")
validator_path = bg_work / "software/validate_cell_output.py"
finalizer_path = bg_work / "software/finalize_local_attempt.py"
runtime_manifest = Path(contract["runtime_scripts_manifest_path"])
runtime_members = tuple(sorted((
    "run_local_cell.sh", "software/finalize_local_attempt.py",
    "software/validate_cell_output.py", "status_local_cell.sh",
    "submit_local_once.sh", "verify_gpu_env_stage.sh",
), key=lambda item: item.encode("utf-8")))
if (runtime_manifest != bg_work / "gpu_runtime_scripts_SHA256SUMS"
        or runtime_manifest.is_symlink() or not runtime_manifest.is_file()
        or hashlib.sha256(runtime_manifest.read_bytes()).hexdigest() != contract["runtime_scripts_manifest_sha256"]):
    raise ValueError("runtime manifest binding mismatch")
runtime_records = []
for line in runtime_manifest.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  \./([^\n\r\0]+)", line)
    if match is None:
        raise ValueError("runtime manifest line invalid")
    expected_sha, relative = match.groups()
    member = bg_work / relative
    if (relative.startswith("/") or "\\" in relative or member.is_symlink()
            or not member.is_file() or member.resolve() != member
            or hashlib.sha256(member.read_bytes()).hexdigest() != expected_sha):
        raise ValueError("runtime manifest member mismatch")
    runtime_records.append(relative)
if tuple(runtime_records) != runtime_members:
    raise ValueError("runtime manifest member set/order mismatch")
if validator_path.is_symlink() or finalizer_path.is_symlink():
    raise ValueError("runtime validator/finalizer is a symlink")
if hashlib.sha256(validator_path.read_bytes()).hexdigest() != value.get("validator_sha256", hashlib.sha256(validator_path.read_bytes()).hexdigest()):
    raise ValueError("validator identity mismatch")
if hashlib.sha256(finalizer_path.read_bytes()).hexdigest() != value.get("finalizer_sha256"):
    raise ValueError("finalizer identity mismatch")
if not emergency and value.get("evidence_files_read_only") is not True:
    raise ValueError("normal terminal marker lacks freeze declaration")
if emergency:
    if (value.get("evidence_freeze_schema_version") != "WSL2_OUTPUT_EVIDENCE_FREEZE_V1"
            or value.get("evidence_files_read_only") is not True
            or value.get("evidence_directories_read_only") is not True):
        raise ValueError("emergency freeze declaration mismatch")
elif (value.get("evidence_freeze_schema_version") != "WSL2_OUTPUT_EVIDENCE_FREEZE_V1"
        or value.get("evidence_directories_read_only_except_terminal_parents") is not True
        or value.get("terminal_publication_parents_mutable") is not True):
    raise ValueError("normal freeze declaration mismatch")

try:
    manifest_text = manifest_bytes.decode("utf-8")
except UnicodeError as exc:
    raise ValueError("manifest is not UTF-8") from exc
if not manifest_text or not manifest_text.endswith("\n") or "\r" in manifest_text or "\0" in manifest_text:
    raise ValueError("manifest framing is invalid")
entries = {}
ordered = []
line_pattern = re.compile(r"([0-9a-f]{64})  \./([^\n\r\0]+)")
for line in manifest_text.splitlines():
    match = line_pattern.fullmatch(line)
    if match is None:
        raise ValueError("manifest line is invalid")
    digest_value, relative = match.groups()
    pure = PurePosixPath(relative)
    if (
        relative.startswith("/")
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != relative
    ):
        raise ValueError("manifest path is non-canonical")
    if relative in entries:
        raise ValueError("duplicate manifest path")
    entries[relative] = digest_value
    ordered.append(relative)
if ordered != sorted(ordered, key=lambda item: item.encode("utf-8")):
    raise ValueError("manifest is not canonically ordered")
expected = {
    relative: digest_value
    for relative, digest_value in observed.items()
    if relative not in {manifest_relative, marker_relative}
}
if entries != expected:
    raise ValueError("manifest is not an exact digest map of the output payload")
entry_count = value.get(manifest_count_key)
if type(entry_count) is not int or entry_count != len(entries):
    raise ValueError("terminal manifest entry count mismatch")
for relative, fingerprint in output_identities.items():
    mode = fingerprint[2]
    if stat.S_ISREG(mode) and mode & 0o222:
        raise ValueError(f"terminal evidence file is writable: {relative}")
    if stat.S_ISDIR(mode):
        publication_exception = not emergency and relative == "operator_logs"
        if not publication_exception and mode & 0o222:
            raise ValueError(f"terminal evidence directory is writable: {relative}")
if emergency and root_identity[2] & 0o222:
    raise ValueError("emergency attempt root is writable")
formal = contract.get("stage_class") == "FORMAL"
if value.get("formal_g1") is not formal:
    raise ValueError("terminal formal_g1 mismatch")
if formal:
    if (value.get("formal_g1_receipt_sha256") != value["environment_receipt_sha256"]
            or value.get("environment_manifest_sha256") != contract.get("environment_provenance_manifest_sha256")):
        raise ValueError("formal terminal provenance mismatch")
elif value.get("formal_g1_receipt_sha256") is not None or (
        not emergency and value.get("environment_manifest_sha256") is not None):
    raise ValueError("engineering terminal carries formal provenance")
if not emergency and marker.name.endswith("SUCCESS.json"):
    validation_relative = "operator_logs/cell_contract.json"
    resolved_relative = "operator_logs/resolved_config_SHA256SUMS"
    validation_sha = observed.get(validation_relative)
    if (validation_sha is None or value.get("validation_sha256") != validation_sha
            or value.get("cell_contract_sha256") != validation_sha
            or value.get("resolved_config_manifest_sha256") != observed.get(resolved_relative)):
        raise ValueError("successful terminal validation/resolved-config binding mismatch")

contract_fd = open_canonical_file(contract_path)
try:
    final_contract_sha = digest_fd(contract_fd)
    final_contract_identity = identity(os.fstat(contract_fd))
finally:
    os.close(contract_fd)
final_contract_now = os.stat(contract_path, follow_symlinks=False)
if (
    final_contract_identity != contract_identity
    or final_contract_identity != identity(final_contract_now)
    or final_contract_sha != actual_contract_sha
):
    raise ValueError("cell execution contract changed before terminal decision")
terminal_digests = close_output_snapshot(
    root_fd,
    output_identities,
    {manifest_relative, marker_relative},
)
if terminal_digests.get(manifest_relative) != observed[manifest_relative]:
    raise ValueError("output manifest changed before terminal decision")
if terminal_digests.get(marker_relative) != observed[marker_relative]:
    raise ValueError("terminal marker changed before terminal decision")
root_final = os.stat(root, follow_symlinks=False)
if root_identity != identity(root_final) or root_identity != identity(os.fstat(root_fd)):
    raise ValueError("output root changed before terminal decision")
os.close(root_fd)
if emergency:
    emergency_failure_classes = {"FINALIZER_FAILURE", "ANCESTOR_IDENTITY_DRIFT"}
    if (value.get("schema_version") != "WSL2_BOLTZGEN_LOCAL_EMERGENCY_FAILURE_V1"
            or value.get("status") != "EMERGENCY_FAILURE"
            or value.get("failure_class") not in emergency_failure_classes
            or type(value.get("pipeline_exit_code")) is not int
            or not 1 <= value["pipeline_exit_code"] <= 255
            or type(value.get("finalizer_exit_code")) is not int
            or not 1 <= value["finalizer_exit_code"] <= 255
            or value.get("terminal_status") != "LOCAL_CELL_FAILED"
            or observed.get("operator_logs/finalizer.log.txt") != value.get("finalizer_log_sha256")):
        raise ValueError("invalid emergency failure marker")
    print("EMERGENCY_FAILURE")
elif marker.name.endswith("SUCCESS.json"):
    if (value.get("schema_version") != "WSL2_BOLTZGEN_LOCAL_SUCCESS_V1"
            or value.get("status") != "SUCCESS"
            or type(value.get("pipeline_exit_code")) is not int
            or value.get("pipeline_exit_code") != 0
            or value.get("terminal_status") != contract.get("success_status")):
        raise ValueError("invalid success marker")
    print("SUCCESS")
else:
    if (value.get("schema_version") != "WSL2_BOLTZGEN_LOCAL_FAILURE_V1"
            or value.get("status") != "FAILURE"
            or type(value.get("pipeline_exit_code")) is not int
            or not 1 <= value["pipeline_exit_code"] <= 255):
        raise ValueError("invalid failure marker")
    failure_pair = (value.get("terminal_status"), value.get("failure_class"))
    expected_pair = ("LOCAL_CELL_FAILED", "PIPELINE_EXIT_NONZERO")
    if engineering_probe and recompute_engineering_gpu_oom(captures):
        expected_pair = (BLOCKED_GPU_MEMORY_STATUS, BLOCKED_GPU_MEMORY_STATUS)
    if failure_pair != expected_pair:
        raise ValueError("failure marker status/classification mismatch")
    print("FAILURE")
PY
)
  TERMINAL_RC=$?
  set -e
  if [ "$TERMINAL_RC" -ne 0 ]; then
    emit_state BLOCKED_TERMINAL_MARKER_INVALID "$UNIT" "terminal marker or output manifest cannot be trusted"
    exit 3
  fi
  if [ "$TERMINAL_KIND" = SUCCESS ]; then
    emit_state SUCCEEDED "$UNIT" "canonical terminal success marker is sealed"
    exit 0
  fi
  emit_state FAILED "$UNIT" "canonical terminal failure marker is sealed"
  exit 1
fi

set +e
query_exact_unit
QUERY_RC=$?
set -e
if [ "$QUERY_RC" -eq 10 ]; then
  emit_state BLOCKED_UNIT_DISAPPEARED "$UNIT" "unit disappeared before a terminal marker was published"
  exit 3
fi
if [ "$QUERY_RC" -ne 0 ]; then
  emit_state BLOCKED_UNIT_AMBIGUOUS "$UNIT" "unit identity is ambiguous"
  exit 3
fi
if [ "$QUERY_INVOCATION_ID" != "$RECEIPT_INVOCATION" ]; then
  emit_state BLOCKED_UNIT_AMBIGUOUS "$UNIT" "live unit InvocationID differs from its receipt"
  exit 3
fi
case "$QUERY_ACTIVE_STATE" in
  active|activating|reloading)
    emit_state RUNNING "$UNIT" "$QUERY_ACTIVE_STATE/$QUERY_SUB_STATE"
    exit 0
    ;;
  failed)
    emit_state FAILED_UNIT "$UNIT" "$QUERY_ACTIVE_STATE/$QUERY_SUB_STATE result=$QUERY_RESULT"
    exit 1
    ;;
  *)
    emit_state BLOCKED_TERMINAL_MARKER_MISSING "$UNIT" "$QUERY_ACTIVE_STATE/$QUERY_SUB_STATE result=$QUERY_RESULT"
    exit 3
    ;;
esac
