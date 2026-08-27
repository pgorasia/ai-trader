from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.job_contracts import validate_job_contracts
from trader.market_calendar import ET, EquityMarketCalendar
from trader.models import CodexRunError, CodexRunResult, DataUnavailableError, PreflightError, ResearchUnavailableError, SchemaValidationError
from trader.operations import counts_toward_ai_circuit, safe_failure_diagnostic, prepare, start
from trader.safety import load_config, validate_json
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime.fromisoformat("2026-08-27T11:00:00-04:00")


class Clock:
    def now(self): return NOW


class Runner:
    def __init__(self, result): self.result = result
    def run(self, **_kwargs): return self.result
    def safe_diagnostics(self): return {}


class Aug27ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 8, 27))

    def setUp(self):
        self.core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        self.core.config = self.config

    @staticmethod
    def fixture(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def unavailable(self, code="REQUIRED_RESEARCH_UNAVAILABLE"):
        decision = self.fixture("senior_no_trade.json")
        decision["decision_timestamp"] = "2026-08-27T11:00:00-04:00"
        decision["errors"] = [code]
        return decision

    def finalists(self):
        return self.fixture("luna_candidate.json")["finalists"]

    def test_legitimate_research_unavailable_is_typed_and_cannot_plan(self):
        with self.assertRaises(ResearchUnavailableError) as raised:
            self.core._validate_senior(self.unavailable(), self.finalists(), initial_state("2026-08-27"), self.session, 0)
        self.assertFalse(counts_toward_ai_circuit(raised.exception))
        plan = self.unavailable(); plan["decision"] = "SHADOW_TRADE_PLAN"; plan["symbol"] = "TEST"
        with self.assertRaises(SchemaValidationError):
            self.core._validate_senior(plan, self.finalists(), initial_state("2026-08-27"), self.session, 0)

    def test_all_availability_codes_have_bounded_distinct_diagnostics(self):
        for code, error_type in (
            ("REQUIRED_RESEARCH_UNAVAILABLE", ResearchUnavailableError),
            ("READ_ONLY_TOOL_DATA_UNAVAILABLE", DataUnavailableError),
            ("OAUTH_MCP_AVAILABILITY_FAILURE", ResearchUnavailableError),
        ):
            with self.subTest(code=code), self.assertRaises(error_type) as raised:
                self.core._validate_senior(self.unavailable(code), self.finalists(), initial_state("2026-08-27"), self.session, 0)
            state = initial_state("2026-08-27"); record = prepare(state, code, "SOL", NOW, 1); start(record, NOW)
            diagnostic = safe_failure_diagnostic(record, raised.exception, NOW, "TERMINAL_FAILED")
            self.assertEqual(diagnostic["sanitized_error"]["code"], code)
            self.assertNotIn("http", diagnostic["sanitized_error"]["message"].lower())

    def test_malformed_sol_content_counts_and_security_failures_remain_hard(self):
        malformed = self.unavailable(); malformed["evaluated_symbols"] = []
        with self.assertRaises(SchemaValidationError) as raised:
            self.core._validate_senior(malformed, self.finalists(), initial_state("2026-08-27"), self.session, 0)
        self.assertTrue(counts_toward_ai_circuit(raised.exception))
        for hard in (PreflightError("account reconciliation failed"), CodexRunError("foreign MCP"), CodexRunError("prohibited tool")):
            self.assertTrue(counts_toward_ai_circuit(hard))

    def test_research_unavailable_operation_does_not_increment_circuit_or_persist_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "state").mkdir()
            core = ShadowOrchestrator.__new__(ShadowOrchestrator)
            core.root = root; core.store = StateStore(root / "state"); core.clock = Clock()
            core.config = {"circuit_breaker": {"consecutive_failures": 1, "total_failures": 1}}
            core.runner = Runner(CodexRunResult(data=self.unavailable()))
            state = initial_state("2026-08-27")
            def validator(result):
                self.core._validate_senior(result.data, self.finalists(), state, self.session, 0)
            with self.assertRaises(ResearchUnavailableError):
                core._run_ai_job(state, operation_id="senior:test", operation_type="SOL", scheduled_for=NOW, result_validator=validator)
            self.assertEqual(state["ai_circuit"]["failure_count"], 0)
            self.assertEqual(state["shadow_plans"], [])

    def test_python_supplies_latest_eight_and_only_allowed_buckets_validate(self):
        starts = self.core._completed_15m_bucket_starts(self.session, datetime(2026, 8, 27, 12, 0, tzinfo=ET))
        self.assertEqual(len(starts), 8)
        self.assertEqual(starts[0], "2026-08-27T10:00:00-04:00")
        cycle = self.fixture("luna_candidate.json")
        cycle["timestamp"] = "2026-08-27T12:00:00-04:00"; cycle["session_date"] = "2026-08-27"
        cycle["finalists"][0]["completed_15m_structure"] = [
            {"timestamp": starts[0], "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "complete": True}
        ]
        self.core._validate_luna(cycle, initial_state("2026-08-27"), self.session, 0,
                                 observed_start=datetime(2026, 8, 27, 12, 0, tzinfo=ET))
        cycle["finalists"][0]["completed_15m_structure"][0]["timestamp"] = "2026-08-27T09:45:00-04:00"
        with self.assertRaisesRegex(SchemaValidationError, "allowed set"):
            self.core._validate_luna(cycle, initial_state("2026-08-27"), self.session, 0,
                                     observed_start=datetime(2026, 8, 27, 12, 0, tzinfo=ET))

    def test_schema_rejects_more_than_eight_even_when_older_buckets_are_complete(self):
        cycle = self.fixture("luna_candidate.json")
        starts = [f"2026-08-27T{hour:02d}:{minute:02d}:00-04:00" for hour, minute in
                  ((9, 30), (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30))]
        cycle["finalists"][0]["completed_15m_structure"] = [
            {"timestamp": stamp, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "complete": True}
            for stamp in starts
        ]
        with self.assertRaises(SchemaValidationError):
            validate_json(cycle, ROOT / "schemas" / "luna-cycle.schema.json")

    def test_tool_boundary_remains_exactly_read_only(self):
        self.assertEqual(len(APPROVED_SHADOW_ROBINHOOD_TOOLS), 22)
        self.assertEqual(validate_job_contracts(), [])
        write_prefixes = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_")
        self.assertEqual([tool for tool in APPROVED_SHADOW_ROBINHOOD_TOOLS if tool.startswith(write_prefixes)], [])


if __name__ == "__main__":
    unittest.main()
