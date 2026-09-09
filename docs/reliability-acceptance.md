# Reliability acceptance

The reliability gate has two parts. `--reliability-acceptance-offline` runs the
offline unittest/fault-injection suite and deterministic policy checks without
constructing a Codex runner or writing state. The operator-run
`--reliability-acceptance-live` command requires an inactive service, repeats
the production read-only preflight, Luna schema/historical-path probe, and historical EOD smokes,
then writes `state/reliability_acceptance.json` for the current commit. The
daemon rejects a missing, malformed, stale, non-SHADOW, or wrong-commit
artifact with `DEPLOYMENT_NOT_ACCEPTED`. Test and acceptance commands are not
gated, preventing a circular dependency.

The `--session YYYY-MM-DD` argument supplies the completed exchange session
used by both historical probes.

Production state and report trees are hashed before and after the live smoke
work. The acceptance artifact is written only after that equality check; it
contains no broker or account data.

## Completed 15-minute structure

The dedicated Luna live probe deterministically requires one approved
`get_equity_historicals` read for completed regular-session 5-minute bars. The
Luna schema carries those source bars and Python validates them and derives at
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
