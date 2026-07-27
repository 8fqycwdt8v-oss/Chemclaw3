# Task: the generic connector seam — one way to add a tool, skill or agentic workflow

Requested 2026-07-26. Design + staging in `docs/connector-plan.md` (deep status-quo analysis,
eight interview decisions, two verified MAF API findings). This file is the working queue.

Branch: `claude/generic-connector-tools-workflows-uz8afs`.

## Decisions driving the work

1. Capability tools move out to connectors; the 11 conversation-plumbing tools stay in core by rule.
2. Agentic workflows: declarative `AgentProfile` bundles now; deterministic step templates specified
   and gated (Stage E).
3. Durable jobs: generic `ConnectorJobWorkflow` in core (idempotency, actor, push-back, PR-gate) over
   a connector-owned workflow addressed by **type-name string**.
4. A connector is an in-tree bundle folder + one config enable-token.
5. One FastAPI app per domain; one composite dev process.
6. `X-Chemclaw-*` header contract (advisory only) + per-connector auth union.
7. Unreachable connector ⇒ degrade loudly; `connectors_required` ⇒ fail fast.
8. `mcp_servers` is **removed**, not deprecated.

## Stage A — the seam (core) — DONE

- [x] `connectors/manifest.py` — `ConnectorManifest`, `EndpointSpec` (stdio|http), `ConnectorAuth`
      (none|bearer), `JobSpec`, `JobParam`; all `extra="forbid"`
- [x] `connectors/identity.py` — header provider (reads the ambient ContextVars per call) +
      `httpx.Auth` per auth mode
- [x] `connectors/jobs.py` — generated durable tool factory (params model via `create_model`,
      docstring from the manifest, registered through the existing `register_tool`)
- [x] `workflows/connector_job.py` — `ConnectorJobWorkflow` + `ConnectorJobInput`/`ConnectorJobResult`
- [x] `connectors/registry.py` — discover → validate → enable → build (MCP tools + job tools)
- [x] `connectors/health.py` — bounded startup probe
- [x] `chemclaw/config.py` — `ConnectorSettings`; delete `mcp_servers` + the three MCP spec models
- [x] `agents/chemclaw_agent.py` — assemble from the connector registry; delete `_mcp_tool`
- [x] worker registration for `ConnectorJobWorkflow`
- [x] `/readyz` detail + `chemclaw_connectors_unhealthy` gauge; `connectors_required` fail-fast
- [x] `scripts/validate_connectors.py` + `make connector-validate`; retarget `validate_skills` and
      `validate_prose_contract` off `settings.mcp_servers`
- [x] tests: manifest validation, registry enable/unknown, header provider, auth, generated job tool
      (audit+authz wrap it), `ConnectorJobWorkflow` against a real `WorkflowEnvironment`
- [x] `.env.example`, Helm values/templates, `docs/runbook.md`, `DECISIONS.md` ADRs

Gate A: `make lint type test` green; audit+authz demonstrably wrap a connector-sourced tool and a
generated job tool; unknown enabled connector fails loud; non-loopback `auth: none` refused.

## Stage B — reference bundles + the durable path proven — DONE

- [x] `connectors/molfp/`, `connectors/rxnfp/`: manifest + FastAPI app mounting
      `FastMCP.streamable_http_app()` at `/mcp` plus `/healthz`
- [x] `scripts/connectors_dev.py` + `make connectors` (its composite must run each mounted app's
      lifespan — Starlette does not, and a connector's lifespan is what starts its MCP session
      manager; caught by the transport test)
- [x] fixture connector (`tests/fixtures/connectors/fixture/`) with its own workflow: the durable
      contract proven end to end under a real `WorkflowEnvironment` (skipped offline, like every
      Temporal test here; the wrapper's worker registration and the envelope shape are pinned by
      sandbox-safe tests that always run)
- [~] The four bespoke adapters are **deliberately not migrated** — they wrap workflows returning
      typed domain results, not the envelope. Moves in Stage C with their code (D-109, plan §9).

Gate B met: fingerprint search reached over HTTP with the identity headers *observed by a live
server*, and two concurrent turns proven to keep their own identity.

## Stage C — domain connectors

- [x] **Safety rubric verified across the process boundary** before moving anything
      (`tests/test_connector_safety_rubric.py`): a connector tool call is audited with the turn's
      actor, and `tool_role_gates` denies it by name without the tool body ever running. Neither is
      inspectable from the wiring — MAF assembles MCP tools separately from the configured ones — so
      this had to be driven through a real agent against a real server.
- [x] `safety` — `screen_hazards`, with the `safety-screening` skill in the bundle
- [x] `chem` — `resolve_compound`, `stoichiometry_table`, `green_metrics`, `render_structure`
      (takes `rdkit` out of the front-door image)
- [x] `calc` — the calculators + the calibration ledger, with `calculation-selection` in the bundle
      (takes `tblite` and the calculation store's driver out)
- [x] Helm: an entry per bundle; Deployment/Service/NetworkPolicy already generalized in Stage A
- [~] `kg` — **won't build** (D-114). Thirteen core modules import `kg`, so moving the three read
      tools out leaves every one of those imports where it is: zero dependency win, plus a second
      read path to one note tree. Re-indexing stays in core with it. The rule is written into
      `connectors/manifest.py` and the runbook rather than left as folklore.
- [x] `bo` — the reference connector-owned durable capability (D-111): its workflow, activities and
      worker live in the bundle on `connector-bo`, `start_optimization_campaign` is a manifest
      `jobs:` entry, and the bespoke adapter is deleted. Core serves no BO workflow. The move needed
      one manifest entry plus the workflow's return type, and no core edit — the property the seam
      was built to have. `write_campaign_node` is gone: the note *mapping* stayed in the bundle, the
      *publish* moved to core, so a connector structurally cannot reach the PR-gate.
      Added `JobSpec.precondition` so the round-ceiling guard survived the migration (every other
      placement re-runs at replay against current config).
- [x] `calc`'s expensive half — the five orphaned hybrid tools became `jobs:` entries over
      `CalcJobWorkflow` on `connector-calc` with `inline_wait_seconds`, so one tool still serves the
      two-second and the twenty-minute case (D-113). This is what finally took `tblite`, RDKit,
      SciPy and the `xtb`/`crest` binaries out of the chat image.
- [~] `report` — **envelope, not a bundle** (D-114). Its closure is what core keeps for
      `gather_evidence` regardless, so isolation buys nothing; but returning `ConnectorJobResult`
      closed the hole where `get_durable_job_status` could report `completed` with nothing to hand
      back. Stays on core's worker.
- [~] `qm` — stays in core: it needs the HPC identity bridge, which is core's, not a capability's.

## Stage D — agentic workflow configuration — DONE

- [x] Profiles authored as files (`AgentProfile` Stage 3): `profiles/<name>.yaml` for a profile that
      spans capabilities, `connectors/<name>/profiles/` for one that belongs to a bundle. The stem is
      the name; a `name:` key is refused; `extra="forbid"` makes a typo'd override a startup error
      rather than a silent no-op.
- [x] `POST /sessions {profile}` + one cached agent per profile, with the profile fixed for the
      session's life and carried on the live-session record so the turn gets the matching agent
      *and* the matching connector set.
- [x] `profiles/property-lookup.yaml` — a real worked profile, not a placeholder.
- [~] Profile-name RBAC gate: deliberately not built. A profile can only attenuate, so gating the
      *name* protects nothing that `tool_role_gates` does not already protect at call time; it would
      be usability, and there is no caller asking.
- [~] A rehydrated session returns on the default profile (the owner row does not record it).
      Documented in the runbook; persisting it is a migration, and the degradation is to the *full*
      surface rather than a wrong one.

## Stage E — step templates — DONE

Built at the user's explicit request, ahead of its recorded trigger (see the review below).

- [x] `templates/manifest.py` — `Template` + three step kinds (`tool`/`job`/`agent`) on a
      discriminated union, with validators that refuse duplicate ids, unknown inputs and forward
      references at load rather than at run time.
- [x] `templates/resolve.py` — `${inputs.x}` / `${steps.id.result}` and nothing else. Pure, so it is
      safe inside the workflow; whole-string references preserve type, embedded ones interpolate JSON.
- [x] `templates/registry.py` — the same seam shape as connectors and profiles: discovered by folder,
      one config token to enable, a generated `run_<name>` tool that starts the run.
- [x] `workflows/template_job.py` + `workflows/template_activities.py` — the sequencer (replayable,
      with the resolved template pinned into the input) and the two activities that do the I/O.
      Identity is re-stamped per step and the audit + authz middleware applied by hand, because MAF
      applies it inside a tool-calling loop a template does not go through.
- [x] `templates/hazard-briefing.yaml` — a real worked template (screen → precedent → brief).
- [x] `make template-validate` — CI gate that a step's tool, job or profile actually exists.
- [x] `templates/README.md` + runbook §(iv-c): when to reach for a template rather than a profile.

## Stage C — completed by the merge with `main` (D-113)

- [x] `calc` is whole. The four calculators `main`'s X8 added, plus `predict_logd` and
      `predict_developability_profile`, moved into the bundle; the duplicate
      `mcp_servers/calc/server.py` is deleted (it and the bundle both defined `predict_pka`).
- [x] The five hybrid inline-or-defer tools (`compute_reaction_energy`, `compare_solvents`,
      `scan_coordinate`, `sample_conformers`, `compute_interaction_energy`) are `jobs:` entries on
      the bundle's own workflow, worker and queue. They were the last thing keeping the whole heavy
      chemistry closure in the chat service's image — and after the merge they were also
      **orphaned**, registered by nothing.
- [x] `JobSpec.inline_wait_seconds`: the launcher waits a bounded moment and returns the result or
      the job id. Replaces a predicted-cost threshold that could only live where the cost model
      lives, which is what had put chemistry in core.
- [x] `run_xtb_task` deleted (the five typed jobs cover its union); its role gate moved onto the two
      CREST searches as `expensive: true` rather than disappearing with it.
- [x] `get_durable_job_status` returns the result, not just a status word — the connector envelope
      made the follow-up answerable. `get_job_status` narrowed to HPC/DFT.
- [x] Adopted `main`'s `workflows/registry.py` for core's two workers; `ConnectorJobWorkflow` and
      `TemplateWorkflow` register on it (both were missing from a worker list — exactly the bug that
      registry exists to prevent, found by its own test).
- [x] Fixed a **pre-existing** bug the merge surfaced: `optimize_structure` tested convergence only
      *after* a leg, so re-optimizing a converged geometry moved it and changed its `structure_id`
      — forking the calculation cache for everything downstream. `tests/test_xtb_opt.py` was red on
      unmodified `main`; the initial gradient was already computed and discarded.
- [ ] `kg` — the last bundle. Still needs the re-indexing decision.
- [ ] The `report` job — moves once its workflow returns the envelope directly.

## Review — reconciliation with `main`, and the two open Stage C points closed

`make lint type test` green: **1172 passed, 57 skipped**, every validator passing
(`connector-`, `template-`, `skill-`, `prose-`). ADRs D-113 (the merge + the calculators) and
D-114 (the two open points).

**The merge was two convergent designs, not a conflict.** `main`'s X8 had independently moved seven
calculators out of the agent's process behind an MCP server, for the same reason the connector seam
exists — via `settings.mcp_servers`, the mechanism this branch removed. Resolution: `mcp_servers/calc`
deleted, its tools re-homed in `connectors/calc/`, taking `main`'s bodies wherever they were newer
(its `predict_pka` carries X11's base support; keeping the bundle's copy would have silently reverted
a real capability). ADR numbers collided too — mine renumbered D-092…D-095 → D-109…D-112.

**Adopted from `main` rather than merged around:** `workflows/registry.py` (D-099). A workflow
declaring its own queue at its definition site fixes exactly the failure my new workflows were
exposed to — written, tested, imported, absent from the worker's list, and therefore never run.

**The defect the merge produced, and the validator that caught it.** Five tools
(`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`,
`compute_interaction_energy`) ended up **orphaned**: nothing imported `agents/calc_tools.py`, so they
were dead code that eleven `SKILL.md` files still declared. `make skill-validate` found it; `make
test` on either branch alone would not have.

They had stayed in-process because each *predicted* its cost and submitted a job when expensive, and
submitting needs ambient identity. Sound reason, obsolete conclusion — the generated launcher already
is the in-process half that holds identity. `JobSpec.inline_wait_seconds` closes the gap: start the
run, wait a bounded moment, return the result or the job id. Better than the threshold it replaced
because elapsed time cannot be wrong about what already happened, and because a cost model can only
live where the chemistry lives — `exceeds_inline_budget` in the agent's process was what kept
`tblite`/RDKit/SciPy and the `xtb` binaries in the chat image, making the `calc` bundle decorative for
the expensive half of its own capability.

**Two open points, both answered by measuring:**

- **`kg`: won't build.** Thirteen core modules import `kg`, so a bundle moves three thin read tools
  and leaves every one of those imports — a zero dependency win plus a second read path to one tree.
  The rule is now written in `connectors/manifest.py` and the runbook so it is not re-litigated.
- **`report`: the envelope, not a bundle.** Its closure is what core keeps for `gather_evidence`
  anyway. But it returned a bare note-ref string, which made it the one job
  `get_durable_job_status` could report `completed` for with nothing to hand back — so it now returns
  `ConnectorJobResult` and stays on core's worker.

**Also fixed while here:** `get_durable_job_status` returns the result, not just a status word (the
hole making the calculators durable would otherwise have opened); `get_job_status` narrowed to the
HPC job it was always about, its dead `xtb-` branch gone; `run_xtb_task`'s role gate moved onto the
two CREST searches rather than disappearing with the tool.

## Review — Stages D and E

`make lint type test` green at **1005 passed, 45 skipped** (the skips are the unchanged offline set:
26 Postgres, 19 Temporal-server). ADR D-112 records the design; three things are worth flagging here.

**The gate found two omissions that would have shipped silently.** The image never `COPY`d
`templates/` or `profiles/` — both are discovered from disk, so the container would have started
perfectly and simply advertised less. `test_image_ships_every_first_party_package` caught the first;
it structurally cannot catch the second (`profiles/` has no `__init__.py`), which is the argument for
the explicit `COPY` and its comment. Separately, `connectors/` and `templates/` were both missing from
`make type`'s package list — type-checked transitively but never directly. Both now listed.

**Stage E shipped ahead of its trigger.** The plan gated it on "a second real use case a profile
provably cannot express"; the user asked for it built, which is their call. `hazard-briefing` is the
one worked case, so the risk the gate guarded — a step engine with a single caller — is open, not
retired. If no second template appears, this is the code to reconsider first.

**What was deliberately not built.** No conditionals, loops or expressions in the substitution
language: that is how a config format becomes a programming language with no debugger. A procedure
needing them wants an `agent` step or real code in a connector.

## Review — Stage C (in progress)

Three bundles migrated, `make lint type test` green at **977 passed**. Two defects found by the
existing suite while doing it, both real and both fixed at the root:

1. **Swallowing `CancelledError` in the connector transport broke the front door's turn timeout.**
   A hung turn ran to completion holding its admission permit — the exact failure
   `service_turn_timeout_seconds` exists to prevent. MAF swallows it in its own MCP paths on the
   grounds that an internal cancel scope is indistinguishable from a real one; at this layer it *is*
   distinguishable (`Task.cancelling()`), and the distinction is load-bearing. Caught by
   `test_stalled_turn_times_out_and_frees_the_permit`, which is why that test is worth its weight.
2. **`AgentProfile.tool_names` could no longer reach a migrated tool.** With the domain capabilities
   behind connectors, a dial that only narrowed the in-process half could not express "a
   property-lookup agent" at all. `tool_names` now spans both halves — narrowing in-process tools
   *and* each connector's allow-list, dropping connectors left with nothing — with one unknown-name
   check over the union, since only that has enough information to tell a typo from a name on the
   other side of the boundary.

Also: connector expectations in tests now derive from `discovered()` rather than hardcoded names, so
adding a bundle does not break unrelated tests.

## Review — Stages A and B

`make lint type test` green: **969 passed, 44 skipped** (the skips are the pre-existing offline set —
26 Postgres, the Temporal-server tests; `temporal.download` and GitHub releases are both blocked by
this sandbox's proxy, so the end-to-end durable test could not be executed here and is CI-gated like
its siblings). `make connector-validate`, `make skill-validate` and `make prose-validate` all pass.

Two things were found by measurement rather than reading, and both changed the design (D-109):

1. **MAF's `header_provider` silently delivers nothing over streamable HTTP.** It is invoked with the
   right values; the server receives no headers, because MAF's ContextVar is set in the calling task
   while the request is issued by the MCP transport's writer task. A request hook on our own httpx
   client works. The transport test now asserts the headers *arrive*, which is the only assertion that
   could have caught this — a unit test of the provider passes either way.
2. **Connectors cannot be process-lived.** Two concurrent turns sharing one connector tool object
   **deadlock**, and any request that did get through would carry the other turn's identity. This is a
   pre-existing hazard on the stdio path (`run_turn` has always entered process-lived tools per turn),
   surfaced by moving capability to HTTP. Fixed at the root: `connector_tools()` builds per turn and
   the caller passes them to `Agent.run(tools=…)`. `build_agent` no longer attaches connectors, so
   profile narrowing of connectors moved to where the set is built.

Cost of that second fix, stated: the front door now owns a `connector_factory` (symmetric with
`agent_factory`, and where per-profile selection attaches in Stage D), and ~18 fake agents in the
suite grew `**_run_options` so a fake cannot silently drift from the real call shape again.
- `deploy.yml` (image build, Helm gate, credentialed rollout) is restored *in tree*
  under `services/chemclaw/.github/` but not re-enabled at the repo root. Its rollout
  job pushes to a registry with secrets; turning that back on is not a call to make
  unprompted while reconciling a regression.
- `make eval`'s three pre-existing gated failures (`pharma-solvent-heavy`
  e_factor/pmi, `retrieval-cross-coupling-literal-miss` recall) are left failing and
  kept out of the root CI gate. They predate all of this — confirmed by stashing and
  re-running on the clean baseline — and deserve their own fix rather than a check
  that is red on arrival.
- `package.json` in the UI declares `check:openapi` -> `scripts/check-openapi.mjs`,
  and that file does not exist, so the script fails for anyone who runs it.

---

# xTB capability layer — X3 (geometries + thermochemistry) and X4 (the composite)

Proposal: `docs/xtb-tools-proposal.md` §12. Branch: `claude/xtb-chemclaw-tools-proposal-nujp14`.

Scope of *this* change: **X3** — `optimize_geometry`, `compute_thermochemistry`, `scan_coordinate`
— and **X4** — `compute_reaction_energy`, `compare_solvent_effects`. Together these are the phases
the skill catalogue says gate 19 of its 28 skills.

## Design decisions taken during planning (deviations from the proposal, with reasons)

1. **No `ase` dependency.** The proposal offered "`ase` (or a scipy L-BFGS over the tblite
   gradient)". Taking the second: `scipy` is already resident (via `scikit-learn`/`bofire`) and
   `scipy.optimize.minimize(method="L-BFGS-B", jac=True)` over tblite's *analytic* gradient is a
   dozen lines. ASE would buy an optimizer we get for free, plus a `Vibrations` class that caches
   displacements **to a directory on disk** — a side effect that does not belong inside a pure,
   content-addressed calculator. Its thermochemistry helper is the only real loss, and RRHO is
   ~80 lines of textbook physics we can pin against water's measured entropy. `scipy` is promoted
   from transitive to declared, because a first-party module now imports it.
2. **Spec *subclasses*, not one widening `XtbSpec`.** Thermochemistry has a temperature, a symmetry
   number and a pressure; optimization has a gradient tolerance and a step cap; a scan has its
   coordinate. Adding them all to `XtbSpec` would put a `temperature_k` in a *single point's* cache
   key. `OptSpec`/`ThermoSpec`/`ScanSpec` inherit `cache_key` unchanged — it derives from
   `model_dump()`, so a subclass field is keyed by construction exactly as a base field is.
3. **The optimized structure is a field of the cached result, not a new store.** X1 deferred a
   structure store until something produced a geometry; X3 does. But `OptimizationResult` carrying
   its `Structure` *is* persistence — the result store already holds it, content-addressed by the
   optimization's key. A second store with one writer would be the speculative abstraction.
4. **`compute_thermochemistry` also returns IR intensities.** The Hessian loop displaces every
   Cartesian and reads the gradient; tblite hands back the **dipole** at the same time, so dipole
   derivatives — and therefore a computed IR spectrum — cost nothing beyond an array we were
   already discarding. This is the same "read what the SCF already produced" move as X2, and it is
   what makes the catalogue's `computed-spectra-comparison` shippable.
5. **`level="thorough"` is not offered.** The proposal's third tier is a conformer ensemble, which
   is X6. A `Literal["quick", "standard"]` that refuses to name what it cannot do beats an option
   that raises.
6. ~~**A size guard instead of half of X5.**~~ **Reversed during the build.** The original plan
   was to refuse anything too slow for an inline turn, on the grounds that durable routing is
   explicitly X5. The measurements said otherwise — 4.6 s for a four-species reaction, ~25 s for a
   five-solvent screen, minutes for a long scan — and refusing work because it is slow is a worse
   answer than running it durably. The expensive tools now route by predicted cost
   (`calc/xtb_cost.py`) onto `XtbJobWorkflow`. The atom and point caps that remain are
   practicality limits, not latency ones.
7. **Relaxed scans freeze the atoms that define the coordinate.** RDKit's `rdMolTransforms` sets a
   bond/angle/dihedral by moving the whole attached fragment; freezing those atoms and relaxing
   everything else is then exactly a constrained minimization over the free subspace, expressed as
   equal L-BFGS-B bounds. The approximation (the frozen atoms' own local geometry cannot relax) is
   stated in the result and in the skill.

## Build

- [x] X3.1 `calc/xtb_engine.py`: `make_calculator` + `evaluate_point` (Angstrom in; Hartree,
      Hartree/Angstrom and the dipole out); friendly failure for an unknown ALPB solvent; the
      spin-polarization contribution for open shells, versioned into the cache key.
- [x] X3.5 Durable routing (unplanned, see decision 6): `calc/xtb_cost.py`, `XtbJobWorkflow` +
      activity, `agents/xtb_job_tools.py`, and `get_qm_job_status` generalized to `get_job_status`.
- [x] X3.2 `calc/xtb_opt.py`: `OptSpec`, `OptimizationResult`, `optimize_structure`,
      `run_cached_optimization`. Frozen-atom support (bounds), convergence on max |gradient|.
- [x] X3.3 `calc/xtb_thermo.py`: finite-difference Hessian + dipole derivatives, Eckart projection,
      harmonic frequencies, IR intensities, quasi-RRHO thermochemistry, `ThermochemistryResult`.
- [x] X3.4 `calc/xtb_scan.py`: `ScanSpec`, relaxed scan over a distance/angle/dihedral.
- [x] X4.1 `calc/reaction.py`: balance check, per-species pipeline, `compute_reaction_energy`.
- [x] X4.2 `calc/reaction.py`: `compare_solvent_effects` over the same reaction machinery.
- [x] X4.3 Agent tools + config + `.env.example`.
- [x] X3/X4 skills: the catalogue entries these unblock.
- [x] Docs: ADR, `BACKLOG.md`, catalogue status.

## Raised by the user mid-build, and done

- [x] **A structured way to register Temporal capabilities** (D-099). Adding `XtbJobWorkflow`
      meant editing a hardcoded list in a worker — the one extension seam left that forced an
      edit to infrastructure code, and a silent one (an unregistered workflow never runs and
      nothing fails until a job waits forever). `workflows/registry.py` now mirrors
      `agents.tool_registry`: `@durable_workflow("hpc")` / `@durable_activity("background")` at
      the definition site, workers read what they serve.
- [x] **Sized for the real workload: 200-800 Da, minutes not seconds** (D-100). The cost model
      was fitted on 3-14 atom test molecules and under-predicted a 76-atom substrate
      **sevenfold**. Refitted on measured drug-sized timings (exponent 1.7 -> 3.0; the 76-atom
      point now reproduces to 1%). Atom ceiling 120 -> 150, optimizer step cap 400 -> 1500, job
      budget 1 h -> 4 h, and the activity heartbeats between species/solvents/scan points so a
      dead worker is caught in minutes rather than at the timeout.
- [ ] **xTB as an MCP server** — answered, not built. Recorded as X8 in `BACKLOG.md` with the
      reason it is an either/or switch rather than an addition.

## Verification (planned before building)

- **Optimization**: ethanol's energy drops and the gradient falls below tolerance; a deliberately
  stretched bond returns to a normal C–O length; optimizing an already-optimized structure is a
  no-op (idempotence, which is also what makes the cache key honest).
- **Frequencies**: water gives 3 real modes, no imaginary; a *distorted* (unoptimized) geometry
  gives at least one imaginary — the `is_minimum=False` case the proposal says must exist.
- **Thermochemistry against measurement**: water's standard entropy at 298.15 K, σ=2, is
  45.10 cal/mol/K. Anything that fails to reproduce it within ~2 units has the physics wrong.
  ZPE against the measured 13.26 kcal/mol.
- **IR**: water's bend is the strongest of its three fundamentals (measured 53.6 km/mol vs. 2.2 and
  44.6) — an ordering, which is what a semiempirical intensity supports.
- **Reaction**: the Fischer esterification of `evals/cases/green-esterification.md` returns
  ΔE/ΔH/ΔG; an unbalanced equation is rejected; a second reaction sharing a species demonstrably
  hits the cache (assert hits, not wall clock).
- **Torsion**: n-butane's C–C–C–C profile has minima at ~180° (anti) and ~±60° (gauche), anti
  lowest, with a barrier of the right order at 0°.

## Review

**Built, and green under `make lint type test` + `make skill-validate`.** Five new calculator
modules, five new agent tools, a durable job path, six new skills and five updated ones.

**Three defects the measurements found, none of which a design review would have.** Open-shell
energies had no spin-polarization term, so triplet O2 came out *above* singlet — a qualitative
inversion that would have made every radical number wrong. The optimizer's first step could
collapse a bond and leave the SCF unconvergeable. And ordinary molecules — ethyl acetate —
optimize onto rotor saddle points, where a "free energy" is not one. Each is recorded in D-098
with the number that exposed it, and each is pinned by a test that fails if it returns.

**One scope decision reversed mid-build, correctly.** X3/X4 first shipped with an atom cap and a
point cap: refusing calculations that would block a turn. The user pushed back that these are
longer-running jobs and belong in Temporal, and the timings agreed — 4.6 s for a reaction, ~25 s
for a solvent screen, minutes for a long scan. Refusing work because it is slow is a worse answer
than running it durably. The caps that remain are practicality limits, not latency ones.

**What is still missing, stated plainly:** no transition-state search, so no barriers and no
rates; one conformer everywhere, so no ensembles; and homolysis energies that rank correctly
while being badly wrong in absolute terms. The first two are X5/X6; the third is carried by
`bond-strength-and-radicals`.


## X5-X7 (added after "continue with all remaining x")

- [x] **X5 the `xtb` binary** — `calc/xtb_cli.py`. The measurement that justified it: 8.3x on a
      76-atom substrate, 10.9x on 118 atoms, because ANCopt optimizes in normal coordinates
      (39 and 94 cycles against 177 and 232 Cartesian steps).
- [x] **X6 CREST** — `calc/crest_cli.py` + `calc/conformers.py`, conformer/tautomer/protomer
      searches, degeneracy-weighted populations, conformational entropy, `level="thorough"`.
- [x] **X7 the expert seam** — `run_xtb_task` over a typed spec, role-gated.
- [x] Both binaries pinned into the container image; every new setting in `.env.example`.
- [x] Skills: `tautomer-analysis`; `conformational-analysis` extended for ensembles;
      `docs/xtb-skill-catalogue.md` §9 ideates the seven further skills CREST's searches unlock.
- [x] ~~X9~~ retired: ANCopt *is* the internal-coordinate optimizer.
- [ ] **X8 (MCP)** — answered, not built. It is an either/or migration of the agent's advertised
      surface, not an addition, and it touches skill frontmatter, the registry test and the
      in-process `bo/` callers. Scoped in `BACKLOG.md`.
- [ ] **X10 transition states** — the largest remaining gap at the model level; unchanged by X5-X7.

### What the binaries changed about the earlier phases

Two X3/X4 decisions are now obsolete and were removed rather than left as dead weight: the
hand-written internal-coordinate optimizer (X9) is unnecessary, and the Cartesian trust-region
loop is demoted to the fallback path. Two are unchanged and were re-validated across backends:
the shared RRHO (both reproduce water's 45.10 cal/(mol K)) and the cost router (still the right
answer — with the binary, drug-sized work is minutes instead of tens of minutes, which is *still*
past any inline budget).

## X8 — the calculation capability as an MCP server

Goal (the user's, stated directly): run the calculators in **their own pod**, so the heavy
chemistry dependencies and the CPU load scale independently of the agent.

### The boundary this forces, discovered before writing any code

Not every calculator tool can move, and the reason is identity rather than chemistry:

- **`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`** route
  to Temporal above a cost threshold, and submitting a durable job needs `require_actor()` and
  `get_current_session_id()` — both **turn-ambient** and, by the F4-T3 rule, never model-supplied.
  An MCP server has neither: it is a separate process with no conversation and no authenticated
  user. Passing them as tool arguments would make identity a model-authored value, which is
  exactly the thing that rule exists to prevent.
- **`run_xtb_task`** is role-gated through `authorize_trigger` for the same reason.

So: **MCP carries capability; identity stays with the agent.** That is the line, and it also
predicts what can ever move.

### Build

- [x] `mcp_servers/calc/server.py` — FastMCP over the synchronous calculators, thin like
      `molfp`/`rxnfp`: every tool body already lives in `calc/`.
- [x] Move (not copy) those tools out of `agents/calc_tools.py`. Two advertisements of one tool
      is the failure mode to avoid.
- [x] `settings.mcp_servers` gains `mcp-calc`; `deploy/entrypoint.sh` gains the component.
- [x] **`scripts/validate_skills.py` must resolve a declared tool against MCP `allowed_tools` too.**
      Today it checks the in-process registry only, so every skill declaring a moved tool would
      fail. Fixing that is not a workaround — a skill names a *capability*, and which transport
      delivers it is a deployment decision the skill should be insulated from.
- [x] Tests: the transport test already parametrizes over configured stdio servers, so the new
      server is covered on adding it; plus the registry set, and the validator's new resolution.

### X8 review

Green. The measure of whether the boundary was drawn in the right place: **no skill changed** in
a migration that moved seven tools out of process, and `test_mcp_transport` needed no edit —
it already parametrizes over configured servers and proved the new one spawns and advertises
exactly its allowed set.

The one non-mechanical change was the validator, and it was a correction rather than an
accommodation: a skill declaring `predict_pka` is declaring a capability, and it should not care
which process answers. Widening the lookup without weakening it (an invented name still fails) is
what makes the transport a deployment decision.

## X11 — two molecules together, and the amine question the measurement re-scoped

Goal (the user's, stated directly): "leave X10 to backlog. However implement the fix for basic
amine and NCI, make it fully operational." Both were the two halves of the X11 backlog entry.

### Build

- [x] `calc/complexes.py` — `ComplexSpec`/`InteractionResult`/`compute_interaction`, over CREST
      `--nci` plus three optimizations. Interaction energy as a difference of **relaxed** species,
      so the deformation cost of binding is included rather than defined away.
- [x] `calc/pka.py` extended for bases: protomer enumeration in RDKit, most stable cation defines
      the conjugate acid, separate calibration, `site: "acid" | "base"` on the result.
- [x] `compute_interaction_energy` in `agents/calc_tools.py`, with the same cost routing as the
      other minute-scale tools (D-100) — it defers to Temporal above the inline budget.
- [x] `ComplexJobSpec` through `workflows/models.py` + `xtb_activities.py`, so it is durable.
- [x] `skills/molecular-association/SKILL.md`; `ionization-and-partitioning` rewritten around the
      measured two-class result; `calculation-selection` and `degradation-liabilities` corrected.
- [x] Tests: `tests/test_complexes.py` (CCSD(T)/CBS references, pair ordering, cache) and the base
      half of `tests/test_pka.py` (in-sample, held-out, the refusal, acid precedence).

### What the measurement changed about the plan

The plan said `--protonate`/`--deprotonate` was how U2 (basic amines) gets solved. It was not.
Fitting 20 experimental amines split the class in two, and the split is electronic rather than
structural: **aromatic and aryl nitrogen calibrates to Spearman 1.000** (RMSE 0.17 — better than
this system's acid calibration), while **aliphatic amines rank at -0.17**, which is no ranking
ability at all. A protomer *search* would not have moved that, because the failure is solvation:
gas-phase GFN2 gets the proton affinity order exactly right, ALPB reverses it, and the truth is
non-monotonic. So half the goal shipped and half is a refusal with a diagnosis (D-104), and the
CREST structural route was left unbuilt rather than built because the plan named it.

Two things the build itself taught, both caught by tests rather than by review:

- **Geometry policy is not free for bases.** MMFF geometries give ρ 0.893, GFN2-optimized ones
  1.000. Protonation pyramidalizes a nitrogen; relaxing it is doing real work, not polishing. The
  acid path keeps its own validated policy — refitting it is a separate decision.
- **`_combine` is not symmetric**, so A-with-B and B-with-A keyed to different cache entries and
  ran the same minutes-long search twice. `_ordered` canonicalizes the pair at the entry points;
  both the asymmetry and the invariant it forces are pinned by tests.

### X11 review

Green. The honest summary of the result is that the interesting half is the part that does *not*
ship: refusing aliphatic amines is worth more than a number would have been, because ρ = -0.17
carries no information while looking exactly like a value that does. The skill says so in the
same terms, so the agent declines rather than reaching for a substitute.

## Reconciliation with `main` (PR #28) — the restored tree

`main` restored the tree the Replit move had rewound while this branch was building the xTB
layer, so the merge was a feature set meeting ~38 modules it had never seen. Recorded in D-105.

- [x] ADR renumbering: this branch's ten xTB ADRs D-082…D-091 → **D-112…D-104**, `main`'s
      allocation keeps the numbers. Every citation moved with them; `tests/test_decision_log.py`
      (which `main` added as the fix for the *previous* collision) passes.
- [x] `_log_prediction` moved to `mcp_servers/calc/server.py` — it hooks `predict_pka` and
      `predict_solubility`, which X8 had already moved there. Same principle, correct layer.
- [x] `workers/background_worker.py`: kept the registry (D-099) and decorated `main`'s four new
      modules (`audit_verify`, `digest`, `note_index`, `retention`) so it serves them. Verified by
      diffing the registry's sets against `main`'s explicit lists — 14 workflows, 24 activities,
      equal both ways.
- [x] `mcp_servers/calc/server.py::predict_pka` docstring updated for X11 — it still said
      O-H/S-H only, which is the one place the base support had not actually reached the agent.

## Heavy review of the whole branch (D-106)

Read the 12k-line diff against `main`. Five real defects, all in green code, three of them
contradicted by their own docstring. Fixed and pinned:

- [x] **GFN-FF optimization could never succeed** — the geometry was checked against a GFN2
      gradient (measured 1.3e-2 vs a 5e-4 target on octane), so every run raised "did not
      converge"; a run that passed would have reported a GFN2 energy labelled GFN-FF.
      `max_gradient` is now `float | None` and GFN-FF converges on its own surface.
- [x] **A crest upgrade served stale ensembles** — `crest_cli.binary_version()` documented
      itself as being for the cache key and no key ever called it.
- [x] **`engine` inherited by two specs that never honour it** — a radical's ensemble was
      keyed as tblite's while crest did the work. `CrestSpec` fixes both; the honest
      consequence (no spin-polarization fallback for a radical search) is now documented.
- [x] **The open-shell caveat was gated on `level == "standard"`** — dropped from the
      `thorough` homolysis a user paid the most for.
- [x] **`conformer_treatment` and `conformational_entropy_kcal` could not tell the truth** —
      a single-value `Literal`, and `0.0 or None`.
- [x] Smaller: `crest_cli.run` promised an ordering it did not enforce (and `lowest` is
      `conformers[0]` after truncation); `_safe` skipped the one config-supplied argv value.

**Left open deliberately:** `ensemble_seconds` has no fixed-overhead term, so a small-molecule
CREST search is predicted at 0.5 s and runs inline when it really takes ~10 s. Same shape as
the error the cost model fixed at the large end; re-fitting it is a measurement session.


## Reconciliation with `main` (PR #31) — the process/analytical calculators (D-107)

`main` landed D-109's logD, developability descriptors, exotherm screen and ETKDG conformer
ensemble, plus two CI fixes. Seven conflicts. Two defects existed **only in the combination**,
so neither branch's tests could have caught them:

- [x] **The unit boundary.** `main`'s `geometry()` returns Bohr; X1 made this branch Angstrom
      above `calc.xtb_engine`. Its new `positions_bohr` helper fed `gfn2_energy`, which here
      converts — every ensemble geometry would have been inflated 1.8897x, plausibly. Renamed
      to `conformer_positions` (Angstrom), pinned by a test on water's O-H.
- [x] **The logD sign.** `calc.logd` hard-coded the acid Henderson-Hasselbalch form, correct
      when `calc.pka` raised for bases. X11 widened the domain; pyridine at pH 7.4 came out at
      -0.92 against a clogP of 1.08 — two log units, silent. Now branches on `PkaResult.site`.
- [x] ADR renumbering, third time: this branch's D-109…D-103 → **D-112…D-106**.
- [x] `workers/background_worker.py`: registry kept, `main`'s conformer workflow + activities
      decorated. Verified served (15 workflows, 26 activities).
- [x] `tests/test_calc_tools.py`: two module aliases, since X8 split the tools across two
      transports and `main`'s three new ones stayed in-process.

**Owed, not done:** two implementations of conformer ensembles and two of reaction energetics
now coexist. Kept both — deleting either is a product decision. Tracked at the top of
`BACKLOG.md`.

## Consolidation: one conformer ensemble, one reaction composite (D-108)

Instruction: remove the old tools completely and replace them with the newly developed
framework. D-107 had kept both and recorded that a decision was owed; this is it.

- [x] Removed `calc/conformer_ensemble.py`, `calc/reaction_energy.py`,
      `agents/conformer_tools.py`, `workflows/conformer_{job,models,activities}.py`,
      their four test modules, and four now-dead config settings + env entries.
- [x] Ported the **exotherm flag** onto `compute_reaction_energy`
      (`is_strongly_exothermic` / `exotherm_threshold_kcal`) so consolidating lost no
      capability — pinned by `test_the_exotherm_flag_survived_the_consolidation`.
- [x] Repointed `skills/calculation-selection` at `sample_conformers` /
      `compute_reaction_energy` + `get_job_status`, and `DEFERRED.md`'s ANI-2x row at
      `calc.conformers`.
- [x] `tests/test_workers.py` now asserts the xTB job is on the **`hpc`** queue — the
      removed workflow was on `background`, which was the wrong queue for a minutes-long
      CREST search (D-006).
- [x] Verified end-to-end: three tool names gone from the registry, every replacement
      present, 37 in-process tools.

**Kept, because they were never duplicates:** `predict_logd`,
`predict_developability_profile`, `generate_screening_design` — genuinely new capability
from D-109 with no counterpart on this branch.

**Cost accepted and recorded in D-108:** the exotherm screen was seconds on cached single
points; `level="quick"` is the equivalent gear but still optimizes. And CREST is an optional
binary — the deployment image ships it, but a bare `pip install` dev environment now has no
conformer ensemble where it previously had a weaker one.
