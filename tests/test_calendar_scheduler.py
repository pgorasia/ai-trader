from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from trader.market_calendar import EquityMarketCalendar
from trader.safety import load_config
from trader.scheduler import SessionScheduler


ROOT = Path(__file__).resolve().parents[1]


class CalendarSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "strategy.yaml")
        cls.calendar = EquityMarketCalendar("XNYS")
        cls.scheduler = SessionScheduler(cls.config["schedule"])

    def test_full_session_schedule(self):
        session = self.calendar.session_for(date(2026, 8, 14))
        self.assertIsNotNone(session)
        self.assertFalse(session.early_close)
        slots = self.scheduler.scan_times(session)
        self.assertEqual(slots[0].strftime("%H:%M"), "09:40")
        self.assertEqual(slots[-1].strftime("%H:%M"), "15:30")
        self.assertTrue(all(item < session.latest_entry for item in slots))
        self.assertEqual(len(slots), 26)

    def test_early_close_market_session(self):
        session = self.calendar.session_for(date(2026, 11, 27))
        self.assertIsNotNone(session)
        self.assertTrue(session.early_close)
        self.assertEqual(session.market_close.strftime("%H:%M"), "13:00")
        self.assertEqual(session.latest_entry.strftime("%H:%M"), "12:40")
        self.assertEqual(session.mandatory_flat.strftime("%H:%M"), "12:55")
        self.assertEqual(session.eod_time.strftime("%H:%M"), "13:05")
        slots = self.scheduler.scan_times(session)
        self.assertEqual(slots[-1].strftime("%H:%M"), "12:15")
        self.assertTrue(all(item < session.latest_entry for item in slots))

    def test_exchange_holiday_is_not_a_session(self):
        self.assertIsNone(self.calendar.session_for(date(2026, 11, 26)))


if __name__ == "__main__":
    unittest.main()
