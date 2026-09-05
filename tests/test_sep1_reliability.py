from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator import ShadowOrchestrator
from trader.market_calendar import ET, EquityMarketCalendar
from trader.models import SchemaValidationError
from trader.safety import load_config, validate_json
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS
from trader.shadow_monitor import aggregate_completed_15m
from trader.state import initial_state


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def bar(timestamp: datetime, open_: float, high: float, low: float, close: float,
        volume: float = 100, complete: bool = True) -> dict:
    return {
        "timestamp": timestamp.isoformat(), "open": open_, "high": high,
        "low": low, "close": close, "volume": volume, "complete": complete,
    }


class Sep1ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 9, 1))

    def fixture(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_post_entry_luna_cycle_never_launches_normal_sol_or_increments_circuit(self):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.config = self.config
        core.root = ROOT
        core.scheduler = Mock()
        core.scheduler.late_selectivity.return_value = False
        core.store = Mock()
        now = datetime(2026, 9, 1, 12, 15, tzinfo=ET)
        core._trusted_now = Mock(return_value=now)
        core._add_usage = Mock()
        core.run_senior = Mock()
        cycle = self.fixture("luna_candidate.json")
        cycle.update({"cycle_id": "2026-09-01-cycle-1", "session_date": "2026-09-01",
                      "timestamp": now.isoformat()})
        calls = {"get_accounts": 1, "get_equity_orders": 1, "get_equity_positions": 1,
                 "run_scan": 1, "get_equity_quotes": 1, "get_equity_tradability": 1,
                 "get_equity_historicals": 1, "get_equity_technical_indicators": 3}
        result = SimpleNamespace(data=cycle, tool_calls=calls, web_searches=0, usage={}, diagnostics={})
        core._run_ai_job = Mock(return_value=result)
        state = initial_state("2026-09-01")
        state["shadow_plans"].append({
            "research_role": "PRIMARY", "outcome": {"entry_triggered": True, "status": "AMBIGUOUS"},
        })
        before = deepcopy(state["ai_circuit"])
        with patch("orchestrator.write_non_destructive_text"), patch("orchestrator.write_json_companion"):
            returned = core.run_luna_cycle(state, self.session, now)
        core.run_senior.assert_not_called()
        self.assertFalse(returned["sol_escalation"])
        self.assertEqual(state["ai_circuit"], before)
        self.assertEqual(state["schedule_events"][-1]["status"], "SKIPPED_PRIMARY_ENTRY_TRIGGERED")

    def test_second_entered_plan_validator_remains_defense_in_depth(self):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.config = self.config
        state = initial_state("2026-09-01")
        state["shadow_plans"].append({"research_role": "PRIMARY", "outcome": {"entry_triggered": True, "status": "STOPPED"}})
        decision = self.fixture("senior_plan.json")
        for key in ("decision_timestamp", "quote_timestamp", "latest_entry_time", "mandatory_flat_time", "time_exit"):
            decision[key] = decision[key].replace("2026-08-14", "2026-09-01")
        with self.assertRaisesRegex(SchemaValidationError, "one entered"):
            core._validate_senior(
                decision, self.fixture("luna_candidate.json")["finalists"],
                state, self.session, 3,
            )

    def test_direct_senior_entry_point_is_also_suppressed_before_runner(self):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.store = Mock()
        core.runner = Mock()
        core._trusted_now = Mock(return_value=datetime(2026, 9, 1, 12, 0, tzinfo=ET))
        state = initial_state("2026-09-01")
        state["shadow_plans"].append({
            "research_role": "PRIMARY", "outcome": {"entry_triggered": True, "status": "STOPPED"},
        })
        cycle = self.fixture("luna_candidate.json")
        cycle.update({"cycle_id": "2026-09-01-cycle-12", "timestamp": "2026-09-01T12:00:00-04:00"})
        result = core.run_senior(state, self.session, cycle)
        self.assertEqual(result["decision"], "SUPPRESSED_PRIMARY_ENTRY_TRIGGERED")
        core.runner.run.assert_not_called()

    def test_deterministic_aggregation_uses_exact_completed_regular_session_groups(self):
        start = self.session.market_open
        bars = [
            bar(start, 10, 11, 9, 10.5, 100),
            bar(start + timedelta(minutes=5), 10.5, 12, 10, 11, 200),
            bar(start + timedelta(minutes=10), 11, 11.5, 10.5, 11.25, 300),
            bar(start + timedelta(minutes=15), 20, 21, 19, 20.5, 400),
            bar(start + timedelta(minutes=20), 20.5, 22, 20, 21, 500),
            bar(start + timedelta(minutes=25), 21, 23, 20.5, 22, 600, complete=False),
            bar(start - timedelta(minutes=5), 8, 9, 7, 8.5),
        ]
        result = aggregate_completed_15m(
            bars, session_open=self.session.market_open, session_close=self.session.market_close,
            as_of=start + timedelta(minutes=30),
        )
        self.assertEqual(result, [{
            "timestamp": start.isoformat(), "open": 10.0, "high": 12.0,
            "low": 9.0, "close": 11.25, "volume": 600.0, "complete": True,
        }])

    def test_latest_eight_only_and_malformed_ohlc_invalidates_whole_group(self):
        start = self.session.market_open
        bars = []
        for group in range(10):
            anchor = start + timedelta(minutes=15 * group)
            bars.extend(bar(anchor + timedelta(minutes=5 * offset), 10, 11, 9, 10, 1)
                        for offset in range(3))
        bars[4]["high"] = 8
        result = aggregate_completed_15m(
            bars, session_open=start, session_close=self.session.market_close,
            as_of=start + timedelta(minutes=150),
        )
        self.assertEqual(len(result), 8)
        self.assertNotIn((start + timedelta(minutes=15)).isoformat(),
                         {item["timestamp"] for item in result})
        self.assertEqual(result[-1]["timestamp"], (start + timedelta(minutes=135)).isoformat())

    def test_luna_cannot_supply_or_override_python_15m_values(self):
        cycle = self.fixture("luna_candidate.json")
        cycle["finalists"][0]["completed_15m_structure"] = [{"timestamp": "invented"}]
        with self.assertRaises(SchemaValidationError):
            validate_json(cycle, ROOT / "schemas" / "luna-cycle.schema.json")

        cycle = self.fixture("luna_candidate.json")
        start = self.session.market_open
        cycle["finalists"][0]["completed_5m_bars"] = [
            bar(start + timedelta(minutes=offset), 10, 11, 9, 10 + offset / 100, 10)
            for offset in (0, 5, 10)
        ]
        ShadowOrchestrator._derive_luna_15m(cycle, self.session, start + timedelta(minutes=15))
        self.assertNotIn("completed_5m_bars", cycle["finalists"][0])
        self.assertEqual(cycle["finalists"][0]["completed_15m_structure"][0]["timestamp"], start.isoformat())

    def test_sep1_avoidable_sequence_does_not_open_circuit(self):
        state = initial_state("2026-09-01")
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.config = self.config
        state["shadow_plans"].append({"research_role": "PRIMARY", "outcome": {"entry_triggered": True}})
        self.assertTrue(core._primary_entry_triggered(state))
        malformed = [
            bar(self.session.market_open, 10, 9, 8, 10),
            bar(self.session.market_open + timedelta(minutes=5), 10, 11, 9, 10),
            bar(self.session.market_open + timedelta(minutes=10), 10, 11, 9, 10),
        ]
        for _ in range(2):
            self.assertEqual(aggregate_completed_15m(
                malformed, session_open=self.session.market_open,
                session_close=self.session.market_close,
                as_of=self.session.market_open + timedelta(minutes=15),
            ), [])
        self.assertEqual(state["ai_circuit"]["failure_count"], 0)
        self.assertEqual(state["ai_circuit"]["status"], "CLOSED")

    def test_mode_strategy_risk_and_tool_boundary_are_unchanged(self):
        self.assertEqual(initial_state("2026-09-01")["mode"], "SHADOW")
        self.assertEqual(len(APPROVED_SHADOW_ROBINHOOD_TOOLS), 22)
        write_prefixes = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_")
        self.assertEqual([name for name in APPROVED_SHADOW_ROBINHOOD_TOOLS if name.startswith(write_prefixes)], [])
        self.assertEqual(self.config["risk"], load_config(ROOT / "config" / "strategy.yaml")["risk"])
        self.assertEqual(self.config["scanner"], load_config(ROOT / "config" / "strategy.yaml")["scanner"])


if __name__ == "__main__":
    unittest.main()
