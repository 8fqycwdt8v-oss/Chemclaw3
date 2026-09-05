# D-2026-09-05-a-composite-with-no-record-cannot-be-republished — the one shape whose outbox row was its only copy

## Context

Every publishable shape is recoverable because a local record and a backfill walk stand behind it —
a primitive from `calculation_results` via `backfill_cached`, a job composite from `job_records` via
`backfill_jobs`. A **tool** composite had neither.

`publish/hooks.py` publishes `ThermochemistryResult` and `LogdResult` from the tool hook, and by
construction those are written to neither table: *"a composite is not written to the calculation
cache"*, because its key would name its own output. So its outbox row was the only copy — and the
enqueue is best-effort by design (`publish/outbox.py` swallows every failure, on the correct
reasoning that a results store which cannot be queued must not fail a tool call). Measured with the
outbox unwritable: the hook returns 0, both existing walks find nothing, and no third walk exists.
A dropped tool composite was **permanently unrecoverable**.

`connectors/results/workflows.py::_walk` called exactly the two walks, and
`cli/backfill_publications.py` listed exactly the two labels.

## Decision

Give the shape the record the other two have. Migration `082` adds `result_composites`;
`publish/composites.py::record_composite` writes it from the tool hook **before** the enqueue and
**independently of whether a sink is enabled**; `publish/backfill.backfill_composites` is the third
walk, wired into both call sites.

Writing it with publishing off is a knowing trade against the subsystem's "zero cost when off"
property — one INSERT per composite tool call. The alternative makes the recovery source conditional
on the subsystem it recovers for, which is the premise `cli/backfill_publications` already rests on
and which would be false for exactly this shape.

`result_composites` is refused by `durable/retention.py` for the reason `calculation_results` and
`job_records` are refused: deleting a row does not reclaim a cache, it ends the ability to republish
a value nothing but re-running the science can regenerate. The grant is **INSERT only** — the row is
written once under `ON CONFLICT (calc_ref) DO NOTHING` and never updated — so the no-DELETE argument
and the write-once argument land in the same line, and `tests/test_database_privileges.py` enforces
both directions: it caught the missing INSERT, and then caught an UPDATE nobody uses.

## Consequences

`tests/test_publish_backfill.py::test_a_dropped_tool_composite_comes_back` drops the enqueue, runs
the walk, and asserts the record arrives — then runs the walk again and asserts it is idempotent,
like its two siblings.

The shape now costs one row per composite tool call in every deployment, including those that will
never attach a sink. That is the stated price of the recovery path existing at all.
