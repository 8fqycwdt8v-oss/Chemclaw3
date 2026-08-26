# Review of #210, and the fourteen defects it found

## Context

`D-2026-08-25-a-cache-is-not-a-record` merged as `4373b72`. A review of the merged range
(`4d1ae72...4373b72`, 66 files, 7,899 insertions) found twelve defects, **each confirmed by running
the code rather than by reading it**. Nine share one cause. Assembling the whole path to verify the
fix then found **two more**, both of which made the seam unusable and neither of which any unit test
could see. All fourteen are recorded in `D-2026-08-26-a-route-is-not-a-shape`.

## Done

- [x] **P1 — the composite path published nothing.** `calc_type = f"{connector}.{job}"` is a route
      and matched no projector; all four shipped jobs resolved to `None`. `ConnectorJobResult` now
      carries `payload_kind`, set from `type(result).__name__` at the three envelope sites that hold
      a typed result; threaded through `JobPublishInput` → `enqueue_payload` → `project`.
      Migration 055 puts the same column on `job_records` so the backfill can route too.
- [x] **P1 — 17 `PAYLOAD_PROJECTORS` were unreachable.** Same fix: nothing set the key they are
      keyed on.
- [x] **P1 — the solvent screen's decomposition was test-only.** New `records_for()` is the single
      entry point that decides one-record-versus-many; `enqueue_payload` goes through it.
- [x] **P1 — DFT bypassed the hook.** `publish_stored_result` is now public and called beside
      `default_store().put(...)` in `persist_qm_result`, which cannot use `cached_compute`.
- [x] **P2 — repeated species collided.** `2 H2O` gave member 1 no facts and produced two facts
      sharing a `value_id` (6 rows, 5 ids). `_member_for` is now one-to-one.
- [x] **P2 — a blank overwrote a known `origin_calc_ref`.** `PRESERVE_ON_BLANK` in `dialect.py`;
      verified against Postgres in both write orderings.
- [x] **P3 — `enqueue_payload` could raise.** Four projectors raise `KeyError` on a missing
      *list-element* field; the guard caught only `(ProjectionError, ValueError)`. Now
      `except Exception`.
- [x] **P3 — one poison row retired its whole batch.** The drain parses per row.
- [x] **P4 — `required_roles` deleted** (declared, read by nothing).
- [x] **P4 — the Snowflake claim corrected**, and the `information_schema` probe made
      case-insensitive so it works on all three engines.
- [x] **P4 — `schema/` ships in the image**, pinned by `tests/test_deploy_chart.py`. The SQL sink's
      own error told operators to run a command that could not run there.
- [x] **P4 — `produced_structure_id` published** as a `produced_structure` fact rather than written
      into a dict nobody read.
- [x] **P5 — the shipped driver failed the shipped sink's own check.** `Warehouse` is
      `@runtime_checkable`, so the check tests for every member; `PostgresWarehouse` had no
      `vector_dialect` and every delivery died at the connect with "did not build a Warehouse".
      Found by building a sink and a driver together for the first time.
- [x] **P5 — every drain pass leaked a Postgres connection.** The drain builds a sink per run
      (deliberately) and `SqlResultSink` holds its connection for the sink's life (also
      deliberately) and nothing closed it — four connections an hour against a stock
      `max_connections` of 100. `aclose()` is now on the `ResultSink` Protocol, so mypy requires it
      of every sink, and the drain calls it in a `finally`.

## Verification

- `tests/test_publish_end_to_end.py` — new; the only test that assembles projector, outbox, drain,
  driver and the shipped DDL. A composite queued the way a finished job queues one reaches a second
  Postgres schema and answers "what was ΔG in THF" for a run submitted as `tetrahydrofuran`. This is
  the file that would have failed on the seam as merged, and it is what found the last two defects.
- `tests/test_publish_reaches_the_hooks.py` — new; every test starts at a production call site.
  Twelve tests: the four shipped jobs route, the envelope and the durable record carry the shape,
  the screen decomposes, repeated species keep distinct ids, and a mutation sweep over two shapes
  (~40 single-field deletions, including nested) asserts the enqueue absorbs all of them.
- **Every regression test was confirmed to fail against the code it replaces** before being kept:
  narrowing the guard back to `(ValueError,)` fails the mutation sweep; stashing the per-row parse
  fails the batch test; stashing the `finally` fails the sink-close test.
- `ruff`, `ruff format`, `mypy --strict` (680 files) clean. Declaration gates green:
  `test_decision_log`, `test_docstring_paths`, `test_schema_inventory`, `test_repo_map`,
  `test_layering`, `test_database_privileges`, `test_deploy_chart`, `test_config`.

## Review

The interesting finding is not any single defect — it is that a green suite of 72 tests said nothing
about the claim the feature was built on. Every test entered at `project()`, which is the function
I wrote; nothing entered at the hook, which is what production calls. `tasks/lessons.md` carries the
rule I drew from it, and the rule is narrower and more useful than "test more": *a test of a seam
starts at the outermost thing production calls, and if I cannot name the production caller of the
function my test invokes first, I have tested my own intentions.*

Two of the four P4 items are the same species of error in prose: a manifest field documented as an
access control with no reader, and a docstring naming two SQL dialects the emitter does not speak.
Both read as claims. Neither was a lie anyone told deliberately.

The two found last are the sharpest version of the whole point. Fixing the nine did not prove
anything; *assembling the path* did, and it failed twice before it passed — on a driver that could
not satisfy its own sink, and on a connection leak that would have killed the worker in a day.
Neither is subtle. Both were invisible to 72 green tests because no test ever put two real pieces
together.
