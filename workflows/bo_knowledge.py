"""Bridge a BO campaign's recommendation into the knowledge graph (plan step 1d.5).

A finished campaign's best point is the experiment the optimizer recommends running
next; like a QM result, it becomes an agent-authored note proposed through the **same**
PR-gate (D-005) so a human validates before it enters the graph. `note_from_campaign_result`
is the pure mapping (tested directly); `write_campaign_node` is the Temporal activity that
runs it and submits, on the `background-jobs` queue. The PR-gate itself (`propose_note` +
`default_submitter`) is reused, not duplicated — this module only adds the BO→note mapping.
"""

from temporalio import activity

from bo.problem import CampaignResult
from chemclaw.ids import stable_hash
from kg.git_submitter import default_submitter
from kg.note import Note
from kg.pr_gate import propose_note


def note_from_campaign_result(objective_name: str, result: CampaignResult) -> Note:
    """Map a campaign's best point to an agent-authored `bo-candidate` note.

    The note records the recommended conditions, the achieved objective value and whether
    it was measured or predicted (`provenance`), and how many evaluations backed the
    recommendation — the context a reviewer needs before approving a lab run. The id is the
    objective plus a hash of the recommended parameters, so re-proposing the same
    recommendation is idempotent. It carries no `[[wikilink]]` (a dangling link would fail
    `kg.validate` on the very PR this opens).
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


@activity.defn
async def write_campaign_node(objective_name: str, result: CampaignResult) -> str:
    """Write a campaign recommendation to the graph as a PR-gated note; return its ref."""
    note = note_from_campaign_result(objective_name, result)
    return await propose_note(note, default_submitter())
