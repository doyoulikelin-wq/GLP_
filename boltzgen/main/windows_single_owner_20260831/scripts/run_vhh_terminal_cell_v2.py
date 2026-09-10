#!/usr/bin/env python3
"""Keep the V1 cell adaptations and change only the CPU validator entry point.

The project-owned historical shell script and V1 adapter remain unchanged.
Three exact in-memory line changes total preserve all GPU parameters, locks,
traps, process groups, logs and finalization. Strict E remains independently
required; successful CPU output validation is not a biological claim.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_vhh_terminal_cell import adapt_legacy

OLD_VALIDATOR = 'validator="$repo_root/boltzgen/main/windows_gpu_handoff_20260829/t3_runtime/validate_cell_output.py"\n'
NEW_VALIDATOR = 'validator="$repo_root/boltzgen/main/windows_single_owner_20260831/scripts/validate_vhh_terminal_cell.py"\n'


def adapt_v2(source: bytes) -> str:
    adapted = adapt_legacy(source)
    if adapted.count(OLD_VALIDATOR) != 1 or adapted.count(NEW_VALIDATOR) != 0:
        raise ValueError("validator entry point must match exactly once")
    result = adapted.replace(OLD_VALIDATOR, NEW_VALIDATOR, 1)
    if result.replace(NEW_VALIDATOR, OLD_VALIDATOR, 1) != adapted:
        raise ValueError("unexpected V2 adaptation changes")
    return result


def command(source: bytes, argv: list[str]) -> list[str]:
    return ["bash", "-c", adapt_v2(source), str(Path(__file__).resolve()), *argv]


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    source = Path(__file__).with_name("run_owner_exploratory_cell.sh")
    if source.is_symlink() or not source.is_file():
        raise ValueError("legacy source must be an ordinary file")
    os.execvp("bash", command(source.read_bytes(), arguments))


if __name__ == "__main__":
    main()
