from __future__ import annotations

import argparse
import json
import sys
import time as time_module
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader.codex_runner import CodexRunner
from trader.market_calendar import ET, EquityMarketCalendar
from trader.models import CodexRunError, PreflightError, SchemaValidationError, ShadowPlanStatus, StateCorruptionError, TraderError
from trader.readiness import calculate_readiness
from trader.reporting import cycle_markdown, eod_markdown, senior_markdown, write_json_companion, write_non_destructive_text
from trader.safety import FORBIDDEN_ROBINHOOD_TOOLS, cooldown_until, enforce_preflight_result, load_config, normalize_tool_name, offline_preflight, validate_json, write_alert
from trader.scheduler import SessionScheduler
from trader.shadow_monitor import ShadowPlanMonitor
from trader.state import StateStore


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "strategy.yaml"


def aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SchemaValidationError(f"Expected timezone-aware timestamp: {value}")
    return parsed


class ShadowOrchestrator:
    def __init__(self, root: Path = ROOT, runner: CodexRunner | None = None, calendar: EquityMarketCalendar | None = None) -> None:
        self.root = root.resolve()
        self.config = load_config(self.root / "config" / "strategy.yaml")
        offline_preflight(self.root, self.config)
        self.store = StateStore(self.root / "state", self.config["timezone"])
        self.runner = runner or CodexRunner(self.root, self.config)
        self.calendar = calendar or EquityMarketCalendar(self.config["exchange_calendar"], int(self.config["schedule"]["eod_offset_minutes"]))
        self.scheduler = SessionScheduler(self.config["schedule"])
        self.monitor = ShadowPlanMonitor()

    def preflight(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        result = None
        state["usage_counts"]["preflight_runs"] += 1
        try:
            if not self.runner.mcp_server_configured():
                raise PreflightError("No enabled Robinhood MCP server appears in the Codex MCP list")
            context = {"mode": "SHADOW", "requested_timestamp": now.astimezone(ET).isoformat(), "forbidden_tools": sorted(FORBIDDEN_ROBINHOOD_TOOLS)}
            result = self.runner.run(
                prompt_path=self.root / "prompts" / "preflight.md",
                schema_path=self.root / "schemas" / "preflight.schema.json",
                model=self.config["models"]["luna"],
                context=context,
                allow_web=False,
            )
            if result.web_searches:
                raise PreflightError("Preflight unexpectedly used web search")
            enforce_preflight_result(result.data)
        except TraderError as exc:
            if result is not None:
                self._add_usage(state, result, result.data.get("tool_call_count", 0))
            state["security_events"].append({"timestamp": now.astimezone(ET).isoformat(), "category": "PREFLIGHT_FAILURE", "message": str(exc)[:600]})
            self._record_failure(state, "PREFLIGHT", exc)
            self.store.save(state)
            report = {"result": result.data if result is not None else None, "cli_usage": result.usage if result is not None else {}, "mode": "SHADOW", "passed": False, "error": str(exc)[:600]}
            write_json_companion(self.root / "reports" / f"{state['session_date']}-preflight.json", report)
            raise
        self._add_usage(state, result, result.data.get("tool_call_count", 0))
        report = {"result": result.data, "cli_usage": result.usage, "mode": "SHADOW", "passed": True}
        write_json_companion(self.root / "reports" / f"{state['session_date']}-preflight.json", report)
        return result.data

    def run_luna_cycle(self, state: dict[str, Any], session, scheduled_for: datetime) -> dict[str, Any]:
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
        result = self.runner.run(
            prompt_path=self.root / "prompts" / "luna-stage-b.md",
            schema_path=self.root / "schemas" / "luna-cycle.schema.json",
            model=self.config["models"]["luna"],
            context=context,
            allow_web=False,
        )
        cycle = result.data
        self._validate_luna(cycle, state, session, result.web_searches)
        cycle["scheduled_for"] = scheduled_for.isoformat()
        cycle["cli_usage"] = result.usage
        cycle["cli_tool_calls"] = result.tool_calls
        state["cycles"].append(cycle)
        state["usage_counts"]["luna_runs"] += 1
        self._add_usage(state, result, cycle["tool_call_count"].get("total", 0))
        write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-cycle-{cycle_number}.md", cycle_markdown(cycle))
        write_json_companion(self.root / "reports" / f"{state['session_date']}-cycle-{cycle_number}.json", cycle)
        if cycle["sol_escalation"]:
            self.run_senior(state, session, cycle)
        return cycle

    def run_senior(self, state: dict[str, Any], session, cycle: dict[str, Any]) -> dict[str, Any]:
        finalists = [item for item in cycle["finalists"] if item["classification"] in {"NEW", "MATERIALLY_REQUALIFIED"}]
        if not finalists:
            raise SchemaValidationError("Sol escalation requested without a qualifying finalist")
        if self._active_plan_count(state):
            raise SchemaValidationError("A new senior plan cannot be evaluated while a Shadow plan is active")
        context = {
            "cycle_id": cycle["cycle_id"],
            "finalists": finalists,
            "session": self._session_context(session),
            "risk": self.config["risk"],
            "immutable_prior_rejections": self._cooldown_context(state, aware(cycle["timestamp"])),
        }
        result = self.runner.run(
            prompt_path=self.root / "prompts" / "sol-senior.md",
            schema_path=self.root / "schemas" / "senior-decision.schema.json",
            model=self.config["models"]["sol"],
            reasoning_effort=self.config["models"]["sol_reasoning_effort"],
            context=context,
            allow_web=True,
        )
        decision = result.data
        self._validate_senior(decision, finalists, state, session, max(result.web_searches, int(decision["web_search_count"])))
        decision["source_cycle_id"] = cycle["cycle_id"]
        decision["cli_usage"] = result.usage
        decision["cli_tool_calls"] = result.tool_calls
        state["senior_decisions"].append(decision)
        state["usage_counts"]["sol_runs"] += 1
        self._add_usage(state, result, decision["robinhood_tool_call_count"])
        state["usage_counts"]["web_searches"] += max(result.web_searches, int(decision["web_search_count"]))
        self._persist_rejections(state, decision)
        if decision["decision"] == "SHADOW_TRADE_PLAN":
            plan_id = f"{state['session_date']}-plan-{len(state['shadow_plans']) + 1}"
            frozen = {
                "plan_id": plan_id,
                "frozen_at": decision["decision_timestamp"],
                "original_plan": deepcopy({key: value for key, value in decision.items() if key not in {"cli_usage", "cli_tool_calls", "source_cycle_id"}}),
                "outcome": self.monitor.initial_outcome(),
            }
            state["shadow_plans"].append(frozen)
        number = len(state["senior_decisions"])
        write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-senior-{number}.md", senior_markdown(decision))
        write_json_companion(self.root / "reports" / f"{state['session_date']}-senior-{number}.json", decision)
        return decision

    def monitor_active_plans(self, state: dict[str, Any], now: datetime) -> None:
        active = [plan for plan in state["shadow_plans"] if plan["outcome"]["status"] in {"PENDING", "OPEN"}]
        if not active:
            state["shadow_positions"] = []
            return
        context = {
            "timestamp": now.astimezone(ET).isoformat(),
            "plans": [{"plan_id": item["plan_id"], "symbol": item["original_plan"]["symbol"], "start_time": item["original_plan"]["decision_timestamp"]} for item in active],
        }
        result = self.runner.run(
            prompt_path=self.root / "prompts" / "shadow-monitor.md",
            schema_path=self.root / "schemas" / "shadow-monitor.schema.json",
            model=self.config["models"]["luna"],
            context=context,
            allow_web=False,
        )
        if result.web_searches or result.data["errors"]:
            raise CodexRunError("Shadow monitor returned a prohibited web call or read error")
        updated_by_id: dict[str, dict[str, Any]] = {}
        for plan in active:
            symbol = plan["original_plan"]["symbol"]
            if symbol not in result.data["symbol_bars"]:
                raise CodexRunError(f"Shadow monitor omitted bars for {symbol}")
            updated_by_id[plan["plan_id"]] = self.monitor.evaluate(plan, result.data["symbol_bars"][symbol], now)
        state["shadow_plans"] = [updated_by_id.get(item["plan_id"], item) for item in state["shadow_plans"]]
        state["shadow_positions"] = [
            {"plan_id": item["plan_id"], "symbol": item["original_plan"]["symbol"], "entry_price": item["outcome"]["entry_price"], "entry_timestamp": item["outcome"]["entry_timestamp"]}
            for item in state["shadow_plans"] if item["outcome"]["status"] == "OPEN"
        ]
        self._record_completed_trades(state)
        state["usage_counts"]["monitor_runs"] += 1
        self._add_usage(state, result, result.data["tool_call_count"])

    def eod(self, state: dict[str, Any], session) -> dict[str, Any]:
        context = {
            "session": self._session_context(session),
            "senior_decisions": [{key: value for key, value in item.items() if key not in {"cli_usage", "cli_tool_calls"}} for item in state["senior_decisions"]],
            "shadow_plans": state["shadow_plans"],
        }
        result = self.runner.run(
            prompt_path=self.root / "prompts" / "eod-review.md",
            schema_path=self.root / "schemas" / "eod-review.schema.json",
            model=self.config["models"]["luna"],
            context=context,
            allow_web=False,
        )
        review = result.data
        if result.web_searches or review["errors"] or review["session_date"] != state["session_date"]:
            raise CodexRunError("EOD review failed data-integrity checks")
        expected_reviews = {
            (rejection["symbol"], decision["decision_timestamp"])
            for decision in state["senior_decisions"]
            for rejection in decision["rejections"]
        }
        actual_reviews = {(item["symbol"], item["decision_timestamp"]) for item in review["decision_reviews"]}
        if expected_reviews != actual_reviews:
            raise CodexRunError("EOD review did not cover every senior rejection exactly once")
        updated = []
        for plan in state["shadow_plans"]:
            symbol = plan["original_plan"]["symbol"]
            if symbol not in review["symbol_bars"]:
                raise CodexRunError(f"EOD review omitted required bars for {symbol}")
            bars = review["symbol_bars"][symbol]
            updated.append(self.monitor.evaluate(plan, bars, session.market_close))
        state["shadow_plans"] = updated
        state["shadow_positions"] = []
        self._record_completed_trades(state)
        state["usage_counts"]["eod_runs"] += 1
        self._add_usage(state, result, review["robinhood_tool_call_count"])
        state["eod_completed"] = True
        state["eod_review"] = review
        self.store.save(state)
        readiness = calculate_readiness(self.store.all_states(), self.config)
        state["readiness"] = readiness
        self.store.save(state)
        companion = {"session_date": state["session_date"], "agent_review": review, "shadow_plans": state["shadow_plans"], "completed_shadow_trades": state["completed_shadow_trades"], "readiness": readiness}
        write_json_companion(self.root / "reports" / f"{state['session_date']}-eod.json", companion)
        write_non_destructive_text(self.root / "reports" / f"{state['session_date']}-eod.md", eod_markdown(state["session_date"], review, readiness, state["completed_shadow_trades"]))
        return companion

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(ET)).astimezone(ET)
        session = self.calendar.session_for(current.date())
        if session is None:
            raise PreflightError("The U.S. equity market is closed today")
        if current < max(session.market_open + timedelta(minutes=10), session.market_open.replace(hour=9, minute=40)):
            raise PreflightError("Stage-B scans are not permitted before 09:40 ET")
        state = self.store.load(session.session_date)
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

    def run_session(self, now: datetime | None = None) -> None:
        current = (now or datetime.now(ET)).astimezone(ET)
        session = self.calendar.session_for(current.date())
        if session is None:
            print("Market closed: no Shadow session scheduled.")
            return
        state = self.store.load(session.session_date)
        self.preflight(state, current)
        self.store.save(state)
        completed_slots = {item.get("scheduled_for") for item in state["cycles"]}
        scan_slots = {slot for slot in self.scheduler.scan_times(session) if slot.isoformat() not in completed_slots and slot >= current}
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
                    self.monitor_active_plans(state, datetime.now(ET))
                elif event_time in scan_slots and event_time < session.latest_entry:
                    self.run_luna_cycle(state, session, event_time)
                self.store.save(state)
            except TraderError as exc:
                self._record_failure(state, "SESSION_CYCLE", exc)
                self.store.save(state)
        self._wait_until(session.eod_time)
        if not state.get("eod_completed"):
            self.eod(state, session)

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
            "usage_counts": state["usage_counts"] if state else {},
            "readiness": readiness,
        }

    def _validate_luna(self, cycle: dict[str, Any], state: dict[str, Any], session, web_searches: int) -> None:
        if web_searches:
            raise SchemaValidationError("Luna used prohibited web search")
        if cycle["errors"]:
            raise CodexRunError("Luna returned data/tool errors")
        security = cycle["security_status"]
        account = cycle["account_status"]
        forbidden = {normalize_tool_name(name) for name in security["forbidden_tools_available"]} & FORBIDDEN_ROBINHOOD_TOOLS
        if not security["robinhood_mcp_available"] or not security["boundary_ok"] or forbidden:
            raise PreflightError("Luna security boundary failed")
        if account["agentic_account_count"] != 1 or not account["reconciled"] or account["unexpected_positions"] or account["unexpected_orders"]:
            raise PreflightError("Luna account reconciliation failed")
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
            qualifying |= classification in {"NEW", "MATERIALLY_REQUALIFIED"}
        if bool(cycle["sol_escalation"]) != qualifying:
            raise SchemaValidationError("Luna sol_escalation does not match finalist classifications")
        if aware(cycle["timestamp"]).astimezone(ET) >= session.latest_entry and cycle["sol_escalation"]:
            raise SchemaValidationError("Luna escalated after the latest-entry cutoff")

    def _validate_senior(self, decision: dict[str, Any], finalists: list[dict[str, Any]], state: dict[str, Any], session, web_searches: int) -> None:
        if decision["errors"]:
            raise CodexRunError("Sol returned data, MCP, OAuth, or required-research errors")
        if web_searches <= 0:
            raise CodexRunError("Sol did not perform required targeted live catalyst research")
        expected = {item["symbol"] for item in finalists}
        evaluated = set(decision["evaluated_symbols"])
        if not evaluated or not evaluated <= expected:
            raise SchemaValidationError("Sol evaluated symbols outside the qualifying finalist set")
        rejected = {item["symbol"] for item in decision["rejections"]}
        if decision["decision"] == "NO_TRADE" and rejected != evaluated:
            raise SchemaValidationError("NO_TRADE must give a durable rejection for every evaluated symbol")
        if decision["decision"] == "SHADOW_TRADE_PLAN":
            if decision["symbol"] not in evaluated or rejected != evaluated - {decision["symbol"]}:
                raise SchemaValidationError("Senior plan/rejection symbol sets are inconsistent")
            if self._active_plan_count(state):
                raise SchemaValidationError("Only one concurrent Shadow plan is permitted")
            risk = self.config["risk"]
            entry, chase, stop, target = (float(decision[key]) for key in ("entry_trigger", "maximum_chase_price", "stop_price", "target1"))
            quantity = float(decision["hypothetical_quantity"])
            if not (0 < stop < entry <= chase < target):
                raise SchemaValidationError("Long plan prices are not structurally ordered")
            if decision["target2_optional"] is not None and float(decision["target2_optional"]) <= target:
                raise SchemaValidationError("Target 2 must exceed Target 1")
            computed_risk = (entry - stop) * quantity
            if computed_risk > float(risk["maximum_planned_loss"]) + 1e-6 or abs(computed_risk - float(decision["planned_dollar_risk"])) > 0.02:
                raise SchemaValidationError("Senior planned risk is inconsistent with entry, stop, and quantity")
            if entry * quantity > float(risk["maximum_hypothetical_notional"]) + 0.01 or float(decision["hypothetical_notional"]) > float(risk["maximum_hypothetical_notional"]):
                raise SchemaValidationError("Senior hypothetical notional exceeds $35")
            decision_time = aware(decision["decision_timestamp"]).astimezone(ET)
            latest = aware(decision["latest_entry_time"]).astimezone(ET)
            flat = aware(decision["mandatory_flat_time"]).astimezone(ET)
            if decision_time >= latest or latest > session.latest_entry or flat > session.mandatory_flat:
                raise SchemaValidationError("Senior plan violates session time gates")

    def _persist_rejections(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        minutes = int(self.config["cooldown"]["senior_rejection_minutes"])
        for rejection in decision["rejections"]:
            state["cooldowns"][rejection["symbol"]] = {
                "symbol": rejection["symbol"],
                "rejected_at": decision["decision_timestamp"],
                "cooldown_until": cooldown_until(decision["decision_timestamp"], minutes),
                "original_rejection_reason": rejection["reason"],
                "rejection_categories": rejection["rejection_categories"],
                "source_decision_number": len(state["senior_decisions"]),
                "active": True,
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
        for record in state["shadow_plans"]:
            outcome = record["outcome"]
            if record["plan_id"] in existing or outcome["status"] not in {"TARGET1", "STOPPED", "FLAT_TIME"}:
                continue
            plan = record["original_plan"]
            entry_time = aware(outcome["entry_timestamp"]).astimezone(ET)
            state["completed_shadow_trades"].append({
                "plan_id": record["plan_id"], "symbol": plan["symbol"], "setup_type": plan["setup_type"], "market_regime": plan["market_regime"],
                "catalyst_classification": plan["catalyst_classification"], "time_of_day": entry_time.strftime("%H:%M"),
                "hypothetical_notional": plan["hypothetical_notional"], "planned_dollar_risk": plan["planned_dollar_risk"],
                "entry_timestamp": outcome["entry_timestamp"], "entry_price": outcome["entry_price"], "exit_timestamp": outcome["exit_timestamp"],
                "exit_price": outcome["exit_price"], "exit_reason": outcome["exit_reason"], "pnl": outcome["pnl"], "realized_r": outcome["realized_r"],
                "mfe": outcome["mfe"], "mae": outcome["mae"], "target2_hit": outcome["target2_hit"],
            })

    def _record_failure(self, state: dict[str, Any], category: str, error: Exception) -> None:
        state["errors"].append({"timestamp": datetime.now(ET).isoformat(), "category": category, "message": str(error)[:600], "resolved": False})
        state["usage_counts"]["failed_runs"] += 1

    def _add_usage(self, state: dict[str, Any], result, reported_robinhood_calls: int) -> None:
        state["usage_counts"]["robinhood_tool_calls"] += max(int(reported_robinhood_calls), sum(result.tool_calls.values()))
        tokens = state["usage_counts"]["tokens"]
        for key, value in result.usage.items():
            if isinstance(value, (int, float)) and "token" in key:
                tokens[key] = tokens.get(key, 0) + value

    @staticmethod
    def _active_plan_count(state: dict[str, Any]) -> int:
        return sum(1 for plan in state["shadow_plans"] if plan["outcome"]["status"] in {"PENDING", "OPEN"})

    @staticmethod
    def _session_context(session) -> dict[str, Any]:
        return {"session_date": session.session_date, "market_open": session.market_open.isoformat(), "market_close": session.market_close.isoformat(), "latest_entry_time": session.latest_entry.isoformat(), "mandatory_flat_time": session.mandatory_flat.isoformat(), "eod_time": session.eod_time.isoformat(), "early_close": session.early_close}

    @staticmethod
    def _wait_until(target: datetime) -> None:
        while True:
            remaining = (target - datetime.now(ET)).total_seconds()
            if remaining <= 0:
                return
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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.self_test:
        return 0 if run_self_test() else 1
    try:
        orchestrator = ShadowOrchestrator()
        if args.status:
            print(json.dumps(orchestrator.status(), indent=2, default=str))
        elif args.preflight:
            now = datetime.now(ET)
            session_date = now.date().isoformat()
            state = orchestrator.store.load(session_date)
            result = orchestrator.preflight(state, now)
            orchestrator.store.save(state)
            print(json.dumps({"status": "PASS", "timestamp": result["timestamp"], "mode": "SHADOW"}, indent=2))
        elif args.once:
            print(json.dumps(orchestrator.run_once(), indent=2, default=str))
        elif args.run_session:
            orchestrator.run_session()
        elif args.eod:
            print(json.dumps(orchestrator.run_eod_only(), indent=2, default=str))
        return 0
    except (TraderError, NotImplementedError, OSError, ValueError) as exc:
        try:
            write_alert(ROOT / "logs", "fatal", str(exc))
        except OSError:
            pass
        print(f"FAIL CLOSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
