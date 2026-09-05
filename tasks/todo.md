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
  read per turn, so ambient identity is reachable there — but building it now with no generator
  to write into it is `D-2026-08-15`'s "capability that ships off". The ADR names the seam.
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

**The user's correction, mid-implementation, changed the design and is the better line.** The draft
had a *personal ungated skills tier*, gated only on promotion. The owner's axis is cleaner:
knowledge global and automatic, behaviour gated, and the behaviour gate belongs to an admin rather
than to a chemist. The personal tier is now recorded as rejected, with its reason — a per-user skill
is still a behaviour change, and it fragments answers across users with nothing recording why.

**Two claims were checked rather than assumed before being written down.** That
`session_messages` actually holds tool results with a `status` (it does — `api/runner.py` stores the
tool exchanges, and leaving them out is recorded there as a silent regression), and that
`entra_privileged_roles` is already the reviewer role (`api/deps.py::_is_reviewer`), so "an admin"
names a role that exists rather than one this ADR invents.

**Verification.** `make lint`, `make type`, `make test` green with Docker up, Postgres migrated —
so the Postgres-backed tests actually ran rather than skipping. `make prose-validate` green.
`make trajectory-census` run end to end against the live database: 0 sessions, both arms not
greenlit, which is the same verdict D-2026-08-27 recorded and the reason nothing further is built.
