#!/usr/bin/env bash
# Build a small owner-mode addendum: full Git history plus complete small sample data.
set -euo pipefail
umask 077

workspace_input="${1:?usage: build_windows_owner_handoff.sh WORKSPACE_ROOT OUTPUT_PARENT}"
output_input="${2:?usage: build_windows_owner_handoff.sh WORKSPACE_ROOT OUTPUT_PARENT}"
bundle_name="WINDOWS_SINGLE_OWNER_HANDOFF_20260831_V1"

workspace_root="$(cd "$workspace_input" && pwd -P)"
repo_root="$workspace_root/GLP_"
source_root="$repo_root/boltzgen/main/windows_single_owner_20260831"

for command_name in git python3 tar zstd shasum sha256sum find sort xargs; do
  command -v "$command_name" >/dev/null || {
    printf 'missing build command: %s\n' "$command_name" >&2
    exit 69
  }
done
output_parent="$(python3 -I - "$output_input" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
)"
case "$output_parent" in
  "$repo_root"|"$repo_root"/*|\
  "$workspace_root/shared"|"$workspace_root/shared"/*|\
  "$workspace_root/boltzgen"|"$workspace_root/boltzgen"/*|\
  "$workspace_root/data"|"$workspace_root/data"/*)
    printf 'output parent overlaps source or runtime data: %s\n' "$output_parent" >&2
    exit 64
    ;;
esac
mkdir -p "$output_parent"
output_parent="$(cd "$output_parent" && pwd -P)"
final_root="$output_parent/$bundle_name"

test -d "$repo_root/.git" && test -f "$source_root/README.md"
test -z "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)" || {
  printf 'commit source changes before building the owner handoff\n' >&2
  exit 74
}
test ! -e "$final_root" || {
  printf 'refusing to overwrite existing package: %s\n' "$final_root" >&2
  exit 73
}

data_paths=(
  shared/data/glp1_positive_conformer_ensemble_20260819
  shared/data/glp2_tuning_countertargets_20260824
  shared/data/peptide_lockbox_countertargets_20260823
  shared/data/vhh_challenger_scaffolds_20260823
)
for relative in "${data_paths[@]}"; do
  test -d "$workspace_root/$relative" || {
    printf 'missing sample data directory: %s\n' "$relative" >&2
    exit 66
  }
done

build_root="$(mktemp -d "$output_parent/.${bundle_name}.build.XXXXXX")"
package_root="$build_root/$bundle_name"
cleanup() {
  local exit_code="$?"
  trap - EXIT INT TERM
  if [ -d "$build_root" ]; then rm -rf "$build_root"; fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM
mkdir -p "$package_root/git" "$package_root/data" "$package_root/docs"

cp "$source_root/README.md" "$package_root/README_FIRST_ZH.md"
cp "$source_root/WINDOWS_OWNER_DIRECTIVE_ZH.md" "$package_root/docs/"
cp "$source_root/WINDOWS_OWNER_TASKS_ZH.md" "$package_root/docs/"
cp "$source_root/GLOSSARY_ZH.md" "$package_root/docs/"
cp "$source_root/WORKSPACE_OWNER_AGENTS.md" "$package_root/docs/"
cp "$source_root/owner_profile.json" "$package_root/"
cp "$source_root/scripts/adopt_windows_owner_mode.sh" "$package_root/ADOPT_IN_WSL.sh"
chmod 0755 "$package_root/ADOPT_IN_WSL.sh"

git -C "$repo_root" bundle create "$package_root/git/GLP_full.bundle" --all
git -C "$repo_root" bundle verify "$package_root/git/GLP_full.bundle" >/dev/null

export COPYFILE_DISABLE=1
(
  cd "$workspace_root"
  find "${data_paths[@]}" -type f ! -name '.DS_Store' -print0 \
    | sort -z | xargs -0 shasum -a 256
) > "$package_root/data/SHARED_DATA.SHA256SUMS"
(
  cd "$workspace_root"
  tar --no-xattrs --no-acls --no-fflags --exclude='.DS_Store' \
    -cf - "${data_paths[@]}" \
    | zstd -T0 -5 -q -o "$package_root/data/shared_data_complete.tar.zst"
)

verify_root="$build_root/verify"
mkdir "$verify_root"
zstd -dc "$package_root/data/shared_data_complete.tar.zst" | tar -xf - -C "$verify_root"
( cd "$verify_root" && sha256sum -c "$package_root/data/SHARED_DATA.SHA256SUMS" )

project_commit="$(git -C "$repo_root" rev-parse HEAD)"
project_branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD)"
python3 -I - "$package_root/OWNER_HANDOFF.json" "$project_commit" "$project_branch" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

payload = {
    "schema_version": "WINDOWS_SINGLE_OWNER_HANDOFF_V1",
    "status": "READY",
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "source_project_commit": sys.argv[2],
    "source_project_branch": sys.argv[3],
    "full_git_history_included": True,
    "small_shared_data_included": True,
    "existing_gpu_runtime_assets_reused": True,
    "mac_review_required": False,
    "environment_contract_required": False,
    "direct_windows_git_allowed": True,
    "credentials_included": False,
    "model_training_allowed": False,
    "purpose": "MIGRATE_EXISTING_WINDOWS_T7_WORKSPACE_TO_SINGLE_OWNER_MODE",
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

(
  cd "$package_root"
  find . -type f ! -name PACKAGE.SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum
) > "$package_root/PACKAGE.SHA256SUMS"
( cd "$package_root" && sha256sum -c PACKAGE.SHA256SUMS )

rm -rf "$verify_root"
mv "$package_root" "$final_root"
rmdir "$build_root"
build_root=""
trap - EXIT INT TERM
printf 'WINDOWS_SINGLE_OWNER_HANDOFF_READY path=%s\n' "$final_root"
