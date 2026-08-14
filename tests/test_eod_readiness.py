from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunResult
from trader.readiness import calculate_readiness
from trader.safety import load_config
from trader.shadow_monitor import ShadowPlanMonitor
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]


class FakeEodRunner:
    def __init__(self, payload):
        self.payload = payload

    def run(self, **kwargs):
        return CodexRunResult(data=self.payload, usage={"total_tokens": 100}, tool_calls={"get_equity_historicals": 1}, web_searches=0)


class EodReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")
        cls.session = EquityMarketCalendar("XNYS").session_for(date(2026, 8, 14))

    def test_readiness_not_reached(self):
        result = calculate_readiness([], self.config)
        self.assertEqual(result["status"], "CONTINUE_SHADOW")
        self.assertFalse(result["requirements"]["market_sessions"])
        self.assertFalse(result["automatic_mode_change"])

    def test_readiness_reached_with_conservative_sample(self):
        states = []
        for session_number in range(10):
            state = initial_state(f"2026-07-{session_number + 1:02d}")
            state["cycles"].append({"cycle_id": str(session_number)})
            for trade_number, pnl in enumerate((0.5, -0.1)):
                state["completed_shadow_trades"].append({
                    "plan_id": f"{session_number}-{trade_number}",
                    "symbol": "TEST",
                    "pnl": pnl,
                    "realized_r": pnl,
                    "hypothetical_notional": 20,
                    "mfe": 0.7,
                    "mae": -0.2,
                    "setup_type": "BASE_BREAKOUT",
                    "market_regime": "TRENDING_BULLISH",
                    "catalyst_classification": "VERIFIED_FRESH",
                    "time_of_day": "10:00",
                })
            states.append(state)
        result = calculate_readiness(states, self.config)
        self.assertEqual(result["status"], "READY_FOR_APPROVAL_REVIEW")
        self.assertTrue(all(result["requirements"].values()))
        self.assertFalse(result["permissions_changed"])

    def test_security_violation_prevents_readiness(self):
        states = []
        for session_number in range(10):
            state = initial_state(f"2026-06-{session_number + 1:02d}")
            state["cycles"].append({"cycle_id": str(session_number)})
            for trade_number in range(2):
                state["completed_shadow_trades"].append({
                    "plan_id": f"{session_number}-{trade_number}", "symbol": "TEST", "pnl": 0.2,
                    "realized_r": 0.2, "hypothetical_notional": 10, "mfe": 0.3, "mae": -0.1,
                    "setup_type": "BASE", "market_regime": "TRENDING_BULLISH",
                    "catalyst_classification": "VERIFIED_FRESH", "time_of_day": "10:00",
                })
            states.append(state)
        states[0]["security_events"].append({"event": "forbidden capability"})
        result = calculate_readiness(states, self.config)
        self.assertEqual(result["status"], "CONTINUE_SHADOW")
        self.assertFalse(result["requirements"]["security_boundary"])

    def test_eod_evaluation_writes_structured_and_markdown_companions(self):
        review = {
            "session_date": "2026-08-14",
            "timestamp": "2026-08-14T16:05:00-04:00",
            "symbol_bars": {},
            "decision_reviews": [],
            "benchmark_closes": {"SPY": 700.0, "QQQ": 600.0},
            "robinhood_tool_call_count": 1,
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = ShadowOrchestrator.__new__(ShadowOrchestrator)
            core.root = root
            core.config = self.config
            core.store = StateStore(root / "state")
            core.runner = FakeEodRunner(review)
            core.monitor = ShadowPlanMonitor()
            state = initial_state("2026-08-14")
            core.store.save(state)
            result = core.eod(state, self.session)
            self.assertTrue((root / "reports" / "2026-08-14-eod.json").is_file())
            self.assertTrue((root / "reports" / "2026-08-14-eod.md").is_file())
            self.assertEqual(result["readiness"]["status"], "CONTINUE_SHADOW")
            saved = core.store.load("2026-08-14")
            self.assertTrue(saved["eod_completed"])


if __name__ == "__main__":
    unittest.main()
