# Deterministic Unattended Shadow Research

This project runs the existing `AI-DayTrader-V1` research process as a Python-owned, fail-closed scheduler. The only implemented operating mode is `SHADOW`. It cannot review, place, modify, simulate, or cancel brokerage orders, and it never changes Codex or Robinhood MCP configuration.

Historical Markdown files already present in `reports/` are inputs and remain untouched. New run artifacts use date-first names such as `reports/2026-08-14-cycle-1.json` and `reports/2026-08-14-eod.md`. Per-day control state is atomically persisted to `state/YYYY-MM-DD.json`.

## Architecture

`orchestrator.py` owns the exchange calendar, deterministic scan clock, model selection, cooldowns, bounded failure/retry policy, state, frozen-plan monitoring, EOD triggering, reporting, and readiness. Codex subprocesses are bounded read-only jobs that return schema-validated JSON:

- Luna (`gpt-5.6-luna`) performs Stage-B without live web search.
- Sol (`gpt-5.6-sol`, high reasoning) runs only for a qualifying NEW or MATERIALLY_REQUALIFIED finalist and may perform targeted live web research.
- Read-only preflight, monitor-data, and EOD jobs have their own schemas. Python makes all control-flow, outcome, and readiness decisions.

All child jobs use `codex exec --sandbox read-only --output-schema ... --json --ephemeral`. No `danger-full-access`, bypass, or yolo flag is used. Luna and monitor invocations explicitly disable browser/search features; only Sol receives `--search`.

## Setup

From PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
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

## Frozen Shadow plans

A Sol plan is persisted immediately as `original_plan`; later observations live only under `outcome`. Python never changes the original entry, stop, targets, thesis, or timestamps. Only fully completed 5-minute bars are used. If the same bar can contain both stop and target, or an entry-cross and stop whose order is unknowable, the result is explicitly `AMBIGUOUS` and excluded from completed-trade statistics. New entries are not inferred from a bar beginning at or after the cutoff.

## Readiness

Readiness is deterministic and configurable in `config/strategy.yaml`. `READY_FOR_APPROVAL_REVIEW` requires zero security violations and unresolved failures, at least 10 sessions and 20 completed unambiguous Shadow trades, positive cost-adjusted expectancy, profit factor above 1, acceptable drawdown, and non-concentrated profits. It is only a review status: it never activates Approval/Live mode or changes permissions.

## Windows wake task

`scripts/install-scheduler.ps1` defines—but does not install until manually run—a weekday wake task. It converts 09:25 America/New_York to the machine's local time at installation. Task Scheduler only wakes `scripts/start-shadow.ps1`; Python remains authoritative for actual exchange days and all strategy timing. On systems whose daylight-saving transitions differ from New York, reinstall or update the wake trigger when offsets change.
