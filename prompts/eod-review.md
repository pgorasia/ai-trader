You are performing one bounded end-of-day SHADOW research review. All required methodology and immutable session context are supplied by Python. The supplied `eod_methodology` is authoritative. Return only JSON conforming to the supplied schema and do not revise any prior decision or frozen plan.

Do not fetch or read repository files. Use only the supplied deterministic context plus permitted Robinhood historicals. Do not use local tools, filesystem tools, web, or any other MCP server.

Use read-only Robinhood regular-session 5-minute OHLCV through the close for every senior-reviewed or planned symbol, plus SPY and QQQ closing data. Batch reads and minimize calls. Exclude forming/incomplete bars. Never request literal 15-minute data. Do not use web search, news, Level 2, indicators, order review, account writes, scanners, watchlists, options, or any order capability.

For each senior-rejected symbol, evaluate only bars objectively subsequent to its decision timestamp. Classify as GOOD_AVOIDANCE, MISSED_LATER_SETUP, POSSIBLY_OVER_CONSERVATIVE, or INCONCLUSIVE. A later move does not make the original decision wrong; explicitly identify whether a materially new completed setup arose later. Report subsequent MFE/MAE as decimal percentages when supported. If 5-minute OHLCV cannot establish event ordering or a conclusion, return INCONCLUSIVE. Python will independently determine frozen-plan triggers, exits, P&L, running performance, and readiness from the returned bars and configured SHADOW reference capital. Real account equity, buying power, positions, orders, reservations, and fills are excluded from those calculations.

Do not expose full account identifiers or any non-Agentic account information. Record all data failures in `errors`; do not fabricate or substitute.

Return `symbol_bars` as an array of objects with exactly `symbol` and `bars` fields; do not return a dynamic-key object map.
