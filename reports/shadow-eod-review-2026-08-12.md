# End-of-Day Shadow Evaluation — 2026-08-12

Mode: **SHADOW MODE**  
Purpose: hindsight measurement only. The three prior senior decision records were treated as immutable and were not modified.

## Scope and method

Robinhood regular-session (`bounds=regular`) 5-minute OHLCV was retrieved in one batched call for CRDO, BWIN, FIGR, SPCX, and HPE through the 16:00 EDT close. A separate batched quote read confirmed the symbols remained active and supplied the prior-session official closes; the session-close figures below are the last completed 15:55–16:00 EDT bar closes.

To measure strictly subsequent behavior, a bar was included only when its left-edge timestamp was after the senior decision timestamp. This excludes the portion of the 5-minute bar that was already forming at the decision:

- CRDO/BWIN: first included bar 14:10 EDT.
- FIGR/SPCX: first included bar 14:25 EDT.
- HPE: first included bar 15:30 EDT.

MFE and MAE use the highest high and lowest low of those completed subsequent bars. High/low times are reported at 5-minute bar resolution as the bar beginning time. No web search, Sol, Level 2, indicators, options, order review, or execution tool was used.

## Decision-by-decision results

| Decision | Symbol | Decision price | Session close | Return to close | MFE | MFE time | MAE | MAE time |
|---|---:|---:|---:|---:|---:|---|---:|---|
| #1, 14:05:18 EDT | CRDO | $276.175 | $268.200 | **-2.888%** | +0.299% ($276.9999) | 14:15 | **-3.217% ($267.290)** | 15:40 |
| #1, 14:05:07 EDT | BWIN | $30.795 | $30.995 | **+0.649%** | +1.494% ($31.255) | 15:35 | +0.016% ($30.800) | 15:00 |
| #2, 14:24:36 EDT | FIGR | $29.9997 | $30.640 | **+2.134%** | **+2.768% ($30.830)** | 15:55 | -0.432% ($29.870) | 15:00 |
| #2, 14:24:36 EDT | SPCX | $148.085 | $146.180 | **-1.286%** | +1.023% ($149.600) | 15:30 | -2.357% ($144.595) | 15:45 |
| #3, 15:27:24 EDT | HPE | $57.850 | $58.795 | **+1.634%** | +1.798% ($58.890) | 15:55 | +0.320% ($58.035) | 15:30 |

### CRDO — Decision #1

Senior concern: a vertical near-high move, a stall after the fast push, no verified current catalyst, and no defensible non-manufactured 2:1 plan.

The concern subsequently occurred. CRDO held near $276 through approximately 14:30, then broke lower: closes declined from $276.64 at 14:30 to $272.39 at 14:45 and $268.52 at 15:30. The later bars showed a sustained reversal rather than continuation; the original near-high entry would have finished down 2.888% by the close.

No new objectively confirmed long entry structure developed before 15:40. There was a short early consolidation near $276, but it did not produce a clean reclaim or breakout. The later 15:00–15:15 stabilization near $271–$273 lacked a confirmed continuation trigger and remained below the rejected high-area structure.

Classification: **GOOD AVOIDANCE**. This is based on the subsequent reversal and absence of a qualified later structure, not merely on the closing loss.

### BWIN — Decision #1

Senior concern: a low-volume stall below the $30.93 session high, declining participation, stale catalyst evidence, and inadequate room for approximately 2:1 reward/risk.

The initial stall persisted. From 14:10 through 14:45, BWIN traded in a narrow low-volume band around $30.82–$30.94. A 14:50 volume spike reached $31.03, but the following 14:55 bar closed at $30.845, so that first attempted breakout failed.

A materially new structure later became visible: a second base formed approximately 15:00–15:15 between $30.80 and $31.03, followed by a breakout above approximately $31.03 in the 15:20 bar, confirmed by its completion at approximately 15:25 EDT. Approximate trigger: **$31.04**. Approximate structural stop: **below $30.80**. A 2:1 target would have been approximately $31.52, but no objective nearby target or sufficient room to that level was established before 15:40; the subsequent high was only $31.255.

Classification: **INCONCLUSIVE**. A later base-breakout structure did form, but its low participation, limited follow-through, and unproven 2:1 profile do not support calling it a fully qualified missed trade.

### FIGR — Decision #2

Senior concern: the initial breakout had fading follow-through volume, had not reclaimed the high, lacked a fresh verified catalyst, carried next-morning earnings risk, and did not offer a non-manufactured 2:1 plan.

The initial concern occurred first. The 14:35 breakout attempt reached $30.29, but price then weakened through 14:55–15:00, including a low of $29.87. This was a failed/stalled breakout sequence and a return toward the prior range.

A new objectively identifiable structure then formed after the rejection. A base developed approximately 15:00–15:15 with a floor around $29.87–$29.89 and a ceiling around $30.10–$30.11. The breakout above that ceiling occurred in the 15:20 bar and was objectively confirmed when that bar completed at approximately **15:25 EDT**. Approximate trigger: **$30.11–$30.12**. Approximate structural stop: **$29.87–$29.89**. Using the newly formed base height, an approximate 2:1 measured objective was about **$30.55–$30.59**, which became available before 15:40; the price reached $30.68 by 15:30 and $30.74 by 15:35.

Classification: **MISSED LATER SETUP**. The original rejection remained reasonable at 14:24; the later base-breakout was materially new and became objectively identifiable only after the earlier failed attempt. This does not mean the original decision was wrong, and next-morning earnings risk remained a valid qualification concern.

### SPCX — Decision #2

Senior concern: a vertical near-high chase, approximately 5% above VWAP, with no completed consolidation or tight objective invalidation.

The original near-high setup did not continue cleanly. SPCX consolidated from approximately 14:25 through 15:20 in a wide range, roughly $147.16–$148.95, rather than extending immediately. A new base-breakout structure did form: the breakout above approximately $148.95 occurred in the 15:25 bar and was confirmed on completion at approximately **15:30 EDT**. Approximate trigger: **$148.96–$149.00**. A conservative structural stop below the completed range low would be approximately **$147.16**, implying a roughly $1.80 risk per share and an approximate 2:1 objective near $152.56. That objective was not available before 15:40; the breakout then reversed sharply, reaching $144.595 at 15:45 and closing at $146.18.

Classification: **GOOD AVOIDANCE**. A later technical breakout existed, but it did not provide a defensible 2:1 intraday plan with a tight objective stop and it failed rapidly after the breakout. The later structure is therefore recorded, but not counted as a qualified missed trade.

### HPE — Decision #3

Senior concern: the earlier base breakout was already extended, its measured move was substantially consumed, no fresh objective entry or unconsumed target existed, the tape was mixed/choppy, and only about 12.6 minutes remained before the 15:40 entry cutoff.

The breakout continued strongly after rejection. HPE remained above the breakout shelf and advanced from the decision price of $57.85 to a subsequent high of $58.89. Volume expanded again late, including 567,311 shares at 15:40 and 1,031,164 shares in the 15:55 bar.

No new entry structure formed before 15:40. The 15:30–15:35 bars were continuation bars, not a completed pullback/reclaim or new base. After 15:40, new entries were prohibited by the session rule. There is therefore no defensible post-rejection entry trigger, structural stop, or target plan to record.

Classification: **GOOD AVOIDANCE** under the system rule that a valid entry must exist before the cutoff. This is an opportunity-cost observation, not evidence that the senior layer should have chased HPE at 15:27; the price continuation was real, but a qualifying fresh entry was not.

## Classification summary

| Classification | Symbols | Count |
|---|---|---:|
| GOOD AVOIDANCE | CRDO, SPCX, HPE | **3** |
| MISSED LATER SETUP | FIGR | **1** |
| POSSIBLY OVER-CONSERVATIVE | None | **0** |
| INCONCLUSIVE | BWIN | **1** |

No symbol meets the strict **POSSIBLY OVER-CONSERVATIVE** standard. At each original cutoff, the evidence did not establish a clearly triggerable entry, objective structural stop, and approximately 2:1 reward/risk plan. HPE is the strongest continuation/opportunity-cost case, but its later move did not retroactively create an entry that was available at 15:27.

## Session summary

- Senior decisions: **3**
- Shadow trade plans produced: **0**
- No-trade decisions: **3**
- Unique senior-reviewed symbols: **5**
- GOOD AVOIDANCE: **3**
- MISSED LATER SETUP: **1**
- POSSIBLY OVER-CONSERVATIVE: **0**
- INCONCLUSIVE: **1**

Largest post-rejection MFE: **FIGR, +2.768%**, high $30.83 at 15:55 EDT.  
Largest post-rejection MAE: **CRDO, -3.217%**, low $267.29 at 15:40 EDT.

## Diagnostic observations

### Observation

The system rejected some eventual winners in this five-symbol snapshot: BWIN, FIGR, and HPE closed above their decision prices, and FIGR/HPE had meaningful later MFE. However, CRDO and SPCX closed below their decision prices, and the session sample is only five symbols from one day. This does **not** support the claim that the system systematically rejected winners.

### Observation

Late-entry and chase prevention appeared useful. CRDO’s near-high rejection was followed by a sharp reversal, SPCX’s later breakout failed into a larger selloff, and HPE had no new entry structure before the 15:40 cutoff despite continuing higher. The HPE result shows the opportunity cost of discipline, not a rule violation.

### Observation

The catalyst requirement was not shown to be too strict. HPE continued without a verified same-day catalyst, but its technical entry was already late and its target/risk definition was weak. FIGR’s later technical breakout occurred while next-morning earnings risk remained. One session cannot distinguish whether catalyst filtering improved expectancy or merely excluded a winner.

### Observation

The approximately 2:1 requirement was not shown to be unrealistic. FIGR later produced a technically measurable 2:1 structure after a new base formed; BWIN and SPCX did not establish comparable objective room with defensible stops. A larger sample is required before evaluating the requirement.

### Observation

Cooldown behaved appropriately. CRDO did not produce a material requalification event after the earlier rejection: no substantial pullback/reclaim, clean new base-breakout, newly verified catalyst, or materially changed structure was available during the cooldown/review window. The later decline further supported retaining the prior rejection without redundant senior analysis.

## Proposed future test — not an implementation change

For future shadow sessions only, continue recording whether a post-rejection base-breakout has: (a) a completed-bar confirmation time, (b) a stop anchored to a structural level rather than the breakout bar’s noise, and (c) an independently defined target that supports approximately 2:1 before the entry cutoff. Do not change thresholds or risk rules based on this single session.

## Data limitations

- OHLCV is 5-minute and bar highs/lows do not reveal the exact intrabar order of events.
- The strict measurement excludes the incomplete bar that was already forming at each decision timestamp; this is conservative and avoids attributing pre-decision movement to the post-decision result.
- Session close is the last completed regular-session 5-minute bar close, not a claim about a later after-hours print.
- No hypothetical quantity, P&L, order, or trade plan was created because the session produced zero shadow trade plans.

SHADOW EOD REVIEW COMPLETE
