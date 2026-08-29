"""The commitment mirror: a unit of committed work, and the join only this system can make.

Nine of nineteen `manager` bucket-C probes needed one object the schema did not have. Seventy-three
migrations, and `project` was a nullable text tag on `reaction_records` — a facet on a row, not an
entity.

The properties asserted here are the ones that keep this a *mirror* rather than a second plan: it
converges on the source's snapshot rather than accumulating, it reports its own staleness, it never
infers a field the export did not state, and it has no write path back.
"""

import asyncio
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import commitment_sync
from chemclaw.ingest.commitments.json_export import json_commitment_export
from chemclaw.ingest.commitments.models import Commitment
from chemclaw.ingest.commitments.store import mirror_freshness, outstanding, record_commitments
from chemclaw.ingest.sources.base import SourceSpec
from chemclaw.ingest.sources.manifest import DataSourceManifest
from tests.pg import migrated_db_or_skip

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
SOURCE = "commitments-test"


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


def test_re_reading_a_snapshot_converges_rather_than_accumulating() -> None:
    """The upsert is keyed on `(source, external_id)`, which is what makes a full re-read free.

    A portfolio extract is a snapshot, not a change feed, so the sync re-reads it whole. If that
    duplicated, the mirror would grow a copy of the programme on every pass and every count over it
    would be wrong in a way no single row reveals.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await record_commitments([_commitment("M-1", due_at=datetime.now(UTC))])
        await record_commitments([_commitment("M-1", due_at=datetime.now(UTC), state="blocked")])

        rows, _freshness = await outstanding(source=SOURCE)
        assert [(row.external_id, row.state) for row in rows] == [("M-1", "blocked")]

    asyncio.run(_run())


def test_the_reading_reports_when_the_mirror_was_last_refreshed() -> None:
    """A mirror's characteristic failure is staleness, not error.

    The export stops running, the numbers keep answering, and a manager acts on last month's
    picture. So freshness is a field on the answer rather than something a reader has to think to
    ask for — the same argument `operations.Coverage` makes about a window.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        before = datetime.now(UTC)
        await record_commitments([_commitment("M-2", due_at=before + timedelta(days=7))])

        rows, freshness = await outstanding(source=SOURCE)
        assert rows and freshness is not None and freshness >= before
        assert await mirror_freshness(SOURCE) is not None

    asyncio.run(_run())


def test_nothing_outstanding_and_nothing_ever_mirrored_are_different_answers() -> None:
    """An empty list has two meanings, and only the freshness separates them.

    Conflating them is how a manager reads "nothing is late" out of a sync that never ran.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        rows, freshness = await outstanding(source=SOURCE)
        assert rows == []
        assert freshness is None
        assert await mirror_freshness(SOURCE) is None

        # Now one, and delivered: still nothing outstanding, but the mirror *has* run.
        await record_commitments([_commitment("M-3", state="done")])
        rows, _f = await outstanding(source=SOURCE)
        assert rows == []
        assert await mirror_freshness(SOURCE) is not None

    asyncio.run(_run())


def test_outstanding_is_ordered_by_deadline_with_undated_work_last() -> None:
    """A commitment with no date is not the most urgent one.

    Which is what a plain `ORDER BY due_at` makes it under Postgres' NULL ordering, and is an easy
    thing to get backwards in the direction that puts undated work at the top of a manager's list.
    """

    async def _run() -> None:
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
        rows, _f = await outstanding(source=SOURCE)
        assert [row.external_id for row in rows] == ["M-soon", "M-late", "M-undated"]

    asyncio.run(_run())


def test_the_link_to_the_science_is_what_the_mirror_is_for() -> None:
    """A commitment with no link is a row the portfolio tool already holds and holds better."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await record_commitments(
            [
                _commitment("M-linked", note_ids=["note-1"], compounds=["CCO"]),
                _commitment("M-bare"),
            ]
        )
        rows, _f = await outstanding(source=SOURCE)
        linked = {row.external_id: row.links_to_science for row in rows}
        assert linked == {"M-linked": True, "M-bare": False}

    asyncio.run(_run())


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
    """An absence pinned: mirroring a milestone in does not confer the ability to move one.

    `ingest/sources/README.md` states the rule for the two corpus halves — a source "cannot acquire
    a write path by declaring one" — and the third half inherits it. Moving a milestone belongs to
    the system that owns it, and would be an effect rather than a tool.
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
    """Reject-and-continue was written for a *row* and the file was left to raise.

    A truncated export — a partial write, a failed extract — aborted `fetch_commitments` before
    every file sorting after it was read, the activity failed, the cursor never advanced, and the
    mirror froze on last week's snapshot while `review_commitments` kept answering from it. One bad
    file must cost that file.
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
    """A wrong path and a genuinely empty portfolio were byte-identical, and one is a defect.

    The adapter found no files, the sync reported success with nothing mirrored, `mirror_freshness`
    stayed NULL, and `review_commitments` reads NULL as "nothing was ever mirrored" — so a mistyped
    `CHEMCLAW_COMMITMENT_EXPORT_DIR` reached a project leader as a truthful empty portfolio.
    Shipping a `data/commitments/` directory only fixes the default, and the knob exists precisely
    so a deployment can point elsewhere.
    """
    missing = tmp_path / "not-mounted"
    export = json_commitment_export(name="probe", path=str(missing))
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(export.fetch_commitments(None)) == []
    assert any("export_dir_missing" in record.message for record in caplog.records), (
        "a wrong export directory is still indistinguishable from an empty one"
    )


def test_the_commitment_cursor_does_not_share_a_row_with_the_eln_sync() -> None:
    """`sync_cursors` is keyed on the source name alone, and nothing forbids both halves.

    A manifest may declare `ingest:` and `commitments:` — the model requires *at least* one — and
    the mirror stores wall-clock now. The next ELN sync would load that and fetch only entries newer
    than it, silently skipping every unread entry: the exact failure `ingest/eln/cursor.py` argues
    cannot happen, under an assumption of one writer per source.
    """
    source = inspect.getsource(commitment_sync)
    assert 'f"{source}:commitments"' in source, (
        "the commitment mirror writes the bare source name again, so it shares the ELN sync's row"
    )
    assert "load_cursor(source)" not in source and "store_cursor(source," not in source
