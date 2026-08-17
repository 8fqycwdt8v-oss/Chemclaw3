# Verification — `ingest/documents/` design findings (lens: does it actually reproduce?)

Scope: the two findings marked **high**. The remaining seven (medium/low) are out of scope and
were not verified. All reproduction written from scratch under `/tmp/verif/`; the reporter's
`/tmp/cw/` scripts were never read or run. Working tree untouched (no source file mutated at any
point — both mutation experiments were run as out-of-tree pytest plugins on `PYTHONPATH`).

---

## `ExternalVectorDocumentIndex.prune_stale` is a copy of the base's, and the hook that deletes it already exists

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

**1. Checked every cited symbol and line.** All real and current at `HEAD` = `e48441d0`:

```
$ sed -n '873p;710p;814p;819p' src/chemclaw/ingest/documents/index.py
    async def prune_stale(self, source: str, before: datetime) -> int:
                    f"{self._drop_unclaimed} RETURNING c.doc_id, c.chunking_key, c.ordinal",
        await self._forget_vectors([(r[0], r[1], r[2]) for r in superseded])
$ sed -n '202p' src/chemclaw/ingest/documents/external_index.py
    async def prune_stale(self, source: str, before: datetime) -> int:
```

The duplication itself is exactly as described: `index.py:885` is
`f"DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL}"` and `external_index.py:225` is the same
f-string over the same imported `CLAIMED_SQL` plus `RETURNING c.doc_id, c.chunking_key, c.ordinal`.
Same `DELETE FROM document_files`, same `rowcount`, same commit, same return. The `_forget_vectors`
hook exists on the base (`index.py:710`, empty body) and is called by `upsert` (`:819`) and by
nothing else. That half of the finding is accurate.

**2. Re-tested the finding's own evidence for *why* it matters — and it is false.** The finding
argues the copy is dangerous because "the second copy has no test of its own (`prune_stale` appears
in the suite only against `PostgresDocumentIndex`, `tests/test_document_share.py:1654`)". The index
under test at that line is built 72 lines earlier:

```
$ grep -n "ExternalVectorDocumentIndex(store" tests/test_document_share.py
1582:        index = ExternalVectorDocumentIndex(store, collection="chunks")
```

It is `test_the_external_store_backend_carries_the_chunking_through_every_write`, it runs against
live Postgres and a real `VectorStore`, and line 1654 is a call to the *overridden* method. So the
one citation offered for "untested" is the override's test.

**3. Mutation-tested that coverage** — deleted the override at collection time (`/tmp/verif/mutplug.py`
rebinds `ExternalVectorDocumentIndex.prune_stale = PostgresDocumentIndex.prune_stale`), i.e. exactly
the deletion the finding proposes, without the compensating change to the base:

```
$ PYTHONPATH=/tmp/verif uv run pytest tests/test_document_share.py tests/test_vector_store.py -q -p mutplug
MUTATION APPLIED: external prune_stale -> base prune_stale
>       assert await store.search("chunks", coarse_vector, 10) == [], "and its point went with it"
E       AssertionError: and its point went with it
E       assert [VectorMatch(id='doc-1@4000:400#0', score=1.0)] == []
1 failed, 96 passed in 3.16s
```

The suite catches it, at the assertion the finding cited as proof of absence. The inverse is the
true gap and the finding does not mention it: `grep -rn "prune_stale" tests/` returns exactly one
call site, so it is `PostgresDocumentIndex.prune_stale` — the *base* — that has no direct test.

**4. Applied the finding's actual merge out-of-tree** (`/tmp/verif/mergeplug.py`: base gains
`RETURNING` + `fetchall` + `await self._forget_vectors(...)` after the commit, `del
ExternalVectorDocumentIndex.prune_stale`):

```
$ PYTHONPATH=/tmp/verif uv run pytest tests/test_document_share.py tests/test_vector_store.py tests/test_sync_share_cli.py -q -p mergeplug
MERGE APPLIED
114 passed in 3.57s
```

So the proposed fix does work.

**5. Measured the one thing "Behaviour-preserving … changes nothing for the pgvector deployment"
glosses over.** The base's orphan `DELETE` is unscoped across every source; adding `RETURNING` makes
the pgvector deployment materialise every orphan row in Python only for `_forget_vectors` to discard
it. Against live Postgres, 200k orphan chunk rows (`/tmp/verif/cost.py`, best of 3):

```
plain DELETE                      : 0.109s  (200000 orphan rows)
DELETE ... RETURNING + fetchall   : 0.321s  (200000 orphan rows)
```

~3x on the sweep statement, plus 200k tuples of transient memory. Small in absolute terms, and only
on a full re-chunk — but it is not "nothing", and `upsert`'s precedent is not equivalent because
`upsert`'s `RETURNING` is scoped to `%(docs)s` while `prune_stale`'s is not.

### Why

The mechanism is real — two spellings of one orphan rule, 33 lines apart, with an unused hook
sitting next to them — and the merge is viable. What does not hold is the argument that makes it
**high**. The finding's severity rests on "the second copy has no test of its own", and that
citation is simply wrong: the line it points at is a live-Postgres assertion on the override that
fails the moment the override is removed. With the invariant pinned by a sharp test on the exact
subclass, this reduces to a behaviour-neutral DRY cleanup with a measurable (if minor) cost on the
default deployment — a low, not a high. Acting on it is defensible; treating it as a divergence
waiting to happen is not, because the suite already stops that divergence.

---

## A bounded crawl pass re-walks everything behind the cursor, so a drain is O(passes × share)

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. Re-derived the mechanism from source.** `_Walk.descend` (`crawl.py:186`) recurses into every
directory at `:207-210` and only tests the cursor at `:211-213`, on files. There is no directory-level
cursor test anywhere in the module. So a resumed pass repeats every `scandir`, every `sorted(...)`,
every `PurePosixPath(...).relative_to(...)` and every `_is_excluded` fnmatch over the whole
behind-cursor region; only `entry.stat()` and `_accept`'s bookkeeping are saved.

**2. Confirmed the drain really is multi-pass, per activity.** `durable/document_sync.py:171-172`
calls `sync_share(..., after=after, limit=settings.document_sync_batch_size)`, the workflow loops on
`chunk.cursor` (`:268-278`), and `sync.py:282` calls `crawl_share` fresh each time.
`document_sync_batch_size` defaults to **500** (`core/config/sources.py:72`), so a 500k-candidate
share is ~1000 passes, each starting from the top of the share.

**3. Measured it myself** (`/tmp/verif/mkrepro.py`, 200 dirs × 100 files = 20,000 on `/dev/shm`):

```
single unbounded pass: 20000 files in 0.285s (has_more=False)
drain: 20 passes, 20000 files, 1.923s  -> 6.7x one pass
final pass (after='dir0179/file0099.txt'): returns 1000 of 20000 files in 0.155s = 54% of a full walk
```

My numbers land on top of the reporter's (0.283s / 1.671s / 0.152s / 54%). The load-bearing one is
the last: **the final pass returns 5% of the corpus and pays 54% of a complete walk.** That is the
per-pass floor, and it is what makes the drain linear in passes.

**4. Reproduced the `limit` sub-claim properly.** The reporter's phrasing needs the unsupported files
to sort *before* the first candidate — my first attempt returned 0 because the walk stops at the
limit before reaching them. Arranged correctly (`/tmp/verif/fix.py`):

```
limit=1, 20000 unsupported .doc sorted before the one candidate: accepted=1 unsupported examined=20000 in 0.192s
```

True, though weak on its own: examining them is how you learn they are unsupported, and the
extension test precedes the `stat`.

**5. Implemented the proposed fix independently and tried to break it.** I wrote my own patched
`descend` (not the reporter's), on a tree with a nested `sub/` level so the skip has to work at
depth:

```
baseline : 20 passes, 20000 files, 1.928s
with-skip: 20 passes, 20000 files, 0.353s  (5.5x)
identical output: True
```

I then attacked the fix's "behaviour-preserving" claim on the one axis the reporter did not consider:
`failed_roots` is a hard prune-safety gate (`sync.py:461-476`, "roots that could not be walked …
nothing is pruned"), and today's re-walk incidentally re-verifies the behind-cursor region on every
pass. `/tmp/verif/residual.py` removes a consumed subtree mid-drain and compares:

```
baseline : pass2 failed_roots=[] -> prune would be allowed
with-skip: pass2 failed_roots=[] -> prune would be allowed
```

No difference — a vanished directory is simply absent from the parent's listing, not an error. The
EACCES form is not simulable here (`id -u` = 0; `scandir` on a `0o000` directory succeeds as root),
so I record that as untested rather than as a defect. My attempt to break the fix failed.

### Why

Everything the finding claims reproduces on my own scaffolding, at my own numbers, on the code path
the durable workflow actually drives. The consequence is cost, not wrongness — but it is cost in the
one module whose entire stated purpose is the cost model of a TB share, on the first drain, which is
precisely the case the design was written for: with the shipped batch size of 500, indexing 500k
candidates costs ~1000 walks of the share, each ≥54% of a full one, and on CIFS each of those walks
is one network round trip per directory. High is right. Two things the reporter left on the table
that make it slightly worse: the batch size default is 500 rather than the 1000 the finding
assumed (double the pass count), and the workflow's `continue_as_new` carries the cursor forward, so
the re-walk is paid across workflow runs too and never amortised anywhere.
