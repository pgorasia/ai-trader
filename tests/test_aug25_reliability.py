from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.models import CodexRunError, CodexRunResult, DataUnavailableError, PreflightError, SchemaValidationError
from trader.operations import failure_counts_now, retry_eligible
from trader.state import StateStore, initial_state


NOW = datetime.fromisoformat("2026-08-25T16:05:00-04:00")


class Clock:
    def __init__(self): self.value = NOW
    def now(self): return self.value


class Runner:
    def __init__(self, results): self.results = list(results)
    def run(self, **_kwargs):
        value = self.results.pop(0)
        if isinstance(value, Exception): raise value
        return value
    def safe_diagnostics(self): return {}


class Aug25ReliabilityTests(unittest.TestCase):
    def core(self, root, results):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = root; core.store = StateStore(root / "state")
        core.runner = Runner(results); core.clock = Clock()
        core.config = {"circuit_breaker": {"consecutive_failures": 1, "total_failures": 1}}
        return core

    def attempt(self, core, state, validator=None):
        return core._run_ai_job(
            state, operation_id="eod:2026-08-25", operation_type="EOD",
            scheduled_for=NOW, max_attempts=3, result_validator=validator)

    def test_retryable_eod_failure_is_deferred_from_circuit_until_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(Path(directory), [CodexRunError("EOD review failed data-integrity checks")] * 3)
            state = initial_state("2026-08-25")
            for attempt in range(3):
                with self.assertRaises(CodexRunError): self.attempt(core, state)
                record = state["ai_operations"][0]
                if attempt < 2:
                    self.assertEqual(record["state"], "RETRY_WAIT")
                    self.assertEqual(state["ai_circuit"]["failure_count"], 0)
                    core.clock.value = datetime.fromisoformat(record["next_retry_at"])
            self.assertEqual(record["state"], "FAILED_TERMINAL")
            self.assertEqual(len(record["failure_diagnostics"]), 3)
            self.assertEqual(state["ai_circuit"]["failure_count"], 1)
            self.assertEqual(state["ai_circuit"]["status"], "OPEN")

    def test_preexisting_open_circuit_blocks_new_eod_work(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(Path(directory), [CodexRunResult(data={})])
            state = initial_state("2026-08-25"); state["ai_circuit"]["status"] = "OPEN"
            with self.assertRaisesRegex(PreflightError, "not eligible"):
                self.attempt(core, state)

    def test_eod_read_errors_are_typed_data_unavailable_and_retryable(self):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        review = {"errors": ["historicals unavailable"], "session_date": "2026-08-25"}
        with self.assertRaises(DataUnavailableError) as raised:
            core._validate_eod_review(review, initial_state("2026-08-25"), 0)
        self.assertTrue(retry_eligible(raised.exception))
        self.assertFalse(failure_counts_now("EOD", "RETRY_AT", raised.exception))

    def test_content_and_schema_failures_still_count(self):
        self.assertTrue(failure_counts_now("STAGE_B", "TERMINAL_FAILED", CodexRunError("bad model content")))
        self.assertTrue(failure_counts_now("STAGE_B", "TERMINAL_FAILED", SchemaValidationError("bad schema")))
        self.assertFalse(failure_counts_now("STAGE_B", "TERMINAL_FAILED", DataUnavailableError("read failed")))


if __name__ == "__main__":
    unittest.main()
