"""The reaction-label index: the two-phase write, staleness as a query, and coverage.

Every test runs against both backends with the same body: the in-memory one is the reference, and
rules such as what a re-ingest may clobber or what `IS DISTINCT FROM NULL` finds are easy to get
right in Python and wrong in SQL. The Postgres half skips without a database (`tests/pg.py`).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.store import (
    InMemoryLabelIndex,
    LabelIndex,
    LabelIndexError,
    PostgresLabelIndex,
    underived_stamp,
)
from chemclaw.science.labels.vocabulary import SpeciesRole
from tests.pg import migrated_db_or_skip

_VERSION = "rxnlabel@1:roles1"
_SOURCE = "test-corpus"


def _label(reaction_id: str = "r1", *, smiles: str | None = None) -> ReactionLabel:
    """A Buchwald-shaped record phase: two substrates, a catalyst, a ligand-ish agent, a product."""
    record = smiles or "Brc1ccccc1.NC1CCCCC1>CC(C)(C)P(C(C)(C)C)C(C)(C)C.CC#N>c1ccc(NC2CCCCC2)cc1"
    return ReactionLabel(
        source=_SOURCE,
        reaction_id=reaction_id,
        record_smiles=record,
        citation=f"reaction-{reaction_id}",
        performed_on=date(2026, 5, 4),
        temperature_c=100.0,
        time_h=16.0,
        yield_percent=78.0,
        workup_text="Quenched with water and extracted with EtOAc.",
        species=[
            SpeciesLabel(ordinal=0, smiles="Brc1ccccc1", role="reactant"),
            SpeciesLabel(ordinal=1, smiles="NC1CCCCC1", role="reactant"),
            SpeciesLabel(ordinal=2, smiles="CC#N", role="solvent"),
            SpeciesLabel(ordinal=3, smiles="c1ccc(NC2CCCCC2)cc1", role="product"),
        ],
    )


def _derived(label: ReactionLabel) -> ReactionLabel:
    """The same reaction with a full derived phase, as a labeller would hand it back."""
    return label.model_copy(
        update={
            "mapped_smiles": "[Br:1]c1ccccc1>>c1ccc(N)cc1",
            "named_reaction": "Buchwald-Hartwig amination",
            "reaction_class": "Heteroatom alkylation and arylation",
            "rxno_id": "RXNO:0000192",
            "confidence": 0.94,
            "method": "smirks",
            "species": [
                label.species[0].model_copy(
                    update={"derived_role": SpeciesRole.STARTING_MATERIAL, "scaffold": "c1ccccc1"}
                ),
                label.species[1].model_copy(update={"derived_role": SpeciesRole.STARTING_MATERIAL}),
                label.species[2].model_copy(update={"derived_role": SpeciesRole.SOLVENT}),
                label.species[3].model_copy(
                    update={
                        "derived_role": SpeciesRole.PRODUCT,
                        "functional_groups": ["secondary amine"],
                    }
                ),
            ],
        }
    )


async def _postgres_or_skip() -> PostgresLabelIndex:
    """A migrated Postgres index, or a skip when no database is reachable."""
    await migrated_db_or_skip()
    return PostgresLabelIndex()


def _both_backends(body: Callable[[LabelIndex, str], Awaitable[None]]) -> None:
    """Run `body` against the in-memory backend and then against Postgres, on distinct keys.

    The durable index is shared with every other test in the schema.
    """

    async def _run() -> None:
        await body(InMemoryLabelIndex(), "mem")
        await body(await _postgres_or_skip(), "pg")

    asyncio.run(_run())


def test_the_record_phase_round_trips_with_its_species() -> None:
    """What was written is what comes back — conditions, workup and every species in order."""

    async def _body(index: LabelIndex, tag: str) -> None:
        await index.record(_label(f"{tag}-round-trip"))
        [stored] = await index.stale(_VERSION, limit=50, sources=[_SOURCE])
        assert stored.reaction_id == f"{tag}-round-trip"
        # The record form, agents kept: the whole reason this row exists beside the fingerprint.
        assert ">CC(C)(C)P" in stored.record_smiles
        assert stored.workup_text is not None and "EtOAc" in stored.workup_text
        assert stored.yield_percent == 78.0
        assert [s.smiles for s in stored.species] == [
            "Brc1ccccc1",
            "NC1CCCCC1",
            "CC#N",
            "c1ccc(NC2CCCCC2)cc1",
        ]
        # Nothing derived yet, and that is a different state from "derived as unknown".
        assert stored.labeller_version is None
        assert all(s.derived_role is None for s in stored.species)

    _both_backends(_body)


def test_a_row_that_was_never_derived_is_stale() -> None:
    """NULL is the commonest stale value on a fresh corpus, so `<>` would have missed all of it."""

    async def _body(index: LabelIndex, tag: str) -> None:
        await index.record(_label(f"{tag}-never"))
        stale = await index.stale(_VERSION, limit=50, sources=[_SOURCE])
        assert f"{tag}-never" in {row.reaction_id for row in stale}

    _both_backends(_body)


def test_labelling_stamps_the_row_out_of_the_stale_set_and_a_version_bump_puts_it_back() -> None:
    """The whole background service in three lines: derive, stamp, and re-stale on a bump.

    This is what makes "as soon as entries are identified that miss these things" a query rather
    than a flag — nothing marks anything, and a labeller upgrade re-opens the corpus by itself.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        label = _label(f"{tag}-stamp")
        await index.record(label)
        await index.store_labels(_derived(label), _VERSION)

        stale = await index.stale(_VERSION, limit=50, sources=[_SOURCE])
        assert f"{tag}-stamp" not in {row.reaction_id for row in stale}

        [stored] = [
            row
            for row in await index.stale("rxnlabel@2:roles1", limit=50, sources=[_SOURCE])
            if row.reaction_id == f"{tag}-stamp"
        ]
        assert stored.named_reaction == "Buchwald-Hartwig amination"
        assert stored.rxno_id == "RXNO:0000192"
        assert stored.species[2].derived_role is SpeciesRole.SOLVENT
        assert stored.species[3].functional_groups == ["secondary amine"]

    _both_backends(_body)


def test_a_derived_phase_is_paired_to_its_species_by_ordinal_in_both_backends() -> None:
    """A derived phase is paired to its species by ordinal in both backends.

    `PostgresLabelIndex` matches `ordinal`; pairing by list position would disagree when a labeller
    returns species in another order, and treat a short answer differently.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        label = _label(f"{tag}-reordered")
        await index.record(label)
        derived = _derived(label)
        await index.store_labels(
            derived.model_copy(update={"species": list(reversed(derived.species))}), _VERSION
        )

        [stored] = [
            row
            for row in await index.stale("rxnlabel@2:roles1", limit=50, sources=[_SOURCE])
            if row.reaction_id == f"{tag}-reordered"
        ]
        assert [(s.ordinal, s.smiles, s.derived_role) for s in stored.species] == [
            (0, "Brc1ccccc1", SpeciesRole.STARTING_MATERIAL),
            (1, "NC1CCCCC1", SpeciesRole.STARTING_MATERIAL),
            (2, "CC#N", SpeciesRole.SOLVENT),
            (3, "c1ccc(NC2CCCCC2)cc1", SpeciesRole.PRODUCT),
        ]

    _both_backends(_body)


def test_re_ingesting_an_unchanged_reaction_keeps_its_labels() -> None:
    """A note edit must not silently discard a backfill that took days."""

    async def _body(index: LabelIndex, tag: str) -> None:
        label = _label(f"{tag}-unchanged")
        await index.record(label)
        await index.store_labels(_derived(label), _VERSION)

        # Same structures, a corrected yield — the ordinary shape of an ELN amendment.
        await index.record(label.model_copy(update={"yield_percent": 81.0}))

        stale = await index.stale(_VERSION, limit=50, sources=[_SOURCE])
        assert f"{tag}-unchanged" not in {row.reaction_id for row in stale}

    _both_backends(_body)


def test_re_ingesting_a_changed_reaction_drops_its_labels_and_re_stales_it() -> None:
    """An amended `record_smiles` is a different reaction, so its name is about something else."""

    async def _body(index: LabelIndex, tag: str) -> None:
        label = _label(f"{tag}-changed")
        await index.record(label)
        await index.store_labels(_derived(label), _VERSION)

        amended = _label(f"{tag}-changed", smiles="CCO.CC(=O)O>>CCOC(C)=O").model_copy(
            update={
                "species": [
                    SpeciesLabel(ordinal=0, smiles="CCO", role="reactant"),
                    SpeciesLabel(ordinal=1, smiles="CC(=O)O", role="reactant"),
                    SpeciesLabel(ordinal=2, smiles="CCOC(C)=O", role="product"),
                ]
            }
        )
        await index.record(amended)

        [stored] = [
            row
            for row in await index.stale(_VERSION, limit=50, sources=[_SOURCE])
            if row.reaction_id == f"{tag}-changed"
        ]
        assert stored.named_reaction is None
        assert stored.labeller_version is None
        # And the species the amendment removed is gone, not left answering for a flask that is
        # no longer recorded.
        assert [s.smiles for s in stored.species] == ["CCO", "CC(=O)O", "CCOC(C)=O"]
        assert all(s.derived_role is None for s in stored.species)

    _both_backends(_body)


def test_labelling_a_reaction_that_was_never_recorded_is_refused() -> None:
    """The derived phase writes over a record phase; there is nothing to write over here.

    A silent no-op would let a drain report progress it did not make.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        with pytest.raises(LabelIndexError, match="record"):
            await index.store_labels(_derived(_label(f"{tag}-absent")), _VERSION)

    _both_backends(_body)


def test_coverage_counts_the_facets_rows_not_the_corpus() -> None:
    """A count over 3% of the corpus, read as a count over the facet, is a different lie."""

    async def _body(index: LabelIndex, tag: str) -> None:
        labelled = _label(f"{tag}-cov-a")
        await index.record(labelled)
        await index.store_labels(_derived(labelled), _VERSION)
        await index.record(_label(f"{tag}-cov-b"))

        keys = [(_SOURCE, f"{tag}-cov-a"), (_SOURCE, f"{tag}-cov-b")]
        coverage = await index.coverage(_VERSION, keys)
        assert (coverage.labelled, coverage.total) == (1, 2)
        assert coverage.sources == [_SOURCE]
        assert coverage.verdict.startswith("PARTIAL")
        assert "lower bound" in coverage.verdict

        complete = await index.coverage(_VERSION, [(_SOURCE, f"{tag}-cov-a")])
        assert complete.verdict.startswith("COMPLETE")

        none_yet = await index.coverage(_VERSION, [(_SOURCE, f"{tag}-cov-b")])
        assert none_yet.verdict.startswith("NOT ANSWERABLE YET")

        empty = await index.coverage(_VERSION, [])
        assert empty.verdict.startswith("NO ROWS IN SCOPE")

    _both_backends(_body)


def test_the_stale_scan_is_bounded_and_deterministic() -> None:
    """A drain that dies mid-batch resumes on the same rows, in the same order."""

    async def _body(index: LabelIndex, tag: str) -> None:
        for n in range(5):
            await index.record(_label(f"{tag}-order-{n}"))
        first = await index.stale(_VERSION, limit=3, sources=[_SOURCE])
        again = await index.stale(_VERSION, limit=3, sources=[_SOURCE])
        assert len(first) == 3
        assert [r.reaction_id for r in first] == [r.reaction_id for r in again]
        assert [r.reaction_id for r in first] == sorted(r.reaction_id for r in first)

    _both_backends(_body)


async def test_current_version_reads_the_index_and_not_the_whole_corpus() -> None:
    """`current_version()` reaches `reaction_labels_current_version_idx`.

    Shape half, no database: the statement's partial-index predicate and `ORDER BY` must match the
    index `(labelled_at DESC, source, reaction_id) WHERE labelled_at IS NOT NULL`, or every rxnfp
    tool call pays a sequential scan. Only the two ends are pinned; that a filter between them is
    still served is what the plan half checks, with `enable_seqscan = off` so reachability shows at
    any row count.
    """
    assert PostgresLabelIndex._CURRENT_VERSION.startswith(
        "SELECT labeller_version FROM reaction_labels WHERE labelled_at IS NOT NULL"
    ), "the statement no longer matches reaction_labels_current_version_idx (086)'s predicate"
    assert PostgresLabelIndex._CURRENT_VERSION.endswith(
        "ORDER BY labelled_at DESC, source, reaction_id LIMIT 1"
    ), "the statement no longer matches reaction_labels_current_version_idx (086)'s order"

    index = await _postgres_or_skip()
    await index.record(_label("pg-current-version"))
    await index.store_labels(_derived(_label("pg-current-version")), _VERSION)
    assert await index.current_version() is not None
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET LOCAL enable_seqscan = off")
            await cur.execute(f"EXPLAIN (COSTS OFF) {PostgresLabelIndex._CURRENT_VERSION}")
            plan = "\n".join(str(row[0]) for row in await cur.fetchall())
    assert "reaction_labels_current_version_idx" in plan, (
        "current_version() does not reach its index; the plan was:\n" + plan
    )


def test_a_labellers_confidence_survives_the_round_trip_in_both_backends() -> None:
    """A labeller's confidence survives the round trip in both backends.

    `ReactionLabel.confidence` is a double; a `REAL` column would return 0.95 as 0.949999988..., and
    a later `WHERE confidence >= 0.95` would miss it.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        reported = 1 / 3
        await index.record(_label(f"{tag}-confidence"))
        await index.store_labels(
            _derived(_label(f"{tag}-confidence")).model_copy(update={"confidence": reported}),
            _VERSION,
        )
        # By id rather than by unpacking the batch: the durable index is shared with every other
        # test in this schema, so `stale` legitimately answers with more than this row.
        rows = await index.stale("a-later-version", limit=500, sources=[_SOURCE])
        [stored] = [row for row in rows if row.reaction_id == f"{tag}-confidence"]
        assert stored.confidence == reported, (
            f"{tag}: a confidence of {reported!r} came back as {stored.confidence!r}"
        )

    _both_backends(_body)


def test_an_underived_stamp_is_not_currency_in_either_backend() -> None:
    """An underived stamp is not currency in either backend.

    `store_labels(derived=False)` advances `stale()` without letting `coverage` count the row as
    labelled. Both backends, since the marker is a Python set on one side and SQL `IS DISTINCT FROM`
    on the other.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        rid = f"{tag}-underived"
        await index.record(_label(rid))
        await index.store_labels(_derived(_label(rid)), _VERSION)
        await index.store_labels(_derived(_label(rid)), "rxnlabel@2:roles1", derived=False)

        keys = [(_SOURCE, rid)]
        assert (await index.coverage("rxnlabel@2:roles1", keys)).labelled == 0, (
            f"{tag}: an un-derived row was counted as labelled at the version it was stamped for"
        )
        # It still left the stale set, or the drain is wedged on it forever.
        stale = await index.stale("rxnlabel@2:roles1", limit=500, sources=[_SOURCE])
        assert rid not in {row.reaction_id for row in stale}, f"{tag}: the drain did not advance"
        # And it is work again the next time the version moves.
        later = await index.stale("rxnlabel@3:roles1", limit=500, sources=[_SOURCE])
        assert rid in {row.reaction_id for row in later}, f"{tag}: the row can never be re-derived"

    _both_backends(_body)


def test_an_underived_stamp_does_not_become_the_current_version_in_either_backend() -> None:
    """An underived stamp does not become the current version in either backend.

    The marked form is skipped, and `labelled_at` is left alone so a degraded row is outranked by
    any genuinely derived one.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        rid = f"{tag}-current"
        await index.record(_label(rid))
        await index.store_labels(_derived(_label(rid)), f"{tag}-derived-version")
        stamped_at = await _labelled_at(index, rid)

        await index.store_labels(_derived(_label(rid)), f"{tag}-degraded-version", derived=False)
        assert await index.current_version() != f"{tag}-degraded-version", (
            f"{tag}: a version nothing was derived under became the one every tool reads"
        )
        assert await _labelled_at(index, rid) == stamped_at, (
            f"{tag}: an un-derived pass moved labelled_at, so it outranks every derived row"
        )

    _both_backends(_body)


async def _labelled_at(index: LabelIndex, reaction_id: str) -> object:
    """One row's `labelled_at`, read back through the stale query both backends share."""
    rows = await index.stale("no-version-is-this", limit=500, sources=[_SOURCE])
    [row] = [r for r in rows if r.reaction_id == reaction_id]
    return row.labelled_at


def test_a_version_that_already_carries_the_marker_is_refused_in_both_backends() -> None:
    """A version that already carries the marker is refused in both backends.

    `remote` comes from a separately versioned server, so a version ending in the marker would make
    a derived row indistinguishable from an underived one; the write is refused instead.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        rid = f"{tag}-marker"
        await index.record(_label(rid))
        with pytest.raises(LabelIndexError, match="indistinguishable"):
            await index.store_labels(_derived(_label(rid)), underived_stamp(_VERSION))

    _both_backends(_body)
