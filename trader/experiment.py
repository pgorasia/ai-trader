from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any


VARIANTS = ("FIXED_TARGET", "TRAILING_STOP")


def _metrics(items: list[dict[str, Any]], cost_bps: float) -> dict[str, Any]:
    values = [float(item["pnl"]) - float(item["hypothetical_notional"]) * cost_bps / 10_000 for item in items]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    gross_profit, gross_loss = sum(winners), abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss else (None if gross_profit else 0.0)
    expectancy = sum(values) / len(values) if values else 0.0
    lower_95 = None if len(values) < 2 else expectancy - 1.645 * statistics.stdev(values) / math.sqrt(len(values))
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "observations": len(values), "wins": len(winners), "losses": len(losers),
        "net_pnl_after_cost": sum(values), "expectancy_after_cost": expectancy,
        "expectancy_lower_95": lower_95, "profit_factor": profit_factor,
        "maximum_drawdown_dollars": drawdown,
        "maximum_single_winner_gross_profit_fraction": max(winners) / gross_profit if winners and gross_profit > 0 else 0.0,
    }


def _usable_outcomes(states: list[dict[str, Any]], *, role: str | None = None) -> list[dict[str, Any]]:
    result = []
    for state in states:
        for item in state.get("research_outcomes", []):
            if item.get("variant") not in VARIANTS or item.get("pnl") is None or not item.get("entry_triggered"):
                continue
            if role is not None and item.get("research_role", "PRIMARY") != role:
                continue
            result.append({**item, "session_date": state["session_date"]})
    return result


def _paired_outcomes(states: list[dict[str, Any]]) -> list[dict[str, dict[str, Any]]]:
    by_plan: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in _usable_outcomes(states):
        by_plan[(item["session_date"], item["plan_id"])][item["variant"]] = item
    return [pair for pair in by_plan.values() if set(pair) == set(VARIANTS)]


def calculate_experiment(states: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    rules = config["experiment"]
    finalized = sorted(
        (state for state in states if state.get("eod_completed") and isinstance(state.get("eod_review"), dict)),
        key=lambda state: state["session_date"],
    )
    development_count = int(rules["development_sessions"])
    validation_count = int(rules["validation_sessions"])
    development = finalized[:development_count]
    minimum_pairs = int(rules["minimum_development_pairs"])
    paired = _paired_outcomes(development)
    if len(development) >= development_count and len(paired) < minimum_pairs:
        for end in range(development_count + 1, len(finalized) + 1):
            candidate_development = finalized[:end]
            candidate_pairs = _paired_outcomes(candidate_development)
            development, paired = candidate_development, candidate_pairs
            if len(paired) >= minimum_pairs:
                break
    paired_items = {variant: [pair[variant] for pair in paired] for variant in VARIANTS}
    stress_bps = float(config["readiness"]["stress_round_trip_cost_bps"])
    development_metrics = {variant: _metrics(paired_items[variant], stress_bps) for variant in VARIANTS}

    selected = None
    if len(development) >= development_count and len(paired) >= minimum_pairs:
        selected = max(
            VARIANTS,
            key=lambda variant: (
                development_metrics[variant]["net_pnl_after_cost"],
                -development_metrics[variant]["maximum_drawdown_dollars"],
                1 if variant == "FIXED_TARGET" else 0,
            ),
        )

    validation = finalized[len(development):len(development) + validation_count] if selected else []
    validation_items = _usable_outcomes(validation, role="PRIMARY")
    selected_validation = [item for item in validation_items if item["variant"] == selected] if selected else []
    validation_metrics = _metrics(selected_validation, stress_bps)
    minimum_validation = int(rules["minimum_validation_primary_trades"])
    readiness = config["readiness"]
    profit_factor = validation_metrics["profit_factor"]
    validation_requirements = {
        "development_sessions": len(development) >= development_count,
        "paired_development_outcomes": len(paired) >= minimum_pairs,
        "exit_policy_frozen": selected is not None,
        "validation_sessions": len(validation) >= validation_count,
        "validation_primary_trades": validation_metrics["observations"] >= minimum_validation,
        "positive_stress_expectancy": validation_metrics["expectancy_after_cost"] > 0,
        "positive_expectancy_lower_95": validation_metrics["expectancy_lower_95"] is not None and validation_metrics["expectancy_lower_95"] > 0,
        "profit_factor": validation_metrics["net_pnl_after_cost"] > 0 if profit_factor is None else profit_factor > float(readiness["minimum_profit_factor"]),
        "maximum_drawdown": validation_metrics["maximum_drawdown_dollars"] <= float(readiness["maximum_drawdown_dollars"]),
        "profit_concentration": validation_metrics["maximum_single_winner_gross_profit_fraction"] <= float(readiness["maximum_single_winner_gross_profit_fraction"]),
    }
    if len(development) < development_count:
        phase = "DEVELOPMENT"
    elif selected is None:
        phase = "DEVELOPMENT_EXTENDED"
    elif len(validation) < validation_count:
        phase = "VALIDATION"
    else:
        phase = "VALIDATION_COMPLETE"
    recommendation = "READY_FOR_APPROVAL_REVIEW" if all(validation_requirements.values()) else "CONTINUE_SHADOW"
    return {
        "phase": phase,
        "development_sessions_completed": len(development),
        "validation_sessions_completed": len(validation),
        "paired_development_outcomes": len(paired),
        "selected_exit_variant": selected,
        "selection_is_frozen": selected is not None,
        "development_variant_metrics": development_metrics,
        "validation_selected_variant_metrics": validation_metrics,
        "validation_requirements": validation_requirements,
        "recommendation": recommendation,
        "attention_required": phase == "VALIDATION_COMPLETE",
        "automatic_live_activation": False,
    }
