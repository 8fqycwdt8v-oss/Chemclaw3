"""The compiled middleware sequence, pinned — the instrument the `create_deep_agent` swap needed.

**Written before the swap, and it earned its place during it.** Every other change in this
workstream fails loudly: delete a module and the prose gate reddens, add a filesystem tool and the
cache-floor ratchet reddens, break the audit ordering and the MCP tests redden. The swap is the one
change whose failure modes are *silent*, and it produced one — the first version compiled a helper
through `create_deep_agent` with an empty roster, which is not what "no helpers" means to upstream:
with no spec claiming the general-purpose name it inserts its own, so the recursion guard grew an
ungoverned `task` surface one level down. Reading the compiled list is what found it.
`tests/test_subagents.py` is where that property now lives.

The hazards this file exists for, all three still live:

- **`_apply_custom_middleware` splices by `.name`.** An entry whose name matches one upstream
  already composed replaces it *in place*; a new name lands after the last core member. So this is
  the difference between `FilesystemMiddleware` withholding `execute`/`delete` and a second one
  sitting beside upstream's offering them anyway.
- **The governance wrappers must stay inside every middleware that registers a tool.** They arrive
  as new names, so their position is decided by upstream's splice rule rather than by this
  repository's list order — an arrangement that is correct today and is not promised.
- **Two skills middlewares.** Upstream composes one only when `skills=` is passed, which is why it
  is not; the failure mode if that changes is a cached role-narrowed listing shadowing a re-narrowed
  one, whose only symptom is a chemist occasionally offered a skill their role no longer holds.

None of those turns a test red on its own. This file is what makes them reviewable: the order is
asserted at construction, and the *effect* of the order is asserted by running a tool through the
compiled graph. Both halves are needed. Order alone is the shape-without-effect failure
`tasks/lessons.md` rule 27 names; effect alone would not notice a second skills middleware.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.audit import AuditEvent, AuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from tests.fakes import scripted

# The sequence as it compiles today, outermost first. `create_agent` nests `wrap_tool_call` in list
# order, so position here *is* nesting depth: entry 0 sees a tool call before entry 1 does.
#
# Recorded rather than derived, and the point is that changing it must be deliberate. Entries 0–3
# are upstream's own core stack, in upstream's order; 4 onwards are this repository's, and they land
# where `_apply_custom_middleware` puts a new name — immediately after the last core member. Three
# positions carry an argument that is not obvious from the name:
#
# - `FilesystemMiddleware` is *this repository's*, occupying upstream's slot by sharing its name.
#   That is what withholds `execute` and `delete`.
# - the `wrap_tool_call` wrappers sit inside it and inside `SubAgentMiddleware`, so a
#   scratchpad write and a `task` spawn cross the audit row and the authorization gate exactly like
#   any other tool call.
# - `AnthropicPromptCachingMiddleware` is last because it too replaces an upstream entry in place,
#   and upstream's sits in the tail after the compaction group — behind even the model-call
#   observers, which are new names and land with the rest of this repository's block. The two do
#   not contend: caching marks the system prompt and tool schemas, which compaction never touches.
# - `enforce_loop_cap` appears on *every* build, harness or not
#   (`D-2026-08-27-the-cap-is-a-property-of-the-loop-not-of-the-mode`): the runaway it bounds is
#   the model-call loop itself, which exists in both modes. Its position carries no nesting
#   argument — it is a model-call hook, not a tool gate.
# - `enforce_spend_cap` and `MeterTurnSpend` are the same guard in the unit that costs money, and
#   they travel with it for the same reason. The pair is split because the two halves cannot live
#   in one hook: only the *response* carries the bill, so metering is a `wrap_model_call`, while
#   enforcement must be a `before_model` — an `after_model` counter is short-circuited by any
#   middleware that jumps from there
#   (`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`). `MeterTurnSpend`'s
#   position among the `wrap_model_call` middlewares carries no argument either: it reads
#   `usage_metadata` off the response and passes it through, so nothing it does depends on what is
#   nested inside it.
_EXPECTED_ORDER = (
    "FilesystemMiddleware",
    "SubAgentMiddleware",
    "SummarizationMiddleware",
    "PatchToolCallsMiddleware",
    "enforce_loop_cap",
    "enforce_spend_cap",
    "MeterTurnSpend",
    "ReloadingSkillsMiddleware",
    "surface_authorization_denials",
    "surface_domain_errors",
    # Inside both converters and outside the trail
    # (`D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so`): a refusal this system
    # composed must not be wrapped in the envelope the instructions call evidence, and the two
    # readers that record a failure — the announcer and the audit trail — must read the result the
    # tool actually returned rather than the one the model is shown.
    "frame_connector_results",
    # Inside the framing and outside the trail, for the two reasons the framing itself is: the
    # envelope must wrap an already-bounded payload rather than lose its closing tag to the cut,
    # and `audit_events.detail` must keep recording what the tool returned rather than what the
    # model was shown (`agent/tool_result_size.py`).
    "bound_tool_results",
    "announce_tool_failures",
    "audit_tool_calls",
    "enforce_tool_authz",
    "refuse_writes_on_dry_run",
    "refuse_repeated_calls",
    # Outermost of the compaction group: a `ContextEdit` sees a message list and a counter, never
    # the request, so the prefix it must budget against can only be published by a middleware above
    # the editor (`agent/context_budget.py`).
    "MeasureRequestPrefix",
    "ContextEditingMiddleware",
    "RecordContextCompaction",
    # The two model-call observers, innermost of this repository's block and therefore closest to
    # the provider call (`D-2026-08-27-a-refusal-is-not-a-crash`). Below the compaction group
    # deliberately: the context edits also run in `wrap_model_call`, so recording from above them
    # would fold this repository's own token counting into the histogram an operator reads as "how
    # slow is the endpoint". The repair is outside the recorder so a repaired turn books both model
    # calls, which is what happened.
    "RepairInvalidToolCalls",
    "RecordModelCalls",
    "AnthropicPromptCachingMiddleware",
)


def _middleware_names(**kwargs: Any) -> list[str]:
    """Build an agent and report the middleware `create_agent` was finally handed, in order.

    Captured at the call rather than read off the compiled graph, because a `CompiledStateGraph`
    exposes its nodes and not the middleware that produced them — so the list is only observable
    where it is passed.

    **Patched inside `deepagents.graph`, which is the whole point of the file.** The list this
    repository passes to `create_deep_agent` is not the list that compiles: upstream splices it into
    a stack of its own by `.name`. Spying on `build_langgraph_agent`'s own argument would assert
    what this repository *asked for*, which is exactly the half that has never been in doubt.

    The helper compiled for the `task` tool goes through `langchain.agents.create_agent` directly,
    so it does not pass this spy — `tests/test_subagents.py` covers it, and keeping it out of this
    capture is why one build yields one list.
    """
    from langchain.agents import create_agent as real

    captured: list[str] = []

    def spy(*args: Any, **call_kwargs: Any) -> Any:
        for entry in call_kwargs.get("middleware", ()):
            name = getattr(entry, "name", None) or getattr(entry, "__name__", None)
            captured.append(name or type(entry).__name__)
        return real(*args, **call_kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("deepagents.graph.create_agent", spy)
        build_langgraph_agent(
            model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])), **kwargs
        )
    return captured


def test_the_middleware_sequence_is_the_recorded_one() -> None:
    """The order is load-bearing, so it is a value a reviewer adjudicates rather than a side effect.

    A change here is not necessarily wrong — it is necessarily *deliberate*. The failure this
    catches is the one that has no other symptom: a middleware that arrives beside the one it was
    meant to replace, or a governance wrapper that ends up outside the thing it was meant to wrap.
    """
    assert tuple(_middleware_names()) == _EXPECTED_ORDER


def test_every_governance_wrapper_sits_inside_the_capability_middleware() -> None:
    """The property the order exists for, stated independently of the exact sequence.

    Written as a relation rather than a list so it survives a deliberate reordering: whatever else
    moves, a tool call must cross the audit row and the authorization gate, and the middleware that
    *registers* tools must be outside them — otherwise a filesystem write or a `task` call would
    execute through a chain that never saw it.
    """
    names = _middleware_names()
    registrars = [
        i for i, n in enumerate(names) if n in {"FilesystemMiddleware", "SubAgentMiddleware"}
    ]
    assert registrars, "no tool-registering middleware found — has the composition changed?"
    for gate in ("audit_tool_calls", "enforce_tool_authz", "refuse_writes_on_dry_run"):
        assert names.index(gate) > max(registrars), (
            f"{gate} is outside the middleware that registers tools, so the tools they add would "
            "execute without crossing it"
        )


def test_the_skills_middleware_appears_exactly_once() -> None:
    """The `.name` splice hazard, asserted directly.

    `ReloadingSkillsMiddleware` exists because upstream caches its listing across turns while this
    repository's listing is narrowed by the caller's role. Two of them in one chain means the
    narrowed one is shadowed by a cached one, and the only symptom is a stale skill offer.
    """
    names = _middleware_names()
    skills = [n for n in names if "Skills" in n]
    assert skills == ["ReloadingSkillsMiddleware"], (
        f"expected exactly one skills middleware, found {skills}. Upstream's caches its listing "
        "across turns; the subclass re-narrows it per caller."
    )


def test_the_filesystem_middleware_is_the_one_that_withholds_the_shell() -> None:
    """The name-splice, asserted on the artifact rather than on the intent.

    This assertion replaces one that pinned `SubAgentMiddleware` as an *absence*, which was true
    while nothing spawned a helper and stopped being true the moment `create_deep_agent` arrived:
    it composes that middleware unconditionally and `_apply_excluded_middleware` raises rather than
    let a profile strip it. What is worth pinning now is the entry that *replaced* an upstream one.

    Exactly one `FilesystemMiddleware`, and its tool set is the narrowed one. Two would mean this
    repository's landed beside upstream's instead of in its slot, and upstream's registers all eight
    verbs — so `execute` (a shell) and `delete` (which decides what judgment the next turn can load)
    would both be reachable while every other test stayed green.
    """
    from chemclaw.agent.scratchpad import scratchpad_tools

    names = _middleware_names()
    assert names.count("FilesystemMiddleware") == 1
    graph = build_langgraph_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    )
    bound = set(graph.nodes["tools"].bound.tools_by_name)
    assert not {"execute", "delete"} & bound, (
        "upstream's filesystem middleware is registering its full verb set: this deployment "
        "withholds the shell and the delete verb, and the narrowing is carried by replacing that "
        "middleware by name"
    )
    assert set(scratchpad_tools()) <= bound


class _Recording(AuditSink):
    """An audit sink that keeps what it was given, so a test can read the trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_a_filesystem_write_crosses_the_audit_trail() -> None:
    """The effect the ordering exists for, on the tools the scratchpad added.

    This is the half that a list cannot give: `write_file` is registered by upstream middleware, not
    by this repository, so "the chain wraps our tools" is not the same claim as "the chain wraps the
    tools upstream added". A scratchpad write that never reached the audit row would be a durable
    side effect with no record that it happened.
    """
    sink = _Recording()

    graph = build_langgraph_agent(
        model=scripted("write_file", {"file_path": "/scratch/notes.md", "content": "hello"}),
        profile=AgentProfile(name="default"),
        audit_sink=sink,
    )
    asyncio.run(graph.ainvoke({"messages": [("user", "write a note")]}, {"recursion_limit": 25}))
    written = [event for event in sink.events if event.tool == "write_file"]
    assert written, (
        "a scratchpad write reached no audit row: the governance chain does not wrap the tools "
        f"upstream middleware registers. Recorded: {[e.tool for e in sink.events]}"
    )
