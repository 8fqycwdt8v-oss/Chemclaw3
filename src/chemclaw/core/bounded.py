"""One bounded LRU map for every "keyed by an unbounded identity" cache in the tree (S2).

A map keyed by session id, user oid or principal is the codebase's recurring unbounded-growth bug —
it was fixed independently four times, each with its own hand-rolled `OrderedDict` loop: the front
door's live sessions (`api/state.py`), the budget counters (`api/budget.py`), the rate limiter's
token buckets (`api/rate_limit.py`) and the attachment store (`agent/attachments.py`). Four copies
of "move to end, pop the oldest past capacity" is three too many, and each drifted its own
subtleties (a live capacity read here, an eviction pin there). This is the one implementation they
now share; the subtleties become explicit parameters instead of copy-paste variance.

**What deliberately does *not* use this: `core/metrics.py`'s label-series cap.** That cap is
*refuse-new*, not evict-oldest — past 64 label-sets it drops new series and keeps existing ones,
because a metric series is an accumulator whose value is the whole point of keeping it. Folding it
into an LRU would convert cardinality *protection* into cardinality *churn*: an attacker minting
label values would rotate real series out and reset their counts. If you are tempted to migrate it
here, that is the reason not to.

Semantics, stated once so the four callers cannot drift again:

- `get` marks the entry most-recently-used; `peek` reads without marking (for pure reads that must
  not extend an entry's life, e.g. listing a session's attachments).
- `put` inserts or refreshes the entry as most-recently-used, then evicts least-recently-used
  entries past capacity. The entry just put is never the victim — its value is being handed to the
  caller, and dropping it would leave a live handle writing outside the cache.
- `capacity` may be a fixed int or a zero-argument callable, so a config-backed bound stays
  live/ENV-overridable without the caller re-reading settings itself.
- `pinned` (optional) names keys eviction must skip right now — consulted at eviction time, not
  stored per entry, so a pin needs no bookkeeping to clear. When every candidate is pinned the map
  briefly holds more than `capacity`; the caller that passes a pin has decided that honoring the
  bound by corrupting an in-use entry would be the wrong trade.

Not thread-safe by itself: callers that run under threads (the budget tracker) hold their own lock,
exactly as they did around their private `OrderedDict`.
"""

from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class BoundedLru(Generic[K, V]):
    """An insertion-capped map that evicts its least-recently-used entry past capacity."""

    def __init__(
        self,
        capacity: int | Callable[[], int],
        *,
        pinned: Callable[[K], bool] | None = None,
    ) -> None:
        """Create the map with `capacity` (fixed, or a callable read at each eviction pass).

        `pinned` says which keys must not be evicted right now (default: none) — see the module
        docstring for the over-capacity trade that implies.
        """
        self._capacity: Callable[[], int] = capacity if callable(capacity) else (lambda: capacity)
        self._pinned: Callable[[K], bool] = pinned if pinned is not None else (lambda _key: False)
        self._entries: OrderedDict[K, V] = OrderedDict()

    def __len__(self) -> int:
        """How many entries are held — what a caller's gauge or bound assertion reads."""
        return len(self._entries)

    def __contains__(self, key: K) -> bool:
        """Whether `key` is currently held, without touching its recency."""
        return key in self._entries

    def get(self, key: K) -> V | None:
        """Return the entry for `key`, marking it most-recently-used, or None."""
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def peek(self, key: K) -> V | None:
        """Return the entry for `key` without marking it used, or None.

        For reads that must not extend an entry's life: a listing is not the activity the LRU
        exists to measure, and letting it refresh recency would quietly change who gets evicted.
        """
        return self._entries.get(key)

    def put(self, key: K, value: V) -> None:
        """Insert or refresh `key` as most-recently-used, then evict past capacity.

        Eviction takes the least-recently-used entry that is neither pinned nor the key just put;
        when no candidate remains the map briefly holds over capacity (see the module docstring).
        """
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity():
            victim = next(
                (
                    candidate
                    for candidate in self._entries
                    if candidate != key and not self._pinned(candidate)
                ),
                None,
            )
            if victim is None:
                break
            del self._entries[victim]
