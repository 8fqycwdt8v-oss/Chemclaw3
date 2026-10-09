"""Which dead turns can be taken up again from their checkpoint, and from where.

`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`. A pod killed between two steps of
a turn leaves the graph's state in the checkpointer and the question `running` in the transcript.
The turn's sender attaching again resumes it; this module answers whether it may, and hands the
runner the messages the dead attempt had already committed.

A turn is resumable when every one of these holds, and a refusal says which did not:

- its lease lapsed with no successor, it has not been resumed before, it is younger than
  `service_turn_timeout_seconds`, and nothing booked its outcome (`LapsedTurn.eligible`);
- its question is still in the checkpointed thread, identified by `question_message_id`, with
  nothing after it but the model's tool calls and their results;
- **it has not acted**: no call in the thread, finished or in flight, is side-effecting. A call in
  flight at the kill may or may not have taken effect and its audit row died with the pod, so the
  turn ends as `interrupted` instead of repeating it. Calls that only read are repeated freely.

Pure judgement is `judge`; the store reads are on the history provider.
"""

import logging
from collections.abc import Sequence
from typing import Literal, NamedTuple, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from chemclaw.agent.authz import changes_the_conversation, side_effecting_call
from chemclaw.agent.checkpointer import checkpointer
from chemclaw.agent.session_store import LapsedTurn
from chemclaw.agent.state import turn_config
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

#: Why a turn that could not be resumed was not. A closed set: it labels a counter.
Refusal = Literal[
    "ineligible",
    "no_checkpoint",
    "unreadable_checkpoint",
    "question_not_in_thread",
    "thread_moved_on",
    "unpaired_tool_calls",
    "acted",
]


class ResumePoint(NamedTuple):
    """Everything the runner needs to continue a dead turn as the turn it was."""

    row_id: int
    correlation_id: str
    question: str
    dry_run: bool
    #: The messages the dead attempt committed after the question, oldest first.
    tail: tuple[BaseMessage, ...]


def question_message_id(correlation_id: str) -> str:
    """The id a turn gives the chemist's message in the thread, so a later reader can find it."""
    return f"q:{correlation_id}"


def judge(
    messages: Sequence[BaseMessage], correlation_id: str
) -> tuple[BaseMessage, ...] | Refusal:
    """The turn's committed tail if it may be resumed, else the first reason it may not.

    Only the thread decides: the tail after the question must be tool calls and their results, the
    last assistant message may still wait for results, and no call may be side-effecting.
    """
    wanted = question_message_id(correlation_id)
    index = next(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.id == wanted), None
    )
    if index is None:
        return "question_not_in_thread"
    tail = tuple(messages[index + 1 :])
    answered = {m.tool_call_id for m in tail if isinstance(m, ToolMessage)}
    assistant = [m for m in tail if isinstance(m, AIMessage)]
    for message in tail:
        if isinstance(message, HumanMessage) or not isinstance(message, AIMessage | ToolMessage):
            return "thread_moved_on"
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            name, arguments = str(call.get("name") or ""), call.get("args") or {}
            if side_effecting_call(name, arguments) or changes_the_conversation(name):
                return "acted"
            if call.get("id") not in answered and message is not assistant[-1]:
                return "unpaired_tool_calls"
    return tail


async def resume_point(session_id: str, turn: LapsedTurn) -> ResumePoint | Refusal:
    """Read the dead turn's checkpoint and judge it; the point to resume from, or why not."""
    if not turn.eligible:
        return "ineligible"
    saver = await checkpointer()
    if saver is None:
        return "no_checkpoint"
    try:
        found = await saver.aget_tuple(cast(RunnableConfig, turn_config(session_id)))
    except Exception:
        logger.warning(
            "could not read session %s's checkpoint to resume turn %s",
            session_id,
            turn.correlation_id,
            exc_info=True,
        )
        return "unreadable_checkpoint"
    if found is None:
        return "no_checkpoint"
    messages = found.checkpoint.get("channel_values", {}).get("messages") or []
    verdict = judge(messages, turn.correlation_id)
    if isinstance(verdict, str):
        return verdict
    return ResumePoint(turn.row_id, turn.correlation_id, turn.question, turn.dry_run, verdict)


def refused(reason: Refusal, session_id: str, turn: LapsedTurn) -> None:
    """Count and log a turn that stays `interrupted`, unless it was never a candidate."""
    if reason == "ineligible":
        return
    METRICS.increment("chemclaw_turn_resume_refused_total", labels={"reason": reason})
    logger.info(
        "session %s's turn %s will not be resumed: %s", session_id, turn.correlation_id, reason
    )
