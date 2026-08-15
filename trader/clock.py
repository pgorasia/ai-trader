from __future__ import annotations

from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


class TrustedClock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(ET)
