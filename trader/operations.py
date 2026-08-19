"""Persisted AI operation and session circuit-breaker primitives."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from .codex_events import sanitize_diagnostic_text
from .models import CodexRunError

OPERATION_STATES = frozenset({"PENDING", "STARTED", "RETRY_WAIT", "COMPLETED", "FAILED_TERMINAL"})
TRANSIENT = re.compile(r"rate.?limit|temporar(?:y|ily)|service unavailable|connection (?:reset|aborted)|http 50[23]", re.I)
NONRETRYABLE = re.compile(r"security|approval|config|schema|invariant|prohibited|unexpected .*tool|invalid_request|contract", re.I)


def ensure_controls(state: dict[str, Any]) -> None:
    state.setdefault("ai_operations", [])
    state.setdefault("ai_circuit", {"status": "CLOSED", "circuit_opened_at": None, "reason": None,
                                     "consecutive_failures": 0, "failure_count": 0,
                                     "last_failure_fingerprint": None})


def operation(state: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    ensure_controls(state)
    return next((item for item in state["ai_operations"] if item.get("operation_id") == operation_id), None)


def prepare(state: dict[str, Any], operation_id: str, operation_type: str,
            scheduled_for: datetime, max_attempts: int) -> dict[str, Any]:
    record = operation(state, operation_id)
    if record is None:
        record = {"operation_id": operation_id, "operation_type": operation_type,
                  "scheduled_for": scheduled_for.isoformat(), "state": "PENDING",
                  "attempt_number": 0, "max_attempts": max_attempts, "next_retry_at": None,
                  "started_at": None, "completed_at": None, "failure_diagnostics": []}
        state["ai_operations"].append(record)
    return record


def eligible(record: dict[str, Any], now: datetime, circuit_open: bool = False) -> bool:
    if circuit_open or record.get("state") in {"STARTED", "COMPLETED", "FAILED_TERMINAL"}:
        return False
    if record.get("state") == "PENDING":
        return now >= datetime.fromisoformat(record["scheduled_for"])
    if record.get("state") == "RETRY_WAIT" and record.get("next_retry_at"):
        return now >= datetime.fromisoformat(record["next_retry_at"])
    return False


def start(record: dict[str, Any], now: datetime) -> None:
    if record["state"] not in {"PENDING", "RETRY_WAIT"}:
        raise ValueError("AI operation is not explicitly eligible to start")
    record.update({"state": "STARTED", "attempt_number": int(record["attempt_number"]) + 1,
                   "started_at": now.isoformat(), "completed_at": None, "next_retry_at": None})


def complete(record: dict[str, Any], now: datetime) -> None:
    record.update({"state": "COMPLETED", "completed_at": now.isoformat(), "next_retry_at": None})


def retry_eligible(error: Exception) -> bool:
    text = sanitize_diagnostic_text(str(error))
    return isinstance(error, CodexRunError) and bool(TRANSIENT.search(text)) and not NONRETRYABLE.search(text)


def safe_failure_diagnostic(record: dict[str, Any], error: Exception, now: datetime,
                            decision: str, runner_diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    supplied = (getattr(error, "diagnostics", None) or (runner_diagnostics or {}).get("codex_failure_diagnostics") or {})
    structured = supplied.get("structured_error") if isinstance(supplied.get("structured_error"), dict) else {}
    lifecycle: dict[tuple[str, str], int] = {}
    for item in supplied.get("event_sequence", []):
        if isinstance(item, dict) and item.get("event") in {"tool.started", "tool.completed"} and isinstance(item.get("tool"), str):
            key = (item["tool"], "STARTED" if item["event"] == "tool.started" else "COMPLETED")
            lifecycle[key] = lifecycle.get(key, 0) + 1
    safe = {
        "operation_type": record["operation_type"], "operation_id": record["operation_id"],
        "scheduled_for": record["scheduled_for"], "started_at": record.get("started_at"),
        "completed_at": now.isoformat(), "attempt_number": record["attempt_number"],
        "max_attempts": record["max_attempts"], "exception_class": type(error).__name__,
        "sanitized_error": {
            "code": structured.get("code", supplied.get("code", "NO_SAFE_STRUCTURED_CODE_AVAILABLE")),
            "message": sanitize_diagnostic_text(str(structured.get("message") or error)),
            "process_return_code": supplied.get("process_return_code"),
            "stage_reached": supplied.get("stage_reached", "UNKNOWN"),
        },
        "event_summary": {key: int(supplied.get(key, 0)) for key in
                          ("turn_started_count", "turn_completed_count", "turn_failed_count", "structured_error_count")},
        "tool_summary": [{"tool": tool, "state": status, "count": count}
                         for (tool, status), count in sorted(lifecycle.items())],
        "observed_tool_summary": supplied.get("observed_tool_summary", []),
        "foreign_mcp": supplied.get("foreign_mcp", []),
        "missing_required_tools": supplied.get("missing_required_tools", []),
        "teardown_classifier": {"reached": bool(supplied.get("teardown_classifier_reached", False)),
                                "result": supplied.get("teardown_classifier_result"),
                                "diagnostic_code": supplied.get("teardown_diagnostic_code")},
        "decision": decision,
    }
    return safe


def fail(record: dict[str, Any], error: Exception, now: datetime,
         runner_diagnostics: dict[str, Any] | None = None) -> str:
    retry = retry_eligible(error) and record["operation_type"] == "EOD" and record["attempt_number"] < record["max_attempts"]
    decision = "RETRY_AT" if retry else "TERMINAL_FAILED"
    if retry:
        delay = timedelta(seconds=60 if record["attempt_number"] == 1 else 300)
        record.update({"state": "RETRY_WAIT", "next_retry_at": (now + delay).isoformat(), "completed_at": now.isoformat()})
    else:
        record.update({"state": "FAILED_TERMINAL", "next_retry_at": None, "completed_at": now.isoformat()})
    record.setdefault("failure_diagnostics", []).append(safe_failure_diagnostic(record, error, now, decision, runner_diagnostics))
    return decision


def record_ai_failure(state: dict[str, Any], error: Exception, now: datetime,
                      consecutive_threshold: int = 3, total_threshold: int = 5) -> bool:
    ensure_controls(state); circuit = state["ai_circuit"]
    fingerprint = hashlib.sha256(f"{type(error).__name__}:{sanitize_diagnostic_text(str(error))}".encode()).hexdigest()
    circuit["consecutive_failures"] = int(circuit.get("consecutive_failures", 0)) + 1
    circuit["failure_count"] = int(circuit.get("failure_count", 0)) + 1
    circuit["last_failure_fingerprint"] = fingerprint
    if circuit["status"] != "OPEN" and (circuit["consecutive_failures"] >= consecutive_threshold or circuit["failure_count"] >= total_threshold):
        circuit.update({"status": "OPEN", "circuit_opened_at": now.isoformat(),
                        "reason": "CONSECUTIVE_FAILURE_THRESHOLD" if circuit["consecutive_failures"] >= consecutive_threshold else "TOTAL_FAILURE_THRESHOLD"})
        return True
    return False


def record_ai_success(state: dict[str, Any]) -> None:
    ensure_controls(state)
    if state["ai_circuit"]["status"] != "OPEN":
        state["ai_circuit"]["consecutive_failures"] = 0
