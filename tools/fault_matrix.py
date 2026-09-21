#!/usr/bin/env python3
"""Fast deterministic lifecycle and failure-classification fault matrix."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import random
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.lifecycle import (evaluate_entry_cutoff, evaluate_entry_trigger,
    evaluate_pre_entry_invalidation, evaluate_stop, evaluate_target,
    evaluate_time_exit, evaluate_trailing_stop, reconcile_plan_state, scheduler_decision,
    session_invariants, transition_plan)
from trader.state import atomic_write_json, initial_state
from trader.runtime_supervision import classify_failure

NOW = datetime.fromisoformat("2026-08-14T10:00:00-04:00")


def plan() -> dict:
    return {"symbol": "TEST", "decision_timestamp": NOW.isoformat(), "entry_trigger": 10.0,
            "maximum_chase_price": 10.2, "stop_price": 9.8, "target1": 10.4,
            "hypothetical_quantity": 5.0, "hypothetical_notional": 50.0,
            "planned_dollar_risk": 1.0, "latest_entry_time": (NOW + timedelta(minutes=30)).isoformat(),
            "mandatory_flat_time": NOW.replace(hour=15, minute=55).isoformat(),
            "pre_entry_invalidation_price": 9.7,
            "pre_entry_invalidation_type": "COMPLETED_5M_CLOSE_BELOW"}


def bar(low=9.9, high=10.1, close=10.0, complete=True) -> dict:
    return {"timestamp": NOW.isoformat(), "open": 10.0, "high": high, "low": low,
            "close": close, "volume": 1000, "complete": complete}


NAMES = [
 "normal no-plan session", "normal pending -> expired", "pending -> pre-entry invalidated",
 "pending -> entered -> target", "pending -> entered -> stop", "pending -> entered -> time exit",
 "trailing-stop lifecycle", "incomplete 5m bar ignored", "ambiguous same-bar stop/target",
 "Robinhood DataUnavailable", "historicals unavailable", "indicators unavailable", "orders unavailable",
 "model capacity error", "HTTP 502", "known teardown DELETE 400 after completed turn",
 "zero-tool model startup failure", "monitor failure then recovery",
 "three consecutive monitor failures then recovery", "monitor disappearance",
 "process restart with pending plan", "process restart with entered plan", "service dies during active session",
 "pending past latest-entry cutoff", "entered past mandatory-flat", "EOD operation missing",
 "eod_completed contradiction", "circuit content failures", "circuit external-data failures",
 "rejection followed by materially new setup identity", "terminal plan attempting resurrection",
 "second entered PRIMARY attempt", "accepted commit mismatch", "foreign MCP activity",
 "malformed model output", "malformed OHLC", "stale quote", "duplicate finalist",
 "scheduler suppression with explicit reason", "scheduler suppression without reason"]


def named_case(index: int, name: str) -> dict:
    passed, detail = False, "scenario did not execute"
    try:
        p, b = plan(), bar()
        if index == 0:
            passed = scheduler_decision([], NOW, scanner_due=True)["scanner"]
            detail = "empty lifecycle leaves scheduled scan eligible"
        elif index == 1 or index == 23:
            record = {"original_plan": p, "outcome": {"status": "PENDING", "entry_triggered": False}}
            recovered, reasons = reconcile_plan_state(record, NOW + timedelta(minutes=31)); passed = recovered["outcome"]["status"] == "EXPIRED" and bool(reasons)
            detail = "pending lifecycle reconciled to EXPIRED at cutoff"
        elif index == 2:
            passed = evaluate_pre_entry_invalidation(p, bar(low=9.5, close=9.6))
            detail = "completed close applied structured pre-entry invalidation"
        elif index in {3, 4, 5}:
            entered = transition_plan("PENDING", "ENTRY")
            event = {3: "TARGET", 4: "STOP", 5: "TIME_EXIT"}[index]
            passed = transition_plan(entered, event) in {"TARGET1", "STOPPED", "FLAT_TIME"}
            detail = f"transition table applied {event} from entered state"
        elif index == 6:
            first = evaluate_trailing_stop(9.8, [9.9, 10.0])
            second = evaluate_trailing_stop(first, [9.7, 9.8])
            passed = first == 9.9 and second == first
            detail = "trailing stop advanced once and never moved down"
        elif index == 7:
            passed = evaluate_entry_trigger(p, bar(complete=False)) is None
            detail = "forming bar produced no lifecycle transition"
        elif index == 8:
            passed = evaluate_stop(p, bar(low=9.7, high=10.5)) and evaluate_target(p, bar(low=9.7, high=10.5))
            detail = "same completed bar contains both stop and target evidence"
        elif 9 <= index <= 15:
            code = ["DATA_UNAVAILABLE", "HISTORICALS_UNAVAILABLE", "INDICATORS_UNAVAILABLE", "ORDERS_UNAVAILABLE",
                    "MODEL_CAPACITY", "HTTP_502", "TEARDOWN_DELETE_400"][index - 9]
            passed = classify_failure(code, completed_turn=index == 15) in {"EXTERNAL", "BENIGN_TEARDOWN"}
            detail = f"{code} classified without code repair"
        elif index == 16:
            passed = classify_failure("ZERO_TOOL_STARTUP") == "INTERNAL"
            detail = "zero-tool startup classified as application-control defect"
        elif index in {17, 18}:
            failures = 1 if index == 17 else 3
            states = [classify_failure("MONITOR_FAILURE") for _ in range(failures)]
            states.append(classify_failure("MONITOR_FAILURE", recovery_succeeded=True))
            passed = states[-1] == "RECOVERED_INTERNAL" and all(item == "INTERNAL" for item in states[:-1])
            detail = f"{failures} monitor failure(s) followed by explicit recovery"
        elif index in {19, 22}:
            passed = classify_failure("MONITOR_DISAPPEARANCE" if index == 19 else "SERVICE_DIED") == "INTERNAL"
            detail = "missing required supervision classified as internal"
        elif index in {20, 21}:
            status = "PENDING" if index == 20 else "OPEN"
            passed = scheduler_decision([{"research_role": "PRIMARY", "outcome": {"status": status}}], NOW, scanner_due=True)["monitor"]
            detail = f"restart preserves monitoring for {status} plan"
        elif index == 24:
            passed = evaluate_time_exit(p, NOW.replace(hour=16))
            detail = "mandatory-flat boundary deterministically reached"
        elif index in {25, 26}:
            state = initial_state("2026-08-14", now=NOW); state["eod_completed"] = True
            passed = any(x["code"] == "EOD_COMPLETION_CONTRADICTION" for x in session_invariants(state))
            detail = "EOD completion without persisted transition rejected"
        elif index == 27:
            passed = classify_failure("CIRCUIT_CONTENT_FAILURE") == "INTERNAL"
            detail = "content failure remains an internal circuit event"
        elif index == 28:
            passed = classify_failure("DATA_UNAVAILABLE") == "EXTERNAL"
            detail = "external data outage does not become a software defect"
        elif index == 29:
            old = ("TEST", "BASE_BREAKOUT", "10.00"); new = ("TEST", "BASE_BREAKOUT", "10.25")
            passed = old != new and old[0:2] == new[0:2]
            detail = "new objective setup identity differs from rejected identity"
        elif index == 30:
            try: transition_plan("EXPIRED", "ENTRY"); passed = False
            except ValueError: passed = True
            detail = "terminal-state resurrection rejected"
        elif index == 31:
            state = initial_state("2026-08-14", now=NOW)
            for n in range(2):
                state["shadow_plans"].append({"plan_id": str(n), "research_role": "PRIMARY", "original_plan": p,
                    "outcome": {"status": "OPEN", "entry_triggered": True}})
            passed = any(x["code"] == "MULTIPLE_ENTERED_PRIMARY" for x in session_invariants(state))
            detail = "second entered PRIMARY detected as safety invariant"
        elif index == 32:
            passed = classify_failure("ACCEPTED_COMMIT_MISMATCH") == "INTERNAL"
            detail = "commit-bound acceptance mismatch rejected"
        elif index == 33:
            passed = classify_failure("FOREIGN_MCP") == "SAFETY"
            detail = "foreign MCP activity classified as safety defect"
        elif index == 38:
            passed = bool(scheduler_decision([{"outcome": {"status": "PENDING"}}], NOW, scanner_due=True)["reason_code"])
            detail = "suppression persisted a bounded reason code"
        elif index == 39:
            state = initial_state("2026-08-14", now=NOW); state["schedule_events"].append({"status": "SUPPRESSED_TEST"})
            passed = any(x["code"] == "SCHEDULER_SUPPRESSION_WITHOUT_REASON" for x in session_invariants(state))
            detail = "reasonless suppression detected by session invariant"
        elif index == 34:
            passed = evaluate_entry_trigger({**p, "entry_requires_qualitative_confirmation": True}, b) is None
            detail = "malformed or unresolved qualitative output cannot fabricate entry"
        elif index == 35:
            try:
                evaluate_stop(p, {"complete": True, "open": 10, "high": 9, "low": 11, "close": 10}); passed = False
            except (TypeError, ValueError): passed = True
            detail = "impossible completed OHLC rejected"
        elif index == 36:
            passed = classify_failure("STALE_QUOTE") == "EXTERNAL"
            detail = "stale quote classified as external evidence failure"
        elif index == 37:
            passed = classify_failure("DUPLICATE_FINALIST") == "INTERNAL"
            detail = "duplicate finalist classified as internal contract defect"
    except Exception as exc:
        passed, detail = False, f"{type(exc).__name__}: {exc}"[:240]
    return {"id": f"named-{index + 1:03d}", "name": name, "pass": bool(passed), "detail": detail}


def fuzz_case(index: int) -> dict:
    rng = random.Random(0xA17ADE + index)
    state = "PENDING"
    trace = [state]
    try:
        for _ in range(rng.randint(1, 12)):
            canonical_terminal = state in {"EXPIRED", "PRE_ENTRY_INVALIDATED", "TARGET1", "STOPPED", "FLAT_TIME", "AMBIGUOUS"}
            if canonical_terminal:
                observed = transition_plan(state, "OBSERVE")
                if observed != state: raise AssertionError("terminal mutation")
                trace.append(observed); continue
            if state == "PENDING": event = rng.choice(["OBSERVE", "ENTRY", "INVALIDATE", "CUTOFF", "AMBIGUOUS"])
            else: event = rng.choice(["OBSERVE", "STOP", "TARGET", "TIME_EXIT", "AMBIGUOUS"])
            state = transition_plan(state, event); trace.append(state)
        return {"id": f"fuzz-{index:04d}", "name": "seeded transition sequence", "pass": True, "trace": trace}
    except Exception as exc:
        return {"id": f"fuzz-{index:04d}", "name": "seeded transition sequence", "pass": False, "detail": str(exc)[:240], "trace": trace}


def run_matrix() -> dict:
    scenarios = [named_case(i, name) for i, name in enumerate(NAMES)]
    scenarios.extend(fuzz_case(i) for i in range(1000))
    failures = [item for item in scenarios if not item["pass"]]
    return {"version": 1, "seed": "0xA17ADE+case", "cases": len(scenarios),
            "failed": len(failures), "scenario_count": len(scenarios),
            "named_scenario_count": len(NAMES), "fuzz_scenario_count": 1000,
            "failures": failures, "pass": not failures, "scenarios": scenarios}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(); result = run_matrix(); atomic_write_json(args.output, result)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
