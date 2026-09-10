#!/usr/bin/env python3
"""Execute the project-owned legacy cell with exactly two in-memory adaptations.

For all A/B/C terminal controls the legacy binding-site filter is disabled: arm A
has no specified binding site and upstream analysis omits that filter's column.
The separate unchanged E evaluator assesses every candidate; legacy filters do
not define biological success. No historical script is edited or copied to disk.
The original process group, locks, traps, finalizer, logs and five-argument
interface are retained by replacing this process with bash.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

LEGACY_SHA256 = "db4375e4f991772703f49435f0b780b3e42117d2a744e0dc1af7f25fdf2b7be0"
REPLACEMENTS = (
    ("  --config filtering 'modality=antibody' 'filter_bindingsite=true'\n",
     "  --config filtering 'modality=antibody' 'filter_bindingsite=false'\n"),
    ('    "filtering.filter_bindingsite": (filtering.get("filter_bindingsite"), True),\n',
     '    "filtering.filter_bindingsite": (filtering.get("filter_bindingsite"), False),\n'),
)


def adapt_legacy(source: bytes) -> str:
    """Refuse changed legacy source or anything other than the exact two edits."""
    if hashlib.sha256(source).hexdigest() != LEGACY_SHA256:
        raise ValueError("legacy source differs from the reviewed SHA256")
    original = source.decode("utf-8")
    adapted = original
    for old, new in REPLACEMENTS:
        if original.count(old) != 1 or original.count(new) != 0:
            raise ValueError("legacy adaptation must match each original fragment exactly once")
        adapted = adapted.replace(old, new, 1)
    restored = adapted
    for old, new in reversed(REPLACEMENTS):
        if restored.count(new) != 1:
            raise ValueError("adapted fragment is not unique")
        restored = restored.replace(new, old, 1)
    if restored != original:
        raise ValueError("unexpected changes outside the two approved replacements")
    return adapted


def command(source: bytes, argv: list[str]) -> list[str]:
    """Arguments are separate argv entries, never formatted into shell source."""
    return ["bash", "-c", adapt_legacy(source), str(Path(__file__).resolve()), *argv]


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    source = Path(__file__).with_name("run_owner_exploratory_cell.sh")
    if source.is_symlink() or not source.is_file():
        raise ValueError("legacy source must be an ordinary file")
    os.execvp("bash", command(source.read_bytes(), arguments))


if __name__ == "__main__":
    main()
