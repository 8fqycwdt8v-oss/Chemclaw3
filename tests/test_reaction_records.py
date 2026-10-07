"""The transcription tier: ELN reactions as store rows rather than git notes.

Asserts that ingest performs no git operation, that a sync run's cost does not grow with the
corpus, that a structural hit still expands into its recipe with no note on disk, and that
`[[reaction-<id>]]` citations are checked against the store. `tests/test_eln.py` covers the
mapping and the sync loop's bookkeeping.
"""

import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent.condense import Protocol
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.agent.graph_tools import expand_note
from chemclaw.agent.protocol_tools import _from_record
from chemclaw.cli.validate_kg import main as _validate_kg_main
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.eln.ingest import ingest_reaction
from chemclaw.ingest.eln.json_adapter import JsonExportAdapter
from chemclaw.ingest.eln.ord import OrdReaction, Role, RoleSpecies
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import (
    _SELECT_BODIES,
    _SELECT_ONE,
    InMemoryReactionRecordStore,
    PostgresReactionRecordStore,
    ReactionRecord,
    UnreadableConditions,
    default_record_store,
)
from chemclaw.ingest.eln.sync import IngestSummary, sync_entries
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

    async def fetch_new_entries(
        self,
        since: datetime,
        limit: int | None = None,
        *,
        report_late_arrivals: bool = True,
    ) -> list[RawEntry]:
        """Return the fixed list, ignoring both capabilities as the file-drop adapters do."""
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
    """Every path from `ingest_reaction` to git raises, so re-introducing a git write fails here."""
    monkeypatch.setattr("chemclaw.kg.git_writer.default_writer", lambda: _ExplodingSubmitter())

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


async def test_a_sync_run_does_not_read_the_corpus_it_is_not_replaying() -> None:
    """Cost is bounded by the page, not by how much has already been ingested.

    Asserted by counting what the store is asked for rather than by timing: a replay asks for
    exactly the batch's ids, and a run with no replay asks for nothing.
    """
    asked: list[int] = []

    class _CountingStore(InMemoryReactionRecordStore):
        """Records how many ids each unchanged-entry lookup asked for."""

        async def bodies(self, reaction_ids: Sequence[str], source: str) -> dict[str, str]:
            """Count the request, then answer it."""
            asked.append(len(reaction_ids))
            return await super().bodies(reaction_ids, source)

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

    assert asked == [1], (
        f"the unchanged-entry lookup asked for {asked}; it must be keyed on the batch (1 id), "
        "never on the 500-record corpus — that is the growth this tier exists to remove"
    )


def test_the_unchanged_check_keys_on_the_record_id_not_the_entry_id() -> None:
    """The unchanged check keys on the record id, which may differ from the source's entry id.

    Keying on the wrong one misses silently (the upsert is idempotent) and re-ingests everything
    forever, so the fixture makes the two ids deliberately unequal.
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
    """A structural hit's citation still expands into its recipe via `expand_note`.

    Runs with no note file and no git repository configured, so only the store path can answer.
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
    # A literal rather than `note_id_for_reaction(...)`, so the expectation is not derived from the
    # function under test; `expand_note` below resolves this exact id.
    assert cited == ["reaction-test-eln.rxn-recipe"]
    assert "80.0 °C" in body and "Ethanol and acetic acid" in body, (
        "a structural hit must expand into the run's conditions and procedure; a citation with no "
        "readable body is the D-018 failure this change was supposed to remove"
    )


def test_a_reaction_cited_by_a_campaign_still_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaction cited by a campaign still expands although the graph holds a bare node for it.

    `build_graph` mints a node for every cited target, so the store fallback must not be guarded on
    graph membership. The citing campaign is written so the graph is not empty.
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


async def test_expanding_a_citation_to_an_unknown_record_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing record is a clear error, not a silently empty view."""
    monkeypatch.setattr(
        "chemclaw.agent.graph_tools.default_record_store", lambda: InMemoryReactionRecordStore()
    )

    with pytest.raises(ChemclawError, match="no reaction record"):
        await expand_note("reaction-never-ingested")


def test_condense_protocols_resolves_a_reaction_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """`condense_protocols` resolves runs from the record store, not only notes from the graph.

    Asserted through `_from_record` because condensing calls a model; it pins the resolution and
    that figures ride along as numbers.
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
    # The structured species ride along too, so the comparison diffs them exactly (#490).
    assert protocol.species is not None
    assert protocol.species.of(Role.REACTANT) == {"CCO", "CC(=O)O"}
    assert protocol.species.of(Role.REAGENT) == frozenset()


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
    """A citation to a missing record is caught by the store half of the check.

    Offline validation cannot tell a real run id from a typo, so `kg-validate` runs with a database.
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


async def test_the_postgres_store_and_the_in_memory_one_answer_alike() -> None:
    """The Postgres store and the in-memory one answer alike.

    Covers the upsert (including amendments), body lookup and every arm of the eligibility filter,
    which exists twice: as `ReactionRecord.passes` and as SQL.
    """
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
    # The species projection round-trips, and "none stored" stays `None` rather than four empty
    # roles — the distinction `agent.condense._changes` skips on (#490).
    projected = records[0].model_copy(
        update={"species": RoleSpecies(reactant=["CCO"], solvent=[], catalyst=["Cl[Pd]Cl"])}
    )
    await durable.record([projected], "pg-eln")
    back = await durable.read("pg-alpha")
    assert back is not None and back.species == projected.species
    undated = await durable.read("pg-undated")
    assert undated is not None and undated.species is None

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


async def _index_behind(statement: str, params: tuple[object, ...]) -> str:
    """The index the planner uses for `statement`, with sequential scans taken away.

    On a fixture-sized table a sequential scan is correct, so it is disabled to ask which index the
    schema offers for the predicate.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("SET LOCAL enable_seqscan = off")
        cursor = await conn.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        row = await cursor.fetchone()
    plan = row[0][0]["Plan"] if row else {}
    nodes = [plan]
    while nodes:
        node = nodes.pop()
        if "Index Name" in node:
            return str(node["Index Name"])
        nodes.extend(node.get("Plans", []))
    return ""


async def _leading_columns() -> set[str]:
    """The first indexed column of every index on `reaction_records`, from the live catalog."""
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT i.relname, a.attname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.indkey[0] "
            "WHERE t.relname = 'reaction_records' "
            "AND t.relnamespace = current_schema()::regnamespace"
        )
        return {str(row[1]) for row in await cursor.fetchall()}


def test_a_record_lookup_by_id_is_served_by_an_index_leading_with_that_id() -> None:
    """`read()` and `known()` filter on the bare `reaction_id`; an index must lead with it.

    Timing cannot show this on fixture-sized tables, so two scale-free checks are used: the plan for
    `_SELECT_ONE` (with sequential scans disabled) names the new index, and the catalog holds an
    index leading with `reaction_id` for the batch read. `_SELECT_BODIES` filters on the pair and
    must keep using the primary key.
    """

    async def _run() -> tuple[str, str, set[str]]:
        await migrated_db_or_skip()
        await PostgresReactionRecordStore().record(
            [ReactionRecord(reaction_id="idx-probe", body="body", source="eln:test")], "pg-eln"
        )
        ids = [f"idx-probe-{index}" for index in range(50)]
        return (
            await _index_behind(_SELECT_ONE, ("idx-probe",)),
            await _index_behind(_SELECT_BODIES, ("pg-eln", ids)),
            await _leading_columns(),
        )

    one, bodies, leading = asyncio.run(_run())
    assert one == "reaction_records_id_idx", (
        f"a single-record read plans through {one!r}, which does not lead with reaction_id"
    )
    assert "reaction_id" in leading, (
        "no index on reaction_records leads with reaction_id, so `known()` scans the whole table; "
        f"the leading columns present are {sorted(leading)}"
    )
    assert bodies == "reaction_records_pkey", (
        f"the source-scoped body read moved off the primary key onto {bodies!r}"
    )


def test_the_default_store_is_the_durable_one() -> None:
    """`default_record_store` must not quietly hand back an in-memory store."""
    assert isinstance(default_record_store(), PostgresReactionRecordStore)


def test_the_citation_gate_fails_when_it_cannot_reach_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The citation gate fails when it cannot reach the store, rather than passing with a warning.

    `dangling_links` ignores `reaction-` targets, so this check is the only one between a typo'd run
    id and a merge.
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


async def test_two_sources_sharing_an_entry_id_do_not_overwrite_each_other() -> None:
    """`EXP-1001` at two sites is two runs, and the row key has to be able to say so.

    With a bare-id key the later sync silently replaced the earlier site's transcription and
    citations resolved to the wrong run; the key is `(source, reaction_id)`.
    """
    store = InMemoryReactionRecordStore()
    await store.record([_sited("EXP-1001", "site-a", "82% Suzuki")], source="eln-a")
    await store.record([_sited("EXP-1001", "site-b", "nitration, failed")], source="eln-b")

    assert len(await store.all_records()) == 2, "one site's transcription was destroyed"
    assert await store.bodies(["EXP-1001"], source="eln-a") == {"EXP-1001": "82% Suzuki"}
    assert await store.bodies(["EXP-1001"], source="eln-b") == {"EXP-1001": "nitration, failed"}


async def test_a_citation_that_two_sources_could_answer_is_refused_rather_than_guessed() -> None:
    """A sourceless citation two rows could answer is refused, naming both sources, not guessed."""
    store = InMemoryReactionRecordStore()
    await store.record([_sited("EXP-1001", "site-a", "82% Suzuki")], source="eln-a")
    await store.record([_sited("EXP-1001", "site-b", "nitration, failed")], source="eln-b")

    with pytest.raises(ChemclawError, match="eln-a"):
        await store.read("EXP-1001")


async def test_the_postgres_store_keys_transcriptions_by_source_too() -> None:
    """The `ON CONFLICT` clause and the primary key are the deployment's half of the same rule."""
    await migrated_db_or_skip()
    durable = PostgresReactionRecordStore()
    await durable.record([_sited("pg-shared", "site-a", "a body")], source="pg-eln-a")
    await durable.record([_sited("pg-shared", "site-b", "b body")], source="pg-eln-b")

    assert await durable.bodies(["pg-shared"], source="pg-eln-a") == {"pg-shared": "a body"}
    assert await durable.bodies(["pg-shared"], source="pg-eln-b") == {"pg-shared": "b body"}
    with pytest.raises(ChemclawError, match="pg-eln-a"):
        await durable.read("pg-shared")


async def _write_raw_conditions(reaction_id: str, conditions: object) -> None:
    """Put `conditions` into the column without going through `record`.

    The payload is one a newer build writes, which no code in this checkout can produce.
    """
    from psycopg.types.json import Jsonb

    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO reaction_records (ingest_source, reaction_id, body, compound_smiles, "
            "project, performed_at, conditions, source) "
            "VALUES ('eln', %s, 'b', 'CCO', 'p', NULL, %s, 'probe') "
            "ON CONFLICT (ingest_source, reaction_id) "
            "DO UPDATE SET conditions = EXCLUDED.conditions",
            (reaction_id, None if conditions is None else Jsonb(conditions)),
        )
        await conn.commit()


def test_a_row_a_newer_build_wrote_is_still_readable_by_an_older_one() -> None:
    """A row a newer build wrote is still readable by an older one.

    `extra="forbid"` guards writes; on reads it would fail a chemist's query during a rolling
    upgrade. The known field is asserted so a read that discarded the payload would fail too.
    """

    async def _run() -> object:
        await migrated_db_or_skip()
        await _write_raw_conditions(
            "rxn-future-field", {"temperature_c": 25.0, "pressure_bar_v2": 3}
        )
        record = await PostgresReactionRecordStore().read("rxn-future-field")
        assert record is not None and record.conditions is not None
        return record.conditions.temperature_c

    assert asyncio.run(_run()) == 25.0, (
        "a row from a newer build was unreadable, or was read by throwing its conditions away"
    )


def test_recorded_but_all_unknown_conditions_are_not_read_as_absent() -> None:
    """`{}` is "recorded, all unknown"; NULL is "not recorded". A falsy test collapsed the two.

    `comparison.MISSING` renders the two differently, so the distinction is one a chemist sees.
    """

    async def _run() -> tuple[object, object]:
        await migrated_db_or_skip()
        store = PostgresReactionRecordStore()
        await _write_raw_conditions("rxn-empty-conditions", {})
        await _write_raw_conditions("rxn-null-conditions", None)
        empty = await store.read("rxn-empty-conditions")
        null = await store.read("rxn-null-conditions")
        assert empty is not None and null is not None
        return empty.conditions, null.conditions

    empty, null = asyncio.run(_run())
    assert empty is not None, "an all-unknown conditions record read as if none were recorded"
    assert null is None, "a row with no conditions grew some"


def test_a_conditions_payload_that_is_not_an_object_is_refused_by_name() -> None:
    """A non-object conditions payload is refused as corruption, naming table and reaction."""

    async def _run() -> str:
        await migrated_db_or_skip()
        await _write_raw_conditions("rxn-array-conditions", [1, 2])
        try:
            await PostgresReactionRecordStore().read("rxn-array-conditions")
        except UnreadableConditions as exc:
            return str(exc)
        return ""

    message = asyncio.run(_run())
    assert "rxn-array-conditions" in message, "the refusal does not name the row to act on"


def test_one_entry_reporting_a_non_finite_number_does_not_wedge_every_later_run() -> None:
    """One entry with a non-finite number is rejected and does not wedge every later run.

    Postgres rejects `NaN` in `jsonb` with an error outside the per-entry guard, which aborted the
    pass and pinned the cursor forever. It must be refused earlier, as one rejected entry. Driven
    against the real column; in-memory stores accept NaN.
    """

    def _with_temperature(entry_id: str, celsius: float) -> RawEntry:
        raw = _entry(entry_id, datetime(2026, 5, 4, tzinfo=UTC))
        raw.payload["temperature_c"] = celsius
        return raw

    async def _run() -> IngestSummary:
        await migrated_db_or_skip()
        entries = [
            _with_temperature("nan-before", 25.0),
            _with_temperature("nan-poison", float("nan")),
            _with_temperature("nan-after", 30.0),
        ]
        return await sync_entries(
            _ListAdapter(entries),
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            PostgresReactionRecordStore(),
            _EPOCH,
            label_index=InMemoryLabelIndex(),
            source="eln:nan",
        )

    summary = asyncio.run(_run())
    assert summary.ingested == ["nan-before", "nan-after"], (
        "the entries either side of the bad one did not survive it"
    )
    assert [r.entry_id for r in summary.rejected] == ["nan-poison"]
    assert "temperature_c" in summary.rejected[0].reason, (
        f"the rejection does not name the field to correct: {summary.rejected[0].reason!r}"
    )
    assert summary.next_cursor > _EPOCH, "the cursor did not advance, so the next run repeats this"


def test_expanding_a_withdrawn_record_resolves_and_says_it_was_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expanding a withdrawn record resolves and says it was withdrawn.

    `read()` keeps answering for a retracted row while `eligible()` stops, so a citing note expands
    into "withdrawn" rather than "no note". The notice carries `SYSTEM_SPEECH_MARK` outside the
    framed body, and `valid_to` carries the same fact in the structured half.
    """
    withdrawn = datetime(2026, 3, 4, tzinfo=UTC)

    async def _run() -> Any:
        store = InMemoryReactionRecordStore()
        adapter = _ListAdapter([_entry("rxn-pulled", datetime(2026, 3, 1, tzinfo=UTC))])
        record = record_from_ord_reaction(adapter.map_to_ord(adapter._entries[0]))
        await store.record([record.model_copy(update={"retracted_at": withdrawn})], "eln-json")
        monkeypatch.setattr("chemclaw.agent.graph_tools.default_record_store", lambda: store)
        return await expand_note(note_id_for_reaction("rxn-pulled"))

    view = asyncio.run(_run())

    assert "Ethanol and acetic acid" in view.body, "the transcription stopped being served at all"
    assert "withdrew this ELN entry on 2026-03-04" in view.body, (
        "a withdrawn run expanded with nothing saying it was withdrawn"
    )
    assert SYSTEM_SPEECH_MARK in view.body.split("Ethanol")[0], (
        "the withdrawal notice is not marked as system speech, so it reads as something the ELN "
        "said about itself"
    )
    assert view.note.valid_to == withdrawn.date()
