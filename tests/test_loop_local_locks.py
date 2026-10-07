"""No `asyncio` synchronization primitive is constructed at import time in `src/`.

`asyncio.Lock.acquire` binds its loop lazily and only when contended, so a module-level lock binds
to the first loop that contends it. A second loop in the same process (e.g. repeated
`pytest.main()` calls under `mutmut`) then raises from the waiter while the holder keeps the lock,
and the symptom is a hang with no exception. `core/aio.LoopLocalLock` resolves per running loop,
and this rule over the tree catches any new import-time primitive.
"""

import ast
import asyncio
import gc
from pathlib import Path
from weakref import WeakKeyDictionary

import pytest

from chemclaw.core.aio import LoopLocalLock

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# Every `asyncio` primitive whose behaviour depends on which loop is running. `Lock`, `Event`,
# `Condition`, `Semaphore` and `BoundedSemaphore` all inherit `_LoopBoundMixin`; `Queue` holds
# futures created on the loop that first waited on it, which is the same trap one level down.
_LOOP_BOUND = frozenset(
    {
        "Lock",
        "Event",
        "Condition",
        "Semaphore",
        "BoundedSemaphore",
        "Queue",
        "LifoQueue",
        "PriorityQueue",
    }
)


def _import_time_constructions(tree: ast.Module) -> list[tuple[int, str]]:
    """Every `asyncio.<primitive>()` call in `tree` that runs when the module is imported.

    Class attributes are constructed at import too, so the walk descends everything except function
    bodies, where construction happens per call.
    """
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _LOOP_BOUND
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "asyncio"
            ):
                found.append((child.lineno, f"asyncio.{child.func.attr}"))
            visit(child)

    visit(tree)
    return found


def test_no_asyncio_primitive_is_built_at_import_time_in_src() -> None:
    """No loop-bound primitive is built at import time anywhere in `src/`.

    Derived over the whole tree so a new instance is caught. `LoopLocalLock` constructs its locks
    inside a method, so it is not an exception here.
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    modules = sorted(_SRC.rglob("*.py"))
    assert len(modules) > 100, f"only {len(modules)} modules found; this walk is measuring air"

    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        hits = _import_time_constructions(tree)
        if hits:
            offenders[str(module.relative_to(_SRC.parents[1]))] = hits

    assert not offenders, (
        f"{offenders} construct an asyncio primitive at import time. Such a primitive binds to the "
        "first event loop that *contends* on it, so a second loop in the process hangs with no "
        "exception and leaves it permanently held — use `core.aio.LoopLocalLock` instead"
    )


async def _contend(lock: asyncio.Lock | LoopLocalLock) -> None:
    """Two holders racing for `lock`, which is the only path that binds an `asyncio.Lock`."""

    async def hold() -> None:
        async with lock:
            await asyncio.sleep(0)

    await asyncio.gather(hold(), hold())


async def _solo(lock: asyncio.Lock | LoopLocalLock) -> None:
    """One holder, taking the fast path that never resolves a loop."""
    async with lock:
        pass


def test_only_contention_binds_a_module_level_lock_and_then_it_wedges() -> None:
    """Only contention binds a module-level lock to a loop, driven from both sides.

    The uncontended lock crosses two `asyncio.run` calls without complaint, which is why the hazard
    survives single-use tests. After the contended run the lock is released; what persists is the
    binding. The resulting permanent hold cannot be asserted without hanging.
    """
    uncontended = asyncio.Lock()
    asyncio.run(_solo(uncontended))
    asyncio.run(_solo(uncontended))  # no raise: the fast path never resolved a loop

    contended = asyncio.Lock()
    asyncio.run(_contend(contended))
    with pytest.raises(RuntimeError, match="bound to a different event loop"):
        asyncio.run(_contend(contended))


def test_a_loop_local_lock_serializes_in_every_loop_it_is_used_from() -> None:
    """`LoopLocalLock` survives the same sequence, and still serializes inside each loop.

    Both halves matter. Surviving is not enough — a "lock" that never blocks would also survive —
    so each loop's run asserts that the two holders did not overlap.
    """
    shared = LoopLocalLock("test lock")
    overlaps: list[int] = []

    async def contend() -> None:
        live = 0

        async def hold() -> None:
            nonlocal live
            async with shared:
                live += 1
                overlaps.append(live)
                await asyncio.sleep(0)
                live -= 1

        await asyncio.gather(hold(), hold())

    for _ in range(3):
        asyncio.run(contend())

    assert overlaps == [1] * 6, (
        f"two holders were inside the lock at once: {overlaps} — a per-loop lock must still be a "
        "lock within its own loop"
    )
    assert len(shared._locks) == 1, (
        f"{len(shared._locks)} loops' locks are retained after three sequential loops; closed ones "
        "are discarded on resolve, so a harness that opens one loop per mutant must not accumulate"
    )


def test_a_weak_mapping_could_not_have_held_these_locks() -> None:
    """Why `LoopLocalLock` prunes instead of keying a `WeakKeyDictionary` on the loop.

    A contended `asyncio.Lock` stores `_loop`, its own key, so the weak entry keeps itself alive.
    Both arms are needed, since the uncontended one releases and would make a one-sided test look
    like proof.
    """
    for body, retained in ((_solo, 0), (_contend, 3)):
        weak: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()

        async def run(body: object = body, weak: object = weak) -> None:
            lock = asyncio.Lock()
            weak[asyncio.get_running_loop()] = lock  # type: ignore[index]
            await body(lock)  # type: ignore[operator]

        for _ in range(3):
            asyncio.run(run())
        gc.collect()

        assert len(weak) == retained, (
            f"{body.__name__} left {len(weak)} of 3 entries in a WeakKeyDictionary, expected "
            f"{retained} — the contended case is the one that cannot be collected, and it is the "
            "only case this lock is for"
        )
        if retained:
            loop, lock = next(iter(weak.items()))
            assert lock._loop is loop, (  # type: ignore[attr-defined]
                "the retained entry's value no longer references its own key, so the mechanism "
                "this test records has changed and the pruning may no longer be necessary"
            )


def test_a_loop_local_lock_names_itself_when_reached_off_a_loop() -> None:
    """Synchronous use raises with the lock's name, not a bare "no running event loop"."""
    lock = LoopLocalLock("kg.git_writer's write lock")
    with pytest.raises(RuntimeError, match="kg.git_writer's write lock"):
        lock.locked()
