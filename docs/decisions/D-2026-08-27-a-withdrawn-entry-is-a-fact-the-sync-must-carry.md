# D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry — a retraction is reported by the source, never inferred from absence

## Status

Accepted (2026-08-27), implemented.

## Context

`docs/planning/BACKLOG.md` carries a row saying a retracted ELN entry stays current evidence: an
entry withdrawn upstream simply disappears from the export, a cursor-based sync never sees an
absence, and what it produced keeps answering. The row says the receiving end is already built
(`Note.valid_to` + `is_current(as_of)`) and that `ingest/documents/sync.py::prune_share` is the
same problem already solved, refusals and all, so the work is to port that shape.

The defect is real. Both claims about how to fix it are wrong, and the second one dangerously.

## What was measured

Against this checkout, not against the prose (the sync driven with in-memory stores and a fake
adapter that serves an entry and then stops serving it):

| | measured |
|---|---|
| notes the ELN sync writes | **zero**. `compound_note` is the only `Note(...)` construction in `ingest/eln/`, and nothing in the sync path calls it. `durable/eln_sync.py` imports one name from `ingest/eln/records.py` and nothing from `kg/` |
| what run 2 reports when the entry vanishes | `ingested=[] skipped_existing=['kept'] rejected=[]` — the withdrawal produced no signal of any kind, in the summary, the log line or the counters |
| the withdrawn row afterwards | `read("withdrawn")` returns it; `is_current(today)` is `True`; `eligible(["withdrawn"], {})` returns `{"withdrawn"}`, so `FingerprintReactionRetriever` served a withdrawn run as current evidence |
| `ReactionRecord` fields, before | `body, compound_smiles, conditions, performed_at, project, reaction_id, source` — no `valid_to`, no tombstone |
| `ReactionRecordStore` methods, before | `record, read, bodies, eligible, known` — none of which could retire a row |
| `fetch_was_truncated(DatedIngest(bounded_adapter))` | **`False`**, for a bounded adapter that answers `True` unwrapped — see the last section |

**The receiving end the row names does not exist.** `Note.valid_to` is real and works, but
D-2026-08-25 made an ELN transcription a `reaction_records` row rather than a PR-gated note, and
`ReactionRecord.is_current` dropped the `valid_to` half deliberately, saying so: "a result does not
expire on its own, it is superseded, which is a separate claim a human makes in a note".

## Decision

**1. A retraction is a fact the source reports; it is never inferred from absence.** This is the
one non-negotiable, and it is why `prune_share` does not port whole. The share's sweep is safe
because a crawl is a *full enumeration*: "this run saw every file, and did not see this row" is
evidence. An ELN sync is a **delta** — `fetch_new_entries(since)` returns only what changed after
the cursor — so "not seen this run" is the normal, permanent state of every entry ever ingested.
Mark-and-sweep is not merely risky here, it is inapplicable: applied to a delta it retires the
entire corpus on the first run, and the only symptom is that the corpus stops answering. What ports
is the *refusal* half. The test in `tests/test_eln.py` that pins this runs five passes with the
export emptied and asserts nothing is ever retired — the guard against a future session
"finishing" this work by porting the sweep wholesale.

**2. The capability is optional, on `BoundedFetch`'s precedent.** Not every tenant's ELN can
express a withdrawal — some soft-delete a flagged row (which the amendment path already carries),
some publish a retraction feed, some do neither. So `RetractionAware` is a second Protocol beside
`ElnAdapter`, read through `fetch_retractions(adapter, since)`, whose answer for an adapter that
cannot report is **`None` — "cannot say"**, a distinct value from an empty report. That distinction
is `prune_share`'s "an unreachable share and an empty one look identical" moved into the type,
where a caller cannot forget it. `RetractionReport.complete` is **required, with no default**, for
the same reason in the other direction: a default of `True` would let an adapter that never
considered truncation be believed by omission.

**The reference `JsonExportAdapter` deliberately does not implement it.** A file-drop adapter reads
a directory; its only possible evidence is absence, and absence is precisely what decision 1
forbids reading as a withdrawal. A required method would have forced it to invent an answer, and
the cheap invention is the wrong one.

**3. It is not PR-gated.** D-2026-08-25 governs, and it turns on whether anything was *inferred*.
A withdrawal reported by the source is a transcription of a fact the source system states, exactly
as the entry itself is; `record_from_ord_reaction` hands a reviewer nothing to decide and neither
does this. Gating it is worse here than it was for the entry: the entry's gate merely delayed
readable data, while a retraction queue leaves *withdrawn* data answering as current for as long as
nobody drains it — the gate would preserve the defect it was added to fix. The inverse case keeps
the gate honest: a retraction **inferred** from absence would be an assertion, and decision 1
refuses to make one at all rather than gating it. There is also no gate on this path to reach — the
receiving end is a Postgres row.

**4. The tombstone is `retracted_at` on the record, not `valid_to`** (migration `066`).
D-2026-08-25's argument for omitting `valid_to` is sound and untouched: a *result* does not expire,
and deciding it has been superseded is a human claim belonging in a note. A source-reported
withdrawal is neither — it is the originating system saying the entry should not have been
published. Distinct name, distinct meaning, and `is_current(as_of)` gains its second clause. The
row is never deleted — `durable/retention.py` already refuses to prune `reaction_records` because
"a row is the only readable form of an ELN run" — so `read()` keeps serving it and `eligible()`
stops, which is exactly "stops answering as current, still readable as of an earlier date".

Two consequences that are easy to get backwards, so both are pinned by tests:

- **The upsert never writes `retracted_at`.** A soft-deleting source keeps exporting the withdrawn
  entry, so the overlap window re-fetches it every run; refreshing the tombstone from an ingest
  would clear it on the first replay. An amended body still overwrites the transcription.
- **Nothing reverses a retraction.** A reinstatement would have to be its own reported fact, and no
  delta fetch can imply one — an entry reappearing in an export is indistinguishable from one that
  never left.

**5. The sweep carries `prune_share`'s three refusals, translated, and counts what it retired.**

| `prune_share` | here |
|---|---|
| a root failed to walk — half a share is not a share | the report could not be fetched (`ChemclawError`/`OSError`); the source did not answer |
| the drain never finished (`has_more`) | the report is not whole (`complete=False`) — a page limit, a partly-readable feed, one backing source down |
| it saw no candidates at all | the adapter **cannot say** (`fetch_retractions` → `None`), which must never read as "none were reported" |

The middle row is the one the design changed on contact: the obvious reading was to reuse
`fetch_was_truncated`, but that is the *entry* drain, and a delta sweep reads no entries as
evidence at all — so the entry page being cut short says nothing either way, and the retraction
report's own completeness is the analogue.

`IngestSummary` gains `retracted` (rows this pass actually retired) and `retraction_refusal`
(empty when the sweep ran), both on the `ingest.finished` event, and
`chemclaw_ingest_records_total{outcome="retracted"}` tallies the first — so "retired nothing" and
"could not run" are two values rather than one silence. The refusal that says the adapter has no
such capability logs at DEBUG rather than WARNING: it is a deployment's shape and would otherwise
fire on every pass of every file-drop source, which is how the two refusals that *are* incidents
get scrolled past.

A retraction never advances the sync cursor — a future-stamped one would otherwise poison the fetch
window the way a future-stamped entry does — so a withdrawal is re-reported every pass until the
entry stream carries the cursor past it. `retract` skips an already-retracted row, so the earliest
report wins and the count keeps meaning "runs that left current evidence today".

## What was rejected

- **Mark-and-sweep over the fetch window**, the literal port the backlog row asks for. See decision
  1. `D-2026-08-27-a-retirement-rides-its-replacement` already refused the weaker version of this
  for the miners, where a truncated corpus read looked identical to vanished reactions.
- **Deleting the row.** It loses the "readable as of an earlier time" half outright, and
  `retention.py` refuses row deletion here for that reason.
- **Carrying the withdrawal only in the rendered body**, via the amendment path that already works.
  It reaches a human reading the record and changes nothing about what retrieval serves.

## What this found on the way

`DatedIngest` — the seam's normalisation wrapper, which `ingest/sources/registry.py` puts around
**every** adapter it builds — declared neither optional capability, and a `runtime_checkable`
Protocol is structural: a wrapper that does not redeclare a method simply does not have it. So
`fetch_was_truncated` answered `False` in every deployment, including for the warehouse adapter
that implements `fetch_truncated` precisely so the workflow comes back for a truncated remainder.
The signal that ADR added was dead through the seam that carries it.

Fixed as a rule rather than a patch, because the next optional capability would have been swallowed
identically: `_adapter_chain` reads a capability through the public `inner`, so a wrapper that
exposes what it wraps forwards every optional capability, present and future, by doing nothing.
Both capabilities are asserted through the wrapper in
`tests/test_eln.py::test_the_seam_wrapper_does_not_swallow_an_optional_capability`.
