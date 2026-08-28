# D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask — a refused record is a question somebody will ask

**Status:** accepted · **Date:** 2026-08-27

## Context

A record an ingest source offers and this system refuses leaves a `WARNING` and nothing else.
`sync.py` builds a `RejectedEntry` per refusal, reports it in the run summary that the Temporal
activity returns, logs it, and drops it: the summary is a return value, not a row.

The seeded corpus has exactly one such record, and it is the one a chemist actually asks about.
`santanilla-orgsyn-boronate-well-Y36` carries `yield_percent = 119.43`; `OrdReaction` bounds
`yield_percent` at 100, so the entry is refused on every run and can never arrive. `gr-08` in
`data/evals/probes/grounded.yaml` is written against that absence and says so in its own comment:
today the probe measures whether a model admits it cannot find the well, and the best answer
available is "I have no such record".

The better answer exists and is unreachable: *that well was rejected on ingest because a yield
cannot exceed 100%; the value is what an uncalibrated relative-UPLC readout does.* The first half
is a fact this system holds — it saw the entry and refused it — and the only place it lives is a
log file no tool can query.

Two smaller absences have the same shape. `warn_late_arrivals` aggregates files that arrived too
late for any run to fetch into one bounded log line, with the explicit note that a permanently-late
file re-qualifies on every run; that aggregate has no home either. And a file this adapter cannot
read at all never reaches the sync report by construction.

## Decision

**A refusal becomes a row.** `infra/sql/065_ingest_rejections.sql` creates `ingest_rejections`,
keyed `(source, entry_id)`, carrying the reason, `first_seen`, `last_seen` and `occurrences`.
`src/chemclaw/ingest/rejections.py` owns its writes and its one read.

**It is a ledger, and the key is what makes it one.** A record refused on every sync run is one row
with a moving `last_seen` and a rising `occurrences`, not a growing trail. "Is this still
happening, and since when" is then a row rather than a count over rows — and the table cannot grow
with the number of runs, only with the number of distinct bad records.

**Growth is bounded by the writer, not by a retention sweep.** At most `_MAX_ROWS_PER_SOURCE`
(1,000) rows survive per source; a write evicts the least recently refused inside the same
transaction, and a reason is truncated at 500 characters with the cut marked. The case that would
otherwise write millions of rows is a corpus with one systematically broken field, and it is
exactly the case where the newest refusals are the informative ones: an aged-out row is a defect
nothing has re-offered since. No sweep runs often enough to be the answer to a source refusing
every record it holds, which is why the bound is in the write path. This is the only DELETE any
code issues against the table, and the grant says so.

**The reader is `gather_evidence`, not a new tool.** Every advertised tool is paid for on every
turn (`tests/test_context_floor.py`), and the question this answers — "is our data any good" — is
the question `gather_evidence` already receives. It returns `EvidenceSweepWithRefusals`, a subclass
adding `refused_on_ingest` and `refusals_unavailable`.

**A rejection is shaped so it cannot be read as a result.** `IngestRejection` carries no yield, no
structure, no conditions and no body — nothing a `ReactionRecord` carries — and a
`kind: "ingest-rejection"` literal, because a pydantic tool return reaches the model as its `repr`
(`tests/test_upstream_surface.py`), so the discriminator travels with the object into the prompt.
The tool's docstring, which is the model's contract, states that these entries are records that are
*absent* and must never be reported as found. Folding them into `chunks` as a retriever would have
been the opposite decision: an `EvidenceChunk` is cited evidence, and citing the 119.43% well would
hand a chemist a yield the system refused to believe.

**An unreachable ledger is reported, never rendered as "nothing was refused".** `refusals_matching`
raises; the tool catches, returns the reason in `refusals_unavailable`, and answers from the sweep
regardless. The write is the opposite half of the same rule and swallows its own failure, because a
side record about a run may not cost the corpus the entries that mapped cleanly.

## Where the writer is wired, and where it is not

The writer is in `OrdJsonAdapter.fetch_new_entries`, covering all three of that adapter's refusals:
a file it cannot read, a file that arrived too late for any run to fetch (the `warn_late_arrivals`
aggregate, now with a home), and a message that cannot be mapped.

**The third is why the recording happens in the fetch rather than at the raise.** A ledger write is
awaited and `ElnAdapter.map_to_ord` is synchronous by contract, so the entries a fetch is about to
hand over are mapped once inside it to find the ones that cannot be. That costs one pure-function
call per entry — **measured at 65 µs** over the shipped ORD example, ~6.5 ms on a full
100-entry `eln_sync_batch_size` chunk, and the same cost `sync.py::_replay_record_ids`
already pays for the same structural reason —
and changes nothing about what is returned: the sync maps them again, refuses the same ones, and
remains the sole author of the run summary. The alternatives were worse: a blocking write inside
`map_to_ord` stalls the event loop, and a buffer flushed on the next fetch loses the last run's
refusals whenever the process exits.

**Three refusal sites are deliberately not covered here, and the omission is the point of saying
so.** `JsonExportAdapter.map_to_ord` (the free-text adapter — and the adapter the live 119.43%
record actually arrives through), `sync.py`'s future-timestamp rejection, and `ingest_reaction`'s
`IngestError` all refuse records this ledger does not see. The single site that would cover every
one of them is `durable/eln_sync.py`, which already holds `IngestSummary.rejected` — entry id,
reason and timestamp — for every source and every adapter, in an `async` activity, with no
pre-flight needed. **That is where this belongs next**, and it is one call.

## Consequences

- A data-quality question is answerable: `refusals_matching` finds a row by the words a chemist
  uses, because the match is substring-based on the question's distinctive words. `119` has to
  reach `input_value=119.43`, which no tokenised full-text match makes.
- The match is deliberately loose. A word qualifies if it carries a digit or is at least five
  characters, so an occasional irrelevant refusal surfaces — and says plainly what it is. The
  opposite error, a ledger holding the answer while the match is too strict to find it, is the
  failure the ledger exists to end.
- `gr-08` can be re-graded against its own comment: the probe still forbids describing the well as
  found in the corpus, and a good answer can now name the refusal instead of only admitting the
  absence.
- `tests/test_ingest_rejections.py` proves the refusal lands with its reason, that a re-offer moves
  `last_seen` without duplicating the row, that a clean entry leaves nothing, that the gr-08
  question reaches the row through the tool, and that the per-source cap holds.
- The ledger's `source` is the registry source name, and the ingest half is never told its own
  name — so `ord_adapter.LEDGER_SOURCE` is a constant that could drift from the manifest. A test
  reads every `datasource.yaml` naming this adapter and fails if it does.
