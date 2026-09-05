# D-2026-09-05-a-rejection-nobody-reads-is-a-decision-taken-twice — the reviewer sees every earlier version of the note in front of them

**Status:** accepted · **Date:** 2026-09-05 · **Builds on:** D-005 (the PR-gate), D-2026-08-25-an-eln-transcription-is-data-not-a-claim (the gate is a credibility budget) · **Answers** the first of the two review-scaling questions raised against `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`; the rest of that ideation is recorded in `docs/planning/BACKLOG.md` and deliberately not built.

## Context

The owner asked two questions about the review line: how does a reviewer avoid drowning in a flood
of near-identical proposals, and how do local and global skills stay convergent. Most of the answer
to both is downstream of a **trajectory→skill distiller that does not exist** and is blocked on an
empty corpus — `make trajectory-census` reports 0 sessions and neither arm greenlit. Building
promotion thresholds, cluster review or a divergence census against an imagined corpus is
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`, which this repository deleted 1,442
lines over.

**One part is real today**, because the queue it concerns already exists and already carries
proposals: `GET /proposals`, `GET /proposals/{id}`, `POST /proposals/{id}/decision`.

A reviewer opening a proposal was shown the note's bytes and **nothing about what had already been
decided about that note**. The gate closes the exact-repeat case — `rejected_version` refuses a
re-proposal of byte-identical rejected content before it reaches git, and it is asked before the
push precisely so a rejection cannot leave a live mergeable branch. But a **changed** re-proposal of
a note somebody already rejected is a different version, so it arrives in the queue legitimately,
and it arrived with the earlier rejection and its stated reason nowhere on the page.

Two costs, and the second is the one that matters. The reviewer re-derives a judgement a colleague
already made — the flood, self-inflicted. And a reviewer who does not know a claim was refused once
may merge it the second time, which turns the gate into a race between proposals and reviewer
memory.

**The record to prevent both was already in the table.** `reason` is written on every decision
(`ProposalDecisionIn` makes it *required* on a rejection, because "why was this refused" is the
question a rejected proposal exists to answer), and `durable/retention.py` refuses to prune
`note_proposals` — "the PR-gate's record of what was proposed and who decided it". Nothing read it
back. The reviewed framework's single largest ablation is the same finding from the other end:
+15.0pp for a proposer that can see rejection history.

## Decision

**`ProposalStore.history(note_id, actor)` — every recorded version of one note, oldest first — and
`GET /proposals/{id}` returns it beside the bytes.**

- **`ProposalSummary` is reused rather than a history shape invented.** It already carries exactly
  what a history entry is — state, who decided, when, and why — and carries **no body**, which is
  the property that matters: the reviewer is being shown what was decided, not handed a second
  document to read. A `ProposalHistoryEntry` would have been the same fields under a second name.
- **Scoped by the same rule that decided visibility, not a looser one.** A reviewer sees every
  version; anybody else sees only their own. This is not a convenience filter: assembling a history
  for a non-reviewer without it would disclose that another chemist proposed the same note, and the
  disclosure fails *open* — it is the kind of predicate that is easy to get right in Python and
  wrong in SQL, which is why the Postgres test asserts the filter is not a no-op.
- **The viewed version is dropped from its own history**, because its decision is the rest of the
  response.
- **Every state appears, including `superseded`.** A version replaced without a decision is part of
  what happened to this note, and hiding it would make a v3 look like a v1.

**No migration.** `note_proposals_note_idx ON note_proposals (note_id, submitted_at DESC)` from
migration 027 already serves the predicate — checked rather than assumed
(`D-2026-08-27-an-index-must-match-the-sort-it-serves`). The sort is `ORDER BY id` rather than
`submitted_at` for the reason the listing pages by id: the timestamp is the store's clock and two
rows can share it, while the id is the submission order itself.

**Both backends, and the Postgres one is tested separately.** `InMemoryProposalStore` is the backend
a `session_store="memory"` deployment really gets, not a double — but the ordering across separate
transactions and the actor predicate are things only the database decides, so
`tests/test_note_proposals_postgres.py` asserts them against the statement that serves them.

## What is deliberately not built

Recorded so the next session does not re-derive it, each with what it waits on:

- **Promotion thresholds on skills** (used N times *and* by ≥ 2 distinct chemists before a human
  sees it) — D-161's two-threshold shape, and the single most effective flood control available.
  Waits on the distiller.
- **Duplicate suppression in the generator rather than in the queue.** Near-duplicates arise because
  each skill is distilled from one trace and traces differ in irrelevant detail; the fix is to
  constrain the distiller to propose an *edit* to the nearest existing skill unless it can show none
  is close. A queue-side deduplicator is a bandage on a generator that should not have produced
  them. Waits on the distiller.
- **Cluster review** (`memory/similarity.py::cluster_by_similarity` is the precedent) and
  **benefit-ranked triage** over `evals/ab.py`, with the machine ordering the queue and the human
  still deciding — which is why it does not re-open `D-2026-08-16`. Both wait on there being enough
  proposals for grouping to pay for itself; today the queue is short because the corpus is empty.
- **The convergence half.** Global-wins-on-conflict with the conflict surfaced (the rule
  `skills/playbook-distillation/SKILL.md` already states for retrieval), promotion retiring the
  local variants that fed it via `memory/supersede.py`, expiry on disuse read as a signal about the
  *distiller* rather than about review capacity, and running `skill-validate` on local skills at
  write time so form converges even where content does not. All wait on the local tier existing.
- One constraint that binds whatever is built: `D-2026-08-25` ends with **no Temporal Schedule opens
  a pull request**. A reconciliation job may cluster, measure and report; the proposal is opened on
  demand by a human reading that report.

## Consequences

- The review page answers "has this been here before" without a second request, which is the
  question a reviewer asks first and previously could only answer by remembering.
- **A rejection now costs the next proposer something.** Writing a real reason was already required;
  it now has a reader, which is what makes the requirement more than a form field.
- The store protocol gains a method, so a third backend must implement it. There are two, both here.
- Nothing about the flood is solved for a queue that is actually flooded — this removes *repeated*
  decisions, not concurrent ones. The mechanisms that address volume are listed above and are
  blocked on the same corpus everything else in this area is blocked on.
