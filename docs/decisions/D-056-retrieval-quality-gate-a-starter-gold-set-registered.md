# D-056 — Retrieval-quality gate: a starter gold set + registered metrics (audit KM-13)

**Context.** KM-13 was the highest-severity knowledge-management gap: the system's core promise is
"surface the right evidence", yet `evals/` scored only *chemistry* output (E-factor, PMI, prediction,
regret) — there was no query→expected-source gold set and no retrieval metric, so a change to the
substring filter or the evidence cap could quietly halve recall with nothing to catch it. The gap
doc calls a gold set "the cheapest high-value fix, and a small corpus is the ideal time to build it."

**Decision.** Build the starter gold set and register two retrieval metrics on the existing `@metric`
seam (plan 2b.5):
- **A fixed corpus fixture** (`evals/retrieval_corpus/`, six realistic notes) — deliberately *not*
  under `knowledge_dir`, so the score is reproducible and independent of the live graph, and
  `kg-validate` (which scans `knowledge_dir`) does not treat the fixtures as real notes. The live
  `knowledge_dir` is effectively empty, so scoring against it would measure nothing.
- **`retrieval_recall` (gated) + `retrieval_precision` (diagnostic)** in a *separate* module
  (`evals/retrieval.py`, not `evals/metrics.py`) because they run `GraphRetriever` over the corpus —
  they are not pure functions of the case, so isolating them keeps `metrics.py`'s "pure function"
  invariant honest. Recall gates the "did we surface the expected sources" signal against
  `retrieval_recall_min` (config, default 0.75); precision is order-independent context (`passed`
  None). Both score `GraphRetriever` — the same source-agnostic path a report uses, and the one that
  now honors the KM-7 freshness filter.
- **Gold cases** (`evals/cases/retrieval-*.md`) pair a query with its expected source ids. Five
  cases: exact-term, broad-recall, a type-filtered query, a conditions-term query — and, on purpose,
  one query (`cross-coupling`) whose relevant Suzuki-reaction note the literal substring filter
  cannot reach, so recall = 0.5 and the gate fails **by design**. That case *measures* the KM-4
  literal-matching limitation (and documents the mitigation — the agent's query reformulation, which
  this lexical metric does not exercise) instead of leaving it anecdotal. It mirrors the existing
  eval philosophy of holding a deliberately-failing case to prove the gate fires.

**Why the test suite, not a CI hard-gate.** As with the other scientific metrics, regression gating is
the **pinned test** (`tests/test_retrieval_eval.py` pins each case's exact recall/precision/verdict),
not a red `make eval` — the CLI stays report-only and exits 0 so the by-design failing case does not
break CI. A filter/cap change that moves recall moves a pinned number and fails the test.

**Result.** `make lint type cov` green; `mypy --strict` clean; `make eval` exits 0 and renders the
retrieval rows (the one literal-miss case shows FAIL, by design). New: `evals/retrieval.py`,
`evals/retrieval_corpus/` (6 notes + README), `evals/cases/retrieval-*.md` (5 cases),
`tests/test_retrieval_eval.py`; config `eval_retrieval_corpus_dir` + `retrieval_recall_min`.
Follow-ups (recorded, not guessed): grow the gold set as the corpus grows, and add an agent-run eval
that exercises the LLM's query reformulation over the lexical layer.
