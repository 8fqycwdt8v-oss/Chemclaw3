"""The one durable wrapper every connector job runs inside — core keeps the cross-cutting concerns.

Why this exists: before connectors, four bespoke adapters (`agents/qm_tools.py`,
`agents/durable_tools.py`) each re-implemented the same shape — derive a deterministic id, stamp the
actor, start a named workflow, map its status, publish a note through the PR-gate, push back to the
launching session — and each imported its workflow class directly, which forced every durable
capability into core's own worker lists (`workers/background_worker.py`).

This workflow inverts that. The connector owns its workflow *code* and the worker that serves it;
core owns the obligations that must never vary per capability:

- **Idempotency** — the wrapper's id is derived from the job and its arguments by
  `connectors.jobs`, with `ALLOW_DUPLICATE_FAILED_ONLY`, so re-asking joins the existing run and
  only a failed one re-executes (D-011: a stored result is never recomputed).
- **Attribution** — the requesting actor travels in the payload (F4-T3), exactly as `QMJobInput`
  carries `requested_by`, so an audit can always name the user behind a durable run. It is handed
  down to the child on its **memo**, not in its argument, so a bundle whose backend runs under a
  shared service identity (the HPC cluster) can still name the user without the actor becoming a
  field the model could author.
- **The PR-gate** — a job that produces knowledge returns a `Note` and core publishes it through
  `kg.pr_gate` (via the existing `publish_memory_note_activity`). A connector never writes to the
  graph itself, so "AI proposes, human signs off" cannot be bypassed by adding a connector.
- **Session push-back** — the launching chat is woken through the one existing channel (F3-T3), so
  a connector job surfaces in the UI exactly as a QM job does, with no per-connector plumbing.

The child is addressed by **workflow type name + task queue**, both strings from the manifest, so
this module imports nothing from any connector — and moving a workflow between workers is a one-line
manifest change rather than a code change (`docs/connector-plan.md` §5.3).
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from kg.note import Note
    from workflows.memory_jobs import publish_memory_note_activity
    from workflows.notify import notify_session_best_effort

from workflows.publish import BAD_DATA_RETRY, publish_note_best_effort
from workflows.registry import durable_workflow


class ConnectorJobInput(BaseModel):
    """What core needs to run one connector job: where the work lives, and the turn it came from.

    `workflow`/`task_queue` come straight from the manifest's `JobSpec` and are the *only* thing
    binding this run to a connector — no import, no shared type. `payload` is the model-supplied
    arguments already validated against the job's generated params model, so the child receives a
    plain, replay-stable mapping rather than a type core would have to know.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str = Field(min_length=1)
    job: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    task_queue: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    # The Entra actor this run is attributed to (`require_actor` at the tool boundary guarantees it
    # is present under Entra). Carried in the payload rather than read ambiently, because a workflow
    # has no request context — the same reason `QMJobInput.requested_by` exists.
    requested_by: str = Field(min_length=1)
    # The chat to wake on completion; empty off the service path (CLI, tests), where there is no
    # session to push back to.
    session_id: str = ""
    publish_to_graph: bool = False


class ConnectorJobResult(BaseModel):
    """The result envelope every connector workflow returns — the whole cross-process contract.

    `summary` is the one line the chat shows and the model reads; `data` is the job's own structured
    result, opaque to core (a connector's domain types stay the connector's business); `note` is the
    optional knowledge contribution. Typing `note` as the existing frozen `Note` means a connector's
    proposal passes the graph's own slug and schema validators on the way in, so a malformed note is
    rejected at the boundary instead of failing later at branch creation in the PR-gate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    note: Note | None = None


# On the light queue: this wrapper does no work itself — it starts a child on the
# connector's own queue and waits — so it belongs with the many light workers, not the few
# heavy ones. The *capability* is heavy; this is not (D-006).
@durable_workflow("background")
@workflow.defn
class ConnectorJobWorkflow:
    """Run one connector-owned workflow as a child, then publish and notify on its behalf."""

    @workflow.run
    async def run(self, job: ConnectorJobInput) -> ConnectorJobResult:
        """Execute the connector's workflow, PR-gate any note it produced, and wake its session.

        The child runs on the connector's own task queue, so its dependencies and its failure domain
        stay outside this worker. A child failure propagates: the job genuinely failed, and the tool
        that launched it reports `failed` through `get_durable_job_status` — deliberately unlike the
        note publish and the push-back below, which are best-effort because the scientific result is
        already durable by the time they run.
        """
        result: ConnectorJobResult = await workflow.execute_child_workflow(
            job.workflow,
            job.payload,
            id=f"{workflow.info().workflow_id}-run",
            task_queue=job.task_queue,
            result_type=ConnectorJobResult,
            # The actor, carried as per-execution metadata rather than in the argument. A bundle
            # whose backend runs under a *shared* service identity — the HPC cluster is the one we
            # have — must still be able to name the user behind a run, and `payload` is exactly the
            # model-authored arguments, so putting the actor there would make it a field the LLM
            # could fill in. A memo is beside the argument, readable with `workflow.memo_value`,
            # and set once here for every connector job rather than per bundle (D-118).
            memo={"requested_by": job.requested_by},
            # A child is started once per parent run; a retried parent activity never re-launches
            # it, so rejecting duplicates is the honest policy (a duplicate id here is a bug).
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            retry_policy=BAD_DATA_RETRY,
            execution_timeout=timedelta(seconds=settings.connector_job_timeout_seconds),
        )
        if job.publish_to_graph and result.note is not None:
            # The same PR-gate activity the memory-synthesis jobs use — one write path into the
            # graph, on the light background queue, bounded retries, never failing the job.
            await publish_note_best_effort(
                publish_memory_note_activity, [result.note], label=f"{job.connector}:{job.job}"
            )
        if job.session_id:
            await notify_session_best_effort(
                job.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "connector": job.connector,
                    "job": job.job,
                    "summary": result.summary,
                },
            )
        return result
