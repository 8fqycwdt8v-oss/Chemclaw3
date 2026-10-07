"""Draining a bulk reaction corpus out of a warehouse and into the label index.

Runs offline against a fake driver, so a real table only needs correct column names in one YAML
file. The corpus must never become an ingest source: ingest paths assume one site's ELN and do
not scale to a corpus of this size.
"""

import asyncio
import re
from datetime import date

import pytest

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.ingest.eln.warehouse.binding import CorpusBinding, load_binding
from chemclaw.ingest.labels.corpus import drain_corpus
from chemclaw.ingest.sources.registry import (
    active_ingest_source_names,
    active_retrieve_sources,
    discovered,
)
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions, record_for_reaction
from chemclaw.science.fingerprints.store import FingerprintInputError, InMemoryFingerprintStore
from chemclaw.science.labels.reactions import corpus_reactions
from chemclaw.science.labels.store import InMemoryLabelIndex
from chemclaw.science.labels.vocabulary import LabelGroup
from tests.pg import migrated_db_or_skip
from tests.warehouse_fake import KeysetWarehouse

_RELATION = "V_REACTION"

_BINDING = {
    "relation": _RELATION,
    "key": "REACTION_ID",
    "order_by": "REACTION_ID",
    "fetch_limit": 2,
    "smiles": {"path": "root.REACTION_SMILES"},
    "citation": {"path": "root.PATENT_NUMBER"},
    "published_on": {"path": "root.PUBLICATION_DATE", "transform": [{"iso_date": {}}]},
    "yield_percent": {"path": "root.YIELD_PCT", "transform": [{"number": {}}]},
    "workup_text": {"path": "root.WORKUP_TEXT"},
    "named_reaction": {"path": "root.NAMERXN_NAME"},
    "rxno_id": {"path": "root.RXNO_ID"},
}


def _rows() -> list[dict[str, object]]:
    """Four patent reactions: two NameRxn-classified, one not, and one with no product."""
    return [
        {
            "REACTION_ID": "p1",
            "REACTION_SMILES": "Brc1ccccc1.NC1CCCCC1>CC#N>c1ccc(NC2CCCCC2)cc1",
            "PATENT_NUMBER": "US9376441B2",
            "PUBLICATION_DATE": "2016-06-28",
            "YIELD_PCT": "88",
            "WORKUP_TEXT": "Diluted with water and extracted with EtOAc.",
            "NAMERXN_NAME": "Buchwald-Hartwig amination",
            "RXNO_ID": "RXNO:0000192",
        },
        {
            "REACTION_ID": "p2",
            "REACTION_SMILES": "Brc1ccccc1.OB(O)c1ccccc1>CCOCC>c1ccc(-c2ccccc2)cc1",
            "PATENT_NUMBER": "US7000000B2",
            "PUBLICATION_DATE": "2006-02-14",
            "YIELD_PCT": "91",
            "WORKUP_TEXT": None,
            "NAMERXN_NAME": "Bromo Suzuki coupling",
            "RXNO_ID": "RXNO:0000140",
        },
        # The third of Pistachio that NameRxn could not classify — the case the labelling drain
        # exists for, and the reason `provides` is never a skip.
        {
            "REACTION_ID": "p3",
            "REACTION_SMILES": "CCO.CC(=O)O>>CCOC(C)=O",
            "PATENT_NUMBER": "US8000000B2",
            "PUBLICATION_DATE": "2011-08-16",
            "YIELD_PCT": None,
            "WORKUP_TEXT": "Concentrated in vacuo.",
            "NAMERXN_NAME": None,
            "RXNO_ID": None,
        },
        # An extraction that resolved no product. Not a precedent, and counted as skipped rather
        # than dropped in silence.
        {
            "REACTION_ID": "p4",
            "REACTION_SMILES": "Brc1ccccc1.NC1CCCCC1>>",
            "PATENT_NUMBER": "US9000000B2",
            "PUBLICATION_DATE": "2015-01-01",
            "YIELD_PCT": None,
            "WORKUP_TEXT": None,
            "NAMERXN_NAME": None,
            "RXNO_ID": None,
        },
    ]


def _fake() -> KeysetWarehouse:
    """A warehouse holding the four rows, honouring keyset paging."""
    return KeysetWarehouse({_RELATION: _rows()}, _RELATION, "REACTION_ID")


def _binding() -> CorpusBinding:
    """The corpus binding under test."""
    return CorpusBinding.model_validate(_BINDING)


async def test_the_drain_pages_by_keyset_and_records_what_it_reads() -> None:
    """Two pages of two, resuming strictly after the last key — never re-reading a row."""
    index, warehouse, binding = InMemoryLabelIndex(), _fake(), _binding()
    first = await drain_corpus(warehouse, binding, index, "pistachio", limit=2)
    assert (first.read, first.recorded, first.cursor, first.has_more) == (2, 2, "p2", True)

    second = await drain_corpus(warehouse, binding, index, "pistachio", after=first.cursor, limit=2)
    assert (second.read, second.recorded, second.skipped) == (2, 1, 1)
    assert second.cursor == "p4"

    third = await drain_corpus(warehouse, binding, index, "pistachio", after=second.cursor, limit=2)
    assert (third.read, third.recorded, third.has_more) == (0, 0, False)

    assert {r.reaction_id for r in await index.stale("any", limit=50)} == {"p1", "p2", "p3"}


async def test_a_field_the_source_supplied_and_the_drain_cannot_read_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A field the source supplied but `_number` cannot read is counted, not silently NULLed.

    Facet search filters on these columns, so an unread unit-bearing cell (`'60 °C'`, `'rt'`)
    would silently drop rows from a precedent search. A blank cell is the source recording nothing
    and is deliberately not counted.
    """
    import logging

    binding = dict(_BINDING)
    # No `transform:` here: a declared transform raises on an unreadable cell, so the silent case
    # is a plainly bound column parsed only by `_number`.
    binding["temperature_c"] = {"path": "root.TEMP"}
    rows = _rows()[:2]
    rows[0]["TEMP"] = "60 °C"
    rows[1]["TEMP"] = ""
    warehouse = KeysetWarehouse({_RELATION: rows}, _RELATION, "REACTION_ID")

    with caplog.at_level(logging.WARNING):
        report = await drain_corpus(
            warehouse,
            CorpusBinding.model_validate(binding),
            InMemoryLabelIndex(),
            "pistachio",
            limit=5,
        )

    assert (report.read, report.recorded) == (2, 2), "the rows are recorded, without the field"
    assert report.unreadable_fields == 1, "the unit-carrying cell is counted; the blank one is not"
    assert "could not be read" in caplog.text


async def test_a_blank_date_is_the_source_recording_nothing_and_is_not_counted() -> None:
    """A blank date is absent, not unreadable, so it is not counted; a garbled date still is."""
    rows = _rows()[:2]
    rows[0]["PUBLICATION_DATE"] = "   "
    rows[1]["PUBLICATION_DATE"] = ""
    warehouse = KeysetWarehouse({_RELATION: rows}, _RELATION, "REACTION_ID")

    report = await drain_corpus(warehouse, _binding(), InMemoryLabelIndex(), "pistachio", limit=5)

    assert (report.recorded, report.unreadable_fields) == (2, 0)


async def test_the_corpus_page_runs_its_patterns_under_one_page_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corpus page runs its transform patterns under one page-wide regex budget.

    The per-cell bound does not compose across a page; a budget too small for an honest pattern
    shows the page bound is in force.
    """
    from chemclaw.ingest.eln.warehouse.expr import PatternBudgetError

    monkeypatch.setattr(settings, "eln_regex_page_budget_seconds", 1e-9)
    binding = dict(_BINDING)
    binding["citation"] = {
        "path": "root.PATENT_NUMBER",
        "transform": [{"regex": {"pattern": r"US(\d+)", "group": 0}}],
    }

    with pytest.raises(PatternBudgetError):
        await drain_corpus(
            _fake(), CorpusBinding.model_validate(binding), InMemoryLabelIndex(), "pistachio"
        )


async def test_a_readable_corpus_counts_no_unreadable_fields() -> None:
    """The counter's zero, so that a rise in it means something.

    A test whose measured value is always nonzero cannot tell a reader that the ordinary case is
    quiet, which is the half that makes the counter above worth reading.
    """
    report = await drain_corpus(_fake(), _binding(), InMemoryLabelIndex(), "pistachio", limit=5)
    assert report.unreadable_fields == 0


async def test_a_recorded_row_carries_the_citation_the_conditions_and_the_species() -> None:
    """A precedent a chemist cannot follow back is not a precedent — so the citation is required."""
    index = InMemoryLabelIndex()
    await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)
    rows = {r.reaction_id: r for r in await index.stale("any", limit=50)}

    buchwald = rows["p1"]
    assert buchwald.citation == "US9376441B2"
    assert buchwald.performed_on == date(2016, 6, 28)
    assert buchwald.yield_percent == 88.0
    assert buchwald.workup_text is not None and "EtOAc" in buchwald.workup_text
    # `reactants>agents>products` split into species, each carrying the slot it came from. The
    # agent slot is `reagent`, not `solvent`: the record form groups solvent, catalyst, ligand
    # and base into one slot, and deciding which is the labeller's job.
    assert [(s.smiles, s.role) for s in buchwald.species] == [
        ("Brc1ccccc1", "reactant"),
        ("NC1CCCCC1", "reactant"),
        ("CC#N", "reagent"),
        ("c1ccc(NC2CCCCC2)cc1", "product"),
    ]
    # Nothing is derived yet — that is the labelling drain's pass.
    assert all(s.derived_role is None for s in buchwald.species)


async def test_a_label_the_corpus_carries_is_recorded_and_marked_as_the_corpus_claim() -> None:
    """A corpus claim and our own SMIRKS match are different evidence, and `method` says so."""
    index = InMemoryLabelIndex()
    await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)
    rows = {r.reaction_id: r for r in await index.stale("any", limit=50)}

    assert rows["p1"].named_reaction == "Buchwald-Hartwig amination"
    assert rows["p1"].rxno_id == "RXNO:0000192"
    assert rows["p1"].method == "source"
    # The unclassified third: a row the corpus left empty, which the labeller must fill. It is
    # stale exactly like every other row, because `provides` is not a skip.
    assert rows["p3"].named_reaction is None
    assert rows["p3"].method is None
    assert rows["p3"].labeller_version is None
    # A NULL column is `None`, not the string "None", which would count as a named reaction.
    assert rows["p3"].rxno_id is None


async def test_re_draining_an_unchanged_release_is_a_no_op_that_keeps_its_labels() -> None:
    """A drain is safe to stop and resume at any point, with no bookkeeping to get wrong."""
    index = InMemoryLabelIndex()
    await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)
    rows = {r.reaction_id: r for r in await index.stale("any", limit=50)}
    await index.store_labels(rows["p3"], "rxnlabel@1")

    await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)
    assert "p3" not in {r.reaction_id for r in await index.stale("rxnlabel@1", limit=50)}


def test_the_shipped_pistachio_manifest_binds_and_declares_what_it_carries() -> None:
    """The manifest is the schema, so what it claims has to be checkable without a tenant."""
    manifest = discovered()["pistachio"]
    assert manifest.ingest is None, "a corpus must not be an ingest source — see the last test"
    assert manifest.retrieve is not None
    assert manifest.labels is not None

    binding = load_binding(manifest.config["binding"])
    assert binding.corpus is not None
    # The two-way check `make datasource-validate` makes: every group claimed has a column.
    assert manifest.labels.provides <= binding.corpus.label_groups()
    assert LabelGroup.NAMED_REACTION in binding.corpus.label_groups()


def test_one_source_carries_both_seams_onto_the_same_table() -> None:
    """A corpus and a vector index are two questions of one table, not two sources.

    `vector:` ranks by similarity; `corpus:` is drained into the queryable label index. They share
    a connection only, and the source declares exactly one `retrieve:` callable.
    """
    binding = load_binding(discovered()["pistachio"].config["binding"])
    assert binding.vector is not None
    assert binding.corpus is not None
    assert binding.vector.relation == binding.corpus.relation, "two seams, one table"


def test_a_reaction_corpus_never_becomes_an_ingest_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaction corpus has no ingest half, so `read_corpus` and O(n²) clustering skip it."""
    monkeypatch.setattr(settings, "data_sources", "graph,pistachio")
    assert "pistachio" not in active_ingest_source_names()
    assert "pistachio" in {source.name for source in active_retrieve_sources()}


# --- the pagination column, when the release leaves it NULL -------------------------------------

_LOAD_SEQ_BINDING = {
    **_BINDING,
    # A release loaded in batches paginates on its load sequence, not on its reaction id — the
    # shape `CorpusBinding.order_by` exists for, and the one where `key` and `cursor_column` are
    # two different columns holding two different domains of value.
    "order_by": "LOAD_SEQ",
}


def _load_seq_rows() -> list[dict[str, object]]:
    """The four reactions, keyed for pagination by a load sequence the first row never got."""
    rows = _rows()
    for row, load_seq in zip(rows, [None, "A100", "B200", "P300"], strict=True):
        row["LOAD_SEQ"] = load_seq
    return rows


def _load_seq_warehouse() -> KeysetWarehouse:
    """A warehouse paginating on `LOAD_SEQ`, NULL first — the order Spark gives an ASC sort."""
    return KeysetWarehouse({_RELATION: _load_seq_rows()}, _RELATION, "LOAD_SEQ")


async def test_a_null_in_the_pagination_column_never_becomes_the_string_none() -> None:
    """A NULL pagination value never becomes the string "None" as the next page's cursor.

    That would resume at `> 'None'` and silently skip keys sorting below it; a cursor that cannot
    advance holds its position so the "no cursor advance" guard fires.
    """
    index, warehouse = InMemoryLabelIndex(), _load_seq_warehouse()
    binding = CorpusBinding.model_validate(_LOAD_SEQ_BINDING)

    page = await drain_corpus(warehouse, binding, index, "pistachio", limit=1)

    assert (page.read, page.recorded, page.has_more) == (1, 1, True)
    assert page.cursor == "", "a NULL cursor value must not advance the keyset"
    # And the *key* column's value is not substituted for it either: `p1` is an id, `LOAD_SEQ`
    # holds load sequences, and comparing one against the other resumes the drain at an
    # arbitrary point in the release.
    await drain_corpus(warehouse, binding, index, "pistachio", after=page.cursor, limit=1)
    assert [params for _, params in warehouse.executed] == [[1], [1]]


async def test_the_cursor_advances_past_a_row_the_drain_skips() -> None:
    """The cursor advances past a keyless row the drain skips, or the drain wedges on it forever."""
    rows = _load_seq_rows()
    rows[1]["REACTION_ID"] = None  # the row `_record` refuses for want of a key
    index = InMemoryLabelIndex()
    warehouse = KeysetWarehouse({_RELATION: rows}, _RELATION, "LOAD_SEQ")
    binding = CorpusBinding.model_validate(_LOAD_SEQ_BINDING)

    page = await drain_corpus(warehouse, binding, index, "pistachio", after="A099", limit=1)

    assert (page.read, page.recorded, page.skipped) == (1, 0, 1)
    assert page.cursor == "A100"


async def test_every_recorded_reaction_is_fingerprinted_under_its_source_and_id() -> None:
    """Every recorded reaction is fingerprinted under its `(source, id)` pair.

    The pair joins a hit to `reaction_labels` and keeps two sources sharing an id apart.
    """
    index = InMemoryLabelIndex()
    reactions = InMemoryFingerprintStore()

    report = await drain_corpus(
        _fake(), _binding(), index, "pistachio", reactions=reactions, limit=10
    )

    stored = await reactions.all_records()
    assert {(r.source, r.id) for r in stored} == {
        ("pistachio", "p1"),
        ("pistachio", "p2"),
        ("pistachio", "p3"),
    }
    assert report.unfingerprintable == 0
    # p4 resolved no product, so it is not a precedent and never reached the index at all.
    assert report.skipped == 1


async def test_the_indexed_reaction_drops_its_agents_so_a_solvent_swap_cannot_dominate() -> None:
    """The indexed label is `reactants>>products`, never the three-part form.

    DRFP folds agents onto reactants, so a solvent swap would dominate similarity. Asserted on the
    stored label, the string a future change would silently widen.
    """
    index = InMemoryLabelIndex()
    reactions = InMemoryFingerprintStore()

    await drain_corpus(_fake(), _binding(), index, "pistachio", reactions=reactions, limit=10)

    stored = {r.id: r for r in await reactions.all_records()}
    # p1 was recorded with `CC#N` (acetonitrile) in the agent slot.
    assert ">CC#N>" in _rows()[0]["REACTION_SMILES"]  # type: ignore[operator]
    assert stored["p1"].label == "Brc1ccccc1.NC1CCCCC1>>c1ccc(NC2CCCCC2)cc1"
    assert "CC#N" not in stored["p1"].label
    assert stored["p1"].definition == reaction_definition()
    assert stored["p1"].source == "pistachio"


async def test_a_reaction_with_no_fingerprint_is_counted_rather_than_failing_the_page() -> None:
    """A reaction with no fingerprint is counted, and the rest of the page is still written."""
    rows = _rows()
    # Identical on both sides: DRFP's symmetric difference is empty, so there are no features
    # to fold and `drfp_bitstring` refuses rather than storing meaningless bits.
    rows[2]["REACTION_SMILES"] = "CCO>>CCO"
    index = InMemoryLabelIndex()
    reactions = InMemoryFingerprintStore()
    warehouse = KeysetWarehouse({_RELATION: rows}, _RELATION, "REACTION_ID")

    report = await drain_corpus(
        warehouse, _binding(), index, "pistachio", reactions=reactions, limit=10
    )

    assert report.unfingerprintable == 1
    assert report.recorded == 3
    # Still recorded, still answerable by facet — only its similarity row is missing.
    assert {r.id for r in await reactions.all_records()} == {"p1", "p2"}
    assert await index.count() == 3


async def test_the_drain_without_a_reaction_store_writes_no_fingerprints_and_still_records() -> (
    None
):
    """`reactions=None` is the release-mode default and must stay a complete drain.

    The molecule half has the same shape and the same reason: a caller that wants the label index
    and not the similarity indexes must not have to pass a store it will never search.
    """
    index = InMemoryLabelIndex()

    report = await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)

    assert report.recorded == 3
    assert report.unfingerprintable == 0


@pytest.mark.anyio
async def test_corpus_reactions_is_searchable_with_no_search_code_of_its_own() -> None:
    """`corpus_reactions` is searchable through the table-parameterised fingerprint store.

    Driven against the real database because the migration and the HNSW index are under test. Two
    sources hold the same reaction id, proving the primary key is the `(source, id)` pair.
    """
    await migrated_db_or_skip()
    store = corpus_reactions()

    coupling = "Brc1ccccc1.NC1CCCCC1>>c1ccc(NC2CCCCC2)cc1"
    esterification = "CCO.CC(=O)O>>CCOC(C)=O"
    await store.add(record_for_reaction("s1", coupling).model_copy(update={"source": "pistachio"}))
    # Same entry id, different source, different chemistry — one row each, not one overwritten.
    await store.add(
        record_for_reaction("s1", esterification).model_copy(update={"source": "other-corpus"})
    )

    hits = await find_similar_reactions(store, coupling, top_k=2)

    assert (hits.hits[0].source, hits.hits[0].id) == ("pistachio", "s1")
    assert hits.hits[0].similarity == pytest.approx(1.0)

    # Both same-id rows survive, told apart by source; read via `all_records` because the
    # esterification is legitimately below the similarity floor.
    stored = {(r.source, r.id) for r in await store.all_records() if r.id == "s1"}
    assert stored == {("pistachio", "s1"), ("other-corpus", "s1")}


def test_an_unparseable_species_is_still_fingerprinted_which_is_why_only_one_error_is_caught() -> (
    None
):
    """Why `_collect_fingerprint` catches `FingerprintInputError` alone.

    `standard_smiles` returns unparseable input unchanged, so DRFP still yields bits while ECFP
    raises. A change to `standard_smiles` should turn this red rather than leave a dead branch.
    """

    async def _run() -> None:
        rows = _rows()
        rows[2]["REACTION_SMILES"] = "CCO.C(((C>>CCOC(C)=O"
        index = InMemoryLabelIndex()
        reactions = InMemoryFingerprintStore()
        warehouse = KeysetWarehouse({_RELATION: rows}, _RELATION, "REACTION_ID")

        report = await drain_corpus(
            warehouse, _binding(), index, "pistachio", reactions=reactions, limit=10
        )

        assert report.unfingerprintable == 0
        assert ("pistachio", "p3") in {(r.source, r.id) for r in await reactions.all_records()}

    asyncio.run(_run())

    with pytest.raises(FingerprintInputError):
        ecfp_bitstring("C(((C")


# --- the pass leaves a series behind ----------------------------------------------------------


def _outcomes(source: str) -> set[str]:
    """Every `outcome` this source rendered a series for.

    The partition is a claim about the whole label set, so the whole set is asserted.
    """
    found = set()
    for line in METRICS.render().splitlines():
        head, _, _reading = line.partition("} ")
        if head.startswith("chemclaw_ingest_records_total{") and f'source="{source}"' in head:
            match = re.search(r'outcome="([^"]+)"', head)
            if match:
                found.add(match.group(1))
    return found


def _series(name: str, **labels: str) -> float:
    """One labelled series' value, read from the rendered exposition Prometheus scrapes."""
    wanted = [f'{label}="{value}"' for label, value in labels.items()]
    for line in METRICS.render().splitlines():
        head, _, reading = line.partition("} ")
        if head.startswith(f"{name}{{") and all(pair in head for pair in wanted):
            return float(reading)
    raise AssertionError(f"no series {name}{{{', '.join(wanted)}}} in the exposition")


def _baseline(name: str, **labels: str) -> float:
    """The same reading taken before the drain under test, with absence read as zero.

    Counters are process-wide and monotonic, so tests assert deltas; absolute readings fail on a
    second run in the same process.
    """
    try:
        return _series(name, **labels)
    except AssertionError:
        return 0.0


async def test_the_drain_books_the_rows_it_read_and_the_two_series_partition_them() -> None:
    """The drain books the rows it read, and `ingested` and `rejected` partition them.

    The page holds one good and one refused row so a partition cannot be a coincidence. `rejected`
    is a reached row that could not become a record; there is no `skipped` population here.
    """
    source = "pistachio-metrics-partition"

    counter = "chemclaw_ingest_records_total"
    # Deltas, because the registry is process-wide and these counters are monotonic: the
    # claim is about what *this* drain booked, not about what the process has booked since it
    # started. See `_baseline`.
    was_ingested = _baseline(counter, source=source, outcome="ingested")
    was_rejected = _baseline(counter, source=source, outcome="rejected")
    index, warehouse, binding = InMemoryLabelIndex(), _fake(), _binding()
    first = await drain_corpus(warehouse, binding, index, source, limit=2)
    page = await drain_corpus(warehouse, binding, index, source, after=first.cursor, limit=2)

    assert (page.read, page.recorded, page.skipped) == (2, 1, 1)
    ingested = _series(counter, source=source, outcome="ingested") - was_ingested
    rejected = _series(counter, source=source, outcome="rejected") - was_rejected
    # Both pages, so the totals are the whole four-row release rather than the second page.
    assert (ingested, rejected) == (3.0, 1.0)
    assert ingested + rejected == float(first.read + page.read)
    # The full label set, not the absence of one word: a third outcome would keep the sum true
    # while counting a row in two series.
    assert _outcomes(source) == {"ingested", "rejected"}


async def test_a_page_that_read_nothing_still_books_a_zero() -> None:
    """An empty page still books a zero, so a missing series means the drain did not run."""
    source = "pistachio-metrics-empty"

    report = await drain_corpus(
        _fake(), _binding(), InMemoryLabelIndex(), source, after="p9", limit=2
    )

    assert (report.read, report.recorded, report.skipped) == (0, 0, 0)
    assert _series("chemclaw_ingest_records_total", source=source, outcome="ingested") == 0.0
    assert _series("chemclaw_ingest_records_total", source=source, outcome="rejected") == 0.0


def test_the_series_are_per_source_which_is_what_the_aggregated_outcome_cannot_say() -> None:
    """Two corpora drained in one run are two label sets, not one sum.

    `CorpusReport` carries no source, but `drain_corpus` is called per source, so the metric can.
    """

    async def _run() -> None:
        counter = "chemclaw_ingest_records_total"
        sources = ("pistachio-metrics-a", "pistachio-metrics-b")
        was = {
            (source, outcome): _baseline(counter, source=source, outcome=outcome)
            for source in sources
            for outcome in ("ingested", "rejected")
        }
        warehouse, binding = _fake(), _binding()
        await drain_corpus(warehouse, binding, InMemoryLabelIndex(), sources[0], limit=2)
        await drain_corpus(warehouse, binding, InMemoryLabelIndex(), sources[1], limit=1)

        def moved(source: str, outcome: str) -> float:
            return _series(counter, source=source, outcome=outcome) - was[(source, outcome)]

        assert moved(sources[0], "ingested") == 2.0
        assert moved(sources[1], "ingested") == 1.0
        # `_series`, not `_baseline`, on the far side: a rejection series that moved by nothing
        # must still *exist*, which is the whole reason the drain books a zero.
        assert moved(sources[0], "rejected") == 0.0
        assert moved(sources[1], "rejected") == 0.0

    asyncio.run(_run())
