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
(`durable/job_record.py::note_with_run_provenance`, D-155). So this builder answers "what came out
and over what space", and never has to know the job id or the requester — which is what keeps the
mapping a pure function of the campaign.
"""

from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note
from chemclaw.science.bo.problem import (
    CampaignResult,
    CategoricalParameter,
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

    It also records the **space that was searched**, which the earlier version left out (D-155). A
    recommendation of "1.2 mol% Pd" means one thing when the campaign could have gone to 5 mol% and
    something else entirely when 1.2 was the ceiling, and the reader of a merged note has no other
    copy of the decision space: the spec lives in the durable job record and in Temporal's history,
    neither of which is in front of someone reviewing a markdown file.

    The id is the objective plus a hash of the recommended parameters, so re-proposing the same
    recommendation is idempotent. The *body* is not quite: core appends the run and its reason
    (D-155), so a second, differently-motivated campaign that lands on the same point proposes the
    same note id with a different footer — which is a real difference (two runs agreeing, for two
    reasons) and one a reviewer should see. The identical campaign never gets that far: it rejoins
    the first run's id and never re-executes.

    The note carries no `[[wikilink]]` (a dangling link would fail `chemclaw.kg.validate` on the
    very PR this opens).
    """
    best = result.best
    conditions = "\n".join(f"- {name}: {value}" for name, value in sorted(best.params.items()))
    space = "\n".join(f"- {_parameter_range(parameter)}" for parameter in problem.parameters)
    body = (
        f"Bayesian-optimization recommendation for objective `{objective_name}`, "
        f"from {len(result.history)} evaluation(s).\n\n"
        f"Recommended conditions:\n{conditions}\n\n"
        f"- objective value: {best.value:.6g} ({best.provenance})\n"
        f"- direction: {problem.objective.direction} `{problem.objective.name}`\n\n"
        f"Searched over:\n{space}\n"
    )
    return Note(
        id=f"bo-{objective_name}-{stable_hash(dict(best.params), chars=12)}",
        type="bo-candidate",
        created_by="agent",
        source=f"bo:{objective_name}",
        body=body,
    )


def _parameter_range(parameter: Parameter) -> str:
    """One decision variable as a single line: its name and what it was allowed to be.

    Categorical options are listed in full rather than counted — "one of 4 ligands" tells a
    reviewer nothing about whether the ligand they would have tried was even on the list.
    """
    if isinstance(parameter, CategoricalParameter):
        return f"{parameter.name}: one of {', '.join(parameter.categories)}"
    return f"{parameter.name}: {parameter.lower:g} to {parameter.upper:g}"
