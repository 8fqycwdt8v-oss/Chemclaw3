"""The evidence pack: what a decision rested on, assembled from what this system already stored.

**Assembly, not capture.** Every component of this exists and has since it was written — the audit
trail records every tool call with its actor, outcome and latency; `job_records` records what a
durable run was asked for and what it returned; `note_proposals` records what the agent proposed
and who decided; `plan_approvals` records who approved a plan and which one; `effects` records what
was changed outside this deployment and who approved it. Nothing here is new capture. What was
missing is the *read* that puts them beside each other.

## Why this and not the hash chain

`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` removed the
audit hash chain, and that reasoning is sound for layer 1 and is not revisited here. What changed is
what the record is *asked for*. Once this system acts on a system it does not own
(`D-2026-08-29-an-effect-declares-whether-it-can-be-undone`) and once a computed value is published
into a scientific record (`D-2026-08-25-a-cache-is-not-a-record`), the artefact somebody needs is
not tamper-evidence — it is a **context-of-use record**: what was asked, what the system was
permitted to do, what evidence it used, what it changed, and who approved it.

That is what the FDA's seven-step credibility framework and the January 2026 FDA–EMA good-practice
principles ask a sponsor to be able to produce, and it is what a procurement conversation about
agentic AI in a regulated setting turns on. It is also, usefully, the same thing an engineer wants
after an incident.

## What it does not claim

Three limits are carried **on the object**, in `limits`, rather than left for a reader to know:

- **Append-only is a database privilege, not tamper-evidence.** The credential that writes the trail
  cannot rewrite it; a database owner still could. The system prompt already says this to chemists
  and the pack must not say less.
- **It is this system's record of its own work**, not the whole record of the decision. A
  conversation, a meeting and a colleague's judgement leave nothing here.
- **A gap is not an absence of activity.** A window outside retention, or a session that predates a
  field, reads identically to one where nothing happened — which is the same distinction
  `Coverage` exists to make one module over.
"""

from typing import Any

from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings

#: The sentences the pack refuses to let a reader supply for themselves. Carried on every pack.
LIMITS: tuple[str, ...] = (
    "The trail is append-only by database privilege, not by cryptography: the credential that "
    "writes it cannot rewrite it, and a database owner still could. This is not tamper-evidence "
    "and must never be described as it.",
    "This is what this system did, not the whole record of the decision. Conversations, meetings "
    "and a person's own judgement leave nothing here.",
    "An empty section means this system recorded nothing for it, which is not the same as nothing "
    "having happened — a window outside retention reads identically.",
)


class ToolCall(BaseModel):
    """One recorded call: what ran, how it ended, and how long it took."""

    tool: str
    outcome: str
    actor: str
    at: str
    latency_ms: float = 0.0
    #: Why the call did not run, for a refusal. The gates working are part of the record.
    detail: str = ""


class PackJob(BaseModel):
    """One durable run this conversation launched, and why it was asked for."""

    job_id: str
    connector: str
    job: str
    rationale: str
    requested_by: str
    summary: str
    note_id: str = ""
    completed_at: str = ""


class PackProposal(BaseModel):
    """One knowledge note the agent proposed, and what a human decided about it."""

    note_id: str
    note_type: str
    state: str
    actor: str
    decided_by: str = ""
    decided_at: str = ""


class PackEffect(BaseModel):
    """One change made in a system this deployment does not own."""

    effect_id: str
    system: str
    job: str
    reversal: str
    state: str
    approved_by: str = ""
    external_ref: str = ""
    attempted_at: str = ""


class PackApproval(BaseModel):
    """One plan a human approved or refused, bound to the plan they were shown."""

    plan_hash: str
    approved: bool
    actor: str
    at: str = ""


class EvidencePack(BaseModel):
    """Everything this system recorded about one conversation, in one object.

    Keyed by session rather than by result, because that is the unit a person asks about: "how did
    we arrive at this" is a question about a piece of work, and a result-keyed pack would have to
    guess which calls contributed to which number — an inference this system does not make anywhere
    else and must not start making here.
    """

    session_id: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    jobs: list[PackJob] = Field(default_factory=list)
    proposals: list[PackProposal] = Field(default_factory=list)
    effects: list[PackEffect] = Field(default_factory=list)
    approvals: list[PackApproval] = Field(default_factory=list)
    limits: tuple[str, ...] = LIMITS

    @property
    def refusals(self) -> list[ToolCall]:
        """The calls a gate stopped.

        Surfaced as a property rather than a separate section because they are not a failure mode
        to be reported apart from the work: a gate refusing is the control operating, and a pack
        that filed refusals elsewhere would read as a list of things that went wrong.
        """
        return [call for call in self.tool_calls if call.outcome == "refused"]

    @property
    def is_empty(self) -> bool:
        """Whether this system recorded anything at all for this session.

        The one thing a caller must check before presenting a pack: an empty pack is a statement
        about the record, not about the work.
        """
        return not (self.tool_calls or self.jobs or self.proposals or self.effects)


async def _rows(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """Every row the query returned, as plain tuples."""
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return [tuple(row) for row in await cur.fetchall()]


def _stamp(value: Any) -> str:
    """An ISO timestamp, or '' when the column was NULL."""
    return value.isoformat() if value is not None else ""


async def assemble(session_id: str, *, limit: int = 200) -> EvidencePack:
    """Build the pack for one session from the five stores that already hold it.

    Five reads rather than one join: the stores are independent by design — an effect is recorded
    whether or not a note was proposed, and a proposal survives the session's messages being pruned
    — and a join would silently drop a row whose partner had been disposed of under a different
    retention rule.
    """
    calls = [
        ToolCall(
            tool=str(tool),
            outcome=str(outcome),
            actor=str(actor),
            at=_stamp(ts),
            latency_ms=float(latency or 0.0),
            detail=str(detail or "") if str(outcome) == "refused" else "",
        )
        for tool, outcome, actor, ts, latency, detail in await _rows(
            "SELECT tool, outcome, actor, ts, latency_ms, detail FROM audit_events "
            "WHERE session_id = %s ORDER BY ts LIMIT %s",
            (session_id, limit),
        )
    ]
    jobs = [
        PackJob(
            job_id=str(job_id),
            connector=str(connector),
            job=str(job),
            rationale=str(rationale),
            requested_by=str(requested_by),
            summary=str(summary),
            note_id=str(note_id or ""),
            completed_at=_stamp(completed_at),
        )
        for job_id, connector, job, rationale, requested_by, summary, note_id, completed_at in (
            await _rows(
                "SELECT job_id, connector, job, rationale, requested_by, summary, note_id, "
                "completed_at FROM job_records WHERE session_id = %s "
                "ORDER BY completed_at LIMIT %s",
                (session_id, limit),
            )
        )
    ]
    proposals = [
        PackProposal(
            note_id=str(note_id),
            note_type=str(note_type),
            state=str(state),
            actor=str(actor),
            decided_by=str(decided_by or ""),
            decided_at=_stamp(decided_at),
        )
        for note_id, note_type, state, actor, decided_by, decided_at in await _rows(
            "SELECT note_id, note_type, state, actor, decided_by, decided_at FROM note_proposals "
            "WHERE session_id = %s ORDER BY submitted_at LIMIT %s",
            (session_id, limit),
        )
    ]
    effects = [
        PackEffect(
            effect_id=str(effect_id),
            system=str(system),
            job=str(job),
            reversal=str(reversal),
            state=str(state),
            approved_by=str(approved_by or ""),
            external_ref=str(external_ref or ""),
            attempted_at=_stamp(attempted_at),
        )
        for effect_id, system, job, reversal, state, approved_by, external_ref, attempted_at in (
            await _rows(
                "SELECT effect_id, system, job, reversal, state, approved_by, external_ref, "
                "attempted_at FROM effects WHERE session_id = %s ORDER BY attempted_at LIMIT %s",
                (session_id, limit),
            )
        )
    ]
    approvals = [
        PackApproval(
            plan_hash=str(plan_hash), approved=bool(approved), actor=str(actor), at=_stamp(at)
        )
        for plan_hash, approved, actor, at in await _rows(
            "SELECT plan_hash, approved, actor, decided_at FROM plan_approvals "
            "WHERE session_id = %s ORDER BY decided_at LIMIT %s",
            (session_id, limit),
        )
    ]
    return EvidencePack(
        session_id=session_id,
        tool_calls=calls,
        jobs=jobs,
        proposals=proposals,
        effects=effects,
        approvals=approvals,
    )
