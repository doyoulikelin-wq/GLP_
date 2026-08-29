#!/usr/bin/env bash
# Export only reviewed source changes from the sanitized Windows Git baseline.
set -euo pipefail
umask 077

repo_root="${1:?usage: export_code_patches_for_mac.sh REPO_ROOT OUTPUT_PARENT ATTEMPT_ID}"
output_parent="${2:?usage: export_code_patches_for_mac.sh REPO_ROOT OUTPUT_PARENT ATTEMPT_ID}"
attempt_id="${3:?usage: export_code_patches_for_mac.sh REPO_ROOT OUTPUT_PARENT ATTEMPT_ID}"

[[ "$attempt_id" =~ ^attempt_[0-9]{3}$ ]] || {
  printf 'invalid attempt ID: %s\n' "$attempt_id" >&2
  exit 64
}
for command_name in git python3 sha256sum find sort xargs; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command: %s\n' "$command_name" >&2
    exit 69
  }
done
canonical_paths_text="$(python3 -I - "$repo_root" "$output_parent" <<'PY'
import sys
from pathlib import Path

home = Path("/home").resolve(strict=True)
resolved = []
for label, value in zip(("repo", "output"), sys.argv[1:]):
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts or raw.is_symlink() or not raw.is_dir():
        raise SystemExit(f"{label} must be an existing absolute non-symlink directory")
    path = raw.resolve(strict=True)
    if path != raw:
        raise SystemExit(f"{label} path must not traverse symlinks: {raw}")
    try:
        relative = path.relative_to(home)
    except ValueError as exc:
        raise SystemExit(f"{label} resolves outside /home") from exc
    if len(relative.parts) < 2:
        raise SystemExit(f"{label} must be below /home/<user>")
    resolved.append(path)
repo, output = resolved
if repo.name != "GLP_" or not (repo / ".git").is_dir():
    raise SystemExit("repo must be the extracted GLP_ Git worktree")
try:
    output.relative_to(repo.parent)
except ValueError as exc:
    raise SystemExit("output must remain under the extracted workspace") from exc
if output == repo or repo in output.parents:
    raise SystemExit("output must be outside the Git repository")
print(repo)
print(output)
PY
)"
readarray -t canonical_paths <<< "$canonical_paths_text"
repo_root="${canonical_paths[0]}"
output_parent="${canonical_paths[1]}"
handoff_status="$(dirname "$repo_root")/handoff/HANDOFF_STATUS.json"
test -f "$handoff_status" && test ! -L "$handoff_status" || {
  printf 'verified handoff status is missing: %s\n' "$handoff_status" >&2
  exit 66
}
test -z "$(git -C "$repo_root" remote)" || {
  printf 'sanitized Windows repository must not have a remote\n' >&2
  exit 65
}
test -z "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)" || {
  printf 'commit or discard the local working-tree changes before export\n' >&2
  exit 74
}
baseline_identity_text="$(python3 -I - "$handoff_status" <<'PY'
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
commit = payload["handoff_git"]["commit"]
branch = payload["handoff_git"]["branch"]
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("invalid sanitized baseline commit")
print(commit)
print(branch)
PY
)"
readarray -t baseline_identity <<< "$baseline_identity_text"
baseline_commit="${baseline_identity[0]}"
baseline_branch="${baseline_identity[1]}"
test "$(git -C "$repo_root" rev-parse "$baseline_commit^{commit}")" = "$baseline_commit"
test "$(git -C "$repo_root" rev-list --max-parents=0 HEAD)" = "$baseline_commit" || {
  printf 'HANDOFF_STATUS baseline is not the unique root commit\n' >&2
  exit 65
}
git -C "$repo_root" merge-base --is-ancestor "$baseline_commit" HEAD
current_branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD)" || {
  printf 'Windows repository must be on a named feature branch\n' >&2
  exit 74
}
[[ "$current_branch" =~ ^codex/windows-gpu-[0-9]{8}$ ]] || {
  printf 'unexpected Windows working branch: %s\n' "$current_branch" >&2
  printf 'expected pattern: codex/windows-gpu-<YYYYMMDD>\n' >&2
  exit 74
}
test "$current_branch" != "$baseline_branch"
test "$(git -C "$repo_root" rev-parse HEAD)" != \
  "$baseline_commit" || {
  printf 'no Windows code commits to export\n' >&2
  exit 66
}

exports_root="$output_parent/windows_code_patch_exports"
test ! -L "$exports_root" || {
  printf 'patch export root must not be a symlink: %s\n' "$exports_root" >&2
  exit 65
}
mkdir -p "$exports_root"
final_attempt="$exports_root/$attempt_id"
external_patch_hash="$exports_root/${attempt_id}.PATCH.SHA256"
test ! -e "$final_attempt" && test ! -L "$final_attempt" || {
  printf 'attempt already exists: %s\n' "$final_attempt" >&2
  exit 73
}
test ! -e "$external_patch_hash" && test ! -L "$external_patch_hash" || {
  printf 'patch checksum already exists: %s\n' "$external_patch_hash" >&2
  exit 73
}
stage_root="$(mktemp -d "$exports_root/.${attempt_id}.build.XXXXXX")"
attempt_root="$stage_root/$attempt_id"
mkdir "$attempt_root"
published_attempt=0
published_hash=0
cleanup() {
  local exit_code="$?"
  trap - EXIT INT TERM
  if [ "$published_hash" -eq 1 ] && [ -f "$external_patch_hash" ]; then
    rm -f -- "$external_patch_hash"
  fi
  if [ "$published_attempt" -eq 1 ] && [ -d "$final_attempt" ]; then
    rm -rf -- "$final_attempt"
  fi
  if [ -d "$stage_root" ]; then
    rm -rf -- "$stage_root"
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

git -C "$repo_root" diff --name-only -z "$baseline_commit"..HEAD \
  | python3 -I -c 'import sys; rows=[x for x in sys.stdin.buffer.read().split(b"\0") if x]; bad=[x for x in rows if b"\n" in x or b"\r" in x]; sys.exit("newline in Git path is forbidden") if bad else print("\n".join(x.decode("utf-8") for x in rows))' \
  > "$attempt_root/changed_paths.txt"

python3 -I - \
  "$repo_root" "$attempt_root/changed_paths.txt" "$baseline_commit" \
  "$attempt_root/patch_scope.json" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve(strict=True)
changed = [line for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines() if line]
baseline_commit = sys.argv[3]
if not changed:
    raise SystemExit("empty changed-path set")
if len(changed) > 250 or len(set(changed)) != len(changed):
    raise SystemExit(f"changed-path count invalid: {len(changed)}")
allowed_prefixes = ("boltzgen/main/", "boltzgen/plans/", "boltzgen/resources/")
forbidden_markers = ("LOCKBOX", "7DTY", "6LMK", "7LLY", "GIP", "GLUCAGON", "OXYNTOMODULIN")
allowed_suffixes = {
    ".cfg", ".csv", ".ini", ".json", ".md", ".ps1", ".py", ".sh",
    ".sql", ".toml", ".tsv", ".txt", ".yaml", ".yml",
}
secret_patterns = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
)
absolute_user_paths = (
    re.compile(r"/Users/[^/\s]+/"),
    re.compile(r"/home/[^/\s]+/"),
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+[\\/]+"),
)
total_bytes = 0
existing_file_count = 0
baseline_file_bytes = 0
baseline_file_count = 0
allowed_git_modes = {"100644", "100755"}

def require_text_blob(raw: bytes, relative: str, identity: str) -> str:
    if len(raw) > 5 * 1024 * 1024:
        raise SystemExit(f"{identity} file exceeds 5 MiB: {relative}")
    if b"\0" in raw:
        raise SystemExit(f"binary NUL byte forbidden in {identity} file: {relative}")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{identity} file is not UTF-8 text: {relative}") from exc

def tree_entry(revision: str, relative: str) -> tuple[str, str] | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-z", revision, "--", relative],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    rows = [row for row in result.stdout.split(b"\0") if row]
    if not rows:
        return None
    if len(rows) != 1:
        raise SystemExit(f"ambiguous Git tree entry: {revision}:{relative}")
    metadata, observed_path = rows[0].split(b"\t", 1)
    mode, object_type, _object_id = metadata.decode("ascii").split(" ", 2)
    if observed_path.decode("utf-8") != relative:
        raise SystemExit(f"Git tree path mismatch: {revision}:{relative}")
    return mode, object_type

for relative in changed:
    upper = relative.upper()
    path_key = Path(relative)
    if (
        not relative.startswith(allowed_prefixes)
        or path_key.is_absolute()
        or ".." in path_key.parts
    ):
        raise SystemExit(f"path outside patch allowlist: {relative}")
    if any(marker in upper for marker in forbidden_markers):
        raise SystemExit(f"forbidden marker in changed path: {relative}")
    if path_key.suffix.lower() not in allowed_suffixes and path_key.name not in {
        "Dockerfile",
        "Makefile",
    }:
        raise SystemExit(f"non-text or unapproved file type in code patch: {relative}")
    baseline_entry = tree_entry(baseline_commit, relative)
    final_entry = tree_entry("HEAD", relative)
    for identity, entry in (("baseline-tree", baseline_entry), ("final-tree", final_entry)):
        if entry is not None and (entry[0] not in allowed_git_modes or entry[1] != "blob"):
            raise SystemExit(
                f"non-regular Git mode forbidden in {identity}: {relative} mode={entry[0]} type={entry[1]}"
            )
    path = repo / relative
    if path.is_symlink():
        raise SystemExit(f"final-tree symlink forbidden in code patch: {relative}")
    if path.exists():
        if not path.is_file():
            raise SystemExit(f"changed path is not a regular file: {relative}")
        size = path.stat().st_size
        raw = path.read_bytes()
        text = require_text_blob(raw, relative, "final-tree")
        if any(pattern.search(text) for pattern in secret_patterns):
            raise SystemExit(f"credential-like content forbidden in code patch: {relative}")
        if any(pattern.search(text) for pattern in absolute_user_paths):
            raise SystemExit(f"machine-specific user path forbidden in code patch: {relative}")
        total_bytes += size
        existing_file_count += 1
    if baseline_entry is not None:
        baseline_blob = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"{baseline_commit}:{relative}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        require_text_blob(baseline_blob.stdout, relative, "baseline-tree")
        baseline_file_bytes += len(baseline_blob.stdout)
        baseline_file_count += 1
if total_bytes > 20 * 1024 * 1024:
    raise SystemExit(f"changed text payload exceeds 20 MiB: {total_bytes}")
payload = {
    "schema_version": "WINDOWS_CODE_PATCH_SCOPE_V1",
    "status": "PASS",
    "changed_path_count": len(changed),
    "existing_file_count": existing_file_count,
    "existing_file_bytes": total_bytes,
    "baseline_file_count": baseline_file_count,
    "baseline_file_bytes": baseline_file_bytes,
    "export_kind": "SQUASHED_FINAL_TREE_DIFF",
    "intermediate_commit_metadata_included": False,
    "text_only": True,
    "secret_scan": "PASS",
    "absolute_user_path_scan": "PASS",
}
Path(sys.argv[4]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

test -z "$(git -C "$repo_root" rev-list --min-parents=2 "$baseline_commit"..HEAD)" || {
  printf 'merge commits are forbidden in a Windows patch export\n' >&2
  exit 74
}
git -C "$repo_root" diff --check "$baseline_commit"..HEAD
mkdir "$attempt_root/patches"
patch_path="$attempt_root/patches/0001-windows-gpu-squashed-final-tree.patch"
git -C "$repo_root" diff \
  --no-ext-diff --no-color --full-index --no-renames --unified=0 \
  "$baseline_commit" HEAD > "$patch_path"
test -s "$patch_path"

python3 -I - "$patch_path" "$attempt_root/patch_scan.json" <<'PY'
import json
import re
import sys
from pathlib import Path

patch_path = Path(sys.argv[1])
raw = patch_path.read_bytes()
if len(raw) > 20 * 1024 * 1024:
    raise SystemExit(f"squashed patch exceeds 20 MiB: {len(raw)}")
if b"\0" in raw or b"GIT binary patch" in raw or b"Binary files " in raw:
    raise SystemExit("binary content is forbidden in squashed patch")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("squashed patch is not UTF-8 text") from exc
secret_patterns = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
)
absolute_user_paths = (
    re.compile(r"/Users/[^/\s]+/"),
    re.compile(r"/home/[^/\s]+/"),
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+[\\/]+"),
)
if any(pattern.search(text) for pattern in secret_patterns):
    raise SystemExit("credential-like content forbidden in emitted patch bytes")
if any(pattern.search(text) for pattern in absolute_user_paths):
    raise SystemExit("machine-specific user path forbidden in emitted patch bytes")
payload = {
    "schema_version": "WINDOWS_CODE_PATCH_BYTE_SCAN_V1",
    "status": "PASS",
    "patch_bytes": len(raw),
    "utf8_text": True,
    "binary_content_included": False,
    "credential_like_match_count": 0,
    "absolute_user_path_match_count": 0,
    "intermediate_commit_metadata_included": False,
}
Path(sys.argv[2]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

apply_check_root="$stage_root/apply_check"
git init -q -b patch-check "$apply_check_root"
git -C "$apply_check_root" fetch -q --no-tags "$repo_root" "$baseline_commit"
git -C "$apply_check_root" checkout -q --detach FETCH_HEAD
git -C "$apply_check_root" apply --check "$patch_path"
git -C "$apply_check_root" apply --index "$patch_path"
target_tree="$(git -C "$repo_root" rev-parse 'HEAD^{tree}')"
test "$(git -C "$apply_check_root" write-tree)" = "$target_tree"
rm -rf -- "$apply_check_root"
(
  cd "$attempt_root"
  find changed_paths.txt patch_scope.json patch_scan.json patches -type f -print0 \
    | sort -z | xargs -0 sha256sum
) > "$attempt_root/PATCH_PAYLOAD.SHA256SUMS"
python3 -I - "$attempt_root" "$baseline_commit" "$current_branch" \
  "$(git -C "$repo_root" rev-parse HEAD)" "$target_tree" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = root / "PATCH_PAYLOAD.SHA256SUMS"
scope = json.loads((root / "patch_scope.json").read_text(encoding="utf-8"))
payload = {
    "schema_version": "WINDOWS_TO_MAC_CODE_PATCH_RECEIPT_V1",
    "attempt_id": root.name,
    "status": "CODE_PATCH_EXPORT_PASS",
    "integration_authority": "MAC_CODEX_REVIEW_REQUIRED",
    "public_github_push_allowed_from_windows": False,
    "export_kind": "SQUASHED_FINAL_TREE_DIFF",
    "intermediate_commit_metadata_included": False,
    "patch_apply_check": "PASS",
    "sanitized_baseline_commit": sys.argv[2],
    "windows_working_branch": sys.argv[3],
    "windows_head_commit": sys.argv[4],
    "changed_path_count": scope["changed_path_count"],
    "changed_existing_file_bytes": scope["existing_file_bytes"],
    "target_tree": sys.argv[5],
    "payload_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
}
(root / "receipt.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
(
  cd "$attempt_root"
  sha256sum PATCH_PAYLOAD.SHA256SUMS receipt.json > PATCH_TRANSFER.SHA256SUMS
)
staged_patch_hash="$stage_root/${attempt_id}.PATCH.SHA256"
(
  cd "$stage_root"
  sha256sum "$attempt_id/PATCH_TRANSFER.SHA256SUMS" > "$staged_patch_hash"
  sha256sum -c "$(basename "$staged_patch_hash")"
)
chmod -R a-w "$attempt_root"
mv "$attempt_root" "$final_attempt"
published_attempt=1
mv "$staged_patch_hash" "$external_patch_hash"
published_hash=1
rmdir "$stage_root"
stage_root=""
trap - EXIT INT TERM
printf 'CODE_PATCH_EXPORT_PASS path=%s checksum=%s\n' \
  "$final_attempt" "$external_patch_hash"
