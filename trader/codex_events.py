from __future__ import annotations

import json
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .models import CodexRunError


KNOWN_NON_TOOL_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed", "error"})
CODEX_EVENT_PROTOCOL = "codex-exec-jsonl-v1-codex-0.147.0"
PROHIBITED_TOOL_PREFIXES = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "add_", "remove_", "exercise_")
KNOWN_ITEM_NON_TOOL_TYPES = frozenset({"agent_message", "reasoning", "message", "todo_list", "plan_update"})
KNOWN_TOOL_ITEM_TYPES = frozenset({"mcp_tool_call", "web_search", "function_call", "tool_call", "command_execution"})
EXPECTED_PREFLIGHT_TOOLS = ("get_accounts", "get_portfolio", "get_equity_orders", "get_equity_positions")
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s,;\"']+|((?:session[_ -]?id|oauth[_ -]?token|authorization|cookie|brokerage[_ -]?account[_ -]?id|account[_ -]?(?:number|id))\s*[:=]\s*)[^\s,;]+|([?&](?:token|key|secret|code)=)[^&\s]+|()\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)


@dataclass(frozen=True)
class ParsedEventStream:
    events: list[dict[str, Any]]
    usage: dict[str, Any]
    tool_calls: dict[str, int]
    web_searches: int
    agent_messages: int


@dataclass(frozen=True)
class _ToolStart:
    item_type: str
    server: str | None
    tool: str | None


def parse_codex_jsonl(stdout: str, *, returncode: int = 0, allow_nonzero: bool = False) -> ParsedEventStream:
    events: list[dict[str, Any]] = []
    for number, raw in enumerate(stdout.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexRunError(f"Malformed Codex JSONL event on line {number}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise CodexRunError(f"Malformed Codex event envelope on line {number}")
        events.append(value)
    if not events:
        raise CodexRunError("Codex event stream was empty or truncated")

    diagnostics = _failure_diagnostics(events, returncode)
    if diagnostics["structured_error_count"]:
        raise CodexRunError("Codex emitted terminal failure event: error", diagnostics=diagnostics)
    if returncode != 0 and not allow_nonzero:
        raise CodexRunError(f"Codex exited nonzero ({returncode}); structured output is not accepted", diagnostics=diagnostics)

    calls: Counter[str] = Counter()
    web_searches = 0
    usage: dict[str, Any] = {}
    terminal = False
    turn_started = 0
    turn_completed = 0
    started_tools: dict[str, _ToolStart] = {}
    completed_tool_ids: set[str] = set()
    agent_messages = 0
    for event in events:
        event_type = event["type"]
        if terminal and event_type != "turn.completed":
            raise CodexRunError("Codex emitted structured events after turn.completed")
        if event_type in {"item.started", "item.updated"}:
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise CodexRunError(f"Malformed {event_type} event")
            item_type = item["type"]
            if item_type not in KNOWN_ITEM_NON_TOOL_TYPES | KNOWN_TOOL_ITEM_TYPES:
                if "tool" in item_type or "function" in item_type or "search" in item_type:
                    raise CodexRunError(f"Unknown tool-execution event shape: {item_type}")
                raise CodexRunError(f"Unknown item type: {item_type}")
            if event_type == "item.started" and item_type in KNOWN_TOOL_ITEM_TYPES:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id or item_id in started_tools:
                    raise CodexRunError("Tool start event has a missing or duplicate id")
                server, tool = _extract_tool_identity(item, item_type)
                if item_type in {"mcp_tool_call", "function_call", "tool_call"} and tool is None:
                    raise CodexRunError("Tool start event omitted its tool identity")
                if item_type == "mcp_tool_call" and server is None:
                    raise CodexRunError("MCP tool start event omitted its server identity")
                started_tools[item_id] = _ToolStart(item_type, server, tool)
            elif event_type == "item.updated" and item_type in KNOWN_TOOL_ITEM_TYPES:
                item_id = item.get("id")
                if not isinstance(item_id, str) or item_id not in started_tools:
                    raise CodexRunError("Tool update event has no matching start event")
                started = started_tools[item_id]
                if started.item_type != item_type:
                    raise CodexRunError("Tool update event does not match its start event")
                updated_server, updated_tool = _extract_tool_identity(item, item_type)
                if updated_server is not None and updated_server != started.server:
                    raise CodexRunError("MCP tool server identity changed during its lifecycle")
                if updated_tool is not None and updated_tool != started.tool:
                    raise CodexRunError("Tool identity changed during its lifecycle")
        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise CodexRunError("Malformed item.completed event")
            item_type = item["type"]
            if item_type in KNOWN_ITEM_NON_TOOL_TYPES:
                if item_type in {"agent_message", "message"}:
                    agent_messages += 1
            elif item_type in KNOWN_TOOL_ITEM_TYPES:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id or item_id in completed_tool_ids:
                    raise CodexRunError("Tool completion event has a missing or duplicate id")
                if item_id not in started_tools:
                    raise CodexRunError("Tool completion event has no matching start event")
                started = started_tools[item_id]
                if started.item_type != item_type:
                    raise CodexRunError("Tool terminal event does not match its start event")
                completed_tool_ids.add(item_id)
                completed_server, completed_tool = _extract_tool_identity(item, item_type)
                if completed_server is not None and completed_server != started.server:
                    raise CodexRunError("MCP tool server identity changed during its lifecycle")
                if completed_tool is not None and completed_tool != started.tool:
                    raise CodexRunError("Tool identity changed during its lifecycle")
                server = started.server
                name = started.tool
                if item_type == "web_search":
                    name = "web_search"
                    web_searches += 1
                elif item_type == "command_execution":
                    name = "command_execution"
                elif name is None:
                    raise CodexRunError("Tool identity could not be resolved from its completed lifecycle")
                status = item.get("status")
                if status is not None and status != "completed":
                    raise CodexRunError(f"Tool call did not complete successfully: {name}")
                normalized = name
                if normalized.startswith(PROHIBITED_TOOL_PREFIXES):
                    raise CodexRunError(f"Observed prohibited tool activity: {normalized}")
                if item_type == "mcp_tool_call":
                    if server is None:
                        raise CodexRunError("MCP tool identity could not be resolved from its completed lifecycle")
                    normalized = f"{server}::{normalized}"
                calls[normalized] += 1
            elif "tool" in item_type or "function" in item_type or "search" in item_type:
                raise CodexRunError(f"Unknown tool-execution event shape: {item_type}")
            else:
                raise CodexRunError(f"Unknown completed item type: {item_type}")
        elif event_type in {"item.started", "item.updated"}:
            pass
        elif event_type == "turn.completed":
            turn_completed += 1
            if turn_completed != 1:
                raise CodexRunError("Expected exactly one turn.completed event")
            terminal = True
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage.update(candidate)
        elif event_type in KNOWN_NON_TOOL_EVENTS:
            if event_type == "turn.started":
                turn_started += 1
            if event_type in {"turn.failed", "error"}:
                raise CodexRunError(f"Codex emitted terminal failure event: {event_type}")
        elif event_type in {"tool_call.completed", "mcp_tool_call.completed"} or "tool" in event_type:
            raise CodexRunError(f"Unknown tool-execution event envelope: {event_type}")
        else:
            raise CodexRunError(f"Unknown Codex event type: {event_type}")
    if turn_started != 1:
        raise CodexRunError(f"Expected exactly one turn.started event; found {turn_started}")
    if not terminal:
        raise CodexRunError("Codex event stream ended without turn.completed")
    incomplete = sorted(set(started_tools) - completed_tool_ids)
    if incomplete:
        raise CodexRunError("Codex event stream contains unterminated tool calls")
    return ParsedEventStream(events, usage, dict(calls), web_searches, agent_messages)


def _failure_diagnostics(events: list[dict[str, Any]], returncode: int) -> dict[str, Any]:
    sequence: list[dict[str, Any]] = []
    expected = {name: "NOT_OBSERVED" for name in EXPECTED_PREFLIGHT_TOOLS}
    counts = Counter(event.get("type") for event in events)
    structured_error = None
    agent_message_count = 0
    identities: dict[str, tuple[str | None, str | None]] = {}
    for index, event in enumerate(events):
        event_type = event.get("type")
        entry: dict[str, Any] | None = None
        if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error"}:
            entry = {"sequence": index, "event": "structured_error" if event_type == "error" else event_type}
        elif event_type in {"item.started", "item.completed"}:
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in KNOWN_TOOL_ITEM_TYPES:
                item_id = item.get("id")
                server, tool = _extract_tool_identity(item, str(item.get("type")))
                if event_type == "item.started" and isinstance(item_id, str):
                    identities[item_id] = (server, tool)
                elif isinstance(item_id, str) and item_id in identities:
                    prior_server, prior_tool = identities[item_id]
                    server = server if server is not None else prior_server
                    tool = tool if tool is not None else prior_tool
                if tool is not None:
                    entry = {
                        "sequence": index,
                        "event": "tool.started" if event_type == "item.started" else "tool.completed",
                        "server": _normalize_metadata(server) if server is not None else None,
                        "tool": _normalize_metadata(tool),
                        "item_id": _safe_item_id(item_id),
                    }
                    if tool in expected:
                        if event_type == "item.completed":
                            expected[tool] = "COMPLETED" if item.get("status") in (None, "completed") else "FAILED"
                        elif expected[tool] == "NOT_OBSERVED":
                            expected[tool] = "STARTED"
            elif event_type == "item.completed" and isinstance(item, dict) and item.get("type") in {"agent_message", "message"}:
                agent_message_count += 1
                entry = {"sequence": index, "event": "agent_message.completed"}
        if entry is not None:
            sequence.append(entry)
        if event_type == "error" and structured_error is None:
            structured_error = _sanitize_structured_error(event)
    return {
        "process_return_code": returncode,
        "structured_error": structured_error,
        "event_sequence": sequence,
        "expected_tools": expected,
        "jsonl_parse_started": True,
        "jsonl_parse_completed": True,
        "turn_started_count": counts["turn.started"],
        "turn_completed_count": counts["turn.completed"],
        "turn_failed_count": counts["turn.failed"],
        "structured_error_count": counts["error"],
        "agent_message_count": agent_message_count,
        "required_tool_validation_reached": False,
        "required_tool_validation_passed": False,
        "prohibited_tool_validation_reached": False,
        "prohibited_tool_validation_passed": False,
        "schema_validation_reached": False,
        "schema_validation_passed": False,
        "semantic_validation_reached": False,
        "semantic_validation_passed": False,
        "teardown_classifier_reached": False,
        "teardown_classifier_result": None,
        "turn_completed": counts["turn.completed"] > 0,
        "turn_failed": counts["turn.failed"] > 0,
    }


def _sanitize_structured_error(event: dict[str, Any]) -> dict[str, Any]:
    error = event.get("error") if isinstance(event.get("error"), dict) else {}
    info = event.get("codexErrorInfo") if isinstance(event.get("codexErrorInfo"), dict) else error.get("codexErrorInfo", {})
    info = info if isinstance(info, dict) else {}
    result: dict[str, Any] = {"event_type": "error"}
    fields = (
        ("message", error.get("message", event.get("message"))),
        ("codex_error_type", info.get("type")),
        ("http_status", event.get("httpStatusCode", error.get("httpStatusCode"))),
        ("code", error.get("code", event.get("code"))),
        ("name", error.get("name", event.get("name"))),
    )
    for key, value in fields:
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            result[key] = _sanitize_text(str(value)) if isinstance(value, str) else value
    return result


def sanitize_diagnostic_text(value: str) -> str:
    """Redact credentials and account identifiers before diagnostics persist."""
    return _SECRET_TEXT.sub(lambda match: (match.group(1) or match.group(2) or match.group(3) or "") + "<redacted>", value)[:600]


# Kept private within this module's structured-error implementation, while the
# public name above is shared by every persistence and console boundary.
_sanitize_text = sanitize_diagnostic_text


def _normalize_metadata(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_.:-]+", "_", value.strip().lower())
    return cleaned[:100]


def _safe_item_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "<redacted>"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _normalize_observed_name(name: str) -> str:
    value = name.strip().lower()
    return value.split("__")[-1]


def _extract_tool_identity(item: dict[str, Any], item_type: str) -> tuple[str | None, str | None]:
    recognized = [(field, item[field]) for field in ("name", "tool_name", "tool") if field in item]
    values: list[str] = []
    for _field, value in recognized:
        if not isinstance(value, str) or not value.strip():
            raise CodexRunError("Tool event has malformed explicit identity")
        normalized = _normalize_observed_name(value)
        if normalized not in values:
            values.append(normalized)
    if len(values) > 1:
        raise CodexRunError("Tool event contains conflicting explicit identities")
    tool = values[0] if values else None
    server_value = item.get("server")
    if server_value is None:
        server = None
    elif not isinstance(server_value, str) or not server_value.strip():
        raise CodexRunError("MCP tool event has malformed server identity")
    else:
        server = server_value.strip().lower()
    if item_type != "mcp_tool_call" and server is not None:
        raise CodexRunError("Non-MCP tool event unexpectedly supplied MCP server identity")
    return server, tool
