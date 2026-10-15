"""The tenant crosses Temporal as a header, so a job acts for whoever started it.

A request binds its tenant in a context variable (`core/tenancy.py`); a worker has no request. This
interceptor carries the value from `start_workflow` (client side) through every activity and child
workflow the workflow starts, including across `continue_as_new`, to the activity that binds it
again for its duration. A payload field would need every workflow input to grow one, and a missed
one would be a worker that silently reads nothing; a header cannot be missed.

Installed on the client only while `tenancy_enabled` (`core/temporal_client.connect_options`), and
the SDK applies a client's interceptors to the workers built from it, so one registration covers
both halves. A job started with no tenant carries no header and acts for none.

Invariant: the header is read as text and validated as a tenant id before it is bound.
"""

from collections.abc import Mapping
from typing import Any, NoReturn

import temporalio.api.common.v1
from temporalio import client, converter, worker, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.tenancy import TENANT_HEADER, bound_tenant, current_tenant

_Headers = Mapping[str, temporalio.api.common.v1.Payload]


def tenant_header(tenant: str) -> dict[str, temporalio.api.common.v1.Payload]:
    """The header that makes a workflow, and what it starts, act for `tenant`."""
    return {TENANT_HEADER: converter.default().payload_converter.to_payload(tenant)}


class TenantInterceptor(client.Interceptor, worker.Interceptor):
    """Stamp the ambient tenant on what a client starts, and bind it around what a worker runs."""

    def intercept_client(self, next: client.OutboundInterceptor) -> client.OutboundInterceptor:
        """Wrap the client's outbound calls."""
        return _ClientOutbound(next)

    def intercept_activity(
        self, next: worker.ActivityInboundInterceptor
    ) -> worker.ActivityInboundInterceptor:
        """Wrap the activity's execution."""
        return _ActivityInbound(next)

    def workflow_interceptor_class(
        self, input: worker.WorkflowInterceptorClassInput
    ) -> type[worker.WorkflowInboundInterceptor]:
        """The sandboxed class that forwards the header."""
        return _WorkflowInbound


class _ClientOutbound(client.OutboundInterceptor):
    """Put the ambient tenant on the workflows this process starts."""

    async def start_workflow(
        self, input: client.StartWorkflowInput
    ) -> client.WorkflowHandle[Any, Any]:
        """Add the header; a process with no tenant bound adds none."""
        tenant = current_tenant() if settings.tenancy_enabled else ""
        if tenant and TENANT_HEADER not in (input.headers or {}):
            input.headers = {**(input.headers or {}), **tenant_header(tenant)}
        return await super().start_workflow(input)


class _ActivityInbound(worker.ActivityInboundInterceptor):
    """Bind the header's tenant for the duration of one activity."""

    async def execute_activity(self, input: worker.ExecuteActivityInput) -> Any:
        """Run the activity for the tenant its header names, or for none."""
        payload = input.headers.get(TENANT_HEADER)
        if payload is None:
            return await self.next.execute_activity(input)
        tenant = converter.default().payload_converter.from_payload(payload, str)
        with bound_tenant(tenant):
            return await self.next.execute_activity(input)


class _WorkflowInbound(worker.WorkflowInboundInterceptor):
    """Remember the header the workflow was started with, for its outbound half to forward."""

    def __init__(self, next: worker.WorkflowInboundInterceptor) -> None:
        """Hold no tenant until `execute_workflow` reads one."""
        super().__init__(next)
        self.payload: temporalio.api.common.v1.Payload | None = None

    def init(self, outbound: worker.WorkflowOutboundInterceptor) -> None:
        """Interpose the outbound half."""
        super().init(_WorkflowOutbound(outbound, self))

    async def execute_workflow(self, input: worker.ExecuteWorkflowInput) -> Any:
        """Read the header the workflow started with."""
        self.payload = input.headers.get(TENANT_HEADER)
        return await super().execute_workflow(input)


class _WorkflowOutbound(worker.WorkflowOutboundInterceptor):
    """Copy the workflow's tenant onto everything it starts."""

    def __init__(self, next: worker.WorkflowOutboundInterceptor, inbound: _WorkflowInbound) -> None:
        """Share the inbound half's reading."""
        super().__init__(next)
        self._inbound = inbound

    def _forward(self, headers: _Headers) -> _Headers:
        payload = self._inbound.payload
        if payload is None or TENANT_HEADER in headers:
            return headers
        return {**headers, TENANT_HEADER: payload}

    def continue_as_new(self, input: worker.ContinueAsNewInput) -> NoReturn:
        """The next run acts for the same tenant."""
        input.headers = self._forward(input.headers)
        self.next.continue_as_new(input)

    def start_activity(self, input: worker.StartActivityInput) -> workflow.ActivityHandle[Any]:
        """The activity acts for the workflow's tenant."""
        input.headers = self._forward(input.headers)
        return self.next.start_activity(input)

    def start_local_activity(
        self, input: worker.StartLocalActivityInput
    ) -> workflow.ActivityHandle[Any]:
        """The local activity acts for the workflow's tenant."""
        input.headers = self._forward(input.headers)
        return self.next.start_local_activity(input)

    async def start_child_workflow(
        self, input: worker.StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle[Any, Any]:
        """The child acts for the workflow's tenant."""
        input.headers = self._forward(input.headers)
        return await self.next.start_child_workflow(input)
