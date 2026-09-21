from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tools.fault_matrix import run_matrix
from tools.replay_session import replay
from tools.runtime_audit import audit as runtime_audit
from trader.lifecycle import (completed_eod_operation_id, evaluate_entry_trigger, evaluate_stop,
    reconcile_plan_state, scheduler_decision, session_invariants, transition_plan)
from trader.operations import complete_eod_recovery, prepare
from trader.runtime_supervision import (EXIT_EXTERNAL_DEGRADED, EXIT_INTERNAL_DEFECT,
    EXIT_SAFETY_DEFECT, classify, remediation_decision, update_stability)
from trader.state import initial_state
from trader.readiness import calculate_readiness
from orchestrator import load_config, CONFIG_PATH


NOW = datetime.fromisoformat("2026-09-17T15:45:00-04:00")


def plan():
    return {"symbol": "TEST", "decision_timestamp": "2026-09-17T10:00:00-04:00",
            "entry_trigger": 10, "maximum_chase_price": 10.2, "stop_price": 9.8, "target1": 10.4,
            "hypothetical_quantity": 5, "hypothetical_notional": 50, "planned_dollar_risk": 1,
            "latest_entry_time": "2026-09-17T15:40:00-04:00",
            "mandatory_flat_time": "2026-09-17T15:55:00-04:00"}


class FoundationReliabilityTests(unittest.TestCase):
    def test_terminal_state_cannot_resurrect(self):
        for status in ("EXPIRED", "PRE_ENTRY_INVALIDATED", "TARGET1", "STOPPED", "FLAT_TIME", "AMBIGUOUS"):
            with self.subTest(status=status), self.assertRaises(ValueError): transition_plan(status, "ENTRY")

    def test_qualitative_gate_never_fabricates_entry(self):
        candidate = {**plan(), "entry_requires_qualitative_confirmation": True}
        bar = {"complete": True, "open": 10, "high": 10.5, "low": 9.9, "close": 10.3}
        self.assertIsNone(evaluate_entry_trigger(candidate, bar))

    def test_close_confirmed_entry_does_not_use_intrabar_high(self):
        candidate = {**plan(), "entry_trigger_type": "COMPLETED_5M_CLOSE_AT_OR_ABOVE"}
        crossed_but_closed_below = {"complete": True, "open": 9.9, "high": 10.1, "low": 9.8, "close": 9.95}
        self.assertIsNone(evaluate_entry_trigger(candidate, crossed_but_closed_below))
        confirmed = {**crossed_but_closed_below, "close": 10.05}
        self.assertEqual(evaluate_entry_trigger(candidate, confirmed), 10.05)

    def test_completed_impossible_ohlc_fails_closed(self):
        malformed = {"complete": True, "open": 10, "high": 9.9, "low": 9.8, "close": 9.85}
        with self.assertRaisesRegex(ValueError, "impossible OHLC"):
            evaluate_entry_trigger(plan(), malformed)
        with self.assertRaisesRegex(ValueError, "impossible OHLC"):
            evaluate_stop(plan(), malformed)

    def test_pending_reconciles_at_cutoff_and_scanning_resumes(self):
        record = {"plan_id": "p", "original_plan": plan(), "outcome": {"status": "PENDING", "entry_triggered": False}}
        fixed, reasons = reconcile_plan_state(record, NOW)
        self.assertEqual(fixed["outcome"]["status"], "EXPIRED")
        self.assertTrue(reasons)
        self.assertEqual(scheduler_decision([fixed], NOW, scanner_due=True)["reason_code"], "SCAN_SLOT_ELIGIBLE")

    def test_scheduler_suppression_always_has_reason(self):
        record = {"original_plan": plan(), "outcome": {"status": "PENDING"}}
        decision = scheduler_decision([record], NOW, scanner_due=True)
        self.assertTrue(decision["monitor"]); self.assertFalse(decision["scanner"]); self.assertTrue(decision["reason_code"])

    def test_eod_recovery_is_a_persisted_transition(self):
        state = initial_state("2026-09-17", now=NOW)
        failed = prepare(state, "eod:2026-09-17", "EOD", NOW, 3); failed["state"] = "FAILED_TERMINAL"
        complete_eod_recovery(state, NOW, {"session_date": "2026-09-17", "status": "RECOVERED"})
        self.assertTrue(state["eod_completed"])
        self.assertFalse(session_invariants(state))
        self.assertEqual(state["usage_counts"]["eod_runs"], 1)
        self.assertEqual(state["usage_counts"]["eod_completed_runs"], 1)

    def test_eod_recovery_is_visible_to_readiness(self):
        state = initial_state("2026-09-17", now=NOW)
        state["strategy_version"] = "ai-daytrader-v1-accelerated-shadow-2026-08"
        complete_eod_recovery(state, NOW, {
            "session_date": "2026-09-17", "status": "RECOVERED",
            "benchmark_closes": {"SPY": None, "QQQ": None},
        })
        result = calculate_readiness([state], load_config(CONFIG_PATH))
        self.assertEqual(result["metrics"]["market_sessions"], 1)

    def test_contradictory_eod_detected(self):
        state = initial_state("2026-09-17", now=NOW); state["eod_completed"] = True
        self.assertIn("EOD_COMPLETION_CONTRADICTION", {x["code"] for x in session_invariants(state)})

    def test_eod_provenance_requires_eod_operation_and_matching_review(self):
        state = initial_state("2026-09-17", now=NOW)
        operation_id = "eod:2026-09-17"
        state["eod_completed"] = True
        state["eod_review"] = {"session_date": "2026-09-16"}
        state["operation_ids"].append(operation_id)
        state["ai_operations"].append({"operation_id": operation_id, "operation_type": "MONITOR", "state": "COMPLETED"})
        state["usage_counts"].update(eod_runs=1, eod_completed_runs=1)
        self.assertIn("EOD_COMPLETION_CONTRADICTION", {x["code"] for x in session_invariants(state)})

    def test_failed_eod_operation_id_is_never_completion_provenance(self):
        state = initial_state("2026-09-17", now=NOW)
        operation_id = "eod:2026-09-17"
        failed = prepare(state, operation_id, "EOD", NOW, 3)
        failed["state"] = "FAILED_TERMINAL"
        state["operation_ids"].append(operation_id)
        state["eod_completed"] = True
        state["eod_review"] = {"session_date": "2026-09-17"}
        state["usage_counts"].update(eod_runs=1, eod_completed_runs=1)
        self.assertIsNone(completed_eod_operation_id(state))
        self.assertIn("EOD_COMPLETION_CONTRADICTION", {x["code"] for x in session_invariants(state)})

    def test_fault_matrix_has_required_scale(self):
        result = run_matrix(); self.assertTrue(result["pass"]); self.assertGreaterEqual(result["scenario_count"], 1000)
        self.assertEqual(result["cases"], result["scenario_count"])
        self.assertEqual(result["failed"], len(result["failures"]))
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["named_scenario_count"], 40)

    def test_replay_reports_incomplete_input_without_fabricating(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-09-17.json"; path.write_text(json.dumps({"session_date": "2026-09-17"}))
            result = replay(path)
        self.assertIn("INCOMPLETE_INPUT", {x["code"] for x in result["errors"]})
        self.assertFalse(result["pass"])

    def test_incident_precedence_and_remediation(self):
        self.assertEqual(classify([{"severity": "EXTERNAL"}]), EXIT_EXTERNAL_DEGRADED)
        self.assertEqual(classify([{"severity": "EXTERNAL"}, {"severity": "INTERNAL"}]), EXIT_INTERNAL_DEFECT)
        self.assertEqual(classify([{"severity": "SAFETY"}]), EXIT_SAFETY_DEFECT)
        self.assertEqual(remediation_decision(service_inactive=True, session_active=True, accepted=True,
            acceptance_valid=True, safety_defect=False)["action"], "RESTART_SERVICE")

    def test_missing_active_session_heartbeat_is_internal_and_restartable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "reliability_acceptance.json").write_text("{}", encoding="utf-8")
            # Acceptance lives below repo/state; use a stable fake HEAD and a
            # complete canonical artifact so liveness is isolated here.
            artifact = {"version": 1, "mode": "SHADOW", "offline_gate": "PASS",
                        "live_read_only_gate": "PASS",
                        "live_run_counts": {"preflight": 5, "luna_schema": 5, "eod": 3},
                        "global_shadow_tool_count": 22, "accepted_git_commit": "a" * 40}
            (state_dir / "reliability_acceptance.json").write_text(json.dumps(artifact), encoding="utf-8")
            from unittest.mock import patch
            with patch("tools.runtime_audit.git_head", return_value="a" * 40):
                result = runtime_audit(root, state_dir, root / "journal.log",
                    now=datetime.fromisoformat("2026-09-17T10:00:00-04:00"))
        self.assertEqual(result["classification"], "INTERNAL_DEFECT")
        self.assertIn("SERVICE_INACTIVE", {item["code"] for item in result["findings"]})
        self.assertEqual(result["remediation"]["action"], "RESTART_SERVICE")

    def test_stability_needs_three_distinct_clean_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stability.json"
            self.assertFalse(path.exists())
            for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
                value = update_stability(path, session=day, complete=True, internal_defect=False,
                    external_degradation=False, recovery_correct=True)
            self.assertTrue(path.is_file())
        self.assertEqual(value["status"], "STABLE")

    def test_stability_requires_consecutive_market_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stability.json"
            for day in ("2026-09-14", "2026-09-16", "2026-09-17"):
                value = update_stability(path, session=day, complete=True, internal_defect=False,
                    external_degradation=False, recovery_correct=True)
        self.assertEqual(value["consecutive_clean_sessions"], 2)
        self.assertEqual(value["status"], "BUILDING")


if __name__ == "__main__": unittest.main()
