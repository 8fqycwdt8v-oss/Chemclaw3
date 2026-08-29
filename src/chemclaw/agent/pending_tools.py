"""Agent tools for the durable wait: raise a question, and read what is outstanding.

Two tools with deliberately asymmetric standing. `request_external_input` **starts a durable
workflow** and so is state-changing: it authorizes, it requires an actor, and it is subject to the
plan gate like every other launcher. `check_pending_requests` reads the projection and is a read.

**Neither of them can answer a question, and that omission is the design.** Answering is
`POST /pending/{id}/answer`, a route, for the same reason a plan decision and a proposal decision
are routes (D-005): a model must never be able to authorize its own work. A tool that could settle
a wait would let the agent ask itself for approval and grant it in the next tool call, and the
audit trail would record a human's question answered by nobody.
"""

from typing import Literal

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.agent.authz import authorize_trigger, require_actor
from chemclaw.agent.framing import defang
from chemclaw.core.config import settings
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_job_started
from chemclaw.durable import pending_store
from chemclaw.durable.awaiting import AwaitAnswerWorkflow, AwaitRequest, request_id_for

#: The kinds a *chemist-facing* ask may take. Narrower than `awaiting.KINDS`, which also carries
#: `approval` — an approval is raised by the effector seam and by the plan gate, never by the model
#: asking for one, for the reason this module's header gives.
AskKind = Literal["measurement", "deliverable", "review"]


@tool
async def request_external_input(
    subject: str,
    rationale: str,
    kind: AskKind = "measurement",
    asked_of: str = "",
    deadline_days: float = 7.0,
) -> str:
    """Hold a question open for a person or a lab until it is answered or its deadline passes.

    Use this when the next step cannot be computed and has to be *done*: conditions run at the
    bench, a sample submitted for analysis, a deliverable a partner owes, a document somebody must
    read. The wait is durable — it survives restarts, outlives this conversation, and appears in
    the inbox of whoever it is routed to, with the reason you gave.

    Write `subject` as what to do, in their words ("run the four conditions from round 3 and report
    isolated yield" — not "await measurements"), and `rationale` as why it matters and what it
    unblocks; that is the only record of why the question was worth asking. `deadline_days` is when
    the answer stops being useful, not how long you are willing to wait: reaching it is an outcome,
    and the requester is told nobody answered.

    Not for asking the chemist you are talking to — that is `ask_clarifying_question`, answered in
    the conversation. This is for work that leaves it. Asking twice for the same thing of the same
    people joins one wait.

    Args:
        subject: What is being asked for, concretely, in the terms the person doing it uses.
        rationale: Why it is being asked and what it unblocks.
        kind: `measurement` for something run or measured, `deliverable` for something owed by a
            partner or another team, `review` for something a person must read and comment on.
        asked_of: Who should answer — an actor id or a team entitlement. Empty means "whoever is
            entitled", the right default when you do not know the name. Routing only.
        deadline_days: How long the question stays open before it expires unanswered.

    Returns:
        The request id, which is also how the wait is found in the inbox.
    """
    authorize_trigger("request_external_input")
    request = AwaitRequest(
        kind=kind,
        subject=subject,
        rationale=rationale,
        asked_of=asked_of,
        # The core rule (F4-T3): refuse durable work with no user behind it.
        requested_by=require_actor(),
        session_id=get_current_session_id() or "",
        # Not clamped here: `open_pending_request_activity` owns the ceiling, so every caller gets
        # it rather than the two that remembered. See `AwaitAnswerWorkflow.run`.
        deadline_days=deadline_days,
    )
    request_id = request_id_for(request)
    client = await connect()
    try:
        handle = await client.start_workflow(
            AwaitAnswerWorkflow.run,
            request.model_dump(mode="json"),
            id=request_id,
            task_queue=settings.background_task_queue,
            # **The policy the deleted D-032 hold was missing.** `D-2026-08-25` recorded that a
            # decided hold could be restarted under the same id because no policy was set. Neither
            # obvious answer works: an *expired* wait completes normally, so both
            # `REJECT_DUPLICATE` and `ALLOW_DUPLICATE_FAILED_ONLY` would make a lapsed question
            # unaskable forever. `ALLOW_DUPLICATE` is correct here precisely because expiry is an
            # ordinary ending — asking again after a deadline passed is a new ask — while the
            # `WorkflowAlreadyStartedError` below still joins a *running* one.
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        # The same question is already open. Hand back its id rather than opening a second wait,
        # and announce nothing: this run already existed, so a start signal would be false.
        return request_id
    record_job_started(handle.id, "awaiting")
    return handle.id


@tool
async def check_pending_requests(asked_of: str = "", limit: int = 20) -> list[dict[str, object]]:
    """Read what this system is still waiting on — questions raised and not yet answered.

    Use it before raising a new one (the answer may already be on its way), when a chemist asks
    what is outstanding, or when explaining why a campaign has not moved.

    Each entry says what was asked, why, who it is routed to, when it is due and how many times it
    has been chased. A request routed to nobody in particular is waiting on whoever is entitled,
    which is why it appears in every query rather than in none.

    This is what is outstanding *in this system*, not what is outstanding in the programme: it
    knows only the questions this system itself raised. Never present it as a complete list of a
    team's open work.

    Args:
        asked_of: Narrow to what is routed to one actor or entitlement, plus everything unrouted.
            Empty returns every open request.
        limit: How many to return, soonest deadline first.

    Returns:
        Open requests, soonest deadline first.
    """
    requests = await pending_store.open_requests(asked_of=asked_of, limit=limit)
    return [
        {
            **request.model_dump(exclude={"subject", "rationale", "answer"}),
            # `subject` and `rationale` are free text a caller supplied — the request is readable
            # by anyone entitled, so these arrive here exactly as a retrieved chunk does.
            "subject": defang(request.subject),
            "rationale": defang(request.rationale),
        }
        for request in requests
    ]
