"""The citation-only tier's label row: what it holds, who reads it, and who must never touch it.

`D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier`: a record whose source named a
species without its structure has a label row of the record phase only. The facet questions keyed on
a drawn species or a role count it in a tier of its own; the labeller never reads it; a facet keyed
on a derived or fingerprint key never reaches it, even when a stale fingerprint names its key.
Every backend assertion runs on the in-memory reference and on Postgres.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tenancy import bound_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from chemclaw.ingest.eln.ord import RecordTier
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import PostgresReactionRecordStore
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.ingest.labels.record import record_phase
from chemclaw.science.labels.facets import Facet
from chemclaw.science.labels.records import LabelTier, ReactionLabel, SpeciesLabel
from chemclaw.science.labels.search import agent_frequency, substrate_precedents
from chemclaw.science.labels.store import (
    InMemoryLabelIndex,
    LabelIndex,
    LabelIndexError,
    PostgresLabelIndex,
)
from chemclaw.science.labels.vocabulary import SpeciesRole
from tests.tenancy_db import enable_tenancy, tenancy_database
from tests.test_label_index import _derived, _label, _postgres_or_skip
from tests.test_ord_citation_tier import _Exporting, _flow_suzuki, _map, _Stores

_VERSION = "rxnlabel@1:roles1"
_SOURCE = "citation-tier-test"
_QUINOLINE = "Clc1ccc2ncccc2c1"
_BASE = "[Na+].[OH-]"


def _cited(reaction_id: str, *, yield_percent: float = 40.0) -> ReactionLabel:
    """A citation-only record phase: the drawn quinoline and a base, no reaction string."""
    return ReactionLabel(
        source=_SOURCE,
        reaction_id=reaction_id,
        tier=LabelTier.CITATION_ONLY,
        citation=f"reaction-{reaction_id}",
        yield_percent=yield_percent,
        species=[
            SpeciesLabel(
                ordinal=0,
                smiles=_QUINOLINE,
                role="reactant",
                derived_role=SpeciesRole.STARTING_MATERIAL,
            ),
            SpeciesLabel(ordinal=1, smiles=_BASE, role="reagent", derived_role=SpeciesRole.REAGENT),
        ],
    )


def _structured(reaction_id: str) -> ReactionLabel:
    """The shared Buchwald-shaped record phase, in this module's own source."""
    return _label(reaction_id).model_copy(update={"source": _SOURCE})


def _both_backends(body: Callable[[LabelIndex, str], Awaitable[None]]) -> None:
    """Run `body` on the in-memory reference and then on Postgres, on distinct keys."""

    async def _run() -> None:
        await body(InMemoryLabelIndex(), "mem")
        await body(await _postgres_or_skip(), "pg")

    asyncio.run(_run())


# --- the model --------------------------------------------------------------------------------


def test_the_label_tier_spells_the_record_tier() -> None:
    """`science/` cannot import `RecordTier`, so its twin is held equal here."""
    assert {tier.value for tier in LabelTier} == {tier.value for tier in RecordTier}


def test_a_citation_only_row_holds_nothing_derived_and_a_structured_row_a_reaction_string() -> None:
    """The invariant the table's CHECK and the labeller's scan rely on, refused at the model."""
    _cited("ok")
    with pytest.raises(ValidationError, match="holds no reaction string"):
        ReactionLabel.model_validate({**_cited("bad").model_dump(), "record_smiles": "C>>C"})
    with pytest.raises(ValidationError, match="holds no reaction string"):
        ReactionLabel.model_validate({**_cited("bad").model_dump(), "named_reaction": "Suzuki"})
    with pytest.raises(ValidationError, match="needs its record_smiles"):
        ReactionLabel(source=_SOURCE, reaction_id="bare", citation="reaction-bare")


def test_the_record_phase_of_a_citation_only_reaction_keeps_drawn_species_and_source_role() -> None:
    """From the production mapping: no reaction string, the drawn species only, the coarse role.

    The named-only partner and product have no row, and the base is a plain `reagent`: the label
    never claims what the structure could have told.
    """
    cited = record_phase(_map(_flow_suzuki("rp-cited")), _SOURCE)
    assert cited.tier is LabelTier.CITATION_ONLY and cited.record_smiles is None
    assert [(s.smiles, s.role, s.derived_role) for s in cited.species] == [
        (_QUINOLINE, "reactant", SpeciesRole.STARTING_MATERIAL),
        ("CC(=O)[O-].CC(=O)[O-].[Pd+2]", "catalyst", SpeciesRole.CATALYST),
        (_BASE, "reagent", SpeciesRole.REAGENT),
    ]
    assert cited.yield_percent == 4.76
    structured = record_phase(
        _map(_flow_suzuki("rp-drawn", partner_smiles="OB(O)c1ccccc1", product_smiles="c1ccccc1")),
        _SOURCE,
    )
    assert structured.tier is LabelTier.STRUCTURED and structured.record_smiles is not None
    assert all(s.derived_role is None for s in structured.species), "structured: nothing looked yet"


# --- the index, on both backends --------------------------------------------------------------


def test_a_citation_only_row_round_trips_and_is_never_offered_to_the_labeller() -> None:
    """Stored with its roles and no reaction string; stale, store_labels and coverage skip it."""

    async def _body(index: LabelIndex, tag: str) -> None:
        cited, drawn = f"{tag}-cit", f"{tag}-drawn"
        await index.record(_cited(cited))
        await index.record(_structured(drawn))
        offered = {r.reaction_id for r in await index.stale(_VERSION, 500, sources=[_SOURCE])}
        assert drawn in offered and cited not in offered
        with pytest.raises(LabelIndexError, match="citation-only|structured"):
            await index.store_labels(_derived(_structured(cited)), _VERSION)

        selection = await index.select(Facet(species_smiles=_QUINOLINE), _VERSION, 500)
        [row] = [r for r in selection.rows if r.reaction_id == cited]
        assert row.tier is LabelTier.CITATION_ONLY and row.record_smiles is None
        assert row.labeller_version is None and row.named_reaction is None
        assert [(s.smiles, s.derived_role) for s in row.species] == [
            (_QUINOLINE, SpeciesRole.STARTING_MATERIAL),
            (_BASE, SpeciesRole.REAGENT),
        ]
        # The structured denominator excludes the tier; the tier is its own count.
        whole = await index.coverage(_VERSION)
        assert whole.citation_only == 0 and whole.citation_tier_searched is False
        assert selection.coverage.citation_tier_searched is True
        assert selection.coverage.citation_only >= 1
        assert (await index.citation_rows([(_SOURCE, cited), (_SOURCE, drawn)])) == {
            (_SOURCE, cited)
        }

    _both_backends(_body)


def test_a_facet_keyed_on_a_derived_or_fingerprint_key_never_reaches_the_tier() -> None:
    """Even with the row's key handed over by a stale fingerprint, no derived question is answered.

    The reaction-key probe is the negative one: an entry amended to citation-only keeps its old
    fingerprint, so a similarity pre-pass can name the key of a row in the tier. The control is the
    same key in the same facet shape asking for the species alone.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        cited = f"{tag}-keyed"
        await index.record(_cited(cited))
        key = (_SOURCE, cited)
        reach = await index.select(
            Facet(species_smiles=_QUINOLINE, sources=frozenset({_SOURCE})), _VERSION, 500
        )
        assert cited in {r.reaction_id for r in reach.rows}, "the control: the tier is reachable"
        for facet in (
            Facet(reaction_keys=frozenset({key})),
            Facet(reaction_keys=frozenset({key}), species_smiles=_QUINOLINE),
            Facet(named_reaction="Suzuki coupling", species_smiles=_QUINOLINE),
            Facet(product_smiles=frozenset({_QUINOLINE})),
        ):
            selection = await index.select(facet, _VERSION, 500)
            assert cited not in {r.reaction_id for r in selection.rows}, facet
            assert selection.coverage.citation_tier_searched is False
            assert selection.coverage.citation_only == 0, "out of scope, so not a denominator"

    _both_backends(_body)


def test_a_tally_splits_every_figure_by_tier() -> None:
    """One structured and two citation-only runs: the counts add up and never share a median."""

    async def _body(index: LabelIndex, tag: str) -> None:
        scope = f"{tag}-split"
        facet = Facet(sources=frozenset({scope}))
        structured = _label(f"{tag}-s").model_copy(update={"source": scope, "yield_percent": 90.0})
        await index.record(structured)
        await index.store_labels(_derived(structured), _VERSION)
        for rid, yield_percent in ((f"{tag}-c1", 10.0), (f"{tag}-c2", 30.0)):
            cited = _cited(rid, yield_percent=yield_percent)
            await index.record(cited.model_copy(update={"source": scope}))
        report = await index.agent_counts(facet, _VERSION, frozenset(), 500)
        assert (report.structured_reactions, report.citation_only_reactions) == (1, 2)
        assert report.reactions_in_scope == 3
        [solvent] = [a for a in report.agents if a.role is SpeciesRole.SOLVENT]
        assert (solvent.structured_count, solvent.citation_only_count) == (1, 0)
        assert solvent.median_yield_percent == 90.0
        [quinoline] = [a for a in report.agents if a.smiles == _QUINOLINE]
        assert (quinoline.structured_count, quinoline.citation_only_count) == (0, 2)
        assert quinoline.median_yield_percent is None
        assert quinoline.citation_only_median_yield_percent == 20.0
        assert quinoline.count == 2
        verdict = report.verdict
        assert "1 labelled structured and 2 citation-only" in verdict
        assert report.coverage.citation_only == 2

    _both_backends(_body)


def test_an_entry_changing_tier_is_re_recorded_from_nothing() -> None:
    """Structured to citation-only to structured: no derived phase and no refined role survives.

    A role a labeller refined for the structured record must not be read as the source's own word,
    and the source's own word must not stop the labeller refining the structured record again
    (`merge` derives roles only where none is held).
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        rid = f"{tag}-moves"
        await index.record(_structured(rid))
        await index.store_labels(_derived(_structured(rid)), _VERSION)

        # The same bromobenzene at the same ordinal, so only a reset can tell the two records apart.
        bromobenzene = SpeciesLabel(
            ordinal=0,
            smiles="Brc1ccccc1",
            role="reactant",
            derived_role=SpeciesRole.STARTING_MATERIAL,
        )
        await index.record(_cited(rid).model_copy(update={"species": [bromobenzene]}))
        [row] = [
            r
            for r in (await index.select(Facet(species_smiles="Brc1ccccc1"), _VERSION, 500)).rows
            if r.reaction_id == rid
        ]
        assert row.tier is LabelTier.CITATION_ONLY
        assert row.named_reaction is None and row.rxno_id is None and row.mapped_smiles is None
        assert len(row.species) == 1, "the structured record's other species are gone"
        assert row.species[0].scaffold is None and row.species[0].functional_groups == []
        coverage = await index.coverage(_VERSION, [(_SOURCE, rid)])
        assert (coverage.labelled, coverage.total) == (0, 0), "no longer a structured row"

        await index.record(_structured(rid))
        stale = {r.reaction_id: r for r in await index.stale(_VERSION, 500, sources=[_SOURCE])}
        assert rid in stale, "structured again, so the labeller is offered it from nothing"
        assert all(s.derived_role is None for s in stale[rid].species)

    _both_backends(_body)


def test_a_facet_function_counts_the_tier_through_the_search_entry_points() -> None:
    """`substrate_precedents` and `agent_frequency` reach the tier, whatever the query spelling."""

    async def _body(index: LabelIndex, tag: str) -> None:
        rid = f"{tag}-entry"
        await index.record(_cited(rid))
        kekule = "ClC1=CC2=CC=CN=C2C=C1"
        found = await substrate_precedents(index, _VERSION, kekule)
        assert rid in {h.reaction_id for h in found.hits}
        [hit] = [h for h in found.hits if h.reaction_id == rid]
        assert hit.tier is LabelTier.CITATION_ONLY and hit.reaction_smiles is None
        assert set(hit.agents) == {"reagent"}
        report = await agent_frequency(index, _VERSION, roles=frozenset({SpeciesRole.LIGAND}))
        assert all(a.citation_only_count == 0 for a in report.agents), "a ligand filter is refined"

    _both_backends(_body)


# --- the sync ----------------------------------------------------------------------------------


def test_a_replayed_citation_only_entry_is_written_once_when_it_has_no_label_row() -> None:
    """An entry stored before the tier had rows is written on its next replay, then skipped.

    The body is byte-identical, which skips a structured replay; the missing row is what a
    citation-only one is also asked for. The first replay is the negative probe (its label row is
    absent and its record is stored), the second the control (nothing left to write).
    """
    stores = _Stores()
    payload = _flow_suzuki("replayed")
    stored = record_from_ord_reaction(_map(payload))
    asyncio.run(stores.records.record([stored], "eln-ord"))
    assert asyncio.run(stores.labels.count()) == 0
    since = datetime(2024, 1, 1, tzinfo=UTC)

    async def _replay() -> Any:
        return await sync_entries(
            _Exporting((payload, None)),
            stores.reactions,
            stores.molecules,
            stores.records,
            since,
            label_index=stores.labels,
            source="eln-ord",
            apply_overlap=False,
        )

    first = asyncio.run(_replay())
    assert (first.ingested, first.skipped_existing) == (["replayed"], [])
    assert first.citation_only == ["replayed"]
    assert asyncio.run(stores.labels.count()) == 1
    second = asyncio.run(_replay())
    assert (second.ingested, second.skipped_existing) == ([], ["replayed"])
    assert asyncio.run(stores.reactions.all_records()) == [], "and no fingerprint at either pass"


# --- tenancy -----------------------------------------------------------------------------------


async def test_a_citation_only_row_is_one_tenants_and_not_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row and its species are tenant-scoped like their sibling label tables.

    Two tenants record the same key through the store as a role the policies bind: both rows exist,
    each tenant's facet counts its own and the other's tier is invisible to it.
    """
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    forget_registered_tenants()
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        index = PostgresLabelIndex()
        for tenant, yield_percent in (("acme", 11.0), ("globex", 22.0)):
            with bound_tenant(tenant):
                await index.record(_cited("shared-key", yield_percent=yield_percent))
        with bound_tenant("globex"):
            await index.record(_cited("only-globex"))

        seen: dict[str, list[tuple[str, float | None]]] = {}
        for tenant in ("acme", "globex"):
            with bound_tenant(tenant):
                selection = await index.select(Facet(species_smiles=_QUINOLINE), _VERSION, 50)
                seen[tenant] = [(r.reaction_id, r.yield_percent) for r in selection.rows]
                assert selection.coverage.citation_only == len(selection.rows)
                assert await index.citation_rows(
                    [(_SOURCE, "shared-key"), (_SOURCE, "only-globex")]
                ) == {(_SOURCE, r) for r, _ in seen[tenant]}
        assert seen == {
            "acme": [("shared-key", 11.0)],
            "globex": [("only-globex", 40.0), ("shared-key", 22.0)],
        }
        with bound_tenant("acme"):
            species = await index.select(Facet(), _VERSION, 50)
            assert [len(r.species) for r in species.rows] == [2], "no species of the other tenant"


# --- the migration -----------------------------------------------------------------------------


async def test_migration_136_derives_the_flag_and_clears_what_a_rolled_back_image_derived() -> None:
    """The block's rule, driven by the migration's own statements over rows in every wrong state.

    The block is also the documented restore after a rollback, so it must both set and clear, and
    must leave a citation-only row with nothing derived.
    """
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    records, labels = PostgresReactionRecordStore(), PostgresLabelIndex()
    source = "bf136-source"
    half = record_from_ord_reaction(_map(_flow_suzuki("bf136-half")))
    pulled = record_from_ord_reaction(_map(_flow_suzuki("bf136-pulled"))).model_copy(
        update={"retracted_at": datetime(2024, 1, 1, tzinfo=UTC)}
    )
    left = record_from_ord_reaction(_map(_flow_suzuki("bf136-left")))
    current = record_from_ord_reaction(
        _map(_flow_suzuki("bf136-current", partner_smiles="OB(O)c1ccccc1", product_smiles="CC"))
    )
    assert (half.tier, current.tier) == (RecordTier.CITATION_ONLY, RecordTier.STRUCTURED)
    await records.record([half, pulled, left, current], source)
    for rid, flag in (("bf136-half", True), ("bf136-pulled", False)):
        await labels.record(_cited(rid).model_copy(update={"source": source, "withheld": flag}))
    # An entry now citation-only whose label row is still the structured one it had.
    await labels.record(_label("bf136-left").model_copy(update={"source": source}))
    await labels.record(_label("bf136-current").model_copy(update={"source": source}))
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE reaction_labels SET named_reaction = 'junk', labeller_version = 'old', "
            "labelled_at = now() WHERE source = %s AND reaction_id = 'bf136-half'",
            (source,),
        )
        await conn.commit()

    migration = Path(__file__).parents[1] / "infra" / "sql" / "136_reaction_label_tier.sql"
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(migration.read_text(encoding="utf-8"))
        cursor = await conn.execute(
            "SELECT reaction_id, tier, withheld, named_reaction, labeller_version "
            "FROM reaction_labels WHERE source = %s ORDER BY reaction_id",
            (source,),
        )
        rows = await cursor.fetchall()
        await conn.commit()

    assert rows == [
        ("bf136-current", "structured", False, None, None),
        ("bf136-half", "citation-only", False, None, None),
        ("bf136-left", "structured", True, None, None),
        ("bf136-pulled", "citation-only", True, None, None),
    ]


async def test_the_table_refuses_a_row_whose_tier_and_reaction_string_disagree() -> None:
    """The CHECK holds the invariant for a writer that bypasses the model."""
    import psycopg

    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    async with db.connection(settings.postgres_dsn) as conn:
        for tier, record_smiles in (("structured", None), ("citation-only", "C>>C")):
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO reaction_labels (source, reaction_id, tier, record_smiles, "
                    "citation) VALUES ('chk136', %s, %s, %s, 'c')",
                    (f"r-{tier}", tier, record_smiles),
                )
            await conn.rollback()
