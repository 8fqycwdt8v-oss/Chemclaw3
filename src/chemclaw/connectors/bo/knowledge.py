"""Map a BO campaign's recommendation to a knowledge-graph note (plan step 1d.5).

A finished campaign's best point is the experiment the optimizer recommends running next; like a QM
result, it becomes an agent-authored note proposed through the **same** PR-gate (D-005) so a human
validates before it enters the graph.

This module is the *mapping only*, which is the connector split: turning a campaign result into a
note is the BO domain's knowledge, so it lives in the bundle; pushing that note through the PR-gate
is the GxP boundary, so it stays in core (`ConnectorJobWorkflow` publishes whatever note the result
envelope carries). The activity that used to do both is gone — a connector must not be able to reach
around the gate, and now it structurally cannot.

Core also stamps the run and *why it was started* onto this note on the way through
(`durable/job_record.py::note_with_run_provenance`, D-157). So this builder answers "what came out
and over what space", and never has to know the job id or the requester — which is what keeps the
mapping a pure function of the campaign.
"""

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note
from chemclaw.science.bo.problem import (
    CampaignResult,
    CategoricalParameter,
    Observation,
    OptimizationProblem,
    Parameter,
)


def note_from_campaign_result(
    objective_name: str, problem: OptimizationProblem, result: CampaignResult
) -> Note:
    """Map a campaign's best point to an agent-authored `bo-candidate` note.

    The note records the recommended conditions, the achieved objective value and whether
    it was measured or predicted (`provenance`), and how many evaluations backed the
    recommendation — the context a reviewer needs before approving a lab run.

    It also records the **space that was searched**, which the earlier version left out (D-157). A
    recommendation of "1.2 mol% Pd" means one thing when the campaign could have gone to 5 mol% and
    something else entirely when 1.2 was the ceiling, and the reader of a merged note has no other
    copy of the decision space: the spec lives in the durable job record and in Temporal's history,
    neither of which is in front of someone reviewing a markdown file.

    The id is the objective plus a hash of the recommended parameters, so re-proposing the same
    recommendation is idempotent. The *body* is not quite: core appends the run and its reason
    (D-157), so a second, differently-motivated campaign that lands on the same point proposes the
    same note id with a different footer — which is a real difference (two runs agreeing, for two
    reasons) and one a reviewer should see. The identical campaign never gets that far: it rejoins
    the first run's id and never re-executes.

    **The value comes before the conditions, and carries the surrogate's opinion of it** (F8-T1).
    Both are the same fix. A retrieval excerpt is a blind character prefix of the body
    (`retrieval.retrievers._excerpt`, 240 characters by default), and the objective value used to
    sit *after* the full conditions list — so a campaign over five or six parameters produced an
    excerpt quoting the recommended conditions with no number attached at all, which is the worst
    of the possible truncations. Leading with the number puts it, its provenance and the model's
    own uncertainty about it inside the prefix that actually gets quoted back.

    The note carries no `[[wikilink]]` (a dangling link would fail `chemclaw.kg.validate` on the
    very PR this opens).
    """
    best = result.best
    conditions = "\n".join(f"- {name}: {value}" for name, value in sorted(best.params.items()))
    space = "\n".join(f"- {_parameter_range(parameter)}" for parameter in problem.parameters)
    body = (
        f"Bayesian-optimization recommendation for objective `{objective_name}`, "
        f"from {len(result.history)} evaluation(s).\n\n"
        f"- objective value: {best.value:.6g} ({best.provenance}; {_surrogate_belief(best)})\n"
        f"- direction: {problem.objective.direction} `{problem.objective.name}`\n\n"
        f"Recommended conditions:\n{conditions}\n\n"
        f"Searched over:\n{space}\n"
    )
    return Note(
        id=f"bo-{objective_name}-{stable_hash(dict(best.params), chars=12)}",
        type="bo-candidate",
        created_by="agent",
        source=f"bo:{objective_name}",
        body=body,
    )


def _surrogate_belief(best: Observation) -> str:
    """What the model thought of this point before it was evaluated, in one clause (F8-T1).

    Two honest readings, and the distinction is the one a reviewer needs. A recorded sd means the
    surrogate proposed this point and says how sure it was of the region: small is an exploit of
    chemistry it has learned, large an excursion into chemistry it has not. No sd means no model
    was involved — the point came from the space-filling seed design — which is a different claim
    entirely and reads as an endorsement if left unsaid.

    Never phrased as the uncertainty *of* the reported value: that value came from the evaluator,
    not from the surrogate, and the sd is what the model believed beforehand.
    """
    if best.surrogate_sd is None:
        return "a space-filling seed point, proposed before any surrogate had an opinion"
    return f"surrogate posterior sd ±{best.surrogate_sd:.3g} at the time it was proposed"


def _parameter_range(parameter: Parameter) -> str:
    """One decision variable as a single line: its name and what it was allowed to be.

    Categorical options are listed rather than counted — "one of 4 ligands" tells a reviewer
    nothing about whether the ligand they would have tried was even on the list — but the listing
    is **bounded**, because one shipped objective makes it unbounded: `molecule_library_problem`
    turns a screening library into one categorical whose levels are every SMILES in it, so a
    500-molecule campaign would write a single 12 KB line into a note whose job is to let a chemist
    decide on one experiment. Past the budget it says how many were left out, and the complete
    space stays one lookup away in the run's durable record (D-157), which is the column that
    exists for exactly this.

    The budget is the shared `note_excerpt_chars` — the one note-excerpt allowance the report
    harness and the memory layer already spend — so this cannot drift into a second answer to
    "how much prose belongs in a note".
    """
    if not isinstance(parameter, CategoricalParameter):
        return f"{parameter.name}: {parameter.lower:g} to {parameter.upper:g}"
    shown: list[str] = []
    budget = settings.note_excerpt_chars
    for category in parameter.categories:
        # +2 for the ", " this level costs once it is not the first.
        budget -= len(category) + 2
        if budget < 0 and shown:
            break
        shown.append(category)
    listed = ", ".join(shown)
    omitted = len(parameter.categories) - len(shown)
    if omitted:
        listed += f", … (+{omitted} more; the full set is in the run record)"
    return f"{parameter.name}: one of {listed}"
