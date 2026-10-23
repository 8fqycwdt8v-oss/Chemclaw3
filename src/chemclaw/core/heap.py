"""Freeze the heap a process built while starting, so a full collection stops walking it.

A generation-2 collection examines every tracked object and holds the interpreter lock while it
does; on a warmed front door (imports, profiles, compiled graphs) that is the whole startup heap,
and a stall of the event loop whichever thread triggered it. Almost all of those objects live for
the whole process, so `gc.freeze()` moves them to the permanent generation the collector skips.
Collected first, so startup's own cyclic garbage is freed rather than frozen with the rest. Only
the cyclic collector is affected: reference counting still frees anything frozen that dies.
"""

import gc
import logging

logger = logging.getLogger(__name__)


def freeze_startup_heap(component: str) -> None:
    """Collect, then freeze every object this process holds.

    Called once, when `component` has finished starting and is about to serve. Callers reach it
    through the module (`heap.freeze_startup_heap`), so the test suite, whose processes run many
    lifespans, can replace it in one place.
    """
    gc.collect()
    gc.freeze()
    logger.info(
        "%s froze %d startup objects out of the cyclic collector", component, gc.get_freeze_count()
    )
