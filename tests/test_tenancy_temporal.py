"""The tenant crosses Temporal: workflow, activity, child, continue-as-new and a schedule.

Driven on the time-skipping test server with the real interceptor registered on the client, as
`core/temporal_client.connect_options` does when tenancy is on. What is asserted is what an activity
reads from `current_tenant()` while it runs, because that is what its database borrow will write
into `app.tenant`.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow
from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.core.temporal_client import connect_options
from chemclaw.core.temporal_tenancy import TenantInterceptor
from chemclaw.core.tenancy import bound_tenant, current_tenant
from chemclaw.durable.schedules import PlannedSchedule, _build_schedule
from tests.temporal_env import pydantic_client, start_env_or_skip, start_local_env_or_skip
from tests.tenancy_workflows import Child, Parent


@activity.defn
async def tenant_seen() -> str:
    """What this activity acts for."""
    return current_tenant()


async def _client(env: object) -> Client:
    """The test server's client with the tenant interceptor, as a deployment's would have."""
    base = pydantic_client(env)  # type: ignore[arg-type]
    config = base.config()
    config["interceptors"] = [TenantInterceptor()]
    return Client(**config)


@pytest.fixture
def tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled, with no process tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")


@pytest.mark.usefixtures("tenancy_on")
async def test_a_workflow_started_for_a_tenant_acts_for_it_everywhere() -> None:
    """Activity, child workflow and the continued run all see the starter's tenant."""
    env = await start_env_or_skip()
    async with env:
        client = await _client(env)
        queue = f"tenancy-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client, task_queue=queue, workflows=[Parent, Child], activities=[tenant_seen]
        ):
            with bound_tenant("acme"):
                handle = await client.start_workflow(
                    Parent.run, id=f"p-{uuid.uuid4().hex[:8]}", task_queue=queue
                )
            assert await handle.result() == ["acme", "acme"]
            with bound_tenant("globex"):
                other = await client.start_workflow(
                    Parent.run, id=f"p-{uuid.uuid4().hex[:8]}", task_queue=queue
                )
            assert await other.result() == ["globex", "globex"]


@pytest.mark.usefixtures("tenancy_on")
async def test_a_workflow_started_with_no_tenant_acts_for_none() -> None:
    """Nothing bound at the start is nothing at the activity: it fails closed, never to `acme`."""
    env = await start_env_or_skip()
    async with env:
        client = await _client(env)
        queue = f"tenancy-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client, task_queue=queue, workflows=[Parent, Child], activities=[tenant_seen]
        ):
            handle = await client.start_workflow(
                Parent.run, id=f"p-{uuid.uuid4().hex[:8]}", task_queue=queue
            )
            assert await handle.result() == ["", ""]


async def test_a_schedule_carries_its_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Schedule has no request to inherit from, so the plan's tenant rides on the action."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    monkeypatch.setattr(settings, "background_task_queue", f"tenancy-{uuid.uuid4().hex[:8]}")
    env = await start_local_env_or_skip()
    async with env:
        client = await _client(env)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[Parent, Child],
            activities=[tenant_seen],
        ):
            planned = PlannedSchedule("retention@acme", Parent, timedelta(hours=1), tenant="acme")
            handle = await client.create_schedule(planned.schedule_id, _build_schedule(planned))
            await handle.trigger()
            for _ in range(100):
                recent = (await handle.describe()).info.recent_actions
                if recent:
                    break
                await asyncio.sleep(0.1)
            assert recent, "the triggered schedule never started a workflow"
            action = recent[-1].action
            assert isinstance(action, ScheduleActionExecutionStartWorkflow)
            run = client.get_workflow_handle(action.workflow_id)
            assert action.workflow_id.startswith("retention@acme-scheduled")
            assert await run.result() == ["acme", "acme"]


def test_a_deployment_with_tenancy_off_installs_no_interceptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenancy off adds no interceptor to the client options; on, it adds this one."""
    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    assert "interceptors" not in connect_options()
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    assert any(isinstance(i, TenantInterceptor) for i in connect_options()["interceptors"])
