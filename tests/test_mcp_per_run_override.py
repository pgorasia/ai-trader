from __future__ import annotations

import subprocess
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from trader.codex_runner import CodexRunner, build_robinhood_enabled_tools_override
from trader.models import CodexRunError, CodexTimeoutError
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, ShadowBoundaryResult, locate_codex_config


ROOT = Path(__file__).resolve().parents[1]
STAGES = {
    "identity": frozenset({"get_accounts"}),
    "portfolio": frozenset({"get_accounts", "get_portfolio"}),
    "positions": frozenset({"get_accounts", "get_equity_positions"}),
    "orders": frozenset({"get_accounts", "get_equity_orders"}),
}


def deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


class McpPerRunOverrideTests(unittest.TestCase):
    def runner(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner.executable = "codex"
        runner.project_root = ROOT
        runner._shadow_boundary = ShadowBoundaryResult(Path("/tmp/config.toml"), "robinhood-trading", APPROVED_SHADOW_ROBINHOOD_TOOLS)
        return runner

    def command(self, tools):
        return self.runner().build_command(
            "gpt-5.6-luna", ROOT / "schemas/preflight.schema.json", Path("/tmp/final.json"),
            allow_web=False, robinhood_enabled_tools=tools,
        )

    def override(self, tools):
        command = self.command(tools)
        return command[command.index("--config") + 1]

    def test_each_stage_has_exact_leaf_override(self):
        expected = {
            "identity": 'mcp_servers.robinhood-trading.enabled_tools=["get_accounts"]',
            "portfolio": 'mcp_servers.robinhood-trading.enabled_tools=["get_accounts","get_portfolio"]',
            "positions": 'mcp_servers.robinhood-trading.enabled_tools=["get_accounts","get_equity_positions"]',
            "orders": 'mcp_servers.robinhood-trading.enabled_tools=["get_accounts","get_equity_orders"]',
        }
        for stage, tools in STAGES.items():
            with self.subTest(stage=stage):
                self.assertEqual(self.override(tools), expected[stage])

    def test_leaf_merge_preserves_transport_required_enabled_and_replaces_tools(self):
        base_text = '''
[mcp_servers.robinhood-trading]
url = "https://example.invalid/mcp"
required = true
enabled = true
enabled_tools = ["a", "b", "c"]
oauth_token = "BASE-ONLY-SECRET"
'''
        base = tomllib.loads(base_text)
        override = tomllib.loads(build_robinhood_enabled_tools_override("robinhood-trading", frozenset({"a"})))
        effective = deep_merge(base, override)["mcp_servers"]["robinhood-trading"]
        self.assertEqual(effective["url"], "https://example.invalid/mcp")
        self.assertIs(effective["required"], True)
        self.assertIs(effective["enabled"], True)
        self.assertEqual(effective["enabled_tools"], ["a"])
        self.assertEqual(effective["oauth_token"], "BASE-ONLY-SECRET")

    def test_base_transport_and_oauth_are_not_copied_into_argv(self):
        joined = " ".join(self.command(STAGES["identity"]))
        self.assertNotIn("url", joined)
        self.assertNotIn("oauth", joined.lower())
        self.assertNotIn("bearer", joined.lower())
        self.assertNotIn("required", joined)
        self.assertNotIn("enabled=true", joined)

    def test_whole_table_replacement_is_prohibited(self):
        for tools in STAGES.values():
            override = self.override(tools)
            self.assertNotIn("robinhood-trading={", override)
            parsed = tomllib.loads(override)
            self.assertEqual(set(parsed["mcp_servers"]["robinhood-trading"]), {"enabled_tools"})

    def test_malformed_toml_array_input_is_rejected(self):
        with self.assertRaises(CodexRunError):
            build_robinhood_enabled_tools_override("robinhood-trading", frozenset({'get_accounts"]'}))

    def test_hyphenated_server_id_is_a_valid_bare_dotted_key(self):
        override = build_robinhood_enabled_tools_override("robinhood-trading", STAGES["identity"])
        self.assertTrue(override.startswith("mcp_servers.robinhood-trading.enabled_tools="))
        self.assertEqual(tomllib.loads(override)["mcp_servers"]["robinhood-trading"]["enabled_tools"], ["get_accounts"])

    def test_tool_outside_global_policy_is_rejected(self):
        runner = self.runner()
        with patch("trader.codex_runner.subprocess.run") as child, self.assertRaises(CodexRunError):
            runner.run(
                prompt_path=ROOT / "prompts/preflight.md", schema_path=ROOT / "schemas/preflight.schema.json",
                model="gpt-5.6-luna", context={}, required_robinhood_tools=frozenset({"review_equity_order"}),
                robinhood_enabled_tools=frozenset({"review_equity_order"}),
            )
        child.assert_not_called()

    def test_subprocess_remains_shell_false(self):
        runner = self.runner()
        runner.child_environment = {}
        runner.timeout_seconds = 1
        runner.transient_retries = 0
        runner.retry_backoff = 0
        runner._last_run_diagnostics = {}
        with patch("trader.codex_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)) as child:
            with self.assertRaises(CodexTimeoutError):
                runner.run(
                    prompt_path=ROOT / "prompts/preflight.md", schema_path=ROOT / "schemas/preflight.schema.json",
                    model="gpt-5.6-luna", context={}, required_robinhood_tools=STAGES["identity"],
                    robinhood_enabled_tools=STAGES["identity"],
                )
        self.assertIs(child.call_args.kwargs["shell"], False)

    def test_global_config_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "config.toml"
            configured.write_text('[mcp_servers.robinhood-trading]\nurl = "https://example.invalid/mcp"\n', encoding="utf-8")
            path = locate_codex_config({"config_path": str(configured.resolve())})
            before = path.read_bytes()
            for tools in STAGES.values():
                self.command(tools)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
