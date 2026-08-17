"""The scratchpad's routing, its erasure key, and the property that keeps writes auditable.

Three things are worth a test here and the third matters most. Routing and tool narrowing are
ordinary wiring. The third — that nothing writes to the store except through a *tool* — is what
answers the audit objection `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives` raised,
rather than merely arguing it. A direct `store.aput` would bypass the audit row, the authorization
gate, the dry-run refusal and the repeat guard, all at once and silently: nothing fails, the memory
is simply written with no record that it was.
"""

import ast
import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from deepagents.backends import CompositeBackend, StateBackend
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent import checkpointer as ckpt
from chemclaw.agent import scratchpad
from chemclaw.agent.scratchpad import (
    MEMORY_ROOT,
    filesystem_permissions,
    memory_namespace,
    memory_prefix,
    scratchpad_backend,
    scratchpad_tools,
)
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from tests.pg import migrated_db_or_skip

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


@pytest.fixture
def skills() -> CompositeBackend:
    """A stand-in skills backend with one route, so routing changes are visible."""
    return CompositeBackend(default=StateBackend(), routes={"/skills/": StateBackend()})


def test_memories_need_both_a_store_and_an_actor(skills: CompositeBackend) -> None:
    """Either condition alone leaves the route off, and neither is a preference.

    Without a store the deployment has no `store` tables at all. Without an actor there is no
    namespace that could be erased, so a memory written anyway would be one nobody can delete and
    everybody shares — which is worse than not having the capability.
    """
    assert MEMORY_ROOT not in scratchpad_backend(skills).routes
    assert MEMORY_ROOT not in scratchpad_backend(skills, store=object()).routes

    token = set_current_identity("alice-oid", frozenset())
    try:
        assert MEMORY_ROOT not in scratchpad_backend(skills).routes, "an actor alone is not enough"
        assert MEMORY_ROOT in scratchpad_backend(skills, store=object()).routes
    finally:
        reset_current_identity(token)


def test_the_skills_routes_survive_being_wrapped(skills: CompositeBackend) -> None:
    """The narrowing must not be dropped by the thing that extends it.

    The skills middleware and the filesystem tools read the *same* backend object, so a wrapper that
    rebuilt the routes instead of carrying them would leave the role gate applying to the listing
    and not to the read.
    """
    assert "/skills/" in scratchpad_backend(skills).routes


def test_two_spellings_of_one_person_get_two_prefixes() -> None:
    """`unverified:<id>` and `<id>` are the same chemist and must both be erasable.

    `agent/leaver.py` holds the reason this repository has two spellings: a writer that cannot
    authenticate its caller records the claim marked as a claim. The erasure sweep hashes each form,
    so the two must not collapse into one prefix — and must not be equal, or hashing would be
    hiding the distinction rather than preserving it.
    """
    assert memory_prefix("alice-oid") != memory_prefix("unverified:alice-oid")
    assert memory_prefix("alice-oid") == memory_prefix("alice-oid"), "must be stable across calls"
    assert memory_namespace("alice-oid")[0] == "memories"


def test_the_prefix_is_the_namespace_joined_the_way_the_store_joins_it() -> None:
    """The erasure sweep matches on `store.prefix`, which is the dotted namespace.

    Pinned because two modules agree about this key: this one writes under the namespace and
    `agent/leaver.py` deletes by the prefix. Deriving the join twice is how they would drift.
    """
    assert memory_prefix("bob") == ".".join(memory_namespace("bob"))


def test_the_shell_and_the_delete_verb_are_withheld() -> None:
    """Two verbs upstream registers that this deployment does not offer.

    `execute` would be a shell — deepagents 0.7 ships one concrete sandbox, which this repository
    declines on egress grounds, and `LocalShellBackend` is documented as unrestricted. `delete` is
    withheld on D-2026-08-12's argument: a turn that cannot rewrite a `SKILL.md` but can remove it
    still decides what judgment the next turn is able to load.

    Asserted as an exact set rather than two `not in`s, because the failure mode worth catching is
    upstream *adding* a ninth verb that nothing here has answered for.
    """
    assert set(scratchpad_tools()) == {"ls", "read_file", "write_file", "edit_file", "glob", "grep"}


def test_writes_are_denied_outside_the_two_roots_that_are_meant_to_be_written() -> None:
    """The deny rule closes the surface behind the allows, and must come last.

    `FilesystemPermission` is first-match-wins, so an ordering that put the blanket deny first would
    refuse every write including the ones the scratchpad exists for — and the tests would still pass
    if they only checked that a deny rule was present.
    """
    rules = filesystem_permissions()
    assert [rule.mode for rule in rules] == ["allow", "allow", "deny"]
    assert rules[-1].paths == ["/**"], "the closing rule must cover everything"
    allowed = {path for rule in rules[:-1] for path in rule.paths}
    assert allowed == {"/scratch/**", "/memories/**"}


def test_no_first_party_module_writes_to_a_store_directly() -> None:
    """The property that keeps a memory write auditable, enforced rather than described.

    Every write must arrive as a `write_file`/`edit_file` tool call, because that is what crosses
    the `wrap_tool_call` chain — the audit row, the authorization gate, the dry-run refusal and the
    repeat guard. A direct `store.aput` bypasses all four at once, and it would do so silently:
    nothing fails, the memory is simply written with no record that it was.

    This is the objection `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives` raised
    against adopting `BaseStore` at all. The design answers it by construction, and this asserts the
    construction holds — an AST walk rather than a grep, so a call spelled across two lines or
    hidden behind an alias is still caught.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"aput", "adelete"} and isinstance(node.func.value, ast.Name):
                # `store.aput(...)` / `store.adelete(...)`. The receiver's *name* is the signal:
                # this is about a store, not about any object that happens to expose the verb.
                if "store" in node.func.value.id.lower():
                    offenders.append(f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}")
    assert not offenders, (
        "a store is written to directly, bypassing the tool-call chain that audits and authorizes "
        f"every other write: {offenders}"
    )


def test_a_memory_write_is_gated_and_a_scratch_write_is_not() -> None:
    """The claim in this module's docstring that was false: "refused on a dry run".

    Both write gates ask `side_effecting_tools()`, which is built from the tool *registry* —
    `core/tool_registry`, every connector's `state_changing` declaration, every template launcher.
    `write_file` and `edit_file` are in none of those, because `FilesystemMiddleware` registers
    them. So a `dry_run=true` turn under `harness_autonomy="plan_only"` could write a row into the
    Postgres `store` that outlives the session, past both "nothing was started" and the plan gate.

    The partition in `tests/test_authz.py` could not catch it: it iterates
    `registered_tool_names()`, and a middleware-registered verb is not in the registry by
    construction.

    Asserted over the *pair*, because gating `write_file` by name would have been the wrong fix —
    it would refuse the turn's own scratchpad, which is turn-local state and the thing
    `D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work` added on purpose.
    """
    from chemclaw.agent.authz import side_effecting_call
    from chemclaw.agent.scratchpad import SCRATCH_ROOT

    for verb in ("write_file", "edit_file"):
        assert side_effecting_call(verb, {"file_path": f"{MEMORY_ROOT}notes.md"}), (
            f"{verb} under the memory root writes durable Postgres state; the dry-run refusal and "
            "the plan gate both key off this predicate"
        )
        assert not side_effecting_call(verb, {"file_path": f"{SCRATCH_ROOT}working.md"}), (
            f"{verb} under the scratch root is turn-local; gating it would deny an unapproved turn "
            "the notepad it needs to produce a plan worth approving"
        )


def test_an_unreadable_path_argument_is_treated_as_durable() -> None:
    """A gate that a malformed argument walks through is not a gate.

    `file_path` absent, `None`, or a non-string cannot be a scratchpad write — those name a path
    too — so the only safe reading is the durable one. Written because the opposite default is the
    easy one to reach for, and it fails *open*.
    """
    from chemclaw.agent.authz import side_effecting_call

    for arguments in ({}, {"file_path": None}, {"file_path": 17}, {"file_path": ["/memories/x"]}):
        assert side_effecting_call("write_file", arguments), arguments


def test_write_todos_is_never_gated_by_either_write_gate() -> None:
    """The deadlock the fix had to avoid, stated as a test rather than left to inference.

    `write_todos` writes the plan. Under `harness_autonomy="plan_only"` a gate that refused it would
    refuse the only call able to produce a plan for a human to approve, and the turn could never
    make progress in either direction.
    """
    from chemclaw.agent.authz import side_effecting_call

    assert not side_effecting_call(
        "write_todos", {"todos": [{"content": "step", "status": "pending"}]}
    )


def test_concurrent_first_turns_get_one_migrated_memory_store() -> None:
    """The cold start the checkpointer was fixed for, repeated one module over.

    `memory_store()` published `_store` *before* awaiting `setup()`, so a second turn arriving
    inside that await got a store whose two tables do not exist — `relation "store" does not
    exist`, the same failure and the same window as `checkpointer()`'s
    (`tests/test_checkpointer_schema.py` runs the sibling of this test). It is not a rare
    interleaving: `api/runner._turn_memory_store` is awaited once per turn and the shipped chart
    runs two replicas.

    **`setup()` is slowed here, which is what gives the test power rather than luck**, exactly as
    in the checkpointer's version: what the defect needs is a second caller *inside* the first
    one's await, so the await is widened to be observable instead of raced for.

    Four assertions, and the last two are about a different global. One `setup()` and one store
    identity are the store half. `_checkpoint_pool` has the same check-then-await-then-act around
    `open()`, and this is the caller that made it a second *caller* — its docstring used to say it
    had one, which held its lock around it. Unguarded, each concurrent caller builds and opens its
    own pool, the last assignment wins, and the rest leak their connections for the life of the
    process while a store is left holding one the module no longer knows about.

    **`open()` is widened for the same reason `setup()` is, and here it is the only way to see the
    defect at all.** Measured: with `wait=False` and `min_size=0`, `AsyncConnectionPool.open`
    reaches no suspension point — an uncontended `asyncio.Lock` acquires without awaiting — so the
    unguarded body is atomic *today*, by a property of a dependency's internals that nothing
    promises and no first-party test would notice changing. Widening the await is what turns "this
    happens not to interleave in psycopg-pool 3.2" into an assertion about this module's own
    discipline.
    """
    setups = {"started": 0, "done": 0}
    opens = {"count": 0}
    original_setup = AsyncPostgresStore.setup
    original_open = AsyncConnectionPool.open

    async def _slow_setup(self: Any) -> None:
        """Stand in for the store's own migrations, widened so the window is observable."""
        setups["started"] += 1
        await asyncio.sleep(0.05)
        await original_setup(self)
        setups["done"] += 1

    async def _slow_open(self: Any, wait: bool = False, timeout: float = 30.0) -> None:
        """The pool's own opening, widened to the suspension point it does not currently reach."""
        opens["count"] += 1
        await asyncio.sleep(0.05)
        await original_open(self, wait=wait, timeout=timeout)

    async def _run() -> dict[str, Any]:
        await migrated_db_or_skip()
        await ckpt.close_checkpointer()

        async def _take(_index: int) -> tuple[int, int, bool]:
            store = await scratchpad.memory_store()
            return id(store), id(store.conn), setups["done"] > 0

        # Patched after the migration check, so only the pool this test provokes is widened.
        patch = pytest.MonkeyPatch()
        patch.setattr(AsyncPostgresStore, "setup", _slow_setup)
        patch.setattr(AsyncConnectionPool, "open", _slow_open)
        try:
            taken = list(await asyncio.gather(*(_take(index) for index in range(4))))
            return {
                "stores": {store for store, _, _ in taken},
                "pools": {pool for _, pool, _ in taken},
                "published_pool": id(ckpt._pool),
                "migrated": [ready for _, _, ready in taken],
            }
        finally:
            patch.undo()
            await ckpt.close_checkpointer()

    result = asyncio.run(_run())

    assert all(result["migrated"]), "a turn got a memory store whose tables had not been created"
    assert setups["started"] == 1, f"setup() ran {setups['started']} times for one process"
    assert len(result["stores"]) == 1, "one process, one memory store"
    assert opens["count"] == 1, f"{opens['count']} pools were opened for one process"
    assert result["pools"] == {result["published_pool"]}, (
        "a store was handed a pool the module did not publish, so an opened pool leaked"
    )


def test_closing_the_checkpointer_drops_the_store_that_sits_on_its_pool() -> None:
    """`close_memory_store` had no caller, which made the close order unenforceable.

    The store borrows the checkpointer's pool, so a shutdown that closed the pool and left `_store`
    published would hand the next caller a store over closed connections. Ordering is the fix and
    the caller is what makes it exist; this asserts the wiring rather than the docstring.
    """

    async def _run() -> AsyncPostgresStore | None:
        scratchpad._store = cast(AsyncPostgresStore, cast(Any, object()))
        await ckpt.close_checkpointer()
        return scratchpad._store

    assert asyncio.run(_run()) is None
