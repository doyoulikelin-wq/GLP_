#!/usr/bin/env bash
# Build and fully revalidate the portable Windows/WSL2 GPU engineering handoff.
set -euo pipefail
umask 077

workspace_input="${1:?usage: build_handoff_bundle.sh WORKSPACE_ROOT OUTPUT_PARENT}"
output_input="${2:?usage: build_handoff_bundle.sh WORKSPACE_ROOT OUTPUT_PARENT}"
bundle_name="WINDOWS_CODEX_GPU_HANDOFF_20260829_V1"
upstream_commit="31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0"
handoff_rel="boltzgen/main/windows_gpu_handoff_20260829"

workspace_root="$(cd "$workspace_input" && pwd -P)"
repo_root="$workspace_root/GLP_"
source_root="$repo_root/$handoff_rel"
upstream_root="$workspace_root/boltzgen/runs/nanobody_mps_smoke_20260819/vendor/boltzgen_v0.3.2"

for command_name in git python3 tar zstd split shasum find sort xargs awk; do
  command -v "$command_name" >/dev/null || {
    printf 'missing build command: %s\n' "$command_name" >&2
    exit 69
  }
done

test -d "$repo_root/.git" || {
  printf 'project repository missing: %s\n' "$repo_root" >&2
  exit 66
}
test -f "$source_root/README.md"
test -d "$upstream_root/.git"

if [ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)" ]; then
  printf 'repository must be clean and committed before packaging\n' >&2
  git -C "$repo_root" status --short >&2
  exit 74
fi

project_commit="$(git -C "$repo_root" rev-parse HEAD)"
project_branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD)" || {
  printf 'project repository must be on a named branch\n' >&2
  exit 74
}
project_origin="$(git -C "$repo_root" remote get-url origin)"
case "$project_origin" in
  https://github.com/doyoulikelin-wq/GLP_.git|git@github.com:doyoulikelin-wq/GLP_.git) ;;
  *)
    printf 'unexpected or credential-bearing project origin: %s\n' "$project_origin" >&2
    exit 74
    ;;
esac
tracking_ref="$(git -C "$repo_root" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" || {
  printf 'current project branch has no upstream tracking ref\n' >&2
  exit 74
}
test "$tracking_ref" = "origin/$project_branch" || {
  printf 'unexpected tracking ref: %s\n' "$tracking_ref" >&2
  exit 74
}
test "$(git -C "$repo_root" rev-parse '@{upstream}')" = "$project_commit" || {
  printf 'project HEAD is not synchronized with its origin tracking ref\n' >&2
  exit 74
}
export GIT_TERMINAL_PROMPT=0
remote_project_commit="$(
  git ls-remote --exit-code "$project_origin" "refs/heads/$project_branch" \
    | awk 'NR == 1 {print $1}'
)" || {
  printf 'cannot verify the current branch on GitHub\n' >&2
  exit 69
}
test "$remote_project_commit" = "$project_commit" || {
  printf 'GitHub branch SHA differs from local HEAD: remote=%s local=%s\n' \
    "$remote_project_commit" "$project_commit" >&2
  exit 74
}
test "$(git -C "$upstream_root" rev-parse "$upstream_commit^{commit}")" = "$upstream_commit"
test -z "$(git -C "$upstream_root" status --porcelain=v1 --untracked-files=all)" || {
  printf 'upstream BoltzGen snapshot is dirty\n' >&2
  exit 74
}

output_parent="$(mkdir -p "$output_input" && cd "$output_input" && pwd -P)"
case "$output_parent" in
  "$repo_root"|"$repo_root"/*|"$workspace_root/boltzgen"|"$workspace_root/boltzgen"/*|\
  "$workspace_root/shared"|"$workspace_root/shared"/*)
    printf 'OUTPUT_PARENT overlaps project sources or active data: %s\n' "$output_parent" >&2
    exit 64
    ;;
esac
final_root="$output_parent/$bundle_name"
external_transfer_hash="$output_parent/${bundle_name}.TRANSFER.SHA256"
test ! -e "$final_root" || {
  printf 'refusing to overwrite existing handoff: %s\n' "$final_root" >&2
  exit 73
}
test ! -e "$external_transfer_hash" || {
  printf 'refusing to overwrite existing transfer hash: %s\n' "$external_transfer_hash" >&2
  exit 73
}

build_parent="$(mktemp -d "$output_parent/.${bundle_name}.build.XXXXXX")"
package_root="$build_parent/$bundle_name"
verify_root=""
published_final=0
published_sidecar=0
cleanup() {
  local exit_code="$?"
  trap - EXIT INT TERM
  if [ "$published_sidecar" -eq 1 ] && [ -f "$external_transfer_hash" ]; then
    rm -f -- "$external_transfer_hash"
  fi
  if [ "$published_final" -eq 1 ] && [ -d "$final_root" ]; then
    rm -rf -- "$final_root"
  fi
  if [ -n "${verify_root:-}" ] && [ -d "$verify_root" ]; then
    rm -rf -- "$verify_root"
  fi
  if [ -d "$build_parent" ]; then
    rm -rf -- "$build_parent"
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p \
  "$package_root/archives" \
  "$package_root/git" \
  "$package_root/manifests" \
  "$package_root/manifests/contracts" \
  "$package_root/scripts/windows" \
  "$package_root/scripts/wsl"

build_log="$package_root/manifests/build_log.txt"
log_step() {
  printf '%s\t%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$1" >> "$build_log"
}
log_step "BUILD_START"

cp "$source_root/README.md" "$package_root/README_FIRST_ZH.md"
cp "$source_root/TASKS_FOR_WINDOWS_CODEX.md" "$package_root/"
cp "$source_root/GLOSSARY_ZH.md" "$package_root/"
cp "$source_root/WINDOWS_CODEX_START_PROMPT.md" "$package_root/"
cp "$source_root/ENGINEERING_EXPERIENCE_EVENT_SCHEMA.json" "$package_root/"
cp "$source_root/expected_manifests/"*.SHA256SUMS \
  "$package_root/manifests/contracts/"
cp "$source_root/scripts/"*.py "$package_root/scripts/"
cp "$source_root/scripts/windows/collect_windows_host.ps1" "$package_root/scripts/windows/"
cp "$source_root/scripts/wsl/"*.sh "$package_root/scripts/wsl/"
chmod 0755 "$package_root/scripts/"*.py "$package_root/scripts/wsl/"*.sh

python3 -I "$source_root/scripts/validate_handoff_sources.py" \
  --workspace-root "$workspace_root" \
  --repo-root "$repo_root" \
  --output-dir "$package_root/manifests/source_validation"
log_step "SOURCE_VALIDATION_PASS"

python3 -I - "$package_root/TASK_ORDER.tsv" <<'PY'
import csv
import sys
from pathlib import Path

rows = [
    ("T0", "校验并在 WSL2 /home 中解包", "TRANSFER_AND_SOURCE_VALIDATION_PASS"),
    ("T1", "采集 Windows 与 WSL2 GPU 只读探针", "ENGINEERING_GPU_PROBE_PASS"),
    ("T2", "构建 CUDA 12.8 / Blackwell 候选环境", "ENGINEERING_COMPATIBILITY_ONLY"),
    ("T3", "补齐本地单 GPU 生产代码与测试", "CODE_AND_TEST_RECEIPTS_COMPLETE"),
    ("T4", "生成并验证 12/12 设计规格", "SPEC_GATE_PASS"),
    ("T5", "7XL0 单候选 batch1 工程冒烟", "ENGINEERING_SMOKE_PASS_NOT_G2"),
    ("T6", "6XYM 单 checkpoint batch1 显存探针", "ENGINEERING_MEMORY_PROBE_ONLY"),
    ("T7", "冻结环境合同修订并运行正式 G1", "G1_PASS_OR_BLOCKED_WITH_EVIDENCE"),
    ("T8", "满足原始三单元后才尝试正式 G2", "G2_PASS_OR_BLOCKED_WITH_EVIDENCE"),
    ("T9", "G2 通过后只发布 7XL0 的 10 个 anchor", "G2_ANCHOR_RELEASE_PASS"),
    ("T10", "实现六组 AIV1 单元并运行 attempt_005 最终预检", "READY_FOR_FORMAL_AIV1_INPUT_VALIDATION"),
    ("T11", "执行 160/800 正式 AIV1 并交接经验库", "AIV1_HANDOFF_PASS_OR_BLOCKED_WITH_EVIDENCE"),
]
with Path(sys.argv[1]).open("x", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
    writer.writerow(("task_order", "task_id", "task", "allowed_terminal_state"))
    for order, (task_id, task, state) in enumerate(rows):
        writer.writerow((order, task_id, task, state))
PY

sanitized_branch="handoff/windows-gpu-sanitized-20260829"
sanitized_repo="$build_parent/sanitized_repo"
mkdir "$sanitized_repo"
git -C "$repo_root" archive --format=tar "$project_commit" -- \
  .gitattributes .gitignore CONTRIBUTING.md DATA_POLICY.md README.md \
  SECURITY.md THIRD_PARTY_NOTICES.md boltzgen \
  | tar -xf - -C "$sanitized_repo"

python3 -I - "$sanitized_repo" "$package_root/manifests/sanitized_source_paths.txt" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
output = Path(sys.argv[2])
forbidden_roots = {"bindcraft", "private", ".github"}
forbidden_structure_markers = ("7DTY", "6LMK", "7LLY", "GIP", "GLUCAGON", "OXYNTOMODULIN")
paths = []
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"sanitized source symlink forbidden: {path}")
    if not path.is_file():
        continue
    relative = path.relative_to(root).as_posix()
    if "\n" in relative or "\r" in relative:
        raise SystemExit(f"newline forbidden in sanitized source path: {relative!r}")
    if relative.split("/", 1)[0].lower() in forbidden_roots:
        raise SystemExit(f"forbidden source root included: {relative}")
    upper = relative.upper()
    if path.suffix.lower() in {".pdb", ".cif", ".mmcif"} and any(
        marker in upper for marker in forbidden_structure_markers
    ):
        raise SystemExit(f"lockbox-like structure path included: {relative}")
    paths.append(relative)
if not paths or not any(path.startswith("boltzgen/main/") for path in paths):
    raise SystemExit("sanitized BoltzGen source is empty")
output.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
PY

git -C "$sanitized_repo" init -q -b "$sanitized_branch"
git -C "$sanitized_repo" config user.name "Codex GPU Handoff Builder"
git -C "$sanitized_repo" config user.email "noreply@local.invalid"
git -C "$sanitized_repo" add --all
source_commit_date="$(git -C "$repo_root" show -s --format=%cI "$project_commit")"
GIT_AUTHOR_DATE="$source_commit_date" GIT_COMMITTER_DATE="$source_commit_date" \
  git -C "$sanitized_repo" -c commit.gpgsign=false commit -q \
  -m "Windows GPU sanitized handoff baseline" \
  -m "Source-project-commit: $project_commit" \
  -m "Lockbox-structure-bytes-included: false"
sanitized_commit="$(git -C "$sanitized_repo" rev-parse HEAD)"

git -C "$sanitized_repo" bundle create \
  "$package_root/git/GLP_sanitized.bundle" "refs/heads/$sanitized_branch"
git -C "$sanitized_repo" bundle verify "$package_root/git/GLP_sanitized.bundle" \
  >/dev/null 2>&1
printf 'status\tPASS\nbaseline_branch\t%s\nbaseline_commit\t%s\nsource_project_commit\t%s\nlockbox_structure_bytes_included\tfalse\n' \
  "$sanitized_branch" "$sanitized_commit" "$project_commit" \
  > "$package_root/manifests/git_bundle_verify.txt"
log_step "SANITIZED_GIT_BUNDLE_PASS"

export COPYFILE_DISABLE=1
git -C "$sanitized_repo" archive --format=tar --prefix=GLP_/ "$sanitized_commit" \
  | zstd -T0 -3 -q -o "$package_root/archives/00_project_sanitized_HEAD.tar.zst"
git -C "$upstream_root" archive --format=tar --prefix=software/boltzgen/ "$upstream_commit" \
  | zstd -T0 -3 -q -o "$package_root/archives/00_boltzgen_upstream_v0.3.2.tar.zst"
log_step "SOURCE_ARCHIVES_PASS"

# The offline bundle and source archive are complete; do not carry the temporary
# repository into publication or leave it behind under build_parent.
rm -rf -- "$sanitized_repo"
sanitized_repo=""

python3 -I - \
  "$package_root/HANDOFF_STATUS.json" \
  "$package_root/git/github_identity.json" \
  "$project_commit" "$project_branch" "$upstream_commit" \
  "$sanitized_commit" "$sanitized_branch" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

status_path, github_path = map(Path, sys.argv[1:3])
project_commit, project_branch, upstream_commit, sanitized_commit, sanitized_branch = sys.argv[3:]
generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status = {
    "schema_version": "WINDOWS_CODEX_GPU_HANDOFF_STATUS_V1",
    "package_id": "WINDOWS_CODEX_GPU_HANDOFF_20260829_V1",
    "generated_at_utc": generated,
    "handoff_status": "ENGINEERING_HANDOFF_READY",
    "project_git": {
        "branch": project_branch,
        "commit": project_commit,
        "public_origin": "https://github.com/doyoulikelin-wq/GLP_.git",
    },
    "handoff_git": {
        "branch": sanitized_branch,
        "commit": sanitized_commit,
        "history_kind": "SANITIZED_SINGLE_ROOT_COMMIT",
        "public_origin_configured": False,
        "return_mode": "SQUASHED_FINAL_TREE_DIFF_TO_MAC_CODEX",
    },
    "boltzgen_upstream": {"version": "0.3.2", "commit": upstream_commit},
    "formal_status": {"AIV0": "PASS", "G1": "NOT_RUN", "G2": "NOT_RUN", "AIV1": "NOT_RUN"},
    "aiv0_authoritative_attempt": "aiv0_asset_validation/attempt_007",
    "model_training_allowed": False,
    "lockbox_structure_files_included": False,
    "lockbox_identity_metadata_included": True,
    "candidate_lockbox_results_included": False,
    "lockbox_access_count": 0,
    "credentials_included": False,
    "full_public_git_history_included": False,
    "tracked_mac_mps_reference_code_and_summaries_included": True,
    "runtime_asset_count": 5,
    "runtime_asset_bytes": 6352944053,
    "aiv1_open_development_state_count": 16,
    "selected_baseline_scaffold_count": 12,
    "known_missing_before_formal_gpu_work": [
        "versioned CUDA 12.8 environment contract and formal G1 receipt",
        "local single-GPU production runtime and tests",
        "12/12 frozen design specs and spec gate receipt",
        "three original G2 cell receipts and released anchors",
        "six formal AIV1 implementation units and result registry",
    ],
    "excluded": [
        "GIP and glucagon lockbox structure files and candidate-by-lockbox results",
        "private directory and credentials",
        "Mac ARM/MPS environment, vendor, and complete run outputs",
        "SAbDab raw snapshot and scaffold SQLite database",
        "model training tasks",
    ],
}
github = {
    "schema_version": "GITHUB_HANDOFF_IDENTITY_V1",
    "repository": "doyoulikelin-wq/GLP_",
    "url": "https://github.com/doyoulikelin-wq/GLP_",
    "visibility": "public",
    "source_branch": project_branch,
    "source_commit": project_commit,
    "offline_git_bundle": "git/GLP_sanitized.bundle",
    "offline_baseline_branch": sanitized_branch,
    "offline_baseline_commit": sanitized_commit,
    "offline_history_kind": "SANITIZED_SINGLE_ROOT_COMMIT",
    "credential_included": False,
    "windows_branch_pattern": "codex/windows-gpu-<YYYYMMDD>",
    "windows_fetch_from_public_origin_allowed": False,
    "integration_mode": "Windows exports a scanned squashed final-tree diff; Mac Codex reviews and pushes",
    "large_assets_must_not_be_pushed": True,
}
status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
github_path.write_text(json.dumps(github, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

runtime_rel="boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819"
runtime_list="$package_root/manifests/runtime_included_paths.txt"
python3 -I - "$workspace_root" "$runtime_rel" \
  "$runtime_list" "$package_root/manifests/runtime_tree.SHA256SUMS" <<'PY'
import hashlib
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve(strict=True)
root = workspace / sys.argv[2]
list_path = Path(sys.argv[3])
output = Path(sys.argv[4])

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

rows = []
included = []
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"runtime symlink forbidden: {path}")
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
        relative = path.relative_to(workspace).as_posix()
        if "\n" in relative or "\r" in relative:
            raise SystemExit(f"newline forbidden in runtime path: {relative!r}")
        included.append(relative)
        rows.append(f"{digest(path)}  {relative}\n")
total_bytes = sum((workspace / relative).stat().st_size for relative in included)
if len(rows) != 61 or total_bytes != 6_359_261_172:
    raise SystemExit(
        f"expected 61 runtime package files/6359261172 bytes, observed {len(rows)}/{total_bytes}"
    )
list_path.write_text("".join(f"{relative}\n" for relative in included), encoding="utf-8")
output.write_text("".join(rows), encoding="utf-8")
PY
cmp \
  "$source_root/expected_manifests/runtime_tree.SHA256SUMS" \
  "$package_root/manifests/runtime_tree.SHA256SUMS"

tar --uid 0 --gid 0 --uname root --gname root \
  -C "$workspace_root" -cf - -T "$runtime_list" \
  | zstd -T0 -1 -q \
  | split -b 1900m -a 3 - "$package_root/archives/01_gpu_runtime_assets.tar.zst.part-"
log_step "GPU_RUNTIME_ARCHIVE_PASS"

active_list="$package_root/manifests/active_included_paths.txt"
python3 -I - "$workspace_root" "$active_list" \
  "$package_root/manifests/active_data_tree.SHA256SUMS" <<'PY'
import hashlib
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve(strict=True)
list_path = Path(sys.argv[2])
manifest_path = Path(sys.argv[3])

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

roots = [
    "boltzgen/data/vhh_scaffold_database_20260819/selected",
    "boltzgen/runs/old12_glp1_mac_enhanced_20260820/configs",
    "boltzgen/runs/old12_glp1_mac_enhanced_20260820/inputs",
    "shared/data/glp1_positive_conformer_ensemble_20260819",
    "shared/data/glp2_tuning_countertargets_20260824",
    "boltzgen/data/ai_structure_asset_validation_registry_20260828_211504",
    "boltzgen/runs/glp1_vhh_formal_campaign_20260828",
    "boltzgen/runs/glp1_vhh_aiv1_preflight_20260828",
]
singles = [
    "boltzgen/data/vhh_scaffold_database_20260819/README.md",
    "boltzgen/data/vhh_scaffold_database_20260819/criteria/scaffold_screening_v1.json",
    "boltzgen/data/vhh_scaffold_database_20260819/registry/selected_scaffolds.tsv",
    "boltzgen/data/vhh_scaffold_database_20260819/registry/export_artifacts.tsv",
    "boltzgen/data/vhh_scaffold_database_20260819/registry/boltzgen_export_validation.tsv",
    "boltzgen/data/vhh_scaffold_database_20260819/registry/database_summary.json",
    "boltzgen/runs/old12_glp1_mac_enhanced_20260820/README.md",
]
paths = set()
for relative in roots:
    root = workspace / relative
    if not root.is_dir() or root.is_symlink():
        raise SystemExit(f"invalid active-data root: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"active-data symlink forbidden: {path}")
        if path.is_file():
            paths.add(path.relative_to(workspace).as_posix())
for relative in singles:
    path = workspace / relative
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"invalid active-data file: {path}")
    paths.add(relative)
for relative in paths:
    if "\n" in relative or "\r" in relative:
        raise SystemExit(f"newline forbidden in active-data path: {relative!r}")
    upper = relative.upper()
    if "PEPTIDE_LOCKBOX_COUNTERTARGETS" in upper or "/PRIVATE/" in f"/{upper}/":
        raise SystemExit(f"forbidden path selected: {relative}")
ordered = sorted(paths)
total_bytes = sum((workspace / relative).stat().st_size for relative in ordered)
if len(ordered) != 474 or total_bytes != 18_143_045:
    raise SystemExit(
        f"active-data identity changed: files={len(ordered)}, bytes={total_bytes}"
    )
list_path.write_text("".join(f"{path}\n" for path in ordered), encoding="utf-8")
with manifest_path.open("x", encoding="utf-8") as handle:
    for relative in ordered:
        handle.write(f"{digest(workspace / relative)}  {relative}\n")
PY
cmp \
  "$source_root/expected_manifests/active_data_tree.SHA256SUMS" \
  "$package_root/manifests/active_data_tree.SHA256SUMS"

tar --uid 0 --gid 0 --uname root --gname root \
  -C "$workspace_root" -cf - -T "$active_list" \
  | zstd -T0 -3 -q -o "$package_root/archives/02_active_project_data.tar.zst"
log_step "ACTIVE_DATA_ARCHIVE_PASS"

cat "$package_root/archives/01_gpu_runtime_assets.tar.zst.part-"* \
  | shasum -a 256 \
  | awk '{print $1 "  concatenated:archives/01_gpu_runtime_assets.tar.zst"}' \
  > "$package_root/manifests/runtime_archive_stream.SHA256SUMS"

for archive in \
  "$package_root/archives/00_project_sanitized_HEAD.tar.zst" \
  "$package_root/archives/00_boltzgen_upstream_v0.3.2.tar.zst" \
  "$package_root/archives/02_active_project_data.tar.zst"; do
  zstd -q -t "$archive"
done
cat "$package_root/archives/01_gpu_runtime_assets.tar.zst.part-"* | zstd -q -t
log_step "COMPRESSED_STREAM_TEST_PASS"

verify_root="$(mktemp -d "$output_parent/.${bundle_name}.verify.XXXXXX")"
zstd -dc "$package_root/archives/00_project_sanitized_HEAD.tar.zst" | tar -xf - -C "$verify_root"
zstd -dc "$package_root/archives/00_boltzgen_upstream_v0.3.2.tar.zst" | tar -xf - -C "$verify_root"
zstd -dc "$package_root/archives/02_active_project_data.tar.zst" | tar -xf - -C "$verify_root"
cat "$package_root/archives/01_gpu_runtime_assets.tar.zst.part-"* | zstd -dc | tar -xf - -C "$verify_root"

( cd "$verify_root" && shasum -a 256 -c "$package_root/manifests/runtime_tree.SHA256SUMS" ) \
  > "$package_root/manifests/runtime_tree_reverify.txt"
( cd "$verify_root" && shasum -a 256 -c "$package_root/manifests/active_data_tree.SHA256SUMS" ) \
  > "$package_root/manifests/active_data_tree_reverify.txt"
( cd "$verify_root" && shasum -a 256 -c \
    "$package_root/manifests/contracts/boltzgen_upstream_tree.SHA256SUMS" ) \
  > "$package_root/manifests/boltzgen_upstream_tree_reverify.txt"
python3 -I "$package_root/scripts/validate_handoff_sources.py" \
  --workspace-root "$verify_root" \
  --repo-root "$verify_root/GLP_" \
  --output-dir "$verify_root/.source_revalidation"
cmp \
  "$package_root/manifests/source_validation/source_validation.json" \
  "$verify_root/.source_revalidation/source_validation.json"
python3 -I - \
  "$verify_root" \
  "$package_root/manifests/source_validation/lockbox_structure_sha256_denylist.tsv" \
  "$package_root/manifests/lockbox_payload_denylist_scan.json" <<'PY'
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
denylist_path = Path(sys.argv[2]).resolve(strict=True)
output = Path(sys.argv[3])
with denylist_path.open("r", encoding="utf-8", newline="") as handle:
    denylist = {row["sha256"] for row in csv.DictReader(handle, delimiter="\t")}
if len(denylist) != 25:
    raise SystemExit(f"expected 25 denylisted structure hashes, observed {len(denylist)}")

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

scanned = 0
matched = []
credential_matches = []
secret_patterns = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"unexpected symlink in extracted payload: {path}")
    if path.is_file():
        scanned += 1
        value = digest(path)
        if value in denylist:
            matched.append({"relative_path": path.relative_to(root).as_posix(), "sha256": value})
        if path.stat().st_size <= 20 * 1024 * 1024:
            raw = path.read_bytes()
            if any(pattern.search(raw) for pattern in secret_patterns):
                credential_matches.append(path.relative_to(root).as_posix())
if matched:
    raise SystemExit(f"denylisted lockbox structure bytes found in payload: {matched}")
if credential_matches:
    raise SystemExit(f"credential-like content found in payload paths: {credential_matches}")
payload = {
    "schema_version": "LOCKBOX_PAYLOAD_DENYLIST_SCAN_V1",
    "status": "PASS",
    "denylist_hash_count": len(denylist),
    "payload_file_count_scanned": scanned,
    "matching_structure_file_count": 0,
    "credential_like_match_count": 0,
    "lockbox_identity_metadata_included": True,
    "candidate_lockbox_results_included": False,
}
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
test ! -e "$verify_root/private"
test ! -e "$verify_root/shared/data/peptide_lockbox_countertargets_20260823"
test ! -e "$verify_root/data/样本数据/not_binding"
test ! -L "$verify_root/data/样本数据/not_binding"
test ! -e "$verify_root/boltzgen/runs/nanobody_mps_smoke_20260819"
test ! -e "$verify_root/boltzgen/runs/old12_glp1_mac_enhanced_20260820/vendor"
test ! -e "$verify_root/GLP_/bindcraft"
test ! -e "$verify_root/GLP_/.github"
log_step "FULL_EXTRACT_REVALIDATION_PASS"

rm -rf -- "$verify_root"
verify_root=""
log_step "FINAL_MANIFEST_GENERATION_START"

python3 -I - "$package_root" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

excluded = {"PAYLOAD.SHA256SUMS", "PACKAGE_RECEIPT.json", "TRANSFER.SHA256SUMS"}
payload_files = sorted(
    path for path in root.rglob("*") if path.is_file() and path.name not in excluded
)
with (root / "PAYLOAD.SHA256SUMS").open("x", encoding="utf-8") as handle:
    for path in payload_files:
        handle.write(f"{digest(path)}  {path.relative_to(root).as_posix()}\n")

parts = sorted((root / "archives").glob("01_gpu_runtime_assets.tar.zst.part-*"))
receipt = {
    "schema_version": "WINDOWS_CODEX_GPU_HANDOFF_PACKAGE_RECEIPT_V1",
    "package_id": root.name,
    "status": "ENGINEERING_HANDOFF_READY",
    "full_extract_revalidation": "PASS",
    "payload_file_count": len(payload_files),
    "payload_bytes": sum(path.stat().st_size for path in payload_files),
    "runtime_archive_part_count": len(parts),
    "runtime_archive_part_max_bytes": max(path.stat().st_size for path in parts),
    "payload_manifest_sha256": digest(root / "PAYLOAD.SHA256SUMS"),
    "formal_g1": False,
    "formal_g2": False,
    "formal_aiv1": False,
    "lockbox_structure_files_included": False,
    "lockbox_identity_metadata_included": True,
    "candidate_lockbox_results_included": False,
    "lockbox_access_count": 0,
    "credentials_included": False,
    "full_public_git_history_included": False,
    "engineering_experience_staging_schema_included": True,
}
(root / "PACKAGE_RECEIPT.json").write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
with (root / "TRANSFER.SHA256SUMS").open("x", encoding="utf-8") as handle:
    for name in ("PAYLOAD.SHA256SUMS", "PACKAGE_RECEIPT.json"):
        handle.write(f"{digest(root / name)}  {name}\n")
PY

staged_transfer_hash="$build_parent/${bundle_name}.TRANSFER.SHA256"
( cd "$build_parent" && shasum -a 256 "$bundle_name/TRANSFER.SHA256SUMS" ) \
  > "$staged_transfer_hash"
( cd "$build_parent" && shasum -a 256 -c "$(basename "$staged_transfer_hash")" )
mv "$package_root" "$final_root"
published_final=1
mv "$staged_transfer_hash" "$external_transfer_hash"
published_sidecar=1
rmdir "$build_parent"
build_parent=""
trap - EXIT INT TERM

printf 'HANDOFF_BUILD_PASS path=%s\n' "$final_root"
