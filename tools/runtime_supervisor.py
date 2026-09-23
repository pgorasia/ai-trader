#!/usr/bin/env python3
"""Source-controlled deterministic supervisor policy; never imports brokerage code."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from trader.state import atomic_write_json
from tools.runtime_audit import parser as runtime_audit_parser
from tools.self_heal_repair import parser as repair_parser

PASSIVE_EXITS = frozenset({0, 10, 20})


def runtime_audit_command(*, python: str, repo: Path, state_dir: Path,
                          journal_file: Path, output: Path) -> list[str]:
    """Canonical runtime-auditor launch contract."""
    return [python, str(repo / "tools/runtime_audit.py"), "--repo", str(repo),
            "--state-dir", str(state_dir), "--journal-file", str(journal_file),
            "--output", str(output)]


def supervisor_arguments(*, repo: Path, python: str) -> list[str]:
    """Canonical arguments shared by the installer and supervisor parser."""
    return ["--repo", str(repo), "--state-dir", str(repo / "state"),
            "--journal-file", "/var/lib/ai-trader-supervisor/trader-journal.log",
            "--runtime-dir", "/var/lib/ai-trader-supervisor", "--python", python]


def repair_command(*, python: str, repo: Path, incident: Path,
                   parent: str, output: Path) -> list[str]:
    """Canonical self-healing launch contract."""
    return [python, str(repo / "tools/self_heal_repair.py"), "--repo", str(repo),
            "--incident", str(incident), "--parent", parent, "--output", str(output),
            "--execute"]


def incident_fingerprint(audit: dict) -> str:
    bounded = {"head": audit.get("head"), "classification": audit.get("classification"),
               "action": audit.get("remediation", {}).get("action"),
               "codes": sorted(str(item.get("code")) for item in audit.get("findings", []))}
    return hashlib.sha256(json.dumps(bounded, sort_keys=True).encode()).hexdigest()


class RuntimeSupervisor:
    def __init__(self, *, repo: Path, state_dir: Path, journal_file: Path, runtime_dir: Path,
                 python: str, service: str = "ai-trader.service", confirmation_seconds: int = 30,
                 cooldown_seconds: int = 3600, run: Callable = subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, audit_func: Callable | None = None) -> None:
        self.repo, self.state_dir, self.journal_file = repo, state_dir, journal_file
        self.runtime_dir, self.python, self.service = runtime_dir, python, service
        self.confirmation_seconds, self.cooldown_seconds = confirmation_seconds, cooldown_seconds
        self.run, self.sleep, self.audit_func = run, sleep, audit_func
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lock(self):
        path = self.runtime_dir / "supervisor.lock"
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    def _audit(self, name: str) -> dict:
        output = self.runtime_dir / name
        if self.audit_func:
            value = self.audit_func()
            atomic_write_json(output, value)
            return value
        completed = self.run(runtime_audit_command(python=self.python, repo=self.repo,
            state_dir=self.state_dir, journal_file=self.journal_file, output=output), check=False)
        try:
            value = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("runtime auditor produced no structured result") from exc
        if int(value.get("exit_code", -1)) != completed.returncode:
            raise RuntimeError("runtime auditor exit and structured result disagree")
        return value

    def _service(self, verb: str) -> bool:
        return self.run(["systemctl", verb, self.service], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def _service_alive(self) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", self.service], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def _accepted(self, audit: dict) -> bool:
        if not audit.get("head") or audit.get("head") != audit.get("accepted_commit"):
            return False
        command = [self.python, "-c", "from orchestrator import verify_deployment_accepted; verify_deployment_accepted()"]
        return self.run(command, cwd=self.repo, check=False, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL).returncode == 0

    def _capture(self, audit: dict, label: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.runtime_dir / "incidents" / f"{stamp}-{label}"
        destination.mkdir(parents=True)
        atomic_write_json(destination / "incident.json", audit)
        if self.journal_file.is_file():
            shutil.copy2(self.journal_file, destination / "journal.log")
        for path in self.state_dir.glob("????-??-??.json"):
            shutil.copy2(path, destination / path.name)
        return destination

    def _repair_allowed(self, audit: dict) -> bool:
        fingerprint = incident_fingerprint(audit)
        path = self.runtime_dir / "repair-attempt.json"
        now = datetime.now(timezone.utc).timestamp()
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = {}
        unchanged_attempt = (previous.get("fingerprint") == fingerprint
                             and previous.get("head") == audit.get("head"))
        cooling_down = now - float(previous.get("attempted_at_epoch", 0)) < self.cooldown_seconds
        # An unchanged commit plus unchanged incident is not new evidence and
        # can never consume another AI attempt. Changed evidence must also
        # respect the global backoff before it can launch a repair.
        if unchanged_attempt or (previous and cooling_down):
            return False
        atomic_write_json(path, {"fingerprint": fingerprint, "head": audit.get("head"),
                                 "attempted_at_epoch": now})
        return True

    def _repair(self, audit: dict, incident: Path) -> int:
        if not self._repair_allowed(audit):
            return 42
        output = incident / "repair-result.json"
        return self.run(repair_command(python=self.python, repo=self.repo,
            incident=incident / "incident.json", parent=str(audit.get("head", "")),
            output=output), check=False).returncode

    def execute(self) -> int:
        with self._lock() as acquired:
            if not acquired:
                return 0
            audit = self._audit("latest-audit.json")
            code = int(audit.get("exit_code", -1))
            if code in PASSIVE_EXITS:
                return 0
            action = str(audit.get("remediation", {}).get("action", "NONE"))
            if code == 43 or action == "STOP_AND_REPAIR":
                self._service("stop")
                incident = self._capture(audit, "safety")
                return self._repair(audit, incident)
            if code != 42:
                return 91
            if action == "RESTART_SERVICE":
                incident = self._capture(audit, "restart")
                if not self._accepted(audit) or not self._service("restart"):
                    self._service("stop")
                    return self._repair(audit, incident)
                self.sleep(self.confirmation_seconds)
                confirmation = self._audit("restart-confirmation.json")
                if int(confirmation.get("exit_code", -1)) == 0:
                    atomic_write_json(incident / "recovery.json", {"status": "DETERMINISTIC_RECOVERY",
                                      "repair_invoked": False, "confirmation": confirmation})
                    return 0
                self._service("stop")
                atomic_write_json(incident / "failed-confirmation.json", confirmation)
                return self._repair(confirmation, incident)
            incident = self._capture(audit, "observe")
            if self._service_alive():
                self.sleep(self.confirmation_seconds)
                confirmation = self._audit("grace-confirmation.json")
                atomic_write_json(incident / "confirmation.json", confirmation)
                # NONE is evidence-only. A confirmed software repair must be explicit.
                return 0 if int(confirmation.get("exit_code", -1)) in PASSIVE_EXITS else 42
            return 42


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--journal-file", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--service", default="ai-trader.service")
    parser.add_argument("--confirmation-seconds", type=int, default=30)
    parser.add_argument("--self-test", action="store_true")
    return parser


def self_test(args: argparse.Namespace) -> int:
    """Validate launch contracts and local entrypoints without running an audit."""
    if not args.repo.is_dir() or not Path(args.python).is_file():
        return 2
    if not (args.repo / "tools/runtime_audit.py").is_file():
        return 2
    audit_args = runtime_audit_command(python=args.python, repo=args.repo,
        state_dir=args.state_dir, journal_file=args.journal_file,
        output=args.runtime_dir / "self-test-audit.json")[2:]
    runtime_audit_parser().parse_args(audit_args)
    repair_args = repair_command(python=args.python, repo=args.repo,
        incident=args.runtime_dir / "self-test-incident.json", parent="0" * 40,
        output=args.runtime_dir / "self-test-repair.json")[2:]
    repair_parser().parse_args(repair_args)
    return 0


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        return self_test(args)
    return RuntimeSupervisor(repo=args.repo, state_dir=args.state_dir, journal_file=args.journal_file,
        runtime_dir=args.runtime_dir, python=args.python, service=args.service,
        confirmation_seconds=args.confirmation_seconds).execute()


if __name__ == "__main__":
    raise SystemExit(main())
