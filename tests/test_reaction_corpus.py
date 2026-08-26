"""Draining a bulk reaction corpus out of a warehouse and into the label index.

The whole path is exercised offline against a fake driver — the same way the warehouse ELN shipped
and was proved before any tenant existed. What that buys is that when the real Pistachio table
arrives, the only thing that has to be right is the column names in one YAML file.

The property this file is really about is the *absence* of an ingest half. Five paths in this tree
assume an ingest source is one site's ELN, and each breaks on a corpus of this size; the last test
here is the assertion that keeps them out of its way.
"""

import asyncio
from datetime import date

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.eln.warehouse.binding import CorpusBinding, load_binding
from chemclaw.ingest.labels.corpus import drain_corpus
from chemclaw.ingest.sources.registry import (
    active_ingest_source_names,
    active_retrieve_sources,
    discovered,
)
from chemclaw.science.labels.store import InMemoryLabelIndex
from chemclaw.science.labels.vocabulary import LabelGroup
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


def test_the_drain_pages_by_keyset_and_records_what_it_reads() -> None:
    """Two pages of two, resuming strictly after the last key — never re-reading a row."""

    async def _run() -> None:
        index, warehouse, binding = InMemoryLabelIndex(), _fake(), _binding()
        first = await drain_corpus(warehouse, binding, index, "pistachio", limit=2)
        assert (first.read, first.recorded, first.cursor, first.has_more) == (2, 2, "p2", True)

        second = await drain_corpus(
            warehouse, binding, index, "pistachio", after=first.cursor, limit=2
        )
        assert (second.read, second.recorded, second.skipped) == (2, 1, 1)
        assert second.cursor == "p4"

        third = await drain_corpus(
            warehouse, binding, index, "pistachio", after=second.cursor, limit=2
        )
        assert (third.read, third.recorded, third.has_more) == (0, 0, False)

        assert {r.reaction_id for r in await index.stale("any", limit=50)} == {"p1", "p2", "p3"}

    asyncio.run(_run())


def test_a_recorded_row_carries_the_citation_the_conditions_and_the_species() -> None:
    """A precedent a chemist cannot follow back is not a precedent — so the citation is required."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_label_the_corpus_carries_is_recorded_and_marked_as_the_corpus_claim() -> None:
    """A corpus claim and our own SMIRKS match are different evidence, and `method` says so."""

    async def _run() -> None:
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
        # And it is `None`, not the string "None". `as_text` is `str()` for everything, so a NULL
        # column reached the model as a four-character name until this test caught it — after
        # which every unclassified patent reaction would have been counted in frequency tables as
        # a named reaction called "None".
        assert rows["p3"].rxno_id is None

    asyncio.run(_run())


def test_re_draining_an_unchanged_release_is_a_no_op_that_keeps_its_labels() -> None:
    """A drain is safe to stop and resume at any point, with no bookkeeping to get wrong."""

    async def _run() -> None:
        index = InMemoryLabelIndex()
        await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)
        rows = {r.reaction_id: r for r in await index.stale("any", limit=50)}
        await index.store_labels(rows["p3"], "rxnlabel@1")

        await drain_corpus(_fake(), _binding(), index, "pistachio", limit=10)
        assert "p3" not in {r.reaction_id for r in await index.stale("rxnlabel@1", limit=50)}

    asyncio.run(_run())


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

    `vector:` ranks by embedding similarity — "find me reactions that read like this one" — and
    cannot answer *which ligand*, *as what* or *under what conditions*, because those are
    properties of the recipe and an embedding is not a queryable decomposition of it. `corpus:` is
    drained into the label index, which can.

    They share a connection and nothing else, and the source declares exactly one `retrieve:`
    callable — which is why `corpus_sources()` reads the manifest rather than asking what the built
    retrieve half is an instance of. This is the assertion that keeps that true.
    """
    binding = load_binding(discovered()["pistachio"].config["binding"])
    assert binding.vector is not None
    assert binding.corpus is not None
    assert binding.vector.relation == binding.corpus.relation, "two seams, one table"


def test_a_reaction_corpus_never_becomes_an_ingest_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assertion that keeps `read_corpus` and the O(n²) clustering away from this corpus.

    `durable/memory_jobs.py::read_corpus` fetches *every* reaction from every active ingest half
    into the worker heap, and `memory/similarity.cluster_by_similarity` is O(n²) over the result.
    Pistachio declares no ingest half, so neither is ever reached — and this is what makes that a
    checked property rather than a comment somebody could delete.
    """
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


def test_a_null_in_the_pagination_column_never_becomes_the_string_none() -> None:
    """`as_text` is `str()`, so a NULL cursor value resumed the next page at `> 'None'`.

    The identical defect `_field` documents and fixes, on the line that decides where the next page
    starts. `"None"` is truthy, so the `or key` fallback never fired either, and the following
    statement asked the warehouse for every key sorting *above* those six characters — silently
    dropping `A100` and `B200`, which on a release keyed by digits or early letters is most of it.
    A cursor that cannot advance holds its position instead, which is what makes the workflow's
    "no cursor advance" guard fire and name the mis-declared `order_by`.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_the_cursor_advances_past_a_row_the_drain_skips() -> None:
    """A row with no key is skipped as a precedent — the drain must still get past it.

    The cursor was only written for rows that *had* a key, so a keyless row at the end of a page
    left it where it was, and the next page returned that same row: a drain wedged permanently by a
    row it was never going to record. Advancing is read from the pagination column alone, which is
    the only column the resume predicate compares.
    """

    async def _run() -> None:
        rows = _load_seq_rows()
        rows[1]["REACTION_ID"] = None  # the row `_record` refuses for want of a key
        index = InMemoryLabelIndex()
        warehouse = KeysetWarehouse({_RELATION: rows}, _RELATION, "LOAD_SEQ")
        binding = CorpusBinding.model_validate(_LOAD_SEQ_BINDING)

        page = await drain_corpus(warehouse, binding, index, "pistachio", after="A099", limit=1)

        assert (page.read, page.recorded, page.skipped) == (1, 0, 1)
        assert page.cursor == "A100"

    asyncio.run(_run())
