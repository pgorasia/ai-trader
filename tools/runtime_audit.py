#!/usr/bin/env python3
"""Offline-first runtime auditor. Never imports or calls brokerage tooling."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from trader.lifecycle import ENTERED_PERSISTED_STATES, session_invariants
from trader.market_calendar import ET, EquityMarketCalendar
from trader.runtime_supervision import classify, remediation_decision, update_stability
from trader.state import atomic_write_json

REQUIRED_ACCEPTANCE_COUNTS = {"preflight": 5, "luna_schema": 5, "eod": 3}
DEFAULT_HEARTBEAT_SECONDS = 60
DEFAULT_MISSED_HEARTBEATS = 3
DEFAULT_OPERATION_GRACE_SECONDS = 30


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _liveness(repo: Path, heartbeat: dict, latest_state: dict | None,
              observed: datetime) -> tuple[str, dict]:
    """Classify daemon liveness using its configured cadence and operation deadline."""
    stamp = datetime.fromisoformat(str(heartbeat["timestamp"]).replace("Z", "+00:00"))
    pid = int(heartbeat["daemon_pid"])
    age = (observed.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
    if not _process_alive(pid):
        return "DEAD_PROCESS", {"heartbeat_age_seconds": age, "daemon_pid": pid}
    try:
        config = yaml.safe_load((repo / "config/strategy.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        config = {}
    supervision = config.get("supervision", {})
    heartbeat_seconds = max(1, int(supervision.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)))
    missed = max(2, int(supervision.get("missed_heartbeats", DEFAULT_MISSED_HEARTBEATS)))
    idle_deadline = heartbeat_seconds * missed
    active = None
    if latest_state:
        active = next((item for item in reversed(latest_state.get("ai_operations", []))
                       if item.get("state") == "STARTED" and item.get("started_at")), None)
    if active:
        started = datetime.fromisoformat(str(active["started_at"]).replace("Z", "+00:00"))
        elapsed = (observed.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        configured_timeout = max(1, int(config.get("codex", {}).get("timeout_seconds", 240)))
        grace = max(0, int(supervision.get("operation_grace_seconds", DEFAULT_OPERATION_GRACE_SECONDS)))
        deadline = configured_timeout + grace
        detail = {"heartbeat_age_seconds": age, "operation_id": active.get("operation_id"),
                  "operation_type": active.get("operation_type"),
                  "operation_elapsed_seconds": elapsed, "operation_deadline_seconds": deadline}
        return ("ACTIVE_OPERATION" if elapsed <= deadline else "STALE_HEARTBEAT"), detail
    return ("IDLE_HEALTHY" if age <= idle_deadline else "STALE_HEARTBEAT"), {
        "heartbeat_age_seconds": age, "idle_deadline_seconds": idle_deadline,
    }


def git_head(repo: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def audit(repo: Path, state_dir: Path, journal_file: Path, *, now: datetime | None = None) -> dict:
    observed = (now or datetime.now(timezone.utc)).astimezone()
    session = EquityMarketCalendar("XNYS").session_for(observed.astimezone(ET).date())
    session_active = bool(
        session
        and session.market_open <= observed.astimezone(ET) <= session.eod_time
    )
    findings: list[dict] = []
    head = git_head(repo)
    accepted = None
    try:
        artifact = json.loads((repo / "state/reliability_acceptance.json").read_text(encoding="utf-8"))
        accepted = artifact.get("accepted_git_commit")
        acceptance_valid = (artifact.get("version") == 1 and artifact.get("mode") == "SHADOW"
                            and artifact.get("offline_gate") == "PASS"
                            and artifact.get("live_read_only_gate") == "PASS"
                            and artifact.get("live_run_counts") == REQUIRED_ACCEPTANCE_COUNTS
                            and artifact.get("global_shadow_tool_count") == 22
                            and accepted == head)
    except (OSError, UnicodeError, json.JSONDecodeError):
        acceptance_valid = False
    if not acceptance_valid:
        findings.append({"code": "DEPLOYMENT_NOT_ACCEPTED", "severity": "INTERNAL",
                         "detail": "HEAD is not bound to a valid canonical acceptance"})

    states = sorted(state_dir.glob("????-??-??.json")) if state_dir.is_dir() else []
    latest_state = None
    if not states:
        findings.append({"code": "NO_SESSION_STATE", "severity": "NOT_ACTIONABLE", "detail": "no persisted session state"})
    else:
        path = states[-1]
        try:
            state = json.loads(path.read_text(encoding="utf-8")); latest_state = state
            # Runtime cutoff/mandatory-flat invariants are evaluated at the
            # trusted audit time for today's session, not at the last write.
            # Otherwise a disappeared scheduler can leave an old coherent
            # timestamp that hides a presently stale plan forever.
            state_day = str(state.get("session_date", ""))
            invariant_as_of = observed if state_day == observed.astimezone(ET).date().isoformat() else datetime.fromisoformat(
                str(state.get("updated_at")).replace("Z", "+00:00"))
            findings.extend(session_invariants(state, as_of=invariant_as_of))
            counts = state.get("usage_counts", {})
            if int(counts.get("codex_failed_attempts", 0)) > int(counts.get("codex_subprocess_attempts", 0)):
                findings.append({"code": "IMPOSSIBLE_CODEX_COUNTERS", "severity": "INTERNAL",
                                 "detail": "failed subprocess attempts exceed all subprocess attempts"})
            for error in state.get("errors", []):
                message = json.dumps(error, sort_keys=True).lower()
                if any(token in message for token in ("dataunavailable", "data_unavailable", "capacity", "http 502")):
                    findings.append({"code": "EXTERNAL_DEGRADATION", "severity": "EXTERNAL",
                                     "detail": "bounded external failure evidence present"})
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            findings.append({"code": "STATE_UNREADABLE", "severity": "INTERNAL", "detail": type(exc).__name__})

    heartbeat = None
    liveness = "DEAD_PROCESS"
    liveness_detail: dict = {}
    try:
        heartbeat = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
        liveness, liveness_detail = _liveness(repo, heartbeat, latest_state, observed)
        if liveness == "STALE_HEARTBEAT":
            findings.append({"code": "SCHEDULER_SILENCE", "severity": "INTERNAL",
                             "detail": "heartbeat exceeded its configured liveness deadline"})
        elif liveness == "DEAD_PROCESS":
            findings.append({"code": "SERVICE_INACTIVE", "severity": "INTERNAL", "detail": "heartbeat PID is not alive"})
        if not heartbeat.get("lifecycle_state"):
            findings.append({"code": "SCHEDULER_REASON_MISSING", "severity": "INTERNAL", "detail": "heartbeat lacks lifecycle reason"})
        active_plans = [] if latest_state is None else [
            plan for plan in latest_state.get("shadow_plans", [])
            if str(plan.get("outcome", {}).get("status", "")) in ({"PENDING"} | ENTERED_PERSISTED_STATES)
        ]
        if active_plans and heartbeat.get("lifecycle_state") != "SESSION_RUNNING":
            findings.append({"code": "MONITOR_DISAPPEARANCE", "severity": "INTERNAL",
                             "detail": "active lifecycle plan lacks SESSION_RUNNING supervision"})
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        findings.append({"code": "SERVICE_INACTIVE" if session_active else "HEARTBEAT_UNAVAILABLE",
                         "severity": "INTERNAL" if session_active else "NOT_ACTIONABLE",
                         "detail": ("required active-session heartbeat unavailable or malformed"
                                    if session_active else "heartbeat unavailable or malformed")})
    if not journal_file.exists():
        findings.append({"code": "JOURNAL_UNAVAILABLE", "severity": "NOT_ACTIONABLE", "detail": "journal file absent"})
    elif latest_state and latest_state.get("eod_completed") is True:
        try:
            # The service log is append-only.  Bound the audit read while
            # retaining enough recent history to verify the latest session.
            with journal_file.open("rb") as source:
                source.seek(0, os.SEEK_END)
                source.seek(max(0, source.tell() - 1_048_576))
                journal_tail = source.read().decode("utf-8", errors="replace")
            marker = f"event=SESSION_COMPLETE session={latest_state['session_date']}"
            if marker not in journal_tail:
                findings.append({"code": "EOD_JOURNAL_EVENT_MISSING", "severity": "INTERNAL",
                                 "detail": "completed EOD lacks bounded session-complete journal evidence"})
        except OSError as exc:
            findings.append({"code": "JOURNAL_UNREADABLE", "severity": "NOT_ACTIONABLE",
                             "detail": type(exc).__name__})
    exit_code = classify(findings)
    restart = remediation_decision(service_inactive=any(x["code"] == "SERVICE_INACTIVE" for x in findings),
        session_active=session_active, accepted=accepted == head and head is not None, acceptance_valid=acceptance_valid,
        safety_defect=exit_code == 43)
    return {"version": 2, "observed_at": observed.isoformat(), "head": head, "accepted_commit": accepted,
            "findings": findings, "classification": {0: "HEALTHY", 10: "EXTERNAL_DEGRADED", 20: "NOT_ACTIONABLE",
                42: "INTERNAL_DEFECT", 43: "SAFETY_DEFECT"}[exit_code], "exit_code": exit_code,
            "remediation": restart, "liveness": {"state": liveness if heartbeat else "DEAD_PROCESS",
                **(liveness_detail if heartbeat else {})},
            "latest_session_complete": bool(latest_state and latest_state.get("eod_completed"))}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(); p.add_argument("--repo", required=True, type=Path)
    p.add_argument("--state-dir", required=True, type=Path); p.add_argument("--journal-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path); p.add_argument("--remediate", action="store_true")
    p.add_argument("--service", default="ai-trader.service")
    return p


def main() -> int:
    args = parser().parse_args()
    result = audit(args.repo, args.state_dir, args.journal_file)
    if args.remediate and result["remediation"]["action"] == "RESTART_SERVICE":
        completed = subprocess.run(["systemctl", "restart", args.service], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result["remediation"]["result"] = "RESTARTED" if completed.returncode == 0 else "RESTART_FAILED"
        if completed.returncode:
            result["findings"].append({"code": "SAFE_RESTART_FAILED", "severity": "INTERNAL", "detail": "systemd restart failed"})
            result["exit_code"] = 42; result["classification"] = "INTERNAL_DEFECT"
    if result.get("latest_session_complete"):
        latest = sorted(args.state_dir.glob("????-??-??.json"))[-1].stem
        update_stability(args.repo / "state/self_heal/stability.json", session=latest, complete=True,
            internal_defect=result["exit_code"] in {42, 43}, external_degradation=result["exit_code"] == 10,
            recovery_correct=not any(x["code"] in {"SCHEDULER_SILENCE", "SERVICE_INACTIVE"} for x in result["findings"]))
    atomic_write_json(args.output, result)
    return int(result["exit_code"])


if __name__ == "__main__": raise SystemExit(main())
