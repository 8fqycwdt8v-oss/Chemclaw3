"""The agent tool over the evidence pack — how a piece of work came to be, from the record.

**A read, and the only one here that returns free text.** `chemclaw.operations.activity` is bounded
to counts and identifiers because an aggregate is visible to everyone who can reach the agent. A
pack is different in exactly the way that matters: it is scoped to *one conversation*, and a
rationale, a plan hash and an external reference are the substance of it rather than a leak.

**The scoping is the control, and this file used to only say so.** An earlier version of this
docstring claimed "the route to another person's session is `CurrentSession`'s ownership check, not
this tool" — but `CurrentSession` is a FastAPI dependency on `/sessions/{id}/…` routes and is not on
this path at all, while `session_id` is a plain argument the model can set to any value. Worse, the
ids were discoverable: `check_pending_requests` returned every open request in the deployment with
its `session_id` attached. So Alice could ask what was outstanding, read Bob's session id out of the
answer, and assemble Bob's every tool call, job rationale, approval and external effect. The gate is
now here, against the same `owner_permits` rule the routes use, and a session the caller does not
own is refused with the wording an unknown one gets — no existence leak, as at the front door.
"""

from chemclaw.agent.framing import defang
from chemclaw.agent.session_store import SessionOwnerStore, owner_permits
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.operations import assemble


async def _may_read(session_id: str) -> bool:
    """Whether this turn's actor may assemble a pack for a session that is not their own.

    The same `owner_permits` rule `/sessions/{id}/…` resolves, so the tool and the route cannot
    disagree about who owns a conversation. A session with no ownership row at all is refused under
    enforcement and allowed in dev, which is what `owner_permits` already decides for a row whose
    owner is absent — an unknown session and an owner-less one are the same claim about the record.
    """
    found, owner, _ = await SessionOwnerStore().lookup(session_id)
    if not found:
        return not settings.entra_required
    return owner_permits(owner, get_current_actor())


@tool
async def assemble_evidence_pack(session_id: str = "") -> dict[str, object]:
    """Assemble what this system recorded about a piece of work: calls, runs, decisions, changes.

    Use it when somebody asks how an answer or a change came about, what the system was permitted
    to do, who approved something, or what it changed outside itself. It reads five stores that
    have always held this and puts them side by side: every tool call with its outcome and actor,
    every durable run with the reason it was launched, every note proposed and how a human decided
    it, every plan approval, and every effect on a system this deployment does not own.

    **Report the `limits` it carries, verbatim, whenever you present it.** Three of them, and each
    corrects a reading somebody will otherwise make: the trail is append-only by database privilege
    and is *not* tamper-evidence; this is what this system did rather than the whole record of the
    decision; and an empty section means nothing was recorded, which is not the same as nothing
    having happened.

    Refusals are part of the record, not a list of faults — a gate refusing is the control
    operating, and an answer that presents them as failures misreads the pack.

    Args:
        session_id: Which conversation to assemble. Empty means this one, which is the usual case.

    Returns:
        The pack, its limits, and whether the record is empty for that session.
    """
    own = get_current_session_id() or ""
    target = session_id or own
    if not target:
        return {
            "empty": True,
            "reason": (
                "no conversation to assemble: this call is outside a session and none was named"
            ),
        }
    if target != own and not await _may_read(target):
        # The wording an unknown session gets, deliberately: telling a caller that a session exists
        # but is somebody else's confirms the id, which is the leak the front door's shared 404 rule
        # exists to prevent.
        return {
            "empty": True,
            "reason": f"no conversation {target!r} to assemble",
        }
    pack = await assemble(target)
    payload = pack.model_dump(mode="json")
    # A rationale and a job summary are text a person wrote and a tool returned; they reach the
    # model exactly as a retrieved chunk does. The rest of the pack is identifiers, outcomes and
    # timestamps from bounded vocabularies.
    for job in payload.get("jobs", []):
        job["rationale"] = defang(str(job.get("rationale", "")))
        job["summary"] = defang(str(job.get("summary", "")))
    payload["empty"] = pack.is_empty
    # A lower bound when the section was truncated, and said so rather than implied: a count of
    # refusals over a prefix reads as "there were none" to anyone who does not know about the cap,
    # which is exactly how a session whose refusals all fell after row 200 reported zero to an
    # auditor.
    payload["refusals"] = len(pack.refusals)
    if "tool_calls" in pack.truncated:
        payload["refusals_are_a_lower_bound"] = True
    return payload
