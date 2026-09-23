from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.install_runtime_supervisor import install
from tools.runtime_audit import _liveness
from tools.runtime_supervisor import RuntimeSupervisor


HEAD = "a" * 40


def result(code=0):
    return subprocess.CompletedProcess([], code)


def audit(code: int, action: str = "NONE", finding: str = "SCHEDULER_SILENCE") -> dict:
    names = {0: "HEALTHY", 10: "EXTERNAL_DEGRADED", 20: "NOT_ACTIONABLE",
             42: "INTERNAL_DEFECT", 43: "SAFETY_DEFECT"}
    return {"version": 2, "head": HEAD, "accepted_commit": HEAD, "exit_code": code,
            "classification": names[code], "findings": ([] if code == 0 else
            [{"code": finding, "severity": "SAFETY" if code == 43 else "INTERNAL", "detail": "bounded"}]),
            "remediation": {"action": action, "reason_code": "TEST"}}


class FakeCommands:
    def __init__(self, *, alive=True, restart=True, accepted=True, repair=42):
        self.calls = []
        self.alive, self.restart, self.accepted, self.repair = alive, restart, accepted, repair

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        joined = " ".join(str(item) for item in command)
        if "is-active" in joined:
            return result(0 if self.alive else 3)
        if " restart " in f" {joined} ":
            return result(0 if self.restart else 1)
        if "verify_deployment_accepted" in joined:
            return result(0 if self.accepted else 1)
        if "self_heal_repair.py" in joined:
            return result(self.repair)
        return result(0)


class RuntimeSupervisorPolicyTests(unittest.TestCase):
    def make(self, directory, audits, commands):
        root = Path(directory); repo = root / "repo"; state = repo / "state"
        state.mkdir(parents=True); journal = root / "journal.log"; journal.write_text("safe")
        values = iter(audits)
        return RuntimeSupervisor(repo=repo, state_dir=state, journal_file=journal,
            runtime_dir=root / "runtime", python="/python", confirmation_seconds=0,
            run=commands, sleep=lambda _seconds: None, audit_func=lambda: next(values))

    def test_internal_none_alive_never_stops_or_repairs(self):
        commands = FakeCommands(alive=True)
        with tempfile.TemporaryDirectory() as directory:
            status = self.make(directory, [audit(42), audit(42)], commands).execute()
        flat = [" ".join(x) for x in commands.calls]
        self.assertEqual(status, 42)
        self.assertFalse(any(" stop " in f" {x} " for x in flat))
        self.assertFalse(any("self_heal_repair.py" in x for x in flat))

    def test_restart_accepted_and_successful_has_no_repair(self):
        commands = FakeCommands()
        with tempfile.TemporaryDirectory() as directory:
            status = self.make(directory, [audit(42, "RESTART_SERVICE", "SERVICE_INACTIVE"), audit(0)], commands).execute()
        flat = [" ".join(x) for x in commands.calls]
        self.assertEqual(status, 0)
        self.assertTrue(any(" restart " in f" {x} " for x in flat))
        self.assertFalse(any("self_heal_repair.py" in x for x in flat))

    def test_failed_restart_confirmation_stops_and_escalates(self):
        commands = FakeCommands(repair=7)
        with tempfile.TemporaryDirectory() as directory:
            status = self.make(directory, [audit(42, "RESTART_SERVICE", "SERVICE_INACTIVE"), audit(42)], commands).execute()
        flat = [" ".join(x) for x in commands.calls]
        self.assertEqual(status, 7)
        self.assertTrue(any(" stop " in f" {x} " for x in flat))
        self.assertTrue(any("self_heal_repair.py" in x for x in flat))

    def test_safety_stops_and_repairs(self):
        commands = FakeCommands(repair=8)
        with tempfile.TemporaryDirectory() as directory:
            status = self.make(directory, [audit(43, "STOP_AND_REPAIR", "FOREIGN_MCP")], commands).execute()
        flat = [" ".join(x) for x in commands.calls]
        self.assertEqual(status, 8)
        self.assertTrue(any(" stop " in f" {x} " for x in flat))
        self.assertTrue(any("self_heal_repair.py" in x for x in flat))

    def test_external_degradation_is_record_only(self):
        commands = FakeCommands()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.make(directory, [audit(10)], commands).execute(), 0)
        self.assertEqual(commands.calls, [])

    def test_same_fingerprint_cooldown_prevents_repair_storm(self):
        commands = FakeCommands(repair=9)
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make(directory, [audit(43, "STOP_AND_REPAIR")], commands)
            self.assertEqual(supervisor.execute(), 9)
            supervisor.audit_func = lambda: audit(43, "STOP_AND_REPAIR")
            self.assertEqual(supervisor.execute(), 42)
        repairs = [x for x in commands.calls if "self_heal_repair.py" in " ".join(x)]
        self.assertEqual(len(repairs), 1)


class OperationAwareHeartbeatTests(unittest.TestCase):
    def repo(self, directory):
        repo = Path(directory); (repo / "config").mkdir()
        (repo / "config/strategy.yaml").write_text(
            "codex:\n  timeout_seconds: 240\nsupervision:\n  heartbeat_seconds: 60\n  missed_heartbeats: 3\n  operation_grace_seconds: 30\n")
        return repo

    def test_active_long_operation_inside_configured_deadline(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state = {"ai_operations": [{"operation_id": "sol:x", "operation_type": "SOL",
                "state": "STARTED", "started_at": (now - timedelta(seconds=200)).isoformat()}]}
            status, detail = _liveness(self.repo(directory), {"timestamp": (now - timedelta(seconds=220)).isoformat(),
                "daemon_pid": os.getpid()}, state, now)
        self.assertEqual(status, "ACTIVE_OPERATION")
        self.assertEqual(detail["operation_deadline_seconds"], 270)

    def test_stale_operation_beyond_deadline(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state = {"ai_operations": [{"operation_id": "sol:x", "operation_type": "SOL",
                "state": "STARTED", "started_at": (now - timedelta(seconds=271)).isoformat()}]}
            status, _ = _liveness(self.repo(directory), {"timestamp": (now - timedelta(seconds=300)).isoformat(),
                "daemon_pid": os.getpid()}, state, now)
        self.assertEqual(status, "STALE_HEARTBEAT")

    def test_dead_heartbeat_pid_is_service_inactive_state(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            status, _ = _liveness(self.repo(directory), {"timestamp": now.isoformat(),
                "daemon_pid": 999999999}, None, now)
        self.assertEqual(status, "DEAD_PROCESS")

    def test_sep22_sequence_is_active_not_scheduler_silence(self):
        observed = datetime.fromisoformat("2026-09-22T15:27:53.522311+00:00")
        with tempfile.TemporaryDirectory() as directory:
            state = {"ai_operations": [{"operation_id": "senior:2026-09-22-cycle-10",
                "operation_type": "SOL", "state": "STARTED",
                "started_at": "2026-09-22T11:27:29.493225-04:00"}]}
            status, _ = _liveness(self.repo(directory),
                {"timestamp": "2026-09-22T15:24:30+00:00", "daemon_pid": os.getpid()}, state, observed)
        self.assertEqual(status, "ACTIVE_OPERATION")


class SupervisorInstallTests(unittest.TestCase):
    def test_source_controlled_install_to_injected_root(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            installed = install(repo, Path(directory), production_repo=Path("/srv/ai-trader"),
                                python=Path("/venv/bin/python"))
            wrapper = installed[0].read_text()
            self.assertTrue(all(path.is_file() for path in installed))
            self.assertIn("/srv/ai-trader/tools/runtime_supervisor.py", wrapper)
            self.assertTrue(os.stat(installed[0]).st_mode & 0o100)
            self.assertIn("ExecStart=/usr/local/bin/ai-trader-supervisor-run", installed[1].read_text())


if __name__ == "__main__":
    unittest.main()
