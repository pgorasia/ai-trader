from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import ShadowPlanStatus, StateCorruptionError


STATE_SCHEMA_VERSION = 2
STRATEGY_VERSION = "ai-daytrader-v1-accelerated-shadow-2026-08"
STATE_MIGRATION_POLICY = "No implicit migrations. Unsupported versions require an explicit, reviewed migration."
STATE_LIST_FIELDS = ("cycles", "senior_decisions", "shadow_plans", "shadow_positions", "completed_shadow_trades", "errors", "security_events", "schedule_events", "operation_ids")
FINAL_OUTCOMES = {"TARGET1", "STOPPED", "FLAT_TIME", "EXPIRED", "AMBIGUOUS"}


def _aware(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateCorruptionError(f"Invalid {label}") from exc
    if parsed.tzinfo is None:
        raise StateCorruptionError(f"{label} must be timezone-aware")
    return parsed


def _finite_tree(value: Any, path: str = "state") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise StateCorruptionError(f"Non-finite number at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _finite_tree(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _finite_tree(child, f"{path}[{index}]")


def initial_state(session_date: str, timezone_name: str = "America/New_York", now: datetime | None = None) -> dict[str, Any]:
    date.fromisoformat(session_date)
    stamp = (now or datetime.now(ZoneInfo(timezone_name))).isoformat()
    state: dict[str, Any] = {
        "version": STATE_SCHEMA_VERSION, "revision": 0, "mode": "SHADOW", "session_date": session_date,
        "strategy_version": STRATEGY_VERSION,
        "created_at": stamp, "updated_at": stamp, "cooldowns": {},
        "usage_counts": {"luna_runs": 0, "sol_runs": 0, "preflight_runs": 0, "monitor_runs": 0, "eod_runs": 0,
                         "codex_subprocess_attempts": 0, "codex_failed_attempts": 0,
                         "stage_b_completed_runs": 0, "stage_b_failed_slots": 0,
                         "sol_completed_runs": 0, "monitor_completed_runs": 0,
                         "eod_completed_runs": 0, "eod_failed_attempts": 0,
                         "session_circuit_breaker_trips": 0,
                         "robinhood_tool_calls": 0, "web_searches": 0, "failed_runs": 0, "tokens": {}},
        "eod_completed": False,
        "preflight_operations": [],
        "ai_operations": [],
        "ai_circuit": {"status": "CLOSED", "circuit_opened_at": None, "reason": None,
                       "consecutive_failures": 0, "failure_count": 0, "last_failure_fingerprint": None},
    }
    for field in STATE_LIST_FIELDS:
        state[field] = []
    state["baseline_positions"] = []
    state["baseline_external_orders"] = []
    return state


def validate_state_shape(state: Any, expected_date: str | None = None) -> None:
    if not isinstance(state, dict):
        raise StateCorruptionError("State root must be a JSON object")
    required = {"version", "revision", "mode", "session_date", "created_at", "updated_at", "cycles", "senior_decisions", "cooldowns", "shadow_plans", "shadow_positions", "completed_shadow_trades", "errors", "security_events", "schedule_events", "operation_ids", "usage_counts", "eod_completed"}
    missing = sorted(required - state.keys())
    if missing:
        raise StateCorruptionError(f"State is missing required fields: {', '.join(missing)}")
    if state["version"] != STATE_SCHEMA_VERSION:
        raise StateCorruptionError(f"Unsupported state schema version {state.get('version')}; {STATE_MIGRATION_POLICY}")
    if not isinstance(state["revision"], int) or state["revision"] < 0:
        raise StateCorruptionError("State revision must be a non-negative integer")
    if state["mode"] != "SHADOW":
        raise StateCorruptionError("Only SHADOW state is supported")
    if "strategy_version" in state and (not isinstance(state["strategy_version"], str) or not state["strategy_version"].strip()):
        raise StateCorruptionError("Invalid strategy_version")
    try:
        date.fromisoformat(state["session_date"])
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError("Invalid state session_date") from exc
    if expected_date and state["session_date"] != expected_date:
        raise StateCorruptionError("State session_date does not match its filename")
    _aware(state["created_at"], "created_at"); _aware(state["updated_at"], "updated_at")
    for field in STATE_LIST_FIELDS:
        if not isinstance(state.get(field), list):
            raise StateCorruptionError(f"State field {field} must be a list")
    baseline_positions = state.get("baseline_positions", [])
    if not isinstance(baseline_positions, list):
        raise StateCorruptionError("State field baseline_positions must be a list")
    for item in baseline_positions:
        if set(item) != {"attribution", "symbol", "quantity"} or item["attribution"] != "BASELINE_EXTERNAL":
            raise StateCorruptionError("Invalid baseline position attribution")
        if not isinstance(item["symbol"], str) or not item["symbol"] or not isinstance(item["quantity"], (int, float)) or item["quantity"] <= 0:
            raise StateCorruptionError("Invalid baseline position")
    baseline_orders = state.get("baseline_external_orders", [])
    if not isinstance(baseline_orders, list):
        raise StateCorruptionError("State field baseline_external_orders must be a list")
    for item in baseline_orders:
        if set(item) != {"attribution", "symbol", "side", "state"} or item["attribution"] != "BASELINE_EXTERNAL_ORDER":
            raise StateCorruptionError("Invalid baseline external order attribution")
        if not all(isinstance(item[key], str) and item[key] for key in ("symbol", "side", "state")):
            raise StateCorruptionError("Invalid baseline external order")
    for field in ("shadow_positions", "completed_shadow_trades"):
        if any(item.get("attribution") != "SHADOW_AI" for item in state[field]):
            raise StateCorruptionError(f"Invalid {field} attribution")
    if len(state["operation_ids"]) != len(set(state["operation_ids"])):
        raise StateCorruptionError("operation_ids must be unique")
    if not all(isinstance(item, str) and item for item in state["operation_ids"]):
        raise StateCorruptionError("operation_ids must contain non-empty strings")
    if not isinstance(state["cooldowns"], dict) or not isinstance(state["usage_counts"], dict) or not isinstance(state["eod_completed"], bool):
        raise StateCorruptionError("Invalid state control fields")
    if "preflight_operations" in state and not isinstance(state["preflight_operations"], list):
        raise StateCorruptionError("Invalid preflight operation metadata")
    preflights = state.get("preflight_operations", [])
    preflight_ids = [item.get("operation_id") for item in preflights if isinstance(item, dict)]
    if len(preflight_ids) != len(preflights) or len(preflight_ids) != len(set(preflight_ids)):
        raise StateCorruptionError("Preflight operation IDs must be unique")
    for item in preflights:
        if item.get("status") not in {"STARTED", "COMPLETED", "FAILED"} or not isinstance(item.get("report_artifact"), str):
            raise StateCorruptionError("Invalid preflight operation record")
        _aware(item.get("started_at"), "preflight started_at")
        if item["status"] == "STARTED" and item.get("completed_at") is not None:
            raise StateCorruptionError("Started preflight has a completion timestamp")
        if item["status"] != "STARTED" and not item.get("completed_at"):
            raise StateCorruptionError("Terminal preflight lacks a completion timestamp")
        if item.get("completed_at"):
            _aware(item["completed_at"], "preflight completed_at")
    operations = state.get("ai_operations", [])
    if not isinstance(operations, list) or len({item.get("operation_id") for item in operations if isinstance(item, dict)}) != len(operations):
        raise StateCorruptionError("Invalid AI operation metadata")
    for item in operations:
        if item.get("state") not in {"PENDING", "STARTED", "RETRY_WAIT", "COMPLETED", "FAILED_TERMINAL"}:
            raise StateCorruptionError("Invalid AI operation state")
        _aware(item.get("scheduled_for"), "AI operation scheduled_for")
        if item.get("next_retry_at"): _aware(item["next_retry_at"], "AI operation next_retry_at")
    circuit = state.get("ai_circuit", {"status": "CLOSED"})
    if not isinstance(circuit, dict) or circuit.get("status") not in {"CLOSED", "OPEN"}:
        raise StateCorruptionError("Invalid session AI circuit")
    _unique_ids(state["cycles"], "cycle_id", "cycle")
    _unique_ids(state["senior_decisions"], "source_cycle_id", "senior decision")
    _unique_ids(state["shadow_plans"], "plan_id", "plan")
    _unique_ids(state["completed_shadow_trades"], "plan_id", "trade")
    entered_plans = sum(1 for item in state["shadow_plans"] if item.get("research_role", "PRIMARY") == "PRIMARY" and item.get("outcome", {}).get("entry_triggered") is True)
    if entered_plans > 1 or len(state["completed_shadow_trades"]) > 1:
        raise StateCorruptionError("V1 permits no more than one entered Shadow trade per session")
    if sum(1 for item in state["shadow_plans"] if item.get("research_role", "PRIMARY") == "PRIMARY") > 1:
        raise StateCorruptionError("Research basket contains more than one primary plan")
    research_outcomes = state.get("research_outcomes", [])
    if not isinstance(research_outcomes, list):
        raise StateCorruptionError("research_outcomes must be a list")
    research_keys = [(item.get("plan_id"), item.get("variant")) for item in research_outcomes if isinstance(item, dict)]
    if len(research_keys) != len(research_outcomes) or len(research_keys) != len(set(research_keys)):
        raise StateCorruptionError("Research outcome identities must be unique")
    if any(variant not in {"FIXED_TARGET", "TRAILING_STOP"} for _plan_id, variant in research_keys):
        raise StateCorruptionError("Unknown research exit variant")
    plan_ids = {item["plan_id"] for item in state["shadow_plans"]}
    if any(plan_id not in plan_ids for plan_id, _variant in research_keys):
        raise StateCorruptionError("Research outcome lacks frozen-plan provenance")
    for item in research_outcomes:
        if item.get("entry_timestamp"): _aware(item["entry_timestamp"], "research entry timestamp")
        if item.get("exit_timestamp"): _aware(item["exit_timestamp"], "research exit timestamp")
    for cycle in state["cycles"]:
        if "timestamp" in cycle:
            timestamp = _aware(cycle["timestamp"], "cycle timestamp")
            if timestamp.date().isoformat() != state["session_date"]:
                raise StateCorruptionError("Cycle timestamp is outside state session")
        for finalist in cycle.get("finalists", []):
            if finalist.get("classification") not in {"NEW", "MATERIALLY_REQUALIFIED", "COOLDOWN", "PREVIOUSLY_REJECTED_NO_MATERIAL_CHANGE"}:
                raise StateCorruptionError("Invalid finalist classification")
    for decision in state["senior_decisions"]:
        if decision.get("decision") not in {"NO_TRADE", "SHADOW_TRADE_PLAN"}:
            raise StateCorruptionError("Invalid senior decision enum")
        timestamp = _aware(decision.get("decision_timestamp"), "senior decision timestamp")
        if timestamp.date().isoformat() != state["session_date"]:
            raise StateCorruptionError("Senior timestamp is outside state session")
    for trade in state["completed_shadow_trades"]:
        entry = _aware(trade.get("entry_timestamp"), "trade entry timestamp")
        exit_time = _aware(trade.get("exit_timestamp"), "trade exit timestamp")
        if entry.date().isoformat() != state["session_date"] or exit_time < entry:
            raise StateCorruptionError("Invalid completed-trade chronology")
    for symbol, cooldown in state["cooldowns"].items():
        if not isinstance(cooldown, dict) or cooldown.get("symbol") != symbol:
            raise StateCorruptionError("Invalid cooldown structure")
        rejected = _aware(cooldown.get("rejected_at"), "cooldown rejected_at")
        until = _aware(cooldown.get("cooldown_until"), "cooldown_until")
        if until <= rejected or not isinstance(cooldown.get("rejection_categories"), list):
            raise StateCorruptionError("Invalid cooldown chronology")
        history = cooldown.get("rejection_history", [])
        if not isinstance(history, list):
            raise StateCorruptionError("Invalid cooldown history")
        previous_time = None
        for prior in history:
            prior_time = _aware(prior.get("rejected_at"), "prior cooldown rejected_at")
            _aware(prior.get("cooldown_until"), "prior cooldown_until")
            if previous_time is not None and prior_time <= previous_time:
                raise StateCorruptionError("Cooldown history is not chronological")
            previous_time = prior_time
        if previous_time is not None and rejected <= previous_time:
            raise StateCorruptionError("Current cooldown does not follow its history")
    valid_statuses = {item.value for item in ShadowPlanStatus}
    for plan in state["shadow_plans"]:
        if not isinstance(plan.get("original_plan"), dict) or not isinstance(plan.get("outcome"), dict):
            raise StateCorruptionError("Invalid frozen plan structure")
        if plan["outcome"].get("status") not in valid_statuses:
            raise StateCorruptionError("Invalid plan outcome status")
        _aware(plan.get("frozen_at"), "plan frozen_at")
        status = plan["outcome"]["status"]
        if status == "OPEN" and not plan["outcome"].get("entry_timestamp"):
            raise StateCorruptionError("OPEN plan lacks entry timestamp")
        if status in {"TARGET1", "STOPPED", "FLAT_TIME"} and (plan["outcome"].get("entry_timestamp") is None or plan["outcome"].get("entry_price") is None or plan["outcome"].get("exit_timestamp") is None or plan["outcome"].get("pnl") is None):
            raise StateCorruptionError("Completed plan lacks exit provenance")
        entry_timestamp = plan["outcome"].get("entry_timestamp")
        exit_timestamp = plan["outcome"].get("exit_timestamp")
        entry_time = _aware(entry_timestamp, "outcome entry timestamp") if entry_timestamp else None
        exit_time = _aware(exit_timestamp, "outcome exit timestamp") if exit_timestamp else None
        if entry_time and exit_time and exit_time < entry_time:
            raise StateCorruptionError("Invalid plan-outcome chronology")
        trailing = plan.get("trailing_outcome")
        if trailing is not None:
            if not isinstance(trailing, dict) or trailing.get("status") not in valid_statuses:
                raise StateCorruptionError("Invalid trailing outcome status")
            trailing_entry = trailing.get("entry_timestamp")
            trailing_exit = trailing.get("exit_timestamp")
            trailing_entry_time = _aware(trailing_entry, "trailing entry timestamp") if trailing_entry else None
            trailing_exit_time = _aware(trailing_exit, "trailing exit timestamp") if trailing_exit else None
            if trailing_entry_time and trailing_exit_time and trailing_exit_time < trailing_entry_time:
                raise StateCorruptionError("Invalid trailing-outcome chronology")
        original = plan["original_plan"]
        for key in ("symbol", "decision_timestamp", "entry_trigger", "maximum_chase_price", "stop_price", "target1", "hypothetical_quantity", "hypothetical_notional", "planned_dollar_risk", "latest_entry_time", "mandatory_flat_time"):
            if key not in original:
                raise StateCorruptionError(f"Frozen plan lacks {key}")
        _aware(original["decision_timestamp"], "plan decision_timestamp")
        _aware(original["latest_entry_time"], "plan latest_entry_time")
        _aware(original["mandatory_flat_time"], "plan mandatory_flat_time")
    for collection in (state["errors"], state["security_events"], state["schedule_events"]):
        for item in collection:
            if not isinstance(item, dict):
                raise StateCorruptionError("Invalid event structure")
            for key in ("timestamp", "observed_at", "scheduled_for", "resolved_at"):
                if key in item:
                    _aware(item[key], f"event {key}")
    for event in state["schedule_events"]:
        if event.get("status") not in {"SKIPPED_STALE", "SKIPPED_CUTOFF", "EOD_STARTED"}:
            raise StateCorruptionError("Invalid schedule-event status")
    _finite_tree(state)


def _unique_ids(items: list[Any], key: str, label: str) -> None:
    values = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key]:
            raise StateCorruptionError(f"Invalid {label} ID")
        values.append(item[key])
    if len(values) != len(set(values)):
        raise StateCorruptionError(f"Duplicate {label} ID")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError("State/report contains non-JSON or non-finite data") from exc
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path.exists(): temp_path.unlink()


class StateStore:
    def __init__(self, root: Path, timezone_name: str = "America/New_York") -> None:
        self.root = root; self.timezone_name = timezone_name

    def path_for(self, session_date: str | date) -> Path:
        value = session_date.isoformat() if isinstance(session_date, date) else session_date
        return self.root / f"{value}.json"

    def load(self, session_date: str | date, create: bool = True) -> dict[str, Any]:
        value = session_date.isoformat() if isinstance(session_date, date) else session_date
        path = self.path_for(value)
        if not path.exists():
            if not create: raise StateCorruptionError(f"State file does not exist: {path}")
            return initial_state(value, self.timezone_name)
        try: state = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (OSError, json.JSONDecodeError, ValueError) as exc: raise StateCorruptionError(f"Cannot parse state file {path}") from exc
        validate_state_shape(state, value)
        return state

    def save(self, state: dict[str, Any]) -> Path:
        candidate = deepcopy(state); validate_state_shape(candidate)
        path = self.path_for(candidate["session_date"])
        disk_revision = None
        disk_state = None
        if path.exists():
            disk_state = self.load(candidate["session_date"], create=False)
            disk_revision = disk_state["revision"]
        expected = candidate["revision"]
        if (disk_revision is None and expected != 0) or (disk_revision is not None and disk_revision != expected):
            raise StateCorruptionError("Lost-update protection rejected stale state revision")
        if disk_state is not None:
            _validate_state_transition(disk_state, candidate)
        candidate["revision"] = expected + 1
        candidate["updated_at"] = datetime.now(ZoneInfo(self.timezone_name)).isoformat()
        atomic_write_json(path, candidate)
        state["revision"] = candidate["revision"]; state["updated_at"] = candidate["updated_at"]
        return path

    def all_states(self) -> list[dict[str, Any]]:
        if not self.root.exists(): return []
        return [self.load(path.stem, create=False) for path in sorted(self.root.glob("????-??-??.json"))]


def _validate_state_transition(old: dict[str, Any], new: dict[str, Any]) -> None:
    allowed = {
        "PENDING": {"PENDING", "OPEN", "TARGET1", "STOPPED", "FLAT_TIME", "EXPIRED", "AMBIGUOUS"},
        "OPEN": {"OPEN", "TARGET1", "STOPPED", "FLAT_TIME", "AMBIGUOUS"},
        "TARGET1": {"TARGET1"}, "STOPPED": {"STOPPED"}, "FLAT_TIME": {"FLAT_TIME"},
        "EXPIRED": {"EXPIRED"}, "AMBIGUOUS": {"AMBIGUOUS"},
    }
    old_plans = {item["plan_id"]: item for item in old["shadow_plans"]}
    new_plans = {item["plan_id"]: item for item in new["shadow_plans"]}
    if not set(old_plans) <= set(new_plans):
        raise StateCorruptionError("Persisted frozen plan cannot be removed")
    for plan_id, prior in old_plans.items():
        current = new_plans[plan_id]
        if current["original_plan"] != prior["original_plan"] or current["frozen_at"] != prior["frozen_at"]:
            raise StateCorruptionError("Persisted frozen plan cannot be modified")
        before, after = prior["outcome"]["status"], current["outcome"]["status"]
        if after not in allowed[before]:
            raise StateCorruptionError(f"Impossible plan transition {before} -> {after}")
        prior_trailing, current_trailing = prior.get("trailing_outcome"), current.get("trailing_outcome")
        if prior_trailing is not None:
            if current_trailing is None or current_trailing.get("status") not in allowed[prior_trailing.get("status")]:
                raise StateCorruptionError("Impossible trailing outcome transition")
    if not set(old["operation_ids"]) <= set(new["operation_ids"]):
        raise StateCorruptionError("Operation IDs are append-only")
    old_preflights = {item["operation_id"]: item for item in old.get("preflight_operations", [])}
    new_preflights = {item["operation_id"]: item for item in new.get("preflight_operations", [])}
    if not set(old_preflights) <= set(new_preflights):
        raise StateCorruptionError("Preflight operation evidence cannot be removed")
    for operation_id, prior in old_preflights.items():
        current = new_preflights[operation_id]
        for key in ("operation_id", "started_at", "report_artifact"):
            if current.get(key) != prior.get(key):
                raise StateCorruptionError("Preflight operation identity is immutable")
        if prior.get("status") in {"COMPLETED", "FAILED"} and current != prior:
            raise StateCorruptionError("Terminal preflight operation evidence is immutable")
    old_research = {(item["plan_id"], item["variant"]): item for item in old.get("research_outcomes", [])}
    new_research = {(item["plan_id"], item["variant"]): item for item in new.get("research_outcomes", [])}
    if not set(old_research) <= set(new_research):
        raise StateCorruptionError("Research outcome evidence cannot be removed")
    if any(new_research[key] != value for key, value in old_research.items()):
        raise StateCorruptionError("Completed research outcome evidence is immutable")
    for symbol, prior in old["cooldowns"].items():
        if symbol not in new["cooldowns"]:
            raise StateCorruptionError("Cooldown history cannot be removed")
        current = new["cooldowns"][symbol]
        if current.get("rejected_at") != prior.get("rejected_at"):
            prior_snapshot = {key: prior.get(key) for key in ("rejected_at", "cooldown_until", "original_rejection_reason", "rejection_categories", "source_decision_number")}
            if aware_datetime(current.get("rejected_at")) <= aware_datetime(prior.get("rejected_at")) or prior_snapshot not in current.get("rejection_history", []):
                raise StateCorruptionError("Cooldown rejection provenance cannot be rewritten")
        else:
            for key in ("cooldown_until", "original_rejection_reason", "rejection_categories"):
                if current.get(key) != prior.get(key):
                    raise StateCorruptionError("Cooldown rejection provenance cannot be rewritten")


def aware_datetime(value: Any) -> datetime:
    return _aware(value, "state transition timestamp")
