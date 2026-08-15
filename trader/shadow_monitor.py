from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
from typing import Any
from zoneinfo import ZoneInfo

from .models import SchemaValidationError, ShadowPlanStatus


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SchemaValidationError(f"Timestamp is not timezone-aware: {value}")
    return parsed


def _ceil_five_minutes(value: datetime) -> datetime:
    floored = value.replace(second=0, microsecond=0, minute=(value.minute // 5) * 5)
    return floored if value == floored else floored + timedelta(minutes=5)


ET = ZoneInfo("America/New_York")


def validate_bar(bar: dict[str, Any]) -> None:
    required = {"timestamp", "open", "high", "low", "close", "volume", "complete"}
    if not isinstance(bar, dict) or not required <= set(bar):
        raise SchemaValidationError("Malformed OHLCV bar structure")
    timestamp = _dt(bar["timestamp"])
    if timestamp.second or timestamp.microsecond or timestamp.astimezone(ET).minute % 5:
        raise SchemaValidationError(f"Bar is not five-minute aligned: {bar['timestamp']}")
    low, high = float(bar["low"]), float(bar["high"])
    open_price, close, volume = float(bar["open"]), float(bar["close"]), float(bar["volume"])
    if not all(math.isfinite(item) for item in (low, high, open_price, close, volume)) or min(low, high, open_price, close) <= 0 or volume < 0:
        raise SchemaValidationError(f"Non-finite or non-positive OHLCV bar at {bar['timestamp']}")
    if low > high or not (low <= open_price <= high) or not (low <= close <= high):
        raise SchemaValidationError(f"Malformed OHLC bar at {bar['timestamp']}")


def validate_bar_series(bars: list[dict[str, Any]], *, session_date: str, as_of: datetime, mandatory_flat: datetime) -> list[dict[str, Any]]:
    if as_of.tzinfo is None:
        raise SchemaValidationError("Trusted as_of must be timezone-aware")
    session_day = datetime.fromisoformat(session_date).date()
    market_open = datetime.combine(session_day, datetime.min.time(), ET).replace(hour=9, minute=30)
    market_close = datetime.combine(session_day, datetime.min.time(), ET).replace(hour=16)
    prior: datetime | None = None
    seen: set[datetime] = set()
    validated = []
    for bar in bars:
        validate_bar(bar)
        if bar.get("complete") is not True:
            raise SchemaValidationError("Incomplete bar cannot be used by ShadowPlanMonitor")
        timestamp = _dt(bar["timestamp"]).astimezone(ET)
        if timestamp.date() != session_day:
            raise SchemaValidationError("Bar belongs to the wrong market session date")
        if timestamp < market_open or timestamp >= market_close:
            raise SchemaValidationError("Bar is outside regular-session bounds")
        if timestamp > mandatory_flat.astimezone(ET):
            raise SchemaValidationError("Bar is after the mandatory-flat boundary")
        if timestamp + timedelta(minutes=5) > as_of.astimezone(ET):
            raise SchemaValidationError("Bar was not knowable at trusted as_of time")
        if timestamp in seen:
            raise SchemaValidationError("Duplicate bar timestamp")
        if prior is not None and timestamp <= prior:
            raise SchemaValidationError("Bars must be in strict chronological order")
        seen.add(timestamp); prior = timestamp; validated.append(bar)
    return validated


def aggregate_completed_15m(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[datetime, list[dict[str, Any]]] = {}
    for bar in bars:
        validate_bar(bar)
        if not bar.get("complete", False):
            continue
        timestamp = _dt(bar["timestamp"])
        anchor = timestamp.replace(minute=(timestamp.minute // 15) * 15, second=0, microsecond=0)
        groups.setdefault(anchor, []).append(bar)
    aggregated: list[dict[str, Any]] = []
    for anchor, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: _dt(item["timestamp"]))
        expected = [anchor + timedelta(minutes=offset) for offset in (0, 5, 10)]
        if len(ordered) != 3 or [_dt(item["timestamp"]) for item in ordered] != expected:
            continue
        aggregated.append({
            "timestamp": anchor.isoformat(),
            "open": float(ordered[0]["open"]),
            "high": max(float(item["high"]) for item in ordered),
            "low": min(float(item["low"]) for item in ordered),
            "close": float(ordered[-1]["close"]),
            "volume": sum(float(item["volume"]) for item in ordered),
            "complete": True,
        })
    return aggregated


class ShadowPlanMonitor:
    """Resolve immutable long-plan outcomes from completed 5-minute OHLCV."""

    def evaluate(self, plan_record: dict[str, Any], bars: list[dict[str, Any]], as_of: datetime | None = None) -> dict[str, Any]:
        record = deepcopy(plan_record)
        plan = record["original_plan"]
        outcome = deepcopy(record.get("outcome") or self.initial_outcome())
        if outcome["status"] not in {ShadowPlanStatus.PENDING, ShadowPlanStatus.OPEN}:
            return record

        decision_time = _dt(plan["decision_timestamp"])
        first_eligible = _ceil_five_minutes(decision_time)
        latest_entry = _dt(plan["latest_entry_time"])
        mandatory_flat = _dt(plan["mandatory_flat_time"])
        if as_of is None or as_of.tzinfo is None:
            raise SchemaValidationError("ShadowPlanMonitor requires a trusted timezone-aware as_of")
        trigger = float(plan["entry_trigger"])
        chase = float(plan["maximum_chase_price"])
        stop = float(plan["stop_price"])
        target = float(plan["target1"])
        target2 = plan.get("target2_optional")
        ordered = validate_bar_series(bars, session_date=decision_time.astimezone(ET).date().isoformat(), as_of=as_of, mandatory_flat=mandatory_flat)
        ordered = [bar for bar in ordered if _dt(bar["timestamp"]) >= first_eligible]
        entry_bar_time = _dt(outcome["entry_bar_timestamp"]) if outcome.get("entry_bar_timestamp") else None
        if outcome["status"] == ShadowPlanStatus.OPEN and entry_bar_time is not None:
            ordered = [bar for bar in ordered if _dt(bar["timestamp"]) >= entry_bar_time]

        for index, bar in enumerate(ordered):
            timestamp = _dt(bar["timestamp"])
            if outcome["status"] == ShadowPlanStatus.PENDING:
                if timestamp >= latest_entry:
                    break
                entry = self._entry_price(plan, bar, trigger, chase)
                if entry is None:
                    continue
                outcome.update({
                    "status": ShadowPlanStatus.OPEN,
                    "entry_triggered": True,
                    "entry_timestamp": timestamp.isoformat(),
                    "entry_bar_timestamp": timestamp.isoformat(),
                    "entry_price": entry[0],
                    "entry_via_open": entry[1],
                    "entry_at_close": entry[2],
                    "entry_before_cutoff": True,
                    "mfe": 0.0 if entry[1] or entry[2] else None,
                    "mae": 0.0 if entry[1] or entry[2] else None,
                    "excursions_unknown": not (entry[1] or entry[2]),
                })
                entry_bar_time = timestamp

            if outcome["status"] != ShadowPlanStatus.OPEN:
                break
            if timestamp >= mandatory_flat:
                self._close(outcome, ShadowPlanStatus.FLAT_TIME, mandatory_flat, float(bar["open"]), plan)
                outcome["flat_price_basis"] = "MANDATORY_FLAT_5M_BAR_OPEN"
                break
            entry_price = float(outcome["entry_price"])
            same_entry_bar = entry_bar_time == timestamp
            if same_entry_bar and outcome.get("entry_at_close", False):
                continue
            if same_entry_bar and not outcome.get("entry_via_open", False):
                outcome["mae_ambiguous"] = True
                outcome["mfe"] = None
                outcome["mae"] = None
                outcome["excursions_unknown"] = True
            elif not outcome.get("excursions_unknown", False):
                outcome["mfe"] = max(float(outcome.get("mfe", 0)), float(bar["high"]) - entry_price)
                outcome["mae"] = min(float(outcome.get("mae", 0)), float(bar["low"]) - entry_price)
            if target2 is not None and float(bar["high"]) >= float(target2):
                outcome["target2_hit"] = True

            stop_hit = float(bar["low"]) <= stop
            target_hit = float(bar["high"]) >= target
            crossed_from_below = same_entry_bar and not outcome.get("entry_via_open", False)
            if stop_hit and target_hit:
                self._ambiguous(outcome, timestamp, "STOP_AND_TARGET_SAME_5M_BAR")
                break
            if stop_hit and crossed_from_below:
                self._ambiguous(outcome, timestamp, "ENTRY_AND_STOP_ORDER_UNKNOWN_IN_5M_BAR")
                break
            if stop_hit:
                self._close(outcome, ShadowPlanStatus.STOPPED, timestamp, min(stop, float(bar["open"])), plan)
                outcome["stop_price_basis"] = "WORSE_OF_STOP_OR_COMPLETED_BAR_OPEN"
                break
            if target_hit:
                self._close(outcome, ShadowPlanStatus.TARGET1, timestamp, target, plan)
                break

        current = as_of
        if outcome["status"] == ShadowPlanStatus.PENDING and current >= latest_entry:
            outcome.update({"status": ShadowPlanStatus.EXPIRED, "entry_before_cutoff": False, "exit_reason": "ENTRY_NOT_TRIGGERED_BEFORE_CUTOFF"})
        elif outcome["status"] == ShadowPlanStatus.OPEN and current >= mandatory_flat:
            eligible = [bar for bar in ordered if _dt(bar["timestamp"]) + timedelta(minutes=5) <= mandatory_flat]
            if eligible:
                proxy = eligible[-1]
                self._close(outcome, ShadowPlanStatus.FLAT_TIME, mandatory_flat, float(proxy["close"]), plan)
                outcome["flat_price_basis"] = "LAST_COMPLETED_5M_CLOSE_AT_OR_BEFORE_FLAT"

        record["outcome"] = outcome
        return record

    def evaluate_trailing(self, plan_record: dict[str, Any], bars: list[dict[str, Any]], as_of: datetime | None = None) -> dict[str, Any]:
        """Evaluate a no-lookahead two-bar structure trail from the same entry."""
        record = deepcopy(plan_record)
        plan = record["original_plan"]
        outcome = deepcopy(record.get("trailing_outcome") or self.initial_trailing_outcome(plan.get("stop_price")))
        if outcome["status"] not in {ShadowPlanStatus.PENDING, ShadowPlanStatus.OPEN}:
            return record
        if as_of is None or as_of.tzinfo is None:
            raise SchemaValidationError("Trailing monitor requires a trusted timezone-aware as_of")

        decision_time = _dt(plan["decision_timestamp"])
        first_eligible = _ceil_five_minutes(decision_time)
        latest_entry = _dt(plan["latest_entry_time"])
        mandatory_flat = _dt(plan["mandatory_flat_time"])
        trigger, chase = float(plan["entry_trigger"]), float(plan["maximum_chase_price"])
        initial_stop = float(plan["stop_price"])
        quantity = float(plan["hypothetical_quantity"])
        activation_r = float(plan.get("trailing_activation_r", 1.0))
        lookback = int(plan.get("trailing_lookback_bars", 2))
        if activation_r <= 0 or lookback < 2:
            raise SchemaValidationError("Trailing configuration is outside safe bounds")
        ordered = validate_bar_series(bars, session_date=decision_time.astimezone(ET).date().isoformat(), as_of=as_of, mandatory_flat=mandatory_flat)
        ordered = [bar for bar in ordered if _dt(bar["timestamp"]) >= first_eligible]
        processed = _dt(outcome["last_processed_bar_timestamp"]) if outcome.get("last_processed_bar_timestamp") else None
        if processed is not None:
            ordered = [bar for bar in ordered if _dt(bar["timestamp"]) > processed]

        for bar in ordered:
            timestamp = _dt(bar["timestamp"])
            if outcome["status"] == ShadowPlanStatus.PENDING:
                if timestamp >= latest_entry:
                    break
                entry = self._entry_price(plan, bar, trigger, chase)
                if entry is None:
                    outcome["last_processed_bar_timestamp"] = timestamp.isoformat()
                    continue
                outcome.update({
                    "status": ShadowPlanStatus.OPEN, "entry_triggered": True,
                    "entry_timestamp": timestamp.isoformat(), "entry_bar_timestamp": timestamp.isoformat(),
                    "entry_price": entry[0], "entry_via_open": entry[1], "entry_at_close": entry[2],
                    "entry_before_cutoff": True, "trailing_stop": initial_stop,
                    "mfe": 0.0 if entry[1] or entry[2] else None,
                    "mae": 0.0 if entry[1] or entry[2] else None,
                    "excursions_unknown": not (entry[1] or entry[2]),
                })

            if outcome["status"] != ShadowPlanStatus.OPEN:
                break
            if timestamp >= mandatory_flat:
                self._close(outcome, ShadowPlanStatus.FLAT_TIME, mandatory_flat, float(bar["open"]), plan)
                outcome["flat_price_basis"] = "MANDATORY_FLAT_5M_BAR_OPEN"
                outcome["last_processed_bar_timestamp"] = timestamp.isoformat()
                break

            entry_price = float(outcome["entry_price"])
            same_entry_bar = outcome.get("entry_bar_timestamp") == timestamp.isoformat()
            if same_entry_bar and outcome.get("entry_at_close"):
                outcome["last_processed_bar_timestamp"] = timestamp.isoformat()
                outcome["recent_completed_lows"] = (outcome.get("recent_completed_lows", []) + [float(bar["low"])])[-lookback:]
                continue
            if same_entry_bar and not outcome.get("entry_via_open"):
                outcome.update({"mae_ambiguous": True, "mfe": None, "mae": None, "excursions_unknown": True})
            elif not outcome.get("excursions_unknown", False):
                outcome["mfe"] = max(float(outcome.get("mfe", 0)), float(bar["high"]) - entry_price)
                outcome["mae"] = min(float(outcome.get("mae", 0)), float(bar["low"]) - entry_price)

            active_stop = float(outcome.get("trailing_stop") or initial_stop)
            stop_hit = float(bar["low"]) <= active_stop
            crossed_from_below = same_entry_bar and not outcome.get("entry_via_open", False)
            if stop_hit and crossed_from_below:
                self._ambiguous(outcome, timestamp, "ENTRY_AND_TRAILING_STOP_ORDER_UNKNOWN_IN_5M_BAR")
                break
            if stop_hit:
                self._close(outcome, ShadowPlanStatus.STOPPED, timestamp, min(active_stop, float(bar["open"])), plan)
                outcome["exit_reason"] = "TRAILING_STOP" if outcome.get("trailing_active") else "INITIAL_STOP"
                outcome["stop_hit"] = True
                outcome["last_processed_bar_timestamp"] = timestamp.isoformat()
                break

            lows = (outcome.get("recent_completed_lows", []) + [float(bar["low"])])[-lookback:]
            outcome["recent_completed_lows"] = lows
            activation_price = entry_price + activation_r * (float(plan["planned_dollar_risk"]) / quantity)
            if float(bar["close"]) >= activation_price:
                outcome["trailing_active"] = True
            if outcome.get("trailing_active") and len(lows) == lookback:
                candidate = min(lows)
                new_stop = max(active_stop, candidate)
                if new_stop > active_stop:
                    outcome["trailing_stop"] = new_stop
                    outcome["trailing_updates"] = int(outcome.get("trailing_updates", 0)) + 1
            outcome["last_processed_bar_timestamp"] = timestamp.isoformat()

        if outcome["status"] == ShadowPlanStatus.PENDING and as_of >= latest_entry:
            outcome.update({"status": ShadowPlanStatus.EXPIRED, "entry_before_cutoff": False, "exit_reason": "ENTRY_NOT_TRIGGERED_BEFORE_CUTOFF"})
        elif outcome["status"] == ShadowPlanStatus.OPEN and as_of >= mandatory_flat:
            eligible = [bar for bar in validate_bar_series(bars, session_date=decision_time.astimezone(ET).date().isoformat(), as_of=as_of, mandatory_flat=mandatory_flat) if _dt(bar["timestamp"]) + timedelta(minutes=5) <= mandatory_flat]
            if eligible:
                self._close(outcome, ShadowPlanStatus.FLAT_TIME, mandatory_flat, float(eligible[-1]["close"]), plan)
                outcome["flat_price_basis"] = "LAST_COMPLETED_5M_CLOSE_AT_OR_BEFORE_FLAT"
        record["trailing_outcome"] = outcome
        return record

    @staticmethod
    def _entry_price(plan: dict[str, Any], bar: dict[str, Any], trigger: float, chase: float) -> tuple[float, bool, bool] | None:
        if plan["entry_trigger_type"] == "COMPLETED_5M_CLOSE_AT_OR_ABOVE":
            close = float(bar["close"])
            return (close, False, True) if trigger <= close <= chase else None
        open_price = float(bar["open"])
        if trigger <= open_price <= chase:
            return open_price, True, False
        if open_price < trigger and float(bar["high"]) >= trigger:
            return trigger, False, False
        return None

    @staticmethod
    def _close(outcome: dict[str, Any], status: ShadowPlanStatus, timestamp: datetime, price: float, plan: dict[str, Any]) -> None:
        entry = float(outcome["entry_price"])
        quantity = float(plan["hypothetical_quantity"])
        planned_risk = float(plan["planned_dollar_risk"])
        pnl = (price - entry) * quantity
        outcome.update({
            "status": status,
            "exit_timestamp": timestamp.isoformat(),
            "exit_price": price,
            "exit_reason": status.value,
            "pnl": pnl,
            "realized_r": pnl / planned_risk if planned_risk else None,
        })
        if status is ShadowPlanStatus.STOPPED:
            outcome["stop_hit"] = True
        elif status is ShadowPlanStatus.TARGET1:
            outcome["target1_hit"] = True

    @staticmethod
    def _ambiguous(outcome: dict[str, Any], timestamp: datetime, reason: str) -> None:
        outcome.update({"status": ShadowPlanStatus.AMBIGUOUS, "ambiguous": True, "ambiguity_reason": reason, "exit_timestamp": timestamp.isoformat(), "exit_reason": "AMBIGUOUS_5M_ORDERING", "pnl": None, "realized_r": None})

    @staticmethod
    def initial_outcome() -> dict[str, Any]:
        return {
            "status": ShadowPlanStatus.PENDING,
            "entry_triggered": False,
            "entry_timestamp": None,
            "entry_bar_timestamp": None,
            "entry_price": None,
            "entry_via_open": False,
            "entry_at_close": False,
            "entry_before_cutoff": None,
            "stop_hit": False,
            "target1_hit": False,
            "target2_hit": False,
            "exit_timestamp": None,
            "exit_price": None,
            "exit_reason": None,
            "mfe": None,
            "mae": None,
            "mae_ambiguous": False,
            "excursions_unknown": False,
            "pnl": None,
            "realized_r": None,
            "ambiguous": False,
            "ambiguity_reason": None,
        }

    @staticmethod
    def initial_trailing_outcome(initial_stop: float | None = None) -> dict[str, Any]:
        outcome = ShadowPlanMonitor.initial_outcome()
        outcome.update({
            "variant": "TRAILING_STOP",
            "trailing_active": False,
            "trailing_stop": float(initial_stop) if initial_stop is not None else None,
            "trailing_updates": 0,
            "recent_completed_lows": [],
            "last_processed_bar_timestamp": None,
        })
        return outcome
