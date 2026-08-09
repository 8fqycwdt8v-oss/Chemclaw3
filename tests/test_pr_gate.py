"""Tests for note rendering and the PR-gate (plan steps 2.6, 2.7)."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from chemclaw.kg.note import Note, parse_note
from chemclaw.kg.pr_gate import propose_note
from chemclaw.kg.render import render_note
from tests.conftest import FakeSubmitter


def test_render_round_trips(tmp_path: Path) -> None:
    """render_note → file → parse_note preserves every field and link."""
    note = Note(
        id="compound-aspirin",
        type="compound",
        compound_smiles="CC(=O)Oc1ccccc1C(=O)O",
        tags=["nsaid"],
        created_by="agent",
        confidence=0.8,
        valid_from=date(2026, 1, 1),
        body="Made from [[compound-salicylic-acid]] via [[reaction-acetylation]].",
    )
    path = tmp_path / "note.md"
    path.write_text(render_note(note), encoding="utf-8")
    parsed = parse_note(path)
    assert parsed.model_dump(exclude={"body"}) == note.model_dump(exclude={"body"})
    assert parsed.outgoing_links() == note.outgoing_links()


def test_gate_submits_agent_note() -> None:
    """An agent note is laid out by type/id on its own branch with a review PR body."""
    note = Note(
        id="job-123",
        type="job-result",
        created_by="agent",
        source="qm",
        body="Energy computed for [[compound-x]].",
    )
    fake = FakeSubmitter()
    ref = asyncio.run(propose_note(note, fake, knowledge_dir="knowledge"))

    assert ref == "pr://note/job-123"
    submission = fake.submissions[0]
    assert submission.branch == "note/job-123"
    assert submission.files[0].path == "knowledge/job-result/job-123.md"
    assert "job-123" in submission.title
    assert "human review" in submission.body.lower()
    assert "qm" in submission.body  # provenance carried into the PR body
    # A note with no dependencies is one file, and the body must not offer the reviewer a count of
    # supporting notes there are none of.
    assert "supporting note" not in submission.body


def test_a_dependency_listed_twice_is_written_once_and_counted_once() -> None:
    """What the reviewer receives: every distinct file, each once, and an honest count of them.

    A caller legitimately repeats a dependency — two computed properties of one compound both name
    it — so this is the ordinary path, not a pathological one.

    **The deduplication half is a deterministic example of something a property test already
    covers, and the reason to add one is speed and reliability, not coverage.**
    `test_properties_core.py::test_a_submission_writes_each_note_once_with_its_subject_first`
    asserts the same invariant over generated notes, and it *does* kill both ways of corrupting the
    dedup loop (`continue` → `break`, which drops every dependency after the first repeat; and
    `seen.add(dependency.id)` → `seen.add(None)`, which writes one path twice). But its generator
    draws ids from `[a-z0-9][a-z0-9._-]{0,20}`, and the shape that discriminates those mutations —
    a repeated dependency followed by a *further, new* one — occurred in **1 of 100 generated
    examples** (measured). So the kill depends on the seed and costs 83 s and 158 s respectively on
    a cold hypothesis database; under `make mutants` one was recorded SURVIVED and the other
    timed out, which `[tool.mutmut]` scores as not-killed. A flaky, three-minute killer is not a
    regression test. This one is deterministic and runs in milliseconds; the property test keeps
    its wider job of finding shapes nobody enumerated.

    **The count half was genuinely unpinned.** The PR body's "with N supporting note(s)" is the
    reviewer's summary of the unit they are approving, and no test read it: `len(files) - 1` →
    `+ 1` and `- 2`, and the threshold `> 1` → `>= 1` and `> 2`, all survived the whole suite.
    """
    compound = Note(id="compound-x", type="compound", created_by="agent", body="the compound")
    solvent = Note(id="compound-thf", type="compound", created_by="agent", body="the solvent")
    note = Note(
        id="job-9",
        type="job-result",
        created_by="agent",
        body="Energy for [[compound-x]] in [[compound-thf]].",
    )
    fake = FakeSubmitter()
    asyncio.run(
        propose_note(
            note, fake, knowledge_dir="knowledge", dependencies=[compound, compound, solvent]
        )
    )

    submission = fake.submissions[0]
    assert [file.path for file in submission.files] == [
        "knowledge/job-result/job-9.md",
        "knowledge/compound/compound-x.md",
        "knowledge/compound/compound-thf.md",
    ]
    assert "with 2 supporting note(s)" in submission.body

    # The one-dependency case, which is both the boundary of that count and the commonest shape
    # the gate sees — a `job-result` and the `compound` its wikilink needs to resolve.
    single = FakeSubmitter()
    asyncio.run(propose_note(note, single, knowledge_dir="knowledge", dependencies=[compound]))
    assert len(single.submissions[0].files) == 2
    assert "with 1 supporting note(s)" in single.submissions[0].body


def test_gate_rejects_human_note() -> None:
    """Human-authored notes are committed directly, not gated (G6/D-005)."""
    note = Note(id="manual", type="compound")  # created_by defaults to human
    with pytest.raises(ValueError, match="agent-authored"):
        asyncio.run(propose_note(note, FakeSubmitter()))
