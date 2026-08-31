from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


EXPORTER = (
    Path(__file__).resolve().parents[2]
    / "scripts/wsl/export_code_patches_for_mac.sh"
)
HOME = Path.home().resolve()
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or len(HOME.parts) < 3 or HOME.parts[:2] != ("/", "home"),
    reason="the exporter contract and ext4 publication test require Linux /home/<user>",
)
BASE_ENV = {"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC"}
EXPECTED_FILES = {
    "PATCH_PAYLOAD.SHA256SUMS",
    "PATCH_TRANSFER.SHA256SUMS",
    "changed_paths.txt",
    "patch_scan.json",
    "patch_scope.json",
    "patches/0001-windows-gpu-squashed-final-tree.patch",
    "receipt.json",
}


def _run(
    *argv: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env=environment or BASE_ENV,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {argv!r}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)
    root.chmod(0o700)


def _prepare_workspace() -> tuple[Path, Path, Path, str, str]:
    root = Path(tempfile.mkdtemp(prefix=".patch-export-test.", dir=HOME))
    workspace = root / "workspace"
    repo = workspace / "GLP_"
    handoff = workspace / "handoff"
    output = workspace / "gpu_work"
    repo.mkdir(parents=True)
    handoff.mkdir()
    output.mkdir()

    _run("/usr/bin/git", "init", "-q", "-b", "sanitized-baseline", str(repo))
    _run("/usr/bin/git", "config", "user.name", "Exporter Test", cwd=repo)
    _run("/usr/bin/git", "config", "user.email", "exporter-test@example.invalid", cwd=repo)
    tracked = repo / "boltzgen/main/exporter_regression.txt"
    tracked.parent.mkdir(parents=True)
    scanner_literal = 'scanner = r"' + "/" + "home" + r"/[^/\s]+/" + '"\n'
    tracked.write_text(scanner_literal + "state = baseline\n", encoding="utf-8")
    rename_source = repo / "boltzgen/main/rename_source.txt"
    rename_source.write_text("innocent rename payload\n", encoding="utf-8")
    deletion_source = repo / "boltzgen/main/interior_delete.txt"
    deletion_source.write_text(
        "alpha\ndelete one\ndelete two\nomega\n",
        encoding="utf-8",
    )
    _run(
        "/usr/bin/git",
        "add",
        "--",
        str(tracked.relative_to(repo)),
        str(rename_source.relative_to(repo)),
        str(deletion_source.relative_to(repo)),
        cwd=repo,
    )
    _run("/usr/bin/git", "commit", "-q", "-m", "sanitized baseline", cwd=repo)
    baseline = _run("/usr/bin/git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    (handoff / "HANDOFF_STATUS.json").write_text(
        json.dumps(
            {"handoff_git": {"branch": "sanitized-baseline", "commit": baseline}},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    _run(
        "/usr/bin/git",
        "switch",
        "-q",
        "-c",
        "codex/windows-gpu-20260829",
        cwd=repo,
    )
    tracked.write_text(scanner_literal + "state = updated\n", encoding="utf-8")
    _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
    _run("/usr/bin/git", "commit", "-q", "-m", "windows change", cwd=repo)
    head = _run("/usr/bin/git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    tree = _run("/usr/bin/git", "rev-parse", "HEAD^{tree}", cwd=repo).stdout.strip()
    return root, repo, output, head, tree


def _export(
    repo: Path,
    output: Path,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "/bin/bash",
        str(EXPORTER),
        str(repo),
        str(output),
        "attempt_001",
        environment=environment,
        check=False,
    )


def _verify_manifest(root: Path, manifest: Path) -> None:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        assert _digest(root / relative) == expected


def _assert_not_published(output: Path) -> None:
    exports = output / "windows_code_patch_exports"
    assert not (exports / "attempt_001").exists()
    assert not (exports / "attempt_001.PATCH.SHA256").exists()
    assert not list(exports.glob(".attempt_001.build.*"))
    assert not list(exports.glob(".attempt_001.PATCH.SHA256.pending.*"))


def _assert_success(
    output: Path,
    head: str,
    tree: str,
    *,
    expected_diff_file_count: int = 1,
) -> None:
    exports = output / "windows_code_patch_exports"
    final = exports / "attempt_001"
    external = exports / "attempt_001.PATCH.SHA256"
    assert final.is_dir() and not final.is_symlink()
    assert external.is_file() and not external.is_symlink()
    assert not list(exports.glob(".attempt_001.build.*"))
    assert not list(exports.glob(".attempt_001.PATCH.SHA256.pending.*"))
    assert stat.S_IMODE(final.stat().st_mode) == 0o500
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o500
        for path in final.rglob("*")
        if path.is_dir()
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o400
        for path in final.rglob("*")
        if path.is_file()
    )
    assert stat.S_IMODE(external.stat().st_mode) == 0o400
    assert {
        path.relative_to(final).as_posix()
        for path in final.rglob("*")
        if path.is_file()
    } == EXPECTED_FILES
    _verify_manifest(final, final / "PATCH_PAYLOAD.SHA256SUMS")
    _verify_manifest(final, final / "PATCH_TRANSFER.SHA256SUMS")
    receipt = json.loads((final / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "CODE_PATCH_EXPORT_PASS"
    assert receipt["attempt_id"] == "attempt_001"
    assert receipt["windows_head_commit"] == head
    assert receipt["target_tree"] == tree
    assert receipt["patch_apply_check"] == "PASS"
    assert receipt["integration_authority"] == "MAC_CODEX_REVIEW_REQUIRED"
    scan = json.loads((final / "patch_scan.json").read_text(encoding="utf-8"))
    assert scan["schema_version"] == "WINDOWS_CODE_PATCH_BYTE_SCAN_V2"
    assert scan["scan_scope"] == "ADDED_DIFF_CONTENT_PER_FILE_AND_CHANGED_PATH_V2"
    assert scan["diff_file_count"] == expected_diff_file_count
    assert scan["changed_paths_exact_match"] is True
    assert scan["git_apply_mode"] == "UNIDIFF_ZERO_REQUIRED"
    assert scan["credential_like_match_count"] == 0
    assert scan["absolute_user_path_match_count"] == 0
    assert scan["changed_path_credential_like_match_count"] == 0
    assert scan["changed_path_absolute_user_path_match_count"] == 0
    patch_text = (
        final / "patches/0001-windows-gpu-squashed-final-tree.patch"
    ).read_text(encoding="utf-8")
    assert "/" + "home" + "/[^/" in patch_text
    transfer_sha = _digest(final / "PATCH_TRANSFER.SHA256SUMS")
    assert external.read_text(encoding="ascii") == (
        f"{transfer_sha}  attempt_001/PATCH_TRANSFER.SHA256SUMS\n"
    )


def test_ext4_success_freezes_then_commits() -> None:
    root, repo, output, head, tree = _prepare_workspace()
    try:
        result = _export(repo, output)
        assert result.returncode == 0, result.stderr
        assert "CODE_PATCH_EXPORT_PASS" in result.stdout
        _assert_success(output, head, tree)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_repo_rename_detection_config_cannot_change_patch_scope() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        source = repo / "boltzgen/main/rename_source.txt"
        destination = repo / "boltzgen/main/rename_destination.txt"
        source.rename(destination)
        _run("/usr/bin/git", "add", "-A", cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "pure rename", cwd=repo)
        _run("/usr/bin/git", "config", "diff.renames", "true", cwd=repo)
        head = _run("/usr/bin/git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        tree = _run("/usr/bin/git", "rev-parse", "HEAD^{tree}", cwd=repo).stdout.strip()
        result = _export(repo, output)
        assert result.returncode == 0, result.stderr
        _assert_success(output, head, tree, expected_diff_file_count=3)
        changed_paths = (
            output
            / "windows_code_patch_exports/attempt_001/changed_paths.txt"
        ).read_text(encoding="utf-8").splitlines()
        assert set(changed_paths) == {
            "boltzgen/main/exporter_regression.txt",
            "boltzgen/main/rename_destination.txt",
            "boltzgen/main/rename_source.txt",
        }
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_repo_noprefix_config_cannot_change_patch_headers() -> None:
    root, repo, output, head, tree = _prepare_workspace()
    try:
        _run("/usr/bin/git", "config", "diff.noprefix", "true", cwd=repo)
        result = _export(repo, output)
        assert result.returncode == 0, result.stderr
        _assert_success(output, head, tree)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_zero_context_interior_deletion_applies_with_explicit_mode() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        source = repo / "boltzgen/main/interior_delete.txt"
        source.write_text("alpha\nomega\n", encoding="utf-8")
        _run("/usr/bin/git", "add", "--", str(source.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "delete interior lines", cwd=repo)
        head = _run("/usr/bin/git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        tree = _run("/usr/bin/git", "rev-parse", "HEAD^{tree}", cwd=repo).stdout.strip()
        result = _export(repo, output)
        assert result.returncode == 0, result.stderr
        _assert_success(output, head, tree, expected_diff_file_count=2)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_failure_after_freeze_thaws_and_cleans() -> None:
    root, repo, output, head, tree = _prepare_workspace()
    try:
        wrappers = root / "wrappers"
        wrappers.mkdir()
        chmod_wrapper = wrappers / "chmod"
        chmod_wrapper.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            "/usr/bin/chmod \"$@\"\n"
            "rc=$?\n"
            "[ \"$rc\" -eq 0 ] || exit \"$rc\"\n"
            "if [ \"${1-}\" = '-R' ] && [ \"${2-}\" = 'a-w' ]; then exit 91; fi\n",
            encoding="utf-8",
        )
        chmod_wrapper.chmod(0o755)
        failing_env = {**BASE_ENV, "PATH": f"{wrappers}:/usr/bin:/bin"}
        failed = _export(repo, output, environment=failing_env)
        assert failed.returncode == 91
        exports = output / "windows_code_patch_exports"
        assert not (exports / "attempt_001").exists()
        assert not (exports / "attempt_001.PATCH.SHA256").exists()
        assert not list(exports.glob(".attempt_001.build.*"))
        assert not list(exports.glob(".attempt_001.PATCH.SHA256.pending.*"))

        retried = _export(repo, output)
        assert retried.returncode == 0, retried.stderr
        _assert_success(output, head, tree)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_commit_marker_is_the_last_publish_step() -> None:
    root, repo, output, head, tree = _prepare_workspace()
    try:
        exports = output / "windows_code_patch_exports"
        final = exports / "attempt_001"
        external = exports / "attempt_001.PATCH.SHA256"
        probe = root / "commit-marker-order.txt"
        wrappers = root / "wrappers"
        wrappers.mkdir()
        mv_wrapper = wrappers / "mv"
        mv_wrapper.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "args=(\"$@\")\n"
            "source_path=\"${args[${#args[@]}-2]}\"\n"
            "destination=\"${args[${#args[@]}-1]}\"\n"
            f"if [ \"$destination\" = '{external}' ]; then\n"
            f"  [ -d '{final}' ] && [ ! -e '{external}' ]\n"
            f"  [ \"$(/usr/bin/stat -c %a '{final}')\" = '500' ]\n"
            f"  [ -z \"$(/usr/bin/find '{final}' -type d ! -perm 0500 -print -quit)\" ]\n"
            f"  [ -z \"$(/usr/bin/find '{final}' -type f ! -perm 0400 -print -quit)\" ]\n"
            f"  if compgen -G '{exports}/.attempt_001.build.*' >/dev/null; then exit 96; fi\n"
            "  [ \"$(/usr/bin/stat -c %a \"$source_path\")\" = '400' ]\n"
            f"  (cd '{exports}' && /usr/bin/sha256sum -c \"$source_path\" >/dev/null)\n"
            f"  printf 'PASS\\n' > '{probe}'\n"
            "fi\n"
            "exec /usr/bin/mv \"$@\"\n",
            encoding="utf-8",
        )
        mv_wrapper.chmod(0o755)
        wrapped_env = {**BASE_ENV, "PATH": f"{wrappers}:/usr/bin:/bin"}
        result = _export(repo, output, environment=wrapped_env)
        assert result.returncode == 0, result.stderr
        assert probe.read_text(encoding="ascii") == "PASS\n"
        _assert_success(output, head, tree)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_concurrent_loser_cannot_remove_the_committed_winner() -> None:
    root, repo, output, head, tree = _prepare_workspace()
    contender: subprocess.Popen[str] | None = None
    try:
        exports = output / "windows_code_patch_exports"
        final = exports / "attempt_001"
        blocked = root / "contender-blocked.txt"
        release = root / "release-contender.txt"
        wrappers = root / "wrappers"
        wrappers.mkdir()
        mv_wrapper = wrappers / "mv"
        mv_wrapper.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "args=(\"$@\")\n"
            "destination=\"${args[${#args[@]}-1]}\"\n"
            f"if [ \"$destination\" = '{final}' ]; then\n"
            f"  printf 'READY\\n' > '{blocked}'\n"
            "  for _ in {1..400}; do\n"
            f"    [ -e '{release}' ] && break\n"
            "    /usr/bin/sleep 0.01\n"
            "  done\n"
            f"  [ -e '{release}' ] || exit 97\n"
            "fi\n"
            "exec /usr/bin/mv \"$@\"\n",
            encoding="utf-8",
        )
        mv_wrapper.chmod(0o755)
        contender_env = {**BASE_ENV, "PATH": f"{wrappers}:/usr/bin:/bin"}
        contender = subprocess.Popen(
            [
                "/bin/bash",
                str(EXPORTER),
                str(repo),
                str(output),
                "attempt_001",
            ],
            env=contender_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15
        while not blocked.exists() and time.monotonic() < deadline:
            if contender.poll() is not None:
                stdout, stderr = contender.communicate()
                raise AssertionError(
                    f"contender exited before the final-move barrier: {stdout=} {stderr=}"
                )
            time.sleep(0.02)
        assert blocked.read_text(encoding="ascii") == "READY\n"

        winner = _export(repo, output)
        assert winner.returncode == 0, winner.stderr
        release.write_text("GO\n", encoding="ascii")
        contender_stdout, contender_stderr = contender.communicate(timeout=30)
        assert contender.returncode != 0, contender_stdout
        assert "attempt_001" in contender_stderr
        _assert_success(output, head, tree)
    finally:
        release_path = root / "release-contender.txt"
        release_path.write_text("GO\n", encoding="ascii")
        if contender is not None and contender.poll() is None:
            contender.terminate()
            contender.communicate(timeout=10)
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_hup_cannot_split_the_commit_marker_from_the_frozen_attempt() -> None:
    root, repo, output, head, tree = _prepare_workspace()
    try:
        exports = output / "windows_code_patch_exports"
        external = exports / "attempt_001.PATCH.SHA256"
        wrappers = root / "wrappers"
        wrappers.mkdir()
        mv_wrapper = wrappers / "mv"
        mv_wrapper.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "args=(\"$@\")\n"
            "destination=\"${args[${#args[@]}-1]}\"\n"
            f"if [ \"$destination\" = '{external}' ]; then\n"
            "  /usr/bin/mv \"$@\"\n"
            "  /usr/bin/kill -HUP \"$PPID\"\n"
            "  exit 0\n"
            "fi\n"
            "exec /usr/bin/mv \"$@\"\n",
            encoding="utf-8",
        )
        mv_wrapper.chmod(0o755)
        wrapped_env = {**BASE_ENV, "PATH": f"{wrappers}:/usr/bin:/bin"}
        result = _export(repo, output, environment=wrapped_env)
        assert result.returncode == 0, result.stderr
        _assert_success(output, head, tree)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_new_machine_specific_path_is_still_rejected() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        tracked = repo / "boltzgen/main/exporter_regression.txt"
        newly_added_path = "/" + "home" + "/example/private/"
        tracked.write_text(
            tracked.read_text(encoding="utf-8") + f"leak = {newly_added_path}\n",
            encoding="utf-8",
        )
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "introduce forbidden path", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert "machine-specific user path forbidden" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_credential_like_changed_filename_is_rejected() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        credential = "sk-" + "proj-" + "A" * 24
        tracked = repo / f"boltzgen/main/{credential}.txt"
        tracked.write_text("innocent payload\n", encoding="utf-8")
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "credential-like filename", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert "credential-like content forbidden in code patch path" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_machine_specific_changed_path_is_rejected() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        tracked = repo / ("boltzgen/main/" + "home/example/private.txt")
        tracked.parent.mkdir(parents=True)
        tracked.write_text("innocent payload\n", encoding="utf-8")
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "machine-specific path", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert "machine-specific user path forbidden in code patch path" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_exact_baseline_match_reintroduced_on_a_new_line_is_rejected() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        tracked = repo / "boltzgen/main/exporter_regression.txt"
        matched_literal = "/" + "home" + r"/[^/\s]+/"
        tracked.write_text(
            f'leak = r"{matched_literal}"\nstate = relocated\n',
            encoding="utf-8",
        )
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "relocate exact match", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert "machine-specific user path forbidden in added code patch lines" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_plus_prefixed_added_payload_is_not_mistaken_for_metadata() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        tracked = repo / "boltzgen/main/exporter_regression.txt"
        machine_path = "/" + "home" + "/example/private/"
        tracked.write_text(
            tracked.read_text(encoding="utf-8") + f"++ leak = {machine_path}\n",
            encoding="utf-8",
        )
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "plus-prefixed leak", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert "machine-specific user path forbidden in added code patch lines" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_credential_like_added_content_is_rejected() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        tracked = repo / "boltzgen/main/exporter_regression.txt"
        credential = "sk-" + "proj-" + "B" * 24
        tracked.write_text(
            tracked.read_text(encoding="utf-8") + f"token = {credential}\n",
            encoding="utf-8",
        )
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "credential-like content", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert "credential-like content forbidden in added code patch lines" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


@pytest.mark.parametrize("separator", ["\u2028", "\u0085", "\v", "\f"])
@pytest.mark.parametrize("payload_kind", ["credential", "machine_path"])
def test_unicode_line_separator_cannot_hide_added_content(
    separator: str,
    payload_kind: str,
) -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        tracked = repo / "boltzgen/main/exporter_regression.txt"
        if payload_kind == "credential":
            forbidden_fragment = "sk-" + "proj-" + "C" * 24
            expected_error = "credential-like content forbidden in added code patch lines"
        else:
            forbidden_fragment = "/" + "home" + "/example/private/"
            expected_error = "machine-specific user path forbidden in added code patch lines"
        tracked.write_text(
            tracked.read_text(encoding="utf-8")
            + "safe-prefix"
            + separator
            + "-"
            + forbidden_fragment
            + "\n",
            encoding="utf-8",
        )
        _run("/usr/bin/git", "add", "--", str(tracked.relative_to(repo)), cwd=repo)
        _run("/usr/bin/git", "commit", "-q", "-m", "unicode separator payload", cwd=repo)
        result = _export(repo, output)
        assert result.returncode != 0
        assert expected_error in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)


def test_head_drift_after_scope_scan_cannot_change_the_export() -> None:
    root, repo, output, _head, _tree = _prepare_workspace()
    try:
        wrappers = root / "wrappers"
        wrappers.mkdir()
        sentinel = root / "head-drift-triggered.txt"
        credential = "sk-" + "proj-" + "D" * 24
        relative = f"boltzgen/main/{credential}.txt"
        target = repo / relative
        git_wrapper = wrappers / "git"
        git_wrapper.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f"if [[ \" $* \" == *\" rev-list --min-parents=2 \"* ]] && "
            f"[ ! -e '{sentinel}' ]; then\n"
            f"  /usr/bin/printf 'TRIGGERED\\n' > '{sentinel}'\n"
            f"  /usr/bin/printf 'innocent payload\\n' > '{target}'\n"
            f"  /usr/bin/git -C '{repo}' add -- '{relative}'\n"
            f"  /usr/bin/git -C '{repo}' commit -q -m 'advance HEAD during export'\n"
            "fi\n"
            "exec /usr/bin/git \"$@\"\n",
            encoding="utf-8",
        )
        git_wrapper.chmod(0o755)
        drifting_env = {**BASE_ENV, "PATH": f"{wrappers}:/usr/bin:/bin"}
        result = _export(repo, output, environment=drifting_env)
        assert sentinel.read_text(encoding="ascii") == "TRIGGERED\n"
        assert result.returncode != 0
        assert "source repository changed during patch export" in result.stderr
        _assert_not_published(output)
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root)
