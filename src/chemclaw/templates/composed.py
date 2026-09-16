"""Workflows the agent composed at run time — and the rule that makes storing one safe.

**The rule first, because the table is not what makes this safe.** A template's `agent` step is
exempt from the plan gate, and `D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only`
argues that exemption from a premise: the file was authored by a person, committed to git and
reviewed, and *"nothing at run time can produce one"*. This module produces one at run time. So the
premise has to be restored some other way, and `authored_problems` is that way:

**An agent-authored workflow may name no side-effecting tool, no durable job, and no
`write_tools`.**

The exemption is about *writes*. A document that cannot contain one never reaches the question, so
nothing about the plan gate changes and a hand-written template in `data/templates/` keeps every
capability it had. The alternative — re-attaching the plan gate to an agent-authored template's
steps — was considered and is not a control but an outage: a template step runs in an activity with
no session, so `enforce_plan_approval` would refuse every write in it for want of a plan nobody can
approve, which is a different way of saying no writes with more moving parts.

**Checked when it is stored and again when it is run**, which is not belt-and-braces for its own
sake. The two happen at different times against possibly different deployments, and
`side_effecting_tools()` grows: a bundle enabled between composing and running can turn a name that
was a read into a write, and the run-time check is what notices.

**What it costs is stated rather than discovered.** A composed workflow cannot rank species, run a
conformer search or write a knowledge note — every durable job is state-changing by classification,
so the expensive computation these procedures exist to sequence is exactly what an *agent-authored*
one may not contain. It composes reads: enumerate, look up, retrieve, screen, then reason over the
results in an `agent` step. A procedure that needs a calculation is a template a person writes,
which is the same answer `skills/` gives for judgment that must be reviewed.

Keyed by `(owner, name)`. A composed workflow is one chemist's working procedure, not a
deployment's catalogue, and resolving against the caller's own rows is what stops a name reaching
somebody else's steps.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.jsonb import json_column
from chemclaw.templates.manifest import AgentStep, JobStep, Template, ToolStep

#: How many workflows one owner may keep. A bound rather than a policy: the listing is read into a
#: tool result, and an unbounded one is the shape `agent/tool_result_size.py` exists to cut.
MAX_PER_OWNER = 50


class ComposedWorkflowError(ChemclawError):
    """A composed workflow could not be stored or could not be run."""


class ComposedWorkflow(BaseModel):
    """One stored workflow: who composed it, what it is, and the document that runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)
    summary: str = ""
    document: Template
    # What a person approved, as a `template_fingerprint` of the document they were shown, or `""`
    # when nobody has. Compared rather than trusted: `unapproved_jobs` asks whether it still matches
    # *this* document, so re-composing lapses the approval without anything having to clear it.
    approved_fingerprint: str = ""
    # The person. Never the agent — nothing the model can call writes this, and
    # `tests/test_composed_workflows.py` asserts that absence rather than trusting it.
    approved_by: str = ""


def authored_problems(template: Template, side_effecting: frozenset[str]) -> list[str]:
    """Why this document may not be run as an agent-authored workflow **at all**, or `[]`.

    The refusals no approval lifts. One function so the write path and the run path ask the same
    question and cannot drift apart; `unapproved_jobs` below holds the one that a human *can* lift,
    and the split between the two files is the whole security argument of this module.

    Two refusals here, and each closes a distinct way the plan gate could be bypassed:

    - a **`tool` step naming a side-effecting tool**, which is the sharp one. A `tool` step runs
      through `invoke_governed` under the requester's identity, so `enforce_tool_authz` decides —
      but the plan gate's own early return (*"No session means no plan to approve"*) lets it past,
      correctly for a human-authored procedure and not for one the agent wrote a moment ago.
    - **`write_tools`**, which `step_profile` would otherwise restore into the step's graph. This
      is the one the exemption's own wording is about.

    **Why a human's approval lifts the `job` step and not these two**, which is the line this
    widening is drawn on rather than a limit nobody argued. A `job` step is *bounded compute whose
    call the approver read*: the job's name and its arguments are in the document they approved, and
    running it produces a result. `write_tools` is not a call at all — it is a permission handed to
    a model turn, spent later on a call nobody has seen, chosen by the model inside the step. A
    person can meaningfully approve the first and cannot meaningfully approve the second, so no
    approval offers to. A side-effecting `tool` step sits nearer the first and stays refused with
    it, because the reachable set is every write in the tree and the case for widening it has not
    been made — `docs/planning/BACKLOG.md` carries that as its own question rather than smuggling
    it in beside the one that was asked.

    An `agent` step is fine and is the point: its surface is already narrowed to reads by
    `step_profile` when it declares no writes, so the reasoning inside a step stays free while the
    procedure around it cannot act.

    Args:
        template: The document to check.
        side_effecting: This deployment's `authz.side_effecting_tools()`. Passed rather than
            imported, because `templates` may not import `agent` (`tests/test_layering.py`) — and
            because passing it is what lets the run-time check use the set the *run's* deployment
            has, which is not necessarily the one the compose-time check saw.

    Returns:
        One line per problem, empty when nothing here refuses the document.
    """
    problems: list[str] = []
    for step in template.steps:
        if isinstance(step, ToolStep) and step.tool in side_effecting:
            problems.append(
                f"step {step.id!r} calls {step.tool!r}, which changes something. A workflow you "
                "composed yourself may only read: it runs with no session to approve a plan in, "
                "so nothing can put a person in front of that call. Ask for the change directly "
                "in the conversation instead, where the plan gate applies."
            )
        elif isinstance(step, AgentStep) and step.write_tools:
            problems.append(
                f"step {step.id!r} declares write tools {sorted(step.write_tools)}. Only a "
                "reviewed template in `data/templates/` may declare those; a workflow you "
                "composed may not hand a model a write to spend."
            )
    return problems


def job_steps(template: Template) -> list[str]:
    """The ids of every `job` step in `template`, in declared order.

    Separate from the refusal below so a caller that wants to *describe* what an approval would
    authorize — the route that shows a person what they are approving — asks the same question the
    enforcement asks, rather than re-deriving it from the document a second way.
    """
    return [step.id for step in template.steps if isinstance(step, JobStep)]


def unapproved_jobs(template: Template, approved_fingerprint: str, fingerprint: str) -> list[str]:
    """Why this document's durable jobs may not run yet, or `[]`.

    **The refusal a human can lift, and the only one.** A composed workflow may contain `job` steps
    and may be stored with them; what it may not do is *run* them until a person has approved this
    exact document. Composing is therefore not the decision — which is deliberate, because a
    workflow nobody may compose is a workflow nobody can put in front of a person to approve.

    **One version of one workflow, not an actor.** The approval is keyed on the document's own hash
    (`durable/template_job.template_fingerprint`), so re-composing lapses it with no clearing logic
    to forget: the stored fingerprint simply stops matching. A standing *per-actor* permission —
    the broader reading, and the phrase this feature was asked for in — never lapses, so a workflow
    re-composed into something else would inherit the approval granted to what it used to be.

    Args:
        template: The document about to run.
        approved_fingerprint: What a person approved, or `""` when nobody has.
        fingerprint: This document's hash now. Passed rather than computed, because
            `templates` may not import `durable` and because the caller already holds it.

    Returns:
        One line naming the jobs and how to authorize them, or `[]`.
    """
    jobs = job_steps(template)
    if not jobs or approved_fingerprint == fingerprint:
        return []
    return [
        f"it launches the durable job(s) at step(s) {jobs}, and a job costs real compute on a "
        "procedure nobody has reviewed. Ask the chemist who owns this workflow to approve it — "
        "`POST /workflows/{name}/approval` on the front door, which only a person can call — and "
        + (
            "it will run then."
            if not approved_fingerprint
            else "approve it again: the approval on file is for an earlier version of these steps."
        )
    ]


@runtime_checkable
class ComposedStore(Protocol):
    """Where composed workflows live. Two real backends, chosen by the session store's switch."""

    async def save(self, workflow: ComposedWorkflow) -> None:
        """Store `workflow`, replacing this owner's workflow of the same name."""
        ...

    async def get(self, owner: str, name: str) -> ComposedWorkflow | None:
        """One of `owner`'s workflows by name, or `None`."""
        ...

    async def list_for(self, owner: str) -> Sequence[ComposedWorkflow]:
        """Every one of `owner`'s workflows, most recently changed first."""
        ...

    async def approve(self, owner: str, name: str, fingerprint: str, approver: str) -> bool:
        """Record that `approver` approved this exact version; False when the row is gone."""
        ...


class InMemoryComposedStore:
    """The store a deployment with no Postgres gets.

    Not a test double, for the reason `InMemoryPlanApprovalStore` says it is not one: a CLI session
    and a dev process are real deployments, and a composed workflow that vanishes with the process
    is the honest behaviour there rather than a failure to configure something.
    """

    def __init__(self) -> None:
        """Start empty, keyed the way the table is."""
        self._rows: dict[tuple[str, str], ComposedWorkflow] = {}
        self._order: list[tuple[str, str]] = []

    async def save(self, workflow: ComposedWorkflow) -> None:
        """Store `workflow`, newest-first in the listing."""
        key = (workflow.owner, workflow.name)
        self._rows[key] = workflow
        if key in self._order:
            self._order.remove(key)
        self._order.insert(0, key)

    async def get(self, owner: str, name: str) -> ComposedWorkflow | None:
        """One workflow, or `None`."""
        return self._rows.get((owner, name))

    async def list_for(self, owner: str) -> Sequence[ComposedWorkflow]:
        """This owner's workflows, most recently saved first."""
        return [self._rows[key] for key in self._order if key[0] == owner]

    async def approve(self, owner: str, name: str, fingerprint: str, approver: str) -> bool:
        """Stamp the approval onto the stored row."""
        row = self._rows.get((owner, name))
        if row is None:
            return False
        self._rows[(owner, name)] = row.model_copy(
            update={"approved_fingerprint": fingerprint, "approved_by": approver}
        )
        return True


class PostgresComposedStore:
    """The durable backend, one row per `(owner, name)`."""

    _UPSERT = """
        INSERT INTO composed_workflows (owner, name, document, summary, session_id, correlation_id)
        VALUES (%(owner)s, %(name)s, %(document)s, %(summary)s, %(session_id)s, %(correlation_id)s)
        ON CONFLICT (owner, name) DO UPDATE SET
            document = EXCLUDED.document,
            summary = EXCLUDED.summary,
            session_id = EXCLUDED.session_id,
            correlation_id = EXCLUDED.correlation_id,
            updated_at = now()
    """

    _SELECT_ONE = """
        SELECT owner, name, summary, document, approved_fingerprint, approved_by
        FROM composed_workflows WHERE owner = %s AND name = %s
    """

    _SELECT_FOR = """
        SELECT owner, name, summary, document, approved_fingerprint, approved_by
        FROM composed_workflows WHERE owner = %s ORDER BY updated_at DESC, name LIMIT %s
    """

    # **The approval is never part of the upsert above.** A save is the agent's write and an
    # approval is a person's, so folding them into one statement would give the compose path a
    # column it must not be able to set — the separation is the control, not a tidiness.
    _APPROVE = """
        UPDATE composed_workflows
        SET approved_fingerprint = %(fingerprint)s, approved_by = %(approver)s, approved_at = now()
        WHERE owner = %(owner)s AND name = %(name)s
    """

    async def save(self, workflow: ComposedWorkflow) -> None:
        """Insert or revise this owner's workflow of that name."""
        from chemclaw.core import db
        from chemclaw.core.identity_context import get_current_correlation_id
        from chemclaw.core.session_context import get_current_session_id

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._UPSERT,
                    {
                        "owner": workflow.owner,
                        "name": workflow.name,
                        # Through `json_column`, not a bare `Jsonb`: it refuses a non-finite
                        # float here rather than at the wall, and `tests/test_jsonb_boundaries`
                        # requires every `jsonb` write in the tree to go one way.
                        "document": json_column(workflow.document.model_dump(mode="json")),
                        "summary": workflow.summary,
                        "session_id": get_current_session_id() or "",
                        "correlation_id": get_current_correlation_id() or "",
                    },
                )
            await conn.commit()

    async def get(self, owner: str, name: str) -> ComposedWorkflow | None:
        """One workflow, revalidated through `Template` on the way out.

        Revalidated rather than trusted: the row was written by an earlier release, and a document
        that no longer parses should refuse loudly here instead of reaching the sequencer as a
        shape it half-understands.
        """
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._SELECT_ONE, (owner, name))
                row = await cur.fetchone()
        return None if row is None else _from_row(row)

    async def list_for(self, owner: str) -> Sequence[ComposedWorkflow]:
        """This owner's workflows, most recently changed first, clamped to `MAX_PER_OWNER`."""
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._SELECT_FOR, (owner, MAX_PER_OWNER))
                rows = await cur.fetchall()
        return [_from_row(row) for row in rows]

    async def approve(self, owner: str, name: str, fingerprint: str, approver: str) -> bool:
        """Stamp the approval, answering False when this owner has no such workflow."""
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._APPROVE,
                    {
                        "owner": owner,
                        "name": name,
                        "fingerprint": fingerprint,
                        "approver": approver,
                    },
                )
                touched = cur.rowcount
            await conn.commit()
        return touched > 0


def _from_row(row: Sequence[object]) -> ComposedWorkflow:
    """One database row as a `ComposedWorkflow`, with its document revalidated."""
    return ComposedWorkflow(
        owner=str(row[0]),
        name=str(row[1]),
        summary=str(row[2]),
        document=Template.model_validate(row[3]),
        approved_fingerprint=str(row[4]),
        approved_by=str(row[5]),
    )


_IN_MEMORY = InMemoryComposedStore()


def default_composed_store() -> ComposedStore:
    """The store this deployment uses, following the session store's own switch.

    The same switch `plan_approval_store` and `default_design_store` follow, rather than a knob of
    its own: whether this deployment keeps conversation state durably is one decision, and a second
    setting answering it differently is how two halves of one session come to disagree.
    """
    if settings.session_store == "postgres":
        return PostgresComposedStore()
    return _IN_MEMORY
