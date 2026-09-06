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
- `weight`/`max_weight` (optional, both or neither) add a *second* bound in the caller's own unit —
  bytes, for the attachment store, whose entries differ in size by six orders of magnitude and
  whose entry count therefore says nothing about what it holds (a 1000-session cap over 10 files of
  2 MB is a 20 GB ceiling in a 1 GiB pod). An entry's weight is measured at `put`, so a caller that
  mutates a stored value must `put` it back — which is what the eviction contract already requires.
  A single entry heavier than `max_weight` is still held: it is the entry just put, which is never
  the victim, exactly as with `pinned`. **And nothing else is evicted for it**: an over-weight entry
  used to drain the map on its way in — ten entries gone and the bound still breached fivefold,
  measured — because the loop ran until the budget was met or no candidate was left, and neither
  ever happened. Evicting every other caller's data to make room for something that does not fit is
  strictly worse than holding it alone: it costs the data *and* leaves the bound breached. So weight
  eviction stops when it cannot reach the bound. The count bound is unaffected — it is always
  reachable, because dropping entries always reduces a count.
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
        weight: Callable[[V], int] | None = None,
        max_weight: int | Callable[[], int] | None = None,
    ) -> None:
        """Create the map with `capacity` (fixed, or a callable read at each eviction pass).

        `pinned` says which keys must not be evicted right now (default: none) — see the module
        docstring for the over-capacity trade that implies.

        `weight` and `max_weight` are one bound and must be passed together: `weight` measures an
        entry in the caller's unit at each `put`, `max_weight` is the total that unit may reach
        before the least-recently-used entries are evicted. Omit both and the map is bounded by
        entry count alone, which is what every caller but the attachment store wants.

        Raises:
            ValueError: if exactly one of `weight`/`max_weight` is given — a weight nothing bounds
                is bookkeeping with no effect, and a budget with no way to measure an entry cannot
                be enforced. Either half alone reads as a bound that is not there.
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

        For reads that must not extend an entry's life: a listing is not the activity the LRU
        exists to measure, and letting it refresh recency would quietly change who gets evicted.
        """
        return self._entries.get(key)

    def total_weight(self) -> int:
        """The summed weight of everything held — 0 when the map has no weight bound.

        What a caller's budget assertion or gauge reads, in the unit `weight` measures.
        """
        return sum(self._weights.values())

    def _should_evict(self, key: K) -> bool:
        """Whether another eviction is both needed and *useful*, with `key` the entry just put.

        Needed: a bound is breached — entry count, or total weight where one is configured.

        Useful: the breach is one eviction can actually close. `key` is never the victim, so when
        it alone weighs more than `max_weight`, no sequence of evictions reaches the budget — and
        running the loop anyway empties the map for nothing. A count breach is always closable,
        which is why only the weight arm carries the test.
        """
        if len(self._entries) > self._capacity():
            return True
        if self._max_weight is None:
            return False
        limit = self._max_weight()
        return self.total_weight() > limit and self._weights.get(key, 0) <= limit

    def put(self, key: K, value: V) -> None:
        """Insert or refresh `key` as most-recently-used, then evict past either bound.

        Eviction takes the least-recently-used entry that is neither pinned nor the key just put;
        when no candidate remains, or when no eviction could close the weight breach anyway, the map
        briefly holds over its bound (see the module docstring). The entry's weight, where the map
        has one, is (re-)measured here — so a caller that mutates a stored value in place must `put`
        it back for the byte budget to see the change.
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
