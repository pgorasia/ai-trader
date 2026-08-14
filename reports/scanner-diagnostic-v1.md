# Scanner Diagnostic V1

## Scope

- Mode: SHADOW MODE
- Production scanner: `AI-DayTrader-V1` (untouched)
- Diagnostic scanner: `AI-DayTrader-Diagnostic-V1`
- Diagnostic scan ID: `23d29507-a0bb-45c3-aceb-320580cd9696`
- Procedure: `update_scan_filters` replaced the complete temporary filter set at each stage; each stage was evaluated exactly once.
- No presets were used during the staged diagnostic.
- No quote, candle, technical, news, Level 2, fundamental, or trading analysis was performed.
- The diagnostic scanner was retained unchanged after testing.

## Stage results

| Stage | Exact filters | `total_items` | Rows returned | Sample symbols |
|---|---|---:|---:|---|
| 1 | `FILTER_TYPE_LAST >= 5` | 393 | 200 | NVDA, AAPL, GOOG, GOOGL, MSFT, AMZN, AVGO, TSM, SPCX, META |
| 2 | `FILTER_TYPE_LAST >= 5`; `FILTER_TYPE_MARKET_CAP >= 300000000` | 393 | 200 | NVDA, AAPL, GOOG, GOOGL, MSFT, AMZN, AVGO, TSM, SPCX, META |
| 3 | Stage 2 + `FILTER_TYPE_SHARES_FLOAT >= 10000000` | 392 | 200 | NVDA, AAPL, GOOGL, MSFT, AMZN, AVGO, TSM, SPCX, META, TSLA |
| 4 | Stage 3 + `FILTER_TYPE_AVERAGE_VOLUME >= 1000000`; `interval=1d`; `length=30` | 394 | 200 | NVDA, AAPL, GOOGL, MSFT, AMZN, AVGO, TSM, SPCX, META, TSLA |
| 5 | Stage 4 + `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE >= 2.0`; `interval=1d`; `plot=Close` | 0 | 0 | None |

## Determination

1. Stage 5 is the first stage where `total_items` becomes zero, falling from 394 at Stage 4 to 0.
2. The newly added filter causing that change is `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE >= 2.0` with `interval=1d` and `plot=Close`.
3. Stage 5 exactly reproduces the five production filters and the production scanner's zero-result output.
4. The diagnostic establishes which filter triggers the empty result, but does not distinguish expected market conditions, an overly restrictive threshold, missing scanner data, or misunderstood filter semantics.

## Restrictions honored

- No orders were placed, modified, or cancelled.
- No options tools or order-review tools were used.
- AI-DayTrader-V1 was not modified.
- Thresholds were not changed.
- No relative volume, current volume, gap, instrument type, or technical-indicator filters were added.
- No stage was rerun because its output was surprising.
