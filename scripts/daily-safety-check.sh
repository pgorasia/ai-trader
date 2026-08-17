#!/usr/bin/env bash
set -euo pipefail

project=/home/ubuntu/projects/ai-trader
python=/home/ubuntu/.venvs/ai-trader/bin/python
cd "$project"
test "$(stat -c '%U' .)" = ubuntu
df -P "$project"
git fsck --no-dangling
test -r /home/ubuntu/.codex/auth.json
"$python" -m compileall -q orchestrator.py trader
"$python" - <<'PY'
from datetime import datetime
from trader.market_calendar import EquityMarketCalendar, ET
from trader.state import StateStore
store = StateStore(__import__("pathlib").Path("state"))
store.all_states()
print(EquityMarketCalendar("XNYS").next_session(datetime.now(ET)))
PY
"$project/scripts/backup-safe-state.sh"
