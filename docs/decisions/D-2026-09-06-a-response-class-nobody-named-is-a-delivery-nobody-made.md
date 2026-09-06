# D-2026-09-06-a-response-class-nobody-named-is-a-delivery-nobody-made — the publish path's success arm is now closed, its outbox has no fourth state, and one destination cannot hold the others

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-08-25-a-cache-is-not-a-record (the outbox and the result store this corrects),
D-011 (a persisted result is never recomputed, which is why a lost publication is not re-derivable),
D-2026-08-26-the-driver-s-signature-is-the-schema (a `connection:` block is the driver's own
keywords, so a connect timeout is a *signature* change),
D-2026-08-27-a-digest-nobody-can-read-is-not-delivered (the delivery seam) ·
**Corrects** two comments and one docstring that described guarantees the code did not have.

## Context

A wave-6 review drove the shipped sinks and channels against injected failures — a scriptable HTTP
listener, a TCP blackhole, interrupted drains, a store built the way this repository tells a site to
build one. Thirteen properties held. The ones that did not share a shape: **the success arm was
open**. Each seam classified the failures somebody had thought of and *returned* on everything else,
and returning is what the outbox reads as "this science is durable at the far end".

## Decision

**1. Only `2xx` is a delivery** (`publish/drivers/http.py`). The classifier retried
`_RETRYABLE_STATUSES`, rejected `>= 400` and returned on the rest — and httpx does not follow
redirects (this fleet's deliberate posture, `connectors/registry.py`), so a `302` fell through both
guards. Measured end to end: the endpoint received the POST, wrote nothing, `deliver()` returned,
and `result_publications` read `state='delivered'` with `delivered_at` set — while the driver's own
log line for that call said `sink.failed … -> 3xx`. `requeue_failed` matches `failed` only, so the
row was unrecoverable and retention would delete it. A redirect is now a `SinkRejectedError` naming
the `Location`: the batch did not land where the manifest addressed it, and no retry to the same URL
changes that. **Following it is not on the table** — the payload is confidential chemistry and the
request may carry a bearer token, neither of which may reach an address no manifest named. The
sibling `WebhookDeliveryDriver` already got this right through `raise_for_status()`; the sink adopts
the *rule* rather than the code, because its contract is the two-way retryable/permanent split the
outbox reads and the sibling's is an `HTTPStatusError`.

**2. A row cannot spend its budget without an outcome** (`publish/outbox.py`). `_CLAIM` spends the
attempt and commits *before* the delivery, by design, so a pod eviction, an activity timeout or a
ceiling reached after the claim leaves the row `pending` with an attempt gone. On the last attempt
that produced a fourth state the three-state contract does not name: excluded from `_CLAIM`, not
counted by `_DEAD_LETTERED`, counted and ageing forever in the two gauges
`ChemclawResultOutboxStuck` reads, and unmatched by `requeue_failed`. Measured over eight
interrupted passes: `('alpha','h1','pending',8,'')`, `claim()` returned `[]`, `dead_lettered` was
empty, `requeue_failed()` reset **0** rows. `_REAP_EXHAUSTED` runs at the head of each claim, in the
same transaction, and retires exactly those rows — which is not a guess about what happened but the
definition of what did not, since a claimed row is `pending` only until it is marked. The last real
`last_error` outranks the reaper's generic one.

**3. `_CLAIM`'s double-claim comment is corrected, not its guard.** `SKIP LOCKED` excludes
overlapping *transactions* and the claim commits immediately; the scenario the comment named — a
scheduled drain and an operator's manual one — overlaps over the *delivery*, so the lock argument
never engaged for it. Measured: two drains 0.3 s apart both delivered one row, `attempts=2`.
Duplicate delivery is safe (every far-side key is a content hash, verified), so the harm is the
accounting. Closing it properly needs a **lease** — `state='in_flight'` plus `claimed_at` — which is
a column and a `CHECK` in `infra/sql/050_result_publications.sql`, outside this change's ownership.
So the comment now says what is true and what is missing, and the reaper turns the doubled burn from
a permanent zombie into an honest dead letter. **The lease is a `BACKLOG.md` row, not a claim here.**

**4. The per-sink ceiling is enforced at the seam that builds sinks** (`publish/registry.py`).
`result_publish_timeout_seconds` is documented as "how long one `deliver` may take" and bounded
nothing; the only ceiling was the activity's `timeout × len(sinks)`, one budget the first sink could
drink. Measured with alpha hanging and beta healthy over eight passes: beta was claimed **zero**
times, its row at `attempts=0` with no `last_error`, so nothing distinguished starved from idle —
while `durable/publish_results.py`'s docstring gives two failure domains as the reason for the
design (true of the *rows*, false of the *pass*). `build()` now returns a `_BoundedSink`. **In the
registry rather than the drain loop**, so the guarantee belongs to the seam: every caller gets it,
and the activity's `× len(sinks)` becomes the honest sum of N per-sink budgets. Re-measured end to
end, beta is delivered on pass 1 and alpha dead-letters at attempt 8 naming the knob.

**5. The Postgres sink bounds its handshake, and an unreachable warehouse is retryable again**
(`publish/drivers/postgres.py`, `publish/drivers/sql.py`). `statement_timeout` starts once there is
a session; against a socket that accepts and never speaks, a sink with `query_timeout_seconds=2` was
**still blocked after 20 s**. `connect_timeout_seconds` (default 10) is a driver keyword, checked
like its sibling because `0` means *no* timeout, and is not passed when the site's own `dsn` already
sets one. Found while measuring: `SqlResultSink` caught `(ConnectionError, OSError)`, and
`psycopg.OperationalError` is neither — so a warehouse that was simply **down** escaped to the
drain's generic arm, which treats a failure as a *poison record* and replays row by row, then
dead-letters every record as though its content were bad. The catch is widened on the seam's own
terms: a content failure arrives as `WarehouseQueryError`/`SinkRejectedError`, and anything else is
the destination not working.

**6. A column that carries a measurement is not an optional column** (`publish/dialect.py`,
`publish/drivers/sql.py`). Writing down to the schema you find is right and stays — a site may not
grant DDL — but the omission filter could not tell a new provenance column from the ones the value
lives in. Measured on a `property_value` missing `value_canonical` and `uncertainty`: `deliver()`
returned, the row was booked `delivered`, and it asserted `uncertainty_kind='reported'` while
holding no uncertainty and no `value_canonical`, the column the DDL calls *"THE predicate column"*.
`REQUIRED_COLUMNS` names those per fact table and a missing one is now the same class of fault as a
missing table. Beside it, the bootstrap: `schema/result-store/` is the DDL and no registry rows, so
applying the directory alone builds a store that takes every spine row and refuses every fact row on
a foreign key — measured, `calculation 1 / property_value 0`, an orphan a `GROUP BY` reads as a
calculation that produced nothing. `property_definition` is now probed for population **before the
first write**, which is the only place this can be caught without residue, since the writer is
row-by-row on an autocommit connection. And the schema-lag warning becomes `degraded()` once per
(sink, table) instead of a bare `logger.warning` per row.

**7. The backlog gauges describe what the drain works on, and an empty queue is not 56 years
behind** (`publish/outbox.py`). Removing a sink from `CHEMCLAW_RESULT_SINKS` left its rows drained,
pruned and requeued by nobody while `chemclaw_outbox_pending` counted them forever, so
`ChemclawResultOutboxStuck` paged permanently for a destination switched off on purpose. The two
reads are scoped to the enabled set and the stranded rows are reported through `degraded()` — a
different fact wanting a non-paging rule. **The larger defect that scoping exposed:** `_replace`
zeroes the *epoch* of a sink that has fallen to zero, and `_oldest_pending_seconds` subtracted that
placeholder from the clock, so a sink whose queue had just drained read
`chemclaw_outbox_oldest_pending_seconds = 1788721651` — about 56 years, the alert's worst reading at
the moment the drain is healthiest. `refresh_backlog`'s docstring had claimed the fixed behaviour
all along ("reads as '0 seconds behind', the honest answer for an empty queue"); the arithmetic said
the opposite.

**8. Delivery is at-least-once on purpose, and now says so on the wire** (`deliver/driver.py`).
`deliver_digest_activity` runs under `BAD_DATA_RETRY`, so a worker death after the POST re-POSTs.
That contract is correct — the alternative is losing a message — and the payload had no field a
receiver could key on, because `correlation_id` is rightly excluded. The file driver had computed
exactly such a handle for years and used it as a filename without sharing it: measured, three
`deliver()` calls of one message left **one** file and **three** POSTs. `message_id` is now one
function serving both, sent as a body field and as `Idempotency-Key`. It folds in `kind`, which
changes every share filename once — the alternative gives a `digest` and a `job-result` with the
same body one key, and that is a receiver dropping a real job result. The file write becomes a
temp-file + `os.replace` + `fsync`: measured on a ~520 kB digest re-delivered 60 times with a
concurrent reader, **227 of 1,883** observations (12%) saw a truncated file; after, 0 of 7,409. The
technique is copied from `kg/git_writer._replace_atomically` rather than imported — it is a
four-line stdlib idiom across a layer boundary, not an abstraction; a third caller moves it to
`core/`.

## Consequences

- A site whose results endpoint answers `3xx` stops being told its science is published. Its rows
  dead-letter, which is visible and requeueable, and the fix is the manifest's `url`.
- A site one migration behind on a *measurement* column now publishes nothing rather than publishing
  rows that assert more than they hold. A site behind on a provenance column is unaffected, which is
  asserted.
- An existing deployment's share gains one new filename per message on the first re-delivery.
- `_CLAIM` keeps `attempts < %s`, now redundant by construction with the reaper's partition. Kept
  because a statement that is correct read alone is worth four characters; said plainly rather than
  left for a reader to discover.
- `tests/test_publish_outbox.py::test_a_row_out_of_attempts_is_not_claimed_again_even_while_it_is_pending`
  asserted `("pending", 2)` as a *proxy* for "the bound did the work". That proxy was the defect;
  it now asserts the partition directly.

## Rejected

- **Following redirects on the sink.** It would send confidential records and a bearer token to an
  address no manifest names, which is what `follow_redirects=False` exists to prevent fleet-wide.
- **A lease, in this change.** It is the right fix for the double claim and it is a schema migration
  in a file this change does not own. A `BACKLOG.md` row with the measurement is the honest form.
- **Shipping the registry seed as `schema/result-store/003_*.sql`.** The seed is *generated* from
  `publish.properties` and `publish.solvents`, and a checked-in copy is a file that drifts from the
  writer it must agree with — the failure its own generated header warns about. The probe that names
  `--seed` closes the same hole without creating one.
- **A `chemclaw_outbox_orphaned` gauge.** A new metric family needs a row in `core/metrics.py`,
  outside this change's ownership; `degraded()` is an existing series that already means "we
  continued with less".
