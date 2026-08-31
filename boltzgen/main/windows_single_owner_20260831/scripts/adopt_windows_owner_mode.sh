#!/usr/bin/env bash
# Migrate the existing sanitized Windows T7 workspace to the full source history.
set -euo pipefail
umask 077

workspace_input="${1:?usage: ADOPT_IN_WSL.sh WORKSPACE_ROOT}"
script_dir="$(cd "$(dirname "$0")" && pwd -P)"
if [ -f "$script_dir/OWNER_HANDOFF.json" ]; then
  package_root="$script_dir"
elif [ -f "$script_dir/../OWNER_HANDOFF.json" ]; then
  package_root="$(cd "$script_dir/.." && pwd -P)"
else
  printf 'cannot locate OWNER_HANDOFF.json beside the adoption script\n' >&2
  exit 66
fi

workspace_root="$(realpath "$workspace_input")"
current_repo="$workspace_root/GLP_"
marker_path="$workspace_root/WINDOWS_OWNER_MODE.json"
case "$workspace_root" in
  /home/*) ;;
  *) printf 'workspace must be under WSL /home: %s\n' "$workspace_root" >&2; exit 64 ;;
esac
test -d "$current_repo/.git" && test ! -L "$current_repo"
test ! -e "$marker_path" && test ! -L "$marker_path" || {
  printf 'owner marker already exists; refusing a second migration: %s\n' "$marker_path" >&2
  exit 73
}
for command_name in git python3 tar zstd sha256sum rsync realpath readlink; do
  command -v "$command_name" >/dev/null || {
    printf 'missing command: %s\n' "$command_name" >&2
    exit 69
  }
done

( cd "$package_root" && sha256sum -c PACKAGE.SHA256SUMS )
git -C "$current_repo" bundle verify "$package_root/git/GLP_full.bundle" >/dev/null
test -z "$(git -C "$current_repo" status --porcelain=v1 --untracked-files=all)" || {
  printf 'commit all Windows T3-T6 changes before migration; current repository is dirty\n' >&2
  git -C "$current_repo" status --short >&2
  exit 74
}

handoff_status="$workspace_root/handoff/HANDOFF_STATUS.json"
test -f "$handoff_status"
identity_text="$(python3 -I - "$handoff_status" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["handoff_git"]["commit"])
print(payload["project_git"]["commit"])
print(payload["project_git"]["branch"])
print(payload["project_git"]["public_origin"])
PY
)"
readarray -t identity <<< "$identity_text"
sanitized_base="${identity[0]}"
source_commit="${identity[1]}"
source_branch="${identity[2]}"
public_origin="${identity[3]}"

owner_handoff_commit="$(python3 -I - "$package_root/OWNER_HANDOFF.json" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["source_project_commit"])
PY
)"
git -C "$current_repo" bundle list-heads "$package_root/git/GLP_full.bundle" >/dev/null
git -C "$current_repo" merge-base --is-ancestor "$sanitized_base" HEAD

timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
backup_root="$workspace_root/windows_owner_migration_backups/$timestamp"
stage_root="$workspace_root/.windows_owner_migration_$timestamp"
backup_repo="$backup_root/GLP_sanitized_before_owner_mode"
failed_owner_repo="$backup_root/GLP_owner_candidate_failed"
patch_path="$backup_root/windows_T3_T6_changes.patch"
report_path="$backup_root/migration_report.txt"
data_new_paths="$backup_root/data_new_paths.txt"
test ! -e "$backup_root" && test ! -e "$stage_root"
mkdir -p "$backup_root" "$stage_root"

migration_complete=0
repo_move_started=0
repo_swap_complete=0
link_paths=()
link_kinds=()
link_values=()
published_paths=()
published_kinds=()
published_backups=()

rollback_migration() {
  local exit_code="$?"
  if [ "$migration_complete" -eq 1 ] && [ "$exit_code" -eq 0 ]; then
    return 0
  fi
  trap - EXIT INT TERM
  set +e

  if [ -e "$marker_path" ] || [ -L "$marker_path" ]; then
    rm -f "$marker_path"
  fi

  local index path kind saved
  for ((index=${#published_paths[@]} - 1; index >= 0; index--)); do
    path="${published_paths[$index]}"
    kind="${published_kinds[$index]}"
    saved="${published_backups[$index]}"
    if [ -f "$path" ] || [ -L "$path" ]; then rm -f "$path"; fi
    if [ "$kind" = "MOVED" ] && { [ -e "$saved" ] || [ -L "$saved" ]; }; then
      mv "$saved" "$path"
    fi
  done

  for ((index=${#link_paths[@]} - 1; index >= 0; index--)); do
    path="${link_paths[$index]}"
    kind="${link_kinds[$index]}"
    saved="${link_values[$index]}"
    if [ -L "$path" ]; then rm "$path"; fi
    case "$kind" in
      SYMLINK) ln -s "$saved" "$path" ;;
      MOVED) if [ -e "$saved" ] || [ -L "$saved" ]; then mv "$saved" "$path"; fi ;;
      ABSENT) ;;
    esac
  done

  if [ "$repo_move_started" -eq 1 ]; then
    if [ -d "$current_repo/.git" ] && [ -d "$backup_repo/.git" ]; then
      mv "$current_repo" "$failed_owner_repo"
      mv "$backup_repo" "$current_repo"
    elif [ ! -e "$current_repo" ] && [ -d "$backup_repo/.git" ]; then
      mv "$backup_repo" "$current_repo"
    fi
  fi

  if [ -f "$data_new_paths" ]; then
    while IFS= read -r relative; do
      case "$relative" in
        shared/data/*)
          if [ -f "$workspace_root/$relative" ] || [ -L "$workspace_root/$relative" ]; then
            rm -f "$workspace_root/$relative"
          fi
          ;;
      esac
    done < "$data_new_paths"
  fi

  printf 'WINDOWS_OWNER_MIGRATION_FAILED exit=%s backup=%s stage=%s\n' \
    "$exit_code" "$backup_root" "$stage_root" >&2
  exit "$exit_code"
}
trap rollback_migration EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

git -C "$current_repo" bundle create "$backup_root/windows_sanitized_progress.bundle" --all
git -C "$current_repo" diff --binary --full-index "$sanitized_base"..HEAD > "$patch_path"
git -C "$current_repo" status --short --branch > "$backup_root/old_git_status.txt"
git_author_name="$(git -C "$current_repo" config --get user.name || true)"
git_author_email="$(git -C "$current_repo" config --get user.email || true)"
if [ -z "$git_author_name" ]; then
  git_author_name="$(git -C "$current_repo" log -1 --format='%an')"
fi
if [ -z "$git_author_email" ]; then
  git_author_email="$(git -C "$current_repo" log -1 --format='%ae')"
fi

git clone --no-local "$package_root/git/GLP_full.bundle" "$stage_root/GLP_" >/dev/null
git -C "$stage_root/GLP_" cat-file -e "$source_commit^{commit}"
git -C "$stage_root/GLP_" cat-file -e "$owner_handoff_commit^{commit}"
git -C "$stage_root/GLP_" merge-base --is-ancestor \
  "$source_commit" "$owner_handoff_commit"
git -C "$stage_root/GLP_" checkout --detach "$owner_handoff_commit" >/dev/null
if [ -s "$patch_path" ]; then
  git -C "$stage_root/GLP_" apply --3way "$patch_path"
fi

owner_branch="codex/windows-primary-$(date -u +'%Y%m%d-%H%M%S')"
git -C "$stage_root/GLP_" switch -c "$owner_branch" >/dev/null
git -C "$stage_root/GLP_" config core.autocrlf false
git -C "$stage_root/GLP_" config user.name "$git_author_name"
git -C "$stage_root/GLP_" config user.email "$git_author_email"
git -C "$stage_root/GLP_" remote remove origin
git -C "$stage_root/GLP_" remote add origin "$public_origin"
git -C "$stage_root/GLP_" add --all
if ! git -C "$stage_root/GLP_" diff --cached --quiet; then
  git -C "$stage_root/GLP_" -c commit.gpgsign=false commit -m \
    "Migrate Windows T3-T6 progress to primary workspace" >/dev/null
fi
test "$(git -C "$stage_root/GLP_" branch --show-current)" = "$owner_branch"
test "$(git -C "$stage_root/GLP_" remote get-url origin)" = "$public_origin"
test -f "$stage_root/GLP_/AGENTS.md"

mkdir -p "$stage_root/data_stage"
zstd -dc "$package_root/data/shared_data_complete.tar.zst" \
  | tar -xf - -C "$stage_root/data_stage"
( cd "$stage_root/data_stage" && sha256sum -c "$package_root/data/SHARED_DATA.SHA256SUMS" )

python3 -I - \
  "$workspace_root" \
  "$package_root/data/SHARED_DATA.SHA256SUMS" \
  "$backup_root/data_conflicts.tsv" \
  "$data_new_paths" <<'PY'
import hashlib
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
manifest = Path(sys.argv[2])
conflict_path = Path(sys.argv[3])
new_path_list = Path(sys.argv[4])
conflicts = []
new_paths = []
for line in manifest.read_text(encoding="utf-8").splitlines():
    expected, relative = line.split("  ", 1)
    if not relative.startswith("shared/data/") or ".." in Path(relative).parts:
        raise SystemExit(f"unsafe data manifest path: {relative}")
    destination = workspace / relative
    if destination.is_symlink():
        conflicts.append((relative, expected, "SYMLINK", "destination_is_symlink"))
    elif destination.exists():
        if not destination.is_file():
            conflicts.append((relative, expected, "NOT_A_FILE", "destination_not_regular_file"))
        else:
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual != expected:
                conflicts.append((relative, expected, actual, "sha256_mismatch"))
    else:
        new_paths.append(relative)

conflict_path.write_text(
    "relative_path\texpected_sha256\tactual\treason\n"
    + "".join("\t".join(row) + "\n" for row in conflicts),
    encoding="utf-8",
)
new_path_list.write_text("".join(path + "\n" for path in new_paths), encoding="utf-8")
if conflicts:
    raise SystemExit(f"shared data conflicts found; see {conflict_path}")
PY

mkdir -p "$backup_root/workspace_data_before_merge"
if [ -d "$workspace_root/shared/data" ]; then
  rsync -a "$workspace_root/shared/data/" \
    "$backup_root/workspace_data_before_merge/shared_data/"
fi
mkdir -p "$workspace_root/shared/data"
rsync -a --ignore-existing "$stage_root/data_stage/shared/data/" "$workspace_root/shared/data/"
( cd "$workspace_root" && sha256sum -c "$package_root/data/SHARED_DATA.SHA256SUMS" ) \
  > "$backup_root/workspace_data_postmerge_sha256.txt"

prepare_root="$stage_root/publish"
mkdir -p "$prepare_root"
cp "$package_root/docs/WORKSPACE_OWNER_AGENTS.md" "$prepare_root/AGENTS.md"
cp "$package_root/docs/WINDOWS_OWNER_DIRECTIVE_ZH.md" "$prepare_root/CURRENT_MODE.md"
cp "$package_root/docs/WINDOWS_OWNER_DIRECTIVE_ZH.md" "$prepare_root/OWNER_MODE_OVERRIDE.md"

python3 -I - "$prepare_root/WINDOWS_OWNER_MODE.json" \
  "$owner_branch" "$source_commit" "$package_root/OWNER_HANDOFF.json" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

handoff = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
payload = {
    "schema_version": "WINDOWS_OWNER_MODE_MARKER_V1",
    "status": "ACTIVE",
    "activated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "authority": "WINDOWS_CODEX",
    "mac_review_required": False,
    "environment_contract_required": False,
    "direct_git_allowed": True,
    "training_allowed": False,
    "model_weights_mutable": False,
    "owner_branch": sys.argv[2],
    "source_project_commit": sys.argv[3],
    "handoff_source_commit": handoff["source_project_commit"],
    "lockbox_present_locally": True,
    "lockbox_use_rule": "EVALUATION_ONLY_OR_RECLASSIFY_AS_DEVELOPMENT",
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

mkdir -p "$workspace_root/data/样本数据"
install_link() {
  local link_path="$1"
  local target_text="$2"
  local backup_label="$3"
  local kind="ABSENT"
  local saved=""
  if [ -L "$link_path" ]; then
    kind="SYMLINK"
    saved="$(readlink "$link_path")"
  elif [ -e "$link_path" ]; then
    kind="MOVED"
    saved="$backup_root/$backup_label.before_owner_mode"
  fi
  link_paths+=("$link_path")
  link_kinds+=("$kind")
  link_values+=("$saved")
  if [ "$kind" = "SYMLINK" ]; then
    rm "$link_path"
  elif [ "$kind" = "MOVED" ]; then
    mv "$link_path" "$saved"
  fi
  ln -s "$target_text" "$link_path"
}

publish_file() {
  local prepared="$1"
  local destination="$2"
  local backup_label="$3"
  local kind="ABSENT"
  local saved=""
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    kind="MOVED"
    saved="$backup_root/$backup_label.before_owner_mode"
    mv "$destination" "$saved"
  fi
  published_paths+=("$destination")
  published_kinds+=("$kind")
  published_backups+=("$saved")
  mv "$prepared" "$destination"
}

repo_move_started=1
mv "$current_repo" "$backup_repo"
mv "$stage_root/GLP_" "$current_repo"
repo_swap_complete=1
test "$(git -C "$current_repo" branch --show-current)" = "$owner_branch"
test "$(git -C "$current_repo" remote get-url origin)" = "$public_origin"

install_link "$workspace_root/data/样本数据/binding-多构象" \
  "../../shared/data/glp1_positive_conformer_ensemble_20260819" \
  "sample_data_binding_multi_conformer"
install_link "$workspace_root/data/样本数据/boltzgen_vhh_scaffolds" \
  "../../shared/data/vhh_challenger_scaffolds_20260823" \
  "sample_data_boltzgen_vhh_scaffolds"
install_link "$workspace_root/data/样本数据/not_binding" \
  "../../shared/data/peptide_lockbox_countertargets_20260823" \
  "sample_data_not_binding_lockbox"
install_link "$workspace_root/data/not_binding" \
  "../shared/data/glp2_tuning_countertargets_20260824" \
  "data_not_binding_glp2_tuning"

chmod u+w "$workspace_root/handoff"
publish_file "$prepare_root/AGENTS.md" "$workspace_root/AGENTS.md" \
  "workspace_root_AGENTS.md"
publish_file "$prepare_root/CURRENT_MODE.md" "$workspace_root/CURRENT_MODE.md" \
  "workspace_root_CURRENT_MODE.md"
publish_file "$prepare_root/OWNER_MODE_OVERRIDE.md" \
  "$workspace_root/handoff/OWNER_MODE_OVERRIDE.md" \
  "handoff_OWNER_MODE_OVERRIDE.md"

{
  printf 'WINDOWS_OWNER_MODE_ACTIVE\n'
  printf 'workspace=%s\n' "$workspace_root"
  printf 'new_repo=%s\n' "$current_repo"
  printf 'backup_repo=%s\n' "$backup_repo"
  printf 'owner_branch=%s\n' "$owner_branch"
  printf 'source_branch=%s\n' "$source_branch"
  printf 'original_sanitized_source_commit=%s\n' "$source_commit"
  printf 'owner_handoff_source_commit=%s\n' "$owner_handoff_commit"
  printf 'public_origin=%s\n' "$public_origin"
  printf 'windows_delta_patch=%s\n' "$patch_path"
  printf 'data_conflicts=%s\n' "$backup_root/data_conflicts.tsv"
} > "$report_path"

rm -rf "$stage_root/data_stage"
mv "$prepare_root/WINDOWS_OWNER_MODE.json" "$marker_path"
migration_complete=1
trap - EXIT INT TERM
rmdir "$prepare_root" "$stage_root" 2>/dev/null || true

cat "$report_path"
printf '\nNext:\n'
printf '  cd %q\n' "$current_repo"
printf '  bash boltzgen/main/windows_single_owner_20260831/scripts/verify_local_env.sh %q\n' \
  "$workspace_root"
printf '  git push -u origin %q\n' "$owner_branch"
printf 'If GitHub asks for credentials, sign in once on Windows; do not copy Mac tokens.\n'
