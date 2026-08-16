# Deep review of knowledge management (layer 4 + its read paths)

Scope: `src/chemclaw/kg/` and everything that reads it on a hot path —
`agent/graph_tools.py`, `retrieval/retrievers.py`, `durable/digest.py`.

Every item below was **measured before being called a defect**, on a synthetic
programme-shaped corpus (2,000 notes, 3.91 MB of body, 7 substrates, 16 note types)
built by `tasks/live-test/`-style generation rather than argued from the code.

## Baseline, measured before any change

Docker, Postgres and Temporal started first (`sudo -n dockerd`, `make up`, `make db-migrate`),
because a local `pytest` without them skips ~157 Postgres tests and still prints green.

| | result |
|---|---|
| `pytest tests/test_{graph,note,note_search,conflicts,crosslink,graph_tools,note_proposals}.py` | 148 passed |
| cold `load_notes`, 1 thread | 198 ms |
| cold `load_notes`, 4 threads | **2,521 ms** |
| cold `load_notes`, 8 threads | **6,219 ms** |
| `collect_digests` match loop, 50 subscriptions | 352 ms |

## The findings

- [x] **K1 — concurrent cold reads of the corpus each parse it in full.** `kg/graph.py`'s
      double-checked caches release `_CACHE_LOCK` before `_dir_fingerprint`/`_parse_notes`, so
      every thread that misses together parses the whole tree. Measured 198 ms at one thread,
      **2,521 ms at four, 6,219 ms at eight** — 31× the work for 8× the callers, because the
      duplicated parses contend on the GIL. Reachable on every process start and after every
      `invalidate_cache()` (the PR-gate's `_repair_parked_checkout` and the note reindex both
      call it), with three retrieval sources and the report harness all offloading `load_notes`
      to threads at once. `kg/conflicts.py` already holds its lock across the computation and
      argues exactly this ("a second caller waiting is strictly better than a second caller
      duplicating"); its sibling in `graph.py` did not.
      **Fix:** a per-directory computation lock, so one caller computes and the rest wait.

- [x] **K2 — two notes with one id collapse in the served tree, silently.** Measured: a corpus
      with `reaction/rxn-1.md` and `compound/rxn-1.md` yields 5 parsed notes and 5 graph nodes;
      whichever file sorts last wins `add_node`, and the other note is unreachable by every
      query. `kg-validate` catches it in the repository, which is not the tree a pod serves — the
      same argument that made `chemclaw_notes_unparseable_total` exist. Worse, the winner
      *disagreed* between readers: `_parse_notes` (list, both present) and
      `note_file_fingerprints` (dict, last-wins) named different files, so the reindex could
      embed one note's text under the other's id.
      **Fix:** one deterministic winner (first in path order, matching `kg/validate.py`'s
      `id_to_path`), applied in both scans, with a WARNING and
      `chemclaw_notes_duplicate_id_total`.

- [x] **K3 — `find_knowledge_gaps` reports notes that do not exist as the graph's top hubs.**
      Measured: on a corpus where four notes cite a still-pending `compound-pending`, `analyze`
      returns it as the **most-cited note in the graph**. `_hubs` ranks every node by in-degree,
      and `build_graph` deliberately keeps a dangling target as a node with no `note` attribute.
      D-018 makes this the normal state, not a corruption: a fingerprint-indexed reaction whose
      note is still in PR-gate review is cited before it exists. The agent is told to check that
      hub first and `expand_note` then raises.
      **Fix:** rank only nodes that carry a note; dangling targets keep their own field.

- [x] **K4 — the digest re-tokenizes its query once per note.** `durable/digest.py::_matches`
      calls `query_terms(subscription.query)` inside the per-note loop: for 50 subscriptions over
      2,000 notes that is 100,000 regex splits of a string that never changes. Measured
      **352 ms → 225 ms** by hoisting it, on an hourly activity.

- [x] **K5 — typed edges reach no reader.** D-134 put `rel`, `confidence` and per-edge validity
      on the graph; `Relation`, `outgoing_relations` and `graph.related` all work. But
      `expand_note` returns neighbours as bare `NoteRef`s, so the model cannot tell a
      `contradicts` neighbour from a `cites` one — including the `contradicts` edge
      `record_failure` writes for the express purpose that a refuted note "arrives marked as
      disputed". Direction is load-bearing here: "A supersedes B" and "B supersedes A" are
      opposite claims.
      **Fix (feature):** neighbours carry the typed edges that connect them to the anchor, with
      the two directions kept apart.

- [x] **K6 — `find_notes` assembles a graph it never traverses.** It calls `build_graph` for a
      pure substring sweep over note metadata, so a cold call pays node/edge insertion for
      nothing, and the sweep then iterates dangling ids that can never match.
      **Fix:** read `load_notes`, sort by id (the order the cap warning already claims).

## Verification plan

- [x] A regression test per finding, each proving the *behaviour*, not the shape:
      concurrent cold parses count one parse; a duplicate id logs, counts and resolves the same
      way in both scans; a dangling hub is absent from `most_cited` and present in
      `dangling_links`; `_matches` is unchanged for every query; a `contradicts` neighbour is
      reported with its direction; `find_notes` returns exactly what it returned before.
- [x] Re-measure K1 and K4 after the change and record the numbers.
- [x] `make lint type test` green with infrastructure up (no skipped Postgres tests).

## Review

**Shipped.** `make lint` and `make type` clean over 593 files; `make test` with Postgres and
Temporal up: **4,051 passed, 1 failed**. That one failure,
`test_no_grandfathered_edit_outlives_its_reason`, **reproduces on the unmodified tree** (verified by
stashing) — this environment's clone is shallow with 170 commits, so the test's `compared < 30` skip
guard never fires while its history diff finds nothing edited. An environment coupling in that
guard, left for the module that owns it. 12 new tests, each verified to fail without its fix.

| measurement | before | after |
|---|---|---|
| cold `load_notes`, 4 concurrent threads | 2,521 ms | **201 ms** (12.5×) |
| cold `load_notes`, 8 concurrent threads | 6,219 ms | **252 ms** (24.7×) |
| corpus parses for 8 concurrent cold readers | 8 | **1** |
| graph assemblies for 8 concurrent cold builders | 8 | **1** |
| `collect_digests` match loop, 50 subscriptions | 352 ms | **220 ms** (1.6×) |
| cold `find_notes` | 220 ms + sweep | **192 ms** |

The decision record is
[`D-2026-08-16-a-cache-that-lets-every-caller-miss-together.md`](../docs/decisions/D-2026-08-16-a-cache-that-lets-every-caller-miss-together.md).

**What was considered and deliberately not done**, so it is not re-derived:

- *Caching the lowered search haystack per note.* Measured 4.48 ms → 2.02 ms on an interactive
  query over 2,000 notes. Real, and not worth a fourth fingerprint-keyed cache layer plus ~4 MB
  of duplicated text held live; K4's hoist took the part that was free.
- *Validating that a note's file sits in a `<type>/` directory.* The layout is a convention the
  code does not depend on — only `path.stem` is an index key — and the suite writes note trees
  flat in `tmp_path` in dozens of places. A check would enforce a rule nothing reads.
- *Clearing `conflicts._INDEX_CACHE` from `graph.invalidate_cache`.* It is already correct: the
  conflict index re-derives its fingerprint through `cached_notes`, so a bust upstream
  invalidates it downstream. A registration hook for two caches is an abstraction with no third
  caller.
- *Making `analytics._DISTILLED_TYPES` connector-extensible.* Consistent with the `note_types:`
  seam in principle, with no bundle today declaring a type that distils anything.
