# D-2026-07-31-a-proposal-is-a-record-not-a-branch — A proposal is a record, not a branch

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-005 (the PR-gate), D-141 (ambient
correlation), D-157 (the durable job record)

## Context

The PR-gate is the control this system is justified by. `CLAUDE.md`, `ARCHITECTURE.md`,
`SECURITY.md` and D-005 all name it the GxP line — AI proposes, a human signs off — and it is
reused deliberately everywhere machine-generated knowledge enters the graph: job results, campaign
narratives, distilled playbooks, report drafts, ELN reactions, confirmed answers.

In code it ended at a branch push. `GitNoteSubmitter.submit` writes the rendered note, commits,
pushes `note/<id>` and returns that name; its own docstring says opening the PR object is "the git
platform's job", and there is no git-platform API call anywhere in the repository. What followed
from that is not a missing feature but a control that does not function:

- **Nothing listed what was awaiting review.** Sixteen routes, none of them `/proposals`. A
  reviewer's only discovery mechanism was browsing `note/*` refs in a git host.
- **A proposer could not learn what became of their note.** `record_proposal` surfaces the branch
  on the turn's SSE stream and that trace dies with the turn.
- **A rejection left no trace whatsoever**, because a rejection is a deleted branch. The system
  could not answer "did we consider this and say no?" — which is the question a review record
  exists for, and the one an auditor asks.
- **A submission that never reached git was lost.** D-2026-07-31 (the deployment envelope) added
  `chemclaw_notes_publish_failures_total`, so a dead remote stopped reading as an idle system. The
  note itself was still gone, with nothing to replay.

The only durable trace of a proposal anywhere was `job_records.note_id`, and only for connector
jobs — whose own comment already conceded the point: "a join to the knowledge graph, not proof of a
merge: an agent note is a *proposal* until a human signs it."

## Decision

**A proposal is a durable record with a state, written by the gate itself.** `note_proposals`
(`infra/sql/027`) holds every submission with its provenance, the rendered note, and what a human
decided; `GET /proposals`, `GET /proposals/{id}` and `POST /proposals/{id}/decision` make it
operable; the merge webhook closes rows so the queue drains.

Five choices inside that are load-bearing.

**Recorded in `propose_note`, not at its callers.** There are eight producers — the agent tool,
three memory activities, the report workflow, the connector-job wrapper, ELN ingest, the corpus
backfill. An obligation that must hold for every proposal belongs to the one wrapper they all run
inside, which is the placement rule the actor stamp (D-141) and the job record (D-157) already
follow. "Each caller remembers" is precisely the discipline that fails silently.

**Keyed on the note's *content*, not on the note.** A decision is evidence and must not be
overwritten by the next submission: "this was rejected in July" has to survive the note being
re-proposed in August. So a byte-identical re-proposal touches the existing row — matching the
submitter, which pushes nothing when there is no diff — while a changed body appends a new row and
leaves the earlier decision standing. Two consequences fall out and both are wanted: an idempotent
re-proposal does not spam the table, and **an unchanged re-proposal cannot reopen a rejection**,
which would otherwise make the gate defeatable by asking again until nobody was looking.

**The rendered note is stored, not summarised.** That is what separates this from a counter. A
`failed` row is replayable because the bytes it would have written are still there, and a reviewer
opening a proposal sees exactly what will land — a paraphrase is the one thing a GxP review must
not be handed.

**Recording a submission never raises; recording a decision does.** By the time the record is
written the note has already reached the branch (or already failed to), so a database blip must not
turn a successful submission into a failed tool call — the trade `chemclaw.agent.audit` makes for
the same reason, with the loss made visible by `chemclaw_note_proposals_total{state}` rather than
silent. A decision is the opposite: a reviewer told their rejection was stored when it was not is
the one failure mode a review surface cannot tolerate.

**The webhook is signed, and a rejection must state why.** `/events/knowledge-merged` previously
took no body and was authenticated but unscoped, which was fine while it only kicked an idempotent
reindex. It now carries an authorization-shaped claim — "a human merged these" — so it is
HMAC-signed under `note_webhook_secret` and compared in constant time; an unsigned caller keeps the
power it had (force a reindex) and gains none of the new one. And a refusal with no reason would
reproduce the gap this table exists to close one level up, so the route rejects it.

## Consequences

The gate is operable: a queue can be listed, a note read as it would land, a decision recorded, and
a merge closes the row. `rejected` is a state the system has for the first time. A lost note is
recoverable rather than only countable. An operator watching `chemclaw_note_proposals_total{state}`
sees a rising `open` against a flat `merged` — a review queue nobody is working — without opening
it.

**Two backends, both real.** `InMemoryProposalStore` is what a `session_store="memory"` deployment
gets, and the CLI is one of those — so the queue exists there rather than reporting an empty gate.
It enforces every rule its Postgres sibling enforces in SQL, in the same terms, because a backend
that agrees on the happy path and diverges on the contended one is worse than no second backend.

**What this deliberately does not do: open the PR object.** A GitHub/GitLab/ADO adapter needs a real
platform token and base URL to be verifiable at all, so writing it now would mean asserting things
about someone else's host. `NoteSubmitter` is the seam it slots behind — a decorator that pushes,
then opens the PR, then returns its URL as the `reference` this record already stores. Until then
the reference is the branch name, which is what it was before, now written down.

**Residual limits, stated rather than discovered later.** A reviewer role is `entra_privileged_roles`
— the same set that guards every write tool, since signing off on machine-written knowledge is the
most consequential write in the system, and inventing a second weaker role for it would be strange.
A deployment that enables identity and names no privileged role therefore has a queue nobody can
decide; that fails closed exactly as `authorize_tool` does, and is a misconfiguration to notice
rather than paper over. Notification is still absent: this makes the queue *findable*, not *pushed*
— routing a new proposal through the existing `notify` seam is the next step and is a separate
decision about who gets told what.
