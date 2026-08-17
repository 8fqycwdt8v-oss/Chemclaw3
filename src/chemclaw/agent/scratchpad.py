"""Where a turn puts intermediate work: a scratchpad, and memories that outlive the session.

**The gap this closes is structural rather than promptable.** Until now the only backend a turn
could reach was the narrowed skills tree — read-only, one verb. Every tool result therefore landed
in the context window and was reclaimed from it by `agent/compaction.py`, so a research turn that
pulled six sources could not hold six sources: the earliest were replaced by a placeholder before
the answer was written. That is not a prompt problem and no instruction fixes it. A hard research
task — pull several sources, launch calculations, merge the lot into a report or a draft procedure —
needs somewhere to put work that is neither the context window nor the knowledge graph.

Three routes over one `CompositeBackend`, and each boundary is a decision:

- **`/scratch/…` → `StateBackend`.** Files live in the graph's own state, so they are per-thread,
  never touch a disk, and die with the checkpoint. This is the working surface: a turn writes its
  running notes here and re-reads them after a calculation returns.
- **`/skills/…` → `NarrowedSkillsBackend`,** exactly as before. Read-only, three predicates, writes
  refused by the backend rather than by a listing filter (`agent/skill_backend.py` says why that
  difference is a security property).
- **`/memories/…` → `StoreBackend`** over `AsyncPostgresStore`, when a deployment enables it *and*
  the turn has an actor. This is the only route that outlives the session.

**The namespace is the erasure key, and that is the whole reason this module chooses it rather than
letting upstream default it.** `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives`
rejected `BaseStore` on four grounds, and the decisive one was not about capability: `store` has no
actor column, so `tests/test_leaver.py`'s derived right-to-erasure check would report a departing
person's memories as absent while they remained. *A safety net that returns a false green is worse
than no safety net, because it is trusted.* That objection is GDPR, not GxP — it survives the
retirement of the regulated framing and had to be answered rather than waived.

The answer is to put the actor in the namespace. `store.prefix` is a plain text column holding the
dotted namespace, so `("memories", <actor digest>)` makes erasure a prefix match — the same shape
every other table in `agent/leaver.py` already uses — and makes the derived completeness test able
to see it.

**Computed at build time, not from the runtime.** Upstream's `NamespaceFactory` takes a runtime so
a namespace can vary per call, which this deliberately does not use: the graph is already compiled
per turn (connectors bind at construction and a session belongs to one turn), so the actor is known
when the backend is built. A closure over a value beats a lookup through somebody else's context —
it is a pure function a test can assert, and it cannot silently resolve to a different person.

**No actor means no route at all**, rather than a shared namespace. The CLI, a template step and the
eval harness all run without ambient identity, and a memory written under an "anonymous" prefix
would be a memory nobody can erase and everybody can read. Those paths fall through to
`StateBackend`, which is turn-scoped — they get a scratchpad and no memory, which is correct.

**What this is not.** It is a working surface, not knowledge. Layer 4 stays Git plus Markdown behind
the PR-gate: a conclusion worth keeping still goes through `propose_knowledge_note` and a human.
Nothing under `/memories/` is evidence a citation can resolve to — `verifier.turn_evidence` scores
against tool outputs, and a file the model wrote itself is not one.

**The audit objection answers itself here, which is worth stating because the same ADR raised it.**
It observed that a store write "passes through none of the six tool middlewares including
`audit._recording`". True of a direct `store.aput`; false of this design, because the only path to
the store is `write_file`/`edit_file`/`delete`, which are *tools* — they cross the same
`wrap_tool_call` chain as every other call, so they are audited, authorized, refused on a dry run
and counted by the repeat guard. `tests/test_scratchpad.py` asserts that no first-party module calls
`aput`/`adelete` on a store directly, so the property is enforced rather than described.

**Three of those four were true when written and the fourth was not**, which is worth keeping
because the reason generalises. Auditing, authorization and the repeat guard key on the tool *name*
and so covered these the day they were registered. The dry-run refusal and the plan gate key on
`side_effecting_tools()`, which is assembled from the tool registry, every connector's
`state_changing` declaration and every template launcher — and `FilesystemMiddleware` registers its
verbs with none of those, so a `dry_run=true` turn could write a durable row past a promise that
nothing had been started. The fix is not a name added to that set: `write_file` is durable under
`/memories/` and turn-local under `/scratch/`, so gating the name would deny a turn its own notepad.
`authz.side_effecting_call` reads the call's `file_path` instead, and both gates ask it.
"""

import logging
from typing import Any, cast

from deepagents import FsToolName
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langgraph.store.postgres.aio import AsyncPostgresStore

from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.ids import stable_hash

logger = logging.getLogger(__name__)

_store: AsyncPostgresStore | None = None

# The tables `AsyncPostgresStore.setup()` creates, named here for the reason `CHECKPOINT_TABLES` is
# named in `agent/checkpointer.py`: nothing can derive the list, because these tables are upstream's
# and appear in no migration in `infra/sql`. The erasure sweep has to reach a departing person's
# memories, and it spells both names itself (`agent/leaver.py`, one `DELETE` per table, dependent
# side first). `store_vectors` carries the embeddings and is keyed on `(prefix, key)`.
#
# **The retention sweep does not touch either table**, and an earlier version of this comment said
# it "has to prune them by age", which was never true: `durable/retention.py`'s `_PRUNABLE` names
# `session_events`, `session_messages`, `tool_result_blobs` and `checkpoints`, and no fifth entry.
# The omission is the design — a memory is written to persist, so disposing of one is a capability
# decision with its own policy, not something an age cutoff may decide. Turn state is the opposite
# and is pruned; that is `checkpoints`, not `store`.
STORE_TABLES: tuple[str, ...] = ("store", "store_vectors")

# The root the memories route is mounted at. A constant because three places spell it — the route
# key, the permission rules that allow writes under it, and the erasure prefix — and a fourth that
# disagreed would be a route nobody can erase.
MEMORY_ROOT = "/memories/"

# The scratchpad root. Unrouted, so it resolves to the composite's default `StateBackend`; named
# only so the permission rules and the system prompt can agree on where a turn may write.
SCRATCH_ROOT = "/scratch/"


def memory_namespace(actor: str) -> tuple[str, ...]:
    """The store namespace one person's memories live under.

    Digested rather than raw, for two reasons that both matter. `store`'s namespace components are
    validated against a character class that rejects most punctuation, and an Entra `oid` is not the
    only actor spelling this system holds — `unverified:<id>` is the other (`agent/leaver.py`
    explains why). A digest is always a legal component and collapses neither spelling into the
    other, so `_actor_forms` can hash each and erase both.

    Args:
        actor: The turn's actor id, in whichever spelling the caller holds.

    Returns:
        The namespace tuple, stable for one actor across processes and restarts.
    """
    return ("memories", stable_hash(actor))


def memory_prefix(actor: str) -> str:
    """The `store.prefix` value that names one person's memories, for the erasure sweep.

    `store` stores a namespace as its components joined by `.`, which is what makes a prefix match
    the right shape for erasure. Exposed so `agent/leaver.py` builds the same string this module
    writes under rather than re-deriving the join — the defect class where two modules agree about a
    key until one of them is edited.

    Args:
        actor: The departing person's id.

    Returns:
        The dotted prefix under which every memory of theirs is stored.
    """
    return ".".join(memory_namespace(actor))


async def memory_store() -> AsyncPostgresStore:
    """The process's memory store, created and migrated on first use.

    Shares the checkpointer's pool deliberately. That pool is autocommit and `min_size=0` for three
    measured reasons recorded in `agent/checkpointer.py` — `CREATE INDEX CONCURRENTLY` cannot run
    inside a transaction, one `asyncio.Lock` per saver, and pipeline mode — and every one of them
    applies to this store's own `setup()` for the same reason. A second pool against the same DSN
    would double the connection budget to buy nothing.

    **Published only once it is usable, under the checkpointer's `_init_lock`.** This was a
    check-then-*await*-then-act that assigned `_store` *before* awaiting `setup()` — the exact
    defect `checkpointer()` was fixed for, failing the same way: a second turn arriving inside that
    await saw a non-`None` global and got a store whose two tables do not exist yet (`relation
    "store" does not exist` on a cold start with traffic). The checkpointer's lock is shared rather
    than a second one of this module's own, because both initializations sit on that one pool and
    one lock is what lets `close_checkpointer` drop the pair together.

    Returns:
        A ready store over this process's session pool.
    """
    global _store
    if _store is not None:
        return _store
    # Imported here rather than at module scope: `checkpointer` imports `state`, which imports
    # config, and a module-scope import would put this module in that cycle for one call.
    from chemclaw.agent.checkpointer import _checkpoint_pool, _initialization_lock

    # Awaited outside the lock, and it must stay outside: `_checkpoint_pool` takes the same lock,
    # and `asyncio.Lock` is not reentrant.
    pool = await _checkpoint_pool()
    async with _initialization_lock():
        if _store is None:
            store = AsyncPostgresStore(pool)
            await store.setup()
            _store = store
            logger.info("memory store ready (%d tables)", len(STORE_TABLES))
    return _store


async def close_memory_store() -> None:
    """Drop the process's store — called by `close_checkpointer`, which owns the pool beneath it.

    The pool belongs to the checkpointer, so this releases the store and leaves the pool to
    `close_checkpointer`. Closing it here would pull the connections out from under the saver.

    **Its caller is `close_checkpointer`, and the ordering is the point**: the store is dropped
    *before* the pool it sits on is closed, so nothing can be handed a store over closed
    connections. It had no caller at all, which left `close_checkpointer` closing the pool while
    `_store` still pointed at it — the next `memory_store()` would have returned that store and
    every operation on it would have failed against a closed pool.
    """
    global _store
    _store = None


def scratchpad_backend(skills: CompositeBackend, store: Any | None = None) -> CompositeBackend:
    """Extend a turn's skills backend with a scratchpad and, when enabled, durable memories.

    Takes the skills backend rather than rebuilding it, because the caller already holds it — the
    skills middleware and this backend must be the *same* object, or a role-gated narrowing computed
    for one would not apply to the other.

    **The store arrives as an argument rather than being built here, and that is deliberate.**
    Creating it is `await`, and making this function async would make `build_langgraph_agent` async
    and every one of its callers with it. The checkpointer already solved the same problem the same
    way — it is a parameter, built by the async caller that has a running loop — so the store
    follows the established seam instead of inventing a second one.

    The memories route is added only when a store was passed **and** the turn has an actor. Neither
    condition is a preference: without the setting a deployment has no `store` tables, and without
    an actor there is no namespace that could be erased.

    Args:
        skills: The narrowed skills backend for this profile (`langgraph_agent.skills_backend`).
        store: This process's `AsyncPostgresStore` from `memory_store()`, or `None` for a turn with
            no durable memory — which is every turn under the default configuration.

    Returns:
        A backend routing `/skills/…` as given, `/memories/…` to the store when both conditions
        hold, and everything else — `/scratch/…` included — to graph state.
    """
    routes = dict(skills.routes)
    actor = get_current_actor()
    if store is not None and actor:
        namespace = memory_namespace(actor)
        # A closure over the value, not a read through the runtime: see the module docstring. The
        # lambda takes the runtime upstream passes and ignores it, which is the whole point.
        routes[MEMORY_ROOT] = StoreBackend(namespace=lambda _runtime: namespace, store=store)
    return CompositeBackend(default=StateBackend(), routes=routes)


def scratchpad_tools() -> tuple[FsToolName, ...]:
    """The filesystem verbs this deployment lets a turn reach, in one place.

    Read off the middleware rather than spelled out — the same rule
    `chemclaw_agent.harness_tool_names` follows for `write_todos` — so an upstream rename becomes a
    changed value instead of a silently stale allow-list. Two are withheld and each has its own
    argument:

    - **`execute`** would be a shell. deepagents 0.7 ships exactly one concrete sandbox
      (`LangSmithSandbox`), which this repository declines on content-egress grounds, and
      `LocalShellBackend` is documented as unrestricted. A shell acquired as a side effect of
      wanting a scratchpad is the objection `D-2026-08-11` raised against the whole harness, and it
      is the one part of that objection that still stands.
    - **`delete`** is withheld on `D-2026-08-12`'s argument, which GxP's retirement does not touch:
      a turn that cannot rewrite a `SKILL.md` but can remove it still decides what judgment the next
      turn is able to load.

    Returns:
        The tool names to hand `FilesystemMiddleware`, sorted so the prompt order is stable.
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware

    withheld = {"execute", "delete"}
    every = {tool.name for tool in FilesystemMiddleware(backend=StateBackend()).tools}
    # `cast` rather than a hand-written literal list: the *names* come from upstream so a rename is
    # caught, and `FsToolName` is upstream's own alias for exactly this set, so the annotation
    # cannot drift from the values either.
    return cast("tuple[FsToolName, ...]", tuple(sorted(every - withheld)))


def filesystem_permissions() -> list[Any]:
    """Deny-rules bounding where a turn may write, evaluated before any filesystem operation.

    The allow-list above decides which *verbs* exist; this decides where they may point. Writes are
    denied everywhere and then allowed back under the two roots that are meant to be written — the
    order matters because `FilesystemPermission` is first-match-wins, so the allows are declared
    first and the blanket deny closes the surface behind them.

    **`/skills/` is refused twice, and that is deliberate rather than redundant.** These rules are
    the outer half; `NarrowedSkillsBackend` refuses the write itself on every call. A security
    property that arrives as somebody else's default can leave the same way, so the backend keeps
    its own refusal and this states the intent where a reader looks for it.

    Returns:
        The rules to pass `create_deep_agent(permissions=…)`.
    """
    from deepagents import FilesystemPermission

    return [
        FilesystemPermission(operations=["write"], paths=[f"{SCRATCH_ROOT}**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=[f"{MEMORY_ROOT}**"], mode="allow"),
        # `/**` rather than `**`: upstream validates that a rule's path is absolute and rejects a
        # bare glob outright, which is a better default than silently matching nothing.
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
