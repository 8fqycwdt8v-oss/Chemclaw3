"""Integration test for the Postgres reaction fingerprint store (plan step 3.4).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the reaction table + generic backend rank DRFP Tanimoto neighbors in SQL.
"""

import asyncio

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions, record_for_reaction
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
    PostgresFingerprintStore,
)
from tests.pg import migrated_db_or_skip

_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_ESTER_PROPYL = "CCCO.CC(=O)O>>CCCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


async def _store_or_skip() -> PostgresFingerprintStore:
    """Return a migrated Postgres reaction store, or skip if no database is reachable.

    `source_keyed=True` is not decoration: since `063` the table's primary key is `(source, id)`,
    so a store constructed without it writes `ON CONFLICT (id)` and Postgres refuses the statement
    outright ("there is no unique or exclusion constraint matching the ON CONFLICT specification").
    That is the same refusal the ADR's rollback section describes, reached here from the other
    direction — which is why the flag has to be a constructor argument rather than a default.
    """
    await migrated_db_or_skip()
    return PostgresFingerprintStore(
        "reaction_fingerprints",
        settings.drfp_bits,
        reaction_definition(),
        source_keyed=True,
    )


def test_reaction_similarity_ranking_in_sql() -> None:
    """The SQL backend ranks DRFP Tanimoto neighbors most-similar-first, honoring threshold."""

    async def _run() -> None:
        store = await _store_or_skip()
        for rid, rxn in [
            ("pg-ethyl", _ESTER_ETHYL),
            ("pg-propyl", _ESTER_PROPYL),
            ("pg-halogenation", _HALOGENATION),
        ]:
            await store.add(record_for_reaction(rid, rxn))

        hits = (await find_similar_reactions(store, _ESTER_ETHYL, top_k=2, threshold=0.1)).hits
        assert hits[0].id == "pg-ethyl"
        assert hits[0].similarity == pytest.approx(1.0)
        assert "pg-halogenation" not in {h.id for h in hits}

    asyncio.run(_run())


def test_an_unbuilt_reaction_index_says_so_over_postgres() -> None:
    """The live-run defect's own backend: an index with nothing searchable must report it.

    Pinned to a definition nothing was indexed under, so the assertion holds against the shared
    CI database — and mirrors the real state it stands in for, a table never backfilled.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        orphaned = PostgresFingerprintStore(
            "reaction_fingerprints",
            settings.drfp_bits,
            "drfp:never-indexed:b2048",
            source_keyed=True,
        )
        assert await orphaned.is_empty() is True
        assert await orphaned.count() == 0

        search = await find_similar_reactions(orphaned, _ESTER_ETHYL, threshold=0.1)
        assert search.hits == []
        assert search.index_empty is True
        assert "SEARCH NOT RUN" in search.model_dump()["verdict"]

        current = await _store_or_skip()
        await current.add(record_for_reaction("pg-empty-guard", _ESTER_PROPYL))
        assert await current.is_empty() is False
        assert await current.count() >= 1
        assert (
            await find_similar_reactions(current, _ESTER_PROPYL, threshold=0.1)
        ).index_empty is False

    asyncio.run(_run())


# --- one entry id, two ELNs ----------------------------------------------------------------------


def _sited(reaction_id: str, source: str, reaction_smiles: str) -> FingerprintRecord:
    """One site's fingerprint of an entry id both sites happen to use."""
    return record_for_reaction(reaction_id, reaction_smiles).model_copy(update={"source": source})


async def _rows(reaction_id: str) -> list[tuple[str, str]]:
    """Every `(source, label)` the table holds for one entry id, straight from SQL.

    Asked of the table rather than of the store, because what this migration changes *is* the
    table's key: a store method that returned one row per id would report success by construction.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT source, label FROM reaction_fingerprints WHERE id = %s ORDER BY source",
                (reaction_id,),
            )
            return [(row[0], row[1]) for row in await cur.fetchall()]


def test_two_sources_sharing_an_entry_id_keep_two_rows_in_postgres() -> None:
    """The primary key and the `ON CONFLICT` target are the deployment's half of the rule.

    Measured before `063`, this scenario left **one** row — site B's bromination — and searching
    the index for site A's own esterification returned no hits under the verdict "this is a genuine
    negative result". The transcription tier already kept both rows (D-2026-08-26); the structural
    index is what made one site's chemistry disappear.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(_sited("pg-shared-1001", "pg-eln-a", _ESTER_ETHYL))
        await store.add(_sited("pg-shared-1001", "pg-eln-b", _HALOGENATION))

        assert await _rows("pg-shared-1001") == [
            ("pg-eln-a", _ESTER_ETHYL),
            ("pg-eln-b", _HALOGENATION),
        ]

    asyncio.run(_run())


def test_a_postgres_hit_cites_the_source_it_matched() -> None:
    """Each site's reaction finds its own row, and the two spell two citations.

    The half that could not be answered before: a hit is what a citation is built from, and with
    one row behind two runs the search had no source to hand `note_id_for_reaction`.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(_sited("pg-cite-1001", "pg-eln-a", _ESTER_ETHYL))
        await store.add(_sited("pg-cite-1001", "pg-eln-b", _HALOGENATION))

        cited = {}
        for smiles, expected in ((_ESTER_ETHYL, "pg-eln-a"), (_HALOGENATION, "pg-eln-b")):
            hits = [
                h
                for h in (await find_similar_reactions(store, smiles, threshold=0.99)).hits
                if h.id == "pg-cite-1001"
            ]
            assert [h.source for h in hits] == [expected], f"{smiles} cited the wrong site"
            cited[expected] = note_id_for_reaction(hits[0].id, hits[0].source)

        assert len(set(cited.values())) == 2, f"two runs, one citation: {cited}"

    asyncio.run(_run())


def test_a_single_source_deployment_reads_exactly_as_before() -> None:
    """One enabled ELN: one row per entry id, amended in place, cited by the bare note id."""

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(_sited("pg-solo-1001", "pg-eln-a", _ESTER_ETHYL))
        await store.add(_sited("pg-solo-1001", "pg-eln-a", _ESTER_PROPYL))

        assert await _rows("pg-solo-1001") == [("pg-eln-a", _ESTER_PROPYL)]
        hits = (await find_similar_reactions(store, _ESTER_PROPYL, threshold=0.99)).hits
        assert "pg-solo-1001" in {h.id for h in hits}
        assert note_id_for_reaction("pg-solo-1001") == "reaction-pg-solo-1001"

    asyncio.run(_run())


def test_a_sourced_write_deletes_the_unsourced_row_063_left_behind() -> None:
    """Migration `063` leaves some rows under `''`; a re-ingest must replace, not duplicate.

    Two rows with identical bits and one label are two *hits*, so a similarity search would report
    two precedents where a chemist has one experiment — which is why the write path supersedes
    rather than leaving the leftovers to a runbook step.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(record_for_reaction("pg-legacy-1001", _ESTER_ETHYL))
        assert await _rows("pg-legacy-1001") == [("", _ESTER_ETHYL)]

        await store.add(_sited("pg-legacy-1001", "pg-eln-a", _ESTER_ETHYL))
        assert await _rows("pg-legacy-1001") == [("pg-eln-a", _ESTER_ETHYL)]

        hits = [
            h
            for h in (await find_similar_reactions(store, _ESTER_ETHYL, threshold=0.99)).hits
            if h.id == "pg-legacy-1001"
        ]
        assert len(hits) == 1, f"one experiment indexed twice: {hits}"

    asyncio.run(_run())


def test_the_molecule_index_refuses_a_sourced_record() -> None:
    """A structure's id is global, so `molecule_fingerprints` has no source column — and says so.

    Written against the molecule table rather than asserted about it: without the refusal the
    source silently never reaches the database, which is the half-write this key change is about.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        molecules = PostgresFingerprintStore(
            "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
        )
        sourced = record_for_reaction("pg-mol-guard", _ESTER_ETHYL).model_copy(
            update={"source": "pg-eln-a"}
        )
        with pytest.raises(FingerprintError, match="not keyed by source"):
            await molecules.add(sourced)

    asyncio.run(_run())
