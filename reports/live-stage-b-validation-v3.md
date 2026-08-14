# AI-DayTrader-V1 — Live Stage-B Validation V3

Date: 2026-08-12  
Mode: SHADOW MODE  
Scope: one validation cycle; no brokerage transaction authorized

## SECURITY

- `place_equity_order`: unavailable
- `cancel_equity_order`: unavailable
- `create_scan`: unavailable
- `update_scan_filters`: unavailable
- `update_scan_config`: unavailable
- Options transaction/exercise tools: none available
- No order-review call was made.
- No web/news, Level 2, fundamentals, options, Sol, or subagents were used.

Security boundary passed at the MCP tool layer.

## ACCOUNT

- Dedicated account: Agentic, account ending `0312` (full account identifier not recorded)
- Account type: cash; active; agentic access confirmed
- Equity positions before scanning: none
- Open/pending equity orders before scanning: none
- Reconciliation was limited to the dedicated Agentic account.

## SCANNER TOTAL

- Scanner: `AI-DayTrader-V1`
- Scanner execution count: exactly 1
- `total_items`: 23
- Rows returned: 23
- Processed: first 20 rows only
- Sort: `% Change desc`

## RAW 0-20 CANDIDATES

Scanner `% Change` is the scanner's change-from-close value; it is not intraday return.

| # | Symbol | Scanner % change | Scanner RVOL |
|---:|---|---:|---:|
| 1 | CRDO | 11.6315% | 1.1258 |
| 2 | REPL | 10.8425% | 1.0311 |
| 3 | VELO | 10.8108% | 1.6273 |
| 4 | MXL | 10.0985% | 1.2749 |
| 5 | TSEM | 8.2979% | 1.1370 |
| 6 | SMTC | 8.2635% | 1.6338 |
| 7 | COHU | 8.1409% | 2.6175 |
| 8 | NUAI | 7.0746% | 1.1462 |
| 9 | LPTH | 6.9519% | 1.0558 |
| 10 | FIGR | 6.8247% | 1.2822 |
| 11 | METC | 5.5828% | 1.0467 |
| 12 | AAOI | 5.3785% | 1.1356 |
| 13 | MRAM | 5.1703% | 1.2570 |
| 14 | CMPS | 5.0448% | 1.3409 |
| 15 | OCUL | 4.9693% | 1.1252 |
| 16 | MOH | 4.5132% | 1.0011 |
| 17 | DXYZ | 4.1864% | 1.2874 |
| 18 | APLD | 3.9811% | 1.0632 |
| 19 | BWIN | 3.6626% | 1.8027 |
| 20 | NG | 3.4505% | 1.0416 |

## QUOTE/TRADABILITY REJECTIONS

- `CMPS`: rejected because Robinhood identifies it as American Depository Shares (ADR/ADS class), which is outside this validation gate.
- The other 19 processed symbols had fresh, well-formed quotes, `state=active`, `has_traded=true`, `tradeable=true`, regular-session account tradability, and `fractional_tradability=tradable`.
- No explicit ETF, preferred share, warrant, unit, right, leveraged ETF, or inverse ETF appeared in the processed 20 rows.
- Ordinary Shares, Ltd, PLC, N.V., and Class A naming were not used as rejection reasons.

## 5-MINUTE REJECTIONS

Current regular-session 5-minute OHLCV was retrieved for the 19 quote/tradability survivors. These 17 symbols were not reduced to serious candidates after review of trend persistence, higher-high/higher-low behavior, failed breakouts, volume persistence, opening-spike versus sustained participation, late-session exhaustion, and distance from the session high:

`REPL`, `VELO`, `MXL`, `TSEM`, `SMTC`, `COHU`, `NUAI`, `LPTH`, `FIGR`, `METC`, `AAOI`, `MRAM`, `OCUL`, `MOH`, `DXYZ`, `APLD`, `NG`.

No final technical enrichment was requested for those 17 symbols.

## FINAL CANDIDATES: 2

All percentages below are deterministic calculations from the current regular-session 5-minute series and the current quote. For CRDO, the current quote exceeded the latest completed-bar high, so session high is the maximum of the completed-bar high and current quote.

### CRDO

- Symbol: `CRDO`
- Scanner % change: `11.6315%`
- RVOL: `1.1258`
- Gap return: `6.7827%`
- Intraday return: `4.5862%`
- Distance from session high: `0.0000%`
- Current price: `$276.62`
- VWAP: `$266.4849`
- Distance from VWAP: `3.8033%`
- RSI(14): `75.8511`
- EMA(20): `$271.5335`
- Completed derived 15-minute structure, 17:15–17:30 UTC: `O 269.58 / H 270.99 / L 269.06 / C 271.73; volume 26,021`
- Completed derived 15-minute structure, 17:30–17:45 UTC: `O 271.735 / H 276.0599 / L 271.735 / C 275.83; volume 89,471`
- Reason it survived: successive higher high/higher low structure with materially expanded latest 15-minute participation; current quote is at a new session high relative to completed bars. RSI flags extension and is treated as context.

### BWIN

- Symbol: `BWIN`
- Scanner % change: `3.6626%`
- RVOL: `1.8027`
- Gap return: `-0.0336%`
- Intraday return: `3.6639%`
- Distance from session high: `0.2910%`
- Current price: `$30.84`
- VWAP: `$30.4620`
- Distance from VWAP: `1.2408%`
- RSI(14): `58.2024`
- EMA(20): `$30.7194`
- Completed derived 15-minute structure, 17:15–17:30 UTC: `O 30.835 / H 30.835 / L 30.5138 / C 30.59; volume 34,712`
- Completed derived 15-minute structure, 17:30–17:45 UTC: `O 30.59 / H 30.86 / L 30.59 / C 30.84; volume 13,305`
- Reason it survived: positive intraday progression, price above VWAP and EMA(20), and close proximity to the session high without an extreme RSI reading. Latest participation contracted, so persistence is weaker than CRDO.

No entry, stop, target, position size, expected return, or trade recommendation is included by instruction.

## TOTAL ROBINHOOD CALLS

`16` total:

- Account/reconciliation: 4 (`get_accounts`, `get_scans`, `get_equity_positions`, `get_equity_orders`)
- Scanner execution: 1
- Quote/tradability gate: 3 (one quote batch, two tradability batches)
- 5-minute OHLCV: 2 batched calls
- Final indicators: 6 calls (VWAP, RSI(14), EMA(20) for two symbols)

## EFFICIENCY ISSUES

- None material. Batching was used wherever supported.
- The two OHLCV calls were required by the tool's 10-symbol request limit.
- No scanner rerun, exploratory interval call, duplicate OHLCV call, or unnecessary enrichment was made.

## DATA QUALITY ISSUES

- Scanner `total_items` exceeded the processed slice: 23 total, 20 processed as instructed.
- Quote timestamps were current at capture, approximately 17:48–17:49 UTC; all 20 quotes were active and traded.
- The latest completed 5-minute OHLCV bars ended at 17:45 UTC (bars beginning at 17:40). Robinhood indicator responses were timestamped 17:45 UTC, which was the current/incomplete 5-minute bar at capture; no 17:45 bar was used in a derived 15-minute candle.
- CRDO's quote was above the latest completed-bar high; the session-high calculation explicitly incorporated the current quote rather than treating the incomplete bar as a completed candle.

## PIPELINE CHANGES RECOMMENDED

- Preserve an explicit distinction between scanner change-from-close, gap return, and intraday return in all reports.
- Tag indicator timestamps as complete or partial relative to capture time; do not present partial-bar indicators as completed-bar values.
- Keep local 15-minute aggregation restricted to three completed 5-minute bars.
- Keep an explicit security-class gate for ADR/ADS, ETF, preferred, warrant, unit, and rights instruments while allowing ordinary-share and Class A naming.
- Preserve quote/tradability and account reconciliation as pre-scan gates.

LIVE STAGE-B V3: PASS
