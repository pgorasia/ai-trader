# AI-DayTrader-V1 — Live-Market Stage-B Validation V2

Date: 2026-08-12  
Mode: SHADOW MODE  
Scope: one live-market candidate-selection cycle; no brokerage writes

## Security boundary

PASS. The available Robinhood tool surface contained no:

- `place_equity_order`
- `cancel_equity_order`
- `create_scan`
- `update_scan_filters`
- `update_scan_config`
- options transaction or exercise tools

No unavailable tool was requested, enabled, or bypassed. No order-review or other write tool was used.

## Account reconciliation

Only the uniquely identified Agentic account (`agentic_allowed=true`, nickname `Agentic`) was reconciled. It had zero open equity positions and zero equity orders at cycle start. No other account information was used.

## Scanner

AI-DayTrader-V1 was run exactly once. The saved scan reported `% Change desc` ordering.

- `total_items`: **221**
- actual rows returned: **221**
- rows processed: **first 20 only**

Scanner `% Change` values below are the returned decimal ratios converted to percentages.

| # | Symbol | Scanner % change |
|---:|:---|---:|
| 1 | NBIS | 27.8865% |
| 2 | QNT | 22.6365% |
| 3 | SHAZ | 20.4312% |
| 4 | CRWV | 19.0932% |
| 5 | SMCI | 18.0854% |
| 6 | WYFI | 17.3410% |
| 7 | FCEL | 14.7396% |
| 8 | CAVA | 14.6029% |
| 9 | CIEN | 13.5912% |
| 10 | WEN | 12.6490% |
| 11 | BE | 12.5089% |
| 12 | AEHR | 11.5805% |
| 13 | QMCO | 11.3907% |
| 14 | CBRS | 11.2945% |
| 15 | EAT | 10.7688% |
| 16 | REPL | 10.6960% |
| 17 | VELO | 10.1534% |
| 18 | DOCN | 10.1109% |
| 19 | MXL | 9.3596% |
| 20 | CRDO | 9.3060% |

## Quote and instrument gate

The batched quote response covered all 20 rows. Quote timestamps were approximately 17:23:47Z–17:24:48Z; all symbols had `has_traded=true`, `state=active`, and nonzero bid/ask. The two batched tradability calls reported `tradeable=true`, individual-account tradability, and fractional tradability for all 20. No stale-quote, inactive, missing-quote, or unusable-spread rejection occurred.

Rejected as outside the V1 instrument boundary:

- **NBIS** — Robinhood instrument name is foreign `N.V. Class A Ordinary Shares`, not an explicit V1 common-stock form.
- **WYFI** — Robinhood instrument name is `Ordinary Shares`, not an explicit V1 common-stock form.
- **CRDO** — Robinhood instrument name is foreign `Holding Ltd Ordinary Shares`, not an explicit V1 common-stock form.

No ETF, warrant, preferred, or explicit ADR label appeared in the remaining rows.

## 5-minute gate

Current regular-session 5-minute OHLCV was retrieved for the 17 quote/instrument survivors. The gate emphasized persistence, higher highs/higher lows, breakout quality, volume continuity, opening-range behavior, and exhaustion.

Reduced to three candidates because they were near session highs with recent constructive structure:

- **SHAZ** — +10.29% session return; latest close 68.14 versus 68.21 session high; 8 higher-high and 6 higher-low transitions in the last 12 bars; late volume improved versus the middle of the session. Early opening volume was still a large spike.
- **SMCI** — +6.53%; latest close 37.275 versus 37.35 high; 8 higher-high and 10 higher-low transitions in the last 12 bars; late volume improved versus the middle of the session. Opening volume was exceptionally concentrated.
- **FCEL** — +9.59%; latest close 22.0499 versus 22.12 high; 6 higher-high and 7 higher-low transitions in the last 12 bars; late volume was approximately flat versus the middle of the session and the late advance was orderly.

5-minute rejects:

- **QNT** — strong move but 3.86% below its 71.4699 high; late volume contracted and the late breakout was not sustained.
- **CRWV** — opening-range spike failed; session return was -0.72%, with opening volume over 11x the later median.
- **CAVA** — only +0.80% session return and 2.85% below its high; late volume contracted materially.
- **CIEN** — positive but late volume was light and recent higher-high/higher-low persistence was weaker than the selected three.
- **WEN** — large move but 3.35% below its high after a failed continuation; volume behavior was dominated by an earlier spike.
- **BE** — opening spike faded; only +0.69% session return and nearly 5% below the high.
- **AEHR** — positive and near its high, but late volume contracted about 56% versus the middle of the session.
- **QMCO** — early volume spike dominated; late volume contracted and price was 1.12% below the high.
- **CBRS** — constructive but less persistent recent highs and materially lower late volume than the selected three.
- **EAT** — positive and near its high, but late participation was thin and declining.
- **REPL** — strong return but 3.51% below its high with roughly 50% late-volume contraction.
- **VELO** — failed breakout / bearish session structure; -11.99% from the opening print.
- **DOCN** — positive but 2.20% below its high with weak late volume and limited recent persistence.
- **MXL** — orderly but only +2.97% and late volume contracted about 56%; insufficient expansion relative to the selected three.

## Final technical gate

Final candidate count: **3 shadow candidates**. These are candidate-selection outputs only, not trade recommendations.

Robinhood rejects the literal `15minute` interval. Therefore, supported 5-minute bars were aggregated locally into 15-minute OHLCV. The final VWAP, RSI(14), and EMA(20) values below are Robinhood-computed at supported 5-minute resolution, latest bar 17:20Z.

| Symbol | Derived 15m observation | Robinhood VWAP (5m) | RSI(14) (5m) | EMA(20) (5m) | Observation |
|:---|:---|---:|---:|---:|:---|
| SHAZ | 61.78 open to 68.14 latest; successive rising 15m closes, then consolidation and renewed high | 65.6719 | 70.84 | 67.2119 | Price above VWAP and EMA; momentum strong, RSI elevated |
| SMCI | 34.99 open to 37.28 latest; opening volatility, consolidation, then late breakout toward 37.35 | 36.1249 | 72.20 | 36.4573 | Price above VWAP and EMA; strong but extended |
| FCEL | 20.12 open to 22.0499 latest; orderly stair-step advance and late high-area continuation | 20.9232 | 76.83 | 21.5834 | Price above VWAP and EMA; strongest RSI extension |

The final derived 15-minute block contains only two source 5-minute bars because the live snapshot ended before the block completed; it is flagged as partial rather than treated as a completed bar.

No entry, stop, target, position size, or trade recommendation was produced.

## Data-quality problems

- Literal `15minute` is unsupported by the Robinhood historical and indicator tools.
- The first batched 5-minute payload was too large for reliable direct inspection and required a controlled read-back.
- The latest 15-minute aggregate is partial.
- Scanner `% Change` is returned as a decimal ratio, requiring explicit percentage conversion.
- Instrument metadata does not expose a direct ADR/foreign-ordinary classification field; three conservative ordinary-share rejects were made from the returned names.

## Model/tool inefficiencies

- The initial 17-symbol 5-minute response was unnecessarily verbose for candidate selection; a compact server-side summary or first-20 projection would reduce inspection cost.
- A failed 15-minute request and two diagnostic calls were needed to discover the interval limitation.
- Technical indicators require one call per symbol/indicator; a batched symbol-plus-indicator endpoint would materially reduce latency and call count.

## Recommended Stage-B adjustments

1. Add native 15-minute aggregation and 15-minute indicator support, or expose an official aggregation endpoint with a partial-bar flag.
2. Add a scanner result limit/projection so Stage-B can receive the first 20 rows without a 221-row payload while still recording `total_items`.
3. Return normalized numeric fields alongside raw scanner values, especially percentage units.
4. Add explicit instrument subtype fields for ADR, foreign ordinary, preferred, warrant, and ETF classification.
5. Add a batched technical-indicator endpoint accepting several symbols and indicator types in one read.

## Robinhood tool-call count

Complete count: **34 Robinhood tool calls**.

- Account/scanner discovery and reconciliation: 4
- Single scanner execution: 1
- Quote and tradability gate: 3
- Initial 5-minute retrieval: 2
- Controlled 5-minute read-back: 2
- Failed literal 15-minute final attempt: 10
- Interval diagnostics: 2
- Corrected final technical gate: 10

No brokerage write or options transaction/exercise call was made.

LIVE STAGE-B V2: PASS
