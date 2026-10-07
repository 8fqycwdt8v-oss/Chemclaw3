"""One turn's model behaviour, written once and injected where a real graph would go.

`graph_factory` is the seam a turn is driven through. A turn's behaviour is written once, as an
async generator of streamed pieces, and `ScriptedTurn` renders it into a real compiled graph over
a model that replays them. Tests assert on the events `run_turn` yields, which is the contract.
A conftest-wide default script or a per-call-site factory would each let a test pass for reasons
unrelated to its assertion.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult


class Chunk:
    """One streamed fragment of the model's reply: text plus the usage it reports.

    One object so a test can pin the ordering of text and usage, which the cancellation suite relies
    on. Input and output tokens are separate because the ledger prices them separately. A bare `str`
    is accepted anywhere a `Chunk` is, meaning "this text, no usage reported".
    """

    __slots__ = ("text", "input_tokens", "output_tokens")

    def __init__(self, text: str = "", input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Hold the fragment's text and the usage reported alongside it."""
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def tokens(self) -> int:
        """The chunk's total spend — what the budget guard meters."""
        return self.input_tokens + self.output_tokens


Piece = Chunk | str


def _chunk(piece: Piece) -> Chunk:
    """Normalise a streamed piece, so the two renderings below read the same shape."""
    return piece if isinstance(piece, Chunk) else Chunk(text=piece)


class ScriptedTurn(ABC):
    """A turn's model behaviour, exposed as the engine's injection point.

    Subclass and implement `stream`; the base supplies `graph_factory`, which is what
    `run_turn(graph_factory=…)` calls.
    """

    @abstractmethod
    def stream(self, message: str) -> AsyncIterator[Piece]:
        """This turn's reply to `message`, as the pieces the model streams.

        An `async def` generator, so a test can do anything between chunks (set an event, block,
        raise). `message` is passed because a resume drives this again with the framed job results.
        """

    def graph_factory(self, **build_kwargs: Any) -> Any:
        """The graph face: the real agent, compiled over a model that replays the same pieces.

        A real `build_langgraph_agent`, so middlewares, the tool node and
        `chemclaw.api.graph_stream` are all under test; only the model is faked. `build_kwargs` from
        `run_turn` is forwarded untouched, except the audit sink, which is forced to a null sink
        because a test process has no database.
        """
        from chemclaw.agent.audit import NullAuditSink
        from chemclaw.agent.langgraph_agent import build_langgraph_agent

        build_kwargs["audit_sink"] = NullAuditSink()
        return build_langgraph_agent(_ReplayingChatModel(turn=self), **build_kwargs)


def _usage_chunk(usage: Chunk) -> AIMessageChunk:
    """The reply's terminal frame: no content, carrying what the whole call reported.

    OpenAI-compatible endpoints deliver usage once, on the final frame
    (`stream_options.include_usage`), so a turn torn down before the reply finishes books nothing —
    the property `tests/test_turn_cancellation.py` depends on. No `input_token_details`: this fake
    reports no caching.
    """
    return AIMessageChunk(
        content="",
        usage_metadata={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.tokens,
        },
    )


class _ReplayingChatModel(BaseChatModel):
    """A chat model whose single reply is a `ScriptedTurn`'s pieces, streamed.

    Private: it exists for `ScriptedTurn.graph_factory`. A fixed script of tool calls and answers
    wants `tests.fakes_langgraph.ScriptedChatModel` instead.
    """

    turn: Any

    @property
    def _llm_type(self) -> str:
        """The identifier LangChain stamps on runs from this model."""
        return "chemclaw-scripted-turn"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding the agent's model node always performs and keep replaying.

        `create_agent` binds on every request, and `BaseChatModel.bind_tools` raises.
        """
        return self

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Replay the turn's pieces as this reply's chunks, then the usage frame the wire sends.

        Each `Chunk`'s counts are summed into the final frame, so a turn abandoned before it meters
        nothing, as in production.
        """
        total = Chunk()
        async for piece in self.turn.stream(_last_human_text(messages)):
            chunk = _chunk(piece)
            total.input_tokens += chunk.input_tokens
            total.output_tokens += chunk.output_tokens
            yield ChatGenerationChunk(message=AIMessageChunk(content=chunk.text))
        if total.tokens:
            yield ChatGenerationChunk(message=_usage_chunk(total))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Refuse the non-streaming path, which nothing in a turn takes.

        Raising, rather than returning an empty reply, makes a stream-shape regression visible.
        """
        raise NotImplementedError("a scripted turn is streamed; `run_turn` never invokes it whole")


def _last_human_text(messages: Sequence[BaseMessage]) -> str:
    """The user message this reply answers — the same string `run_turn` was handed.

    Read off the end: the list opens with the system prompt and, on a resume, carries the first half
    of the turn.
    """
    for message in reversed(messages):
        if message.type == "human":
            return str(message.content)
    return ""
