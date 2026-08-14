# AI-DayTrader-V1 Scanner Design

Read-only design exercise. No market scan, scanner write, watchlist write, order review, simulation, or trade action was performed.

Evidence collected from Robinhood Trading MCP:

- `get_scanner_filter_specs` returned 56 filter types: 16 fundamental, 13 option, 13 price/volume, and 14 technical.
- `get_scans` returned no saved scanners, so there was no existing suitable scanner to duplicate.
- The specification response exposes filter enums, units, value types, predicates, intervals, lengths, and plots. It does not expose a complete sort-field catalog, sort directions, configurable result limits, presets, column configuration, or OR/union semantics.

## 1. Supported Robinhood filters relevant to this strategy

The following are supported filter types and exact schema capabilities. Thresholds below are proposed design values, not current-market observations.

| Filter enum | Meaning | Supported schema | Proposed use |
|---|---|---|---|
| `FILTER_TYPE_INSTRUMENT_TYPE` | Asset class | `STRING`; `=` or `ANY_OF` | `=` `STOCK` — the value domain is not enumerated by the spec endpoint and must be validated before creation |
| `FILTER_TYPE_LAST` | Last price | Dollar decimal; `>`, `>=`, `<`, `<=`, `BETWEEN`, `OUTSIDE` | `>= 5.00` |
| `FILTER_TYPE_MARKET_CAP` | Market capitalization | Dollar integer; `>`, `>=`, `<`, `<=`, `BETWEEN`, `OUTSIDE` | `>= 300,000,000` |
| `FILTER_TYPE_SHARES_FLOAT` | Public float | Plain integer shares; `>`, `>=`, `<`, `<=`, `BETWEEN`, `OUTSIDE` | `>= 10,000,000` |
| `FILTER_TYPE_AVERAGE_VOLUME` | Historical average share volume | Plain integer; lengths `1,2,4,5,10,14,15,30,60,90`; intervals `1m,1h,1d,1w,1mo`; numeric predicates `>`, `>=`, `<`, `<=`, `BETWEEN`, `OUTSIDE` | `>= 1,000,000`, interval `1d`, length `30` |
| `FILTER_TYPE_VOLUME` | Volume for the selected interval | Plain integer; intervals `1m,5m,15m,30m,1h,4h,1d`; numeric predicates above | `>= 250,000`, interval `1d` |
| `FILTER_TYPE_RELATIVE_VOLUME` | Current volume relative to historical volume | Plain decimal; lengths `1,2,4,5,10,14,15,30,60,90`; intervals `1m,1h,1d,1w,1mo`; numeric predicates above | `>= 2.0`, interval `1d`, length `30` |
| `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE` | Percentage price change | Percentage decimal; intervals `1m,5m,15m,30m,1h,4h,1d,1w,1mo`; plots `High,Low,Open,Close`; numeric predicates above | `>= 3.0`, interval `1d`, plot `Close` |
| `FILTER_TYPE_GAP` | Gap measure | Percentage decimal; intervals `1m,5m,15m,30m,1h,4h,1d,1w,1mo`; numeric predicates above | Available as an optional catalyst variant: `>= 2.0`, interval `1d`; omitted from the core AND-filter set |
| `FILTER_TYPE_VWAP` | VWAP indicator value | Plain decimal; intervals `1m,5m,15m,30m,1h,4h,1d,1w,1mo`; `>`, `>=`, `<`, `<=`, `BETWEEN`, `OUTSIDE` | Defer to Stage B; the spec does not establish a cross-field “price above VWAP” relationship |
| `FILTER_TYPE_AVERAGE_TRUE_RANGE` | ATR | Plain decimal; lengths `10,14,20,25`; intervals `1m,5m,15m,30m,1h,4h,1d,1w,1mo`; numeric predicates above | Available, but defer to Stage B so the scanner remains a cheap universe reducer |
| `FILTER_TYPE_AVERAGE_DIRECTIONAL_INDEX` | ADX | Plain decimal; lengths `9,10,14,20,25,50,100`; intervals `1m,5m,15m,30m,1h,4h,1d,1w,1mo`; numeric predicates above | Available, but defer to Stage B |

Other supported technical filters—`FILTER_TYPE_EMA`, `FILTER_TYPE_MACD`, `FILTER_TYPE_RSI`, `FILTER_TYPE_BOLLINGER_BAND`, `FILTER_TYPE_AROON_INDICATOR`, `FILTER_TYPE_AROON_OSCILLATOR`, `FILTER_TYPE_COMMODITY_CHANNEL_INDEX`, `FILTER_TYPE_STOCHASTIC_OSCILLATOR`, `FILTER_TYPE_SUPPORT`, `FILTER_TYPE_RESISTANCE`, and `FILTER_TYPE_WILLIAMS_PERCENT_RANGE`—are available, but are intentionally not required in V1. They are better used for batched follow-up validation than for encoding the complete trading strategy into the scanner.

### Complete filter-type inventory returned by the specification endpoint

- Fundamental: `FILTER_TYPE_INSTRUMENT_TYPE`, `FILTER_TYPE_EPS`, `FILTER_TYPE_EARNINGS_DATE`, `FILTER_TYPE_EX_DIVIDEND_DATE`, `FILTER_TYPE_SHARES_FLOAT`, `FILTER_TYPE_FORWARD_PE`, `FILTER_TYPE_GROSS_MARGIN`, `FILTER_TYPE_MARKET_CAP`, `FILTER_TYPE_NET_PROFIT_MARGIN`, `FILTER_TYPE_OPERATING_MARGIN`, `FILTER_TYPE_PE`, `FILTER_TYPE_PEG`, `FILTER_TYPE_RETURN_ON_ASSETS`, `FILTER_TYPE_RETURN_ON_EQUITY`, `FILTER_TYPE_SECTOR`, `FILTER_TYPE_SHARES_OUTSTANDING`.
- Option: `FILTER_TYPE_AVERAGE_CALL_VOLUME`, `FILTER_TYPE_AVERAGE_OPTIONS_VOLUME`, `FILTER_TYPE_AVERAGE_PUT_VOLUME`, `FILTER_TYPE_HISTORICAL_VOLATILITY`, `FILTER_TYPE_IMPLIED_VOLATILITY`, `FILTER_TYPE_TOTAL_OPEN_INTEREST`, `FILTER_TYPE_OPEN_INTEREST_VOLUME`, `FILTER_TYPE_TOTAL_OPTIONS_VOLUME`, `FILTER_TYPE_RELATIVE_OPTIONS_VOLUME`, `FILTER_TYPE_TOTAL_CALL_OPEN_INTEREST`, `FILTER_TYPE_TOTAL_CALL_VOLUME`, `FILTER_TYPE_TOTAL_PUT_OPEN_INTEREST`, `FILTER_TYPE_TOTAL_PUT_VOLUME`.
- Price/volume: `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE`, `FILTER_TYPE_ASK_PRICE`, `FILTER_TYPE_AVERAGE_VOLUME`, `FILTER_TYPE_BID_PRICE`, `FILTER_TYPE_CLOSE`, `FILTER_TYPE_GAP`, `FILTER_TYPE_HIGH`, `FILTER_TYPE_LAST`, `FILTER_TYPE_LOW`, `FILTER_TYPE_DOLLAR_CHANGE_FROM_CLOSE`, `FILTER_TYPE_OPEN`, `FILTER_TYPE_RELATIVE_VOLUME`, `FILTER_TYPE_VOLUME`.
- Technical: `FILTER_TYPE_AROON_INDICATOR`, `FILTER_TYPE_AROON_OSCILLATOR`, `FILTER_TYPE_AVERAGE_DIRECTIONAL_INDEX`, `FILTER_TYPE_AVERAGE_TRUE_RANGE`, `FILTER_TYPE_BOLLINGER_BAND`, `FILTER_TYPE_COMMODITY_CHANNEL_INDEX`, `FILTER_TYPE_EMA`, `FILTER_TYPE_MACD`, `FILTER_TYPE_RSI`, `FILTER_TYPE_RESISTANCE`, `FILTER_TYPE_STOCHASTIC_OSCILLATOR`, `FILTER_TYPE_SUPPORT`, `FILTER_TYPE_VWAP`, `FILTER_TYPE_WILLIAMS_PERCENT_RANGE`.

## 2. Unsupported or unverified desired filters

Robinhood does not directly expose these as reliable scanner filters in the inspected specification:

- U.S. exchange/listing country, common-stock subtype, or exchange membership. `Asset type` is the closest available filter; Stage B must verify instrument/tradability data.
- A penny-stock flag, pump-and-dump flag, halt flag, or direct low-float-risk classification. Price, market cap, float, and volume are only proxies.
- Dollar volume, average dollar volume, bid/ask spread, book depth, turnover, or a direct liquidity score.
- Verified current news, filing, earnings surprise, guidance, contract, regulatory, or other catalyst freshness/significance. `Earnings date` exists, but is not a catalyst-verification filter.
- Opening-range behavior, relative strength versus SPY/QQQ/sector, market-regime alignment, or a guaranteed “price above/below VWAP” relationship.
- A documented OR/union rule for combining alternative setups. The design therefore uses one conservative AND-filter set and keeps gap behavior optional.
- A complete supported sort-field list, sort-direction list, configurable result-limit field, preset list, or column-configuration schema. These were not returned by `get_scanner_filter_specs`, and no saved scan existed from which to infer them.

## 3. Proposed `AI-DayTrader-V1` configuration

This is a design proposal only; it was not created.

### Exact core filters

All filters below are intended to be ANDed:

```text
FILTER_TYPE_INSTRUMENT_TYPE        =        ["STOCK"]
FILTER_TYPE_LAST                    >=       ["5.00"]
FILTER_TYPE_MARKET_CAP              >=       ["300000000"]
FILTER_TYPE_SHARES_FLOAT            >=       ["10000000"]
FILTER_TYPE_AVERAGE_VOLUME          >=       ["1000000"], interval="1d", length=30
FILTER_TYPE_VOLUME                  >=       ["250000"], interval="1d"
FILTER_TYPE_RELATIVE_VOLUME         >=       ["2.0"], interval="1d", length=30
FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE >=     ["3.0"], interval="1d", plot="Close"
```

`FILTER_TYPE_GAP >= 2.0`, interval `1d`, is a supported optional variant, not a required core filter. Requiring it would bias the scanner toward gap continuations and miss non-gap intraday momentum.

- Sort field: **unverified; not exposed by the inspected read-only specification**.
- Sort direction: **unverified; not exposed by the inspected read-only specification**.
- Desired result count: **10–20**, with a preferred hard cap of 20 if a future creation/configuration schema explicitly confirms such a limit. Until then, the agent must cap downstream processing at 20 and treat larger/unordered results as a configuration defect.

Because sort and result-limit capabilities are not verified, this proposal is not creation-ready.

## 4. Stage-B filtering plan

Use the two-stage architecture. The scanner only reduces the universe; it does not decide whether to trade.

1. Resolve the saved scanner ID with `get_scans` when the ID is not already stored. Then call `run_scan` once. Do not run a current scan during this design exercise.
2. Keep no more than 20 returned symbols. Record `total_items`; if it exceeds the returned rows or 20 because of an upstream/frontend cap, do not pretend the set is complete.
3. Call `get_equity_quotes` once for up to 20 symbols. Remove symbols with missing quotes, inactive state, `has_traded=false`, invalid prices, or zero/unavailable bid and ask. Use the quote timestamps to reject stale data. Apply a simple spread-quality check only in Stage B, not as a scanner filter.
4. Call `get_equity_historicals` for the survivors, using regular-hours `5minute` bars for the current session, up to 10 symbols per call. Use only a short current-session window; inspect price structure, bar volume, opening-range behavior, and whether activity is persistent. This is one call when 10 or fewer survive, two when 11–20 survive.
5. Retain at most three serious candidates based on data completeness, sustained activity, coherent 5-minute structure, and consistency with the scanner’s price/volume signals. This is candidate ranking, not trade qualification.
6. For those at most three, call `get_equity_historicals` once more for regular-hours `15minute` bars and call `get_equity_technical_indicators` per symbol for a small fixed set such as `vwap`, `rsi` period 14, and `ema` period 20. The indicator tool accepts one symbol per call, so this is at most nine indicator calls.
7. Optionally call `get_equity_tradability` once for the final three using the dedicated Agentic account, to remove inactive, untradable, or non-fractional-ineligible names before any later analysis. Do not call order review or any order tool.

Do not use Level 2 for the full Stage-A list. Do not retrieve fundamentals, financial statements, or large OHLCV windows for every Stage-A result. Broader-market context (`get_indexes` then `get_index_quotes`) and Level 2 belong to later deep analysis of the final candidates, not routine scanning.

## 5. Expected tool-call cost per routine scan

With a persisted scanner ID:

- 1 `run_scan` call.
- 1 batched `get_equity_quotes` call for up to 20 symbols.
- 1–2 batched `get_equity_historicals` calls for 5-minute bars, depending on whether more than 10 survive the quote gate.
- 1 batched `get_equity_historicals` call for 15-minute bars on at most three candidates.
- 0–9 `get_equity_technical_indicators` calls, capped at three indicators for each of at most three candidates.
- 0–1 `get_equity_tradability` call for the final candidates.

Expected routine total: **4–15 Robinhood read calls**, with the upper end representing 20 Stage-A symbols and all three follow-up indicators. `get_scans` is an occasional setup/configuration call, not a required every-cycle call. No web search, Level 2 full-list call, fundamental deep dive, scanner write, watchlist write, or order call is part of the routine.

## 6. Risks and failure modes

- The scanner may include non-U.S. listings, ETFs, halted names, or instruments with unusable fractional/tradability status because those conditions are not fully expressible in the filter set.
- Price, market cap, float, and volume proxies do not eliminate every micro-cap or pump-and-dump situation.
- Daily volume and relative volume are time-of-day sensitive; early-session scans can be too sparse, while later scans can be stale for a fast-moving name.
- Strict AND semantics can miss valid non-gap momentum or valid lower-volume catalysts; the optional gap filter should not be mandatory in V1.
- A high percentage move can be a reversal, halt release, or disorderly event. The scanner cannot validate thesis, catalyst quality, support, resistance, or market regime.
- Missing or stale quotes, interpolated OHLCV bars, upstream caps, and an unverified sort/limit configuration can make the candidate set incomplete or poorly ordered.
- Overly high thresholds can produce no candidates; overly low thresholds can return too many. Thresholds should be evaluated on recorded shadow results before any creation.

## 7. Recommendation

**REVISE BEFORE CREATION.** The filter design is suitable as a two-stage concept, but the scanner should not be created until Robinhood exposes or confirms the exact sort field, sort direction, result-limit behavior, column configuration, and accepted `Asset type` value. No scanner was created.

SCANNER DESIGN COMPLETE
