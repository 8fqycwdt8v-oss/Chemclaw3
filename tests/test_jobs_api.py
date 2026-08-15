"""A durable job a user can find, and a failure they can act on (the product floor).

Two gaps, both invisible from inside the system and obvious from outside it.

**There was no job surface at all.** Status and result were reachable *only* as an agent tool
inside a turn, so a chemist could not list what was running, could not fetch a result once the
session was gone, and could not stop a runaway run. `job_records` held the result the whole time
(D-157) — nothing exposed it.

**Every turn failure was one opaque string.** `runner.py` caught `Exception` and returned "an
internal error", so a surface could not tell a connector being down from an LLM timeout from a
database outage from a malformed tool argument. It could therefore offer no next step, and "try
again" was as likely to be wrong as right — and the message named the *session*, which the user
already has, rather than the correlation id the audit trail is actually keyed on.
"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.events import ErrorEvent
from chemclaw.api.runner import _classify
from chemclaw.core.errors import ChemclawError
from chemclaw.durable.job_record import JobRecordSummary

_DEV_OID = "dev-user"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real app; the dev principal holds the privileged role."""
    return TestClient(create_app())


@pytest.fixture
def plain_user_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The app seen by an authenticated chemist holding no operator role."""
    monkeypatch.setattr("chemclaw.core.config.settings.entra_required", True)
    monkeypatch.setattr("chemclaw.core.config.settings.entra_privileged_roles", "operator")
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: Principal(oid=_DEV_OID)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_finished_jobs_are_listable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route that did not exist: what has this system run, and why.

    The reason is on every row — `job_records.rationale` (D-157) — which is what makes the listing
    worth reading rather than a wall of opaque ids.
    """

    async def _records(text: str = "", connector: str = "") -> list[JobRecordSummary]:
        return [
            JobRecordSummary(
                job_id="job-1",
                connector="qm",
                job="compute_dft_energy",
                rationale="the reviewer questioned the reported barrier",
                summary="done",
            )
        ]

    monkeypatch.setattr("chemclaw.api.app.search_job_records", _records)

    listed = client.get("/jobs").json()
    assert [item["job_id"] for item in listed] == ["job-1"]
    assert "reviewer questioned" in listed[0]["rationale"]


def test_a_finished_job_answers_after_its_session_is_gone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result outlives both the conversation and Temporal's history.

    Reachable only from inside a turn before, so a chemist whose session had been evicted could
    not get at a result the durable record was holding for them.
    """
    from chemclaw.agent.durable_tools import DurableJobStatus

    async def _status(job_id: str) -> DurableJobStatus:
        return DurableJobStatus(job_id=job_id, status="completed", summary="done", result={"e": 1})

    monkeypatch.setattr("chemclaw.api.app.job_status", _status)

    body = client.get("/jobs/job-1").json()
    assert body["status"] == "completed"
    assert body["result"] == {"e": 1}


def test_an_unknown_job_is_a_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`job_status` raises for an id neither Temporal nor the record knows; the route says 404."""

    async def _missing(job_id: str) -> Any:
        raise ValueError("no durable job")

    monkeypatch.setattr("chemclaw.api.app.job_status", _missing)
    assert client.get("/jobs/nope").status_code == 404


def test_cancelling_needs_an_operator_role(plain_user_client: TestClient) -> None:
    """The design finding: a running job has no single owner, so "cancel mine" cannot exist.

    `job_workflow_id` hashes `[connector, job, payload]` and deliberately excludes the requester,
    so two chemists asking for the identical campaign rejoin one run (D-011). Cancelling it cancels
    it for everyone who joined, and the first requester is not more entitled to that than the
    second — so an owner-scope check here would read as ownership and not be it.
    """
    assert plain_user_client.delete("/jobs/job-1").status_code == 403


def test_an_operator_can_cancel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """202, not 204: cancellation is cooperative, so the request is delivered, not completed."""
    cancelled: list[str] = []

    async def _cancel(job_id: str) -> bool:
        cancelled.append(job_id)
        return True

    monkeypatch.setattr("chemclaw.api.app.cancel_job", _cancel)

    response = client.delete("/jobs/job-1")
    assert response.status_code == 202
    assert response.json()["status"] == "cancelling"
    assert cancelled == ["job-1"]


def test_profiles_are_discoverable(client: TestClient) -> None:
    """`POST /sessions` 400s an unknown profile and nothing listed the known ones.

    So a surface had to hardcode names that live in files it cannot see, and a deployment adding a
    profile had no way to make it reachable.

    **Asserting a *name*, because the shape assertion passed while the route was empty.** This test
    used to check only `isinstance(names, list)` and `names == sorted(names)`, both of which are
    true of `[]` — and `[]` is exactly what the route returned in every deployment, because it
    called `load_profiles()`, which reports only what it newly registered, after the lifespan had
    already registered everything. `default` is the one name that must always be there: it is a
    profile a caller may pass, it is registered without a file, and no discovery order can drop it.
    """
    names = client.get("/profiles").json()
    assert isinstance(names, list)
    assert names == sorted(names)
    assert "default" in names


def test_profiles_answers_the_same_list_on_the_second_call(client: TestClient) -> None:
    """The route is a read, so asking twice answers twice — the defect above, from the other side.

    A registry read is idempotent; the discovery call it replaced was idempotent only in its
    *effect*, not in its return value, which is the whole of the bug.
    """
    first = client.get("/profiles").json()
    second = client.get("/profiles").json()
    assert first == second
    assert first, "the profile list is never empty — `default` is always registered"


# --- the error taxonomy ---------------------------------------------------------------------


def test_a_failure_says_what_kind_it_was_and_whether_to_retry() -> None:
    """One opaque string made every failure the same failure.

    A database outage and a malformed SMILES need opposite responses from the user, and the turn
    reported them identically — so a surface could only ever say "something went wrong".
    """
    assert _classify(ConnectionError("db down")) == ("storage_unavailable", True)
    assert _classify(TimeoutError()) == ("llm_timeout", True)
    assert _classify(ChemclawError("unbalanced equation")) == ("bad_tool_arguments", False)


def test_an_unclassified_failure_stays_internal_rather_than_guessing() -> None:
    """`internal` is the honest default: nobody has decided this one's user-facing meaning.

    Guessing a friendlier code would be worse than admitting the classification is missing, because
    a wrong `retryable=True` sends a user to burn another turn on a failure that cannot succeed.
    """
    assert _classify(RuntimeError("something odd")) == ("internal", False)


def test_the_error_carries_the_key_the_audit_trail_is_keyed_on() -> None:
    """The old message named the session — the id the user already has.

    The correlation id is what `audit_events` is keyed on
    (D-2026-07-31-the-audit-chain-is-versioned), so quoting it in a bug report is what lets an
    operator find the turn. A random per-turn hex string, so nothing sensitive travels with it.
    """
    event = ErrorEvent(
        message="boom", code="storage_unavailable", retryable=True, correlation_id="c-1"
    )
    assert event.model_dump()["correlation_id"] == "c-1"
    # And the default stays safe for every producer that has not been taught the taxonomy yet.
    assert ErrorEvent(message="boom").code == "internal"
    assert ErrorEvent(message="boom").retryable is False


def _run(awaitable: Any) -> Any:
    """Drive a coroutine from a sync test."""
    return asyncio.run(awaitable)


# --- the transcript contract ------------------------------------------------------------------


def test_a_reload_recovers_what_the_agent_did_not_only_what_it_said() -> None:
    """The live stream carries fourteen event types; a reload got `role` and `text`.

    So everything the agent *did* vanished on refresh and a UI could not render history at parity
    with the live view — the largest single blocker for the frontend repo. The tool calls were
    never missing from storage: a MAF message already holds `function_call`/`function_result`
    contents, and the route was flattening them away.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from chemclaw.api.app import _transcript

    stored = [
        HumanMessage(content="pKa of ethanol?"),
        AIMessage(
            content="Let me compute it.",
            tool_calls=[{"name": "predict_pka", "args": {"smiles": "CCO"}, "id": "c1"}],
        ),
        ToolMessage(content="pKa 15.9", tool_call_id="c1"),
        AIMessage(content="15.9."),
    ]

    transcript = _transcript(stored)

    # The bare `tool` message is folded into the call it answers rather than rendered as its own
    # bubble, which would show every tool twice.
    assert [entry.role for entry in transcript] == ["user", "assistant", "assistant"]
    [call] = transcript[1].tool_calls
    assert call.tool == "predict_pka"
    assert "CCO" in call.arguments
    assert call.result == "pKa 15.9"


def test_an_unanswered_tool_call_is_rendered_as_unanswered() -> None:
    """A turn that failed mid-call is a real state, and `None` is the honest rendering.

    An empty-string result would read as "it ran and returned nothing", which is a different and
    more reassuring claim than "it ran and we do not know how it ended".
    """
    from langchain_core.messages import AIMessage

    from chemclaw.api.app import _transcript

    stored = [
        AIMessage(content="", tool_calls=[{"name": "predict_pka", "args": {}, "id": "orphan"}])
    ]

    [entry] = _transcript(stored)
    assert entry.tool_calls[0].result is None


def test_a_transcript_bounds_what_one_call_can_carry() -> None:
    """A tool argument can be a whole optimization problem; a reload must not ship one per call.

    The same bound the audit trail applies, for the same reason.
    """
    from langchain_core.messages import AIMessage

    from chemclaw.api.app import _TRANSCRIPT_ARG_CHARS, _transcript

    stored = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "suggest_next_experiment",
                    "args": {"problem": "x" * 5000},
                    "id": "big",
                }
            ],
        )
    ]

    [entry] = _transcript(stored)
    assert len(entry.tool_calls[0].arguments) <= _TRANSCRIPT_ARG_CHARS + 1
