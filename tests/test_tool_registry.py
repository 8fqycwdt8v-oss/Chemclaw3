"""The capability-tool registry seam.

Registration is by function name, duplicates are a loud programming error, and the agent
advertises exactly the expected in-process tools wrapped by the audit and authorization
middleware.
"""

import pytest

from chemclaw.agent.chemclaw_agent import _capability_tools, _withheld_tool_names
from chemclaw.connectors.registry import enabled, withheld_job_names
from chemclaw.core.tool_registry import (
    _REGISTRY,
    register_tool,
    registered_tool_names,
    registered_tools,
    tool,
)
from chemclaw.templates.registry import template_tool_names
from tests.surface import surface

# Every in-process capability tool, spelled out: the registry must reproduce this set exactly.
# Connector tools are advertised separately per turn. Adding one is a reviewed edit here, which
# invites the question "should this be a connector tool instead?".
_EXPECTED_INPROCESS_TOOLS = {
    # The conversation plumbing: everything that reads or writes the *turn's own* state, which is
    # by definition unavailable to another process.
    "ask_clarifying_question",
    "list_attachments",
    "read_attachment",
    "watch_for",
    "list_watches",
    "stop_watching",
    "remember_preference",
    "recall_preferences",
    "forget_preference",
    # The knowledge layer: reads, plus the two PR-gate writers. The gate is core's, so its writers
    # are too — a connector reaches it only by returning a note in a job envelope.
    "find_notes",
    "expand_note",
    "gather_evidence",
    "condense_protocols",
    # Experiment design tools: write one proposed design and read it back. In-process because the
    # store is core's and drafting is part of a turn, not durable work.
    "structure_experiment_request",
    "compose_workflow",
    "run_composed_workflow",
    # In-process for the same reason `compose_workflow` is: the store is core's, and proposing is
    # the turn's own composition rather than durable work. It writes a proposal and never a skill —
    # a person's route is what turns one into behaviour.
    "propose_skill",
    "draft_experiment_protocol",
    "read_experiment_protocol",
    "find_experiment_protocols",
    # Scaling a stored design to a new basis. In-process for the same reason as the three above:
    # it reads the design store, which is core's, and writes nothing — keeping a rescale goes back
    # through `draft_experiment_protocol` under the ordinary `parent_revision` check.
    "rescale_experiment_protocol",
    # The two halves of the plate-results loop, in-process for the three above's reason: both
    # read the design store, which is core's.
    "attach_plate_results",
    "read_plate_results",
    # Translates a campaign's suggested `{parameter: value}` points into labelled design arms.
    # In-process: the campaign store is core's and the translation is arithmetic.
    "experiment_arms_from_campaign",
    # Unit arithmetic with a verdict, over values passed in the call. In-process because it reaches
    # nothing — no store, no connector, no engine — and because `core/units` is where the comparison
    # that refuses an area percent against a weight-percent limit already lives.
    "check_against_specification",
    # Its sibling over time, and scipy's least squares plus a t-quantile is still arithmetic.
    "estimate_stability_trend",
    "find_knowledge_gaps",
    "record_knowledge_note",
    "record_confirmed_answer",
    "record_failure",
    "recall_observations",
    # The durable launchers core still owns, and the status tool every durable job is collected
    # with. Expensive jobs are otherwise declared connector jobs. `synthesize_memory` is the
    # on-demand trigger for the corpus miners, which run on no schedule.
    "request_development_report",
    "rank_competing_hypotheses",
    "synthesize_memory",
    "get_durable_job_status",
    # The retrospective half of that pair (D-157): the durable record of every finished run, which
    # is core's for the same reason the status tool is — it is generic over every job, and a
    # connector must not be able to see another bundle's runs.
    "find_past_jobs",
    # The operational read model. In-process because it is generic over every capability and a
    # connector bundle must not read another bundle's record.
    "review_activity",
    # The durable wait (D-2026-08-29). In-process because the wait is core's primitive rather than
    # any capability's: a BO round, a gate review and an effect approval are the same object, and a
    # bundle owning it would make four copies of one deadline.
    "request_external_input",
    "check_pending_requests",
    # The commitment mirror (D-2026-08-29). In-process because the mirror is core's: it
    # spans every source, and a bundle owning it could see only its own.
    "review_commitments",
    # The evidence pack (D-2026-08-29). In-process for the reason every operational read
    # is: it spans the whole record, and a bundle could see only its own part of it.
    "assemble_evidence_pack",
    # Artefacts (`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`). In-process because
    # an artefact is session state beside the transcript — the turn's own session id scopes every
    # read — and part of the answer rather than a capability any bundle owns.
    "create_exhibit",
    "revise_exhibit",
    "read_exhibit",
}


def test_registry_holds_the_inprocess_tools_and_only_generated_launchers_besides() -> None:
    """Importing the agent registers precisely the in-process tools; building it adds launchers.

    Generated launchers for connector jobs and step templates are registered at build time so they
    are gated and audited like any other tool; the invariant is that nothing else ever appears.
    """
    assert _EXPECTED_INPROCESS_TOOLS <= set(registered_tool_names())
    surface(None)
    extra = set(registered_tool_names()) - _EXPECTED_INPROCESS_TOOLS
    jobs = {job.name for manifest in enabled() for job in manifest.jobs}
    # Bounded on both sides rather than equal: the registry only grows, so a launcher an earlier
    # build registered may still be held while this deployment withholds it. A job this deployment
    # cannot run is withheld too, so the lower bound leaves it out and the upper keeps it.
    bound_jobs = jobs - set(withheld_job_names())
    assert (
        bound_jobs | set(template_tool_names())
        <= extra
        <= jobs | set(template_tool_names(declared=True))
    )


def test_capability_tools_are_exactly_the_registry() -> None:
    """`_capability_tools()` is the registry, whole and in order; connectors are not in it.

    Connector tools are per turn. Template launchers this deployment withholds are subtracted.
    """
    tools = _capability_tools()
    withheld = _withheld_tool_names()
    assert tools == [tool for tool in registered_tools() if tool.__name__ not in withheld]


def test_agent_advertises_the_registered_inprocess_tools() -> None:
    """The built agent advertises every registered in-process tool under its function name."""
    agent = surface(None)
    advertised = agent.tool_names
    assert _EXPECTED_INPROCESS_TOOLS <= advertised


def test_duplicate_registration_is_a_loud_error() -> None:
    """Registering two tools under one name is a programming error (as in `evals.metric`)."""

    async def gather_evidence() -> None:  # shadows an always-registered name on purpose
        return None

    with pytest.raises(ValueError, match="already registered"):
        register_tool(gather_evidence)


def test_decorator_registers_and_returns_function_unchanged() -> None:
    """`@tool` registers by name and returns the same object the framework wraps (identity)."""
    try:

        @tool
        async def _probe_only_tool() -> int:
            return 7

        assert "_probe_only_tool" in registered_tool_names()
        assert _probe_only_tool.__name__ == "_probe_only_tool"  # unchanged by the decorator
    finally:
        _REGISTRY.pop("_probe_only_tool", None)  # keep the module-global registry clean for others
