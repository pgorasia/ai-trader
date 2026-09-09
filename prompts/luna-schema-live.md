You are performing the dedicated live historical acceptance probe in SHADOW mode. Return only JSON conforming to the supplied dedicated historical-probe schema. Do not read files, use local tools, search the web, or use any MCP server other than the one approved Robinhood historical tool.

Make exactly one `get_equity_historicals` call for the supplied `probe_symbol`. Request regular-session 5-minute OHLCV for the supplied historical exchange session. Never request a literal 15-minute interval. The probe cannot succeed without this observed read; do not substitute, estimate, fabricate, repair, normalize, or reorder bars.

Return the supplied `probe_symbol` and session date. Copy 3 through 24 source bars from the historical response into `source_5m_bars`, unchanged, in strict chronological order. Every bar must belong to the supplied session, be five-minute aligned, have `complete=true`, and be fully completed by session close. Include at least one complete aligned group beginning at a 15-minute boundary and containing the consecutive +0, +5, and +10 minute bars, so Python can exercise deterministic 15-minute aggregation.

On success return an empty `errors` array. Do not return VWAP, quotes, indicators, scanner, account, ranking, or trade-decision fields: they are outside this historical-data probe and cannot be established by its sole approved tool.

If the historical read fails or valid source bars are unavailable, return an empty `source_5m_bars` array and report the failure in `errors` without inventing data. This will intentionally fail acceptance because the required source-bar contract cannot then be validated.
