"""The commitment mirror: a unit of committed work, and the join only this system can make.

Asserted: the mirror converges on the source's snapshot rather than accumulating, reports its own
staleness, never infers a field the export did not state, and has no write path back.
"""

import asyncio
import dataclasses
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from chemclaw.agent.commitment_tools import review_commitments
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import commitment_sync
from chemclaw.ingest.commitments.json_export import json_commitment_export
from chemclaw.ingest.commitments.models import Commitment
from chemclaw.ingest.commitments.store import mirror_freshness, outstanding, record_commitments
from chemclaw.ingest.sources.base import SourceSpec
from chemclaw.ingest.sources.manifest import DataSourceManifest
from chemclaw.ingest.sources.registry import _build_half
from tests.pg import migrated_db_or_skip

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
SOURCE = "commitments-test"


async def _no_cursor(_key: str) -> datetime | None:
    """Stands in for the cursor load: this file's passes always read the whole export."""
    return None


async def _record_cursor(_key: str, _cursor: datetime) -> None:
    """Stands in for the cursor store, which needs no assertion here."""


async def _clean() -> None:
    """Remove this file's rows so a re-run starts from the same place."""
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM commitments WHERE source = %s", (SOURCE,))
        await conn.commit()


def _commitment(external_id: str, **kwargs: object) -> Commitment:
    """One mirrored row with this file's source."""
    return Commitment(
        source=SOURCE,
        external_id=external_id,
        title=kwargs.pop("title", "deliver the tox batch"),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


async def test_re_reading_a_snapshot_converges_rather_than_accumulating() -> None:
    """Re-reading a snapshot converges rather than accumulating.

    The upsert is keyed on `(source, external_id)`, so a full re-read of a snapshot export is free.
    """
    await migrated_db_or_skip()
    await _clean()
    await record_commitments([_commitment("M-1", due_at=datetime.now(UTC))])
    await record_commitments([_commitment("M-1", due_at=datetime.now(UTC), state="blocked")])

    rows = (await outstanding(source=SOURCE)).commitments
    assert [(row.external_id, row.state) for row in rows] == [("M-1", "blocked")]


async def test_a_snapshot_source_converges_downward_when_a_commitment_is_withdrawn() -> None:
    """A snapshot source converges downward when a commitment is withdrawn.

    A snapshot says "withdrawn" by omitting the row, which an upsert cannot express, and a stale row
    would travel under the refreshed rows' `observed_at`. Mark-and-sweep: rows not restated since
    the pass's mark are removed, only for adapters that declare themselves snapshots.
    """
    await migrated_db_or_skip()
    await _clean()
    await record_commitments([_commitment("MS-1"), _commitment("MS-2")])
    # The mark: everything the next pass writes lands strictly after this.
    marked_at = datetime.now(UTC)
    await asyncio.sleep(0.01)
    # The next snapshot: MS-2 was withdrawn, so the source simply stops exporting it.
    await record_commitments([_commitment("MS-1")])
    swept = await commitment_sync.sweep_withdrawn(SOURCE, marked_at)

    rows = (await outstanding(source=SOURCE)).commitments
    assert [row.external_id for row in rows] == ["MS-1"], (
        "a withdrawn commitment is still outstanding, in a list whose reported freshness comes "
        "from the rows that *were* refreshed — so it reads as current work nobody is doing"
    )
    assert swept == 1, f"the sweep reported {swept} rows removed"

    # And a pass in which nothing was withdrawn removes nothing.
    marked_at = datetime.now(UTC)
    await asyncio.sleep(0.01)
    await record_commitments([_commitment("MS-1")])
    assert await commitment_sync.sweep_withdrawn(SOURCE, marked_at) == 0


class _Export:
    """A portfolio export that answers with whatever it is holding."""

    def __init__(self, rows: list[Commitment], *, snapshot: bool) -> None:
        self._rows = rows
        self.snapshot = snapshot

    async def fetch_commitments(self, since: datetime | None) -> list[Commitment]:
        return list(self._rows)


async def _mirror_pass(
    export: _Export, *, broker_clock: datetime | None = None
) -> commitment_sync.CommitmentSyncResult:
    """Run one `mirror_commitments_activity` over `export`, with the broker's clock as given.

    `broker_clock` is the Temporal server's `started_time`; it is a parameter so a test can skew it
    and show the mark is not read from it.
    """
    from temporalio.testing import ActivityEnvironment

    with (
        mock.patch.object(
            commitment_sync, "make_data_source", lambda _name: SimpleNamespace(commitments=export)
        ),
        mock.patch.object(commitment_sync, "load_cursor", _no_cursor),
        mock.patch.object(commitment_sync, "store_cursor", _record_cursor),
    ):
        env = ActivityEnvironment()
        if broker_clock is not None:
            env.info = dataclasses.replace(env.info, started_time=broker_clock)
        result = await env.run(commitment_sync.mirror_commitments_activity, SOURCE)
    return result


# How far a worker's "now" may differ from the mirror database's; far larger than one pass, since
# any skew longer than a fetch would trigger the failure.
_BROKER_CLOCK_SKEW = timedelta(minutes=5)


async def test_the_pass_marks_from_the_database_that_stamps_the_rows_not_from_the_broker() -> None:
    """The pass marks from the database that stamps the rows, not from the broker.

    `observed_at` is Postgres `now()`, so the mark must be too: a leading broker clock sweeps every
    row just mirrored, a lagging one never sweeps. Both directions asserted; the skew is handed in
    to show the broker's clock does not reach the outcome at all.
    """
    await migrated_db_or_skip()

    # A pass whose export restates everything: nothing may be swept, however far ahead the
    # broker's clock runs.
    await _clean()
    await record_commitments([_commitment("MS-1"), _commitment("MS-2")])
    ahead = await _mirror_pass(
        _Export([_commitment("MS-1"), _commitment("MS-2")], snapshot=True),
        broker_clock=datetime.now(UTC) + _BROKER_CLOCK_SKEW,
    )
    rows = (await outstanding(source=SOURCE)).commitments
    assert ahead.withdrawn == 0, (
        f"the pass swept {ahead.withdrawn} of the rows it had just written: the mark came from "
        "a clock that leads the one stamping `observed_at`"
    )
    assert {row.external_id for row in rows} == {"MS-1", "MS-2"}, (
        "a mirror the pass had just restated in full came back as "
        f"{sorted(row.external_id for row in rows)} — the sweep deleted this pass's own work"
    )

    # And a pass whose export drops MS-2 must still remove it, however far *behind* the
    # broker's clock runs.
    behind = await _mirror_pass(
        _Export([_commitment("MS-1")], snapshot=True),
        broker_clock=datetime.now(UTC) - _BROKER_CLOCK_SKEW,
    )
    rows = (await outstanding(source=SOURCE)).commitments
    assert behind.withdrawn == 1, (
        f"the pass swept {behind.withdrawn} rows: a mark from a lagging clock is older than "
        "every row already in the mirror, so a withdrawn commitment is never removed"
    )
    assert {row.external_id for row in rows} == {"MS-1"}


async def test_an_export_that_answers_with_nothing_does_not_empty_the_mirror() -> None:
    """An export that answers with nothing does not empty the mirror.

    Zero rows may be an empty programme or a broken export; deleting the mirror is unrecoverable
    while keeping a row too long self-corrects. Driven through the activity, because the guard is at
    the call site, not in `sweep_withdrawn`.
    """
    await migrated_db_or_skip()
    await _clean()
    await record_commitments([_commitment("MS-1"), _commitment("MS-2")])
    empty = await _mirror_pass(_Export([], snapshot=True))
    rows = (await outstanding(source=SOURCE)).commitments
    assert empty.withdrawn == 0, (
        f"an export that returned nothing swept {empty.withdrawn} rows; a broken export and a "
        "finished programme look identical from here, and only one of them is recoverable"
    )
    assert {row.external_id for row in rows} == {"MS-1", "MS-2"}, (
        "a snapshot source's whole mirror was deleted by a pass that read nothing"
    )
    # And the source that genuinely empties still converges — one pass later, on its first
    # remaining row. That is the price of the guard, stated rather than assumed.
    remaining = await _mirror_pass(_Export([_commitment("MS-1")], snapshot=True))
    rows = (await outstanding(source=SOURCE)).commitments
    assert remaining.withdrawn == 1
    assert {row.external_id for row in rows} == {"MS-1"}


def test_the_pass_sweeps_only_where_the_adapter_promises_a_whole_picture() -> None:
    """The pass sweeps only where the adapter promises a whole picture.

    Both adapters return the same list; only the `snapshot` one has absent rows removed. Driven
    through the activity because the property is the wiring. No `started_time` is supplied: the mark
    is the database's.
    """

    async def _run() -> None:
        await migrated_db_or_skip()

        async def _pass(*, snapshot: bool) -> commitment_sync.CommitmentSyncResult:
            """One mirror pass over a source now exporting MS-1 alone."""
            await _clean()
            await record_commitments([_commitment("MS-1"), _commitment("MS-2")])
            return await _mirror_pass(_Export([_commitment("MS-1")], snapshot=snapshot))

        incremental = await _pass(snapshot=False)
        rows = (await outstanding(source=SOURCE)).commitments
        assert incremental.withdrawn == 0
        assert {row.external_id for row in rows} == {"MS-1", "MS-2"}, (
            "an incremental source's unmentioned row was deleted; for that source an absent row "
            "means unchanged, so this empties the mirror on the first quiet pass"
        )

        snapshotted = await _pass(snapshot=True)
        rows = (await outstanding(source=SOURCE)).commitments
        assert snapshotted.withdrawn == 1
        assert {row.external_id for row in rows} == {"MS-1"}, (
            "a snapshot source withdrew MS-2 and the mirror still carries it"
        )

    asyncio.run(_run())


def test_the_shipped_adapter_takes_its_completeness_promise_from_the_manifest() -> None:
    """The shipped adapter takes its `snapshot` promise from the manifest.

    Completeness is a property of one site's export, so it is a manifest key passed through
    `factory(**manifest.config)` (D-120), not a process-wide setting. Both directions: a manifest
    that says nothing keeps the non-sweeping default.
    """
    default = json_commitment_export(name="probe")
    assert default.snapshot is False, (
        "the shipped manifest declares no `config:`, so an unset key must mean no sweep — "
        "otherwise this change deletes rows on deployments that never asked for it"
    )

    manifest = DataSourceManifest(
        name="probe",
        description="A probe source whose export tool writes the whole directory atomically.",
        commitments="chemclaw.ingest.commitments.json_export:json_commitment_export",
        config={"snapshot": True, "path": "/nonexistent-by-design"},
    )
    built = _build_half(manifest, manifest.commitments or "", name=manifest.name)
    assert built.snapshot is True, (
        "a `snapshot: true` in the manifest did not reach the adapter, so a site cannot state that "
        "its export is complete and `sweep_withdrawn` stays unreachable for the shipped adapter"
    )


async def test_the_reading_reports_when_the_mirror_was_last_refreshed() -> None:
    """The reading reports when the mirror was last refreshed.

    A mirror's characteristic failure is staleness, so freshness is a field on every answer.
    """
    await migrated_db_or_skip()
    await _clean()
    before = datetime.now(UTC)
    await record_commitments([_commitment("M-2", due_at=before + timedelta(days=7))])

    _page = await outstanding(source=SOURCE)
    rows, freshness = _page.commitments, _page.mirrored_at
    assert rows and freshness is not None and freshness >= before
    assert await mirror_freshness(SOURCE) is not None


async def test_nothing_outstanding_and_nothing_ever_mirrored_are_different_answers() -> None:
    """An empty list has two meanings, and only the freshness separates them.

    Conflating them is how a manager reads "nothing is late" out of a sync that never ran.
    """
    await migrated_db_or_skip()
    await _clean()
    _page = await outstanding(source=SOURCE)
    rows, freshness = _page.commitments, _page.mirrored_at
    assert rows == []
    assert freshness is None
    assert await mirror_freshness(SOURCE) is None

    # Now one, and delivered: still nothing outstanding, but the mirror *has* run.
    await record_commitments([_commitment("M-3", state="done")])
    rows = (await outstanding(source=SOURCE)).commitments
    assert rows == []
    assert await mirror_freshness(SOURCE) is not None


async def test_outstanding_is_ordered_by_deadline_with_undated_work_last() -> None:
    """A commitment with no date is not the most urgent one.

    Which is what a plain `ORDER BY due_at` makes it under Postgres' NULL ordering, and is an easy
    thing to get backwards in the direction that puts undated work at the top of a manager's list.
    """
    await migrated_db_or_skip()
    await _clean()
    now = datetime.now(UTC)
    await record_commitments(
        [
            _commitment("M-late", due_at=now + timedelta(days=30)),
            _commitment("M-undated"),
            _commitment("M-soon", due_at=now + timedelta(days=1)),
        ]
    )
    rows = (await outstanding(source=SOURCE)).commitments
    assert [row.external_id for row in rows] == ["M-soon", "M-late", "M-undated"]


async def test_the_link_to_the_science_is_what_the_mirror_is_for() -> None:
    """A commitment with no link is a row the portfolio tool already holds and holds better."""
    await migrated_db_or_skip()
    await _clean()
    await record_commitments(
        [
            _commitment("M-linked", note_ids=["note-1"], compounds=["CCO"]),
            _commitment("M-bare"),
        ]
    )
    rows = (await outstanding(source=SOURCE)).commitments
    linked = {row.external_id: row.links_to_science for row in rows}
    assert linked == {"M-linked": True, "M-bare": False}


def test_the_json_export_rejects_a_bad_row_and_keeps_the_rest(tmp_path: Path) -> None:
    """Reject-and-continue: one malformed row in a thousand must not cost the other 999.

    And nothing is repaired — a row with no `title` is dropped rather than given one, because a
    mirror that invented a field would be asserting a plan.
    """
    export = tmp_path / "export.json"
    export.write_text(
        json.dumps(
            [
                {"external_id": "A", "title": "run the stability pull", "kind": "milestone"},
                {"external_id": "B"},
                {"external_id": "C", "title": "file the section", "state": "not-a-state"},
                {"external_id": "D", "title": "ship the batch"},
            ]
        ),
        encoding="utf-8",
    )
    adapter = json_commitment_export(name=SOURCE, path=str(export))
    found = asyncio.run(adapter.fetch_commitments(None))
    assert [row.external_id for row in found] == ["A", "D"]
    assert found[0].kind == "milestone"


def test_a_source_may_declare_the_commitments_half_alone() -> None:
    """The third half stands on its own — a portfolio export carries no corpus and no evidence."""
    spec = SourceSpec(name="x", commitments=json_commitment_export(name="x", path="/tmp"))
    assert spec.ingest is None and spec.retrieve is None and spec.commitments is not None

    manifest = DataSourceManifest(
        name="commitments-json",
        description="a portfolio export",
        commitments="chemclaw.ingest.commitments.json_export:json_commitment_export",
        config={"path": "data/commitments"},
    )
    assert manifest.commitments and manifest.ingest is None

    # And a source declaring no half at all is still refused, in both places that can be reached.
    with pytest.raises(ValueError, match="no `ingest:`, `retrieve:` or `commitments:` half"):
        DataSourceManifest(name="empty", description="nothing")
    with pytest.raises(ValueError, match="ingest, retrieve or commitments half"):
        SourceSpec(name="empty")


def test_the_mirror_has_no_write_path_back() -> None:
    """The mirror has no write path back.

    A source cannot acquire a write path; moving a milestone belongs to the system that owns it.
    """
    protocol = (SRC / "ingest" / "commitments" / "adapter.py").read_text(encoding="utf-8")
    for verb in ("def update", "def push", "def write", "def create"):
        assert verb not in protocol
    tools = (SRC / "agent" / "commitment_tools.py").read_text(encoding="utf-8")
    assert "record_commitments" not in tools, (
        "the agent tool reaches the mirror's writer. Reading the mirror is a tool; changing a "
        "programme's plan is not one."
    )


def test_one_unreadable_file_costs_that_file_and_not_the_pass(tmp_path: Path) -> None:
    """One unreadable file costs that file and not the pass.

    Otherwise the cursor never advances and the mirror freezes on an old snapshot.
    """
    (tmp_path / "a-good.json").write_text(
        json.dumps([{"external_id": "M-1", "title": "one", "kind": "milestone"}]), encoding="utf-8"
    )
    (tmp_path / "b-truncated.json").write_text('[{"external_id": "M-2",', encoding="utf-8")
    (tmp_path / "c-null.json").write_text("null", encoding="utf-8")
    (tmp_path / "d-good.json").write_text(
        json.dumps([{"external_id": "M-3", "title": "three", "kind": "milestone"}]),
        encoding="utf-8",
    )

    export = json_commitment_export(name="probe", path=str(tmp_path))
    found = asyncio.run(export.fetch_commitments(None))
    assert sorted(row.external_id for row in found) == ["M-1", "M-3"], (
        "a malformed file cost the files sorting after it, which freezes the whole mirror"
    )


def test_a_missing_export_directory_is_reported_rather_than_read_as_an_empty_portfolio(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing export directory is reported rather than read as an empty portfolio.

    A mistyped `CHEMCLAW_COMMITMENT_EXPORT_DIR` would otherwise look like a truthful empty
    portfolio. It is counted on `chemclaw_degraded_total{subsystem="commitment_mirror"}`, which is
    alerted, not only logged.
    """
    from chemclaw.core.metrics import METRICS

    series = 'chemclaw_degraded_total{subsystem="commitment_mirror"}'

    def _count() -> float:
        for line in METRICS.render().splitlines():
            if line.startswith(series):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    before = _count()
    missing = tmp_path / "not-mounted"
    export = json_commitment_export(name="probe", path=str(missing))
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(export.fetch_commitments(None)) == []
    assert any("export_dir_missing" in record.message for record in caplog.records), (
        "a wrong export directory is still indistinguishable from an empty one"
    )
    assert _count() == before + 1, (
        "the mirror reading nothing left no counter, so a mistyped export directory is invisible "
        "to everything except a log nobody is reading"
    )


def _degraded_count(subsystem: str) -> float:
    """The `chemclaw_degraded_total` series for one subsystem, or 0 before it exists."""
    from chemclaw.core.metrics import METRICS

    series = f'chemclaw_degraded_total{{subsystem="{subsystem}"}}'
    for line in METRICS.render().splitlines():
        if line.startswith(series):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_an_export_directory_that_exists_and_holds_nothing_is_reported_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An export directory that exists and holds no `*.json` is reported too.

    Same `commitment_mirror` subsystem as the missing path: both mean the pointer to the export is
    wrong, one alert and one operator action.
    """
    before = _degraded_count("commitment_mirror")
    empty = tmp_path / "mounted-but-empty"
    empty.mkdir()
    (empty / "commitments.jsonl").write_text('{"external_id": "M-1"}\n', encoding="utf-8")

    export = json_commitment_export(name="probe", path=str(empty))
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(export.fetch_commitments(None)) == []
    assert any("export_empty" in record.message for record in caplog.records), caplog.records
    assert _degraded_count("commitment_mirror") == before + 1, (
        "an export directory that exists and holds nothing this reads left no counter, so the "
        "wrong-subdirectory case is exactly as invisible as the wrong-path case used to be"
    )


def test_unreadable_files_and_rejected_rows_are_counted_not_only_logged(
    tmp_path: Path,
) -> None:
    """Unreadable files and rejected rows are counted, not only logged.

    They need a different operator action from a wrong export path, so they get a different
    subsystem.
    """
    before_export = _degraded_count("commitment_export")
    before_mirror = _degraded_count("commitment_mirror")

    root = tmp_path / "export"
    root.mkdir()
    (root / "a.json").write_text("{ this is not json", encoding="utf-8")
    (root / "b.json").write_text('[{"no_external_id_at_all": true}]', encoding="utf-8")

    export = json_commitment_export(name="probe", path=str(root))
    assert asyncio.run(export.fetch_commitments(None)) == []

    assert _degraded_count("commitment_export") == before_export + 2, (
        "an unreadable file and a rejected row left no counter; from outside, an export that "
        "parsed to nothing is indistinguishable from an export with nothing in it"
    )
    assert _degraded_count("commitment_mirror") == before_mirror, (
        "a content fault was counted as a pointer fault, which sends an operator to the knob "
        "instead of to the export"
    )


def test_the_commitment_cursor_does_not_share_a_row_with_the_eln_sync() -> None:
    """The commitment cursor does not share a row with the ELN sync.

    `sync_cursors` is keyed on source name, and one manifest may declare both `ingest:` and
    `commitments:`; a shared row would make the ELN sync skip unread entries.
    """
    source = inspect.getsource(commitment_sync)
    assert 'f"{source}:commitments"' in source, (
        "the commitment mirror writes the bare source name again, so it shares the ELN sync's row"
    )
    assert "load_cursor(source)" not in source and "store_cursor(source," not in source


def test_cancelling_a_mirror_stops_it_instead_of_skipping_the_source_in_flight() -> None:
    """Cancelling a mirror stops it instead of skipping the source in flight.

    A workflow cancel reaches `execute_activity` as an `ActivityError` caused by `CancelledError`,
    which the per-source catch must re-raise. Driven on the real-time server so the cancel arrives
    during the activity. Asserted on the run status and on the source after the cancelled one.
    """
    import contextlib
    from typing import Any

    from temporalio import activity
    from temporalio.client import Client, WorkflowFailureError
    from temporalio.worker import Worker

    from chemclaw.durable.commitment_sync import CommitmentSyncResult, CommitmentSyncWorkflow
    from tests.temporal_env import pydantic_client, start_local_env_or_skip

    queue = "test-commitment-cancel"
    mirrored: list[str] = []
    in_flight = asyncio.Event()

    @activity.defn(name="list_commitment_sources_activity")
    async def three_sources() -> list[str]:
        return ["src-a", "src-b", "src-c"]

    @activity.defn(name="mirror_commitments_activity")
    async def mirror(source: str) -> CommitmentSyncResult:
        """`src-b` hangs so the cancel lands during it; `src-c` is the one that must not run."""
        if source == "src-b":
            in_flight.set()
            while True:
                await asyncio.sleep(0.05)
        mirrored.append(source)
        return CommitmentSyncResult(source=source, mirrored=1)

    async def _run() -> Any:
        async with await start_local_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=queue,
                workflows=[CommitmentSyncWorkflow],
                activities=[three_sources, mirror],
            ):
                handle = await client.start_workflow(
                    CommitmentSyncWorkflow.run, id="commitment-sync-cancelled", task_queue=queue
                )
                await asyncio.wait_for(in_flight.wait(), timeout=60)
                await handle.cancel()
                with contextlib.suppress(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), timeout=60)
                return (await handle.describe()).status

    status = asyncio.run(_run())

    assert status.name == "CANCELED", (
        f"the cancelled mirror ended {status!r}; a cancel absorbed by the per-source skip makes "
        "this run unstoppable — it books the source in flight as a failed export and mirrors the "
        "rest"
    )
    assert "src-c" not in mirrored, (
        "the source after the cancelled one was mirrored anyway: the cancel skipped one source "
        "instead of stopping the run"
    )


async def test_a_page_of_the_portfolio_says_how_much_of_it_is_a_page() -> None:
    """A page of the portfolio says how much of it is a page.

    Otherwise a risk answer built over the soonest deadlines reads as the whole book.
    `limit_applied` exposes the 200-row clamp on `outstanding`.
    """
    await migrated_db_or_skip()
    await _clean()
    now = datetime.now(UTC)
    await record_commitments(
        [
            _commitment(f"M-page-{index:02d}", due_at=now + timedelta(days=index + 1))
            for index in range(40)
        ]
    )

    page = await outstanding(source=SOURCE, limit=25)
    assert len(page.commitments) == 25
    assert page.total_outstanding == 40
    assert page.limit_applied == 25
    assert page.truncated

    clamped = await outstanding(source=SOURCE, limit=1000)
    assert clamped.limit_applied == 200

    whole = await outstanding(source=SOURCE, limit=200)
    assert not whole.truncated


async def test_the_review_tool_says_which_of_its_two_silences_is_biting() -> None:
    """`review_commitments` says which of its two silences applies.

    Empty versus never-mirrored, and page versus population, are on the payload as a
    `computed_field`, so they survive `model_dump()`.
    """
    await migrated_db_or_skip()
    await _clean()
    now = datetime.now(UTC)
    await record_commitments(
        [
            _commitment(f"M-tool-{index:02d}", due_at=now + timedelta(days=index + 1))
            for index in range(40)
        ]
    )

    review = await review_commitments(source=SOURCE, limit=25)
    payload = review.model_dump()
    assert len(review.commitments) == 25
    assert review.total_outstanding == 40
    assert "PARTIAL" in payload["verdict"]
    assert "40" in payload["verdict"]


async def test_an_empty_portfolio_and_a_sync_that_never_ran_read_differently() -> None:
    """The distinction the docstring argued for, moved onto the payload where a model reads it."""
    await migrated_db_or_skip()
    await _clean()

    never = await review_commitments(source=SOURCE)
    assert never.mirrored_at is None
    assert "NEVER MIRRORED" in never.model_dump()["verdict"]

    await record_commitments([_commitment("M-done", state="done")])
    delivered = await review_commitments(source=SOURCE)
    assert delivered.commitments == []
    assert delivered.mirrored_at is not None
    assert "NOTHING OUTSTANDING" in delivered.model_dump()["verdict"]
