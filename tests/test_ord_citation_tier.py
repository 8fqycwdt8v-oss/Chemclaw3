"""The citation-only tier: a reaction the source only half-drew is evidence, never a structure.

`D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable` records the owner decision:
a record naming a species without its structure — the Perera flow-Suzuki screen's `2a, Boronic
Acid`, 5,760 of the mock's 10,011 seeded records — is ingested **citable** and **excluded from
structure and similarity search**. Each test below pins one half of that sentence at the seam where
it could break, starting from the entry points production calls:

- the adapter carries the name verbatim and invents nothing;
- the sync stores the record, in the tier, and writes **no** fingerprint, molecule or label row;
- a structure search — the retriever a sweep runs and the tool a chemist calls — never returns
  one, including the record whose stale fingerprint predates an amendment;
- a chemist who reaches one by citation is told which tier it is in, by the system, outside the
  source's framed text;
- the memory miners, which are all structural, never see one.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.agent.graph_tools import expand_note
from chemclaw.core.config import settings
from chemclaw.durable import memory_jobs
from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.eln.ord import (
    Component,
    OrdReaction,
    RecordTier,
    Role,
    StructureNotGiven,
    UnstructuredComponent,
)
from chemclaw.ingest.eln.ord_adapter import OrdFormatError, OrdJsonAdapter
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import (
    InMemoryReactionRecordStore,
    PostgresReactionRecordStore,
)
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.pg import migrated_db_or_skip

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SOURCE = "eln-ord"

# The real shape, transcribed from the mock repository's flow-Suzuki seeding: the quinoline, the
# catalyst, the ligand and the base carry SMILES, the second coupling partner and the product carry
# only a NAME.
_QUINOLINE = "Clc1ccc2ncccc2c1"
_PARTNER = "2a, Boronic Acid"
_PRODUCT = "Suzuki-Miyaura coupling product of 6-chloroquinoline with 2a, Boronic Acid"
# What an amendment could have published for the partner, so one entry can change tier.
_PARTNER_SMILES = "OB(O)c1ccccc1"
_PRODUCT_SMILES = "c1ccc(-c2ccc3ncccc3c2)cc1"


def _identifiers(smiles: str | None, name: str | None) -> list[dict[str, str]]:
    """ORD `CompoundIdentifier`s for a species: its SMILES, its NAME, or both."""
    found = []
    if smiles:
        found.append({"type": "SMILES", "value": smiles})
    if name:
        found.append({"type": "NAME", "value": name})
    return found


def _flow_suzuki(
    reaction_id: str,
    *,
    partner_smiles: str | None = None,
    product_smiles: str | None = None,
    yield_pct: float = 4.76,
) -> dict[str, Any]:
    """One flow-Suzuki ORD export — citation-only unless the partner and product are drawn."""

    def one(key: str, order: int, identifiers: list[dict[str, str]], role: str) -> dict[str, Any]:
        return {
            key: {
                "components": [{"identifiers": identifiers, "reactionRole": role}],
                "additionOrder": order,
            }
        }

    inputs: dict[str, Any] = {}
    inputs.update(one("quinoline", 1, _identifiers(_QUINOLINE, None), "REACTANT"))
    inputs.update(one("partner", 2, _identifiers(partner_smiles, _PARTNER), "REACTANT"))
    inputs.update(one("catalyst", 3, _identifiers("CC(=O)O[Pd]OC(=O)C", None), "CATALYST"))
    inputs.update(one("base", 4, _identifiers("[OH-].[Na+]", "NaOH"), "REAGENT"))
    return {
        "reactionId": reaction_id,
        "inputs": inputs,
        "outcomes": [
            {
                "products": [
                    {
                        "identifiers": _identifiers(product_smiles, _PRODUCT),
                        "measurements": [{"type": "YIELD", "percentage": {"value": yield_pct}}],
                    }
                ]
            }
        ],
        "notes": {"procedureDetails": "Flow screen in MeCN. UPLC-UV area-percent yield."},
        "provenance": {"recordCreated": {"time": {"value": "2023-04-01T09:00:00Z"}}},
    }


def _map(payload: dict[str, Any]) -> OrdReaction:
    """Map one export through the production adapter's `map_to_ord`."""
    return OrdJsonAdapter("/nonexistent").map_to_ord(
        RawEntry(entry_id=str(payload["reactionId"]), created_at=_EPOCH, payload=payload)
    )


def _write(directory: Path, *payloads: dict[str, Any]) -> OrdJsonAdapter:
    """Drop `payloads` into `directory` as ORD exports; return the adapter that reads them."""
    for payload in payloads:
        (directory / f"{payload['reactionId']}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return OrdJsonAdapter(str(directory))


class _Stores:
    """The four stores a sync writes, in memory, so the test can read back every one of them."""

    def __init__(self) -> None:
        """Start every store empty."""
        self.reactions = InMemoryFingerprintStore()
        self.molecules = InMemoryFingerprintStore()
        self.records = InMemoryReactionRecordStore()
        self.labels = InMemoryLabelIndex()

    async def sync(self, adapter: OrdJsonAdapter) -> Any:
        """Run the production `sync_entries` over `adapter` from the epoch."""
        return await sync_entries(
            adapter,
            self.reactions,
            self.molecules,
            self.records,
            _EPOCH,
            label_index=self.labels,
            source=_SOURCE,
            apply_overlap=False,
        )


# --- the adapter ------------------------------------------------------------------------------


def test_a_named_only_species_arrives_as_its_name_and_makes_the_record_citation_only() -> None:
    """The source's text verbatim, in the unstructured list, and the structured half untouched."""
    reaction = _map(_flow_suzuki("suzuki-1"))

    assert reaction.tier is RecordTier.CITATION_ONLY
    assert [(c.name, c.role) for c in reaction.unstructured] == [
        (_PARTNER, Role.REACTANT),
        (_PRODUCT, Role.PRODUCT),
    ]
    assert [c.smiles for c in reaction.inputs] == [
        _QUINOLINE,
        "CC(=O)O[Pd]OC(=O)C",
        "[OH-].[Na+]",
    ]
    assert reaction.outcomes == []
    assert reaction.yield_percent == 4.76


def test_a_citation_only_record_refuses_to_assemble_a_reaction_smiles() -> None:
    """Both structure strings refuse, rather than returning the reaction minus its partner.

    `transformation_smiles` is the fingerprint input; the structured subset of this record is a
    reaction nobody ran, and returning it would put that into an index silently.
    """
    reaction = _map(_flow_suzuki("suzuki-1"))
    with pytest.raises(StructureNotGiven, match="2a, Boronic Acid"):
        reaction.transformation_smiles()
    with pytest.raises(StructureNotGiven):
        reaction.reaction_smiles()


def test_a_name_the_reagent_table_knows_still_resolves_to_a_structure() -> None:
    """The tier is what is *left* after every exact lookup, not a shortcut past them."""
    payload = _flow_suzuki("suzuki-1", product_smiles=_PRODUCT_SMILES)
    payload["inputs"]["partner"]["components"][0]["identifiers"] = [
        {"type": "NAME", "value": "acetonitrile"}
    ]
    reaction = _map(payload)
    assert reaction.tier is RecordTier.STRUCTURED
    assert "CC#N" in {c.smiles for c in reaction.inputs}


def test_a_compound_with_neither_a_structure_nor_a_name_is_still_refused() -> None:
    """Nothing to carry and nothing to show a reader: still a refusal, and a named one."""
    payload = _flow_suzuki("suzuki-1")
    payload["inputs"]["partner"]["components"][0]["identifiers"] = [
        {"type": "INCHI", "value": "InChI=1S/not-a-structure"}
    ]
    with pytest.raises(OrdFormatError, match="no resolvable structure identifier"):
        _map(payload)


def test_a_fully_drawn_export_is_structured_exactly_as_before() -> None:
    """The control arm: the same export with every species drawn has no unstructured species."""
    reaction = _map(
        _flow_suzuki("suzuki-1", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES)
    )
    assert reaction.tier is RecordTier.STRUCTURED
    assert reaction.unstructured == []
    assert reaction.transformation_smiles().endswith(">>" + _PRODUCT_SMILES)


def test_the_model_requires_an_input_and_a_product_across_both_tiers() -> None:
    """`min_length=1` moved into a validator that counts named-only species too."""
    named_product = UnstructuredComponent(name="the product", role=Role.PRODUCT)
    OrdReaction(
        reaction_id="r",
        inputs=[Component(smiles="CCO", role=Role.REACTANT)],
        unstructured=[named_product],
        provenance="test",
    )
    with pytest.raises(ValueError, match="at least one product"):
        OrdReaction(
            reaction_id="r", inputs=[Component(smiles="CCO", role=Role.REACTANT)], provenance="t"
        )
    with pytest.raises(ValueError, match="at least one input"):
        OrdReaction(reaction_id="r", unstructured=[named_product], provenance="t")


# --- validation and the record ----------------------------------------------------------------


def test_a_named_only_input_is_not_a_mass_balance_violation() -> None:
    """The balance is not checkable without the partner, so it is not run — and says nothing.

    Driven with a drawn product carrying boron that no *drawn* input supplies: the named reagent is
    where the boron came from, so comparing the structured halves would report a false violation.
    The control arm without the named reagent does report it, and a structure that does not parse
    is still reported whatever the tier.
    """
    reaction = OrdReaction(
        reaction_id="r",
        inputs=[Component(smiles="CCO", role=Role.REACTANT)],
        outcomes=[Component(smiles="CCOB(O)O", role=Role.PRODUCT)],
        unstructured=[UnstructuredComponent(name="a boron reagent", role=Role.REAGENT)],
        provenance="t",
    )
    assert validate_ord(reaction) == []
    drawn = OrdReaction.model_validate({**reaction.model_dump(), "unstructured": []})
    assert validate_ord(drawn) == ["mass balance: products contain B but no input supplies it"]
    broken = OrdReaction.model_validate(
        {**reaction.model_dump(), "inputs": [{"smiles": "C1CC", "role": "reactant"}]}
    )
    assert validate_ord(broken) == ["unparseable SMILES: 'C1CC'"]


def test_the_record_states_the_tier_and_names_each_species_as_the_source_gave_it() -> None:
    """What the chemist reads: the tier in the lead, each named species flagged, the yield kept."""
    record = record_from_ord_reaction(_map(_flow_suzuki("suzuki-1")))
    lead = record.body.split("\n", 1)[0]

    assert record.tier is RecordTier.CITATION_ONLY
    assert "Structure not given by the source" in lead and "citation-only" in lead
    assert "`" not in lead, "a citation-only record has no reaction SMILES to show"
    assert f"- {_PARTNER} — structure not given by the source (reactant)\n" in record.body
    assert f"- {_PRODUCT} — structure not given by the source (product)\n" in record.body
    assert f"- `{_QUINOLINE}` (reactant)\n" in record.body
    assert "- yield: 4.76%\n" in record.body
    assert record.compound_smiles is None
    assert record.conditions is not None and record.conditions.yield_percent == 4.76


def test_a_citation_only_record_stores_no_species_projection() -> None:
    """A projection reads structures only, so on this tier it would drop the named-only species.

    A neighbouring run's comparison would then report them removed. `None` is the one value every
    comparison skips; the drawn control arm still carries its projection.
    """
    named = record_from_ord_reaction(_map(_flow_suzuki("suzuki-1")))
    drawn = record_from_ord_reaction(
        _map(
            _flow_suzuki("suzuki-2", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES)
        )
    )

    assert named.tier is RecordTier.CITATION_ONLY and named.species is None
    assert drawn.tier is RecordTier.STRUCTURED and drawn.species is not None


def test_a_named_product_beside_a_drawn_one_is_two_products() -> None:
    """`compound_smiles` names the one product only when the source recorded exactly one."""
    reaction = OrdReaction(
        reaction_id="r",
        inputs=[Component(smiles="CCO", role=Role.REACTANT)],
        outcomes=[Component(smiles="CC=O", role=Role.PRODUCT)],
        unstructured=[UnstructuredComponent(name="a by-product", role=Role.PRODUCT)],
        provenance="t",
    )
    assert record_from_ord_reaction(reaction).compound_smiles is None


# --- ingest: stored, citable, and in no structural index --------------------------------------


def test_the_sync_stores_a_citation_only_record_and_indexes_nothing(tmp_path: Path) -> None:
    """The write half of the exclusion: no DRFP row, no molecule row, no label row.

    The structured control rides in the same batch, so an empty index cannot pass by the sync
    having written nothing at all.
    """
    adapter = _write(
        tmp_path,
        _flow_suzuki("suzuki-cited"),
        _flow_suzuki(
            "suzuki-drawn", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
        ),
    )
    stores = _Stores()

    async def _run() -> tuple[Any, dict[str, RecordTier], set[str], int]:
        summary = await stores.sync(adapter)
        tiers = {r.reaction_id: r.tier for r in await stores.records.all_records()}
        fingerprinted = {r.id for r in await stores.reactions.all_records()}
        return summary, tiers, fingerprinted, await stores.labels.count()

    summary, tiers, fingerprinted, labels = asyncio.run(_run())

    assert sorted(summary.ingested) == ["suzuki-cited", "suzuki-drawn"]
    assert summary.citation_only == ["suzuki-cited"]
    assert tiers == {
        "suzuki-cited": RecordTier.CITATION_ONLY,
        "suzuki-drawn": RecordTier.STRUCTURED,
    }
    assert fingerprinted == {"suzuki-drawn"}
    assert labels == 1
    molecules = {r.id for r in asyncio.run(stores.molecules.all_records())}
    assert _PARTNER_SMILES in molecules, "the structured control indexed its molecules"
    assert _QUINOLINE in molecules  # from the control, which charges the same quinoline


def test_a_citation_only_record_writes_no_molecule_row_of_its_own(tmp_path: Path) -> None:
    """Alone in the batch, so every molecule row would have to be its own."""
    stores = _Stores()
    asyncio.run(stores.sync(_write(tmp_path, _flow_suzuki("suzuki-cited"))))
    assert asyncio.run(stores.molecules.all_records()) == []
    assert asyncio.run(stores.reactions.all_records()) == []


# --- structure search never returns one -------------------------------------------------------


def _drawn_query() -> str:
    """The fully drawn transformation — the query a chemist asking about this coupling types."""
    return _map(
        _flow_suzuki("q", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES)
    ).transformation_smiles()


def test_structure_search_returns_the_drawn_run_and_never_the_citation_only_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the sweep's retriever and a filtered search, over a corpus holding both tiers."""
    monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
    adapter = _write(
        tmp_path,
        _flow_suzuki("suzuki-cited"),
        _flow_suzuki(
            "suzuki-drawn", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
        ),
    )
    stores = _Stores()

    async def _run() -> tuple[list[str], list[str]]:
        await stores.sync(adapter)
        retriever = FingerprintReactionRetriever(stores.reactions, stores.records)
        unfiltered = await retriever.retrieve(_drawn_query(), {})
        filtered = await retriever.retrieve(_drawn_query(), {"type": "reaction"})
        return [c.source_note_id for c in unfiltered], [c.source_note_id for c in filtered]

    unfiltered, filtered = asyncio.run(_run())
    drawn = note_id_for_reaction("suzuki-drawn", _SOURCE)
    assert unfiltered == [drawn]
    assert filtered == [drawn]


def _amended_to_citation_only(tmp_path: Path, stores: _Stores) -> None:
    """Ingest `suzuki-amended` drawn, then re-ingest it after the source took the structure away.

    The app role cannot DELETE from `reaction_fingerprints`, so the first ingest's row stays. This
    is the one route by which a citation-only record can sit in the structure index at all.
    """
    drawn = _flow_suzuki(
        "suzuki-amended", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
    )
    asyncio.run(stores.sync(_write(tmp_path, drawn)))
    amended = _flow_suzuki("suzuki-amended")
    amended["provenance"]["recordModified"] = [{"time": {"value": "2023-05-01T09:00:00Z"}}]
    summary = asyncio.run(stores.sync(_write(tmp_path, amended)))
    assert summary.citation_only == ["suzuki-amended"]


def test_a_stale_fingerprint_left_by_an_amendment_is_never_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read half of the exclusion, driven through the retriever on both of its paths."""
    monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
    stores = _Stores()
    _amended_to_citation_only(tmp_path, stores)

    async def _run() -> tuple[set[str], list[Any], list[Any]]:
        indexed = {r.id for r in await stores.reactions.all_records()}
        retriever = FingerprintReactionRetriever(stores.reactions, stores.records)
        return (
            indexed,
            await retriever.retrieve(_drawn_query(), {}),
            await retriever.retrieve(_drawn_query(), {"type": "reaction"}),
        )

    indexed, unfiltered, filtered = asyncio.run(_run())
    assert indexed == {"suzuki-amended"}, "the precondition: the stale row is really there"
    assert unfiltered == [] and filtered == []


def test_the_similar_reactions_tool_never_serves_a_citation_only_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool a chemist calls directly, over the same stale row."""
    from chemclaw.connectors.rxnfp.server import tools

    monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
    stores = _Stores()
    _amended_to_citation_only(tmp_path, stores)
    monkeypatch.setattr(tools, "_store", stores.reactions)
    monkeypatch.setattr(tools, "_records", stores.records)

    search = asyncio.run(tools.similar_reactions(_drawn_query()))
    assert search.hits == []

    # The control: the same row served while the record says structured, so the empty answer
    # above is the tier's doing and not the query's.
    async def _restructure() -> None:
        record = await stores.records.read("suzuki-amended", _SOURCE)
        assert record is not None
        await stores.records.record(
            [record.model_copy(update={"tier": RecordTier.STRUCTURED})], _SOURCE
        )

    asyncio.run(_restructure())
    served = asyncio.run(tools.similar_reactions(_drawn_query()))
    assert [hit.id for hit in served.hits] == [note_id_for_reaction("suzuki-amended", _SOURCE)]


# --- citing one -------------------------------------------------------------------------------


def test_expanding_a_citation_only_record_says_what_tier_it_is_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved by citation, with the tier as system text ahead of the source's framed body."""
    monkeypatch.setattr(settings, "knowledge_dir", "/nonexistent-knowledge")
    stores = _Stores()
    asyncio.run(stores.sync(_write(tmp_path, _flow_suzuki("suzuki-cited"))))
    monkeypatch.setattr("chemclaw.agent.graph_tools.default_record_store", lambda: stores.records)

    body = asyncio.run(expand_note(note_id_for_reaction("suzuki-cited", _SOURCE))).body

    notice, _, framed = body.partition("\n\n")
    assert notice.startswith("Citation-only record") and notice.endswith(SYSTEM_SPEECH_MARK)
    assert "infer no structure" in notice
    assert _PARTNER in framed and "yield: 4.76%" in framed


def test_expanding_a_structured_record_carries_no_tier_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the notice is the tier's, not every record's."""
    monkeypatch.setattr(settings, "knowledge_dir", "/nonexistent-knowledge")
    stores = _Stores()
    drawn = _flow_suzuki(
        "suzuki-drawn", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
    )
    asyncio.run(stores.sync(_write(tmp_path, drawn)))
    monkeypatch.setattr("chemclaw.agent.graph_tools.default_record_store", lambda: stores.records)

    body = asyncio.run(expand_note(note_id_for_reaction("suzuki-drawn", _SOURCE))).body
    assert "Citation-only" not in body


# --- the memory corpus ------------------------------------------------------------------------


def test_the_memory_corpus_leaves_citation_only_records_out_and_stays_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every miner is structural, so the tier is outside its corpus — and that is no gap in it."""
    adapter = _write(
        tmp_path,
        _flow_suzuki("suzuki-cited"),
        _flow_suzuki(
            "suzuki-drawn", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
        ),
    )
    monkeypatch.setattr(memory_jobs, "active_ingest_sources", lambda: [adapter])

    corpus = asyncio.run(memory_jobs.read_corpus())
    assert [r.reaction_id for r in corpus.reactions] == ["suzuki-drawn"]
    assert corpus.complete is True


# --- the durable store ------------------------------------------------------------------------


async def test_the_durable_store_keeps_the_tier_and_withholds_it_from_structure_search() -> None:
    """`110`'s column round-trips, the upsert moves it, and `structurally_withheld` reads it."""
    await migrated_db_or_skip()
    store = PostgresReactionRecordStore()
    source = "tier-test-source"
    cited = record_from_ord_reaction(_map(_flow_suzuki("tier-cited")))
    drawn = record_from_ord_reaction(
        _map(
            _flow_suzuki(
                "tier-drawn", partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
            )
        )
    )
    await store.record([cited, drawn], source)

    read = await store.read("tier-cited", source)
    assert read is not None and read.tier is RecordTier.CITATION_ONLY
    refs = [(source, "tier-cited"), (source, "tier-drawn"), ("", "tier-cited")]
    assert await store.structurally_withheld(refs) == {(source, "tier-cited"), ("", "tier-cited")}
    assert await store.retracted(refs) == set(), "citation-only is not a withdrawal"

    # An amendment that draws the partner moves the row back, through the same upsert.
    await store.record([cited.model_copy(update={"tier": RecordTier.STRUCTURED})], source)
    assert await store.structurally_withheld(refs) == set()


# --- every structural answer says what it did not search -------------------------------------
#
# The 2026-10-02 lane (finding N6): with the indexes complete, `substrate_precedent` said
# "COMPLETE: all 4282 …", `substructure_matches` said "a genuine negative result", and the model
# told a chemist there was no in-house data on 6-iodoquinoline while
# `reaction-suzuki-flow-hte-01243` drew it as a reactant and was citation-only because its partner
# was only named. The exclusion is the decided tier and stays; what changes is that every verdict
# states it, and names a record that lists the queried structure so the model can cite it.

# A quinoline only the structured control draws, so a query for it is one the tier cannot answer.
_BROMOQUINOLINE = "Brc1ccc2ncccc2c1"


def _drawn_control(reaction_id: str) -> dict[str, Any]:
    """A fully drawn flow-Suzuki on 6-bromoquinoline: structured, and sharing no substrate."""
    payload = _flow_suzuki(
        reaction_id, partner_smiles=_PARTNER_SMILES, product_smiles=_PRODUCT_SMILES
    )
    payload["inputs"]["quinoline"]["components"][0]["identifiers"] = _identifiers(
        _BROMOQUINOLINE, None
    )
    return payload


def _synced_and_labelled(tmp_path: Path) -> _Stores:
    """The cited 6-chloroquinoline run beside a drawn 6-bromoquinoline one, every label current.

    Labelled to completion, because the lane's defect was the *complete* index: the degraded
    states already hedged, and it was "COMPLETE" that was read as the whole ELN.
    """
    stores = _Stores()

    async def _run() -> None:
        await stores.sync(
            _write(tmp_path, _flow_suzuki("suzuki-cited"), _drawn_control("drawn-br"))
        )
        for row in await stores.labels.stale("v-test", 100):
            await stores.labels.store_labels(row, "v-test")

    asyncio.run(_run())
    return stores


def _tools_over(stores: _Stores, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """Both structural bundles' tool modules, reading `stores` in place of the Postgres ones."""
    from chemclaw.connectors.molfp.server import tools as molfp
    from chemclaw.connectors.rxnfp.server import tools as rxnfp

    monkeypatch.setattr(rxnfp, "_store", stores.reactions)
    monkeypatch.setattr(rxnfp, "_records", stores.records)
    monkeypatch.setattr(rxnfp, "_labels", stores.labels)
    monkeypatch.setattr(molfp, "_store", stores.molecules)
    monkeypatch.setattr(molfp, "_records", stores.records)
    return molfp.server, rxnfp.server


def _payload(server: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """What MCP sends back for one call — the structured payload, `verdict` included."""
    _content, structured = asyncio.run(server.call_tool(tool, arguments))
    assert isinstance(structured, dict)
    return structured


def _verdict(payload: dict[str, Any]) -> str:
    """The sentence a result carries, at whichever level its type keeps it."""
    return str(payload.get("verdict") or payload["coverage"]["verdict"])


def test_a_structural_negative_names_the_citation_only_record_that_draws_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The N6 repro, through each structural tool's MCP surface: the empty answer cites the run.

    Driven from `server.call_tool`, because the payload MCP sends is the only thing the model reads
    and a field `model_dump()` drops does not exist for it. The query is spelled in Kekulé form, so
    the record's aromatic spelling is reached through the canonical spelling and not by luck.
    """
    monkeypatch.setattr(settings, "fingerprint_similarity_threshold", 0.0)
    stores = _synced_and_labelled(tmp_path)
    molfp, rxnfp = _tools_over(stores, monkeypatch)
    kekule = "ClC1=CC2=CC=CN=C2C=C1"  # 6-chloroquinoline, which only the cited record draws
    cited = note_id_for_reaction("suzuki-cited", _SOURCE)

    answers = {
        "substrate_precedent": _payload(rxnfp, "substrate_precedent", {"smiles": kekule}),
        "substructure_matches": _payload(molfp, "substructure_matches", {"query": kekule}),
        "workup_precedent": _payload(rxnfp, "workup_precedent", {"reagent_smiles": kekule}),
    }
    for tool, payload in answers.items():
        assert payload["hits"] == [], f"{tool}: the precondition — nothing indexed draws it"
        verdict = _verdict(payload)
        # The citation-only clause leads, so a reader stopping at the first sentence cannot
        # take the indexed negative for the answer.
        assert verdict.startswith(("NOT SEARCHED", "NO PRECEDENT IN THE LABELLED CORPUS — BUT")), (
            tool,
            verdict,
        )
        assert cited in verdict and "expand_note" in verdict, (tool, verdict)
        assert "Do NOT report that no in-house precedent exists" in verdict, (tool, verdict)

    # The similarity tools find the drawn analog, and still say the run outside the index exists.
    reaction = f"{kekule}.{_PARTNER_SMILES}>>{_PRODUCT_SMILES}"
    for server, tool, arguments in (
        (molfp, "similar_molecules", {"smiles": kekule}),
        (rxnfp, "similar_reactions", {"reaction_smiles": reaction}),
    ):
        payload = _payload(server, tool, arguments)
        assert payload["unsearched"]["giving_query"] == 1, (tool, payload["unsearched"])
        assert cited in _verdict(payload), (tool, _verdict(payload))


def test_a_complete_coverage_says_it_counts_structured_records_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "COMPLETE" coverage is scoped to the label index, and the tier's count is said beside it.

    `reagent_frequency` asks about no structure, so it can only count — and must not name a record
    it never checked.
    """
    stores = _synced_and_labelled(tmp_path)
    _molfp, rxnfp = _tools_over(stores, monkeypatch)

    coverage = _payload(rxnfp, "reagent_frequency", {})["coverage"]
    assert coverage["labelled"] == coverage["total"] == 1
    assert coverage["unsearched"] == {
        "outside_index": 1,
        "query_checked": False,
        "giving_query": 0,
        "examples": [],
        "verdict": coverage["unsearched"]["verdict"],
    }
    assert coverage["verdict"].startswith("COMPLETE: all 1 matching reaction(s) in the label index")
    assert "structured records" in coverage["verdict"]
    assert "NOT SEARCHED: 1 citation-only ELN record(s)" in coverage["verdict"]
    assert "suzuki-cited" not in coverage["verdict"]


def test_a_query_no_citation_only_record_draws_is_counted_and_qualified_not_cited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the count is still said, no record is named, and the check's blind side is."""
    stores = _synced_and_labelled(tmp_path)
    molfp, rxnfp = _tools_over(stores, monkeypatch)

    payload = _payload(rxnfp, "substrate_precedent", {"smiles": _BROMOQUINOLINE})
    assert payload["hits"], "the precondition: the drawn control is a labelled precedent"
    verdict = _verdict(payload)
    assert "NOT SEARCHED: 1 citation-only" in verdict
    assert "None of them lists the queried structure" in verdict and "text check" in verdict
    assert "suzuki-cited" not in verdict

    empty = _payload(molfp, "substructure_matches", {"query": "c1ccc2[nH]ccc2c1"})  # indole
    assert empty["hits"] == []
    # Indexed answer first when no citation-only record is named, now scoped to what it covered.
    assert empty["verdict"].startswith("No indexed molecule matched this query.")
    assert "genuine negative result over the indexed records" in empty["verdict"]


def test_only_a_citable_citation_only_record_is_counted() -> None:
    """A withdrawn record is not one to cite, and a structured record is inside the index."""
    store = InMemoryReactionRecordStore()
    cited = record_from_ord_reaction(_map(_flow_suzuki("cited")))
    withdrawn = record_from_ord_reaction(_map(_flow_suzuki("withdrawn"))).model_copy(
        update={"retracted_at": datetime(2024, 1, 1, tzinfo=UTC)}
    )
    drawn = record_from_ord_reaction(_map(_drawn_control("drawn")))
    asyncio.run(store.record([cited, withdrawn, drawn], _SOURCE))

    found = asyncio.run(store.citation_only(_QUINOLINE))
    assert (found.outside_index, found.giving_query) == (1, 1)
    assert found.examples == [note_id_for_reaction("cited", _SOURCE)]
    # Named-only species are not structures, so their text is never a match.
    assert asyncio.run(store.citation_only(_PARTNER)).giving_query == 0
    assert asyncio.run(store.citation_only(None)).query_checked is False


async def test_the_durable_store_answers_the_disclosure_as_the_reference_does() -> None:
    """`114`'s read against Postgres, differential to the in-memory oracle, by delta.

    The session's schema is shared, so other tests' rows are counted too: the assertion is on what
    this test's rows add. A ring-bond `%10` in a stored SMILES pins `strpos` over `LIKE`, where it
    would be a wildcard matching any text.
    """
    await migrated_db_or_skip()
    store = PostgresReactionRecordStore()
    source = "disclosure-test-source"
    query = "C%10CCC(Cl)CC%10"  # chlorocyclohexane with a two-digit ring bond, spelled as stored
    payload = _flow_suzuki("disc-cited")
    payload["inputs"]["quinoline"]["components"][0]["identifiers"] = _identifiers(query, None)
    cited = record_from_ord_reaction(_map(payload))
    assert f"- `{query}` (" in cited.body, "the precondition: the source's spelling is rendered"
    withdrawn = cited.model_copy(
        update={"reaction_id": "disc-withdrawn", "retracted_at": datetime(2024, 1, 1, tzinfo=UTC)}
    )
    drawn = record_from_ord_reaction(_map(_drawn_control("disc-drawn")))

    before = await store.citation_only(query)
    await store.record([cited, withdrawn, drawn], source)
    after = await store.citation_only(query)

    assert after.outside_index - before.outside_index == 1
    assert after.giving_query - before.giving_query == 1
    assert note_id_for_reaction("disc-cited", source) in after.examples
    assert after.query_checked is True
    # `%` as a LIKE wildcard would have matched every record: here it matches only its own text.
    wildcard = await store.citation_only("C%10")
    assert note_id_for_reaction("disc-cited", source) not in wildcard.examples
