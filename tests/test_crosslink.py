"""Crosslinking the calculation store and the knowledge graph, in both directions (STO-7).

The gap: the removed DFT bundle's note builder documented that it *could not* wikilink the
compound its
result was about, because a dangling link fails `kg-validate` on the very PR that adds the note.
The consequence was that the two halves of the system's memory — what we computed and what we know
— were disjoint stores with no reference between them in either direction.

The fix is not an index. It is letting a submission carry a note *with its dependencies*, so the
link and its target land in one reviewable unit.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.core.chem import compound_id
from chemclaw.ingest.eln.compound import compound_dependencies, compound_note
from chemclaw.kg.crosslink import calc_ref_index, cited_calculations, notes_for_calculation
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.record import NoteWrite, WriteOutcome, record_note
from chemclaw.kg.render import render_note
from chemclaw.kg.validate import validate

_KEY = "xtb.hess@GFN2-xTB+tblite+0.4.0:ab12cd:34ef56"
_OTHER_KEY = "xtb.opt@GFN2-xTB+tblite+0.4.0:9988aa:112233"


class _Capturing:
    """A `NoteWriter` that keeps the write instead of committing it."""

    def __init__(self) -> None:
        """Start with nothing captured."""
        self.captured: NoteWrite | None = None

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Record the write and return a stub commit reference."""
        self.captured = write
        return WriteOutcome(reference="commit://1")


def test_a_note_may_cite_a_calculation_that_lives_outside_the_graph() -> None:
    """`calc_refs` is a frontmatter field, not a wikilink, and that is deliberate.

    A calculation key names a row in Postgres. Making it an edge would mean every computed note
    has a dangling link by construction — the exact failure this stage removes, reintroduced from
    the other side.
    """
    note = Note(id="n", type="job-result", calc_refs=[_KEY], artifact_refs=[f"{_KEY}#hessian"])
    assert note.calc_refs == [_KEY]
    # The graph sees no edge for either — they point out of it.
    assert note.outgoing_links() == []


@pytest.mark.parametrize(
    "bad",
    ["the GFN2 run", "xtb.hess@v1", "xtb.hess@v1:nothex:0011", "xtb.hess:0011:2233"],
)
def test_a_calc_ref_that_is_not_a_calculation_key_is_refused(bad: str) -> None:
    """Prose in this field is a crosslink nothing can resolve, so it fails at the schema.

    The whole value of the field is that a machine can follow it. `"the GFN2 run"` looks like
    provenance and is not, and a note carrying it would pass review looking perfectly informative.
    """
    with pytest.raises(ValueError, match="not a calculation key"):
        Note(id="n", type="job-result", calc_refs=[bad])


def test_an_artifact_ref_must_name_both_a_calculation_and_an_artifact() -> None:
    """`<calc key>#<name>` — a bare key is not an artifact reference."""
    with pytest.raises(ValueError, match="not an artifact reference"):
        Note(id="n", type="job-result", artifact_refs=[_KEY])
    with pytest.raises(ValueError, match="not an artifact reference"):
        Note(id="n", type="job-result", artifact_refs=[f"{_KEY}#"])
    # The valid form is accepted.
    assert Note(id="n", type="job-result", artifact_refs=[f"{_KEY}#hessian"]).artifact_refs


def test_an_artifact_citation_implies_a_citation_of_the_run_that_produced_it() -> None:
    """A note citing only a Hessian is still found by a question about its calculation."""
    note = Note(id="n", type="job-result", artifact_refs=[f"{_KEY}#hessian"])
    assert cited_calculations(note) == [_KEY]


def test_the_reverse_lookup_finds_every_note_resting_on_one_calculation() -> None:
    """The direction that makes a recomputation actionable.

    When a method version changes and a cached result is invalidated, this is what says which
    conclusions now rest on something the system would no longer reproduce.
    """
    first = Note(id="a", type="job-result", calc_refs=[_KEY])
    second = Note(id="b", type="report", artifact_refs=[f"{_KEY}#vibspectrum"])
    unrelated = Note(id="c", type="report", calc_refs=[_OTHER_KEY])

    index = calc_ref_index([first, second, unrelated])
    assert sorted(note.id for note in index[_KEY]) == ["a", "b"]
    assert [note.id for note in index[_OTHER_KEY]] == ["c"]


def test_the_reverse_lookup_reads_the_note_tree(tmp_path: Path) -> None:
    """End to end over a real directory, through the shared parsed-note cache."""
    directory = tmp_path / "knowledge" / "job-result"
    directory.mkdir(parents=True)
    (directory / "a.md").write_text(
        render_note(Note(id="a", type="job-result", calc_refs=[_KEY])), encoding="utf-8"
    )
    (directory / "b.md").write_text(
        render_note(Note(id="b", type="job-result", calc_refs=[_OTHER_KEY])), encoding="utf-8"
    )
    invalidate_cache()
    assert [note.id for note in notes_for_calculation(tmp_path / "knowledge", _KEY)] == ["a"]


def test_a_note_and_the_compound_it_links_land_in_one_write() -> None:
    """The actual unblocking change: a reviewable unit is a note *and what it needs*.

    Before this a `NoteWrite` was one path and one content, which is why a note could never
    link a note that did not already exist on the base branch.
    """

    async def _run() -> None:
        smiles = "CCO"
        note = Note(
            id="job-1",
            type="job-result",
            compound_smiles=smiles,
            created_by="agent",
            body=f"Computed for [[{compound_id(smiles)}]].",
        )
        submitter = _Capturing()
        await record_note(
            note, submitter, knowledge_dir="knowledge", dependencies=compound_dependencies(note)
        )

        assert submitter.captured is not None
        paths = [file.path for file in submitter.captured.files]
        # The compound is written **before** the note that cites it: a reader scanning mid-write
        # must never meet a note whose `[[wikilink]]` dangles
        # (`D-2026-09-05-the-gate-is-deleted-not-dormant`). Under the PR-gate both files merged in
        # one commit, so the order was free and the subject came first.
        assert paths == [
            f"knowledge/compound/{compound_id(smiles)}.md",
            "knowledge/job-result/job-1.md",
        ]

    asyncio.run(_run())


def test_that_submission_passes_kg_validate(tmp_path: Path) -> None:
    """The claim the old comment doubted, checked against the validator itself.

    That bundle's note builder avoided the link because it would fail validation. Write both
    files of the submission to disk and run the real validator over them: no dangling link.
    """

    async def _run() -> None:
        smiles = "CCO"
        note = Note(
            id="job-1",
            type="job-result",
            compound_smiles=smiles,
            created_by="agent",
            body=f"Computed for [[{compound_id(smiles)}]].",
        )
        submitter = _Capturing()
        await record_note(
            note, submitter, knowledge_dir="knowledge", dependencies=compound_dependencies(note)
        )
        assert submitter.captured is not None
        for file in submitter.captured.files:
            path = tmp_path / file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.content, encoding="utf-8")

        assert validate(tmp_path / "knowledge") == []

    asyncio.run(_run())


def test_the_note_alone_would_not_have_passed(tmp_path: Path) -> None:
    """The negative control, so the test above is measuring something.

    Without its dependency the same note has a dangling link — which is precisely the failure the
    old code avoided by not linking at all.
    """
    smiles = "CCO"
    note = Note(
        id="job-1",
        type="job-result",
        compound_smiles=smiles,
        created_by="agent",
        body=f"Computed for [[{compound_id(smiles)}]].",
    )
    directory = tmp_path / "knowledge" / "job-result"
    directory.mkdir(parents=True)
    (directory / "job-1.md").write_text(render_note(note), encoding="utf-8")
    problems = validate(tmp_path / "knowledge")
    assert any("unknown note" in problem for problem in problems)


def test_a_dependency_is_not_duplicated_however_many_times_it_is_named() -> None:
    """Writing one path twice in a commit is noise at best and a race at worst."""

    async def _run() -> None:
        note = Note(id="n", type="job-result", created_by="agent", body="[[compound-x]]")
        duplicate = compound_note("CCO")
        submitter = _Capturing()
        await record_note(
            note,
            submitter,
            knowledge_dir="knowledge",
            dependencies=[duplicate, duplicate, note],
        )
        assert submitter.captured is not None
        paths = [file.path for file in submitter.captured.files]
        assert len(paths) == len(set(paths)) == 2  # the note, and one copy of the compound

    asyncio.run(_run())


def test_a_note_that_does_not_link_its_compound_brings_nothing_along() -> None:
    """The rule is "a note that links a compound gets it", not "every note gets a compound note".

    A note may legitimately carry `compound_smiles` as metadata without citing the compound note —
    minting one unasked would put a file in the PR the author did not write.
    """
    note = Note(id="n", type="reaction", compound_smiles="CCO", body="no links here")
    assert compound_dependencies(note) == []


def test_an_unparseable_smiles_does_not_fail_a_submission() -> None:
    """This helper reads a field opportunistically; it is not the place to reject a bad SMILES."""
    note = Note(id="n", type="reaction", compound_smiles="not-a-molecule", body="[[compound-x]]")
    assert compound_dependencies(note) == []
