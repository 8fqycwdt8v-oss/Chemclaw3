"""The scheduled comparison: its activity reads both stores and publishes, its workflow returns it.

The activity runs against a real notes directory and real Postgres. The workflow runs in a real
Temporal dev server under its sandbox (skipped where the server cannot be fetched), which is where
the gauge's module-level state would be rebound to an empty copy if it lived in the workflow module.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.core.db import pooling
from chemclaw.core.metrics import METRICS
from chemclaw.durable.kg_compare import KgCompareWorkflow, compare_knowledge_stores
from chemclaw.kg.note import Note
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.store import NoteUnit
from tests.kg_repo import write_notes
from tests.note_store_harness import empty_note_tables
from tests.temporal_env import pydantic_client, start_local_env_or_skip


def _note(note_id: str, body: str = "A note.") -> Note:
    return Note(id=note_id, type="playbook", created_by="agent", body=body)


@pytest.fixture
async def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    """`kg_store=dual` over a notes directory and empty tables; the settle pause is off."""
    await empty_note_tables()
    notes_dir = tmp_path / "knowledge"
    notes_dir.mkdir()
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "kg_store", "dual")
    monkeypatch.setattr(settings, "kg_compare_settle_seconds", 0.0)
    async with pooling():
        yield notes_dir


async def _seed(notes_dir: Path) -> None:
    """Two notes in both stores, one only in the directory, one differing."""
    both, edited, tree_only = _note("both"), _note("edited", "Tree."), _note("tree-only")
    write_notes(notes_dir, both, edited, tree_only)
    store = PostgresNoteStore()
    await store.put(NoteUnit(both))
    await store.put(NoteUnit(_note("edited", "Postgres.")))


async def test_the_activity_reports_what_differs_and_publishes_it(deployment: Path) -> None:
    await _seed(deployment)
    report = await compare_knowledge_stores()
    assert report.clean is False
    assert (report.git_notes, report.postgres_notes) == (3, 2)
    assert report.counts["missing_in_postgres"] == 1 and report.counts["canonical"] == 1
    assert report.differing == ["edited", "tree-only"]
    text = METRICS.render()
    assert 'chemclaw_kg_compare_differences{kind="missing_in_postgres"} 1' in text
    assert 'chemclaw_kg_compare_differences{kind="canonical"} 1' in text


async def test_the_activity_reports_a_clean_day(deployment: Path) -> None:
    both = _note("both")
    write_notes(deployment, both)
    await PostgresNoteStore().put(NoteUnit(both))
    report = await compare_knowledge_stores()
    assert report.clean and report.differing == []
    assert 'chemclaw_kg_compare_differences{kind="canonical"} 0' in METRICS.render()


async def test_the_workflow_runs_the_comparison_under_the_sandbox(deployment: Path) -> None:
    """A difference is a result of the run, not a failure of it."""
    await _seed(deployment)
    async with await start_local_env_or_skip() as env:
        client = pydantic_client(env)
        async with Worker(
            client,
            task_queue="kg-compare-test",
            workflows=[KgCompareWorkflow],
            activities=[compare_knowledge_stores],
        ):
            report = await client.execute_workflow(
                KgCompareWorkflow.run, id="kg-compare-test", task_queue="kg-compare-test"
            )
    assert report.clean is False and report.counts["canonical"] == 1
    assert 'chemclaw_kg_compare_differences{kind="canonical"} 1' in METRICS.render()
