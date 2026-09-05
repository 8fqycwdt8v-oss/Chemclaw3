# D-2026-09-05-an-outbox-row-is-a-record-and-its-publications — the requester is part of the identity, and nobody was recorded

## Context

`result_publications` deduped on `ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING`. That
index is right and the `DO NOTHING` read it as meaning "one calculation, one row" — which is true of
the *record* and false of the row, because the row carries the record **and its publications**. The
shipped sink keys `calculation_publication` on `(calc_ref, tenant_id, session_id, job_id)` and
carries `calculation_publication_actor_idx`, both built to hold several; `publish/record.py` says so
in prose: *"Two chemists running the same calculation produce one `ResultRecord` and two
publications."*

Measured: enqueue `calc_ref="shared-key"` for alice, then for bob → `alice_rows=1 bob_rows=0`, and
the single stored document names alice. Bob's run is invisible in the results store and in the actor
index.

**Underneath that, the primitive path named nobody at all.** `science/calc/store.py`'s
`publish_stored_result` passed no `Publication`, so `calculation_publication` held not one row for
any primitive this system had ever computed — only the durable-job and backfill paths ever named a
requester. The finding as first written ("a second chemist's provenance is discarded") understated
it: the *first* chemist's was never recorded either.

**And a cache hit returned before the hook.** `cached_compute` answered from the store and returned
at `store.py`'s hit branch, above the `publish_stored_result` call on the miss branch. The comment
there argued that keeping the write off the hottest read was the point. That is a real cost and it
was the wrong trade, because *who asked* is not a property of the calculation, and a hit is
precisely the second chemist asking.

## Decision

**An outbox row's identity is the calculation plus the requester, not the calculation.** The enqueue
`DO UPDATE`s, merging the incoming `publications` into the stored document and re-queueing the row,
guarded by `WHERE NOT stored @> incoming`. A replay of the *same* publication — all three call
sites, a retried Temporal activity, a re-run backfill — still writes nothing, so every idempotency
property the `DO NOTHING` was protecting survives and is asserted.

Migration `081` adds `revision`/`claimed_revision`: `claim` snapshots the revision and
`mark_delivered` settles only rows whose revision has not moved, so a publication merged into an
**in-flight** row is not marked delivered unsent. The merge opens that race; the pair closes it.

**`publish_stored_result` names its requester**, from `get_current_actor()` and the session and
correlation context — and passes `None` rather than a `Publication` of empty strings when there is
no actor. A backfill walk and a scheduled sweep genuinely have no requester, and an empty-actor row
sitting in the index that answers "who relied on this number" is the `audit_events.agent` failure of
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`, one table over.

**The cache-hit path publishes too.** The cost is bounded at both ends: `publishing_enabled()` is a
list lookup, so a deployment with no `CHEMCLAW_RESULT_SINKS` — every shipped configuration today —
pays one comparison and never imports the projection machinery; and with a sink attached a repeat by
the same actor and session is `WHERE NOT stored @> incoming` and writes nothing.

## Consequences

The three causes are independent and the fix is only correct as a whole.
`tests/test_store.py::test_two_chemists_sharing_one_cached_result_both_reach_the_results_store`
drives two chemists through one calculation end to end and asserts on the **set of actors** rather
than a row count — a count of 1 was the old bug and a count of 2 carrying one name would be the same
bug wearing a different number. Reverting the hit-path publish alone fails it; reverting the outbox
merge alone fails it; both together pass. That property is the reason the test exists: a reviewer
who fixes one half will otherwise measure success while the other half stays open.

A deployment with a sink attached now writes one extra no-op statement per cache hit. That is the
price of the actor index being true, and it is stated here so nobody reads the removed
"keep the write off the hottest read" comment as an argument that was abandoned rather than lost.
