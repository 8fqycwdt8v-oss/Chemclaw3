# D-140 — A template's job step: resolved off the workflow thread, and finally able to fail

**Status:** accepted · **Context:** REV-13 and REV-17 from the agentic-system review. Both are
claims the code made about itself that were not true: a comment saying a lookup was I/O-free while
it read the filesystem, and a docstring saying the image build injected the deployment revision
while no build set it anywhere.

### The `job` step read the disk from workflow code, and could not fail

`TemplateWorkflow._run_job_step` called `connectors.registry.find_job` directly, inside
`workflow.unsafe.imports_passed_through()`. Its own comment acknowledged the registry "does
filesystem + YAML I/O on a cold process" and treated `@cache` as the mitigation. It is not one: the
cache is per worker process, so which connector, workflow type and task queue a child was started on
came from the disk of whichever worker happened to be replaying rather than from history. A worker
that came up with a different bundle set resolves the same step differently and Temporal refuses the
resulting mismatch.

**Decision:** resolve through a local activity, `resolve_job_step`. This is the pattern the repo had
already adopted one module over — `workflows.orchestrator.resolve_fan_out_limit` resolves the
fan-out bound the same way, for the same stated reason ("making the batch shape a pure function of
history"). Local rather than remote because it is a cached in-process lookup, not a network call:
the point is recording the answer, not offloading the work.

The second defect compounds with the first and is the worse of the two. `find_job` raises
`ConnectorError`, which subclasses `ValueError` — a plain exception, not an SDK `FailureError`.
Raised in workflow code, the Temporal SDK treats it as a suspected bug and suspends the workflow in
an internal task-failure retry loop that ignores the retry policy and never gives up. This is the
exact trap D-093 documented for fan-out children; nobody checked whether the template sequencer had
it too. So a template naming a job no enabled connector declares produced a run that **hung
forever** — strictly worse than one that fails, because nothing alerts and the run holds its id
against `REJECT_DUPLICATE`, so the corrected re-run is refused as a duplicate of the zombie.

Moving the lookup fixes this as a consequence: across an activity boundary the same error arrives as
an `ActivityError`, and `BAD_DATA_RETRY` lists `ValueError` non-retryable, so it fails on the first
attempt with a message naming the declared jobs. But the sequencer raises plain exceptions of its
own — an unknown step kind, an unresolvable reference — so `TemplateWorkflow` also gains
`failure_exception_types=[Exception]`. Scoped to `Exception` rather than a name list because the
classification that matters at an activity boundary is already made by `BAD_DATA_RETRY`; what this
decides is only whether the workflow may fail at all, and the answer is always yes.

`JobStep` was the one step kind no test had ever constructed, which is why both defects survived a
suite that covers the other two thoroughly.

### `deployment_revision` could never be set (REV-17)

`chemclaw/config.py` said the F6 image build injects the digest. Nothing did — not the
Containerfile, not the chart, not CI — so `settings.deployment_revision` was the literal `"unknown"`
in every deployment, and every audit record's "which version produced this result" column was a
constant. AG-14 read as met while being unmet, which is the failure mode worth naming: a GxP control
that is documented, tested, and inert.

**Decision:** a `CHEMCLAW_REVISION` build ARG exported as `CHEMCLAW_DEPLOYMENT_REVISION`, with the
image workflow passing the commit SHA.

A build ARG rather than a chart value because *the image is the thing that has a revision*. A chart
can be re-rendered against any tag, and a revision that disagrees with the running bytes is worse
than an honest "unknown". The default stays `unknown` so a local `docker build` reports truthfully
that it does not know. `envFrom` a ConfigMap does not clobber an image `ENV`, so a deployment that
genuinely wants to override it can still put the key in `.Values.config` — no chart change needed.

The commit SHA rather than the tag: a tag moves, and an audit record has to name bytes.

Pinned in two places for two different reasons. `tests/test_deploy_chart.py` checks the wiring
offline — the ARG exists, it reaches the environment under the name the settings prefix reads, and
CI passes a value — because each of those three is separately droppable and none is visible to
mypy or pytest on the Python tree. The image workflow additionally runs the built image and compares
the value, because only a built image can prove the ARG actually arrived, which is the half that was
missing when the original claim was written.
