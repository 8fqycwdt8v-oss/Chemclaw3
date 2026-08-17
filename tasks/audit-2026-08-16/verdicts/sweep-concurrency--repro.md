# Verdicts — sweep-concurrency (lens: does it actually reproduce?)

Scope: critical/high findings only. `sweep-concurrency.md` contains **one** such finding (the
embedding cache, severity high). The other four findings are medium/medium/medium/low/low and are
out of scope; the two trailing sections are not findings.

Working tree check: `src/chemclaw/core/embeddings.py` is byte-identical to the pristine `HEAD` copy
(`diff -q … && echo IDENTICAL` → IDENTICAL), and `git diff --stat HEAD -- src/` is empty. Nothing
below is an artifact of another agent's mutation. Python 3.11, GIL present.

## The embedding cache is a plain dict mutated from several worker threads, and a concurrent trim evicts a key the other thread is about to read

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**: I did not run the reporter's script and did not read their transcript for
  anything but the claim. I read `src/chemclaw/core/embeddings.py` and wrote four scripts of my own
  under `/tmp/vfy/`.

  First, the line numbers and symbols in the finding are all real and current: `_CACHE` is declared
  at line 45, `embed_texts` spans 140–185, the membership test is line 165, the insert loop 170–171,
  the read `vectors = [_CACHE[key] for key in keys]` is line 179, the trim
  `while len(_CACHE) > size: del _CACHE[next(iter(_CACHE))]` is 183–184, and
  `embedding_cache_size: int = Field(default=2048, ge=0)` is `core/config/llm.py:154`. Every cited
  call site matches too (`retrieval/retrievers.py:377`, `retrieval/vector_index.py:520`,
  `ingest/documents/sync.py:368,417`, `ingest/documents/retriever.py:196`,
  `ingest/eln/warehouse/retriever.py:143`).

  **(1) Shipped defaults, thread pool.** `/tmp/vfy/repro.py` — two threads embedding a 4000-text
  reindex-shaped batch, four threads looping single-text query embeds:

  ```
  $ uv run python /tmp/vfy/repro.py
  provider: hash cache size: 2048 dim: 1536
  cache size after: 2047
  errors: 1
  --- big KeyError ('hash:ep-none:d1536:model-none', 'note-1-0 some words about a reaction')
    File "/home/user/Chemclaw3/src/chemclaw/core/embeddings.py", line 179, in <listcomp>
      vectors = [_CACHE[key] for key in keys]
  KeyError: ('hash:ep-none:d1536:model-none', 'note-1-0 some words about a reaction')
  ```

  Reproduced on the first attempt, at the shipped default, on the exact line the finding names.

  **(2) It does not need an oversized batch.** `/tmp/vfy/repro2.py` — two threads, each a 1500-text
  batch, i.e. *neither* batch exceeds the 2048 cache on its own; only their union does:

  ```
  $ uv run python /tmp/vfy/repro2.py
  cache size: 2048
  KeyError: ('hash:ep-none:d1536:model-none', 't0-1-0 lorem ipsum reaction yield')
  trials with KeyError (two 1500-text batches, cache 2048): 1/5
  ```

  **(3) The interactive single-text path fails at the shipped default, not only at 64.**
  `/tmp/vfy/repro3.py` — a query embedded early (so its key is old in the FIFO), then re-embedded in
  a loop by four threads while one thread runs a 4000-note reindex batch:

  ```
  $ uv run python /tmp/vfy/repro3.py
  cache size: 2048
  single-text query-path failures: 1 | batch-path failures: 2 (6 trials)
  ```

  **(4) The shipped call shape, not a raw thread pool.** `/tmp/vfy/repro5.py` uses exactly what the
  code does — `asyncio.to_thread(embed_texts, …)` on one loop over the default executor, a corpus
  reindex under continuous query embeds:

  ```
  $ uv run python /tmp/vfy/repro5.py
  first failure: KeyError ('hash:ep-none:d1536:model-none', 'note-2-0 body')
  failing trials: 4/12 (default cache 2048)
  ```

  (For honesty about the shape of the window: `/tmp/vfy/repro4.py`, three `to_thread` calls fired
  once per trial and gathered with no continuous query traffic, produced 0/5. The race needs a trim
  to land inside another thread's insert→read window, so it wants overlapping traffic rather than a
  single simultaneous start. Continuous query traffic during a background reindex is the normal
  shape, and that is repro5.)

  Downstream, I read rather than assumed: `retrieval/fanout.py:98–105` catches `Exception` (a
  `KeyError` is one), logs, increments `chemclaw_evidence_source_failures_total` and returns
  `chunks = []` — so on the chat path the vector leg silently contributes zero chunks, exactly as
  claimed. `VectorRetriever.retrieve` (`retrievers.py:372–384`) has no handler of its own.
  No test in `tests/` exercises `embed_texts` from more than one thread (grep for
  `embed_texts` + thread/concurrent in `tests/` returns only single-call `to_thread` uses).

- **Why**: The mechanism is exactly as described and nothing upstream prevents it. The comment at
  lines 172–178 is a *claim* about a single-threaded hazard ("read before trimming"), and reading
  the code shows it closes nothing across threads: between the membership test at 165 / the insert
  loop at 170–171 and the read at 179 there is no lock, and another thread's `while len(_CACHE) >
  size: del _CACHE[next(iter(_CACHE))]` deletes oldest-first from the *whole* dict, including keys
  the first thread is still holding. Python 3.11's GIL guarantees each `del` and each lookup is
  atomic; it guarantees nothing about the sequence, and the switch interval (5 ms) is far shorter
  than a multi-thousand-text embed. Every call site is an `asyncio.to_thread`, so the default
  executor (8 workers here) genuinely runs several of these at once.

  Two things I would add that make it slightly worse than filed:
  - the single-text interactive failure happens at the **shipped** `embedding_cache_size = 2048`,
    not only at the reporter's 64 (measurement 3);
  - **no batch has to exceed the cache**: two ordinary sub-cache batches whose union crosses 2048
    are enough (measurement 2), so "corpus above 2048 notes" is not the boundary.

  One thing that softens it, which the finding does not mention and which I checked: the durable
  half retries. `reindex_notes_activity` runs under `BAD_DATA_RETRY`
  (`durable/note_index.py`, `durable/publish.py:140`), whose `non_retryable_error_types` list is the
  bad-data names; `KeyError` is not among them, so a failed reindex is retried up to
  `activity_max_attempts` and a transient race will usually pass on the next attempt. That mitigates
  the background jobs. It does not touch the interactive path, where `_sweep` swallows the error and
  the turn answers from fewer sources with nothing visible to the user — which is the reason I keep
  the severity at high rather than dropping it to medium.

  The reporter's proposed fix (lock around hit-collection and around insert+trim, provider call
  outside the lock, result built from the batch's own dict rather than by re-reading `_CACHE`) is
  the right shape: it removes the read-after-evict rather than narrowing the window.
