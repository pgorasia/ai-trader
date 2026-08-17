# Operation counter semantics

- `codex_subprocess_attempts`: top-level unattended Codex invocations started; runner-internal transport retries are reported by the result's attempt metadata.
- `codex_failed_attempts`: top-level invocations ending in `CodexRunError`.
- `stage_b_completed_runs`: Stage B slots that completed and passed validation.
- `stage_b_failed_slots`: independent Stage B slots made terminal by a Codex failure.
- `sol_completed_runs`: completed, validated senior jobs.
- `monitor_completed_runs`: completed, validated monitor jobs.
- `eod_completed_runs`: AI or deterministic-local EOD finalizations completed.
- `eod_failed_attempts`: failed EOD Codex attempts, independent of final session completion.
- `session_circuit_breaker_trips`: transitions from closed to open in a session.

Legacy `luna_runs`, `sol_runs`, `monitor_runs`, `eod_runs`, and `failed_runs` remain for compatibility. They do not define subprocess-attempt or operation-terminal semantics.
