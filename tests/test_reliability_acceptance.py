from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator
from trader.models import PreflightError


class ReliabilityAcceptanceTests(unittest.TestCase):
    def test_missing_acceptance_artifact_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator._git_commit", return_value="a" * 40):
            with self.assertRaisesRegex(PreflightError, "DEPLOYMENT_NOT_ACCEPTED"):
                orchestrator.verify_deployment_accepted(Path(directory))

    def test_wrong_commit_acceptance_artifact_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "state").mkdir()
            artifact = {"version": 1, "accepted_git_commit": "a" * 40, "mode": "SHADOW",
                        "strategy_version": orchestrator.STRATEGY_VERSION,
                        "offline_gate": "PASS", "live_read_only_gate": "PASS",
                        "live_run_counts": {"preflight": 5, "luna_schema": 5, "eod": 3},
                        "global_shadow_tool_count": 22}
            (root / "state/reliability_acceptance.json").write_text(json.dumps(artifact), encoding="utf-8")
            with patch("orchestrator._git_commit", return_value="b" * 40), self.assertRaisesRegex(PreflightError, "DEPLOYMENT_NOT_ACCEPTED"):
                orchestrator.verify_deployment_accepted(root)

    def test_matching_commit_acceptance_artifact_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "state").mkdir()
            artifact = {"version": 1, "accepted_git_commit": "a" * 40, "mode": "SHADOW",
                        "strategy_version": orchestrator.STRATEGY_VERSION,
                        "offline_gate": "PASS", "live_read_only_gate": "PASS",
                        "live_run_counts": {"preflight": 5, "luna_schema": 5, "eod": 3},
                        "global_shadow_tool_count": 22}
            (root / "state/reliability_acceptance.json").write_text(json.dumps(artifact), encoding="utf-8")
            with patch("orchestrator._git_commit", return_value="a" * 40):
                orchestrator.verify_deployment_accepted(root)

    def test_strategy_freeze_baseline_and_tool_boundary(self):
        config = orchestrator.load_config(orchestrator.CONFIG_PATH)
        result = orchestrator._strategy_freeze_result(orchestrator.ROOT, config)
        self.assertEqual(result["mode"], "SHADOW")
        self.assertTrue(result["strategy_config_unchanged"])
        self.assertEqual(result["global_shadow_tool_count"], 22)
        self.assertEqual(result["forbidden_or_write_tools"], [])


if __name__ == "__main__":
    unittest.main()
