# D-2026-09-05-the-gate-follows-behaviour-not-knowledge — knowledge is global the moment it is learned; a behaviour change waits for an admin before it reaches a second person

**Status:** accepted · **Date:** 2026-09-05 · **Builds on:**
D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks (the premise
D-005 was written under is gone), D-2026-08-25-an-eln-transcription-is-data-not-a-claim (the gate
is a credibility budget), D-161 (an ungated tier, and the self-confirmation guard), D-160
(provenance on the evidence sweep) · **Narrows** D-005: the PR-gate stops being the answer to
"an agent wrote something" and becomes the answer to "an agent would change how the agent behaves".

## Context

D-005 put a human in front of everything an agent writes. It was written when this system described
itself in GxP vocabulary, and that premise is gone:
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` removed the
regulatory framing, re-derived two surviving controls on their own terms — bi-temporal validity as
*retrieval correctness*, the erasure tier as *the only record of a tool call* — and left the PR-gate
under "Everything else keeps working, and only its wording changes". The one control now in question
is the one that got a rename instead of an argument.

The boundary has since moved twice anyway, both times on measurement rather than taste:

- `D-2026-08-25-an-eln-transcription-is-data-not-a-claim` withdrew the gate from ELN ingestion, and
  gave the rule the rest of this decision follows: *"three hundred rubber-stamp merges a day is the
  training regime that produces a reviewer who also rubber-stamps the distilled playbook that did
  need reading. The gate was spending its credibility on the cases that did not need it."* The gate
  is a **finite credibility budget**, not a free safety property.
- `D-2026-08-06-a-share-is-mounted-not-called` admitted a mounted share's documents as cited
  evidence rather than PR-gated notes, and D-161 opened an ungated observation tier outright.

What none of that settled is the general rule, so each path argued it again from scratch. And it
leaves the system unable to do the thing it most obviously should: **learn, in the moment, from its
own error and from what a chemist just corrected**, and have the next person benefit. Today that
learning either opens a pull request nobody drains or does not happen.

## Decision

**The gate follows *behaviour*, not *knowledge*. One axis: does this change what the agent does?**

### 1. Knowledge is global the moment it is learned, and is not gated

Anything the system learns — from a failed calculation, from a chemist's correction, from what a
user told it, from a completed job — becomes readable by everyone immediately. No pull request, no
reviewer, no queue. A chemist who tells the agent that a recommendation was wrong has, by saying so,
recorded it for the next person.

**Three properties already in the tree are what make that safe, and they replace the reviewer:**

- **It arrives labelled.** D-160 put provenance on the evidence sweep precisely so an
  `agent`-authored note is distinguishable from human-curated knowledge at the point of use. An
  ungated note that says what it is is a different object from one wearing a reviewer's approval.
- **It arrives with its citations.** Knowledge is read as evidence, next to the reactions and notes
  it rests on, and a chemist checks it where it is used. This is the asymmetry the whole decision
  turns on: a wrong note is *visible where it is consumed*.
- **It can be contradicted.** `memory/failure.py` writes a `contradicts` edge rather than editing the
  refuted note, `kg/conflicts.py` surfaces the disagreement, `memory/supersede.py` retires an older
  finding without deleting it, and bi-temporal `valid_to`/`is_current` keep a superseded note out of
  current evidence. **Correction, not pre-approval, is the control on knowledge** — and it is the
  one that scales with a corpus a reviewer could never read.

### 2. A behaviour change that reaches everyone is gated, and the reviewer is an admin

A skill in the shared tree is the case: it is injected into the prompt and silently reshapes how
every later answer is formed, for everyone, **with no citation trail**. Nobody reads a skill at the point of use; they read
its consequences without knowing it ran. That is the exact inverse of the property that makes ungated
knowledge safe, and it is why the same argument that frees knowledge binds behaviour.

`skills/playbook-distillation/SKILL.md` already reasons this way about a playbook note — more
authority, so a process chemist reviews it — and a skill sits a level above that.

**The reviewer is an admin, not the chemist who triggered it.** The role set exists: `_is_reviewer`
(`api/deps.py`) already gates the proposal queue on `entra_privileged_roles`, on the stated grounds
that *"signing off on machine-written knowledge is the most consequential write in the system, so
inventing a second, weaker role for it would be strange"*. That sentence is now true of behaviour
rather than of knowledge, and the role moves with it. An end user never sees this gate; a platform
owner decides it, for the deployment, once.

`SkillsReadOnlyRefusal` stays: no agent path writes a skill directly, whatever else changes.

**What "reaches everyone" excludes is section 3**, and the exclusion is deliberate rather than an
oversight in this rule: a skill acting only on the turns of the person it was distilled for is
answerable to that person, who can see it and delete it. The gate is for the step where it stops
being theirs.

### 3. A skill is live for its own user before review, and global only after

Section 2 is the rule for the **shared** tree. A distilled skill lands first in the chemist's own
skills directory, where it is active for their turns immediately and for nobody else's; promotion
into `skills/` in git is the admin gate above. So the end user never waits on a review, and no
unreviewed instruction ever reaches a second person.

**This was proposed, then withdrawn, then taken** — recorded because the reasoning matters more than
the outcome. It was withdrawn on the reading that "only after human review" is absolute; the owner's
answer is that it applies to *shared* behaviour, and the local tier is the point of the design rather
than a concession in it. The objection raised against it stands as a **cost**, not a refutation, and
is stated below rather than dropped.

**The invariants the local tier owes**, so whoever builds it does not re-derive them:

- **Per-actor, resolved per turn.** `agent/langgraph_agent.py::_skill_directories` is the seam — read
  per turn because the graph is compiled per turn, so ambient identity is reachable there exactly the
  way `agent/skill_access.py`'s role gate reads it. A directory resolved once at startup would be one
  user's skills served to everyone, which is the shared tier with no gate.
- **It is never a source of shared truth.** A local skill may shape its own user's turns and may not
  be cited, promoted automatically, or read by another actor's turn.
- **The user can see what it does.** This is the answer to the cost below and is a requirement, not a
  nicety: a chemist must be able to list and read the local skills acting on their turns, and remove
  one. A behaviour change nobody can inspect is the property that makes the *shared* tier need a gate;
  the local tier earns its exemption by being visible to the one person it affects.
- **`SkillsReadOnlyRefusal` is unchanged.** No agent tool writes a skill, local or shared. The
  distiller is a separate, deliberate write path, and it is the only one.

**The accepted cost, stated because it is real.** Two chemists can now get different answers to the
same question, and the record of why is a file in one of their directories rather than in git. That
is a genuine loss of the property the shared tree has, taken deliberately in exchange for a loop that
closes without a human in it — and the inspectability requirement above is what keeps it debuggable
rather than mysterious.

## What this decision does not carry, and why

**Neither the local skills tier nor the code that ungates agent-asserted notes is built here, and
this ADR claims neither shipped.**

The local tier is blocked on the same thing everything in this area is blocked on: there is no
distiller, so nothing writes into a per-actor directory and nobody can populate one — building the
resolution now is `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`. What it takes is
small and is named above; what it waits on is
`D-2026-09-05-a-census-that-counts-only-success-is-blind-to-half-the-signal`'s trigger, measured on a
deployment with real sessions.

**The knowledge half is the larger follow-up.** Today `kg/pr_gate.py` still fronts job results, campaign narratives, distilled playbooks,
report drafts, `failure_note` and observation promotion. Carrying section 1 means, at minimum: a
direct write path for those note types, the note-proposal queue reduced to the behaviour cases,
`durable/retention.py`'s refusal on `note_proposals` re-argued, and the `GET /proposals` surface and
CLI narrowed to match. That is a change to the core knowledge path and it earns its own verification
rather than riding here.

**What ships with this ADR is the decision and the axis**, because every path that has faced this
question so far has re-argued it from first principles and reached a different answer, and the next
one should not have to.

**The self-confirmation guard is owed by whoever writes that follow-up**, imported from D-161's
migration `025` (*"a self-confirming loop wearing the costume of cross-project evidence"*): ungated
knowledge that the agent itself wrote must not count as independent support for a later claim of its
own. D-161 solved this with a CHECK rather than a convention, and that is the bar.

## Consequences

- **The gate gets smaller and therefore means more.** It stops being the thing a reviewer sees three
  hundred times a day and becomes the thing an admin sees when the agent's behaviour would change —
  which is the credibility argument `D-2026-08-25` made, applied where it leads.
- **A wrong ungated note is now corrected rather than prevented.** That is a real, accepted change in
  failure mode, stated plainly: the system will sometimes serve an agent-authored claim that is
  wrong, labelled as agent-authored, until somebody contradicts it. The alternative was a queue
  nobody drains, which serves nothing at all and was measured at 4.2–42 person-years per million
  entries on the one path where it was tried.
- **`skills/` stays git-resident and human-merged**, so a bad *shared* behaviour change is a
  revert — the rollback property any future skill-evolution loop rests on. A local skill's rollback
  is the user deleting it, which is why being able to see it is an invariant rather than a feature.
- D-005 is narrowed, not retired: the PR-gate mechanism, its branch/worktree submitter and the
  proposal record are all unchanged, and keep their one remaining subject.
