# `chemclaw.templates` — deterministic multi-step workflows

A **template** is an ordered list of steps run as one durable Temporal workflow. Where a *profile*
configures an agent and leaves the order of work to the model, a template fixes the order and the
model only fills the gaps: it is the answer to "this procedure is always the same five steps, and I
need it reproducible, resumable and auditable" — a validated protocol, a standard screening sweep, a
report that must always gather the same evidence in the same order.

Both exist because they answer different questions, and using the wrong one is the common mistake:

| | **Profile** (`profiles/`) | **Template** (`templates/`) |
|---|---|---|
| Decides the order of work | the model | the file |
| Runs on | a chat turn | Temporal (durable, resumable) |
| Good for | a specialized assistant | a procedure that must not vary |
| Bad for | a fixed procedure | open-ended research |

**Reach for a profile first.** A template pins its steps, which is exactly what you do not want
while the procedure is still being figured out.

## Shape

```yaml
# templates/<name>.yaml — the filename is the template name.
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

## Running one

Each template becomes a generated agent tool named `run_<name>`, so the model can start it exactly
as it starts any durable job — and it is gated, audited and attributed exactly the same way. It
returns a job id; poll it with `get_durable_job_status`.

`make template-validate` checks every template before it ships: unique step ids, references that
resolve, tools that exist, profiles that exist, and no forward references.

## Versioning

A run pins the *resolved* template in its workflow input, so editing a file never changes an
in-flight run and Temporal can replay it deterministically. Editing a template therefore affects
only runs started afterwards — there is no migration to do, and no way for an edit to corrupt
history.
