# Verification (lens: does it actually reproduce?) — kg / retrieval / memory — design

Two findings in scope (`high`). Both reproduce; I wrote my own scripts from source and ran the
durable path on a real Temporal server rather than reading the reporter's transcript. Scripts under
`/tmp/vrf/`. Working tree untouched (no source mutated; `git status` clean apart from other agents'
verdict files).

---

## The report path fans out over sources a second time, and its copy has no failure isolation

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. **Unit level, my own retrievers, both paths side by side** (`/tmp/vrf/f1.py`): one healthy
     retriever (`name="graph"`, returns 1 chunk) and one raising (`name="vector"`,
     `RuntimeError("pgvector unreachable")`), fed to `gather_section` and to `sweep_sources`:

     ```
     gather_section RAISED RuntimeError: pgvector unreachable
     evidence source 'vector' failed; the sweep continues without it
     sweep_sources  -> [1, 0]
     ```

  2. **End-to-end on the real durable path** (`/tmp/vrf/f1_wf.py`): the actual
     `DevelopmentReportWorkflow` + `ReportSectionWorkflow` + `retrieve_section` / `propose_report`
     activities on Temporal's time-skipping server, with `default_retrievers` returning
     `[Healthy, Broken]` and a fake submitter. Printed:

     ```
     report section 'Yield' retrieval failed; marked (... 'workflow_id': 'vrf-report-mixed-section-0' ...)
     result: Drafted 'Widget development' with 1 section(s); opened for review as pr://note/...
     ## Yield [layer: episodic]
     _Retrieval failed for this section; incomplete — re-run required._
     healthy hit present: False
     activity_max_attempts = 5
     ```

     `grep -c "pgvector unreachable"` over the same run: **10** — i.e. 5 activity attempts
     (`BAD_DATA_RETRY`, `maximum_attempts=settings.activity_max_attempts=5`), each of which re-ran
     the *healthy* retriever too and threw its result away.

  3. **Trigger reachability, checked per production retriever** rather than assumed:
     `grep -n "except" src/chemclaw/retrieval/vector_index.py` → **no matches**. `PostgresNoteIndex.
     search_dense` / `search_lexical` propagate every psycopg error, and neither `VectorRetriever.
     retrieve` (`retrievers.py:372-384`) nor `LexicalRetriever.retrieve` (`:411-418`) catches
     anything. `FingerprintReactionRetriever` catches only `FingerprintError`
     (`retrievers.py:242`).

- **Why**

  The mechanism, the cited lines and the stated consequence all hold on my own scaffolding.
  `harness.py:174` is a bare `asyncio.gather` with no `return_exceptions`; `fanout.py:98-105` is the
  same fan-out with a `try/except` per branch, a counter and a stream write. The durable path loses
  the *whole* section including the healthy source's evidence, and burns all five attempts doing it.
  Line numbers and symbols are current (`git status` clean at `e48441d0`).

  Two corrections to the finding, neither fatal:

  - **One of the three named triggers is wrong.** `ShareDocumentRetriever` cannot trigger this:
    `ingest/documents/retriever.py:151-174` catches `DocumentIndexError, ConnectionError, OSError,
    RuntimeError`, then `DocumentShareError`, then a bare `except Exception` — it returns `[]` on an
    unreachable share and never raises. The finding credits that leg as a trigger; it is in fact the
    only production retriever that already has the isolation `gather_section` lacks.
  - **`data_sources` defaults to `graph,eln-json`** (`core/config/sources.py:45`), not `graph`.

  One thing the reporter missed that makes it *worse*: `VectorRetriever.retrieve` calls
  `embed_texts` (`retrievers.py:377`) with no guard at all, while the documents retriever's
  `except Exception` carries a comment saying exactly why it needs one ("an `openai.APIError` is in
  neither list above, so a rate-limited embedding endpoint escaped this leg and failed the whole
  turn"). Under the `openai_compatible` provider, a rate-limited embedding endpoint therefore fails
  the whole report section — the same defect that leg already fixed for itself.

  Mitigations that keep this at high rather than critical: the failure is *visible*
  (`retrieval_failed=True` renders an explicit marker), nothing is silently dropped, and the report
  is re-runnable. The cost is availability — a single-source outage costs every section of every
  report while three healthy sources hold the evidence — plus the retry amplification.

---

## `GraphRetriever` is the only `SourceRetriever` that returns an unbounded result

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. **Read the return** — `retrievers.py:181-187` is a `sorted(...)` comprehension with no slice.
     Checked every sibling implementation of the contract:
     `grep -n "retrieval_top_k\|fingerprint_top_k"` over the retrieve halves gives
     `retrievers.py:382` (vector), `:417` (lexical), `:231` (fingerprint, `fingerprint_top_k`),
     `ingest/eln/warehouse/retriever.py:150`, `ingest/sources/vendored_dataset.py:191`,
     `ingest/documents/retriever.py:192`. `GraphRetriever` is the only one with no cap.

  2. **On the committed corpus as it ships today** (39 notes, `/tmp/vrf/f2_small.py`) —
     already over the cap without any growth story:

     ```
     'reaction'           -> 8 chunks (top_k=8)
     'yield'              -> 16 chunks (top_k=8)
     'solvent'            -> 12 chunks (top_k=8)
     'suzuki amide'       -> 21 chunks (top_k=8)
     ```

  3. **On my own grown corpus** — I built it myself (`/tmp/vrf/f2_build.py`, 2000 copies of
     `rxn-suzuki-biaryl` with distinct ids/sources, total 2039 `.md`), then `/tmp/vrf/f2.py`:

     ```
     settings.retrieval_top_k = 8
     settings.data_sources = graph,eln-json
     cold GraphRetriever('reaction'): 2008 chunks in 356 ms
     warm GraphRetriever('reaction'): 2008 chunks in  37 ms, 839 KiB of chunk JSON
     report note body: 582460 chars
       GraphRetriever('suzuki') -> 2011 chunks
       GraphRetriever('solvent') -> 2012 chunks
       GraphRetriever('yield')   -> 2016 chunks
     ```

     My numbers vs the reporter's: 2008 vs 2008 chunks (identical), 839 vs 861 KiB, 37 vs 23 ms,
     582 KB vs 606 KB of note body. Within noise of each other.

  4. **End-to-end through the real durable workflow** (`/tmp/vrf/f2_wf.py`, `f2_wf3.py`) with the
     real `GraphRetriever` over that corpus, on Temporal's time-skipping server:

     ```
     2 sections: WARN [TMPRL1103] payloads exceeded warning limit  size=1724864 limit=524288
                 result: Drafted 'Programme review' with 2 section(s) ...
                 committed note bytes: 1166121
     3 sections: WARN [TMPRL1103] ... size=2589945 limit=524288
                 committed note bytes: 1751109
     ```

  5. **Checked the downstream for any cap that would rescue it**: `report_note`
     (`harness.py:224-262`) iterates `section.evidence` with no slice; `gather_section` concatenates;
     `note_proposals.content` is a bare `TEXT` (`infra/sql/027_note_proposals.sql`) with no size
     constraint; `grep` for `max_length`/`MAX_` in `kg/pr_gate.py`, `kg/git_submitter.py`,
     `kg/note.py` returns nothing. The conversational escape the finding names is real:
     `research_tools.py:237` truncates at `settings.gather_evidence_max_chunks` (default **40**,
     not `retrieval_top_k`) *after* fusion.

- **Why**

  Every element reproduces on my own scaffolding and my numbers land on the reporter's. The claim
  that this is the sole unbounded implementation of the contract is verifiable by grep and holds.
  The corpus-growth premise is not load-bearing either: the *shipped* 39-note corpus already returns
  2–2.6x the cap for ordinary one-word queries, so the gap exists today and only widens.

  Two things I found that the reporter did not, both aggravating:

  - The committed artefact is **1.17 MB at two sections and 1.75 MB at three**, not ~600 KB — the
    note is per-report, not per-section.
  - The activity/workflow payloads cross Temporal's limits: measured **2.59 MB** on the
    `propose_report` payload at three sections, 5x the `TMPRL1103` warning threshold and above the
    2 MB `BlobSizeLimitError` a production Temporal server applies by default. The dev test server
    only warned, so I do not claim an observed hard failure — but a production cluster terminating
    the workflow on the final payload is the plainly reachable next step, and that failure would
    *not* be visible as `retrieval_failed`.

  Minor inaccuracies, neither material: `data_sources` defaults to `graph,eln-json` (graph is still
  default-enabled and is the leg at issue), and the bullet count depends on how one counts lines
  inside an excerpt (I measured 12,026 `\n- ` occurrences against the reporter's 8,026; the note
  size, which is the consequence, agrees).
