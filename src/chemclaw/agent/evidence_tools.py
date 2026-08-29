"""The agent tool over the evidence pack — how a piece of work came to be, from the record.

**A read, and the only one here that returns free text.** `chemclaw.operations.activity` is bounded
to counts and identifiers because an aggregate is visible to everyone who can reach the agent. A
pack is different in exactly the way that matters: it is scoped to *one conversation*, and a
rationale, a plan hash and an external reference are the substance of it rather than a leak. The
scoping is the control — this returns one session's own record, and the route to another person's
session is `CurrentSession`'s ownership check, not this tool.
"""

from chemclaw.agent.framing import defang
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.operations import assemble


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
    target = session_id or get_current_session_id() or ""
    if not target:
        return {
            "empty": True,
            "reason": (
                "no conversation to assemble: this call is outside a session and none was named"
            ),
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
    payload["refusals"] = len(pack.refusals)
    return payload
