from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import ReadinessStatus


def _group(trades: list[dict[str, Any]], key: str) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        label = str(trade.get(key) or "UNKNOWN")
        groups[label].append(float(trade["net_pnl_after_cost"]))
    return {label: {"trades": len(values), "net_pnl": sum(values), "average_pnl": sum(values) / len(values)} for label, values in sorted(groups.items())}


def calculate_readiness(states: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    rules = config["readiness"]
    completed: list[dict[str, Any]] = []
    security_violations = 0
    unresolved_failures = 0
    luna_cycles = sol_decisions = no_trade = plans = 0
    benchmark_points: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for state in states:
        luna_cycles += len(state.get("cycles", []))
        sol_decisions += len(state.get("senior_decisions", []))
        no_trade += sum(1 for item in state.get("senior_decisions", []) if item.get("decision") == "NO_TRADE")
        plans += len(state.get("shadow_plans", []))
        security_violations += len(state.get("security_events", []))
        unresolved_failures += sum(1 for error in state.get("errors", []) if not error.get("resolved", False))
        benchmark_closes = state.get("eod_review", {}).get("benchmark_closes", {})
        for symbol in ("SPY", "QQQ"):
            if benchmark_closes.get(symbol) is not None:
                benchmark_points[symbol].append((state["session_date"], float(benchmark_closes[symbol])))
        for trade in state.get("completed_shadow_trades", []):
            if trade.get("pnl") is None:
                continue
            item = dict(trade)
            notional = float(item.get("hypothetical_notional", 0))
            cost = notional * float(rules["estimated_round_trip_cost_bps"]) / 10_000
            item["estimated_cost"] = cost
            item["net_pnl_after_cost"] = float(item["pnl"]) - cost
            completed.append(item)

    values = [float(item["net_pnl_after_cost"]) for item in completed]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)
    expectancy = sum(values) / len(values) if values else 0.0
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    concentration = max(winners) / gross_profit if winners and gross_profit > 0 else 1.0
    sessions = sum(1 for state in states if state.get("cycles") or state.get("eod_completed"))
    benchmark_performance = {}
    for symbol, points in benchmark_points.items():
        ordered = sorted(points)
        start, end = ordered[0][1], ordered[-1][1]
        benchmark_performance[symbol] = {
            "observations": len(ordered),
            "start_close": start,
            "end_close": end,
            "total_return": (end - start) / start if start else 0.0,
        }
    strategy_total_return = sum(values) / float(config["risk"]["reference_capital"])
    requirements = {
        "security_boundary": security_violations <= int(rules["maximum_security_violations"]),
        "state_reconciliation": unresolved_failures <= int(rules["maximum_unresolved_state_failures"]),
        "market_sessions": sessions >= int(rules["minimum_market_sessions"]),
        "completed_shadow_trades": len(completed) >= int(rules["minimum_completed_shadow_trades"]),
        "positive_expectancy_after_cost": expectancy > 0,
        "profit_factor": profit_factor > float(rules["minimum_profit_factor"]),
        "profit_concentration": concentration <= float(rules["maximum_single_winner_gross_profit_fraction"]),
        "maximum_drawdown": drawdown <= float(rules["maximum_drawdown_dollars"]),
    }
    status = ReadinessStatus.READY_FOR_APPROVAL_REVIEW if all(requirements.values()) else ReadinessStatus.CONTINUE_SHADOW
    return {
        "status": status,
        "requirements": requirements,
        "metrics": {
            "market_sessions": sessions,
            "luna_cycles": luna_cycles,
            "sol_decisions": sol_decisions,
            "no_trade_count": no_trade,
            "shadow_trade_plans": plans,
            "completed_shadow_trades": len(completed),
            "wins": len(winners),
            "losses": len(losers),
            "win_rate": len(winners) / len(completed) if completed else 0.0,
            "average_r": sum(float(item.get("realized_r", 0)) for item in completed) / len(completed) if completed else 0.0,
            "average_winner": gross_profit / len(winners) if winners else 0.0,
            "average_loser": sum(losers) / len(losers) if losers else 0.0,
            "profit_factor": profit_factor,
            "expectancy_after_estimated_cost": expectancy,
            "strategy_total_return_after_estimated_cost": strategy_total_return,
            "benchmark_performance": benchmark_performance,
            "benchmark_relative_return": {
                symbol: strategy_total_return - values_for_symbol["total_return"]
                for symbol, values_for_symbol in benchmark_performance.items()
            },
            "maximum_drawdown_dollars": drawdown,
            "maximum_single_winner_gross_profit_fraction": concentration,
            "average_mfe": sum(float(item.get("mfe", 0) or 0) for item in completed) / len(completed) if completed else 0.0,
            "average_mae": sum(float(item.get("mae", 0) or 0) for item in completed) / len(completed) if completed else 0.0,
            "security_violations": security_violations,
            "unresolved_state_reconciliation_failures": unresolved_failures,
            "by_setup_type": _group(completed, "setup_type"),
            "by_market_regime": _group(completed, "market_regime"),
            "by_catalyst_classification": _group(completed, "catalyst_classification"),
            "by_time_of_day": _group(completed, "time_of_day"),
        },
        "permissions_changed": False,
        "automatic_mode_change": False,
    }
