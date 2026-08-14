from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import atomic_write_json


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content.rstrip() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_non_destructive_text(path: Path, content: str) -> Path:
    normalized = content.rstrip() + "\n"
    if not path.exists():
        _atomic_text(path, normalized)
        return path
    if path.read_text(encoding="utf-8") == normalized:
        return path
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        if not candidate.exists():
            _atomic_text(candidate, normalized)
            return candidate
    raise RuntimeError(f"Could not allocate non-destructive report path for {path}")


def cycle_markdown(cycle: dict[str, Any]) -> str:
    lines = [
        f"# Shadow Stage-B Cycle {cycle['cycle_id']}",
        "",
        f"- Timestamp: {cycle['timestamp']}",
        "- Mode: SHADOW",
        f"- Scanner total: {cycle['scanner_total']}",
        f"- Symbols processed: {len(cycle['symbols_processed'])}",
        f"- Sol escalation: {cycle['sol_escalation']}",
        "",
        "## Finalists",
        "",
    ]
    if not cycle["finalists"]:
        lines.append("None.")
    for finalist in cycle["finalists"]:
        lines.extend([f"- {finalist['symbol']} — {finalist['classification']}: {finalist['technical_reason']}"])
    if cycle["errors"]:
        lines.extend(["", "## Errors", ""] + [f"- {item}" for item in cycle["errors"]])
    return "\n".join(lines)


def senior_markdown(decision: dict[str, Any]) -> str:
    lines = [
        "# Senior Shadow Decision",
        "",
        f"- Decision timestamp: {decision['decision_timestamp']}",
        f"- Decision: {decision['decision']}",
        f"- Evaluated symbols: {', '.join(decision['evaluated_symbols'])}",
        "- Brokerage action: NONE",
        "",
        "## Rejections",
        "",
    ]
    lines.extend(f"- {item['symbol']}: {item['reason']}" for item in decision["rejections"])
    if decision["decision"] == "SHADOW_TRADE_PLAN":
        lines.extend(["", "## Frozen plan", "", f"- Symbol: {decision['symbol']}", f"- Entry trigger: {decision['entry_trigger']}", f"- Stop: {decision['stop_price']}", f"- Target 1: {decision['target1']}", f"- Notional: {decision['hypothetical_notional']}", f"- Planned risk: {decision['planned_dollar_risk']}"])
    return "\n".join(lines)


def eod_markdown(session_date: str, review: dict[str, Any], readiness: dict[str, Any], trades: list[dict[str, Any]]) -> str:
    metrics = readiness["metrics"]
    lines = [
        f"# End-of-Day Shadow Evaluation — {session_date}",
        "",
        "Mode: SHADOW. Prior decisions and frozen plans remain immutable.",
        "",
        "## Senior rejection reviews",
        "",
    ]
    if not review["decision_reviews"]:
        lines.append("None.")
    for item in review["decision_reviews"]:
        lines.append(f"- {item['symbol']} — {item['classification']}: {item['analysis']}")
    lines.extend(["", "## Frozen-plan outcomes", ""])
    if not trades:
        lines.append("No completed unambiguous Shadow trades.")
    for trade in trades:
        lines.append(f"- {trade['symbol']}: {trade['exit_reason']}, P&L {trade['pnl']:.4f}, R {trade['realized_r']:.3f}")
    lines.extend([
        "", "## Running deterministic metrics", "",
        f"- Readiness: {readiness['status']}",
        f"- Sessions: {metrics['market_sessions']}",
        f"- Completed Shadow trades: {metrics['completed_shadow_trades']}",
        f"- Win rate: {metrics['win_rate']:.2%}",
        f"- Average R: {metrics['average_r']:.3f}",
        f"- Profit factor: {metrics['profit_factor']}",
        f"- Expectancy after estimated costs: {metrics['expectancy_after_estimated_cost']:.4f}",
        f"- Maximum drawdown: {metrics['maximum_drawdown_dollars']:.4f}",
        "- Permission or mode changes: NONE",
    ])
    return "\n".join(lines)


def write_json_companion(path: Path, data: dict[str, Any]) -> Path:
    if path.exists():
        import json
        current = json.loads(path.read_text(encoding="utf-8"))
        if current == data:
            return path
        for number in range(2, 1000):
            candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
            if not candidate.exists():
                atomic_write_json(candidate, data)
                return candidate
    atomic_write_json(path, data)
    return path
