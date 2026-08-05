# D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches — A skill that outlives the tools it teaches

**Status:** accepted · **Date:** 2026-08-05 · **Builds on:** D-117 (one definition of the tool
surface), D-118 (capability lives in a connector bundle), D-112 (profiles are files) ·
**Supersedes:** D-029 (b), which kept `agents/search_tools.py` as an in-process seam

## Context

Layer 3 is judgment *about* layer 2 capability. The two were joined at **build** time and not at
**run** time: `make skill-validate` refuses a `SKILL.md` declaring a tool nothing provides, and
nothing anywhere narrowed the advertised *skill* set by what a given agent can actually call.

Meanwhile the tool surface acquired three narrowings — `connectors_enabled`, and a profile's
`tool_names` and `mcp_server_names` (D-112) — and the skill surface acquired none of them. Measured
against the one shipped narrowing profile, `property-lookup`, whose five callable tools are
`compute_xtb_energy`, `predict_pka`, `predict_solubility`, `calculator_trust` and
`ask_clarifying_question`:

> **8 of 28 advertised skills had no reachable tool at all** — `experiment-design` teaching
> `suggest_next_experiment`, `qm-job-submission` teaching `compute_dft_energy`, `reaction-search`
> teaching all three fingerprint tools, plus `reaction-thermodynamics`, `reactivity-descriptors`,
> `atropisomer-assessment`, `bond-strength-and-radicals`, `computed-spectra-comparison`.

The profile compensated in prose — "If a question needs experimental history, past reactions or a
literature search, say that it is outside this mode" — which is a sentence asking the model not to
believe the surface it was handed. This repository has twice decided that the fix for prose
promising capability the agent lacks is a gate rather than a longer instruction
(`chemclaw.cli.validate_prose_contract`, D-117). This is the same defect arriving through the skill
instead of through the prompt.

Four further defects in the same seam surfaced while measuring, and they are of a piece: each is a
place where the tool half and the skill half were joined by a convention nobody could check.

## Decision

**Capability becomes the second of three skill narrowings, and the `tools:` declaration becomes the
thing it reads.**

### 1. `ToolScopedSkillsSource` — hide a skill whose whole declared capability is gone

A third `SkillsSource` decorator, between deployment enablement and role scoping. A skill is
dropped when **every** tool it declares is absent from the agent's advertised surface; one
surviving tool keeps it, and a skill declaring nothing is always visible.

**The threshold was measured, not reasoned.** "Hide when *any* declared tool is missing" and "hide
when *all* are" are equally defensible in prose. The first takes 20 of 28 skills off
`property-lookup`, including `calculation-selection` — the skill that profile's own instructions
tell the model to load — because a skill routinely names one tool outside a narrow agent's surface
while remaining wholly useful for the rest. The second takes 8, and every one is genuinely
orphaned. The conservative rule is not a preference; it is the one that leaves the shipped profile
working. `tests/test_skill_access.py` pins the boundary from both sides, because the rejected
reading is the one a future change drifts into.

Undeclared skills stay visible because an empty list honestly means "depends on nothing" — which is
true of `development-report`, `playbook-distillation` and three others that name no tool at all.

**Nothing here widens anything.** All three sources only remove; `authorize_tool`, the audit
middleware and the profile's own narrowing are untouched and still run afterwards. A declaration
still grants no access — it can only cost a skill its place on the list.

### 2. The declaration is validated in both directions

Making the list load-bearing required making it trustworthy. `make skill-validate` gains the
missing half: **a tool a skill's body names must be declared**. Without it, an incomplete `tools:`
is indistinguishable from an honest one — and an under-declared skill would be hidden from
precisely the agent that can run what it teaches, which is the fix failing rather than the defect.

The body is read with `validate_prose_contract.referenced_tool_names`, the extractor the prose gate
already uses, so the two cannot disagree about what a skill says. One skill violated it
(`deep-research`, four tools); three others that clearly teach tools declared none and now do
(`knowledge-graph-query`, `knowledge-graph-write`, `safety-screening`).

### 3. `skill_role_gates` is validated, because it fails open

`RoleScopedSkillsSource` reads "absent from the map" as "ungated", so a typo'd key applies no
restriction at all and nothing at run time can report it. Its twin `skills_enabled` fails the other
way — the skill vanishes and someone notices — and only that one was validated. Both are now.

This is not a privilege escalation: the tools a skill teaches remain gated by `authorize_tool`, and
skill visibility has never been an access boundary on its own. It is a control an operator believes
they configured and did not.

### 4. `agent/search_tools.py` is deleted

D-029 (b) kept it as the credential-free, subprocess-free in-process seam for examples and tests,
with the standing instruction "Keep the two in sync if the search surface changes." **They had
already diverged**: no `threshold` argument, and a different result model (`ReactionHit.
reaction_note_id` against the connector's `Match.id`). It had two callers — one example, and a test
that exercised only the wrapper, so it could not fail for any defect in the surface a turn actually
uses.

Its three assertions moved onto `tests/test_molfp_server.py` and `tests/test_rxnfp_server.py`, over
the MCP tools a turn really calls, so the deletion raises production-path coverage. The example now
reaches the fingerprint capability through `connectors.rxnfp.server.tools`, exactly as it already
reached the calculators through `connectors.calc.server.tools`, and produces identical output.

### 5. One dangling reference fixed by hand

`experiment-design/SKILL.md` told the model to gather history with `` `find_similar_reactions` `` —
an in-process function name, never an agent tool. The tool is `similar_reactions`.

## What was measured and rejected

**Widening the prose gate to see backticked tool names.** `_BARE` deliberately skips a backticked
span, and `_CALL` requires a following `(`, so `` `predict_pka` `` — the form skills usually use —
is seen by neither. That is how finding 5 survived. Widening the rule produces **60** candidates
over the corpus, of which roughly 46 are result-field names (`created_by`, `yield_percent`,
`index_empty`, `valid_from`): a rule wrong more often than the prose it checks, needing a
hand-maintained exclusion list as long as the corpus.

The asymmetry is the reason, and it is worth stating because it recurs: **checking that a known
name is present is safe with a loose pattern; checking that an unknown name is absent needs a
strict one.** The new declaration rule is the first kind and can afford to be precise; a widened
rule 2 is the second kind and cannot. So finding 5 is fixed by hand, and finding 4 removes the
module that made that name plausible in the first place.

## Consequences

- A profile can now express "a property-lookup agent" in full: five tools **and** the seventeen
  skills that are about them, instead of five tools and every skill in the tree.
- The default profile is unchanged — all 28 skills, verified by
  `test_no_shipped_skill_is_orphaned_on_the_full_surface`, which doubles as the drift guard for a
  capability that leaves the system without its skill following.
- `advertised_tool_names(profile)` joins `available_tool_names()` as the per-agent answer to "what
  can be called". It reads manifests rather than building connector tools, because constructing one
  opens an `httpx.AsyncClient` that only a turn's exit stack closes — and it re-applies the
  narrowing rules `connector_tools` applies, so `tests/test_profile_discovery.py` compares the two
  against each other. That test was mutation-checked: dropping the `tool_names` narrowing from one
  side fails it.
- Three near-identical `SkillsSource` decorators became one `_NarrowingSkillsSource` plus three
  predicates (Rule of Three).
- Structural fingerprint search now has exactly one implementation, reached exactly one way.
