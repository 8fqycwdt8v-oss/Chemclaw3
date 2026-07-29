"""Map a BO campaign's recommendation to a knowledge-graph note (plan step 1d.5).

A finished campaign's best point is the experiment the optimizer recommends running next; like a QM
result, it becomes an agent-authored note proposed through the **same** PR-gate (D-005) so a human
validates before it enters the graph.

This module is the *mapping only*, which is the connector split: turning a campaign result into a
note is the BO domain's knowledge, so it lives in the bundle; pushing that note through the PR-gate
is the GxP boundary, so it stays in core (`ConnectorJobWorkflow` publishes whatever note the result
envelope carries). The activity that used to do both is gone — a connector must not be able to reach
around the gate, and now it structurally cannot.
"""

from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note
from chemclaw.science.bo.problem import CampaignResult


def note_from_campaign_result(objective_name: str, result: CampaignResult) -> Note:
    """Map a campaign's best point to an agent-authored `bo-candidate` note.

    The note records the recommended conditions, the achieved objective value and whether
    it was measured or predicted (`provenance`), and how many evaluations backed the
    recommendation — the context a reviewer needs before approving a lab run. The id is the
    objective plus a hash of the recommended parameters, so re-proposing the same
    recommendation is idempotent. It carries no `[[wikilink]]` (a dangling link would fail
    `chemclaw.kg.validate` on the very PR this opens).
    """
    best = result.best
    conditions = "\n".join(f"- {name}: {value}" for name, value in sorted(best.params.items()))
    body = (
        f"Bayesian-optimization recommendation for objective `{objective_name}`, "
        f"from {len(result.history)} evaluation(s).\n\n"
        f"Recommended conditions:\n{conditions}\n\n"
        f"- objective value: {best.value:.6g} ({best.provenance})\n"
    )
    return Note(
        id=f"bo-{objective_name}-{stable_hash(dict(best.params), chars=12)}",
        type="bo-candidate",
        created_by="agent",
        source=f"bo:{objective_name}",
        body=body,
    )
