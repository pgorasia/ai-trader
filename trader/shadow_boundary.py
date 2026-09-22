from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import ConfigurationError, PreflightError


SHADOW_TOOL_POLICY_VERSION = "shadow-robinhood-readonly-v1"
HOST_POLICY_VERSION = "codex-host-tool-boundary-v1"
DEFAULT_HOST_REQUIREMENTS_PATH = Path("/etc/codex/requirements.toml")
ROBINHOOD_MCP_SERVER = "robinhood-trading"
ROBINHOOD_MCP_IDENTITY_URL = "https://agent.robinhood.com/mcp/trading"
REQUIRED_DISABLED_HOST_FEATURES = frozenset({
    "apps",
    "plugins",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
})

# This is the complete, reviewed Robinhood surface permitted by policy version v1.
# A configured set may be a subset, but no name outside this set is authorized.
APPROVED_SHADOW_ROBINHOOD_TOOLS = frozenset({
    "get_accounts",
    "get_earnings_calendar",
    "get_earnings_results",
    "get_equity_fundamentals",
    "get_equity_historicals",
    "get_equity_orders",
    "get_equity_positions",
    "get_equity_price_book",
    "get_equity_quotes",
    "get_equity_tax_lots",
    "get_equity_technical_indicators",
    "get_equity_tradability",
    "get_financials",
    "get_index_quotes",
    "get_indexes",
    "get_pnl_trade_history",
    "get_portfolio",
    "get_realized_pnl",
    "get_scanner_filter_specs",
    "get_scans",
    "run_scan",
    "search",
})

# Preflight cannot reconcile the dedicated account without these reads. Other
# jobs may require additional allowed reads and fail independently if absent.
REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_orders", "get_equity_positions"
})

_NORMAL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ShadowBoundaryResult:
    config_path: Path
    server_name: str
    enabled_tools: frozenset[str]
    policy_version: str = SHADOW_TOOL_POLICY_VERSION


@dataclass(frozen=True)
class HostPolicyResult:
    requirements_path: Path
    server_name: str = ROBINHOOD_MCP_SERVER
    identity_url: str = ROBINHOOD_MCP_IDENTITY_URL
    policy_version: str = HOST_POLICY_VERSION


def verify_host_policy(requirements_path: Path = DEFAULT_HOST_REQUIREMENTS_PATH) -> HostPolicyResult:
    """Verify the administrator-enforced Codex boundary before model execution."""
    path = Path(requirements_path).expanduser().resolve(strict=False)
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreflightError(f"Codex host requirements are missing at {path}") from exc
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PreflightError(f"Cannot parse Codex host requirements at {path}: {type(exc).__name__}") from exc

    features = config.get("features")
    if not isinstance(features, dict):
        raise PreflightError("Codex host requirements have no features mapping")
    enabled = sorted(name for name in REQUIRED_DISABLED_HOST_FEATURES if features.get(name) is not False)
    if enabled:
        raise PreflightError("Codex host requirements do not disable required integrations: " + ", ".join(enabled))

    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or set(servers) != {ROBINHOOD_MCP_SERVER}:
        observed = sorted(str(name) for name in servers) if isinstance(servers, dict) else []
        raise PreflightError(
            "Codex host MCP allowlist must contain only robinhood-trading; found: "
            + (", ".join(observed) if observed else "none")
        )
    server = servers[ROBINHOOD_MCP_SERVER]
    identity = server.get("identity") if isinstance(server, dict) else None
    if not isinstance(identity, dict) or identity.get("url") != ROBINHOOD_MCP_IDENTITY_URL:
        raise PreflightError("Codex host Robinhood MCP identity URL does not match the production endpoint")
    return HostPolicyResult(path)


def normalize_configured_tool_name(value: Any) -> str:
    if not isinstance(value, str):
        raise PreflightError("Robinhood enabled_tools entries must be strings")
    name = value.strip().lower()
    for prefix in ("mcp__robinhood__", "robinhood__"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not _NORMAL_NAME.fullmatch(name):
        raise PreflightError("Robinhood enabled_tools contains a malformed tool name")
    return name


def locate_codex_config(codex_settings: Mapping[str, Any], environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    explicit = codex_settings.get("config_path")
    if explicit:
        path = Path(str(explicit)).expanduser()
        if not path.is_absolute():
            raise ConfigurationError("codex.config_path must be absolute when configured")
        return path.resolve(strict=False)
    codex_home = env.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return (base / "config.toml").resolve(strict=False)


def verify_shadow_mcp_boundary(config_path: Path, *, require_unattended_approvals: bool = True) -> ShadowBoundaryResult:
    try:
        raw = config_path.read_text(encoding="utf-8")
        config = tomllib.loads(raw)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PreflightError(f"Cannot parse active Codex config at {config_path}: {type(exc).__name__}") from exc
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        raise PreflightError("Active Codex config has no mcp_servers mapping")
    matches = [(name, value) for name, value in servers.items() if "robinhood" in str(name).lower()]
    if len(matches) != 1:
        raise PreflightError(f"Expected exactly one Robinhood MCP server; found {len(matches)}")
    server_name, server = matches[0]
    if not isinstance(server, dict):
        raise PreflightError("Robinhood MCP server configuration must be a mapping")
    if server.get("enabled") is False or server.get("disabled") is True:
        raise PreflightError("Robinhood MCP server is disabled")
    configured = server.get("enabled_tools")
    if not isinstance(configured, list) or not configured:
        raise PreflightError("Robinhood MCP enabled_tools must be an explicit non-empty list")
    normalized = [normalize_configured_tool_name(item) for item in configured]
    if len(normalized) != len(set(normalized)):
        raise PreflightError("Robinhood MCP enabled_tools contains duplicates after normalization")
    enabled = frozenset(normalized)
    extra = sorted(enabled - APPROVED_SHADOW_ROBINHOOD_TOOLS)
    if extra:
        raise PreflightError(f"Robinhood MCP exposes tools outside {SHADOW_TOOL_POLICY_VERSION}: {', '.join(extra)}")
    missing = sorted(REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS - enabled)
    if missing:
        raise PreflightError(f"Robinhood preflight unavailable; required read tools missing: {', '.join(missing)}")
    from .job_contracts import UNATTENDED_APPROVAL_TOOLS
    required_approvals = UNATTENDED_APPROVAL_TOOLS if require_unattended_approvals else REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS
    unavailable = sorted(required_approvals - enabled)
    if unavailable:
        raise PreflightError("Robinhood unattended scheduled tools are not globally enabled: " + ", ".join(unavailable))
    tool_settings = server.get("tools")
    if not isinstance(tool_settings, dict):
        raise PreflightError("Robinhood unattended preflight approvals are not configured")
    unapproved = sorted(
        name for name in required_approvals
        if not isinstance(tool_settings.get(name), dict)
        or tool_settings[name].get("approval_mode") != "approve"
    )
    if unapproved:
        raise PreflightError(
            "Robinhood unattended explicit tool approvals are missing: " + ", ".join(unapproved)
        )
    return ShadowBoundaryResult(config_path.resolve(), str(server_name), enabled)
