from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_runner import CodexRunner
from trader.models import CodexRunError
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, ShadowBoundaryResult


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders")


def stream(tools: list[tuple[str, bool]], *, claimed_count: int = 4) -> str:
    data = {
        "passed": True,
        "account_classifications": [{"agentic_allowed": True, "brokerage_account_type": "individual", "management_type": "self_directed", "state": "active", "deactivated": False, "permanently_deactivated": False}],
        "errors": [],
    }
    events = [{"type": "thread.started", "thread_id": "test"}, {"type": "turn.started"}]
    for index, (tool, completed) in enumerate(tools):
        item = {"id": f"m{index}", "type": "mcp_tool_call", "server": "robinhood-trading", "tool": tool}
        events.append({"type": "item.started", "item": item})
        if completed:
            events.append({"type": "item.completed", "item": item})
    events.extend([
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": json.dumps(data)}},
        {"type": "turn.completed", "usage": {}},
    ])
    return "\n".join(json.dumps(event) for event in events) + "\n"


class PreflightToolContractTests(unittest.TestCase):
    def setUp(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner.project_root = ROOT
        runner.executable = "codex"
        runner.child_environment = {}
        runner.timeout_seconds = 10
        runner.transient_retries = 0
        runner.retry_backoff = 0
        runner.version = "test"
        runner._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        runner._shadow_boundary = ShadowBoundaryResult(
            Path("/tmp/config.toml"), "robinhood-trading", APPROVED_SHADOW_ROBINHOOD_TOOLS
        )
        self.runner = runner

    def run_stream(self, output: str):
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        def execute(command, **_kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            message = json.loads(next(
                event for event in output.splitlines()
                if json.loads(event).get("type") == "item.completed"
                and json.loads(event).get("item", {}).get("type") == "agent_message"
            ))["item"]["text"]
            output_path.write_text(message, encoding="utf-8")
            return completed
        with patch("trader.codex_runner.subprocess.run", side_effect=execute):
            return self.runner.run(
                prompt_path=ROOT / "prompts" / "preflight.md",
                schema_path=ROOT / "schemas" / "preflight.schema.json",
                model="test",
                context={},
                required_robinhood_tools=frozenset(REQUIRED),
                exact_robinhood_tools=True,
            )

    def assert_contract_fails(self, tools, *, claimed_count=4):
        with self.assertRaises(CodexRunError):
            self.run_stream(stream(tools, claimed_count=claimed_count))

    def test_all_four_required_calls_exactly_once_pass(self):
        result = self.run_stream(stream([(tool, True) for tool in REQUIRED]))
        self.assertEqual(result.tool_calls, {tool: 1 for tool in REQUIRED})

    def test_get_accounts_only_fails(self):
        self.assert_contract_fails([("get_accounts", True)], claimed_count=4)

    def test_each_missing_account_scoped_call_fails(self):
        for missing in REQUIRED[1:]:
            with self.subTest(missing=missing):
                self.assert_contract_fails([(tool, True) for tool in REQUIRED if tool != missing])

    def test_duplicate_required_call_fails(self):
        calls = [(tool, True) for tool in REQUIRED] + [("get_portfolio", True)]
        self.assert_contract_fails(calls, claimed_count=5)

    def test_started_but_uncompleted_required_call_fails(self):
        calls = [(tool, tool != "get_equity_orders") for tool in REQUIRED]
        self.assert_contract_fails(calls)

    def test_fifth_allowed_robinhood_read_fails(self):
        calls = [(tool, True) for tool in REQUIRED] + [("get_equity_quotes", True)]
        self.assert_contract_fails(calls, claimed_count=5)

    def test_prohibited_robinhood_tool_fails(self):
        calls = [(tool, True) for tool in REQUIRED] + [("review_equity_order", True)]
        self.assert_contract_fails(calls, claimed_count=5)

    def test_model_claim_cannot_replace_observed_calls(self):
        self.assert_contract_fails([("get_accounts", True)], claimed_count=4)

    def test_exact_observed_calls_and_valid_result_pass(self):
        self.assertTrue(self.run_stream(stream([(tool, True) for tool in REQUIRED])).data["passed"])

    def test_get_accounts_must_complete_before_scoped_calls_start(self):
        calls = [("get_portfolio", True), ("get_accounts", True), ("get_equity_positions", True), ("get_equity_orders", True)]
        self.assert_contract_fails(calls)


if __name__ == "__main__":
    unittest.main()
