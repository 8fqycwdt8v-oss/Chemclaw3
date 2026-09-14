# D-2026-09-13-the-lock-is-not-the-bound-the-commit-is — a note write is bounded by one commit-and-push, not by the advisory lock in front of it

A queue row read *knowledge writes serialise cluster-wide on one advisory lock*, with a measured
ceiling near 14,000 writes/hour for the whole fleet and the note that it does not improve by adding
pods. The measurement below says the lock is not what that ceiling is made of, which changes what
the fix would have to be.

## What was measured

A real bare remote, a real Postgres, the shipped `GitNoteWriter`, medians over 9 writes at each
corpus size:

| corpus | git body (fetch, commit, push) | advisory lock | lock share |
|---|---|---|---|
| 100 notes | 141.7 ms | 15.01 ms | 10.6% |
| 1,000 notes | 160.4 ms | 14.56 ms | 9.1% |
| 10,000 notes | 298.8 ms | 14.39 ms | 4.8% |

The lock is **flat and small**: it does not grow with the corpus, and at the size where the ceiling
actually bites it is under a twentieth of the write. Removing it would not raise throughput by any
amount worth a design change, and it would not remove the serialisation either — two pods cannot
fast-forward one branch concurrently, so what the lock buys is a *queue* instead of a push race that
`_replay_our_unpushed_commits` resolves on the next write, which is the same work plus a retry.

What the ceiling is made of is **one commit and one push per note**. Measured at a 10,000-note
corpus, same harness, medians over 7 rounds, reported per note:

| notes per commit | per note | fleet-wide ceiling |
|---|---|---|
| 1 | 327.3 ms | ~11,000 notes/hour |
| 10 | 31.6 ms | ~114,000 notes/hour |
| 50 | 8.5 ms | ~421,000 notes/hour |

Batching is a **10x at ten and a 38x at fifty**, on a path where the lock is worth 5%.

## The decision

**Nothing is done to the lock, and the reason is recorded so nobody optimises it.** The row's own
framing points at the cheapest 5% of the problem; a session reading it without the numbers would
naturally reach for the lock first.

**Batching is the lever and it is not taken now**, for a reason that is about the product rather
than the cost. `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` made knowledge land "the
moment it is learned", readable beside its own citations; a batch is a queue, and a queued note is
one a chemist cannot read yet. Trading that for headroom nobody needs at 11,000 notes/hour is the
wrong trade today. `DEFERRED.md` holds it with a trigger that is a *rate* rather than a feeling.

**What is closed is the correctness half the row named in passing.** `_cluster_lock` is taken only
under `session_store == "postgres"`, and its own docstring says the uncovered case is "several
writer pods with a memory session store". A process cannot count its own replicas; a **chart** can,
and a chart render is multi-process by construction — this one renders a front door and a background
worker as separate pods, each with its own `emptyDir` clone, so the host-local `flock` beneath the
advisory lock excludes nothing between them. `templates/config.yaml` now refuses to render a release
whose `CHEMCLAW_SESSION_STORE` is not `postgres`, alongside the egress, retention and Temporal
namespace postures.

**Refused rather than warned, which is the opposite of the answer next door**, and the difference is
whose deployments break. `values.yaml` already states `postgres`, so no existing release changes;
`framingEnvelopeSecret` is *unset* in the shipped chart, which is why
`D-2026-08-27-a-warning-is-the-shape-a-guard-takes-when-raising-would-break-a-deployment` made that
one a warning. Both of those `values.yaml` comments pointed at a `BACKLOG.md` row for "should this
be refused at startup" that does not exist; they point here instead.

## What keeps it true

- `tests/test_deploy_chart.py::test_a_release_on_the_memory_session_store_refuses_to_render` — both
  arms against the real `helm template`, because a gate nobody has watched refuse is a claim that a
  gate exists, and because the positive arm is what shows the refusal is safe for the shipped
  defaults.
