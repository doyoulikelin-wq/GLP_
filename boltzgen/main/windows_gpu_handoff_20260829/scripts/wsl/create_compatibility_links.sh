#!/usr/bin/env bash
# Create only the approved WSL2 compatibility links required by G2/AIV1.
set -euo pipefail
umask 077

workspace_input="${1:?usage: create_compatibility_links.sh WORKSPACE_ROOT}"
workspace_root="$(realpath "$workspace_input")"
test "$workspace_root" != "/"
test "$workspace_root" != "/home"
case "$workspace_root" in
  /home/*) ;;
  *) printf 'workspace resolves outside /home: %s\n' "$workspace_root" >&2; exit 64 ;;
esac
test -d "$workspace_root/GLP_"

ensure_directory() {
  local path="$1"
  case "$path" in
    "$workspace_root"/*) ;;
    *) printf 'directory escapes workspace: %s\n' "$path" >&2; return 64 ;;
  esac
  if [ -L "$path" ]; then
    printf 'directory must not be a symlink: %s\n' "$path" >&2
    return 65
  fi
  if [ ! -e "$path" ]; then
    mkdir "$path"
  fi
  test -d "$path" && test ! -L "$path"
  test "$(realpath "$path")" = "$path"
}

ensure_directory "$workspace_root/data"
ensure_directory "$workspace_root/data/boltzgen_data"
ensure_directory "$workspace_root/data/样本数据"

make_link() {
  local link_path="$1"
  local target_text="$2"
  local resolved_target lexical_target current relative_target old_ifs component

  lexical_target="$(python3 -I - "$(dirname "$link_path")" "$target_text" <<'PY'
import os
import sys
print(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])))
PY
)"
  case "$lexical_target" in
    "$workspace_root"/*) ;;
    *) printf 'compatibility target escapes workspace: %s\n' "$lexical_target" >&2; return 65 ;;
  esac
  current="$workspace_root"
  relative_target="${lexical_target#"$workspace_root"/}"
  old_ifs="$IFS"
  IFS='/'
  for component in $relative_target; do
    current="$current/$component"
    test ! -L "$current" || {
      printf 'symlinked compatibility target component: %s\n' "$current" >&2
      IFS="$old_ifs"
      return 65
    }
  done
  IFS="$old_ifs"
  resolved_target="$(realpath "$lexical_target")"
  case "$resolved_target" in
    "$workspace_root"/*) ;;
    *) printf 'compatibility target escapes workspace: %s\n' "$resolved_target" >&2; return 65 ;;
  esac
  test "$resolved_target" = "$lexical_target" && test -d "$resolved_target" || {
    printf 'missing compatibility target: %s -> %s\n' "$link_path" "$target_text" >&2
    return 66
  }
  if [ -L "$link_path" ]; then
    test "$(readlink "$link_path")" = "$target_text" || {
      printf 'existing link has wrong target: %s\n' "$link_path" >&2
      return 73
    }
    return 0
  fi
  test ! -e "$link_path" || {
    printf 'refusing to replace existing non-link: %s\n' "$link_path" >&2
    return 73
  }
  ln -s "$target_text" "$link_path"
}

make_link \
  "$workspace_root/data/boltzgen_data/mvp_assets_v0.3.2" \
  "../../boltzgen/data/boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819"
make_link \
  "$workspace_root/data/boltzgen_data/sabdab2_vhh_scaffolds_v1" \
  "../../boltzgen/data/vhh_scaffold_database_20260819"
make_link \
  "$workspace_root/data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820" \
  "../../boltzgen/runs/old12_glp1_mac_enhanced_20260820"
make_link \
  "$workspace_root/data/样本数据/binding-多构象" \
  "../../shared/data/glp1_positive_conformer_ensemble_20260819"
make_link \
  "$workspace_root/data/not_binding" \
  "../shared/data/glp2_tuning_countertargets_20260824"

# Deliberately do not create data/样本数据/not_binding: that path is the sealed lockbox.
test ! -e "$workspace_root/data/样本数据/not_binding"
test ! -L "$workspace_root/data/样本数据/not_binding"

printf 'link_path\ttarget_text\tresolved_workspace_path\n'
for link_path in \
  "$workspace_root/data/boltzgen_data/mvp_assets_v0.3.2" \
  "$workspace_root/data/boltzgen_data/sabdab2_vhh_scaffolds_v1" \
  "$workspace_root/data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820" \
  "$workspace_root/data/样本数据/binding-多构象" \
  "$workspace_root/data/not_binding"; do
  resolved_link_target="$(realpath "$link_path")"
  printf '%s\t%s\t%s\n' \
    "${link_path#"$workspace_root"/}" \
    "$(readlink "$link_path")" \
    "${resolved_link_target#"$workspace_root"/}"
done
