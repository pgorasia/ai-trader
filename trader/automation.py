from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .market_calendar import ET
from .codex_events import sanitize_diagnostic_text
from .maintenance import LocalMaintenanceController
from .models import TraderError
from .state import atomic_write_json, validate_state_shape

HEARTBEAT_SCHEMA_VERSION = 1
HEARTBEAT_MAX_AGE = timedelta(minutes=3)
LOGGER = logging.getLogger("ai_trader")
_UNSAFE_DIAGNOSTIC_PAYLOAD = re.compile(
    r"(?is)\b(prompt|tool[_ ]?(?:arguments?|results?)|oauth|raw[_ ]?(?:stdout|stderr))\s*[:=].*$"
)


def _audit(event: str, **values: Any) -> None:
    LOGGER.info("AI_TRADER event=%s%s", event, "".join(f" {key}={value}" for key, value in values.items()))


def safe_daemon_error(error: Exception) -> dict[str, Any]:
    """Return only bounded, sanitized exception metadata suitable for persistence."""
    message = (sanitize_diagnostic_text(str(error)) if isinstance(error, TraderError)
               else "Local session operation failed")
    message = _UNSAFE_DIAGNOSTIC_PAYLOAD.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    return {"exception_class": type(error).__name__, "sanitized_error": {"message": message}}


def git_commit(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


class Heartbeat:
    def __init__(self, path: Path, root: Path, pid: int | None = None) -> None:
        self.path, self.root, self.pid = path, root, pid or os.getpid()
        self.values: dict[str, Any] = {
            "version": HEARTBEAT_SCHEMA_VERSION, "timestamp": "", "daemon_pid": self.pid,
            "mode": "SHADOW", "lifecycle_state": "STARTING",
            "last_preflight": None, "last_cycle": None,
            "last_eod": None, "next_scheduled_action": None,
            "last_local_health_check": None, "last_local_test_result": None,
            "git_commit": git_commit(root),
        }

    def update(self, lifecycle_state: str, next_action: datetime | None = None, **values: Any) -> None:
        self.values.update(values)
        self.values.update({
            "timestamp": datetime.now(timezone.utc).isoformat(), "daemon_pid": self.pid,
            "mode": "SHADOW", "lifecycle_state": lifecycle_state,
            "next_scheduled_action": next_action.isoformat() if next_action else None,
        })
        atomic_write_json(self.path, self.values)


class DaemonSupervisor:
    """Calendar-driven, interruptibly sleeping wrapper around Shadow sessions."""
    def __init__(self, orchestrator: Any, *, stop_event: threading.Event | None = None,
                 heartbeat: Heartbeat | None = None, preflight_lead_minutes: int = 10,
                 preflight_tolerance_minutes: int | None = None,
                 heartbeat_seconds: int = 60, local_maintenance: LocalMaintenanceController | None = None) -> None:
        self.orchestrator = orchestrator
        self.calendar = orchestrator.calendar
        self.stop_event = stop_event or threading.Event()
        self.heartbeat = heartbeat or Heartbeat(orchestrator.root / "state" / "heartbeat.json", orchestrator.root)
        self.preflight_lead_minutes = preflight_lead_minutes
        configured = orchestrator.config.get("schedule", {}).get("preflight_tolerance_minutes", 5) if hasattr(orchestrator, "config") else 5
        self.preflight_tolerance_minutes = int(configured if preflight_tolerance_minutes is None else preflight_tolerance_minutes)
        self.heartbeat_seconds = heartbeat_seconds
        self.local_maintenance = local_maintenance or LocalMaintenanceController(
            orchestrator.root, python="/home/ubuntu/.venvs/ai-trader/bin/python"
        )

    def install_signal_handlers(self) -> None:
        def stop(_signum: int, _frame: Any) -> None:
            self.stop_event.set()
            self.heartbeat.update("SHUTTING_DOWN")
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def wait_until(self, target: datetime, lifecycle: str) -> bool:
        while not self.stop_event.is_set():
            remaining = (target - self.orchestrator._trusted_now()).total_seconds()
            if remaining <= 0:
                return True
            self._run_local_maintenance()
            self.heartbeat.update(lifecycle, target)
            self.stop_event.wait(min(float(self.heartbeat_seconds), remaining))
        return False

    def run_forever(self) -> None:
        self.install_signal_handlers()
        self.orchestrator.stop_event = self.stop_event
        self.orchestrator.heartbeat_callback = self._session_heartbeat
        _audit("DAEMON_STARTED", mode="SHADOW")
        while not self.stop_event.is_set():
            now = self.orchestrator._trusted_now()
            if self._recover_expired_session(now):
                continue
            session = self._session_to_run(now)
            preflight_at = session.market_open - timedelta(minutes=self.preflight_lead_minutes)
            if now < preflight_at and not self.wait_until(preflight_at, "WAITING_FOR_PREFLIGHT"):
                break
            if self.stop_event.is_set():
                break
            state = self.orchestrator.store.load(session.session_date)
            terminal = [item for item in state.get("preflight_operations", [])
                        if item.get("status") in {"COMPLETED", "FAILED"}]
            if any(item["status"] == "FAILED" for item in terminal):
                self.heartbeat.update("SESSION_FAILED_CLOSED", session.eod_time)
                self.wait_until(session.eod_time + timedelta(seconds=1), "SESSION_FAILED_CLOSED")
                continue
            try:
                if not any(item["status"] == "COMPLETED" for item in terminal):
                    current = self.orchestrator._trusted_now()
                    if not self._preflight_eligible(session, current):
                        _audit("OPERATION_NOT_ELIGIBLE", operation="PREFLIGHT", reason="WINDOW_EXPIRED",
                               session=session.session_date)
                        next_session = self.calendar.next_session(session.eod_time + timedelta(seconds=1))
                        next_action = self._preflight_at(next_session)
                        _audit("WAITING_FOR_NEXT_SESSION", session=next_session.session_date)
                        _audit("NEXT_ACTION", scheduled_for=next_action.isoformat())
                        if not self.wait_until(next_action, "WAITING_FOR_NEXT_SESSION"):
                            break
                        continue
                    _audit("PREFLIGHT_START", session=session.session_date)
                    self.heartbeat.update("PREFLIGHT_RUNNING")
                    started = [item for item in state.get("preflight_operations", [])
                               if item.get("status") == "STARTED"]
                    operation_id = started[-1]["operation_id"] if started else None
                    self.orchestrator.preflight(state, self.orchestrator._trusted_now(),
                                                operation_id=operation_id)
                    self.orchestrator.store.save(state)
                    _audit("PREFLIGHT_PASS", session=session.session_date)
                passed = self.orchestrator._trusted_now().isoformat()
                self.heartbeat.update("SESSION_RUNNING", last_preflight=passed)
                self.orchestrator.run_session(preflight_already_passed=True)
                completed = self.orchestrator.store.load(session.session_date, create=False)
                last_cycle = completed["cycles"][-1].get("timestamp") if completed["cycles"] else None
                last_eod = completed.get("updated_at") if completed.get("eod_completed") else None
                self.heartbeat.update("SESSION_COMPLETE", last_cycle=last_cycle, last_eod=last_eod)
                _audit("SESSION_COMPLETE", session=session.session_date)
            except (TraderError, OSError, ValueError) as exc:
                diagnostic = safe_daemon_error(exc)
                safe_message = diagnostic["sanitized_error"]["message"]
                self.local_maintenance.record_application_failure(
                    "DAEMON_SESSION", f"{type(exc).__name__}:{safe_message}"
                )
                try:
                    failed = self.orchestrator.store.load(session.session_date, create=False)
                    failed.setdefault("errors", []).append({
                        "timestamp": self.orchestrator._trusted_now().isoformat(),
                        "category": "DAEMON_SESSION", **diagnostic, "resolved": False,
                    })
                    self.orchestrator.store.save(failed)
                except (TraderError, OSError, ValueError):
                    pass
                self.heartbeat.update("SESSION_FAILED_CLOSED", session.eod_time)
                self.wait_until(session.eod_time + timedelta(seconds=1), "SESSION_FAILED_CLOSED")
        self.heartbeat.update("STOPPED")

    def _session_heartbeat(self, next_action: datetime) -> None:
        self._run_local_maintenance()
        self.heartbeat.update("SESSION_RUNNING", next_action)

    def _run_local_maintenance(self) -> None:
        try:
            results = self.local_maintenance.run_due()
            if not results:
                return
            now = datetime.now(timezone.utc).isoformat()
            values: dict[str, Any] = {"last_local_health_check": now}
            tests = [results[name] for name in ("six_hour", "daily") if name in results]
            if tests:
                values["last_local_test_result"] = {
                    "timestamp": now, "passed": all(item["ok"] for item in tests),
                }
            self.heartbeat.update("LOCAL_MAINTENANCE", **values)
        except (OSError, ValueError, TraderError):
            self.heartbeat.update("LOCAL_MAINTENANCE_FAILED",
                                  last_local_health_check=datetime.now(timezone.utc).isoformat())

    def _session_to_run(self, now: datetime):
        """Select only a current/future session; expired sessions are never executable."""
        return self.calendar.next_session(now)

    def _preflight_at(self, session: Any) -> datetime:
        return session.market_open - timedelta(minutes=self.preflight_lead_minutes)

    def _preflight_eligible(self, session: Any, now: datetime) -> bool:
        start = self._preflight_at(session)
        return start <= now.astimezone(ET) <= start + timedelta(minutes=self.preflight_tolerance_minutes)

    @staticmethod
    def _eod_terminal(state: dict[str, Any]) -> bool:
        return bool(state.get("eod_completed")
                    or state.get("legacy_recovery_state") == "LEGACY_RECOVERY_FINALIZED")

    def _recover_expired_session(self, now: datetime) -> bool:
        """Recover post-close state, including bounded read-only EOD retries."""
        session = self.calendar.session_for(now.astimezone(ET).date())
        if session is None or now.astimezone(ET) < session.market_close:
            return False
        path = self.orchestrator.store.path_for(session.session_date)
        if not path.exists():
            return False
        state = self.orchestrator.store.load(session.session_date, create=False)
        if self._eod_terminal(state):
            return False
        eod_operation = next((item for item in state.get("ai_operations", [])
                              if item.get("operation_type") == "EOD"), None)
        if (eod_operation and eod_operation.get("state") == "RETRY_WAIT"
                and int(eod_operation.get("attempt_number", 0)) < int(eod_operation.get("max_attempts", 0))):
            retry_at = datetime.fromisoformat(eod_operation["next_retry_at"])
            if now < retry_at:
                if not self.wait_until(retry_at, "EOD_RETRY_WAIT"):
                    return True
                now = self.orchestrator._trusted_now()
            _audit("EOD_RETRY", session=session.session_date, attempt=int(eod_operation["attempt_number"]) + 1)
            try:
                self.orchestrator.eod(state, session)
            except TraderError:
                refreshed = self.orchestrator.store.load(session.session_date, create=False)
                current = next((item for item in refreshed.get("ai_operations", [])
                                if item.get("operation_type") == "EOD"), None)
                if current and current.get("state") == "RETRY_WAIT":
                    return True
                if not current or current.get("state") != "FAILED_TERMINAL":
                    raise
                state = refreshed
            else:
                return True
        _audit("POST_CLOSE_RECOVERY", session=session.session_date)
        circuit_open = state.get("ai_circuit", {}).get("status") == "OPEN"
        has_operation_structure = bool(state.get("ai_operations"))
        if circuit_open:
            status = "SKIPPED_CIRCUIT_OPEN"
        elif not has_operation_structure:
            _audit("LEGACY_SESSION_DETECTED", session=session.session_date)
            status = "LEGACY_RECOVERY_FINALIZED"
            state["legacy_recovery_state"] = status
            _audit("LEGACY_SESSION_FINALIZED", session=session.session_date, reason="MISSING_OPERATION_STATE")
        else:
            status = "POST_CLOSE_RECOVERY_FINALIZED"
            state["post_close_recovery_state"] = status
        state["eod_completed"] = True
        state["session_terminal"] = True
        terminal_eod = next((item for item in state.get("ai_operations", [])
                             if item.get("operation_type") == "EOD"
                             and item.get("state") == "FAILED_TERMINAL"), None)
        if terminal_eod:
            status = "POST_CLOSE_RECOVERY_FINALIZED_AFTER_AI_FAILURE"
            state["post_close_recovery_state"] = status
        state["eod_review"] = {"session_date": session.session_date, "status": status,
                               "metrics_retained": True, "recovery_reason": "STARTED_AFTER_MARKET_CLOSE",
                               "ai_eod_outcome": "FAILED_TERMINAL" if terminal_eod else "NOT_COMPLETED"}
        self.orchestrator.store.save(state)
        next_session = self.calendar.next_session(session.eod_time + timedelta(seconds=1))
        next_action = self._preflight_at(next_session)
        _audit("WAITING_FOR_NEXT_SESSION", session=next_session.session_date)
        _audit("NEXT_ACTION", scheduled_for=next_action.isoformat())
        self.heartbeat.update("WAITING_FOR_NEXT_SESSION", next_action)
        return True


def health_check(root: Path, *, now: datetime | None = None,
                 max_age: timedelta = HEARTBEAT_MAX_AGE) -> list[str]:
    """Offline health assessment; never constructs a Codex/Robinhood runner."""
    problems: list[str] = []
    heartbeat: dict[str, Any] = {}
    try:
        heartbeat = json.loads((root / "state" / "heartbeat.json").read_text(encoding="utf-8"))
        required = {"version", "timestamp", "daemon_pid", "mode", "lifecycle_state", "git_commit",
                    "last_preflight", "last_cycle", "last_eod", "last_local_health_check",
                    "last_local_test_result", "next_scheduled_action"}
        if not isinstance(heartbeat, dict) or not required <= heartbeat.keys() or heartbeat["version"] != HEARTBEAT_SCHEMA_VERSION:
            raise ValueError("invalid heartbeat schema")
        stamp = datetime.fromisoformat(heartbeat["timestamp"].replace("Z", "+00:00"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if current - stamp.astimezone(timezone.utc) > max_age:
            problems.append("heartbeat is stale")
        if heartbeat.get("mode") != "SHADOW":
            problems.append("heartbeat mode is not SHADOW")
        pid = int(heartbeat["daemon_pid"])
        if pid <= 0:
            raise ValueError("invalid daemon pid")
        os.kill(pid, 0)
    except ProcessLookupError:
        problems.append("daemon pid is not running")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        problems.append(f"heartbeat invalid: {type(exc).__name__}")
    try:
        lock = json.loads((root / ".runtime" / "orchestrator.lock").read_text(encoding="utf-8"))
        if int(lock.get("pid", -1)) != int(heartbeat.get("daemon_pid", -2)):
            problems.append("lock owner differs from daemon")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        problems.append("lock metadata invalid")
    try:
        import yaml
        config = yaml.safe_load((root / "config" / "strategy.yaml").read_text(encoding="utf-8"))
        if config.get("mode") != "SHADOW":
            problems.append("configured mode is not SHADOW")
    except (OSError, ValueError, AttributeError):
        problems.append("strategy configuration unavailable")
    try:
        for path in (root / "orchestrator.py", root / "trader" / "shadow_boundary.py", root / "AGENTS.md"):
            if not path.is_file():
                problems.append(f"required file missing: {path.name}")
        for path in (root / "state").glob("????-??-??.json"):
            validate_state_shape(json.loads(path.read_text(encoding="utf-8")), path.stem)
    except (OSError, ValueError, json.JSONDecodeError, TraderError):
        problems.append("state integrity check failed")
    usage = shutil.disk_usage(root)
    if usage.free / max(usage.total, 1) < 0.05:
        problems.append("disk critically full")
    if shutil.which("codex") is None:
        problems.append("Codex executable is not resolvable")
    return problems
