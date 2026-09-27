# D-2026-09-16-a-skill-nothing-counts-is-a-skill-nobody-can-retire — the two questions an evolving skill rests on, and why neither was answerable

**Status:** accepted · **Date:** 2026-09-16

## Context

The owner asked for skills and specialist agents whose content **evolves over time under a human
gate**, with new skills and agents proposed automatically from how the system is actually used.
Every part of that rests on being able to answer two questions about a skill that already exists:
*is it used*, and *does it help*. Measured against this tree, neither was answerable.

**Nothing counted a skill.** `chemclaw_skill_reads_denied_total` was the only skill series in the
whole registry, and it counts *refusals* — so the exposition could say a gate had fired and could
never say a skill had been read. The one place that knew was an INFO log line
(`skill_backend.read`'s `skill.read`), which is reconstructible from a live pod's log stack and from
nowhere else: not in Postgres, not in `audit_events` (a skill body arrives as a generic `read_file`
tool call), not in `session_messages`. So "used N times by ≥2 distinct chemists" — the promotion
threshold every design for this has reached for, and D-161's own two-threshold shape — had no
numerator.

**And nothing could measure a skill's effect, for a structural reason rather than a missing
fixture.** The A/B harness pairs two arms across *profile files* (`evals/tool_utility.paired_tasks`),
so an arm is whatever an `AgentProfile` can express — and a profile could express a tool surface and
a prompt. The skill surface was narrowed by three predicates
(`agent/skill_access.py`), and all three belong to somebody else: `EnabledSkills` is a deployment's
one list for the whole process, `ToolScopedSkills` is a function of the tool surface, and
`RoleScopedSkills` is the caller's identity. The only per-profile lever on skills was therefore the
*indirect* one — take a tool away and `ToolScopedSkills` takes its skills with it — which is exactly
the coupling that makes the delta unattributable. That is
`D-2026-09-14-tools-were-never-the-variable` in the dimension nobody had a knob for: an arm that
moves skills necessarily moves tools, so every number it could report is a number about both.

## Decision

Three changes, and the third is the one the other two exist for.

**1. `chemclaw_skill_loads_total{skill}` counts a skill body that was actually delivered.** Three
conditions, and it takes all three to clamp the label. `skill` is the first segment of a path the
**model** wrote, and `permits` only ever narrows, so in a deployment configuring none of the three
gates it returns True for any string: counting beside the INFO line would mint a series per
invented name.

The read having resolved is the first condition and it is **not sufficient**. The argument that it
was — a resolved path is one inside `root_dir`, so its first segment is a directory that exists —
is true and is not the property wanted: `skills/README.md` exists on the shipped tree, `ls("/")`
lists it to the model, and it booked a skill called `README.md`. A series whose stated purpose is
ranking, promoting and retiring skills must not carry a row for a document that is not one. So the
second condition is that the path lies inside a skill *directory* rather than beside the tree, and
the third is that lines were requested at all, since `read_file(limit=0)` returns empty content
with no error and would otherwise book a load of zero bytes.

This is recorded rather than quietly fixed because the first two were asserted in a docstring, a
metric HELP string and this section while the shipped tree falsified them — which is
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` happening inside the commit that wrote
it down, for the second time in this repository.

**2. `AgentProfile.skill_names` is a fourth narrowing**, composed by `skill_permits` alongside the
other three and applied by the same `_Narrowing` short-circuit. It only ever removes, so it changes
no invariant: a profile remains an attenuation. `None` narrows nothing, which is every shipped
profile, so this is a no-op on the running system.

**`None` and the empty set are deliberately different here, and they are not in `EnabledSkills`.**
There, an empty list is the unset default because a deployment naming no skill means "all of them".
A profile is the other way round: `skill_names: []` is an author writing down that this agent
reaches no skill, which is a real configuration — the control arm below *is* it — and is
unrepresentable if empty silently means everything. `ProfileScopedSkills._narrows` is written around
keeping the two distinguishable.

**3. `data/evals/profiles/skills-removed.yaml` is the arm that varies only the skills.** It declares
`skill_names: []` and nothing else: the tool surface is `default`'s entire set, the prose is the
deployment's own, and `instructions_for`'s `PromptBlock` gating is untouched because that gating is
a function of the tool surface. It sits in `data/evals/profiles/` rather than `data/profiles/` for
the reason the two controls beside it do — a measurement instrument is not a capability a session
may pick by name.

`make skill-validate` gains the third configured map it now has to check. A profile's `skill_names`
fails the same quiet way the other two do: `ProfileScopedSkills` narrows rather than raising,
because a turn must not break over a typo in a file a deployment dropped in, so the loud failure
belongs in the gate. The check calls `load_profiles()` itself — `validate_skills` is a CLI process
where nothing has registered a profile, so a version that skipped it would hold `default` alone,
declare no `skill_names`, and be green against every tree forever.

## Consequences

**The promotion threshold gains one of its two halves, and only one.** The shape every design for
this reaches for is D-161's — used N times **and** by at least two distinct chemists. A counter
labelled `{skill}` answers the first; it cannot answer the second, and must not, because an actor
label on the exposition is forbidden here. The `skill.read` INFO line does carry actor and session
through `core/logging.py`'s context filter, so the distinct-chemist half remains a log question
until something persists it. Said plainly because the draft of this ADR claimed the threshold "now
has a numerator" full stop, which reads as both halves arriving.

The half that did arrive is a live-deployment number: the series moves only on a real read by a
real turn, so on a database that has never served a user it reads zero — the same honest zero
`make trajectory-census` reports, and for the same reason. No engineering moves it.

**What the skills arm measures is the skills' *content*, not the existence of a skills mechanism**,
and the residual is stated rather than hidden: with no skill permitted, `SkillsMiddleware` still
publishes its listing prose, so the arm carries this repository's empty-listing override (upstream's
own default invites the model to create skills in a tree that refuses every write). That is a small
fixed prose difference in the arm, and it is the honest limit of what a delta against it can claim.

**The two skill counters do not sum to the asks.** A read that passes the gate, and then fails on a
missing file, or names a document beside the tree, or asks for no lines, is in neither. Said here
because a reader deriving "asks" from the pair would be deriving a number that does not exist.

**And the skills arm's residual is larger than a listing.** The deployment's own prose still orders
the model to load skills the arm cannot reach — those sentences are `PromptBlock`s gated on
*tools*, and the arm keeps every tool — so the arm can spend turns on instructions it cannot
satisfy. A delta measured against it is a **lower bound** on what skills are worth rather than an
unbiased estimate, and a negative result from it needs `chemclaw_skill_reads_denied_total` read
alongside. The arm's own header carries this; it is repeated here because the first draft of both
named only the listing.

**This does not let any agent path write a skill.** `SkillsReadOnlyRefusal` is untouched, and a
fourth narrowing is still a narrowing — it removes, and nothing here adds a write verb, a local
tier or a proposal. Those are separate decisions with their own records.

## What keeps it true

- `tests/test_agent_observability_skills.py::test_a_skill_the_model_reads_is_counted_by_name`
- `tests/test_agent_observability_skills.py::test_a_refused_read_is_not_counted_as_a_load`
- `tests/test_agent_observability_skills.py::test_a_name_no_directory_backs_mints_no_series` — the
  clamp against an invented name, which is the case a resolved-path check already handled.
- `tests/test_agent_observability_skills.py::test_a_file_beside_the_tree_is_not_a_skill` and
  `::test_a_read_that_asks_for_no_lines_is_not_a_load` — the two it did not, asserted as properties
  rather than against the one filename that exposed them.
- `tests/test_agent_observability_skills.py::test_a_supporting_document_inside_a_skill_counts_for_that_skill`
  — the negative arm, since narrowing to `SKILL.md` alone would lose real reads.
- `tests/test_validate_skills.py::test_a_malformed_profile_is_reported_rather_than_raised`
- `tests/test_validate_skills.py::test_an_unknown_skill_name_in_a_profile_is_reported`
- `tests/test_validate_skills.py::test_a_profile_file_on_disk_is_read_rather_than_assumed_registered`
  — the arm that distinguishes "looked and found nothing" from "did not look".
- `tests/test_tool_utility.py::test_the_skills_arm_keeps_every_tool_and_reaches_no_skill` — both
  halves, since neither implies the other.
- `tests/test_tool_utility.py::test_the_skills_arm_is_not_in_the_shipped_profile_set`
