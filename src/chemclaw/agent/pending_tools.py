"""Agent tools for the durable wait: raise a question, and read what is outstanding.

`request_external_input` starts a durable workflow, so it is state-changing (authorized, needs an
actor, plan-gated); `check_pending_requests` is a read. Neither can answer a question: answering is
the `POST /pending/{id}/answer` route, because a model must never be able to authorize its own work
(D-005).
"""

from typing import Literal

from pydantic import BaseModel, Field, computed_field

from chemclaw.agent.authz import authorize_trigger, require_actor
from chemclaw.agent.tool_framing import defanged_payload
from chemclaw.core.errors import ChemclawError
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_job_started
from chemclaw.durable import pending_store
from chemclaw.durable.awaiting import AwaitRequest, open_wait
from chemclaw.kg.premise import count_refusals, premise_breaks

#: The kinds a chemist-facing ask may take. Narrower than `awaiting.KINDS`: an `approval` is raised
#: by the effector seam and the plan gate, never by the model.
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
    # The premise is derived by `AwaitRequest` itself, never passed as an argument, so no producer
    # of a wait can skip it.
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
    # Refused at the ask so every open wait begins with a whole premise; that is what lets a break
    # found at answer time mean "since the question". Every break refuses here, including `absent`,
    # because the model can fix its own citation. Nothing has been written yet.
    broken = await premise_breaks(request.premise_note_ids)
    if broken:
        count_refusals("ask", broken)
        raise ChemclawError(
            "this question rests on knowledge that no longer holds, so nobody could answer it "
            "usefully: " + "; ".join(item.describe() for item in broken) + ". Re-read the current "
            "evidence and ask again on what it says."
        )
    # The launch, its id and reuse policy are `durable/awaiting.py`'s, shared with the runner; this
    # tool owns authorization, the premise refusal and the announcement.
    request_id, opened = await open_wait(request)
    if not opened:
        # The same question is already open. Hand back its id rather than opening a second wait, and
        # announce nothing: this run already existed, so a start signal would be false.
        return request_id
    record_job_started(request_id, "awaiting")
    return request_id


class PendingOverview(BaseModel):
    """What this system is still waiting on, **and whether that is all of it**.

    The bare `list[dict]` this replaced carried its incompleteness in neither channel that matters.
    Measured against a real database: 35 waiting rows, 20 returned, no field, no log line and no
    counter naming the fifteen. The tool's own docstring said "everything still waiting" and warned
    about the *other* incompleteness — that this system knows only the questions it raised itself —
    so the one caveat present was the one that was not biting.
    """

    requests: list[dict[str, object]] = Field(default_factory=list)
    # Everything matching before the page bound, counted in the same transaction as the page.
    total_waiting: int = Field(default=0, ge=0)
    # The bound the store actually applied, which is not always the one asked for.
    limit_applied: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before saying what is outstanding.

        A `computed_field` so the sentence is serialized with the result.
        """
        scope = (
            "This is what is outstanding *in this system* — only the questions this system itself "
            "raised, never a team's whole open work."
        )
        shown = len(self.requests)
        if self.total_waiting > shown:
            return (
                f"PARTIAL: {shown} of {self.total_waiting} open requests are shown, soonest "
                f"deadline first (page bound {self.limit_applied}). The rest have later deadlines "
                "and are NOT resolved — narrow with `asked_of` or raise `limit` before saying "
                f"anything about how much is outstanding. {scope}"
            )
        if not shown:
            return (
                f"NOTHING WAITING: this system holds no open request matching that query. {scope}"
            )
        return f"COMPLETE: every open request matching that query is shown. {scope}"


@tool
async def check_pending_requests(asked_of: str = "", limit: int = 20) -> PendingOverview:
    """Read what this system is still waiting on — questions raised and not yet answered.

    Use it before raising a new one (the answer may already be on its way), when a chemist asks
    what is outstanding, or when explaining why a campaign has not moved.

    Each entry says what was asked, why, who it is routed to, when it is due and how many times it
    has been chased. A request routed to nobody in particular is waiting on whoever is entitled,
    which is why it appears in every query rather than in none.

    Incomplete in two ways, both on the answer: it knows only the questions this system raised,
    and `requests` is a page. Read `verdict` before saying how much is outstanding.

    Args:
        asked_of: Narrow to what is routed to one actor or entitlement, plus everything unrouted.
            Empty returns every open request.
        limit: How many to return, soonest deadline first (bounded; see `limit_applied`).

    Returns:
        A page of open requests, `total_waiting`, and a verdict saying which to build on.
    """
    page = await pending_store.open_requests(asked_of=asked_of, limit=limit)
    return PendingOverview(
        requests=[
            # Every field of `PendingRequest` is unconstrained text raised by some turn, so the
            # whole row is defanged rather than a field list. `answer` is excluded: these are open
            # requests, so it is empty.
            defanged_payload(request.model_dump(exclude={"answer"}))
            for request in page.requests
        ],
        total_waiting=page.total_waiting,
        limit_applied=page.limit_applied,
    )
