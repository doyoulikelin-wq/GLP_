"""The continuation adapter changes only the declared validator entry point."""
import difflib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_vhh_terminal_cell as V1
import run_vhh_terminal_cell_v2 as V2


def source():
    return Path(V2.__file__).with_name("run_owner_exploratory_cell.sh").read_bytes()


def test_exact_third_change_only():
    original, first = source().decode(), V1.adapt_legacy(source())
    adapted = V2.adapt_v2(source())
    assert adapted.replace(V2.NEW_VALIDATOR, V2.OLD_VALIDATOR, 1) == first
    delta = list(difflib.ndiff(original.splitlines(), adapted.splitlines()))
    assert sum(line.startswith("- ") for line in delta) == 3
    assert sum(line.startswith("+ ") for line in delta) == 3


@pytest.mark.parametrize("count", [0, 2])
def test_ambiguous_validator_match_refused(monkeypatch, count):
    malformed = V1.adapt_legacy(source()).replace(V2.OLD_VALIDATOR, V2.OLD_VALIDATOR * count)
    monkeypatch.setattr(V2, "adapt_legacy", lambda value: malformed)
    with pytest.raises(ValueError, match="exactly once"):
        V2.adapt_v2(source())


def test_legacy_hash_change_refused():
    with pytest.raises(ValueError, match="SHA256"):
        V2.adapt_v2(source() + b"\n")


def test_arguments_and_exec_preserved_without_interpolation(monkeypatch):
    seen = []
    monkeypatch.setattr(V2.os, "execvp", lambda executable, argv: seen.append((executable, argv)))
    args = ["/home/space name", "cell;not-a-command", "$(unsafe).yaml", "adherence", "2"]
    V2.main(args)
    executable, command = seen[0]
    assert executable == "bash" and command[:2] == ["bash", "-c"]
    assert command[3] == str(Path(V2.__file__).resolve())
    assert command[4:] == args
    assert all(arg not in command[2] for arg in args[:3])
