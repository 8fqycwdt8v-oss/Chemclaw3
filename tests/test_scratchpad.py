"""The scratchpad's routing, its erasure key, and the two properties that bound a turn's writes.

- Nothing writes to the store except through a tool, so every write crosses the audit row, the
  authorization gate, the dry-run refusal and the repeat guard.
- The deny rules reach the middleware that enforces them, driven through a compiled graph.
"""

import ast
import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.runnables import RunnableConfig
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent import checkpointer as ckpt
from chemclaw.agent import scratchpad
from chemclaw.agent.scratchpad import (
    MEMORY_ROOT,
    STORE_TABLES,
    filesystem_permissions,
    memory_namespace,
    memory_prefix,
    scratchpad_backend,
    scratchpad_tools,
)
from chemclaw.agent.skill_access import SkillNarrowing
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.metrics import METRICS
from tests.pg import migrated_db_or_skip

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


#: A narrowing that narrows nothing — what these tests assert *around*.
#:
#: `scratchpad_backend` requires the narrowing as a keyword so a mount cannot silently get none;
#: these route tests state the permissive answer explicitly.
_ALL = SkillNarrowing.permissive()


@pytest.fixture
def skills() -> CompositeBackend:
    """A stand-in skills backend with one route, so routing changes are visible."""
    return CompositeBackend(default=StateBackend(), routes={"/skills/": StateBackend()})


def test_memories_need_both_a_store_and_an_actor(skills: CompositeBackend) -> None:
    """Memories need both a store and an actor.

    Without an actor there is no erasable namespace, so a memory would be undeletable and shared.
    """
    assert MEMORY_ROOT not in scratchpad_backend(skills, permits=_ALL).routes
    assert MEMORY_ROOT not in scratchpad_backend(skills, store=object(), permits=_ALL).routes

    token = set_current_identity("alice-oid", frozenset())
    try:
        assert MEMORY_ROOT not in scratchpad_backend(skills, permits=_ALL).routes, (
            "an actor alone is not enough"
        )
        assert MEMORY_ROOT in scratchpad_backend(skills, store=object(), permits=_ALL).routes
    finally:
        reset_current_identity(token)


def test_the_skills_routes_survive_being_wrapped(skills: CompositeBackend) -> None:
    """The skills routes survive being wrapped, so the role gate also applies to reads."""
    assert "/skills/" in scratchpad_backend(skills, permits=_ALL).routes


def test_two_spellings_of_one_person_get_two_prefixes() -> None:
    """`unverified:<id>` and `<id>` get distinct prefixes, so erasure can reach both."""
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
    """The shell (`execute`) and the `delete` verb are withheld.

    A turn that cannot rewrite a `SKILL.md` must not be able to remove one either. Asserted as an
    exact set, so a verb added upstream fails here until answered for.
    """
    assert set(scratchpad_tools()) == {"ls", "read_file", "write_file", "edit_file", "glob", "grep"}


def test_the_verb_list_is_computed_once() -> None:
    """The verb list is computed once, since `agent/tool_framing.py` asks for it per tool call.

    Asserted by identity, alongside the value test above so the cache is not stably wrong.
    `subagent_tool_names` is cached the same way; neither depends on connector discovery.
    """
    assert scratchpad_tools() is scratchpad_tools(), (
        "scratchpad_tools rebuilds a FilesystemMiddleware on every call, and "
        "agent/tool_framing.frame_connector_results calls it once per tool call"
    )


def test_writes_are_denied_outside_the_two_roots_that_are_meant_to_be_written() -> None:
    """Writes are denied outside the two writable roots, and the deny rule comes last.

    `FilesystemPermission` is first-match-wins, so a deny placed first would refuse every write.
    """
    rules = filesystem_permissions()
    assert [rule.mode for rule in rules] == ["allow", "allow", "deny"]
    assert rules[-1].paths == ["/**"], "the closing rule must cover everything"
    allowed = {path for rule in rules[:-1] for path in rule.paths}
    assert allowed == {"/scratch/**", "/memories/**"}


def test_a_write_out_of_bounds_is_refused_by_the_graph_that_really_compiles() -> None:
    """A write out of bounds is refused by the graph that really compiles.

    This repository supplies its own `FilesystemMiddleware`, which replaces upstream's in place, so
    the permissions must be carried onto it. Both directions are asserted from one run.
    """
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.state import turn_config, turn_input
    from tests.fakes_langgraph import ScriptedChatModel

    graph = build_langgraph_agent(
        model=ScriptedChatModel(
            [
                {"name": "write_file", "args": {"file_path": "/scratch/ok.md", "content": "keep"}},
                {"name": "write_file", "args": {"file_path": "/outside/evil.md", "content": "no"}},
                "done",
            ]
        ),
        audit_sink=NullAuditSink(),
    )

    final = asyncio.run(graph.ainvoke(turn_input("write two files"), config=turn_config()))
    written = set(final.get("files") or {})

    assert "/scratch/ok.md" in written, "the scratchpad's own root was refused"
    assert "/outside/evil.md" not in written, (
        "a write landed outside the two roots the deny rule exists to close — the permission list "
        "is not reaching the FilesystemMiddleware this repository substitutes"
    )
    # Matched on the path rather than on `status`: `tool_authz.answered_failure` rewrites a
    # returned failure's status to `"success"` before anything downstream sees it, deliberately, so
    # a provider does not read `is_error` as "retry this".
    refusal = next(
        message
        for message in final["messages"]
        if getattr(message, "name", None) == "write_file" and "/outside/evil.md" in message.text
    )
    assert "permission denied" in refusal.text, (
        f"the model was told something other than a refusal: {refusal.text!r}"
    )


def test_no_first_party_module_writes_to_a_store_directly() -> None:
    """No first-party module writes to a store directly.

    Every write must arrive as a `write_file`/`edit_file` tool call to cross the tool-call chain.
    An AST walk catches aliases and multi-line calls. The one exemption is
    `agent/scratchpad.BoundedStoreBackend`, whose eviction runs inside the tool's own call; a
    second module acquiring the verb fails.
    """
    allowed = {"agent/scratchpad.py"}
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.relative_to(_SRC).as_posix() in allowed:
            continue
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
    """A memory write is gated and a scratch write is not.

    `write_file`/`edit_file` are registered by middleware, not the tool registry, so
    `side_effecting_tools()` must classify durable memory writes by path; gating the name would
    refuse the turn's own scratchpad.
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
    """An absent, `None` or non-string path is treated as durable, so the gate fails closed."""
    from chemclaw.agent.authz import side_effecting_call

    for arguments in ({}, {"file_path": None}, {"file_path": 17}, {"file_path": ["/memories/x"]}):
        assert side_effecting_call("write_file", arguments), arguments


def test_write_todos_is_never_gated_by_either_write_gate() -> None:
    """`write_todos` is never gated, or `plan_only` could never produce a plan to approve."""
    from chemclaw.agent.authz import side_effecting_call

    assert not side_effecting_call(
        "write_todos", {"todos": [{"content": "step", "status": "pending"}]}
    )


def test_concurrent_first_turns_get_one_migrated_memory_store() -> None:
    """Concurrent first turns get one migrated memory store and one checkpoint pool.

    Publishing `_store` before `setup()` finishes hands a second turn a store with no tables, and an
    unguarded `_checkpoint_pool` open leaks pools. `setup()` and `open()` are slowed so a second
    caller lands inside the first's await; without that, `open()` happens not to yield today and
    the race would be invisible.
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


def test_what_an_ended_loop_left_is_replaced_on_the_next_loop() -> None:
    """A store and a saver built on a loop that has ended are rebuilt, not handed to the next loop.

    They sit on a pool pinned to the loop that opened it, so a later `asyncio.run` that reused them
    failed with "Event loop is closed" on its first query. The first run ends without closing
    anything, as a script or a test that never called `close_checkpointer` does.
    """
    from chemclaw.agent.state import turn_config

    namespace = scratchpad.memory_namespace("ended-loop-probe")
    config = cast(RunnableConfig, turn_config("ended-loop-probe"))

    async def _first() -> tuple[object, object]:
        await migrated_db_or_skip()
        await ckpt.close_checkpointer()
        store = await scratchpad.memory_store()
        await store.asearch(namespace, limit=1)
        return store, await ckpt.checkpointer()

    async def _second() -> tuple[object, object]:
        try:
            store = await scratchpad.memory_store()
            await store.asearch(namespace, limit=1)
            saver = await ckpt.checkpointer()
            await saver.aget_tuple(config)
            return store, saver
        finally:
            await ckpt.close_checkpointer()

    first = asyncio.run(_first())
    second = asyncio.run(_second())

    assert second[0] is not first[0], "the store of an ended loop was handed to the next one"
    assert second[1] is not first[1], "the saver of an ended loop was handed to the next one"


def test_closing_the_checkpointer_drops_the_store_that_sits_on_its_pool() -> None:
    """Closing the checkpointer drops the store that sits on its pool.

    Otherwise the next caller would get a store over closed connections.
    """

    async def _run() -> AsyncPostgresStore | None:
        scratchpad._store = cast(AsyncPostgresStore, cast(Any, object()))
        await ckpt.close_checkpointer()
        return scratchpad._store

    assert asyncio.run(_run()) is None


def test_the_store_table_list_is_derived_from_upstream_not_asserted_against_itself() -> None:
    """`STORE_TABLES` matches the tables upstream's store migrations create.

    A new store table would otherwise escape the erasure sweep and the retention register. The
    `*_migrations` ledgers are excluded by name, though today they are created inside `setup()`
    where this regex cannot see them; a table added that way would also escape.
    """
    import re

    from langgraph.store.postgres import base

    created = {
        match.group(1)
        for statement in (*base.MIGRATIONS, *base.VECTOR_MIGRATIONS)
        if (match := re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", str(statement)))
    }
    assert created, "no CREATE TABLE found in the store's migrations — the parse is broken"

    ledgers = {"store_migrations", "vector_migrations"}
    assert set(STORE_TABLES) == created - ledgers, (
        "the memory store creates a table the erasure sweep does not clear (or clears one it does "
        "not create): " + str(sorted((created - ledgers) ^ set(STORE_TABLES)))
    )


def test_the_memory_store_is_bounded_per_namespace() -> None:
    """The memory store is bounded per namespace.

    Written through `BoundedStoreBackend.awrite`, the function `write_file` reaches. The least
    recently updated files are evicted, so the newest survive.
    """
    cap = 5

    async def _run() -> tuple[int, list[str]]:
        await migrated_db_or_skip()
        patch = pytest.MonkeyPatch()
        patch.setattr(settings, "agent_memory_max_files", cap)
        try:
            store = await scratchpad.memory_store()
            namespace = scratchpad.memory_namespace("bounded-probe")
            backend = scratchpad.BoundedStoreBackend(
                namespace=lambda _runtime: namespace, store=store
            )
            for index in range(cap * 3):
                await backend.awrite(f"/memories/note-{index:02d}.md", "x" * 5_000)
            held = await store.asearch(namespace, limit=100)
            for item in held:
                await store.adelete(namespace, item.key)
            return len(held), sorted(item.key for item in held)
        finally:
            patch.undo()
            await ckpt.close_checkpointer()

    count, keys = asyncio.run(_run())
    assert count == cap, f"{count} memories survived a {cap}-file cap"
    # The last `cap` written, so the eviction took the least recently updated rather than a
    # convenient set.
    assert keys == sorted(f"/memories/note-{index:02d}.md" for index in range(cap * 2, cap * 3)), (
        keys
    )


def test_evicting_a_memory_is_counted_and_logged() -> None:
    """Evicting a memory is counted and logged with the file names.

    The counter shows whether the cap binds; the WARNING is the only trace of which memory went.
    """
    cap = 2

    async def _run() -> tuple[float, int]:
        await migrated_db_or_skip()
        patch = pytest.MonkeyPatch()
        patch.setattr(settings, "agent_memory_max_files", cap)
        try:
            store = await scratchpad.memory_store()
            namespace = scratchpad.memory_namespace("counted-probe")
            backend = scratchpad.BoundedStoreBackend(
                namespace=lambda _runtime: namespace, store=store
            )
            before = METRICS.value("chemclaw_memory_evictions_total")
            for index in range(cap + 3):
                await backend.awrite(f"/memories/n{index}.md", "hello")
            after = METRICS.value("chemclaw_memory_evictions_total")
            held = await store.asearch(namespace, limit=100)
            for item in held:
                await store.adelete(namespace, item.key)
            return after - before, len(held)
        finally:
            patch.undo()
            await ckpt.close_checkpointer()

    evicted, held = asyncio.run(_run())
    assert held == cap
    assert evicted == 3, f"{evicted} evictions counted for three files over the cap"


def test_the_memories_route_the_wiring_installs_is_the_bounded_one(
    skills: CompositeBackend,
) -> None:
    """The memories route `scratchpad_backend` installs is the bounded one.

    The cap tests construct their own backend, so only this asserts the production wiring.
    """
    token = set_current_identity("wiring-probe", frozenset())
    try:
        route = scratchpad_backend(skills, store=object(), permits=_ALL).routes[MEMORY_ROOT]
    finally:
        reset_current_identity(token)
    assert isinstance(route, scratchpad.BoundedStoreBackend), (
        f"the /memories/ route is a {type(route).__name__}, so nothing bounds a namespace "
        "written through the shipped wiring"
    )


def test_eviction_takes_the_least_recently_updated_even_far_past_the_cap() -> None:
    """Eviction takes the least recently updated even far past the cap.

    `asearch` returns newest first, so evicting the oldest of one page would delete a middle band.
    The namespace is seeded more than `_EVICTION_PAGE` past its cap via `store.aput`, so eviction
    runs once, on the write under test.
    """
    cap = 5
    seeded = scratchpad._EVICTION_PAGE + cap + 20

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        patch = pytest.MonkeyPatch()
        patch.setattr(settings, "agent_memory_max_files", cap)
        try:
            store = await scratchpad.memory_store()
            namespace = scratchpad.memory_namespace("evict-order-probe")
            for stale in await store.asearch(namespace, limit=1000):
                await store.adelete(namespace, stale.key)
            for index in range(seeded):
                await store.aput(namespace, f"/memories/f-{index:03d}.md", {"content": "x"})
            backend = scratchpad.BoundedStoreBackend(
                namespace=lambda _runtime: namespace, store=store
            )
            await backend.awrite("/memories/newest.md", "y")
            held = sorted(item.key for item in await store.asearch(namespace, limit=1000))
            for item in await store.asearch(namespace, limit=1000):
                await store.adelete(namespace, item.key)
            return held
        finally:
            patch.undo()
            await ckpt.close_checkpointer()

    survivors = asyncio.run(_run())
    expected = sorted(
        [
            "/memories/newest.md",
            *(f"/memories/f-{i:03d}.md" for i in range(seeded - cap + 1, seeded)),
        ]
    )
    assert survivors == expected, survivors


def test_a_memory_edit_that_would_duplicate_itself_is_refused() -> None:
    """A memory edit that would duplicate itself is refused.

    A tool killed mid-call is re-run on resume, and a `/memories/` edit is a read-modify-write
    outside the checkpoint. An insert-under-anchor edit (replacement contains `old_string`) is
    refused; a plain substitution still works.
    """

    async def _run() -> tuple[str, str, str]:
        await migrated_db_or_skip()
        store = await scratchpad.memory_store()
        namespace = scratchpad.memory_namespace("edit-replay-probe")
        backend = scratchpad.BoundedStoreBackend(namespace=lambda _runtime: namespace, store=store)
        path = "/memories/findings.md"
        try:
            await backend.awrite(path, "# Notes\n\n## Findings\n- first item\n")
            first = await backend.aedit(path, "## Findings", "## Findings\n- added")
            replay = await backend.aedit(path, "## Findings", "## Findings\n- added")
            after = await backend._current_content(path)
            return str(first.error or ""), str(replay.error or ""), str(after or "")
        finally:
            for item in await store.asearch(namespace, limit=100):
                await store.adelete(namespace, item.key)
            await ckpt.close_checkpointer()

    first_error, replay_error, content = asyncio.run(_run())

    assert not first_error, f"the first application must succeed; got {first_error!r}"
    assert content.count("- added") == 1, (
        f"the replay inserted a second copy into durable memory: {content!r}"
    )
    assert "already applied" in replay_error, (
        f"the replay must be refused with a reason, not silently applied; got {replay_error!r}"
    )


def test_a_plain_memory_substitution_is_not_refused_by_that_guard() -> None:
    """The negative control: a replacement that does not contain its anchor is idempotent already.

    Without this the guard above could be satisfied by refusing every edit, which would be a worse
    bug than the one it fixes — a memory nobody can correct.
    """

    async def _run() -> tuple[str, str]:
        await migrated_db_or_skip()
        store = await scratchpad.memory_store()
        namespace = scratchpad.memory_namespace("edit-plain-probe")
        backend = scratchpad.BoundedStoreBackend(namespace=lambda _runtime: namespace, store=store)
        path = "/memories/solvent.md"
        try:
            await backend.awrite(path, "prefers toluene for the coupling\n")
            result = await backend.aedit(path, "toluene", "2-MeTHF")
            after = await backend._current_content(path)
            return str(result.error or ""), str(after or "")
        finally:
            for item in await store.asearch(namespace, limit=100):
                await store.adelete(namespace, item.key)
            await ckpt.close_checkpointer()

    error, content = asyncio.run(_run())

    assert not error, f"a plain substitution must still apply; got {error!r}"
    assert "2-MeTHF" in content and "toluene" not in content


def test_the_migrate_role_creates_the_store_tables_it_is_about_to_grant_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chemclaw.agent.store_setup` creates the store tables before the grants run.

    Driven on a real database: it creates the tables, a second run applies nothing, and it does
    nothing where the deployment keeps no store.
    """
    from chemclaw.agent.store_setup import create_store_tables

    async def _run() -> tuple[bool, bool, bool, set[str]]:
        await migrated_db_or_skip()
        monkeypatch.setattr(settings, "agent_memory_enabled", True)
        monkeypatch.setattr(settings, "session_store", "postgres")
        first = await create_store_tables()
        again = await create_store_tables()

        import psycopg

        from chemclaw.core.migrate import migration_dsn

        async with await psycopg.AsyncConnection.connect(migration_dsn()) as conn:
            rows = await (
                await conn.execute(
                    "SELECT tablename FROM pg_tables WHERE tablename = ANY(%s)",
                    (list(scratchpad.STORE_TABLES),),
                )
            ).fetchall()

        monkeypatch.setattr(settings, "agent_memory_enabled", False)
        skipped = await create_store_tables()
        return first, again, skipped, {str(row[0]) for row in rows}

    created, idempotent, skipped, tables = asyncio.run(_run())

    assert created and idempotent, "the step did not run, or a second run was not a no-op"
    assert not skipped, "a deployment that keeps no store was given the tables anyway"
    # `store_vectors` is not among them and must not be: `setup()` builds it only for a store
    # constructed with an `index_config`, and none is passed. `STORE_TABLES` names both because
    # erasure must reach both wherever a site does set one.
    assert "store" in tables, f"the grants would find no `store` to grant on: {tables}"


# ------------------------------------------------ a turn's own files: the per-write cap and expiry
#
# Driven through the compiled graph, since `write_file` reaches `StateBackend` as a channel write.


def _run_scripted(calls: list[Any], seed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the compiled agent over scripted tool calls and return its final state."""
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.state import turn_config, turn_input
    from tests.fakes_langgraph import ScriptedChatModel

    graph = build_langgraph_agent(
        model=ScriptedChatModel([*calls, "done"]), audit_sink=NullAuditSink()
    )
    payload = {**turn_input("work in the scratchpad"), **({"files": seed} if seed else {})}
    return cast(dict[str, Any], asyncio.run(graph.ainvoke(payload, config=turn_config())))


def _answer(final: dict[str, Any], tool: str, path: str) -> str:
    """What the model was told for the one call to `tool` naming `path`."""
    return str(
        next(
            message.text
            for message in final["messages"]
            if getattr(message, "name", None) == tool and path in message.text
        )
    )


def test_a_scratch_write_past_the_cap_is_refused_and_one_at_the_cap_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused whole — never truncated — and the boundary is inclusive, both from one run."""
    monkeypatch.setattr(settings, "agent_scratch_file_max_chars", 100)
    final = _run_scripted(
        [
            {"name": "write_file", "args": {"file_path": "/scratch/fits.md", "content": "a" * 100}},
            {"name": "write_file", "args": {"file_path": "/scratch/big.md", "content": "b" * 101}},
        ]
    )
    files = final.get("files") or {}
    assert "/scratch/fits.md" in files, "a file exactly at the cap was refused"
    assert "/scratch/big.md" not in files, (
        "a caller's own write past agent_scratch_file_max_chars reached the files channel — "
        "nothing bounds what StateBackend writes directly"
    )
    refusal = _answer(final, "write_file", "/scratch/big.md")
    assert "agent_scratch_file_max_chars" in refusal and "Nothing was truncated" in refusal, refusal


def test_an_edit_that_would_grow_a_file_past_the_cap_is_refused_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit is how a file walks past a write-only bound, one append at a time."""
    monkeypatch.setattr(settings, "agent_scratch_file_max_chars", 100)
    final = _run_scripted(
        [
            {"name": "write_file", "args": {"file_path": "/scratch/log.md", "content": "head\n"}},
            {
                "name": "edit_file",
                "args": {
                    "file_path": "/scratch/log.md",
                    "old_string": "head\n",
                    "new_string": "head\n" + "c" * 200,
                },
            },
            {
                "name": "edit_file",
                "args": {"file_path": "/scratch/log.md", "old_string": "head", "new_string": "top"},
            },
        ]
    )
    content = (final.get("files") or {})["/scratch/log.md"]["content"]
    assert "c" not in content, "an edit grew the file past the cap"
    assert content.startswith("top"), "an edit under the cap was refused along with the one over it"


def test_a_memory_write_past_the_cap_is_refused_before_it_lands() -> None:
    """The `/memories/` route takes the same `write_file`, so it is held to the same number."""
    from langgraph.store.memory import InMemoryStore

    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "agent_scratch_file_max_chars", 50)
    try:
        store = InMemoryStore()
        namespace = scratchpad.memory_namespace("cap-probe")
        backend = scratchpad.BoundedStoreBackend(namespace=lambda _runtime: namespace, store=store)
        big = asyncio.run(backend.awrite("/memories/big.md", "x" * 51))
        small = asyncio.run(backend.awrite("/memories/small.md", "x" * 50))
        grown = asyncio.run(backend.aedit("/memories/small.md", "x", "yy", replace_all=True))
        held = {item.key for item in store.search(namespace, limit=10)}
    finally:
        patch.undo()
    assert big.error and "agent_scratch_file_max_chars" in big.error, big
    assert small.error is None, small
    assert grown.error and "agent_scratch_file_max_chars" in grown.error, grown
    assert held == {"/memories/small.md"}, held


def _file(text: str, days_ago: float | None) -> dict[str, Any]:
    """A `files` entry in upstream's shape, last written `days_ago` (or undated when `None`)."""
    from datetime import UTC, datetime, timedelta

    entry: dict[str, Any] = {"content": text, "encoding": "utf-8"}
    if days_ago is not None:
        stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        entry["created_at"] = entry["modified_at"] = stamp
    return entry


def test_a_file_past_the_retention_window_is_removed_at_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped 90 days, on either side of it, plus a file that carries no date at all."""
    assert settings.agent_scratch_retention_days == 90, "the owner's default moved"
    final = _run_scripted(
        [],
        seed={
            "/scratch/stale.md": _file("old", 91),
            "/scratch/fresh.md": _file("new", 89),
            "/scratch/undated.md": _file("legacy", None),
        },
    )
    files = set(final.get("files") or {})
    assert "/scratch/stale.md" not in files, (
        "a file not written for 91 days survived a 90-day window"
    )
    assert {"/scratch/fresh.md", "/scratch/undated.md"} <= files, files


def test_a_retention_of_zero_keeps_every_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 is "keep for ever", and it is a value a deployment sets rather than the default."""
    monkeypatch.setattr(settings, "agent_scratch_retention_days", 0)
    final = _run_scripted([], seed={"/scratch/ancient.md": _file("old", 3_650)})
    assert "/scratch/ancient.md" in (final.get("files") or {})
