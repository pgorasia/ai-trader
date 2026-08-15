from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import StateCorruptionError


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _exclusive_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() == payload:
            return path
        raise StateCorruptionError(f"Report operation collision at {path}; existing evidence was preserved")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except Exception:
        # Preserve a created artifact as evidence; never silently overwrite it.
        raise
    return path


def write_non_destructive_text(path: Path, content: str) -> Path:
    return _exclusive_bytes(path, (content.rstrip() + "\n").encode("utf-8"))


def write_json_companion(path: Path, data: dict[str, Any]) -> Path:
    try:
        payload = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError("Report contains non-JSON or non-finite data") from exc
    return _exclusive_bytes(path, payload)


def preflight_report_artifact(session_date: str, started_at: datetime, operation_id: str) -> Path:
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise StateCorruptionError("Preflight operation start must be timezone-aware")
    if started_at.date().isoformat() != session_date or not operation_id:
        raise StateCorruptionError("Preflight operation identity is inconsistent")
    timestamp = started_at.strftime("%H%M%S%f%z")
    short_id = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:12]
    return Path("reports") / f"{session_date}-preflight-{timestamp}-{short_id}.json"


def cycle_markdown(cycle: dict[str, Any]) -> str:
    lines = [f"# Shadow Stage-B Cycle {cycle['cycle_id']}", "", f"- Timestamp: {cycle['timestamp']}", "- Mode: SHADOW", f"- Scanner total: {cycle['scanner_total']}", f"- Symbols processed: {len(cycle['symbols_processed'])}", f"- Sol escalation: {cycle['sol_escalation']}", "", "## Finalists", ""]
    if not cycle["finalists"]: lines.append("None.")
    for finalist in cycle["finalists"]: lines.append(f"- {finalist['symbol']} — {finalist['classification']}: {finalist['technical_reason']}")
    if cycle["errors"]: lines.extend(["", "## Errors", ""] + [f"- {item}" for item in cycle["errors"]])
    return "\n".join(lines)


def senior_markdown(decision: dict[str, Any]) -> str:
    lines = ["# Senior Shadow Decision", "", f"- Decision timestamp: {decision['decision_timestamp']}", f"- Decision: {decision['decision']}", f"- Evaluated symbols: {', '.join(decision['evaluated_symbols'])}", "- Brokerage action: NONE", "", "## Rejections", ""]
    lines.extend(f"- {item['symbol']}: {item['reason']}" for item in decision["rejections"])
    if decision["decision"] == "SHADOW_TRADE_PLAN": lines.extend(["", "## Frozen plan", "", f"- Symbol: {decision['symbol']}", f"- Entry trigger: {decision['entry_trigger']}", f"- Stop: {decision['stop_price']}", f"- Target 1: {decision['target1']}", f"- Notional: {decision['hypothetical_notional']}", f"- Planned risk: {decision['planned_dollar_risk']}"])
    return "\n".join(lines)


def eod_markdown(session_date: str, review: dict[str, Any], readiness: dict[str, Any], trades: list[dict[str, Any]]) -> str:
    metrics = readiness["metrics"]
    lines = [f"# End-of-Day Shadow Evaluation — {session_date}", "", "Mode: SHADOW. Prior decisions and frozen plans remain immutable.", "", "## Senior rejection reviews", ""]
    if not review["decision_reviews"]: lines.append("None.")
    for item in review["decision_reviews"]: lines.append(f"- {item['symbol']} — {item['classification']}: {item['analysis']}")
    lines.extend(["", "## Frozen-plan outcomes", ""])
    if not trades: lines.append("No completed unambiguous Shadow trades.")
    for trade in trades: lines.append(f"- {trade['symbol']}: {trade['exit_reason']}, P&L {trade['pnl']:.4f}, R {trade['realized_r']:.3f}")
    lines.extend(["", "## Running deterministic metrics", "", f"- Readiness: {readiness['status']}", f"- Sessions: {metrics['market_sessions']}", f"- Completed Shadow trades: {metrics['completed_shadow_trades']}", f"- Win rate: {metrics['win_rate']:.2%}", f"- Average R: {metrics['average_r']:.3f}", f"- Profit factor: {metrics['profit_factor']}", f"- Expectancy after estimated costs: {metrics['expectancy_after_estimated_cost']:.4f}", f"- Maximum drawdown: {metrics['maximum_drawdown_dollars']:.4f}", "- Permission or mode changes: NONE"])
    return "\n".join(lines)
