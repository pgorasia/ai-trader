"""Pure mechanical lifecycle and session-invariant primitives.

This module deliberately has no model, network, broker, filesystem, or clock
dependencies.  Callers must supply a trusted ``as_of`` and completed bars.
``OPEN`` is the historical persisted spelling of the explicit ENTERED state.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from enum import StrEnum
import math
from typing import Any, Iterable


class PlanState(StrEnum):
    PENDING = "PENDING"
    ENTERED = "ENTERED"
    PRE_ENTRY_INVALIDATED = "PRE_ENTRY_INVALIDATED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    AMBIGUOUS = "AMBIGUOUS"


TERMINAL_PERSISTED_STATES = frozenset(
    {"PRE_ENTRY_INVALIDATED", "EXPIRED", "TARGET1", "STOPPED", "FLAT_TIME", "CLOSED", "AMBIGUOUS"}
)
ENTERED_PERSISTED_STATES = frozenset({"OPEN", "ENTERED"})


def validate_completed_bar(bar: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Validate completed OHLC evidence, returning ``None`` for a forming bar.

    Lifecycle evaluators deliberately ignore forming bars.  Once a bar claims
    to be complete, however, malformed or impossible OHLC must fail closed
    instead of being interpreted as an ordinary non-trigger.
    """
    if bar.get("complete") is not True:
        return None
    try:
        opened, high, low, close = (float(bar[key]) for key in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("completed bar has malformed OHLC") from exc
    if (not all(math.isfinite(value) and value > 0 for value in (opened, high, low, close))
            or low > high or not low <= opened <= high or not low <= close <= high):
        raise ValueError("completed bar has impossible OHLC")
    return opened, high, low, close


def parse_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("lifecycle timestamps must be timezone-aware")
    return parsed


def canonical_state(value: str) -> PlanState:
    if value in ENTERED_PERSISTED_STATES:
        return PlanState.ENTERED
    if value in {"TARGET1", "STOPPED", "FLAT_TIME", "CLOSED"}:
        return PlanState.CLOSED
    return PlanState(value)


def evaluate_pre_entry_invalidation(plan: dict[str, Any], bar: dict[str, Any]) -> bool:
    price = plan.get("pre_entry_invalidation_price")
    kind = plan.get("pre_entry_invalidation_type")
    if price is None and kind is None:
        return False
    if price is None or kind != "COMPLETED_5M_CLOSE_BELOW":
        raise ValueError("malformed structured pre-entry invalidation")
    threshold = float(price)
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("invalid pre-entry invalidation price")
    prices = validate_completed_bar(bar)
    return prices is not None and prices[3] < threshold


def evaluate_entry_trigger(plan: dict[str, Any], bar: dict[str, Any], *, qualitative_entry_confirmed: bool | None = None) -> float | None:
    """Return deterministic hypothetical fill, never bypassing qualitative gates."""
    prices = validate_completed_bar(bar)
    if prices is None:
        return None
    if plan.get("entry_requires_qualitative_confirmation", False) and qualitative_entry_confirmed is not True:
        return None
    trigger, chase = float(plan["entry_trigger"]), float(plan["maximum_chase_price"])
    opened, high, _low, close = prices
    if not all(math.isfinite(value) and value > 0 for value in (trigger, chase, opened, high, close)):
        raise ValueError("invalid entry price evidence")
    if chase < trigger:
        raise ValueError("maximum chase price is below entry trigger")
    # A close-confirmed trigger cannot be inferred from an earlier intrabar
    # high.  Its hypothetical fill is the completed close, matching the
    # production monitor's conservative/no-lookahead semantics.
    if plan.get("entry_trigger_type") == "COMPLETED_5M_CLOSE_AT_OR_ABOVE":
        return close if trigger <= close <= chase else None
    if opened >= trigger:
        return opened if opened <= chase else None
    if high >= trigger:
        return trigger if trigger <= chase else None
    return None


def evaluate_entry_cutoff(plan: dict[str, Any], as_of: datetime) -> bool:
    return as_of >= parse_time(plan["latest_entry_time"])


def evaluate_stop(plan: dict[str, Any], bar: dict[str, Any], stop: float | None = None) -> bool:
    prices = validate_completed_bar(bar)
    if prices is None:
        return False
    threshold = float(stop if stop is not None else plan["stop_price"])
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("invalid stop price")
    return prices[2] <= threshold


def evaluate_target(plan: dict[str, Any], bar: dict[str, Any]) -> bool:
    prices = validate_completed_bar(bar)
    if prices is None:
        return False
    threshold = float(plan["target1"])
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("invalid target price")
    return prices[1] >= threshold


def evaluate_time_exit(plan: dict[str, Any], as_of: datetime) -> bool:
    return as_of >= parse_time(plan["mandatory_flat_time"])


def evaluate_trailing_stop(current_stop: float, completed_lows: Iterable[float]) -> float:
    values = [float(value) for value in completed_lows]
    stop = float(current_stop)
    if not math.isfinite(stop) or stop <= 0 or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("invalid trailing-stop evidence")
    return max(stop, min(values)) if values else stop


def transition_plan(current: str, event: str) -> str:
    """Small total transition table; illegal resurrection fails loudly."""
    state = canonical_state(current)
    if state in {PlanState.PRE_ENTRY_INVALIDATED, PlanState.EXPIRED, PlanState.CLOSED, PlanState.AMBIGUOUS}:
        if event == "OBSERVE":
            return current
        raise ValueError(f"terminal plan cannot transition: {current} -> {event}")
    table = {
        (PlanState.PENDING, "ENTRY"): "OPEN",
        (PlanState.PENDING, "INVALIDATE"): "PRE_ENTRY_INVALIDATED",
        (PlanState.PENDING, "CUTOFF"): "EXPIRED",
        (PlanState.PENDING, "AMBIGUOUS"): "AMBIGUOUS",
        (PlanState.ENTERED, "STOP"): "STOPPED",
        (PlanState.ENTERED, "TARGET"): "TARGET1",
        (PlanState.ENTERED, "TIME_EXIT"): "FLAT_TIME",
        (PlanState.ENTERED, "AMBIGUOUS"): "AMBIGUOUS",
    }
    if event == "OBSERVE":
        return current
    try:
        return table[(state, event)]
    except KeyError as exc:
        raise ValueError(f"invalid plan transition: {current} -> {event}") from exc


def reconcile_plan_state(plan_record: dict[str, Any], as_of: datetime) -> tuple[dict[str, Any], list[str]]:
    """Apply only recovery transitions provable without market fabrication."""
    record = deepcopy(plan_record)
    outcome = record.setdefault("outcome", {"status": "PENDING", "entry_triggered": False})
    plan = record["original_plan"]
    reasons: list[str] = []
    status = str(outcome.get("status", "PENDING"))
    if status == "PENDING" and evaluate_entry_cutoff(plan, as_of):
        outcome.update(status="EXPIRED", entry_triggered=False, entry_before_cutoff=False,
                       exit_reason="ENTRY_NOT_TRIGGERED_BEFORE_CUTOFF")
        reasons.append("PENDING_EXPIRED_AT_ENTRY_CUTOFF")
    elif status in ENTERED_PERSISTED_STATES and evaluate_time_exit(plan, as_of):
        # Price cannot be reconstructed safely. Mark ambiguity rather than invent an exit.
        outcome.update(status="AMBIGUOUS", exit_reason="MANDATORY_FLAT_PRICE_EVIDENCE_MISSING")
        reasons.append("ENTERED_PAST_MANDATORY_FLAT_WITHOUT_PRICE")
    return record, reasons


def scheduler_decision(plans: list[dict[str, Any]], as_of: datetime, *, scanner_due: bool) -> dict[str, Any]:
    primary = [p for p in plans if p.get("research_role", "PRIMARY") == "PRIMARY"]
    entered = [p for p in primary if canonical_state(str(p.get("outcome", {}).get("status", "PENDING"))) == PlanState.ENTERED]
    pending = [p for p in plans if canonical_state(str(p.get("outcome", {}).get("status", "PENDING"))) == PlanState.PENDING]
    if entered:
        return {"scanner": False, "monitor": True, "reason_code": "PRIMARY_ENTERED_ONE_ENTRY_LOCK", "as_of": as_of.isoformat()}
    if pending:
        return {"scanner": False, "monitor": True, "reason_code": "ACTIVE_PLAN_MONITORING", "as_of": as_of.isoformat()}
    return {"scanner": bool(scanner_due), "monitor": False,
            "reason_code": "SCAN_SLOT_ELIGIBLE" if scanner_due else "NO_SCAN_SLOT_DUE", "as_of": as_of.isoformat()}


def completed_eod_operation_id(state: dict[str, Any]) -> str | None:
    """Return the one persisted operation that authoritatively completed EOD."""
    session = str(state.get("session_date", ""))
    candidates = (f"eod:{session}", f"eod-recovery:{session}")
    completed = {
        str(item.get("operation_id"))
        for item in state.get("ai_operations", [])
        if (item.get("state") == "COMPLETED"
            and item.get("operation_type") in {"EOD", "EOD_RECOVERY"})
    }
    matching = [operation_id for operation_id in candidates if operation_id in completed]
    persisted = state.get("operation_ids", [])
    if len(matching) == 1 and matching[0] in persisted:
        return matching[0]
    # Early schema-v2 sessions persisted successful regular EOD provenance in
    # operation_ids before ai_operations became authoritative.  Accept that
    # representation only when no regular EOD record exists at all.  A
    # FAILED_TERMINAL/STARTED/RETRY_WAIT record must never be upgraded merely
    # because its identifier was also persisted.
    regular, recovery = candidates
    regular_records = [
        item for item in state.get("ai_operations", [])
        if item.get("operation_id") == regular and item.get("operation_type") == "EOD"
    ]
    if not regular_records and regular in persisted and recovery not in persisted:
        return regular
    return None


def session_invariants(state: dict[str, Any], *, as_of: datetime | None = None) -> list[dict[str, str]]:
    """Return bounded structured invariant violations without mutating state."""
    problems: list[dict[str, str]] = []
    def add(code: str, severity: str, detail: str) -> None:
        problems.append({"code": code, "severity": severity, "detail": detail[:240]})

    plans = state.get("shadow_plans", [])
    entered_primary = [p for p in plans if p.get("research_role", "PRIMARY") == "PRIMARY"
                       and (p.get("outcome", {}).get("entry_triggered") is True
                            or p.get("outcome", {}).get("status") in ENTERED_PERSISTED_STATES)]
    if len(entered_primary) > 1:
        add("MULTIPLE_ENTERED_PRIMARY", "SAFETY", "more than one PRIMARY plan entered")
    if len({p.get("plan_id") for p in plans}) != len(plans):
        add("DUPLICATE_PLAN_ID", "INTERNAL", "plan identifiers are not unique")
    for plan in plans:
        outcome = plan.get("outcome", {})
        status = str(outcome.get("status", ""))
        if status == "PENDING" and outcome.get("entry_triggered"):
            add("PENDING_MARKED_ENTERED", "INTERNAL", str(plan.get("plan_id", "unknown")))
        if status in {"PRE_ENTRY_INVALIDATED", "EXPIRED"} and outcome.get("entry_triggered"):
            add("TERMINAL_UNENTERED_HAS_ENTRY", "INTERNAL", str(plan.get("plan_id", "unknown")))
        trailing = plan.get("trailing_outcome") or {}
        trailing_status = str(trailing.get("status", ""))
        if trailing_status == "PENDING" and trailing.get("entry_triggered"):
            add("TRAILING_PENDING_MARKED_ENTERED", "INTERNAL", str(plan.get("plan_id", "unknown")))
        if trailing_status in {"PRE_ENTRY_INVALIDATED", "EXPIRED"} and trailing.get("entry_triggered"):
            add("TRAILING_TERMINAL_UNENTERED_HAS_ENTRY", "INTERNAL", str(plan.get("plan_id", "unknown")))
        if as_of is not None and status == "PENDING" and as_of >= parse_time(plan["original_plan"]["latest_entry_time"]):
            add("PENDING_PAST_CUTOFF", "INTERNAL", str(plan.get("plan_id", "unknown")))
        if as_of is not None and status in ENTERED_PERSISTED_STATES and as_of >= parse_time(plan["original_plan"]["mandatory_flat_time"]):
            add("ENTERED_PAST_MANDATORY_FLAT", "SAFETY", str(plan.get("plan_id", "unknown")))

    usage = state.get("usage_counts", {})
    eod_id = f"eod:{state.get('session_date', '')}"
    eod_ops = [op for op in state.get("ai_operations", []) if op.get("operation_id") == eod_id]
    recovery_id = f"eod-recovery:{state.get('session_date', '')}"
    recovery_ops = [op for op in state.get("ai_operations", []) if op.get("operation_id") == recovery_id]
    completed_op = completed_eod_operation_id(state) is not None
    complete = state.get("eod_completed") is True
    review = state.get("eod_review")
    provenance = (isinstance(review, dict)
                  and review.get("session_date") == state.get("session_date"))
    if complete and not (completed_op and provenance and int(usage.get("eod_completed_runs", 0)) >= 1):
        add("EOD_COMPLETION_CONTRADICTION", "INTERNAL", "completion flag lacks operation/review/counter provenance")
    if int(usage.get("eod_completed_runs", 0)) > int(usage.get("eod_runs", 0)):
        add("IMPOSSIBLE_EOD_COUNTERS", "INTERNAL", "completed EOD runs exceed EOD runs")
    for completed_key, attempted_key in (("codex_failed_attempts", "codex_subprocess_attempts"),
                                          ("stage_b_completed_runs", "codex_subprocess_attempts"),
                                          ("sol_completed_runs", "codex_subprocess_attempts"),
                                          ("monitor_completed_runs", "codex_subprocess_attempts")):
        if int(usage.get(completed_key, 0)) > int(usage.get(attempted_key, 0)):
            add("IMPOSSIBLE_OPERATION_COUNTERS", "INTERNAL", f"{completed_key} exceeds {attempted_key}")
    circuit = state.get("ai_circuit", {})
    if circuit.get("status") == "OPEN" and not circuit.get("reason"):
        add("CIRCUIT_OPEN_WITHOUT_REASON", "INTERNAL", "open circuit lacks reason")
    for event in state.get("schedule_events", []):
        if str(event.get("status", "")).startswith("SUPPRESSED") and not event.get("reason_code"):
            add("SCHEDULER_SUPPRESSION_WITHOUT_REASON", "INTERNAL", str(event.get("operation_id", "unknown")))
    return problems
