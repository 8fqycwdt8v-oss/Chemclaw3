"""The evaluation & metric layer (plan Phase 2b, F10-F2).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class EvalSettings(BaseSettings):
    """The evaluation & metric layer (plan Phase 2b, F10-F2).

    Grouped because a metric's pass/fail threshold is config, never hardcoded (G3): the case-set
    locations, the green-chemistry gates, the A/B noise floor, the drift job, and the
    retrieval-quality gate all live here.
    """

    # A metric is a pure function; the green-chemistry limits are dimensionless (kg waste or
    # input per kg product) and process-dependent — these defaults are lenient gate values, tune
    # them per chemistry. Versioned eval case-set. Its own directory, not under `knowledge_dir`:
    # an eval case is a structured evaluation payload (output/reference), not a relational note,
    # so it neither uses the note schema nor passes through kg-validate.
    eval_case_dir: str = "data/evals/cases"
    eval_efactor_max: float = 50.0
    eval_pmi_max: float = 50.0
    # Absolute error (in the prediction's own unit, e.g. log S) still counted as an accurate
    # prediction against a held-out reference.
    eval_prediction_tolerance: float = 1.0
    # Noise floor for the per-task tool-utility A/B (plan step 2b.4): a metric delta within +/-
    # this magnitude counts as "no effect", so tool augmentation is only credited (or blamed)
    # for changes above measurement noise. One global scalar — a comparison does not know which
    # metric produced its scores, so set it to the noisiest metric's floor (per-metric floors
    # need a per-metric parameter first). The default is a small floating-point floor so runs
    # differing only by rounding register as "no effect" (a 0.0 default made *every*
    # non-exact-tie helped/hurt, defeating the band); raise it to the actual measurement noise
    # of the metric a given case-set exercises.
    eval_ab_epsilon: float = Field(default=1e-6, ge=0.0)
    # Eval drift detection (plan F10-F2). A `background-jobs` workflow re-runs the committed
    # case-set on a cadence and alerts when an aggregate metric moves further than a *relative*
    # band (`eval_drift_epsilon` × the baseline value) from the Git-committed baseline
    # (`data/evals/baseline.json`). Relative, so one knob is scale-appropriate across metrics of
    # different magnitudes (an `f1` in [0, 1] vs an `e_factor` near 35); 0.05 = a 5%
    # proportional move. Off by default; enabling it adds the Schedule (D-035).
    eval_drift_enabled: bool = False
    eval_drift_schedule_minutes: int = Field(default=1440, ge=1)
    eval_drift_epsilon: float = Field(default=0.05, ge=0)
    # The drift-check activity's own timeout (not borrowed from the memory job's): five pinned
    # cases score in well under this, but a dedicated knob keeps the two jobs' timeouts
    # independent.
    eval_drift_timeout_seconds: float = Field(default=300.0, gt=0)
    eval_baseline_path: str = "data/evals/baseline.json"
    # Live probes (AG-13): questions asked of a *running* system over the HTTP/SSE front door,
    # against a real model. Their own directory, not `eval_case_dir`: a probe is an input to a
    # conversation that has not happened yet, while an eval case is output already produced —
    # the two are scored by different machinery and must not be loaded by one another's reader.
    live_probe_dir: str = "data/evals/probes"
    # Where the front door is, for the probe runner. Separate from `service_host`/`service_port`
    # (which bind a server) because the runner is a *client* and is routinely pointed at a
    # deployment it did not start.
    live_probe_base_url: str = "http://127.0.0.1:8000"
    # The bearer the probe runner presents. Empty against a dev-posture front door, which reads no
    # Authorization header at all; set when the lane runs with `entra_required=true`, where every
    # probe is otherwise a 401 before a single turn starts.
    #
    # A token rather than a tenant/client/secret triple, because the runner is not an OAuth client
    # and should not become one: whoever starts the lane already has to mint an identity with the
    # roles the probes need (the expensive-job probes need a privileged one), so the only thing
    # this needs to know is the result. `infra/live/processes.sh` mints it and exports this.
    live_probe_token: str = ""
    # One turn's ceiling. Generous: a probe that triggers an inline calculation legitimately
    # waits, and cutting it short would record a system timeout as a model failure.
    live_probe_timeout_seconds: float = Field(default=300.0, gt=0)
    # Concurrent probes in flight. Bounded because every probe shares one front door, one
    # Postgres and one upstream model account; the point of the run is the system's behaviour,
    # not its rate limit.
    live_probe_concurrency: int = Field(default=4, ge=1)
    # Where transcripts land. Every probe writes one file: the full event stream is the evidence
    # a finding cites, and a finding whose reproduction is not on disk is prose.
    live_probe_transcript_dir: str = "tasks/live-test/transcripts"
    # The judge that grades an answer against its probe's `direction`. Deliberately a different,
    # stronger model than the agent under test: grading is where model quality buys the most,
    # and it is one call per probe against the agent's many.
    live_probe_judge_model: str = "claude-sonnet-5"
    # The judge's own output ceiling. It must clear a verdict plus a reason plus a claims array
    # comfortably: at 1024 the reply was truncated mid-JSON on long answers and the parse failure
    # was recorded as a verdict of `unserved`, mislabelling 65 of 190 probes in the first run.
    live_probe_judge_max_tokens: int = Field(default=4096, gt=0)
    # The M12 re-validation suites (plan gate, durable-launcher ordering, team routing). Their own
    # directory *under* the corpus, not beside it: `load_probes` globs one level, so a subdirectory
    # is invisible to `make live-probes` — which is the point. These probes are scripted
    # conversations and routing keys scored by their own suites, and folding them into the
    # 190-question corpus would change what that run measures without changing what it reports.
    live_m12_probe_dir: str = "data/evals/probes/m12"
    # How long to wait for a turn's row to appear in `turn_costs`. The ledger's write is scheduled
    # rather than awaited (D-130), so it lands shortly after the stream this harness reads closes;
    # this bounds the wait rather than expressing an expectation about it. Exceeded, the turn is
    # recorded as *unmeasured* rather than as free.
    live_probe_cost_wait_seconds: float = Field(default=5.0, gt=0)
    # Where an archived probe run is published so it can be diffed against the next one (AG-13,
    # `D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment` left this half open).
    #
    # **A URL rather than a switch, and it points at localhost.** Phoenix is a container an
    # operator runs beside the eval lane, not a dependency of this system: the publisher is a
    # client, so the only thing this repo needs to know is where that process is. The default is
    # the eval lane's own — nothing is published anywhere until somebody runs `make phoenix-up`
    # and then the CLI, which is the same posture `live_probe_base_url` takes toward the front
    # door it points at.
    phoenix_base_url: str = "http://127.0.0.1:6006"
    # The dataset an archived run is published *into*. One name across runs on purpose: Phoenix
    # versions a dataset when its examples change and hangs every experiment off it, so
    # re-publishing the same probe set under the same name is what makes two runs comparable.
    # A second name would produce two datasets that cannot be diffed, which is the whole ask.
    phoenix_dataset_name: str = "chemclaw-live-probes"
    # Retrieval-quality gate (audit KM-13). A gold query→expected-source set scores
    # `GraphRetriever` over this fixed corpus fixture (a small versioned set of notes, NOT the
    # live `knowledge_dir`, so the score is reproducible). `retrieval_recall_min` is the floor
    # the "did we surface the expected evidence?" recall metric gates against — the seam that
    # catches a substring-filter or evidence-cap change quietly dropping recall.
    eval_retrieval_corpus_dir: str = "data/evals/retrieval_corpus"
    retrieval_recall_min: float = Field(default=0.75, ge=0.0, le=1.0)
    # Autonomy gates (F9-T3). These score a *scripted* transcript, so they measure the harness's
    # plumbing — that a plan is produced, that work is closed before answering, that the A/B
    # arithmetic holds — and never the model's judgment, which needs the live endpoint AG-13 is
    # waiting on. `eval_plan_quality_min` is deliberately below 1.0: a plan that names an extra
    # reasonable step is not a regression, while dropping a required one is. `eval_runaway_max` is
    # 0.0 because the pinned turns are scripted to finish; any runaway among them is a plumbing
    # break, not a hard case.
    eval_plan_quality_min: float = Field(default=0.8, ge=0.0, le=1.0)
    eval_runaway_max: float = Field(default=0.0, ge=0.0, le=1.0)
