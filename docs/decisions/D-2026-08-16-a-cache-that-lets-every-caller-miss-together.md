# D-2026-08-16-a-cache-that-lets-every-caller-miss-together — six findings in layer 4

**Status:** accepted · **Date:** 2026-08-16 · A review of `chemclaw.kg` and the three hot paths that
read it (`agent/graph_tools.py`, `retrieval/retrievers.py`, `durable/digest.py`). No decision is
superseded; four defects are closed, one feature gap is closed, and three candidate changes are
declined with the measurement that declined them.

## Context

Layer 4 is the one part of this system with no database under it: the corpus is Markdown in Git,
and every read goes through `kg/graph.py`'s three caches — parsed notes, the assembled NetworkX
graph, and (in `kg/conflicts.py`) the conflict index — each keyed on one stat fingerprint of the
notes directory. The design is right and is argued at length in both modules. What this review
found is that **two sibling caches, built for the same corpus and documented in the same terms,
disagreed about the one thing that matters when they are cold**, and that three readers of the
graph were reading it slightly wrong in ways no test could see.

Everything below was measured on a synthetic programme-shaped corpus — 2,000 notes, 3.91 MB of
body text, 16 note types over 7 substrates — rather than argued from the code, because the two
largest findings are about contention and the smallest is about a number.

## What was measured

### 1. Concurrent cold reads each parsed the whole corpus

`cached_notes` was a textbook double-checked lock: take `_CACHE_LOCK`, miss, **release it**, do the
O(notes) stat scan and the full parse, take it again to store. Correct, and it means every caller
that misses together does all of the work.

| concurrent cold `load_notes` | wall clock |
|---|---|
| 1 thread | 198 ms |
| 4 threads | **2,521 ms** |
| 8 threads | **6,219 ms** |

Eight callers did not pay 8× the work. They paid **31×**, because eight parses of one tree contend
on the GIL as well as duplicating each other. This is not a startup-only cost: a `gather_evidence`
sweep runs its sources under `asyncio.gather` with `load_notes` offloaded to a thread each, the
report harness runs one per section, and `invalidate_cache()` is called by the PR-gate's
parked-checkout repair and by the note reindex — so the cold window recurs.

`kg/conflicts.py` had already met this exact problem and solved it, holding `_INDEX_LOCK` across
the *computation* with the argument written into the module: "a second caller waiting is strictly
better than a second caller duplicating: it waits exactly as long as the work it would otherwise
have redone." Its measurement — 4,238 ms for three concurrent computations against 1,525 ms for
one — is the same shape as the table above. The sibling in `graph.py` never got the same
treatment.

### 2. Two files claiming one note id collapsed in silence, and the two scans disagreed about which

Measured directly: a tree holding `compound/x.md` and `reaction/x.md` yields five parsed notes and
five graph nodes. `_assemble_graph` calls `add_node(note.id, …)` for both, so whichever file sorted
*last* replaced the other — one of two curated notes unreachable by every query, the winner decided
by a directory name, and nothing anywhere saying so.

Worse than the silence: `note_file_fingerprints` was a dict comprehension over the same scan, where
the last entry also won — but it keys on `path.stem` and the parse keys on `note.id`, so the two
readers of one tree could name **different files**. `reindex_notes` diffs one against the other, so
that disagreement is a note embedded under another note's id.

`kg-validate` fails a duplicate id, which is a property of the repository and not of the tree a pod
is serving — the same distinction that made `chemclaw_notes_unparseable_total` exist. An rsync that
lands a renamed note before removing the old one produces this state in a healthy deployment.

### 3. `find_knowledge_gaps` reported notes that do not exist as the graph's top hubs

`build_graph` deliberately keeps a link to an unknown id as a node with no `note` attribute, so
`kg-validate` can report it rather than the graph silently dropping the edge. `analytics._hubs`
ranked *every* node by in-degree — including those, by the very citations that make them dangling.

Measured: on a corpus where four notes cite a `compound-pending` that has no file, `analyze`
returns `[('compound-pending', 4), …]` — the missing note as **the most-cited note in the graph**.
D-018 makes this the normal state rather than a corruption: a fingerprint-indexed reaction is
citable before its note clears the PR-gate. So the tool whose whole purpose is "what should a
reviewer check first" answered with a note `expand_note` then refuses to open.

### 4. The digest re-tokenized its query once per note

`digest._matches` called `query_terms(subscription.query)` inside the per-note loop. Over 50
subscriptions and 2,000 notes that is 100,000 regex splits of a string that never varies:
**352 ms → 225 ms** by hoisting it, on an activity that runs hourly and holds a background worker
for the whole of it.

### 5. Typed edges reached no reader

D-134 gave edges a `rel`, a `confidence` and their own validity window; `Relation`,
`Note.outgoing_relations` and `graph.related` all work and are tested. **Nothing in `src/` read
any of it.** `kg/README.md` says so about `related` and treats it as a deliberate hold, which it
was — but the consequence had not been drawn: `expand_note` returns neighbours as bare `NoteRef`s,
so a `contradicts` neighbour arrives indistinguishable from an ordinary citation. Including the
`contradicts` edge `record_failure` writes for the express purpose that a refuted note "arrives
marked as disputed"; that promise was kept by the retrieval path's conflict flags and by nothing on
the traversal path the agent is told to use.

### 6. `find_notes` assembled a graph it never traverses

It is a substring sweep over each note's own metadata and body and follows no edge, but it read
`build_graph` — so a cold call paid node and edge insertion for nothing (220 ms against 184 ms for
the parse alone) and then iterated dangling link targets it skipped one line later.

## Decision

Five changes, each with a regression test that fails without it:

1. **A per-directory re-entrant computation lock in `kg/graph.py`**, held across the scan, the
   parse *and* the assembly — the shape `conflicts.py` already argued for. Re-entrant because
   `build_graph` holds it across `cached_notes`, so eight cold builders share one parse *and* one
   assembly rather than sharing the parse and each assembling their own. A waiter that reaches the
   lock finds the answer it queued for and never rescans, because the winner has just stamped
   `_LAST_SCAN`. Skipped entirely when `graph_cache_enabled` is false: there is no cache to fill,
   so serializing would answer a question nobody asked.
2. **One deterministic winner for a duplicate id — first in path order**, matching
   `kg/validate.py`'s `id_to_path`, applied in `_parse_notes` *and* `note_file_fingerprints` so
   every reader of one tree names the same file. Reported at WARNING with the losing path and
   counted as `chemclaw_notes_duplicate_id_total`, on exactly the terms an unparseable note already
   gets.
3. **`_hubs` ranks only nodes that carry a note.** A dangling target is not dropped, it is reported
   as what it is: `GraphGaps.dangling_links`, filled by the same `analyze` from the same graph.
4. **The digest's query is tokenized once per subscription.**
5. **`expand_note`'s neighbours carry the typed edges that connect them**, in `relations_out` and
   `relations_in`. Two fields rather than one because direction is the claim: "A supersedes B" and
   "B supersedes A" are opposite statements about which note is current, and `contradicts`,
   `precursor-of` and `computed-from` have the same asymmetry. `cites` is filtered out — it is
   `DEFAULT_RELATION`, what every untyped `[[wikilink]]` already means, so reporting it would put
   the word on most neighbours while drowning the edges an author typed on purpose. An empty pair
   means "adjacent, nothing asserted about how", which is what a two-hop neighbour honestly is.

And `find_notes` reads `load_notes`, sorted by id — the order its own truncation warning already
promises.

## Result

| | before | after |
|---|---|---|
| cold `load_notes`, 4 concurrent threads | 2,521 ms | **201 ms** |
| cold `load_notes`, 8 concurrent threads | 6,219 ms | **252 ms** |
| corpus parses for 8 concurrent cold readers | 8 | **1** |
| graph assemblies for 8 concurrent cold builders | 8 | **1** |
| `collect_digests` match loop, 50 subscriptions | 352 ms | **220 ms** |
| cold `find_notes` | 220 ms + sweep | **192 ms** |

`make lint type test` green with Postgres and Temporal up: 4,051 passed. (One unrelated failure,
`test_no_grandfathered_edit_outlives_its_reason`, reproduces on the unmodified tree — this
environment's clone is shallow with 170 commits, so the test's `compared < 30` skip guard does not
fire while its history-diff finds nothing edited. It is an environment coupling in that guard, not
a defect in this change, and it is left for the module that owns it.)

## What was declined, and why

**Caching the lowered search haystack per note.** `search_text(note).lower()` is rebuilt for every
note on every query, by `find_notes`, `GraphRetriever` and the digest alike. Measured: 4.48 ms →
2.02 ms on an interactive query over 2,000 notes, and 225 ms → 110 ms on the digest's 50×2,000
pass. Real, and declined: it buys 2 ms on the path a chemist waits on, at the cost of a fourth
fingerprint-keyed cache layer and ~4 MB of duplicated text held live beside the notes it was
derived from. The half of it that was free — the digest's hoisted tokenization — is taken above.
*Revisit if a corpus an order of magnitude larger makes the interactive figure a number anyone can
feel.*

**Validating that a note's file sits in a `<type>/` directory.** `note_relative_path` writes
`<type>/<id>.md` and every note in the shipped corpus obeys it, so a check looks free. It is not
an invariant: only `path.stem` is an index key, nothing reads the parent directory, and the suite
writes note trees flat in `tmp_path` in dozens of places. Enforcing a layout no reader depends on
would make the validator assert a convention rather than a contract — and the failure it would
catch (a re-proposal landing the same id at a second path) is already caught, as a duplicate id, by
the check that exists.

**Clearing `conflicts._INDEX_CACHE` from `graph.invalidate_cache`.** It looks like an omission and
is not: the conflict index re-derives its fingerprint through `cached_notes`, so a bust upstream
invalidates it downstream on the next read. The coupling is real but stated in both modules, and a
registration hook for two caches is an abstraction with no third caller. *Revisit if a third
derived cache appears, or if one ever computes its own fingerprint.*
