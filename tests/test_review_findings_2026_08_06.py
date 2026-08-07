"""The nine findings an intensive review of this branch produced, each pinned.

Every one of these is a defect the branch *introduced* (or, for the retention DSN, extended), and
every one passed the suite that shipped with it. They are together in one file because what they
share is the failure mode, not the subsystem: a test that exercises a shape production never
produces, a guard whose condition is narrower than its subject, an inventory keyed on names rather
than values.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agent_framework._types import ResponseStream
from pydantic import SecretStr

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw"


# --- F1: `SecretStr` silently emptied the log-redaction inventory ------------------------------


def test_a_secretstr_credential_is_still_redacted_from_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch's own `SecretStr` conversion turned redaction off for all six credentials.

    `SecretStr` is not a `str` subclass, so `_secret_values`' `isinstance(value, str)` guard —
    written to skip a non-credential field — began skipping every converted credential. The
    inventory came back empty and `redact_secrets` passed a live key through verbatim.

    The mitigation is what hid it: a masked `repr` means the key rarely reaches a log *by
    accident*, so the paths that mattered — a provider echoing it in an auth error, git stderr
    persisted into `note_proposals.reason` — were the ones nobody was watching. And
    `tests/test_secret_settings.py` asserted the field *names* were in the inventory, which stayed
    true with the redaction dead.
    """
    from chemclaw.core import logging as clog
    from chemclaw.core.config import settings

    canary = "sk-live-LEAKCANARY-0123456789"
    monkeypatch.setattr(settings, "llm_api_key", SecretStr(canary))
    assert canary not in clog.redact_secrets(f"auth failed for key {canary}")


@pytest.mark.parametrize(
    "name",
    [
        "llm_api_key",
        "hpc_api_token",
        "hpc_artifact_store_token",
        "temporal_api_key",
        "note_webhook_secret",
        "audit_anchor_secret",
    ],
)
def test_every_converted_credential_is_redacted_by_value(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted per field and *by value*, which is the assertion the old test was missing.

    Listing the names proves the inventory mentions them. Only feeding a value through
    `redact_secrets` proves the inventory can read them.
    """
    from chemclaw.core import logging as clog
    from chemclaw.core.config import settings

    canary = f"CANARY-{name}-0123456789"
    monkeypatch.setattr(settings, name, SecretStr(canary))
    assert canary not in clog.redact_secrets(f"trace mentioning {canary}")


def test_a_plain_string_credential_is_still_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: not every secret setting is a `SecretStr`, and those must keep working.

    A DSN password is the case that matters, because it is what a connect error quotes.
    """
    from chemclaw.core import logging as clog
    from chemclaw.core.config import settings

    canary = "postgresql://u:CANARYPASSWORD123@h:5432/db"
    monkeypatch.setattr(settings, "postgres_dsn", canary)
    assert "CANARYPASSWORD123" not in clog.redact_secrets(f"cannot connect: {canary}")


# --- F2: `_closing` never reached the generator on the shape production produces ---------------


def _wrapped_stream(closed: list[str], cleaned: list[str]) -> Any:
    """The shape `agent.run(stream=True)` actually returns: a stream wrapping a stream.

    `ChatAgent.run` hands back `ResponseStream.from_awaitable(...)` over a `.map(...)`-wrapped
    inner stream, so the outer object's `_iterator` is another `ResponseStream` — which has no
    `aclose` — and the outer's own `cleanup_hooks` list is empty. A *flat*
    `ResponseStream(async_generator)`, which is what the first version of this test built, has
    neither property and cannot see the defect.
    """

    async def _source() -> AsyncIterator[int]:
        try:
            for index in range(10):
                yield index
        finally:
            closed.append("source closed")

    inner: Any = ResponseStream(_source(), cleanup_hooks=[lambda: cleaned.append("inner cleanup")])
    return inner.map(lambda update: update, finalizer=list)


def test_abandoning_a_wrapped_stream_releases_the_generator_underneath_it() -> None:
    """The finding: `_closing` read `_iterator` once, found a `ResponseStream`, and did nothing.

    So on every real turn the HTTP response to the model stayed open until garbage collection —
    the exact connection-pool exhaustion the helper's docstring says it prevents.
    """
    from chemclaw.api.runner import _closing

    closed: list[str] = []
    cleaned: list[str] = []

    async def _run() -> None:
        async with _closing(_wrapped_stream(closed, cleaned)) as stream:
            async for update in stream:
                if update == 2:
                    break

    asyncio.run(_run())
    assert closed == ["source closed"], "the generator at the bottom of the nest was not released"
    assert cleaned == ["inner cleanup"], "the inner stream's cleanup hooks never ran"


def test_the_walk_terminates_on_a_stream_that_points_at_itself() -> None:
    """`ResponseStream.__aiter__` returns `self`, so a naive walk down `_iterator` can loop.

    Cheap to guard and expensive to discover: the failure would be a turn that never finishes
    tearing down, on the teardown path.
    """
    from chemclaw.api.runner import _closing

    class _SelfReferential:
        def __init__(self) -> None:
            self._iterator = self

    async def _run() -> None:
        async with _closing(_SelfReferential()):
            pass

    asyncio.run(asyncio.wait_for(_run(), timeout=5))


# --- F9: the non-note citation branch forged wikilinks ----------------------------------------


def test_a_hostile_retriever_key_cannot_forge_an_edge_in_a_report() -> None:
    """The branch closed this for ELN bodies and left it open one file over.

    A warehouse retriever's source is `<source>:<row key>` built from warehouse data. The
    "not a note id, so write it as plain text" branch wrote it *verbatim* into the report note's
    body — where `Note.outgoing_links` reads a `[[supersedes:…]]` sitting in the middle of it as a
    real edge, and the generated report proposes retiring another team's result.
    """
    from chemclaw.kg.note import Note
    from chemclaw.retrieval.harness import _citation

    hostile = "x]] and [[supersedes:reaction-eln-0001]] [[z"
    body = f"Findings rest on {_citation(hostile)}.\n"
    note = Note(id="report-probe", type="report", body=body)
    # `outgoing_links()` returns bare target ids. The first version of this destructured them as
    # `(relation, target)` pairs, which mypy caught: on the fixed tree the list is empty so the
    # comprehension never ran and the test passed without asserting anything, and under mutation it
    # raised a `ValueError` from unpacking a string rather than failing its assertion. Passing for
    # the wrong reason on one side and failing for the wrong reason on the other.
    assert note.outgoing_links() == [], (
        f"the report body carries a forged edge: {note.outgoing_links()}"
    )


def test_a_real_note_id_still_becomes_a_link() -> None:
    """The bound: the reduction must not cost the citation its whole purpose."""
    from chemclaw.retrieval.harness import _citation

    assert _citation("reaction-e-1041") == "[[reaction-e-1041]]"


def test_an_ordinary_warehouse_key_stays_readable() -> None:
    """A colon-bearing provenance label is the common case and must survive recognisably."""
    from chemclaw.retrieval.harness import _citation

    assert _citation("eln-snowflake:12") == "source eln-snowflake:12"


# --- F3: the framing-secret guard was silent on the shipped default ---------------------------


def test_the_framing_secret_warning_does_not_depend_on_the_session_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard fired only under `session_store="postgres"`, which is not the default.

    A *connector* frames in its own process by construction, so the per-process nonce mismatch is
    guaranteed on any deployment running one — regardless of how sessions are stored. The guard was
    therefore silent on exactly the configuration the chart ships.
    """
    import logging

    from chemclaw.core.config import Settings

    with caplog.at_level(logging.WARNING):
        Settings(session_store="memory", framing_envelope_secret="")
    assert any("FRAMING_ENVELOPE_SECRET" in record.message for record in caplog.records)


def test_a_configured_secret_warns_about_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The other direction, so the guard cannot degenerate into an unconditional warning."""
    import logging

    from chemclaw.core.config import Settings

    with caplog.at_level(logging.WARNING):
        Settings(framing_envelope_secret="a-deployment-wide-value")
    assert not any("FRAMING_ENVELOPE_SECRET" in record.message for record in caplog.records)


# --- F5: the disposal predicate named a state that does not exist -----------------------------


def test_the_proposal_predicate_names_states_that_exist() -> None:
    """`state <> 'pending'` was unconditionally true: there is no `pending`.

    The column `CHECK`s `open|merged|rejected|failed` and `ProposalState` enumerates the same four,
    so the guard the comment called "the second of two" contributed nothing. Named positively now,
    and `failed` is excluded with `open`: its own docstring says it is *not a decision* — the
    submission never reached git — and it is kept so the proposal can be replayed.
    """
    from chemclaw.durable.retention import _PRUNABLE
    from chemclaw.kg.proposal import ProposalState

    _, disposable = _PRUNABLE["note_proposals"]
    named = {word.strip("(),'") for word in disposable.split() if word.strip("(),'")}
    states = {state.value for state in ProposalState}
    assert named & states, f"the predicate names no real state: {disposable!r}"
    assert not (named & {"pending"}), "the predicate still names a state that cannot exist"
    assert "failed" not in named, "a failed submission is not a decision and must stay replayable"


# --- F6: the sweep pruned the wrong database on a split deployment ----------------------------


def test_the_retention_sweep_follows_the_session_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every table this sweep prunes is a session-layer table, and they follow `session_store_dsn`.

    Opening `postgres_dsn` unconditionally meant a split deployment deleted from the calculation
    database's empty copies and reported `deleted: {..: 0}` — which an operator reads as the policy
    being enforced while the real leases and ledger grow without bound. The copies exist on both
    databases precisely because this branch taught `migrate.py` to create them there.
    """
    from chemclaw.core.config import settings
    from chemclaw.durable import retention

    opened: list[str] = []
    monkeypatch.setattr(settings, "session_store_dsn", "postgresql://u:p@sessions:5432/sessions")
    monkeypatch.setattr(settings, "retention_session_events_days", 0)
    monkeypatch.setattr(settings, "retention_session_messages_days", 0)
    monkeypatch.setattr(settings, "retention_session_turns_days", 0)
    monkeypatch.setattr(settings, "retention_turn_costs_days", 0)
    monkeypatch.setattr(settings, "retention_note_proposals_days", 0)

    class _Conn:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def _bounded(dsn: str | None = None) -> object:
        opened.append(str(dsn))
        return _Conn()

    monkeypatch.setattr(retention, "bounded", _bounded)
    asyncio.run(retention.prune_expired_rows())

    assert opened == ["postgresql://u:p@sessions:5432/sessions"], (
        "the sweep prunes the calculation database while the session tables live elsewhere"
    )


# --- F7: two spellings of one slug rule -------------------------------------------------------


def test_the_slug_predicate_and_the_model_agree_on_a_trailing_newline() -> None:
    """`match` stops at a final newline where `fullmatch` does not, and `Note` uses `fullmatch`.

    Narrow — no traversal survives, since `/` is outside the charset — but unifying the two
    spellings is the entire reason `is_note_slug` was extracted.
    """
    from chemclaw.kg.note import is_note_slug, note_relative_path

    assert not is_note_slug("reaction-e-1041\n")
    with pytest.raises(ValueError, match="plain slug"):
        note_relative_path("reaction", "reaction-e-1041\n")


# --- F4: the workload-identity label reached one of two pod templates -------------------------


def test_every_pod_template_carries_the_workload_identity_label() -> None:
    """Counted per pod template, not searched per file — which is how the omission survived.

    `deployment-connectors.yaml` holds two pod templates, the connector server and the connector
    worker. The label was added to the server block, and the guard asked only whether the string
    appeared *somewhere* in the file. `qm` declares no endpoint, so the worker template is its only
    pod: the bundle that talks to HPC was the one left without a projected token.
    """
    label = 'azure.workload.identity/use: "true"'
    offenders = []
    for path in sorted((CHART / "templates").glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        pod_specs = text.count("serviceAccountName:")
        if pod_specs and text.count(label) < pod_specs:
            offenders.append(f"{path.name} ({text.count(label)}/{pod_specs})")
    assert not offenders, f"pod templates without the workload-identity label: {offenders}"
