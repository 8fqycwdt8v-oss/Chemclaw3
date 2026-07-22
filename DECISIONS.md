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

## D-039 — Phase 6, part 1: a `Principal` identity + role-scoped skills (offline core)
Phase 6 (identity/RBAC) is mostly infra-gated — real Entra JWT validation, OAuth-proxy/OBO,
Temporal mTLS, and the HPC bridge all need a live tenant/cluster to build and test (see
`SECURITY.md`). This lands the part that is fully implementable and unit-testable **offline**,
and that the rest hangs off: a validated caller identity and role-scoped skill visibility (plan
step 6.2).

- `chemclaw/identity.py`: `Principal` — a frozen model of the caller's Entra `oid`/`upn` +
  app-roles/groups. `principal.actor` (the `oid`) is the stable id the GxP audit trail records.
  Immutable so a downstream tool/middleware can't quietly change who it acts as.
- `agents/skill_access.py`: the placeholder `RoleFilteredSkillsSource` (name-set filter) becomes
  `RoleScopedSkillsSource` — a config-driven gate (`settings.skill_role_gates`: skill name →
  allowed roles). Ungated skills stay visible to all (empty map = today's behavior); a gated
  skill is hidden from an anonymous caller and from one lacking its role. One thin decorator
  over any `SkillsSource` (DRY).
- `agents/chemclaw_agent.py`: `build_agent(principal=…)` replaces the unused `allowed_skills`
  seam. A verified principal's `oid` becomes the audit actor and its roles scope skills;
  anonymous/dev (no principal) keeps `actor="unknown"` and every skill visible.

**Deliberately NOT here (the next, infra-gated increment):** the *enforcement* of authorization
on expensive actions (`submit_qm_job`, calculators, BO). `architektur.md` §8 says authz belongs
in the MCP server, but those actions are in-process agent tools (not MCP) by an earlier KISS
decision — so *where* the enforcement point sits (a policy middleware at the in-process tool
boundary vs. moving protected tools behind an authz'd MCP server) is an open architectural
choice, left for a follow-up rather than pre-judged here. Token validation itself (JWKS/OBO)
also waits for the live tenant. This increment adds identity + visibility, not gate-keeping.

## D-040 — Phase 6, part 2: tool authorization at the in-process tool boundary (option a)
`architektur.md` §8 centralizes authorization where the caller's token is present — the MCP
server. But the expensive actions (`submit_qm_job`, calculators, BO) are **in-process agent
tools**, not MCP tools (a deliberate KISS decision, D-029 moved only fingerprint search to MCP).
Rather than move them behind an MCP server just to gate them (a large change), authorization is
enforced by a MAF **function middleware** over the in-process tool boundary — the same seam the
audit trail already uses (D-027). One policy over every tool (DRY), the "one place" §8 asks for,
adapted to where the tools actually run.

- `agents/authz.py`: `authorize(principal, tool, gates)` (pure) + `make_authz_middleware`. A tool
  is gated by config (`settings.tool_role_gates`: tool → allowed roles). Ungated tools run for
  anyone; a gated tool runs only for a caller holding one of its roles; an anonymous caller (no
  principal, no roles) is denied. Denial raises `ToolNotAuthorizedError`.
- `agents/chemclaw_agent.py`: `build_agent` wires `middleware=[audit, authz]` — audit **first =
  outermost**, so a denied attempt is recorded in the GxP trail (outcome "error") before the
  error returns to the model. The authz middleware is added **only when `tool_role_gates` is
  non-empty**, so with no gates the default path is byte-for-byte unchanged (audit-only).

Enforcement is opt-in by config, mirroring `skill_role_gates` (D-039): turning on a gate is an
admin change, not code. What still needs live infra: validating the Entra JWT that produces the
`Principal` in the first place (JWKS/OBO), Temporal mTLS, and the HPC bridge — those remain the
infra-gated tail of Phase 6.

## D-041 — Phase 6, part 3 (offline slice): Entra token validation → Principal
The identity that parts 1–2 consume (D-039/D-040) is *produced* by validating the caller's Entra
JWT. The validation logic is standard and testable offline; only the signing-key source (the
tenant JWKS endpoint) is live. So `chemclaw/auth.py::TokenValidator` takes a pluggable key
resolver: `validate(token)` verifies the RS256 signature, `aud`, `iss`, and `exp`, then maps the
Entra claims (`oid`, `preferred_username`/`upn`, `roles`, `groups`) to a `Principal`; a token
failing any check — or lacking `oid` — raises `TokenValidationError`. `TokenValidator.for_entra(
tenant_id, audience)` wires the live `PyJWKClient` (the network edge); tests inject a static
public key and exercise the whole decision with a synthetic RSA keypair (valid → Principal; bad
signature / wrong audience / expired / no-oid → rejected).

Dependency added: `pyjwt[crypto]`. Config: `entra_tenant_id`/`entra_audience`/`entra_jwks_url`
(empty defaults — the live validator is built only where a front door authenticates a request).
Still infra-gated (the true tail of Phase 6): calling `for_entra` from a real entrypoint, the MCP
OAuth-proxy/OBO flow, Temporal mTLS, and the HPC bridge — none buildable without a tenant/cluster.
