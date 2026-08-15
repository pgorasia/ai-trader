# Deterministic Unattended Shadow Research

This project runs the existing `AI-DayTrader-V1` research process as a Python-owned, fail-closed scheduler. The only implemented operating mode is `SHADOW`. It cannot review, place, modify, simulate, or cancel brokerage orders, and it never changes Codex or Robinhood MCP configuration.

Historical Markdown files already present in `reports/` are inputs and remain untouched. New run artifacts use date-first names such as `reports/2026-08-14-cycle-1.json` and `reports/2026-08-14-eod.md`. Per-day control state is atomically persisted to `state/YYYY-MM-DD.json`.

State schema v2 is strict and revisioned. There are no implicit migrations: an unsupported older or future version fails closed until a reviewed migration is implemented. Saves use optimistic revision checks in addition to the process-wide native lock, reject non-finite numbers, fsync file contents, and on POSIX fsync the containing directory after replacement. Windows replacement remains atomic where supported by the filesystem, but Python exposes no equivalent portable directory-fsync guarantee; no stronger durability claim is made. Reports use exclusive, idempotent creation and never overwrite conflicting evidence.

## Architecture

`orchestrator.py` owns the exchange calendar, deterministic scan clock, model selection, cooldowns, bounded failure/retry policy, state, frozen-plan monitoring, EOD triggering, reporting, and readiness. Codex subprocesses are bounded read-only jobs that return schema-validated JSON:

- Luna (`gpt-5.6-luna`) performs Stage-B without live web search.
- Sol (`gpt-5.6-sol`, high reasoning) runs only for a qualifying NEW or MATERIALLY_REQUALIFIED finalist and may perform targeted live web research.
- Read-only preflight, monitor-data, and EOD jobs have their own schemas. Python makes all control-flow, outcome, and readiness decisions.

All child jobs use `codex exec --sandbox read-only --output-schema ... --json --ephemeral` with local shell and multi-agent execution disabled. Foreign MCP activity is rejected. Luna and monitor invocations explicitly disable browser/search features; only Sol receives targeted `--search` access.

## Setup

The production launcher uses the existing WSL environment. From WSL:

```powershell
source ~/.venvs/ai-trader/bin/activate
python -m pip install -r requirements.txt
python orchestrator.py --self-test
```

The configured calendar is XNYS (`exchange-calendars`) in `America/New_York`. Holidays and early closes come from that calendar. On a normal session, Python schedules 10-minute morning cycles, 25-minute midday cycles, and 10-minute afternoon cycles; the latest entry, flat, and EOD times are adjusted earlier when the exchange closes early.

## Commands

```text
python orchestrator.py --self-test   Offline unit/config/schema/schedule/state tests only
python orchestrator.py --preflight   Live read-only MCP/tool/account security audit
python orchestrator.py --once        One permitted Stage-B cycle; Sol only if qualified
python orchestrator.py --run-session Full Python-owned session schedule
python orchestrator.py --eod         EOD-only processing for today's existing state
python orchestrator.py --status      Local state, cooldown, usage, and readiness summary
```

`--once`, `--run-session`, `--eod`, and `--preflight` may contact Codex/Robinhood. Tests never do. A timeout is not retried. Only clearly transient read-only failures (for example a service-unavailable or connection-reset response) receive the configured single retry. Any malformed output, MCP/OAuth failure, ambiguous account, unexpected real position/order, forbidden tool exposure, corrupted state, or required research failure stops the affected operation safely and writes an alert under `logs/alerts/`.

## Accelerated paired Shadow experiment

Luna may nominate up to four serious finalists. Sol evaluates them independently in ranked order. The first accepted plan is the primary isolated-$100 observation; up to three later accepted plans are research-only challengers and can never be interpreted as simultaneous deployable positions.

Every accepted plan is persisted immediately as `original_plan` and evaluated from the exact same entry under two exits: `FIXED_TARGET` and `TRAILING_STOP`. The trailing variant activates only after a completed 5-minute close reaches +1R, trails the lowest low of the configured completed-bar lookback, never moves downward, and applies an updated stop only to later bars. Python never changes the original entry, initial stop, target, thesis, or timestamps. Ambiguous 5-minute ordering is recorded as `AMBIGUOUS`, never guessed.

The first 15 completed sessions are development. If at least eight paired outcomes exist, Python freezes the better stress-cost exit policy using only those observations. The next 15 completed sessions are out-of-sample validation; later data cannot change the frozen selection. Insufficient development pairs extend development rather than weakening the evidence gate.

## Readiness

Readiness is deterministic and configurable in `config/strategy.yaml`. The accelerated gate requires at least 15 development sessions, a frozen exit selection, 15 fresh validation sessions, at least eight primary validation trades, positive stress-cost expectancy and its one-sided 95% lower bound, profit factor above 1.2, acceptable drawdown, non-concentrated profits, complete SPY/QQQ benchmarks, and zero security or unresolved reconciliation failures. Completing 30 sessions does not guarantee approval: failed evidence means `CONTINUE_SHADOW`. Passing produces only `READY_FOR_APPROVAL_REVIEW`; it never activates live mode or changes permissions.

## Windows wake task

Open PowerShell once as Administrator, change to this repository, and run `powershell -ExecutionPolicy Bypass -File .\scripts\install-scheduler.ps1`. The installed S4U task can run while the user is logged off, wakes a sleeping PC, starts the WSL virtual environment, and writes a dated local log. The PC must remain powered or sleeping, not shut down. Verify it with `powershell -ExecutionPolicy Bypass -File .\scripts\check-scheduler.ps1`.

Operational state, logs, and date-based reports stay out of Git because they can contain account equity and proprietary trading evidence. Git remains the reviewed source of truth for code, prompts, schemas, and configuration. A `major-decision-required` report is created only after validation completes; no automated process can enable live trading, short selling, or broader permissions.

`scripts/install-scheduler.ps1` defines—but does not install until manually run—a conservative 05:00-local weekday wake task. It performs no one-time Eastern/local DST conversion, uses Task Scheduler's `IgnoreNew` overlap policy, and only wakes `scripts/start-shadow.ps1`. The process-wide lock is the authoritative overlap boundary; Python's trusted clock and XNYS calendar remain authoritative for exchange days, DST, early closes, scan slots, cutoffs, and EOD timing.
