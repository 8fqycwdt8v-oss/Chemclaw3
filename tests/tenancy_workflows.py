"""Workflows for `tests/test_tenancy_temporal.py`, in a module of their own.

A workflow module is re-imported inside Temporal's sandbox, so it imports nothing but the SDK and
reaches the activity by name.
"""

from datetime import timedelta

from temporalio import workflow


@workflow.defn
class Child:
    """Reports the tenant its activity sees."""

    @workflow.run
    async def run(self) -> str:
        """Run one activity."""
        return str(
            await workflow.execute_activity(
                "tenant_seen", schedule_to_close_timeout=timedelta(seconds=30)
            )
        )


@workflow.defn
class Parent:
    """Reports the tenant its activity and its child's see, then again after `continue_as_new`."""

    @workflow.run
    async def run(self, hop: int = 0) -> list[str]:
        """Run an activity and a child, and continue once."""
        seen = [
            str(
                await workflow.execute_activity(
                    "tenant_seen", schedule_to_close_timeout=timedelta(seconds=30)
                )
            ),
            await workflow.execute_child_workflow(Child.run, id=f"child-{workflow.info().run_id}"),
        ]
        if hop == 0:
            workflow.continue_as_new(1)
        return seen
