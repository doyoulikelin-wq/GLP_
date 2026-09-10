#!/usr/bin/env python3
"""New CPU output contract: legacy binding-site column is optional, never zero.

All original structural, sequence, coordinate, sample-count and file-closure
checks are executed unchanged. If the optional column exists its original
numeric/finite checks remain active. Strict E evaluation is a separate required
step; this wrapper does not replace it or confer biological success.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType

BASE_SHA256 = "55c59b5ba738511496d301ca16454deda6334a6072072295a74ac8fb58d76e83"
OPTIONAL_METRIC = "bindsite_under_8rmsd"
SCHEMA = "VHH_TERMINAL_CELL_VALIDATION_V1"


def load_base_validator():
    """Load one fresh, SHA-bound module without __main__ or any disk writes."""
    path = Path(__file__).resolve().parents[2] / "windows_gpu_handoff_20260829/t3_runtime/validate_cell_output.py"
    if path.is_symlink() or not path.is_file():
        raise ValueError("base validator must be an ordinary file")
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != BASE_SHA256:
        raise ValueError("base validator differs from the reviewed SHA256")
    module = ModuleType("_vhh_terminal_base_validator")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    if module.REQUIRED_ANALYSIS_NUMERIC.count(OPTIONAL_METRIC) != 1:
        raise ValueError("base optional metric must occur exactly once")
    return module


def validate(root_argument: str):
    """Validate a real two-candidate/five-fold terminal arm under the new schema."""
    if not __debug__:
        raise RuntimeError("must run without python -O")
    if os.environ.get("EXPECTED_DESIGNS") != "2" or os.environ.get("EXPECTED_FOLD_SAMPLES", "5") != "5":
        raise ValueError("terminal controls require EXPECTED_DESIGNS=2 and EXPECTED_FOLD_SAMPLES=5")
    root = Path(root_argument)
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("output path must be an existing canonical ordinary directory")
    module = load_base_validator()
    filtering = module.load_yaml_mapping(root / "config/filtering.yaml")
    if filtering.get("filter_bindingsite") is not False:
        raise ValueError("optional legacy binding metric requires the common disabled legacy filter")
    table = root / "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv"
    if table.is_symlink() or not table.is_file():
        raise ValueError("analysis table must be an ordinary file")
    with table.open(newline="", encoding="utf-8") as handle:
        fields = csv.DictReader(handle).fieldnames or []
    present = OPTIONAL_METRIC in fields
    if not present:
        module.REQUIRED_ANALYSIS_NUMERIC = tuple(name for name in module.REQUIRED_ANALYSIS_NUMERIC if name != OPTIONAL_METRIC)
    result = module.validate(str(root))
    wrapper_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {**result, "base_schema_version": result["schema_version"], "schema_version": SCHEMA,
            "base_validator_sha256": result["validator_sha256"], "wrapper_sha256": wrapper_sha,
            "validator_sha256": wrapper_sha,
            "optional_analysis_metric": {"name": OPTIONAL_METRIC, "required": False,
                "present": present, "finite_check_when_present": True,
                "status": "PRESENT_NUMERIC_FINITE_VALIDATED" if present else "NOT_APPLICABLE_ABSENT_NO_VALUE_SYNTHESIZED",
                "role": "legacy token-centre binding-site descriptor; not the common strict E His/Ala endpoint"},
            "base_checks_otherwise_unchanged": True, "strict_E_evaluation_still_required": True,
            "biological_pass": False, "source_outputs_modified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_path")
    parser.add_argument("--receipt-dir", type=Path)
    args = parser.parse_args(argv)
    if args.receipt_dir is not None:
        destination = args.receipt_dir.absolute()
        if destination.exists() or destination.is_symlink() or any((p / ".git").exists() for p in (destination, *destination.parents)):
            raise ValueError("receipt directory must be new and private outside Git")
        destination.mkdir(parents=True, mode=0o700)
    try:
        result = validate(args.output_path)
    except Exception as exc:
        failure = {"schema_version": SCHEMA, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        if args.receipt_dir is not None:
            with (destination / "VALIDATION_FAILED.json").open("x", encoding="utf-8") as handle:
                json.dump(failure, handle, indent=2, sort_keys=True)
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    if args.receipt_dir is not None:
        with (destination / "VALIDATION.json").open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
