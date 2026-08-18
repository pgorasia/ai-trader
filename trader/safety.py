from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jsonschema
import yaml

from .models import ConfigurationError, OperatingMode, PreflightError, SchemaValidationError
from .shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, REQUIRED_PREFLIGHT_ROBINHOOD_TOOLS
from .state import atomic_write_json
from .state import STRATEGY_VERSION


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
    "prompts/preflight-portfolio.md",
    "prompts/preflight-positions.md",
    "prompts/preflight-orders.md",
    "prompts/shadow-monitor.md",
    "methodology/eod-v1.md",
    "scripts/start-shadow.ps1",
    "scripts/install-scheduler.ps1",
    "scripts/check-scheduler.ps1",
    "schemas/luna-cycle.schema.json",
    "schemas/senior-decision.schema.json",
    "schemas/eod-review.schema.json",
    "schemas/preflight.schema.json",
    "schemas/preflight-portfolio.schema.json",
    "schemas/preflight-positions.schema.json",
    "schemas/preflight-orders.schema.json",
    "schemas/shadow-monitor.schema.json",
)

# Codex Structured Outputs supports a deliberately small JSON Schema subset.
# Keep semantic constraints out of schemas passed to ``--output-schema`` and
# enforce them below. This explicit deny-list is also a regression guard.
UNSUPPORTED_CODEX_SCHEMA_KEYWORDS = frozenset({
    "allOf", "contains", "dependentRequired", "dependentSchemas", "else",
    "exclusiveMaximum", "exclusiveMinimum", "format", "if", "maxContains",
    "maxItems", "maxLength", "maxProperties", "maximum", "minContains",
    "minItems", "minLength", "minProperties", "minimum", "multipleOf", "not",
    "oneOf", "pattern", "patternProperties", "propertyNames", "then",
    "unevaluatedItems", "unevaluatedProperties", "uniqueItems",
})


def lint_codex_output_schema(schema: Any, *, location: str = "<root>") -> None:
    """Reject schema features outside the Codex strict-output subset."""
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"Codex output schema at {location} must be an object")
    violations: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in UNSUPPORTED_CODEX_SCHEMA_KEYWORDS:
                    violations.append(child_path)
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(schema, location)
    if violations:
        raise SchemaValidationError("Unsupported Codex output-schema keyword(s): " + ", ".join(violations))
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise SchemaValidationError("Codex output schema root must have type object and explicit properties")
    if schema.get("additionalProperties") is not False:
        raise SchemaValidationError("Codex output schema root must set additionalProperties false")
    _lint_strict_objects(schema, location)


def _lint_strict_objects(value: Any, path: str) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object" and isinstance(value.get("properties"), dict):
            properties = set(value["properties"])
            required = value.get("required")
            if value.get("additionalProperties") is not False:
                raise SchemaValidationError(f"Strict object at {path} must set additionalProperties false")
            if not isinstance(required, list) or set(required) != properties or len(required) != len(properties):
                raise SchemaValidationError(f"Strict object at {path} must require every property exactly once")
        for key, child in value.items():
            _lint_strict_objects(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _lint_strict_objects(child, f"{path}[{index}]")


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
    if config.get("strategy_version") != STRATEGY_VERSION:
        raise ConfigurationError("Configured strategy_version does not match the state implementation")
    if int(config.get("scanner", {}).get("maximum_results", 0)) > 20:
        raise ConfigurationError("Scanner maximum_results may not exceed 20")
    models = config.get("models", {})
    expected_models = {"luna": "gpt-5.6-luna", "sol": "gpt-5.6-sol", "sol_reasoning_effort": "high"}
    if not isinstance(models, dict) or any(models.get(key) != value for key, value in expected_models.items()):
        raise ConfigurationError("Model policy must be Luna gpt-5.6-luna and Sol gpt-5.6-sol with high reasoning")
    readiness = config.get("readiness", {})
    required_readiness = {"minimum_market_sessions", "minimum_completed_shadow_trades", "estimated_round_trip_cost_bps", "stress_round_trip_cost_bps", "minimum_profit_factor", "maximum_single_winner_gross_profit_fraction", "maximum_drawdown_dollars", "maximum_security_violations", "maximum_unresolved_state_failures"}
    if not isinstance(readiness, dict) or not required_readiness <= readiness.keys():
        raise ConfigurationError("Readiness policy is incomplete")
    if float(readiness["stress_round_trip_cost_bps"]) < float(readiness["estimated_round_trip_cost_bps"]):
        raise ConfigurationError("Stress transaction cost must not be below the base cost")
    experiment = config.get("experiment", {})
    required_experiment = {"maximum_research_candidates_per_session", "development_sessions", "validation_sessions", "minimum_development_pairs", "minimum_validation_primary_trades", "trailing_activation_r", "trailing_lookback_completed_bars"}
    if not isinstance(experiment, dict) or not required_experiment <= experiment.keys():
        raise ConfigurationError("Accelerated Shadow experiment policy is incomplete")
    if int(experiment["maximum_research_candidates_per_session"]) != 4 or int(experiment["development_sessions"]) < 1 or int(experiment["validation_sessions"]) < 1:
        raise ConfigurationError("Accelerated Shadow experiment must use four candidates and positive phase lengths")
    if float(experiment["trailing_activation_r"]) <= 0 or int(experiment["trailing_lookback_completed_bars"]) < 2:
        raise ConfigurationError("Trailing-exit policy is outside safe deterministic bounds")
    return config


def validate_schema_file(path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        lint_codex_output_schema(schema, location=path.name)
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
    _validate_removed_schema_invariants(data, schema_path.name)


def normalize_codex_output(data: dict[str, Any], schema_name: str) -> dict[str, Any]:
    """Convert strict model-facing arrays to the established internal maps."""
    if schema_name in {"shadow-monitor.schema.json", "eod-review.schema.json"}:
        data = dict(data)
        data["symbol_bars"] = {item["symbol"]: item["bars"] for item in data["symbol_bars"]}
    return data


_SYMBOL = re.compile(r"^[A-Z.\-]{1,10}$")


def _aware_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{label} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaValidationError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise SchemaValidationError(f"{label} must be timezone-aware")
    return parsed


def _date(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{label} must be an ISO date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SchemaValidationError(f"{label} must be an ISO date") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise SchemaValidationError(f"{label} must be an ISO date")


def _number(value: Any, label: str, *, minimum: float | None = None, exclusive_minimum: float | None = None, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SchemaValidationError(f"{label} must be a finite number")
    if minimum is not None and value < minimum or exclusive_minimum is not None and value <= exclusive_minimum or maximum is not None and value > maximum:
        raise SchemaValidationError(f"{label} is outside its permitted bounds")


def _nonempty(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise SchemaValidationError(f"{label} must not be empty")


def _symbol(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise SchemaValidationError(f"{label} is not a valid normalized symbol")


def _unique(values: list[Any], label: str, *, normalize=lambda item: item) -> None:
    normalized = [normalize(item) for item in values]
    if len(normalized) != len(set(normalized)):
        raise SchemaValidationError(f"{label} contains duplicate values after normalization")


def _bar(bar: dict[str, Any], label: str) -> None:
    _aware_timestamp(bar["timestamp"], f"{label}.timestamp")
    for key in ("open", "high", "low", "close"):
        _number(bar[key], f"{label}.{key}", exclusive_minimum=0)
    _number(bar["volume"], f"{label}.volume", minimum=0)
    if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > bar["high"]:
        raise SchemaValidationError(f"{label} has impossible OHLC ordering")


def _validate_removed_schema_invariants(data: dict[str, Any], schema_name: str) -> None:
    """Preserve constraints omitted solely for Structured Outputs compatibility."""
    if schema_name in {"preflight.schema.json", "preflight-portfolio.schema.json", "preflight-positions.schema.json", "preflight-orders.schema.json"}:
        for index, account in enumerate(data["account_classifications"]):
            if set(account) != set(_PREFLIGHT_ACCOUNT_CLASSIFICATION_FIELDS):
                raise SchemaValidationError(f"account_classifications[{index}] has unsafe or missing fields")
        if schema_name == "preflight-portfolio.schema.json":
            for key in ("account_equity", "buying_power"):
                if data[key] is not None: _number(data[key], key, minimum=0)
        elif schema_name == "preflight-positions.schema.json":
            _number(data["baseline_position_count"], "baseline_position_count", minimum=0)
            for index, item in enumerate(data["baseline_positions"]): _symbol(item["symbol"], f"baseline_positions[{index}].symbol"); _number(item["quantity"], "quantity", exclusive_minimum=0)
            if data["baseline_position_count"] != len(data["baseline_positions"]): raise SchemaValidationError("Baseline position count does not match details")
            if data["baseline_positions_present"] != bool(data["baseline_positions"]): raise SchemaValidationError("Baseline position presence does not match details")
        elif schema_name == "preflight-orders.schema.json":
            for key in ("relevant_order_count", "open_pending_count", "baseline_external_order_count"): _number(data[key], key, minimum=0)
            for index, item in enumerate(data["baseline_external_orders"]): _symbol(item["symbol"], f"baseline_external_orders[{index}].symbol")
            if data["baseline_external_order_count"] != len(data["baseline_external_orders"]): raise SchemaValidationError("Baseline external order count does not match details")
            if data["baseline_external_orders_present"] != bool(data["baseline_external_orders"]): raise SchemaValidationError("Baseline external order presence does not match details")
    elif schema_name == "luna-cycle.schema.json":
        _nonempty(data["cycle_id"], "cycle_id"); _date(data["session_date"], "session_date"); _aware_timestamp(data["timestamp"], "timestamp")
        _nonempty(data["scanner"]["name"], "scanner.name"); _nonempty(data["scanner"]["id"], "scanner.id")
        _number(data["scanner_total"], "scanner_total", minimum=0)
        if len(data["symbols_processed"]) > 20 or len(data["finalists"]) > 4: raise SchemaValidationError("Luna array exceeds its permitted maximum length")
        _unique(data["symbols_processed"], "symbols_processed")
        for index, symbol in enumerate(data["symbols_processed"]): _symbol(symbol, f"symbols_processed[{index}]")
        for index, finalist in enumerate(data["finalists"]):
            _symbol(finalist["symbol"], f"finalists[{index}].symbol"); _nonempty(finalist["technical_reason"], f"finalists[{index}].technical_reason")
            _number(finalist["scanner_percent_change"], "scanner_percent_change", minimum=-1)
            _number(finalist["gap_return"], "gap_return", minimum=-1)
            _number(finalist["intraday_return"], "intraday_return", minimum=-1)
            _number(finalist["distance_from_vwap"], "distance_from_vwap", minimum=-1)
            for key in ("rvol", "volume_persistence"): _number(finalist[key], key, minimum=0)
            _number(finalist["distance_from_high"], "distance_from_high", minimum=0, maximum=1)
            _number(finalist["vwap"], "vwap", exclusive_minimum=0); _number(finalist["ema20"], "ema20", exclusive_minimum=0); _number(finalist["rsi14"], "rsi14", minimum=0, maximum=100)
            material = finalist["material_requalification"]
            if material is not None: _nonempty(material["evidence"], "evidence"); _aware_timestamp(material["evidence_timestamp"], "evidence_timestamp")
            bars = finalist.get("completed_15m_structure", [])
            if len(bars) > 8: raise SchemaValidationError("completed_15m_structure exceeds eight bars")
            for bar_index, bar in enumerate(bars): _bar(bar, f"completed_15m_structure[{bar_index}]")
        _unique(data["security_status"]["forbidden_tools_available"], "forbidden_tools_available", normalize=normalize_tool_name)
        for key in ("agentic_account_count", "baseline_position_count", "baseline_external_order_count"):
            _number(data["account_status"][key], key, minimum=0)
        external_orders = data["account_status"]["baseline_external_orders"]
        for index, item in enumerate(external_orders): _symbol(item["symbol"], f"account_status.baseline_external_orders[{index}].symbol")
        if data["account_status"]["baseline_external_order_count"] != len(external_orders): raise SchemaValidationError("Stage-B baseline external order count does not match details")
        if data["account_status"]["baseline_external_orders_present"] != bool(external_orders): raise SchemaValidationError("Stage-B baseline external order presence does not match details")
        for key in ("account_equity", "buying_power"):
            if data["account_status"][key] is not None: _number(data["account_status"][key], key, minimum=0)
        for key, value in data["tool_call_count"].items(): _number(value, f"tool_call_count.{key}", minimum=0)
    elif schema_name == "senior-decision.schema.json":
        _aware_timestamp(data["decision_timestamp"], "decision_timestamp")
        if not 1 <= len(data["evaluated_symbols"]) <= 4 or len(data["rejections"]) > 4: raise SchemaValidationError("Senior array length is outside its permitted bounds")
        _unique(data["evaluated_symbols"], "evaluated_symbols")
        for index, symbol in enumerate(data["evaluated_symbols"]): _symbol(symbol, f"evaluated_symbols[{index}]")
        for index, rejection in enumerate(data["rejections"]):
            _symbol(rejection["symbol"], f"rejections[{index}].symbol"); _nonempty(rejection["reason"], "rejection.reason")
            if not rejection["rejection_categories"]: raise SchemaValidationError("rejection_categories must not be empty")
        for key in ("robinhood_tool_call_count", "web_search_count"): _number(data[key], key, minimum=0)
        if data["decision"] == "SHADOW_TRADE_PLAN":
            for key in ("symbol", "setup_type", "entry_condition", "stop_basis", "invalidation_condition"): _nonempty(data[key], key)
            _symbol(data["symbol"], "symbol")
            for key in ("decision_timestamp", "time_exit", "latest_entry_time", "mandatory_flat_time"): _aware_timestamp(data[key], key)
            if data["quote_timestamp"] is not None: _aware_timestamp(data["quote_timestamp"], "quote_timestamp")
            for key in ("current_price", "entry_trigger", "maximum_chase_price", "stop_price", "target1", "hypothetical_notional", "hypothetical_quantity", "planned_dollar_risk", "planned_account_risk_percent"): _number(data[key], key, exclusive_minimum=0)
            if data["target2_optional"] is not None: _number(data["target2_optional"], "target2_optional", exclusive_minimum=0)
            _number(data["hypothetical_notional"], "hypothetical_notional", maximum=35); _number(data["planned_dollar_risk"], "planned_dollar_risk", maximum=1); _number(data["planned_account_risk_percent"], "planned_account_risk_percent", maximum=1)
            _number(data["reward_risk_target1"], "reward_risk_target1", minimum=1.9); _number(data["confidence"], "confidence", minimum=0, maximum=1)
        elif any(data[key] is not None for key in set(data) - {"decision", "decision_timestamp", "evaluated_symbols", "rejections", "robinhood_tool_call_count", "web_search_count", "errors"}):
            raise SchemaValidationError("NO_TRADE plan fields must all be null")
    elif schema_name in {"shadow-monitor.schema.json", "eod-review.schema.json"}:
        if schema_name == "eod-review.schema.json":
            _date(data["session_date"], "session_date")
            for index, review in enumerate(data["decision_reviews"]): _symbol(review["symbol"], f"decision_reviews[{index}].symbol"); _aware_timestamp(review["decision_timestamp"], "decision_timestamp"); _nonempty(review["analysis"], "analysis")
            for key, value in data["benchmark_closes"].items():
                if value is not None: _number(value, f"benchmark_closes.{key}", exclusive_minimum=0)
            count_key = "robinhood_tool_call_count"
        else: count_key = "tool_call_count"
        _aware_timestamp(data["timestamp"], "timestamp"); _number(data[count_key], count_key, minimum=0)
        symbols = [item["symbol"] for item in data["symbol_bars"]]
        _unique(symbols, "symbol_bars symbols")
        for item in data["symbol_bars"]:
            _symbol(item["symbol"], "symbol_bars.symbol")
            for index, bar in enumerate(item["bars"]): _bar(bar, f"symbol_bars.{item['symbol']}[{index}]")


def offline_preflight(project_root: Path, config: dict[str, Any]) -> None:
    missing = [name for name in REQUIRED_PROJECT_FILES if not (project_root / name).is_file()]
    if missing:
        raise PreflightError(f"Required project files are missing: {', '.join(missing)}")
    for path in sorted((project_root / "schemas").glob("*.schema.json")):
        validate_schema_file(path)
    risk = config["risk"]
    if float(risk["maximum_hypothetical_notional"]) > 35 or float(risk["maximum_planned_loss"]) > 1:
        raise PreflightError("Configured V1 Shadow risk exceeds the hard safety ceiling")


_PREFLIGHT_ACCOUNT_CLASSIFICATION_FIELDS = (
    "agentic_allowed",
    "brokerage_account_type",
    "management_type",
    "state",
    "deactivated",
    "permanently_deactivated",
)


def derive_preflight_identity(result: dict[str, Any]) -> dict[str, Any]:
    """Derive Agentic identity from safe get_accounts classifications only."""
    accounts = result.get("account_classifications")
    if not isinstance(accounts, list):
        raise PreflightError("Safe account classification evidence is missing")
    candidates = [account for account in accounts if account.get("agentic_allowed") is True]
    result["agentic_account_count"] = len(candidates)
    result["unique_agentic_account"] = len(candidates) == 1
    if not candidates:
        raise PreflightError("No account with agentic_allowed=true")
    if len(candidates) > 1:
        raise PreflightError("Multiple accounts with agentic_allowed=true")
    selected = candidates[0]
    expected = {
        "brokerage_account_type": "individual",
        "management_type": "self_directed",
        "state": "active",
        "deactivated": False,
        "permanently_deactivated": False,
    }
    failed = [key for key, value in expected.items() if selected.get(key) != value]
    if failed:
        raise PreflightError(f"Agentic account sanity check failed: {', '.join(failed)}")
    result["selected_account_classification"] = {
        key: selected[key] for key in _PREFLIGHT_ACCOUNT_CLASSIFICATION_FIELDS
    }
    return result


def enforce_preflight_stage(stage: str, result: dict[str, Any]) -> None:
    reasons: list[str] = []
    if result["agentic_account_count"] != 1:
        reasons.append("The dedicated Agentic account was not uniquely identified")
    if not result["unique_agentic_account"]:
        reasons.append("Unique Agentic account confirmation is false")
    if not result["passed"]:
        reasons.append(f"{stage} stage did not pass")
    if stage != "identity" and not result["account_reconciled"]:
        reasons.append("Agentic account reconciliation failed")
    if result["errors"]:
        reasons.append(f"{stage} stage returned errors")
    if reasons:
        raise PreflightError("; ".join(reasons))


def enforce_preflight_result(result: dict[str, Any]) -> None:
    reasons: list[str] = []
    if result.get("boundary_status") != "PASS": reasons.append("Deterministic SHADOW boundary failed")
    for stage in ("identity_job", "portfolio_job", "positions_job", "orders_job"):
        if result.get(stage, {}).get("status") != "PASS": reasons.append(f"{stage} failed")
    if reasons: raise PreflightError("; ".join(reasons))


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
