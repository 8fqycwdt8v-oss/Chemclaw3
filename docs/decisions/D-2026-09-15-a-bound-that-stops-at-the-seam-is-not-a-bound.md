# D-2026-09-15-a-bound-that-stops-at-the-seam-is-not-a-bound — the two template bounds that did not bound

**Status:** accepted · **Date:** 2026-09-15 · Revisits
`D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` (whose cap this extends to the seam it
never reached) and `D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only` (whose placement
argument is why the cap could not reach it).

## Context

An analysis of how well this system composes multi-tool workflows found the template seam sound
where it is argued and shallow in two bounds that are stated and do not hold. Both are the same
shape: a ceiling that is enforced on one path and named — in a docstring, in a validator — as
though it held on both.

**1. Nothing bounds what a template step puts in front of a model.** `bound_tool_results` is an
entry of `tool_call_middleware`. A template `tool` step runs through `invoke_governed`, which folds
`tool_governance_middleware` — the same chain minus the three entries that exist to serve a model,
deliberately, because a `tool` step has no model. That is correct for the step and silently wrong
for the *next* one, whose prompt interpolates that unbounded result through `${steps.<id>.result}`
and does have a model. Measured on one payload through both paths:

| | characters |
| --- | ---: |
| raw step result | 245,688 |
| what a chat turn hands its model (`agent_max_tool_result_chars`) | 60,000 |
| what a template `agent` step handed its model | **245,700** |

Nothing downstream could reclaim it either, which is what makes the hole the expensive kind. The
step's graph gets `agent/compaction.py` like any turn, and both of its edits are for *history* —
`ClearToolUsesEdit` clears tool results, the conversation window drops old turns. A step is one
`HumanMessage` with no history at all, so a prompt over the budget is unreducible by construction:
it ticks `chemclaw_context_unreducible_total` and goes out whole.

**2. The run ceiling covers one step, and the validator that says so is the one that reads it.**
`_the_template_run_ceiling_covers_one_step` checks `template_run_timeout_seconds` against the
longest *single* step, and its own docstring concedes the narrowing: *"how many steps a template has
is a property of a YAML file this object cannot see."* Measured on the shipped defaults, one `job`
step's ceiling is 39,330 s against a run ceiling of 45,330 s — so the validator passes and two
`job` steps in one file miss by 33,330 s. What that costs is why it matters more than the arithmetic
suggests: a workflow *execution* timeout is not delivered to workflow code, so `TemplateWorkflow`'s
`except BaseException -> _notify_failure` never runs. No failure row, no push-back, nothing on the
session stream. The run simply stops. Every other way a template can fail says so somewhere.

## Decision

**A bound is enforced at the edge it names, and the reader that can see the whole procedure is the
one that checks it.**

- `durable/template_activities.bounded_prompt` cuts an `agent` step's prompt to
  `agent_max_tool_result_chars` — the same ceiling, because it is this system's one answer to "how
  much text may reach a model in one blob" and a prompt is a blob. A second setting would be a
  second ceiling nobody could reason about against the first.
- `agent/template_surface.run_ceiling_problems` sums the step ceilings a *file* declares and
  refuses a procedure the run ceiling cannot hold. Read by `make template-validate` and by
  `registry.unrunnable_reason`, so such a file is refused at the gate and again at launch.
- `Settings.template_step_ceilings` is the one definition of what a step of each kind may take.
  The config validator asks it for the maximum, the template gate for the sum over a file's steps.

**In the activity and not in the sequencer**, which is the placement decision rather than a
detail. Cutting in workflow code would put a mutable setting into an activity *argument*, and an
argument is recomputed on replay while a result is read from history — so a deployment that lowered
the ceiling between an execution and its replay would fail the run with a non-determinism error
rather than bound anything. It is also where `agent/durable_tools.py` already puts this class of
rewrite, for a reason that reads the same here: the envelope belongs to the model's context, so it
belongs at the model's edge.

**`_notice` takes its remedy as a parameter, because the wrong advice is worse than none.** Every
sentence in that notice is true of any cut except the last, which assumes the model *asked* for the
text and can ask for less. True of a tool result and of a `task` report; false here, where the
prompt was interpolated by a reference in a file the model cannot see and did not write — telling
it to narrow its question sends it to re-fetch what the step was handed. `TOOL_REMEDY` and
`STEP_REMEDY`; the width measurement takes the same argument, so a longer remedy tightens the cut
instead of escaping it.

**Head and tail is what makes cutting a *prompt* safe at all.** A template prompt is instructions,
then interpolated data, then instructions — read `tautomer-resolution`, whose last four sentences
are the whole judgment the step exists for. `_HEAD_SHARE` keeps both ends, so a cut costs the data
that was already too large to read and never the ask.

## What was measured rather than assumed

- The prompt defect, end to end: 245,688 characters in, 245,700 out, against a 60,000 ceiling.
  After: 60,000, both ends intact, the notice present and marked.
- The same thing on the shape `tautomer-resolution` actually interpolates — a `rank_species`
  result of 4,000 species through that template's own `report` prompt — because a synthetic
  payload proves the arithmetic and not the exposure:

  | | characters | estimated tokens |
  | --- | ---: | ---: |
  | before | 257,816 | 64,458 |
  | after | 60,000 | 15,004 |

  **64,458 tokens is the part worth reading.** It is a single step's prompt, against an
  `agent_context_token_budget` derived downwards from a 128k window, on top of a static prefix the
  ratchet bounds at 67,200 plus what the sibling fleet serves. The step did not merely exceed its
  thread allowance; with the prefix it did not fit in the window at all — and, being one
  `HumanMessage`, it was the one shape neither compaction edit can reduce.
- Every shipped template's declared ceiling, against 45,330 s: `degradant-triage` and
  `hazard-briefing` 2,700 s; `ensemble-free-energy` and `regioselectivity-in-conformer` 40,230 s;
  the other five 41,130 s. **All nine fit, with 4,200 s of headroom on the longest.** So the second
  defect is latent rather than live — which is exactly when a bound is worth adding, because the
  first template that deepens one is the one that finds out.
- The two job steps that do not fit: 79,560 s against 45,330 s, refused by name at the gate and at
  the launcher.
- `template_step_ceilings` before and after the extraction: `tool` 900, `agent` 900, `job` 39,330 —
  identical to the arithmetic it replaced, so the refactor moves no ceiling.
- `_notice` for a tool result is byte-identical to what it was, which is what makes the `remedy`
  parameter a widening rather than a change.

## What this deliberately does not do

**Parallel or fan-out steps.** `D-2026-08-25-the-loop-is-a-composite-not-a-template` places the
loop in a composite on the MCP side and the sequence in the template, and `durable/template_job.py`
is one `await` per step because of it. Adding `gather` is a new decision about that split, not a
missing part of this one. The sum in `run_ceiling_problems` is written on the sequential reading
and would have to be revisited with it.

**Agent-authored templates.** `D-2026-08-12`'s plan-gate exemption for a template `agent` step
holds *because* nothing at run time can create a template. An agent that could author one, with a
`write_tools:` line, would be granting itself an ungated write path — `SkillsReadOnlyRefusal` is
the same argument one seam over. Any future "the agent composes a workflow" feature has to
re-attach the plan gate or refuse; it cannot simply add a writer.

**Resume from a failed step.** A failed run restarts at step 1: `failed_template_record` keeps the
completed steps' results and nothing reads them back. The reason it is not now is a measurement
rather than a preference — `tool` and `job` steps re-run through `cached_compute`, so D-011 makes
most of a retry a cache hit, and the exposure is the agent step's tokens. `docs/planning/BACKLOG.md`
carries the row and the trigger.
