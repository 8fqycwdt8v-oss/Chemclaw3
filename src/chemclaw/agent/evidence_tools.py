"""The agent tool over the evidence pack — how a piece of work came to be, from the record.

A pack is scoped to one conversation and returns free text (rationales, plan hashes, external
references). `session_id` is model-controlled, so this tool enforces the same participant rule as
the `/sessions/{id}` routes, and refuses a foreign session with the unknown-session wording.
"""

from chemclaw.agent.framing import defang
from chemclaw.agent.session_members import participant_permits
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.operations import assemble


async def _may_read(session_id: str) -> bool:
    """Whether this turn's actor may assemble a pack for a session that is not their own.

    Same rule as the `/sessions/{id}/…` routes: the owner or an admitted member. A session with no
    ownership row is refused under enforcement and allowed in dev.
    """
    found, owner, _ = await SessionOwnerStore().lookup(session_id)
    if not found:
        return not settings.entra_required
    return await participant_permits(session_id, owner, get_current_actor())


@tool
async def assemble_evidence_pack(session_id: str = "") -> dict[str, object]:
    """Assemble what this system recorded about a piece of work: calls, runs, decisions, changes.

    Use it when somebody asks how an answer or a change came about, what the system was permitted
    to do, who approved something, or what it changed outside itself. It reads five stores that
    have always held this and puts them side by side: every tool call with its outcome and actor,
    every durable run with the reason it was launched, every plan approval, every effect on a
    system this deployment does not own, and how each of the session's turns ended.

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
        # Same wording as an unknown session: confirming the id exists would leak it.
        return {
            "empty": True,
            "reason": f"no conversation {target!r} to assemble",
        }
    pack = await assemble(target)
    payload = pack.model_dump(mode="json")
    # Rationales, summaries, failure reasons and external refs are foreign text and reach the model
    # like a retrieved chunk, so they are defanged. `ToolCall.detail` is excluded: it is only ever
    # this system's own refusal wording.
    for job in payload.get("jobs", []):
        job["rationale"] = defang(str(job.get("rationale", "")))
        job["summary"] = defang(str(job.get("summary", "")))
        job["failure_reason"] = defang(str(job.get("failure_reason", "")))
    for effect in payload.get("effects", []):
        effect["external_ref"] = defang(str(effect.get("external_ref", "")))
    payload["empty"] = pack.is_empty
    # A lower bound when the section was truncated, flagged so a capped count never reads as "none".
    payload["refusals"] = len(pack.refusals)
    if "tool_calls" in pack.truncated:
        payload["refusals_are_a_lower_bound"] = True
    # Surface degraded turns as correlation ids pointing into `payload["turns"]`, so a degraded
    # answer
    # never reads as complete.
    payload["degraded_turns"] = [turn.correlation_id for turn in pack.degraded_turns]
    return payload
