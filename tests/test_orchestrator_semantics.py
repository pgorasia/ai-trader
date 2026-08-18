from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunError, SchemaValidationError
from trader.safety import load_config
from trader.state import initial_state


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class OrchestratorSemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 8, 14))

    def setUp(self):
        self.core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        self.core.config = self.config

    @staticmethod
    def fixture(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_luna_no_candidate_does_not_escalate(self):
        cycle = self.fixture("luna_empty.json")
        self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)

    def test_luna_new_candidate_escalates(self):
        cycle = self.fixture("luna_candidate.json")
        self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)

    def test_cooldown_candidate_cannot_escalate_without_material_change(self):
        cycle = self.fixture("luna_candidate.json")
        cycle["finalists"][0]["classification"] = "COOLDOWN"
        cycle["sol_escalation"] = False
        state = initial_state("2026-08-14")
        state["cooldowns"]["TEST"] = {
            "symbol": "TEST",
            "rejected_at": "2026-08-14T09:45:00-04:00",
            "cooldown_until": "2026-08-14T10:15:00-04:00",
            "original_rejection_reason": "Extension",
            "rejection_categories": ["EXTENSION"],
            "active": True,
        }
        self.core._validate_luna(cycle, state, self.session, 0)

    def test_material_requalification_allows_escalation(self):
        cycle = self.fixture("luna_candidate.json")
        finalist = cycle["finalists"][0]
        finalist["classification"] = "MATERIALLY_REQUALIFIED"
        finalist["material_requalification"] = {
            "event_type": "COMPLETED_BASE_BREAKOUT",
            "evidence": "A new three-bar completed base broke on persistent volume.",
            "evidence_timestamp": "2026-08-14T09:55:00-04:00",
            "new_high_only": False,
        }
        state = initial_state("2026-08-14")
        state["cooldowns"]["TEST"] = {
            "symbol": "TEST",
            "rejected_at": "2026-08-14T09:20:00-04:00",
            "cooldown_until": "2026-08-14T09:50:00-04:00",
            "original_rejection_reason": "No base",
            "rejection_categories": ["NO_OBJECTIVE_ENTRY"],
            "active": True,
        }
        self.core._validate_luna(cycle, state, self.session, 0)

    def test_new_high_alone_cannot_be_structured_as_material(self):
        cycle = self.fixture("luna_candidate.json")
        finalist = cycle["finalists"][0]
        finalist["classification"] = "MATERIALLY_REQUALIFIED"
        finalist["material_requalification"] = {"event_type": "COMPLETED_BASE_BREAKOUT", "evidence": "new high", "evidence_timestamp": "2026-08-14T09:55:00-04:00", "new_high_only": True}
        with self.assertRaises(SchemaValidationError):
            from trader.safety import validate_json
            validate_json(cycle, ROOT / "schemas" / "luna-cycle.schema.json")

    def test_senior_no_trade(self):
        decision = self.fixture("senior_no_trade.json")
        finalists = self.fixture("luna_candidate.json")["finalists"]
        self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 2)

    def test_senior_shadow_plan_risk_and_time_gates(self):
        decision = self.fixture("senior_plan.json")
        finalists = self.fixture("luna_candidate.json")["finalists"]
        self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 3)

    def test_senior_sizing_ignores_external_positions_orders_and_brokerage_capital(self):
        decision = self.fixture("senior_plan.json")
        finalists = self.fixture("luna_candidate.json")["finalists"]
        state = initial_state("2026-08-14")
        state["baseline_positions"] = [{"attribution": "BASELINE_EXTERNAL", "symbol": "TEST", "quantity": 1000000.0}]
        state["baseline_external_orders"] = [{"attribution": "BASELINE_EXTERNAL_ORDER", "symbol": "TEST", "side": "buy", "state": "open"}]
        state["brokerage_snapshot"] = {"account_equity": 1.0, "buying_power": 0.0, "reserved_buying_power": 1000000.0}
        self.core._validate_senior(decision, finalists, state, self.session, 3)

    def test_senior_required_web_failure_fails_closed(self):
        decision = self.fixture("senior_no_trade.json")
        finalists = self.fixture("luna_candidate.json")["finalists"]
        with self.assertRaises(CodexRunError):
            self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 0)

    def test_senior_plan_over_risk_is_rejected(self):
        decision = self.fixture("senior_plan.json")
        decision["hypothetical_quantity"] = 3
        finalists = self.fixture("luna_candidate.json")["finalists"]
        with self.assertRaises(SchemaValidationError):
            self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 3)

    def test_luna_duplicate_and_invented_finalists_rejected(self):
        cycle = self.fixture("luna_candidate.json")
        cycle["finalists"].append(deepcopy(cycle["finalists"][0]))
        with self.assertRaises(SchemaValidationError): self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)
        cycle = self.fixture("luna_candidate.json"); cycle["finalists"][0]["symbol"] = "FAKE"
        with self.assertRaises(SchemaValidationError): self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)

    def test_luna_requires_exactly_one_observed_scan(self):
        cycle = self.fixture("luna_candidate.json")
        for count in (0, 2):
            with self.subTest(count=count), self.assertRaises(SchemaValidationError):
                self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0, observed_tool_calls={"run_scan": count})

    def test_senior_inconsistent_notional_and_reward_risk_rejected(self):
        finalists = self.fixture("luna_candidate.json")["finalists"]
        for key, value in (("hypothetical_notional", 19.0), ("reward_risk_target1", 3.0)):
            decision = self.fixture("senior_plan.json"); decision[key] = value
            with self.subTest(key=key), self.assertRaises(SchemaValidationError):
                self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 3)

    def test_senior_must_evaluate_every_finalist(self):
        decision = self.fixture("senior_no_trade.json")
        finalists = self.fixture("luna_candidate.json")["finalists"]
        second = deepcopy(finalists[0]); second["symbol"] = "ALT"
        with self.assertRaises(SchemaValidationError):
            self.core._validate_senior(decision, finalists + [second], initial_state("2026-08-14"), self.session, 2)

    def test_senior_sizes_at_worst_allowed_chase_price(self):
        decision = self.fixture("senior_plan.json")
        decision["maximum_chase_price"] = 10.1
        decision["entry_condition"] = "Trigger at 10.00 and allow a fill through 10.10."
        with self.assertRaisesRegex(SchemaValidationError, "maximum chase"):
            self.core._validate_senior(decision, self.fixture("luna_candidate.json")["finalists"], initial_state("2026-08-14"), self.session, 3)

    def test_senior_rejects_stale_or_missing_quote_and_already_chased_plan(self):
        finalists = self.fixture("luna_candidate.json")["finalists"]
        for mutation in ("missing_quote", "stale_quote", "already_chased"):
            decision = self.fixture("senior_plan.json")
            if mutation == "missing_quote": decision["quote_timestamp"] = None
            elif mutation == "stale_quote": decision["quote_timestamp"] = "2026-08-14T10:00:00-04:00"
            else: decision["current_price"] = 10.01
            with self.subTest(mutation=mutation), self.assertRaises(SchemaValidationError):
                self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 3)

    def test_second_entered_shadow_trade_is_rejected(self):
        state = initial_state("2026-08-14")
        state["shadow_plans"].append({"outcome": {"status": "STOPPED", "entry_triggered": True}})
        with self.assertRaisesRegex(SchemaValidationError, "one entered"):
            self.core._validate_senior(self.fixture("senior_plan.json"), self.fixture("luna_candidate.json")["finalists"], state, self.session, 3)

    def test_luna_finalist_requires_observed_market_evidence(self):
        cycle = self.fixture("luna_candidate.json")
        observed = {"get_accounts": 1, "get_portfolio": 1, "get_equity_orders": 1, "get_equity_positions": 1, "run_scan": 1}
        cycle["tool_call_count"]["total"] = sum(observed.values())
        with self.assertRaisesRegex(SchemaValidationError, "market-data evidence"):
            self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0, observed_tool_calls=observed)

    def test_luna_rejects_misaligned_or_forming_15_minute_structure(self):
        cycle = self.fixture("luna_candidate.json")
        cycle["finalists"][0]["completed_15m_structure"] = [{"timestamp": "2026-08-14T09:55:00-04:00", "open": 9.8, "high": 10.1, "low": 9.7, "close": 10.0, "volume": 1000, "complete": True}]
        with self.assertRaisesRegex(SchemaValidationError, "15-minute"):
            self.core._validate_luna(cycle, initial_state("2026-08-14"), self.session, 0)

    def test_senior_nonfinite_values_rejected(self):
        finalists = self.fixture("luna_candidate.json")["finalists"]
        for value in (float("nan"), float("inf")):
            decision = self.fixture("senior_plan.json"); decision["hypothetical_quantity"] = value
            with self.subTest(value=value), self.assertRaises(SchemaValidationError):
                self.core._validate_senior(decision, finalists, initial_state("2026-08-14"), self.session, 3)

    def test_exact_cooldown_boundary_does_not_create_materiality(self):
        cycle = self.fixture("luna_candidate.json"); finalist = cycle["finalists"][0]
        state = initial_state("2026-08-14"); state["cooldowns"]["TEST"] = {"symbol": "TEST", "rejected_at": "2026-08-14T09:30:00-04:00", "cooldown_until": "2026-08-14T10:00:00-04:00", "original_rejection_reason": "x", "rejection_categories": ["EXTENSION"], "active": True}
        finalist["classification"] = "PREVIOUSLY_REJECTED_NO_MATERIAL_CHANGE"; cycle["sol_escalation"] = False
        self.core._validate_luna(cycle, state, self.session, 0)
        finalist["classification"] = "MATERIALLY_REQUALIFIED"; finalist["material_requalification"] = {"event_type": "COMPLETED_BASE_BREAKOUT", "evidence": "old", "evidence_timestamp": "2026-08-14T09:30:00-04:00", "new_high_only": False}; cycle["sol_escalation"] = True
        with self.assertRaises(SchemaValidationError): self.core._validate_luna(cycle, state, self.session, 0)

    def test_bogus_material_requalification_rejected(self):
        cycle = self.fixture("luna_candidate.json"); finalist = cycle["finalists"][0]
        state = initial_state("2026-08-14"); state["cooldowns"]["TEST"] = {"symbol": "TEST", "rejected_at": "2026-08-14T09:30:00-04:00", "cooldown_until": "2026-08-14T10:00:00-04:00", "original_rejection_reason": "x", "rejection_categories": ["EXTENSION"], "active": True}
        finalist["classification"] = "MATERIALLY_REQUALIFIED"; finalist["material_requalification"] = {"event_type": "COOLDOWN_EXPIRED", "evidence": "30 minutes elapsed", "evidence_timestamp": "2026-08-14T10:00:00-04:00", "new_high_only": False}
        with self.assertRaises(SchemaValidationError): self.core._validate_luna(cycle, state, self.session, 0)


if __name__ == "__main__":
    unittest.main()
