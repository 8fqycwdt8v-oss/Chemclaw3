# `chemclaw.evals` — the eval harness and its metrics

**Responsibility:** measuring whether the system still answers the way it should. `metric.py` is the
metric interface and registry, `metrics.py` the seed metrics (importing the package registers them,
so callers resolve by name), `harness.py` runs a case-set, `retrieval.py` scores retrieval quality,
`baseline.py` compares a run against the committed baseline, `ab.py` is the tool-utility A/B.

## Code here, cases in `data/evals/`

This package holds no test case. The versioned case-set, the retrieval corpus and the committed
baseline live in `data/evals/`, pointed at by `CHEMCLAW_EVAL_CASE_DIR` and its two siblings — so a
deployment can score itself against its own cases without a rebuild.

Run it with `make eval`. A drift against the baseline is a durable job (`durable/eval_drift.py`), not
something that only happens when someone remembers to look.
