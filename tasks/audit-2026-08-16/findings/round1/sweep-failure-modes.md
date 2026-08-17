# Sweep: failure modes across `src/`

Read every `except`, `contextlib.suppress`, `degraded()` call and documented "best-effort" path in
the tree (65 broad handlers across 43 modules, plus every narrow one), and reproduced the ones
below against the live venv.

**Overall**: this tree is unusually hardened against failure. Most of what a fresh-eyes sweep would
normally find has already been found and fixed here, and the comments record the measurements. The
five findings below are the ones that survived checking. Four are reproduced with a script; the
fifth is a mechanism reproduced against the code, with its trigger stated honestly.

The recurring *shape* of what did survive is worth naming: **the swallow is usually right, and the
thing that is wrong is that the swallowed condition is then indistinguishable, to the next reader,
from a legitimate answer.** Findings 1 and 2 are both that shape, one in the conversation layer and
one in ingestion.

---

## An evidence source that fails is reported to the model as "nothing on file"

- **Severity**: high
- **Location**: `src/chemclaw/retrieval/fanout.py:98-105` (`_sweep`), reaching
  `src/chemclaw/agent/research_tools.py:152-239` (`gather_evidence`); the same swallow is repeated
  at `src/chemclaw/ingest/documents/retriever.py:161-174` and
  `src/chemclaw/ingest/eln/warehouse/retriever.py:113-130`.
- **Trigger**: any retrieval source raises — Postgres unreachable, the embedding endpoint
  rate-limiting, a vector store down, a `_backend()` that cannot be built. Reproduced by pointing
  `_text_retrievers` at a retriever that raises `ConnectionError("postgres: connection refused")`.

- **Consequence**: `_sweep` sets `chunks = []` and the sweep returns. `gather_evidence` returns
  `[]`, and its docstring — which is the model's contract for the tool — says:

  > *"Results are merged and de-duplicated. **Empty is a valid answer (nothing on file), never
  > invented.**"*

  So the model is explicitly instructed to read `[]` as "the knowledge graph has no record of
  this". A chemist asking "have we run this nitration before?" during a Postgres blip is told the
  company has no prior art, in a confident sentence, with nothing on the stream or in the answer
  saying a source was down.

  This is precisely the class the runner treats as *announce-worthy* one layer up: an unreachable
  Temporal broker is probed per turn and announced to the chemist before the turn starts
  (`api/runner.py:636-680`, "*Announced rather than discovered … the model met the outage as a tool
  failure mid-answer and, in the live run, read it as its own bad input*"). The same argument
  applies verbatim to the knowledge graph, and nothing implements it.

  **The module docstring's own claim does not hold.** `fanout.py` says the branch exists so that

  > *"A source that returns nothing is indistinguishable from a source nobody asked in an aggregate
  > hit-list, and telling those two apart is what `D-2026-08-01-a-cap-that-starves-a-source` needed
  > and did not have."*

  and `_sweep`'s own docstring says *"The failure is logged and counted, never swallowed silently."*
  But the failure path falls through to the identical `_report(name, len(chunks))` with
  `len(chunks) == 0`, so the branch event a live surface receives is
  `{"evidence_source": "graph", "chunks": 0}` — byte-identical to a source that ran fine and matched
  nothing. The machinery built to distinguish "returned nothing" from "wasn't asked" cannot
  distinguish "returned nothing" from "raised".

  There is no backstop by default: `verifier_enabled` and `answer_shape_gate_enabled` are both
  `False` (`core/config/llm.py:117,139`), and even with the verifier on, an answer citing nothing
  is only *flagged for review*, never withheld.

- **Evidence**:

  ```python
  # /tmp/repro_fanout.py
  class Dead:
      name = "graph"
      async def retrieve(self, q, f): raise ConnectionError("postgres: connection refused")
  class Live:
      name = "documents"
      async def retrieve(self, q, f): return []
  with patch.object(rt, "_text_retrievers", lambda: [Dead(), Live()]):
      out = await rt.gather_evidence(query="nitration of toluene prior art")
  ```

  ```
  evidence source 'graph' failed; the sweep continues without it
  ConnectionError: postgres: connection refused
  TOOL RETURNED: []
  type: <class 'list'> len: 0
  ```

  `record_metric("chemclaw_evidence_source_failures_total")` fires — server-side only. Note it does
  *not* go through `core.metrics_bridge.degraded()`, so it is also absent from the
  `chemclaw_degraded_total{subsystem=...}` family an operator would read first.

- **Fix**: carry the failure out of the sweep rather than only counting it. `sweep_sources` should
  return the failed source names alongside the ranked lists, and `gather_evidence` should append a
  synthetic, framed chunk naming them — e.g. `EvidenceChunk(source_note_id="retrieval-degraded",
  content="the following evidence sources could not be reached this turn and contributed nothing:
  graph")` — so the model cannot read the empty result as "nothing on file". Cheapest correct
  version: make `_report` emit a distinct `{"evidence_source": name, "failed": true}` payload and
  have `gather_evidence` refuse to return an *entirely* empty list when every source failed, raising
  a `SubsystemUnavailableError` instead (the retryable family `surface_domain_errors` already hands
  to the model verbatim).

---

## A transient git outage is filed as per-entry bad data, and the ELN cursor advances past the lost entries

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/sync.py:158-231` (`sync_entries` — the
  `except (ChemclawError, ValidationError)` at line 216 combined with the `cursor = max(cursor,
  window)` at line 183, which runs *before* the `try`), persisted by
  `src/chemclaw/durable/eln_sync.py:245-250` (`store_sync_cursor` on every chunk unconditionally).
- **Trigger**: the note submitter raises `GitSubmitError` for any transient reason during a
  scheduled sync — a dead git remote, a DNS failure, a push rejected by the server, or the
  cross-process `flock` being held (`kg/git_submitter.py:103`, "*note_repo_dir is in use by another
  process*"). `GitSubmitError` is a `ChemclawError`, so it lands in the reject-and-continue clause.

- **Consequence**: three things at once.

  1. **Silent permanent data loss.** Every entry in the batch is recorded as `rejected` — the same
     bucket as a mass-balance failure or an unmappable role, i.e. deterministic bad data — and the
     summary's `next_cursor` has already advanced past all of them. The workflow persists that
     cursor. The entries come back only inside `eln_sync_overlap_seconds` (default 86 400 s); a git
     outage or a stuck lock lasting longer than a day drops those experiments from the corpus with
     no automatic path back. `IngestSummary`'s own docstring states the intended invariant and it is
     violated here: *"The cursor advances past **rejected** entries too (a rejection is
     deterministic bad data — re-fetching it would only re-reject it)."* A dead remote is not
     deterministic bad data.
  2. **Orphaned fingerprint rows.** `ingest_reaction` writes the reaction and molecule fingerprints
     *before* it calls `propose_note` (`ingest/eln/ingest.py:51-58`), so the reaction is in the
     structural index while its note never exists. `FingerprintReactionRetriever` then returns
     `source_note_id=reaction-rxn-1` as a citation for a note that will never be merged.
     `retrievers.py:210-218` anticipates the *pending-review* version of this ("*surfacing the
     pending note to the reviewer — the PR-gate working*"); this one is permanent, and nothing
     surfaces it.
  3. **The operator report is wrong about the cause.** The run logs
     `ingested=0 rejected=5` with a per-entry WARNING that reads like five bad ELN records.

  The durable `note_proposals` FAILED row does keep the rendered bytes — but nothing in the tree
  replays them. Grepping `ProposalState.FAILED` finds no resubmission path; recovery is a human
  action that is not implemented.

- **Evidence**: `/tmp/repro_eln.py` — five well-formed entries, a submitter that raises
  `GitSubmitError`:

  ```
  ingested : []
  rejected : [('rxn-1', "git push origin note/x failed: fatal: unable to access 'http"), ... x5]
  NEXT CURSOR -> 2026-01-05 00:00:00+00:00
  ```

  and the fingerprint store after the same run:

  ```
  rxn store internals: {'rxn-1': FingerprintRecord(id='rxn-1', label='CCO.CC(=O)O>>CCOC(C)=O', ...),
                        'rxn-2': ..., 'rxn-3': ..., 'rxn-4': ..., 'rxn-5': ...}
  ```

  Five structural index rows, zero notes, cursor at the newest entry.

  `durable/eln_sync.py:245-250` shows the persistence is unconditional:

  ```python
  summaries.append(chunk.summary)
  if since is None:
      await workflow.execute_activity(store_sync_cursor, args=[source, chunk.summary.next_cursor], ...)
  ```

- **Fix**: split "the entry is bad" from "we could not write it". Catch `GitSubmitError` (and any
  `SubsystemUnavailableError`) separately in `sync_entries` and **do not advance the cursor past
  that entry** — the same asymmetry the module already implements for a future-dated amendment
  stamp, which ingests the entry but refuses to move the cursor. Track a `blocked` list distinct
  from `rejected`, floor `next_cursor` at the oldest blocked entry's window, and let the activity
  fail (so Temporal retries the chunk) when every entry in a chunk is blocked. Secondarily: index
  the fingerprints only after `propose_note` returns, so a failed submission leaves no orphan
  citation target.

---

## `HeldConnectorSession._shut_down` swallows the caller's own cancellation

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/transport.py:167-174` (`_shut_down`)
- **Trigger**: a cancellation is delivered to the calling task while `_shut_down` is suspended in
  `await task` — i.e. the front door's `asyncio.timeout(service_turn_timeout_seconds)`
  (`api/routes/turns.py:159`) expires, or the client disconnects, during the window in which the
  turn's `AsyncExitStack` is closing its connector sessions.

- **Consequence**: `contextlib.suppress(asyncio.CancelledError, Exception)` catches the
  `CancelledError` that `asyncio.timeout` raised in order to *be* a timeout. `asyncio.Timeout.__aexit__`
  then sees `exc_type is None`, calls `uncancel()`, and **does not raise `TimeoutError`** — so the
  deadline silently does not fire. The caller believes the block completed inside its budget; in
  fact the budget was exceeded and the cancellation was thrown away. On the disconnect path the same
  swallow means `run_turn`'s `except (GeneratorExit, asyncio.CancelledError)` teardown clause is not
  entered for that turn.

  This module is elsewhere careful about exactly this distinction —
  `absorb_connect_failure`/`_is_really_cancelled` (lines 44-80) exist to tell "the caller cancelled
  us" apart from "an inner anyio scope unwound", and re-raise in the first case. `_shut_down` does
  not consult that guard, so the one function that can eat a real deadline is the one that skips
  the rule the module wrote for itself. The `Exception` half of the same suppress is separately
  worth narrowing: a genuine failure in the holder task's unwind is discarded with no log line.

- **Evidence**: `/tmp/repro_shutdown.py` — a holder task that takes 0.5 s to unwind, a 0.15 s
  `asyncio.timeout` around a body that reaches `_shut_down`:

  ```
  RESULT: shut_down returned normally -- timeout did NOT fire
  current task cancelling() = 0
  ```

  Expected: `TimeoutError propagated`. The cancellation counter is back at 0, so nothing downstream
  can tell it happened.

  (Honest scope: the window is the length of the connector-session unwind, so this needs the
  deadline or the disconnect to land inside it. The *mechanism* is proven; I did not measure how
  often production lands there.)

- **Fix**:

  ```python
  if task is not None:
      try:
          await task
      except asyncio.CancelledError:
          if _is_really_cancelled():
              raise
      except Exception:
          logger.warning("connector %s did not unwind cleanly", self._spec.name, exc_info=True)
  ```

  i.e. reuse the module's own `_is_really_cancelled()` rule, and log rather than discard a real
  error.

---

## The embedding provider's `index` field is ignored, so vectors are positionally assigned

- **Severity**: medium
- **Location**: `src/chemclaw/core/embeddings.py:228-238` (`_openai_compatible_embeddings`), and the
  positional `zip`s that trust it: `embeddings.py:170` (`zip(unique, _embed_uncached(unique))`),
  `ingest/documents/sync.py:369` (`zip(stale, embeddings, strict=True)`),
  `retrieval/vector_index.py:520`.
- **Trigger**: an OpenAI-compatible endpoint whose `embeddings.create` response returns `data` in an
  order other than the request order. The OpenAI schema carries a per-item `index` field precisely
  because position is not part of the contract; internal gateways, batching proxies and vLLM-style
  servers are exactly the deployments this repo targets (`llm_base_url` is an internal endpoint by
  design).

- **Consequence**: silent, undetectable mis-assignment. `strict=True` catches a *length* mismatch
  and nothing else. Note A is stored in `note_index` under note B's vector; document chunk 7 gets
  chunk 3's embedding. Dense retrieval then returns confidently wrong evidence — chunks whose text
  has nothing to do with the query — and every downstream check reads it as legitimate: the chunk
  carries a real `source_note_id`, so the citation gate resolves it, and `verify_claims` passes. It
  is the "plausible but wrong" class, at the retrieval layer, with no error path at all.

- **Evidence**: `/tmp/repro_embed.py` — a fake OpenAI-compatible client that returns correctly
  `index`-tagged items in reverse order:

  ```
  'note-a body'        -> [2.0, 2.0, 2.0, 2.0]   (provider said index 2)
  'note-b body'        -> [1.0, 1.0, 1.0, 1.0]   (provider said index 1)
  'note-c body'        -> [0.0, 0.0, 0.0, 0.0]   (provider said index 0)
  ```

  Every vector landed on the wrong text; nothing raised, nothing logged.

- **Fix**: three lines, and they also assert the invariant.

  ```python
  response = client.embeddings.create(model=settings.embedding_model, input=texts)
  by_index = {item.index: item.embedding for item in response.data}
  if len(by_index) != len(texts):
      raise ValueError(f"embedding endpoint returned {len(by_index)} vectors for {len(texts)} texts")
  return [by_index[i] for i in range(len(texts))]
  ```

  A `KeyError`/`ValueError` here is strictly better than a silently transposed index.

---

## `ChemclawError` messages carrying filesystem paths are handed to the model verbatim

- **Severity**: low
- **Location**: `src/chemclaw/agent/tool_authz.py:131-133` (`domain_error_result`) reading messages
  raised by e.g. `src/chemclaw/ingest/sources/registry.py:83,91` and
  `src/chemclaw/connectors/registry.py:140-148`.
- **Trigger**: a tool call reaches a data-source or connector manifest load that fails —
  `gather_evidence` → `active_retrieve_sources()` → `_read_manifest` on a malformed
  `datasource.yaml`.
- **Consequence**: `surface_domain_errors` catches `ChemclawError` and returns `f"Error: {exc}"` to
  the model. `DataSourceError(f"{manifest_path}: unreadable data source manifest: {exc}")` puts an
  absolute container path (and the underlying YAML/OS error) into the model's context, from where it
  can be relayed into a chemist-facing answer. This contradicts the stated contract of the sibling
  branch: `unexpected_error_result`'s docstring says the two safe families are safe *"because
  someone decided their messages are fit for a model to read; anything else is an internal fault
  whose text can carry a DSN, a path or a row of data"* — and `CalculationDomainError`'s docstring
  repeats it (*"explain the limit in the chemist's terms, never echo internal state"*). Some
  `ChemclawError` messages do echo internal state.
- **Evidence**: the well-behaved case round-trips cleanly —

  ```
  model is told verbatim -> Error: unknown data source 'no-such-source'; valid sources: eln-json, ...
  ```

  — while `registry.py:83` and `:91` interpolate `manifest_path` directly, and
  `connectors/registry.py:140` interpolates `path`. `DocumentIndexError` at
  `ingest/documents/index.py:949` is the counter-example done right ("*the document index did not
  answer, so the search never ran*", detail on `__cause__` only), and its docstring argues exactly
  this contract.
- **Fix**: apply the `DocumentIndexError` pattern to the manifest raisers — a caller-safe sentence
  naming the *source* (`"data source 'sharedrive' is misconfigured and was not loaded"`), with the
  path and the parser error on `__cause__` for the log. Optionally add a test asserting no
  `ChemclawError` raise site in `src/` interpolates a `Path`.

---

## Checked and found sound (so the negative result is on the record)

- **`durable/publish.py::_BAD_DATA_TYPES`** — Temporal matches non-retryable types by bare class
  name, so every `ChemclawError` subclass needs listing. Enumerated all of them against the live
  import graph: **zero missing**. `SubsystemUnavailableError` is correctly absent (it does not
  inherit `ChemclawError`, so it stays retryable).
- **`agent/plan_gate.py`** — fails closed on every unreadable path: an unreadable checkpoint yields
  `None` from `session_todos`, which refuses; `consume_turn_approval` leaves the approval live
  rather than spending it silently; a gated call in the same batch as a `write_todos` is refused
  without asking the store.
- **`api/auth.py`** — an unreachable JWKS is a 503, not a 401 and not a bypass; the unknown-`kid`
  refresh is cooldown-limited; `options={"require": ["exp"]}` closes the no-expiry token.
- **`api/runner.py` teardown** — `answered or run_complete` correctly keeps a committed exchange,
  the `finally` block genuinely contains no `await`, and `_classify` yields a user-safe event
  rather than a traceback.
- **`agent/job_results.py`** — the `gather(return_exceptions=True)` result is now bound and each
  outcome counted; a failed workflow is reported to the model as `status: failed` rather than
  omitted.
- **`durable/heartbeat.py::beating`** — the `finally` cancels *and awaits* the wrapped task, so no
  exit leaves detached work running.
- **`science/calc/logd.py` + `connectors/calc/compose.py`** — the symmetry-number claim in
  `thermo.py`'s docstring ("*a caller that leaves it unstated is told so*") is **true**:
  `reaction_energy` sets `delta_g_kcal=None` and emits a warning when any species' sigma is
  unstated. `logd_from_pka` genuinely refuses amphoteric and substantially-ionised polyprotic
  molecules rather than returning a number.
- **`kg/git_submitter.py`** — the worktree/flock/sweep discipline and the one-sided
  `_release_worktree` swallow are correct as documented.
- **`agent/repeat_guard.py`** — cannot produce a false `RepeatedCallRefusal` on a Temporal activity
  retry, because the contextvar counter is only started by `api/runner.py` and is `None` (a no-op)
  on the template path.
