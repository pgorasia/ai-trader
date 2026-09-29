#!/usr/bin/env python3
"""Install the commit-bound source-controlled supervisor and systemd units."""
from __future__ import annotations
import argparse
from pathlib import Path
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.runtime_supervisor import parser as supervisor_parser, supervisor_arguments


def _write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(repo: Path, destination_root: Path, *, production_repo: Path, python: Path) -> list[Path]:
    runner = destination_root / "usr/local/bin/ai-trader-supervisor-run"
    script = ("#!/usr/bin/env bash\nset -Eeuo pipefail\n"
              "install -d -m 0755 /var/lib/ai-trader-supervisor\n"
              "journalctl -u ai-trader.service --since '36 hours ago' --no-pager "
              "> /var/lib/ai-trader-supervisor/trader-journal.log\n"
              f"exec {python} {production_repo}/tools/runtime_supervisor.py "
              + " ".join(supervisor_arguments(repo=production_repo, python=str(python))) + " \"$@\"\n")
    _write(runner, script.encode(), 0o755)
    installed = [runner]
    for name in ("ai-trader-supervisor.service", "ai-trader-supervisor.timer"):
        target = destination_root / "etc/systemd/system" / name
        _write(target, (repo / "ops" / name).read_bytes())
        installed.append(target)
    validate_installation(installed, production_repo=production_repo, python=python)
    return installed


def validate_installation(installed: list[Path], *, production_repo: Path, python: Path) -> None:
    runner, service, timer = installed
    if not service.is_file() or not timer.is_file():
        raise RuntimeError("generated supervisor units are missing")
    if not runner.is_file() or not os.access(runner, os.X_OK):
        raise RuntimeError("supervisor ExecStart target is missing or not executable")
    service_text = service.read_text(encoding="utf-8")
    if "ExecStart=/usr/local/bin/ai-trader-supervisor-run" not in service_text:
        raise RuntimeError("supervisor unit ExecStart does not name the installed runner")
    timer_text = timer.read_text(encoding="utf-8")
    if "Unit=ai-trader-supervisor.service" not in timer_text:
        raise RuntimeError("supervisor timer does not target the generated service")
    if "OnCalendar=*-*-* *:00/5:00" not in timer_text:
        raise RuntimeError("supervisor timer does not define the recurring five-minute schedule")
    if "Persistent=true" not in timer_text:
        raise RuntimeError("supervisor timer does not explicitly enable downtime catch-up")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("supervisor interpreter is missing or not executable")
    if not (production_repo / "tools/runtime_supervisor.py").is_file():
        raise RuntimeError("supervisor entrypoint is missing")
    arguments = supervisor_arguments(repo=production_repo, python=str(python))
    supervisor_parser().parse_args(arguments)
    completed = subprocess.run([str(python), str(production_repo / "tools/runtime_supervisor.py"),
                                *arguments, "--self-test"], check=False,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if completed.returncode:
        raise RuntimeError(f"supervisor startup self-test failed: {completed.stderr.strip()}")
    verifier = shutil.which("systemd-analyze")
    if verifier:
        verified = subprocess.run([verifier, "verify", str(service), str(timer)], check=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if verified.returncode:
            raise RuntimeError(f"systemd unit verification failed: {verified.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination-root", type=Path, default=Path("/"))
    parser.add_argument("--production-repo", type=Path)
    parser.add_argument("--repo", type=Path, help="compatibility alias for --production-repo")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--install", action="store_true", help="compatibility flag (installation is always performed)")
    args = parser.parse_args()
    if args.production_repo is None and args.repo is None:
        parser.error("one of --production-repo or --repo is required")
    if (args.production_repo is not None and args.repo is not None
            and args.production_repo != args.repo):
        parser.error("--production-repo and --repo must specify the same path when used together")
    production_repo = args.production_repo if args.production_repo is not None else args.repo
    install(ROOT, args.destination_root, production_repo=production_repo, python=args.python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
