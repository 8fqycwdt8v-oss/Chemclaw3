# D-2026-09-07-a-changed-note-is-not-a-changed-corpus — the graph is patched, not reassembled

**Status:** accepted · **Date:** 2026-09-07 · **Closes:** the residual
`D-2026-09-06-one-note-changed-is-not-the-corpus-changed` named and deferred ·
**Narrows** that ADR's "`invalidate_cache`'s clear-everything default stays", for `_GRAPH_CACHE`
only and with the argument below.

## Context

`D-2026-09-06-one-note-changed-is-not-the-corpus-changed` made the note *parse* per file and said
in its own "What this does not fix" that the assembly was still whole — `_assemble_graph` re-adding
every node and every edge whenever the corpus fingerprint moves. Since
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` the agent is the writer, so that is paid at
usage rate rather than at a curator's commit rate.

That ADR also named the trap, and it is the reason the work was deferred rather than bundled:
patching incrementally means removing and re-adding one note's node, and `networkx.remove_node`
takes the node's **in**-edges with it. Those edges belong to *other* notes. A naive patch therefore
drops every citation *into* the changed note and leaves a graph that is well-formed, traversable,
and answering every other query correctly.

## What was measured

Own measurements, on synthetic corpora with realistic frontmatter and four wikilinks a note. Arms
interleaved rather than run in sequence, because three other agents share this machine and a
sequential A/B measures load drift; load average 0.97–1.88 across the runs, and the deciding
20,000-note figure was re-taken under both a quiet machine and a busy one with the ratio unchanged.

`build_graph` after touching **one** file, which is what a note write costs the next reader.
"Loop lost" is the time an `asyncio` heartbeat is late by while the build runs under
`asyncio.to_thread` — the GIL is held, so a worker thread does not spare the front door.

| notes | before, wall | after, wall | before, loop lost | after, loop lost |
|---|---|---|---|---|
| 1,000 | 45.8 ms | **29.5 ms** (×1.55) | 15.7 ms | **7.5 ms** (×2.09) |
| 5,000 | 383.7 ms | **217.0 ms** (×1.77) | 242.1 ms | **115.4 ms** (×2.10) |
| 20,000 | 1,724.8 ms | **1,085.4 ms** (×1.59) | 1,176.2 ms | **663.8 ms** (×1.77) |

The assembly term itself, isolated at 20,000 notes: `_assemble_graph` 939.7 ms against
`_patch_graph` 359.9 ms. The rest of the 1,085 ms is the stat scan (204.6 ms) and the per-file
loop in `_parse_notes` reading through `_PARSED_FILES` (442.1 ms) — both named in the parent ADR,
neither touched here.

**The measurement is also what corrected this commit's own first threshold.** The break-even
between patching and rebuilding was *reasoned* at "roughly a sixth of the corpus" from a per-note
cost ratio, and written into the constant. Measured at 20,000 notes against an 884 ms rebuild, a
patch costs 399 ms at 0.01% changed and 981 ms at 50%: the copy is a ~400 ms floor, a changed note
costs ~58 µs against `_assemble_graph`'s ~44 µs, and the curves cross near **42%**. The guess was
wrong by a factor of two and a half, in the direction that would have declined work worth taking.

## Decision

**`_patch_graph` brings the cached graph up to the current notes by touching only what changed**,
and `build_graph` uses it whenever a cached graph exists at a stale fingerprint. Five things it is
built out of, each of which is the answer to a way of getting this wrong:

- **`_detach_note` removes a note's out-edges and its `note` attribute, never its node.** What
  survives is exactly what a rebuild of the corpus-without-that-note produces: a bare node if
  anything still cites the id, nothing at all if not (`_drop_if_uncited`). This is the trap, taken
  head-on rather than avoided.
- **Every detach runs before any attach**, so the intermediate state is a rebuild of the unchanged
  corpus and the result cannot depend on iteration order.
- **`_attach_edges` is one function, shared with `_assemble_graph`.** The whole correctness claim is
  that the patch produces what the rebuild would; two loops deriving "what edges does this note
  contribute" is two places for that to quietly stop being true.
- **What changed is decided by object identity.** `_PARSED_FILES` hands back the very same frozen
  `Note` for a file whose `(mtime_ns, size)` has not moved, so `is` answers "was this re-read" in
  O(1) per note — and errs only toward more work.
- **Patched on a copy, never in place.** `build_graph` hands every caller the same frozen instance
  and its docstring says freezing is what makes that sharing safe. Mutating the cached graph is a
  `RuntimeError` out of an adjacency iteration in whatever query is running. The copy is 45% of a
  rebuild rather than 100%, and it is the price of that invariant.

**`invalidate_cache` keeps `_GRAPH_CACHE`, and that is what makes the patch reach the path it
exists for.** Measured first without this and the change was worth **nothing** — 1,618 ms against
1,584 ms — because `kg/git_writer.py` calls `invalidate_cache()` after every note write and the
base to patch from had just been thrown away. The graph cache is the one cache here that can be
kept: `_LAST_SCAN` is what lets `_within_ttl` serve `_NOTES_CACHE` *without* a scan, so a stale
pair there is served as current, whereas `build_graph` returns its graph entry only on exact
fingerprint equality and otherwise uses it purely as a base. It adds no class of staleness that is
not already accepted — after that call a file whose stat has not moved is already served from
`_PARSED_FILES` unchanged, and the graph is keyed on the same two fields.

## Consequences

- `kg/graph.py`: `_MAX_PATCHED_FRACTION`, `_attach_edges`, `_drop_if_uncited`, `_detach_note`,
  `_patch_graph`; `_assemble_graph` and `build_graph` read through them; `invalidate_cache` no
  longer drops `_GRAPH_CACHE`.
- **Memory, stated rather than hidden.** Retaining the graph across `invalidate_cache` costs
  **+65.1 MB** at 20,000 notes over the `Note` objects `_PARSED_FILES` holds either way, and the
  patch's copy peaks **+29.1 MB** transiently. For a pod that serves queries this is the steady
  state it already had — the graph was freed on a write and rebuilt by the next read; what is new
  is only the window between them. For a process that read a tree once and never again, that graph
  is now held for the life of the process.
- `tests/test_graph.py`: five tests, each watched failing against a specific wrong version rather
  than only against the unfixed source. `test_one_changed_note_does_not_reassemble_the_whole_graph`
  fails against the unfixed module (`assert 2 == 1`, counting `_assemble_graph` calls — counted,
  not timed, for the reason the parent ADR counts `read_note` calls).
  `test_a_patched_graph_is_identical_to_the_rebuilt_one` and
  `test_changing_a_note_keeps_the_citations_into_it` fail against a `remove_node` patch, reporting
  the ten missing `citer-N → hub` in-edges.
  `test_a_graph_already_handed_out_is_not_mutated_by_a_later_patch` fails against a patch applied
  in place. The equality assertion compares node set, edge set **and every attribute on both**,
  against a fresh reassembly — anything weaker can be passed by a patch that drops one citation.
- Beyond the five: a differential fuzz over ~4,000 randomized corpora per seed (add, delete,
  change, retire, dangling targets, self-citation) found no divergence from the rebuild, and 3,198
  of 3,952 trials diverged when `_detach_note` was armed with `remove_node` — so the corpus has
  teeth rather than merely being green.

## What this does not fix

- **The stat scan** (204.6 ms) and **the per-file loop through `_PARSED_FILES`** (442.1 ms) are now
  the larger half of a warm rebuild at 20,000 notes. The scan is the floor
  `graph_cache_ttl_seconds` already buys a window against; the loop is O(notes) dict lookups and
  would need the fingerprint itself to become incremental.
- **A cold start still assembles the whole graph**, because there is nothing to patch from. That is
  the correct cost and not a residual.
