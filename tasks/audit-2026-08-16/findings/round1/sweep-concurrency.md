# Sweep: concurrency and the event loop

Scope: all of `src/`, read across packages. Everything below was run in this environment
(`uv run python`, scripts under `/tmp`) unless marked otherwise.

## The embedding cache is a plain dict mutated from several worker threads, and a concurrent trim evicts a key the other thread is about to read

- **Severity**: high
- **Location**: `src/chemclaw/core/embeddings.py:140-185` (`embed_texts`), specifically the
  read at line 179 and the FIFO trim at lines 183-184; the cache itself is `_CACHE` at line 45.
- **Trigger**: two or more concurrent `embed_texts` calls on different threads, which is the
  normal shape — every call site reaches it through `asyncio.to_thread`
  (`retrieval/retrievers.py:377`, `retrieval/vector_index.py:520`,
  `ingest/documents/sync.py:368,417`, `ingest/documents/retriever.py:196`,
  `ingest/eln/warehouse/retriever.py:143`). On the `background` Temporal worker,
  `note_index` reindex, `document_sync` reembed and `report_workflow` sections are all
  `@durable_activity("background")` on the same loop and the same default thread pool, so a
  corpus-sized batch and a query embed genuinely overlap.
- **Consequence**: `embed_texts` raises a bare `KeyError` whose payload is the cache key.
  Whatever asked for the embedding fails — a note reindex, a document re-embed, or, in the chat
  sweep, one retrieval leg that then silently contributes zero chunks (the `_sweep` branch in
  `retrieval/fanout.py:98-105` swallows it and counts it as an empty source).
- **Evidence**: the comment at lines 172-178 asserts this hazard is closed —
  *"Read the batch out **before** trimming. … Trimming first therefore raised `KeyError` on the
  line below"*. Reading before trimming closes it for one thread only. Nothing guards the window
  between the insert loop (line 170-171) / the membership test (line 165) and the read at
  line 179, so another thread's trim at line 183-184 can delete a key from under it.

  Reproduction at the shipped default `embedding_cache_size = 2048`
  (`core/config/llm.py:154`) — two threads embedding a 4000-text batch plus four threads
  embedding a single query:

  ```
  $ uv run python /tmp/repro_embed.py
  cache size = 2048
  errors: 1
  --- big KeyError ('hash:ep-none:d1536:model-none', 'note-0-1488')
    File "src/chemclaw/core/embeddings.py", line 179, in embed_texts
      vectors = [_CACHE[key] for key in keys]
  KeyError: ('hash:ep-none:d1536:model-none', 'note-0-1488')
  ```

  At `embedding_cache_size = 64` the same script produced 4 failures within 15 s, including one
  on a **single-text** batch (`query-0`), i.e. the plain interactive retrieval path:

  ```
  --- small KeyError ('hash:ep-none:d1536:model-none', 'query-0')
      File "src/chemclaw/core/embeddings.py", line 179, in embed_texts
  ```

  `_CACHE.clear()` (`clear_embedding_cache`, line 203) and the trim's
  `del _CACHE[next(iter(_CACHE))]` are equally unsynchronised.
- **Fix**: put a `threading.Lock` around the cache. The cheapest correct shape keeps the
  provider call outside the lock: take the lock to read hits and record which keys are missing,
  release it for `_embed_uncached`, retake it to insert and trim, and build the result list from
  the locally-held vectors (the batch's own `dict` of text → vector) rather than by re-reading
  `_CACHE`. That removes the read-after-evict entirely instead of narrowing it.

## Four `background`-queue Temporal activities do whole-corpus CPU work inline on the worker's event loop

- **Severity**: medium
- **Location**:
  - `src/chemclaw/durable/digest.py:64` (`collect_digests`, `async def` at line 48 under
    `@durable_activity("background") @activity.defn`) — `notes = load_notes(settings.knowledge_path)`
  - `src/chemclaw/durable/memory_jobs.py:116,123,130`
    (`build_campaign_notes_activity`, `build_playbook_notes_activity`,
    `build_optimization_notes_activity`) — each calls a synchronous builder that fingerprints
    every reaction and clusters them O(n²) (`memory/similarity.py:17-57`) and then reads the whole
    note corpus again through `memory/jobs.py:81` (`_with_supersedes` → `load_notes`).
- **Trigger**: the schedule. `durable/schedules.py:104-109` gives all three memory-synthesis
  workflows a **single shared cadence** (`memory_synthesis_schedule_minutes`), so they fire in the
  same minute onto the same worker; `collect_digests` runs on that worker too.
- **Consequence**: `durable/serve.py` states the property that makes this matter —
  *"Every activity here is a coroutine on this process's one event loop"*. While one of these runs,
  the worker stops polling task queues, stops sending activity heartbeats (`durable/heartbeat.py`
  beats on a timer that cannot fire), and every other `background` activity — ELN sync chunks,
  document sync, note reindex, report sections, the connector-job wrapper, retention — is stopped.
- **Evidence**: `observation_jobs.py:56-59` does the *same* corpus read with
  `await asyncio.to_thread(load_notes, …)` and a comment saying exactly why
  ("`load_notes` is a synchronous full parse of the corpus, and an async activity …"), and
  `connectors/bo/activities.py:59,75` threads the same BoFire calls these builders make inline.
  These four are the ones that were missed.

  Measured on this box, 1800 parseable notes, with a 5 ms heartbeat coroutine measuring the stall:

  ```
  $ uv run python /tmp/repro_digest.py 2000
  baseline max heartbeat gap :      7.9 ms
  inline load_notes          :    362.5 ms  -> loop stalled    363.8 ms
  to_thread load_notes       :    380.7 ms  -> loop stalled     12.5 ms
  ```

  And the clustering half of the memory builders, pure Python O(n²) over DRFP bitstrings:

  ```
  $ uv run python /tmp/repro_cluster.py
  n=500:  cluster_by_similarity   76 ms
  n=1000: cluster_by_similarity  301 ms
  n=2000: cluster_by_similarity 1203 ms
  ```

  So a 2,000-reaction corpus is ~1.2 s of frozen loop per memory activity before the corpus
  re-read, three of them in the same minute, plus `collect_digests`. `kg/graph.py:44` cites
  ~86 ms per assembly at 10k notes and the parse scales with it.
- **Fix**: wrap each in `asyncio.to_thread` exactly as `observation_jobs.py` and
  `connectors/bo/activities.py` already do —
  `notes = await asyncio.to_thread(load_notes, settings.knowledge_path)` in `collect_digests`, and
  `return await asyncio.to_thread(build_campaign_notes, await all_reactions())` (and its two
  siblings). Then extend `tests/test_event_loop_offload.py` to assert the hop, since it currently
  asserts exactly one (see the last finding).

## A process-global lock held across a whole-corpus computation is taken from the shared default executor, so it stalls bearer-token validation

- **Severity**: medium
- **Location**: `src/chemclaw/kg/conflicts.py:296` (`_INDEX_LOCK = threading.Lock()`), taken at
  line 337 and held across `cached_notes` + `find_conflicts` + `_strongest`. Reached only through
  `await asyncio.to_thread(conflict_index, …)` (`retrieval/retrievers.py:116`).
- **Trigger**: any turn whose evidence sweep runs after the corpus changed (a knowledge-sync
  `rsync`, a merged note) or after process start. `gather_evidence` fans out one branch per
  source, and `GraphRetriever`, `VectorRetriever` and `LexicalRetriever` each call
  `_conflict_index` — three `to_thread` calls per turn, all contending for this one lock, plus
  every concurrent turn's three.
- **Consequence**: the blocked threads are **default-executor** threads, and the default executor
  is `min(32, cpu_count+4)` — 8 on this box. `api/auth.py:226` runs *every* bearer-token
  validation through `asyncio.to_thread(validate_token, …)` on that same executor. So one
  conflict-index computation does not merely serialise the retrieval legs, it occupies every
  executor slot and every authenticated request on the pod queues behind it.
- **Evidence**: the codebase already knows this hazard and names it —
  `agent/attachments.py:129-132`: *"a waiter holds a future, not a thread, so no number of them
  can crowd the default executor where `chemclaw.api.auth` validates every bearer token."* The
  conflict index is precisely the case that does crowd it, because the lock converts *waiting* into
  a held thread.

  Measured (8 concurrent `to_thread(conflict_index, …)` calls, one `to_thread` standing in for
  `validate_token` arriving 50 ms later):

  ```
  $ uv run python /tmp/repro_lock.py
  notes on disk: 2000
  one cold conflict_index: 369 ms
  default executor max_workers: 8
  8 concurrent conflict_index calls: 293 ms total
  the token-validation to_thread that arrived alongside waited 239 ms
  ```

  The module's own comment cites **1,525 ms** for one computation on a 2,000-note
  programme-shaped corpus, which is ~6× the synthetic corpus here; the auth stall scales with it.
- **Fix**: keep the single-flight property (it is worth having — the comment's 4,238 ms vs
  1,525 ms measurement is real) but stop paying for it with an executor thread. Give the
  computation its own single-threaded `ThreadPoolExecutor(max_workers=1)` and have
  `_conflict_index` submit to it via `loop.run_in_executor`, so waiters are futures on the loop
  rather than blocked default-executor threads; the lock then becomes unnecessary because the
  executor serialises. Failing that, at minimum run every `to_thread` on this path against a
  dedicated retrieval executor so it cannot reach the pool that authenticates requests.

## `gather_section` gathers retrievers without `return_exceptions`, so one failing source fails the whole report section — the opposite of what the chat sweep does

- **Severity**: medium
- **Location**: `src/chemclaw/retrieval/harness.py:174-176` (`gather_section`), called by
  `durable/report_workflow.py:75,78` (both `@durable_activity("background")`).
- **Trigger**: any retriever raising. `VectorRetriever.retrieve`
  (`retrieval/retrievers.py:372-384`) has no handler at all: `embed_texts` can raise (see the
  first finding, and a network error under `openai_compatible`), and `search_dense` can raise on a
  statement timeout. `LexicalRetriever.retrieve` (lines 411-420) likewise.
- **Consequence**: the first exception propagates out of `gather` and fails the whole section
  activity, discarding the graph retriever's and the document share's evidence that had already
  been fetched. The remaining coroutines were already wrapped as tasks by `gather` and are not
  cancelled, so their results — and any exception they raise afterwards — are never retrieved.
- **Evidence**: `retrieval/fanout.py:98-105`, the conversational path, does the opposite and
  says why: *"A branch that raises costs its own source and not the sweep … exactly as an
  unreachable connector costs its tools and not the turn."* `ingest/documents/index.py:918-923`
  records a live instance of exactly this failure mode on the chat path — *"a statement timeout on
  a large share propagated out through `gather_evidence`'s `asyncio.gather` and failed the whole
  turn, taking the knowledge graph's answer with it"* — fixed there by wrapping the exception in
  the retriever. The durable path never got the structural fix, only the per-retriever patch, so
  any raiser the retrievers do not already swallow reproduces it.
- **Fix**: `return_exceptions=True` plus a per-retriever log/counter, mirroring `_sweep`:
  ```python
  gathered = await asyncio.gather(*(...), return_exceptions=True)
  evidence = []
  for retriever, result in zip(retrievers, gathered, strict=True):
      if isinstance(result, BaseException):
          logger.exception("evidence source %r failed; the section continues without it",
                           retriever.name)
          record_metric(lambda m: m.increment("chemclaw_evidence_source_failures_total", 1))
          continue
      evidence.extend(result)
  ```
  (re-raising `asyncio.CancelledError`, as `durable/orchestrator.py:167-170` already does).

## The JWKS forced-refresh cooldown is a cross-thread check-then-act, so its "at most once per cooldown" is not true

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:98-108` (`_forced_refresh_allowed`), reading and writing
  the module dict `_last_forced_refresh` (line 76).
- **Trigger**: a burst of requests carrying tokens with an unknown `kid` (a genuine key rotation,
  or an unauthenticated flood — the `kid` comes from the caller's own token header, as the comment
  at lines 71-75 says). `require_principal` runs every validation on the default executor
  (`auth.py:226`), so several threads reach lines 104-107 together.
- **Consequence**: every thread that reads `last is None` (or a stale timestamp) before any of
  them writes line 107 is granted a forced refresh, so N concurrent unknown-`kid` requests cost N
  outbound JWKS fetches instead of one — the amplification the cooldown was added to remove. Each
  fetch is blocking urllib on an executor thread, so it also compounds the executor pressure in the
  finding above.
- **Evidence**: the docstring claims the property the code does not have —
  *"Whether an unknown `kid` may pay for a JWKS re-fetch — **at most once per cooldown**."* There
  is no lock and no atomic operation between the read at line 104 and the write at line 107, and
  the function is called only from worker threads.
- **Fix**: guard lines 104-107 (and `_client_for`, lines 79-85, which has the same shape) with a
  module-level `threading.Lock`. Both are microsecond-scale critical sections, so this does not
  reintroduce the executor problem above.

## `run_turn`'s contextvar teardown raises `ValueError` when the turn's generator is finalised deferredly, aborting the rest of the `finally`

- **Severity**: low
- **Location**: `src/chemclaw/api/runner.py:588-594` (the `finally` block's six `reset` calls),
  with tokens taken at lines 187-209.
- **Trigger**: the SSE consumer task is cancelled *after* `run_turn` yielded a chunk and *while*
  the transport is sending it — i.e. a client disconnect that does not land inside `run_turn`
  itself. The generator is then left suspended and finalised later by asyncio's async-generator
  finaliser, which runs `aclose()` in a **new task with a different `Context`**.
- **Consequence**: `ContextVar.reset(token)` raises
  `ValueError: <Token …> was created in a different Context` at the first reset
  (`end_call_watch`), so the five resets after it never run and the `ValueError` escapes into a
  finaliser task nobody awaits — surfacing as an unattributable
  "an error occurred during closing of asynchronous generator" with no session id on it. The
  accounting above it (`budget.record`, `record_turn_cost`, the metric counters) has already run,
  and the identity does not leak into another turn's context because each turn's task carries its
  own copy — so this is noise plus a dead teardown path, not a security defect.
- **Evidence**: the block's own comment states the invariant it is protecting —
  *"an `await` here re-raises the cancellation on the spot and silently skips everything below it,
  including the five context-var resets"* — and the same skip happens for a different reason.
  Reproduced with the exact generator topology (`run_turn` inside `_turn_events` inside a
  cancellable consumer):

  ```
  $ uv run python /tmp/repro_ctx2.py
  cancelled. pending tasks: ['main', 'Task-3']
    finally runs in task: Task-4 | cv = turn-1
    RESET FAILED: ValueError <Token …> was created in a different Context
  ```
- **Fix**: make the teardown tolerant of the context it is actually running in — wrap the reset
  helpers so a `ValueError` from a foreign-context token is logged at debug and the remaining
  resets still run (the value is unreachable in that context anyway), or set the contextvars in
  `_turn_events` (the route's own frame) rather than inside the generator, so the tokens and the
  teardown are always in one context.

## What the offload policeman covers, and what it does not

Not a finding on its own, but it is what the sweep was asked to check.
`tests/test_event_loop_offload.py` is two tests: one asserts the `asyncio.to_thread` hop around
`thermochemistry_from_hessian`, one asserts that `gather_evidence` overlaps two retrievers. Both
pass (`2 passed in 0.44s`).

It therefore pins **one** offload hop out of the 55 `to_thread` sites in `src/`, and none of the
places where the hop is *missing*. Nothing in the suite would have caught any of the first three
findings above: not `collect_digests` or the three memory builders running a whole-corpus parse
and an O(n²) clustering on a Temporal worker's loop; not `conflict_index`'s global lock consuming
default-executor threads that `api/auth` needs; not `embed_texts` being called from several
threads at once without a lock. The test's own docstring says the right thing — *"the blocking
call happens on a different thread than the coroutine that awaited it"* — the gap is that it is
applied to one call rather than derived over the set of known-blocking helpers
(`load_notes`, `build_graph`, `conflict_index`, `embed_texts`, `propose_candidates`,
`initial_candidates`, the memory builders). A derived test over that set, asserting every
`async def` reaching them does so off-thread, is what would have failed here.

## What I checked and found sound

- `agent/turn_cost.py` — `_PENDING` really does hold strong references and the write really is
  scheduled without awaiting; the `create_task` context copy carries the turn's identity because
  `record_turn_cost` is called before the resets.
- `api/state.py::_claim_turn_slot` — the claim is genuinely atomic on the loop (no `await`
  between the test and the write), and the lease bound is what it claims to be.
- `api/state.py::_release_turn_claim` / `agent/attachments.py::parse_attachment_off_loop` — the
  `asyncio.shield` reasoning is correct in both, and `_ParseSlots` really does release from the
  worker's completion callback rather than from the waiting request.
- `core/db.py::_pool_for` — the "dictionary insert happens before any `await`" claim holds.
- `agent/checkpointer.py::checkpointer` / `core/temporal_client.py::connect` — both publish the
  singleton only after initialisation completes, under a lock, with a lock-free warm path.
- `connectors/transport.py::HeldConnectorSession` — the one-task-per-session confinement is real
  and is what makes `registry.open_connector_specs`'s `gather` safe.
- `core/turn_signals.py`, `agent/loop_cap.py`, `agent/repeat_guard.py` — all three carry *mutable
  records* in contextvars rather than rebinding, which is what makes them visible to the runner
  when the stream is driven from another task; the reasoning matches the code.
- `durable/orchestrator.py::fan_out` — batched rather than semaphored, `return_exceptions=True`,
  and `CancelledError` re-raised rather than logged. Correct.
- `retrieval/fanout.py` — per-branch failure isolation and deterministic fan-in order both hold.
- `science/calc/store.py::cached_compute` — the documented check-then-act is exactly as
  documented; I found no *undocumented* second instance on a hot path
  (`connectors/jobs.py::_params_model`, `core/reagents.py`, `evals/retrieval.py::_RETRIEVAL_MEMO`
  are all either loop-only or import-time).
