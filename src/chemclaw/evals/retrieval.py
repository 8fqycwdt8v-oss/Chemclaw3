"""Retrieval-quality metrics over a gold query→expected-source set.

Retrieval quality cannot be a pure function of a case, so these metrics run `GraphRetriever` over a
small versioned gold corpus (`eval_retrieval_corpus_dir`, a fixture, not `knowledge_dir`) and score
the returned note ids against `reference.expected_note_ids`. A regression in retrieval then moves a
pinned number. The gold set includes a query the literal substring filter cannot reach, measuring
that limitation rather than hiding it.

Only `GraphRetriever` is scored: the vector and lexical legs and RRF fusion need a derived index
over this fixture in Postgres (a `DEFERRED.md` row). When a deployment enables those paths, the
metrics refuse to report rather than mislabel a graph-only number, and every provenance string names
the retriever.
"""

import asyncio
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from chemclaw.core.config import NOTE_INDEX_SOURCES, settings
from chemclaw.core.db import close_pools_of_this_loop
from chemclaw.evals.metric import Direction, EvalCase, MetricError, MetricResult, metric
from chemclaw.kg.graph import scan_notes_dir
from chemclaw.retrieval.retrievers import GraphRetriever

_T = TypeVar("_T")


async def _closing_this_loops_pools(coro: Coroutine[Any, Any, _T]) -> _T:
    """Await `coro`, then close the pools its loop opened — inside that loop, where it is legal.

    In a `finally`, since the teardown hang `_run_sync` describes happens whether the coroutine
    answered or raised.
    """
    try:
        return await coro
    finally:
        await close_pools_of_this_loop()


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine to completion from this metric's sync interface.

    The `Metric` contract is sync, and `asyncio.run` cannot nest inside a running loop. With no
    running loop on this thread (scripts, or the `to_thread` worker `durable/eval_drift` uses), run
    directly; otherwise run on a fresh loop in a second thread and join it.

    Both paths close the pools their loop opened before the loop ends: `asyncio.run` awaits all
    remaining tasks at shutdown, and `psycopg_pool`'s background workers would keep it from
    returning.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_closing_this_loops_pools(coro))

    outcome: list[_T] = []
    failure: list[BaseException] = []

    def _target() -> None:
        try:
            outcome.append(asyncio.run(_closing_this_loops_pools(coro)))
        except BaseException as exc:  # re-raised on the calling thread below, not swallowed here
            failure.append(exc)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return outcome[0]


def _expected_ids(case: EvalCase) -> set[str]:
    """The gold set of note ids this query should surface, from the case reference."""
    if case.reference is None:
        raise MetricError("retrieval metrics need a reference with `expected_note_ids`")
    raw = case.reference.get("expected_note_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        raise MetricError("reference.expected_note_ids must be a non-empty list of note ids")
    return set(raw)


# Memo of retrieved ids keyed by (corpus dir, corpus signature, query, filters), so recall and
# precision share one retrieval per case. The signature makes an on-disk corpus change a miss, which
# a long-lived process (the drift worker) needs; stale entries are dropped on insert, bounding the
# memo by the case-set size.
_RETRIEVAL_MEMO: dict[tuple[str, tuple[int, int], str, frozenset[tuple[str, str]]], list[str]] = {}


def _corpus_signature(corpus_dir: str) -> tuple[int, int]:
    """A cheap content signature of the corpus: (note-file count, newest mtime_ns).

    Stat-only over the same note set the retriever parses (`chemclaw.kg.graph.scan_notes_dir`), so
    any add, edit or delete invalidates the memo.
    """
    count = 0
    newest = 0
    for _, stat in scan_notes_dir(Path(corpus_dir)):
        count += 1
        newest = max(newest, stat.st_mtime_ns)
    return count, newest


# The retrieval path this module can actually score. Anything else is a *different* retriever, and
# reporting its quality under this metric's name would be the failure the metric exists to prevent.
_SCORED_RETRIEVER = "GraphRetriever"


def _require_scoreable_retrieval() -> None:
    """Refuse to score when the deployment's retrieval path is not the one this runs.

    This module runs one `GraphRetriever`; a deployment with `vector`/`lexical` sources or
    `retrieval_mode="hybrid"` retrieves differently, and a graph-only number under the same name
    would be a green gate on an unmeasured path. `MetricError` names the case and metric
    (`run_eval`).
    """
    extra = NOTE_INDEX_SOURCES & set(settings.data_source_list)
    if settings.retrieval_mode == "graph" and not extra:
        return
    reason = (
        f"retrieval_mode={settings.retrieval_mode!r}"
        if settings.retrieval_mode != "graph"
        else f"active source(s) {sorted(extra)}"
    )
    raise MetricError(
        f"this metric scores {_SCORED_RETRIEVER} only, but {reason} means the deployment retrieves "
        "differently — scoring the fused/derived path needs the note index built over the eval "
        "corpus (see DEFERRED.md, live-retriever drift). Refusing rather than reporting a "
        "graph-only figure under this name."
    )


def _retrieved_ids(case: EvalCase) -> list[str]:
    """Run `GraphRetriever` over the gold corpus for the case query; return the note ids.

    Reads `output.query` (required) and optional `output.filters` (type/tag). Order is preserved and
    duplicates collapsed. Memoized per (corpus, signature, query, filters).
    """
    query = case.output.get("query")
    if not isinstance(query, str) or not query.strip():
        raise MetricError("output.query must be a non-empty string")
    filters = case.output.get("filters") or {}
    if not isinstance(filters, dict):
        raise MetricError("output.filters must be a mapping if given")
    _require_scoreable_retrieval()
    corpus_dir = settings.eval_retrieval_corpus_dir
    signature = _corpus_signature(corpus_dir)
    key = (corpus_dir, signature, query, frozenset((str(k), str(v)) for k, v in filters.items()))
    ids = _RETRIEVAL_MEMO.get(key)
    if ids is None:
        retriever = GraphRetriever(corpus_dir)
        chunks = _run_sync(retriever.retrieve(query, filters))
        ids = list(dict.fromkeys(chunk.source_note_id for chunk in chunks))
        for stale in [k for k in _RETRIEVAL_MEMO if k[0] == corpus_dir and k[1] != signature]:
            del _RETRIEVAL_MEMO[stale]
        _RETRIEVAL_MEMO[key] = ids
    return list(ids)


@metric("retrieval_recall", Direction.HIGHER_IS_BETTER, live=True, gated=True)
def retrieval_recall(case: EvalCase) -> MetricResult:
    """Fraction of the gold expected sources that retrieval actually surfaced.

    Missing a relevant note is the failure this measures, so it is the gated retrieval metric
    (`retrieval_recall_min`).
    """
    expected = _expected_ids(case)
    hits = expected & set(_retrieved_ids(case))
    value = len(hits) / len(expected)
    return MetricResult(
        metric="retrieval_recall",
        value=value,
        passed=value >= settings.retrieval_recall_min,
        provenance=(
            f"recall = {len(hits)}/{len(expected)} expected sources retrieved by "
            f"{_SCORED_RETRIEVER} for query {case.output['query']!r}; "
            f"floor {settings.retrieval_recall_min}"
        ),
    )


@metric("retrieval_precision", Direction.HIGHER_IS_BETTER, live=True)
def retrieval_precision(case: EvalCase) -> MetricResult:
    """Fraction of retrieved notes that are gold-relevant — a diagnostic, not gated.

    A broad query legitimately returns many notes, so precision is context for recall; `passed` is
    None.
    """
    expected = _expected_ids(case)
    retrieved = _retrieved_ids(case)
    hits = expected & set(retrieved)
    value = len(hits) / len(retrieved) if retrieved else 0.0
    detail = (
        f"{len(hits)}/{len(retrieved)} retrieved notes were gold-relevant"
        if retrieved
        else "no notes retrieved"
    )
    return MetricResult(
        metric="retrieval_precision",
        value=value,
        passed=None,
        provenance=f"precision = {detail} for query {case.output['query']!r}",
    )
