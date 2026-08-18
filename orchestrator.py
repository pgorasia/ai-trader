from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import sys
import time as time_module
import unittest
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader.codex_runner import CodexRunner
from trader.codex_events import sanitize_diagnostic_text
from trader.clock import SystemClock, TrustedClock
from trader.instance_lock import SingleInstanceLock
from trader.market_calendar import ET, EquityMarketCalendar
from trader.models import CodexRunError, PreflightError, SchemaValidationError, ShadowPlanStatus, StateCorruptionError, TraderError
from trader.readiness import calculate_readiness
from trader.reporting import cycle_markdown, eod_markdown, preflight_report_artifact, senior_markdown, write_json_companion, write_non_destructive_text
from trader.safety import FORBIDDEN_ROBINHOOD_TOOLS, cooldown_until, derive_preflight_identity, enforce_preflight_result, enforce_preflight_stage, load_config, normalize_tool_name, offline_preflight, validate_json, write_alert
from trader.scheduler import SessionScheduler
from trader.shadow_monitor import ShadowPlanMonitor
from trader.state import STRATEGY_VERSION, StateStore
from trader.automation import DaemonSupervisor, Heartbeat, health_check
from trader.job_contracts import JOB_TOOL_CONTRACTS, validate_job_contracts
from trader.operations import (complete as complete_operation, eligible as operation_eligible,
    ensure_controls, fail as fail_operation, operation as find_operation, prepare as prepare_operation,
    record_ai_failure, record_ai_success, start as start_operation)
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, locate_codex_config, verify_shadow_mcp_boundary


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "strategy.yaml"
LOGGER = logging.getLogger("ai_trader")


def audit(event: str, **values: Any) -> None:
    fields = " ".join(f"{key}={sanitize_diagnostic_text(str(value))}" for key, value in values.items() if value is not None)
    LOGGER.info("AI_TRADER event=%s%s", event, f" {fields}" if fields else "")


def validate_unattended_config(root: Path = ROOT) -> dict[str, Any]:
    """Local-only startup validation: no runner, child process, MCP, or network activity."""
    config = load_config(root / "config" / "strategy.yaml")
    offline_preflight(root, config)
    problems = validate_job_contracts()
    if config.get("mode") != "SHADOW": problems.append("configured mode is not SHADOW")
    if len(APPROVED_SHADOW_ROBINHOOD_TOOLS) != 22: problems.append("global SHADOW tool boundary is not exactly 22 tools")
    try: boundary = verify_shadow_mcp_boundary(locate_codex_config(config["codex"]))
    except TraderError as exc: problems.append(sanitize_diagnostic_text(str(exc))); boundary = None
    configured_executable = config["codex"].get("executable", "auto")
    candidate = shutil.which("codex") if configured_executable in (None, "", "auto") else str(configured_executable)
    resolved = Path(candidate).expanduser().resolve(strict=False) if candidate else None
    if resolved is None or not resolved.is_file() or not os.access(resolved, os.X_OK):
        problems.append("Codex executable is not locally resolvable")
    return {"status": "PASS" if not problems else "FAIL", "mode": config.get("mode"),
            "problems": problems, "required_approvals": sorted(set().union(*JOB_TOOL_CONTRACTS.values())),
            "global_tool_count": len(APPROVED_SHADOW_ROBINHOOD_TOOLS),
            "codex_executable": str(resolved) if resolved else None,
            "robinhood_server": boundary.server_name if boundary else None}


def aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SchemaValidationError(f"Expected timezone-aware timestamp: {value}")
    return parsed


class ShadowOrchestrator:
    def __init__(self, root: Path = ROOT, runner: CodexRunner | None = None, calendar: EquityMarketCalendar | None = None, clock: TrustedClock | None = None) -> None:
        self.root = root.resolve()
        self.config = load_config(self.root / "config" / "strategy.yaml")
        offline_preflight(self.root, self.config)
        self.store = StateStore(self.root / "state", self.config["timezone"])
        self.runner = runner or CodexRunner(self.root, self.config)
        self.boundary = self.runner.verify_shadow_boundary()
        self.calendar = calendar or EquityMarketCalendar(self.config["exchange_calendar"], int(self.config["schedule"]["eod_offset_minutes"]))
        self.scheduler = SessionScheduler(self.config["schedule"])
        self.monitor = ShadowPlanMonitor()
        self.clock = clock or SystemClock()

    def _trusted_now(self) -> datetime:
        clock = getattr(self, "clock", None)
        return (clock.now() if clock else datetime.now(ET)).astimezone(ET)

    def _run_ai_job(self, state: dict[str, Any], *, operation_id: str, operation_type: str,
                    scheduled_for: datetime, max_attempts: int = 1, **runner_args: Any):
        now = self._trusted_now(); ensure_controls(state)
        record = prepare_operation(state, operation_id, operation_type, scheduled_for, max_attempts)
        circuit_open = state["ai_circuit"]["status"] == "OPEN"
        if not operation_eligible(record, now, circuit_open):
            raise PreflightError(f"AI operation is not eligible: {record['state']}")
        start_operation(record, now)
        counts = state["usage_counts"]
        counts["codex_subprocess_attempts"] = counts.get("codex_subprocess_attempts", 0) + 1
        audit(f"{operation_type}_START" if operation_type != "STAGE_B" else "CYCLE_START",
              operation_id=operation_id, attempt=record["attempt_number"])
        self.store.save(state)
        try:
            result = self.runner.run(**runner_args)
        except CodexRunError as exc:
            ended = self._trusted_now(); counts["codex_failed_attempts"] = counts.get("codex_failed_attempts", 0) + 1
            if operation_type == "EOD": counts["eod_failed_attempts"] = counts.get("eod_failed_attempts", 0) + 1
            if operation_type == "STAGE_B": counts["stage_b_failed_slots"] = counts.get("stage_b_failed_slots", 0) + 1
            decision = fail_operation(record, exc, ended, self.runner.safe_diagnostics())
            opened = record_ai_failure(state, exc, ended,
                int(self.config.get("circuit_breaker", {}).get("consecutive_failures", 3)),
                int(self.config.get("circuit_breaker", {}).get("total_failures", 5)))
            if opened:
                counts["session_circuit_breaker_trips"] = counts.get("session_circuit_breaker_trips", 0) + 1
                audit("SESSION_CIRCUIT_OPEN", failures=state["ai_circuit"]["failure_count"])
            self.store.save(state)
            audit("CYCLE_FAILED" if operation_type == "STAGE_B" else f"{operation_type}_FAILED",
                  operation_id=operation_id, error_class=type(exc).__name__, decision=decision)
            raise
        complete_operation(record, self._trusted_now()); record_ai_success(state)
        if operation_type == "STAGE_B": counts["stage_b_completed_runs"] = counts.get("stage_b_completed_runs", 0) + 1
        elif operation_type == "SOL": counts["sol_completed_runs"] = counts.get("sol_completed_runs", 0) + 1
        elif operation_type == "MONITOR": counts["monitor_completed_runs"] = counts.get("monitor_completed_runs", 0) + 1
        elif operation_type == "EOD": counts["eod_completed_runs"] = counts.get("eod_completed_runs", 0) + 1
        self.store.save(state)
        return result

    def preflight(self, state: dict[str, Any], now: datetime, *, operation_id: str | None = None) -> dict[str, Any]:
        self._ensure_strategy_version(state)
        started_at = now.astimezone(ET)
        operations = state.setdefault("preflight_operations", [])
        if operation_id is None:
            operation_id = f"preflight:{started_at.isoformat()}:{uuid.uuid4().hex}"
            artifact = preflight_report_artifact(state["session_date"], started_at, operation_id)
            record = {"operation_id": operation_id, "started_at": started_at.isoformat(), "completed_at": None, "status": "STARTED", "report_artifact": artifact.as_posix()}
            operations.append(record)
            state["operation_ids"].append(operation_id)
            self.store.save(state)
        else:
            matches = [item for item in operations if item.get("operation_id") == operation_id]
            if len(matches) != 1:
                raise StateCorruptionError("Preflight retry operation_id is missing or ambiguous")
            record = matches[0]
            artifact = preflight_report_artifact(state["session_date"], aware(record["started_at"]), operation_id)
            if record.get("report_artifact") != artifact.as_posix():
                raise StateCorruptionError("Preflight operation artifact identity is inconsistent")
        report_path = self.root / artifact
        recovered = self._recover_preflight_report(state, record, report_path)
        if recovered is not None:
            if recovered["status"] == "COMPLETED":
                return recovered["result"]
            raise PreflightError("Persisted preflight operation previously failed")

        working = deepcopy(state)
        working_record = next(item for item in working["preflight_operations"] if item["operation_id"] == operation_id)
        result = {
            "timestamp": started_at.isoformat(),
            "boundary_status": "PASS",
            "account_consistency": "INDEPENDENT_UNIQUE_ACCOUNT_CHECKS",
            "identity_job": {"status": "NOT_STARTED", "observed_calls": {}, "mcp_teardown_warning_codes": []},
            "portfolio_job": {"status": "NOT_STARTED", "observed_calls": {}, "mcp_teardown_warning_codes": []},
            "positions_job": {"status": "NOT_STARTED", "observed_calls": {}, "mcp_teardown_warning_codes": []},
            "orders_job": {"status": "NOT_STARTED", "observed_calls": {}, "mcp_teardown_warning_codes": []},
        }
        job_results = []
        job_diagnostics: dict[str, Any] = {}
        working["usage_counts"]["preflight_runs"] += 1
        try:
            boundary = self.boundary
            context = {"mode": "SHADOW", "requested_timestamp": now.astimezone(ET).isoformat(), "deterministic_boundary_policy": boundary.policy_version}
            stages = (
                ("identity", "identity_job", "preflight.md", "preflight.schema.json", JOB_TOOL_CONTRACTS["PREFLIGHT_IDENTITY"]),
                ("portfolio", "portfolio_job", "preflight-portfolio.md", "preflight-portfolio.schema.json", JOB_TOOL_CONTRACTS["PREFLIGHT_PORTFOLIO"]),
                ("positions", "positions_job", "preflight-positions.md", "preflight-positions.schema.json", JOB_TOOL_CONTRACTS["PREFLIGHT_POSITIONS"]),
                ("orders", "orders_job", "preflight-orders.md", "preflight-orders.schema.json", JOB_TOOL_CONTRACTS["PREFLIGHT_ORDERS"]),
            )
            selected_classification = None
            for stage, report_key, prompt_name, schema_name, expected_tools in stages:
                result[report_key]["status"] = "FAIL"
                child = self.runner.run(
                    prompt_path=self.root / "prompts" / prompt_name,
                    schema_path=self.root / "schemas" / schema_name,
                    model=self.config["models"]["luna"],
                    context=context,
                    required_robinhood_tools=expected_tools,
                    allow_web=False,
                    robinhood_enabled_tools=expected_tools,
                    exact_robinhood_tools=True,
                )
                job_results.append(child)
                diagnostics = child.diagnostics or self.runner.safe_diagnostics()
                job_diagnostics[report_key] = diagnostics
                derive_preflight_identity(child.data)
                current_classification = child.data["selected_account_classification"]
                if selected_classification is None:
                    selected_classification = current_classification
                elif current_classification != selected_classification:
                    raise PreflightError("Safe Agentic account classification changed between preflight stages")
                summary = {"status": "FAIL", "observed_calls": dict(child.tool_calls), "mcp_teardown_warning_codes": list(diagnostics.get("diagnostic_codes", []))}
                if stage == "identity": summary.update({"agentic_account_count": child.data["agentic_account_count"], "unique_agentic_account": child.data["unique_agentic_account"]})
                elif stage == "portfolio": summary.update({"account_equity": child.data["account_equity"], "buying_power": child.data["buying_power"], "portfolio_status": child.data["portfolio_status"]})
                elif stage == "positions":
                    baseline_positions = [{"attribution": "BASELINE_EXTERNAL", "symbol": item["symbol"], "quantity": item["quantity"]} for item in child.data["baseline_positions"]]
                    working["baseline_positions"] = baseline_positions
                    summary.update({"baseline_position_count": child.data["baseline_position_count"], "baseline_positions_present": child.data["baseline_positions_present"], "baseline_positions": baseline_positions})
                else:
                    baseline_orders = [{"attribution": "BASELINE_EXTERNAL_ORDER", "symbol": item["symbol"], "side": item["side"], "state": item["state"]} for item in child.data["baseline_external_orders"]]
                    working["baseline_external_orders"] = baseline_orders
                    summary.update({"relevant_order_count": child.data["relevant_order_count"], "open_pending_count": child.data["open_pending_count"], "baseline_external_order_count": child.data["baseline_external_order_count"], "baseline_external_orders_present": child.data["baseline_external_orders_present"], "baseline_external_orders": baseline_orders})
                result[report_key] = summary
                if child.web_searches: raise PreflightError(f"{stage} preflight stage unexpectedly used web search")
                enforce_preflight_stage(stage, child.data)
                summary["status"] = "PASS"
            enforce_preflight_result(result)
        except TraderError as exc:
            for child in job_results: self._add_usage(working, child)
            self._record_failure(working, "PREFLIGHT", exc)
            completed_at = self._trusted_now().isoformat()
            metadata = {**working_record, "completed_at": completed_at, "status": "FAILED"}
            runner_diagnostics = self.runner.safe_diagnostics()
            report = {**metadata, "result": result, "cli_usage": [child.usage for child in job_results], "codex_jobs": job_diagnostics, "codex": runner_diagnostics, "mode": "SHADOW", "passed": False, "error": sanitize_diagnostic_text(str(exc))}
            if "codex_failure_diagnostics" in runner_diagnostics:
                report["codex_failure_diagnostics"] = runner_diagnostics["codex_failure_diagnostics"]
            write_json_companion(report_path, report)
            working_record.update(metadata)
            working_record["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            self.store.save(working)
            state.clear(); state.update(working)
            raise
        for child in job_results: self._add_usage(working, child)
        completed_at = self._trusted_now().isoformat()
        for error in working["errors"]:
            if error.get("category") == "PREFLIGHT" and not error.get("resolved", False):
                error["resolved"] = True
                error["resolved_at"] = completed_at
                error["resolution_operation_id"] = operation_id
        metadata = {**working_record, "completed_at": completed_at, "status": "COMPLETED"}
        report = {**metadata, "result": result, "cli_usage": [child.usage for child in job_results], "codex_jobs": job_diagnostics, "codex": self.runner.safe_diagnostics(), "boundary_policy": boundary.policy_version, "mode": "SHADOW", "passed": True}
        write_json_companion(report_path, report)
        working_record.update(metadata)
        working_record["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        self.store.save(working)
        state.clear(); state.update(working)
        return result

    def _recover_preflight_report(self, state: dict[str, Any], record: dict[str, Any], report_path: Path) -> dict[str, Any] | None:
        if not report_path.exists():
            if record.get("status") != "STARTED":
                raise StateCorruptionError("Terminal preflight operation is missing its report artifact")
            return None
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptionError("Preflight report artifact is malformed") from exc
        identity = ("operation_id", "started_at", "report_artifact")
        if any(report.get(key) != record.get(key) for key in identity) or report.get("status") not in {"COMPLETED", "FAILED"} or not report.get("completed_at"):
            raise StateCorruptionError(f"Report operation collision at {report_path}; existing evidence was preserved")
        if record.get("status") != "STARTED" and any(report.get(key) != record.get(key) for key in (*identity, "completed_at", "status")):
            raise StateCorruptionError(f"Report operation collision at {report_path}; existing evidence was preserved")
        expected_hash = record.get("report_sha256")
        if expected_hash and hashlib.sha256(report_path.read_bytes()).hexdigest() != expected_hash:
            raise StateCorruptionError(f"Report operation collision at {report_path}; existing evidence was preserved")
        if record.get("status") == "STARTED":
            record.update({"completed_at": report["completed_at"], "status": report["status"]})
            record["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            self.store.save(state)
        return report

    def run_luna_cycle(self, state: dict[str, Any], session, scheduled_for: datetime) -> dict[str, Any]:
        luna_started = self._trusted_now()
        if luna_started >= session.latest_entry:
            raise PreflightError("Trusted clock is at or after the latest-entry cutoff; Luna was not started")
        cycle_number = len(state["cycles"]) + 1
        cycle_id = f"{state['session_date']}-cycle-{cycle_number}"
        cooldown_context = self._cooldown_context(state, scheduled_for)
        context = {
            "cycle_id": cycle_id,
            "scheduled_for": scheduled_for.isoformat(),
            "session": self._session_context(session),
            "late_selectivity": self.scheduler.late_selectivity(session, scheduled_for),
            "scanner": self.config["scanner"],
            "cooldowns_and_prior_rejections": cooldown_context,
            "active_shadow_plan_count": self._active_plan_count(state),
        }
        result = self._run_ai_job(state, operation_id=f"stage_b:{scheduled_for.isoformat()}", operation_type="STAGE_B",
            scheduled_for=scheduled_for,
            prompt_path=self.root / "prompts" / "luna-stage-b.md",
            schema_path=self.root / "schemas" / "luna-cycle.schema.json",
            model=self.config["models"]["luna"],
            context=context,
            required_robinhood_tools=frozenset({"get_accounts", "get_portfolio", "get_equity_orders", "get_equity_positions", "run_scan"}),
            allow_web=False, robinhood_enabled_tools=JOB_TOOL_CONTRACTS["STAGE_B"],
        )
        luna_ended = self._trusted_now()
        cycle = result.data
        self._validate_luna(cycle, state, session, result.web_searches, expected_cycle_id=cycle_id, observed_tool_calls=result.tool_calls, observed_start=luna_started, observed_end=luna_ended)
        cycle["scheduled_for"] = scheduled_for.isoformat()
        cycle["cli_usage"] = result.usage
        cycle["cli_tool_calls"] = result.tool_calls
        cycle["cli_diagnostics"] = result.diagnostics
        state["baseline_external_orders"] = [
            {"attribution": "BASELINE_EXTERNAL_ORDER", "symbol": item["symbol"], "side": item["side"], "state": item["state"]}
            for item in cycle["account_status"]["baseline_external_orders"]
        ]
        state["cycles"].append(cycle)
        operation_id = f"luna:{cycle_id}"
        if operation_id in state["operation_ids"]:
            raise StateCorruptionError("Duplicate Luna operation ID")
        state["operation_ids"].append(operation_id)
        state["usage_counts"]["luna_runs"] += 1
        self._add_usage(state, result)
        self.store.save(state)
        write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-cycle-{cycle_number}.md", cycle_markdown(cycle))
        write_json_companion(self.root / "reports" / f"{state['session_date']}-cycle-{cycle_number}.json", cycle)
        if cycle["sol_escalation"]:
            if self._trusted_now() >= session.latest_entry:
                state["schedule_events"].append({"operation_id": f"sol-skip:{cycle_id}", "status": "SKIPPED_CUTOFF", "scheduled_for": scheduled_for.isoformat(), "observed_at": self._trusted_now().isoformat()})
                state["operation_ids"].append(f"sol-skip:{cycle_id}")
                self.store.save(state)
            else:
                self.run_senior(state, session, cycle)
        return cycle

    def run_senior(self, state: dict[str, Any], session, cycle: dict[str, Any]) -> dict[str, Any]:
        finalists = [item for item in cycle["finalists"] if item["classification"] in {"NEW", "MATERIALLY_REQUALIFIED"}]
        if not finalists:
            raise SchemaValidationError("Sol escalation requested without a qualifying finalist")
        maximum = int(self.config["experiment"]["maximum_research_candidates_per_session"])
        decisions: list[dict[str, Any]] = []
        for rank, finalist in enumerate(finalists[:maximum], 1):
            suffix = "" if rank == 1 else f":{rank}:{finalist['symbol']}"
            decision_operation = f"senior:{cycle['cycle_id']}{suffix}"
            source_id = cycle["cycle_id"] if rank == 1 else f"{cycle['cycle_id']}:research-{rank}:{finalist['symbol']}"
            if decision_operation in state["operation_ids"]:
                matches = [item for item in state["senior_decisions"] if item.get("source_cycle_id") == source_id]
                if len(matches) != 1:
                    raise StateCorruptionError("Persisted senior research operation has ambiguous provenance")
                decision = matches[0]
                number = state["senior_decisions"].index(decision) + 1
                write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-senior-{number}.md", senior_markdown(decision))
                write_json_companion(self.root / "reports" / f"{state['session_date']}-senior-{number}.json", decision)
                decisions.append(decision)
                continue

            sol_started = self._trusted_now()
            if sol_started >= session.latest_entry:
                break
            context = {
                "cycle_id": cycle["cycle_id"], "research_rank": rank,
                "finalists": [finalist], "session": self._session_context(session),
                "risk": self.config["risk"],
                "immutable_prior_rejections": self._cooldown_context(state, aware(cycle["timestamp"])),
            }
            result = self._run_ai_job(state, operation_id=decision_operation, operation_type="SOL",
                scheduled_for=sol_started,
                prompt_path=self.root / "prompts" / "sol-senior.md",
                schema_path=self.root / "schemas" / "senior-decision.schema.json",
                model=self.config["models"]["sol"], reasoning_effort=self.config["models"]["sol_reasoning_effort"],
                context=context, required_robinhood_tools=JOB_TOOL_CONTRACTS["SOL_SENIOR"], allow_web=True,
                robinhood_enabled_tools=JOB_TOOL_CONTRACTS["SOL_SENIOR"],
            )
            sol_ended = self._trusted_now()
            decision = result.data
            self._validate_senior(decision, [finalist], state, session, result.web_searches, observed_tool_calls=result.tool_calls, observed_start=sol_started, observed_end=sol_ended, allow_research_concurrency=True)
            if decision["decision"] == "SHADOW_TRADE_PLAN" and self._trusted_now() >= session.latest_entry:
                raise PreflightError("Trusted clock reached latest-entry cutoff before Shadow plan acceptance")
            decision.update({
                "source_cycle_id": source_id, "parent_cycle_id": cycle["cycle_id"], "research_rank": rank,
                "cli_usage": result.usage, "cli_tool_calls": result.tool_calls, "cli_diagnostics": result.diagnostics,
            })
            state["senior_decisions"].append(decision)
            state["operation_ids"].append(decision_operation)
            state["usage_counts"]["sol_runs"] += 1
            self._add_usage(state, result)
            state["usage_counts"]["web_searches"] += result.web_searches
            self._persist_rejections(state, decision)
            if decision["decision"] == "SHADOW_TRADE_PLAN":
                primary_exists = any(item.get("research_role", "PRIMARY") == "PRIMARY" for item in state["shadow_plans"])
                role = "CHALLENGER" if primary_exists else "PRIMARY"
                plan_id = f"{state['session_date']}-plan-{len(state['shadow_plans']) + 1}"
                original_plan = deepcopy({key: value for key, value in decision.items() if key not in {"cli_usage", "cli_tool_calls", "source_cycle_id", "parent_cycle_id", "research_rank"}})
                original_plan["trailing_activation_r"] = float(self.config["experiment"]["trailing_activation_r"])
                original_plan["trailing_lookback_bars"] = int(self.config["experiment"]["trailing_lookback_completed_bars"])
                frozen = {
                    "plan_id": plan_id, "frozen_at": decision["decision_timestamp"],
                    "attribution": "SHADOW_AI",
                    "research_role": role, "research_rank": rank, "parent_cycle_id": cycle["cycle_id"],
                    "original_plan": original_plan, "outcome": self.monitor.initial_outcome(),
                    "trailing_outcome": self.monitor.initial_trailing_outcome(decision["stop_price"]),
                }
                state["shadow_plans"].append(frozen)
                state["operation_ids"].append(f"plan:{plan_id}")
            self.store.save(state)
            number = len(state["senior_decisions"])
            write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-senior-{number}.md", senior_markdown(decision))
            write_json_companion(self.root / "reports" / f"{state['session_date']}-senior-{number}.json", decision)
            decisions.append(decision)
        if not decisions:
            raise PreflightError("No senior research evaluation completed before the entry cutoff")
        return decisions[0]

    def monitor_active_plans(self, state: dict[str, Any], now: datetime) -> None:
        monitor_operation = f"monitor:{now.astimezone(ET).isoformat()}"
        if monitor_operation in state["operation_ids"]:
            return
        active = [plan for plan in state["shadow_plans"] if self._plan_is_active(plan)]
        if not active:
            state["shadow_positions"] = []
            return
        context = {
            "timestamp": now.astimezone(ET).isoformat(),
            "plans": [{"plan_id": item["plan_id"], "symbol": item["original_plan"]["symbol"], "start_time": item["original_plan"]["decision_timestamp"]} for item in active],
        }
        result = self._run_ai_job(state, operation_id=monitor_operation, operation_type="MONITOR", scheduled_for=now,
            prompt_path=self.root / "prompts" / "shadow-monitor.md",
            schema_path=self.root / "schemas" / "shadow-monitor.schema.json",
            model=self.config["models"]["luna"],
            context=context,
            required_robinhood_tools=JOB_TOOL_CONTRACTS["MONITOR"],
            allow_web=False, robinhood_enabled_tools=JOB_TOOL_CONTRACTS["MONITOR"],
        )
        if result.diagnostics.get("mcp_teardown_warning"):
            state.setdefault("runtime_diagnostics", []).append({"operation_id": monitor_operation, **result.diagnostics})
        if result.web_searches or result.data["errors"]:
            raise CodexRunError("Shadow monitor returned a prohibited web call or read error")
        updated_by_id: dict[str, dict[str, Any]] = {}
        for plan in active:
            symbol = plan["original_plan"]["symbol"]
            if symbol not in result.data["symbol_bars"]:
                raise CodexRunError(f"Shadow monitor omitted bars for {symbol}")
            updated = self.monitor.evaluate(plan, result.data["symbol_bars"][symbol], now)
            updated_by_id[plan["plan_id"]] = self.monitor.evaluate_trailing(updated, result.data["symbol_bars"][symbol], now)
        state["shadow_plans"] = [updated_by_id.get(item["plan_id"], item) for item in state["shadow_plans"]]
        state["shadow_positions"] = [
            {"attribution": "SHADOW_AI", "plan_id": item["plan_id"], "symbol": item["original_plan"]["symbol"], "research_role": item.get("research_role", "PRIMARY"), "variant": variant, "entry_price": outcome["entry_price"], "entry_timestamp": outcome["entry_timestamp"]}
            for item in state["shadow_plans"] for variant, outcome in (("FIXED_TARGET", item["outcome"]), ("TRAILING_STOP", item.get("trailing_outcome", {}))) if outcome.get("status") == "OPEN"
        ]
        self._record_completed_trades(state)
        state["operation_ids"].append(monitor_operation)
        state["usage_counts"]["monitor_runs"] += 1
        self._add_usage(state, result)
        self.store.save(state)

    def eod(self, state: dict[str, Any], session) -> dict[str, Any]:
        eod_operation = f"eod:{state['session_date']}"
        ensure_controls(state)
        persisted = find_operation(state, eod_operation)
        if state["ai_circuit"]["status"] == "OPEN" or (persisted and persisted.get("state") == "FAILED_TERMINAL"):
            return self._finalize_eod_without_ai(state, session,
                "SKIPPED_CIRCUIT_OPEN" if state["ai_circuit"]["status"] == "OPEN" else "SKIPPED_AI_FAILED_TERMINAL")
        if eod_operation in state["operation_ids"]:
            if not state.get("eod_completed") or not isinstance(state.get("eod_review"), dict):
                raise StateCorruptionError("Persisted EOD operation has incomplete provenance")
            readiness = calculate_readiness(self.store.all_states(), self.config)
            companion = {"session_date": state["session_date"], "agent_review": state["eod_review"], "shadow_plans": state["shadow_plans"], "completed_shadow_trades": state["completed_shadow_trades"], "research_outcomes": state.get("research_outcomes", []), "readiness": readiness}
            write_json_companion(self.root / "reports" / f"{state['session_date']}-eod.json", companion)
            write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-eod.md", eod_markdown(state["session_date"], state["eod_review"], readiness, state["completed_shadow_trades"]))
            self._write_experiment_report(state["session_date"], readiness)
            return companion
        context = {
            "session": self._session_context(session),
            "senior_decisions": [{key: value for key, value in item.items() if key not in {"cli_usage", "cli_tool_calls"}} for item in state["senior_decisions"]],
            "shadow_plans": state["shadow_plans"],
        }
        result = self._run_ai_job(state, operation_id=eod_operation, operation_type="EOD",
            scheduled_for=session.eod_time, max_attempts=3,
            prompt_path=self.root / "prompts" / "eod-review.md",
            schema_path=self.root / "schemas" / "eod-review.schema.json",
            model=self.config["models"]["luna"],
            context=context,
            required_robinhood_tools=JOB_TOOL_CONTRACTS["EOD"],
            allow_web=False, robinhood_enabled_tools=JOB_TOOL_CONTRACTS["EOD"],
        )
        review = result.data
        review["cli_diagnostics"] = result.diagnostics
        if result.web_searches or review["errors"] or review["session_date"] != state["session_date"]:
            raise CodexRunError("EOD review failed data-integrity checks")
        if any(review["benchmark_closes"].get(symbol) is None for symbol in ("SPY", "QQQ")):
            raise CodexRunError("EOD review requires both SPY and QQQ benchmark closes")
        expected_reviews = [
            (rejection["symbol"], decision["decision_timestamp"])
            for decision in state["senior_decisions"]
            for rejection in decision["rejections"]
        ]
        actual_reviews = [(item["symbol"], item["decision_timestamp"]) for item in review["decision_reviews"]]
        if Counter(expected_reviews) != Counter(actual_reviews) or len(actual_reviews) != len(set(actual_reviews)):
            raise CodexRunError("EOD review did not cover every senior rejection exactly once")
        updated = []
        for plan in state["shadow_plans"]:
            symbol = plan["original_plan"]["symbol"]
            if symbol not in review["symbol_bars"]:
                raise CodexRunError(f"EOD review omitted required bars for {symbol}")
            bars = review["symbol_bars"][symbol]
            fixed = self.monitor.evaluate(plan, bars, session.market_close)
            updated.append(self.monitor.evaluate_trailing(fixed, bars, session.market_close))
        state["shadow_plans"] = updated
        state["shadow_positions"] = []
        self._record_completed_trades(state)
        state["usage_counts"]["eod_runs"] += 1
        self._add_usage(state, result)
        state["eod_completed"] = True
        state["eod_review"] = review
        state["operation_ids"].append(eod_operation)
        self.store.save(state)
        readiness = calculate_readiness(self.store.all_states(), self.config)
        state["readiness"] = readiness
        self.store.save(state)
        companion = {"session_date": state["session_date"], "agent_review": review, "shadow_plans": state["shadow_plans"], "completed_shadow_trades": state["completed_shadow_trades"], "research_outcomes": state.get("research_outcomes", []), "readiness": readiness}
        write_json_companion(self.root / "reports" / f"{state['session_date']}-eod.json", companion)
        write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-eod.md", eod_markdown(state["session_date"], review, readiness, state["completed_shadow_trades"]))
        self._write_experiment_report(state["session_date"], readiness)
        return companion

    def _finalize_eod_without_ai(self, state: dict[str, Any], session, status: str) -> dict[str, Any]:
        operation_id = f"eod:{state['session_date']}"
        record = prepare_operation(state, operation_id, "EOD", session.eod_time, 3)
        if record["state"] not in {"COMPLETED", "FAILED_TERMINAL"}:
            record.update({"state": "FAILED_TERMINAL", "completed_at": self._trusted_now().isoformat(), "next_retry_at": None})
        review = {"session_date": state["session_date"], "status": status,
                  "failure_summary": {"errors": len(state.get("errors", [])),
                                      "ai_failures": state.get("ai_circuit", {}).get("failure_count", 0)},
                  "metrics_retained": True}
        state.update({"eod_completed": True, "eod_review": review, "session_terminal": True})
        if operation_id not in state["operation_ids"]: state["operation_ids"].append(operation_id)
        state["usage_counts"]["eod_completed_runs"] = state["usage_counts"].get("eod_completed_runs", 0) + 1
        self.store.save(state); audit("SESSION_COMPLETE", eod=status)
        return {"session_date": state["session_date"], "agent_review": review,
                "shadow_plans": state["shadow_plans"], "completed_shadow_trades": state["completed_shadow_trades"],
                "research_outcomes": state.get("research_outcomes", []), "readiness": state.get("readiness")}

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(ET)).astimezone(ET)
        session = self.calendar.session_for(current.date())
        if session is None:
            raise PreflightError("The U.S. equity market is closed today")
        if current < max(session.market_open + timedelta(minutes=10), session.market_open.replace(hour=9, minute=40)):
            raise PreflightError("Stage-B scans are not permitted before 09:40 ET")
        state = self.store.load(session.session_date)
        self._regenerate_cycle_reports(state)
        self.preflight(state, current)
        if self._active_plan_count(state):
            self.monitor_active_plans(state, current)
            self.store.save(state)
            return {"action": "MONITORED_ACTIVE_PLAN"}
        if current >= session.latest_entry:
            raise PreflightError("New Shadow plans are closed for this session")
        try:
            cycle = self.run_luna_cycle(state, session, current)
        except TraderError as exc:
            self._record_failure(state, "LUNA_OR_SOL", exc)
            self.store.save(state)
            raise
        self.store.save(state)
        return cycle

    def run_session(self, now: datetime | None = None, *, preflight_already_passed: bool = False) -> None:
        current = (now or datetime.now(ET)).astimezone(ET)
        session = self.calendar.session_for(current.date())
        if session is None:
            print("Market closed: no Shadow session scheduled.")
            return
        state = self.store.load(session.session_date)
        self._regenerate_cycle_reports(state)
        if not preflight_already_passed:
            self.preflight(state, current)
            self.store.save(state)
        elif not any(item.get("status") == "COMPLETED" for item in state.get("preflight_operations", [])):
            raise PreflightError("Session research requires a completed preflight")
        completed_slots = {item.get("scheduled_for") for item in state["cycles"]}
        remaining_slots = [slot for slot in self.scheduler.scan_times(session) if slot.isoformat() not in completed_slots]
        stale_initial = [slot for slot in remaining_slots if self.scheduler.is_stale(slot, current)]
        for slot in stale_initial:
            operation_id = f"stale-slot:{slot.isoformat()}"
            if operation_id not in state["operation_ids"]:
                state["schedule_events"].append({"operation_id": operation_id, "status": "SKIPPED_STALE", "scheduled_for": slot.isoformat(), "observed_at": current.isoformat()})
                state["operation_ids"].append(operation_id)
                audit("STALE_SLOT_MARKED", scheduled_for=slot.isoformat())
        if stale_initial:
            self.store.save(state)
        scan_slots = {slot for slot in remaining_slots if not self.scheduler.is_stale(slot, current)}
        monitor_cursor = max(current.replace(second=0, microsecond=0), session.market_open + timedelta(minutes=10))
        monitor_cursor += timedelta(minutes=(-monitor_cursor.minute) % 5)
        monitor_slots = set()
        while monitor_cursor <= session.mandatory_flat:
            monitor_slots.add(monitor_cursor)
            monitor_cursor += timedelta(minutes=5)
        for event_time in sorted(scan_slots | monitor_slots):
            self._wait_until(event_time)
            try:
                if self._active_plan_count(state):
                    self.monitor_active_plans(state, self._trusted_now())
                elif event_time in scan_slots and event_time < session.latest_entry:
                    actual = self._trusted_now()
                    if self.scheduler.is_stale(event_time, actual):
                        operation_id = f"stale-slot:{event_time.isoformat()}"
                        if operation_id not in state["operation_ids"]:
                            state["schedule_events"].append({"operation_id": operation_id, "status": "SKIPPED_STALE", "scheduled_for": event_time.isoformat(), "observed_at": actual.isoformat()})
                            state["operation_ids"].append(operation_id)
                            audit("STALE_SLOT_MARKED", scheduled_for=event_time.isoformat())
                            self.store.save(state)
                    else:
                        self.run_luna_cycle(state, session, event_time)
                self.store.save(state)
            except TraderError as exc:
                self._record_failure(state, "SESSION_CYCLE", exc)
                self.store.save(state)
        self._wait_until(session.eod_time)
        if not state.get("eod_completed"):
            persisted_eod = find_operation(state, f"eod:{state['session_date']}")
            if persisted_eod and persisted_eod.get("state") == "RETRY_WAIT" and persisted_eod.get("next_retry_at"):
                retry_at = aware(persisted_eod["next_retry_at"])
                audit("EOD_RETRY", next_retry_at=retry_at.isoformat())
                self._wait_until(retry_at)
            timing_operation = f"eod-timing:{state['session_date']}"
            if timing_operation not in state["operation_ids"]:
                state["schedule_events"].append({"operation_id": timing_operation, "status": "EOD_STARTED", "scheduled_for": session.eod_time.isoformat(), "observed_at": self._trusted_now().isoformat()})
                state["operation_ids"].append(timing_operation)
                self.store.save(state)
            try:
                self.eod(state, session)
            except CodexRunError:
                persisted_eod = find_operation(state, f"eod:{state['session_date']}")
                if persisted_eod and persisted_eod.get("state") == "FAILED_TERMINAL":
                    audit("EOD_FAILED_TERMINAL")
                    self._finalize_eod_without_ai(state, session, "SKIPPED_AI_FAILED_TERMINAL")
                    return
                raise

    def run_eod_only(self, day: datetime | None = None) -> dict[str, Any]:
        current = (day or datetime.now(ET)).astimezone(ET)
        session = self.calendar.session_for(current.date())
        if session is None:
            raise PreflightError("No exchange session exists for this date")
        state = self.store.load(session.session_date, create=False)
        self.preflight(state, current)
        return self.eod(state, session)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(ET)).astimezone(ET)
        path = self.store.path_for(current.date())
        state = self.store.load(current.date(), create=False) if path.exists() else None
        readiness = calculate_readiness(self.store.all_states(), self.config)
        return {
            "mode": "SHADOW",
            "session_date": current.date().isoformat(),
            "state_exists": state is not None,
            "last_cycle": state["cycles"][-1] if state and state["cycles"] else None,
            "cooldowns": state["cooldowns"] if state else {},
            "shadow_plans": state["shadow_plans"] if state else [],
            "research_outcomes": state.get("research_outcomes", []) if state else [],
            "usage_counts": state["usage_counts"] if state else {},
            "readiness": readiness,
            "codex": self.runner.safe_diagnostics(),
            "boundary_policy": self.boundary.policy_version,
        }

    def _validate_luna(self, cycle: dict[str, Any], state: dict[str, Any], session, web_searches: int, *, expected_cycle_id: str | None = None, observed_tool_calls: dict[str, int] | None = None, observed_start: datetime | None = None, observed_end: datetime | None = None) -> None:
        if web_searches:
            raise SchemaValidationError("Luna used prohibited web search")
        if cycle["errors"]:
            raise CodexRunError("Luna returned data/tool errors")
        security = cycle["security_status"]
        account = cycle["account_status"]
        forbidden = {normalize_tool_name(name) for name in security["forbidden_tools_available"]} & FORBIDDEN_ROBINHOOD_TOOLS
        if not security["robinhood_mcp_available"] or not security["boundary_ok"] or forbidden:
            raise PreflightError("Luna security boundary failed")
        if account["agentic_account_count"] != 1 or not account["reconciled"]:
            raise PreflightError("Luna account reconciliation failed")
        if expected_cycle_id is not None and cycle["cycle_id"] != expected_cycle_id:
            raise SchemaValidationError("Luna returned an unexpected cycle_id")
        if cycle.get("session_date") != session.session_date or cycle.get("scanner") != {"name": self.config["scanner"]["name"], "id": self.config["scanner"]["id"]}:
            raise SchemaValidationError("Luna returned unexpected session/scanner identity")
        self._validate_model_timestamp(cycle["timestamp"], observed_start, observed_end, "Luna")
        if aware(cycle["timestamp"]).astimezone(ET).date().isoformat() != session.session_date:
            raise SchemaValidationError("Luna timestamp is outside the expected session date")
        if observed_tool_calls is not None:
            required_once = {"get_accounts", "get_portfolio", "get_equity_orders", "get_equity_positions", "run_scan"}
            bad_counts = sorted(name for name in required_once if observed_tool_calls.get(name, 0) != 1)
            if bad_counts:
                raise SchemaValidationError("Luna reconciliation and scan calls must each complete exactly once: " + ", ".join(bad_counts))
            observed_total = sum(observed_tool_calls.values())
            if cycle["tool_call_count"]["total"] != observed_total or cycle["tool_call_count"]["run_scan"] != observed_tool_calls.get("run_scan"):
                raise SchemaValidationError("Luna reported tool-call counts do not match observed events")
        symbols = cycle["symbols_processed"]
        finalist_symbols = [item["symbol"] for item in cycle["finalists"]]
        if len(finalist_symbols) != len(set(finalist_symbols)):
            raise SchemaValidationError("Luna returned duplicate finalists")
        if not set(finalist_symbols) <= set(symbols):
            raise SchemaValidationError("Luna invented a finalist outside symbols_processed")
        if not isinstance(cycle["scanner_total"], int) or cycle["scanner_total"] < len(symbols) or cycle["scanner_total"] > 1_000_000:
            raise SchemaValidationError("Luna scanner_total is inconsistent or insane")
        if len(cycle["symbols_processed"]) > int(self.config["scanner"]["maximum_results"]):
            raise SchemaValidationError("Luna exceeded the first-20 processing limit")
        prior = state["cooldowns"]
        qualifying = False
        for finalist in cycle["finalists"]:
            symbol = finalist["symbol"]
            classification = finalist["classification"]
            if symbol in prior and classification == "NEW":
                raise SchemaValidationError(f"Previously rejected {symbol} cannot be classified NEW")
            if symbol not in prior and classification != "NEW":
                raise SchemaValidationError(f"Never-rejected {symbol} must be classified NEW")
            if classification == "MATERIALLY_REQUALIFIED" and not finalist["material_requalification"]:
                raise SchemaValidationError(f"{symbol} lacks a material-requalification event")
            if classification != "MATERIALLY_REQUALIFIED" and finalist["material_requalification"] is not None:
                raise SchemaValidationError(f"{symbol} has inconsistent material-requalification evidence")
            if classification == "MATERIALLY_REQUALIFIED":
                evidence = finalist["material_requalification"]
                allowed_material_events = {"COMPLETED_BASE_BREAKOUT", "PULLBACK_RECLAIM", "REDUCED_EXTENSION_RENEWED_STRUCTURE", "MATERIALLY_DIFFERENT_PRICE_VOLUME", "NEW_CATALYST"}
                if evidence.get("event_type") not in allowed_material_events or not str(evidence.get("evidence", "")).strip():
                    raise SchemaValidationError("Material requalification has no objective supported event")
                if evidence.get("new_high_only") is not False:
                    raise SchemaValidationError("A new session high alone is not material requalification")
                evidence_time = aware(evidence["evidence_timestamp"])
                if symbol not in prior or evidence_time <= aware(prior[symbol]["rejected_at"]):
                    raise SchemaValidationError("Material-requalification evidence must postdate the prior rejection")
                if evidence_time > aware(cycle["timestamp"]):
                    raise SchemaValidationError("Material-requalification evidence cannot be from the future")
            previous_bar_time = None
            cycle_time = aware(cycle["timestamp"]).astimezone(ET)
            for bar in finalist["completed_15m_structure"]:
                bar_time = aware(bar["timestamp"]).astimezone(ET)
                offset_seconds = (bar_time - session.market_open).total_seconds()
                if offset_seconds < 0 or offset_seconds % (15 * 60) != 0 or bar_time + timedelta(minutes=15) > min(cycle_time, session.market_close):
                    raise SchemaValidationError("Luna 15-minute structure contains a misaligned, forming, or out-of-session bar")
                if previous_bar_time is not None and bar_time <= previous_bar_time:
                    raise SchemaValidationError("Luna 15-minute structure is not strictly chronological")
                previous_bar_time = bar_time
            qualifying |= classification in {"NEW", "MATERIALLY_REQUALIFIED"}
        if observed_tool_calls is not None and cycle["finalists"]:
            evidence_minimums = {
                "get_equity_quotes": 1,
                "get_equity_tradability": 1,
                "get_equity_historicals": 1,
                "get_equity_technical_indicators": 3 * len(cycle["finalists"]),
            }
            missing_evidence = sorted(name for name, minimum in evidence_minimums.items() if observed_tool_calls.get(name, 0) < minimum)
            if missing_evidence:
                raise SchemaValidationError("Luna finalist lacks observed market-data evidence: " + ", ".join(missing_evidence))
        if bool(cycle["sol_escalation"]) != qualifying:
            raise SchemaValidationError("Luna sol_escalation does not match finalist classifications")
        if aware(cycle["timestamp"]).astimezone(ET) >= session.latest_entry and cycle["sol_escalation"]:
            raise SchemaValidationError("Luna escalated after the latest-entry cutoff")

    def _validate_senior(self, decision: dict[str, Any], finalists: list[dict[str, Any]], state: dict[str, Any], session, web_searches: int, *, observed_tool_calls: dict[str, int] | None = None, observed_start: datetime | None = None, observed_end: datetime | None = None, allow_research_concurrency: bool = False) -> None:
        if decision["errors"]:
            raise CodexRunError("Sol returned data, MCP, OAuth, or required-research errors")
        if web_searches <= 0:
            raise CodexRunError("Sol did not perform required targeted live catalyst research")
        self._validate_model_timestamp(decision["decision_timestamp"], observed_start, observed_end, "Sol")
        if aware(decision["decision_timestamp"]).astimezone(ET).date().isoformat() != session.session_date:
            raise SchemaValidationError("Sol decision timestamp is outside the expected session date")
        expected = {item["symbol"] for item in finalists}
        evaluated = set(decision["evaluated_symbols"])
        if evaluated != expected:
            raise SchemaValidationError("Sol must evaluate every qualifying finalist exactly once")
        if observed_tool_calls:
            if decision["web_search_count"] != web_searches:
                raise SchemaValidationError("Sol reported web-search count does not match observed events")
            if decision["robinhood_tool_call_count"] != sum(observed_tool_calls.values()):
                raise SchemaValidationError("Sol reported Robinhood tool-call count does not match observed events")
            if observed_tool_calls.get("get_equity_quotes", 0) < 1 or observed_tool_calls.get("get_equity_historicals", 0) < 1:
                raise SchemaValidationError("Sol lacks observed quote or historical market evidence")
        rejected = {item["symbol"] for item in decision["rejections"]}
        if decision["decision"] == "NO_TRADE" and rejected != evaluated:
            raise SchemaValidationError("NO_TRADE must give a durable rejection for every evaluated symbol")
        if decision["decision"] == "SHADOW_TRADE_PLAN":
            if decision["symbol"] not in evaluated or rejected != evaluated - {decision["symbol"]}:
                raise SchemaValidationError("Senior plan/rejection symbol sets are inconsistent")
            if self._active_plan_count(state) and not allow_research_concurrency:
                raise SchemaValidationError("Only one concurrent Shadow plan is permitted")
            if any(plan["outcome"].get("entry_triggered") for plan in state["shadow_plans"] if plan.get("research_role", "PRIMARY") == "PRIMARY"):
                raise SchemaValidationError("V1 permits no more than one entered Shadow trade per session")
            risk = self.config["risk"]
            entry, chase, stop, target = (float(decision[key]) for key in ("entry_trigger", "maximum_chase_price", "stop_price", "target1"))
            quantity = float(decision["hypothetical_quantity"])
            numeric = [entry, chase, stop, target, quantity, float(decision["hypothetical_notional"]), float(decision["planned_dollar_risk"]), float(decision["planned_account_risk_percent"]), float(decision["reward_risk_target1"])]
            numeric.append(float(decision["current_price"]))
            if decision["target2_optional"] is not None:
                numeric.append(float(decision["target2_optional"]))
            if not all(math.isfinite(item) and item > 0 for item in numeric):
                raise SchemaValidationError("Senior plan contains non-finite or non-positive values")
            if not (0 < stop < entry <= chase < target):
                raise SchemaValidationError("Long plan prices are not structurally ordered")
            if float(decision["current_price"]) > chase:
                raise SchemaValidationError("Senior plan is already beyond its maximum chase price")
            if decision["target2_optional"] is not None and float(decision["target2_optional"]) <= target:
                raise SchemaValidationError("Target 2 must exceed Target 1")
            computed_risk = (chase - stop) * quantity
            computed_notional = chase * quantity
            computed_account_percent = computed_risk / float(risk["reference_capital"]) * 100
            computed_rr = (target - chase) / (chase - stop)
            if computed_risk > float(risk["maximum_planned_loss"]) + 1e-6 or abs(computed_risk - float(decision["planned_dollar_risk"])) > 0.01:
                raise SchemaValidationError("Senior planned risk is inconsistent with maximum chase, stop, and quantity")
            if computed_notional > float(risk["maximum_hypothetical_notional"]) + 0.01 or abs(computed_notional - float(decision["hypothetical_notional"])) > 0.01:
                raise SchemaValidationError("Senior hypothetical notional exceeds $35")
            if abs(computed_account_percent - float(decision["planned_account_risk_percent"])) > 0.01 or abs(computed_rr - float(decision["reward_risk_target1"])) > 0.01:
                raise SchemaValidationError("Senior account-risk or reward/risk calculation is inconsistent")
            if computed_account_percent > float(risk["maximum_account_risk_percent"]) + 1e-6 or computed_rr + 1e-6 < float(risk["minimum_reward_risk"]):
                raise SchemaValidationError("Senior plan violates deterministic account-risk or reward/risk limits")
            decision_time = aware(decision["decision_timestamp"]).astimezone(ET)
            latest = aware(decision["latest_entry_time"]).astimezone(ET)
            flat = aware(decision["mandatory_flat_time"]).astimezone(ET)
            time_exit = aware(decision["time_exit"]).astimezone(ET)
            if decision_time.date().isoformat() != session.session_date or latest.date() != decision_time.date() or flat.date() != decision_time.date() or time_exit.date() != decision_time.date():
                raise SchemaValidationError("Senior plan timestamps do not belong to the session")
            if decision_time >= latest or latest != session.latest_entry or flat != session.mandatory_flat or time_exit > flat:
                raise SchemaValidationError("Senior plan violates session time gates")
            if observed_end is not None and observed_end.astimezone(ET) >= session.latest_entry:
                raise PreflightError("Sol completed at or after latest-entry cutoff; plan rejected")
            if not decision.get("quote_timestamp"):
                raise SchemaValidationError("Senior plan requires a fresh quote timestamp")
            quote_time = aware(decision["quote_timestamp"]).astimezone(ET)
            if quote_time > decision_time or (decision_time - quote_time).total_seconds() > 60:
                raise SchemaValidationError("Senior quote timestamp is stale or from the future")

    @staticmethod
    def _validate_model_timestamp(value: str, observed_start: datetime | None, observed_end: datetime | None, label: str) -> None:
        if observed_start is None or observed_end is None:
            return
        timestamp = aware(value)
        tolerance = timedelta(seconds=5)
        if timestamp < observed_start - tolerance or timestamp > observed_end + tolerance:
            raise SchemaValidationError(f"{label} timestamp is implausible relative to subprocess timing")

    def _persist_rejections(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        minutes = int(self.config["cooldown"]["senior_rejection_minutes"])
        for rejection in decision["rejections"]:
            existing = state["cooldowns"].get(rejection["symbol"])
            history = deepcopy(existing.get("rejection_history", [])) if existing else []
            if existing:
                history.append({key: deepcopy(existing[key]) for key in ("rejected_at", "cooldown_until", "original_rejection_reason", "rejection_categories", "source_decision_number")})
            state["cooldowns"][rejection["symbol"]] = {
                "symbol": rejection["symbol"],
                "rejected_at": decision["decision_timestamp"],
                "cooldown_until": cooldown_until(decision["decision_timestamp"], minutes),
                "original_rejection_reason": rejection["reason"],
                "rejection_categories": rejection["rejection_categories"],
                "source_decision_number": len(state["senior_decisions"]),
                "active": True,
                "rejection_history": history,
            }

    def _cooldown_context(self, state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        result = []
        for value in state["cooldowns"].values():
            item = deepcopy(value)
            item["cooldown_active_now"] = now < aware(item["cooldown_until"])
            result.append(item)
        return result

    def _record_completed_trades(self, state: dict[str, Any]) -> None:
        existing = {item["plan_id"] for item in state["completed_shadow_trades"]}
        research = state.setdefault("research_outcomes", [])
        existing_research = {(item["plan_id"], item["variant"]) for item in research}
        for record in state["shadow_plans"]:
            outcome = record["outcome"]
            plan = record["original_plan"]
            if record.get("research_role", "PRIMARY") == "PRIMARY" and record["plan_id"] not in existing and outcome["status"] in {"TARGET1", "STOPPED", "FLAT_TIME"}:
                entry_time = aware(outcome["entry_timestamp"]).astimezone(ET)
                state["completed_shadow_trades"].append({
                    "attribution": "SHADOW_AI", "plan_id": record["plan_id"], "symbol": plan["symbol"], "setup_type": plan["setup_type"], "market_regime": plan["market_regime"],
                    "catalyst_classification": plan["catalyst_classification"], "time_of_day": entry_time.strftime("%H:%M"),
                    "hypothetical_notional": plan["hypothetical_notional"], "planned_dollar_risk": plan["planned_dollar_risk"],
                    "entry_timestamp": outcome["entry_timestamp"], "entry_price": outcome["entry_price"], "exit_timestamp": outcome["exit_timestamp"],
                    "exit_price": outcome["exit_price"], "exit_reason": outcome["exit_reason"], "pnl": outcome["pnl"], "realized_r": outcome["realized_r"],
                    "mfe": outcome["mfe"], "mae": outcome["mae"], "target2_hit": outcome["target2_hit"],
                })
            for variant, variant_outcome in (("FIXED_TARGET", outcome), ("TRAILING_STOP", record.get("trailing_outcome"))):
                if not variant_outcome or (record["plan_id"], variant) in existing_research or variant_outcome.get("status") not in {"TARGET1", "STOPPED", "FLAT_TIME", "EXPIRED", "AMBIGUOUS"}:
                    continue
                research.append({
                    "plan_id": record["plan_id"], "variant": variant, "research_role": record.get("research_role", "PRIMARY"),
                    "research_rank": record.get("research_rank", 1), "symbol": plan["symbol"], "setup_type": plan["setup_type"],
                    "market_regime": plan["market_regime"], "hypothetical_notional": plan["hypothetical_notional"],
                    "entry_triggered": bool(variant_outcome.get("entry_triggered")), "entry_timestamp": variant_outcome.get("entry_timestamp"),
                    "exit_timestamp": variant_outcome.get("exit_timestamp"), "exit_reason": variant_outcome.get("exit_reason"),
                    "pnl": variant_outcome.get("pnl"), "realized_r": variant_outcome.get("realized_r"),
                    "mfe": variant_outcome.get("mfe"), "mae": variant_outcome.get("mae"),
                    "trailing_active": bool(variant_outcome.get("trailing_active", False)), "trailing_updates": int(variant_outcome.get("trailing_updates", 0)),
                })
                existing_research.add((record["plan_id"], variant))

    def _record_failure(self, state: dict[str, Any], category: str, error: Exception) -> None:
        state["errors"].append({"timestamp": datetime.now(ET).isoformat(), "category": category, "message": sanitize_diagnostic_text(str(error)), "resolved": False})
        state["usage_counts"]["failed_runs"] += 1

    def _regenerate_cycle_reports(self, state: dict[str, Any]) -> None:
        for number, cycle in enumerate(state["cycles"], 1):
            write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-cycle-{number}.md", cycle_markdown(cycle))
            write_json_companion(self.root / "reports" / f"{state['session_date']}-cycle-{number}.json", cycle)
        for number, decision in enumerate(state["senior_decisions"], 1):
            write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-senior-{number}.md", senior_markdown(decision))
            write_json_companion(self.root / "reports" / f"{state['session_date']}-senior-{number}.json", decision)

    def _write_experiment_report(self, session_date: str, readiness: dict[str, Any]) -> None:
        experiment = readiness["metrics"]["accelerated_experiment"]
        payload = {"session_date": session_date, "mode": "SHADOW", "experiment": experiment, "readiness": readiness["status"], "live_permissions_changed": False}
        write_json_companion(self.root / "reports" / f"{session_date}-experiment.json", payload)
        if experiment["attention_required"]:
            write_json_companion(self.root / "reports" / f"{session_date}-major-decision-required.json", payload)

    def _add_usage(self, state: dict[str, Any], result) -> None:
        state["usage_counts"]["robinhood_tool_calls"] += sum(result.tool_calls.values())
        tokens = state["usage_counts"]["tokens"]
        for key, value in result.usage.items():
            if isinstance(value, (int, float)) and "token" in key:
                tokens[key] = tokens.get(key, 0) + value

    @staticmethod
    def _active_plan_count(state: dict[str, Any]) -> int:
        return sum(1 for plan in state["shadow_plans"] if ShadowOrchestrator._plan_is_active(plan))

    @staticmethod
    def _plan_is_active(plan: dict[str, Any]) -> bool:
        return plan["outcome"]["status"] in {"PENDING", "OPEN"} or plan.get("trailing_outcome", {}).get("status") in {"PENDING", "OPEN"}

    def _ensure_strategy_version(self, state: dict[str, Any]) -> None:
        configured = self.config.get("strategy_version", STRATEGY_VERSION)
        existing = state.get("strategy_version")
        if existing == configured:
            return
        has_strategy_evidence = bool(state["cycles"] or state["senior_decisions"] or state["shadow_plans"] or state["completed_shadow_trades"] or state["eod_completed"])
        if not has_strategy_evidence:
            state["strategy_version"] = configured
            return
        raise StateCorruptionError("State strategy version is missing or does not match the configured cohort")

    @staticmethod
    def _session_context(session) -> dict[str, Any]:
        return {"session_date": session.session_date, "market_open": session.market_open.isoformat(), "market_close": session.market_close.isoformat(), "latest_entry_time": session.latest_entry.isoformat(), "mandatory_flat_time": session.mandatory_flat.isoformat(), "eod_time": session.eod_time.isoformat(), "early_close": session.early_close}

    def _wait_until(self, target: datetime) -> None:
        while True:
            stop_event = getattr(self, "stop_event", None)
            if stop_event is not None and stop_event.is_set():
                raise SystemExit(0)
            remaining = (target - self._trusted_now()).total_seconds()
            if remaining <= 0:
                return
            callback = getattr(self, "heartbeat_callback", None)
            if callback is not None:
                callback(target)
            if stop_event is not None:
                stop_event.wait(min(30, remaining))
            else:
                time_module.sleep(min(30, remaining))


def run_self_test() -> bool:
    config = load_config(CONFIG_PATH)
    offline_preflight(ROOT, config)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Deterministic unattended Shadow trading research orchestrator")
    group = result.add_mutually_exclusive_group(required=True)
    group.add_argument("--self-test", action="store_true")
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--once", action="store_true")
    group.add_argument("--run-session", action="store_true")
    group.add_argument("--eod", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--daemon", action="store_true")
    group.add_argument("--health-check", action="store_true")
    group.add_argument("--validate-unattended-config", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parser().parse_args(argv)
    if args.self_test:
        return 0 if run_self_test() else 1
    if args.health_check:
        problems = health_check(ROOT)
        if problems:
            print(json.dumps({"status": "UNHEALTHY", "problems": problems}, indent=2), file=sys.stderr)
            return 3
        print(json.dumps({"status": "HEALTHY", "mode": "SHADOW"}, indent=2))
        return 0
    if args.validate_unattended_config:
        result = validate_unattended_config(ROOT)
        print(json.dumps(result, indent=2), file=sys.stdout if result["status"] == "PASS" else sys.stderr)
        return 0 if result["status"] == "PASS" else 2
    try:
        started_at = datetime.now(ET)
        lock = SingleInstanceLock(ROOT / ".runtime" / "orchestrator.lock", started_at.date().isoformat(), started_at)
        with lock:
            orchestrator = ShadowOrchestrator()
            if args.daemon:
                validation = validate_unattended_config(ROOT)
                if validation["status"] != "PASS":
                    raise PreflightError("Unattended configuration invalid: " + "; ".join(validation["problems"]))
                DaemonSupervisor(orchestrator, heartbeat=Heartbeat(ROOT / "state" / "heartbeat.json", ROOT)).run_forever()
            elif args.status:
                print(json.dumps(orchestrator.status(), indent=2, default=str))
            elif args.preflight:
                now = orchestrator._trusted_now()
                session_date = now.date().isoformat()
                state = orchestrator.store.load(session_date)
                result = orchestrator.preflight(state, now)
                print(json.dumps({"status": "PASS", "timestamp": result.get("timestamp", now.isoformat()), "mode": "SHADOW"}, indent=2))
            elif args.once:
                print(json.dumps(orchestrator.run_once(), indent=2, default=str))
            elif args.run_session:
                orchestrator.run_session()
            elif args.eod:
                print(json.dumps(orchestrator.run_eod_only(), indent=2, default=str))
        return 0
    except (TraderError, NotImplementedError, OSError, ValueError) as exc:
        try:
            write_alert(ROOT / "logs", "fatal", sanitize_diagnostic_text(str(exc)))
        except OSError:
            pass
        print(f"FAIL CLOSED: {sanitize_diagnostic_text(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
