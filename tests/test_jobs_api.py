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
from chemclaw.durable.job_record import JobRecordSearch, JobRecordSummary

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

    async def _records(text: str = "", connector: str = "", after: str = "") -> JobRecordSearch:
        return JobRecordSearch(
            hits=[
                JobRecordSummary(
                    job_id="job-1",
                    connector="qm",
                    job="sample_conformers",
                    rationale="the reviewer questioned the reported barrier",
                    summary="done",
                )
            ]
        )

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


def _gateway_400(message: str) -> Any:
    """A 400 exactly as an OpenAI-compatible gateway returns it, body and all."""
    import httpx2
    import openai

    request = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")
    body = {"code": "invalid_request_error", "message": message, "type": "invalid_request_error"}
    response = httpx2.Response(400, request=request, json={"error": body})
    return openai.BadRequestError(
        f"Error code: 400 - {{'error': {body}}}", response=response, body=body
    )


@pytest.mark.parametrize(
    "message",
    [
        # The live lane's own refusal, verbatim from `api.log` on 2026-09-27 — the turn that
        # reported `internal` two lines below a model-call log line saying `context_length`.
        "prompt is too long: 84578 tokens > 2000 maximum",
        # DeepSeek's own wording, and OpenRouter's when it refuses before forwarding.
        "This model's maximum context length is 131072 tokens. However, you requested 140211 "
        "tokens (136115 in the messages, 4096 in the completion). Please reduce the length of "
        "the messages or completion.",
        "This endpoint's maximum context length is 163840 tokens. However, you requested about "
        "170000 tokens (165904 of text input, 4096 in the output).",
    ],
)
def test_an_oversize_request_is_a_context_length_failure_not_an_internal_one(message: str) -> None:
    """The one failure whose remedy is the chemist's was reported as `internal, do not retry`.

    Driven through the shapes the turn actually receives: the SDK's `BadRequestError`, and
    `langchain_openai`'s re-raise of it as `OpenAIContextOverflowError` — which is the exception
    the live lane's traceback ends on, and which is what `_classify` is really handed.
    """
    from langchain_openai.chat_models.base import OpenAIContextOverflowError

    from chemclaw.api.runner import failure_event

    raw = _gateway_400(message)
    rewrapped = OpenAIContextOverflowError(
        message=raw.message, response=raw.response, body=raw.body
    )
    for exc in (raw, rewrapped):
        assert _classify(exc) == ("context_length", False), type(exc).__name__
    event = failure_event(rewrapped, "s-1", "c-1")
    assert event.code == "context_length"
    assert "internal error" not in event.message
    assert "too long" in event.message


def test_a_provider_stall_is_worded_as_one_and_not_as_an_internal_error() -> None:
    """A gateway that goes quiet mid-stream is the provider's fault, and the sentence says so.

    Live re-verification 2026-10-02 (D9): `langchain_openai` raised `StreamChunkTimeoutError` after
    120 s without a chunk, `_classify` already called it `llm_timeout` and retryable, and the
    chemist read "could not be completed due to an internal error" beside both. Driven with the
    exception the lane's traceback ended on, because that is what `_classify` is really handed.
    """
    from langchain_openai.chat_models._client_utils import StreamChunkTimeoutError

    from chemclaw.api.runner import failure_event

    stall = StreamChunkTimeoutError(120.0, model_name="deepseek/deepseek-v4-pro")
    event = failure_event(stall, "s-1", "c-1")
    assert (event.code, event.retryable) == ("llm_timeout", True)
    assert "internal error" not in event.message
    assert "model provider" in event.message


def _gateway_error(status: int) -> Any:
    """An OpenAI-compatible gateway answering `status`, as the SDK raises it after its retries."""
    import httpx2
    import openai

    request = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")
    body = {"message": "injected failure", "type": "server_error"}
    response = httpx2.Response(status, request=request, json={"error": body})
    kind = openai.RateLimitError if status == 429 else openai.InternalServerError
    return kind(f"Error code: {status}", response=response, body=body)


def _provider_failures() -> list[Exception]:
    """Every shape a provider-side failure reaches `_classify` in, raw and as LangChain re-raises.

    `OpenAIAPIError` is the one the kind cluster's turn ended on (the mock answering HTTP 500): it
    is what `langchain_openai` re-raises a 5xx as, and it is an `InternalServerError`.
    """
    import httpx2
    import openai
    from langchain_openai.chat_models.base import (
        OpenAIAPIError,
        OpenAIConnectionError,
        OpenAITimeoutError,
    )

    request = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")
    server = _gateway_error(500)
    return [
        server,
        OpenAIAPIError(message=server.message, response=server.response, body=server.body),
        _gateway_error(503),
        _gateway_error(429),
        openai.APIConnectionError(request=request),
        OpenAIConnectionError(request=request),
        openai.APITimeoutError(request=request),
        OpenAITimeoutError(request=request),
    ]


def test_a_model_gateway_failure_is_the_provider_retryable_not_an_internal_error() -> None:
    """A gateway that answered 500 reached the chemist as "an internal error", not retryable.

    Measured on the kind cluster with the scripted mock's `f-http-500`: the SDK retried three
    times, `model.call_failed … (transport: OpenAIAPIError)` was logged, and two lines later the
    turn ended `internal`. `classify_model_failure` already knew it was the provider; `_classify`
    only asked it about `context_length`.
    """
    from chemclaw.api.runner import failure_event

    for exc in _provider_failures():
        assert _classify(exc) == ("llm_timeout", True), type(exc).__name__
        event = failure_event(exc, "s-1", "c-1")
        assert "internal error" not in event.message, type(exc).__name__
        assert "model provider" in event.message


def test_a_request_the_provider_refused_as_wrong_is_not_called_transient() -> None:
    """A 401 or a 404 is about the request (a key, a model name), not a provider outage.

    It stays `internal, do not retry`: telling a chemist to try again in a moment about a
    misconfigured credential would send them round a loop that cannot succeed.
    """
    import httpx2
    import openai

    request = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")
    for status, kind in ((401, openai.AuthenticationError), (404, openai.NotFoundError)):
        response = httpx2.Response(status, request=request, json={"error": {"message": "no"}})
        exc = kind(f"Error code: {status}", response=response, body=None)
        assert _classify(exc) == ("internal", False), status


def test_only_an_unclassified_failure_is_called_an_internal_error() -> None:
    """A code that knows its cause has a sentence naming it; `internal` alone admits it does not."""
    from chemclaw.api.runner import failure_event

    worded = {
        type(exc).__name__: failure_event(exc, "s-1", "c-1")
        for exc in (ConnectionError("db down"), ChemclawError("bad SMILES"), RuntimeError("odd"))
    }
    assert "internal error" in worded["RuntimeError"].message
    assert all(
        "internal error" not in event.message
        for name, event in worded.items()
        if name != "RuntimeError"
    )


def test_a_streamed_overflow_the_client_library_recognised_is_context_length_too() -> None:
    """`OpenAIAPIContextOverflowError` is an `APIError` and **not** a `BadRequestError`.

    The message test is gated on `BadRequestError`, so the streamed half of the same failure fell
    through to `internal` even after the non-streamed half was classified. The client library's
    own `ContextOverflowError` type is the signal both share.
    """
    import httpx2
    from langchain_openai.chat_models.base import OpenAIAPIContextOverflowError

    request = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")
    streamed = OpenAIAPIContextOverflowError(
        message="context window exceeded mid-stream", request=request, body=None
    )
    assert _classify(streamed) == ("context_length", False)


def test_a_bad_request_that_is_not_about_length_stays_internal() -> None:
    """The control: a 400 alone is not an overflow, so the new arm must not swallow every 400."""
    assert _classify(_gateway_400("tools[0].function.name is invalid")) == ("internal", False)


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
