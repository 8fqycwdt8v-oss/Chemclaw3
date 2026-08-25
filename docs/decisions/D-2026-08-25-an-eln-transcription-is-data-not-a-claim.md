# D-2026-08-25-an-eln-transcription-is-data-not-a-claim — a deterministic ELN transcription is a row, not a knowledge claim

Every ELN entry was rendered as a `created_by: agent` markdown note and pushed through the D-005
PR-gate for a human to merge. That is withdrawn. A transcription is now a row in
`reaction_records`, readable the moment it is ingested, and no scheduled job opens a pull request
at all.

## What was measured

The change was argued from numbers rather than from taste. All figures are from this repository on
2026-08-25.

| | measured |
|---|---|
| LLM calls in the ingest path | **zero** — 84 µs and 940 bytes per entry (RDKit + pydantic + string templates) |
| `propose_note` → branch + worktree + commit + push | **202 ms/note** median, against a *local bare remote* with no network and no PR-creation API |
| gate throughput | **4.9 notes/s**, serialized by a module-level asyncio lock **and** an `flock`, one submitting process per host by design |
| `_merged_note_bodies()` corpus scan | **425 µs and ~2.9 kB resident per note**, linear, run once per chunk that touched a replay |
| git refs at 100k branches | 1.8 s for `branch --list`, 15 MB — **not** the bottleneck (a suspicion checked and disconfirmed) |
| corpus this had ever run against | **39 notes, 2 ELN fixtures** |

Two consequences follow arithmetically.

**A hard wedge at ~700k entries.** The corpus scan alone reaches 300 s at 705,000 notes, which is
`eln_sync_timeout_seconds`. Past that every activity attempt times out and retries forever.
`eln_sync_batch_size` does not help: it bounds *new entries* per chunk, while the scan is O(all
merged notes).

**A review queue nobody can drain.** One PR per entry, one human merge each. At 30 s of pure
clicking that is 4.2 person-years per million entries; at five minutes — what reading a procedure,
a charge sheet and an impurity profile actually takes — 42 person-years.

## Why the gate was wrong here specifically

D-005 exists to put a human in front of *machine-generated knowledge* — job results, distilled
playbooks, campaign narratives, report drafts. Things a model **asserted**.

`record_from_ord_reaction` (was `note_from_ord_reaction`) is a pure deterministic mapping. It
infers nothing. The reviewer was approving a rendering of data a chemist had already signed off on
in the source system — a `str.split()`, essentially. The gate's cost was real and its yield was
approximately zero.

Worse than useless, in fact: three hundred rubber-stamp merges a day is the training regime that
produces a reviewer who also rubber-stamps the distilled playbook that *did* need reading. The gate
was spending its credibility on the cases that did not need it.

The repository had already made this argument twice, in the other direction, and simply not applied
it here:

- `ingest/eln/ingest.py` writes fingerprints ungated because they are "a deterministic serving
  index" — the same property, asserted of two of the three writes in the same function.
- `D-2026-08-06-a-share-is-mounted-not-called` admits a mounted share's documents as **cited
  evidence** rather than PR-gated notes.

`DEFERRED.md`'s OCR row poses exactly this question — "either PR-gated like a note, or admitted as
evidence carrying a machine-read marker" — and treats it as open. For the ELN path it had been
answered one way without ever being asked.

## What was rejected

**An LLM extraction pass over the free text.** The transcription preserves *what was done* and
loses *what was learned*: `_segment_steps` is a sentence split, `_classify` is a substring keyword
match, `hypothesis` and `failure_reason` are read only if the source already has those named
columns, and the canonical schema has **no field at all** for observations, conclusions or
discussion. A model reading the prose would cost roughly $4.8k (Haiku 4.5) to $9.6k (Sonnet 5)
batched for 3M entries — three orders of magnitude less than the human queue it would replace, and
it would give the gate something real to review.

It is declined anyway, as a cost the owner is not willing to carry. Recorded here because the
finding is durable: **the extraction is the cheap part and the human gate is the expensive one**,
which is the inverse of the intuition, and a later decision to revisit should start from that
rather than re-deriving it.

The consequence stands and is not hidden: **the chemist's actual insight is still not captured.**
It sits in an ELN field nobody mapped, or lands in the untyped `attributes` bag. What a human
asserts about these runs still has to be written by a human, as a playbook or a campaign, and
nothing proposes one.

## What changed

**The record.** `ingest/eln/note.py` → `ingest/eln/record.py`; the same block renderers, returning
a `ReactionRecord` instead of a `Note`. `ingest/eln/records.py` is the tier — a Protocol with an
in-memory and a Postgres backend, shaped like `science/fingerprints/store.py` and for the same
reason. Migration `052`. Upsert-by-id *is* the idempotency, which is what deletes the corpus scan
and the wedge with it: the unchanged-entry check is now keyed on the batch, not the corpus.

**Reactions left the graph's id space.** `dangling_links` no longer reports `[[reaction-<id>]]` as
broken (`kg.note.resolves_outside_graph`), because `memory.campaign` and `memory.optimization` cite
every run that way and would otherwise fail `kg-validate` en masse. The cost is stated rather than
hidden: offline validation can no longer tell a real run id from a typo'd one. That is bought back
by `cli/validate_kg.py`, which checks citation existence against the store — and CI runs with a
Postgres service, so the check really runs rather than being a claim in a docstring. When the
database is unreachable it prints `NOT CHECKED` and the count, because a validator that silently
skips is indistinguishable in a log from one that found nothing.

**D-018's failure mode is gone.** `expand_note`'s own docstring recorded it: a fingerprint hit on a
reaction whose note had not been merged raised an error "the chemist can otherwise not distinguish
from a typo". Index entry and record are now written by the same call, so there is no review step
between them to fall through. The graph is still consulted **first** — `reaction-` is a prefix, not
a reservation, and a human-authored note under that name must still win.

**No Schedule opens a pull request.** `campaign-synthesis`, `playbook-distillation` and
`optimization-campaign` are removed from `planned_schedules()`, and the observations tier's
`promote` step is split into `ObservationPromotionWorkflow`. The miners are **unchanged** and still
run — they read `list[OrdReaction]` straight from the ingest sources, never from notes — they are
started on demand now, by a person or by an agent workflow with a reason to look. Mining and
retirement stay on a timer because they write ungated rows and cost no review.
`test_no_scheduled_job_opens_a_pull_request` asserts the rule over whatever the plan returns, so a
Schedule added later for a note-proposing job fails rather than quietly restoring the behaviour.

**Two layering edges were removed by moving code, not by declaring them.** `ingest` depends on
`kg` and on `retrieval`, so a store in `ingest` that those packages read is a cycle.
`retrieval.retrievers.ReactionMetadata` and `kg.validate.RecordExistence` are one-method Protocols
each consumer declares for itself; the concrete store satisfies both structurally, and the caller
supplies it. `FingerprintReactionRetriever`'s `records` argument is **required** rather than
defaulted, because a default reaching for the production store would be the forbidden import
wearing a different hat.

**`ProcessConditions` rides on the row.** `D-2026-08-25-the-structure-is-discarded-at-the-note-boundary`
landed on `main` while this was in flight and put a run's figures into frontmatter as numbers,
arguing for that over "a second table". The argument survives the move rather than being overridden
by it: what it rejected was a store *just* for conditions, and the reaction row already exists here,
so the numbers ride on it and there is still exactly one place a run's figures live.
`agent/protocol_tools.condense_protocols` gains a record fallback beside its share-document one —
without it every reaction reference would read as `missing`, which is the same silent hole
`_from_share` was written to close, arriving from the other side.

## What was kept, and why

- **The slug rule.** `ReactionRecord.reaction_id` still validates through `kg.note.require_note_slug`
  (extracted, not copied). An entry id is no longer a filename, but it is still the
  `reaction-<id>` citation that campaign notes carry into git, so external JSON reaches a committed
  note body through here. Dropping the constraint because the storage changed would have been a
  silent widening — it was caught by an existing test, not by review.
- **`_without_wikilinks`.** A transcription that could spell `[[contradicts:reaction-9]]` would let
  an ELN forge a graph edge. This matters *more* now, not less: the body is served verbatim by
  `expand_note` and quoted into report drafts, and nothing reviews it on the way.
- **`knowledge/reaction/*.md`.** The six seed notes are `created_by: human` — hand-written knowledge
  that playbooks cite, which never touched the gate. They use bare `rxn-*` ids, so they do not
  collide with the `reaction-` citation namespace.
- **The fingerprint index, untouched.** Structural search is the capability this change existed to
  protect, not to trade away.

## Consequences

- "Have we run this before / same product / similar reaction" answers in full, with the procedure,
  with no PR in the loop.
- An amendment overwrites its row. Git diffs used to be the versioning scheme for a corrected yield;
  they are not needed for something that asserts nothing, and `last_seen` records the touch.
- `IngestSummary.awaiting_merge` is gone. It reported entries stuck in a review queue that had not
  moved — an honest signal about a queue that no longer exists.
- The transcription tier is **not** subject to retention (`infra/sql/README.md`): a row is the only
  readable form of a run, so pruning one deletes a result.
