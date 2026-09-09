#!/usr/bin/env python3
"""Report the next VHH revision stage from input-bound receipts; never run a GPU.

Receipts describe checks performed by stage-specific validators, not new evidence
created by this gate. SHA256 bindings catch accidental reuse, not malicious forgery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "VHH_STAGE_RECEIPT_V1"
HEX64 = re.compile(r"[0-9a-f]{64}")


def digest(value):
    """Canonical JSON digest for a protocol or one stage's private input binding."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def candidate_set_digest(candidates):
    """Bind actual sequence identities, independent of arbitrary candidate labels."""
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("nonempty actual candidate sequence records required")
    ids, sequences = [], []
    for row in candidates:
        if not isinstance(row, dict) or not isinstance(row.get("candidate_id"), str) or not row["candidate_id"]:
            raise ValueError("actual candidate_id missing")
        sequence_hash = row.get("vhh_sequence_sha256")
        if not isinstance(sequence_hash, str) or not HEX64.fullmatch(sequence_hash):
            raise ValueError("actual vhh_sequence_sha256 missing or invalid")
        ids.append(row["candidate_id"])
        sequences.append(sequence_hash)
    if len(set(ids)) != len(ids) or len(set(sequences)) != len(sequences):
        raise ValueError("actual candidate identities and sequences must be unique")
    return digest(sorted(sequences))


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_artifacts(artifacts):
    """Verify only small result JSONs, never weights or full coordinate directories."""
    issues = []
    if not isinstance(artifacts, list) or not artifacts:
        return ["result_artifacts must bind at least one actual result JSON"]
    for artifact in artifacts:
        try:
            path = Path(artifact["path"])
            expected = artifact["sha256"]
            if not path.is_absolute() or path.suffix.lower() != ".json" or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("require an absolute small JSON result path (at most 8 MiB)")
            if not isinstance(expected, str) or not HEX64.fullmatch(expected):
                raise ValueError("invalid result SHA256")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("result artifact hash mismatch")
            if not isinstance(json.loads(data.decode("utf-8-sig")), dict):
                raise ValueError("result JSON must be an object")
        except (KeyError, OSError, TypeError, ValueError, UnicodeError) as exc:
            issues.append(f"result artifact invalid: {exc}")
    return issues


def validate_receipt(contract, stage, binding, receipt, now):
    issues = validate_artifacts(receipt.get("result_artifacts"))
    expected = {"schema": SCHEMA, "protocol_id": contract["protocol_id"],
                "protocol_sha256": digest(contract), "stage_id": stage["id"],
                "status": "COMPLETED", "evidence_kind": stage["evidence_kind"],
                "registration": stage["registration"]}
    if not isinstance(binding, dict) or not binding:
        issues.append("missing nonempty stage input binding")
    else:
        expected["input_binding_sha256"] = digest(binding)
    for field, value in expected.items():
        if receipt.get(field) != value:
            issues.append(f"{field}: missing or mismatched")
    try:
        created = timestamp(receipt["created_at_utc"])
        if created > now + timedelta(minutes=5):
            issues.append("receipt timestamp is in the future")
        if stage["registration"] == "PROSPECTIVE":
            frozen = timestamp(receipt["preregistered_at_utc"])
            started = timestamp(receipt["started_at_utc"])
            if not frozen <= started <= created:
                issues.append("preregistration/start/completion timestamps out of order")
    except (KeyError, TypeError, ValueError, AttributeError):
        issues.append("missing or invalid timezone-aware stage timestamps")
    checks = receipt.get("checks", {})
    if not isinstance(checks, dict):
        checks = {}
    for check in stage["required_checks"]:
        if checks.get(check) is not True:
            issues.append(f"check not passed: {check}")
    actual = receipt.get("actual", {})
    if not isinstance(actual, dict):
        actual = {}
    for key, limit in stage["budget"].items():
        if key.startswith("max_") or key.startswith("min_"):
            name = key[4:]
            value = actual.get(name)
            if not number(value) or value < 0:
                issues.append(f"missing or invalid actual.{name}")
            elif key.startswith("max_") and value > limit:
                issues.append(f"actual.{name} exceeds budget")
            elif key.startswith("min_") and value < limit:
                issues.append(f"actual.{name} below required diversity")
            if name != "wall_seconds" and number(value) and (value == 0 or int(value) != value):
                issues.append(f"actual.{name} must be a positive integer")
    budget = stage["budget"]
    if "required_samples" in budget and actual.get("samples") != budget["required_samples"]:
        issues.append("required historical samples not complete")
    if "required_fold_samples" in budget and actual.get("fold_samples") != budget["required_fold_samples"]:
        issues.append("required native fold samples not complete")
    if budget["gpu_allowed"] and actual.get("execution_device") != "cuda":
        issues.append("GPU stage requires actual.execution_device=cuda; CPU self-check is not a free refold")
    if receipt.get("biological_pass") is not False:
        issues.append("biological_pass must be explicitly false")
    if stage["id"] == "diversified_pilot":
        claimed = receipt.get("actual_candidate_set_sha256")
        verified = False
        for artifact in receipt.get("result_artifacts", []) if isinstance(receipt.get("result_artifacts"), list) else []:
            try:
                path = Path(artifact["path"])
                if path.stat().st_size > 8 * 1024 * 1024:
                    continue
                evidence = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(evidence, dict) or "actual_candidate_set_sha256" not in evidence:
                    continue
                observed = candidate_set_digest(evidence.get("candidates"))
                if observed != claimed or evidence["actual_candidate_set_sha256"] != observed:
                    issues.append("pilot actual candidate sequence digest mismatch")
                elif len(evidence["candidates"]) != actual.get("candidates"):
                    issues.append("pilot actual candidate count differs from sequence evidence")
                else:
                    verified = True
            except (KeyError, TypeError, ValueError, OSError):
                issues.append("pilot candidate output evidence invalid")
        if not verified:
            issues.append("pilot actual_candidate_set_sha256 requires actual sequence-bound result evidence")
    if stage["id"] == "native_free_refold":
        calibration = receipt.get("calibration", {})
        if not isinstance(calibration, dict):
            calibration = {}
        thresholds = stage["calibration_thresholds"]
        if not isinstance(binding, dict) or binding.get("calibration_thresholds") != thresholds:
            issues.append("input binding must contain protocol calibration_thresholds")
        if calibration.get("thresholds") != thresholds:
            issues.append("native calibration thresholds missing or changed")
        samples = calibration.get("samples")
        if not isinstance(samples, list) or len(samples) != budget["required_fold_samples"]:
            issues.append("native calibration requires both actual fold metric records")
        else:
            for index, sample in enumerate(samples):
                if not isinstance(sample, dict) or sample.get("sample_index") != index:
                    issues.append("native calibration sample indices must be exactly 0, 1")
                    continue
                for metric, rule in thresholds.items():
                    value = sample.get(metric)
                    if not number(value) or value < 0:
                        issues.append(f"native sample {index} invalid metric: {metric}")
                    elif ((rule["direction"] == "max" and value > rule["value"]) or
                          (rule["direction"] == "min" and value < rule["value"])):
                        issues.append(f"native sample {index} fails calibration: {metric}")
                    if metric.endswith("recall") and number(value) and value > 1:
                        issues.append(f"native sample {index} recall outside [0,1]")
    if stage["id"] == "active_truncated_pairing":
        if receipt.get("terminal_chemistry_status") not in contract["pairing"]["terminal_chemistry_statuses"]:
            issues.append("terminal chemistry status is missing or invalid")
        if receipt.get("target_states") != contract["pairing"]["primary_states"]:
            issues.append("both required primary target states must be reported in contract order")
        count = actual.get("candidates")
        expected_samples = count * 2 * budget["folds_per_candidate_per_state"] if number(count) else None
        if actual.get("fold_samples") != expected_samples:
            issues.append("paired fold sample count incomplete")
    return issues


def evaluate(contract, bindings, receipts, now=None):
    now = now or datetime.now(timezone.utc)
    if contract.get("schema") != "VHH_REVISED_PROTOCOL_V1":
        raise ValueError("unsupported protocol schema")
    if not isinstance(bindings, dict):
        raise ValueError("bindings must be an object keyed by stage_id")
    stage_ids = [stage["id"] for stage in contract["stages"]]
    selected = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("stage_id") not in stage_ids:
            raise ValueError("receipt has unknown or missing stage_id")
        stage_id = receipt["stage_id"]
        if stage_id in selected:
            raise ValueError(f"multiple receipts for {stage_id}; select one explicitly")
        selected[stage_id] = receipt
    report = {"schema": "VHH_STAGE_GATE_REPORT_V1", "protocol_id": contract["protocol_id"],
              "protocol_sha256": digest(contract), "biological_pass": False,
              "selectivity_claim_allowed": False, "gpu_started": False,
              "claim_boundary": contract["claim_boundary"], "stages": [], "next_stage": None}
    waiting = False
    for stage in contract["stages"]:
        stage_id = stage["id"]
        row = {"stage_id": stage_id, "state": "NOT_EVALUATED", "issues": [], "advisories": []}
        if not waiting:
            receipt = selected.get(stage_id)
            if receipt is None:
                row.update(state="READY", issues=["stage receipt missing; stage is not completed"])
            else:
                row["issues"] = validate_receipt(contract, stage, bindings.get(stage_id), receipt, now)
                try:
                    if now - timestamp(receipt["created_at_utc"]) > timedelta(days=contract["receipt_age_advisory_days"]):
                        row["advisories"].append("old receipt accepted only because protocol/input binding still matches; age alone does not trigger rerunning")
                except (KeyError, TypeError, ValueError, AttributeError):
                    pass
                if stage_id == "active_truncated_pairing":
                    current = bindings.get(stage_id, {})
                    candidate_digest = current.get("candidate_set_sha256") if isinstance(current, dict) else None
                    if not isinstance(candidate_digest, str) or not HEX64.fullmatch(candidate_digest):
                        row["issues"].append("stage binding requires candidate_set_sha256")
                if stage_id == "active_truncated_pairing":
                    pilot = selected.get("diversified_pilot", {})
                    paired = bindings.get(stage_id, {})
                    if not isinstance(pilot, dict) or not isinstance(paired, dict) or pilot.get("actual_candidate_set_sha256") != paired.get("candidate_set_sha256"):
                        row["issues"].append("paired candidates do not match the completed pilot actual output sequences")
                row["state"] = "BLOCKED" if row["issues"] else "COMPLETE"
            if row["state"] != "COMPLETE":
                report["next_stage"] = stage_id
                report["next_stage_budget"] = stage["budget"]
                report["next_stage_question"] = stage["question"]
                waiting = True
        report["stages"].append(row)
    report["status"] = "COMPLETE" if not waiting else next(row["state"] for row in report["stages"] if row["state"] in ("READY", "BLOCKED"))
    report["action"] = ("REVIEW_COMPUTATIONAL_RESULTS_NO_BIOLOGICAL_PASS" if not waiting else
                        "REPAIR_OR_RERUN_FAILED_STAGE_NO_DOWNSTREAM_GPU" if report["status"] == "BLOCKED" else
                        "PREPARE_AND_EXECUTE_ONLY_NEXT_STAGE_WITH_ITS_BUDGET")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--receipts", nargs="*", type=Path, default=[])
    args = parser.parse_args()
    try:
        read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
        result = evaluate(read(args.contract), read(args.bindings), [read(path) for path in args.receipts])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "ERROR", "gpu_started": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 42 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
