from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import StateCorruptionError


STATE_LIST_FIELDS = (
    "cycles",
    "senior_decisions",
    "shadow_plans",
    "shadow_positions",
    "completed_shadow_trades",
    "errors",
    "security_events",
)


def initial_state(session_date: str, timezone_name: str = "America/New_York") -> dict[str, Any]:
    now = datetime.now(ZoneInfo(timezone_name)).isoformat()
    state: dict[str, Any] = {
        "version": 1,
        "mode": "SHADOW",
        "session_date": session_date,
        "created_at": now,
        "updated_at": now,
        "cooldowns": {},
        "usage_counts": {
            "luna_runs": 0,
            "sol_runs": 0,
            "preflight_runs": 0,
            "monitor_runs": 0,
            "eod_runs": 0,
            "robinhood_tool_calls": 0,
            "web_searches": 0,
            "failed_runs": 0,
            "tokens": {},
        },
        "eod_completed": False,
    }
    for field in STATE_LIST_FIELDS:
        state[field] = []
    return state


def validate_state_shape(state: Any, expected_date: str | None = None) -> None:
    if not isinstance(state, dict):
        raise StateCorruptionError("State root must be a JSON object")
    required = {"mode", "session_date", "cycles", "senior_decisions", "cooldowns", "shadow_plans", "shadow_positions", "completed_shadow_trades", "errors", "security_events", "usage_counts"}
    missing = sorted(required - state.keys())
    if missing:
        raise StateCorruptionError(f"State is missing required fields: {', '.join(missing)}")
    if state["mode"] != "SHADOW":
        raise StateCorruptionError("Only SHADOW state is supported")
    if expected_date and state["session_date"] != expected_date:
        raise StateCorruptionError("State session_date does not match its filename")
    for field in STATE_LIST_FIELDS:
        if not isinstance(state.get(field), list):
            raise StateCorruptionError(f"State field {field} must be a list")
    if not isinstance(state["cooldowns"], dict) or not isinstance(state["usage_counts"], dict):
        raise StateCorruptionError("cooldowns and usage_counts must be objects")


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


class StateStore:
    def __init__(self, root: Path, timezone_name: str = "America/New_York") -> None:
        self.root = root
        self.timezone_name = timezone_name

    def path_for(self, session_date: str | date) -> Path:
        value = session_date.isoformat() if isinstance(session_date, date) else session_date
        return self.root / f"{value}.json"

    def load(self, session_date: str | date, create: bool = True) -> dict[str, Any]:
        value = session_date.isoformat() if isinstance(session_date, date) else session_date
        path = self.path_for(value)
        if not path.exists():
            if not create:
                raise StateCorruptionError(f"State file does not exist: {path}")
            return initial_state(value, self.timezone_name)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptionError(f"Cannot parse state file {path}: {exc}") from exc
        validate_state_shape(state, value)
        return state

    def save(self, state: dict[str, Any]) -> Path:
        candidate = deepcopy(state)
        validate_state_shape(candidate)
        candidate["updated_at"] = datetime.now(ZoneInfo(self.timezone_name)).isoformat()
        path = self.path_for(candidate["session_date"])
        atomic_write_json(path, candidate)
        return path

    def all_states(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        states = []
        for path in sorted(self.root.glob("????-??-??.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                validate_state_shape(state, path.stem)
            except (OSError, json.JSONDecodeError, StateCorruptionError) as exc:
                raise StateCorruptionError(f"Cannot load historical state {path}: {exc}") from exc
            states.append(state)
        return states
