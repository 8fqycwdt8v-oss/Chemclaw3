"""Retrieval-quality metrics over a gold query→expected-source set (audit KM-13).

The scientific metrics in `chemclaw.evals.metrics` are pure functions of a case; retrieval quality
cannot
be — "did we surface the right notes?" requires actually running retrieval over a corpus. So this
metric lives in its own module: it runs `GraphRetriever` against a small versioned gold corpus
(`eval_retrieval_corpus_dir`, a fixed fixture, not the live `knowledge_dir`) for a case's query and
scores the returned note ids against `reference.expected_note_ids`.

This is the gate the KM-13 gap names: the system's core promise is "surface the right evidence", yet
retrieval quality was previously unmeasured, so a change to the substring filter or the evidence cap
could quietly halve recall unnoticed. With this, such a regression moves a pinned number in the test
suite (as the other scientific metrics are pinned) instead of going silent. The gold set is
deliberately small — a small corpus is the ideal time to build it — and includes one query whose
relevant note the literal substring filter cannot reach, which documents (and measures) the KM-4
literal-matching limitation rather than hiding it.

**It scores `GraphRetriever`, and it now says so — loudly.** `VectorRetriever`, `LexicalRetriever`
and the RRF fusion have no coverage here, because scoring them needs the derived index built over
*this fixture corpus*, which needs Postgres; that is the "live-retriever drift" row in
`DEFERRED.md`, not something to fake. The defect worth fixing today was narrower and worse than the
gap itself: a deployment switching `retrieval_mode` to `hybrid` — the entire point of F10-A — flips
the product to an unmeasured path while this kept reporting a graph-only number under the same
metric name, so the number looked like coverage it was not. It now refuses to report rather than
mislabel, and every provenance string names the retriever behind the figure.
"""

import asyncio
from pathlib import Path

from chemclaw.core.config import NOTE_INDEX_SOURCES, settings
from chemclaw.evals.metric import EvalCase, MetricError, MetricResult, metric
from chemclaw.retrieval.retrievers import GraphRetriever


def _expected_ids(case: EvalCase) -> set[str]:
    """The gold set of note ids this query should surface, from the case reference (G4)."""
    if case.reference is None:
        raise MetricError("retrieval metrics need a reference with `expected_note_ids`")
    raw = case.reference.get("expected_note_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        raise MetricError("reference.expected_note_ids must be a non-empty list of note ids")
    return set(raw)


# Memo of retrieved ids keyed by (corpus dir, corpus signature, query, filters). Recall and
# precision are both pure functions of the same retrieved-id list, and every gold case names
# both, so without the memo each case sweeps the corpus twice per eval run for no
# informational gain. The signature makes an on-disk corpus change a natural miss: the memo
# lives for the process, and a long-lived process (the scheduled drift worker) must observe
# corpus edits rather than serve ids retrieved before them. Stale-signature entries are
# dropped on insert, so the memo stays bounded by the gold case-set size per corpus dir.
_RETRIEVAL_MEMO: dict[tuple[str, tuple[int, int], str, frozenset[tuple[str, str]]], list[str]] = {}


def _corpus_signature(corpus_dir: str) -> tuple[int, int]:
    """A cheap content signature of the corpus: (note-file count, newest mtime_ns).

    Stat-only over the same `*.md` set the retriever parses — any add, edit, or delete
    changes the count or the newest mtime, invalidating the memo without reading a byte.
    A file vanishing mid-scan (e.g. a `git pull` rewriting the tree) is simply absent,
    matching `chemclaw.kg.graph`'s fingerprint tolerance.
    """
    count = 0
    newest = 0
    for path in Path(corpus_dir).rglob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest = max(newest, stat.st_mtime_ns)
    return count, newest


# The retrieval path this module can actually score. Anything else is a *different* retriever, and
# reporting its quality under this metric's name would be the failure the metric exists to prevent.
_SCORED_RETRIEVER = "GraphRetriever"


def _require_scoreable_retrieval() -> None:
    """Refuse to score when the deployment's retrieval path is not the one this runs.

    `gather_evidence` assembles its retrievers from the data-source registry and, under
    `retrieval_mode="hybrid"`, fuses their rankings with RRF. This module runs one `GraphRetriever`
    over a fixture corpus. While those coincide — the shipped default — the number means what its
    name says. The moment a deployment turns on `vector`/`lexical` or `hybrid`, they diverge, and a
    graph-only recall reported as "retrieval_recall" is worse than no number: it is a green gate on
    a path nobody measured.

    Raising is the right failure. `MetricError` names the case and metric that triggered it
    (`run_eval`), so an operator who enabled hybrid retrieval learns their gate no longer covers
    their retriever, instead of being reassured by a figure about something else.
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

    Reads `output.query` (required) and optional `output.filters` (type/tag), scoring the same
    retrieval path a report uses. Order is preserved and duplicates collapsed, though at present
    each note yields at most one chunk. The result is memoized per (corpus, corpus signature,
    query, filters), so a case scored by both retrieval metrics runs live retrieval once, not
    once per metric — while an on-disk corpus change invalidates the memo naturally.
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
        chunks = asyncio.run(retriever.retrieve(query, filters))
        ids = list(dict.fromkeys(chunk.source_note_id for chunk in chunks))
        for stale in [k for k in _RETRIEVAL_MEMO if k[0] == corpus_dir and k[1] != signature]:
            del _RETRIEVAL_MEMO[stale]
        _RETRIEVAL_MEMO[key] = ids
    return list(ids)


@metric("retrieval_recall")
def retrieval_recall(case: EvalCase) -> MetricResult:
    """Fraction of the gold expected sources that retrieval actually surfaced (KM-13).

    Recall is the "surface the right evidence" signal — missing a relevant note is the failure
    this measures — so it is the gated retrieval metric, against `retrieval_recall_min`.
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


@metric("retrieval_precision")
def retrieval_precision(case: EvalCase) -> MetricResult:
    """Fraction of retrieved notes that are gold-relevant — a diagnostic, not gated (KM-13).

    A broad query legitimately returns many notes (low precision) without being "wrong", so
    precision reports context alongside recall rather than gating; `passed` is None.
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
