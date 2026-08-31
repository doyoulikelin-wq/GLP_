#!/usr/bin/env python3
"""Build the twelve deterministic, target-containing BoltzGen design specs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import NoReturn

import yaml

if not __debug__:
    raise RuntimeError("must run without python -O")


FIELDS = [
    "spec_id", "scaffold_id", "scaffold_role", "target_id", "target_chain",
    "binding_label_seq_ids", "cdr1_range", "cdr2_range", "cdr3_range",
    "cdr1_length", "cdr2_length", "cdr3_length", "spec_path",
    "spec_sha256", "scaffold_sha256", "target_sha256",
]
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
RANGE = re.compile(r"([1-9][0-9]*)\.\.([1-9][0-9]*)")
REQUIRED_SELECTED = {
    "selection_rank", "role", "candidate_id", "cdr1_length_aa", "cdr2_length_aa",
    "cdr3_length_aa", "package_path", "boltzgen_check_status",
}
REQUIRED_EXPORT = {
    "candidate_id", "normalized_cif_path", "normalized_cif_sha256",
    "scaffold_yaml_path", "scaffold_yaml_sha256", "target_cif_sha256",
    "boltzgen_check_status",
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"BLOCKED_SPEC_BUILD: {message}")


def require_file(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = path.absolute()
    if ".." in path.parts or path.is_symlink() or not path.is_file():
        fail(f"{label} must be a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    if path != resolved:
        fail(f"{label} path must be canonical without symlink hops: {path}")
    return resolved


def require_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = path.absolute()
    if ".." in path.parts or path.is_symlink() or not path.is_dir():
        fail(f"{label} must be a directory: {path}")
    resolved = path.resolve(strict=True)
    if path != resolved:
        fail(f"{label} path must be canonical without symlink hops: {path}")
    return resolved


def require_output_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = path.absolute()
    if ".." in path.parts or path.name in {"", ".", ".."}:
        fail(f"{label} must be an absolute canonical output path: {path}")
    parent = require_directory(path.parent, f"{label} parent")
    canonical = parent / path.name
    if path != canonical or path.is_symlink():
        fail(f"{label} contains a symlink hop or non-canonical component: {path}")
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_stable_file(path: Path, label: str) -> tuple[bytes, str]:
    """Read one canonical input exactly once and reject identity/content races."""

    canonical = require_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(canonical, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            fail(f"{label} is not a regular file")
        blocks: list[bytes] = []
        hasher = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
            hasher.update(block)
        after = os.fstat(descriptor)
        signature = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if signature(before) != signature(after):
            fail(f"{label} changed while being read")
        current = require_file(path, label).stat()
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            fail(f"{label} path identity changed while being read")
        return b"".join(blocks), hasher.hexdigest()
    finally:
        os.close(descriptor)


def safe_relative(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or path.as_posix() != value
    ):
        fail(f"unsafe {label}: {value!r}")
    return path


def read_tsv(path: Path, required: set[str], label: str) -> list[dict[str, str]]:
    try:
        content, _ = read_stable_file(path, label)
        handle = io.StringIO(content.decode("utf-8"), newline="")
        reader = csv.DictReader(handle, delimiter="\t")
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not required.issubset(fields):
            fail(f"{label} header is missing {sorted(required - set(fields))}")
        rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        fail(f"cannot read {label}: {error}")
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        fail(f"empty or malformed {label}")
    return rows


def parse_ranges(
    scaffold_yaml_bytes: bytes, scaffold_yaml: Path,
) -> tuple[tuple[str, str, str], tuple[int, int, int]]:
    try:
        payload = yaml.safe_load(scaffold_yaml_bytes.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        fail(f"invalid scaffold YAML {scaffold_yaml}: {error}")
    if not isinstance(payload, dict) or payload.get("path") != "scaffold.cif":
        fail(f"scaffold YAML path must be scaffold.cif: {scaffold_yaml}")
    design = payload.get("design")
    if not isinstance(design, list) or len(design) != 1 or not isinstance(design[0], dict):
        fail(f"scaffold YAML must contain one design chain: {scaffold_yaml}")
    chain = design[0].get("chain")
    if not isinstance(chain, dict) or not isinstance(chain.get("id"), str):
        fail(f"invalid scaffold design chain: {scaffold_yaml}")
    ranges_text = chain.get("res_index")
    if not isinstance(ranges_text, str):
        fail(f"missing scaffold CDR ranges: {scaffold_yaml}")
    items = tuple(item.strip() for item in ranges_text.split(","))
    if len(items) != 3:
        fail(f"expected exactly three CDR ranges: {scaffold_yaml}")
    lengths: list[int] = []
    previous_end = 0
    for item in items:
        match = RANGE.fullmatch(item)
        if match is None:
            fail(f"non-canonical CDR range {item!r}: {scaffold_yaml}")
        start, end = map(int, match.groups())
        if end < start or start <= previous_end:
            fail(f"invalid or overlapping CDR range {item!r}: {scaffold_yaml}")
        previous_end = end
        lengths.append(end - start + 1)
    return (items[0], items[1], items[2]), (lengths[0], lengths[1], lengths[2])


def render_design_yaml(target_chain: str) -> bytes:
    payload = {
        "entities": [
            {
                "file": {
                    "path": "target.cif",
                    "include": [{"chain": {"id": target_chain, "res_index": "1..30"}}],
                    "binding_types": [{"chain": {"id": target_chain, "binding": "1..2"}}],
                    "structure_groups": [{"group": {"id": target_chain, "visibility": 1}}],
                }
            },
            {"file": {"path": "scaffold.yaml"}},
        ]
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")


def tree_snapshot(root: Path) -> dict[str, str]:
    observed: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            fail(f"symlink forbidden in spec tree: {path}")
        if path.is_file():
            observed[path.relative_to(root).as_posix()] = f"file:{digest(path)}"
        elif path.is_dir():
            observed[path.relative_to(root).as_posix()] = "directory"
        else:
            fail(f"special file forbidden in spec tree: {path}")
    return observed


def compatible_tree(observed: dict[str, str], expected: dict[str, str]) -> bool:
    """Allow an identical partial tree to be completed after an interrupted run."""

    return all(expected.get(name) == value for name, value in observed.items())


def publish_file(path: Path, content: bytes) -> None:
    parent = require_directory(path.parent, f"output parent for {path.name}")
    if path.parent != parent:
        fail(f"output parent is non-canonical: {path.parent}")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            fail(f"immutable output differs: {path}")
        existing, _ = read_stable_file(path, f"existing output {path.name}")
        if existing != content:
            fail(f"immutable output differs: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard-link publication is atomic on this filesystem and, unlike
            # os.replace(), can never overwrite a concurrently-created receipt.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                fail(f"immutable output differs: {path}")
            existing, _ = read_stable_file(path, f"colliding output {path.name}")
            if existing != content:
                fail(f"immutable output differs: {path}")
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_tree_no_replace(
    source: Path, destination: Path, expected: dict[str, str]
) -> None:
    """Publish a tree file-by-file without replacing any directory entry."""

    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            fail(f"immutable specs tree differs: {destination}")
        observed = tree_snapshot(destination)
        if not compatible_tree(observed, expected):
            fail(f"immutable specs tree differs: {destination}")
    else:
        try:
            destination.mkdir()
        except FileExistsError:
            if destination.is_symlink() or not destination.is_dir():
                fail(f"immutable specs tree publication collided: {destination}")

    for relative, kind in sorted(expected.items()):
        target = destination / Path(*PurePosixPath(relative).parts)
        if kind == "directory":
            try:
                target.mkdir()
            except FileExistsError:
                if target.is_symlink() or not target.is_dir():
                    fail(f"immutable spec directory differs: {target}")
            continue
        source_file = source / Path(*PurePosixPath(relative).parts)
        content, source_sha = read_stable_file(source_file, f"staged spec {relative}")
        if kind != f"file:{source_sha}":
            fail(f"staged spec changed before publication: {relative}")
        publish_file(target, content)
    if tree_snapshot(destination) != expected:
        fail(f"published specs tree differs: {destination}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--scaffold-root", required=True)
    parser.add_argument("--selected-scaffolds", required=True)
    parser.add_argument("--export-artifacts", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--target-chain", required=True)
    parser.add_argument("--binding-label-seq-ids", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()

    if args.target_id != "GLP1_7-36_NH2" or args.target_chain != "E":
        fail("the frozen target identity/chain contract is GLP1_7-36_NH2/E")
    if args.binding_label_seq_ids != "1,2":
        fail("the frozen target binding label_seq_id contract is exactly 1,2")
    target = require_file(args.target, "target")
    scaffold_root = require_directory(args.scaffold_root, "scaffold root")
    selected_path = require_file(args.selected_scaffolds, "selected scaffolds")
    export_path = require_file(args.export_artifacts, "export artifacts")
    output_root = require_output_path(args.output_root, "spec output root")
    manifest = require_output_path(args.manifest, "spec manifest")
    if output_root.name != "specs" or output_root.is_symlink() or manifest.is_symlink():
        fail("output root must be a non-symlink directory named specs")
    if manifest.parent != output_root.parent:
        fail("manifest and specs must share the same input root")

    selected_rows = read_tsv(selected_path, REQUIRED_SELECTED, "selected scaffolds")
    export_rows = read_tsv(export_path, REQUIRED_EXPORT, "export artifacts")
    if len(selected_rows) != 12 or len(export_rows) != 12:
        fail("the frozen baseline requires exactly 12 selected and export rows")
    if [row["selection_rank"] for row in selected_rows] != [str(index) for index in range(1, 13)]:
        fail("selection_rank must be exactly 1..12 in file order")
    roles = [row["role"] for row in selected_rows]
    if roles.count("PRIMARY") != 10 or roles.count("RESERVE") != 2 or any(role not in {"PRIMARY", "RESERVE"} for role in roles):
        fail("scaffold roles must be PRIMARY=10 and RESERVE=2")
    export_by_candidate = {row["candidate_id"]: row for row in export_rows}
    if len(export_by_candidate) != 12:
        fail("duplicate candidate in export artifacts")
    if set(export_by_candidate) != {row["candidate_id"] for row in selected_rows}:
        fail("selected/export candidate sets differ")

    temporary = Path(tempfile.mkdtemp(prefix=".specs.build.", dir=output_root.parent))
    rows: list[dict[str, str]] = []
    target_bytes, target_sha = read_stable_file(target, "target")
    try:
        for selected in selected_rows:
            candidate = selected["candidate_id"]
            if SAFE_ID.fullmatch(candidate) is None:
                fail(f"unsafe scaffold candidate ID: {candidate!r}")
            package_path = safe_relative(selected["package_path"], "package path")
            if len(package_path.parts) != 2 or package_path.parts[0] != "selected":
                fail(f"unexpected package path: {package_path}")
            spec_id = package_path.parts[1]
            if SAFE_ID.fullmatch(spec_id) is None:
                fail(f"unsafe spec ID: {spec_id!r}")
            package = scaffold_root / spec_id
            package = require_directory(package, f"staged scaffold package {candidate}")
            scaffold = require_file(package / "scaffold.cif", "scaffold CIF")
            scaffold_yaml = require_file(package / "scaffold.yaml", "scaffold YAML")
            exported = export_by_candidate[candidate]
            if exported["boltzgen_check_status"] != "PASS" or selected["boltzgen_check_status"] != "PASS":
                fail(f"baseline scaffold check is not PASS: {candidate}")
            expected_cif = safe_relative(exported["normalized_cif_path"], "export CIF path")
            expected_yaml = safe_relative(exported["scaffold_yaml_path"], "export YAML path")
            if (
                expected_cif != package_path / "scaffold.cif"
                or expected_yaml != package_path / "scaffold.yaml"
            ):
                fail(f"export artifact package mismatch: {candidate}")
            scaffold_bytes, scaffold_sha = read_stable_file(
                scaffold, f"scaffold CIF {candidate}"
            )
            scaffold_yaml_bytes, scaffold_yaml_sha = read_stable_file(
                scaffold_yaml, f"scaffold YAML {candidate}"
            )
            if scaffold_sha != exported["normalized_cif_sha256"] or scaffold_yaml_sha != exported["scaffold_yaml_sha256"]:
                fail(f"export artifact hash drift: {candidate}")
            if exported["target_cif_sha256"] != target_sha:
                fail(f"target hash differs from checked export: {candidate}")
            cdr_ranges, cdr_lengths = parse_ranges(scaffold_yaml_bytes, scaffold_yaml)
            expected_lengths = tuple(int(selected[f"cdr{index}_length_aa"]) for index in range(1, 4))
            if cdr_lengths != expected_lengths:
                fail(f"CDR length drift for {candidate}: YAML={cdr_lengths} registry={expected_lengths}")

            destination = temporary / spec_id
            destination.mkdir()
            (destination / "target.cif").write_bytes(target_bytes)
            (destination / "scaffold.cif").write_bytes(scaffold_bytes)
            (destination / "scaffold.yaml").write_bytes(scaffold_yaml_bytes)
            design = destination / "design.yaml"
            design.write_bytes(render_design_yaml(args.target_chain))
            manifest_row = {
                "spec_id": spec_id,
                "scaffold_id": candidate,
                "scaffold_role": selected["role"],
                "target_id": args.target_id,
                "target_chain": args.target_chain,
                "binding_label_seq_ids": args.binding_label_seq_ids,
                "cdr1_range": cdr_ranges[0], "cdr2_range": cdr_ranges[1], "cdr3_range": cdr_ranges[2],
                "cdr1_length": str(cdr_lengths[0]), "cdr2_length": str(cdr_lengths[1]), "cdr3_length": str(cdr_lengths[2]),
                "spec_path": f"specs/{spec_id}/design.yaml",
                "spec_sha256": digest(design), "scaffold_sha256": scaffold_sha, "target_sha256": target_sha,
            }
            # Reuse the verifier itself so the builder cannot publish a weaker
            # target/include/visibility/reset/mmCIF contract than T3 verifies.
            from verify_specs import validate_spec_contract

            validate_spec_contract(manifest_row, destination)
            rows.append(manifest_row)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        manifest_content = buffer.getvalue().encode("utf-8")

        # Preflight every immutable destination before publishing either one.
        # If a crash occurs after the directory rename but before the manifest
        # link, an identical rerun recognizes the tree and safely completes the
        # missing link without overwriting either artifact.
        expected_tree = tree_snapshot(temporary)
        output_exists = output_root.exists() or output_root.is_symlink()
        if output_exists:
            if output_root.is_symlink() or not output_root.is_dir():
                fail(f"immutable specs tree differs: {output_root}")
            if not compatible_tree(tree_snapshot(output_root), expected_tree):
                fail(f"immutable specs tree differs: {output_root}")
        manifest_exists = manifest.exists() or manifest.is_symlink()
        if manifest_exists and (
            manifest.is_symlink()
            or not manifest.is_file()
        ):
            fail(f"immutable output differs: {manifest}")
        if manifest_exists:
            existing_manifest, _ = read_stable_file(manifest, "existing spec manifest")
            if existing_manifest != manifest_content:
                fail(f"immutable output differs: {manifest}")

        publish_tree_no_replace(temporary, output_root, expected_tree)
        publish_file(manifest, manifest_content)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
