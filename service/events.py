"""The turn event contract (plan step F2-T3): one typed schema every surface shares.

A turn does not just return a final string — the experience is watching the agent *work*: its plan,
its tool calls, streamed tokens, a launched async job, an approval prompt, then the answer. Modeling
these as a discriminated union (on `type`) means the web UI now — and Slack/mobile later — render
the same events instead of each parsing a bespoke stream. The runner emits these; the app serializes
each as one SSE `data:` line via `model_dump_json()`.
"""

from typing import Literal

from pydantic import BaseModel


class PlanEvent(BaseModel):
    """The agent's current plan/todo list (harness mode) — rendered as a checklist."""

    type: Literal["plan"] = "plan"
    todos: list[str]


class ToolCallEvent(BaseModel):
    """A single tool invocation in the turn's trace (name + a short argument preview)."""

    type: Literal["tool_call"] = "tool_call"
    tool: str
    arguments: str = ""


class TokenEvent(BaseModel):
    """One streamed chunk of the assistant's answer text."""

    type: Literal["token"] = "token"
    text: str


class JobStartedEvent(BaseModel):
    """An async (Temporal/HPC/BO) job was launched; the UI shows "job started (id …)"."""

    type: Literal["job_started"] = "job_started"
    job_id: str


class JobCompletedEvent(BaseModel):
    """An async job finished and pushed its result back to the session (F3-T3, no polling)."""

    type: Literal["job_completed"] = "job_completed"
    job_id: str
    summary: dict[str, object] = {}


class ApprovalRequestEvent(BaseModel):
    """The turn is waiting on a human decision (plan approval or an interaction approval)."""

    type: Literal["approval_request"] = "approval_request"
    prompt: str


class AnswerEvent(BaseModel):
    """The turn's final assembled answer (the complete text, after the token stream)."""

    type: Literal["answer"] = "answer"
    text: str


class ErrorEvent(BaseModel):
    """The turn failed; the message is safe to show the user (no stack traces)."""

    type: Literal["error"] = "error"
    message: str


# The closed set of events a turn can emit. New surfaces switch on `type`; adding an event is a new
# class here plus one branch in the runner and the UI — never a bespoke per-surface stream.
Event = (
    PlanEvent
    | ToolCallEvent
    | TokenEvent
    | JobStartedEvent
    | JobCompletedEvent
    | ApprovalRequestEvent
    | AnswerEvent
    | ErrorEvent
)
