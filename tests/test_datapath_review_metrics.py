"""Three claims about measurement that the measurement itself refutes.

An embedding chokepoint one retrieval leg walks past; a label value that does not match the HELP
describing it; and a WARNING documented under a rule ("log volume is a function of the pass") that
it does not obey, on the one path where the pass is a chemist's turn.
"""

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core import embeddings
from chemclaw.core.metrics import METRICS
from chemclaw.ingest.documents import external_index
from chemclaw.ingest.eln.records import InMemoryReactionRecordStore
from chemclaw.ingest.eln.warehouse.retriever import WarehouseVectorRetriever
from tests import warehouse_fake
from tests.test_datapath_observability import _counter


def _help_for(name: str) -> str:
    """The `# HELP` line the exposition carries for `name` — all an operator has to read."""
    prefix = f"# HELP {name} "
    for line in METRICS.render().splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"no HELP for {name} in the exposition")


def test_the_embedding_outcome_label_is_a_value_its_own_help_names() -> None:
    """A rule written from the HELP selected an empty series for the life of the metric.

    The HELP says "by outcome (ok / error)" and the failure path emitted `outcome="failure"`, so
    `chemclaw_embedding_calls_total{outcome="error"}` — the series anybody reading the HELP would
    alert on — never existed. Checked against the *rendered* HELP rather than against a literal,
    because the HELP is the only description of this series an operator has and a test that
    hardcoded "error" would go green if the HELP changed underneath it.
    """

    def broken(text: str) -> list[float]:
        raise RuntimeError("provider refused")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(embeddings, "_hash_embedding", broken)
    try:
        with pytest.raises(RuntimeError):
            embeddings.embed_texts(["review-probe"], cache=False)
    finally:
        monkey.undo()
    embeddings.embed_texts(["review-probe-ok"], cache=False)

    described = _help_for("chemclaw_embedding_calls_total")
    emitted = {
        head.partition('outcome="')[2].partition('"')[0]
        for head, _, _ in (line.partition("} ") for line in METRICS.render().splitlines())
        if head.startswith("chemclaw_embedding_calls_total{")
    }
    assert emitted, "sanity: the counter emitted something to check"
    for outcome in emitted:
        assert outcome in described, (
            f"outcome={outcome!r} is emitted but the HELP describes {described!r} — a rule written "
            "from the HELP selects nothing"
        )


def test_the_embedding_chokepoint_claim_names_its_own_exception() -> None:
    """`_embed_uncached` called itself "the one place every provider goes through". It is not.

    An absence assertion, because the defect *is* the sentence: a docstring that overstates the
    coverage of a metric is what makes an operator read a missing leg as a quiet one. The measured
    fact is the test below; this is what stops the claim coming back.
    """
    source = inspect.getsource(embeddings._embed_uncached)
    assert "one place every provider goes through" not in source
    assert "server" in source, "the exception is named where the claim used to be"


def test_a_server_side_warehouse_embedding_books_nothing_on_the_embedding_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`vector: {embedding: server}` hands the raw query to the warehouse; `embed_texts` is skipped.

    So that leg's calls, failures and latency are absent from `chemclaw_embedding_*` — which is a
    property of where the embedding happens (inside the warehouse's SQL, with no client call to
    time) rather than a gap to plug. It is named here so the number is a measured fact rather than
    a reading of the branch.
    """
    monkeypatch.setattr(
        "chemclaw.ingest.eln.warehouse.retriever.default_record_store",
        lambda: InMemoryReactionRecordStore(),
    )
    binding: dict[str, Any] = {
        "connection": {"driver": "tests.warehouse_fake:open_fake"},
        "vector": {
            "relation": "V_EMBEDDING",
            "key": "REACTION_ID",
            "vector_column": "REACTION_VECTOR",
            "content_columns": ["PROTOCOL_TEXT"],
            "embedding": "server",
            "server_embed_function": "ai_query",
            "server_embed_model": "e5-base-v2",
        },
    }
    rows = {
        "V_EMBEDDING": [
            {"REACTION_ID": "RX-1", "PROTOCOL_TEXT": "Reflux 90 min.", "CHEMCLAW_SCORE": 0.9}
        ]
    }

    embeddings.clear_embedding_cache()
    before = _counter("chemclaw_embedding_calls_total")
    warehouse_fake.prime(**rows)
    retriever = WarehouseVectorRetriever(binding=binding, name="review-warehouse")
    chunks = asyncio.run(retriever.retrieve("ester formation", {}))

    assert chunks, "sanity: the leg answered, so it is a leg that ran"
    assert _counter("chemclaw_embedding_calls_total") == before, (
        "the server-side leg booked an embedding call it does not make"
    )


# --- the interactive WARNING that never stops --------------------------------------------------


@pytest.fixture
def _fresh_unresolved_state() -> Any:
    """The throttle's memory is module state; put back what the process had."""
    saved = dict(external_index._LAST_UNRESOLVED_WARNING)
    external_index._LAST_UNRESOLVED_WARNING.clear()
    yield
    external_index._LAST_UNRESOLVED_WARNING.clear()
    external_index._LAST_UNRESOLVED_WARNING.update(saved)


def test_a_drifted_collection_warns_once_and_counts_every_time(
    _fresh_unresolved_state: None, caplog: pytest.LogCaptureFixture
) -> None:
    """`_report_unresolved` runs on every external vector search, so drift is per turn forever.

    A collection that has drifted meets the condition on *every* query against it — one WARNING per
    source per turn, indefinitely, for a standing fault an operator has already been told about.
    That is the failure `ingest/documents/sync._summarise_skips` states the rule against and this
    work cites four times: log volume must be a function of the fault, not of the traffic. There is
    no pass to summarise at the end of here, so the unit is the collection and a clock.

    The counter stays per query, because that is what says how *much* drift there is and it is what
    an alert reads.
    """
    before = _counter("chemclaw_vector_unresolved_points_total")

    with caplog.at_level(logging.DEBUG, logger="chemclaw.ingest.documents.external_index"):
        for _ in range(50):
            external_index._report_unresolved(10, 4, 4, "review-collection")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1, f"one standing fault, {len(warnings)} WARNINGs"
    assert len(debugs) == 49, "the per-query trail survives, at DEBUG"
    assert _counter("chemclaw_vector_unresolved_points_total") == before + 50 * 6
    assert "review-collection" in warnings[0].getMessage(), (
        "the line must name the collection an operator re-syncs"
    )


def test_a_second_drifted_collection_is_reported_on_its_own(
    _fresh_unresolved_state: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The throttle is per collection, because re-syncing one says nothing about another."""
    with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.documents.external_index"):
        external_index._report_unresolved(10, 4, 4, "collection-a")
        external_index._report_unresolved(10, 4, 4, "collection-a")
        external_index._report_unresolved(10, 4, 4, "collection-b")

    named = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(named) == 2
    assert any("collection-a" in line for line in named)
    assert any("collection-b" in line for line in named)


def test_a_resolved_search_says_and_counts_nothing(_fresh_unresolved_state: None) -> None:
    """The healthy path must stay free: no counter, no record, no throttle entry."""
    before = _counter("chemclaw_vector_unresolved_points_total")
    external_index._report_unresolved(10, 10, 10, "review-healthy")
    assert _counter("chemclaw_vector_unresolved_points_total") == before
    assert "review-healthy" not in external_index._LAST_UNRESOLVED_WARNING


def test_the_throttle_interval_is_a_stated_number_not_a_once_per_process_flag() -> None:
    """A fault that is fixed and returns must be reported again; "once ever" would hide it.

    Read off the module rather than restated, so the constant and this statement cannot drift.
    """
    assert external_index._UNRESOLVED_WARN_INTERVAL_SECONDS > 0
    source = Path(external_index.__file__).read_text(encoding="utf-8")
    assert "_UNRESOLVED_WARN_INTERVAL_SECONDS" in source
