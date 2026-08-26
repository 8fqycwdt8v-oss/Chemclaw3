# D-2026-08-26-a-route-is-not-a-shape — a composite's shape is carried on the envelope, never inferred from the job that produced it

## Status

Accepted. Amends `D-2026-08-25-a-cache-is-not-a-record`, which stands: the seam, the schema and the
outbox are right. What it got wrong is that nothing reached them.

## Context

`D-2026-08-25-a-cache-is-not-a-record` shipped `src/chemclaw/publish/` — 21 tables, an outbox, a
Temporal drain, 17 projectors and three enqueue hooks — under one headline claim, taken verbatim
from its own investigation:

> Composites are not persisted at all … `compute_thermochemistry`, `compute_reaction_energy`,
> `compare_solvents` and the Boltzmann-weighted ensemble survive only as `job_records.result`
> JSONB … *The shapes a chemist actually reasons about are the least recorded.*

A review of the merged range found **twelve defects, each confirmed by execution**. Nine of them are
one failure with one cause, and the claim above was the first casualty: **no composite published.**

Assembling the whole path to verify the fix then found **two more**, both of which made the seam
unusable in a way no unit test could see — see *What assembling the path found* below.

### The shape of the mistake

`projector_for(calc_type, payload_kind)` dispatches two ways. `payload_kind` is exact — a model
name, matched against a 17-entry table. `calc_type` is an inference from a prefix (`xtb.*`, `dft`,
`pka`, `logd`, `solubility`, `descriptors`).

The composite hook built `calc_type = f"{job.connector}.{job.job}"` and set no `payload_kind`.
Measured, all four shipped jobs:

```
calc.compute_reaction_energy   -> None
qm.compute_dft_energy          -> None
bo.start_optimization_campaign -> None
results.republish_calculations -> None
```

`<connector>.<job>` is a **route**. It names where the work was dispatched. It does not name what
came back, and no amount of prefix matching can make it: two routes may return one shape, and a
job's return type may change without its name changing. The projector table was keyed on the right
thing all along; nothing gave it the key.

Three further defects were the same cause wearing different clothes:

- **All 17 `PAYLOAD_PROJECTORS` were unreachable.** Outside `project.py` the only mention of
  `payload_kind` in `src/` read it *back* out of a record. No production site ever set it, so the
  exact half of the dispatch was dead and only the 14 prefixes fired.
- **`records_from_solvent_screen` had no production caller.** The rule its docstring states —
  *never store an aggregate whose parts are not also stored* — held only inside the two tests that
  called it directly. A chemist would have found `best_solvent='toluene'` and no way to ask what ΔG
  was in toluene.
- **DFT bypassed the hook entirely.** `persist_qm_result` writes with `default_store().put(...)`,
  not `cached_compute`, because its computation happened on a cluster rather than behind a
  callable. `CALC_TYPE = "dft"` *does* resolve — it was a missed call site, not a missing projector.

### Why nothing caught it

`tests/test_publish_projection.py` passes `payload_kind="ReactionEnergyResult"` by hand.
`tests/test_publish_sql.py` calls `records_from_solvent_screen()` by hand. Both green. And
`grep -rn "_publish_result\|publish_job_result\|JobPublishInput" tests/*.py` returned **nothing** —
the composite hook had no test at all.

The suite proved the projectors work. It proved nothing about whether anything calls them.

This is `tasks/lessons.md`'s newest entry one level up. That entry says: measure the mechanism
*something else actually calls*. A projector is a mechanism, and testing it is still testing a
mechanism I chose. Recording the rule and applying it turn out to be different acts, and the second
has to happen while writing the code.

## Decision

**A shape is carried, never inferred from a route.**

`ConnectorJobResult` gains `payload_kind: str = ""`, set at the site that still holds the typed
result (`type(result).__name__`) and threaded through `JobPublishInput` → `enqueue_payload` →
`project`. Additive and defaulted for the reason `calc_refs` two fields above it already records:
it crosses the Temporal wire and histories are in flight. Empty means "this run did not say", which
falls back to the prefix inference exactly as before.

The backfill reads `job_records`, not the envelope, so migration **055** adds the same column there
— its own column beside `note_id` and `calc_refs`, following their precedent: `result` is the
connector's domain payload, opaque to core, and how core routes it is a fact *about* the run.

**One entry point decides one-versus-many.** `records_for()` replaces the three hooks' direct calls
to `project()`, consulting a `_MULTI_RECORD_PROJECTORS` table keyed the same way. That is what puts
the solvent screen's decomposition on the live path.

**`publish_stored_result` is public and paired with the write, not with `cached_compute`.** Every
writer to the calculation store is a producer of publishable science; there are two, and only one
had the hook.

### The other three defects

- **Repeated species collided.** `_member_for` matched each `SpeciesEnergy` to the *first* member
  with that identity, which looked harmless because both copies carry the same numbers. Measured on
  `2 H2O`: member 1 received no facts and the two facts for member 0 collided on `value_id` — a
  content hash over `(calc_ref, scope, ordinal, property)` — so the far end's `DO UPDATE` kept one
  and discarded the other. 6 rows, 5 ids. Matching is now one-to-one.
- **A blank overwrote a known value.** Two builders emit `structure` rows; the member-derived one
  knows only the id. `DO UPDATE SET col = EXCLUDED.col` let whichever landed second win, so a member
  row after a conformer row blanked a real `origin_calc_ref`. `DO NOTHING` would have fixed that
  ordering and broken the mirror one. `PRESERVE_ON_BLANK` says the actual rule: a writer who does
  not know a fact cannot erase it from one who does. Verified against Postgres in both orderings.
- **`enqueue_payload`'s "never raises" was false.** Its guard caught `(ProjectionError, ValueError)`
  — what a projector raises *deliberately*. Four projectors raise a bare `KeyError` when a field is
  missing from a *list element*. Live calculations were safe (pydantic had just produced the
  payload); `backfill_cached` walks rows an older calculator wrote, and one aborted the whole walk,
  breaking the exact property `backfill.py`'s docstring promises. Now `except Exception`: the
  comment beneath it always argued the right policy and the tuple was narrower than the argument.
- **One poison row retired its whole batch.** The drain validated the claim inside a single `try`
  and marked *every* claimed id failed. Now per row.

### Four declarations that described absent behaviour

Each removed or made true, per `D-2026-08-15`: an unread control is worse than an absent one.

- **`required_roles` is deleted.** Declared on the sink manifest, read by nothing — the
  `reject_widening` / `map_to_hpc_identity` shape, a claim that a control exists. Worse than the
  precedent it cited: `D-2026-08-07` records that the *share's* `required_roles` defaulting to `[]`
  served an AD-gated drive to every authenticated caller, and that one at least had a reader. The
  architecture is also why it was never wired: the outbox decouples the calculation from the
  delivery, so by the time a row is drained there is no turn and no actor to gate on. Re-adding it
  means deciding that at enqueue, and that is a new decision.
- **The Snowflake claim is corrected.** `dialect.py` said Snowflake and Oracle "spell it `MERGE`",
  which read as though the driver spoke them. It emits `ON CONFLICT` and nothing else. The
  *schema* is portable — that was always the real claim — and the docstring now says so. The
  `information_schema` probe's lowercase match, which would have found nothing on either engine, is
  fixed with `LOWER(table_name)` because that is a one-word correction rather than a promise.
- **`schema/` ships in the image.** `drivers/sql.py` tells an operator to run
  `python -m chemclaw.cli.sink_schema` when the target has no tables — and the Containerfile never
  COPYed `schema/`, so the remediation failed exactly where it was being recommended.
  `tests/test_deploy_chart.py` pins it.
- **`produced_structure_id` is published.** Written into `extra` by two projectors and read by
  nobody, so an optimization's relaxed geometry and a scan's minimum lost their address. Now a
  `produced_structure` fact — a registry row and an INSERT, never an ALTER, which is the extension
  story this schema was built for, demonstrated rather than asserted.

### What assembling the path found

Fixing the nine did not prove the path works; building it end to end did, and it failed twice
before it passed.

- **The shipped driver could not satisfy the shipped sink.** `SqlResultSink._connect` checks
  `isinstance(warehouse, Warehouse)`, and `Warehouse` is `@runtime_checkable` — which tests for the
  *presence of every member*. `PostgresWarehouse` had no `vector_dialect`, because it searches
  nothing and there was nothing to write. So the check failed and every delivery died at the
  connect with "did not build a Warehouse". The one driver this repository ships for the one sink it
  ships could not deliver a single row. It now returns `None`, which is the honest answer rather
  than a stub: `vector_dialect` exists so the *inbound* seam can spell a similarity search.
- **Every drain pass leaked a Postgres connection.** Two decisions, each correct alone. The drain
  builds a sink per run so a rotated credential takes effect on the next pass rather than the next
  restart; `SqlResultSink` connects lazily and holds the connection for its life, which is right for
  a batch of upserts. Nothing closed the sink. At the default
  `result_publish_schedule_minutes: 15` that is four connections an hour against a stock
  `max_connections` of 100 — the worker dies in about a day, and it fails *everything*, not just
  publishing. `aclose()` is now on the `ResultSink` Protocol (so mypy requires it of every sink, and
  it immediately did) and the drain calls it in a `finally`, because a sink that failed its batch
  holds the same connection as one that succeeded.

Neither is subtle. Both were invisible to 72 green tests, for the same reason as the other nine:
`test_publish_outbox.py` delivers to a stub sink and `test_publish_projection.py` never builds one.
`tests/test_publish_end_to_end.py` is the file that assembles them, and it is the only test here
that would have failed on the seam as merged.

## Consequences

- Composites publish, and a composite queued the way a finished job queues one now reaches a real
  Postgres running the shipped DDL and answers "what was ΔG in THF" — including for a run submitted
  under the alias `tetrahydrofuran`. `tests/test_publish_end_to_end.py` is that path;
  `tests/test_publish_reaches_the_hooks.py` asserts the routing from the envelope and from the
  durable record for all four shipped jobs.
- **A test that starts at a projector is not evidence about a seam, and one that ends at a stub is
  not evidence about a driver.** The new files start at a real hook and end at a real database.
  Every regression test added here was confirmed to fail against the code it replaces before being
  kept — the mutation sweep, the batch parse, the sink close.
- Migration 055 is additive and defaulted; rows written before it decode as "did not say".
- Still open, unchanged from D-2026-08-25: no deployment points at a real results database, and
  nothing has measured rows-per-calculation on a real corpus.

## Alternatives rejected

**Map `<connector>.<job>` to a projector.** A second table to keep in step with the first, keyed on
the thing that does not identify a shape. It would have worked today and drifted the first time a
job's return type changed — silently, which is the failure mode this whole ADR is about.

**Infer the shape from the payload's keys.** Duck-typing a scientific record. Two shapes that
overlap in their fields would route to whichever the matcher tried first, and the failure would be
a plausible number in the wrong table.

**Keep `required_roles` and wire it at delivery.** There is no actor at delivery; the outbox is what
removed one. Wiring it would have meant inventing an actor, which is worse than having no gate.
