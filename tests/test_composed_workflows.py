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
    unapproved_jobs,
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
_JOB_STEP = {"id": "rank", "kind": "job", "job": "rank_species", "arguments": {}}
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


def test_a_job_step_is_not_refused_outright_any_more_but_is_not_authorized_either() -> None:
    """The widening, and the shape of it: composing is allowed, *running* is what waits.

    Refusing the composition was the first design and is the wrong one — a workflow nobody may
    compose is a workflow nobody can put in front of a person to approve. So `authored_problems`
    says nothing about a `job` step and `unapproved_jobs` withholds it until an approval stands.
    """
    document = _document([_JOB_STEP, _REASON_STEP])

    assert authored_problems(document, side_effecting_tools()) == []
    withheld = unapproved_jobs(document, approved_fingerprint="", fingerprint="fp-1")
    assert len(withheld) == 1
    assert "['rank']" in withheld[0]
    # The refusal names the route, because a model told only "no" retries the same call.
    assert "/workflows/{name}/approval" in withheld[0]


def test_an_approval_for_this_exact_version_releases_the_job() -> None:
    """The control arm for the widening: the approval has to actually authorize something."""
    document = _document([_JOB_STEP, _REASON_STEP])

    assert unapproved_jobs(document, approved_fingerprint="fp-1", fingerprint="fp-1") == []


def test_an_approval_for_an_earlier_version_does_not_carry_over() -> None:
    """The reason the approval is keyed on the document and not on the actor.

    A standing per-actor permission never lapses, so a workflow re-composed into something else
    would inherit the approval granted to what it used to be. Keyed on the document's own hash,
    re-composing lapses it with nothing having to remember to clear it — and the refusal says so,
    because "not approved" and "approved, but not this version" are different things to be told.
    """
    withheld = unapproved_jobs(
        _document([_JOB_STEP, _REASON_STEP]), approved_fingerprint="fp-1", fingerprint="fp-2"
    )

    assert len(withheld) == 1
    assert "earlier version" in withheld[0]


def test_a_workflow_with_no_job_steps_needs_no_approval() -> None:
    """The read-only case is unchanged by all of this, which is most composed workflows."""
    assert unapproved_jobs(_document([_READ_STEP, _REASON_STEP]), "", "fp-1") == []


def test_no_approval_lifts_a_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The line this widening is drawn on**, asserted rather than left in a docstring.

    A `job` step is bounded compute whose call the approver read in the document. `write_tools` is
    not a call at all — it is a permission handed to a model turn, spent later on a call nobody has
    seen. A person can meaningfully approve the first and cannot meaningfully approve the second,
    so an approval offers to lift only the first; `authored_problems` takes no approval argument at
    all, which is how that is enforced rather than remembered.
    """
    declaring = {
        "id": "say",
        "kind": "agent",
        "prompt": "write it up",
        "write_tools": ["record_knowledge_note"],
    }
    write_step = {"id": "note", "kind": "tool", "tool": "record_knowledge_note", "arguments": {}}

    both: list[list[dict[str, Any]]] = [[_READ_STEP, declaring], [write_step, _REASON_STEP]]
    for steps in both:
        document = _document(steps)
        # Approved to the hilt, and still refused: the approval is not an argument this can take.
        assert unapproved_jobs(document, "fp-1", "fp-1") == []
        assert authored_problems(document, side_effecting_tools()) != []


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


def test_a_workflow_with_a_job_composes_but_will_not_run_until_it_is_approved() -> None:
    """The whole loop through the real tools: compose, refused, approved, runs.

    Composing is deliberately not the decision — a workflow nobody may compose is one nobody can
    put in front of a person — so the refusal lands at the run and the compose result says so at
    the moment the chemist is still in the conversation to hear it.
    """
    from chemclaw.agent import workflow_tools
    from chemclaw.durable.template_job import template_fingerprint
    from chemclaw.templates.composed import ComposedWorkflowError

    started: list[str] = []

    with _as("chemist-jobs"):
        answer = _compose(
            name="ranking",
            summary="Rank and report.",
            inputs=[],
            steps=[
                workflow_tools.WorkflowStep(id="rank", job="rank_species", arguments={}),
                workflow_tools.WorkflowStep(id="say", prompt="which one: ${steps.rank.result}"),
            ],
        )
        # Said where the chemist can act on it, rather than only at the run that fails.
        assert "will not run yet" in answer
        assert "['rank']" in answer

        with pytest.raises(ComposedWorkflowError, match="not approved to run"):
            asyncio.run(workflow_tools.run_composed_workflow(name="ranking", inputs={}))

        store = default_composed_store()
        stored = asyncio.run(store.get("chemist-jobs", "ranking"))
        assert stored is not None
        # The human's act, through the store the route writes — there is no tool for this.
        asyncio.run(
            store.approve(
                "chemist-jobs", "ranking", template_fingerprint(stored.document), "a-person"
            )
        )

        # It runs now. The launcher is stubbed: what is under test is the gate, not Temporal.
        monkey = pytest.MonkeyPatch()
        try:

            async def _fake_start(document: Any, inputs: dict[str, Any]) -> str:
                started.append(document.name)
                return "job-1"

            monkey.setattr("chemclaw.templates.registry.start_template_run", _fake_start)
            assert asyncio.run(workflow_tools.run_composed_workflow("ranking", {})) == "job-1"
        finally:
            monkey.undo()

    assert started == ["ranking"]
