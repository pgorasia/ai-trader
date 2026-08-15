# EOD Shadow Evaluation Methodology v1

This file is authoritative executable policy for deterministic EOD review.
Historical reports are evidence only and never define methodology.

- Use only completed, regular-session five-minute OHLCV for the session date.
- Evaluate rejection outcomes only from bars knowable after each decision time.
- Do not infer intrabar ordering. Classify unknowable sequences as inconclusive.
- Preserve frozen plans. Python computes triggers, exits, P&L, MFE/MAE, and readiness.
- Review every expected senior rejection exactly once using `(symbol, decision_timestamp)`.
- Do not add unexplained reviews or omit/duplicate an expected review.
- Use SPY and QQQ completed session closes for benchmarks when available.
- Missing, malformed, stale, or contradictory data fails closed.

Methodology version: `eod-shadow-v1`.
