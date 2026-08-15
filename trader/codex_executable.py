from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import ConfigurationError


CODEX_VERSION_PATTERN = re.compile(r"^codex-cli\s+(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
_WINDOWS_SUFFIXES = {".exe", ".cmd", ".bat"}


@dataclass(frozen=True)
class ResolvedCodex:
    path: Path
    version: str


def _is_wsl() -> bool:
    return platform.system() == "Linux" and ("microsoft" in platform.release().lower() or "WSL_DISTRO_NAME" in os.environ)


def _is_windows_mounted(path: Path) -> bool:
    parts = path.resolve(strict=False).parts
    return len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1


def _discover_windows_codex_candidates() -> list[Path]:
    suffixes = [value.lower() for value in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";") if value]
    candidates: set[Path] = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for suffix in suffixes:
            path = Path(directory) / f"codex{suffix}"
            if path.is_file():
                candidates.add(path.resolve(strict=False))
    return sorted(candidates, key=str)


def resolve_codex_executable(settings: Mapping[str, Any]) -> ResolvedCodex:
    explicit_value = settings.get("executable")
    explicit = explicit_value not in (None, "", "auto")
    if explicit:
        candidate = Path(str(explicit_value)).expanduser()
        if not candidate.is_absolute():
            raise ConfigurationError("codex.executable must be an absolute path or 'auto'")
    else:
        if platform.system() == "Windows":
            candidates = _discover_windows_codex_candidates()
            if len(candidates) != 1:
                raise ConfigurationError(f"Expected exactly one native Windows Codex executable; found {len(candidates)}")
            candidate = candidates[0]
        else:
            found = shutil.which("codex")
            if not found:
                raise ConfigurationError("No native Codex executable was found")
            candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("Configured Codex executable does not exist") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ConfigurationError("Configured Codex executable is not an executable file")
    if platform.system() == "Linux":
        suffix = resolved.suffix.lower()
        if suffix in _WINDOWS_SUFFIXES or _is_windows_mounted(resolved):
            qualifier = "explicitly configured" if explicit else "discovered"
            raise ConfigurationError(f"Windows Codex executable was {qualifier} under Linux/WSL and is unsupported")
    try:
        completed = subprocess.run(
            [str(resolved), "--version"], text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=15, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigurationError("Codex version check could not run") from exc
    if completed.returncode != 0:
        raise ConfigurationError("Codex version check failed")
    output = completed.stdout.strip()
    match = CODEX_VERSION_PATTERN.fullmatch(output)
    if not match:
        raise ConfigurationError("Codex returned an unparseable CLI version")
    return ResolvedCodex(resolved, match.group(1))


def codex_child_environment(settings: Mapping[str, Any], source: Mapping[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ if source is None else source)
    if bool(settings.get("strip_openai_api_key", True)):
        result.pop("OPENAI_API_KEY", None)
    return result
