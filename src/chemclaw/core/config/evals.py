"""Settings for the evaluation and metric layer.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class EvalSettings(BaseSettings):
    """The evaluation & metric layer (plan Phase 2b, F10-F2).

    Grouped because a metric's pass/fail threshold is config, never hardcoded (G3): the case-set
    locations, the green-chemistry gates, the A/B noise floor, the drift job, and the
    retrieval-quality gate all live here.
    """

    # Versioned eval case-set. Not under `knowledge_dir`: a case is an evaluation payload, not a
    # note. The green-chemistry limits below are dimensionless (kg waste or input per kg product)
    # and lenient; tune them per chemistry.
    eval_case_dir: str = "data/evals/cases"
    eval_efactor_max: float = 50.0
    eval_pmi_max: float = 50.0
    # Absolute error (in the prediction's own unit, e.g. log S) still counted as accurate.
    eval_prediction_tolerance: float = 1.0
    # Noise floor for the per-task tool-utility A/B: a delta within +/- this counts as "no effect".
    # One global scalar, so set it to the noisiest metric's floor; the default only absorbs float
    # rounding.
    eval_ab_epsilon: float = Field(default=1e-6, ge=0.0)
    # Price of a cached token relative to an input token, for `turn_cost_ratio`; a provider's price
    # list, so configurable. Defaults are Anthropic's (read 0.1, write 1.25). The metric scores
    # billed tokens so a new cache breakpoint does not read as a regression.
    eval_cache_read_weight: float = Field(default=0.1, ge=0.0)
    eval_cache_write_weight: float = Field(default=1.25, ge=0.0)
    # Eval drift detection: a `background-jobs` workflow re-runs the committed case-set and alerts
    # when an aggregate metric moves more than `eval_drift_epsilon` x baseline from
    # `data/evals/baseline.json`. Relative, so one knob fits metrics of different scale. Off by
    # default; enabling adds the Schedule.
    eval_drift_enabled: bool = False
    eval_drift_schedule_minutes: int = Field(default=1440, ge=1)
    eval_drift_epsilon: float = Field(default=0.05, ge=0)
    # The drift-check activity's own timeout.
    eval_drift_timeout_seconds: float = Field(default=300.0, gt=0)
    eval_baseline_path: str = "data/evals/baseline.json"
    # Live probes: questions asked of a running system over the HTTP/SSE front door against a real
    # model. Separate from `eval_case_dir` because a probe is an input, a case is a produced output.
    live_probe_dir: str = "data/evals/probes"
    # Front-door URL for the probe runner, a client often pointed at a deployment it did not start.
    live_probe_base_url: str = "http://127.0.0.1:8000"
    # Bearer the probe runner presents; empty against a dev-posture front door, required under
    # `entra_required`. A token rather than OAuth client credentials: `infra/live/processes.sh`
    # mints it. Privileged, hence a `SecretStr` in `_SECRET_SETTINGS`.
    live_probe_token: SecretStr = SecretStr("")
    # One turn's ceiling; generous so an inline calculation is not recorded as a model failure.
    live_probe_timeout_seconds: float = Field(default=300.0, gt=0)
    # Concurrent probes; they share one front door, Postgres and model account.
    live_probe_concurrency: int = Field(default=4, ge=1)
    # Where transcripts land; each probe writes one, the evidence a finding cites.
    live_probe_transcript_dir: str = "tasks/live-test/transcripts"
    # The judge model is routed by `model_routes["live-probe-judge"]`, ideally stronger than the
    # agent; `evals/live_judge.py` warns when unset. This is its output ceiling, which must fit a
    # verdict, a reason and a claims array without truncating the JSON.
    live_probe_judge_max_tokens: int = Field(default=4096, gt=0)
    # Characters of each tool result shown to the judge, for results small enough to ride the stream
    # whole (`stream_inline_result_bytes`), instead of the 200-character preview. 0 shows previews
    # only.
    live_probe_judge_result_chars: int = Field(default=4096, ge=0)
    # The M12 re-validation suites (plan gate, durable-launcher ordering, team routing). A
    # subdirectory of the corpus so `load_probes` (one level) does not fold them into `make
    # live-probes`.
    live_m12_probe_dir: str = "data/evals/probes/m12"
    # How long `make live-jobs` waits for a launched workflow to finish before judging it; separate
    # from a turn's `inline_wait_seconds`. Covers a cold worker's first quick xTB job.
    live_jobs_terminal_wait_seconds: float = Field(default=180.0, gt=0)
    # How long the delegation runner waits for a turn's `turn_costs` row, which is written off the
    # hot path and is eventually consistent. `evals/delegation_run.billed_by_session_when_booked`
    # polls within this bound; a row absent after it is reported as a hole.
    eval_delegation_ledger_wait_seconds: float = Field(default=10.0, ge=0)
    # The vendored benchmark `make live-benchmark` scores; a directory so `dataset.json` (licence,
    # checksum, provenance) sits beside the questions.
    benchmark_dir: str = "data/evals/benchmarks/chembench"
    # Where an archived probe run is published for diffing. Phoenix is a container an operator runs
    # beside the eval lane (`make phoenix-up`), not a dependency; nothing is published until then.
    phoenix_base_url: str = "http://127.0.0.1:6006"
    # Phoenix dataset an archived run is published into; one name across runs so runs are
    # comparable.
    phoenix_dataset_name: str = "chemclaw-live-probes"
    # Retrieval-quality gate: a gold query→source set scores `GraphRetriever` over this fixed corpus
    # (not the live `knowledge_dir`, so the score is reproducible).
    eval_retrieval_corpus_dir: str = "data/evals/retrieval_corpus"
    # Recall floor for the retrieval gate. Must sit strictly above `max((n-1)/n)` over the gated
    # gold sets so one lost note fails; `tests/test_retrieval_eval.py` asserts that inequality.
    retrieval_recall_min: float = Field(default=0.80, ge=0.0, le=1.0)
    # Autonomy gates over scripted transcripts: they test harness plumbing, not model judgment. Plan
    # quality is below 1.0 because an extra reasonable step is not a regression; runaway is 0.0
    # because the scripted turns always finish.
    eval_plan_quality_min: float = Field(default=0.8, ge=0.0, le=1.0)
    eval_runaway_max: float = Field(default=0.0, ge=0.0, le=1.0)
