from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from trader.automation import DaemonSupervisor, Heartbeat, health_check
from trader.maintenance import (candidate_policy, isolated_codex_environment,
                                promote, trading_active_or_imminent, validate_candidate,
                                AIMaintenanceGate, LocalMaintenanceController,
                                autonomous_promotion_allowed)
from trader.market_calendar import EquityMarketCalendar
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS
from trader.state import StateStore, initial_state

ROOT = Path(__file__).resolve().parents[1]


class LinuxAutomationTests(unittest.TestCase):
    def test_next_session_skips_holiday_and_weekend(self):
        calendar = EquityMarketCalendar("XNYS")
        session = calendar.next_session(datetime.fromisoformat("2026-11-26T10:00:00-05:00"))
        self.assertEqual(session.session_date, "2026-11-27")

    def test_next_session_handles_dst(self):
        calendar = EquityMarketCalendar("XNYS")
        winter = calendar.next_session(datetime.fromisoformat("2026-01-05T08:00:00-05:00"))
        summer = calendar.next_session(datetime.fromisoformat("2026-08-14T08:00:00-04:00"))
        self.assertEqual(winter.market_open.utcoffset(), timedelta(hours=-5))
        self.assertEqual(summer.market_open.utcoffset(), timedelta(hours=-4))

    def test_early_close_drives_all_offsets(self):
        session = EquityMarketCalendar("XNYS").session_for(date(2026, 11, 27))
        self.assertEqual((session.market_close.strftime("%H:%M"), session.latest_entry.strftime("%H:%M"),
                          session.mandatory_flat.strftime("%H:%M"), session.eod_time.strftime("%H:%M")),
                         ("13:00", "12:40", "12:55", "13:05"))

    def test_heartbeat_contains_safe_fields_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heartbeat = Heartbeat(root / "state/heartbeat.json", root, pid=os.getpid())
            heartbeat.update("WAITING")
            value = json.loads((root / "state/heartbeat.json").read_text())
            self.assertEqual(value["mode"], "SHADOW")
            self.assertFalse(any(word in json.dumps(value).lower() for word in ("account_id", "oauth", "balance", "token")))

    def _healthy_root(self, directory: str, stamp: datetime) -> Path:
        root = Path(directory)
        (root / "state").mkdir()
        (root / ".runtime").mkdir()
        (root / "config").mkdir()
        (root / "trader").mkdir()
        for name in ("orchestrator.py", "AGENTS.md"):
            (root / name).write_text("safe")
        (root / "trader/shadow_boundary.py").write_text("safe")
        (root / "config/strategy.yaml").write_text("mode: SHADOW\n")
        heartbeat = {
            "version": 1, "timestamp": stamp.isoformat(), "daemon_pid": os.getpid(),
            "mode": "SHADOW", "lifecycle_state": "WAITING", "git_commit": "abc",
            "last_preflight": None, "last_cycle": None, "last_eod": None,
            "last_local_health_check": None, "last_local_test_result": None,
            "next_scheduled_action": None,
        }
        (root / "state/heartbeat.json").write_text(json.dumps(heartbeat))
        (root / ".runtime/orchestrator.lock").write_text(json.dumps({"pid": os.getpid()}))
        return root

    def test_health_accepts_fresh_heartbeat_and_valid_lock(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch("trader.automation.shutil.which", return_value="/bin/codex"):
            self.assertEqual(health_check(self._healthy_root(directory, now), now=now), [])

    def test_health_rejects_stale_heartbeat(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch("trader.automation.shutil.which", return_value="/bin/codex"):
            problems = health_check(self._healthy_root(directory, now - timedelta(minutes=10)), now=now)
            self.assertIn("heartbeat is stale", problems)

    def test_restart_after_eod_does_not_select_expired_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state")
            state = initial_state("2026-08-14")
            operation_id = "preflight:test"
            state["preflight_operations"].append({
                "operation_id": operation_id, "started_at": "2026-08-14T09:20:00-04:00",
                "completed_at": "2026-08-14T09:21:00-04:00", "status": "COMPLETED",
                "report_artifact": "reports/test.json",
            })
            state["operation_ids"].append(operation_id)
            store.save(state)
            orchestrator = type("Orchestrator", (), {
                "root": root, "calendar": EquityMarketCalendar("XNYS"), "store": store,
            })()
            supervisor = DaemonSupervisor(orchestrator)
            recovered = supervisor._session_to_run(datetime.fromisoformat("2026-08-14T17:00:00-04:00"))
            self.assertEqual(recovered.session_date, "2026-08-17")

    def test_maintenance_refuses_market_activity_and_imminence(self):
        calendar = EquityMarketCalendar("XNYS")
        self.assertTrue(trading_active_or_imminent(datetime.fromisoformat("2026-08-14T09:10:00-04:00"), calendar))
        self.assertFalse(trading_active_or_imminent(datetime.fromisoformat("2026-08-14T23:30:00-04:00"), calendar))

    def test_maintenance_codex_home_has_no_robinhood_or_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            env, codex_home = isolated_codex_environment(worktree, {"PATH": "/bin", "ROBINHOOD_TOKEN": "secret"})
            try:
                config = (codex_home / "config.toml").read_text()
                self.assertNotIn("robinhood", config.lower())
                self.assertNotIn("ROBINHOOD_TOKEN", env)
                self.assertFalse((codex_home / "auth.json").exists())
                self.assertEqual(env["AI_TRADER_MAINTENANCE_WORKTREE"], str(worktree))
            finally:
                import shutil
                shutil.rmtree(codex_home)

    def test_validation_failure_blocks_candidate(self):
        failed = subprocess.CompletedProcess([], 1, "failed")
        with patch("trader.maintenance.run", return_value=failed):
            self.assertFalse(validate_candidate(ROOT, "/python")[0])

    def test_ai_gate_deduplicates_identical_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = AIMaintenanceGate(Path(directory) / "gate.json")
            now = datetime.fromisoformat("2026-08-14T23:30:00-04:00")
            self.assertTrue(gate.consider("SOFTWARE_TEST_FAILURE", "test_x failed", now,
                                         mode_shadow=True, trading_blocked=False)[0])
            self.assertFalse(gate.consider("SOFTWARE_TEST_FAILURE", "test_x failed", now + timedelta(days=2),
                                          mode_shadow=True, trading_blocked=False)[0])
            record = next(iter(gate.load()["failures"].values()))
            self.assertEqual(record["occurrence_count"], 2)

    def test_ai_gate_cooldown_and_changed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = AIMaintenanceGate(Path(directory) / "gate.json")
            now = datetime.fromisoformat("2026-08-14T23:30:00-04:00")
            gate.consider("SOFTWARE_TEST_FAILURE", "failure A", now,
                          mode_shadow=True, trading_blocked=False)
            self.assertFalse(gate.consider("SOFTWARE_TEST_FAILURE", "failure B", now + timedelta(hours=2),
                                          mode_shadow=True, trading_blocked=False)[0])
            self.assertTrue(gate.consider("SOFTWARE_TEST_FAILURE", "failure B", now + timedelta(hours=25),
                                         mode_shadow=True, trading_blocked=False)[0])

    def test_non_events_never_trigger_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = AIMaintenanceGate(Path(directory) / "gate.json")
            now = datetime.now(timezone.utc)
            for event in ("MARKET_CLOSED", "WEEKEND", "LOW_CPU", "NO_TRADE"):
                self.assertFalse(gate.consider(event, event, now, mode_shadow=True,
                                              trading_blocked=False)[0])
            self.assertIsNone(gate.queued())

    def test_market_window_blocks_approved_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = AIMaintenanceGate(Path(directory) / "gate.json")
            self.assertFalse(gate.consider(
                "SOFTWARE_TEST_FAILURE", "failed", datetime.now(timezone.utc),
                mode_shadow=True, trading_blocked=True,
            )[0])

    def test_local_schedules_never_execute_codex_or_network(self):
        calls = []
        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "abc\n")
        with tempfile.TemporaryDirectory() as directory, \
             patch("trader.maintenance.shutil.which", return_value="/bin/true"):
            root = Path(directory)
            (root / "state").mkdir()
            (root / ".runtime").mkdir()
            (root / ".runtime/orchestrator.lock").write_text(json.dumps({"pid": os.getpid()}))
            controller = LocalMaintenanceController(root, python="/python", command_runner=runner)
            self.assertTrue(controller._run_frequent(datetime.now(timezone.utc))[0])
            self.assertTrue(controller._run_half_hour(datetime.now(timezone.utc))[0])
            self.assertTrue(controller._run_six_hour(datetime.now(timezone.utc))[0])
            self.assertTrue(controller._run_daily(datetime.now(timezone.utc))[0])
        flattened = [" ".join(command) for command in calls]
        self.assertFalse(any(command.startswith("codex ") for command in flattened))
        self.assertFalse(any(word in command for command in flattened
                             for word in ("curl ", "wget ", "git fetch", "git pull", "ssh ")))

    def test_failed_daily_check_creates_approved_trigger_without_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state").mkdir()
            (root / ".runtime").mkdir()
            (root / "config").mkdir()
            (root / "config/strategy.yaml").write_text("mode: SHADOW\n")
            controller = LocalMaintenanceController(
                root, python="/python",
                now=lambda: datetime.fromisoformat("2026-08-14T23:30:00-04:00"),
            )
            controller._run_frequent = lambda _now: (True, "ok")
            controller._run_half_hour = lambda _now: (True, "ok")
            controller._run_six_hour = lambda _now: (True, "ok")
            controller._run_daily = lambda _now: (False, "unit suite failed")
            controller.run_due(force_daily=True)
            queue = controller.gate.queued()
            self.assertEqual(queue["trigger_class"], "SOFTWARE_TEST_FAILURE")

    def test_repeated_application_failure_triggers_only_at_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state").mkdir()
            (root / "config").mkdir()
            (root / "config/strategy.yaml").write_text("mode: SHADOW\n")
            controller = LocalMaintenanceController(
                root, python="/python",
                now=lambda: datetime.fromisoformat("2026-08-14T23:30:00-04:00"),
            )
            self.assertEqual(controller.record_application_failure("PROTOCOL", "same"), 1)
            self.assertIsNone(controller.gate.queued())
            self.assertEqual(controller.record_application_failure("PROTOCOL", "same"), 2)
            self.assertIsNone(controller.gate.queued())
            self.assertEqual(controller.record_application_failure("PROTOCOL", "same"), 3)
            self.assertEqual(controller.gate.queued()["trigger_class"],
                             "REPEATED_APPLICATION_FAILURE")

    def test_live_write_capability_disables_auto_promotion(self):
        tools = frozenset({"place_equity_order"} | {f"read_{index}" for index in range(21)})
        with patch("trader.maintenance.shadow_mode", return_value=True), \
             patch("trader.maintenance.APPROVED_SHADOW_ROBINHOOD_TOOLS", tools):
            allowed, _reason = autonomous_promotion_allowed(ROOT)
            self.assertFalse(allowed)

    def test_security_file_change_blocks_promotion(self):
        with patch("trader.maintenance.shadow_mode", return_value=True), \
             patch("trader.maintenance.changed_files", return_value=["trader/safety.py"]):
            self.assertFalse(candidate_policy(ROOT, "a", "b")[0])

    def test_non_shadow_blocks_promotion(self):
        with patch("trader.maintenance.shadow_mode", return_value=False):
            self.assertFalse(candidate_policy(ROOT, "a", "b")[0])

    def test_shadow_safe_candidate_is_eligible(self):
        completed = subprocess.CompletedProcess([], 0, "")
        with patch("trader.maintenance.shadow_mode", return_value=True), \
             patch("trader.maintenance.changed_files", return_value=["trader/automation.py"]), \
             patch("trader.maintenance.run", return_value=completed):
            self.assertTrue(candidate_policy(ROOT, "a", "b")[0])

    def test_rollback_on_failed_deployment(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if command[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, "old\n")
            if "--self-test" in command:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "")
        self.assertFalse(promote(ROOT, "candidate", command_runner=runner))
        self.assertIn(["git", "reset", "--hard", "old"], calls)

    def test_reports_and_agents_are_protected(self):
        for changed in (["reports/old.md"], ["AGENTS.md"], ["state/2026-01-01.json"]):
            with patch("trader.maintenance.shadow_mode", return_value=True), \
                 patch("trader.maintenance.changed_files", return_value=changed):
                self.assertFalse(candidate_policy(ROOT, "a", "b")[0])

    def test_boundary_remains_exactly_22_read_tools(self):
        self.assertEqual(len(APPROVED_SHADOW_ROBINHOOD_TOOLS), 22)
        self.assertFalse(any(name.startswith(("place_", "review_", "cancel_"))
                             for name in APPROVED_SHADOW_ROBINHOOD_TOOLS))

    def test_systemd_uses_absolute_paths_and_unprivileged_user(self):
        service = (ROOT / "deployment/systemd/ai-trader.service").read_text()
        self.assertIn("User=ubuntu", service)
        self.assertIn("ExecStart=/home/ubuntu/.venvs/ai-trader/bin/python", service)
        self.assertNotIn("User=root", service)
        self.assertIn("Restart=on-failure", service)

    def test_backup_excludes_credentials_and_venv(self):
        script = (ROOT / "scripts/backup-safe-state.sh").read_text()
        self.assertNotIn(".codex/auth.json", script)
        self.assertIn(r"\.venv", script)
        self.assertIn("Backup validation rejected", script)
        self.assertIn("state reports logs", script)


if __name__ == "__main__":
    unittest.main()
