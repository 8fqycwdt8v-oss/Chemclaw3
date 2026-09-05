# D-2026-09-05-a-reader-with-no-caller-passes-its-own-tests — six defects, one shape

**Status:** accepted · **Date:** 2026-09-05

Four fresh-context reviews of `D-2026-09-05-the-cut-the-gate-could-not-see`'s changes, run against
the branch rather than against its description. They found no broken code and six defects, and the
shape they share is worth naming before the list: **every one of them is a component that works
when called directly and is never called that way in production.** A unit test that constructs the
component and asserts on it is evidence about the component; the question that decides whether a
column is populated, a gate is scoped or a number is real is *who calls it, with what*.

## The turn record wrote three columns nothing ever filled

`turn_costs.answer_confidence`, `review_required` and `notes_cited` are read off the turn's
`AnswerEvent` in `_TurnLedger.note_event`, described in the migration and in the ledger's own
comment as "the one place the counts are taken". They were `NULL/false/0` on every row, because the
`AnswerEvent` is **built** in `run_turn` (`build_answer_event`) rather than streamed, and
`note_event` is fed by `_stream_into` over the graph's event stream. The one event those three
columns exist for is the one event that does not pass through the place that counts them.

Every test in `tests/test_turn_knowledge.py` called `note_event(AnswerEvent(...))` directly and
passed. `ledger.note_event(answer)` now sits immediately before `yield answer` — before, for the
same reason `ledger.answered` is: the cancellation that reaches a finished turn is delivered while
suspended in that yield. The test that holds it drives `run_turn` end to end and asserts what
`record_turn_cost` was handed, against the yielded event rather than against literals.

## `capture_calls` counted 49 tools, six of which write knowledge

It was derived from `side_effecting_tools()`, on the argument that a derived set beats a stated one
because a bundle's write tool is then counted the day it is enabled. Measured, that set spans 49
tools: a turn that computed one xTB energy booked `capture_calls = 1`. The column then answers "did
this turn call a state-changing tool", which `tool_calls` minus the reads already approximates, and
not "did this turn write anything back", which is the question no other column can answer.

`KNOWLEDGE_WRITE_TOOLS` is the stated subset, symmetric with `KNOWLEDGE_READ_TOOLS`, held inside
`side_effecting_tools()` by a test. **A derived set is only better than a stated one when the
derivation answers the question being asked**, and the generality traded away is smaller than it
looks: a connector bundle cannot reach the knowledge graph or the memory tiers at all.

## `retrieval_calls` could not see a connector's searches

The other half of the same defect, in the other direction. `rxnfp` serves seven searches over the
reaction corpus and `molfp` two over the fingerprint index; a turn that answered entirely from
`substrate_precedent` booked `retrieval_calls = 0`. Naming those nine in core would be the copy of
somebody else's classification D-118 exists to prevent — a bundle's tools are the bundle's fact —
so a manifest now declares `knowledge_read`, a subset of its own `read_only` (a tool listed there
and in `state_changing` is refused: that pair is a write counted as a look). `knowledge_read_tools()`
is the union, cached and cleared exactly like `side_effecting_tools()`.

It is **optional** where the `read_only`/`state_changing` classification is a mandatory partition,
and the asymmetry is deliberate: getting that partition wrong fails open, into a write the plan gate
reads as a read, while omitting `knowledge_read` understates a metric.

## `NOT NULL DEFAULT 0` asserted a measurement about every row taken before it existed

Migration 082 defaulted `retrieval_calls`, `capture_calls`, `notes_cited` and `review_required`.
Zero is the most interesting value those columns can hold — a turn that answered without consulting
the record — so the default backfills that finding onto the entire history of the table, and a query
for "turns that answered blind" returns rows nobody measured. The file's own paragraph about
`answer_confidence` argued exactly this and then did the opposite one column over. All five are
nullable now; a row written by the image that has the columns always supplies a value, so NULL means
one thing.

**As migration 083 rather than an edit to 082, and CI is what insisted.** `core/migrate.py` keys on
a checksum of a migration's *statements*, so editing an applied file breaks `make db-migrate` on
every database holding it — including the dev database this branch's own author had already
migrated. `tests/test_migrations_are_additive.py` asks git the same question, is skipped on the
sandbox's truncated clone and ran on CI's `fetch-depth: 0`. "It is unmerged, so nobody has applied
it" is not the test's question and must not be: who has applied a file is unknowable from inside the
repository, which is exactly why the rule is mechanical. 083 also nulls the rows 082's backfill
wrote a measurement onto, with a predicate that cannot be exact — the file states the residual case
rather than denying it.

## The compaction citation index read from any tool result, and named ids nothing can resolve

`ClearOlderToolResultsEdit` reads `source_note_id='…'` out of a result's repr before upstream deletes
the bodies, so a cleared evidence sweep still names its sources. Two holes:

- It read from **every** `ToolMessage`. A connector's payload is text an external server wrote, so a
  result containing that literal would be quoted back inside a *system-authored* placeholder as a
  citation, with an instruction to `expand_note` on it. Framing does not reach this — `defang`
  neutralises delimiters, not the contents of a field this module greps. Scoped now to results whose
  **calling `AIMessage`** names a `KNOWLEDGE_READ_TOOLS` tool: not `ToolMessage.name`, which three
  `wrap_tool_call` middlewares rebuild, and a scope that depends on a field surviving three rewrites
  fails open — silently naming nothing, which is this defect's own shape.
- A chunk's origin is only sometimes a note. The mounted share writes `<share>:<doc>#<ordinal>`, the
  warehouse ELN `<source>:<key>`, a vendored dataset `vendored:<name>:<index>`. Filtered through the
  note-slug rule, now `kg.note.is_note_slug` — the predicate half of `require_note_slug`, extracted
  rather than restated.

## The stored text derivation changed and its version stamp did not

`_NOTE_TEXT_VERSION` guards "the text a fresh index would store differs from the text an existing row
holds", and its comment said "bump whenever `search_text`'s composition changes". `upsert` normalises
`record.text` on the way in, so the sign anchoring changed the stored derivation as surely as a
composition change would, and every existing `lexeme` stays built from the old rule. Bumped to
`ntv3`, which empties the fingerprints the reindex compares against and rewrites every row. The
lexical read cannot filter on this key — `ts_rank` has no notion of an embedding — so **the bump is
not the repair; it is what makes the next reindex perform the repair.**

## `restated_as_position`'s exemption for the default mode

Applied to `hybrid` only, on the argument that round-robin preserves each source's ordering so a
chunk's score still explains its position "within the list it came from". The model is handed one
interleaved column. Measured over the shipped corpus in `graph` mode, that column was monotone with
the delivered order on **2 of 7** queries, and both of those returned fewer than three chunks. The
exemption's second argument — that restating deletes KM-5's truncation signal — was wrong twice: the
score orders a source's own list *inside* the retriever, which has already happened, and the note's
confidence is a field of its own. It applies in both modes now, and the test that pinned the
exemption pins the behaviour.

## Four errors in the eval corpus, three of them contradictions with itself

The 48-note corpus grown in the previous ADR was written for retrieval shape and not read as
chemistry. The gold Suzuki run recorded 80 °C at "reflux in THF/water" — 66 °C, as the corpus's own
THF note says — with K2CO3, while the campaign that points at it as its written-up winning point
names 80 °C and K3PO4. A Heck "at reflux" in DMF at 110 °C, where DMF boils at 153. A Dean–Stark trap
on an ethanol esterification, which separates nothing: ethanol boils below water and is miscible with
it. A Negishi whose organozinc was inserted into "the aryl bromide" and then coupled onto
4-bromotoluene, which is a self-coupling to the symmetric dimer rather than the biaryl the Suzuki
route makes. Corrected so that the temperature follows the solvent in each case; the baseline is
regenerated and `make eval-baseline-check` is green.

**Two of the four were inside gold sets**, which is the part that matters: a retrieval gate whose
gold notes are chemically wrong measures ranking correctly and teaches the wrong thing to anyone
reading the corpus to understand what good evidence looks like.

## Numbers that were true about an earlier commit

The context-floor figures in the previous ADR (43,316 → 43,333) were measured on a basis
`D-2026-09-05-a-ratchet-that-re-derives-half-its-basis-bounds-half-a-request` replaced two days
later. Re-measured against `origin/main` on the observed basis: **43,954 → 43,971, the same +17**.
And the decision ledger attributed "24 chunks against a cap of 40" to the shipped configuration —
measured, the shipped configuration runs **one** note leg and delivers at most `retrieval_top_k` 8;
24 is the three-note-leg hybrid nothing ships.

## Consequences

- A bundle that searches the record declares it. Two do today; a third costs one manifest key.
- `capture_calls` and `retrieval_calls` change meaning for anyone who has already queried them.
  Nothing has: 082 has never been on `main` without 083 beside it, so no deployment holds a row
  written under the old meaning.
- Every deployment re-embeds its note corpus on the next reindex, which is the `ntv3` bump working.
- The `graph` mode's evidence list reports rank rather than the finder's number. A reader wanting a
  note's confidence reads `EvidenceChunk.confidence`, where it has always been.
