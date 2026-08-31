#!/usr/bin/env -S -i PATH=/usr/bin:/bin BOLTZGEN_CLEAN_LAUNCH=1 /bin/bash
# Execute the twelve non-inference BoltzGen specification checks with replayable evidence.
set -euo pipefail
umask 077

die() {
  printf 'BLOCKED_SPEC_GATE: %s\n' "${1:-check runner failed}" >&2
  exit "${2:-70}"
}

[ "${BOLTZGEN_CLEAN_LAUNCH:-}" = 1 ] || die "execute this runner directly so its clean-environment shebang is enforced" 64
unset BOLTZGEN_CLEAN_LAUNCH
for name in PYTHONPATH PYTHONHOME PYTHONOPTIMIZE BASH_ENV ENV LD_PRELOAD LD_LIBRARY_PATH; do
  if [ "${!name+x}" = x ]; then
    die "$name must be unset before the evidence runner starts" 64
  fi
done
PATH=/usr/bin:/bin
export PATH PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 LC_ALL=C
unset CDPATH GLOBIGNORE
IFS=$' \t\n'

[ "$#" -eq 8 ] || die "usage: run_check_specs.sh CAMPAIGN_ROOT CHECKER EXPECTED_CHECKER_SHA256 MOLDIR EXPECTED_MOLDIR_SHA256 ENVIRONMENT_RECEIPT EXPECTED_ENVIRONMENT_SHA256 EXPECTED_RUNNER_SHA256" 64
CAMPAIGN_ROOT=$1
CHECKER=$2
EXPECTED_CHECKER_SHA=$3
MOLDIR=$4
EXPECTED_MOLDIR_SHA=$5
ENVIRONMENT_RECEIPT=$6
EXPECTED_ENVIRONMENT_SHA=$7
EXPECTED_RUNNER_SHA=$8
RUNNER=$(/usr/bin/readlink -f -- "$0")

for value in "$EXPECTED_CHECKER_SHA" "$EXPECTED_MOLDIR_SHA" "$EXPECTED_ENVIRONMENT_SHA" "$EXPECTED_RUNNER_SHA"; do
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || die "expected hashes must be lowercase SHA-256 values" 64
done

guard_directory() {
  local path=$1 label=$2 resolved owner
  [ -d "$path" ] && [ ! -L "$path" ] || die "$label is not a safe directory: $path" 66
  resolved=$(/usr/bin/readlink -f -- "$path")
  [ "$resolved" = "$path" ] || die "$label is not canonical or contains a symlink hop: $path" 66
  owner=$(/usr/bin/stat -Lc '%u' -- "$path")
  [ "$owner" = "$EUID" ] || die "$label is not owned by the invoking user: $path" 66
}

guard_file() {
  local path=$1 label=$2 resolved owner
  [ -f "$path" ] && [ ! -L "$path" ] || die "$label is not a safe regular file: $path" 66
  resolved=$(/usr/bin/readlink -f -- "$path")
  [ "$resolved" = "$path" ] || die "$label is not canonical or contains a symlink hop: $path" 66
  owner=$(/usr/bin/stat -Lc '%u' -- "$path")
  [ "$owner" = "$EUID" ] || die "$label is not owned by the invoking user: $path" 66
}

directory_signature() {
  guard_directory "$1" "$2"
  /usr/bin/stat -Lc '%d:%i:%u:%f' -- "$1"
}

file_signature() {
  guard_file "$1" "$2"
  /usr/bin/stat -Lc '%d:%i:%u:%f:%s:%Y:%Z' -- "$1"
}

sha_file() {
  local digest remainder
  IFS=' ' read -r digest remainder < <(/usr/bin/sha256sum -- "$1")
  printf '%s\n' "$digest"
}

declare -a DIRECTORY_PATHS=() DIRECTORY_LABELS=() DIRECTORY_SIGNATURES=()
declare -a FILE_PATHS=() FILE_LABELS=() FILE_SIGNATURES=() FILE_HASHES=()

register_directory() {
  local path=$1 label=$2 signature
  signature=$(directory_signature "$path" "$label")
  DIRECTORY_PATHS+=("$path")
  DIRECTORY_LABELS+=("$label")
  DIRECTORY_SIGNATURES+=("$signature")
}

register_file() {
  local path=$1 expected=$2 label=$3 signature observed
  signature=$(file_signature "$path" "$label")
  observed=$(sha_file "$path")
  if [ -n "$expected" ] && [ "$observed" != "$expected" ]; then
    die "$label hash differs from the trusted value" 65
  fi
  FILE_PATHS+=("$path")
  FILE_LABELS+=("$label")
  FILE_SIGNATURES+=("$signature")
  FILE_HASHES+=("$observed")
}

verify_guards() {
  local index current
  for index in "${!DIRECTORY_PATHS[@]}"; do
    current=$(directory_signature "${DIRECTORY_PATHS[$index]}" "${DIRECTORY_LABELS[$index]}")
    [ "$current" = "${DIRECTORY_SIGNATURES[$index]}" ] || die "directory identity drift: ${DIRECTORY_LABELS[$index]}" 65
  done
  for index in "${!FILE_PATHS[@]}"; do
    current=$(file_signature "${FILE_PATHS[$index]}" "${FILE_LABELS[$index]}")
    [ "$current" = "${FILE_SIGNATURES[$index]}" ] || die "file identity drift: ${FILE_LABELS[$index]}" 65
  done
}

verify_all_hashes() {
  local index current
  for index in "${!FILE_PATHS[@]}"; do
    current=$(sha_file "${FILE_PATHS[$index]}")
    [ "$current" = "${FILE_HASHES[$index]}" ] || die "file content drift: ${FILE_LABELS[$index]}" 65
  done
}

for pair in \
  "$CAMPAIGN_ROOT|campaign root" \
  "$CAMPAIGN_ROOT/project_input|project input" \
  "$CAMPAIGN_ROOT/project_input/specs|spec root" \
  "$CAMPAIGN_ROOT/provenance|provenance root" \
  "$CAMPAIGN_ROOT/env|environment executable root" \
  "$CAMPAIGN_ROOT/env/bin|environment executable directory" \
  "$CAMPAIGN_ROOT/runtime_cache|runtime cache" \
  "$CAMPAIGN_ROOT/environment|environment evidence root" \
  "$CAMPAIGN_ROOT/software|software evidence root"; do
  register_directory "${pair%%|*}" "${pair#*|}"
done

[ "$CHECKER" = "$CAMPAIGN_ROOT/env/bin/boltzgen" ] || die "checker path is outside the canonical campaign env layout" 66
[ "$MOLDIR" = "$CAMPAIGN_ROOT/runtime_cache/mols.zip" ] || die "mols.zip path is outside the canonical campaign runtime layout" 66
[ "$RUNNER" = "$CAMPAIGN_ROOT/software/run_check_specs.sh" ] || die "runner path is outside the canonical campaign software layout" 66
case "$ENVIRONMENT_RECEIPT" in
  "$CAMPAIGN_ROOT/environment/"*) ;;
  *) die "environment receipt is outside the canonical campaign environment layout" 66 ;;
esac
[ -x "$CHECKER" ] || die "checker is not executable: $CHECKER" 66

register_file "$CHECKER" "$EXPECTED_CHECKER_SHA" "checker executable"
register_file "$MOLDIR" "$EXPECTED_MOLDIR_SHA" "mols.zip"
register_file "$ENVIRONMENT_RECEIPT" "$EXPECTED_ENVIRONMENT_SHA" "environment receipt"
register_file "$RUNNER" "$EXPECTED_RUNNER_SHA" "check runner"

MANIFEST="$CAMPAIGN_ROOT/project_input/spec_manifest.tsv"
register_file "$MANIFEST" "" "spec manifest"
MANIFEST_SHA=${FILE_HASHES[4]}

MANIFEST_ROWS=$(/usr/bin/python3 -I -S - "$MANIFEST" <<'PY'
import csv
import re
import sys
from pathlib import Path

fields = [
    "spec_id", "scaffold_id", "scaffold_role", "target_id", "target_chain",
    "binding_label_seq_ids", "cdr1_range", "cdr2_range", "cdr3_range",
    "cdr1_length", "cdr2_length", "cdr3_length", "spec_path",
    "spec_sha256", "scaffold_sha256", "target_sha256",
]
safe = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
sha = re.compile(r"[0-9a-f]{64}")
range_pattern = re.compile(r"([1-9][0-9]*)\.\.([1-9][0-9]*)")
try:
    with Path(sys.argv[1]).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != fields:
            raise ValueError("manifest header drift")
        rows = list(reader)
except (OSError, UnicodeError, csv.Error, ValueError) as error:
    raise SystemExit(f"manifest cannot be parsed: {error}")
if len(rows) != 12:
    raise SystemExit("manifest must contain exactly 12 rows")
if any(set(row) != set(fields) or None in row or any(value == "" for value in row.values()) for row in rows):
    raise SystemExit("manifest contains an empty or malformed row")
ids = [row["spec_id"] for row in rows]
scaffolds = [row["scaffold_id"] for row in rows]
if len(set(ids)) != 12 or len(set(scaffolds)) != 12:
    raise SystemExit("manifest IDs must be unique")
roles = [row["scaffold_role"] for row in rows]
if roles.count("PRIMARY") != 10 or roles.count("RESERVE") != 2 or set(roles) != {"PRIMARY", "RESERVE"}:
    raise SystemExit("manifest roles must be PRIMARY=10 and RESERVE=2")
target_hashes = {row["target_sha256"] for row in rows}
if len(target_hashes) != 1:
    raise SystemExit("manifest target hash must be identical in all rows")
for row in rows:
    spec_id = row["spec_id"]
    if safe.fullmatch(spec_id) is None or safe.fullmatch(row["scaffold_id"]) is None:
        raise SystemExit(f"unsafe manifest ID: {spec_id}")
    if row["target_id"] != "GLP1_7-36_NH2" or row["target_chain"] != "E" or row["binding_label_seq_ids"] != "1,2":
        raise SystemExit(f"target contract drift: {spec_id}")
    if row["spec_path"] != f"specs/{spec_id}/design.yaml":
        raise SystemExit(f"spec path drift: {spec_id}")
    for field in ("spec_sha256", "scaffold_sha256", "target_sha256"):
        if sha.fullmatch(row[field]) is None:
            raise SystemExit(f"invalid {field}: {spec_id}")
    previous = 0
    for index in range(1, 4):
        match = range_pattern.fullmatch(row[f"cdr{index}_range"])
        if match is None:
            raise SystemExit(f"invalid CDR range: {spec_id}")
        start, end = map(int, match.groups())
        if start <= previous or end < start or str(end - start + 1) != row[f"cdr{index}_length"]:
            raise SystemExit(f"invalid CDR contract: {spec_id}")
        previous = end
    print("\t".join((spec_id, row["spec_path"], row["spec_sha256"], row["scaffold_sha256"], row["target_sha256"])))
PY
) || die "spec manifest validation failed" 65

declare -a SPEC_IDS=() SPEC_PATHS=()
declare -A SEEN_SPEC_IDS=()
while IFS=$'\t' read -r spec_id spec_path spec_sha scaffold_sha target_sha; do
  [ -n "$spec_id" ] || continue
  [ -z "${SEEN_SPEC_IDS[$spec_id]+x}" ] || die "duplicate spec ID after manifest validation: $spec_id" 65
  SEEN_SPEC_IDS[$spec_id]=1
  SPEC_DIR="$CAMPAIGN_ROOT/project_input/specs/$spec_id"
  register_directory "$SPEC_DIR" "spec directory $spec_id"
  SPEC="$CAMPAIGN_ROOT/project_input/$spec_path"
  MEMBERS=$(/usr/bin/python3 -I -S - "$SPEC_DIR" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = {"design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"}
with os.scandir(root) as entries:
    items = list(entries)
observed = {entry.name for entry in items}
if observed != expected:
    raise SystemExit(f"spec artifact set differs: {sorted(observed)}")
for entry in items:
    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
        raise SystemExit(f"unsafe spec artifact: {entry.name}")
print("OK")
PY
  ) || die "spec artifact preflight failed: $spec_id" 66
  [ "$MEMBERS" = OK ] || die "spec artifact preflight returned unexpected output: $spec_id" 66
  register_file "$SPEC" "$spec_sha" "design spec $spec_id"
  register_file "$SPEC_DIR/scaffold.cif" "$scaffold_sha" "scaffold CIF $spec_id"
  register_file "$SPEC_DIR/target.cif" "$target_sha" "target CIF $spec_id"
  register_file "$SPEC_DIR/scaffold.yaml" "" "scaffold YAML $spec_id"
  SPEC_IDS+=("$spec_id")
  SPEC_PATHS+=("$SPEC")
done <<< "$MANIFEST_ROWS"
[ "${#SPEC_IDS[@]}" -eq 12 ] || die "preflight did not yield exactly 12 specs" 65

CHECK_ROOT="$CAMPAIGN_ROOT/project_input/check_outputs"
LOG_ROOT="$CAMPAIGN_ROOT/provenance/check_logs"
OUTPUT_MANIFEST="$CAMPAIGN_ROOT/provenance/check_outputs_SHA256SUMS"
LOG_MANIFEST="$CAMPAIGN_ROOT/provenance/check_logs_SHA256SUMS"
for path in "$CHECK_ROOT" "$LOG_ROOT" "$OUTPUT_MANIFEST" "$LOG_MANIFEST"; do
  [ ! -e "$path" ] && [ ! -L "$path" ] || die "immutable evidence destination already exists: $path" 73
done

verify_guards
CHECKER_VERSION=$("$CHECKER" --version) || die "checker version command failed" 65
[ "$CHECKER_VERSION" = "boltzgen 0.3.2" ] || die "unexpected checker version: $CHECKER_VERSION" 65
verify_guards
verify_all_hashes
[ "$(sha_file "$MANIFEST")" = "$MANIFEST_SHA" ] || die "spec manifest changed before execution" 65

/usr/bin/mkdir -- "$CHECK_ROOT" "$LOG_ROOT"
register_directory "$CHECK_ROOT" "check output root"
register_directory "$LOG_ROOT" "check log root"
verify_guards
set -o noclobber

publish_exit_code() {
  /usr/bin/python3 -I -S - "$1" "$2" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
content = (sys.argv[2] + "\n").encode("ascii")
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
with os.fdopen(fd, "wb") as handle:
    handle.write(content)
    handle.flush()
    os.fsync(handle.fileno())
PY
}

for index in "${!SPEC_IDS[@]}"; do
  spec_id=${SPEC_IDS[$index]}
  SPEC=${SPEC_PATHS[$index]}
  OUTPUT="$CHECK_ROOT/$spec_id"
  LOG="$LOG_ROOT/$spec_id"
  verify_guards
  [ ! -e "$OUTPUT" ] && [ ! -L "$OUTPUT" ] || die "check output already exists: $spec_id" 73
  [ ! -e "$LOG" ] && [ ! -L "$LOG" ] || die "check log already exists: $spec_id" 73
  /usr/bin/mkdir -- "$LOG"
  register_directory "$LOG" "check log directory $spec_id"
  verify_guards

  set +e
  "$CHECKER" check "$SPEC" --output "$OUTPUT" --moldir "$MOLDIR" \
    > "$LOG/check.stdout.log" 2> "$LOG/check.stderr.log"
  CHECK_EXIT=$?
  set -e
  publish_exit_code "$LOG/check.exit_code.txt" "$CHECK_EXIT"
  verify_guards
  if [ "$CHECK_EXIT" -ne 0 ]; then
    die "boltzgen check failed for $spec_id with exit $CHECK_EXIT" "$CHECK_EXIT"
  fi

  register_directory "$OUTPUT" "check output directory $spec_id"
  CHECK_CIF=$(/usr/bin/python3 -I -S - "$OUTPUT" <<'PY'
import os
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
safe_component = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
files = []
stack = [root]
while stack:
    directory = stack.pop()
    with os.scandir(directory) as entries:
        for entry in entries:
            if safe_component.fullmatch(entry.name) is None:
                raise SystemExit(f"unsafe check output name: {entry.path}")
            if entry.is_symlink():
                raise SystemExit(f"symlink forbidden in check output: {entry.path}")
            if entry.is_file(follow_symlinks=False):
                files.append(Path(entry.path))
            elif entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            else:
                raise SystemExit(f"special file forbidden in check output: {entry.path}")
cifs = [path for path in files if path.suffix.lower() in {".cif", ".mmcif"}]
if len(files) != 1 or len(cifs) != 1:
    raise SystemExit("check output must contain exactly one CIF/mmCIF")
print(cifs[0])
PY
  ) || die "unsafe check output for $spec_id" 65
  guard_file "$CHECK_CIF" "check CIF $spec_id"

  /usr/bin/python3 -I -S - \
    "$LOG/check.execution.json" "$spec_id" "${FILE_HASHES[$((5 + index * 4))]}" \
    "$CHECKER" "$EXPECTED_CHECKER_SHA" "$CHECKER_VERSION" \
    "$EXPECTED_MOLDIR_SHA" "$EXPECTED_RUNNER_SHA" "$EXPECTED_ENVIRONMENT_SHA" \
    "$LOG/check.stdout.log" "$LOG/check.stderr.log" "$CHECK_CIF" \
    "$SPEC" "$OUTPUT" "$MOLDIR" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

(
    output_json, spec_id, spec_sha, checker, checker_sha, checker_version,
    moldir_sha, runner_sha, environment_sha, stdout_raw, stderr_raw,
    check_cif_raw, spec_raw, output_raw, moldir_raw,
) = sys.argv[1:]

def digest(raw: str) -> str:
    path = Path(raw)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"evidence member is not regular: {path}")
        value = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            value.update(block)
        after = os.fstat(fd)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(before) != signature(after):
            raise SystemExit(f"evidence member changed while hashing: {path}")
        return value.hexdigest()
    finally:
        os.close(fd)

payload = {
    "schema_version": "BOLTZGEN_CHECK_EXECUTION_V1",
    "spec_id": spec_id,
    "spec_sha256": spec_sha,
    "checker_executable_path": checker,
    "checker_executable_sha256": checker_sha,
    "checker_version": checker_version,
    "moldir_sha256": moldir_sha,
    "runner_sha256": runner_sha,
    "environment_receipt_sha256": environment_sha,
    "argv": [checker, "check", spec_raw, "--output", output_raw, "--moldir", moldir_raw],
    "exit_code": 0,
    "stdout_sha256": digest(stdout_raw),
    "stderr_sha256": digest(stderr_raw),
    "check_cif_sha256": digest(check_cif_raw),
}
path = Path(output_json)
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
  verify_guards
done

verify_guards
verify_all_hashes
[ "$(sha_file "$MANIFEST")" = "$MANIFEST_SHA" ] || die "spec manifest changed during execution" 65

/usr/bin/python3 -I -S - \
  "$MANIFEST" "$CHECK_ROOT" "$LOG_ROOT" "$OUTPUT_MANIFEST" "$LOG_MANIFEST" \
  "$CHECKER" "$EXPECTED_CHECKER_SHA" "$CHECKER_VERSION" \
  "$MOLDIR" "$EXPECTED_MOLDIR_SHA" "$EXPECTED_RUNNER_SHA" "$EXPECTED_ENVIRONMENT_SHA" <<'PY'
import csv
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

(
    manifest_raw, output_root_raw, log_root_raw, output_manifest_raw, log_manifest_raw,
    checker, checker_sha, checker_version, moldir, moldir_sha, runner_sha, environment_sha,
) = sys.argv[1:]
manifest = Path(manifest_raw)
output_root = Path(output_root_raw)
log_root = Path(log_root_raw)
output_manifest = Path(output_manifest_raw)
log_manifest = Path(log_manifest_raw)
evidence_fields = {
    "schema_version", "spec_id", "spec_sha256", "checker_executable_path",
    "checker_executable_sha256", "checker_version", "moldir_sha256",
    "runner_sha256", "environment_receipt_sha256", "argv", "exit_code",
    "stdout_sha256", "stderr_sha256", "check_cif_sha256",
}
safe_component = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

def digest(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"non-regular evidence file: {path}")
        value = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            value.update(block)
        after = os.fstat(fd)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(before) != signature(after):
            raise SystemExit(f"evidence file changed while hashing: {path}")
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise SystemExit(f"evidence path identity changed while hashing: {path}")
        return value.hexdigest()
    finally:
        os.close(fd)

def children(directory: Path):
    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit(f"unsafe evidence directory: {directory}")
    with os.scandir(directory) as entries:
        return sorted(list(entries), key=lambda entry: entry.name)

def all_files(directory: Path):
    result = []
    stack = [directory]
    while stack:
        current = stack.pop()
        for entry in children(current):
            path = Path(entry.path)
            if safe_component.fullmatch(entry.name) is None:
                raise SystemExit(f"unsafe name in final evidence tree: {path}")
            if entry.is_symlink():
                raise SystemExit(f"symlink forbidden in final evidence tree: {path}")
            if entry.is_file(follow_symlinks=False):
                result.append(path)
            elif entry.is_dir(follow_symlinks=False):
                stack.append(path)
            else:
                raise SystemExit(f"special file forbidden in final evidence tree: {path}")
    return sorted(result)

def load_unique_json(path: Path):
    def unique(pairs):
        payload = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate key: {key}")
            payload[key] = value
        return payload
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(f"invalid execution evidence {path}: {error}")

with manifest.open(newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))
ids = [row["spec_id"] for row in rows]
if len(ids) != 12 or len(set(ids)) != 12:
    raise SystemExit("final manifest ID set is not exactly twelve")
for root, label in ((output_root, "output"), (log_root, "log")):
    entries = children(root)
    if {entry.name for entry in entries} != set(ids):
        raise SystemExit(f"final {label} top-level set differs from manifest")
    if any(entry.is_symlink() or not entry.is_dir(follow_symlinks=False) for entry in entries):
        raise SystemExit(f"final {label} top-level member is unsafe")

for row in rows:
    spec_id = row["spec_id"]
    output = output_root / spec_id
    log = log_root / spec_id
    output_files = all_files(output)
    cifs = [path for path in output_files if path.suffix.lower() in {".cif", ".mmcif"}]
    if len(output_files) != 1 or len(cifs) != 1:
        raise SystemExit(f"final output must contain exactly one CIF/mmCIF: {spec_id}")
    log_entries = children(log)
    expected_logs = {"check.stdout.log", "check.stderr.log", "check.exit_code.txt", "check.execution.json"}
    if {entry.name for entry in log_entries} != expected_logs:
        raise SystemExit(f"final log member set differs: {spec_id}")
    if any(entry.is_symlink() or not entry.is_file(follow_symlinks=False) for entry in log_entries):
        raise SystemExit(f"final log member is unsafe: {spec_id}")
    stdout = log / "check.stdout.log"
    stderr = log / "check.stderr.log"
    exit_code = log / "check.exit_code.txt"
    execution = log / "check.execution.json"
    if exit_code.read_bytes() != b"0\n":
        raise SystemExit(f"non-canonical final exit code: {spec_id}")
    payload = load_unique_json(execution)
    if not isinstance(payload, dict) or set(payload) != evidence_fields:
        raise SystemExit(f"final execution evidence fields differ: {spec_id}")
    expected = {
        "schema_version": "BOLTZGEN_CHECK_EXECUTION_V1",
        "spec_id": spec_id,
        "spec_sha256": row["spec_sha256"],
        "checker_executable_path": checker,
        "checker_executable_sha256": checker_sha,
        "checker_version": checker_version,
        "moldir_sha256": moldir_sha,
        "runner_sha256": runner_sha,
        "environment_receipt_sha256": environment_sha,
        "argv": [checker, "check", str(manifest.parent / row["spec_path"]), "--output", str(output), "--moldir", moldir],
        "exit_code": 0,
        "stdout_sha256": digest(stdout),
        "stderr_sha256": digest(stderr),
        "check_cif_sha256": digest(cifs[0]),
    }
    if payload != expected:
        raise SystemExit(f"final execution evidence content differs: {spec_id}")

def manifest_content(root: Path) -> bytes:
    lines = []
    for path in all_files(root):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{digest(path)}  ./{relative}\n")
    return "".join(lines).encode("utf-8")

def freeze_tree(root: Path) -> None:
    directories = [root]
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in children(current):
            path = Path(entry.path)
            if entry.is_symlink():
                raise SystemExit(f"symlink forbidden while freezing evidence: {path}")
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
                stack.append(path)
            elif not entry.is_file(follow_symlinks=False):
                raise SystemExit(f"special file forbidden while freezing evidence: {path}")
    for path in all_files(root):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, 0o400, follow_symlinks=False)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        os.chmod(path, 0o500, follow_symlinks=False)

def require_frozen_tree(root: Path) -> None:
    stack = [root]
    while stack:
        current = stack.pop()
        if stat.S_IMODE(current.stat(follow_symlinks=False).st_mode) != 0o500:
            raise SystemExit(f"evidence directory is not frozen read-only: {current}")
        for entry in children(current):
            path = Path(entry.path)
            if entry.is_symlink():
                raise SystemExit(f"symlink forbidden in frozen evidence: {path}")
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o400:
                    raise SystemExit(f"evidence file is not frozen read-only: {path}")
            else:
                raise SystemExit(f"special file forbidden in frozen evidence: {path}")

def publish(path: Path, content: bytes):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o400)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())

freeze_tree(output_root)
freeze_tree(log_root)
require_frozen_tree(output_root)
require_frozen_tree(log_root)
publish(output_manifest, manifest_content(output_root))
publish(log_manifest, manifest_content(log_root))
directory_fd = os.open(output_manifest.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
if stat.S_IMODE(output_manifest.stat(follow_symlinks=False).st_mode) != 0o400:
    raise SystemExit("output digest manifest is not frozen read-only")
if stat.S_IMODE(log_manifest.stat(follow_symlinks=False).st_mode) != 0o400:
    raise SystemExit("log digest manifest is not frozen read-only")
if output_manifest.read_bytes() != manifest_content(output_root):
    raise SystemExit("output digest manifest differs immediately after publication")
if log_manifest.read_bytes() != manifest_content(log_root):
    raise SystemExit("log digest manifest differs immediately after publication")
PY

verify_all_hashes
guard_file "$OUTPUT_MANIFEST" "check output digest manifest"
guard_file "$LOG_MANIFEST" "check log digest manifest"
/usr/bin/python3 -I -S - \
  "$MANIFEST" "$CHECK_ROOT" "$LOG_ROOT" "$OUTPUT_MANIFEST" "$LOG_MANIFEST" \
  "$CHECKER" "$EXPECTED_CHECKER_SHA" "$CHECKER_VERSION" \
  "$MOLDIR" "$EXPECTED_MOLDIR_SHA" "$EXPECTED_RUNNER_SHA" "$EXPECTED_ENVIRONMENT_SHA" <<'PY'
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

(
    manifest_raw, output_root_raw, log_root_raw, output_manifest_raw, log_manifest_raw,
    checker, checker_sha, checker_version, moldir, moldir_sha, runner_sha, environment_sha,
) = sys.argv[1:]
manifest = Path(manifest_raw)
output_root = Path(output_root_raw)
log_root = Path(log_root_raw)
output_manifest = Path(output_manifest_raw)
log_manifest = Path(log_manifest_raw)
safe_component = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
evidence_fields = {
    "schema_version", "spec_id", "spec_sha256", "checker_executable_path",
    "checker_executable_sha256", "checker_version", "moldir_sha256",
    "runner_sha256", "environment_receipt_sha256", "argv", "exit_code",
    "stdout_sha256", "stderr_sha256", "check_cif_sha256",
}

def read_stable(path: Path, *, frozen: bool = False) -> tuple[bytes, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"terminal member is not regular: {path}")
        if frozen and stat.S_IMODE(before.st_mode) != 0o400:
            raise SystemExit(f"terminal member is not frozen read-only: {path}")
        blocks = []
        hasher = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
            hasher.update(block)
        after = os.fstat(fd)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(before) != signature(after):
            raise SystemExit(f"terminal member changed while reading: {path}")
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
            raise SystemExit(f"terminal member path identity changed: {path}")
        return b"".join(blocks), hasher.hexdigest()
    finally:
        os.close(fd)

def children(directory: Path):
    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit(f"unsafe terminal evidence directory: {directory}")
    if stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode) != 0o500:
        raise SystemExit(f"terminal evidence directory is not frozen read-only: {directory}")
    with os.scandir(directory) as entries:
        return sorted(list(entries), key=lambda entry: entry.name)

def all_files(root: Path) -> list[Path]:
    files = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in children(current):
            path = Path(entry.path)
            if safe_component.fullmatch(entry.name) is None:
                raise SystemExit(f"unsafe terminal evidence name: {path}")
            if entry.is_symlink():
                raise SystemExit(f"symlink forbidden in terminal evidence: {path}")
            if entry.is_file(follow_symlinks=False):
                read_stable(path, frozen=True)
                files.append(path)
            elif entry.is_dir(follow_symlinks=False):
                stack.append(path)
            else:
                raise SystemExit(f"special file forbidden in terminal evidence: {path}")
    return sorted(files)

def unique_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload

manifest_bytes, _ = read_stable(manifest)
try:
    rows = list(csv.DictReader(io.StringIO(manifest_bytes.decode("utf-8"), newline=""), delimiter="\t"))
except (UnicodeError, csv.Error) as error:
    raise SystemExit(f"terminal manifest parse failed: {error}")
ids = [row["spec_id"] for row in rows]
if len(ids) != 12 or len(set(ids)) != 12:
    raise SystemExit("terminal spec ID set is not exactly twelve")
for root, label in ((output_root, "output"), (log_root, "log")):
    entries = children(root)
    if {entry.name for entry in entries} != set(ids):
        raise SystemExit(f"terminal {label} top-level set differs")
    if any(entry.is_symlink() or not entry.is_dir(follow_symlinks=False) for entry in entries):
        raise SystemExit(f"terminal {label} top-level member is unsafe")

for row in rows:
    spec_id = row["spec_id"]
    output = output_root / spec_id
    log = log_root / spec_id
    output_files = all_files(output)
    cifs = [path for path in output_files if path.suffix.lower() in {".cif", ".mmcif"}]
    if len(output_files) != 1 or len(cifs) != 1:
        raise SystemExit(f"terminal output must contain exactly one CIF/mmCIF: {spec_id}")
    log_entries = children(log)
    expected_logs = {"check.stdout.log", "check.stderr.log", "check.exit_code.txt", "check.execution.json"}
    if {entry.name for entry in log_entries} != expected_logs:
        raise SystemExit(f"terminal log set differs: {spec_id}")
    if any(entry.is_symlink() or not entry.is_file(follow_symlinks=False) for entry in log_entries):
        raise SystemExit(f"terminal log member is unsafe: {spec_id}")
    stdout = log / "check.stdout.log"
    stderr = log / "check.stderr.log"
    exit_path = log / "check.exit_code.txt"
    execution = log / "check.execution.json"
    exit_bytes, _ = read_stable(exit_path, frozen=True)
    if exit_bytes != b"0\n":
        raise SystemExit(f"terminal exit code differs: {spec_id}")
    execution_bytes, _ = read_stable(execution, frozen=True)
    try:
        payload = json.loads(execution_bytes, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(f"terminal execution JSON is invalid for {spec_id}: {error}")
    _, stdout_sha = read_stable(stdout, frozen=True)
    _, stderr_sha = read_stable(stderr, frozen=True)
    _, cif_sha = read_stable(cifs[0], frozen=True)
    expected = {
        "schema_version": "BOLTZGEN_CHECK_EXECUTION_V1",
        "spec_id": spec_id,
        "spec_sha256": row["spec_sha256"],
        "checker_executable_path": checker,
        "checker_executable_sha256": checker_sha,
        "checker_version": checker_version,
        "moldir_sha256": moldir_sha,
        "runner_sha256": runner_sha,
        "environment_receipt_sha256": environment_sha,
        "argv": [checker, "check", str(manifest.parent / row["spec_path"]), "--output", str(output), "--moldir", moldir],
        "exit_code": 0,
        "stdout_sha256": stdout_sha,
        "stderr_sha256": stderr_sha,
        "check_cif_sha256": cif_sha,
    }
    if not isinstance(payload, dict) or set(payload) != evidence_fields or payload != expected:
        raise SystemExit(f"terminal execution evidence differs: {spec_id}")

def expected_digest_manifest(root: Path) -> bytes:
    lines = []
    for path in all_files(root):
        _, digest = read_stable(path, frozen=True)
        relative = path.relative_to(root).as_posix()
        lines.append(f"{digest}  ./{relative}\n")
    return "".join(lines).encode("utf-8")

output_manifest_bytes, _ = read_stable(output_manifest, frozen=True)
log_manifest_bytes, _ = read_stable(log_manifest, frozen=True)
if output_manifest_bytes != expected_digest_manifest(output_root):
    raise SystemExit("published output manifest differs from terminal output tree")
if log_manifest_bytes != expected_digest_manifest(log_root):
    raise SystemExit("published log manifest differs from terminal log tree")
print("BOLTZGEN_CHECK_12_OF_12_PASS")
PY
