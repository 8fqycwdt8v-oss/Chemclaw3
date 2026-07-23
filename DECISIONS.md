# DECISIONS

Architecture decisions with rationale. Append-only; newest last. See `docs/architektur.md`
for full context (referenced section numbers).

## D-001 — Runtime is Python
MAF `SkillsProvider`, the Temporal SDK, and RDKit are all Python-native. One language across
orchestration, workflows, and cheminformatics avoids a polyglot seam.

## D-002 — MAF for orchestration, Temporal for durability (kept separate)
MAF orchestrates the conversation and short reasoning steps; Temporal owns the lifecycle of
long scientific jobs (QM/DFT). Merging both durability models is explicitly avoided ("a
torturous path"). MAF ↔ Temporal integration is one thin DIY adapter (no official adapter
exists), not a framework. §1, §2, §15.

## D-003 — Agent Skills (SKILL.md) for capability integration
Progressive disclosure keeps context lean vs. loading many MCP tools at once. Skills = domain
judgment ("when/how"); MCP servers = deterministic capability ("do X"). §3, §12.3.

## D-004 — Knowledge as a Markdown + Git graph (NetworkX), not a graph DB
Frontmatter makes notes structured-queryable; wikilinks encode real chemical relations;
retrieval is graph traversal (1–2 hops), not top-k vector similarity. Git gives versioning +
audit trail. No Neo4j/dedicated graph DB. §4, §10.

## D-005 — Human-in-the-loop via PR-gate
Every `created_by: agent` note (job results, campaigns, playbooks, report drafts) lands on a
branch/PR and needs human approval before merge. Built once, reused everywhere. §4.

## D-006 — One execution system: Temporal task queues, no pg-boss
Since Temporal already runs for HPC jobs, small async jobs (ELN sync, re-index, notifications,
reports) use a separate `background-jobs` task queue instead of a second queue system. §12.1.

## D-007 — First milestone: MAF + Temporal spine (HPC mocked)
Prove the async, durable job path end-to-end before building the rest; everything else hangs
off this pattern. `submit_to_hpc` is mocked so durability is testable without SLURM. Plan Phase 1.

## D-009 — Evaluation/metrics layer is first-class (Phase 2b)
The external review (docs/research-review.md) showed tool augmentation is not uniformly
beneficial (F8/F9) and that reproducible agent evaluation needs concrete benchmarks plus
green-chemistry metrics (F7). So scientific-output quality gets its own cross-cutting layer
(metric interface + eval harness + per-task tool-value A/B), and every later capability phase
must register ≥1 metric. This is what lets us apply Skills/tools *selectively and measured*
rather than universally. Chemical/biological safety is a *separate* concern and stays in the
backlog (user decision), not part of this layer.

## D-008 — Deep-research/report harness: one core, pluggable retrievers
The synthesis engine (decompose → fan-out → adversarial-verify → cite → synthesize) is
source-agnostic; internal sources (graph, fingerprints, ORD/analytical data, TabPFN) and later
external literature are interchangeable retrievers behind one interface. Long runs are Temporal
background workflows; drafts are PR-gated. Plan Phase 5b.

## D-010 — HPC/DFT deferred; lead with fast local calculators (user decision)
The real HPC/SLURM DFT path is postponed. The mock spine (Phase 1) already proves the durable
async pattern, so early value comes from **fast, locally runnable** compute instead: semiempirical
**xTB (latest GFN, GFN2)** and ML predictors (**GNN solubility**, **pKa/property**). They reuse the
identical Temporal durability pattern; only the heavy HPC/DFT backend is wired later, when that
accuracy is actually needed and HPC access exists. Plan Phase 1c; DEFERRED.md row for HPC/DFT.

## D-011 — Results are persisted once, never recomputed (calculation store, first-class)
Every calculation goes through **one** result store keyed by
`(calc_type, calc_version, input_hash, params_hash)` — the calculator version is in the key so a
model/method update cannot silently poison the cache. One interface, swappable backend
(in-memory for tests, Postgres for real). This generalizes the QM-only step 1.10 into a
cross-cutting layer every calculator and every BO objective evaluation shares (DRY, no per-calc
cache). Plan Phase 1b.

## D-012 — BoFire is the Bayesian-optimization engine (no in-house BO), pulled forward
Optimization campaigns use the fast predictors + store as objective evaluations. We adopt
**BoFire** (domain modelling + BoTorch strategies) behind a thin adapter rather than building our
own BO; BoFire types stay encapsulated and never leak into the agent/skill. BO is pulled forward
from "defer until measured" because it drives which calculations are worth running. Plan Phase 1d.

## D-013 — MAF stays the orchestrator (reaffirmed vs. LangGraph)
Reconsidered MAF vs. LangGraph explicitly. LangGraph's main edge (durable/checkpointed execution)
is largely moot here because durability lives in Temporal (D-002); MAF's native Agent-Skills
(SKILL.md progressive disclosure) and Entra/Azure fit are load-bearing for our design. The agent
layer is kept thin and framework-swappable, bounding MAF's maturity risk. Decision: keep MAF.

## D-014 — Eval cases live outside the knowledge graph (own versioned dir, not notes)
Phase 2b's eval case-set is versioned in Git (reviewable, cited by the report) but lives under its
own `eval_case_dir` (default `evals/cases`), **not** under `knowledge_dir`. Reason: an eval case is
a structured evaluation payload (`output`/`reference` masses, predicted/actual, optimum), which the
relational note schema (`kg/note.py`: id/type/links/…) cannot carry, and putting such files under
`knowledge_dir` would make `kg-validate` reject them as malformed notes. So the metric layer parses
eval-case frontmatter directly instead of through `kg.note`. Regression gating is done by the test
suite (which pins each case's expected pass/fail), not by a CI hard-gate — because the seed set
deliberately contains a case that *fails* its gate to prove gating works. Plan Phase 2b.

## D-015 — Calculator contract now (`run_cached`), name-registry deferred
With three calculators sharing the same skeleton (xTB, solubility, pKa), the Rule of Three is
met, so the shared **contract** is extracted: `calc.store.run_cached` is the one place that
offloads a blocking calculator, stores the result as a plain dict, and reconstructs the typed
model — each `run_cached_*` now only derives its versioned key and delegates (DRY, plan 1c.1).
The **name→calculator registry** half of 1c.1 is deliberately *not* built: nothing dispatches a
calculator by name yet (the agent tools call each wrapper directly, and `bo.objectives` has its
own name registry). Adding a second registry now would be an abstraction with no second caller
(KISS) — it lands when a real name-dispatch consumer appears (e.g. a generic calc activity).

## D-016 — MCP capability servers live in `mcp_servers/`, not `mcp/`
The plan named the capability-server directory `mcp/`, but that package name is taken by the
installed MCP SDK (`from mcp.server.fastmcp import FastMCP`). A local top-level `mcp/` package
shadows the SDK on `sys.path`, so `mcp.server` becomes unreachable and no FastMCP server can be
built. The directory is therefore `mcp_servers/`. This is a naming-only deviation from the plan;
the responsibility (deterministic capability, one small server per concern) is unchanged.

## D-017 — One generic fingerprint store for molecules and reactions
Reactions (DRFP) are the second fingerprint domain after molecules (ECFP4), so the
Rule of Three fired and the Tanimoto ranking, the record/Match types, the store Protocol,
and both backends (in-memory + Postgres) live once in `mcp_servers/fpstore.py`. A record is
a neutral `(id, label, bits)`; each domain supplies only its fingerprint function, its table
name, and its bit width (constructor params, both trusted constants). This mirrors the
calculation store (D-011): one ranking contract, swappable backend, no per-domain copy. The
molecule table column was renamed `smiles → label` to match (greenfield, CI recreates the DB).

## D-018 — ELN ingestion: ORD-subset schema, one JSON adapter, LLM-per-field deferred
Phase 4 keeps the canonical target schema (`eln/ord.py`) a **pragmatic subset** of the ORD
proto — only the fields Chemclaw consumes (structure, roles, amounts, headline conditions,
yield, provenance) — so there is no speculative schema and nothing above the adapter knows any
ELN's shape (G6). One concrete adapter is built (`JsonExportAdapter`, for a JSON-exporting
ELN), not a universal abstraction (generalize only from a third source — DEFERRED). Free-text
condition recovery is deterministic regex for the common cases; the **per-field LLM fallback**
(plan 4.4) is documented as judgment in the `eln-reaction-extraction` skill but not wired in
code — it needs a live model and is non-deterministic, so it stays out of the tested pipeline
until a real ELN needs it (same discipline as other LLM/infra-dependent deferrals). Ingestion
splits cleanly: the fingerprint index is a deterministic serving copy (not gated); the reaction
note is a knowledge claim (PR-gated, D-005).

## D-019 — Memory layers add no new infrastructure (note types + jobs only)
Phase 5's episodic (`campaign`) and semantic (`playbook`) memory reuses what exists: structural
identity comes from the fingerprint index's canonical-SMILES compound ids (Phase 3), the reaction
source is the ELN adapter (Phase 4), and every synthesized note enters through the one PR-gate
(Phase 2). Chain detection (`memory/chains.py`) links a product of one reaction to a reactant of
another; a chain of ≥2 becomes a `campaign` note citing its members. Cross-project structural
recurrence (`memory/playbook.py`, DRFP similarity across ≥2 projects) becomes a `playbook` note
with mandatory evidence. No new store, table, or queue — only new note types + background jobs on
the existing background-jobs queue. The LLM narrative/distillation prose stays in the two skills
(judgment), layered on the deterministic, tested skeletons.

## D-020 — Report harness reuses retrievers over existing data (no new store)
Phase 5b's report/deep-research harness turns the deep-research pattern (decompose → fan-out →
verify → cite → synthesize) inward onto internal notes. A stable, source-agnostic core
(`report/harness.py`) knows only the `SourceRetriever` contract; concrete retrievers
(`report/retrievers.py`) are thin adapters over the knowledge graph (Phase 2) and reaction
fingerprint search (Phase 3) — no new data store, and a future source (analytics, external
literature) is just another retriever behind the same interface. Citation is mandatory
(`EvidenceChunk.source_note_id`), unsupported claims are discarded (`verify_claims`, guarding the
`citations and all(...)` empty-list trap), unsupported sections are marked not invented, each
section declares its memory layer (structural provenance separation), long reports run as a
durable per-section Temporal workflow, and the draft is PR-gated. The decompose/synthesize prose
is the `development-report` skill's judgment on the deterministic, tested core.

## D-021 — Production-readiness review: one bad-data contract, hardened PR-gate
The whole-repo review (post-5b) fixed systemic issues rather than adding features. (a) All
bad-input errors (`FingerprintError`, `ElnMappingError`, `IngestError`, `MetricError`,
`PlaybookError`, `NoteError`) now derive from one `chemclaw.errors.ChemclawError(ValueError)`:
reject-and-continue boundaries catch the base instead of enumerating types — forgetting one had
turned a single degenerate ELN entry into a batch-aborting poison pill. It stays a `ValueError`
so Temporal's fail-fast-on-bad-data retry policy keeps applying; the shared policy and the
note-publish discipline live once in `workflows/publish.py`. (b) The git PR-gate submitter is
hardened: submissions serialize through a lock (checkout -B switches the whole tree), the
checkout is `note_repo_dir` config (a dedicated clone in production), note ids/types are
slug-constrained at the model (ELN-derived ids reach file paths and git refs), and the note
branch is fetched before `--force-with-lease` so re-proposals from fresh clones push. (c) ELN
mass balance is downgraded to element-set subsumption: without stoichiometric coefficients a
per-molecule count comparison falsely rejects dimerizations, so the sound necessary condition is
"no product element absent from the inputs". (d) Store factories (`default_molecule_store`/
`default_reaction_store`) pair table name and bit width once, and the pKa cache key now embeds
the tblite version like xTB's (an engine upgrade is a cache miss, not a stale hit, D-011).

## D-022 — ELN carries step-by-step recipes; a second adapter reads native ORD
A late-development record is a *procedure* (charge → cool → dropwise addition → age → quench →
extract → crystallize), not one set of headline conditions. The canonical `OrdReaction` gained an
ordered `steps` list (`ReactionStep`: kind, verbatim text, optional components + per-step
temperature/duration) plus `procedure_text`, mirroring ORD's `inputs`(`addition_time`/`order`) +
`conditions` + `workups[]`. The flat headline fields stay the summary every existing consumer
reads; `steps` is a purely additive overlay that never feeds the reaction SMILES / fingerprints,
so search and metrics are untouched. Mass balance folds step-added species into the input element
set (a workup reagent can legitimately supply a product element). Two ingestion paths now feed the
one schema: `eln.json_adapter` segments free-text prose into labeled steps (lossless — text kept
verbatim, no SMILES guessed from prose; that stays the LLM skill's job), and `eln.ord_adapter`
maps native Open Reaction Database JSON into **component-linked** steps with unit conversion,
tolerating snake_case and camelCase. Both satisfy the one `ElnAdapter` contract and flow through
the same `sync_entries` pipeline; the reaction note now renders the numbered procedure so the
recipe survives to the graph for human sign-off.

## D-023 — The agent is the research surface; integrations stay dumb
The chat agent — not any single integration — is where intelligence lives. Data sources (ELN
free-text, native ORD, future analytics/literature) only map their content into the canonical
schema and the graph; the agent composes **every** tool and source to answer open-ended
questions and to propose new chemistry. Three moves:
(1) The fingerprint capabilities are now agent tools (`agents/search_tools.py`:
`find_similar_reactions`/`find_similar_molecules`/`find_substructure_matches`) — structural
cross-learning ("what was tried for this transformation", "what do we know when this functional
group is present"), previously built but unexposed.
(2) `agents/research_tools.py:gather_evidence` sweeps every internal source in one call behind
the report harness's `SourceRetriever` contract (graph over all note types ∪ reaction-fingerprint
search), returning note-cited chunks; adding a source later is one retriever in
`_text_retrievers`, no agent change. The `deep-research` skill holds the method: decompose any
question (any output — yield, impurities, observations — or general protocol guidance), gather
across similar *and* transferable-principle notes, keep evidenced fact separate from analogy, and
draft new conditions/protocols as PR-gated `protocol` notes (never asserted until a human merges).
(3) An **optimization campaign** (`memory/optimization.py`) is a new episodic grouping: repeated
runs of the *same* transformation (DRFP-similar, tight threshold), laid out as a comparative
conditions×outcomes table citing each run — the substrate for "what moved the result". The DRFP
clustering that playbook and optimization now share is extracted to `memory/similarity.py`
(Rule-of-Three). The memory corpus reads from **all** ELN adapters, not just the free-text one.

## D-024 — The agent computes and designs experiments proactively, not just retrieves

**Decision.** Two capability gaps found while checking whether the agent behaves autonomously
are closed, and a token-frugality bound is added:

(1) *Proactive property computation.* The fast calculators (`predict_solubility`, `predict_pka`,
`compute_xtb_energy`) were already agent tools; the instructions and `deep-research` skill now
tell the agent to invoke them *unprompted* when a question turns on a property the record does
not state — e.g. weighing an untried solvent against the ones in the ELN — folding the
prediction (with its uncertainty) into the answer instead of leaving the gap.

(2) *Next-experiment design.* BoFire existed only as the durable `BoCampaignWorkflow` (an
automated closed loop). A "which experiment/condition next?" question is a single ask, not a
campaign, so `agents/bo_tools.py:suggest_next_experiment` exposes BoFire's ask step inline (GP
fit off the event loop, like the calculators): the agent frames the decision space + the
historic runs it gathered and gets the next point(s) to try — proposals a human runs, gated as
`experiment-batch` notes if recorded. Judgment lives in the new `experiment-design` skill; the
neutral `bo.problem` types cross the boundary, never BoFire (G6). The durable workflow remains
the path for a self-evaluating multi-round loop. **TabICL/TabPFN stays deferred** — it needs a
model download + license check, and BoFire covers the design question today.

(3) *Context-window budget.* `gather_evidence` caps its sweep at
`gather_evidence_max_chunks` (config, default 40) so a broad question over a large corpus fills
only as much context as it needs; the agent narrows the query or drills in with `expand_note`
when truncated. This complements the two existing frugality mechanisms — bounded excerpts
(`report_excerpt_chars`) and offline memory-synthesis jobs that pre-digest many runs into one
comparative `optimization-campaign` note, so the agent reads a distillation instead of N raw
recipes.

`examples/research_demo.py` demonstrates the whole loop (gather → cross-learn → proactive
compute → next experiment) over a seeded in-memory corpus with **no LLM and no database**, and
is covered by `tests/test_research_demo.py`.

## D-025 — The agent keeps its chat thread within a token budget (MAF compaction)

**Decision.** The MAF agent now carries an `InMemoryHistoryProvider` (so a session accumulates a
thread) and a `CompactionProvider` that keeps that thread within a configurable token budget —
built in `agents/chemclaw_agent.py:_build_compaction`. Compaction fires **only when the included
context exceeds `agent_context_token_budget`** ("reduce when applicable"), then reclaims tokens
cheapest-first via a `TokenBudgetComposedStrategy`:
1. `ToolResultCompactionStrategy` — collapse older tool-result payloads (the big `gather_evidence`
   sweeps and full `expand_note` recipes) into a short cited `[Tool results: …]` trace, keeping the
   newest `agent_keep_last_tool_groups` verbatim.
2. `SlidingWindowStrategy` — drop conversation turns older than `agent_keep_last_conversation_groups`.
3. The composed strategy's built-in fallback excludes the oldest groups if still over budget.
System instructions and skills are always preserved. The same strategy runs `before_run` (guard the
model input) and `after_run` (shrink persisted history so the next turn starts smaller).

**Why this shape.** (a) Tool results are Chemclaw's largest context consumers, so collapsing them
first is the highest-value, cheapest move and keeps a readable, still-cited trace. (b) **No LLM
summarizer** — the char/4 `CharacterEstimatorTokenizer` and deterministic strategies need no extra
credentials, stay reproducible/testable, and avoid the indirect-prompt-injection risk MAF documents
for `SummarizationStrategy` (a compromised summarizer would persist unsafe text in history). (c)
Durability stays in Temporal — this is conversation-context management, not job state (layer rule
intact). Knobs live in the one config source (`CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET`,
`…_KEEP_LAST_TOOL_GROUPS`, `…_KEEP_LAST_CONVERSATION_GROUPS`). This complements the existing
per-answer frugality (capped `gather_evidence`, sized excerpts, offline distillation into campaign
notes). `SummarizationStrategy` remains a documented opt-in (DEFERRED).

## D-026 — Observability floor: config-driven logging + one clear DB-connect failure

**Context.** An admin audit of configurability/error-handling/logging found the app emitted
essentially **one** log line: workers started silently, an ELN sync's rejections lived only in
the returned summary, broken export files were dropped with no signal, and an unreachable
Postgres surfaced as a raw psycopg traceback that never said which database or why.
Troubleshooting meant reading the Temporal UI and guessing.

**Decision.** Add the smallest high-value observability floor, all config-driven:
1. **One logging switch** — `chemclaw/logging.py::configure_logging()` wires the stdlib root
   logger from `CHEMCLAW_LOG_LEVEL` + `CHEMCLAW_LOG_FORMAT` (idempotent, `force=True`), called
   at each worker's entrypoint. Modules just `logging.getLogger(__name__)`; no module configures
   logging itself. Verbosity is an ENV change, not a code change.
2. **Worker startup logs** — each worker logs its connected address / namespace / queue and its
   registered workflows (+ activities for the HPC worker). The HPC worker's registration lists are
   hoisted to module level so the log and the `Worker(...)` share one source (DRY), mirroring the
   background worker.
3. **ELN sync trail** — `eln.sync.sync_entries` logs `ingested=N rejected=M` at INFO and one
   WARNING per rejected entry (id + reason), so a scheduled run is diagnosable without opening the
   workflow result. The broken-file skips in both adapters (`json_adapter`, `ord_adapter`) — which
   can never reach the sync report — now log a WARNING naming the dropped file.
4. **One clear DB-connect failure** — `chemclaw/db.py::connect(dsn)` is the single Postgres connect
   (used by the calculation store and the fingerprint store, DRY). It applies the configured connect
   timeout and turns `psycopg.OperationalError` into `ConnectionError("Postgres unreachable at
   <host>: <cause>")` with the **DSN password redacted**. It is deliberately **not** a `ChemclawError`
   (a `ValueError`, which Temporal treats as non-retryable bad data): an unreachable database is a
   transient infra fault, so the activity should retry.

**Why this shape.** It is the cheapest change that makes the system troubleshootable, and it stays
inside the existing rules — one config source, DRY seams, no new dependency (stdlib `logging`, no
OpenTelemetry/structured-logging yet). The MAF function-middleware tool-audit trail and an OTel
toggle are the natural next tiers on top of this floor (see BACKLOG P1/P2), not part of it.

## D-027 — GxP tool-audit middleware + opt-in OpenTelemetry (MAF out-of-the-box)

**Context.** With the logging floor in place (D-026), the two natural next tiers from the MAF
feature analysis were: a per-tool audit trail (a GxP "who ran what, with which inputs, did it
succeed" record and the first thing needed to debug an agent turn), and distributed tracing.

**Decision.**
1. **One function middleware audits every tool call.** `agents/audit.py::audit_tool_calls` is a
   MAF `@function_middleware` attached once via `Agent(..., middleware=[audit_tool_calls])`. It
   logs one line per invocation — tool name, truncated arguments, outcome, wall-clock latency —
   at INFO on success and WARNING on failure, re-raising the original exception unchanged
   (observe-only: it never edits arguments or results). This is the audit trail as a single
   reusable piece over all ~13 tools (DRY), not per-tool logging. Argument size is bounded by
   `agent_audit_max_arg_chars` so a large payload can't flood the log.
2. **OpenTelemetry is an opt-in toggle, not a forced dependency.** `chemclaw.logging.
   configure_telemetry()` is a no-op unless `CHEMCLAW_OTEL_ENABLED=true`; when on it calls MAF's
   `configure_otel_providers` once (reading the standard `OTEL_EXPORTER_OTLP_*` env vars) at each
   worker's entrypoint. The OpenTelemetry **SDK + OTLP exporter are not installed** (only the API
   is, transitively), so enabling it requires an admin to add those extras — the toggle raises a
   directive error if they are missing, rather than us vendoring heavy tracing deps with no
   collector to receive them (KISS / "no dependency without a real consumer").

**Why this split.** The middleware is the high-value, zero-new-dependency deliverable and works
today; OTel is genuinely useful but only with a collector, so it ships as a config-flagged
capability an admin turns on deliberately. Structured/typed agent outputs (`response_format`) —
the third MAF-analysis pick — stays open in BACKLOG; it changes call sites, not startup wiring,
so it belongs with the feature that first needs a validated payload.

## D-028 — Admin pluggability: ELN adapter registry, multi-dir skills, cache-trace log

**Context.** The admin audit's P1 findings: adding/switching an ELN source or a skills directory
meant editing code (the durable sync hardcoded `JsonExportAdapter()`, the memory jobs hardcoded
`[JsonExportAdapter(), OrdJsonAdapter()]`, `skills_dir` was a single string), and "why did this
recompute?" had no answer at the cache boundary.

**Decision.**
1. **One ELN adapter registry** (`eln/registry.py`): `ELN_ADAPTERS` maps a stable config name to
   each `ElnAdapter`. `make_eln_adapter(name)` picks one (clear error listing valid names);
   `all_eln_adapters()` returns the whole set. The durable sync's source is now
   `CHEMCLAW_ELN_SYNC_ADAPTER` (it tracks one high-water cursor, so it runs a single source — the
   deliberate deferral of running both under one cursor stands), and the memory jobs read
   `all_eln_adapters()` (the corpus is the union of every source). Adding a source is one registry
   entry, nowhere else — replacing the class names previously hardcoded in two workflow modules.
2. **Multi-directory skills** (`Settings.skills_dirs`): `CHEMCLAW_SKILLS_DIR` is now an
   OS-path-separator list (like `PATH`, e.g. `skills:/opt/team-skills`), read through the
   `skills_dirs` property that `FileSkillsSource` already accepts. An admin adds a second
   (e.g. team-private) skills directory with no code change and no JSON-in-env quoting. The
   SKILL.md front-matter schema + a template are now documented in `skills/README.md`.
3. **Cache-trace log**: `cached_compute` logs hit-vs-miss at DEBUG with the flat calculation key,
   the one place that answers "why did this recompute?" (behind the D-026 log-level switch).
4. **Runbook** (`docs/runbook.md`): the four recurring admin tasks (add a skill / add-or-repoint a
   DB / add-or-switch an ELN source / add a capability) + the troubleshooting surface (log switch,
   Temporal UI :8080, the DB-unreachable message).

**Why this shape.** Each change is a config switch over an existing seam — no new abstraction
without a real second caller (the registry genuinely serves both the pick-one sync and the
read-all memory jobs; the `skills_dirs` property has one consumer but matches the framework's
list signature and the audit's explicit ask). KISS/DRY intact, one config source, no new deps.

## D-029 — The agent consumes fingerprint search over MCP (config-driven servers)

**Context.** The FastMCP servers in `mcp_servers/` (molfp, rxnfp) existed but the agent used
their capability *in-process* (`agents/search_tools.py` imported the search functions), so the
servers were dead relative to the agent path and "add a capability" meant editing agent code —
the gap the admin audit flagged and the architecture doc's "MCP servers hold capability" line
called for.

**Decision.** `build_agent` attaches each configured MCP server as a MAF `MCPStdioTool`
(`_mcp_capability_tools` over `settings.mcp_servers`, a list of `McpServerSpec`), so the agent
reaches structural search (`similar_reactions`, `similar_molecules`, `substructure_matches`)
over the MCP protocol. Adding/replacing a capability is a `CHEMCLAW_MCP_SERVERS` entry (JSON,
ENV-overridable), never a change to `build_agent`. `allowed_tools` restricts the agent to each
server's read/search tools — the `index_*` write tools stay off the conversational agent
(ingestion writes go through the PR-gate). Construction is lazy (no subprocess spawned in
`build_agent`, which stays synchronous); the run harness owns the MCP lifecycle
(`async with *agent.mcp_tools: await agent.run(...)`).

**Trade-offs accepted (the KISS tension, chosen deliberately by the user).** MCP transport adds
a subprocess boundary and per-turn lifecycle for what were local RDKit functions, and it moves
the in-process store test-seam out of reach for the agent path. Mitigations: (a) tool
*discovery* over stdio needs no database, so `tests/test_mcp_transport.py` spawns each real
server and asserts it advertises exactly its `allowed_tools` — the transport + config wiring is
verified in-sandbox; tool *invocation* stays covered by the Postgres-backed server tests in CI.
(b) `agents/search_tools.py` and its in-process functions are **kept** for `examples/
research_demo.py` (a deliberately credential-/DB-free in-process walkthrough) and their unit
tests — not dead, but no longer the agent's path. This duplication (in-process capability +
MCP transport) is the cost of the walkthrough staying runnable without Postgres/subprocess.

## D-030 — Deep-review hardening: bounded retries, git-ref-safe slugs, git timeouts, cache keys

**Context.** A full-codebase review (six parallel review passes, findings independently
verified) rated the architecture and compute core clean but surfaced one concentrated risk
class in the Temporal retry/error-classification policy plus a few lower-severity robustness
and correctness gaps.

**Decision — fixes applied.**
- **Bounded bad-data retries (HIGH).** `workflows.publish.BAD_DATA_RETRY` had no
  `maximum_attempts`, so any exception whose class name was *not* in the non-retryable list
  (e.g. a deterministic `KeyError`/`RuntimeError`, or a git ref that can never be created)
  retried forever and pinned a worker. It now sets `maximum_attempts=settings.activity_max_
  attempts` (default 5) — bad data stays non-retryable by type, transient faults get bounded
  retries. The type list gained `ValidationError` (pydantic's `ValueError` subclass, matched
  by its own class name), `OrdFormatError`, and `EvalCaseError`; `note_publish_retry` now
  shares the same list (DRY) so a bad note fails fast instead of burning its retry budget.
- **Git-ref-safe note slugs (HIGH, composes with the above).** `kg.note.Note` accepted ids
  ending in `.` or `.lock`, which pass the slug schema but make git reject the `note/<id>`
  branch — a `GitSubmitError` that (pre-fix) retried unbounded and wedged the ELN sync. The
  slug validator now rejects a trailing `.` and a `.lock` suffix at the model.
- **Git subprocess timeout + kill (MEDIUM).** `GitNoteSubmitter._run` now bounds every git
  command by `settings.git_command_timeout_seconds` (default 60) and kills the child on
  timeout/cancellation, so a hung fetch/push can never deadlock the process-wide submit lock
  or orphan a git process holding `.git/index.lock`. `CancelledError` still propagates.
- **Cache keys include reported uncertainty (LOW).** The solubility and pKa calculation-cache
  keys now version on `solubility_rmse_log` / `pka_uncertainty`, so re-tuning the reported
  uncertainty recomputes rather than serving the stale value (the point estimate was already
  correctly keyed).
- **Test-skip narrowed (MEDIUM, test).** `tests/test_mcp_transport.py` skipped on a bare
  `except Exception`, which in CI could mask a real regression of the `allowed_tools` boundary
  (the D-029 line keeping write/index tools off the agent). It now skips only on a genuinely
  absent toolchain (`FileNotFoundError`/`ImportError`); anything else fails loudly.

**Consciously deferred (with reason).**
- **ELN reject re-drive.** The sync cursor advances past *rejected* entries (deterministic bad
  data — re-fetching only re-rejects). Rejections are reported in the summary and logged, not
  retried; correcting a source record upstream and re-ingesting is a manual/backlog action. A
  dead-letter/re-drive mechanism is over-engineering at current volume (KISS). Documented in
  `eln/sync.py`.
- **Fingerprint-definition versioning.** `molecule_fingerprints`/`reaction_fingerprints` store
  no record of the `ecfp_radius`/`ecfp_bits` that produced a row, so changing the definition
  and re-indexing alongside old rows would silently compare mismatched features. Latent (needs
  a config change *and* a re-index). Trigger to fix: the first time a second fingerprint
  definition is introduced — add a definition signature to the row + search guard (one
  migration). Tracked in `BACKLOG.md`.
- **KISS cleanups** (`gather_report`, `note_from_confirmed_answer`, `StoredResult.provenance`,
  the single-implementer `SolubilityModel` seam): left in place — each is plan-anticipated
  future wiring or a public batch API, not obvious boilerplate; deleting blindly is riskier
  than tracking. Listed in `BACKLOG.md` as conscious cleanup for the next touch.

## D-031 — Deep-review deferred items worked off: fp-definition guard, ELN re-drive, KISS cleanups

**Context.** D-030 deferred three items with documented reasons; this closes all three.

**Decision — done.**
- **Fingerprint-definition guard (was latent LOW).** Every fingerprint row now records the
  *definition* that produced its bits (`ecfp:r{radius}:b{bits}`, `drfp:b{bits}` — from
  `molecule_definition()`/`reaction_definition()`), and similarity search returns only rows
  matching the store's current definition. Equal-width bits of a different Morgan radius are
  incomparable; the width check (`bit(N)`) can't catch that, but the definition filter does —
  after a definition change, stale rows fall out of similarity search (safe: no wrong scores,
  just missing hits) until re-indexed. The durable `PostgresFingerprintStore` takes the
  definition as a constructor arg and filters in SQL; the ephemeral `InMemoryFingerprintStore`
  filters only when explicitly bound to a definition (it can't accumulate mixed definitions, so
  the default is unfiltered — this also makes the guard testable without Postgres). Migration
  `004_fingerprint_definition.sql` adds the column (002/003 carry it for fresh DBs). Substructure
  search stays unfiltered by design — it re-matches the stored SMILES with RDKit and never
  touches the bits, so a stale-definition row is still a correct substructure hit. Runbook (vi).
- **ELN reject re-drive (was MEDIUM).** `RejectedEntry` now carries the entry's `created_at`,
  and the rejection WARNING logs it — the exact `since` an admin re-runs the sync from to
  re-ingest a corrected entry. The re-drive capability already existed (the sync is re-runnable
  from any earlier cursor; ingestion is idempotent); this makes each rejection self-describing.
  No dead-letter/automatic re-drive was built (KISS — deterministic bad data shouldn't retry
  itself). Runbook (v) documents the procedure.
- **KISS cleanups.** (a) Inlined the single-implementer `SolubilityModel` seam: removed the
  Protocol, the never-passed `model=` param on `predict_solubility`/`run_cached_solubility`, and
  the `_DEFAULT_MODEL` indirection — `EsolBaseline` is now called directly (reintroduce a seam at
  the second model, Rule of Three). (b) Deleted `report.harness.gather_report` (no production
  caller — the report workflow assembles the `Report` itself, per-section, for durability); its
  three tests now assemble via a local `_gather` helper over `gather_section`. (c) Wired
  `memory.interaction.note_from_confirmed_answer` (was implemented+tested but unreachable) into a
  new agent tool `record_confirmed_answer` (`agents/memory_tools.py`) that routes a
  chemist-confirmed answer through the PR-gate — completing plan step 5.5's "user interaction as
  the fourth memory source" instead of deleting it. (d) Kept `StoredResult.provenance` after
  review: it is accurate GxP audit metadata (every value in a compute cache *is* "computed"), not
  a dead stub; docstring clarified that it is audit trail, not a control signal (no code branches
  on it), and the seam for a future `provenance="measured"` value under the same key.

**Result.** `make lint type test` green: 229 passed / 16 skipped (sandbox-infra only). New/moved
tests: the in-memory definition-exclusion guard, the `record_confirmed_answer` gate test, and the
retargeted report gathers.

## D-032 — Durable async approval hold for captured user answers (Yes/No button seam)

**Context.** `record_confirmed_answer` (D-031) proposes an interaction note synchronously,
inside one agent turn. A chat "save this knowledge? [Yes]/[No]" affordance is *asynchronous*:
the human may click minutes later, after the turn or session has ended, so the pending
candidate must outlive the conversation. The architecture rule is that durability lives only in
Temporal, never in MAF — so the pending state cannot sit in the agent's in-memory session.

**Decision.** Added `workflows/interaction_approval.py`: `InteractionApprovalWorkflow` holds one
candidate (`InteractionCandidate`), waits on a bounded `wait_condition` for a `decide(approved)`
signal — the button click — and only on Yes runs an activity that proposes the note through the
PR-gate. Reject or timeout ends the workflow without proposing (`ApprovalOutcome.status` =
`approved`/`rejected`/`expired`). A `status` query lets a polling UI render the button. The hold
is durable: restarting a worker mid-wait resumes from history. Runs on `background-jobs`;
registered on the background worker.

**Why the button gates the proposal, not the merge.** An approved candidate still lands on a
feature branch for the real human PR review (D-005 unchanged) — a chat click is not an auditable
GxP sign-off. Collapsing the PR-gate into the button was rejected; it would need its own ADR.

**DRY.** The build-and-gate logic moved into `memory.interaction.propose_confirmed_answer` (two
real callers now: the synchronous agent tool and the durable activity), so the inline tool and
the Yes button produce byte-identical PRs. The hold timeout is config
(`interaction_approval_timeout_seconds`, default 7 days), never hardcoded.

**Scope.** Backend seam only — no frontend. It exposes exactly what a future chat UI hooks onto:
start-workflow (surface candidate) → `decide` signal (click) → `ApprovalOutcome` (PR ref).

**Result.** `make lint type test` green: 231 passed / 17 skipped (sandbox-infra only). New tests:
the in-sandbox signal/query state machine + worker registration, and a server-backed test
(CI; skips offline) proving Yes proposes exactly one PR while No and an unanswered (time-skipped)
hold propose none.

## D-033 — One canonical identity scheme: SHA-256 hashing + canonical SMILES in every key

**Context.** In-depth review found the "compute once, never twice" guarantee (D-011) had a hole:
the calculation cache keys (`calc.xtb`/`pka`/`solubility`) and the QM workflow-dedup id
(`workflows.models.qm_job_key`) were built from the **raw** SMILES string, so `"CCO"` and `"OCC"`
— the same molecule — produced different keys and recomputed. Separately, four near-identical
canonical-JSON hash helpers had drifted: three used SHA-256 (at 12 or 16 hex chars) and
`qm_job_key` used **SHA-1** (48 bits — the weakest identity in the system, yet load-bearing as
workflow id, scheduler handle, and cache key at once).

**Decision.** Two shared modules now own identity: `chemclaw.ids.stable_hash(payload, *, chars)`
(the one canonical-JSON + SHA-256 helper, all four call sites ported) and `chemclaw.chem`
(`canonical_smiles` moved here from `eln.chem` since the compute layer needs it too, plus a strict
`require_canonical_smiles` that raises `InvalidSmilesError`). Every calculator cache key and
`qm_job_key` canonicalizes the SMILES before hashing; `qm_job_key` moved to SHA-256 (16 hex / 64
bits). `prepare_input` (the QM G4 boundary) now canonicalizes, so an invalid molecule is rejected
at the durable boundary instead of flowing through the mock into a stored result. `InvalidSmilesError`
was added to `publish._BAD_DATA_TYPES` (Temporal matches non-retryable types by exact class name).

**Key-material change.** `qm_job_key` output changed (algorithm + canonicalization), so QM workflow
ids and QM cache entries for pre-existing non-canonical inputs are a one-time miss — acceptable while
the cache is young. The `calc` cache keys kept SHA-256[:16], so only genuinely non-canonical SMILES
re-key; canonical inputs still hit existing rows. `eln.chem` was deleted (its two callers now import
`chemclaw.chem`).

**Result.** `tests/test_ids.py` proves equivalent SMILES share one key across all three calculators
and `qm_job_key`, and that invalid SMILES are rejected. Lint/type/test green.

## D-034 — Review hardening: migration ledger, durable audit trail, injection framing, stmt timeout

**Context.** The in-depth review surfaced four hardening gaps in otherwise-green code.

**Migration ledger (`calc.migrate`).** The old runner split files on `;` (fragile against a
`DO $$ … $$` block or a semicolon in a string) and re-ran every statement each time, leaving no
record of what applied. Now each file is sent whole (psycopg simple-query protocol) and tracked
in `schema_migrations` (`infra/sql/000_…`) by filename + SHA-256; an already-applied file that
changes is rejected as drift (`MigrationError`) rather than silently re-run. The runner reuses
`chemclaw.db.connect` (redacted-DSN errors) instead of re-implementing the connect.

**Durable GxP audit trail (`agents.audit` + `agents.audit_store`).** The middleware logged to
stdlib only, with no identity, no correlation, no outcome, no durable store. It is now built
per-conversation (`make_audit_middleware`) stamping a `correlation_id` and an `actor` (the Phase-6
identity seam — `"unknown"` until Entra auth), capturing each call's outcome and a short effect
summary (e.g. the PR ref a `propose_*` returned), and emitting to an optional `AuditSink`.
`PostgresAuditSink` writes the append-only `audit_events` table (`infra/sql/006_…`); the default
stays log-only (`NullAuditSink`), so no DB coupling is forced on lightweight runs. A sink failure
is logged and swallowed — the audit store can never break a tool call. Args may hold user PII;
the char budget bounds what is stored (noted in the field docs). A tamper-evident hash chain is
left for Phase 6.

**Indirect-prompt-injection framing (`agents.framing`).** `expand_note`/`gather_evidence` fed note
bodies verbatim into context; ingested (non-agent-authored) notes bypass the PR-gate, so an
adversarial body was a live vector. Retrieved content is now wrapped in a `<retrieved-note id=…>`
envelope, paired with an agent instruction that envelope contents are evidence to cite, never
commands. Cheap, centralized, marks the trust boundary; full content-provenance stays Phase 6.

**Per-statement DB timeout.** `chemclaw.db.connect` gained an optional `statement_timeout_seconds`
(libpq `statement_timeout`), applied by both stores from `settings.pg_statement_timeout_seconds`,
so a hung query is cancelled rather than burning the whole enclosing activity budget; migrations
opt out (an index build may run long).

**Also:** an absolute `knowledge_dir` is rejected at startup (it would escape the note repo via
`Path` join); the memory-job corpus reader catches only `ChemclawError` (not bare `ValueError`)
and logs each skipped entry. The fingerprint bit-width "dual source of truth" was left as-is: a
width change already fails loudly (SQL `bit(<configured>)` insert vs the column, plus the
definition string), so a runtime assertion would be redundant defensive code.

**Result.** New/updated tests: `test_ids`, `test_config` (absolute `knowledge_dir`), `test_evals`
(A/B epsilon band, `bo_regret` case), `test_audit` (factory, sink, outcome, sink-failure),
`test_framing`, `test_postgres_store` (idempotent tracked migrate). `make lint type` green;
`make test` green (server/pg-backed cases skip offline, run in CI).

## D-035 — Missing runnable seams: schedules, ELN cursor persistence, approval + skill-role seams

**Context.** The review found subsystems that were built and worker-registered but could not
actually run as designed, plus two Phase-6 seams worth landing early.

**Temporal Schedules (`scripts/schedules.py`, `make schedules-apply`).** The ELN sync and the
three memory-synthesis workflows documented themselves as Schedule-driven, but no
`create_schedule` call existed anywhere — they were unrunnable on a cadence. `planned_schedules()`
is the pure, testable list of what is maintained; `apply_schedules` creates each Schedule or
updates it in place (idempotent). Intervals are config (`*_schedule_minutes`).

**ELN sync cursor persistence (`eln.cursor`, `sync_cursors` table).** `ElnSyncWorkflow` required a
mandatory `since` with no caller and nothing fed `next_cursor` back. It is now self-cursoring:
started with no `since` (the scheduled case) it loads its high-water mark from `sync_cursors`,
syncs, and stores the advanced value via two new activities (`load_sync_cursor`/`store_sync_cursor`,
registered on the background worker). An explicit `since` (manual backfill) runs without touching
the stored cursor. Durability stays in Temporal + Postgres, per the layer rules.

**Approval starter/decider seam (`agents.interaction_tools`).** `InteractionApprovalWorkflow`
(D-032) had no in-repo starter. `start_approval`/`decide_approval`/`approval_status` are the one
working reference caller a chat UI hooks onto — mirroring the `qm_tools` client pattern, stable
`approval-<interaction_id>` id (idempotent surface), clear errors on an unknown hold.

**Phase-6 code-side seams.** `build_agent(actor=…)` threads an actor through the audit trail
(D-034), and `build_agent(allowed_skills=…)` + `agents.skill_access.RoleFilteredSkillsSource`
scope which skills the agent advertises — both default to today's behavior (`"unknown"` /
all-skills-visible), so Phase 6 is a value change at the call site, not new surgery. MCP auth,
Temporal mTLS, namespaces, and the HPC bridge remain true Phase-6 work (need live infra).

**Result.** New tests: `test_schedules` (plan coverage + config intervals), `test_cursor`
(pg-backed round-trip), `test_interaction_tools` (server-backed start/signal/query), plus
worker-registration assertions and `test_skill_access` (filter/pass-through/fail-closed).
`make lint type` green; `make test` green offline (server/pg cases run in CI).

## D-036 — Review cleanup: dedupe, name-drift guard, neutral config names, doc refresh

**Context.** The review's lower-severity cleanups, batched.

- **Tool-name drift.** `bo_tools`' docstring told the model to call `find_similar_reactions`,
  but the agent's actual MCP tool is `similar_reactions`. Fixed, and `test_agent` now asserts
  every tool the instructions name is in the agent's advertised surface (registered function
  tools + allowed MCP tools) — a regression guard against this class of bug.
- **Duplicated `_WIKILINK`.** The identical regex in `kg.note` and `report.retrievers` is now
  one public `kg.note.WIKILINK`, imported by the report layer.
- **Scattered hashing.** `report.harness._report_id` used a bare `hashlib.sha256`; it now uses
  the shared `chemclaw.ids.stable_hash` (report ids stay ref-safe and unique — the test checks
  properties, not the exact digest).
- **Neutral config name.** `report_excerpt_chars` → `note_excerpt_chars`: both the report
  harness and the memory layer excerpt note bodies with it, so the name no longer implies the
  budget is report-only (one knob, cannot drift).
- **`search_tools`** is documented as the in-process example/test seam that is NOT registered on
  the live agent (which uses the MCP capability servers); the two must stay in sync.
- **Docs refresh.** `agents/__init__.py` and `agents/README.md` no longer claim the tools are all
  MAF↔Temporal adapters or that the package is "empty until Phase 1"; the `evals.metric` (singular
  interface/registry) vs `evals.metrics` (plural functions) split is called out in both headers.
- **ESOL coefficients stay inline** (`calc.solubility`): the Delaney (2004) model is a fixed,
  published closed form, so its five coefficients are a deliberate, documented exception to
  "config, never magic numbers" (unlike the pKa calibration, which is tunable and lives in
  config). Recorded here so it stops resurfacing in review.
- The ADR convention (`DECISIONS.md` = terse running log, `docs/adr/` = long-form when a rationale
  outgrows a paragraph) was already documented in `docs/adr/README.md` and is left as-is.

## D-037 — Tooling gaps: coverage, unified mypy scope, worker tests, preflight, skill-validate

**Context.** The review found tooling gaps that let regressions slip past the local gate.

- **Coverage.** No coverage measurement existed. Added `pytest-cov` (dev dep), a `make cov`
  target (kept out of the default `make test` so it stays fast/dependency-light), and
  `[tool.coverage]` config over the first-party packages. No hard `--cov-fail-under` yet — it
  can't be calibrated offline; set it from the first CI baseline (BACKLOG P2), then ratchet.
- **Pre-commit vs CI mypy drift.** The pre-commit mypy hook checked a narrower package set than
  the Makefile/CI, so a type regression in `eln/evals/mcp_servers/memory/report/scripts` passed
  pre-commit and failed CI. The hook now invokes `make type` — one source of truth.
- **Worker entrypoints.** `workers/*` had no direct tests. `test_workers` asserts both mains
  import cleanly, register non-empty duplicate-free workflow/activity sets, and cover their
  responsibilities (QM on hpc; ELN sync + cursor activities on background) — a wiring-drift guard.
- **API-key preflight.** `_default_chat_client` now fails at agent build with a clear
  "set ANTHROPIC_API_KEY" message instead of an opaque 401 on the first model call (injected
  clients skip it).
- **`make skill-validate`.** `scripts/validate_skills.py` validates every SKILL.md's frontmatter
  (name/description present, `name` matches its directory) and gates in CI, mirroring
  `kg-validate`/`eln-validate`, so a broken skill fails the build rather than vanishing from the
  agent's skill surface.

**Result.** New tests: `test_workers`, `test_validate_skills`, and an `_default_chat_client`
preflight case in `test_agent`. `make lint type` green; `make test` green offline. CI gains a
`make skill-validate` step.

## D-038 — MAF Agent Harness as an optional third reasoning backbone
The reasoning layer (§1) had two building blocks — plain `Agent` and (planned) MAF graph
workflows. The installed `agent-framework-core` 1.11 ships a third, the **Agent Harness**
(`create_harness_agent`): a self-managed todo list (`TodoProvider`) + explicit plan/execute
mode (`AgentModeProvider`) that lets the agent decompose an open, multi-step request into a
visible, checkable plan and work through it autonomously. (Not to be confused with the Phase-5b
*report* harness, D-020 — that is a deterministic synthesis pipeline over retrievers; this is
the MAF conversation agent's own planning loop.) `build_agent` wires it behind `harness_enabled`
(default off) over the **same** tools, skills, history, compaction (D-025), and audit middleware
(D-027); the classic `Agent` stays the tested default and the one-switch fallback (the harness
API is `[Experimental]`). `harness_autonomy` gates the completion loop: `plan_only` stays
interactive; `execute` loops the agent through its todos but **only in execute mode**
(`todos_remaining(looping_modes=["execute"])`), so a plan is made — and can be approved — in
plan mode first. The loop is hard-capped by `harness_max_loop_iterations`.

This **refines D-002, it does not overturn it**: the harness is strictly MAF-internal and holds
only lightweight conversation state (the todo list); it adds **no** new durability. Long/expensive
work still hands off fire-and-forget to Temporal, which remains the only durable execution system.
The generic file-memory/file-access/shell/web-search batteries `create_harness_agent` enables by
default are turned **off** — Chemclaw's capability is its explicit tools/skills, not a generic
filesystem or shell (§6, G6). Our own deterministic compaction (D-025) replaces the harness's
default (passed as the last context provider, preserving the history→skills→compaction order).

**Does it replace the graph-based approaches? No.** It replaces neither Temporal (durability) nor
MAF graph workflows (fixed, deterministic reasoning flows). Phase 5b's report pipeline landed as a
source-agnostic pure-function core + Temporal `report_workflow` — no MAF graph-workflow code
exists in the repo, so nothing is replaced in code. The three are complementary: Temporal =
durable execution · graph workflow/deterministic pipeline = fixed flows · agent harness = open
dynamic multi-step planning. See `docs/harness-konzept.md`.

## D-039 — F0: config-selected LLM provider seam (foundation-plan D-A1)

**Context.** The target deployment serves the LLM from an internal OpenAI-compatible ("OpenLLM-like")
endpoint, not Anthropic. The agent must reach it by config, and the raw inference credential is
**one generic API key, not per-user Entra** (the model call is not a user-scoped resource; identity
scoping applies to *who* takes the turn / *which* workflow runs, handled in F4).

- **One import site.** `agents/llm_provider.py::build_chat_client()` is the only place a chat-client
  class is imported (mirrors the ELN adapter registry). `build_agent` calls it; the deleted
  `_default_chat_client` is gone. `settings.llm_provider ∈ {openai_compatible, anthropic}`.
- **openai_compatible** builds MAF `OpenAIChatClient(model=llm_model, async_client=AsyncOpenAI(...))`,
  where the `AsyncOpenAI` carries `llm_base_url`, the generic `llm_api_key` (a non-empty placeholder
  if the endpoint is keyless), `llm_timeout_seconds`, `llm_max_retries`, and a CA-pinned httpx client
  when `llm_tls_ca_bundle` is set — so a firewalled internal endpoint with a private CA works from
  config alone. **anthropic** keeps the pre-seam dev path (its own key preflight, `agent_model`).
- **Default `anthropic`** so the config singleton is valid with no endpoint set; production sets
  `CHEMCLAW_LLM_PROVIDER=openai_compatible` + base_url/model (validated at startup).
- **Generation params** (`llm_temperature`/`llm_max_tokens`) thread onto `Agent(default_options=…)`.
- New dep: `agent-framework-openai`. Tests: `test_llm_provider`, `test_config`, `test_agent`.
- **Open (F0-T4):** the internal model's function-calling reliability is the project's #1 risk; a
  spike verdict (`docs/spikes/f0-toolcalling.md`) is pending a live endpoint before building further.

## D-040 — F1: MAF Agent Harness is the autonomous plan/execute backbone (foundation D-020)

**Relation to D-038.** This re-integrates and supersedes the earlier harness-adoption decision
(D-038): the same `create_harness_agent` wiring, now promoted from an *optional* backbone to the
foundation's autonomous plan/execute path and refactored into `_build_harness_agent`/
`_capability_tools`/`_history_provider` (F0 options + F3 durable sessions on both paths).

**Context.** Foundations #1/#2 (an actually-run agentic loop + a visible plan/todo list) — the
Claude-Code-like experience — were absent. MAF **ships** the harness (`create_harness_agent` +
`TodoProvider`/`AgentModeProvider`/`todos_remaining`), so the decision is to *wire* it, not build it.

- **Wiring, batteries off.** `build_agent` branches on `settings.harness_enabled`; `_build_harness_agent`
  calls `create_harness_agent` over the **same** `_capability_tools()` (the full function+MCP set),
  `RoleFilteredSkillsSource`, audit middleware, and a shared `_compaction_strategy()` (extracted so
  classic and harness compaction cannot drift). MAF's generic batteries (file memory/access, web
  search, shell) are **disabled** — capability is ours (MCP servers + tools), not the harness built-ins.
- **Plan→approve→execute for free.** `AgentModeProvider` ships `plan`/`execute` modes ("present plan →
  approval → `mode_set` execute"). `harness_autonomy=plan_only` (default, pharma-safe) starts in `plan`
  and, because the loop predicate `todos_remaining(looping_modes=["execute"])` only continues in
  execute mode, the agent produces a plan and stops for approval — the pre-execution GxP gate. `execute`
  starts looping immediately, capped by `harness_max_loop_iterations` (runaway guard).
- **Classic path is the load-bearing fallback** against the harness's `[Experimental]` API — off by
  default; a test asserts it attaches no todo/mode providers.
- The completion loop is *driven* by the run service (F2); this ADR covers the wiring, proven by
  `test_agent` (todo/mode added, full toolset kept, audit kept, start-mode per autonomy).

## D-041 — F2: front-door run service (foundation-plan D-A2)

**Context.** The decisive gap: the agent was only ever *built* (in tests), never *run*. A chemist
needs a browser surface, and someone has to own the MCP tool lifecycle the constructor leaves open.

- **One ASGI service.** `service/app.py::create_app` (FastAPI) builds/holds one agent per process and
  a per-session `AgentSession`; `service/runner.py::run_turn` opens the MCP contexts for the turn
  (`AsyncExitStack` over `agent.mcp_tools` — the lifecycle the agent docstring delegates to its
  caller), runs `agent.run(..., stream=True, session=…)`, and translates streamed updates into typed
  events. When the harness is on, the *same* `agent.run` drives its completion loop — no separate
  driver. The agent factory is injectable, so the whole HTTP surface is tested with a fake streaming
  agent (no live model/MCP/creds).
- **Typed turn contract.** `service/events.py` is a discriminated union on `type`
  (plan/tool_call/token/job_started/approval_request/answer/error) serialized one-per-SSE-line, so
  the web UI now and Slack/mobile later render one contract, not a bespoke stream each. Tool calls are
  extracted **duck-typed** from update contents (MAF's function-call content class is not a stable
  export), keeping the runner version-robust. A failed turn becomes one user-safe `ErrorEvent`, never
  a mid-stream 500 or a leaked trace.
- **Thin built-in web chat** (`service/static/`), not an adopted generic UI — full control over plan
  display, tool trace, citations, and the approval affordances a generic chat UI can't render. The
  messages endpoint is POST+SSE, so the page reads the response body as a stream (native `EventSource`
  is GET-only). Config: `service_host`/`service_port`/`service_cors_origins` (empty CORS = safe
  default). Deps: `fastapi`/`uvicorn`/`sse-starlette`.
- **Deferred within F2:** emitting `PlanEvent` from harness todo state and a real `JobStartedEvent`
  when a tool launches a Temporal job — both land with F3's durable session + job→session push-back.
  Identity (Entra OIDC on every non-health route) is F4.

## D-042 — F3: durable session + job→session push-back (foundation-plan D-A3)

**Context.** Two gaps: a conversation died with the pod (in-memory history), and a finished job could
not reach a waiting chat (the user had to poll). F3 closes both without moving durability out of
Temporal (D-002) — session history and the push-back *notification* are their own layer.

- **F3-T1 durable history.** `agents/session_store.py::PostgresHistoryProvider` overrides only
  `get_messages`/`save_messages` (like `InMemoryHistoryProvider`), persisting `Message.to_dict()` to
  `session_messages` (`infra/sql/008`) keyed by session id, reloaded in `id` order. `build_agent`
  selects it via `_history_provider()` on `settings.session_store` (`memory` default | `postgres`);
  a fresh instance over the same DSN resumes the thread. `session_store_dsn` falls back to
  `postgres_dsn`.
- **F3-T2 push-back channel.** `session_events` (`infra/sql/009`, partial index over unconsumed) is a
  durable mailbox. `agents/session_events.py` is the writer (`record_session_event`), reader
  (`fetch_unconsumed`/`mark_consumed`), and a `stream_new_events` tailer whose fetch/mark/poll are
  dependency-injected so its consume-once loop is unit-tested with no DB. `workflows/notify.py` wraps
  the write in a Temporal activity (`record_session_event_activity`, on the background queue) plus a
  workflow-side `notify_session_best_effort` — same never-fail-the-science discipline as
  `publish_note_best_effort`.
- **F3-T3 wiring.** The turn's session is *ambient*, not a model argument: the runner stamps
  `agents/session_context.py`'s contextvar around the turn, and `submit_qm_job` reads it into
  `QMJobInput.session_id` (excluded from `qm_job_key`, so identical science still dedups across
  sessions and the completion notifies the launching session). The QM workflow calls
  `notify_session_best_effort` on completion; the front door exposes `GET /sessions/{id}/events` (SSE)
  streaming `job_completed` as a `JobCompletedEvent`, so a finished job wakes the chat with no polling.
- **Offline-tested with fakes** (contextvar, submit stamping, runner stamp/clear, tailer loop, events
  endpoint, activity forwarding); the Postgres round-trips and the Temporal workflow-emit prove out
  against live infra (they skip in the sandbox, joining the existing durable-layer skips).
- **Deferred (needs the live harness loop):** flipping the harness `awaiting` todo on completion
  (MAF TodoProvider store mutation) and emitting `PlanEvent`/live `JobStartedEvent`.

## D-043 — F4: Entra ID identity & RBAC — front-door OIDC + one authorization gate (D-A4)

**Context.** Identity via Entra is a hard requirement, and it becomes load-bearing the moment the
harness can autonomously trigger expensive HPC/BO paths ("who asked", "may they"). F4 makes
`architektur.md` §7/§8 real; the offline-verifiable core landed first, the tenant/federation edges are
infra-gated.

- **F4-T1 front-door OIDC.** `service/auth.py` validates every non-health request's Entra JWT —
  RS256 against the tenant JWKS, **audience** checked (confused-deputy: the front door is client *and*
  resource), issuer checked — into a `Principal(oid, upn, roles)`; `require_principal` is the FastAPI
  guard (401 without a valid token). `entra_required` gates enforcement; off in local dev (a stand-in
  principal), on everywhere real. JWKS/issuer derive from `entra_tenant_id`. Dep `pyjwt[crypto]`;
  bugbear allows the `fastapi.Depends` idiom. Tested with locally-signed RSA tokens (no network).
- **F4-T5 one authorization point + real actor.** `agents/authz.py::authorize_trigger(action)` is the
  single gate: an action in `entra_expensive_actions` needs a user holding an `entra_privileged_roles`
  role, else `AuthorizationError` — checked before the durable job starts, so an autonomously-planned
  todo can't launch an expensive path outside the user's entitlements (open in dev). The turn's
  identity is **ambient** (`agents/identity_context.py` contextvar, stamped by the runner from the
  `Principal`, like the session id), so the audit middleware records the real Entra oid over its
  build-time default, and `submit_qm_job` both authorizes and stamps `requested_by` = oid — all
  without rebuilding the per-process agent. `requested_by` stays out of `qm_job_key` (D-011).
- **Deferred / infra-gated:** workload identity federation (F4-T2), OBO to ELN (F4-T4), the Temporal
  mTLS + HPC identity bridges (F4-T6) — need live Entra/tenant + Temporal. Also remaining: making
  `requested_by` a *required* Entra oid across all workflow inputs, and per-request
  role→`RoleFilteredSkillsSource` scoping (needs a per-user agent or an ambient skills filter).

## D-044 — F4-T3: the core rule — user-triggered workflows are user-specific via `require_actor`

**Context.** The mandate is "every backend workflow is user-specific via Entra (required,
authorizing, reject-if-absent)." Taken literally that means a required `requested_by` oid on every
workflow input. But two facts shape the honest implementation: (1) only two workflows have a **live
agent-tool trigger** today — `submit_qm_job` and the interaction-approval — the BO campaign, report,
and memory workflows have no user-facing trigger yet; (2) the memory-distillation and ELN-sync
workflows take **no user input at all** — they are scheduled/background jobs, not launched by a
person.

**Decision.**
- **One reusable guard.** `agents/authz.py::require_actor()` is the single place the rule flows
  through: it returns the turn's ambient Entra oid, and under `entra_required` **rejects** a
  user-triggered workflow with no authenticated user (`AuthorizationError`) *before* any durable
  work — mirroring how `require_canonical_smiles` rejects bad data at the durable boundary. In dev
  (no tenant) it returns the configured `service_actor_id` (replacing the old magic `"unknown"`).
- **Wired into the one live user-trigger.** `submit_qm_job` now populates `requested_by =
  require_actor()`, so the reject-if-absent rule is enforced there. `requested_by` stays out of
  `qm_job_key` (D-011: cache identity is molecular, not per-user; two users share one cached compute).
- **No speculative fields.** Adding a required `requested_by` to `CampaignSpec`/`ReportRequest` now —
  with no caller to populate it — would be a dead "for-later" field, which CLAUDE.md forbids. Those
  inputs adopt the same `require_actor()` guard when they gain live triggers (a later phase).
- **System jobs are not user-specific by design.** Scheduled ELN-sync and memory-distillation run as
  the service, not on behalf of a person; they never call `require_actor`. Attributing them to a user
  would be wrong. The rule is precisely: every *user-triggered* backend workflow is user-specific.

**Consequence.** The core rule is real and enforced at the only live trigger, via one reusable piece;
the mechanism is ready for every future user-trigger; no dead code; the science-dedup cache is
untouched. Tested offline: ambient-user attribution, dev fallback, and reject-if-absent (both the
guard directly and through `submit_qm_job`, independent of the role gate).

## D-045 — F4-T2: workload identity federation (a pod mints its own token, no secret at rest)

**Context.** Backend components (front door, workers, MCP servers) must call Entra-protected
resources as themselves. Storing a client secret per component is the anti-pattern §7/ADR D-A4 rules
out. Entra Workload Identity Federation lets a pod present its projected ServiceAccount JWT as a
`client_assertion` in the OAuth2 client-credentials grant — no secret ever at rest.

**Decision.** `agents/identity/workload.py::WorkloadTokenProvider` performs that exchange and caches
per scope until `entra_token_refresh_leeway_seconds` before expiry; the SA token is re-read from
`entra_sa_token_path` on every exchange (it rotates). Transport and clock are constructor-injected so
the exchange is exercised offline against an `httpx.MockTransport` with a hand-cranked clock. A
process-wide `default_provider` + `get_service_token(scope)` convenience share one cache. Config:
`entra_workload_federation_enabled` (off in dev), `entra_workload_client_id`, `entra_token_endpoint`,
`entra_sa_token_path`, `entra_token_refresh_leeway_seconds`, `entra_http_timeout_seconds`.

**Consequence.** Any backend component can obtain its own Entra token with no stored secret; the LLM
generic credential remains the one documented exception (it does not use this path). Live tenant
exchange is the only gated edge — the code + request construction + caching are proven offline.

## D-046 — F4-T4: On-Behalf-Of exchange for user-scoped downstream (wired, dormant)

**Context.** When a backend acts for a *specific user* against a user-scoped resource (ELN/LIMS), it
must present the user's identity downstream, not its own service identity. OAuth2 OBO (RFC 7523)
exchanges the user's token for a downstream-scoped one.

**Decision.** `agents/identity/obo.py::exchange_obo(user_token, scope)` performs the OBO grant,
authenticating to the token endpoint with the federated SA assertion (`read_sa_token`, shared with
F4-T2 — one reader, two callers, no duplication). Transport injected for offline tests. Config
`entra_obo_enabled` (off). It is deliberately **generic and dormant**: no user-scoped source exists
yet (the first, a custom Snowflake ELN connector, is deferred behind the F7 seam), so nothing calls
it — a source opts in later. This is the wired-but-unused seam the ticket asks for, not a dead stub:
it is the single mechanism every user-scoped source will use.

**Consequence.** OBO is available for any future user-scoped source; the exchange, the OBO assertion,
and the federated client-assertion are proven offline; the live tenant exchange is the only gated edge.

## D-047 — F4-T6: the two non-Entra transport bridges carry identity as a claim

**Context.** §7.2 names two transports that are not Entra relying parties — Temporal and HPC/Nextflow.
The rule is that identity rides *inside* the workflow payload (`requested_by`, D-044), never the
transport; the transports themselves are secured and, for HPC, every identity mapping is logged.

**Decision.**
- **Temporal transport auth.** `chemclaw/temporal_client.py` now builds its `Client.connect` kwargs
  in a pure `connect_options()`: mTLS (`temporal_tls_cert`/`_key`/`_ca` → `TLSConfig`, PEM paths read
  to bytes) when set, and/or a Temporal Cloud `temporal_api_key`. Extracting the options makes
  transport security assertable offline (constructed-args, no broker); dev stays plaintext when none
  are set. Identity is *not* put on the transport — it is already in the payload.
- **HPC identity bridge.** `agents/identity/hpc_bridge.py::map_to_hpc_identity(oid)` returns the one
  shared `hpc_bridge_identity` a user's job runs under (HPC is not an Entra RP) and **logs every
  oid→HPC-identity mapping** at INFO — the sole audit link from a cluster run back to the real user.
  No `hpc_bridge_log_dsn` key was added: the audit trail already *is* structured logging, so a DSN
  with no consumer would be a dead config knob.

**Consequence.** Both bridges are ready and proven offline; the live broker/cluster wiring is the only
gated edge. Together with D-043/D-044/D-045/D-046 this closes F4's offline-verifiable scope: front-door
OIDC, one authorization gate, the reject-if-absent core rule, federation, OBO, and both bridges — the
generic LLM key remaining the one documented exception.

## D-048 — F5: real HPC execution via a Nextflow launcher behind the QM activities (D-A5, D-A5a)

**Context.** The QM spine was mocked (a SLURM-style sleep). Its module docstring promised that
making compute real would touch *only* `workflows/activities.py`. F5 keeps that promise.

**Decision (D-A5a — launch interface).** The launcher is the **Seqera Platform / Tower REST API**:
run status is a plain GET, which survives a durable heartbeat-poll cleanly (no long-lived SSH
session to keep alive across worker restarts, unlike `nextflow` CLI over SSH; no bespoke internal
launcher to build). `workflows/hpc/nextflow.py` is that adapter — `launch_run` / `poll_run` /
`fetch_artifacts`, each taking an injectable httpx transport so the full launch→poll→fetch lifecycle
is proven offline against a fake endpoint.

**Decision (D-A5 — wiring).**
- `hpc_launch_interface` selects the backend inside the two QM activities: `"mock"` (default, kept
  for CI/local — no cluster) or `"nextflow"`. The activities are the *only* module changed; the
  workflow, the worker registration, and the agent are untouched — the mock's original promise held.
- The mock is retained verbatim behind the switch, so every existing durable test passes unchanged.
- `fetch_artifacts` returns the same `energy=… converged=…` text shape, so `parse_qm_output` is
  unchanged whether output came from the mock or a real run.
- **F5-T3 cache versioning:** `qm_job_key` folds in `hpc_pipeline_version` **only when set** — a
  pipeline bump becomes a cache miss (D-011/D-033), while the empty dev/mock version leaves keys
  byte-identical to before F5 (no orphaned cache, no test churn).
- **F5-T4 worker placement:** the `hpc-jobs` worker already registers the two activities by name;
  the real launcher therefore runs on that worker with no topology change — network reachability to
  the launcher is a deploy concern carried into F6.

**Deferred (noted, not silently dropped).** The cosmetic `QMJobWorkflow→CalculationWorkflow` /
`qm_job_key→calculation_key` rename (F5-T3, plan 1c.5): pure naming, high-churn across ids/tests, no
behavior change — deferred to avoid risk with no functional gain. Real `cclib` parsing of genuine QM
output replaces the regex parser once a real pipeline output format is fixed.

**Consequence.** The real Nextflow path is code-complete and lifecycle-tested offline; the mock keeps
CI cluster-free; a pipeline version is in the cache key. The only gated edge is a live cluster run.

## D-049 — F6: OpenShift delivery — one image, one config source, three plain secrets (D-A6, D-A6a)

**Context.** The stack must run in-cluster with OIDC, secrets, workers, and probes, without a second
config system and without long-lived client secrets.

**Decision.**
- **One multi-target image** (`deploy/Containerfile`, UBI9, rootless UID 1001, arbitrary-UID safe):
  service, both Temporal workers, and the MCP servers all ship the same bits; `deploy/entrypoint.sh`
  dispatches on `CHEMCLAW_COMPONENT`. No secret baked in.
- **One config source.** The Helm `values.yaml` `config:` block → a `ConfigMap` → `CHEMCLAW_*` env,
  keys mirroring `Settings` exactly. `otel_endpoint` was added and bridged to the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT` in `chemclaw/logging.py` so the collector is one value like the rest.
- **Three plain secrets only** (F6-T6): the generic LLM key (the one Entra exception), Temporal mTLS,
  the HPC-bridge credential. Everything else is Workload Identity Federation (D-045) — the SA is
  annotated, no client secret at rest.
- **D-A6a — Temporal self-hosted in-cluster**, not Temporal Cloud: keeps the durable core inside the
  same OIDC trust boundary and avoids egressing workflow payloads (which carry the Entra `oid`,
  D-044) to a third party. Cloud remains a values-swap (`temporal_api_key` vs the mTLS trio).
- **Migrations as a pre-deploy Helm hook** (`python -m calc.migrate`, D-034) that completes before any
  app container starts. **NetworkPolicy** default-deny egress + allow-list (DNS/Postgres/Temporal/
  HTTPS). Probes: `/readyz`+`/healthz` for the service; the Temporal poll is the workers' liveness.
- **CI** (`deploy.yml`): build + non-root entrypoint smoke, `helm lint`, `helm template | kubeconform`;
  guarded rollout on the default branch.

**Consequence.** The full stack is described as deployable manifests with no second config source and
no stored client secrets beyond the three documented. **Verified offline:** YAML parse, template
brace-balance, `Settings` key mapping. `helm template`/`kubeconform`/the image build are CI-gated —
inherent to a deploy phase (no helm/daemon in the sandbox), not a manifest gap.

## D-050 — F7: the generic data-source seam (compose two half-contracts, don't merge them)

**Context.** The system had two disjoint half-contracts — `ElnAdapter` (ingest: fetch + map to the
canonical ORD reaction) and `SourceRetriever` (retrieve: evidence for a query) — with different
methods and DTOs, and two selection styles (a config-string dict factory for ELN, a hardcoded
`[GraphRetriever()]` list for retrieval). Attaching a new source (first live one: a custom Snowflake
ELN connector) touched both places.

**Decision.**
- **One seam by composition, not merger** (`sources/base.py`). A `DataSource` names itself and
  exposes an optional `ingest` half and an optional `retrieve` half, each being the *existing*
  protocol verbatim (`IngestHalf = ElnAdapter`, `RetrieveHalf = SourceRetriever`). No new DTOs —
  `RawEntry`/`OrdReaction`/`EvidenceChunk` are reused. `SourceSpec` (frozen) is the concrete impl and
  rejects a source that provides neither half. The protocol members are read-only properties so a
  frozen impl satisfies it.
- **Config-driven registry** (`sources/registry.py`, `data_sources` config). `graph` is
  retrieve-only (the knowledge graph); `eln-json`/`eln-ord` are ingest-only (the ELN adapters
  re-hosted verbatim — the ELN is not *also* the graph retriever, so no double count).
  `active_retrieve_sources()` / `active_ingest_sources()` select by config.
- **Both consumers re-hosted with no behavior change** (F7-T3). `gather_evidence`'s
  `_text_retrievers()` now returns `active_retrieve_sources()` — the default yields exactly the one
  `GraphRetriever` as before. `eln_sync.sync_eln_entries` now ingests `active_ingest_sources()` and
  merges per-source summaries — the single default source folds to the previous single-adapter
  behavior. All existing ELN/research tests pass unchanged (the acceptance bar).
- **Provenance already flows** (F7-T4): the mapped `OrdReaction` carries `provenance` + `reaction_id`
  (native ref), and knowledge still enters via the terminal PR-gate while serving indices stay
  ungated (D-018) — source-agnostically, because the seam changed only the *selection*, not the flow.

**Deferred behind the seam (unchanged from the plan):** the live custom Snowflake ELN connector
(durable `background-jobs` sync with a per-source *pipeline cursor* over Snowflake's load-timestamp) —
lands as the first registered adapter. The current shared single cursor is adequate for one ingest
source; per-source cursors arrive with that connector.

**Consequence.** A second source is one registry entry + one config token, zero edits to the ingest
loop or the evidence gatherer — proven by a fake retriever appearing in `gather_evidence` and a fake
source's halves being selected, all offline.

## D-051 — Foundation review (F4–F7): adversarial review + fixes

Four parallel adversarial reviewers audited F4 (identity/security), F5 (HPC), F6 (deploy), and F7
(seam) over the session's changes. The core paths were confirmed correct (reject-if-absent ordering,
token cache math, OBO non-deputy, TLS None-handling, contextvar reset, audience/alg pinning, cache-key
byte-identity, F7 default behavior preservation). The following real findings were **fixed**:

**F5 (HIGH + hardening).**
- The poll activity's `start_to_close_timeout` was `hpc_mock_run_seconds + qm_activity_timeout` (≈36s)
  — a mock-derived cap that would kill *every* real Nextflow run. `qm_job.py` now branches on
  `hpc_launch_interface`: the nextflow path uses `hpc_run_timeout_seconds` (24h) +
  `hpc_run_heartbeat_timeout_seconds` (120s). New configs added.
- Launcher/artifact HTTP now uses a dedicated `hpc_http_timeout_seconds` (not the Entra-token knob).
- Tower `UNKNOWN` is treated as non-terminal (keep polling), not a hard `FAILED`.

**F4 (misconfig + defense-in-depth).**
- Startup validator: under `entra_required`, `entra_audience` and a tenant/issuer are mandatory (an
  empty audience is a deny-all outage), and `entra_expensive_actions`/`entra_privileged_roles` must be
  set together (declaring one without the other leaves the role gate silently open). A second
  validator rejects a Temporal client cert without its key (half-mTLS).
- `service/app.py` now binds a session to its creator's Entra `oid`; a non-owner gets 404 on
  post/stream (no existence leak) — defense-in-depth beyond the unguessable uuid4.
- `service/auth.py` caches the `PyJWKClient` per endpoint (was rebuilt per request → JWKS re-fetch on
  the hot path) and requires the `exp` claim.

**F6 (CRITICAL + HIGH + medium).**
- `deploy/Containerfile` was missing `kg`, `memory`, `sources` (imported by the entrypoints) → pods
  and the CI smoke import would `ModuleNotFoundError`. Added, and cross-checked against every
  first-party import in the runtime packages.
- NetworkPolicy egress omitted the internal LLM (8000) and OTLP collector (4317) ports → the agent
  could not reach its model / ship traces. Added.
- The MCP Deployment lacked `chemclaw.env`, so its pods had no `CHEMCLAW_POSTGRES_DSN` (fell back to
  localhost). Added.
- The pre-install migrate hook ran before the ConfigMap/ServiceAccount it needs; those are now
  earlier-weighted (-10) pre-install hooks. `deploy.yml` smoke now imports the correct module per
  component (MCP entrypoints were never checked), and the rollout uses `helm upgrade` (runs the hook)
  instead of a nonexistent path.

Accepted deferrals (single-ingest-source cursor, token-exchange lock, ELN-shaped ingest half) are
recorded in `DEFERRED.md`. Tests added: session ownership (`test_service.py`), the enforcement/mTLS
validators (`test_config.py`), and `UNKNOWN`-non-terminal (`test_nextflow_adapter.py`).

## D-052 — Role-scoped skill visibility (salvaged from the phase6-authz branch)

**Context.** F4 (D-043…D-047) landed real Entra identity + RBAC — token validation, the
`require_actor` reject-if-absent rule, and `authorize_trigger` gating expensive *actions* by role —
but left **skill visibility** as a dead placeholder: `RoleFilteredSkillsSource` filtered by an
`allowed_skills` name-set that **no caller ever computed**. A parallel `phase6-authz` line of work
had independently built a better skill-scoping mechanism (plus a duplicate `Principal` and a second,
competing tool-authorization path). Per the instruction to keep only the better code, this salvages
the one genuinely-superior, non-redundant piece and discards the rest.

**Decision.**
- `agents/skill_access.py`: `RoleFilteredSkillsSource` → `RoleScopedSkillsSource` — a config-driven
  gate (`settings.skill_role_gates`: skill name → allowed roles). Ungated skills stay visible to all
  (empty map = today's behavior); a gated skill is hidden from a caller holding none of its roles.
  Roles are read from the turn's **ambient identity** (`agents.identity_context.get_current_roles`,
  the same source `audit`/`authz` read) rather than threaded through `build_agent`, so it composes
  with the landed F4 flow instead of introducing a second identity object.
- `build_agent` drops the unused `allowed_skills` param and wires the gate from config.
- `chemclaw/config.py`: `skill_role_gates` (JSON-overridable) + `.env.example`.

**Deliberately dropped from that branch (already implemented better by F4, so not merged):** its
`chemclaw/identity.py::Principal` (F4's `service/auth.py::Principal` does real JWT validation), its
`agents/authz.py` + `tool_role_gates` (F4's `require_actor`/`authorize_trigger` is the landed
action-authz — a second mechanism would violate DRY), and the `security-posture-note` branch's
"no authn/authz yet" documentation, whose premise F4 has superseded.

**Result.** `make lint type test` green; `mypy --strict` clean. `tests/test_skill_access.py`
rewritten for the ambient-roles design (no gates = all visible; gated skill hidden from an anonymous
turn and from a role-lacking caller, shown to one holding the role; ungated skills unaffected).


## D-053 — Consolidate ELN source selection onto the F7 seam; memory honors `data_sources` (audit DUP-1)

**Context.** The forensic audit (`docs/audit/`) found the F7 "generic data-source seam" migration
left half-done. Two registries were live at once: `sources/registry.py` (config-driven via
`settings.data_sources`, used by the durable ELN sync) and `eln/registry.py` (a hardcoded
json+ord union via `all_eln_adapters()`, used by the memory-synthesis jobs). With the default
`data_sources="graph,eln-json"`, the durable sync ingested JSON only while the memory jobs read
json+ord — the two subsystems disagreed on the corpus, and `CHEMCLAW_DATA_SOURCES` silently had no
effect on memory synthesis, breaking the F7 "config, not code" guarantee.

**Decision.**
- `workflows/memory_jobs._all_reactions()` now reads `sources.registry.active_ingest_sources()` (the
  ingest halves of the configured active sources) instead of `eln.registry.all_eln_adapters()`.
- `eln/registry.py` is deleted — `sources/registry.py` is the single source-selection registry.
- `settings.eln_sync_adapter` is clarified as the ELN sync's **cursor-key label** (it was already only
  that after F7 — the sync ingests `active_ingest_sources()`, not this field); the field is kept so
  the stored high-water cursor key is stable, with a corrected docstring.

**Consequence (intentional behavior change, signed off).** Memory synthesis now honors
`data_sources`. With the default config it reads the JSON ELN source only; **ORD reactions are no
longer included in memory synthesis until `eln-ord` is added to `CHEMCLAW_DATA_SOURCES`.** This makes
the sync and the memory jobs read the identical, config-driven source set, so the two corpora can
never disagree again.

**Result.** `make lint type test` green; `mypy --strict` clean. New `tests/test_memory_jobs.py` pins
that the memory corpus tracks `data_sources` (adding `eln-ord` expands it; a retrieve-only config
yields an empty corpus); the removed `eln/registry.py` tests are covered by
`tests/test_datasource_seam.py`.

## D-054 — Per-source ELN cursors + a per-scope token lock (close the two F-review deferrals)

**Context.** Two consciously-deferred items from the F4–F7 review (D-051) were re-examined under a
"close all found gaps" pass and found genuinely implementable offline against the *existing*
contracts — no live infrastructure, no speculative abstraction:

1. **Shared ELN cursor (F7 review F-1/F-2).** The durable sync tracked one high-water cursor
   (keyed by the now-dead `eln_sync_adapter` label) while F7/DUP-1 made a *multi*-ingest-source
   config reachable. Two sources whose newest entries differ would let the furthest `max()` cursor
   skip the lagging source's entries — silent data loss. D-053 shipped an interim fail-fast guard
   (>1 ingest source → non-retryable error); this ADR removes the guard and does the real fix.
2. **Thundering-herd token exchange.** On a cold/stale cache, N concurrent
   `WorkloadTokenProvider.get_service_token(scope)` callers each fired the federation exchange —
   correct (never a stale token) but wastefully redundant.

The deferral reasoning for (1) was "wait for the second real source (Snowflake), which brings its
own pipeline cursor." Re-checked: **both** current ingest adapters are datetime-cursored because the
`ElnAdapter` contract *is* `fetch_new_entries(since: datetime)`. Per-source datetime cursors is
therefore the faithful generalization of the contract that exists today, not a guess about a source
that doesn't. A future non-datetime cursor source would generalize the `ElnAdapter` contract itself,
at which point the cursor storage generalizes with it. So the gap is closable now.

**Decision.**
- `sources/registry.py` gains `active_ingest_source_names()` (registry names of active sources with
  an ingest half). `ElnSyncWorkflow` iterates those names: for each source it loads that source's own
  cursor (scheduled runs), syncs it via `sync_eln_entries(source, since)`, and stores the advanced
  cursor per source. The `sync_cursors` table already keys by source name — no schema change. A
  manual backfill (explicit `since`) runs every source from that point and touches no stored cursor.
- The interim multi-ingest guard is removed; multiple ingest sources are now first-class.
- `settings.eln_sync_adapter` is **deleted** (audit DUP-2): it was only the single shared-cursor
  label, which no longer exists. `.env.example` and the runbook (iii) are updated to the
  `data_sources` reality.
- `WorkloadTokenProvider` gains a per-scope `asyncio.Lock`; `get_service_token` re-checks the cache
  under the lock (double-checked), so N concurrent misses on one scope do a single exchange while
  distinct scopes never block each other.

**Consequence (contract note — dev-stage, no live cluster yet).** The sync's stored-cursor keying
changes from one `eln_sync_adapter` label to per-source registry names; on a live system the first
scheduled run after the change re-ingests each source from its epoch once (harmless — ingestion is
idempotent, id-keyed upserts + idempotent note branches). Removing `eln_sync_adapter` is a config
surface change: a deployment that set `CHEMCLAW_ELN_SYNC_ADAPTER` must drop it (`extra="forbid"`).
Both are acceptable now because the F-layer live edges are still open (no in-flight workflows, no
real deployment).

**Result.** `make lint type test` green; `mypy --strict` clean. `tests/test_eln_workflow.py` adds
offline unit tests (named-source activity, `active_ingest_source_names`, the summary fold) and a
server-backed test proving each active ingest source gets its own stored cursor;
`tests/test_workload_identity.py` adds a concurrency test asserting 10 concurrent misses do exactly
one exchange.

## D-055 — GxP freshness + read-time provenance in graph retrieval (audit KM-6, KM-7)

**Context.** The knowledge-management gap analysis (`docs/audit/09-knowledge-management-gaps.md`)
found two read-path gaps that are cheap, offline, and central to the GxP posture — no infra, no
schema migration, no curated artifact, no chosen threshold:

- **KM-7 (freshness).** `Note.valid_from`/`valid_to` existed but were **never checked at read**, so a
  not-yet-valid or expired note served as current fact with no signal — sharp for a GxP base that
  must not present superseded conditions as current.
- **KM-6 (provenance at read).** `NoteRef` (the agent-facing view from `find_notes`/`expand_note`)
  exposed only `id/type/smiles/tags`, so the agent could not weigh a source by author/origin/
  confidence/validity without a second lookup, even though the note carried all of it.

**Decision.**
- `Note.is_current(as_of)` encodes the validity window (inclusive bounds; either bound optional).
  The three discovery/evidence sweeps — `find_notes`, `expand_note`'s neighbor list, and
  `GraphRetriever.retrieve` (the report path) — now exclude non-current notes as of `date.today()`.
  **Explicit by-id expansion still returns the anchor** even if expired (an explicit lookup, not a
  discovery sweep); only discovered/neighbor/report evidence is freshness-filtered. Nothing is
  deleted — the note stays in Git and reachable by id, it is only dropped from *current-evidence*
  results.
- `NoteRef` carries `created_by`, `source`, `confidence`, `valid_from`, `valid_to` (all defaulted so
  a bare reference is still constructible); `_ref` fills them from the note. This also wires the
  previously-unread `confidence` field into the agent's view (part of KM-5's concern) without
  building a cross-source ranker.

**Consequence (behavior change, flagged).** Retrieval results change: an expired or not-yet-valid
note no longer appears in `find_notes`, in `expand_note` neighbors, or in report evidence. This is
intended GxP behavior (don't serve superseded facts as current). The chosen policy is *exclude
silently from current-evidence sweeps* (the note is still in Git and by-id reachable) rather than
*include-with-a-flag*; if a surfaced-but-flagged behavior is later wanted, `is_current` is the single
seam to branch on.

**Result.** `make lint type test` green; `mypy --strict` clean. Tests: `test_note.py` (window
semantics incl. inclusive boundaries), `test_graph_tools.py` (provenance surfaced; expired excluded
from `find_notes` and from `expand_note` neighbors while the anchor is kept), `test_report.py`
(`GraphRetriever` skips expired). The remaining gap-doc items are either deferred-by-design/
infra-gated or carry a design decision (a gold-set, a ranking function, a concurrency limit, an audit
schema migration) and are left for an explicit follow-up rather than guessed.

## D-056 — Retrieval-quality gate: a starter gold set + registered metrics (audit KM-13)

**Context.** KM-13 was the highest-severity knowledge-management gap: the system's core promise is
"surface the right evidence", yet `evals/` scored only *chemistry* output (E-factor, PMI, prediction,
regret) — there was no query→expected-source gold set and no retrieval metric, so a change to the
substring filter or the evidence cap could quietly halve recall with nothing to catch it. The gap
doc calls a gold set "the cheapest high-value fix, and a small corpus is the ideal time to build it."

**Decision.** Build the starter gold set and register two retrieval metrics on the existing `@metric`
seam (plan 2b.5):
- **A fixed corpus fixture** (`evals/retrieval_corpus/`, six realistic notes) — deliberately *not*
  under `knowledge_dir`, so the score is reproducible and independent of the live graph, and
  `kg-validate` (which scans `knowledge_dir`) does not treat the fixtures as real notes. The live
  `knowledge_dir` is effectively empty, so scoring against it would measure nothing.
- **`retrieval_recall` (gated) + `retrieval_precision` (diagnostic)** in a *separate* module
  (`evals/retrieval.py`, not `evals/metrics.py`) because they run `GraphRetriever` over the corpus —
  they are not pure functions of the case, so isolating them keeps `metrics.py`'s "pure function"
  invariant honest. Recall gates the "did we surface the expected sources" signal against
  `retrieval_recall_min` (config, default 0.75); precision is order-independent context (`passed`
  None). Both score `GraphRetriever` — the same source-agnostic path a report uses, and the one that
  now honors the KM-7 freshness filter.
- **Gold cases** (`evals/cases/retrieval-*.md`) pair a query with its expected source ids. Five
  cases: exact-term, broad-recall, a type-filtered query, a conditions-term query — and, on purpose,
  one query (`cross-coupling`) whose relevant Suzuki-reaction note the literal substring filter
  cannot reach, so recall = 0.5 and the gate fails **by design**. That case *measures* the KM-4
  literal-matching limitation (and documents the mitigation — the agent's query reformulation, which
  this lexical metric does not exercise) instead of leaving it anecdotal. It mirrors the existing
  eval philosophy of holding a deliberately-failing case to prove the gate fires.

**Why the test suite, not a CI hard-gate.** As with the other scientific metrics, regression gating is
the **pinned test** (`tests/test_retrieval_eval.py` pins each case's exact recall/precision/verdict),
not a red `make eval` — the CLI stays report-only and exits 0 so the by-design failing case does not
break CI. A filter/cap change that moves recall moves a pinned number and fails the test.

**Result.** `make lint type cov` green; `mypy --strict` clean; `make eval` exits 0 and renders the
retrieval rows (the one literal-miss case shows FAIL, by design). New: `evals/retrieval.py`,
`evals/retrieval_corpus/` (6 notes + README), `evals/cases/retrieval-*.md` (5 cases),
`tests/test_retrieval_eval.py`; config `eval_retrieval_corpus_dir` + `retrieval_recall_min`.
Follow-ups (recorded, not guessed): grow the gold set as the corpus grows, and add an agent-run eval
that exercises the LLM's query reformulation over the lexical layer.

## D-057 — Four more engine gaps closed (KM-5, KM-14 retrieval half, AG-14, AG-15)

**Context.** After D-055/D-056, five gap-doc findings remained. Each carried a design decision that
had been left un-guessed. Directed to implement four of them (AG-13 stays deferred — see below), each
with a **defensible default** documented here rather than a new config knob per open question.

**Decisions.**
- **KM-5 — rank-before-truncate.** `EvidenceChunk` gains an optional `score` in [0,1]; `gather_evidence`
  sorts by it (stable) before applying `gather_evidence_max_chunks`, so a truncated sweep keeps the
  best-supported evidence, not an arbitrary disk slice. Scoring is per-retriever in its own terms —
  graph hits score by the note's `confidence` (`retrieval_default_confidence` when absent, wiring the
  previously-unread field), structural hits by their Tanimoto similarity. It is a within-sweep
  ordering heuristic, **not** a calibrated cross-source probability (documented on the field). Finer
  lexical relevance is deliberately skipped: the graph filter is whole-substring, so every returned
  note already contains the full query — a lexical-overlap term would be vacuous until KM-4 lands.
- **KM-14 — retrieval-path cache (not the clustering half).** `load_notes` caches the parsed notes
  per directory behind a cheap stat fingerprint (`(path, mtime_ns, size)` per file); any add/edit/
  delete busts it, so retrieval stays **always-live** while skipping the re-parse when nothing
  changed. Guarded by a lock (retrieval offloads to threads). `graph_cache_enabled` (default on) can
  disable it. The separately-deferred O(n²) *clustering* half of KM-14 is untouched — it is a
  background job, not the per-query interactive path the gap flags as the sharper concern.
- **AG-14 — version provenance.** `AuditEvent` gains `revision`, stamped from `deployment_revision`
  (the deployment's Git SHA / image digest, "unknown" until F6 sets it) at middleware build time;
  migration `010_audit_revision.sql` adds the column (idempotent, `NOT NULL DEFAULT 'unknown'`, no
  backfill). A past result now ties to the exact version that produced it. The *behavioral* half of
  AG-14 (a pre-live gate) is AG-13, deferred.
- **AG-15 — admission control.** The front door holds a config-capped `asyncio.Semaphore`
  (`service_max_concurrent_turns`, default 8) for a turn's whole streamed run; a turn that cannot get
  a permit within `service_turn_admission_timeout_seconds` (default 5) is shed with **503** rather
  than piling onto the shared LLM endpoint. Only the LLM-bound message turn is gated (health and
  push-back streams are not). The cap is a conservative default to be tuned to the real endpoint's
  throughput — picking it does not need to wait for that number, only tuning does.

**Deferred (unchanged).** **AG-13** (agent-behavior / prompt / skill regression eval) stays in
`DEFERRED.md`: a faithful behavior eval must run the agent against the real internal LLM endpoint
(unreachable offline); a mock would only test the mock. It is the one genuinely infra-gated item.

**Contract / behavior notes.** `gather_evidence` now returns its cap's worth of *highest-scored*
chunks (order/content otherwise unchanged; an all-unscored corpus keeps disk order via the stable
sort). Retrieval reads may be served from the graph cache (busted on any note change — never stale).
The front door can now answer **503** on the messages route under load. Migration `010` must be
applied (`make db-migrate`); it is idempotent.

**Post-implementation review hardening.** An independent diff review found no live bug but five
latent/robustness items; four were fixed here, one consciously kept:
- *Graph cache — stat on a vanished file.* `_dir_fingerprint` now wraps `path.stat()` in
  `except OSError: continue`, so a note deleted between `rglob` and `stat` (a `git pull` under a live
  query) drops out of the fingerprint and busts the cache on the next read, instead of crashing the
  query — the resilience `_parse_notes` already had.
- *Graph cache — shared mutable notes.* `Note` is now `frozen=True`. The cache hands the same
  instances to every reader; immutability makes that sharing provably safe (no reader can corrupt a
  cached note), and no code mutated a note in place, so freezing is behavior-preserving.
- *Evidence score default.* `EvidenceChunk.score` defaults to a neutral **0.5** (was 0.0). Every
  current retriever sets it explicitly; the default only governs a future retriever that forgets to,
  and neutral keeps such a chunk mid-ranking instead of silently pinning it last-and-truncated.
- *Admission-permit release is now tested.* A test runs three sequential turns against a single
  permit and asserts all succeed and the permit returns — guarding the `finally: release()` whose
  regression would silently collapse capacity.
- *Kept as-is:* the permit is acquired in the handler (not inside the SSE generator) so a shed turn
  can return a clean **503** before the response starts — moving the acquire into the generator, as
  one suggested, would break that. The only leak path (response created but never iterated) needs an
  exotic failure between endpoint return and `response.__call__` under sse-starlette; accepted.

**Result.** `make lint type cov` green; `mypy --strict` clean. Tests: `test_research_tools.py`
(rank-before-truncate keeps the confident notes), `test_report.py` (`GraphRetriever` scores by
confidence), `test_graph.py` (cache reuse + fingerprint-bust + disable + vanished-file tolerance),
`test_note.py` (note is immutable), `test_audit.py` (revision stamped), `test_service.py` (503 at
zero capacity + permit released across sequential turns). New config: `retrieval_default_confidence`,
`graph_cache_enabled`, `deployment_revision`, `service_max_concurrent_turns`,
`service_turn_admission_timeout_seconds`; new migration `infra/sql/010_audit_revision.sql`.

## D-058 — F10-E/B: per-task model routing + answer verification & confidence routing (D-A11)

**Context.** A capability comparison against a commercial pharma-agent *platform* (IntuitionLabs)
found Chemclaw at or ahead on the durability/identity/audit spine, with deltas in retrieval breadth,
output verification, fine-grained authz, orchestration topology, and metrics polish. Phase F10
(`docs/parity-plan.md`) closes the ones that add value now and records triggers for the deferred
ones. Two of those deltas: no per-task model selection, and no verifier/confidence on the answer
path (only the report's deterministic citation gate).

**Decision.**
- **F10-E:** `build_chat_client(task="agent")` consults `settings.model_routes` (JSON task→model),
  falling back to the provider default. Still the single import site for a chat client — a task is a
  per-model choice on the one internal endpoint, not a second provider.
- **F10-B:** `agents/verifier.py::verify_answer(answer, evidence)` scores citation faithfulness and
  returns a `VerificationResult` (per-claim `ClaimCheck` + aggregate `confidence`). When
  `verifier_enabled`, an LLM-as-judge runs on the cheap routed `"verifier"` model via structured
  output; otherwise the deterministic report gate (`report.harness.verify_claims`) is the offline
  fallback (DRY, one citation check). `verify_turn_answer` resolves an answer's `[[wikilinks]]` to
  the notes it cites — the conversational scoring input. The runner stamps `AnswerEvent.confidence`
  + `unsupported_claims`; a low-confidence answer surfaces a review affordance and routes to the
  existing D-032 hold. No new gate primitive; a verifier failure degrades to the unscored answer.

**Consequence.** Default-off: `model_routes={}` and `verifier_enabled=False` reproduce today's
single-model, unscored-answer behavior exactly. The durable report workflow verifies at citation
level (it has no synthesized prose); the conversational path gets the LLM faithfulness score.

**Result.** `make lint type test` green. Tests: `test_llm_provider`, `test_verifier`, `test_runner`,
`test_config`.

## D-059 — F10-C: per-tool authorization middleware (supersedes D-044 scope, D-A12)

**Context.** `authorize_trigger` guarded only the expensive `submit_qm_job` trigger (F4-T5). Tool-use
governance at *every* invocation was a platform delta.

**Decision.** `agents/tool_authz.py::enforce_tool_authz` is a MAF `@function_middleware` (same shape
as the audit middleware) that calls `agents/authz.py::authorize_tool(tool)` before each tool runs,
gating on `settings.tool_role_gates` (JSON tool→roles) with `tool_authz_default` (`allow`|`deny`).
`authorize_tool` and `authorize_trigger` share one `_has_required_role` predicate (DRY). Enforcement
is active only under `entra_required`; the expensive-trigger call stays as defense-in-depth.

**Consequence.** Default `allow` + empty gates = zero behavior change; a deployment opts into an
allowlist by config. Authorization is now uniform per tool call, superseding D-044's trigger-only
scope.

**Result.** `make lint type test` green. Tests: `test_tool_authz`, `test_agent` (two middlewares),
`test_config`.

## D-060 — F10-G: audit hash-chain + bi-temporal note fields (D-A15)

**Context.** D-034 left the audit hash-chain "for Phase 6"; `architektur.md` §10.4 proposed
bi-temporal note fields but never schematized them. Both are low-complexity, GxP-relevant.

**Decision.**
- **F10-G1:** `011_audit_hash_chain.sql` adds `prev_hash`/`row_hash` to `audit_events`.
  `PostgresAuditSink.record` computes `row_hash = chain_hash(prev_hash, event)` (reusing
  `chemclaw.ids.stable_hash`, one hashing scheme — D-033) under a transaction advisory lock so
  concurrent appends cannot fork the chain. `scripts/verify_audit_chain.py` (`make audit-verify`)
  walks the rows and reports the first broken link; legacy empty-hash rows are skipped.
- **F10-G2:** `kg/note.py` gains optional `valid_from`/`valid_to` with a validator rejecting
  `valid_to < valid_from`; retrievers may filter on them later (no premature consumer).

**Consequence.** Tampering with any audited row is detectable; notes can record what was known and
when it was valid. The `NullAuditSink` default is unaffected.

**Result.** `make lint type test` green. Tests: `test_audit_chain`, `test_note`, `test_kg_validate`.

## D-061 — F10-A: hybrid retrieval — dense + lexical entry points, RRF fusion (D-A10)

**Context.** Retrieval was graph traversal + binary structural fingerprints: no dense-semantic and no
lexical rank, so a note sharing neither a substring nor a wikilink with the query was unreachable.
This executes and extends the planned-but-unbuilt F8-T2.

**Decision.** `agents/embedding_provider.py` is the one embedding seam (`hash` offline / internal
`openai_compatible`). `report/vector_index.py` (`012_note_index.sql`) is a derived, rebuildable
pgvector + `tsvector` index over notes with in-memory + Postgres backends. `VectorRetriever` +
`LexicalRetriever` join `gather_evidence` via the F7 source registry (`vector`/`lexical` keys —
registry membership is the enable switch, D-018). `report/hybrid.py::reciprocal_rank_fusion` fuses
the per-source rankings under `retrieval_mode="hybrid"`; graph expansion stays the reasoning path
(D-004 intact — the new retrievers are *entry points* into the graph, never a replacement).

**Consequence.** Default `retrieval_mode="graph"` + `hash` embedder + `vector`/`lexical` not in
`data_sources` = today's flat union, unchanged. Git-markdown stays the source of truth; the index is
derived. A scheduled reindex activity is a documented follow-up (today `make reindex`/CLI populate).

**Result.** `make lint type test` green. Tests: `test_embedding_provider`, `test_vector_index`,
`test_hybrid_retrieval`, `test_config`.

## D-062 — F10-F: classification metrics (P/R/F1) + eval drift detection (D-A14)

**Context.** The eval harness scored green-chemistry/prediction metrics with absolute-error
tolerances; it had no precision/recall/F1 and no drift detection.

**Decision.** `evals/metrics.py` adds `precision`/`recall`/`f1` over `output.predicted_note_ids`
vs `reference.expected_note_ids`, sharing one pure `precision_recall_f1` (report/drift metrics, no
per-case gate). `evals/baseline.py` (`aggregate_metrics`/`detect_drift`, committed
`evals/baseline.json`) + `workflows/eval_drift.py::EvalDriftWorkflow` (background-jobs, alerts via
the notify seam) re-run the case-set on an opt-in Schedule and flag any metric that moved past
`eval_drift_epsilon`. Live *retriever* scoring is not re-invented here: the merge with the
audit-hardening line adopted its KM-13 gold-set (D-056) — `retrieval_recall`/`retrieval_precision`
over a committed fixture corpus — as the corpus-backed retrieval measure; the earlier
one-caller `run_retrieval_eval` driver was dropped as redundant (KISS). A pinned static
`precision`/`recall`/`f1` case (`retrieval-precision-recall.md`) keeps those generic metrics under
the versioned case-set and gives drift a number to watch.

**Consequence.** Retrieval/extraction quality is measurable as P/R/F1 on versioned cases; a silent
regression trips a scheduled alert. Over the deterministic committed case-set the scheduled job is a
deployment-consistency tripwire; live drift over the deployment's own graph stays deferred
(DEFERRED.md). Drift is off by default.

**Result.** `make lint type test` green. Tests: `test_metrics_classification`, `test_eval_drift`
(incl. a baseline-matches-case-set guard), `test_schedules`, `test_config`; the KM-13 gold-set is
pinned by `test_retrieval_eval` (D-056).

## D-063 — F10-D: sub-agent orchestration via Temporal child workflows (D-A13)

**Context.** Report-section retrieval and memory synthesis each fanned a task into independent steps
but ran them in one monolithic activity, so a single poison item failed the whole batch and there was
no per-step durability. A generic child-workflow fan-out was justified by these *two* real callers
(Rule of Three), not speculatively.

**Decision.** `workflows/orchestrator.py::fan_out(child, inputs, *, id_prefix, ...)` runs each input
as a child workflow with bounded concurrency (fixed-size batches — deterministic under replay),
per-child retry, and D-030 isolation (a child that exhausts its retries is logged and dropped, its
siblings unaffected, successful results in input order). Adopted by `ReportSectionWorkflow` (one per
section) and — after extracting the pure `build_*_notes` in `memory/jobs.py` — a shared
`PublishNoteWorkflow` (one per memory note). Orchestration stays a Temporal-layer concern; MAF remains
the single conversational agent. The conversational multi-agent mesh stays gated (trigger recorded).

**Consequence.** Report + memory synthesis now run as exactly-once child workflows with per-child
retry and worker-restart durability. Section/group logic is unchanged (still PR-gated, still cited);
only the execution topology gained parallelism + isolation. Config
`orchestrator_max_parallel_children` (default 8).

**Result.** `make lint type test` green (the Temporal-env fan-out test runs in CI, skips offline).
Tests: `test_orchestrator`, `test_memory` (builder behavior-preserving), `test_report_workflow` /
`test_workers` registration, `test_config`.

## D-064 — F10 post-implementation review cycle: verified fixes

**Context.** After F10 (A–G) landed, an adversarial review — five agent teams over the new features
and the whole codebase — surfaced real plan-vs-code and correctness gaps. The most severe: F10-B's
`verifier_confidence_threshold` was defined but never read (dead config), so the ticket's headline
*confidence routing* was not actually wired; several docstrings over-claimed behavior that did not
exist (a D-032 hold, "any deleted row breaks the chain").

**Decision.** Fixed each confirmed finding in-branch rather than deferring:
- **F10-B routing wired.** `AnswerEvent.review_required` is set when `confidence <
  verifier_confidence_threshold` — the config is now consumed and low/high confidence are
  distinguishable. Over-claiming docstrings corrected (the durable D-032 hold is deferred, not built).
  Wikilink extraction unified into `kg.note.cited_ids` (strips targets, one definition); a citation
  miss now reports the *unresolved* id, not `citations[0]`.
- **F10-D report durability.** A failed section degrades to a visible `retrieval_failed` marker
  (never silently dropped from a GxP draft); the redundant child-level `BAD_DATA_RETRY` was removed
  (the activity is the single retry boundary); `fan_out` re-raises `CancelledError` instead of
  logging it as a drop, and guards `max_parallel >= 1`.
- **F10-F drift honesty.** `detect_drift` uses a *relative* band (scale-appropriate across
  heterogeneous metrics); `DriftAlert.vanished` disambiguates an absent metric from a 0.0 score; the
  alert now rides a *must-deliver* `notify_session` (a dropped regression alert fails the run); the
  scheduled job is documented as a deployment-consistency tripwire (live-retriever drift deferred).
- **Shipped retrieval/audit.** `PostgresNoteIndex.search_dense` now applies the positive-similarity /
  zero-vector guard the tested InMemory reference already had (backends no longer diverge);
  `GraphRetriever` uses the shared `note_text` haystack; RRF is 1-based (canonical); the audit chain
  gained a genesis anchor (catches prefix truncation) with docstrings corrected to the true guarantee
  (tip truncation needs an external count anchor — deferred).

**Consequence.** Two of F10-B's three CHECKMATE claims that were aspirational in the merged code are
now real (routing) or honestly deferred with a trigger (report prose verification). Three deferrals
are recorded in DEFERRED.md (F10-B3, live-retriever drift, audit tip-truncation anchor).

**Result.** `make lint type test` green. New/updated tests: `test_runner` (threshold routing),
`test_report`/`test_report_workflow` (failed-section marker), `test_eval_drift` (relative band +
`vanished`), `test_audit_chain` (prefix-truncation), `test_memory_jobs` (fan-out registration),
`test_config`.
