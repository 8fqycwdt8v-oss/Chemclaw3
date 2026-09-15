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

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import Component, OrdReaction, OutcomeClass, Role
from chemclaw.kg.note import Note
from chemclaw.kg.record import record_note
from chemclaw.kg.render import render_note
from chemclaw.memory.campaign import campaign_note_from_chain
from chemclaw.memory.chains import detect_chains
from chemclaw.memory.ids import stable_id
from chemclaw.memory.interaction import note_from_confirmed_answer
from chemclaw.memory.jobs import (
    PARTIAL_READ_CAVEAT,
    SynthesisUnit,
    build_campaign_notes,
    build_playbook_notes,
    supported_from,
)
from chemclaw.memory.observations import Observation
from chemclaw.memory.playbook import (
    SOURCE_DISTILLATION,
    SOURCE_PROMOTED_OBSERVATION,
    PlaybookError,
    find_playbook_candidates,
    playbook_note,
)
from chemclaw.memory.supersede import supersede_updates
from tests.conftest import FakeWriter


def _reaction(
    rid: str,
    reactants: list[str],
    products: list[str],
    project: str | None = None,
    outcome_class: OutcomeClass | None = OutcomeClass.SUCCESS,
) -> OrdReaction:
    """A minimal reaction from reactant/product SMILES lists.

    Successful unless a test says otherwise, and *explicitly* so: since
    `D-2026-08-26-silence-is-not-a-successful-run` an unstated outcome is no longer read as a
    success, and `find_playbook_candidates` distils only from stated ones — so a fixture that leaves
    this unset is a fixture no playbook can be built from, which is the correct rule and would make
    every distillation test here vacuous.
    """
    return OrdReaction(
        reaction_id=rid,
        inputs=[Component(smiles=s, role=Role.REACTANT) for s in reactants],
        outcomes=[Component(smiles=s, role=Role.PRODUCT) for s in products],
        provenance="test",
        project=project,
        outcome_class=outcome_class,
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
    member_ids: list[str],
    *,
    note_id: str | None = None,
    note_type: str = "campaign",
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> Note:
    """A merged campaign-style note citing its members, as the corpus would hold it.

    The id is derived the way the builders derive it (`stable_id` over the cited members), because
    that derivation is now what marks a note as this synthesis's own — a hand-picked id like
    "campaign-aaa" is a note no memory job could have written, and the retirement pass must (and
    now does) leave those alone. `note_id` overrides it only where a test needs a note *outside*
    the lineage.
    """
    citations = "\n".join(f"- [[reaction-{rid}]]" for rid in member_ids)
    return Note(
        id=note_id if note_id is not None else stable_id(note_type.split("-")[0], member_ids),
        type=note_type,
        body=f"Campaign.\n\n{citations}\n",
        valid_from=valid_from,
        valid_to=valid_to,
    )


def test_merged_cluster_retires_the_losing_note() -> None:
    """When two clusters merge, the note the merge orphaned stops being current knowledge.

    The winner keeps its anchor id and updates in place; without this the loser stayed in the
    graph as a *current* account of experiments it no longer describes.
    """
    winner = _memory_note(["r1", "r2"])  # the merged cluster's note
    loser = _memory_note(["r2"])  # the pre-merge note for a subset
    today = date(2026, 7, 25)

    retired = supersede_updates([winner], [winner, loser], today)

    assert [n.id for n in retired] == [loser.id]
    assert retired[0].valid_to == today  # excluded from current-evidence sweeps from tomorrow
    assert f"Superseded by {winner.id}" in retired[0].body
    assert f"[[{winner.id}]]" not in retired[0].body  # plain text: the successor is not merged yet
    assert "[[reaction-r2]]" in retired[0].body  # the original record is kept, not rewritten


def test_shrunk_cluster_retires_the_pre_shrink_note() -> None:
    """Losing the anchor member mints a new id, so the pre-shrink note must be retired too."""
    before = _memory_note(["r1", "r2", "r3"])
    after = _memory_note(["r2", "r3"])  # r1 (the anchor) dropped out
    retired = supersede_updates([after], [before, after], date(2026, 7, 25))
    assert [n.id for n in retired] == [before.id]


def test_growing_cluster_is_not_retired() -> None:
    """Ordinary growth re-mints the same id and updates in place — nothing is superseded.

    This is the case anchoring on the smallest member was designed for; retiring here would
    close a note's validity on every routine ELN sync.
    """
    grown = _memory_note(["r1", "r2", "r3"])
    previous = _memory_note(["r1", "r2"])
    assert grown.id == previous.id  # the anchor survived the growth, so it is one note
    assert supersede_updates([grown], [previous], date(2026, 7, 25)) == []


def test_unrelated_and_already_retired_notes_are_left_alone() -> None:
    """No member overlap, a different type, or an already-closed window: all untouched.

    The already-retired case is what makes the job idempotent — a second run must not re-close a
    note it already closed, which would append the marker line again on every single run.
    """
    new = _memory_note(["r1"])
    unrelated = _memory_note(["r9"])
    other_type = _memory_note(["r1"], note_type="playbook")
    already = _memory_note(["r0", "r1"], valid_to=date(2026, 1, 1))  # overlaps, but already closed
    assert already.id != new.id  # ...so only the closed window keeps it out
    retired = supersede_updates([new], [unrelated, other_type, already], date(2026, 7, 25))
    assert retired == []


def test_a_note_this_synthesis_never_minted_is_not_retired() -> None:
    """The lineage rule (D-161 fallout): a `playbook` id nothing here mints is left alone.

    The observations tier promotes an observation into `playbook-<observation hash>`, an id
    anchored on the observation's *scope* rather than on the cluster's smallest member — so
    `distill_playbooks` can never re-mint it, and "same type, overlapping members, id I no longer
    mint" matched it on every single run. The retirement it proposed carried the body line "this
    cluster's membership changed (merge or shrink)", which is untrue of a note that was never a
    cluster's; through the PR-gate that is a misleading PR inviting a rubber-stamp, and merging one
    drops a human-approved playbook out of every current-evidence sweep (`Note.is_current`).

    The human-authored variant of the same match is covered here too: it used to reach
    `record_note`, which refuses a `human` note — loud, but still a synthesis run crashing on a
    note it had no business touching.
    """
    fresh = _memory_note(["r1", "r2"], note_type="playbook")
    # Exactly how `observation_jobs.promote_observations_activity` names a promoted playbook.
    observation = Observation(
        statement="failed in both projects",
        scope="transformation:r1",
        evidence_note_ids=["reaction-r1", "reaction-r2"],
    ).with_id()
    promoted_id = f"playbook-{observation.id.removeprefix('observation-')}"
    promoted = _memory_note(["r1", "r2"], note_id=promoted_id, note_type="playbook")
    handwritten = _memory_note(["r1"], note_id="playbook-degassing", note_type="playbook")

    assert promoted.id != fresh.id  # the scope anchor, not the cluster anchor — hence "never mints"
    assert supersede_updates([fresh], [promoted, handwritten], date(2026, 7, 25)) == []


def test_retiring_a_not_yet_valid_note_keeps_a_legal_window() -> None:
    """A note whose validity starts in the future closes at its start, never before it (F10-G2)."""
    future = _memory_note(["r1"], valid_from=date(2027, 1, 1))
    retired = supersede_updates([_memory_note(["r0", "r1"])], [future], date(2026, 7, 25))
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
    stale = _memory_note(["b"])  # its id is `stable_id("campaign", ["b"])` — the lineage marker
    stale_id = stale.id
    (knowledge / f"{stale_id}.md").write_text(render_note(stale), encoding="utf-8")
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path / "knowledge"))

    units = build_campaign_notes([a, b])

    merged_id = stable_id("campaign", ["a", "b"])
    by_id = {unit.note.id: unit for unit in units}
    assert merged_id in by_id and by_id[merged_id].note.valid_to is None  # the live note
    # The retirement rides the replacement's unit — one PR, one reviewable decision — and points
    # forward with a real superseded-by edge now that the pair shares a branch.
    (retired,) = by_id[merged_id].retirements
    assert retired.id == stale_id and retired.valid_to == date.today()
    assert any(r.rel == "superseded-by" and r.to == merged_id for r in retired.relations)


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


def test_a_note_from_an_incomplete_read_says_so_in_the_body_a_chemist_reads() -> None:
    """`memory_corpus_max_reactions` is justified by "partial knowledge that says it is partial".

    It did not. `corpus_complete=False` skipped the retirement pass and logged a WARNING into a
    worker's log, and the note landing in `knowledge/` was byte-identical to one distilled from
    the whole record — so the only statement that the evidence might be a subset was in a place
    nobody reading the note would look.

    Both halves are asserted in one test on purpose: skipping the retirement pass without marking
    the note, or marking it without skipping, each looks like the fix and is half of it.
    """
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")

    partial = build_playbook_notes([ester_x, ester_y], corpus_complete=False)
    whole = build_playbook_notes([ester_x, ester_y])

    assert partial, "the fixture distilled nothing; this test proves nothing"
    assert all(PARTIAL_READ_CAVEAT in unit.note.body for unit in partial)
    assert all(not unit.retirements for unit in partial)
    assert all(PARTIAL_READ_CAVEAT not in unit.note.body for unit in whole), (
        "a complete read must not caveat its own notes, or the mark says nothing"
    )


def test_the_caveat_names_the_id_risk_the_skipped_retirement_pass_leaves_open() -> None:
    """The second-order consequence, which the flag's other half is what exposes.

    `stable_id` anchors on the cluster's *smallest* member, so a truncated read that drops that
    member mints a **different id for the same cluster** — the case
    `test_shrunk_cluster_retires_the_pre_shrink_note` exists for, and which the retirement pass
    normally resolves by superseding the predecessor. An incomplete read is precisely the run whose
    retirement pass is skipped, so the two notes coexist with nothing linking them, and the caveat
    is the only thing a reader has. Asserted on the id derivation as well as on the words, because
    the sentence is only worth having while the mechanism behind it is real.
    """
    whole = stable_id("playbook", ["r-001", "r-002", "r-003"])
    truncated = stable_id("playbook", ["r-002", "r-003"])

    assert whole != truncated, "dropping the anchor no longer changes the id; re-read the caveat"
    assert "id may differ" in PARTIAL_READ_CAVEAT
    assert "No note was retired" in PARTIAL_READ_CAVEAT


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
    note = playbook_note(
        "playbook-ester", "Fischer esterification recurs.", ["reaction-x", "reaction-y"]
    )
    assert note.type == "playbook"
    assert note.id == "playbook-ester"  # the full note id is passed in, not re-prefixed
    # Evidence is cited verbatim. It used to be bare reaction ids that this function prefixed,
    # which quietly required every caller's evidence to be a reaction — and the observations tier
    # promotes findings evidenced partly by an `interaction` note, which the prefixing turned into
    # a link to a note that cannot exist.
    assert note.outgoing_links() == ["reaction-x", "reaction-y"]
    assert playbook_note("p", "s", ["interaction-42"]).outgoing_links() == ["interaction-42"]
    with pytest.raises(PlaybookError, match="no evidence"):
        playbook_note("playbook-empty", "no evidence here", [])


def test_a_playbook_states_which_of_its_two_producers_wrote_it() -> None:
    """`playbook` has two provenances, and a reader of the merged file has to be able to tell.

    Cluster distillation ("this transformation recurs across projects") and a promoted observation
    ("enough merged notes backed this reading that a human was asked to judge it") are different
    kinds of claim, and both used to stamp `memory:cross-project-distillation` — so the graph said
    the second was the first.

    Derived from the id, never asserted by the caller: distillation anchors on the cluster's
    smallest member, promotion on the observation's scope. A `source` a caller passes could
    disagree with the id, which is why `supersede._is_synthesis_minted` cannot trust one — and
    deriving both from `is_cluster_anchored` is what keeps the note's own statement and the
    retirement rule from ever answering differently.
    """
    evidence = ["reaction-r1", "reaction-r2"]
    distilled = playbook_note(stable_id("playbook", ["r1", "r2"]), "recurs", evidence)
    assert distilled.source == SOURCE_DISTILLATION

    observation = Observation(
        statement="failed in both projects", scope="transformation:r1", evidence_note_ids=evidence
    ).with_id()
    promoted = playbook_note(
        f"playbook-{observation.id.removeprefix('observation-')}", "noticed", evidence
    )
    assert promoted.source == SOURCE_PROMOTED_OBSERVATION
    # The note the graph already distinguishes is also the note supersede must not retire, from the
    # same derivation — one rule, two readings, no way for them to disagree.
    assert supersede_updates([distilled], [promoted], date(2026, 7, 25)) == []


# --- jobs (5.3/5.4 wiring) ------------------------------------------------------------


async def _build_and_propose(units: list[SynthesisUnit], submitter: FakeWriter) -> list[str]:
    """Publish built notes the way the durable job does: one PR-gate proposal each.

    These three tests used to call `synthesize_campaigns` / `distill_playbooks`, which built and
    published in one pass and which nothing in `src/` has run since F10-D2 split the jobs — the
    durable workflow imports the builders and fans each note out to its own child. They are gone, so
    the tests take the same two steps the live path takes rather than a convenience wrapper that
    only tests had.
    """
    return [await record_note(unit.note, submitter, superseded=unit.retirements) for unit in units]


def test_campaign_synthesis_proposes_notes_via_pr_gate() -> None:
    """The campaign job proposes one PR-gated campaign note per detected chain."""
    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O"], ["CC(O)O"], project="proj-x")
    sub = FakeWriter()
    refs = asyncio.run(_build_and_propose(build_campaign_notes([a, b]), sub))
    assert len(refs) == 1
    assert sub.writes[0].files[0].path.startswith("knowledge/campaign/campaign-")


def test_playbook_distillation_proposes_evidence_backed_notes() -> None:
    """The playbook job proposes a cross-project playbook note citing its evidence."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    sub = FakeWriter()
    refs = asyncio.run(_build_and_propose(build_playbook_notes([ester_x, ester_y]), sub))
    assert len(refs) == 1
    assert sub.writes[0].files[0].path.startswith("knowledge/playbook/playbook-")
    assert "proj-x" in sub.writes[0].files[0].content and "proj-y" in sub.writes[0].files[0].content


def test_every_built_campaign_note_reaches_the_pr_gate() -> None:
    """What the builder yields is what the gate receives — ids, order and type (F10-D2).

    The fan-out workflow builds notes in one activity and publishes each in its own child, so the
    property worth pinning is that nothing is dropped or reordered between the two halves. This
    assertion used to be a *parity* between the builder and a publisher nothing ran, which would
    have gone on passing while the live path broke.
    """
    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O"], ["CC(O)O"], project="proj-x")
    units = build_campaign_notes([a, b])
    sub = FakeWriter()
    asyncio.run(_build_and_propose(units, sub))
    assert len(units) == len(sub.writes)
    assert all(u.note.id in s.files[0].path for u, s in zip(units, sub.writes, strict=True))
    assert all(u.note.type == "campaign" for u in units)


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


def test_a_correction_is_recorded_as_a_correction_not_as_agreement() -> None:
    """The one case where the system was demonstrably wrong must not be stored as it agreeing.

    The body was rendered `A (confirmed):` unconditionally, while this module's docstring, the
    tool's docstring and the system prompt all said "confirmed **or corrected**". So a chemist
    correcting an answer — the highest-value thing they ever hand this system, and the only place
    that fact exists — went into the record as a confirmation, and a later reader could not tell
    the two apart.
    """
    note = note_from_confirmed_answer(
        "q-43",
        "What base did we use on the biaryl?",
        "Potassium carbonate, not caesium.",
        None,
        corrected_from="Caesium carbonate, per the earlier screen.",
    )
    body = note.body.lower()
    assert "corrected" in body
    assert "confirmed" not in body, "a correction still reads as a confirmation"
    # And *what* was wrong, not merely that something was: the superseded answer is the signal.
    assert "Caesium carbonate, per the earlier screen." in note.body


def test_a_confirmation_is_unchanged_by_the_correction_path() -> None:
    """Empty `corrected_from` means the chemist confirmed — the default stays exactly as it was."""
    note = note_from_confirmed_answer("q-44", "Did the degas step help?", "Yes, markedly.")
    assert "(confirmed)" in note.body
    assert "corrected" not in note.body.lower()


def test_record_confirmed_answer_tool_uses_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent tool routes a confirmed answer through the (fake) PR-gate (5.5 wiring)."""
    from chemclaw.agent import memory_tools

    fake = FakeWriter()
    monkeypatch.setattr(memory_tools, "default_writer", lambda: fake)
    ref = asyncio.run(
        memory_tools.record_confirmed_answer(
            "q-42", "Best solvent?", "Aqueous dioxane at 90 C.", ["reaction-eln-2026-002"]
        )
    )
    assert ref == "commit://1"
    submitted = fake.writes[0]
    assert submitted.files[0].path.endswith("interaction/interaction-q-42.md")
    assert "reaction-eln-2026-002" in submitted.files[0].content


# --- when a synthesized note became knowledge ----------------------------------------------------


def test_a_synthesized_note_is_dated_by_its_evidence_and_not_by_the_clock() -> None:
    """The date has to be a function of the members, and this is why.

    Every synthesized note is keyed by `stable_id(kind, reaction_ids)`, so a miner re-run over the
    same members mints the same id. A `date.today()` would then rewrite that note with a new
    `valid_from` on every run — the content changes, `record_note` commits, and the digest reports
    it as new again the next day. That is the storm
    `D-2026-09-14-an-undated-note-is-not-news-every-hour` closed, in a new dress.

    Asserted as *stability*, not as a literal: two runs over the same evidence agree, which is the
    property that matters and the one a fixture reworded with different dates cannot fake.
    """
    runs = {
        "r1": _dated("r1", date(2026, 7, 1)),
        "r2": _dated("r2", date(2026, 7, 31)),
    }

    first = supported_from(["r1", "r2"], runs)
    again = supported_from(["r1", "r2"], runs)

    assert first == again == date(2026, 7, 1)


def test_a_cluster_that_gains_a_member_keeps_both_its_id_and_its_date() -> None:
    """The one assertion that would have caught the defect this function shipped with.

    `supported_from` was `max(performed_at)` over the members, and its docstring justified that
    with "when a member joins, the id changes too, so the identity and the date move together or
    not at all". `memory/ids.stable_id` hashes `min(member_ids)` *deliberately* — its own docstring
    says hashing the set would mint a new id on every cluster growth — so the premise was false and
    nothing asserted it either way.

    What that cost: a nightly ELN sync adds a member, the id does not move, `valid_from` rises, and
    `digest._is_new` reports a note the subscriber already holds as news. The silent direction is
    worse — a member dropping out, or a `corpus_complete=False` partial read, lowers `valid_from`
    under one id, and `_is_new` then answers `False` forever for a note whose content just changed.

    So the property is asserted as the docstring states it: over a *growing* cluster, id and date
    are both invariant. `max` fails this; the anchor does not.
    """
    runs = {
        "r1": _dated("r1", date(2026, 7, 1)),
        "r2": _dated("r2", date(2026, 7, 31)),
        "r3": _dated("r3", date(2026, 8, 20)),
    }
    before, after = ["r1", "r2"], ["r1", "r2", "r3"]

    assert stable_id("playbook", before) == stable_id("playbook", after)
    assert supported_from(before, runs) == supported_from(after, runs) == date(2026, 7, 1)


def test_the_date_is_the_anchor_run_rather_than_the_newest_or_the_oldest() -> None:
    """Keyed on `min(reaction_ids)` — the same single input the note's id is keyed on.

    Not the earliest date and not the latest: either is a function of the member *set*, and a
    function of the set moves under an id that is a function of one member. The anchor's own date
    is the only reading that makes the two move together, which is what the id's stability rule
    already promised and what `supported_from` claimed and did not deliver.

    Ordered so the anchor is neither the newest nor the oldest run, because a fixture where it
    happens to be both cannot tell the three rules apart.
    """
    runs = {
        "r2": _dated("r2", date(2026, 7, 15)),
        "r1": _dated("r1", date(2026, 7, 20)),
        "r3": _dated("r3", date(2026, 7, 10)),
    }

    assert min(["r2", "r1", "r3"]) == "r1"
    assert supported_from(["r2", "r1", "r3"], runs) == date(2026, 7, 20)


def test_evidence_that_states_no_date_leaves_the_note_open_ended() -> None:
    """`None` is the truthful answer, not a fallback to today.

    `OrdReaction.performed_at` is optional because a source may not state one, and a corpus that
    never said when its runs happened cannot support a narrower claim than "open-ended".
    """
    runs = {"r1": _reaction("r1", ["CC"], ["CCO"])}

    assert supported_from(["r1"], runs) is None


def test_a_member_the_corpus_does_not_hold_is_skipped_rather_than_raising() -> None:
    """A partial corpus read is an ordinary condition here — `_units` has a whole guard for it.

    Three cases, because the two-tier rule answers each differently and the differences are the
    whole design: a missing *non-anchor* is invisible (the point — the date does not move as the
    cluster changes around a dated anchor); a missing or undated *anchor* falls to the earliest
    dated member rather than leaving the note open-ended, which is the coverage the first version
    of this fix lost; and a corpus that dates nothing at all is still `None`, because that is the
    truthful answer rather than a fallback to today.
    """
    runs = {"r1": _dated("r1", date(2026, 7, 1)), "r2": _dated("r2", date(2026, 8, 20))}

    assert supported_from(["r1", "zz-missing"], runs) == date(2026, 7, 1)
    assert supported_from(["aa-missing", "r1", "r2"], runs) == date(2026, 7, 1)
    assert supported_from([], runs) is None
    assert supported_from(["r1"], {"r1": _reaction("r1", ["CC"], ["CCO"])}) is None


def test_an_undated_anchor_falls_to_the_earliest_dated_member_rather_than_to_nothing() -> None:
    """The coverage this fix lost on its first attempt, asserted so it cannot be lost again.

    Anchoring the date on `min(reaction_ids)` made the note open-ended whenever *the anchor* was
    undated, however many members carried dates — `None` went from meaning "no member is dated" to
    "one particular member is not", which for a per-member dating probability `p` over `n` members
    takes the undated rate from `(1-p)^n` to `(1-p)`. An undated note reaches no subscriber holding
    a watermark, so that is a regression in the metric this whole wave exists to improve, and the
    first version stated it as a residual without measuring it.

    Tier 2 is `min` rather than `max` for the reason tier 1 exists at all: a cluster growing
    forward in time does not move its earliest member, where `max(performed_at)` moved on every
    arrival.
    """
    runs = {
        "r1": _reaction("r1", ["CC"], ["CCO"]),  # the anchor, undated
        "r2": _dated("r2", date(2026, 8, 20)),
        "r3": _dated("r3", date(2026, 7, 31)),
    }

    assert min(["r1", "r2", "r3"]) == "r1", "the fixture's anchor must be the undated one"
    assert supported_from(["r1", "r2", "r3"], runs) == date(2026, 7, 31)

    # And it is still stable under growth: a later run joining moves neither tier.
    runs["r4"] = _dated("r4", date(2026, 9, 30))
    assert supported_from(["r1", "r2", "r3", "r4"], runs) == date(2026, 7, 31)


def _dated(rid: str, performed_at: date) -> OrdReaction:
    """A reaction that says when it was run."""
    return _reaction(rid, ["CC"], ["CCO"]).model_copy(update={"performed_at": performed_at})


def test_every_miner_dates_the_note_it_mints() -> None:
    """Three builders, one defect — fixing one would leave the other two silently unreachable.

    `D-2026-09-14-an-undated-note-is-not-news-every-hour` named only the playbook producer and left
    the rest "for the pass that touches it". The argument does not distinguish them: a mined note
    became knowledge the day its evidence did.

    **This assertion has been wrong twice, each time one notch less wrong, and the third version is
    the first that drives anything.** It shipped asserting `"minted_on" in kwargs`, and all three
    builders re-broken to `minted_on=None` left 38 passing — an AST guard reading the call's
    *shape*. It was then "fixed" to assert `ast.unparse(value).startswith("supported_from(")`,
    which is the callee's *name*: `minted_on=supported_from([], by_id)` (always `None`) and
    `minted_on=supported_from(sorted(ids)[1:], by_id)` — the original defect in a new dress, a date
    keyed on something other than the id's own input — both passed, 33 green. The list of builders
    was hardcoded besides, so a fourth miner minting undated notes was invisible.

    So it drives the builders and reads the result. The property is the one the whole fix rests on:
    **a note's `valid_from` is the date `supported_from` derives from the members its own id is
    anchored on**, recomputed here from the note's citations rather than from the builder's
    arguments, so a date wired to a different member set fails.
    """
    from chemclaw.memory.ids import MEMBER_PREFIX
    from chemclaw.memory.jobs import (
        build_campaign_notes,
        build_optimization_notes,
        build_playbook_notes,
        supported_from,
    )

    def _on(reaction: OrdReaction, day: date) -> OrdReaction:
        """The corpus's own reaction, dated — `_dated` builds its own fixed structure."""
        return reaction.model_copy(update={"performed_at": day})

    chain = [
        _on(_reaction("c1", ["CCO"], ["CC=O"]), date(2026, 7, 10)),
        _on(_reaction("c2", ["CC=O"], ["CC(O)O"]), date(2026, 8, 20)),
    ]
    playbook = [
        _on(
            _reaction("p1", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x"),
            date(2026, 7, 10),
        ),
        _on(
            _reaction("p2", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y"),
            date(2026, 8, 20),
        ),
    ]
    optimization = [
        _on(_reaction("o1", ["CCO", "CC(=O)O"], ["CCOC(C)=O"]), date(2026, 7, 10)),
        _on(_reaction("o2", ["CCO", "CC(=O)O"], ["CCOC(C)=O"]), date(2026, 8, 20)),
    ]

    built = {
        "campaign": (build_campaign_notes(chain), chain),
        "playbook": (build_playbook_notes(playbook), playbook),
        "optimization": (build_optimization_notes(optimization), optimization),
    }

    for kind, (units, corpus) in built.items():
        assert units, f"the {kind} fixture built nothing; this test would prove nothing"
        by_id = {reaction.reaction_id: reaction for reaction in corpus}
        for unit in units:
            members = [
                cited.removeprefix(MEMBER_PREFIX)
                for cited in unit.note.outgoing_links()
                if cited.startswith(MEMBER_PREFIX)
            ]
            assert members, f"the {kind} note cites no member, so its date cannot be checked"
            expected = supported_from(members, by_id)
            assert expected is not None, "the fixture must date its runs, or this proves nothing"
            assert unit.note.valid_from == expected, (
                f"the {kind} note's valid_from is {unit.note.valid_from}, not the "
                f"{expected} its own cited members anchor. A date keyed on anything but the "
                "members the id is keyed on moves under a stable id — the storm D-2026-09-14 "
                "closed, and the defect the first two versions of this test could not see."
            )


def test_no_miner_mints_a_note_without_passing_the_corpus_derived_date() -> None:
    """The builder list is derived from the calls, not written down beside them.

    The driving test above proves the three miners that exist are wired correctly; this one is what
    notices a *fourth*. Its predecessor hardcoded `["campaign_note_from_chain", "playbook_note",
    "optimization_campaign_note"]` while holding every `ast.Call` in the module, so a new miner
    minting undated notes passed — cause (f) in `tasks/lessons.md`, a universe that is a strict
    subset of the surface at risk.

    Every call in `memory/jobs.py` to a `*_note*` builder must pass `minted_on`, and the argument
    must name `supported_from`. That is deliberately the weaker of the two assertions — the strong
    one is above — because its job is coverage rather than correctness.
    """
    import ast
    from pathlib import Path

    import chemclaw.memory.jobs as jobs

    tree = ast.parse(Path(jobs.__file__).read_text(encoding="utf-8"))
    thin: dict[str, str] = {}
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if not (node.func.id.endswith("_note") or "_note_" in node.func.id):
            continue
        seen += 1
        passed = {keyword.arg: keyword.value for keyword in node.keywords}
        if "minted_on" not in passed:
            thin[node.func.id] = "no minted_on at all"
        elif "supported_from" not in ast.unparse(passed["minted_on"]):
            thin[node.func.id] = ast.unparse(passed["minted_on"])
    assert seen >= 3, f"only {seen} note builders found in memory/jobs.py; this scan is stale"
    assert not thin, (
        f"{thin} mint notes the digest reads as open-ended, so they reach no subscriber who has a "
        "watermark. Every miner dates its note from the members its id is anchored on."
    )
