# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this system is, today

**This section states what is true. It does not say how it got that way** — that is
`docs/decisions/`, one file per ADR with a ledger row each in `docs/decisions/README.md`, and it used
to be a changelog in this file taking up over half of it. That changelog carried most of this file's
falsifiable surface: audited, its load-bearing figures were *mostly wrong*, including a spend cap it called "ships at 0" after two raises, a tool
count whose base had moved under the subtraction it was making, and a bolded headline saying no
specialist team ships 68 lines above a bolded headline saying one does
(`D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision`). **No figure appears in this section.** Where a
number matters, the symbol that holds it is named instead, and `tests/test_claude_md_figures.py`
is what keeps a new one from creeping back.

**Layers.** Four, never merged (`ARCHITECTURE.md` maps every directory):

1. **LangGraph** — conversation orchestration, one compiled graph per turn over `create_agent`
   (`agent/langgraph_agent.py`), with a Postgres checkpointer (`agent/checkpointer.py`) on its own
   autocommit pool. Per turn, because LangGraph binds tools at construction and a connector session
   belongs to one turn.
2. **Temporal** — durable execution of long or expensive work: the semiempirical calculations and
   BoFire BO. Queues: `background-jobs` plus one derived `connector-<name>` per bundle that owns
   durable work. A persisted result is never recomputed (D-011); `cached_compute` single-flights
   concurrent misses on one key *in one process*, and the cross-process half is a `DEFERRED.md` row.
3. **Agent Skills** (`SKILL.md`) — judgment, loaded on demand.
4. **Markdown knowledge graph in Git** — what we know. `kg/record.py` is the one write path, and its
   order is load-bearing: dependencies, then the subject, then the retirements, so a note never
   appears in the graph before what it cites.

Durability lives **only** in Temporal, never in layer 1's own stores — stricter since the
checkpointer arrived, because the checkpointer holds turn state and every long job is still
Temporal's.

**The tool chain** is `@wrap_tool_call` middlewares over extracted decision functions, so an
authorization refusal or an audit row cannot depend on which engine ran. How many there are is
`len(tool_call_middleware(...))`. Skills come from `deepagents.SkillsMiddleware` over a backend
narrowed by three predicates (`agent/skill_backend.py`) — the gate is in the backend because
deepagents publishes skill *paths* into the prompt. The plan is `TodoListMiddleware`'s todo list,
read by `agent/plan_gate.py` as it stands at that instant, and the runaway cap is a first-party
`before_model` counter over `ChemclawState.model_calls` (`agent/loop_cap.py`), so the number that
enforces the limit and the number that records it are the same number.

**`ModelCallLimitMiddleware` is unsafe to compose with any middleware that jumps from
`after_model`**, which is the general rule left by trying it and reverting: upstream counts in
`after_model`, so a jumping middleware short-circuits the count, and its `exit_behavior="end"`
fabricates an assistant message the CLI, the report and the persisted thread all read.

**Delegation.** `task` ships on every turn whether or not a deployment wants it —
`SubAgentMiddleware` is in `create_deep_agent`'s `_REQUIRED_MIDDLEWARE` and `_apply_excluded_middleware`
*raises* rather than let a profile strip it. So the only decidable thing is what a helper reaches.
**A helper is an attenuation, not a new actor** (`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`):
its surface is *what its caller holds ∩ what the profile names − `authz.side_effecting_tools()`*, on
both the in-process and the connector half, plus `ask_clarifying_question`, which would write onto a
chemist's stream from a context the chemist cannot see. `tests/test_subagents.py` asserts the strict
subset rather than either count. A named roster ships and is **on by default**
(`agent_helper_roster`, profiles in `data/profiles/`); what a name varies is only its instructions
and its model route, both of which carry no authority, and selection is the model's ordinary tool
call. Adding a profile is a file, not a decision. A helper shares the connector sessions its caller
already opened and opens none of its own. Its report is **defanged, not framed** — it is this
system's own paraphrase, and `agent/tool_result_shape.py` is the one function both result-rewriting
middlewares go through, because `task` returns a `Command` rather than a `ToolMessage`.

**Handoff.** Delegation returns; a handoff does not. With `agent_peer_roster` set,
`agent/turn_graph.py` compiles a `StateGraph` whose nodes are several `build_langgraph_agent`
graphs, and a `transfer_to_<peer>` tool moves the conversation between them with
`Command(goto=…, graph=Command.PARENT)`; `active_agent` is checkpointed, so a later turn resumes
with whoever held it. A peer keeps the acting tools and answers the chemist directly, which is what
a helper may not do. **A handoff redistributes the turn's authority and cannot extend it**
(`D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`): a peer's surface
is *the **root's** surface ∩ what the profile names*, so a chain of any length is bounded by its
first frame — strictly stronger than `D-2026-08-10` invariant 1, whose pairwise form says nothing
about a hop that re-widens after two narrowings. That ADR's topology half is superseded; its
invariants 2-4 hold, and a handoff being an ordinary tool call is what answers its objection that a
swarm loses the routing node where delegation is visible. It **ships off**, so `build_turn_graph`
returns `None` and a turn runs the same object it always did. `langgraph-swarm` stays declined:
it builds on `create_react_agent`, deprecated since LangGraph 1.0.

**The constraint that binds whoever adds delegation**: deepagents builds a bare `SubAgent` dict with
*only* `spec["middleware"]`, so anything not compiled by `build_langgraph_agent` runs with **no audit
trail, no authorization and no plan gate — silently.** What is still open is whether delegation pays:
`evals/delegation.py` has never run against a model, and the corpus that was meant to settle it
measured delegation *rate* over one-tool probes, which is a mediator rather than an outcome.

**Context and cost.** Compaction is `agent/compaction.py` — upstream's `ClearToolUsesEdit` for tool
results and a first-party conversation window, both non-destructive inside `wrap_model_call`, with
`chemclaw_context_compactions_total` making it checkable. The budgets are **billed**-token budgets,
converted by a ratio `agent/context_budget.py` measures from the provider's own `input_tokens` and
clamps so it can only tighten, and they charge the request's own prefix unconditionally — so
`agent_context_token_budget` bounds **request** spend, not thread spend. Read the arithmetic in
`core/config/agent.py` — both defaults are *derived* from `tests/test_context_floor.PREFIX_BOUND` plus the thread allowances `tests/test_compaction.py` holds, and the derivation is the thing to
read, never the result. The prefix floor is `tests/test_context_floor.py`: `CEILINGS["__default__"]` is the live ceiling, `PREFIX_BOUND` adds
`SERVED_ELSEWHERE_ALLOWANCE` for the bundles this repository does not serve and cannot watch, and the fixture must bind
connectors or it measures a smaller system than a turn runs. A turn's spend is bounded by `agent/spend_cap.py`,
counted in a `TurnTotal` channel so a fan-out shares one budget; `api/budget.py` meters around a
turn and cannot see inside one. Deferring connector tool schemas is designed and deliberately
unbuilt.

**Observability.** `CHEMCLAW_OTEL_LLM_SPANS` attaches OpenInference's LangChain instrumentation, so a
model call is a span carrying its token counts, model name and provider, with content suppressed by
default and `otel_include_sensitive_data` the one knob that decides it. Apache-2.0, plain OTLP, so
Arize Phoenix is a deployment choice rather than a dependency. **LangSmith is declined** — proprietary,
no OSS self-host, and its core value is prompt/response content in a third-party service, which four
merged decisions forbid. It is declined, not unreachable: `langsmith_tracing_allowed` exists and
`pin_langsmith_egress()` is what a deployment that overrides this has to go through.

**Identity and delivery.** Entra runs system-wide — front-door OIDC, one authorization gate,
`require_actor`'s reject-if-absent core rule, Temporal-mTLS — and is proven end to end against a real
JWKS with nothing patched; the one unproven hop is browser → tenant. OpenShift delivery is one
rootless image plus a Helm chart, Temporal self-hosted. The chart **refuses to render** until a
release states its egress posture and its retention posture, and `temporal.namespace` has no default
at all, because the broker is cluster-shared and a constant there put every environment on one
namespace, one task queue and one schedule-id space. **Two releases need separate databases**, which
no chart guard can check.

**Seams.** A connector is a directory with a `connector.yaml` (D-118); a data source is
`ingest/sources/<name>/datasource.yaml` plus its name in `CHEMCLAW_DATA_SOURCES`, with zero core
edits (D-120); a result sink is the third (`publish/`, schema in `schema/result-store/`, off until
`CHEMCLAW_RESULT_SINKS` names one) — a connector *produces*, a source *supplies*, a sink *consumes
what the system produced*. A warehouse ELN's schema and its database are both bindings in the
manifest: a `connection:` block is the driver's own keyword arguments, checked against its signature
offline. A mounted SMB/CIFS share is a source too — mounted rather than called, so no client, no
credential, no egress, and its AD group is an entitlement in the one role set every gate reads.

**There is no HPC tier and no DFT.** Every calculation is semiempirical — GFN2-xTB through tblite,
and CREST — and runs in its own pod (`Chemclaw3-mcp`'s `servers/calc`, `CHEMCLAW_CALC_SERVER_URL`),
never on a cluster. **When a decision turns on a difference inside GFN2-xTB's error bar, say so and
propose an experiment — there is no tier to escalate to.**

**Knowledge is written directly and corrected, not pre-approved**
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`). The PR-gate is gone and so is every module
behind it; there is no path in this tree that opens a pull request, and a rule phrased as "a human
opens the proposal" is describing a mechanism that no longer exists. The axis is whether a thing
changes what the agent *does*: knowledge does not, so it lands in `knowledge/` carrying
`created_by: agent`, readable beside its own citations. What makes that safe is provenance on every
retrieved chunk, the citations a chemist checks at the point of use, and contradiction
(`memory/failure.py`'s `contradicts`, `kg/conflicts.py`, `memory/supersede.py`, bi-temporal
`valid_to`). **A skill is the opposite case** and is refused outright in every tier: no agent path
writes a `SKILL.md` (`agent/skill_backend.SkillsReadOnlyRefusal`,
`agent/skill_store.PermittedStoreBackend`). **Which route changes a tier follows its blast radius**
(`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius`): `skills/` acts on every deployment
and changes only through a reviewed commit; the organisation's tier acts on every turn here and
changes through `POST /skills/org`, which takes the privileged role; a chemist keeps their own
through `POST /skills/mine`, where what makes it safe is blast radius rather than review — the
namespace closes over one actor. The agent's part is `propose_skill`, which writes a proposal and
never a skill, and an administrator promotes a body rather than reaching into anybody's queue. A
turn reads every tier and writes none. The stored tiers hold their own versions, so a bad
organisation-wide skill is reverted by naming a body the store already holds rather than by
re-authoring it. **No Temporal Schedule mines knowledge on a timer**: the
campaign, playbook and optimization miners run on demand.

**An ELN transcription is data, not a claim**, so it is readable the moment it is ingested:
`record_from_ord_reaction` infers nothing and hands a reviewer nothing to decide.

**Code execution.** There is no local shell and no `Bash` tool — `agent/scratchpad.py` withholds
deepagents' `execute` and `delete` verbs. That is not a rule against code execution: `pyexec` is a
sandboxed MCP server in the sibling fleet, reachable by naming it in a deployment's connector set,
and a turn runs Python through it. Propose a *local* exec path and it is refused; propose a second
sandboxed one and it is an ordinary connector question. There is no `WebSearch`/`WebFetch`, and that
one is the no-egress posture, which holds.

**Shapes upstream never promised.** `tests/test_upstream_surface.py` asserts every one of them in a
single file, each naming the module that would break, and some asserting an *absence* so upstream
fixing something turns the workaround red. **How many there are is that file's own length** — its
header says so, and this sentence used to state a count the file explicitly refuses to state. `session_store.message_from_row` is
the one function allowed to turn a `session_messages` row back into a message, and the shape stamp
has exactly one definition (`agent/message_migration.py`).

**Not in this system, and each absence is a decision rather than a gap**: a specialist *router* and a
challenge panel, `reject_widening`, workload-identity federation, OBO, the HPC identity bridge, the
audit hash chain, the Nextflow/Seqera launcher, `compute_dft_energy`. Re-adding any is a new
decision, and the ADRs that designed them stand.

**Live edges** — the things that need a real broker or cluster — are in `docs/planning/BACKLOG.md`.

**The design documents are historical, not current.** `docs/reference/architektur.md` is
pre-implementation: it is right about the four layers and wrong in its details, and its HPC/SLURM/
Nextflow and DFT-escalation prose describes a design retracted in full. Read it for intent; read
`docs/decisions/`, the package READMEs and `docs/guides/runbook.md` for what is true today.

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

Three rules the tree is arranged around. **Two are enforced by a test rather than asked for; the
third is a review rule, and saying so is the point** — "each enforced by a test" is what made the
middle one feel safe when nothing checks it:

- **`src/` is all the code.** Everything beside it is data, configuration or documents.
  (`tests/test_repo_map.py`, `test_no_import_package_sits_beside_data`.)
- **Capability code lives in a connector bundle or in `science/`, nowhere else.** The rule stands;
  what it covers shrank. `science/bo`, `science/fingerprints` and `science/labels` are still
  engines — pure
  computation, with the bundle as their durable-job and MCP wrapper, a pair rather than a
  duplication. `science/calc` no longer holds an engine at all: after
  `D-2026-08-16-the-physics-leaves-the-cache-stays` it is the cache, the calibration ledger, the
  RRHO/Crippen arithmetic and the models the Temporal wire carries, while the physics answers from
  `Chemclaw3-mcp`. **This one is reviewed, not tested.** `tests/test_layering.py` enforces import
  *direction* and `tests/test_third_party_layering.py` which stack a package may import; neither
  asks where a capability lives. The obvious derivable form — "only `connectors/*/` and `science/`
  may import `rdkit`/`bofire`/`tblite`" — was measured and is false today: `core/chem.py` and the
  ELN adapter and validator under `ingest/` import RDKit for structure handling that is
  infrastructure, so the rule would have to be written as an allowlist of its own exceptions, which
  is a policy nobody reads.
- **`data/` holds every corpus the code reads at runtime**, with three exceptions and no more.
  `knowledge/` and `skills/` stay at the root because they are architecture layers 4 and 3, not
  configuration. The third is `science/bo/benchmarks/data/`: package data pinned to a *registered
  benchmark's* name and read through `Path(__file__).parent`, where `data/vendored/` is a
  `DataSource` with a checksum and licence contract this corpus does not have. Enforced in both
  directions — `tests/test_deploy_chart.py::test_every_runtime_data_directory_actually_exists`
  says the declared ones exist, and `test_no_corpus_lives_outside_data_except_the_one_that_is_argued`
  says no fourth appears.

Four layers, each with a single responsibility. **Never merge their concerns.**

1. **LangGraph** — conversation orchestration + short reasoning steps, as one compiled graph per
   turn with a Postgres checkpointer under it.
2. **Temporal** — durable execution of long/expensive jobs: the semiempirical calculations
   (xTB/GFN2 single points, CREST conformer and complex searches, scans, rotational profiles) and
   BoFire BO. Queues: `background-jobs` for core's light work (sync, re-index, reports, the
   connector-job wrapper) plus one derived `connector-<name>` queue per bundle that owns durable
   work (D-118/D-150), so there is no second *core* queue.
   Every result is persisted once via the calculation store, and a *persisted* result is never
   recomputed (D-011). Concurrent misses on one key in one process share one computation
   (`cached_compute` single-flights them; `tests/test_store.py` drives 8 together → 1 compute);
   misses in *different processes* still each compute, and that cross-process half is a
   `docs/planning/DEFERRED.md` row with its own trigger.
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data + relations).

Durability lives **only** in Temporal, never in the conversation layer's own ad-hoc stores — the
rule is D-002's and it got *stricter* when layer 1 gained a checkpointer, because the checkpointer
holds turn state and every long or expensive job is still Temporal's (D-2026-08-10 §3). Skills hold
judgment; **connectors** hold capability (deterministic tools) — MCP is the protocol a connector
speaks, not the thing that holds the capability (D-110/D-118).

**Knowledge writes, the two skills tiers and the ELN transcription rule are stated once, above.**
What belongs here is only where they sit in the layering: knowledge is layer 4 and lands through
`kg/record.py`; a skill is layer 3 and no turn writes one. See `docs/reference/architektur.md`
§4, §9, §12 for the layers as originally designed, and
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` for what replaced the gate.

## Commands

The toolchain is scaffolded and `make help` (the default goal) lists every target — a count is not
written here, because the one that was said 23 while the file held 28. Use them rather than raw
invocations — CI runs exactly these, so a green `make` locally means a green CI.

- **The gate**: `make lint` (ruff lint + format) · `make type` (`mypy --strict`, every first-party
  package) · `make test` (pytest) · `make check` runs all three · `make cov` adds the coverage floor.
  **`make test PYTEST_WORKERS=4` is about twice as fast and is an opt-in rather than the default**
  (`D-2026-09-13-a-stable-failure-set-is-not-two-green-runs`): measured 18:13 → 09:30 on the suite and
  27:25 → 12:28 on `cov`, coverage unchanged — but two tests fail 2-in-5 and 1-in-5 in parallel and
  never serially, and a gate that reds for a scheduling artefact teaches everybody to re-run. So the
  gate is serial; four workers are for a local loop, and a failure under them is re-run serially
  before it is believed. `-n auto` is declined: every worker draws its own Postgres pool.
- **The validators**, each guarding a declaration against the live surface: `kg-validate`,
  `skill-validate`, `connector-validate`, `datasource-validate`, `sink-validate`,
  `channel-validate`,
  `template-validate`, `prose-validate`, `eln-validate`, `helm-validate`. Not counted here, and
  derived from the `ci` target by `tests/test_repo_map.py`: the count that used to open this list
  said eight over nine targets, and the one it omitted was the newest.
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
- **DRY**: no duplicate logic — extract shared code. `kg/record.py` and the retriever interface
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

**Two rules about what an ADR is for, because the record became something else**
(`D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision`):

- **A defect fix is a commit and a test, not an ADR.** Measured over the corpus, 60% of these files
  read as defect reports and 14% weigh an alternative, which is the defining content of a decision.
  Every one of them lands as a permanent, never-edited document stamped `accepted` whose *title* is
  a rule, and that is where the volume comes from and where a stale constraint hides. Write the
  finding in the commit message and the guard in a test. Write an ADR when a **choice between
  options** is being taken, or when something is being **declined**.
- **An ADR that declines a class of future work carries `Revisit when:`** — the condition that
  would make it worth reopening, in the shape `DEFERRED.md`'s "Trigger to revisit" column has
  always required. Without it a refusal never expires while a deferral does, and a refusal is the
  stronger word. `tests/test_declines_carry_a_trigger.py` holds it from its cursor forward. That is
  a bound on the trigger being *written*, not on anyone checking it: `D-092` stated a precise one
  ("revisit only if a deployment vendors the weight files into the container image at build time"),
  and it was met — in `Chemclaw3-mcp`, twice, by SHA-pinned build-time weight bakes that no reader
  of `D-092` was watching — while `DEFERRED.md` had separately rewritten one of the two blockers the
  trigger was written for. Nobody noticed either. **Make the trigger executable where you can**, and
  name the file that would show it had fired.

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
`pytest` skips every Postgres-backed test — the set that gates on `tests/pg.py::migrated_db_or_skip`
— and still prints a green line, so a change that breaks the durable layer, the session store, the
note-proposal tables or retention passes locally and fails in CI, and the session that trusted the
green line has already pushed. **How many that is, the run itself says**: `tests/conftest.py`'s
terminal epilogue counts them and names what the run is therefore not evidence about. A count is
not written here, because the one that was said ~157 while the suite skipped 216 — stale by ~38%,
in the direction that understates the risk it exists to warn about
(`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`). The same belief makes the live lane
(`make live-infra`, `make live-up`, `make live-probes`) and the four-repo
`infra/live/e2e-full-stack/up.sh` look impossible when they are not.

**So: start the daemon before claiming anything about the suite, and never report a local run as
green without saying what it skipped.**

## Local live/e2e credentials

Some Claude Code Remote environments for this repo carry a working Anthropic credential for the
live lane (`infra/live/`, `infra/live/e2e-full-stack/`) as an environment variable literally named
`API-KEY` — hyphenated, so it is not `$`-referenceable in bash and has to be read with
`printenv 'API-KEY'`. Never print or commit the value itself — this note records where to look,
not what it is, and it may not exist in every environment.

**What to map it onto is `CHEMCLAW_LLM_API_KEY`, and only beside a gateway**
(`D-2026-09-04-a-gateway-is-the-only-provider`). Nothing in `src/` dials a vendor any more, so a
bare `ANTHROPIC_API_KEY` is not a credential this stack can use: every model call goes to the one
OpenAI-compatible endpoint `CHEMCLAW_LLM_BASE_URL` names. Set that plus `CHEMCLAW_LLM_MODEL` to a
gateway fronting the vendor and the key belongs on `CHEMCLAW_LLM_API_KEY`;
`infra/live/e2e-full-stack/up.sh` does exactly that mapping, and only when a base URL is named.
Name no gateway and the lane runs against `chemclaw.cli.mock_llm` on loopback, which needs no
credential at all — so the key is what `make live-probes` needs, not what the lane needs to start.

## Governance

Treat this file like code: version it, review changes in a PR, and re-test it in a fresh
session before merge. Do not duplicate anything already in `README.md` or a package manifest.
