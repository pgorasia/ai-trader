# Relative Volume Diagnostic V1

Date: 2026-08-12  
Mode: SHADOW MODE  
Scan: `AI-DayTrader-Diagnostic-V1`  
Scan ID: `23d29507-a0bb-45c3-aceb-320580cd9696`

Only the diagnostic scan was modified. `AI-DayTrader-V1` was not modified. All scanner results below are live results captured during this diagnostic.

## Tests

Base filters used in every test:

- `FILTER_TYPE_LAST >= 5`
- `FILTER_TYPE_MARKET_CAP >= 300000000`
- `FILTER_TYPE_SHARES_FLOAT >= 10000000`
- `FILTER_TYPE_AVERAGE_VOLUME >= 1000000`, interval `1d`, length `30`
- `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE BETWEEN ["0.02","0.12"]`, interval `1d`, plot `Close`

| TEST | RVOL THRESHOLD | TOTAL ITEMS | ROWS RETURNED |
|---|---:|---:|---:|
| A | none | 216 | 200 |
| B | >= 0.5 | 133 | 133 |
| C | >= 1.0 | 22 | 22 |
| D | >= 1.5 | 4 | 4 |
| E | >= 2.0 | 1 | 1 |

Test A returned 200 rows against 216 total matches, indicating the scanner result-row limit was reached. Tests B–E returned all matching rows.

### First 10 symbols and scanner values

Values are shown as `SYMBOL: scanner RVOL / scanner percentage change`; percentage change is the scanner decimal value, with the percentage equivalent in parentheses where useful.

- **A — none:** `NVDA: 0.665384 / 0.029839 (2.98%)`; `SPCX: 0.926471 / 0.087628 (8.76%)`; `MU: 0.814079 / 0.072790 (7.28%)`; `AMD: 0.644874 / 0.027861 (2.79%)`; `INTC: 0.513781 / 0.045338 (4.53%)`; `CSCO: 0.681583 / 0.021838 (2.18%)`; `ORCL: 0.898909 / 0.050350 (5.04%)`; `AMAT: 0.451557 / 0.049875 (4.99%)`; `LRCX: 0.475110 / 0.055746 (5.57%)`; `CAT: 0.667334 / 0.029726 (2.97%)`.
- **B — >= 0.5:** `NVDA: 0.665527 / 0.029839 (2.98%)`; `SPCX: 0.926719 / 0.087591 (8.76%)`; `MU: 0.814253 / 0.072957 (7.30%)`; `AMD: 0.645176 / 0.027850 (2.79%)`; `INTC: 0.514008 / 0.045241 (4.52%)`; `CSCO: 0.681722 / 0.021795 (2.18%)`; `ORCL: 0.899199 / 0.050179 (5.02%)`; `CAT: 0.667438 / 0.029726 (2.97%)`; `DELL: 0.542829 / 0.067170 (6.72%)`; `ANET: 0.779799 / 0.056703 (5.67%)`.
- **C — >= 1.0:** `CRDO: 1.057612 / 0.109169 (10.92%)`; `WEC: 1.238795 / 0.023481 (2.35%)`; `TSEM: 1.102136 / 0.076208 (7.62%)`; `LNT: 1.138221 / 0.023622 (2.36%)`; `SMTC: 1.581967 / 0.077794 (7.78%)`; `AAOI: 1.120815 / 0.046676 (4.67%)`; `APLD: 1.049974 / 0.037723 (3.77%)`; `WULF: 1.013430 / 0.020000 (2.00%)`; `MXL: 1.221973 / 0.097364 (9.74%)`; `FIGR: 1.259587 / 0.071300 (7.13%)`.
- **D — >= 1.5:** `SMTC: 1.582044 / 0.077794 (7.78%)`; `BWIN: 1.777450 / 0.034946 (3.49%)`; `COHU: 2.590963 / 0.086142 (8.61%)`; `VELO: 1.618842 / 0.100804 (10.08%)`.
- **E — >= 2.0:** `COHU: 2.590963 / 0.086142 (8.61%)`.

No explicit `ETF`, `ADR`, `Preferred`, `Warrant`, `Unit`, or `Right` label appeared in the returned rows. No issuer was rejected solely for an `Ltd`, `PLC`, `N.V.`, ordinary-share, or class-share naming form.

## SAMPLE VALIDATION

Sample selected from Test C (`RVOL >= 1.0`), near the threshold: CRDO, APLD, and WULF. Current cumulative volume was summed from regular-session 5-minute bars for 2026-08-12 through the live data timestamp near 17:40 UTC. The 30-day average uses the 30 completed regular daily sessions from 2026-06-29 through 2026-08-10.

| SYMBOL | ROBINHOOD RVOL | CURRENT CUMULATIVE VOLUME | 30-DAY AVG DAILY VOLUME | SIMPLE VOLUME RATIO |
|---|---:|---:|---:|---:|
| CRDO | 1.057612 | 1,049,974 | 6,154,304.53 | 0.170608 |
| APLD | 1.049974 | 6,741,171 | 19,677,642.53 | 0.342580 |
| WULF | 1.013430 | 11,481,553 | 36,258,484.37 | 0.316658 |

The simple ratios are materially below Robinhood's scanner RVOL values during the partial session. The observed behavior is consistent with a time-of-day-normalized relative-volume measure rather than the simple cumulative-volume / full-session-average calculation above. Robinhood's exact formula was not inferred.

## Conclusions

**RVOL SEMANTICS:** Robinhood's `FILTER_TYPE_RELATIVE_VOLUME` does not appear to equal the simple cumulative-volume ratio tested here. In this sample it behaves consistently with time-of-day normalization, but this diagnostic does not establish the exact formula.

**BEST V1 THRESHOLD:** `FILTER_TYPE_RELATIVE_VOLUME >= 1.0`.

**EXPECTED CANDIDATE COUNT:** Approximately 22 matches under this live snapshot, within the preferred 15–60 Stage-A range.

**PRODUCTION RECOMMENDATION:** Treat `RVOL >= 1.0` as the initial Stage-A reduction candidate for further controlled validation. Do not automatically use `>= 1.5` or `>= 2.0`; those produced only 4 and 1 matches here and risk starving discovery. Keep the diagnostic interpretation separate from any simple-volume-ratio calculation, and leave `AI-DayTrader-V1` unchanged until separately approved.

**SECURITY BOUNDARY:** PASS — `place_equity_order` unavailable; `cancel_equity_order` unavailable; no options transaction or exercise tools available. No brokerage order, cancellation, option action, web/news lookup, technical-indicator call, Level 2 call, or production-scan modification was performed.

