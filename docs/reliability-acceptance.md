# Reliability acceptance

The reliability gate has two parts. `--reliability-acceptance-offline` runs the
offline unittest/fault-injection suite and deterministic policy checks without
constructing a Codex runner or writing state. The operator-run
`--reliability-acceptance-live` command requires an inactive service, repeats
the production read-only preflight, Luna schema, and historical EOD smokes,
then writes `state/reliability_acceptance.json` for the current commit. The
daemon rejects a missing, malformed, stale, non-SHADOW, or wrong-commit
artifact with `DEPLOYMENT_NOT_ACCEPTED`. Test and acceptance commands are not
gated, preventing a circular dependency.

Production state and report trees are hashed before and after the live smoke
work. The acceptance artifact is written only after that equality check; it
contains no broker or account data.

## Completed 15-minute structure

Impossible OHLC remains strictly invalid model content and terminates only the
affected Stage-B operation. The current Codex event layer intentionally keeps
only tool identity/count/lifecycle evidence, not raw historical response
envelopes. Consequently Python cannot reconstruct genuine 5-minute bars at
this boundary without weakening provenance or adding sensitive raw payload
persistence. Deterministic aggregation therefore remains a future architecture
task, to be implemented only when a typed, sanitized historical-evidence
channel can provide exactly three completed, aligned regular-session 5-minute
bars per aggregate. No model-derived repair or invented shortcut is used.

## Required offline scenarios

The regression suite covers the 50 acceptance scenarios: event lifecycle and
teardown placement, exact/ordered preflight contracts, foreign/local tool
fail-closed behavior, Luna reconciliation/evidence/count/escalation/cooldown
semantics, strict 15-minute OHLC validation, Sol observed research and risk
arithmetic, bounded EOD attempts and read-only smoke behavior, modern versus
legacy recovery, isolated Stage-B continuity, circuit behavior, deterministic
restart/retry behavior, and commit-gate policy.
