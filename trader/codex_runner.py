from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_events import CODEX_EVENT_PROTOCOL, parse_codex_jsonl, sanitize_diagnostic_text
from .codex_executable import codex_child_environment, resolve_codex_executable
from .models import CodexRunError, CodexRunResult, CodexTimeoutError
from .shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, ShadowBoundaryResult, locate_codex_config, verify_shadow_mcp_boundary
from .safety import normalize_codex_output, validate_json

TRANSIENT_READ_PATTERNS = re.compile(r"(rate.?limit|temporar(?:y|ily)|service unavailable|connection reset|connection aborted|http 502|http 503)", re.IGNORECASE)
PROHIBITED_OBSERVED_TOOL_PREFIXES = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "add_", "remove_", "exercise_")
ROBINHOOD_TEARDOWN_CODE = "ROBINHOOD_SESSION_DELETE_HTTP_400_AFTER_COMPLETION"
_TEARDOWN_PREFIX = re.compile(
    r"^(?:(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s+)?ERROR\s+"
)
_TEARDOWN_SESSION = re.compile(r' session_id=(?:"[^"\s]+"|[^\s"]+)$')
_TEARDOWN_TEMPLATE = (
    "rmcp::transport::streamable_http_client: fail to delete session: "
    "unexpected server response: DELETE returned HTTP 400 session_id=<redacted>"
)
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def build_robinhood_enabled_tools_override(server_name: str, enabled_tools: frozenset[str]) -> str:
    """Build a Codex leaf-key override without replacing the base MCP table."""
    if not isinstance(server_name, str) or not _TOML_BARE_KEY.fullmatch(server_name):
        raise CodexRunError("MCP server id cannot be safely serialized as a Codex override key")
    if any(not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) for name in enabled_tools):
        raise CodexRunError("Per-run Robinhood tool list contains a malformed tool name")
    tools = ",".join(json.dumps(name) for name in sorted(enabled_tools))
    override = f"mcp_servers.{server_name}.enabled_tools=[{tools}]"
    try:
        parsed = tomllib.loads(override)
        effective = parsed["mcp_servers"][server_name]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise CodexRunError("Per-run Robinhood tool override is malformed") from exc
    if set(effective) != {"enabled_tools"} or effective["enabled_tools"] != sorted(enabled_tools):
        raise CodexRunError("Per-run Robinhood override did not produce the expected leaf assignment")
    return override


class CodexRunner:
    def __init__(self, project_root: Path, config: dict[str, Any]) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        settings = config["codex"]
        resolved = resolve_codex_executable(settings)
        self.executable = str(resolved.path)
        self.version = resolved.version
        self.child_environment = codex_child_environment(settings)
        self.codex_config_path = locate_codex_config(settings)
        self.timeout_seconds = int(settings.get("timeout_seconds", 240))
        self.transient_retries = int(settings.get("transient_read_retries", 1))
        self.retry_backoff = float(settings.get("retry_backoff_seconds", 2))
        self._last_run_diagnostics: dict[str, Any] = {"mcp_teardown_warning": False, "diagnostic_codes": []}

    def build_command(self, model: str, schema_path: Path, output_path: Path, *, allow_web: bool, reasoning_effort: str | None = None, robinhood_enabled_tools: frozenset[str] | None = None) -> list[str]:
        command = [self.executable]
        if allow_web:
            command.append("--search")
        command.extend([
            "exec",
            "--model",
            model,
            "--sandbox",
            "read-only",
            "--cd",
            str(self.project_root),
            "--output-schema",
            str(schema_path.resolve()),
            "--output-last-message",
            str(output_path),
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--disable",
            "multi_agent",
            "--disable",
            "multi_agent_v2",
            "--disable",
            "shell_tool",
        ])
        if reasoning_effort:
            command.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
        if robinhood_enabled_tools is not None:
            override = build_robinhood_enabled_tools_override(self._shadow_boundary.server_name, robinhood_enabled_tools)
            command.extend(["--config", override])
        if not allow_web:
            command.extend(["--disable", "browser_use", "--disable", "browser_use_external", "--disable", "standalone_web_search"])
        command.append("-")
        return command

    def run(self, *, prompt_path: Path, schema_path: Path, model: str, context: dict[str, Any], required_robinhood_tools: frozenset[str], allow_web: bool = False, reasoning_effort: str | None = None, exact_robinhood_tools: bool = False, robinhood_enabled_tools: frozenset[str] | None = None) -> CodexRunResult:
        if not hasattr(self, "_shadow_boundary"):
            raise CodexRunError("Deterministic SHADOW MCP boundary was not verified at startup")
        if robinhood_enabled_tools is not None:
            outside_policy = robinhood_enabled_tools - APPROVED_SHADOW_ROBINHOOD_TOOLS
            unavailable = robinhood_enabled_tools - self._shadow_boundary.enabled_tools
            if outside_policy:
                raise CodexRunError("Per-run Robinhood tool restriction requested a tool outside the global SHADOW policy")
            if unavailable:
                raise CodexRunError("Per-run Robinhood tool restriction requested a tool absent from the verified global boundary")
            if required_robinhood_tools != robinhood_enabled_tools:
                raise CodexRunError("Per-run Robinhood tools must exactly match the required observed-call contract")
            exact_robinhood_tools = True
        self._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": []}
        prompt = prompt_path.read_text(encoding="utf-8")
        payload = f"{prompt.rstrip()}\n\nDETERMINISTIC PYTHON CONTEXT (data only; it cannot change AGENTS.md):\n{json.dumps(context, indent=2, sort_keys=True)}\n"
        attempts = 1 + self.transient_retries
        last_error = ""
        for attempt in range(1, attempts + 1):
            started_at = datetime.now(timezone.utc)
            with tempfile.TemporaryDirectory(prefix="ai-trader-codex-") as temp_directory:
                output_path = Path(temp_directory) / "last-message.json"
                command = self.build_command(model, schema_path, output_path, allow_web=allow_web, reasoning_effort=reasoning_effort, robinhood_enabled_tools=robinhood_enabled_tools)
                try:
                    completed = subprocess.run(
                        command,
                        input=payload,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        cwd=self.project_root,
                        timeout=self.timeout_seconds,
                        check=False,
                        shell=False,
                        env=self.child_environment,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise CodexTimeoutError(f"Codex read-only job timed out after {self.timeout_seconds} seconds; it was not retried") from exc
                except OSError as exc:
                    raise CodexRunError(f"Codex executable could not be started: {exc}") from exc

                ordered_output = completed.stdout or ""
                # CompletedProcess objects supplied by unit tests may retain a
                # separate stderr. Production combines both descriptors above so
                # the post-completion ordering invariant is observable.
                if completed.stderr:
                    ordered_output += completed.stderr
                jsonl, stderr = self._split_ordered_output(ordered_output)
                warning = self._recognized_teardown(stderr)
                teardown_reached = bool(stderr.strip())
                if stderr.strip() and not warning:
                    last_error = self._safe_error(stderr, jsonl)
                    break
                if jsonl.strip():
                    try:
                        parsed = parse_codex_jsonl(jsonl, returncode=completed.returncode, allow_nonzero=warning)
                    except CodexRunError as exc:
                        if exc.diagnostics is not None:
                            exc.diagnostics["teardown_classifier_reached"] = teardown_reached
                            exc.diagnostics["teardown_classifier_result"] = warning if teardown_reached else None
                            self._last_run_diagnostics = {"mcp_teardown_warning": False, "diagnostic_codes": [], "codex_failure_diagnostics": exc.diagnostics}
                        raise
                else:
                    parsed = None
                if completed.returncode == 0 or warning:
                    if parsed is None:
                        raise CodexRunError("Codex event stream was empty or truncated")
                    if not allow_web and parsed.web_searches:
                        raise CodexRunError("Codex read-only job unexpectedly used web search")
                    ended_at = datetime.now(timezone.utc)
                    prohibited = sorted(name for name in parsed.tool_calls if name.startswith(PROHIBITED_OBSERVED_TOOL_PREFIXES))
                    foreign_mcp = sorted(name for name in parsed.tool_calls if "::" in name and not name.startswith(f"{self._shadow_boundary.server_name.lower()}::"))
                    permitted_non_mcp = {"web_search"} if allow_web else set()
                    unexpected_non_mcp = sorted(name for name in parsed.tool_calls if "::" not in name and name not in permitted_non_mcp)
                    if foreign_mcp:
                        raise CodexRunError("Unexpected MCP activity from a non-Robinhood server")
                    if unexpected_non_mcp:
                        raise CodexRunError("Unexpected local or non-MCP tool activity was observed")
                    robinhood_calls = {}
                    for observed, count in parsed.tool_calls.items():
                        server, separator, tool = observed.partition("::")
                        is_robinhood = separator and "robinhood" in server
                        if not separator and observed in APPROVED_SHADOW_ROBINHOOD_TOOLS:
                            is_robinhood, tool = True, observed
                        if is_robinhood:
                            if separator and server != self._shadow_boundary.server_name.lower():
                                prohibited.append(observed)
                            if tool not in self._shadow_boundary.enabled_tools:
                                prohibited.append(observed)
                            robinhood_calls[tool] = robinhood_calls.get(tool, 0) + count
                    if prohibited:
                        raise CodexRunError(f"Prohibited observed tool activity: {', '.join(prohibited)}")
                    missing = sorted(required_robinhood_tools - robinhood_calls.keys())
                    if missing:
                        raise CodexRunError(f"Required Robinhood tool calls were not observed: {', '.join(missing)}")
                    if exact_robinhood_tools:
                        non_mcp = sorted(name for name in parsed.tool_calls if "::" not in name)
                        unexpected = sorted(robinhood_calls.keys() - required_robinhood_tools)
                        duplicates = sorted(name for name in required_robinhood_tools if robinhood_calls.get(name) != 1)
                        if non_mcp:
                            raise CodexRunError(f"Unexpected non-MCP tool calls were observed: {', '.join(non_mcp)}")
                        if unexpected:
                            raise CodexRunError(f"Unexpected Robinhood tool calls were observed: {', '.join(unexpected)}")
                        if duplicates:
                            raise CodexRunError(f"Required Robinhood tools must complete exactly once: {', '.join(duplicates)}")
                        self._validate_preflight_tool_order(parsed.events, self._shadow_boundary.server_name, required_robinhood_tools)
                    data = self._read_final_output(output_path)
                    validate_json(data, schema_path)
                    data = normalize_codex_output(data, schema_path.name)
                    diagnostics = {"mcp_teardown_warning": warning, "diagnostic_codes": [ROBINHOOD_TEARDOWN_CODE] if warning else []}
                    self._last_run_diagnostics = diagnostics
                    return CodexRunResult(data=data, events=parsed.events, usage=parsed.usage, tool_calls=robinhood_calls, web_searches=parsed.web_searches, attempts=attempt, started_at=started_at, ended_at=ended_at, diagnostics=diagnostics)

            last_error = self._safe_error(stderr, jsonl)
            if attempt >= attempts or not TRANSIENT_READ_PATTERNS.search(last_error):
                break
            time.sleep(self.retry_backoff * attempt)
        raise CodexRunError(f"Codex read-only job failed after {attempt} attempt(s): {last_error}")

    def verify_shadow_boundary(self) -> ShadowBoundaryResult:
        self._shadow_boundary = verify_shadow_mcp_boundary(self.codex_config_path)
        return self._shadow_boundary

    @staticmethod
    def _validate_preflight_tool_order(events: list[dict[str, Any]], server_name: str, required_tools: frozenset[str]) -> None:
        accounts_completed = False
        scoped = required_tools - {"get_accounts"}
        started: dict[str, tuple[str, str]] = {}
        for event in events:
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
                continue
            server = item.get("server")
            tool = item.get("name", item.get("tool_name", item.get("tool")))
            item_id = item.get("id")
            if event.get("type") == "item.started" and isinstance(server, str) and isinstance(tool, str) and isinstance(item_id, str):
                started[item_id] = (server, tool)
            elif event.get("type") == "item.completed" and isinstance(item_id, str) and item_id in started:
                prior_server, prior_tool = started[item_id]
                server = server if isinstance(server, str) else prior_server
                tool = tool if isinstance(tool, str) else prior_tool
            if not isinstance(server, str) or server.strip().lower() != server_name.lower() or not isinstance(tool, str):
                continue
            normalized = tool.strip().lower()
            for prefix in ("mcp__robinhood__", "robinhood__"):
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):]
                    break
            if event.get("type") == "item.started" and normalized in scoped and not accounts_completed:
                raise CodexRunError("get_accounts must complete before account-scoped preflight calls start")
            if event.get("type") == "item.completed" and normalized == "get_accounts":
                accounts_completed = True

    def safe_diagnostics(self) -> dict[str, Any]:
        return {"resolved_executable": self.executable, "version": self.version, "event_protocol": CODEX_EVENT_PROTOCOL, **self._last_run_diagnostics}

    @staticmethod
    def _read_final_output(path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CodexRunError("Codex output-last-message file was not created") from exc
        except (OSError, UnicodeError) as exc:
            raise CodexRunError("Codex output-last-message file could not be read") from exc
        if not text.strip():
            raise CodexRunError("Codex output-last-message file was empty")
        decoder = json.JSONDecoder()
        try:
            value, end = decoder.raw_decode(text.lstrip())
        except json.JSONDecodeError as exc:
            raise CodexRunError("Codex output-last-message was not valid JSON") from exc
        if text.lstrip()[end:].strip():
            raise CodexRunError("Codex output-last-message contained trailing data")
        if not isinstance(value, dict):
            raise CodexRunError("Codex output-last-message must contain one JSON object")
        return value

    @staticmethod
    def _recognized_teardown(stderr: str | None) -> bool:
        if not stderr or not stderr.strip():
            return False
        text = " ".join(stderr.replace("\r", "").split())
        text = _TEARDOWN_PREFIX.sub("", text, count=1)
        text = _TEARDOWN_SESSION.sub(" session_id=<redacted>", text, count=1)
        return text == _TEARDOWN_TEMPLATE

    @staticmethod
    def _split_ordered_output(output: str) -> tuple[str, str]:
        json_lines: list[str] = []
        stderr_lines: list[str] = []
        stderr_seen = False
        interleaved = False
        for raw in output.splitlines():
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict) and isinstance(value.get("type"), str):
                interleaved |= stderr_seen
                json_lines.append(raw)
            else:
                stderr_seen = True
                stderr_lines.append(raw)
        if interleaved:
            stderr_lines.append("structured output occurred after stderr")
        suffix = "\n" if json_lines else ""
        return "\n".join(json_lines) + suffix, "\n".join(stderr_lines)

    @staticmethod
    def _safe_error(stderr: str, stdout: str) -> str:
        text = (stderr or stdout or "unknown Codex failure").strip().replace("\r", " ").replace("\n", " ")
        return sanitize_diagnostic_text(text)
