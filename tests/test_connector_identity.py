"""What crosses the process boundary with a connector call — and what deliberately does not.

Two mechanisms, tested for the two different reasons they exist:

- The **identity headers** must reflect the turn that is calling, so `turn_headers` is tested for
  reading the *ambient* context rather than anything captured earlier — the property that makes
  a connector's own request log joinable to the core audit trail.
- The **auth flow** must read its credential per request, so a rotated secret takes effect without a
  restart rather than pinning whatever was mounted when the client was built.

Both have a negative half worth pinning: an absent actor must yield an absent header (not an empty
one, which would let a connector's log claim an anonymous user made the call), and a missing
credential must raise rather than send an empty `Authorization`.

That the headers actually *arrive* is a transport property, proven against a live server in
`test_connector_transport.py` — it cannot be shown here, and assuming it is exactly the mistake
that
made MAF's own `header_provider` look usable (see `chemclaw.connectors.identity`).
"""

import httpx
import pytest

from chemclaw.agent.dialogue_tools import reset_dry_run, set_dry_run
from chemclaw.agent.identity_context import reset_current_identity, set_current_identity
from chemclaw.agent.session_context import reset_current_session_id, set_current_session_id
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_DRY_RUN,
    HEADER_ROLES,
    HEADER_SESSION,
    MissingConnectorCredential,
    auth_for,
    turn_headers,
)
from chemclaw.connectors.manifest import BearerAuth, NoAuth


def test_no_ambient_identity_sends_no_identity_headers() -> None:
    """Off the request path there is no actor, and claiming one would corrupt an audit join."""
    headers = turn_headers()
    assert HEADER_ACTOR not in headers
    assert HEADER_ROLES not in headers
    assert HEADER_SESSION not in headers
    # Dry-run is always sent: "not a dry run" is a real state, not an absence.
    assert headers[HEADER_DRY_RUN] == "false"


def test_headers_are_read_from_the_ambient_turn_at_call_time() -> None:
    """The property the whole design rests on: the headers describe the turn in flight.

    Anything captured earlier — at client construction, at connect — would make every call in
    the process report whichever user happened to be first, which is precisely the
    misattribution the per-turn connector lifetime exists to prevent.
    """
    identity = set_current_identity("user-1", frozenset({"process-chemist", "admin"}))
    session = set_current_session_id("session-abc")
    dry_run = set_dry_run(True)
    try:
        headers = turn_headers()
    finally:
        reset_dry_run(dry_run)
        reset_current_session_id(session)
        reset_current_identity(identity)
    assert headers[HEADER_ACTOR] == "user-1"
    # Sorted and space-delimited (the OAuth `scope` convention), so two calls by one user match.
    assert headers[HEADER_ROLES] == "admin process-chemist"
    assert headers[HEADER_SESSION] == "session-abc"
    assert headers[HEADER_DRY_RUN] == "true"
    # And once the turn is over, there is no identity to report again.
    assert HEADER_ACTOR not in turn_headers()


def test_the_headers_carry_only_identity_never_call_content() -> None:
    """The headers say *who* is calling, never *what* they asked for.

    Nothing from the tool call reaches them by construction — `turn_headers` takes no argument
    at all — which is the point: model-authored text in the transport envelope would be read as
    our own metadata by a connector's request log and by any intermediary.
    """
    import inspect

    assert inspect.signature(turn_headers).parameters == {}
    assert set(turn_headers()) == {HEADER_DRY_RUN}


def test_no_auth_needs_no_credential() -> None:
    """`mode: none` is the trust-boundary case (stdio, loopback dev): nothing to attach."""
    assert auth_for(NoAuth(), "alpha") is None


def test_bearer_reads_its_token_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated secret must take effect without a restart, so the variable is read in the flow.

    Proven by rotating it *between* two flows over the same auth object — a token captured in
    `__init__` would send the stale value the second time.
    """
    auth = auth_for(BearerAuth(token_env="CHEMCLAW_TEST_TOKEN"), "alpha")
    assert auth is not None
    monkeypatch.setenv("CHEMCLAW_TEST_TOKEN", "first")
    first = next(auth.auth_flow(httpx.Request("GET", "http://alpha/mcp")))
    assert first.headers["Authorization"] == "Bearer first"
    monkeypatch.setenv("CHEMCLAW_TEST_TOKEN", "rotated")
    second = next(auth.auth_flow(httpx.Request("GET", "http://alpha/mcp")))
    assert second.headers["Authorization"] == "Bearer rotated"


def test_a_missing_credential_raises_instead_of_sending_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named configuration error beats a 401 from a call that silently carried no credential."""
    monkeypatch.delenv("CHEMCLAW_TEST_TOKEN", raising=False)
    auth = auth_for(BearerAuth(token_env="CHEMCLAW_TEST_TOKEN"), "alpha")
    assert auth is not None
    with pytest.raises(MissingConnectorCredential, match="CHEMCLAW_TEST_TOKEN"):
        next(auth.auth_flow(httpx.Request("GET", "http://alpha/mcp")))


def test_the_correlation_id_crosses_the_connector_boundary() -> None:
    """The audit trail joins across processes, on the key core already stamps (REV-11).

    `chemclaw.agent.audit` records a correlation id for every in-core tool call, and the connector
    serving
    that call logged under an id of its own with nothing tying the two together. "Show me everything
    that happened in this turn" was therefore answerable in core and unanswerable across the four
    runtimes a turn actually spans — which is most of what an audit trail is for.

    Advisory like the rest of these headers: a connector may join its records to ours on it and must
    never make an access decision on it.
    """
    from chemclaw.agent.identity_context import (
        reset_current_correlation_id,
        set_current_correlation_id,
    )
    from chemclaw.connectors.identity import HEADER_CORRELATION

    token = set_current_correlation_id("turn-7f3a")
    try:
        headers = turn_headers()
    finally:
        reset_current_correlation_id(token)
    assert headers[HEADER_CORRELATION] == "turn-7f3a"
    # Absent, not empty, once the turn is over — an empty id in a connector's log reads as one
    # that exists, which is the failure this header is meant to remove rather than reproduce.
    assert HEADER_CORRELATION not in turn_headers()


def test_a_durable_job_carries_the_turn_it_was_launched_from() -> None:
    """The other half of the same gap: a durable run must not be an island in the trail.

    `ConnectorJobInput` reaches a Temporal worker that has no request context, so the id has to
    travel in the input — the same argument that puts `requested_by` there. It is then set as a
    workflow *memo* rather than folded into `payload`, because `payload` is exactly the arguments
    the model filled in, and metadata the LLM can write is not metadata.
    """
    from chemclaw.durable.connector_job import ConnectorJobInput

    job = ConnectorJobInput(
        connector="calc",
        job="compute_reaction_energy",
        workflow="CalcJobWorkflow",
        task_queue="background-jobs",
        requested_by="user-1",
        correlation_id="turn-7f3a",
    )
    assert job.correlation_id == "turn-7f3a"
    # Defaulted, so every existing caller keeps working and an off-request-path launch (the CLI, a
    # scheduled job) records the honest absence rather than a fabricated id.
    assert (
        ConnectorJobInput(
            connector="calc",
            job="compute_reaction_energy",
            workflow="CalcJobWorkflow",
            task_queue="background-jobs",
            requested_by="user-1",
        ).correlation_id
        == ""
    )
