# Task: the tool/skill seam — a skill that outlives the tools it teaches

Branch: `claude/tool-skill-integration-refactor-qmjbe3`. Decision:
`docs/decisions/D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches.md`.

**The question was whether the two halves of layer 3 — the judgment (`SKILL.md`) and the
capability it is written about (tools) — are actually joined. They are joined at *build* time
(`make skill-validate` refuses a skill declaring a tool nothing provides) and not at *run* time:
nothing narrows the advertised skill set by what the turn can actually call.** Measured against
the one shipped narrowing profile, `property-lookup` (5 callable tools): **8 of 28 skills are
advertised whose entire declared capability is unreachable** — `experiment-design` teaching
`suggest_next_experiment`, `qm-job-submission` teaching `compute_dft_energy`, `reaction-search`
teaching all three fingerprint tools, and five more. The profile compensates in prose ("if a
question needs experimental history, say that it is outside this mode"), which is exactly the shape
this repository fixes with structure.

_(The previous occupant of this file was the live-test lane for Temporal + durable workflows
(#124/#127); it is in `git log` and its outcome is in `docs/decisions/`.)_

---

## Plan

### 1. Make the `tools:` declaration trustworthy (prerequisite for 2)

- [x] `make skill-validate` gains the missing direction: a tool a skill's **body** names in a
      checkable form must be **declared** in its frontmatter. Today only the reverse is checked
      (declared ⇒ exists), so `tools:` can be silently incomplete — and an incomplete declaration
      is what would make step 2 hide the wrong skill.
- [x] Fix the one violator (`deep-research`, 4 undeclared) and fill in the declarations of the
      three other skills that clearly teach tools (`knowledge-graph-query`,
      `knowledge-graph-write`, `safety-screening`). Skills that name no tool stay undeclared —
      pure process guidance depends on nothing.
- [x] Fix the dangling reference the gate cannot see: `experiment-design` names
      `` `find_similar_reactions` ``, an in-process function the agent cannot call. The tool is
      `similar_reactions`.

### 2. Scope skills by the capability the turn actually has

- [x] `ToolScopedSkillsSource`: hide a skill whose declared tools are **all** absent from this
      agent's advertised surface. Conservative on purpose — one surviving tool keeps the skill,
      and an undeclared skill is always visible; over-hiding judgment is worse than under-hiding.
- [x] `advertised_tool_names(profile)` — both halves of the surface under one profile, computed
      from manifests rather than by building connector tools (which would open httpx clients).
      Pinned by a test against what `_capability_tools` + `connector_tools` actually yield, so the
      two narrowings cannot drift.

### 3. Refactor the three skill sources onto one narrowing base (Rule of Three)

- [x] `_NarrowingSkillsSource`: await the inner source, short-circuit when unconfigured, filter by
      a subclass predicate. Three copies of that loop become one.

### 4. Close the gate that fails open

- [x] A typo'd key in `skill_role_gates` silently un-gates the skill (`_permitted` reads "absent
      from the map" as "ungated"), and nothing validated it — `skills_enabled` is validated, its
      security-relevant twin was not. `make skill-validate` should check both maps.

### 5. Delete the parallel tool surface

- [x] `agent/search_tools.py` is an unregistered copy of the `molfp`/`rxnfp` connector tools
      carrying an explicit hand-sync obligation ("Keep the two in sync") that has **already
      failed**: no `threshold` argument, and a different result model. Two callers, one of them a
      test that proves nothing about the production path.
- [x] Its three assertions move to `test_molfp_server.py` / `test_rxnfp_server.py`, over the MCP
      tools a turn really calls, so the deletion *raises* production-path coverage.
- [x] `examples/research_demo.py` reaches the fingerprint capability the way it already reaches
      the calculators: through the connector's own server module.

### 6. Record and verify

- [x] ADR, `skills/README.md`, `agent/README.md`, `ARCHITECTURE.md` row if the tree changes.
- [x] `make lint type test` green; `skill-validate`, `connector-validate`, `prose-validate` green.

---

## Review

Done and verified: `make lint type test` green (one unrelated flake, below), and
`skill-validate` / `connector-validate` / `prose-validate` all pass. The `property-lookup` profile
now advertises 17 skills instead of 28; the default profile still advertises all 28, asserted by
`test_no_shipped_skill_is_orphaned_on_the_full_surface`.

Four things worth keeping:

- **The measurement, not the reasoning, settled the design.** "Hide a skill when *every* declared
  tool is gone" and "when *any* is" both sound right in prose. The first hides 8 skills under
  `property-lookup`; the second hides 20, including `deep-research` and `calculation-selection` —
  the two that profile's own instructions tell the model to load. The conservative rule was not a
  preference, it was the one that did not break the shipped profile. Both sides of the boundary are
  now pinned, because the rejected reading is what a future change drifts into.
- **One widening was measured and rejected.** The prose gate cannot see a backticked tool name with
  no call parentheses (`` `predict_pka` ``), which is how skills usually write one. Widening rule 2
  produces 60 candidates over the corpus, ~46 of them result-field names (`created_by`,
  `yield_percent`, `index_empty`) — a rule wrong more often than the prose. The asymmetry that
  explains it is worth remembering: *checking that a known name is present is safe with a loose
  pattern; checking that an unknown name is absent needs a strict one.* The one real defect it was
  hiding (`find_similar_reactions`) is fixed by hand, and deleting `search_tools.py` removed the
  module that made the name plausible.
- **The drift guard was mutation-checked.** `advertised_tool_names` re-applies the narrowing rules
  `connector_tools` applies, which is exactly how two implementations of one rule diverge — and the
  divergence would be silent, since the skill surface would simply be scoped against a slightly
  wrong tool set. Dropping the `tool_names` narrowing from one side fails the test, so it is a
  guard rather than a description. (That check also cost an hour: `git checkout` on a file with
  uncommitted work is a delete, not an undo. Recorded in `tasks/lessons.md`.)
- **Nothing widens.** All three skill sources only remove; `authorize_tool`, the audit middleware
  and the profile narrowing are untouched and still run afterwards. A `tools:` declaration can cost
  a skill its visibility and can never grant a tool.

**The one failure, and why it is not ours.** `tests/test_bo_predict.py::test_the_suggestion_wires_
the_assay_noise_through_to_the_front` hit its own `@pytest.mark.timeout(60)` in the full run. It
passes alone (14.6s) and passes with its whole file (38/38, 103s). It fits a GP and runs a
multi-start acquisition, and its own docstring records the variance as the known risk ("a sibling
was measured spiking from 4.3s to 39.9s"). Nothing in this change is on the BO path — the only
`connectors/registry.py` edit is extracting `endpoint_tool_names`, which no suggestion calls.
