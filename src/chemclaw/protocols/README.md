# `chemclaw.protocols` — prescriptive experiment designs

**Responsibility:** the shape a *proposed* experiment takes, the deterministic checks it has to
survive, and its revision history. Everything else in this tree that looks like a reaction is
descriptive — `ingest.eln.ord.OrdReaction` and `reaction_records` hold what a chemist *did*. This
package holds what to do.

That distinction is not a preference. The ORD schema this system borrows states it: a record
"should describe what was actually done in the lab, and not an idealized protocol or instruction
set". A prescriptive object needs its own type, or the two get stored in one table and a proposal
starts reading as a result.

## The one thing to internalize

**A single experiment is a design with one arm and no factors.** An HTE screen is the same object
with factors, levels, N arms and a plate layout.

```
ExperimentDesign
├── request   ExperimentRequest    the structured ask, each slot carrying its basis
├── base      ProtocolBody         what every arm shares: setpoints, charge, steps, analytics, hazards
├── factors   list[Factor]         [] for a single experiment
├── arms      list[ProtocolArm]    1 for a single experiment; N for a screen
├── layout    PlateLayout | None   wells and run order (a plate only)
└── evidence  list[EvidenceRef]    why these conditions — precedent and tool citations
```

Because there is one shape, the store, the checks, the renderer, the CSV export and the UI are each
written once. A second "HTE campaign" type would have duplicated all five.

## The modules

| Module | What it is |
| --- | --- |
| `models.py` | The shape. Short docstrings on purpose — pydantic ships a class docstring as the JSON-schema `description` on every turn, so rationale lives in `#` comments. |
| `checks.py` | The deterministic verdicts, computed from the design and never asked of a model. `check_ids()` is the list; a number here would be a second one that goes stale. |
| `layout.py` | Plate arithmetic: formats, well labels, placement, run order. No chemistry. |
| `diff.py` | What changed between two revisions, as dotted paths. |
| `render.py` | The receipt a tool returns, the run sheet, and the Markdown a chemist reads. |
| `store.py` | `experiment_protocols` + its append-only revision table. |

## What is deliberately not here

**Judgment.** Which precedent counts as precedent, which factors are worth varying, what the levels
should be, when a prediction may be trusted — all of that is layer 3, in
`skills/protocol-generation` and `skills/hte-campaign-design`. The agent composes the precedent,
prediction and safety tools under those skills and hands the result here.

The split is the same one `science/` and `connectors/` have, and it is what keeps a protocol from
being whatever a hard-coded pipeline decided. What this package contributes instead is that the
judgment cannot be skipped: `checks.evidence_present` refuses a design citing no precedent and no
tool, so "use the record and the tools" is enforced by code rather than hoped for in a prompt.

**A second extractor.** Free-text history is already structured twice over — deterministically at
ingest (`ingest.eln.json_adapter` segments a procedure and keeps the prose verbatim) and, where a
number really is only in prose, behind `condense_protocols`, which marks what it read as
`digest_source: extracted`. This package consumes both and adds neither.

**A Temporal workflow.** Drafting is composition inside a turn today. It becomes durable work when a
draft starts fanning out `calc` jobs, and that is a measurement to take before a workflow is
written.

## The rule about inference

`RequestField` carries `basis: stated | inferred | absent`, and `stated` obliges a verbatim quote
that `protocols.request` checks against the chemist's own text. Inference is *allowed* here, which
is the opposite of the transcription rule
(`D-2026-08-26-a-transcription-may-not-infer-a-setpoint`) and for a consistent reason: a record must
not gain a number nobody measured, and a proposal is nothing but numbers nobody has measured yet.
What the two rules share is that neither permits the inference to be silent.

## Tables

`experiment_protocols` (one row per design: identity, status, head revision) and
`experiment_protocol_revisions` (append-only; `document`, `checks`, `parent_revision`,
`author_kind`). Migration `073`. The revision table has `INSERT` and no `UPDATE` by grant, not by
convention — a revision is what an expert's correction *is*, so a credential that could rewrite one
could erase the signal the table exists to keep.
