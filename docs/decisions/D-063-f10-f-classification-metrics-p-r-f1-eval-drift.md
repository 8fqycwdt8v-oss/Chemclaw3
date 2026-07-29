# D-063 — F10-F: classification metrics (P/R/F1) + eval drift detection (D-A14)

**Context.** The eval harness scored green-chemistry/prediction metrics with absolute-error
tolerances; it had no precision/recall/F1 and no drift detection.

**Decision.** `evals/metrics.py` adds `precision`/`recall`/`f1` over `output.predicted_note_ids`
vs `reference.expected_note_ids`, sharing one pure `precision_recall_f1` (report/drift metrics, no
per-case gate). `evals/baseline.py` (`aggregate_metrics`/`detect_drift`, committed
`evals/baseline.json`) + `workflows/eval_drift.py::EvalDriftWorkflow` (background-jobs, alerts via
the notify seam) re-run the case-set on an opt-in Schedule and flag any metric that moved past
`eval_drift_epsilon`. Live *retriever* scoring is not re-invented here: the merge with the
audit-hardening line adopted its KM-13 gold-set (D-056) — `retrieval_recall`/`retrieval_precision`
over a committed fixture corpus — as the corpus-backed retrieval measure; the earlier
one-caller `run_retrieval_eval` driver was dropped as redundant (KISS). A pinned static
`precision`/`recall`/`f1` case (`retrieval-precision-recall.md`) keeps those generic metrics under
the versioned case-set and gives drift a number to watch.

**Consequence.** Retrieval/extraction quality is measurable as P/R/F1 on versioned cases; a silent
regression trips a scheduled alert. Over the deterministic committed case-set the scheduled job is a
deployment-consistency tripwire; live drift over the deployment's own graph stays deferred
(DEFERRED.md). Drift is off by default.

**Result.** `make lint type test` green. Tests: `test_metrics_classification`, `test_eval_drift`
(incl. a baseline-matches-case-set guard), `test_schedules`, `test_config`; the KM-13 gold-set is
pinned by `test_retrieval_eval` (D-056).
