#!/usr/bin/env python3
"""Append a success or failure event to the Windows-local authoritative registry."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--event", required=True, type=Path)
    return parser.parse_args()


def load_json_object(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"expected a JSON object: {path}")
    return payload


def main() -> int:
    args = parse_args()
    event = load_json_object(args.event)
    for field in ("event_id", "iteration_id", "stage", "outcome", "summary"):
        value = event.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"missing non-empty string field: {field}")
    if event["outcome"] not in {"SUCCESS", "FAILURE", "PARTIAL"}:
        raise SystemExit("outcome must be SUCCESS, FAILURE, or PARTIAL")

    registry = args.registry
    registry.parent.mkdir(parents=True, exist_ok=True)
    existing_ids: set[str] = set()
    if registry.exists():
        for number, line in enumerate(registry.read_text(encoding="utf-8").splitlines(), 1):
            if not line:
                continue
            row = json.loads(line)
            existing_ids.add(row["event_id"])
    if event["event_id"] in existing_ids:
        raise SystemExit(f"duplicate event_id: {event['event_id']}")

    event["schema_version"] = "WINDOWS_OWNER_EXPERIENCE_EVENT_V1"
    event["authority"] = "WINDOWS_CODEX"
    event["review_state"] = "LOCAL_FINAL"
    event["mac_review_required"] = False
    event.setdefault("created_at_utc", dt.datetime.now(dt.timezone.utc).isoformat())
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event["event_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    descriptor = os.open(registry, flags, 0o600)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    print(f"OWNER_EXPERIENCE_APPENDED event_id={event['event_id']} registry={registry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
