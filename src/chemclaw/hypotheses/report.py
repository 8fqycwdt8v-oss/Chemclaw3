"""Turning a finished tournament into something a chemist reads, and into a proposal note.

**The table never asserts an ordering it cannot support.** `rating.Separation.decisive` is consulted
for the top pair, and when the leader is inside its own uncertainty the summary says the field is
unseparated rather than naming a winner. That is the same discipline `science/bo/engine.py` applies
to a multi-objective problem — "do **not** announce a single 'best' point … because there is not
one" — and the failure mode it avoids is identical: a number rendered without its interval gets
acted on as though it had none.

**And it still names one experiment.** `skills/experiment-progression/SKILL.md` §5 is emphatic that
"a list of five is a way of avoiding the question", and it is right about the thing it is
protecting:
a technician runs one experiment tomorrow. The ranking is not a substitute for that answer, it is
the reasoning behind it made checkable — so the summary leads with the single next check and the
table follows as the argument for it. Where the leader is not decisive, the recommended check is the
one that best separates the *unseparated* candidates, which is the honest answer to "what should I
run" when the ranking has not settled.

The proposal body follows §5's required order exactly — proposal, rationale, falsifiable
expectation, fallback, what it will not tell you — because that shape is already what a chemist
reading `knowledge/experiment-proposal/` expects, and a second shape would be a second convention.
"""

from __future__ import annotations

from chemclaw.hypotheses.models import RankedHypothesis, TournamentOutcome
from chemclaw.kg.note import as_cell


def _interval(row: RankedHypothesis) -> str:
    """A rating with its uncertainty and its evidence count, never a bare number."""
    if row.comparisons == 0:
        return "unrated (never compared)"
    return f"{row.rating:.0f} ± {row.standard_error:.0f} over {row.comparisons} comparison(s)"


def summarise(outcome: TournamentOutcome) -> str:
    """The chemist-facing summary: the next check first, then the ranked field and its caveats."""
    if not outcome.ranked:
        rejected = len(outcome.rejected)
        if rejected:
            return (
                f"No hypothesis survived screening for {outcome.question!r}: "
                f"{rejected} candidate(s) were rejected for having no usable refutation condition."
            )
        return f"No hypotheses were generated for {outcome.question!r}."

    leader = outcome.ranked[0]
    lines: list[str] = []

    if leader.check is not None:
        verb = "Ran" if leader.check.kind == "computable" else "Proposed"
        lines.append(
            f"**Next: {leader.check.question}** ({verb.lower()}; {leader.check.expectation})"
        )
    lines.append("")

    if outcome.leader_is_decisive:
        lines.append(f"The field separates: **{leader.hypothesis.statement}** leads.")
    else:
        lines.append(
            "**The field does not separate.** The leading hypotheses sit inside their own "
            "uncertainty, so the order below is not yet evidence — it says which comparisons have "
            "been run, not which explanation is right."
        )
    lines.append("")

    for position, row in enumerate(outcome.ranked, start=1):
        lines.append(f"{position}. **{row.hypothesis.statement}** — {_interval(row)}")
        lines.append(f"   - refuted if: {row.hypothesis.refuted_if}")
        if row.hypothesis.mechanism:
            lines.append(f"   - mechanism: {row.hypothesis.mechanism}")
        for objection in row.objections:
            lines.append(f"   - objection: {objection.concern} — {objection.rationale}")
        if row.outcome is not None and row.outcome.verdict != "not-run":
            lines.append(f"   - check ran: **{row.outcome.verdict}** — {row.outcome.detail}")
        elif row.check is not None and row.check.kind == "physical":
            lines.append(f"   - to settle in the lab: {row.check.question}")

    caveats: list[str] = []
    if outcome.rejected:
        caveats.append(
            f"{len(outcome.rejected)} candidate(s) rejected for having no usable refutation "
            "condition"
        )
    if outcome.merged:
        caveats.append(f"{len(outcome.merged)} merged as duplicates")
    if outcome.position_bias is not None:
        caveats.append(
            f"position bias measured at {outcome.position_bias:.0%} of double-judged pairs"
        )
    if caveats:
        lines.extend(["", f"_{outcome.comparisons_run} comparisons; " + "; ".join(caveats) + "._"])
    else:
        lines.extend(["", f"_{outcome.comparisons_run} comparisons._"])

    return "\n".join(lines)


def proposal_body(row: RankedHypothesis, *, question: str) -> str:
    """An `experiment-proposal` note body for a hypothesis whose check needs a laboratory.

    Written in `skills/experiment-progression` §5's order so it reads like every other proposal in
    `knowledge/experiment-proposal/`, and carries the rating *with its interval* so a reader cannot
    mistake a tournament placing for a measurement.
    """
    if row.check is None:  # pragma: no cover - callers filter on `check` first
        raise ValueError(f"{row.hypothesis.id} has no discriminating check to propose")

    # Every span below is model-authored, so each is placed as a cell: it may fill a bullet, never
    # add one, and never mint a citation. `cited_note_ids` is the one channel allowed to produce a
    # wikilink, because those ids came from a retriever rather than from the model's prose.
    citations = " ".join(f"[[{note_id}]]" for note_id in row.hypothesis.cited_note_ids)
    mechanism = as_cell(row.hypothesis.mechanism)
    objections = (
        "\n".join(f"  - {as_cell(o.concern)} — {as_cell(o.rationale)}" for o in row.objections)
        if row.objections
        else "  - none recorded"
    )
    return "\n".join(
        [
            f"Proposed to discriminate a hypothesis raised while answering: {as_cell(question)}",
            "",
            f"- **run**: {as_cell(row.check.question)}",
            f"- **rationale**: tests {as_cell(row.hypothesis.statement)!r}"
            + (f" ({mechanism})" if mechanism else "")
            + (f". Evidence: {citations}" if citations else "."),
            f"- **expectation**: {as_cell(row.check.expectation)}",
            f"- **refutes the hypothesis if**: {as_cell(row.hypothesis.refuted_if)}",
            "- **objections raised against this hypothesis**:",
            objections,
            "",
            f"Ranked {_interval(row)} against the other explanations considered. That is a "
            "preference ordering over the candidates generated, not a probability that this "
            "hypothesis is correct, and it is not evidence about the chemistry — the "
            "experiment is.",
        ]
    )


def field_body(outcome: TournamentOutcome) -> str:
    """A `hypothesis-field` note body: the whole field and how it placed.

    **Worth writing down because of what it holds that nothing else does — the alternatives.** The
    `experiment-proposal` notes this tournament also writes each record one chosen next step;
    between them they lose the thing that makes a ranking useful later, which is what was
    considered and came second. A chemist returning to a stalled series wants the rejected branches,
    and `failure-mode` notes exist in this graph for the same reason: "A run recorded with
    `outcome: failure` and its reason has usually eliminated more of the space than a mediocre
    success did."

    The ratings are carried with their intervals, and the note says in as many words what they are
    not, because a bare number in a durable record outlives the conversation that could explain it.
    """
    lines = [
        f"Competing explanations generated and ranked for: {as_cell(outcome.question)}",
        "",
        summarise(outcome),
        "",
        "---",
        "",
        "_Ratings are on the Elo scale and come from pairwise judgements of these hypotheses "
        "against each other, seeded with retrieved evidence. A rating is a preference ordering "
        "over this field — not a probability that a hypothesis is true, and not evidence about the "
        "chemistry. Only the experiments settle that._",
    ]
    if outcome.proposal_note_ids:
        lines.extend(
            [
                "",
                "Proposed experiments: "
                + " ".join(f"[[{note_id}]]" for note_id in outcome.proposal_note_ids),
            ]
        )
    return "\n".join(lines)
