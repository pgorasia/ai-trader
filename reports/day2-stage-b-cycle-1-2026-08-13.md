# DAY-2 LIVE SHADOW SCAN — CYCLE 1

- Date: 2026-08-13
- Mode: SHADOW MODE
- No brokerage transaction authorized, requested, or attempted.
- Strategy: frozen AI-DayTrader-V1

## Security boundary

- `place_equity_order`: unavailable
- `cancel_equity_order`: unavailable
- Scanner modification tools: unavailable
- Options transaction/exercise tools: unavailable
- Result: security boundary intact.

## Agentic-account reconciliation

Only the dedicated Agentic account was queried. The account identifier is masked as `••••0312`.

- Account value: $100 USD
- Buying power: $100 USD
- Equity positions: none
- Open/pending equity orders: none
- Hypothetical Shadow position active: no

## Stage A — scanner

`AI-DayTrader-V1` was executed exactly once. It returned 21 matches; only the first 20 were processed, in scanner order.

| # | Symbol | Scanner % change | Scanner RVOL |
|---:|:---|---:|---:|
| 1 | MWH | 9.4716% | 1.0857 |
| 2 | BLSH | 8.0187% | 1.7765 |
| 3 | ZIM | 7.0044% | 3.1759 |
| 4 | HLIT | 6.9167% | 1.5544 |
| 5 | XE | 6.3882% | 1.3216 |
| 6 | ABTC | 5.9885% | 1.1260 |
| 7 | GDS | 5.7728% | 1.3077 |
| 8 | ACDC | 5.4475% | 1.0010 |
| 9 | TRVI | 4.2301% | 1.0483 |
| 10 | NFLX | 4.0561% | 1.0518 |
| 11 | KURA | 3.9738% | 2.5796 |
| 12 | SGI | 3.8599% | 1.3046 |
| 13 | IREN | 3.7898% | 1.1283 |
| 14 | HPQ | 3.56899% | 1.1848 |
| 15 | POET | 3.5513% | 1.2021 |
| 16 | UGP | 3.3333% | 1.1714 |
| 17 | AMBP | 3.2609% | 1.5480 |
| 18 | LINE | 3.2048% | 1.0359 |
| 19 | BBNX | 3.1737% | 1.0416 |
| 20 | POR | 2.9098% | 1.1908 |

### Quote, tradability, and instrument-class gates

All 20 processed symbols returned active, traded quotes and account-specific regular-session tradability. All returned `tradeable=true`, individual-account tradability, and fractional tradability.

`GDS` was rejected because Robinhood identifies it as an ADS. No other first-20 symbol was rejected solely because of foreign domicile or an ordinary-share naming convention. No ETF, preferred, warrant, unit, right, leveraged ETF, or inverse ETF was carried forward.

## Stage B — completed 5-minute structure

Regular-session 5-minute OHLCV was used. The latest forming bar was excluded. Completed 15-minute structure below was derived locally from groups of three completed 5-minute bars; no literal 15-minute request was made.

The three technical finalists were MWH, TRVI, and ZIM. The remaining eligible symbols were not carried because of opening-gap reversal, failure to hold the high, late-volume decay, or lack of a confirmed breakout:

- BLSH: intraday high was not reclaimed; approximately 5.8% below the completed session high and late volume was about 0.70x the preceding comparison window.
- HLIT, XE, KURA, IREN, and HPQ: opening-gap reversal or substantial intraday giveback.
- ABTC, NFLX, SGI, POET, UGP, AMBP, LINE, BBNX, and POR: no sufficiently clean breakout/pullback-reclaim structure with persistent late sponsorship.
- ACDC: gap reversal despite a late-volume burst.

### Finalist structure

| Symbol | Gap return | Intraday return | Distance from high | Recent-volume persistence | Structure assessment |
|:---|---:|---:|---:|---:|:---|
| MWH | +5.88% | +2.76% | 1.86% | 4.01x prior comparison window | Early gap continuation and higher structure; late high-volume push reached 33.27 but price settled back at 32.74, below EMA(20). Continuation was not confirmed. |
| TRVI | +1.47% | +2.45% | 0.70% | 0.67x prior comparison window | Higher-high/higher-low early structure and a test of 18.56; the latest completed groups faded to 18.43 while volume contracted after the spike. Near-high stall, not a confirmed breakout. |
| ZIM | +4.55% | +2.54% | 0.59% | 0.73x prior comparison window | Gap held and price stayed near 27.00–27.25, but the latest groups were flat-to-soft with declining volume. No objective late breakout or reclaim. |

The latest completed locally derived 15-minute groups were:

- MWH: 32.635→32.760, 32.810→32.920, 32.920→32.935, 32.920→32.930; the last group volume expanded to 96,028 but did not produce a new close high.
- TRVI: 18.390→18.435, 18.435→18.500, 18.500→18.480, 18.4899→18.430; the 18.56 high was not reclaimed.
- ZIM: 27.110→27.125, 27.130→27.085, 27.0841→27.0001, 27.030→27.040; no sustained upside expansion.

## Market regime

SPY and QQQ were both above their prior closes and their session opens, with prices near their completed-session highs:

- SPY: $776.91 versus $772.49 prior close; session open $774.86; completed-session high $779.37.
- QQQ: $732.91 versus $723.70 prior close; session open $725.13; completed-session high $733.755.

Classification: **TRENDING BULLISH**, with positive broad-market alignment. This did not override the finalists' individual structure and volume weaknesses.

## Final Luna gate

Indicator values use Robinhood's latest returned completed 5-minute indicator bar, beginning 2026-08-13T17:25:00Z. `distance_from_vwap = (current price - VWAP) / VWAP`.

| Symbol | Current price | VWAP | Distance from VWAP | RSI(14) | EMA(20) | Cooldown classification | Luna result |
|:---|---:|---:|---:|---:|---:|:---|:---|
| MWH | $32.74 | $32.4682 | +0.84% | 58.56 | $32.7734 | NEW | Reject: price is below EMA(20), 1.86% below the session high, and the late push did not hold. |
| TRVI | $18.43 | $18.1209 | +1.71% | 57.33 | $18.4101 | NEW | Reject: only marginally above EMA(20), below the 18.56 high, and recent volume faded after the high test. |
| ZIM | $27.09 | $26.9471 | +0.53% | 51.37 | $27.0595 | NEW | Reject: price is only slightly above EMA(20), RSI is neutral, and the late structure did not confirm a breakout. |

No finalist was previously rejected in the available senior-decision reports. Therefore each finalist is classified exactly as `NEW`; none is `MATERIALLY REQUALIFIED`, `COOLDOWN`, or `PREVIOUSLY REJECTED — NO MATERIAL CHANGE`.

## Decision

No serious candidate produced a complete, objective, non-chasing entry with sufficient continuation evidence for Sol escalation. No catalyst, web/news, Level 2, fundamentals, order review, options, or subagent tools were used in this Luna cycle.

SOL ESCALATION: NO
