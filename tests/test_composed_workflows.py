"""A workflow the agent composed is read-only, and that is what keeps the plan gate's exemption.

The security argument is the subject of this file, not the storage. `D-2026-08-12-a-template-is-
the-plan-so-the-step-is-read-only` exempts a template's `agent` step from the plan gate because the
file *"is authored by a person, committed to git and reviewed, and nothing at run time can produce
one"*. `compose_workflow` produces one at run time, so either the exemption has to be re-argued or
the premise restored. It is restored: an agent-authored workflow may name no side-effecting tool and
no `write_tools`, so the exemption is never reached. A durable `job` step is the one thing a person
can authorize (`D-2026-09-15-an-approval-is-for-one-version-of-one-workflow`), and the tests below
hold both halves — what an approval releases, and what no approval touches.

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
    """The reason the key is `(owner, name)`.

    Two chemists are each entitled to their own "triage", and a name that resolved across owners
    would let one silently run steps the other wrote — which is a worse failure than a collision,
    because nothing about the result would look wrong.
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
    """**The journey that had no ending**, driven rather than assumed.

    A composed workflow is keyed `(owner, name)` on the ambient actor. That is the request
    principal's oid at the front door and `cli_admin_actor` at this prompt — the same person's oid
    once identity is enforced, and three different strings in a dev deployment (`admin@localhost`,
    `dev-user`, `service-account`). So a workflow composed in the terminal was invisible to
    `GET /workflows/{name}`, and its job steps could never be released by anybody: measured before
    this command existed, the route answered 404 for a workflow the CLI had just stored.

    The fix is the shape `/approve` already has for plans — each surface's person approves on that
    surface — and not a change to what an actor is called, which would move identity semantics to
    fix a feature.
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

        # **Read, then approve what was read.** The first line shows the procedure and hands back
        # the fingerprint; the second binds to it. One line that approved "as it stands" was the
        # defect: the *agent* also acts in this terminal under this same owner between two typed
        # commands, so "the person reading and the person approving are the same terminal" is false
        # here in a way it is not for `/approve`.
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
    """`approved_by` is the record, so it must be the typist rather than whatever composed it.

    And it can only ever be the owner: `ComposedStore.approve` takes no approver argument, because
    both callers resolve the workflow against the caller's own rows and so had nobody else to name.
    That is what lets `agent/leaver.py` erase a departing person's approvals with `WHERE owner =
    ANY(...)` — a fourth parameter would have made a state the write path cannot produce but the
    erase predicate cannot reach.
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

    Driven because it used to be false in both directions at once. `_UPSERT` names no approval
    column, so Postgres kept a stored approval across a re-compose; `InMemoryComposedStore.save`
    replaced the whole object, so memory destroyed it *and* would have accepted an approval handed
    to it in a `ComposedWorkflow`. The same call sequence therefore produced a different stored row
    in each — and the weaker of the two is what a CLI or dev process runs.

    The two properties this pins are the ones the security argument actually rests on: the agent's
    write cannot *grant* an approval, and it cannot *erase* the record of a person's. Whether the
    approval still has effect is `unapproved_jobs`' question, asked against the fingerprint.
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
    """The cap's other half, in both backends.

    Without a delete, `MAX_PER_OWNER` left one remedy — re-compose over a name — which destroys the
    document anyway *and* leaves a row whose name lies about its contents. `False` rather than a
    silent success for a name that is not there, because the caller asked for a specific thing to
    stop existing and needs to know whether it did.
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
    """`list_for` fetches one past `MAX_PER_OWNER`, so the guard is not reading its own page size.

    Driven against Postgres because that is where the clamp is a SQL `LIMIT`. At `LIMIT
    MAX_PER_OWNER` the surplus was invisible to the cap guard *and* to the "you have: […]" listing
    `run_composed_workflow` gives, so re-composing a workflow the owner still had was refused with
    "you already have 50" while `get` went on finding and running it.
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
    """`authored_problems` fails *closed* on a step kind it does not recognise.

    The chain was `if ToolStep … elif AgentStep …` with no final branch, so a fourth step kind added
    later would have been silently permitted here on the day it was added — failing open in the one
    function whose whole job is to fail closed, and where "allowed" means "exempt from the plan
    gate". Driven with a stand-in rather than argued, because the whole defect is about a class that
    does not exist yet.

    `agent/template_surface.template_step_ceilings` takes the same position from the other side: an
    unsized kind raises rather than counting as free.
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
    """Both launchers validate, because a check only one of them performs is not a check.

    `build_template_tool.launch` validated against the template's own params model — the D-138 fix —
    and `run_composed_workflow` called the shared launcher with a raw dict. So a composed run
    started with whatever it was handed: a *declared required* input could be omitted entirely, and
    `${inputs.smiles}` then failed deep inside a durable run rather than at the launch, which is
    precisely the wasted launch `unrunnable_reason` exists to refuse. The validation now lives in
    `start_template_run`, where both callers reach it, and a misspelled key is the same failure
    because the one it was meant to be is then missing.
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
    """Provenance is the store's observation, not a field the writer declares.

    The column shipped in migration 100 saying it exists "so a workflow that later looks wrong can
    be traced back to the conversation that produced it", and nothing in `src/` selected it — a
    promise only somebody holding a psql prompt could keep, and one the in-memory backend could not
    keep at all. Stamped from the ambient context by both backends, the way `approved_at` is, and
    read by `GET /workflows/{name}`.
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
