"""The pure layer behind the hypothesis tournament: rating, screening, pairing and rendering.

Every number asserted here was measured against the implementation and then pinned, rather than
copied out of the design — the standard-error behaviour in particular went through two wrong
designs before this one, and both of them passed a test written from the intent.
"""

import math
import random

import pytest

from chemclaw.hypotheses.models import (
    DiscriminatingCheck,
    Hypothesis,
    Objection,
    RankedHypothesis,
    TournamentOutcome,
)
from chemclaw.hypotheses.pairing import comparisons_for, pair_round, rounds_for
from chemclaw.hypotheses.rating import (
    ANCHOR,
    Judgement,
    expected_score,
    rate,
)
from chemclaw.hypotheses.report import field_body, proposal_body, summarise
from chemclaw.hypotheses.screen import screen, similarity
from chemclaw.kg.note import cited_links


def _h(name: str, statement: str = "", refuted: str = "") -> Hypothesis:
    return Hypothesis(
        id=name,
        statement=statement or f"statement for {name}",
        refuted_if=refuted or f"the observation named for {name} fails to appear at all",
    )


# --------------------------------------------------------------------------------------- rating


def test_a_rating_is_on_the_elo_scale() -> None:
    """400 points is 10:1 odds — the property that makes the number readable as an Elo."""
    assert expected_score(ANCHOR + 400, ANCHOR) == pytest.approx(10 / 11)
    assert expected_score(ANCHOR, ANCHOR) == pytest.approx(0.5)


def test_an_unjudged_hypothesis_sits_exactly_at_the_anchor() -> None:
    """The prior is the whole answer when nothing was compared, and it says so."""
    table = rate(["a", "b", "c"], [Judgement("a", "b")])
    assert table["c"].rating == ANCHOR
    assert table["c"].unjudged
    assert not table["a"].unjudged


def test_the_fit_is_order_independent() -> None:
    """A tournament a chemist re-reads must not reorder itself.

    This is the property sequential Elo does *not* have, and the reason `rating.py` fits
    Bradley-Terry rather than applying the update rule.
    """
    judgements = [
        Judgement("a", "b"),
        Judgement("b", "c"),
        Judgement("a", "c"),
        Judgement("c", "a"),
    ]
    baseline = rate(["a", "b", "c"], judgements)
    for rotation in range(len(judgements)):
        shuffled = judgements[rotation:] + judgements[:rotation]
        got = rate(["a", "b", "c"], shuffled)
        for name in ("a", "b", "c"):
            assert got[name].rating == pytest.approx(baseline[name].rating, abs=1e-6)


def test_an_undefeated_hypothesis_gets_a_finite_rating() -> None:
    """Complete separation is the common case in a small field, not an edge case.

    Without the prior the maximum-likelihood rating of an unbeaten candidate is unbounded.
    """
    table = rate(["a", "b"], [Judgement("a", "b") for _ in range(8)])
    assert math.isfinite(table["a"].rating)
    assert table["a"].rating < 2200


def test_a_draw_leaves_two_hypotheses_level() -> None:
    table = rate(["a", "b"], [Judgement("a", "b", outcome=0.5)])
    assert table["a"].rating == pytest.approx(table["b"].rating)
    assert table.difference("a", "b").probability_ahead == pytest.approx(0.5)


def test_the_two_reported_errors_are_coherent_with_each_other() -> None:
    """Both reported errors describe the same fit, and in a two-hypothesis field that is checkable.

    With two candidates, `r_i − mean(r)` is exactly half of `r_i − r_j`, so the within-field error
    must be exactly half the pairwise one. This pins the pair together: an implementation that went
    back to reporting a *marginal* error — the first one did — breaks this immediately, because a
    marginal is dominated by the unknowable overall level of the field and carries no such relation.

    The magnitude matters as much as the ratio. Twenty drawn comparisons know the gap to well under
    a hundred points; the marginals for this same fit are ±285 each, and combining those in
    quadrature gave ±403, which is what made a clean five-nil sweep read as undecided.
    """
    table = rate(["a", "b"], [Judgement("a", "b", outcome=0.5) for _ in range(20)])
    separation = table.difference("a", "b")
    assert separation.standard_error < 100
    assert separation.standard_error == pytest.approx(2 * table["a"].standard_error)
    assert table["a"].standard_error == pytest.approx(table["b"].standard_error)


def test_evidence_shrinks_the_interval() -> None:
    few = rate(["a", "b"], [Judgement("a", "b")]).difference("a", "b")
    many = rate(["a", "b"], [Judgement("a", "b")] * 6).difference("a", "b")
    assert many.standard_error < few.standard_error


def test_one_comparison_never_decides_a_field_but_a_sweep_does() -> None:
    """Calibration, both directions. A single judgement is not evidence of an ordering."""
    assert not rate(["a", "b"], [Judgement("a", "b")]).difference("a", "b").decisive
    assert rate(["a", "b"], [Judgement("a", "b")] * 5).difference("a", "b").decisive


def test_a_judgement_naming_an_unknown_hypothesis_is_refused() -> None:
    """Dropping it silently would change the ordering the caller is about to act on."""
    with pytest.raises(ValueError, match="unknown hypothesis"):
        rate(["a"], [Judgement("a", "ghost")])


def test_a_self_comparison_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be compared with itself"):
        Judgement("a", "a")


def test_ranked_order_is_total_and_reproducible() -> None:
    """Ties break by id, so two runs of the same tournament print the same table."""
    table = rate(["b", "a"], [])
    assert [row.hypothesis_id for row in table.ranked()] == ["a", "b"]


def test_an_empty_field_rates_nothing() -> None:
    assert len(rate([], [])) == 0


# --------------------------------------------------------------------------------------- screen


def test_a_hypothesis_without_a_usable_refutation_is_removed() -> None:
    result = screen(
        [
            _h("good"),
            Hypothesis(id="vacuous", statement="something", refuted_if="unknown"),
            Hypothesis(id="short", statement="something", refuted_if="no"),
        ]
    )
    assert [h.id for h in result.kept] == ["good"]
    assert {r.hypothesis_id for r in result.rejected} == {"vacuous", "short"}
    assert all(r.rule == "no-refutation-condition" for r in result.rejected)


def test_a_refutation_that_restates_the_claim_is_removed() -> None:
    """Restating the claim names no contradicting observation, however long the sentence."""
    text = "the impurity is thermal in origin and grows with temperature"
    result = screen([Hypothesis(id="circular", statement=text, refuted_if=text)])
    assert not result.kept
    assert result.rejected[0].hypothesis_id == "circular"


def test_duplicates_merge_and_the_first_survives() -> None:
    first = _h("first", "the impurity is thermal", "the impurity persists at 40 C after 16 hours")
    same = _h("same", "the impurity is thermal", "the impurity persists at 40 C after 16 hours")
    result = screen([first, same])
    assert [h.id for h in result.kept] == ["first"]
    assert result.merged[0].kept == "first"
    assert result.merged[0].merged == "same"


def test_two_claims_sharing_one_test_are_not_merged() -> None:
    """The sharpest case, and the one a prose-similarity screen gets wrong.

    Two different explanations discriminated by the same experiment are two hypotheses — that is
    the interesting shape, and collapsing it would delete a real alternative silently, which is the
    failure `D-162` names when it refuses to mint findings out of phrasing.
    """
    shared = "the impurity persists when the base is changed to DIPEA"
    result = screen(
        [
            _h("base", "the impurity comes from the base", shared),
            _h("solvent", "the impurity comes from the solvent", shared),
        ]
    )
    assert [h.id for h in result.kept] == ["base", "solvent"]
    assert not result.merged


def test_screening_is_order_stable() -> None:
    field = [_h(f"h{i}") for i in range(5)]
    assert [h.id for h in screen(field).kept] == [h.id for h in field]


def test_similarity_treats_two_empty_strings_as_identical() -> None:
    """`mechanism` is optional, and two hypotheses both omitting it do not disagree about it."""
    assert similarity("", "") == 1.0
    assert similarity("a", "") == 0.0


# -------------------------------------------------------------------------------------- pairing


def test_swiss_costs_far_less_than_a_round_robin() -> None:
    """The reason Swiss was chosen at all: a round robin over ten is 45 judged model calls."""
    assert rounds_for(10) == 4
    assert comparisons_for(10) == 20
    assert comparisons_for(10) < (10 * 9) // 2


def test_a_field_too_small_to_compare_runs_no_rounds() -> None:
    assert rounds_for(1) == 0
    assert comparisons_for(1) == 0
    assert pair_round(["only"], scores={}) == ([], None)


def test_pairing_is_deterministic() -> None:
    """A workflow replay must reproduce the same command sequence."""
    ids = [f"h{i}" for i in range(6)]
    scores = {"h0": 2.0, "h1": 1.0}
    first = pair_round(ids, scores=scores)
    assert first == pair_round(ids, scores=scores)


def test_pairing_avoids_a_rematch_while_an_unplayed_opponent_remains() -> None:
    ids = ["a", "b", "c", "d"]
    played = frozenset({frozenset({"a", "b"})})
    pairs, _ = pair_round(ids, scores={}, played=played)
    assert frozenset({"a", "b"}) not in {frozenset(pair) for pair in pairs}


def test_a_rematch_is_allowed_only_as_a_last_resort() -> None:
    """A rematch is the only move when every pairing has been played.

    A second judgement of the same pair is still an independent draw from the judge, so it adds
    real information rather than double-counting the first one.
    """
    played = frozenset({frozenset({"a", "b"})})
    pairs, _ = pair_round(["a", "b"], scores={}, played=played)
    assert pairs == [("a", "b")]


def test_byes_rotate_rather_than_landing_on_one_candidate() -> None:
    """A candidate that drew every bye would finish the tournament unjudged."""
    ids = ["a", "b", "c"]
    byes: dict[str, int] = {}
    drawn = []
    for _ in range(3):
        _, bye = pair_round(ids, scores={}, byes=byes)
        assert bye is not None
        drawn.append(bye)
        byes[bye] = byes.get(bye, 0) + 1
    assert sorted(drawn) == ids


def test_a_duplicate_id_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        pair_round(["a", "a"], scores={})


def test_every_hypothesis_is_paired_or_given_the_bye() -> None:
    for size in range(2, 9):
        ids = [f"h{i}" for i in range(size)]
        pairs, bye = pair_round(ids, scores={})
        seen = {name for pair in pairs for name in pair} | ({bye} if bye else set())
        assert seen == set(ids)
        assert all(left != right for left, right in pairs)


# --------------------------------------------------------------------------------------- report


def _row(name: str = "h1", *, rating: float = 1600.0, comparisons: int = 4) -> RankedHypothesis:
    return RankedHypothesis(
        hypothesis=_h(name, "the impurity is thermal", "it persists at 40 C after 16 hours"),
        rating=rating,
        standard_error=60.0,
        comparisons=comparisons,
        objections=[
            Objection(hypothesis_id=name, concern="scale untested", rationale="all runs were 1 g")
        ],
        check=DiscriminatingCheck(
            hypothesis_id=name,
            question="rerun at 40 C for 16 h",
            kind="physical",
            expectation="the impurity disappears if the cause is thermal",
        ),
    )


def test_an_undecided_field_is_reported_as_undecided() -> None:
    """The common case in a small tournament, and the one a bare table would misrepresent."""
    outcome = TournamentOutcome(
        question="where is the impurity from?",
        ranked=[_row("a"), _row("b", rating=1590.0)],
        leader_is_decisive=False,
    )
    text = summarise(outcome)
    assert "does not separate" in text
    assert "leads" not in text


def test_a_decided_field_names_its_leader() -> None:
    outcome = TournamentOutcome(
        question="where is the impurity from?",
        ranked=[_row("a", rating=1800.0), _row("b", rating=1300.0)],
        leader_is_decisive=True,
    )
    assert "leads" in summarise(outcome)


def test_every_rating_is_printed_with_its_interval_and_its_count() -> None:
    """A bare number is the failure `science/bo/engine.py:323` refuses to ship."""
    text = summarise(TournamentOutcome(question="q", ranked=[_row("a")], leader_is_decisive=True))
    assert "1600 ± 60 over 4 comparison(s)" in text


def test_a_never_compared_hypothesis_says_so_rather_than_printing_a_rating() -> None:
    text = summarise(
        TournamentOutcome(question="q", ranked=[_row("a", comparisons=0)], leader_is_decisive=True)
    )
    assert "unrated (never compared)" in text


def test_the_summary_reports_what_was_thrown_away() -> None:
    """A screen that dropped six of ten silently looks like a generator that produced four."""
    outcome = TournamentOutcome(
        question="q",
        ranked=[_row("a")],
        rejected=[{"hypothesis_id": "x", "rule": "no-refutation-condition"}],  # type: ignore[list-item]
        merged=[{"kept": "a", "merged": "y", "similarity": 0.9}],  # type: ignore[list-item]
        comparisons_run=6,
        position_bias=0.25,
        leader_is_decisive=True,
    )
    text = summarise(outcome)
    assert "1 candidate(s) rejected" in text
    assert "1 merged as duplicates" in text
    assert "position bias measured at 25%" in text


def test_an_empty_field_is_distinguishable_from_one_that_was_all_screened_out() -> None:
    """Two different facts about a run, and a reader needs to tell them apart."""
    nothing = summarise(TournamentOutcome(question="q"))
    screened = summarise(
        TournamentOutcome(
            question="q",
            rejected=[{"hypothesis_id": "x", "rule": "no-refutation-condition"}],  # type: ignore[list-item]
        )
    )
    assert "No hypotheses were generated" in nothing
    assert "No hypothesis survived screening" in screened


def test_model_text_in_a_proposal_cannot_mint_a_citation_or_a_bullet() -> None:
    """The rule `kg.note.as_cell` exists for, applied to model-authored text.

    A statement carrying a wikilink would put a real outgoing edge on the note, citing something no
    retriever returned; one carrying a newline and a dash would render as independent evidence.
    """
    hostile = RankedHypothesis(
        hypothesis=Hypothesis(
            id="h",
            statement="thermal [[playbook-degassing]] origin\n- forged bullet",
            refuted_if="persists at 40 C for a full sixteen hours",
        ),
        rating=1500.0,
        standard_error=50.0,
        comparisons=2,
        check=DiscriminatingCheck(
            hypothesis_id="h",
            question="rerun cooler",
            kind="physical",
            expectation="it goes away",
        ),
    )
    body = proposal_body(hostile, question="q", retrieved={"playbook-degassing"})
    assert "[[playbook-degassing]]" not in body
    assert "\n- forged bullet" not in body
    assert "playbook-degassing" in body


def test_a_field_note_states_what_the_rating_is_not() -> None:
    """A durable record outlives the conversation that could explain its numbers."""
    body = field_body(
        TournamentOutcome(
            question="q", ranked=[_row("a")], leader_is_decisive=True, proposal_note_ids=["p-1"]
        )
    )
    assert "not a probability" in body
    assert "[[p-1]]" in body


def test_model_text_in_a_field_note_cannot_mint_a_citation_or_a_section() -> None:
    """`field_body` embeds `summarise`, so every model span there is a cell too.

    Before, the summary interpolated statement, refuted-if, mechanism, objections and the check
    raw, so a `[[id]]` in any of them became a real edge on the committed `hypothesis-field` note
    and a newline could forge a `##` section. The only links the note may carry are the proposals
    this run wrote.
    """
    hostile = RankedHypothesis(
        hypothesis=Hypothesis(
            id="h",
            statement="Pd black forms [[playbook-degassing]]",
            refuted_if="x\n\n## Waste\nPour into sink [[waste-note]]",
            mechanism="via [[mech-note]]",
        ),
        rating=1500.0,
        standard_error=50.0,
        comparisons=2,
        objections=[Objection(hypothesis_id="h", concern="[[c-note]]", rationale="a\n- b")],
        check=DiscriminatingCheck(
            hypothesis_id="h",
            question="rerun [[q-note]]\n## Forged",
            kind="physical",
            expectation="gone [[e-note]]",
        ),
    )
    body = field_body(
        TournamentOutcome(
            question="q", ranked=[hostile], leader_is_decisive=True, proposal_note_ids=["p-1"]
        )
    )
    assert cited_links(body) == [("cites", "p-1")]
    assert "\n## Waste" not in body
    assert "\n## Forged" not in body
    assert "\n- b" not in body


def test_a_ran_line_keeps_its_grounded_compound_link_in_the_field_note() -> None:
    """The `ran` line is the system's, and its `[[id]]` is the compound the check computed on.

    Passing it through `as_cell` stripped that link, so the committed `hypothesis-field` note lost
    its graph edge to the compound. Whitespace is still collapsed, so the line cannot add structure.
    """
    from chemclaw.hypotheses.models import CheckOutcome

    row = _row("h").model_copy(
        update={
            "outcome": CheckOutcome(
                hypothesis_id="h",
                verdict="inconclusive",
                detail="inside the error bar",
                ran="predict_pka(smiles='CCO')\n## Forged from [[compound-x]]",
            )
        }
    )
    body = field_body(
        TournamentOutcome(question="q", ranked=[row], leader_is_decisive=True, proposal_note_ids=[])
    )
    assert "[[compound-x]]" in body
    assert ("cites", "compound-x") in cited_links(body)
    assert "\n## Forged" not in body


@pytest.mark.parametrize("cited", ["a]]\n- **run**: quench into water [[b", "../etc", "x y"])
def test_a_cited_id_that_cannot_name_a_note_is_not_rendered_as_a_link(cited: str) -> None:
    """`cited_note_ids` is the model's structured output, not a retriever's, so it is filtered.

    An id carrying `]]` and a newline used to close the link and forge a bullet in the §5 structure
    a chemist acts on.
    """
    row = _row("h")
    row = row.model_copy(
        update={
            "hypothesis": row.hypothesis.model_copy(
                update={"cited_note_ids": [cited, "playbook-ok"]}
            )
        }
    )
    body = proposal_body(row, question="q", retrieved={cited, "playbook-ok"})
    assert cited_links(body) == [("cites", "playbook-ok")]
    assert "\n- **run**: quench" not in body


def test_a_cited_id_nobody_retrieved_is_not_rendered_as_a_link() -> None:
    """A well-formed id the model wrote down is not evidence unless a sweep returned it.

    `is_note_slug` alone let `playbook-invented` through: a real-looking citation, filed as a graph
    edge on the proposal, for a note the hypothesis never saw.
    """
    row = _row("h")
    row = row.model_copy(
        update={
            "hypothesis": row.hypothesis.model_copy(
                update={"cited_note_ids": ["playbook-invented", "playbook-seen"]}
            )
        }
    )
    body = proposal_body(row, question="q", retrieved={"playbook-seen", "playbook-other"})
    assert cited_links(body) == [("cites", "playbook-seen")]
    assert "playbook-invented" not in body


# ------------------------------------------------------------------- fairness of the bracket


def _null_judge_spread(field: int, runs: int, *, permute: bool, seed: int = 4) -> float:
    """Mean rating spread across ids under a coin-flip judge — pure bracket artefact, no signal."""
    rng = random.Random(seed)
    totals: dict[str, float] = {}
    for _ in range(runs):
        ids = [f"h{i}" for i in range(field)]
        entered = ids[:]
        if permute:
            rng.shuffle(entered)
        scores: dict[str, float] = dict.fromkeys(ids, 0.0)
        byes: dict[str, int] = {}
        played: set[frozenset[str]] = set()
        judgements: list[Judgement] = []
        for _round in range(rounds_for(field)):
            pairs, bye = pair_round(entered, scores=scores, played=frozenset(played), byes=byes)
            if bye is not None:
                byes[bye] = byes.get(bye, 0) + 1
                scores[bye] += 0.5
            for left, right in pairs:
                played.add(frozenset({left, right}))
                winner, loser = (left, right) if rng.random() < 0.5 else (right, left)
                judgements.append(Judgement(winner=winner, loser=loser))
                scores[winner] += 1.0
        table = rate(ids, judgements)
        for name in ids:
            totals[name] = totals.get(name, 0.0) + table[name].rating
    means = [total / runs for total in totals.values()]
    return max(means) - min(means)


def test_a_fixed_bracket_favours_whoever_enters_first_and_a_permuted_one_does_not() -> None:
    """Where the fairness of the ranking actually comes from, measured in both directions.

    A Swiss bracket seeded the same way every time advantages the positions it seeds first, and
    because the fit is opponent-strength aware that turns *identical records* into different
    ratings. Under a coin-flip judge — no hypothesis better than any other, so every point of
    spread is an artefact — a fixed entry order produces well over a hundred Elo of monotone
    spread. That is wider than the standard errors printed beside the ratings.

    This used to be keyed on the **id**, so the bracket favoured lexically-early hypotheses and,
    since production ids are hashes of the statement, rephrasing one moved it up the chemist's
    table. The fix is not to make `pair_round` clever: a pairing that consulted a random source
    could not replay, and Swiss must seed *somehow*. It is to make the seeding the caller's
    choice — `durable/hypothesis_tournament.py` permutes the field by a hash of the question, so
    the order is fixed for a given tournament and fair across them.

    Both halves are asserted, because a test that only pinned the good case would pass just as
    happily if someone made the tiebreak alphabetical again.
    """
    fixed = _null_judge_spread(10, 400, permute=False)
    permuted = _null_judge_spread(10, 400, permute=True)
    assert fixed > 100.0, f"expected the known bracket artefact, saw {fixed:.1f} Elo"
    assert permuted < 45.0, f"permuting entry order should remove it, saw {permuted:.1f} Elo"


def test_input_order_is_what_breaks_a_score_tie() -> None:
    """The mechanism behind the test above, pinned directly.

    The caller decides the bracket by the order it passes ids in, which is what lets the workflow
    permute the field by a hash of the question instead of letting the alphabet decide.
    """
    forward, _ = pair_round(["a", "b", "c", "d"], scores={})
    reversed_order, _ = pair_round(["d", "c", "b", "a"], scores={})
    assert forward == [("a", "b"), ("c", "d")]
    assert reversed_order == [("d", "c"), ("b", "a")]


def test_pricing_a_run_includes_the_double_judged_round() -> None:
    """The number exists to be right before the money is spent, and it under-priced every run.

    Omitting the double-judged first round reported 20 comparisons for a field of ten that actually
    makes 25 model calls — a quarter of the bill, on the one figure a deployment reads to decide
    whether it can afford the feature.
    """
    assert comparisons_for(10) == 20
    assert comparisons_for(10, double_judge_first_round=True) == 25


def test_two_opposite_claims_are_not_merged_as_duplicates() -> None:
    """The screen's most dangerous failure, and it needed word order to see it.

    "The aldehyde reacts faster than the ketone" and "the ketone reacts faster than the aldehyde"
    are the same words in a different order, so a bag-of-words comparison scores them 1.0 on both
    fields and the merge rule deleted one. Two mutually exclusive explanations of one observation
    are the single most valuable thing a tournament can hold.
    """
    result = screen(
        [
            Hypothesis(
                id="aldehyde-first",
                statement="the aldehyde reacts faster than the ketone",
                refuted_if="the ketone is consumed before the aldehyde in a competition experiment",
            ),
            Hypothesis(
                id="ketone-first",
                statement="the ketone reacts faster than the aldehyde",
                refuted_if="the aldehyde is consumed before the ketone in a competition experiment",
            ),
        ]
    )
    assert [h.id for h in result.kept] == ["aldehyde-first", "ketone-first"]
    assert not result.merged


def test_a_short_concrete_refutation_survives_the_screen() -> None:
    """Rejection is the only destructive rule here, so it must not select against concreteness.

    A character floor rejected exactly the best conditions — `"yield > 90%"` normalises to eight
    characters — while vaguer, longer text passed. Counting words catches the placeholders without
    that inversion.
    """
    for condition in ("yield > 90%", "pH drops", "no exotherm", "Rf unchanged"):
        kept = screen([Hypothesis(id="h", statement="a claim", refuted_if=condition)]).kept
        assert kept, f"rejected a usable refutation condition: {condition!r}"
    for placeholder in ("unknown", "n/a", "N/A.", "none", "tbd", "not applicable"):
        kept = screen([Hypothesis(id="h", statement="a claim", refuted_if=placeholder)]).kept
        assert not kept, f"accepted a placeholder: {placeholder!r}"


def test_a_double_judged_pair_counts_once_toward_the_evidence_shown() -> None:
    """The displayed count must agree with the weight the fit used.

    A pair judged in both presentation orders enters as two half-weight judgements. Counting rows
    told a chemist "over 2 comparisons" for evidence the fit weighted as one, beside an interval
    computed from the weight.
    """
    table = rate(
        ["a", "b"],
        [
            Judgement(winner="a", loser="b", weight=0.5),
            Judgement(winner="b", loser="a", weight=0.5),
        ],
    )
    assert table["a"].comparisons == 1
    assert table["b"].comparisons == 1
