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
from chemclaw.kg import analytics
from chemclaw.kg.analytics import analyze
from chemclaw.kg.graph import build_graph, load_notes
from chemclaw.kg.note import KNOWN_NOTE_TYPES, Note, known_note_types
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
    """A small graph: two linked reactions, one orphan, one *tag* with no distillation.

    The tags are topic tags (`amide-coupling`, `suzuki`) because that is what the corpus actually
    holds — `Note` has no project field — and a fixture that says `proj-x` invites the reader to
    believe the gap query knows about projects.
    """
    _write(
        tmp_path,
        Note(id="reaction-a", type="reaction", tags=["amide-coupling"], body="see [[hub]]"),
    )
    _write(tmp_path, Note(id="reaction-b", type="reaction", tags=["suzuki"], body="see [[hub]]"))
    _write(tmp_path, Note(id="hub", type="playbook", tags=["suzuki"], body="the rule"))
    _write(
        tmp_path,
        Note(id="orphan", type="reaction", tags=["amide-coupling"], body="nothing links here"),
    )
    return tmp_path


# --- KNW-5: gap queries ----------------------------------------------------------------------


def test_unreachable_notes_are_named(corpus: Path) -> None:
    """An unlinked note is reachable only by a literal substring hit — invisible to traversal."""
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.isolated_note_ids == ["orphan"]
    assert gaps.total_notes == 4


def test_a_tag_with_evidence_but_no_distillation_is_surfaced(corpus: Path) -> None:
    """The concrete "what don't we know" answer: where synthesis is owed.

    `amide-coupling` has reactions and nothing distilled from them; `suzuki` has a playbook.
    """
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.tags_without_distillation == ["amide-coupling"]


def test_the_gap_query_never_calls_a_tag_a_project(corpus: Path) -> None:
    """The field named these free-text tags "projects" and the model believed the field.

    A live run turned this list into a confident portfolio status — "27 projects tagged" — from a
    computation that is a set difference over `note.tags` and has no project concept anywhere near
    it (`Note` carries no project field). The computation was never wrong; the name asserted
    something false, and a field name is all the model has to go on. Asserted over the serialized
    payload because that, not the Python attribute, is what reaches the agent via
    `find_knowledge_gaps`.
    """
    payload = analyze(build_graph(corpus), load_notes(corpus)).model_dump()
    assert not [key for key in payload if "project" in key]
    assert payload["tags_without_distillation"] == ["amide-coupling"]


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
    """The registry must cover what the system actually writes, or the gate cries wolf.

    Against the *effective* vocabulary, because one of these is minted by a bundle rather than by
    core: `bo-candidate` by `bo`, declared in its own `connector.yaml`. That is the point of the
    union — what this deployment can write is core's set plus its enabled bundles' — and checking
    core's frozenset alone would assert that a bundle's note type is registered in a file that
    deliberately does not name it.
    """
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
    assert minted <= known_note_types()


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


def test_the_distilled_types_are_all_real_note_types() -> None:
    """A typo in `_DISTILLED_TYPES` disables the gap query silently, in the direction of "clean".

    `_undistilled_tags` is a set difference: a misspelt distilling type moves its notes to the
    *evidence* side, so a tag that has a playbook starts being reported as needing one. Nothing
    else would notice — the query still runs, still returns a list, and is simply wrong. Same
    class of defect as an unregistered note type (KNW-6), one level in.
    """
    assert analytics._DISTILLED_TYPES <= KNOWN_NOTE_TYPES


def test_hubs_never_name_a_note_that_does_not_exist(tmp_path: Path) -> None:
    """A dangling link target is reported as dangling, never as the graph's top hub.

    `build_graph` deliberately keeps a link to an unknown id as a node with no `note` attribute so
    `kg-validate` can report it, and `_hubs` ranked those nodes by the very citations that make
    them dangling. Measured before the fix: four notes citing a `compound-pending` that does not
    exist put it back as *the most-cited note in the graph*.

    This is the state D-018 calls normal, not a corruption — a fingerprint-indexed reaction is
    citable before its note clears the PR-gate — so the tool was telling a chemist to check the
    hub that matters most and `expand_note` on it then raised.
    """
    for index in range(4):
        _write(
            tmp_path,
            Note(id=f"reaction-{index}", type="reaction", body="rests on [[compound-pending]]"),
        )
    _write(tmp_path, Note(id="hub", type="playbook", body="the rule"))
    _write(tmp_path, Note(id="reaction-x", type="reaction", body="see [[hub]]"))

    gaps = analyze(build_graph(tmp_path), load_notes(tmp_path))

    assert [note_id for note_id, _ in gaps.most_cited] == ["hub"]
    assert gaps.most_cited[0] == ("hub", 1)
    # Not dropped — reported as what it is, by the same call, from the same graph.
    assert len(gaps.dangling_links) == 4
    assert "reaction-0 -> compound-pending" in gaps.dangling_links
