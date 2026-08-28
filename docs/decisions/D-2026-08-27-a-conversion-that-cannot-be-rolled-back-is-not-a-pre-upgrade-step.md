# D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step — a conversion that cannot be rolled back is not a pre-upgrade step

**Status:** accepted · **Date:** 2026-08-27

## Context

The M6 stored-message conversion (`chemclaw.agent.message_migration`) rewrites `session_messages`
rows from the removed framework's shape into LangChain's. Three documents describe it as safe, and
each describes a different property than the one the code has.

- `043_session_message_shape.sql`:12-22 argues that "an unversioned rewrite destroys the evidence"
  and that keeping the original readable "is what makes this step reversible in practice, whatever
  the plan calls it".
- The module docstring makes the same argument in the same words: "Versioned rows keep the original
  readable until the conversion has been trusted on real data for a while."
- `convert_stored_messages` calls itself resumable, which it is, and treats that as what "makes an
  irreversible step survivable".

The stamp those two arguments rest on cannot do what they claim. `message_shape` says which shape a
row holds **now**. `_MARK_CONVERTED` set it in the same statement that overwrote `message`, so the
earlier bytes were gone the instant a row was marked converted, and nothing anywhere held them.

And that statement ran, per `migrate-job.yaml`:10, as a **`pre-upgrade` hook** — before any pod of
the new release exists, while the previous release is the only thing serving traffic.

## Measurement

Seed one three-row exchange in MAF shape in a live database, convert it, and read it back the way
the previous release does — its SELECT does not name `message_shape`, so its reader is given no
stamp.

| Reader | Before the pass | After the pass |
| --- | --- | --- |
| `to_langchain` (the strict reader, the only one a pre-M6 image had) | converts 3/3 | `UnconvertibleMessage: stored message has unknown role ''` on 3/3 |
| `session_store.message_from_row(payload, None)` | `HumanMessage`, `AIMessage` with `tool_calls=[{… 'id': 'c1'}]`, `ToolMessage` with `tool_call_id='c1'`; degraded 0/3 | degraded 3/3: `HumanMessage`, `AIMessage` with `tool_calls=[]`, and the tool result as a plain `AIMessage` with **no** `tool_call_id` |
| `chemclaw.cli.explain._speaker` | `user`, `assistant`, `tool` | `unknown`, `unknown`, `unknown` |
| `SELECT count(*) … WHERE message ? 'contents'` | 3 | **0** |

Three things this pins that the prose did not.

1. The **words** survive the forgiving read path, so a chemist reading a transcript sees text and
   might not notice. The **structure** does not: the call/result pairing — the exact thing
   `agent/message_pairing.py` exists to protect, and the thing a provider rejects a thread over —
   is gone from the previous release's view of its own data.
2. The blank-render class `D-2026-08-11-what-the-removal-found` documents is the same failure seen
   from the other side, and `explain` reaching `unknown` on every row is its current form: the
   degraded stamp is what keeps it from printing a guessed speaker as the record.
3. The original was not somewhere else. It was nowhere.

`helm rollback` does not undo a data conversion and does not re-run a `pre-upgrade` hook, so none of
this heals itself. The worst case is the one the pre-deploy hook exists to protect against: a
release whose rollout *fails* had already converted the table, and the image it rolls back to cannot
read it.

## Decision

Two changes, deliberately separable, because they fix two different things.

### 1. The conversion preserves what it overwrites

`067_session_message_original.sql` adds `session_messages.message_original JSONB`, and
`_MARK_CONVERTED` becomes

```sql
UPDATE session_messages
   SET message_original = message, message = %s, message_shape = 'langchain'
 WHERE id = %s
```

The original is copied **from the column the same statement overwrites** rather than passed back in
as a parameter: every `SET` expression in one `UPDATE` reads the row as it was before any of them
applied, so `message_original` cannot hold something other than the exact bytes the row is losing —
which a re-serialisation of what the `SELECT` decoded could not promise.

The recovery is one statement, published in the SQL comment and in the module docstring, and
executed verbatim by `tests/test_message_migration.py::_roll_back` so that a documented procedure is
not first executed by whoever needs it:

```sql
UPDATE session_messages
   SET message = message_original, message_shape = 'maf', message_original = NULL
 WHERE message_original IS NOT NULL;
```

Measured after the change: `message_original` equals the inserted payload byte-for-byte on every
converted row, the previous release reads those originals back as `HumanMessage` / `AIMessage` with
its call / `ToolMessage` with `tool_call_id='c1'`, **none** degraded, and the rollback statement
restores every row to exactly what was written.

**Written for every row the conversion rewrites, which here is the same set as "rows whose shape
actually changed".** The pass selects only rows still stamped `maf`, and every one of those does
change shape, so there is no third case to decide between. A refused row is not updated at all and
keeps a `NULL` — asserted, so that a `NULL` means "never converted" rather than "converted and not
preserved".

**Disposal.** Nothing new. The column is on a table `durable/retention.py` already prunes per
session through the pairing closure (D-145), so it dies with its row: no new lifetime, no new sweep,
no edit to retention. What bounds it beyond that is **history rather than traffic** — nothing has
written a MAF-shaped row since M13 removed the framework, so the set of rows that can ever carry a
value here was fixed then and only shrinks. This is why it cannot become the shape
`D-2026-08-27-a-session-nobody-can-reopen-is-disposable` had to close for `session_owners`: that
population grew with use, and this one cannot grow at all. An operator who has trusted the
conversion on real data may `UPDATE session_messages SET message_original = NULL` to reclaim the
bytes early; that is the deliberate act of giving up the rollback this column exists to keep, which
is why nothing does it on a timer and why it is stated in `infra/sql/README.md`'s row rather than
implemented as a sweep.

### 2. The DDL stays pre-upgrade; the data conversion moves to post-upgrade

`migrate-job.yaml` now renders two Job documents.

- `-migrate`, unchanged in slot: `pre-install,pre-upgrade`, weight `-5`, running
  `python -m chemclaw.core.migrate && python -m chemclaw.core.grants`. Everything it does is safe
  for the release still running — the schema is forward-only and additive
  (D-2026-08-04-the-schema-only-goes-forward), so a migration adds what the new image needs and
  takes nothing the old image uses, and the grants only widen.
- `-convert`, new: `post-install,post-upgrade`, weight `5` (beside the Schedules hook, which it
  neither needs nor blocks), running `python -m chemclaw.agent.message_migration`.

The `&&` could order the conversion but could never move it, which is why this is a second document
and not a third command.

**Why post is correct rather than merely later.** `session_store.message_from_row` reads both
shapes, so an unconverted row is a readable row and the new pods do not depend on the pass at all.
It is a backfill of historical rows. `tests/test_message_migration.py` asserts the stronger form: an
unconverted row does not merely render, it converts cleanly through the strict reader — so deferring
the pass costs a chemist nothing.

**Failure semantics, stated because they are the reason for the slot.** A failed `post-upgrade` hook
does **not** roll the release back. Helm marks the release failed and reports the Job; the pods stay
up serving both shapes, and the pass is resumable, so the recovery is to fix the refusal and run the
Job again. That is what we want: a healthy release must not be lost over a backfill. The corollary
is a constraint on the operator — `helm upgrade --atomic` *would* roll back on this hook, turning
the conversion back into the release gate it was moved out of the pre-upgrade path to stop being.
Do not run this chart with `--atomic`; the template comment says so at the point of the annotation.

**Its own bounds.** `convertJob.activeDeadlineSeconds` (1800) rather than `migrateJob`'s (900),
because the two bound different work: that one covers DDL and the locks it waits on, this one covers
a row count, on the single upgrade in a deployment's life that carries it across M6. Every later
release finds nothing stamped `maf` and exits at once. `backoffLimit: 3`, because the pass is
resumable and a retry continues rather than repeats.

**The credential does not follow.** The converter resolves its DSN through
`session_store._session_dsn`, so it runs as the runtime role, which already holds `UPDATE` on
`session_messages`. The convert Job therefore includes `chemclaw.env` and **not**
`chemclaw.migrationEnv` — the credential that owns the schema and can rewrite the audit trail stays
on the one Job that issues DDL. The existing file-level check cannot see this now that both Jobs
share a file, so `tests/test_helm_chart.py` splits the template on its document separator and
asserts it per document.

## Consequences

- `043`'s line-20 promise is now true, and it is true because the code changed rather than because
  the comment did. The migration is applied and is never edited (checksum drift); `067` is where the
  promise is kept, and its comment says so in the first paragraph.
- A release that fails its rollout converts nothing. A release that rolls out and is rolled back
  afterwards is recoverable by one `UPDATE` instead of by a database backup.
- The chart declares two pod specs in `migrate-job.yaml`, which `tests/test_deploy_chart.py`'s
  `_POD_SPECS` inventory pins deliberately; its entry moved from 1 to 2 in the same commit.
- `make db-migrate` still runs the conversion inline, and deliberately: it is a developer command
  against a database no previous release is serving, so the hazard this ADR is about cannot arise
  there. The pre/post distinction is a property of a rolling upgrade, not of the pass.
- `helm` is still not installed in this sandbox, so both chart assertions read template *source*,
  as `tests/test_helm_chart.py`'s own docstring requires them to admit. The rendered-YAML edge is
  unchanged and still open.
