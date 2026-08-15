from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator import ShadowOrchestrator
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunResult, PreflightError
from trader.safety import load_config
from trader.scheduler import SessionScheduler
from trader.shadow_monitor import ShadowPlanMonitor
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]


class SequenceClock:
    def __init__(self, *values): self.values = list(values); self.last = self.values[-1]
    def now(self):
        if self.values: self.last = self.values.pop(0)
        return self.last


class FakeRunner:
    def __init__(self, payload, *, web=0, tools=None): self.payload = payload; self.web = web; self.tools = tools or {}; self.calls = 0
    def run(self, **kwargs): self.calls += 1; return CodexRunResult(data=deepcopy(self.payload), web_searches=self.web, tool_calls=self.tools)


class CandidatePlanRunner:
    def __init__(self, template): self.template = template; self.calls = 0
    def run(self, **kwargs):
        self.calls += 1
        symbol = kwargs["context"]["finalists"][0]["symbol"]
        payload = deepcopy(self.template); payload["symbol"] = symbol; payload["evaluated_symbols"] = [symbol]
        return CodexRunResult(data=payload, web_searches=1, tool_calls={})


class Phase2OrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config/strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 8, 14))
        cls.cycle = json.loads((ROOT / "tests/fixtures/luna_candidate.json").read_text(encoding="utf-8"))
        cls.no_trade = json.loads((ROOT / "tests/fixtures/senior_no_trade.json").read_text(encoding="utf-8"))

    def core(self, directory, runner, clock):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator); core.root = Path(directory); core.config = self.config
        core.store = StateStore(Path(directory) / "state"); core.runner = runner; core.clock = clock; core.monitor = ShadowPlanMonitor(); core.scheduler = SessionScheduler(self.config["schedule"])
        return core

    @staticmethod
    def luna_tools():
        return {"get_accounts": 1, "get_portfolio": 1, "get_equity_orders": 1, "get_equity_positions": 1, "run_scan": 1, "get_equity_quotes": 1, "get_equity_tradability": 1, "get_equity_historicals": 1, "get_equity_technical_indicators": 4}

    def test_luna_one_second_before_cutoff_can_start_but_crossing_skips_sol(self):
        cycle = deepcopy(self.cycle); cycle["timestamp"] = "2026-08-14T15:39:59-04:00"
        runner = FakeRunner(cycle, tools=self.luna_tools())
        clock = SequenceClock(datetime.fromisoformat("2026-08-14T15:39:59-04:00"), datetime.fromisoformat("2026-08-14T15:40:00-04:00"))
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory, runner, clock); state = initial_state("2026-08-14")
            core.run_luna_cycle(state, self.session, datetime.fromisoformat("2026-08-14T15:39:59-04:00"))
            self.assertEqual(runner.calls, 1); self.assertEqual(state["schedule_events"][0]["status"], "SKIPPED_CUTOFF")

    def test_luna_exactly_at_and_after_cutoff_does_not_start(self):
        for timestamp in ("2026-08-14T15:40:00-04:00", "2026-08-14T15:40:01-04:00"):
            runner = FakeRunner(self.cycle, tools=self.luna_tools())
            with tempfile.TemporaryDirectory() as directory:
                core = self.core(directory, runner, SequenceClock(datetime.fromisoformat(timestamp)))
                with self.assertRaises(PreflightError): core.run_luna_cycle(initial_state("2026-08-14"), self.session, datetime.fromisoformat(timestamp))
                self.assertEqual(runner.calls, 0)

    def test_long_sol_crossing_cutoff_rejects_backdated_plan(self):
        decision = json.loads((ROOT / "tests/fixtures/senior_plan.json").read_text(encoding="utf-8"))
        decision["decision_timestamp"] = "2026-08-14T15:39:30-04:00"
        decision["quote_timestamp"] = "2026-08-14T15:39:00-04:00"
        core = ShadowOrchestrator.__new__(ShadowOrchestrator); core.config = self.config
        with self.assertRaises(PreflightError):
            core._validate_senior(decision, self.cycle["finalists"], initial_state("2026-08-14"), self.session, 1, observed_start=datetime.fromisoformat("2026-08-14T15:39:00-04:00"), observed_end=datetime.fromisoformat("2026-08-14T15:40:00-04:00"))

    def test_senior_trusted_completion_boundary(self):
        decision = json.loads((ROOT / "tests/fixtures/senior_plan.json").read_text(encoding="utf-8")); decision["decision_timestamp"] = "2026-08-14T15:39:55-04:00"; decision["quote_timestamp"] = "2026-08-14T15:39:30-04:00"
        core = ShadowOrchestrator.__new__(ShadowOrchestrator); core.config = self.config
        core._validate_senior(decision, self.cycle["finalists"], initial_state("2026-08-14"), self.session, 1, observed_start=datetime.fromisoformat("2026-08-14T15:39:50-04:00"), observed_end=datetime.fromisoformat("2026-08-14T15:39:59-04:00"))
        for ended in ("2026-08-14T15:40:00-04:00", "2026-08-14T15:40:01-04:00"):
            with self.subTest(ended=ended), self.assertRaises(PreflightError):
                core._validate_senior(decision, self.cycle["finalists"], initial_state("2026-08-14"), self.session, 1, observed_start=datetime.fromisoformat("2026-08-14T15:39:50-04:00"), observed_end=datetime.fromisoformat(ended))

    def test_state_committed_before_cycle_report_crash(self):
        cycle = deepcopy(self.cycle); cycle["finalists"] = []; cycle["sol_escalation"] = False; cycle["timestamp"] = "2026-08-14T10:00:01-04:00"
        cycle["tool_call_count"]["total"] = 5
        runner = FakeRunner(cycle, tools={"get_accounts": 1, "get_portfolio": 1, "get_equity_orders": 1, "get_equity_positions": 1, "run_scan": 1}); clock = SequenceClock(datetime.fromisoformat("2026-08-14T10:00:00-04:00"), datetime.fromisoformat("2026-08-14T10:00:02-04:00"))
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory, runner, clock); state = initial_state("2026-08-14")
            with patch("orchestrator.write_non_destructive_text", side_effect=OSError("crash")), self.assertRaises(OSError): core.run_luna_cycle(state, self.session, datetime.fromisoformat("2026-08-14T10:00:00-04:00"))
            recovered = core.store.load("2026-08-14"); self.assertEqual(len(recovered["cycles"]), 1); self.assertIn("luna:2026-08-14-cycle-1", recovered["operation_ids"])

    def test_persisted_senior_decision_is_idempotent_after_report_crash(self):
        runner = FakeRunner(self.no_trade, web=1); clock = SequenceClock(datetime.fromisoformat("2026-08-14T10:02:59-04:00"), datetime.fromisoformat("2026-08-14T10:03:01-04:00"))
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory, runner, clock); state = initial_state("2026-08-14"); core.store.save(state)
            with patch("orchestrator.write_non_destructive_text", side_effect=OSError("crash")), self.assertRaises(OSError): core.run_senior(state, self.session, self.cycle)
            recovered = core.store.load("2026-08-14"); self.assertEqual(len(recovered["senior_decisions"]), 1)
            core.run_senior(recovered, self.session, self.cycle); self.assertEqual(runner.calls, 1)

    def test_one_senior_process_tracks_primary_and_challenger_plans(self):
        plan = json.loads((ROOT / "tests/fixtures/senior_plan.json").read_text(encoding="utf-8"))
        runner = CandidatePlanRunner(plan)
        cycle = deepcopy(self.cycle)
        challenger = deepcopy(cycle["finalists"][0]); challenger["symbol"] = "ALT"
        cycle["finalists"].append(challenger)
        clock = SequenceClock(*([datetime.fromisoformat("2026-08-14T10:03:00-04:00")] * 8))
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory, runner, clock); state = initial_state("2026-08-14"); core.store.save(state)
            core.run_senior(state, self.session, cycle)
            self.assertEqual(runner.calls, 2)
            self.assertEqual(len(state["senior_decisions"]), 2)
            self.assertEqual([item["research_role"] for item in state["shadow_plans"]], ["PRIMARY", "CHALLENGER"])
            self.assertTrue(all("trailing_outcome" in item for item in state["shadow_plans"]))

    def test_stale_slot_grace(self):
        scheduler = SessionScheduler(self.config["schedule"]); slot = datetime.fromisoformat("2026-08-14T10:00:00-04:00")
        self.assertFalse(scheduler.is_stale(slot, datetime.fromisoformat("2026-08-14T10:00:30-04:00")))
        self.assertTrue(scheduler.is_stale(slot, datetime.fromisoformat("2026-08-14T10:00:31-04:00")))

    def test_scheduler_script_has_dst_independent_wake_and_no_overlap(self):
        text = (ROOT / "scripts/install-scheduler.ps1").read_text(encoding="utf-8")
        self.assertNotIn("ConvertTime", text); self.assertIn("MultipleInstances IgnoreNew", text)
        self.assertIn("LogonType S4U", text); self.assertIn("WakeToRun", text)
        launcher = (ROOT / "scripts/start-shadow.ps1").read_text(encoding="utf-8")
        self.assertIn("wsl.exe", launcher); self.assertIn("~/.venvs/ai-trader", launcher)


if __name__ == "__main__": unittest.main()
