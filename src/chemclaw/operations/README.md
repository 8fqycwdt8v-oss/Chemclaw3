# `operations/` — the operational read model

**What this system did, read back out of the record it already keeps.**

Everything else under `src/chemclaw/` answers a question about the chemistry. This package answers
a question about the *work*: which tools were used and how their calls ended, which durable jobs
ran, what the agent proposed for the knowledge graph and what a human decided about it, and where
the turns and tokens went.

## Why it exists

The tables it reads have all been written since the system was built and none of them could be read
*across*. (First written here as "none of them had a reader", which is false — `cli/explain.py`,
`publish/backfill.py`, `durable/job_record_store.py`, `kg/proposal_store.py` and
`agent/plan_approval_store.py` all read one, and only `turn_costs` had none. The second attempt
called them all point lookups, which is false too: two are searches and one is a full sweep. What
none of them does is **aggregate**, and that is the whole of it.)
`chemclaw.agent.audit_store.PostgresAuditStore` exposes `record` and `flush`; the grant matrix hands
the runtime principal `SELECT` on every table and no code used it on this one. So the trail proved
*that* something happened and could not answer a question *about* it — and a set of questions a
group leader asks routinely ("is this playbook actually used", "how did our hazard flags trend last
quarter", "how much of that note did this system write") were unanswerable from data already
stamped.

## The three rules

1. **Counts and identifiers only — never a caller's free text.** `audit_events.arguments`,
   `note_proposals.content` and `job_records.rationale` all hold text a caller supplied, and there
   is one shared corpus with no record-level scoping, so an aggregate is visible to everyone who can
   reach the agent. A tool name, a connector name, a note type and an outcome are bounded
   vocabularies; a rationale is not. The free-text half is `find_past_jobs`, which goes through the
   retrieval path that frames what it returns.
2. **Every reading carries the window it covered** (`Coverage`). An operational zero is ambiguous in
   a way a scientific one is not: "no flags in 90 days" is a finding, and the same zero out of a
   deployment that prunes at 30 is a question about deleted rows.
3. **A row this system never wrote is never inferred.** `authorship` reports what the agent proposed
   and how it was decided. It carries a `boundary` sentence saying it is not a share of a document's
   authorship, because a human edit made in the git host leaves no row here.

## Why it is not gated

A deterministic aggregate over rows nobody wrote for this purpose infers nothing, so it hands a
reviewer nothing to decide — the argument
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` makes one level down. Nothing here is
proposed, nothing reaches the knowledge graph, and nothing is remembered.

## Layout

| Module | What it is |
| --- | --- |
| `window.py` | `Window` — a half-open span and the phrase that asked for it. |
| `activity.py` | The four readings and the models they return. |
| `evidence_pack.py` | `assemble` — one conversation's whole record, from the five stores that hold it. |

The agent-facing surface is two tools: `review_activity` in `chemclaw.agent.operations_tools` and
`assemble_evidence_pack` in `chemclaw.agent.evidence_tools`. The second is gated on session
ownership and is withheld from the read-only MCP face, which has no actor to own anything.
The store lives here and the tool lives there for the same reason `memory/` and
`agent/memory_tools.py` are separate: a tool is conversation plumbing, and a store is not.
