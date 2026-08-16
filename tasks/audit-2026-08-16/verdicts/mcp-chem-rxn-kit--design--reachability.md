# Reachability verdicts — `mcp-chem-rxn-kit--design.md`

Scope: findings marked **critical** or **high** only. The file contains exactly one such finding;
the other nine are medium/low and were not reviewed.

---

## `manifests/` is the discovery directory *and* contains the one bundle that must not be discovered

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. Reproduced the shadow with the caller's own registry, using the README's own recipe ordering:

     ```
     $ CHEMCLAW_CONNECTORS_DIR="/workspace/chemclaw3-mcp/manifests:$CORE/src/chemclaw/connectors" \
         uv run python /tmp/probe_shadow.py
     bo           -> /home/user/Chemclaw3/src/chemclaw/connectors/bo
     calc         -> /workspace/chemclaw3-mcp/manifests/calc        <-- shadowed
     chem         -> /workspace/chemclaw3-mcp/manifests/chem
     molfp        -> /home/user/Chemclaw3/src/chemclaw/connectors/molfp
     props        -> /workspace/chemclaw3-mcp/manifests/props
     qm           -> /home/user/Chemclaw3/src/chemclaw/connectors/qm
     rxnfp        -> /home/user/Chemclaw3/src/chemclaw/connectors/rxnfp
     rxnpredict   -> /workspace/chemclaw3-mcp/manifests/rxnpredict
     safety       -> /workspace/chemclaw3-mcp/manifests/safety

     calc dir: /workspace/chemclaw3-mcp/manifests/calc
     jobs: []
     skills: []
     ```

     `discovered()` returned normally. No exception, no `logging.WARNING` (root logger set to
     WARNING for the run), no second entry for the losing bundle.

  2. Diffed the agent's actual resolved surface, base vs shadow, through
     `chemclaw.agent.chemclaw_agent.available_tool_names()`:

     ```
     MODE: BASE                          MODE: SHADOW
     total tools: 66                     total tools: 75
       compute_reaction_energy   present   compute_reaction_energy   GONE
       compare_solvents          present   compare_solvents          present (see below)
       scan_coordinate           present   scan_coordinate           GONE
       sample_conformers         present   sample_conformers         GONE
       compute_interaction_energy present  compute_interaction_energy GONE
       compute_thermochemistry   present   compute_thermochemistry   GONE
       calculator_trust          present   calculator_trust          GONE
       calculator_outliers       present   calculator_outliers       GONE
       report_measurement        present   report_measurement        GONE
       find_calculations         present   find_calculations         GONE
       list_artifacts            present   list_artifacts            GONE
       fetch_artifact            present   fetch_artifact            GONE
     ```

  3. Confirmed the enforcing test really does pin the hazardous state — moved the directory away and
     ran it:

     ```
     $ mv manifests/calc /tmp/... && uv run pytest tests/test_fleet.py -k symlink -q
     FAILED tests/test_fleet.py::test_the_manifest_is_registered_by_symlink[calc]
     1 failed, 4 passed
     ```
     (restored; `git status --porcelain` clean.)

  4. Checked the two places that could have made it loud instead of silent:

     - the front door's `_lifespan` (`src/chemclaw/api/app.py:124-156`) calls `load_profiles()` and
       `check_connectors_at_startup()`. `load_profiles()` only parses and registers — under the
       shadow it succeeded. Nothing at startup compares a profile's `tool_names` to the built
       surface.
     - `make connector-validate` **does** catch it — but it is a dev/CI target, and under the
       README's *intended* configuration it already fails for unrelated reasons, so its output is
       not a usable signal here:
       ```
       - connector 'calc': tool 'calculator_trust' is served on /mcp but the manifest does not declare it
       ... (7 such lines)
       - connector 'props': its server module could not be imported (No module named 'chemclaw.connectors.props')
       - connector 'rxnpredict': its server module could not be imported (No module named 'chemclaw.connectors.rxnpredict')
       ```
       An operator who registers `props`/`rxnpredict` as the README tells them to gets errors for
       those two whatever they do, which is exactly the condition under which the seven `calc` lines
       get dismissed as noise.

- **Why**

  Every load-bearing claim held under execution. The mechanism is `registry._bundle_dirs()`
  (`found.setdefault(path.name, path)` over `settings.connectors_dirs` in order — first dir wins)
  plus `enabled()`'s "empty list means everything", and I watched both do it. `manifests/calc/` is a
  discoverable subdirectory holding a valid `connector.yaml` whose `name` matches its directory, so
  `_load_manifest`'s one consistency check (name vs folder) passes and there is no remaining gate.
  The five durable jobs vanish exactly as stated, from the default profile, with no error.

  On reachability, honestly: the trigger is an operator action, not a caller input, and the only
  *executable* wiring in the calling repo already avoids it —
  `infra/live/e2e-full-stack/up.sh:185` sets
  `CHEMCLAW_CONNECTORS_DIR="$own_connectors:$MCP_REPO/manifests:$HARNESS_DIR/manifests"`, core
  first. I also checked whether manifests-first is ever *necessary*, since the README justifies the
  ordering by the `chem`/`safety` override: it is not. Core's `chem` and `safety` manifests and the
  fleet's are functionally identical — same `name`, same `tools`, same `read_only`/`state_changing`
  split, same URL, same auth block (diffed; `IDENTICAL` for both) — so the override buys nothing and
  manifests-last is a complete wiring. That weakens the README's stated reason for the dangerous
  ordering but does not weaken the finding: the README's first code block is still the primary
  documented recipe, an operator reading only this repo will run it, and nothing in either codebase
  stops them.

  I keep **high** rather than dropping to medium because of what the silence costs on a chemistry
  answer, which the reporter understated in three ways I verified:

  1. **It is eleven tools and a skill, not five jobs.** `compute_thermochemistry`,
     `calculator_trust`, `calculator_outliers`, `report_measurement`, `find_calculations`,
     `list_artifacts` and `fetch_artifact` go too (measured above), and `calc`'s
     `skills: [calculation-selection]` becomes `skills: []`, so the guidance on which calculator
     fits a question and how far to trust it leaves with the tools. What a chemist is then shown:
     the agent still has `compute_xtb_energy`, so "is this reaction favourable" degrades from a
     balance-checked, RRHO-corrected, uncertainty-quoting durable job to single-point energies the
     model subtracts itself — and `calculator_trust`, the only reader of the calibration ledger and
     the thing the `computation` profile's own instructions require before presenting a number as
     settled, is not there to qualify it.
  2. **`compare_solvents` does not disappear — it silently rebinds to a different tool.** It is the
     one name in the list that survives, because `servers/props` serves a tool of the same name
     (`manifests/props/connector.yaml:40`). Core's is a durable Temporal job that ranks solvents by
     computed ΔG and is classified `state_changing`; props' is
     `compare_solvents(names: list[str]) -> ComparisonResult`, a read of a static property table
     (boiling point, flash point, ICH class), classified `read_only`. So the plan gate's
     classification flips from state-changing to read-only for that name, and the agent's system
     prompt (`src/chemclaw/agent/chemclaw_agent.py:77-78`) still describes `compare_solvents` as a
     bigger calculation that may return a job id.
  3. **The name collision exists even in the "correct" wiring**, so the reporter's fix does not fully
     close it. With `calc` deliberately excluded and only `chem`/`props`/`rxnpredict`/`safety`
     registered, I enumerated every enabled bundle's endpoint tools and job names:
     ```
     calc from: /home/user/Chemclaw3/src/chemclaw/connectors/calc
     DUPLICATE NAMES across enabled bundles: {'compare_solvents': ['calc:job', 'props:tool']}
     ```
     That is a separate defect and out of this finding's scope, but whoever implements the fix should
     know the directory split alone leaves it standing.

  The one thing that partially rescues the "no error" claim is the profile path: a request naming the
  `computation` profile fails loudly rather than silently, at agent-build time —
  ```
  computation profile FAILED: ValueError agent profile 'computation' lists unknown tool(s)
  ['calculator_outliers', 'calculator_trust', 'compute_interaction_energy',
   'compute_reaction_energy', 'compute_thermochemistry', 'fetch_artifact', 'find_calculations',
   'list_artifacts', 'report_measurement', 'sample_conformers', 'scan_coordinate']; kn…
  ```
  — which is a turn-time 500, not a startup failure, and does not fire at all for the default
  profile. The finding's wording ("no error, no warning and no failed startup") is exactly right for
  the default profile and slightly too strong for a deployment that pins `computation`. That is not
  enough to move the verdict.
