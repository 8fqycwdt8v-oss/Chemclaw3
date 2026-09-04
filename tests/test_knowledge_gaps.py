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

from collections import Counter
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


def test_silence_is_not_a_successful_run() -> None:
    """A source that said nothing about how a run turned out has not said it worked.

    This asserted the opposite until `D-2026-08-26-silence-is-not-a-successful-run`, on the
    compatibility argument that silence had always meant an ordinary run. That argument covered a
    status column that happened to be null; it did not cover a source with no status column at all,
    where the default made every record in the corpus assert a success nobody claimed — silently,
    on the one field whose purpose is that a failure must not read as an ordinary run.

    `None` and not `INCONCLUSIVE`: that value means the run carries no evidence about the chemistry,
    which is a statement somebody made. "Nobody has read the prose yet" is a different fact.
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
    """The documented example weight starved a whole retrieval leg to zero surviving chunks.

    RRF's rank term is nearly flat at `k=60` — rank 1 scores 0.01639 and rank 30 scores 0.01111, a
    ratio of 1.48 across thirty positions — so a *multiplicative* weight does not nudge a tier, it
    replaces rank with source: at `w = 1.5` a graph hit outranks every other source's best hit for
    all its own ranks below 31 (`graph rank30 * 1.5 = 0.01667` vs `lexical rank1 = 0.01639`).

    Measured on a 40-chunk sweep of four sources, with the weights the ENV comment in
    `core/config/retrieval.py` gives as its example:

        weights=None                            -> graph 15 / lexical 8 / share 10 / vector 7
        weights={'graph': 1.5, 'vector': 0.8}   -> graph 34 / lexical  3 / share  3 / vector 0

    That is literally the "one retrieval leg contributed *zero* chunks" outcome
    `D-2026-08-01-a-cap-that-starves-a-source` records as the merge design's reason to exist, and
    nothing reports it: the per-source counters show the vector branch returning its seven chunks,
    and they all die in the fusion. The invariant pinned here is the general one, not the example —
    for any weights, every source's rank-1 hit survives a cap of 40 over four sources.
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


def test_a_non_positive_source_weight_is_refused_by_the_config() -> None:
    """A weight divides the rank, so zero is a division by zero and a negative one inverts a source.

    Both were meaningless under the multiplier this replaced too — `0` deleted a source from every
    sweep silently, which is the starvation the knob exists to prevent — so refusing them is the
    config stating what the arithmetic always required.
    """
    from chemclaw.core.config.retrieval import RetrievalSettings

    with pytest.raises(ValidationError, match="must be positive"):
        RetrievalSettings(retrieval_source_weights={"graph": 0.0})
    assert RetrievalSettings(retrieval_source_weights={"graph": 1.5}).retrieval_source_weights


def test_a_non_finite_source_weight_is_refused_by_the_config() -> None:
    """The three the positivity check cannot see, because ordering is what it rests on.

    `nan <= 0` is False, so NaN walked straight through the guard that refuses `-1` — and a NaN
    weight makes `1.0 / (k + rank / weight)` NaN for every hit the weighted source contributed,
    which propagates into the fused score of every note it touched. `sorted` then compares those
    keys, every comparison is False, and the ranking degenerates to the order the dicts happened to
    be built in: the fusion still returns a list, of the right length, in an order that is not a
    ranking. The same defect `find_matches` closed for a NaN Tanimoto threshold — refused there
    too, and for the same reason: a value with no nearest bound cannot be clamped toward one.

    `+inf` is refused on the guard's own stated grounds rather than by association. It survives
    `weight <= 0`, and `rank / inf` is `0.0` for *every* rank, so every hit in that source fuses at
    `1/k` — its best and its worst hit score identically. That is precisely "names no ordering at
    all", which is what the refusal message says a rejected weight is.
    """
    from chemclaw.core.config.retrieval import RetrievalSettings

    for weight in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="finite|positive"):
            RetrievalSettings(retrieval_source_weights={"graph": weight})


# --- STO-8, second half: relation direction and note-shape hardening -------------------------
#
# The shipped corpus once held twelve edges written backwards against the vocabulary's declared
# directions, all green under a gate that checked relation *names* only, with a seed-corpus test
# pinning the inversion as correct (`docs/archive/REVIEW-2026-08-27-knowledge-system-analysis.md`
# §1). These tests are the checks that would have refused it.


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
    """`_text_is_writable` once walked only top-level strings.

    A surrogate in `conditions.major_impurity` therefore built a Note that raised
    `UnicodeEncodeError` in the PR-gate's commit — the exact late failure the validator exists
    to prevent.
    """
    from chemclaw.kg.note import ProcessConditions

    with pytest.raises(ValidationError, match="conditions.major_impurity"):
        Note(
            id="rxn-s",
            type="reaction",
            conditions=ProcessConditions(major_impurity="bad\ud800"),
        )


def test_a_calc_ref_the_store_never_produced_is_reported() -> None:
    """The calc half of the citation-existence gate (2026-08-27 review §5).

    `_calc_ref_shape` validates a key's *form* and its own comment concedes that existence "is a
    question only a database can answer" — and nothing asked it, so a transposed digit merged
    silently and `calc_ref_index` indexed a key no calculation ever produced. The store is a
    parameter (`CalculationExistence`), so this needs no patching, exactly like the reaction check.
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
