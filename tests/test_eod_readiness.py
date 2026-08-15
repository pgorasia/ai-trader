from __future__ import annotations

import json
import math
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunResult, StateCorruptionError
from trader.readiness import calculate_readiness
from trader.safety import load_config
from trader.shadow_monitor import ShadowPlanMonitor
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]


class FakeEodRunner:
    def __init__(self, payload): self.payload = payload
    def run(self, **kwargs): return CodexRunResult(data=self.payload, usage={"total_tokens": 100}, tool_calls={"get_equity_historicals": 1}, web_searches=0)


class EodReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config/strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 8, 14))
        cls.plan_template = json.loads((ROOT / "tests/fixtures/senior_plan.json").read_text(encoding="utf-8"))

    def finalized_state(self, day: str, pnls=(0.5,)):
        state = initial_state(day)
        state["cycles"].append({"cycle_id": f"{day}-cycle-1"})
        state["eod_completed"] = True
        state["operation_ids"].append(f"eod:{day}")
        state["eod_review"] = {"session_date": day, "benchmark_closes": {"SPY": 700.0, "QQQ": 600.0}}
        for index, pnl in enumerate(pnls):
            plan_id = f"{day}-plan-{index + 1}"
            plan = deepcopy(self.plan_template)
            for key in ("decision_timestamp", "latest_entry_time", "mandatory_flat_time", "time_exit"):
                plan[key] = day + plan[key][10:]
            entry = f"{day}T10:05:00-04:00"; exit_time = f"{day}T10:10:00-04:00"
            outcome = ShadowPlanMonitor.initial_outcome()
            outcome.update({"status": "TARGET1" if pnl > 0 else "STOPPED", "entry_triggered": True, "entry_timestamp": entry, "entry_bar_timestamp": entry, "entry_price": 10.0, "exit_timestamp": exit_time, "exit_price": 10.0 + pnl / 2, "exit_reason": "TARGET1" if pnl > 0 else "STOPPED", "pnl": pnl, "realized_r": pnl, "mfe": 0.7, "mae": -0.2})
            trailing = deepcopy(outcome)
            trailing_pnl = pnl + 0.1 if pnl > 0 else pnl
            trailing.update({"pnl": trailing_pnl, "realized_r": trailing_pnl, "exit_price": 10.0 + trailing_pnl / 2, "exit_reason": "TRAILING_STOP", "variant": "TRAILING_STOP", "trailing_active": True, "trailing_updates": 1, "recent_completed_lows": [], "last_processed_bar_timestamp": exit_time})
            state["shadow_plans"].append({"plan_id": plan_id, "frozen_at": plan["decision_timestamp"], "research_role": "PRIMARY", "original_plan": plan, "outcome": outcome, "trailing_outcome": trailing})
            state["completed_shadow_trades"].append({"plan_id": plan_id, "symbol": "TEST", "pnl": pnl, "realized_r": pnl, "hypothetical_notional": 20, "mfe": 0.7, "mae": -0.2, "setup_type": "BASE_BREAKOUT", "market_regime": "TRENDING_BULLISH", "catalyst_classification": "VERIFIED_FRESH", "time_of_day": "10:05", "entry_timestamp": entry, "exit_timestamp": exit_time})
            for variant, value in (("FIXED_TARGET", pnl), ("TRAILING_STOP", trailing_pnl)):
                state.setdefault("research_outcomes", []).append({"plan_id": plan_id, "variant": variant, "research_role": "PRIMARY", "research_rank": 1, "symbol": "TEST", "setup_type": "BASE_BREAKOUT", "market_regime": "TRENDING_BULLISH", "hypothetical_notional": 20, "entry_triggered": True, "entry_timestamp": entry, "exit_timestamp": exit_time, "exit_reason": variant, "pnl": value, "realized_r": value, "mfe": 0.7, "mae": -0.2, "trailing_active": variant == "TRAILING_STOP", "trailing_updates": 1 if variant == "TRAILING_STOP" else 0})
        return state

    def test_zero_trade_readiness(self):
        result = calculate_readiness([], self.config)
        self.assertEqual(result["status"], "CONTINUE_SHADOW"); self.assertEqual(result["metrics"]["completed_shadow_trades"], 0)

    def test_readiness_reached_with_conservative_sample(self):
        days = ["2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"]
        policy = deepcopy(self.config)
        policy["readiness"]["minimum_market_sessions"] = 10
        policy["readiness"]["minimum_completed_shadow_trades"] = 10
        policy["experiment"].update({"development_sessions": 5, "validation_sessions": 5, "minimum_development_pairs": 5, "minimum_validation_primary_trades": 5})
        result = calculate_readiness([self.finalized_state(day) for day in days], policy)
        self.assertEqual(result["status"], "READY_FOR_APPROVAL_REVIEW"); self.assertTrue(all(result["requirements"].values()))
        experiment = result["metrics"]["accelerated_experiment"]
        self.assertEqual(experiment["phase"], "VALIDATION_COMPLETE")
        self.assertEqual(experiment["selected_exit_variant"], "TRAILING_STOP")
        self.assertTrue(experiment["selection_is_frozen"])
        self.assertTrue(experiment["attention_required"])

    def test_production_readiness_requires_a_material_sample(self):
        self.assertEqual(self.config["readiness"]["minimum_market_sessions"], 30)
        self.assertEqual(self.config["experiment"]["development_sessions"], 15)
        self.assertEqual(self.config["experiment"]["validation_sessions"], 15)

    def test_duplicate_trade_in_readiness_fails(self):
        state = self.finalized_state("2026-07-01", (0.5,)); state["completed_shadow_trades"].append(deepcopy(state["completed_shadow_trades"][0]))
        with self.assertRaises(StateCorruptionError): calculate_readiness([state], self.config)

    def test_negative_notional_and_nonfinite_fail(self):
        for field, value in (("hypothetical_notional", -1), ("pnl", math.nan), ("mfe", math.inf)):
            with self.subTest(field=field):
                state = self.finalized_state("2026-07-01", (0.5,)); state["completed_shadow_trades"][0][field] = value
                with self.assertRaises(StateCorruptionError): calculate_readiness([state], self.config)

    def test_all_winning_and_all_losing_are_finite_safe(self):
        winners = calculate_readiness([self.finalized_state("2026-07-01", (0.5,))], self.config)
        losers = calculate_readiness([self.finalized_state("2026-07-01", (-0.5,))], self.config)
        self.assertIsNone(winners["metrics"]["profit_factor"])
        self.assertEqual(losers["metrics"]["profit_factor"], 0.0)

    def test_unknown_excursions_are_not_converted_to_zero(self):
        state = self.finalized_state("2026-07-01", (0.5,)); state["completed_shadow_trades"][0]["mfe"] = None; state["completed_shadow_trades"][0]["mae"] = None
        result = calculate_readiness([state], self.config)
        self.assertIsNone(result["metrics"]["average_mfe"]); self.assertIsNone(result["metrics"]["average_mae"])

    def test_readiness_requires_complete_benchmarks_and_one_strategy_cohort(self):
        state = self.finalized_state("2026-07-01")
        state["eod_review"]["benchmark_closes"]["QQQ"] = None
        result = calculate_readiness([state], self.config)
        self.assertFalse(result["requirements"]["benchmark_completeness"])
        state = self.finalized_state("2026-07-01"); state["strategy_version"] = "old-strategy"
        result = calculate_readiness([state], self.config)
        self.assertFalse(result["requirements"]["single_strategy_cohort"])

    def test_legacy_operational_failure_is_not_a_security_violation(self):
        state = self.finalized_state("2026-07-01")
        state["security_events"].append({"timestamp": "2026-07-01T09:35:00-04:00", "category": "PREFLIGHT_FAILURE", "message": "temporary outage"})
        result = calculate_readiness([state], self.config)
        self.assertEqual(result["metrics"]["security_violations"], 0)

    def test_statistical_and_stress_expectancy_are_reported(self):
        states = [self.finalized_state(day) for day in ("2026-07-01", "2026-07-02", "2026-07-06")]
        result = calculate_readiness(states, self.config)
        self.assertGreater(result["metrics"]["stress_expectancy_after_estimated_cost"], 0)
        self.assertGreater(result["metrics"]["expectancy_lower_95"], 0)

    def test_insufficient_pairs_extend_development_without_using_future_validation(self):
        policy = deepcopy(self.config)
        policy["readiness"].update({"minimum_market_sessions": 4, "minimum_completed_shadow_trades": 3})
        policy["experiment"].update({"development_sessions": 2, "validation_sessions": 1, "minimum_development_pairs": 2, "minimum_validation_primary_trades": 1})
        states = [self.finalized_state(day) for day in ("2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07")]
        states[0]["research_outcomes"] = []
        result = calculate_readiness(states, policy)
        experiment = result["metrics"]["accelerated_experiment"]
        self.assertEqual(experiment["development_sessions_completed"], 3)
        self.assertEqual(experiment["validation_sessions_completed"], 1)
        self.assertEqual(experiment["phase"], "VALIDATION_COMPLETE")

    def test_eod_evaluation_writes_structured_and_markdown_companions(self):
        review = {"session_date": "2026-08-14", "timestamp": "2026-08-14T16:05:00-04:00", "symbol_bars": {}, "decision_reviews": [], "benchmark_closes": {"SPY": 700.0, "QQQ": 600.0}, "robinhood_tool_call_count": 1, "errors": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); core = ShadowOrchestrator.__new__(ShadowOrchestrator)
            core.root = root; core.config = self.config; core.store = StateStore(root / "state"); core.runner = FakeEodRunner(review); core.monitor = ShadowPlanMonitor()
            state = initial_state("2026-08-14"); core.store.save(state); result = core.eod(state, self.session)
            self.assertTrue((root / "reports/2026-08-14-eod.json").is_file()); self.assertEqual(result["readiness"]["status"], "CONTINUE_SHADOW")
            self.assertTrue((root / "reports/2026-08-14-experiment.json").is_file())

    def test_duplicate_eod_review_rejected(self):
        review_item = {"symbol": "TEST", "decision_timestamp": "2026-08-14T10:03:00-04:00", "classification": "GOOD_AVOIDANCE", "subsequent_mfe_percent": 0.1, "subsequent_mae_percent": -0.1, "later_material_setup": False, "analysis": "x"}
        review = {"session_date": "2026-08-14", "timestamp": "2026-08-14T16:05:00-04:00", "symbol_bars": {}, "decision_reviews": [review_item, deepcopy(review_item)], "benchmark_closes": {"SPY": 700.0, "QQQ": 600.0}, "robinhood_tool_call_count": 1, "errors": []}
        core = ShadowOrchestrator.__new__(ShadowOrchestrator); core.config = self.config; core.runner = FakeEodRunner(review)
        state = initial_state("2026-08-14"); state["senior_decisions"] = [{"source_cycle_id": "c1", "decision_timestamp": review_item["decision_timestamp"], "rejections": [{"symbol": "TEST"}]}]
        with self.assertRaises(Exception): core.eod(state, self.session)

    def test_eod_rejects_missing_benchmark_close(self):
        review = {"session_date": "2026-08-14", "timestamp": "2026-08-14T16:05:00-04:00", "symbol_bars": {}, "decision_reviews": [], "benchmark_closes": {"SPY": 700.0, "QQQ": None}, "robinhood_tool_call_count": 1, "errors": []}
        core = ShadowOrchestrator.__new__(ShadowOrchestrator); core.config = self.config; core.runner = FakeEodRunner(review); core.monitor = ShadowPlanMonitor()
        with self.assertRaises(Exception): core.eod(initial_state("2026-08-14"), self.session)


if __name__ == "__main__": unittest.main()
