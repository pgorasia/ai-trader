from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from .market_calendar import EquityMarketCalendar
from .experiment import calculate_experiment
from .models import ReadinessStatus, StateCorruptionError
from .state import validate_state_shape


def _finite(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise StateCorruptionError(f"Readiness {label} must be finite")
    return number


def _aware(value: Any, label: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StateCorruptionError(f"Readiness {label} must be timezone-aware")
    return parsed


def _group(trades: list[dict[str, Any]], key: str) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for trade in trades: groups[str(trade.get(key) or "UNKNOWN")].append(trade["net_pnl_after_cost"])
    return {label: {"trades": len(values), "net_pnl": sum(values), "average_pnl": sum(values) / len(values)} for label, values in sorted(groups.items())}


def calculate_readiness(states: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    rules = config["readiness"]
    cost_bps = _finite(rules["estimated_round_trip_cost_bps"], "cost bps")
    stress_cost_bps = _finite(rules["stress_round_trip_cost_bps"], "stress cost bps")
    if cost_bps < 0 or stress_cost_bps < cost_bps:
        raise StateCorruptionError("Readiness costs cannot be negative")
    calendar = EquityMarketCalendar(config["exchange_calendar"], int(config["schedule"]["eod_offset_minutes"]))
    completed: list[dict[str, Any]] = []
    seen_trades: set[str] = set()
    security_violations = unresolved_failures = luna_cycles = sol_decisions = no_trade = plans = 0
    benchmark_points: dict[str, list[tuple[str, float]]] = defaultdict(list)
    cohort_versions: set[str] = set()
    cohort_complete = True
    benchmark_complete = True
    valid_sessions = 0
    for state in states:
        validate_state_shape(state)
        if not state["eod_completed"] or f"eod:{state['session_date']}" not in state["operation_ids"] or not isinstance(state.get("eod_review"), dict) or state["eod_review"].get("session_date") != state["session_date"]:
            continue
        if calendar.session_for(datetime.fromisoformat(state["session_date"]).date()) is None:
            raise StateCorruptionError("Readiness state is not an XNYS session")
        valid_sessions += 1
        version = state.get("strategy_version")
        if isinstance(version, str) and version:
            cohort_versions.add(version)
        else:
            cohort_complete = False
        luna_cycles += len(state["cycles"]); sol_decisions += len(state["senior_decisions"])
        no_trade += sum(1 for item in state["senior_decisions"] if item.get("decision") == "NO_TRADE")
        plans += len(state["shadow_plans"])
        security_violations += sum(1 for event in state["security_events"] if event.get("category") == "SECURITY_VIOLATION")
        unresolved_failures += sum(1 for error in state["errors"] if not error.get("resolved", False))
        benchmark_closes = state["eod_review"].get("benchmark_closes", {})
        for symbol in ("SPY", "QQQ"):
            if benchmark_closes.get(symbol) is None:
                benchmark_complete = False
                continue
            close = _finite(benchmark_closes[symbol], f"{symbol} close")
            if close <= 0: raise StateCorruptionError("Benchmark close must be positive")
            benchmark_points[symbol].append((state["session_date"], close))
        plans_by_id = {item["plan_id"]: item for item in state["shadow_plans"]}
        for trade in state["completed_shadow_trades"]:
            plan_id = trade["plan_id"]
            if plan_id in seen_trades: raise StateCorruptionError("Duplicate trade included in readiness")
            seen_trades.add(plan_id)
            plan_record = plans_by_id.get(plan_id)
            if plan_record is None or plan_record["outcome"].get("status") not in {"TARGET1", "STOPPED", "FLAT_TIME"}:
                raise StateCorruptionError("Completed trade lacks frozen-plan provenance")
            outcome, original = plan_record["outcome"], plan_record["original_plan"]
            if trade.get("entry_timestamp") != outcome.get("entry_timestamp") or trade.get("exit_timestamp") != outcome.get("exit_timestamp") or _finite(trade["pnl"], "P&L") != _finite(outcome.get("pnl"), "outcome P&L"):
                raise StateCorruptionError("Completed trade conflicts with frozen-plan outcome")
            if abs(_finite(trade["hypothetical_notional"], "notional") - _finite(original.get("hypothetical_notional"), "plan notional")) > 0.01:
                raise StateCorruptionError("Completed trade conflicts with frozen-plan notional")
            entry, exit_time = _aware(trade["entry_timestamp"], "entry"), _aware(trade["exit_timestamp"], "exit")
            if exit_time < entry: raise StateCorruptionError("Trade chronology is invalid")
            notional, pnl = _finite(trade["hypothetical_notional"], "notional"), _finite(trade["pnl"], "P&L")
            if notional <= 0: raise StateCorruptionError("Trade notional must be positive")
            for field in ("mfe", "mae"):
                if trade.get(field) is not None: _finite(trade[field], field)
            item = dict(trade); item["estimated_cost"] = max(0.0, notional * cost_bps / 10_000)
            item["net_pnl_after_cost"] = pnl - item["estimated_cost"]
            item["stress_estimated_cost"] = max(0.0, notional * stress_cost_bps / 10_000)
            item["stress_net_pnl_after_cost"] = pnl - item["stress_estimated_cost"]
            completed.append(item)

    values = [item["net_pnl_after_cost"] for item in completed]
    winners, losers = [v for v in values if v > 0], [v for v in values if v < 0]
    gross_profit, gross_loss = sum(winners), abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss else (None if gross_profit else 0.0)
    profit_factor_pass = gross_profit > 0 if gross_loss == 0 else profit_factor > float(rules["minimum_profit_factor"])
    expectancy = sum(values) / len(values) if values else 0.0
    stress_values = [item["stress_net_pnl_after_cost"] for item in completed]
    stress_expectancy = sum(stress_values) / len(stress_values) if stress_values else 0.0
    expectancy_lower_95 = None if len(values) < 2 else expectancy - 1.645 * statistics.stdev(values) / math.sqrt(len(values))
    equity = peak = drawdown = 0.0
    for value in values: equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    concentration = max(winners) / gross_profit if winners and gross_profit > 0 else 0.0
    benchmark_performance = {}
    for symbol, points in benchmark_points.items():
        ordered = sorted(points); start, end = ordered[0][1], ordered[-1][1]
        benchmark_performance[symbol] = {"observations": len(ordered), "start_close": start, "end_close": end, "total_return": (end - start) / start}
    reference_capital = _finite(config["risk"]["reference_capital"], "reference capital")
    strategy_total_return = sum(values) / reference_capital
    requirements = {
        "security_boundary": security_violations <= int(rules["maximum_security_violations"]),
        "state_reconciliation": unresolved_failures <= int(rules["maximum_unresolved_state_failures"]),
        "single_strategy_cohort": valid_sessions > 0 and cohort_complete and cohort_versions == {config["strategy_version"]},
        "benchmark_completeness": valid_sessions > 0 and benchmark_complete and all(len(benchmark_points[symbol]) == valid_sessions for symbol in ("SPY", "QQQ")),
        "market_sessions": valid_sessions >= int(rules["minimum_market_sessions"]),
        "completed_shadow_trades": len(completed) >= int(rules["minimum_completed_shadow_trades"]),
        "positive_expectancy_after_cost": expectancy > 0,
        "positive_stress_expectancy": stress_expectancy > 0,
        "positive_expectancy_lower_95": expectancy_lower_95 is not None and expectancy_lower_95 > 0,
        "profit_factor": profit_factor_pass,
        "profit_concentration": concentration <= float(rules["maximum_single_winner_gross_profit_fraction"]),
        "maximum_drawdown": drawdown <= float(rules["maximum_drawdown_dollars"]),
    }
    known_mfe = [_finite(item["mfe"], "mfe") for item in completed if item.get("mfe") is not None]
    known_mae = [_finite(item["mae"], "mae") for item in completed if item.get("mae") is not None]
    experiment = calculate_experiment(states, config)
    if experiment["selected_exit_variant"] is not None:
        validation_requirements = experiment["validation_requirements"]
        requirements.update({
            "positive_expectancy_after_cost": validation_requirements["positive_stress_expectancy"],
            "positive_stress_expectancy": validation_requirements["positive_stress_expectancy"],
            "positive_expectancy_lower_95": validation_requirements["positive_expectancy_lower_95"],
            "profit_factor": validation_requirements["profit_factor"],
            "profit_concentration": validation_requirements["profit_concentration"],
            "maximum_drawdown": validation_requirements["maximum_drawdown"],
        })
    requirements["accelerated_validation"] = experiment["recommendation"] == "READY_FOR_APPROVAL_REVIEW"
    status = ReadinessStatus.READY_FOR_APPROVAL_REVIEW if all(requirements.values()) else ReadinessStatus.CONTINUE_SHADOW
    return {"status": status, "requirements": requirements, "metrics": {
        "market_sessions": valid_sessions, "luna_cycles": luna_cycles, "sol_decisions": sol_decisions, "no_trade_count": no_trade, "shadow_trade_plans": plans,
        "completed_shadow_trades": len(completed), "wins": len(winners), "losses": len(losers), "win_rate": len(winners) / len(completed) if completed else 0.0,
        "average_r": sum(_finite(item.get("realized_r", 0), "realized R") for item in completed) / len(completed) if completed else 0.0,
        "average_winner": gross_profit / len(winners) if winners else 0.0, "average_loser": sum(losers) / len(losers) if losers else 0.0,
        "profit_factor": profit_factor, "expectancy_after_estimated_cost": expectancy, "stress_expectancy_after_estimated_cost": stress_expectancy,
        "expectancy_lower_95": expectancy_lower_95, "strategy_total_return_after_estimated_cost": strategy_total_return,
        "benchmark_performance": benchmark_performance, "benchmark_relative_return": {symbol: strategy_total_return - item["total_return"] for symbol, item in benchmark_performance.items()},
        "maximum_drawdown_dollars": drawdown, "maximum_single_winner_gross_profit_fraction": concentration,
        "average_mfe": sum(known_mfe) / len(known_mfe) if known_mfe else None, "average_mae": sum(known_mae) / len(known_mae) if known_mae else None,
        "security_violations": security_violations, "unresolved_state_reconciliation_failures": unresolved_failures,
        "by_setup_type": _group(completed, "setup_type"), "by_market_regime": _group(completed, "market_regime"), "by_catalyst_classification": _group(completed, "catalyst_classification"), "by_time_of_day": _group(completed, "time_of_day"),
        "accelerated_experiment": experiment,
    }, "permissions_changed": False, "automatic_mode_change": False}
