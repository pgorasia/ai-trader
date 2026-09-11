# Sep-11 lifecycle audit

This audit is based on the saved `2026-09-11.json`, summary, and journal. Times
below are Eastern.

## BMNR

The frozen machine fields were trigger 26.20, maximum chase 26.24, post-entry
stop 25.90, target 26.97, latest entry 15:40, and mandatory flat 15:55. The
phrase "a completed 5-minute bar loses 25.92" existed only inside
`invalidation_condition`; it was not duplicated in an executable field. The
11:45 completed bar closed at 25.8401, below 25.92. The monitor only evaluated
trigger/chase before entry and therefore left the plan PENDING until cutoff,
when it became EXPIRED. Root-cause classification: A (prose-only pre-entry
invalidation).

## Scheduling and DELL

The session loop used `_active_plan_count`, which includes PENDING, as an
exclusive choice between monitoring and Stage B. Thus cycle 8 was the final
scan after BMNR froze. DELL was evaluated once as cycle-8 research rank 2 and
rejected at 10:55:47; its cooldown ended at 11:25:47, but no later scanner ran.
The EOD review found a later materially different consolidation/breakout. That
is hindsight evidence, not proof DELL would have appeared or passed unchanged
live thresholds. Resumed Stage B would nevertheless have had opportunities to
observe and classify it. Root cause: scheduler replacement plus the missing
BMNR terminal transition, not DELL-specific thresholds.

## Failures and accounting

The 12:05 monitor operation recorded zero observed calls and then failed the
response validator's combined web/read-error branch. Because the runner
structurally disabled web and separately rejects any observed web event, the
saved evidence supports a returned collector `errors` value, not prohibited web
execution; the old message was ambiguous. Four other monitors contain an
observed completed history call followed by a failed duplicate call. Ten more
failed after the collector completed because a forming bar was passed to a
validator that rejected `complete=false` despite the prompt requiring that bar.

`monitor_completed_runs=56` counts collector subprocesses whose output passed
the response validator. `monitor_runs=45` counts operations that also completed
deterministic evaluation and persistence. The eleven-operation difference is
the eleven forming-bar deterministic failures; failed collector/tool operations
never incremented the former. The
counters have different intentional stage semantics, now documented.

The failed cycle-7 Sol operation observed quote and historical calls but no web
search. Web was enabled and the identical later stage succeeded, so the direct
cause was skipped mandatory research, not tool unavailability or missed event
observation. The runner's event-level `require_web_search` gate correctly made
that attempt terminal rather than accepting or retrying the decision.
