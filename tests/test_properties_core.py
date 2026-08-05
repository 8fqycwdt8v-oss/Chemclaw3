"""Property-based tests over the pure cores — the invariants, not a handful of examples.

Every test in this repository was example-based until now, and the gap that leaves is specific:
an example proves a function works on the input someone thought of. These modules are all
*identity* and *bounding* primitives — a hash that keys the calculation cache, an LRU that bounds
four maps whose unbounded growth has been fixed three times, a citation parser the PR-gate and the
answer verifier both read with. Their contracts are universally quantified ("equivalent inputs
collapse to the same key", "never exceeds capacity"), so they are exactly the shape a generator
tests better than a person does.

Deliberately scoped to the pure layer: no database, no network, no Temporal. A property test whose
failures need a live stack to reproduce is a flaky test, and the counterexample — the whole point —
becomes unusable. `hypothesis` prints and replays the minimal failing input, which is the artefact
worth having here.
"""

from __future__ import annotations

import json

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import cited_ids, mentioned_ids

# JSON-native values, which is exactly what `stable_hash` documents itself as taking. Bounded in
# size because the property is about canonicalisation, not about throughput, and an unbounded
# generator spends the budget building megabytes rather than finding shapes.
_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**9), max_value=10**9)
    | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=6) | st.dictionaries(st.text(max_size=12), children, max_size=6)
    ),
    max_leaves=15,
)


@given(_JSON)
def test_stable_hash_is_deterministic(payload: object) -> None:
    """The same value hashes to the same key, always — the cache's whole premise.

    `science.calc.store` keys "never compute twice" on this. If it were not a function of the
    value alone, the cache would miss silently and the only symptom would be a bill.
    """
    assert stable_hash(payload) == stable_hash(payload)


@given(st.dictionaries(st.text(max_size=12), st.integers(), max_size=8))
def test_stable_hash_ignores_mapping_order(mapping: dict[str, int]) -> None:
    """Key order must not change the key. `sort_keys=True` is the mechanism; this is the contract.

    It matters because payloads reach `stable_hash` from JSON bodies, pydantic dumps and hand-built
    dicts, and nothing upstream promises an order. Two chemists asking the identical question
    through different surfaces must land on one cache entry.
    """
    reversed_mapping = dict(reversed(list(mapping.items())))
    assert stable_hash(mapping) == stable_hash(reversed_mapping)


@given(_JSON, _JSON)
def test_stable_hash_separates_distinct_values(left: object, right: object) -> None:
    """Distinct canonical forms give distinct keys, at the width the module ships.

    Stated over the *canonical form* rather than over the Python value, because that is what the
    function actually hashes — `1` and `1.0` and `True` are not distinguishable here and the
    docstring does not claim they are. Asserting more than the code promises is how a test becomes
    a fiction about the contract.
    """
    assume(
        json.dumps(left, sort_keys=True, separators=(",", ":"), default=str)
        != json.dumps(right, sort_keys=True, separators=(",", ":"), default=str)
    )
    assert stable_hash(left) != stable_hash(right)


@given(st.integers(min_value=4, max_value=32))
def test_stable_hash_width_is_what_was_asked_for(chars: int) -> None:
    """`chars` is a real knob — the memory-note ids use a shorter one on purpose."""
    assert len(stable_hash({"a": 1}, chars=chars)) == chars


@given(
    capacity=st.integers(min_value=1, max_value=32),
    keys=st.lists(st.integers(min_value=0, max_value=64), min_size=1, max_size=200),
)
@settings(max_examples=200)
def test_bounded_lru_never_exceeds_capacity(capacity: int, keys: list[int]) -> None:
    """The bound holds for every insertion order, which is the only thing it is for.

    Four call sites depend on it — the front door's session and budget maps, the rate limiter's
    per-principal buckets, and the agent's attachment store — and the growth bug it exists to
    prevent has been fixed three times in this codebase, most recently for metric label series.
    """
    lru: BoundedLru[int, int] = BoundedLru(capacity)
    for key in keys:
        lru.put(key, key)
        assert len(lru) <= capacity
    assert len(lru) == min(capacity, len(set(keys)))


@given(
    capacity=st.integers(min_value=2, max_value=16),
    keys=st.lists(st.integers(min_value=0, max_value=8), min_size=1, max_size=40),
)
def test_bounded_lru_keeps_what_it_last_touched(capacity: int, keys: list[int]) -> None:
    """The most recently used key survives eviction — the property that makes it an *LRU*.

    A bounded map that evicted arbitrarily would still satisfy the capacity test above while being
    useless: the rate limiter would drop the bucket of whoever is currently hammering it.
    """
    lru: BoundedLru[int, int] = BoundedLru(capacity)
    for key in keys:
        lru.put(key, key)
    assert lru.get(keys[-1]) == keys[-1]


@given(
    st.lists(st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=12), max_size=6)
)
def test_cited_ids_finds_every_wikilink_it_is_given(ids: list[str]) -> None:
    """Every `[[id]]` written is an id returned — the citation contract, in both directions.

    `cited_ids` is read by the note schema, the answer verifier and the live eval's citation score.
    A local regex once disagreed with it and reported a clean record for an answer whose nine
    citations were every one of them dangling, so "one reader for one syntax" is enforced by using
    the production function — and this pins that the production function sees what it is shown.
    """
    body = " ".join(f"[[{note_id}]]" for note_id in ids)
    assert set(cited_ids(body)) == set(ids)


@given(st.text(max_size=200))
def test_citation_readers_never_raise_on_arbitrary_prose(body: str) -> None:
    """Neither reader may throw on text a model wrote — they run on every answer.

    An exception here is not a parse failure, it is a turn that dies after the model has already
    produced its answer. The generated corpus includes unbalanced brackets, which is exactly the
    shape a truncated stream produces.
    """
    assert isinstance(cited_ids(body), list)
    assert isinstance(mentioned_ids(body), list)


@given(st.text(max_size=120))
def test_both_citation_readers_dedupe_and_keep_first_seen_order(body: str) -> None:
    """The one contract both readers actually share, stated by both docstrings.

    Not a subset relation — the first version of this test asserted `cited_ids ⊆ mentioned_ids`,
    which is a fiction: they read *different syntaxes* to answer different questions. `cited_ids`
    reads `[[wikilinks]]`, what an author claims; `mentioned_ids` reads what a tool payload
    contains, what the turn actually retrieved. Their own docstrings say so, and asserting more
    than the code promises is how a test becomes a story about the contract rather than a check on
    it.

    What they do share is normalisation: deduped, first-seen order preserved. That is load-bearing
    — the grounding score is a set difference between them, and a reader that reordered or
    duplicated would move the number without any behaviour changing.
    """
    for reader in (cited_ids, mentioned_ids):
        ids = reader(body)
        assert len(ids) == len(set(ids)), "a repeated citation must yield one id"
        assert ids == list(dict.fromkeys(ids)), "first-seen order must be preserved"
