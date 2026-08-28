#!/usr/bin/env python3
"""Validate AIV1 inputs and build the deterministic 10 x 16 task matrix.

This module is deliberately fail-closed.  It does not discover structures by
directory scan, does not infer labels from path names, and does not fabricate
the ten G2 anchor candidates.  A real matrix can only be emitted when an
official Linux/NVIDIA G2 receipt and all referenced artifacts are present.

Code source: project_original.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np


SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
SEQUENCE_RE = re.compile(r"[ACDEFGHIKLMNPQRSTVWY]+")

G2_CANDIDATE_COUNT = 10
G2_FOLD_SAMPLE_COUNT = 5
G2_FOLD_SCORE_FIELDS = (
    "iptm",
    "ptm",
    "design_to_target_iptm",
    "design_ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
)

STATE_FIELDS = (
    "state_order",
    "target_state_id",
    "panel_role",
    "target_identity",
    "source_id",
    "source_pdb",
    "cohort_id",
    "conformer_id",
    "independence_group",
    "relative_path",
    "sha256",
    "coordinate_sha256",
    "required_status",
    "required_active_for_ai",
    "required_parse_status",
    "required_geometry_complete",
    "compact_cluster_weight",
)

ANCHOR_FIELDS = (
    "anchor_order",
    "candidate_id",
    "full_sequence",
    "full_sequence_sha256",
    "generation_cell_id",
    "shard_id",
    "scaffold_id",
    "checkpoint_id",
    "candidate_artifact_uri",
    "candidate_artifact_sha256",
    "config_sha256",
    "code_sha256",
    "environment_sha256",
    "rng_seed_status",
    "rng_seed",
)

TASK_FIELDS = (
    "task_id",
    "campaign_id",
    "stage",
    "candidate_index",
    "candidate_id",
    "full_sequence_sha256",
    "candidate_artifact_sha256",
    "generation_cell_id",
    "shard_id",
    "scaffold_id",
    "scaffold_sha256",
    "checkpoint_id",
    "checkpoint_sha256",
    "config_sha256",
    "code_sha256",
    "environment_sha256",
    "rng_seed_status",
    "rng_seed",
    "target_state_id",
    "target_identity",
    "source_deposition",
    "independence_group",
    "conformer_id",
    "data_partition",
    "panel_role",
    "compact_cluster_weight",
    "target_logical_path",
    "target_sha256",
    "target_coordinate_sha256",
    "fold_run",
    "sample_count",
    "execution_mode",
    "expected",
    "lockbox_access",
)

G2_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "gate_id",
        "status",
        "source_stage",
        "generation_cell_id",
        "shard_id",
        "scaffold_id",
        "scaffold_sha256",
        "checkpoint_id",
        "checkpoint_sha256",
        "platform_class",
        "platform_evidence_uri",
        "platform_evidence_sha256",
        "g2_acceptance_gate_uri",
        "g2_acceptance_gate_sha256",
        "g2_resource_probe_status_uri",
        "g2_resource_probe_status_sha256",
        "aggregate_metrics_uri",
        "aggregate_metrics_sha256",
        "candidate_count",
        "candidate_id_set_sha256",
        "anchor_manifest_sha256",
        "aiv0_final_check_receipt_sha256",
        "config_sha256",
        "code_sha256",
        "environment_sha256",
        "output_manifest_uri",
        "output_manifest_sha256",
        "completed_at_utc",
    }
)

G2_ACCEPTANCE_GATE_FIELDS = frozenset(
    {
        "status",
        "spec_gate_bundle_sha256",
        "acceptance_success_sha256",
        "probe_success_sha256",
        "output_manifest_sha256",
        "resolved_config_manifest_sha256",
        "peak_memory_fraction",
        "resource_summary_sha256",
    }
)

PLATFORM_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "os_family",
        "architecture",
        "accelerator_vendor",
        "cuda_available",
        "nvidia_smi_exit_code",
        "gpu_name",
        "driver_version",
        "cuda_runtime_version",
        "gpu_compute_capability",
        "bfloat16_supported",
        "environment_sha256",
        "collected_at_utc",
    }
)

CANONICAL_INPUT_CONTRACT_RELATIVE = Path(
    "boltzgen/resources/data/AIV1技术门合同_20260828/aiv1_input_contract.json"
)
CANONICAL_STATE_CONTRACT_RELATIVE = Path(
    "boltzgen/resources/data/AIV1技术门合同_20260828/development_state_contract.tsv"
)
CANONICAL_REGISTRY_SCHEMA_RELATIVE = Path(
    "boltzgen/resources/data/AIV1技术门合同_20260828/aiv1_experience_registry_schema.sql"
)
CANONICAL_AIV0_SUMMARY_RELATIVE = Path(
    "boltzgen/resources/data/AI结构资产验证登记册_20260828/AIV0验证摘要_20260828.json"
)
REGISTERED_STATE_ALIASES = {
    Path("data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820"): (
        Path("boltzgen/runs/old12_glp1_mac_enhanced_20260820"),
        "../../boltzgen/runs/old12_glp1_mac_enhanced_20260820",
    ),
    Path("data/样本数据/binding-多构象"): (
        Path("shared/data/glp1_positive_conformer_ensemble_20260819"),
        "../../shared/data/glp1_positive_conformer_ensemble_20260819",
    ),
    Path("data/not_binding"): (
        Path("shared/data/glp2_tuning_countertargets_20260824"),
        "../shared/data/glp2_tuning_countertargets_20260824",
    ),
}

EXPECTED_INPUT_CONTRACT_STATIC = {
    "schema_version": "AIV1_INPUT_CONTRACT_V1",
    "campaign_type": "AIV1_TECHNICAL_GATE",
    "candidate_count": 10,
    "logical_states_per_candidate": 16,
    "fold_run": 1,
    "samples_per_task": 5,
    "expected_logical_tasks": 160,
    "expected_sample_rows": 800,
    "generation_contract": {
        "source_stage": "STEP8_G2_ACCEPTANCE",
        "generation_cell_id": "7xl0_adherence__attempt_001",
        "shard_id": "acceptance",
        "scaffold_id": "01_pdb_00007xl0-A",
        "scaffold_sha256": "68a4c9545a51c56f652c503c94e572e035556998bb3a83d78b99ad80ae1a97d2",
        "checkpoint_id": "adherence",
        "checkpoint_sha256": "ac7078b3dc13064c68e0c3fd542e5bc538c33558bf6607f65e499eb336ca5e5d",
        "platform_class": "LINUX_NVIDIA",
    },
    "state_role_counts": {
        "positive_primary": 1,
        "positive_fixed_control": 1,
        "positive_compact_medoid": 3,
        "tuning_primary_truncation": 1,
        "tuning_family_glp2": 10,
    },
    "allowed_cohorts": [
        "positive_6x18_reference",
        "positive_1d0r_all",
        "countertarget_glp1_9_36_9ivm",
        "challenge_glp2_2l63",
    ],
    "forbidden_cohort_markers": ["lockbox", "quarantine"],
    "forbidden_target_markers": ["2B4N", "6LMK", "GIP", "GLUCAGON"],
    "execution_mode": "REFOLD_REQUIRED",
    "scientific_boundary": {
        "proves_only": "the AIV1 input and execution contract is technically replayable",
        "does_not_prove": [
            "binding",
            "non-binding",
            "affinity or KD",
            "selectivity",
            "cross-scaffold generalization",
            "experimental success",
        ],
    },
}

THREE_TO_ONE = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLN": "Q",
    "GLU": "E",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "PHE": "F",
    "PRO": "P",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
}


class ContractViolation(ValueError):
    """A machine-readable, fail-closed input contract violation."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedInputs:
    """Inputs that have passed all AIV1 matrix preconditions."""

    input_contract: Mapping[str, object]
    states: tuple[Mapping[str, str], ...]
    anchors: tuple[Mapping[str, str], ...]
    aiv0_handoff: Mapping[str, object]
    g2_receipt: Mapping[str, object]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def require_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ContractViolation(
            "BLOCKED_INPUT_TYPE", f"{label} must not be a symlink: {path}"
        )
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ContractViolation("BLOCKED_MISSING_INPUT", f"{label}: {path}") from error
    if not resolved.is_file():
        raise ContractViolation(
            "BLOCKED_INPUT_TYPE", f"{label} must be a non-symlink regular file: {path}"
        )
    return resolved


def require_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ContractViolation("BLOCKED_MISSING_INPUT", f"{label}: {path}") from error
    if not resolved.is_dir():
        raise ContractViolation("BLOCKED_INPUT_TYPE", f"{label}: {path}")
    return resolved


def _validate_relative_path(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ContractViolation("BLOCKED_INVALID_URI", f"{label} is empty")
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ContractViolation("BLOCKED_INVALID_URI", f"{label} has control bytes")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ContractViolation("BLOCKED_PATH_ESCAPE", f"unsafe {label}: {value}")
    if any(part in {"", "."} for part in relative.parts):
        raise ContractViolation(
            "BLOCKED_INVALID_URI", f"non-canonical {label}: {value}"
        )
    return relative


def resolve_relative_file(base: Path, relative_value: str, label: str) -> Path:
    """Resolve a regular file below ``base`` without following any symlink hop."""

    resolved_base = require_directory(base, f"{label} base")
    relative = _validate_relative_path(relative_value, label)
    current = resolved_base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ContractViolation(
                "BLOCKED_INPUT_TYPE", f"{label} traverses a symlink: {current}"
            )
    resolved = require_file(current, label)
    try:
        resolved.relative_to(resolved_base)
    except ValueError as error:
        raise ContractViolation(
            "BLOCKED_PATH_ESCAPE", f"{label}: {relative_value}"
        ) from error
    return resolved


def resolve_state_target_file(
    workspace_root: Path, relative_value: str, label: str
) -> Path:
    """Resolve a state file through only the exact AIV0-registered aliases."""

    relative = _validate_relative_path(relative_value, label)
    matched: tuple[Path, Path, str] | None = None
    suffix: Path | None = None
    for alias_relative, (
        target_relative,
        link_text,
    ) in REGISTERED_STATE_ALIASES.items():
        try:
            suffix = relative.relative_to(alias_relative)
        except ValueError:
            continue
        matched = (alias_relative, target_relative, link_text)
        break
    if matched is None or suffix is None:
        return resolve_relative_file(workspace_root, relative_value, label)
    alias_relative, target_relative, link_text = matched

    workspace = require_directory(workspace_root, "workspace root")
    current = workspace
    for part in alias_relative.parent.parts:
        current = current / part
        if current.is_symlink():
            raise ContractViolation(
                "BLOCKED_INPUT_TYPE", f"registered alias parent is a symlink: {current}"
            )
    alias = workspace / alias_relative
    if not alias.is_symlink() or os.readlink(alias) != link_text:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            f"registered compatibility alias target/text drifted: {alias_relative}",
        )
    target_root = workspace / target_relative
    if alias.resolve(strict=True) != target_root.resolve(strict=True):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            f"registered compatibility alias resolves to wrong target: {alias_relative}",
        )
    return resolve_relative_file(target_root, suffix.as_posix(), label)


def require_canonical_repo_file(
    path: Path, *, repo_root: Path, relative: Path, label: str
) -> Path:
    expected = resolve_relative_file(repo_root, relative.as_posix(), label)
    actual = require_file(path, label)
    if actual != expected:
        raise ContractViolation(
            "BLOCKED_NONCANONICAL_PATH",
            f"{label} must be repo://{relative.as_posix()}",
        )
    return actual


def require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContractViolation("BLOCKED_INVALID_HASH", f"{label}: {value!r}")
    return value


def load_json(path: Path, label: str) -> Mapping[str, object]:
    resolved = require_file(path, label)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractViolation("BLOCKED_INVALID_JSON", f"{label}: {path}") from error
    if not isinstance(payload, dict):
        raise ContractViolation("BLOCKED_INVALID_JSON", f"{label} must be an object")
    return payload


def load_sha256_manifest(path: Path, label: str) -> Mapping[str, str]:
    """Parse a strict ``sha256<two spaces>relative-path`` manifest."""

    resolved = require_file(path, label)
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ContractViolation("BLOCKED_INVALID_UTF8", f"{label}: {path}") from error
    if not lines:
        raise ContractViolation("BLOCKED_SCHEMA_MISMATCH", f"{label} is empty")
    members: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n\0]+)", line)
        if match is None:
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH", f"{label} malformed line {number}"
            )
        digest, name = match.groups()
        member = Path(name)
        if member.is_absolute() or ".." in member.parts or name in members:
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH", f"{label} unsafe/duplicate member: {name}"
            )
        members[name] = digest
    return members


def load_g2_output_manifest(path: Path) -> Mapping[str, str]:
    """Read the Step-8 output manifest and normalize its leading ``./``."""

    resolved = require_file(path, "G2 output manifest")
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ContractViolation(
            "BLOCKED_INVALID_UTF8", "G2 output manifest is not UTF-8"
        ) from error
    members: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64})[ \t]+(?:\*| )?(\./[^\r\n\0]+)", line)
        if match is None:
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH",
                f"G2 output manifest malformed line {number}",
            )
        digest, raw_name = match.groups()
        name = raw_name.removeprefix("./")
        member = Path(name)
        if member.is_absolute() or ".." in member.parts or name in members:
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH",
                f"G2 output manifest unsafe/duplicate member: {name}",
            )
        members[name] = digest
    if not members:
        raise ContractViolation(
            "BLOCKED_SCHEMA_MISMATCH", "G2 output manifest is empty"
        )
    return members


def mmcif_tokens(text: str) -> list[str]:
    """Tokenize the subset of STAR/mmCIF needed for entity-poly sequences."""

    import shlex

    tokens: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(";"):
            block = [line[1:]]
            index += 1
            while index < len(lines) and not lines[index].startswith(";"):
                block.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ContractViolation(
                    "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                    "unterminated mmCIF text block",
                )
            tokens.append("\n".join(block))
            index += 1
            continue
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens.extend(list(lexer))
        except ValueError as error:
            raise ContractViolation(
                "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH", "invalid mmCIF quoting"
            ) from error
        index += 1
    return tokens


def extract_mmcif_canonical_sequences(path: Path) -> set[str]:
    """Extract polymer sequences from canonical or residue-level mmCIF fields."""

    resolved = require_file(path, "candidate mmCIF")
    if resolved.suffix.casefold() != ".cif":
        raise ContractViolation(
            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH", "candidate artifact must be .cif"
        )
    try:
        tokens = mmcif_tokens(resolved.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise ContractViolation(
            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH", "candidate mmCIF is not UTF-8"
        ) from error
    canonical_key = "_entity_poly.pdbx_seq_one_letter_code_can"
    entity_key = "_entity_poly_seq.entity_id"
    number_key = "_entity_poly_seq.num"
    monomer_key = "_entity_poly_seq.mon_id"
    values: list[str] = []
    residue_sequences: dict[str, dict[int, str]] = {}
    index = 0
    control = {"loop_", "stop_", "global_"}
    while index < len(tokens):
        token = tokens[index]
        if token == canonical_key:
            if index + 1 >= len(tokens):
                break
            values.append(tokens[index + 1])
            index += 2
            continue
        if token.casefold() == "loop_":
            index += 1
            headers: list[str] = []
            while index < len(tokens) and tokens[index].startswith("_"):
                headers.append(tokens[index])
                index += 1
            if not headers:
                continue
            row_tokens: list[str] = []
            while index < len(tokens):
                current = tokens[index]
                lowered = current.casefold()
                if (
                    current.startswith("_")
                    or lowered in control
                    or lowered == "loop_"
                    or lowered.startswith("data_")
                    or lowered.startswith("save_")
                ):
                    break
                row_tokens.append(current)
                index += 1
            if len(row_tokens) % len(headers) != 0:
                raise ContractViolation(
                    "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                    "mmCIF entity loop has an incomplete row",
                )
            if canonical_key in headers:
                sequence_index = headers.index(canonical_key)
                values.extend(
                    row_tokens[offset + sequence_index]
                    for offset in range(0, len(row_tokens), len(headers))
                )
            required_residue_headers = {entity_key, number_key, monomer_key}
            if required_residue_headers.issubset(headers):
                entity_index = headers.index(entity_key)
                number_index = headers.index(number_key)
                monomer_index = headers.index(monomer_key)
                for offset in range(0, len(row_tokens), len(headers)):
                    entity = row_tokens[offset + entity_index]
                    try:
                        number = int(row_tokens[offset + number_index])
                    except ValueError as error:
                        raise ContractViolation(
                            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                            "mmCIF entity_poly_seq.num is not an integer",
                        ) from error
                    monomer = row_tokens[offset + monomer_index].upper()
                    one_letter = THREE_TO_ONE.get(monomer)
                    if one_letter is None:
                        raise ContractViolation(
                            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                            f"unsupported mmCIF protein monomer: {monomer}",
                        )
                    positions = residue_sequences.setdefault(entity, {})
                    if number in positions and positions[number] != one_letter:
                        raise ContractViolation(
                            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                            "conflicting mmCIF entity_poly_seq residue",
                        )
                    positions[number] = one_letter
            continue
        index += 1
    normalized = {re.sub(r"\s+", "", value).upper() for value in values}
    normalized.update(
        "".join(positions[number] for number in sorted(positions))
        for positions in residue_sequences.values()
        if positions
    )
    if not normalized or any(
        SEQUENCE_RE.fullmatch(value) is None for value in normalized
    ):
        raise ContractViolation(
            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
            "candidate mmCIF lacks valid canonical entity-poly sequence",
        )
    return normalized


def read_tsv(
    path: Path,
    *,
    label: str,
    exact_fields: Sequence[str] | None = None,
    required_fields: Iterable[str] = (),
) -> list[dict[str, str]]:
    resolved = require_file(path, label)
    try:
        with resolved.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = tuple(reader.fieldnames or ())
            if exact_fields is not None and fields != tuple(exact_fields):
                raise ContractViolation(
                    "BLOCKED_SCHEMA_MISMATCH",
                    f"{label} header {fields!r} != {tuple(exact_fields)!r}",
                )
            missing = sorted(set(required_fields) - set(fields))
            if missing:
                raise ContractViolation(
                    "BLOCKED_SCHEMA_MISMATCH", f"{label} missing fields: {missing}"
                )
            rows = [dict(row) for row in reader]
    except UnicodeDecodeError as error:
        raise ContractViolation("BLOCKED_INVALID_UTF8", f"{label}: {path}") from error
    for index, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH", f"{label} malformed TSV row {index}"
            )
    return rows


def resolve_uri(uri: str, *, repo_root: Path, workspace_root: Path, label: str) -> Path:
    if uri.startswith("repo://"):
        base = repo_root.resolve(strict=True)
        suffix = uri.removeprefix("repo://")
    elif uri.startswith("workspace://"):
        base = workspace_root.resolve(strict=True)
        suffix = uri.removeprefix("workspace://")
    else:
        raise ContractViolation(
            "BLOCKED_INVALID_URI", f"{label} must use repo:// or workspace://: {uri}"
        )
    return resolve_relative_file(base, suffix, label)


def canonical_uri(path: Path, *, repo_root: Path, workspace_root: Path) -> str:
    resolved = path.resolve(strict=True)
    for prefix, root in (
        ("repo", repo_root.resolve(strict=True)),
        ("workspace", workspace_root.resolve(strict=True)),
    ):
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        return f"{prefix}://{relative.as_posix()}"
    raise ContractViolation(
        "BLOCKED_NONCANONICAL_PATH", f"file is outside repository/workspace: {path}"
    )


def load_input_contract(path: Path) -> Mapping[str, object]:
    payload = load_json(path, "AIV1 input contract")
    expected_keys = set(EXPECTED_INPUT_CONTRACT_STATIC) | {
        "development_state_contract_sha256",
        "experience_registry_schema_sha256",
    }
    if set(payload) != expected_keys:
        raise ContractViolation(
            "BLOCKED_SCHEMA_MISMATCH",
            f"AIV1 input contract keyset drifted: {sorted(set(payload) ^ expected_keys)}",
        )
    for key, value in EXPECTED_INPUT_CONTRACT_STATIC.items():
        if payload.get(key) != value:
            raise ContractViolation(
                "BLOCKED_CONTRACT_DRIFT", f"input contract {key} != {value!r}"
            )
    require_sha256(
        payload.get("development_state_contract_sha256"),
        "input contract development_state_contract_sha256",
    )
    require_sha256(
        payload.get("experience_registry_schema_sha256"),
        "input contract experience_registry_schema_sha256",
    )
    return payload


def validate_aiv0_handoff(
    *,
    summary_path: Path,
    receipt_path: Path,
    derived_manifest_path: Path,
    inventory_path: Path,
) -> Mapping[str, object]:
    summary = load_json(summary_path, "AIV0 repository summary")
    receipt = load_json(receipt_path, "AIV0 final check receipt")
    if summary.get("schema_version") != "AIV0_M0_REPOSITORY_SUMMARY_V1":
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF", "unexpected AIV0 summary schema"
        )
    if summary.get("status") != "M0_PASS_ASSET_AND_SEMANTIC_READINESS":
        raise ContractViolation("BLOCKED_AIV0_HANDOFF", "AIV0 summary is not PASS")
    if (
        receipt.get("schema_version") != "AIV0_STAGE_RECEIPT_V1"
        or receipt.get("status") != "PASS"
    ):
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF", "AIV0 final receipt is not PASS"
        )
    if receipt.get("validator_mode") != "check" or receipt.get("exit_code") != 0:
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF",
            "AIV0 final receipt is not a successful read-only check",
        )
    evidence = summary.get("authoritative_evidence")
    if not isinstance(evidence, dict):
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF", "missing authoritative evidence"
        )
    actual_receipt_sha = sha256_file(require_file(receipt_path, "AIV0 receipt"))
    if evidence.get("final_check_receipt_sha256") != actual_receipt_sha:
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF", "AIV0 final receipt hash is not bound by summary"
        )
    derived_manifest = require_file(derived_manifest_path, "AIV0 derived manifest")
    derived_manifest_sha = sha256_file(derived_manifest)
    if (
        evidence.get("final_derived_manifest_sha256") != derived_manifest_sha
        or receipt.get("derived_outputs_manifest_sha256") != derived_manifest_sha
    ):
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF",
            "AIV0 derived manifest hash is not jointly bound by summary and receipt",
        )
    gates = summary.get("gates")
    if not isinstance(gates, dict) or gates.get("experimental_negative_labels") != 0:
        raise ContractViolation(
            "BLOCKED_LABEL_SEMANTICS",
            "AIV0 must report exactly zero experimental negatives",
        )
    inventory = require_file(inventory_path, "AIV0 structure inventory")
    derived_members = load_sha256_manifest(derived_manifest, "AIV0 derived manifest")
    if derived_members.get("structure_inventory.tsv") != sha256_file(inventory):
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF", "structure inventory is not bound by AIV0 manifest"
        )
    return {
        "schema_version": "AIV0_TO_AIV1_HANDOFF_V1",
        "aiv0_summary_sha256": sha256_file(require_file(summary_path, "AIV0 summary")),
        "aiv0_final_check_receipt_sha256": actual_receipt_sha,
        "aiv0_inventory_sha256": sha256_file(inventory),
        "aiv0_derived_manifest_sha256": derived_manifest_sha,
        "aiv0_status": receipt["status"],
        "experimental_negative_labels": 0,
    }


def validate_states(
    *,
    contract_path: Path,
    inventory_path: Path,
    input_contract: Mapping[str, object],
    workspace_root: Path,
) -> tuple[Mapping[str, str], ...]:
    expected_contract_sha = input_contract.get("development_state_contract_sha256")
    if (
        sha256_file(require_file(contract_path, "development state contract"))
        != expected_contract_sha
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "development state contract hash differs from AIV1 input contract",
        )
    states = read_tsv(
        contract_path, label="development state contract", exact_fields=STATE_FIELDS
    )
    if len(states) != 16:
        raise ContractViolation(
            "BLOCKED_STATE_DENOMINATOR", f"expected 16 states, found {len(states)}"
        )
    inventory_required = {
        "cohort_id",
        "source_id",
        "source_pdb",
        "relative_path",
        "sha256",
        "coordinate_sha256",
        "status",
        "active_for_ai",
        "parse_status",
        "geometry_complete",
        "independence_group",
        "experimental_negative",
        "binding_label",
    }
    inventory = read_tsv(
        inventory_path,
        label="AIV0 structure inventory",
        required_fields=inventory_required,
    )
    by_path: dict[str, dict[str, str]] = {}
    for row in inventory:
        relative_path = row["relative_path"]
        if relative_path in by_path:
            raise ContractViolation(
                "BLOCKED_DUPLICATE_INVENTORY_PATH",
                f"duplicate inventory path: {relative_path}",
            )
        by_path[relative_path] = row

    if [row["state_order"] for row in states] != [str(i) for i in range(16)]:
        raise ContractViolation(
            "BLOCKED_STATE_ORDER", "state_order must be the exact sequence 0..15"
        )
    if [row["target_state_id"] for row in states] != [
        f"DEV_{i:02d}" for i in range(16)
    ]:
        raise ContractViolation(
            "BLOCKED_STATE_ORDER", "target_state_id must be DEV_00..DEV_15"
        )
    for field in ("target_state_id", "relative_path", "sha256", "coordinate_sha256"):
        values = [row[field] for row in states]
        if len(set(values)) != len(values):
            raise ContractViolation(
                "BLOCKED_DUPLICATE_STATE", f"development states repeat {field}"
            )

    forbidden_markers = [
        str(value).casefold()
        for value in (
            list(input_contract.get("forbidden_cohort_markers", []))
            + list(input_contract.get("forbidden_target_markers", []))
        )
    ]
    for row in states:
        if any(
            marker in "|".join(row.values()).casefold() for marker in forbidden_markers
        ):
            raise ContractViolation(
                "BLOCKED_LOCKBOX_LEAK", f"forbidden marker in {row['target_state_id']}"
            )

    expected_projection: list[dict[str, str]] = [
        {
            "panel_role": "positive_primary",
            "target_identity": "GLP1_7_36_6X18",
            "source_id": "6X18",
            "source_pdb": "",
            "cohort_id": "positive_6x18_reference",
            "conformer_id": "6X18_model01",
            "independence_group": "PDB:6X18",
            "relative_path": "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820/inputs/target/6X18_GLP1_7-36_geometry.cif",
            "required_status": "USE_PRIMARY",
            "compact_cluster_weight": "",
        }
    ]
    for model, weight, role, status in (
        (10, "", "positive_fixed_control", "USE_POSITIVE_FIXED_CONTROL"),
        (12, "6", "positive_compact_medoid", "USE_POSITIVE_COMPACT"),
        (19, "10", "positive_compact_medoid", "USE_POSITIVE_COMPACT"),
        (20, "4", "positive_compact_medoid", "USE_POSITIVE_COMPACT"),
    ):
        expected_projection.append(
            {
                "panel_role": role,
                "target_identity": "GLP1_7_36_1D0R",
                "source_id": "1D0R",
                "source_pdb": "",
                "cohort_id": "positive_1d0r_all",
                "conformer_id": f"1D0R_model{model}",
                "independence_group": "PDB:1D0R",
                "relative_path": (
                    "data/样本数据/binding-多构象/all_conformers/"
                    f"1D0R_model{model}.cif"
                ),
                "required_status": status,
                "compact_cluster_weight": weight,
            }
        )
    expected_projection.append(
        {
            "panel_role": "tuning_primary_truncation",
            "target_identity": "GLP1_9_36_9IVM",
            "source_id": "9IVM",
            "source_pdb": "9IVM",
            "cohort_id": "countertarget_glp1_9_36_9ivm",
            "conformer_id": "9IVM_model01",
            "independence_group": "PDB:9IVM",
            "relative_path": "data/not_binding/GLP1_9_36/GLP1_9_36_reference_conf01.cif",
            "required_status": "USE_TUNING_CHALLENGE",
            "compact_cluster_weight": "",
        }
    )
    for model in range(1, 11):
        expected_projection.append(
            {
                "panel_role": "tuning_family_glp2",
                "target_identity": "GLP2_1_33_2L63",
                "source_id": "2L63",
                "source_pdb": "2L63",
                "cohort_id": "challenge_glp2_2l63",
                "conformer_id": f"2L63_model{model:02d}",
                "independence_group": "PDB:2L63",
                "relative_path": f"data/not_binding/GLP2_1_33/GLP2_1_33_conf{model:02d}.cif",
                "required_status": "USE_TUNING_CHALLENGE",
                "compact_cluster_weight": "",
            }
        )
    for index, (row, expected_row) in enumerate(
        zip(states, expected_projection, strict=True)
    ):
        for field, expected_value in expected_row.items():
            if row[field] != expected_value:
                raise ContractViolation(
                    "BLOCKED_STATE_ALLOWLIST",
                    f"DEV_{index:02d} {field} != {expected_value!r}",
                )
        for field, expected_value in {
            "required_active_for_ai": "true",
            "required_parse_status": "PASS",
            "required_geometry_complete": "true",
        }.items():
            if row[field] != expected_value:
                raise ContractViolation(
                    "BLOCKED_STATE_ALLOWLIST",
                    f"DEV_{index:02d} {field} != {expected_value!r}",
                )

    expected_roles = input_contract.get("state_role_counts")
    if not isinstance(expected_roles, dict) or Counter(
        row["panel_role"] for row in states
    ) != Counter(expected_roles):
        raise ContractViolation(
            "BLOCKED_STATE_DENOMINATOR", "panel role counts drifted"
        )
    allowed_cohorts = input_contract.get("allowed_cohorts")
    if not isinstance(allowed_cohorts, list) or set(
        row["cohort_id"] for row in states
    ) - set(allowed_cohorts):
        raise ContractViolation(
            "BLOCKED_STATE_ALLOWLIST", "unapproved cohort in state panel"
        )

    compact_weights = {
        row["conformer_id"]: row["compact_cluster_weight"]
        for row in states
        if row["panel_role"] == "positive_compact_medoid"
    }
    if compact_weights != {
        "1D0R_model12": "6",
        "1D0R_model19": "10",
        "1D0R_model20": "4",
    }:
        raise ContractViolation(
            "BLOCKED_AGGREGATION_CONTRACT", "1D0R compact weights must be 6/10/4"
        )
    if any(
        row["compact_cluster_weight"]
        for row in states
        if row["panel_role"] != "positive_compact_medoid"
    ):
        raise ContractViolation(
            "BLOCKED_AGGREGATION_CONTRACT", "only compact medoids may carry weights"
        )

    inventory_mapping = {
        "cohort_id": "cohort_id",
        "source_id": "source_id",
        "source_pdb": "source_pdb",
        "sha256": "sha256",
        "coordinate_sha256": "coordinate_sha256",
        "required_status": "status",
        "required_active_for_ai": "active_for_ai",
        "required_parse_status": "parse_status",
        "required_geometry_complete": "geometry_complete",
        "independence_group": "independence_group",
    }
    for row in states:
        source = by_path.get(row["relative_path"])
        if source is None:
            raise ContractViolation(
                "BLOCKED_STATE_ALLOWLIST",
                f"state absent from inventory: {row['relative_path']}",
            )
        for contract_field, inventory_field in inventory_mapping.items():
            if row[contract_field] != source[inventory_field]:
                raise ContractViolation(
                    "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                    f"{row['target_state_id']} {contract_field} != inventory {inventory_field}",
                )
        if (
            source["experimental_negative"] != "false"
            or source["binding_label"] != "unknown_or_not_applicable"
        ):
            raise ContractViolation(
                "BLOCKED_LABEL_SEMANTICS",
                f"state carries an experimental binding label: {row['target_state_id']}",
            )
        target_file = resolve_state_target_file(
            workspace_root, row["relative_path"], f"target {row['target_state_id']}"
        )
        if sha256_file(target_file) != row["sha256"]:
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"target bytes drifted: {row['target_state_id']}",
            )
    return tuple(states)


def candidate_id_set_sha256(candidate_ids: Sequence[str]) -> str:
    return sha256_text("".join(f"{value}\n" for value in sorted(candidate_ids)))


def _validate_bound_uri(
    *,
    uri: object,
    expected_sha: object,
    label: str,
    repo_root: Path,
    workspace_root: Path,
) -> Path:
    if not isinstance(uri, str):
        raise ContractViolation("BLOCKED_INVALID_URI", f"{label} URI is not text")
    expected = require_sha256(expected_sha, f"{label} sha256")
    resolved = resolve_uri(
        uri, repo_root=repo_root, workspace_root=workspace_root, label=label
    )
    if sha256_file(resolved) != expected:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", f"{label} hash mismatch"
        )
    return resolved


def _require_exact_file(path: Path, expected: Path, label: str) -> Path:
    resolved = require_file(path, label)
    expected_resolved = require_file(expected, f"expected {label}")
    if resolved != expected_resolved:
        raise ContractViolation(
            "BLOCKED_WRONG_ANCHOR_ORIGIN",
            f"{label} is not the canonical G2 path",
        )
    return resolved


def _verify_g2_output_tree(
    *, output_root: Path, manifest_path: Path, success_name: str
) -> Mapping[str, str]:
    root = require_directory(output_root, "G2 cell output root")
    manifest = load_g2_output_manifest(manifest_path)
    for relative_name, expected_hash in manifest.items():
        member = resolve_relative_file(root, relative_name, "G2 output member")
        if sha256_file(member) != expected_hash:
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"G2 output member hash mismatch: {relative_name}",
            )
    allowed_unmanifested = {
        "operator_logs/output_SHA256SUMS",
        f"operator_logs/{success_name}",
    }
    observed: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise ContractViolation(
                "BLOCKED_INPUT_TYPE", f"G2 output tree contains symlink: {candidate}"
            )
        if candidate.is_file():
            observed.add(candidate.relative_to(root).as_posix())
    expected_members = set(manifest) | allowed_unmanifested
    if observed != expected_members:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 output tree differs from its immutable manifest: "
            f"missing={sorted(expected_members - observed)}, "
            f"extra={sorted(observed - expected_members)}",
        )
    return manifest


def _require_peak_memory_fraction(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= 0.90
    ):
        raise ContractViolation(
            "BLOCKED_GPU_MEMORY",
            f"{label} must be finite and inside the interval (0, 0.90]",
        )
    return float(value)


def _parse_nvidia_memory_value(value: object, label: str) -> float:
    text = str(value).strip()
    match = re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:\s*(?:MiB))?",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ContractViolation(
            "BLOCKED_GPU_MEMORY", f"{label} is not a finite MiB value: {text!r}"
        )
    numeric_text = re.match(r"[0-9.]+", text)
    if numeric_text is None:
        raise ContractViolation(
            "BLOCKED_GPU_MEMORY", f"{label} has no numeric MiB value: {text!r}"
        )
    result = float(numeric_text.group(0))
    if not math.isfinite(result):
        raise ContractViolation(
            "BLOCKED_GPU_MEMORY", f"{label} is not finite: {text!r}"
        )
    return result


def _recompute_peak_memory_fraction(telemetry_path: Path, label: str) -> float:
    path = require_file(telemetry_path, f"{label} NVIDIA telemetry")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, skipinitialspace=True, strict=True)
            raw_fields = list(reader.fieldnames or [])
            stripped_fields = [field.strip() for field in raw_fields]
            if len(stripped_fields) != len(set(stripped_fields)):
                raise ContractViolation(
                    "BLOCKED_GPU_MEMORY",
                    f"{label} NVIDIA telemetry has duplicate columns",
                )
            used_fields = [
                raw
                for raw, stripped in zip(raw_fields, stripped_fields, strict=True)
                if stripped.casefold().startswith("memory.used")
            ]
            total_fields = [
                raw
                for raw, stripped in zip(raw_fields, stripped_fields, strict=True)
                if stripped.casefold().startswith("memory.total")
            ]
            if len(used_fields) != 1 or len(total_fields) != 1:
                raise ContractViolation(
                    "BLOCKED_GPU_MEMORY",
                    f"{label} NVIDIA telemetry must have exactly one memory.used "
                    "and one memory.total column",
                )
            used_values: list[float] = []
            total_values: list[float] = []
            for row_index, row in enumerate(reader, start=2):
                used = _parse_nvidia_memory_value(
                    row.get(used_fields[0]),
                    f"{label} NVIDIA telemetry row {row_index} memory.used",
                )
                total = _parse_nvidia_memory_value(
                    row.get(total_fields[0]),
                    f"{label} NVIDIA telemetry row {row_index} memory.total",
                )
                if total <= 0:
                    raise ContractViolation(
                        "BLOCKED_GPU_MEMORY",
                        f"{label} NVIDIA telemetry row {row_index} memory.total "
                        "must be positive",
                    )
                used_values.append(used)
                total_values.append(total)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ContractViolation(
            "BLOCKED_GPU_MEMORY", f"{label} NVIDIA telemetry is not parseable CSV"
        ) from error
    if not used_values:
        raise ContractViolation(
            "BLOCKED_GPU_MEMORY", f"{label} NVIDIA telemetry has no samples"
        )
    peak = max(used_values) / min(total_values)
    return _require_peak_memory_fraction(peak, f"{label} recomputed peak memory")


def _read_g2_candidate_index(
    output_root: Path, label: str
) -> tuple[Path, tuple[str, ...], tuple[str, ...]]:
    aggregate_path = require_file(
        output_root
        / "intermediate_designs_inverse_folded"
        / "aggregate_metrics_analyze.csv",
        f"{label} aggregate metrics",
    )
    try:
        with aggregate_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            fields = list(reader.fieldnames or [])
            if len(fields) != len(set(fields)) or not {"id", "file_name"}.issubset(
                fields
            ):
                raise ContractViolation(
                    "BLOCKED_G2_OUTPUT_SET",
                    f"{label} aggregate must have unique id and file_name columns",
                )
            rows = [dict(row) for row in reader]
    except (UnicodeDecodeError, csv.Error) as error:
        raise ContractViolation(
            "BLOCKED_G2_OUTPUT_SET", f"{label} aggregate is not parseable CSV"
        ) from error
    if len(rows) != G2_CANDIDATE_COUNT:
        raise ContractViolation(
            "BLOCKED_G2_OUTPUT_SET",
            f"{label} aggregate has {len(rows)} rows, expected {G2_CANDIDATE_COUNT}",
        )
    candidate_ids: list[str] = []
    file_names: list[str] = []
    for row_index, row in enumerate(rows, start=2):
        candidate_id = str(row.get("id", ""))
        file_name = str(row.get("file_name", ""))
        if SAFE_ID_RE.fullmatch(candidate_id) is None:
            raise ContractViolation(
                "BLOCKED_G2_OUTPUT_SET",
                f"{label} aggregate row {row_index} has unsafe id {candidate_id!r}",
            )
        if file_name.casefold() in {value.casefold() for value in file_names}:
            raise ContractViolation(
                "BLOCKED_DUPLICATE_ANCHOR",
                f"{label} aggregate repeats/case-collides file_name {file_name!r}",
            )
        if (
            not file_name
            or Path(file_name).name != file_name
            or Path(file_name).suffix != ".cif"
            or any(
                character in file_name for character in ("/", "\\", "\n", "\r", "\0")
            )
        ):
            raise ContractViolation(
                "BLOCKED_G2_OUTPUT_SET",
                f"{label} aggregate row {row_index} has unsafe file_name",
            )
        candidate_ids.append(candidate_id)
        file_names.append(file_name)
    if (
        len(set(candidate_ids)) != G2_CANDIDATE_COUNT
        or len({value.casefold() for value in candidate_ids}) != G2_CANDIDATE_COUNT
        or len(set(file_names)) != G2_CANDIDATE_COUNT
        or len({value.casefold() for value in file_names}) != G2_CANDIDATE_COUNT
    ):
        raise ContractViolation(
            "BLOCKED_G2_OUTPUT_SET",
            f"{label} aggregate candidate IDs/file names are not unique",
        )
    return aggregate_path, tuple(candidate_ids), tuple(file_names)


def _top_level_suffix_files(
    directory: Path, suffix: str, label: str
) -> tuple[Path, ...]:
    root = require_directory(directory, label)
    return tuple(
        sorted(
            path.resolve(strict=True)
            for path in root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix == suffix
            and not path.name.endswith(f"_native{suffix}")
        )
    )


def _validate_fold_npz(path: Path, label: str) -> None:
    resolved = require_file(path, f"{label} fold NPZ")
    try:
        with np.load(resolved, allow_pickle=False) as arrays:
            required_fields = {"coords", *G2_FOLD_SCORE_FIELDS}
            missing_fields = required_fields - set(arrays.files)
            if missing_fields:
                raise ContractViolation(
                    "BLOCKED_G2_FOLD_NPZ",
                    f"{label} fold NPZ is missing {sorted(missing_fields)}: {resolved}",
                )
            coords = np.asarray(arrays["coords"])
            if (
                coords.ndim != 3
                or coords.shape[0] != G2_FOLD_SAMPLE_COUNT
                or coords.shape[1] <= 0
                or coords.shape[2] != 3
                or not np.issubdtype(coords.dtype, np.number)
                or np.issubdtype(coords.dtype, np.complexfloating)
                or not np.isfinite(coords).all()
            ):
                raise ContractViolation(
                    "BLOCKED_G2_FOLD_NPZ",
                    f"{label} coords must be finite numeric "
                    f"({G2_FOLD_SAMPLE_COUNT}, atom_count, 3): {resolved} {coords.shape}",
                )
            for field in G2_FOLD_SCORE_FIELDS:
                values = np.asarray(arrays[field])
                if (
                    values.shape != (G2_FOLD_SAMPLE_COUNT,)
                    or not np.issubdtype(values.dtype, np.number)
                    or np.issubdtype(values.dtype, np.complexfloating)
                    or not np.isfinite(values).all()
                ):
                    raise ContractViolation(
                        "BLOCKED_G2_FOLD_NPZ",
                        f"{label} {field} must be a finite numeric "
                        f"({G2_FOLD_SAMPLE_COUNT},) array: {resolved} {values.shape}",
                    )
    except ContractViolation:
        raise
    except Exception as error:
        raise ContractViolation(
            "BLOCKED_G2_FOLD_NPZ",
            f"{label} fold NPZ is not safely readable: {resolved}",
        ) from error


def _validate_g2_candidate_outputs(output_root: Path, label: str) -> tuple[Path, ...]:
    root = require_directory(output_root, f"{label} output root")
    aggregate_path, candidate_ids, file_names = _read_g2_candidate_index(root, label)
    expected_ids = set(candidate_ids)
    expected_cif_names = set(file_names)
    expected_npz_names = {f"{candidate_id}.npz" for candidate_id in candidate_ids}
    design_directory = root / "intermediate_designs"
    inverse_directory = root / "intermediate_designs_inverse_folded"
    collections = {
        "top-level design CIF": (
            _top_level_suffix_files(design_directory, ".cif", f"{label} design output"),
            expected_ids,
            expected_cif_names,
        ),
        "top-level design NPZ": (
            _top_level_suffix_files(design_directory, ".npz", f"{label} design output"),
            expected_ids,
            expected_npz_names,
        ),
        "top-level inverse CIF": (
            _top_level_suffix_files(
                inverse_directory, ".cif", f"{label} inverse-folded output"
            ),
            expected_ids,
            expected_cif_names,
        ),
        "top-level inverse NPZ": (
            _top_level_suffix_files(
                inverse_directory, ".npz", f"{label} inverse-folded output"
            ),
            expected_ids,
            expected_npz_names,
        ),
        "fold_out_npz": (
            _top_level_suffix_files(
                inverse_directory / "fold_out_npz",
                ".npz",
                f"{label} fold output",
            ),
            expected_ids,
            expected_npz_names,
        ),
        "refold CIF": (
            _top_level_suffix_files(
                inverse_directory / "refold_cif", ".cif", f"{label} refold output"
            ),
            expected_ids,
            expected_cif_names,
        ),
    }
    for collection_label, (
        paths,
        expected_stems,
        expected_names,
    ) in collections.items():
        observed_stems = {path.stem for path in paths}
        observed_names = {path.name for path in paths}
        if (
            len(paths) != G2_CANDIDATE_COUNT
            or observed_stems != expected_stems
            or observed_names != expected_names
        ):
            raise ContractViolation(
                "BLOCKED_G2_OUTPUT_SET",
                f"{label} {collection_label} must be exactly the aggregate's "
                f"{G2_CANDIDATE_COUNT} candidates; observed={sorted(observed_names)}",
            )
    fold_paths = collections["fold_out_npz"][0]
    for fold_path in fold_paths:
        _validate_fold_npz(fold_path, label)
    return (aggregate_path, *fold_paths)


def validate_g2_evidence_chain(
    *,
    receipt: Mapping[str, object],
    repo_root: Path,
    workspace_root: Path,
) -> tuple[Mapping[str, object], Mapping[str, str], tuple[Path, ...]]:
    """Revalidate the G2 gate and every deterministic acceptance/probe dependency."""

    gate_path = _validate_bound_uri(
        uri=receipt["g2_acceptance_gate_uri"],
        expected_sha=receipt["g2_acceptance_gate_sha256"],
        label="G2 acceptance gate",
        repo_root=repo_root,
        workspace_root=workspace_root,
    )
    if gate_path.name != "G2_acceptance_gate.json":
        raise ContractViolation(
            "BLOCKED_WRONG_ANCHOR_ORIGIN", "G2 acceptance gate filename drifted"
        )
    acceptance_root = gate_path.parent
    status_path = _validate_bound_uri(
        uri=receipt["g2_resource_probe_status_uri"],
        expected_sha=receipt["g2_resource_probe_status_sha256"],
        label="G2 resource probe status",
        repo_root=repo_root,
        workspace_root=workspace_root,
    )
    _require_exact_file(
        status_path,
        acceptance_root / "G2_resource_probe.status.txt",
        "G2 resource status",
    )
    if status_path.read_bytes() != b"PASS\n":
        raise ContractViolation(
            "BLOCKED_G2_EVIDENCE", "G2 resource probe status must be exactly PASS\\n"
        )

    gate = load_json(gate_path, "G2 acceptance gate")
    if set(gate) != G2_ACCEPTANCE_GATE_FIELDS or gate.get("status") != "PASS":
        raise ContractViolation(
            "BLOCKED_SCHEMA_MISMATCH", "G2 acceptance gate keyset/status mismatch"
        )
    for field in (
        "spec_gate_bundle_sha256",
        "acceptance_success_sha256",
        "resource_summary_sha256",
    ):
        require_sha256(gate.get(field), f"G2 gate {field}")
    nested_hash_contracts = {
        "probe_success_sha256": {"diverse", "adherence"},
        "output_manifest_sha256": {
            "7xl0_adherence",
            "6xym_diverse",
            "6xym_adherence",
        },
        "resolved_config_manifest_sha256": {
            "6xym_diverse",
            "6xym_adherence",
        },
    }
    for field, expected_keys in nested_hash_contracts.items():
        values = gate.get(field)
        if not isinstance(values, dict) or set(values) != expected_keys:
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH", f"G2 gate {field} keyset mismatch"
            )
        for key, value in values.items():
            require_sha256(value, f"G2 gate {field}.{key}")
    peak_values = gate.get("peak_memory_fraction")
    if not isinstance(peak_values, dict) or set(peak_values) != {
        "diverse",
        "adherence",
    }:
        raise ContractViolation(
            "BLOCKED_SCHEMA_MISMATCH", "G2 gate peak_memory_fraction keyset mismatch"
        )
    declared_gate_peaks = {
        key: _require_peak_memory_fraction(value, f"G2 {key} gate peak memory")
        for key, value in peak_values.items()
    }

    generation_cell_id = str(receipt["generation_cell_id"])
    acceptance_output = acceptance_root / generation_cell_id
    acceptance_log = acceptance_output / "operator_logs"
    acceptance_success = require_file(
        acceptance_log / "cell.SUCCESS.json", "G2 7XL0 acceptance success"
    )
    if sha256_file(acceptance_success) != gate["acceptance_success_sha256"]:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", "G2 acceptance success hash mismatch"
        )
    acceptance_payload = load_json(acceptance_success, "G2 7XL0 acceptance success")
    if not (
        acceptance_payload.get("status") == "SUCCESS"
        and acceptance_payload.get("pipeline_exit_code") == 0
        and acceptance_payload.get("spec_gate_bundle_sha256")
        == gate["spec_gate_bundle_sha256"]
    ):
        raise ContractViolation(
            "BLOCKED_G2_EVIDENCE", "G2 7XL0 acceptance success fields are invalid"
        )
    acceptance_manifest_path = require_file(
        acceptance_log / "output_SHA256SUMS", "G2 7XL0 output manifest"
    )
    acceptance_manifest_sha = sha256_file(acceptance_manifest_path)
    if not (
        acceptance_manifest_sha
        == gate["output_manifest_sha256"]["7xl0_adherence"]
        == receipt["output_manifest_sha256"]
        == acceptance_payload.get("output_manifest_sha256")
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 7XL0 output manifest binding mismatch",
        )
    receipt_manifest_path = _validate_bound_uri(
        uri=receipt["output_manifest_uri"],
        expected_sha=receipt["output_manifest_sha256"],
        label="G2 output manifest",
        repo_root=repo_root,
        workspace_root=workspace_root,
    )
    _require_exact_file(
        receipt_manifest_path, acceptance_manifest_path, "G2 output manifest"
    )
    acceptance_manifest = _verify_g2_output_tree(
        output_root=acceptance_output,
        manifest_path=acceptance_manifest_path,
        success_name="cell.SUCCESS.json",
    )
    acceptance_candidate_evidence = _validate_g2_candidate_outputs(
        acceptance_output, "G2 7XL0 acceptance"
    )
    acceptance_config_manifest = require_file(
        acceptance_log / "resolved_config_SHA256SUMS",
        "G2 7XL0 resolved config manifest",
    )
    runtime_scripts_manifest = resolve_relative_file(
        acceptance_root.parent.parent,
        "provenance/gpu_runtime_scripts_SHA256SUMS",
        "G2 runtime scripts manifest",
    )
    model_inputs_manifest = resolve_relative_file(
        acceptance_root.parent.parent,
        "provenance/model_inputs_SHA256SUMS",
        "G2 model inputs manifest",
    )
    spec_gate_bundle = resolve_relative_file(
        acceptance_root.parent.parent,
        "provenance/spec_gate_bundle.tar",
        "G2 spec gate bundle",
    )
    environment_manifest = resolve_relative_file(
        acceptance_root.parent.parent,
        "environment_provenance.SHA256SUMS",
        "G1 environment provenance manifest",
    )
    if sha256_file(acceptance_config_manifest) != receipt["config_sha256"]:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 release config hash is not the 7XL0 resolved config manifest hash",
        )
    if not (
        sha256_file(runtime_scripts_manifest)
        == receipt["code_sha256"]
        == acceptance_payload.get("runtime_scripts_manifest_sha256")
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 release code hash is not the executed runtime scripts manifest hash",
        )
    if acceptance_payload.get("model_inputs_manifest_sha256") != sha256_file(
        model_inputs_manifest
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 7XL0 success is not bound to the actual model inputs manifest",
        )
    if gate["spec_gate_bundle_sha256"] != sha256_file(spec_gate_bundle):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 gate is not bound to the actual spec gate bundle",
        )
    if sha256_file(environment_manifest) != receipt["environment_sha256"]:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 release environment hash is not the G1 environment manifest hash",
        )
    acceptance_contract_path = require_file(
        acceptance_log / "cell_contract.json", "G2 7XL0 cell contract"
    )
    if acceptance_payload.get("cell_contract_sha256") != sha256_file(
        acceptance_contract_path
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", "G2 7XL0 cell contract hash mismatch"
        )
    acceptance_contract = load_json(acceptance_contract_path, "G2 7XL0 cell contract")
    for field, expected in {
        "status": "PASS",
        "expected_designs": 10,
        "observed_unique_ids": 10,
        "fold_samples_per_candidate": 5,
        "resolved_design_diffusion_samples": 1,
        "resolved_design_multiplicity": 10,
    }.items():
        if acceptance_contract.get(field) != expected:
            raise ContractViolation(
                "BLOCKED_G2_EVIDENCE", f"G2 7XL0 cell contract {field} != {expected}"
            )

    evidence_paths: list[Path] = [
        gate_path,
        status_path,
        acceptance_success,
        acceptance_manifest_path,
        acceptance_contract_path,
        acceptance_config_manifest,
        runtime_scripts_manifest,
        model_inputs_manifest,
        spec_gate_bundle,
        environment_manifest,
        *acceptance_candidate_evidence,
    ]
    checkpoint_hashes = {
        "diverse": "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c",
        "adherence": "ac7078b3dc13064c68e0c3fd542e5bc538c33558bf6607f65e499eb336ca5e5d",
    }
    for checkpoint_name in ("diverse", "adherence"):
        probe_output = acceptance_root / f"6xym_{checkpoint_name}_batch5__attempt_001"
        probe_log = probe_output / "operator_logs"
        success_path = require_file(
            probe_log / "probe.SUCCESS.json", f"G2 6XYM {checkpoint_name} success"
        )
        if sha256_file(success_path) != gate["probe_success_sha256"][checkpoint_name]:
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"G2 6XYM {checkpoint_name} success hash mismatch",
            )
        payload = load_json(success_path, f"G2 6XYM {checkpoint_name} success")
        expected_probe = {
            "status": "SUCCESS",
            "pipeline_exit_code": 0,
            "probe_id": f"6xym_{checkpoint_name}_batch5",
            "checkpoint_name": checkpoint_name,
            "checkpoint_sha256": checkpoint_hashes[checkpoint_name],
            "num_designs": 10,
            "diffusion_batch_size": 5,
            "fold_samples": 5,
            "spec_gate_bundle_sha256": gate["spec_gate_bundle_sha256"],
            "model_inputs_manifest_sha256": sha256_file(model_inputs_manifest),
            "runtime_scripts_manifest_sha256": sha256_file(runtime_scripts_manifest),
        }
        for field, expected in expected_probe.items():
            if payload.get(field) != expected:
                raise ContractViolation(
                    "BLOCKED_G2_EVIDENCE",
                    f"G2 6XYM {checkpoint_name} {field} != {expected!r}",
                )
        success_peak = _require_peak_memory_fraction(
            payload.get("peak_memory_fraction"),
            f"G2 6XYM {checkpoint_name} SUCCESS peak memory",
        )
        output_manifest_path = require_file(
            probe_log / "output_SHA256SUMS",
            f"G2 6XYM {checkpoint_name} output manifest",
        )
        resolved_config_path = require_file(
            probe_log / "resolved_config_SHA256SUMS",
            f"G2 6XYM {checkpoint_name} resolved config manifest",
        )
        output_key = f"6xym_{checkpoint_name}"
        if not (
            sha256_file(output_manifest_path)
            == gate["output_manifest_sha256"][output_key]
            == payload.get("output_manifest_sha256")
        ):
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"G2 6XYM {checkpoint_name} output manifest binding mismatch",
            )
        if not (
            sha256_file(resolved_config_path)
            == gate["resolved_config_manifest_sha256"][output_key]
            == payload.get("resolved_config_manifest_sha256")
        ):
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"G2 6XYM {checkpoint_name} config manifest binding mismatch",
            )
        probe_manifest = _verify_g2_output_tree(
            output_root=probe_output,
            manifest_path=output_manifest_path,
            success_name="probe.SUCCESS.json",
        )
        probe_candidate_evidence = _validate_g2_candidate_outputs(
            probe_output, f"G2 6XYM {checkpoint_name}"
        )
        cell_contract_path = require_file(
            probe_log / "cell_contract.json", f"G2 6XYM {checkpoint_name} cell contract"
        )
        if payload.get("cell_contract_sha256") != sha256_file(cell_contract_path):
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"G2 6XYM {checkpoint_name} cell contract hash mismatch",
            )
        cell_contract = load_json(
            cell_contract_path, f"G2 6XYM {checkpoint_name} contract"
        )
        for field, expected in {
            "status": "PASS",
            "expected_designs": 10,
            "observed_unique_ids": 10,
            "fold_samples_per_candidate": 5,
            "resolved_design_diffusion_samples": 5,
            "resolved_design_multiplicity": 2,
        }.items():
            if cell_contract.get(field) != expected:
                raise ContractViolation(
                    "BLOCKED_G2_EVIDENCE",
                    f"G2 6XYM {checkpoint_name} contract {field} != {expected!r}",
                )
        peak_path = require_file(
            probe_log / "peak_memory_fraction.txt",
            f"G2 6XYM {checkpoint_name} peak memory",
        )
        try:
            observed_peak = _require_peak_memory_fraction(
                float(peak_path.read_text(encoding="utf-8").strip()),
                f"G2 6XYM {checkpoint_name} peak memory file",
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise ContractViolation(
                "BLOCKED_GPU_MEMORY",
                f"G2 6XYM {checkpoint_name} peak memory is invalid",
            ) from error
        telemetry_path = require_file(
            probe_log / "nvidia_smi.csv",
            f"G2 6XYM {checkpoint_name} NVIDIA telemetry",
        )
        telemetry_relative = telemetry_path.relative_to(probe_output).as_posix()
        if probe_manifest.get(telemetry_relative) != sha256_file(telemetry_path):
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"G2 6XYM {checkpoint_name} NVIDIA telemetry is not manifest-bound",
            )
        recomputed_peak = _recompute_peak_memory_fraction(
            telemetry_path, f"G2 6XYM {checkpoint_name}"
        )
        declared_peaks = (
            declared_gate_peaks[checkpoint_name],
            success_peak,
            observed_peak,
        )
        all_peak_values = (recomputed_peak, *declared_peaks)
        if (
            any(
                not math.isclose(recomputed_peak, declared, rel_tol=0.0, abs_tol=1e-9)
                for declared in declared_peaks
            )
            or max(all_peak_values) - min(all_peak_values) > 1e-9
        ):
            raise ContractViolation(
                "BLOCKED_GPU_MEMORY",
                f"G2 6XYM {checkpoint_name} raw telemetry peak does not match "
                "the gate, SUCCESS payload, and peak_memory_fraction.txt",
            )
        evidence_paths.extend(
            (
                success_path,
                output_manifest_path,
                resolved_config_path,
                cell_contract_path,
                peak_path,
                telemetry_path,
                *probe_candidate_evidence,
            )
        )

    resource_summary = require_file(
        acceptance_root / "6xym_batch5_resource_summary.txt", "G2 resource summary"
    )
    if sha256_file(resource_summary) != gate["resource_summary_sha256"]:
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", "G2 resource summary hash mismatch"
        )
    evidence_paths.append(resource_summary)
    return gate, acceptance_manifest, tuple(evidence_paths)


def validate_anchors_and_g2(
    *,
    anchor_manifest_path: Path,
    g2_receipt_path: Path,
    input_contract: Mapping[str, object],
    aiv0_handoff: Mapping[str, object],
    repo_root: Path,
    workspace_root: Path,
) -> tuple[tuple[Mapping[str, str], ...], Mapping[str, object]]:
    anchors = read_tsv(
        anchor_manifest_path, label="AIV1 anchor manifest", exact_fields=ANCHOR_FIELDS
    )
    if len(anchors) != 10:
        raise ContractViolation(
            "BLOCKED_MISSING_G2_ANCHORS", f"expected 10 anchors, found {len(anchors)}"
        )
    if [row["anchor_order"] for row in anchors] != [str(i) for i in range(10)]:
        raise ContractViolation(
            "BLOCKED_ANCHOR_ORDER", "anchor_order must be the exact sequence 0..9"
        )
    candidate_ids = [row["candidate_id"] for row in anchors]
    if candidate_ids != sorted(candidate_ids):
        raise ContractViolation(
            "BLOCKED_ANCHOR_ORDER", "anchors must be canonically sorted by candidate_id"
        )
    if len(set(candidate_ids)) != 10:
        raise ContractViolation(
            "BLOCKED_DUPLICATE_ANCHOR", "candidate IDs are not unique"
        )

    generation = input_contract.get("generation_contract")
    if not isinstance(generation, dict):
        raise ContractViolation("BLOCKED_CONTRACT_DRIFT", "missing generation contract")
    forbidden_anchor_markers = [
        str(value).casefold()
        for value in (
            list(input_contract.get("forbidden_cohort_markers", []))
            + list(input_contract.get("forbidden_target_markers", []))
        )
    ]
    for row in anchors:
        if SAFE_ID_RE.fullmatch(row["candidate_id"]) is None:
            raise ContractViolation(
                "BLOCKED_ANCHOR_IDENTITY",
                f"unsafe candidate ID: {row['candidate_id']!r}",
            )
        if SEQUENCE_RE.fullmatch(row["full_sequence"]) is None:
            raise ContractViolation(
                "BLOCKED_ANCHOR_IDENTITY", f"invalid sequence: {row['candidate_id']}"
            )
        sequence_sha = sha256_text(row["full_sequence"])
        if row["full_sequence_sha256"] != sequence_sha:
            raise ContractViolation(
                "BLOCKED_ANCHOR_IDENTITY",
                f"sequence hash mismatch: {row['candidate_id']}",
            )
        for field in (
            "generation_cell_id",
            "shard_id",
            "scaffold_id",
            "checkpoint_id",
        ):
            if row[field] != generation.get(field):
                raise ContractViolation(
                    "BLOCKED_WRONG_ANCHOR_ORIGIN",
                    f"{row['candidate_id']} has wrong {field}",
                )
        if SAFE_ID_RE.fullmatch(row["shard_id"]) is None:
            raise ContractViolation(
                "BLOCKED_ANCHOR_IDENTITY", f"unsafe shard ID: {row['shard_id']!r}"
            )
        if any(
            marker in row["candidate_artifact_uri"].casefold()
            for marker in forbidden_anchor_markers
        ):
            raise ContractViolation(
                "BLOCKED_LOCKBOX_LEAK",
                f"anchor artifact URI contains a forbidden marker: {row['candidate_id']}",
            )
        if row["rng_seed_status"] != "NOT_EXPOSED_BY_CLI" or row["rng_seed"] != "":
            raise ContractViolation(
                "BLOCKED_ANCHOR_IDENTITY",
                "BoltzGen v0.3.2 has no documented global CLI seed; status must be "
                "NOT_EXPOSED_BY_CLI and rng_seed must be empty",
            )
        for field in (
            "candidate_artifact_sha256",
            "config_sha256",
            "code_sha256",
            "environment_sha256",
        ):
            require_sha256(row[field], f"{row['candidate_id']} {field}")

    receipt = load_json(g2_receipt_path, "G2 anchor release receipt")
    if set(receipt) != G2_RECEIPT_FIELDS:
        raise ContractViolation(
            "BLOCKED_SCHEMA_MISMATCH",
            f"G2 receipt keyset mismatch: {sorted(set(receipt) ^ G2_RECEIPT_FIELDS)}",
        )
    expected_values = {
        "schema_version": "AIV1_G2_ANCHOR_RELEASE_V1",
        "gate_id": "G2",
        "status": "PASS",
        "candidate_count": 10,
        **generation,
    }
    for field, expected in expected_values.items():
        if receipt.get(field) != expected:
            raise ContractViolation(
                "BLOCKED_WRONG_ANCHOR_ORIGIN", f"G2 receipt {field} != {expected!r}"
            )
    if receipt["anchor_manifest_sha256"] != sha256_file(
        require_file(anchor_manifest_path, "anchor manifest")
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", "anchor hash mismatch"
        )
    expected_id_set = candidate_id_set_sha256(candidate_ids)
    if receipt["candidate_id_set_sha256"] != expected_id_set:
        raise ContractViolation(
            "BLOCKED_ANCHOR_IDENTITY", "candidate ID set hash mismatch"
        )
    if receipt["aiv0_final_check_receipt_sha256"] != aiv0_handoff.get(
        "aiv0_final_check_receipt_sha256"
    ):
        raise ContractViolation(
            "BLOCKED_AIV0_HANDOFF", "G2 receipt binds a different AIV0"
        )
    for field in (
        "scaffold_sha256",
        "checkpoint_sha256",
        "platform_evidence_sha256",
        "aggregate_metrics_sha256",
        "anchor_manifest_sha256",
        "aiv0_final_check_receipt_sha256",
        "candidate_id_set_sha256",
        "config_sha256",
        "code_sha256",
        "environment_sha256",
        "output_manifest_sha256",
        "g2_acceptance_gate_sha256",
        "g2_resource_probe_status_sha256",
    ):
        require_sha256(receipt[field], f"G2 receipt {field}")
    for row in anchors:
        for anchor_field, receipt_field in (
            ("config_sha256", "config_sha256"),
            ("code_sha256", "code_sha256"),
            ("environment_sha256", "environment_sha256"),
        ):
            if row[anchor_field] != receipt[receipt_field]:
                raise ContractViolation(
                    "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                    f"{row['candidate_id']} {anchor_field} differs from G2 receipt",
                )

    gate, output_manifest, _ = validate_g2_evidence_chain(
        receipt=receipt,
        repo_root=repo_root,
        workspace_root=workspace_root,
    )

    platform_path = _validate_bound_uri(
        uri=receipt["platform_evidence_uri"],
        expected_sha=receipt["platform_evidence_sha256"],
        label="G2 platform evidence",
        repo_root=repo_root,
        workspace_root=workspace_root,
    )
    platform = load_json(platform_path, "G2 platform evidence")
    if set(platform) != PLATFORM_EVIDENCE_FIELDS:
        raise ContractViolation(
            "BLOCKED_SCHEMA_MISMATCH", "platform evidence keyset mismatch"
        )
    if not (
        platform.get("schema_version") == "AIV1_PLATFORM_EVIDENCE_V1"
        and platform.get("os_family") == "Linux"
        and str(platform.get("architecture")).casefold() in {"x86_64", "amd64"}
        and platform.get("accelerator_vendor") == "NVIDIA"
        and platform.get("cuda_available") is True
        and platform.get("nvidia_smi_exit_code") == 0
        and isinstance(platform.get("gpu_name"), str)
        and bool(str(platform.get("gpu_name")).strip())
        and isinstance(platform.get("driver_version"), str)
        and bool(str(platform.get("driver_version")).strip())
        and isinstance(platform.get("cuda_runtime_version"), str)
        and bool(str(platform.get("cuda_runtime_version")).strip())
        and re.fullmatch(r"[0-9]+\.[0-9]+", str(platform.get("gpu_compute_capability")))
        is not None
        and platform.get("bfloat16_supported") is True
        and platform.get("environment_sha256") == receipt["environment_sha256"]
    ):
        raise ContractViolation(
            "BLOCKED_UNSUPPORTED_RUNTIME",
            "G2 evidence is not x86_64 Linux/NVIDIA/CUDA with BF16 support",
        )
    aggregate_path = _validate_bound_uri(
        uri=receipt["aggregate_metrics_uri"],
        expected_sha=receipt["aggregate_metrics_sha256"],
        label="G2 aggregate_metrics_analyze.csv",
        repo_root=repo_root,
        workspace_root=workspace_root,
    )
    gate_path = resolve_uri(
        str(receipt["g2_acceptance_gate_uri"]),
        repo_root=repo_root,
        workspace_root=workspace_root,
        label="G2 acceptance gate",
    )
    acceptance_root = gate_path.parent
    acceptance_output = acceptance_root / str(generation["generation_cell_id"])
    inverse_folded = acceptance_output / "intermediate_designs_inverse_folded"
    _require_exact_file(
        aggregate_path,
        inverse_folded / "aggregate_metrics_analyze.csv",
        "G2 aggregate_metrics_analyze.csv",
    )
    aggregate_relative = aggregate_path.relative_to(acceptance_output).as_posix()
    if output_manifest.get(aggregate_relative) != sha256_file(aggregate_path):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "aggregate_metrics_analyze.csv is not bound by the 7XL0 output manifest",
        )
    with aggregate_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        required_fields = {"id", "file_name", "designed_chain_sequence"}
        if len(fields) != len(set(fields)) or not required_fields.issubset(fields):
            raise ContractViolation(
                "BLOCKED_SCHEMA_MISMATCH",
                "aggregate metrics must have unique id/file_name/designed_chain_sequence columns",
            )
        aggregate_rows = [dict(row) for row in reader]
    aggregate_ids = [str(row["id"]) for row in aggregate_rows]
    if (
        len(aggregate_ids) != 10
        or len(set(aggregate_ids)) != 10
        or sorted(aggregate_ids) != candidate_ids
    ):
        raise ContractViolation(
            "BLOCKED_ANCHOR_IDENTITY",
            "anchor IDs must exactly equal aggregate_metrics_analyze.csv IDs in canonical order",
        )
    if (
        gate["output_manifest_sha256"]["7xl0_adherence"]
        != receipt["output_manifest_sha256"]
    ):
        raise ContractViolation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
            "G2 gate/release manifest hashes differ",
        )

    aggregate_by_id = {str(row["id"]): row for row in aggregate_rows}
    artifact_paths: set[Path] = set()
    file_names: set[str] = set()
    for anchor in anchors:
        aggregate = aggregate_by_id[anchor["candidate_id"]]
        file_name = str(aggregate["file_name"])
        if (
            not file_name
            or Path(file_name).name != file_name
            or file_name in {".", ".."}
            or any(
                character in file_name for character in ("/", "\\", "\n", "\r", "\0")
            )
        ):
            raise ContractViolation(
                "BLOCKED_CANDIDATE_ARTIFACT",
                f"unsafe aggregate file_name: {file_name!r}",
            )
        if file_name.casefold() in file_names:
            raise ContractViolation(
                "BLOCKED_DUPLICATE_ANCHOR",
                f"duplicate/case-colliding file_name: {file_name}",
            )
        file_names.add(file_name.casefold())
        designed_sequence = re.sub(
            r"\s+", "", str(aggregate["designed_chain_sequence"])
        ).upper()
        if (
            SEQUENCE_RE.fullmatch(designed_sequence) is None
            or designed_sequence != anchor["full_sequence"]
        ):
            raise ContractViolation(
                "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                f"aggregate full sequence differs for {anchor['candidate_id']}",
            )
        expected_artifact = inverse_folded / "refold_cif" / file_name
        artifact_path = _validate_bound_uri(
            uri=anchor["candidate_artifact_uri"],
            expected_sha=anchor["candidate_artifact_sha256"],
            label=f"candidate artifact {anchor['candidate_id']}",
            repo_root=repo_root,
            workspace_root=workspace_root,
        )
        _require_exact_file(
            artifact_path,
            expected_artifact,
            f"candidate artifact {anchor['candidate_id']}",
        )
        artifact_relative = artifact_path.relative_to(acceptance_output).as_posix()
        if (
            output_manifest.get(artifact_relative)
            != anchor["candidate_artifact_sha256"]
        ):
            raise ContractViolation(
                "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT",
                f"candidate artifact is not bound by G2 output manifest: {anchor['candidate_id']}",
            )
        if artifact_path in artifact_paths:
            raise ContractViolation(
                "BLOCKED_DUPLICATE_ANCHOR",
                "candidate artifact URI/path must be unique even when full sequences repeat",
            )
        artifact_paths.add(artifact_path)
        if anchor["full_sequence"] not in extract_mmcif_canonical_sequences(
            artifact_path
        ):
            raise ContractViolation(
                "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH",
                f"candidate mmCIF does not contain full sequence: {anchor['candidate_id']}",
            )
    return tuple(anchors), receipt


def validate_inputs(
    *,
    repo_root: Path,
    workspace_root: Path,
    input_contract_path: Path,
    state_contract_path: Path,
    inventory_path: Path,
    aiv0_summary_path: Path,
    aiv0_receipt_path: Path,
    aiv0_derived_manifest_path: Path,
    anchor_manifest_path: Path,
    g2_receipt_path: Path,
) -> ValidatedInputs:
    repo = require_directory(repo_root, "repository root")
    workspace = require_directory(workspace_root, "workspace root")
    require_canonical_repo_file(
        input_contract_path,
        repo_root=repo,
        relative=CANONICAL_INPUT_CONTRACT_RELATIVE,
        label="AIV1 input contract",
    )
    require_canonical_repo_file(
        state_contract_path,
        repo_root=repo,
        relative=CANONICAL_STATE_CONTRACT_RELATIVE,
        label="development state contract",
    )
    require_canonical_repo_file(
        aiv0_summary_path,
        repo_root=repo,
        relative=CANONICAL_AIV0_SUMMARY_RELATIVE,
        label="AIV0 repository summary",
    )
    input_contract = load_input_contract(input_contract_path)
    aiv0_handoff = validate_aiv0_handoff(
        summary_path=aiv0_summary_path,
        receipt_path=aiv0_receipt_path,
        derived_manifest_path=aiv0_derived_manifest_path,
        inventory_path=inventory_path,
    )
    states = validate_states(
        contract_path=state_contract_path,
        inventory_path=inventory_path,
        input_contract=input_contract,
        workspace_root=workspace,
    )
    anchors, g2_receipt = validate_anchors_and_g2(
        anchor_manifest_path=anchor_manifest_path,
        g2_receipt_path=g2_receipt_path,
        input_contract=input_contract,
        aiv0_handoff=aiv0_handoff,
        repo_root=repo,
        workspace_root=workspace,
    )
    return ValidatedInputs(input_contract, states, anchors, aiv0_handoff, g2_receipt)


def build_task_rows(
    validated: ValidatedInputs, *, campaign_id: str
) -> list[dict[str, str]]:
    if SAFE_ID_RE.fullmatch(campaign_id) is None:
        raise ContractViolation(
            "BLOCKED_CAMPAIGN_ID", f"unsafe campaign ID: {campaign_id!r}"
        )
    rows: list[dict[str, str]] = []
    for candidate_index, anchor in enumerate(validated.anchors):
        for state in validated.states:
            state_order = int(state["state_order"])
            partition = (
                "positive_compact"
                if state["panel_role"].startswith("positive_")
                else "tuning_challenge"
            )
            rows.append(
                {
                    "task_id": (
                        f"{campaign_id}__AIV1_C{candidate_index:02d}_S{state_order:02d}"
                    ),
                    "campaign_id": campaign_id,
                    "stage": "AIV1",
                    "candidate_index": str(candidate_index),
                    "candidate_id": anchor["candidate_id"],
                    "full_sequence_sha256": anchor["full_sequence_sha256"],
                    "candidate_artifact_sha256": anchor["candidate_artifact_sha256"],
                    "generation_cell_id": anchor["generation_cell_id"],
                    "shard_id": anchor["shard_id"],
                    "scaffold_id": anchor["scaffold_id"],
                    "scaffold_sha256": str(validated.g2_receipt["scaffold_sha256"]),
                    "checkpoint_id": anchor["checkpoint_id"],
                    "checkpoint_sha256": str(validated.g2_receipt["checkpoint_sha256"]),
                    "config_sha256": anchor["config_sha256"],
                    "code_sha256": anchor["code_sha256"],
                    "environment_sha256": anchor["environment_sha256"],
                    "rng_seed_status": anchor["rng_seed_status"],
                    "rng_seed": anchor["rng_seed"],
                    "target_state_id": state["target_state_id"],
                    "target_identity": state["target_identity"],
                    "source_deposition": state["source_id"],
                    "independence_group": state["independence_group"],
                    "conformer_id": state["conformer_id"],
                    "data_partition": partition,
                    "panel_role": state["panel_role"],
                    "compact_cluster_weight": state["compact_cluster_weight"],
                    "target_logical_path": f"workspace://{state['relative_path']}",
                    "target_sha256": state["sha256"],
                    "target_coordinate_sha256": state["coordinate_sha256"],
                    "fold_run": "1",
                    "sample_count": "5",
                    "execution_mode": "REFOLD_REQUIRED",
                    "expected": "true",
                    "lockbox_access": "false",
                }
            )
    expected = int(validated.input_contract["expected_logical_tasks"])
    if len(rows) != expected:
        raise ContractViolation(
            "BLOCKED_TASK_DENOMINATOR", f"expected {expected} tasks, built {len(rows)}"
        )
    if len({row["task_id"] for row in rows}) != expected:
        raise ContractViolation("BLOCKED_DUPLICATE_TASK", "task IDs are not unique")
    if any(
        row["lockbox_access"] != "false"
        or row["data_partition"] == "lockbox"
        or any(
            marker in "|".join(row.values()).casefold() for marker in ("2b4n", "6lmk")
        )
        for row in rows
    ):
        raise ContractViolation("BLOCKED_LOCKBOX_LEAK", "lockbox task materialized")
    return rows


def render_tsv(rows: Sequence[Mapping[str, str]], fields: Sequence[str]) -> str:
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=fields, delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def write_new(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def materialize_matrix_bundle(
    *,
    validated: ValidatedInputs,
    output_dir: Path,
    campaign_id: str,
    input_paths: Sequence[Path],
    repo_root: Path,
    workspace_root: Path,
    builder_path: Path | None = None,
) -> Mapping[str, object]:
    resolved_builder = require_file(
        Path(__file__) if builder_path is None else builder_path, "matrix builder"
    )
    rows = build_task_rows(validated, campaign_id=campaign_id)
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise ContractViolation(
            "BLOCKED_IMMUTABLE_OUTPUT_EXISTS", f"output exists: {output}"
        )
    parent = require_directory(output.parent, "matrix output parent")
    final_output = parent / output.name
    resolved_repo = require_directory(repo_root, "repository root")
    resolved_workspace = require_directory(workspace_root, "workspace root")
    try:
        final_output.relative_to(resolved_workspace)
    except ValueError as error:
        raise ContractViolation(
            "BLOCKED_NONCANONICAL_PATH", "matrix output must be inside workspace root"
        ) from error
    try:
        final_output.relative_to(resolved_repo)
    except ValueError:
        pass
    else:
        raise ContractViolation(
            "BLOCKED_NONCANONICAL_PATH", "matrix output must be outside repository"
        )
    staging = parent / f".{output.name}.staging.{uuid.uuid4().hex}"
    staging.mkdir(mode=0o750)
    try:
        matrix_path = staging / "task_matrix.tsv"
        write_new(matrix_path, render_tsv(rows, TASK_FIELDS))
        write_new(staging / "aiv0_handoff.json", canonical_json(validated.aiv0_handoff))
        snapshot = {
            "schema_version": "AIV1_INPUT_SNAPSHOT_V1",
            "campaign_id": campaign_id,
            "candidate_count": len(validated.anchors),
            "duplicate_candidate_artifact_content_count": (
                len(validated.anchors)
                - len({row["candidate_artifact_sha256"] for row in validated.anchors})
            ),
            "candidate_id_set_sha256": candidate_id_set_sha256(
                [row["candidate_id"] for row in validated.anchors]
            ),
            "anchor_manifest_sha256": validated.g2_receipt["anchor_manifest_sha256"],
            "g2_receipt_sha256": sha256_file(
                require_file(input_paths[-1], "G2 receipt")
            ),
            "aiv0_handoff": validated.aiv0_handoff,
            "development_state_count": len(validated.states),
            "state_contract_sha256": sha256_file(
                require_file(input_paths[1], "state contract")
            ),
            "input_contract_sha256": sha256_file(
                require_file(input_paths[0], "input contract")
            ),
            "matrix_builder_sha256": sha256_file(resolved_builder),
            "lockbox_task_count": 0,
            "aggregation_contract": {
                "panel_role_materialized": True,
                "compact_cluster_weight_materialized": True,
            },
            "scientific_boundary": validated.input_contract["scientific_boundary"],
        }
        write_new(staging / "input_snapshot.json", canonical_json(snapshot))
        summary = {
            "schema_version": "AIV1_TASK_MATRIX_SUMMARY_V1",
            "status": "READY_FOR_EXECUTION",
            "campaign_id": campaign_id,
            "candidate_count": 10,
            "development_state_count": 16,
            "logical_task_count": len(rows),
            "expected_sample_result_rows": sum(
                int(row["sample_count"]) for row in rows
            ),
            "new_refold_task_count": 160,
            "reused_verified_task_count": 0,
            "lockbox_task_count": 0,
            "task_matrix_sha256": sha256_file(matrix_path),
            "cross_scaffold_thresholds_frozen": False,
            "binding_claim": False,
        }
        write_new(staging / "task_matrix_summary.json", canonical_json(summary))
        _, _, g2_evidence_paths = validate_g2_evidence_chain(
            receipt=validated.g2_receipt,
            repo_root=repo_root,
            workspace_root=workspace_root,
        )
        additional_bound_paths = [
            _validate_bound_uri(
                uri=validated.g2_receipt[field],
                expected_sha=validated.g2_receipt[field.replace("_uri", "_sha256")],
                label=field,
                repo_root=repo_root,
                workspace_root=workspace_root,
            )
            for field in ("platform_evidence_uri", "aggregate_metrics_uri")
        ]
        additional_bound_paths.extend(
            _validate_bound_uri(
                uri=anchor["candidate_artifact_uri"],
                expected_sha=anchor["candidate_artifact_sha256"],
                label=f"candidate artifact {anchor['candidate_id']}",
                repo_root=repo_root,
                workspace_root=workspace_root,
            )
            for anchor in validated.anchors
        )
        manifest_rows = []
        for path in (
            *input_paths,
            *g2_evidence_paths,
            *additional_bound_paths,
            resolved_builder,
        ):
            resolved = require_file(path, "matrix input")
            manifest_rows.append(
                f"{sha256_file(resolved)}  "
                f"{canonical_uri(resolved, repo_root=repo_root, workspace_root=workspace_root)}"
            )
        write_new(
            staging / "inputs.SHA256SUMS",
            "\n".join(sorted(set(manifest_rows))) + "\n",
        )
        fsync_directory(staging)
        if final_output.exists() or final_output.is_symlink():
            raise ContractViolation(
                "BLOCKED_IMMUTABLE_OUTPUT_EXISTS", f"output exists: {final_output}"
            )
        os.rename(staging, final_output)
        fsync_directory(parent)
        return summary
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--input-contract", type=Path, required=True)
    parser.add_argument("--state-contract", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--aiv0-summary", type=Path, required=True)
    parser.add_argument("--aiv0-receipt", type=Path, required=True)
    parser.add_argument("--aiv0-derived-manifest", type=Path, required=True)
    parser.add_argument("--anchor-manifest", type=Path, required=True)
    parser.add_argument("--g2-receipt", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validated = validate_inputs(
            repo_root=args.repo_root,
            workspace_root=args.workspace_root,
            input_contract_path=args.input_contract,
            state_contract_path=args.state_contract,
            inventory_path=args.inventory,
            aiv0_summary_path=args.aiv0_summary,
            aiv0_receipt_path=args.aiv0_receipt,
            aiv0_derived_manifest_path=args.aiv0_derived_manifest,
            anchor_manifest_path=args.anchor_manifest,
            g2_receipt_path=args.g2_receipt,
        )
        summary = materialize_matrix_bundle(
            validated=validated,
            output_dir=args.output_dir,
            campaign_id=args.campaign_id,
            input_paths=(
                args.input_contract,
                args.state_contract,
                args.inventory,
                args.aiv0_summary,
                args.aiv0_receipt,
                args.aiv0_derived_manifest,
                args.anchor_manifest,
                args.g2_receipt,
            ),
            repo_root=args.repo_root.resolve(strict=True),
            workspace_root=args.workspace_root.resolve(strict=True),
        )
    except ContractViolation as error:
        print(
            canonical_json(
                {"status": "BLOCKED", "code": error.code, "detail": error.message}
            ),
            end="",
        )
        return 2
    print(canonical_json(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
