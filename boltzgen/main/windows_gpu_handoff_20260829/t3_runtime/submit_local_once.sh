#!/usr/bin/env bash
# Submit one immutable local cell to a deterministic non-restarting systemd unit.

set -euo pipefail
umask 077

die() {
  printf 'submit_local_once: %s\n' "${1:-submission failed}" >&2
  exit "${2:-70}"
}

[ "$#" -eq 2 ] || die "usage: submit_local_once.sh BG_WORK CELL_CONTRACT" 64
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
RUNNER="$BG_WORK/run_local_cell.sh"
[ -f "$RUNNER" ] && [ ! -L "$RUNNER" ] || die "missing local cell runner: $RUNNER" 66

validate_contract() {
  python3 -I -S - "$CONTRACT" "$BG_WORK" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
FORMAL_REVISION = re.compile(
    r"WSL2_CU128_BLACKWELL_FORMAL_ENVIRONMENT_CONTRACT_V[1-9][0-9]*"
)
MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  \./([^\n\r\0]+)")
ENGINEERING_MEMORY_PROBE_RUN_KIND = "ENGINEERING_MEMORY_PROBE"
ENGINEERING_MEMORY_PROBE_STATUS = "ENGINEERING_MEMORY_PROBE_ONLY"
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
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or resolved.is_symlink():
        raise ValueError(f"{path_key} must name a regular non-symlink file")
    observed = digest(resolved)
    if observed != expected:
        raise ValueError(f"{path_key}/{sha_key} mismatch: expected {expected}, observed {observed}")
    return resolved

def runtime_manifest(data):
    manifest = bound_file(
        data, "runtime_scripts_manifest_path", "runtime_scripts_manifest_sha256"
    )
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
        expected_sha, relative = match.groups()
        pure = Path(relative)
        if (
            relative.startswith("/")
            or "\\" in relative
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError("runtime scripts manifest path is unsafe")
        target = bg_work / pure
        if target.is_symlink() or not target.is_file() or target.resolve() != target:
            raise ValueError(f"runtime script is missing or unsafe: {relative}")
        if digest(target) != expected_sha:
            raise ValueError(f"runtime script SHA-256 mismatch: {relative}")
        records.append(relative)
    if tuple(records) != RUNTIME_MEMBERS:
        raise ValueError("runtime scripts manifest member set/order mismatch")
    return manifest


def validate_engineering_probe(data, paths):
    """Reject every approximate T6 claim while leaving FORMAL probes unchanged."""

    if data["stage_class"] != "ENGINEERING":
        return
    spec_path = paths["spec_path"]
    is_6xym_spec = tuple(spec_path.parts[-4:]) == ENGINEERING_6XYM_SPEC_SUFFIX
    probe_hint = (
        "PROBE" in data["run_kind"].upper()
        or "PROBE" in data["success_status"].upper()
        or data["cell_id"].startswith("6xym_")
        or is_6xym_spec
        or any(field in data for field in ENGINEERING_PROBE_FIELDS)
    )
    if not probe_hint:
        return

    match = ENGINEERING_MEMORY_PROBE_ID.fullmatch(data["cell_id"])
    if (
        data["run_kind"] != ENGINEERING_MEMORY_PROBE_RUN_KIND
        or data["success_status"] != ENGINEERING_MEMORY_PROBE_STATUS
        or match is None
        or data.get("probe_id") != data["cell_id"]
    ):
        raise ValueError("ENGINEERING probe must be the exact T6 memory-probe contract")
    checkpoint_name = data.get("checkpoint_name")
    if checkpoint_name != match.group(1):
        raise ValueError("engineering probe_id/checkpoint_name mismatch")
    expected_checkpoint_name = ENGINEERING_PROBE_CHECKPOINTS[checkpoint_name]
    if (
        paths["design_checkpoint"].name != expected_checkpoint_name
        or data.get("checkpoint_sha256") != data["design_checkpoint_sha256"]
    ):
        raise ValueError("engineering probe design checkpoint name/SHA mismatch")
    if not is_6xym_spec:
        raise ValueError("engineering probe must use the frozen 6XYM specification")
    for field, expected in {
        "expected_designs": 1,
        "budget": 1,
        "diffusion_batch_size": 1,
        "inverse_fold_num_sequences": 1,
        "expected_fold_samples": 5,
    }.items():
        if data.get(field) != expected:
            raise ValueError(f"engineering probe {field} must equal {expected}")

contract_path = Path(sys.argv[1])
bg_work = Path(sys.argv[2])
if contract_path.is_symlink() or not contract_path.is_file():
    raise ValueError("unsafe cell contract")
with contract_path.open(encoding="utf-8") as handle:
    data = json.load(handle, object_pairs_hook=no_duplicates)
if not isinstance(data, dict) or data.get("schema_version") != "WSL2_BOLTZGEN_LOCAL_CELL_V1":
    raise ValueError("unsupported cell-contract schema")
cell_id = text(data.get("cell_id"), "cell_id")
attempt_id = text(data.get("attempt_id"), "attempt_id")
if SAFE_ID.fullmatch(cell_id) is None or SAFE_ID.fullmatch(attempt_id) is None:
    raise ValueError("cell_id and attempt_id must be filesystem-safe identifiers")
for key in ("run_kind", "success_status", "stage_class"):
    text(data.get(key), key)
if data.get("stage_class") not in {"ENGINEERING", "FORMAL"}:
    raise ValueError("stage_class must be ENGINEERING or FORMAL")
for key in ("expected_designs", "expected_fold_samples", "budget", "diffusion_batch_size", "inverse_fold_num_sequences", "devices", "num_workers"):
    integer(data.get(key), key)
if data["expected_fold_samples"] != 5 or data["devices"] != 1:
    raise ValueError("local cell requires exactly five fold samples and one device")
if data.get("use_kernels") != "auto":
    raise ValueError("use_kernels must be auto")
if data.get("protocol") != "nanobody-anything":
    raise ValueError("protocol must be nanobody-anything")
if data.get("analysis_modality") != "antibody" or data.get("filtering_modality") != "antibody":
    raise ValueError("analysis/filtering modality must be antibody")
if data.get("filter_bindingsite") is not True:
    raise ValueError("filter_bindingsite must be true")

bindings = (
    ("spec_path", "spec_sha256"),
    ("design_checkpoint", "design_checkpoint_sha256"),
    ("inverse_fold_checkpoint", "inverse_fold_checkpoint_sha256"),
    ("folding_checkpoint", "folding_checkpoint_sha256"),
    ("mols_path", "mols_sha256"),
    ("model_inputs_manifest_path", "model_inputs_manifest_sha256"),
    ("runtime_scripts_manifest_path", "runtime_scripts_manifest_sha256"),
    ("spec_gate_bundle_path", "spec_gate_bundle_sha256"),
    ("environment_receipt", "environment_receipt_sha256"),
)
paths = {}
for path_key, sha_key in bindings:
    paths[path_key] = bound_file(data, path_key, sha_key)
runtime_manifest(data)
validate_engineering_probe(data, paths)
optional = ("environment_provenance_manifest_path", "environment_provenance_manifest_sha256")
environment_provenance = None
if any(key in data for key in optional):
    if not all(key in data for key in optional):
        raise ValueError("environment provenance path and SHA must be supplied together")
    environment_provenance = bound_file(data, *optional)
if data["stage_class"] == "FORMAL" and environment_provenance is None:
    raise ValueError("FORMAL cell contract requires an environment provenance manifest")

environment_contract_path = bg_work / "contract" / "environment_contract.json"
if (
    (bg_work / "contract").is_symlink()
    or not (bg_work / "contract").is_dir()
    or environment_contract_path.is_symlink()
    or not environment_contract_path.is_file()
):
    raise ValueError("environment contract is missing or unsafe")
with environment_contract_path.open(encoding="utf-8") as handle:
    environment_contract = json.load(handle, object_pairs_hook=no_duplicates)
if (
    not isinstance(environment_contract, dict)
    or environment_contract.get("schema_version") != "WSL2_GPU_STAGE_ENVIRONMENT_CONTRACT_V1"
):
    raise ValueError("unsupported environment contract schema")
if environment_contract.get("stage_class") != data["stage_class"]:
    raise ValueError("cell/environment stage_class mismatch")
executor_uid = environment_contract.get("executor_uid")
if type(executor_uid) is not int or executor_uid != os.getuid():
    raise ValueError("environment contract executor_uid mismatch")
attempt_root_raw = environment_contract.get("environment_attempt_root")
if not isinstance(attempt_root_raw, str) or not attempt_root_raw:
    raise ValueError("environment contract lacks attempt root")
attempt_root = Path(attempt_root_raw)
if not attempt_root.is_absolute() or attempt_root.is_symlink():
    raise ValueError("environment attempt root is unsafe")
attempt_root = attempt_root.resolve(strict=True)
if not attempt_root.is_dir():
    raise ValueError("environment attempt root is not a directory")
environment_path = Path(data["environment_receipt"])
contract_receipt = environment_contract.get("environment_receipt_path")
contract_receipt_sha = environment_contract.get("environment_receipt_sha256")
if (
    contract_receipt != str(environment_path)
    or contract_receipt_sha != data["environment_receipt_sha256"]
    or environment_path != attempt_root / "receipt.json"
):
    raise ValueError("cell/environment receipt binding mismatch")
environment_root = attempt_root / "env"
launcher = environment_root / "bin" / "boltzgen-wsl-sm120"
python_path = environment_root / "bin" / "python"
if environment_root.is_symlink() or not environment_root.is_dir():
    raise ValueError("environment root is unsafe")
if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
    raise ValueError("compatibility launcher is missing or unsafe")
if not python_path.exists() or not os.access(python_path, os.X_OK):
    raise ValueError("environment Python is missing")
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
    data["stage_class"] == "FORMAL"
    or data["run_kind"].upper() == "G2"
    or data["run_kind"].upper().startswith("G2_")
    or re.search(r"(?:^|_)G[12](?:_[A-Z0-9]+)*_PASS(?:_|$)", data["success_status"].upper()) is not None
)
if formal_claim and not formal_g1:
    raise ValueError("formal/G2 work requires a formal G1 receipt")
if formal_g1:
    if (
        environment.get("schema_version") != "WSL2_CU128_BLACKWELL_FORMAL_G1_RECEIPT_V1"
        or environment.get("status") != "G1_PASS"
        or data["stage_class"] != "FORMAL"
    ):
        raise ValueError("formal_g1=true requires G1_PASS")
    revision = environment.get("environment_contract_revision")
    if not isinstance(revision, str) or FORMAL_REVISION.fullmatch(revision) is None:
        raise ValueError("formal G1 receipt has an invalid environment contract revision")
    if environment.get("environment_contract_revision_required") is not False:
        raise ValueError("formal G1 receipt still requires an environment contract revision")
    expected_provenance = attempt_root / "recursive_payload.SHA256SUMS"
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
    or data["stage_class"] != "ENGINEERING"
    or environment.get("status") != "ENGINEERING_COMPATIBILITY_ONLY"
    or environment.get("environment_contract_revision_required") is not True
):
    raise ValueError("engineering environment receipt has invalid status/revision semantics")
if environment.get("compatibility_activation") != "EXPLICIT_PROCESS_LOCAL_ONLY":
    raise ValueError("environment receipt does not require explicit process-local compatibility")

print(cell_id)
print(attempt_id)
print(digest(contract_path))
print(executor_uid)
PY
}

if ! CONTRACT_VALUES=$(validate_contract); then
  die "immutable cell-contract validation failed" 65
fi
mapfile -t CONTRACT_FIELDS <<< "$CONTRACT_VALUES"
[ "${#CONTRACT_FIELDS[@]}" -eq 4 ] || die "could not read validated cell contract" 65
CELL_ID=${CONTRACT_FIELDS[0]}
ATTEMPT_ID=${CONTRACT_FIELDS[1]}
CONTRACT_SHA=${CONTRACT_FIELDS[2]}
EXECUTOR_UID=${CONTRACT_FIELDS[3]}
UNIT="boltzgen-local-${CONTRACT_SHA}.service"

# /run/user is a root-owned, non-writable directory inode.  Locking that inode
# gives submissions one non-replaceable serialization domain without a
# symlink-following lock-file open.  GPU execution uses /run/user/$UID, a
# distinct fixed inode, so launching a unit cannot deadlock its runner.
if [ -z "${BG_SUBMISSION_LOCK_FD:-}" ]; then
  exec python3 -I -S - "$0" "$BG_WORK_INPUT" "$CONTRACT_INPUT" <<'PY'
import fcntl
import os
import stat
import sys

script, bg_work, contract = sys.argv[1:]
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open("/run/user", flags)
info = os.fstat(descriptor)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise SystemExit(66)
fcntl.flock(descriptor, fcntl.LOCK_EX)
os.set_inheritable(descriptor, True)
environment = os.environ.copy()
environment["BG_SUBMISSION_LOCK_FD"] = str(descriptor)
os.execve("/bin/bash", ["bash", script, bg_work, contract], environment)
PY
fi
python3 -I -S - "${BG_SUBMISSION_LOCK_FD}" <<'PY' || \
  die "fixed submission lock identity is unsafe" 66
import fcntl
import os
import stat
import sys

if not sys.argv[1].isdecimal():
    raise SystemExit(1)
descriptor = int(sys.argv[1])
opened = os.fstat(descriptor)
current = os.stat("/run/user", follow_symlinks=False)
if (
    not stat.S_ISDIR(opened.st_mode)
    or opened.st_uid != 0
    or opened.st_mode & 0o022
    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
):
    raise SystemExit(1)
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY

SUBMISSION_ROOT="$BG_WORK/local_submissions"
python3 -I -S - "$BG_WORK" <<'PY' || die "submission directory ownership/mode is unsafe" 66
import os
import stat
import sys
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
root = os.open(sys.argv[1], flags)
try:
    try:
        os.mkdir("local_submissions", 0o700, dir_fd=root)
        os.fsync(root)
    except FileExistsError:
        pass
    info = os.stat("local_submissions", dir_fd=root, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise SystemExit(1)
finally:
    os.close(root)
PY

# Revalidate after serialization so the intent never binds a moving contract.
if ! SECOND_VALUES=$(validate_contract); then
  die "cell contract changed before intent publication" 65
fi
[ "$SECOND_VALUES" = "$CONTRACT_VALUES" ] || die "cell contract changed before intent publication" 65

BASE="$SUBMISSION_ROOT/$CELL_ID.$ATTEMPT_ID"
INTENT="$BASE.intent.json"
RECEIPT="$BASE.receipt.json"
SERVICE_UID=$(id -u)
[ "$SERVICE_UID" = "$EXECUTOR_UID" ] || die "current UID differs from environment executor_uid" 65
SERVICE_USER=$(id -un)
SERVICE_HOME=$(getent passwd "$SERVICE_UID" | cut -d: -f6)
[ -n "$SERVICE_HOME" ] && [ -d "$SERVICE_HOME" ] || die "could not resolve executor home" 66
SERVICE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/wsl/lib
TRAMPOLINE_CODE='import os,re,sys;token,runner,bg_work,contract,path,home,user,uid=sys.argv[1:];invocation=os.environ.get("INVOCATION_ID","");re.fullmatch(r"[0-9a-f]{32}",invocation) or sys.exit(75);environment={"PATH":path,"HOME":home,"USER":user,"LOGNAME":user,"XDG_RUNTIME_DIR":f"/run/user/{uid}","BG_SUBMISSION_TOKEN":token,"INVOCATION_ID":invocation};os.execve(runner,[runner,bg_work,contract],environment)'

compute_exec_binding() {
  local values
  values=$(python3 -I -S - \
    "$TRAMPOLINE_CODE" "$SERVICE_PATH" "$SERVICE_HOME" "$SERVICE_USER" "$SERVICE_UID" \
    "$SUBMISSION_TOKEN" "$RUNNER" "$BG_WORK" "$CONTRACT" <<'PY'
import hashlib
import json
import sys
code, path, home, user, uid, token, runner, bg_work, contract = sys.argv[1:]
argv = [
    "/usr/bin/python3", "-I", "-S", "-c", code, token, runner, bg_work,
    contract, path, home, user, uid,
]
canonical = json.dumps(argv, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
print(canonical)
print(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
PY
  ) || return 1
  mapfile -t EXEC_VALUES <<< "$values"
  [ "${#EXEC_VALUES[@]}" -eq 2 ] || return 1
  EXEC_START_JSON=${EXEC_VALUES[0]}
  EXEC_START_SHA=${EXEC_VALUES[1]}
}
SUBMISSION_TOKEN=$(python3 -I -S -c 'import secrets; print(secrets.token_hex(16))')
compute_exec_binding || die "could not build the exact service ExecStart binding" 70

validate_intent() {
  python3 -I -S - "$INTENT" "$CELL_ID" "$ATTEMPT_ID" "$CONTRACT" "$CONTRACT_SHA" "$UNIT" "$RUNNER" "$EXECUTOR_UID" "$EXEC_START_SHA" <<'PY'
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
if path.is_symlink() or not path.is_file():
    raise ValueError("unsafe submission intent")
with path.open(encoding="utf-8") as handle:
    value = json.load(handle, object_pairs_hook=no_duplicates)
expected = {
    "schema_version": "WSL2_LOCAL_SUBMISSION_INTENT_V1",
    "status": "SUBMISSION_INTENT",
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": sys.argv[2],
    "attempt_id": sys.argv[3],
    "cell_contract_path": sys.argv[4],
    "cell_contract_sha256": sys.argv[5],
    "unit": sys.argv[6],
    "runner_path": sys.argv[7],
    "executor_uid": int(sys.argv[8]),
    "exec_start_sha256": sys.argv[9],
}
for key, expected_value in expected.items():
    if value.get(key) != expected_value:
        raise ValueError(f"submission intent mismatch: {key}")
token = value.get("submission_token")
if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
    raise ValueError("submission intent token is invalid")
if set(value) != set(expected) | {"submission_token", "intent_at_utc"}:
    raise ValueError("submission intent key set is not fixed")
PY
}

read_intent_token() {
  python3 -I -S - "$INTENT" <<'PY'
import json
import re
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
token = value.get("submission_token")
if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
    raise SystemExit(1)
print(token)
PY
}

create_intent() {
  python3 -I -S - "$INTENT" "$CELL_ID" "$ATTEMPT_ID" "$CONTRACT" "$CONTRACT_SHA" "$UNIT" "$RUNNER" "$SUBMISSION_TOKEN" "$EXECUTOR_UID" "$EXEC_START_SHA" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": "WSL2_LOCAL_SUBMISSION_INTENT_V1",
    "status": "SUBMISSION_INTENT",
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
    "intent_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
temporary = Path(name)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        os.fsync(handle.fileno())
    os.link(temporary, path, follow_symlinks=False)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    temporary.unlink(missing_ok=True)
PY
}

validate_receipt() {
  python3 -I -S - "$RECEIPT" "$CELL_ID" "$ATTEMPT_ID" "$CONTRACT" "$CONTRACT_SHA" "$UNIT" "$RUNNER" "$SUBMISSION_TOKEN" "$EXECUTOR_UID" "$EXEC_START_SHA" <<'PY'
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
if path.is_symlink() or not path.is_file():
    raise ValueError("unsafe submission receipt")
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
for key, expected_value in expected.items():
    if value.get(key) != expected_value:
        raise ValueError(f"submission receipt mismatch: {key}")
invocation = value.get("invocation_id")
if not isinstance(invocation, str) or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
    raise ValueError("submission receipt InvocationID is invalid")
if set(value) != set(expected) | {
    "active_state_at_receipt", "sub_state_at_receipt", "unit_result_at_receipt",
    "invocation_id", "submitted_at_utc",
}:
    raise ValueError("submission receipt key set is not fixed")
PY
}

QUERY_ACTIVE_STATE=
QUERY_SUB_STATE=
QUERY_RESULT=
QUERY_INVOCATION_ID=
QUERY_EXEC_START_SHA=
query_exact_unit() {
  local raw object_raw object_path exec_raw systemctl_rc object_rc exec_rc parsed parse_rc
  set +e
  raw=$(systemctl --user show "$UNIT" \
    --property=Id --property=LoadState --property=ActiveState \
    --property=SubState --property=Result --property=Description \
    --property=Restart --property=Type --property=InvocationID \
    --property=KillMode --property=UMask --no-pager 2>/dev/null)
  systemctl_rc=$?
  object_raw=$(busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
    org.freedesktop.systemd1.Manager GetUnit s "$UNIT" 2>/dev/null)
  object_rc=$?
  object_path=$(python3 -I -S -c 'import json,sys; raw=sys.argv[1]; raw.startswith("o ") or sys.exit(2); value=json.loads(raw[2:]); isinstance(value,str) and value.startswith("/org/freedesktop/systemd1/unit/") or sys.exit(2); print(value)' "$object_raw" 2>/dev/null)
  exec_raw=$(busctl --json=short --user get-property org.freedesktop.systemd1 "$object_path" \
    org.freedesktop.systemd1.Service ExecStart 2>/dev/null)
  exec_rc=$?
  set -e
  [ "$systemctl_rc" -eq 0 ] && [ "$object_rc" -eq 0 ] && [ -n "$object_path" ] \
    && [ "$exec_rc" -eq 0 ] || return 10
  set +e
  parsed=$(printf '%s\n' "$raw" | python3 -I -S -c '
import hashlib
import json
import sys
expected_unit = sys.argv[1]
expected_description = sys.argv[2]
expected_argv = json.loads(sys.argv[3])
exec_payload = json.loads(sys.argv[4])
expected_keys = {
    "Id", "LoadState", "ActiveState", "SubState", "Result", "Description",
    "Restart", "Type", "InvocationID", "KillMode", "UMask",
}
values = {}
for raw_line in sys.stdin:
    line = raw_line.rstrip("\n")
    if not line or "=" not in line:
        raise SystemExit(2)
    key, value = line.split("=", 1)
    if key not in expected_keys or key in values or not value:
        raise SystemExit(2)
    values[key] = value
if set(values) != expected_keys or values["Id"] != expected_unit or values["LoadState"] != "loaded":
    raise SystemExit(2)
if (values["Description"] != expected_description or values["Restart"] != "no"
        or values["Type"] != "exec" or values["KillMode"] != "control-group"
        or values["UMask"] != "0077"):
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
print(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
' "$UNIT" "boltzgen-local:$CONTRACT_SHA:$SUBMISSION_TOKEN" "$EXEC_START_JSON" "$exec_raw")
  parse_rc=$?
  set -e
  [ "$parse_rc" -eq 0 ] || return 11
  mapfile -t QUERY_VALUES <<< "$parsed"
  [ "${#QUERY_VALUES[@]}" -eq 5 ] || return 11
  QUERY_ACTIVE_STATE=${QUERY_VALUES[0]}
  QUERY_SUB_STATE=${QUERY_VALUES[1]}
  QUERY_RESULT=${QUERY_VALUES[2]}
  QUERY_INVOCATION_ID=${QUERY_VALUES[3]}
  QUERY_EXEC_START_SHA=${QUERY_VALUES[4]}
  [ "$QUERY_EXEC_START_SHA" = "$EXEC_START_SHA" ] || return 11
  return 0
}

create_receipt() {
  python3 -I -S - "$RECEIPT" "$CELL_ID" "$ATTEMPT_ID" "$CONTRACT" "$CONTRACT_SHA" "$UNIT" "$QUERY_ACTIVE_STATE" "$QUERY_SUB_STATE" "$QUERY_RESULT" "$QUERY_INVOCATION_ID" "$RUNNER" "$SUBMISSION_TOKEN" "$EXECUTOR_UID" "$QUERY_EXEC_START_SHA" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": "WSL2_LOCAL_SUBMISSION_RECEIPT_V1",
    "status": "SUBMITTED",
    "executor_kind": "WSL2_SYSTEMD_SINGLE_GPU",
    "cell_id": sys.argv[2],
    "attempt_id": sys.argv[3],
    "cell_contract_path": sys.argv[4],
    "cell_contract_sha256": sys.argv[5],
    "unit": sys.argv[6],
    "active_state_at_receipt": sys.argv[7],
    "sub_state_at_receipt": sys.argv[8],
    "unit_result_at_receipt": sys.argv[9],
    "invocation_id": sys.argv[10],
    "runner_path": sys.argv[11],
    "submission_token": sys.argv[12],
    "executor_uid": int(sys.argv[13]),
    "exec_start_sha256": sys.argv[14],
    "submitted_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
temporary = Path(name)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        os.fsync(handle.fileno())
    os.link(temporary, path, follow_symlinks=False)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    temporary.unlink(missing_ok=True)
PY
}

if [ -e "$INTENT" ] || [ -L "$INTENT" ]; then
  SUBMISSION_TOKEN=$(read_intent_token) || die "existing submission intent token is invalid" 73
  compute_exec_binding || die "could not rebuild existing service ExecStart binding" 70
  validate_intent || die "existing submission intent does not match the immutable request" 73
  if [ -e "$RECEIPT" ] || [ -L "$RECEIPT" ]; then
    validate_receipt || die "existing submission receipt is unsafe or inconsistent" 73
    printf '%s\n' "$RECEIPT"
    exit 0
  fi
  set +e
  query_exact_unit
  query_rc=$?
  set -e
  if [ "$query_rc" -ne 0 ]; then
    if [ "$query_rc" -eq 10 ]; then
      die "submission intent exists but its unit disappeared; refusing relaunch" 75
    fi
    die "submission intent exists but unit identity is ambiguous; refusing relaunch" 75
  fi
  create_receipt || die "could not reconcile a unique unit to its submission receipt" 73
  validate_receipt || die "reconciled receipt failed validation" 73
  printf '%s\n' "$RECEIPT"
  exit 0
fi

[ ! -e "$RECEIPT" ] && [ ! -L "$RECEIPT" ] || die "receipt exists without a matching intent" 73
create_intent || die "could not publish submission intent" 73
validate_intent || die "new submission intent failed validation" 73

set +e
systemd-run --user --no-block \
  --property=Restart=no --property=Type=exec --property=KillMode=control-group \
  --property=UMask=0077 \
  --description="boltzgen-local:$CONTRACT_SHA:$SUBMISSION_TOKEN" \
  --unit="$UNIT" -- \
  /usr/bin/python3 -I -S -c "$TRAMPOLINE_CODE" \
    "$SUBMISSION_TOKEN" "$RUNNER" "$BG_WORK" "$CONTRACT" \
    "$SERVICE_PATH" "$SERVICE_HOME" "$SERVICE_USER" "$SERVICE_UID"
SYSTEMD_RUN_RC=$?
set -e

# A transport error is not proof that the unit was absent. Query the exact unit
# before deciding whether a receipt can be published.
QUERY_RC=10
for _ in 1 2 3 4 5 6 7 8 9 10; do
  set +e
  query_exact_unit
  query_attempt_rc=$?
  set -e
  if [ "$query_attempt_rc" -eq 0 ]; then
    QUERY_RC=0
    break
  fi
  QUERY_RC=$query_attempt_rc
  [ "$QUERY_RC" -eq 10 ] || break
  sleep 0.1
done
if [ "$QUERY_RC" -ne 0 ]; then
  if [ "$QUERY_RC" -eq 10 ]; then
    die "intent published, but exact unit is unavailable (systemd-run rc=$SYSTEMD_RUN_RC); refusing relaunch" 75
  fi
  die "intent published, but unit identity is ambiguous (systemd-run rc=$SYSTEMD_RUN_RC)" 75
fi
create_receipt || die "unit exists but receipt publication failed" 73
validate_receipt || die "new submission receipt failed validation" 73
printf '%s\n' "$RECEIPT"
