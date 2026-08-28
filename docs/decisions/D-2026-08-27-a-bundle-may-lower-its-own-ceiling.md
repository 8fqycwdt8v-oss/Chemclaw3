# D-2026-08-27-a-bundle-may-lower-its-own-ceiling — a job declares what it costs, the deployment keeps the maximum

## Status

Accepted. Closes the `BACKLOG.md` row "`connector_job_timeout_seconds` bounds a 20-second job and a
4-hour job identically". Implemented in this commit; no shipped manifest declares the new key, and
§"What ships, and what does not" says why that is the honest state rather than an oversight.

## Context

`ConnectorJobWorkflow` gives its child one execution timeout,
`settings.connector_job_timeout_seconds`, and that number has always been the whole story for every
durable job in the fleet. Its comment states the reasoning that kept it a single global:

> Deliberately one global ceiling rather than a per-manifest field: a bundle in the repo must not be
> able to grant itself unlimited runtime — that is a deployment's call.

That is right about *direction* and was over-applied to *both* directions. What it costs is that one
number bounds a twenty-second job and a four-hour job identically. The default is sized off the
longest thing in the fleet — 18,000 s, being a CREST search at `xtb_job_timeout_seconds` plus an
hour, after `D-2026-08-26-semiempirical-is-the-whole-tier` retired the 24 h DFT poll the previous
90,000 s was sized for. So with a bundle's worker down, a job that would have answered in seconds
sits `running` for up to five hours and says nothing, because the only thing that would end it is a
number chosen for something else. Shrinking the global does not fix this; it just moves which job is
mis-bounded.

## Decision

`JobSpec` gains an optional `timeout_seconds`, and the ceiling one child actually gets is

```
min(declared, connector_job_timeout_seconds)
```

computed in one place, `durable/connector_job.py::child_execution_timeout`.

**A bundle may lower its own ceiling and may never raise it.** The asymmetry is the entire safety
argument and it is what makes the field admissible at all: a manifest lives in this repository, so a
key that could raise a ceiling would be a capability granting itself runtime the operator never
funded — exactly what the original comment refused. A `min` gives a bundle the only power that is
safe to hand it. A declaration above the setting is clamped rather than obeyed, so an operator who
lowers `connector_job_timeout_seconds` still bounds every job in the fleet, including the ones that
asked for more.

**Absent is the identity.** `None` resolves to the setting unchanged, which is what every manifest
written before this key existed gets, and what every Temporal history in flight decodes to.
`tests/test_connector_job_workflow.py::test_a_job_that_declares_nothing_is_bounded_exactly_as_it_was_before`
pins that separately from the `min`, because it is the compatibility claim rather than the semantic
one.

**The declared number travels on the wire, not the resolved one.** `ConnectorJobInput` carries what
the manifest said and `child_execution_timeout` applies the `min` in the worker that is about to
start the child. Carrying the already-resolved value would freeze a deployment's ceiling into the
payload at launch, so lowering `connector_job_timeout_seconds` would bind only jobs launched
afterwards and not the ones already queued behind a wedged worker — which is the population the
lowering is usually for.

**A workflow cannot read a `connector.yaml`, so the copy is made at the launch site.** Both launch
sites, and that is deliberate: `connectors/jobs.py` (a chat turn) and `ResolvedJob` /
`TemplateWorkflow._run_job_step` (a template step). A field the template path drops is a field that
quietly means something else on that path — the standing example being the session and correlation
ids that path dropped for as long as it existed, which made every template-launched failure silent.

## What was decided against

**`connector-validate` gains no check.** Three candidates were considered and each fails on its own
terms:

- *Declared above the setting* — clamped by design, not a defect. A validator firing on it would
  fire on the normal case for any deployment that lowers the global below a bundle's honest
  self-estimate, which is the "an alarm that fires on the normal case teaches people to disable it"
  trap `manifest.py::_a_networked_endpoint_carries_a_credential` already names.
- *Declared below the longest activity the job's own child runs* — the one genuinely dangerous
  direction, and invisible to core, which can see neither the bundle's workflow body nor its
  activity budgets. It is stated as a rule in the field's own comment instead of being claimed as a
  control, because `map_to_hpc_identity` is what a control nobody can run looks like.
- *A floor* — there is none to write. A bundle whose job really costs two seconds may honestly
  declare two seconds, so any constant would be an invention.

What remains is total and is enforced where a bundle author meets it: `gt=0` refuses zero and
negative, `extra="forbid"` refuses a typo, and `registry._load_manifest` wraps the failure with the
path of the file it read, which is what makes a manifest problem a fail-closed startup error
somebody can act on.

**`wrapper_execution_timeout` stays the global number.** It exists to be *strictly above* whatever
the child gets, so that the four post-child steps — the durable record, the results-store publish,
the PR-gate note, the session push-back — have headroom, since an execution timeout is not delivered
to workflow code and a wrapper that expires first fails in complete silence
(`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed`). A declared ceiling can only lower
the child's, so the global value clears every one of them by construction. Deriving the wrapper from
the job's own number would shrink it in step with the child and hand back exactly the headroom it
was written for.

**`_the_job_ceiling_covers_the_activity_it_bounds` is untouched.** It relates two *settings* — the
global ceiling against the max over `xtb_job_timeout_seconds` and
`result_republish_timeout_seconds` — and a `min` cannot raise the global, so the invariant holds
unchanged. The per-job form of it is the rule in the bullet above, which core cannot check.

## What ships, and what does not

**No shipped bundle declares the key, and that was measured rather than assumed.** Every job in the
tree was checked against the longest activity its own workflow starts:

| Bundle | Job | Longest activity under it |
| --- | --- | --- |
| `calc` | all nine | one `run_xtb_calculation`, `xtb_job_timeout_seconds` = 15,000 s — the *same* budget for every job, because they share `CalcJobWorkflow` |
| `results` | `republish_calculations` | `result_republish_timeout_seconds` = 14,400 s |
| `bo` | `start_optimization_campaign` | 300 s, but `n_rounds` × 2 activities and a continue-as-new chain, bounded only by `bo_max_rounds` = 500 |

Against an 18,000 s ceiling, none of these can honestly declare less. `calc` is the interesting one
and the finding worth carrying: a per-*job* ceiling cannot help `calc` today because its per-*job*
cost is not expressed anywhere either — all nine jobs share one activity budget sized for the
worst of them, so `compute_reaction_energy` over two small species is bounded by a CREST search's
number twice over. Making that bundle's short jobs genuinely short is a change to
`connectors/calc/workflows.py`'s activity budget, not to its manifest, and it is a separate
decision.

So the mechanism ships complete and total — every launch on both paths passes through
`child_execution_timeout` — with no bundle currently exercising it. A token declaration on a job
that cannot honestly make one would be worse than none: it would cut that job's own activity short,
which is precisely the defect the global invariant exists to prevent, reintroduced one level down
and out of sight of the validator that catches it.

## Consequences

- A bundle whose job is genuinely short can now say so, and a wedged or unserved queue costs that
  job its own budget rather than the fleet's.
- The operator's maximum is unchanged and unchangeable from a manifest, in either direction.
- One more field crosses the Temporal wire; additive and defaulted, like `plan_step` and
  `calc_refs` before it, so histories in flight decode.
- A bundle author now owns a number core cannot check. The comment on the field says so in those
  terms, and says what going too low costs.
