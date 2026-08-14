# Percentage-Change Unit Diagnostic V1

Mode: SHADOW MODE  
Scanner: `AI-DayTrader-Diagnostic-V1`  
Scan ID: `23d29507-a0bb-45c3-aceb-320580cd9696`  
Scope: diagnostic scanner only; `AI-DayTrader-V1` was not modified.

## Scanner results

| TEST | THRESHOLD | TOTAL ITEMS | ROWS RETURNED |
|---|---:|---:|---:|
| A — baseline | none | 394 | 200 |
| B — fractional percent change | 0.02 | 222 | 200 |

The scanner returned at most 200 rows, so the row count is capped while `total_items` reports the full match count. Test C was not run because Test B returned nonzero results.

Test B first 10 symbols and scanner values:

| Symbol | Scanner % Change value |
|---|---:|
| NVDA | 0.029839080459770156 |
| SPCX | 0.08387726010953565 |
| MU | 0.06751139870123889 |
| AMD | 0.025657783774666924 |
| INTC | 0.04252379490328525 |
| ORCL | 0.04495463293923578 |
| AMAT | 0.048760107303894434 |
| LRCX | 0.054365627308050314 |
| CAT | 0.025522605736509454 |
| DELL | 0.060945189015125745 |

## Independent validation

Fresh Robinhood quote data was used for the first five Test-B symbols. Previous close is the official completed-session close dated 2026-08-11.

| SYMBOL | CURRENT PRICE | PREVIOUS CLOSE | CALCULATED DECIMAL CHANGE | CALCULATED PERCENT CHANGE | SCANNER VALUE if supplied |
|---|---:|---:|---:|---:|---:|
| NVDA | 223.980000 | 217.500000 | 0.0297931034 | 2.97931034% | 0.0298390805 |
| SPCX | 144.370000 | 133.290000 | 0.0831270163 | 8.31270163% | 0.0838772601 |
| MU | 926.900000 | 868.520000 | 0.0672177958 | 6.72177958% | 0.0675113987 |
| AMD | 486.570000 | 474.320000 | 0.0258264463 | 2.58264463% | 0.0256577838 |
| INTC | 101.845000 | 97.710000 | 0.0423191076 | 4.23191076% | 0.0425237949 |

The small differences between scanner and validation values are consistent with live prices changing between the scanner execution and the quote request. The unit relationship is consistent: scanner values are decimal returns, not percentage-point values.

## Conclusion

UNIT INTERPRETATION: A. CONFIRMED — `0.02` behaves consistently with approximately +2%; the scanner values align with decimal returns.

CONFIDENCE: High for this diagnostic. Five independent validations agree, and Test B produced 222 matches rather than zero.

PRODUCTION CHANGE RECOMMENDED: Use fractional decimal thresholds for future scanner configurations, e.g. `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE >= 0.02` for approximately +2%. Do not apply this recommendation automatically; `AI-DayTrader-V1` was intentionally left unchanged.

SECURITY BOUNDARY: PASSED. `place_equity_order` and `cancel_equity_order` were unavailable; no options transaction or exercise tools were available; no brokerage orders were submitted.

PERCENTAGE UNIT DIAGNOSTIC COMPLETE
