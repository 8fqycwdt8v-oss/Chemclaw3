"""Knowledge-model completeness: gap queries, type registry, negative results, source tiers.

The corpus must be able to reason about itself:

- gap queries answer "what don't we know", the question that steers experimental design;
- note types come from a registry, so a typo cannot mint a type that retrieval filters then miss;
- an experiment can be marked failed, since distillation is biased toward what recurs;
- fusion can weigh sources differently, rather than treating a validated ELN entry and a
  transferred analogy identically.
"""

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.ingest.eln.ord import Component, OrdReaction, OutcomeClass, Role
from chemclaw.kg import analytics
from chemclaw.kg.analytics import analyze
from chemclaw.kg.graph import build_graph, load_notes
from chemclaw.kg.note import KNOWN_NOTE_TYPES, UNDISTILLED_TAG, Note, known_note_types
from chemclaw.kg.render import render_note
from chemclaw.kg.validate import validate
from chemclaw.memory.playbook import find_playbook_candidates
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion, with_no_leg_cut_out


def _write(directory: Path, note: Note) -> None:
    """Lay a note down the way the PR-gate does, so the readers under test see a real corpus."""
    path = directory / note.type / f"{note.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(note))


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A small graph: two linked reactions, one orphan, one tag with no distillation.

    The tags are topic tags (`amide-coupling`, `suzuki`), since `Note` has no project field.
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


def test_a_playbook_that_records_a_recurrence_and_states_no_rule_is_named(corpus: Path) -> None:
    """A playbook that records a recurrence and states no rule is named.

    `tags_without_distillation` asks which topics have evidence and no playbook; this asks which
    playbooks record a recurrence nobody has generalised. Independent: `suzuki` has a playbook, so
    it is not in the first list, yet that playbook still awaits a rule. Asserted with a distilled
    playbook present too, so naming every playbook does not pass.
    """
    _write(
        corpus,
        Note(
            id="playbook-recurrence",
            type="playbook",
            tags=["suzuki", UNDISTILLED_TAG],
            created_by="agent",
            body="This transformation recurs across 2 projects.",
        ),
    )
    gaps = analyze(build_graph(corpus), load_notes(corpus))
    assert gaps.undistilled_playbook_ids == ["playbook-recurrence"], (
        "a playbook awaiting a rule is not reported, so the only way to find one is to read every "
        "playbook in the corpus"
    )
    assert "hub" not in gaps.undistilled_playbook_ids, "a distilled playbook is not awaiting one"
    assert "suzuki" not in gaps.tags_without_distillation, (
        "the fixture no longer shows the two fields answering different questions"
    )


def test_the_gap_query_never_calls_a_tag_a_project(corpus: Path) -> None:
    """The gap query never calls a tag a project.

    The computation is a set difference over `note.tags`, with no project concept; a field named
    "projects" would make the model report project status. Asserted over the serialized payload,
    which is what `find_knowledge_gaps` hands the agent.
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
    """Every type the code mints is registered.

    Checked against the effective vocabulary: core's set plus enabled bundles' declared types (e.g.
    `bo-candidate` from `bo`'s `connector.yaml`).
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


def test_silence_is_not_a_successful_run() -> None:
    """Silence is not a successful run.

    A source that said nothing about the outcome has not said it worked. `None` rather than
    `INCONCLUSIVE`, which is a statement somebody made about the chemistry.
    """
    assert _reaction().outcome_class is None
    assert _reaction(outcome_class=OutcomeClass.SUCCESS).outcome_class is OutcomeClass.SUCCESS


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
    # Stated successes, because since `D-2026-08-26-silence-is-not-a-successful-run` an unset
    # outcome is not one — and the filter that drops failures drops unassessed runs by the same
    # identity test. Asserted below so the third state is pinned here rather than only implied.
    successes = [
        _reaction(reaction_id="s1", project="p1", outcome_class=OutcomeClass.SUCCESS),
        _reaction(reaction_id="s2", project="p2", outcome_class=OutcomeClass.SUCCESS),
    ]
    assert find_playbook_candidates(successes), "stated successes should still distil"
    unassessed = [
        _reaction(reaction_id="u1", project="p1"),
        _reaction(reaction_id="u2", project="p2"),
    ]
    assert find_playbook_candidates(unassessed) == [], (
        "a playbook says 'this works'; distilling one from runs nobody assessed is a claim "
        "built on silence"
    )


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


def test_a_weighted_source_cannot_starve_another_source_out_of_the_cap() -> None:
    """A weighted source cannot starve another source out of the cap.

    RRF's rank term is nearly flat at `k=60` (rank 1 is 0.01639, rank 30 is 0.01111), so a
    multiplicative weight of 1.5 lets a source's rank-30 hit outrank every other source's best. The
    invariant pinned is general: for any weights, every source's rank-1 hit survives a cap of 40
    over four sources.
    """
    depths = {"graph": 45, "lexical": 8, "vector": 7, "share": 10}
    lists = [
        [_chunk(f"{source}-{rank}", source) for rank in range(depth)]
        for source, depth in depths.items()
    ]
    weights = {"graph": 1.5, "vector": 0.8}

    fused = reciprocal_rank_fusion(lists, k=60, weights=weights)[:40]
    surviving = Counter(chunk.retriever for chunk in fused)

    assert surviving["vector"] > 0, (
        f"the dense leg contributed nothing to the fused sweep: {dict(surviving)}"
    )
    best = {chunk.source_note_id for chunk in fused}
    assert {f"{source}-0" for source in depths} <= best, (
        "a weight may reorder tiers; it may not push a source's own best hit below another "
        f"source's tail — survivors: {dict(surviving)}"
    )


#: The five legs and depths the `BACKLOG.md` row measured its starvation over, kept as one
#: constant because three tests below are about the *same* sweep seen at different cuts.
_THE_LEGS_THE_ROW_MEASURED = {"graph": 45, "lexical": 8, "share": 10, "vector": 7, "warehouse": 12}


def _the_sweep_the_row_measured() -> list[list[EvidenceChunk]]:
    """One ranked list per leg, every note unique to its leg: the worst case for a floor.

    Overlap rescues a leg by collecting votes; with none, a weight can put one leg's whole list
    above every other leg's best hit.
    """
    return [
        [_chunk(f"{leg}-{rank}", leg) for rank in range(depth)]
        for leg, depth in _THE_LEGS_THE_ROW_MEASURED.items()
    ]


def _kept_per_leg(kept: list[EvidenceChunk], legs: list[list[EvidenceChunk]]) -> dict[str, int]:
    """Count survivors by what each leg offered, never by `chunk.retriever`.

    The fusion keeps the first chunk it sees for a note, so `retriever` names whichever leg ran
    first and would under-count later legs.
    """
    survivors = {chunk.source_note_id for chunk in kept}
    return {
        leg[0].retriever: len(survivors & {chunk.source_note_id for chunk in leg})
        for leg in legs
        if leg
    }


def test_a_weight_can_take_the_whole_window_and_the_floor_gives_every_leg_one_back() -> None:
    """A weight can take the whole window, and the floor gives every leg one slot back.

    `retrieval_source_weights` refuses only non-positive and non-finite values, so `{"graph": 10}`
    is valid, and at a cut of eight it keeps only graph chunks. Both halves at the shipped
    `retrieval_fusion_k`.
    """
    legs = _the_sweep_the_row_measured()
    fused = reciprocal_rank_fusion(legs, k=60, weights={"graph": 10.0})

    assert _kept_per_leg(fused[:8], legs) == {
        "graph": 8,
        "lexical": 0,
        "share": 0,
        "vector": 0,
        "warehouse": 0,
    }

    floored = with_no_leg_cut_out(fused, legs, limit=8)
    assert _kept_per_leg(floored[:8], legs) == {
        "graph": 4,
        "lexical": 1,
        "share": 1,
        "vector": 1,
        "warehouse": 1,
    }, "every leg that offered something keeps one, and the weight still buys graph the rest"


def test_the_floor_is_the_identity_wherever_no_leg_was_at_zero() -> None:
    """The floor is the identity wherever no leg was at zero.

    Swept over the uniform case and the starving weight at the shipped cut
    (`gather_evidence_max_chunks`) and two smaller ones. Identity is asserted on the chunk list,
    since a per-leg count can match while the order moved.
    """
    legs = _the_sweep_the_row_measured()
    moved = []
    for weights in (None, {"graph": 1.5, "vector": 0.8}, {"graph": 10.0}):
        fused = reciprocal_rank_fusion(legs, k=60, weights=weights)
        for cut in (8, 30, 40):
            floored = with_no_leg_cut_out(fused, legs, limit=cut)
            starved = [leg for leg, n in _kept_per_leg(fused[:cut], legs).items() if n == 0]
            if fused[:cut] != floored[:cut]:
                moved.append((weights, cut, starved))
            assert starved or fused[:cut] == floored[:cut], (
                f"nothing was starved at weights={weights} cut={cut} and the floor moved the "
                "ranking anyway"
            )

    assert moved == [({"graph": 10.0}, 8, ["lexical", "share", "vector", "warehouse"])], (
        f"exactly one of the nine cases should move, and it is the starved one: {moved}"
    )


def _a_floor_that_reads_the_retriever_field(
    fused: list[EvidenceChunk], legs: list[list[EvidenceChunk]], limit: int
) -> list[EvidenceChunk]:
    """The floor this repository did not build, coded so a test can separate the two.

    Identical to `with_no_leg_cut_out` except that it reads `chunk.retriever`.
    """
    reserved = set()
    for leg in legs:
        places = [index for index, chunk in enumerate(fused) if chunk.retriever == leg[0].retriever]
        if places:
            reserved.add(places[0])
    keep = set(reserved)
    for index in range(len(fused)):
        if len(keep) >= limit:
            break
        keep.add(index)
    return [chunk for index, chunk in enumerate(fused) if index in keep] + [
        chunk for index, chunk in enumerate(fused) if index not in keep
    ]


def test_the_floor_reads_what_a_leg_offered_not_who_found_the_note_first() -> None:
    """The floor reads what a leg offered, not who found the note first.

    Three legs with heavily overlapping notes: every leg offered every note in the window, so
    nothing is starved and the correct answer is the identity. A `retriever`-reading floor sees two
    legs at zero and promotes the lowest-ranked notes carrying their labels, evicting well-ranked
    ones.
    """
    legs = [
        [_chunk(note, leg) for note in notes]
        for leg, notes in (
            ("alpha", ["n0", "n3", "n1", "n4", "n5", "n8", "n7", "n2"]),
            ("beta", ["n3", "n2", "n4", "n1", "n9", "n8"]),
            ("gamma", ["n7", "n5", "n4", "n8", "n6", "n1", "n3"]),
        )
    ]
    fused = reciprocal_rank_fusion(legs, k=60, weights={"alpha": 2.0, "beta": 2.0, "gamma": 1.0})

    # Nothing is starved: at a window of three, all three legs offered all three notes.
    assert _kept_per_leg(fused[:3], legs) == {"alpha": 3, "beta": 3, "gamma": 3}
    assert with_no_leg_cut_out(fused, legs, limit=3)[:3] == fused[:3], "the correct answer is inert"

    naive = _a_floor_that_reads_the_retriever_field(fused, legs, 3)[:3]
    assert naive != fused[:3], "the field-reading floor must move this window, or nothing separates"
    assert _kept_per_leg(naive, legs) == {"alpha": 1, "beta": 2, "gamma": 2}, (
        "the field-reading floor should make every leg's representation *worse* here, which is "
        "what makes this a design choice rather than a preference"
    )


def test_a_window_smaller_than_the_leg_count_is_decided_by_the_fusion() -> None:
    """A window smaller than the leg count is decided by the fusion.

    Reserving in `ranked_lists` order would let the config's line order decide retrieval.
    """
    legs = _the_sweep_the_row_measured()
    fused = reciprocal_rank_fusion(legs, k=60, weights={"graph": 10.0})
    floored = with_no_leg_cut_out(fused, legs, limit=2)

    assert len(floored) == len(fused), "the floor reorders; it never drops a chunk"
    kept = _kept_per_leg(floored[:2], legs)
    assert sum(kept.values()) == 2 and kept["graph"] == 1, (
        f"graph's best hit is the fusion's own top and must keep its slot: {kept}"
    )


def test_the_floor_holds_under_the_two_stage_corpus_fusion() -> None:
    """The floor holds under the two-stage corpus fusion.

    `corpora` relabels a representative's `retriever` to its corpus name; the floor reads
    `source_note_id`, which the relabelling does not touch.
    """
    legs = _the_sweep_the_row_measured()
    corpora = ["knowledge-notes", "knowledge-notes", "sharedrive", "knowledge-notes", "warehouse"]
    fused = reciprocal_rank_fusion(legs, k=60, weights={"graph": 10.0}, corpora=corpora)

    starved = [leg for leg, n in _kept_per_leg(fused[:8], legs).items() if n == 0]
    floored = with_no_leg_cut_out(fused, legs, limit=8)
    assert not [leg for leg, n in _kept_per_leg(floored[:8], legs).items() if n == 0], (
        f"legs starved under the corpus path and the floor did not reach them: {starved}"
    )


def test_the_distilled_types_are_all_real_note_types() -> None:
    """The distilled types are all real note types.

    `_undistilled_tags` is a set difference, so a misspelt distilling type would report tags that
    have a playbook as needing one, with no error.
    """
    assert analytics._DISTILLED_TYPES <= KNOWN_NOTE_TYPES


def test_hubs_never_name_a_note_that_does_not_exist(tmp_path: Path) -> None:
    """Hubs never name a note that does not exist.

    `build_graph` keeps a link to an unknown id as a node without a `note` attribute so
    `kg-validate` can report it; ranking hubs by citations would surface such a node, and
    `expand_note` on it would raise. A cited-but-unstored note is a normal state, not corruption.
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


def test_a_non_positive_source_weight_is_refused_by_the_config() -> None:
    """A non-positive source weight is refused by the config.

    A weight divides the rank: zero divides by zero and a negative one inverts a source.
    """
    from chemclaw.core.config.retrieval import RetrievalSettings

    with pytest.raises(ValidationError, match="must be positive"):
        RetrievalSettings(retrieval_source_weights={"graph": 0.0})
    assert RetrievalSettings(retrieval_source_weights={"graph": 1.5}).retrieval_source_weights


def test_a_non_finite_source_weight_is_refused_by_the_config() -> None:
    """A non-finite source weight is refused by the config.

    `nan <= 0` is False, and a NaN weight makes every fused score it touches NaN, so `sorted`
    returns an order that is not a ranking. `+inf` makes `rank / inf` zero for every rank, so a
    source's best and worst hits score identically: no ordering at all.
    """
    from chemclaw.core.config.retrieval import RetrievalSettings

    for weight in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="finite|positive"):
            RetrievalSettings(retrieval_source_weights={"graph": weight})


# --- Relation direction and note-shape hardening ---
#
# The gate checks each edge's direction against the vocabulary's declared directions, not only the
# relation names.


def test_an_inverted_edge_fails_the_gate(tmp_path: Path) -> None:
    """`product-of` runs compound → reaction; a reaction asserting it is the classic inversion."""
    _write(tmp_path, Note(id="rxn-x", type="reaction", body="[[product-of:compound-y]]"))
    _write(tmp_path, Note(id="compound-y", type="compound", body="the product"))
    problems = validate(tmp_path)
    assert any("inverse direction" in p and "rxn-x" in p for p in problems)


def test_a_correctly_directed_edge_passes_the_gate(tmp_path: Path) -> None:
    """The same pair the right way round is clean — the check must not cry wolf."""
    _write(tmp_path, Note(id="rxn-x", type="reaction", body="made [[compound-y]]"))
    _write(tmp_path, Note(id="compound-y", type="compound", body="[[product-of:rxn-x]]"))
    assert validate(tmp_path) == []


def test_a_signature_never_fires_on_a_dangling_target(tmp_path: Path) -> None:
    """A dangling target is the dangling-link check's finding.

    Reporting it twice would send a reader two ways about one problem.
    """
    _write(tmp_path, Note(id="compound-y", type="compound", body="[[product-of:rxn-ghost]]"))
    problems = validate(tmp_path)
    assert any("unknown note" in p for p in problems)
    assert not any("inverse direction" in p for p in problems)


def test_a_note_filed_under_the_wrong_type_directory_fails_the_gate(tmp_path: Path) -> None:
    """The directory is an index key like the filename.

    The PR-gate derives a note's path from its type, so a mis-filed note means the next proposal
    for the same id writes a second file — and first-in-path-order then serves the stale one.
    """
    path = tmp_path / "compound" / "pb-x.md"
    path.parent.mkdir(parents=True)
    path.write_text(render_note(Note(id="pb-x", type="playbook", body="misfiled")))
    problems = validate(tmp_path)
    assert any("second file" in p and "pb-x" in p for p in problems)


def test_a_typo_in_a_frontmatter_key_fails_the_gate(tmp_path: Path) -> None:
    """Pydantic's default `extra="ignore"` silently dropped a mistyped key.

    The note then sat outside every query keyed on the field the author thought they had set.
    """
    path = tmp_path / "compound" / "compound-w.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: compound-w\ntype: compound\nvalid-from: 2026-01-01\n---\nbody\n")
    problems = validate(tmp_path)
    assert any("compound-w" in p and "valid-from" in p for p in problems)


def test_a_malformed_link_target_is_named_as_such(tmp_path: Path) -> None:
    """`[[a:b:c]]` used to surface only as `unknown note 'b:c'`.

    That tells the author the wrong thing — the id is not missing, the syntax is broken.
    """
    _write(tmp_path, Note(id="compound-z", type="compound", body="[[a:b:c]]"))
    problems = validate(tmp_path)
    assert any("not a valid note id" in p and "b:c" in p for p in problems)


def test_a_corpus_note_in_the_external_namespace_is_not_reported_missing(tmp_path: Path) -> None:
    """`reaction-` is a namespace, not a reservation.

    A real note under that name must not be sent to the record store and reported as an absent
    ELN transcription.
    """
    from chemclaw.kg.validate import external_citations, validate_with_notes

    _write(tmp_path, Note(id="reaction-abc", type="reaction", body="a real note"))
    _write(
        tmp_path,
        Note(id="compound-q", type="compound", body="[[reaction-abc]] and [[reaction-missing]]"),
    )
    assert external_citations(validate_with_notes(tmp_path)[1]) == [
        ("compound-q", "reaction-missing")
    ]


def test_a_surrogate_in_a_nested_conditions_field_is_refused_at_the_note() -> None:
    """A surrogate in a nested conditions field is refused at the note.

    `_text_is_writable` walks nested strings, so `conditions.major_impurity` cannot fail later at
    commit with `UnicodeEncodeError`.
    """
    from chemclaw.kg.note import ProcessConditions

    with pytest.raises(ValidationError, match="conditions.major_impurity"):
        Note(
            id="rxn-s",
            type="reaction",
            conditions=ProcessConditions(major_impurity="bad\ud800"),
        )


def test_a_calc_ref_the_store_never_produced_is_reported() -> None:
    """A `calc_ref` the store never produced is reported.

    `_calc_ref_shape` checks only a key's form; existence needs the store, which is a parameter
    (`CalculationExistence`), so no patching is needed.
    """
    import asyncio

    from chemclaw.kg.validate import calc_citations, unresolved_calc_refs
    from chemclaw.science.calc.store import (
        CalculationKey,
        InMemoryStore,
        StoredResult,
    )

    async def _run() -> list[str]:
        store = InMemoryStore()
        real = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
        await store.put(StoredResult(key=real, result={"energy": -1.0}, provenance="computed"))
        typo = real.as_str()[:-1] + ("0" if not real.as_str().endswith("0") else "1")
        note = Note(
            id="job-result-x",
            type="job-result",
            created_by="agent",
            calc_refs=[real.as_str(), typo],
            body="two refs, one real",
        )
        citations = calc_citations([note])
        assert citations == [("job-result-x", real.as_str()), ("job-result-x", typo)] or (
            citations == [("job-result-x", typo), ("job-result-x", real.as_str())]
        )
        return await unresolved_calc_refs(citations, store)

    problems = asyncio.run(_run())
    assert len(problems) == 1
    assert "job-result-x" in problems[0] and "calc_refs" in problems[0]


def test_every_relation_this_graph_declares_has_a_note_type_that_can_be_its_target() -> None:
    """Every relation whose wording names a kind of thing has a note type that can be its target.

    `measured-by` says "an experimental method or instrument", so a note type for one must exist, or
    authors point it at the nearest wrong thing. Asserted as a mapping, since most relations are
    note-to-note and need no special type.
    """
    from chemclaw.kg.relations import KNOWN_RELATIONS

    needs_a_kind = {"measured-by": "analytical-method"}
    for relation, target_type in needs_a_kind.items():
        assert relation in KNOWN_RELATIONS, (
            f"{relation!r} is no longer a declared relation; if it was removed, remove its row here"
        )
        assert target_type in known_note_types(), (
            f"{relation!r} says it points at an experimental method or instrument, and "
            f"{target_type!r} is not a note type this deployment can write — so the edge has no "
            "legal target and the only way to use it is to point at something it is not"
        )
