from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_runner import CodexRunner, ROBINHOOD_TEARDOWN_CODE
from trader.models import CodexRunError, SchemaValidationError
from trader.shadow_boundary import ShadowBoundaryResult


ROOT = Path(__file__).resolve().parents[1]
WARNING = '''ERROR rmcp::transport::streamable_http_client:
fail to delete session:
unexpected server response:
DELETE returned HTTP 400 session_id="test-session"
'''
PRODUCTION_WARNING = '2026-08-14T19:02:51.222104Z ERROR rmcp::transport::streamable_http_client: fail to delete session: unexpected server response: DELETE returned HTTP 400 session_id="REDACTED"\n'


def preflight_output(*, server="robinhood-trading", terminal="turn.completed", tool_completed=True, missing_field=False, warning_before=False):
    data = {
        "passed": True, "account_classifications": [{"agentic_allowed": True, "brokerage_account_type": "individual", "management_type": "self_directed", "state": "active", "deactivated": False, "permanently_deactivated": False}], "errors": [],
    }
    if missing_field:
        del data["account_classifications"]
    events = [
        {"type": "thread.started", "thread_id": "test"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": server, "tool": "get_accounts"}},
    ]
    if tool_completed:
        events.append({"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call", "server": server, "tool": "get_accounts"}})
    events.append({"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": json.dumps(data)}})
    if terminal:
        events.append({"type": terminal, "usage": {}})
    output = "\n".join(json.dumps(item) for item in events) + "\n"
    return WARNING + output if warning_before else output


class McpTeardownCompatibilityTests(unittest.TestCase):
    def setUp(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner.project_root = ROOT
        runner.config = {}
        runner.executable = "codex"
        runner.child_environment = {}
        runner.timeout_seconds = 10
        runner.transient_retries = 0
        runner.retry_backoff = 0
        runner.version = "test"
        runner._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        runner._shadow_boundary = ShadowBoundaryResult(Path("/tmp/config.toml"), "robinhood-trading", frozenset({"get_accounts"}))
        self.runner = runner

    def run_case(self, stdout, stderr=WARNING, returncode=1):
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        def execute(command, **_kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            message = json.loads(next(
                event for event in stdout.splitlines()
                if event.startswith("{") and json.loads(event).get("type") == "item.completed"
                and json.loads(event).get("item", {}).get("type") == "agent_message"
            ))["item"]["text"]
            output_path.write_text(message, encoding="utf-8")
            return completed
        with patch("trader.codex_runner.subprocess.run", side_effect=execute):
            return self.runner.run(
                prompt_path=ROOT / "prompts" / "preflight.md",
                schema_path=ROOT / "schemas" / "preflight.schema.json",
                model="test", context={}, required_robinhood_tools=frozenset({"get_accounts"}),
            )

    def assert_fails(self, *args, **kwargs):
        with self.assertRaises((CodexRunError, SchemaValidationError)):
            self.run_case(*args, **kwargs)

    def test_nonzero_exact_post_completion_warning_is_accepted(self):
        result = self.run_case(preflight_output())
        self.assertTrue(result.diagnostics["mcp_teardown_warning"])
        self.assertEqual(result.diagnostics["diagnostic_codes"], [ROBINHOOD_TEARDOWN_CODE])

    def test_exact_production_single_line_warning_is_accepted(self):
        result = self.run_case(preflight_output(), stderr=PRODUCTION_WARNING)
        self.assertTrue(result.diagnostics["mcp_teardown_warning"])
        self.assertEqual(result.diagnostics["diagnostic_codes"], [ROBINHOOD_TEARDOWN_CODE])

    def test_harmless_line_wrapping_is_accepted(self):
        wrapped = (
            "2026-08-14T19:02:51.222104Z ERROR rmcp::transport::streamable_http_client: fail to\n"
            " delete session: unexpected server response: DELETE returned HTTP 400\n"
            ' session_id="wrapped-session"\n'
        )
        self.assertTrue(self.run_case(preflight_output(), stderr=wrapped).diagnostics["mcp_teardown_warning"])

    def test_timestamp_log_prefix_variations_are_accepted(self):
        messages = (
            'ERROR rmcp::transport::streamable_http_client: fail to delete session: unexpected server response: DELETE returned HTTP 400 session_id="no-timestamp"\n',
            '2026-08-14T19:02:51Z ERROR rmcp::transport::streamable_http_client: fail to delete session: unexpected server response: DELETE returned HTTP 400 session_id="whole-seconds"\n',
            '2026-08-14T19:02:51.9+00:00 ERROR rmcp::transport::streamable_http_client: fail to delete session: unexpected server response: DELETE returned HTTP 400 session_id="offset"\n',
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(self.run_case(preflight_output(), stderr=message).diagnostics["mcp_teardown_warning"])

    def test_session_id_variations_are_accepted(self):
        for session_id in ('"alpha-123_opaque"', 'unquoted.opaque:123'):
            with self.subTest(session_id=session_id):
                warning = WARNING.replace('"test-session"', session_id)
                self.assertTrue(self.run_case(preflight_output(), stderr=warning).diagnostics["mcp_teardown_warning"])

    def test_zero_exact_post_completion_warning_is_accepted(self):
        self.assertTrue(self.run_case(preflight_output(), returncode=0).diagnostics["mcp_teardown_warning"])

    def test_no_turn_completed_fails(self):
        self.assert_fails(preflight_output(terminal=None))

    def test_turn_failed_fails(self):
        self.assert_fails(preflight_output(terminal="turn.failed"))

    def test_structured_error_fails(self):
        self.assert_fails(preflight_output(terminal="error"))

    def test_unrelated_stderr_fails(self):
        self.assert_fails(preflight_output(), stderr=WARNING + "ERROR unrelated\n")

    def test_warning_before_structured_completion_fails(self):
        self.assert_fails(preflight_output(warning_before=True))

    def test_another_mcp_server_fails(self):
        self.assert_fails(preflight_output(server="other-server"))

    def test_other_http_statuses_fail(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                self.assert_fails(preflight_output(), stderr=WARNING.replace("400", str(status)))

    def test_generic_http_400_fails(self):
        self.assert_fails(preflight_output(), stderr="ERROR request returned HTTP 400\n")

    def test_started_tool_without_completion_fails(self):
        self.assert_fails(preflight_output(tool_completed=False))

    def test_truncated_jsonl_fails(self):
        self.assert_fails(preflight_output() + '{"type":')

    def test_multiple_turn_completed_fails(self):
        extra = json.dumps({"type": "turn.completed", "usage": {}}) + "\n"
        self.assert_fails(preflight_output() + extra)

    def test_prohibited_tool_fails(self):
        output = preflight_output().replace("get_accounts", "review_equity_order")
        self.assert_fails(output)

    def test_schema_required_field_missing_fails(self):
        self.assert_fails(preflight_output(missing_field=True))

    def test_ordinary_nonzero_exit_still_fails(self):
        self.assert_fails(preflight_output(), stderr="")


if __name__ == "__main__":
    unittest.main()
