"""Config-selectable ELN adapters: one place mapping a stable name to each adapter.

Why this exists: adapters were constructed by class name inside the workflows — the durable
sync picked `JsonExportAdapter` directly, and the memory jobs hardcoded the list of both — so
switching or adding an ELN source meant editing workflow code (G3/G6 friction the admin audit
flagged). This registry maps a stable config name to each `ElnAdapter`, so the durable sync's
source becomes the `CHEMCLAW_ELN_SYNC_ADAPTER` setting and the corpus readers get every
registered source without knowing its class. Adding a source is one entry here, nowhere else.
"""

from collections.abc import Callable

from eln.adapter import ElnAdapter
from eln.json_adapter import JsonExportAdapter
from eln.ord_adapter import OrdJsonAdapter

# Stable config name -> adapter factory. The keys are exactly the values
# `CHEMCLAW_ELN_SYNC_ADAPTER` accepts; each factory builds an adapter reading its configured
# export directory.
ELN_ADAPTERS: dict[str, Callable[[], ElnAdapter]] = {
    "json": JsonExportAdapter,
    "ord": OrdJsonAdapter,
}


def make_eln_adapter(name: str) -> ElnAdapter:
    """Return the ELN adapter registered under `name`, or raise listing the valid names.

    Raised as `ValueError` so a misconfigured adapter name fails fast (and, inside a Temporal
    activity, is treated as non-retryable bad config rather than looping).
    """
    factory = ELN_ADAPTERS.get(name)
    if factory is None:
        valid = ", ".join(sorted(ELN_ADAPTERS))
        raise ValueError(f"unknown ELN adapter {name!r}; valid names: {valid}")
    return factory()


def all_eln_adapters() -> list[ElnAdapter]:
    """Every registered ELN adapter — the full source set the corpus readers ingest.

    The memory jobs reason over the union of all sources, so they take the whole registry;
    a future source added above is picked up here with no change to those jobs.
    """
    return [factory() for factory in ELN_ADAPTERS.values()]
