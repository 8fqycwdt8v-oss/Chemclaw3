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

from collections.abc import Collection

from chemclaw.hypotheses.models import RankedHypothesis, TournamentOutcome
from chemclaw.kg.note import as_cell, is_note_slug

#: Rows the prose summary renders in full. The rest of the field is still returned — in the result
#: envelope's `data` and in the `hypothesis-field` note — but it is not re-rendered as prose.
#:
#: **This is a bound on the tool result, measured rather than guessed.** The summary and the
#: structured outcome ride in the same `ToolMessage`, so they share `agent_max_tool_result_chars`
#: (60,000). A ten-hypothesis field with verbose content serialises to 32,378 characters of `data`
#: and the summary re-rendered every one of those rows for another 24,837 — 57,215 combined, a
#: factor of 1.05 rather than the 2x an earlier comment claimed by counting `data` alone. Going
#: over does not fail loudly: `agent/tool_result_size.py` cuts from the *middle*, which leaves the
#: JSON unparseable and removes the centre of the ranking.
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
        # **The verb comes from what happened, never from what kind of check it is.** Reading it
        # off `kind == "computable"` printed "(ran; …)" for every computable check — and nothing
        # runs one, so the first line a chemist read announced a calculation that had not happened.
        # That is precisely the failure the whole feature undertakes to avoid, inverted.
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
                # The call as it was made, including what stayed at the tool's default. A number
                # computed in the default solvent answers a different question from one computed
                # in the solvent the hypothesis is about, and only this line can tell them apart.
                # Whitespace-collapsed but **not** `as_cell`ed: the system builds this line and its
                # `[[note-id]]`s are the grounded compounds the check computed on, the edge the
                # field note must keep. Its model-reachable parts are unlinked where it is built.
                lines.append(f"     ran: `{' '.join(row.outcome.ran.split())}`")
        elif row.check is not None and row.check.kind == "physical":
            lines.append(f"   - to settle in the lab: {as_cell(row.check.question)}")
        elif row.check is not None:
            # A computable check that did not run, with the reason. Saying so beats saying nothing:
            # a `computable` check used to fall through every branch, so the chemist saw a
            # hypothesis with no check at all while the reason sat in a Python docstring.
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

    Written in `skills/experiment-progression` §5's order so it reads like every other proposal in
    `knowledge/experiment-proposal/`, and carries the rating *with its interval* so a reader cannot
    mistake a tournament placing for a measurement.

    `retrieved` is every note id the evidence sweeps behind this hypothesis actually returned, and
    only a cited id inside it is rendered as a wikilink.
    """
    if row.check is None:  # pragma: no cover - callers filter on `check` first
        raise ValueError(f"{row.hypothesis.id} has no discriminating check to propose")

    # Every span below is model-authored, so each is placed as a cell: it may fill a bullet, never
    # add one, and never mint a citation. `cited_note_ids` is the one channel allowed to produce a
    # wikilink — and it is model-authored too: the generator's structured output names them. So an
    # id is rendered only if a sweep for this hypothesis returned it (`retrieved`), which is what
    # stops a well-formed id for a note nobody retrieved being filed as its evidence, and only if it
    # could name a note at all (`is_note_slug`), which is what stops `a]]\n- **run**: … [[b` from
    # closing the link and forging a bullet even if a retriever ever returned one.
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
