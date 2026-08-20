from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.models import CodexRunError, CodexRunResult
from trader.operations import retry_eligible
from trader.state import StateStore, initial_state


NOW = datetime.fromisoformat("2026-08-20T16:05:00-04:00")


class Clock:
    def __init__(self, value=NOW): self.value = value
    def now(self): return self.value


class Runner:
    def __init__(self, results): self.results = list(results); self.calls = 0
    def run(self, **_kwargs):
        result = self.results[self.calls]; self.calls += 1
        if isinstance(result, Exception): raise result
        return result
    def safe_diagnostics(self): return {}


class EodLifecycleTests(unittest.TestCase):
    def core(self, root: Path, runner: Runner, clock: Clock) -> ShadowOrchestrator:
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = root; core.store = StateStore(root / "state"); core.runner = runner; core.clock = clock
        core.config = {"circuit_breaker": {"consecutive_failures": 99, "total_failures": 99}}
        return core

    def attempt(self, core, state, validator):
        return core._run_ai_job(
            state, operation_id="eod:2026-08-20", operation_type="EOD",
            scheduled_for=NOW, max_attempts=3, result_validator=validator)

    def test_validator_success_is_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = CodexRunResult(data={"accepted": True})
            core = self.core(Path(directory), Runner([result]), Clock()); state = initial_state("2026-08-20")
            self.assertIs(self.attempt(core, state, lambda _result: None), result)
            self.assertEqual(state["ai_operations"][0]["state"], "COMPLETED")
            self.assertEqual(state["usage_counts"]["eod_completed_runs"], 1)

    def test_validator_failure_is_retry_wait_with_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            message = "EOD review requires both SPY and QQQ benchmark closes"
            core = self.core(Path(directory), Runner([CodexRunResult(data={})]), Clock())
            state = initial_state("2026-08-20")
            with self.assertRaisesRegex(CodexRunError, "both SPY and QQQ"):
                self.attempt(core, state, lambda _result: (_ for _ in ()).throw(CodexRunError(message)))
            record = state["ai_operations"][0]
            self.assertEqual(record["state"], "RETRY_WAIT")
            self.assertEqual(state["usage_counts"]["eod_completed_runs"], 0)
            self.assertEqual(state["usage_counts"]["eod_failed_attempts"], 1)
            self.assertEqual(record["failure_diagnostics"][0]["sanitized_error"]["message"], message)

    def test_second_valid_attempt_succeeds_and_total_attempts_cap_at_three(self):
        with tempfile.TemporaryDirectory() as directory:
            message = "EOD review failed data-integrity checks"
            clock = Clock(); core = self.core(Path(directory), Runner([
                CodexRunResult(data={}), CodexRunResult(data={"accepted": True})]), clock)
            state = initial_state("2026-08-20")
            calls = 0
            def validator(result):
                nonlocal calls; calls += 1
                if calls == 1: raise CodexRunError(message)
            with self.assertRaises(CodexRunError): self.attempt(core, state, validator)
            clock.value = datetime.fromisoformat(state["ai_operations"][0]["next_retry_at"])
            self.attempt(core, state, validator)
            self.assertEqual(state["ai_operations"][0]["state"], "COMPLETED")
            self.assertEqual(state["ai_operations"][0]["attempt_number"], 2)
            self.assertEqual(state["usage_counts"]["eod_completed_runs"], 1)
            self.assertEqual(state["usage_counts"]["eod_failed_attempts"], 1)

    def test_three_rejected_attempts_are_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock(); core = self.core(Path(directory), Runner([CodexRunResult(data={})] * 3), clock)
            state = initial_state("2026-08-20")
            def reject(_result): raise CodexRunError("EOD review failed data-integrity checks")
            for attempt in range(3):
                with self.assertRaises(CodexRunError): self.attempt(core, state, reject)
                record = state["ai_operations"][0]
                if attempt < 2: clock.value = datetime.fromisoformat(record["next_retry_at"])
            self.assertEqual(record["state"], "FAILED_TERMINAL")
            self.assertEqual(record["attempt_number"], 3)
            self.assertEqual(state["usage_counts"]["eod_failed_attempts"], 3)
            self.assertEqual(state["usage_counts"]["eod_completed_runs"], 0)

    def test_security_and_tool_boundary_errors_are_nonretryable(self):
        self.assertFalse(retry_eligible(CodexRunError("foreign MCP server observed: evil")))
        self.assertFalse(retry_eligible(CodexRunError("prohibited tool activity")))
        self.assertFalse(retry_eligible(CodexRunError("schema security violation")))

    def test_eod_web_activity_is_nonretryable(self):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        review = {"errors": [], "session_date": "2026-08-20"}
        with self.assertRaisesRegex(CodexRunError, "prohibited tool activity") as raised:
            core._validate_eod_review(review, initial_state("2026-08-20"), 1)
        self.assertFalse(retry_eligible(raised.exception))


if __name__ == "__main__":
    unittest.main()
