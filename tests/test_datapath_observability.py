"""The data path leaves evidence behind: one record per pass, a duration, and a number to alert on.

Measured before any of this existed: a grep for `perf_counter` or `monotonic` over `ingest/`,
`retrieval/`, `memory/`, `kg/`, `publish/` and `core/` returned **two** hits, both a cache TTL in
`kg/graph.py`. Not one duration was measured anywhere in ~26,000 lines, and the only two latency
histograms in the system were both recorded from `api/` and `agent/`. So the packages that carry
every corpus, every embedding, every published result and every database call were, from outside,
indistinguishable from packages that were not running.

These tests drive the real functions against the real registry rather than asserting that a call
was made — the discipline `tests/test_metrics_bridge.py` records — because the failure mode being
closed is not "the wrong function was called", it is "nothing was emitted at all".
"""

import asyncio
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chemclaw.core import db, embeddings
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.migrate import migrate
from chemclaw.ingest.documents.binding import load_binding
from chemclaw.ingest.documents.external_index import _report_unresolved
from chemclaw.ingest.documents.index import InMemoryDocumentIndex
from chemclaw.ingest.documents.sync import reembed_stale, sync_share
from chemclaw.ingest.eln import cursor as eln_cursor
from chemclaw.kg.git_submitter import GitNoteSubmitter, GitRemoteError, _is_auth_failure
from chemclaw.publish import outbox
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever
from chemclaw.retrieval.fanout import record_kept_chunks, sweep_sources
from tests.pg import migrated_db_or_skip

_SOURCE = "sharedrive-observability"


def _counter(name: str) -> float:
    """One counter's total across every label set — what `Metrics.value` reports."""
    return METRICS.value(name)


def _series(name: str, **labels: str) -> float:
    """One labelled series' value, read out of the rendered exposition.

    Read from the text rather than from a private dict on purpose: the exposition *is* the
    contract with Prometheus, and a series that renders wrong is a series nobody can alert on
    however right the in-memory number is.
    """
    wanted = [f'{label}="{value}"' for label, value in labels.items()]
    for line in METRICS.render().splitlines():
        head, _, reading = line.partition("} ")
        if head.startswith(f"{name}{{") and all(pair in head for pair in wanted):
            return float(reading)
    raise AssertionError(f"no series {name}{{{', '.join(wanted)}}} in the exposition")


def _rendered(name: str) -> list[str]:
    """Every exposition line for `name` (the metric's own lines, not its HELP/TYPE)."""
    return [
        line
        for line in METRICS.render().splitlines()
        if line.startswith(name) and not line.startswith("#")
    ]


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The `event` field of every structured record captured — `log_event`'s discriminator."""
    return [str(record.event) for record in caplog.records if hasattr(record, "event")]


# --- G4: a pass produces a record -----------------------------------------------------------


def _share(tmp_path: Path) -> dict[str, Any]:
    """A two-file share: one indexable document and one extension nothing here can read."""
    mount = tmp_path / "share"
    (mount / "Projects").mkdir(parents=True)
    (mount / "Projects" / "run.txt").write_text("A nitration at 40 C in toluene.", encoding="utf-8")
    (mount / "Projects" / "legacy.doc").write_bytes(b"\x00binary")
    return {
        "mount": str(mount),
        "roots": [{"path": "."}],
        "extensions": [".txt"],
        "public": True,
    }


def test_a_document_sync_pass_leaves_exactly_one_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`SyncReport` was built, returned, and logged nowhere: a TB sweep was silent by design.

    One `ingest.finished` per pass, carrying the report's fields — including the per-extension
    skips, which is the number this module's own docstring calls the answer that is never silence.
    """
    index = InMemoryDocumentIndex()
    binding = load_binding(_share(tmp_path))
    before = _counter("chemclaw_ingest_records_total")
    with caplog.at_level(logging.INFO, logger="chemclaw.ingest.documents.sync"):
        report = asyncio.run(sync_share(_SOURCE, binding, index))

    finished = [r for r in caplog.records if getattr(r, "event", "") == "ingest.finished"]
    assert len(finished) == 1
    fields = finished[0].__dict__
    assert fields["source"] == _SOURCE
    assert fields["indexed"] == report.indexed == 1
    assert fields["duration_s"] >= 0.0
    assert fields["next_cursor"] == report.cursor
    # The `.doc` never reaches the index and would otherwise be invisible in every count.
    assert fields["skipped_unsupported"] == {".doc": 1}
    assert _counter("chemclaw_ingest_records_total") > before
    assert _series("chemclaw_ingest_records_total", source=_SOURCE, outcome="ingested") == 1.0


def test_a_pass_that_indexed_nothing_still_leaves_a_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty crawl returns early — which is the pass most worth being able to see.

    A share that stopped mounting crawls to nothing, and the four early returns in the pass each
    used to end in silence. The record is emitted by the wrapper, so the absence of a record now
    means the pass did not run rather than the share being empty.
    """
    (tmp_path / "empty").mkdir()
    binding = load_binding(
        {"mount": str(tmp_path / "empty"), "roots": [{"path": "."}], "public": True}
    )
    with caplog.at_level(logging.INFO, logger="chemclaw.ingest.documents.sync"):
        asyncio.run(sync_share(_SOURCE, binding, InMemoryDocumentIndex()))

    assert _events(caplog).count("ingest.finished") == 1


# --- G5: ingest lag ---------------------------------------------------------------------------


def test_a_cursor_that_stands_still_reports_a_growing_lag() -> None:
    """`sync_cursors` carried a cursor and an `updated_at` and nothing read either for monitoring.

    The lag is computed at scrape time from the observed cursor, so a wedged source — one whose
    fetch keeps returning the same page and never advances — reports a climbing number while its
    own log keeps reading `ingested=N rejected=0`.
    """
    eln_cursor.observe_cursor("wedged-eln", datetime.now(UTC) - timedelta(hours=3))
    eln_cursor.observe_cursor("fresh-eln", datetime.now(UTC))

    wedged = _series("chemclaw_ingest_cursor_lag_seconds", source="wedged-eln")
    fresh = _series("chemclaw_ingest_cursor_lag_seconds", source="fresh-eln")
    assert 10_000 < wedged < 11_000  # three hours, in seconds
    assert fresh < 60


# --- G1: what survived the merge, and how long a source took ----------------------------------


class _Retriever:
    """A source that returns a fixed hit list, or raises, or takes a moment."""

    def __init__(self, name: str, chunks: list[EvidenceChunk], fail: bool = False) -> None:
        self.name = name
        self._chunks = chunks
        self._fail = fail

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Answer with the fixed list, or raise as an unreachable backing store would."""
        if self._fail:
            raise RuntimeError("backing store unreachable")
        return list(self._chunks)


def _chunk(source: str) -> EvidenceChunk:
    """One chunk attributed to `source`, which is what the kept counter is labelled by."""
    return EvidenceChunk(content="x", source_note_id="note-1", retriever=source)


def test_a_starved_source_reads_as_zero_rather_than_as_absent() -> None:
    """The ADR's own table, as a ratio: contributed 2, survived 0.

    `chemclaw_evidence_source_chunks_total` counts what a retriever handed over — pre-merge — so a
    leg that contributes and survives nothing is indistinguishable from a healthy one. Seeding the
    kept series at zero is what gives the ratio a denominator at the moment it matters; without it
    the starved source would simply be missing from the metric.
    """
    record_kept_chunks([_chunk("graph")], asked=["graph", "lexical"])

    assert _series("chemclaw_evidence_source_kept_total", source="graph") >= 1.0
    assert _series("chemclaw_evidence_source_kept_total", source="lexical") == 0.0


def test_every_evidence_source_is_timed_including_the_one_that_failed() -> None:
    """A vector store that is timing out and one that is empty both return `[]`.

    So the duration is the only thing that separates them, and it has to be recorded on the
    failing path too — a leg that raises after twenty seconds and one that raises immediately are
    different faults.
    """
    sources: list[tuple[str, SourceRetriever]] = [
        ("graph", _Retriever("graph", [_chunk("graph")])),
        ("vector", _Retriever("vector", [], fail=True)),
    ]
    asyncio.run(sweep_sources(sources, "why", {}))

    rendered = _rendered("chemclaw_evidence_source_seconds")
    assert any('source="graph"' in line and "_count" in line for line in rendered)
    assert any('source="vector"' in line and "_count" in line for line in rendered)


# --- G7: external vector store drift ----------------------------------------------------------


def test_points_the_catalogue_cannot_resolve_are_counted_and_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`len(addressed)`, `len(rows)` and `len(hits)` were three numbers nothing compared.

    A Qdrant collection holding points whose `document_chunks` rows were swept gives `top_k`
    matches in and zero hits out, and the fan-out books an honest `chunks=0`.
    """
    before = _counter("chemclaw_vector_unresolved_points_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.documents.external_index"):
        _report_unresolved(addressed=5, rows=3, hits=2)

    assert _counter("chemclaw_vector_unresolved_points_total") == before + 3
    assert "drifted" in caplog.text
    caplog.clear()
    _report_unresolved(addressed=4, rows=4, hits=4)
    assert not caplog.records  # nothing to say when everything resolved


# --- G6: embeddings ---------------------------------------------------------------------------


def test_every_embedding_call_is_counted_and_timed() -> None:
    """301 lines with one log call in them, no timing and no counter for calls or failures."""
    before = _counter("chemclaw_embedding_calls_total")
    count_before, _ = METRICS.observations("chemclaw_embedding_duration_seconds")

    embeddings.embed_texts(["ethanol in toluene"], cache=False)

    assert _counter("chemclaw_embedding_calls_total") == before + 1
    count_after, _ = METRICS.observations("chemclaw_embedding_duration_seconds")
    assert count_after == count_before + 1
    assert _series("chemclaw_embedding_calls_total", outcome="ok") >= 1.0


def test_a_failing_embedding_provider_names_itself(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Retries live inside the SDK with no callback, so a failure had to be visible here or nowhere.

    The exception still propagates — nothing continues with less — but the *embedder* is named,
    with the exception type and the batch size, neither of which survives into any caller's own
    handler.
    """

    def broken(text: str) -> list[float]:
        raise RuntimeError("provider refused")

    monkeypatch.setattr(embeddings, "_hash_embedding", broken)
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.embeddings"):
        with pytest.raises(RuntimeError):
            embeddings.embed_texts(["x"], cache=False)

    assert "embedding.failed" in _events(caplog)
    assert _series("chemclaw_embedding_calls_total", outcome="failure") >= 1.0


# --- G12: a re-embed pass that made no progress -----------------------------------------------


def test_a_re_embed_that_failed_everything_does_not_report_completion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`has_more=False` on a total failure is byte-identical to "everything is up to date".

    The progress gate is right — a deterministic batch would otherwise be retried forever — so the
    third state gets its own field instead of being folded into the two that already existed.
    """

    def broken(texts: list[str], **kwargs: object) -> list[list[float]]:
        raise RuntimeError("provider down")

    index = InMemoryDocumentIndex()
    asyncio.run(
        index.upsert(
            [],
            [],
            "key-a",
        )
    )
    monkeypatch.setattr("chemclaw.ingest.documents.sync.embed_texts", broken)

    class _Stale(InMemoryDocumentIndex):
        """An index holding one chunk cut under a superseded configuration."""

        async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[Any]:
            """One stale chunk, deterministically — the shape that wedges a drain."""
            from chemclaw.ingest.documents.index import StaleChunk

            return [StaleChunk(doc_id="doc-1", chunking_key="c", ordinal=0, content="text")]

    with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.documents.sync"):
        report = asyncio.run(reembed_stale(_Stale(), {"c"}, limit=10))

    assert report.embedded == 0
    assert report.failed == 1
    assert report.has_more is False
    assert report.stalled is True
    assert "RuntimeError x1" in caplog.text  # the distinct reasons, summarised once


# --- G15: an expired credential is not a network partition ------------------------------------


def test_git_stderr_reaches_the_log_at_the_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`GitRemoteError` carried git's stderr, and `durable/publish.py` dropped it for the label.

    So the one text that says *why* a push failed never reached a log at all. Driven against a real
    `git` process, because what is being checked is that git's own words survive.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    submitter = GitNoteSubmitter(repo_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.git_submitter"):
        with pytest.raises(GitRemoteError):
            asyncio.run(submitter._git("push", "no-such-remote", "main", transient=True))

    assert "git.failed" in _events(caplog)
    assert "no-such-remote" in caplog.text


def test_an_authentication_failure_is_classified_apart_from_a_partition() -> None:
    """A 403 from the git host is a fact about the token; no number of retries installs one."""
    assert _is_auth_failure(
        "remote: Invalid username or password.\nfatal: Authentication failed for 'https://host/x'"
    )
    assert _is_auth_failure(
        "fatal: unable to access 'https://h/': The requested URL returned error: 403"
    )
    assert not _is_auth_failure("fatal: unable to access 'https://h/': Could not resolve host: h")
    assert not _is_auth_failure("fatal: 'origin' does not appear to be a git repository")


# --- G8, G9, G3: the Postgres-backed half ------------------------------------------------------


def test_a_slow_unit_of_work_names_its_operation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`core/db.py` had zero timing calls: no query latency and no slow-query threshold at all."""
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(settings, "pg_slow_query_seconds", 0.05)

    async def run() -> None:
        async with db.connection(settings.postgres_dsn, operation="probe_slow") as conn:
            await conn.execute("SELECT pg_sleep(0.2)")

    with caplog.at_level(logging.WARNING, logger="chemclaw.core.db"):
        asyncio.run(run())

    assert "db.slow" in _events(caplog)
    assert any(
        'operation="probe_slow"' in line for line in _rendered("chemclaw_db_query_duration_seconds")
    )


def test_a_cancelled_statement_is_counted_as_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    """The 30 s `statement_timeout` raises `QueryCanceled`, which nothing caught, counted or named.

    So a database cancelling a runaway query looked, from every dashboard, exactly like a database
    that was down.
    """
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_db_query_failures_total")

    async def run() -> None:
        async with db.connection(
            settings.postgres_dsn, statement_timeout_seconds=0.1, operation="probe_cancel"
        ) as conn:
            await conn.execute("SELECT pg_sleep(5)")

    with caplog.at_level(logging.WARNING, logger="chemclaw.core.db"):
        with pytest.raises(psycopg.errors.QueryCanceled):
            asyncio.run(run())

    assert _counter("chemclaw_db_query_failures_total") == before + 1
    assert _series("chemclaw_db_query_failures_total", kind="cancelled") >= 1.0
    assert "db.failed" in _events(caplog)


def test_a_migration_names_the_file_it_is_applying(caplog: pytest.LogCaptureFixture) -> None:
    """A deploy blocked on the advisory lock and one running normally produced identical output.

    Which was nothing at all, until everything had already completed — in a `pre-install` hook Job,
    where "which migration is it stuck on?" is the first question anyone asks.
    """
    asyncio.run(migrated_db_or_skip())
    with caplog.at_level(logging.INFO, logger="chemclaw.core.migrate"):
        asyncio.run(migrate())

    events = _events(caplog)
    assert "migrate.waiting" in events  # said *before* the wait, which is the whole point
    assert "migrate.locked" in events
    assert "migrate.finished" in events


def test_the_outbox_backlog_is_a_count_and_an_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented formula was wrong three ways; this reads the queue instead.

    Executed on the old formula: queued=10, published=0, failures=50, and the true pending row
    count was 0. The age is what separates a backlog of five that turns over every second from a
    backlog of five that has not moved since Tuesday.
    """
    asyncio.run(migrated_db_or_skip())

    async def run() -> None:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications")
            await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version, "
                "enqueued_at) VALUES ('lims', 'calc-1', '{}'::jsonb, '1', now() - interval '2 h')"
            )
            await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version, "
                "state) VALUES ('lims', 'calc-2', '{}'::jsonb, '1', 'failed')"
            )
            await conn.commit()
        await outbox.refresh_backlog()

    asyncio.run(run())

    assert _series("chemclaw_outbox_pending", sink="lims") == 1.0
    assert _series("chemclaw_outbox_oldest_pending_seconds", sink="lims") > 7000
    assert _series("chemclaw_outbox_dead_lettered", sink="lims") == 1.0


def test_a_row_that_spent_its_attempts_is_counted_as_dead_lettered() -> None:
    """A retired row never increments `published`, which is why the difference is not a backlog."""
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_results_dead_lettered_total")

    async def run() -> list[int]:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications")
            cursor = await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version, "
                "attempts) VALUES ('lims', 'calc-9', '{}'::jsonb, '1', %s) RETURNING id",
                (settings.result_publish_max_attempts,),
            )
            rows = await cursor.fetchall()
            await conn.commit()
        ids = [int(row[0]) for row in rows]
        await outbox.mark_failed(ids, "the endpoint refused")
        return ids

    asyncio.run(run())

    assert _counter("chemclaw_results_dead_lettered_total") == before + 1
