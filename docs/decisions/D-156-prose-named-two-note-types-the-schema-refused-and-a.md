# D-156 — Prose named two note types the schema refused, and a lost note could not be counted

**Status:** accepted · **Date:** 2026-07-31

## Context

Two skills instruct the agent to write knowledge-graph notes of types the graph does not have.
`skills/knowledge-graph-write/SKILL.md` teaches the note vocabulary and lists `protocol` and
`experiment-batch` among the kinds to choose from; the `bo` bundle's `experiment-design` skill
tells the agent, in the one place it explains how to persist a suggestion, to
"draft it through `propose_knowledge_note` (type `experiment-batch`)".

Neither type was in `KNOWN_NOTE_TYPES`, and `chemclaw.kg.validate` fails any note whose type is
not listed. So the documented way to record a Bayesian-optimization suggestion produced a note
that `kg-validate` would reject.

It never produced a visible rejection, and that is the more interesting half. `kg-validate` runs
on the PR the proposal opens — and nothing opens that PR. `GitNoteSubmitter` pushes a branch and
returns its name; opening the pull request is described as the git platform's job and no code in
this repository does it. So the sequence was: the skill names a type, the agent writes the note,
the branch is pushed, the validator that would have objected never runs, and the proposal sits on
a ref nobody lists. The contradiction was invisible from the schema's side (nothing reads skills)
and from the skill's side (nothing reads the schema).

`make prose-validate` exists precisely for this class of defect — prose promising capability the
code does not have — but it checked only *tool* names. A note type is the same promise about the
surface the agent *writes into* rather than the one it calls.

Separately, and found while tracing where a proposal goes: `publish_note_best_effort` catches a
failed publish, logs a warning inside a Temporal workflow, and returns. That is correct for the
job — the science is already durable and a dead git remote must not fail a completed calculation —
but `chemclaw_notes_proposed_total` increments only on success. With no failure counter, a total
git outage and an idle deployment produce byte-identical exposition: zero proposals, no errors.

## Decision

**Add `protocol` and `experiment-batch` to `KNOWN_NOTE_TYPES`,** rather than removing them from
the skills. The distinction they draw is real and no existing type covers it: both record what
someone *should run*, where every other type records what was observed, computed, or distilled.
They are also distinct from `bo-candidate`, which is machine-minted from a finished durable
campaign — these are agent-drafted from an inline suggestion, so they carry a human's framing and
must cite the evidence they rest on.

**Extend `make prose-validate` with a fourth rule:** every note type named in agent-facing prose
must exist in `KNOWN_NOTE_TYPES`. Three anchored forms are matched — `` type `x` ``, `` `x` note ``,
and the parenthesised enumeration that follows a `**type**` label — deliberately not "every
backticked slug", because a note type is a lowercase slug and so is a dozen other things a skill
legitimately backticks. Narrow and true beats broad and noisy, the same trade the existing
`_BARE` rule makes with its underscore requirement.

The enumeration form matters most: it is what an agent copies from, and checking it immediately
showed the drift ran both ways. `knowledge-graph-write` named two types that did not exist **and
omitted three that did** (`interaction`, `bo-candidate`, `failure-mode`). The omission direction is
one no checker can see from prose alone — a missing entry is silence, not an error — so it is
covered by a test asserting `KNOWN_NOTE_TYPES` is a subset of what the skills teach. The three
were added to the skill with the note that each is minted by a dedicated tool and is not the
agent's to hand-draft, because listing them as free choices would invite exactly the wrong thing:
a hand-written `interaction` note that looks identical to a real one and carries none of its
evidence.

**Add `chemclaw_notes_publish_failures_total`,** incremented in the swallowed-failure path and
guarded on `workflow.unsafe.is_replaying()` so a replayed history does not re-count every failure
the workflow has ever seen — the same discipline Temporal's own workflow logger applies.

## Consequences

The two proposal types are writable, which is a prerequisite for persisting an inline BO
suggestion at all (that work is separate and larger — there is still no campaign entity for such a
note to belong to).

`prose-validate` now covers both directions of the vocabulary, so the skill that teaches note types
and the schema that accepts them cannot drift apart silently again.

A dead git remote is now distinguishable from an idle system. This does **not** make a lost note
recoverable: the failure is counted, not recorded, so there is nothing to replay. A durable record
of attempted submissions belongs with the work that gives the PR-gate an actual reviewer surface,
and is tracked in `BACKLOG.md` rather than half-built here.

The deeper finding this came out of — that the PR-gate opens no PR, notifies nobody, and offers no
list of pending proposals — is **not** addressed by this ADR. It is the largest open item in the
knowledge layer and needs its own design.
