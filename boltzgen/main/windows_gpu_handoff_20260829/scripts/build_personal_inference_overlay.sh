#!/usr/bin/env bash
# Build the small personal inference entry overlay bound to an immutable V1 base package.
set -euo pipefail
umask 077

base_input="${1:?usage: build_personal_inference_overlay.sh BASE_BUNDLE OUTPUT_PARENT EXPECTED_BASE_TRANSFER_SHA256}"
output_input="${2:?usage: build_personal_inference_overlay.sh BASE_BUNDLE OUTPUT_PARENT EXPECTED_BASE_TRANSFER_SHA256}"
expected_base_transfer_sha256="${3:?usage: build_personal_inference_overlay.sh BASE_BUNDLE OUTPUT_PARENT EXPECTED_BASE_TRANSFER_SHA256}"

[[ "$expected_base_transfer_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'EXPECTED_BASE_TRANSFER_SHA256 must be 64 lowercase hexadecimal characters\n' >&2
  exit 64
}
for command_name in git python3 sha256sum shasum realpath; do
  command -v "$command_name" >/dev/null || {
    printf 'missing build command: %s\n' "$command_name" >&2
    exit 69
  }
done

script_dir="$(cd "$(dirname "$0")" && pwd -P)"
source_root="$(cd "$script_dir/.." && pwd -P)"
repo_root="$(git -C "$source_root" rev-parse --show-toplevel)"
test ! -L "$base_input" || {
  printf 'base package input must not be a symlink: %s\n' "$base_input" >&2
  exit 65
}
base_root="$(realpath "$base_input")"
base_name="$(basename "$base_root")"
expected_base_name="WINDOWS_CODEX_GPU_HANDOFF_20260829_V1"
test "$base_name" = "$expected_base_name" || {
  printf 'unexpected base package name: %s\n' "$base_name" >&2
  exit 64
}
test -d "$base_root" && test ! -L "$base_root"
for required_file in \
  "$base_root/TRANSFER.SHA256SUMS" \
  "$base_root/PAYLOAD.SHA256SUMS" \
  "$base_root/PACKAGE_RECEIPT.json" \
  "$base_root/HANDOFF_STATUS.json" \
  "${base_root}.TRANSFER.SHA256"; do
  test -f "$required_file" && test ! -L "$required_file" || {
    printf 'missing or symlinked base package file: %s\n' "$required_file" >&2
    exit 66
  }
done
case "$base_root" in
  "$repo_root"|"$repo_root"/*)
    printf 'base package must be outside the Git repository: %s\n' "$base_root" >&2
    exit 64
    ;;
esac

test -z "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)" || {
  printf 'repository must be clean and committed before overlay packaging\n' >&2
  git -C "$repo_root" status --short >&2
  exit 74
}
project_commit="$(git -C "$repo_root" rev-parse HEAD)"
project_branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD)"
test "$(git -C "$repo_root" rev-parse '@{upstream}')" = "$project_commit" || {
  printf 'project HEAD is not synchronized with its origin tracking ref\n' >&2
  exit 74
}

observed_base_transfer_sha256="$(sha256sum "$base_root/TRANSFER.SHA256SUMS" | awk '{print $1}')"
test "$observed_base_transfer_sha256" = "$expected_base_transfer_sha256" || {
  printf 'base transfer digest mismatch: expected=%s observed=%s\n' \
    "$expected_base_transfer_sha256" "$observed_base_transfer_sha256" >&2
  exit 65
}
(
  cd "$(dirname "$base_root")"
  shasum -a 256 -c "$(basename "${base_root}.TRANSFER.SHA256")"
)
(
  cd "$base_root"
  sha256sum -c TRANSFER.SHA256SUMS
)

manifest_hash_for() {
  python3 -I - "$base_root/PAYLOAD.SHA256SUMS" "$1" <<'PY'
import re
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
expected_path = sys.argv[2]
matches = []
for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
    if "  " not in line:
        raise SystemExit(f"invalid payload manifest row {line_number}")
    digest, relative = line.split("  ", 1)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(f"invalid payload digest at row {line_number}")
    if relative == expected_path:
        matches.append(digest)
if len(matches) != 1:
    raise SystemExit(f"expected exactly one payload row for {expected_path}, observed {len(matches)}")
print(matches[0])
PY
}
verifier_rel="scripts/wsl/verify_and_extract_in_wsl.sh"
expected_verifier_hash="$(manifest_hash_for "$verifier_rel")"
test "$(sha256sum "$base_root/$verifier_rel" | awk '{print $1}')" = "$expected_verifier_hash"
status_rel="HANDOFF_STATUS.json"
expected_status_hash="$(manifest_hash_for "$status_rel")"
test "$(sha256sum "$base_root/$status_rel" | awk '{print $1}')" = "$expected_status_hash"
upstream_manifest_rel="manifests/contracts/boltzgen_upstream_tree.SHA256SUMS"
active_manifest_rel="manifests/active_data_tree.SHA256SUMS"
runtime_manifest_rel="manifests/contracts/runtime_tree.SHA256SUMS"
expected_upstream_manifest_hash="$(manifest_hash_for "$upstream_manifest_rel")"
expected_active_manifest_hash="$(manifest_hash_for "$active_manifest_rel")"
expected_runtime_manifest_hash="$(manifest_hash_for "$runtime_manifest_rel")"
for item in \
  "$upstream_manifest_rel:$expected_upstream_manifest_hash" \
  "$active_manifest_rel:$expected_active_manifest_hash" \
  "$runtime_manifest_rel:$expected_runtime_manifest_hash"; do
  relative="${item%%:*}"
  expected="${item##*:}"
  test -f "$base_root/$relative" && test ! -L "$base_root/$relative"
  test "$(sha256sum "$base_root/$relative" | awk '{print $1}')" = "$expected"
done

test -d "$output_input" && test ! -L "$output_input" || {
  printf 'OUTPUT_PARENT must be the existing real directory beside the base package: %s\n' \
    "$output_input" >&2
  exit 64
}
output_parent="$(cd "$output_input" && pwd -P)"
test "$output_parent" = "$(dirname "$base_root")" || {
  printf 'overlay must be built beside the immutable base package: expected=%s observed=%s\n' \
    "$(dirname "$base_root")" "$output_parent" >&2
  exit 64
}
overlay_name="${base_name}_PERSONAL_INFERENCE_OVERLAY_V1"
final_root="$output_parent/$overlay_name"
external_transfer_hash="$output_parent/${overlay_name}.TRANSFER.SHA256"
test ! -e "$final_root" && test ! -L "$final_root" || {
  printf 'refusing to overwrite existing overlay: %s\n' "$final_root" >&2
  exit 73
}
test ! -e "$external_transfer_hash" && test ! -L "$external_transfer_hash" || {
  printf 'refusing to overwrite existing overlay sidecar: %s\n' "$external_transfer_hash" >&2
  exit 73
}

build_parent="$(mktemp -d "$output_parent/.${overlay_name}.build.XXXXXX")"
package_root="$build_parent/$overlay_name"
published_root=0
published_sidecar=0
cleanup() {
  local exit_code="$?"
  trap - EXIT INT TERM
  if [ "$published_sidecar" -eq 1 ] && [ -f "$external_transfer_hash" ]; then
    rm -f -- "$external_transfer_hash"
  fi
  if [ "$published_root" -eq 1 ] && [ -d "$final_root" ]; then
    rm -rf -- "$final_root"
  fi
  if [ -d "$build_parent" ]; then
    rm -rf -- "$build_parent"
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$package_root/scripts/wsl"
cp "$source_root/QUICK_START_PERSONAL_INFERENCE_ZH.md" "$package_root/README_FIRST_ZH.md"
cp "$source_root/scripts/personal/START_PERSONAL_INFERENCE.ps1" \
  "$package_root/START_PERSONAL_INFERENCE.ps1"
cp "$source_root/scripts/personal/start_personal_vhh_inference.sh" \
  "$package_root/scripts/wsl/start_personal_vhh_inference.sh"
chmod 0755 "$package_root/scripts/wsl/start_personal_vhh_inference.sh"

base_payload_manifest_sha256="$(sha256sum "$base_root/PAYLOAD.SHA256SUMS" | awk '{print $1}')"
base_receipt_sha256="$(sha256sum "$base_root/PACKAGE_RECEIPT.json" | awk '{print $1}')"
python3 -I - \
  "$package_root/BASE_BINDING.json" "$base_root/HANDOFF_STATUS.json" \
  "$base_name" "$expected_base_transfer_sha256" \
  "$base_payload_manifest_sha256" "$base_receipt_sha256" \
  "$project_commit" "$project_branch" \
  "$expected_verifier_hash" "$expected_upstream_manifest_hash" \
  "$expected_active_manifest_hash" "$expected_runtime_manifest_hash" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
base_status = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
payload = {
    "schema_version": "PERSONAL_INFERENCE_OVERLAY_BASE_BINDING_V1",
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "operation": "EXISTING_WEIGHT_VHH_INFERENCE_GENERATION_SCREENING",
    "standalone": False,
    "base_assets_copied": False,
    "base_payload_rehash_at_overlay_build": False,
    "base_payload_rehash_required_at_first_wsl_extraction": True,
    "base_package_name": sys.argv[3],
    "base_package_id": base_status["package_id"],
    "base_transfer_sha256": sys.argv[4],
    "base_payload_manifest_sha256": sys.argv[5],
    "base_package_receipt_sha256": sys.argv[6],
    "base_project_commit": base_status["project_git"]["commit"],
    "base_sanitized_commit": base_status["handoff_git"]["commit"],
    "overlay_source_project_commit": sys.argv[7],
    "overlay_source_project_branch": sys.argv[8],
    "base_verifier_sha256": sys.argv[9],
    "base_upstream_manifest_sha256": sys.argv[10],
    "base_active_manifest_sha256": sys.argv[11],
    "base_runtime_manifest_sha256": sys.argv[12],
    "weight_updates_allowed": False,
    "formal_g1": False,
    "formal_g2": False,
    "formal_aiv1": False,
}
output.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

python3 -I - "$package_root" <<'PY'
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

payload_names = [
    "BASE_BINDING.json",
    "README_FIRST_ZH.md",
    "START_PERSONAL_INFERENCE.ps1",
    "scripts/wsl/start_personal_vhh_inference.sh",
]
with (root / "OVERLAY_PAYLOAD.SHA256SUMS").open("x", encoding="utf-8") as handle:
    for name in payload_names:
        handle.write(f"{digest(root / name)}  {name}\n")

receipt = {
    "schema_version": "PERSONAL_INFERENCE_OVERLAY_RECEIPT_V1",
    "package_id": root.name,
    "status": "PERSONAL_INFERENCE_OVERLAY_READY",
    "operation": "EXISTING_WEIGHT_VHH_INFERENCE_GENERATION_SCREENING",
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "standalone": False,
    "base_assets_copied": False,
    "payload_file_count": len(payload_names),
    "payload_manifest_sha256": digest(root / "OVERLAY_PAYLOAD.SHA256SUMS"),
    "weight_updates_allowed": False,
    "formal_g1": False,
    "formal_g2": False,
    "formal_aiv1": False,
}
(root / "OVERLAY_RECEIPT.json").write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
with (root / "OVERLAY_TRANSFER.SHA256SUMS").open("x", encoding="utf-8") as handle:
    for name in ("OVERLAY_PAYLOAD.SHA256SUMS", "OVERLAY_RECEIPT.json"):
        handle.write(f"{digest(root / name)}  {name}\n")
PY

staged_sidecar="$build_parent/${overlay_name}.TRANSFER.SHA256"
(
  cd "$build_parent"
  shasum -a 256 "$overlay_name/OVERLAY_TRANSFER.SHA256SUMS"
) > "$staged_sidecar"
(
  cd "$package_root"
  sha256sum -c OVERLAY_TRANSFER.SHA256SUMS
  sha256sum -c OVERLAY_PAYLOAD.SHA256SUMS
)
(
  cd "$build_parent"
  shasum -a 256 -c "$(basename "$staged_sidecar")"
)

mv "$package_root" "$final_root"
published_root=1
mv "$staged_sidecar" "$external_transfer_hash"
published_sidecar=1
rmdir "$build_parent"
build_parent=""
trap - EXIT INT TERM

printf 'PERSONAL_INFERENCE_OVERLAY_BUILD_PASS path=%s\n' "$final_root"
