# Wave 3 — the two stored skills tiers are narrowed by the wrong three of four

Two backlog rows ([S] each) plus a defect neither names, which the measurement found and which both
stored-tier modules' own docstrings say cannot happen.

## What was measured before any code was written

Driven through `scratchpad_backend` over an `InMemoryStore`, at zero bound tools:

| narrowing | what the prose claims | what happens |
|---|---|---|
| `EnabledSkills` | `local_skills.py`: "would delete the tier outright" (as a reason it does not apply); `org_skills.py`: "It is **not** narrowed by `EnabledSkills` … would therefore delete this tier outright" | **deletes both tiers outright.** `CHEMCLAW_SKILLS_ENABLED=development-report` → `ls('/mine/')` and `ls('/org/')` both `[]` |
| `ProfileScopedSkills` | `local_skills.py`: "must not" apply | applies, **deliberately** — `test_org_skills.py::test_the_control_arm_that_removes_skills_removes_this_tier_too` |
| `RoleScopedSkills` | `local_skills.py`: "must not" apply | applies; a no-op in practice because `skill_role_gates`' keys are validated against the *filed* trees |
| `ToolScopedSkills` | "the one that would have been worth keeping, and it is not applied" | correct — and correct **by accident**: the predicate runs, the declaration map simply has no entry, which `_permits` reads as "declares nothing" and leaves visible |

R6's own control: a personal skill declaring `[compute_thermochemistry, sample_conformers]` is served
and listed at zero bound tools, in a turn where **34 of 39** filed skills are hidden by that same
predicate.

## The shape of the fix

One decision rather than three patches: **`skill_permits` produces a narrowing per tier, because a
stored tier and a filed tree are not asked the same question.** A narrowing whose basis is a
deployment's list of *filed* names cannot narrow a stored tier — it can only empty it.

- [ ] 1. `agent/skill_access.py` — `skill_permits` returns a frozen `SkillNarrowing(filed, stored)`
      built from **one** tuple of narrowing objects, so there is no second composition to keep in
      step. `stored` is that tuple minus `EnabledSkills`, plus the reserved-name rule (R7).
      `SkillNarrowing.permissive()` for the tests that are about something else.
- [ ] 2. `skills_backend` takes the `SkillNarrowing` and uses `.filed`; `scratchpad_backend` takes it
      and uses `.stored`, so the two cannot be swapped at a call site.
- [ ] 3. **R6** — `skill_manifest`: extract the frontmatter read from `_declared_pair` so a *body*
      and a *path* answer through one function. New `agent/stored_skill_tools.py`:
      `async def stored_skill_declarations(store, actor)`, one `asearch` per tier, fail-closed to
      `UNREADABLE_DECLARATION`. Threaded `api/runner.py` → `build_langgraph_agent(stored_skills=…)`
      → `skill_narrowing`, the same hoisted-await seam `store` and `checkpointer` already use.
      **Read rather than parse-on-write** (the row proposes the latter): `asearch` already returns
      each item's whole `value` including `content`, so the declarations come off the paged search a
      listing already costs — one source of truth, no migration, no drift.
- [ ] 4. **R6 privacy** — `_log_narrowing` keeps the **filed** map. A stored name in that DEBUG line
      would put a person's private vocabulary in a log field, which `_count_a_local_load` refuses to
      put in a metric label for exactly the same reason.
- [ ] 5. **R7** — the read side agrees with the write side's *discovered* basis: a stored skill under
      a name this deployment ships is never served. Closes the grandfathered names and the ones a
      rename into `skills/` creates; `deep-research` is the measured case.
- [ ] 6. Correct the prose in `local_skills.py` and `org_skills.py`, which is wrong about all four.
- [ ] 7. ADR: a stored tier and a filed tree are not asked the same question.
- [ ] 8. Delete both backlog rows in the commit that closes them.

## Verification

- [x] Every claim in the table above as a test, both arms — `tests/test_stored_skill_tools.py`, 17
      tests, each with its defect arm (`stored=False`, `reserved=frozenset()`, the enable-list arm
      that still hides a filed skill).
- [x] The full serial suite.
- [ ] `make lint type` immediately before the commit (lesson 107).
- [ ] Fresh-context subagent review before the PR.

## Review

All eight steps done. Three things worth recording beyond the diff:

**The row's framing was inverted and the measurement is what said so.** R6 asked for one narrowing to
be *added*; what was actually wrong was that three narrowings were being applied that two module
docstrings said were not, and one of them (`EnabledSkills`) emptied both stored tiers outright. Both
docstrings named that exact outcome as their reason for believing it did not happen. Re-checking each
row against `HEAD` before writing code is what found it — the fourth time in this wave series that a
row's own framing was stale.

**I introduced a defect and caught it by measuring a comment I had written.** The declaration merge
originally put the stored entries over the filed ones, with a comment arguing the collision could not
happen because `UnreservedNames` removes it. That is true of the stored predicate and silent about the
filed one, which reads the same map: a grandfathered `/mine/deep-research` declaring one unbindable
tool made the **reviewed** `deep-research` invisible in a turn binding all twelve tools it declares.
Driven, fixed, and pinned by
`test_a_stored_declaration_cannot_hide_the_reviewed_skill_of_that_name`.

**One privacy hazard avoided rather than shipped.** Merging the stored names into the map
`_log_narrowing` reads would have put every personal skill's name into a DEBUG field on every turn its
owner takes — the thing `local_skills._count_a_local_load` refuses to do to a metric label. The
narrowing reads both maps; the log keeps the filed one.

Lessons 112–116 added.
