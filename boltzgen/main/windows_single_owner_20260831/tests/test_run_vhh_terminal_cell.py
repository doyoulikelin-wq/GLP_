"""Exact legacy adapter changes, source binding and safe argument forwarding."""
import difflib
import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_vhh_terminal_cell as C


@pytest.fixture
def legacy():
    return Path(C.__file__).with_name("run_owner_exploratory_cell.sh").read_bytes()


def test_real_legacy_exactly_two_line_changes(legacy):
    assert hashlib.sha256(legacy).hexdigest() == C.LEGACY_SHA256
    adapted = C.adapt_legacy(legacy)
    changes = list(difflib.ndiff(legacy.decode().splitlines(), adapted.splitlines()))
    removed = [line[2:] for line in changes if line.startswith("- ")]
    added = [line[2:] for line in changes if line.startswith("+ ")]
    assert removed == [old.rstrip("\n") for old, new in C.REPLACEMENTS]
    assert added == [new.rstrip("\n") for old, new in C.REPLACEMENTS]
    for preserved in ("trap finalize EXIT", "full_finalizer_ready=1", "stop_monitor", "runtime_assets_used.SHA256SUMS"):
        assert adapted.count(preserved) == legacy.decode().count(preserved)


def test_hash_change_rejected(legacy):
    with pytest.raises(ValueError, match="SHA256"):
        C.adapt_legacy(legacy + b"\n")


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_fragment_rejected_even_if_hash_matches(monkeypatch, legacy, index, count):
    old = C.REPLACEMENTS[index][0].encode()
    malformed = legacy.replace(old, old * count)
    monkeypatch.setattr(C, "LEGACY_SHA256", hashlib.sha256(malformed).hexdigest())
    with pytest.raises(ValueError, match="exactly once"):
        C.adapt_legacy(malformed)


def test_already_adapted_fragment_rejected(monkeypatch, legacy):
    malformed = legacy + C.REPLACEMENTS[0][1].encode()
    monkeypatch.setattr(C, "LEGACY_SHA256", hashlib.sha256(malformed).hexdigest())
    with pytest.raises(ValueError, match="exactly once"):
        C.adapt_legacy(malformed)


def test_argv_never_interpolated_or_evaluated(legacy):
    args = ["/tmp/workspace with spaces", "cell;touch /tmp/should-not-exist", "$(echo injected).yaml", "adherence", "2"]
    invocation = C.command(legacy, args)
    assert invocation[:2] == ["bash", "-c"]
    assert invocation[3] == str(Path(C.__file__).resolve())
    assert invocation[4:] == args
    for value in args[:3]:
        assert value not in invocation[2]


def test_exec_replaces_process_and_preserves_five_parameters(monkeypatch, legacy):
    seen = []
    monkeypatch.setattr(C.os, "execvp", lambda executable, argv: seen.append((executable, argv)))
    arguments = ["/home/lin/creator", "cell_a_n2", "/private/A/design.yaml", "adherence", "2"]
    C.main(arguments)
    assert seen == [("bash", C.command(legacy, arguments))]


def test_help_forwarded_unchanged(monkeypatch, legacy):
    seen = []
    monkeypatch.setattr(C.os, "execvp", lambda executable, argv: seen.append(argv))
    C.main(["--help"])
    assert seen == [C.command(legacy, ["--help"])]
