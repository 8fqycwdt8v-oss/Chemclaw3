# core-infra — CORRECTNESS

Slice: `src/chemclaw/core/{db,migrate,grants,http,egress,logging,metrics,metrics_bridge,embeddings,temporal_client,identity_context,turn_signals}.py`

Environment was live for this pass: `dockerd` started, `make up` (Postgres 16 + Temporal) running, everything
run with `uv run`. Six findings below; five carry a script and its output.

Claims I checked and found **true** (recorded so they are not re-litigated):

- `db.connection()` really does apply `pg_statement_timeout_seconds` by default on a pooled connection, `0`
  really does opt out, `connect()` really is unbounded, and `_merged_options` really does preserve a DSN's own
  `options` — measured against live PG: `pooled default statement_timeout: 30s` / `opt-out (0): 0` /
  `search_path preserved: public` + `and timeout applied: 30s` / `connect() (unpooled): 0`.
- `migrate.py`'s `lock_timeout` claim: `lock_timeout` *does* bound a `pg_advisory_xact_lock` wait
  (measured: `raised after 1.00s: LockNotAvailable: canceling statement due to lock timeout`).
- `grants.py`'s claim that a `GRANT` does not queue in front of live traffic: measured with an open
  `ACCESS SHARE` reader, the `GRANT` did not block and a concurrent `SELECT`/`INSERT` from a third session
  completed in 0.00 s.
- `egress.py`'s claim that `langsmith.configure(enabled=False)` beats a warm `lru_cache`: measured with
  `LANGSMITH_TRACING=true` set *before* importing `langchain_core`, `tracing_is_enabled()` goes `True → False`
  across the pin and `CallbackManager.configure()` attaches zero handlers.
- `logging.py`'s ReDoS bounds: `redact_secrets` is linear on both hostile inputs (10/20/40/80 KB of
  `password=` → 0.007/0.014/0.030/0.058 s; of `-eyJ` → 0.003/0.005/0.011/0.021 s).

---

## The embedding cache is mutated from multiple threads with no lock, so `embed_texts` raises `KeyError` on a text it just proved was cached

- **Severity**: high
- **Location**: `src/chemclaw/core/embeddings.py:164-185` (`embed_texts`)
- **Trigger**: two concurrent calls to `embed_texts` in one process, with the cache at its size limit.
  `embed_texts` is called from six sites, five of them through `asyncio.to_thread`
  (`retrieval/retrievers.py:377`, `retrieval/vector_index.py:520`, `ingest/documents/retriever.py:196`,
  `ingest/documents/sync.py:368` and `:417`, `ingest/eln/warehouse/retriever.py:143`), so the calls run on the
  default executor's threads and genuinely overlap. The sequence:
  1. Thread A computes `keys` and `missing`. Some of A's keys are **hits**, so they are not in `missing`.
  2. A enters `_embed_uncached(unique)`. Under `openai_compatible` that is a network round trip which
     releases the GIL for hundreds of milliseconds.
  3. Thread B inserts into `_CACHE` and runs the trim `while len(_CACHE) > size`. At a full cache — the
     steady state, since `embedding_cache_size` defaults to 2048 — **one miss evicts one entry**, FIFO,
     oldest first. A's hit was an old query, so it is the victim.
  4. A returns from the provider, inserts only its own misses, and evaluates
     `vectors = [_CACHE[key] for key in keys]` — which now raises for the key it saw at step 1.
- **Consequence**: a bare `KeyError` naming a `(config_key, text)` tuple propagates out of `embed_texts` into
  whatever asked. On the retrieval path that fails the dense leg of a chemist's turn; on
  `reembed_stale`/`reindex_notes` it aborts a corpus rebuild partway, leaving the index half-stale. Two threads
  trimming concurrently can also both take `next(iter(_CACHE))` and the loser raises `KeyError` inside the
  trim itself.
- **Evidence**: the comment at `embeddings.py:172-178` claims this exact failure was fixed — *"Read the batch
  out **before** trimming … Trimming first therefore raised `KeyError` on the line below"*. Reordering fixed
  the single-threaded case only; nothing serialises `_CACHE` across threads. Reproduced at the **shipped
  default cache size** with a single concurrent miss (`/tmp/audit/repro_embed_race2.py`):

  ```
  cache size: 2048 victim present: True
  File "/home/user/Chemclaw3/src/chemclaw/core/embeddings.py", line 179, in embed_texts
      vectors = [_CACHE[key] for key in keys]
  KeyError: ('hash:ep-none:d1536:model-none', 'old-cached-query')
  ```

  (A second script, `/tmp/audit/repro_embed_race.py`, shows the same at `size=8`.) Both used the `hash`
  provider with a 0.30 s sleep standing in for the provider round trip; under `openai_compatible` that sleep
  *is* the real network call, so the window is not synthetic.
- **Fix**: put a `threading.Lock` around every `_CACHE` read/write/trim, and — more importantly — stop
  round-tripping through the dict for the answer. Build the result list from a local mapping:
  `resolved = {key: _CACHE[key] for key in keys if key in _CACHE}` under the lock, then merge the freshly
  computed vectors into `resolved`, return `[resolved[key] for key in keys]`, and only then write back to
  `_CACHE` and trim (also under the lock). The returned batch then cannot depend on what another thread did to
  the cache. `clear_embedding_cache` needs the same lock.

---

## `_openai_compatible_embeddings` zips the provider's response positionally and ignores `Embedding.index`

- **Severity**: medium
- **Location**: `src/chemclaw/core/embeddings.py:237-238` (`_openai_compatible_embeddings`)
- **Trigger**: `settings.embedding_provider = "openai_compatible"` against any server whose `data` array is not
  in request order — vLLM, TEI, LiteLLM, or any gateway that fans a batch out and reassembles it. The batches
  here are large (`ingest/documents/sync.py:245` embeds every pending chunk in one call,
  `retrieval/vector_index.py:520` embeds one text per note), which is exactly the shape a server parallelises.
- **Consequence**: each text is assigned another text's vector, silently. `embed_texts` then caches the
  mis-paired vectors under the *correct* `(config_key, text)` keys, and the callers persist them into
  `document_chunks.embedding` / `note_index.embedding` alongside a *correct-looking* `embedding_key` — so
  `reembed_stale` will never re-embed them and no validator can detect it. Every subsequent similarity search
  over that corpus returns wrong documents, permanently, with no error anywhere. This is precisely the class of
  silent corruption `embedding_config_key`'s own docstring is written to prevent ("corrupts every similarity,
  silently, and no error is ever raised") — closed against a config change, left open against the wire format.
- **Evidence**: the response model in the installed SDK carries the index explicitly, and its docstring says
  what it is for:

  ```
  >>> from openai.types.embedding import Embedding
  {'embedding': ..., 'index': FieldInfo(annotation=int, required=True), 'object': ...}
      index: int
      """The index of the embedding in the list of embeddings."""
  ```

  The field exists because position in `data` is not the contract. The code reads
  `[item.embedding for item in response.data]`. There is also no length check: a server returning fewer items
  than requested is caught only incidentally, by `zip(..., strict=True)` on the *cached* path, and not at all
  when `embedding_cache_size <= 0`.
- **Fix**:
  ```python
  data = sorted(response.data, key=lambda item: item.index)
  if len(data) != len(texts) or [item.index for item in data] != list(range(len(texts))):
      raise ValueError(f"embedding endpoint returned {len(data)} vectors for {len(texts)} inputs")
  return [item.embedding for item in data]
  ```

---

## Every exported counter and histogram sum is truncated to six significant digits by `:g`

- **Severity**: medium
- **Location**: `src/chemclaw/core/metrics.py:531`, `:539`, `:549`, `:559`, `:562-564` (`Metrics.render`)
- **Trigger**: any counter passing 10^6. `chemclaw_tokens_total`, `chemclaw_input_tokens_total`,
  `chemclaw_cache_read_tokens_total` and `chemclaw_context_reclaimed_tokens_total` count *model tokens*, so a
  pod crosses 10^6 within hours of real traffic; `chemclaw_job_runtime_seconds_total` accumulates HPC seconds.
- **Consequence**: the scrape reports a number that is not the counter's value, and the error grows with the
  counter. Past 10^6 the exposed value quantises to steps of 1; past 10^7 to steps of 100; past 10^9 to steps
  of 1000. `rate(chemclaw_tokens_total[5m])` therefore reads 0 for minutes at a time and then spikes, on the
  series whose stated purpose (`metrics.py:244-247`) is "what is this deployment costing per hour". The same
  applies to `<histogram>_sum`, so mean turn latency is wrong once the pod has accumulated ~10^6 seconds of
  turns, and to `<histogram>_count`.
- **Evidence** (`/tmp/audit/repro_metrics_g.py`, run against `Metrics` directly):

  ```
  'chemclaw_tokens_total{profile="chemist"} 1e+06'
  'chemclaw_context_reclaimed_tokens_total 1.23457e+07'
  true value: 1000003.0
  ['chemclaw_turn_duration_seconds_sum 1.23457e+06']
  ```

  The registry's own `value()` accessor returns `1000003.0` while `/metrics` publishes `1e+06` — three tokens
  are not merely rounded in the display, they are unrecoverable by the scraper. `12_345_678` is published as
  `1.23457e+07`, i.e. off by 22.
- **Fix**: use `repr(float(x))` (or `f"{x!r}"`), which round-trips a double exactly and is valid Prometheus
  exposition, instead of `:g` — at all five sites. If integer-looking output is wanted, format as
  `f"{x:.0f}"` when `x == int(x)` and `repr` otherwise. There is a test-visible invariant worth adding:
  parsing the rendered text back must reproduce `value()`/`observations()` exactly.

---

## A comment-only edit to a migration still fails on any database whose ledger row predates the statement-only checksum, and the error says the statements changed when they did not

- **Severity**: medium
- **Location**: `src/chemclaw/core/migrate.py:183-197` (`migrate`), `_legacy_checksum` at `:121-129`
- **Trigger**: a database that recorded `filename → sha256(whole file)` (the pre-fix ledger), plus a
  comment-only edit to that file made at any time. `row[0] == _legacy_checksum(text)` compares the recorded
  hash of the **old** text against the whole-file hash of the **new** text; a comment edit changes the
  whole-file hash, so the acceptance branch is skipped, and `row[0] != _checksum(text)` is then true because
  the recorded value is a whole-file hash and `checksum` is a statement hash. The legacy escape hatch only ever
  fires for files that have *not* been edited.
- **Consequence**: `make db-migrate` refuses, so a deploy stops — the exact outage `_statements` was written
  to end ("hashing the whole file made every comment edit a deployment outage … Both edits were *right*, and
  both broke migrations for four days and an hour respectively"). And the message actively misdirects the
  operator: it asserts *"its statements differ, not just its comments"* when the statements are byte-identical,
  sending them to write a new migration file for a change that is not a schema change.
- **Evidence** (`/tmp/audit/repro_migrate_legacy.py`, live PG, scratch schema `auditlegacy`): apply a file,
  rewrite its ledger row to the legacy whole-file hash, change one leading `--` comment line, re-run:

  ```
  first run: ['900_demo.sql']
  MigrationError: migration 900_demo.sql was edited after being applied
    (its statements differ, not just its comments); add a new migration file instead
  ```
- **Fix**: make the legacy branch recognise the shape of the recorded value rather than its equality with an
  unchanged file. Record which algorithm produced a row (a `checksum_algo` column, defaulted to `legacy`), or —
  cheaper and migration-free — accept the row when `row[0]` matches *either* hash and, when it matches
  neither, distinguish the two cases before raising: only claim "the statements differ" when the recorded value
  is known to be a statement hash. A recorded legacy hash that matches nothing is "this file changed somehow
  and this database cannot tell whether it was only comments", which is a different, honest message.

---

## `pooling()` is a boolean, not a depth counter, and `make connectors` enters it once per connector

- **Severity**: low
- **Location**: `src/chemclaw/core/db.py:244-266` (`pooling`), `:63` (`_POOLING`)
- **Trigger**: `chemclaw.cli.connectors_dev.build_composite` enters **every** mounted connector app's lifespan
  through one `AsyncExitStack` (`connectors_dev.py:72-75`), and each connector lifespan is
  `async with db.pooling(), server.session_manager.run():` (`connectors/server.py:433`). With N enabled local
  connectors, `pooling()` is entered N times in one process. On shutdown the stack unwinds in reverse: the
  first `__aexit__` sets `_POOLING = False`, clears `_POOLS` and closes every pool, while the other N−1
  lifespans are still live.
- **Consequence**: the remaining connectors finish shutting down with pooling silently off — every
  `connection()` they make becomes a dedicated connect — and any of their in-flight checkouts (including the
  `on_start` diagnostic tasks the server starts and does not await) are cut against a pool being closed. The
  pool gauges also read 0 for the rest of the process's life. Contained to the dev composite today, but the
  invariant the module states ("Entered once per process") is violated by shipped code in this repo, so nothing
  stops a future second entry on a serving path.
- **Evidence** (`/tmp/audit/repro_pooling_nested.py`): two nested `pooling()` contexts, exit only the inner
  one:

  ```
  both entered: _POOLING = True pools = 1
  after inner exit (outer still live): _POOLING = False pools = 0 gauge pool_size = 0
    -> outer's connection was UNPOOLED (dedicated connect)
  ```
- **Fix**: make it a depth counter — increment on entry, decrement in the `finally`, and only clear/close the
  pools when the count reaches zero (`_POOLING` becomes `_POOL_DEPTH > 0`). `bind_pool_metrics()` should then
  run only on the 0→1 transition.

---

## `JsonFormatter` stamps `time` in the pod's local timezone with no offset

- **Severity**: low
- **Location**: `src/chemclaw/core/logging.py:948` (`JsonFormatter.format`, `self.formatTime(record)`)
- **Trigger**: `CHEMCLAW_LOG_JSON=true` (which the shipped chart sets) in any container whose `TZ` is not UTC.
  Nothing in `deploy/` or the Dockerfile pins `TZ` — `grep -rn "TZ\b|timezone" deploy/ Dockerfile*` returns
  nothing — so it is whatever the base image or the node supplies. `logging.Formatter.converter` is
  `time.localtime`, and the default `datefmt` produces `%Y-%m-%d %H:%M:%S,%03d` with no offset and no `Z`.
- **Consequence**: the one field a log stack indexes on is both ambiguous and, off UTC, wrong. Every consumer
  parsing a naked `YYYY-MM-DD HH:MM:SS` reads it as UTC, so lines are filed at the wrong instant and cannot be
  joined to `audit_events` (which stores a `timestamptz`) — defeating the stated purpose of this module's
  correlation fields. The comma-before-milliseconds spelling is also not ISO-8601, so strict parsers fall back
  to ingest time.
- **Evidence**:

  ```
  $ TZ="America/New_York" uv run python -c '...JsonFormatter().format(record)...'
  {"time": "2026-08-16 17:41:28,858", "level": "INFO", ...}
  UTC now: 2026-08-16T21:41:28.858233+00:00
  ```

  Four hours off, with nothing in the line to say so.
- **Fix**: emit an unambiguous instant instead of `formatTime`:
  `datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).isoformat(timespec="milliseconds")`
  → `2026-08-16T21:41:28.858+00:00`. (Equivalently, set `JsonFormatter.converter = time.gmtime` and
  `datefmt="%Y-%m-%dT%H:%M:%S"` with an explicit `Z` — but the `datetime` form removes the millisecond
  special-case as well.)

---

## Not findings

Checked and clean under this lens: `metrics.observe`'s `bisect_left` really does implement Prometheus `le`
semantics (a sample exactly on a boundary lands in that boundary's bucket); `db.existing_tables`' `relkind='r'`
filter is safe because no table in `infra/sql` is partitioned or a foreign table; `identity_context`'s
token-based set/reset pairs; `http.is_loopback_url`'s unparseable-URL case falls on the demanding side as
documented; `turn_signals.stream_writer_or_none`'s `(RuntimeError, LookupError, AttributeError)` really does
subsume both measured upstream shapes; `metrics_bridge.degraded(exc_info=True)` outside an `except` block
renders `NoneType: None` rather than crashing.
