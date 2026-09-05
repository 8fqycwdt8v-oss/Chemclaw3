"""The transcription tier: what removing the PR-gate from ELN ingest had to keep (D-2026-08-25).

Four claims, each of which the change would be wrong without:

1. Ingest performs **no git operation at all** — the property the whole change is for, asserted
   against a submitter that raises rather than by reading the diff.
2. A sync run's cost does not grow with the corpus. The old loop parsed every merged note per
   chunk, which is what made it outgrow `eln_sync_timeout_seconds` at ~700k entries.
3. The capability survives end to end: a structural hit still expands into its recipe, with no
   note file on disk and no git repository configured.
4. A campaign citing `[[reaction-<id>]]` still passes `kg-validate` now that reactions are rows,
   and a citation to a record that does not exist is still caught — by the half of the check that
   can see the store.

`tests/test_eln.py` covers the mapping and the sync loop's own bookkeeping; this file covers the
seam between the tier and everything that reads it.
"""

import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from chemclaw.agent.condense import Protocol
from chemclaw.agent.graph_tools import expand_note
from chemclaw.agent.protocol_tools import _from_record
from chemclaw.cli.validate_kg import main as _validate_kg_main
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.eln.ingest import ingest_reaction
from chemclaw.ingest.eln.json_adapter import JsonExportAdapter
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import (
    InMemoryReactionRecordStore,
    PostgresReactionRecordStore,
    ReactionRecord,
    default_record_store,
)
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.kg.note import Note, note_id_for_reaction
from chemclaw.kg.validate import external_citations, unresolved_citations, validate
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.pg import migrated_db_or_skip

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _entry(entry_id: str, created_at: datetime) -> RawEntry:
    """One well-formed free-text ELN export entry."""
    return RawEntry(
        entry_id=entry_id,
        created_at=created_at,
        payload={
            "id": entry_id,
            "timestamp": created_at.isoformat(),
            "reactants": [
                {"smiles": "CCO", "role": "reactant", "mass_mg": 460},
                {"smiles": "CC(=O)O", "role": "reactant", "mass_mg": 600},
            ],
            "products": [{"smiles": "CCOC(C)=O", "yield_percent": 85}],
            "procedure": "Ethanol and acetic acid were stirred at 80 °C for 3 h.",
            "operator": "chemist-a",
        },
    )


class _ListAdapter(JsonExportAdapter):
    """An adapter serving a fixed entry list, recording what it was asked for."""

    def __init__(self, entries: list[RawEntry]) -> None:
        """Serve `entries` regardless of the cursor."""
        super().__init__("/nonexistent")
        self._entries = entries

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return the fixed list."""
        return self._entries


class _ExplodingSubmitter:
    """A `NoteSubmitter` that fails if anything tries to open a PR.

    The assertion is the *absence* of a call, and an absence is only worth asserting if calling
    would be loud. A counter would pass just as well with the call removed for the wrong reason.
    """

    async def submit(self, submission: object) -> str:
        """Fail — ingest must never reach the PR-gate."""
        raise AssertionError(f"ELN ingest opened a pull request: {submission!r}")


def test_ingesting_a_reaction_opens_no_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the change, asserted where it would break rather than in the diff.

    Every path out of `ingest_reaction` to git is stubbed with something that raises, so a future
    edit re-introducing the gate fails here instead of quietly costing a reviewer 202 ms of
    serialized git per ELN entry and a human merge per experiment.
    """
    monkeypatch.setattr(
        "chemclaw.kg.git_submitter.default_writer", lambda: _ExplodingSubmitter()
    )

    async def _run() -> ReactionRecord:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        adapter = _ListAdapter([_entry("no-pr", datetime(2026, 3, 1, tzinfo=UTC))])
        reaction = adapter.map_to_ord(adapter._entries[0])
        return await ingest_reaction(
            reaction, rxn, mol, rec, label_index=InMemoryLabelIndex(), source="test-eln"
        )

    record = asyncio.run(_run())
    assert record.reaction_id == "no-pr"


def test_a_sync_run_does_not_read_the_corpus_it_is_not_replaying() -> None:
    """Cost is bounded by the page, not by how much has already been ingested.

    The old loop answered "is this entry unchanged?" by parsing every merged note on disk, once
    per chunk that touched a replay — 425 µs and 2.9 kB per note, linear — so at ~700k entries the
    lookup alone outlived the activity's 300 s start-to-close and the sync wedged permanently.

    Asserted by counting what the store is *asked* for rather than by timing, because a timing
    assertion on a small fixture proves nothing: the defect was the shape of the query, and the
    shape is what this pins. A replay asks for exactly the ids in the batch; a run with no replay
    at all asks for nothing.
    """
    asked: list[int] = []

    class _CountingStore(InMemoryReactionRecordStore):
        """Records how many ids each unchanged-entry lookup asked for."""

        async def bodies(self, reaction_ids: Sequence[str], source: str) -> dict[str, str]:
            """Count the request, then answer it."""
            asked.append(len(reaction_ids))
            return await super().bodies(reaction_ids, source)

    async def _run() -> None:
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        rxn, mol = InMemoryFingerprintStore(), InMemoryFingerprintStore()
        rec = _CountingStore()
        # A corpus far larger than the batch: none of it may be read.
        await rec.record(
            [
                ReactionRecord(reaction_id=f"old-{i}", body=f"body {i}", source="eln:test")
                for i in range(500)
            ],
            "test-eln",
        )
        replayed = _entry("replayed", cursor - datetime.resolution)
        await sync_entries(
            _ListAdapter([replayed]),
            rxn,
            mol,
            rec,
            cursor,
            label_index=InMemoryLabelIndex(),
            source="test-eln",
        )

    asyncio.run(_run())
    assert asked == [1], (
        f"the unchanged-entry lookup asked for {asked}; it must be keyed on the batch (1 id), "
        "never on the 500-record corpus — that is the growth this tier exists to remove"
    )


def test_the_unchanged_check_keys_on_the_record_id_not_the_entry_id() -> None:
    """A record id is not an entry id, and a source where they differ must still skip a replay.

    `RawEntry.entry_id` is whatever the source keys its rows on; `OrdReaction.reaction_id` is a
    separately declared field — in a warehouse binding, two different columns. Looking the store up
    by one and reading the answer by the other misses on every such source, and misses *silently*:
    the upsert is idempotent, so the run stays correct and simply re-ingests everything forever
    while `skipped_existing` reports nothing. This is the shape of that bug, so the fixture makes
    the two ids deliberately unequal.
    """

    class _RenamingAdapter(_ListAdapter):
        """An adapter whose reaction id is not its entry id — what a warehouse binding allows."""

        def map_to_ord(self, raw: RawEntry) -> OrdReaction:
            """Map as usual, then rename the reaction so it differs from the entry id."""
            reaction = super().map_to_ord(raw)
            return reaction.model_copy(update={"reaction_id": f"exp-{raw.entry_id}"})

    async def _run() -> tuple[list[str], list[str]]:
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        replayed = _entry("row-4711", cursor - datetime.resolution)
        adapter = _RenamingAdapter([replayed])
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        # An earlier run's record, stored under the *reaction* id.
        await rec.record([record_from_ord_reaction(adapter.map_to_ord(replayed))], "test-eln")
        summary = await sync_entries(
            adapter, rxn, mol, rec, cursor, label_index=InMemoryLabelIndex(), source="test-eln"
        )
        return summary.skipped_existing, summary.ingested

    skipped, ingested = asyncio.run(_run())
    assert (skipped, ingested) == (["row-4711"], []), (
        "the replay was re-ingested, so the unchanged-entry lookup is keyed on the entry id while "
        "the record is stored under the reaction id"
    )


def test_a_structural_hit_still_expands_into_its_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capability the user asked to keep: "same product / similar reaction", with the prose.

    End to end with **no note file on disk and no git repository configured**, which is what makes
    this a test of the new path rather than of a leftover of the old one. The chunk a structural
    hit yields is a citation, so the recipe question is answered by handing that citation to
    `expand_note` — exactly the round trip a chemist takes.
    """
    monkeypatch.setattr(settings, "knowledge_dir", "/nonexistent-knowledge")

    async def _run() -> tuple[list[str], str]:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        adapter = _ListAdapter([_entry("rxn-recipe", datetime(2026, 3, 1, tzinfo=UTC))])
        await sync_entries(
            adapter, rxn, mol, rec, _EPOCH, label_index=InMemoryLabelIndex(), source="test-eln"
        )

        monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
        retriever = FingerprintReactionRetriever(rxn, rec)
        chunks = await retriever.retrieve("CCO.CC(=O)O>>CCOC(C)=O", {})
        cited = [chunk.source_note_id for chunk in chunks]

        # Patched where `graph_tools` bound the name, not where it is defined: it imports the
        # function directly, so patching the source module would leave the real store in place.
        monkeypatch.setattr("chemclaw.agent.graph_tools.default_record_store", lambda: rec)
        view = await expand_note(cited[0])
        return cited, view.body

    cited, body = asyncio.run(_run())
    assert cited == [note_id_for_reaction("rxn-recipe")]
    assert "80.0 °C" in body and "Ethanol and acetic acid" in body, (
        "a structural hit must expand into the run's conditions and procedure; a citation with no "
        "readable body is the D-018 failure this change was supposed to remove"
    )


def test_a_reaction_cited_by_a_campaign_still_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case, and the one the first version of this file could not see.

    `build_graph` mints a bare node for every cited-but-undefined link target, so a reaction cited
    by any campaign or playbook **is** a member of the graph while carrying no note. `expand_note`
    guarded its store fallback on `note_id not in graph`, which is therefore False for exactly the
    reactions the fallback exists to serve — and `_require_note` raised "no note with id" for a run
    that was sitting in the corpus.

    The original test missed it by pointing `knowledge_dir` at a nonexistent path, so the graph was
    empty and membership was never True. This one writes the citing campaign, which is what the
    corpus actually looks like once `memory.campaign` has run.
    """
    (tmp_path / "campaign").mkdir()
    (tmp_path / "campaign" / "campaign-x.md").write_text(
        "---\nid: campaign-x\ntype: campaign\ncreated_by: agent\n---\n\n"
        f"1. [[{note_id_for_reaction('rxn-cited')}]]: `CCO.CC(=O)O>>CCOC(C)=O`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    async def _run() -> str:
        store = InMemoryReactionRecordStore()
        adapter = _ListAdapter([_entry("rxn-cited", datetime(2026, 3, 1, tzinfo=UTC))])
        await store.record(
            [record_from_ord_reaction(adapter.map_to_ord(adapter._entries[0]))], "eln-json"
        )
        monkeypatch.setattr("chemclaw.agent.graph_tools.default_record_store", lambda: store)
        return (await expand_note(note_id_for_reaction("rxn-cited"))).body

    body = asyncio.run(_run())
    assert "Ethanol and acetic acid" in body, (
        "a reaction cited by a campaign did not expand; the graph holds a bare node for it, so a "
        "membership test skips the store fallback the citation exists to reach"
    )


def test_expanding_a_citation_to_an_unknown_record_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing record is a clear error, not a silently empty view."""
    monkeypatch.setattr(
        "chemclaw.agent.graph_tools.default_record_store", lambda: InMemoryReactionRecordStore()
    )

    async def _run() -> None:
        with pytest.raises(ChemclawError, match="no reaction record"):
            await expand_note("reaction-never-ingested")

    asyncio.run(_run())


def test_condense_protocols_resolves_a_reaction_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """`condense_protocols` reads runs out of the store, not only notes out of the graph.

    Runs left the graph's id space when they became rows, and they are the largest class of
    protocol this tool exists to compare — the hits `similar_reactions` hands back. Without the
    record fallback every one of them reads as `missing`: the tool would answer "I could not find
    those" about the corpus it was built for, which is the silent hole `_from_share` was written to
    close for share documents, arriving from the other side.

    Asserted through `_from_record` rather than through the whole tool, because condensing calls a
    model; what is being pinned here is the resolution, including that the figures ride along as
    numbers rather than being left for the comparison to re-derive from prose.
    """
    monkeypatch.setattr(settings, "knowledge_dir", "/nonexistent-knowledge")

    async def _run() -> Protocol | None:
        store = InMemoryReactionRecordStore()
        adapter = _ListAdapter([_entry("rxn-cond", datetime(2026, 3, 1, tzinfo=UTC))])
        record = record_from_ord_reaction(adapter.map_to_ord(adapter._entries[0]))
        await store.record([record], "eln-json")
        monkeypatch.setattr("chemclaw.agent.protocol_tools.default_record_store", lambda: store)
        return await _from_record(note_id_for_reaction("rxn-cond"))

    protocol = asyncio.run(_run())
    assert protocol is not None, (
        "a reaction reference resolved to nothing — the tool would report it as `missing`"
    )
    assert protocol.conditions is not None and protocol.conditions.yield_percent == 85.0
    assert "Ethanol and acetic acid" in protocol.text


def test_condense_protocols_leaves_a_non_reaction_reference_alone() -> None:
    """The record fallback must not swallow a share document's `source:doc_id` citation."""

    async def _run() -> Protocol | None:
        return await _from_record("share:some-document-id")

    assert asyncio.run(_run()) is None


def _campaign_citing(tmp_path: Path, target: str) -> None:
    """Write a campaign note whose body cites `target`, as `memory.campaign` renders one."""
    directory = tmp_path / "campaign"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "campaign-x.md").write_text(
        "---\nid: campaign-x\ntype: campaign\ncreated_by: agent\n---\n\n"
        f"1. [[{target}]]: `CCO.CC(=O)O>>CCOC(C)=O`\n",
        encoding="utf-8",
    )


def test_a_campaign_citing_a_reaction_record_is_not_dangling(tmp_path: Path) -> None:
    """Reactions left the graph's id space, so the offline check must stop calling them broken.

    Without this, every campaign, playbook and optimization note fails `kg-validate` the moment
    transcriptions stop being files — for links that resolve perfectly well.
    """
    _campaign_citing(tmp_path, note_id_for_reaction("rxn-1"))
    assert validate(tmp_path) == []


def test_a_citation_to_a_missing_record_is_still_caught() -> None:
    """The other half: what `dangling_links` gave up, the store has to answer for.

    Offline validation can no longer tell a real run id from a typo'd one — that is the stated cost
    of the namespace rule. This is the check that takes it back, and it is why `kg-validate` is run
    in CI with a database rather than without one.
    """

    async def _run() -> tuple[list[str], list[str]]:
        store = InMemoryReactionRecordStore()
        await store.record(
            [ReactionRecord(reaction_id="real", body="a real run", source="eln:test")], "test-eln"
        )
        citations = external_citations(
            [
                Note(
                    id="campaign-x",
                    type="campaign",
                    created_by="agent",
                    body="1. [[reaction-real]] then [[reaction-typo]]",
                )
            ]
        )
        # The store is a parameter, so the check needs no patching at all — which is the point of
        # `RecordExistence` being a Protocol the caller satisfies.
        return [target for _, target in citations], await unresolved_citations(citations, store)

    cited, problems = asyncio.run(_run())
    assert cited == ["reaction-real", "reaction-typo"]
    assert len(problems) == 1 and "reaction-typo" in problems[0]


def test_the_postgres_store_and_the_in_memory_one_answer_alike() -> None:
    """The two backends must agree, or the ingest tests prove something the deployment does not.

    Exercises the durable store against a real database: the upsert (including the amendment
    overwrite), the body lookup, and every arm of the eligibility filter — which is the one piece
    written twice, once as `ReactionRecord.passes` and once as SQL.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        durable = PostgresReactionRecordStore()
        memory = InMemoryReactionRecordStore()
        records = [
            ReactionRecord(
                reaction_id="pg-alpha",
                body="alpha body",
                project="prj-alpha",
                performed_at=date(2026, 3, 1),
                source="eln:test",
            ),
            ReactionRecord(
                reaction_id="pg-undated", body="undated body", project=None, source="eln:test"
            ),
        ]
        for store in (durable, memory):
            await store.record(records, "pg-eln")

        ids = ["pg-alpha", "pg-undated", "pg-absent"]
        cases: list[dict[str, object]] = [
            {},
            {"type": "reaction"},
            {"type": "playbook"},
            {"tag": "prj-alpha"},
            {"tag": "prj-nope"},
            {"since": date(2026, 1, 1)},
            {"since": date(2026, 6, 1)},
            {"until": date(2026, 6, 1)},
            {"since": date(2026, 1, 1), "until": date(2026, 6, 1)},
        ]
        for filters in cases:
            assert await durable.eligible(ids, filters) == await memory.eligible(ids, filters), (
                f"the SQL filter and `ReactionRecord.passes` disagree on {filters}"
            )

        assert await durable.bodies(ids, "pg-eln") == await memory.bodies(ids, "pg-eln")
        assert await durable.known(ids) == {"pg-alpha", "pg-undated"}

        # An amendment overwrites in place — no second row, no versioning scheme.
        amended = records[0].model_copy(update={"body": "alpha body, yield corrected to 31%"})
        await durable.record([amended], "pg-eln")
        stored = await durable.read("pg-alpha")
        assert stored is not None and stored.body == amended.body
        assert await durable.known(["pg-alpha"]) == {"pg-alpha"}

    asyncio.run(_run())


def test_the_default_store_is_the_durable_one() -> None:
    """`default_record_store` must not quietly hand back an in-memory store."""
    assert isinstance(default_record_store(), PostgresReactionRecordStore)


def test_the_citation_gate_fails_when_it_cannot_reach_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gate that could not look has not passed — it printed a line and returned success.

    `dangling_links` ignores every `reaction-` target since D-2026-08-25, deliberately, because the
    graph cannot see the store. That makes this half the *only* thing between a typo'd run id and a
    merge, so an unreachable database is a failed gate rather than a warning beside a zero exit.
    """
    _campaign_citing(tmp_path, note_id_for_reaction("rxn-1"))

    class _Unreachable:
        async def known(self, reaction_ids: Sequence[str]) -> set[str]:
            raise ConnectionError("Postgres unreachable")

    monkeypatch.setattr("chemclaw.cli.validate_kg.default_record_store", lambda: _Unreachable())
    monkeypatch.setattr(sys, "argv", ["validate_kg", str(tmp_path)])
    exit_code = _validate_kg_main()

    printed = capsys.readouterr().out
    assert exit_code == 1, f"the gate passed without checking anything:\n{printed}"
    assert "NOT CHECKED" in printed and "did not pass" in printed


# --- one entry id, two ELNs ----------------------------------------------------------------------


def _sited(reaction_id: str, site: str, body: str) -> ReactionRecord:
    """One site's transcription of an entry id both sites happen to use."""
    return ReactionRecord(reaction_id=reaction_id, body=body, source=f"{site}:{reaction_id}")


def test_two_sources_sharing_an_entry_id_do_not_overwrite_each_other() -> None:
    """`EXP-1001` at two sites is two runs, and the row key has to be able to say so.

    `ingest_reaction`'s own docstring names the collision — "two ELNs may legitimately use one entry
    id" — and answered it with a `source` column beside a bare-id key. That column only records
    *which one won*: the upsert refreshes every field including `source`, so with two ingest sources
    enabled the later sync silently replaced the earlier site's transcription, and every
    `reaction-EXP-1001` citation a playbook carried then resolved to a different run at a different
    site. `kg-validate` still passed — the citation resolves, to the wrong record. The label index
    put `(source, reaction_id)` in its key for exactly this reason; this tier did not.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        await store.record([_sited("EXP-1001", "site-a", "82% Suzuki")], source="eln-a")
        await store.record([_sited("EXP-1001", "site-b", "nitration, failed")], source="eln-b")

        assert len(await store.all_records()) == 2, "one site's transcription was destroyed"
        assert await store.bodies(["EXP-1001"], source="eln-a") == {"EXP-1001": "82% Suzuki"}
        assert await store.bodies(["EXP-1001"], source="eln-b") == {"EXP-1001": "nitration, failed"}

    asyncio.run(_run())


def test_a_citation_that_two_sources_could_answer_is_refused_rather_than_guessed() -> None:
    """`reaction-EXP-1001` names no source, so with two rows behind it there is no right answer.

    Returning either is a coin flip that reads as a fact — the failure mode this whole finding is
    about — so the read refuses and names both sources. An operator can then scope the sources or
    the site can re-key its export; what they cannot do is not find out.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        await store.record([_sited("EXP-1001", "site-a", "82% Suzuki")], source="eln-a")
        await store.record([_sited("EXP-1001", "site-b", "nitration, failed")], source="eln-b")

        with pytest.raises(ChemclawError, match="eln-a"):
            await store.read("EXP-1001")

    asyncio.run(_run())


def test_the_postgres_store_keys_transcriptions_by_source_too() -> None:
    """The `ON CONFLICT` clause and the primary key are the deployment's half of the same rule."""

    async def _run() -> None:
        await migrated_db_or_skip()
        durable = PostgresReactionRecordStore()
        await durable.record([_sited("pg-shared", "site-a", "a body")], source="pg-eln-a")
        await durable.record([_sited("pg-shared", "site-b", "b body")], source="pg-eln-b")

        assert await durable.bodies(["pg-shared"], source="pg-eln-a") == {"pg-shared": "a body"}
        assert await durable.bodies(["pg-shared"], source="pg-eln-b") == {"pg-shared": "b body"}
        with pytest.raises(ChemclawError, match="pg-eln-a"):
            await durable.read("pg-shared")

    asyncio.run(_run())
