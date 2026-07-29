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

## D-058 — Prove the harness loop live; close the F3-T3 awaiting-todo deferral

**Context.** D-040 wired MAF's harness (`TodoProvider`/`AgentModeProvider`/`AgentLoopMiddleware`),
but every test built it with a dummy `object()` client — construction was proven, the loop itself
never actually ran. Separately, F3-T3 shipped job→session push-back but explicitly deferred
"flipping the harness `awaiting` todo on completion" (`BACKLOG.md`) because it needed the loop
exercised live to get the mutation right, not guessed at in the abstract.

**Decisions.**
- **A real scripted chat client, not a mock of the loop.** `tests/test_harness_execution.py` adds
  `ScriptedChatClient(FunctionInvocationLayer, BaseChatClient)` — the same base classes every
  concrete MAF client composes — whose replies are a fixed script. `build_agent(chat_client=...)`
  wires it through the *actual* harness path (`_build_harness_agent`), so `TodoProvider`,
  `AgentModeProvider`, `AgentLoopMiddleware`, and `todos_remaining` all run for real: the scripted
  model adds todos, the loop re-invokes it while any remain open (reading real todo-store state,
  not a stub), completes them one by one, and the loop stops itself. Three cases proven live:
  `execute` autonomy loops a two-step plan to completion; `plan_only` autonomy produces the plan and
  genuinely stops (not just a different `default_mode` value); `harness_max_loop_iterations` caps a
  todo the model never finishes. Nothing about the loop or todo store is faked — only the model.
- **`agents/harness_todo.py`: the awaiting-job bridge, scoped to what's actually buildable today.**
  `mark_awaiting_job`/`complete_awaiting_job` operate directly on MAF's `TodoSessionStore`.
  `TodoItem` has no field for an arbitrary job id, so the link is a description-string convention —
  never model-authored: `submit_qm_job` creates the "awaiting" todo itself right after Temporal
  hands back a job id, so the match is exact-string. On the `job_completed` push-back
  (`service/app.py`'s `/sessions/{id}/events`), the live session is looked up in `_LiveSessions` and
  the matching todo is flipped complete. This closes exactly what F3-T3 deferred.
  - **Not attempted:** resuming the *same* streamed turn while the job is still running. That needs
    deciding how a new turn gets triggered server-side with no client request in flight — genuinely
    open (`docs/harness-konzept.md` §4, and the F1 backlog's `awaiting`-state-resume follow-up) and
    not guessed at here. The flipped todo is picked up on the session's *next* turn instead.
  - A fresh submit marks awaiting; a re-submit that hits `WorkflowAlreadyStartedError` (an
    already-running *or already-completed* job, D-011) does not — marking again for an
    already-completed job would create a todo no future push-back will ever flip, blocking
    `todos_remaining` forever.
  - Gated on `settings.harness_enabled` at both ends (submit and completion) and on the ambient live
    session being present, so the classic (default) agent path never writes to a todo list nothing
    reads, and the CLI (single-shot, no `AgentSession`) is an inert no-op.
- **New ambient: `agents.session_context.get_current_session`/`set_current_session`.** A second
  contextvar alongside the existing session-id one, carrying the live `AgentSession` object —
  needed because `TodoSessionStore` operates on `session.state`, not reachable from the id alone.
  Kept separate rather than changing the id ambient's contract so every existing id-only consumer is
  unaffected. `service/runner.py::run_turn` sets/resets it alongside the id, same turn lifecycle.

**Result.** `make lint type test` green. New tests: `test_harness_execution.py` (3, the live loop),
`test_harness_todo.py` (4, the bridge in isolation), plus wiring tests in `test_qm_tools.py` (3) and
`test_service.py` (2). No changes to `agents/chemclaw_agent.py` — the harness wiring from D-040 was
correct as built; this proves it and closes the one deferral that was actually gated on doing so.

## D-059 — F10-E/B: per-task model routing + answer verification & confidence routing (D-A11)

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

## D-060 — F10-C: per-tool authorization middleware (supersedes D-044 scope, D-A12)

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

## D-061 — F10-G: audit hash-chain + bi-temporal note fields (D-A15)

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

## D-062 — F10-A: hybrid retrieval — dense + lexical entry points, RRF fusion (D-A10)

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

## D-063 — F10-F: classification metrics (P/R/F1) + eval drift detection (D-A14)

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

## D-064 — F10-D: sub-agent orchestration via Temporal child workflows (D-A13)

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

## D-065 — F10 post-implementation review cycle: verified fixes

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

## D-066 — Resilience hardening: DB-query clamps, session reattach, turn/token budgets

**Context.** A review against four failure modes seen in another agent system (no memory on restart,
no idempotency, no budget, unbounded DB queries) found Chemclaw already covers idempotency (D-011
content-addressed cache + workflow-id dedup) and durable job execution (Temporal), but left three
concrete residual gaps: (1) an agent-supplied `top_k` and the substructure scan could issue an
unbounded query/full-table load — the closest analog to "no row limits"; (2) the front door held
live sessions only in an in-process LRU, so a pod restart forced returning clients onto new sessions,
orphaning their durable history + unconsumed push-back; (3) a single turn is iteration-capped but
nothing capped the *number* of turns, leaving cumulative LLM spend unbounded (the "$400 loop").

**Decision.** Closed all three on the feature branch, each config-gated to preserve today's behavior:
- **DB clamps (#4).** `find_matches` clamps a model-supplied `top_k` to `[1, fingerprint_max_top_k]`
  (default 100) — the fingerprint-search analog of the existing `graph_max_hops` clamp, applied at
  the one DRY chokepoint both similarity entry points share. `all_records(limit)` gained a bounded,
  id-ordered variant; `find_substructure_matches` scans at most `substructure_scan_max_records`
  (default 5000) and **logs a warning when it truncates** (no silent cap). The universal 30s
  `statement_timeout` remains the time backstop; these bound rows materialized into the worker heap.
- **Session reattach (#1).** New `session_owners` table (migration 013) + `SessionOwnerStore` record
  one durable identity row per session at creation. On a live-cache miss the front door looks the
  owner up, authorizes the caller, and rebuilds the live handle over the same durable history
  (`PostgresHistoryProvider` reloads the thread on first use). Gated on `session_store="postgres"`
  (rehydration is meaningful only with durable history); under the in-memory store a miss stays a
  404 (unchanged). Owner-scoped: a different user still gets a 404, no existence leak. This is the
  *front-door restart-reattach* gap, distinct from the still-open mid-flight same-turn resume
  (BACKLOG "resuming the same streamed turn mid-flight", D-032/D-035 seam).
- **Turn/token budgets (#3).** New `service.budget.BudgetTracker` meters each turn's reported token
  usage (`UsageDetails` from MAF's usage content) and counts turns per session and per user; the
  front door refuses a turn (HTTP 429) that would exceed a cap, before taking an admission permit.
  Five config knobs (`budget_enabled` + per-session/per-user turn+token caps, 0 = unlimited), off by
  default. In-process and best-effort — the missing ceiling above the per-turn loop cap; it partly
  closes the AG-15 "per-user quota" deferral (in-process now; durable rolling-window still deferred).

**Consequence.** The three residual gaps are closed for a running deployment. Two conscious
deferrals recorded in DEFERRED.md: a durable rolling-window budget quota (survives restart /
multi-pod), and the substructure pattern-fingerprint prefilter (sound screening at ~10⁴+ molecules).

**Result.** `make lint type test` green (490 passed, 34 infra-skipped). New tests: `test_budget.py`
(tracker caps + usage metering), `test_service.py` (rehydration, owner-scoping, 429 over budget),
`test_molfp.py` (top_k clamp, bounded scan + truncation warning), `test_session_store.py`
(`SessionOwnerStore` round-trip), `test_config.py` unaffected.

## D-067 — Fail-closed startup: unauthenticated + network-exposed refuses to boot

**Context.** With `entra_required=false` every request runs as the shared dev principal and every
authorization gate is open (SEC-2). The default bind is `service_host="0.0.0.0"` (correct inside a
container behind the OpenShift Route), so the *default* combination — no auth, all interfaces —
was a network-exposed, gates-open deployment guarded only by a startup WARNING log line. A missed
log line is not a security control; the earlier sign-off ("warn and still boot") predated the F4
identity work that made `entra_required=true` the sole production posture.

**Decision.** `create_app` now *refuses to boot* (`RuntimeError` with an actionable message) when
`entra_required` is false and `service_host` is non-loopback. Two escapes, both explicit: bind a
loopback interface (the local dev flow, unchanged), or set the new `service_allow_insecure=true`
(default false), which boots and keeps the loud warning — making an exposed unauthenticated
deployment a conscious, greppable decision instead of a default. Entra-enforced deployments are
untouched.

**Consequence.** A deployment that forgets `CHEMCLAW_ENTRA_REQUIRED=true` now fails at startup
with the fix in the error message, rather than serving the network with authorization gates open.

**Result.** Tests pin all four postures (loopback+no-auth boots; exposed+no-auth refuses;
exposed+no-auth+opt-in boots with warning; exposed+enforced boots clean); the in-process test
suite uses the loopback posture via an autouse fixture. `make lint type test` green.

## D-068 — Write tools are role-gated by default (DEFAULT_WRITE_TOOL_GATES)

**Context.** Per-tool RBAC defaulted to `tool_authz_default="allow"`: any tool without an explicit
`tool_role_gates` entry — including job launchers and state-mutating tools — was callable by every
authenticated user. Flipping the global default to `deny` would break the dev flow and every read
tool.

**Decision.** `agents/authz.py` gains `DEFAULT_WRITE_TOOL_GATES`, the built-in set of
write/side-effect tools (`submit_qm_job`, `propose_knowledge_note`, `record_confirmed_answer`,
plus `index_molecule`/`index_reaction` as defense-in-depth behind the D-029 `allowed_tools`
boundary). Under `entra_required`, an *unconfigured* tool in this set requires a role from
`entra_privileged_role_set` — reusing the F4-T5 privileged set rather than inventing a second role
vocabulary — and fails closed when that set is empty. An explicit `tool_role_gates` entry
overrides the built-in gate; read tools keep the `allow` default; dev mode is unchanged. The
constant lives in `authz.py`, the one home for authorization decisions.

**Consequence.** Secure by default: an enforced deployment can no longer expose writes by
forgetting to configure a gate. A new write tool must be added to the set when registered — a
hand-maintained list, acceptable at the current tool count.

**Result.** `tests/test_tool_authz.py` proves: default-gated write denied/allowed by privileged
role, fail-closed on an empty privileged set, operator override wins, read tools and dev mode
unchanged.

## D-069 — Submitter checkout ownership enforced with an OS-level advisory lock

**Context.** `GitNoteSubmitter` serializes submissions with a module-level `asyncio.Lock`, but that
lock is per-process: two processes sharing `settings.note_repo_dir` would interleave `checkout -B`
calls and silently corrupt each other's note branches. The "dedicated clone per process" rule was
documented, never enforced.

**Decision.** Every submission additionally holds an exclusive non-blocking `flock` on
`.git/chemclaw-submit.lock` inside the checkout for its full duration. A second process gets an
immediate `GitSubmitError` ("note_repo_dir is in use by another process") instead of corruption.
The lock file lives under `.git/` because `submit()` now runs `reset --hard` + `clean -fd` before
each submission (itself a fix: staged residue from a failed submission no longer leaks into the
next note's branch) — deleting a held lock file would let a new process lock a fresh inode at the
same path and break mutual exclusion. The asyncio lock stays for in-process serialization.

**Consequence.** Misconfiguration (two workers, one clone) is now a loud, actionable error, not a
data-integrity incident. flock is advisory: out-of-band git use in the clone remains outside the
contract; the kernel releases the lock if the holder dies (no stale-lock cleanup needed).

**Result.** Cross-process denial proven with a real child process holding the flock
(`tests/test_knowledge.py`); lock release after a failed submission proven too.

## D-070 — ELN sync cursor semantics: future-tolerance clamp, overlap window, chunked activities

**Context.** Three independent failure modes could silently stall or starve ELN ingestion: (1) one
future-dated entry timestamp became the persisted high-water cursor, permanently skipping all later
real entries; (2) an export file landing *after* a newer-stamped sibling was dropped forever by the
`created_at >= since` filter; (3) the sync activity ingested an unbounded backlog in one 300s
attempt with no heartbeat.

**Decision.** The cursor still advances past sane-timestamped rejections (re-fetching
deterministic bad data only re-rejects it), but entries stamped beyond wall clock +
`eln_sync_future_tolerance_seconds` are rejected *without* cursor advance; every fetch reaches
`eln_sync_overlap_seconds` behind the cursor (idempotent ingestion makes re-fetch free) with the
cursor floored at `since`; and the activity heartbeats and ingests in cursor-persisting chunks of
`eln_sync_batch_size`, capping only past-cursor entries so a truncated chunk strictly advances.
Relatedly, memory note ids now anchor on the cluster's *smallest member* rather than the full
member set, so a grown cluster supersedes its note in place through the PR-gate instead of minting
a duplicate note per sync.

**Consequence.** A typo'd year, a late-landing export, or a large backfill each degrade to a
visible per-run warning and bounded catch-up work instead of silent permanent data loss.

**Result.** Behavior tests in `tests/test_eln.py`, `tests/test_eln_workflow.py`,
`tests/test_memory.py`; chunk-resume proven with cursor persistence per chunk.

## D-071 — Deterministic config capture in workflows; idempotent session events

**Context.** Two at-least-once/replay correctness gaps: `fan_out` read live
`settings.orchestrator_max_parallel_children` inside workflow code (replay after a config change
sees a different batch structure), and `record_session_event` had no idempotency key (an activity
retry after a committed-but-unacked insert duplicated the notification).

**Decision.** Workflow code must never read live settings where the value shapes command
structure: such values are captured once via a local activity (`resolve_fan_out_limit`) so replay
sees the recorded value. Settings reads that only shape command *attributes* (timeouts, queue
names) remain acceptable. Push-back events recorded from activities carry a deterministic
`dedupe_key` (`workflow_id:run_id:kind:payload-digest`) enforced by a partial unique index
(`infra/sql/014`), converting at-least-once retries into exactly-once notifications; NULL-key
writers keep plain append semantics.

**Consequence.** Replays are structurally deterministic under config drift, and duplicate
notifications from activity retries are impossible for workflow-recorded events.

**Result.** Proven against real Postgres (duplicate-insert dedupe) and via worker-registration +
history tests. `make lint type test` green.

## D-072 — CHECKMATE campaign 2026-07: adversarially-verified review, hardening, and refactor pass

**Context.** A full-codebase review campaign (13 reviewer agents; 73 raw findings; every finding
adversarially verified by an independent skeptic, 23 refuted; 50 confirmed + 10 of 11
orphaned-verifier findings confirmed by the orchestrator) followed by per-package fix waves. No S1
found; 13 S2 correctness bugs, the rest hardening/simplification.

**Decision (highlights beyond D-067…D-071).**
- *Chemistry correctness*: `run_xtb` rejects charge/SMILES-formal-charge mismatches and
  odd-electron species (fail-fast beats a silently guessed doublet — SMILES carries no spin);
  `predict_pka` rejects net-charged inputs (v1 calibration is neutral-acid-only); cached compute
  runs on the same canonical form its cache key hashes; `engine_version()` embeds the RDKit build
  so geometry/descriptor stacks invalidate stale cache entries per D-011.
- *Retrieval correctness*: one eligibility gate (`_eligible_notes`: type/tag + KM-7 currency)
  feeds graph, vector, and lexical retrieval; filters push into the index query
  (`NoteIndex.search_*(within=…)`) so top-k slots are never spent on ineligible neighbors; graph
  hits rank best-first for RRF; index scores survive into evidence chunks.
- *Layering*: the embedding seam moved to `chemclaw/embeddings.py`; `report/` depends only on the
  kernel, enforced by `tests/test_layering.py` (fresh-interpreter import guard). `Settings` was
  restructured into 18 cohesive mixin sections with zero call-site churn (160 fields byte-identical).
- *Front door bounds*: per-session turn serialization (409 on concurrent POST), streamed turns
  bounded by `service_turn_timeout_seconds`, per-user SSE stream cap, one connection per stream,
  kind-scoped event claims, LRU-bounded budget counters, JWKS validation off the event loop.
- *Config fail-fast*: Entra enforcement requires a resolvable JWKS source; nextflow completeness
  and poll-vs-heartbeat pairs validated at startup; `_redact` strips passwords from all libpq DSN
  forms before they can reach persisted error messages.

**Consequence.** The review's verified-findings queue is fully drained (59 fixed, 1 refuted);
coverage rose from 88.43% to 89.60% with 108 new behavior tests (616 passing), all proven against
real RDKit/BoFire/Postgres where applicable.

**Result.** Branch `claude/code-review-refactor-plan-wm34wc`, commits `2e7148c`…`4afbada`;
`make lint type test` + `make cov` green at every landed cluster.

## D-073 — Final adversarial diff pass: campaign-introduced defects caught and fixed

**Context.** After all fix waves landed green, one adversarial reviewer re-read the entire branch
diff (90 files) hunting only for defects the campaign itself introduced or left: regressions,
incomplete fixes, cross-agent seam inconsistencies, and dishonest tests.

**Decision (findings → fixes).** Nine confirmed: (1) **S2** — the new submitter reset/clean plus
the `note_repo_dir="."` default could destroy a developer's own working tree; `submit()` now
refuses the process's CWD/checkout root before any destructive git command. (2) deny-mode authz
inversion — the built-in write gate now only narrows `allow`, never widens `deny`. (3) ELN
overlap×chunk amplification — merged-note short-circuit, first-chunk-only overlap, DEBUG replay
logging (sync trail INFO line now also reports `skipped_existing=K`). (4) `CampaignSpec` read
live config inside a Temporal-crossing validator — ceiling moved to the creation entry point so
replays cannot fail on config drift. (5) drift-eval memo keyed on paths — corpus stat signature
added. (6) `except Exception` counter cleanup — BaseException-safe try/finally for turn and
stream slots. Plus docs: workflow-versioning policy recorded in BACKLOG (no live histories yet),
memory-id one-time migration note, stale `entra_client_id` references removed. SQL surface,
artifact-token scoping, flock semantics, budget LRU, charge-validation coverage, and test honesty
were probed and confirmed sound.

**Consequence.** The campaign's own changes went through the same skeptic gauntlet as the code it
reviewed; the one severe self-introduced risk (dev-tree destruction) was caught before merge.

**Result.** Final gate: lint + mypy strict clean; 625 passed / 17 Temporal-only skips; coverage
89.64% (baseline 88.43%). Branch `claude/code-review-refactor-plan-wm34wc`.

## D-074 — Compared against Google's Open Knowledge Format (OKF v0.1): design reaffirmed, two follow-ups queued

**Context.** OKF (a git-native markdown format Google open-sourced: no cloud account/SDK, an
AI agent as the "wiki librarian" that keeps docs in sync, explicit `[[concept_path]]` links as
a deterministic graph instead of cosine-similarity RAG, a hybrid router splitting core/precise
truths from a wide RAG-searched archive) was checked against the knowledge-graph design already
built here.

**Finding.** D-004/D-005 independently arrived at the same three pillars: git-native Markdown
notes (no graph DB), agent-authored/updated content gated through a PR-gate rather than trusted
blind, and `[[wikilink]]`-driven `NetworkX` graph traversal in place of top-k vector similarity
(D-004's rationale predates and matches OKF's). The hybrid-router split (deterministic bundle for
core truths vs. RAG for wide/archival search) is already our shape too: the graph is the
default retrieval path (D-004), embeddings are only an optional entry point (D-062 hybrid
retrieval — RRF fusion over `vector`/`lexical`/graph, graph traversal stays the reasoning path).
No architecture change follows from this comparison.

**Two OKF conventions queued as backlog, not adopted here.** (1) OKF bundles keep a per-bundle
`log.md` audit trail; we currently only have PR/git history, no explicit per-note-type changelog
— worth a small addition. (2) OKF's format is deliberately untyped bare `[[links]]`; our
frontmatter `type` field is a string with no controlled vocabulary or class hierarchy, so an
agent cannot query by subsumption (e.g. "all electrophilic aromatic substitutions" matching a
`reaction_class: acetylation` note). Rather than building an in-house OWL/RDF ontology (no
second caller yet — KISS), the queued move is to anchor existing external ontology IDs (ChEBI
for compounds, RXNO for reaction classes) as additional frontmatter fields, reusing controlled
vocabularies instead of owning a schema. Both tracked in `BACKLOG.md` under "OKF-inspired
graph polish"; neither is scheduled against a phase yet.

## D-075 — Config-extensibility: `@tool` registry + `AgentProfile` seam (audit doc 10, items 2–3)

**Context.** `docs/audit/10-config-extensibility.md` found the five extension seams at wildly
different maturity: tools were the weakest (a hardcoded `_capability_tools()` list — the one seam
forcing an orchestration-code edit), and per-use-case agent configuration was absent (one global
`build_agent`). The substrate verdict was to evolve additively with existing in-repo idioms, not
adopt any out-of-tree plugin framework (entry-points/pluggy/Django-apps).

**Decision.** Two seams landed, each mirroring an idiom already in the repo:
1. **Tool registry** (`agents/tool_registry.py`): a `@tool` decorator + name-keyed `_REGISTRY` with
   a duplicate-name guard — the exact shape of `evals.metric`. Tools register at their definition
   site; `_capability_tools()` assembles `[*registered_tools(), *_mcp_capability_tools()]`. The MCP
   capability path stays config-driven, and the shared `[audit, enforce_tool_authz]` middleware
   still wraps the assembled toolset — collection changed, gating did not.
2. **`AgentProfile` seam** (`agents/profiles.py`): a small pydantic spec + one-entry `{name: profile}`
   registry (mirroring `sources.registry`/`config.McpServerSpec`). `build_agent(profile=…)` resolves
   `None`→global default, narrows the tool/MCP surface, and swaps instructions/harness. Every
   override field is `None`-defaulted so the `"default"` profile reproduces today's agent verbatim
   and `profiles.py` imports neither `chemclaw_agent` nor `settings` (no cycle, no second config).

**Invariant preserved.** A profile *attenuates, it never authorizes*: the narrowing happens before
the unconditional audit + per-tool authz middleware and the skill role-gates, so a profile can
remove capability but never bypass RBAC or the PR-gate. An unknown tool/MCP name in a profile is a
build-time error, not a silently-empty surface (fail-fast, matching the config `@model_validator`s).

**Deliberate deviations from the spikes (KISS / Rule of Three).** Spike 1's `agent_facing` flag was
dropped — no hidden in-process tool exists today, so the flag would be a speculative param; add it
when a second, non-advertised tool appears. No `make tool-validate` target was added — name drift is
already guarded by `tests/test_agent.py::test_instructions_only_name_available_tools` plus the
registration guard, so a separate CLI gate would be redundant churn.

**Staging.** Profile Stage 2 (front-door `POST /sessions` selection) and Stage 3 (filesystem-discovered
profiles) remain deferred until a **second real use case** forces them (BACKLOG). The `DataSourceSpec`
discriminated union (audit item 4) landed subsequently — see D-076.

## D-076 — Config-extensibility: `DataSourceSpec` discriminated union (audit doc 10, item 4)

**Context.** The data-source seam (`sources/registry.py`) was structurally good — `{name: factory}` +
one config token — but had **no per-*instance* config**: a "type" was just a registry key bound to a
factory reading flat globals, so the single global `eln_export_dir` served every JSON-ELN source and
two instances of one type (prod + staging, different directories) were impossible (audit §2.3). The
audit (§5) recommended a scoped pydantic discriminated union carrying per-instance config, additive
to the comma-string token, reusing two in-repo idioms (`config.McpServerSpec` typed list +
`bo/problem.py:57`'s `Field(discriminator=…)`).

**Decision.** `DataSourceSpec = Annotated[JsonElnSourceSpec | OrdElnSourceSpec, Field(discriminator="type")]`
in `config.py` (beside `McpServerSpec`), plus an additive `data_source_specs: list[DataSourceSpec]`
token in `SourcesSettings`. `sources.registry.build_data_source(spec)` dispatches `type → adapter`,
each variant nesting its own `export_dir`. `_active_sources()` now builds the comma-list sources then
the spec sources; consumers (`gather_evidence`, the ELN sync) are untouched — they still iterate built
`DataSource`s. Keyless/default sources stay in the comma list (no regression).

**Temporal boundary kept string-keyed.** `sync_eln_entries(source: str)` still calls
`make_data_source(name)`; that resolver now falls through to spec-by-name after the built-in keys, so
in-flight workflow histories stay byte-identical (durability > signature elegance, audit §5).

**Real second caller, no stub.** Both ELN adapters already accept an `export_dir` constructor arg, so
the two variants expose an existing, working parameter per-instance — delivering the "two instances /
different dirs" capability with **zero** speculative code. The Snowflake connector (nesting
connection/credential-ref/schema-mapping config, the first `exchange_obo` caller) stays deferred and
joins as one more variant + one `build_data_source` branch when it lands (DEFERRED.md).

**Deliberate deviation from audit §5 (KISS / DRY).** Dropped the proposed near-empty
`RegisteredSourceSpec` bridge variant: it would duplicate the comma-string token (the §2.4 "two ways
to configure a list" friction) and introduce double-build/collision ambiguity between the two tokens.
The two real ELN variants already make it a genuine discriminated union, so the bridge variant was
ceremony without a caller.

**Invariant preserved (fail-fast).** Names are unique across both tokens (a shared name = a shared
`sync_cursors` row, so one cursor could skip the other's entries) — a startup `@model_validator`; and
a spec reusing a built-in registry key (which `make_data_source` resolves first, silently shadowing
the spec) is a loud error in `build_data_source`, not a sync-time surprise. RBAC/audit/PR-gate are
untouched — the seam only changes how a `DataSource` is *built*, never how its ingest is gated.

## D-077 — The turn stream emits its plan and its job launches (F2/F3 deferred item closed)

**Context.** `service/events.py` defines seven turn events; the web surface renders all seven; two —
`PlanEvent` and `JobStartedEvent` — were emitted by nothing since F2-T3 (ADR D-042 recorded the
deferral). The practical effect: a chemist who asked for a QM calculation saw silence between their
message and the answer, learning about the job only when its completion pushed back (F3-T3, possibly
a turn later); and the harness's plan — the whole point of an autonomous plan/execute backbone — was
invisible while it executed. Dead types also violate the repo's "no 'for later' stubs" rule: the
choice was emit or delete.

**Decision — emit.** Both inputs now exist offline, so emitting is the smaller diff than deleting a
contract two surfaces already render.

- **`JobStartedEvent`** — `agents/job_events.py`: a per-turn contextvar sink (`set_job_sink` /
  `announce_job_started` / `drain_started_jobs`), the same carrier and rationale as
  `agents/session_context` (task-local, so concurrent turns never cross; absent off the request path,
  where announcing to nobody is a no-op). `submit_qm_job` announces right where it already marks the
  awaiting todo; `run_turn` drains between streamed updates and once after the stream, so a launch in
  the closing update is not lost. A plain list, not a queue: the runner drains synchronously and
  nothing ever awaits it.
- **`PlanEvent`** — `agents.harness_todo.todo_titles` renders the todo store as `[x]`/`[ ]` lines
  (the read side beside the two existing mutators, so all todo-store access stays in one module);
  `run_turn` emits it only when the list *changed* since the last emission, so an unchanged plan does
  not flood the transcript.

**Only a genuine launch is announced.** The idempotent re-submit branch (`WorkflowAlreadyStartedError`)
returns an existing — possibly already completed — job id, which will never emit a matching
`job_completed` push-back; announcing it would leave a permanently "running" row in the UI. This is
the same reasoning that already governs the awaiting todo, kept consistent.

**A plan is a view, never a risk to the turn.** Off the harness path `_current_plan` returns `None`
rather than `[]` (an empty checklist reads as "the agent has no plan", not "this agent does not
plan"), and a malformed todo state is logged and skipped. No plan read can fail a turn.

**Not addressed (still open).** Resuming the *same* streamed turn mid-flight when a job completes
(the D-032/D-035 durable-approval seam) is untouched — this ADR makes the launch visible, not the
turn resumable.

## D-078 — Memory notes are retired when their cluster merges or shrinks

**Context.** `memory.ids.stable_id` anchors a campaign/playbook/optimization note on its cluster's
*smallest* member id (D-070). That is exactly right for **growth** — a grown cluster re-mints the
same id, so periodic re-synthesis updates the note in place through the idempotent PR-gate branch —
and silently wrong for two other transitions. On a **merge**, two clusters become one whose anchor
is one of the two old anchors, leaving the *loser's* note in the graph as a current account of a
subset that no longer exists. On a **shrink** (the anchor member drops out), a new id is minted and
the pre-shrink note stays current beside it. Either way retrieval can serve a stale note as fact,
with nothing linking it to what replaced it — the failure the bi-temporal fields exist to prevent.

**Decision.** `memory/supersede.py::supersede_updates(new_notes, existing, as_of)` — pure — returns
retired copies of merged notes this run replaced: same type as the run's output, an id the run no
longer mints, no `valid_to` yet, and at least one cited member now covered by a new note. Each copy
gets `valid_to = as_of` (`Note.is_current` then drops it from current-evidence sweeps; the note is
never deleted — it stays in Git, reachable by id) and a body line naming its successors.

**Applied in the builders, not at the publish sites.** `memory/jobs.py::_with_supersedes` wraps all
three `build_*_notes` functions, so the in-process job and the durable activity both get it and
neither can forget; the retirement then travels the *same* PR-gate/fan-out path as every other
memory note — no second write path.

**Overlap, not equality, and `valid_to`, not `is_current`.** Overlap catches merges (all members to
one successor) and splits (members to several) alike. Testing `valid_to is None` rather than
`is_current(as_of)` makes the job idempotent — a second run cannot re-close, and re-append its
marker line to, a note it already closed — and still covers a note whose validity begins in the
future (closed at its own `valid_from`, never before it, so the F10-G2 window check holds).

**The successor is plain text, not a `[[wikilink]]`.** The successor is an unmerged proposal from
the same run, so a link would dangle and fail `kg-validate` if a reviewer merged the supersede PR
first — an ordering trap for a human, in exchange for an edge nothing traverses (a non-current note
is already out of retrieval).

**Side effect that closes a manual chore.** BACKLOG recorded a one-time hand-cleanup for notes
minted under the older set-derived ids. Such a note intersects its successor's members under a
different id, so the first run after this ships retires it automatically.

## D-079 — Workflow versioning is a deploy checklist, not a CI guard

**Context.** Temporal replays workflow code against recorded history, so a control-flow change
deployed while a run is in flight fails that run with a nondeterminism error — surfacing after the
fact, on an unattended workflow, pointing at the new code rather than at the deploy. The 2026-07
campaign changed workflow logic (fan_out's local activity, `ElnSyncWorkflow`'s chunk loop, BO
activity seed args) with no `workflow.patched()` gates, which is safe only because no live cluster
holds Chemclaw histories yet. That safety expires at the first production deploy.

**Decision.** `docs/workflow-versioning.md` states the policy: what counts as a logic change (the
replayed command stream — activity/child calls, their arguments, type names, timers, loop bounds
and branch conditions) versus what does not (activity *bodies*, docstrings, logging, code no
workflow calls); the two sanctioned responses (`workflow.patched()` with a stable id and a planned
`deprecate_patch` retirement, or pausing the Schedules and draining in-flight runs as an explicit
deploy step); and a checklist for the release ticket. Cross-linked from `deploy/README.md` and the
runbook. Today's un-gated changes need **no retroactive patches** — gating them would add permanent
branches for a case that cannot occur without histories.

**Consequence, already applied.** The deferred `QMJobWorkflow` → `CalculationWorkflow` rename is
**dropped**, not deferred: a workflow type name is part of history, so renaming a class in place is
exactly the change this policy forbids — a cosmetic gain for a migration window.

**No CI guard, deliberately.** A check that fails a PR touching `workflows/*.py` without a
`workflow.patched()` call cannot distinguish a docstring edit from a reordered activity call, so it
would fire on nearly every PR; a check that is wrong most of the time trains its own bypass and
takes the real signal with it. `InteractionApprovalWorkflow`'s 7-day human hold is the concrete
reason draining is not always available, so the patch path stays the default. Revisit only if a real
incident shows the checklist being skipped.

## D-080 — Chemical safety: a deterministic, advisory structural screen (never a clearance)

**Context.** The last remaining capability gap the user had parked *for a decision* rather than
deferred. Its own precondition — "decide scope before any capability phase that could propose a
hazardous route or procedure" — was already past: BO recommendations (1d.5) and development reports
(5b) publish agent-authored procedures today, and no hazard logic existed anywhere in the tree (only
prose cautions in two `SKILL.md` files). Unlike every other open capability item, this one is not
infra-gated: it can be built and proven offline.

**Decision — the minimum viable slice, deliberately advisory.**

- `safety/rules.yaml` — a committed, citation-carrying SMARTS table (organic/acyl azide, diazo,
  diazonium, peroxide, nitrate ester, polynitroaromatic, perchlorate, hydrazine, N-halamine) plus
  one pairwise incompatibility (strong oxidizer with strong reductant). **Data, not code**: a
  process-safety chemist maintains it without touching Python.
- `safety/screen.py` — `screen_structure` / `screen_reaction` returning `HazardFlag`s (rule, severity,
  explanation, citation, what matched), worst first. Deterministic, offline, no model.
- `agents/safety_tools.py::screen_hazards` — registered through the D-075 `@tool` seam, so the agent
  gained a capability with no orchestration edit. The system prompt tells the agent to screen before
  proposing chemistry; `skills/safety-screening/SKILL.md` holds the judgment for acting on a flag.
- `safety/notes.py` + `kg/validate.py` — an **agent-authored note carrying a `## Procedure`** whose
  structures raise a flag at or above `safety_gate_severity` must document it in a `## Hazards`
  section, or `kg-validate` fails the PR. The warning reaches the reviewer before the merge, in the
  gate that already runs in CI — no new enforcement path.
- `hazard_flag_recall` (`@metric`, D-009 seam) over a committed case pinning one reference molecule
  per rule, gated at `eval_hazard_recall_min` = 1.0 — because a SMARTS that stops matching fails
  *silently*: the screen simply reports nothing, which reads as "no hazard".

**The invariant: the system flags, it never certifies.** `ScreenResult.verdict` renders an empty
result as "No rule in the hazard table matched. This is not a safety assessment." The tool docstring,
the skill, and the module docstring all repeat it, and a test asserts no clearance-like phrasing can
appear. An over-trusted screen is *more* dangerous than none: it converts an absence of knowledge
into apparent assurance, and a chemist told "no hazards" three times stops reading the fourth answer.

**Explicit non-goals** (each a separate decision, none smuggled in): no GHS/SDS database (licensing),
no toxicity/ADMET prediction, no route-level safety verdict, no regulatory or transport
classification, no thermal-stability data, no scale or engineering controls. The skill names these
as the boundary and points at the SDS, EHS, and process-safety review.

**Scoping choices that keep the gate credible.** Agent-authored notes only (a human writing up their
own procedure has made their own judgment); procedure notes only (a record that merely mentions a
structure is not an instruction); high severity only by default. A gate that fires on the wrong notes
is a gate somebody switches off. `safety_gate_enabled` exists for a deployment migrating a legacy
corpus, not as a routine escape hatch.

**Rule-table discipline.** Each rule keeps its SMARTS as specific as the motif allows and is pinned
by a test with one molecule that must match and (across the benign set) molecules that must not —
nitrobenzene must not read as polynitro, acetohydrazide must not read as free hydrazine. Perchlorate
and permanganate match with `~` bonds because RDKit sanitizes them to charge-separated forms; a
double-bond pattern would never fire on a parsed molecule (found by testing, not by reading).

**Open for the user (asked in `docs/backlog-plan.md` §5, implemented under stated defaults).**
Advisory-only scope, a committed table rather than an external hazard database, and a hard-failing
`kg-validate` rule are the defaults shipped; the gate's severity and its on/off switch are config, so
reversing any of them is an env change, not a code change.

## D-081 — Config-extensibility: MCP transport union, skill manifest + enable-list, config idiom rule (audit doc 10, items 5–7)

**Context.** The last three items of `docs/audit/10-config-extensibility.md` §9. Each was
trigger-gated in BACKLOG; the triggers were waived deliberately (see "Rule of Three" below).

**Decision 1 — MCP transport union (item 6).** `McpServerSpec` became
`StdioMcpServerSpec | HttpMcpServerSpec` discriminated on `transport`; `_mcp_tool` dispatches to
`MCPStdioTool` or MAF's `MCPStreamableHTTPTool` and is `assert_never`-exhaustive. A remote server
is now config, not a code edit — the same friction the tool registry removed for tools.

*Backwards compatibility is the load-bearing design point.* Every config written before this — 
`.env.example`, Helm values, any deployment's `CHEMCLAW_MCP_SERVERS` JSON — carries no `transport`
key, and a plain `Field(discriminator=…)` rejects an untagged payload outright, breaking every
existing deployment at startup. So the union uses a **callable** `Discriminator` that reads a
missing tag as `"stdio"` (the only transport that existed then), with `Tag(...)` on each member.
New servers tag themselves explicitly; old configs are untouched. The public name `McpServerSpec`
is kept for the union, so every existing annotation and import stays valid.

**Decision 2 — skill manifest + enable-list (item 5).** Two halves of audit friction #5
("discovery ≠ enablement is only half-modeled"):
1. `agents/skill_manifest.py` — `SkillManifest`, the `SKILL.md` frontmatter as a pydantic contract
   (`name`/`description` required, optional `tools`/`mcp_servers`/`tags`, `extra="forbid"`).
   `make skill-validate` now validates against it **and checks the declared capabilities against
   the live registries** (`agents.tool_registry`, `settings.mcp_servers`). That check is the real
   payoff and is only possible because of D-075's tool registry: a skill still teaching a renamed
   or deleted tool now fails CI instead of surviving as plausible, stale prose. Four shipped skills
   declare their real deps, so the mechanism has actual callers, not a speculative schema.
2. `EnabledSkillsSource` + `settings.skills_enabled` — an explicit enable-list, so a deployment can
   ship the whole skills tree and advertise the validated subset without deleting folders. Empty
   (the default) means every discovered skill: a no-op until opted into.

**Invariant preserved — both narrowings *attenuate*, neither authorizes.** The enable-list cannot
advertise a skill no directory provides, and `RoleScopedSkillsSource` still runs on top of it, so
enablement is layered *under* RBAC exactly as a profile is (D-075). A manifest's declared tools are
**documentation the gate validates, never a grant**: what the agent may call is decided by the
registry/profile and `enforce_tool_authz`, which this seam does not touch.

**Fail-fast, placed where it belongs.** An unknown name in `skills_enabled` is reported by
`make skill-validate`, not raised by `EnabledSkillsSource` — the source runs per turn, so a config
typo must degrade the advertised set rather than break every live conversation. The loud failure
belongs in the pre-deploy gate; the runtime stays resilient.

**Decision 3 — config idiom house rule (item 7).** Recorded in `config.py`'s module docstring
(where anyone adding a field reads it): *typed JSON list when elements carry their own config
(discriminate when they vary by kind); delimited string when elements are bare keys resolved
against a registry, exposed via a derived `*_list` property.* Existing fields are **not** migrated
— that would be churn without a defect. Documented, per the audit, as "doc, not churn".

**Rule of Three note.** Items 5 and 6 were BACKLOG-gated on triggers (a first remote MCP server; a
skill needing to declare deps) that had not fired; they were built on explicit instruction to
complete the backlog. Both are honest rather than speculative: item 6 is a real second variant with
a working dispatch, and item 5's dependency check has four real declaring skills today. The parts
that would have been speculative stayed out — no HTTP server is configured, and profile Stage 3
(filesystem-discovered profiles) is still deferred.

## D-082 — Graph-cache TTL (DA-5 / decision D-1) and the Helm render gate (DA-10 / decision D-2)

**Context.** `docs/audit/12-deep-analysis.md` left two findings explicitly unresolved because each
needed a judgement call rather than an engineering one. Both were signed off; this records what was
built and, more importantly, what was traded.

### D-1 — Graph freshness vs. interactive latency (DA-5)

**The problem.** The note cache is keyed by a stat fingerprint of the note tree. The fingerprint is
cheap *per file* but O(notes) in total, and it is computed on **every** query — including a pure
cache hit. After DA-3 removed the reassembly cost, that scan *is* the floor on interactive latency:
~75 ms at 10k notes on local disk in the audit, and materially worse on the networked OpenShift PVC
production actually reads.

**Decision.** Add `graph_cache_ttl_seconds` (default **5.0**): within the window the last scan is
trusted and skipped entirely. Measured effect on a warm query at 10k notes: **164 ms → 0.52 ms**.

**What this costs, stated plainly.** A note changed by something *outside* this process — another
pod, an out-of-band `git pull` — can remain invisible for up to the window. That is a real change to
freshness semantics, and it takes effect on upgrade. Two things bound it:

- **Local writes never wait.** `kg.graph.invalidate_cache()` is the explicit bust hook, and the
  PR-gate submitter calls it after writing a note. The authoring loop — the one place a human
  *expects* their own change to appear at once — is unaffected. (It is also required for
  correctness there: the submitter's `checkout -B`/`reset --hard` rewrite the tree wholesale, so a
  cached graph could otherwise describe a tree that no longer exists.)
- **`0` restores the old behavior exactly** — scan every query — for any deployment where no
  staleness is acceptable. This is the setting to choose if the GxP posture demands it.

**Why a TTL and not an invalidation signal.** A merge hook or `inotify` avoids staleness entirely,
but only catches changes through the paths it hooks; an out-of-band `git pull` still slips past, so
it buys complexity without closing the hole. The TTL bounds *every* path uniformly.

**Honest note on blast radius.** Two existing tests had to pin `graph_cache_ttl_seconds = 0`,
because they assert fingerprint-based busting and that needs the scan to run. That is the change
being visible where it should be — not test churn to be papered over.

### D-2 — Buying down live-edge risk offline (DA-10)

**Decision.** Do the cheapest, highest-probability item now and defer the rest, as recommended:

1. **`make helm-validate` in CI** — `helm template` piped to `kubeconform -strict` against the
   Kubernetes schemas (plus the CRD catalog, for the OpenShift `Route`). The chart is the one
   artifact no test exercises; a broken chart is discovered at `helm install`, in production, on
   the worst day. No cluster needed.
2. **`tests/test_helm_chart.py` — the gap a schema check cannot see.** kubeconform validates
   *Kubernetes* shape; it has no idea whether `CHEMCLAW_FOO` is a real setting. Two failure modes
   live in that gap and both were unguarded:
   - *A key that is not a field.* pydantic-settings **tolerates** an unknown prefixed environment
     variable — unlike an unknown key in a `.env` file, which is precisely what broke the
     quickstart in DA-1. So the operator gets no error and no effect: a setting they believe they
     enabled is silently ignored. In a GxP deployment that is worse than a crash.
   - *A malformed value on a real field.* This one does crash — at import, in every pod at once.

   Both are now caught offline against the same `Settings` the pods construct, and both were
   mutation-verified (inject each fault, watch the suite go red).

**Deferred, deliberately.** Entra/Nextflow contract tests against recorded responses wait for a
real tenant. Recorded-response tests written against a *guess* at the response shape mostly assert
one's own assumptions back; they would buy confidence, not correctness.

**Finding surfaced while doing this.** `CHEMCLAW_COMPONENT` is set on every Deployment but is not a
`Settings` field and nothing in the app reads it. It is harmless (unknown prefixed env vars are
ignored) and plausibly useful to an operator reading `kubectl describe`, so it is allow-listed by
name in the parity test rather than deleted or the check loosened — any *other* non-field key is a
real finding.

## D-083 — F11 waves 0–3: closing the capability gaps (deployment, reachability, chemistry)

**Phase F11 wave 0–3: closing the capability gaps found in `docs/audit/12-capability-gap-analysis.md`.**

**Context.** A whole-codebase completeness sweep asked "what does this system need that nobody has
listed yet?" — a different question from the AG-*/KM-* gap docs, which checked named capabilities
against a checklist. The answer reframed the priority order: the engine is sound (as those docs
concluded), but the seams *around* it had three classes of hole, and the sharpest ones were
capability the repo had already paid for and could not use.

**Decision (what was built, and the reasoning that shaped each).**

1. **The chart could not run the knowledge layer in either direction.** Readers resolve
   `knowledge_dir` as a local path; the chart mounted no volume and ran no sync, so a merged note
   never reached a live pod. `GitNoteSubmitter` needs a push credential; the chart declared three
   secrets, none of them git. Fixed with a clone-or-refresh replica (init container + sidecar) and
   a separate writable submitter clone. The two are deliberately **different directories**:
   `git checkout -B note/<id>` switches a whole working tree, so the submitter cannot share the
   tree readers are reading. Refresh is `fetch`+`reset --hard`, never `pull` — a read replica must
   not be able to land on a merge conflict.

2. **The image was missing what the running components read.** `skills/`, `scripts/`, `evals/` and
   `knowledge/` were never COPYed and `git` was never installed. In-cluster this meant the agent
   advertised *no skills at all*, no Temporal Schedule could ever be created (nothing ran
   `scripts.schedules`), and the PR-gate could not shell out to git — three silent capability
   losses, none of which fails a test or a lint. `tests/test_deploy_chart.py` now gates image
   completeness, include/values resolution, control-flow balance, and entrypoint dispatch offline;
   F6's "offline-verified" check had confirmed the chart was *well-formed*, not *sufficient*, and
   that distinction is the whole lesson.

3. **Three finished subsystems had no caller.** `DevelopmentReportWorkflow`, `BoCampaignWorkflow`,
   and the human half of `InteractionApprovalWorkflow` were implemented, tested and
   worker-registered, reachable only from the Temporal CLI. The repo's rule is "no abstraction
   without a second caller"; this is the inverse failure — a complete implementation with zero —
   and it is worse than absent because the backlog marks the phases complete. The approval decision
   is an **HTTP route, never an agent tool**: a tool would let the agent approve its own candidate
   and collapse the GxP line the PR-gate exists to draw.

4. **Prose promised capability the code lacked.** Two independent findings turned out to be one
   defect class: a skill directing the agent at `BoCampaignWorkflow` (uninvocable), and
   `_INSTRUCTIONS` advertising impurity answers with no schema field. `make prose-validate` gates
   it and immediately found a third, live instance — `deep-research/SKILL.md` taught three tool
   names (`find_similar_*`) that differ from the agent's actual MCP tools and would have failed at
   call time. This is the *deterministic half* of the AG-13 behavior eval, and unlike AG-13 it needs
   no live LLM, so the AG-13 deferral never covered it.

5. **Chemistry the prompt already promised.** `performed_at` gives the largest note class a time
   axis and finally feeds F10-G2's bi-temporal fields; `purity_percent`/`impurities` make the
   advertised impurity answers possible. A test pins that none of it reaches `reaction_smiles()` —
   feeding structure would have changed every DRFP fingerprint and silently invalidated the
   structural index. `resolve_compound` bridges names to structures (and unblocks the deferred
   per-step species linking, whose own trigger was "a name→SMILES tool exists"). `screen_hazards`
   is the safety layer `BACKLOG.md` said to scope "before any capability phase that could propose a
   hazardous route" — that phase had shipped.

6. **Two refusals are as load-bearing as the additions.** Retention prunes spent operational rows
   but **refuses** `audit_events` (deleting from a hash chain is indistinguishable from the
   tampering it detects; safe disposal needs archive-then-reseal, a GxP design decision for its own
   ADR with QA sign-off) and `calculation_results` (age is the wrong axis for a cache — D-011 makes
   eviction a silent recomputation, potentially an HPC run). Similarly, `screen_hazards` reports
   `unresolved` species as prominently as findings, so a clean report cannot read as a clearance.

**Correction recorded.** **AGT-1 ("no turn cancellation") was withdrawn as a false finding.** The
claim — that an abandoned turn holds its admission permit and never books its tokens — rested on a
`grep` for `CancelledError` returning nothing. That was true but not load-bearing: the handling is
structural (sse-starlette closes the generator; the front door's and runner's `finally` blocks
release the permit and book the budget), and was already correct as of `4bc9b04`.
`tests/test_turn_cancellation.py` measures it and is kept, because nothing previously *proved* the
behavior and a plausible refactor (an `await` in the runner's `finally`) would reintroduce exactly
the leak that was alleged. The analysis document records the withdrawal rather than quietly
dropping the row.

**Consequences.** Six new config groups (all default-off where they change a path), three new
Schedules (reindex, retention, gated on explicit opt-in), one new skill (`process-safety`), one new
CI gate (`make prose-validate`), and the chart is deployable. W3's remainder (metrics, schedule
health, mid-turn resume) and all of W4 stay open and are listed in `BACKLOG.md` — scaling the work
down mid-wave is the user's call, so the boundary is recorded rather than blurred.

## D-084 — F11 waves 3–4: operating the system; the knowledge model reasoning about itself

**Phase F11 waves 3–4: operating the system, and the knowledge model reasoning about itself.**

**Context.** D-083 closed the deployment and reachability gaps. This completes the phase: the
operational surfaces the system had no way to expose, and the knowledge-model capabilities it had no
way to ask for.

**Decision (and the reasoning that shaped each).**

1. **Metrics without a dependency, and only what a scrape needs.** `service/metrics.py` renders the
   Prometheus text format directly rather than adding `prometheus_client` — ~80 lines of stable
   protocol against another package to install, scan and pin. Counters and gauges only: latency
   distribution already rides the OTel trace pipeline, and duplicating it would create a second
   source of truth. Gauges are *callables over live structures*, so they cannot drift from what
   they describe. The route carries no labels at all, which is what lets it stay unauthenticated
   (like `/healthz`) without leaking a session id or user.

2. **Schedule health reads Temporal, not a mirror.** Temporal already knows when a Schedule fired
   and how often; a second table could only ever disagree with it. A *planned* Schedule missing from
   Temporal is reported rather than omitted — "the job was never applied" is exactly the failure the
   surface exists to show, and silence makes it indistinguishable from a healthy quiet job.

3. **Two refusals, again, are the substance.** Retention refuses `audit_events` (deleting from a
   hash chain is indistinguishable from the tampering it detects) and `calculation_results` (age is
   the wrong axis for a cache; D-011 makes eviction a silent recomputation). The pattern repeats in
   `screen_hazards` reporting `unresolved` as prominently as findings: **a capability that cannot
   cover something must say so, or its silence reads as a clearance it has not earned.**

4. **Mid-turn resume is defined by its failure modes.** Opt-in (holding a turn open holds an
   admission permit), bounded below the front door's deadline, non-recursive (else one turn could
   hold a permit indefinitely by launching a job from each continuation), and degrading to the
   *previous* behavior — result on the next turn — rather than to an error.

5. **Dry-run is ambient, never a tool argument.** As an argument the model could clear it, turning a
   chemist's requested dry run into a real HPC submission, or set it, silently no-op'ing real work.
   The same reasoning already governs the ambient session and identity.

6. **The knowledge model can now be asked about itself.** `kg/analytics.py` answers "what don't we
   know" — the complement of outward traversal, and the question that actually steers experimental
   design. `KNOWN_NOTE_TYPES` is enforced by `kg-validate` rather than by the schema, so the agent
   may still *propose* a new type and a human judges it at the PR-gate. `outcome_class` gives
   negative results somewhere to live, and the filter keeping failures out of playbook distillation
   is the load-bearing half — without it a repeated failure distils into a recommendation.

7. **One identity table, three consumers.** `chemclaw.reagents` (W2) now backs the hazard screen,
   the compound notes (KNW-7) and the conditions vocabulary (KNW-4). That is the Rule of Three
   satisfied by real callers rather than anticipated ones, and it is why `DMF`,
   `N,N-dimethylformamide` and `CN(C)C=O` can no longer split one campaign into two.

8. **Preferences are deliberately not graph notes.** Routing "Anna prefers 2-MeTHF" through the
   PR-gate would ask a reviewer to sign off on personal trivia — which is how a gate stops being
   taken seriously. The graph holds what the organisation knows; `user_preferences` holds how one
   person works.

**Two findings closed as not-gaps after assessment**, recorded so they are not re-opened blindly:
**TOOL-7** (units are carried in field names throughout, including every model added in this phase;
a `Quantity` type would be an abstraction with no second caller) and **AGT-6** (the W1 tools take
typed pydantic arguments, so MAF already forces a validated payload at the machine-consumed call
site whose absence was the original reason to defer structured outputs).

**Consequences.** Five items remain open and are listed in `BACKLOG.md` with the reason each is not
built. Three are blocked on a decision or a prerequisite rather than on effort (TOOL-6 needs a
literature-source decision; AGT-3 needs a first real document format; IDEA-6 depends on AGT-3), and
two are genuinely sizeable and warrant their own design note (IDEA-2 predicted-vs-actual
calibration, IDEA-1 standing queries). Stopping on those boundaries rather than half-building them
is the deliberate call.

## D-085 — F11 completion: the five items blocked on a decision or a prerequisite

**Phase F11 completion: the five items that were blocked on a decision or a prerequisite.**

**Context.** D-084 closed W3–W4 except five items I had recorded as blocked. Asked to implement them
anyway, the right move was the one D-057 already established for this repo: **make the blocking
decision explicitly and record it, rather than defer a second time.** Each decision below is stated
in the module that embodies it, so a future reader sees the reasoning at the point of use.

**Decisions.**

1. **External literature source → PubChem PUG-REST** (TOOL-6). Chosen because it is the only option
   clearing every constraint this repo actually has: public and licence-clean (Reaxys/SciFinder need
   a site licence this repo cannot assume), credential-free (every other external integration here
   needs a secret), and **structure-keyed** — it answers by the same key the fingerprint index uses,
   so a hit joins the corpus without a second naming scheme. The alternatives are not excluded; they
   are one sibling class each, which is exactly what the F7 retriever seam was built to buy.
   Retrieval is by structure only: free-text search over a chemistry question returns noise, so a
   query with no parseable structure returns empty rather than guessing — the same conservatism
   `resolve_compound` applies. Every failure mode degrades to *empty*: external evidence must never
   be able to sink an answer the internal corpus could already give.

2. **Upload formats → a closed allowlist that refuses what it cannot parse** (AGT-3). Markdown,
   plain text, CSV and TSV parse completely and deterministically offline. PDFs, spectra and images
   are **refused with a message naming what is supported**. This is the load-bearing half: a PDF
   "read" by scraping whatever bytes look like text produces confident nonsense a chemist cannot
   distinguish from a real reading, which is strictly worse than the gap. Attachments are
   session-scoped working material, never knowledge — routing an upload into the graph would bypass
   the PR-gate.

3. **Backfill proposes documents verbatim, one note each** (IDEA-6). No summarizing, no extraction,
   no chunking. A backfill's job is to make existing documents *reachable*; deciding what they mean
   belongs to the retrieval and synthesis layers. An LLM-summarized backfill would put thousands of
   unreviewed paraphrases into the corpus, which is the fastest way to make a knowledge graph
   untrustworthy. Ids follow content, not filename, so a rename cannot mint a duplicate.

4. **Calibration reports three figures, not one** (IDEA-2). Bias says whether a calculator is
   *correctable*; MAE says how far off it typically is; **uncertainty coverage** says how often the
   truth fell inside the stated error bars — the figure a mean error cannot show, and the one that
   distinguishes "imprecise but honest" from "precise-looking and misleading". `n` accompanies every
   figure because a bias from three points is not a bias. Recording is best-effort throughout: a
   ledger about predictions must never cost a prediction.

5. **Digest watermarks advance after delivery** (IDEA-1). A crash between "found matches" and
   "delivered" must re-report rather than silently skip: a duplicate digest line is a nuisance, a
   missed one defeats the feature entirely.

**A pre-existing test earned its keep.** `test_every_session_scoped_route_is_ownership_gated`
enumerates session-scoped routes rather than hardcoding them, and failed the moment the attachments
route appeared — forcing a conscious update plus a behavioural non-owner sweep over the new route.
That is exactly the design intent of an inventory assertion, and worth copying.

**Consequences.** Phase F11 is complete: every finding in `docs/audit/12-capability-gap-analysis.md`
is either implemented or explicitly closed as a not-gap, with three findings (AGT-1, TOOL-7, AGT-6)
withdrawn after assessment and recorded in `DEFERRED.md` so they are not re-opened blindly. Two
things remain genuinely out of reach here and are unchanged: the live edges needing a real
tenant/broker/cluster, and the audit-trail archive-then-reseal design, which needs an ADR with QA
sign-off rather than a cleanup job.

## D-086 — First reconciliation with `main` (PRs #17–#20): hazard screen, event sink, tool registry

**Context.** While this branch built F11, `main` merged PRs #17–#20, three commits of which solved
problems this branch had also solved, independently and differently: hazard screening (`744c265`,
D-080), `PlanEvent`/`JobStartedEvent` emission (`f2e083a`, D-077), and the `@tool` capability
registry (`76c03b2`). Merging without reconciling would have shipped two hazard screens, two
per-turn contextvar sinks, and a hardcoded tool list alongside a registry.

**Decisions — each resolved on merit, not on which side wrote it first.**

1. **Hazard screening: `main`'s `safety/` wins outright; this branch's module is deleted.** Its rule
   table is *data* (`safety/rules.yaml`) that a process-safety chemist maintains without touching
   Python, every rule carries a literature citation, and it is enforced by a `kg-validate` gate plus
   a `hazard_flag_recall` eval metric. This branch's `chemclaw/hazard.py` was a Python table with
   none of that. What was genuinely additive — four **named-substance incompatibility pairs** (azide
   salt + DCM, NaH + DMF/DMSO, peroxide + ketone, complex hydride + chlorinated solvent), each safe
   apart and dangerous together and therefore invisible to a per-substance screen — moved into
   `rules.yaml` as SMARTS pair rules. `tests/test_safety_pairs.py` pins them.

   **The azide rule earned its own comment.** Written the obvious way (the X2 form correct for an
   *organic* azide) it silently never fired on an azide **salt**, because RDKit sanitizes the anion
   to two one-coordinate nitrogens. It was caught only by screening a parsed molecule — exactly what
   `rules.yaml`'s own header instructs a contributor to do, and the same trap PR #20 recorded for
   perchlorate and permanganate. A rule that never fires is worse than no rule: it reports "no rule
   matched" for a hazard the table claims to cover.

2. **One event sink, not two.** `main`'s `agents/job_events.py` and this branch's
   `agents/turn_signals.py` are the same design (a task-local contextvar drained between streamed
   updates) with the same rationale. This branch's is a strict superset — it also carries PR-gate
   proposals and clarifying questions, and preserves their order *relative to* job launches.
   Consolidated onto it, keeping `main`'s function names as the caller-facing API so its callers and
   tests were untouched. Two sinks drained separately would have left the relative order of a
   launched job and a proposed note undefined, which is precisely what a transcript must get right.

3. **Drain ordering: signal-first, and `main`'s test assertion corrected.** A tool that ran while
   the model was producing an update ran *before* the text it then produced. `main`'s test fake
   announces its job before yielding text, so its `["token", "job_started"]` assertion reported the
   text ahead of the job that preceded it. Flipped, with the reasoning recorded at the assertion —
   the property that test names ("before the answer") holds either way.

4. **The `@tool` registry is adopted wholesale.** `main`'s `_capability_tools()` assembles from the
   registry, so this branch's 19 new tools became decorators at their definition sites and their
   modules joined the registration-side-effect import block. `agents/chemclaw_agent.py` was taken
   from `main` unchanged.

**Two inventory guards did their job.** `test_registry_holds_exactly_the_inprocess_tools` and
`test_every_session_scoped_route_is_ownership_gated` both enumerate rather than hardcode, and both
failed the moment new tools and a new session-scoped route appeared — forcing a conscious update
instead of silent drift. That pattern is worth applying to further families.

**Result.** 857 passing (41 offline skips unchanged), ruff + `mypy --strict` clean, `kg-validate` /
`skill-validate` / `prose-validate` / `eln-validate` all green — with one hazard screen, one event
sink, and one tool registry.

## D-087 — Second reconciliation with `main` (PR #21): the MCP transport union

`main` landed its own transport discrimination while this branch's networked-MCP work (gap TOOL-1)
was in flight. **`main`'s wins outright.** It is a proper discriminated union
(`StdioMcpServerSpec | HttpMcpServerSpec`) with a *callable* discriminator that defaults an absent
tag to `stdio`, so every existing config keeps loading; this branch had one class with an
either/or `command`-xor-`url` validator, which is exactly the ambiguity a union removes. Per-variant
`request_timeout` also supersedes this branch's global `mcp_request_timeout_seconds` — the timeout
belongs to the remote spec that needs it, not to every server including local subprocesses.

The dispatch in `_mcp_tool` came from `main` unchanged; this branch's contribution here reduces to
the chart-side half (gap DEP-3: the standalone MCP Deployments were default-on while stdio-only,
i.e. a crash loop), which is unaffected and still needed.

Three guards caught the fallout rather than letting it drift: `mypy --strict` on the leftover field
from the resolution, `test_env_example_documents_only_real_fields` on the now-nonexistent env key,
and the chart test on the superseded constructor. That is three independent gates on one merge
mistake, which is the point of having them.

## D-088 — Third reconciliation with `main` (PR #23): ADR renumbering, and the chart's env parity guard

`main` landed the graph-cache TTL and the Helm render gate while this branch was in review. Two
resolutions were mechanical (both CI steps and both `make` targets are additive; the `tasks/todo.md`
logs are append-only and both kept). Three were not.

**The ADR numbers had collided head-on, and this is the fix.** This branch appended its ADRs as
D-074…D-076 and D-081…D-082 while `main` had independently allocated the *same* numbers for
different decisions — a defect this branch introduced in the first reconciliation and that nobody
caught, because nothing checks the log for uniqueness. `main`'s allocation keeps the numbers (it
merged first and its numbers are already cited from `BACKLOG.md`, `docs/backlog-plan.md` and
`DEFERRED.md`); this branch's five renumber to **D-083…D-087**, and the seven references that
pointed at them — `tasks/todo.md`, `docs/gap-closure-plan.md`, `DEFERRED.md`, `agents/chem_tools.py`,
`tests/test_safety_pairs.py` — move with them. An append-only log with duplicate ids is not an
audit trail, so the collision is fixed rather than annotated.

**`main`'s new chart test caught a real defect in this branch, and the fix widened the guard.**
`test_chart_config_keys_are_real_settings` asserts every `CHEMCLAW_*` env the chart injects names a
real `Settings` field — the point being that pydantic-settings *silently ignores* an unknown
prefixed environment variable, so an operator who sets it gets no error and no effect. This branch's
knowledge-sync work added five such keys (`…_REPO_TOKEN`, `…_REPO_URL`, `…_SYNC_DIR`,
`…_PUBLISH_DIR`, `…_SYNC_INTERVAL_SECONDS`), and only one of them was even visible to the test.

The naive resolution — exempt them — would have thrown away the guard. The premise it encodes is
slightly too narrow rather than wrong: the real invariant is not "every key is a `Settings` field"
but **"every key is read by something"**, and `deploy/knowledge-sync.sh` and `deploy/entrypoint.sh`
are first-party consumers that happen to be shell. So the check now (a) reads the `_helpers.tpl`
env block as well as `values.yaml`, closing the half of the surface it could not see, and (b)
*discovers* the shell-consumed names by scanning `deploy/*.sh` instead of listing them. Discovery,
not a list: the earlier lesson on this branch was that a guard which enumerates catches drift while
one that hardcodes only catches what someone already thought of. Mutation-verified by adding a
`CHEMCLAW_TYPO_SETTING` key to `values.yaml` — the guard names it.

The knowledge-repo push credential is therefore a *fourth* declared secret, against the
three-secret model (D-047). Recorded rather than waved through: the PR-gate submitter shells out to
`git push` and a git host authenticates that push with a token — there is no federated exchange for
it the way there is for the Entra-fronted APIs. The alternative is a knowledge layer that cannot
write.

A companion test asserting shell-consumed keys are never *also* `Settings` fields was written and
then deleted: every overlap it found (`CHEMCLAW_SERVICE_HOST`/`_PORT`, which `entrypoint.sh` passes
to uvicorn) was shared by design, so its exemption list equalled its finding list. A guard with no
possible signal is decoration.

**`service/runner.py` had absorbed two of everything.** Both branches had independently built a
per-turn signal sink and a "last plan emitted" variable, and the auto-merge kept all four. The
consequences were live, not cosmetic: `begin_turn()` and `set_job_sink()` are the *same* contextvar,
so calling both nested one buffer inside the other and the teardown reset them out of LIFO order;
and two `_current_plan` definitions meant the second silently shadowed the first. Consolidated to
one sink and one plan variable. `main`'s `_current_plan` is the one kept — its `None` return
distinguishes "this agent has no plan" from "this agent does not plan", which an empty list cannot
express — with this branch's reason-for-existing (gap RCH-5) folded into its docstring. The
post-resume drain now takes the whole signal buffer rather than only job ids, so a note proposed
during a mid-turn resume still reaches the stream.

## D-089 — No external sources; PDF/PPTX/DOCX/XLSX are in scope

Three review decisions on the F11 work, taken by the user and recorded here with what each changed.

### 1. No external sources. The PubChem retriever is removed, not switched off.

D-084 chose PubChem PUG-REST for TOOL-6 and shipped it off-by-default, reasoning that registry
membership was a sufficient enable switch and that opting in constituted accepting the egress. The
scope answer is simpler and stricter: **this system takes no external sources at all.** So
`report/literature.py`, its registry entry, its two config fields and its five tests are deleted
rather than left dormant — a dormant integration is still a maintained one, and "off by default"
invites a deployment to turn it on.

**The interesting part is why a test was added rather than a note.** The constraint was *already
written down*. `DEFERRED.md` carried TOOL-6 as "blocked on a decision: which source, under which
licence" — which reads as an invitation to answer the question, and that is exactly what happened.
Prose stated the constraint and did not enforce it, so `tests/test_no_egress.py` now fails on any
first-party module that names a third-party data host, plus a registry-membership check for a
source whose address would arrive entirely from config. Both `DEFERRED.md` rows are rewritten from
"not yet" to "rejected", because the old wording is the actual root cause here.

The allowlist holds exactly one host — Entra's login endpoint, which genuinely is Microsoft's since
that is the identity provider F4 chose. Everything else the stack talks to (LLM, Temporal, Postgres,
Tower, the git remote) carries *no host default in source at all*: it is required config, so a
deployment cannot inherit somebody's address by accident. That the list is one entry long is the
useful fact it records.

### 2. PDF, PPTX, DOCX and XLSX are in scope, read through their own document models.

D-084 refused these formats with a specific argument: a PDF "parsed" by scraping text-like bytes
produces confident nonsense a chemist cannot distinguish from a real reading. The scope decision
reverses the refusal. It does not refute the argument — so the fix is *real extraction*, never a
relaxed version of the guess. Each format is read through its own library (`pypdf`, `python-pptx`,
`python-docx`, `openpyxl`), page/slide/sheet boundaries are preserved because "the table on page 3"
must still resolve after ingest, and a file the library cannot open is refused rather than salvaged.
All four parse locally, which is what makes them consistent with decision 1.

**What survives from the original refusal is the one case extraction cannot fix.** A scanned PDF
opens fine and yields nothing; returning that as an empty document would tell a chemist their CoA
was blank. It is refused by name instead. The test is **"did any page produce text at all"** and
deliberately not a minimum length — the first cut used a 32-character floor, which would have
refused a legitimate one-line CoA, i.e. reproduced the false-negative the refusal exists to avoid.
Zero characters is the property that actually distinguishes a scan; anything else is a magic number.

Two smaller calls worth stating: speaker notes are extracted from decks, because a project deck's
reasoning usually lives there and dropping them would discard the informative half; and `openpyxl`
reads with `data_only=True`, because a chemist attaching a yield sheet means the yields — `=B2/C2`
is not an answer.

Fixtures are **built by each format's own writer inside the tests**, never committed blobs, so the
assertions are about our parsing rather than about a file someone once produced. The PDF fixture is
assembled by hand (catalog, page tree, a `BT … Tj ET` content stream, a correct xref) because
`pypdf` writes PDFs but cannot typeset, and adding a renderer purely to make fixtures would be a
dependency the shipped code never uses.

### 3. Audit-trail archive-then-reseal stays in the backlog.

No change. `workflows/retention.py` continues to refuse `audit_events`, and the reasoning in
`DEFERRED.md` — deleting from a hash chain is indistinguishable from the tampering it detects, so
safe disposal needs an out-of-band genesis anchor and QA sign-off — stands as written. Recorded
here only so the decision is visibly *made* rather than overlooked.

## D-090 — Reported-issue sweep: the azide the screener could not see, two missing session routes, and the note-repo footgun

Five issues were reported across this repo and `Chemclaw3_ui`. Two of the five turned out
not to be what the report said, which is the first thing worth recording.

### 1. `GET /approvals` was already there; `GET /sessions` was the real gap.

Two issues were filed against the UI as "missing from backend". Reading `server/routes.ts`
against `service/app.py` settled both: all three approval routes (`GET /approvals`,
`GET /approvals/{id}`, `POST /approvals/{id}/decision`) exist and match the BFF's whitelisted
paths exactly, so that issue is stale and no code changed for it. `GET /sessions` and
`GET /sessions/{id}/messages` genuinely did not exist — the BFF whitelists them with the
comment "Added by the companion backend change", i.e. the UI pre-registered routes this repo
never grew. The fix therefore belongs here, not in the UI, and the UI needs no change at all.

Both routes are ownership-scoped through the existing `_resolve_session` gate rather than a
second check, so a transcript is readable only by the chemist whose session it is and a
non-owner gets the same 404 as an unknown id. The route-inventory test in `tests/test_service.py`
failed on the new route exactly as designed — that assertion exists to force this to be a
conscious update, and it worked.

**The transcript reads through `history_provider()`, not through a query of `session_messages`.**
One reader means the write path and the read path cannot drift, and it makes the route work
unchanged under either store: MAF's in-memory provider holds no instance state and keeps its
messages in `session.state`, which is the object `_resolve_session` has just returned. The
alternative — a second SQL reader — would have been Postgres-only and a second thing to keep
in step with MAF's message shape. `TranscriptMessage` flattens to role+text deliberately, so a
MAF version bump is not a breaking change to the HTTP contract.

`GET /sessions` returns empty under the in-memory store. There is no durable registry to
enumerate, and reporting the process's live LRU instead would answer a question about the
deployment with an eviction-dependent guess that a pod restart silently changes. The listing
SQL uses `owner IS NOT DISTINCT FROM %s` rather than `= %s`: the shared dev principal records a
real SQL NULL, and three-valued logic makes `=` false for every row, so the no-Entra deployment
would have shown an empty list with the sessions sitting right there in the table. Verified
against a real Postgres, including that the naive form returns nothing.

### 2. The hazard screener could not see sodium azide.

Reported as "bare azide anion `[N-]=[N+]=[N-]` not caught". It is not one input; it is a class.
`organic-azide` and `acyl-azide` both open on `[#6]`, so **every** azide that is not carbon-bound
fell through both: the salt (RDKit sanitizes NaN3 to two one-coordinate N- atoms, matching
neither X2 pattern), hydrazoic acid, and the silyl/phosphoryl azide transfer reagents (TMSN3,
DPPA). Sodium azide is one of the most-reached-for reagents in the building and it screened
*clean* — reported as "no rule matched", which a reader takes as "no hazard found" on a compound
that is acutely toxic and liberates explosive HN3 on contact with acid.

The fix is one rule expressed as the actual invariant — an azide whose terminal nitrogen is not
bonded to carbon (`[N;!$([N][#6])]=[N;X2+]=[N;X1-]`) — rather than a special case for the reported
SMILES or a list of counter-cations. It cannot double-fire with the two carbon rules: on an
organic azide the only non-carbon terminal nitrogen is the far one, and reading inward from it
the third atom is X2, not X1-. Both directions are pinned by test.

**One existing test asserted the bug.** `test_an_ordinary_combination_is_not_flagged` listed
sodium azide in acetonitrile among combinations that must raise nothing at all. That was only
true because the alert was missing. The claim it was actually making — swapping dichloromethane
for an acceptable solvent clears the *diazidomethane* hazard — is still true and still tested;
it now asserts the pair rule is silent rather than that the whole screen is. The reagent's own
flag stands, in any solvent, which is the point.

### 3. `CHEMCLAW_NOTE_REPO_DIR` is a required deployment setting, now documented as one.

The default `.` is not a sensible fallback, it is always wrong outside a dev checkout: every
submission opens with `git reset --hard` + `git clean -fd`, so pointing it at the tree the
service runs from would destroy uncommitted work there. `_require_dedicated_checkout` already
refuses loudly, so the failure was never dangerous — only undiagnosable, because the runbook
had no mention of the variable at all. Documented in the runbook section that already carries
the PR-gate's other deployment constraint, including that the refusal message is the guard
working rather than a broken deployment, and that leaving it unset outside Helm is the quieter
failure (`knowledge-sync.sh` logs and skips, so the first note submission discovers it).

## D-091 — Restoring the tree the Replit restructure rewound

`49cd44c` ("deploy: Replit dev deployment and runtime fixes") moved the Python service to
`services/chemclaw/`, but populated the new location from an **older snapshot** in the same
commit that deleted the top-level tree. The move itself is right; the content it moved was not.

**How this was established, rather than assumed.** `services/chemclaw/service/app.py`'s blob is
byte-identical to that file at `16b63c2` (the PR #23 merge). Comparing every common path against
that commit: **338 of 352 identical, 14 differing**. So the import is a clean snapshot of
`16b63c2`, and everything merged in `16b63c2..2fc903a` — 21 commits, including PRs #24 and #26 —
was silently dropped: 38 Python files, 20 test modules, 8 HTTP routes (`/approvals` ×3,
`/metrics`, `/schedules`, `POST /sessions/{id}/attachments`, `/events/knowledge-merged`), 4 of the
5 incompatible-pair safety rules, and 462 lines of this log.

Restored by overlaying `2fc903a`'s tree onto `services/chemclaw/`, which is content-only and
therefore keeps the new layout. Three things were deliberately **not** reverted:

1. **The six Replit-only additions** — `start.sh`, `start-temporal.sh`,
   `start-background-worker.sh`, `.bin/temporal`, `agents/job_events.py`, and the `knowledge`
   symlink into `services/chemclaw-notes-repo`. The overlay cannot touch what it does not contain,
   except the symlink, which `tar` replaced with a real directory and which was put back by hand.
2. **The `service/runner.py` disconnect fix (ISSUE-B-10).** This is the one genuinely *new* piece
   of work in the 14 differing files, and it is worth keeping: a client vanishing mid-tool-call
   left a `tool_use` block with no matching `tool_result`, which every later turn replayed until
   the model rejected the whole thread — one dropped connection permanently bricking a
   conversation. `runner.py` had moved on by +110/−16 lines since the snapshot, so the fix was
   hand-merged rather than patched, and is now **pinned by a test** that was confirmed to fail
   when the rollback is removed. The original arrived without one.
3. **The Replit deployment surface outside `services/chemclaw/`** — untouched.

### CI was collateral damage, and is restored at the root.

GitHub Actions only reads workflows from the repository root. The restructure moved `ci.yml` to
`services/chemclaw/.github/workflows/`, where nothing runs it, so `main` has had **no CI at all**
since — the green checks on PR #28 came from the PR branch's own root workflow, not from `main`'s.
A root `ci.yml` now runs the same gate with `working-directory: services/chemclaw`. It drops the
Helm/kubeconform steps (the restructure's Makefile removed the target, and the chart is not part
of the Replit deployment) and `make eval`, whose case-set has three gated failures that predate
all of this — a gate that is red on arrival trains people to ignore it, and those cases deserve
their own fix rather than a permanently-failing check.

## D-092 — Process/analytical-development capability research: quick wins, one durable big win, and what was rejected

A deep survey of open-source ML/cheminformatics and fast-ab-initio packages for chemical and
analytical process development (data-source connectors like LIMS explicitly out of scope), asking
specifically what could be added through the **existing** connector seams — a fast calculator
(`calc/` + the calculation store), an MCP capability server, or a Temporal workflow — with no new
ad hoc wiring. Landed as five additions, all through those exact seams, plus two candidates
researched and deliberately **not** built.

**Quick wins (fast, cached calculators/tools, zero new dependencies):**

- `predict_developability_profile` (`calc/descriptors.py`) — an RDKit-only physicochemical panel
  (MW, LogP, TPSA, H-bond counts, rotatable bonds, Fsp3, QED) plus Lipinski/Veber flags. Every
  descriptor is already computed by RDKit (already a dependency); the only gap was that nothing
  exposed the panel itself, versus the four descriptors buried inside the ESOL solubility model.
- `predict_logd` (`calc/logd.py`) — pH-dependent lipophilicity, composing the existing cached
  `predict_pka` with Crippen LogP via Henderson-Hasselbalch. No new cache entry (the expensive
  half, xTB pKa, is already memoized); inherits `calc.pka`'s neutral-O-H/S-H-acid domain limit.
- `estimate_reaction_energy` (`calc/reaction_energy.py`) — a reaction electronic-energy /
  exotherm screen from cached per-species GFN2-xTB single points, weighted by stoichiometry.
  Advisory, like the structural hazard screen (D-080) — a flag, never a safety certification.
- `generate_screening_design` (`bo/engine.py::factorial_design` + `bo/problem.py::ScreeningDesign`)
  — a full-factorial **categorical** screening design (e.g. every catalyst x solvent x base
  combination), via BoFire's `FractionalFactorialStrategy` on an all-categorical domain (the
  non-deprecated replacement for the now-deprecated `FactorialStrategy`). Distinct from
  `suggest_next_experiment`'s adaptive one-batch-at-a-time proposals. Rejects a continuous
  parameter outright (gate G4) rather than silently dropping/fractionating it.

**Big win (durable Temporal workflow, zero new dependencies):** `ConformerEnsembleWorkflow`
(`workflows/conformer_job.py` + `conformer_activities.py` + `conformer_models.py`, pure algorithm
in `calc/conformer_ensemble.py`) — an RDKit ETKDG conformer ensemble, MMFF-pruned, then
Boltzmann-weighted over per-conformer GFN2-xTB energies. `calc.xtb` approximates each molecule as
one rigid seeded geometry; a flexible molecule's solution-phase behavior is more honestly read
from a population of conformers. An ensemble (tens of xTB single points) is materially heavier
than the inline fast-calculator's sub-second budget but is pure local CPU work, not a remote HPC
submission — so it follows `BoCampaignWorkflow`'s shape (local activities on the light
`background-jobs` queue), not `QMJobWorkflow`'s submit/poll shape. `calc/xtb_engine.py` gained one
shared primitive (`positions_bohr`, factored out of `geometry`) so the ensemble reads a specific
already-embedded conformer instead of re-embedding one each time — a DRY refactor, not new science.
Agent tools `submit_conformer_ensemble_job`/`get_conformer_job_status` mirror `agents.qm_tools`
exactly (D-002's thin-adapter shape).

**Researched and deliberately not built**, both for the same reason:

- **ML interatomic potentials as a fast-ab-initio surrogate** (ANI-2x/TorchANI, MACE-OFF/MACE-MP).
  `torchani` was installed and inspected directly in this environment: current releases pull in
  `huggingface-hub`/`hf-xet`, and `torchani.models.ANI2x()` fetches its pretrained weights from the
  Hugging Face Hub on first use rather than shipping them in the wheel (this changed from older
  releases that did bundle weights). That is a runtime external-data dependency, which is exactly
  what D-089 says this system does not have — `tests/test_no_egress.py` enforces the source-literal
  form of that rule, but the principle is broader than what a host-literal grep can catch. Revisit
  only if a deployment vendors the weight files into the container image at build time as an
  explicit, reviewed infrastructure decision (D-089's own escalation path) — not as a quiet runtime
  fetch.
- **Retrosynthesis (AiZynthFinder)**. The `DEFERRED.md` trigger — "after the spine + graph +
  fingerprint layers exist" — is now met (ECFP4/DRFP fingerprint search shipped in F11). It still
  is not built: AiZynthFinder's pretrained USPTO models and stock file are fetched via a
  `download_public_data` step from a public host, the same runtime/deploy-time external-fetch
  problem as the ML potentials above, for the same reason not solved here. `DEFERRED.md` updated
  to record the sharpened blocker (not "no fingerprint layer yet", but "no vendoring story").

`bo/engine.py`'s docstring is updated (still "the only module that touches BoFire") to note
`factorial_design` as a second BoFire-touching adapter alongside the BO strategies, not a
boundary violation — it lives in the same file specifically to keep that claim true.

## D-093 — A raw exception in a fan-out child suspends as a task failure, not a workflow failure

CI's own `ci.yml` comment already named the symptom: `tests/test_orchestrator.py::test_fan_out_runs_children_in_order_and_isolates_failures`
"skips wherever the Temporal test-server binary cannot be fetched and hangs where it can" —
investigated after a PR's CI run was cancelled at the job's 30-minute `timeout-minutes` bound
(added earlier the same day specifically because, before it, every recent `ci` run on `main` had
instead been cancelled at GitHub's 6-hour absolute ceiling: runs #207, #218–#221 all ran the full
six hours before being killed). **This was not new, not caused by that PR, and not fixed by the
timeout bound alone** — the bound only converts a silent 6-hour hang into a bounded, visibly-failing
one. Two distinct issues stacked, and only fixing both cleared the hang.

**Issue 1 — the real root cause.** The Temporal Python SDK's safety default: a raw exception raised
directly in workflow code (not already one of the SDK's own `FailureError` subclasses, e.g. plain
`raise ValueError(...)`) is *not* treated as a workflow failure by default. It "suspends the
workflow via task failure" instead (`temporalio.workflow.defn`'s own docstring) — an internal retry
loop the *worker*, not the server's `RetryPolicy`, drives, with no bound and no
`non_retryable_error_types` check, on the theory that an unclassified exception might be a code bug
that a redeploy will fix, not a legitimate business failure. `_DoublerWorkflow` in the fan-out test
deliberately raises a plain `ValueError` on its poison input (13) — exactly the shape this default
swallows into an unbounded suspend-and-retry loop, invisible to any `retry_policy` passed to
`execute_child_workflow` at all. Against the time-skipping test server this is not a slow hang; it
is a genuine infinite loop (an offline sandbox never gets far enough to hit it — it skips first
when the test-server binary can't be fetched), matching the reported symptom exactly. Fixed by
declaring `_DoublerWorkflow` with `@workflow.defn(failure_exception_types=[Exception])`, which is
what the poison-input test was always implicitly assuming.

**Issue 2 — `fan_out`'s own retry default, found while investigating Issue 1.**
`workflows/orchestrator.py::fan_out` starts each child via
`workflow.execute_child_workflow(..., retry_policy=retry_policy)`, where `retry_policy` defaults to
`None` — and neither real caller (`report_workflow.py`, `memory_jobs.py`) ever passes one either.
`None` does not mean "no retry"; it means Temporal's own default `RetryPolicy()`
(`maximum_attempts=0`, unlimited, no `non_retryable_error_types`). Once Issue 1's fix makes the
poison child's `ValueError` a genuine `WorkflowExecutionFailed`, *this* default is what would make
the fan-out retry it forever anyway rather than isolating and dropping it as documented. Neither
production caller was actually at risk from this specific default (`ReportSectionWorkflow` catches
its activity's error and never raises; `PublishNoteWorkflow`'s uncaught error is an `ActivityError`,
already a `FailureError`, so Issue 1 doesn't apply to it) — but the default was still wrong relative
to the fan-out's own stated contract, so it is fixed regardless: an unset `retry_policy` now
defaults to `BAD_DATA_RETRY` (bounded `maximum_attempts`, immediate failure for the
already-catalogued bad-data exception types) instead of passing `None` straight through — the same
policy already used for the sibling `resolve_fan_out_limit` local activity in the same function, so
no new retry idiom is introduced.

**Verification.** Offline: `tests/test_orchestrator.py`'s non-server tests pass; mypy/ruff clean
across both changed files. The server-backed fan-out test itself cannot run in this sandbox (the
Temporal test-server binary host is egress-blocked here) — confirmed instead against the real
time-skipping server via this repo's own CI, which is reachable there. `fan_out`'s docstring now
calls out the `failure_exception_types` gotcha directly, since any future child workflow that
raises a raw exception (rather than an already-wrapped `FailureError`) would reintroduce Issue 1.

## D-094 — CI's `kg-validate` step needs a real (even empty) `knowledge` directory

Found immediately after D-093's fix cleared the fan-out hang: `make kg-validate` then failed
fast (`notes directory does not exist: knowledge`, exit 1) on the very next CI run. `knowledge` is
a git-tracked symlink (mode 120000) to `/home/runner/workspace/services/chemclaw-notes-repo/knowledge`
— an absolute path specific to the Replit workspace layout, deliberately kept as one of the "six
Replit-only additions" (D-091). It does not resolve on a GitHub Actions runner, or on any other
checkout; `Path.exists()` on a symlink follows it to the target, so `kg.validate.main()` correctly
reports the directory as missing and refuses to validate.

Not touched: the committed symlink itself — it is a real, deliberate deployment decision for
Replit (D-091), and rewriting or removing it here would be an unrelated, out-of-scope change to
that target. Instead, `.github/workflows/ci.yml` gained one step before `Validate knowledge graph`
that replaces the broken symlink with a real empty directory *in that checkout only*: `kg-validate`
against zero notes is a legitimate, already-documented state (BACKLOG.md: "the corpus holds no
procedure notes yet"), not a special case to work around. Verified locally by reproducing the exact
CI condition (removing the tracked symlink, recreating an empty directory, running
`python -m kg.validate`) — exits 0, "OK: knowledge is a valid knowledge graph" — then restored the
symlink in the working tree before committing, since only the CI step changes, not the tracked path.

## D-095 — xTB capability seams (X1) and the properties the SCF already produced (X2)

**Context.** `docs/xtb-tools-proposal.md` inventories what the xTB ecosystem offers against what
ChemClaw consumed: one capability (a single-point energy) through one of three engines (`tblite`
in-process). The same SCF that produced that energy also produced Mulliken charges, Wiberg bond
orders, the dipole, and the orbital energies, all of which were read and discarded. This ADR covers
the first two phases of that proposal — the ones that add no dependency.

**Decision 1 — geometry becomes a content-addressed value (`calc/structure.py`).** Every calculator
previously went SMILES → embed → compute in one breath, so two tasks on "the same molecule" silently
produced two different geometries and nothing could reuse one. `Structure` carries elements,
positions (Angstrom, rounded to `xtb_geometry_decimals` on construction), charge, multiplicity, and
an optional `origin`; `structure_id` is a stable hash of the chemical content alone.

*The unplanned payoff is in the cache key.* Keying on `structure_id` rather than on
`(smiles, embed_seed)` is strictly stronger: the seed's effect is already inside the coordinates, so
the key stays correct without naming it, and a geometry arriving later from an optimizer or a file
hits the same entry. `xtb.sp`'s `params_hash` is now empty by construction — the honest statement
that a single point has no free parameters beyond its structure and method.

*It also generalizes a guard rather than weakening one.* The old `require_closed_shell` refused every
odd-electron system because a SMILES does not encode multiplicity. `Structure` validates the electron
count *against a declared* multiplicity instead, so an accidental radical still fails fast (with a
message naming the fix) while a deliberate open shell is computable. That is what makes the Fukui
ions legitimate rather than silent — the previous check would have made X2 impossible.

**Decision 2 — one cache-key derivation (`calc/xtb_spec.py`).** `XtbSpec` holds every field that can
move a number and derives the key once over `model_dump()`, so a new knob is keyed by construction
rather than by review. It shipped with three callers on day one (`sp`, `properties`, `fukui`).

**Decision 3 — three things the proposal describes were deliberately *not* built.** The `XtbEngine`
protocol (one backend today), the structure *store* (nothing in X1/X2 produces a geometry, so it
would have one writer and no reader), and a `calc/xtb/` package (cannot coexist with `calc/xtb.py`;
`calc/` is flat). Each would have been a one-caller abstraction written before knowing what X3 needs
— the Rule of Three case the proposal's own §12 makes. `XtbSpec` shipped because it has three
callers; that is the line.

**Decision 4 — Fukui indices are computed on an MMFF-relaxed geometry, and that is load-bearing.**
Measured, not assumed: on a raw ETKDG embedding the residual distortion breaks the symmetry of
chemically equivalent ring positions badly enough to invert the ordering for phenol and toluene
(*ortho* and *meta* overlap). Relaxing first restores the equivalence — toluene's two *ortho* carbons
agree to 1e-4 — and recovers *para* > *ortho* > *meta*, while nitrobenzene correctly inverts to
*meta*. `calc.pka` already set the same flag for the same reason. A GFN2 optimization would be better
and is the first thing X3 improves; until then `structure_id` records honestly that these are
force-field geometries.

**Consequence — a one-time cache invalidation, accepted.** `calc_type` moved from `xtb` to `xtb.sp`
and the key's inputs changed, so existing `calculation_results` rows for the energy calculator are
orphaned. Energies are unchanged; only the addressing is. The cost is one recomputation of a
sub-second calculator, and it is the documented kind of invalidation (D-011: a widened key is a
correctness feature). Nothing else was touched — `XtbInput`/`XtbResult`/`run_xtb`/`run_cached_xtb`
keep their signatures and their values, and `calc.pka`'s calibrated path is untouched.

**Verification.** The physics is asserted rather than assumed: the definitional identity
f⁰ = (f⁻ + f⁺)/2 per atom, the per-molecule normalization Σf ≈ 1, benzene's six equivalent aromatic
bond orders and zero dipole, and — the discriminating case — nitrobenzene inverting to *meta* while
phenol and toluene direct *ortho/para*. A descriptor that merely tracked ring position would pass the
activating cases and fail that one. Ring positions in the tests are derived from the molecular graph,
not hardcoded, so a change in RDKit's canonical atom order cannot leave them silently checking the
wrong atoms.

## D-096 — xTB descriptors as BO featurization (U1)

**Context.** `docs/xtb-use-cases.md` §6.2 ranked this the highest-value xTB integration and noted
it needs **no new xTB capability** — only wiring. A BoFire campaign over "which ligand / base /
solvent" modelled the choice as a bare category, so the surrogate learned an independent effect per
label and could say nothing about an option nobody had run. With eight ligands and a budget of
twelve experiments, most of the budget goes to discovering that the model has no opinion.

**Decision.** `CategoricalParameter` gains two optional fields: `structures` (category → SMILES,
the declared input) and `descriptors` (category → values, the computed output). `bo.featurize`
fills the second from the first through `calc.xtb_props`, and `bo.engine` maps a featurized
parameter to BoFire's `CategoricalDescriptorInput` instead of `CategoricalInput`.

**Both halves are carried deliberately.** `structures` is provenance — which molecule produced
which descriptor row — and `descriptors` is what the surrogate saw. Storing the *values* in the
spec (rather than recomputing per round) is what keeps a durable campaign's featurization stable
across rounds and worker restarts: a campaign cannot silently re-featurize itself mid-run because
a calculator was upgraded.

**Descriptor set, and one deliberate omission.** HOMO (donor strength), LUMO (acceptor strength),
dipole (polarity), and the most positive / most negative Mulliken charge (electrostatic extremes,
carrying H-bond donor and acceptor character). The **HOMO-LUMO gap is excluded**: it equals
`lumo - homo` exactly, so shipping it alongside both would hand the GP a perfectly collinear
column — worse kernel conditioning for no information.

**The trap this decision walked into and out of.** Swapping the BoFire feature type looks
sufficient but is not obviously so: a strategy's `input_preprocessing_specs` reports ORDINAL even
for a descriptor input, which reads like the descriptors are being ignored. They are not — that
field is the *pre-processing* step, and the encoding that matters is the surrogate's own
`categorical_encodings`, which defaults to DESCRIPTOR for a `CategoricalDescriptorInput` and to
ORDINAL for a plain one. Since we *depend on a default rather than setting it*, and since the
failure mode is silent (the campaign still runs, still returns candidates, and simply stops
generalizing), `tests/test_bo_featurize.py` pins both encodings explicitly.

**Verification is the payoff, not the plumbing.** With three ligands observed and PCy3 unobserved,
the bare surrogate predicts exactly the mean of the observed values for PCy3 — the arithmetic
signature of having no information about it — while the featurized surrogate moves the prediction
toward its descriptor neighbour PtBu3. That is asserted directly, because a test that only checks
candidate shape would pass just as happily on a featurization that was wired up but inert. The
descriptors are also checked to carry real chemistry (trialkylphosphines rank above
triarylphosphines on HOMO; the aryl ligand's low-lying pi* shows in its LUMO), and the
values-matrix row/column order is asserted against the declared order, since BoFire matches by
position and a transpose would build a working campaign on the wrong molecules.

**Ancillary move.** `default_store()` moved from `agents.calc_tools` to `calc.postgres_store`:
storage is not a calculator concept, and the featurizer needs the same seam. Tests that patch it
at the importing module are unaffected.

**Limit, stated in the skill rather than hidden.** The featurization is **electronic only**.
Cone angles and buried volume need a 3D geometry, so two ligands differing mainly in bulk look
similar — a real limitation for phosphine selection specifically, and one the geometry tasks
(plan X3) would address.

## D-097 — The single point runs on a relaxed geometry, and the skill catalogue that found it

**Context.** Ideating the skill layer (`docs/xtb-skill-catalogue.md`) surfaced that
`compute_xtb_energy` is the tool an agent naturally reaches for to compare isomers, and that no
skill governed that use. Measuring before writing the judgment — the discipline that produced the
pKa finding in D-095's companion review — found a defect rather than a limitation.

**The finding.** Over five textbook isomer pairs, the single point on a raw ETKDG embedding got
the **sign of the relative energy wrong in two**: isobutane vs. n-butane, and ethanol vs. dimethyl
ether. The cause is not the Hamiltonian but the geometry — residual strain in an unrelaxed
embedding exceeds the energy difference being asked about. The same geometries relaxed with MMFF
give all five orderings correctly.

**Decision.** `calc.xtb` relaxes before the single point, via `_sp_structure`. This makes the
geometry policy uniform: `calc.pka` and `calc.xtb_props` already relaxed for exactly this reason,
and the energy path was the one that did not. Pinned by a parametrized regression test over all
five pairs, so a change that reverts the relaxation fails loudly rather than returning confident,
backwards chemistry.

**Consequence.** Cached single-point energies re-address (the geometry is part of `structure_id`),
so old entries are recomputed rather than mixed with new ones — the same clean invalidation D-095
recorded, for the same reason. Absolute energies shift slightly; every *ordering* improves.

**The residual limit, carried by a skill rather than a comment.** Relaxed magnitudes are still
poor — ethanol vs. dimethyl ether comes out ~3.5 kcal/mol against an experimental ~12. The new
`relative-energy-comparisons` skill states the rule this implies (orderings, not magnitudes; ties
under ~1 kcal/mol; same formula and charge or the comparison is meaningless, not merely
imprecise) and points at X3 for anything quantitative.

**Skill catalogue.** `docs/xtb-skill-catalogue.md` maps 28 skills across six families — product
prediction, degradation/stability, conformation, reaction design, process/formulation, and
cross-cutting — against the capability each needs. Three shipped here because they need none:
`product-prediction` (regioisomers and the kinetic-vs-thermodynamic question the tools cannot
answer for you), `relative-energy-comparisons`, and `degradation-liabilities` (forced-degradation
study design and impurity hypothesis filtering).

**What the distribution argues.** **19 of the 28 catalogued skills are gated on X3 or X4.** The
judgment layer is not the bottleneck — the capability under it is. Two entries also change the
value case for those phases: an xTB Hessian yields IR *intensities* as well as frequencies, so a
computed IR spectrum is a real discriminator between candidate impurity structures (X3); and
bond dissociation energies — radical stability, HAT selectivity, antioxidant strength — are now
unblocked at the model level, because D-095's `Structure` validates a declared multiplicity
instead of refusing every open shell. Both need only the X4 reaction composite, not new physics.

## D-098 — X3/X4: geometries, free energies, the reaction composite, and durable routing

**Context.** X1/X2 gave the xTB layer its seams and the properties a single point already
produces. Everything above that — "what does it look like", "what is ΔG", "does this reaction
go" — needed a geometry optimizer and a Hessian. The skill catalogue (D-097) had measured the
gap precisely: **19 of its 28 skills were gated on X3 or X4**.

**Decision.** Build both phases: `calc/xtb_opt.py` (L-BFGS-B over tblite's analytic gradient),
`calc/xtb_thermo.py` (finite-difference Hessian, quasi-RRHO thermochemistry, IR intensities),
`calc/xtb_scan.py` (relaxed scans), `calc/reaction.py` (balanced reaction energies and solvent
comparisons), five agent tools, and — see below — a durable execution path for the expensive
ones.

**No `ase`.** The proposal offered "`ase` (or a scipy L-BFGS over the tblite gradient)". Taking
the second: `scipy` was already resident and `minimize(method="L-BFGS-B", jac=True)` over an
*analytic* gradient is a dozen lines. ASE's `Vibrations` caches displacements **to a directory
on disk**, a side effect that does not belong inside a content-addressed calculator. `scipy` is
promoted from transitive to declared, because first-party modules now import it.

**Spec subclasses, not one widening `XtbSpec`.** `OptSpec`/`ThermoSpec`/`ScanSpec` inherit
`cache_key` unchanged — it derives from `model_dump()`, so a subclass field is keyed by
construction. Adding `temperature_k` to the base model would have put a temperature in a
*single point's* cache key.

**The optimized structure is a field of the cached result.** X1 deferred a structure store until
something produced a geometry; X3 does. It turned out to need one field, not a subsystem: the
result store already persists it, content-addressed by the optimization's own key, and `origin`
records the lineage.

**IR intensities came free.** The Hessian loop displaces every Cartesian and reads the gradient;
tblite returns the dipole from the same SCF, so dipole derivatives — and therefore a computable
IR spectrum — cost one array that was being discarded. Same move X2 made for charges and bond
orders. This is what makes `computed-spectra-comparison` shippable.

### Three defects the measurements found

**1. Open-shell energies were silently wrong.** tblite's `uhf` only sets the *occupation*; with
no spin-dependent term the energy expression does not stabilize an open shell at all. Triplet O2
came out **1.7 kcal/mol above** singlet O2 — the ground state, inverted. Adding the
spin-polarization contribution wherever there are unpaired electrons puts the triplet 15.8
kcal/mol below (experimental gap ~22) and cuts ethane's C–C dissociation error from +42 to +25
kcal/mol. Measured that this leaves the validated X2 Fukui orderings (phenol, toluene,
nitrobenzene) unchanged, so it applies uniformly rather than as a special case. Cache impact is
handled by a new `_HAMILTONIAN_REVISION` tag in `engine_version()`: a change to *how* a
calculation is set up is otherwise invisible to the key.

**2. The optimizer's first step could destroy the molecule.** L-BFGS-B scales its opening trial
step by 1/|gradient|, which on a strained geometry is wildly too large — measured on a water
with a 1.6 Å O–H, its first move collapsed the bond to **0.20 Å** and the SCF then failed to
converge at all. Fixed with a trust radius enforced through bounds, re-entered per leg.

**3. Ordinary molecules optimize onto saddle points.** A force field hands over an eclipsed
methyl and a Cartesian optimizer preserves that symmetry all the way down. Ethyl acetate — an
ordinary ester — settles at a **-42 cm⁻¹** mode, where its "free energy" is not one.
`relax_to_minimum` displaces along the imaginary mode and re-optimizes; ethyl acetate needs one
such step and lands 0.016 kcal/mol lower, which confirms the diagnosis (a shallow rotor saddle,
not a different structure).

A fourth was found by a test rather than a measurement: filtering the x/y/z rotations by
singular value looks equivalent to a proper linearity test and is not — an optimized CO2 is bent
by a fraction of a degree, so its "null" rotation survives the cut and eats a real vibration.
Rotations are now built about the principal axes and kept by moment of inertia, the same
criterion the entropy uses.

**Validation is against measurement, not against itself.** Water's standard entropy comes out
**45.05 cal/mol/K against a measured 45.10**; the ZPE, the mode counts (including CO2's 3N−5),
the n-butane torsion profile (anti lowest, gauche +0.6, syn barrier 5.7) and water's IR band
ordering are all pinned the same way.

### Temporal, and a stopgap that was the wrong call

X3/X4 were first shipped with an *atom cap and a point cap* — refusing work that would block a
turn. That was wrong, and the timings say so: a four-species reaction is 4.6 s, a seven-point
scan 4.2 s, a five-solvent screen ~25 s, and a long scan on a mid-sized molecule is minutes.
Refusing a calculation because it is slow is a worse answer than running it durably. (X1/X2 were
genuinely different: a single point is 2.4 ms, where a workflow is pure overhead.)

So the expensive tools now **route by predicted cost** (`calc/xtb_cost.py`, a power law fitted
to those measurements, used only against a threshold): under the inline budget they compute in
the turn, over it they submit an `XtbJobWorkflow` on the existing `hpc-jobs` queue and return a
job id with a push-back. One activity rather than a fan-out, because every expensive part is
already content-addressed — a retry after a worker restart walks straight through the work it
already did. The job spec is a **closed, typed union**, the same boundary rule the proposal sets
for the expert escape hatch.

**`get_qm_job_status` → `get_job_status`.** Generalized rather than duplicated: "how is my
calculation doing" is one question, and two near-identical tools is a way to have the model
choose wrong. Dispatch is on the id prefix, so a foreign id is rejected before anything is
deserialized.

**Skills.** Six new: `reaction-thermodynamics`, `conformational-analysis`,
`atropisomer-assessment` (the one with a regulatory hook — a computed barrier maps to an
interconversion half-life and therefore to an ICH class, and the method's error spans two
classes, which is the whole point of the skill), `computed-spectra-comparison`,
`solvent-selection`, `bond-strength-and-radicals`. Five existing skills updated for the widened
ladder.

**The limit carried by skills rather than code, as with pKa (D-097/U3).** GFN2 homolysis
energies are badly overestimated in absolute terms even with spin polarization, while the
*orderings* hold (benzylic C–H clearly weaker than methane's). `bond-strength-and-radicals`
states the rule this implies — rank, never quote — and the reaction result attaches an
open-shell warning of its own.

## D-099 — Durable capabilities declare their own queue

**Context.** Adding `XtbJobWorkflow` (D-098) meant editing a hardcoded list inside
`workers/hpc_worker.py`. That was the *one* extension seam left in the system that forced an
edit to infrastructure code: agent tools declare themselves with `@tool`, metrics with
`@metric`, skills by folder, MCP servers and data sources by config token — and workflows by
being remembered. The failure is silent and total: a workflow that is written, tested and
imported but missing from a worker's list never runs, and nothing fails until a job sits in the
queue forever.

**Decision.** `workflows/registry.py`, shaped exactly like `agents.tool_registry`: a
`@durable_workflow("hpc")` / `@durable_activity("background")` decorator at the definition site,
a dict per queue keyed by the name Temporal will advertise, insertion-ordered, with a duplicate
guard. Both workers now read what they serve from the registry instead of restating it, and the
startup log line is derived from it too, so it cannot go stale.

**The queue is a property of the capability, not of the deployment** (D-006): `hpc` for few
heavy workers, `background` for many light ones. Which one a durable job belongs on follows from
what it does, so the declaration belongs next to the code that does it.

**Two details the shape forced.** The key is the *Temporal* name, read from the definition
Temporal attached, not the Python name — the registry's job is catching two capabilities
claiming one name, so it has to key on the name that actually collides. And re-registering the
**same** definition is allowed, because Temporal's workflow sandbox re-imports workflow modules
to run them and would otherwise trip the guard on every workflow task; the guard compares the
defining module rather than object identity.

**What still requires an edit,** and honestly: a workflow in a *new* module needs one import
line in the worker, because importing is what triggers registration. That is the same
side-effect-import contract `agents.chemclaw_agent` has for tools, and it is one line rather
than two lists.

## D-100 — Sizing for real substrates: the workload is 200-800 Da

**Context.** The X3/X4 cost model was fitted on 3-14 atom test molecules. The actual target is
process R&D substrates in the 200-800 Da range, where conformer and job work runs in minutes,
not seconds.

**Measured, on this stack** (optimize + Hessian, one core):

| molecule                   | atoms | optimize (steps) | Hessian  | total   |
|----------------------------|-------|------------------|----------|---------|
| ibuprofen (MW 206)         |    33 |   11.6 s ( 71)   |   7.5 s  |  19 s   |
| sildenafil (MW 475)        |    63 |   66.0 s (154)   | 435.1 s  | 501 s   |
| atorvastatin core (MW 559) |    76 |   96.6 s (177)   | 218.3 s  | 315 s   |
| erythromycin (MW 734)      |   118 |  552.6 s (232)   |1007.1 s  |1560 s   |

**The old model predicted 47 s for the 76-atom case — under by a factor of seven**, and 100 s
for the 118-atom one against a measured 26 minutes. The exponent fitted on small molecules was
1.7; on real substrates it is ~3, because the fixed overhead that dominates a small molecule is
irrelevant at 76 atoms and the real scaling takes over.

**And atom count is not the whole story.** Sildenafil at 63 atoms costs *more* than the
atorvastatin core at 76 — its Hessian alone is twice as expensive — because a heteroatom-dense,
conjugated system carries more basis functions per atom and converges its SCF harder. No
function of atom count removes that scatter, so the refitted model (exponent 3.0, set to err
high) carries a factor of ~2 either way in the drug range. That is fine for its only job —
comparing against a threshold — and it is why the estimate reported to a user is an order of
magnitude, never a countdown.

**Consequences, all of them pointing the same way.** Everything in the target range now routes
to a durable job, which is correct rather than a limitation. `xtb_hessian_max_atoms` goes to 150
(an 800 Da molecule is ~120 atoms with hydrogens, so 120 was exactly at the ceiling);
`xtb_opt_max_steps` to 1500 (177 steps at 76 atoms, and the count grows with size, so 400 would
have failed large substrates *after* doing all the work); the job's start-to-close budget to four
hours. The activity now **heartbeats** between species, solvents and scan points through a
`Progress` callback (`calc/progress.py`), so a dead worker is detected in minutes rather than at
the four-hour timeout — and `calc/` still knows nothing about Temporal.

**A second finding, carried rather than fixed.** Sildenafil does **not** reach a clean minimum on
the first pass, so `relax_to_minimum`'s displacement-and-reoptimize loop is not a rare path at
drug size — and each attempt costs a full optimization *and* a full Hessian, which at 100 atoms
is tens of minutes. When the refinement triggers on a large molecule, it dominates the job. The
config comment says so; the reaction result already warns when a species is not a minimum.

**The bottleneck this exposes, recorded as X9 rather than fixed.** 177 Cartesian L-BFGS steps for
one 76-atom molecule (232 for 118 atoms) is the dominant cost, and it compounds through every scan point and every
species. A redundant-internal-coordinate optimizer typically cuts that 3-5x. The Cartesian
optimizer was the right first choice — dependency-free and easy to reason about — and it is now
the single largest speedup available for this workload.

## D-101 — X5/X6/X7: the binaries, and what they change

**X5, the `xtb` binary.** Added as a second backend behind the same task API, selected by
`settings.xtb_engine` (`auto` by default) and resolved to a concrete name *before* the cache key
is built — a key containing "auto" would mean different things on two deployments and they would
share entries computed by different programs.

**It is not a marginal improvement.** Measured, optimize + Hessian on the substrates this system
is pointed at:

| molecule                   | atoms | tblite + Cartesian L-BFGS | xtb backend | speedup |
|----------------------------|-------|---------------------------|-------------|---------|
| ibuprofen (MW 206)         |    33 |   19.0 s                  |    5.7 s    |  3.3x   |
| atorvastatin core (MW 559) |    76 |  315 s (177 steps)        |   38.1 s (39) | 8.3x  |
| erythromycin (MW 734)      |   118 | 1560 s (232 steps)        |  142.5 s (94) | 10.9x |

**This retires X9.** The internal-coordinate optimizer filed as "the single largest speedup
available for this workload" is ANCopt, and it is a process call away. Writing one would have
been a reimplementation of the reference.

**The seam is the Hessian, not the thermochemistry.** xtb prints its own thermodynamic block and
this backend ignores it, taking the Hessian matrix and handing it to `calc.xtb_thermo`. One RRHO
implementation — the one validated against water's measured standard entropy — keeps the symmetry
number an explicit input instead of xtb's silent guess, keeps quasi-RRHO identical across
backends, and therefore keeps free energies from the two comparable. The binary path reproduces
water's 45.10 cal/(mol K) exactly as the in-process path does, and that cross-backend agreement
is a test.

Also from X5: **GFN-FF**, which optimized the 118-atom substrate in 0.7 s. Not a quantum method
and it yields no orbitals, but it makes large-system pre-optimization free.

**A threading default that cost 4x.** Pinning `--parallel 1` was the cautious first choice and
made a 76-atom Hessian 98 s instead of 27 s. The default is now xtb's own (use the machine),
which is right for a dedicated worker pod; pin to 1 only where activities share one.

**X6, CREST.** Conformer, tautomer and protomer sampling. It removes this system's most pervasive
caveat — every other number describes one conformer — and supplies the **conformational entropy**
that every single-conformer free energy is missing. `compute_reaction_energy` gains
`level="thorough"`, which searches, works from the lowest member, and adds that term. It does
*not* Boltzmann-average free energies over every conformer: that is one Hessian per member, half
an hour each at 76 atoms. `treatment` on the result says which approximation was used rather than
letting a reader assume the better one.

**Rotamer degeneracy is load-bearing, not bookkeeping.** n-butane's gauche stands for two
mirror-image rotamers and its methyl rotations multiply further; weighting by degeneracy puts the
anti at 59.2% against CREST's own reported 59.14%, and the ensemble entropy at 6.23 against its
6.227. Ignoring degeneracy gives 73% — simply wrong. Both are pinned by a hand-computed test.

**CREST is the system's first non-deterministic calculator, and that had to be said out loud.**
Metadynamics samples from a random seed, so two runs differ. Everything else in `calc/` satisfies
"same key, same value"; this does not. The store is what makes it *stable* — the first run's
ensemble is what every later question sees, so a report and the number behind it cannot drift —
and `sampled: True` on the result tells a reader the populations are a sample.

This bit immediately, and instructively: the first test asserted `total_found == 2` for n-butane,
passed twice, and returned 4 on the third run because CREST split methyl-rotor variants
differently. The test now pins what is stable across runs and never a sampled count. A test that
pins a sampled quantity is a CI flake with a delay fuse.

**X7, the expert seam.** `run_xtb_task` takes a **typed spec, never a string** — no argv, no
flags, no `$...` control file, no paths. That is concrete rather than theoretical: a SMILES, an
ELN record and a retrieved document all reach this tool through the model, and xtb's control-file
syntax can reference external files and point charges. With a typed spec the worst a prompt
injection achieves is an expensive but well-formed calculation, which the authorization gate and
the cost router already bound. It is in `DEFAULT_WRITE_TOOL_GATES` — closed until an operator
grants the role — and deliberately has no second on/off setting, because two independent switches
for one capability is how a deployment comes to believe something is disabled when it is not.

Built last, as the proposal argued: after X1-X6 the list it has to cover is short — a non-default
GFN parametrization, a tightened accuracy — rather than everything the shaped tools had not got
to yet.

**Two things the binary does that its exit code does not tell you.** Its default
optimization level converges to ~1e-3 Hartree/Bohr, looser than the tolerance
`calc.xtb_opt` promises — ethanol stopped at 6.3e-4 Hartree/Angstrom against a 5e-4 target and
was correctly rejected, wasting the run; the fix is to ask for `vtight` rather than to loosen the
promise, because that promise is what makes the Hessian on top of it meaningful. And a Hessian on
**linear CO2** computes correctly — the output file holds its textbook 655/1345/2446 cm^-1 — and
then the process aborts during teardown with SIGABRT. A non-zero exit is therefore accepted when
every file the task is defined by is present, and logged; discarding a complete calculation over
a crash in its own cleanup would silently have lost every linear molecule.

**Both binaries are in the image**, as pinned release tarballs (UBI9 has neither in its
repositories). xtb is LGPL-3.0, crest GPL-3.0; both are invoked as separate processes over files
and never linked, so the usual analysis is that neither affects this codebase's licence — but
*distributing* them in an image is a decision for whoever owns the product, and the crest layer
is separable for exactly that reason. Both are optional at runtime: absent, `xtb_engine=auto`
falls back and the ensemble tools report that they are unavailable.

## D-102 — X9 revisited: preconditioning the path the binary cannot take

**Context.** D-101 retired X9 on the grounds that ANCopt *is* the internal-coordinate optimizer
and it is a process call away. That was right about the general case and wrong about the scope:
two paths cannot use the binary at all, and neither is rare.

- **Relaxed scans.** Holding atoms fixed is expressible as optimizer bounds but not as an xtb
  flag without writing a control file — precisely the input surface `calc.xtb_cli` refuses to
  have. A scan is one constrained optimization *per point*, so a 24-point profile pays the
  Cartesian cost 24 times.
- **Open-shell species**, which route to the in-process backend because the binary cannot apply
  the spin-polarization term their energy needs.

So the work was never "replace ANCopt"; it was "stop the fallback path — which handles exactly
what ANCopt cannot — from being the slow one".

**Decision.** Optimize in the eigenbasis of an approximate Hessian, scaled by the square root of
its curvature (`calc/anc.py`). The transform is **linear**, so a step is an exact Cartesian
displacement and there is nothing to back-transform — the same reason xtb's own optimizer uses
approximate normal coordinates rather than redundant internals. Frozen atoms are excluded from
the basis by construction, so the constraint is not something the optimizer can violate.

### Three things that only measurement decided

**The first version was 10x slower than no preconditioner at all.** Setting L-BFGS-B's `gtol` to
zero left it with no stopping criterion, so every leg ran to `maxiter`. The second attempt
converted the threshold into preconditioned units using the *softest* direction's scale — the
wrong end — and every leg then stopped almost immediately, failing to converge in 1500 steps. The
fix is to stop on the quantity actually promised: the objective records the Cartesian gradient
and a callback halts the leg when it meets the tolerance, so no threshold is ever converted
between unit systems.

**The eigenvalue floor is not a safety net — it is the model.** Lindh's pairwise form has no
bending or torsional terms, and on ibuprofen that leaves **37 of 99 directions with essentially
zero curvature**, where the true Hessian's lower quartile is 0.089 and its median 0.40
Hartree/Angstrom^2. The floor is the stand-in for what the model cannot see. At a safety-net
0.005 the preconditioner was *slower* than none; swept against measured step counts it optimizes
near 1.0 and turns over by 1.5.

**The payoff, at floor 1.0:**

| case                      | Cartesian | preconditioned |
|---------------------------|-----------|----------------|
| naproxen                  |  44 steps |  19 steps      |
| ibuprofen                 |  71 steps |  24 steps      |
| ibuprofen, 2 atoms frozen |  57 steps |  27 steps      |
| benzyl radical            |  10 steps |   6 steps      |

About **2x**, consistently, including both cases this exists for. Stated honestly rather than
sold: that is modest beside ANCopt's 8-11x, and the reason is visible in the number — with the
floor this high the scale ratio is only ~3, so the model is damping the stiff directions it
identifies reliably and is trusted for nothing else. A full Lindh model with angle and torsion
terms would do better, at the cost of primitive-internal machinery and a Wilson B matrix. Worth
it only if scans and radicals ever become the common case; recorded rather than built.

## D-103 — X8: the calculators as an MCP server, and the line identity draws

**Context.** The heavy half of this system's dependency closure — RDKit, tblite, scipy, and the
`xtb`/`crest` binaries — belongs to the calculators, as does the CPU load. Hosting them in the
agent's process means an optimization that saturates a core competes with a conversation for it.
The requirement was stated plainly: run them in their own pod.

**Decision.** `mcp_servers/calc` (`mcp-calc`), a third FastMCP capability server alongside
`molfp`/`rxnfp`, hosting the seven tools that compute: `compute_xtb_energy`,
`compute_electronic_properties`, `predict_site_reactivity`, `optimize_geometry`,
`compute_thermochemistry`, `predict_solubility`, `predict_pka`. Thin, like its siblings — every
body already lived in `calc/`, so this is transport. It runs as its own pod via
`CHEMCLAW_COMPONENT=mcp-calc`, or over `http` against an already-running remote.

**The tools were moved, not copied.** One capability advertised twice is a surface the model has
to choose between for no reason, and the two copies drift.

### What cannot move, and why it is not about chemistry

`compute_reaction_energy`, `compare_solvents`, `scan_coordinate` and `sample_conformers` route to
Temporal above a cost threshold; `run_xtb_task` is role-gated. All five need `require_actor()` and
`get_current_session_id()` — the turn's authenticated user and the conversation to notify, both
**ambient** and, by the F4-T3 reject-if-absent rule, never model-supplied. An MCP server is a
separate process with no conversation and no authenticated user; the only way to give it those
would be as tool *arguments*, which would make identity a model-authored value — precisely what
that rule exists to prevent.

So the boundary is **MCP carries capability, the agent keeps identity**, and it predicts what can
ever move: anything that computes, nothing that authorizes. The tools that stay are the ones that
*decide and delegate* — they price the request and either run it or hand back a job id — while the
computation itself is the same `calc/` code the server hosts.

### The one change that was not mechanical

`scripts/validate_skills` resolved a declared tool against the in-process registry only, so every
skill teaching a moved tool would have failed the gate. Widening it to include each configured
server's `allowed_tools` is not a workaround for the migration — it is the correct model: **a
skill names a capability, and which process delivers it is a deployment decision the judgment
layer should be insulated from.** The evidence that this is right is that **no skill changed** in
a migration that moved seven tools out of process. The check is not weakened: an invented tool
name still fails, and both cases are tested.

`test_mcp_transport` needed no edit either — it parametrizes over configured stdio servers, so it
picked the new one up and proved it spawns as a real subprocess advertising exactly its seven
tools, which is the boundary that keeps anything else on that server off the agent (D-029).

### A regression the migration caused, and the better mechanism it forced

Agent *profiles* attenuate the advertised surface by name, and `mcp_server_names` narrows whole
servers. So a profile that named `predict_pka` broke: the tool was no longer in-process, and
MCP attenuation was server-granular — the choice was all seven calculators or none.

That is the same mistake the skill validator would have made, one layer up: a profile is a
statement about *capabilities*, not about which process hosts them. `tool_names` now resolves
across both transports and narrows a server's `allowed_tools` to the **intersection** with what
the profile asked for, on a copy so one profile cannot narrow the surface for everyone else. A
server with nothing asked for is not attached at all. Naming one tool grants that tool, never its
server — pinned by a test, because an attenuation mechanism that silently widens is worse than
none.

**What did not move and is worth naming:** `bo/featurize.py` imports `calc.xtb_props` directly,
in-process, because it is not a tool call — the BO featurization is library use, and MCP is the
*agent's* transport, not an internal one. A second consumer of the calculators inside the same
process is not a reason to route it through a subprocess.

## D-104 — X11: two molecules together, and the half of the amine problem that is refused

**Context.** Two gaps were named together in the X11 backlog entry because both are CREST searches
this system already had wired at the CLI layer and neither had a calculator: `--nci` samples how
two molecules associate, and `--protonate`/`--deprotonate` was the presumed route to **U2**, the
basic amines the pKa predictor had never covered. Both were assumed to be work, not risk. One of
them was.

### Non-covalent complexes: the only question here about a pair

`calc.complexes` computes an interaction energy as the difference of **relaxed** species — the
complex at its best sampled binding mode, minus each monomer optimized alone. That deliberately
includes the deformation cost of binding, which a rigid-monomer definition drops and which is part
of what associating actually costs. `--nci` is what makes the search tractable: it wraps the pair
in a logfermi wall, without which metadynamics simply lets the two molecules drift apart.

Validated against CCSD(T)/CBS: water dimer **-4.97** (ref -5.0), ammonia dimer **-2.86** (-3.1),
methane dimer **-0.41** (-0.5), water-ammonia **-5.31** (-6.4). Three within a few tenths, the
mixed donor/acceptor pair 1.1 kcal/mol under-bound. Good enough to rank association strength and
to say bound or not; not good enough to quote a binding constant.

The pair is the **cache subject**: `run_cached_interaction` keys on the combined starting
structure, so A-with-B and B-with-A are one entry rather than two runs of a minutes-long search.
Two limits ship with every number and are stated in the model and the skill: it is an *energy*,
not a free energy — the association entropy that decides whether the complex exists at a given
temperature is absent, and for weak pairs it is comparable to the interaction itself — and the
search is stochastic, so a binding mode that was not sampled cannot be reported.

### Basic amines: one class calibrates better than the acids, the other is refused

Fitted over 20 experimental amines. The class splits so sharply that shipping one number for both
halves would have been indefensible:

| class | n | Spearman | R² | RMSE | ships |
|---|---|---|---|---|---|
| aromatic / aryl N — pyridines, azoles, anilines | 7 | **1.000** | 0.993 | 0.17 | yes, ±1.0 |
| aliphatic amines | 13 | **-0.17** | — | — | **no** |

Aromatic nitrogen is the *better* of this system's two pKa calibrations — better than the acid
path's ρ 0.965 / RMSE ~1.5. Held out afterwards: 1,2,3-triazole +0.57, 3,4-lutidine -0.25.

**The refusal is diagnosed, not cautious.** In the gas phase GFN2 reproduces the experimental
proton affinity order exactly (NH₃ < MeNH₂ < Me₂NH < Me₃N), so the Hamiltonian is not the problem.
Switching on ALPB **reverses** that order completely. And the true aqueous order is neither: it is
non-monotonic (Me₃N < NH₃ < MeNH₂ < Me₂NH), because aqueous aliphatic amine basicity is set by how
many hydrogen bonds the ammonium ion can donate to water — which falls with substitution, and
which a continuum model, having no explicit solvent, cannot see. **No linear recalibration
recovers a non-monotonic relationship**, so this is not a threshold waiting to be relaxed; it
changes when the solvation treatment changes, and explicit-solvent or cluster-continuum is not in
this system. ρ = -0.17 is not "imprecise", it is no ranking ability, so a number would carry no
information while looking exactly like one that did (G4).

**The base path optimizes where the acid path does not**, and that was measured too: on the same
seven references, MMFF geometries give ρ 0.893 and GFN2-optimized ones give 1.000. Protonation
pyramidalizes a nitrogen and puckers a ring — the relaxation is doing real work. The acid
calibration keeps its force-field policy because it was fitted and validated through that path;
refitting it is a separate deliberate change, not a side effect of this one.

**Acid wins when a molecule has both.** A compound with an O-H has a pKa in the ordinary sense and
that is the number the question means. The `site` field says which equilibrium was computed,
because an amine's tabulated value is its *conjugate acid's* pKa and quoting it as "the pKa" is
wrong by orders of magnitude in the wrong direction.

**What was not built.** `--protonate`/`--deprotonate` — the structural half — turned out not to be
what U2 needed. The split above is electronic and the protomer enumeration is cheap in RDKit; a
metadynamics search for protonation *sites* would not have moved a correlation that fails for
solvation reasons. Left in the backlog rather than built speculatively.

## D-105 — Fourth reconciliation with `main` (PR #28): the restored tree meets the xTB layer

**Context.** `main` landed the restore of the tree the Replit move rewound (D-091) while this
branch was building the xTB capability layer. The branch was based on the *rewound* tree, so the
merge is not two feature sets meeting — it is a feature set meeting ~38 modules it had never seen.
Five files conflicted. Two were mechanical; three were not, and each of the three was a place where
the two designs disagreed about the same thing rather than merely touching the same lines.

**The ADR numbers collided again, exactly as D-088 describes.** Both sides independently allocated
D-082…D-091. `main`'s allocation keeps the numbers — it is the trunk, it merged first, and its ids
are already cited from `BACKLOG.md`, `DEFERRED.md` and several modules. This branch's ten xTB ADRs
renumber to **D-095…D-104**, and every citation moved with them: `BACKLOG.md` (the X-entries only —
`main`'s DA-5/DA-10/TOOL-6 rows keep theirs), `tasks/todo.md`, `tasks/lessons.md`,
`calc/xtb_spec.py`, `agents/calc_tools.py`, `workflows/README.md`, `workers/README.md`, and the
three xTB design docs. `tests/test_decision_log.py`, which `main` added *as the fix for the last
collision*, is what makes this checkable rather than reviewable — and it passes.

### `_log_prediction` follows the calculators it hooks

`main` added a prediction ledger (`calc/calibration.py`, D-090's gap IDEA-2) and hooked it into
`predict_pka` and `predict_solubility` in `agents/calc_tools.py` — deliberately at the *tool* layer,
"the boundary where a prediction becomes advice a chemist acts on". X8 (D-103) had moved both of
those calculators to `mcp_servers/calc`. So the hook's stated principle and its location had come
apart.

Resolved by moving the hook, not by weakening either side: the MCP server's tool functions *are*
the tool layer now, so `_log_prediction` lives there and hooks the same two calculators at the same
boundary. It needs no ambient identity — the ledger is keyed on the canonical SMILES, not on who
asked — so it crosses the D-103 line cleanly, which is the test that boundary was written to pass.
`report_measurement` and `calculator_trust` stay in-process: they record and score, they do not
compute, and nothing about them is a calculator.

`default_store()` keeps X8's home in `calc/postgres_store.py` rather than `agents/calc_tools.py`,
because the MCP server needs it too and a tool module is the wrong place for the one naming of the
production backend.

### The registry absorbed four workflows rather than being replaced by them

`workers/background_worker.py` was the sharpest conflict: this branch reads what it serves from the
registry (D-099), `main` restored the hand-maintained lists and *added four modules to them* —
`audit_verify`, `digest`, `note_index`, `retention`. Taking the registry naively would have dropped
four workflows and six activities on the floor, silently, which is the exact failure mode the
registry exists to prevent.

So the four modules were decorated at their definition sites, which is what D-099 says adding a
durable capability means. Then the resolution was *verified rather than asserted*: the registry's
served sets were diffed against `main`'s explicit lists, and they are equal — fourteen workflows and
twenty-four activities, nothing missing and nothing extra. A merge that claims to preserve a
capability list should prove it against the list it replaced.

**One thing the merge caught that the branch had missed.** `mcp_servers/calc/server.py`'s
`predict_pka` docstring still described the tool as O-H/S-H only — stale since D-104 added
aromatic-nitrogen bases and the aliphatic refusal. The agent reads that docstring, so it was the
one place the X11 result had not actually shipped. Corrected here.

## D-106 — Heavy review of the xTB layer: five defects the tests did not catch

A full read of the branch's 12k lines against `main`. The green suite was not evidence:
every defect below sits in a path the tests exercised from the wrong side, and three of
them were **contradicted by their own docstring**, which turned out to be the most
reliable place to look. Where a module said what it did and the code did something else,
the docstring was right about the intent and the code was wrong.

### 1. GFN-FF optimization could never succeed

`_energy_and_gradient` substituted GFN2 for GFN-FF and `_optimize_with_binary` then
checked the result against this module's Cartesian gradient tolerance. A converged
force-field geometry is not a GFN2 stationary point: measured on octane, GFN2 max-gradient
**1.3e-2** against a 5e-4 target, so every GFN-FF optimization raised "did not converge".
Had one passed, its `energy_hartree` would have been a GFN2 number labelled GFN-FF.

The whole large-system escape valve — the 118-atom substrate in 0.7 s that justifies
carrying GFN-FF at all — was unreachable through `optimize_structure`, and reachable by a
model through `run_xtb_task(task="opt", method="GFN-FF")`. The docstring already described
the correct behaviour ("for it the check is skipped and xtb's own convergence stands"); it
is now implemented. `max_gradient` is `float | None`, `None` for GFN-FF only, and
convergence there is xtb's own "CONVERGED AFTER" — required, not inferred from an exit
code the module elsewhere documents as unreliable. Widening the type made `mypy` name both
call sites, which is the argument for widening it rather than returning a sentinel.

### 2. A CREST upgrade served stale ensembles

`calc_version` named the tblite/xtb build for *every* spec, including `ConformerSpec` and
`ComplexSpec`, whose work crest does. `crest_cli.binary_version()` existed and its
docstring read "for the cache key (an upgrade must recompute)" — and nothing ever called
it. So upgrading crest, the program that produced the number, changed no key and every
stored ensemble and interaction energy survived it. A dead function whose docstring
asserts a guarantee is worse than an absent one; it reads as implemented.

### 3. `engine` was inherited by two specs that never honour it

`compute_ensemble` and `compute_interaction` call `crest_cli.run` whatever `engine` says.
`XtbSpec.for_structure` rewrites `engine` to `tblite` for any open shell — so a radical's
ensemble was keyed as tblite's while crest did the work.

Both are fixed by one seam: `calc_version()` is now an overridable method, and `CrestSpec`
overrides it to key on crest's build and drops `engine` from the key entirely, with
`for_structure` a no-op. The honest consequence is now *stated* rather than hidden: an
open-shell CREST search gets no D-098 spin-polarization fallback, because there is nowhere
to fall back to. `ComplexSpec` additionally propagates its engine into the `OptSpec` its
three optimizations use, which they previously re-resolved independently.

### 4. The open-shell caveat was gated on one level

`if level == "standard" and any(multiplicity > 1)` — so a homolysis run at `thorough`, the
most expensive path, lost the warning that unrestricted GFN2 energies are an ordering
rather than a value. The caveat is about the energies, which every level differences.

### 5. Two fields that could not tell the truth

`conformer_treatment: Literal["single"] = "single"` was structurally incapable of
reporting the ensemble treatment, and was therefore wrong at exactly `thorough`, the one
level where a reader needs it. And `conformational_entropy_kcal=round(x, 3) or None` sent
a rigid species' genuine 0.000 to `None`, which means "not computed at this level" — a
different claim.

### Two smaller ones, and one calibration note left open

`crest_cli.run` promised "lowest energy first" and returned file order; crest does sort, but
`ConformerEnsemble.lowest` is `conformers[0]` *after* a truncation to `max_members`, so the
unenforced invariant would have silently dropped and misreported the lowest conformer. Now
sorted. `xtb_cli._safe` was applied to the solvent but not to `xtb_cli_opt_level`, against
the module's stated rule that every argv value is checked — operator-supplied rather than
model-supplied, so not a boundary breach, but a rule with a quiet exception is not a rule.

**Left open, deliberately:** `ensemble_seconds` has no fixed-overhead term, so it predicts
0.5 s for a water CREST search that really takes ~10 s, and small searches route inline.
It is the mirror of the error the cost model fixed at the large end. Recorded rather than
re-fitted, because fixing it properly means measuring CREST's startup across sizes, which
is a measurement session and not a review edit.

## D-107 — Fifth reconciliation with `main` (PR #31): a unit boundary and a sign, both silent

`main` landed D-092's process/analytical calculators — logD, developability descriptors, a
reaction exotherm screen, a Boltzmann conformer ensemble — plus two CI fixes, while this
branch was under review. Seven files conflicted. The textual ones were routine. Two were
not, and neither would have failed a test on either branch alone: each is a defect that
exists **only in the combination**.

**The ADR numbers collided for the third time**, so this branch shifts again, D-092…D-103
to **D-095…D-106**. `main` keeps its allocation, as in D-105. This is now a recurring cost
of parallel branches rather than an accident, and `tests/test_decision_log.py` catches it
every time — which is the argument for the check existing, not against the practice.

### The unit boundary: `geometry()` returned different units on the two branches

X1 made `calc.xtb_engine` the **single unit boundary** — Angstrom above it, Bohr only
inside, conversion in `make_calculator`/`evaluate_point`. `main` never had that change, so
its `geometry()` returns Bohr and its `gfn2_energy` consumes Bohr, which is self-consistent
*there*. It also added `positions_bohr`, a genuinely useful helper for reading one conformer
of a multi-conformer embedding by id, which `calc.conformer_ensemble` feeds straight into
`gfn2_energy`.

Merged naively, that helper hands Bohr to a function which on this branch multiplies by
1.8897 — every ensemble geometry inflated by that factor, energies wrong and entirely
plausible. Resolved by keeping the boundary and renaming the helper to
`conformer_positions`, returning Angstrom: the name now states the unit, which is what stops
the next person reintroducing it. Pinned by a test that asserts water's O-H is ~0.96 and not
~1.81 — two numbers no one can confuse.

### The sign: logD took the acid form for a base

`calc.logd` composes `predict_logd` from Crippen LogP and `calc.pka` via
Henderson-Hasselbalch, and hard-coded `logD = clogP - log10(1 + 10**(pH - pKa))`. That is the
**acid** form, and it was correct when written: `calc.pka` covered acids only and *raised*
for a base.

X11 widened `calc.pka` to aromatic and aryl nitrogen. Pyridine stopped raising and started
flowing into the acid formula, where the ionized fraction rises with pH instead of falling.
Measured: pyridine at pH 7.4 came out at **-0.92 against a clogP of 1.08** — two full log
units too lipophobic, for a base that is >99% neutral at that pH, and nothing raised.
`predict_logd` now branches on `PkaResult.site`, which is the field that makes the two
distinguishable and the reason it exists.

The general lesson is worth more than the fix: **widening a domain is a breaking change to
every consumer that encoded the old one**, even though nothing about its signature changed.
`calc.pka` gained a capability; `calc.logd` silently lost its correctness.

### Two implementations of two capabilities now coexist, deliberately

`calc.conformer_ensemble` (RDKit ETKDG + MMFF prune + GFN2 single points) alongside
`calc.conformers` (CREST metadynamics, rotamer degeneracies, conformational entropy); and
`calc.reaction_energy` (cached single points, stoichiometric coefficients, exotherm flag)
alongside `calc.reaction` (optimizes every species, Hessians, ΔH/ΔG, balance enforced).

Both pairs are kept, and neither was deleted, because the choice is a product decision rather
than a merge decision. They are also genuinely different: the CREST search needs an optional
binary and costs minutes; the ETKDG ensemble is dependency-free and always available. The
exotherm screen is a hazard flag on unoptimized geometries; the reaction composite is a
thermodynamic answer that refuses an unbalanced equation. The tool names do not collide, so
the registry is satisfied. **What is owed is a decision, not a merge**: `BACKLOG.md` carries
it as an open item rather than this ADR pretending it was resolved.

## D-108 — One conformer ensemble, one reaction composite: the duplicates are removed

D-107 kept two implementations of two capabilities through a merge and recorded that a
*decision* was owed rather than pretending the merge had made one. This is the decision,
and it was taken on the user's instruction: remove the older tools and replace them with
the framework this branch built.

**What was removed.** `calc/conformer_ensemble.py`, `workflows/conformer_job.py`,
`workflows/conformer_models.py`, `workflows/conformer_activities.py`,
`agents/conformer_tools.py` (tools `submit_conformer_ensemble_job`,
`get_conformer_job_status`), `calc/reaction_energy.py` and the `estimate_reaction_energy`
tool — with their four test modules and four config settings.

**What replaced them.** `calc/conformers.py` behind `sample_conformers`, and
`calc/reaction.py` behind `compute_reaction_energy`. Both route through the one durable xTB
job (`XtbJobSpec` discriminated union → `XtbJobWorkflow`) and are polled with the one
`get_job_status`, rather than each capability carrying its own workflow, its own models, its
own activities and its own status tool.

### Why the replacements are strictly better, and where they are not

The conformer ensembles are not close. ETKDG + MMFF prune + GFN2 singles enumerates
*embeddings*; CREST searches conformational space by metadynamics, and it returns two things
the older path structurally could not: **rotamer degeneracies**, without which n-butane's
anti population comes out at 73% against a measured 59%, and the **conformational entropy**
that every single-conformer free energy is missing. It also feeds `level="thorough"` in the
reaction composite, which the standalone workflow could not.

The reaction pair is closer, and the honest reading is that they answered *overlapping*
questions rather than one. The removed screen differenced cached single points on
force-field geometries; the composite optimizes every species, can add Hessians for ΔH/ΔG,
and refuses an unbalanced equation instead of returning a difference that includes whatever
atoms the two sides do not share. The screen's one genuine capability — the thermal-hazard
flag — was **moved onto the composite** (`is_strongly_exothermic` against the same
configured threshold) rather than dropped, and is pinned by its own test. Consolidating is
not the same as losing a feature, and the difference is exactly that port.

**Two costs, stated rather than buried.**

- The removed screen ran on **cached single points and no optimization**, so it was seconds
  where the composite is minutes. `level="quick"` is the equivalent gear — it optimizes but
  skips every Hessian — and the exotherm flag is available there. It is still slower, and
  that is a real trade for correctness (a screen on an unrelaxed geometry is differencing
  two arbitrary conformers).
- CREST is an **optional binary**; the ETKDG path needed only RDKit. The deployment image
  installs both `xtb` and `crest` (`deploy/Containerfile`), so this costs nothing where the
  system actually runs — but a bare `pip install` dev environment now has no conformer
  ensemble at all, where before it had a weaker one. `crest_cli.run` already names the
  missing binary and says which capabilities it takes with it.

### The queue was wrong too, which the consolidation fixed for free

The standalone conformer workflow sat on the **`background`** queue — many light workers.
A CREST search is minutes of saturated CPU, which is the definition of the **`hpc`** queue
(D-006). Folding it into the xTB job put it on the right one, and it is now one queue choice
for every expensive xTB task rather than a decision repeated per capability. Pinned by a test
in `tests/test_workers.py`, which previously asserted the wrong queue and now asserts why.

## D-109 — Four fixes from the live e2e pass, and two root causes that were not what they looked like

**Context.** A nine-stage live pass against the real stack (Postgres+pgvector, Temporal, real
Anthropic calls, real signed tokens) left four open findings. Fixing them changed the diagnosis of
two, and the corrected diagnoses are the part worth recording.

**1. Harness mode failed on every tool call — and the test double is why nobody knew.**
`create_harness_agent` sets `require_per_service_call_history_persistence=True`, whose middleware
replaces the outgoing messages each model call and signals "stop resending the transcript" with a
sentinel `conversation_id` on the finalized response. It also installs `MessageInjectionMiddleware`
unconditionally, which *while streaming* returns a new `ChatResponse` from
`ChatResponse.from_updates()` — and the sentinel, living on the inner response rather than on any
streamed update, does not survive. The function-invocation loop therefore re-sent the whole
transcript while history was independently re-injected, and the duplicate put a `user` block
between a `tool_use` and its `tool_result`, which Anthropic rejects outright. Both autonomy modes,
single and parallel calls, 100%.

Chemclaw sets the flag back to `False` after construction. That breaks the chain at its start —
nothing injects, so no sentinel is needed — at the cost of per-*run* rather than per-model-call
history durability, which is exactly what the classic path has always done and what
`harness_enabled=False` (the default) already gives everyone. The correct fix is upstream and is
recorded in `DEFERRED.md`.

**Decision: treat the test double's class hierarchy as production-relevant.**
`ScriptedChatClient` derived from `FunctionInvocationLayer + BaseChatClient` and its docstring
claimed that mirrored a concrete client. It did not: `BaseChatClient` is deliberately the base
*without* middleware wrapping, and the omitted `ChatMiddlewareLayer` is what consumes
`client_kwargs["middleware"]`. Every harness test ran a pipeline containing **zero** chat
middleware — including the two the harness installs — so three tests passed green against
machinery production never used. Adding the layer reproduces the failure offline with no network.
A fake that diverges from the real type's *layering*, not just its behaviour, tests nothing; the
regression tests now assert the wire invariant (every call followed by its result) over the
messages actually handed to the client.

**2. The suite was destroying live data.** Nine test files wrote to production tables with no
isolation. `test_audit_chain` truncated `audit_events` — the GxP tamper-evident hash chain — then
deliberately corrupted a row and left it that way, so `make audit-verify` failed permanently
afterwards. On the dev database this was not hypothetical: rows 1–3 of the "real" audit trail were
that test's own fixtures, with row 2 still reading `actor='attacker'`. CI never noticed because its
database is a per-run container, which is precisely why a shared database was where it bit.

**Decision: isolate by schema, carried on the DSN, not by a parameter threaded through the stores.**
Every store already resolves its connection from `settings.postgres_dsn`, so redirecting that one
value (to `options=-c search_path=chemclaw_test,public`) isolates all of them with no schema
argument anywhere in product code. `public` stays second because `vector` is installed per
database. The schema name is a constant in `tests/pg.py`, not a `Settings` field: `config.py` is
the operator-facing deployment surface and its parity tests require every field to appear in
`.env.example` — a test-only knob does not belong there.

This surfaced a real product bug (**3**): `chemclaw.db.connect` passed `options=` as a psycopg
keyword, which *overrides* the connection string rather than merging with it — but only when a
statement timeout was set, since `None` is dropped. An operator's `search_path`, `application_name`
or `work_mem` therefore vanished on some call sites and survived on others, non-deterministically.
Now merged, with ours appended last so libpq's last-occurrence-wins keeps our timeout authoritative.

**4. The orphan-`tool_use` rollback protected nothing on the path that ships.** D-091 §2 snapshots
and restores `session.state` on a client disconnect. Under `session_store="postgres"` the messages
are not in `session.state` — `save_messages` has already committed them — so the orphan survived
the rollback meant to discard it, and every later turn on that session replayed it into the same
400. **Decision: enforce the invariant on read, and make the rollback durable as well.**
Read-time repair (`PostgresHistoryProvider.get_messages` drops and deletes unanswered calls) is the
load-bearing half, because the disconnect handler is not the only way a turn dies between writing a
call and writing its result — a `SIGKILL`, an OOM, or a pod eviction runs no Python cleanup at all,
and the harness's per-service-call persistence had been *widening* that window by writing the call
before the tool ran. It also heals sessions already broken in the field. The watermark rollback is
kept alongside it because the two differ: repair removes orphans, whereas the rollback's contract
is that a half-written turn is discarded whole.

The pairing rule lives in `agents/message_pairing.py` with two forms, and the distinction is
load-bearing: `unmatched_call_ids` (by id, order-independent) decides what is safe to *delete from
storage*, where a merely out-of-order pair is intact history; `calls_without_adjacent_results`
(the stricter wire rule) validates what is about to be *sent*. Using the lenient one on the wire
would have missed finding 1 entirely — duplicated history leaves a second, unanswered copy of a
call whose id does appear answered once.

**5. RBAC denial narration — the reported cause was wrong.** The pass attributed inconsistent
narration to tool docstrings ("gated" tools explained themselves, others did not). No tool
docstring mentions gating, permissions, or privileges anywhere. The actual cause: `authorize_tool`
emits three different messages, and under the shipped default only the five
`DEFAULT_WRITE_TOOL_GATES` tools can ever be denied — so the self-explaining "lacks a privileged
role" message was the only one anyone had seen. The deny-default message, "not in the tool
allowlist (deny by default)", is written from the perspective of whoever edits the config, and the
model relayed it as "not currently available… a configuration issue" — which sends a chemist to
report a bug rather than to request access. **Decision: all three refusals share one chemist-facing
shape** (who, which tool, why), the operator's remedy moves to the docstring and runbook, and
`_INSTRUCTIONS` gains a passage on narrating a refusal — mirroring the compaction passage, which
was already the house pattern for honest limitation-reporting. Verified live: all five previously
vague read tools now state it is an access decision and say how to get access.

**5b. The test schema is per-process.** Found by hitting it: the session fixture *drops* its
schema on the way out, so a fixed name means a second pytest run deletes the first run's tables
mid-flight — which is what happened when a single test file was run while the full suite was
going. The schema is now suffixed with the pid, verified by running two suites concurrently
against one database and confirming both pass and neither leaves residue. A hard kill can strand
an orphan schema; it is inert and unmistakably named, which is the right trade against the
alternative of a shared name that is unsafe by construction.

**5c. A converged geometry was not a fixed point.** Unrelated to the four findings above and
folded in only because it blocked this branch's CI: `tests/test_xtb_opt.py::test_a_converged_
structure_is_a_fixed_point` failed identically on pristine `main` (verified in a clean worktree —
same two structure ids), so it was `main`'s failure, not a merge artifact.

The in-process optimizer's loop was bounded only by the step count, so it always ran at least one
leg before testing convergence. Re-optimizing an already-relaxed water therefore moved it 3e-4
Angstrom, and a third pass moved it again. Because a structure id is a hash of the coordinates,
every pass minted a new id — which silently forks the calculation cache and quietly voids the
"compute once, never recompute" guarantee (D-011) for every task keyed on a geometry. The test was
right to call this out; it was pinning a property the code did not have.

The fix seeds the convergence test from the *input* geometry's gradient and makes the loop
`while max_gradient > tolerance and steps < max_steps`. It costs nothing: `evaluate_point` already
computed that gradient for the initial energy and discarded it. An already-minimal structure now
runs zero legs and returns byte-identical. Scoped to the library backend, which is the one
reachable here; whether the `xtb` binary's own ANCopt has the same property is untested, because
the binary is not installed in this environment — flagged rather than guessed at.

**5d. The durable-capability registry was not re-import safe.** The second of two failures
inherited from `main` rather than caused by the merge, and the more interesting one, because it
could only ever fail where Temporal actually runs.

`workflows/registry.py` anticipated the sandbox: its duplicate guard compares the defining
*module* rather than object identity, precisely so that Temporal re-importing a workflow module
is not mistaken for two capabilities claiming one name. Having allowed the re-registration, it
then stored it — and the sandbox's re-import builds a *new* class object for the same definition,
so the registry quietly swapped out the very object `workers/hpc_worker.py` captured at import
time. `HPC_WORKFLOWS == registered_workflows("hpc")` then compared two classes that print
identically and are not the same object: `QMJobWorkflow != QMJobWorkflow`.

The fix is to keep the *first* registration and return the incoming object unchanged, so Temporal
still receives the class it built while the registry keeps the one the workers hold. That is what
the guard's own docstring already implied; only the store was missing it.

Worth recording as a testing lesson rather than a one-line fix: `test_workflow_registry` already
had a re-registration test, and it passed throughout — it counted entries *by name*, which is
invariant under exactly this bug. The assertion that mattered was identity, and it was missing.
The regression test now added builds the second class object by hand, so it reproduces a sandbox
re-import with no Temporal server at all — the failure was otherwise invisible in any environment
where the test server cannot be downloaded, which is every environment this was developed in.

**6. ADR numbers now have an allocation ledger.** This ADR was written as D-092, renumbered to
D-095, then to D-109 — three collisions in one day, each found only when a merge conflicted. The
cause is structural: concurrent branches all append to the end of `DECISIONS.md` and all compute
"highest visible + 1" against their own branch, which by construction cannot see the others.
`ADR-REGISTRY.md` is the ledger — one line per number, so "what is taken?" is a grep against
`origin/main` rather than a scan of a 3,700-line document — and `CLAUDE.md` carries the procedure:
enumerate against `origin/main`, reserve in the *first* commit, and on a collision the branch
merging **second** renumbers (a rule, so neither session waits for the other).

Stated honestly, because a ledger that overpromises is worse than none: **this does not prevent
collisions.** Two branches can still append the same number to the ledger. What changes is the
cost — a one-line conflict a grep finds, instead of a ninety-line conflict inside a prose block
where the number is easy to miss. The collision-proof fix is to drop the global sequence for
date-plus-slug ids; that is a convention change worth making deliberately if this recurs, and it
is recorded as the escalation rather than done unilaterally here.

## D-110 — The connector seam: one way to add a tool, a skill, or an agentic workflow

**Context.** Five extension seams existed, and adding a capability meant touching four unrelated
places — a `@tool` function in `agents/`, a `settings.mcp_servers` entry, a bespoke Temporal adapter
plus a hand-maintained worker list, and a `SKILL.md` folder — three of them Python edits to
orchestration code. A *capability* was a concept the codebase could not name, and its dependencies
(`rdkit`, `torch`, `bofire`, `tblite`) all lived in the chat service's image because tools ran
in-process. `deploy/helm/.../deployment-mcp.yaml` had anticipated the fix and sat inert since F6.

**Decision.** A capability is a **bundle**: `connectors/<name>/connector.yaml` declaring everything it
contributes — the MCP tools its own FastAPI server serves, the durable jobs its own Temporal worker
runs, the skills that teach them, the agent profiles they enable. Discovered by folder (as skills
are), validated by a pydantic manifest with `extra="forbid"` (as `SKILL.md` frontmatter is), enabled
by one config token (as data sources are), gated by `make connector-validate` in CI (as notes and
skills are). No new vocabulary: registry + discriminated union + filesystem discovery + enable-token,
exactly the four shapes D-081 settled on.

`mcp_servers` and its three spec models are **removed**, not deprecated — two mechanisms for
registering a capability is the problem this solves, so keeping one as a compatibility path would
preserve it. `molfp`/`rxnfp` are re-hosted as HTTP connector bundles over their existing FastMCP
capability (`mcp_servers/` unmoved), and the Helm chart now computes `CHEMCLAW_CONNECTOR_URLS` from
the same `.Values.connectors` block that creates the Services, so the addresses the front door dials
cannot drift from the pods that exist.

**Durable jobs: core wrapper over a connector-owned workflow.** `ConnectorJobWorkflow` keeps every
obligation that must not vary per capability — the idempotent workflow id (D-011), `require_actor`
attribution (F4-T3), the PR-gate publish through the *existing* `publish_memory_note_activity`, and
session push-back (F3-T3) — while the connector owns the workflow and its worker, addressed by
**workflow type name + task queue** as strings from the manifest. Core imports nothing from a bundle,
and moving a workflow between workers is a one-line manifest change. The contract is one envelope
(`ConnectorJobResult`: summary, data, optional `Note`), and typing the note as the existing frozen
`Note` means a connector's proposal passes the graph's own validators at the boundary.

A `jobs:` entry declares its arguments either inline (closed scalar types → a generated pydantic
model → a real typed schema) **or** by a `module:Attribute` reference to an existing model. The second
is what makes "any tool" true rather than aspirational: `CampaignSpec` nests a discriminated
optimization problem that YAML cannot re-declare without losing the structure that makes the model
call it correctly — and re-declaring it would be a second source of truth for a schema that already
exists in code.

**The four existing bespoke adapters are deliberately not migrated.** `submit_qm_job`,
`request_development_report` and `start_optimization_campaign` wrap workflows returning typed domain
results their callers consume, not the envelope. Converting them now means either changing three
tested durable workflows' return types — orphaning in-flight histories for no functional gain — or
stacking a third wrapper layer. In Stage C their code moves into its bundle and the moved workflow
returns the envelope directly: one change instead of two. Until then core durable capabilities and
connector durable capabilities coexist by design, and the generic path is what every *new* one uses.

**Two findings from measurement, which changed the design.** Both are recorded because both look
settled from the API surface and are not.

1. **MAF's `header_provider` does not work over streamable HTTP.** It is invoked, with the right
   values, and the server receives nothing: MAF passes the headers through a `ContextVar` set in
   `call_tool` while the request is issued by the MCP transport's `post_writer` task, created when the
   connection opened. A request hook on our own `httpx.AsyncClient` runs *in* that task and works.
   (MAF documents the sibling trap for auth itself: provider headers are absent during
   `session.initialize()`, so a credential passed that way 401s at connect. Auth is an `httpx.Auth`
   on the same client.)
2. **Connectors must be built per turn, not per process.** A connection's transport tasks inherit the
   context of whoever opened it, so connect-time identity is only truthful if a connection belongs to
   one turn. Probing the shared-object shape showed it is worse than inaccurate: **two concurrent
   turns over one connector tool deadlock** — a pre-existing hazard on the stdio path too, since
   `run_turn` has always entered process-lived tools' contexts per turn. So connectors are not
   attached by `build_agent`; `connector_tools()` builds them per turn and the caller passes them to
   `Agent.run(tools=…)`. One fix for both problems, pinned by a concurrency test.

**Consequences.** The identity headers are advisory and stay that way: audit and per-tool authz run in
core before a call leaves the process, and a NetworkPolicy restricts connectors to Chemclaw's own
pods — a connector must never make an access decision on a header's word. The agent-facing `tools`
allow-list is read/compute-only *by validated contract* (`make connector-validate` refuses a mutating
name), so mutation stays on the job path or the core PR-gate tools. An unreachable connector costs its
tools and not the turn, reported by `/readyz` and the `chemclaw_connectors_unhealthy` gauge;
`connectors_required` inverts that to fail-fast for a deployment that prefers not serving.
`SkillManifest` drops `mcp_servers` for the finer-grained `tools`, validated against the whole surface
including out-of-process tools — the coarser field would have passed while the tool it taught was gone.

Design, staging and open questions: `docs/connector-plan.md`. Supersedes the `mcp_servers` half of
D-029 and D-081's transport union; the rest of D-081 stands.

## D-111 — Stage C: the domain connectors, and two defects the migration surfaced

`safety`, `chem` and `calc` moved out of the agent's process to their own bundles (D-110's seam).
Five connectors ship now; `rdkit`, `tblite` and the calculation store's driver are no longer the chat
service's dependencies, which was the operational point of the exercise.

**Verification came first.** Two of the four safety-rubric invariants are MAF function middleware
over tools we did not write, and MAF assembles MCP tools into a run's tool list separately from the
configured ones — so whether audit and authz reach a connector's tools is a property of the framework
that no amount of reading our wiring establishes. `tests/test_connector_safety_rubric.py` drives a
real agent, MAF's own tool-calling loop and a real connector server, and asserts on the audit sink
and on what the server observed. Both hold: a connector call is audited with the turn's actor, and a
`tool_role_gates` denial is recorded as an error while the tool body never runs. Had either failed,
the migration would have been unsafe and Stage C would not have proceeded.

**Two defects, both found by the existing suite, both fixed at the root:**

1. **Swallowing `CancelledError` in `connectors.transport` broke the front door's turn bound.** The
   degrade-on-connect-failure mixin caught it — following MAF, which swallows it in its own MCP paths
   because an internal `anyio` cancel scope is indistinguishable from a real cancellation. At *this*
   layer it is distinguishable: `Task.cancelling()` is non-zero only when cancellation was requested
   on this task, which an inner scope never does. Without that check a hung turn ran to completion
   holding its admission permit — precisely the collapse `service_turn_timeout_seconds` exists to
   prevent, and a much worse failure than the one the swallow was protecting against.

2. **`AgentProfile.tool_names` could no longer reach a migrated tool.** Profiles had two dials —
   `tool_names` for in-process tools, `mcp_server_names` for whole connectors — which was coherent
   while capability lived in-process and became incoherent the moment it did not: a profile could
   name a whole `calc` connector but not "just the two predictors". `tool_names` now spans both
   halves, narrowing the in-process tools *and* each connector's agent-facing allow-list, dropping a
   connector left with no named tool. Mutating `allowed_tools` per instance is safe only because
   connectors are per-turn objects (D-110) — on a shared connector it would have been a cross-turn
   surface change. The unknown-name check moved to the union, since only a view of the whole surface
   can tell a typo from a name that lives on the other side of the boundary.

**A boundary clarification worth stating.** `calc` exposes `report_measurement`, which writes. The
read/compute-only rule for a connector's agent-facing tools is about the *knowledge graph and the
fingerprint index* — the paths the PR-gate governs — not about all state: a capability's own store is
its own business, and the calibration ledger is `calc`'s. What remains structurally impossible from a
connector is unchanged: it cannot write a graph note (its only route is a job result core publishes)
and cannot launch durable work (a `jobs:` entry is a core-generated tool).

Remaining in Stage C: `kg` (needs a decision on whether it also owns re-indexing) and `bo` (whose
workflow moves to its own worker, taking `start_optimization_campaign` onto the generic job path with
it). See `tasks/todo.md`.

## D-112 — `bo` as the reference connector-owned durable capability

The `bo` bundle is the one that proves the durable half of the seam rather than describing it. It
owns both flavours: `suggest_next_experiment` is an inline MCP tool on its own FastAPI server, and
the campaign is a `jobs:` entry whose workflow, activities and **worker** all live in the bundle,
polling `connector-bo`. Core's background worker no longer serves any BO workflow or activity, and
`agents/durable_tools.py`'s bespoke campaign launcher is deleted — the manifest replaced it.

**What this establishes.** Moving a durable workflow out of core was one manifest entry plus changing
the workflow's return type to `ConnectorJobResult`. Nothing in core was edited to accommodate it,
because `ConnectorJobWorkflow` addresses the child by workflow *type name* and task queue, both
strings from `connector.yaml` — the property D-110 claimed and this is the first exercise of. The
practical payoff is that `bofire`/`botorch` now load only in the bundle's two processes.

**The PR-gate split, made structural.** `write_campaign_node` — an activity that both *built* the
recommendation note and *published* it — is gone. The mapping (BO result → note) stayed in the
bundle, because that is the domain's knowledge; the publish moved to core, because the PR-gate is the
GxP boundary. A connector now returns a note in its envelope and cannot reach the gate at all, which
is a stronger statement than "it is not supposed to".

**One new manifest field, and why it earns its place with a single caller.** The deleted adapter
enforced `require_rounds_within_ceiling` before starting: a campaign re-sends its whole observation
history each round, so history grows quadratically and past the ceiling Temporal terminates the run
mid-flight, losing every already-paid evaluation. Migrating the job would have dropped that guard
silently. Every other placement is replay-unsafe — a validator on `CampaignSpec` or a check inside
the workflow re-runs during replay against *current* config, so lowering the ceiling would
retroactively fail an in-flight campaign that was legal when it started. The launch boundary is the
only safe place, and after the factory replaced the hand-written adapters that boundary is the
generated tool. So `JobSpec.precondition` names a `module:function` the factory calls before any
durable work. It has one caller today and is not speculative: without it, migrating a job to the
generic path is a silent regression, which is the opposite of what the seam is for.

Remaining in Stage C: the `kg` bundle, and the `qm`/`report` jobs, which follow the same shape once
their workflows move and return the envelope directly.

## D-113 — Stages D and E: profiles select an agent, templates fix a procedure

The connector seam (D-110) made *capability* one thing to add. These two stages do the same for the
two ways an "agentic workflow" is configured, and the decision worth recording is that they are two
things and not one.

**A profile (Stage D) configures an agent; the model still chooses the order.** It is a YAML file
under `profiles/` — or inside a connector bundle, when it is about that one capability — naming
instructions, a narrowed tool set, and the harness settings. A session picks one with
`POST /sessions {"profile": ...}`; an unknown name is a 400, not a silent fallback to the default,
because a caller that asked for a narrowed agent and quietly got the full one is the failure mode
worth being loud about. Agents are built once per profile and cached on the app, so the profile is a
key rather than a per-turn cost.

*The filename is the name.* `profiles/property-lookup.yaml` is `property-lookup`, and a `name:` key
in the body is refused rather than merged. Two sources of truth for one identity is drift waiting to
happen, and this is the same rule `skills/` already follows.

**A template (Stage E) fixes the procedure; the model only fills the gaps.** Also a YAML file, also
discovered, also enabled by one config token — but it runs as a Temporal workflow with an ordered
step list. Three kinds: `tool` (call anything on the agent's surface), `job` (run a connector's
durable job and *await* it), `agent` (one model turn under an optional profile). The last is what
keeps a template agentic rather than a script: the sequence does not vary, the reasoning inside a
step does.

**Why both, when the user's ask was "configure an agentic workflow easily".** A profile cannot
express "these five steps, in this order, every time" — the model may reorder or skip, which for a
safety screen preceding a written brief is precisely the judgment nobody wants delegated. A template
cannot express open-ended research. The shipped pair demonstrates the split: `property-lookup`
narrows to four calculators and lets the model work; `hazard-briefing` screens, then searches
precedent, then writes — in that order, durably, or not at all.

**Substitution is deliberately not a template language.** `${inputs.x}` and `${steps.id.result}`,
nothing else — no conditionals, loops or expressions. Those are how a config format becomes a
programming language with no debugger, and a procedure that needs them wants an agent step or real
code in a connector, not more YAML. Two rules inside that small surface earn their complexity: a
whole-string reference substitutes the *value* with its type (so a tool wanting `list[str]` does not
receive the repr of one), while an embedded reference interpolates JSON text (so a prompt reads);
and an unresolvable reference raises rather than yielding `None`, refused at *validation* time so a
broken template cannot start rather than dying on step four having already spent the compute.

**The resolved template travels in the workflow input, not its name.** Editing
`templates/<name>.yaml` therefore cannot change a run already in flight. That is the versioning
story — no migration, an edit affects only later runs — and simultaneously a hard replay
requirement: a workflow re-reading a file on replay would diverge from its own history and Temporal
would reject it.

**Identity travels too, and is re-stamped per step.** A workflow has no request context, so the
actor and roles ride in each activity's input and are set ambient before the work happens. The part
that matters is `run_tool_step` applying the audit and authz middleware *by hand*: MAF applies an
agent's middleware inside its own tool-calling loop, which a template does not go through, so a
direct `tool.invoke(...)` would run ungoverned. A template must not become a way to run a tool the
requester could not run directly, and that line is enforced there.

**Two omissions the gate caught, worth naming because both would have shipped silently.** The image
never `COPY`d `templates/` or `profiles/`: a discovered-from-disk seam that is missing simply
advertises less, so the container would have started clean and offered fewer capabilities.
`test_image_ships_every_first_party_package` catches the first by discovery; the second it structurally
cannot (no `__init__.py`), which is the argument for the explicit `COPY` and the comment above it.
`connectors/` and `templates/` were also both absent from `make type`'s package list — checked
transitively, never directly.

**Deviation from the staged plan.** Stage E was gated on "a second real use case a profile provably
cannot express". The user overrode that gate and asked for it built; `hazard-briefing` is the one
worked case, not two. The gate existed to prevent building a step engine nobody needed, so the risk
it was guarding — a second caller failing to materialize — remains open and is noted here rather
than presented as retired.

## D-114 — Sixth reconciliation with `main`: the xTB layer meets the connector seam

Two branches solved the same problem in the same window without knowing it. `main`'s X8 moved the
seven calculators out of the agent's process behind an MCP server because "the calculators carry the
heavy half of this system's dependency closure"; the connector seam (D-110) built the general
mechanism for exactly that. The merge is where they become one thing, and the interesting part is
what the merge *exposed* rather than what it moved.

**Convergent evidence, worth stating.** X8's reasoning and D-110's are nearly word-for-word — the
capability scales on its own pod, judgment stays out, only DTOs cross. Two independent derivations
of the same boundary is the strongest argument either has, and it settles the "is this seam the
right shape" question better than another round of design would.

**What was duplicated, and how the merge chose.** `mcp_servers/calc/server.py` and
`connectors/calc/server/tools.py` both defined `predict_pka`, `predict_solubility` and
`compute_xtb_energy` — two live definitions of one tool, differing in one place (X11's base-pKa
support, which the connector's copy lacked). The bundle is the surviving home and took `main`'s
better bodies plus its four newer calculators. `mcp_servers/calc/` is deleted. `mcp_servers/molfp`
and `mcp_servers/rxnfp` stay as the implementation modules their bundles wrap — those are one
capability with one definition, which is not the defect this was.

**The defect the merge exposed, and the reason this ADR is not just a merge note.** Five tools —
`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`,
`compute_interaction_energy` — stayed in-process on `main` with an explicit and well-argued
justification: they submit durable jobs, submitting needs `require_actor()` and
`get_current_session_id()`, and those are ambient to the turn and never model-supplied (F4-T3). The
argument is correct. Its conclusion was not, and after the merge the cost was visible: because they
route by *predicted* cost, they import `calc.xtb_cost`, `calc.reaction`, `calc.complexes` and
`calc.conformers` — so the chat service's image still loaded the entire heavy chemistry closure, and
the `calc` connector saved nothing it was built to save. The merge also left them **orphaned**: no
module imported them any more, so five capabilities were silently absent from the agent, caught by
`make skill-validate` rather than by anything at run time.

**The fix, and why it is better than what it replaced.** A new `JobSpec.inline_wait_seconds`: the
generated launcher starts the durable run and waits a bounded moment for it, returning the result if
it arrives and a job id if it does not. Identity never leaves core — the launcher is core's, running
in the turn, exactly as before. The capability never leaves the connector. One model-facing tool
serves both the two-second case and the twenty-minute one.

That it *replaces a prediction with a measurement* is the part worth keeping. A cost model is a
second model of the calculation and can be wrong in both directions: a mispredicted "cheap" call
blocks the turn anyway, and a mispredicted "expensive" one is deferred for nothing. Elapsed time
needs no model and cannot be wrong. And a prediction can only live where the cost model lives, which
is what had put chemistry in core in the first place — so the simpler mechanism is also the one that
removes the coupling. The wait is cancel-safe by construction: `asyncio.wait_for` cancels the waiter,
never the workflow, so an abandoned turn leaves a run that still completes, still caches and still
pushes back.

All five share one workflow. `XtbJobSpec` was already a closed union discriminated on `kind`, so each
job references its own member as `params_model` and `CalcJobWorkflow` dispatches — one durable path,
five separately-documented tools, because "compare these solvents" and "scan this bond" are different
questions even when the machinery is identical.

**Three consequences, none of them silent.**

1. **`run_xtb_task` is deleted.** X7's expert escape hatch took the raw union; the five typed jobs
   now cover that union exactly, so it had become a sixth tool doing what the five do, chosen by the
   model. Its role gate did not vanish with it: it existed for *unbounded* calculations, so it moved
   onto the two CREST searches (`sample_conformers`, `compute_interaction_energy`) as
   `expensive: true`. Dropping a gate along with the tool it guarded is how a posture loosens
   quietly.
2. **`get_job_status` narrowed to HPC/DFT, and `get_durable_job_status` grew a result.** The former
   dispatched on an id prefix over two kinds; one of those kinds no longer exists. The latter used
   to return a bare status word, which left a chemist holding a completed connector job with no tool
   that could fetch it — the connector envelope made that answerable, so it now reports the summary
   and the structured result in the same call.
3. **`main`'s `workflows/registry.py` is adopted, and a connector stays out of it.** The declarative
   `@durable_workflow(queue)` seam fixes a real failure — a workflow written, tested and imported but
   missing from a worker's list never runs — and core's two workers now assemble from it.
   A *connector's* workflows are deliberately not registered there: that registry serves core's
   queues, and a bundle polling its own queue on its own worker is the whole point. The test asserts
   the absence rather than the presence, because a connector workflow drifting back onto a core queue
   is the regression that would quietly restore the coupling this removed.

**ADR renumbering.** The branch's four ADRs were written as D-092…D-095 while `main` independently
used those numbers. They are D-110…D-113 here, with every in-repo reference updated. Numbering
collisions are the predictable cost of an append-only log on two branches; the alternative (a
reservation) is worse than renaming on merge.

**Two production gaps closed while reviewing, both of the same kind — a gate that existed but was
not wired.** CI ran `make skill-validate` and neither `connector-validate`, `template-validate` nor
`prose-validate`, so three of the five gates the seam added were enforceable only by hand; they are
CI steps now. And the image never `COPY`d `templates/` or `profiles/` (D-113) — both discovered from
disk, so the container would have started clean and simply offered fewer capabilities. A validator
nobody runs and a directory nobody ships fail the same way: silently, in the direction of less.

## D-115 — The two remaining Stage C items, answered: neither becomes a bundle

Both open points closed by measuring rather than by preference, and the measurement says no in both
cases. Worth recording because "everything becomes a connector" is the wrong reading of D-110: a
capability earns a bundle by taking a dependency closure *with* it, and a tool that leaves the
closure behind gains nothing but a second code path.

**The `kg` bundle: won't build.** The open question was whether it would also own re-indexing. The
answer is that the question does not arise, because the graph is not a peripheral capability — it is
core's own data layer. Thirteen core modules import `kg`: the PR-gate, all six memory layers, the
report retrievers, the eval verifier, the note index. Moving `find_notes`, `expand_note` and
`find_knowledge_gaps` out would leave every one of those imports where it is, for a dependency win
of exactly zero, and add a second read path to one note tree. Re-indexing stays in core for the same
reason and one more: it is triggered by a merge into the note repo, which core owns. The rule is
written into `connectors/manifest.py`'s docstring and the runbook so the next author who notices
`find_notes` is not behind a connector finds the answer instead of re-deriving it.

**The `report` job: the envelope, not a bundle.** Its closure — the graph, the retrievers, the
embedding index — is what core keeps for `gather_evidence` regardless, so the isolation half buys
nothing. But the *uniformity* half turned out to matter: `DevelopmentReportWorkflow` returned a bare
note-ref string, which made the report the one durable job `get_durable_job_status` could report
`completed` for while having nothing to hand back. It now returns `ConnectorJobResult` and stays on
core's background worker.

It still publishes its own note rather than returning one for core to gate — correct here for
precisely the reason it would be wrong in a bundle. The note *reference* is the workflow's result, so
publishing is the work rather than a side effect, and this workflow already sits on the side of the
boundary the PR-gate lives on. A connector cannot make that claim, which is why D-112 took the
publish away from `bo`.

**What this leaves in core, as a closed list rather than a backlog:** conversation plumbing, the two
PR-gate writers, the knowledge-graph reads, `submit_qm_job` (it needs the HPC identity bridge, which
is core's), the report, and the two status tools. Every one of those is a rule with a reason, and
`tests/test_tool_registry.py` pins the set so adding to it is a reviewed edit.

## D-116 — Seventh reconciliation with `main` (PR #30): two capabilities the merge silently restored

The e2e-testing branch merged into `main` while this one was open, and the reconciliation is
mechanical in the direction that matters — `main` does not have the connector seam, so every
conflict where it re-introduces `settings.mcp_servers`, `agents/calc_tools.py`, `agents/bo_tools.py`
or `workflows/bo_campaign.py` resolves to this branch. What is worth recording is the two places
where "resolve to ours" was the *wrong* answer, and the class of defect that produced them.

**A merge that deletes a file on one side and edits it on the other restores the file.** Git reports
that as `modify/delete` and asks; it does not report the *transitive* case, where the deleted file's
module is still imported. Four modules came back this way — `workflows/xtb_job.py`,
`workflows/xtb_activities.py`, `agents/xtb_job_tools.py`, `agents/xtb_expert_tools.py` — all replaced
by the `calc` bundle's durable half in D-114, none flagged as a conflict, because on this branch they
simply no longer existed. Two of them were dead-but-harmless; `xtb_job_tools.py` imported a module
this branch had deleted, so it was an `ImportError` waiting for the first test that touched it.

**The one that would have been a real regression.** `connectors/bo/activities.py` came back carrying
`@durable_activity("background")`. Git's rename detection had matched it to `main`'s
`workflows/bo_activities.py`, which legitimately registers on core's queue — so the decorator
followed the file across the boundary. The effect: core's background worker would have served the BO
activities again, loading `bofire` and `botorch` into the process the bundle exists to keep them out
of. Nothing about it looks wrong in a diff; it is three decorator lines in a file whose contents are
otherwise correct.

`tests/test_workflow_registry.py` caught it, and how it caught it is the lesson. `main`'s
`test_every_declared_capability_reaches_its_worker` asserts `BACKGROUND_WORKFLOWS ==
registered_workflows("background")` — a snapshot taken at worker import compared against the live
registry. A capability registering *after* that snapshot makes the two disagree, which is exactly
what a stray connector registration does. The absence-assertion added in D-114 covers the same
boundary from the other side; between them the failure is now caught twice, and the docstring in
`connectors/bo/activities.py` says why the decorator must not be there.

**Adopted from PR #30, each verified present after resolution rather than assumed:** the two
error-surfacing middlewares (`surface_authorization_denials`, `surface_domain_errors`) around audit
and authz — the chain is four deep now, not two; the BO argument coercion, which this branch had to
*port* into `connectors/bo/server/tools.py` because that file is a rename of the module the fix
landed in, so the merge kept this branch's older body (a plain "ours" resolution would have dropped a
live-e2e finding: the model sometimes JSON-encodes the observations array as a string); the report's
retrievers coming from `sources.registry.active_retrieve_sources()` rather than a hardcoded
`GraphRetriever()`; `find_notes` matching every query word independently; the xTB fixed-point fix;
and the registry's re-import safety.

**Two of this branch's own tests were wrong in the same way, and it is worth naming.** Both asserted
a *count* where they meant a *property*: the middleware chain "has length 2", and an authorization
message contained one specific phrase. Both broke on additions that were improvements. They now
assert what they meant — the narrowed agent's chain equals the default agent's (by name, since the
audit entry is a per-agent closure), and the denial names the actor and the tool. A test that pins an
incidental number is a test that will one day block a good change and teach nobody anything.

**ADR numbering, per the ledger rule `main` added in the interim.** That rule says the branch merging
second renumbers; this is that branch. `main` had taken D-109, so this branch's six ADRs moved from
D-109…D-114 to D-110…D-115, with every in-repo reference updated, and all seven numbers are now
reserved in `ADR-REGISTRY.md` — which is the mechanism that should make this the last renumber.

## D-117 — Consolidating the outstanding branches, and deleting what four generations of the design left behind

Three branches were open against `main`, two of them with live PRs, and none of them could be
merged. All three were cut before the Replit monorepo restructure, so `git diff main..branch`
reports a whole-tree file move: the branch's `agents/`, `workflows/`, `workers/` and `tests/` sit at
the repository root while `main`'s sit under `services/chemclaw/`. `git cherry` marks every commit
unmerged, which reads as "none of this work has landed" and is wrong — it is a patch-id artifact of
the move, and most of the content *had* landed by other routes.

**What a merge would actually have done.** `git diff --diff-filter=A main..branch` — the files a
merge would add — is the honest measure. Every added path is at the old root layout, so a merge
re-creates the whole service a second time in the wrong place. Worse, several of the additions are
modules `main` deliberately deleted: `agents/calc_tools.py` and `agents/bo_tools.py` (D-111/D-114),
`deploy/helm/chemclaw/templates/deployment-mcp.yaml`, `tests/test_mcp_server_spec.py`,
`tests/test_mcp_transport.py`, `workflows/bo_campaign.py`, and two `SKILL.md` files that moved into
`connectors/*`. Merging would have resurrected the pre-connector-seam architecture D-110 retired,
duplicated at paths nothing imports. So the branches are *ported*, not merged, and then deleted.

**What was genuinely missing turned out to be two small things and one real bug.**

`claude/chemclaw3-github-repos-8w2wvg` contributed nothing — every file it adds relative to `main`
is already present. It is deleted with no port.

`claude/ci-test-timeout-guard` (PR #27) contributed only `pytest-timeout`. Its job-level
`timeout-minutes` had already reached `main` by another route; the per-test cap had not. Both
matter, and they are not redundant: the job timeout bounds the runner bill, the per-test timeout
*names the test*. The `signal` default is kept rather than `thread` because `thread` calls
`os._exit()` and takes the whole session down, which would reproduce the original failure — a hang
early in alphabetical collection order stopping everything after it from running.

`claude/session-history-endpoints` (PR #25) contributed only its approval-signal fix; its two
routes and `SessionOwnerStore.list_for_owner` had already landed. That fix is the real find.
`service/runner.py` yielded `ApprovalRequestEvent(prompt=...)` and never set `approval_id`, so the
field documented since D-032 as "the durable hold's handle, so a surface can actually answer it via
`POST /approvals/{id}/decision`" was always `""`. `service/static/app.js` does
`if (!evt.approval_id) return;` — so the Yes/No control never rendered, and **every interaction
approval was unanswerable from every surface**. The durable hold, the decision route and the review
queue were all built, tested, and reachable only by a client that already knew an id nothing ever
told it.

The fix is the mechanism the same file already uses three times over: an `ApprovalSignal` on the
per-turn signal buffer, recorded by `start_approval` and mapped in `_signal_event` beside
`JobSignal`, `ProposalSignal` and `QuestionSignal`. A turn signal rather than a return value for
exactly the reason D-077 gives for the other three — the handle must come from the tool that opened
the hold, never from anything the model can author, or the agent could fabricate an approval.
It is announced on the already-started path too, so re-surfacing a candidate stays answerable;
without that, the idempotent branch would hand back an id it never announced.

Plan approvals keep `approval_id == ""`, and that emptiness is now load-bearing rather than
incidental: they have no durable hold and are answered by the next turn, so a handle there would
point a surface at a workflow that does not exist. The field is what distinguishes the two kinds.

**What was deleted, and why each was safe.** The repository carried four generations of itself.

*The Replit-era TypeScript monorepo* — `artifacts/`, `lib/`, `scripts/`, `package.json`, a 210 KB
`pnpm-lock.yaml`, `tsconfig{,.base}.json`, `.replit`, `replit.md`, `issues_replit.md`,
`attached_assets/`, and `.agents/` — 174 tracked files, every one last touched by the single Replit
commit, and referenced by **nothing** in the Python service (grepped across `*.py`, `*.md`, `*.yml`,
`*.yaml`, `Makefile`, `*.sh`, `*.toml`). The real client lives in its own repository. Removing it
removes the only reason this repo needed a Node toolchain, and removes `replit.md` — a second
status document that contradicted the first on two load-bearing points (a pip-managed venv against
`uv sync` everywhere, and "Anthropic via Replit AI Integration, no API key required" against the
config-selected provider seam). `.agents/memory/` went with it: one of its two files recorded "no
pull requests" as a standing rule, which is the opposite of what `CLAUDE.md` says.

*A bare git repository committed into the source tree* — `services/chemclaw-notes-remote.git/`, 319
files. It is the throwaway push target for the PR-gate during local testing; it has no business in
version control.

*Three submodule gitlinks with no `.gitmodules`* — `chemclaw-mock`, `chemclaw-notes-repo`,
`chemclaw-ui` were all mode-`160000` entries in a repository that has no `.gitmodules` file at all,
so every fresh clone, CI included, got three directories `git submodule update` could not resolve.
A dangling gitlink is not a third state between "vendored" and "separate repo"; it is a defect.

*`mcp_servers/calc/server.py`* — 297 lines defining a **second live copy** of seven tools the `calc`
bundle also serves, with byte-identical `predict_pka` bodies. Three documents already stated it was
deleted (`mcp_servers/README.md`, this log at D-113, `tasks/todo.md`), and it was still built into
the image and dispatchable as `CHEMCLAW_COMPONENT=mcp-calc`. This is exactly the merge class D-116
describes — a file deleted on one side and edited on the other comes back — and it is the one
instance that pass did not catch.

*The xTB cost-model island* — `calc/xtb_cost.py` had **zero importers**; its only mentions anywhere
were past-tense prose describing the design D-114 replaced. It went with its test module and its
five orphaned settings (each of which also had an `.env.example` line). The `.env.example`↔config
parity tests passed throughout, because they only check that the two sides mirror *each other* —
neither can see that both sides are dead.

*Two more zero-importer modules* — `agents/job_events.py`, whose replacement's docstring already
said the consolidation had happened; and `scripts/validate_ord.py`, a self-declared shim
(`make eln-validate` runs `python -m eln.validate`).

**One thing the deletion pass found rather than removed.** `xtb_scan_max_points` looked like part of
the dead cost island — its only reference was inside the dead test. It is not: it is a *cap on an
agent-triggerable operation* that has described itself since it was added as bounding a scan "the
way `xtb_hessian_max_atoms` bounds a Hessian", and unlike that one it was **never enforced
anywhere**. `ScanSpec.values` carried `min_length=1` and no maximum, and every point is a full
constrained geometry optimization, so the length of a list the model supplies *is* the cost of the
call. Deleting it would have quietly removed an intended safety property; it is now a validator on
the spec, where every caller — tool, durable job, cache key — is built from it.

**The CI that everyone believed ran.** GitHub Actions reads workflows only from the repository root,
so `services/chemclaw/.github/workflows/{ci,deploy}.yml` had never executed once. What that cost:
`make cov` and its 80% floor, `make eval`, `make eln-validate`, `make helm-validate`, and the image
build with its non-root entrypoint smoke test. Three live documents asserted otherwise —
`pyproject.toml` ("CI runs `make cov` as a gate") and two `[x]` entries in `BACKLOG.md`. Every gate
now runs from the root; the stranded copies are deleted rather than left as "the service's own
record", because a record that contradicts the executing configuration is worse than no record.

The `rollout` job did not come with them. Its entire body was `echo "docker push + helm upgrade
..."`. Writing the real one now would mean asserting a registry, a namespace and a credential shape
that do not exist yet, so it is recorded in `DEFERRED.md` with the trigger that would make it
writable — a real cluster.

**The `mcp-calc` case taught a second lesson.** `tests/test_deploy_chart.py` checked that every
component the chart declares has an entrypoint case — the crash-loop direction. It could not see the
reverse: an entrypoint case for a component nothing deploys. That is precisely how a "deleted"
module stayed routable in a production image. Both directions are now asserted.

**Three lists of "the first-party packages", all wrong in different ways.** `make type` omitted
`service` and `sources`; the wheel `packages` list and `[tool.coverage.run] source` both omitted
`connectors` — 37 modules, the entire capability surface — and `templates`. `pyproject.toml` states
the invariant it was violating ("a non-editable `pip install` of the wheel must ship all of them or
the `chemclaw` command and its imports break"), and nothing checked it. The `make type` gap is the
*same* bug the repo had already found and hand-fixed once for `connectors`/`templates`; the hand fix
did not stop it recurring for two other packages, because the mechanism was a comment saying "keep
this list in sync". `tests/test_packaging.py` now derives all three from the filesystem. Type
checking went from 353 to 366 files, and `service/` — never directly checked before — had four real
errors, one of them a `type: ignore` silencing an `Any` leak out of `app.state`.

**The prose contract was checking the wrong file.** `scripts/validate_prose_contract.py` matched a
backtick immediately followed by `(`. `_INSTRUCTIONS` — the most important agent-facing prose in the
codebase, and the first thing a tool rename breaks — names every tool **bare**
(`gather_evidence sweeps all internal sources`). The pattern therefore matched **zero times** there,
and only `SKILL.md` files were ever really validated. Its stand-in, a hardcoded eleven-name set in
`tests/test_agent.py`, could only catch drift in names someone had already thought to list, while
the instructions named at least ten more that nothing covered.

The fix is a second pattern for bare `snake_case`. An underscore is what makes that safe against
English prose, which does not contain any: measured over the entire corpus it produced exactly one
false positive, an argument name inside a call, now excluded by the pattern. Both the validator and
the test extract from one function, so they cannot disagree about what the prose says. This is
sequenced deliberately **before** the connector-seam work that renames tools — a contract fixed
after the rename it was supposed to catch is not a contract.

Fixing it also exposed a fourth name space problem: `validate_skills`, `validate_templates` and
`validate_prose_contract` each unioned in-process tools with connector tools, while only
`agents/chemclaw_agent.py` also unioned the generated template launchers. A skill naming
`run_hazard_briefing` failed validation although the tool exists. All four now call one
`available_tool_names()`.

## D-118 — One connector seam for MCP, Temporal and long-running HPC tools

The seam D-110 built covers plain MCP tools and connector-owned durable jobs declaratively. It does
not cover the third kind — a long-running HPC job — which is still four hand-written core edits per
capability, and it carries three smaller duplications beside it. This ADR closes them. It opens with
the defect that shaped the rest, because that defect was invisible and is the reason the seam needs
a *mechanical* boundary rather than a documented one.

**The chat service was loading the quantum-chemistry closure the `calc` bundle exists to exclude.**

`connector.yaml`'s `params_model` names a pydantic model as `module:Class`, and
`connectors/jobs.py` resolves that name by **importing** it — inside `build_job_tool`, which
`agents/chemclaw_agent.py` calls on *every* `build_agent`, and again in `make connector-validate`.
The `calc` bundle pointed its five jobs at `workflows/models.py`, which imported `calc.complexes`,
`calc.conformers`, `calc.reaction` and `calc.xtb_scan` for the *result* types that happened to live
in the same file as the *request* types.

Measured from a clean interpreter, building the enabled job tools loaded:

```
heavy third-party: ['tblite', 'tblite._libtblite', 'tblite.exceptions',
                    'tblite.interface', 'tblite.library']
calc.* modules:    15  (anc, complexes, conformers, crest_cli, progress, reaction, store,
                        structure, xtb_cli, xtb_engine, xtb_opt, xtb_scan, xtb_spec, xtb_thermo)
```

`tblite` is a compiled quantum-chemistry library. It was resident in the chat pod, which never calls
it. Nothing failed and no test noticed, because the coupling arrives through a *string in YAML*
rather than through an import statement anyone would read. This is exactly the closure
`connectors/calc/workflows.py` says D-114 removed — *"which is what kept the whole heavy chemistry
closure inside the chat service's image"* — quietly restored by the one field that resolves an
import.

**The fix is a split on what a module may import, not on what it is about.** Requests move to
`connectors/calc/specs.py`, a leaf importing pydantic and config only; results move to
`connectors/calc/results.py`, which may import the heavy `calc.*` types because only this bundle's
own worker ever imports *it*. After the move the same measurement reports `heavy: NONE`,
`calc.*: NONE`.

`tests/test_connector_isolation.py` asserts it in a **subprocess**, which is not incidental: by the
time any test runs, the session's `sys.modules` already holds everything every other test imported,
so an in-process check would pass no matter what the manifest said. Counterfactually verified — a
single heavy import added back to the leaf module fails it with the exact five `tblite` entries.

**`JobStatus.xtb_result` went with it, and was already dead.** The field, and the
`kind: Literal["qm", "xtb"]` that chose between it and `qm_result`, date from when one status tool
answered for both engines. D-114 moved the xTB job into the `calc` bundle, where
`get_durable_job_status` reports it through the connector envelope, so `agents/job_status.py` has
hardcoded `kind="qm"` and populated only `qm_result` ever since. Nothing wrote or read
`xtb_result` — grep confirms a single occurrence in the whole tree, its own declaration. Removed
rather than kept as a field whose `None` means "unreachable here"; it was also the last thing
pulling the `calc.*` result closure into core.

**A bundle's isolation comes from the import boundary, not from withholding a decorator.**

`connectors/bo/activities.py` carried this rationale for leaving its activities undecorated:
*"registering them there would put `bofire` and `botorch` back into core's background worker, which
is exactly the coupling the bundle removed."* The conclusion was right and the mechanism was
backwards. `workflows/registry.py`'s dicts are populated at **import** time, and core's workers
import only `workflows.*` — so a decorator on a module core never imports cannot move anything into
core. What kept `bofire` out was the missing import, not the missing decorator.

The cost of getting the mechanism wrong was real: each bundle had to hand-maintain `TASK_QUEUE`,
`_WORKFLOWS` and `_ACTIVITIES` in its worker module, which re-created one level down the exact
failure the registry exists to prevent — *a workflow that is written, tested and imported but
missing from the worker's list never runs, and nothing fails until someone submits one and it waits
in the queue forever*. And the queue name had three copies that all had to agree (the manifest, the
worker constant, the Helm component); two that disagree is a job in a queue nobody polls.

So: `Queue` widens from a two-member `Literal` to a string, because a bundle must be able to name
its queue without editing core. `connectors/queues.py::bundle_queue` derives it from the bundle
name, so the three copies become one derivation. Bundles decorate normally, and each worker module
is now five lines — two registration imports and a call — with `connectors/worker.py` holding the
shared body. That extraction is sanctioned by the file it replaces: `bo/worker.py` said *"the second
connector worker is when to look at it again"*, and `calc` is the second.

D-006's queue split therefore moves down one level: from core's two queues to one core queue plus
one per bundle, each sized for its own work in Helm.

`tests/test_workflow_registry.py` swaps an assertion about a decorator's absence for the property
that was actually doing the work — in a fresh interpreter, importing core's workers must load no
bundle package and none of `tblite`/`bofire`/`botorch`. The bundle list is derived from
`connectors.registry.discovered()`, so adding a bundle extends the check on the day it is created.

Verified live against the dev server — both workers connect and serve exactly what the registry
holds, with nothing hand-listed:

```
bo   connector worker connected: queue=connector-bo   workflows=[BoCampaignWorkflow]
     activities=[evaluate_candidates, propose_initial, propose_next]
calc connector worker connected: queue=connector-calc workflows=[CalcJobWorkflow]
     activities=[run_xtb_calculation]
```

**The HPC job is a declared connector job, and core's `hpc-jobs` queue is gone with it.**

That was the third kind this ADR opened with, and it cost four hand-written core edits per
capability: a launcher tool (`agents/qm_tools.py`), a status tool that knew the job's own result
shape and id prefix (`agents/job_status.py`), a queue (`hpc_task_queue`), and a worker
(`workers/hpc_worker.py`). `connectors/qm/` replaces all four with a manifest. The move is
mechanical because the earlier commits made it so — `bundle_queue("qm")` derives the queue, a
five-line `worker.py` serves whatever the imports registered, and `specs.py`/the rest is the leaf
split `calc` established.

**The class is not renamed, and that is deliberate.** `@workflow.defn` derives the Temporal type
name from `__name__`, so a *module* move is invisible to a recorded history while a *class* rename
is a different command in it. `docs/workflow-versioning.md` already records the
`QMJobWorkflow` → `CalculationWorkflow` rename as dropped rather than deferred, for exactly this
reason; `QMJobWorkflow` therefore keeps its name in its new home.

**What the workflow stopped doing is the substance of the change.** It published its own graph note
and sent its own session push-back. Both are obligations `ConnectorJobWorkflow` — now its parent —
already owns for every other bundle, so the note is *built* in `connectors/qm/knowledge.py` and
returned on `ConnectorJobResult.note`, and `write_knowledge_node` (the activity that called
`propose_note` directly) is deleted. `connectors/qm/knowledge.py` no longer imports `kg.pr_gate` at
all: a connector reaching around the GxP gate is now structurally impossible rather than merely
against the rules, which is the same correction `connectors/bo/knowledge.py` took in D-111.

**`requested_by` travels on the run's memo.** The HPC cluster is submitted to under a shared service
identity, so the requesting user is the only thing that makes a run attributable (F4-T3), and it
must reach `submit_to_hpc`. It cannot ride on the spec: `params_model` becomes the JSON schema the
model fills in, so a `requested_by` field there would be one an LLM could author. So
`ConnectorJobWorkflow` passes `memo={"requested_by": job.requested_by}` on `execute_child_workflow`
and the bundle reads `workflow.memo_value(...)` — per-execution metadata beside the argument, not
inside it. `QmJobSpec` is the three scientific fields and nothing else; `QMJobInput` subclasses it
with the actor, so the two cannot drift.

**`get_durable_job_status` is now the only way a finished job is collected**, and its
`ValidationError` fallback became a hard error. That branch existed for one job — the QM run, whose
bespoke result the generic tool could not read — and every launcher in the system returns the
envelope now. Reporting `completed` with an empty result for a week-long calculation is worse than
raising.

Two things could not be done as specified, and one gap was found in passing. `JobSpec` has no
`timeout_seconds` field, so the manifest declares none: a connector job's ceiling is the global
`connector_job_timeout_seconds` (24 h), which the field's own comment defends as "a bundle in the
repo must not be able to grant itself unlimited runtime". A DFT run that legitimately needs a week
therefore needs that number raised at the deployment, not in `connector.yaml`. And the harness's
awaiting-todo bridge (`mark_awaiting_job`, D-040) turned out to have exactly one caller — the QM
launcher — so it had never applied to any other durable job; it moved into `connectors/jobs.py`,
where it now covers all of them.

Verified live against the dev server, end to end through the generated tool:

```
qm connector worker connected: queue=connector-qm workflows=[QMJobWorkflow]
   activities=[parse_qm_output, poll_hpc_status, prepare_input, submit_to_hpc]

launched: qm-compute_dft_energy-29776d63ecaa48fb
status:   completed
summary:  B3LYP/def2-SVP on CCO: -94.100000 Hartree (converged)
result:   {... 'requested_by': 'oid-live-check'}
```

## D-119 — Production scale: the event loop, the connection pool, and a guard that switched itself off

**Context.** A 50-concurrent-user load test against the live stack (Postgres 16 + pgvector,
Temporal, the connector fleet, 50 signed identities, `session_store=postgres`) with a stub LLM at a
fixed 400 ms think-time measured three things the code review had only inferred.

Throughput was **flat at ~1.18 turns/s from 10 concurrent users to 50** — five times the load for
1.7% more work and 5× the latency (p50 7.4 s → 37.3 s). That is a serialization point, not a
resource limit: added concurrency became queueing. The box had 4 CPUs and the service used one.

**32 Postgres connect timeouts** occurred while peak concurrent connections was 28 of
`max_connections=100`. The database was idle. The connects timed out because the single event loop
could not schedule them inside `pg_connect_timeout_seconds`. The real load was churn: **401
connections opened for 150 turns**.

And every one of those 32 failures was the same call site — the rollback watermark (D-107), whose
handler is deliberately non-fatal. So the churn did not merely cost latency: it **silently disarmed
a correctness guard**, precisely under the conditions (loaded server, slow turns, impatient users)
that make the failure it guards against likely.

**Decision.**

1. *Blocking work leaves the loop.* RDKit depiction, parsing and descriptors in the `chem`
   connector; `structure_from_smiles(..., optimize=True)` at every async call site; and
   `spec.cache_key(structure)` in all eight `run_cached_*` wrappers — the last of which was
   invisible, being an argument expression evaluated before `run_cached`'s own offload, and which
   shells out to `xtb --version` on its first call in a process. The long `subprocess.run` in
   `xtb_cli`/`crest_cli` was already offloaded; only the version probe was not. `gather_evidence`
   moves from a sequential list comprehension to `asyncio.gather`.

2. *Connections are pooled per process.* `chemclaw/db.py` gains `connection()` and `pooling()`,
   entered once by the front door's lifespan, each worker, and each connector app. Pools are keyed
   by `(dsn, merged libpq options)` so a migration's untimed connection cannot share a pool with a
   request path's bounded one, and `_merged_options` (D-107) is unchanged, so a DSN's own
   `search_path` still survives — the test-schema isolation depends on it. Pool exhaustion raises
   `ConnectionError`, the same retryable infrastructure fault an unreachable database raises.

3. *The disarmed guard becomes loud, and stays non-fatal.* Failing the turn would trade a
   conditional future fault (this session breaks only if the client also disconnects mid-tool-call)
   for a certain immediate one (every turn fails whenever the session store hiccups). A mitigation
   must not take down what it mitigates. What was wrong was the silence, so it is now an ERROR plus
   `chemclaw_rollback_watermark_unavailable_total`.

**The one thing deliberately not done.** `service_uvicorn_workers` exists and defaults to **1**.
`active_turns` — the 409 that stops two turns interleaving on one session's thread — and the
admission semaphore are per-process in-memory guards; with N workers each sees 1/N of the traffic,
so two turns on one session landing on different workers would both be admitted and corrupt the
thread. Moving the guard to a Postgres advisory lock was considered and rejected: the lock is
connection-scoped, so it would pin one pooled connection for a turn's whole duration —
reintroducing exactly the exhaustion this ADR removes. Threads, not processes, are what the change
actually buys, and they touch neither guard. The same hazard already exists across `replicas` and
remains tracked in `BACKLOG.md`.

**Guarantees traded, in full.** A connector state reported by `/readyz` may be up to
`service_readiness_cache_seconds` (5 s) stale. A note changed *outside* this process may be
invisible for up to `graph_cache_ttl_seconds`, raised 5 s → 60 s — which costs nothing real,
because the only out-of-process writer is the knowledge-sync sidecar on a 300 s cadence, so the
shorter window bought scans and no freshness. Both are settable to 0. Nothing else changed
behaviour.

**Also landed.** One Temporal client per process instead of one gRPC channel (and, under mTLS, one
TLS handshake plus three blocking PEM reads) per job launch and status poll. One `httpx.AsyncClient`
per readiness sweep instead of one per connector. `configure_logging`/`configure_telemetry` at the
front door, which had never called either — so `CHEMCLAW_OTEL_ENABLED` was inert at the one process
a chemist talks to. And the correlation id becomes per-turn ambient state
(`agents.identity_context`) rather than a value bound inside `build_agent`: agents are cached per
profile for the pod's life, so every turn from every user had been sharing one id, which made the
GxP audit trail unable to separate two conversations.

## D-120 — A data source becomes a manifest: the second config-side union replaced by a folder

**Context.** The user's direction for this pass named data sources beside tools: *"For anything
which will be exchanged or added future on a regular basis (tools, datasources, etc) I want to have
nicely defined generic connector approaches. Keep in mind that the number of databases, tools and
of course user will increase in future significantly."* D-118 did the tool half. This is the other.

`sources/` already had the right *contract* — `DataSource` with two independent, optional halves
(D-054), which nothing here changes. What it did not have was a way to attach one without editing
core. Adding a source meant an entry in `DATA_SOURCES` (a dict of factories in
`sources/registry.py`); adding a source that carried its own config meant three edits: a pydantic
model in `chemclaw/config.py`, an arm of the `DataSourceSpec` discriminated union, and a branch in
`build_data_source`. That was D-076, and it was a reasonable answer to the question asked at the
time — "how does a source carry per-instance config?" — but it makes the cost of a source scale
with core, which is the wrong direction for the thing the user says will grow most.

**The defect that decided the shape.** A dict of factories cannot say that a source *has* an ingest
half without also naming what builds it, so every adapter was imported at the registry's module
scope. The two consumers want disjoint halves — `gather_evidence` wants retrievers in the chat
process, the durable ELN sync wants adapters in a worker — and each was paying for the other.
Measured from a clean interpreter, `active_ingest_source_names()`, which returns two strings:

```
BEFORE  names=['eln-json']  modules=836  heavy=[drfp, numpy, psycopg, rdkit]
AFTER   names=['eln-json']  modules=292  heavy=NONE
```

The ELN sync worker was loading `report.retrievers` — rdkit, the reaction-fingerprint index, the
Postgres note index — to learn two names. Nothing failed; the cost is image size, process memory
and start-up time, and it grows per source added. This is the same class of defect as D-118's
`params_model` finding, arriving through a different mechanism: there a string in YAML resolved an
import no reader could see, here the registry's *shape* forced one.

**Decision.** A data source is a folder with a `datasource.yaml`, discovered from a search path,
enabled by name — structurally identical to a connector bundle.

- `sources/manifest.py::DataSourceManifest` (`extra="forbid"`): `name`, `description`, optional
  `ingest`/`retrieve` as `module:callable`, and free-form `config` passed as kwargs.
- `sources/registry.py` discovers manifests over `data_sources_dir` (OS-pathsep, earlier wins) and
  resolves a half **only when that half is about to be built**. `discovered()` is cached; built
  halves never are, so per-call config still applies.
- `DATA_SOURCES`, `build_data_source`, `DataSourceSpec`, `JsonElnSourceSpec`, `OrdElnSourceSpec`
  and `data_source_specs` are deleted. No back-compat shim: the user's direction for this pass was
  that breaking changes are acceptable.
- `make datasource-validate` (`scripts/validate_datasources.py`) resolves every declared half and
  binds its `config` against the real signature. This seam was the only registry with no validator
  — defensible when a source was Python that `mypy` checked, not once it is a string in YAML.

**Why `config` is free-form rather than typed.** It is the one thing D-076 gave that this takes
away, so it is worth being explicit. A typed union validates config at config-load; the cost is
that every adapter needs a parallel pydantic model in core, kept in step by hand. Here the
callable's signature *is* the schema — there is nothing to keep in step — and the validator binds
against it in CI, which catches the same typos at the same point in the workflow. The one case it
does not catch is a `config` value of the wrong *type* for a parameter with no annotation; adapters
in this repo are annotated, and `mypy --strict` covers them.

**Consequences.**

- Attaching a source touches zero core Python. `tests/test_datasource_seam.py` is the acceptance
  test and demonstrates it: it attaches a working source by writing one YAML file into a tmp dir.
  It previously had to `monkeypatch.setitem` a dict inside `sources.registry` — a test reaching
  into core to add a source is evidence the seam does not work.
- A second instance of an existing adapter (a staging ELN drop) needs no code at all, and a
  deployment can override a shipped source by mounting a directory earlier on the search path.
- `tests/test_datasource_isolation.py` holds the import property in a subprocess, counterfactually
  verified: restoring one module-level adapter import fails it.
- `chemclaw/config.py` now has **no** pydantic models. `McpServerSpec` went to a connector manifest
  in D-118 and `DataSourceSpec` goes to a source manifest here, and they went the same way for the
  same reason: each described the internals of one attached thing. The rule that leaves behind is
  recorded in that module's docstring — *config says which and where; a manifest says what* — and
  it is the rule that keeps the config file from growing with the deployment.
- Supersedes D-076. `sources/base.py` (D-054) is untouched; the contract was never the problem.

**Not done, deliberately.** `report.retrievers` still loads in the chat process, because the one
active retrieve source genuinely needs it — the win there is structural (a future ingest-only
driver no longer lands in the chat pod), not a number today, and claiming otherwise would be
dishonest. The Snowflake ELN source remains deferred; it is now a manifest and an adapter class,
with nothing owed by core.
## D-121 — The front door as a multi-process service: pure-ASGI headers, a durable turn claim, a pool timeout that sheds

**Context.** D-119 pooled Postgres and got the blocking work off the event loop. A 50-user load run
against that branch showed what those fixes did and did not buy: connection churn gone (401 opened
connections for 150 turns became zero — the pool reused what it had), p50 down 30 % and p95 down
50 %, and **throughput unchanged at ~1.18 turns/s from 10 users to 50**. Five times the load, 1.7 %
more work. The serialization point was neither the database nor the offloaded CPU: it was the single
event loop, and the box had four idle CPUs beside it.

The decisive experiment — `--workers 4` — was recorded as failing outright for all 50 users. It did
not fail; **it never ran.** The 4-worker server's own log opens with `ERROR: [Errno 98] Address
already in use`: the previous single-worker service still held the port. The 50 clients' errors are
`status=0, All connection attempts failed` — nothing was listening — and the 44
`RuntimeError("No response returned.")` tracebacks in that log belong to the *previous* process,
being torn down with streams still open. So the recorded conclusion "multi-process does not work at
all" was not measured. What the log does prove is worse in one way and better in another: the
`BaseHTTPMiddleware` defect is real and fires on **one** worker, on every stream that outlives its
server; and nothing at all is known against multi-process from that run.

Three real blockers stood between the branch and a multi-process front door, and they are what this
ADR records.

**1. `BaseHTTPMiddleware` cannot carry an SSE stream.** `_add_security_headers` was one, which runs
the downstream app as a second task and pipes its ASGI messages through a memory object stream. A
request that ends without ever sending a response — a pod draining mid-stream, a client that gave up
waiting for an admission permit, any cancelled handler — reaches `call_next` as a closed stream and
is re-raised as `RuntimeError("No response returned.")`. That is an HTTP 500 with a traceback where
the honest outcome is a closed connection.

Not hypothetical, and not a multi-worker problem at all: the **single-worker** process logged 44 of
them, every one on the SSE turn route, as it was shut down with streams open. That is what a rolling
deploy does to every in-flight conversation.

*Decision:* pure ASGI middleware that wraps only `send` and stamps the headers onto the
`http.response.start` message. The body is never re-tasked and never buffered, so an SSE stream is
byte-for-byte what the route produced.

**2. The per-session turn guard was per-process.** `active_turns` is a Python set in one process's
memory and the shipped chart runs the front door at `minReplicas: 2` — so a double-submit landing on
the other replica **has always been admitted twice**, and the two turns interleaved their messages
into one conversation thread. That is the exact corruption the 409 exists to prevent, and it was
live before any of this work; raising the worker count would have added the same hazard inside a pod.

*Decision:* a turn also takes a **leased row** in `session_turns`, under the same
`session_store="postgres"` gate as session ownership — that switch is precisely the condition under
which two processes share a conversation's durable history and can corrupt it.

A lease, not a lock. An advisory lock and `SELECT … FOR UPDATE` are both connection- or
transaction-scoped, so holding one for a turn means pinning a pooled connection for minutes,
re-creating the starvation this work exists to remove. Claim, refresh and release are one short
statement each: borrow a connection, give it straight back. The claim is a single
`INSERT … ON CONFLICT DO UPDATE … WHERE expires_at <= now()`, so the check and the take cannot be
interleaved, and a process SIGKILLed mid-turn stops blocking its session after one lease rather than
until a restart.

The in-process set is kept and checked first, so the single-worker guarantee is byte-for-byte what
it was — no I/O, no race window, no lease involved. The lease adds the cross-process half, with the
property every lease has: exclusion holds while the holder is scheduled often enough to refresh,
which the front door does three times per lease. A failed refresh is **counted, not swallowed** —
D-107 already taught this branch that a guard which quietly switches itself off is worse than one
that fails loudly.

**3. A pool timeout surfaced as a 500.** The run's 16 HTTP 500s were all `psycopg_pool.PoolTimeout`
at `create_session` → `SessionOwnerStore.record`, and the pool was **never exhausted**: 13 of a
permitted 64 connections, zero opened during the run. Callers waited >10 s for a connection that was
*available* and could not be handed over, because the loop could not schedule the handoff. Raising
`pg_pool_max_size` 16 → 64 changed nothing, which is the whole story — this is the same starvation
that used to appear as a connect timeout, made user-visible because a bounded pool raises where an
unbounded connect eventually succeeded.

*Decision:* one `ConnectionError` handler on the app, not a try/except per route — `chemclaw.db`
already funnels "no database" and "no free connection in time" into that one exception precisely
because no caller can act on the difference, and every route touching durable session state can hit
it. It answers **503** with the admission path's own wording, so a client's back-off behaviour is
identical and a browser learns nothing about the infrastructure. Counted as
`chemclaw_db_unavailable_total`, separate from the admission shed, so "the loop could not schedule a
handoff" is never read as "the LLM endpoint is full".

**Consequences.**

- `CHEMCLAW_SERVICE_UVICORN_WORKERS` still defaults to **1**, but no longer because of the turn
  guard. What remains per-process is *capability*, not correctness of durable history: attachments,
  harness todos and the live `AgentSession`. No ingress can pin a request below the pod, so replicas
  plus Route affinity stay the supported way to use more CPU, and the Route now states that affinity
  explicitly instead of relying on the haproxy router's default. A chart test holds it.
- `infra/sql/018_session_turns.sql` is the new migration.
- The 44 spurious 500s per run disappear, independently of worker count.
- **Verified live, not inferred.** The real front door on `--workers 4` against the live stack
  (Postgres, Temporal, the connector fleet, the stub LLM) served 8 concurrent streaming turns to
  completion with the security headers on every stream, and 6 out of 6 pairs of concurrent turns on
  *one* session answered `[200, 409]`. The same 6 pairs run with `session_store=memory` — where no
  shared claim exists — answered `[200, 404]` or `[404, 404]` every time, which is how we know the
  two requests really did land on different workers and that the 409 came from the durable claim
  rather than from either worker's own `active_turns`.
- **Not claimed here:** that throughput improves much. A smoke check (24 concurrent turns, not the
  load harness) measured 0.92 turns/s on one worker against 1.33 on four — real, and far short of
  4×. On this box it cannot be more: four CPUs are shared with Postgres, Temporal, the background
  worker and, above all, `scripts.connectors_dev`, which serves all six connector bundles from **one
  uvicorn process on one event loop**. In production each bundle is its own Deployment; in the load
  harness it is a single loop that every tool call from every turn passes through, so a
  `--workers 4` run there may simply relocate the ceiling rather than raise it.
- Renumbered from D-120 during the merge: `claude/datasource-seam` had already published D-120
  (per CLAUDE.md, the branch merging second renumbers).

## D-122 — The GxP audit trail defaults to durable, because opting in per call site did not work

**Context.** `PostgresAuditSink`, the tamper-evident hash chain (`chain_hash`, `row_hash`),
`infra/sql/011`, `make audit-verify` and `scripts/verify_audit_chain.py` were all built, tested and
documented as the GxP "who ran what" record. The sink was constructed in exactly **one** place:
`agents/cli.py`, behind `--audit-postgres`. The deployed service's `_default_agent_factory` called
`build_agent(profile=…)` with no `audit_sink`, so `agents/audit.py` installed `NullAuditSink()` and
the compliance record was log-only in the one process chemists actually talk to. The Temporal
template activities had the same gap, independently, in two more call sites.

Nothing failed and no test noticed: `tests/test_audit.py` drives the middleware directly and
`tests/test_audit_store.py` writes to the sink directly, so both pass while the wiring between them
is absent. `audit_events` was simply empty.

**Decision.** The default moves from the call site to the one place that decides.
`agents.audit.default_audit_sink()` returns `PostgresAuditSink` where `session_store="postgres"`
and `NullAuditSink` otherwise, and `make_audit_middleware(sink=None)` resolves it.

The polarity is the whole point. Opting *in* to a compliance control, once per entry point, means a
forgotten keyword argument silently downgrades it — and there is no failure to notice, because the
downgraded state is "the log still has it". So the durable sink is what a caller gets by default,
log-only is the fallback where no database is configured, and opting *out* requires passing
`NullAuditSink()` explicitly, which is a visible act.

Fixing `service/app.py` alone was the obvious change and was rejected: it would have left the
identical trap set for the template activities and for every entry point added later. The gate is
`session_store="postgres"` for the same reason `_default_owner_store` uses it — that switch is the
deployment's statement that a Postgres exists — with a lazy import so the dev/test path never pulls
psycopg for a store it will not use.

`agents/cli.py --audit-postgres` survives with narrowed meaning: it *forces* the durable sink for an
operator running a terminal session against a database without switching `session_store`.

**Consequences.**

- Three call sites stop being able to get this wrong, and so does the next one.
- Verified counterfactually at the decision line rather than by deleting the function: reverting
  only `sink if sink is not None else default_audit_sink()` back to `NullAuditSink()` fails
  `test_an_omitted_sink_no_longer_silently_means_log_only` and nothing else.

**Verified end to end.** `audit_events: 4 -> 5` on a live turn against the stub model, with
`default_audit_sink()` resolving to `PostgresAuditSink` under the load-test config.

That verification took two attempts, and the first one was wrong in a way worth recording. It
reported that the middleware never fires and warned that `enforce_tool_authz` — the RBAC gate,
registered the same way — might be inert too. The cause was the *test harness*: the stub model sent
`{"query": "benzene"}` while `find_notes` takes `text`, so every call failed argument validation
inside `agent_framework._tools._auto_invoke_function` and returned at the parse-error branch, which
sits before the middleware branch. No tool body ran, so nothing was audited. With the stub
corrected: `PIPELINE.EXECUTE fired n=4`, `exc=None`, and the row lands. RBAC was never affected.

What survives is smaller and is tracked separately in `BACKLOG.md`: a call rejected for bad
arguments is not audited at all (**AUDIT-2**), so the trail cannot answer "what did the agent
attempt and get wrong" — and the load runs' "100 tool calls" were all parse failures, so their
tool-path claim is being re-measured (**LOAD-1**).

## D-123 — One agent per concurrent turn: a shared chat client corrupts streamed tool calls

**Context.** The live 50-user run (Haiku, 4 workers, 50 signed identities) admitted every turn —
150/150, no shed, no conflict, no transport error — and then lost **30 of them (20 %)** to an
Anthropic 400:

```
messages.1.content.3.tool_use.name: String should have at least 1 character
```

**The cause, isolated by elimination.** Eight live attempts per configuration:

| Variant | Setup | Result |
|---|---|---|
| A | bare `agent_framework`, 3 tools, sequential | 0/8 fail |
| B | + the 6 MCP connectors, sequential | 0/8 fail |
| C | full `build_agent()` + connectors, sequential | 0/8 fail |
| **D** | full `build_agent()`, 8 turns **concurrent, one shared agent** | **8/8 fail** |
| **E** | identical, but **one agent per turn** | 0/8 fail |
| **F** | **per-turn agents, one shared *client*** | **8/8 fail** |

E and F differ only in whether the *client* is shared, which is what names the client rather than
the agent. `agent_framework_anthropic/_chat_client.py` keeps the tool call it is currently parsing
on the instance:

```python
case "tool_use":
    self._last_call_id_name = (content_block.id, content_block.name)
...
case "input_json_delta":
    call_id = self._last_call_id_name[0] if self._last_call_id_name else ""
    contents.append(Content.from_function_call(call_id=call_id, name="", ...))
```

An argument delta carries `name=""` **by design** and recovers its identity from that attribute. Two
turns streaming through one client interleave: B's `tool_use` overwrites the attribute between A's
`tool_use` and A's deltas, A's arguments are filed under B's call id, and A's assistant message goes
out carrying a `tool_use` block with an empty name. It needs two or more tool calls in one message
to show, which is why every failure named `content.2` or `content.3`.

**Decision.** `agents/agent_pool.py::AgentPool` leases one agent — and with it one chat client — to
one turn at a time, sized to `service_max_concurrent_turns`. The front door leases around the
streamed run; everything that does not stream (session creation, `/readyz`) keeps the cached
per-profile agent, because only a stream can interleave.

A pool rather than per-turn construction: building is cheap enough (~90 ms agent, ~95 ms client) but
a fresh client is a fresh `AsyncAnthropic`, hence a fresh connection pool and TLS handshake on every
turn — reintroducing exactly the per-call handshake churn D-119 removed from Postgres. A lease keeps
connections warm across turns while guaranteeing no two *concurrent* turns share one.

Sized to the admission cap so the pool is never the queue: the semaphore already bounds concurrency
at the same number, so a lease does not block in normal operation.

**Result, measured on the same live run:**

| | before | after |
|---|---|---|
| answers / errors | 120 / **30** | **150 / 0** |
| empty `tool_use` names in the log | 30 | **0** |
| p50 | 19.8 s | 16.9 s |
| throughput | 1.76/s | 1.99/s |
| tool calls | 151 | 208 |

Latency and throughput improved as well, which follows: a turn that died at its first tool call was
finishing early, and 208 tool calls against 151 is the count of tools that now run to completion.

**Why no test caught it.** Every stub run reported a clean 150/150 because the stub emits exactly
one tool call per response, and a single `tool_use` block has nothing to interleave with. Only a
real model making *parallel* calls under *concurrency* reaches it — the intersection of two
conditions, neither of which a unit test has.

`tests/test_agent_pool.py` asserts the property that makes the corruption impossible — no agent held
by two turns at once — rather than the corruption itself, which is upstream code.

**This is a workaround, written to be deleted.** The real fix is for the parser to hold that state
per stream. `DEFERRED.md` records the trigger: when it does, the pool collapses back to one shared
agent per profile and `agents/agent_pool.py` goes away.

## D-124 — A calculation's by-products outlive the directory it ran in

`calc/xtb_cli.py` runs xtb inside a `tempfile.TemporaryDirectory`. It writes `input.xyz`, the
binary writes `hessian`, `vibspectrum`, `xtbopt.xyz`, `xtbout.json`, `_collect` parses them into a
`CliResult`, and then the `with` block ends and every file is deleted. The system kept one JSON
summary per calculation and threw away everything else it had paid for.

That is affordable for a single point. It is not affordable for a Hessian. D-092 measured one at 26 s
on 76 atoms through the binary and 218 s through finite differences, and `ThermoSpec` puts
`temperature_k` in the cache key — so asking the *same* molecule for thermochemistry at 350 K after
298 K is a cache miss that recomputes the Hessian, a quantity that does not depend on temperature at
all. The expensive half was being recomputed to answer a question about the cheap half.

### Two tables, because a blob and its role are different facts

`artifact_blobs` is keyed by the SHA-256 of the artifact's **uncompressed** bytes.
`calculation_artifacts` maps `(calc_key, name)` to that hash. Content addressing gives dedup for
free — two runs that converge to the same geometry write one copy of it — and the link row is what
makes a blob reachable *from* a calculation rather than only by its hash. The split is DataJoint's
hash-addressed model, and it is what keeps the design open: a DFT wavefunction or SCF restart file
is another `(calc_key, name)` row over the same blob table, not a new mechanism.

The address is over the uncompressed bytes deliberately, so it does not change when the compression
level does. `ON DELETE CASCADE` from blob to link is load-bearing: evicting a blob removes the rows
that point at it, so `list_for` can never hand back a ref whose bytes are gone.

### Postgres `BYTEA`, and why not the three alternatives

The artifacts this system actually produces are kilobytes to a few megabytes — a Turbomole `hessian`
is repetitive numeric text that deflates several-fold. That is squarely `BYTEA` territory, and
Postgres is the only durable store the deployment already has.

*Not an object store.* It adds an infrastructure dependency, a fourth secret to the three-secret
model, a client library, and a bucket-endpoint host literal that muddies `tests/test_no_egress.py`
for no gain at this size. *Not a shared filesystem CAS.* The service and the workers are separate
pods, so it needs an RWX volume no OpenShift storage class guarantees, plus its own GC and backup
story. *Not `hpc_artifact_store_url`* — that is a **read** endpoint the Nextflow launcher fetches
finished-run blobs from, not a store this system writes to.

The `ArtifactStore` Protocol is the seam. When DFT lands and a wavefunction is 200 MB rather than
2 MB, a third backend is one class and no caller changes.

zlib rather than zstd: Python 3.11 has no stdlib zstd, and a dependency for the remaining ~15% on
text is not a trade this codebase makes. The codec is recorded per row, so changing it later is a new
value, never a migration. Compression that does not shrink a payload is not applied — an
already-compressed artifact would otherwise be stored *larger* than it arrived.

### An artifact is optional by construction

`put` returns `None` — it does not raise — when the store is disabled or the payload exceeds
`artifact_max_bytes`, and the capture path `stat`s every file before reading it so an outsized one
never enters memory. When the *store itself* fails, `run_cached_with_artifacts` logs a warning and
returns the result anyway.

This is the whole contract, and it is deliberate in both directions. Losing an artifact costs a
future recomputation. Propagating the failure would discard a calculation that had already succeeded
and was already in the result store — trading a cheap loss for an expensive one. Capturing a
by-product must never be able to fail the thing it is a by-product of.

Capture happens *after* `_collect` succeeds, so a parse failure raises exactly as it did before this
existed. The cost is that the raw files are then unavailable for a post-mortem on a parse failure.
Plumbing bytes onto an exception to fix that is not worth it; the trade is recorded rather than
hidden.

### The capture manifest is derived, not restated

`_REQUIRED_OUTPUTS` already declares what each task must leave behind for its run to have succeeded.
`_CAPTURED` is that same map minus `sp`, so the two cannot drift — adding a task declares both facts
once.

`sp` is the one exclusion, and the reason is exact: its `xtbout.json` is parsed *in full* into
`CliResult.properties`, which lands in the cached JSON result. Storing the file too would be a second
copy of the cache with none of the value.

### The cost policy `retention.py` asked for, and the eviction it unblocks

`workflows/retention.py` refuses to age-prune `calculation_results` and says why: a cache is bounded
by cost policy, not by a retention clock, and evicting a cached result silently converts a hit into a
recomputation. It then names the policy it would need — "LRU by access, or by compute cost" — and
declines to invent it.

`cached_compute` now times every miss and stores `compute_seconds`. That is the missing number, and
it resolves the tension rather than reopening it: **eviction targets blobs, never results.** The JSON
result is the *answer*, and evicting it would void D-011. A blob is a *by-product* from which the
answer can be regenerated, so evicting one costs recompute time on a future reuse and nothing else.
`retention.py`'s refusal therefore stays literally true.

There is deliberately no `last_access_at` on `calculation_results`. Nothing evicts it, so nothing
would keep the column current, and an access stamp on the cache-hit path is a write on the hottest
read in the system. On `artifact_blobs`, where eviction does need it, the stamp is refreshed lazily —
only once the recorded value is already older than `artifact_access_stamp_seconds` — so a read on the
reuse path stays a read.

### What this does not yet do

The store is wired into the thermochemistry path, which is the one that pays for it. The optimizer
and the conformer ensemble capture their files but do not yet persist them, and the eviction sweep
is designed here but not built. The reuse that makes the stored Hessian *worth* storing — thermo at a
second temperature without recomputing — is D-125's, and is the reason this landed first.

The end-to-end assertion that a captured Hessian reparses to the same matrix is written and
`@needs_xtb`-gated; it does not run where the binary is absent, which includes this session's
environment. It is a real test of a real property, and it is unverified here.

## D-130 — Turn teardown runs in a cancelled task, so its cleanup has to be shielded to happen at all

> **On the number.** This was written as D-124 and renumbered on merging second, per `CLAUDE.md`.
> The gap to D-130 is deliberate rather than an accident of the merge. The procedure says "highest
> allocated + 1", which is D-125 — but D-125…D-129 are *intended* by the in-flight storage and
> knowledge-substrate sequence, which reserved them, was forced by `tests/test_decision_log.py` to
> un-reserve them (the registry may not name an ADR that does not exist yet), and still forward-
> references D-125 from the merged body of D-124. Taking D-125 would have followed the letter of a
> rule whose entire purpose is to avoid collisions while causing one, and would have made an
> already-merged ADR's citation wrong. Gaps are explicitly harmless (`CLAUDE.md` rule 4); a
> renumbered neighbour is not.
>
> That contradiction — reserve early, but the test forbids reserving what you have not written — is
> a real defect in the convention, already flagged by the session that hit it (`8f6a319`). It is not
> mine to resolve unilaterally: whoever owns `CLAUDE.md` should decide whether the ledger gains a
> "reserved" state or the advice changes. Recorded here so the next session that trips on it finds
> two witnesses rather than one.

**Context.** Stage 5e's chaos pass found CHAOS-1: abandon an SSE turn mid-stream and the same
session refuses its owner's next turn — `a turn is already running for this session` — for **63
seconds measured**. A chemist who closed a tab could not reopen the conversation for a minute.

The finding stayed open across two sessions because two explanations were tested and **both were
wrong**. Detaching the durable claim release onto its own task changed the measured time not at all
(63.5 s vs 65.1 s). The theory that the abandoned turn simply ran on to completion was refuted by
its actor producing zero `audit_events` rows. The written next step — instrument the teardown — is
what finally settled it, and the instrument mattered more than the reasoning did.

**What it actually was.** Two guards can hold that 409, and no previous measurement separated them.
Sampling both once per second while polling settles it in one run:

```
t+ 0.0s  POST=409  in_flight=0.0  claim=81d518@+59.9s
t+30.9s  POST=409  in_flight=0.0  claim=81d518@+28.9s
t+59.9s  POST=409  in_flight=0.0  claim=81d518@+0.0s
t+60.9s  POST=200  in_flight=0.0  claim=81d518@+60.0s
```

`in_flight` is 0 from the first sample: the in-process `active_turns` set was freed *immediately*,
so the generator's `finally` did run promptly — the third disproved theory. The durable claim's
`expires_at` counts monotonically down from 60 s and is never refreshed, so the heartbeat was
cancelled too. The recovery time is exactly `service_turn_claim_lease_seconds`. **The release never
landed**, and the lease — designed as the backstop — was carrying the whole path.

Tracing the claim store proves the mechanism rather than inferring it:

```
CLAIMTRACE t+ 0.75s claim(71743695) -> True
STREAMTRACE agent stream got CancelledError
CLAIMTRACE t+ 0.75s release(71743695) ENTERED
CLAIMTRACE t+ 0.76s claim(71743695) -> False        <- and no COMPLETED, ever
```

The release is *entered* on every abandoned turn and *completes* on none. sse-starlette answers
`http.disconnect` by cancelling its task group; a bare `await` inside a cancelled task raises at its
first suspension point, so `_release_turn_claim` reached the database call and died there. The
earlier "detach it onto a task" experiment was the right idea measured on the wrong branch.

**Decision.** Shield the release, and give the shielded coroutine its own error handling:

```python
async def _release() -> None:
    try:
        await claims.release(session_id, _WORKER_ID)
    except (ConnectionError, OSError, RuntimeError):
        logger.warning("could not release the turn claim for session %s; it expires on its own", ...)

await asyncio.shield(_release())
```

`shield` runs the release as an independent task that outlives the cancelled frame. The error
handling belongs *inside* that task rather than around the `await`: once the awaiting task is
cancelled, `shield` drops its bookkeeping callback on the inner task, so a failure raised afterwards
is never retrieved and asyncio reports it as a bare `Task exception was never retrieved` with
nothing tying it to a session. A task that cannot fail cannot produce one. The same restructuring is
applied to the runner's pre-existing `rollback_to` shield, which had the identical hazard.

The lease stays as the backstop for what shielding cannot cover — the process being killed, the loop
closing under it. It is now what it was always meant to be: the exceptional path, not the only one.

**The second defect, found by the same instrument.** The trace line `agent stream got
CancelledError` is the answer to a question nobody had asked: which exception does a real disconnect
deliver? `service/runner.py` rolled a half-written turn back under `except GeneratorExit:` — the
exception `aclose()` raises. sse-starlette **never calls `aclose()` on the body iterator** on the
disconnect path; it cancels. So the rollback that exists to stop one dropped connection from
poisoning a conversation with an orphaned `tool_use` was **dead code on the only path that reaches
it**, and had been since it was written. The clause now catches `(GeneratorExit,
asyncio.CancelledError)`, which also brings the front door's whole-turn deadline under the same
rollback — a timed-out turn is half-written in exactly the same way.

This was a silent weakness rather than an outage only because `agents.session_store` repairs
unmatched tool calls at read time. That backstop strips the orphan; only the rollback discards the
rest of the abandoned turn.

**Why no test caught either.** `tests/test_turn_cancellation.py` had three tests about abandoned
turns, every one of them tearing the stream down with `await stream.aclose()` under the comment
*"what sse-starlette does when the client disconnects"*. It is not what sse-starlette does. The
suite simulated the one teardown production never takes, and reported green while the real path was
unhandled — the same shape as LIVE-1, where `ScriptedChatClient` derived from the base class
*without* middleware and so tested a pipeline production never ran.

Writing the regression test reproduced the trap once more, and that is worth recording. The first
version cancelled the consuming task while it sat in its own frame rather than inside the turn; the
abandoned generator was then finalised by `asyncio.run`'s async-generator shutdown, which raises
`GeneratorExit` — so the test passed against the unfixed code. It now waits for the agent to signal
that it has stalled, guaranteeing the cancel lands inside the turn, and asserts *before* the loop
closes.

**Result, measured on the real stack** (live Anthropic, Postgres sessions, disconnect after a
`tool_call` event was seen on the wire):

| | before | after |
|---|---|---|
| single replica: session freed after | **60.9 s** | **0.0 s** |
| two replicas, next turn on the other process | **HTTP 409** | HTTP 200, answered in 4.8 s |
| unmatched `tool_use` ids left in durable history | — | none |

The two-replica row is the one that matters for the shipped chart: a process that never served the
abandoned turn has only the `session_turns` row to go on, so the durable claim is the entire guard
there and its release is the entire fix.

**Cost.** Cleanup now outlives the request that scheduled it, by one task and typically ~140 ms.
That is the price of cleanup that runs at all, and it is bounded: the task does one DELETE and
cannot fail outward.

## D-131 — The connector health probe follows the address override, instead of probing the pod itself

**Context.** Re-running Stage 5e's connector-kill scenario after D-130 produced a result that could
not be read: `/readyz` reported every connector `unreachable` **both before and after** the fleet
was SIGKILLed mid-turn. The scenario exists to prove the unreachable signal is loud (the failure
D-118 called out, where an agent silently runs with only its in-process tools), and it could not
distinguish a killed connector from one that was never probed correctly.

It was the latter, and not only in dev. `connectors/registry.py` applied the deployment's
`connector_urls` override to the connector's *tool* endpoint and nowhere else, while
`connectors/health.py` read `manifest.endpoint.health_url` straight off the file. A bundle's
manifest ships a loopback dev default, so the two disagreed the moment the override was set — and
**the shipped chart always sets it**: `chemclaw.connectorUrls` computes one in-cluster Service URL
per enabled bundle precisely so the front door does not have to be patched per environment.

The consequence in a cluster is that the front door probed `http://127.0.0.1:881x/healthz` — its own
pod, where nothing listens. Every connector read `unreachable` however healthy it was, so `/readyz`
and the `chemclaw_connectors_unhealthy` gauge were decorative; and under `connectors_required: true`
— the GxP fail-fast posture, the one a regulated deployment would pick — the probe raises at
startup, so the front door would have failed to start every time, with a message blaming connectors
that were fine.

**Decision.** One public `connectors.registry.health_url(manifest)`, and the probe goes through it.
The probe is a second caller of the override, so the override is what it must ask.

The move is a **suffix replacement, not an origin swap**, because the two deployments that exist put
a connector in different *places* rather than merely on different hosts:

| | endpoint | health |
|---|---|---|
| Helm (per-bundle Service) | `http://…-connector-chem:8814/mcp` | `…:8814/healthz` |
| `scripts.connectors_dev` (one port, mounted by name) | `http://127.0.0.1:8810/chem/mcp` | `…/chem/healthz` |

Keeping the health path verbatim is right for the first and wrong for the second — `…:8810/healthz`
is a 404 there, which is exactly why the dev topology never revealed the bug. So the manifest's own
two URLs define the relationship (whatever distinguishes its health URL from its endpoint URL), and
that difference is re-applied at the effective address. An override that does not end the way the
manifest's endpoint does falls back to the declared URL: possibly wrong, but not silently invented.

**Result, measured on the running stack** with the dev composite serving all six bundles:

| | before | after |
|---|---|---|
| `/readyz` with every connector healthy | `bo=unreachable, calc=unreachable, chem=unreachable, molfp=unreachable, rxnfp=unreachable, safety=unreachable` | `bo=healthy, calc=healthy, chem=healthy, molfp=healthy, rxnfp=healthy, safety=healthy` |
| `/readyz` after the fleet is killed mid-turn | (unchanged — indistinguishable) | all six flip to `unreachable` |

With the signal working, the scenario finally reports something: the turn whose connector died at
2.7 s still **answered**, retrying tools three times against a dead server and finishing in 39 s, and
the next turn completed on the reduced surface. Losing a connector costs capability, not the
conversation — which is what `connectors_required: false` promises and what had never actually been
observed end to end.

**Why no test caught it.** Every registry test asserted the override on the tool URL
(`test_connector_urls_override_the_manifest_address`) and no test asserted anything about the probe
URL at all, so the two halves of one address were covered asymmetrically. `tests/test_deploy_chart.py`
checks that the chart *computes* the URLs; nothing checked that everything reading an address goes
through the same function. Three tests now pin it, including the path-moving case that the naive
origin swap would fail.

---

## D-132 — The Hessian is its own calculation: splitting the matrix from the thermochemistry computed over it

**Context.** D-124 kept the by-products a calculation used to destroy. Keeping them was worth
nothing until something read one back, and the first thing worth reading back was the Hessian.

The defect the storage audit found is narrow and expensive. `ThermoSpec` carried
`temperature_k`, `pressure_pa`, `symmetry_number`, `rrho_cutoff_cm` *and* `displacement_angstrom`
in one model, and `XtbSpec.cache_key` keys on every field via `model_dump()`. So asking for
thermochemistry at 350 K after 298 K was a cache miss that recomputed the second derivatives —
a quantity that does not depend on temperature at all. Measured in D-092, that matrix costs 26 s
on 76 atoms through the binary and 218 s through finite differences. **The one question a stored
Hessian answers trivially was the exact question that forced a full recomputation.**

**Decision.** A Hessian becomes a cached calculation in its own right (`calc/xtb_hessian.py`),
keyed by `HessianSpec` — the geometry, the method, the displacement, and nothing else.
`ThermoSpec` is unchanged and keeps every field it had; it gains `hessian_spec()`, the projection
onto what a Hessian actually depends on. Two `ThermoSpec`s differing only in a state variable
project onto the *same* `HessianSpec`.

So a second temperature is a miss on the thermochemistry — correct, the free energy really does
differ — and a hit on the Hessian. Minutes of second derivatives become milliseconds of partition
functions.

**The matrix lives in the artifact store, not in the result row.** A 76-atom Hessian is 228x228
float64: 416 kB, which has no business in JSONB. The row (`HessianResult`) holds content
addresses; the arrays are `.npy` blobs beside the Turbomole `hessian` and `vibspectrum` files the
binary wrote. This is what makes D-124 load-bearing rather than decorative — the artifact store is
now on the read path, not only the write path.

**A cached row whose artifact is gone is a miss, not a hit.** This is the load-bearing detail and
the reason `run_cached_hessian` is not built on `run_cached_with_artifacts`: those decide hit
versus miss from the result row alone, and here the row is only half the result. Artifacts are
optional by construction (D-124) — the store can be disabled, an artifact can exceed the cap, the
eviction sweep may reclaim a blob — so the read path verifies it can load the matrix before
claiming a hit, and a shape disagreeing with `atom_count` is rejected too. Without this, eviction
would be data loss and a mismatched blob would produce plausible, wrong frequencies.

A deployment with `artifact_store_enabled=False` therefore caches no Hessians and recomputes
exactly as it did before this split. That is a stated consequence, not a silent degradation.

**Two things this deleted.** `_CACHED_DIPOLE_DERIVATIVES` was a module-global dict handing
tblite's dipole derivatives across one call; with the Hessian cached, a hit would find it empty
and the IR intensities would break, so the derivatives became an artifact and the global went
away. And `compute_thermochemistry_with_artifacts` — added a week ago by D-124 — is gone, because
artifacts now belong to the layer that produces them.

**Also in this decision.**

- **`max_members` left the conformer cache key (STO-3).** It truncates a finished ensemble; it
  does not search. Keying on it meant "show me 20 instead of 10" re-ran CREST — by
  `calc/conformers.py`'s own docstring the most expensive single calculation in the system — to
  obtain an answer already in the store. `XtbSpec.unkeyed_fields()` is the seam: overriding it
  keeps the key derivation in one place, so a new field is still keyed by construction and
  *excluding* one is the visible, deliberate act.

- **A cross-method geometry pointer (STO-4), opt-in by construction.** The optimization cache keys
  on coordinates, so two RDKit embeddings of one molecule miss each other and a GFN-FF minimum
  cannot seed a GFN2 run. `calc/geometry.py` records the best known geometry per *subject*
  (canonical SMILES + charge + multiplicity + solvent) as an ordinary cached calculation — no new
  table. `run_cached_optimization` writes to it and deliberately does **not** read from it:
  silently swapping a caller's starting geometry would make one request return different answers
  under one key depending on what the store happened to hold, which is precisely the cache
  dishonesty `calc/xtb_spec.py` was written to prevent. The reuse is an explicit lookup that
  resolves a subject and *then* optimizes normally, so the key always names what really ran.

**Costs, stated rather than discovered.** Existing `xtb.hess` and `xtb.conformers` cache rows
cold-start: the key shapes changed, and there is no migration path that would be honest about what
the old rows contain. A stored conformer ensemble is now the whole ensemble rather than a
truncated one, so those rows are larger — `total_found` already reported the true count, so
nothing starts lying; the row simply holds what it counted.

**A finding that revised the plan.** The audit assumed every task had by-products worth keeping and
that the optimizer's capture path merely needed wiring. It does not: `xtbopt.xyz` is parsed in full
into `OptimizationResult.structure`, which the cache already persists, so capturing it would be a
second copy of the cache. `_ALREADY_STORED` names it alongside `xtbout.json` and an `opt` run
captures nothing. The same reasoning applies to CREST, whose ensemble file is now fully represented
in the result row. `hessian`/`vibspectrum` are *not* on that list even though the `.npy` holds the
same numbers: the two serve different readers — the `.npy` is this system's read path, the
Turbomole files are what every other quantum chemistry program can open — and content addressing
means two runs over an identical geometry share one copy of each.

**Alternatives rejected.** Putting the matrix in JSONB (a 416 kB row on the hot path, and Postgres
would TOAST it anyway with none of the dedup). Making artifacts mandatory once something reads them
(it would turn the eviction sweep D-124 built into data loss). Keying the Hessian on the full
`ThermoSpec` and post-correcting the thermochemistry (the recomputation is exactly what this
removes).

**Verification.** `tests/test_xtb_hessian.py` asserts the property end to end as a call count on
the expensive half: two thermochemistry requests differing only in temperature produce two
different free energies and exactly **one** Hessian. Also pinned: the negative control
(`displacement_angstrom` still forces a recomputation), the evicted-artifact fallback, the
disabled-store behaviour, and the shape check. `tests/test_conformers.py` and
`tests/test_geometry.py` cover STO-3 and STO-4, including that consulting the geometry pointer
never changes an optimization's cache key.

**Not covered here.** The end-to-end assertions that need the `xtb`/`crest` binaries are
`@needs_xtb`/`@needs_crest` and do not run in the environment this was written in. Every logic path
that does not need a binary is tested by a test that actually runs.

---

## D-133 — A submission is a note and what it needs, so a computed result can cite the compound it is about

**Context.** `connectors/qm/knowledge.py` documented its own limitation precisely: it emitted no
wikilink to the compound a calculation was about, because a dangling link fails `kg-validate` on
the very PR that adds the note. The compound note might not exist yet, and there was no way to
create it in the same change.

That single constraint made the calculation store and the knowledge graph disjoint. "What we
computed" and "what we know" could not reference each other in either direction — a stale
calculation could not be traced to the conclusions drawn from it, and a conclusion could not be
traced to the run behind it. In a GxP system that is a provenance gap, not an ergonomic one.
`memory/supersede.py` hit the same wall and worked around it by naming a replacement in plain text.

The cause was one field. `NoteSubmission` was exactly one `path` plus one `content`.

**Decision.** A submission carries `files: list[NoteFile]` — the note first, then whatever it
depends on. `propose_note(..., dependencies=[...])` lays them into one PR, so a note and its
targets land in one reviewable unit and one human signs off on both. A dependency already merged
renders byte-identically and produces no diff, so the submission stays idempotent.

The rule is applied **once, at the gate**, not in each connector: `eln.compound.compound_dependencies`
mints the compound note a note links, and `publish_memory_note_activity` (the one path every
machine-written note takes) calls it. A note author states the link; the gate makes it resolve.
Because `compound_id` is derived from the canonical structure, the target is fully determined by
the SMILES the note already carries.

**`calc_refs` and `artifact_refs` are frontmatter, deliberately not wikilinks.** They point *out*
of the graph into Postgres. Making them edges would reintroduce, from the other side, the exact
dangling-link failure this decision removes. They are shape-validated at the schema — prose like
`"the GFN2 run"` in a provenance field is a crosslink nothing can resolve, and it should fail at
the gate rather than pass review looking informative. Whether the target *exists* is a question
only a database can answer, and `kg-validate` runs in CI without one; making it need a database
would be a worse regression than the gap it closes.

`kg/crosslink.py` is the reverse direction — calculation key to the notes resting on it — and is
nine lines over the already-cached parsed notes rather than an index, because a second store here
would be a derived index of a derived index. An `artifact_refs` entry contributes the key of the
run that produced it, so a note citing only a Hessian is still found by a question about its
calculation.

**Rejected: convenience `path`/`content` properties on `NoteSubmission`.** They were written and
removed. A read-only property shadows anything `model_copy(update=...)` writes, so the old field
names kept resolving and silently ignored the update — a real test caught it. One shape, no
aliases.

**Left alone: `memory/supersede.py`.** Its plain-text replacement marker could now be a
`superseded-by` edge, but its choice is deliberate and documented: the replacement is itself an
unmerged proposal in the same run, published as a separate note by the fan-out, so a link would
dangle if a reviewer merged the supersede PR first. Churning a working GxP path for a marginal gain
is not warranted; D-134 makes the alternative available when the fan-out is revisited.

**Verification.** `tests/test_crosslink.py` writes both files of a real submission to disk and runs
the actual validator over them — no dangling link — with the negative control that the note alone
would have failed. The assertion in `tests/test_knowledge.py` that used to read
`note.outgoing_links() == []`, with a comment explaining why the link was impossible, now asserts
the link and its dependency.

---

## D-134 — Edges carry relations and their own validity, so the graph stops being a citation network

**Context.** `kg/graph.py:150` was `graph.add_edge(note.id, target)`. No attributes at all. Nothing
could say *precursor-of*, *contradicts*, *measured-by* or *computed-from*, so every graph query was
structurally blind to what a connection meant, and the retrieval layer treated the graph as what it
was: a citation network. Separately, `valid_from`/`valid_to` existed on nodes, so a *fact* that
stopped being true was expressible while a *relation* that stopped being true was not.

**Decision.** Two syntaxes, because they serve different authors:

- **Body:** `[[rel:target]]`. The syntax was free to take — `_SLUG` excludes `:`, so
  `[[precursor-of:x]]` previously parsed as one dangling id and failed `kg-validate`, meaning no
  corpus could be relying on it.
- **Frontmatter:** `relations: [{rel, to, confidence?, valid_from?, valid_to?}]` — the structured
  form, and the only place per-edge metadata can live.

`cited_ids` and `outgoing_links` keep returning bare targets, so `kg.validate`'s dangling-link
check and the answer verifier work unchanged through one code path rather than two. A bare
`[[link]]` still means exactly what it always meant (`cites`), which is asserted against the
shipped corpus rather than assumed.

`kg/relations.py` holds `KNOWN_RELATIONS`, **adopted from RXNO / CHMO / CHEMINF / OntoRXN** rather
than invented, so it maps to a standard later instead of being one more thing to reconcile.
Enforced by `kg.validate`, not by the schema — exactly as `KNOWN_NOTE_TYPES` is, and for the same
reason: the agent must be able to propose a genuinely new relation, and the PR-gate is where a
human decides.

**Kept on `nx.DiGraph`, with a tuple of relations per edge.** A `MultiDiGraph` models parallel
edges properly and would change the meaning of `graph[a][b]` for every existing reader —
`neighborhood`, `kg.analytics`, the retrievers — to solve a case that barely arises. The cost is
that an edge holds a *set* of relations rather than one, which is why the attribute is plural and
why a compound that is both precursor and product of one reaction is tested.

**A bug this created, and fixed.** `report/retrievers.py:_excerpt` did `WIKILINK.sub(r"\1", ...)`,
which would have rendered `[[precursor-of:x]]` into a report a person reads as
`precursor-of:x`. It now strips to the target through the same shared splitter the indexer uses.

**Downstream in the same decision.**

- **Conflict signalling (KM-8).** Retrieval used to return two contradictory notes with no marker,
  which reads as corroboration — worse than returning neither. `kg/conflicts.py` reports a
  `declared` conflict (a `contradicts`/`supersedes` edge, now expressible) and a `suspected` one
  (same type, same compound, overlapping validity, materially different confidence). There is
  deliberately **no property extractor**: parsing "the yield was 82%" out of prose and comparing it
  across notes is a natural-language problem this layer would get subtly wrong, and a false
  conflict is as damaging as a missed one. A conflict is a **flag** on the evidence
  (`EvidenceChunk.conflicts_with`), never a filter — dropping one side would be retrieval deciding
  which of two curated notes is right, and it has no basis for that.

- **Negative feedback (KM-12).** `failure-mode` sat in `KNOWN_NOTE_TYPES` with nothing minting one.
  `memory/failure.py` builds it, carrying a `contradicts` relation to what it refutes — which is
  what makes the feedback actually feed back, since before typed edges a correction could only be
  prose and `find_conflicts` could not see it. It goes through the PR-gate like everything else: a
  machine-written note asserting that curated knowledge is wrong needs *more* human sign-off, not
  less. The refuted note is never edited or deleted.

**Verification.** `tests/test_relations.py` pins backward compatibility against the real shipped
fixture corpus (every pre-existing edge is still exactly `cites`), the new syntax, both forms
producing one edge, an unknown relation failing validation, every known relation passing, and an
edge whose validity has lapsed dropping out of a time-scoped query while both its notes stay
current. `tests/test_conflicts.py` covers the detector, including the cases it must *not* report.

---

## D-135 — A dataset may be vendored into the image at build time — the one amendment to D-089's scope

**Context.** D-089 fixed the scope: this system takes no external data sources, and
`tests/test_no_egress.py` enforces it because the prose form of the same constraint demonstrably
did not (TOOL-6 sat in `DEFERRED.md` as "blocked on choosing a source", which reads as an
invitation, and duly got built).

That decision is right about what it rules out: a *runtime* dependency on somebody else's service —
an address in first-party code, a network call on the retrieval path, an availability and licensing
question the deployment cannot answer. What it was never meant to rule out is knowing things. The
gap that leaves is concrete: `chemclaw/reagents.py` is a hand-maintained name→SMILES table and it
is the hard ceiling on `resolve_compound`, so a chemist naming an ordinary reagent gets nothing
back. Every fix for that is a dataset.

**Decision.** A dataset may arrive the way a dependency arrives: **installed into the container
image at build time**, pinned to a version, checksummed, licence-labelled, and reviewed once in a
pull request by a person who can read its licence. At runtime it is a file on local disk.
`sources/vendored/` attaches it through the existing manifest seam (D-120) with zero core edits.

The escalation is narrow, and three things keep it that way:

1. **No network path exists.** `tests/test_no_egress.py` is *extended, never relaxed*: a new test
   asserts `sources/vendored_dataset.py` imports no HTTP client, so it cannot acquire one by
   accident in a later edit either. The source is also named in the registry assertion rather than
   exempted from it.
2. **Provenance is required by the schema.** `name`, `version`, `licence`, `retrieved_from`,
   `description` and `sha256` are all mandatory. A corpus with no recorded licence is a legal
   question nobody can answer later; one with no checksum cannot be shown to be what the review
   approved. `retrieved_from` is documentation — nothing reads it as an address and nothing can
   fetch it.
3. **Retrieve-only.** Vendored data is reference material, not experiments. An ingest half would
   give unreviewed third-party records a write path into the knowledge graph behind the PR-gate's
   back, which is a much larger decision than reading a table.

A checksum mismatch refuses to load and names both hashes, because the tempting fix — editing the
manifest to agree with the bytes — defeats the mechanism entirely. A missing dataset yields no
evidence rather than raising: an optional corpus that is not installed must not break every query
in the process. Citations read `vendored:<dataset>:<row>` rather than posing as note ids: a
citation must resolve to something a reader can check, and for vendored data that is the row.

**What actually ships, stated plainly.** The mechanism, plus `common-reagents` v0.1.0 — a
first-party, hand-authored reagent/solvent/base/ligand table under the trivial names chemists write
(`DIPEA`, `Cs2CO3`, `mCPBA`, `T3P`). It carries no licensing question at all and is independently
useful. **No third-party dataset has been vendored.** Doing so is a build-pipeline step plus a
licence review and belongs to whoever adds one; `data/vendored/README.md` says how. Not enabled by
default — a deployment shipping no dataset is unaffected by the mechanism existing.

**Also in this decision: the embedding cache (STO-12).** The audit's finding on "tool result
caching" was largely that it is *not* a gap — every calculator already routes through `run_cached`,
and the RDKit chem tools are cheaper than the Postgres round trip a cache would add, so building a
caching subsystem there would have been ceremony. Saying so is the finding. The one genuine
repetition is `embed_texts`: every retrieval embeds its query, the same query recurs constantly,
and under a real provider each repeat is a network round trip on the interactive path paid by all
three graph-backed retrievers. It is now cached, bounded, keyed on **provider + model + dimension +
text** — the same lesson D-011 taught, since serving one model's vectors after a switch would
corrupt every similarity comparison silently.

**And the seed corpus (STO-10).** `knowledge/` held `.gitkeep`, so `make kg-validate` passed by
validating nothing and every retrieval, crosslink and conflict property was measured against
fixtures. It now holds 37 seed notes covering all ten note types and all fourteen relations, with
real instances of the awkward cases: a superseded pair with a closed `valid_to`, a declared
conflict, calculation crosslinks including an artifact reference. The original plan proposed
*promoting* `evals/retrieval_corpus/` into it; that was wrong and is recorded as such — that
directory's README states it is kept outside `knowledge_dir` precisely so the recall/precision
numbers stay independent of the live graph. The two are separate, and a test asserts they share no
ids.
## D-136 — The shipped defaults were never executed: three configurations that fail on first contact

An intense review of the agentic system, asked to find ways to make it faster *and* more reliable
at once. The performance leads it started from were mostly already fixed (D-119 pooling, D-121
multi-process, the gathered retriever fan-out), so what it found instead was a class of defect the
1176-test suite is structurally unable to see, and three live instances of it.

**The class: a value that is only wrong at a boundary no test crosses.** Every test injects a fake
chat client, so no test has ever sent a generation parameter to a real model endpoint. Every chart
test constructs `Settings(**helm_values)`, so no test has ever *executed* a production config
value. Both suites are green, thorough, and blind in the same direction — they validate shapes,
and these defects are all about what happens when a shape meets a real system.

**Instance 1 — the default config could not complete a single turn.** `build_agent` always put
`temperature` on the wire from `llm_temperature` (default `0.0`). The shipped `agent_model`,
claude-sonnet-5, rejects it: `400 invalid_request_error: temperature is deprecated for this
model`. Every turn on the default Anthropic path failed on first contact. Found by capturing the
real outgoing request for one turn. `llm_temperature` is now `float | None`, unset by default, and
the key is omitted from `ChatOptions` entirely when None — omitting is not the same as sending
null, which the API also rejects.

**Instance 2 — the shipped chart could not start a pod.** `values.yaml` sets
`CHEMCLAW_OTEL_ENABLED: "true"`; no OpenTelemetry SDK or OTLP exporter was declared in
`pyproject.toml`. `configure_telemetry()` runs unconditionally at process start in the front door,
the background worker and every connector worker, so all of them raised and the ASGI lifespan
returned `lifespan.startup.failed`. On a real cluster the whole deployment CrashLoopBackOffs;
only the six connector MCP servers stay up, serving tools no agent can reach. The dependencies are
now declared, and the new test *executes* `configure_telemetry()` under the shipped value.

**Instance 3 — a raising agent factory deadlocked the pod permanently.** `AgentPool._checkout`
incremented `_built` before calling the factory, so a factory that raised burned a slot: the pool
counted an agent that never existed and never reached the free queue. After `size` such failures
it could neither build nor hand one out, and every later turn blocked for the full
`service_turn_timeout_seconds`, forever, on a pod still reporting healthy. Reachable: the factory
reads the TLS CA bundle from disk and requires a credential, so a cold pod taking its first turns
before its secret volume is populated hits exactly this. The count is now committed only after the
agent exists.

**Also landed, from the same review — the per-turn connector seam, three consequences of one
design decision.** A connector tool is built fresh per turn, which is a correctness requirement
(D-118). Three things followed from it that were not intended:

- *Every connector call was capped at 5 s.* The per-turn `httpx.AsyncClient` was constructed with
  no `timeout=`, so httpx's 5 s default applied to every phase, while `request_timeout` bounded
  only the MCP application-level wait. Measured against a real server: an 8 s tool call had its
  HTTP stream torn down at 5 s, the MCP response never arrived, and the caller then blocked for the
  *full* `request_timeout` before failing — 60 s for calc, holding an admission permit and an agent
  lease throughout. `request_timeout` was not preventing a hang; it was setting its length. A tool
  slower than 5 s is ordinary here: an uncached `predict_pka` runs xTB inline.
- *Six `httpx.AsyncClient`s leaked per turn.* Neither layer below takes ownership of a
  caller-supplied client — MCP enters it into an exit stack only when it created it, and MAF's
  `close()` never touches it. The same leak class D-119 fixed for Postgres, on the connector side.
  `DegradingHttpConnector` now closes what it was handed.
- *The six connects were serial.* `connectors.health.probe_connectors` already gathers its probes
  with the rationale "the sum of the timeouts rather than the slowest one"; the path every turn
  actually takes did not. Gathering is safe for the per-turn-instance rule, which is about object
  lifetime rather than connect ordering, and MAF runs each connector's lifecycle on its own task.

**Measured, and the reason caching is the next thing worth doing.** Capturing the real request for
one turn: the fixed prefix is **14,595 tokens** before the chemist says anything — 3,463 of system
instructions plus skills manifest, 11,132 of tool schemas — rising to ~20.5 k once the connector
MCP tools are attached. There is no prompt caching anywhere in first-party code (`cache_control`
appears zero times), so that prefix is re-paid on every model call, and up to 25 times per turn in
harness mode. This is recorded rather than fixed: MAF's Anthropic client exposes structured
instruction blocks, which reaches the system half, but offers no `cache_control` hook for `tools`
— the 11 k that dominates. See `BACKLOG.md`.

**What this changes about how to test this system.** A green suite proved these paths were
*shaped* correctly. The gap is that a shipped default is a claim about the world, and the only way
to check it is to run it. The new tests execute production values rather than validating them; the
chart parity test should grow the same property.

## D-137 — The plan the model could approve for itself: a pre-execution gate that is not a tool

`SECURITY.md`, `docs/harness-konzept.md` §6 and `build_agent`'s docstring all described a GxP
pre-execution gate: in `plan_only` the agent proposes a plan and waits for a human before
executing. The shipped production configuration runs exactly that (`harness_enabled=true`,
`harness_autonomy=plan_only`).

**The gate did not exist.** MAF's `AgentModeProvider.before_run` injects a `mode_set` tool into the
model's own tool surface on every run, declared `approval_mode="never_require"`, and its
instructions tell the model to use it: *"When approval is granted, always switch to execute mode
(using the `mode_set` tool)"* — where "approval is granted" is the model's own reading of the
conversation. `grep set_agent_mode` returned zero callers in the repository. `plan_mode_required_for`,
which `harness-konzept.md` §6 specifies as the enforcement mechanism, exists nowhere in the code.

Three properties were missing, and the third is the one that makes this worse than a missing
control rather than merely equal to one:

1. Nothing stopped the model changing its own mode.
2. Nothing bound an approval to a *particular* plan, so a plan approved and then rewritten kept
   its authorization.
3. The audit middleware attributes every tool call to the ambient actor — so the trail recorded the
   agent's self-authorization under the **chemist's** Entra oid. An attributable-looking approval
   with no human act behind it is evidence of the wrong thing.

**The fix, and why it is shaped this way.**

*Retract, do not reimplement.* `PlanApprovalModeProvider` runs MAF's `before_run` unchanged and
then removes `mode_set` from the invocation's tool list. The same method also injects `mode_get`,
the mode instructions, and the external-change notification; a reimplementation would silently drop
whichever of those upstream adds next. `mode_get` stays — reading the mode is harmless, and a model
that cannot see its own mode behaves worse, not better.

*Use the supported external seam.* MAF ships `set_agent_mode` precisely for callers outside the
model, and it records the previous mode so the next `before_run` tells the agent the mode changed
underneath it. Writing session state directly would have skipped that and left the agent anchored
to what it last believed.

*Bind the approval to a plan hash.* An approval recording only "this session may execute" would
authorize whatever the plan later became. The hash is over the rendered todo lines — exactly the
strings the surfaces display (`todo_titles` feeds `PlanEvent`) — so what was approved and what was
shown cannot diverge. Hashing richer internal state would let the authorized artifact drift from the
displayed one. A changed plan is a different hash and is unapproved; the decision route answers 409
rather than silently approving the current plan.

*Persist it.* `plan_approvals` is append-only: each row is a GxP record of something a person did at
a moment, so a second decision is a second row and the read path takes the latest — a rejection
after an approval revokes it. It is durable rather than session state because the mode it authorizes
is *already* durable: an approval that vanished on an LRU eviction while its effect persisted would
leave a session running in execute mode with nothing recording who allowed it.

*Not an agent tool.* `POST /sessions/{id}/plan/decision` is owner-scoped and reachable only by an
authenticated principal, for the same reason `POST /approvals/{id}/decision` is not a tool (D-005).

**Why the existing tests could not see it.** Two tests asserted the gate. One checked
`mode_provider.default_mode` — the initial value. One checked that the loop does not *auto*-start.
Neither ever had the model call `mode_set`, which was the only thing that broke it. A test for an
access-control property has to attempt the access. `tests/test_harness_mode.py` now does, and it
also pins the upstream behaviour: if MAF ever stops injecting `mode_set`, the assertion that it is
absent would start passing vacuously, so a second test asserts stock `AgentModeProvider` still
advertises it. That failing is a signal to re-decide, not a bug.

## D-138 — Fifty questions, asked live: the job surface was dead, the trace was blind, and a failed tool was silent

**Status:** accepted · **Context:** a catalogue of 50 questions from a process/analytical development
scientist and their project manager, asked against the running stack (real Anthropic traffic, real
per-user Entra identity, Postgres sessions and audit, Temporal, all six connector bundles) rather
than against the test suite.

Five defects, four of them invisible to a suite that is otherwise thorough. Each is recorded with
the question that exposed it, because the questions are the reason they were found at all.

### 1. Every declared connector job was broken, in production, from the day the seam landed

Q11 asked for the reaction energy of an acetylation. `compute_reaction_energy` failed three times
with `'dict' object has no attribute 'model_dump'`, MAF stopped the tool loop after three
consecutive errors, and the turn ended mid-sentence. The same failure hit `compare_solvents`
(Q13, Q07), `scan_coordinate` (Q12) and `compute_thermochemistry` (Q10). It applies to every job
the manifests declare — `compute_reaction_energy`, `compare_solvents`, `scan_coordinate`,
`sample_conformers`, `compute_interaction_energy`, `start_optimization_campaign`,
`compute_dft_energy` — so the entire durable-compute half of the system was unreachable from a
conversation. Across 50 questions, before the fix: **zero jobs started, ever**.

`build_job_tool` declared its parameter as a generated pydantic model and then called
`model_dump()` on it, under the comment *"Validation has already happened — MAF constructs the
model from the tool call's arguments before the body runs."* It does not. MAF publishes the
model's JSON schema and hands the body the decoded JSON *object*. The tool now validates what it
is given (`params_model.model_validate`), which also repairs the `precondition` hook — it had been
receiving a dict whose attributes it could not read.

**Why the tests could not see it.** `tests/test_connector_jobs.py` has twenty-one tests over this
factory, and its helper builds the model and passes the instance — with a docstring claiming that
is "what MAF does". A test that constructs the argument itself cannot discover that nothing else
does. The three tests added here go through the framework's own dispatcher instead, and the middle
one pins the property that would otherwise be repaired the lazy way: accepting a dict must not mean
forwarding *any* dict, or the declared type would be advertised to the model and enforced nowhere.

### 2. `ToolCallEvent.arguments` was empty on every call ever emitted

The field is documented as "a short argument preview" and is rendered by the UI trace. Across the
first run: 112 tool calls, **0 with arguments**. Not a bug in the sense of a wrong value — the
field could not have carried anything else.

A streamed call does not arrive as one object. The name comes first, on a content whose `arguments`
is still empty; the argument JSON then streams as fragments on contents carrying only the
`call_id`. The extractor read name-and-arguments off a single content, so it matched exactly the
one content that never has arguments and skipped every fragment for want of a name. `_ToolCallTrace`
reassembles the call and emits it once complete — which is also the more truthful moment, since a
tool cannot run before its arguments are.

**This defect survived its own first fix**, which is the part worth remembering. The reassembly was
written, unit-tested against a synthetic stream, deployed — and the live run came back 0/147 again.
The provider opens the argument stream with an *empty* fragment, and the first version read that as
"nothing more is coming" and closed the call immediately. The synthetic stream had been written from
a capture that omitted it. After: **139 of 147** calls carry their arguments; the remainder are
calls that genuinely had none.

### 3. A failing tool was invisible to the person who asked

Q11's turn ended on the model's last words before its final failure — *"Let me try the carboxylic
acid acetylation:"* — with no answer and no error. The failure was in the log, in the audit trail,
and in the model's context. It was in none of the places the chemist can see. `ErrorEvent` was right
to stay silent (the turn had not failed); what was missing was the trace being honest about a step
that did not work.

`ToolFailureSignal` joins the existing turn-signal union and surfaces as a `tool_failed` event.
`announce_tool_failures` is attached innermost, closest to the tool body, so it sees the raw
exception from *every* failure including the two that `surface_authorization_denials` and
`surface_domain_errors` convert into results: what the model is told and what the transcript shows
are separate questions. It observes and re-raises, so audit and both converters behave exactly as
before.

### 4. The graph retriever matched the query verbatim, so ordinary phrasing found nothing

Q26 ("have we run anything like this biaryl coupling before?"), Q43 and Q46 were all answered with
some form of "I need you to tell me which one" — against a corpus whose largest cluster is a Suzuki
biaryl campaign. `gather_evidence("biaryl")` returns the campaign, the compound and the playbook.
`gather_evidence("the biaryl")` returned nothing at all.

`GraphRetriever` tested `query.lower() in note_text(note).lower()` — the whole query as one
substring — so a note had to literally contain the sentence a chemist typed. The docstring warned
about the opposite risk (`ester` matching `polyester`) and never about this one, and with the graph
retriever the only source enabled by default, an empty result is the agent's whole view of the
record. Matching is now per term, all-terms-must-match so precision is unchanged for any query that
already worked, widening to any-term with coverage ranking rather than answering "nothing known".
The stopword list is deliberately fourteen words: it exists to stop `the` from erasing a hit, not to
do linguistics.

### 5. Two instruction gaps the tools could not fix

*Ask-before-search.* Sixteen of fifty answers used **no tool at all**, most of them handing the
chemist a form to fill in for data the system holds or can compute. With defect 4 fixed and an
explicit "look before you ask" rule — search first, resolve names, ask only when the search came
back empty, and answer partially rather than withholding everything — that fell to **ten**, and six
questions that had asked for input now answer from the record.

*The system did not know its own compliance story.* Asked (Q46) what to show an auditor who wants
proof a computed number was not edited, the agent described job ids and re-polling
`get_durable_job_status` — reproducibility, which is a different claim — and never mentioned the
tamper-evident hash chain, the fields it records, or `make audit-verify`. It is the one question in
the catalogue where being confidently wrong has a regulatory cost, so the trail is now described in
the instructions.

### What this says about the test suite

Four of these five were invisible to 1450 passing tests, and the pattern is the same each time: the
test supplied the thing the system was supposed to supply. The job tests built the model MAF was
meant to build. The tool-call test asserted the event type, never its contents. The retriever tests
queried with the exact word the fixture contained. None of that is sloppiness — each is the natural
way to write the test — and none of it can find a defect in the seam between the component and its
real caller. That is what a live catalogue is for, and it is why the three new job tests drive MAF's
dispatcher and the retriever tests query the way a person would rather than the way the fixture was
written.

### Left open, deliberately

*A durable job's domain error does not reach the model.* With the launcher fixed, Q11 launched and
`CalcJobWorkflow` correctly rejected the model's unbalanced equation — the model had written
salicylic acid + Ac2O → aspirin with no acetic acid by-product. The message the chemist needed
("reaction is not atom-balanced (reactants minus products): C +2, H +4, O +2") stayed in the worker
log; the tool raised `WorkflowFailureError: Workflow execution failed`. The check also retried five
times, though no retry can change the outcome. Recorded as VIBE-1 rather than fixed here: relaying
a workflow's failure text to the model is a policy decision about what is safe to surface (the same
question `surface_domain_errors` answers by naming known-safe types), and it wants deciding rather
than patching.

*`resolve_compound` knows solvents and bases, not substrates.* Its table is 87 spellings, almost all
reagents; every substrate in the corpus — 4-bromoanisole, phenylboronic acid, salicylic acid — misses
and the model falls back to its own memory of the structure. It happened to be right each time
observed, which is the problem: a wrong structure propagates silently into every downstream
calculation. Recorded as VIBE-2; the fix touches the connector seam (the graph holds these
structures and the bundle must not import the graph) and is a design question, not an oversight.

## D-139 — Three silent failures: a degraded turn, a pooled calibration, and two counters wired to nothing

**Status:** accepted · **Context:** the fourth batch of the agentic-system review
(`docs/audit/2026-07-agentic-system-review.md`), taking the items whose common shape is that the
system was already *wrong* and said nothing. None of the three produced an error, a red test or a
failed turn; each produced a plausible answer or a plausible number.

**Decision.** Announce the degradation at the one place that can see it, scope the calibration read
to the version that produced the numbers, and increment the counters that were declared and never
written.

### 1. A turn that lost its connectors answered as if it had them (REV-6)

`connectors.registry.open_reachable` returns "the names of the connectors that are not connected,
**for the caller to surface**". All four callers — `service/runner.py`, `agents/cli.py`, and both
activities in `workflows/template_activities.py` — called it bare and discarded the list.

This is the quietest failure found in the review, because nothing in the system is in a position to
notice it. A connector that is down contributes no tools; the model is handed a shorter list and has
no way to know it is shorter, so it reasons from what remains and answers confidently. "The ELN has
nothing on that batch" and "the ELN was unreachable" arrive as the same sentence, and only one of
them is a fact about the chemistry.

The announcement moved *into* `open_reachable` rather than being added to four call sites: a return
value that must be read is a rule a new caller can forget, and this one had been forgotten four
times out of four. What the function now guarantees is the operator-visible half — a WARNING naming
the connectors, and `chemclaw_connectors_unreachable_total`, counted per connector so one dark host
and a dark fleet are different rates. Callers that can reach a *human* still read the list and say
so on their own surface: the front door yields a `CapabilityDegradedEvent` before the first token,
so an answer can be marked provisional while it streams rather than retroactively; the CLI prints to
stderr, which its docstring had promised since it was written and never did.

Deliberately not an error. An unreachable connector costs its tools, not the conversation — the
obvious over-correction for a silent failure is to start raising, which would turn one dark
connector into a dead front door. The turn still answers; it just stops pretending.

`run_tool_step` gets the list too, only to make its failure legible: a missing connector's functions
are simply absent from the assembled surface, so the error blamed the template for naming a tool the
template names correctly, which sends an operator to the wrong file on a retried activity.

This is the same defect class as D-138's `ToolFailedEvent` and the two are complements: that one
covers a tool that ran and raised, this one a tool that was never offered.

### 2. Calibration pooled every calculator version (REV-12)

`connectors/calc/server/tools.py` built every `PredictionRecord` without a `calc_version`, so all of
them carried the default `""` and the unique index `(calc_type, calc_version, input_hash)`
degenerated to `(calc_type, input_hash)`. A v2 prediction upserted over v1's row — destroying the
record it existed to be compared against.

Fixed on **both** sides, because either alone changes nothing. The write path passes the running
version (`calc.pka.calc_version`, `calc.solubility.calc_version`, promoted from private helpers). The
read path gained `AND calc_version = %s` and `calibration_for` now *requires* the version rather than
defaulting it — a default would silently reproduce the pooled reading this removes, and every caller
already knows which version answered. Pooled, a version running high and one running low cancel to a
bias near zero and the pair reads as well calibrated; `calculator_trust` was one line away from
telling a chemist that.

The observation write stays version-blind on purpose: a measurement is a fact about the molecule, not
about the calculator that guessed at it, and one reported value scoring every version's prediction is
what makes a version-over-version comparison possible at all.

Dormant today (`calibration_enabled` is off), which is exactly when to fix it — before any of these
numbers is quoted to a chemist.

### 3. Two counters declared and incremented by nothing (REV-19)

`chemclaw_jobs_started_total` and `chemclaw_notes_proposed_total` sat in the declaration table and
were written by no code, so every scrape reported a flat `0`. That is worse than omitting them:
`service/metrics.py`'s gauge path already refuses to emit an unbound gauge because "a fabricated
zero would be indistinguishable from a genuinely idle service", and these two had precisely that
failure with no such protection. A PR-gate rejecting every write looked identical to a quiet
afternoon.

The note counter increments *after* the submitter returns, not before: counting the attempt would
report a healthy gate during exactly the outage the metric exists to reveal.

`agents/audit.py`'s private `_record_metric` — the lazy, tolerant import that lets a Temporal worker
record a metric without ever building `service` — was promoted to `chemclaw/metrics_bridge.py` at its
fourth caller rather than being imported across modules by its underscore name. The swallow-all is
written once on purpose: a second copy of a bare `except Exception: pass` is where a real error goes
to hide.

### What this batch did not do

Two of the six items planned were **refuted by reading the code they proposed to change**, and both
are recorded rather than quietly dropped:

- **REV-7** (job→session push-back is at-most-once) proposed yielding before marking rows consumed.
  `agents/session_events.py` documents at-most-once as a *deliberate* trade made by COR-4, replacing
  an at-least-once claim that double-delivered. The recommendation would have reintroduced the bug
  COR-4 closed. The underlying risk is real — a consumer lost between claim and delivery loses the
  notification — but the fix is a visibility-timeout redelivery, a design change to a durable path,
  not a reordering. Rewritten in `BACKLOG.md` with that shape.
- **The planned ADR duplicate guard already exists.** `tests/test_decision_log.py` has held
  `test_the_registry_has_no_duplicate_reservations` since D-109, and it goes red on exactly the bad
  merge that prompted the plan item — verified by injecting a duplicate row. The collision was caught
  by hand during a merge before CI ever ran, which is why the guard was never observed firing and was
  wrongly assumed absent.

That is five refuted leads across this review against fourteen confirmed. Each refutation had the
same shape: something that looked like a missing safeguard was a considered trade whose reasoning
lived in a docstring or a test that had not been read closely enough.

## D-140 — A template's job step: resolved off the workflow thread, and finally able to fail

**Status:** accepted · **Context:** REV-13 and REV-17 from the agentic-system review. Both are
claims the code made about itself that were not true: a comment saying a lookup was I/O-free while
it read the filesystem, and a docstring saying the image build injected the deployment revision
while no build set it anywhere.

### The `job` step read the disk from workflow code, and could not fail

`TemplateWorkflow._run_job_step` called `connectors.registry.find_job` directly, inside
`workflow.unsafe.imports_passed_through()`. Its own comment acknowledged the registry "does
filesystem + YAML I/O on a cold process" and treated `@cache` as the mitigation. It is not one: the
cache is per worker process, so which connector, workflow type and task queue a child was started on
came from the disk of whichever worker happened to be replaying rather than from history. A worker
that came up with a different bundle set resolves the same step differently and Temporal refuses the
resulting mismatch.

**Decision:** resolve through a local activity, `resolve_job_step`. This is the pattern the repo had
already adopted one module over — `workflows.orchestrator.resolve_fan_out_limit` resolves the
fan-out bound the same way, for the same stated reason ("making the batch shape a pure function of
history"). Local rather than remote because it is a cached in-process lookup, not a network call:
the point is recording the answer, not offloading the work.

The second defect compounds with the first and is the worse of the two. `find_job` raises
`ConnectorError`, which subclasses `ValueError` — a plain exception, not an SDK `FailureError`.
Raised in workflow code, the Temporal SDK treats it as a suspected bug and suspends the workflow in
an internal task-failure retry loop that ignores the retry policy and never gives up. This is the
exact trap D-093 documented for fan-out children; nobody checked whether the template sequencer had
it too. So a template naming a job no enabled connector declares produced a run that **hung
forever** — strictly worse than one that fails, because nothing alerts and the run holds its id
against `REJECT_DUPLICATE`, so the corrected re-run is refused as a duplicate of the zombie.

Moving the lookup fixes this as a consequence: across an activity boundary the same error arrives as
an `ActivityError`, and `BAD_DATA_RETRY` lists `ValueError` non-retryable, so it fails on the first
attempt with a message naming the declared jobs. But the sequencer raises plain exceptions of its
own — an unknown step kind, an unresolvable reference — so `TemplateWorkflow` also gains
`failure_exception_types=[Exception]`. Scoped to `Exception` rather than a name list because the
classification that matters at an activity boundary is already made by `BAD_DATA_RETRY`; what this
decides is only whether the workflow may fail at all, and the answer is always yes.

`JobStep` was the one step kind no test had ever constructed, which is why both defects survived a
suite that covers the other two thoroughly.

### `deployment_revision` could never be set (REV-17)

`chemclaw/config.py` said the F6 image build injects the digest. Nothing did — not the
Containerfile, not the chart, not CI — so `settings.deployment_revision` was the literal `"unknown"`
in every deployment, and every audit record's "which version produced this result" column was a
constant. AG-14 read as met while being unmet, which is the failure mode worth naming: a GxP control
that is documented, tested, and inert.

**Decision:** a `CHEMCLAW_REVISION` build ARG exported as `CHEMCLAW_DEPLOYMENT_REVISION`, with the
image workflow passing the commit SHA.

A build ARG rather than a chart value because *the image is the thing that has a revision*. A chart
can be re-rendered against any tag, and a revision that disagrees with the running bytes is worse
than an honest "unknown". The default stays `unknown` so a local `docker build` reports truthfully
that it does not know. `envFrom` a ConfigMap does not clobber an image `ENV`, so a deployment that
genuinely wants to override it can still put the key in `.Values.config` — no chart change needed.

The commit SHA rather than the tag: a tag moves, and an audit record has to name bytes.

Pinned in two places for two different reasons. `tests/test_deploy_chart.py` checks the wiring
offline — the ARG exists, it reaches the environment under the name the settings prefix reads, and
CI passes a value — because each of those three is separately droppable and none is visible to
mypy or pytest on the Python tree. The image workflow additionally runs the built image and compares
the value, because only a built image can prove the ARG actually arrived, which is the half that was
missing when the original claim was written.

## D-141 — Two facts that stopped at a process boundary: a session's profile, and the turn's correlation id

**Status:** accepted · **Context:** REV-14 and REV-11. Both are cases where a fact core knows was
not written down at the edge of the process that knew it — so the next process, or the next hour,
made up a different answer.

### An evicted session silently regained the tools its profile had removed (REV-14)

`_LiveSessions` stores `(session, owner, profile)` and says why in its own docstring: the three "can
never drift", because "the profile decides which agent runs the turn *and* which connectors it gets,
so a session that lost it would silently change agent mid-conversation." The durable
`session_owners` row stored only the owner. So rehydration rebuilt the handle on the **default**
profile, and the code called that graceful:

> the conversation resumes with the full tool surface rather than a narrowed one

That has the direction backwards. A profile is **attenuation only** — `agents/chemclaw_agent.py`
states it twice, "it can only attenuate, never widen" — and `property-lookup` cuts the surface to
four tools, drops every connector but `calc`, and specifically removes the ability to start a
durable job. Coming back with the full surface is not a graceful degradation; it is the control
being switched off. Losing an attenuation is never the safe direction to fail in.

And it never needed a restart, which is how it was framed. The live cache is an LRU with a capacity
and **no TTL**, so on a busy pod session 1001 evicts session 1 while both are in use. A chemist
mid-conversation, having done nothing, regains every tool their profile removed, and nothing
anywhere says so.

**Decision:** persist the profile beside the owner (`infra/sql/021`, a nullable column) and rehydrate
onto it. A column rather than a second table because that row is already "the facts about a session
that must survive the LRU", and the profile is one of them by exactly the argument that put the
owner there. The comment declined this as "a migration in service of a case that degrades
gracefully" — the migration is one `ADD COLUMN IF NOT EXISTS`, and the case does not degrade
gracefully.

`None` has to survive the round trip as `None`: storing `""` for "no profile" would turn every
ordinary session into a request for a profile named empty-string, which `get_profile` rejects. That
is pinned by its own test, because it is the natural way to write this fix.

### The correlation id stopped at the process boundary (REV-11)

`agents.audit` stamps every in-core tool call with a correlation id, and it went no further. Not in
the connector identity headers, so a connector logged under an id of its own with nothing tying the
two records together. Not in `ConnectorJobInput`, so a durable run was an island in the trail. "Show
me everything that happened in this turn" was answerable in core and unanswerable across the four
runtimes a turn actually spans — which is most of what a GxP trail is for.

**Decision:** an `X-Chemclaw-Correlation-Id` header beside the actor, roles and session, and a
`correlation_id` field on `ConnectorJobInput` that becomes a workflow memo beside `requested_by`.

Both follow the shape already established for the actor rather than inventing one. The header is
**advisory, never authorization**, exactly as the module docstring requires of the others: a
connector may join its records to ours on it and must never make an access decision on a header's
word. The job field travels in the *input* because a workflow has no request context — the same
argument that put `requested_by` there — and is set as a **memo** rather than folded into `payload`,
because `payload` is exactly the arguments the model filled in, and metadata the LLM can write is
not metadata.

Absent rather than empty when there is no turn, matching the actor header's rule: off the request
path there genuinely is no correlation, and an empty id in a connector's log reads as one that
exists — the precise confusion this header exists to remove.

## D-142 — A production value has to be executed, not type-checked — and two guards that were off in the one deployment that needed them

**Status:** accepted · **Context:** REV-15 and REV-16. One is about what the chart tests can prove;
the other is about what the chart actually ships. They belong together because the first is what
makes the second checkable.

### The parity check did not reach half the pod's environment (REV-15)

`tests/test_helm_chart.py` built its view of pod environment from `.Values.config`, the secret refs,
the mTLS paths and `_helpers.tpl`. It never read `templates/config.yaml`, which *derives* two more
keys rather than copying them: `CHEMCLAW_NOTE_REPO_DIR` from the knowledge volume layout, and
`CHEMCLAW_CONNECTOR_URLS` from the enabled bundle set. Both were outside **both** tests — neither
"is this a real setting" nor "does this value load" applied to them.

`connector_urls` is the one that matters: a `dict[str, str]` parsed from rendered JSON, which is
exactly the shape that constructs fine in a unit test and crashes every pod at import when the
render is wrong. It had never been fed a rendered value at all.

**Decision:** discover the derived keys from the template (so a third is covered on the day it is
added), reproduce the helper's render offline, and feed both through `Settings`.

Writing that surfaced the more interesting half. Passing the rendered JSON as an `__init__` kwarg
**fails** with `dict_type`: pydantic-settings JSON-decodes a complex field from an environment
variable and does not from a kwarg. So the existing test's model of "the pod environment" was not
merely incomplete for these keys, it was the wrong mechanism — and a test that constructs `Settings`
from literals cannot discover that. The derived keys now go through `monkeypatch.setenv`, which is
how the pod receives them.

The `connector_urls` result is asserted, not merely constructed: a render that produced `{}` still
builds a perfectly valid `Settings` while pointing the front door at nothing. Verified by disabling
every connector server in `values.yaml` and watching the test go red.

**And the inverse direction now has tests of its own.** This is the lesson D-136 paid for: OTel was
enabled in the chart, loaded as a perfectly valid bool, and CrashLoopBackOff'd every Python
component on first deploy because the SDK was not in the dependency closure. `test_logging.py` added
one executed-value test for that case; the two below generalize the shape — take the shipped values
and assert the thing they switch on actually *happens*.

### Two guards that were off in the only deployment that needed them (REV-16)

**`budget_enabled` → true in the chart.** Its rationale for being off was, in full, "Off by default
(today's behavior)" — off because it was off. Off is right as a *code* default: a CLI or a test must
not answer 429. But a deployment serving real users has no reason to be unguarded. A single turn is
iteration-capped and the *number* of turns is not, so a client or an automated push-back loop
accumulates unbounded LLM spend, and the load run that validated this system ran with budgets on —
so "on" is the configuration that was actually measured.

**`audit_verify_enabled` → true in the chart.** Its docstring says it "only earns a Schedule where a
durable audit sink is actually configured". This chart sets `SESSION_STORE: postgres`, which is
precisely what makes `default_audit_sink()` durable — so the precondition holds here and nowhere
else, and the flag was still off. The one deployment that *has* a tamper-evident chain was the one
never checking it, and a chain nobody checks detects tampering only after somebody thinks to look,
which is the failure mode the chain exists to remove.

**`connectors_required` deliberately left false.** The third flag REV-16 named, and the one the
review had wrong. Unlike the other two, its docstring is a real considered trade — `false` is
"degrade loudly", `true` is fail-fast "for a deployment where serving with a silently reduced tool
surface is worse than not serving at all". The review's argument for flipping it was that the
degradation was silent. **That was true when the review was written and is no longer true**: D-139
made an unreachable connector produce a `CapabilityDegradedEvent`, a WARNING and a counter. Flipping
to fail-fast now would trade availability away for a property already obtained more cheaply — one
dark connector taking down the whole front door, to get visibility that already exists. Recorded
rather than done, because a switch whose reasoning has been read is the only kind worth flipping.

Both flags that did change are pinned by *executed* tests rather than by asserting the flag: the
budget one drives a `BudgetTracker` past a cap under the chart's own settings (because
`budget_enabled=true` with every cap at 0 also parses and guards nothing), and the audit one asserts
`audit-verify` appears in the built schedule list (because a flag is one branch away from a schedule
that is planned and never applied). Both go red when the value is set back to `"false"`.

## D-143 — Nobody was collecting the metrics, and the durable history is never compacted

**Status:** accepted · **Context:** REV-2 and REV-4, the two remaining High findings. One is a
four-line chart file that should have existed from the start; the other is a confirmed defect whose
obvious fix destroys data, so it is documented and pinned rather than patched.

### Nothing scraped `/metrics` (REV-2)

The route has existed since DEP-4. Nothing under `deploy/` collected it — no ServiceMonitor, no
PodMonitor, no `prometheus.io/scrape` annotation. Every counter, gauge and histogram in the system
was exposed and uncollected in production.

That is the quiet way an observability story fails: the code is written, the endpoint answers, and
no dashboard or alert has ever had a data point. It is also the finding that retroactively blunts
several others — `chemclaw_connectors_unreachable_total` (D-139), `chemclaw_notes_proposed_total`
and `chemclaw_jobs_started_total` (D-139), `chemclaw_rollback_watermark_unavailable_total` all exist
specifically so an operator can *see* something, and none of them was reaching anyone.

**Decision:** a `ServiceMonitor` on the front-door Service, gated on `monitoring.enabled`.

- **A ServiceMonitor, not annotations**, because the target is OpenShift, whose user-workload
  monitoring stack is the Prometheus Operator; annotations are the older convention its default
  configuration does not read.
- **By port *name*** (`http`), so a port change cannot silently orphan the scrape.
- **The front door only.** The workers and connector pods import `chemclaw.metrics_bridge`, whose
  entire contract is that recording a metric outside the front door is a no-op — there is no
  registry and no HTTP surface in those processes. A scrape pointed at them would collect nothing
  and report the target as healthy.
- **`additionalLabels: {}`**, because a cluster's `serviceMonitorSelector` is release-specific and
  cannot be guessed; an operator sets it, and an empty default is honest about not knowing.

`ServiceMonitor` joins `Route` in `_UNVALIDATED_KINDS` — the chart's existing guard caught the new
CRD immediately, which is what that guard is for. And the path is checked against the *app*: a
ServiceMonitor naming `/metric` renders, validates, deploys, and collects nothing forever while
Prometheus reports the target down and an operator reads it as a broken pod. That is D-142's lesson
applied — a production value has to be executed, not type-checked.

### After-run compaction does not apply to the durable store, and the obvious fix corrupts data (REV-4)

**Confirmed.** `CompactionProvider.after_run` reads
`session.state[history_source_id]["messages"]` — where `InMemoryHistoryProvider` keeps its thread.
`PostgresHistoryProvider` deliberately keeps nothing there, which is the entire point of it, so the
lookup finds nothing and the strategy returns having touched nothing. Under the production default,
`_build_compaction`'s `after_strategy` is a silent no-op, and its docstring's promise to "shrink the
persisted history so the next turn starts smaller" was false.

**Two corrections to how the finding was framed.** The `before_run` half *does* work under Postgres
— it compacts what earlier providers loaded into the context — so the model's input is still
bounded and this is not a context-window bug. What is unbounded is this provider's own read
(`_SELECT` has no `LIMIT`, so every turn loads the entire history) and the stored history, which
grows for the session's whole life.

**Decision: document and pin, do not patch.** The obvious fix is a `LIMIT` on the load, and it is
unsafe in a way that is easy to miss. `get_messages` repairs unmatched tool-call pairings on read —
correctly, because a `SIGKILL` between a tool call and its result leaves a genuine orphan that
breaks every later turn — and that repair **writes back**, deleting and rewriting stored rows. Over
a windowed read, a `tool_result` whose `tool_use` merely fell outside the window is indistinguishable
from one whose `tool_use` never arrived. The repair would strip it and commit that, permanently
destroying a pairing that was intact on disk.

So a correct bound needs either the repair to run in memory only when the load is partial, or real
durable compaction that prunes whole groups from `session_messages`. Both are design changes to a
durable path with a data-loss failure mode, and both want their own ADR rather than being written
under a review item.

What ships here is the honest version: both docstrings that promised the opposite are corrected, and
`tests/test_durable_compaction_gap.py` pins the no-op *and* the write-back hazard — the second
asserting that `_persist_repair` is still called, so the change that would make bounding safe is
also the change that turns that test red and forces the question to be asked. Pinning a trap is
worth more than a patch that hides it.
