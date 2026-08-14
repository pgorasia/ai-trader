from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jsonschema
import yaml

from .models import ConfigurationError, OperatingMode, PreflightError, SchemaValidationError
from .state import atomic_write_json


FORBIDDEN_ROBINHOOD_TOOLS = {
    "place_equity_order",
    "cancel_equity_order",
    "place_option_order",
    "cancel_option_order",
    "exercise_option",
    "cancel_option_exercise",
    "create_scan",
    "update_scan_filters",
    "update_scan_config",
    "delete_scan",
    "create_watchlist",
    "update_watchlist",
    "delete_watchlist",
    "add_watchlist_symbol",
    "remove_watchlist_symbol",
    "add_symbol_to_watchlist",
    "remove_symbol_from_watchlist",
    "review_equity_order",
}

REQUIRED_PROJECT_FILES = (
    "AGENTS.md",
    "config/strategy.yaml",
    "prompts/luna-stage-b.md",
    "prompts/sol-senior.md",
    "prompts/eod-review.md",
    "prompts/preflight.md",
    "prompts/shadow-monitor.md",
    "schemas/luna-cycle.schema.json",
    "schemas/senior-decision.schema.json",
    "schemas/eod-review.schema.json",
    "schemas/preflight.schema.json",
    "schemas/shadow-monitor.schema.json",
)


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot load strategy configuration: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigurationError("Strategy configuration must be a mapping")
    mode = OperatingMode(str(config.get("mode", "SHADOW")))
    if mode is not OperatingMode.SHADOW:
        raise NotImplementedError(f"Operating mode {mode.value} is intentionally not implemented")
    if config.get("timezone") != "America/New_York":
        raise ConfigurationError("Trading-session timezone must be America/New_York")
    if int(config.get("scanner", {}).get("maximum_results", 0)) > 20:
        raise ConfigurationError("Scanner maximum_results may not exceed 20")
    return config


def validate_schema_file(path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise SchemaValidationError(f"Invalid JSON schema {path}: {exc}") from exc
    return schema


def validate_json(data: Any, schema_path: Path) -> None:
    schema = validate_schema_file(schema_path)
    try:
        jsonschema.validate(data, schema, format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "<root>"
        raise SchemaValidationError(f"Response failed {schema_path.name} at {location}: {exc.message}") from exc


def offline_preflight(project_root: Path, config: dict[str, Any]) -> None:
    missing = [name for name in REQUIRED_PROJECT_FILES if not (project_root / name).is_file()]
    if missing:
        raise PreflightError(f"Required project files are missing: {', '.join(missing)}")
    for path in sorted((project_root / "schemas").glob("*.schema.json")):
        validate_schema_file(path)
    risk = config["risk"]
    if float(risk["maximum_hypothetical_notional"]) > 35 or float(risk["maximum_planned_loss"]) > 1:
        raise PreflightError("Configured V1 Shadow risk exceeds the hard safety ceiling")


def enforce_preflight_result(result: dict[str, Any]) -> None:
    exposed = {normalize_tool_name(name) for name in result["available_robinhood_tools"]}
    forbidden = sorted(exposed & FORBIDDEN_ROBINHOOD_TOOLS)
    reported = sorted({normalize_tool_name(name) for name in result["forbidden_tools_available"]})
    reasons: list[str] = []
    if not result["robinhood_mcp_available"]:
        reasons.append("Robinhood MCP is unavailable")
    if forbidden or reported:
        reasons.append(f"Forbidden Robinhood capabilities are exposed: {', '.join(sorted(set(forbidden + reported)))}")
    if result["agentic_account_count"] != 1:
        reasons.append("The dedicated Agentic account was not uniquely identified")
    if not result["account_reconciled"]:
        reasons.append("Agentic account reconciliation failed")
    if result["unexpected_equity_positions"]:
        reasons.append("Unexpected real equity position exists")
    if result["unexpected_equity_orders"]:
        reasons.append("Unexpected real equity order exists")
    if result["errors"]:
        reasons.append("Preflight returned errors")
    if reasons:
        raise PreflightError("; ".join(reasons))


def normalize_tool_name(name: Any) -> str:
    return re.split(r"__|[./:]", str(name))[-1]


def cooldown_until(decision_timestamp: str, minutes: int) -> str:
    value = datetime.fromisoformat(decision_timestamp)
    if value.tzinfo is None:
        raise SchemaValidationError("Senior decision timestamp must be timezone-aware")
    return (value + timedelta(minutes=minutes)).isoformat()


def write_alert(log_root: Path, category: str, message: str, timezone_name: str = "America/New_York") -> Path:
    now = datetime.now(ZoneInfo(timezone_name))
    path = log_root / "alerts" / f"{now.strftime('%Y%m%dT%H%M%S%z')}-{category}.json"
    atomic_write_json(path, {"timestamp": now.isoformat(), "category": category, "message": message, "mode": "SHADOW", "action": "STOPPED_FAIL_CLOSED"})
    return path
