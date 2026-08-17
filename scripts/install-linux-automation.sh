#!/usr/bin/env bash
set -euo pipefail

project=/home/ubuntu/projects/ai-trader
python=/home/ubuntu/.venvs/ai-trader/bin/python
dry_run=false
if [[ "${1:-}" == "--dry-run" ]]; then dry_run=true; fi

[[ "$(uname -s)" == Linux ]] || { echo "Linux required" >&2; exit 1; }
[[ -r /etc/os-release ]] && grep -qi ubuntu /etc/os-release || { echo "Ubuntu required" >&2; exit 1; }
[[ "$(id -un)" == ubuntu ]] || { echo "Run as ubuntu (sudo is used only to install units)" >&2; exit 1; }
[[ "$(pwd -P)" == "$project" ]] || { echo "Run from $project" >&2; exit 1; }
[[ -x "$python" ]] || { echo "Missing venv Python: $python" >&2; exit 1; }
command -v codex >/dev/null || { echo "Codex is not resolvable" >&2; exit 1; }

"$python" -m unittest discover -s tests -p 'test_*.py' -v
"$python" orchestrator.py --self-test
"$python" -m compileall -q orchestrator.py trader tests
git diff --check

if "$dry_run"; then
  echo "Dry run complete; no systemd files installed and nothing started."
  exit 0
fi

sudo install -o root -g root -m 0644 deployment/systemd/ai-trader.service /etc/systemd/system/ai-trader.service
sudo install -o root -g root -m 0644 deployment/systemd/ai-trader-maintenance.service /etc/systemd/system/ai-trader-maintenance.service
sudo install -o root -g root -m 0644 deployment/systemd/ai-trader-maintenance.timer /etc/systemd/system/ai-trader-maintenance.timer
sudo visudo -cf deployment/sudoers/ai-trader-maintenance
sudo install -o root -g root -m 0440 deployment/sudoers/ai-trader-maintenance /etc/sudoers.d/ai-trader-maintenance
sudo systemctl daemon-reload
sudo systemctl enable ai-trader.service
sudo systemctl enable ai-trader-maintenance.timer
echo "Installed and enabled for the next boot. Nothing was started."
