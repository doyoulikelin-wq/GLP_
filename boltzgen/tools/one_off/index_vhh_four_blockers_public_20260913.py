#!/usr/bin/env python3
"""Index and validate the small public four-blocker report (2026-09-13).

Inputs: repository root and existing sanitized report directory. Outputs:
SOURCE_INDEX.json, written only on first indexing. Side effects: that one
small public index. No raw data, GPU, model invocation, uploads or deletion.
Exit 0 means this changed-file publication scope passed, not whole-repo policy.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

BASELINE = "2a3a0e789001e8568ab364341a66f89515a8aab8"
REPORT = Path("boltzgen/main/windows_single_owner_20260831/reports/vhh_four_blockers_20260913")


def digest(path):
    """Hash one bounded public artifact, not private model assets."""
    if path.stat().st_size > 2 * 1024**2:
        raise ValueError("public report artifact unexpectedly large")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    """Check authored public files and preserve an inspectable source index."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo_root.resolve(strict=True)
    root = repo / REPORT
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SOURCE_INDEX.json")
    for path in files:
        if path.is_symlink() or path.suffix not in {".json", ".md", ".jsx", ".css"}:
            raise ValueError("unapproved public report file kind")
        text = path.read_text(encoding="utf-8")
        if re.search(r"/home/|(?<![A-Za-z])[A-Za-z]:[\\/]|-----BEGIN .*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{20,}", text):
            raise ValueError("private path or credential-shaped text in public report")
    index = {
        "schema": "VHH_FOUR_BLOCKERS_PUBLIC_SOURCE_INDEX_20260913_V1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Inspect four-blocker engineering corrections and bounded inference, not biological validation.",
        "sources": [
            {"id": "original_terminal_control", "revision": BASELINE,
             "public_summary": "../vhh_terminal_control_20260911/PUBLIC_TERMINAL_CONTINUATION_SUMMARY.json",
             "correction": "Old generated fields were IF placeholder inputs; use new three-stage diagnosis."},
            {"id": "short_peptide_positive_control", "pdb": "9NK9",
             "url": "https://www.rcsb.org/structure/9NK9",
             "paper_doi": "10.1016/j.jbc.2025.110268",
             "license_scope": "Public entry metadata and locally derived numerical summary only; no third-party structure or source copied into Git."},
        ],
        "formats": sorted({p.suffix for p in files}),
        "grain": "candidate or nested fold, explicitly labeled in each source artifact",
        "file_count": len(files),
        "total_bytes": sum(p.stat().st_size for p in files),
        "files": [{"file": str(p.relative_to(root)), "bytes": p.stat().st_size, "sha256": digest(p)}
                  for p in files],
        "sensitivity": "sanitized public derived data; no sequences, coordinates, raw NPZ, private paths, weights or credentials",
        "git_policy": "small authored sources and sanitized summaries only; portable runtime export remains local",
        "license": "Authored text/code inherit repository terms; third-party metadata attribution retained, no broader license asserted.",
        "consumers": ["project maintainers", "Data report readers"],
        "validation_scope": "Changed-file policy plus explicitly recorded scientific checks; not full repository compliance.",
    }
    destination = root / "SOURCE_INDEX.json"
    if destination.exists():
        prior = json.loads(destination.read_text(encoding="utf-8"))
        if prior["files"] != index["files"]:
            raise ValueError("public source index stale; update deliberately, do not silently overwrite")
    else:
        destination.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("repository_policy", repo / "shared/main/repository_policy/check_repository_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    names = set(subprocess.check_output(["git", "diff", "--name-only", BASELINE], cwd=repo, text=True).splitlines())
    names.update(subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo, text=True).splitlines())
    errors = [(name, error) for name in sorted(names) if (repo/name).is_file()
              for error in policy.check_file(repo/name)]
    if errors:
        raise ValueError("changed-file policy failures: " + repr(errors))
    print(json.dumps({"changed_file_policy": "PASS", "checked_files": len(names),
                      "public_index_files": len(files)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
