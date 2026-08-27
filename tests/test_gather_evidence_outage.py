"""`gather_evidence` must not report an outage as "nothing on file".

The tool's docstring is the model's contract, and it says an empty result means *nothing on file,
never invented*. Every evidence source swallowed its own failure into an empty list, so a chemist
asking "have we run this nitration before?" during a Postgres blip was told, confidently, that the
company has no prior art — with nothing on the stream or in the answer saying a source was down.

The fix is narrow on purpose. A single flaky source still costs its own leg and not the turn, which
is the trade `fanout._sweep` argues for and is right about. What changed is the case where *nothing*
could be asked: there is no honest empty answer to give, so the tool raises and the model reports a
failure it can say out loud.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent import research_tools
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.documents.index import InMemoryDocumentIndex
from chemclaw.ingest.documents.retriever import ShareDocumentRetriever
from chemclaw.ingest.eln.warehouse.retriever import WarehouseVectorRetriever
from chemclaw.ingest.sources.vendored_dataset import (
    VendoredDatasetError,
    VendoredDatasetRetriever,
)
from chemclaw.retrieval.evidence import EvidenceChunk, EvidenceSweep, SourceRetriever
from chemclaw.retrieval.fanout import sweep_sources
from tests import warehouse_fake


class _Dead:
    """A source whose backing store is unreachable."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def retrieve(self, _query: str, _filters: dict[str, object]) -> list[EvidenceChunk]:
        raise ConnectionError(f"{self.name}: connection refused")


class _Live:
    """A source that answers — with `count` hits, possibly none."""

    def __init__(self, name: str, count: int = 0) -> None:
        self.name = name
        self._count = count

    async def retrieve(self, _query: str, _filters: dict[str, object]) -> list[EvidenceChunk]:
        return [
            EvidenceChunk(
                content=f"hit {index}",
                source_note_id=f"note-{index}",
                retriever=self.name,
                score=0.5,
            )
            for index in range(self._count)
        ]


def _gather() -> EvidenceSweep:
    """Call the tool's underlying coroutine, as the agent would."""
    return asyncio.run(research_tools.gather_evidence(query="have we run this nitration"))


def test_every_source_down_raises_instead_of_reporting_nothing_on_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect itself: an outage presented to a chemist as an absence of prior art."""
    monkeypatch.setattr(
        research_tools,
        "_sources",
        lambda _anchor: [("graph", _Dead("graph")), ("lexical", _Dead("lexical"))],
    )

    with pytest.raises(ChemclawError) as excinfo:
        _gather()

    message = str(excinfo.value)
    assert "graph" in message and "lexical" in message, (
        "the error must name which sources were unavailable, or an operator cannot act on it"
    )


def test_all_sources_healthy_and_empty_is_still_a_real_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. A quiet search is a legitimate result and must not become an error."""
    monkeypatch.setattr(
        research_tools,
        "_sources",
        lambda _anchor: [("graph", _Live("graph")), ("lexical", _Live("lexical"))],
    )

    sweep = _gather()
    assert sweep.chunks == []
    # And it says so as an absence rather than as a degradation: nothing failed, nothing was cut.
    assert sweep.sources_failed == [] and sweep.truncated_by is None


def test_one_source_down_still_answers_from_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single flaky source costs its own leg, not the turn — deliberately unchanged."""
    monkeypatch.setattr(
        research_tools,
        "_sources",
        lambda _anchor: [("graph", _Live("graph", 2)), ("lexical", _Dead("lexical"))],
    )

    sweep = _gather()
    assert len(sweep.chunks) == 2
    # **The half that used to be invisible.** A partial outage returned real-but-incomplete
    # evidence with the degradation visible only on the stream, so a chemist reading the tool's
    # result could not tell this from a corpus that genuinely holds two chunks.
    assert sweep.sources_failed == ["lexical"]


def test_the_shipped_retrieve_halves_report_a_failure_instead_of_an_empty_result(
    tmp_path: Path,
) -> None:
    """The other half of the same defect, one layer down — and where it actually lived.

    The two tests above pin `sweep_sources`'s channel and `gather_evidence`'s guard, both of which
    were correct all along. What made them unreachable is that three of the shipped retrieve halves
    caught everything internally and returned `[]`, so their branch reported `failed=False,
    chunks=0` — byte-identical to a source that ran fine and matched nothing. Driving the real
    `sweep_sources` with the real halves over unreachable backings measured it:

        raising halves    sources_failed=['sharedrive', 'eln-warehouse', 'vendored'] -> raises
        swallowing halves sources_failed=[]                                          -> no raise

    Row two was the shipped configuration. A retriever may still *decide* that a condition is not a
    failure — an unentitled caller, a filter this source cannot honour, a blank query — and those
    still return `[]`. What it may not do is decide that on behalf of its own backing store.
    """
    share_root = tmp_path / "share"
    (share_root / "docs").mkdir(parents=True)

    class _BrokenIndex(InMemoryDocumentIndex):
        """A document index whose database is unreachable."""

        async def search_dense(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("document index unreachable")

    share = ShareDocumentRetriever(
        binding={
            "mount": str(share_root),
            "roots": [{"path": "docs"}],
            "public": True,
            "extensions": [".txt"],
        },
        name="sharedrive",
        index=_BrokenIndex(),
    )
    warehouse_fake.prime(V_EMBEDDING=[]).fail_with = ConnectionError("warehouse down")
    warehouse = WarehouseVectorRetriever(
        binding={
            "connection": {"driver": "tests.warehouse_fake:open_fake"},
            "vector": {
                "relation": "V_EMBEDDING",
                "key": "REACTION_ID",
                "vector_column": "REACTION_VECTOR",
                "content_columns": ["REACTION_SMILES"],
            },
        },
        name="eln-warehouse",
    )
    # No manifest, no CSV: the corpus this half was told to read is not on disk.
    vendored = VendoredDatasetRetriever(dataset_dir=str(tmp_path / "absent"), name="vendored")

    sources: list[tuple[str, SourceRetriever]] = [
        ("sharedrive", share),
        ("eln-warehouse", warehouse),
        ("vendored", vendored),
    ]
    ranked, failed, _skipped = asyncio.run(sweep_sources(sources, "have we run this nitration", {}))

    assert [len(chunks) for chunks in ranked] == [0, 0, 0]
    assert sorted(failed) == ["eln-warehouse", "sharedrive", "vendored"], (
        f"every source was unreachable and the sweep reported sources_failed={sorted(failed)}; "
        "a failure that arrives as an empty list is a confident 'no prior art' during an outage"
    )


def test_a_vendored_corpus_that_failed_to_load_is_retried_rather_than_remembered_as_empty(
    tmp_path: Path,
) -> None:
    """A cached failure is worse than a transient one: it outlives the condition that caused it.

    `_load` used to store `[]` behind `if self._records is not None`, so one unreadable manifest at
    the first query made the corpus report empty for the life of the pod — after a single warning
    at startup, with every later query silent. The dataset is still read once *on success*; what
    may not be cached is the failure.
    """
    dataset = tmp_path / "reagents"
    dataset.mkdir()
    retriever = VendoredDatasetRetriever(dataset_dir=str(dataset), name="vendored")

    with pytest.raises(VendoredDatasetError):
        asyncio.run(retriever.retrieve("acetone", {}))

    # The operator mounts the corpus the deployment was missing. Nothing restarts.
    (dataset / "dataset.json").write_text(
        json.dumps(
            {
                "name": "reagents",
                "version": "1",
                "licence": "CC-BY-4.0",
                "retrieved_from": "https://example.invalid/reagents.csv",
                "description": "reagent names",
                "sha256": hashlib.sha256(b"name\nacetone\n").hexdigest(),
                "text_column": "name",
            }
        )
    )
    (dataset / "records.csv").write_bytes(b"name\nacetone\n")

    chunks = asyncio.run(retriever.retrieve("acetone", {}))
    assert [chunk.source_note_id for chunk in chunks] == ["vendored:reagents:0"], (
        "the next query must read the corpus again; a remembered empty is a permanent outage"
    )
