from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trader.codex_runner import CodexRunner
from trader.models import PreflightError
from trader.shadow_boundary import (
    APPROVED_SHADOW_ROBINHOOD_TOOLS,
    HostPolicyResult,
    ROBINHOOD_MCP_IDENTITY_URL,
    ShadowBoundaryResult,
    verify_host_policy,
)

ROOT = Path(__file__).resolve().parents[1]


VALID = f'''[features]
apps = false
plugins = false
browser_use = false
browser_use_external = false
browser_use_full_cdp_access = false
computer_use = false

[mcp_servers.robinhood-trading]
identity = {{ url = "{ROBINHOOD_MCP_IDENTITY_URL}" }}
'''


class HostPolicyTests(unittest.TestCase):
    def write(self, text: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "requirements.toml"
        path.write_text(text, encoding="utf-8")
        return directory, path

    def test_valid_injected_policy_passes_without_real_etc_dependency(self):
        directory, path = self.write(VALID)
        self.addCleanup(directory.cleanup)
        result = verify_host_policy(path)
        self.assertEqual(result.identity_url, ROBINHOOD_MCP_IDENTITY_URL)

    def test_missing_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(PreflightError):
            verify_host_policy(Path(directory) / "missing.toml")

    def test_feature_and_integration_drift_fails_preflight(self):
        for feature in ("apps", "plugins", "browser_use", "browser_use_external",
                        "browser_use_full_cdp_access", "computer_use"):
            with self.subTest(feature=feature):
                directory, path = self.write(VALID.replace(f"{feature} = false", f"{feature} = true"))
                self.addCleanup(directory.cleanup)
                with self.assertRaises(PreflightError):
                    verify_host_policy(path)

    def test_foreign_server_and_identity_drift_fail_preflight(self):
        cases = (
            VALID + '\n[mcp_servers.sites]\nidentity = { url = "https://example.invalid" }\n',
            VALID.replace(ROBINHOOD_MCP_IDENTITY_URL, "https://example.invalid/mcp"),
            VALID.replace("[mcp_servers.robinhood-trading]", "[mcp_servers.sites]"),
        )
        for text in cases:
            with self.subTest(text=text):
                directory, path = self.write(text)
                self.addCleanup(directory.cleanup)
                with self.assertRaises(PreflightError):
                    verify_host_policy(path)

    def test_drift_after_startup_stops_before_codex_child(self):
        directory, path = self.write(VALID)
        self.addCleanup(directory.cleanup)
        runner = CodexRunner.__new__(CodexRunner)
        runner._host_policy = HostPolicyResult(path)
        runner._shadow_boundary = ShadowBoundaryResult(
            Path("/tmp/config.toml"), "robinhood-trading", APPROVED_SHADOW_ROBINHOOD_TOOLS
        )
        path.write_text(VALID.replace("plugins = false", "plugins = true"), encoding="utf-8")
        with patch("trader.codex_runner.subprocess.run") as child, self.assertRaises(PreflightError):
            runner.run(
                prompt_path=ROOT / "prompts/preflight.md",
                schema_path=ROOT / "schemas/preflight.schema.json",
                model="test", context={}, required_robinhood_tools=frozenset(),
            )
        child.assert_not_called()

    def test_canonical_robinhood_policy_has_reads_and_zero_writes(self):
        self.assertIn("get_accounts", APPROVED_SHADOW_ROBINHOOD_TOOLS)
        self.assertIn("get_equity_historicals", APPROVED_SHADOW_ROBINHOOD_TOOLS)
        write_prefixes = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_")
        self.assertEqual([], sorted(name for name in APPROVED_SHADOW_ROBINHOOD_TOOLS
                                    if name.startswith(write_prefixes)))


if __name__ == "__main__":
    unittest.main()
