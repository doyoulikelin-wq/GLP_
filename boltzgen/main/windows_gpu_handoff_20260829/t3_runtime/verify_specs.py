#!/usr/bin/env python3
"""Verify the frozen twelve-spec input, check, and manual-review contract."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

import gemmi
import yaml


if not __debug__:
    raise RuntimeError("must run without python -O")


MANIFEST_FIELDS = [
    "spec_id", "scaffold_id", "scaffold_role", "target_id", "target_chain",
    "binding_label_seq_ids", "cdr1_range", "cdr2_range", "cdr3_range",
    "cdr1_length", "cdr2_length", "cdr3_length", "spec_path",
    "spec_sha256", "scaffold_sha256", "target_sha256",
]
REVIEW_FIELDS = [
    "spec_id", "machine_status", "manual_status", "reviewer",
    "reviewed_at_utc", "screenshot_path", "notes",
]
EVIDENCE_FIELDS = {
    "schema_version", "spec_id", "spec_sha256", "checker_executable_path",
    "checker_executable_sha256", "checker_version", "moldir_sha256",
    "runner_sha256", "environment_receipt_sha256", "argv", "exit_code",
    "stdout_sha256", "stderr_sha256", "check_cif_sha256",
}
LOG_FILES = {
    "check.stdout.log", "check.stderr.log", "check.exit_code.txt",
    "check.execution.json",
}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
RANGE = re.compile(r"([1-9][0-9]*)\.\.([1-9][0-9]*)")
THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"duplicate key {key!r}", key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"BLOCKED_SPEC_VERIFICATION: {message}")


def regular_file(path: Path, label: str, *, nonempty: bool = True) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.is_symlink()
        or not path.is_file()
    ):
        fail(f"{label} must be a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    if path != resolved:
        fail(f"{label} path must be canonical without symlink hops: {path}")
    if nonempty and resolved.stat().st_size == 0:
        fail(f"{label} is empty: {path}")
    return resolved


def regular_directory(path: Path, label: str) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.is_symlink()
        or not path.is_dir()
    ):
        fail(f"{label} must be a non-symlink directory: {path}")
    resolved = path.resolve(strict=True)
    if path != resolved:
        fail(f"{label} path must be canonical without symlink hops: {path}")
    return resolved


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def replay_log_digest(content: bytes, replay_root: Path) -> str:
    """Hash replay logs after replacing the intentionally-random temp root."""
    normalized = content.replace(os.fsencode(replay_root), b"$REPLAY_ROOT")
    return digest_bytes(normalized)


def require_sha(value: str, label: str) -> str:
    if SHA256.fullmatch(value) is None:
        fail(f"invalid SHA-256 for {label}: {value!r}")
    return value


def safe_relative(value: str, label: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in value
        or candidate.as_posix() != value
    ):
        fail(f"unsafe {label}: {value!r}")
    return candidate


def canonical_posix_absolute(value: object, label: str) -> PurePosixPath:
    """Parse an already-canonical absolute POSIX path from execution evidence."""

    if not isinstance(value, str) or "\\" in value:
        fail(f"{label} must be a canonical absolute POSIX path")
    candidate = PurePosixPath(value)
    if (
        not candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts[1:])
        or candidate.as_posix() != value
    ):
        fail(f"{label} must be a canonical absolute POSIX path: {value!r}")
    return candidate


def output_path(path: Path, label: str) -> Path:
    """Require a canonical output leaf below an existing canonical parent."""

    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        fail(f"{label} must be an absolute canonical file path: {path}")
    parent = regular_directory(path.parent, f"{label} parent")
    canonical = parent / path.name
    if path != canonical or path.is_symlink():
        fail(f"{label} contains a non-canonical or symlink path: {path}")
    if path.exists() and (not path.is_file() or path.resolve(strict=True) != path):
        fail(f"{label} existing leaf is not a canonical regular file: {path}")
    return path


def rows_from_tsv(path: Path, expected_fields: list[str], label: str) -> list[dict[str, str]]:
    regular_file(path, label)
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != expected_fields:
                fail(f"{label} header differs: {reader.fieldnames!r}")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        fail(f"cannot read {label}: {error}")
    if any(set(row) != set(expected_fields) or None in row for row in rows):
        fail(f"malformed {label} row")
    return rows


def load_json(path: Path, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                fail(f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"cannot read {label}: {error}")
    if not isinstance(payload, dict):
        fail(f"{label} root must be an object")
    return payload


def load_yaml(path: Path, label: str) -> dict[str, Any]:
    regular_file(path, label)
    try:
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        fail(f"cannot parse {label}: {error}")
    if not isinstance(payload, dict):
        fail(f"{label} root must be a mapping")
    return payload


def parse_utc(value: str) -> None:
    if not value.endswith("Z"):
        fail(f"reviewed_at_utc must end in Z: {value!r}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        fail(f"invalid reviewed_at_utc: {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        fail(f"reviewed_at_utc is not UTC: {value!r}")


def parse_range(value: str, expected_length: str, label: str) -> set[int]:
    match = RANGE.fullmatch(value)
    if match is None:
        fail(f"non-canonical {label}: {value!r}")
    start, end = map(int, match.groups())
    if end < start or str(end - start + 1) != expected_length:
        fail(f"{label} length differs from manifest: {value!r}/{expected_length!r}")
    return set(range(start, end + 1))


def require_singleton_chain(
    value: object, outer_key: str, expected: dict[str, object], label: str,
) -> None:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        fail(f"{label} must contain exactly one mapping")
    if set(value[0]) != {outer_key} or value[0][outer_key] != expected:
        fail(f"{label} differs from frozen contract")


def parse_mmcif(path: Path, label: str) -> dict[str, object]:
    regular_file(path, label)
    try:
        document = gemmi.cif.read_file(str(path))
    except (OSError, RuntimeError) as error:
        fail(f"cannot parse {label} as mmCIF: {error}")
    if len(document) != 1:
        fail(f"{label} must contain exactly one data block")
    block = document.sole_block()

    def column(tag: str) -> list[str]:
        values = list(block.find_values(tag))
        if not values:
            fail(f"{label} is missing required mmCIF column {tag}")
        return values

    entity_ids = column("_entity_poly_seq.entity_id")
    sequence_numbers = column("_entity_poly_seq.num")
    monomers = column("_entity_poly_seq.mon_id")
    if not (len(entity_ids) == len(sequence_numbers) == len(monomers)):
        fail(f"{label} has ragged entity_poly_seq columns")
    entity_positions: dict[str, dict[int, str]] = {}
    for entity, number_text, monomer_text in zip(
        entity_ids, sequence_numbers, monomers, strict=True
    ):
        try:
            number = int(number_text)
        except ValueError:
            fail(f"{label} has non-integer entity_poly_seq.num")
        monomer = monomer_text.upper()
        if number <= 0 or monomer not in THREE_TO_ONE:
            fail(f"{label} has unsupported polymer residue {monomer_text!r}")
        positions = entity_positions.setdefault(entity, {})
        if number in positions:
            fail(f"{label} has duplicate entity/polymer position {entity}:{number}")
        positions[number] = THREE_TO_ONE[monomer]
    for entity, positions in entity_positions.items():
        if sorted(positions) != list(range(1, len(positions) + 1)):
            fail(f"{label} has non-contiguous polymer numbering for entity {entity}")

    asym_ids = column("_struct_asym.id")
    asym_entities = column("_struct_asym.entity_id")
    if len(asym_ids) != len(asym_entities) or len(asym_ids) != len(set(asym_ids)):
        fail(f"{label} has malformed struct_asym mapping")
    chain_entities = dict(zip(asym_ids, asym_entities, strict=True))
    if any(entity not in entity_positions for entity in chain_entities.values()):
        fail(f"{label} contains a non-polymer or unmapped structure chain")
    if set(chain_entities.values()) != set(entity_positions):
        fail(f"{label} contains an unused or hidden polymer entity")
    sequences = {
        chain: "".join(entity_positions[entity][index] for index in sorted(entity_positions[entity]))
        for chain, entity in chain_entities.items()
    }

    atom_tags = (
        "_atom_site.group_PDB", "_atom_site.label_asym_id",
        "_atom_site.label_entity_id", "_atom_site.label_seq_id",
        "_atom_site.label_atom_id", "_atom_site.label_comp_id",
        "_atom_site.Cartn_x", "_atom_site.Cartn_y", "_atom_site.Cartn_z",
        "_atom_site.occupancy", "_atom_site.B_iso_or_equiv",
        "_atom_site.pdbx_PDB_model_num",
    )
    atom_columns = [column(tag) for tag in atom_tags]
    atom_count = len(atom_columns[0])
    if atom_count == 0 or any(len(values) != atom_count for values in atom_columns):
        fail(f"{label} has empty or ragged atom_site columns")
    residue_atoms: dict[tuple[str, int], set[str]] = {}
    atom_flags: list[tuple[str, int, float]] = []
    seen_atoms: set[tuple[str, int, str]] = set()
    for values in zip(*atom_columns, strict=True):
        (
            group, chain, entity, number_text, atom_name, monomer,
            x_text, y_text, z_text, occupancy_text, b_text, model,
        ) = values
        if group != "ATOM" or model != "1" or chain not in chain_entities:
            fail(f"{label} contains a non-polymer, extra-model, or unknown-chain atom")
        if entity != chain_entities[chain]:
            fail(f"{label} atom/entity mapping differs for chain {chain}")
        try:
            number = int(number_text)
            coordinates = (float(x_text), float(y_text), float(z_text))
            occupancy = float(occupancy_text)
            b_factor = float(b_text)
        except ValueError:
            fail(f"{label} contains a non-numeric atom record")
        if not all(math.isfinite(item) for item in (*coordinates, occupancy, b_factor)):
            fail(f"{label} contains non-finite atom data")
        if not 0.0 <= occupancy <= 1.0:
            fail(f"{label} contains atom occupancy outside 0..1")
        positions = entity_positions[entity]
        if number not in positions or THREE_TO_ONE.get(monomer.upper()) != positions[number]:
            fail(f"{label} atom residue differs from entity_poly_seq")
        atom_key = (chain, number, atom_name)
        if atom_key in seen_atoms:
            fail(f"{label} has duplicate materialized atom {atom_key}")
        seen_atoms.add(atom_key)
        residue_atoms.setdefault((chain, number), set()).add(atom_name)
        atom_flags.append((chain, number, b_factor))
    expected_residues = {
        (chain, number)
        for chain, entity in chain_entities.items()
        for number in entity_positions[entity]
    }
    if set(residue_atoms) != expected_residues:
        fail(f"{label} does not materialize every declared polymer residue")
    if any(not {"N", "CA", "C"}.issubset(atoms) for atoms in residue_atoms.values()):
        fail(f"{label} has a polymer residue missing N/CA/C backbone atoms")
    return {
        "sha256": digest(path), "atom_count": atom_count, "sequences": sequences,
        "residue_atoms": residue_atoms, "atom_flags": atom_flags,
    }


def validate_spec_contract(
    row: dict[str, str], spec_dir: Path,
) -> tuple[str, set[int], dict[str, object], dict[str, object], str]:
    spec_id = row["spec_id"]
    if row["target_id"] != "GLP1_7-36_NH2" or row["target_chain"] != "E":
        fail(f"target identity/chain drift for {spec_id}")
    if row["binding_label_seq_ids"] != "1,2":
        fail(f"binding residue contract drift for {spec_id}")
    ranges: list[set[int]] = []
    previous_end = 0
    for index in range(1, 4):
        value = row[f"cdr{index}_range"]
        positions = parse_range(value, row[f"cdr{index}_length"], f"CDR{index} for {spec_id}")
        if min(positions) <= previous_end:
            fail(f"CDR ranges overlap or are out of order for {spec_id}")
        previous_end = max(positions)
        ranges.append(positions)
    design_positions = set().union(*ranges)

    design_path = spec_dir / "design.yaml"
    design = load_yaml(design_path, f"design YAML for {spec_id}")
    if set(design) != {"entities"} or not isinstance(design["entities"], list) or len(design["entities"]) != 2:
        fail(f"design YAML must contain exactly the frozen target+scaffold entities: {spec_id}")
    target_entity, scaffold_entity = design["entities"]
    if not isinstance(target_entity, dict) or set(target_entity) != {"file"} or not isinstance(target_entity["file"], dict):
        fail(f"invalid target entity in design YAML: {spec_id}")
    target_file = target_entity["file"]
    if set(target_file) != {"path", "include", "binding_types", "structure_groups"}:
        fail(f"target entity fields differ from frozen contract: {spec_id}")
    if target_file["path"] != "target.cif":
        fail(f"target entity path differs for {spec_id}")
    require_singleton_chain(target_file["include"], "chain", {"id": "E", "res_index": "1..30"}, f"target include for {spec_id}")
    require_singleton_chain(target_file["binding_types"], "chain", {"id": "E", "binding": "1..2"}, f"target binding_types for {spec_id}")
    require_singleton_chain(target_file["structure_groups"], "group", {"id": "E", "visibility": 1}, f"target structure_groups for {spec_id}")
    if scaffold_entity != {"file": {"path": "scaffold.yaml"}}:
        fail(f"scaffold entity reference differs for {spec_id}")

    scaffold_yaml_path = spec_dir / "scaffold.yaml"
    scaffold_yaml = load_yaml(scaffold_yaml_path, f"scaffold YAML for {spec_id}")
    if scaffold_yaml.get("path") != "scaffold.cif":
        fail(f"scaffold YAML path differs for {spec_id}")
    include = scaffold_yaml.get("include")
    if not isinstance(include, list) or len(include) != 1 or not isinstance(include[0], dict):
        fail(f"scaffold include is malformed for {spec_id}")
    chain_record = include[0].get("chain")
    if not isinstance(chain_record, dict) or set(chain_record) != {"id"}:
        fail(f"scaffold include chain is malformed for {spec_id}")
    scaffold_chain = chain_record["id"]
    if not isinstance(scaffold_chain, str) or SAFE_ID.fullmatch(scaffold_chain) is None or scaffold_chain == "E":
        fail(f"unsafe or colliding scaffold chain for {spec_id}")
    ranges_text = ",".join(row[f"cdr{i}_range"] for i in range(1, 4))
    require_singleton_chain(scaffold_yaml.get("design"), "chain", {"id": scaffold_chain, "res_index": ranges_text}, f"scaffold design for {spec_id}")
    required_groups = [
        {"group": {"id": scaffold_chain, "visibility": 2}},
        {"group": {"id": scaffold_chain, "visibility": 0, "res_index": ranges_text}},
    ]
    if scaffold_yaml.get("structure_groups") != required_groups:
        fail(f"scaffold structure_groups differs from frozen fixed-framework contract: {spec_id}")
    require_singleton_chain(scaffold_yaml.get("reset_res_index"), "chain", {"id": scaffold_chain}, f"scaffold reset_res_index for {spec_id}")

    target_summary = parse_mmcif(spec_dir / "target.cif", f"target CIF for {spec_id}")
    scaffold_summary = parse_mmcif(spec_dir / "scaffold.cif", f"scaffold CIF for {spec_id}")
    if set(target_summary["sequences"]) != {"E"} or len(target_summary["sequences"]["E"]) != 30:
        fail(f"target CIF must contain exactly chain E with 30 residues: {spec_id}")
    if set(scaffold_summary["sequences"]) != {scaffold_chain}:
        fail(f"scaffold CIF chain differs from scaffold YAML: {spec_id}")
    if not design_positions or max(design_positions) > len(scaffold_summary["sequences"][scaffold_chain]):
        fail(f"CDR range exceeds scaffold polymer for {spec_id}")
    return scaffold_chain, design_positions, target_summary, scaffold_summary, digest(scaffold_yaml_path)


def validate_check_cif(
    path: Path, spec_id: str, target_chain: str, scaffold_chain: str,
    design_positions: set[int], target_summary: dict[str, object],
    scaffold_summary: dict[str, object],
) -> dict[str, object]:
    summary = parse_mmcif(path, f"check CIF for {spec_id}")
    sequences = summary["sequences"]
    if set(sequences) != {target_chain, scaffold_chain}:
        fail(f"check CIF has extra/missing target or scaffold chains for {spec_id}")
    if sequences[target_chain] != target_summary["sequences"][target_chain]:
        fail(f"check CIF target sequence differs from target.cif for {spec_id}")
    if sequences[scaffold_chain] != scaffold_summary["sequences"][scaffold_chain]:
        fail(f"check CIF scaffold sequence differs from scaffold.cif for {spec_id}")
    for chain, number, observed in summary["atom_flags"]:
        expected = 80.0 if chain == target_chain and number in {1, 2} else 0.0
        if chain == scaffold_chain:
            expected = 100.0 if number in design_positions else 0.0
        if not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-6):
            fail(f"check CIF binding/design flag differs for {spec_id}: {chain}:{number}")
    return summary


def only_check_cif(directory: Path, spec_id: str) -> Path:
    regular_directory(directory, f"check directory for {spec_id}")
    files: list[Path] = []
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            fail(f"symlink forbidden in check output: {item}")
        if item.is_file():
            files.append(item)
        elif not item.is_dir():
            fail(f"special file forbidden in check output: {item}")
    cifs = [item for item in files if item.suffix.lower() in {".cif", ".mmcif"}]
    if len(files) != 1 or len(cifs) != 1:
        fail(f"{spec_id} check output must contain exactly one CIF/mmCIF")
    return regular_file(cifs[0], f"check CIF for {spec_id}")


def validate_execution_evidence(
    log_root: Path, spec_id: str, row: dict[str, str], check_cif: Path,
    checker_sha: str, checker_version: str, moldir_sha: str,
    runner_sha: str, environment_sha: str,
) -> tuple[dict[str, Any], str]:
    directory = regular_directory(log_root / spec_id, f"check log directory for {spec_id}")
    observed = {item.name for item in directory.iterdir()}
    if observed != LOG_FILES or any(item.is_symlink() or not item.is_file() for item in directory.iterdir()):
        fail(f"check log directory fields differ for {spec_id}: {sorted(observed)}")
    stdout_path = regular_file(directory / "check.stdout.log", f"check stdout for {spec_id}")
    stderr_path = regular_file(directory / "check.stderr.log", f"check stderr for {spec_id}", nonempty=False)
    exit_path = regular_file(directory / "check.exit_code.txt", f"check exit for {spec_id}")
    evidence_path = regular_file(directory / "check.execution.json", f"check evidence for {spec_id}")
    if exit_path.read_bytes() != b"0\n":
        fail(f"check exit code is not canonical zero for {spec_id}")
    evidence = load_json(evidence_path, f"check evidence for {spec_id}")
    if set(evidence) != EVIDENCE_FIELDS or evidence["schema_version"] != "BOLTZGEN_CHECK_EXECUTION_V1":
        fail(f"check evidence schema/version differs for {spec_id}")
    expected_scalars = {
        "spec_id": spec_id, "spec_sha256": row["spec_sha256"],
        "checker_executable_sha256": checker_sha, "checker_version": checker_version,
        "moldir_sha256": moldir_sha, "runner_sha256": runner_sha,
        "environment_receipt_sha256": environment_sha, "exit_code": 0,
        "stdout_sha256": digest(stdout_path), "stderr_sha256": digest(stderr_path),
        "check_cif_sha256": digest(check_cif),
    }
    for field, expected in expected_scalars.items():
        if evidence[field] != expected:
            fail(f"check evidence field {field} differs for {spec_id}")
    executable_path = evidence["checker_executable_path"]
    argv = evidence["argv"]
    checker_path = canonical_posix_absolute(
        executable_path, f"checker executable path for {spec_id}"
    )
    if checker_path.parts[-3:] != ("env", "bin", "boltzgen"):
        fail(f"checker executable path is outside the canonical env layout for {spec_id}")
    remote_root = checker_path.parent.parent.parent
    expected_argv = [
        checker_path.as_posix(),
        "check",
        (remote_root / "project_input" / "specs" / spec_id / "design.yaml").as_posix(),
        "--output",
        (remote_root / "project_input" / "check_outputs" / spec_id).as_posix(),
        "--moldir",
        (remote_root / "runtime_cache" / "mols.zip").as_posix(),
    ]
    if not isinstance(argv, list) or argv != expected_argv:
        fail(f"check evidence argv is not the canonical T4 command for {spec_id}")
    return evidence, digest(evidence_path)


def run_replay(
    checker: Path, moldir: Path, design: Path, replay_output: Path, spec_id: str,
) -> tuple[subprocess.CompletedProcess[bytes], Path, list[str]]:
    command = [str(checker), "check", str(design), "--output", str(replay_output), "--moldir", str(moldir)]
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
        environment.pop(name, None)
    environment.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        result = subprocess.run(command, cwd=design.parent, env=environment, capture_output=True, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"cannot replay boltzgen check for {spec_id}: {error}")
    if result.returncode != 0:
        fail(f"replayed boltzgen check returned {result.returncode} for {spec_id}")
    return result, only_check_cif(replay_output, spec_id), command


def publish_json(path: Path, payload: dict[str, object]) -> None:
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            fail(f"immutable output differs: {path}")
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                fail(f"immutable output differs: {path}")
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-manifest", required=True)
    parser.add_argument("--check-root", required=True)
    parser.add_argument("--check-log-root")
    parser.add_argument("--manual-review", required=True)
    parser.add_argument("--expected-target-sha256", required=True)
    parser.add_argument("--boltzgen-executable")
    parser.add_argument("--expected-boltzgen-sha256")
    parser.add_argument("--moldir")
    parser.add_argument("--expected-moldir-sha256")
    parser.add_argument("--check-runner")
    parser.add_argument("--expected-check-runner-sha256")
    parser.add_argument("--environment-receipt")
    parser.add_argument("--expected-environment-receipt-sha256")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    t4_fields = {
        "--check-log-root": args.check_log_root,
        "--boltzgen-executable": args.boltzgen_executable,
        "--expected-boltzgen-sha256": args.expected_boltzgen_sha256,
        "--moldir": args.moldir,
        "--expected-moldir-sha256": args.expected_moldir_sha256,
        "--check-runner": args.check_runner,
        "--expected-check-runner-sha256": args.expected_check_runner_sha256,
        "--environment-receipt": args.environment_receipt,
        "--expected-environment-receipt-sha256": args.expected_environment_receipt_sha256,
    }
    missing = [field for field, value in t4_fields.items() if value is None]
    if missing:
        fail("current frozen artifacts do not prove boltzgen check execution; T4 runner must provide " + ", ".join(missing))
    expected_target_sha = require_sha(args.expected_target_sha256, "expected target")
    expected_checker_sha = require_sha(args.expected_boltzgen_sha256, "expected boltzgen")
    expected_moldir_sha = require_sha(args.expected_moldir_sha256, "expected moldir")
    expected_runner_sha = require_sha(args.expected_check_runner_sha256, "expected check runner")
    expected_environment_sha = require_sha(args.expected_environment_receipt_sha256, "expected environment receipt")

    manifest = regular_file(Path(args.spec_manifest), "spec manifest")
    review_path = regular_file(Path(args.manual_review), "manual review")
    check_root = regular_directory(Path(args.check_root), "check root")
    log_root = regular_directory(Path(args.check_log_root), "check log root")
    checker = regular_file(Path(args.boltzgen_executable), "boltzgen executable")
    if not os.access(checker, os.X_OK) or digest(checker) != expected_checker_sha:
        fail("boltzgen executable is not executable or differs from expected SHA-256")
    moldir = regular_file(Path(args.moldir), "mols.zip")
    if digest(moldir) != expected_moldir_sha:
        fail("mols.zip differs from expected SHA-256")
    runner = regular_file(Path(args.check_runner), "T4 check runner")
    if digest(runner) != expected_runner_sha:
        fail("T4 check runner differs from expected SHA-256")
    environment_receipt = regular_file(Path(args.environment_receipt), "formal environment receipt")
    if digest(environment_receipt) != expected_environment_sha:
        fail("formal environment receipt differs from expected SHA-256")
    output = output_path(Path(args.output), "verification output")

    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
        environment.pop(name, None)
    try:
        version_result = subprocess.run([str(checker), "--version"], env=environment, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"cannot execute boltzgen --version: {error}")
    checker_version = version_result.stdout.strip()
    if version_result.returncode != 0 or checker_version != "boltzgen 0.3.2":
        fail(f"checker is not exactly boltzgen 0.3.2: {checker_version!r}")

    manifest_rows = rows_from_tsv(manifest, MANIFEST_FIELDS, "spec manifest")
    review_rows = rows_from_tsv(review_path, REVIEW_FIELDS, "manual review")
    if len(manifest_rows) != 12 or len(review_rows) != 12:
        fail("the frozen contract requires exactly 12 manifest and review rows")
    spec_ids = [row["spec_id"] for row in manifest_rows]
    scaffold_ids = [row["scaffold_id"] for row in manifest_rows]
    if len(set(spec_ids)) != 12 or len(set(scaffold_ids)) != 12 or any(SAFE_ID.fullmatch(item) is None for item in spec_ids + scaffold_ids):
        fail("spec/scaffold IDs must be 12 unique safe identifiers")
    roles = [row["scaffold_role"] for row in manifest_rows]
    if roles.count("PRIMARY") != 10 or roles.count("RESERVE") != 2 or any(role not in {"PRIMARY", "RESERVE"} for role in roles):
        fail("scaffold roles must be exactly PRIMARY=10 and RESERVE=2")
    review_by_id = {row["spec_id"]: row for row in review_rows}
    if len(review_by_id) != 12 or set(review_by_id) != set(spec_ids):
        fail("manual-review spec set differs from manifest")
    for root, label in ((check_root, "check root"), (log_root, "check log root")):
        observed = {path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()}
        if observed != set(spec_ids) or any(not path.is_dir() or path.is_symlink() for path in root.iterdir()):
            fail(f"{label} must contain exactly the twelve non-symlink spec directories")

    inputs_root = manifest.parent
    campaign_root = inputs_root.parent
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix=".verify_specs.replay.", dir=output.parent) as temporary:
        replay_root = Path(temporary)
        for row in manifest_rows:
            spec_id = row["spec_id"]
            for field in ("spec_sha256", "scaffold_sha256", "target_sha256"):
                require_sha(row[field], f"{field} for {spec_id}")
            relative_design = safe_relative(row["spec_path"], "spec path")
            if relative_design != PurePosixPath("specs", spec_id, "design.yaml"):
                fail(f"non-canonical spec path for {spec_id}: {relative_design}")
            spec_dir = regular_directory(inputs_root / "specs" / spec_id, f"spec directory {spec_id}")
            observed = {path.name for path in spec_dir.iterdir()}
            if observed != {"design.yaml", "scaffold.cif", "scaffold.yaml", "target.cif"} or any(path.is_symlink() or not path.is_file() for path in spec_dir.iterdir()):
                fail(f"spec directory artifact set differs for {spec_id}")
            design_path = regular_file(inputs_root / Path(*relative_design.parts), f"design for {spec_id}")
            scaffold_path = regular_file(spec_dir / "scaffold.cif", f"scaffold for {spec_id}")
            target_path = regular_file(spec_dir / "target.cif", f"target for {spec_id}")
            if digest(design_path) != row["spec_sha256"] or digest(scaffold_path) != row["scaffold_sha256"]:
                fail(f"spec or scaffold hash drift for {spec_id}")
            target_sha = digest(target_path)
            if target_sha != row["target_sha256"] or target_sha != expected_target_sha:
                fail(f"target hash drift for {spec_id}")

            scaffold_chain, design_positions, target_summary, scaffold_summary, scaffold_yaml_sha = validate_spec_contract(row, spec_dir)
            archived_cif = only_check_cif(check_root / spec_id, spec_id)
            archived_summary = validate_check_cif(archived_cif, spec_id, "E", scaffold_chain, design_positions, target_summary, scaffold_summary)
            evidence, evidence_sha = validate_execution_evidence(log_root, spec_id, row, archived_cif, expected_checker_sha, checker_version, expected_moldir_sha, expected_runner_sha, expected_environment_sha)
            replay_result, replay_cif, _replay_command = run_replay(checker, moldir, design_path, replay_root / spec_id, spec_id)
            replay_summary = validate_check_cif(replay_cif, spec_id, "E", scaffold_chain, design_positions, target_summary, scaffold_summary)
            if replay_summary["sha256"] != archived_summary["sha256"]:
                fail(f"replayed boltzgen check output differs from frozen output for {spec_id}")

            review = review_by_id[spec_id]
            if review["machine_status"] != "PASS" or review["manual_status"] != "PASS":
                fail(f"machine/manual review is not PASS for {spec_id}")
            if not review["reviewer"].strip():
                fail(f"missing reviewer for {spec_id}")
            parse_utc(review["reviewed_at_utc"])
            screenshot_rel = safe_relative(review["screenshot_path"], "screenshot path")
            screenshot = regular_file(campaign_root / Path(*screenshot_rel.parts), f"screenshot for {spec_id}")
            try:
                screenshot.relative_to(campaign_root.resolve(strict=True))
            except ValueError:
                fail(f"screenshot escapes campaign root for {spec_id}")
            results.append({
                "spec_id": spec_id, "spec_sha256": row["spec_sha256"],
                "scaffold_sha256": row["scaffold_sha256"],
                "scaffold_yaml_sha256": scaffold_yaml_sha, "target_sha256": target_sha,
                "check_cif_sha256": archived_summary["sha256"],
                "check_cif_size_bytes": archived_cif.stat().st_size,
                "check_cif_atom_count": archived_summary["atom_count"],
                "machine_evidence_sha256": evidence_sha,
                "machine_stdout_sha256": evidence["stdout_sha256"],
                "machine_stderr_sha256": evidence["stderr_sha256"],
                "replay_stdout_sha256": replay_log_digest(replay_result.stdout, replay_root),
                "replay_stderr_sha256": replay_log_digest(replay_result.stderr, replay_root),
                "replay_argv_contract": [
                    "boltzgen", "check", f"specs/{spec_id}/design.yaml", "--output",
                    f"check_outputs/{spec_id}", "--moldir", "runtime_cache/mols.zip",
                ],
                "screenshot_sha256": digest(screenshot),
                "reviewer": review["reviewer"], "reviewed_at_utc": review["reviewed_at_utc"],
                "status": "PASS",
            })

    payload: dict[str, object] = {
        "schema_version": "t3_spec_verification_v2", "status": "PASS",
        "spec_count": 12, "machine_pass_count": 12, "manual_pass_count": 12,
        "spec_manifest_sha256": digest(manifest), "manual_review_sha256": digest(review_path),
        "expected_target_sha256": expected_target_sha,
        "checker_executable_sha256": expected_checker_sha, "checker_version": checker_version,
        "moldir_sha256": expected_moldir_sha, "check_runner_sha256": expected_runner_sha,
        "environment_receipt_sha256": expected_environment_sha, "specs": results,
    }
    publish_json(output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
