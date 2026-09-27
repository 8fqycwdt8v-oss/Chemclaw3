# D-2026-09-16-a-stat-cache-and-a-content-hash-do-not-agree-about-what-changed — the re-index embedded the old body under the new digest, and would never have healed

## Status

Accepted. Closes a defect introduced by
`D-2026-09-16-a-fingerprint-that-names-a-checkout-is-not-a-fingerprint-of-a-note`, whose decision
stands. It narrows `D-2026-09-06-one-note-changed-is-not-the-corpus-changed` by one caller rather
than reversing it.

## Context

Two caches answer "what changed" in the knowledge tree, and `reindex_notes` reads both.

`_PARSED_FILES` is the per-file parse cache. `invalidate_cache` deliberately does **not** clear it,
and its docstring argues why:

> after this call a file whose `(mtime_ns, size)` has not moved is served from `_PARSED_FILES`
> unchanged, so a write invisible to the fingerprint is already invisible to the parse. The graph is
> keyed on the same two stat fields and can therefore be wrong in exactly the same cases and no
> others.

That was a correct argument about a `note_file_fingerprints` that returned `"mtime_ns:size"`. Both
halves were blind in the same way, so a write neither could see changed nothing and nothing was
re-embedded.

`note_file_fingerprints` became a **hash of the file's bytes** earlier the same day, for a good
reason: an mtime is a property of a checkout, so two pods at one commit re-embedded each other's
work for ever. That fixed its own defect and silently falsified the sentence above — and this one
is worse than what it replaced, because the two halves now disagree in the harmful direction.

Measured, on a note edited to the same size with its mtime restored (what a `git checkout` between
two branches differing by a few characters looks like on a filesystem that restores times), after
`invalidate_cache`:

```
fingerprint moved : True
load_notes body   : 'The body says AAAA.'      <- the previous content
bytes on disk     : 'The body says BBBB.'
```

`reindex_notes` diffs the *hash* to decide what changed and reads `load_notes` for the text to
embed. So it embeds the **old** body, stores it under the **new** digest, and on every later run the
digest matches — the row is wrong, costs an embedding call to become wrong, and never heals. Before
the fingerprint was a hash, this file simply was not re-embedded at all.

## Decision

`invalidate_cache` takes `reparse: bool = False`. When true it drops `_PARSED_FILES` as well, so the
next read comes off disk.

**Exactly one caller passes it**: `reindex_notes`, which is the only function in the tree that pairs
`load_notes` against `note_file_fingerprints`. It already busts the other caches "deliberately", for
this same consistency reason and in the same line; the bust simply did not reach the cache that
mattered. It is also the caller that can afford the full re-parse — it is about to re-embed the
corpus.

**A note write still pays nothing.** `kg/git_writer.py` calls `invalidate_cache()` on every write and
the agent has been the writer since `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`, so the
cost `D-2026-09-06` measured and removed — 2,969 ms of `load_notes` at 20,000 notes — is the cost
this must not reintroduce. It does not: the default is `False`, and that direction is asserted rather
than described.

The two docstrings that state the now-conditional invariant say so, including which claim stopped
being true and when.

## Consequences

- One job pays a full corpus re-parse per run. It already reads and hashes every file in the same
  pass, and re-embeds what changed, so the parse is the smaller half of what it was going to do.
- The general rule this leaves behind: **a cache keyed on a stat and a signal derived from content
  are not interchangeable, and pairing them is a defect even when each is individually correct.**
  Anything else that comes to diff a content hash against `load_notes` owes `reparse=True`.

## What keeps it true

- `tests/test_graph.py::test_the_parse_cache_and_the_content_fingerprint_disagree_and_reparse_is_what_settles_it`
  drives a same-size, same-mtime edit through both busts and asserts both directions — that the
  plain bust still serves the cached parse (D-2026-09-06's win) and that `reparse=True` does not.
  It also asserts the fingerprint still moves, so the test retires itself if that ever becomes a
  stat pair again.
- `tests/test_graph.py::test_the_reindex_job_asks_for_the_reparse_its_own_comparison_needs` holds
  the call site, off the **AST** rather than the source text. Its first version grepped for
  `reparse=True` and passed with the call reverted, because the docstring beside that call contains
  the same string — a control satisfied by the prose describing it.
- `tests/test_graph.py::test_one_note_changed_re_reads_one_file_and_not_the_corpus` is unchanged and
  is what fails if the default is ever flipped.
