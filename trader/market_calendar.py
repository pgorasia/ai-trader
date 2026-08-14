from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from .models import MarketSession


ET = ZoneInfo("America/New_York")


class EquityMarketCalendar:
    def __init__(self, calendar_name: str = "XNYS", eod_offset_minutes: int = 5) -> None:
        self.calendar = xcals.get_calendar(calendar_name)
        self.eod_offset_minutes = eod_offset_minutes

    def session_for(self, day: date) -> MarketSession | None:
        session_label = day.isoformat()
        if not self.calendar.is_session(session_label):
            return None
        market_open = self.calendar.session_open(session_label).to_pydatetime().astimezone(ET)
        market_close = self.calendar.session_close(session_label).to_pydatetime().astimezone(ET)
        standard_close = datetime.combine(day, time(16, 0), ET)
        early = market_close < standard_close
        latest_entry = min(datetime.combine(day, time(15, 40), ET), market_close - timedelta(minutes=20))
        mandatory_flat = min(datetime.combine(day, time(15, 55), ET), market_close - timedelta(minutes=5))
        eod_time = market_close + timedelta(minutes=self.eod_offset_minutes)
        return MarketSession(day.isoformat(), market_open, market_close, latest_entry, mandatory_flat, eod_time, early)

    def current_or_none(self, now: datetime | None = None) -> MarketSession | None:
        current = (now or datetime.now(ET)).astimezone(ET)
        return self.session_for(current.date())
