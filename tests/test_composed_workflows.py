"""A workflow the agent composed is read-only, and that is what keeps the plan gate's exemption.

A template's `agent` step is exempt from the plan gate because a person authored and reviewed the
template; `compose_workflow` produces one at run time, so an agent-authored workflow may name no
side-effecting tool and no `write_tools`. A durable `job` step is the one thing a person can
approve (`D-2026-09-15-an-approval-is-for-one-version-of-one-workflow`). Every refusal is paired
with the composition it must still allow.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.templates.composed import (
    MAX_PER_OWNER,
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
    """A composed workflow may not call a tool that changes anything.

    A `tool` step runs through `invoke_governed`, so authorization still applies, but
    `enforce_plan_approval` returns early with no session; nothing else asks whether a human saw
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
    """A `job` step may be composed, but does not run until approved.

    A workflow nobody may compose cannot be put in front of a person to approve, so
    `authored_problems` allows it and `unapproved_jobs` withholds it.
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
    """An approval for an earlier version does not carry over.

    The approval is keyed on the document's hash, so re-composing lapses it automatically, and the
    refusal says "approved, but not this version".
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
    """No approval lifts a write.

    A `job` call is visible in the document an approver reads; `write_tools` is a permission spent
    later on calls nobody has seen. `authored_problems` takes no approval argument, which enforces
    it.
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
    """The rule reads the deployment it is asked about: `side_effecting_tools()` is a parameter.

    Compose and run happen at different times; enabling a bundle grows the side-effecting set, so
    only the run-time check with the run's own set can see a read that became a write.
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
async def test_a_workflow_round_trips_and_re_composing_replaces_it(backend: str) -> None:
    """Both real backends prove the same claim, which is what makes the switch below safe."""
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


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_one_owners_workflows_are_not_another_owners(backend: str) -> None:
    """One owner's workflows are not another's: the key is `(owner, name)`.

    A name resolved across owners would silently run steps another chemist wrote.
    """
    store = await _backend(backend)
    mine = f"a-{backend}"
    theirs = f"b-{backend}"
    await store.save(
        ComposedWorkflow(owner=mine, name="t", document=_document([_REASON_STEP], "t"))
    )

    assert await store.get(theirs, "t") is None
    assert await store.list_for(theirs) == []


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

    Uses the real contextvar, because `require_actor` calls `identity_context.get_current_actor` and
    a patched module attribute would not be read.
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
    """Running a name you do not have lists the ones you do.

    The listing rides on this refusal rather than costing a tool schema on every model call.
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
    """A stored workflow whose tool became a write is refused at run time.

    Driven by widening `side_effecting_tools()` between compose and run, as enabling a bundle does.
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

    The compose result says approval is needed while the chemist is still in the conversation.
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
        asyncio.run(store.approve("chemist-jobs", "ranking", template_fingerprint(stored.document)))

        # It runs now. The launcher is stubbed: what is under test is the gate, not Temporal.
        monkey = pytest.MonkeyPatch()
        try:

            async def _fake_start(document: Any, inputs: dict[str, Any], scope: str = "") -> str:
                # `scope` is asserted, not ignored: it is what stops two chemists' `triage` — and
                # two versions of one — sharing a Temporal id and rejoining each other's runs.
                started.append(f"{document.name}|{scope}")
                return "job-1"

            monkey.setattr("chemclaw.templates.registry.start_template_run", _fake_start)
            assert asyncio.run(workflow_tools.run_composed_workflow("ranking", {})) == "job-1"
        finally:
            monkey.undo()

    assert len(started) == 1
    name, _, scope = started[0].partition("|")
    assert name == "ranking"
    assert scope.startswith("chemist-jobs:"), scope


# --- the terminal's approver, which is the only one that surface had ------------------------------


def test_the_cli_can_approve_what_the_cli_composed() -> None:
    """The CLI can approve what the CLI composed.

    Composed workflows are keyed on the ambient actor, which differs between the CLI and the front
    door in a dev deployment, so each surface's person approves on that surface, as `/approve` does
    for plans.
    """
    from chemclaw.agent.workflow_tools import WorkflowStep, run_composed_workflow
    from chemclaw.cli.chat import _workflow_command
    from chemclaw.templates.composed import ComposedWorkflowError

    # A distinct actor per test: `InMemoryComposedStore` is a process singleton (deliberately —
    # a process has one store), so tests that shared an owner would see each other's rows.
    actor = "cli-approves"
    with _as(actor):
        _compose(
            name="ranking",
            summary="Rank and report.",
            inputs=[],
            steps=[
                WorkflowStep(id="rank", job="rank_species", arguments={}),
                WorkflowStep(id="say", prompt="which: ${steps.rank.result}"),
            ],
        )
        listing = asyncio.run(_workflow_command("/workflows", actor))
        assert "needs approval" in listing
        assert "['rank']" in listing

        with pytest.raises(ComposedWorkflowError, match="not approved to run"):
            asyncio.run(run_composed_workflow(name="ranking", inputs={}))

        # Read, then approve what was read: the first command shows the procedure and its
        # fingerprint, the second binds to that fingerprint, because the agent acts in this terminal
        # under the same owner between the two commands.
        shown = asyncio.run(_workflow_command("/approve-workflow ranking", actor))
        assert "rank_species" in shown, "the approver has to be shown the call, not a step id"
        assert "/approve-workflow ranking " in shown
        fingerprint = shown.rsplit(" ", 1)[-1].strip()

        stale = asyncio.run(_workflow_command("/approve-workflow ranking not-that-hash", actor))
        assert "changed since it was shown" in stale
        assert asyncio.run(_workflow_command("/workflows", actor)).count("needs approval") == 1

        answer = asyncio.run(_workflow_command(f"/approve-workflow ranking {fingerprint}", actor))
        assert "approved 'ranking'" in answer
        assert asyncio.run(_workflow_command("/workflows", actor)).count("ready") == 1

        # And the cap's other half: a workflow the owner no longer wants is gone, rather than
        # overwritten by a re-compose under a name that would then lie about its contents.
        assert "forgot" in asyncio.run(_workflow_command("/forget-workflow ranking", actor))
        assert asyncio.run(_workflow_command("/workflows", actor)) == "(no composed workflows)"


def test_the_cli_listing_answers_the_question_a_refusal_could_not() -> None:
    """Discovery across sessions, which was a `BACKLOG.md` row until this command existed.

    `run_composed_workflow`'s refusal names the workflows an owner has, which works only once you
    have already guessed a name wrong. `/workflows` is the question asked directly.
    """
    from chemclaw.cli.chat import _workflow_command

    fresh = "cli-has-nothing"
    with _as(fresh):
        assert "no composed workflows" in asyncio.run(_workflow_command("/workflows", fresh))


def test_approving_a_name_the_caller_does_not_have_says_what_they_do_have() -> None:
    """A terminal refusal has to be actionable, because there is no UI to fall back on."""
    from chemclaw.agent.workflow_tools import WorkflowStep
    from chemclaw.cli.chat import _workflow_command

    actor = "cli-one-workflow"
    with _as(actor):
        _compose(name="mine", summary="s", inputs=[], steps=[WorkflowStep(id="say", prompt="hi")])
        answer = asyncio.run(_workflow_command("/approve-workflow theirs", actor))

    assert "no composed workflow called 'theirs'" in answer
    assert "['mine']" in answer


def test_the_cli_approval_names_the_person_and_not_the_agent() -> None:
    """`approved_by` is the typist, never the agent that composed it.

    `ComposedStore.approve` takes no approver argument, so the approver is always the owner, which
    is what lets `agent/leaver.py` erase approvals by `owner`.
    """
    from chemclaw.agent.workflow_tools import WorkflowStep
    from chemclaw.cli.chat import _workflow_command

    actor = "cli-records-approver"
    with _as(actor):
        _compose(
            name="ranking",
            summary="s",
            inputs=[],
            steps=[
                WorkflowStep(id="rank", job="rank_species", arguments={}),
                WorkflowStep(id="say", prompt="x"),
            ],
        )
        shown = asyncio.run(_workflow_command("/approve-workflow ranking", actor))
        asyncio.run(
            _workflow_command(f"/approve-workflow ranking {shown.rsplit(' ', 1)[-1]}", actor)
        )
        stored = asyncio.run(default_composed_store().get(actor, "ranking"))

    assert stored is not None
    assert stored.approved_by == actor
    assert stored.approved_at is not None


@pytest.mark.parametrize("backend", _BACKENDS)
def test_both_backends_agree_about_what_a_save_may_touch(backend: str) -> None:
    """A save carries an approval forward and can never set one — in both real backends.

    The agent's write cannot grant an approval and cannot erase the record of one; whether it still
    applies is `unapproved_jobs`' question, against the fingerprint.
    """

    async def _drive() -> None:
        store = await _backend(backend)
        owner = f"save-scope-{backend}"
        document = _document([_JOB_STEP, _REASON_STEP], "triage")
        await store.save(
            ComposedWorkflow(
                owner=owner,
                name="triage",
                summary="one",
                document=document,
                # The forgery: an approval smuggled in on the agent's own write.
                approved_fingerprint="forged",
                approved_by="somebody-who-never-approved",
            )
        )
        stored = await store.get(owner, "triage")
        assert stored is not None
        assert stored.approved_fingerprint == "", "a save must not be able to grant an approval"
        assert stored.approved_by == ""

        await store.approve(owner, "triage", "the-real-hash")
        await store.save(
            ComposedWorkflow(owner=owner, name="triage", summary="two", document=document)
        )
        after = await store.get(owner, "triage")
        assert after is not None
        assert after.summary == "two", "the document half of a save still replaces"
        assert after.approved_fingerprint == "the-real-hash", (
            "and the agent's write must not erase the record of a person's decision"
        )
        assert after.approved_by == owner
        assert after.approved_at is not None

    asyncio.run(_drive())


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_workflow_can_be_forgotten_and_forgetting_one_that_is_gone_says_so(backend: str) -> None:
    """A workflow can be forgotten, and forgetting one that is gone returns `False`.

    Without delete, the only remedy for `MAX_PER_OWNER` is overwriting a name with different
    content.
    """

    async def _drive() -> None:
        store = await _backend(backend)
        owner = f"forgetful-{backend}"
        await store.save(
            ComposedWorkflow(
                owner=owner,
                name="triage",
                summary="s",
                document=_document([_REASON_STEP], "triage"),
            )
        )
        assert await store.forget(owner, "triage") is True
        assert await store.get(owner, "triage") is None
        assert await store.forget(owner, "triage") is False
        assert await store.forget("somebody-else", "triage") is False

    asyncio.run(_drive())


def test_the_cap_can_tell_a_full_page_from_a_clamped_one() -> None:
    """The cap can tell a full page from a clamped one.

    `list_for` fetches one past `MAX_PER_OWNER`, so neither the cap guard nor the "you have" listing
    reads its own page size. Driven against Postgres, where the clamp is a SQL `LIMIT`.
    """

    async def _drive() -> None:
        await migrated_db_or_skip()
        store = PostgresComposedStore()
        owner = "at-the-cap"
        document = _document([_REASON_STEP], "probe")
        try:
            for index in range(MAX_PER_OWNER + 3):
                await store.save(
                    ComposedWorkflow(
                        owner=owner, name=f"w{index:03d}", summary="s", document=document
                    )
                )
            rows = await store.list_for(owner)
            assert len(rows) == MAX_PER_OWNER + 1, (
                "the page has to exceed the cap by one, or 'at it' and 'over it' read the same"
            )
        finally:
            for index in range(MAX_PER_OWNER + 3):
                await store.forget(owner, f"w{index:03d}")

    asyncio.run(_drive())


def test_a_step_kind_nobody_has_a_position_on_is_refused_rather_than_allowed() -> None:
    """`authored_problems` fails closed on a step kind it does not recognise.

    Driven with a stand-in class; an unhandled kind must not be exempt from the plan gate.
    `agent/template_surface.template_step_ceilings` likewise raises on an unsized kind.
    """

    class _FutureStep:
        """A step kind from next year: not a tool, not an agent step, not a job."""

        id = "surprise"
        kind = "surprise"

    document = _document([_REASON_STEP])
    # Past the model rather than through it: `Template` validates its union, and what is under test
    # is the branch that runs when something gets past a validator this function does not own.
    object.__setattr__(document, "steps", [*document.steps, _FutureStep()])

    problems = authored_problems(document, side_effecting_tools())

    assert len(problems) == 1
    assert "surprise" in problems[0]
    assert "_FutureStep" in problems[0]


def test_running_a_composed_workflow_validates_its_inputs_before_anything_is_queued() -> None:
    """Running a composed workflow validates its inputs before anything is queued.

    Validation lives in `start_template_run`, shared by both launchers, so a missing required input
    or a misspelled key fails at launch rather than deep in a durable run.
    """
    from chemclaw.agent.workflow_tools import WorkflowInput, WorkflowStep, run_composed_workflow
    from chemclaw.templates.registry import TemplateError

    with _as("chemist-validates"):
        _compose(
            name="brief",
            summary="s",
            inputs=[WorkflowInput(name="smiles", description="the molecule")],
            steps=[
                WorkflowStep(
                    id="forms", tool="enumerate_tautomers", arguments={"smiles": "${inputs.smiles}"}
                ),
                WorkflowStep(id="say", prompt="which: ${steps.forms.result}"),
            ],
        )
        with pytest.raises(TemplateError) as missing:
            asyncio.run(run_composed_workflow(name="brief", inputs={}))
        assert "smiles" in str(missing.value)
        assert "['smiles']" in str(missing.value), "the refusal has to name what is declared"

        with pytest.raises(TemplateError):
            asyncio.run(run_composed_workflow(name="brief", inputs={"smilez": "CCO"}))


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_store_stamps_the_conversation_a_workflow_was_composed_in(backend: str) -> None:
    """The store stamps the conversation a workflow was composed in.

    Provenance is the store's observation from ambient context, in both backends, and is read by
    `GET /workflows/{name}`.
    """
    from chemclaw.core.session_context import (
        reset_current_session_id,
        set_current_session_id,
    )

    async def _drive() -> None:
        store = await _backend(backend)
        owner = f"traceable-{backend}"
        token = set_current_session_id("session-abc")
        try:
            await store.save(
                ComposedWorkflow(
                    owner=owner,
                    name="triage",
                    summary="s",
                    document=_document([_REASON_STEP], "triage"),
                    # Declared by the caller and ignored: the store observes this, it is not told.
                    session_id="a-session-the-caller-made-up",
                )
            )
        finally:
            reset_current_session_id(token)

        stored = await store.get(owner, "triage")
        assert stored is not None
        assert stored.session_id == "session-abc"
        await store.forget(owner, "triage")

    asyncio.run(_drive())
