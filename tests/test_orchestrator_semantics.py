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
        finalist["material_requalification"] = {"event_type": "COMPLETED_BASE_BREAKOUT", "evidence": "new high", "new_high_only": True}
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


if __name__ == "__main__":
    unittest.main()
