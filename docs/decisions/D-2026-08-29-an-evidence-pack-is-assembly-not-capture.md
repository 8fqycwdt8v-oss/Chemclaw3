# D-2026-08-29-an-evidence-pack-is-assembly-not-capture — the context-of-use record

**Status:** accepted · **Date:** 2026-08-29 · Eighth and last of the infrastructure findings from
the 2026-08-28 audit (F8). Does **not** revisit
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks`; that decision
stands and the hash chain stays removed.

## Context

The audit's F8 was the one finding that is not a gap: the auditable trail, the PR-gate, the
untrusted-content envelope, the verifier, the refusal list in the system prompt and the honest "what
this system does not hold" paragraph are the strongest part of this system. Dotmatics cites a
Gartner projection that 80% of agentic AI initiatives in life sciences will not clear their first
governance checkpoint in 2026 — not on model quality, on traceability.

What changed on 2026-08-29 is what the record is *asked for*. Two of the other findings moved the
line: this system now acts on systems it does not own
(`D-2026-08-29-an-effect-declares-whether-it-can-be-undone`), and computed values already leave for
a scientific record (`D-2026-08-25-a-cache-is-not-a-record`). Once both are true the artefact
somebody needs is not tamper-evidence — it is a **context-of-use record**: what was asked, what the
system was permitted to do, what evidence it used, what it changed, and who approved it. That is
what the FDA's seven-step credibility framework and the January 2026 FDA–EMA good-practice
principles ask a sponsor to produce.

Every component of it already existed. `audit_events` records every call with its actor, outcome and
latency; `job_records` records what a run was asked for and returned; `note_proposals` records what
was proposed and who decided; `plan_approvals` records who approved which plan; `effects` records
what was changed outside and who approved it. **What was missing is the read.**

## Decision

`chemclaw.operations.evidence_pack.assemble(session_id)` — five reads, one object, and three
sentences the object carries about itself.

**Assembly, not capture.** Nothing here is new instrumentation. That is the whole reason this is a
small change and the reason it is trustworthy: an artefact assembled from records written for their
own purposes, at the time, is stronger evidence than one written to be evidence.

**Keyed by session, not by result.** "How did we arrive at this" is a question about a piece of
work. A result-keyed pack would have to guess which calls contributed to which number — an inference
this system does not make anywhere else and must not start making here.

**Five reads rather than one join.** The stores are independent by design: an effect is recorded
whether or not a note was proposed, and a proposal survives the session's messages being pruned. A
join would silently drop a row whose partner had been disposed of under a different retention rule.

### The three limits, carried on the object

`limits` is a field, not a docstring, because each one corrects a reading somebody will otherwise
make:

1. **Append-only is a database privilege, not tamper-evidence.** The credential that writes the
   trail cannot rewrite it; a database owner still could. The system prompt already says exactly
   this to chemists, and a pack presented to an auditor must not claim more than the prompt says to
   the person doing the work.
2. **It is this system's record of its own work**, not the whole record of the decision.
   Conversations, meetings and a person's judgement leave nothing here.
3. **An empty section is a statement about the record**, not about the work — a window outside
   retention reads identically to one in which nothing happened. The same distinction `Coverage`
   makes one module over, and `is_empty` is the property a caller must check before presenting one.

### Refusals are part of the record

Surfaced as a property of the calls rather than as a section of their own. A gate refusing is the
control operating, and a pack that filed refusals separately would read as a list of things that
went wrong — which is precisely backwards for the audience this exists for. The refusal reason is
carried, because "refused" without one is indistinguishable from a broken tool.

## Consequences

- `assemble_evidence_pack` is the one operational reading that returns free text. `activity` is
  bounded to counts and bounded vocabularies because an aggregate is visible to everyone who can
  reach the agent; a pack is scoped to **one conversation**, and a rationale and a plan hash are its
  substance rather than a leak. The scoping is the control.
- The pack is not a document. It is the assembled record; rendering one for a submission is a
  reporting concern, and doing it here would put a template in the same module as the read.
- Nothing signs it, and nothing should on this evidence. A signature over an assembly whose inputs a
  database owner can edit adds an appearance of integrity rather than integrity — which is the
  distinction D-2026-08-14 removed the hash chain over, and this decision keeps.
