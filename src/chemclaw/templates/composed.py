"""Workflows the agent composed at run time — and the rule that makes storing one safe.

A template's `agent` step is exempt from the plan gate because a person authored and reviewed the
file (`D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only`). An agent-authored workflow
lacks that premise, so it may name no side-effecting tool step and no `write_tools`, and no approval
lifts either (`authored_problems`). A durable `job` step is the one thing a person can authorize,
per document version (`unapproved_jobs`): its name and arguments are in the document they read,
whereas `write_tools` is a permission spent later on calls nobody saw.

Both checks run when a workflow is stored and again when it is run, because `side_effecting_tools()`
can grow between the two.

Keyed by `(owner, name)`: a composed workflow is one chemist's procedure, and resolving against the
caller's own rows stops a name reaching somebody else's steps.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
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
    # The `template_fingerprint` of the document a person approved, or `""`. Compared, not trusted,
    # so re-composing lapses the approval with nothing to clear.
    approved_fingerprint: str = ""
    # The approving person, never the agent: nothing the model can call writes this.
    approved_by: str = ""
    # When the approval was given; read by `GET /workflows/{name}` as the audit of a human decision.
    approved_at: datetime | None = None
    # The conversation it was composed in, stamped by the store from the ambient context rather than
    # taken from the caller. Shown by `GET /workflows/{name}` so the approver sees which
    # conversation asked for it.
    session_id: str = ""


def authored_problems(template: Template, side_effecting: frozenset[str]) -> list[str]:
    """Why this document may not be run as an agent-authored workflow **at all**, or `[]`.

    The refusals no approval lifts, in one function so the write and run paths ask the same
    question:

    - a `tool` step naming a side-effecting tool, which the plan gate would let through because a
      template step has no session;
    - `write_tools`, which `step_profile` would otherwise restore into the step's graph;
    - a step kind this function does not recognise, refused so a new kind fails closed.

    An `agent` step is allowed: with no writes declared its surface is narrowed to reads, so
    reasoning stays free while the procedure cannot act.

    Args:
        template: The document to check.
        side_effecting: This deployment's `authz.side_effecting_tools()`. Passed rather than
        imported because `templates` may not import `agent`, and so the run-time check uses the
        run's own set.

    Returns:
        One line per problem, empty when nothing here refuses the document.
    """
    problems: list[str] = []
    for step in template.steps:
        if isinstance(step, ToolStep):
            if step.tool in side_effecting:
                problems.append(
                    f"step {step.id!r} calls {step.tool!r}, which changes something. A workflow "
                    "you composed yourself may only read: it runs with no session to approve a "
                    "plan in, so nothing can put a person in front of that call. Ask for the "
                    "change directly in the conversation instead, where the plan gate applies."
                )
        elif isinstance(step, AgentStep):
            if step.write_tools:
                problems.append(
                    f"step {step.id!r} declares write tools {sorted(step.write_tools)}. Only a "
                    "reviewed template in `data/templates/` may declare those; a workflow you "
                    "composed may not hand a model a write to spend."
                )
        elif not isinstance(step, JobStep):
            # Unknown step kinds are refused: here "allowed" means "exempt from the plan gate", so
            # this must fail closed.
            problems.append(
                f"step {step.id!r} is a {type(step).__name__}, which is a kind of step this "
                "system has no position on for a workflow the agent composed. Only a reviewed "
                "template in `data/templates/` may use it."
            )
    return problems


def step_call(step: object) -> tuple[str, dict[str, object], str]:
    """What one step calls, with what, and what it reasons about — for a screen a person approves.

    Returns `(calls, arguments, prompt)`: the tool or job name (empty for a reasoning step), its
    arguments with `${…}` references intact, and the prompt. Shared by `GET /workflows/{name}` and
    `/approve-workflow`. Reads fields by `getattr` rather than branching on step classes, so a new
    step kind still renders (and `authored_problems` refuses it).
    """
    return (
        str(getattr(step, "tool", "") or getattr(step, "job", "")),
        dict(getattr(step, "arguments", {}) or {}),
        str(getattr(step, "prompt", "") or ""),
    )


def job_steps(template: Template) -> list[str]:
    """The ids of every `job` step in `template`, in declared order.

    Shared by the enforcement and the route that shows a person what an approval would authorize.
    """
    return [step.id for step in template.steps if isinstance(step, JobStep)]


def unapproved_jobs(template: Template, approved_fingerprint: str, fingerprint: str) -> list[str]:
    """Why this document's durable jobs may not run yet, or `[]`.

    The only refusal a human can lift. A workflow may be stored with `job` steps but not run them
    until a person approves this exact document. The approval is keyed on the document hash, so
    re-composing lapses it; a per-actor permission would let a changed workflow inherit an old
    approval.

    Args:
        template: The document about to run.
        approved_fingerprint: What a person approved, or `""` when nobody has.
        fingerprint: This document's hash now. Passed because `templates` may not import `durable`.

    Returns:
        One line naming the jobs and how to authorize them, or `[]`.
    """
    jobs = job_steps(template)
    if not jobs or approved_fingerprint == fingerprint:
        return []
    return [
        f"it launches the durable job(s) at step(s) {jobs}, and a job costs real compute on a "
        "procedure nobody has reviewed. Ask the chemist who owns this workflow to approve it, at "
        "whichever surface they are on — `/approve-workflow <name>` at a terminal, "
        "`POST /workflows/{name}/approval` on the front door; both are a person's and neither is "
        "a tool — and "
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

    async def approve(self, owner: str, name: str, fingerprint: str) -> bool:
        """Record that `owner` approved this exact version; False when the row is gone.

        The approver is always the owner: the route resolves the workflow against the caller's own
        rows, so there is no third party to name, and the store cannot represent one.
        """
        ...

    async def forget(self, owner: str, name: str) -> bool:
        """Remove one of `owner`'s workflows; False when they have no such name."""
        ...


class InMemoryComposedStore:
    """The store a deployment with no Postgres gets.

    Not a test double: a CLI session or dev process is a real deployment, where a workflow vanishing
    with the process is the honest behaviour.
    """

    def __init__(self) -> None:
        """Start empty, keyed the way the table is."""
        self._rows: dict[tuple[str, str], ComposedWorkflow] = {}
        self._order: list[tuple[str, str]] = []

    async def save(self, workflow: ComposedWorkflow) -> None:
        """Store `workflow`, newest-first in the listing, carrying any approval forward.

        The approval fields are never the caller's to set, in either backend: a save is the agent's
        write and carries the stored approval forward unchanged. The approval still lapses because
        `unapproved_jobs` compares fingerprints; readers must not render `approved_by` without that
        comparison.
        """
        from chemclaw.core.session_context import get_current_session_id

        key = (workflow.owner, workflow.name)
        stored = self._rows.get(key)
        self._rows[key] = workflow.model_copy(
            update={
                "approved_fingerprint": stored.approved_fingerprint if stored else "",
                "approved_by": stored.approved_by if stored else "",
                "approved_at": stored.approved_at if stored else None,
                "session_id": get_current_session_id() or "",
            }
        )
        if key in self._order:
            self._order.remove(key)
        self._order.insert(0, key)

    async def get(self, owner: str, name: str) -> ComposedWorkflow | None:
        """One workflow, or `None`."""
        return self._rows.get((owner, name))

    async def list_for(self, owner: str) -> Sequence[ComposedWorkflow]:
        """This owner's workflows, most recently saved first, clamped one past `MAX_PER_OWNER`.

        The same page as the SQL backend's `LIMIT`, since the cap guard counts what this returns.
        """
        mine = [self._rows[key] for key in self._order if key[0] == owner]
        return mine[: MAX_PER_OWNER + 1]

    async def forget(self, owner: str, name: str) -> bool:
        """Drop one workflow, answering False when this owner has no such name."""
        key = (owner, name)
        if key not in self._rows:
            return False
        del self._rows[key]
        self._order.remove(key)
        return True

    async def approve(self, owner: str, name: str, fingerprint: str) -> bool:
        """Stamp the approval onto the stored row."""
        row = self._rows.get((owner, name))
        if row is None:
            return False
        self._rows[(owner, name)] = row.model_copy(
            update={
                "approved_fingerprint": fingerprint,
                "approved_by": owner,
                # The Postgres backend uses the database's `now()`; with no database the store
                # stamps it.
                "approved_at": datetime.now(tz=UTC),
            }
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
        SELECT owner, name, summary, document, approved_fingerprint, approved_by, approved_at,
               session_id
        FROM composed_workflows WHERE owner = %s AND name = %s
    """

    # `MAX_PER_OWNER + 1`, so the cap guard can tell "at the cap" from "over the cap" and the route
    # can say the page was clamped.
    _SELECT_FOR = """
        SELECT owner, name, summary, document, approved_fingerprint, approved_by, approved_at,
               session_id
        FROM composed_workflows WHERE owner = %s ORDER BY updated_at DESC, name LIMIT %s
    """

    # The approval is never part of the upsert: a save is the agent's write and an approval a
    # person's, so the compose path must not be able to set it.
    _APPROVE = """
        UPDATE composed_workflows
        SET approved_fingerprint = %(fingerprint)s, approved_by = %(owner)s, approved_at = now()
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
                        # `json_column` refuses a non-finite float here;
                        # `tests/test_jsonb_boundaries` requires every `jsonb` write to use it.
                        "document": json_column(workflow.document.model_dump(mode="json")),
                        "summary": workflow.summary,
                        "session_id": get_current_session_id() or "",
                        "correlation_id": get_current_correlation_id() or "",
                    },
                )
            await conn.commit()

    async def get(self, owner: str, name: str) -> ComposedWorkflow | None:
        """One workflow, revalidated through `Template` on the way out.

        A row written by an earlier release that no longer parses fails here rather than reaching
        the sequencer.
        """
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._SELECT_ONE, (owner, name))
                row = await cur.fetchone()
        return None if row is None else _from_row(row)

    async def list_for(self, owner: str) -> Sequence[ComposedWorkflow]:
        """This owner's workflows, most recently changed first, clamped one past `MAX_PER_OWNER`."""
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._SELECT_FOR, (owner, MAX_PER_OWNER + 1))
                rows = await cur.fetchall()
        return [_from_row(row) for row in rows]

    _FORGET = "DELETE FROM composed_workflows WHERE owner = %s AND name = %s"

    async def forget(self, owner: str, name: str) -> bool:
        """Drop one workflow, answering False when this owner has no such name.

        A real delete, not a tombstone, so an owner at `MAX_PER_OWNER` can make room without
        overwriting a name.
        """
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._FORGET, (owner, name))
                removed = cur.rowcount
            await conn.commit()
        return removed > 0

    async def approve(self, owner: str, name: str, fingerprint: str) -> bool:
        """Stamp the approval, answering False when this owner has no such workflow."""
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._APPROVE,
                    {"owner": owner, "name": name, "fingerprint": fingerprint},
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
        approved_at=row[6] if isinstance(row[6], datetime) else None,
        session_id=str(row[7]),
    )


_IN_MEMORY = InMemoryComposedStore()


def default_composed_store() -> ComposedStore:
    """The store this deployment uses, following the session store's own switch.

    Same switch as `plan_approval_store` and `default_design_store`, so durability of conversation
    state is one decision.
    """
    if settings.session_store == "postgres":
        return PostgresComposedStore()
    return _IN_MEMORY
