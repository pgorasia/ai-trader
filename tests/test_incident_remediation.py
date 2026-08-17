from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from trader.job_contracts import JOB_TOOL_CONTRACTS, UNATTENDED_APPROVAL_TOOLS, validate_job_contracts
from trader.maintenance import AIMaintenanceGate, process_ai_queue
from trader.models import CodexRunError, PreflightError
from trader.operations import (eligible, fail, prepare, record_ai_failure, safe_failure_diagnostic, start)
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, verify_shadow_mcp_boundary
from trader.state import initial_state


NOW = datetime(2026, 8, 17, 20, 5, tzinfo=timezone.utc)


def config_text(approved: set[str], *, default_approve: bool = False) -> str:
    enabled = ",".join(json.dumps(x) for x in sorted(APPROVED_SHADOW_ROBINHOOD_TOOLS))
    text = f'[mcp_servers.robinhood]\nenabled_tools=[{enabled}]\n'
    if default_approve: text += 'default_tools_approval_mode="approve"\n'
    for name in sorted(approved): text += f'[mcp_servers.robinhood.tools.{name}]\napproval_mode="approve"\n'
    return text


class ApprovalAndContractTests(unittest.TestCase):
    def verify(self, approved, default=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; path.write_text(config_text(set(approved), default_approve=default))
            return verify_shadow_mcp_boundary(path)

    def test_exact_nine_approval_union(self):
        self.assertEqual(len(UNATTENDED_APPROVAL_TOOLS), 9); self.verify(UNATTENDED_APPROVAL_TOOLS)

    def test_each_critical_missing_fails(self):
        for name in ("run_scan", "get_equity_historicals", "get_equity_quotes"):
            with self.subTest(name=name), self.assertRaisesRegex(PreflightError, name):
                self.verify(UNATTENDED_APPROVAL_TOOLS - {name})

    def test_default_approve_does_not_substitute(self):
        with self.assertRaises(PreflightError): self.verify(set(), default=True)

    def test_unused_global_tool_needs_no_approval(self):
        self.verify(UNATTENDED_APPROVAL_TOOLS)
        self.assertNotIn("get_financials", UNATTENDED_APPROVAL_TOOLS)

    def test_all_contracts_subset_and_read_only(self):
        self.assertEqual(validate_job_contracts(), [])
        self.assertTrue(all(tools <= APPROVED_SHADOW_ROBINHOOD_TOOLS for tools in JOB_TOOL_CONTRACTS.values()))

    def test_exact_per_job_exposure(self):
        self.assertEqual(len(JOB_TOOL_CONTRACTS["STAGE_B"]), 9)
        self.assertEqual(JOB_TOOL_CONTRACTS["SOL_SENIOR"], {"get_equity_quotes", "get_equity_historicals"})
        self.assertEqual(JOB_TOOL_CONTRACTS["MONITOR"], {"get_equity_historicals"})
        self.assertEqual(JOB_TOOL_CONTRACTS["EOD"], {"get_equity_historicals"})


class OperationPolicyTests(unittest.TestCase):
    def state(self): return initial_state("2026-08-17", now=NOW)

    def test_eod_retry_waits_and_caps_at_three(self):
        state = self.state(); record = prepare(state, "eod:2026-08-17", "EOD", NOW, 3)
        for attempt, wait in ((1, 60), (2, 300)):
            start(record, NOW); self.assertEqual(fail(record, CodexRunError("HTTP 503 temporary"), NOW), "RETRY_AT")
            self.assertEqual(datetime.fromisoformat(record["next_retry_at"]), NOW + timedelta(seconds=wait))
            record["next_retry_at"] = NOW.isoformat()
        start(record, NOW); self.assertEqual(fail(record, CodexRunError("HTTP 503 temporary"), NOW), "TERMINAL_FAILED")
        self.assertEqual(record["state"], "FAILED_TERMINAL"); self.assertFalse(eligible(record, NOW + timedelta(days=1)))

    def test_nonretryable_and_unknown_are_terminal(self):
        for message in ("missing approval configuration", "unclassified failure"):
            record = prepare(self.state(), "eod:x", "EOD", NOW, 3); start(record, NOW)
            self.assertEqual(fail(record, CodexRunError(message), NOW), "TERMINAL_FAILED")

    def test_stage_b_slot_terminal_and_later_slot_independent(self):
        state = self.state(); first = prepare(state, "stage:1", "STAGE_B", NOW, 1); start(first, NOW)
        fail(first, CodexRunError("application failure"), NOW)
        later = prepare(state, "stage:2", "STAGE_B", NOW + timedelta(minutes=10), 1)
        self.assertFalse(eligible(first, NOW + timedelta(hours=1))); self.assertTrue(eligible(later, NOW + timedelta(minutes=10)))

    def test_circuit_consecutive_and_total_thresholds(self):
        state = self.state()
        for index in range(3): opened = record_ai_failure(state, CodexRunError(str(index)), NOW)
        self.assertTrue(opened); self.assertEqual(state["ai_circuit"]["status"], "OPEN")
        state = self.state(); state["ai_circuit"]["consecutive_failures"] = 0
        for index in range(5):
            opened = record_ai_failure(state, CodexRunError(str(index)), NOW, 99, 5)
            state["ai_circuit"]["consecutive_failures"] = 0
        self.assertTrue(opened)

    def test_safe_diagnostic_has_counts_not_payloads(self):
        state = self.state(); record = prepare(state, "x", "EOD", NOW, 3); start(record, NOW)
        error = CodexRunError("token=secret account_id=123", diagnostics={"process_return_code": 7,
            "event_sequence": [{"event": "tool.started", "tool": "get_equity_historicals"}]})
        value = safe_failure_diagnostic(record, error, NOW, "TERMINAL_FAILED")
        encoded = json.dumps(value); self.assertEqual(value["sanitized_error"]["process_return_code"], 7)
        self.assertNotIn("secret", encoded); self.assertNotIn("123", encoded); self.assertNotIn("raw_result", encoded.lower())

    def test_failure_log_is_sanitized(self):
        stream = io.StringIO(); handler = logging.StreamHandler(stream); logger = logging.getLogger("ai_trader"); logger.addHandler(handler)
        try: logger.warning("AI_TRADER event=CYCLE_FAILED error_code=SAFE decision=TERMINAL_FAILED")
        finally: logger.removeHandler(handler)
        self.assertIn("CYCLE_FAILED", stream.getvalue()); self.assertNotIn("token", stream.getvalue())


class MaintenanceStalenessTests(unittest.TestCase):
    def test_legacy_and_prior_commit_queue_are_stale(self):
        for source in (None, "old"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / "state").mkdir(); gate = AIMaintenanceGate(root / "state/ai_maintenance.json")
                queue = {"trigger_class": "SOFTWARE_TEST_FAILURE", "failure_fingerprint": "f", "evidence": "x", "queued_at": NOW.isoformat()}
                if source: queue["source_git_commit"] = source
                gate.save({"version": 1, "failures": {"f": {"resolution_status": "QUEUED"}}, "queue": queue, "last_codex_attempt": None})
                with patch("trader.maintenance.git_head", return_value="new"):
                    self.assertEqual(process_ai_queue(root, root / "worktree"), 0)
                self.assertEqual(gate.load()["failures"]["f"]["resolution_status"], "STALE_NEEDS_REEVALUATION")


if __name__ == "__main__": unittest.main()
