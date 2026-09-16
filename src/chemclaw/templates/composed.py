"""Workflows the agent composed at run time — and the rule that makes storing one safe.

**The rule first, because the table is not what makes this safe.** A template's `agent` step is
exempt from the plan gate, and `D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only`
argues that exemption from a premise: the file was authored by a person, committed to git and
reviewed, and *"nothing at run time can produce one"*. This module produces one at run time. So the
premise has to be restored some other way, and `authored_problems` is that way:

**An agent-authored workflow may name no side-effecting tool and no `write_tools`. Ever — no
approval lifts either.** A durable `job` step is the one thing a person *can* authorize, and
`unapproved_jobs` is where that is decided (`D-2026-09-15-an-approval-is-for-one-version-of-one-
workflow`); `authored_problems` takes no approval argument at all, which is how the first sentence
is enforced rather than remembered.

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

**Where the line falls, and why a job is on the other side of it from a write.** A `job` step is
bounded compute *whose call the approver read*: the job's name and its arguments are in the
document a person approved, and running it produces a result. `write_tools` is not a call at all —
it is a permission handed to a model turn, spent later on a call nobody has seen, chosen by the
model inside the step. A person can meaningfully approve the first and cannot meaningfully approve
the second. A side-effecting `tool` step sits nearer the first and stays refused with it anyway,
because the reachable set is every write in the tree and that case has not been made.

Keyed by `(owner, name)`. A composed workflow is one chemist's working procedure, not a
deployment's catalogue, and resolving against the caller's own rows is what stops a name reaching
somebody else's steps.
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
    # What a person approved, as a `template_fingerprint` of the document they were shown, or `""`
    # when nobody has. Compared rather than trusted: `unapproved_jobs` asks whether it still matches
    # *this* document, so re-composing lapses the approval without anything having to clear it.
    approved_fingerprint: str = ""
    # The person. Never the agent — nothing the model can call writes this, and
    # `tests/test_composed_workflows.py` asserts that absence rather than trusting it.
    approved_by: str = ""
    # When. Carried because the row *is* this system's audit of a human decision, and a column
    # written and never selected is an attribution nothing can read — the mirror of
    # `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`. `GET /workflows/{name}`
    # is the reader.
    approved_at: datetime | None = None
    # The conversation it was composed in, stamped by the store from the ambient context rather
    # than taken from the caller — the same treatment `approved_at` gets, and for the same reason:
    # it is provenance the store observes, not a field the writer declares. Read by
    # `GET /workflows/{name}`, so the approver can see which conversation asked for this procedure.
    # The column had no reader in code at all, which makes its "so a workflow that later looks
    # wrong can be traced back" a promise only somebody holding a psql prompt could keep.
    session_id: str = ""


def authored_problems(template: Template, side_effecting: frozenset[str]) -> list[str]:
    """Why this document may not be run as an agent-authored workflow **at all**, or `[]`.

    The refusals no approval lifts. One function so the write path and the run path ask the same
    question and cannot drift apart; `unapproved_jobs` below holds the one that a human *can* lift,
    and the split between the two files is the whole security argument of this module.

    Two refusals here close a distinct way the plan gate could be bypassed, and a third closes the
    way a *future* step kind would bypass both:

    - a **`tool` step naming a side-effecting tool**, which is the sharp one. A `tool` step runs
      through `invoke_governed` under the requester's identity, so `enforce_tool_authz` decides —
      but the plan gate's own early return (*"No session means no plan to approve"*) lets it past,
      correctly for a human-authored procedure and not for one the agent wrote a moment ago.
    - **`write_tools`**, which `step_profile` would otherwise restore into the step's graph. This
      is the one the exemption's own wording is about.
    - **a step kind this function does not recognise**, which is not defensive generality: the
      chain was `if ToolStep … elif AgentStep …` with no final branch, so a fourth kind added next
      year was silently *allowed* on the day it was added — failing open in the one function whose
      job is to fail closed. `agent/template_surface.template_step_ceilings` takes the same
      position from the other side, where an unsized kind raises rather than counting as free.

    **Why a human's approval lifts the `job` step and not these two**, which is the line this
    widening is drawn on rather than a limit nobody argued. A `job` step is *bounded compute whose
    call the approver read*: the job's name and its arguments are in the document they approved, and
    running it produces a result. `write_tools` is not a call at all — it is a permission handed to
    a model turn, spent later on a call nobody has seen, chosen by the model inside the step. A
    person can meaningfully approve the first and cannot meaningfully approve the second, so no
    approval offers to. A side-effecting `tool` step sits nearer the first and stays refused with
    it, because the reachable set is every write in the tree and the case for widening it has not
    been made. **That is a refusal, not a deferral**, and this paragraph said the opposite — it
    claimed `docs/planning/BACKLOG.md` carried the question, which no commit ever wrote there.
    Widening it is a new ADR arguing what a person is approving when the call is "any write this
    deployment has", and nothing is holding a place for one.

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
            # **Refused by default, because this is the module where "allowed" means "exempt from
            # the plan gate".** The chain above was `if ToolStep … elif AgentStep …` with no final
            # branch, so a fourth step kind added next year would be silently permitted here on the
            # day it was added — failing open in the one function whose whole job is to fail
            # closed. `agent/template_surface.template_step_ceilings` takes the same position from
            # the other side, where an unsized kind raises rather than counting as free.
            problems.append(
                f"step {step.id!r} is a {type(step).__name__}, which is a kind of step this "
                "system has no position on for a workflow the agent composed. Only a reviewed "
                "template in `data/templates/` may use it."
            )
    return problems


def step_call(step: object) -> tuple[str, dict[str, object], str]:
    """What one step calls, with what, and what it reasons about — for a screen a person approves.

    **One definition, because two surfaces render the same approval.** `GET /workflows/{name}` and
    the terminal's `/approve-workflow <name>` each have to show the procedure rather than its step
    ids (`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` one layer over), and
    two copies of this reading are two chances for one of them to stop showing a field.

    By `getattr` over the union's fields rather than by branching on the three step classes: a step
    kind added later renders as much as it has instead of being silently omitted from the screen a
    person approves from, which is the failure this exists to prevent. `authored_problems` refuses
    such a step outright, so the two answers agree — one shows it, the other will not run it.

    Args:
        step: One step of a resolved `Template`.

    Returns:
        `(calls, arguments, prompt)` — the tool or job name (empty for a reasoning step), its
        arguments as written with their `${…}` references intact, and the prompt.
    """
    return (
        str(getattr(step, "tool", "") or getattr(step, "job", "")),
        dict(getattr(step, "arguments", {}) or {}),
        str(getattr(step, "prompt", "") or ""),
    )


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

    async def approve(self, owner: str, name: str, fingerprint: str) -> bool:
        """Record that `owner` approved this exact version; False when the row is gone.

        **The approver is the owner and cannot be anybody else**, which is why there is no separate
        argument for one. It took a fourth parameter, and both callers passed the same value twice
        — `POST /workflows/{name}/approval` resolves the workflow against the caller's own rows, so
        a name that is not yours is a 404 and there is no third party to name. Keeping the
        parameter meant the *store* permitted a state the write path cannot produce, and
        `agent/leaver.py` then had to reason about a departing approver its erase predicate
        (`WHERE owner = ANY(...)`) could not reach. Removing it makes that state unrepresentable
        rather than merely unreached.
        """
        ...

    async def forget(self, owner: str, name: str) -> bool:
        """Remove one of `owner`'s workflows; False when they have no such name."""
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
        """Store `workflow`, newest-first in the listing, carrying any approval forward.

        **The approval columns are never the caller's to set**, and that is structural in both
        backends rather than a convention: `PostgresComposedStore._UPSERT` names none of them, and
        this carries the stored row's forward. It replaced the whole object, so the same
        `ComposedWorkflow(..., approved_by="alice")` was an approval in one backend and a no-op in
        the other — a divergence in the direction where the weaker deployment is the one a CLI runs.

        **Carried forward and not cleared**, which is the other half the two backends disagreed on.
        A save is the *agent's* write, and an agent erasing the record of a person's decision is
        the one thing this column exists to prevent; the approval lapses anyway, because
        `unapproved_jobs` compares the fingerprint and a re-composed document no longer matches.
        What a reader must not do is render `approved_by` without that comparison —
        `api/routes/workflows.py` returns a derived `approved` beside it for exactly that reason.
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
        """This owner's workflows, most recently saved first."""
        return [self._rows[key] for key in self._order if key[0] == owner]

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
                # The Postgres backend takes this from the database's own `now()`; here there is no
                # database, so the store stamps it. Both backends must answer the question, or a
                # property that held in only one is a property this deployment does not have.
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

    # `MAX_PER_OWNER + 1` and not `MAX_PER_OWNER`: the cap's guard counts what this returns, so a
    # page size equal to the cap makes "at the cap" and "over the cap" the same answer. Driven at 60
    # rows, the oldest workflows became invisible to the guard *and* to the listing that names them
    # — so re-composing a workflow the owner still had was refused with "you already have 50" while
    # `get` went on finding and running it. One spare row is the whole fix: the count can now exceed
    # the cap, so the guard can tell the two apart and the route can say the page was clamped.
    _SELECT_FOR = """
        SELECT owner, name, summary, document, approved_fingerprint, approved_by, approved_at,
               session_id
        FROM composed_workflows WHERE owner = %s ORDER BY updated_at DESC, name LIMIT %s
    """

    # **The approval is never part of the upsert above.** A save is the agent's write and an
    # approval is a person's, so folding them into one statement would give the compose path a
    # column it must not be able to set — the separation is the control, not a tidiness.
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

        **A real delete rather than a tombstone**, because the alternative a chemist had was worse:
        with a cap of `MAX_PER_OWNER` and no way to remove one, the only way to make room was to
        re-compose over a name — which destroys the document anyway *and* leaves a row whose name
        lies about its contents. The grant already permits DELETE for offboarding
        (`agent/leaver.py`); this is the same verb for the owner's own act.
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

    The same switch `plan_approval_store` and `default_design_store` follow, rather than a knob of
    its own: whether this deployment keeps conversation state durably is one decision, and a second
    setting answering it differently is how two halves of one session come to disagree.
    """
    if settings.session_store == "postgres":
        return PostgresComposedStore()
    return _IN_MEMORY
