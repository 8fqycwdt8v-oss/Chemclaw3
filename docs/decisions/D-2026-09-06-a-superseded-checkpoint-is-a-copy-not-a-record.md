# D-2026-09-06-a-superseded-checkpoint-is-a-copy-not-a-record — a live thread is bounded by its writer, not by a retention clock

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-08-10 §3 (turn state lives in the checkpointer),
D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has (compaction is non-destructive),
D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers (the sweep and the saver are two writers, and
the residual window that argument left open),
D-2026-08-29 (`session_fork` copies a thread whole rather than its tip),
D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit ·
**Corrects** `durable/retention.py`'s claim that in-thread pruning is impossible, and the
per-thread depth `_EXPIRED_THREADS`' benchmark was taken at.

## Context

Six review waves have found and left open the same defect: `checkpoint_blobs` grows with the
**square** of a thread's turn count. Every wave stopped at the same place, and it was not a hard
problem — it was a sentence. `src/chemclaw/durable/retention.py`, in the docstring that decides
what this system is allowed to delete:

> Pruned by **thread**, not by row. A checkpoint chains to the one before it through
> `parent_checkpoint_id`, so deleting the old rows inside a live thread would leave the survivors
> pointing at nothing

That is a claim about behaviour, and it was never run.

## What was measured

**The growth.** One thread, one tool call a turn, driven through the real compiled agent against the
real `SchemaStampedSaver`:

| turns | `checkpoints` | `checkpoint_blobs` |
|---|---|---|
| 20 | 260 | 2.57 MB |
| 40 | 520 | 10.29 MB |
| 80 | 1,040 | 41.17 MB |

Ratio 4.00 for a 2x thread, twice — exponent 2.00. A turn with one tool call writes **13**
`checkpoints` rows and four full copies of the whole message list, so the total is `4c·Σk`. A
40-turn thread carrying 139.6 kB of conversation stored ~15 MB, 109x.

**The sentence.** Keeping the newest three checkpoints per `(thread_id, checkpoint_ns)` and every
blob version they reference, then resuming on a fresh saver: 520 → **15** `checkpoints`,
10,288,879 → **757,059** bytes of `checkpoint_blobs` (−92.6%), and the thread resumed with **all
164 messages**, identical to the unpruned arm's, with no `CheckpointValuesMissing`. At 80 turns:
41.17 → 1.53 MB, −96.3%. Post-prune the series is 0.371 / 0.757 / 1.529 MB — ratio 2.02, i.e.
**linear**.

What a dangling `parent_checkpoint_id` actually costs is `aget_state_history` depth and time-travel.
`grep -rn aget_state_history src/` finds no caller; the only `aget_tuple` in `src/`
(`agent/plan_state.py:99`) passes `thread_id` alone; `alist` has no first-party caller;
`agent/session_fork.py` copies whatever rows exist rather than walking the chain. Nothing in this
repository reads a superseded checkpoint.

**Latency is not the cost, and saying otherwise would have sent this the wrong way.** A 100-turn
thread's machinery went 112 → 175 ms, ~2% of a real 8.3 s turn. The harm is bytes and the retention
sweep's depth.

**The namespace caveat, and the direction it fails in.** A `task` call writes a `tools:<uuid>`
namespace on the caller's own `thread_id` — measured, one **new** namespace per call, 7
`checkpoints` and 3 `checkpoint_blobs` each. The review that asked for the partition expected
*over*-pruning. Measured with the `PARTITION BY` removed and nothing else changed, on two threads
driven identically: the root namespace went 52 → 3 in both arms, while every helper namespace went
7 → 3 partitioned and **stayed at 7** unpartitioned. The failure is a leak that grows with helper
use, not a loss, because `oldest_kept` groups by namespace and a namespace outside the global top-K
gets no floor at all. Over-pruning stays possible only where a helper's own checkpoints are the
newest on the thread. One `PARTITION BY` closes both.

**The sweep's own benchmark was taken at a depth no shipped thread has.** `_EXPIRED_THREADS` is
documented at "200 000 threads x 3 checkpoints … 2.5 ms". Re-measured this session (load average
1.05–1.43, so ratios rather than milliseconds): 20,000 x 3 all expired **2.4 ms**; 20,000 x 13
**6.8 ms**; 2,000 x 520 — one 40-turn session's depth — **281.8 ms**, 117x the documented figure for
the identical capped statement, because the `LIMIT` caps *threads* while the streaming group-by
reads all of their rows. Sparse (2% expired, the steady state): 20,000 x 13 goes 6.8 → **248.9 ms**,
and 2,000 x 520 spends **1,181.7 ms scanning 1.04 M rows to retire 40 threads**.

## Decision

**A superseded checkpoint is a copy, not a record, and is deleted by the writer.**
`agent/checkpointer._PRUNE_SUPERSEDED` keeps `checkpoint_retain_per_thread` (default **3**)
checkpoints per `(thread_id, checkpoint_ns)` and deletes below the per-channel **version floor**.
It runs once a turn, from `aput`, on the root namespace's `source == "input"` checkpoint.

Four things make that the shape rather than a `_prune_within_threads` pass in `durable/retention.py`,
which is what the review proposed:

1. **It is not disposal, so it must not be gated on a disposal policy.** `retention_enabled` is
   `False` by default and a window is a decision a deployment states. Nothing a chemist, the model,
   `session_fork` or `plan_state` reads changes when a superseded copy goes, so there is no policy
   to state — and behind `retention_enabled` the default deployment would have gone on paying the
   quadratic, which is every deployment this repository ships.
2. **A live thread has to be bounded by its writer.** A daily sweep bounds a thread that has
   stopped. That is exactly the case the six waves kept finding.
3. **A version floor is race-safe where a `NOT IN` set is not.** Channel versions are zero-padded
   monotone counters, so a row written after the statement's snapshot sorts above every floor it
   computed and cannot be deleted — which closes the one window
   `D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers` left open for `_DELETE_ORPHANED`. Driven
   at `keep=1` every 5 ms against eight live turns on the same thread, the thread resumed with all
   nine answers.
4. **One statement.** On this autocommit pool that is one transaction, so a concurrent reader sees
   the thread before the prune or after it, never mid-prune, and the delete-order hazard
   `checkpoint_thread_delete_statements` exists for does not arise.

**Once a turn rather than once a write.** Pruning after every `aput` bounds the thread harder (3
rows against 15) and costs 2.88 s of extra statements over 40 turns against 0.88 s, on a pool where
every statement queues behind one process-wide lock. Turn latency is unchanged either way
(p50 125.1 ms unpruned, 117.4 ms pruned — within this fixture's noise).

**A failure does not fail the turn.** The checkpoint is written and committed before the prune runs;
it is logged at WARNING and the thread keeps its copies.

## What this does not fix, stated because a claim that it did would be the failure this repository
keeps finding

- **Write volume stays quadratic.** 16.7 MB of WAL for 139.6 kB of conversation is upstream's
  `_dump_blobs` rewriting the whole `messages` channel per superstep, and no prune reaches it —
  measured, the prune *adds* ~4% (3.51 → 3.64 MB over 20 turns, reproduced twice after an earlier
  reading was found contaminated by a Postgres checkpoint's full-page writes). Only a destructive
  trim of state would, and that one contradicts
  `D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has`.
- **A single runaway turn is bounded by the loop cap, not by this.** Pruning at the turn boundary
  leaves a thread at the retained checkpoints plus one turn's writes.
- **The retention sweep's sparsity multiplier is untouched.** Bounding depth turns the steady-state
  20,000-thread pass from 248.9 ms into 20,000 x 3's 59.8 ms, but finding an expired minority still
  means visiting every thread; that needs the durable resume watermark `retention.py` already names
  as missing.

## Consequences

- `agent/checkpointer.py`: `_PRUNE_SUPERSEDED` and `SchemaStampedSaver._prune_superseded`.
- `core/config/memory.py`: `checkpoint_retain_per_thread`, default 3, `0` disables. The comment on
  `retention_checkpoints_days` no longer carries the false claim.
- `durable/retention.py`: the module docstring and the `_EXPIRED_THREADS` comment say what was
  measured instead of what was believed, and the depth their benchmark rests on is named.
- `tests/test_checkpointer_prune.py` holds the A/B, the namespace arm driven through a real `task`
  call, the concurrent-turn arm, the disabled arm and the non-fatal-failure arm. Every one of the
  first two was watched failing against the unfixed source.
- **Prose counts deleted in the same commit.** `agent/checkpointer.py` twice said `ChemclawState`
  holds six channels; measured, it holds eight (four of them langchain's). Rather than write "eight"
  for the next session to find stale, both numbers are gone and
  `tests/test_checkpointer_schema.py::test_the_declared_channels_partition_the_state` asserts the
  partition — what this repository declares plus what the base declares is exactly the state, and
  nothing is in both. Watched failing with one channel dropped from the derivation.
