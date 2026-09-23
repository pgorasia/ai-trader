#!/usr/bin/env python3
"""Install the commit-bound source-controlled supervisor and systemd units."""
from __future__ import annotations
import argparse
from pathlib import Path
import os
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]


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
              f"--repo {production_repo} --state-dir {production_repo}/state "
              f"--journal-file /var/lib/ai-trader-supervisor/trader-journal.log "
              f"--runtime-dir /var/lib/ai-trader-supervisor --python {python}\n")
    _write(runner, script.encode(), 0o755)
    installed = [runner]
    for name in ("ai-trader-supervisor.service", "ai-trader-supervisor.timer"):
        target = destination_root / "etc/systemd/system" / name
        _write(target, (repo / "ops" / name).read_bytes())
        installed.append(target)
    return installed


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
