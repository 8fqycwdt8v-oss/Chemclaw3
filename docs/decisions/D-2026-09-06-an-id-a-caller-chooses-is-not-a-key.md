# D-2026-09-06-an-id-a-caller-chooses-is-not-a-key — `turn_costs` is keyed on a server-minted turn id, and the correlation id stays as the join

`033_cost_attribution.sql` made `correlation_id` the primary key of the cost ledger, and
`D-2026-08-01-spend-is-a-ledger-not-a-label` gave the reason:

> keyed on `correlation_id`, which already identifies the turn, already keys `audit_events`, and is
> already on every log line

Every clause of that is true of a correlation id **this system mints**. The front door does not
always mint one. `api/middleware._request_correlation_id` *adopts* an inbound
`X-Chemclaw-Correlation-Id` whenever it matches `[A-Za-z0-9_-]{8,64}`, deliberately and correctly —
`D-2026-08-01-a-turn-you-can-follow-across-a-process` argues that trace context is safe to adopt
because "the worst a forged `traceparent` achieves is attaching spans to a trace that is not
theirs. It grants no authority."

That argument holds for a span and fails for a primary key. A label that becomes a row's identity
grants exactly one authority: the authority to be that row.

## What was happening

Measured 2026-09-06 against the live Postgres, two turns for one actor written under one fixed
header:

```
rows, summed input_tokens for the two turns: (1, 1000)
actor 'victim' total spend per the ledger:   1000   (really spent 901,000)
client id accepted by the header filter:     True
```

`run_turn` keys the ledger on the adopted id and `agent/turn_cost_store` writes `ON CONFLICT
(correlation_id) DO UPDATE`, so a client that sends a constant header collapses its whole history
to one row holding the **last** turn's numbers. The ledger is erasable by the party it bills, with
no privilege, no malformed input and nothing in any log to say it happened. `chemclaw_tokens_total`
and `api/budget.py` go on seeing every turn, so the two records disagree by whatever was
overwritten — and the ledger is the one an operator bills against
(`D-2026-08-29-a-trail-nobody-can-read-answers-no-question`).

The upsert itself is not the defect and was never wrong about its own case. Its docstring names
"the one arithmetic error a cost ledger must never make is counting a turn twice". The other
arithmetic error is counting one turn **zero** times, and choosing the conflict target is what
decides which of the two you get.

## Decision

**`turn_costs.turn_id` is the primary key: minted per record by `core/turn_cost.TurnCost`, crossing
no wire in either direction.** `correlation_id` stays on the row, as an ordinary indexed column,
which is what it always was semantically — the join to `audit_events`, `session_messages`, the
access log and `chemclaw explain`. Nothing that resolved before resolves differently; a join that
used to return one row may now return two, and two turns under one label really are two turns.

Both properties the upsert was chosen for survive, and they were the constraint on the design:

- **A retried write is still not a double count.** Idempotency moves from "the same correlation id"
  to "the same record". Both producers — `api/runner._book_turn_spend` and
  `durable/template_activities._book_step_spend` — build exactly one `TurnCost` per turn and hand
  that object to `record_turn_cost`, so a re-execution of the write upserts on the id the object is
  already carrying. Measured: three writes of two records leave two rows summing 901,000.
- **The join is unchanged**, because the column is unchanged. `088` adds
  `turn_costs_correlation_id_idx`, non-unique on purpose: a template run already books one row per
  `agent` step under one run correlation id, so the "unique per turn" premise was not even true of
  this system's own writer.

**A `default_factory`, not a required argument.** There is no value a writer could pass that would
be more correct than a fresh one, and requiring the argument would put the identity back in reach of
whatever is calling — the shape being removed. It also means the second producer needed no edit to
become safe.

## Alternatives considered

**Stop adopting the header.** Refused: it is a real tracing feature, and the same id would still key
the ledger from the request path. The defect is not that the id is adopted, it is that an adopted
id was a key.

**Key on `(correlation_id, session_id, started_at)`.** A composite whose leading term the caller
chooses is still a caller-influenced key, `session_id` is also caller-supplied within the caller's
own scope, and `started_at` is not a column — so this trades one migration for a wider one and
leaves the upsert idempotent on a timestamp, which a retried write does not reproduce.

**Refuse a duplicate correlation id at the front door.** A rejected turn for a header collision is
a denial of service a caller can aim at itself, and the state to detect it is the ledger the
caller can already overwrite.

## Rollback

This migration ends "deploy the previous image" for this table, in both directions: the previous
image's `ON CONFLICT (correlation_id)` no longer matches a constraint, and its `INSERT` omits a
column the new primary key makes `NOT NULL`. It is registered in
`tests/test_migrations_are_additive._REVIEWED_ROLLBACK_BREAKS` naming this ADR, per
`D-2026-08-08-a-rollback-that-is-not-a-schema-step`.

**What an operator does instead.** No row is lost by rolling forward: `088_turn_cost_identity.sql`
backfills `turn_id` from each existing row's own `correlation_id`, which *was* its identity, so
every historical row keeps its meaning and its key value. To run the previous image against a
migrated database:

```sql
ALTER TABLE turn_costs ALTER COLUMN turn_id DROP NOT NULL;
ALTER TABLE turn_costs ALTER COLUMN turn_id SET DEFAULT '';
CREATE UNIQUE INDEX turn_costs_correlation_id_key ON turn_costs (correlation_id);
```

— which restores the arbiter the old writer's `ON CONFLICT` infers and lets its column list insert.
It also restores the overwrite this ADR exists to remove, so it is a step taken knowingly and for
as long as the old image is running. The `DELETE` that a unique index would need if two rows
already share a correlation id is the operator's decision and not a migration's: this schema does
not delete (`D-2026-08-04-the-schema-only-goes-forward`).

## Consequences

- The cost ledger is no longer writable across turn boundaries by anything outside this process.
- `turn_costs` may hold several rows per correlation id. Any future reader that assumes one must
  aggregate; the only current reader (`operations/activity.spend`) groups by actor and is unaffected.
- `tests/test_turn_cost.py` drives two records under one correlation id against real Postgres and
  asserts both survive, and re-writes one of them and asserts it does not double.
