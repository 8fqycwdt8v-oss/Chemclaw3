# D-2026-09-06-one-note-changed-is-not-the-corpus-changed — the note cache is per file, not per tree

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-09-05-the-gate-follows-behaviour-not-knowledge (the agent writes notes directly, so the
corpus changes at usage rate), D-2026-08-02-embed-only-what-changed (the same argument, taken for
embeddings, over the same stat scan) ·
**Does not supersede** `invalidate_cache`'s clear-everything default, which stays.

## Context

`kg/graph.py` holds two whole-corpus caches keyed on one fingerprint over every note file's
`(path, mtime_ns, size)`. `kg/git_writer.py` calls `invalidate_cache()` after every note write —
correctly, because under-clearing serves a note the caller just wrote as absent. What that costs is
that **any** change re-parses **everything**.

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` made the agent the writer. So the corpus now
changes at *usage* rate rather than at a human's commit rate, and each change bills the next reader
a full re-parse.

## What was measured

Synthetic corpora with realistic frontmatter and four wikilinks a note, load average 1.20–1.37:

| notes | cold `load_notes` | `build_graph` after touching **one** file | after, with this change |
|---|---|---|---|
| 1,000 | 155.5 ms | 159.1 ms | **39.7 ms** |
| 5,000 | 688.1 ms | 898.5 ms | **333.3 ms** |
| 20,000 | 2,969.4 ms | 4,031.9 ms | **1,702.9 ms** |

A third of the pre-change cost is taken off the event loop even under `to_thread`, because the
parse holds the GIL: 1,245 ms lost of a 3,935 ms rebuild at 20,000 notes, max single stall 162 ms.

The residual after this change is **not** the parse. At 20,000 notes it is 214 ms of stat scan plus
~1,450 ms of `_assemble_graph`, and that half is stated below rather than claimed fixed.

## Decision

**`_PARSED_FILES` caches parse outcomes per file**, keyed by directory then path, holding the same
`(mtime_ns, size)` pair `_dir_fingerprint` already compares — so the two agree by construction
rather than by a second convention. `_parse_notes` re-reads only the files whose pair moved.

Four properties it was written to keep:

- **The log's denominator is a property of the corpus, not of what this process re-read.** The
  cached outcome is the `Note`, `None` for a file that is not a note, or the `NoteError`'s message,
  so a reused entry re-emits exactly the warning, the metric and the summary count a fresh parse
  would. A corpus with four thousand bad notes must not look like one with two.
- **`invalidate_cache` does not clear it, and that is the point.** It holds no aggregate — every
  entry is keyed on its own file's stat — so unlike `_NOTES_CACHE` it cannot serve a stale corpus.
- **A path that leaves the scan loses its entry**, in the same pass. `(mtime_ns, size)` is a strong
  signal for a file that has existed continuously and a guessable one for a path that has been away
  and come back smaller.
- **Off with the cache.** With `graph_cache_enabled` false there is no cache and no `_corpus_lock`;
  the per-file map is skipped too, so that mode still means "parse it yourself".

## What this does not fix

- **The graph assembly.** `_assemble_graph` rebuilds every node and edge whenever the corpus
  fingerprint moves, and that is the ~1,450 ms still in the 20,000-note figure above. Patching it
  incrementally means removing and re-adding a changed note's node, and `remove_node` takes its
  **in**-edges from unchanged notes with it — a correctness trap serious enough that it wants its
  own measurement and its own commit rather than being bundled here.
- **The stat scan**, 214 ms at 20,000 notes, which is the floor `graph_cache_ttl_seconds` already
  buys a window against.
- **Resident memory.** The per-file map holds references to `Note` objects `_NOTES_CACHE` is
  holding anyway; the addition is dict overhead plus entries for non-notes.

## Consequences

- `kg/graph.py`: `_PARSED_FILES`, `_parsed_files`, `_note_for`; `_parse_notes` reads through them
  and its summary line gains a `reused` count, so an operator can see the cache working.
- `tests/test_graph.py` asserts it by **counting `read_note` calls**, not by timing — a wall-clock
  threshold on a synthetic corpus is a machine-load assertion, and what changed is how many files
  are read. Both tests were watched failing against the unfixed parse: the first read all four
  files where one had changed.
