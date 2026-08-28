# D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry — a retraction is reported by the source, never inferred from absence

## Status

Accepted (2026-08-27) — and **revised on review the same day**. The rule this ADR is named for
stands. The storage-and-sweep tier built to carry it was measured to be unreachable in three
independent places and has been **removed**; the defect it was written for is open again, and what
a working implementation costs is written down below so the next attempt starts from the readers
rather than from the column.

## Context

`docs/planning/BACKLOG.md` carried a row saying a retracted ELN entry stays current evidence: an
entry withdrawn upstream simply disappears from the export, a cursor-based sync never sees an
absence, and what it produced keeps answering. The row said the receiving end was already built
(`Note.valid_to` + `is_current(as_of)`) and that `ingest/documents/sync.py::prune_share` is the
same problem already solved, refusals and all, so the work was to port that shape.

The defect is real. Both claims about how to fix it are wrong, and the second one dangerously.

## What was measured, the first time

Against this checkout, not against the prose (the sync driven with in-memory stores and a fake
adapter that serves an entry and then stops serving it):

| | measured |
|---|---|
| notes the ELN sync writes | **zero**. `compound_note` is the only `Note(...)` construction in `ingest/eln/`, and nothing in the sync path calls it. `durable/eln_sync.py` imports one name from `ingest/eln/records.py` and nothing from `kg/` |
| what run 2 reports when the entry vanishes | `ingested=[] skipped_existing=['kept'] rejected=[]` — the withdrawal produced no signal of any kind, in the summary, the log line or the counters |
| the withdrawn row afterwards | `read("withdrawn")` returns it; `is_current(today)` is `True`; `eligible(["withdrawn"], {})` returns `{"withdrawn"}`, so `FingerprintReactionRetriever` served a withdrawn run as current evidence |
| `ReactionRecordStore` methods | `record, read, bodies, eligible, known` — none of which can retire a row |
| `fetch_was_truncated(DatedIngest(bounded_adapter))` | **`False`**, for a bounded adapter that answers `True` unwrapped — see "What was kept" |

**The receiving end the backlog row named does not exist.** `Note.valid_to` is real and works, but
D-2026-08-25 made an ELN transcription a `reaction_records` row rather than a PR-gated note, and
`ReactionRecord.is_current` dropped the `valid_to` half deliberately, saying so: "a result does not
expire on its own, it is superseded, which is a separate claim a human makes in a note".

## Decision

**1. A retraction is a fact the source reports; it is never inferred from absence.** This is the
one non-negotiable, it survives the removal, and it binds whoever builds this next. It is why
`prune_share` does not port whole. The share's sweep is safe because a crawl is a *full
enumeration*: "this run saw every file, and did not see this row" is evidence. An ELN sync is a
**delta** — `fetch_new_entries(since)` returns only what changed after the cursor — so "not seen
this run" is the normal, permanent state of every entry ever ingested. Mark-and-sweep is not merely
risky here, it is inapplicable: applied to a delta it retires the entire corpus on the first run,
and the only symptom is that the corpus stops answering.

**2. The storage-and-sweep tier is removed, because none of it could fire.** What was built —
a `RetractionAware` protocol, a `RetractionReport` with a required `complete` flag,
`fetch_retractions` through the seam, `retract` on all three stores, an after-the-ingest sweep in
`sync_entries`, two `IngestSummary` fields and a `retracted_at` bound in `is_current` and in the
eligibility SQL — is gone. Three independent measurements, taken on review:

| | measured |
|---|---|
| implementers of `RetractionAware` in `src/` | **zero**. The only one was a fake in `tests/test_eln.py`, so `fetch_retractions` answered `None` in every deployment, `retracted` was always 0, and the `066` column, its index and `retract` were never touched |
| the report through the production wrapper | bare adapter → the report; `DatedIngest` → the report; **`_BoundedIngest` → `None`**. `durable/eln_sync.py::_BoundedIngest` — which `sync_eln_entries` wraps every adapter in — keeps `self._inner` private while the capability walk follows the public `inner`, so production could not reach a producer even if one existed |
| the unfiltered evidence sweep, for a retracted run | `is_current(today)` `False`, `eligible([id], {})` empty, and `FingerprintReactionRetriever.retrieve(query, {})` **still returned `reaction-EXP-1001`** — only the *filtered* leg consults the record store |

The third is the one that decides it. `agent.graph_tools` never reads the tombstone,
`connectors.rxnfp` never asks the record store at all, and `record_from_ord_reaction` renders no
withdrawal into the body — so of the four readers `ingest/eln/records.py`'s own module docstring
names, exactly one honoured a withdrawal, and only in the mode a `gather_evidence` sweep does not
use. A chemist handed a withdrawn run had no way to see that it was withdrawn.

So the tier as shipped was a control that reads as enabled and is not, which is worse than the gap
it closes and is the shape CLAUDE.md says to delete on sight — the same shape as
`map_to_hpc_identity` and `audit_events.agent`. Keeping the storage half alone and calling the
rest a follow-up would preserve exactly that reading.

**3. What a working implementation costs**, so the next attempt is scoped from the start. All five,
or none:

- **A producer.** Not a transport question: what makes an adapter able to report is a *field*.
  `OrdJsonAdapter` cannot, because native ORD `Reaction` JSON has nowhere to say it.
  `JsonExportAdapter` can — its format is defined by that adapter and nowhere else, so an optional
  `retracted` tombstone on the entry being withdrawn is a report in exactly the sense the entry's
  own `timestamp` is one. That producer was built during this review and removed with the rest; the
  file-drop exclusion in the first draft of this ADR ("its only possible evidence is absence") is
  right about absence and does not cover an explicit field. Two properties it needs and is easy to
  miss: a tombstone must also be reported when the *file* arrived after the fetch floor even though
  the moment it names predates it (`is_late_arrival`, or a site that stamps the withdrawal date
  loses it permanently), and a bare `true` must be refused rather than filled in from the wall
  clock, because `retracted_at` *is* the fact.
- **`inner` on `_BoundedIngest`**, or the capability never reaches production. Six lines, and it is
  the rule `fetch_was_truncated`'s walk already documents, missed one wrapper up.
- **The unfiltered retrieval path.** `FingerprintReactionRetriever` must drop a withdrawn hit with
  no filter given, which is not the existing `eligible` call: that one also drops a hit with *no
  stored record*, and an unfiltered sweep legitimately surfaces reactions the transcription tier
  does not hold. It needs its own narrowing — "which of these are withdrawn" — not a reuse.
- **The agent-facing search.** `connectors.rxnfp`'s `similar_reactions` consults no store at all.
- **A visible tombstone.** `expand_note` and the rendered body must say a run was withdrawn, or the
  one reader who can still reach it by citation is told nothing.

**4. Not PR-gated, when it is built.** D-2026-08-25 governs and turns on whether anything was
*inferred*. A withdrawal reported by the source is a transcription of a fact the source system
states, exactly as the entry is; gating it is worse here than it was for the entry, because a
retraction queue leaves *withdrawn* data answering as current for as long as nobody drains it — the
gate would preserve the defect it was added to fix. The inverse keeps the gate honest: a retraction
**inferred** from absence would be an assertion, and decision 1 refuses to make one at all rather
than gating it.

**5. When it is built, the tombstone is `retracted_at`, not `valid_to`.** D-2026-08-25's argument
for omitting `valid_to` is sound and untouched: a *result* does not expire, and deciding it has
been superseded is a human claim belonging in a note. A source-reported withdrawal is neither.
Two consequences that are easy to get backwards: the upsert must never write `retracted_at` (a
soft-deleting source keeps exporting the withdrawn entry, so an ingest that refreshed the tombstone
would clear it on the first overlap replay), and nothing reverses a retraction, because an entry
reappearing in an export is indistinguishable from one that never left.

## What was kept

- **Migration `066`'s column, index and comment.** It is applied, it is unread, and it is
  deliberately not dropped: dropping it is a destructive migration that buys nothing, and a real
  implementation reuses it exactly as designed.
- **The wrapper fix found on the way, which was a live defect of its own.** `DatedIngest` — the
  normalisation wrapper `ingest/sources/registry.py` puts around **every** adapter it builds —
  declared no optional capability, and a `runtime_checkable` Protocol is structural: a wrapper that
  does not redeclare a method simply does not have it. So `fetch_was_truncated` answered `False` in
  every deployment, including for the warehouse adapter that implements `fetch_truncated` precisely
  so the workflow comes back for the truncated remainder. `DatedIngest.inner` and the walk inside
  `fetch_was_truncated` fix that, and `tests/test_eln.py` pins it. (The walk was a separate
  `_adapter_chain` helper while two capabilities used it; with one left it is inlined.)
- **An absence test.** `test_no_retraction_tier_claims_to_exist_without_the_readers_that_honour_it`
  fails whoever re-adds the storage half without the readers, and carries the three measurements so
  they do not have to be taken again. It exists to be deleted by whoever implements this properly.

## What was rejected

- **Mark-and-sweep over the fetch window**, the literal port the backlog row asks for. See decision
  1. `D-2026-08-27-a-retirement-rides-its-replacement` already refused the weaker version of this
  for the miners, where a truncated corpus read looked identical to vanished reactions.
- **Deleting the row** rather than tombstoning it. It loses the "readable as of an earlier time"
  half outright, and `durable/retention.py` refuses row deletion here for that reason.
- **Carrying the withdrawal only in the rendered body**, via the amendment path that already works.
  It reaches a human reading the record and changes nothing about what retrieval serves. (It is
  still *half* of what a working implementation owes — see decision 3's last item.)
- **Keeping the storage half and deferring the readers.** That is the state the review found, and
  the reason it is not a smaller version of the fix is decision 2's third row.

## Two other defects this review found, in work this branch shipped

**A retraction did not take effect for up to a day, and the two backends disagreed.** Measured
before the tier was removed: `ReactionRecord.is_current` compared `as_of >= retracted_at.date()`,
while the eligibility SQL asked `retracted_at > %(today)s` with `today` a **date** — which Postgres
coerces to that day's midnight, in the session's time zone. For a run withdrawn today at 12:00Z,
`eligible` returned it from Postgres and dropped it in memory, while `is_current(today)` was
already `False`; the durable store went on serving it for the rest of the day. Midnight is the one
value at which the broken form and the correct one agree, which is why the cross-backend parity
test that shipped with this ADR — written at `2026-03-04 09:00Z`, a date in the past — did not
catch it. It is recorded rather than fixed, because the column it compares is gone: a rebuild wants
one UTC frame on both sides, an explicit `timestamptz` parameter rather than a server-coerced date,
and a parity test whose stamp is *today* at a non-midnight time.

**`note_id_for_reaction(record_id, source="")` and `REACTION_SOURCE_SEPARATOR` are deleted.** No
caller in `src/` ever passed `source`; both real call sites pass one argument, and the docstring
admitted that "passing a source here today produces an id nothing can look up", because none of the
six readers that would have to resolve `reaction-<source>.<id>` accept it. A spelling no reader
accepts is a claim that two sites can be told apart in a citation, which is what `reject_widening`
was deleted for. The need is real and unchanged — `ingest.eln.records._one_of` still refuses a
two-site read rather than guessing, which leaves a chemist unable to open a run a search just found
— and it is a knowledge-graph *identity* change: the readers, the stored citations and the
validator move together or not at all. It starts from the six readers, not from the spelling.
