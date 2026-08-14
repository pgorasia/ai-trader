from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .models import SchemaValidationError, ShadowPlanStatus


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SchemaValidationError(f"Timestamp is not timezone-aware: {value}")
    return parsed


def _ceil_five_minutes(value: datetime) -> datetime:
    floored = value.replace(second=0, microsecond=0, minute=(value.minute // 5) * 5)
    return floored if value == floored else floored + timedelta(minutes=5)


def validate_bar(bar: dict[str, Any]) -> None:
    _dt(bar["timestamp"])
    low, high = float(bar["low"]), float(bar["high"])
    open_price, close = float(bar["open"]), float(bar["close"])
    if low > high or not (low <= open_price <= high) or not (low <= close <= high):
        raise SchemaValidationError(f"Malformed OHLC bar at {bar['timestamp']}")


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
        trigger = float(plan["entry_trigger"])
        chase = float(plan["maximum_chase_price"])
        stop = float(plan["stop_price"])
        target = float(plan["target1"])
        target2 = plan.get("target2_optional")
        ordered: list[dict[str, Any]] = []
        for bar in bars:
            validate_bar(bar)
            if bar.get("complete"):
                ordered.append(bar)
        ordered.sort(key=lambda item: _dt(item["timestamp"]))
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
                    "mfe": 0.0,
                    "mae": 0.0,
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
            outcome["mfe"] = max(float(outcome.get("mfe", 0)), float(bar["high"]) - entry_price)
            if same_entry_bar and not outcome.get("entry_via_open", False):
                outcome["mae_ambiguous"] = True
            else:
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
                self._close(outcome, ShadowPlanStatus.STOPPED, timestamp, stop, plan)
                break
            if target_hit:
                self._close(outcome, ShadowPlanStatus.TARGET1, timestamp, target, plan)
                break

        current = as_of or (max((_dt(bar["timestamp"]) + timedelta(minutes=5) for bar in ordered), default=decision_time))
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
            "pnl": None,
            "realized_r": None,
            "ambiguous": False,
            "ambiguity_reason": None,
        }
