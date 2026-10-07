"""Property-based tests over the pure cores: the invariants, not a handful of examples.

These are identity and bounding primitives (the cache-key hash, the bounded LRU, the citation
readers) plus the note round trip, the record write order and the budget tracker's monotonicity,
whose contracts are universally quantified. Scoped to the pure layer, no database or network, so
`hypothesis` can replay the minimal counterexample. The in-memory/Postgres `find` agreement needs
a database and lives in `tests/test_postgres_store.py`.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings as config
from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note, Relation, cited_ids, mentioned_ids, parse_note
from chemclaw.kg.record import _build_write
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
    """Key order must not change the key.

    Payloads reach `stable_hash` from JSON bodies, pydantic dumps and hand-built dicts with no
    guaranteed order, and identical questions must share one cache entry.
    """
    reversed_mapping = dict(reversed(list(mapping.items())))
    assert stable_hash(mapping) == stable_hash(reversed_mapping)


@given(_JSON, _JSON)
def test_stable_hash_separates_distinct_values(left: object, right: object) -> None:
    """Distinct canonical forms give distinct keys, at the width the module ships.

    Stated over the canonical form, which is what is hashed: `1`, `1.0` and `True` are not
    distinguished, and the docstring does not claim they are.
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
    """The count bound holds for every insertion order.

    The front door's session and budget maps, the rate limiter's buckets and the attachment store
    depend on it.
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
    max_weight=st.integers(min_value=32, max_value=64),
    items=st.lists(
        st.tuples(st.integers(min_value=0, max_value=32), st.integers(min_value=1, max_value=32)),
        min_size=1,
        max_size=200,
    ),
)
@settings(max_examples=200)
def test_bounded_lru_never_exceeds_its_weight_bound(
    max_weight: int, items: list[tuple[int, int]]
) -> None:
    """The weight bound holds for every insertion order.

    The attachment store's entries differ in size by orders of magnitude, so an entry cap alone does
    not bound memory. Every generated value fits on its own (`max_weight >= 32`), making this the
    exact bound; the oversized entry is its own case below.
    """
    lru: BoundedLru[int, int] = BoundedLru(
        1_000_000, weight=lambda value: value, max_weight=max_weight
    )
    for key, weight in items:
        lru.put(key, weight)
        assert lru.total_weight() <= max_weight


def test_bounded_lru_does_not_empty_itself_for_an_entry_that_cannot_fit() -> None:
    """An entry heavier than the whole budget is held, and nothing is evicted to make room for it.

    The entry just put is never the victim, so evicting others cannot meet the bound and would only
    destroy every other caller's data. The next entry that does fit resumes eviction and takes the
    oversized one with it.
    """
    lru: BoundedLru[str, int] = BoundedLru(1_000_000, weight=lambda value: value, max_weight=100)
    for index in range(10):
        lru.put(f"k{index}", 5)

    lru.put("big", 500)

    assert len(lru) == 11
    assert lru.peek("k0") == 5
    assert lru.total_weight() == 550

    lru.put("next", 5)

    assert lru.total_weight() <= 100
    assert "big" not in lru


def test_bounded_lru_refuses_half_a_weight_bound() -> None:
    """Half a bound reads as a bound that is not there.

    A weight nothing enforces, or a budget with no way to measure an entry.
    """
    with pytest.raises(ValueError, match="pass both or neither"):
        BoundedLru[int, int](8, weight=lambda value: value)
    with pytest.raises(ValueError, match="pass both or neither"):
        BoundedLru[int, int](8, max_weight=16)


@given(
    st.lists(st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=12), max_size=6)
)
def test_cited_ids_finds_every_wikilink_it_is_given(ids: list[str]) -> None:
    """Every `[[id]]` written is an id returned by `cited_ids`.

    The note schema, the answer verifier and the eval citation score all read with this one
    function, so it must see what it is shown.
    """
    body = " ".join(f"[[{note_id}]]" for note_id in ids)
    assert set(cited_ids(body)) == set(ids)


@given(st.text(max_size=200))
def test_citation_readers_never_raise_on_arbitrary_prose(body: str) -> None:
    """Neither citation reader raises on arbitrary text a model wrote.

    They run on every answer, so an exception would kill the turn after the answer exists. Generated
    text includes unbalanced brackets, the shape of a truncated stream.
    """
    assert isinstance(cited_ids(body), list)
    assert isinstance(mentioned_ids(body), list)


@given(st.text(max_size=120))
def test_both_citation_readers_dedupe_and_keep_first_seen_order(body: str) -> None:
    """Both citation readers dedupe and keep first-seen order.

    They read different syntaxes (`cited_ids` wikilinks, `mentioned_ids` tool payloads), so no
    subset relation holds. The shared normalisation matters because the grounding score is a set
    difference between them.
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

# Bodies are generated stripped and CR-free: `python-frontmatter` strips content and
# `Path.read_text` translates newlines, both normalisations Markdown does not distinguish (and
# `render.py`'s docstring says so). Surrogates are excluded because `Note._text_is_writable` refuses
# them; `test_a_note_refuses_text_utf8_cannot_encode` pins that refusal.
_TEXT = st.characters(exclude_categories=["Cs"])
_BODIES = st.text(
    alphabet=st.characters(exclude_characters="\r", exclude_categories=["Cs"]), max_size=120
).map(str.strip)


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
        tags=draw(st.lists(st.text(alphabet=_TEXT, min_size=1, max_size=10), max_size=3)),
        created_by=draw(st.sampled_from(["human", "agent"])),
        source=draw(st.none() | st.text(alphabet=_TEXT, min_size=1, max_size=20)),
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
    """`parse_note(write(render_note(n))) == n`, quantified over notes.

    Every agent-authored note reaches Git through `render_note` and returns through `parse_note`,
    and `exclude_none=True` would make a lost optional field look like an absence. Generating covers
    every combination of the optional fields.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "note.md"
        path.write_text(render_note(note), encoding="utf-8")
        assert parse_note(path) == note


_SURROGATE = "\ud800"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("body", _SURROGATE),
        ("source", _SURROGATE),
        ("compound_smiles", _SURROGATE),
        ("tags", [_SURROGATE]),
    ],
)
def test_a_note_refuses_text_utf8_cannot_encode(field: str, value: object) -> None:
    r"""Every unconstrained string a note carries must be UTF-8 encodable, not only the body.

    `json.loads('"\ud800"')` yields an unpaired surrogate that later raises `UnicodeEncodeError` in
    whichever writer touches it; refusing at the schema turns that into one rejected note.
    Parametrized over the field kinds `_text_is_writable` walks.
    """
    with pytest.raises(ValidationError, match="UTF-8 cannot encode"):
        Note(id="n", type="reaction", **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"id": _SURROGATE}, id="id"),
        pytest.param({"type": _SURROGATE}, id="type"),
        pytest.param({"relations": [{"rel": "cites", "to": _SURROGATE}]}, id="relation"),
        pytest.param({"calc_refs": [_SURROGATE]}, id="calc_refs"),
        pytest.param({"artifact_refs": [_SURROGATE]}, id="artifact_refs"),
    ],
)
def test_an_already_validated_field_is_refused_before_our_validator(
    kwargs: dict[str, object],
) -> None:
    """Fields already validated elsewhere are refused before our validator.

    Pydantic's constrained strings reject `id`, `type` and `Relation.rel`/`to`, and the ref-shape
    validators reject the ref lists, so `_text_is_writable` skips them. This asserts rejection
    without asserting who rejects, so if either stops doing its half the gap fails here.
    """
    with pytest.raises(ValidationError):
        Note(**{"id": "n", "type": "reaction", **kwargs})  # type: ignore[arg-type]


@given(
    note=_notes(),
    dependencies=st.lists(_notes(), max_size=5),
    superseded=st.lists(_notes(), max_size=3),
    directory=st.sampled_from(["knowledge", "kg/notes"]),
)
@settings(max_examples=100)
def test_a_write_writes_each_note_once_in_dependency_subject_retirement_order(
    note: Note, dependencies: list[Note], superseded: list[Note], directory: str
) -> None:
    """A write writes each note once, in dependency → subject → retirement order.

    Each leg cites the one before it, so a reader mid-write never sees a note before what it cites;
    a retirement written first would point `superseded-by` at a missing successor. Generating over
    all three legs also pins cross-leg dedup (first occurrence wins), including a dependency that is
    the subject and two notes sharing an id. The commit-message count is pinned too.
    """
    write = _build_write(note, directory, dependencies, superseded)
    paths = [file.path for file in write.files]
    ids = [_id_of(path, directory) for path in paths]
    assert len(paths) == len(set(paths)), "a commit that writes one path twice"

    # Asserted as the invariant rather than as a re-derived sequence: a model that repeats
    # `_build_write`'s own dedup would agree with it however either was mutated.
    assert ids.count(note.id) == 1, "the subject is written exactly once"
    subject = ids.index(note.id)
    assert paths[subject].startswith(f"{directory}/{note.type}/{note.id}")
    for dependency in dependencies:
        if dependency.id != note.id:
            assert ids.index(dependency.id) < subject, "a dependency lands after its subject"
    for retired in superseded:
        if retired.id != note.id and retired.id not in {d.id for d in dependencies}:
            assert ids.index(retired.id) > subject, "a retirement lands before its successor"

    # Which leg a file is in decides whether it may overwrite: a dependency re-rides on every citing
    # write, so overwriting would revert a chemist's edit; a retirement is that copy with `valid_to`
    # closed, so rewriting is the point.
    for file in write.files[:subject]:
        assert file.overwrite is False, "a dependency may overwrite a human's edit"
    for file in write.files[subject:]:
        assert file.overwrite is True, "the subject or a retirement refuses to land"

    extra = len(paths) - 1
    assert write.message.endswith(
        f"{note.id} with {extra} supporting note(s)" if extra else note.id
    )


def _id_of(path: str, directory: str) -> str:
    """The note id a rendered path ends in — `<directory>/<type>/<id>.md`."""
    return path.removeprefix(f"{directory}/").split("/", 1)[1].removesuffix(".md")


# --- budget monotonicity: a booked turn is never unbooked ---------------------------------------


@given(
    turns=st.lists(st.integers(min_value=-50, max_value=500), min_size=1, max_size=25),
    cap=st.integers(min_value=1, max_value=8),
)
@settings(max_examples=100)
def test_the_budget_refusal_is_permanent_once_a_cap_is_reached(turns: list[int], cap: int) -> None:
    """A scope that has been refused by the budget stays refused.

    Overshoot under concurrency is tolerated; un-firing is not, since nothing upstream re-checks.
    Negative token counts are generated because `_book` clamps provider usage with `max(tokens, 0)`,
    and without the clamp a bad report would refund a budget.
    """
    # A manual `MonkeyPatch()`: pytest's `monkeypatch` is set up once around a `@given` test, so
    # each example would inherit the previous one's state. Patching and undoing in the body gives
    # each example a clean tracker.
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
                asyncio.run(tracker.check("s", None))
            except BudgetExceeded:
                refused = True
            else:
                assert not refused, "a refused session was admitted again by a later turn"
            tracker.record("s", None, tokens=tokens)
        assert refused == (len(turns) > cap)
    finally:
        patch.undo()
