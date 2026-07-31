"""The durable record of a finished connector job (D-155) — the offline half.

What is under test here is everything about the record that does *not* need a Temporal server or a
database: the mapping from a run to its record, the provenance footer core stamps onto a
connector's note, and the sink selection that decides whether any of it is kept. The end-to-end
path (core's wrapper actually writing the record and publishing the stamped note) is in
`test_connector_job_workflow.py`, which needs a live server and skips offline — which is precisely
why the pure pieces are pulled out and pinned here instead.

`test_job_record_postgres.py` covers the store itself against a real database.
"""

import asyncio

import pytest

from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobInput, ConnectorJobResult, job_record_for
from chemclaw.durable.job_record import (
    JobRecord,
    NullJobRecordSink,
    default_job_record_sink,
    note_with_run_provenance,
    search_job_records,
)
from chemclaw.kg.note import Note

_INPUT = ConnectorJobInput(
    connector="bo",
    job="start_optimization_campaign",
    workflow="BoCampaignWorkflow",
    task_queue="connector-bo",
    payload={"objective_name": "solubility_max", "n_rounds": 4},
    rationale="the Tuesday batch stalled at 60% and we need a solvent that dissolves the amine",
    requested_by="oid-42",
    session_id="sess-7",
    correlation_id="turn-9",
)

_RESULT = ConnectorJobResult(
    summary="campaign finished after 9 evaluation(s); best objective -1.2 (predicted)",
    data={"best": {"value": -1.2}, "history": [{"value": -3.0}, {"value": -1.2}]},
    note=Note(id="bo-solubility-max-abc123", type="bo-candidate", created_by="agent", body="Best."),
)


def test_the_record_carries_the_arguments_the_whole_result_and_the_reason() -> None:
    """The three things that were unrecoverable once Temporal's history expired."""
    record = job_record_for("bo-start_optimization_campaign-deadbeef", _INPUT, _RESULT)

    assert record.job_id == "bo-start_optimization_campaign-deadbeef"
    assert record.connector == "bo" and record.job == "start_optimization_campaign"
    # The launch arguments — for a campaign, the decision space and the budget it was given.
    assert record.payload == {"objective_name": "solubility_max", "n_rounds": 4}
    # The *whole* envelope, not a summary of it: every observation the campaign paid for.
    assert record.result == _RESULT.data
    assert len(record.result["history"]) == 2
    assert record.rationale.startswith("the Tuesday batch stalled")
    # And the joins: who asked, which conversation, and the note it proposed.
    assert record.requested_by == "oid-42"
    assert record.session_id == "sess-7" and record.correlation_id == "turn-9"
    assert record.note_id == "bo-solubility-max-abc123"


def test_a_run_that_produced_no_note_records_an_empty_note_id() -> None:
    """Most jobs propose nothing; that is an empty join, not a missing record."""
    result = ConnectorJobResult(summary="done", data={})
    assert job_record_for("job-1", _INPUT, result).note_id == ""


def test_a_launch_with_no_reason_cannot_be_expressed() -> None:
    """The reject-if-absent rule holds at the type, not only at the tool that fills it in.

    The launcher refuses a blank rationale (`test_connector_jobs.py`), but the input model is the
    other construction site — `TemplateWorkflow` builds one directly — so the guarantee has to
    live here too, or a second caller could reintroduce the gap without touching the first.
    """
    with pytest.raises(ValueError, match="rationale"):
        ConnectorJobInput(
            connector="bo",
            job="start_optimization_campaign",
            workflow="BoCampaignWorkflow",
            task_queue="connector-bo",
            rationale="",
            requested_by="oid-42",
        )


def test_the_note_gains_the_reason_and_the_run_that_produced_it() -> None:
    """The answer to "why was this done" reaches the markdown file a human reviews and merges."""
    record = job_record_for("bo-campaign-abc", _INPUT, _RESULT)
    note = Note(
        id="bo-solubility-max-abc123",
        type="bo-candidate",
        created_by="agent",
        body="Recommended conditions:\n- solvent: 2-MeTHF\n",
    )

    stamped = note_with_run_provenance(note, record)

    assert "the Tuesday batch stalled at 60%" in stamped.body
    assert "bo-campaign-abc" in stamped.body
    assert "bo/start_optimization_campaign" in stamped.body
    assert "oid-42" in stamped.body
    # The original claim survives intact — the footer is added, never a rewrite.
    assert "- solvent: 2-MeTHF" in stamped.body
    # Everything else about the note is the connector's, untouched.
    assert stamped.id == note.id and stamped.type == note.type
    assert stamped.created_by == "agent"


def test_the_footer_adds_no_wikilink() -> None:
    """A link to a note that does not exist fails `kg-validate` on the PR this note opens.

    The job id names a database row, not a graph node, so it is rendered as code. This is the trap
    `note_from_campaign_result` documented for the connector — inherited by the footer that is now
    applied to *every* connector's note, where getting it wrong would break all of them at once.
    """
    stamped = note_with_run_provenance(
        Note(id="n-1", type="job-result", created_by="agent", body="Body."),
        job_record_for("qm-compute_dft_energy-99", _INPUT, _RESULT),
    )
    assert stamped.outgoing_links() == []


def test_stamping_does_not_mutate_the_connectors_note() -> None:
    """`Note` is frozen and shared: the tool's return value must not change under it."""
    note = Note(id="n-1", type="job-result", created_by="agent", body="Body.")
    note_with_run_provenance(note, job_record_for("job-1", _INPUT, _RESULT))
    assert note.body == "Body."


def test_the_sink_is_durable_wherever_a_database_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opting *in* to a record per call site is the polarity that failed for the audit trail.

    So the default follows `default_audit_sink`: a deployment that has stated it keeps durable
    records (`session_store="postgres"`) gets them for job runs too, without a second switch to
    forget.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(default_job_record_sink(), NullJobRecordSink)

    monkeypatch.setattr(settings, "session_store", "postgres")
    from chemclaw.durable.job_record_store import PostgresJobRecordSink

    assert isinstance(default_job_record_sink(), PostgresJobRecordSink)


def test_searching_without_a_store_answers_honestly_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`find_past_jobs` on a memory-store deployment reports no history, not an error."""
    monkeypatch.setattr(settings, "session_store", "memory")
    assert asyncio.run(search_job_records("suzuki")) == []


def test_the_null_sink_keeps_nothing_and_says_so() -> None:
    """The fallback must be inert, not half-working: no exception, no state."""
    record = job_record_for("job-1", _INPUT, _RESULT)
    assert asyncio.run(NullJobRecordSink().record(record)) is None
    assert isinstance(record, JobRecord)


def test_the_background_worker_actually_serves_the_record_activity() -> None:
    """A written-but-unregistered activity is the failure `durable/registry.py` exists to prevent.

    Sandbox-safe on purpose, like `test_the_wrapper_is_served_by_the_background_worker`: if nothing
    polls for `record_job`, every finished job retries the write to its bound and then logs — the
    result stays in Temporal, the record is never written, and the loss is invisible until an id
    expires months later. That is the worst possible time to discover it, so it is pinned by a test
    that runs everywhere rather than only where a Temporal server exists.
    """
    import chemclaw.durable.background_worker  # noqa: F401  (registers by import)
    from chemclaw.durable.job_record import record_job
    from chemclaw.durable.registry import registered_activities

    assert record_job in registered_activities("background")
