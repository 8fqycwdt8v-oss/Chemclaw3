# D-2026-09-13-an-interleaving-whose-mechanism-is-absent-is-not-a-residual — the row goes, and what was actually unheld gets an assertion

A `BACKLOG.md` row has carried, as a medium-priority open item, the claim that "the checkpoint sweep
and a live turn are two writers, and only the read side notices": that `aput` "writes blobs first and
the `checkpoints` row second, so a turn whose blobs land before `_DELETE_ORPHANED`'s snapshot and
whose row lands after it loses them", and that closing it needs "a lock on the turn-serving write
path".

Both halves of the premise are false, and the code said so before this row was last read.

## What was measured

`AsyncPostgresSaver.aput` runs its blob `executemany` and its `checkpoints` upsert inside
`self._cursor(pipeline=True)`, which opens `conn.pipeline()` — and a psycopg pipeline on an autocommit
connection is **one** transaction. Measured end to end rather than read: driving `aput` with two
channels against a real database and reading `xmin` — the transaction that inserted each row — off
what it wrote:

| rows | distinct `xmin` |
| --- | --- |
| 2 × `checkpoint_blobs`, 1 × `checkpoints` | **1** |

So the two writes are not separately visible and the named interleaving has no mechanism.

**The code had already retracted it.** `durable/retention.py` carries the retraction in two comments
— *"This paragraph used to name a residual that does not exist, and it was the written justification
for the read guard"* — and `agent/checkpointer.py`'s header carries the third. What survived in the
queue was the sentence those retractions were written against.

## The decision

**Delete the row.** It names a mechanism that is absent, and a queue row is a claim about the code
(`BACKLOG.md`'s own rule). Keeping it as "investigated and retracted" is the appending habit D-154
removed from `DEFERRED.md`; the ADR is the record and `git log` is the history.

**This is not a claim that no interleaving exists**, and the row's remedy is not being declined on the
merits. `agent/checkpointer._refuse_if_values_are_missing` stays, and `retention.py` already states
its real justification: a torn thread has producers this sweep is not one of and cannot be — a
point-in-time restore, hand surgery on the tables, a future upstream change that stops pipelining. A
guard on the read is right because it is the last place that can tell. What is settled is only that
`aput` is not one of those producers.

## What was genuinely unheld

`tests/test_upstream_surface.py::test_a_pipeline_block_on_an_autocommit_connection_is_still_one_transaction`
already pins the psycopg half, with a control arm. It is about **psycopg**: that an autocommit
connection inside `conn.pipeline()` does not commit per statement. Upstream could keep that true and
*stop using it* — `pipeline=True` in `aput` is a keyword argument in a file this repository does not
own, and dropping it would falsify three comments in `durable/retention.py` and the header of
`agent/checkpointer.py` with nothing going red.

`test_aput_still_writes_its_blobs_and_its_checkpoint_row_in_one_transaction` is that assertion. It
measures `xmin` over the rows `aput` actually wrote, so it is end-to-end rather than a source-shape
check and fails for any reason `aput` stops being atomic, not only for a changed keyword. Two channels
deliberately: one blob row and one checkpoint row would pass if upstream committed each *table*
separately, and a blob commit separated from a checkpoint commit is precisely the interleaving the
retracted residual named.

## What keeps it true

| property | test |
| --- | --- |
| `aput`'s blob rows and its `checkpoints` row carry one `xmin` — one transaction — with at least two blob rows so a per-table commit cannot pass | `tests/test_upstream_surface.py::test_aput_still_writes_its_blobs_and_its_checkpoint_row_in_one_transaction` |
| a psycopg pipeline on an autocommit connection is still one transaction, and the control arm shows autocommit outside one is not | `tests/test_upstream_surface.py::test_a_pipeline_block_on_an_autocommit_connection_is_still_one_transaction` |
| the sweep still survives a turn landing between its statements, whichever order the deleter takes | `tests/test_checkpoint_delete_order.py` |
| a thread whose channel values are missing is still refused on the read | `agent/checkpointer._refuse_if_values_are_missing` and its own tests |

One mutation, on the installed distribution and restored from a `.bak`: changing `aput`'s
`self._cursor(pipeline=True)` to `pipeline=False` produced **2** transactions
(`['200452', '200453']`) and the named assertion. That is the exact upstream change the new test
exists to catch, and it was run rather than reasoned about.
