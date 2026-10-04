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

`answer_shape.py` measures what recorded live answers are made of — tables, document-shaped prose,
structure lists and `render_structure` calls — over the outcome JSON `live.py` writes. It is the
measurement behind `D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect` and one of that
decision's `Revisit when:` lines is a re-run of it (`python -m chemclaw.evals.answer_shape
tasks/live-*`).

The live lane: `probe.py` is the live-probe declaration (one question and how to tell whether the
answer served it), `live.py` asks a running front door those questions and records what it did,
`live_judge.py` grades an answer against its probe's `direction` with a model as judge,
`tool_utility.py` turns two graded answers into the tool-utility A/B, `delegation_run.py` is the run
half of the delegation experiment, and `phoenix.py` publishes an archived probe run to Phoenix.
`autonomy.py` scores whether the *harness* behaved over a scripted transcript.

## Code here, cases in `data/evals/`

This package holds no test case. The versioned case-set, the retrieval corpus and the committed
baseline live in `data/evals/`, pointed at by `CHEMCLAW_EVAL_CASE_DIR` and its two siblings — so a
deployment can score itself against its own cases without a rebuild.

Run it with `make eval` (`make eval-strict` to gate on a regression). The baseline comparison has two
front ends over the same pure logic in `baseline.py`: `make eval-baseline-check` runs it offline and
exits non-zero on a *worsening* move, and `durable/eval_drift.py` schedules it so a drift is not
something that only happens when someone remembers to look. The offline one declares which case-set
it scored (`--case-set-version`) and refuses to compare across two of them.
