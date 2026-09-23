from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.install_runtime_supervisor import install
from tools.runtime_audit import _liveness, _off_hours_idle_healthy, audit as runtime_audit
from tools.runtime_supervisor import RuntimeSupervisor, parser as supervisor_parser, supervisor_arguments
from trader.runtime_supervision import update_stability
from trader.state import atomic_write_json


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
        repair = next(x for x in commands.calls if "self_heal_repair.py" in " ".join(x))
        self.assertIn("--parent", repair)
        self.assertEqual(repair[repair.index("--parent") + 1], HEAD)
        self.assertIn("--output", repair)

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

    def audit_repo(self, directory, observed):
        repo = self.repo(directory)
        state = repo / "state"
        state.mkdir()
        artifact = {"version": 1, "mode": "SHADOW", "offline_gate": "PASS",
                    "live_read_only_gate": "PASS",
                    "live_run_counts": {"preflight": 5, "luna_schema": 5, "eod": 3},
                    "global_shadow_tool_count": 22, "accepted_git_commit": HEAD}
        (state / "reliability_acceptance.json").write_text(json.dumps(artifact))
        (state / "heartbeat.json").write_text(json.dumps({
            "timestamp": (observed - timedelta(minutes=10)).isoformat(),
            "daemon_pid": os.getpid(), "lifecycle_state": "WAITING_FOR_NEXT_SESSION"}))
        with patch("tools.runtime_audit.git_head", return_value=HEAD):
            return runtime_audit(repo, state, repo / "journal.log", now=observed)

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

    def test_2am_waiting_stale_live_pid_is_off_hours_idle_healthy(self):
        observed = datetime.fromisoformat("2026-09-23T06:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            status, detail = _liveness(self.repo(directory), {
                "timestamp": "2026-09-23T05:50:00+00:00", "daemon_pid": os.getpid(),
                "lifecycle_state": "WAITING_FOR_NEXT_SESSION"}, None, observed)
        self.assertTrue(_off_hours_idle_healthy(session_active=False,
            heartbeat={"lifecycle_state": "WAITING_FOR_NEXT_SESSION"}, active_plans=[],
            liveness=status, liveness_detail=detail))

    def test_2am_audit_has_no_internal_scheduler_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.audit_repo(directory,
                datetime.fromisoformat("2026-09-23T06:00:00+00:00"))
        self.assertEqual(result["liveness"]["state"], "IDLE_HEALTHY")
        self.assertNotIn("SCHEDULER_SILENCE", {x["code"] for x in result["findings"]})
        self.assertEqual(result["exit_code"], 20)

    def test_active_session_audit_retains_internal_scheduler_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.audit_repo(directory,
                datetime.fromisoformat("2026-09-23T14:00:00+00:00"))
        self.assertIn("SCHEDULER_SILENCE", {x["code"] for x in result["findings"]})
        self.assertEqual(result["exit_code"], 42)

    def test_active_session_stale_heartbeat_is_not_idle_healthy(self):
        self.assertFalse(_off_hours_idle_healthy(session_active=True,
            heartbeat={"lifecycle_state": "WAITING_FOR_NEXT_SESSION"}, active_plans=[],
            liveness="STALE_HEARTBEAT", liveness_detail={}))

    def test_off_hours_active_plan_never_receives_idle_exemption(self):
        self.assertFalse(_off_hours_idle_healthy(session_active=False,
            heartbeat={"lifecycle_state": "WAITING_FOR_NEXT_SESSION"},
            active_plans=[{"outcome": {"status": "PENDING"}}],
            liveness="STALE_HEARTBEAT", liveness_detail={}))

    def test_overdue_operation_never_receives_idle_exemption(self):
        self.assertFalse(_off_hours_idle_healthy(session_active=False,
            heartbeat={"lifecycle_state": "WAITING_FOR_NEXT_SESSION"}, active_plans=[],
            liveness="STALE_HEARTBEAT", liveness_detail={"operation_id": "sol:x"}))


class StabilityOwnershipTests(unittest.TestCase):
    def test_atomic_rewrite_preserves_readable_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "self_heal" / "stability.json"
            update_stability(path, session="2026-09-21", complete=True,
                internal_defect=False, external_degradation=False, recovery_correct=True,
                ownership_source=root)
            first = path.stat()
            update_stability(path, session="2026-09-22", complete=True,
                internal_defect=False, external_degradation=False, recovery_correct=True,
                ownership_source=root)
            second = path.stat()
            self.assertEqual((second.st_uid, second.st_gid), (first.st_uid, first.st_gid))
            self.assertEqual(second.st_mode & 0o777, 0o644)
            self.assertTrue(os.access(path, os.R_OK))

    def test_privileged_atomic_write_uses_repository_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state" / "self_heal" / "stability.json"
            calls = []
            real_fchown = os.fchown
            with patch("trader.state.os.geteuid", return_value=0), patch(
                    "trader.state.os.fchown",
                    side_effect=lambda fd, uid, gid: (calls.append((uid, gid)), real_fchown(fd, uid, gid))[1]):
                atomic_write_json(path, {"status": "BUILDING"}, ownership_source=root, mode=0o644)
            owner = root.stat()
            self.assertEqual(calls, [(owner.st_uid, owner.st_gid)])
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual((path.parent.stat().st_uid, path.parent.stat().st_gid),
                             (owner.st_uid, owner.st_gid))


class SupervisorInstallTests(unittest.TestCase):
    def invoke_installer(self, destination_root, *arguments):
        repo = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, str(repo / "tools/install_runtime_supervisor.py"),
             "--destination-root", str(destination_root), *arguments],
            capture_output=True, text=True, check=False,
        )

    def test_source_controlled_install_to_injected_root(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            installed = install(repo, Path(directory), production_repo=repo,
                                python=Path(sys.executable))
            wrapper = installed[0].read_text()
            self.assertTrue(all(path.is_file() for path in installed))
            self.assertIn(str(repo / "tools/runtime_supervisor.py"), wrapper)
            self.assertTrue(os.stat(installed[0]).st_mode & 0o100)
            self.assertIn("ExecStart=/usr/local/bin/ai-trader-supervisor-run", installed[1].read_text())

    def test_canonical_production_repo_cli_remains_supported(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke_installer(directory, "--production-repo", str(repo),
                                           "--python", sys.executable)
            self.assertEqual(result.returncode, 0, result.stderr)
            wrapper = (Path(directory) / "usr/local/bin/ai-trader-supervisor-run").read_text()
            self.assertIn(str(repo / "tools/runtime_supervisor.py"), wrapper)

    def test_repo_alias_and_install_flag_are_supported(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke_installer(directory, "--repo", str(repo),
                                           "--python", sys.executable, "--install")
            self.assertEqual(result.returncode, 0, result.stderr)
            repeated = self.invoke_installer(directory, "--repo", str(repo),
                                             "--python", sys.executable, "--install")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            wrapper = (Path(directory) / "usr/local/bin/ai-trader-supervisor-run").read_text()
            self.assertIn(str(repo / "tools/runtime_supervisor.py"), wrapper)

    def test_generated_supervisor_contract_and_safe_startup_self_test(self):
        repo = Path(__file__).resolve().parents[1]
        arguments = supervisor_arguments(repo=repo, python=sys.executable)
        parsed = supervisor_parser().parse_args([*arguments, "--self-test"])
        self.assertTrue(parsed.self_test)
        completed = subprocess.run([sys.executable, str(repo / "tools/runtime_supervisor.py"),
                                    *arguments, "--self-test"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_conflicting_repo_arguments_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke_installer(directory, "--production-repo", "/srv/canonical",
                                           "--repo", "/srv/different",
                                           "--python", "/venv/bin/python")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must specify the same path", result.stderr)

    def test_python_remains_required_with_repo_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke_installer(directory, "--repo", "/srv/compatibility")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--python", result.stderr)


if __name__ == "__main__":
    unittest.main()
