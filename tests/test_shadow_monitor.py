from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from trader.shadow_monitor import ShadowPlanMonitor, aggregate_completed_15m


ROOT = Path(__file__).resolve().parents[1]


def bar(timestamp: str, open_price: float, high: float, low: float, close: float, complete: bool = True):
    return {"timestamp": timestamp, "open": open_price, "high": high, "low": low, "close": close, "volume": 1000, "complete": complete}


class ShadowMonitorTests(unittest.TestCase):
    def setUp(self):
        plan = json.loads((ROOT / "tests" / "fixtures" / "senior_plan.json").read_text(encoding="utf-8"))
        self.record = {"plan_id": "p1", "frozen_at": plan["decision_timestamp"], "original_plan": plan, "outcome": ShadowPlanMonitor.initial_outcome()}
        self.monitor = ShadowPlanMonitor()

    def test_shadow_trigger(self):
        bars = [bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1)]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "OPEN")
        self.assertEqual(result["outcome"]["entry_price"], 10.0)

    def test_stop_before_target(self):
        bars = [
            bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1),
            bar("2026-08-14T10:10:00-04:00", 10.1, 10.4, 9.4, 9.6),
        ]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T10:15:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "STOPPED")
        self.assertTrue(result["outcome"]["stop_hit"])
        self.assertAlmostEqual(result["outcome"]["pnl"], -1.0)

    def test_target_before_stop(self):
        bars = [
            bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1),
            bar("2026-08-14T10:10:00-04:00", 10.1, 11.1, 9.6, 11.0),
        ]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T10:15:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "TARGET1")
        self.assertTrue(result["outcome"]["target1_hit"])
        self.assertAlmostEqual(result["outcome"]["pnl"], 2.0)

    def test_ambiguous_same_bar_stop_and_target(self):
        bars = [
            bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1),
            bar("2026-08-14T10:10:00-04:00", 10.1, 11.1, 9.4, 10.0),
        ]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T10:15:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "AMBIGUOUS")
        self.assertIsNone(result["outcome"]["pnl"])

    def test_entry_and_stop_same_crossing_bar_is_ambiguous(self):
        bars = [bar("2026-08-14T10:05:00-04:00", 9.4, 10.2, 9.3, 9.8)]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "AMBIGUOUS")

    def test_entry_after_cutoff_is_not_inferred(self):
        bars = [bar("2026-08-14T15:40:00-04:00", 9.8, 10.2, 9.7, 10.1)]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T15:45:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "EXPIRED")
        self.assertFalse(result["outcome"]["entry_triggered"])
        self.assertFalse(result["outcome"]["entry_before_cutoff"])

    def test_mandatory_flat_exit_uses_last_completed_close(self):
        bars = [
            bar("2026-08-14T15:35:00-04:00", 9.9, 10.2, 9.8, 10.1),
            bar("2026-08-14T15:40:00-04:00", 10.1, 10.4, 9.8, 10.2),
            bar("2026-08-14T15:45:00-04:00", 10.2, 10.4, 9.8, 10.3),
            bar("2026-08-14T15:50:00-04:00", 10.3, 10.4, 9.8, 10.25),
        ]
        result = self.monitor.evaluate(self.record, bars, datetime.fromisoformat("2026-08-14T15:55:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "FLAT_TIME")
        self.assertEqual(result["outcome"]["exit_price"], 10.25)

    def test_partial_five_minute_bar_is_ignored(self):
        bars = [
            bar("2026-08-14T10:00:00-04:00", 10, 10.2, 9.9, 10.1),
            bar("2026-08-14T10:05:00-04:00", 10.1, 10.3, 10, 10.2),
            bar("2026-08-14T10:10:00-04:00", 10.2, 12, 9, 11, complete=False),
        ]
        self.assertEqual(aggregate_completed_15m(bars), [])

    def test_completed_15_minute_aggregation_requires_three_aligned_bars(self):
        bars = [
            bar("2026-08-14T10:00:00-04:00", 10, 10.2, 9.9, 10.1),
            bar("2026-08-14T10:05:00-04:00", 10.1, 10.3, 10, 10.2),
            bar("2026-08-14T10:10:00-04:00", 10.2, 10.4, 10.1, 10.35),
        ]
        aggregate = aggregate_completed_15m(bars)
        self.assertEqual(len(aggregate), 1)
        self.assertEqual(aggregate[0]["close"], 10.35)
        self.assertEqual(aggregate[0]["volume"], 3000)

    def test_original_plan_is_never_modified(self):
        original = deepcopy(self.record["original_plan"])
        self.monitor.evaluate(self.record, [bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1)])
        self.assertEqual(self.record["original_plan"], original)

    def test_resumed_open_plan_does_not_replay_pre_entry_bars(self):
        first = self.monitor.evaluate(
            self.record,
            [bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1)],
            datetime.fromisoformat("2026-08-14T10:10:00-04:00"),
        )
        bars = [
            bar("2026-08-14T10:00:00-04:00", 9.0, 9.2, 8.5, 9.1),
            bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1),
            bar("2026-08-14T10:10:00-04:00", 10.1, 10.3, 9.8, 10.2),
        ]
        resumed = self.monitor.evaluate(first, bars, datetime.fromisoformat("2026-08-14T10:15:00-04:00"))
        self.assertEqual(resumed["outcome"]["status"], "OPEN")

    def test_completed_close_entry_ignores_same_bar_prior_extremes(self):
        record = deepcopy(self.record)
        record["original_plan"]["entry_trigger_type"] = "COMPLETED_5M_CLOSE_AT_OR_ABOVE"
        bars = [bar("2026-08-14T10:05:00-04:00", 9.4, 11.2, 9.0, 10.05)]
        result = self.monitor.evaluate(record, bars, datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "OPEN")
        self.assertEqual(result["outcome"]["mfe"], 0.0)
        self.assertEqual(result["outcome"]["mae"], 0.0)

    def test_flat_bar_open_precedes_that_bars_extremes(self):
        record = deepcopy(self.record)
        record["outcome"].update({
            "status": "OPEN", "entry_triggered": True, "entry_timestamp": "2026-08-14T15:35:00-04:00",
            "entry_bar_timestamp": "2026-08-14T15:35:00-04:00", "entry_price": 10.0,
            "entry_via_open": True, "entry_at_close": False, "entry_before_cutoff": True, "mfe": 0.2, "mae": -0.1,
        })
        bars = [bar("2026-08-14T15:55:00-04:00", 10.2, 11.5, 9.0, 10.5)]
        result = self.monitor.evaluate(record, bars, datetime.fromisoformat("2026-08-14T16:00:00-04:00"))
        self.assertEqual(result["outcome"]["status"], "FLAT_TIME")
        self.assertEqual(result["outcome"]["exit_price"], 10.2)


if __name__ == "__main__":
    unittest.main()
