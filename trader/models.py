from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OperatingMode(StrEnum):
    SHADOW = "SHADOW"
    APPROVAL = "APPROVAL"
    LIVE_100 = "LIVE_100"


class CandidateClassification(StrEnum):
    NEW = "NEW"
    MATERIALLY_REQUALIFIED = "MATERIALLY_REQUALIFIED"
    COOLDOWN = "COOLDOWN"
    PREVIOUSLY_REJECTED_NO_MATERIAL_CHANGE = "PREVIOUSLY_REJECTED_NO_MATERIAL_CHANGE"


class SeniorDecision(StrEnum):
    NO_TRADE = "NO_TRADE"
    SHADOW_TRADE_PLAN = "SHADOW_TRADE_PLAN"


class ReadinessStatus(StrEnum):
    CONTINUE_SHADOW = "CONTINUE_SHADOW"
    READY_FOR_APPROVAL_REVIEW = "READY_FOR_APPROVAL_REVIEW"


class ShadowPlanStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    TARGET1 = "TARGET1"
    STOPPED = "STOPPED"
    FLAT_TIME = "FLAT_TIME"
    EXPIRED = "EXPIRED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class MarketSession:
    session_date: str
    market_open: datetime
    market_close: datetime
    latest_entry: datetime
    mandatory_flat: datetime
    eod_time: datetime
    early_close: bool


@dataclass
class CodexRunResult:
    data: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: dict[str, int] = field(default_factory=dict)
    web_searches: int = 0
    attempts: int = 1


class TraderError(RuntimeError):
    """Base error for controlled orchestration failures."""


class ConfigurationError(TraderError):
    pass


class StateCorruptionError(TraderError):
    pass


class SchemaValidationError(TraderError):
    pass


class CodexRunError(TraderError):
    pass


class CodexTimeoutError(CodexRunError):
    pass


class PreflightError(TraderError):
    pass
