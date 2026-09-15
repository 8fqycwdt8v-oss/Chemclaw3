"""A workflow the agent composed is read-only, and that is what keeps the plan gate's exemption.

The security argument is the subject of this file, not the storage. `D-2026-08-12-a-template-is-
the-plan-so-the-step-is-read-only` exempts a template's `agent` step from the plan gate because the
file *"is authored by a person, committed to git and reviewed, and nothing at run time can produce
one"*. `compose_workflow` produces one at run time, so either the exemption has to be re-argued or
the premise restored. It is restored: an agent-authored workflow may name no side-effecting tool,
no durable job and no `write_tools`, so the exemption is never reached.

**Both directions**, always. A refusal that also refuses the legitimate case is not a control, it
is an outage with a good docstring — so every refusal below is paired with the composition it must
still allow.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.templates.composed import (
    ComposedStore,
    ComposedWorkflow,
    InMemoryComposedStore,
    PostgresComposedStore,
    authored_problems,
    default_composed_store,
)
from chemclaw.templates.manifest import Template
from chemclaw.templates.schedule import schedule
from tests.pg import migrated_db_or_skip

_BACKENDS = ("memory", "postgres")


async def _backend(name: str) -> ComposedStore:
    """A fresh store of the named kind, skipping when no database is reachable."""
    if name == "postgres":
        await migrated_db_or_skip()
        return PostgresComposedStore()
    return InMemoryComposedStore()


def _document(steps: list[dict[str, Any]], name: str = "probe") -> Template:
    """A composed document with `steps`, through the same model a YAML file parses into."""
    return Template.model_validate(
        {
            "name": name,
            "summary": "A composed probe.",
            "inputs": [{"name": "smiles", "type": "string", "description": "the molecule"}],
            "steps": steps,
        }
    )


_READ_STEP = {
    "id": "forms",
    "kind": "tool",
    "tool": "enumerate_tautomers",
    "arguments": {"smiles": "${inputs.smiles}"},
}
# Referring to nothing on purpose: these fixtures are combined with different first steps, and a
# reference is a dependency the forward-reference validator enforces. The chained case has its
# own steps below, where the reference is part of what is being asserted.
_REASON_STEP = {"id": "say", "kind": "agent", "prompt": "summarise what the earlier steps found"}


# --- the rule ------------------------------------------------------------------------------------


def test_a_composed_workflow_of_reads_is_allowed() -> None:
    """The control arm, first: the rule has to leave the thing the feature is for.

    Without this every assertion below is satisfied by refusing everything, which is a control that
    reads well and delivers nothing.
    """
    assert authored_problems(_document([_READ_STEP, _REASON_STEP]), side_effecting_tools()) == []


def test_a_composed_workflow_may_not_call_a_tool_that_changes_anything() -> None:
    """The sharp one, because this is the step kind the plan gate genuinely does not see.

    A `tool` step runs through `invoke_governed` under the requester's identity, so
    `enforce_tool_authz` still decides — but `enforce_plan_approval` returns early when there is no
    session (*"No session means no plan to approve"*), which is right for a reviewed procedure and
    wrong for one the agent wrote a moment ago. Nothing else in the chain asks whether a human saw
    this sequence.
    """
    write = {"id": "note", "kind": "tool", "tool": "record_knowledge_note", "arguments": {}}

    problems = authored_problems(_document([write, _REASON_STEP]), side_effecting_tools())

    assert len(problems) == 1
    assert "record_knowledge_note" in problems[0]
    # The refusal says where the change *can* be made, because a refusal that only says no leaves
    # the model to retry the same call in a different shape.
    assert "plan gate applies" in problems[0]


def test_a_composed_workflow_may_not_launch_a_durable_job() -> None:
    """Every job launcher is side-effecting, and a job spends real compute on nobody's review."""
    job = {"id": "rank", "kind": "job", "job": "rank_species", "arguments": {}}

    problems = authored_problems(_document([job, _REASON_STEP]), side_effecting_tools())

    assert len(problems) == 1
    assert "rank_species" in problems[0]


def test_a_composed_workflow_may_not_declare_write_tools() -> None:
    """The one the exemption's own wording is about.

    `step_profile` removes every side-effecting tool from a step's graph *unless the step declares
    it*, so a `write_tools:` line is precisely the lever that would put a write back into an
    ungated turn. Refused at the document, not filtered at run time: the tool is then absent from
    the graph by the same structural route it always was.
    """
    declaring = {
        "id": "say",
        "kind": "agent",
        "prompt": "write it up",
        "write_tools": ["record_knowledge_note"],
    }

    problems = authored_problems(_document([_READ_STEP, declaring]), side_effecting_tools())

    assert len(problems) == 1
    assert "record_knowledge_note" in problems[0]


def test_every_refusal_is_reported_rather_than_the_first() -> None:
    """A model told about one problem at a time re-composes once per problem."""
    job = {"id": "rank", "kind": "job", "job": "rank_species", "arguments": {}}
    write = {"id": "note", "kind": "tool", "tool": "record_knowledge_note", "arguments": {}}

    assert (
        len(authored_problems(_document([job, write, _REASON_STEP]), side_effecting_tools())) == 2
    )


def test_the_rule_reads_the_deployment_it_is_asked_about() -> None:
    """Which is why `side_effecting_tools()` is a parameter and not an import.

    The compose-time check and the run-time check happen at different times against possibly
    different deployments, and that set *grows* when a bundle is enabled — so a name that was a
    read when the workflow was composed can be a write by the time it runs, and only the run-time
    check with the run's own set can notice.
    """
    document = _document([_READ_STEP, _REASON_STEP])

    assert authored_problems(document, frozenset()) == []
    assert authored_problems(document, frozenset({"enumerate_tautomers"})) != []


# --- the document is a template, so it gets every template rule for free -------------------------


def test_a_composed_workflow_is_scheduled_like_any_other_template() -> None:
    """Composed through the same `Template` model, so concurrency is derived here too.

    The point of reusing the model rather than writing a parallel one: a composed workflow whose
    steps do not read each other runs them at the same time, with no code in this seam saying so.
    """
    independent = _document(
        [
            {
                "id": "a",
                "kind": "tool",
                "tool": "enumerate_tautomers",
                "arguments": {"smiles": "C"},
            },
            {"id": "b", "kind": "tool", "tool": "describe_topology", "arguments": {"smiles": "C"}},
            {"id": "say", "kind": "agent", "prompt": "${steps.a.result} ${steps.b.result}"},
        ]
    )

    assert [len(wave) for wave in schedule(independent)] == [2, 1]


def test_a_composed_workflow_cannot_refer_forward() -> None:
    """The validator a YAML file gets applies to a composed document unchanged."""
    with pytest.raises(ValueError, match="not the result of an earlier step"):
        _document([{"id": "say", "kind": "agent", "prompt": "${steps.later.result}"}])


# --- the store -----------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_workflow_round_trips_and_re_composing_replaces_it(backend: str) -> None:
    """Both real backends prove the same claim, which is what makes the switch below safe."""

    async def _drive() -> None:
        store = await _backend(backend)
        owner = f"chemist-{backend}"
        first = ComposedWorkflow(
            owner=owner, name="triage", summary="one", document=_document([_REASON_STEP], "triage")
        )
        await store.save(first)

        read = await store.get(owner, "triage")
        assert read is not None
        assert read.summary == "one"
        # The document comes back as a `Template`, revalidated rather than trusted: the row may
        # have been written by an earlier release, and a shape that no longer parses should refuse
        # here rather than reach the sequencer half-understood.
        assert isinstance(read.document, Template)

        await store.save(first.model_copy(update={"summary": "two"}))
        again = await store.get(owner, "triage")
        assert again is not None and again.summary == "two"
        assert [row.name for row in await store.list_for(owner)] == ["triage"]

    asyncio.run(_drive())


@pytest.mark.parametrize("backend", _BACKENDS)
def test_one_owners_workflows_are_not_another_owners(backend: str) -> None:
    """The reason the key is `(owner, name)`.

    Two chemists are each entitled to their own "triage", and a name that resolved across owners
    would let one silently run steps the other wrote — which is a worse failure than a collision,
    because nothing about the result would look wrong.
    """

    async def _drive() -> None:
        store = await _backend(backend)
        mine = f"a-{backend}"
        theirs = f"b-{backend}"
        await store.save(
            ComposedWorkflow(owner=mine, name="t", document=_document([_REASON_STEP], "t"))
        )

        assert await store.get(theirs, "t") is None
        assert await store.list_for(theirs) == []

    asyncio.run(_drive())


def test_both_backends_satisfy_the_declared_protocol() -> None:
    """The boilerplate that stops the protocol drifting from what either backend actually offers."""
    assert isinstance(InMemoryComposedStore(), ComposedStore)
    assert isinstance(PostgresComposedStore(), ComposedStore)


def test_the_default_store_follows_the_session_store_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One decision about durability, not a second setting answering it differently."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(default_composed_store(), InMemoryComposedStore)

    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_composed_store(), PostgresComposedStore)


# --- the tools -----------------------------------------------------------------------------------


@contextmanager
def _as(actor: str) -> Iterator[None]:
    """Bind an ambient actor and an in-memory store for the duration.

    The real contextvar rather than a patched getter: `require_actor` calls
    `identity_context.get_current_actor`, so patching a module attribute would leave the function
    it actually calls untouched — the shape that makes a test pass while the production path reads
    nobody.
    """
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity

    tokens = set_current_identity(actor, frozenset())
    try:
        yield
    finally:
        reset_current_identity(tokens)


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tool test below runs against the in-memory store, which is a real backend."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "session_store", "memory")


def _compose(**kwargs: Any) -> str:
    """Call the real tool."""
    from chemclaw.agent.workflow_tools import compose_workflow

    return str(asyncio.run(compose_workflow(**kwargs)))


def test_composing_a_read_only_workflow_stores_it_and_says_how_to_run_it() -> None:
    """The happy path through the real tool, not through `authored_problems` alone."""
    from chemclaw.agent.workflow_tools import WorkflowInput, WorkflowStep

    with _as("chemist-1"):
        answer = _compose(
            name="tautomer-brief",
            summary="Enumerate tautomers and say which dominates.",
            inputs=[WorkflowInput(name="smiles", description="the molecule")],
            steps=[
                WorkflowStep(
                    id="forms",
                    tool="enumerate_tautomers",
                    arguments={"smiles": "${inputs.smiles}"},
                ),
                WorkflowStep(id="say", prompt="which form: ${steps.forms.result}"),
            ],
        )
        stored = asyncio.run(default_composed_store().get("chemist-1", "tautomer-brief"))

    assert "tautomer-brief" in answer
    assert "run_composed_workflow" in answer
    assert stored is not None
    assert [step.id for step in stored.document.steps] == ["forms", "say"]


def test_composing_a_workflow_that_writes_is_refused_and_stores_nothing() -> None:
    """Refused at the tool, so nothing is stored that a later run would have to refuse again."""
    from chemclaw.agent.workflow_tools import WorkflowStep
    from chemclaw.templates.composed import ComposedWorkflowError

    with _as("chemist-2"):
        with pytest.raises(ComposedWorkflowError, match="record_knowledge_note"):
            _compose(
                name="write-it",
                summary="Write a note.",
                inputs=[],
                steps=[
                    WorkflowStep(id="note", tool="record_knowledge_note", arguments={}),
                    WorkflowStep(id="say", prompt="done"),
                ],
            )
        assert asyncio.run(default_composed_store().get("chemist-2", "write-it")) is None


def test_running_a_name_you_do_not_have_names_the_ones_you_do() -> None:
    """The listing rides on this refusal rather than costing a third tool schema.

    A name is already wrong on the turn this fires, which is exactly when knowing the real ones is
    worth a tool result — while a `list_composed_workflows` tool would be re-sent on every model
    call to answer a question asked once.
    """
    from chemclaw.agent.workflow_tools import WorkflowStep, run_composed_workflow
    from chemclaw.templates.composed import ComposedWorkflowError

    with _as("chemist-3"):
        _compose(name="mine", summary="s", inputs=[], steps=[WorkflowStep(id="say", prompt="hi")])
        with pytest.raises(ComposedWorkflowError, match=r"\['mine'\]"):
            asyncio.run(run_composed_workflow(name="theirs", inputs={}))


def test_a_stored_workflow_whose_tool_became_a_write_is_refused_at_run_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the rule is checked twice rather than once.

    `side_effecting_tools()` grows when a bundle is enabled, so a tool that was a read when the
    workflow was composed can be a write by the time it runs. Driven by widening that set between
    the two calls, which is what enabling a bundle does.
    """
    from chemclaw.agent import workflow_tools
    from chemclaw.templates.composed import ComposedWorkflowError

    with _as("chemist-4"):
        _compose(
            name="reads",
            summary="s",
            inputs=[],
            steps=[
                workflow_tools.WorkflowStep(
                    id="forms", tool="enumerate_tautomers", arguments={"smiles": "C"}
                ),
                workflow_tools.WorkflowStep(id="say", prompt="ok"),
            ],
        )
        monkeypatch.setattr(
            workflow_tools, "side_effecting_tools", lambda: frozenset({"enumerate_tautomers"})
        )
        with pytest.raises(ComposedWorkflowError, match="cannot run here any more"):
            asyncio.run(workflow_tools.run_composed_workflow(name="reads", inputs={}))
