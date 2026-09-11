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
import logging
from pathlib import Path

import pytest

from chemclaw.core.chem import compound_id
from chemclaw.core.config import settings
from chemclaw.ingest.eln.compound import compound_dependencies, compound_note
from chemclaw.kg.crosslink import cited_calculations
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
    [
        "the GFN2 run",
        "xtb.hess@v1",
        "xtb.hess:0011:2233",
        # An empty hash segment, either side. `CalculationKey` gives both `min_length=1`, so these
        # are refused by the *store's* rule rather than by a stricter one invented here.
        "xtb.hess@v1::0011",
        "xtb.hess@v1:0011:",
    ],
)
def test_a_calc_ref_that_is_not_a_calculation_key_is_refused(bad: str) -> None:
    r"""Prose in this field is a crosslink nothing can resolve, so it fails at the schema.

    The whole value of the field is that a machine can follow it. `"the GFN2 run"` looks like
    provenance and is not, and a note carrying it would pass review looking perfectly informative.

    **`"xtb.hess@v1:nothex:0011"` used to be in this list and is not a defensible refusal.** It
    asserted that a hash must be lowercase hex, which `CalculationKey` never said — its two hash
    fields are `[^\s:]+` — so this case was pinning the note side *narrower* than the store it
    claims to mirror. Removed rather than kept, because keeping it would hold `_CALC_REF` to a rule
    that refuses keys the calculation cache really writes; the cases that replace it are refusals
    the store makes too. `tests/test_note.py` holds the other half, that every key the store
    accepts is citable.
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


def test_the_reverse_lookup_is_gone_and_stays_gone_until_something_calls_it() -> None:
    """The two functions that answered "which notes rest on this key" had no caller, ever.

    D-133 wrote them, D-158 gave them a producer in the `qm` bundle's note builder, and
    `D-2026-08-26-semiempirical-is-the-whole-tier` deleted that bundle — so from that day the
    index had neither a caller nor a writer, and the only thing keeping it alive was the two
    tests above this one, which called it directly. That is the shape CLAUDE.md names
    (`map_to_hpc_identity`, `reject_widening`) and deletes.

    An **absence** test rather than nothing, because two merged ADRs deliberately kept this module
    and a third designed it: re-adding the lookup is a decision somebody takes on purpose with a
    caller in hand, not a revert. `cited_calculations` stays and is asserted above — it has a real
    caller (`tests/test_seed_corpus.py`) and it is the definition of what a note rests on.
    """
    import chemclaw.kg.crosslink as crosslink

    assert not hasattr(crosslink, "calc_ref_index")
    assert not hasattr(crosslink, "notes_for_calculation")


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


def test_a_link_to_a_note_that_does_not_exist_is_reported_at_write_time(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-written `[[wikilink]]` at a note nobody wrote used to land in silence.

    `compound_dependencies` mints the derived `compound-<hash>` id and nothing else, so a target
    the model typed itself is carried by no dependency — the note commits, `expand_note` on the
    target then raises "no note with id …", and the chip in the UI 404s. The write is the one
    moment the writer can say so, and `kg-validate` — the check that does catch it — runs over
    *this* repository's corpus in CI, never over a deployment's.

    A WARNING and not a refusal: the note is the record either way, the model is told what it
    linked to, and refusing would lose a real observation over a typo in a citation.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
        note = Note(
            id="job-1",
            type="job-result",
            created_by="agent",
            body="Computed for [[compound-ethanol-w141]].",
        )
        with caplog.at_level(logging.WARNING, logger="chemclaw.kg.record"):
            await record_note(note, _Capturing(), knowledge_dir="knowledge")

    asyncio.run(_run())
    assert "compound-ethanol-w141" in caplog.text
    assert "job-1" in caplog.text


def test_a_link_whose_target_lands_in_the_same_write_is_not_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: the ordinary computed note must not warn on every write.

    Its compound dependency is written first (`record._build_write`), so the link resolves the
    moment the unit lands — warning about it would make the marker noise and train the model to
    ignore it.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
        smiles = "CCO"
        note = Note(
            id="job-2",
            type="job-result",
            compound_smiles=smiles,
            created_by="agent",
            body=f"Computed for [[{compound_id(smiles)}]].",
        )
        with caplog.at_level(logging.WARNING, logger="chemclaw.kg.record"):
            await record_note(
                note,
                _Capturing(),
                knowledge_dir="knowledge",
                dependencies=compound_dependencies(note),
            )

    asyncio.run(_run())
    assert caplog.text == ""


def test_a_citation_of_a_transcribed_reaction_is_not_a_dangling_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[[reaction-<id>]]` resolves in the record store, not in the graph (D-2026-08-25).

    Every campaign and optimization note cites its runs that way, so reporting them would warn on
    the notes the miners write most.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
        note = Note(
            id="campaign-1",
            type="campaign",
            created_by="agent",
            body="Distilled from [[reaction-eln-7]].",
        )
        with caplog.at_level(logging.WARNING, logger="chemclaw.kg.record"):
            await record_note(note, _Capturing(), knowledge_dir="knowledge")

    asyncio.run(_run())
    assert caplog.text == ""
