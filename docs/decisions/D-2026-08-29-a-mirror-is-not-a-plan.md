# D-2026-08-29-a-mirror-is-not-a-plan — the commitment model

**Status:** accepted · **Date:** 2026-08-29 · Sixth of the eight infrastructure findings from the
2026-08-28 audit (F4).

## Context

Nine of the nineteen `manager` bucket-C probes need one object this schema did not have: **a unit of
committed work**. `pl-29` (consolidated status), `pl-30` (what slips if we reprioritise), `pl-31`
(capacity), `pl-33`, `pl-34`, `rp-22`, `rp-27`, `rp-28` and `op-27` all bottom out in the same
absence.

Seventy-three migrations, and `project` is a nullable text tag on `reaction_records` — a facet on a
row, not an entity. There is no programme, no activity, no dependency, no milestone, no capacity,
and no person beyond an `actor` string. The system prompt is honest about it: *"no project,
programme, capacity, headcount or timeline data."*

The subtler half of the finding is why the existing seam could not carry it. `DataSource` is
**corpus-shaped**: its two halves turn records into chunks, notes, fingerprints and evidence. A
portfolio export is not a corpus — it is a set of typed entities with lifecycles — and ingesting one
through the corpus halves would land a milestone as searchable prose.

## Decision

### A mirror, and the restraint is the decision

`commitments` holds what a programme has committed to, **mirrored in from the system that owns it**.
The organisation already runs a portfolio tool and that tool is the truth. Nothing here plans,
schedules, levels resources or computes a critical path; a deployment that let it try would have two
answers to "when does this land", and the second one would be wrong more often.

What this adds is the one join no portfolio tool can compute: between a slipping milestone and the
*chemistry* slipping it. `note_ids`, `job_ids` and `compounds` are how the source states that link,
and they are the reason the mirror is worth keeping at all — a commitment with no link is a row the
portfolio tool already holds and holds better. `CommitmentSyncResult.linked_to_science` counts them,
so a deployment can see whether its export is supplying the only thing this table is for.

### The third half, not a fourth seam

`ingest/sources/base.py` already argues the case: *"The two capabilities are genuinely disjoint
today… so this seam does not merge them into one fat interface. It **composes** them."* A
commitments half is the third such capability and takes the same treatment — its own Protocol, its
own DTO, `commitments:` in the manifest, `SourceSpec.commitments` beside the other two.

The audit's finding (the seam is corpus-shaped; a portfolio export is not a corpus) is right and
does not imply a new seam. Adding a half costs a field. A fourth seam would cost a manifest, a
registry, a validator, a discovery path and a mental model an operator has to learn — for a thing
that is attached exactly the way the other three are.

### Four rules the tier keeps

1. **Read-only.** `ingest/sources/README.md`'s rule — a source "cannot acquire a write path by
   declaring one" — is unchanged. Mirroring a milestone in does not confer the ability to move one;
   writing back belongs to the effector seam. An absence test holds it in both places: no
   write-shaped verb on the Protocol, and the agent tool does not reach the store's writer.
2. **It converges rather than accumulating.** Upserted on `(source, external_id)` — two systems may
   both call something `PRJ-14`, and a bare id would silently merge them, which is the same argument
   `D-2026-08-27-a-fingerprint-is-keyed-by-its-source` makes one table over. A full re-read is
   therefore free, which is what lets `fetch_commitments` ignore its watermark: a portfolio extract
   is a snapshot, and filtering one by a cursor on this side would drop rows whose *state* moved
   without their file being rewritten.
3. **Every reading reports its own staleness.** A mirror's characteristic failure is being stale
   rather than wrong: the export stops running, the numbers keep answering, and a manager acts on
   last month's picture. `observed_at` travels on the answer rather than being something a reader
   has to think to ask for — the argument `chemclaw.operations.Coverage` makes about a window. And
   `outstanding` and `mirror_freshness` are separate calls because an empty list has two meanings:
   nothing is due, or nobody has ever mirrored anything. Conflating them is how "nothing is late"
   gets read out of a sync that never ran.
4. **It infers nothing.** A row missing a required field is rejected and counted rather than
   repaired. A mirror that guessed a due date would be asserting a plan.

### What is deliberately absent

- **No mapping from `owner` to an Entra principal.** The owner is a name in the *source's*
  namespace. A mapping this system invented would be a second directory, and a wrong entry would
  attribute somebody else's work. `agent/leaver.py` therefore files this column under
  `_BEYOND_REACH` rather than erasing on a string match — and the row is not this system's to delete
  anyway, since the next sync would restore it.
- **No `parent_id` foreign key.** A mirror receives rows in whatever order the export produces them,
  and a constraint would reject a child that arrived before its parent, turning a partial sync into
  a failed one.
- **No retention window.** A mirror that converges is bounded by the portfolio it reflects, and a
  clock cutoff would delete the delivered rows that make "what did we ship last quarter"
  answerable — the question the table was added for.

## Consequences

- `pl-29` and `rp-27` move C → **B**, and their directions now name precisely what is still absent:
  the mirror is a snapshot, so *"what moved last week"* has no previous state to diff against, and
  *"at risk"* is a judgement rather than a field — past-due and blocked are facts, and the honest
  answer lays them out rather than manufacturing a risk score.
- `pl-30`, `pl-31`, `pl-33`, `pl-34` and `rp-28` stay **C**. They ask for re-planning, capacity and
  resource levelling, which is exactly what this decision declines to do.
- A Schedule exists only where an enabled source declares a `commitments:` half, daily rather than
  hourly — a portfolio tool's dates move on a human cadence, so a tighter loop would spend a
  vendor's API budget to learn nothing. And no Schedule here opens a pull request, which is what
  makes running it on a timer safe at all (`D-2026-08-25-an-eln-transcription-is-data-not-a-claim`).
