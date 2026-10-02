# Point-in-time evidence capture

The SHADOW runtime writes observational evidence under `state/replay_capture/`.
Capture never changes prompts, schemas, tool permissions, model selection,
strategy inputs, or lifecycle decisions, and it issues no Robinhood, web, or
model requests.

## Storage format

`objects/<sha256>.json` contains an immutable canonical-JSON object. The file
name is the SHA-256 of its exact bytes. Objects are created atomically with
exclusive creation; an existing object must have identical bytes and digest.

`sessions/YYYY-MM-DD/manifest.jsonl` is an append-only event ledger. Each line
contains `schema_version`, a timezone-aware `recorded_at`, `session_date`,
`operation_id`, `attempt`, `event_kind`, `fidelity`, `content_digest`, the
runtime Git commit, and the accepted production commit. Identical objects may
deduplicate, but every observation still appends a ledger event.

The current schema is `point-in-time-evidence/v1`. Writes are flushed and
fsynced. The ledger is never rewritten by capture.

## Fidelity and completeness

- `EXACT`: the value is preserved exactly as exposed to the capture hook.
- `SANITIZED_EXACT`: only secret or identifying fields were removed/redacted.
- `UNAVAILABLE`: the production runtime did not expose the evidence. This is
  explicit and is never a reconstruction.

Session status is `COMPLETE` only when every required category exists (an
explicit `UNAVAILABLE` record satisfies a category whose absence is part of
the runtime contract), every referenced object verifies, and no capture
exception occurred. Missing categories or invalid objects are `INCOMPLETE`.
Any capture exception is `DEGRADED`; such a session is ineligible for exact
replay even if trading execution continued normally.

## Captured production path

The runner records the exact rendered Luna/Sol/monitor/EOD input, model and
invocation configuration, exact returned structured JSON before normalization,
normalized output, available usage/timing, and the already-produced Codex event
stream. Tool arguments/results and web evidence are retained only when the
runtime event stream actually exposes them. Post-observation hooks record the
validated output, scanner/finalist ordering, senior decision, frozen shadow
plan, monitor input bars and before/after lifecycle state, and EOD disposition.

The Robinhood scanner's separate full eligible universe is not exposed to
Python; it is recorded as `UNAVAILABLE`, never inferred from `scanner_total` or
`symbols_processed`. Fundamentals are not separately captured unless a future
runtime path actually returns and consumes them.

## Sanitization

Sanitization recursively drops authorization data, access/refresh/session
tokens, API keys, passwords, cookies, MCP credentials, raw account numbers and
IDs, and account-holder PII. Credential-shaped text is redacted. Numerical and
semantic projections used by strategy—equity, buying power, risk limits,
prices, quantities, bars, indicators, and classifications—remain intact.

## Replay and retention

Replay reads a session ledger in order, verifies each `content_digest`, checks
the recorded Git and accepted commits, then supplies object payloads to an
offline fixture harness. Replayers must preserve original list order and bar
timestamps and must not fill `UNAVAILABLE` categories. Keep manifests and all
referenced objects together. Content-addressed objects can be shared by many
sessions; retention tooling must use reference analysis before deleting any
object. Capture files may contain market and model evidence and should retain
owner-only filesystem access.

## Deliberate limits

The capture does not add full-universe scanner output, hidden MCP transport
data, unexposed fundamentals, or data the model/tool protocol omits. It also
does not add 1-minute or tick requests. Extra requests could affect rate limits
and runtime timing, would violate observational equivalence, and still would
not defensibly resolve historical same-bar ordering. Existing 5-minute
ambiguity therefore remains explicitly ambiguous.
