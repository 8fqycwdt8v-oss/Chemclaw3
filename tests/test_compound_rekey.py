"""A `STANDARDIZATION_VERSION` bump that moves a compound's id re-keys it rather than orphaning it.

`D-2026-09-27-a-compound-id-a-bump-moves-is-superseded-not-orphaned`. The corpus is built under the
previous version for real (`std11` is `std12` without `_IONISABLE_NEUTRAL_ACIDS`, so emptying that
table and clearing the cache reproduces it), then carried across by the production entry point
`rekey_standardization`, writing through `kg.record.record_note`.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path

import networkx as nx
import pytest

import chemclaw.core.chem as chem
from chemclaw.agent.graph_tools import expand_note
from chemclaw.cli import rekey_compounds
from chemclaw.core.chem import STANDARDIZATION_VERSION, compound_id, standard_smiles
from chemclaw.core.config import settings
from chemclaw.ingest.eln.compound import compound_dependencies, compound_note
from chemclaw.kg.graph import (
    build_graph,
    current_successor,
    invalidate_cache,
    load_notes,
    note_in,
)
from chemclaw.kg.note import Note, Relation, note_relative_path
from chemclaw.kg.record import NoteWrite, WriteOutcome
from chemclaw.kg.render import render_note
from chemclaw.memory.compound_rekey import (
    StandardizationRekeyReport,
    plan_compound_rekey,
    rekey_standardization,
)
from chemclaw.retrieval.retrievers import GraphRetriever
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.search import find_similar_molecules
from chemclaw.science.fingerprints.rekey import (
    FingerprintRekeyCounts,
    rebuild_molecule,
    rebuild_reaction,
    rekey_fingerprints,
)
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring, reaction_definition
from chemclaw.science.fingerprints.store import FingerprintRecord, InMemoryFingerprintStore

_PREVIOUS = "std11"
#: The last day an old id was current. `valid_to` is inclusive, so a note closed *today* is still
#: current until midnight — so the command passes yesterday, and these tests a fixed past day.
_RETIRED_ON = date(2026, 9, 1)

#: Ethylammonium perchlorate in both spellings, and two salts written only neutral whose free bases
#: have no note yet — on three different amines, since every ethylamine salt is one compound now.
_NEUTRAL = "CCN.OCl(=O)(=O)=O"
_IONIC = "CC[NH3+].[O-]Cl(=O)(=O)=O"
_BORIC = "c1ccncc1.OB(O)O"
_SULFAMIC = "CN.NS(=O)(=O)O"


@contextmanager
def _previous_version() -> Iterator[None]:
    """Standardize as `std11` did: without the table of ionisable neutral acids."""
    saved = chem._IONISABLE_NEUTRAL_SPECTATORS
    chem._standardized.cache_clear()
    chem._IONISABLE_NEUTRAL_SPECTATORS = frozenset()
    try:
        yield
    finally:
        chem._IONISABLE_NEUTRAL_SPECTATORS = saved
        chem._standardized.cache_clear()


def _write(root: Path, note: Note) -> None:
    """Put `note` where `record_note` would, under `root/knowledge`."""
    path = root / "knowledge" / note_relative_path(note.type, note.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()


class _DiskWriter:
    """A `NoteWriter` that lands files on disk the way the git writer does, minus git.

    `overwrite=False` leaves an existing file alone and an amendment of a person's note is refused,
    which are the two rules of `git_writer` the re-key's counts depend on.
    """

    def __init__(self, root: Path) -> None:
        """Write under `root`, the note repository."""
        self.root = root
        self.writes: list[NoteWrite] = []

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Write each file in order; report how many subjects changed."""
        self.writes.append(write)
        changed = 0
        for file in write.files:
            path = self.root / file.path
            if path.exists() and (not file.overwrite or "created_by: human" in path.read_text()):
                continue
            if path.exists() and path.read_text(encoding="utf-8") == file.content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.content, encoding="utf-8")
            changed += 1
        # What `git_writer` does after every commit, so a reader in this process sees the write.
        invalidate_cache()
        return WriteOutcome(reference=f"disk://{len(self.writes)}", notes=min(changed, 1))


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A knowledge tree written under `std11`, and the ids it was written under.

    Every planner branch has a member: two old spellings onto an id that already has a note, one
    onto an id with none, a person-written note, a slug-named seed note, and a job result citing an
    old id with a typed edge.
    """
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    with _previous_version():
        neutral, ionic = compound_note(_NEUTRAL), compound_note(_IONIC)
        boric = compound_note(_BORIC)
        sulfamic = compound_note(_SULFAMIC).model_copy(update={"created_by": "human"})
        neutral_standard = standard_smiles(_NEUTRAL)
    assert neutral.id != ionic.id, "the corpus has to hold the defect the bump fixes"
    job = Note(
        id="job-perchlorate",
        type="job-result",
        created_by="agent",
        compound_smiles=neutral_standard,
        body=f"Computed on [[{neutral.id}]].",
        relations=[Relation(rel="computed-from", to=neutral.id)],
    )
    slug = Note(
        id="compound-thf",
        type="compound",
        compound_smiles="C1CCOC1",
        created_by="human",
        body="THF",
    )
    for note in (neutral, ionic, boric, sulfamic, job, slug):
        _write(tmp_path, note)
    return {
        "neutral": neutral.id,
        "ionic": ionic.id,
        "boric": boric.id,
        "sulfamic": sulfamic.id,
    }


def _rekey(tmp_path: Path, *, apply: bool) -> StandardizationRekeyReport:
    """The production entry point over this corpus, with empty fingerprint indexes."""
    return asyncio.run(
        rekey_standardization(
            apply=apply,
            notes_dir=settings.knowledge_path,
            writer=lambda: _DiskWriter(tmp_path),
            molecule_store=InMemoryFingerprintStore(molecule_definition()),
            reaction_store=InMemoryFingerprintStore(reaction_definition()),
            as_of=_RETIRED_ON,
        )
    )


def _tree(root: Path) -> dict[str, str]:
    """Every file under `root/knowledge`, by relative path."""
    base = root / "knowledge"
    return {str(path.relative_to(base)): path.read_text() for path in sorted(base.rglob("*.md"))}


def test_the_bump_moved_exactly_the_spellings_the_table_names(corpus: dict[str, str]) -> None:
    """The precondition, measured: `std12` puts both perchlorate spellings on the free base's id.

    Without it every later assertion is about a corpus in which nothing moved.
    """
    assert STANDARDIZATION_VERSION == "std12"
    assert compound_id(_NEUTRAL) == compound_id(_IONIC) == compound_id("CCN") == corpus["ionic"]
    assert compound_id(_BORIC) != corpus["boric"]


def test_a_citation_to_a_pre_bump_id_resolves_to_the_post_bump_note(
    tmp_path: Path, corpus: dict[str, str]
) -> None:
    """`expand_note` on the old id returns the current note, and says it was redirected."""
    _rekey(tmp_path, apply=True)
    for old in (corpus["neutral"], corpus["boric"]):
        view = asyncio.run(expand_note(old))
        assert view.note.id != old
        assert view.note.valid_to is None
        assert view.body.startswith(f"{old} is a superseded id for this compound")
    assert asyncio.run(expand_note(corpus["neutral"])).note.id == corpus["ionic"]
    assert asyncio.run(expand_note(corpus["boric"])).note.id == compound_id(_BORIC)


def test_a_note_citing_the_old_id_keeps_its_compound_neighbour_and_its_typed_edge(
    tmp_path: Path, corpus: dict[str, str]
) -> None:
    """A note citing the old id keeps its compound neighbour and its typed edge.

    After the re-key the old note is not current, so the neighbour is reported as the note that
    superseded it, carrying the `computed-from` edge typed against the old id.
    """
    _rekey(tmp_path, apply=True)
    view = asyncio.run(expand_note("job-perchlorate"))
    (neighbour,) = view.neighbors
    assert neighbour.id == corpus["ionic"]
    assert neighbour.relations_out == ["computed-from"]


def test_no_link_in_the_corpus_is_left_pointing_at_nothing_current(
    tmp_path: Path, corpus: dict[str, str]
) -> None:
    """Every link to a compound resolves to a current compound note, directly or through a chain."""
    _rekey(tmp_path, apply=True)
    graph = build_graph(settings.knowledge_path)
    for note in load_notes(settings.knowledge_path):
        if not note.is_current(date.today()):
            continue
        for target in note.outgoing_links():
            if not target.startswith("compound-"):
                continue
            reached = note_in(graph, target)
            if reached is None or not reached.is_current(date.today()):
                reached = current_successor(graph, target, date.today())
            # A person's note is left current, so it resolves as itself.
            assert reached is not None and reached.is_current(date.today()), (note.id, target)


def test_a_pre_bump_note_re_recorded_still_carries_its_compound(corpus: dict[str, str]) -> None:
    """`compound_dependencies` used to return `[]` here, so the note landed without its compound."""
    job = next(n for n in load_notes(settings.knowledge_path) if n.id == "job-perchlorate")
    (dependency,) = compound_dependencies(job)
    assert dependency.id == corpus["ionic"]


def test_the_old_notes_are_retired_in_place_and_the_successor_names_them(
    tmp_path: Path, corpus: dict[str, str]
) -> None:
    """The write is the supersede machinery's, in `kg/record.py`'s order, and deletes nothing."""
    before = set(_tree(tmp_path))
    _rekey(tmp_path, apply=True)
    assert before <= set(_tree(tmp_path)), "a re-key deletes nothing"
    notes = {note.id: note for note in load_notes(settings.knowledge_path)}
    old = notes[corpus["neutral"]]
    assert old.valid_to == _RETIRED_ON
    assert Relation(rel="superseded-by", to=corpus["ionic"]) in old.relations
    successor = notes[corpus["ionic"]]
    assert successor.valid_to is None
    assert Relation(rel="supersedes", to=corpus["neutral"]) in successor.relations
    # A person's note is superseded by name and never closed in place.
    sulfamic = notes[corpus["sulfamic"]]
    assert sulfamic.valid_to is None
    assert Relation(rel="supersedes", to=corpus["sulfamic"]) in (
        notes[compound_id(_SULFAMIC)].relations
    )
    # And the slug-named seed note is not a candidate at all.
    assert notes["compound-thf"].valid_to is None


def test_the_preview_counts_what_the_apply_writes_and_a_second_apply_writes_nothing(
    tmp_path: Path, corpus: dict[str, str]
) -> None:
    """Dry run by default, idempotent, and the two report the same numbers."""
    untouched = _tree(tmp_path)
    preview = _rekey(tmp_path, apply=False)
    assert _tree(tmp_path) == untouched, "a preview wrote"
    applied = _rekey(tmp_path, apply=True)
    assert preview.notes == applied.notes
    assert applied.notes == {
        "examined": 5,
        "unchanged": 1,
        "not_structural": 1,
        "unreadable": 0,
        "cyclic": 0,
        "successors": 3,
        "retired": 2,
        "kept_current_human": 1,
        "blocked_successor": 0,
    }
    after = _tree(tmp_path)
    again = _rekey(tmp_path, apply=True)
    assert _tree(tmp_path) == after
    assert again.notes["successors"] == again.notes["retired"] == 0


@pytest.mark.parametrize(
    "person", [{"created_by": "human"}, {"actor": "oid-of-a-chemist"}], ids=["wrote", "written-for"]
)
def test_a_successor_a_person_wrote_or_was_written_for_is_not_recorded_onto(
    tmp_path: Path, corpus: dict[str, str], person: dict[str, str]
) -> None:
    """`record_note` refuses both from an operator's run, so the plan does not offer either."""
    ionic = next(n for n in load_notes(settings.knowledge_path) if n.id == corpus["ionic"])
    _write(tmp_path, ionic.model_copy(update=person))
    plan = plan_compound_rekey(load_notes(settings.knowledge_path), _RETIRED_ON)
    assert plan.blocked == [corpus["ionic"]]
    assert corpus["ionic"] not in {rekey.successor.id for rekey in plan.rekeys}


def test_retrieval_serves_the_successor_and_not_the_retired_spelling(
    tmp_path: Path, corpus: dict[str, str]
) -> None:
    """Current-evidence retrieval sees one note for the substance after the re-key, not two."""
    before = asyncio.run(GraphRetriever(str(settings.knowledge_path)).retrieve("Compound", {}))
    assert corpus["neutral"] in {chunk.source_note_id for chunk in before}
    _rekey(tmp_path, apply=True)
    after = asyncio.run(GraphRetriever(str(settings.knowledge_path)).retrieve("Compound", {}))
    served = {chunk.source_note_id for chunk in after}
    assert corpus["neutral"] not in served
    assert corpus["ionic"] in served


def test_the_command_previews_unless_told_to_apply(
    tmp_path: Path, corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare command writes nothing, and its report names every count it would write."""
    monkeypatch.setattr(rekey_compounds, "default_molecule_store", InMemoryFingerprintStore)
    monkeypatch.setattr(rekey_compounds, "default_reaction_store", InMemoryFingerprintStore)
    monkeypatch.setattr(rekey_compounds, "default_writer", lambda: _DiskWriter(tmp_path))
    untouched = _tree(tmp_path)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", printed.append)
    assert rekey_compounds.main([]) == 0
    assert _tree(tmp_path) == untouched
    assert printed[0].startswith("PREVIEW")
    assert "        3  successors" in printed[0]
    assert rekey_compounds.main(["--apply"]) == 0
    assert _tree(tmp_path) != untouched


# --- the superseded generation (#526) ------------------------------------------------------------


def test_the_disposal_is_refused_without_apply() -> None:
    """A preview rebuilds nothing, so it may not delete what it would have rebuilt."""
    with pytest.raises(SystemExit) as refused:
        rekey_compounds.main(["--dispose-superseded"])
    assert refused.value.code == 2
    with pytest.raises(ValueError, match="preview"):
        preview = StandardizationRekeyReport(
            applied=False,
            notes={},
            molecules=FingerprintRekeyCounts(),
            reactions=FingerprintRekeyCounts(),
        )
        asyncio.run(rekey_compounds.settle_indexes(preview))


def test_the_command_disposes_only_when_asked_and_only_after_the_apply(
    tmp_path: Path, corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--apply` alone keeps the shelf; `--dispose-superseded` settles each index after the re-key.

    The disposal is recorded rather than run, pinning the wiring (tables, definitions, ordering);
    `settle_index` itself is driven against a real table in `tests/test_live_index.py`.
    """
    monkeypatch.setattr(rekey_compounds, "default_molecule_store", InMemoryFingerprintStore)
    monkeypatch.setattr(rekey_compounds, "default_reaction_store", InMemoryFingerprintStore)
    monkeypatch.setattr(rekey_compounds, "default_writer", lambda: _DiskWriter(tmp_path))
    disposed: list[tuple[str, str]] = []

    async def record(table: str, definition: str) -> int:
        disposed.append((table, definition))
        return 4

    monkeypatch.setattr(rekey_compounds, "dispose_superseded", record)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", printed.append)

    assert rekey_compounds.main(["--apply"]) == 0
    assert disposed == [], "an apply without --dispose-superseded deleted a generation"

    assert rekey_compounds.main(["--apply", "--dispose-superseded"]) == 0
    assert disposed == [
        ("reaction_fingerprints", reaction_definition()),
        ("molecule_fingerprints", molecule_definition()),
    ]
    assert printed[-1].splitlines()[1:] == [
        "superseded generations:",
        "  reaction fingerprints: 0 re-fingerprinted, 0 already current, "
        "4 superseded row(s) disposed of",
        "  molecule fingerprints: 0 re-fingerprinted, 0 already current, "
        "4 superseded row(s) disposed of",
    ]


def test_the_disposal_connects_as_the_schema_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disposal connects as the schema owner, never the runtime role.

    The runtime credential has no `DELETE` on these tables (`tests/test_database_privileges.py`).
    """
    seen: list[str] = []

    class _Cursor:
        rowcount = 3

    class _Conn:
        async def execute(self, statement: str, params: dict[str, str]) -> _Cursor:
            assert statement.startswith("DELETE FROM molecule_fingerprints WHERE definition <>")
            return _Cursor()

    @asynccontextmanager
    async def connection(dsn: str, *, operation: str) -> AsyncIterator[_Conn]:
        seen.append(dsn)
        yield _Conn()

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://runtime@db/chemclaw")
    monkeypatch.setattr(settings, "postgres_migration_dsn", "postgresql://owner@db/chemclaw")
    monkeypatch.setattr(rekey_compounds, "db_connection", connection)
    removed = asyncio.run(rekey_compounds.dispose_superseded("molecule_fingerprints", "ecfp:x"))
    assert removed == 3
    assert seen == ["postgresql://owner@db/chemclaw"]


# --- the fingerprint half ------------------------------------------------------------------------


def _shelved_molecule(smiles: str) -> FingerprintRecord:
    """A molecule row as `std11` wrote it: its structure standardized then, under `std11`."""
    with _previous_version():
        standard = standard_smiles(smiles)
    return FingerprintRecord(
        id=standard,
        label=standard,
        bits=ecfp_bitstring(standard),
        definition=molecule_definition().replace(STANDARDIZATION_VERSION, _PREVIOUS),
    )


def _shelved_reaction(record_id: str, smiles: str) -> FingerprintRecord:
    """A reaction row as `std11` wrote it: the ingested label, under the old definition."""
    return FingerprintRecord(
        id=record_id,
        label=smiles,
        bits=drfp_bitstring(smiles),
        definition=reaction_definition().replace(STANDARDIZATION_VERSION, _PREVIOUS),
        source="eln-ord",
    )


def test_shelved_molecule_rows_are_re_keyed_onto_the_current_ids() -> None:
    """Both perchlorate spellings were two rows; they are one searchable row under `std12`."""
    store = InMemoryFingerprintStore(molecule_definition())
    for smiles in (_NEUTRAL, _IONIC, _BORIC):
        asyncio.run(store.add(_shelved_molecule(smiles)))
    assert asyncio.run(store.count()) == 0, "a bump shelves every row"

    preview = asyncio.run(
        rekey_fingerprints(store, molecule_definition(), rebuild_molecule, apply=False)
    )
    assert asyncio.run(store.count()) == 0, "a preview wrote"
    applied = asyncio.run(
        rekey_fingerprints(store, molecule_definition(), rebuild_molecule, apply=True)
    )
    assert preview == applied
    assert (applied.examined, applied.rekeyed, applied.already_current) == (3, 2, 1)
    assert applied.moved == 2, "the neutral perchlorate and the boric-acid salt changed identity"
    assert asyncio.run(store.count()) == 2
    hits = asyncio.run(find_similar_molecules(store, _NEUTRAL, top_k=1, threshold=1.0)).hits
    assert [hit.compound_note_id for hit in hits] == [compound_id("CCN")]

    again = asyncio.run(
        rekey_fingerprints(store, molecule_definition(), rebuild_molecule, apply=True)
    )
    assert again.rekeyed == 0


def test_shelved_reaction_rows_keep_their_source_and_id() -> None:
    """A reaction row's key is the ELN's, so the re-key moves only its definition."""
    store = InMemoryFingerprintStore(reaction_definition())
    asyncio.run(store.add(_shelved_reaction("EXP-1", "CCN.OCl(=O)(=O)=O>>CCNC(C)=O")))
    counts = asyncio.run(
        rekey_fingerprints(store, reaction_definition(), rebuild_reaction, apply=True)
    )
    assert counts.rekeyed == 1
    (record,) = asyncio.run(store.all_records())
    assert (record.source, record.id, record.definition) == (
        "eln-ord",
        "EXP-1",
        reaction_definition(),
    )


# --- following the link -------------------------------------------------------------------------


def _chain(*edges: tuple[str, str, str]) -> nx.DiGraph:
    """A graph of compound notes, `(source, rel, target)` per edge; sources are notes."""
    graph: nx.DiGraph = nx.DiGraph()
    for source, rel, target in edges:
        graph.add_node(
            source,
            note=Note(
                id=source,
                type="compound",
                valid_to=_RETIRED_ON if rel == "superseded-by" else None,
            ),
        )
        graph.add_edge(source, target, relations=(Relation(rel=rel, to=target),))
    return graph


def test_a_chain_of_bumps_is_followed_to_the_note_current_now() -> None:
    """`std10` -> `std11` -> `std12`: an id two bumps old still reaches today's note."""
    graph = _chain(
        ("compound-a", "superseded-by", "compound-b"), ("compound-b", "superseded-by", "compound-c")
    )
    graph.add_node("compound-c", note=Note(id="compound-c", type="compound"))
    successor = current_successor(graph, "compound-a", date.today())
    assert successor is not None and successor.id == "compound-c"


def test_an_id_no_note_defines_resolves_through_the_note_that_supersedes_it() -> None:
    """The alias half: only the replacement's `supersedes` exists, which is enough."""
    graph = _chain(("compound-new", "supersedes", "compound-gone"))
    successor = current_successor(graph, "compound-gone", date.today())
    assert successor is not None and successor.id == "compound-new"


def test_a_supersede_cycle_ends_the_walk() -> None:
    """Two retired notes naming each other have no current successor, and the walk terminates."""
    graph = _chain(
        ("compound-a", "superseded-by", "compound-b"), ("compound-b", "superseded-by", "compound-a")
    )
    assert current_successor(graph, "compound-a", date.today()) is None


def test_a_target_that_is_itself_moving_is_followed_to_the_end_of_the_chain() -> None:
    """A lands on B's id while B's structure moves B on to C: both are superseded by C, once each.

    Planned pairwise, B was written twice in one pass — as A's successor and as C's retirement —
    and the second write discarded the `supersedes` edge the first had added.
    """
    b_id = compound_id("CCO")
    a = Note(id="compound-000000000001", type="compound", compound_smiles="CCO", created_by="agent")
    b = Note(id=b_id, type="compound", compound_smiles="CCN", created_by="agent")
    plan = plan_compound_rekey([a, b], _RETIRED_ON)
    (rekey,) = plan.rekeys
    assert rekey.successor.id == compound_id("CCN")
    assert {r.to for r in rekey.successor.relations if r.rel == "supersedes"} == {a.id, b_id}
    assert sorted(note.id for note in rekey.retired) == sorted([a.id, b_id])
    written = [rekey.successor.id, *(note.id for note in rekey.retired)]
    assert len(written) == len(set(written)), "one note written twice in one pass"


def test_a_cycle_of_moves_is_left_alone() -> None:
    """Two notes each filed under the other's structure id have no end to supersede onto."""
    a = Note(id=compound_id("CCN"), type="compound", compound_smiles="CCO", created_by="agent")
    b = Note(id=compound_id("CCO"), type="compound", compound_smiles="CCN", created_by="agent")
    plan = plan_compound_rekey([a, b], _RETIRED_ON)
    assert plan.rekeys == [] and plan.cyclic == 2
