# `chemclaw.evals` — the eval harness and its metrics

**Responsibility:** measuring whether the system still answers the way it should. `metric.py` is the
metric interface and registry, `metrics.py` the seed metrics (importing the package registers them,
so callers resolve by name), `harness.py` runs a case-set, `retrieval.py` scores retrieval quality,
`baseline.py` compares a run against the committed baseline, `ab.py` is the tool-utility A/B, and
`delegation.py` is the three-arm delegation comparison built on it — quality through `ab.py`'s own
noise floor, billed tokens and wall clock reported beside it rather than folded in, because
"cheaper but worse" and "better but slower" are different answers that one number hides.

`hypothesis_tournament.py` is the odd one out and says so: it measures an **instrument** rather
than the system's answers. Given a judge of a stated accuracy, does Swiss pairing plus the
Bradley-Terry fit recover a known ordering, and by how much does it beat not ranking at all? The
ground truth is constructed, so it needs no model and no credential and runs in CI
(`make hypothesis-recovery`) — and so it cannot say whether a *language model* judging real
chemistry is an accurate judge. `backtest_shape()` states the corpus backtest that would settle
that and records that it has never run, for `delegation.py`'s reason.

## Code here, cases in `data/evals/`

This package holds no test case. The versioned case-set, the retrieval corpus and the committed
baseline live in `data/evals/`, pointed at by `CHEMCLAW_EVAL_CASE_DIR` and its two siblings — so a
deployment can score itself against its own cases without a rebuild.

Run it with `make eval` (`make eval-strict` to gate on a regression). The baseline comparison has two
front ends over the same pure logic in `baseline.py`: `make eval-baseline-check` runs it offline and
exits non-zero on a *worsening* move, and `durable/eval_drift.py` schedules it so a drift is not
something that only happens when someone remembers to look. The offline one declares which case-set
it scored (`--case-set-version`) and refuses to compare across two of them.
