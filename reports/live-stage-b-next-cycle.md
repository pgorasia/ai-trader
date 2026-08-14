# AI-DayTrader-V1 Stage-B — New Shadow Scan Cycle

- Cycle date: 2026-08-12
- Mode: SHADOW MODE
- Data snapshot: approximately 2026-08-12T18:13:40Z; regular session
- Scope: fresh cycle only; the prior frozen CRDO/BWIN senior decision was not reconsidered, modified, or evaluated
- Decision status: candidate selection only; no trade decision and no brokerage write action

## Security boundary

- `place_equity_order`: unavailable
- `cancel_equity_order`: unavailable
- Scanner modification tools: unavailable
- Options transaction/exercise tools: unavailable
- Result: security boundary intact

## Agentic-account reconciliation

Only the dedicated Agentic account was queried.

- Account: Agentic (masked identifier `••••0312`)
- Equity positions: 0
- Equity orders, including open/pending orders: 0

## Scanner execution

`AI-DayTrader-V1` was executed exactly once. It returned 33 total matches, sorted by `% Change desc`; only the first 20 were processed.

| # | Symbol | Scanner % change | Scanner RVOL |
|---:|:---|---:|---:|
| 1 | CBRS | 11.884% | 1.751 |
| 2 | VELO | 11.833% | 1.663 |
| 3 | CRDO | 11.672% | 1.217 |
| 4 | REPL | 10.733% | 1.058 |
| 5 | MXL | 10.403% | 1.373 |
| 6 | SPCX | 10.083% | 1.024 |
| 7 | COHR | 9.286% | 1.004 |
| 8 | COHU | 8.671% | 2.722 |
| 9 | FIGR | 8.226% | 1.531 |
| 10 | SMTC | 8.027% | 1.780 |
| 11 | TSEM | 7.789% | 1.186 |
| 12 | NUAI | 7.650% | 1.218 |
| 13 | METC | 7.620% | 1.196 |
| 14 | LPTH | 6.952% | 1.083 |
| 15 | MKSI | 6.203% | 1.011 |
| 16 | JACK | 5.571% | 1.097 |
| 17 | OCUL | 5.482% | 1.229 |
| 18 | MRAM | 5.444% | 1.610 |
| 19 | APLD | 5.324% | 1.119 |
| 20 | GRRR | 4.893% | 1.013 |

## Quote/tradability gate

All 20 processed symbols resolved with fresh quotes, `state=active`, `tradeable=true`, and regular-session fractional eligibility. No malformed, stale, inactive, or untradeable symbol was found. No first-20 instrument was identified as an ETF, ADR/ADS, preferred, warrant, unit, right, leveraged ETF, or inverse ETF. CRDO, TSEM, and GRRR were retained as ordinary shares under the foreign-domicile rule where applicable.

## 5-minute gate

Regular-session 5-minute OHLCV was used. The current incomplete 5-minute bar was excluded from candle-structure analysis. `HH/HL` is the fraction of consecutive bars making a higher high/higher low; `late/early` and `late/mid` are volume ratios; `break` counts late bars persisting at/above the earlier session high; `open5` and `last5` are returns over the first and last five completed bars. Distance from high uses the current quote when it exceeded the last completed-bar high.

| Symbol | Gap | Intraday | Dist. high | HH/HL | Break | Late/early | Late/mid | Open5 | Last5 | 5-minute gate result |
|:---|---:|---:|---:|:---:|---:|---:|---:|---:|---:|:---|
| CBRS | 5.99% | 5.60% | 1.08% | 56/69% | 4 | 0.19 | 0.47 | -0.74% | -0.87% | Not carried: late volume contraction and tail fade |
| VELO | 24.91% | -10.41% | 13.00% | 51/58% | 0 | 0.03 | 1.20 | -9.65% | 1.46% | Reject: opening spike reversal |
| CRDO | 6.78% | 4.61% | 0.19% | 60/58% | 8 | 0.39 | 1.09 | -2.69% | 0.27% | Carried |
| REPL | -0.22% | 10.98% | 3.54% | 51/62% | 0 | 0.26 | 1.03 | 8.59% | -0.20% | Not carried: opening spike, no late breakout |
| MXL | 6.20% | 3.87% | 0.29% | 45/62% | 5 | 0.25 | 1.39 | 1.56% | 0.21% | Not carried: weaker continuation score |
| SPCX | 1.31% | 8.62% | 0.00% | 58/65% | 0 | 0.43 | 0.86 | 0.97% | 0.22% | Carried |
| COHR | 9.11% | 0.14% | 0.84% | 55/60% | 3 | 0.20 | 1.13 | -3.55% | 0.02% | Not carried: gap stall |
| COHU | 4.37% | 4.12% | 3.45% | 49/65% | 0 | 1.32 | 0.76 | 0.73% | 0.10% | Not carried: below high with fading late volume |
| FIGR | 3.52% | 4.55% | 0.33% | 49/47% | 7 | 2.03 | 2.66 | -0.94% | 1.34% | Carried |
| SMTC | 6.57% | 1.39% | 0.37% | 51/58% | 8 | 0.61 | 1.39 | -0.60% | 0.04% | Not carried: modest intraday continuation |
| TSEM | 5.83% | 1.85% | 1.40% | 49/49% | 0 | 0.19 | 0.81 | 1.31% | -0.25% | Not carried: no breakout and fading volume |
| NUAI | 3.63% | 4.15% | 0.09% | 40/62% | 1 | 0.42 | 1.19 | 0.18% | 0.54% | Not carried: limited higher-high persistence |
| METC | 1.96% | 5.55% | 0.00% | 45/56% | 1 | 0.36 | 0.65 | 2.45% | 1.16% | Not carried: late volume decay |
| LPTH | 4.81% | 2.04% | 1.27% | 40/38% | 0 | 0.07 | 0.45 | 1.17% | 0.27% | Not carried: very thin late volume |
| MKSI | 6.29% | -0.16% | 1.45% | 44/56% | 0 | 0.59 | 2.70 | -0.02% | 0.30% | Not carried: gap stall |
| JACK | 0.00% | 5.57% | 1.49% | 44/60% | 0 | 1.10 | 1.46 | 0.84% | -0.48% | Not carried: late fade and wide range |
| OCUL | 0.10% | 5.37% | 1.29% | 49/51% | 0 | 1.80 | 1.39 | 2.25% | 0.05% | Not carried: no breakout persistence |
| MRAM | 4.26% | 1.14% | 3.11% | 47/49% | 0 | 1.07 | 6.40 | 1.98% | 0.29% | Not carried: no breakout, still off high |
| APLD | 9.06% | -3.46% | 5.39% | 44/56% | 0 | 0.15 | 1.87 | -3.41% | 1.10% | Reject: gap fade |
| GRRR | 1.39% | 3.49% | 0.41% | 49/58% | 0 | 1.37 | 4.59 | -0.31% | 1.18% | Not carried: no confirmed breakout persistence |

The three carried symbols were selected from the new 5-minute evidence: CRDO showed repeated late breakout persistence; FIGR showed the strongest volume persistence with late continuation; SPCX showed the strongest intraday continuation and remained at the current session high. This is not a trade decision.

## Final gate — carried candidates only

VWAP, RSI(14), and EMA(20) were retrieved on 5-minute data. The indicator values below are from the latest completed 5-minute bar at 18:05Z; current price is the fresh quote at approximately 18:13Z. `distance_from_vwap = (current - VWAP) / VWAP`.

| Symbol | Current | VWAP | Dist. VWAP | RSI(14) | EMA(20) | 15-minute completed structure |
|:---|---:|---:|---:|---:|---:|:---|
| CRDO | 276.695 | 267.197 | 3.56% | 73.08 | 273.067 | Last completed groups rose 269.76 → 271.73 → 275.83 → 275.94; late volume expanded, with a small near-high pause |
| FIGR | 30.130 | 29.113 | 3.49% | 78.40 | 29.655 | Completed groups advanced 29.53 → 29.56 → 29.64 → 29.765 → 29.785; late volume spike persisted but RSI indicates extension |
| SPCX | 146.670 | 140.806 | 4.16% | 63.66 | 144.362 | Completed groups advanced through 145.73, pulled back to 144.94, then recovered to 145.49; volume tapered across the late groups |

15-minute candles were derived locally from completed groups of three 5-minute candles. No literal 15-minute Robinhood interval was requested. The current incomplete 18:00–18:15 aggregate was excluded.

No web/news, fundamentals, Level 2, options, order review, scanner modification, or subagent tools were used. No order was placed, reviewed, modified, or cancelled.

NEXT STAGE-B CYCLE: PASS
