"""Every `NoteStore` answers the same questions the same way.

One suite, parametrised over the stores that exist (`STORES` in `tests/note_store_harness.py`): the
git store and the Postgres store. A harness builds its corpus with the store's own real write, and
the assertions read only through `NoteStore`. What only one store has (a checkout, a history, stat
caches; a transaction) is tested after the suite and says so; the Postgres store's own are in
`tests/test_postgres_note_store.py`.
"""

import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg import graph as corpus
from chemclaw.kg.note import Note, Relation, note_relative_path
from chemclaw.kg.render import render_note
from chemclaw.kg.store import GitNoteStore, NoteStore, note_store
from tests.kg_repo import write_notes
from tests.note_store_harness import AUTUMN, SPRING, STORES, Harness
from tests.note_store_harness import corpus as _corpus

_SPRING = SPRING
_AUTUMN = AUTUMN


@pytest.fixture(params=sorted(STORES))
async def harness(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Harness:
    """An empty store of each kind."""
    return await STORES[request.param](tmp_path, monkeypatch)


async def _loaded(harness: Harness) -> NoteStore:
    await harness.add(*_corpus())
    return harness.store


async def test_get_many_answers_each_id_as_get_does_from_one_read(harness: Harness) -> None:
    """An id nothing defines is `None` in the batch too, and an empty batch is an empty answer."""
    store = await _loaded(harness)
    ids = ["alpha", "missing-note", "reaction-eln.123", "no-such-id", "loner"]
    found = await store.get_many(ids)
    assert list(found) == ids
    for note_id in ids:
        assert found[note_id] == await store.get(note_id)
    assert found["alpha"] is not None and found["missing-note"] is None
    assert await store.get_many([]) == {}


async def test_edges_hold_every_link_into_and_out_of_a_note_with_its_relations(
    harness: Harness,
) -> None:
    """Both directions, every relation on a link whatever its window, dangling ids included."""
    store = await _loaded(harness)
    by_link = {(edge.src, edge.dst): edge.relations for edge in await store.edges("alpha")}
    assert set(by_link) == {
        ("alpha", "beta"),
        ("alpha", "future"),
        ("alpha", "old"),
        ("alpha", "missing-note"),
        ("alpha", "reaction-eln.123"),
    }
    # `[[beta]]` cites and the frontmatter says `uses` until March: one link, two relations.
    assert {r.rel for r in by_link[("alpha", "beta")]} == {"cites", "uses"}
    (uses,) = [r for r in by_link[("alpha", "beta")] if r.rel == "uses"]
    assert uses.valid_to == date(2026, 3, 31) and uses.valid_from is None
    # Into `mid`: `old` is superseded by it and `new` supersedes it, in the direction each asserts.
    into_mid = {(e.src, e.dst): {r.rel for r in e.relations} for e in await store.edges("mid")}
    assert into_mid == {
        ("old", "mid"): {"superseded-by"},
        ("mid", "old"): {"supersedes"},
        ("mid", "new"): {"superseded-by"},
        ("new", "mid"): {"supersedes"},
    }
    assert await store.edges("loner") == []
    assert await store.edges("never-mentioned") == []
    assert [(e.src, e.dst) for e in await store.edges("beta")] == [("alpha", "beta")]


async def test_get_returns_a_defined_note_and_none_for_an_id_only_cited(harness: Harness) -> None:
    """A link target nothing defines is a node in the graph and never a note."""
    store = await _loaded(harness)
    note = await store.get("alpha")
    assert note is not None and note.type == "playbook" and note.tags == ["suzuki"]
    assert await store.get("missing-note") is None
    assert await store.get("reaction-eln.123") is None
    assert await store.get("no-such-id") is None


async def test_notes_lists_every_defined_note_once(harness: Harness) -> None:
    store = await _loaded(harness)
    assert sorted(note.id for note in await store.notes()) == sorted(note.id for note in _corpus())


async def test_related_follows_one_relation_one_way_and_honours_the_edge_window(
    harness: Harness,
) -> None:
    """`alpha uses beta` holds until March; the same edge read backwards is not `beta uses`."""
    store = await _loaded(harness)
    assert await store.related("alpha", "uses") == ["beta", "future"]
    assert await store.related("alpha", "uses", as_of=_SPRING) == ["beta", "future"]
    assert await store.related("alpha", "uses", as_of=date(2026, 5, 1)) == ["future"]
    assert await store.related("alpha", "precursor-of") == ["old"]
    assert await store.related("beta", "uses") == []
    assert await store.related("old", "superseded-by") == ["mid"]


async def test_related_and_neighborhood_refuse_an_id_no_note_defines(harness: Harness) -> None:
    """An unknown id is an error, because an empty answer would read as "nothing relates"."""
    store = await _loaded(harness)
    with pytest.raises(KeyError):
        await store.related("no-such-id", "uses")
    with pytest.raises(KeyError):
        await store.related("missing-note", "uses")
    with pytest.raises(KeyError):
        await store.neighborhood("no-such-id")


async def test_neighborhood_follows_links_both_ways_and_keeps_dangling_ids(
    harness: Harness,
) -> None:
    """One hop reaches what `alpha` cites and what cites it, dangling ids included; two reach on."""
    store = await _loaded(harness)
    assert await store.neighborhood("alpha") == {
        "beta",
        "future",
        "old",
        "missing-note",
        "reaction-eln.123",
    }
    assert await store.neighborhood("alpha", hops=2) == {
        "beta",
        "future",
        "old",
        "missing-note",
        "reaction-eln.123",
        "mid",
    }
    assert await store.neighborhood("beta") == {"alpha"}
    assert await store.neighborhood("loner") == set()
    assert await store.neighborhood("alpha", hops=0) == set()


async def test_current_successor_walks_a_supersede_chain_to_the_first_current_note(
    harness: Harness,
) -> None:
    """From either end of a link, past a successor already replaced, and never to itself."""
    store = await _loaded(harness)
    spring = await store.current_successor("old", _SPRING)
    assert spring is not None and spring.id == "mid"
    autumn = await store.current_successor("old", _AUTUMN)
    assert autumn is not None and autumn.id == "new"
    assert await store.current_successor("mid", _SPRING) is None
    on_the_day_it_took_over = await store.current_successor("mid", date(2026, 7, 1))
    assert on_the_day_it_took_over is not None and on_the_day_it_took_over.id == "new"
    assert await store.current_successor("alpha", _SPRING) is None
    assert await store.current_successor("no-such-id", _SPRING) is None


async def test_current_successor_ends_a_cycle_instead_of_walking_it(harness: Harness) -> None:
    """Two notes that replace each other have no current successor, and the walk terminates."""
    store = await _loaded(harness)
    assert await store.current_successor("cyclic-a", _SPRING) is not None  # b is current
    only = Note(
        id="retired-a",
        type="playbook",
        created_by="agent",
        valid_to=date(2026, 1, 1),
        relations=[Relation(rel="superseded-by", to="retired-b")],
    )
    other = Note(
        id="retired-b",
        type="playbook",
        created_by="agent",
        valid_to=date(2026, 1, 1),
        relations=[Relation(rel="superseded-by", to="retired-a")],
    )
    await harness.add(only, other)
    assert await harness.store.current_successor("retired-a", _SPRING) is None


async def test_search_matches_current_notes_by_substring_and_counts_what_it_searched(
    harness: Harness,
) -> None:
    """Coverage counts matched terms; a note not current on the date is not searched at all."""
    store = await _loaded(harness)
    found = await store.search(["suzuki", "solvent"], _SPRING)
    assert [(coverage, note.id) for coverage, note in found.matches] == [(2, "alpha"), (1, "beta")]
    # alpha, beta, mid, loner and the two cyclic notes: `old` ended; `new`, `future` not begun.
    assert found.searched == 6
    # Substring, so `solv` finds `solvent`; and `future` is found once it is current.
    later = await store.search(["solv"], date(2027, 6, 1))
    assert [note.id for _, note in later.matches] == ["alpha", "future"]
    assert (await store.search(["zzzz"], _SPRING)).matches == []


async def test_search_matches_pattern_characters_as_text_and_refuses_nothing_it_cannot_store(
    harness: Harness,
) -> None:
    """Pattern characters in a term are text, a term is not lowered, and a NUL matches nothing."""
    await harness.add(
        Note(id="literal", type="reaction", created_by="agent", body="50% yield at pH_7, C:\\tmp."),
        Note(id="plain", type="reaction", created_by="agent", body="500 yields at pHx7 in C:/tmp."),
    )

    async def hits(*terms: str) -> list[tuple[int, str]]:
        found = await harness.store.search(list(terms), _SPRING)
        return [(coverage, note.id) for coverage, note in found.matches]

    assert await hits("50%") == [(1, "literal")]
    assert await hits("ph_7") == [(1, "literal")]
    assert await hits("\\tmp") == [(1, "literal")]
    assert await hits("%") == [(1, "literal")]
    assert await hits("50%", "yield") == [(2, "literal"), (1, "plain")]
    assert await hits("PH_7") == [], "a term is matched as given, against a lowercased note"
    assert await hits("\x00") == []
    assert await hits("a\x00b", "ph_7") == [(1, "literal")]
    assert await hits("") == [(1, "literal"), (1, "plain")]


async def test_neighborhood_stops_at_the_hop_limit_on_a_ring_a_chain_and_a_self_link(
    harness: Harness,
) -> None:
    """A cycle does not run on, a chain reaches exactly `hops` far, and a self-link adds nothing."""
    ring = [
        Note(id=f"ring-{i}", type="playbook", created_by="agent", body=f"[[ring-{(i + 1) % 6}]]")
        for i in range(6)
    ]
    chain = [
        Note(
            id=f"chain-{i}",
            type="playbook",
            created_by="agent",
            body=f"[[chain-{i + 1}]]" if i < 9 else "end",
        )
        for i in range(10)
    ]
    loop = Note(id="loop", type="playbook", created_by="agent", body="[[loop]] and [[ring-0]]")
    await harness.add(*ring, *chain, loop)
    store = harness.store
    around_ring_0 = {
        0: set(),
        1: {"ring-1", "ring-5", "loop"},
        2: {"ring-1", "ring-5", "ring-2", "ring-4", "loop"},
        3: {f"ring-{i}" for i in range(1, 6)} | {"loop"},
        9: {f"ring-{i}" for i in range(1, 6)} | {"loop"},
    }
    for hops, expected in around_ring_0.items():
        assert await store.neighborhood("ring-0", hops) == expected, hops
    assert await store.neighborhood("loop", 1) == {"ring-0"}
    for hops in range(12):
        assert await store.neighborhood("chain-0", hops) == {
            f"chain-{i}" for i in range(1, min(hops, 9) + 1)
        }
    assert await store.neighborhood("chain-5", 2) == {f"chain-{i}" for i in (3, 4, 6, 7)}
    assert await store.neighborhood("chain-0", -1) == set()


async def test_an_agent_write_does_not_replace_a_humans_note(harness: Harness) -> None:
    """The curated note stays whatever the agent wrote; the refusal is a domain error."""
    human = Note(id="curated", type="playbook", created_by="human", body="Curated by a person.")
    await harness.add(human)
    with pytest.raises(ChemclawError):
        await harness.add(
            Note(id="curated", type="playbook", created_by="agent", body="An agent's rewrite.")
        )
    kept = await harness.store.get("curated")
    assert kept is not None and kept.created_by == "human" and kept.body == human.body


async def test_a_note_comes_back_as_its_file_text_reads_including_the_whitespace_it_drops(
    harness: Harness,
) -> None:
    """Both stores serve a note as the canonical file reads back, so a body is stripped in both."""
    written = Note(
        id="padded",
        type="reaction",
        created_by="agent",
        confidence=0.25,
        compound_smiles="CCO",
        tags=["a", "b"],
        body="\n\nBody with edges.  \n\n",
    )
    await harness.add(written)
    read = await harness.store.get("padded")
    assert read == written.model_copy(update={"body": "Body with edges."})


async def test_graph_has_a_node_per_note_and_one_per_dangling_target(harness: Harness) -> None:
    store = await _loaded(harness)
    graph = await store.graph()
    assert set(graph.nodes) == {note.id for note in _corpus()} | {
        "missing-note",
        "reaction-eln.123",
    }
    assert graph.has_edge("alpha", "beta") and graph.has_edge("mid", "old")
    relations = {relation.rel for relation in graph["alpha"]["beta"]["relations"]}
    assert relations == {"cites", "uses"}


async def test_fingerprints_cover_every_note_and_move_only_with_its_content(
    harness: Harness,
) -> None:
    """Keyed by id, spelt `sha256:<hex>`; an edit changes one entry and nothing else."""
    store = await _loaded(harness)
    before = await store.fingerprints()
    assert set(before) == {note.id for note in _corpus()}
    assert all(value.startswith("sha256:") for value in before.values())
    await harness.add(Note(id="beta", type="reaction", created_by="agent", body="Edited."))
    after = await store.fingerprints()
    assert {key for key in before if before[key] != after[key]} == {"beta"}


async def test_the_snapshot_token_and_the_signature_change_when_the_corpus_does(
    harness: Harness,
) -> None:
    """What a cache keys on moves on an edit and on an addition, and the notes come with it."""
    store = await _loaded(harness)
    token, notes = await store.snapshot()
    signature = await store.signature()
    assert len(notes) == len(_corpus()) and signature[0] == len(_corpus())
    await harness.add(Note(id="beta", type="reaction", created_by="agent", body="Edited."))
    edited_token, edited_notes = await store.snapshot()
    if token is not None:
        assert edited_token != token
    assert {n.id: n.body for n in edited_notes}["beta"] == "Edited."
    assert await store.signature() != signature
    await harness.add(Note(id="extra", type="reaction", created_by="agent", body="Added."))
    assert (await store.signature())[0] == len(_corpus()) + 1


async def test_the_bookkeeping_a_cache_asks_for_has_the_types_it_promises(harness: Harness) -> None:
    store = await _loaded(harness)
    arrivals = await store.arrivals()
    assert all(isinstance(day, date) for day in arrivals.values())
    revision = await store.revision()
    assert revision is None or revision >= 1
    assert await store.unparseable([]) == []
    assert await store.unparseable(["alpha"]) == []
    await store.refresh()
    await store.refresh(reparse=True)
    assert len(await store.notes()) == len(_corpus())


# --- What only the git store has ----------------------------------------------------------------


async def _git(root: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return await STORES["git"](root, monkeypatch)


async def test_the_git_store_dates_a_note_by_the_commit_that_added_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`arrivals` is the commit date in UTC, and `revision` the commit count behind the checkout."""
    harness = await _git(tmp_path, monkeypatch)
    await harness.add(*_corpus())
    today = datetime.now(UTC).date()
    arrivals = await harness.store.arrivals()
    assert {arrivals[note.id] for note in _corpus()} == {today}
    assert await harness.store.revision() == 2


async def test_the_git_store_does_not_see_an_outside_edit_until_it_is_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the scan window a changed file is invisible; `refresh` ends the window at once."""
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 3600.0)
    harness = await _git(tmp_path, monkeypatch)
    await harness.add(*_corpus())
    store = harness.store
    assert len(await store.notes()) == len(_corpus())
    extra = Note(id="outside", type="reaction", created_by="agent", body="Landed by a sync.")
    path = tmp_path / "clone" / "knowledge" / note_relative_path(extra.type, extra.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(extra), encoding="utf-8")
    assert await store.get("outside") is None
    await store.refresh()
    assert await store.get("outside") is not None


async def test_the_git_store_names_a_broken_note_file_and_not_a_file_that_was_never_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed note is skipped by the readers and reported here; a plain README is no note."""
    harness = await _git(tmp_path, monkeypatch)
    await harness.add(*_corpus())
    knowledge = tmp_path / "clone" / "knowledge"
    (knowledge / "reaction" / "broken.md").write_text("---\nid: [unterminated\n---\nbody", "utf-8")
    (knowledge / "README.md").write_text("# Notes\n\nNo frontmatter here.\n", encoding="utf-8")
    await harness.store.refresh(reparse=True)
    assert await harness.store.unparseable({"broken", "README", "alpha"}) == ["broken"]
    assert "broken" not in {note.id for note in await harness.store.notes()}


async def test_the_factory_returns_the_store_the_setting_names_over_the_directory_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kg_store` is `git`, so the store is the git one; `notes_dir` overrides the tree."""
    assert settings.kg_store == "git"
    assert isinstance(note_store(tmp_path), GitNoteStore)
    (tmp_path / "reaction").mkdir()
    note = Note(id="somewhere", type="reaction", body="A fixture tree.")
    (tmp_path / "reaction" / "somewhere.md").write_text(render_note(note), encoding="utf-8")
    assert [n.id for n in await note_store(tmp_path).notes()] == ["somewhere"]
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", ".")
    assert [n.id for n in await note_store().notes()] == ["somewhere"]


# --- A batch of lookups is one read of the corpus ------------------------------------------------


@pytest.fixture
def corpus_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    """Count reads of the corpus over a small directory the settings point at, uncached.

    With the cache off, each read parses every file, which is the cost a per-id lookup multiplies.
    """
    write_notes(tmp_path, *_corpus())
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", ".")
    monkeypatch.setattr(settings, "graph_cache_enabled", False)
    reads: list[Path] = []
    real = corpus.cached_notes

    def counting(notes_dir: Path) -> Any:
        reads.append(notes_dir)
        return real(notes_dir)

    monkeypatch.setattr(corpus, "cached_notes", counting)
    return reads


async def test_get_many_reads_the_corpus_once_however_many_ids_it_is_given(
    corpus_reads: list[Path],
) -> None:
    store = note_store()
    found = await store.get_many(["alpha", "beta", "old", "mid", "new", "missing-note"])
    assert len(corpus_reads) == 1
    assert found["mid"] is not None and found["missing-note"] is None
    await store.get_many([])
    assert len(corpus_reads) == 2, "an empty batch still reads, so an unreadable corpus fails"


async def test_a_premise_check_reads_the_corpus_once(corpus_reads: list[Path]) -> None:
    from chemclaw.kg.premise import _breaks

    broken = await _breaks(
        ["alpha", "old", "new", "future", "missing-note", "reaction-eln.1"], _SPRING
    )
    assert len(corpus_reads) == 1
    assert {(b.note_id, b.reason) for b in broken} == {
        ("old", "retired"),
        ("new", "not-yet-valid"),
        ("future", "not-yet-valid"),
        ("missing-note", "absent"),
    }


async def test_condensing_protocols_reads_the_corpus_once(
    corpus_reads: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from chemclaw.agent import protocol_tools
    from chemclaw.agent.condense import Condensation

    seen: list[int] = []

    async def condensed(protocols: Any) -> Condensation:
        seen.append(len(protocols))
        return Condensation(table="t", complete=True)

    monkeypatch.setattr(protocol_tools, "_condense", condensed)
    await protocol_tools.condense_protocols(["alpha", "beta", "loner", "old"])
    assert seen == [4]
    assert len(corpus_reads) == 1


async def test_grounding_a_job_reads_the_corpus_once_for_all_its_subjects_and_once_for_none(
    corpus_reads: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One read before the loop even with no subjects, so a corpus error still surfaces."""
    from chemclaw.durable.hypothesis_tournament import ground_check_job
    from chemclaw.hypotheses.models import CheckCall, DiscriminatingCheck

    monkeypatch.setattr("chemclaw.connectors.registry.find_job", lambda name: (object(), object()))

    def check(subjects: dict[str, list[str]]) -> DiscriminatingCheck:
        return DiscriminatingCheck(
            hypothesis_id="h",
            question="q",
            kind="computable",
            call=CheckCall(job="a-job", subjects=subjects),
            expectation="e",
        )

    grounded = await ground_check_job(check({"reactants": ["alpha", "beta", "no-such-id"]}))
    assert grounded.refusal_code, "a subject that is no compound note is a reported refusal"
    assert len(corpus_reads) == 1

    def unreadable(notes_dir: Path) -> Any:
        raise RuntimeError("the corpus cannot be read")

    monkeypatch.setattr(corpus, "cached_notes", unreadable)
    refused = await ground_check_job(check({}))
    assert refused.refusal_code == "job-refused"
    assert "cannot be read" in refused.refusal_detail


# --- The store never blocks the event loop -------------------------------------------------------


async def test_every_read_of_the_store_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each coroutine does its file and graph work in a worker thread.

    Spied on the `kg.graph` entry points the store delegates to, so a method that called one
    inline, or a new one that forgot the hop, is named. The spies record the thread and call
    through.
    """
    write_notes(tmp_path, *_corpus())
    threads: dict[str, set[int]] = {}

    def spy(name: str) -> None:
        real = getattr(corpus, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            threads.setdefault(name, set()).add(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(corpus, name, wrapper)

    real_scan = corpus.scan_notes_dir

    def scan(directory: Path) -> Any:
        threads.setdefault("scan_notes_dir", set()).add(threading.get_ident())
        yield from real_scan(directory)

    entry_points = (
        "build_graph",
        "load_notes",
        "cached_notes",
        "note_arrivals",
        "note_file_fingerprints",
        "corpus_revision",
        "invalidate_cache",
    )
    for name in entry_points:
        spy(name)
    monkeypatch.setattr(corpus, "scan_notes_dir", scan)

    store = GitNoteStore(tmp_path)
    await store.get("alpha")
    await store.get_many(["alpha"])
    await store.notes()
    await store.related("alpha", "uses")
    await store.neighborhood("alpha")
    await store.edges("alpha")
    await store.current_successor("old", _SPRING)
    await store.search(["suzuki"], _SPRING)
    await store.graph()
    await store.snapshot()
    await store.signature()
    await store.arrivals()
    await store.fingerprints()
    await store.unparseable(["alpha"])
    await store.revision()
    await store.refresh(reparse=True)

    assert set(threads) == {*entry_points, "scan_notes_dir"}
    loop_thread = threading.get_ident()
    on_the_loop = sorted(name for name, ids in threads.items() if loop_thread in ids)
    assert not on_the_loop, f"{on_the_loop} ran on the event loop"
