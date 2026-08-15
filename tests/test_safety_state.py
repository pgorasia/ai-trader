from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_runner import CodexRunner
from trader.codex_events import parse_codex_jsonl
from trader.models import CodexRunError, CodexTimeoutError, PreflightError, SchemaValidationError, StateCorruptionError
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

    def test_model_policy_is_pinned(self):
        import yaml
        from trader.models import ConfigurationError
        mutations = {"luna": "gpt-5.6-sol", "sol": "gpt-5.6-luna", "sol_reasoning_effort": "medium"}
        for key, value in mutations.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                changed = dict(self.config)
                changed["models"] = dict(self.config["models"], **{key: value})
                path = Path(directory) / "strategy.yaml"
                path.write_text(yaml.safe_dump(changed), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_recorded_luna_empty_scanner_and_candidate_fixtures(self):
        validate_json(self.fixture("luna_empty.json"), ROOT / "schemas" / "luna-cycle.schema.json")
        validate_json(self.fixture("luna_candidate.json"), ROOT / "schemas" / "luna-cycle.schema.json")

    def test_luna_rejects_nonfinite_returns_and_impossible_ohlc(self):
        candidate = self.fixture("luna_candidate.json")
        candidate["finalists"][0]["intraday_return"] = float("nan")
        with self.assertRaises(SchemaValidationError):
            validate_json(candidate, ROOT / "schemas" / "luna-cycle.schema.json")
        candidate = self.fixture("luna_candidate.json")
        candidate["finalists"][0]["completed_15m_structure"] = [{"timestamp": "2026-08-14T09:30:00-04:00", "open": 10.0, "high": 9.9, "low": 9.8, "close": 10.0, "volume": 100, "complete": True}]
        with self.assertRaises(SchemaValidationError):
            validate_json(candidate, ROOT / "schemas" / "luna-cycle.schema.json")

    def test_recorded_senior_fixtures(self):
        validate_json(self.fixture("senior_no_trade.json"), ROOT / "schemas" / "senior-decision.schema.json")
        validate_json(self.fixture("senior_plan.json"), ROOT / "schemas" / "senior-decision.schema.json")

    def test_malformed_json_response_is_rejected(self):
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl("not-json")
        malformed = self.fixture("luna_empty.json")
        del malformed["sol_escalation"]
        with self.assertRaises(SchemaValidationError):
            validate_json(malformed, ROOT / "schemas" / "luna-cycle.schema.json")

    def test_preflight_mcp_unavailable(self):
        result = self.good_preflight()
        result["boundary_status"] = "FAIL"
        with self.assertRaises(PreflightError):
            enforce_preflight_result(result)

    def test_preflight_unexpected_account_position(self):
        result = self.good_preflight()
        result["positions_job"]["unexpected_position_count"] = 1
        with self.assertRaisesRegex(PreflightError, "position"):
            enforce_preflight_result(result)

    def test_preflight_unexpected_order(self):
        result = self.good_preflight()
        result["orders_job"]["unexpected_order_count"] = 1
        with self.assertRaisesRegex(PreflightError, "order"):
            enforce_preflight_result(result)

    def test_preflight_forbidden_tool(self):
        result = self.good_preflight()
        result["orders_job"]["status"] = "FAIL"
        with self.assertRaises(PreflightError):
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
        from trader.shadow_boundary import ShadowBoundaryResult, REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS
        runner._shadow_boundary = ShadowBoundaryResult(Path("/tmp/config.toml"), "robinhood", REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS)
        with patch("trader.codex_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)) as mocked:
            with self.assertRaises(CodexTimeoutError):
                runner.run(
                    prompt_path=ROOT / "prompts" / "preflight.md",
                    schema_path=ROOT / "schemas" / "preflight.schema.json",
                    model="gpt-5.6-luna",
                    context={},
                    required_robinhood_tools=frozenset({"get_accounts"}),
                )
        self.assertEqual(mocked.call_count, 1)

    def test_cli_commands_enforce_model_web_and_read_only_boundaries(self):
        runner = CodexRunner(ROOT, self.config)
        luna = runner.build_command("gpt-5.6-luna", ROOT / "schemas" / "luna-cycle.schema.json", Path("/tmp/luna-final.json"), allow_web=False)
        sol = runner.build_command("gpt-5.6-sol", ROOT / "schemas" / "senior-decision.schema.json", Path("/tmp/sol-final.json"), allow_web=True, reasoning_effort="high")
        self.assertIn("read-only", luna)
        self.assertIn("--output-last-message", luna)
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
        self.assertIn("shell_tool", luna)
        self.assertIn("shell_tool", sol)

    @staticmethod
    def good_preflight():
        return {
            "boundary_status": "PASS",
            "identity_job": {"status": "PASS"},
            "portfolio_job": {"status": "PASS"},
            "positions_job": {"status": "PASS", "unexpected_position_count": 0},
            "orders_job": {"status": "PASS", "unexpected_order_count": 0},
        }


if __name__ == "__main__":
    unittest.main()
