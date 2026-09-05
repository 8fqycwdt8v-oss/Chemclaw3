# Task — WikiSkill relevance: the skills tier boundary and the census's blind arm

Source: arXiv 2608.27454v1 (WikiSkill). Three review rounds established that two of the three
"blockers" against a skill-evolution loop here do not hold, and that the third — the trajectory
census (D-2026-08-27) — measures a signal this paper does not run on.

## Plan

- [x] 1. Census extension: a second recurrence arm over recurring **failure**, read from the same
      `session_messages` read-model (`ToolMessage.status == "error"` is persisted — verified).
      Existing keys and thresholds unchanged; new arm additive.
- [x] 2. Tests for the new arm in `tests/test_trajectory_census.py`, holding each definition.
- [x] 3. ADR: skills get D-161's two-tier treatment. Ungated personal tier, gated promotion,
      the fitness-function constraint from D-2026-08-16, and the census's second arm.
- [x] 4. Ledger row in `docs/decisions/README.md`.
- [x] 5. BACKLOG row rewritten to name the second arm and the tier decision.
- [x] 6. `make lint type test` green; report what the run skipped.

## Deliberately NOT built

- The per-actor personal skills directory. `_skill_directories()` (`langgraph_agent.py:759`) is
  read per turn, so ambient identity is reachable there — but nothing writes a per-actor directory
  until a distiller exists, so building the resolution now is `D-2026-08-15`'s "capability that
  ships off". The ADR states the tier's invariants so they are not re-derived.
- The distiller itself. D-2026-08-27's posture is unchanged: define the measurement, build when
  it says to. This adds the missing half of the measurement, not the generator.
- The code that ungates agent-asserted notes. `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`
  decides it and explicitly does not claim it shipped: it needs a direct write path for job
  results, campaign narratives, playbooks, report drafts and `failure_note`, the proposal queue
  narrowed to behaviour cases, `durable/retention.py`'s `note_proposals` refusal re-argued, and
  `GET /proposals` plus the CLI narrowed — a change to the core knowledge path that earns its own
  verification.

## Review

**What shipped.** One code change (`chemclaw.cli.trajectory_census`, the second arm) with 11 new
tests, and two ADRs. Split into two because the census arm and the gate boundary are two decisions,
and CLAUDE.md's rule is that an id naming two of them is the failure the ledger prevents.

**The design took two corrections from the owner and the second reversed the first.** The draft had
a personal ungated skills tier; the owner's mid-turn message ("skills only after human review") read
as absolute, so it was written up as *rejected*; the owner then clarified that the review applies to
the **shared** tree and the local tier is the point of the design. It is now the decision. The
objection raised against it was kept as an accepted cost rather than dropped — two chemists can get
different answers with the reason in a file rather than in git — and answered by an invariant: a
user must be able to list, read and delete the local skills acting on their turns.

**The lesson worth carrying** (also in `tasks/lessons.md`): an absolute-sounding qualifier in a
one-line design instruction is the place to ask rather than infer. "Only after human review" had two
readings that differ in exactly one property — whether an unreviewed skill may touch its own author's
turns — and a whole ADR section was written the wrong way round before the question was put.

**Two claims were checked rather than assumed before being written down.** That
`session_messages` actually holds tool results with a `status` (it does — `api/runner.py` stores the
tool exchanges, and leaving them out is recorded there as a silent regression), and that
`entra_privileged_roles` is already the reviewer role (`api/deps.py::_is_reviewer`), so "an admin"
names a role that exists rather than one this ADR invents.

**Verification.** `make lint`, `make type`, `make test` green with Docker up, Postgres migrated —
so the Postgres-backed tests actually ran rather than skipping. `make prose-validate` green.
`make trajectory-census` run end to end against the live database: 0 sessions, both arms not
greenlit, which is the same verdict D-2026-08-27 recorded and the reason nothing further is built.
