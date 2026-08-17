# Verdicts — sweep-concurrency, lens: is the trigger reachable and is the consequence what is claimed?

Scope note: exactly **one** finding in `sweep-concurrency.md` is marked critical or high (the
embedding cache). The other five are medium/low and are out of scope; no verdict is given for them.

Working tree check: `src/chemclaw/core/embeddings.py` is byte-identical to the pristine `HEAD` copy
(`diff … && echo IDENTICAL` → `IDENTICAL`), so nothing below is an artifact of another agent's
mutation.

## The embedding cache is a plain dict mutated from several worker threads, and a concurrent trim evicts a key the other thread is about to read

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed). One sub-claim is softened, and two things the
  reporter did not name make it worse.

### What I did

**1. Reproduced the stated defect at shipped defaults** (`embedding_cache_size = 2048`,
`embedding_dim = 1536`, `hash` provider — no config override):

```
$ uv run python /tmp/x/repro_embed.py
cache size = 2048 dim = 1536
errors: 1
--- big0
  File "/home/user/Chemclaw3/src/chemclaw/core/embeddings.py", line 179, in <listcomp>
    vectors = [_CACHE[key] for key in keys]
KeyError: ('hash:ep-none:d1536:model-none', 'note-0-2443')
```

Two threads on 4,000-text batches plus four on single-text queries. Line 179 is exactly the read the
finding names.

**2. Checked whether the *front-door-only* shape reaches it — no big batch anywhere.** Eight threads
doing nothing but single-text query embeds (one hot repeated query + one fresh one, which is what a
retrieval process actually does):

```
$ uv run python /tmp/x/repro_query_only.py
cache size = 2048
errors: 1
  File "/home/user/Chemclaw3/src/chemclaw/core/embeddings.py", line 184, in embed_texts
    del _CACHE[next(iter(_CACHE))]
KeyError: ('hash:ep-none:d1536:model-none', 'fresh-730')
```

and, from the same workload, threads dying on

```
RuntimeError: dictionary changed size during iteration
  File "src/chemclaw/core/embeddings.py", line 184, in embed_texts
    del _CACHE[next(iter(_CACHE))]
```

**This is a second failure mode the finding does not name**: the trim races *itself*. Two threads
both evaluate `next(iter(_CACHE))`, get the same oldest key, and the loser's `del` raises `KeyError`;
or one thread's insert lands while the other is inside `iter(_CACHE)` and `next()` raises
`RuntimeError`. Neither needs a batch larger than the cache — a single-text query embed is enough
once the cache is at its bound, which any long-lived retrieval process reaches after 2,048 distinct
texts.

**3. Measured the rate in that front-door shape**, since severity turns on it:

```
$ uv run python /tmp/x/rate.py 3
threads=3 calls=529406 failures=81 rate=1.53e-04 (1 in 6535)
$ uv run python /tmp/x/rate.py 8
threads=8 calls=526390 failures=81 rate=1.54e-04 (1 in 6498)
```

~1 failure per 6,500 `embed_texts` calls with only three threads, counting `KeyError` and
`RuntimeError` together.

**4. Checked the production provider, which the CPU-bound repro understates.** Under
`openai_compatible` the miss path (`_embed_uncached`, line 170) is a network round trip, and it sits
*inside* the unguarded window — between the membership test at line 165 and the read at line 179. So
a call that saw a key as a hit can lose it during its own provider request. Simulated with a 0.5 s
provider call on the reading thread only:

```
$ uv run python /tmp/x/slow_provider2.py
cache at bound: 2048
B: inserted 60 fresh texts and trimmed
A failed: True
    vectors = [_CACHE[key] for key in keys]
KeyError: ('hash:ep-none:d1536:model-none', 'chunk-0')
```

The window in the deployment that matters is **as wide as one embeddings round trip**, not as wide
as a Python loop.

**5. Traced reachability to the outermost entry point, looking for anything that stands in the way.**
Nothing does:

- There is **no lock, no `threading.local`, no single-thread confinement** anywhere on this path.
  `_CACHE`, `clear_embedding_cache`'s `_CACHE.clear()` and the trim are all bare dict operations.
- **Every** call site is `asyncio.to_thread(embed_texts, …)` — `retrieval/retrievers.py:377`,
  `retrieval/vector_index.py:520`, `ingest/documents/retriever.py:196`,
  `ingest/eln/warehouse/retriever.py:143`, `ingest/documents/sync.py:368,417`. `to_thread` *is* the
  default executor, so concurrency is the design, not an accident.
- Concurrency needs no second user: one `gather_evidence` turn fans out one branch per source, and
  three of those sources (`VectorRetriever`, the document-share retriever, the warehouse retriever)
  each embed the query on their own thread. The repo's own
  `tests/test_event_loop_offload.py` asserts that overlap and passes (`2 passed in 0.45s`).
- Batches larger than the 2,048 bound are ordinary, not pathological: `reindex_notes`
  (`vector_index.py:520`) embeds every changed note in **one** batch, and
  `ingest/documents/sync.py:245` (`_chunks_for`) embeds every chunk of a document-share pass in one
  batch, with a comment saying it does so deliberately.
- `tests/test_embedding_cache.py` contains no thread or lock at all — nothing pins this property.

### Why

The mechanism is real, the trigger is reachable from an ordinary authenticated turn with no unusual
input, and the consequence is close to what is claimed. Specifically:

**Reachable — yes, at the outermost entry point.** A single HTTP turn is sufficient: three retrieval
branches embed concurrently on default-executor threads. No pydantic model, validator, Helm default,
startup guard or caller-side constraint narrows this; the shipped default `embedding_cache_size =
2048` is what I reproduced against, and the field's only constraint is `ge=0`.

**Consequence — as stated on the chat path, softened on the durable path.**

- Chat path: `retrieval/fanout.py:98-105` catches `Exception`, logs, increments
  `chemclaw_evidence_source_failures_total` and substitutes `chunks = []`. The finding says exactly
  this, and it is the serious half — a chemist gets an answer built on fewer evidence sources with
  **no user-visible indication that a source was dropped**. The failure is visible to an operator in
  logs and a counter, not to the person reading the answer. "The caller catches it" is therefore not
  a mitigation here: the caught exception is converted into a quieter, worse answer.
- Durable path: the finding's "a note reindex … fails" is slightly overstated. `KeyError` is a
  `LookupError`, and `BAD_DATA_RETRY`'s `non_retryable_error_types` (`durable/publish.py:32-36`)
  lists only `ValueError`/`ValidationError`/`ChemclawError` and friends, so `reindex_notes_activity`
  and the document-sync activities are **retried** up to `activity_max_attempts` and will normally
  self-heal. That costs a wasted whole-corpus embed, not a lost index. This is the one place I would
  trim the finding's wording; it does not change the verdict or the severity.

**Two things that make it worse than filed**, both of which I would add to the fix:

1. The trim races *itself* (line 184), producing `KeyError` **and**
   `RuntimeError: dictionary changed size during iteration`, with **no large batch required** — so
   the plain interactive retrieval process is exposed on its own, not only when a corpus job
   overlaps a query. The finding's proposed fix (lock around read/insert/trim, build the result from
   the batch's own dict) does close this too, but the reporter's stated reason —
   "read-after-evict" — is only half of what the lock is for.
2. Under `openai_compatible` the unguarded window contains the provider round trip, so it is
   ~500 ms wide in my simulation rather than microseconds. The reporter measured the `hash` provider
   only and so measured the narrow case.

Severity **high** stands: reachable from an unauthenticated-shaped ordinary request path with no
precondition, measurable at 1-in-6,500 calls with only three threads, widening to a round-trip-sized
window in the configuration a real deployment uses, and landing as a silently under-evidenced answer
to a chemist. It is not critical: no wrong value is ever *returned* (the failure is always an
exception, never a mismatched vector), no data is corrupted, and the durable jobs retry.
