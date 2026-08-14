# Stage-B Smoke Test V1

## 1. SECURITY BOUNDARY

- Operating mode: SHADOW MODE.
- Read-only pipeline test; no trade or simulated trade decision made.
- `place_equity_order`: unavailable.
- `cancel_equity_order`: unavailable.
- `create_scan`: unavailable.
- `update_scan_filters`: unavailable.
- `update_scan_config`: unavailable.
- Options execution/exercise tools: none exposed.
- No prohibited capability from the requested security list was available. No write, review, options, Sol, web/news, or agent/subagent tool was invoked.

## 2. AGENTIC ACCOUNT RECONCILIATION

Reconciled only the Robinhood account returned with `agentic_allowed=true`.

- Total equity / account value: **$100.00 USD**
- Buying power: **$100.0000 USD**
- Equity positions: **none**
- Pending/open equity orders: **none**

The other Robinhood account was not used or reported.

## 3. SCANNER RESULT COUNT

- Saved scanner: `AI-DayTrader-V1`
- Expected scan ID: `c4646257-9c99-4e35-801b-80f8b83b81ea`
- Scanner verified: **yes**
- Run count: **1**
- Total results reported by Robinhood: **0**
- Results actually processed: **0**
- Top 20 symbols returned: **none**
- Scanner percentage-change value: **not supplied for any result because the result set was empty**
- Sort reported by Robinhood: `% Change desc`

## 4. SYMBOLS PROCESSED

None. The empty scan result caused the pipeline to stop before quote retrieval.

## 5. QUOTE-GATE REJECTIONS AND REASONS

None. No symbols were returned, so no quote or tradability calls were needed.

## 6. 5-MINUTE-GATE REJECTIONS AND REASONS

None. No Stage-A survivors existed; no regular-session 5-minute OHLCV call was made.

## 7. FINAL 0–3 CANDIDATES

**0 candidates.**

## 8. TECHNICAL OBSERVATIONS

None. No candidate reached technical validation. No 15-minute OHLCV, VWAP, RSI(14), or EMA(20) calls were made.

## 9. ROBINHOOD TOOL-CALL COUNT BY TOOL

| Tool | Calls |
|---|---:|
| `get_accounts` | 1 |
| `get_portfolio` | 1 |
| `get_equity_positions` | 1 |
| `get_equity_orders` | 1 |
| `get_scans` | 1 |
| `run_scan` | 1 |
| `get_equity_quotes` | 0 |
| `get_equity_tradability` | 0 |
| `get_equity_historicals` | 0 |
| `get_equity_technical_indicators` | 0 |

## 10. STALE / MISSING / AMBIGUOUS DATA

- No stale or ambiguous quote/bar/indicator data was encountered because no symbols were returned.
- The scanner returned an empty result set; therefore no per-symbol percentage-change value was available.

## 11. ETF / NON-COMMON-EQUITY INSTRUMENTS

None encountered. The scanner returned no instruments, so instrument-type validation was not reached.

## 12. MODEL-USAGE BEHAVIOR

The pipeline avoided unnecessary usage correctly: an empty scan result short-circuited quote, tradability, OHLCV, and indicator retrieval. It also did not invoke web/news, fundamentals, Level 2, options, review, Sol, or another agent.

## 13. RECOMMENDED STAGE-B CHANGES

- Preserve the current empty-result short circuit.
- Keep explicit reporting of total scanner results versus symbols processed.
- Keep instrument-type validation before any Stage-B escalation when results are present.
- No Stage-B logic change is required based on this run.

PIPELINE SMOKE TEST: PASS
