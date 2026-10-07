"""Authorization decisions live in exactly one module.

- `authorize_trigger` — the coarse gate a job-launching tool calls before starting expensive
  durable work: `expensive_actions()` against `entra_privileged_roles`.
- `authorize_tool` — the per-tool RBAC gate applied to every call by `agent.tool_authz`:
  `tool_role_gates` plus `tool_authz_default`, with `DEFAULT_WRITE_TOOL_GATES` closing three
  knowledge-graph writers by default.

Both read the turn's ambient identity, are enforced only under `entra_required` (open in dev),
and share `_has_required_role`. This module also owns the tool classifications the plan gate
and dry-run read (`side_effecting_tools`, `side_effecting_call`).
"""

from collections.abc import Mapping
from functools import cache
from typing import Any

from chemclaw.agent.framing import safe_id
from chemclaw.agent.refusal_route import routed
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_roles


class AuthorizationError(Exception):
    """The current user is not entitled to trigger the requested action.

    Deliberately not a `ChemclawError`: that means "invalid input", and the same call succeeds for
    another user. It also keeps `surface_domain_errors` (which answers `"Error: ..."`) from
    swallowing it before `surface_authorization_denials` answers `"Refused: ..."`. Registered by
    name as non-retryable in `durable.publish._BAD_DATA_TYPES`, since a refusal never changes on
    retry.
    """


# The expensive actions core itself owns, beside those connector manifests declare with
# `expensive: true`. Core-launched jobs have no manifest, so they are declared here and gated
# without any configuration: `request_development_report` and `synthesize_memory` are unbounded
# over the corpus, and the latter writes notes directly into the graph.
CORE_EXPENSIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "request_development_report",
        "synthesize_memory",
        # Raises a durable wait that spends a person's or a lab's time, not just tokens.
        "request_external_input",
        # A tournament is the most expensive thing a turn can start (dozens of structured model
        # calls), and its cost scales with `hypothesis_max_field`, a setting rather than the
        # request.
        "rank_competing_hypotheses",
    }
)

# The three knowledge-graph writers gated to `entra_privileged_role_set` when no explicit
# `tool_role_gates` entry exists. This is not the write surface: under `tool_authz_default="allow"`
# reads and most writes stay open to RBAC, and `tests/test_authz.py` measures the sets.
#
# The plan gate carries the rest: with the harness in `plan_only`, a human approves a plan before
# any side-effecting tool runs. With the harness off, under `execute` autonomy, or inside an
# approved plan, a role-less authenticated user can reach a durable launcher. Widening this set is
# an operator's `tool_role_gates` decision, not a default that arrives with an upgrade.
DEFAULT_WRITE_TOOL_GATES: frozenset[str] = frozenset(
    {
        "record_knowledge_note",  # commits into the knowledge repo
        "record_confirmed_answer",  # commits into the knowledge repo
        "record_failure",  # commits into the knowledge repo, and retires a claim already in it
    }
)

# Every in-process tool that changes stored state or starts durable work — the set the plan gate
# refuses under an unapproved plan.
#
# A superset of `DEFAULT_WRITE_TOOL_GATES`, kept separate: that set is an RBAC fallback, and
# widening it would close tools on unconfigured deployments. The complete side-effecting set adds
# every enabled connector job and template launcher (`side_effecting_tools()`).
STATE_CHANGING_TOOLS: frozenset[str] = (
    frozenset(
        {
            "record_knowledge_note",  # pushes a branch to the knowledge repo
            "record_confirmed_answer",  # pushes a branch to the knowledge repo
            "remember_preference",  # writes user_preferences
            "forget_preference",  # deletes from user_preferences
            "watch_for",  # writes subscriptions
            "stop_watching",  # deletes from subscriptions
            "request_development_report",  # starts a durable report workflow
            "synthesize_memory",  # starts a corpus scan that opens knowledge PRs
            # Starts a durable wait that commits somebody else's time, which the plan gate should
            # see.
            "request_external_input",
            # Both write `experiment_protocols` and its revision table. Plan-gated rather than
            # write-gated: a design is a chemist's working document, not a knowledge claim.
            "structure_experiment_request",  # writes the structured ask as revision 1
            "draft_experiment_protocol",  # writes a protocol revision
            # Writes `experiment_arm_results`, an INSERT-only table: an outcome attached wrongly
            # stays.
            "attach_plate_results",  # writes measured outcomes against a revision
            # Both halves of the composed-workflow seam: `compose_workflow` writes a procedure that
            # later runs, and `run_composed_workflow` starts a durable run like a `run_<template>`
            # launcher (listed here because `template_tool_names()` reads only `data/templates/`).
            "compose_workflow",  # writes composed_workflows
            "run_composed_workflow",  # starts a TemplateWorkflow run
            # Writes a `behaviour_proposals` row; the plan gate should see a turn proposing a
            # behaviour change. Being here also removes it from every helper's surface.
            "propose_skill",  # writes behaviour_proposals
            # Starts a durable tournament that writes notes into the graph. Being here also keeps it
            # off helpers, where it would be a fan-out inside a fan-out.
            "rank_competing_hypotheses",  # starts a durable tournament that records notes
        }
    )
    | DEFAULT_WRITE_TOOL_GATES
)

# The in-process reads that consult the record — core's half of what a turn looked at. Bundles
# declare their half as `knowledge_read`; `knowledge_read_tools()` is the union.
#
# Hand-written, not derived from `READ_ONLY_TOOLS`: some reads (`ask_clarifying_question`) consult
# nothing. `tests/test_turn_knowledge.py` holds it a subset of `READ_ONLY_TOOLS`, and
# `tests/test_authz.py` holds `READ_ONLY_TOOLS` and `STATE_CHANGING_TOOLS` to a partition of the
# tool registry, so an unclassified tool fails the suite.
KNOWLEDGE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "assemble_evidence_pack",
        "condense_protocols",
        "expand_note",
        "find_knowledge_gaps",
        "find_notes",
        "find_past_jobs",
        "gather_evidence",
        "recall_observations",
        "recall_preferences",
    }
)

# The writes that reach the record — what a turn put back into what we know.
#
# Stated rather than derived from `side_effecting_tools()`, which would count any state-changing
# call; connector bundles cannot write to the graph or memory tiers, so these are all of them.
# `synthesize_memory` launches a job that writes notes; `forget_preference` changes the durable
# record. `tests/test_turn_knowledge.py` holds this inside `side_effecting_tools()`.
KNOWLEDGE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "forget_preference",
        "record_confirmed_answer",
        "record_failure",
        "record_knowledge_note",
        "remember_preference",
        "synthesize_memory",
    }
)

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "ask_clarifying_question",
        # An artefact is part of the answer, not an effect: creating or revising one needs no
        # approved plan. Promoting it to a note or protocol goes through the gated tools. The
        # writers are still kept off helpers (`agent/subagents.SPEAKS_TO_THE_CHEMIST`).
        "create_exhibit",
        "read_exhibit",
        "revise_exhibit",
        "expand_note",
        "find_knowledge_gaps",
        "find_notes",
        # A search over the durable job record; a read the agent should make before asking for an
        # expensive run.
        "find_past_jobs",
        "gather_evidence",
        # A model call is a cost, not an effect: it writes nothing and reaches only readable
        # sources.
        "condense_protocols",
        # The ungated observations tier, read to find evidence worth gathering before anything is
        # authorized.
        "recall_observations",
        # A read of the durable-wait inbox; its sibling `request_external_input` is state-changing.
        "check_pending_requests",
        # A read of the commitment mirror; deliberately no writing counterpart.
        "review_commitments",
        # Scoped to one conversation, which is what makes its free text safe to return.
        "assemble_evidence_pack",
        # Aggregates over this system's own tables: counts and bounded vocabularies only.
        "review_activity",
        # Reads the agent should make before revising, so revisions derive from the current head.
        "read_experiment_protocol",
        "find_experiment_protocols",
        # Stores nothing: it returns a proposal, and keeping one goes through
        # `draft_experiment_protocol`. A chemist asks this while deciding whether to approve work.
        "rescale_experiment_protocol",
        # A read asked while deciding whether to approve the next round.
        "read_plate_results",
        # Arithmetic over a campaign's recorded points, writing nothing; must be answerable before a
        # plan to draft the experiments is approved.
        "experiment_arms_from_campaign",
        # Arithmetic over the call's own numbers; reads no store and writes nothing.
        "check_against_specification",
        # The same case: a regression over timepoints the caller passed in the call.
        "estimate_stability_trend",
        "get_durable_job_status",
        "list_attachments",
        "list_watches",
        "read_attachment",
        "recall_preferences",
    }
)


@cache
def knowledge_read_tools() -> frozenset[str]:
    """Every tool that consults the record — in-process, plus what the enabled bundles declare.

    The union `turn_costs.retrieval_calls` counts against. Bundles declare their own half so core
    never names other bundles' tools. Cached for the process; cleared by the test discovery-cache
    fixture.
    """
    from chemclaw.connectors.registry import knowledge_read_tool_names

    return KNOWLEDGE_READ_TOOLS | frozenset(knowledge_read_tool_names())


@cache
def side_effecting_tools() -> frozenset[str]:
    """Every tool that changes something outside the turn — the one set the write gates share.

    Three sources, each owned where its knowledge lives:

    - `STATE_CHANGING_TOOLS` — the in-process writes;
    - every enabled connector's declared `state_changing` endpoint tools plus all its jobs;
    - every enabled template launcher.

    So a bundle is gated the day it is enabled. Cached because its inputs are cached discovery
    (the test fixture clears it with them); imported lazily because the registries reach the agent
    builder. Lives here rather than in the plan gate because dry-run needs it too.
    """
    from chemclaw.connectors.registry import state_changing_tool_names
    from chemclaw.templates.registry import template_tool_names

    return (
        STATE_CHANGING_TOOLS
        | frozenset(state_changing_tool_names())
        | frozenset(template_tool_names())
    )


# The filesystem verbs that can reach the durable memory store; `delete` and `execute` are
# withheld by `scratchpad_tools()`. Both take the path as `file_path`.
_MEMORY_WRITE_VERBS: frozenset[str] = frozenset({"write_file", "edit_file"})


def memory_write_verbs() -> frozenset[str]:
    """The tools whose gatedness is a function of their *arguments* rather than their name.

    Together with `side_effecting_tools()` this is the whole gated surface; exposed so a test can
    enumerate both halves. To ask whether a call is gated, use `side_effecting_call`.
    """
    return _MEMORY_WRITE_VERBS


def writes_durable_memory(name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether this *call* writes a person's durable memories, as opposed to the turn's scratchpad.

    `write_file` serves two roots: `/scratch/` (turn state) and `/memories/` (Postgres, durable), so
    the path argument decides. The path is normalised with upstream's `validate_path` first, exactly
    as `FilesystemMiddleware` does before routing, so spellings like `memories/a.md` or the bare
    root `/memories` cannot slip past. A missing, non-string or unresolvable path counts as durable:
    an argument the gate cannot read is never the ungated case. `file_path` is pinned in
    `tests/test_upstream_surface.py`, since a rename would fail open.

    Args:
        name: The tool being called.
        arguments: That call's arguments, as the model supplied them.

    Returns:
        `True` when this call would write under the memory root.
    """
    from deepagents.backends.utils import validate_path

    from chemclaw.agent.scratchpad import MEMORY_ROOT

    if name not in _MEMORY_WRITE_VERBS:
        return False
    path = arguments.get("file_path")
    if not isinstance(path, str):
        return True
    try:
        routed = validate_path(path)
    except ValueError:
        return True
    return routed == MEMORY_ROOT.rstrip("/") or routed.startswith(MEMORY_ROOT)


def side_effecting_call(name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether this call changes something outside the turn — the question both write gates ask.

    One predicate so dry-run and the plan gate cannot drift. `write_todos` is not covered: gating it
    under an unapproved plan would deadlock the only call that produces a plan. Handoffs are
    `changes_the_conversation`'s concern.
    """
    return name in side_effecting_tools() or writes_durable_memory(name, arguments)


def changes_the_conversation(name: str) -> bool:
    """Whether this call moves the conversation to another agent — what dry-run must also refuse.

    A `transfer_to_<peer>` writes the checkpointed `active_agent`, so a dry run must refuse it. The
    plan gate must not: a handoff cannot extend the turn's authority, so gating it protects nothing.
    Recognised by shape (`handoff.is_handoff_tool_name`), imported lazily to keep LangGraph out of
    this module's import.
    """
    from chemclaw.agent.handoff import is_handoff_tool_name

    return is_handoff_tool_name(name)


def expensive_actions() -> frozenset[str]:
    """Every action the coarse trigger gate protects — the declarations plus the operator's list.

    Derived from enabled manifests' `expensive: true` jobs, plus `CORE_EXPENSIVE_ACTIONS`, unioned
    with `entra_expensive_actions` for anything an operator adds, so a bundle is gated the day it is
    enabled. Imported lazily because the connector registry reaches the agent builder.
    """
    from chemclaw.connectors.registry import enabled

    declared = frozenset(
        job.name for manifest in enabled() for job in manifest.jobs if job.expensive
    )
    return settings.entra_expensive_action_set | declared | CORE_EXPENSIVE_ACTIONS


def _actor() -> str:
    """Name the turn's user for a refusal message, or say plainly that there isn't one."""
    return get_current_actor() or "an unauthenticated user"


def _has_required_role(required: frozenset[str]) -> bool:
    """Whether the turn's user holds at least one of `required` (the shared membership predicate).

    An empty `required` is always satisfied; callers that must fail closed check for it first.
    """
    if not required:
        return True
    return bool(get_current_roles() & required)


def authorize_tool(tool: str) -> None:
    """Authorize the current turn's user to invoke `tool`, or raise `AuthorizationError`.

    Consults `tool_role_gates` against the turn's roles. An ungated tool follows
    `tool_authz_default`: refused under `"deny"`, open under `"allow"` except
    `DEFAULT_WRITE_TOOL_GATES`, which require a privileged role (an explicit gate overrides). The
    built-in gate only narrows `"allow"`; it never widens `"deny"`. Enforced only under
    `entra_required`.

    Refusals are relayed verbatim to the chemist, so each names who, which tool and why, with the
    routed footer declaring no sanctioned path; they never enumerate role names.

    Args:
        tool: The tool's registered name (e.g. `"record_knowledge_note"`, `"gather_evidence"`).

    Raises:
        AuthorizationError: When enforcement is on and the user may not call `tool` — its gate
            lists roles the user lacks, or it is ungated under a `deny` default.
    """
    if not settings.entra_required:
        return  # dev: no tenant, open gate
    # Only the message uses the sanitized name; decisions use `tool` verbatim. The name is
    # model-supplied and unvalidated here, so it must not be able to forge the refusal footer.
    named = safe_id(tool)
    required = settings.tool_role_gates.get(tool)
    if required is not None:
        if not _has_required_role(frozenset(required)):
            raise AuthorizationError(
                routed(
                    f"{_actor()} is not authorized to use {named}: the account holds none of the "
                    "roles this tool requires",
                    code="tool_role_not_held",
                    boundary="this deployment's per-tool authorization",
                    who_can_act=f"an account holding one of the roles {named} requires here",
                )
            )
        return
    if settings.tool_authz_default == "deny":
        # Allowlist mode: not listed ⇒ refused, always — checked before the built-in write gate so a
        # privileged role never opens an unlisted write tool under `deny`.
        raise AuthorizationError(
            routed(
                f"{_actor()} is not authorized to use {named}: this deployment permits only an "
                "approved list of tools, and this one is not on it",
                code="tool_not_permitted_here",
                boundary="this deployment's list of permitted tools",
                who_can_act=f"an account this deployment has permitted for {named}",
            )
        )
    if tool in DEFAULT_WRITE_TOOL_GATES:
        privileged = settings.entra_privileged_role_set
        # An empty privileged set fails closed: `_has_required_role` would treat it as satisfied.
        if not privileged or not _has_required_role(privileged):
            raise AuthorizationError(
                routed(
                    f"{_actor()} is not authorized to use {named}: it changes stored data, so it "
                    "requires a privileged role the account does not hold",
                    code="privileged_role_not_held",
                    boundary="the built-in gate on tools that change stored data",
                    who_can_act="an account holding a privileged role in this deployment",
                )
            )


def authorize_trigger(action: str) -> None:
    """Authorize the current turn's user to trigger `action`, or raise `AuthorizationError`.

    Args:
        action: The trigger's name (e.g. `"sample_conformers"`). If it is not in
            `expensive_actions()`, the call is always allowed.

    Raises:
        AuthorizationError: When enforcement is on, the action is expensive, and the user holds none
            of the `entra_privileged_roles` (or there is no authenticated user at all, or the
            deployment declared no privileged role for an expensive action to require).
    """
    if not settings.entra_required:
        return  # dev: no tenant, open gate
    if action not in expensive_actions():
        return  # not a gated action
    actor = get_current_actor()
    if actor is None:
        raise AuthorizationError(
            routed(
                f"{action} requires an authenticated user",
                code="expensive_action_unauthenticated",
                boundary="the entitlement gate on expensive actions",
                who_can_act="an authenticated user holding a privileged role",
            )
        )
    privileged = settings.entra_privileged_role_set
    # An empty privileged set fails closed, as in `authorize_tool`. Config validation cannot catch
    # it: a manifest-declared expensive job needs no entry in either role setting.
    if not privileged or not _has_required_role(privileged):
        raise AuthorizationError(
            routed(
                f"user {actor} lacks a privileged role for {action}",
                code="expensive_action_role_not_held",
                boundary="the entitlement gate on expensive actions",
                who_can_act="an account holding a privileged role in this deployment",
            )
        )


def require_actor() -> str:
    """Return the turn's Entra actor for a user-triggered workflow, or raise if absent.

    The core rule: every user-triggered backend workflow carries the requesting user's `oid`, and
    under `entra_required` a trigger with no authenticated user is rejected before durable work
    starts. In dev, `service_actor_id` stands in. System-triggered jobs do not call this.

    Returns:
        The authenticated user's Entra `oid`, or `settings.service_actor_id` when enforcement's off.

    Raises:
        AuthorizationError: When `entra_required` and there is no authenticated user in context.
    """
    actor = get_current_actor()
    if actor is not None:
        return actor
    if settings.entra_required:
        raise AuthorizationError("a user-triggered workflow requires an authenticated user")
    return settings.service_actor_id
