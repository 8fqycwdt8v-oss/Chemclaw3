# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phases 0–5b of the plan are **implemented and CHECKMATE-reviewed**: toolchain + config,
the agent+Temporal spine, fast calculators (xTB/pKa/solubility) with the Postgres
calculation cache (the calculators themselves have since moved to `Chemclaw3-mcp`; the cache and
the ledger stayed), BoFire BO campaigns, the knowledge graph + PR-gate, the eval/metric
layer, ECFP4/DRFP fingerprint search, ELN ingestion, the memory layers, and the report
harness.

The **foundation build F0–F7** (the real target stack: OpenShift + an internal
OpenAI-compatible LLM, Entra identity system-wide) is **implemented for everything verifiable
offline**, each phase ADR'd (D-039…D-050) and green under `make lint type test`:

- **F0** LLM provider seam (generic credential, not Entra) · **F1** the plan/execute harness ·
  **F2** FastAPI+SSE front door · **F3** durable Postgres sessions + job→session push-back.
- **F4** Entra identity/RBAC: front-door OIDC, one authorization gate, `require_actor` reject-if-absent
  core rule, Temporal-mTLS. (Workload-identity federation, OBO and the HPC identity bridge were
  built and never wired to anything; D-2026-08-15 deleted all three — 254 LOC whose only callers
  were their own tests. Re-adding one is a new decision, and the ADRs that designed them stand.)
- **F5** was the real Nextflow (Seqera/Tower) launcher behind the QM activities. **It is gone**
  along with the whole HPC/DFT tier — see the section below.
- **F6** OpenShift delivery: one rootless image, Helm chart, CI, the plain-secret set `values.yaml`
  declares and `tests/test_helm_chart.py` pins, Temporal self-hosted.
- **F7** the generic `DataSource` seam (`chemclaw.ingest.sources`) — ELN re-hosted unchanged; a new source is one
  `ingest/sources/<name>/datasource.yaml` folder plus its name in `CHEMCLAW_DATA_SOURCES`, with **zero**
  core edits (D-120). The first live connector — a warehouse ELN — went one step further
  (D-2026-08-04-the-schema-is-a-file): `chemclaw.ingest.eln.warehouse` is a generic engine naming no
  table and no column, and the site's schema is a *binding* in the manifest, because a schema nobody
  can see yet cannot be written into Python. Both halves ship, proven against a fake driver; only the
  tenant is missing. **The database it attaches to is as free as the schema**
  (D-2026-08-26-the-driver-s-signature-is-the-schema): a `connection:` block is the driver's *own*
  keyword arguments, checked against its signature offline, so a lakehouse, a Postgres, a DuckDB
  export and a vector database need no shared model to be their union — the Snowflake driver that
  model was shaped around never had a tenant and is deleted, the first integration is **Pistachio on
  Databricks**, and `vector_store_provider` takes a `module:callable` on the same terms.
  The same argument then carried a **mounted SMB/CIFS file share**
  (`chemclaw.ingest.documents`, D-2026-08-06): the share's folder tree is a binding, the share is
  *mounted* rather than called (no client, no credential, no egress), its documents are indexed as
  cited evidence rather than PR-gated notes, and its AD group becomes an entitlement in the one role
  set every gate already reads.

**Layer 1 was then rebuilt on LangGraph** (D-2026-08-10, phases M0–M13), replacing the Microsoft
Agent Framework everything above was first built on. The case was never capability — it was that
four pieces of this tree existed only to work around framework defects, and two of those defects
were *silent*: one agent leased per concurrent turn, because the Anthropic client kept streaming
tool-call identity on the client instance (8/8 concurrent turns failed on a shared client, 0/8 on
per-turn ones), and a history-persistence flag whose consequence was that **harness mode never
worked** while every unit test passed. What stands in their place: `agent/langgraph_agent.py`
builds a compiled graph over `create_agent` — per turn, because LangGraph binds tools at
construction and a connector session belongs to exactly one turn; turn state lives in a Postgres
checkpointer (`agent/checkpointer.py`) on its own autocommit pool instead of being hand-built;
the tool chain is seven `@wrap_tool_call` middlewares in the old nesting order over the *same*
extracted decision functions, so an authorization refusal or an audit row cannot depend on which
engine ran; skills come from `deepagents.SkillsMiddleware` over a backend narrowed by the same
three predicates (`agent/skill_backend.py` — the gate had to move to the backend because deepagents
publishes skill *paths* into the prompt); the plan is `TodoListMiddleware`'s todo list, which the
gate (`agent/plan_gate.py`) reads as it stands at that instant; the runaway cap is upstream's
`ModelCallLimitMiddleware`, subclassed only to record that it fired (`agent/loop_cap.py`).

**There is no specialist team and no challenge panel** (D-2026-08-15). Both shipped off, stayed off
in every configuration, and were deleted with the routing measurement built to decide whether the
first should ever be turned on — 1,442 lines of agent code, ~400 of eval machinery, 1,506 lines of
tests, seven settings and three metric series, none of it reachable. The delegation question was
never settled and this corpus could not settle it: D-2026-08-12 measured **2 of 15**, D-2026-08-13's
reframing measured **14/15 against 14/15** with the old arm already at ceiling, and two of the
fifteen probes span two specialists so the accuracy figure had an unpassable floor before any model
was involved. Neither number was a deployment's rate.

`reject_widening` went with it, deliberately: a guard with no caller, kept alive by a test that
calls it directly, is the `map_to_hpc_identity` shape — a claim that a control exists. The invariant
is not lost, because an invariant is not a function. `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`
is merged and states the rule, and it binds whoever re-adds subagents. So does the constraint that
outlives all of this: deepagents builds a bare `SubAgent` dict with *only* `spec["middleware"]`, so
anything not compiled by `build_langgraph_agent` runs with no audit trail, no authz and no plan
gate — silently.

**That sweep missed one, and 2026-08-26 finished it**
(`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`): `audit_events.agent` was
empty on **every row that trail has ever written**, because `set_current_specialist` had no caller
in `src/` and `record_handoff` had none anywhere — while three docstrings said in the present tense
that the trail names the agent beside the human. The contextvar trio, `record_handoff` and
`HandoffSignal` are gone; the column, `HandoffEvent` and the D-2026-08-10 rule stay, and an
*absence* test now fails whoever re-adds the claim without a producer.

An audit against LangChain's own **deep-agents** pillars (D-2026-08-11-a-policy-nobody-can-see…)
then found five of six sound and each narrowing already argued for — and the sixth, *context
management*, gone. D-025's compaction lived in the removed framework, and what survived it was the
appearance of the policy: three settings with no reader, a config comment in the present tense, and
a sentence in the system prompt telling the model its context was compacted while the whole thread
was replayed every turn. `agent/compaction.py` is that policy again (upstream's `ClearToolUsesEdit`
for tool results, a first-party conversation window, both non-destructive inside `wrap_model_call`),
and `chemclaw_context_compactions_total` is what makes it checkable rather than believed. The same
sweep put the LangGraph checkpoint tables into `durable/retention.py` — pruned by *thread*, because
`parent_checkpoint_id` chains them — and gave the CLI the checkpointer two of its docstrings already
described. **LangSmith is declined** (D-2026-08-11-the-observability-gap…): it is proprietary with no
OSS self-host, and its core value is prompt/response content in a third-party service, which four
merged decisions forbid. The trace half of what it was wanted for is now first-party
(D-2026-08-11-a-model-call-is-a-span…): `CHEMCLAW_OTEL_LLM_SPANS` attaches OpenInference's LangChain
instrumentation, so a **model call is a span** carrying its token counts, model name and provider —
closing the two regressions the framework removal left — with content suppressed by default and
`otel_include_sensitive_data` restored as the one knob that decides it. The instrumentation is
Apache-2.0 and speaks plain OTLP, so **Arize Phoenix is a deployment choice rather than a
dependency**; what stays open is AG-13's eval surface, which wants that backend actually run.

M13 removed the dependency itself: `agent-framework-*` is out of `pyproject.toml` and the suite is
green with it uninstalled, which is how that was verified. Taking it out is also what exposed
readers that only knew the *old* stored message shape — `chemclaw.cli.explain` was rendering every
current session's audit reconstruction blank — so `session_store.message_from_row` is now the one
function allowed to decide which serialization a `session_messages` row holds
(D-2026-08-11-what-the-removal-found).

A later pass (D-2026-08-14-the-coupling-is-the-cost-not-the-line-count) asked what the LangGraph
stack now does out of the box, and answered it against the installed distributions rather than the
documentation. The finding worth carrying: **what breaks on a dependency bump is not the volume of
first-party code but the number of places reading a shape upstream never promised.** Six existed;
`tests/test_upstream_surface.py` now asserts every one in a single file, each naming the module that
would break, two of them asserting an *absence* so that upstream fixing something turns the
workaround red instead of letting it outlive its reason. The same pass moved the runaway cap onto
`ModelCallLimitMiddleware` and **that half was reverted a day later**
(`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`): upstream counts in
`after_model`, which any middleware jumping from `after_model` runs *before* and short-circuits —
measured, the challenge gate's revision jump let a cap of 2 run 4 model calls — and its
`exit_behavior="end"` fabricates an assistant message the CLI, the specialist report and the
persisted thread all read. The general rule left behind: **`ModelCallLimitMiddleware` is unsafe to
compose with any middleware that jumps from `after_model`.** What survived is the reduction of
`ReloadingSkillsMiddleware` to a single `UntrackedValue` channel, deleting a dependency on the
*arity* LangChain invokes a hook with — with upstream's `PrivateStateAttr` kept, because dropping it
put the role-narrowed skills listing into the graph's *input* schema where a caller could replace it.
`tests/test_state_channels.py` now drives a compiled graph for every channel `ChemclawState`
declares, because all three of that week's defects were a hook writing a channel the graph did not
have — which LangGraph drops in silence. It also declined three adoptions
that looked obvious and are not: `ToolErrorMiddleware` and `ToolRetryMiddleware` both trigger on
raised exceptions and MCP tools never raise, and `plan_state`'s `channel_values` read turned out to
be public `Checkpoint` API rather than an internal. The same pass **built and reverted** the front door's
move to `stream_events(version="v3")`: it retires the largest coupling of all — `astream`'s tuple
arity — and the event contract survived unmodified, but v3 reports token usage only at
`message-finish`, so a turn abandoned mid-message books **0** tokens where the current driver books
~30, which makes "drop the connection just before the answer" a free bypass of the token budget.
A maintenance coupling is the smaller harm; the finding and the restart condition are in the ADR.
**GxP is no longer a constraint on layer 1** — a conclusion
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` reached
independently and carried out, removing the audit hash chain while keeping the trail, the gates and
the INSERT-only grant. What that leaves open in `docs/planning/BACKLOG.md` is the durable approval
store, the `session_messages` read-model and `HumanInTheLoopMiddleware`. `RubricMiddleware` is **declined** (`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer`) — it cannot reuse `score_answer`, and a failed grading returns the ungraded answer.

**There is no HPC tier, and there is no DFT** (`D-2026-08-26-semiempirical-is-the-whole-tier`).
Every calculation this system runs is semiempirical — GFN2-xTB through tblite, and CREST — and it
runs in its own pod (`Chemclaw3-mcp`'s `servers/calc`, addressed by `CHEMCLAW_CALC_SERVER_URL`) on
OpenShift or Databricks, never on a cluster. The `qm` connector bundle, the `hpc` config section and
its fourteen `hpc_*` settings, the Seqera/Tower launcher, the mock launcher in `Chemclaw3_mock`, the chart's
`connectors.qm` entry and `hpcApiToken` secret, and `compute_dft_energy` itself are all deleted —
not deferred. Three things were deliberately kept and each says why in the ADR: the `dft` *backfill*
projector (`calculation_results` is never pruned, so a deployment still holds rows the removed
bundle stamped), the parent-ceiling invariant rewritten against `xtb_job_timeout_seconds` (the CREST
search is the longest activity now), and `job-result` back in core's `KNOWN_NOTE_TYPES` because no
bundle mints it any more. **When a decision turns on a difference inside GFN2-xTB's error bar, say
so and propose an experiment — there is no tier to escalate to.**

**Live edges remain open** (need a real Temporal broker / OpenShift cluster): live cluster durability
+ `helm`/`kubeconform` render. See `docs/planning/BACKLOG.md` for the exact list. Note that the
render edge now has one more thing to catch: `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`
makes the chart **refuse to render** until a release states its egress posture, so `helm template` on
the shipped defaults takes `--set networkPolicy.allowAnyDestination=true` — as the Makefile's two
renders, the runbook and `deploy/README.md` all now do. The same ADR derives
`CHEMCLAW_CONNECTORS_ENABLED` from the `connectors` block (`enabled: false` used to take a bundle's
pods and leave its tools advertised) and splits `replicas` into `serverReplicas`/`workerReplicas`.

**Identity is no longer one of them** (D-2026-08-20-a-tenant-is-a-jwks-document-and-an-issuer-string).
A tenant, to a resource server, is a JWKS document and an issuer string, so `Chemclaw3_mock`'s
`app/entra/` is one: `tests/test_entra_end_to_end.py` runs the production app with
`entra_required=True` against a real HTTP JWKS with nothing patched, and `make live-up` runs the
enforced posture end to end. The one hop still unproven is browser → tenant, because MSAL talks to
`login.microsoftonline.com` and mocking that is mocking a login UI rather than a key set.

**On the design documents below: they are historical, not current.** `docs/reference/architektur.md` is
pre-implementation design and contains **zero** references to connectors — the seam that now carries
every tool, job and skill (D-118) — so it describes a system that no longer exists in its details
while remaining right about the four layers. Read it for intent; read `docs/decisions/`, the package
READMEs and `docs/guides/runbook.md` for what is true today.

- `docs/reference/architektur.md` — the four-layer architecture (§6 = the real OpenShift/internal-LLM
  deployment; §7/§8 = Entra durchgängig). Its HPC/SLURM/Nextflow and DFT-escalation prose describes a
  design that was retracted in full; §6 carries the note saying so.
- `docs/archive/plans/implementation-plan.md` — the original build order; `docs/archive/plans/implementation-tickets.md` — the
  F0–F9 ticket backlog with per-phase status.

## Related repositories

This repo is the backend/orchestration core. **Three** companion repos complete the system and are
developed separately. Work only within this family — `chemclaw` and `chemclaw2*` are earlier
generations and are not in scope for any task here.

- [`8fqycwdt8v-oss/Chemclaw3-mcp`](https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp) — the MCP tool
  fleet: one capability per server, one server per process, each with the `connector.yaml` this repo
  picks up with no code change. Every server answers from data baked into its image and makes **no
  outbound call at request time**.
- [`8fqycwdt8v-oss/Chemclaw3_ui`](https://github.com/8fqycwdt8v-oss/Chemclaw3_ui) — the ChemClaw3
  frontend.
- [`8fqycwdt8v-oss/Chemclaw3_mock`](https://github.com/8fqycwdt8v-oss/Chemclaw3_mock) — a mock
  server that stands in for external MCP tools and data sources, so the system can be live-tested
  end-to-end without real integrations.

**Where a capability belongs.** This repo holds *infrastructure*: conversation orchestration, the
knowledge graph, retrieval, memory, ingestion, identity, **publication** and durable execution. Scientific capability
— quantum chemistry, reaction prediction, property lookup, optimization — belongs in `Chemclaw3-mcp`
as a server. **The boundary within science is by *composability*, not by speed or by subject**
(`D-2026-08-16-the-physics-leaves-the-cache-stays`): a *primitive* — one calculation whose identity
is derivable from its inputs — is a stateless MCP server there, while *orchestration* and the D-011
cache stay here. A **composite**, whose key would name an output, is not shipped at all: it is
decomposed, and this repo composes the parts so every step is cached. Scientific capability here
means **semiempirical** capability: there is no DFT and no cluster
(`D-2026-08-26-semiempirical-is-the-whole-tier`).

That replaced an earlier fast/slow rule, and measurement is what replaced it. Leaving the durable
jobs' physics here would have *copied* the engine rather than moved it — the four modules behind
them transitively needed almost all of it — and shipping `compute_thermochemistry` whole would have
turned a 0.007 s repeat into a full recompute, because its key names the geometry its refinement
loop settles on. Duration was never the property that mattered; a server there may be slow, it may
not be stateful.

**Computed values leave, too** (`D-2026-08-25-a-cache-is-not-a-record`). `calculation_results` is a
*cache* — `key` onto an opaque `result JSONB`, and its own query model refuses any predicate on the
payload, because "a `total_energy_hartree > x` predicate would put one calculator's schema inside
the thing that persists all of them". That is right for exact-key lookup and is exactly why it
cannot also be the scientific record. So `src/chemclaw/publish/` projects every result — primitive
or composite, single compound, multi-compound, reaction or ensemble — into a typed record and
delivers it to a database this system does **not** own: a third manifest seam beside
`connector.yaml` and `datasource.yaml`, because a connector *produces*, a source *supplies*, and a
sink *consumes what the system produced*. The schema ships in `schema/result-store/` and a site
creates it; publishing is off until `CHEMCLAW_RESULT_SINKS` names a sink.

`science/fingerprints` stays here despite the name — retrieval, memory and ELN ingest import it
in-process, which makes it infrastructure by this rule rather than an exception to it. `science/safety` used to be listed beside it on the same grounds; that argument
died when the `kg-validate` hazard gate that made the claim true was retired
(`D-2026-08-15-safety-is-a-tool-not-a-gate`), and the screen is now an ordinary MCP server with no
in-process caller left.

If a task requires changing or fixing code that lives in a companion repo (not this one), add that
repo to the session (`add_repo`) and open a PR directly against it — do not proxy the change through
this repo, and do not just describe the fix here and stop. Each repo gets its own branch/commit/PR,
scoped to that repo's own conventions. Only pause to ask first if the required change is
destructive, ambiguous, or outside what was asked.

## Architecture (the one thing to internalize)

`ARCHITECTURE.md` maps every directory to its layer and explains the two name pairs that look
like duplicates and are not (`science/calc/` vs `connectors/calc/`; `skills/` vs
`connectors/*/skills/`). Adding a top-level directory or a subpackage means **adding a row there
and giving the directory a `README.md`** — `tests/test_repo_map.py` fails otherwise (D-156).

Three rules the tree is arranged around, each enforced by a test rather than asked for:

- **`src/` is all the code.** Everything beside it is data, configuration or documents.
- **Capability code lives in a connector bundle or in `science/`, nowhere else.** The rule stands;
  what it covers shrank. `science/bo` and `science/fingerprints` are still engines — pure
  computation, with the bundle as their durable-job and MCP wrapper, a pair rather than a
  duplication. `science/calc` no longer holds an engine at all: after
  `D-2026-08-16-the-physics-leaves-the-cache-stays` it is the cache, the calibration ledger, the
  RRHO/Crippen arithmetic and the models the Temporal wire carries, while the physics answers from
  `Chemclaw3-mcp`.
- **`data/` holds every corpus the code reads at runtime** — except `knowledge/` and `skills/`,
  which stay at the root because they are architecture layers 4 and 3, not configuration.

Four layers, each with a single responsibility. **Never merge their concerns.**

1. **LangGraph** — conversation orchestration + short reasoning steps, as one compiled graph per
   turn with a Postgres checkpointer under it.
2. **Temporal** — durable execution of long/expensive jobs: the semiempirical calculations
   (xTB/GFN2 single points, CREST conformer and complex searches, scans, rotational profiles) and
   BoFire BO. Queues: `background-jobs` for core's light work (sync, re-index, reports, the
   connector-job wrapper) plus one derived `connector-<name>` queue per bundle that owns durable
   work (D-118/D-150), so there is no second *core* queue.
   Every result is persisted once via the calculation store, and a *persisted* result is never
   recomputed (D-011) — `cached_compute` is a check-then-act, so concurrent misses on one key each
   compute (measured: 8 together → 8 computes; 4 after the write → 0). Per-key in-flight dedup is a
   `docs/planning/DEFERRED.md` row with its own trigger.
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data + relations).

Durability lives **only** in Temporal, never in the conversation layer's own ad-hoc stores — the
rule is D-002's and it got *stricter* when layer 1 gained a checkpointer, because the checkpointer
holds turn state and every long or expensive job is still Temporal's (D-2026-08-10 §3). Skills hold
judgment; **connectors** hold capability (deterministic tools) — MCP is the protocol a connector
speaks, not the thing that holds the capability (D-110/D-118). Anything agent-*asserted* enters the
graph via a **PR-gate** (human validates before merge) — the agent proposes, a human decides, reused
everywhere (job results, reports, distilled playbooks). See `docs/reference/architektur.md` §4, §9, §12.

**A deterministic transcription is not an assertion, and is not gated**
(D-2026-08-25-an-eln-transcription-is-data-not-a-claim). An ELN entry becomes a row in
`reaction_records` — readable the moment it is ingested, queryable by structure, expandable into its
recipe — because `record_from_ord_reaction` infers nothing and so hands a reviewer nothing to
decide. Measured, the gate cost 202 ms of serialized git per entry and a corpus scan that wedged the
sync at ~700k entries, for 4 person-years of clicking per million. The same rule now runs the other
way too: **no Temporal Schedule opens a pull request.** The campaign/playbook/optimization miners are
unchanged and still run, on demand rather than hourly, so knowledge never arrives on a timer.

## Commands

The toolchain is scaffolded and `make help` (the default goal) lists every target — a count is not
written here, because the one that was said 23 while the file held 28. Use them rather than raw
invocations — CI runs exactly these, so a green `make` locally means a green CI.

- **The gate**: `make lint` (ruff lint + format) · `make type` (`mypy --strict`, every first-party
  package) · `make test` (pytest) · `make check` runs all three · `make cov` adds the coverage floor.
- **The validators**, each guarding a declaration against the live surface: `kg-validate`,
  `skill-validate`, `connector-validate`, `datasource-validate`, `template-validate`,
  `prose-validate`, `eln-validate`, `helm-validate`.
- **Running things**: `make up` (docker-compose: Temporal + Postgres/pgvector) · `make connectors`
  (every enabled connector in one dev process) · `make chat` · `make db-migrate`.
- Single test: `pytest path/to/test_file.py::test_name` or `pytest -k "name substring"`.

A step is done only when its acceptance check passes **and** `make lint type test` is green.

## Workflow (how to work a task)

**Plan first.** For any non-trivial task (3+ steps or an architectural decision), enter plan
mode before touching code; simple, obvious fixes skip this. Write the plan to `tasks/todo.md`
as checkable items, write detailed specs upfront to kill ambiguity, and check in before
implementing. Mark items done as you go and give a one-line summary at each step. Plan
verification too, not just building. If something goes sideways, **stop and re-plan** — never
keep pushing a failing approach. Close the loop with a short review section in `tasks/todo.md`.

**Verify before done.** Never mark a task complete without proving it works: run the tests,
check the logs, demonstrate correctness. Where it clarifies things, diff behavior between the
base and your change. The bar is "would a staff engineer approve this?" — if not, it is not done.

**Fix bugs autonomously.** Given a bug report, failing CI, or an error/log, just fix it:
find the root cause and resolve it without asking for hand-holding or step-by-step direction.

**Ship automatically.** Once a task is fully done and verified (tests pass, `make lint type
test` green where applicable), do not stop at a pushed branch and wait for a go-ahead: open the
PR, merge it directly to `main` yourself, and delete the branch once the PR is closed. This
applies here and in the companion repos (`Chemclaw3_ui`, `Chemclaw3_mock`) — each repo's change
gets its own PR, auto-merged the same way. Skip the auto-merge only if CI is red, the change is
destructive/ambiguous, or the user asked to review before merge for this task.

## Code quality (non-negotiable)

- **Perfection over speed**: when unsure, ask — do not guess.
- **Demand elegance (balanced)**: for non-trivial changes, pause and ask "is there a more
  elegant way?" and challenge your own work before presenting it. If a fix feels hacky,
  redo it as the elegant solution knowing everything you now know. Skip this for simple,
  obvious fixes — don't over-engineer.
- **Root cause, not band-aid**: no temporary patches; fix the underlying cause. Keep changes
  minimal and focused — touch only what the task needs, and don't introduce new bugs.
- **Measure it, don't argue it**: when two explanations of a defect compete, or a claim is that
  something works, run it and report the number. Prose is evidence about what its author believed,
  never about what the code does — a solvent-domination fix was asserted by two docstrings, an ADR
  and a closed backlog row, and the similarity was unchanged to the fourth decimal. The cost is
  usually one script; the alternative is picking the more articulate explanation, which is
  uncorrelated with the true one. This is also how you find out both explanations were wrong: the
  retrieval leg everyone was arguing about turned out to contribute *zero* chunks, and the mechanism
  each side blamed was mitigating a third cause neither had named (D-2026-08-01-a-cap-that-starves-a-source).
- **KISS**: simplest working solution; no over-engineering. No abstraction without a second
  real caller (Rule of Three); an abstraction with one caller gets inlined.
- **DRY**: no duplicate logic — extract shared code. The PR-gate and the retriever interface
  are single reusable pieces, not copy-paste.
- **No boilerplate**: only code that is actually used. Delete dead params, empty interfaces,
  and "for later" stubs on sight.
- **Docstrings on every module/function**: state the *purpose* and the *why*, not just the what.
  Every public function is fully type-annotated.
- **Small, single-responsibility, clearly named functions.**
- **After every change**: run existing tests, add tests where they prove behavior (not mocks).
- **Config, never magic numbers**: every URL, path, threshold, timeout, model name comes from
  the one `pydantic-settings` config, ENV-overridable.

Run the plan's **Quality-Gate ("Checkmate")** checklist (G1–G7, see `docs/archive/plans/implementation-plan.md`)
after each cluster of steps before moving on.

## Persistent knowledge (read at session start, update at session end)

- `docs/planning/BACKLOG.md` — prioritized open action items, with rationale. It does **not** track
  who is currently working an item. When someone actually starts one, open a GitHub Issue linking
  back to the row (or the ADR behind it) and mark the row `(issue #NNN)` — the issue's assignee is
  the claim (atomic; a row edit is not) and its label/linked PR is the status. Delete the `BACKLOG.md`
  row in the same commit that merges the PR, same rule as `DEFERRED.md` below. Not test-enforced —
  see `docs/decisions/D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit.md`.
- `docs/planning/DEFERRED.md` — consciously postponed items **with the reason they are not now**.
  It is a register of what is *pending*, never a log of what was decided. **When an ADR closes a
  deferral, delete its row in the same commit** — do not strike it through, and do not append a
  status note or a dated section saying an earlier row is now out of date. The ADR is the record and
  `git log` is the history; a row that outlives its closure reads as live state. Appending instead
  of deleting is exactly how the file grew nine sections describing each other, three of them false
  and five rows describing shipped work (D-154). `tests/test_deferred_register.py` enforces what a
  machine can see of this.
- `docs/decisions/` — architecture decisions with rationale, one file per ADR (`D-NNN-<slug>.md`).
  Never edit a merged ADR; a decision that has changed gets a new ADR that supersedes it.
- `docs/decisions/README.md` — the `D-NNN` allocation ledger, one row per number. **Every session that
  writes an ADR must reserve its number here** (see below).
- `tasks/lessons.md` — self-improvement log. Review it at session start; after **any**
  correction from the user, add the pattern here and write a rule for yourself that prevents
  the same mistake. Iterate ruthlessly until the mistake rate drops.

Keep these current; they are the memory across sessions. For recurring patterns, prefer a
`.claude/skills/<name>/SKILL.md` over bloating this file.

### Writing an ADR

**Name the file `D-YYYY-MM-DD-<slug>.md`, today's date plus a slug naming the decision, and add its
row to `docs/decisions/README.md`.** That is the whole procedure. Nothing to enumerate, nothing to
reserve, nothing to coordinate with other sessions.

The id is the *whole stem*, not the date — two ADRs on one day is normal here, and an id naming two
decisions is the failure the ledger exists to prevent.

**Why not numbers any more.** ADR numbers collided repeatedly, and the cause was structural rather
than careless: many sessions run at once, and "highest on `origin/main`, plus one" is a read that is
stale the moment another session pushes. D-147 made a collision *loud* (one file per ADR, so two
claims to one number conflict on a filename) and left the allocation itself unfixed, so they kept
happening — in a single day one branch renumbered three ADRs twice while another renumbered three
times, five collisions, all on numbers nobody had merged. This file used to name date-plus-slug ids
as the escape hatch to take deliberately if that continued. It continued; D-2026-07-31 takes it.

**The `D-NNN` sequence is frozen, not migrated.** Every numbered ADR keeps its name, so every
citation still resolves — a *merged* ADR never collided, and there are no unallocated numbers left
to contend for. Never renumber one, and never renumber to close a gap: a gap is harmless, a moved
number breaks every citation to it (`D-008` was written after `D-009` for exactly this reason).

`tests/test_decision_log.py` enforces both forms — unique ids, filename matching heading, and the
ledger listing exactly the files beside it, in record order.

## Token / context management

- **Compact policy** — when context is compacted (`/compact`), the summary MUST preserve:
  open TODOs (from `docs/planning/BACKLOG.md`), API/interface changes **with their rationale**, the list of
  changed files, and a one-line summary of any failed approach (so it is not retried).
- After finishing a self-contained step, actively suggest/use `/compact` (or `/clear`).
- Keep replies as short as possible; no explanations without added value.
- Use **subagents** liberally to keep the main context clean: offload research, exploration,
  and parallel analysis so failed attempts never accumulate in the main window (subagents
  have their own context and tools). One focused task per subagent. For hard problems, throw
  more compute at them by fanning out across several subagents.

## The sandbox is not offline — start the infrastructure

**Docker, Postgres and Temporal all run in the Claude Code Remote environment for this repo.** The
daemon is simply not started at session start, which is easy to misread as "no Docker here": a bare
`docker info` fails, `/var/run/docker.sock` is absent, and every Postgres-backed test then skips
with a message that *says* "offline sandbox" (`tests/pg.py`). That message describes a default, not
a limit.

```
sudo -n dockerd &        # then `docker info` answers within ~8s
make up                  # Postgres/pgvector + Temporal + the Temporal UI
make db-migrate
```

Why this matters enough to be written down: believing it costs coverage silently. A full local
`pytest` skips ~157 Postgres tests and still prints a green line, so a change that breaks the
durable layer, the session store, the note-proposal tables or retention passes locally and fails in
CI — and the session that trusted the green line has already pushed. The same belief makes the live
lane (`make live-infra`, `make live-up`, `make live-probes`) and the four-repo
`infra/live/e2e-full-stack/up.sh` look impossible when they are not.

**So: start the daemon before claiming anything about the suite, and never report a local run as
green without saying what it skipped.**

## Local live/e2e credentials

Some Claude Code Remote environments for this repo carry a working Anthropic credential for the
live lane (`infra/live/`, `infra/live/e2e-full-stack/`) as an environment variable literally named
`API-KEY` — hyphenated, so it is not `$`-referenceable in bash and has to be read with
`printenv 'API-KEY'`. Where present, map it to `ANTHROPIC_API_KEY` before starting the front door;
`infra/live/e2e-full-stack/up.sh` does this automatically. Never print or commit the value itself —
this note records where to look, not what it is, and it may not exist in every environment.

## Governance

Treat this file like code: version it, review changes in a PR, and re-test it in a fresh
session before merge. Do not duplicate anything already in `README.md` or a package manifest.
