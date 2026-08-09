"""The durable memory-synthesis corpus reader honors the data-source config (DUP-1).

`chemclaw.durable.memory_jobs.all_reactions` is the corpus every memory job reasons over. After
the F7
seam it must read the *configured* active ingest sources (`settings.data_sources`), not a hardcoded
union of every ELN adapter — so toggling `CHEMCLAW_DATA_SOURCES` actually changes what memory sees,
the same guarantee the durable ELN sync already honors. Uses the committed sample exports that the
default config points at (`data/eln-exports` + `data/eln-exports/ord`); no server needed.
"""

import asyncio
from datetime import datetime

import pytest

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.durable import memory_jobs
from chemclaw.durable.memory_jobs import all_reactions
from chemclaw.ingest.eln.ord import Component, OrdReaction, Role
from chemclaw.ingest.sources.base import RawEntry


def testall_reactions_honors_data_sources_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding the ORD source to `data_sources` brings its reactions into the memory corpus."""
    # Default: only the free-text JSON ELN source is active.
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    json_only = asyncio.run(all_reactions())
    # Adding the native-ORD source to the config expands the corpus (config drives it, not code).
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    json_and_ord = asyncio.run(all_reactions())
    assert len(json_and_ord) > len(json_only)


def testall_reactions_empty_when_no_ingest_source_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only a retrieve-only source active, memory synthesis reads an empty corpus."""
    monkeypatch.setattr(settings, "data_sources", "graph")
    assert asyncio.run(all_reactions()) == []


class _PartialSource:
    """An ingest half whose second entry cannot be mapped — the degraded read, minimally.

    `map_to_ord` raising `ChemclawError` is the documented bad-data contract, and `read_corpus`
    skips such an entry and goes on. That skip is precisely what makes a pass non-authoritative.
    """

    def __init__(self, bad: int) -> None:
        """Return three entries, of which `bad` (by index) fails to map."""
        self._bad = bad

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Three raw entries, ids `0`..`2` — the timestamp filter is irrelevant here."""
        return [RawEntry(entry_id=str(i), payload={}, created_at=since) for i in range(3)]

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map every entry but the designated bad one, which raises the bad-data error."""
        if raw.entry_id == str(self._bad):
            raise ChemclawError(f"entry {raw.entry_id} is unmappable")
        return OrdReaction(
            reaction_id=raw.entry_id,
            inputs=[Component(smiles="CCO", role=Role.REACTANT)],
            outcomes=[Component(smiles="CC=O", role=Role.PRODUCT)],
            provenance=f"test:{raw.entry_id}",
        )


def test_a_corpus_read_that_skipped_an_entry_reports_itself_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signal `record(complete=…)` rests on, proved at the place it is produced.

    Nothing downstream can tell a shrunken corpus from a shrinking one, so the read has to say
    which it was. Without this the observation upsert would keep replacing evidence on a pass that
    saw part of the record — a partial reading written down as the complete one, which is the
    defect this lane is named for.
    """
    monkeypatch.setattr(memory_jobs, "active_ingest_sources", lambda: [_PartialSource(bad=1)])
    partial = asyncio.run(memory_jobs.read_corpus())
    assert len(partial.reactions) == 2 and partial.complete is False

    monkeypatch.setattr(memory_jobs, "active_ingest_sources", lambda: [_PartialSource(bad=9)])
    whole = asyncio.run(memory_jobs.read_corpus())
    assert len(whole.reactions) == 3 and whole.complete is True


def test_background_worker_registers_memory_fan_out() -> None:
    """The publish child + the three build activities are wired onto the background worker (F10-D2).

    Registration is easy to forget when a synthesis job's topology changes, and a missing child or
    activity only fails at runtime on the server (which CI's server tests skip offline), so pin it.
    """
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS
    from chemclaw.durable.memory_jobs import (
        PublishNoteWorkflow,
        build_campaign_notes_activity,
        build_optimization_notes_activity,
        build_playbook_notes_activity,
        publish_memory_note_activity,
    )

    assert PublishNoteWorkflow in BACKGROUND_WORKFLOWS  # the fan-out publish child
    for built in (
        build_campaign_notes_activity,
        build_playbook_notes_activity,
        build_optimization_notes_activity,
        publish_memory_note_activity,
    ):
        assert built in BACKGROUND_ACTIVITIES
