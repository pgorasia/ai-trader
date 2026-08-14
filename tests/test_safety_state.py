from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_runner import CodexRunner
from trader.models import CodexTimeoutError, PreflightError, SchemaValidationError, StateCorruptionError
from trader.safety import enforce_preflight_result, load_config, offline_preflight, validate_json
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class SafetySchemaStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")

    def fixture(self, name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_required_files_and_all_schemas(self):
        offline_preflight(ROOT, self.config)

    def test_recorded_luna_empty_scanner_and_candidate_fixtures(self):
        validate_json(self.fixture("luna_empty.json"), ROOT / "schemas" / "luna-cycle.schema.json")
        validate_json(self.fixture("luna_candidate.json"), ROOT / "schemas" / "luna-cycle.schema.json")

    def test_recorded_senior_fixtures(self):
        validate_json(self.fixture("senior_no_trade.json"), ROOT / "schemas" / "senior-decision.schema.json")
        validate_json(self.fixture("senior_plan.json"), ROOT / "schemas" / "senior-decision.schema.json")

    def test_malformed_json_response_is_rejected(self):
        with self.assertRaises(SchemaValidationError):
            CodexRunner._extract_final_json([], "not-json")
        malformed = self.fixture("luna_empty.json")
        del malformed["sol_escalation"]
        with self.assertRaises(SchemaValidationError):
            validate_json(malformed, ROOT / "schemas" / "luna-cycle.schema.json")

    def test_preflight_mcp_unavailable(self):
        result = self.good_preflight()
        result["robinhood_mcp_available"] = False
        with self.assertRaises(PreflightError):
            enforce_preflight_result(result)

    def test_preflight_unexpected_account_position(self):
        result = self.good_preflight()
        result["unexpected_equity_positions"] = [{"symbol": "TEST", "quantity": 1}]
        with self.assertRaisesRegex(PreflightError, "position"):
            enforce_preflight_result(result)

    def test_preflight_unexpected_order(self):
        result = self.good_preflight()
        result["unexpected_equity_orders"] = [{"symbol": "TEST", "state": "open", "side": "buy"}]
        with self.assertRaisesRegex(PreflightError, "order"):
            enforce_preflight_result(result)

    def test_preflight_forbidden_tool(self):
        result = self.good_preflight()
        result["available_robinhood_tools"].append("mcp__robinhood__review_equity_order")
        with self.assertRaisesRegex(PreflightError, "Forbidden"):
            enforce_preflight_result(result)

    def test_state_round_trip_is_atomic_and_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            state = initial_state("2026-08-14")
            state["cycles"].append({"cycle_id": "one"})
            path = store.save(state)
            self.assertEqual(store.load("2026-08-14")["cycles"][0]["cycle_id"], "one")
            path.write_text("{partial", encoding="utf-8")
            with self.assertRaises(StateCorruptionError):
                store.load("2026-08-14")

    def test_codex_timeout_is_not_retried(self):
        runner = CodexRunner(ROOT, self.config)
        with patch("trader.codex_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)) as mocked:
            with self.assertRaises(CodexTimeoutError):
                runner.run(
                    prompt_path=ROOT / "prompts" / "preflight.md",
                    schema_path=ROOT / "schemas" / "preflight.schema.json",
                    model="gpt-5.6-luna",
                    context={},
                )
        self.assertEqual(mocked.call_count, 1)

    def test_mcp_list_without_robinhood_is_unavailable(self):
        runner = CodexRunner(ROOT, self.config)
        completed = subprocess.CompletedProcess([], 0, stdout='[{"name":"other","enabled":true}]', stderr="")
        with patch("trader.codex_runner.subprocess.run", return_value=completed):
            self.assertFalse(runner.mcp_server_configured())

    def test_cli_commands_enforce_model_web_and_read_only_boundaries(self):
        runner = CodexRunner(ROOT, self.config)
        luna = runner.build_command("gpt-5.6-luna", ROOT / "schemas" / "luna-cycle.schema.json", allow_web=False)
        sol = runner.build_command("gpt-5.6-sol", ROOT / "schemas" / "senior-decision.schema.json", allow_web=True, reasoning_effort="high")
        self.assertIn("read-only", luna)
        self.assertNotIn("--search", luna)
        self.assertIn("--search", sol)
        self.assertLess(sol.index("--search"), sol.index("exec"))
        self.assertIn('model_reasoning_effort="high"', sol)
        joined = " ".join(luna + sol)
        self.assertNotIn("danger-full-access", joined)
        self.assertNotIn("bypass", joined)
        self.assertNotIn("yolo", joined)
        self.assertIn("multi_agent", luna)
        self.assertIn("multi_agent_v2", luna)

    @staticmethod
    def good_preflight():
        return {
            "timestamp": "2026-08-14T09:35:00-04:00",
            "robinhood_mcp_available": True,
            "available_robinhood_tools": ["get_accounts", "get_equity_positions", "get_equity_orders"],
            "forbidden_tools_available": [],
            "agentic_account_count": 1,
            "account_reconciled": True,
            "account_equity": 100.0,
            "buying_power": 100.0,
            "unexpected_equity_positions": [],
            "unexpected_equity_orders": [],
            "tool_call_count": 4,
            "errors": [],
        }


if __name__ == "__main__":
    unittest.main()
