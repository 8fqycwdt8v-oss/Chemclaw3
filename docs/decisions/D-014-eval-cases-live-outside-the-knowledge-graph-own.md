# D-014 — Eval cases live outside the knowledge graph (own versioned dir, not notes)

Phase 2b's eval case-set is versioned in Git (reviewable, cited by the report) but lives under its
own `eval_case_dir` (default `evals/cases`), **not** under `knowledge_dir`. Reason: an eval case is
a structured evaluation payload (`output`/`reference` masses, predicted/actual, optimum), which the
relational note schema (`kg/note.py`: id/type/links/…) cannot carry, and putting such files under
`knowledge_dir` would make `kg-validate` reject them as malformed notes. So the metric layer parses
eval-case frontmatter directly instead of through `kg.note`. Regression gating is done by the test
suite (which pins each case's expected pass/fail), not by a CI hard-gate — because the seed set
deliberately contains a case that *fails* its gate to prove gating works. Plan Phase 2b.
