"""The PR-gate's record and its review surface: what was proposed, and what a human decided.

Before this, the gate every other control in the system is justified by ended at a branch push.
Nothing listed what was awaiting review, the chemist who proposed a note could not learn what
became of it, and a rejection left no trace at all — a rejection is a deleted branch. These tests
pin the two halves that fix it: `propose_note` records both outcomes of a submission, and the
`/proposals` routes make the queue operable and its decisions scoped.

The store's own rules are exercised against `InMemoryProposalStore`, which is the backend a
`session_store="memory"` deployment really gets rather than a test double — so what is asserted
here is production behaviour on that path, and the contract its Postgres sibling must match.
"""

import asyncio
import hashlib
import hmac
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core.metrics import METRICS
from chemclaw.kg.note import Note
from chemclaw.kg.pr_gate import propose_note
from chemclaw.kg.proposal import (
    InMemoryProposalStore,
    NoteProposal,
    ProposalState,
    proposal_store,
    record_proposal_submitted,
)
from tests.conftest import FakeSubmitter

_SECRET = "webhook-secret"
_DEV_OID = "dev-user"


def _note(note_id: str = "reaction-1", body: str = "yield 82%") -> Note:
    """An agent-authored note, the only kind the gate accepts."""
    return Note(id=note_id, type="reaction", created_by="agent", body=body)


def _proposal(**overrides: Any) -> NoteProposal:
    """A submitted proposal with everything the record needs."""
    fields: dict[str, Any] = {
        "note_id": "reaction-1",
        "note_type": "reaction",
        "content": "rendered note",
        "branch": "note/reaction-1",
        "actor": _DEV_OID,
    }
    fields.update(overrides)
    return NoteProposal(**fields)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryProposalStore:
    """One fresh in-memory store, shared by the gate and the routes for the whole test.

    `proposal_store` is `@cache`d on purpose — writer and readers must see one instance — so it is
    replaced rather than merely cleared, which also keeps one test's queue out of the next.
    """
    fresh = InMemoryProposalStore()
    monkeypatch.setattr("chemclaw.kg.proposal.proposal_store", lambda: fresh)
    proposal_store.cache_clear()
    return fresh


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real app with the webhook secret set; the dev principal is a reviewer."""
    monkeypatch.setattr("chemclaw.core.config.settings.note_webhook_secret", _SECRET)
    monkeypatch.setattr("chemclaw.api.app.request_note_reindex", _fake_reindex)
    return TestClient(create_app(agent_factory=lambda _profile: object()))


@pytest.fixture
def plain_user_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app seen by an authenticated chemist holding no review role."""
    monkeypatch.setattr("chemclaw.core.config.settings.entra_required", True)
    monkeypatch.setattr("chemclaw.core.config.settings.entra_privileged_roles", "note-reviewer")
    app = create_app(agent_factory=lambda _profile: object())
    app.dependency_overrides[require_principal] = lambda: Principal(oid=_DEV_OID)
    yield TestClient(app)
    app.dependency_overrides.clear()


async def _fake_reindex() -> str:
    """Stand in for the Temporal-backed reindex the webhook kicks."""
    return "note-reindex-test"


def _signed(body: bytes) -> dict[str, str]:
    """The signature header a git host would send for `body`."""
    digest = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Chemclaw-Signature": f"sha256={digest}"}


def _run(awaitable: Any) -> Any:
    """Drive one store coroutine from a sync test (the store holds no loop-bound state)."""
    return asyncio.run(awaitable)


# --- the gate records both outcomes -------------------------------------------------------


def test_a_successful_submission_is_recorded_as_open(store: InMemoryProposalStore) -> None:
    """The queue exists at all: a proposed note is findable without browsing git refs."""
    reference = _run(propose_note(_note(), FakeSubmitter()))

    [recorded] = _run(store.listing(None, "", 10, None))
    assert recorded.note_id == "reaction-1"
    assert recorded.state is ProposalState.OPEN
    assert recorded.reference == reference
    # The rendered note, not a summary of it: a reviewer signs off on the bytes that will land.
    assert "yield 82%" in recorded.content


def test_a_failed_submission_keeps_the_note_it_could_not_push(
    store: InMemoryProposalStore,
) -> None:
    """The half a counter could not give: a lost note is replayable, not merely tallied."""

    class DeadRemote:
        async def submit(self, submission: object) -> str:
            raise RuntimeError("could not push to origin")

    with pytest.raises(RuntimeError):
        _run(propose_note(_note(), DeadRemote()))

    [recorded] = _run(store.listing(None, "", 10, None))
    assert recorded.state is ProposalState.FAILED
    assert "yield 82%" in recorded.content
    assert "could not push" in recorded.reason


def test_a_multi_file_submission_records_every_file_it_would_have_written(
    store: InMemoryProposalStore,
) -> None:
    """The measured gap (D-2026-08-05): the record kept `files[0]` and dropped the rest.

    A submission is one reviewable unit — a note and the notes its links depend on (D-133) — and
    the record of a `FAILED` one is only replayable if the unit is what was kept. It was not: a
    `job-result` proposal replayed from its row would have written a note whose
    `[[wikilink]]` to its `compound` dangled, failing `kg-validate` on the very PR it reopened.
    Two docstrings asserted the opposite while it was true.
    """

    class DeadRemote:
        async def submit(self, submission: object) -> str:
            raise RuntimeError("could not push to origin")

    compound = Note(id="compound-ethanol", type="compound", created_by="agent", body="the compound")
    subject = _note(body="rests on [[compound-ethanol]]")

    with pytest.raises(RuntimeError):
        _run(propose_note(subject, DeadRemote(), dependencies=[compound]))

    [recorded] = _run(store.listing(None, "", 10, None))
    assert recorded.state is ProposalState.FAILED
    assert "rests on" in recorded.content
    # Everything else the submission would have written, with the paths it would have written them
    # to — which is what "replayable" means.
    assert [file.path for file in recorded.dependencies] == [
        "knowledge/compound/compound-ethanol.md"
    ]
    assert "the compound" in recorded.dependencies[0].content


def test_a_credential_in_a_git_error_is_redacted_before_it_is_stored(
    store: InMemoryProposalStore,
) -> None:
    """Truncation is not redaction, and the bound was described as if it were both.

    `note_proposals` is a compliance table `chemclaw.durable.retention` deliberately never prunes,
    so anything written to `reason` is written forever. The reason text is whatever git wrote to
    stderr, and git quotes the push URL — with its token in the userinfo — on the most ordinary
    authentication failure there is. That message measures 118 characters against a 300-character
    cut, so the bound the comment relied on had never once applied to the case it named.

    Asserted on the token itself rather than on the redacted form, because what matters is that
    the secret is absent, not how the remainder reads.
    """
    secret = "ghp_S3cretTokenValue"

    class UnauthenticatedRemote:
        async def submit(self, submission: object) -> str:
            raise RuntimeError(
                "fatal: could not read Username for "
                f"'https://x-access-token:{secret}@git.example.invalid': No such device"
            )

    with pytest.raises(RuntimeError):
        _run(propose_note(_note(), UnauthenticatedRemote()))

    [recorded] = _run(store.listing(None, "", 10, None))
    assert secret not in recorded.reason
    # Still diagnostic: which remote failed, and why, survive the redaction.
    assert "git.example.invalid" in recorded.reason
    assert "could not read Username" in recorded.reason


def test_a_store_failure_never_fails_the_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note reaching the branch outranks the record of it — the audit-sink trade.

    A database blip must not turn a successful PR-gate submission into a failed tool call, so the
    write path swallows. Asserted directly rather than inferred, because "it did not raise" is the
    whole behaviour.
    """

    class BrokenStore(InMemoryProposalStore):
        async def upsert(self, proposal: NoteProposal) -> int:
            raise ConnectionError("database down")

    monkeypatch.setattr("chemclaw.kg.proposal.proposal_store", BrokenStore)

    _run(record_proposal_submitted(_proposal()))
    assert _run(propose_note(_note(), FakeSubmitter())) == "pr://note/reaction-1"


# --- the store's rules --------------------------------------------------------------------


def test_reproposing_identical_content_collapses_onto_one_row() -> None:
    """Matches the submitter, which pushes nothing when there is no diff."""
    store = InMemoryProposalStore()
    first = _run(store.upsert(_proposal()))
    second = _run(store.upsert(_proposal(reference="pr://again")))

    assert first == second
    assert len(_run(store.listing(None, "", 10, None))) == 1


def test_changed_content_is_a_new_version_beside_the_old_one() -> None:
    """A note's history survives its revision — the point of keying on content, not on the note."""
    store = InMemoryProposalStore()
    _run(store.upsert(_proposal(content="yield 82%")))
    _run(store.upsert(_proposal(content="yield 31% (corrected)")))

    assert len(_run(store.listing(None, "", 10, None))) == 2


def test_an_unchanged_reproposal_does_not_reopen_a_rejection() -> None:
    """A rejection stands until the content actually changes.

    Without this the gate is trivially defeatable: re-ask with the same bytes until the row reads
    open again and nobody looking at the queue can tell it was refused.
    """
    store = InMemoryProposalStore()
    proposal_id = _run(store.upsert(_proposal()))
    _run(store.decide(proposal_id, ProposalState.REJECTED, "reviewer", "not reproducible"))

    _run(store.upsert(_proposal()))

    still = _run(store.read(proposal_id))
    assert still is not None
    assert still.state is ProposalState.REJECTED
    assert still.reason == "not reproducible"


def test_a_retry_that_succeeded_supersedes_the_failure_it_replaced() -> None:
    """A submission that failed once and then landed must not stay `failed` forever.

    The retries live in `durable/memory_jobs`, `report_workflow` and `observation_jobs`, all under
    `note_publish_retry()`, and they re-render byte-identical content — so the successful attempt
    collapses onto the failed row. Leaving that row `failed` made the record assert the opposite of
    what happened: the branch is up awaiting review, while `state='open'` queries skip it, the
    decision route answers 409 and the merge webhook's `mark_merged` moves nothing. `failed` is a
    statement that git was never reached, not a decision, so a later success supersedes it.
    """
    store = InMemoryProposalStore()
    failed_id = _run(
        store.upsert(_proposal(state=ProposalState.FAILED, reason="git push failed: no route"))
    )

    retried_id = _run(store.upsert(_proposal(reference="pr://note/reaction-1")))

    assert retried_id == failed_id  # same content, same row — that is why the record was stuck
    recorded = _run(store.read(failed_id))
    assert recorded is not None
    assert recorded.state is ProposalState.OPEN
    assert recorded.reason == ""  # the stale git error does not outlive the failure it explained
    assert _run(store.listing(ProposalState.OPEN, "", 10, None)) == [recorded]


def test_a_decision_is_never_superseded_by_a_later_submission() -> None:
    """The other direction, and the rule that must survive the fix above.

    A rejection re-proposed unchanged must not reopen — otherwise the gate is defeatable by
    re-asking until nobody looking at the queue can tell it was refused. A merged row is likewise
    final: a redelivered submission must not walk it back to `open` and re-queue merged knowledge.
    """
    decisions = ((ProposalState.REJECTED, "not reproducible"), (ProposalState.MERGED, ""))
    for decision, reason in decisions:
        store = InMemoryProposalStore()
        proposal_id = _run(store.upsert(_proposal()))
        _run(store.decide(proposal_id, decision, "reviewer", reason))

        _run(store.upsert(_proposal(reference="pr://again")))

        still = _run(store.read(proposal_id))
        assert still is not None
        assert still.state is decision
        assert still.reason == reason


def test_a_decided_proposal_cannot_be_decided_again() -> None:
    """Two reviewers racing: the second learns it was taken instead of overwriting the first."""
    store = InMemoryProposalStore()
    proposal_id = _run(store.upsert(_proposal()))

    assert _run(store.decide(proposal_id, ProposalState.MERGED, "first", "")) is not None
    assert _run(store.decide(proposal_id, ProposalState.REJECTED, "second", "no")) is None


def test_marking_notes_merged_is_idempotent() -> None:
    """A webhook redelivery decides nothing twice."""
    store = InMemoryProposalStore()
    _run(store.upsert(_proposal()))

    assert _run(store.mark_merged(["reaction-1"], "webhook")) == 1
    assert _run(store.mark_merged(["reaction-1"], "webhook")) == 0


def test_listing_filters_by_state_and_proposer_and_pages_by_id() -> None:
    """The three reads the queue is built on, including the cursor that cannot skip a row."""
    store = InMemoryProposalStore()
    first = _run(store.upsert(_proposal(content="a", actor="chemist-a")))
    _run(store.upsert(_proposal(content="b", actor="chemist-b")))
    third = _run(store.upsert(_proposal(content="c", actor="chemist-a")))
    _run(store.decide(third, ProposalState.MERGED, "reviewer", ""))

    mine = _run(store.listing(None, "chemist-a", 10, None))
    assert [proposal.id for proposal in mine] == [third, first]

    still_open = _run(store.listing(ProposalState.OPEN, "", 10, None))
    assert third not in [proposal.id for proposal in still_open]

    older = _run(store.listing(None, "", 10, third))
    assert max(proposal.id for proposal in older) < third


# --- the review surface -------------------------------------------------------------------


def test_the_queue_is_listable_over_http(client: TestClient, store: InMemoryProposalStore) -> None:
    """The route that did not exist: what is awaiting review, without browsing `note/*` refs."""
    _run(propose_note(_note(), FakeSubmitter()))

    listed = client.get("/proposals").json()
    assert [item["note_id"] for item in listed] == ["reaction-1"]
    assert listed[0]["state"] == "open"
    # The listing is deliberately body-free; the note is one lookup away.
    assert "content" not in listed[0]

    detail = client.get(f"/proposals/{listed[0]['id']}").json()
    assert "yield 82%" in detail["content"]


def test_the_detail_route_shows_every_file_the_reviewer_is_signing_off_on(
    client: TestClient, store: InMemoryProposalStore
) -> None:
    """`GET /proposals/{id}` claimed to show "the note exactly as it would land in the tree".

    For a submission with dependencies it showed one file of an indivisible unit — so a reviewer
    approved a `[[wikilink]]` whose far end was not on screen, which is precisely the review the
    multi-file submission was introduced to make possible (D-133).
    """
    compound = Note(id="compound-ethanol", type="compound", created_by="agent", body="the compound")
    _run(
        propose_note(
            _note(body="rests on [[compound-ethanol]]"),
            FakeSubmitter(),
            dependencies=[compound],
        )
    )

    listed = client.get("/proposals").json()
    detail = client.get(f"/proposals/{listed[0]['id']}").json()

    assert "rests on" in detail["content"]
    assert [file["path"] for file in detail["dependencies"]] == [
        "knowledge/compound/compound-ethanol.md"
    ]
    assert "the compound" in detail["dependencies"][0]["content"]


def test_an_unknown_state_filter_is_refused(
    client: TestClient, store: InMemoryProposalStore
) -> None:
    """A typo'd filter must not silently return the unfiltered queue."""
    assert client.get("/proposals", params={"state": "approved"}).status_code == 422


def test_a_non_reviewer_sees_only_their_own_proposals(
    plain_user_client: TestClient, store: InMemoryProposalStore
) -> None:
    """Scoping mirrors sessions and holds: someone else's proposal is 404, never 403."""
    mine = _run(store.upsert(_proposal(content="mine", actor=_DEV_OID)))
    theirs = _run(store.upsert(_proposal(content="theirs", actor="someone-else")))

    listed = plain_user_client.get("/proposals").json()
    assert [item["id"] for item in listed] == [mine]
    assert plain_user_client.get(f"/proposals/{theirs}").status_code == 404


def test_deciding_needs_a_review_role(
    plain_user_client: TestClient, store: InMemoryProposalStore
) -> None:
    """A chemist cannot sign off on their own proposal — that is the line the gate draws."""
    proposal_id = _run(store.upsert(_proposal(actor=_DEV_OID)))

    refused = plain_user_client.post(f"/proposals/{proposal_id}/decision", json={"approved": True})
    assert refused.status_code == 403
    stored = _run(store.read(proposal_id))
    assert stored is not None and stored.state is ProposalState.OPEN


def test_a_rejection_must_say_why(client: TestClient, store: InMemoryProposalStore) -> None:
    """A record that says only "no" reproduces the gap the rejected row exists to close."""
    proposal_id = _run(store.upsert(_proposal()))

    refused = client.post(
        f"/proposals/{proposal_id}/decision", json={"approved": False, "reason": "  "}
    )
    assert refused.status_code == 422

    accepted = client.post(
        f"/proposals/{proposal_id}/decision",
        json={"approved": False, "reason": "the yield could not be reproduced"},
    )
    assert accepted.status_code == 204
    stored = _run(store.read(proposal_id))
    assert stored is not None
    assert stored.state is ProposalState.REJECTED
    assert stored.decided_at is not None


def test_deciding_twice_is_a_conflict_not_a_silent_overwrite(
    client: TestClient, store: InMemoryProposalStore
) -> None:
    """The route surfaces the store's single-decision rule rather than reporting success."""
    proposal_id = _run(store.upsert(_proposal()))

    first = client.post(f"/proposals/{proposal_id}/decision", json={"approved": True})
    assert first.status_code == 204
    again = client.post(f"/proposals/{proposal_id}/decision", json={"approved": True})
    assert again.status_code == 409


def test_an_unsigned_webhook_cannot_close_a_proposal(
    client: TestClient, store: InMemoryProposalStore
) -> None:
    """The body now carries an authorization-shaped claim, so it has to be signed.

    Unsigned callers keep the pre-existing power (force a reindex) and gain none of the new one.
    """
    proposal_id = _run(store.upsert(_proposal()))

    unsigned = client.post("/events/knowledge-merged", json={"note_ids": ["reaction-1"]})
    assert unsigned.status_code == 401
    stored = _run(store.read(proposal_id))
    assert stored is not None and stored.state is ProposalState.OPEN


def test_a_signed_webhook_closes_the_notes_it_names(
    client: TestClient, store: InMemoryProposalStore
) -> None:
    """The loop closes: a merged note stops sitting in the queue forever."""
    proposal_id = _run(store.upsert(_proposal()))

    body = b'{"note_ids": ["reaction-1"]}'
    response = client.post("/events/knowledge-merged", content=body, headers=_signed(body))
    assert response.status_code == 202
    assert response.json()["proposals_closed"] == "1"
    stored = _run(store.read(proposal_id))
    assert stored is not None and stored.state is ProposalState.MERGED


def test_once_a_secret_is_set_every_call_must_be_signed(
    client: TestClient, store: InMemoryProposalStore
) -> None:
    """Configuring the secret is the deployment saying "this endpoint is the git host's".

    Distinct from the rule below it: this refuses even a bodyless reindex-only call, which is the
    difference between "unsigned callers may still poke the index" (no secret configured) and "this
    route now belongs to one signed caller" (secret configured).
    """
    assert client.post("/events/knowledge-merged").status_code == 401
    assert (
        client.post("/events/knowledge-merged", content=b"", headers=_signed(b"")).status_code
        == 202
    )


def test_without_a_secret_a_reindex_still_works_but_decides_nothing(
    client: TestClient, store: InMemoryProposalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-existing power is kept; only the new, authorization-shaped one is withheld.

    An operator forcing a reindex by hand is a real workflow, and requiring them to compute an HMAC
    for it would be a regression dressed as hardening.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.note_webhook_secret", "")
    proposal_id = _run(store.upsert(_proposal()))

    assert client.post("/events/knowledge-merged").status_code == 202
    refused = client.post("/events/knowledge-merged", json={"note_ids": ["reaction-1"]})
    assert refused.status_code == 401
    stored = _run(store.read(proposal_id))
    assert stored is not None and stored.state is ProposalState.OPEN


def test_a_tampered_signature_is_refused(client: TestClient, store: InMemoryProposalStore) -> None:
    """The check must actually reject a MAC computed over different bytes."""
    body = b'{"note_ids": ["reaction-1"]}'
    headers = _signed(b'{"note_ids": ["reaction-2"]}')

    assert client.post("/events/knowledge-merged", content=body, headers=headers).status_code == 401


def test_every_state_is_counted(store: InMemoryProposalStore) -> None:
    """The gate's outcomes are a metric, so a queue nobody works is visible without opening it."""
    before = METRICS.value("chemclaw_note_proposals_total")

    _run(propose_note(_note(), FakeSubmitter()))

    assert METRICS.value("chemclaw_note_proposals_total") > before
