"""One turn's model behaviour, written once and injected where a real graph would go.

**Why this module exists.** `run_turn` once took a built agent object *and* a `graph_factory` and
used exactly one of them, and sixteen test files were written when only the first existed — so they
handed `run_turn` a fake agent and let `graph_factory` keep its production default, the real
`build_langgraph_agent`, which needs a live model credential. Measured on 2026-08-11: selecting the
graph engine with nothing else changed turned 67 of those tests into `RuntimeError:
ANTHROPIC_API_KEY is not set`, and left the stall-and-cancel cases waiting on an agent that was
never run. The asymmetry was the whole finding: one seam was injectable and the other effectively
was not. That argument outlived the engine it was made about — `graph_factory` is now the only seam
a turn can be driven through, so it has to stay one.

**What it replaces, and why not the alternatives.** Two cheaper shapes were rejected:

- *A conftest default that swaps `build_langgraph_agent` for a scripted graph.* It would turn the
  failures green without re-pointing anything: the graph would answer with whatever the default
  script said, which has no relation to the behaviour each test was written to pin. Tests that
  pass for a reason unrelated to their assertion are worse than tests that fail.
- *A `graph_factory=` written out at each of the ~40 call sites.* That is forty chances to build a
  graph slightly differently, and it leaves each test's model behaviour stated twice — free to
  drift apart. `tests/fakes.py` records what that costs: twenty hand-written update fakes drifted
  until the runner's approval branch was covered by none of them.

So a turn's behaviour is written **once**, as an async generator of streamed pieces, and this class
renders it into a real compiled graph over a model that replays them. A test asserts on the events
`run_turn` yields, which is the contract, rather than on a shape it invented for a fake.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult


class Chunk:
    """One streamed fragment of the model's reply, in terms both engines can render.

    `text` is what reaches `TokenEvent`; the token counts are what reach the turn's usage ledger.
    They are one object rather than two streams because a provider reports usage *on* a chunk, and
    a test that could only say "some text" or "some usage" could not pin the ordering between them
    — which is precisely what the cancellation suite asserts about an abandoned turn.

    Input and output are separate because the ledger prices them separately (`TurnUsage`), and both
    engines report both; collapsing them into one total would make the cost-row assertions
    untestable on either.

    A bare `str` is accepted anywhere a `Chunk` is, meaning "this text, no usage reported": most
    fakes never mention tokens, and making them say `Chunk(text=...)` would bury the few that do.
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
    """A turn's model behaviour, exposed as both engines' injection points.

    Subclass and implement `stream`; the base supplies `graph_factory`, which is what
    `run_turn(graph_factory=…)` calls. It briefly had a second face, for the other engine's
    injection point, so one test body could cover both without a branch in it. That face went with
    the engine; what it bought — the behaviour stated once — is why this one exists.
    """

    @abstractmethod
    def stream(self, message: str) -> AsyncIterator[Piece]:
        """This turn's reply to `message`, as the pieces the model streams.

        Implemented as an `async def` generator, so anything a real turn does between chunks — set
        an `asyncio.Event`, record a turn signal, block forever, raise — is written the way it
        would be written for either engine. `message` is passed because a resume drives this a
        second time with the framed job results, and some tests assert on what arrived.
        """

    def graph_factory(self, **build_kwargs: Any) -> Any:
        """The graph face: the real agent, compiled over a model that replays the same pieces.

        A *real* `build_langgraph_agent` rather than a stand-in for a compiled graph, because the
        thing under test is the runner driving an engine — middlewares, tool node and
        `chemclaw.api.graph_stream` included. Only the model is faked, which is the one component a
        test cannot have.

        `build_kwargs` is whatever `run_turn` passes (profile, actor, correlation id, the turn's
        connectors, the checkpointer, its audit sink), forwarded untouched so the graph a test
        drives is the graph production builds. The audit sink is the one thing overridden: a test
        process has no database, and a durable sink would reach for one on every tool call. The
        runner now passes its own (`default_audit_sink()`, so it can flush the batching sink at
        turn end); under the test settings that resolves to a `NullAuditSink`, and this override
        keeps the graph on a null sink even for a test that flips `session_store`.
        """
        from chemclaw.agent.audit import NullAuditSink
        from chemclaw.agent.langgraph_agent import build_langgraph_agent

        build_kwargs["audit_sink"] = NullAuditSink()
        return build_langgraph_agent(_ReplayingChatModel(turn=self), **build_kwargs)


def _graph_chunk(chunk: Chunk) -> AIMessageChunk:
    """One piece as a LangChain message chunk, with usage in the shape that adapter reports it.

    No `input_token_details`, deliberately: this fake reports no caching, so `graph_usage_tokens`
    subtracts nothing and the split it produces is the split the chunk stated.
    """
    if not chunk.tokens:
        return AIMessageChunk(content=chunk.text)
    return AIMessageChunk(
        content=chunk.text,
        usage_metadata={
            "input_tokens": chunk.input_tokens,
            "output_tokens": chunk.output_tokens,
            "total_tokens": chunk.tokens,
        },
    )


class _ReplayingChatModel(BaseChatModel):
    """A chat model whose single reply is a `ScriptedTurn`'s pieces, streamed.

    Private to this module: it exists only to give `ScriptedTurn.graph_factory` something to
    compile, and a test that wants a *scripted* model (a fixed sequence of tool calls and answers)
    wants `tests.fakes_langgraph.ScriptedChatModel` instead. The difference is which end holds the
    control flow — there, the script; here, the test's own generator.
    """

    turn: Any

    @property
    def _llm_type(self) -> str:
        """The identifier LangChain stamps on runs from this model."""
        return "chemclaw-scripted-turn"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding the agent's model node always performs and keep replaying.

        `create_agent` binds on every request, and `BaseChatModel.bind_tools` raises
        `NotImplementedError` — so without this the graph cannot be driven at all, faked model or
        not.
        """
        return self

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Replay the turn's pieces as this reply's chunks."""
        async for piece in self.turn.stream(_last_human_text(messages)):
            yield ChatGenerationChunk(message=_graph_chunk(_chunk(piece)))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Refuse the non-streaming path, which nothing in a turn takes.

        `BaseChatModel` requires it; `ainvoke` routes through `_astream` because this class
        overrides it, and a turn is always streamed. Raising says so rather than quietly returning
        an empty reply, which is how a stream-shape regression would otherwise look like a model
        that had nothing to say.
        """
        raise NotImplementedError("a scripted turn is streamed; `run_turn` never invokes it whole")


def _last_human_text(messages: Sequence[BaseMessage]) -> str:
    """The user message this reply answers — the same string `run_turn` was handed.

    Read off the end rather than the start because the graph's message list opens with the system
    prompt and, on a resume, carries the whole first half of the turn before the framed job
    results.
    """
    for message in reversed(messages):
        if message.type == "human":
            return str(message.content)
    return ""
