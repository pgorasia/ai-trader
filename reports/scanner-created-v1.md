# AI-DayTrader-V1 Scanner Record

- Scanner title: `AI-DayTrader-V1`
- Scanner ID: `c4646257-9c99-4e35-801b-80f8b83b81ea`
- Creation timestamp: `2026-08-11T23:34:00.879Z`

## Exact filters

1. `filter_type: FILTER_TYPE_LAST`; `predicate: >=`; `values: ["5"]`
2. `filter_type: FILTER_TYPE_MARKET_CAP`; `predicate: >=`; `values: ["300000000"]`
3. `filter_type: FILTER_TYPE_SHARES_FLOAT`; `predicate: >=`; `values: ["10000000"]`
4. `filter_type: FILTER_TYPE_AVERAGE_VOLUME`; `predicate: >=`; `values: ["1000000"]`; `interval: "1d"`; `length: 30`
5. `filter_type: FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE`; `predicate: >=`; `values: ["0.02"]`; `interval: "1d"`; `plot: "Close"`

No instrument-type, volume, relative-volume, gap, or technical filters were added.

- Filter validation: PASS
- Sorting: configured
- Sorting field/direction: `% Change` / `desc`
- DOWNSTREAM RESULT CAP = 20 SYMBOLS
- Current market scan executed: NO
- Brokerage order capability used: NO
- Options capability used: NO
- Scanner re-read after creation and after sorting update: YES


## Percentage-unit correction

- Previous percent threshold: `2.0`
- Corrected threshold: `0.02`
- Reason: Robinhood scanner uses decimal-return units.
- Validation source: controlled live-market diagnostic.
- All other scanner configuration unchanged.

## Configuration history

### 2026-08-12 — V1.1 Shadow Mode update

- Updated only `AI-DayTrader-V1` (`scan_id: c4646257-9c99-4e35-801b-80f8b83b81ea`).
- Retained the `+2%` lower bound in decimal-return units (`0.02`).
- Introduced the `+12%` upper bound (`0.12`) to reduce extreme-mover bias.
- Introduced `FILTER_TYPE_RELATIVE_VOLUME >= 1.0` after controlled live-market testing.
- Complete filter set now contains exactly six filters; sorting remains `% Change` descending.
- All changes remain SHADOW MODE only; no scanner run, order review, trade, options action, or Sol action was performed.