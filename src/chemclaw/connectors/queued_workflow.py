"""The durable half of a queued tool call: wait for a slot, make the call, deliver the answer.

Started by the turn (`connectors/queued.py`) on the connector's interactive queue and run by that
connector's interactive worker (`connectors/interactive_worker.py`). The turn waits
`queued.inline_wait_seconds` for it; when that runs out it signals `detach` with its session, and
this run delivers the answer to that session's mailbox when it lands — the same `job_completed`
push-back a durable job sends, so every surface that already shows one shows this.

**One run per identical call.** The turn starts it under an id derived from the call and joins the
run already open under that id, so fifty chemists asking for the same pKa share one calculation —
the cross-process single-flight `cached_compute` does not have. Every chemist whose wait ran out
detaches with their own session, and each is told.
"""

import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.queued_call import QueuedToolCall, call_queued_tool
    from chemclaw.durable.connector_job import ConnectorJobResult, failure_reason
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.publish import queued_tool_retry

#: What `ConnectorJobResult.payload_kind` says for a queued call's envelope.
PAYLOAD_KIND = "mcp_tool_result"
#: Where the server's own `CallToolResult` sits inside `ConnectorJobResult.data`.
RESULT_KEY = "call_tool_result"
_SUMMARY_CHARS = 500


def envelope(call: QueuedToolCall, raw: dict[str, Any]) -> ConnectorJobResult:
    """Wrap the server's answer in the connector envelope every job collector already decodes.

    So `get_durable_job_status` and `GET /jobs/{id}` answer for a detached queued call with no
    branch of their own, and the summary a chemist sees in the mailbox is the tool's own words.
    """
    text = " ".join(
        str(block.get("text", "")) for block in raw.get("content", []) if isinstance(block, dict)
    ).strip()
    verb = "refused" if raw.get("isError") else "answered"
    summary = f"{call.tool} {verb}: {text}" if text else f"{call.tool} {verb} with no text"
    if len(summary) > _SUMMARY_CHARS:
        summary = summary[: _SUMMARY_CHARS - 1] + "…"
    return ConnectorJobResult(summary=summary, data={RESULT_KEY: raw}, payload_kind=PAYLOAD_KIND)


@workflow.defn(failure_exception_types=[Exception])
class QueuedToolWorkflow:
    """Run one queued tool call and tell every detached session how it ended."""

    def __init__(self) -> None:
        """No session is waiting on the mailbox until a turn's inline wait runs out."""
        self._sessions: list[str] = []

    @workflow.signal
    def detach(self, session_id: str) -> None:
        """A turn stopped waiting inline; deliver the answer to `session_id` when it lands."""
        if session_id and session_id not in self._sessions:
            self._sessions.append(session_id)

    @workflow.run
    async def run(self, call: QueuedToolCall) -> ConnectorJobResult:
        """Wait for a slot, make the call, and deliver it to whoever stopped waiting."""
        try:
            raw = await workflow.execute_activity(
                call_queued_tool,
                args=[
                    call,
                    workflow.memo_value("requested_by", ""),
                    workflow.memo_value("correlation_id", ""),
                    workflow.memo_value("session_id", ""),
                ],
                start_to_close_timeout=timedelta(seconds=call.call_timeout_seconds),
                schedule_to_close_timeout=timedelta(seconds=call.queue_timeout_seconds),
                retry_policy=queued_tool_retry(),
            )
        except Exception as exc:
            # Suppressed as `ConnectorJobWorkflow._notify_failure` suppresses it: the failure being
            # reported is the one this run must end with, and a push-back that fails too must not
            # replace it.
            for session in self._sessions:
                with contextlib.suppress(BaseException):
                    await notify_session_best_effort(
                        session,
                        "job_failed",
                        {
                            "job_id": workflow.info().workflow_id,
                            "connector": call.connector,
                            "job": call.tool,
                            "reason": failure_reason(exc),
                        },
                    )
            raise
        result = envelope(call, raw)
        for session in self._sessions:
            await notify_session_best_effort(
                session,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "connector": call.connector,
                    "job": call.tool,
                    "summary": result.summary,
                },
            )
        return result
