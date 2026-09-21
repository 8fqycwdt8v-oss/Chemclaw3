# D-2026-09-21-a-stored-tier-and-a-filed-tree-are-not-asked-the-same-question — one narrowing per kind of skills tier

**Status:** accepted · **Date:** 2026-09-21 · Supersedes nothing. Closes two `BACKLOG.md` rows —
*"`ToolScopedSkills` is applied to neither stored tier, so a skill about tools the turn cannot reach
is still offered"* and *"A shared skill that a narrowing hides leaves its name to the personal tier,
which the collision order was chosen to prevent"* — and fixes a third defect neither row names, which
is what makes this a decision rather than two commits.

## Context

Three skills tiers are mounted on one backend per turn: the reviewed trees in git (`/skills/…`, plus
each enabled bundle's own), the organisation's stored tier (`/org/…`) and the chemist's own
(`/mine/…`). `agent/langgraph_agent.skill_narrowing` computed **one** predicate and handed it to every
mount, and its docstring said so as an invariant: *"one predicate for every tier"*, because "a
narrowing that answered differently depending on which mount asked would not be a narrowing".

That invariant conflated two things. **Computing the answer once** is what closed a real hole — the
personal tier shipped narrowed in the *prompt* and not at the backend, so `skill_names: []`, the eval
control arm whose whole job is removing skills, advertised nothing and still served every body to
anyone who guessed a path. **Every tier getting the same answer** is a different claim, and it is
false: three of the four narrowings take their basis from configuration that `make skill-validate`
checks against the *discovered* trees, so none of them can name a stored skill.

Driven through `scratchpad_backend` over an `InMemoryStore` before any code was written:

| narrowing | what both stored tiers' module docstrings claimed | what was measured |
|---|---|---|
| `EnabledSkills` | `local_skills.py`: "would delete the tier outright", as its reason for not applying. `org_skills.py`: "It is **not** narrowed by `EnabledSkills`, which names *shipped* skills and would therefore delete this tier outright" | **emptied both tiers.** With `CHEMCLAW_SKILLS_ENABLED=development-report`, `ls('/mine/')` and `ls('/org/')` both returned `[]` |
| `ProfileScopedSkills` | `local_skills.py`: "must not" apply | applies, **deliberately**, with a test (`tests/test_org_skills.py::test_the_control_arm_that_removes_skills_removes_this_tier_too`) |
| `RoleScopedSkills` | `local_skills.py`: "must not" apply | applies, and is a no-op: its keys are validated against the filed trees |
| `ToolScopedSkills` | "the one that would have been worth keeping, and it is not applied" | correct, and correct **by accident** — the predicate runs, the declaration map has no entry, and `_permits` reads a missing entry as "declares nothing" |

So the paragraph that existed to say plainly which narrowings applied was wrong about all four, in
both directions. The one it wanted was absent; three it disclaimed were present, and one of those
silently deleted a tier that acts on everybody's turns.

The capability gap has its own measurement. A personal skill declaring
`tools: [compute_thermochemistry, sample_conformers]` was served and listed in a turn that bound
**zero** tools, while **34 of 39** filed skills were hidden by that same predicate in that same turn.
A narrowing that runs and cannot narrow is worse than an absent one, because the prose says it is
applied.

The name half is older than `SkillManifest.requires`. Upstream resolves a collision between two
mounts by *listing*, so a shared skill that any narrowing removes is not there to displace anything:
`skills/deep-research/SKILL.md` carries no `requires:`, and one `skill_role_gates` entry the caller's
roles do not satisfy took the listing from `/skills/deep-research/SKILL.md` to
`/mine/deep-research/SKILL.md`. Both write doors already refuse a stored skill under a name
`shipped_skill_names()` occupies; what they cannot refuse is a body stored before that tree shipped the
name, or a name a later commit moved *into* `skills/`.

## Decision

**`skill_permits` returns a `SkillNarrowing` with one predicate per kind of tier**, built from one
tuple of narrowing objects. `skills_backend` binds `.filed`; `scratchpad_backend` binds `.stored` for
both stored mounts.

- **`EnabledSkills` is in `filed` only.** A narrowing whose basis cannot name a member of a set cannot
  narrow that set, it can only empty it. This is the defect above, fixed structurally rather than
  asserted in prose a second time.
- **`ToolScopedSkills` applies to every tier**, reading the stored declarations from the bodies the
  tiers already hold. `agent/stored_skill_tools.stored_skill_declarations` is the reader, and
  `skill_manifest.declared_triple` is the single parse both a file and a body go through.
- **`UnreservedNames` is in `stored` only** — the fifth narrowing, and the first that belongs to a kind
  of tier rather than to a kind of question. A stored skill under a name this deployment ships is never
  served, so the read side agrees with the write side's *discovered* basis, which is the invariant the
  second row asked for.
- **`ProfileScopedSkills` and `RoleScopedSkills` stay in both**, each for a reason rather than by
  default; see *Consequences*.

**`ToolScopedSkills` is fixed by reading, not by the parse-on-write the row proposed.** The row's
cheap shape was to parse each body in the two publish routes and keep the declared tools beside it.
`BaseStore.asearch` already returns each item's whole `value`, `content` included, so the declarations
come off the same paged walk a listing already costs — one source of truth (the body), no second
stored artefact, no migration for skills already saved, and nothing that can drift from what the model
reads. The row's actual objection was parsing "inside a possibly-synchronous `ls`", and that is
answered by *where* this runs: the async caller that already builds the store, which is the seam
`store` and `checkpointer` established precisely because `build_langgraph_agent` is synchronous and
must stay so.

## Consequences

**`ProfileScopedSkills` keeps applying to the stored tiers, and that is chosen.** `skill_names:
frozenset()` is a profile author writing down that this agent reaches no skill at all — the
`skills-removed.yaml` control arm — and a tier escaping it would make every A/B result measured
against that arm a comparison with a system that still had skills. The residual is recorded rather
than hidden: a profile naming a *non-empty* `skill_names` removes both stored tiers as collateral, for
the same structural reason the enable-list did, because that field is validated against the filed trees
too. It is kept because the measurement case wants it — an arm naming its skill surface means that
surface — and because no shipped profile sets the field at all, so this is a latent consequence of a
deliberate rule rather than a live defect.

**`RoleScopedSkills` keeps applying and is a no-op.** Its keys are validated against the filed trees,
so the only stored name it can reach is one a filed tree also holds — the case `UnreservedNames`
removes from the listing regardless.

**No stored skill's name reaches a log line or a metric label.** `_log_narrowing` writes one DEBUG
line per build naming what a profile was offered, and it keeps counting the **filed** map even though
the narrowing now reads both: a personal skill's name is a person's own words, which is the reason
`local_skills._count_a_local_load` refuses to put one in a metric label. `degraded` in the new reader
reports an unreadable body without naming it.

**An unreadable stored body is scoped to nothing**, as an unreadable filed manifest is. Dropping the
entry would make an unparseable body a *widening*, which is the defect `_declared_pair`'s own `except`
arm exists to refuse. Both write doors run `validated_skill`, so this is reachable only for a body
stored before a rule tightened — the same class of thing `UnreservedNames` closes on the name.

**The filed declaration wins a name both hold, and the direction is load-bearing.** Both predicates
read one merged declaration map, so merging the stored entries *over* the filed ones lets a stored
body describe a filed one. Driven at that revision: a grandfathered `/mine/deep-research` declaring
one tool nothing binds made the **reviewed** `deep-research` invisible, in a turn that bound all
twelve tools it declares. `UnreservedNames` guarantees the stored body of a shipped name is never
served, which is exactly why its declaration must not describe the body that is.

**One paging walk, not three.** `skill_store.paged_items` now serves both tiers' listings and this
reader; it was two copies of the same loop before the third caller arrived.

**Cost per turn:** one `asearch` page per mounted tier, on namespaces capped by
`agent_local_skills_max` and `agent_org_skills_max`, hoisted beside the two awaits already there.
Nothing is added to a turn that mounts no stored tier — the CLI, a template step and every helper pass
no store, so they pass no declarations either.

## Alternatives considered

- **Parse on write, keeping the declarations beside the body** — the row's own proposal. Refused
  above: a second stored artefact per skill, a migration, and a thing that can disagree with the body.
- **Keep one predicate and exclude nothing, documenting that an enable-list disables the stored
  tiers.** This is what shipped, minus the belief that it did not. Refused because there is no
  configuration that works around it: a deployment cannot name a stored skill in `skills_enabled`, so
  the setting's only possible effect on those tiers is deletion.
- **Hide the *personal* copy of a reserved name and nothing else.** Refused because it is the same
  rule with a smaller subject: an organisation's skill under a shipped name is the same collision with
  a larger blast radius, and `org_skills.py`'s write door already refuses new ones for that reason.
- **Make `UnreservedNames` a fifth entry in one shared narrowing.** Refused: asked of a filed tree it
  would hide every shipped skill from itself. It is the clearest case that the partition is what makes
  the narrowing safe to add at all, and `tests/test_stored_skill_tools.py` asserts both directions.
