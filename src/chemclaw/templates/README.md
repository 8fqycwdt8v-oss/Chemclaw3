# `chemclaw.templates` — deterministic multi-step workflows

A **template** is an ordered list of steps run as one durable Temporal workflow. Where a *profile*
configures an agent and leaves the order of work to the model, a template fixes the order and the
model only fills the gaps: it is the answer to "this procedure is always the same five steps, and I
need it reproducible, resumable and auditable" — a validated protocol, a standard screening sweep, a
report that must always gather the same evidence in the same order.

Both exist because they answer different questions, and using the wrong one is the common mistake:

| | **Profile** (`data/profiles/`) | **Template** (`data/templates/`) |
|---|---|---|
| Decides the order of work | the model | the file |
| Runs on | a chat turn | Temporal (durable, resumable) |
| Good for | a specialized assistant | a procedure that must not vary |
| Bad for | a fixed procedure | open-ended research |

**Reach for a profile first.** A template pins its steps, which is exactly what you do not want
while the procedure is still being figured out.

## Shape

```yaml
# data/templates/<name>.yaml — the filename is the template name.
summary: One line the model reads when deciding to run this.
description: >-
  The rest of what the model needs: when this applies and what it produces.
inputs:
  - {name: smiles, type: string, description: The molecule to screen.}
steps:
  - id: hazards                      # unique within the template; how later steps refer to it
    kind: tool
    tool: screen_hazards
    arguments:
      smiles: ["${inputs.smiles}"]   # substitution, see below
  - id: verdict
    kind: agent
    profile: property-lookup         # optional; omit for the default agent
    write_tools: []                  # optional; see "An agent step is read-only" below
    prompt: >-
      Summarize these hazard flags for a chemist: ${steps.hazards.result}
```

Three step kinds:

- **`tool`** — call any tool on the agent's surface (in-process or a connector's) with resolved
  arguments. The step's result is whatever the tool returned.
- **`job`** — run a connector's durable job and *await* it, so a template can orchestrate long work
  rather than just firing it off. The result is the job's `ConnectorJobResult`.
- **`agent`** — run one agent turn with a rendered prompt, optionally under a named profile. The
  result is the answer text. This is what keeps a template *agentic*: the sequence is fixed, the
  reasoning inside a step is not.

## An agent step is read-only

**A template is not plan-gated.** The plan gate puts a human between an autonomously-chosen write
and its execution; a template already has that human — the file is authored by a person, committed
to git and reviewed, and nothing at run time can produce one. Asking an `agent` step to get its plan
approved would be asking for approval of a plan nobody wrote, and there is no session to approve it
in.

**So the step is narrowed instead.** Its agent is built with every state-changing tool removed from
both halves of its surface — the in-process tools *and* every connector's allow-list — unless the
step names them:

```yaml
  - id: record
    kind: agent
    write_tools: [record_knowledge_note]
    prompt: Write up what step two found and propose it as a note.
```

The removal is structural, not a filter: the tool is absent from the graph the step runs on, so it
is not reachable by any name. Declaring one only *restores* it — every other gate still applies, so
the run's requester must still be authorized for it.

Only side-effecting tools belong here. A read tool is reachable without any declaration, and naming
one fails `make template-validate` — otherwise the list becomes a general allow-list wearing a
write-list's name. The same check rejects a typo, and a tool the step's `profile` does not
advertise: a step can only ever narrow what its profile already offered.

## Substitution

`${inputs.<name>}` and `${steps.<id>.result}`, and nothing else. A whole-string reference
(`"${inputs.smiles}"`) substitutes the *value*, preserving its type; a reference inside a larger
string interpolates its text. A reference to an unknown input or to a step that has not run yet is a
validation error, not an empty string — a template that silently passed `None` into a calculation
would be worse than one that refused to start.

Deliberately not a template language: no conditionals, no loops, no expressions. Those are what
makes a "simple config format" become a programming language with no debugger, and the moment a
procedure needs them it wants an agent (a profile) or real code (a connector workflow), not more
YAML.

## Concurrency, which you do not write down

Steps that do not read each other run at the same time. There is no `parallel:` key and there is
nothing to opt into: a `${steps.<id>.result}` reference **is** a dependency edge, the validators
above refuse a forward reference, so the declared order is already a topological order of a DAG and
`templates/schedule.py` reads the waves straight off it. Two of the nine shipped templates —
`degradant-triage` and `hazard-briefing` — turned out to be shaped this way and had been running
one step after another for no reason anybody had written down.

**This is not the fan-out `D-2026-08-25-the-loop-is-a-composite-not-a-template` declined.** That
decision is about a *loop*: ranking N microstates, where N is known only once an earlier step has
answered. A loop needs iteration and expressions and still lives in a composite. What runs together
here is steps the file already declares.

So the way to make a procedure faster is to stop making a step read something it does not need: a
step that references an earlier result only to pass it through has just serialised itself.

## Running one

Each template becomes a generated agent tool named `run_<name>`, so the model can start it exactly
as it starts any durable job — and it is gated, audited and attributed exactly the same way. It
returns a job id; poll it with `get_durable_job_status`.

`make template-validate` checks every template before it ships: unique step ids, references that
resolve, tools that exist, profiles that exist, declared write tools that exist and actually write,
and no forward references. It also checks that the **run** can finish the steps the file declares —
`template_run_timeout_seconds` against the sum of the per-kind step ceilings — and
`unrunnable_reason` asks the same question again at launch, so a procedure that cannot complete is
refused before a workflow id exists rather than terminated hours later. That second check is not
redundant with `core/config`'s: a `Settings` object cannot see this directory, so it can only
require that *one* step fits, and one `job` step is 39,330 s against a run ceiling of 45,330 s.

## What an `agent` step is handed

A step's prompt is cut to `agent_max_tool_result_chars` at the model's edge
(`durable/template_activities.bounded_prompt`), head and tail, with a notice saying so in this
system's own marked words. The chat path's cap does not reach here — `bound_tool_results` is an
entry of `tool_call_middleware`, and a `tool` step runs through `invoke_governed`, which folds the
governance chain without the three entries that exist to serve a model. So a `${steps.<id>.result}`
reference to an oversized result used to arrive whole: measured, 245,700 characters against a
60,000 ceiling, and unreclaimable, because compaction's two edits are for history and a step has
none. Reference a *field* of a large result (`${steps.ranking.result.smiles}`) rather than all of
it when you can — `chemclaw_template_prompt_truncated_total` names the template when you have not.

## A failed run resumes

A run's id is `hash([name, inputs])` under `ALLOW_DUPLICATE_FAILED_ONLY`, so the only way to
re-execute one is after a failure — and the steps that *had* finished were already recorded
(`failed_template_record`) and were not read, so the next attempt redid them. It does not now: the
sequencer asks `completed_steps` what the previous attempt finished, folds those results into
scope, and dispatches only what is left.

Three conditions, each of them a way this could be *wrong* rather than merely absent: the row must
exist, it must be a failure (`job_records` is upserted on `job_id`, so a completed run's row would
otherwise read back as a resume of itself), and its fingerprint must match the resolved template —
because the run id says nothing about the steps, so editing the file and relaunching lands on the
same id carrying a different procedure.

## Versioning

A run pins the *resolved* template in its workflow input, so editing a file never changes an
in-flight run and Temporal can replay it deterministically. Editing a template therefore affects
only runs started afterwards — there is no migration to do, and no way for an edit to corrupt
history.
