# Scanner Schema V1 Inspection

Inspection mode: SHADOW MODE. No market scan, web search, order review, simulation, scanner creation, scanner update, or scan execution was performed. The read-only scanner filter-spec catalog was inspected to confirm filter domains.

## 1. CREATE_SCAN

The exposed input schema accepts exactly these arguments:

| Argument | Type | Required | Confirmed domain / meaning |
|---|---|---:|---|
| `filters` | `null` or array of filter objects | No for a new scan; required when `scan_id` is supplied | Enum and/or expression filters. |
| `preset` | `string` | No | `INITIAL`, `DAILY_GAINERS`, `DAILY_LOSERS`, `HIGH_OPTIONS_VOLUME_IV`, or `UPCOMING_EARNINGS`. `INITIAL` is only valid with filters; non-`INITIAL` cannot be combined with expression filters or `scan_id`. |
| `scan_id` | `string` | No | If supplied, appends a new configuration version to an existing scan; replacement semantics apply to the supplied filter set. |
| `title` | `string` | No | Custom human-readable scan name. This is the supported name field; there is no separate `name` argument. |

Each `filters` element has this schema:

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `display_title` | `string` | No | Label for an expression-filter results column. |
| `expression` | `string` | No | Raw market-data expression; omit `filter_type` when used. |
| `filter_type` | `string` | No | Wire-format `FILTER_TYPE_...` enum for an enum filter; omit when using `expression`. |
| `interval` | `string` | No | Supported time granularity for the selected enum filter. |
| `length` | `number` | No | Supported lookback length for the selected enum filter. |
| `plot` | `string` | No | Supported plot/price field for the selected enum filter. |
| `predicate` | `string` | Yes | Predicate/operator accepted by the selected filter. |
| `values` | `null` or array of `string` | Yes | Threshold values; cardinality depends on the predicate. |

`create_scan` does not accept a sort field, sort direction, result limit, result count, or column configuration. It also has no separate scanner-name argument beyond `title`.

## 2. UPDATE_SCAN_CONFIG

The exposed input schema accepts exactly:

| Argument | Type | Required | Confirmed domain / meaning |
|---|---|---:|---|
| `scan_id` | `string` | Yes | Existing scan identifier. |
| `sorting_column` | `string` | Yes | Must exactly match, case-sensitively, a currently visible column's display name. There is no closed enum in the schema; the domain is the target scan's visible column names. |
| `sorting_direction` | `string` | Yes | `asc` or `desc`. |

This tool changes sort only. It does not expose result-limit/count parameters or column add/remove/visibility/reorder configuration. `% Change` is a confirmed scanner display label, but sorting by it is valid only when that column is visible on the target scan. The schema does not provide a way to add the column here.

## 3. UPDATE_SCAN_FILTERS

The exposed top-level schema is:

```text
{
  scan_id: string,                         // required
  filters: null | FilterSpec[]             // required; [] clears all filters
}
```

The filter data structure is:

```text
FilterSpec {
  filter_type?: string,                    // FILTER_TYPE_... wire enum
  predicate: string,                       // required; e.g. ">=", "BETWEEN", "ANY_OF"
  values: null | string[],                 // required; serialized threshold values
  interval?: string,                       // e.g. "1d", when the filter requires it
  length?: number,                         // numeric lookback when the filter requires it
  plot?: string,                            // supported plot/price-field value
  expression?: string,                     // present in the declared shape, but rejected by update_scan_filters
  display_title?: string                   // only meaningful for expression filters
}
```

For a normal enum filter, `filter_type` is the wire-format string, `predicate` is the symbolic operator string, `values` is an array of strings, and `interval`, `length`, and `plot` are plain optional fields whose values must match that filter's catalog. Unary predicates use one value; `BETWEEN` uses two; list predicates may use multiple values. The operation is replace-all, not merge. Expression filters are not usable with this update operation despite the optional expression-shaped fields appearing in the declared object type; expression filters require `create_scan`.

## 4. INSTRUMENT TYPE

The read-only filter catalog confirms:

```text
FILTER_TYPE_INSTRUMENT_TYPE
display_name: Asset type
value_type: STRING
supported_predicates: ["=", "ANY_OF"]
```

It does not enumerate valid string values. Other result schemas mention asset classes such as `STOCK` and `ETF`, but that is an output field and does not confirm the accepted filter value. Therefore:

```text
FILTER_TYPE_INSTRUMENT_TYPE = STOCK: UNVERIFIED
```

No create/update call was made to test it.

## 5. REVISED V1 CONFIGURATION

Using only confirmed functionality, the proposed enum-only filter set is:

```json
[
  {"filter_type":"FILTER_TYPE_LAST","predicate":">=","values":["5"]},
  {"filter_type":"FILTER_TYPE_MARKET_CAP","predicate":">=","values":["300000000"]},
  {"filter_type":"FILTER_TYPE_SHARES_FLOAT","predicate":">=","values":["10000000"]},
  {"filter_type":"FILTER_TYPE_AVERAGE_VOLUME","predicate":">=","values":["1000000"],"interval":"1d","length":30},
  {"filter_type":"FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE","predicate":">=","values":["2.0"],"interval":"1d","plot":"Close"}
]
```

The catalog confirms all five filter types, their value units/types, the `>=` predicate, and the required 30-day/1-day settings where applicable. `FILTER_TYPE_INSTRUMENT_TYPE` is omitted because `STOCK` is unverified. Current-day volume and relative volume are intentionally not mandatory filters.

Preferred sort, conditional on `% Change` being visible:

```text
sorting_column: "% Change"
sorting_direction: "desc"
```

No result-limit/count argument is exposed by the inspected tools. Downstream processing must therefore enforce a maximum of 20 symbols.

## 6. SAFETY BOUNDARY

The exposed Robinhood tool set contains no `place_equity_order` and no `cancel_equity_order`. It also contains no options-trading tools. The scanner filter catalog includes OPTION-category scanner filters, but those are filter metadata, not options execution tools. The prohibited order and simulation tools were not invoked.

SCHEMA READY FOR CREATION
