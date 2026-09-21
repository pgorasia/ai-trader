#!/usr/bin/env python3
"""Offline deterministic replay of persisted Shadow lifecycle state."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.lifecycle import session_invariants
from trader.state import atomic_write_json


def replay(path: Path) -> dict:
    result = {"session": path.stem, "invariants": [], "plans": [], "timeline": [], "errors": [], "pass": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["errors"].append({"code": "STATE_UNREADABLE", "detail": type(exc).__name__})
        return result
    result["session"] = str(state.get("session_date") or path.stem)
    try:
        as_of = datetime.fromisoformat(str(state["updated_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        as_of = None
        result["errors"].append({"code": "INCOMPLETE_INPUT", "detail": "trusted updated_at unavailable; time invariants omitted"})
    try:
        result["invariants"] = session_invariants(state, as_of=as_of)
    except (KeyError, TypeError, ValueError) as exc:
        result["errors"].append({"code": "INCOMPLETE_LIFECYCLE_INPUT", "detail": str(exc)[:240]})
    for record in state.get("shadow_plans", []):
        outcome = record.get("outcome", {})
        item = {"plan_id": record.get("plan_id"), "role": record.get("research_role", "PRIMARY"),
                "status": outcome.get("status"), "entry_triggered": bool(outcome.get("entry_triggered", False)),
                "exit_reason": outcome.get("exit_reason")}
        result["plans"].append(item)
        for field, event in (("frozen_at", "PLAN_FROZEN"),):
            if record.get(field):
                result["timeline"].append({"timestamp": record[field], "event": event, "plan_id": record.get("plan_id")})
        for field, event in (("entry_timestamp", "ENTRY"), ("exit_timestamp", "EXIT")):
            if outcome.get(field):
                result["timeline"].append({"timestamp": outcome[field], "event": event, "plan_id": record.get("plan_id")})
    for event in state.get("schedule_events", []):
        stamp = event.get("observed_at") or event.get("scheduled_for")
        if stamp:
            result["timeline"].append({"timestamp": stamp, "event": event.get("status"),
                                       "reason_code": event.get("reason_code")})
    result["timeline"].sort(key=lambda item: (str(item.get("timestamp")), str(item.get("event"))))
    # Incomplete evidence is an explicit non-passing replay.  The caller can
    # distinguish it from an invariant violation via ``errors`` without the
    # tool pretending that an unobservable lifecycle was healthy.
    result["pass"] = not result["invariants"] and not result["errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = replay(args.state)
    atomic_write_json(args.output, result)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
