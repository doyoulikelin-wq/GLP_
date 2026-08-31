#!/usr/bin/env python3
"""Build the frozen, allowlist-only source manifest for the GLP-1 campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


FIELDS = [
    "asset_id", "asset_role", "source_url", "source_snapshot", "local_source_path",
    "run_copy_path", "bytes", "records", "format", "sha256", "license",
    "chemistry_status", "model_role", "allowed_in_current_run", "limitation",
    "independence_group", "target_identity", "conformer_id", "data_partition",
    "label_status", "experimental_label",
]
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
RUNTIME_ORDER = [
    "boltzgen1_diverse.ckpt", "boltzgen1_adherence.ckpt", "boltzgen1_ifold.ckpt",
    "boltz2_conf_final.ckpt", "mols.zip",
]
TARGET_ALLOW_CONTRACT = {
    "asset": "6X18_glp1_7-36NH2_labelE_authP.cif",
    "role": "positive-target geometry",
    "status": "project_input_geometry_with_chemistry_caveat",
    "use_level": "conditional_geometry_only",
}
FROZEN_PARTITIONS = {
    "primary_target", "runtime", "baseline_scaffold", "positive_compact",
    "tuning_challenge", "lockbox", "incomplete_quarantine", "challenger_scaffold",
}
UNSPECIFIED_LICENSE = "UNSPECIFIED_IN_SUPPLIED_SOURCE_CONTRACT"


def fail(message: str) -> NoReturn:
    raise SystemExit(f"BLOCKED_INPUT_MANIFEST: {message}")


def reject_symlink_components(path: Path, label: str, *, allow_missing: bool) -> None:
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            fail(f"{label} contains a symlink component: {current}")
        if not current.exists() and not allow_missing:
            fail(f"{label} path component is missing: {current}")


def regular_file(value: str | Path, label: str, *, nonempty: bool = True) -> Path:
    path = Path(os.path.abspath(value))
    if path.is_symlink() or not path.is_file():
        fail(f"{label} must be a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    if nonempty and resolved.stat().st_size == 0:
        fail(f"{label} is empty: {path}")
    return resolved


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def safe_segment(value: str, label: str) -> str:
    if SAFE_SEGMENT.fullmatch(value) is None or PurePosixPath(value).name != value:
        fail(f"unsafe single-segment {label}: {value!r}")
    return value


def path_beneath(root: Path, relative: str, label: str, *, require_leaf: bool) -> Path:
    """Resolve a safe relative path without symlink hops outside its root."""

    safe = safe_relative(relative, label)
    lexical = root / Path(*safe.parts)
    canonical_root = root.resolve(strict=True)
    if require_leaf:
        resolved = regular_file(lexical, label)
        try:
            resolved.relative_to(canonical_root)
        except ValueError:
            fail(f"{label} escapes its project root: {relative!r}")
        return resolved
    existing = lexical
    while not existing.exists() and existing != root:
        existing = existing.parent
    try:
        existing.resolve(strict=True).relative_to(canonical_root)
    except ValueError:
        fail(f"{label} escapes its project root: {relative!r}")
    return lexical


def load_json(path: Path, label: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                fail(f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"cannot read {label}: {error}")
    if not isinstance(payload, dict):
        fail(f"{label} root must be an object")
    return payload


def load_tsv(path: Path, required: set[str], label: str) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not required.issubset(fields):
                fail(f"{label} header is invalid; missing {sorted(required - set(fields))}")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        fail(f"cannot read {label}: {error}")
    if not rows or any(None in row for row in rows):
        fail(f"{label} is empty or malformed")
    return rows


def bool_text(value: str, label: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    fail(f"{label} must be exactly true or false, got {value!r}")


def integer_text(value: Any, label: str, *, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(f"{label} must be an integer, got {value!r}")
    if number < minimum or str(number) != str(value):
        fail(f"{label} is non-canonical or below {minimum}: {value!r}")
    return number


def require_sha(value: str, label: str) -> str:
    if SHA256.fullmatch(value) is None:
        fail(f"invalid SHA-256 for {label}: {value!r}")
    return value


def summary_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(f"{label} must be a JSON integer >= {minimum}")
    return value


def find_frozen_file(reference: Path, relative: str, label: str) -> Path:
    safe = safe_relative(relative, label)
    for ancestor in reference.parents:
        candidate = ancestor / Path(*safe.parts)
        if candidate.is_file() or candidate.is_symlink():
            return regular_file(candidate, label)
    fail(f"cannot resolve {label} from project ancestry: {relative}")


def derived_limitation(
    base: str,
    *,
    source_asset_id: str,
    source_sha256: str,
    evidence_key: str,
    evidence_sha256: str,
    upstream_license: str | None,
) -> str:
    if not source_asset_id or ";" in source_asset_id:
        fail("derived source_asset_id is missing or unsafe")
    if evidence_key not in {"transform_code_sha256", "frozen_validator_sha256"}:
        fail("derived provenance evidence key is invalid")
    source_sha = require_sha(source_sha256, f"derived source {source_asset_id}")
    evidence_sha = require_sha(evidence_sha256, f"derived evidence {source_asset_id}")
    license_value = upstream_license.strip() if isinstance(upstream_license, str) else ""
    if not license_value:
        license_value = UNSPECIFIED_LICENSE
    if ";" in license_value:
        fail(f"upstream license contains an unsafe delimiter: {source_asset_id}")
    prefix = base.strip().rstrip(";")
    return (
        f"{prefix}; source_asset_id={source_asset_id}; source_sha256={source_sha}; "
        f"{evidence_key}={evidence_sha}; upstream_license={license_value}"
    )


def row(**values: str) -> dict[str, str]:
    result = {field: "" for field in FIELDS}
    unknown = set(values) - set(FIELDS)
    if unknown:
        fail(f"internal unknown output fields: {sorted(unknown)}")
    result.update(values)
    return result


def verify_summary(
    summary: dict[str, Any],
    *,
    cohort_path: Path,
    override_path: Path,
    structure_path: Path,
    scaffold_path: Path,
    validator_path: Path,
    cohorts: list[dict[str, str]],
    structures: list[dict[str, str]],
    historical_path: Path,
    historical: list[dict[str, str]],
) -> None:
    try:
        checks = [
            summary["schema_version"] == "AI_VALIDATION_ASSET_REGISTRY_V2",
            summary["overall_status"] == "PASS",
            summary["positive_ensemble"]["compact_panel_models"] == [10, 12, 19, 20],
            summary["positive_ensemble"]["compact_panel_paths_verified"] == 4,
            summary["positive_ensemble"]["active_representative_aliases"] == 0,
            summary["challenge_panel"]["usable_challenge_conformers"] == 32,
            summary["challenge_panel"]["usable_target_source_groups"] == 4,
            summary["challenge_panel"]["experimental_negative_labels"] == 0,
            summary["scaffold_libraries"]["new_scaffold_packages"] == 17,
            summary["scaffold_libraries"]["old12_instance_overlaps"] == 4,
            summary["scaffold_libraries"]["overlap_use_old_canonical"] == 4,
            summary["scaffold_libraries"]["production_active_from_new17"] == 0,
            summary["semantic_contract"]["experimental_negative_labels"] == 0,
            summary["semantic_contract"]["challenge_target_source_groups"] == 4,
            summary["semantic_contract"]["no_binding_directory_is_label"] is False,
            summary["errors"] == [],
            isinstance(summary["warnings"], list),
        ]
    except (KeyError, TypeError):
        fail("AI validation summary is missing a frozen contract field")
    if not all(checks):
        fail("AI validation summary differs from the frozen PASS/count/semantic contract")

    count_fields = {
        "source_file_count": 1,
        "source_bytes": 1,
        "structure_path_count": 1,
        "structure_parse_pass": 1,
        "cohort_count": 1,
        "asset_mount_count": 1,
        "compatibility_aliases_verified": 0,
        "historical_output_hashes_verified": 1,
    }
    for field, minimum in count_fields.items():
        try:
            summary_integer(summary[field], f"AI validation summary {field}", minimum=minimum)
        except KeyError:
            fail(f"AI validation summary is missing production count field: {field}")
    if summary["structure_path_count"] != len(structures):
        fail("AI validation structure_path_count differs from supplied structure inventory")
    if summary["structure_parse_pass"] != sum(item["parse_status"] == "PASS" for item in structures):
        fail("AI validation structure_parse_pass differs from supplied structure inventory")
    if summary["cohort_count"] != len(cohorts):
        fail("AI validation cohort_count differs from supplied cohort registry")

    semantics = summary.get("source_count_semantics")
    if not isinstance(semantics, dict):
        fail("AI validation summary is missing source_count_semantics")
    for field in (
        "historical_logical_files", "non_system_metadata_logical_files",
        "system_metadata_files_excluded_from_model_input",
        "intentional_positive_mirror_logical_files",
    ):
        try:
            summary_integer(semantics[field], f"source_count_semantics {field}")
        except KeyError:
            fail(f"source_count_semantics is missing {field}")
    if semantics["historical_logical_files"] != summary["source_file_count"]:
        fail("source_count_semantics historical count differs from source_file_count")

    hashes = {
        "input_registry_sha256": digest(cohort_path),
        "override_registry_sha256": digest(override_path),
        "historical_output_hash_registry_sha256": digest(historical_path),
        "validator_sha256": digest(validator_path),
    }
    for field in ("asset_mount_registry_sha256", "compatibility_alias_registry_sha256"):
        try:
            require_sha(summary[field], f"AI validation summary {field}")
        except KeyError:
            fail(f"AI validation summary is missing production hash field: {field}")
    for field, observed in hashes.items():
        try:
            expected = require_sha(summary[field], f"AI validation summary {field}")
        except KeyError:
            fail(f"AI validation summary is missing production hash field: {field}")
        if expected != observed:
            fail(f"AI validation summary {field} differs from supplied frozen artifact")

    historical_by_name = {item["filename"]: item for item in historical}
    expected_historical = {
        "source_file_inventory.tsv", "structure_inventory.tsv", "duplicate_groups.tsv",
        "cohort_summary.tsv", "scaffold_comparison.tsv",
    }
    if len(historical_by_name) != len(historical) or set(historical_by_name) != expected_historical:
        fail("historical-output-hash registry must contain exactly the five frozen outputs")
    if summary["historical_output_hashes_verified"] != len(historical):
        fail("historical_output_hashes_verified differs from the supplied registry")
    for filename, supplied in (
        ("structure_inventory.tsv", structure_path),
        ("scaffold_comparison.tsv", scaffold_path),
    ):
        expected = require_sha(historical_by_name[filename]["sha256"], f"historical {filename}")
        if digest(supplied) != expected:
            fail(f"supplied {filename} differs from historical-output-hash registry")


def verify_cohort_contract(
    cohorts: list[dict[str, str]],
    structures: list[dict[str, str]],
    overrides: dict[str, dict[str, str]],
) -> None:
    """Close every structure and override against one canonical cohort row."""

    cohort_by_id: dict[str, dict[str, str]] = {}
    for cohort in cohorts:
        cohort_id = safe_segment(cohort["cohort_id"], "AI cohort ID")
        safe_segment(cohort["source_id"], f"source ID for {cohort_id}")
        safe_segment(cohort["ai_role"], f"AI role for {cohort_id}")
        safe_segment(cohort["default_status"], f"default status for {cohort_id}")
        pattern_text = cohort["canonical_glob"]
        pattern = safe_relative(pattern_text, f"canonical glob for {cohort_id}")
        if pattern.parts[0] != "data" or "**" in pattern_text:
            fail(f"AI cohort {cohort_id} lacks a fixed data/ path prefix")
        fixed_prefix = []
        for part in pattern.parts:
            if any(character in part for character in "*?["):
                break
            fixed_prefix.append(part)
        if not fixed_prefix or fixed_prefix[0] != "data":
            fail(f"AI cohort {cohort_id} lacks a fixed canonical path prefix")
        if cohort_id in cohort_by_id:
            fail("duplicate AI cohort ID")
        cohort_by_id[cohort_id] = cohort

    seen_cohorts: set[str] = set()
    seen_paths: set[str] = set()
    for structure in structures:
        relative = safe_relative(
            structure["relative_path"], "AI structure relative path"
        )
        if relative.suffix.lower() not in {".cif", ".mmcif"}:
            fail(f"AI structure path is not a CIF/mmCIF: {relative}")
        if relative.as_posix() in seen_paths:
            fail("duplicate AI structure path")
        seen_paths.add(relative.as_posix())
        cohort_id = safe_segment(structure["cohort_id"], "AI structure cohort ID")
        cohort = cohort_by_id.get(cohort_id)
        if cohort is None:
            fail(f"AI structure references an unknown cohort: {cohort_id}")
        seen_cohorts.add(cohort_id)
        if not relative.match(cohort["canonical_glob"]):
            fail(
                f"AI structure path is outside cohort {cohort_id} canonical glob: "
                f"{relative}"
            )
        for field in ("source_id", "ai_role", "terminal_chemistry_claim"):
            if structure[field] != cohort[field]:
                fail(f"AI structure/cohort {field} mismatch: {relative}")
        override = overrides.get(relative.as_posix())
        expected_status = (
            override["status"] if override is not None else cohort["default_status"]
        )
        if structure["status"] != expected_status:
            fail(f"AI structure/cohort/override status mismatch: {relative}")

    if seen_cohorts != set(cohort_by_id):
        fail(
            "AI cohort registry is not completely represented by the structure "
            f"inventory: {sorted(set(cohort_by_id) - seen_cohorts)}"
        )
    for relative, override in overrides.items():
        safe_relative(relative, "AI override relative path")
        safe_segment(override["status"], f"override status for {relative}")
        if relative not in seen_paths:
            fail("AI override path is missing from structure inventory")


def find_project_root(reference: Path, relative_paths: list[str]) -> Path | None:
    for ancestor in reference.parents:
        valid = True
        for value in relative_paths:
            try:
                candidate = ancestor / Path(*safe_relative(value, "AI source path").parts)
                if not candidate.is_file() or candidate.is_symlink():
                    valid = False
                    break
                candidate.resolve(strict=True).relative_to(ancestor.resolve(strict=True))
            except (OSError, SystemExit):
                valid = False
                break
            except ValueError:
                valid = False
                break
        if valid:
            return ancestor
    return None


def source_path(project_root: Path | None, relative: str) -> str:
    safe = safe_relative(relative, "AI source path")
    if project_root is None:
        return safe.as_posix()
    return str(path_beneath(project_root, safe.as_posix(), "AI source path", require_leaf=False))


def verify_model_file(project_root: Path, inventory: dict[str, str]) -> Path:
    path = path_beneath(
        project_root,
        inventory["relative_path"],
        f"AI model input {inventory['relative_path']}",
        require_leaf=True,
    )
    if path.stat().st_size != integer_text(inventory["bytes"], "AI bytes") or digest(path) != require_sha(inventory["sha256"], "AI input"):
        fail(f"AI model input hash/size drift: {inventory['relative_path']}")
    return path


def conformer_id(item: dict[str, str]) -> str:
    stem = PurePosixPath(item["relative_path"]).stem
    match = re.search(r"(?:model|conf)([0-9]+)$", stem, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))
    return item.get("model_numbers", "")


def ai_row(
    item: dict[str, str], *, partition: str, allowed: bool, snapshot: str,
    project_root: Path, validator_sha256: str, limitation: str | None = None,
) -> dict[str, str]:
    experimental = item.get("experimental_negative", "false")
    if bool_text(experimental, f"experimental_negative for {item['relative_path']}"):
        fail(f"experimental-negative labels are forbidden: {item['relative_path']}")
    if item.get("parse_status") != "PASS":
        fail(f"AI structure parse is not PASS: {item['relative_path']}")
    if item.get("finite_coordinates") not in {None, "", "True", "true"}:
        fail(f"AI structure coordinates are not finite: {item['relative_path']}")
    if allowed and (item.get("validation_status") != "PASS" or item.get("geometry_complete") != "true"):
        fail(f"allowed AI structure is incomplete or unvalidated: {item['relative_path']}")
    if allowed and item.get("active_for_ai") != "true":
        fail(f"allowed AI structure is not active_for_ai=true: {item['relative_path']}")
    if item.get("binding_label") != "unknown_or_not_applicable":
        fail(f"AI structure carries a forbidden binding label: {item['relative_path']}")
    source_pdb = item.get("source_pdb", "") or item.get("source_id", "")
    model_role = {
        "positive_compact": "positive_geometry_state",
        "tuning_challenge": "computational_tuning_challenge",
        "lockbox": "sealed_lockbox_identity",
        "incomplete_quarantine": "incomplete_audit_only",
    }[partition]
    label_status = {
        "positive_compact": "geometry_reference_unlabeled",
        "tuning_challenge": "computational_challenge_unvalidated",
        "lockbox": "computational_challenge_unvalidated",
        "incomplete_quarantine": "quarantined_incomplete",
    }[partition]
    cohort_id = safe_segment(item["cohort_id"], "AI output cohort ID")
    filename = PurePosixPath(item["relative_path"]).name
    run_copy = ""
    if partition == "positive_compact":
        safe_segment(filename, "positive compact output filename")
        run_copy = f"02_inputs/ai_validation/positive_states/compact/{filename}"
    elif partition == "tuning_challenge":
        safe_segment(filename, "tuning output filename")
        run_copy = f"02_inputs/ai_validation/tuning_challenges/{cohort_id}__{filename}"
    base_limitation = limitation if limitation is not None else item.get("limitation_or_reason", "")
    if partition == "lockbox":
        base_limitation = f"identity-only sealed lockbox; no file is exposed to the current run; {base_limitation}"
    lineage = derived_limitation(
        base_limitation,
        source_asset_id=f"AI:{cohort_id}:{item['relative_path']}",
        source_sha256=item["sha256"],
        evidence_key="frozen_validator_sha256",
        evidence_sha256=validator_sha256,
        upstream_license=None,
    )
    local_source = "" if partition == "lockbox" else source_path(project_root, item["relative_path"])
    return row(
        asset_id=f"AI:{cohort_id}:{filename}",
        asset_role="geometry_reference" if partition == "positive_compact" else "challenge_geometry",
        source_url=f"https://files.rcsb.org/download/{source_pdb}.cif" if source_pdb else "",
        source_snapshot=snapshot,
        local_source_path=local_source,
        run_copy_path=run_copy,
        bytes=str(integer_text(item["bytes"], "AI bytes")),
        records="1",
        format="PDBx/mmCIF",
        sha256=require_sha(item["sha256"], f"AI structure {filename}"),
        license="DERIVED_SEE_SOURCE_MANIFEST",
        chemistry_status=item.get("terminal_chemistry_claim", "not_explicit"),
        model_role=model_role,
        allowed_in_current_run="true" if allowed else "false",
        limitation=lineage,
        independence_group=item.get("independence_group", ""),
        target_identity=source_pdb or item.get("source_id", ""),
        conformer_id=conformer_id(item),
        data_partition=partition,
        label_status=label_status,
        experimental_label="",
    )


def verify_run_copy_paths(rows: list[dict[str, str]]) -> None:
    """Require each published run path to use one frozen prefix and safe leaf."""

    for item in rows:
        value = item["run_copy_path"]
        partition = item["data_partition"]
        if partition in {"lockbox", "incomplete_quarantine", "challenger_scaffold"}:
            if value:
                fail(f"inactive partition exposes a run copy path: {item['asset_id']}")
            continue
        path = safe_relative(value, f"run copy path for {item['asset_id']}")
        parts = path.parts
        if partition == "primary_target":
            valid = parts == ("02_inputs", "target", "target.cif")
        elif partition == "runtime":
            valid = (
                len(parts) == 2
                and parts[0] == "runtime"
                and parts[1] in RUNTIME_ORDER
            )
        elif partition == "baseline_scaffold":
            valid = (
                len(parts) == 4
                and parts[:2] == ("02_inputs", "scaffolds")
                and SAFE_SEGMENT.fullmatch(parts[2]) is not None
                and parts[3] == "scaffold.cif"
            )
        elif partition == "positive_compact":
            valid = (
                len(parts) == 5
                and parts[:4]
                == ("02_inputs", "ai_validation", "positive_states", "compact")
                and SAFE_SEGMENT.fullmatch(parts[4]) is not None
            )
        elif partition == "tuning_challenge":
            valid = (
                len(parts) == 4
                and parts[:3]
                == ("02_inputs", "ai_validation", "tuning_challenges")
                and SAFE_SEGMENT.fullmatch(parts[3]) is not None
            )
        else:
            valid = False
        if not valid:
            fail(f"run copy path differs from frozen prefix contract: {value!r}")


def publish(path: Path, rows: list[dict[str, str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = buffer.getvalue().encode("utf-8")
    reject_symlink_components(path, "output", allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(path.parent, "output parent", allow_missing=False)
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
                fail(f"immutable output publication collided: {path}")
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--curation-manifest", required=True)
    parser.add_argument("--runtime-manifest", required=True)
    parser.add_argument("--selected-scaffolds", required=True)
    parser.add_argument("--export-artifacts", required=True)
    parser.add_argument("--screening-criteria", required=True)
    parser.add_argument("--ai-cohorts", required=True)
    parser.add_argument("--ai-overrides", required=True)
    parser.add_argument("--ai-structures", required=True)
    parser.add_argument("--ai-scaffolds", required=True)
    parser.add_argument("--ai-validation-summary", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    paths = {
        name: regular_file(getattr(args, name.replace("-", "_")), name)
        for name in [
            "allowlist", "curation_manifest", "runtime_manifest", "selected_scaffolds",
            "export_artifacts", "screening_criteria", "ai_cohorts", "ai_overrides",
            "ai_structures", "ai_scaffolds", "ai_validation_summary",
        ]
    }
    output = Path(os.path.abspath(args.output))
    reject_symlink_components(output, "output", allow_missing=True)

    allowlist = load_tsv(paths["allowlist"], {"asset", "path", "role", "status", "use_level", "conditions", "sha256"}, "allowlist")
    if any(row_["path"].startswith("raw_sources/") or "/raw_sources/" in row_["path"] for row_ in allowlist):
        fail("raw_sources paths are forbidden in the project-input allowlist")
    curation = load_json(paths["curation_manifest"], "curation manifest")
    runtime = load_json(paths["runtime_manifest"], "runtime manifest")
    selected = load_tsv(paths["selected_scaffolds"], {"selection_rank", "role", "candidate_id", "package_path", "boltzgen_check_status"}, "selected scaffolds")
    exports = load_tsv(paths["export_artifacts"], {"candidate_id", "normalized_cif_path", "normalized_cif_sha256", "curation_json_path", "boltzgen_check_status"}, "export artifacts")
    criteria = load_json(paths["screening_criteria"], "screening criteria")
    cohorts = load_tsv(paths["ai_cohorts"], {"cohort_id", "source_id", "canonical_glob", "ai_role", "default_status", "terminal_chemistry_claim", "limitations"}, "AI cohorts")
    overrides = load_tsv(paths["ai_overrides"], {"relative_path", "status", "reason"}, "AI overrides")
    structures = load_tsv(paths["ai_structures"], {"cohort_id", "source_id", "relative_path", "bytes", "sha256", "ai_role", "status", "active_for_ai", "experimental_negative", "binding_label", "independence_group", "terminal_chemistry_claim", "parse_status", "validation_status", "geometry_complete", "model_numbers", "source_pdb", "limitation_or_reason"}, "AI structures")
    scaffold_rows = load_tsv(paths["ai_scaffolds"], {"instance", "new_rank", "new_folder", "new_qc_status", "ai_status", "old12_overlap", "old12_folder"}, "AI scaffold comparison")
    summary = load_json(paths["ai_validation_summary"], "AI validation summary")
    historical_path = regular_file(
        paths["ai_cohorts"].parent / "historical_output_hashes.tsv",
        "historical-output-hash registry",
    )
    historical = load_tsv(
        historical_path,
        {"filename", "sha256", "contract"},
        "historical-output-hash registry",
    )
    validator_path = find_frozen_file(
        paths["ai_structures"],
        "GLP_/boltzgen/main/asset_validation_20260820/validate_assets.py",
        "frozen AI validator",
    )
    verify_summary(
        summary,
        cohort_path=paths["ai_cohorts"],
        override_path=paths["ai_overrides"],
        structure_path=paths["ai_structures"],
        scaffold_path=paths["ai_scaffolds"],
        validator_path=validator_path,
        cohorts=cohorts,
        structures=structures,
        historical_path=historical_path,
        historical=historical,
    )

    cohort_ids = [safe_segment(item["cohort_id"], "AI cohort ID") for item in cohorts]
    if len(cohort_ids) != len(set(cohort_ids)):
        fail("duplicate AI cohort ID")
    override_paths = [
        safe_relative(item["relative_path"], "AI override path").as_posix()
        for item in overrides
    ]
    override_map = dict(zip(override_paths, overrides, strict=True))
    if len(override_map) != len(overrides):
        fail("duplicate AI override path")
    inventory_paths = [
        safe_relative(item["relative_path"], "AI structure path").as_posix()
        for item in structures
    ]
    inventory_map = dict(zip(inventory_paths, structures, strict=True))
    if len(inventory_map) != len(structures):
        fail("duplicate AI structure path")
    if not set(override_map).issubset(inventory_map):
        fail("AI override path is missing from structure inventory")
    verify_cohort_contract(cohorts, structures, override_map)

    result: list[dict[str, str]] = []

    # The only campaign target is the curated 6X18 peptide geometry.
    target_allow = [item for item in allowlist if item["asset"] == TARGET_ALLOW_CONTRACT["asset"]]
    if len(target_allow) != 1:
        fail("allowlist must contain exactly the frozen 6X18 target asset")
    target_allow_row = target_allow[0]
    for field, expected in TARGET_ALLOW_CONTRACT.items():
        if target_allow_row.get(field) != expected:
            fail(f"6X18 target allowlist {field} differs from the frozen contract")
    target_rel = safe_relative(target_allow_row["path"], "target allowlist path")
    if target_rel.parts[0] != "curated_project_inputs":
        fail("target must come from curated_project_inputs")
    curated_files = curation.get("curated_files")
    if not isinstance(curated_files, list):
        fail("curation manifest curated_files is missing")
    curated_matches = [item for item in curated_files if isinstance(item, dict) and item.get("path") == target_rel.as_posix()]
    if len(curated_matches) != 1:
        fail("target does not join uniquely to curated_files")
    curated = curated_matches[0]
    target_path = regular_file(paths["curation_manifest"].parent / Path(*target_rel.parts), "curated target")
    target_sha = require_sha(target_allow_row["sha256"], "target allowlist")
    if target_sha != curated.get("sha256") or digest(target_path) != target_sha:
        fail("target allowlist/curation/file hash drift")
    if "size_bytes" in curated and int(curated["size_bytes"]) != target_path.stat().st_size:
        fail("target curated size drift")

    structure_records = curation.get("curated_structure_records", [])
    if not isinstance(structure_records, list):
        fail("curated_structure_records is not a list")
    target_records = [item for item in structure_records if isinstance(item, dict) and item.get("curated_path") == target_rel.as_posix()]
    if len(target_records) != 1:
        fail("target must join exactly one curated structure record")
    source_url = str(curated.get("source_url", ""))
    source_snapshot = str(curated.get("source_snapshot", curation.get("dataset_release_context", "")))
    record_count = curated.get("record_count", 1)
    target_format = str(curated.get("format", "PDBx/mmCIF"))
    record = target_records[0]
    expected_record = {
        "artifact_id": "GLP1_7-36NH2_6X18_peptide_only",
        "status": TARGET_ALLOW_CONTRACT["status"],
        "project_role": "receptor-bound positive-target geometry",
        "source_pdb_id": "6X18",
        "curated_path": target_rel.as_posix(),
        "curated_sha256": target_sha,
    }
    for field, expected in expected_record.items():
        if record.get(field) != expected:
            fail(f"target curated structure record differs at {field}")
    record_count = record.get("model_count", record_count)
    raw_files = curation.get("raw_files", [])
    raw_matches = [item for item in raw_files if isinstance(item, dict) and item.get("path") == record.get("source_path")]
    if len(raw_matches) != 1 or raw_matches[0].get("sha256") != record.get("source_sha256"):
        fail("target curated record does not join to one raw provenance record")
    raw_record = raw_matches[0]
    raw_sha = require_sha(str(record.get("source_sha256", "")), "6X18 raw source")
    source_url = str(raw_record.get("source_url", source_url))
    target_format = str(raw_record.get("format", target_format))
    target_transform = find_frozen_file(
        paths["curation_manifest"],
        "GLP_/boltzgen/main/mvp_data_assets_20260818/scripts/curate_small_sources.py",
        "frozen target curation code",
    )
    upstream_target_license = curated.get("license", raw_record.get("license"))
    target_limitation = derived_limitation(
        target_allow_row["conditions"],
        source_asset_id="RCSB:6X18",
        source_sha256=raw_sha,
        evidence_key="transform_code_sha256",
        evidence_sha256=digest(target_transform),
        upstream_license=upstream_target_license if isinstance(upstream_target_license, str) else None,
    )
    result.append(row(
        asset_id="GLP1_7-36_NH2", asset_role="target_geometry", source_url=source_url,
        source_snapshot=source_snapshot, local_source_path=str(target_path),
        run_copy_path="02_inputs/target/target.cif", bytes=str(target_path.stat().st_size),
        records=str(record_count), format=target_format, sha256=target_sha,
        license="DERIVED_SEE_SOURCE_MANIFEST",
        chemistry_status="geometry_only", model_role="primary_target", allowed_in_current_run="true",
        limitation=target_limitation,
        independence_group="PDB:6X18",
        target_identity="GLP1_7-36_NH2", conformer_id="6X18", data_partition="primary_target",
        label_status="geometry_reference_unlabeled", experimental_label="",
    ))

    # Runtime assets are a five-file pinned release, never interpreted as training records.
    runtime_files = runtime.get("files")
    pinned = runtime.get("pinned_sources")
    if runtime.get("boltzgen_release") != "v0.3.2" or not isinstance(runtime_files, list) or not isinstance(pinned, dict):
        fail("runtime manifest release/schema differs")
    runtime_by_name = {item.get("filename"): item for item in runtime_files if isinstance(item, dict)}
    if set(runtime_by_name) != set(RUNTIME_ORDER) or len(runtime_files) != 5:
        fail("runtime manifest must contain exactly the five frozen files")
    model_revision = pinned.get("model_repository", {}).get("revision")
    mols_revision = pinned.get("chemical_dictionary_repository", {}).get("revision")
    if not isinstance(model_revision, str) or not isinstance(mols_revision, str):
        fail("runtime source revisions are missing")
    for filename in RUNTIME_ORDER:
        item = runtime_by_name[filename]
        local = regular_file(paths["runtime_manifest"].parent / filename, f"runtime asset {filename}")
        expected_size = integer_text(item.get("bytes"), f"runtime bytes {filename}")
        expected_sha = require_sha(str(item.get("sha256", "")), f"runtime asset {filename}")
        if local.stat().st_size != expected_size or digest(local) != expected_sha:
            fail(f"runtime asset drift: {filename}")
        if filename in {"boltzgen1_diverse.ckpt", "boltzgen1_adherence.ckpt"}:
            role = "design_checkpoint"
        elif filename == "boltzgen1_ifold.ckpt":
            role = "inverse_fold_checkpoint"
        elif filename == "boltz2_conf_final.ckpt":
            role = "folding_checkpoint"
        else:
            role = "chemical_dictionary"
        result.append(row(
            asset_id=filename, asset_role=role, source_url=str(item.get("source_url", "")),
            source_snapshot=mols_revision if filename == "mols.zip" else model_revision,
            local_source_path=str(local), run_copy_path=f"runtime/{filename}", bytes=str(expected_size),
            records="1", format=str(item.get("format", "")), sha256=expected_sha, license="MIT",
            chemistry_status="not_applicable", model_role="runtime_asset", allowed_in_current_run="true",
            limitation="pinned inference runtime; archive members are not biological records",
            independence_group="", target_identity="", conformer_id="", data_partition="runtime",
            label_status="not_applicable", experimental_label="",
        ))

    # The old twelve selected packages are the complete baseline scaffold set.
    if len(selected) != 12 or len(exports) != 12:
        fail("baseline scaffold registries must each contain exactly 12 rows")
    if [item["selection_rank"] for item in selected] != [str(number) for number in range(1, 13)]:
        fail("selected scaffold ranks must be exactly 1..12")
    roles = [item["role"] for item in selected]
    if roles.count("PRIMARY") != 10 or roles.count("RESERVE") != 2:
        fail("baseline scaffold roles must be PRIMARY=10 and RESERVE=2")
    export_by_id = {item["candidate_id"]: item for item in exports}
    if len(export_by_id) != 12 or set(export_by_id) != {item["candidate_id"] for item in selected}:
        fail("selected/export scaffold candidate sets differ")
    selection_contract = criteria.get("selection")
    if selection_contract is not None and (
        not isinstance(selection_contract, dict)
        or selection_contract.get("primary_count") != 10
        or selection_contract.get("reserve_count") != 2
    ):
        fail("screening criteria selection count drift")
    scaffold_root = paths["selected_scaffolds"].parent.parent
    scaffold_summary_path = regular_file(
        scaffold_root / "registry" / "database_summary.json",
        "scaffold database summary",
    )
    scaffold_summary = load_json(scaffold_summary_path, "scaffold database summary")
    source_release = scaffold_summary.get("source_release")
    if not isinstance(source_release, dict):
        fail("scaffold database summary lacks source_release")
    scaffold_release = source_release.get("release_id")
    scaffold_license = source_release.get("license")
    if not isinstance(scaffold_release, str) or not scaffold_release:
        fail("scaffold source release ID is missing")
    scaffold_transform = find_frozen_file(
        paths["selected_scaffolds"],
        "GLP_/boltzgen/main/sabdab2_scaffold_curation_20260819/scripts/build_scaffold_database.py",
        "frozen scaffold curation code",
    )
    scaffold_transform_sha = digest(scaffold_transform)
    for item in selected:
        exported = export_by_id[item["candidate_id"]]
        safe_segment(item["candidate_id"], "baseline scaffold candidate ID")
        if item["boltzgen_check_status"] != "PASS" or exported["boltzgen_check_status"] != "PASS":
            fail(f"baseline scaffold check is not PASS: {item['candidate_id']}")
        package = safe_relative(item["package_path"], "scaffold package path")
        cif_rel = safe_relative(exported["normalized_cif_path"], "export scaffold path")
        if len(package.parts) != 2 or package.parts[0] != "selected" or cif_rel != package / "scaffold.cif":
            fail(f"baseline scaffold package/export mismatch: {item['candidate_id']}")
        safe_segment(package.parts[1], "baseline scaffold package")
        local = regular_file(scaffold_root / Path(*cif_rel.parts), f"baseline scaffold {item['candidate_id']}")
        expected_sha = require_sha(exported["normalized_cif_sha256"], f"baseline scaffold {item['candidate_id']}")
        if digest(local) != expected_sha:
            fail(f"baseline scaffold hash drift: {item['candidate_id']}")
        curation_rel = safe_relative(exported["curation_json_path"], "scaffold curation record")
        if curation_rel.parent != package:
            fail(f"baseline scaffold curation path differs from package: {item['candidate_id']}")
        curation_path = regular_file(
            scaffold_root / Path(*curation_rel.parts),
            f"baseline scaffold curation {item['candidate_id']}",
        )
        scaffold_curation = load_json(curation_path, f"baseline scaffold curation {item['candidate_id']}")
        if scaffold_curation.get("candidate_id") != item["candidate_id"]:
            fail(f"baseline scaffold curation candidate mismatch: {item['candidate_id']}")
        source_contract = scaffold_curation.get("source")
        if not isinstance(source_contract, dict):
            fail(f"baseline scaffold source contract is missing: {item['candidate_id']}")
        source_asset = source_contract.get("sabdab2_archive_member")
        if not isinstance(source_asset, str) or not source_asset:
            fail(f"baseline scaffold source asset ID is missing: {item['candidate_id']}")
        original = regular_file(
            scaffold_root / Path(*package.parts) / "source_rcsb_original.cif",
            f"baseline scaffold original source {item['candidate_id']}",
        )
        scaffold_limitation = derived_limitation(
            item.get("selection_interpretation", "unlabeled structural scaffold"),
            source_asset_id=f"SABDAB2:{source_asset}",
            source_sha256=digest(original),
            evidence_key="transform_code_sha256",
            evidence_sha256=scaffold_transform_sha,
            upstream_license=scaffold_license if isinstance(scaffold_license, str) else None,
        )
        pdb_code = item.get("pdb_code", "")
        result.append(row(
            asset_id=item["candidate_id"], asset_role="baseline_scaffold",
            source_url=f"https://files.rcsb.org/download/{pdb_code}.cif" if pdb_code else "",
            source_snapshot=scaffold_release, local_source_path=str(local),
            run_copy_path=f"02_inputs/scaffolds/{package.parts[1]}/scaffold.cif",
            bytes=str(local.stat().st_size), records="1", format="PDBx/mmCIF", sha256=expected_sha,
            license="DERIVED_SEE_SOURCE_MANIFEST", chemistry_status="not_applicable",
            model_role="baseline_scaffold", allowed_in_current_run="true",
            limitation=scaffold_limitation,
            independence_group=f"SCAFFOLD:{item['candidate_id']}", target_identity="",
            conformer_id="1", data_partition="baseline_scaffold", label_status="unlabeled_scaffold",
            experimental_label="",
        ))

    # Join AI rows to overrides. Lockbox rows are identity-only: never read their CIFs here.
    compact_paths = {
        path for path, override in override_map.items()
        if override["status"] in {"USE_POSITIVE_FIXED_CONTROL", "USE_POSITIVE_COMPACT"}
    }
    compact = [inventory_map[path] for path in compact_paths]
    tuning = [item for item in structures if item["status"] == "USE_TUNING_CHALLENGE" and bool_text(item["active_for_ai"], f"active_for_ai {item['relative_path']}")]
    lockbox = [item for item in structures if item["status"] == "USE_LOCKBOX_CHALLENGE" and bool_text(item["active_for_ai"], f"active_for_ai {item['relative_path']}")]
    incomplete_paths = {path for path, override in override_map.items() if override["status"] == "EXCLUDE_INCOMPLETE"}
    incomplete = [inventory_map[path] for path in incomplete_paths]
    if len(compact) != 4 or len(tuning) != 11 or len(lockbox) != 21 or len(incomplete) != 4:
        fail(f"AI partition count drift: compact={len(compact)} tuning={len(tuning)} lockbox={len(lockbox)} incomplete={len(incomplete)}")
    if {item["cohort_id"] for item in compact} != {"positive_1d0r_all"}:
        fail("compact panel must use only canonical positive_1d0r_all paths")
    if {conformer_id(item) for item in compact} != {"10", "12", "19", "20"}:
        fail("compact panel must be 1D0R models 10/12/19/20")
    expected_compact_status = {
        "10": "USE_POSITIVE_FIXED_CONTROL",
        "12": "USE_POSITIVE_COMPACT",
        "19": "USE_POSITIVE_COMPACT",
        "20": "USE_POSITIVE_COMPACT",
    }
    for item in compact:
        model = conformer_id(item)
        override = override_map[item["relative_path"]]
        expected_status = expected_compact_status[model]
        if override["status"] != expected_status or item["status"] != expected_status:
            fail(f"compact model {model} override/inventory status is not closed")
        if item["active_for_ai"] != "true":
            fail(f"compact model {model} is not active_for_ai=true")
        if item["ai_role"] != "positive_geometry_sensitivity_ensemble":
            fail(f"compact model {model} has the wrong ai_role")
        if item["binding_label"] != "unknown_or_not_applicable":
            fail(f"compact model {model} carries a forbidden label")
        if not override["reason"].strip() or not item["limitation_or_reason"].strip():
            fail(f"compact model {model} lacks an override or inventory limitation")
    challenge_groups = {item["independence_group"] for item in tuning + lockbox}
    if len(challenge_groups) != 4:
        fail("tuning+lockbox must contain exactly four independence groups")
    if {item.get("source_pdb", "") or item["source_id"] for item in tuning} != {"9IVM", "2L63"}:
        fail("tuning panel identity must be 9IVM + 2L63")
    if {item.get("source_pdb", "") or item["source_id"] for item in lockbox} != {"2B4N", "6LMK"}:
        fail("lockbox identity must be 2B4N + 6LMK")
    if {item.get("source_pdb", "") or item["source_id"] for item in incomplete} != {"9IVG", "9N0E", "6PHI", "7DTY"}:
        fail("incomplete quarantine identity set differs")

    nonlock_for_root = [item["relative_path"] for item in compact + tuning]
    project_root = find_project_root(paths["ai_structures"], nonlock_for_root)
    if project_root is None:
        fail("cannot resolve every allowed AI source path from project ancestry")
    snapshot = str(summary.get("input_registry_sha256", summary["schema_version"]))
    validator_sha = require_sha(summary["validator_sha256"], "AI validator")
    for item in sorted(compact, key=lambda value: int(conformer_id(value))):
        verify_model_file(project_root, item)
        result.append(ai_row(
            item, partition="positive_compact", allowed=True, snapshot=snapshot,
            project_root=project_root, validator_sha256=validator_sha,
        ))
    for item in sorted(tuning, key=lambda value: (value["cohort_id"], value["relative_path"])):
        verify_model_file(project_root, item)
        result.append(ai_row(
            item, partition="tuning_challenge", allowed=True, snapshot=snapshot,
            project_root=project_root, validator_sha256=validator_sha,
        ))
    for item in sorted(lockbox, key=lambda value: (value.get("source_pdb", ""), value["relative_path"])):
        result.append(ai_row(
            item, partition="lockbox", allowed=False, snapshot=snapshot,
            project_root=project_root, validator_sha256=validator_sha,
        ))
    for item in sorted(incomplete, key=lambda value: value.get("source_pdb", "") or value["source_id"]):
        if item["status"] != "EXCLUDE_INCOMPLETE" or item["validation_status"] not in {"PASS", "EXPECTED_EXCLUSION"}:
            fail(f"incomplete quarantine contract drift: {item['relative_path']}")
        result.append(ai_row(
            item, partition="incomplete_quarantine", allowed=False, snapshot=snapshot,
            project_root=project_root, validator_sha256=validator_sha,
            limitation=override_map[item["relative_path"]]["reason"],
        ))

    # New 17 scaffolds remain inactive; overlaps name the checked old canonical package.
    if len(scaffold_rows) != 17 or [item["new_rank"] for item in scaffold_rows] != [str(number) for number in range(1, 18)]:
        fail("challenger scaffold comparison must contain ranks 1..17")
    overlap = [item for item in scaffold_rows if bool_text(item["old12_overlap"], f"old12_overlap {item['instance']}")]
    if len(overlap) != 4 or any(not item["old12_folder"] or "USE_OLD_CANONICAL" not in item["ai_status"] for item in overlap):
        fail("challenger overlap-to-old-canonical mapping differs")
    challenger_inventory = [item for item in structures if item["cohort_id"] == "scaffolds_new17"]
    by_folder: dict[str, dict[str, str]] = {}
    for item in challenger_inventory:
        parts = safe_relative(item["relative_path"], "challenger path").parts
        if len(parts) < 2 or parts[-1] in {"", ".", ".."}:
            fail(f"invalid challenger inventory path: {item['relative_path']}")
        safe_segment(parts[-2], "challenger inventory folder")
        if parts[-2] in by_folder:
            fail(f"duplicate challenger inventory folder: {parts[-2]}")
        by_folder[parts[-2]] = item
    comparison_folders = {
        safe_segment(item["new_folder"], "challenger comparison folder")
        for item in scaffold_rows
    }
    for item in scaffold_rows:
        safe_segment(item["instance"], "challenger instance")
        if item["old12_folder"]:
            safe_segment(item["old12_folder"], "old canonical scaffold folder")
    if len(challenger_inventory) != 17 or set(by_folder) != comparison_folders:
        fail("challenger comparison does not join one-to-one to structure inventory")
    for item in scaffold_rows:
        inventory = by_folder.get(item["new_folder"])
        if inventory is None:
            fail(f"challenger scaffold inventory is missing: {item['instance']}")
        if bool_text(inventory["active_for_ai"], f"challenger active_for_ai {item['instance']}"):
            fail(f"challenger scaffold is unexpectedly active: {item['instance']}")
        if inventory["status"] != item["ai_status"]:
            fail(f"challenger comparison/inventory status mismatch: {item['instance']}")
        local_source = source_path(project_root, inventory["relative_path"])
        byte_count = str(integer_text(inventory["bytes"], "challenger bytes"))
        sha = require_sha(inventory["sha256"], f"challenger {item['instance']}")
        limitation = inventory["limitation_or_reason"]
        old_mapping = f"; old_canonical={item['old12_folder']}" if item["old12_folder"] else ""
        challenger_limitation = derived_limitation(
            f"{item['ai_status']}: {limitation}{old_mapping}",
            source_asset_id=f"AI:{inventory['cohort_id']}:{inventory['relative_path']}",
            source_sha256=sha,
            evidence_key="frozen_validator_sha256",
            evidence_sha256=validator_sha,
            upstream_license=None,
        )
        result.append(row(
            asset_id=f"CHALLENGER:{item['instance']}", asset_role="challenger_scaffold", source_url="",
            source_snapshot=snapshot, local_source_path=local_source, run_copy_path="",
            bytes=byte_count, records="1", format="PDBx/mmCIF", sha256=sha,
            license="DERIVED_SEE_SOURCE_MANIFEST",
            chemistry_status="not_applicable", model_role="challenger_scaffold",
            allowed_in_current_run="false",
            limitation=challenger_limitation,
            independence_group=f"SCAFFOLD:{item['instance']}", target_identity="", conformer_id="1",
            data_partition="challenger_scaffold", label_status="unlabeled_scaffold",
            experimental_label="",
        ))

    ids = [item["asset_id"] for item in result]
    if len(ids) != len(set(ids)):
        fail("output asset IDs are not unique")
    if any("raw_sources/" in item["local_source_path"] for item in result):
        fail("raw_sources path reached the output manifest")
    if any(item["experimental_label"] for item in result):
        fail("experimental labels are forbidden")
    if any(item["allowed_in_current_run"] not in {"true", "false"} for item in result):
        fail("internal allowed flag is invalid")
    if any(item["data_partition"] not in FROZEN_PARTITIONS for item in result):
        fail("output contains a non-frozen data_partition value")
    verify_run_copy_paths(result)
    lineage_patterns = (
        re.compile(r"(?:^|; )source_asset_id=[^;]+"),
        re.compile(r"(?:^|; )source_sha256=[0-9a-f]{64}(?:;|$)"),
        re.compile(r"(?:^|; )upstream_license=[^;]+"),
    )
    evidence_pattern = re.compile(
        r"(?:^|; )(?:transform_code_sha256|frozen_validator_sha256)=[0-9a-f]{64}(?:;|$)"
    )
    for item in result:
        if item["license"] != "DERIVED_SEE_SOURCE_MANIFEST":
            continue
        if not all(pattern.search(item["limitation"]) for pattern in lineage_patterns):
            fail(f"derived row lacks source/license lineage: {item['asset_id']}")
        if evidence_pattern.search(item["limitation"]) is None:
            fail(f"derived row lacks transformation/validator lineage: {item['asset_id']}")
    publish(output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
