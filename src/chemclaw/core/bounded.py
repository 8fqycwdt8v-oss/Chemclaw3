"""One bounded LRU map for every cache keyed by an unbounded identity.

Such keys are session ids, user oids and principals. Shared by the front door's live sessions,
the budget counters, the rate limiter's buckets and the
attachment store. `core/metrics.py`'s label-series cap deliberately does not use it: that cap
refuses new series rather than evicting old ones, since evicting would let an attacker reset real
counters.

Semantics:

- `get` marks the entry most-recently-used; `peek` reads without marking.
- `put` inserts or refreshes as most-recently-used, then evicts least-recently-used entries past
  capacity. The entry just put is never the victim, since its value is being handed to the caller.
- `capacity` may be an int or a zero-argument callable, so a config-backed bound stays live.
- `weight`/`max_weight` (both or neither) add a second bound in the caller's unit (bytes, for the
  attachment store). Weight is measured at `put`, so a mutated value must be `put` back. An entry
  heavier than `max_weight` is held alone without evicting anything else for it, since no eviction
  could reach the bound.
- `pinned` names keys eviction must skip right now; when every candidate is pinned the map briefly
  exceeds `capacity` rather than corrupt an in-use entry.

Not thread-safe; threaded callers hold their own lock.
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
        weight: Callable[[V], int] | None = None,
        max_weight: int | Callable[[], int] | None = None,
    ) -> None:
        """Create the map with `capacity` (fixed, or a callable read at each eviction pass).

        `pinned` says which keys must not be evicted right now. `weight` and `max_weight` are one
        bound:
        `weight` measures an entry at each `put`, `max_weight` is the total before
        least-recently-used
        entries are evicted. Omit both to bound by entry count alone.

        Raises:
            ValueError: if exactly one of `weight`/`max_weight` is given; either half alone reads as
            a
                bound that is not there.
        """
        if (weight is None) != (max_weight is None):
            raise ValueError("weight and max_weight are one bound: pass both or neither")
        self._capacity: Callable[[], int] = capacity if callable(capacity) else (lambda: capacity)
        self._pinned: Callable[[K], bool] = pinned if pinned is not None else (lambda _key: False)
        self._weight: Callable[[V], int] | None = weight
        self._max_weight: Callable[[], int] | None = (
            None
            if max_weight is None
            else (max_weight if callable(max_weight) else (lambda: max_weight))
        )
        self._entries: OrderedDict[K, V] = OrderedDict()
        self._weights: dict[K, int] = {}

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

        For reads, such as a listing, that must not change who gets evicted.
        """
        return self._entries.get(key)

    def total_weight(self) -> int:
        """The summed weight of everything held — 0 when the map has no weight bound.

        What a caller's budget assertion or gauge reads, in the unit `weight` measures.
        """
        return sum(self._weights.values())

    def _should_evict(self, key: K) -> bool:
        """Whether another eviction is both needed and useful, with `key` the entry just put.

        Needed: a bound is breached. Useful: one eviction can close it; when `key` alone exceeds
        `max_weight` none can, so the loop must not empty the map. A count breach is always
        closable.
        """
        if len(self._entries) > self._capacity():
            return True
        if self._max_weight is None:
            return False
        limit = self._max_weight()
        return self.total_weight() > limit and self._weights.get(key, 0) <= limit

    def put(self, key: K, value: V) -> None:
        """Insert or refresh `key` as most-recently-used, then evict past either bound.

        Evicts the least-recently-used entry that is neither pinned nor `key`; when none remains, or
        no
        eviction could close the weight breach, the map briefly holds over its bound. Weight is
        (re-)measured here, so a value mutated in place must be `put` back.
        """
        self._entries[key] = value
        self._entries.move_to_end(key)
        if self._weight is not None:
            self._weights[key] = self._weight(value)
        while self._should_evict(key):
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
            self._weights.pop(victim, None)
