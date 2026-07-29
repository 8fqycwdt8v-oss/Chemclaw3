"""Knowledge-model completeness: gap queries, type registry, negative results, source tiers.

Four W4 findings that share a theme — the corpus could record and retrieve, but could not reason
about *itself*:

- **KNW-5** the graph could only be walked outward from a hit, so "what do we know about X" was
  answerable and "what don't we know" was not — and the second is the question that steers
  experimental design.
- **KNW-6** `type` was an unconstrained slug written from nine call sites, so a typo minted a new
  type silently and any retrieval filter keyed on type then missed with no error.
- **KNW-3** nothing marked an experiment as failed, and the distillation is *structurally* biased
  against failures: playbooks distil what recurs, and failures get abandoned after one attempt.
- **IDEA-5** RRF fused a validated ELN entry and a transferred analogy identically.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.ingest.eln.ord import Component, OrdReaction, OutcomeClass, Role
from chemclaw.kg.analytics import analyze
from chemclaw.kg.graph import build_graph, load_notes
from chemclaw.kg.note import KNOWN_NOTE_TYPES, Note
from chemclaw.kg.render import render_note
from chemclaw.kg.validate import validate
from chemclaw.memory.playbook import find_playbook_candidates
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion


def _write(directory: Path, note: Note) -> None:
    """Lay a note down the way the PR-gate does, so the readers under test see a real corpus."""
    path = directory / note.type / f"{note.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(note))


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A small graph: two linked reactions, one orphan, one project with no distillation."""
    _write(tmp_path, Note(id="reaction-a", type="reaction", tags=["proj-x"], body="see [[hub]]"))
    _write(tmp_path, Note(id="reaction-b", type="reaction", tags=["proj-y"], body="see [[hub]]"))
    _write(tmp_path, Note(id="hub", type="playbook", tags=["proj-y"], body="the rule"))
    _write(tmp_path, Note(id="orphan", type="reaction", tags=["proj-x"], body="nothing links here"))
    return tmp_path


# --- KNW-5: gap queries ----------------------------------------------------------------------


def test_unreachable_notes_are_named(corpus: Path) -> None:
    """An unlinked note is reachable only by a literal substring hit — invisible to traversal."""
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.isolated_note_ids == ["orphan"]
    assert gaps.total_notes == 4


def test_a_project_with_evidence_but_no_distillation_is_surfaced(corpus: Path) -> None:
    """The concrete "what don't we know" answer: where synthesis is owed.

    proj-x has reactions and nothing distilled from them; proj-y has a playbook, so it is done.
    """
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.projects_without_distillation == ["proj-x"]


def test_hubs_are_ranked_so_a_reviewer_knows_where_an_error_propagates(corpus: Path) -> None:
    """The most-cited note is where a mistake spreads furthest — check it first."""
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.most_cited[0] == ("hub", 2)


def test_type_counts_show_the_shape_of_the_corpus(corpus: Path) -> None:
    """Which area has the least evidence is a count, and nothing exposed one."""
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.type_counts == {"playbook": 1, "reaction": 3}


def test_analysis_of_an_empty_graph_is_empty_not_an_error(tmp_path: Path) -> None:
    """A fresh deployment ships an empty graph; asking it what it lacks must still work."""
    gaps = analyze(build_graph(tmp_path), load_notes(tmp_path))
    assert gaps.total_notes == 0
    assert gaps.isolated_note_ids == []


# --- KNW-6: the note-type registry -----------------------------------------------------------


def test_a_typo_in_a_note_type_fails_the_gate(tmp_path: Path) -> None:
    """Previously silent: the note landed and every type-filtered retrieval then missed it."""
    _write(tmp_path, Note(id="oops", type="reactio", body="typo"))
    problems = validate(tmp_path)
    assert any("unknown type" in p and "oops" in p for p in problems)


def test_every_type_the_code_mints_is_registered() -> None:
    """The registry must cover what the system actually writes, or the gate cries wolf."""
    minted = {
        "reaction",
        "campaign",
        "optimization-campaign",
        "playbook",
        "interaction",
        "report",
        "job-result",
        "bo-candidate",
    }
    assert minted <= KNOWN_NOTE_TYPES


def test_the_registry_is_not_enforced_at_the_schema() -> None:
    """The agent may propose a genuinely new type; the PR-gate + this CI gate are the control.

    A hard schema rejection would block that at the tool, where no human is present to judge it.
    """
    assert Note(id="n", type="brand-new-kind").type == "brand-new-kind"


# --- KNW-3: negative results -----------------------------------------------------------------


def _reaction(**overrides: object) -> OrdReaction:
    base: dict[str, object] = {
        "reaction_id": "r",
        "inputs": [Component(smiles="CCO", role=Role.REACTANT)],
        "outcomes": [Component(smiles="CC=O", role=Role.PRODUCT)],
        "provenance": "eln",
        "project": "p",
    }
    return OrdReaction(**{**base, **overrides})  # type: ignore[arg-type]


def test_silence_still_means_an_ordinary_run() -> None:
    """Defaulting to success preserves the meaning of every record written before the field."""
    assert _reaction().outcome_class is OutcomeClass.SUCCESS


def test_a_failure_must_say_why() -> None:
    """A negative result's whole value is the reason; unexplained, it just looks like evidence."""
    with pytest.raises(ValidationError):
        _reaction(outcome_class=OutcomeClass.FAILURE)
    assert _reaction(outcome_class=OutcomeClass.FAILURE, failure_reason="decomposed").failure_reason


def test_inconclusive_is_distinct_from_failure() -> None:
    """An aborted or unassayed run carries no evidence about the chemistry; conflating them lies."""
    assert _reaction(outcome_class=OutcomeClass.INCONCLUSIVE).failure_reason is None


def test_a_recurring_failure_never_distils_into_a_playbook() -> None:
    """The load-bearing fix: playbooks distil what *recurs*, and a repeated failure recurs.

    Without the filter, the same failed conditions tried in two projects would be distilled into a
    transferable recommendation — the exact inversion of what the record says.
    """
    failures = [
        _reaction(reaction_id="f1", project="p1", outcome_class="failure", failure_reason="tar"),
        _reaction(reaction_id="f2", project="p2", outcome_class="failure", failure_reason="tar"),
    ]
    assert find_playbook_candidates(failures) == []
    successes = [
        _reaction(reaction_id="s1", project="p1"),
        _reaction(reaction_id="s2", project="p2"),
    ]
    assert find_playbook_candidates(successes), "successes should still distil"


# --- IDEA-5: source-tier weighting -----------------------------------------------------------


def _chunk(note_id: str, retriever: str) -> EvidenceChunk:
    return EvidenceChunk(content="x", source_note_id=note_id, retriever=retriever)


def test_unweighted_fusion_is_unchanged() -> None:
    """The default must reproduce today's behavior exactly — this ships inert."""
    lists = [[_chunk("a", "graph")], [_chunk("b", "vector")]]
    assert [c.source_note_id for c in reciprocal_rank_fusion(lists, k=60)] == ["a", "b"]
    weighted = reciprocal_rank_fusion(lists, k=60, weights={})
    assert [c.source_note_id for c in weighted] == ["a", "b"]


def test_a_trusted_source_outranks_an_equally_ranked_weaker_one() -> None:
    """RRF is score-agnostic — right for combining rankers, wrong for evidence classes (IDEA-5)."""
    lists = [[_chunk("analogy", "vector")], [_chunk("measured", "graph")]]
    ordered = reciprocal_rank_fusion(lists, k=60, weights={"graph": 3.0})
    assert [c.source_note_id for c in ordered] == ["measured", "analogy"]


def test_an_unlisted_retriever_keeps_neutral_weight() -> None:
    """Adding a weight for one source must not silently demote every other source."""
    lists = [[_chunk("a", "graph")], [_chunk("b", "brand-new")]]
    ordered = reciprocal_rank_fusion(lists, k=60, weights={"graph": 1.0})
    assert {c.source_note_id for c in ordered} == {"a", "b"}
