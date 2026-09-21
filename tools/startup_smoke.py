#!/usr/bin/env python3
"""Offline application-startup gate matching the systemd daemon import/init path."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace


MCP_DISABLE_OVERRIDE = "mcp_servers.robinhood-trading.enabled=false"


class OfflineRunner:
    """Constructor dependency that proves startup without invoking Codex or MCP."""

    def verify_shadow_boundary(self):
        return SimpleNamespace(server_name="robinhood-trading")


class NoopMaintenance:
    def run_due(self):
        return {}


def startup_smoke(repo: Path) -> dict[str, object]:
    root = repo.resolve(strict=True)
    if not (root / "orchestrator.py").is_file():
        raise ValueError("repo does not contain orchestrator.py")
    sys.path.insert(0, str(root))

    # These imports intentionally mirror ExecStart and catch import-time defects.
    from orchestrator import ShadowOrchestrator, validate_unattended_config
    from trader.automation import DaemonSupervisor, Heartbeat

    validation = validate_unattended_config(root)
    if validation.get("status") != "PASS" or validation.get("mode") != "SHADOW":
        raise RuntimeError(f"unattended startup validation failed: {validation.get('problems')}")

    core = ShadowOrchestrator(root=root, runner=OfflineRunner())
    stop = threading.Event()
    stop.set()
    with tempfile.TemporaryDirectory(prefix="ai-trader-startup-smoke-") as directory:
        heartbeat_path = Path(directory) / "heartbeat.json"
        supervisor = DaemonSupervisor(
            core,
            stop_event=stop,
            heartbeat=Heartbeat(heartbeat_path, root),
            local_maintenance=NoopMaintenance(),
        )
        supervisor.run_forever()
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    if heartbeat.get("mode") != "SHADOW" or heartbeat.get("lifecycle_state") != "STOPPED":
        raise RuntimeError("daemon startup did not reach its readiness/stop lifecycle")
    return {
        "status": "PASS",
        "mode": "SHADOW",
        "startup_path": "ShadowOrchestrator->DaemonSupervisor.run_forever",
        "network_used": False,
        "brokerage_used": False,
        "mcp_override": MCP_DISABLE_OVERRIDE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = startup_smoke(args.repo)
    except Exception as exc:  # gate must convert every startup defect to nonzero
        print(json.dumps({"status": "FAIL", "error_class": type(exc).__name__,
                          "message": str(exc)[:500]}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
