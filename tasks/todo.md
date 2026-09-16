# Multi-agent team, evolving skills, and automatic expert selection

Five phases, each its own PR, each green under `make lint type test` before merge.

**The product requirement driving this** (owner, 2026-09-16): expert selection must happen
automatically; the chemist gets a chat interface and never configures infrastructure. Everything
stays LangChain/LangGraph-native.

**What the analysis found and this plan is shaped by**
- A supervisor holding every tool has no reason to delegate (`D-2026-08-12`), so capability-framed
  routing needs specialists to hold what the orchestrator lacks — a widening that inverts a merged
  invariant. **Not taken.** The roster here is perspective-differentiated: every helper is still an
  attenuation, varying by instructions and model route.
- The fan-out/compaction defect is **already fixed** — `ClearOlderToolResultsEdit` keeps the newest
  batch structurally. Nothing owed.
- A candidate *profile* is gradeable today; a candidate *skill* is not, because nothing varies the
  skill surface independently of the tool surface.
- The self-confirmation guard is owed, and it is owed **with its caller** — built in phase 5.

## Phase 1 — Instrument the skills surface, and make a skill gradeable
- [x] `chemclaw_skill_loads_total{skill}` at the existing `skill.read` log site
- [x] `AgentProfile.skill_names` — a fourth narrowing, only-ever-narrows, so a skill arm can vary
      skills independently of tools
- [x] `ProfileScopedSkills` narrowing in `agent/skill_access.py`, composed like the other three
- [x] `make skill-validate` checks a profile's `skill_names` against discovered skills
- [x] An eval arm profile that differs only in its skill surface
- [x] ADR

## Phase 2 — The roster: N named helpers, selected automatically in-context
- [ ] A roster setting naming which profiles are offered as helpers
- [ ] `_subagents` builds one governed entry per roster name, each an attenuation
- [ ] Each helper's `task` description derived from its profile so selection has information
      (`D-2026-08-12` measured identical descriptions costing every delegation)
- [ ] The caller stays `default`; selection is the model's ordinary tool-call decision
- [ ] ADR recording that the reason the backlog said was missing has arrived

## Phase 3 — The per-actor local skills tier
- [ ] Per-turn, actor-scoped skills directory resolved where ambient identity is reachable
- [ ] Never shared, never citable, never auto-promoted
- [ ] Routes so a chemist can list, read and delete the local skills acting on their turns
- [ ] `SkillsReadOnlyRefusal` unchanged — no agent tool writes a skill
- [ ] ADR

## Phase 4 — The proposal queue and its human gate
- [ ] A content-hashed, append-only proposal table modelled on `plan_approvals`, schema-informed by
      retired `note_proposals`
- [ ] Decision is an HTTP route, never a tool
- [ ] A rejection leaves a trace and an unchanged re-proposal cannot reopen it
- [ ] A proposer can learn what became of its proposal
- [ ] ADR

## Phase 5 — The proposers
- [ ] Profile proposer over session tool co-occurrence
- [ ] Skill distiller over recurring trajectories and human protocol corrections
- [ ] **The self-confirmation guard, with its caller**: nothing distilled may count evidence it
      itself produced
- [ ] Approval writes the local tier (phase 3); the shared tree stays git-merged by a human
- [ ] ADR

## Review
(filled in as phases land)
