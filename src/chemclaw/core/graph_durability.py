"""A graph with no checkpointer has no checkpoint to wait for, whatever its caller's run asks.

A run's durability mode is written into its config and read by every graph started under it. The
turn runs `"sync"` (`api/graph_stream.TURN_DURABILITY`), and a graph compiled with
`checkpointer=False` (a helper, a peer, the evidence fan-out) that inherits it waits on a write it
never started and fails with an `AttributeError` inside the tool that called it. Such a graph is
given the one resolution upstream lacks: no saver means `"async"`.

Invariant: only the resolved durability changes, and only for a graph whose resolved checkpointer
is `None`; a graph that has a saver, own or inherited, keeps the mode it was asked for.
"""

from typing import Any, TypeVar

_G = TypeVar("_G")

#: One subclass per upstream class, so the class swap is repeatable and cheap.
_SUBCLASSES: dict[type, type] = {}


def ignoring_inherited_durability(graph: _G) -> _G:
    """`graph`, which must be compiled without a checkpointer, made immune to an inherited "sync".

    Changes the object's class in place (a subclass overriding upstream's `_defaults`), so every
    way of reaching the graph, as a node or through `ainvoke`, resolves the same. Idempotent.
    """
    base = type(graph)
    if base in _SUBCLASSES.values():
        return graph
    if base not in _SUBCLASSES:

        def _defaults(self: Any, config: Any, **kwargs: Any) -> tuple[Any, ...]:
            resolved = base._defaults(self, config, **kwargs)  # type: ignore[attr-defined]
            *head, durability = resolved
            checkpointer = head[4]
            return (*head, "async" if checkpointer is None and durability == "sync" else durability)

        _SUBCLASSES[base] = type(base.__name__, (base,), {"_defaults": _defaults})
    graph.__class__ = _SUBCLASSES[base]
    return graph
