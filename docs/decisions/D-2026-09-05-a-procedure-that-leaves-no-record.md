# D-2026-09-05-a-procedure-that-leaves-no-record — the capture half, and the publisher not to build

**Status:** accepted · **Date:** 2026-09-05

## Context

The retrieval half of the knowledge loop was fixed in
`D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker`, which closed with a plain
statement of what it had *not* done: **data is captured automatically, conclusions are not.** This
is that half.

Four claims from the review were re-verified against `HEAD` before anything was built, because the
tree had moved twice since they were written. All four held.

## What was wrong

**Nine shipped `run_*` procedures wrote no durable record at all.** `record_job` had exactly one
caller in the tree — `durable/connector_job.py` — so a template run left no `job_records` row. It
was therefore never findable by `find_past_jobs`, and `get_durable_job_status` answered for its id
only until Temporal retained its history away. A failing run left nothing anywhere, which is the run
somebody actually goes looking for. `hazard-briefing` makes it concrete: its entire product is a
chemist-facing brief, and the brief was unrecoverable the moment the conversation closed.

Two docstrings in `agent/durable_tools.py` asserted the opposite in the present tense — that status
"answers for finished jobs indefinitely", and that `find_past_jobs` is "the retrospective view over
every campaign, calculation and report job that has ended". Both were true of connector jobs alone.

**A correction was recorded as a confirmation.** `memory/interaction.py` rendered every body as
`A (confirmed):`, while that module's docstring, the tool's docstring and the system prompt all said
"confirmed **or corrected**". So the one case where the system was demonstrably wrong — the
highest-value thing a chemist ever hands it, and the only place that fact exists — went into the
record as agreement.

## Decision

`TemplateWorkflow` records on both paths, through the same `record_job` activity on the same queue,
best-effort in the same sense the connector wrapper means it: a finished run is finished, and losing
its row must not undo the work.

`JobRecord.rationale` relaxes from `min_length=1` to a documented empty. **This does not weaken the
connector-job contract, because that contract was never enforced there**: `connectors/jobs.py`
refuses a blank rationale at the launcher, with a message written for the model, and its own comment
says the check belongs there. Empty now means "a declared procedure, launched by name" — the reading
`plan_step` and `session_id` already carry. Copying a template's own `summary` into a field
documented as *the requester's words* would have been the shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` is about. The launcher refusal
is now pinned by a test, so the guarantee is asserted where it actually lives.

`record_confirmed_answer` gains `corrected_from`: what the system had said. Empty means confirmed.
One field rather than a separate flag, because a correction with no account of what it replaced is
the same loss one step smaller.

## The publisher deliberately not built

The obvious companion fix was to give the `calc` bundle's twelve durable jobs a `note_from_*`
builder and `publish_to_graph: true`, on the reasoning that the semiempirical tier is the system's
actual scientific output and leaves no knowledge behind. **A design review rejected it, and two of
that reasoning's premises turned out to be false.**

`job-result` is *not* an unminted type: `agent/graph_tools.py` mints it through
`propose_knowledge_note`, `skills/knowledge-graph-write` names it, and CLAUDE.md's "no *bundle*
mints one" is precise where this proposal read it broadly. And the scientific record does not stop
at the cache: `connector_job._publish_result` runs for every job and all twelve result shapes have
projectors in `publish/project.py`. What the graph lacks is the *reasoning*, and a pure replay-stable
function of a result model cannot produce reasoning — it has no tool access, so it cannot combine a
solvent screen with a hazard screen, a solubility, or precedent, which
`skills/solvent-selection` says outranks every calculation.

A merged policy already forbade it. `skills/computational-evidence`: *"Do not record routine
exploratory calculations; the calculation cache already keeps them, and a graph full of unremarkable
numbers makes the remarkable ones harder to find."* An automatic publisher records every exploratory
calculation — the thing that skill exists to prevent. Measured, roughly half the notes would have
said "this calculation could not distinguish them", because the margin sits inside GFN2-xTB's ±3
kcal/mol; and neither default is defensible, since `true` floods a 39-note graph while `false` that
nobody enables is the dead-capability shape this repository keeps deleting.

**What replaced it is smaller and closes the actual gap.** The recording rule had no trigger — "a
computed value that matters beyond the conversation" — so it named no moment. It now does: a
comparison whose margin *clears* the stated uncertainty is a conclusion, and the conclusion is the
part no other store holds. Where the margin does not clear it, the ceiling section already applies,
and a note saying "we could not tell" is the unremarkable-number case the same skill refuses.

## The eval half, and a recommendation corrected by measurement

`propose_knowledge_note` is named by fourteen probes across seven files and by **none** in
`durable.yaml` or `multistep-calculation.yaml` — the two covering exactly the
calculation-then-conclude flow. So the corpus asked whether a ranking ran and never whether its
conclusion was written down.

The review's proposed fix was to add `propose_knowledge_note` to `expects_tools` on the existing
ranking probes. **That would have made them weaker.** `evals/live.py` scores `expects_tools` with
`any()`, so appending a second name makes a probe pass on *either* tool — `ms-07` would then have
been satisfied by a turn that recorded a note and never ranked anything. A capture probe has to be
its own probe, and `ms-18`/`ms-19` are. `ms-19` is bucket C on purpose: it asks for a note about a
two-kcal margin, which is *inside* the error bar, and the graded behaviour is declining to write it.

## Consequences

- The default profile's context floor moves 43,063 → 43,316 estimated tokens against the unraised
  43,500 ceiling. **184 tokens of headroom**, which is tight enough to be the next person's problem:
  the reclaim is queued (`propose_knowledge_note` alone is 1,126 tokens and a `BACKLOG.md` row
  measures tool schemas at 38% developer rationale).
- `find_past_jobs(connector="template")` now filters to procedures. Template rows carry an empty
  `rationale` by design; a reader wanting the why reads the template.
- A run recorded before its push-back means the id a chemist is handed is one the retrospective view
  can already answer for.
