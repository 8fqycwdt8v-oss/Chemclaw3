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
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chemclaw.agent.research_tools import gather_evidence
from chemclaw.core import db, embeddings
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.migrate import migrate
from chemclaw.ingest.documents.binding import load_binding
from chemclaw.ingest.documents.external_index import _report_unresolved
from chemclaw.ingest.documents.index import InMemoryDocumentIndex
from chemclaw.ingest.documents.sync import reembed_stale, sync_share
from chemclaw.ingest.eln import cursor as eln_cursor
from chemclaw.kg import graph as kg_graph
from chemclaw.kg.git_writer import GitNoteWriter, GitRemoteError, _is_auth_failure
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


def _gauge(name: str) -> float:
    """One unlabelled gauge's reading, out of the rendered exposition.

    Read from the text for the reason `_series` gives: a gauge bound to a source that raises is
    *omitted* from the scrape and counted as a read failure, so the in-memory callable answering
    correctly is not evidence that anything can be alerted on.
    """
    for line in METRICS.render().splitlines():
        if line.startswith(f"{name} "):
            return float(line.split(" ", 1)[1])
    raise AssertionError(f"{name} is absent from the exposition")


def test_a_pod_serving_a_frozen_knowledge_corpus_says_how_old_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The knowledge graph coming *in* had no first-party signal, only the graph going out.

    `ChemclawKnowledgeNotesLost` covers a note that failed to reach the PR-gate. Nothing covered a
    pod whose corpus stopped arriving: `knowledge-sync.sh`'s `loop` swallows a failed refresh so a
    dead remote cannot kill the pod, and the pod then serves the frozen snapshot indefinitely while
    logging one WARNING per interval into a stream nobody tails. The sidecar's heartbeat lives in
    its own container's `/tmp`, so this is the half that is readable from the process that answers
    from the tree.

    Driven through the real registry's rendered exposition, because that is the contract the rule
    evaluates against — and because a gauge whose source raises is silently absent from it.
    """
    # Through the two settings `knowledge_path` derives from, because it is a read-only property:
    # one definition of where notes live, which is the point of it being derived at all.
    corpus = tmp_path / "knowledge"
    (corpus / "insight").mkdir(parents=True)
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    # The stat scan behind the gauge is cached for `knowledge_age_scan_ttl_seconds`, so this test
    # busts it after each write — it rewrites the tree three times inside one second, which is a
    # local writer's pattern and exactly what `invalidate_cache` exists for, not a pod's.
    # `test_the_corpus_age_gauge_is_not_a_tree_walk_per_scrape` is where the cache itself is driven.
    kg_graph.invalidate_cache()

    assert _gauge("chemclaw_knowledge_sync_age_seconds") == kg_graph.NO_NOTES, (
        "a tree with no note at all is the volume that was never populated, and 0 would read as a "
        "corpus that has just been refreshed"
    )

    note = corpus / "insight" / "frozen.md"
    note.write_text("---\nid: frozen\n---\nbody\n", encoding="utf-8")
    os.utime(note, (time.time() - 7200, time.time() - 7200))
    fresh = corpus / "insight" / "fresh.md"
    fresh.write_text("---\nid: fresh\n---\nbody\n", encoding="utf-8")
    kg_graph.invalidate_cache()

    # The *newest* note decides: one stale file beside a fresh one is an ordinary corpus.
    assert _gauge("chemclaw_knowledge_sync_age_seconds") < 60
    fresh.unlink()
    kg_graph.invalidate_cache()
    assert 7_000 < _gauge("chemclaw_knowledge_sync_age_seconds") < 7_400


def test_the_corpus_age_gauge_is_not_a_tree_walk_per_scrape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live gauge callback runs inside `/metrics`, which is served on the event loop.

    The gauge shipped as an unguarded `rglob` + `stat` sweep of the whole knowledge tree, evaluated
    on every scrape. Measured on this sandbox, `METRICS.render()` went from 0.128 ms on an empty
    tree to 8.7 ms at 1k notes and 102.6 ms at 10k — and `api/routes/ops.py::metrics` renders
    synchronously inside an `async def`, so that is the front door's whole event loop stalled every
    30 s, not one request's latency. `_dir_fingerprint` does the same sweep and has been TTL-gated
    since DA-5; this one had no gate at all.

    Both halves are asserted here, because fixing the cost by capping the freshness would have
    replaced a slow gauge with a lying one:

    1. Repeated scrapes inside the window walk the tree **once**.
    2. The age still **grows in real time** while that entry is warm — a corpus that stopped
       arriving cannot be cached into looking fresh, because what is cached is the newest note's
       mtime and the age is recomputed from the clock on every read.
    """
    corpus = tmp_path / "knowledge" / "insight"
    corpus.mkdir(parents=True)
    note = corpus / "frozen.md"
    note.write_text("---\nid: frozen\n---\nbody\n", encoding="utf-8")
    frozen_at = time.time() - 7200
    os.utime(note, (frozen_at, frozen_at))
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    kg_graph.invalidate_cache()

    walks = 0
    real_scan = kg_graph.scan_notes_dir

    def counting_scan(notes_dir: Path) -> Any:
        nonlocal walks
        walks += 1
        return real_scan(notes_dir)

    monkeypatch.setattr(kg_graph, "scan_notes_dir", counting_scan)

    readings = [_gauge("chemclaw_knowledge_sync_age_seconds") for _ in range(5)]
    assert walks == 1, (
        f"five scrapes walked the tree {walks} times — the gauge is back to an O(notes) sweep per "
        "scrape, on the event loop that serves every other request"
    )
    assert all(7_000 < value < 7_400 for value in readings), readings

    # The half that must survive the cache: an hour passes with no sync and no rescan. The reading
    # has to move, or a wedged corpus would read as whatever it read when the entry was filled.
    # `kg/graph.py` reads the clock as `time.time()` through the stdlib module, so this is the same
    # object it will call. Captured before the patch, or the replacement calls itself; monkeypatch
    # puts it back, and the shift is a shifted log timestamp to everything else in the meantime.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    assert 10_600 < _gauge("chemclaw_knowledge_sync_age_seconds") < 11_000, (
        "the age was cached rather than the mtime, so a corpus that stopped arriving stops "
        "reporting that it did — which is the failure this gauge exists for"
    )
    assert walks == 1, "reading the age must not need a rescan"


def test_a_slower_earlier_scan_cannot_clobber_a_fresher_concurrent_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact race a fresh-context review found: last-writer-wins was not last-*scanner*-wins.

    Two scrapes can race a cold `_NEWEST_MTIME` entry. Before this fix the write back was plain
    last-writer-wins with no check against `scanned_at`, so whichever call happened to *finish*
    last won — even if it *started* first and therefore looked at an older, possibly-stale view of
    the corpus. Concretely: scan A starts, then a note is written, then scan B starts and finishes
    (sees the new note, writes the fresh result), then scan A — slower, e.g. scheduling or disk
    contention — finally finishes and overwrites B's fresh result with its own stale one. For up to
    one more `knowledge_age_scan_ttl_seconds` window, `knowledge_sync_age_seconds()` would then
    over-report staleness by however old A's view was.

    Reproduced deterministically rather than with real thread timing (a timing-dependent test would
    be flaky rather than a proof): `time.monotonic` is patched to hand out the two scans'
    `scanned_at` values in *start* order while the two calls to `_newest_note_mtime` still happen in
    *finish* order — B (started later) first, A (started earlier) second — which is exactly "A
    started first but finished last" without needing real concurrency.
    """
    corpus = tmp_path / "knowledge" / "insight"
    corpus.mkdir(parents=True)
    note = corpus / "note.md"
    note.write_text("---\nid: note\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    # TTL=0 so every call below runs a fresh scan and reaches the write-back instead of being
    # answered out of the (still-empty) cache — the race is in the write, not in the cache hit.
    monkeypatch.setattr(settings, "knowledge_age_scan_ttl_seconds", 0)
    kg_graph.invalidate_cache()

    scan_a_started_at = 100.0  # A started first...
    scan_b_started_at = 200.0  # ...but B started later and, in this race, finishes first.
    stale_mtime = time.time() - 3600  # what A saw before the note below existed
    fresh_mtime = time.time()  # what B saw after the note was (re)written

    monotonic_values = iter([scan_b_started_at, scan_a_started_at])
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_values))

    real_scan = kg_graph.scan_notes_dir
    mtimes = iter([fresh_mtime, stale_mtime])  # B's view, then A's — in call (finish) order

    def fake_scan(notes_dir: Path) -> Any:
        real_stat = next(iter(real_scan(notes_dir)))[1]
        fake_stat = os.stat_result(
            (
                real_stat.st_mode,
                real_stat.st_ino,
                real_stat.st_dev,
                real_stat.st_nlink,
                real_stat.st_uid,
                real_stat.st_gid,
                real_stat.st_size,
                real_stat.st_atime,
                next(mtimes),
                real_stat.st_ctime,
            )
        )
        return iter([(note, fake_stat)])

    monkeypatch.setattr(kg_graph, "scan_notes_dir", fake_scan)

    # B finishes first: scans (sees the fresh mtime), stamps scanned_at=200.0, writes.
    b_result = kg_graph._newest_note_mtime(settings.knowledge_path)
    assert b_result == fresh_mtime
    # A finishes second, but it *started* first (scanned_at=100.0 < B's 200.0): its stale result
    # must not overwrite B's fresher one.
    a_result = kg_graph._newest_note_mtime(settings.knowledge_path)
    assert a_result == stale_mtime, "the scan itself still reports what it saw"

    cached = kg_graph._NEWEST_MTIME[str(settings.knowledge_path)]
    assert cached == (scan_b_started_at, fresh_mtime), (
        "A's write (scanned_at=100.0, stale) clobbered B's fresher write (scanned_at=200.0, "
        f"fresh) — the cache must keep whichever scan started last, got {cached}"
    )


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


def _chunk(source: str, note_id: str = "note-1") -> EvidenceChunk:
    """One chunk attributed to `source`, citing `note_id`.

    The id is a parameter because the kept counter is keyed on `(source_note_id, content)` — the
    same key both merge paths dedup on — so two chunks sharing an id are *one note two legs found*,
    which is a different situation from two legs finding different notes.
    """
    return EvidenceChunk(content="x", source_note_id=note_id, retriever=source)


def test_a_starved_source_reads_as_zero_rather_than_as_absent() -> None:
    """The ADR's own table, as a ratio: contributed 2, survived 0.

    `chemclaw_evidence_source_chunks_total` counts what a retriever handed over — pre-merge — so a
    leg that contributes and survives nothing is indistinguishable from a healthy one. Seeding the
    kept series at zero is what gives the ratio a denominator at the moment it matters; without it
    the starved source would simply be missing from the metric.
    """
    offered = _chunk("graph")
    record_kept_chunks([offered], {"graph": [offered], "lexical": [_chunk("lexical", "note-2")]})

    assert _series("chemclaw_evidence_source_kept_total", source="graph") >= 1.0
    assert _series("chemclaw_evidence_source_kept_total", source="lexical") == 0.0


def test_a_note_two_legs_agreed_on_counts_for_both_of_them() -> None:
    """Agreement is the healthy case and must not read as starvation.

    Both merge paths keep the *first* occurrence of a note, so `chunk.retriever` names only the leg
    that found it first. Attributing the kept count by that field credited every shared note to
    whichever source `_sources()` happened to list first — measured on a healthy three-leg corpus,
    `graph 16, lexical 0, vector 0`, which is exactly what a starved leg looks like. The one metric
    built to detect `D-2026-08-01-a-cap-that-starves-a-source` was therefore pinned at zero for
    every index-backed leg in every hybrid deployment.
    """
    shared = _chunk("graph")
    before_lexical = _series("chemclaw_evidence_source_kept_total", source="lexical")
    record_kept_chunks([shared], {"graph": [shared], "lexical": [shared]})

    assert _series("chemclaw_evidence_source_kept_total", source="lexical") == before_lexical + 1.0


@pytest.mark.anyio
async def test_gathering_evidence_records_the_surviving_count_without_being_asked_to() -> None:
    """Driven through `gather_evidence`, because the unit test above cannot see the defect.

    `record_kept_chunks` shipped with **no caller**: the only invocation in the tree was the test
    one line up, calling it directly. So the helper was covered, the metric was declared, the ADR
    said `research_tools` "must call" it, a dashboard panel queried it — and the series had no
    producer, which is the `audit_events.agent` shape this repository has two ADRs about.
    `test_every_declared_metric_is_named_somewhere_in_the_source` could not catch it either: the
    name is a literal inside the helper, and a helper nothing calls still names it.

    A test that drives the real path is the only kind that can fail for the real reason, so this
    one asks `gather_evidence` for evidence and looks at the registry afterwards.
    """
    before = _series("chemclaw_evidence_source_kept_total", source="graph")
    await gather_evidence("anything at all")
    after = _series("chemclaw_evidence_source_kept_total", source="graph")

    assert after > before, (
        "gather_evidence recorded no surviving-chunk count — `record_kept_chunks` has lost its "
        "only caller again, and the kept/chunks ratio has no numerator"
    )


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
    # The WARNING is throttled per collection (`tests/test_datapath_review_metrics.py`), so this
    # names one nothing else uses — the point here is that drift is *said*, not how often.
    with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.documents.external_index"):
        _report_unresolved(addressed=5, rows=3, hits=2, collection="observability-drifted")

    assert _counter("chemclaw_vector_unresolved_points_total") == before + 3
    assert "drifted" in caplog.text
    caplog.clear()
    _report_unresolved(addressed=4, rows=4, hits=4, collection="observability-healthy")
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
    # `error`, the value the metric's own HELP names — see `tests/test_datapath_review_metrics.py`
    # for why a rule written from the HELP used to select nothing.
    assert _series("chemclaw_embedding_calls_total", outcome="error") >= 1.0


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
    submitter = GitNoteWriter(repo_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.git_writer"):
        with pytest.raises(GitRemoteError):
            asyncio.run(submitter._git("push", "no-such-remote", "main", transient=True))

    assert "git.failed" in _events(caplog)
    assert "no-such-remote" in caplog.text


def test_an_authentication_failure_is_classified_apart_from_a_partition() -> None:
    """A dead credential is a fact about the token; no number of retries installs one."""
    assert _is_auth_failure(
        "remote: Invalid username or password.\nfatal: Authentication failed for 'https://host/x'"
    )
    # A forge's genuine denial names the reason as well as the status, which is what classifies it.
    assert _is_auth_failure(
        "remote: Permission to owner/repo.git denied to bot.\n"
        "fatal: unable to access 'https://h/': The requested URL returned error: 403"
    )
    assert not _is_auth_failure("fatal: unable to access 'https://h/': Could not resolve host: h")


def test_a_bare_403_stays_transient_because_a_forge_throttles_with_one() -> None:
    """The status line alone must not make a note proposal permanent.

    `GitWriteError` is in `durable/publish.py`'s `non_retryable_error_types`, so classifying a
    push failure as auth *drops* the proposal rather than backing off. GitHub answers a bare
    `The requested URL returned error: 403` for secondary rate limits and abuse detection — both
    of which clear on their own — so treating the code as a credential fact would let a throttle
    silently stop the PR-gate while every run reported success.

    A genuine denial is not lost by this: it carries a phrase too (the test above), and the marker
    list is documented as wrong in the safe direction — a missed phrase retries, a false positive
    is permanent.
    """
    assert not _is_auth_failure(
        "fatal: unable to access 'https://h/': The requested URL returned error: 403"
    )
    assert not _is_auth_failure(
        "remote: You have exceeded a secondary rate limit.\n"
        "fatal: unable to access 'https://h/': The requested URL returned error: 403"
    )
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
