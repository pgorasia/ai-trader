from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_events import parse_codex_jsonl
from trader.codex_executable import codex_child_environment, resolve_codex_executable
from trader.models import CodexRunError, ConfigurationError, PreflightError, SchemaValidationError
from trader.shadow_boundary import (
    APPROVED_SHADOW_ROBINHOOD_TOOLS,
    REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS,
    verify_shadow_mcp_boundary,
)


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tests" / "fixtures" / "events"


def config_text(tools: set[str], *, second: bool = False) -> str:
    quoted = ", ".join(f'"{name}"' for name in sorted(tools))
    extra = '\n[mcp_servers."another-robinhood"]\nenabled_tools = ["get_accounts"]\n' if second else ""
    approvals = "".join(
        f'\n[mcp_servers."robinhood-trading".tools.{name}]\napproval_mode = "approve"\n'
        for name in sorted(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS)
    )
    return f'[mcp_servers."robinhood-trading"]\nenabled_tools = [{quoted}]\n{approvals}{extra}'


class ShadowBoundaryTests(unittest.TestCase):
    def verify(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            return verify_shadow_mcp_boundary(path)

    def test_exact_approved_set_passes(self):
        result = self.verify(config_text(set(APPROVED_SHADOW_ROBINHOOD_TOOLS)))
        self.assertEqual(result.enabled_tools, APPROVED_SHADOW_ROBINHOOD_TOOLS)

    def test_safe_subset_with_required_reads_passes(self):
        result = self.verify(config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS)))
        self.assertEqual(result.enabled_tools, REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS)

    def test_known_forbidden_tool_fails(self):
        with self.assertRaises(PreflightError):
            self.verify(config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS) | {"review_equity_order"}))

    def test_unknown_future_tool_fails(self):
        with self.assertRaises(PreflightError):
            self.verify(config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS) | {"future_market_read"}))

    def test_renamed_looking_mutation_fails(self):
        with self.assertRaises(PreflightError):
            self.verify(config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS) | {"safely_adjust_equity_order"}))

    def test_missing_robinhood_server_fails(self):
        with self.assertRaises(PreflightError):
            self.verify('[mcp_servers.other]\nenabled_tools=["read"]')

    def test_enabled_tools_missing_fails(self):
        with self.assertRaises(PreflightError):
            self.verify('[mcp_servers."robinhood-trading"]\ncommand="server"')

    def test_missing_unattended_read_approval_fails_clearly(self):
        content = config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS)).replace(
            '\n[mcp_servers."robinhood-trading".tools.get_portfolio]\napproval_mode = "approve"\n',
            "",
        )
        with self.assertRaisesRegex(PreflightError, "get_portfolio"):
            self.verify(content)

    def test_non_approve_read_mode_fails_clearly(self):
        content = config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS)).replace(
            '[mcp_servers."robinhood-trading".tools.get_equity_orders]\napproval_mode = "approve"',
            '[mcp_servers."robinhood-trading".tools.get_equity_orders]\napproval_mode = "prompt"',
        )
        with self.assertRaisesRegex(PreflightError, "get_equity_orders"):
            self.verify(content)

    def test_malformed_config_fails_without_echoing_contents(self):
        with self.assertRaises(PreflightError) as caught:
            self.verify('oauth_token="SECRET"\n[')
        self.assertNotIn("SECRET", str(caught.exception))

    def test_ambiguous_robinhood_servers_fail(self):
        with self.assertRaises(PreflightError):
            self.verify(config_text(set(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS), second=True))


class CodexExecutableTests(unittest.TestCase):
    def make_binary(self, directory: str, name: str = "codex") -> Path:
        path = Path(directory) / name
        path.write_text("placeholder", encoding="utf-8")
        path.chmod(0o755)
        return path

    def completed(self, version: str = "codex-cli 0.147.0", returncode: int = 0):
        return subprocess.CompletedProcess([], returncode, stdout=version + "\n", stderr="")

    def test_native_linux_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.make_binary(directory)
            with patch("trader.codex_executable.shutil.which", return_value=str(binary)), patch("trader.codex_executable.subprocess.run", return_value=self.completed()), patch("trader.codex_executable.platform.system", return_value="Linux"):
                result = resolve_codex_executable({"executable": "auto"})
            self.assertEqual(result.path, binary.resolve())

    def test_explicit_configured_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.make_binary(directory)
            with patch("trader.codex_executable.subprocess.run", return_value=self.completed("codex-cli 1.2.3")), patch("trader.codex_executable.platform.system", return_value="Linux"):
                result = resolve_codex_executable({"executable": str(binary)})
            self.assertEqual(result.version, "1.2.3")

    def test_executable_missing(self):
        with self.assertRaises(ConfigurationError):
            resolve_codex_executable({"executable": "/definitely/missing/codex"})

    def test_version_command_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.make_binary(directory)
            with patch("trader.codex_executable.subprocess.run", return_value=self.completed(returncode=1)), patch("trader.codex_executable.platform.system", return_value="Linux"):
                with self.assertRaises(ConfigurationError):
                    resolve_codex_executable({"executable": str(binary)})

    def test_malformed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.make_binary(directory)
            with patch("trader.codex_executable.subprocess.run", return_value=self.completed("Codex unknown")), patch("trader.codex_executable.platform.system", return_value="Linux"):
                with self.assertRaises(ConfigurationError):
                    resolve_codex_executable({"executable": str(binary)})

    def test_windows_binary_discovered_under_wsl_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.make_binary(directory, "codex.exe")
            with patch("trader.codex_executable.shutil.which", return_value=str(binary)), patch("trader.codex_executable.platform.system", return_value="Linux"):
                with self.assertRaises(ConfigurationError):
                    resolve_codex_executable({"executable": "auto"})

    def test_ambiguous_windows_candidates_fail(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.make_binary(first, "codex.exe")
            self.make_binary(second, "codex.exe")
            environment = {"PATH": os.pathsep.join((first, second)), "PATHEXT": ".EXE"}
            with patch.dict(os.environ, environment, clear=False), patch("trader.codex_executable.platform.system", return_value="Windows"):
                with self.assertRaises(ConfigurationError):
                    resolve_codex_executable({"executable": "auto"})

    def test_path_change_does_not_change_resolved_value(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            binary1, binary2 = self.make_binary(first), self.make_binary(second)
            with patch("trader.codex_executable.shutil.which", return_value=str(binary1)), patch("trader.codex_executable.subprocess.run", return_value=self.completed()), patch("trader.codex_executable.platform.system", return_value="Linux"):
                selected = resolve_codex_executable({"executable": "auto"})
            with patch.dict(os.environ, {"PATH": second}, clear=False):
                self.assertEqual(selected.path, binary1.resolve())
                self.assertNotEqual(selected.path, binary2.resolve())

    def test_openai_api_key_stripped_from_child_only(self):
        source = {"PATH": "/bin", "OPENAI_API_KEY": "SECRET"}
        child = codex_child_environment({"strip_openai_api_key": True}, source)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertIn("OPENAI_API_KEY", source)


class CodexEventTests(unittest.TestCase):
    def fixture(self, name: str) -> str:
        return (EVENTS / name).read_text(encoding="utf-8")

    def test_luna_no_web_passes_and_claim_does_not_count(self):
        result = parse_codex_jsonl(self.fixture("luna_no_web.jsonl"))
        self.assertEqual(result.web_searches, 0)
        self.assertEqual(result.tool_calls, {"robinhood-trading::get_accounts": 1})

    def test_luna_actual_web_is_observed(self):
        result = parse_codex_jsonl(self.fixture("luna_web.jsonl"))
        self.assertEqual(result.web_searches, 1)

    def test_sol_claim_without_event_cannot_satisfy_search(self):
        result = parse_codex_jsonl(self.fixture("luna_no_web.jsonl"))
        self.assertFalse(result.web_searches > 0)

    def test_unknown_tool_shape_fails_closed(self):
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl(self.fixture("unknown_tool.jsonl"))

    def test_prohibited_observed_tool_fails(self):
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl(self.fixture("prohibited_tool.jsonl"))

    def test_malformed_jsonl_fails(self):
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl(self.fixture("malformed.jsonl"))

    def test_valid_output_with_nonzero_exit_fails(self):
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl(self.fixture("luna_no_web.jsonl"), returncode=2)

    def test_truncated_stream_fails(self):
        with self.assertRaises(CodexRunError):
            parse_codex_jsonl(self.fixture("truncated.jsonl"))

    def test_multiple_agent_messages_are_lifecycle_only(self):
        result = parse_codex_jsonl(self.fixture("multiple_final.jsonl"))
        self.assertEqual(result.agent_messages, 2)


if __name__ == "__main__":
    unittest.main()
