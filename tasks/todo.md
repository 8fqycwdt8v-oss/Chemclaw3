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
- [x] The full serial suite — **1 failed, 10505 passed**: a new `degraded` subsystem label
      (`stored_skill_manifest`) undeclared in `tests/test_degraded.py`'s label space. Declared, with
      the reason it is separate from the filed tier's.
- [x] Two fresh-context adversarial reviews. Six findings, all fixed, listed below.
- [x] The three mutations that previously left the file green now each turn it red.
- [ ] `make lint type` immediately before the commit (lesson 107), and the full suite re-run over the
      review fixes.

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

Lessons 112–120 added.

### What the two reviews found

**My headline measurement for `UnreservedNames` measured a different narrowing.** I drove it with a
`skill_role_gates` entry, saw the personal copy absent, and put that in the docstring, the test, the
ADR and the commit message. `RoleScopedSkills` is in the same composition, so the gate was doing all
the work: a mutation review showed the test stayed green both with `UnreservedNames` deleted and with
`reserved=` emptied at the production call site. Driven over four arms, the mechanism that binds is the
**enable-list** — the one narrowing this change moves. Re-derived further: the row's own premise was
true when written, closed silently since by `D-2026-09-20`'s backend predicate, and **re-opened by the
`EnabledSkills` fix in this same commit**, which is the real reason the two rows belong together. Test,
docstring, ADR and commit message all corrected; a second test now records the role-gate scenario as
*not* the mechanism, so the next reader does not reach for it again.

**The `/mine` ↔ `/org` merge was backwards.** I keyed a colliding name from the personal body, citing
mount order. Measured: `_skills_middleware` lists `/mine`, `/org`, filed, and upstream resolves
*last*-source-wins, so the **organisation's** body is served — `local_skills.save_local_skill` says so
in its own comment. One person's private document was deciding the visibility of a skill acting on
everybody's turns. The tiers are now read `/mine` first, and the test asserts the served path beside
the declaration, which is the assertion whose absence let the wrong direction pass.

**The reader resolved the actor from a different spelling than the mount.** `api/runner.py` passed the
request's raw value; `scratchpad_backend` resolves through `get_current_actor()`, which strips. For a
padded oid the two spelled one actor two ways, the declarations came back empty, and a missing entry
reads as "declares nothing" — the whole `/mine` tier silently unscoped. Fixed at the root: the reader
takes no actor and asks the same ambient the mount asks.

**The stored `requires:` half bought nothing.** Dropping it from the merge left the file green, because
the assertions read the reader's map rather than the visibility it buys. Now driven through the mount,
with the arm where `tools:` alone would keep the skill visible.

**A log line could carry a person's own words.** `_unreadable(str(exc))` passed parser text through, and
a YAML parser quotes what it choked on — a body with `name: !project_<something> x` put that tag
verbatim into a shared WARNING, against the module docstring's own absolute claim. It now logs the
exception *type*.

Plus four count/wording corrections: `paged_items` has five call sites in three modules (not "three
callers"), with `scratchpad.py`'s eviction walk named as the fourth copy deliberately left alone;
`_name_of` is *stricter* than both listings rather than the same filter; `StoredSkillTools`' maps are
read-only by convention, not by `frozen=True`; `StoredSkillTools.__bool__` was dead and is gone.
