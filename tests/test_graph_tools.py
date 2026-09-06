"""Tests for the agent knowledge-graph tools (plan steps 2.5, 2.6)."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

import chemclaw.agent.graph_tools as graph_tools
from chemclaw.agent.graph_tools import (
    expand_note,
    find_notes,
    record_failure,
    record_knowledge_note,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.kg.conflicts import find_conflicts
from chemclaw.kg.note import Note, parse_note
from chemclaw.kg.record import NoteWrite
from tests.conftest import FakeWriter


def _seed(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\nid: compound-a\ntype: compound\ntags: [target]\n---\nMakes [[reaction-r]].\n",
        encoding="utf-8",
    )
    (tmp_path / "r.md").write_text(
        "---\nid: reaction-r\ntype: reaction\n---\nYields [[compound-a]].\n", encoding="utf-8"
    )


def test_find_notes_matches_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """find_notes locates a note by tag substring."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    refs = asyncio.run(find_notes("target")).matches
    assert {r.id for r in refs} == {"compound-a"}


def test_find_notes_matches_all_words_not_a_literal_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every word in the query must appear somewhere in the note — not as one exact phrase.

    Regression guard: a natural multi-word question ("target reaction") used to require that
    exact run of text to appear verbatim, so it missed a note whose words are present but not
    adjacent in that order — a real live-e2e finding where the model then reported "no data"
    even though the corpus had it.
    """
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    # "target" is on compound-a; "reaction" only appears on reaction-r's own id/type, and
    # compound-a's body only links to it as "[[reaction-r]]" — no note contains the literal
    # phrase "target reaction", but compound-a contains both words independently.
    refs = asyncio.run(find_notes("target reaction")).matches
    assert {r.id for r in refs} == {"compound-a"}


def test_find_notes_returns_nothing_when_one_word_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-words matching widens rather than dropping to nothing — and says it widened.

    A query with one absent word used to return `[]`, while the sweep's graph leg widened to
    partial matches over the same corpus — so the two tools the prompt chains disagreed about one
    question. The partial hit now comes back marked `widened`, which is the honest version of
    both behaviours.
    """
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    found = asyncio.run(find_notes("target nonexistentword"))
    assert found.widened is True
    assert {r.id for r in found.matches} == {"compound-a"}
    # And a query where *nothing* matches at all is still an empty result.
    nothing = asyncio.run(find_notes("nonexistentword absentterm"))
    assert nothing.matches == [] and nothing.widened is False


def test_expand_note_returns_neighbors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_note returns the body and the linked note as a neighbor."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("compound-a", hops=1))
    assert view.note.id == "compound-a"
    assert [n.id for n in view.neighbors] == ["reaction-r"]


def test_expand_unknown_note_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expanding an unknown id is a clear error (G4), and a `ChemclawError` specifically.

    `ChemclawError` (a `ValueError` subclass) is chemclaw's own always-safe "bad input"
    contract, so `chemclaw.agent.tool_authz.surface_domain_errors` surfaces this message to the
    model
    verbatim instead of MAF's opaque generic failure — the common real cause is a citation to a
    note still pending PR-gate review, which the chemist can otherwise not distinguish from a
    typo or a deleted note.
    """
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    with pytest.raises(ChemclawError, match="no note with id"):
        asyncio.run(expand_note("ghost"))


def test_expand_note_clamps_hops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge `hops` is clamped to the configured max, not traversed unbounded (SEC-4)."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "graph_max_hops", 2)
    # An absurd hop count returns the same bounded neighborhood as the max, never errors or hangs.
    huge = asyncio.run(expand_note("compound-a", hops=10_000))
    at_max = asyncio.run(expand_note("compound-a", hops=2))
    assert {n.id for n in huge.neighbors} == {n.id for n in at_max.neighbors}


def test_find_notes_surfaces_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A NoteRef carries provenance (author/source/confidence) so the agent can weigh it (KM-6)."""
    (tmp_path / "p.md").write_text(
        "---\nid: reaction-p\ntype: reaction\ncreated_by: agent\nsource: eln-7\n"
        "confidence: 0.8\n---\nA [[compound-a]] prep.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    (ref,) = asyncio.run(find_notes("prep")).matches
    assert ref.created_by == "agent"
    assert ref.source == "eln-7"
    assert ref.confidence == 0.8


def test_find_notes_excludes_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired note (valid_to in the past) is not surfaced as current evidence (KM-7)."""
    (tmp_path / "old.md").write_text(
        "---\nid: reaction-old\ntype: reaction\nvalid_to: 2000-01-01\ntags: [reflux]\n---\nOld.\n",
        encoding="utf-8",
    )
    (tmp_path / "new.md").write_text(
        "---\nid: reaction-new\ntype: reaction\ntags: [reflux]\n---\nCurrent.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    refs = asyncio.run(find_notes("reflux")).matches
    assert {r.id for r in refs} == {"reaction-new"}  # the expired note is dropped


def test_expand_note_drops_expired_neighbor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anchor is returned by explicit id, but an expired neighbor is filtered out (KM-7)."""
    (tmp_path / "a.md").write_text(
        "---\nid: compound-a\ntype: compound\n---\nMakes [[reaction-old]] and [[reaction-r]].\n",
        encoding="utf-8",
    )
    (tmp_path / "old.md").write_text(
        "---\nid: reaction-old\ntype: reaction\nvalid_to: 2000-01-01\n---\nExpired.\n",
        encoding="utf-8",
    )
    (tmp_path / "r.md").write_text(
        "---\nid: reaction-r\ntype: reaction\n---\nCurrent.\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("compound-a", hops=1))
    assert [n.id for n in view.neighbors] == ["reaction-r"]  # expired neighbor excluded


def test_find_notes_caps_the_hit_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broad needle is truncated to the cap, in a stable order.

    Every hit lands in the model's context window, so an uncapped sweep over a real corpus is a
    context blowout. Truncation is by sorted id so the same query returns the same notes.
    """
    for i in range(10):
        (tmp_path / f"n{i:02d}.md").write_text(
            f"---\nid: reaction-{i:02d}\ntype: reaction\n---\nAn acetylation.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "graph_max_results", 3)
    found = asyncio.run(find_notes("acetylation"))
    assert [r.id for r in found.matches] == ["reaction-00", "reaction-01", "reaction-02"]
    assert found.total_matches == 10, "the cut must be declared, not silent"
    assert [r.id for r in asyncio.run(find_notes("acetylation")).matches] == [
        r.id for r in found.matches
    ]


def test_find_notes_declares_a_cut_in_the_value_the_model_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut is in the return value, not a log line no model reads.

    The old warning was exactly the defect `EvidenceSweep.truncated_by` fixed in the sibling
    tool: a capped list with no marker is byte-identical to a small corpus.
    """
    for i in range(4):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nAn acetylation.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    monkeypatch.setattr(settings, "graph_max_results", 2)
    cut = asyncio.run(find_notes("acetylation"))
    assert (len(cut.matches), cut.total_matches) == (2, 4)

    monkeypatch.setattr(settings, "graph_max_results", 50)
    whole = asyncio.run(find_notes("acetylation"))
    assert (len(whole.matches), whole.total_matches) == (4, 4)


def test_record_knowledge_note_writes_through_the_record_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write tool proposes an agent note through the (fake) PR-gate."""
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)
    ref = asyncio.run(
        record_knowledge_note(
            id="reaction-x", type="reaction", body="From [[compound-a]].", source="eln-1"
        )
    )
    assert ref == "commit://1"
    assert fake.writes[0].files[0].path.endswith("reaction/reaction-x.md")


def _seed_playbook(tmp_path: Path, **frontmatter: str) -> None:
    """A merged, human-authored playbook — the kind of note a chemist reports as wrong."""
    extra = "".join(f"{key}: {value}\n" for key, value in frontmatter.items())
    (tmp_path / "playbook.md").write_text(
        f"---\nid: playbook-pd\ntype: playbook\n{extra}---\nUse 5 mol% Pd for the aryl coupling.\n",
        encoding="utf-8",
    )


def _submitted(submission: NoteWrite, tmp_path: Path) -> dict[str, Note]:
    """Parse every file in a submission back off disk, keyed by note id.

    Round-tripping through the parser rather than reading the model in memory is the point: what
    a reviewer merges is these bytes, so a correction that only exists in the object graph is not
    a correction.
    """
    parsed = {}
    for index, file in enumerate(submission.files):
        path = tmp_path / f"submitted-{index}.md"
        path.write_text(file.content, encoding="utf-8")
        note = parse_note(path)
        parsed[note.id] = note
    return parsed


def test_record_failure_records_a_refutation_conflict_detection_can_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop that closes: a chemist's report becomes a note that flags the claim it refutes.

    Asserted through `find_conflicts` over the merged corpus rather than by inspecting the note's
    fields, because "the graph now knows this is disputed" is the behaviour — a `failure-mode` note
    whose edge conflict detection cannot read would satisfy every structural check and change
    nothing a chemist ever sees.
    """
    _seed_playbook(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    ref = asyncio.run(
        record_failure("playbook-pd", "Ran it four times at scale; the yield was half.")
    )

    assert ref.startswith("commit://")
    notes = _submitted(fake.writes[0], tmp_path)
    (failure,) = notes.values()
    assert failure.type == "failure-mode"
    merged = [parse_note(tmp_path / "playbook.md"), failure]
    assert [(c.kind, c.other_id) for c in find_conflicts(merged, as_of=date.today())] == [
        ("declared", "playbook-pd")
    ]


def test_record_failure_attributes_the_report_to_the_authenticated_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance comes from the turn's identity, never from a string the model composed.

    A reporter the model can fill in is a reporter it can get wrong — and this note's whole content
    is an accusation that curated knowledge is false, so "who says so" is the load-bearing field.
    """
    _seed_playbook(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    tokens = set_current_identity("chemist-oid-42", frozenset())
    try:
        asyncio.run(record_failure("playbook-pd", "it did not dissolve"))
    finally:
        reset_current_identity(tokens)

    (failure,) = _submitted(fake.writes[0], tmp_path).values()
    assert failure.source == "feedback:chemist-oid-42"
    assert "chemist-oid-42" in failure.body


def test_record_failure_leaves_the_refuted_note_current_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting a claim wrong does not mean it was true until today, so nothing is retired.

    `valid_to` is a valid-time bound: closing a never-true claim would assert it held up to the
    reporting date, and would drop it out of the retrieval-time conflict scan that is the only
    thing marking it disputed. So the default submission is the failure note alone.
    """
    _seed_playbook(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    asyncio.run(record_failure("playbook-pd", "the yield was half"))

    assert len(fake.writes[0].files) == 1
    assert parse_note(tmp_path / "playbook.md").valid_to is None


def test_record_failure_retires_a_claim_that_stopped_holding_in_the_same_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction half: the superseded claim stops reading as current, in one reviewable PR.

    Both files ride together so a human signs off on the refutation and the retirement as the one
    decision they are. The amended note keeps its own content and gains the end date plus a link to
    the note that ended it.
    """
    _seed_playbook(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    asyncio.run(
        record_failure(
            "playbook-pd",
            "the supplier changed the Pd lot and 5 mol% stopped converting",
            held_until=date(2026, 3, 1),
        )
    )

    notes = _submitted(fake.writes[0], tmp_path)
    amended = notes["playbook-pd"]
    failure = next(note for note in notes.values() if note.type == "failure-mode")
    assert amended.valid_to == date(2026, 3, 1)
    assert amended.is_current(date(2026, 1, 1))  # it really did hold, and still says so
    assert not amended.is_current(date(2026, 6, 1))  # and no longer reads as current fact
    assert "Use 5 mol% Pd" in amended.body  # the original claim is kept, never edited away
    assert failure.id in amended.outgoing_links()  # and points at what ended it
    # The retirement must OVERWRITE: the refuted note already exists on the base branch, and a file
    # marked overwrite=False (a `dependencies` entry) is skipped by `_write_and_push` when it exists
    # on base — so the retirement was silently dropped on the real git path and the refuted claim
    # stayed served as current. `superseded` marks it overwrite=True. Asserted on the submission
    # because the FakeWriter never runs the skip, which is why this bug survived the old test.
    retirement_file = next(f for f in fake.writes[0].files if f.path.endswith("playbook-pd.md"))
    assert retirement_file.overwrite is True


def test_record_failure_refuses_to_reclose_an_already_retired_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-closing would extend a closed note's validity and append the marker twice — so it says so.

    Refused rather than quietly skipped. Both dates came from a person, and dropping one of them
    is the same silent correction `close_refuted_note` already refuses when the window ends before
    it starts; the message names the date that already holds so the chemist can pick.
    """
    _seed_playbook(tmp_path, valid_to="2025-01-01")
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    with pytest.raises(ChemclawError, match="already retired on 2025-01-01"):
        asyncio.run(
            record_failure("playbook-pd", "still does not work", held_until=date(2026, 3, 1))
        )
    assert fake.writes == [], "nothing is filed when the dates disagree"


def test_record_failure_without_a_date_still_works_on_a_retired_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal above is about the *date*, not the note: a plain refutation is always allowed."""
    _seed_playbook(tmp_path, valid_to="2025-01-01")
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    asyncio.run(record_failure("playbook-pd", "still does not work"))

    assert len(fake.writes[0].files) == 1  # the failure note only


def test_record_failure_on_an_unknown_note_says_so_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `ChemclawError` reaches the model verbatim; anything else becomes a generic failure.

    And nothing is submitted: a refutation of a note that does not exist would be a `contradicts`
    edge to nowhere, which `find_conflicts` drops and `kg-validate` fails.
    """
    _seed_playbook(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    with pytest.raises(ChemclawError, match="no note with id 'playbook-typo'"):
        asyncio.run(record_failure("playbook-typo", "the yield was half"))
    assert fake.writes == []


def test_record_failure_refuses_an_end_date_before_the_note_began(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backwards window is rejected with both dates, not clamped into a date nobody asked for."""
    _seed_playbook(tmp_path, valid_from="2026-05-01")
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    fake = FakeWriter()
    monkeypatch.setattr(graph_tools, "default_writer", lambda: fake)

    with pytest.raises(ChemclawError, match="only became valid on 2026-05-01"):
        asyncio.run(record_failure("playbook-pd", "no good", held_until=date(2026, 3, 1)))
    assert fake.writes == []


def _seed_typed(tmp_path: Path) -> None:
    """A refuted note, its refutation, its replacement, and one plain citation.

    Modelled on what `record_failure` actually writes: a `failure-mode` note carrying a
    `contradicts` edge back at the note it refutes.
    """
    (tmp_path / "old.md").write_text(
        "---\nid: playbook-old\ntype: playbook\n---\nUse DCM. See [[compound-a]].\n",
        encoding="utf-8",
    )
    (tmp_path / "fail.md").write_text(
        "---\nid: failure-dcm\ntype: failure-mode\n"
        "relations:\n  - rel: contradicts\n    to: playbook-old\n---\nIt did not couple.\n",
        encoding="utf-8",
    )
    (tmp_path / "new.md").write_text(
        "---\nid: playbook-new\ntype: playbook\n"
        "relations:\n  - rel: supersedes\n    to: playbook-old\n---\nUse THF instead.\n",
        encoding="utf-8",
    )
    (tmp_path / "a.md").write_text(
        "---\nid: compound-a\ntype: compound\n---\nA molecule.\n", encoding="utf-8"
    )


def test_expand_note_reports_the_typed_edge_and_its_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A neighbour that contradicts or supersedes this note arrives legible as one.

    D-134 put `rel` on every edge and `_assemble_graph` has carried it since; no reader in this
    repository read it. So `record_failure` wrote a `contradicts` edge for the express purpose that
    a refuted note "arrives marked as disputed", and `expand_note` returned that neighbour
    indistinguishable from an ordinary citation.

    Direction is asserted separately because it is the claim: `relations_in` on `playbook-old` says
    the *neighbours* supersede and contradict *it*, and the opposite reading is a different fact
    about which note is current.
    """
    _seed_typed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("playbook-old", hops=1))
    by_id = {neighbor.id: neighbor for neighbor in view.neighbors}

    assert by_id["failure-dcm"].relations_in == ["contradicts"]
    assert by_id["failure-dcm"].relations_out == []
    assert by_id["playbook-new"].relations_in == ["supersedes"]
    assert by_id["playbook-new"].relations_out == []

    # Seen from the other end, the same edge is an outgoing claim.
    replacement = asyncio.run(expand_note("playbook-new", hops=1))
    assert {n.id: n.relations_out for n in replacement.neighbors} == {
        "playbook-old": ["supersedes"]
    }


def test_expand_note_leaves_a_plain_citation_untyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `[[wikilink]]` reports no relation — `cites` is what every untyped link already means.

    Reporting it would put the word on the majority of neighbours while saying nothing the
    neighbourhood does not already say, and would drown the edges an author typed on purpose.
    """
    _seed_typed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("playbook-old", hops=1))
    cited = next(neighbor for neighbor in view.neighbors if neighbor.id == "compound-a")
    assert cited.relations_out == []
    assert cited.relations_in == []


def test_expand_note_two_hop_neighbour_asserts_no_relation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note reached in two hops is adjacent in the neighbourhood and linked to by nothing here.

    Empty rather than inferred: there is no edge between the anchor and it, and inventing one from
    a path would be this layer asserting a relation no author wrote.
    """
    _seed_typed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "graph_max_hops", 2)
    view = asyncio.run(expand_note("playbook-new", hops=2))
    two_hops = next(neighbor for neighbor in view.neighbors if neighbor.id == "compound-a")
    assert two_hops.relations_out == []
    assert two_hops.relations_in == []


def test_find_notes_ignores_a_dangling_link_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep is over notes, so an id nothing defines can never be returned as a match.

    `find_notes` used to iterate the assembled graph's nodes, which include link targets that have
    no note behind them; they were skipped one line later, so this pins the behaviour rather than a
    change to it — and pins that reading `load_notes` instead of `build_graph` kept it.
    """
    (tmp_path / "r.md").write_text(
        "---\nid: reaction-r\ntype: reaction\ntags: [target]\n---\nrests on [[compound-pending]]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    assert [ref.id for ref in asyncio.run(find_notes("target")).matches] == ["reaction-r"]
    # The dangling id is findable as *text in a body*, which is right — it is in that note's
    # haystack. What it must never be is a hit in its own right, a reference to a note that has no
    # body, type or provenance to report.
    assert [ref.id for ref in asyncio.run(find_notes("compound-pending")).matches] == ["reaction-r"]


def test_find_notes_truncates_in_id_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap keeps the lowest ids, which is what its warning tells the operator it does.

    Load order is path order and the cap is applied while iterating, so the two must not be
    conflated: the files here are laid down in the reverse of their id order.
    """
    for index, note_id in enumerate(["compound-d", "compound-c", "compound-b", "compound-a"]):
        (tmp_path / f"{index}.md").write_text(
            f"---\nid: {note_id}\ntype: compound\ntags: [target]\n---\nbody\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "graph_max_results", 2)
    assert [ref.id for ref in asyncio.run(find_notes("target")).matches] == [
        "compound-a",
        "compound-b",
    ]
