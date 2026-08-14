from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import MarketSession


ET = ZoneInfo("America/New_York")


def _at(day, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), ET)


class SessionScheduler:
    def __init__(self, schedule_config: dict) -> None:
        self.config = schedule_config

    def scan_times(self, session: MarketSession) -> list[datetime]:
        day = session.market_open.date()
        first = max(session.market_open + timedelta(minutes=10), _at(day, self.config["no_entry_before"]))
        morning_end = min(_at(day, self.config["morning_end"]), session.latest_entry)
        midday_end = min(_at(day, self.config["midday_end"]), session.latest_entry)
        periods = [
            (first, morning_end, int(self.config["morning_interval_minutes"])),
            (max(first, morning_end), midday_end, int(self.config["midday_interval_minutes"])),
            (max(first, midday_end), session.latest_entry, int(self.config["afternoon_interval_minutes"])),
        ]
        slots: set[datetime] = set()
        for start, end, minutes in periods:
            cursor = start
            while cursor < end and cursor < session.latest_entry:
                if cursor >= first:
                    slots.add(cursor)
                cursor += timedelta(minutes=minutes)
        return sorted(slots)

    def due_or_future(self, session: MarketSession, now: datetime, completed_slot_ids: Iterable[str]) -> list[datetime]:
        completed = set(completed_slot_ids)
        current = now.astimezone(ET)
        return [slot for slot in self.scan_times(session) if slot >= current and slot.isoformat() not in completed]

    @staticmethod
    def late_selectivity(session: MarketSession, timestamp: datetime) -> str:
        remaining = (session.latest_entry - timestamp.astimezone(ET)).total_seconds() / 60
        if remaining <= 0:
            return "CLOSED"
        if remaining <= 10:
            return "EXTREME"
        if remaining <= 20:
            return "HIGH"
        return "NORMAL"
