"""The evidence pack: what a decision rested on, assembled from what this system already stored.

**Assembly, not capture.** The audit trail, `job_records`, `plan_approvals`, `effects` and
`turn_costs` already record every tool call, durable run, approval, external change and how each
turn ended; this module is the read that puts them side by side. `turn_costs` (see `PackTurn`)
is what distinguishes a loop-capped or degraded session from a clean one. A note written inside
a turn shows as its `record_knowledge_note` call; its id is not recorded (stated in `LIMITS`).

The artefact is a **context-of-use record** (what was asked, what the system was permitted to
do, what evidence it used, what it changed, who approved it), which is what a regulated
deployment and an incident review both need. It is not tamper-evidence.

Three limits are carried on the object, in `limits`:

- **Append-only is a database privilege, not tamper-evidence.** A database owner could still
  rewrite the trail.
- **It is this system's record of its own work**, not the whole record of the decision.
- **A gap is not an absence of activity.** A window outside retention reads identically to one
  where nothing happened.
"""

from typing import Annotated, Any, TypeVar

from psycopg.rows import class_row
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import IsoStamp
from chemclaw.operations.activity import safe_tool_name

#: The sentences the pack refuses to let a reader supply for themselves. Carried on every pack.
LIMITS: tuple[str, ...] = (
    "The trail is append-only by database privilege, not by cryptography: the credential that "
    "writes it cannot rewrite it, and a database owner still could. This is not tamper-evidence "
    "and must never be described as it.",
    "This is what this system did, not the whole record of the decision. Conversations, meetings "
    "and a person's own judgement leave nothing here.",
    "An empty section means this system recorded nothing for it, which is not the same as nothing "
    "having happened — a window outside retention reads identically.",
    "A section named in `truncated` is a prefix, not the whole of it: this pack reads a bounded "
    "number of rows per store, oldest first. Read the counts as lower bounds there.",
    "A knowledge note written inside a turn appears here as the tool call that wrote it, not by "
    "its note id: nothing stores the id of a note against the session that wrote it. A note a "
    "durable run recorded does carry its id, on that job.",
    "A turn's `outcome` of 'unknown' means the row was written before the column existed "
    "(migration 060), not that the turn ended in some unknown way. And a capability bundle that "
    "was unreachable for a turn is not recorded anywhere: `turns` says the answer was cut short, "
    "never that a whole class of evidence was unavailable while it was produced.",
)


#: `audit_events.tool` is the model's raw string, so it is bounded on the field: no builder,
#: including `class_row`, can produce a model holding an unsanitised name.
SafeToolName = Annotated[str, BeforeValidator(lambda value: safe_tool_name(str(value)))]


class ToolCall(BaseModel):
    """One recorded call: what ran, how it ended, and how long it took."""

    #: `extra="forbid"`: built by `class_row` from a SELECT, so a column with no field is an error
    #: naming it rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    tool: SafeToolName
    outcome: str
    actor: str
    at: IsoStamp
    latency_ms: float = 0.0
    #: Why the call did not run, for a refusal. The gates working are part of the record.
    #: Restricted to refusals in the SELECT rather than after it — see `assemble`.
    detail: str = ""


class PackJob(BaseModel):
    """One durable run this conversation launched, why it was asked for, and how it ended.

    **`state` and `failure_reason` are here because a pack that omitted them presented a failed
    computation as evidence.** `connector_job.py` leaves `summary` empty on failure and puts the
    reason in `failure_reason`, so a run that died on an unknown solvent appeared as a job that ran,
    returned nothing, and completed — indistinguishable from a successful run with no summary, in
    the one document whose stated purpose is "what evidence it used". Migration 061 exists because
    "all my CREST jobs are failing" and "nobody is running jobs" were the same picture in this
    table; the pack reproduced that.
    """

    #: `extra="forbid"`: built by `class_row` from a SELECT, so a column with no field is an error
    #: naming it rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    job_id: str
    connector: str
    job: str
    rationale: str
    requested_by: str
    summary: str
    state: str = ""
    failure_reason: str = ""
    note_id: str = ""
    completed_at: IsoStamp = ""


class PackEffect(BaseModel):
    """One change made in a system this deployment does not own."""

    #: `extra="forbid"`: built by `class_row` from a SELECT, so a column with no field is an error
    #: naming it rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    effect_id: str
    system: str
    job: str
    reversal: str
    state: str
    approved_by: str = ""
    external_ref: str = ""
    attempted_at: IsoStamp = ""


class PackTurn(BaseModel):
    """One turn of the conversation, and **how it ended**.

    The section this pack did not have, and its absence was the pack's own theme turned on itself.
    Measured on two sessions, one loop-capped with the durable tier dark and one clean: the
    assembled packs were byte-identical once the timestamp and the latency were scrubbed. Four
    stores answer *what ran*; none answers *whether the turn that ran it finished*. A pack whose
    stated purpose is "what was asked, what the system was permitted to do, what evidence it used"
    presented a truncated turn's evidence as a completed turn's, in the one document a reader is
    told to treat as the record.

    `turn_costs` is where that has always been: `outcome` (migration 060), `compacted` and
    `context_unreducible` (069), `answer_confidence` and `review_required` (082-083). Only the
    columns that qualify the *answer* are read; the token counts are `operations.activity.spend`'s
    question and would make this a billing document.

    **`outcome='unknown'` means "written before migration 060", not "ended in some unknown way"**,
    which is the column's own documented default and the one value a reader must not take at face
    value.
    """

    #: `extra="forbid"`: built by `class_row` from a SELECT, so a column with no field is an error
    #: naming it rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    outcome: str
    completed: bool = True
    at: IsoStamp = ""
    compacted: bool = False
    context_unreducible: bool = False
    answer_confidence: float | None = None
    review_required: bool = False
    error_code: str = ""

    @property
    def degraded(self) -> bool:
        """Whether this turn's answer was produced with something missing or cut short.

        `compacted` is not degradation (it is the policy working); `context_unreducible` is, because
        the request could not be brought under budget.
        """
        return (
            self.outcome not in ("answered", "unknown")
            or not self.completed
            or self.context_unreducible
            or self.review_required
        )


class PackApproval(BaseModel):
    """One plan a human approved or refused, bound to the plan they were shown."""

    #: `extra="forbid"`: built by `class_row` from a SELECT, so a column with no field is an error
    #: naming it rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    plan_hash: str
    approved: bool
    actor: str
    at: IsoStamp = ""


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
    effects: list[PackEffect] = Field(default_factory=list)
    approvals: list[PackApproval] = Field(default_factory=list)
    #: How each of this session's turns ended — see `PackTurn` for why a pack without it
    #: presented a truncated turn's evidence as a completed turn's.
    turns: list[PackTurn] = Field(default_factory=list)
    limits: tuple[str, ...] = LIMITS
    #: Sections that hit the per-store row cap, so what is shown is a prefix, not the whole record.
    #: Without this a capped section reads as complete, and the dropped rows are the most recent.
    truncated: tuple[str, ...] = ()

    @property
    def refusals(self) -> list[ToolCall]:
        """The calls a gate stopped.

        A property rather than a separate section: a refusal is the control operating, part of the
        work rather than a failure to report apart.
        """
        return [call for call in self.tool_calls if call.outcome == "refused"]

    @property
    def degraded_turns(self) -> list[PackTurn]:
        """The turns whose answer was cut short or flagged: the pack's own headline.

        A degraded turn is the record saying its own evidence is partial, so a reader who checks
        nothing else must be able to check this.
        """
        return [turn for turn in self.turns if turn.degraded]

    @property
    def is_empty(self) -> bool:
        """Whether this system recorded anything at all for this session.

        Check before presenting a pack: empty is a statement about the record, not the work.
        `approvals` and `turns` count because an abandoned turn may leave only a `plan_approvals` or
        a `turn_costs` row.
        """
        return not (self.tool_calls or self.jobs or self.effects or self.approvals or self.turns)


_Section = TypeVar("_Section", bound=BaseModel)


async def _section(model: type[_Section], sql: str, params: tuple[Any, ...]) -> list[_Section]:
    """Every row the query returned, already the model it belongs to.

    `class_row` binds each selected column by name, so the SELECT list and the model are one
    declaration and a mismatched column is a `ValidationError` at the read, never a silent swap.

    Raises:
        pydantic.ValidationError: A SELECT and its model no longer describe the same row.
            Deliberately uncaught: a partially assembled pack must not be returned, since nothing in
            it could say that a section failed to build.
    """
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        async with conn.cursor(row_factory=class_row(model)) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def assemble(session_id: str, *, limit: int = 200) -> EvidencePack:
    """Build the pack for one session from the five stores that already hold it.

    Five reads rather than one join: the stores are independent with different retention rules, and
    a join would silently drop a row whose partner had been disposed of.
    """
    calls = await _section(
        ToolCall,
        # `detail` is narrowed to refusals in the statement, where the restriction is visible and
        # cannot be lost. `ts AS at` because `class_row` binds by name.
        "SELECT tool, outcome, actor, ts AS at, latency_ms, "
        "CASE WHEN outcome = 'refused' THEN detail ELSE '' END AS detail "
        "FROM audit_events WHERE session_id = %s ORDER BY ts LIMIT %s",
        (session_id, limit),
    )
    jobs = await _section(
        PackJob,
        "SELECT job_id, connector, job, rationale, requested_by, summary, state, "
        "failure_reason, note_id, completed_at FROM job_records WHERE session_id = %s "
        "ORDER BY completed_at LIMIT %s",
        (session_id, limit),
    )
    effects = await _section(
        PackEffect,
        "SELECT effect_id, system, job, reversal, state, approved_by, external_ref, "
        "attempted_at FROM effects WHERE session_id = %s ORDER BY attempted_at LIMIT %s",
        (session_id, limit),
    )
    approvals = await _section(
        PackApproval,
        "SELECT plan_hash, approved, actor, decided_at AS at FROM plan_approvals "
        "WHERE session_id = %s ORDER BY decided_at LIMIT %s",
        (session_id, limit),
    )
    turns = await _section(
        PackTurn,
        # `COALESCE`: the column is nullable for rows written before it existed; NULL reads as
        # False.
        "SELECT correlation_id, outcome, completed, recorded_at AS at, compacted, "
        "context_unreducible, answer_confidence, COALESCE(review_required, false) AS "
        "review_required, error_code "
        "FROM turn_costs WHERE session_id = %s ORDER BY recorded_at LIMIT %s",
        (session_id, limit),
    )
    # A section that came back exactly full is a section that may have more behind it. Reported
    # rather than inferred by the caller, because the caller cannot see `limit`.
    sections: list[tuple[str, int]] = [
        ("tool_calls", len(calls)),
        ("jobs", len(jobs)),
        ("effects", len(effects)),
        ("approvals", len(approvals)),
        ("turns", len(turns)),
    ]
    return EvidencePack(
        session_id=session_id,
        tool_calls=calls,
        jobs=jobs,
        effects=effects,
        approvals=approvals,
        turns=turns,
        truncated=tuple(name for name, count in sections if count >= limit),
    )
