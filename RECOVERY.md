# AI Trader VM Recovery

This recovery keeps the trader in SHADOW mode. Do not restore credentials from a
state backup and do not enable brokerage write tools.

1. Build an Ubuntu ARM64 VM and create the `ubuntu` user.
2. Clone the private repository to `/home/ubuntu/projects/ai-trader` and check
   out the last known-good commit.
3. Create `/home/ubuntu/.venvs/ai-trader`, install `requirements.txt`, and
   install/verify the Codex CLI.
4. Restore the newest safe-state archive into the project. Archives contain only
   `state/`, `reports/`, and valuable `logs/`; inspect permissions afterward.
5. Authenticate Codex interactively as `ubuntu`. Restore Robinhood OAuth through
   its normal trusted setup separately; never copy it from backups.
6. Verify the active Codex configuration still exposes exactly the approved
   read-only 22-tool Robinhood Shadow boundary.
7. Run `./scripts/install-linux-automation.sh --dry-run`, then
   `./scripts/install-linux-automation.sh`. Reboot or explicitly start the
   service only when ready.
8. Verify `systemctl status ai-trader.service`, then run
   `/home/ubuntu/.venvs/ai-trader/bin/python orchestrator.py --health-check`.

If a deployment fails, maintenance records the old Git commit, restores it with
an atomic Git reset, and restarts that known-good version. Historical reports
must never be rewritten during recovery.

Routine maintenance is deterministic and token-free. The nightly timer runs the
local controller; it does not schedule a Codex review. A Codex repair is possible
only when an approved persisted trigger passes SHADOW mode, market-window,
fingerprint, evidence-change, and cooldown gates. Its temporary Codex home has no
MCP servers, and its separate worktree is removed after validation or rejection.
