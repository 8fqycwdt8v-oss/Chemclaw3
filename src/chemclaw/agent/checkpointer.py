"""The LangGraph turn-state checkpointer, on its own Postgres pool (M6, D-2026-08-10).

Where MAF gave layer 1 no durability at all — so Chemclaw hand-built it in `agent/session_store.py`
— LangGraph ships a checkpointer, and D-2026-08-10 §3 draws the line: Temporal keeps every long or
expensive job, and this takes turn state, rollback and resume. `interrupt()` needs it too; without a
checkpointer there is nowhere for a suspended turn to live.

**Its own pool, deliberately, and for three measured reasons.** `core/db.py` owns the shared pool
that the calculation cache, the vector index and the session store borrow from. `AsyncPostgresSaver`
must not join it:

1. **`setup()` cannot run there.** Three of its ten migrations are `CREATE INDEX CONCURRENTLY`,
   which Postgres refuses inside a transaction block, and `db._pool_for` builds pools without
   `autocommit`, so psycopg opens an implicit transaction on the first execute.
2. **One `asyncio.Lock` per saver serializes every checkpointer statement**, and `alist` yields
   *inside* both that lock and the borrowed connection. A paginated history read would therefore
   hold a shared-pool connection for its entire iteration, starving call sites that have nothing to
   do with conversations.
3. The saver enters **pipeline mode** on the connection it borrows, which is not something to do to
   a connection another subsystem may have opinions about.

A separate pool is also what makes the first point cheap: this one is opened with
`autocommit=True`, so `setup()` just works.

**What that does *not* mean is "every checkpointer write is its own transaction", which is what
this paragraph said and what two modules reasoned from.**
`AsyncPostgresSaver._cursor(pipeline=True)` opens `conn.pipeline()`, and a pipeline block on an
autocommit connection is **one transaction** —
measured, `txid_current()` is identical across both statements inside it, where two statements
outside one differ. So `aput`, `aput_writes` and `adelete_thread` are each atomic across every
statement they issue, and a concurrent reader watching a real `aput` sees only `(0, 0)` and
`(1, 1)`, never the blob without its checkpoint. `tests/test_upstream_surface.py` pins that, because
it is a psycopg property nobody promised and the sentence above went on being quoted after it
stopped being true.

The atomicity of one write is not the atomicity of the *pool*: each write commits the instant it
completes, on a connection no sweep is inside, which is the property
`checkpoint_thread_delete_statements` and `durable/retention.py` are both built against.

**One saver per process, pinned to its loop.** `AsyncPostgresSaver.__init__` calls
`asyncio.get_running_loop()` and keeps it, so the saver cannot outlive or precede the loop it was
built in — hence the async factory rather than a module-level instance.

**Every checkpoint records the state channels this repository declared when it was written, and a
thread that never held one this build declares is refused by name.** LangGraph restores a
checkpoint's `channel_values` into channels built from the *current* graph's state schema, and it
has no migration system: a channel the checkpoint never held simply stays empty, so a node that
indexes it raises a bare `KeyError` naming the field — from inside the node, with nothing in it
naming the thread, the schema change or a remedy.

**The direction that fails is not the intuitive one, so it was measured.** Each finding below names
the test in `tests/test_checkpointer_schema.py` that asserts it, because that test is the record
that cannot go stale — the prose restating its numbers here could, and this file is where a reader
decides whether the guard still means what it says:

- An **added** name is what raises — a channel this build declares that the checkpoint does not
  hold. A rename is an addition plus a removal, and it is the addition half that raises
  (`..._the_added_half_of_a_rename_that_raises_and_not_the_removed_half`).
- A **removed** channel is harmless: nothing declares it any more, so nothing indexes it
  (`..._a_channel_this_build_no_longer_declares_does_not_refuse_the_thread`).
- It only bites a turn resumed *inside* the graph, which is what `interrupt()` produces. At a turn
  boundary the run starts at `START` and a node indexing an unwritten channel fails identically on
  a brand-new thread, so the checkpoint contributed nothing to that one
  (`..._a_moved_channel_strands_a_turn_resumed_inside_the_graph`).
- `NotRequired` is not a filter this can use: it says how the *input* may be spelled, not how a
  node reads the channel (`..._notrequired_does_not_make_an_added_channel_safe`).

`SchemaStampedSaver` writes `FIRST_PARTY_CHANNELS` into every checkpoint's metadata, and on resume
refuses a checkpoint whose stamp is missing one of them, raising `CheckpointSchemaMismatch` naming
the thread, the missing channels and the remedy.

**Only the channels this repository declares, and that is the whole point of the exclusion.**
`ChemclawState` extends langchain's `PlanningState`, from which `messages`, `jump_to`,
`structured_response` and `todos` arrive. A stamp over *every* name
`ChemclawState.__annotations__` reports would move on any langchain minor bump that adds or renames
one of *its* channels, refusing every in-flight thread in the fleet on a dependency change nobody
associated with turn state — the guard causing the exact harm it exists to prevent.

**No count is written here, and that is deliberate.** This paragraph and `_first_party_channels`
both said "six" over a state that had grown to eight — the two halves moved when `loop_cap` and
`spend_cap` added channels, and neither sentence's author was editing this file. The set is
derivable, so `tests/test_checkpointer_schema.py::test_the_declared_channels_partition_the_state`
asserts the partition instead: what this repository declares plus what the base declares is exactly
what the state declares, with nothing in both and nothing in neither.

Middleware channels are outside it for a second reason: `create_agent` merges those in and
this module cannot see them without importing the agent builder that imports it.

**What is not caught, and where the refusal is deliberately wider than the failure.** Not caught: a
same-name *type* change (a type repr is not stable enough to hang a session's resumability on); an
upstream or middleware channel that moves; a first-party channel that is only *removed* (measured
harmless above). Wider than the failure: an added channel is refused even when every reader of it
uses `.get()` and the resume would have worked, because the stamp holds names and cannot see how a
node reads one. That over-refusal lands on a change this repository is itself deploying — which it
can drain sessions for, and which the paragraph below says it should — never on a dependency's.

**Refusing rather than silently starting the thread over**, which is the same call
`agent/plan_state.py` makes for an unreadable plan and for the same reason: the two are
indistinguishable to a chemist and not at all indistinguishable in what they authorize. A turn that
resumes with the conversation dropped answers *normally* — confidently, out of context, with no
sign anything is missing — and a confidently wrong answer about a process is worse here than no
answer. Nothing is destroyed by the refusal: the checkpoint rows stay until `durable/retention.py`
prunes them, and the transcript (`session_messages`) and the audit chain are separate stores that
the checkpointer never held (D-2026-08-10 §3). What the chemist gets is `api/runner.py`'s ordinary
turn-failure event — classified `internal` and non-retryable, which is exactly right, because
retrying cannot give a checkpoint a channel it never held — while the log carries this module's own
ERROR naming the session, the missing channels and the ones the thread does hold.

**A second stamp answers a second question: what this checkpoint was holding.** The stamp above is
about the *build*; `CHECKPOINT_VALUES_KEY` records the channels that had a value at the instant the
checkpoint was written, and `_refuse_if_values_are_missing` refuses one that cannot load them back.
The failure it catches is a thread whose `checkpoint_blobs` rows are gone while its `checkpoints`
row survives — measured coming out of `durable/retention.py`'s sweep racing a live turn, and
reachable identically from a restore, a partial `session_fork` copy or hand surgery. Before it, that
thread resumed as an *empty conversation* with no exception and no log line, which is precisely the
outcome the paragraph below calls worse than no answer. Two things this one does not do: it says
nothing about a value that is present and wrong, and it is on the resume only, not on `alist` —
history rendering shows what a session did, and a row with a hole in it is still something to show.

**An *unstamped* checkpoint is accepted, and so is a stamp this build cannot read.** Refusing those
would brick every live session at the deploy that introduces the guard — the exact outcome the
guard exists to prevent, caused by the guard. They resume as they always did, and the first write
of each thread stamps it from then on. The same rule covers a *rolling* deploy in both directions,
because the stamp lives under its own metadata key
(`test_a_checkpoint_from_before_the_guard_resumes_rather_than_being_refused`,
`test_a_stamp_this_build_cannot_read_is_treated_as_absent`).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast, get_origin, get_type_hints

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent.session_store import _session_dsn
from chemclaw.agent.state import ChemclawState
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import register_pool, unregister_pool
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

_saver: AsyncPostgresSaver | None = None
_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

# Guards the two lazy initializations below — and `scratchpad.memory_store()`, which shares this
# lock because it shares the pool — each of them a check-then-*await*-then-act.
#
# **Publishing before the await is what made this a race rather than a style question.** Both
# `checkpointer()` and `_checkpoint_pool()` assigned their global *before* awaiting the work that
# makes the object usable — `setup()`'s ten migrations, and `pool.open()`. A second turn arriving
# inside either await saw a non-`None` global and got a saver whose tables do not exist yet, or a
# pool that is not open: `relation "checkpoints" does not exist` on a cold start with traffic,
# which is every deploy of a two-replica chart, since `api/runner._turn_checkpointer()` is awaited
# once per turn.
#
# Created lazily rather than at import, for the reason the saver itself is: `asyncio.Lock` binds to
# the running loop, and this module is imported by processes (Temporal workers, the CLI) that build
# their loop later or never. `close_checkpointer` drops it with the pool so the next loop gets its
# own.
_init_lock: asyncio.Lock | None = None

# How many checkpointer statements are queued on the saver's lock right now, across every saver in
# this process. A plain module-level int rather than a per-saver field because it is read by a
# scrape from outside any saver, and correct without a lock of its own: every mutation happens on
# the single thread of one event loop, with no `await` between the read and the write.
_statements_waiting = 0


def checkpointer_statements_waiting() -> float:
    """The gauge source for `chemclaw_checkpointer_statements_waiting`.

    See `SchemaStampedSaver._cursor` for what it measures and why no pool metric could.
    """
    return float(_statements_waiting)


def _strict_serde() -> JsonPlusSerializer:
    """The checkpoint serializer, pinned to reject import-by-name deserialization.

    `AsyncPostgresSaver` with no `serde=` builds `JsonPlusSerializer()`, whose msgpack ext hook
    defaults to **permissive** (`allowed_msgpack_modules=True` in `langgraph-checkpoint`): a stored
    blob may name *any* importable `module:callable`, and the hook runs
    `getattr(import_module(mod), attr)(*args)` on it — arbitrary code execution in the turn-serving
    pod on the resume of a poisoned `checkpoint_blobs` row. The app credential holds INSERT+DELETE
    on those tables (a delete+insert is an update), so this is reachable from the one privilege the
    least-privilege split (D-2026-08-05) grants the runtime role, not only from a DBA compromise.

    `allowed_msgpack_modules=None` restricts the hook to `SAFE_MSGPACK_TYPES`; a poisoned type is
    blocked (measured: the `os.system` payload returns a degraded value rather than executing) while
    every legitimate channel — LangChain messages, the todo list, `model_calls` — still round-trips,
    because those travel the typed/JSON path, not the import-by-name one. `pickle_fallback` stays
    upstream's `False`. `tests/test_upstream_surface.py` pins the permissive default so an upstream
    change to it turns red here rather than silently widening this surface.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=None)


def _initialization_lock() -> asyncio.Lock:
    """The current loop's initialization lock, created on first use.

    Not a race itself: this is called from coroutines, so it runs on the single thread of one event
    loop and cannot be interleaved before the assignment — there is no `await` between the check
    and the store.
    """
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


# The tables `AsyncPostgresSaver.setup()` creates. Named here because two other things need the
# list and neither can derive it: the erasure sweep (`agent/leaver.py`) has to delete a departing
# person's turn state, and its test has to prove the list is complete. `checkpoint_migrations` is
# deliberately absent from the *erasure* half — it holds schema versions, not anyone's conversation.
CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


# One turn's prune of the copies the newest checkpoint has superseded
# (`D-2026-09-06-a-superseded-checkpoint-is-a-copy-not-a-record`).
#
# **What it is for.** Every superstep of every turn rewrites the whole `messages` channel, so a
# thread stores four full copies of its entire conversation per turn and its blob bytes go as the
# square of its turn count. Measured on one thread, one tool call a turn: 0.881 / 3.524 / 7.930 /
# 14.098 MB of `checkpoint_blobs` at 10 / 20 / 30 / 40 turns — 1 : 4 : 9 : 16 against n**2's
# 1 : 4 : 9 : 16 — for 139.6 kB of conversation at the end of it, 109x. Nothing bounded that.
# `retention_checkpoints_days` disposes of a thread that has *stopped*; a thread still in use was
# bounded by nothing at all, and the reason it stayed that way for six review waves was a sentence
# rather than a defect: `durable/retention.py` said in-thread pruning would leave "survivors
# pointing at nothing". Measured, it does not — the ADR carries the run.
#
# **A version floor, not a `NOT IN` set, and that is what makes it safe beside a live turn.**
# LangGraph channel versions are zero-padded monotone counters (`000...040.0.5709...`), so a row
# written *after* this statement's snapshot sorts above every floor it computed and cannot be
# deleted. That closes the window `D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers` left open
# for `durable/retention.py`'s `_DELETE_ORPHANED`, which reasons from a `NOT EXISTS` instead.
# One statement, so on this autocommit pool it is one transaction: a concurrent reader sees the
# thread before it or after it, never mid-prune.
#
# **Partitioned by `checkpoint_ns`, which is the caveat that would have been found in production.**
# A turn that spawns the `task` helper writes a subgraph namespace beside the root one on the *same*
# `thread_id` — measured, one `tools:<uuid>` namespace per `task` call, 7 `checkpoints` and 3
# `checkpoint_blobs` each, and a *new* namespace every call. The review that asked for the partition
# expected over-pruning: a thread-wide floor taking a live helper's namespace whole. Measured, that
# is not this statement's failure — `oldest_kept` groups by `checkpoint_ns`, so a namespace with no
# row in the global top-K gets no floor and is simply never touched. With the `PARTITION BY` removed
# and nothing else changed, the root namespace went 52 -> 3 either way while every helper namespace
# went 7 -> 3 partitioned and stayed at **7** unpartitioned: a leak that grows with helper use
# rather than a loss. Over-pruning stays possible only in the window where a helper's own
# checkpoints are the newest on the thread, and one `PARTITION BY` closes both.
# `tests/test_checkpointer_prune.py` drives a real `task` call rather than asserting either.
#
# **The `EXISTS` is conservative in the safe direction.** A blob whose channel appears in no kept
# checkpoint's `channel_versions` is *not* deleted: the floor for it does not exist, so the clause
# is false and the row stays. Leaving a row nothing references costs bytes; deleting one something
# references costs the conversation.
_PRUNE_SUPERSEDED = """
WITH ranked AS (
    SELECT checkpoint_ns, checkpoint_id, checkpoint,
           row_number() OVER (PARTITION BY checkpoint_ns ORDER BY checkpoint_id DESC) AS rn
      FROM checkpoints WHERE thread_id = %(thread)s
),
kept AS (SELECT checkpoint_ns, checkpoint_id, checkpoint FROM ranked WHERE rn <= %(keep)s),
floors AS (
    SELECT kept.checkpoint_ns AS ns, versions.key AS channel, min(versions.value) AS floor_version
      FROM kept, jsonb_each_text(kept.checkpoint -> 'channel_versions') AS versions
     GROUP BY 1, 2
),
oldest_kept AS (
    SELECT checkpoint_ns AS ns, min(checkpoint_id) AS floor_id FROM kept GROUP BY 1
),
pruned_checkpoints AS (
    DELETE FROM checkpoints c USING oldest_kept
     WHERE c.thread_id = %(thread)s AND c.checkpoint_ns = oldest_kept.ns
       AND c.checkpoint_id < oldest_kept.floor_id
    RETURNING 1
),
pruned_writes AS (
    DELETE FROM checkpoint_writes w USING oldest_kept
     WHERE w.thread_id = %(thread)s AND w.checkpoint_ns = oldest_kept.ns
       AND w.checkpoint_id < oldest_kept.floor_id
    RETURNING 1
),
pruned_blobs AS (
    DELETE FROM checkpoint_blobs b
     WHERE b.thread_id = %(thread)s
       AND EXISTS (SELECT 1 FROM floors f
                    WHERE f.ns = b.checkpoint_ns AND f.channel = b.channel
                      AND b.version < f.floor_version)
    RETURNING 1
)
SELECT (SELECT count(*) FROM pruned_checkpoints),
       (SELECT count(*) FROM pruned_writes),
       (SELECT count(*) FROM pruned_blobs)
"""


def checkpoint_thread_delete_statements(match: str) -> tuple[tuple[str, str], ...]:
    """The three per-thread DELETEs, in an order a concurrent turn cannot tear.

    **A checkpointer write and a sweep are two writers, and the sweep's own transaction protects it
    only from itself.** This pool is `autocommit=True`, so a live turn commits its rows the instant
    it writes them, on a connection the deleter knows nothing about; and at READ COMMITTED each
    statement in the deleter's transaction takes a *fresh* snapshot. So a turn landing between
    `DELETE FROM checkpoints` and `DELETE FROM checkpoint_blobs` leaves its `checkpoints` row
    standing while its payload goes. Measured by hand on two connections, stepped statement by
    statement because the window is a millisecond wide: `residue: 1 checkpoints, 0 blobs`, and the
    surviving row stamps a channel value it can no longer load.

    So the two dependent statements **re-ask their question inside the deleter's transaction**: a
    thread's blobs and writes go only while that thread has no `checkpoints` row at all, which is
    exactly the racing turn's row, committed by then and visible to this statement's snapshot.
    `durable/retention.py` reached the same shape first, under
    `D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers`, and its sweep is where the whole
    argument is written down.

    **Here rather than in each deleter, because there were three and the fix landed in one.** The
    erasure sweep (`agent/leaver.py`) and the single-session delete
    (`agent/session_store.py`) both built the same checkpoints-first order by iterating
    `CHECKPOINT_TABLES`, and both kept it after the retention sweep was fixed — three sites past
    the Rule of Three, with the one that was corrected unable to tell the others. The retention
    sweep keeps its own pair because its first statement re-runs an *expiry* predicate and drives
    the other two from `RETURNING thread_id`, which is a different question; it asserts the rule for
    itself in `tests/test_retention.py`, and `tests/test_checkpoint_delete_order.py` asserts it for
    the two statements this function builds — by interleaving a real committed checkpoint between
    them, which is the only way to tell the two orders apart.

    Args:
        match: The caller's own predicate selecting the threads to delete, written against the
            table's `thread_id` — `"thread_id = %(session_id)s"`, or an `IN (…)` subselect. It is
            interpolated, so it must be the caller's own SQL and never a value from a request; the
            thread ids themselves belong in bound parameters, as every caller passes them.

    Returns:
        `(table, statement)` pairs in delete order, one per `CHECKPOINT_TABLES` entry.
    """
    statements: list[tuple[str, str]] = []
    for table in CHECKPOINT_TABLES:
        if table == "checkpoints":
            statements.append((table, f"DELETE FROM {table} WHERE {match}"))
            continue
        statements.append(
            (
                table,
                f"DELETE FROM {table} WHERE {match} AND NOT EXISTS ("
                f"SELECT 1 FROM checkpoints c WHERE c.thread_id = {table}.thread_id)",
            )
        )
    return tuple(statements)


# The metadata key each checkpoint's channel stamp is written under. Metadata is a plain jsonb
# column the saver round-trips untouched, so this needs no migration and no table of its own — and
# it travels *with* the checkpoint, which is the only thing that makes the check possible on a
# thread whose writer was a different build.
#
# Its own key rather than the `chemclaw_state_schema` one the first version of this guard used,
# because the *value* changed shape (a schema hash then, a channel list now) and a rolling deploy
# runs both builds at once. Under one key each build would read the other's value as a mismatch and
# refuse the thread; under two, each reads the other's checkpoints as unstamped and resumes them.
STATE_CHANNELS_KEY = "chemclaw_state_channels"

# The metadata key each checkpoint's *value* stamp is written under: the channel names that held a
# value at the instant this checkpoint was written.
#
# **Written because nothing upstream records it and the read cannot derive it.** A checkpoint's
# `channel_values` is split across two stores by `AsyncPostgresSaver.aput` — primitives stay inline
# in the `checkpoints` row, everything else moves to `checkpoint_blobs` — so a value that has gone
# missing from `checkpoint_blobs` is simply a channel the reader does not see. `channel_versions`
# looks like the answer and is not: measured on a healthy three-turn thread, **every** checkpoint
# names channels there that legitimately hold no value (`__start__` and `branch:to:*` are consumed
# by the step that reads them, which bumps the version and writes no blob), so a guard comparing
# the two refuses every thread in the fleet. This stamp is taken before the split, from the writer,
# where the answer is known exactly.
#
# Absent on a checkpoint written by a build without this stamp, and treated as unstamped for
# `STATE_CHANNELS_KEY`'s reason: a rolling deploy runs both builds, and refusing what the older one
# wrote would be the guard causing the harm it exists to prevent.
CHECKPOINT_VALUES_KEY = "chemclaw_checkpoint_values"


def _first_party_channels(state: Any) -> tuple[str, ...]:
    """The channel names `state` declares itself, with those of the base it extends left out.

    **Derived, not declared, because a version somebody has to remember to bump is a version that
    silently stops being one.** The failure this guards is invisible at the moment it is
    introduced: the change looks like an ordinary field rename and every test passes, because
    nothing in a unit test has a checkpoint from the previous build.

    **Names only.** A name is what a node indexes state by, so a name that appears is precisely what
    becomes a `KeyError` on a mid-turn resume. A same-name type change is not covered, and that is
    stated rather than fixed because a type repr is not stable enough to hang a session's
    resumability on.

    **The base's channels are subtracted, and that is the reason this function exists rather than a
    one-line `get_type_hints`.** A `TypedDict` merges its bases' annotations into its own
    `__annotations__` (measured on 3.11: `ChemclawState.__annotations__` reports langchain's
    channels beside this repository's, indistinguishably), so "what this repository declares" is not
    directly readable and has to be computed by difference. `__orig_bases__` is where the
    pre-merge base list survives. It is
    only populated when a base is generic — true of `PlanningState`, which extends
    `AgentState[ResponseT]` — so the subtraction can silently become a no-op if that ever changes;
    `tests/test_checkpointer_schema.py` asserts the result stays disjoint from the upstream base's
    channels, which turns that into a red build rather than a fleet-wide refusal.

    Args:
        state: The graph state class to read — `ChemclawState` in this process, and stand-in
            classes in the tests that prove what the derivation includes and excludes.

    Returns:
        The names this class adds to its base, sorted, so declaration order cannot move the stamp.
    """
    inherited: set[str] = set()
    for base in getattr(state, "__orig_bases__", ()):
        # `__orig_bases__` holds the written base, so a generic one arrives subscripted
        # (`AgentState[ResponseT]`); `get_type_hints` needs the class under it.
        origin = get_origin(base) or base
        # `__required_keys__` is what makes a class a `TypedDict` rather than `Generic` or `dict`,
        # both of which also appear in these lists and neither of which declares channels.
        if isinstance(origin, type) and hasattr(origin, "__required_keys__"):
            inherited |= set(get_type_hints(origin, include_extras=True))
    return tuple(sorted(set(get_type_hints(state, include_extras=True)) - inherited))


FIRST_PARTY_CHANNELS = _first_party_channels(ChemclawState)


class CheckpointValuesMissing(RuntimeError):
    """A thread's newest checkpoint has lost channel values it was written holding.

    Its own type, for `CheckpointSchemaMismatch`'s reason: "half this thread's rows are gone" is a
    different fact from "this session predates a state change" and from "the database is down", and
    only the first of the three is a reason to stop trusting what the thread reads back.
    """


class CheckpointSchemaMismatch(RuntimeError):
    """A thread's turn state never held a state channel this build declares.

    Raised instead of letting the restore proceed to the `KeyError` a node indexing that channel
    would otherwise produce. Its own type is the point: a caller can tell "this session predates a
    state change" from "the database is down", which is not something a `KeyError` on a field name
    supports.
    """


@asynccontextmanager
async def _translating(operation: str, config: RunnableConfig | None) -> AsyncIterator[None]:
    """Run one checkpointer statement, turning a pool or connection outage into `ConnectionError`.

    **This is the same translation `core/db.connection()` makes, and it is here because this pool
    is the one that does not go through it.** `api/runner._classify` decides what a chemist is told
    from the exception's *type*, and it tests `ConnectionError` and `TimeoutError` — which
    `psycopg_pool.PoolTimeout` is neither (measured: its MRO is `PoolTimeout → OperationalError →
    DatabaseError → Error → Exception`, and `PoolClosed`'s is the same). `core/db.py` translates for
    exactly that reason at both its connect paths; the checkpointer's autocommit pool bypasses them
    by design (the module docstring's three reasons), so the one Postgres pool that is not
    `core/db`'s was the one whose outage told the chemist "internal error, do not retry" — about
    the most retryable failure this system has.

    **`psycopg.OperationalError` and not `psycopg.Error`, which is what this caught.** `core/db.py`
    catches `OperationalError` at connect and `(PoolTimeout, PoolClosed)` at checkout, and says why
    a broader test is wrong: it collapses failures that are not the same failure. `psycopg.Error`
    is two levels wider and takes in `ProgrammingError`, `DataError` and every `IntegrityError` —
    so a pod started against a database where LangGraph's checkpoint tables were never created
    raised `UndefinedTable` (measured: a `ProgrammingError`, *not* an `OperationalError`), which
    became `ConnectionError`, which the front door classified `("storage_unavailable",
    retryable=True)`, which told a chemist to retry forever a failure no retry can fix. A schema
    fault has to reach the front door as what it is.

    **Every statement, not only the write.** This covered `aput` alone, and the other three run on
    the same bypassing pool: a `PoolTimeout` on `aget_tuple` — the *load* at the start of a turn,
    where saturation is at least as likely as at write time — reached the front door untranslated
    and booked `("internal", False)`. One wrapper, four call sites, so the answer cannot depend on
    which statement met the outage.

    Args:
        operation: what was being done, for the message the front door logs beside the failure.
        config: the `configurable` naming the thread, for the same message. Absent on some history
            reads, which stamp the empty thread id rather than failing a second time.

    Raises:
        ConnectionError: the statement could not run. Deliberately the same type `core/db.py`
            raises, so a caller classifying a database outage cannot get a different answer
            depending on which pool the statement went through.
    """
    try:
        yield
    except psycopg.OperationalError as exc:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "")
        # Counted before it is re-raised, because nothing counted a checkpointer failure at all. On
        # the write this is silent loss of the turn's state: the graph carries on in memory, and
        # whatever the turn had accumulated cannot be resumed. `degraded` is the shape every other
        # swallow in this repository uses — except that this one does not swallow. The statement
        # did not run, so the caller must still fail; what `degraded` buys is that
        # `chemclaw_degraded_total{subsystem="checkpointer"}` moves before it does.
        degraded(
            logger,
            "checkpointer",
            "could not %s turn state for session %s: %s",
            operation,
            thread_id,
            type(exc).__name__,
        )
        raise ConnectionError(
            f"checkpointer {operation} failed for session {thread_id!r}: {exc}"
        ) from exc


def _refuse_if_values_are_missing(stored: CheckpointTuple) -> None:
    """Refuse a checkpoint that no longer holds channel values it was written with.

    **The defect this exists for is that the half-deleted thread reads back as an empty
    conversation.** `durable/retention.py` prunes an expired thread out of `checkpoints`,
    `checkpoint_blobs` and `checkpoint_writes` in one transaction, and this pool is
    `autocommit=True` by design, so a live turn committing between two of those statements leaves
    its own `checkpoints` row standing while the sweep takes the blobs it just wrote. Measured on
    the real sweep and the real saver: `checkpoints=3, blobs=0`, `aget_state` returning `{'log':
    []}` with no exception, no log line and no counter, and the next turn answering as a brand-new
    conversation. The sweep's own half of that race is fixed where it happens; this is the guard
    for every *other* route into the same state — a restore, a partial `session_fork` copy, hand
    surgery on the tables — because a reader that cannot tell "resumed" from "started over" is the
    failure, not the sweep.

    **It compares the writer's own record, not `channel_versions`.** `channel_versions` names every
    channel the checkpoint depends on, and comparing it against what loaded is what this guard's
    first draft did. Measured on a healthy three-turn thread it flags **every checkpoint**:
    `__start__` and `branch:to:*` are consumed by the step that reads them, which bumps the version
    and writes no blob, so a legitimate checkpoint routinely names channels that hold no value. The
    stamp `aput` writes is taken before the inline/blob split, from the values the writer actually
    had, so a channel in it that does not load back is missing rather than absent.

    An unstamped checkpoint — one written by a build older than the stamp, or by
    `InMemorySaver` — passes, for the reason `STATE_CHANNELS_KEY` states: a rolling deploy runs both
    builds, and refusing the older one's checkpoints would brick every live session on the deploy
    that introduces the guard.

    Args:
        stored: The checkpoint tuple as loaded, values already merged from both stores.

    Raises:
        CheckpointValuesMissing: A stamped channel did not load back.
    """
    stamp = (stored.metadata or {}).get(CHECKPOINT_VALUES_KEY)
    if not isinstance(stamp, list):
        return
    loaded = stored.checkpoint.get("channel_values") or {}
    missing = [str(name) for name in stamp if name not in loaded]
    if not missing:
        return
    thread_id = stored.config.get("configurable", {}).get("thread_id", "")
    checkpoint_id = stored.config.get("configurable", {}).get("checkpoint_id", "")
    # `logger.error` rather than `degraded`, for the same reason the schema refusal beside it uses
    # one: `degraded` records a deliberate swallow — "the caller continued with less" — and this
    # call site continues with nothing. The turn fails, and the failure carries its own type.
    logger.error(
        "refusing turn state for session %s: checkpoint %s has lost channel value(s) %s",
        thread_id,
        checkpoint_id,
        ", ".join(missing),
    )
    raise CheckpointValuesMissing(
        f"session {thread_id!r} has turn state that is half gone: checkpoint {checkpoint_id!r} was "
        f"written holding channel(s) {', '.join(missing)} and no longer has them. Rows this "
        "checkpoint needs have been deleted from checkpoint_blobs or checkpoint_writes without its "
        "own row going with them. Resuming would answer out of an empty conversation as if it were "
        "the whole one, so it is refused. Start a new session: this one's transcript and audit "
        "trail are separate stores and are unaffected."
    )


class SchemaStampedSaver(AsyncPostgresSaver):
    """`AsyncPostgresSaver` that records the channels it writes and refuses a thread missing one.

    Two *schema* overrides, on the write and the resume, because those are the only two points
    where the state schema is knowable and where it matters. `alist` carries no schema guard:
    history reads render checkpoints, they do not restore them into a running graph, and a state
    change is not a reason to stop showing what a session did.

    **The outage translation is on all four**, which is a different question with a different
    answer: a saturated pool is a saturated pool whichever statement met it, and `_translating`
    says what reading only `aput` cost.

    **And one *observability* override, on `_cursor`**, which is where every statement of all four
    passes and where the pod's most-taken lock is. See `_cursor` for what it measures and why no
    pool metric could.

    The module docstring holds what is and is not caught by the schema stamp, and the argument for
    refusing rather than resuming empty.
    """

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncIterator[Any]:
        """Upstream's cursor, with the wait to get into it counted.

        **The pod's single most-taken lock was unmonitored, and the metric an operator was told to
        watch could not see it.** `AsyncPostgresSaver._cursor` opens
        `async with self.lock, get_connection(...)`, so every checkpointer statement in the process
        runs one at a time — *before* the pool is asked for anything. Measured: 8 concurrent turns
        on 8 different threads gave max concurrency 1 inside the saver and 612 ms of waiting, and
        during a deliberate stall `chemclaw_pg_pool_requests_waiting` read **0**, which is the
        precise symptom `core/db.register_pool`'s docstring claimed to have closed. It reads 0
        because the queue is the saver's lock and not the pool's; `pool_available: 0` is no
        substitute either, since a one-connection pool reads that whenever it is in use at all.

        The wait also has **no bound**: `asyncio.Lock` takes no timeout, so nothing raises and
        `_translating` — which exists to turn a checkpointer stall into a retryable
        `ConnectionError` — never fires. The only ceiling is `service_turn_timeout_seconds`, per
        turn, and every queued turn pays it in series. Making the queue visible is what lets an
        operator see that before the timeouts do; removing the serialization is a separate change
        (one saver per turn over the shared pool) and wants its own measurement.

        The gauge is entry/exit counted rather than read off the lock, because `asyncio.Lock`
        exposes no waiter count that is public API — a private `_waiters` read would be one more
        upstream shape nobody promised.

        Args:
            pipeline: Passed straight through to upstream; see `AsyncPostgresSaver._cursor`.
        """
        global _statements_waiting
        started = time.perf_counter()
        _statements_waiting += 1
        try:
            async with super()._cursor(pipeline=pipeline) as cur:
                # Sampled here rather than in a `finally`, so the measurement is the *wait* and not
                # the wait plus the statement — the two are separate questions and only the first
                # one is this lock's.
                METRICS.observe(
                    "chemclaw_checkpointer_lock_wait_seconds", time.perf_counter() - started
                )
                yield cur
        finally:
            _statements_waiting -= 1

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Write the checkpoint with this build's channel names, and the values it holds, stamped.

        Two stamps, answering two different questions on resume. `STATE_CHANNELS_KEY` is what this
        *build* declared; `CHECKPOINT_VALUES_KEY` is what this *checkpoint* held — read here,
        before `super().aput` splits those values between the inline column and `checkpoint_blobs`,
        because after the split neither store can say which channels the other was supposed to
        have. Both constants carry the argument for their own shape.

        The outage translation is `_translating`'s, shared with the other three statements on this
        pool; that function holds the measurements and why it is `OperationalError` rather than
        `psycopg.Error`.

        **And one write of every turn also prunes what it superseded** — `_prune_superseded` says
        when and why, and `_PRUNE_SUPERSEDED` says what and how safely. After the write rather than
        before it, so a thread is never smaller than the checkpoint that is about to replace it.

        Raises:
            ConnectionError: The checkpoint could not be written.
        """
        stamped = cast(
            CheckpointMetadata,
            {
                **metadata,
                STATE_CHANNELS_KEY: list(FIRST_PARTY_CHANNELS),
                # `.get`, because the stamp must never be the reason a checkpoint write fails:
                # `channel_values` is always present on a checkpoint LangGraph built, and a
                # hand-constructed one (a test, a future caller) would otherwise raise here rather
                # than at the statement that actually needs it.
                CHECKPOINT_VALUES_KEY: sorted(checkpoint.get("channel_values") or {}),
            },
        )
        async with _translating("write", config):
            written = await super().aput(config, checkpoint, stamped, new_versions)
        await self._prune_superseded(config, metadata)
        return written

    async def _prune_superseded(self, config: RunnableConfig, metadata: CheckpointMetadata) -> None:
        """Delete the copies this thread's newest checkpoints have superseded.

        `_PRUNE_SUPERSEDED` carries the statement, the measurement and why a version floor is the
        predicate. This method is only the *when* and the *whether*.

        **Once a turn, on the root namespace's input checkpoint.** A turn writes thirteen
        checkpoints and pruning after each of them was measured — it bounds the thread more tightly
        (3 rows against 15) and costs 2.88 s of extra statements over 40 turns against 0.88 s, on a
        pool whose every statement already queues behind one process-wide lock. `source == "input"`
        is LangGraph's own `CheckpointMetadata` literal and is written exactly once per `ainvoke`
        per namespace, which makes "one prune per turn" a property of upstream's write pattern
        rather than a counter this class would have to keep. Restricted to `checkpoint_ns == ""`
        because the statement already prunes every namespace of the thread, so a helper's own input
        checkpoint would only repeat the same work.

        **The residual is stated rather than implied**: pruning at the turn boundary bounds a thread
        at the retained checkpoints plus one turn's writes, so a single runaway turn is bounded by
        the loop cap and not by this. And the *write* volume stays quadratic under any prune — that
        is upstream's `_dump_blobs` rewriting the whole `messages` channel per superstep, and only a
        destructive trim of state would reach it, which
        `D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has` forbids.

        **A failure here does not fail the turn.** The checkpoint is already written and committed;
        this is housekeeping on a separate statement, and taking a chemist's answer away because a
        `DELETE` could not run would trade a bounded disk cost for a lost turn. It is logged at
        WARNING rather than swallowed, so a prune that never works is visible.

        Args:
            config: The `configurable` of the write, naming the thread and the namespace.
            metadata: The checkpoint's metadata, read for upstream's `source`.
        """
        keep = settings.checkpoint_retain_per_thread
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        if not keep or not thread_id:
            return
        if configurable.get("checkpoint_ns") or metadata.get("source") != "input":
            return
        try:
            async with self._cursor() as cur:
                await cur.execute(_PRUNE_SUPERSEDED, {"thread": thread_id, "keep": keep})
                await cur.fetchone()
        except psycopg.Error as exc:
            logger.warning(
                "could not prune superseded checkpoints for session %s: %s; the thread is intact "
                "and keeps every superseded copy until this succeeds",
                thread_id,
                exc,
            )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Write one task's pending channel updates, translating an outage like every other write.

        No schema stamp: this writes into `checkpoint_writes`, which carries channel values for a
        task rather than a checkpoint's metadata, so there is nothing here to stamp and nothing to
        refuse on resume. What it shares with `aput` is the pool, and therefore the outage.
        """
        async with _translating("write", config):
            await super().aput_writes(config, writes, task_id, task_path)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Load the checkpoint, refusing one that predates a channel this build declares.

        And refusing one whose stored values no longer cover what it was written holding —
        `_refuse_if_values_are_missing` carries that argument and the measurement behind it.

        A stamp that is absent, or that this build cannot read — the schema-hash string the first
        version of this guard wrote, or anything else that is not a list of names — is treated the
        same as an unstamped checkpoint and resumed, for the module docstring's reason: refusing it
        would brick live sessions on the deploy that changed the stamp.

        Args:
            config: The `configurable` naming the thread (and optionally the checkpoint) to load.

        Returns:
            The stored checkpoint, or `None` when the thread has none.

        Raises:
            CheckpointValuesMissing: The stored checkpoint has lost channel values it was written
                holding, so resuming it would answer out of a conversation that is half gone.
            CheckpointSchemaMismatch: The stored checkpoint never held a channel this build
                declares, so restoring it can fail inside a node instead of here.
            ConnectionError: The checkpoint could not be read — see `_translating`. This is the
                *load* at the start of a turn, and it was the untranslated one: a saturated pool
                here told the chemist "internal error, do not retry" about a wait.
        """
        async with _translating("read", config):
            stored = await super().aget_tuple(config)
        if stored is None:
            return None
        _refuse_if_values_are_missing(stored)
        stamp = (stored.metadata or {}).get(STATE_CHANNELS_KEY)
        if not isinstance(stamp, list):
            return stored
        missing = [name for name in FIRST_PARTY_CHANNELS if name not in stamp]
        if not missing:
            return stored
        held = ", ".join(str(name) for name in stamp) or "none"
        thread_id = stored.config.get("configurable", {}).get("thread_id", "")
        logger.error(
            "refusing turn state for session %s: it never held state channel(s) %s; it holds %s",
            thread_id,
            ", ".join(missing),
            held,
        )
        raise CheckpointSchemaMismatch(
            f"session {thread_id!r} has turn state from before this build declared the state "
            f"channel(s) {', '.join(missing)} (it holds {held}). LangGraph has no migration for "
            "that, and a turn resuming mid-graph raises a bare KeyError from whichever node "
            "indexes one of them. Start a new session: this one's transcript and audit trail are "
            "unaffected and its checkpoints stay until retention prunes them."
        )

    def alist(
        self,
        config: RunnableConfig | None,
        *,
        # `filter` shadows the builtin; it is upstream's parameter name and a caller
        # passes it by keyword, so renaming it here would break the override.
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Page a thread's history, translating an outage the way every other statement here does.

        Overridden only for that. There is no schema guard on a history read — the class docstring
        says why — but a saturated pool is the same wait whether a turn is starting or the CLI is
        rendering what a session did, and this is the fourth statement on the pool `core/db.py`
        does not translate for.

        Written as a plain method returning the guarded generator rather than as an `async def`
        generator, because the two differ in *when* the body starts: an async generator's body
        does not run until the first `__anext__`, so a caller that builds the iterator and awaits
        something else first would see the failure at a place unrelated to the statement. Neither
        arrangement changes what is raised; this one keeps `_translating`'s message beside the
        iteration it describes.
        """

        async def _guarded() -> AsyncIterator[CheckpointTuple]:
            async with _translating("read", config):
                async for stored in super(SchemaStampedSaver, self).alist(
                    config, filter=filter, before=before, limit=limit
                ):
                    yield stored

        return _guarded()


async def process_checkpointer() -> Any:
    """Turn state for a process that keeps **one** graph alive for its whole run.

    Durable where the deployment has a database, `InMemorySaver` otherwise — which is a real
    conversation for as long as the process lives, and the honest lifetime for a run whose
    transcript is not stored anywhere either.

    **Not the same question `api/runner._turn_checkpointer` answers, and the difference is the
    graph's lifetime.** The front door compiles a graph *per turn* (it binds that turn's connector
    tools at construction), so an in-memory saver there would be created and discarded inside one
    turn and hold nothing — `None` is the truthful answer for a deployment with no database. A
    terminal session builds its graph once, so the same saver spans every turn of the run and the
    in-memory branch is worth taking.

    Here rather than in `chemclaw.cli` because the caller must not name `langgraph` at module scope:
    `tests/test_third_party_layering.py` polices which package may depend on which third-party
    stack, and the CLI is not one that owns this one. That is not a formality worked around — this
    module is what "where turn state lives" means, so the decision belongs beside the durable
    saver it chooses between.

    Returns:
        A checkpointer to build a long-lived graph on and to read that session's plan from.
    """
    if settings.session_store == "postgres":
        return await checkpointer()
    return InMemorySaver()


async def checkpointer() -> AsyncPostgresSaver:
    """The process's checkpointer, created and migrated on first use.

    Idempotent: `setup()` records applied versions in `checkpoint_migrations` and applies only what
    is missing, so calling this on every agent build costs one query after the first.

    A `SchemaStampedSaver` rather than a bare `AsyncPostgresSaver`, because the durable saver is the
    one whose checkpoints outlive the build that wrote them — the in-memory saver
    `process_checkpointer` falls back to cannot be resumed by a different schema at all, since it
    dies with the process that declared one.

    **Published only once it is usable, under `_init_lock`.** The assignment used to happen before
    `setup()` was awaited, so a concurrent second turn saw a non-`None` global and got a saver whose
    migrations had not run — see the lock's own comment. A ready saver is returned without taking
    the lock at all, so the steady-state cost is one `is None` check.

    Returns:
        A ready saver over this process's checkpointer pool.
    """
    global _saver
    if _saver is not None:
        return _saver
    # Awaited outside the lock, because `_checkpoint_pool` takes that same lock itself and
    # `asyncio.Lock` is not reentrant.
    pool = await _checkpoint_pool()
    async with _initialization_lock():
        if _saver is None:
            saver = SchemaStampedSaver(pool, serde=_strict_serde())
            await _setup_once(saver, _session_dsn())
            _saver = saver
            logger.info("checkpointer ready")
    return _saver


# The advisory-lock key that serializes checkpointer migrators. Arbitrary but stable, and
# deliberately distinct from `core/migrate._MIGRATION_LOCK_KEY`: advisory locks share one namespace
# per database, so two subsystems picking the same number would block each other for no reason
# either could diagnose. Same convention, next discriminator.
_SETUP_LOCK_KEY = 0x43484D4157_00_03  # "CHMAW" + a discriminator for the checkpointer's setup

# How long a pod waits for a peer's `setup()` before giving up on the lock and running its own.
# Not a config knob: it is a property of how long this one migration takes, not something a
# deployment tunes. Ten seconds of 0.1 s polls — `setup()` against an already-migrated schema costs
# one query, and against a virgin schema it is three `CREATE TABLE`s and three
# `CREATE INDEX CONCURRENTLY` on empty tables.
_SETUP_LOCK_POLL_SECONDS = 0.1
_SETUP_LOCK_POLLS = 100


async def _setup_once(saver: AsyncPostgresSaver, dsn: str) -> None:
    """Migrate the checkpoint tables under an advisory lock, so two pods cannot race each other.

    **The in-process half of this was closed and the cross-process half was not.** `_init_lock`
    exists because a second turn used to get a saver whose migrations had not run; two *pods* doing
    the same thing on a fresh database is the identical failure one layer out, and it is every
    deploy of a two-replica chart. `CREATE TABLE IF NOT EXISTS` is not race-safe against itself —
    the existence check and the create are not one operation — and neither is the version ledger
    `setup()` keeps. Measured, two savers running it concurrently against a schema that had never
    seen these tables: one raised
    `UniqueViolation: duplicate key value violates unique constraint "checkpoint_migrations_pkey"`,
    the other succeeded, and afterwards all seven indexes were present and valid.

    The blast radius was small and pointed exactly the wrong way: the failure is a `psycopg.Error`
    that is **not** an `OperationalError`, so `_translating` does not touch it (and `setup()` is
    called outside it anyway), and the chemist got `("internal", retryable=False)` about the one
    state a retry fixes immediately.

    **A lock rather than a retry, which was tried first and measured failing.** Retrying once is
    not enough, because the winner's own migration is still in flight when the loser retries: it
    reads the ledger at version -1 again and collides on the same row a second time.

    **And a *polled* lock rather than a held wait, which is the shape `core/migrate.py` uses and is
    wrong here — measured, it deadlocks.** Three of `setup()`'s migrations are
    `CREATE INDEX CONCURRENTLY`, and CIC waits for every other transaction on the database that
    holds a snapshot. A waiting `pg_advisory_lock` (or a `pg_advisory_xact_lock` inside a
    transaction) *is* such a snapshot, so the winner's CIC waits for the loser's wait while the
    loser waits for the winner's lock — two pods stuck forever, which is how this test first hung
    to its timeout. `pg_try_advisory_lock` returns immediately, so the waiting pod is idle with no
    transaction between polls and the winner's CIC can finish.

    **On a dedicated autocommit connection, not one borrowed from the saver's pool**: the lock is
    held *across* `setup()`, which runs on that pool, so borrowing from it would deadlock the moment
    the pool is small — and autocommit is what keeps each poll from opening a transaction.

    A pod that never gets the lock runs `setup()` anyway and says so: the alternative is a process
    with no checkpointer because a peer's backend is wedged, and after ten seconds the peer is not
    mid-`CREATE TABLE`.

    Args:
        saver: The saver whose `setup()` to run.
        dsn: The database to take the advisory lock on — the saver's own.

    Raises:
        psycopg.Error: `setup()` itself failed for a reason that is not a concurrent migrator.
        ConnectionError: The lock connection could not be opened.
    """
    async with await db.connect(dsn) as guard:
        await guard.set_autocommit(True)
        held = False
        for attempt in range(_SETUP_LOCK_POLLS):
            cursor = await guard.execute("SELECT pg_try_advisory_lock(%s)", (_SETUP_LOCK_KEY,))
            row = await cursor.fetchone()
            if row is not None and row[0]:
                held = True
                break
            if attempt == 0:
                logger.info("another pod is migrating the checkpoint tables; waiting for it")
            await asyncio.sleep(_SETUP_LOCK_POLL_SECONDS)
        if not held:
            logger.warning(
                "waited %.0fs for another pod's checkpointer migration and never got the lock; "
                "running setup() anyway rather than leaving this process without a checkpointer",
                _SETUP_LOCK_POLLS * _SETUP_LOCK_POLL_SECONDS,
            )
        try:
            await saver.setup()
        finally:
            if held:
                # The lock also dies with this session, which is what covers the pod being killed
                # mid-migration. Releasing it here is what keeps the next caller in this same
                # process from polling against a lock nobody is using.
                await guard.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK_KEY,))


async def _checkpoint_pool() -> Any:
    """This process's checkpointer pool — autocommit, opened once.

    `min_size=0` because a process that never takes a turn (a Temporal worker running calculations)
    should not hold connections open for a checkpointer it will not use, and the pool fills on
    demand.

    **Takes `_init_lock` itself, so no caller may hold it.** This used to say it had exactly one
    caller — `checkpointer()`, which held the lock around it — and that stopped being true when
    `scratchpad.memory_store()` became the second: two cold callers then raced the same
    check-then-*await*-then-act the lock exists for, each building a pool and awaiting `open()`
    before either published `_pool`, so one opened pool was overwritten and leaked its connections
    for the life of the process while a store was left sitting on a pool the module no longer
    knows about. Owning the lock here rather than borrowing a caller's is what makes the guarantee
    independent of who calls: `asyncio.Lock` is not reentrant, so both callers await this *before*
    taking the lock for their own object.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _initialization_lock():
        if _pool is None:
            pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
                conninfo=_session_dsn(),
                kwargs={"autocommit": True, "connect_timeout": settings.pg_connect_timeout_seconds},
                # The one deliberate divergence from `core/db`'s pool, which uses
                # `pg_pool_min_size`: a process that never takes a turn (a Temporal worker running
                # calculations) should not hold connections open for a checkpointer it will not
                # use, and the pool fills on demand.
                min_size=0,
                # **Sized for the *store*, not for the saver, and that is worth saying because the
                # saver is what the pool is named after.** `AsyncPostgresSaver._cursor` holds one
                # `asyncio.Lock` around its connection checkout, so the saver alone can never use
                # more than one connection here — measured, 8 concurrent turns opened exactly 1
                # against a `max_size` of 16. The obvious conclusion, "so build it with
                # `max_size=1`", is wrong: `scratchpad.memory_store()` puts an `AsyncPostgresStore`
                # on this same pool, and upstream's store `_cursor` **deliberately does not
                # serialize on a pooled connection** ("the pool does not hand out the same
                # connection concurrently, so a shared lock across calls is unnecessary"), so it
                # is the genuinely concurrent consumer and capping the pool at 1 would serialize
                # agent memory to make a gauge tidy.
                max_size=settings.pg_pool_max_size,
                # **The three settings this pool used to decline, and it is the one pool every
                # turn's state write goes through.** It named none of them, so it ran on
                # psycopg_pool's defaults while every `core/db` pool in the same process ran on the
                # configured ones — measured live: `timeout=30.0` against
                # `pg_pool_timeout_seconds=10.0`, `max_idle=600` against
                # `pg_pool_max_idle_seconds=300`, and no `check` at all.
                #
                # Neither difference is tidiness. A saturated waiter was refused at **30.02 s**
                # rather than 10.01 s, holding an admission permit for six times the admission
                # timeout — a degradation shape, not a rounding error. And with no `check`, a
                # backend killed from outside the pool (a managed-Postgres idle limit, a load
                # balancer's NAT timeout, `idle_in_transaction_session_timeout`) is handed straight
                # to a turn as `AdminShutdown` where `core/db`'s pool swaps it silently.
                #
                # `core/db._pool_for` is the reference rather than these literals:
                # `tests/test_checkpointer_concurrency.py` asserts the two pools agree, so a
                # setting added there is not silently declined here.
                timeout=settings.pg_pool_timeout_seconds,
                max_idle=settings.pg_pool_max_idle_seconds,
                check=AsyncConnectionPool.check_connection,
                open=False,
            )
            await pool.open()
            # Counted in this process's pool readings. It is not a `core.db` pool — this module
            # owns its lifecycle, which is why registration is all that happens here — but it is
            # `pg_pool_max_size` more connections the process may open, and every turn's state
            # write goes through it. Unregistered, a turn-serving process opened twice what
            # `chemclaw_pg_pool_max_size` reported (the number the fleet budget is checked
            # against), and a saturated checkpointer stalled turns inside `AsyncPostgresSaver`
            # while `chemclaw_pg_pool_requests_waiting` read 0.
            register_pool(pool)
            # Bound here rather than in `core/db.py`, which may not import `agent` (layering), and
            # rather than in `api/app.py`, which is not the only process that builds a
            # checkpointer: any process that has one has this queue.
            METRICS.bind_gauge(
                "chemclaw_checkpointer_statements_waiting", checkpointer_statements_waiting
            )
            _pool = pool
    return _pool


async def close_checkpointer() -> None:
    """Drop the process's checkpointer and close its pool — for tests and orderly shutdown.

    The saver is dropped with the pool because it holds both the pool *and* the loop it was built
    in; keeping one without the other is how a second caller in a second event loop gets a saver
    pinned to a loop that has closed.

    **The memory store is dropped first, for the same reason and in that order.** It sits on this
    pool too (`scratchpad.memory_store`), so closing the pool while it is still published would
    hand the next caller a store over closed connections — the store has to go before what it
    stands on does. This is `close_memory_store`'s only caller, which is what makes the pair a
    lifecycle rather than two functions that happen to exist.

    **A pool whose loop has already closed is dropped, not awaited.** `psycopg_pool` schedules its
    workers' shutdown on the loop it was opened in, so closing it from a *different* live loop
    raises `RuntimeError: Event loop is closed` — from inside the close, after the reference would
    otherwise have been cleared, leaving the process holding a pool nobody can close. Production has
    one loop, so this is a test-shaped hazard; it is handled here rather than in the tests because
    the alternative is every caller remembering which loop opened the pool. The connections are
    released with their dead loop either way, so there is nothing left to leak.
    """
    global _saver, _pool, _init_lock
    # Imported here rather than at module scope: `scratchpad` pulls the deepagents backends in, and
    # a worker that only needs `CHECKPOINT_TABLES` should not import the agent's filesystem stack.
    from chemclaw.agent.scratchpad import close_memory_store

    await close_memory_store()
    _saver = None
    # Dropped with the pool for the same reason the saver is: an `asyncio.Lock` belongs to the loop
    # it was created in, so a lock kept across `close_checkpointer` would be one the next loop's
    # first caller waits on forever.
    _init_lock = None
    pool, _pool = _pool, None
    if pool is None:
        return
    # Off the process's readings before it is closed, so a gauge scraped mid-shutdown does not
    # count connections that are going away.
    unregister_pool(pool)
    try:
        await pool.close()
    except RuntimeError:
        logger.debug("the checkpointer pool outlived its event loop; dropped without closing")
