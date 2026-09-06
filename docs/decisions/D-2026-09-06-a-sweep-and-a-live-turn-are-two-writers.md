# D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers — the checkpoint sweep re-asks its question, and a checkpoint says what it was holding

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-08-10 §3 (turn state lives in the checkpointer, durability lives in Temporal), D-011 (a
persisted result is never recomputed), D-2026-08-25-a-cache-is-not-a-record (`calculation_results`
is a cache and refuses a predicate on its payload),
D-2026-08-16-the-physics-leaves-the-cache-stays (the calculators' schemas are not in this
repository), D-2026-09-06-a-decode-the-workflow-does-not-do-is-a-failure-nobody-hears (the
read-tolerates / write-forbids asymmetry, taken here on a second wire) ·
**Corrects** the `_prune_checkpoints` docstring, which was wrong twice about the same race.

## Context

A hostile review drove the real retention sweep against the real `SchemaStampedSaver` and a real
compiled graph, statement by statement on two connections, and found the failure
`agent/checkpointer.py` says at length that it refuses — reached by a route its guard cannot see.

## What was measured

`_prune_checkpoints` deleted an expired thread from `checkpoints`, `checkpoint_blobs` and
`checkpoint_writes` in one transaction. The checkpointer's pool is `autocommit=True` **on purpose**
("every checkpointer write is its own transaction, which is what a checkpoint already is"), so a
live turn commits its rows the instant it writes them, on a connection the sweep knows nothing
about. A turn landing between the first and second DELETE:

```
before: {'log': ['step0', 'step1', 'step2']}
turn during sweep -> {'log': ['step0', 'step1', 'step2', 'step3']}
after sweep: checkpoints=3 blobs=0
resumed state: {'log': []}
next turn -> {'log': ['step0']}
```

No exception, no log line, no counter — a brand-new conversation presented to the chemist as a
continuation of the old one, which is verbatim what that module's docstring calls "worse here than
no answer". The docstring of the function that produced it was **wrong twice** in one sentence: one
transaction protects the sweep from *itself* and says nothing about the other writer, and the state
it names does not "raise when read". Reversing the delete order was measured and loses the
conversation just the same; it only changes which rows are left behind.

## Decision

**Both of the sweep's deletes re-ask their question inside its own transaction.**
`_DELETE_EXPIRED_CHECKPOINTS` re-runs the expiry predicate as part of the `DELETE`, restricted to
the candidate ids so it stays an index probe per thread, and drives the other two statements from
its `RETURNING thread_id`. `_DELETE_ORPHANED` takes a thread's blobs and writes only while that
thread has **no** `checkpoints` row at all, which is what catches the measured interleaving: the
racing turn's row is committed and visible to that statement's snapshot. Measured after, the same
interleaving leaves `checkpoints=3 blobs=12`, the thread resumes with `['step0', 'step1', 'step2',
'step3']` and the next turn continues it. A thread revived mid-sweep loses the *older* checkpoints
already deleted — a shorter `aget_state_history`, not a state that reads back empty.

**What is left open is stated rather than papered over.** A turn whose blobs commit before
`_DELETE_ORPHANED` takes its snapshot and whose `checkpoints` row commits after it still loses them:
`aput` writes blobs first and the checkpoint row second, and nothing synchronises the two parties
without a lock on the turn-serving write path. That is why the guard that matters is on the read.

**Every checkpoint records the channels it was written holding, and a checkpoint that cannot load
one of them is refused.** `CHECKPOINT_VALUES_KEY` is stamped by `aput` before
`AsyncPostgresSaver` splits those values between the inline column and `checkpoint_blobs`;
`_refuse_if_values_are_missing` compares it against what loaded and raises `CheckpointValuesMissing`
naming the session, the channels and the remedy. It catches every route into this state — the race,
a restore, a partial `session_fork` copy, hand surgery — not only the one that was found.

**The signal the review proposed is wrong, and finding that out is the reason this is a stamp.** The
obvious comparison is `channel_versions` against what loaded, and the review confirmed it exact on
the corrupted row. Measured on a **healthy** three-turn thread it flags *every* checkpoint:
`__start__` and `branch:to:*` are consumed by the step that reads them, which bumps the version and
writes no blob, so a legitimate checkpoint routinely names channels holding no value. A guard built
on it would have refused every thread in the fleet on the deploy that introduced it — the guard
causing the exact harm it exists to prevent, for the second time in this module's history.
`test_the_value_stamp_does_not_refuse_a_healthy_thread` asserts both halves, so a future rewrite onto
the naive signal goes red. An unstamped checkpoint resumes, for the reason `STATE_CHANNELS_KEY`'s
unstamped case does.

**A non-mapping `session_messages.message` is unreadable, not a crash.** `stored_call_ids` is typed
`Mapping`, which satisfies mypy and decides nothing at runtime; on a scalar it raised
`AttributeError` two lines *before* `_prune_session_messages`'s per-session `unreadable_rows` skip
written for exactly that row, so one bad row took the whole `session_messages` pass down — every
session stopped being pruned and Temporal retried the activity to exhaustion. That is the failure
the comment above that call site records as already fixed once, through a second door.
`session_store.message_from_row` guards the same column the same way one module over; the defect was
that the two readers of one column disagreed and the one that disagreed was the one that deletes.
(`"contents" in payload` is a *substring* test on a string, so a row whose text contains the word
took the MAF branch and raised there instead.)

**A cache row is checked for the one shape this repository is entitled to know.**
`calculation_results.result` is `JSONB NOT NULL` and nothing else, so `{"energy": "not a number"}`
answered `hit=True computes=0` and flowed out as the tool's answer — permanently, because D-011
never recomputes and that table is never pruned — while a jsonb array, string, number or `null`
reached `json.loads` as a `list`/`int` and produced `TypeError: the JSON object must be str, bytes
or bytearray, not list`, one of which took `find`'s whole browse down. `checked_payload` enforces
what `ResultPayload` already declares: a **non-empty JSON object**. That is not one calculator's
schema — those live in `Chemclaw3-mcp` and are deliberately not here — it is the store's own type,
and enforcing a declared type is not the payload predicate the query model refuses. Emptiness is the
half that can be acted on: `{}` is what a truncated or failed call to the calculation server
degrades into, and `remote_compute` returns `dict[str, Any]` persisted unvalidated, so the sibling
fleet could poison a key forever by returning `{}` once. The gate is in `cached_compute`, the one
lookup-before-compute path every calculator goes through. A wrong *value* under a right key stays
undetectable here and is stated rather than fixed. `get` refuses by name (the caller asked for that
row); `find` drops and logs (a browse must not be emptied by a row nobody asked for), which is the
call `retrievers._chunks_from_hits` already makes.

**`reaction_records.conditions` tolerates on read what it forbids on write.** A row carrying one
added field raised `ValidationError: pressure_bar_v2 — Extra inputs are not permitted` on every pod
older than the one that wrote it — and a reaction is looked up by *structure*, so that landed on a
chemist's query for a molecule rather than on the ingest that caused it. `_stored_conditions` keeps
unknown keys out of the frozen model and logs them; `ProcessConditions` stays `extra="forbid"` where
`ingest/eln/record.py` builds it from ORD data inside this image. Same asymmetry, same argument, same
week as the Temporal wire. `{}` now reads as "recorded, all unknown" rather than as "not recorded",
which `comparison.MISSING` renders differently; a payload that is not an object at all is
`UnreadableConditions` naming the row.

## Consequences

- A retention pass may now leave a thread it selected, and says so in its log line. That is the
  self-correcting direction: it goes on the next pass.
- `CheckpointValuesMissing` is a new failure a turn can take. It is non-retryable in effect — the
  rows are gone — and it replaces a turn that answered confidently out of nothing.
- Every checkpoint's metadata grows by one sorted list of channel names.
- A `calculation_results` row that is empty or not an object now fails the calculation that
  addresses it. There is no migration: such a row cannot be produced by this store's own `put`, and
  an operator deletes it and lets the next call recompute.
- No metric distinguishes checkpoint value loss from other turn failures. `logger.error` matches the
  refusal beside it, and `degraded` was declined because it records a deliberate swallow and this
  call site continues with nothing.
