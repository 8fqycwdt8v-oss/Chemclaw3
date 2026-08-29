# D-2026-08-29-a-trail-nobody-can-read-answers-no-question — the operational read model

**Status:** accepted · **Date:** 2026-08-29 · First of the eight infrastructure findings from the
2026-08-28 audit (F3), and the one built first because it is entirely internal: no new seam, no new
risk, and it reads tables that already exist.

## Context

This system writes five tables about its own work and reads none of them.

`chemclaw.agent.audit_store.PostgresAuditStore` exposes exactly two methods, `record` and `flush`.
`infra/sql/grants/app_privileges.sql` hands the runtime principal `GRANT SELECT ON ALL TABLES`, so
the privilege to read the trail has been there since the grant matrix was written and no code has
ever used it. `turn_costs` is the same shape and says so in its own module docstring: *"Write-only
from this process, and that is the honest state rather than an oversight."* `job_records`,
`note_proposals` and `bo_campaigns` each have exactly one reader, and each of those answers a
question about one row rather than about the record.

The consequence was measured against the probe corpus rather than argued. Grouping
`data/evals/probes/` by persona and bucket — a grouping nobody had run — gives:

| persona | A | B | C | share absent |
| --- | --- | --- | --- | --- |
| `lab_technician` | 78 | 25 | 17 | 14% |
| `lab_leader` | 70 | 33 | 17 | 14% |
| `manager` | 12 | 8 | 19 | **49%** |

Six of those nineteen manager-C probes are answerable from data this system had already stamped and
could not read: `rp-12` (how much of that note did this system write), `kn-28` (is this being used),
`op-27`/`op-28` (what did the campaigns cost and save), `pl-21` (how did screening trend last
quarter), `pl-27` in part. The trail proved *that* something happened and could answer no question
*about* it.

## Decision

**`src/chemclaw/operations/` is the read model, and it is a projection rather than a claim.**

Four readings over a `Window`: `tool_usage` (calls per tool split across the four outcomes),
`job_activity` (durable runs per connector job, and how many proposed a note), `authorship` (what
the agent proposed for the graph, by note type, and how humans decided), `spend` (turns, tokens and
wall clock per actor). One agent tool, `review_activity`, with an `aspect` enum.

Three properties are the decision; everything else is SQL.

**1. It is ungated, for the reason the ELN transcription tier is ungated.**
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` established that a deterministic projection
infers nothing and so hands a reviewer nothing to decide. A `GROUP BY` over rows nobody wrote for
this purpose is that argument one level up. Nothing here is proposed, nothing reaches the knowledge
graph, and nothing is remembered.

**2. Counts and identifiers only — never a caller's free text.** `audit_events.arguments`,
`audit_events.detail`, `job_records.rationale` and `note_proposals.content` all hold text a caller
supplied, and the system prompt is explicit that there is one shared corpus with no record-level
scoping: *"every note, job record and calculation you can reach is visible to every user who can
reach you."* An aggregate is therefore visible to everyone who can reach the agent. A tool name, a
connector name, a note type and an outcome are bounded vocabularies and are safe to return; a
rationale is not. `tests/test_operations.py` writes a marker into each of those four columns and
scans the serialized readings for it, which is the direction that matters — a field added later
would otherwise leak in silence.

The one asymmetry is deliberate: `tool_usage` returns a *count* of distinct actors and `spend`
returns actor ids. "Who else uses this" is answered by how many, because naming colleagues in an
aggregate is a different disclosure from naming the actor on a row that person can already see;
"where did the effort go" has no answer at all without a subject.

**3. Every reading carries the window it covered.** An operational zero is ambiguous in a way a
scientific one is not. "No hazard screens in 90 days" is a finding; the same zero out of a
deployment whose `durable/retention.py` prunes at 30 is a question about deleted rows. `Coverage`
travels with all four readings and states `since`, `until`, the phrase the caller's argument became,
and how many rows the window held before grouping. `Window.trailing` clamps rather than raising, and
rebuilds `described` from the clamped span, so the phrase can never overstate the reading.

## What this does not do, and why the probes were re-bucketed rather than passed

Three probes move from C to **B**, not to A, and each direction file now says exactly what the new
capability is and is not:

- **`pl-21`** — screening calls are now countable and trendable against the preceding window. Teams,
  reaction classes and near-misses remain absent, and the figure counts *screens run* rather than
  *hazards found*; reporting it as a flag rate would be the same fabrication in a smaller font.
- **`rp-12`** — the agent-authored side is now a real number with a window. `Authorship` carries a
  `boundary` field, returned with every reading, stating that this system holds no record of what a
  person wrote or edited in the git host. The percentage is still refused, because the denominator
  does not exist.
- **`kn-28`** — "is this capability being used" has a figure and a distinct-actor count. "Who read
  this note, and what were they trying to do" does not, and cannot: the arguments are not returned.

`op-27`, `op-28` and `pl-27` stay C. They need the commitment model (F4) and instrument data, not
this.

## Consequences

- Five tables that had writers and no readers now have exactly one reader each, in one package.
- `tests/test_turn_cost.py` pins the *absence* of a `turn_costs` reader
  ("what reads the ledger today is an operator with `psql`"). `operations.spend` is that reader, so
  that pin and its docstring are updated rather than deleted — the absence test was right to exist
  and is now a statement about which reader is allowed.
- The distinct-actor count is a lower bound and the field says so: the per-`(tool, outcome)` count
  cannot be summed into a per-tool one, because the same person appears under two outcomes, so the
  maximum is taken.
- `OUTCOMES` is transcribed rather than imported from `chemclaw.agent.audit`. `operations` sits
  below `agent` in the layering, and more to the point a reader of history must not be bounded by
  today's producer: the trail holds rows written by every revision that ever ran.
