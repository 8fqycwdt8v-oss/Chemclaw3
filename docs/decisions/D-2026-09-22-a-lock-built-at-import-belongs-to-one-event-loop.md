# D-2026-09-22-a-lock-built-at-import-belongs-to-one-event-loop — per-loop locks, and the pruning a weak map cannot do

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Found as the third blocker in
`D-2026-09-22-a-mutation-backstop-that-cannot-start`, which records the hunt; this is the decision
about what replaces the two locks it found.

## Context

`kg/git_writer._WRITE_LOCK` and `core/temporal_client._CONNECT_LOCK` were module-level
`asyncio.Lock()` objects. Both comments said one lock per process is right because one event loop
per process is the deployment shape, and `temporal_client`'s went further and priced the multi-loop
case: two loops "would each see `_CLIENT is None` and each connect", at a cost of "a second channel
rather than a fault".

That price is wrong, and the reason it looked right is the interesting part. **`Lock.acquire`
resolves its event loop lazily and only on the contended path** — the fast path sets `_locked` and
returns without ever calling `_get_loop()`. Measured:

```
uncontended, two sequential asyncio.run calls : no error at all
contended,   two sequential asyncio.run calls : RuntimeError: <Lock [locked]> is bound to a
                                                different event loop
```

So a module-level lock binds itself to the first loop that *races* on it, which is the first time it
does its job. Every use before that is silent. And the failure is not the error it sounds like: the
`RuntimeError` is raised by the **waiter**, while the holder keeps the lock, so `asyncio.run`'s
shutdown tries to cancel a holder whose cancellation never completes. What a caller sees is a **hang
with no exception**, and the lock is left permanently `[locked]` for everyone after it in that
process.

Driven on the real writer: one `asyncio.run` of two concurrent `GitNoteWriter.write` calls passes in
3.4 s, and the identical second call in the same process never returns — killed at pytest-timeout's
180 s, and again at 720 s under `PYTEST_TIMEOUT_SCALE=4`, so it is a hang and not a slow test. That
is what stops `make mutants` from completing, because `mutmut` runs the suite through `pytest.main()`
once for stats, once for the clean baseline and once per mutant, all in one process.

**One loop per process is still the deployment shape, and that was never the whole population.** The
API serves on one loop, a Temporal worker runs one, a CLI command is one `asyncio.run`. A test
harness that reuses its process is many, and so is any command that calls `asyncio.run` twice. The
cost of being wrong there is not a slow path; it is a wedge.

## Decision

**A lock is resolved per running event loop, by `core/aio.LoopLocalLock`, and the weakening that
buys is stated at each declaration.**

Two loops in one process no longer serialize against each other. That is a real loss and it is the
right trade, because it is reachable only from two *simultaneous* loops in separate threads, where
each caller is already covered:

- `git_writer` holds an exclusive OS-level `flock` on the checkout, non-blocking, so a genuine
  second writer is refused fast rather than interleaved. The `asyncio.Lock` was the in-process
  courtesy on top of it.
- `temporal_client`'s own comment already argued this exact case and reached this exact conclusion —
  a second channel is a cost, not a fault. Now the code does what the comment said.

Sequential loops, which is the case that actually occurs, get precisely the old semantics. A
permanent wedge is not a stronger guarantee than that.

**Two callers, so it is a shared class rather than the same fix written twice** — the second real
caller is what this repository's Rule of Three asks for, and the argument is identical at both
sites.

## Consequences

**A `WeakKeyDictionary` keyed by the loop is the obvious implementation and it cannot work here.**
The value is an `asyncio.Lock`, and a *contended* `asyncio.Lock` stores `_loop` — its own key — so
the entry keeps itself alive. Measured over three `asyncio.run` calls with a collection in between:

| lock is | entries surviving in a `WeakKeyDictionary` |
|---|---|
| never contended | **0 of 3** |
| contended | **3 of 3**, with `lock._loop is` its own key |

The weak mapping would have released exactly the entries that do not matter and retained exactly
the ones that do. So `LoopLocalLock` holds a plain dict and **discards closed loops when it is
read**: a closed loop's lock can never be used again, the map holds one entry in the shape that
occurs, and the cost is an `is_closed()` per entry on a path that is already taking a lock. Both
arms of that measurement are in `tests/test_loop_local_locks.py`, because a one-sided test of the
uncontended case would have read as proof that the weak map works.

**What holds it is a rule over the tree, not a list of two modules.**
`test_no_asyncio_primitive_is_built_at_import_time_in_src` walks every module in `src/` for an
`asyncio.Lock`/`Event`/`Condition`/`Semaphore`/`Queue` constructed at import — descending class
bodies too, since a class attribute is constructed at import and is the next spelling somebody
reaches for. The two that had one were the two somebody happened to write; the guard's value is
entirely in the third.

**The regression is pinned where it bit**, not only where the mechanism lives:
`tests/test_knowledge.py::test_two_sequential_event_loops_can_both_write_concurrently` drives two
real loops through `GitNoteWriter` and times out without the fix. It is written as "the second one
returns", because the failure is a hang and there is no assertion to make about a call that never
comes back. It is in `[tool.mutmut]`'s test selection, so the mutation run exercises it.

**Revisit when:** a second loop in one process ever needs to serialize against the first — a thread
running its own `asyncio.run` beside the serving loop, in either of these two call sites. At that
point the answer is a `threading.Lock` around the critical section rather than a per-loop
`asyncio.Lock`, and the trade changes because blocking the event loop starts to cost something real.
The file that would show it had fired is `tests/test_loop_local_locks.py`, whose serialization test
asserts the property only *within* a loop and would have to grow a cross-loop arm.
