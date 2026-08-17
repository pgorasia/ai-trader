"""Deterministic unattended-job Robinhood contracts (policy, not discovery)."""
from __future__ import annotations

from .shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS

JOB_TOOL_CONTRACTS: dict[str, frozenset[str]] = {
    "PREFLIGHT_IDENTITY": frozenset({"get_accounts"}),
    "PREFLIGHT_PORTFOLIO": frozenset({"get_accounts", "get_portfolio"}),
    "PREFLIGHT_POSITIONS": frozenset({"get_accounts", "get_equity_positions"}),
    "PREFLIGHT_ORDERS": frozenset({"get_accounts", "get_equity_orders"}),
    "STAGE_B": frozenset({
        "get_accounts", "get_portfolio", "get_equity_orders", "get_equity_positions",
        "run_scan", "get_equity_quotes", "get_equity_tradability",
        "get_equity_historicals", "get_equity_technical_indicators",
    }),
    "SOL_SENIOR": frozenset({"get_equity_quotes", "get_equity_historicals"}),
    "MONITOR": frozenset({"get_equity_historicals"}),
    "EOD": frozenset({"get_equity_historicals"}),
}

UNATTENDED_APPROVAL_TOOLS = frozenset().union(*JOB_TOOL_CONTRACTS.values())


def validate_job_contracts() -> list[str]:
    problems: list[str] = []
    for job, tools in JOB_TOOL_CONTRACTS.items():
        extra = tools - APPROVED_SHADOW_ROBINHOOD_TOOLS
        if extra:
            problems.append(f"{job} tools outside global SHADOW policy: {', '.join(sorted(extra))}")
        writes = sorted(name for name in tools if name.startswith(
            ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_")
        ))
        if writes:
            problems.append(f"{job} contains write capabilities: {', '.join(writes)}")
    return problems
