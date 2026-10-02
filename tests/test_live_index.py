"""The live lane's index step (`cli/live_index.py`), against a real broker and a real database.

On the four-repo lane `substrate_precedent` reported 0 of 4,282 reactions labelled and
`similar_reactions` a partial index, on every run and across `down`/`up` (#520): nothing ran the
label drain, and nothing carried or disposed of a previous lane's fingerprint generation. These
drive the step's two halves where they act — `ReactionLabelWorkflow` on a Temporal server, the
disposal on a Postgres table — because a stub of either would be blind to the property each half
exists for: a second bring-up must rejoin a running drain rather than race it, and a disposal must
never drop a row the current generation does not hold.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.cli import live_index
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.label_sync import LabelSyncPlan, ReactionLabelWorkflow
from chemclaw.ingest.labels.enrich import LabelReport
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.molfp.search import find_similar_molecules, record_for
from chemclaw.science.fingerprints.rekey import rebuild_molecule, rekey_fingerprints
from chemclaw.science.fingerprints.store import FingerprintRecord, PostgresFingerprintStore
from tests.pg import migrated_db_or_skip
from tests.temporal_env import pydantic_client, start_local_env_or_skip

# ------------------------------------------------------------------------ the label drain


class _Labeller:
    """The drain's two activities, standing in for the index and the `rxnlabel` server.

    Registered under the real activities' names, so the workflow under test is the shipped one.
    `release` holds the first batch until the test lets it go — the state in which a second
    bring-up arrives while the first drain is still running.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.batches = 0

    def activities(self) -> list[Any]:
        @activity.defn(name="plan_label_sync")
        async def plan() -> LabelSyncPlan:
            return LabelSyncPlan(version="rxnlabel@test", max_iterations=10)

        @activity.defn(name="label_stale_reactions")
        async def label(version: str) -> LabelReport:
            await self.release.wait()
            self.batches += 1
            return LabelReport(labelled=7, unlabelled=1, has_more=False)

        return [plan, label]


async def _drain_env() -> AsyncIterator[tuple[Client, _Labeller]]:
    env = await start_local_env_or_skip()
    async with env:
        client = pydantic_client(env)
        labeller = _Labeller()
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[ReactionLabelWorkflow],
            activities=labeller.activities(),
        ):
            yield client, labeller


async def test_a_second_bring_up_rejoins_the_running_drain_and_a_later_one_starts_afresh() -> None:
    """One drain per stale set: rejoined while it runs, started again once it has finished.

    Two drains over one stale set would contend on the same rows and label nothing the first would
    not — `live_data.backfill`'s argument, and the reason the id is fixed. And a finished drain must
    not block the next bring-up from labelling rows that have gone stale since.
    """
    async for client, labeller in _drain_env():
        first = await live_index.start_label_drain(client)
        second = await live_index.start_label_drain(client)
        assert second.id == first.id == live_index.LABEL_DRAIN_ID
        assert (await second.describe()).run_id == first.result_run_id, "a second drain started"

        waiting = await live_index.finish_label_drain(second, 0.5)
        assert "still draining at the deadline" in waiting

        labeller.release.set()
        done = await live_index.finish_label_drain(second, 30)
        assert done == (
            f"{live_index.LABEL_DRAIN_ID}: labelled 7 reaction(s), 1 stamped with nothing derived"
        )
        assert labeller.batches == 1

        again = await live_index.start_label_drain(client)
        assert again.result_run_id != first.result_run_id, "a finished drain blocked the next one"
        assert "labelled 7" in await live_index.finish_label_drain(again, 30)


async def test_a_failed_re_key_does_not_cost_the_label_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two indexes are independent, so one step's failure is reported beside the other's result.

    The run is marked failed — `up.sh` turns that into its warning — and the drain's outcome is
    still read, because it is half the fix and the half the precedent tools need.
    """

    async def broken_rekey(*, apply: bool) -> Any:
        raise RuntimeError("note_repo_dir is in use by another process")

    monkeypatch.setattr(live_index, "_rekey", broken_rekey)
    async for client, labeller in _drain_env():
        labeller.release.set()
        run = await live_index.bring_current(30, client=client)
    assert run.failed
    assert run.lines[0] == (
        "Fingerprints: FAILED — RuntimeError: note_repo_dir is in use by another process"
    )
    assert run.lines[1].startswith(f"Labels: {live_index.LABEL_DRAIN_ID}: labelled 7")


# ------------------------------------------------------------------------ the shelved generation


_SCRATCH = "live_index_dispose_probe"


async def _scratch_store() -> AsyncIterator[PostgresFingerprintStore]:
    """A molecule-shaped table of this test's own, so "nothing superseded" is assertable.

    The shared table is written by every other test, so its superseded population is nobody's to
    assert on — the reason `tests/test_molfp_postgres.py` uses the same shape.
    """
    await migrated_db_or_skip()
    async with await db.connect(settings.postgres_dsn) as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {_SCRATCH}")
        await conn.execute(
            f"CREATE TABLE {_SCRATCH} (id TEXT NOT NULL, label TEXT NOT NULL, "
            f"bits BIT({settings.ecfp_bits}) NOT NULL, definition TEXT NOT NULL, "
            "PRIMARY KEY (id, definition))"
        )
        await conn.commit()
    try:
        yield PostgresFingerprintStore(_SCRATCH, settings.ecfp_bits, molecule_definition())
    finally:
        async with await db.connect(settings.postgres_dsn) as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {_SCRATCH}")
            await conn.commit()


def _shelved(record: FingerprintRecord) -> FingerprintRecord:
    """`record` as a previous lane wrote it, under a definition this build no longer uses."""
    return record.model_copy(update={"definition": record.definition + "-previous-lane"})


async def _rows() -> list[tuple[str, str]]:
    async with await db.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT id, definition FROM {_SCRATCH} ORDER BY id, definition")
        return [(str(row[0]), str(row[1])) for row in await cur.fetchall()]


async def test_a_completed_rebuild_disposes_of_the_shelf_and_the_index_is_whole_again() -> None:
    """The lane's `index_partial: true` was a previous lane's generation, rebuilt and still shelved.

    After the re-key every shelved row has a current twin, so the superseded generation holds
    nothing the search does not — and until it is gone every search says PARTIAL, because the
    probe reads the table's definitions, not whether they were rebuilt.
    """
    async for store in _scratch_store():
        for smiles in ("CCO", "CCCO"):
            await store.add(_shelved(record_for(smiles, smiles)))
        assert (await find_similar_molecules(store, "CCO")).index_partial is True

        counts = await rekey_fingerprints(
            store, molecule_definition(), rebuild_molecule, apply=True
        )
        assert counts.rekeyed == 2 and counts.unreadable == 0
        assert (await find_similar_molecules(store, "CCO")).index_partial is True, (
            "the re-key alone cleared the flag, so this test no longer shows why disposal exists"
        )

        said = await live_index.settle_index("molecules", _SCRATCH, molecule_definition(), counts)
        assert said == (
            "molecules: 2 re-fingerprinted, 0 already current, 2 superseded row(s) disposed of"
        )
        search = await find_similar_molecules(store, "CCO")
        assert search.index_partial is False
        assert search.hits and search.hits[0].smiles == "CCO"
        assert {definition for _, definition in await _rows()} == {molecule_definition()}

        # Idempotent: a second bring-up finds everything current and disposes of nothing.
        again = await rekey_fingerprints(store, molecule_definition(), rebuild_molecule, apply=True)
        assert again.rekeyed == 0
        said = await live_index.settle_index("molecules", _SCRATCH, molecule_definition(), again)
        assert said.endswith("0 superseded row(s) disposed of")


async def test_a_row_the_rebuild_could_not_read_keeps_the_shelf_and_the_search_honest() -> None:
    """Disposal after an incomplete rebuild would delete the only copy of a row, silently.

    A shelved row whose label no longer parses has no current twin, so the search that says PARTIAL
    is telling the truth about it. Nothing is deleted, and the line says what to do instead.
    """
    async for store in _scratch_store():
        await store.add(_shelved(record_for("CCO", "CCO")))
        unreadable = _shelved(record_for("CCCO", "CCCO")).model_copy(
            update={"id": "garbled", "label": "not a smiles ((("}
        )
        await store.add(unreadable)

        counts = await rekey_fingerprints(
            store, molecule_definition(), rebuild_molecule, apply=True
        )
        assert counts.unreadable == 1
        before = await _rows()

        said = await live_index.settle_index("molecules", _SCRATCH, molecule_definition(), counts)
        assert "1 shelved row(s) could not be rebuilt" in said
        assert await _rows() == before, "a disposal ran over an incomplete rebuild"
        assert (await find_similar_molecules(store, "CCO")).index_partial is True


async def test_the_disposal_refuses_a_table_name_it_would_have_to_trust() -> None:
    """The statement interpolates the table, so only a plain identifier may reach it."""
    with pytest.raises(ValueError, match="plain SQL identifier"):
        await live_index.dispose_superseded("reaction_fingerprints; DROP TABLE x", "d")
