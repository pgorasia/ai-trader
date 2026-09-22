#!/usr/bin/env python3
"""Fail-closed controller for confirmed, sanitized software-defect repair.

The controller prepares an isolated exact-parent clone and a bounded repair
contract. It never handles external degradations and never accesses brokerage
MCP. Execution is deliberately explicit via ``--execute``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from trader.state import atomic_write_json

ALLOWED = frozenset({"INTERNAL_DEFECT", "SAFETY_DEFECT"})
MAX_NEW_COMMITS = 8
SECRET = re.compile(r"(?i)(account|token|oauth|cookie|authorization|tool[_ -]?result)")
COMMIT = re.compile(r"[0-9a-f]{40}")


def load_incident(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("classification") not in ALLOWED or int(value.get("exit_code", -1)) not in {42, 43}:
        raise ValueError("repair refused: incident is not a confirmed software or safety defect")
    safe_findings = []
    for item in value.get("findings", []):
        detail = str(item.get("detail", ""))
        safe_findings.append({"code": str(item.get("code", "UNKNOWN"))[:80],
                              "severity": str(item.get("severity", "INTERNAL"))[:20],
                              "detail": "<redacted>" if SECRET.search(detail) else detail[:240]})
    head = value.get("head")
    return {"classification": value["classification"], "exit_code": value["exit_code"],
            "incident_commit": head if isinstance(head, str) and COMMIT.fullmatch(head) else None,
            "findings": safe_findings}


def prepare_clone(repo: Path, parent: str, destination: Path) -> None:
    if not COMMIT.fullmatch(parent):
        raise ValueError("parent must be an exact 40-character commit")
    exists = subprocess.run(["git", "cat-file", "-e", f"{parent}^{{commit}}"], cwd=repo,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if exists.returncode:
        raise ValueError("parent commit is not present in source repository")
    subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(repo), str(destination)], check=True)
    subprocess.run(["git", "checkout", "--detach", parent], cwd=destination, check=True)
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=destination, text=True,
                            stdout=subprocess.PIPE, check=True).stdout.strip()
    if actual != parent: raise RuntimeError("isolated clone parent mismatch")


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--repo", required=True, type=Path)
    p.add_argument("--incident", required=True, type=Path); p.add_argument("--parent", required=True)
    p.add_argument("--output", required=True, type=Path); p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    try: evidence = load_incident(args.incident)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        atomic_write_json(args.output, {"status": "REFUSED", "reason": str(exc)[:240]}); return 20
    if not COMMIT.fullmatch(args.parent):
        atomic_write_json(args.output, {"status": "REFUSED", "reason": "parent must be an exact commit"})
        return 20
    try:
        parent_exists = subprocess.run(["git", "cat-file", "-e", f"{args.parent}^{{commit}}"], cwd=args.repo,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        atomic_write_json(args.output, {"status": "REFUSED", "reason": "source repository unavailable"})
        return 20
    if parent_exists.returncode:
        atomic_write_json(args.output, {"status": "REFUSED", "reason": "parent commit is unavailable"})
        return 20
    if evidence.get("incident_commit") and evidence["incident_commit"] != args.parent:
        atomic_write_json(args.output, {"status": "REFUSED", "reason": "incident and parent commit differ"})
        return 20
    contract = {"status": "PREPARED", "parent_commit": args.parent, "model": "gpt-5.6-sol",
        "max_new_repair_commits": MAX_NEW_COMMITS, "network_brokerage_access": False,
        "requirements": ["reproduce incident in regression test", "run full tests after every repair",
                         "never accept unchanged failed commit", "bind acceptance to exact commit",
                         "do not tune strategy or risk thresholds"], "sanitized_evidence": evidence}
    if not args.execute:
        atomic_write_json(args.output, contract); return 0
    work = Path(tempfile.mkdtemp(prefix="ai-trader-repair-"))
    try:
        clone = work / "repo"; prepare_clone(args.repo, args.parent, clone)
        prompt = ("Repair only the confirmed defect in the sanitized incident below. First reproduce it in a regression test. "
                  "Preserve SHADOW-only behavior, all strategy/risk thresholds, and the read-only tool boundary. "
                  "Do not access Robinhood. Run the full unit suite after repair.\n" + json.dumps(evidence, sort_keys=True))
        clean_home = work / "codex-home"; clean_home.mkdir()
        (clean_home / "config.toml").write_text(
            '[features]\napps = false\nplugins = false\nbrowser_use = false\n'
            'browser_use_external = false\nbrowser_use_full_cdp_access = false\ncomputer_use = false\n'
            '[mcp_servers.robinhood-trading]\nenabled = false\n', encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if key not in {"OPENAI_API_KEY", "CODEX_HOME"}}
        environment["CODEX_HOME"] = str(clean_home)
        before = args.parent
        invoked = subprocess.run(["codex", "exec", "--model", "gpt-5.6-sol", "--sandbox", "workspace-write",
                                  "--config", "features.apps=false", "--config", "features.plugins=false",
                                  "--config", "mcp_servers.robinhood-trading.enabled=false", prompt],
                                 cwd=clone, env=environment, check=False, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=1800)
        after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, text=True, stdout=subprocess.PIPE,
                               check=True).stdout.strip()
        commits = subprocess.run(["git", "rev-list", "--reverse", f"{before}..{after}"], cwd=clone,
                                 text=True, stdout=subprocess.PIPE, check=True).stdout.splitlines()
        count = len(commits)
        if invoked.returncode or after == before or count > MAX_NEW_COMMITS:
            raise RuntimeError("repair produced no eligible bounded candidate")
        changed = subprocess.run(["git", "diff", "--name-only", f"{before}..{after}"], cwd=clone,
                                 text=True, stdout=subprocess.PIPE, check=True).stdout.splitlines()
        if not any(path.startswith("tests/test_") and path.endswith(".py") for path in changed):
            raise RuntimeError("repair lacks a regression test")
        # Every repair commit must independently preserve a green full suite;
        # a later commit may not conceal an earlier broken repair checkpoint.
        for commit in commits:
            subprocess.run(["git", "checkout", "--detach", commit], cwd=clone, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                                   cwd=clone, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if tests.returncode:
                raise RuntimeError("repair checkpoint tests failed")
            for command in ([sys.executable, "orchestrator.py", "--self-test"],
                            [sys.executable, "orchestrator.py", "--reliability-acceptance-offline"],
                            [sys.executable, "orchestrator.py", "--validate-unattended-config"]):
                gate = subprocess.run(command, cwd=clone, check=False,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if gate.returncode:
                    raise RuntimeError("repair checkpoint acceptance failed")
        contract.update(status="CANDIDATE_REQUIRES_INDEPENDENT_ACCEPTANCE", isolated_clone=str(clone),
                        candidate_commit=after, new_commit_count=count, tests="PASS")
        atomic_write_json(args.output, contract); return 20
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        contract.update(status="FAILED_CLOSED", reason=type(exc).__name__); atomic_write_json(args.output, contract); return 42


if __name__ == "__main__": raise SystemExit(main())
