from __future__ import annotations

import json
import subprocess
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator import ShadowOrchestrator
from trader.codex_runner import CodexRunner
from trader.market_calendar import EquityMarketCalendar, ET
from trader.models import CodexRunError, DataUnavailableError, PreflightError, SchemaValidationError
from trader.operations import counts_toward_ai_circuit, prepare, record_ai_failure, safe_failure_diagnostic, start
from trader.safety import load_config, validate_json
from trader.shadow_boundary import ShadowBoundaryResult, APPROVED_SHADOW_ROBINHOOD_TOOLS
from trader.state import initial_state


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class Aug24ReliabilityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 8, 14))

    def setUp(self):
        self.core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        self.core.config = self.config

    @staticmethod
    def fixture(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def data_unavailable_cycle(self):
        cycle = self.fixture("luna_empty.json")
        cycle["errors"] = ["required historical data unavailable"]
        return cycle

    def test_luna_data_unavailable_is_safe_noop_and_cannot_escalate(self):
        cycle = self.data_unavailable_cycle()
        observed = {"get_accounts": 1, "get_equity_orders": 1, "get_equity_positions": 1, "run_scan": 1}
        self.core._validate_luna(
            cycle, initial_state("2026-08-14"), self.session, 0,
            observed_tool_calls=observed,
        )
        self.assertEqual(cycle["finalists"], [])
        self.assertFalse(cycle["sol_escalation"])
        cycle["sol_escalation"] = True
        with self.assertRaisesRegex(SchemaValidationError, "cannot contain"):
            self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)

    def test_luna_data_noop_still_fails_closed_for_security_and_account(self):
        for mutation in ("security", "account"):
            cycle = self.data_unavailable_cycle()
            if mutation == "security":
                cycle["security_status"]["boundary_ok"] = False
            else:
                cycle["account_status"]["agentic_account_count"] = 2
            with self.subTest(mutation=mutation), self.assertRaises(PreflightError):
                self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)

    def test_python_supplies_only_exact_completed_15m_boundaries(self):
        starts = self.core._completed_15m_bucket_starts(
            self.session, datetime(2026, 8, 14, 10, 7, tzinfo=ET)
        )
        self.assertEqual(starts, [
            "2026-08-14T09:30:00-04:00",
            "2026-08-14T09:45:00-04:00",
        ])

    def test_luna_schema_removes_model_owned_15_minute_structure(self):
        cycle = self.fixture("luna_candidate.json")
        cycle["finalists"][0]["completed_15m_structure"] = []
        with self.assertRaisesRegex(SchemaValidationError, "Additional properties"):
            validate_json(cycle, ROOT / "schemas" / "luna-cycle.schema.json")

    def test_invalid_sol_plan_timestamp_remains_rejected(self):
        decision = self.fixture("senior_plan.json")
        decision["time_exit"] = "15:55"
        with self.assertRaisesRegex(SchemaValidationError, "ISO timestamp"):
            validate_json(decision, ROOT / "schemas" / "senior-decision.schema.json")

    def test_approved_read_failure_is_typed_but_foreign_failure_is_not(self):
        runner = CodexRunner(ROOT, self.config)
        runner._shadow_boundary = ShadowBoundaryResult(
            Path("/tmp/config.toml"), "robinhood", APPROVED_SHADOW_ROBINHOOD_TOOLS
        )
        def events(server: str):
            return "\n".join(json.dumps(item) for item in [
                {"type": "thread.started"}, {"type": "turn.started"},
                {"type": "item.started", "item": {"id": "x", "type": "mcp_tool_call", "server": server, "name": "get_equity_historicals"}},
                {"type": "item.completed", "item": {"id": "x", "type": "mcp_tool_call", "server": server, "name": "get_equity_historicals", "status": "failed"}},
                {"type": "turn.completed", "usage": {}},
            ])
        kwargs = dict(
            prompt_path=ROOT / "prompts" / "shadow-monitor.md",
            schema_path=ROOT / "schemas" / "shadow-monitor.schema.json",
            model="gpt-5.6-luna", context={}, required_robinhood_tools=frozenset(),
            robinhood_enabled_tools=frozenset({"get_equity_historicals"}),
        )
        with patch("trader.codex_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, events("robinhood"), "")):
            with self.assertRaises(DataUnavailableError):
                runner.run(**kwargs)
        with patch("trader.codex_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, events("evil"), "")):
            with self.assertRaises(CodexRunError) as raised:
                runner.run(**kwargs)
        self.assertNotIsInstance(raised.exception, DataUnavailableError)

    def test_safe_diagnostics_use_application_codes(self):
        state = initial_state("2026-08-14")
        now = datetime(2026, 8, 14, 10, 0, tzinfo=ET)
        cases = (
            (SchemaValidationError("time_exit must be an ISO timestamp"), "MODEL_SCHEMA_VALIDATION_FAILURE", "SCHEMA_VALIDATION"),
            (CodexRunError("INVALID_MODEL_CONTENT: bad bar"), "INVALID_MODEL_CONTENT", "SEMANTIC_VALIDATION"),
            (DataUnavailableError("Approved read-only data unavailable"), "READ_ONLY_DATA_UNAVAILABLE", "TOOL_EXECUTION"),
        )
        for index, (error, code, stage) in enumerate(cases):
            record = prepare(state, f"x:{index}", "STAGE_B", now, 1)
            start(record, now)
            value = safe_failure_diagnostic(record, error, now, "TERMINAL_FAILED")
            self.assertEqual(value["sanitized_error"]["code"], code)
            self.assertEqual(value["sanitized_error"]["stage_reached"], stage)
            self.assertNotEqual(code, "NO_SAFE_STRUCTURED_CODE_AVAILABLE")

    def test_data_unavailable_does_not_poison_ai_circuit(self):
        state = initial_state("2026-08-14")
        now = datetime(2026, 8, 14, 10, 0, tzinfo=ET)
        unavailable = DataUnavailableError("Approved read-only data unavailable")
        self.assertFalse(counts_toward_ai_circuit(unavailable))
        if counts_toward_ai_circuit(unavailable):
            record_ai_failure(state, unavailable, now)
        self.assertEqual(state["ai_circuit"]["failure_count"], 0)
        security = PreflightError("Luna security boundary failed")
        self.assertTrue(counts_toward_ai_circuit(security))
        record_ai_failure(state, security, now)
        self.assertEqual(state["ai_circuit"]["failure_count"], 1)

    def test_prompt_contracts_keep_read_only_tools_and_exact_timestamp_rules(self):
        luna = (ROOT / "prompts/luna-stage-b.md").read_text(encoding="utf-8")
        sol = (ROOT / "prompts/sol-senior.md").read_text(encoding="utf-8")
        self.assertIn("Python alone derives any 15-minute structure", luna)
        self.assertNotIn("legal_completed_15m_bucket_starts", luna)
        self.assertIn("non-null timezone-aware ISO-8601", sol)
        self.assertFalse(any(name.startswith(("place_", "review_", "cancel_")) for name in APPROVED_SHADOW_ROBINHOOD_TOOLS))


if __name__ == "__main__":
    unittest.main()
