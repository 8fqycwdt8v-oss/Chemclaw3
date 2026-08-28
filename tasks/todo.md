# Audit: the two data seams (ingest in, publish out)

Branch `audit-ingest`, worktree only. Four already-measured BACKLOG rows, each re-measured
against HEAD before it was worked.

## 1. The ORD pre-flight maps the whole fetch, once per drain chunk — CONFIRMED

Re-measured on this HEAD, 5,000 synthetic ORD exports built from the shipped
`data/eln-exports/ord/ord-2026-001.json`, three runs:

    run 0: total 0.969s  _unmappable 0.317s (32.7%)  63 us/entry
    run 1: total 1.712s  _unmappable 0.304s (17.8%)  61 us/entry
    run 2: total 0.851s  _unmappable 0.320s (37.6%)  64 us/entry

The row's 0.374 s / 75 us / ~26% stands (this sandbox is a little faster). `_unmappable`
maps every entry the fetch returns; `_BoundedIngest` truncates to `eln_sync_batch_size`
*after* the adapter returns, so a 100k backfill re-maps 100k entries per chunk.

- [x] Root cause: the refusal *recording* lives in the adapter, which knows neither which
      entries the chunk will keep nor its own registry source name (row 3, same cause).
- [x] Move it to `sync_entries`, which maps each entry exactly once, already builds the
      reason string, and takes `source`. The adapter *reports* its fetch-level refusals
      through an optional protocol, exactly as `BoundedFetch`/`fetch_was_truncated` already do.
- [x] `record_refusals` upserts with `executemany` instead of a Python loop.

## 2. A tool composite publishes twice and pins to its first computation — CONFIRMED, both halves

- [x] Half A measured: `predict_logd` ph=None -> `#1677c5556d3891f4`, ph=7.4 ->
      `#a357791989b0e1fe`; `compute_thermochemistry` 0.0 -> `#44005f1f6014fab5`,
      298.15 -> `#f93f5448d1dfed3b`. Same measurement, two refs.
- [x] Half B measured against Postgres: same ref, changed document, second enqueue writes 0 rows.
- [x] Fix: a composite's identity is **what it measured**, not what was asked.

## 3. `LEDGER_SOURCE` is a constant where the schema documents a registry source name — CONFIRMED
- [x] Closed by 1: the sync passes the registry name; the constant is deleted.

## 4. The corpus drain is the one ingest pass with no metric — CONFIRMED
- [x] `chemclaw_ingest_records_total` emitted from `drain_corpus`, `source` naming the data source.

## 5. Found while fixing 1 — a wrapper that hides the capability it wraps

`_BoundedIngest` had no public `inner`, so `fetch_refusals(adapter)` walked one step and stopped:
an unreadable ORD export left **no** ledger row on the durable path, silently. That is the same
defect the `fetch_was_truncated` docstring already records for `fetch_truncated` — reintroduced by
this change on the other capability, one wrapper further out. Failing test written first
(`test_an_unreadable_export_reaches_the_ledger_through_the_durable_wrapper`), `inner` exposed as
`DatedIngest` already does.

## Review

Two ADRs written (`D-2026-08-28-a-refusal-is-recorded-by-the-pass-not-by-the-fetch`,
`D-2026-08-28-a-composite-is-identified-by-what-it-measured`) and the ledger row added for each.
All four BACKLOG rows deleted in the same commit. Five tests written and seen failing first:

| test | proved failing on |
| --- | --- |
| `test_a_sentinel_default_and_the_value_it_stands_for_are_one_measurement` | two refs for one measurement |
| `test_a_composite_recomputed_by_a_changed_calculator_is_not_dropped_as_a_duplicate` | one ref for -1.850 and +1.349 |
| `test_a_corpus_drain_pass_is_counted_like_every_other_ingest_pass` | `0.0 > 0.0` — no metric at all |
| `test_a_second_ord_source_files_its_refusals_under_its_own_name` | both sources under `"eln-ord"` |
| `test_a_bounded_chunk_records_only_the_refusals_that_chunk_saw` | `6 == 2` |
| `test_an_unreadable_export_reaches_the_ledger_through_the_durable_wrapper` | my own change's gap |

## 6. A latent cross-test coupling this change introduces — measured, left open

`sync_entries` now writes `ingest_rejections` for every source, so the ELN sync tests leave rows
behind inside a pytest session: measured, `tests/test_eln.py` writes **9** rows under `test-eln`.
`gather_evidence` reads that table (`refusals_matching`), so a later test whose query shares a
qualifying word with one of those reasons would see a `refused_on_ingest` entry it did not have
before. Latent today rather than hypothetical-only: running `test_eln.py` first and then the five
files most likely to trip on it (`test_gather_evidence_outage`, `test_evidence_fanout`,
`test_framing`, `test_datasource_seam`, `test_dialogue`) in one deterministic invocation is
**171 passed**. Recorded rather than papered over with an autouse truncate, which would open a
connection per test for a hazard nothing currently trips.

**Unproven, reported as such**: nothing in the four rows was left unmeasured. What this change
does *not* settle is whether the calculation server's `embed_structure` is deterministic — if it is
not, `compute_thermochemistry` with no `structure_id` now publishes one record per call. That is
argued as correct (each is a different conformer, and the result says which) rather than measured,
because the determinism lives in `Chemclaw3-mcp` and is not observable from here.
