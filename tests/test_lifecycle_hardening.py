from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator import ROOT, ShadowOrchestrator
from trader.market_calendar import EquityMarketCalendar
from trader.models import PreflightError
from trader.safety import load_config
from trader.state import initial_state


class LifecycleHardeningTests(unittest.TestCase):
    def bare(self) -> ShadowOrchestrator:
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = ROOT
        core.config = load_config(ROOT / "config/strategy.yaml")
        core.calendar = EquityMarketCalendar(
            core.config["exchange_calendar"],
            int(core.config["schedule"]["eod_offset_minutes"]),
        )
        core.store = Mock()
        core.monitor = Mock()
        core.clock = Mock()
        return core

    def test_result_validator_failure_never_leaves_ai_operation_started(self):
        core = self.bare()
        session = core.calendar.session_for(date(2026, 8, 19))
        self.assertIsNotNone(session)
        now = session.market_open
        core.clock.now.return_value = now
        core.runner = Mock()
        core.runner.run.return_value = SimpleNamespace()
        core.runner.safe_diagnostics.return_value = {}
        state = initial_state(now.date().isoformat(), core.config["timezone"], now)

        def reject(_observed):
            raise PreflightError("synthetic semantic rejection")

        with self.assertRaisesRegex(PreflightError, "synthetic semantic rejection"):
            core._run_ai_job(
                state,
                operation_id="stage_b:synthetic",
                operation_type="STAGE_B",
                scheduled_for=now,
                result_validator=reject,
            )

        self.assertEqual(state["ai_operations"][0]["state"], "FAILED_TERMINAL")

    def test_senior_semantics_are_passed_as_precommit_validator(self):
        core = self.bare()
        session = core.calendar.session_for(date(2026, 8, 19))
        self.assertIsNotNone(session)
        core.clock.now.return_value = session.market_open + timedelta(hours=1)
        state = initial_state(session.session_date, core.config["timezone"], session.market_open)
        cycle = {
            "cycle_id": "lifecycle-test-cycle",
            "timestamp": (session.market_open + timedelta(minutes=30)).isoformat(),
            "finalists": [{"symbol": "TEST", "classification": "NEW"}],
        }
        core._run_ai_job = Mock(side_effect=RuntimeError("capture"))
        with self.assertRaisesRegex(RuntimeError, "capture"):
            core.run_senior(state, session, cycle)

        call = core._run_ai_job.call_args.kwargs
        self.assertIn("result_validator", call)
        core._validate_senior = Mock()
        call["result_validator"](
            SimpleNamespace(
                data={"decision": "NO_TRADE"},
                web_searches=1,
                tool_calls={},
            )
        )
        core._validate_senior.assert_called_once()

    def test_monitor_errors_are_rejected_before_operation_completion(self):
        core = self.bare()
        session = core.calendar.session_for(date(2026, 8, 19))
        self.assertIsNotNone(session)
        now = session.market_open + timedelta(hours=1)
        core.clock.now.return_value = now
        state = initial_state(session.session_date, core.config["timezone"], session.market_open)
        state["shadow_plans"] = [{
            "plan_id": "plan-1",
            "original_plan": {
                "symbol": "TEST",
                "decision_timestamp": (session.market_open + timedelta(minutes=20)).isoformat(),
            },
            "outcome": {"status": "PENDING"},
            "trailing_outcome": {"status": "PENDING"},
        }]
        core._run_ai_job = Mock(side_effect=RuntimeError("capture"))
        with self.assertRaisesRegex(RuntimeError, "capture"):
            core.monitor_active_plans(state, now)

        validator = core._run_ai_job.call_args.kwargs["result_validator"]
        with self.assertRaisesRegex(Exception, "Shadow monitor returned"):
            validator(SimpleNamespace(
                data={"errors": ["synthetic read failure"], "symbol_bars": {}},
                web_searches=0,
                tool_calls={},
            ))

    def test_completed_eod_wins_over_later_open_circuit(self):
        core = self.bare()
        session = core.calendar.session_for(date(2026, 8, 19))
        self.assertIsNotNone(session)
        core.clock.now.return_value = session.eod_time + timedelta(minutes=1)
        state = initial_state(session.session_date, core.config["timezone"], session.market_open)
        operation_id = f"eod:{session.session_date}"
        original_review = {"session_date": session.session_date, "status": "COMPLETED"}
        state["ai_operations"].append({
            "operation_id": operation_id,
            "operation_type": "EOD",
            "scheduled_for": session.eod_time.isoformat(),
            "state": "COMPLETED",
            "attempt_number": 1,
            "max_attempts": 3,
            "next_retry_at": None,
            "started_at": session.eod_time.isoformat(),
            "completed_at": session.eod_time.isoformat(),
            "failure_diagnostics": [],
        })
        state["operation_ids"].append(operation_id)
        state["eod_completed"] = True
        state["eod_review"] = original_review
        state["ai_circuit"]["status"] = "OPEN"
        core.store.all_states.return_value = [state]

        with patch.object(
            core,
            "_finalize_eod_without_ai",
            side_effect=AssertionError("completed EOD must not be replaced"),
        ) as fallback, patch(
            "orchestrator.calculate_readiness", return_value={"status": "TEST"}
        ), patch(
            "orchestrator.write_json_companion"
        ), patch(
            "orchestrator.write_non_destructive_text"
        ), patch(
            "orchestrator.eod_markdown", return_value="synthetic"
        ), patch.object(
            core, "_write_experiment_report"
        ):
            result = core.eod(state, session)

        fallback.assert_not_called()
        self.assertIs(state["eod_review"], original_review)
        self.assertIs(result["agent_review"], original_review)


if __name__ == "__main__":
    unittest.main()
