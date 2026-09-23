"""Deterministic incident classification, remediation policy, and stability state."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .state import atomic_write_json
from .market_calendar import ET, EquityMarketCalendar

EXIT_HEALTHY = 0
EXIT_EXTERNAL_DEGRADED = 10
EXIT_NOT_ACTIONABLE = 20
EXIT_INTERNAL_DEFECT = 42
EXIT_SAFETY_DEFECT = 43

EXTERNAL_CODES = frozenset({"DATA_UNAVAILABLE", "HISTORICALS_UNAVAILABLE", "INDICATORS_UNAVAILABLE",
                            "ORDERS_UNAVAILABLE", "MODEL_CAPACITY", "HTTP_502", "STALE_QUOTE"})


def classify_failure(code: str, *, recovery_succeeded: bool = False,
                     completed_turn: bool = False) -> str:
    """Classify bounded fault codes; transient infrastructure is never a code defect."""
    normalized = str(code).upper()
    if normalized == "TEARDOWN_DELETE_400" and completed_turn:
        return "BENIGN_TEARDOWN"
    if normalized in EXTERNAL_CODES:
        return "RECOVERED_EXTERNAL" if recovery_succeeded else "EXTERNAL"
    if normalized in {"FOREIGN_MCP", "MULTIPLE_ENTERED_PRIMARY", "ENTERED_PAST_MANDATORY_FLAT"}:
        return "SAFETY"
    if normalized in {"NO_TRADE", "LOW_PNL", "MISSED_LATER_SETUP"}:
        return "NOT_ACTIONABLE"
    return "RECOVERED_INTERNAL" if recovery_succeeded else "INTERNAL"


def classify(findings: list[dict[str, Any]]) -> int:
    severities = {str(item.get("severity")) for item in findings}
    if "SAFETY" in severities: return EXIT_SAFETY_DEFECT
    if "INTERNAL" in severities: return EXIT_INTERNAL_DEFECT
    if "EXTERNAL" in severities: return EXIT_EXTERNAL_DEGRADED
    if "NOT_ACTIONABLE" in severities: return EXIT_NOT_ACTIONABLE
    return EXIT_HEALTHY


def remediation_decision(*, service_inactive: bool, session_active: bool, accepted: bool,
                         acceptance_valid: bool, safety_defect: bool) -> dict[str, Any]:
    eligible = service_inactive and session_active and accepted and acceptance_valid and not safety_defect
    return {"action": "RESTART_SERVICE" if eligible else "NONE",
            "reason_code": "SAFE_ACCEPTED_SERVICE_RESTART" if eligible else "REMEDIATION_PRECONDITIONS_NOT_MET"}


def update_stability(path: Path, *, session: str, complete: bool, internal_defect: bool,
                     external_degradation: bool, recovery_correct: bool,
                     repair_commit: str | None = None,
                     ownership_source: Path | None = None) -> dict[str, Any]:
    try:
        import json
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {"consecutive_clean_sessions": 0, "last_clean_session": None,
                 "last_internal_defect": None, "last_external_degradation": None,
                 "last_repair_commit": None, "status": "BUILDING"}
    date.fromisoformat(session)
    if repair_commit: value["last_repair_commit"] = repair_commit
    if internal_defect:
        value["last_internal_defect"] = session; value["consecutive_clean_sessions"] = 0
    elif not complete:
        value["consecutive_clean_sessions"] = 0
    elif complete and (not external_degradation or recovery_correct):
        if value.get("last_clean_session") != session:
            previous = value.get("last_clean_session")
            consecutive = False
            if previous:
                try:
                    calendar = EquityMarketCalendar("XNYS")
                    following = calendar.next_session(
                        datetime.combine(date.fromisoformat(previous) + timedelta(days=1),
                                         datetime.min.time(), tzinfo=ET)
                    )
                    consecutive = following.session_date == session
                except (TypeError, ValueError):
                    consecutive = False
            value["consecutive_clean_sessions"] = (
                int(value.get("consecutive_clean_sessions", 0)) + 1 if consecutive else 1
            )
        value["last_clean_session"] = session
    if external_degradation: value["last_external_degradation"] = session
    value["status"] = "STABLE" if int(value["consecutive_clean_sessions"]) >= 3 else "BUILDING"
    atomic_write_json(path, value, ownership_source=ownership_source, mode=0o644)
    return value
