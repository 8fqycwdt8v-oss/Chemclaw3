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

Three more invariants joined the beachhead (T11), each one a claim some module already makes in
prose and no test quantified: the note serialization round-trip that `kg/render.py`'s docstring
states as an equation, the PR-gate submission's dedup, and the budget tracker's monotonicity. The
round-trip writes to a `tempfile` — still no service, and the counterexample still replays.

The fourth candidate, "the in-memory and Postgres `find` backends agree", is deliberately *not*
here: it needs a database, which is the one thing this file will not take.
`tests/test_postgres_store.py::test_find_matches_the_in_memory_backend` compares them on fixed
fixtures, and `docs/planning/BACKLOG.md` carries the generated version.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings as config
from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note, Relation, cited_ids, mentioned_ids, parse_note
from chemclaw.kg.pr_gate import _build_submission
from chemclaw.kg.render import render_note

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


# --- the note round trip, the equation `kg/render.py` states -----------------------------------

# `Note._slug_only` bounds ids and types; generating outside it would only exercise the validator.
_SLUGS = st.from_regex(r"\A[a-z0-9][a-z0-9._-]{0,20}\Z").filter(
    lambda slug: ".." not in slug and not slug.endswith(".") and not slug.endswith(".lock")
)

# Bodies are generated **stripped and carriage-return-free**, and both exclusions are findings
# rather than convenience. `python-frontmatter` strips the content it parses, so a body of `" "`
# comes back `""`; and `Path.read_text` translates newlines, so `"a\rb"` comes back `"a\nb"`. Both
# are normalisations of characters Markdown does not distinguish, so neither is worth fixing — but
# `render.py` stated the round trip as an unqualified equation, and it is not one. Its docstring
# now says which two things it is up to.
_BODIES = st.text(alphabet=st.characters(exclude_characters="\r"), max_size=120).map(str.strip)


# A window that is never inverted, since `TemporalWindow` refuses those at construction and this
# property is about serialization, not about the validator.
def _ordered_window(pair: tuple[date | None, date | None]) -> tuple[date | None, date | None]:
    """Put a generated pair of dates the right way round; leave an open-ended one alone."""
    start, end = pair
    if start is None or end is None or start <= end:
        return pair
    return end, start


_WINDOWS = st.tuples(st.none() | st.dates(), st.none() | st.dates()).map(_ordered_window)


@st.composite
def _notes(draw: st.DrawFn) -> Note:
    """A schema-valid `Note` across every optional field, so none is silently never generated."""
    valid_from, valid_to = draw(_WINDOWS)
    return Note(
        id=draw(_SLUGS),
        type=draw(_SLUGS),
        body=draw(_BODIES),
        tags=draw(st.lists(st.text(min_size=1, max_size=10), max_size=3)),
        created_by=draw(st.sampled_from(["human", "agent"])),
        source=draw(st.none() | st.text(min_size=1, max_size=20)),
        confidence=draw(st.none() | st.floats(min_value=0.0, max_value=1.0)),
        valid_from=valid_from,
        valid_to=valid_to,
        relations=draw(
            st.lists(
                st.builds(Relation, rel=_SLUGS, to=_SLUGS, confidence=st.none()),
                max_size=2,
            )
        ),
    )


@given(_notes())
@settings(max_examples=150)
def test_a_note_survives_the_write_read_round_trip(note: Note) -> None:
    """`parse_note(write(render_note(n))) == n`, quantified rather than asserted in a docstring.

    This is the graph's durability claim. Every agent-authored note reaches Git through
    `render_note` and comes back through `parse_note`, so a field that does not survive is a fact
    the system silently forgets between proposing it and reading it — and `exclude_none=True`
    means an optional field that round-tripped wrongly would look like an absence rather than an
    error. The existing tests cover one hand-written note; a generator covers every combination of
    the nine optional fields, which is where an omission would actually hide.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "note.md"
        path.write_text(render_note(note), encoding="utf-8")
        assert parse_note(path) == note


@given(
    note=_notes(),
    dependencies=st.lists(_notes(), max_size=5),
    directory=st.sampled_from(["knowledge", "kg/notes"]),
)
@settings(max_examples=100)
def test_a_submission_writes_each_note_once_with_its_subject_first(
    note: Note, dependencies: list[Note], directory: str
) -> None:
    """One path per note, subject first — the invariant `_build_submission`'s docstring claims.

    It argues that a caller "may legitimately list the same dependency twice" and that writing one
    path twice in a commit is "at best noise and at worst two different renderings racing". Both
    halves are quantified here, because the generator produces exactly the collisions a fixed
    example cannot enumerate: a dependency repeated, a dependency that *is* the subject, and two
    distinct notes that share an id and differ in body — the racing-renderings case, where the
    first occurrence must win rather than the last.
    """
    submission = _build_submission(note, directory, dependencies)
    paths = [file.path for file in submission.files]
    assert len(paths) == len(set(paths)), "a commit that writes one path twice"
    assert submission.files[0].path.startswith(f"{directory}/{note.type}/{note.id}")
    assert submission.branch == f"note/{note.id}"

    expected_ids = list(dict.fromkeys([note.id, *(dep.id for dep in dependencies)]))
    assert len(paths) == len(expected_ids)


# --- budget monotonicity: a booked turn is never unbooked ---------------------------------------


@given(
    turns=st.lists(st.integers(min_value=-50, max_value=500), min_size=1, max_size=25),
    cap=st.integers(min_value=1, max_value=8),
)
@settings(max_examples=100)
def test_the_budget_refusal_is_permanent_once_a_cap_is_reached(turns: list[int], cap: int) -> None:
    """A scope that has been refused stays refused — the guard's one safety property.

    The tracker is documented as best-effort about *overshoot*: concurrent turns may pass `check`
    before any of them `record`. It is not best-effort about the other direction. A cap that
    un-fired would let the "$400 in twenty minutes" runaway resume by itself, and nothing upstream
    re-checks. So: once `check` raises for a session, no later `record` may make it pass again.

    Negative token counts are generated deliberately — a provider's usage field is not
    trustworthy, `_book` clamps with `max(tokens, 0)`, and a clamp that was removed would let a
    bad usage report *refund* a session's budget.
    """
    # The manual `MonkeyPatch()` is required here and is the one place in this repository that is
    # true: pytest's `monkeypatch` fixture is function-scoped, and a function-scoped fixture is set
    # up *once* around a `@given` test while the body runs once per example — so every example
    # after the first would inherit whatever the previous one left. Hypothesis says so by raising
    # `HealthCheck.function_scoped_fixture`. Patching and undoing inside the body is what gives
    # each example a clean tracker. (The `suppress_health_check` that used to sit on `@settings`
    # here suppressed nothing, because this test takes no fixture for it to fire on.)
    patch = pytest.MonkeyPatch()
    patch.setattr(config, "budget_enabled", True)
    patch.setattr(config, "budget_max_turns_per_session", cap)
    patch.setattr(config, "budget_max_tokens_per_session", 0)
    patch.setattr(config, "budget_max_turns_per_user", 0)
    patch.setattr(config, "budget_max_tokens_per_user", 0)
    try:
        tracker = BudgetTracker()
        refused = False
        for tokens in turns:
            try:
                tracker.check("s", None)
            except BudgetExceeded:
                refused = True
            else:
                assert not refused, "a refused session was admitted again by a later turn"
            tracker.record("s", None, tokens=tokens)
        assert refused == (len(turns) > cap)
    finally:
        patch.undo()
