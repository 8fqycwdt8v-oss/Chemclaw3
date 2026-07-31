# D-161 — The human gate moves from every observation to the few worth promoting

## Status

Accepted. Implements W3.3 of the dataflow review's plan, and carries W3.2 (the memory-job cap) with
it. Depends on D-160, which shipped first for the reason recorded there.

## Context

Knowledge has had exactly one tier and one gate. Anything an agent writes is proposed as a note and
a human merges it before it counts (D-005). That is right for anything asserted as fact, and it is
also the reason there is no proactive cross-project learning loop: the only way to record something
noticed is to open a PR about it, so every candidate learning costs a reviewer, and most candidates
do not earn one.

The effect is a specific and invisible loss. `find_playbook_candidates` keeps `SUCCESS` runs only
(KNW-3), and correctly — distilling a recurring failure into a playbook would invert what the
record says. But that filter discards the *finding* along with the recommendation. "This
transformation has gone badly in three separate projects" is exactly what a process chemist wants
before trying it in a fourth, and there is nowhere in the system it can be said. The same is true
of a chemist's question answered from two projects' reactions: the transfer happened, in one
conversation, and a third project cannot find it.

## Decision

**A second knowledge tier that is not gated, because it does not claim to be true — and a
promotion path that puts the gate back exactly where a claim is made.**

### Postgres, not Git

Git's value is human review, diff and audit. With no review it buys PR noise and repo churn and
returns nothing. A table gives cheap upsert-accumulation of support, TTL eviction (the artifact
eviction workflow is the precedent), and no branch-per-note explosion.

This *preserves* "git is the source of truth" precisely because observations are explicitly not
truth. Putting them in the graph would be the change that breaks that rule, not the change that
keeps it.

### Support is a count of merged notes, not a counter

`support` is `len(evidence_note_ids)`, derived. A stored counter can be incremented by something
that is not a merged note; a derived count cannot be.

Migration `025` adds the other half as a CHECK: an observation id may never appear in
`evidence_note_ids`. The failure this prevents is the one that would be hardest to see from
outside — the agent writes an observation, a later run retrieves its own observation, counts it as
corroboration, and inflates past the promotion threshold into a PR. A self-confirming loop wearing
the costume of cross-project evidence. `Observation` refuses the same thing at construction, so a
miner fails where it is written rather than at the insert; the constraint is the guarantee and the
validator is the error message.

### The anti-feedback rule decides what may be mined

This is the finding that shaped the miners, and it is the reverse of how the plan framed it.
Support counts merged notes, so a miner producing observations backed by anything else produces
observations that can never accumulate support and therefore never promote — a write-only log with
extra steps. **Raw session transcripts are the concrete case this rules out**, even though "mine
interaction history" was the plan's phrasing.

So both miners read only merged notes:

- **`mine_corpus`** takes cross-project clusters that the playbook bar discards — the non-success
  ones — and states what the record shows rather than what to do about it. The same cluster that
  makes an inadmissible playbook makes a legitimate thing to notice.
- **`mine_interactions`** takes merged `interaction` notes whose *own cited evidence* spans more
  than one project. A confirmed answer is already a merged note, so it is admissible support; what
  nothing reads today is that its evidence crossed a project boundary. Project attribution is
  derived by resolving the note's wikilinks against the reaction corpus, because an `interaction`
  note has no project field of its own.

Interactions are deliberately **not** clustered by topic. Grouping questions by prose similarity
would mint findings out of phrasing, which this repo has already ruled out for hypotheses (D-162):
a pattern-matched motive is indistinguishable downstream from testimony.

### Retrieval: a separate call, not a labelled bucket in the same list

`recall_observations` is its own agent tool. Making it a second field on `gather_evidence`'s return
would have put both kinds of thing in one call and left the separation to a label, and the label is
the part a model skips. A separate call means an observation cannot arrive as an evidence chunk by
any path, and the instruction that governs it is short enough to hold: *an observation may direct
what you look for; it may never be the evidence for a claim.*

### Promotion returns to the ordinary PR-gate

Crossing both thresholds — `observation_promote_min_evidence` (3) and
`observation_promote_min_projects` (2) — opens **one** playbook PR through the existing
`propose_note`. No second write path into the graph (D-019/D-078). The tier's entire contribution
is deciding *which* candidates are worth a reviewer's time; it decides nothing about what is true.

Two thresholds rather than one because they answer different questions: evidence count says the
finding is not a coincidence, project count says it is not one team's local habit. A finding with
ten notes from a single project is a well-evidenced *episodic* fact, which the campaign layer
already covers.

The status flips to `promoted` only after the PR exists. Flipping first would lose the observation
if the submission failed — no longer open, so nothing retries it — at the one moment it had proved
itself worth keeping.

### Off by default

`observations_enabled` is `False` and the Schedule is not registered without it. This is the first
knowledge surface the agent can read that no human signed off, and a deployment must choose that
rather than inherit it.

## What was dropped, and why

- **`contradiction_count`.** The plan expected it "nearly free" from `kg/conflicts.py`. It is not:
  that detector compares two notes' claims about the same *compound* with a confidence gap, and an
  observation is a statement about a transformation class or a conversation. Wiring a column
  nothing can populate would be the "reserved for later" stub this repo deletes on sight. The
  promotion rule is therefore the two thresholds, and nothing pretends to check contradictions.
- **`support_count` and `first_seen`/`last_seen` as separate concepts from status.** Support is
  derived (above); `last_seen` is refreshed by every run that still finds the finding, which is
  what makes `retire_stale` mean "the corpus stopped supporting this" rather than "this is old".

## Consequences

- The tier is instrumented to be *deletable*. `retire_stale` returning numbers close to the mining
  rate says the miners produce noise; a promotion rate of zero over a quarter says the tier is a
  write-only log and should be removed rather than defended. Both are logged.
- The workflow's three steps run in one order for one reason: mine (refreshing `last_seen` for
  everything still supported), then retire (so only what was *not* re-observed ages out), then
  promote (so a row cannot be retired in the same run that would have promoted it).
- D-160 is what makes a mistake here recoverable. An observation's claims reach the model labelled;
  a promoted observation becomes an `agent`-authored note whose provenance the evidence sweep now
  reports. Without that, a promoted observation would be indistinguishable from human-merged
  knowledge the moment its PR merged.
- **W3.2 rides here**: `memory_max_notes_per_run` caps what one synthesis run may propose. The cap
  rotates its window by run date rather than truncating, because the builders are deterministic
  over the corpus — `notes[:cap]` would propose the same first N every night and the tail never,
  trading a visible flood for silently lost knowledge.
- `all_reactions` in `durable/memory_jobs.py` lost its underscore: it is now read by two durable
  modules, and a private cross-module import is a worse signal than a public helper.
