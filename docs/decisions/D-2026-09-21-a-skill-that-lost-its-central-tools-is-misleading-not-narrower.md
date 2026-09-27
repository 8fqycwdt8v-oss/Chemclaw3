# D-2026-09-21-a-skill-that-lost-its-central-tools-is-misleading-not-narrower — `requires:`, the subset a skill cannot be read without

**Status:** accepted · **Date:** 2026-09-21 · Supersedes nothing. Narrows the visibility rule
`D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches` established, and pays back part of the
prefix `D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions` obliged every
deployment to carry.

## Context

`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions` declared five
process-development bundles with `default_enabled: false`, so a deployment that wants the
capability names it and pays ~22,000 tokens of prefix, and one that does not pays nothing. That
worked for the *tools*. It did not work for the judgment written about them.

Six cross-capability skills shipped into `skills/` rather than into a bundle, and the reason is in
`tests/test_context_floor.py`'s own entry: four of the six span two or more bundles
(`crystallisation-design` reads `unitops` and `props`; `scale-up-readiness-review` reads
everything), and a bundled skill belongs to one capability. Splitting judgment across bundles to
save prefix would put one skill in two places, which is the duplication `connectors/README.md`'s
ownership rule exists to prevent. So the six are global, and that entry raised the ceiling to
72,000 to pay for them, stating the cost plainly: every deployment pays on every model call.

`ToolScopedSkills` was supposed to be the thing that made this safe. It hides a skill whose
declared tools are absent from the agent's surface — and it hides it only when **every** declared
tool is absent. That rule is right and is pinned by a test, because the alternative was measured:
hiding on *any* absent tool takes 20 of 28 skills off the shipped `property-lookup` profile,
including `calculation-selection`, which that profile's own instructions tell the model to load.

**The six are the case that rule does not catch.** Each names a handful of default-enabled tools
alongside the bundle tools it is really about, so each survives the all-absent test, and three of
the six survive it having lost the tools they are centrally about. Measured on the default surface:
`solvent-swap-and-distillation` declares twelve tools, six of which are the `props` chain plus
`shortcut_distillation`, so a default deployment can execute **one step of its five-step answer**
and is offered the whole thing. `crystallisation-design` and `analytical-readiness` are the same
shape.

That is not a narrower skill. A skill listed to a turn that cannot take the path it describes is
worse than a missing one, because the model spends the load deciding to follow a procedure whose
middle it will then have to improvise — and the deployment pays for the listing on every model
call to get it.

## Decision

**A manifest gains one optional key**, the subset of `tools` without which the skill is misleading
rather than merely narrower:

```yaml
requires:
  - solvent_swap_candidates
  - shortcut_distillation
```

`ToolScopedSkills` consults it before the existing rule, with the opposite quantifier: a skill is
hidden unless **every** required tool is available, then kept if **any** declared tool is. The two
quantifiers are why this is a second key rather than a reading of the first — `tools` answers "is
there anything here this agent can do", `requires` answers "is the thing this is about still
here", and no single list answers both.

Three skills carry it today:

| Skill | `requires:` | The bundle that has to be on |
| --- | --- | --- |
| `crystallisation-design` | `crystallisation_yield` | `unitops` |
| `solvent-swap-and-distillation` | `solvent_swap_candidates`, `shortcut_distillation` | `props`, `unitops` |
| `analytical-readiness` | `system_suitability_report` | `suitability` |

The other three keep none, and that is a judgment rather than an omission: `impurity-fate-and-purge`,
`scale-up-readiness-review` and `robustness-and-edge-of-failure` are process guidance whose
argument survives without any one tool. **Empty is the expected answer for almost every skill**,
which is what keeps this from becoming a second enable-list.

### Two things hold it, because the failure directions are not symmetric

- **`make skill-validate` refuses a `requires` entry outside `tools`.** A `tools` typo is caught
  twice already — against the live surface, and by the taught-⇒-declared rule if the body names it
  — and its run-time effect is to leave the skill *visible*. A `requires` typo would be caught by
  neither, and its run-time effect is the opposite: a name no tool has is absent everywhere, so the
  skill disappears from every deployment with nothing reporting it. The check that fails loudly is
  therefore the one the unchecked key needed.
- **An unreadable manifest sentinels both maps.** `_declared_pair` already answered
  `UNREADABLE_DECLARATION` for `tools`, because an empty set there means "declares nothing" and
  leaves the skill visible. Under an all-of rule an empty `requires` is the same fail-open answer,
  so the sentinel goes in both slots and `tests/test_skill_manifest.py` compares the triple whole
  rather than its first two elements.

## Consequences

**243 tokens come back on every model call, in every deployment that leaves the bundles off.**
Measured through `tests/test_context_floor.py`: the `skills-listing` contributor goes 3,785 →
3,542, total 72,398, and `CEILINGS["__default__"]` drops 73,100 → 72,850 — the first entry in that
file to move downward. `tests/test_compaction.py`'s two thread allowances rise by the same 250.

**The ceiling drops by 250 rather than by this branch's 2,500, and the difference is the point.**
The other raises on this branch bought capability every deployment can use; this part bought
capability most of them cannot, and only that part is refundable. A deployment that enables the
bundles sees no change at all — the skills come back the moment their tools do, with no second
switch to set.

**The skills stay global.** Nothing here reverses the ownership argument that put them in
`skills/`; `requires` makes a global skill behave like a bundled one on the deployments that do not
bind the bundle, without putting one skill's judgment in two places.

**The two stored tiers gain a key nothing reads, which is the state `tools:` was already in.**
`agent/local_skills.py` says plainly that `ToolScopedSkills` is *not* applied to a stored skill —
its frontmatter lives in a store rather than on a directory, and `declared_tools` walks
directories — and `docs/planning/BACKLOG.md` carries the row. `requires` inherits exactly that gap
and does not widen it: `SkillManifest` will accept the key on a saved skill, and the save route
checks neither list against the surface today. Whoever closes that row closes both halves at once,
which is one reason to keep the two maps coming off a single read.

**Two callers must now pass the same basis.** `skill_permits` defaults `required` to *no
requirements*, so a caller that omits it does not take a different branch — it takes a **weaker
predicate**. `agent/langgraph_agent.py`'s `skill_narrowing` passes it, and the parity test in
`tests/test_langgraph_agent.py` that compares the backend against the shared predicate had to pass
it too or report its own omission as a disagreement about the corpus.

## Alternatives rejected

- **Make the `tools` rule all-of.** One key, no new field, and it hides 20 of 28 skills on a
  shipped profile — including one that profile's instructions name. The measurement that pinned
  the any-of rule is the reason this key exists at all.
- **Move the six into bundles.** Four of them span two or more bundles, so this is the duplication
  `connectors/README.md` forbids, and it was already declined once in `test_context_floor.py`.
- **A `connectors:` key naming the bundle instead of the tools.** Coarser and wrong in the same way
  `SkillManifest.tools`' own comment gives for not having one: the thing that breaks a skill is
  the tool being absent, not the bundle being renamed, and a bundle-level key would pass while the
  tool it teaches was gone.
- **Trim the six descriptions further.** Already done twice, 830 → 594, and it stopped where it
  should: a description is the only thing deciding whether the model loads a skill, so trimming
  past the trigger phrases buys prefix by making the judgment unfindable.
- **Leave it and take the 243 tokens.** The prefix is the smaller half. The larger one is a turn
  being handed a five-step procedure it can execute one step of, which no amount of headroom fixes.

## Revisit when

More than a handful of skills carry `requires:` — say a quarter of the corpus. At that point the
key has stopped being the exception it is designed to be and is doing work bundling should do, and
the ownership argument that kept these six global deserves re-reading against a corpus that has
grown since. The file that would show it is `src/chemclaw/agent/skill_manifest.py`'s own readers:
`required_tools` returning a mostly non-empty map is the signal, and the count is one `grep -l
'^requires:' skills/*/SKILL.md` away.

Also revisit if a deployment enables all five bundles and reports the opposite problem — a skill
hidden that it wanted — which would mean a `requires` entry names a tool the skill is not actually
centrally about. That is a per-skill correction, not a reversal of the rule.
