from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from .market_calendar import ET, EquityMarketCalendar
from .models import TraderError
from .shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, verify_shadow_mcp_boundary
from .state import StateStore, atomic_write_json

ACTIVE_ROOT = Path("/home/ubuntu/projects/ai-trader")
WORKTREE = Path("/home/ubuntu/projects/ai-trader-maintenance")
SENSITIVE = {
    "trader/shadow_boundary.py", "trader/safety.py", "trader/codex_runner.py",
    "trader/codex_events.py", "trader/readiness.py", "config/strategy.yaml",
}
FORBIDDEN_PREFIXES = ("reports/", ".codex/", "state/", "logs/")
FORBIDDEN_FILES = {"AGENTS.md"}
PERMITTED_PREFIXES = ("trader/", "tests/", "scripts/", "deployment/")
PERMITTED_FILES = {"orchestrator.py", "requirements.txt", "README.md", "RECOVERY.md"}
VALIDATION = (
    ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
    ["python", "orchestrator.py", "--self-test"],
    ["python", "-m", "compileall", "-q", "orchestrator.py", "trader", "tests"],
    ["python", "-m", "unittest", "tests.test_schema_compatibility", "-v"],
    ["git", "diff", "--check"],
)
APPROVED_AI_TRIGGERS = frozenset({
    "SOFTWARE_TEST_FAILURE", "REPEATED_APPLICATION_FAILURE", "TRUSTED_CODE_UPDATE",
    "LOCAL_INTEGRITY_FAILURE", "MANUAL_MAINTENANCE_REQUEST",
})
AI_COOLDOWN = timedelta(hours=24)
REPEATED_FAILURE_THRESHOLD = int(os.environ.get("AI_TRADER_FAILURE_THRESHOLD", "3"))
LOCAL_INTERVALS = {
    "frequent": timedelta(minutes=5), "half_hour": timedelta(minutes=30),
    "six_hour": timedelta(hours=6), "daily": timedelta(hours=24),
}
WRITE_CAPABILITY_PREFIXES = (
    "place_", "review_", "cancel_", "create_order", "modify_order",
    "exercise_", "replace_order", "submit_order",
)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


class AIMaintenanceGate:
    """Persistent fingerprint/cooldown gate. It never invokes Codex itself."""
    def __init__(self, path: Path, cooldown: timedelta = AI_COOLDOWN) -> None:
        self.path, self.cooldown = path, cooldown

    def consider(self, trigger_class: str, evidence: str, now: datetime, *,
                 mode_shadow: bool, trading_blocked: bool) -> tuple[bool, str]:
        if trigger_class not in APPROVED_AI_TRIGGERS:
            return False, "unapproved trigger"
        if not mode_shadow:
            return False, "mode is not SHADOW"
        if trading_blocked:
            return False, "trading active or imminent"
        normalized = " ".join(evidence.split())[:8000]
        fingerprint = hashlib.sha256(
            f"{trigger_class}\n{normalized}".encode("utf-8", "replace")
        ).hexdigest()
        data = self.load()
        records = data.setdefault("failures", {})
        record = records.get(fingerprint)
        current = now.astimezone(timezone.utc)
        if record is None:
            record = {
                "failure_fingerprint": fingerprint, "trigger_class": trigger_class,
                "first_seen": _stamp(current), "last_seen": _stamp(current),
                "occurrence_count": 1, "last_codex_attempt": None,
                "resolution_status": "UNINVESTIGATED",
            }
            records[fingerprint] = record
        else:
            record["last_seen"] = _stamp(current)
            record["occurrence_count"] = int(record["occurrence_count"]) + 1
        previous = _parse(record.get("last_codex_attempt"))
        global_previous = _parse(data.get("last_codex_attempt"))
        eligible = record["resolution_status"] == "UNINVESTIGATED"
        if previous is not None:
            eligible = False  # identical evidence requires evidence change, not merely time
        if global_previous is not None and current - global_previous < self.cooldown:
            eligible = False
        if eligible:
            source_commit = git_head(self.path.parent.parent)
            record["last_codex_attempt"] = _stamp(current)
            record["resolution_status"] = "QUEUED"
            data["last_codex_attempt"] = _stamp(current)
            data["queue"] = {
                "trigger_class": trigger_class, "failure_fingerprint": fingerprint,
                "evidence": normalized, "queued_at": _stamp(current),
                "source_git_commit": source_commit,
            }
        self.save(data)
        return eligible, fingerprint

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "failures": {}, "queue": None, "last_codex_attempt": None}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("failures"), dict):
            raise ValueError("invalid AI maintenance gate state")
        return value

    def save(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def queued(self) -> dict[str, Any] | None:
        return self.load().get("queue")

    def resolve(self, fingerprint: str, status: str) -> None:
        data = self.load()
        record = data["failures"].get(fingerprint)
        if record:
            record["resolution_status"] = status
        if data.get("queue", {}).get("failure_fingerprint") == fingerprint:
            data["queue"] = None
        self.save(data)


class LocalMaintenanceController:
    """Deterministic local checks. No method contains an AI, MCP, web, or fetch call."""
    def __init__(self, root: Path, *, python: str, now: Callable[[], datetime] | None = None,
                 command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> None:
        self.root, self.python, self.command_runner = root, python, command_runner or run
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.path = root / "state/local_maintenance.json"
        self.gate = AIMaintenanceGate(root / "state/ai_maintenance.json")
        self.calendar = EquityMarketCalendar("XNYS")

    def run_due(self, *, force_daily: bool = False) -> dict[str, Any]:
        lock_path = self.root / ".runtime/local-maintenance.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return {}
        try:
            return self._run_due_unlocked(force_daily=force_daily)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()

    def _run_due_unlocked(self, *, force_daily: bool = False) -> dict[str, Any]:
        current = self.now().astimezone(timezone.utc)
        state = self._load()
        results: dict[str, Any] = {}
        for name in ("frequent", "half_hour", "six_hour", "daily"):
            last = _parse(state["last_runs"].get(name))
            due = force_daily and name == "daily"
            due |= last is None or current - last >= LOCAL_INTERVALS[name]
            if not due:
                continue
            method = getattr(self, f"_run_{name}")
            ok, evidence = method(current)
            results[name] = {"ok": ok, "evidence": evidence}
            state["last_runs"][name] = _stamp(current)
            state["last_results"][name] = results[name]
            if not ok:
                trigger = "SOFTWARE_TEST_FAILURE" if name in {"six_hour", "daily"} else "LOCAL_INTEGRITY_FAILURE"
                self.gate.consider(trigger, f"{name}:{evidence}", current,
                                   mode_shadow=shadow_mode(self.root),
                                   trading_blocked=trading_active_or_imminent(current, self.calendar))
        state["updated_at"] = _stamp(current)
        atomic_write_json(self.path, state)
        return results

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "last_runs": {}, "last_results": {}, "updated_at": None}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1:
            raise ValueError("unsupported local maintenance state")
        return value

    def _run_frequent(self, _now: datetime) -> tuple[bool, str]:
        problems = []
        usage = shutil.disk_usage(self.root)
        if usage.free / max(usage.total, 1) < 0.05:
            problems.append("disk critically full")
        try:
            StateStore(self.root / "state").all_states()
            lock = json.loads((self.root / ".runtime/orchestrator.lock").read_text(encoding="utf-8"))
            pid = int(lock.get("pid", 0))
            if pid <= 0:
                problems.append("invalid daemon lock")
            else:
                os.kill(pid, 0)
        except ProcessLookupError:
            problems.append("daemon lock owner is not running")
        except (OSError, ValueError, TypeError, TraderError):
            problems.append("state or lock invalid")
        return not problems, "; ".join(problems) or "healthy"

    def _run_half_hour(self, now: datetime) -> tuple[bool, str]:
        commands = (
            ["git", "diff", "--check"], ["git", "status", "--porcelain=v1"],
        )
        for command in commands:
            completed = self.command_runner(command, cwd=self.root, check=False)
            if completed.returncode:
                return False, f"{' '.join(command)} failed"
        codex = shutil.which("codex")
        if codex is None or not os.access(codex, os.X_OK):
            return False, "Codex executable unavailable"
        session = self.calendar.next_session(now.astimezone(ET))
        return True, f"codex={codex};next={session.session_date}:{session.market_open.isoformat()}"

    def _run_six_hour(self, _now: datetime) -> tuple[bool, str]:
        self._prune_logs()
        commands: list[list[str]] = [
            [self.python, "-m", "compileall", "-q", "orchestrator.py", "trader"],
            [self.python, "-m", "unittest", "tests.test_phase1_security", "-q"],
        ]
        backups = Path("/home/ubuntu/ai-trader-backups")
        newest = max(backups.glob("ai-trader-safe-state-*.tar.gz"),
                     key=lambda path: path.stat().st_mtime, default=None)
        if newest is None or time.time() - newest.stat().st_mtime >= 24 * 60 * 60:
            commands.append([str(self.root / "scripts/backup-safe-state.sh")])
        return self._commands(commands)

    def _prune_logs(self) -> None:
        cutoff = time.time() - 30 * 24 * 60 * 60
        logs = self.root / "logs"
        if not logs.exists():
            return
        for path in logs.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()

    def _run_daily(self, _now: datetime) -> tuple[bool, str]:
        commands = (
            [self.python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
            [self.python, "orchestrator.py", "--self-test"],
            [self.python, "-m", "compileall", "-q", "orchestrator.py", "trader", "tests"],
            ["git", "fsck", "--no-dangling"], ["git", "diff", "--check"],
            [str(self.root / "scripts/backup-safe-state.sh")],
        )
        return self._commands(commands)

    def _commands(self, commands: Sequence[Sequence[str]]) -> tuple[bool, str]:
        forbidden = {"codex", "curl", "wget", "ssh", "git fetch", "git pull"}
        for command in commands:
            joined = " ".join(command)
            if any(item in joined for item in forbidden):
                return False, "external command prohibited in local maintenance"
            completed = self.command_runner(command, cwd=self.root, check=False)
            if completed.returncode:
                digest = hashlib.sha256(completed.stdout[-8000:].encode("utf-8", "replace")).hexdigest()
                return False, f"{joined} failed output_sha256={digest}"
        return True, "passed"

    def record_application_failure(self, category: str, evidence: str) -> int:
        """Count sanitized software failures and gate AI only at the repeat threshold."""
        current = self.now().astimezone(timezone.utc)
        normalized = " ".join(evidence.split())[:1000]
        fingerprint = hashlib.sha256(f"{category}\n{normalized}".encode()).hexdigest()
        path = self.root / "state/application_failures.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "version": 1, "failures": {},
        }
        record = data["failures"].setdefault(fingerprint, {
            "failure_fingerprint": fingerprint, "category": category,
            "first_seen": _stamp(current), "last_seen": _stamp(current), "occurrence_count": 0,
        })
        record["occurrence_count"] += 1
        record["last_seen"] = _stamp(current)
        atomic_write_json(path, data)
        if record["occurrence_count"] >= REPEATED_FAILURE_THRESHOLD:
            self.gate.consider(
                "REPEATED_APPLICATION_FAILURE", f"{category}:{normalized}", current,
                mode_shadow=shadow_mode(self.root),
                trading_blocked=trading_active_or_imminent(current, self.calendar),
            )
        return int(record["occurrence_count"])


def record_service_start(root: Path = ACTIVE_ROOT) -> None:
    controller = LocalMaintenanceController(
        root, python="/home/ubuntu/.venvs/ai-trader/bin/python"
    )
    path = root / "state/service_starts.json"
    now = datetime.now(timezone.utc)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "version": 1, "starts": [],
    }
    cutoff = now - timedelta(hours=1)
    starts = [item for item in data["starts"] if (_parse(item) or now) >= cutoff]
    starts.append(_stamp(now))
    data["starts"] = starts[-20:]
    atomic_write_json(path, data)
    if len(starts) >= REPEATED_FAILURE_THRESHOLD:
        controller.record_application_failure("SERVICE_RESTART_LOOP", "three or more starts within one hour")


def run(command: Sequence[str], *, cwd: Path, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check, timeout=1800, env=env)


def git_head(root: Path) -> str:
    try:
        result = run(["git", "rev-parse", "HEAD"], cwd=root)
        value = result.stdout.strip()
        return value if value else "UNKNOWN"
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def shadow_mode(root: Path) -> bool:
    try:
        return yaml.safe_load((root / "config/strategy.yaml").read_text(encoding="utf-8")).get("mode") == "SHADOW"
    except (OSError, AttributeError, yaml.YAMLError):
        return False


def trading_active_or_imminent(now: datetime, calendar: EquityMarketCalendar,
                               buffer: timedelta = timedelta(minutes=30)) -> bool:
    current = now.astimezone(ET)
    session = calendar.next_session(current)
    return session.market_open - timedelta(minutes=10) - buffer <= current <= session.eod_time + buffer


def changed_files(root: Path, base: str, candidate: str) -> list[str]:
    output = run(["git", "diff", "--name-only", f"{base}..{candidate}"], cwd=root).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def candidate_policy(root: Path, base: str, candidate: str) -> tuple[bool, str]:
    active_config = Path("/home/ubuntu/.codex/config.toml")
    allowed, reason = autonomous_promotion_allowed(
        root, active_config if active_config.exists() else None
    )
    if not allowed:
        return False, reason
    files = changed_files(root, base, candidate)
    if not files:
        return False, "candidate has no changes"
    if any(path in FORBIDDEN_FILES or path.startswith(FORBIDDEN_PREFIXES) for path in files):
        return False, "candidate changes protected state, reports, credentials, logs, or AGENTS.md"
    if any(path not in PERMITTED_FILES and not path.startswith(PERMITTED_PREFIXES) for path in files):
        return False, "candidate changes a file outside the automatic-promotion allow-list"
    if any(path in SENSITIVE for path in files):
        return False, "security-sensitive files are not eligible for automatic promotion"
    deleted_tests = run(["git", "diff", "--diff-filter=D", "--name-only", f"{base}..{candidate}", "--", "tests"], cwd=root).stdout.strip()
    if deleted_tests:
        return False, "candidate deletes tests"
    modified_tests = run(
        ["git", "diff", "--diff-filter=M", "--name-only", f"{base}..{candidate}", "--", "tests"],
        cwd=root,
    ).stdout.strip()
    if modified_tests:
        return False, "automatic candidates may add tests but may not modify existing tests"
    if len(APPROVED_SHADOW_ROBINHOOD_TOOLS) != 22 or "place_" in "\n".join(APPROVED_SHADOW_ROBINHOOD_TOOLS):
        return False, "global Robinhood Shadow boundary invariant failed"
    return True, "eligible"


def autonomous_promotion_allowed(root: Path, codex_config: Path | None = None) -> tuple[bool, str]:
    """Hard future-live interlock for every automatic deployment path."""
    if not shadow_mode(root):
        return False, "autonomous promotion disabled outside SHADOW"
    if len(APPROVED_SHADOW_ROBINHOOD_TOOLS) != 22:
        return False, "Robinhood Shadow allow-list invariant changed"
    if any(name.startswith(WRITE_CAPABILITY_PREFIXES) for name in APPROVED_SHADOW_ROBINHOOD_TOOLS):
        return False, "brokerage write capability is present"
    if codex_config is not None:
        try:
            # Promotion verifies the immutable global boundary. Full scheduled
            # approval coverage is a daemon-startup concern.
            boundary = verify_shadow_mcp_boundary(codex_config, require_unattended_approvals=False)
        except Exception:
            return False, "active Robinhood boundary verification failed"
        if any(name.startswith(WRITE_CAPABILITY_PREFIXES) for name in boundary.enabled_tools):
            return False, "active brokerage write capability is present"
    return True, "SHADOW read-only boundary"


def validate_candidate(worktree: Path, python: str) -> tuple[bool, str]:
    for command in VALIDATION:
        resolved = [python if item == "python" else item for item in command]
        completed = run(resolved, cwd=worktree, check=False)
        if completed.returncode:
            return False, f"validation failed: {' '.join(command)}\n{completed.stdout[-4000:]}"
    return True, "validated"


def isolated_codex_environment(worktree: Path, base_env: dict[str, str] | None = None) -> tuple[dict[str, str], Path]:
    """Create an MCP-free Codex home. The caller removes it after the job."""
    source = os.environ if base_env is None else base_env
    codex_home = Path(tempfile.mkdtemp(prefix="ai-trader-maintenance-codex-", dir="/tmp"))
    (codex_home / "config.toml").write_text(
        'approval_policy = "never"\nsandbox_mode = "workspace-write"\n', encoding="utf-8"
    )
    env = {key: value for key, value in source.items()
           if key not in {"OPENAI_API_KEY", "ROBINHOOD_OAUTH", "ROBINHOOD_TOKEN"}}
    env.update({"CODEX_HOME": str(codex_home), "HOME": str(codex_home),
                "AI_TRADER_MAINTENANCE_WORKTREE": str(worktree)})
    return env, codex_home


def invoke_maintenance_codex(worktree: Path, queue: dict[str, Any], *,
                             executable: str = "codex") -> subprocess.CompletedProcess[str]:
    """Trusted Codex child only; isolated HOME has authentication but no MCP config."""
    env, codex_home = isolated_codex_environment(worktree)
    source_auth = Path("/home/ubuntu/.codex/auth.json")
    try:
        if not source_auth.is_file():
            return subprocess.CompletedProcess([executable], 70, "Codex authentication unavailable")
        target_auth = codex_home / "auth.json"
        shutil.copyfile(source_auth, target_auth)
        target_auth.chmod(0o600)
        prompt = (
            "Repair this offline SHADOW-only trading research codebase. Robinhood, MCP, web, "
            "and network access are unavailable. Work only in this worktree. Do not modify "
            "AGENTS.md, reports, state, logs, credentials, the Robinhood allow-list, or "
            "weaken/delete tests. Make the smallest safe patch for this evidence:\n"
            + queue["trigger_class"] + "\n" + queue["evidence"]
        )
        command = [
            executable, "exec", "--sandbox", "workspace-write", "--cd", str(worktree),
            "--ephemeral", "--disable", "multi_agent", "--disable", "multi_agent_v2",
            "--disable", "browser_use", "--disable", "browser_use_external",
            "--disable", "standalone_web_search", "--json", "-",
        ]
        return subprocess.run(command, input=prompt, cwd=worktree, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=1800, check=False)
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)


def process_ai_queue(active: Path = ACTIVE_ROOT, worktree: Path = WORKTREE) -> int:
    gate = AIMaintenanceGate(active / "state/ai_maintenance.json")
    queue = gate.queued()
    if queue is None:
        return 0
    current_commit = git_head(active)
    if not queue.get("source_git_commit") or queue.get("source_git_commit") != current_commit:
        gate.resolve(queue["failure_fingerprint"], "STALE_NEEDS_REEVALUATION")
        return 0
    now = datetime.now(ET)
    if not shadow_mode(active) or trading_active_or_imminent(now, EquityMarketCalendar("XNYS")):
        return 30
    if worktree.exists():
        return 31
    base = current_commit
    target = base
    if queue["trigger_class"] == "TRUSTED_CODE_UPDATE":
        target = queue["evidence"]
        ancestor = run(["git", "merge-base", "--is-ancestor", base, target], cwd=active, check=False)
        if ancestor.returncode:
            gate.resolve(queue["failure_fingerprint"], "REJECTED_UNTRUSTED_UPDATE")
            return 32
    run(["git", "worktree", "add", "--detach", str(worktree), target], cwd=active)
    try:
        if queue["trigger_class"] != "TRUSTED_CODE_UPDATE":
            result = invoke_maintenance_codex(worktree, queue)
            if result.returncode:
                gate.resolve(queue["failure_fingerprint"], "CODEX_FAILED")
                return 33
            status = run(["git", "status", "--porcelain=v1"], cwd=worktree).stdout.strip()
            if not status:
                gate.resolve(queue["failure_fingerprint"], "NO_PATCH")
                return 0
            run(["git", "add", "--", "."], cwd=worktree)
            run(["git", "-c", "user.name=AI Trader Maintenance",
                 "-c", "user.email=maintenance@localhost", "commit",
                 "-m", f"maintenance: {queue['trigger_class'].lower()}"], cwd=worktree)
            target = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        eligible, reason = candidate_policy(active, base, target)
        if not eligible:
            gate.resolve(queue["failure_fingerprint"], f"REJECTED:{reason}")
            return 34
        valid, _details = validate_candidate(worktree, "/home/ubuntu/.venvs/ai-trader/bin/python")
        if not valid:
            gate.resolve(queue["failure_fingerprint"], "VALIDATION_FAILED")
            return 35
        if not promote(active, target):
            gate.resolve(queue["failure_fingerprint"], "DEPLOYMENT_ROLLED_BACK")
            return 36
        gate.resolve(queue["failure_fingerprint"], "PROMOTED")
        return 0
    finally:
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=active, check=False)


def promote(active: Path, candidate: str, *, service: str = "ai-trader.service",
            command_runner: Callable[..., subprocess.CompletedProcess[str]] = run) -> bool:
    allowed, _reason = autonomous_promotion_allowed(active)
    if not allowed:
        return False
    old = command_runner(["git", "rev-parse", "HEAD"], cwd=active).stdout.strip()
    command_runner(["sudo", "-n", "/usr/bin/systemctl", "stop", service], cwd=active)
    try:
        command_runner(["git", "merge", "--ff-only", candidate], cwd=active)
        command_runner(["/home/ubuntu/.venvs/ai-trader/bin/python", "orchestrator.py", "--self-test"], cwd=active)
        command_runner(["sudo", "-n", "/usr/bin/systemctl", "start", service], cwd=active)
        healthy = False
        for _attempt in range(18):
            health = command_runner(
                ["/home/ubuntu/.venvs/ai-trader/bin/python", "orchestrator.py", "--health-check"],
                cwd=active, check=False,
            )
            if health.returncode == 0:
                healthy = True
                break
            time.sleep(5)
        if not healthy:
            raise RuntimeError("post-deploy health check failed")
        return True
    except Exception:
        command_runner(["git", "reset", "--hard", old], cwd=active)
        command_runner(["sudo", "-n", "/usr/bin/systemctl", "start", service], cwd=active, check=False)
        return False


def maintenance_once(active: Path = ACTIVE_ROOT, worktree: Path = WORKTREE) -> int:
    controller = LocalMaintenanceController(active, python="/home/ubuntu/.venvs/ai-trader/bin/python")
    controller.run_due(force_daily=True)
    return process_ai_queue(active, worktree)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily", "--local", dest="local", action="store_true")
    parser.add_argument("--process-ai-queue", action="store_true")
    parser.add_argument("--manual-trigger")
    parser.add_argument("--record-service-start", action="store_true")
    args = parser.parse_args(argv)
    if args.manual_trigger:
        AIMaintenanceGate(ACTIVE_ROOT / "state/ai_maintenance.json").consider(
            "MANUAL_MAINTENANCE_REQUEST", args.manual_trigger, datetime.now(timezone.utc),
            mode_shadow=shadow_mode(ACTIVE_ROOT),
            trading_blocked=trading_active_or_imminent(datetime.now(ET), EquityMarketCalendar("XNYS")),
        )
    if args.local:
        LocalMaintenanceController(
            ACTIVE_ROOT, python="/home/ubuntu/.venvs/ai-trader/bin/python"
        ).run_due(force_daily=True)
    if args.process_ai_queue:
        return process_ai_queue()
    if args.record_service_start:
        record_service_start()
    return 0 if (args.local or args.manual_trigger or args.record_service_start) else 2


if __name__ == "__main__":
    raise SystemExit(main())
