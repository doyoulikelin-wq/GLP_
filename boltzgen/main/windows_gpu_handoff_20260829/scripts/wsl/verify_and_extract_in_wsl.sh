#!/usr/bin/env bash
# Verify the transfer package, extract it only into WSL2 Linux storage, and rehash it.
set -euo pipefail
umask 077

bundle_input="${1:?usage: verify_and_extract_in_wsl.sh BUNDLE_ROOT TARGET_ROOT EVIDENCE_ROOT ATTEMPT_ID EXPECTED_TRANSFER_SHA256}"
target_input="${2:?usage: verify_and_extract_in_wsl.sh BUNDLE_ROOT TARGET_ROOT EVIDENCE_ROOT ATTEMPT_ID EXPECTED_TRANSFER_SHA256}"
evidence_input="${3:?usage: verify_and_extract_in_wsl.sh BUNDLE_ROOT TARGET_ROOT EVIDENCE_ROOT ATTEMPT_ID EXPECTED_TRANSFER_SHA256}"
attempt_id="${4:?usage: verify_and_extract_in_wsl.sh BUNDLE_ROOT TARGET_ROOT EVIDENCE_ROOT ATTEMPT_ID EXPECTED_TRANSFER_SHA256}"
expected_transfer_sha256="${5:?usage: verify_and_extract_in_wsl.sh BUNDLE_ROOT TARGET_ROOT EVIDENCE_ROOT ATTEMPT_ID EXPECTED_TRANSFER_SHA256}"
[[ "$attempt_id" =~ ^attempt_[0-9]{3}$ ]] || {
  printf 'invalid attempt ID: %s\n' "$attempt_id" >&2
  exit 64
}
[[ "$expected_transfer_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'EXPECTED_TRANSFER_SHA256 must be 64 lowercase hexadecimal characters\n' >&2
  exit 64
}

for command_name in sha256sum zstd tar git python3 realpath awk; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command: %s\n' "$command_name" >&2
    exit 69
  }
done

bundle_root="$(realpath "$bundle_input")"
test -d "$bundle_root"
target_root="$(python3 -I - "$target_input" <<'PY'
import sys
from pathlib import Path

raw = Path(sys.argv[1])
if not raw.is_absolute() or ".." in raw.parts or raw.name in {"", ".", ".."}:
    raise SystemExit("TARGET_ROOT must be an absolute normalized path")
parent = raw.parent.resolve(strict=True)
home = Path("/home").resolve(strict=True)
try:
    relative_parent = parent.relative_to(home)
except ValueError as exc:
    raise SystemExit("TARGET_ROOT parent resolves outside /home") from exc
if not relative_parent.parts:
    raise SystemExit("TARGET_ROOT must be below a /home/<user> directory")
target = parent / raw.name
print(target)
PY
)"
evidence_root="$(python3 -I - "$evidence_input" <<'PY'
import sys
from pathlib import Path

raw = Path(sys.argv[1])
if not raw.is_absolute() or ".." in raw.parts or raw.name in {"", ".", ".."}:
    raise SystemExit("EVIDENCE_ROOT must be an absolute normalized path")
if raw.exists() and raw.is_symlink():
    raise SystemExit("EVIDENCE_ROOT must not be a symlink")
parent = raw.parent.resolve(strict=True)
home = Path("/home").resolve(strict=True)
try:
    relative_parent = parent.relative_to(home)
except ValueError as exc:
    raise SystemExit("EVIDENCE_ROOT parent resolves outside /home") from exc
if not relative_parent.parts:
    raise SystemExit("EVIDENCE_ROOT must be below a /home/<user> directory")
print(raw.resolve(strict=True) if raw.exists() else parent / raw.name)
PY
)"
test ! -e "$target_root" && test ! -L "$target_root" || {
  printf 'refusing to overwrite existing target: %s\n' "$target_root" >&2
  exit 73
}

grep -Eqi '(microsoft-standard-WSL2|WSL2)' /proc/sys/kernel/osrelease || {
  printf 'this extractor must run inside WSL2\n' >&2
  exit 65
}

mkdir -p "$evidence_root"
test ! -L "$evidence_root" && test -d "$evidence_root"
attempt_parent="$evidence_root/t0_transfer"
test ! -L "$attempt_parent"
mkdir -p "$attempt_parent"
attempt_root="$attempt_parent/$attempt_id"
mkdir "$attempt_root" || {
  printf 'T0 attempt already exists: %s\n' "$attempt_root" >&2
  exit 73
}
chmod 0750 "$attempt_root"
stage_root=""
published_target=0
exec 3>&1 4>&2

finalize() {
  local exit_code="$?"
  trap - EXIT INT TERM
  set +e
  if [ -n "${stage_root:-}" ] && [ -d "$stage_root" ]; then
    rm -rf -- "$stage_root"
  fi
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/ended_at_utc.txt"
  if [ "$exit_code" -eq 0 ] && [ "$published_target" -eq 1 ]; then
    final_status="TRANSFER_AND_SOURCE_VALIDATION_PASS"
    if ! cp "$target_root/handoff/T0_RECEIPT.json" \
        "$attempt_root/internal_T0_RECEIPT.json" \
      || ! cp "$target_root/handoff/T0_OUTPUTS.SHA256SUMS" \
        "$attempt_root/internal_T0_OUTPUTS.SHA256SUMS"; then
      final_status="BLOCKED_TRANSFER_INTEGRITY"
      exit_code=75
    fi
  else
    final_status="BLOCKED_TRANSFER_INTEGRITY"
    if [ "$exit_code" -eq 0 ]; then
      exit_code=75
    fi
  fi
  printf '%s\n' "$exit_code" > "$attempt_root/exit_code.txt"
  printf '%s\n' "$final_status" > "$attempt_root/STATUS.txt"
  (
    cd "$attempt_root" || exit 1
    find . -type f ! -name 'outputs.SHA256SUMS' ! -name 'receipt.json' \
      -print0 | sort -z | xargs -0 sha256sum
  ) > "$attempt_root/outputs.SHA256SUMS"
  python3 -I - \
    "$attempt_root" "$exit_code" "$final_status" \
    "$expected_transfer_sha256" "$published_target" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = root / "outputs.SHA256SUMS"
payload = {
    "schema_version": "T0_TRANSFER_ATTEMPT_RECEIPT_V1",
    "attempt_id": root.name,
    "exit_code": int(sys.argv[2]),
    "status": sys.argv[3],
    "expected_transfer_sha256": sys.argv[4],
    "target_published": sys.argv[5] == "1",
    "outputs_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "formal_gate": False,
}
(root / "receipt.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
  receipt_exit="$?"
  chmod -R a-w "$attempt_root"
  printf '%s evidence=%s\n' "$final_status" "$attempt_root" >&3
  if [ "$receipt_exit" -ne 0 ]; then
    exit 75
  fi
  exit "$exit_code"
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%q ' "$0" "$@" > "$attempt_root/command.txt"
printf '\n' >> "$attempt_root/command.txt"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$attempt_root/started_at_utc.txt"
exec >"$attempt_root/stdout.log" 2>"$attempt_root/stderr.log"

cd "$bundle_root"
external_transfer_hash="${bundle_root}.TRANSFER.SHA256"
test -f "$external_transfer_hash" && test ! -L "$external_transfer_hash" || {
  printf 'missing external transfer checksum: %s\n' "$external_transfer_hash" >&2
  exit 66
}
observed_transfer_sha256="$(sha256sum TRANSFER.SHA256SUMS | awk '{print $1}')"
test "$observed_transfer_sha256" = "$expected_transfer_sha256" || {
  printf 'out-of-band transfer digest mismatch: expected=%s observed=%s\n' \
    "$expected_transfer_sha256" "$observed_transfer_sha256" >&2
  exit 65
}
(
  cd "$(dirname "$bundle_root")"
  sha256sum -c "$(basename "$external_transfer_hash")"
)
sha256sum -c TRANSFER.SHA256SUMS
sha256sum -c PAYLOAD.SHA256SUMS
python3 -I - "$bundle_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
expected = {"PAYLOAD.SHA256SUMS", "PACKAGE_RECEIPT.json", "TRANSFER.SHA256SUMS"}
for line in (root / "PAYLOAD.SHA256SUMS").read_text(encoding="utf-8").splitlines():
    _, relative = line.split("  ", 1)
    expected.add(relative)
actual = set()
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit(f"symlink forbidden in transfer package: {path}")
    if path.is_file():
        actual.add(path.relative_to(root).as_posix())
    elif not path.is_dir():
        raise SystemExit(f"non-regular transfer member forbidden: {path}")
if actual != expected:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise SystemExit(f"transfer inventory mismatch; missing={missing}; extra={extra}")
PY
git bundle list-heads git/GLP_sanitized.bundle >/dev/null
zstd -q -t archives/00_project_sanitized_HEAD.tar.zst
zstd -q -t archives/00_boltzgen_upstream_v0.3.2.tar.zst
zstd -q -t archives/02_active_project_data.tar.zst
cat archives/01_gpu_runtime_assets.tar.zst.part-* | zstd -q -t

mkdir "$attempt_root/archive_members"
zstd -dc archives/00_project_sanitized_HEAD.tar.zst \
  | tar -tf - > "$attempt_root/archive_members/project.txt"
zstd -dc archives/00_project_sanitized_HEAD.tar.zst \
  | tar -tvf - | awk 'substr($0,1,1) !~ /^[-d]$/ {bad=1} END {exit bad}'
zstd -dc archives/00_boltzgen_upstream_v0.3.2.tar.zst \
  | tar -tf - > "$attempt_root/archive_members/upstream.txt"
zstd -dc archives/00_boltzgen_upstream_v0.3.2.tar.zst \
  | tar -tvf - | awk 'substr($0,1,1) !~ /^[-d]$/ {bad=1} END {exit bad}'
zstd -dc archives/02_active_project_data.tar.zst \
  | tar -tf - > "$attempt_root/archive_members/active.txt"
zstd -dc archives/02_active_project_data.tar.zst \
  | tar -tvf - | awk 'substr($0,1,1) !~ /^[-d]$/ {bad=1} END {exit bad}'
cat archives/01_gpu_runtime_assets.tar.zst.part-* | zstd -dc \
  | tar -tf - > "$attempt_root/archive_members/runtime.txt"
cat archives/01_gpu_runtime_assets.tar.zst.part-* | zstd -dc \
  | tar -tvf - | awk 'substr($0,1,1) !~ /^[-d]$/ {bad=1} END {exit bad}'

python3 -I - "$bundle_root" "$attempt_root/archive_members" <<'PY'
import sys
from pathlib import Path, PurePosixPath

bundle = Path(sys.argv[1]).resolve(strict=True)
observed_root = Path(sys.argv[2]).resolve(strict=True)

def manifest_paths(path: Path) -> set[str]:
    rows = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        relative = line.split("  ", 1)[1] if "  " in line else line
        rows.add(relative)
    return rows

expected = {
    "project": {
        f"GLP_/{path}"
        for path in manifest_paths(bundle / "manifests/sanitized_source_paths.txt")
    },
    "upstream": manifest_paths(
        bundle / "manifests/contracts/boltzgen_upstream_tree.SHA256SUMS"
    ),
    "active": manifest_paths(bundle / "manifests/active_included_paths.txt"),
    "runtime": manifest_paths(bundle / "manifests/runtime_included_paths.txt"),
}
for label, expected_files in expected.items():
    observed_files = set()
    observed_directories = set()
    for name in (observed_root / f"{label}.txt").read_text(encoding="utf-8").splitlines():
        if not name:
            continue
        key = PurePosixPath(name)
        if key.is_absolute() or ".." in key.parts or "\x00" in name:
            raise SystemExit(f"unsafe {label} archive member: {name!r}")
        if name.endswith("/"):
            observed_directories.add(name.rstrip("/"))
        else:
            if name in observed_files:
                raise SystemExit(f"duplicate {label} archive file member: {name}")
            observed_files.add(name)
    if observed_files != expected_files:
        raise SystemExit(
            f"{label} archive file inventory mismatch; "
            f"missing={sorted(expected_files-observed_files)}; "
            f"extra={sorted(observed_files-expected_files)}"
        )
    for directory in observed_directories:
        prefix = f"{directory}/"
        if not any(path.startswith(prefix) for path in expected_files):
            raise SystemExit(f"orphan {label} archive directory member: {directory}")
print("ARCHIVE_MEMBER_INVENTORY_PASS")
PY

git_identity_text="$(python3 -I - "$bundle_root/HANDOFF_STATUS.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["handoff_git"]["branch"])
print(payload["handoff_git"]["commit"])
PY
)"
readarray -t git_identity <<< "$git_identity_text"
baseline_branch="${git_identity[0]}"
baseline_commit="${git_identity[1]}"

target_parent="$(dirname "$target_root")"
mkdir -p "$target_parent"
stage_root="$(mktemp -d "$target_parent/.windows_gpu_handoff.extract.XXXXXX")"

git clone --no-local --branch "$baseline_branch" \
  "$bundle_root/git/GLP_sanitized.bundle" "$stage_root/GLP_" >/dev/null
test "$(git -C "$stage_root/GLP_" rev-parse HEAD)" = "$baseline_commit"
git -C "$stage_root/GLP_" bundle verify \
  "$bundle_root/git/GLP_sanitized.bundle" >/dev/null
git -C "$stage_root/GLP_" config core.autocrlf false
git -C "$stage_root/GLP_" remote remove origin
test -z "$(git -C "$stage_root/GLP_" remote)"
working_branch="codex/windows-gpu-$(date -u +'%Y%m%d')"
git -C "$stage_root/GLP_" switch -c "$working_branch" "$baseline_commit" >/dev/null
git -C "$stage_root/GLP_" config user.name "Windows GPU Codex"
git -C "$stage_root/GLP_" config user.email "noreply@local.invalid"
test "$(git -C "$stage_root/GLP_" symbolic-ref --short HEAD)" = "$working_branch"
zstd -dc archives/00_boltzgen_upstream_v0.3.2.tar.zst | tar -xf - -C "$stage_root"
zstd -dc archives/02_active_project_data.tar.zst | tar -xf - -C "$stage_root"
cat archives/01_gpu_runtime_assets.tar.zst.part-* | zstd -dc | tar -xf - -C "$stage_root"

( cd "$stage_root" && sha256sum -c "$bundle_root/manifests/runtime_tree.SHA256SUMS" )
( cd "$stage_root" && sha256sum -c "$bundle_root/manifests/active_data_tree.SHA256SUMS" )
( cd "$stage_root" && sha256sum -c \
    "$bundle_root/manifests/contracts/boltzgen_upstream_tree.SHA256SUMS" )

python3 -I "$bundle_root/scripts/validate_handoff_sources.py" \
  --workspace-root "$stage_root" \
  --repo-root "$stage_root/GLP_" \
  --output-dir "$stage_root/.handoff_source_revalidation"

bash "$bundle_root/scripts/wsl/create_compatibility_links.sh" "$stage_root" \
  > "$stage_root/.compatibility_links.tsv"

test ! -e "$stage_root/private"
test ! -e "$stage_root/shared/data/peptide_lockbox_countertargets_20260823"
test ! -e "$stage_root/data/样本数据/not_binding"
test ! -L "$stage_root/data/样本数据/not_binding"

mkdir -p "$stage_root/handoff"
cp "$bundle_root/HANDOFF_STATUS.json" "$stage_root/handoff/"
cp "$bundle_root/PACKAGE_RECEIPT.json" "$stage_root/handoff/"
cp "$bundle_root/TASKS_FOR_WINDOWS_CODEX.md" "$stage_root/handoff/"
cp "$bundle_root/GLOSSARY_ZH.md" "$stage_root/handoff/"
cp "$bundle_root/WINDOWS_CODEX_START_PROMPT.md" "$stage_root/handoff/"
cp "$bundle_root/ENGINEERING_EXPERIENCE_EVENT_SCHEMA.json" "$stage_root/handoff/"
cp "$bundle_root/TASK_ORDER.tsv" "$stage_root/handoff/"
cp -R "$bundle_root/scripts" "$stage_root/handoff/"
cp -R "$bundle_root/manifests" "$stage_root/handoff/package_manifests"
cp "$bundle_root/PAYLOAD.SHA256SUMS" "$stage_root/handoff/"
cp "$bundle_root/TRANSFER.SHA256SUMS" "$stage_root/handoff/"
cp "$external_transfer_hash" "$stage_root/handoff/EXTERNAL_TRANSFER.SHA256"
cp -R "$stage_root/.handoff_source_revalidation" \
  "$stage_root/handoff/source_revalidation"
cp "$stage_root/.compatibility_links.tsv" "$stage_root/handoff/compatibility_links.tsv"
printf 'TRANSFER_AND_SOURCE_VALIDATION_PASS\n' \
  > "$stage_root/handoff/T0_STATUS.txt"
(
  cd "$stage_root/handoff"
  find . -type f ! -name 'T0_OUTPUTS.SHA256SUMS' ! -name 'T0_RECEIPT.json' \
    -print0 | sort -z | xargs -0 sha256sum
) > "$stage_root/handoff/T0_OUTPUTS.SHA256SUMS"
python3 -I - "$stage_root/handoff/T0_RECEIPT.json" \
  "$baseline_branch" "$baseline_commit" "$working_branch" \
  "$expected_transfer_sha256" "$stage_root/handoff/T0_OUTPUTS.SHA256SUMS" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
payload = {
    "schema_version": "WINDOWS_GPU_HANDOFF_T0_RECEIPT_V1",
    "status": "TRANSFER_AND_SOURCE_VALIDATION_PASS",
    "exit_code": 0,
    "sanitized_git_branch": sys.argv[2],
    "sanitized_git_commit": sys.argv[3],
    "windows_working_branch": sys.argv[4],
    "external_transfer_hash_verified": True,
    "package_inventory_exact": True,
    "source_revalidation": "PASS",
    "out_of_band_transfer_sha256_verified": True,
    "lockbox_structure_files_included": False,
    "lockbox_identity_metadata_included": True,
    "candidate_lockbox_results_included": False,
    "lockbox_access_count": 0,
    "expected_transfer_sha256": sys.argv[5],
    "outputs_manifest_sha256": hashlib.sha256(Path(sys.argv[6]).read_bytes()).hexdigest(),
}
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chmod -R a-w "$stage_root/handoff"

mv "$stage_root" "$target_root"
stage_root=""
published_target=1

printf 'TRANSFER_AND_SOURCE_VALIDATION_PASS target=%s\n' "$target_root"
exit 0
