"""The evidence pack: what a decision rested on, assembled from what this system already stored.

**Assembly, not capture.** Every component of this exists and has since it was written — the audit
trail records every tool call with its actor, outcome and latency; `job_records` records what a
durable run was asked for and what it returned, including the note it recorded; `plan_approvals`
records who approved a plan and which one; `effects` records what was changed outside this
deployment and who approved it; `turn_costs` records how each turn *ended*. Nothing here is new
capture. What was missing is the *read* that puts them beside each other.

**The fifth read is the newest and its absence was this pack's own theme turned on itself.** Four
stores answer "what ran" and none answered "did the turn that ran it finish", so a session that
was loop-capped with the durable tier dark assembled a pack byte-identical to a clean one's once
the timestamp and the latency were scrubbed — measured, in the module whose stated purpose is a
record of "what evidence it used". See `PackTurn`.

**There used to be a fifth section and there is not.** `proposals` read `note_proposals` — the
note the agent submitted to the PR-gate and who decided it. That gate is gone
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`): nothing writes that table, so the
section would have been permanently empty on any session recorded since, which under this pack's
own `limits` reads as "this system recorded nothing" rather than as "this reading is dead". What a
pack shows instead is the `record_knowledge_note` call in `tool_calls` with its actor, outcome and
timestamp, and `PackJob.note_id` for a note a durable run recorded. The note *id* of a note written
inside a turn is the one thing lost, and it is named in `LIMITS` rather than left to be noticed.

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


#: `audit_events.tool` is the model's own string rather than a registered name, so it is bounded
#: **on the field** rather than at one of its two readers. That placement is the point: the note on
#: `assemble` used to record that the sanitisation "went into one reader of this column and not its
#: sibling in the same package", and a bound applied to one reader is not a bound. Now the model
#: cannot hold an unsanitised name, whoever builds it — including `class_row`, which builds one
#: straight out of a row and calls no code of this module's on the way.
SafeToolName = Annotated[str, BeforeValidator(lambda value: safe_tool_name(str(value)))]


class ToolCall(BaseModel):
    """One recorded call: what ran, how it ended, and how long it took."""

    #: `extra="forbid"` because this model is built by `class_row` straight out of a SELECT: with
    #: pydantic's default a column nobody added a field for is *ignored*, so the read that was
    #: supposed to catch the SELECT and the model drifting apart would silently drop it. Forbidding
    #: makes the drift an error naming the column, which is the whole reason for the row factory.
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

    #: `extra="forbid"` because this model is built by `class_row` straight out of a SELECT: with
    #: pydantic's default a column nobody added a field for is *ignored*, so the read that was
    #: supposed to catch the SELECT and the model drifting apart would silently drop it. Forbidding
    #: makes the drift an error naming the column, which is the whole reason for the row factory.
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

    #: `extra="forbid"` because this model is built by `class_row` straight out of a SELECT: with
    #: pydantic's default a column nobody added a field for is *ignored*, so the read that was
    #: supposed to catch the SELECT and the model drifting apart would silently drop it. Forbidding
    #: makes the drift an error naming the column, which is the whole reason for the row factory.
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

    #: `extra="forbid"` because this model is built by `class_row` straight out of a SELECT: with
    #: pydantic's default a column nobody added a field for is *ignored*, so the read that was
    #: supposed to catch the SELECT and the model drifting apart would silently drop it. Forbidding
    #: makes the drift an error naming the column, which is the whole reason for the row factory.
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

        `compacted` is deliberately not here: compaction is the policy working as designed on a
        long thread and says nothing about the answer's completeness. `context_unreducible` is,
        because it means the policy could not get the request under its budget.
        """
        return (
            self.outcome not in ("answered", "unknown")
            or not self.completed
            or self.context_unreducible
            or self.review_required
        )


class PackApproval(BaseModel):
    """One plan a human approved or refused, bound to the plan they were shown."""

    #: `extra="forbid"` because this model is built by `class_row` straight out of a SELECT: with
    #: pydantic's default a column nobody added a field for is *ignored*, so the read that was
    #: supposed to catch the SELECT and the model drifting apart would silently drop it. Forbidding
    #: makes the drift an error naming the column, which is the whole reason for the row factory.
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
    #: Sections that hit the per-store row cap, so what is shown is a prefix rather than the whole
    #: record. **Silence here was the defect**: every read is `ORDER BY <ts> LIMIT 200` and nothing
    #: said so, while `limits` trains a reader to treat a *non-empty* section as complete. A session
    #: with 260 calls whose refusals all came after #200 reported zero refusals to an auditor, and
    #: because effects are ordered oldest-first the rows dropped were the most recent — the ones an
    #: incident is actually about.
    truncated: tuple[str, ...] = ()

    @property
    def refusals(self) -> list[ToolCall]:
        """The calls a gate stopped.

        Surfaced as a property rather than a separate section because they are not a failure mode
        to be reported apart from the work: a gate refusing is the control operating, and a pack
        that filed refusals elsewhere would read as a list of things that went wrong.
        """
        return [call for call in self.tool_calls if call.outcome == "refused"]

    @property
    def degraded_turns(self) -> list[PackTurn]:
        """The turns whose answer was cut short or flagged — the pack's own headline.

        A property beside `refusals` and for the mirror-image reason: a refusal is a control
        operating and belongs in the record, while a degraded turn is the record saying its own
        evidence is partial. A reader who checks nothing else must be able to check this.
        """
        return [turn for turn in self.turns if turn.degraded]

    @property
    def is_empty(self) -> bool:
        """Whether this system recorded anything at all for this session.

        The one thing a caller must check before presenting a pack: an empty pack is a statement
        about the record, not about the work.

        `approvals` counts, and its omission was a real hole: a chemist who approved a plan and then
        abandoned the turn left a row in `plan_approvals` and nothing in the other four, so the pack
        reported "nothing recorded" for a session in which a human authorized spending.

        `turns` counts for the same reason one step further out: a turn that spent tokens and was
        then abandoned books a `turn_costs` row and nothing else at all — no audit row, no job, no
        approval — so without it the pack still reported "nothing recorded" for a session that
        demonstrably ran.
        """
        return not (self.tool_calls or self.jobs or self.effects or self.approvals or self.turns)


_Section = TypeVar("_Section", bound=BaseModel)


async def _section(model: type[_Section], sql: str, params: tuple[Any, ...]) -> list[_Section]:
    """Every row the query returned, already the model it belongs to.

    **The section models are built by name, which is what the five comprehensions this replaced
    could not do.** They unpacked tuples of up to ten elements — nine of `PackJob`'s ten adjacent
    and all `TEXT` — so the SELECT list's order was restated a second time in Python and reordering
    the SELECT swapped fields silently, type-checked, and produced a plausible-looking pack. That
    is the fault this whole module exists to be trusted against. `class_row` passes each selected
    column as a keyword argument, so the SELECT list and the model are one declaration: a column
    whose name is not a field is a `ValidationError` naming it, at the read.

    Raises:
        pydantic.ValidationError: A SELECT here and its model have stopped describing the same row.
            Deliberately uncaught: a pack that cannot be assembled must not be returned partially
            assembled, because `is_empty` and `truncated` are how a reader is told what the pack
            does *not* say, and neither can express "this section failed to build".
    """
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        async with conn.cursor(row_factory=class_row(model)) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def assemble(session_id: str, *, limit: int = 200) -> EvidencePack:
    """Build the pack for one session from the five stores that already hold it.

    Five reads rather than one join: the stores are independent by design — an effect is recorded
    whether or not a job ran, and a job record survives the session's messages being pruned — and a
    join would silently drop a row whose partner had been disposed of under a different retention
    rule.
    """
    calls = await _section(
        ToolCall,
        # **`detail` is narrowed to refusals in the statement rather than after it.** It used to be
        # blanked in the comprehension, and a comprehension is exactly what a row factory removes;
        # a `CASE` keeps the restriction where it cannot be lost and makes it visible to anyone
        # reading the query. `ts AS at` because `class_row` binds by name and the model's field is
        # `at` — the column keeps the name the trail gave it.
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
        # `COALESCE(review_required, false)`: migration 082 added the column nullable, and a row
        # written before it holds NULL where `PackTurn.review_required` is a `bool`. The
        # comprehension this replaced coerced it with `bool(...)`, which read NULL as False — the
        # same answer, and this is where it now has to be said out loud.
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
