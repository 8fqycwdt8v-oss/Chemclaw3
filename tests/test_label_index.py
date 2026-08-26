"""The reaction-label index: the two-phase write, staleness as a query, and coverage.

Every test here runs against **both** backends, driven by the same body, because the in-memory one
is the reference the SQL one has to match and the interesting rules — what a re-ingest may clobber,
what `IS DISTINCT FROM NULL` finds — are exactly the ones that are easy to get right in Python and
wrong in SQL. The Postgres half skips when no database is reachable (see `tests/pg.py`; a green
local run without `make up` has executed only half of this file).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date

import pytest

from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.store import (
    InMemoryLabelIndex,
    LabelIndex,
    LabelIndexError,
    PostgresLabelIndex,
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

    Distinct keys because the durable index is shared with every other test in the same schema, and
    a fixture id colliding with another file's is the failure mode `tests/pg.py`'s isolation note
    describes one level up.
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
