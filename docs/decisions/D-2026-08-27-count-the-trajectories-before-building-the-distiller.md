# D-2026-08-27-count-the-trajectories-before-building-the-distiller — the distillation loop gets its measurement instrument, and the generator waits for the number

## Status

Accepted. Ships `chemclaw.cli.trajectory_census` (`make trajectory-census`) and defines the
trigger the BACKLOG "Memory records; it does not change what the next turn does" row has been
waiting on. The generator itself is **not** built — that is this decision's point, not its gap.

## Context

Six memory tiers exist and all six are read on request; nothing changes the agent's behaviour on
the next turn unless a human writes a `SKILL.md`. The 2026 literature (SkillRL, SkillForge, the
self-evolving-agent surveys) abstracts recurring trajectories into reusable procedure
automatically, and this repository's own PR-gate is the right control for such a generator — a
proposed skill is exactly the shape the gate already carries, and the skills tree stays
agent-unwritable either way.

The BACKLOG row demands a measurement before a mechanism: over the sessions on disk, how many
recurring trajectories *are* there, and would a distilled one have changed a later answer? The
2026-08-25 attempt found the corpus empty — 12 session messages, 0 turns, all from that day's own
probe run — so the row is blocked on deployment history, not on effort. What that leaves undefined
is the measurement itself: "recurring trajectory" had no computable definition, so the day a
deployment has sessions, whoever opens the row would first have to invent one — and an instrument
invented on the day it is needed measures whatever is convenient that day.

## Decision

**Define the measurement now, run it whenever, build the generator only when it says to.**

A **trajectory** is one turn's tool-call sequence: the tool names an assistant emitted between one
human message and the next, in order, with consecutive duplicates collapsed (a retry is not a
step). A trajectory **recurs** when the identical normalized sequence of length ≥ 2 appears in two
or more distinct sessions. A recurrence **would have helped** when a later session's first
occurrence follows an earlier session's — the case where a skill distilled from the first
occurrence could have changed the later answer, which is the row's own question made computable.

`chemclaw.cli.trajectory_census` computes exactly that over `session_messages`, decoding rows
through `session_store.message_from_row` (the one function allowed to decide which serialization
a row holds). It prints the totals, the recurring classes with their session spread, and the
would-have-helped subset; `--json` emits the machine-readable form. An empty store prints zeros
rather than refusing, so the BACKLOG row's "the corpus does not exist" stays a number anyone can
re-produce with one command.

**The trigger that greenlights building the generator**, stated here so the greenlight is a
measurement rather than a mood: on a deployment's real sessions, the census reports

- **≥ 5 distinct recurring classes**, across **≥ 3 sessions**, with
- **≥ 1 would-have-helped recurrence of length ≥ 3** (a multi-tool procedure, not a lookup pair).

Below that, a generator would be built against a corpus that does not recur enough to reuse —
a routing hypothesis nobody measured, which is the D-2026-08-15 mistake this row already cites.

## What the generator will owe when it is built

Recorded now so the next session does not re-derive it: the trajectory→`SKILL.md` proposal goes
through the **PR-gate** (the agent proposes, a human decides — `SkillsReadOnlyRefusal` stays); the
trigger signal for proposing is an open UX question — a consumed plan approval with no follow-up
correction is the candidate that exists today, an explicit answer-feedback control is the candidate
that does not — and deciding between them belongs to the generator's own ADR, informed by what the
census found recurring.

## Consequences

- The BACKLOG row loses its duplicate (the same text was pasted twice, once with and once without
  the 2026-08-25 measurement paragraph) and the surviving row now names the instrument, so its
  trigger is checkable the day a deployment has sessions.
- The census reads the durable read-model, not the checkpointer: it measures what turns *did*
  (the transcript's tool calls), not what a graph might replay. A deployment that prunes
  `session_messages` by retention measures its retention window — stated in the CLI's own output.
