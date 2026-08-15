from __future__ import annotations

import json
import os
import platform
import socket
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from .models import PreflightError


class SingleInstanceLock:
    """Non-blocking process lock for the current native platform.

    Linux/WSL uses flock and Windows uses msvcrt.locking. Lock semantics are
    never shared across the Windows/WSL boundary; the native process owns them.
    """

    def __init__(self, path: Path, session_date: str, started_at: datetime) -> None:
        self.path = path.resolve(strict=False)
        self.metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_start_timestamp": started_at.isoformat(),
            "session_date": session_date,
            "platform": platform.system(),
        }
        self._stream: IO[str] | None = None

    def acquire(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            self._lock_native(stream)
            prior = self._read_metadata(stream)
            if prior and not self._stale_owner_is_safe(prior):
                self._unlock_native(stream)
                raise PreflightError("Single-instance lock has ambiguous stale owner metadata")
            stream.seek(0)
            stream.truncate()
            json.dump(self.metadata, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            self._stream = stream
            return self
        except Exception:
            stream.close()
            raise

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            self._unlock_native(self._stream)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()

    @staticmethod
    def _read_metadata(stream: IO[str]) -> dict[str, Any] | None:
        stream.seek(0)
        text = stream.read().strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PreflightError("Single-instance lock metadata is malformed") from exc
        if not isinstance(value, dict):
            raise PreflightError("Single-instance lock metadata is malformed")
        return value

    def _stale_owner_is_safe(self, owner: dict[str, Any]) -> bool:
        required = {"pid", "hostname", "process_start_timestamp", "session_date", "platform"}
        if set(owner) != required or owner.get("platform") != platform.system():
            return False
        if owner.get("hostname") != socket.gethostname():
            return False
        try:
            pid = int(owner["pid"])
            datetime.fromisoformat(str(owner["process_start_timestamp"]))
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return pid == os.getpid()

    @staticmethod
    def _lock_native(stream: IO[str]) -> None:
        if platform.system() == "Windows":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(" "); stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise PreflightError("Another orchestrator instance owns the process lock") from exc
        elif platform.system() == "Linux":
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PreflightError("Another orchestrator instance owns the process lock") from exc
        else:
            raise PreflightError(f"Unsupported process-lock platform: {platform.system()}")

    @staticmethod
    def _unlock_native(stream: IO[str]) -> None:
        if platform.system() == "Windows":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        elif platform.system() == "Linux":
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
