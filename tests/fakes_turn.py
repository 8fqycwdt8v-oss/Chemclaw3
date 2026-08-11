"""One turn's model behaviour, written once and played on either engine.

**Why this module exists.** `chemclaw.api.runner.run_turn` takes a built MAF `agent` *and* a
`graph_factory`, and uses exactly one of them depending on `settings.agent_engine`. Sixteen test
files were written when only the first existed, so they hand `run_turn` a fake MAF agent and let
`graph_factory` keep its production default — the real `build_langgraph_agent`, which needs a live
model credential. Measured on 2026-08-11: flipping `CHEMCLAW_AGENT_ENGINE=langgraph` with nothing
else changed turned 67 of those tests into `RuntimeError: ANTHROPIC_API_KEY is not set`, and left
the stall-and-cancel cases waiting on an agent that is never run. The asymmetry was the whole
finding: `agent` was injectable and `graph_factory` effectively was not.

**What it replaces, and why not the alternatives.** Two cheaper shapes were rejected:

- *A conftest default that swaps `build_langgraph_agent` for a scripted graph.* It would turn the
  failures green without re-pointing anything: the graph would answer with whatever the default
  script said, which has no relation to the behaviour each test was written to pin. Tests that
  pass for a reason unrelated to their assertion are worse than tests that fail.
- *A `graph_factory=` written out at each of the ~40 call sites.* That is forty chances to build a
  graph slightly differently, and it leaves each test's model behaviour stated twice — once as a
  fake MAF agent and once as a script — free to drift apart. `tests/fakes.py` records what that
  costs: twenty hand-written update fakes drifted until the runner's approval branch was covered by
  none of them.

So a turn's behaviour is written **once**, as an async generator of streamed pieces, and this
class renders it into whichever framework's shape the configured engine wants. That generator is
the same object the MAF fakes already were — their `_gen()` bodies, with the framework's update
type removed — so porting a fake is a signature change rather than a rewrite. When M13 deletes the
MAF branch, `run` goes and the rest stays.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from chemclaw.agent.chemclaw_agent import graph_engine_selected
from tests.fakes import FakeUpdate


def maf_engine_only(subject: str) -> pytest.MarkDecorator:
    """Skip on the graph engine, naming what this test pins that only the MAF engine has.

    Not every failing test wanted re-pointing. A few have no graph counterpart at all — MAF's
    `user_input_requests` approval content, the plan read through `runner.todo_titles`, the
    `open_reachable` connector lifecycle — and weakening those until they pass on both engines
    would leave a test whose name still promises something it no longer checks. Skipping with the
    subject spelled out keeps them honest and makes M13's deletion mechanical: when the branch
    goes, so does everything carrying this mark.

    Args:
        subject: What the test pins and where the graph engine's equivalent lives, in one phrase.

    Returns:
        A `skipif` mark, active only while `agent_engine` selects the graph.
    """
    return pytest.mark.skipif(graph_engine_selected(), reason=f"MAF-engine-only: {subject}")


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

    Subclass and implement `stream`; the base supplies `run` (what MAF's `agent.run` looks like
    from `run_turn`'s side) and `graph_factory` (what `run_turn(graph_factory=…)` calls). A test
    then hands the *same object* to both parameters — the turn in the `agent` slot and its own
    `graph_factory` in the other — and the engine in force decides which face is used, so one test
    body covers both without a branch in it.

    `mcp_tools` is here because MAF agents advertise it and the fakes all carried it; nothing on
    the graph path reads it.
    """

    mcp_tools: list[Any] = []

    @abstractmethod
    def stream(self, message: str) -> AsyncIterator[Piece]:
        """This turn's reply to `message`, as the pieces the model streams.

        Implemented as an `async def` generator, so anything a real turn does between chunks — set
        an `asyncio.Event`, record a turn signal, block forever, raise — is written the way it
        would be written for either engine. `message` is passed because a resume drives this a
        second time with the framed job results, and some tests assert on what arrived.
        """

    def run(
        self,
        message: str,
        *,
        stream: bool,
        session: Any,
        **_run_options: Any,
    ) -> AsyncIterator[Any]:
        """The MAF face: the same pieces, wrapped in the streamed-update shape MAF emits.

        `stream` and the run options are accepted and ignored exactly as the hand-written fakes
        accepted them — `run_turn` always streams, and what it passes besides (`tools=`) is the
        turn's connectors, which a fake has nothing to do with.
        """

        async def _updates() -> AsyncIterator[Any]:
            async for piece in self.stream(message):
                yield _maf_update(_chunk(piece))

        return _updates()

    def graph_factory(self, **build_kwargs: Any) -> Any:
        """The graph face: the real agent, compiled over a model that replays the same pieces.

        A *real* `build_langgraph_agent` rather than a stand-in for a compiled graph, because the
        thing under test is the runner driving an engine — middlewares, tool node and
        `chemclaw.api.graph_stream` included. Only the model is faked, which is the one component a
        test cannot have.

        `build_kwargs` is whatever `run_turn` passes (profile, actor, correlation id, the turn's
        connectors, the checkpointer), forwarded untouched so the graph a test drives is the graph
        production builds. The audit sink is the one thing overridden: a test process has no
        database, and `default_audit_sink()` would reach for one on every tool call.
        """
        from chemclaw.agent.audit import NullAuditSink
        from chemclaw.agent.langgraph_agent import build_langgraph_agent

        return build_langgraph_agent(
            _ReplayingChatModel(turn=self), audit_sink=NullAuditSink(), **build_kwargs
        )


def _maf_update(chunk: Chunk) -> FakeUpdate:
    """One piece as MAF's streamed update, with usage only when the piece reported some.

    A usage content that reports zero is not the same as no usage content at all — `usage_tokens`
    counts the first as `unreadable` — so a piece with no token count carries no content, which is
    what every fake that predates this module did.
    """
    if not chunk.tokens:
        return FakeUpdate(text=chunk.text)
    return FakeUpdate(
        text=chunk.text,
        contents=[
            SimpleNamespace(
                usage_details={
                    "input_token_count": chunk.input_tokens,
                    "output_token_count": chunk.output_tokens,
                }
            )
        ],
    )


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
