# Reliability acceptance

The reliability gate has two parts. `--reliability-acceptance-offline` runs the
offline unittest/fault-injection suite and deterministic policy checks without
constructing a Codex runner or writing state. The operator-run
`--reliability-acceptance-live` command requires an inactive service, repeats
a deterministically staged equivalent of the production read-only preflight, the Luna
schema/historical-path probe, and historical EOD smokes,
then writes `state/reliability_acceptance.json` for the current commit. The
daemon rejects a missing, malformed, stale, non-SHADOW, or wrong-commit
artifact with `DEPLOYMENT_NOT_ACCEPTED`. Test and acceptance commands are not
gated, preventing a circular dependency.

The live acceptance preflight first exposes only `get_accounts`. Python applies
the production identity and account-sanity rules before any account-scoped run
is launched. Portfolio, position, and order stages then each expose exactly one
approved read tool and use the ephemeral established account context. The
identifier is never included in reports or the acceptance artifact. Production
unattended preflight sequencing and its independent per-stage account/provenance
checks are unchanged.

The `--session YYYY-MM-DD` argument supplies the completed exchange session
used by both historical probes.

Production state and report trees are hashed before and after the live smoke
work. The acceptance artifact is written only after that equality check; it
contains no broker or account data.

## Completed 15-minute structure

The dedicated live historical probe uses its own minimal output schema and
deterministically requires exactly one approved
`get_equity_historicals` read for completed regular-session 5-minute bars. The
probe schema carries only the symbol, session, source bars, and bounded error
diagnostics; it does not ask the model for unrelated VWAP, quote, scanner,
account, or decision fields. Python validates the source bars and derives at
least one completed 15-minute aggregate. Missing calls, malformed bars,
forming/off-session bars, or source data that cannot form an aligned group all
fail acceptance. Normal Stage-B still requires only reconciliation and scan
calls universally; historical evidence remains conditional on a finalist.

## Required offline scenarios

The regression suite covers the 50 acceptance scenarios: event lifecycle and
teardown placement, exact/ordered preflight contracts, foreign/local tool
fail-closed behavior, Luna reconciliation/evidence/count/escalation/cooldown
semantics, strict 15-minute OHLC validation, Sol observed research and risk
arithmetic, bounded EOD attempts and read-only smoke behavior, modern versus
legacy recovery, isolated Stage-B continuity, circuit behavior, deterministic
restart/retry behavior, and commit-gate policy.
