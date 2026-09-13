# D-2026-09-13-a-cap-proven-on-a-class-is-not-proven-on-the-wiring — three memory bounds, each true of something other than what ships

Three bounds added over the last two waves — the `store` file cap, its eviction order, and the
in-memory preference cap — were each correct about an object and wrong about the thing a deployment
runs. This is one ADR because the shape is one shape.

## What was measured

**The `store` cap was never exercised through `scratchpad_backend`.** Both cap tests construct their
own `BoundedStoreBackend`. Mutated to `StoreBackend(` in `agent/scratchpad.py` — the cap absent in
production, every memory namespace unbounded again — `tests/test_scratchpad.py` passed 17 of 17 and
six related files passed 145.

**Eviction took the newest of the surplus.** `asearch` with no query resolves, against
`AsyncPostgresStore`, to `ORDER BY updated_at DESC LIMIT …`, so reading `cap + _EVICTION_PAGE` rows
and taking the oldest of *that page* is a middle band. Driven on real Postgres with 89 files written
oldest-first, a cap of 5 and one bounded write: it deleted **021-084** and kept **000-020** — every
one of the twenty-one least recently updated files retained, and the twenty-one *most recent* of the
surplus destroyed. Both spellings converge to the same steady state over later writes, which is
presumably why it survived review; what differs is the state a deployment is left in when the writes
stop, and it is the exact inverse of the stated policy, in the case the docstring itself addresses.

Fixing that by asking for `offset=cap` instead is the same bug: that page is the newest of the
surplus too. It is also a bet on an ordering `BaseStore` does not promise — measured,
`InMemoryStore` answers a query-less search in **insertion** order, so the two shipped
implementations of one interface disagree and the Postgres one is the only reason the old spelling
converged at all.

**Memory-mode preference eviction inverted its own docstring.** `_evict_in_memory` deletes from the
front of `self._memory` and `recall` keeps its tail, both on the stated understanding that
"`remember` re-inserts on every write". `d[k] = v` on a key already present does not move it, so
that order was least recently *created*. Driven at a cap of 3 — write a, b, c, update a, add d:

    memory   : [('b','v1'), ('c','v1'), ('d','v1')]     # evicted 'a', the one just restated
    postgres : [('a','v2'), ('c','v1'), ('d','v1')]     # evicted 'b'

Worse, with the cap lowered under an existing owner, rewriting the oldest-inserted key evicted the
row it had just written while `remember` returned **True** and the tool answered "Remembered for
future sessions".

## The decision

- `scratchpad_backend`'s `/memories/` route is asserted to be a `BoundedStoreBackend`.
- `_evict_past_the_cap` pages the namespace whole with `offset`, orders by `updated_at` **itself**,
  and deletes the entire surplus in one pass. It depends on nothing but the field `Item` documents
  and on `offset` skipping. `_EVICTION_PAGE` is the page size of that walk rather than a bound on
  the deletion — a page that bounded the deletion is what made this take the newest of the surplus.
  In steady state the walk is one query for `cap + 1` rows, fewer than the `cap + _EVICTION_PAGE`
  it replaced.
- `PreferenceStore.remember` pops before it sets, so the dict's insertion order is write order,
  which is what both of its readers already claimed.

## What keeps it true

- `tests/test_scratchpad.py::test_the_memories_route_the_wiring_installs_is_the_bounded_one`.
- `tests/test_scratchpad.py::test_eviction_takes_the_least_recently_updated_even_far_past_the_cap`,
  on real Postgres, with the namespace deliberately more than `_EVICTION_PAGE` past its cap — the
  case the old spelling could not see and the existing tests, which write fifteen files, are always
  inside.
- `tests/test_upstream_surface.py::test_a_store_search_still_pages_by_offset_and_still_dates_every_item`,
  which asserts the two shapes the eviction rests on and records the ordering disagreement it
  deliberately does *not* rest on.
- `tests/test_preferences.py::test_updating_a_preference_makes_it_the_most_recent_in_both_modes`,
  written as an *agreement between the two modes* rather than against a transcribed list, because
  the claim the code makes is that they answer the same question the same way. Every key is
  distinct **and re-used**, which the two existing cap tests are not: both write only fresh keys,
  so neither can reach the line under test.
- `tests/test_preferences.py::test_a_preference_that_was_evicted_is_not_reported_as_remembered`.

Four mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| `BoundedStoreBackend(` → `StoreBackend(` in `scratchpad_backend` | red — the wiring test, 1 of 19 |
| eviction back to the `cap + _EVICTION_PAGE` read | red — the eviction-order test |
| `asearch(..., offset=cap)` alone (the near-miss fix) | red — same test, same middle band |
| the `pop` removed from `remember` | red — both preference tests |
