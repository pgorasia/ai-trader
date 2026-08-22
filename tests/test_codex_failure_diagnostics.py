from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_events import parse_codex_jsonl
from trader.codex_runner import CodexRunner
from trader.models import CodexRunError
from trader.operations import retry_eligible
from trader.shadow_boundary import ShadowBoundaryResult


ROOT = Path(__file__).resolve().parents[1]


def line(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def failed_stream(*, trailing=True):
    events = [
        {"type": "thread.started", "thread_id": "opaque-thread"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "opaque-1", "server": "Robinhood-Trading", "tool": "get_accounts", "arguments": {"account_number": "999"}}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "opaque-1", "server": "Robinhood-Trading", "tool": "get_accounts", "result": {"balances": [100], "positions": ["SECRET"]}}},
        {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "opaque-2", "server": "Robinhood-Trading", "tool": "get_equity_orders", "arguments": {"authorization": "Bearer hidden"}}},
        {"type": "error", "error": {"message": "request failed session_id=abc Bearer token123", "code": "transport_error", "account_number": "123456", "additionalDetails": {"positions": ["SECRET"]}}, "codexErrorInfo": {"type": "McpError"}, "httpStatusCode": 500},
    ]
    if trailing:
        events.extend([
            {"type": "item.completed", "item": {"type": "agent_message", "id": "a1", "text": json.dumps({"passed": True, "account_number": "SECRET"})}},
            {"type": "turn.completed", "usage": {}},
        ])
    return line(*events)


class CodexFailureDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def bare_runner():
        runner = CodexRunner.__new__(CodexRunner)
        runner._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        return runner

    def diagnostics(self, *, returncode=17):
        with self.assertRaises(CodexRunError) as caught:
            parse_codex_jsonl(failed_stream(), returncode=returncode)
        return caught.exception.diagnostics

    def test_structured_error_is_sanitized_and_still_fails(self):
        diagnostics = self.diagnostics()
        error = diagnostics["structured_error"]
        self.assertEqual(error["codex_error_type"], "McpError")
        self.assertEqual(error["http_status"], 500)
        self.assertEqual(error["code"], "transport_error")
        self.assertIn("session_id=<redacted>", error["message"])
        self.assertIn("Bearer <redacted>", error["message"])
        self.assertNotIn("abc", json.dumps(diagnostics))
        self.assertNotIn("token123", json.dumps(diagnostics))

    def test_usage_limit_has_stable_nonretryable_code(self):
        message = (
            "You've hit your usage limit. Upgrade to Pro, purchase more credits "
            "or try again at Aug 27th, 2026 5:16 PM."
        )
        stream = line(
            {"type": "thread.started", "thread_id": "opaque-thread"},
            {"type": "turn.started"},
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
        )
        with self.assertRaisesRegex(CodexRunError, r"^CODEX_USAGE_LIMIT:") as caught:
            parse_codex_jsonl(stream, returncode=1)
        self.assertEqual(
            caught.exception.diagnostics["structured_error"]["code"],
            "CODEX_USAGE_LIMIT",
        )
        self.assertFalse(retry_eligible(caught.exception))

    def test_sensitive_fields_and_tool_bodies_are_never_persisted(self):
        encoded = json.dumps(self.diagnostics())
        for forbidden in ('"account_number"', '"balances"', '"positions"', '"arguments"', '"result"', "123456", "SECRET"):
            self.assertNotIn(forbidden, encoded)

    def test_tool_status_order_and_hashed_ids(self):
        diagnostics = self.diagnostics()
        self.assertEqual(diagnostics["expected_tools"]["get_accounts"], "COMPLETED")
        self.assertEqual(diagnostics["expected_tools"]["get_equity_orders"], "STARTED")
        self.assertEqual(diagnostics["expected_tools"]["get_equity_positions"], "NOT_OBSERVED")
        sequence = diagnostics["event_sequence"]
        self.assertEqual([entry["sequence"] for entry in sequence], sorted(entry["sequence"] for entry in sequence))
        tool_events = [entry for entry in sequence if entry["event"].startswith("tool.")]
        self.assertEqual(tool_events[0]["item_id"], tool_events[1]["item_id"])
        self.assertNotIn("opaque-1", json.dumps(sequence))

    def test_return_code_and_failure_stages_retained(self):
        diagnostics = self.diagnostics(returncode=23)
        self.assertEqual(diagnostics["process_return_code"], 23)
        self.assertTrue(diagnostics["jsonl_parse_started"])
        self.assertTrue(diagnostics["jsonl_parse_completed"])
        self.assertEqual(diagnostics["structured_error_count"], 1)
        self.assertFalse(diagnostics["required_tool_validation_reached"])
        self.assertFalse(diagnostics["schema_validation_reached"])
        self.assertFalse(diagnostics["semantic_validation_reached"])

    def test_later_completion_and_final_output_do_not_override_error(self):
        diagnostics = self.diagnostics()
        self.assertTrue(diagnostics["turn_completed"])
        self.assertEqual(diagnostics["agent_message_count"], 1)
        self.assertEqual(diagnostics["structured_error_count"], 1)

    def test_runner_exposes_failure_diagnostics_but_success_has_none(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner.project_root = ROOT
        runner.executable = "codex"
        runner.child_environment = {}
        runner.timeout_seconds = 10
        runner.transient_retries = 0
        runner.retry_backoff = 0
        runner.version = "test"
        runner._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        runner._shadow_boundary = ShadowBoundaryResult(Path("/tmp/config.toml"), "robinhood-trading", frozenset({"get_accounts"}))
        completed = subprocess.CompletedProcess([], 17, stdout=failed_stream(), stderr="")
        with patch("trader.codex_runner.subprocess.run", return_value=completed):
            with self.assertRaises(CodexRunError):
                runner.run(prompt_path=ROOT / "prompts/preflight.md", schema_path=ROOT / "schemas/preflight.schema.json", model="test", context={}, required_robinhood_tools=frozenset({"get_accounts"}))
        self.assertIn("codex_failure_diagnostics", runner.safe_diagnostics())
        runner._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        self.assertNotIn("codex_failure_diagnostics", runner.safe_diagnostics())

    def test_unstructured_runner_error_is_redacted(self):
        message = CodexRunner._safe_error("failure session_id=opaque-secret account_number=998877 user@example.com", "")
        self.assertNotIn("opaque-secret", message)
        self.assertNotIn("998877", message)
        self.assertNotIn("user@example.com", message)
        self.assertGreaterEqual(message.count("<redacted>"), 3)

    def test_post_parse_foreign_mcp_diagnostic_retains_only_safe_identity_and_counts(self):
        runner = self.bare_runner()
        with self.assertRaises(CodexRunError) as caught:
            runner._raise_observed_tool_error(
                "Unexpected MCP activity from a non-Robinhood server",
                {"foreign-server::read_file": 2, "robinhood-trading::get_equity_historicals": 1},
                foreign_mcp=["foreign-server::read_file"],
            )
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["foreign_mcp"], ["foreign-server::read_file"])
        self.assertEqual(diagnostics["observed_tool_summary"], [
            {"name": "foreign-server::read_file", "count": 2},
            {"name": "robinhood-trading::get_equity_historicals", "count": 1},
        ])
        encoded = json.dumps(diagnostics)
        self.assertNotIn("arguments", encoded); self.assertNotIn("result", encoded)

    def test_post_parse_missing_tool_diagnostic_retains_missing_names_and_counts(self):
        runner = self.bare_runner()
        with self.assertRaises(CodexRunError) as caught:
            runner._raise_observed_tool_error(
                "Required Robinhood tool calls were not observed: get_portfolio",
                {"robinhood-trading::get_accounts": 1},
                missing_required_tools=["get_portfolio"],
            )
        self.assertEqual(caught.exception.diagnostics["missing_required_tools"], ["get_portfolio"])
        self.assertEqual(caught.exception.diagnostics["observed_tool_summary"], [
            {"name": "robinhood-trading::get_accounts", "count": 1},
        ])


if __name__ == "__main__":
    unittest.main()
