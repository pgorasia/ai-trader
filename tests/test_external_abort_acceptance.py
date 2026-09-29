from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import orchestrator
from trader.models import CodexRunError, PreflightError, SchemaValidationError, StateCorruptionError


COMMIT = "a" * 40
SOURCE = "b" * 64


class ExternalAbortAcceptanceTests(unittest.TestCase):
    def root(self, directory: str) -> Path:
        root = Path(directory)
        (root / "state").mkdir()
        (root / "reports").mkdir()
        return root

    def run_gate(self, root: Path, core: Mock):
        with patch("orchestrator._git_commit", return_value=COMMIT), \
             patch("orchestrator._candidate_source_identity", return_value=SOURCE), \
             patch("orchestrator._service_active", return_value=False), \
             patch("orchestrator.reliability_acceptance_offline", return_value={"status": "PASS"}), \
             patch("orchestrator._acceptance_artifact", return_value={"accepted_git_commit": COMMIT}):
            return orchestrator.reliability_acceptance_live(
                root, core, "2026-08-19", preflight_runs=1,
                luna_schema_runs=1, eod_runs=1,
            )

    def test_model_refresh_timeout_is_external_abort_and_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory); core = Mock()
            core.smoke_preflight_acceptance.side_effect = CodexRunError(
                "failed to refresh available models: timeout waiting for child process to exit")
            result = self.run_gate(root, core)
            self.assertEqual(result["status"], "EXTERNAL_ABORT")
            self.assertTrue(result["retryable_same_commit"])
            self.assertFalse((root / "state/reliability_acceptance.json").exists())

    def test_backend_502_before_response_is_external_abort(self):
        error = CodexRunError("Codex backend HTTP 502", diagnostics={"event_sequence": []})
        self.assertEqual(orchestrator._external_abort_reason(error), "CODEX_BACKEND_UNAVAILABLE")

    def test_teardown_400_alone_never_classifies_external(self):
        error = CodexRunError("DELETE returned HTTP 400 session teardown")
        self.assertIsNone(orchestrator._external_abort_reason(error))

    def test_usable_malformed_response_is_terminal(self):
        error = CodexRunError("HTTP 503 malformed model output", diagnostics={
            "usable_response": True,
            "event_sequence": [{"event": "agent_message.completed"}]})
        self.assertIsNone(orchestrator._external_abort_reason(error))
        self.assertIsNone(orchestrator._external_abort_reason(SchemaValidationError("malformed")))

    def test_foreign_and_write_tool_exposure_are_terminal(self):
        for message in ("foreign MCP server observed", "Observed prohibited tool activity: place_order"):
            with self.subTest(message=message):
                self.assertIsNone(orchestrator._external_abort_reason(CodexRunError(
                    message, diagnostics={"required_tool_validation_reached": True})))

    def test_production_mutation_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory); core = Mock()
            def mutate():
                (root / "state/production.json").write_text("changed", encoding="utf-8")
                raise CodexRunError("Codex backend HTTP 503")
            core.smoke_preflight_acceptance.side_effect = mutate
            with self.assertRaises(CodexRunError):
                self.run_gate(root, core)
            record = json.loads((root / orchestrator.LIVE_ACCEPTANCE_ATTEMPT_PATH).read_text())
            self.assertEqual(record["outcome"], "TERMINAL_FAILURE")
            self.assertTrue(record["production_modified"])

    def test_failed_account_provenance_is_terminal(self):
        self.assertIsNone(orchestrator._external_abort_reason(
            PreflightError("account provenance mismatch")))

    def test_success_writes_canonical_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory); result = self.run_gate(root, Mock())
            self.assertEqual(result["status"], "PASS")
            self.assertTrue((root / "state/reliability_acceptance.json").exists())
            record = json.loads((root / orchestrator.LIVE_ACCEPTANCE_ATTEMPT_PATH).read_text())
            self.assertEqual(record["outcome"], "PASS")
            self.assertTrue(record["canonical_acceptance_written"])

    def test_terminal_failure_same_commit_is_not_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory); core = Mock()
            core.smoke_preflight_acceptance.side_effect = CodexRunError("malformed model output")
            with self.assertRaises(CodexRunError): self.run_gate(root, core)
            with self.assertRaisesRegex(PreflightError, "non-repeatable"):
                self.run_gate(root, core)

    def test_external_abort_same_commit_permits_another_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory); core = Mock()
            core.smoke_preflight_acceptance.side_effect = [
                CodexRunError("Codex backend HTTP 503"), None]
            self.assertEqual(self.run_gate(root, core)["status"], "EXTERNAL_ABORT")
            self.assertEqual(self.run_gate(root, core)["status"], "PASS")
            self.assertEqual(core.smoke_preflight_acceptance.call_count, 2)

    def test_source_change_invalidates_retry_entitlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            with patch("orchestrator._git_commit", return_value=COMMIT), \
                 patch("orchestrator._candidate_source_identity", return_value="c" * 64):
                orchestrator._write_live_acceptance_attempt(
                    root, outcome="EXTERNAL_ABORT", commit=COMMIT,
                    source_identity=SOURCE, reason="CODEX_BACKEND_UNAVAILABLE")
                with self.assertRaisesRegex(PreflightError, "source changed"):
                    orchestrator._begin_live_acceptance_attempt(root)


if __name__ == "__main__":
    unittest.main()
