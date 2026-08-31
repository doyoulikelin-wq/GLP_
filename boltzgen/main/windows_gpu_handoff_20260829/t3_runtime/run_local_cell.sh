#!/usr/bin/env bash
# Execute one receipt-bound BoltzGen cell under its contract-limited executor UID lock.

set -euo pipefail
umask 077

die() {
  printf 'run_local_cell: %s\n' "${1:-local cell failed}" >&2
  exit "${2:-70}"
}

[ "$#" -eq 2 ] || die "usage: run_local_cell.sh BG_WORK CELL_CONTRACT" 64
BG_WORK_INPUT=$1
CONTRACT_INPUT=$2
[ -d "$BG_WORK_INPUT" ] && [ ! -L "$BG_WORK_INPUT" ] || die "BG_WORK must be a non-symlink directory" 66
BG_WORK=$(readlink -f -- "$BG_WORK_INPUT")
python3 -I -S - "$BG_WORK" <<'PY' || die "BG_WORK ownership/mode is unsafe" 66
import os
import stat
import sys
info = os.stat(sys.argv[1], follow_symlinks=False)
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022):
    raise SystemExit(1)
PY
[ -f "$CONTRACT_INPUT" ] && [ ! -L "$CONTRACT_INPUT" ] || die "cell contract must be a regular non-symlink file" 66
CONTRACT=$(readlink -f -- "$CONTRACT_INPUT")
SCRIPT_PATH=$(readlink -f -- "$0")

for contaminated in PYTHONPATH PYTHONHOME PYTHONOPTIMIZE CUDA_VISIBLE_DEVICES; do
  if [ "${!contaminated+x}" = x ]; then
    die "refusing inherited $contaminated" 65
  fi
done
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 LC_ALL=C

read_contract() {
  python3 -I -S - "$CONTRACT" "$BG_WORK" "$SCRIPT_PATH" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ENGINEERING_MEMORY_PROBE_ID = re.compile(
    r"6xym_(diverse|adherence)_batch1_engineering"
)
ENGINEERING_MEMORY_PROBE_RUN_KIND = "ENGINEERING_MEMORY_PROBE"
ENGINEERING_MEMORY_PROBE_STATUS = "ENGINEERING_MEMORY_PROBE_ONLY"
ENGINEERING_6XYM_SPEC_SUFFIX = (
    "project_input", "specs", "08_pdb_00006xym-A", "design.yaml"
)
ENGINEERING_PROBE_FIELDS = ("probe_id", "checkpoint_name", "checkpoint_sha256")
FORMAL_REVISION = re.compile(
    r"WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V[1-9][0-9]*"
)
MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  \./([^\n\r\0]+)")
RUNTIME_MEMBERS = tuple(sorted((
    "run_local_cell.sh",
    "software/finalize_local_attempt.py",
    "software/validate_cell_output.py",
    "status_local_cell.sh",
    "submit_local_once.sh",
    "verify_gpu_env_stage.sh",
), key=lambda value: value.encode("utf-8")))

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

def text(value, label):
    if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{label} must be a non-empty control-free string")
    return value

def integer(value, label, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value

def bound_file(data, path_key, sha_key):
    raw = text(data.get(path_key), path_key)
    expected = data.get(sha_key)
    if not isinstance(expected, str) or SHA.fullmatch(expected) is None:
        raise ValueError(f"{sha_key} must be a lowercase SHA-256")
    supplied = Path(raw)
    if not supplied.is_absolute() or supplied.is_symlink():
        raise ValueError(f"{path_key} must be an absolute non-symlink path")
    resolved = supplied.resolve(strict=True)
    if str(resolved) != str(supplied):
        raise ValueError(f"{path_key} must already be canonical")
    if not stat.S_ISREG(resolved.stat().st_mode) or resolved.is_symlink():
        raise ValueError(f"{path_key} must name a regular non-symlink file")
    if digest(resolved) != expected:
        raise ValueError(f"{path_key}/{sha_key} mismatch")
    return resolved

def runtime_manifest(data, bg_work):
    manifest = bound_file(data, "runtime_scripts_manifest_path", "runtime_scripts_manifest_sha256")
    if manifest != bg_work / "gpu_runtime_scripts_SHA256SUMS":
        raise ValueError("runtime scripts manifest must be at the BG_WORK root")
    raw = manifest.read_text(encoding="utf-8")
    if not raw or not raw.endswith("\n") or "\r" in raw or "\0" in raw:
        raise ValueError("runtime scripts manifest framing is invalid")
    records = []
    for line in raw.splitlines():
        match = MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise ValueError("runtime scripts manifest line is invalid")
        expected, relative = match.groups()
        pure = Path(relative)
        if (relative.startswith("/") or "\\" in relative or pure.as_posix() != relative
                or any(part in {"", ".", ".."} for part in pure.parts)):
            raise ValueError("unsafe runtime manifest path")
        member = bg_work / pure
        if member.is_symlink() or not member.is_file() or member.resolve() != member:
            raise ValueError(f"runtime script is missing or unsafe: {relative}")
        if digest(member) != expected:
            raise ValueError(f"runtime script SHA-256 mismatch: {relative}")
        records.append(relative)
    if tuple(records) != RUNTIME_MEMBERS:
        raise ValueError("runtime scripts manifest member set/order mismatch")
    return manifest

path = Path(sys.argv[1])
bg_work = Path(sys.argv[2])
script_path = Path(sys.argv[3])
if path.is_symlink() or not path.is_file():
    raise ValueError("unsafe cell contract")
with path.open(encoding="utf-8") as handle:
    data = json.load(handle, object_pairs_hook=no_duplicates)
if not isinstance(data, dict) or data.get("schema_version") != "WSL2_BOLTZGEN_LOCAL_CELL_V1":
    raise ValueError("unsupported cell-contract schema")
cell = text(data.get("cell_id"), "cell_id")
attempt = text(data.get("attempt_id"), "attempt_id")
if SAFE_ID.fullmatch(cell) is None or SAFE_ID.fullmatch(attempt) is None:
    raise ValueError("unsafe cell or attempt identifier")
run_kind = text(data.get("run_kind"), "run_kind")
success_status = text(data.get("success_status"), "success_status")
stage_class = text(data.get("stage_class"), "stage_class")
if stage_class not in {"ENGINEERING", "FORMAL"}:
    raise ValueError("invalid stage_class")

paths = {}
for path_key, sha_key in (
    ("spec_path", "spec_sha256"),
    ("design_checkpoint", "design_checkpoint_sha256"),
    ("inverse_fold_checkpoint", "inverse_fold_checkpoint_sha256"),
    ("folding_checkpoint", "folding_checkpoint_sha256"),
    ("mols_path", "mols_sha256"),
    ("model_inputs_manifest_path", "model_inputs_manifest_sha256"),
    ("runtime_scripts_manifest_path", "runtime_scripts_manifest_sha256"),
    ("spec_gate_bundle_path", "spec_gate_bundle_sha256"),
    ("environment_receipt", "environment_receipt_sha256"),
):
    paths[path_key] = bound_file(data, path_key, sha_key)
manifest = runtime_manifest(data, bg_work)
if script_path != bg_work / "run_local_cell.sh" or script_path.is_symlink() or digest(script_path) != dict(
    (line.split("  ./", 1)[1], line.split("  ./", 1)[0])
    for line in manifest.read_text(encoding="utf-8").splitlines()
)["run_local_cell.sh"]:
    raise ValueError("running runner is not the manifest-bound runner")
optional = ("environment_provenance_manifest_path", "environment_provenance_manifest_sha256")
environment_provenance = None
if any(key in data for key in optional):
    if not all(key in data for key in optional):
        raise ValueError("environment provenance path and SHA must be supplied together")
    environment_provenance = bound_file(data, *optional)
if stage_class == "FORMAL" and environment_provenance is None:
    raise ValueError("FORMAL cell contract requires an environment provenance manifest")

numbers = {}
for key in ("expected_designs", "expected_fold_samples", "budget", "diffusion_batch_size", "inverse_fold_num_sequences", "devices", "num_workers"):
    numbers[key] = integer(data.get(key), key)
if numbers["expected_fold_samples"] != 5 or numbers["devices"] != 1:
    raise ValueError("local execution requires five fold samples and one device")
if data.get("use_kernels") != "auto" or data.get("protocol") != "nanobody-anything":
    raise ValueError("invalid kernels/protocol contract")
if data.get("analysis_modality") != "antibody" or data.get("filtering_modality") != "antibody":
    raise ValueError("invalid analysis/filtering modality")
if data.get("filter_bindingsite") is not True:
    raise ValueError("filter_bindingsite must be true")

probe_id_value = data.get("probe_id")
is_engineering_6xym_spec = (
    tuple(paths["spec_path"].parts[-4:]) == ENGINEERING_6XYM_SPEC_SUFFIX
)
engineering_probe_claim = (
    run_kind.upper().startswith(ENGINEERING_MEMORY_PROBE_RUN_KIND)
    or success_status.upper().startswith(ENGINEERING_MEMORY_PROBE_STATUS)
    or ENGINEERING_MEMORY_PROBE_ID.fullmatch(cell) is not None
    or (
        isinstance(probe_id_value, str)
        and ENGINEERING_MEMORY_PROBE_ID.fullmatch(probe_id_value) is not None
    )
    or (
        stage_class == "ENGINEERING"
        and (
            "PROBE" in run_kind.upper()
            or "PROBE" in success_status.upper()
            or cell.startswith("6xym_")
            or is_engineering_6xym_spec
            or any(field in data for field in ENGINEERING_PROBE_FIELDS)
        )
    )
)
if run_kind == ENGINEERING_MEMORY_PROBE_RUN_KIND:
    if stage_class != "ENGINEERING":
        raise ValueError("engineering memory probe requires stage_class=ENGINEERING")
    if success_status != ENGINEERING_MEMORY_PROBE_STATUS:
        raise ValueError("engineering memory probe has a non-canonical success_status")
    match = ENGINEERING_MEMORY_PROBE_ID.fullmatch(cell)
    probe_id = text(probe_id_value, "probe_id")
    if match is None or probe_id != cell:
        raise ValueError("engineering memory probe has a non-canonical probe_id")
    checkpoint_name = text(data.get("checkpoint_name"), "checkpoint_name")
    if checkpoint_name != match.group(1):
        raise ValueError("engineering probe_id/checkpoint_name mismatch")
    checkpoint_sha = data.get("checkpoint_sha256")
    if (
        not isinstance(checkpoint_sha, str)
        or SHA.fullmatch(checkpoint_sha) is None
        or checkpoint_sha != data["design_checkpoint_sha256"]
    ):
        raise ValueError("engineering probe checkpoint SHA is not the design checkpoint")
    if paths["design_checkpoint"].name != f"boltzgen1_{checkpoint_name}.ckpt":
        raise ValueError("engineering probe checkpoint path/name mismatch")
    if not is_engineering_6xym_spec:
        raise ValueError("engineering memory probe must use the frozen 6XYM spec")
    for field, expected in {
        "expected_designs": 1,
        "budget": 1,
        "diffusion_batch_size": 1,
        "inverse_fold_num_sequences": 1,
        "expected_fold_samples": 5,
    }.items():
        if numbers[field] != expected:
            raise ValueError(f"engineering memory probe requires {field}={expected}")
elif engineering_probe_claim:
    raise ValueError("approximate engineering memory-probe contracts are forbidden")

environment_contract_path = bg_work / "contract" / "environment_contract.json"
if ((bg_work / "contract").is_symlink() or not (bg_work / "contract").is_dir()
        or environment_contract_path.is_symlink() or not environment_contract_path.is_file()):
    raise ValueError("environment contract is missing or unsafe")
with environment_contract_path.open(encoding="utf-8") as handle:
    environment_contract = json.load(handle, object_pairs_hook=no_duplicates)
if (not isinstance(environment_contract, dict)
        or environment_contract.get("schema_version") != "WSL2_GPU_STAGE_ENVIRONMENT_CONTRACT_V1"
        or environment_contract.get("stage_class") != stage_class):
    raise ValueError("cell/environment contract mismatch")
executor_uid = environment_contract.get("executor_uid")
if type(executor_uid) is not int or executor_uid != os.getuid():
    raise ValueError("environment contract executor_uid mismatch")
attempt_root_raw = environment_contract.get("environment_attempt_root")
if not isinstance(attempt_root_raw, str) or not attempt_root_raw:
    raise ValueError("environment attempt root is missing")
environment_attempt_root = Path(attempt_root_raw)
if not environment_attempt_root.is_absolute() or environment_attempt_root.is_symlink():
    raise ValueError("environment attempt root is unsafe")
environment_attempt_root = environment_attempt_root.resolve(strict=True)
if not environment_attempt_root.is_dir():
    raise ValueError("environment attempt root is not a directory")
environment_path = paths["environment_receipt"]
if (environment_contract.get("environment_receipt_path") != str(environment_path)
        or environment_contract.get("environment_receipt_sha256") != data["environment_receipt_sha256"]
        or environment_path != environment_attempt_root / "receipt.json"):
    raise ValueError("cell/environment receipt binding mismatch")
environment_root = environment_attempt_root / "env"
environment_python = environment_root / "bin" / "python"
environment_launcher = environment_root / "bin" / "boltzgen-wsl-sm120"
if environment_root.is_symlink() or not environment_root.is_dir():
    raise ValueError("environment root is unsafe")
try:
    resolved_environment_python = environment_python.resolve(strict=True)
except OSError as exc:
    raise ValueError(f"environment Python cannot be resolved: {exc}")
if not resolved_environment_python.is_file() or not os.access(environment_python, os.X_OK):
    raise ValueError("environment Python is missing or unsafe")
if (environment_launcher.is_symlink() or not environment_launcher.is_file()
        or not os.access(environment_launcher, os.X_OK)):
    raise ValueError("compatibility launcher is missing or unsafe")
with environment_path.open(encoding="utf-8") as handle:
    environment = json.load(handle, object_pairs_hook=no_duplicates)
if not isinstance(environment, dict):
    raise ValueError("environment receipt must be an object")
if isinstance(environment.get("exit_code"), bool) or environment.get("exit_code") != 0:
    raise ValueError("environment receipt is not successful")
formal_g1 = environment.get("formal_g1")
if type(formal_g1) is not bool:
    raise ValueError("environment formal_g1 must be a JSON boolean")
formal_claim = (
    stage_class == "FORMAL"
    or run_kind.upper() == "G2"
    or run_kind.upper().startswith("G2_")
    or re.search(r"(?:^|_)G[12](?:_[A-Z0-9]+)*_PASS(?:_|$)", success_status.upper()) is not None
)
if formal_claim and not formal_g1:
    raise ValueError("formal/G2 work requires a formal G1 receipt")
if formal_g1:
    if (environment.get("schema_version") != "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1"
            or environment.get("status") != "G1_PASS" or stage_class != "FORMAL"):
        raise ValueError("formal_g1=true requires G1_PASS")
    revision = environment.get("environment_contract_revision")
    if not isinstance(revision, str) or FORMAL_REVISION.fullmatch(revision) is None:
        raise ValueError("formal G1 receipt has an invalid environment contract revision")
    if environment.get("environment_contract_revision_required") is not False:
        raise ValueError("formal G1 receipt still requires an environment contract revision")
    expected_provenance = environment_attempt_root / "recursive_payload.SHA256SUMS"
    recursive = environment_contract.get("artifact_bindings", {}).get(
        "recursive_payload_manifest"
    )
    if not isinstance(recursive, dict) or set(recursive) != {"path", "sha256"}:
        raise ValueError("formal environment contract lacks the recursive manifest binding")
    recursive_path = Path(recursive.get("path", ""))
    recursive_sha = recursive.get("sha256")
    if (
        environment_provenance != expected_provenance
        or recursive_path != expected_provenance
        or recursive_path.is_symlink()
        or not recursive_path.is_file()
        or SHA.fullmatch(str(recursive_sha)) is None
        or digest(recursive_path) != recursive_sha
        or data["environment_provenance_manifest_sha256"] != recursive_sha
        or environment.get("environment_manifest_sha256") != recursive_sha
        or environment.get("recursive_payload_manifest_sha256") != recursive_sha
    ):
        raise ValueError("formal environment provenance bindings do not agree")
elif (
    environment.get("schema_version") != "WSL2_CU128_BLACKWELL_ENGINEERING_ENV_RECEIPT_V4"
    or stage_class != "ENGINEERING"
    or environment.get("status") != "ENGINEERING_COMPATIBILITY_ONLY"
    or environment.get("environment_contract_revision_required") is not True
):
    raise ValueError("engineering environment receipt has invalid status/revision semantics")
if environment.get("compatibility_activation") != "EXPLICIT_PROCESS_LOCAL_ONLY":
    raise ValueError("environment receipt lacks explicit compatibility activation")

values = (
    cell, attempt, run_kind, success_status, stage_class,
    paths["spec_path"], paths["design_checkpoint"], paths["inverse_fold_checkpoint"],
    paths["folding_checkpoint"], paths["mols_path"], environment_path,
    str(numbers["expected_designs"]), str(numbers["expected_fold_samples"]),
    str(numbers["budget"]), str(numbers["diffusion_batch_size"]),
    str(numbers["inverse_fold_num_sequences"]), str(numbers["devices"]),
    str(numbers["num_workers"]), data["use_kernels"], data["protocol"],
    data["analysis_modality"], data["filtering_modality"], "true", digest(path),
    manifest, data["runtime_scripts_manifest_sha256"], environment_root,
    environment_python, environment_launcher, digest(environment_launcher),
    data["environment_receipt_sha256"],
    str(executor_uid),
)
for value in values:
    print(value)
PY
}

load_contract() {
  local output
  if ! output=$(read_contract); then
    return 65
  fi
  mapfile -t CELL_FIELDS <<< "$output"
  [ "${#CELL_FIELDS[@]}" -eq 32 ] || return 65
  CELL_ID=${CELL_FIELDS[0]}
  ATTEMPT_ID=${CELL_FIELDS[1]}
  RUN_KIND=${CELL_FIELDS[2]}
  SUCCESS_STATUS=${CELL_FIELDS[3]}
  STAGE_CLASS=${CELL_FIELDS[4]}
  SPEC_PATH=${CELL_FIELDS[5]}
  DESIGN_CHECKPOINT=${CELL_FIELDS[6]}
  INVERSE_CHECKPOINT=${CELL_FIELDS[7]}
  FOLDING_CHECKPOINT=${CELL_FIELDS[8]}
  MOLS_PATH=${CELL_FIELDS[9]}
  ENVIRONMENT_RECEIPT=${CELL_FIELDS[10]}
  EXPECTED_DESIGNS=${CELL_FIELDS[11]}
  EXPECTED_FOLD_SAMPLES=${CELL_FIELDS[12]}
  BUDGET=${CELL_FIELDS[13]}
  DIFFUSION_BATCH_SIZE=${CELL_FIELDS[14]}
  INVERSE_FOLD_NUM_SEQUENCES=${CELL_FIELDS[15]}
  DEVICES=${CELL_FIELDS[16]}
  NUM_WORKERS=${CELL_FIELDS[17]}
  USE_KERNELS=${CELL_FIELDS[18]}
  PROTOCOL=${CELL_FIELDS[19]}
  ANALYSIS_MODALITY=${CELL_FIELDS[20]}
  FILTERING_MODALITY=${CELL_FIELDS[21]}
  FILTER_BINDINGSITE=${CELL_FIELDS[22]}
  CURRENT_CONTRACT_SHA=${CELL_FIELDS[23]}
  RUNTIME_MANIFEST=${CELL_FIELDS[24]}
  RUNTIME_MANIFEST_SHA=${CELL_FIELDS[25]}
  ENVIRONMENT_ROOT=${CELL_FIELDS[26]}
  ENV_PYTHON=${CELL_FIELDS[27]}
  BOLTZGEN=${CELL_FIELDS[28]}
  LAUNCHER_SHA=${CELL_FIELDS[29]}
  ENVIRONMENT_RECEIPT_SHA=${CELL_FIELDS[30]}
  EXECUTOR_UID=${CELL_FIELDS[31]}
}

load_contract || die "immutable cell-contract validation failed" 65
INITIAL_CONTRACT_SHA=$CURRENT_CONTRACT_SHA
INITIAL_CELL_ID=$CELL_ID
INITIAL_ATTEMPT_ID=$ATTEMPT_ID
ENGINEERING_MEMORY_PROBE=0
MEMORY_PROBE=0
if [ "$RUN_KIND" = "ENGINEERING_MEMORY_PROBE" ]; then
  ENGINEERING_MEMORY_PROBE=1
  MEMORY_PROBE=1
elif [ "$STAGE_CLASS" = "FORMAL" ] && [[ "${RUN_KIND^^}" == *PROBE* ]]; then
  # Preserve the frozen formal-probe classification used by the finalizer.
  MEMORY_PROBE=1
fi
revalidate_contract() {
  load_contract || return 65
  [ "$CURRENT_CONTRACT_SHA" = "$INITIAL_CONTRACT_SHA" ] || return 65
  [ "$CELL_ID" = "$INITIAL_CELL_ID" ] && [ "$ATTEMPT_ID" = "$INITIAL_ATTEMPT_ID" ] || return 65
}

SUBMISSION_BASE="$BG_WORK/local_submissions/$CELL_ID.$ATTEMPT_ID"
INTENT="$SUBMISSION_BASE.intent.json"
RECEIPT="$SUBMISSION_BASE.receipt.json"
[ -f "$INTENT" ] && [ ! -L "$INTENT" ] || die "missing or unsafe submission intent" 75

# systemd may start this process before the submitter has queried the unit and
# atomically published its receipt. Wait only for that one expected receipt.
for _ in $(seq 1 300); do
  if [ -f "$RECEIPT" ] && [ ! -L "$RECEIPT" ]; then
    break
  fi
  [ ! -L "$RECEIPT" ] || die "submission receipt is a symlink" 75
  sleep 0.1
done
[ -f "$RECEIPT" ] && [ ! -L "$RECEIPT" ] || die "submission receipt was not published" 75

if ! SUBMISSION_VALUES=$(python3 -I -S - "$INTENT" "$RECEIPT" "$CELL_ID" "$ATTEMPT_ID" "$CONTRACT" "$INITIAL_CONTRACT_SHA" "$SCRIPT_PATH" "${BG_SUBMISSION_TOKEN:-}" "${INVOCATION_ID:-}" "$EXECUTOR_UID" <<'PY'
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

def load(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("unsafe submission artifact")
    with target.open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=no_duplicates)

intent = load(sys.argv[1])
receipt = load(sys.argv[2])
common = {
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": sys.argv[3],
    "attempt_id": sys.argv[4],
    "cell_contract_path": sys.argv[5],
    "cell_contract_sha256": sys.argv[6],
    "executor_uid": int(sys.argv[10]),
}
if intent.get("schema_version") != "WSL2_LOCAL_SUBMISSION_INTENT_V1" or intent.get("status") != "SUBMISSION_INTENT":
    raise ValueError("invalid intent schema/status")
if receipt.get("schema_version") != "WSL2_LOCAL_SUBMISSION_RECEIPT_V1" or receipt.get("status") != "SUBMITTED":
    raise ValueError("invalid receipt schema/status")
for key, value in common.items():
    if intent.get(key) != value or receipt.get(key) != value:
        raise ValueError(f"submission binding mismatch: {key}")
unit = intent.get("unit")
if not isinstance(unit, str) or re.fullmatch(r"boltzgen-local-[0-9a-f]{64}\.service", unit) is None:
    raise ValueError("invalid unit")
if receipt.get("unit") != unit:
    raise ValueError("intent/receipt unit mismatch")
token = intent.get("submission_token")
if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
    raise ValueError("invalid submission token")
if (receipt.get("submission_token") != token or token != sys.argv[8]
        or intent.get("runner_path") != sys.argv[7] or receipt.get("runner_path") != sys.argv[7]):
    raise ValueError("submission token/runner binding mismatch")
invocation = receipt.get("invocation_id")
if not isinstance(invocation, str) or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
    raise ValueError("invalid unit InvocationID")
if invocation != sys.argv[9]:
    raise ValueError("runner INVOCATION_ID differs from submission receipt")
exec_start_sha = intent.get("exec_start_sha256")
if (not isinstance(exec_start_sha, str) or re.fullmatch(r"[0-9a-f]{64}", exec_start_sha) is None
        or receipt.get("exec_start_sha256") != exec_start_sha):
    raise ValueError("intent/receipt ExecStart binding mismatch")
print(unit)
print(token)
print(invocation)
print(exec_start_sha)
print(hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest())
PY
); then
  die "submission intent/receipt binding validation failed" 75
fi
mapfile -t SUBMISSION_FIELDS <<< "$SUBMISSION_VALUES"
[ "${#SUBMISSION_FIELDS[@]}" -eq 5 ] || die "submission binding fields are incomplete" 75
UNIT=${SUBMISSION_FIELDS[0]}
SUBMISSION_TOKEN=${SUBMISSION_FIELDS[1]}
SUBMISSION_INVOCATION_ID=${SUBMISSION_FIELDS[2]}
EXEC_START_SHA=${SUBMISSION_FIELDS[3]}
SUBMISSION_RECEIPT_SHA=${SUBMISSION_FIELDS[4]}

# The environment contract fixes one executor UID. Its systemd user-runtime
# directory is the canonical, non-replaceable lock inode for that executor.
# The parent is root-owned and non-writable, so the contract executor cannot
# unlink/rename the locked directory and create a second lock domain.
EXECUTOR_RUNTIME_PARENT=/run/user
EXECUTOR_RUNTIME_ROOT="$EXECUTOR_RUNTIME_PARENT/$EXECUTOR_UID"

verify_executor_gpu_lock() {
  python3 -I -S - "$EXECUTOR_RUNTIME_PARENT" "$EXECUTOR_RUNTIME_ROOT" \
    "${BG_EXECUTOR_LOCK_PARENT_FD:-}" "${BG_EXECUTOR_LOCK_FD:-}" <<'PY'
import fcntl
import os
import signal
import stat
import sys

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

parent_path, runtime_path, parent_text, lock_text = sys.argv[1:]
if not parent_text.isdecimal() or not lock_text.isdecimal():
    raise SystemExit(1)
parent_fd = int(parent_text)
lock_fd = int(lock_text)
parent = os.fstat(parent_fd)
runtime = os.fstat(lock_fd)
if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
    raise SystemExit(1)
if not stat.S_ISDIR(runtime.st_mode) or runtime.st_uid != os.getuid():
    raise SystemExit(1)
if stat.S_IMODE(runtime.st_mode) != 0o700 or runtime.st_nlink < 2:
    raise SystemExit(1)
parent_now = os.stat(parent_path, follow_symlinks=False)
runtime_now = os.stat(str(os.getuid()), dir_fd=parent_fd, follow_symlinks=False)
if (parent.st_dev, parent.st_ino) != (parent_now.st_dev, parent_now.st_ino):
    raise SystemExit(1)
if (runtime.st_dev, runtime.st_ino) != (runtime_now.st_dev, runtime_now.st_ino):
    raise SystemExit(1)
if os.path.realpath(runtime_path) != runtime_path:
    raise SystemExit(1)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(75)
PY
}

if [ -z "${BG_EXECUTOR_LOCK_FD:-}" ] || [ -z "${BG_EXECUTOR_LOCK_PARENT_FD:-}" ]; then
  exec python3 -I -S - "$0" "$BG_WORK_INPUT" "$CONTRACT_INPUT" \
    "$EXECUTOR_RUNTIME_PARENT" "$EXECUTOR_RUNTIME_ROOT" <<'PY'
import fcntl
import os
import stat
import sys

script, bg_work, contract, parent_path, runtime_path = sys.argv[1:]
uid = os.getuid()
if runtime_path != f"/run/user/{uid}" or os.path.realpath(runtime_path) != runtime_path:
    raise SystemExit(66)
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
try:
    parent_fd = os.open(parent_path, flags)
    runtime_fd = os.open(str(uid), flags, dir_fd=parent_fd)
except OSError:
    raise SystemExit(66)
parent = os.fstat(parent_fd)
runtime = os.fstat(runtime_fd)
if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
    raise SystemExit(66)
if not stat.S_ISDIR(runtime.st_mode) or runtime.st_uid != uid:
    raise SystemExit(66)
if stat.S_IMODE(runtime.st_mode) != 0o700 or runtime.st_nlink < 2:
    raise SystemExit(66)
runtime_now = os.stat(str(uid), dir_fd=parent_fd, follow_symlinks=False)
if (runtime.st_dev, runtime.st_ino) != (runtime_now.st_dev, runtime_now.st_ino):
    raise SystemExit(66)
try:
    fcntl.flock(runtime_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(75)
os.set_inheritable(parent_fd, True)
os.set_inheritable(runtime_fd, True)
environment = os.environ.copy()
environment["BG_EXECUTOR_LOCK_PARENT_FD"] = str(parent_fd)
environment["BG_EXECUTOR_LOCK_FD"] = str(runtime_fd)
os.execve("/bin/bash", ["bash", script, bg_work, contract], environment)
PY
fi
verify_executor_gpu_lock || {
  rc=$?
  [ "$rc" -eq 75 ] && die "the contract executor GPU lock is already held" 75
  die "canonical executor GPU lock identity is unsafe" 66
}
revalidate_contract || die "cell contract changed before GPU lock acquisition" 65

# Every executable used after attempt creation is already fixed by the strict
# runtime manifest or the environment contract.  Fail before creating a run
# directory when any prerequisite is absent or unsafe.
VERIFY_STAGE="$BG_WORK/verify_gpu_env_stage.sh"
VALIDATOR="$BG_WORK/software/validate_cell_output.py"
FINALIZER="$BG_WORK/software/finalize_local_attempt.py"
for dependency in "$VERIFY_STAGE" "$VALIDATOR" "$FINALIZER"; do
  [ -f "$dependency" ] && [ ! -L "$dependency" ] || die "missing or unsafe runtime dependency: $dependency" 66
done
[ -x "$BOLTZGEN" ] && [ ! -L "$BOLTZGEN" ] || die "missing bound compatibility launcher" 66
[ -x "$ENV_PYTHON" ] || die "missing bound environment Python" 66
[ "$BOLTZGEN" = "$ENVIRONMENT_ROOT/bin/boltzgen-wsl-sm120" ] || die "unexpected compatibility launcher path" 65
[ "$ENV_PYTHON" = "$ENVIRONMENT_ROOT/bin/python" ] || die "unexpected environment Python path" 65

RUNS_LEXICAL="$BG_WORK/runs"
RUN_PARENT_LEXICAL="$RUNS_LEXICAL/$CELL_ID"
ATTEMPT_ROOT_LEXICAL="$RUN_PARENT_LEXICAL/$ATTEMPT_ID"
OPERATOR_LOGS_LEXICAL="$ATTEMPT_ROOT_LEXICAL/operator_logs"

# A child cannot return open descriptors to its parent shell.  Create the
# hierarchy with openat/O_NOFOLLOW, keep every directory descriptor inheritable,
# and exec the runner once more.  The second invocation therefore operates on
# the exact directory inodes it created, even if an ancestor name is replaced.
if [ -z "${BG_HIERARCHY_READY:-}" ]; then
  exec /usr/bin/python3 -I -S - "$SCRIPT_PATH" "$BG_WORK_INPUT" "$CONTRACT_INPUT" \
    "$BG_WORK" "$CELL_ID" "$ATTEMPT_ID" <<'PY'
import os
import stat
import sys

script, bg_work_input, contract_input, bg_work, cell_id, attempt_id = sys.argv[1:]
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)

def validate(directory, label):
    info = os.fstat(directory)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError(f"unsafe {label} ownership/mode")

bg = os.open(bg_work, flags)
try:
    bg_info = os.fstat(bg)
    if bg_info.st_uid != os.getuid() or stat.S_IMODE(bg_info.st_mode) & 0o022:
        raise ValueError("unsafe BG_WORK")
    submissions = os.open("local_submissions", flags, dir_fd=bg)
    validate(submissions, "local submissions")
    try:
        os.mkdir("runs", 0o700, dir_fd=bg)
        os.fsync(bg)
    except FileExistsError:
        pass
    runs = os.open("runs", flags, dir_fd=bg)
    validate(runs, "runs")
    try:
        os.mkdir(cell_id, 0o700, dir_fd=runs)
        os.fsync(runs)
    except FileExistsError:
        pass
    cell = os.open(cell_id, flags, dir_fd=runs)
    validate(cell, "cell run parent")
    os.mkdir(attempt_id, 0o700, dir_fd=cell)
    attempt = os.open(attempt_id, flags, dir_fd=cell)
    validate(attempt, "attempt root")
    os.mkdir("operator_logs", 0o700, dir_fd=attempt)
    logs = os.open("operator_logs", flags, dir_fd=attempt)
    validate(logs, "operator logs")
    os.fsync(logs)
    os.fsync(attempt)
    os.fsync(cell)
    for descriptor in (bg, submissions, runs, cell, attempt, logs):
        os.set_inheritable(descriptor, True)
    environment = os.environ.copy()
    environment.update({
        "BG_HIERARCHY_READY": "1",
        "BG_HIERARCHY_BG_FD": str(bg),
        "BG_HIERARCHY_SUBMISSIONS_FD": str(submissions),
        "BG_HIERARCHY_RUNS_FD": str(runs),
        "BG_HIERARCHY_CELL_FD": str(cell),
        "BG_HIERARCHY_ATTEMPT_FD": str(attempt),
        "BG_HIERARCHY_LOGS_FD": str(logs),
    })
    os.execve(
        "/bin/bash",
        ["bash", script, bg_work_input, contract_input],
        environment,
    )
except Exception:
    for descriptor in (
        locals().get("logs"),
        locals().get("attempt"),
        locals().get("cell"),
        locals().get("runs"),
        locals().get("submissions"),
        bg,
    ):
        if isinstance(descriptor, int):
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise
PY
fi

RUNNER_PID=$BASHPID
case "$RUNNER_PID" in
  ''|*[!0-9]*) die "main runner PID is invalid" 70 ;;
esac
[ "$RUNNER_PID" -eq "$$" ] || die "main runner PID identity is unstable" 70
[ -d "/proc/$RUNNER_PID/fd" ] || die "main runner descriptor directory is unavailable" 70
readonly RUNNER_PID
ATTEMPT_ROOT="/proc/$RUNNER_PID/fd/${BG_HIERARCHY_ATTEMPT_FD:-invalid}"
OPERATOR_LOGS="/proc/$RUNNER_PID/fd/${BG_HIERARCHY_LOGS_FD:-invalid}"
HIERARCHY_DRIFT=0

revalidate_hierarchy() {
  /usr/bin/python3 -I -S - "$BG_WORK" "$CELL_ID" "$ATTEMPT_ID" \
    "${BG_HIERARCHY_BG_FD:-}" "${BG_HIERARCHY_SUBMISSIONS_FD:-}" \
    "${BG_HIERARCHY_RUNS_FD:-}" \
    "${BG_HIERARCHY_CELL_FD:-}" "${BG_HIERARCHY_ATTEMPT_FD:-}" \
    "${BG_HIERARCHY_LOGS_FD:-}" <<'PY'
import os
import signal
import stat
import sys

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

bg_work, cell_id, attempt_id, *fd_text = sys.argv[1:]
if len(fd_text) != 6 or any(not value.isdecimal() for value in fd_text):
    raise SystemExit(1)
bg, submissions, runs, cell, attempt, logs = map(int, fd_text)
held = [os.fstat(value) for value in (bg, submissions, runs, cell, attempt, logs)]
for index, info in enumerate(held):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink < 2:
        raise SystemExit(1)
    mode = stat.S_IMODE(info.st_mode)
    if (index == 0 and mode & 0o022) or (index > 0 and mode != 0o700):
        raise SystemExit(1)

current = [
    os.stat(bg_work, follow_symlinks=False),
    os.stat("local_submissions", dir_fd=bg, follow_symlinks=False),
    os.stat("runs", dir_fd=bg, follow_symlinks=False),
    os.stat(cell_id, dir_fd=runs, follow_symlinks=False),
    os.stat(attempt_id, dir_fd=cell, follow_symlinks=False),
    os.stat("operator_logs", dir_fd=attempt, follow_symlinks=False),
]
for expected, observed in zip(held, current):
    if not stat.S_ISDIR(observed.st_mode):
        raise SystemExit(1)
    if (expected.st_dev, expected.st_ino) != (observed.st_dev, observed.st_ino):
        raise SystemExit(1)
PY
}

guard_hierarchy() {
  if ! revalidate_hierarchy; then
    HIERARCHY_DRIFT=1
    PIPELINE_RC=75
    TERMINAL_STATUS=LOCAL_CELL_FAILED
    return 75
  fi
  return 0
}

revalidate_runtime_contract() {
  guard_hierarchy || return 75
  local rc=0
  revalidate_contract || rc=$?
  guard_hierarchy || return 75
  return "$rc"
}

MONITOR_PID=
MONITOR_STARTED=0
MONITOR_STOPPED=0
MONITOR_PROBE_RC=125
MONITOR_WAIT_RC=125
MONITOR_HEALTHY=0
stop_monitor() {
  if [ "$MONITOR_STOPPED" -eq 1 ]; then
    return 0
  fi
  local wait_rc=125
  if [ -n "$MONITOR_PID" ]; then
    guard_hierarchy || true
    kill "$MONITOR_PID" 2>/dev/null || true
    local monitor_still_running=1
    local monitor_poll=0
    while [ "$monitor_poll" -lt 100 ]; do
      if ! kill -0 "$MONITOR_PID" 2>/dev/null; then
        monitor_still_running=0
        break
      fi
      guard_hierarchy || true
      sleep 0.05
      guard_hierarchy || true
      monitor_poll=$((monitor_poll + 1))
    done
    if [ "$monitor_still_running" -eq 1 ]; then
      kill -KILL "$MONITOR_PID" 2>/dev/null || true
    fi
    set +e
    wait "$MONITOR_PID"
    wait_rc=$?
    set -e
  fi
  MONITOR_WAIT_RC=$wait_rc
  if [ "$MONITOR_PROBE_RC" -eq 0 ] \
    && { [ "$MONITOR_WAIT_RC" -eq 0 ] || [ "$MONITOR_WAIT_RC" -eq 143 ]; } \
    && [ -s "$OPERATOR_LOGS/gpu_probe.csv" ] \
    && [ -s "$OPERATOR_LOGS/gpu_monitor.csv" ]; then
    MONITOR_HEALTHY=1
  else
    MONITOR_HEALTHY=0
  fi
  guard_hierarchy || true
  python3 -I -S - "$OPERATOR_LOGS/monitor.stopped.json" "$MONITOR_PID" "$wait_rc" "$MONITOR_PROBE_RC" "$MONITOR_HEALTHY" "$MONITOR_STARTED" <<'PY'
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

path = Path(sys.argv[1])
started = sys.argv[6] == "1"
pid = int(sys.argv[2]) if started else None
wait_rc = int(sys.argv[3])
probe_rc = int(sys.argv[4])
healthy = sys.argv[5] == "1"
payload = {
    "schema_version": "WSL2_LOCAL_GPU_MONITOR_STOP_V1",
    "status": "STOPPED",
    "wait_completed": True,
    "monitor_started": started,
    "monitor_pid": pid,
    "monitor_probe_exit_code": probe_rc,
    "wait_exit_code": wait_rc,
    "monitor_healthy": healthy,
    "stopped_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(content)
    handle.flush()
    os.fsync(handle.fileno())
PY
  guard_hierarchy || true
  MONITOR_STOPPED=1
}

seal_failure_output() {
  local finalizer_rc=$1
  local finalizer_log=$2
  local failure_class=${3:-FINALIZER_FAILURE}
  /usr/bin/python3 -I -S - \
    "$ATTEMPT_ROOT" "$OPERATOR_LOGS" "$CONTRACT" "$ENVIRONMENT_RECEIPT" \
    "$OPERATOR_LOGS/monitor.stopped.json" "$RECEIPT" "$finalizer_log" \
    "$PIPELINE_RC" "$finalizer_rc" "$UNIT" "$SUBMISSION_TOKEN" \
    "$SUBMISSION_INVOCATION_ID" "$EXECUTOR_UID" "$EXEC_START_SHA" "$FINALIZER" \
    "$failure_class" <<'PY'
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
from pathlib import Path

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

(root, logs, contract_path, environment_path, monitor_path, submission_path,
 finalizer_log, pipeline_text, finalizer_text, unit, token, invocation,
 executor_text, exec_start_sha, finalizer_path, failure_class) = sys.argv[1:]
root = Path(root)
logs = Path(logs)
contract_path = Path(contract_path)
environment_path = Path(environment_path)
monitor_path = Path(monitor_path)
submission_path = Path(submission_path)
finalizer_log = Path(finalizer_log)
finalizer_path = Path(finalizer_path)
pipeline_rc = int(pipeline_text)
finalizer_rc = int(finalizer_text)
executor_uid = int(executor_text)
if not 1 <= pipeline_rc <= 255 or not 1 <= finalizer_rc <= 255:
    raise ValueError("emergency sealing requires non-zero POSIX exit codes")
if failure_class not in {"FINALIZER_FAILURE", "ANCESTOR_IDENTITY_DRIFT"}:
    raise ValueError("invalid emergency failure class")

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

def load(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or unsafe emergency input: {path}")
    return json.loads(path.read_text(encoding="utf-8"))

contract = load(contract_path)
environment = load(environment_path)
monitor = load(monitor_path)
submission = load(submission_path)
if monitor.get("status") != "STOPPED" or monitor.get("wait_completed") is not True:
    raise ValueError("monitor is not stopped")
if (submission.get("executor_uid") != executor_uid or submission.get("unit") != unit
        or submission.get("submission_token") != token
        or submission.get("invocation_id") != invocation
        or submission.get("exec_start_sha256") != exec_start_sha):
    raise ValueError("emergency submission binding mismatch")
if executor_uid != os.getuid():
    raise ValueError("emergency executor UID mismatch")
probe = "PROBE" in str(contract.get("run_kind", "")).upper()
marker_name = "probe.EMERGENCY_FAILURE.json" if probe else "cell.EMERGENCY_FAILURE.json"
marker = logs / marker_name
manifest = logs / "emergency_output_SHA256SUMS"
if marker.is_symlink() or manifest.is_symlink():
    raise ValueError("unsafe emergency terminal path")

def collect():
    records = []
    excluded = {
        "operator_logs/emergency_output_SHA256SUMS",
        f"operator_logs/{marker_name}",
    }
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort(key=lambda value: value.encode("utf-8"))
        files.sort(key=lambda value: value.encode("utf-8"))
        base = Path(directory)
        for name in names:
            candidate = base / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError("unsafe emergency output directory")
        for name in files:
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"unsafe emergency output member: {relative}")
            if relative in excluded:
                continue
            records.append((relative, digest(candidate)))
    records.sort(key=lambda item: item[0].encode("utf-8"))
    if not records:
        raise ValueError("empty emergency output")
    return records

records = collect()
manifest_bytes = "".join(f"{value}  ./{relative}\n" for relative, value in records).encode()

def publish(path, content):
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"existing emergency evidence differs: {path}")
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)

publish(manifest, manifest_bytes)
if collect() != records:
    raise ValueError("output changed during emergency manifest publication")
payload = {
    "schema_version": "WSL2_BOLTZGEN_LOCAL_EMERGENCY_FAILURE_V1",
    "status": "EMERGENCY_FAILURE",
    "terminal_status": "LOCAL_CELL_FAILED",
    "pipeline_exit_code": pipeline_rc,
    "failure_class": failure_class,
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": contract["cell_id"],
    "attempt_id": contract["attempt_id"],
    "run_kind": contract["run_kind"],
    "formal_g1": bool(environment.get("formal_g1")),
    "formal_g1_receipt_sha256": digest(environment_path) if environment.get("formal_g1") is True else None,
    "environment_manifest_sha256": (
        contract.get("environment_provenance_manifest_sha256")
        if environment.get("formal_g1") is True else None
    ),
    "completed_at_utc": monitor["stopped_at_utc"],
    "execution_contract_sha256": digest(contract_path),
    "environment_receipt_sha256": digest(environment_path),
    "monitor_stopped_sha256": digest(monitor_path),
    "monitor_healthy": monitor.get("monitor_healthy") is True,
    "submission_receipt_sha256": digest(submission_path),
    "systemd_unit": unit,
    "submission_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    "invocation_id": invocation,
    "executor_uid": executor_uid,
    "exec_start_sha256": exec_start_sha,
    "finalizer_exit_code": finalizer_rc,
    "finalizer_log_sha256": digest(finalizer_log),
    "finalizer_sha256": digest(finalizer_path),
    "runtime_scripts_manifest_sha256": contract["runtime_scripts_manifest_sha256"],
    "model_inputs_manifest_sha256": contract["model_inputs_manifest_sha256"],
    "spec_gate_bundle_sha256": contract["spec_gate_bundle_sha256"],
    "emergency_output_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "emergency_output_manifest_entry_count": len(records),
    "evidence_freeze_schema_version": "WSL2_OUTPUT_EVIDENCE_FREEZE_V1",
    "evidence_files_read_only": True,
    "evidence_directories_read_only": True,
}
marker_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
publish(marker, marker_bytes)
file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
directory_flags = file_flags | os.O_DIRECTORY

def freeze_path(path, expected_kind, mode):
    if expected_kind == "directory" and path == root:
        descriptor = os.dup(int(root.name))
    else:
        descriptor = os.open(path, directory_flags if expected_kind == "directory" else file_flags)
    try:
        info = os.fstat(descriptor)
        valid = stat.S_ISDIR(info.st_mode) if expected_kind == "directory" else stat.S_ISREG(info.st_mode)
        if not valid:
            raise ValueError(f"emergency freeze target changed type: {path}")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

for directory, names, files in os.walk(root, topdown=False, followlinks=False):
    base = Path(directory)
    for name in files:
        freeze_path(base / name, "file", 0o444)
    for name in names:
        freeze_path(base / name, "directory", 0o500)
    freeze_path(base, "directory", 0o500)
for directory in (logs, root):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

PIPELINE_RC=70
TERMINAL_STATUS=LOCAL_CELL_FAILED
FINALIZATION_COMPLETE=0
FINALIZATION_ACTIVE=0
DEFERRED_SIGNAL_RC=0
FINALIZER_WRAPPER_PID=
FINALIZER_LAUNCHED=0
CANONICAL_TERMINAL_STATUS_RC=
prepare_memory_probe_evidence() {
  [ "$MEMORY_PROBE" -eq 1 ] || return 0

  local peak_rc=75
  if [ "$MONITOR_STOPPED" -eq 1 ] && [ "$HIERARCHY_DRIFT" -eq 0 ] \
      && guard_hierarchy; then
    set +e
    "$ENV_PYTHON" -I "$FINALIZER" compute-peak-memory \
      --gpu-monitor "$OPERATOR_LOGS_LEXICAL/gpu_monitor.csv" \
      --output "$OPERATOR_LOGS_LEXICAL/peak_memory_fraction.txt" \
      >"$OPERATOR_LOGS/peak_memory.helper.stdout.txt" \
      2>"$OPERATOR_LOGS/peak_memory.helper.stderr.txt"
    peak_rc=$?
    set -e
    guard_hierarchy || peak_rc=75
    if [ "$HIERARCHY_DRIFT" -eq 0 ]; then
      printf '%s\n' "$peak_rc" >"$OPERATOR_LOGS/peak_memory.helper.exit_code.txt"
    fi
  fi
  if [ "$peak_rc" -ne 0 ] && [ "$PIPELINE_RC" -eq 0 ]; then
    PIPELINE_RC=$peak_rc
  fi

  if [ "$PIPELINE_RC" -eq 0 ]; then
    TERMINAL_STATUS=$SUCCESS_STATUS
    return 0
  fi

  TERMINAL_STATUS=LOCAL_CELL_FAILED
  if [ "$ENGINEERING_MEMORY_PROBE" -ne 1 ]; then
    return 0
  fi

  local oom_rc=75
  if [ "$HIERARCHY_DRIFT" -eq 0 ] && guard_hierarchy; then
    set +e
    "$ENV_PYTHON" -I "$FINALIZER" detect-gpu-oom \
      --attempt-root "$ATTEMPT_ROOT_LEXICAL" \
      >"$OPERATOR_LOGS/gpu_oom_detection.helper.stdout.txt" \
      2>"$OPERATOR_LOGS/gpu_oom_detection.helper.stderr.txt"
    oom_rc=$?
    set -e
    guard_hierarchy || oom_rc=75
    if [ "$HIERARCHY_DRIFT" -eq 0 ]; then
      printf '%s\n' "$oom_rc" >"$OPERATOR_LOGS/gpu_oom_detection.helper.exit_code.txt"
    fi
  fi
  case "$oom_rc" in
    0)
      TERMINAL_STATUS=BLOCKED_GPU_MEMORY
      ;;
    2)
      TERMINAL_STATUS=LOCAL_CELL_FAILED
      ;;
    *)
      # A helper/evidence error must not silently become an OOM classification.
      PIPELINE_RC=$oom_rc
      [ "$PIPELINE_RC" -ne 0 ] || PIPELINE_RC=75
      TERMINAL_STATUS=LOCAL_CELL_FAILED
      ;;
  esac
}

finalize_current_attempt() {
  stop_monitor || return $?
  prepare_memory_probe_evidence
  local rc emergency_rc
  local finalizer_log="$OPERATOR_LOGS/finalizer.log.txt"
  local finalizer_capture="$SUBMISSION_BASE.finalizer.capture.tmp"

  write_finalizer_log() {
    local content=$1
    /usr/bin/python3 -I -S - "${BG_HIERARCHY_LOGS_FD:-}" "$content" <<'PY'
import os
import signal
import stat
import sys

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

if not sys.argv[1].isdecimal():
    raise SystemExit(75)
logs = int(sys.argv[1])
content = (sys.argv[2] + "\n").encode("utf-8")
parent_info = os.fstat(logs)
if (
    not stat.S_ISDIR(parent_info.st_mode)
    or parent_info.st_uid != os.getuid()
    or parent_info.st_nlink < 2
    or stat.S_IMODE(parent_info.st_mode) != 0o700
):
    raise SystemExit(75)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open("finalizer.log.txt", flags, 0o600, dir_fd=logs)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(content)
    handle.flush()
    os.fchmod(handle.fileno(), 0o600)
    os.fsync(handle.fileno())
os.fsync(logs)
PY
  }

  run_bound_finalizer() {
    exec /usr/bin/python3 -I -S - \
      "$finalizer_capture" "$SUBMISSION_BASE" \
      "${BG_HIERARCHY_SUBMISSIONS_FD:-}" "$RUNNER_PID" \
      "$BG_WORK" "$CELL_ID" "$ATTEMPT_ID" \
      "${BG_HIERARCHY_BG_FD:-}" "${BG_HIERARCHY_RUNS_FD:-}" \
      "${BG_HIERARCHY_CELL_FD:-}" "${BG_HIERARCHY_ATTEMPT_FD:-}" \
      "${BG_HIERARCHY_LOGS_FD:-}" \
      "$ENV_PYTHON" -I "$FINALIZER" \
      --attempt-root "$ATTEMPT_ROOT_LEXICAL" \
      --cell-contract "$CONTRACT" \
      --environment-receipt "$ENVIRONMENT_RECEIPT" \
      --submission-receipt "$RECEIPT" \
      --monitor-stopped "$OPERATOR_LOGS_LEXICAL/monitor.stopped.json" \
      --terminal-status "$TERMINAL_STATUS" \
      --pipeline-exit-code "$PIPELINE_RC" <<'PY'
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

capture = Path(sys.argv[1])
submission_base = Path(sys.argv[2])
submissions_text = sys.argv[3]
runner_text, bg_work, cell_id, attempt_id = sys.argv[4:8]
hierarchy_text = sys.argv[8:13]
command = sys.argv[13:]
expected_capture = Path(f"{submission_base}.finalizer.capture.tmp")
parent = submission_base.parent
if (
    capture != expected_capture
    or capture.parent != parent
    or parent.name != "local_submissions"
    or not submissions_text.isdecimal()
    or not runner_text.isdecimal()
    or int(runner_text) != os.getppid()
    or not Path(bg_work).is_absolute()
    or not cell_id
    or not attempt_id
    or any("/" in value or "\\" in value for value in (cell_id, attempt_id))
    or len(hierarchy_text) != 5
    or any(not value.isdecimal() for value in hierarchy_text)
    or not command
):
    raise SystemExit(75)
parent_descriptor = int(submissions_text)
hierarchy_descriptors = tuple(map(int, hierarchy_text))
if (
    parent_descriptor < 3
    or any(value < 3 for value in hierarchy_descriptors)
    or parent_descriptor in hierarchy_descriptors
    or len(set(hierarchy_descriptors)) != 5
):
    raise SystemExit(75)
try:
    parent_info = os.fstat(parent_descriptor)
    held = [os.fstat(value) for value in hierarchy_descriptors]
    bg, runs, cell, attempt, logs = hierarchy_descriptors
    current = [
        os.stat(bg_work, follow_symlinks=False),
        os.stat("runs", dir_fd=bg, follow_symlinks=False),
        os.stat(cell_id, dir_fd=runs, follow_symlinks=False),
        os.stat(attempt_id, dir_fd=cell, follow_symlinks=False),
        os.stat("operator_logs", dir_fd=attempt, follow_symlinks=False),
    ]
    current_submissions = os.stat(
        "local_submissions", dir_fd=bg, follow_symlinks=False
    )
except OSError:
    raise SystemExit(75)
if (
    not stat.S_ISDIR(parent_info.st_mode)
    or parent_info.st_uid != os.getuid()
    or parent_info.st_nlink < 2
    or stat.S_IMODE(parent_info.st_mode) != 0o700
    or (parent_info.st_dev, parent_info.st_ino)
    != (current_submissions.st_dev, current_submissions.st_ino)
):
    raise SystemExit(75)
for index, (expected, observed) in enumerate(zip(held, current)):
    if (
        not stat.S_ISDIR(expected.st_mode)
        or expected.st_uid != os.getuid()
        or expected.st_nlink < 2
        or (index == 0 and stat.S_IMODE(expected.st_mode) & 0o022)
        or (index > 0 and stat.S_IMODE(expected.st_mode) != 0o700)
        or not stat.S_ISDIR(observed.st_mode)
        or (expected.st_dev, expected.st_ino)
        != (observed.st_dev, observed.st_ino)
    ):
        raise SystemExit(75)

def option(name):
    positions = [index for index, value in enumerate(command) if value == name]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise SystemExit(75)
    return command[positions[0] + 1]

expected_attempt = str(Path(bg_work) / "runs" / cell_id / attempt_id)
expected_logs = f"{expected_attempt}/operator_logs"
if (
    option("--attempt-root") != expected_attempt
    or option("--monitor-stopped") != f"{expected_logs}/monitor.stopped.json"
):
    raise SystemExit(75)
try:
    pinned_attempt = os.stat(
        f"/proc/{runner_text}/fd/{attempt}", follow_symlinks=True
    )
    pinned_logs = os.stat(f"/proc/{runner_text}/fd/{logs}", follow_symlinks=True)
except OSError:
    raise SystemExit(75)
if (
    (pinned_attempt.st_dev, pinned_attempt.st_ino)
    != (held[3].st_dev, held[3].st_ino)
    or (pinned_logs.st_dev, pinned_logs.st_ino)
    != (held[4].st_dev, held[4].st_ino)
):
    raise SystemExit(75)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(capture.name, flags, 0o600, dir_fd=parent_descriptor)
with os.fdopen(descriptor, "wb", buffering=0) as output:
    os.fchmod(output.fileno(), 0o600)
    child = None
    caught_signal = 0

    def relay_signal(signum, _frame):
        nonlocal_state[0] = nonlocal_state[0] or signum
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    nonlocal_state = [0]
    for caught in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(caught, relay_signal)
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            close_fds=True,
            pass_fds=hierarchy_descriptors,
        )
        if nonlocal_state[0]:
            child.send_signal(nonlocal_state[0])
        return_code = child.wait()
        caught_signal = nonlocal_state[0]
    except OSError as exc:
        output.write(
            f"finalizer launcher failed: {exc}\n".encode(
                "utf-8", errors="replace"
            )
        )
        return_code = 70
    os.fsync(output.fileno())
os.fsync(parent_descriptor)
if return_code < 0:
    return_code = 128 - return_code
elif caught_signal and return_code == 0:
    return_code = 128 + caught_signal
raise SystemExit(return_code)
PY
  }

  ensure_signal_finalizer_capture() {
    local signal_rc=$1
    /usr/bin/python3 -I -S - \
      "$finalizer_capture" "$SUBMISSION_BASE" \
      "${BG_HIERARCHY_SUBMISSIONS_FD:-}" "$signal_rc" <<'PY'
import os
import signal
import stat
import sys
from pathlib import Path

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

capture = Path(sys.argv[1])
submission_base = Path(sys.argv[2])
submissions_text = sys.argv[3]
signal_text = sys.argv[4]
expected_capture = Path(f"{submission_base}.finalizer.capture.tmp")
if (
    capture != expected_capture
    or capture.parent != submission_base.parent
    or capture.parent.name != "local_submissions"
    or not submissions_text.isdecimal()
    or not signal_text.isdecimal()
):
    raise SystemExit(75)
signal_rc = int(signal_text)
if not 129 <= signal_rc <= 192:
    raise SystemExit(75)
submissions = int(submissions_text)
parent_info = os.fstat(submissions)
if (
    not stat.S_ISDIR(parent_info.st_mode)
    or parent_info.st_uid != os.getuid()
    or parent_info.st_nlink < 2
    or stat.S_IMODE(parent_info.st_mode) != 0o700
):
    raise SystemExit(75)
read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(capture.name, read_flags, dir_fd=submissions)
except FileNotFoundError:
    content = (
        f"finalizer wrapper terminated before capture initialization: rc={signal_rc}\n"
    ).encode("ascii")
    write_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(capture.name, write_flags, 0o600, dir_fd=submissions)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fchmod(output.fileno(), 0o600)
        os.fsync(output.fileno())
else:
    with os.fdopen(descriptor, "rb") as existing:
        info = os.fstat(existing.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SystemExit(75)
        os.fsync(existing.fileno())
os.fsync(submissions)
PY
  }

  finish_finalizer_capture() {
    local action=$1
    /usr/bin/python3 -I -S - \
      "$finalizer_capture" "$SUBMISSION_BASE" "$action" \
      "${BG_HIERARCHY_SUBMISSIONS_FD:-}" \
      "${BG_HIERARCHY_LOGS_FD:-}" <<'PY'
import os
import signal
import stat
import sys
from pathlib import Path

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

capture = Path(sys.argv[1])
submission_base = Path(sys.argv[2])
action = sys.argv[3]
submissions_text = sys.argv[4]
logs_text = sys.argv[5]
source_parent = submission_base.parent
expected_capture = Path(f"{submission_base}.finalizer.capture.tmp")
if (
    action not in {"discard", "publish"}
    or capture != expected_capture
    or capture.parent != source_parent
    or source_parent.name != "local_submissions"
    or not submissions_text.isdecimal()
    or not logs_text.isdecimal()
):
    raise SystemExit(75)
source_parent_descriptor = int(submissions_text)
logs = int(logs_text)
source_parent_info = os.fstat(source_parent_descriptor)
if (
    not stat.S_ISDIR(source_parent_info.st_mode)
    or source_parent_info.st_uid != os.getuid()
    or source_parent_info.st_nlink < 2
    or stat.S_IMODE(source_parent_info.st_mode) != 0o700
):
    raise SystemExit(75)
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(capture.name, flags, dir_fd=source_parent_descriptor)
try:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
    ):
        raise SystemExit(75)
    current = os.stat(
        capture.name, dir_fd=source_parent_descriptor, follow_symlinks=False
    )
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise SystemExit(75)
    os.fsync(descriptor)
    linked_by_us = False
    source_unlinked = False
    try:
        if action == "publish":
            logs_info = os.fstat(logs)
            if (
                not stat.S_ISDIR(logs_info.st_mode)
                or logs_info.st_uid != os.getuid()
                or logs_info.st_nlink < 2
                or stat.S_IMODE(logs_info.st_mode) != 0o700
            ):
                raise SystemExit(75)
            os.link(
                capture.name,
                "finalizer.log.txt",
                src_dir_fd=source_parent_descriptor,
                dst_dir_fd=logs,
                follow_symlinks=False,
            )
            linked_by_us = True
            linked = os.stat(
                "finalizer.log.txt", dir_fd=logs, follow_symlinks=False
            )
            after_link = os.fstat(descriptor)
            if (
                (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino)
                or (after_link.st_dev, after_link.st_ino)
                != (before.st_dev, before.st_ino)
                or after_link.st_nlink != 2
                or after_link.st_size != before.st_size
                or after_link.st_mtime_ns != before.st_mtime_ns
            ):
                raise SystemExit(75)
            os.fsync(logs)
        os.unlink(capture.name, dir_fd=source_parent_descriptor)
        source_unlinked = True
        after_unlink = os.fstat(descriptor)
        if after_unlink.st_nlink != (1 if action == "publish" else 0):
            raise SystemExit(75)
        os.fsync(source_parent_descriptor)
    except BaseException:
        if linked_by_us:
            if source_unlinked:
                os.link(
                    "finalizer.log.txt",
                    capture.name,
                    src_dir_fd=logs,
                    dst_dir_fd=source_parent_descriptor,
                    follow_symlinks=False,
                )
                os.fsync(source_parent_descriptor)
            os.unlink("finalizer.log.txt", dir_fd=logs)
            os.fsync(logs)
        raise
finally:
    os.close(descriptor)
PY
  }

  canonical_terminal_marker_valid() {
    local shape_rc=0 status_rc=0
    /usr/bin/python3 -I -S - "${BG_HIERARCHY_LOGS_FD:-}" <<'PY' || shape_rc=$?
import os
import signal
import stat
import sys

for blocked_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(blocked_signal, signal.SIG_IGN)

if not sys.argv[1].isdecimal():
    raise SystemExit(75)
logs = int(sys.argv[1])
logs_info = os.fstat(logs)
if (
    not stat.S_ISDIR(logs_info.st_mode)
    or logs_info.st_uid != os.getuid()
    or logs_info.st_nlink < 2
):
    raise SystemExit(75)
terminal_names = (
    "cell.SUCCESS.json",
    "cell.FAILURE.json",
    "probe.SUCCESS.json",
    "probe.FAILURE.json",
)
found = []
for name in terminal_names:
    try:
        info = os.stat(name, dir_fd=logs, follow_symlinks=False)
    except FileNotFoundError:
        continue
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o222
        or info.st_nlink != 1
    ):
        raise SystemExit(75)
    found.append(name)
if not found:
    raise SystemExit(1)
if len(found) != 1:
    raise SystemExit(75)
try:
    manifest = os.stat("output_SHA256SUMS", dir_fd=logs, follow_symlinks=False)
except FileNotFoundError:
    raise SystemExit(75)
if (
    not stat.S_ISREG(manifest.st_mode)
    or manifest.st_uid != os.getuid()
    or manifest.st_mode & 0o222
    or manifest.st_nlink != 1
):
    raise SystemExit(75)
raise SystemExit(0)
PY
    [ "$shape_rc" -eq 0 ] || return "$shape_rc"
    revalidate_runtime_contract || return 75
    if "$BG_WORK/status_local_cell.sh" \
      "$BG_WORK" "$CELL_ID" "$ATTEMPT_ID" >/dev/null 2>&1; then
      status_rc=0
    else
      status_rc=$?
    fi
    revalidate_runtime_contract || return 75
    if [ "$status_rc" -eq 0 ] || [ "$status_rc" -eq 1 ]; then
      CANONICAL_TERMINAL_STATUS_RC=$status_rc
      return 0
    fi
    return 75
  }

  if ! guard_hierarchy; then
    write_finalizer_log 'normal finalizer suppressed: run hierarchy identity drifted' || return $?
    PIPELINE_RC=75
    TERMINAL_STATUS=LOCAL_CELL_FAILED
    if seal_failure_output 75 "$finalizer_log" ANCESTOR_IDENTITY_DRIFT; then
      emergency_rc=0
    else
      emergency_rc=$?
    fi
    return "$emergency_rc"
  fi
  verify_executor_gpu_lock || return 75
  if ! guard_hierarchy; then
    write_finalizer_log 'normal finalizer suppressed: run hierarchy identity drifted' || return $?
    PIPELINE_RC=75
    TERMINAL_STATUS=LOCAL_CELL_FAILED
    if seal_failure_output 75 "$finalizer_log" ANCESTOR_IDENTITY_DRIFT; then
      emergency_rc=0
    else
      emergency_rc=$?
    fi
    return "$emergency_rc"
  fi
  run_bound_finalizer &
  FINALIZER_WRAPPER_PID=$!
  FINALIZER_LAUNCHED=1
  while true; do
    if wait "$FINALIZER_WRAPPER_PID"; then
      rc=0
      break
    else
      rc=$?
    fi
    if ! kill -0 "$FINALIZER_WRAPPER_PID" 2>/dev/null; then
      break
    fi
  done
  FINALIZER_WRAPPER_PID=
  local canonical_terminal=0 marker_rc=0
  if canonical_terminal_marker_valid; then
    canonical_terminal=1
  else
    marker_rc=$?
    if [ "$marker_rc" -ne 1 ]; then
      finish_finalizer_capture discard || return $?
      return "$marker_rc"
    fi
  fi
  if ! guard_hierarchy; then
    if [ "$canonical_terminal" -eq 1 ]; then
      finish_finalizer_capture discard || return $?
      return 75
    fi
    finish_finalizer_capture publish || return $?
    PIPELINE_RC=75
    TERMINAL_STATUS=LOCAL_CELL_FAILED
    if seal_failure_output 75 "$finalizer_log" ANCESTOR_IDENTITY_DRIFT; then
      emergency_rc=0
    else
      emergency_rc=$?
    fi
    return "$emergency_rc"
  fi
  if [ "$rc" -eq 0 ] && [ "$canonical_terminal" -ne 1 ]; then
    printf '%s\n' 'run_local_cell: canonical finalizer returned success without a terminal marker' >&2
    rc=70
  fi
  if [ "$rc" -ne 0 ]; then
    if [ "$canonical_terminal" -eq 1 ]; then
      finish_finalizer_capture discard || return $?
      return 0
    fi
    printf 'run_local_cell: canonical finalization failed with rc=%s; sealing emergency evidence\n' "$rc" >&2
    if [ "$rc" -ge 129 ] && [ "$rc" -le 192 ]; then
      ensure_signal_finalizer_capture "$rc" || return $?
    fi
    finish_finalizer_capture publish || return $?
    guard_hierarchy || return 75
    if [ "$PIPELINE_RC" -eq 0 ]; then
      PIPELINE_RC=$rc
      [ "$PIPELINE_RC" -ne 0 ] || PIPELINE_RC=70
    fi
    TERMINAL_STATUS=LOCAL_CELL_FAILED
    if seal_failure_output "$rc" "$finalizer_log" FINALIZER_FAILURE; then
      emergency_rc=0
    else
      emergency_rc=$?
    fi
    if [ "$emergency_rc" -ne 0 ]; then
      printf 'run_local_cell: emergency evidence sealing failed with rc=%s\n' "$emergency_rc" >&2
      return "$rc"
    fi
    return 0
  fi
  finish_finalizer_capture discard || return $?
  return 0
}

handle_runner_signal() {
  local signal_rc=$1
  if [ "$FINALIZATION_ACTIVE" -eq 1 ]; then
    if [ "$DEFERRED_SIGNAL_RC" -eq 0 ]; then
      DEFERRED_SIGNAL_RC=$signal_rc
    fi
    if [ "$FINALIZER_LAUNCHED" -eq 0 ]; then
      PIPELINE_RC=$signal_rc
      TERMINAL_STATUS=LOCAL_CELL_FAILED
    elif [ -n "$FINALIZER_WRAPPER_PID" ]; then
      kill "-$((signal_rc - 128))" "$FINALIZER_WRAPPER_PID" 2>/dev/null || true
    fi
    return 0
  fi
  PIPELINE_RC=$signal_rc
  exit "$signal_rc"
}

run_finalization_once() {
  if [ "$FINALIZATION_COMPLETE" -eq 1 ]; then
    return 0
  fi
  if [ "$FINALIZATION_ACTIVE" -eq 1 ]; then
    return 75
  fi
  FINALIZATION_ACTIVE=1
  local rc=0
  finalize_current_attempt || rc=$?
  if [ "$DEFERRED_SIGNAL_RC" -ne 0 ] && [ "$rc" -eq 0 ] \
    && [ "$CANONICAL_TERMINAL_STATUS_RC" = 0 ]; then
    # A fully validated canonical SUCCESS is the commit point; a signal that
    # arrived after that point cannot safely rewrite the sealed attempt.
    PIPELINE_RC=0
    DEFERRED_SIGNAL_RC=0
  fi
  FINALIZATION_COMPLETE=1
  trap - EXIT HUP INT TERM
  FINALIZER_LAUNCHED=0
  FINALIZATION_ACTIVE=0
  return "$rc"
}

on_exit() {
  local shell_rc=$?
  trap - EXIT
  if [ "$FINALIZATION_COMPLETE" -ne 1 ] && [ "$FINALIZATION_ACTIVE" -ne 1 ]; then
    [ "$PIPELINE_RC" -ne 0 ] || PIPELINE_RC=$shell_rc
    [ "$PIPELINE_RC" -ne 0 ] || PIPELINE_RC=70
    TERMINAL_STATUS=LOCAL_CELL_FAILED
    run_finalization_once || true
  fi
  exit "$shell_rc"
}
trap on_exit EXIT
trap 'handle_runner_signal 129' HUP
trap 'handle_runner_signal 130' INT
trap 'handle_runner_signal 143' TERM

if guard_hierarchy; then
  set +e
  nvidia-smi \
    --query-gpu=timestamp,index,name,memory.total,memory.used,utilization.gpu,power.draw \
    --format=csv \
    >"$OPERATOR_LOGS/gpu_probe.csv" 2>"$OPERATOR_LOGS/gpu_probe.stderr.txt"
  MONITOR_PROBE_RC=$?
  set -e
  guard_hierarchy || MONITOR_PROBE_RC=75
else
  MONITOR_PROBE_RC=75
fi

if [ "$HIERARCHY_DRIFT" -eq 0 ] && guard_hierarchy; then
  stdbuf -oL -eL nvidia-smi \
    --query-gpu=timestamp,index,name,memory.total,memory.used,utilization.gpu,power.draw \
    --format=csv --loop=1 \
    >"$OPERATOR_LOGS/gpu_monitor.csv" 2>"$OPERATOR_LOGS/gpu_monitor.stderr.txt" &
  MONITOR_PID=$!
  MONITOR_STARTED=1
  guard_hierarchy || true
fi

verify_environment() {
  local label=$1
  local stage_id="$CELL_ID.$ATTEMPT_ID.$label"
  revalidate_runtime_contract || return $?
  guard_hierarchy || return 75
  set +e
  "$VERIFY_STAGE" "$BG_WORK" "$stage_id" \
    >"$OPERATOR_LOGS/$label.environment.stdout.txt" \
    2>"$OPERATOR_LOGS/$label.environment.stderr.txt"
  local rc=$?
  set -e
  guard_hierarchy || return 75
  printf '%s\n' "$rc" > "$OPERATOR_LOGS/$label.environment.exit_code.txt"
  [ "$rc" -eq 0 ] || return "$rc"
  local bound
  guard_hierarchy || return 75
  if ! bound=$(python3 -I -S - \
      "$OPERATOR_LOGS/$label.environment.stdout.txt" "$BG_WORK" "$stage_id" \
      "$ENVIRONMENT_ROOT" "$ENVIRONMENT_RECEIPT" "$RUNTIME_MANIFEST" \
      "$RUNTIME_MANIFEST_SHA" "$BOLTZGEN" "$LAUNCHER_SHA" \
      "$ENVIRONMENT_RECEIPT_SHA" "$STAGE_CLASS" "$EXECUTOR_UID" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

stdout, bg_work, stage_id, environment_root, receipt, runtime_manifest, runtime_sha, launcher, launcher_sha, receipt_sha, stage_class, executor_uid = sys.argv[1:]
lines = Path(stdout).read_text(encoding="utf-8").splitlines()
if len(lines) != 1 or not lines[0]:
    raise ValueError("stage verifier did not return exactly one audit path")
audit = Path(lines[0])
expected_audit = Path(bg_work) / "stage_audits" / stage_id
if audit != expected_audit or audit.is_symlink() or not audit.is_dir() or audit.resolve() != audit:
    raise ValueError("stage audit path is unsafe or unexpected")
manifest = audit / "stage_environment.SHA256SUMS"
if manifest.is_symlink() or not manifest.is_file():
    raise ValueError("stage audit manifest is missing or unsafe")
pattern = re.compile(r"([0-9a-f]{64})  \./([^\n\r\0]+)")
raw = manifest.read_text(encoding="utf-8")
if not raw.endswith("\n") or "\r" in raw or "\0" in raw:
    raise ValueError("stage audit manifest framing is invalid")
members = []
for line in raw.splitlines():
    match = pattern.fullmatch(line)
    if match is None:
        raise ValueError("invalid stage audit manifest line")
    expected, relative = match.groups()
    pure = Path(relative)
    if relative.startswith("/") or "\\" in relative or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("unsafe stage audit member")
    target = audit / pure
    if target.is_symlink() or not target.is_file() or target.resolve().parent != audit:
        raise ValueError("unsafe stage audit file")
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise ValueError("stage audit member SHA-256 mismatch")
    members.append(relative)
if members != sorted(members, key=lambda value: value.encode("utf-8")) or "contract_binding.json" not in members:
    raise ValueError("stage audit manifest order/member mismatch")
binding = json.loads((audit / "contract_binding.json").read_text(encoding="utf-8"))
expected = {
    "schema_version": "WSL2_GPU_STAGE_CONTRACT_BINDING_V1",
    "stage_id": stage_id,
    "stage_class": stage_class,
    "executor_uid": int(executor_uid),
    "environment_root": environment_root,
    "receipt_path": receipt,
    "receipt_sha256": receipt_sha,
    "runtime_scripts_manifest_path": runtime_manifest,
    "runtime_scripts_manifest_sha256": runtime_sha,
    "environment_launcher": launcher,
    "environment_launcher_sha256": launcher_sha,
}
for key, value in expected.items():
    if binding.get(key) != value:
        raise ValueError(f"stage binding mismatch: {key}")
python_path = binding.get("environment_python")
if python_path != str(Path(environment_root) / "bin" / "python"):
    raise ValueError("stage binding environment Python mismatch")
print(python_path)
print(binding["environment_launcher"])
PY
  ); then
    return 65
  fi
  guard_hierarchy || return 75
  mapfile -t bound_values <<< "$bound"
  [ "${#bound_values[@]}" -eq 2 ] || return 65
  ENV_PYTHON=${bound_values[0]}
  BOLTZGEN=${bound_values[1]}
  revalidate_runtime_contract || return $?
  return 0
}

run_configure() {
  revalidate_runtime_contract || return $?
  verify_environment configure || return $?
  guard_hierarchy || return 75
  set +e
  "$BOLTZGEN" configure "$SPEC_PATH" \
    --output "$ATTEMPT_ROOT" \
    --protocol "$PROTOCOL" \
    --num_designs "$EXPECTED_DESIGNS" \
    --budget "$BUDGET" \
    --diffusion_batch_size "$DIFFUSION_BATCH_SIZE" \
    --inverse_fold_num_sequences "$INVERSE_FOLD_NUM_SEQUENCES" \
    --design_checkpoints "$DESIGN_CHECKPOINT" \
    --inverse_fold_checkpoint "$INVERSE_CHECKPOINT" \
    --folding_checkpoint "$FOLDING_CHECKPOINT" \
    --moldir "$MOLS_PATH" \
    --devices "$DEVICES" \
    --num_workers "$NUM_WORKERS" \
    --use_kernels "$USE_KERNELS" \
    --config analysis "liability_modality=$ANALYSIS_MODALITY" \
    --config filtering "modality=$FILTERING_MODALITY" "filter_bindingsite=$FILTER_BINDINGSITE" \
    >"$OPERATOR_LOGS/configure.stdout.txt" 2>"$OPERATOR_LOGS/configure.stderr.txt"
  local rc=$?
  set -e
  guard_hierarchy || return 75
  printf '%s\n' "$rc" > "$OPERATOR_LOGS/configure.exit_code.txt"
  return "$rc"
}

build_resolved_config_manifest() {
  guard_hierarchy || return 75
  local rc=0
  set +e
  python3 -I -S - "$ATTEMPT_ROOT" "$OPERATOR_LOGS/resolved_config_SHA256SUMS" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = root / "config"
output = Path(sys.argv[2])
if config.is_symlink() or not config.is_dir():
    raise ValueError("resolved config directory is missing or unsafe")
records = []
for path in config.rglob("*"):
    relative = path.relative_to(root).as_posix()
    if path.is_symlink():
        raise ValueError(f"resolved config contains symlink: {relative}")
    mode = path.stat().st_mode
    if stat.S_ISDIR(mode):
        continue
    if not stat.S_ISREG(mode):
        raise ValueError(f"resolved config contains special file: {relative}")
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    records.append((relative, value))
required = {
    "config/analysis.yaml",
    "config/design.yaml",
    "config/filtering.yaml",
    "config/folding.yaml",
    "config/inverse_folding.yaml",
}
observed = {relative for relative, _ in records}
if observed != required:
    raise ValueError(
        f"resolved config file set differs: missing={sorted(required - observed)} "
        f"unexpected={sorted(observed - required)}"
    )
records.sort(key=lambda item: item[0].encode("utf-8"))
content = "".join(f"{value}  ./{relative}\n" for relative, value in records).encode()
descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(content)
    handle.flush()
    os.fsync(handle.fileno())
PY
  rc=$?
  set -e
  guard_hierarchy || return 75
  return "$rc"
}

run_stage() {
  local stage=$1
  revalidate_runtime_contract || return $?
  verify_environment "$stage" || return $?
  guard_hierarchy || return 75
  set +e
  # The validated local-cell contract fixes devices=1.  Keep each stage in
  # the launcher process so its CURRENT_PROCESS_ONLY NVML compatibility
  # wrappers remain active; BoltzGen's default subprocess would bypass them.
  "$BOLTZGEN" execute "$ATTEMPT_ROOT" --no_subprocess --steps "$stage" \
    >"$OPERATOR_LOGS/$stage.stdout.txt" 2>"$OPERATOR_LOGS/$stage.stderr.txt"
  local rc=$?
  set -e
  guard_hierarchy || return 75
  printf '%s\n' "$rc" > "$OPERATOR_LOGS/$stage.exit_code.txt"
  return "$rc"
}

PIPELINE_RC=0
if [ "$HIERARCHY_DRIFT" -eq 1 ]; then
  PIPELINE_RC=75
elif [ "$MONITOR_PROBE_RC" -ne 0 ]; then
  PIPELINE_RC=$MONITOR_PROBE_RC
elif run_configure; then
  if build_resolved_config_manifest; then
    :
  else
    PIPELINE_RC=$?
  fi
else
  PIPELINE_RC=$?
fi
if [ "$PIPELINE_RC" -eq 0 ]; then
  for STAGE in design inverse_folding folding analysis filtering; do
    if run_stage "$STAGE"; then
      :
    else
      PIPELINE_RC=$?
      break
    fi
  done
fi

stop_monitor
if [ "$HIERARCHY_DRIFT" -eq 1 ]; then
  PIPELINE_RC=75
fi
if [ "$PIPELINE_RC" -eq 0 ] && [ "$MONITOR_HEALTHY" -ne 1 ]; then
  PIPELINE_RC=74
fi

if [ "$PIPELINE_RC" -eq 0 ]; then
  if revalidate_runtime_contract; then
    guard_hierarchy || PIPELINE_RC=75
  else
    PIPELINE_RC=$?
    [ "$PIPELINE_RC" -ne 0 ] || PIPELINE_RC=65
  fi
  if [ "$PIPELINE_RC" -eq 0 ]; then
    set +e
    EXPECTED_DESIGNS="$EXPECTED_DESIGNS" \
      EXPECTED_FOLD_SAMPLES="$EXPECTED_FOLD_SAMPLES" \
      "$ENV_PYTHON" -I "$VALIDATOR" "$ATTEMPT_ROOT_LEXICAL" \
      >"$OPERATOR_LOGS/cell_contract.json" 2>"$OPERATOR_LOGS/validation.stderr.txt"
    VALIDATION_RC=$?
    set -e
    guard_hierarchy || VALIDATION_RC=75
    printf '%s\n' "$VALIDATION_RC" > "$OPERATOR_LOGS/validation.exit_code.txt"
    if [ "$VALIDATION_RC" -ne 0 ]; then
      PIPELINE_RC=$VALIDATION_RC
    else
      guard_hierarchy || PIPELINE_RC=75
    fi
    if [ "$PIPELINE_RC" -eq 0 ] && [ "$VALIDATION_RC" -eq 0 ]; then
      set +e
      python3 -I -S - "$OPERATOR_LOGS/cell_contract.json" "$EXPECTED_DESIGNS" "$EXPECTED_FOLD_SAMPLES" <<'PY'
import json
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if value.get("status") != "PASS":
    raise SystemExit(1)
if value.get("expected_designs") != int(sys.argv[2]) or value.get("observed_unique_ids") != int(sys.argv[2]):
    raise SystemExit(1)
if value.get("fold_samples_per_candidate") != int(sys.argv[3]):
    raise SystemExit(1)
PY
      VALIDATION_CONTRACT_RC=$?
      set -e
      guard_hierarchy || VALIDATION_CONTRACT_RC=75
      if [ "$VALIDATION_CONTRACT_RC" -ne 0 ]; then
        PIPELINE_RC=$VALIDATION_CONTRACT_RC
      fi
    fi
  fi
fi

if [ "$HIERARCHY_DRIFT" -eq 1 ]; then
  PIPELINE_RC=75
fi

if [ "$PIPELINE_RC" -eq 0 ]; then
  TERMINAL_STATUS=$SUCCESS_STATUS
else
  TERMINAL_STATUS=LOCAL_CELL_FAILED
fi

legacy_seal_failure_output_unused() {
  python3 -I -S - "$ATTEMPT_ROOT" "$CONTRACT" "$ENVIRONMENT_RECEIPT" \
    "$OPERATOR_LOGS/monitor.stopped.json" "$TERMINAL_STATUS" "$PIPELINE_RC" <<'PY'
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

root, contract_path, environment_path, monitor_path = map(Path, sys.argv[1:5])
terminal_status = sys.argv[5]
pipeline_rc = int(sys.argv[6])
if pipeline_rc == 0:
    raise ValueError("failure sealing requires a non-zero pipeline code")

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

def load(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or unsafe evidence: {path}")
    return json.loads(path.read_text(encoding="utf-8"))

contract = load(contract_path)
monitor = load(monitor_path)
if monitor.get("status") != "STOPPED" or monitor.get("wait_completed") is not True:
    raise ValueError("monitor is not STOPPED+wait_completed")
probe = "PROBE" in str(contract.get("run_kind", "")).upper()
marker_relative = "operator_logs/probe.FAILURE.json" if probe else "operator_logs/cell.FAILURE.json"
manifest_relative = "operator_logs/output_SHA256SUMS"
terminal_paths = {
    "operator_logs/cell.SUCCESS.json", "operator_logs/probe.SUCCESS.json",
    "operator_logs/cell.FAILURE.json", "operator_logs/probe.FAILURE.json",
}
for relative in terminal_paths:
    candidate = root / relative
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(f"terminal marker already exists: {relative}")
manifest = root / manifest_relative
if manifest.exists() or manifest.is_symlink():
    raise ValueError("output manifest already exists without a terminal marker")

def collect():
    records = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort(key=lambda item: item.encode("utf-8"))
        files.sort(key=lambda item: item.encode("utf-8"))
        base = Path(directory)
        for name in list(names):
            path = base / name
            if path.is_symlink() or not stat.S_ISDIR(path.stat().st_mode):
                raise ValueError(f"unsafe output directory: {path.relative_to(root)}")
        for name in files:
            path = base / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"unsafe output member: {relative}")
            if relative in {manifest_relative, marker_relative}:
                continue
            if relative in terminal_paths:
                raise ValueError(f"conflicting terminal marker: {relative}")
            if relative in {"outputs.SHA256SUMS", "receipt.json", "STATUS.txt"}:
                raise ValueError(f"forbidden legacy root output: {relative}")
            records.append((relative, digest(path)))
    records.sort(key=lambda item: item[0].encode("utf-8"))
    if not records:
        raise ValueError("failure output manifest may not be empty")
    return "".join(f"{value}  ./{relative}\n" for relative, value in records).encode()

manifest_bytes = collect()
descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(manifest_bytes)
    handle.flush()
    os.fsync(handle.fileno())
if collect() != manifest_bytes:
    raise ValueError("output changed while failure manifest was published")

payload = {
    "schema_version": "WSL2_BOLTZGEN_LOCAL_FAILURE_V1",
    "status": "FAILURE",
    "terminal_status": terminal_status,
    "pipeline_exit_code": pipeline_rc,
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": contract["cell_id"],
    "attempt_id": contract["attempt_id"],
    "run_kind": contract["run_kind"],
    "completed_at_utc": monitor["stopped_at_utc"],
    "execution_contract_sha256": digest(contract_path),
    "environment_receipt_sha256": digest(environment_path),
    "monitor_stopped_sha256": digest(monitor_path),
    "output_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "model_inputs_manifest_sha256": contract["model_inputs_manifest_sha256"],
    "runtime_scripts_manifest_sha256": contract["runtime_scripts_manifest_sha256"],
    "spec_gate_bundle_sha256": contract["spec_gate_bundle_sha256"],
}
validation = root / "operator_logs" / "cell_contract.json"
if validation.is_file() and not validation.is_symlink():
    payload["cell_contract_sha256"] = digest(validation)
marker = root / marker_relative
content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(content)
    handle.flush()
    os.fsync(handle.fileno())
PY
}

FINALIZE_RC=0
run_finalization_once || FINALIZE_RC=$?
if [ "$FINALIZE_RC" -ne 0 ]; then
  exit "$FINALIZE_RC"
fi
exit "$PIPELINE_RC"
