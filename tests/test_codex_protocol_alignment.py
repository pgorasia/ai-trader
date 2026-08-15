from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_events import CODEX_EVENT_PROTOCOL, parse_codex_jsonl
from trader.codex_runner import CodexRunner
from trader.models import CodexRunError, SchemaValidationError
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, ShadowBoundaryResult


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders")


def encode(events: list[dict]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def lifecycle(*, agent_messages: int = 1, completed_identity: bool = False, extra: list[dict] | None = None, terminal: str = "turn.completed") -> str:
    events = [{"type": "thread.started"}, {"type": "turn.started"}]
    for index, tool in enumerate(REQUIRED):
        started = {"id": f"m{index}", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": tool}
        completed = dict(started) if completed_identity else {"id": f"m{index}", "type": "mcp_tool_call"}
        events.extend([{"type": "item.started", "item": started}, {"type": "item.completed", "item": completed}])
    events.extend(extra or [])
    for index in range(agent_messages):
        events.append({"type": "item.completed", "item": {"id": f"a{index}", "type": "agent_message", "text": json.dumps({"progress": index})}})
    events.append({"type": terminal})
    return encode(events)


def good_final() -> dict:
    return {
        "passed": True,
        "account_classifications": [{"agentic_allowed": True, "brokerage_account_type": "individual", "management_type": "self_directed", "state": "active", "deactivated": False, "permanently_deactivated": False}],
        "errors": [],
    }


class CodexProtocolAlignmentTests(unittest.TestCase):
    def setUp(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner.project_root = ROOT
        runner.executable = "codex"
        runner.child_environment = {}
        runner.timeout_seconds = 10
        runner.transient_retries = 0
        runner.retry_backoff = 0
        runner.version = "0.147.0"
        runner._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        runner._shadow_boundary = ShadowBoundaryResult(Path("/tmp/config.toml"), "robinhood-trading", APPROVED_SHADOW_ROBINHOOD_TOOLS)
        self.runner = runner

    def run_job(self, event_stream: str, *, final_text: str | None = None, returncode: int = 0, allow_web: bool = False):
        completed = subprocess.CompletedProcess([], returncode, stdout=event_stream, stderr="")
        final_text = json.dumps(good_final()) if final_text is None else final_text

        def execute(command, **_kwargs):
            if final_text != "<missing>":
                Path(command[command.index("--output-last-message") + 1]).write_text(final_text, encoding="utf-8")
            return completed

        with patch("trader.codex_runner.subprocess.run", side_effect=execute):
            return self.runner.run(
                prompt_path=ROOT / "prompts" / "preflight.md",
                schema_path=ROOT / "schemas" / "preflight.schema.json",
                model="test",
                context={},
                required_robinhood_tools=frozenset(REQUIRED),
                allow_web=allow_web,
                exact_robinhood_tools=True,
            )

    def assert_job_fails(self, event_stream: str, **kwargs):
        with self.assertRaises((CodexRunError, SchemaValidationError)):
            self.run_job(event_stream, **kwargs)

    def test_one_two_or_three_agent_messages_use_separate_final_output(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                result = self.run_job(lifecycle(agent_messages=count))
                self.assertTrue(result.data["passed"])
                self.assertEqual(parse_codex_jsonl(lifecycle(agent_messages=count)).agent_messages, count)

    def test_agent_messages_are_not_parsed_as_final_responses(self):
        output = lifecycle(agent_messages=3)
        self.assertEqual(parse_codex_jsonl(output).agent_messages, 3)
        self.assertTrue(self.run_job(output).data["passed"])

    def test_missing_empty_malformed_and_trailing_final_output_fail(self):
        for value in ("<missing>", "", "{bad", json.dumps(good_final()) + " {}"):
            with self.subTest(value=value):
                self.assert_job_fails(lifecycle(), final_text=value)

    def test_schema_invalid_final_output_fails(self):
        self.assert_job_fails(lifecycle(), final_text=json.dumps({"not": "the schema"}))

    def test_valid_final_file_cannot_override_failed_turn(self):
        self.assert_job_fails(lifecycle(terminal="turn.failed"))

    def test_valid_final_file_cannot_override_structured_error(self):
        output = lifecycle(extra=[{"type": "error", "error": {"message": "failed"}}])
        self.assert_job_fails(output)

    def test_started_identity_completed_without_identity_passes(self):
        parsed = parse_codex_jsonl(lifecycle())
        self.assertEqual(parsed.tool_calls, {f"robinhood-trading::{tool}": 1 for tool in REQUIRED})

    def test_started_and_completed_matching_identity_passes(self):
        self.assertEqual(len(parse_codex_jsonl(lifecycle(completed_identity=True)).tool_calls), 4)

    def test_completed_identity_disagreement_fails(self):
        events = [
            {"type": "thread.started"}, {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": "get_accounts"}},
            {"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": "get_portfolio"}},
            {"type": "turn.completed"},
        ]
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl(encode(events))

    def test_unknown_completion_unresolved_identity_and_duplicates_fail(self):
        cases = [
            [
                {"type": "item.completed", "item": {"id": "unknown", "type": "mcp_tool_call"}},
            ],
            [
                {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call"}},
            ],
            [
                {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": "get_accounts"}},
                {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": "get_accounts"}},
            ],
            [
                {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": "get_accounts"}},
                {"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call"}},
                {"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call"}},
            ],
        ]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(CodexRunError):
                parse_codex_jsonl(encode([{"type": "thread.started"}, {"type": "turn.started"}, *items, {"type": "turn.completed"}]))

    def test_required_tool_started_but_not_completed_fails(self):
        output = lifecycle().replace('{"type": "item.completed", "item": {"id": "m3", "type": "mcp_tool_call"}}\n', "")
        self.assert_job_fails(output)

    def test_failed_or_cancelled_tool_completion_fails(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                output = lifecycle().replace(
                    '{"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call"}}',
                    json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call", "status": status, "error": {"message": "user cancelled MCP tool call"}}}),
                )
                self.assert_job_fails(output)

    def test_exact_four_pass_and_fifth_or_prohibited_fail(self):
        self.assertTrue(self.run_job(lifecycle()).data["passed"])
        for tool in ("get_equity_quotes", "review_equity_order"):
            extra = [
                {"type": "item.started", "item": {"id": "m5", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": tool}},
                {"type": "item.completed", "item": {"id": "m5", "type": "mcp_tool_call"}},
            ]
            with self.subTest(tool=tool):
                self.assert_job_fails(lifecycle(extra=extra))

    def test_reasoning_messages_plan_and_tools_can_be_intermixed(self):
        extra = [
            {"type": "item.completed", "item": {"id": "r1", "type": "reasoning"}},
            {"type": "item.completed", "item": {"id": "p1", "type": "plan_update"}},
            {"type": "item.completed", "item": {"id": "a9", "type": "agent_message", "text": "progress"}},
        ]
        self.assertTrue(self.run_job(lifecycle(extra=extra)).data["passed"])

    def test_web_search_fails_when_web_is_disabled(self):
        extra = [
            {"type": "item.started", "item": {"id": "w1", "type": "web_search"}},
            {"type": "item.completed", "item": {"id": "w1", "type": "web_search"}},
        ]
        self.assert_job_fails(lifecycle(extra=extra))

    def test_successful_local_command_fails_in_exact_preflight(self):
        extra = [
            {"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "pwd", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "pwd", "status": "completed"}},
        ]
        self.assert_job_fails(lifecycle(extra=extra))

    def test_unknown_security_relevant_item_fails_closed(self):
        extra = [{"type": "item.completed", "item": {"id": "x1", "type": "future_tool_execution"}}]
        self.assert_job_fails(lifecycle(extra=extra))

    def test_benign_recognized_non_tool_item_is_allowed(self):
        extra = [{"type": "item.completed", "item": {"id": "r1", "type": "reasoning"}}]
        self.assertTrue(self.run_job(lifecycle(extra=extra)).data["passed"])

    def test_sanitized_codex_0147_fixture_and_protocol_marker(self):
        fixture = ROOT / "tests" / "fixtures" / "codex-0.147.0" / "mcp-name-on-start-only.jsonl"
        parsed = parse_codex_jsonl(fixture.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed.tool_calls), 4)
        self.assertIn("0.147.0", CODEX_EVENT_PROTOCOL)

    def test_generic_nonzero_run_fails(self):
        self.assert_job_fails(lifecycle(), returncode=7)


if __name__ == "__main__":
    unittest.main()
