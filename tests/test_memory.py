"""Behavioral tests for the memory layers (plan Phase 5), runnable without a server.

Proves the CHECKMATE 5 acceptance: chained experiments become a `campaign` note that cites
each member reaction, and reactions recurring across >=2 projects become a `playbook`
candidate + note with mandatory evidence — all from existing pieces (fingerprint identity,
the reaction schema, the note model), no new infrastructure.
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from chemclaw.config import settings
from eln.ord import Component, OrdReaction, Role
from kg.note import Note
from kg.render import render_note
from memory.campaign import campaign_note_from_chain
from memory.chains import detect_chains
from memory.ids import stable_id
from memory.interaction import note_from_confirmed_answer
from memory.jobs import build_campaign_notes, distill_playbooks, synthesize_campaigns
from memory.playbook import (
    PlaybookError,
    find_playbook_candidates,
    playbook_note,
)
from memory.supersede import supersede_updates
from tests.conftest import FakeSubmitter


def _reaction(
    rid: str, reactants: list[str], products: list[str], project: str | None = None
) -> OrdReaction:
    """A minimal reaction from reactant/product SMILES lists."""
    return OrdReaction(
        reaction_id=rid,
        inputs=[Component(smiles=s, role=Role.REACTANT) for s in reactants],
        outcomes=[Component(smiles=s, role=Role.PRODUCT) for s in products],
        provenance="test",
        project=project,
    )


# --- chain detection (5.2) ------------------------------------------------------------


def test_detect_chain_links_product_to_reactant() -> None:
    """A→B when a product of A is a reactant of B; the linked pair is one ordered chain."""
    a = _reaction("a", ["CCO"], ["CC=O"])  # ethanol → acetaldehyde
    b = _reaction("b", ["CC=O", "O"], ["CC(O)O"])  # acetaldehyde → ...
    chains = detect_chains([b, a])  # order-independent input
    assert len(chains) == 1
    assert chains[0].reaction_ids == ["a", "b"]  # topological: producer before consumer
    assert chains[0].links[0].from_reaction == "a"
    assert chains[0].links[0].to_reaction == "b"


def test_unlinked_reactions_are_not_a_chain() -> None:
    """Reactions that share no product↔reactant compound form no campaign."""
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["c1ccccc1"], ["Brc1ccccc1"])  # unrelated
    assert detect_chains([a, b]) == []


def test_reagent_match_does_not_chain() -> None:
    """Only true reactant inputs link a chain — a shared solvent/reagent does not."""
    a = _reaction("a", ["CCO"], ["O"])  # product water
    b = OrdReaction(
        reaction_id="b",
        inputs=[
            Component(smiles="CCO", role=Role.REACTANT),
            Component(smiles="O", role=Role.SOLVENT),  # water only as solvent
        ],
        outcomes=[Component(smiles="CC=O", role=Role.PRODUCT)],
        provenance="test",
    )
    assert detect_chains([a, b]) == []  # water is a's product but only b's solvent


def test_two_shared_compounds_produce_two_links() -> None:
    """A pair sharing two product→reactant compounds yields one link per compound.

    Regression: a single edge attribute used to be overwritten per compound, silently
    dropping all but the last handoff from the campaign's evidence.
    """
    a = _reaction("a", ["CCO"], ["CC=O", "O"])  # two products, both consumed by b
    b = _reaction("b", ["CC=O", "O"], ["CC(O)O"])
    chains = detect_chains([a, b])
    assert len(chains) == 1
    links = chains[0].links
    assert len(links) == 2
    assert all(link.from_reaction == "a" and link.to_reaction == "b" for link in links)
    assert {link.via_compound for link in links} == {"CC=O", "O"}
    # The campaign note renders one handoff line per shared compound.
    note = campaign_note_from_chain(chains[0], {"a": a, "b": b})
    assert note.body.count("product of a → reactant of b") == 2


def test_cycle_is_flagged_not_a_false_sequence() -> None:
    """A reversible pair (A→B and B→A) is a chain marked unordered, not a fake causal order."""
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["CC=O"], ["CCO"])  # reverses a
    chains = detect_chains([a, b])
    assert len(chains) == 1
    assert chains[0].ordered is False  # cycle → not a topological sequence
    note = campaign_note_from_chain(chains[0], {"a": a, "b": b})
    assert "cycle" in note.body.lower()  # the note is honest about it


# --- campaign note (5.1/5.3) ----------------------------------------------------------


def test_campaign_note_cites_each_member() -> None:
    """The campaign note links every chained reaction (real back-references) + carries project."""
    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O", "O"], ["CC(O)O"], project="proj-x")
    chain = detect_chains([a, b])[0]
    note = campaign_note_from_chain(chain, {"a": a, "b": b})
    assert note.type == "campaign"
    assert note.created_by == "agent"
    assert note.outgoing_links() == ["reaction-a", "reaction-b"]  # cites both members
    assert note.tags == ["proj-x"]
    assert note.id.startswith("campaign-")


def test_campaign_id_is_stable() -> None:
    """The campaign id is derived from its members, so re-synthesis is idempotent."""
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["CC=O"], ["CC(O)O"])
    chain = detect_chains([a, b])[0]
    first = campaign_note_from_chain(chain, {"a": a, "b": b}).id
    second = campaign_note_from_chain(chain, {"a": a, "b": b}).id
    assert first == second


def test_growing_cluster_keeps_its_note_id() -> None:
    """A cluster that gains a member keeps its note id, so re-synthesis supersedes in place.

    A member-set-derived id would mint a fresh note on every corpus growth, accumulating
    stale siblings in the graph; anchoring on the smallest member keeps the `note/<id>`
    PR-gate branch (and the merged file path) stable while the note's body grows.
    """
    assert stable_id("optimization", ["r2", "r1"]) == stable_id("optimization", ["r1", "r2", "r3"])
    assert stable_id("optimization", ["r1", "r2"]) != stable_id("optimization", ["r4", "r5"])
    assert stable_id("optimization", ["r1"]) != stable_id("playbook", ["r1"])  # prefix separates


# --- supersede on merge / shrink (D-078) ----------------------------------------------


def _memory_note(
    note_id: str,
    member_ids: list[str],
    *,
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> Note:
    """A merged campaign-style note citing its members, as the corpus would hold it."""
    citations = "\n".join(f"- [[reaction-{rid}]]" for rid in member_ids)
    return Note(
        id=note_id,
        type="campaign",
        body=f"Campaign.\n\n{citations}\n",
        valid_from=valid_from,
        valid_to=valid_to,
    )


def test_merged_cluster_retires_the_losing_note() -> None:
    """When two clusters merge, the note the merge orphaned stops being current knowledge.

    The winner keeps its anchor id and updates in place; without this the loser stayed in the
    graph as a *current* account of experiments it no longer describes.
    """
    winner = _memory_note("campaign-aaa", ["r1", "r2"])  # the merged cluster's note
    loser = _memory_note("campaign-bbb", ["r2"])  # the pre-merge note for a subset
    today = date(2026, 7, 25)

    retired = supersede_updates([winner], [winner, loser], today)

    assert [n.id for n in retired] == ["campaign-bbb"]
    assert retired[0].valid_to == today  # excluded from current-evidence sweeps from tomorrow
    assert "Superseded by campaign-aaa" in retired[0].body
    assert "[[campaign-aaa]]" not in retired[0].body  # plain text: the successor is not merged yet
    assert "[[reaction-r2]]" in retired[0].body  # the original record is kept, not rewritten


def test_shrunk_cluster_retires_the_pre_shrink_note() -> None:
    """Losing the anchor member mints a new id, so the pre-shrink note must be retired too."""
    before = _memory_note("campaign-old", ["r1", "r2", "r3"])
    after = _memory_note("campaign-new", ["r2", "r3"])  # r1 (the anchor) dropped out
    retired = supersede_updates([after], [before, after], date(2026, 7, 25))
    assert [n.id for n in retired] == ["campaign-old"]


def test_growing_cluster_is_not_retired() -> None:
    """Ordinary growth re-mints the same id and updates in place — nothing is superseded.

    This is the case anchoring on the smallest member was designed for; retiring here would
    close a note's validity on every routine ELN sync.
    """
    grown = _memory_note("campaign-aaa", ["r1", "r2", "r3"])
    previous = _memory_note("campaign-aaa", ["r1", "r2"])
    assert supersede_updates([grown], [previous], date(2026, 7, 25)) == []


def test_unrelated_and_already_retired_notes_are_left_alone() -> None:
    """No member overlap, a different type, or an already-closed window: all untouched.

    The already-retired case is what makes the job idempotent — a second run must not re-close a
    note it already closed, which would append the marker line again on every single run.
    """
    new = _memory_note("campaign-aaa", ["r1"])
    unrelated = _memory_note("campaign-zzz", ["r9"])
    other_type = Note(id="playbook-p", type="playbook", body="- [[reaction-r1]]\n")
    already = _memory_note("campaign-old", ["r1"], valid_to=date(2026, 1, 1))
    retired = supersede_updates([new], [unrelated, other_type, already], date(2026, 7, 25))
    assert retired == []


def test_retiring_a_not_yet_valid_note_keeps_a_legal_window() -> None:
    """A note whose validity starts in the future closes at its start, never before it (F10-G2)."""
    future = _memory_note("campaign-future", ["r1"], valid_from=date(2027, 1, 1))
    retired = supersede_updates([_memory_note("campaign-new", ["r1"])], [future], date(2026, 7, 25))
    assert retired[0].valid_to == date(2027, 1, 1)  # == valid_from, a legal (single-day) window


def test_synthesis_publishes_supersedes_alongside_new_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a merge in the corpus makes the builder emit the retirement note too.

    Proves the retirement travels the *same* PR-gate path as every other memory note — no second
    write path — and that both the in-process job and the durable activity get it, since both go
    through this builder.
    """
    knowledge = tmp_path / "knowledge" / "campaign"
    knowledge.mkdir(parents=True)
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["CC=O"], ["CC(O)O"])
    # The corpus already holds the note for the "b"-only cluster, from before "a" was ingested.
    stale_id = stable_id("campaign", ["b"])
    (knowledge / f"{stale_id}.md").write_text(
        render_note(_memory_note(stale_id, ["b"])), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path / "knowledge"))

    notes = build_campaign_notes([a, b])

    by_id = {n.id: n for n in notes}
    merged_id = stable_id("campaign", ["a", "b"])
    assert merged_id in by_id and by_id[merged_id].valid_to is None  # the live note
    assert stale_id in by_id and by_id[stale_id].valid_to == date.today()  # retired in the same run


# --- playbook (5.4) -------------------------------------------------------------------


def test_playbook_candidate_needs_two_projects() -> None:
    """Similar reactions recur into a playbook candidate only across >=2 projects."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    ester_x2 = _reaction("x2", ["CCCCO", "CC(=O)O"], ["CCCCOC(C)=O"], project="proj-x")

    # Two projects (x, y) → a candidate.
    candidates = find_playbook_candidates([ester_x, ester_y, ester_x2], threshold=0.3)
    assert len(candidates) == 1
    assert candidates[0].projects == ["proj-x", "proj-y"]
    assert set(candidates[0].reaction_ids) >= {"x", "y"}


def test_single_project_repetition_is_not_a_playbook() -> None:
    """Repetition within one project is episodic, not a transferable playbook."""
    a = _reaction("a", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    b = _reaction("b", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-x")
    assert find_playbook_candidates([a, b], threshold=0.3) == []


def test_degenerate_reaction_does_not_abort_distillation() -> None:
    """A reaction whose fingerprint is degenerate is skipped, not fatal to the whole job (G4)."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    degenerate = _reaction("bad", ["C"], ["C"], project="proj-z")  # empty DRFP → FingerprintError
    candidates = find_playbook_candidates([ester_x, degenerate, ester_y], threshold=0.3)
    assert len(candidates) == 1  # the good cross-project pair still surfaces
    assert "bad" not in candidates[0].reaction_ids


def test_playbook_note_requires_evidence() -> None:
    """A playbook with citations builds; one without is rejected (Belegverweise verpflichtend)."""
    note = playbook_note("playbook-ester", "Fischer esterification recurs.", ["x", "y"])
    assert note.type == "playbook"
    assert note.id == "playbook-ester"  # the full note id is passed in, not re-prefixed
    assert note.outgoing_links() == ["reaction-x", "reaction-y"]  # mandatory evidence
    with pytest.raises(PlaybookError, match="no evidence"):
        playbook_note("playbook-empty", "no evidence here", [])


# --- jobs (5.3/5.4 wiring) ------------------------------------------------------------


def test_synthesize_campaigns_proposes_notes_via_pr_gate() -> None:
    """The campaign job proposes one PR-gated campaign note per detected chain."""
    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O"], ["CC(O)O"], project="proj-x")
    sub = FakeSubmitter()
    refs = asyncio.run(synthesize_campaigns([a, b], sub))
    assert len(refs) == 1
    assert sub.submissions[0].files[0].path.startswith("knowledge/campaign/campaign-")


def test_distill_playbooks_proposes_evidence_backed_notes() -> None:
    """The playbook job proposes a cross-project playbook note citing its evidence."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    sub = FakeSubmitter()
    refs = asyncio.run(distill_playbooks([ester_x, ester_y], sub))
    assert len(refs) == 1
    assert sub.submissions[0].files[0].path.startswith("knowledge/playbook/playbook-")
    assert (
        "proj-x" in sub.submissions[0].files[0].content
        and "proj-y" in sub.submissions[0].files[0].content
    )


def test_build_campaign_notes_is_the_pure_half_of_synthesis() -> None:
    """`build_campaign_notes` builds exactly the notes `synthesize_campaigns` publishes (F10-D2).

    The fan-out workflow builds notes in one activity and publishes each in its own child; this pins
    that the extracted builder yields the same content the in-process publish path does, so the
    refactor is behavior-preserving.
    """
    from memory.jobs import build_campaign_notes

    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O"], ["CC(O)O"], project="proj-x")
    notes = build_campaign_notes([a, b])
    sub = FakeSubmitter()
    asyncio.run(synthesize_campaigns([a, b], sub))
    # The builder yields exactly the notes the publish path submits (same ids, in order).
    assert len(notes) == len(sub.submissions)
    assert all(n.id in s.files[0].path for n, s in zip(notes, sub.submissions, strict=True))
    assert all(n.type == "campaign" for n in notes)


# --- user interaction (5.5) -----------------------------------------------------------


def test_interaction_note_captures_confirmed_answer() -> None:
    """A confirmed user answer becomes an episodic `interaction` note citing its evidence."""
    note = note_from_confirmed_answer(
        "q-42",
        "Best solvent for the coupling?",
        "Aqueous dioxane worked at 90 °C.",
        ["reaction-eln-2026-002"],
    )
    assert note.type == "interaction"
    assert note.created_by == "agent"  # still PR-gated before it is trusted
    assert "confirmed" in note.body.lower()
    assert note.outgoing_links() == ["reaction-eln-2026-002"]


def test_record_confirmed_answer_tool_uses_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent tool routes a confirmed answer through the (fake) PR-gate (5.5 wiring)."""
    from agents import memory_tools

    fake = FakeSubmitter()
    monkeypatch.setattr(memory_tools, "default_submitter", lambda: fake)
    ref = asyncio.run(
        memory_tools.record_confirmed_answer(
            "q-42", "Best solvent?", "Aqueous dioxane at 90 C.", ["reaction-eln-2026-002"]
        )
    )
    assert ref == "pr://note/interaction-q-42"
    submitted = fake.submissions[0]
    assert submitted.files[0].path.endswith("interaction/interaction-q-42.md")
    assert "reaction-eln-2026-002" in submitted.files[0].content
