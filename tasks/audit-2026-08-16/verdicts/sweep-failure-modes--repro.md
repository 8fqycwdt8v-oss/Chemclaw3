# Adversarial verification — `sweep-failure-modes.md` (lens: does it actually reproduce?)

In scope: the two findings marked **high**. Findings 3–5 (medium/medium/low) were not examined.

Every reproduction below is my own script, written from the source. I did not run the reporter's
`/tmp/repro_*.py` and did not accept any transcript in the findings file. Where the reporter used a
hand-written stub, I preferred a *genuine* trigger (a real unreachable Postgres, a real
`GitSubmitError` from the real submitter's own error type, a contract-abiding adapter).

---

## An evidence source that fails is reported to the model as "nothing on file"

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**:

  **(a) The genuine trigger, no patching of any internal seam.** I enabled the shipped `lexical`
  data source and pointed the configured DSN at a closed port, then called the real tool:

  ```
  $ CHEMCLAW_DATA_SOURCES=lexical \
    CHEMCLAW_POSTGRES_DSN='postgresql://chemclaw:chemclaw@127.0.0.1:59999/chemclaw' \
    uv run python /tmp/v1_fanout.py
  data_sources = lexical
  sources asked: ['lexical']
  ERROR ... evidence source 'lexical' failed; the sweep continues without it
    File "src/chemclaw/retrieval/fanout.py", line 99, in _sweep
    File "src/chemclaw/retrieval/retrievers.py", line 417, in retrieve
    File "src/chemclaw/retrieval/vector_index.py", line 428, in search_lexical
    File "src/chemclaw/core/db.py", line 133, in connect
  ConnectionError: Postgres unreachable at ... port 59999: connection failed: Connection refused
  TOOL RETURNED: []
  type: <class 'list'> len: 0
  ```

  `gather_evidence` — the tool the model calls, by its registered name — returned the empty list.
  No exception, no marker, no second return channel. The stub in the finding was not load-bearing:
  a real down database produces the identical result.

  **(b) The "indistinguishable on the live stream" half.** I drove `sweep_sources` inside a real
  compiled parent graph and drained `astream(..., stream_mode="custom", subgraphs=True)`, which is
  how `api/graph_stream.py:218` receives these:

  ```
  $ uv run python /tmp/v1_stream.py
  ERROR ... evidence source 'graph' failed; the sweep continues without it
  CUSTOM STREAM EVENTS: [(('n:…',), {'evidence_source': 'graph',    'chunks': 0}),
                         (('n:…',), {'evidence_source': 'lexical',  'chunks': 0})]
  ```

  `graph` raised `ConnectionError`; `lexical` ran cleanly and matched nothing. The two payloads are
  byte-identical. `api/events.py:415-417` declares the event as exactly `{type, source, chunks}` —
  there is no field a surface could read to tell them apart.

  **(c) Backstops.** `grep` confirms `verifier_enabled: bool = False` (`core/config/llm.py:117`) and
  `answer_shape_gate_enabled: bool = False` (`:139`). Reading `agent/verifier.py:435-468`: every
  branch sets `review.review_required = True` and returns the review — the answer is never withheld.
  `grep -rn "chemclaw_evidence_source_failures_total" src/` finds one writer and no reader, and
  `core/metrics.py:358` gives it no labels; it does **not** pass through
  `core/metrics_bridge.degraded()`, so it is absent from `chemclaw_degraded_total{subsystem=…}`.

- **Why**: The claim reproduces exactly as stated, on a real trigger, at the tool's public surface.
  Line numbers and symbols are current: `fanout.py:98-105` is the `try/except Exception/chunks=[]`
  with the fall-through to `_report(name, len(chunks))` at 104; `gather_evidence` is at
  `research_tools.py:152-239` and its docstring does say *"Empty is a valid answer (nothing on
  file), never invented."* The module docstring's own claim ("telling those two apart is what
  D-2026-08-01 needed and did not have") is falsified by (b) — the branch report cannot separate
  *returned nothing* from *raised*, and `_sweep`'s "never swallowed silently" is true only of the
  server-side log.

  **Two things that make it worse than reported**, which I add rather than subtract:

  1. The two "repeats" the finding cites — `ingest/documents/retriever.py:151-174` and
     `ingest/eln/warehouse/retriever.py:105-130` — are not merely the same swallow one level down.
     They catch broadly and `return []` *before* the fan-out ever sees an exception. So for the
     sharedrive and Snowflake-ELN sources the failure does not even increment
     `chemclaw_evidence_source_failures_total` and does not produce `_sweep`'s
     `logger.exception`. Those two sources fail *more* invisibly than the case I reproduced.
  2. The failure counter carries no `source` label (`core/metrics.py:358` labels only
     `…_chunks_total`), so even the server-side signal cannot say which source was down.

  The one caveat worth recording, which does not change the verdict: the **default** source set is
  `graph,eln-json` (`core/config/sources.py:45`), of which only `graph` retrieves, and it reads the
  filesystem — I checked that a malformed note file does *not* make it raise (it returned `[]`
  cleanly). So on a stock config the trigger needs a filesystem fault; every network-backed source
  (`lexical`, `vector`, `sharedrive`, `eln-snowflake`) is a deployment switch away, and the finding's
  stated trigger list is about those. The mechanism is source-agnostic either way.

---

## A transient git outage is filed as per-entry bad data, and the ELN cursor advances past the lost entries

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**: `/tmp/v2_eln.py` — my own script: five well-formed, mass-balanced `OrdReaction`s
  stamped 2026-01-01…01-05, real `InMemoryFingerprintStore`s, a submitter raising the real
  `chemclaw.kg.git_submitter.GitSubmitError` with a real push-failure message, and an adapter that
  **honours its own contract** (`fetch_new_entries` filters at the floor — the reporter's did not
  matter here, but a non-filtering adapter would have hidden the loss, see below).

  Run 1, one transient git outage:

  ```
  ingested : []
  rejected : [('rxn-1', "git push origin note/x failed: fatal: unable to access 'http"), … x5]
  SINCE        -> 2025-12-01 00:00:00+00:00
  NEXT CURSOR  -> 2026-01-05 00:00:00+00:00
  cursor advanced past all 5 entries: True
  reaction fp rows: {'rxn-1': FingerprintRecord(id='rxn-1', label='CCO.CC(=O)O>>CCOC(C)=O', …),
                     'rxn-2': …, 'rxn-3': …, 'rxn-4': …, 'rxn-5': …}
  ```

  Five structural index rows, zero notes, cursor at the newest entry — reproduced.

  Then the part the finding asserts but does not demonstrate: I ran the **next** sync from the
  persisted cursor with a *working* submitter, at the shipped default
  `eln_sync_overlap_seconds = 86400.0` (`core/config/eln.py:30`):

  ```
  RUN2 since   : 2026-01-05 00:00:00+00:00
  RUN2 ingested: ['rxn-4', 'rxn-5']
  PERMANENTLY LOST: ['rxn-1', 'rxn-2', 'rxn-3']
  ```

  A single sync run overlapping a transient git failure permanently drops every entry in the batch
  older than 24 h before the advanced cursor. No further outage is required — one run is enough.

  I confirmed the cursor really is persisted: `durable/eln_sync.py:244-251` appends the summary and,
  `if since is None`, executes `store_sync_cursor` with `chunk.summary.next_cursor` unconditionally;
  the workflow docstring and `durable/schedules.py:106` confirm scheduled runs pass `since=None`.
  The activity **succeeds**, so `BAD_DATA_RETRY` never engages.

  Recovery path: `grep -rn "ProposalState.FAILED" src/` returns two sites — `proposal.py:288`
  (writing it) and `proposal.py:187` (a transition applied only when the same content is
  *re-proposed*). There is no resubmitter, no CLI, no route. Re-proposal requires a re-sync, which
  the advanced cursor prevents. `grep -n "retry|attempt" kg/git_submitter.py` shows no retry inside
  the submitter either.

  Orphaned citations: `retrievers.py:246-255` builds the chunk with
  `source_note_id=note_id_for_reaction(match.id)` for every hit, and `_eligible` filtering runs only
  `if wanted` (a metadata filter was given). An unfiltered `similar_reactions` sweep therefore cites
  `reaction-rxn-1` for a note that will never exist.

- **Why**: Every link in the chain executes as claimed, on the real error type, at shipped defaults,
  and I measured the consequence the finding only asserts (three of five experiments gone after one
  run). Symbols are current; line numbers are off by one in two places and I note it only for
  precision — the `except (ChemclawError, ValidationError)` is at `sync.py:217` not 216, and the
  persistence block is `durable/eln_sync.py:244-251` not 245-250. `cursor = max(cursor, window)` is
  at 183 as stated, and it does run before the `try`.

  Nothing upstream bounds it. The `IngestSummary` docstring's stated invariant ("a rejection is
  deterministic bad data — re-fetching it would only re-reject it") is exactly what a
  `GitSubmitError` violates, so the docstring is a claim the code does not keep.

  **Worse than reported**: the finding lists the flock case last, and it is the most reachable
  trigger of all. `kg/git_submitter.py:95-106` takes a **non-blocking** `LOCK_EX | LOCK_NB` and
  raises `GitSubmitError("note_repo_dir is in use by another process …")` immediately on contention.
  So an ordinary agent turn proposing a note, or a document sync, running concurrently with the
  scheduled ELN sync converts that whole chunk into `rejected` and advances the cursor past it — no
  network outage, no infrastructure failure, just two processes sharing a clone, which is a
  configuration mistake the code explicitly anticipates and reports as a per-entry data defect.
