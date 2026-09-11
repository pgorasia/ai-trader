from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from orchestrator import ROOT, ShadowOrchestrator
from trader.market_calendar import EquityMarketCalendar
from trader.shadow_monitor import ShadowPlanMonitor
from trader.safety import load_config
from trader.state import initial_state


def bar(timestamp: str, close: float, *, complete: bool = True) -> dict:
    return {"timestamp": timestamp, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "complete": complete}


class Sep11PlanLifecycleTests(unittest.TestCase):
    def setUp(self):
        plan = json.loads((Path(ROOT) / "tests/fixtures/senior_plan.json").read_text())
        self.record = {
            "plan_id": "p1", "frozen_at": plan["decision_timestamp"],
            "research_role": "PRIMARY", "original_plan": plan,
            "outcome": ShadowPlanMonitor.initial_outcome(),
            "trailing_outcome": ShadowPlanMonitor.initial_trailing_outcome(plan["stop_price"]),
        }
        self.monitor = ShadowPlanMonitor()

    def test_pending_plan_is_not_an_entered_primary(self):
        self.assertFalse(ShadowOrchestrator._primary_entry_triggered({"shadow_plans": [self.record]}))
        entered = deepcopy(self.record)
        entered["outcome"].update({"status": "OPEN", "entry_triggered": True})
        self.assertTrue(ShadowOrchestrator._primary_entry_triggered({"shadow_plans": [entered]}))

    def test_structured_pre_entry_invalidation_terminates_both_variants(self):
        record = deepcopy(self.record)
        record["original_plan"].update({
            "pre_entry_invalidation_price": 9.92,
            "pre_entry_invalidation_type": "COMPLETED_5M_CLOSE_BELOW",
        })
        evidence = [bar("2026-08-14T10:05:00-04:00", 9.84)]
        fixed = self.monitor.evaluate(record, evidence, datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        paired = self.monitor.evaluate_trailing(fixed, evidence, datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertEqual(paired["outcome"]["status"], "PRE_ENTRY_INVALIDATED")
        self.assertEqual(paired["trailing_outcome"]["status"], "PRE_ENTRY_INVALIDATED")
        self.assertFalse(ShadowOrchestrator._plan_is_active(paired))
        self.assertFalse(paired["outcome"]["entry_triggered"])

    def test_post_entry_stop_is_not_a_pre_entry_invalidation(self):
        record = deepcopy(self.record)
        record["original_plan"]["pre_entry_invalidation_price"] = None
        record["original_plan"]["pre_entry_invalidation_type"] = None
        result = self.monitor.evaluate(record, [bar("2026-08-14T10:05:00-04:00", 9.4)],
                                       datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "PENDING")

    def test_unresolved_qualitative_condition_cannot_fabricate_entry(self):
        record = deepcopy(self.record)
        record["original_plan"]["entry_requires_qualitative_confirmation"] = True
        evidence = [bar("2026-08-14T10:05:00-04:00", 10.0)]
        unresolved = self.monitor.evaluate(record, evidence, datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertEqual(unresolved["outcome"]["status"], "PENDING")
        confirmed = self.monitor.evaluate(record, evidence, datetime.fromisoformat("2026-08-14T10:10:00-04:00"), qualitative_entry_confirmed=True)
        self.assertEqual(confirmed["outcome"]["status"], "OPEN")

    def test_forming_bar_is_ignored_not_a_monitor_failure(self):
        result = self.monitor.evaluate(self.record, [bar("2026-08-14T10:05:00-04:00", 10.0, complete=False)],
                                       datetime.fromisoformat("2026-08-14T10:06:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "PENDING")


class Sep11OrchestratorTests(unittest.TestCase):
    def bare(self):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = ROOT
        core.config = load_config(ROOT / "config/strategy.yaml")
        core.calendar = EquityMarketCalendar(core.config["exchange_calendar"], int(core.config["schedule"]["eod_offset_minutes"]))
        core.monitor = ShadowPlanMonitor()
        core.store = Mock()
        return core

    def test_expired_pending_plan_avoids_subprocess_and_releases_scanner(self):
        core = self.bare()
        session = core.calendar.session_for(date(2026, 8, 19))
        plan = json.loads((Path(ROOT) / "tests/fixtures/senior_plan.json").read_text())
        plan["latest_entry_time"] = session.latest_entry.isoformat()
        state = initial_state(session.session_date, core.config["timezone"], session.market_open)
        state["shadow_plans"] = [{
            "plan_id": "p1", "frozen_at": plan["decision_timestamp"], "research_role": "PRIMARY",
            "original_plan": plan, "outcome": core.monitor.initial_outcome(),
            "trailing_outcome": core.monitor.initial_trailing_outcome(plan["stop_price"]),
        }]
        core._run_ai_job = Mock(side_effect=AssertionError("deterministic expiry must not launch model"))
        core.monitor_active_plans(state, session.latest_entry)
        core._run_ai_job.assert_not_called()
        self.assertEqual(state["shadow_plans"][0]["outcome"]["status"], "EXPIRED")
        self.assertEqual(core._active_plan_count(state), 0)

    def test_monitor_history_fanout_is_one_per_distinct_symbol_and_web_disabled(self):
        core = self.bare()
        session = core.calendar.session_for(date(2026, 8, 19))
        plan = json.loads((Path(ROOT) / "tests/fixtures/senior_plan.json").read_text())
        plan["decision_timestamp"] = (session.market_open + timedelta(minutes=10)).isoformat()
        plan["latest_entry_time"] = session.latest_entry.isoformat()
        state = initial_state(session.session_date, core.config["timezone"], session.market_open)
        state["shadow_plans"] = [{"plan_id": "p1", "original_plan": plan,
            "outcome": core.monitor.initial_outcome(), "trailing_outcome": core.monitor.initial_trailing_outcome(plan["stop_price"])}]
        core._run_ai_job = Mock(side_effect=RuntimeError("capture"))
        with self.assertRaisesRegex(RuntimeError, "capture"):
            core.monitor_active_plans(state, session.market_open + timedelta(minutes=20))
        call = core._run_ai_job.call_args.kwargs
        self.assertFalse(call["allow_web"])
        self.assertEqual(call["maximum_robinhood_tool_calls"], {"get_equity_historicals": 1})


if __name__ == "__main__":
    unittest.main()
