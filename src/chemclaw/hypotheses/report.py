"""Turning a finished tournament into something a chemist reads, and into a proposal note.

The table never asserts an ordering it cannot support: when the leader is not decisive over the
runner-up the summary says the field is unseparated. It still names one next check — the one that
best separates the unseparated candidates — and the table follows as the argument for it.

The proposal body follows `skills/experiment-progression` §5's order: proposal, rationale,
falsifiable expectation, fallback, what it will not tell you.
"""

from __future__ import annotations

from collections.abc import Collection

from chemclaw.hypotheses.models import RankedHypothesis, TournamentOutcome
from chemclaw.kg.note import as_cell, is_note_slug

#: Rows the prose summary renders in full; the rest of the field is still in the envelope's `data`
#: and in the `hypothesis-field` note. Bounds the tool result, which shares
#: `agent_max_tool_result_chars` with the structured outcome and is cut from the middle if over.
_SUMMARY_ROWS = 5


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
        # The verb comes from what happened (`outcome.verdict`), never from the check's kind, so a
        # check that did not run is never announced as run.
        ran = leader.outcome is not None and leader.outcome.verdict != "not-run"
        verb = "ran" if ran else "to run"
        lines.append(
            f"**Next: {as_cell(leader.check.question)}** "
            f"({verb}; {as_cell(leader.check.expectation)})"
        )
    lines.append("")

    if outcome.leader_is_decisive:
        lines.append(f"The field separates: **{as_cell(leader.hypothesis.statement)}** leads.")
    else:
        lines.append(
            "**The field does not separate.** The leading hypotheses sit inside their own "
            "uncertainty, so the order below is not yet evidence — it says which comparisons have "
            "been run, not which explanation is right."
        )
    lines.append("")

    for position, row in enumerate(outcome.ranked[:_SUMMARY_ROWS], start=1):
        lines.append(f"{position}. **{as_cell(row.hypothesis.statement)}** — {_interval(row)}")
        lines.append(f"   - refuted if: {as_cell(row.hypothesis.refuted_if)}")
        if row.hypothesis.mechanism:
            lines.append(f"   - mechanism: {as_cell(row.hypothesis.mechanism)}")
        for objection in row.objections:
            lines.append(
                f"   - objection: {as_cell(objection.concern)} — {as_cell(objection.rationale)}"
            )
        if row.outcome is not None and row.outcome.verdict != "not-run":
            lines.append(
                f"   - check ran: **{row.outcome.verdict}** — {as_cell(row.outcome.detail)}"
            )
            if row.outcome.ran:
                # The call as made, including defaults, so a reader can see the conditions behind
                # the number. Whitespace-collapsed but not `as_cell`ed: the system builds this line
                # and its wikilinks are the grounded compounds; its model-reachable parts are
                # unlinked where it is built.
                lines.append(f"     ran: `{' '.join(row.outcome.ran.split())}`")
        elif row.check is not None and row.check.kind == "physical":
            lines.append(f"   - to settle in the lab: {as_cell(row.check.question)}")
        elif row.check is not None:
            # A computable check that did not run, with the reason.
            reason = as_cell(row.outcome.detail) if row.outcome is not None else ""
            lines.append(
                f"   - answerable with this system's tools, not run: {as_cell(row.check.question)}"
                + (f" — {reason}" if reason else "")
            )

    hidden = len(outcome.ranked) - _SUMMARY_ROWS
    if hidden > 0:
        lines.append(
            f"\n_{hidden} further hypothesis(es) were ranked below these; the full field is in "
            "this job's result and in its `hypothesis-field` note._"
        )

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


def proposal_body(row: RankedHypothesis, *, question: str, retrieved: Collection[str]) -> str:
    """An `experiment-proposal` note body for a hypothesis whose check needs a laboratory.

    Written in `skills/experiment-progression` §5's order and carries the rating with its interval.
    `retrieved` is every note id the evidence sweeps returned; only a cited id inside it is rendered
    as a wikilink.
    """
    if row.check is None:  # pragma: no cover - callers filter on `check` first
        raise ValueError(f"{row.hypothesis.id} has no discriminating check to propose")

    # Every span below is model-authored, so each is placed as a cell: it may fill a bullet, never
    # add one or mint a citation. A cited id becomes a wikilink only if a sweep returned it
    # (`retrieved`) and it is a valid note slug (`is_note_slug`), so a crafted id cannot close the
    # link and forge a bullet.
    citations = " ".join(
        f"[[{note_id}]]"
        for note_id in row.hypothesis.cited_note_ids
        if note_id in retrieved and is_note_slug(note_id)
    )
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

    Records the alternatives that came second, which the per-hypothesis proposal notes lose. Ratings
    carry their intervals, and the note states what they are not.
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
