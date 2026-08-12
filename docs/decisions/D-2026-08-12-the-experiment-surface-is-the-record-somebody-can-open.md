# D-2026-08-12-the-experiment-surface-is-the-record-somebody-can-open — AG-13's backend, built from transcripts that already exist

**Status:** accepted · **Date:** 2026-08-12

**Follows:** `D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape` (LangSmith
declined) and `D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment` (the trace half,
which said in as many words that it did not close this).

## Context

AG-13 asked for "dataset versioning, run-over-run diffing and annotation" and had been blocked since
D-057 on "needs an external benchmark + a live LLM to score it". The trace half closed on 2026-08-11
with first-party OpenInference spans; the row that stayed open was the **backend** — "someone
running Phoenix against the probe transcripts so there are datasets, experiments and annotation" —
with the trigger recorded as "a live model credential plus somebody who wants the A/B answered".

**The trigger was wrong about what was missing, and that is the whole of this decision.** Every
probe run this repo has taken is already on disk. `evals/live.py` writes `{probe, outcome}` per
probe as each lands, `cli/live_probes.py` writes the judge's verdicts to `grades.json` beside them,
and `evals/live_judge.judgement_from_transcript` already rehydrates one into `(Probe, ProbeOutcome)`
because re-grading had to be possible offline. `tasks/live-test/` alone holds three arms of the same
corpus — 190 probes, a 92-probe sonnet arm, a 6-probe after-fix set — which is a run-over-run
comparison nobody could see. What was missing was not a credential. It was a reader.

## Decision

**1. Phoenix is an eval-lane deployment, in compose, not in the chart.**
`infra/docker-compose.observability.yml` runs one `arizephoenix/phoenix` container on 6006 (UI +
REST) and 4317 (OTLP/gRPC, which `CHEMCLAW_OTEL_ENDPOINT` already speaks, so no collector sits in
between). A second compose file rather than a service in the spine's: `make up` is what a developer
needs to run anything, and this is opened deliberately to ask a question about a run.

Not in the Helm chart, and that is AG-13's own scope — "the eval lane only. Non-production… do not
re-open it as [a production-tracing question]". The chart keeps pointing
`CHEMCLAW_OTEL_ENDPOINT` at a collector in an `observability` namespace it does not manage, which
stays the production story. It also keeps `tests/test_deploy_chart.py`'s pinned kind set intact: a
`StatefulSet` or `PersistentVolumeClaim` for Phoenix would fail it, and correctly so.

**Licence, unchanged from the ADR that drew the line.** The Phoenix *server image* is
Elastic-2.0 — source-available, not OSI-approved — so it is a container an operator chooses to run.
The only Phoenix code entering this tree is `arize-phoenix-client`, which is **Apache-2.0**, and
whose closure (`httpx`, `openinference-*`, `opentelemetry-sdk`) is almost entirely already here.

**2. `infra/live/processes.sh` probes 4317 and exports the OTLP settings only if something answers.**
The live lane has never had a trace destination — it set no `CHEMCLAW_OTEL_*` at all, so `otel_enabled`
was false and spans went nowhere. A probe rather than a flag because the alternative is a variable
somebody has to remember, and forgetting it produces a run with no traces and no complaint.
`CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` is deliberately *not* set: spans carry token counts, model
names and durations, and a lane that wants the prompts turns that on itself.

**3. `evals/phoenix.py` publishes an archived run, and calls no model.** A dataset example is a
probe; an experiment run is a `ProbeOutcome`; an evaluation is a judgement about one. It reuses
`judgement_from_transcript` rather than re-parsing, for the reason that function exists.

**4. The dataset is the *corpus*, not the run — and this was measured, not reasoned.** Building the
examples from the run's own transcripts is the obvious implementation and it is wrong. Measured
against a live Phoenix: publishing the 190-probe haiku arm, then the same arm again, then the
92-probe sonnet arm produced **two versions**, the newest holding 92 examples — a run that merely
*covered less* recorded as a corpus that had *lost* 98 questions. Reading `data/evals/probes/`
instead gives one dataset of 230 examples, **one version**, and three experiments of 190/190/92
runs, where an incomplete run is visible as coverage.

Examples are matched to outcomes by `probe_id` and never by position. Position works until a corpus
gains a probe, at which point every result after the insertion attaches to the wrong question and
nothing about the numbers looks wrong.

**5. An evaluation records what kind of thing made it.** The judge's verdict is
`annotator_kind="LLM"`; signals read off the outcome — `expected_tools_met`, `answered`,
`uncited_note_ids`, `failed_loudly` — are `"CODE"`. Phoenix carries the distinction natively and
this module refuses to flatten it: "a stronger model called this answer served" and "the transport
recorded no error" are not the same class of claim. Counts are scored as numbers rather than
labelled, because a second run is compared on the number.

**6. A run with no `grades.json` still publishes.** Transcripts written and judge pass not yet run
is the most common state of a fresh run, and the signals that need no grader are exactly the ones it
wants to look at.

## The measurement

Against a real Phoenix 20.1.0 — installed in a Python 3.12 venv of its own, because Phoenix needs
3.12 and this repo is 3.11, which is the topology anyway and the same thing the previous ADR found.
No docker daemon in this sandbox, so it was run natively; the compose file is the shipped form.

| publish | runs | evaluations | dataset after |
|---|---:|---:|---|
| haiku (190 probes) | 190 | 760 | 230 examples, 1 version |
| haiku again | 190 | 760 | 230 examples, 1 version |
| sonnet (92 probes) | 92 | 411 | 230 examples, 1 version |

Three experiments over one dataset version, which is the diff AG-13 asked for. The rejected
implementation, on the same data, ended at *92* examples and 2 versions — that number is why
decision 4 is written the way it is rather than as a preference.

The CLI was driven end to end (`published sonnet-arm: 92/230 corpus probes, 411 evaluations`), its
failure path returns 1 (`publish failed: no transcript directory at …`), and its printed URL was
fixed after the first run printed the base twice — `get_dataset_experiments_url` is already
absolute.

**The trace half was checked against the same backend, through the shipped path** rather than
through a hand-built exporter: `configure_telemetry()` with `CHEMCLAW_OTEL_ENABLED=true`,
`CHEMCLAW_OTEL_LLM_SPANS=true` and `CHEMCLAW_OTEL_ENDPOINT=http://127.0.0.1:4317`, two nested
`core/tracing.start_span` blocks, then read back from Phoenix's REST API: **2 spans**,
`chemclaw.turn` carrying `session_id` and `chemclaw.tool` carrying `tool_name`, and no content
attribute — which is `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` staying off, as the live lane leaves it.
So the port the probe in `processes.sh` tests for is the port that actually receives.

The compose file itself was validated with `docker compose config` (both projects render, and the
observability stack carries its own project name so `phoenix-down` cannot reach the spine's
Postgres), and the image tag was checked against the registry rather than assumed.

`tests/test_evals_phoenix.py` drives the mapping against a recorder rather than a server, so a
mapping bug and a server bug are different failures. Its fixture covers the corpus's *last* two
probes deliberately: the first two sit at example positions 0 and 1, where id-matching and
position-matching agree, and the first version of that test asserted nothing because of it — the
assertion that caught this is in the test.

## Consequences

- **AG-13's stated blocker is answered without a credential**, which is the part worth stating
  plainly: the row's exit criterion is "whether it closes AG-13's stated blocker — not whether the
  UI is nice", and datasets, run-over-run diffing and annotation now exist over data already
  committed to this repo. What a credential still buys is a *new* arm to compare, not the surface to
  compare it in.
- The eval lane emits traces for the first time. They land in the same Phoenix that holds the
  experiments, so a probe's spans and its result are one click apart.
- `arize-phoenix-client` is a direct dependency; the server is not a dependency at all. A deployment
  that never runs Phoenix imports the client and never calls it.
- Two settings ship: `CHEMCLAW_PHOENIX_BASE_URL` and `CHEMCLAW_PHOENIX_DATASET_NAME`. One dataset
  name across runs is load-bearing rather than cosmetic — a second name produces two datasets that
  cannot be diffed, which is the thing this exists to prevent.
- The A/B row that AG-13 blocks (`the plan-vs-single-shot A/B has no real task set`) is **not**
  closed by this. It needs a task set, and this needs none.
