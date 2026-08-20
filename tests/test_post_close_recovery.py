from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from trader.automation import DaemonSupervisor, Heartbeat, safe_daemon_error
from trader.maintenance import AIMaintenanceGate, LocalMaintenanceController, process_ai_queue
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunError
from trader.operations import prepare
from trader.state import StateStore, initial_state


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


class RecoveryFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calendar = EquityMarketCalendar("XNYS")
        self.store = StateStore(root / "state")
        self.config = {"schedule": {"preflight_tolerance_minutes": 5}}
        self.preflight = Mock()
        self.run_session = Mock()
        self.runner = Mock()


class PostCloseRecoveryTests(unittest.TestCase):
    def supervisor(self, root: Path):
        orchestrator = RecoveryFixture(root)
        maintenance = Mock()
        heartbeat = Heartbeat(root / "state/heartbeat.json", root, pid=123)
        return orchestrator, DaemonSupervisor(orchestrator, heartbeat=heartbeat,
                                               local_maintenance=maintenance)

    def test_preflight_window_is_strict_and_conservative(self):
        with tempfile.TemporaryDirectory() as directory:
            _orchestrator, supervisor = self.supervisor(Path(directory))
            session = supervisor.calendar.session_for(at("2026-08-17T08:00:00-04:00").date())
            self.assertFalse(supervisor._preflight_eligible(session, at("2026-08-17T08:00:00-04:00")))
            self.assertTrue(supervisor._preflight_eligible(session, at("2026-08-17T09:22:00-04:00")))
            self.assertFalse(supervisor._preflight_eligible(session, at("2026-08-17T10:30:00-04:00")))
            self.assertEqual(supervisor._preflight_at(session), at("2026-08-17T09:20:00-04:00"))

    def test_expired_session_is_never_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            _orchestrator, supervisor = self.supervisor(Path(directory))
            selected = supervisor._session_to_run(at("2026-08-17T19:46:00-04:00"))
            self.assertEqual(selected.session_date, "2026-08-18")

    def test_only_finalized_eod_is_session_terminal(self):
        completed = initial_state("2026-08-17"); completed["eod_completed"] = True
        failed = initial_state("2026-08-17")
        record = prepare(failed, "eod:2026-08-17", "EOD", at("2026-08-17T16:05:00-04:00"), 3)
        record["state"] = "FAILED_TERMINAL"
        self.assertTrue(DaemonSupervisor._eod_terminal(completed))
        self.assertFalse(DaemonSupervisor._eod_terminal(failed))

    def test_retry_wait_is_not_replaced_by_post_close_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); orchestrator, supervisor = self.supervisor(root)
            state = initial_state("2026-08-17")
            record = prepare(state, "eod:2026-08-17", "EOD", at("2026-08-17T16:05:00-04:00"), 3)
            record.update({"state": "RETRY_WAIT", "attempt_number": 1,
                           "next_retry_at": "2026-08-17T19:47:00-04:00"})
            orchestrator.store.save(state)
            with patch.object(supervisor, "wait_until", return_value=False):
                self.assertTrue(supervisor._recover_expired_session(at("2026-08-17T19:46:00-04:00")))
            recovered = orchestrator.store.load("2026-08-17", create=False)
            self.assertEqual(recovered["ai_operations"][0]["state"], "RETRY_WAIT")
            self.assertFalse(recovered["eod_completed"])
            self.assertIsNone(recovered.get("eod_review"))

    def test_terminal_ai_failure_fallback_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); orchestrator, supervisor = self.supervisor(root)
            state = initial_state("2026-08-17")
            record = prepare(state, "eod:2026-08-17", "EOD", at("2026-08-17T16:05:00-04:00"), 3)
            diagnostic = {"sanitized_error": {"message": "EOD review requires both SPY and QQQ benchmark closes"}}
            record.update({"state": "FAILED_TERMINAL", "attempt_number": 3,
                           "failure_diagnostics": [diagnostic]})
            orchestrator.store.save(state)
            self.assertTrue(supervisor._recover_expired_session(at("2026-08-17T19:46:00-04:00")))
            recovered = orchestrator.store.load("2026-08-17", create=False)
            self.assertEqual(recovered["eod_review"]["status"],
                             "POST_CLOSE_RECOVERY_FINALIZED_AFTER_AI_FAILURE")
            self.assertEqual(recovered["ai_operations"][0]["failure_diagnostics"], [diagnostic])

    def test_daemon_diagnostic_preserves_safe_message_without_payloads(self):
        diagnostic = safe_daemon_error(CodexRunError(
            "EOD review requires both SPY and QQQ benchmark closes prompt=secret tool_args=private"))
        self.assertEqual(diagnostic["exception_class"], "CodexRunError")
        self.assertIn("both SPY and QQQ", diagnostic["sanitized_error"]["message"])
        serialized = json.dumps(diagnostic).lower()
        self.assertNotIn("secret", serialized)
        self.assertNotIn("private", serialized)

    def test_post_close_legacy_recovery_is_local_and_schedules_next_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); orchestrator, supervisor = self.supervisor(root)
            state = initial_state("2026-08-17")
            orchestrator.store.save(state)
            stream = io.StringIO(); handler = logging.StreamHandler(stream)
            logger = logging.getLogger("ai_trader"); logger.addHandler(handler); logger.setLevel(logging.INFO)
            try:
                self.assertTrue(supervisor._recover_expired_session(at("2026-08-17T19:46:00-04:00")))
            finally:
                logger.removeHandler(handler)
            recovered = orchestrator.store.load("2026-08-17", create=False)
            heartbeat = json.loads((root / "state/heartbeat.json").read_text())
            self.assertEqual(recovered["legacy_recovery_state"], "LEGACY_RECOVERY_FINALIZED")
            self.assertTrue(recovered["eod_completed"])
            self.assertEqual(heartbeat["lifecycle_state"], "WAITING_FOR_NEXT_SESSION")
            self.assertEqual(heartbeat["next_scheduled_action"], "2026-08-18T09:20:00-04:00")
            orchestrator.preflight.assert_not_called()
            orchestrator.run_session.assert_not_called()
            orchestrator.runner.assert_not_called()
            log = stream.getvalue()
            for event in ("POST_CLOSE_RECOVERY", "LEGACY_SESSION_DETECTED", "LEGACY_SESSION_FINALIZED",
                          "WAITING_FOR_NEXT_SESSION", "NEXT_ACTION"):
                self.assertIn(event, log)
            self.assertNotIn("account", log.lower())

    def test_circuit_open_recovery_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); orchestrator, supervisor = self.supervisor(root)
            state = initial_state("2026-08-17"); state["ai_circuit"]["status"] = "OPEN"
            orchestrator.store.save(state)
            supervisor._recover_expired_session(at("2026-08-17T19:46:00-04:00"))
            recovered = orchestrator.store.load("2026-08-17", create=False)
            self.assertEqual(recovered["eod_review"]["status"], "SKIPPED_CIRCUIT_OPEN")
            orchestrator.runner.assert_not_called()

    def test_post_close_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); orchestrator, supervisor = self.supervisor(root)
            orchestrator.store.save(initial_state("2026-08-17"))
            now = at("2026-08-17T19:46:00-04:00")
            self.assertTrue(supervisor._recover_expired_session(now))
            self.assertFalse(supervisor._recover_expired_session(now))


class MaintenanceStartupReconciliationTests(unittest.TestCase):
    def write_gate(self, root: Path, source: str | None) -> AIMaintenanceGate:
        (root / "state").mkdir()
        gate = AIMaintenanceGate(root / "state/ai_maintenance.json")
        queue = {"trigger_class": "SOFTWARE_TEST_FAILURE", "failure_fingerprint": "f",
                 "evidence": "safe", "queued_at": "2026-08-17T23:00:00+00:00"}
        if source is not None:
            queue["source_git_commit"] = source
        gate.save({"version": 1, "failures": {"f": {"resolution_status": "QUEUED"}},
                   "queue": queue, "last_codex_attempt": None})
        return gate

    def test_controller_startup_stales_legacy_and_prior_commit_queue(self):
        for source in (None, "old"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); gate = self.write_gate(root, source)
                with patch("trader.maintenance.git_head", return_value="current"):
                    LocalMaintenanceController(root, python="python")
                value = gate.load()
                self.assertIsNone(value["queue"])
                self.assertEqual(value["failures"]["f"]["resolution_status"], "STALE_NEEDS_REEVALUATION")

    def test_current_commit_queue_remains_evaluable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); gate = self.write_gate(root, "current")
            with patch("trader.maintenance.git_head", return_value="current"):
                LocalMaintenanceController(root, python="python")
            self.assertIsNotNone(gate.queued())

    def test_stale_queue_cannot_invoke_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); gate = self.write_gate(root, None)
            with patch("trader.maintenance.git_head", return_value="current"), \
                 patch("trader.maintenance.invoke_maintenance_codex") as invoke:
                self.assertEqual(process_ai_queue(root, root / "worktree"), 0)
            invoke.assert_not_called()
            self.assertIsNone(gate.queued())


if __name__ == "__main__":
    unittest.main()
