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

### Phase 1 — landed

Shipped: `chemclaw_skill_loads_total{skill}`, `AgentProfile.skill_names` with the
`ProfileScopedSkills` narrowing, a `skill-validate` check for it, and
`data/evals/profiles/skills-removed.yaml`. No behaviour change on any shipped profile —
`skill_names` is `None` everywhere.

**Two things measurement changed before they shipped.**

- The fan-out/compaction defect this plan listed for phase 2 is **already fixed**:
  `ClearOlderToolResultsEdit` makes `keep` the larger of the configured floor and the newest
  batch's size, so the batch survives structurally. Nothing was owed; a "fix" would have been a
  change to working code.
- The self-confirmation guard stays in phase 5 rather than moving earlier, because its caller is
  the distiller. Built now it would be a guard with no caller kept alive by its own test — the
  `reject_widening` shape this repository deleted once already.

**What a fresh-context review caught, and it was the same failure twice.** The first version of the
counter's clamp was asserted in a docstring, a metric HELP string and the ADR, and the shipped tree
falsified it: `skills/README.md` resolves, so the counter booked a skill called `README.md` — in
the one series whose stated purpose is deciding which skills to promote and retire. `limit=0`
booked a load of zero bytes under a paragraph claiming the count is taken on the bytes. Both are
fixed with the argument recorded rather than the prose quietly corrected, and both now have tests
asserting the *property* rather than the filename that exposed them. Three smaller findings went
with them: the validator tracebacked instead of reporting when a profile file was malformed, two
tests leaked the shipped profiles into a module-global registry, and the control arm's "what it
still carries" paragraph named the listing and missed the larger residual — the deployment's prose
still orders the model to load skills the arm cannot reach, which makes a delta measured there a
lower bound rather than an unbiased estimate.

### Phase 2 — what the measurement decided before any code

Three of the six shipped profiles are **unusable as helpers**, because `helper_profile` subtracts
`side_effecting_tools()` and their job is writing: `reporting` keeps 3 of 8, `property-lookup` 1 of
5, and `design` keeps 4 of 8 but loses `suggest_next_experiment`, which is the whole specialist.
What survives coherently is `evidence` (14 of 15), `computation` (12 of 41) and `safety` (4 of 6).
So the roster is those three plus `general-purpose`, which is a measured choice and a much smaller
prefix cost than the six this plan assumed.

`computation`'s surviving 12 are the enumeration family, topology, the calibration ledger and
calculation lookup — a real capability, and **not** "compute things". So a roster description is a
human-written purpose sentence *plus the tool list derived from the compiled helper*, which makes
`D-2026-08-12`'s measured defect — five specialists whose descriptions were identical and carried
no capability information — structurally unrepeatable rather than fixed by hand.
