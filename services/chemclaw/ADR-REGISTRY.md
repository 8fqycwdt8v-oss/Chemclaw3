# ADR number registry

The allocation ledger for `DECISIONS.md`'s `D-NNN` numbers — one line per number, ascending.
`DECISIONS.md` holds the *reasoning*; this file exists so that "which numbers are taken?" is one
grep instead of a scan of a several-thousand-line document.

**Why it exists.** ADR numbers have collided repeatedly (D-105 and D-107 are both merge commits
whose job was partly to renumber somebody's ADRs; D-109 is a third instance). The cause is
structural: several branches run concurrently, each appends to the end of `DECISIONS.md`, and each
picks "the highest number I can see, plus one" — against its own branch, which cannot see the
others. They therefore pick the *same* number and conflict on the *same* line of the file.

**What this file does and does not fix.** It does not prevent collisions — two branches can still
append the same number here. What it changes is the cost: a collision becomes a one-line conflict
in a ledger, caught by a grep, instead of a ninety-line conflict buried inside a prose ADR where
the number is easy to miss. If collisions keep happening despite the procedure below, the next
step is to stop using a global sequence at all (date-plus-slug ids, e.g. `D-2026-07-27-harness`,
never collide) — that is a convention change worth making deliberately, not a silent drift.

**Allocating a number** — the procedure lives in `CLAUDE.md` ("Allocating an ADR number").
The short form, and the only correct source to allocate against is `origin/main`:

```sh
git fetch origin main
git show origin/main:services/chemclaw/ADR-REGISTRY.md | grep -oE '^\| D-[0-9]+' | sort -V | tail -1
```

Reserve the number here in your **first** commit on the branch, not at the end — an unreserved
number is one another session will take. Because that necessarily happens before the ADR exists,
write the row as:

```
| D-NNN | RESERVED — one line on what the decision will be about |
```

and replace the marker with the real title in the commit that adds the ADR. `RESERVED` rows are
exempt from the registry-matches-log check and *not* exempt from the duplicate check, which is
exactly the point: the number is claimed the moment it is pushed. A reservation that is never
written up leaves a gap, and a gap is harmless (`CLAUDE.md`, rule 4).

| ADR | Title |
|---|---|
| D-001 | Runtime is Python |
| D-002 | MAF for orchestration, Temporal for durability (kept separate) |
| D-003 | Agent Skills (SKILL.md) for capability integration |
| D-004 | Knowledge as a Markdown + Git graph (NetworkX), not a graph DB |
| D-005 | Human-in-the-loop via PR-gate |
| D-006 | One execution system: Temporal task queues, no pg-boss |
| D-007 | First milestone: MAF + Temporal spine (HPC mocked) |
| D-009 | Evaluation/metrics layer is first-class (Phase 2b) |
| D-008 | Deep-research/report harness: one core, pluggable retrievers |
| D-010 | HPC/DFT deferred; lead with fast local calculators (user decision) |
| D-011 | Results are persisted once, never recomputed (calculation store, first-class) |
| D-012 | BoFire is the Bayesian-optimization engine (no in-house BO), pulled forward |
| D-013 | MAF stays the orchestrator (reaffirmed vs. LangGraph) |
| D-014 | Eval cases live outside the knowledge graph (own versioned dir, not notes) |
| D-015 | Calculator contract now (`run_cached`), name-registry deferred |
| D-016 | MCP capability servers live in `mcp_servers/`, not `mcp/` |
| D-017 | One generic fingerprint store for molecules and reactions |
| D-018 | ELN ingestion: ORD-subset schema, one JSON adapter, LLM-per-field deferred |
| D-019 | Memory layers add no new infrastructure (note types + jobs only) |
| D-020 | Report harness reuses retrievers over existing data (no new store) |
| D-021 | Production-readiness review: one bad-data contract, hardened PR-gate |
| D-022 | ELN carries step-by-step recipes; a second adapter reads native ORD |
| D-023 | The agent is the research surface; integrations stay dumb |
| D-024 | The agent computes and designs experiments proactively, not just retrieves |
| D-025 | The agent keeps its chat thread within a token budget (MAF compaction) |
| D-026 | Observability floor: config-driven logging + one clear DB-connect failure |
| D-027 | GxP tool-audit middleware + opt-in OpenTelemetry (MAF out-of-the-box) |
| D-028 | Admin pluggability: ELN adapter registry, multi-dir skills, cache-trace log |
| D-029 | The agent consumes fingerprint search over MCP (config-driven servers) |
| D-030 | Deep-review hardening: bounded retries, git-ref-safe slugs, git timeouts, cache keys |
| D-031 | Deep-review deferred items worked off: fp-definition guard, ELN re-drive, KISS cleanups |
| D-032 | Durable async approval hold for captured user answers (Yes/No button seam) |
| D-033 | One canonical identity scheme: SHA-256 hashing + canonical SMILES in every key |
| D-034 | Review hardening: migration ledger, durable audit trail, injection framing, stmt timeout |
| D-035 | Missing runnable seams: schedules, ELN cursor persistence, approval + skill-role seams |
| D-036 | Review cleanup: dedupe, name-drift guard, neutral config names, doc refresh |
| D-037 | Tooling gaps: coverage, unified mypy scope, worker tests, preflight, skill-validate |
| D-038 | MAF Agent Harness as an optional third reasoning backbone |
| D-039 | F0: config-selected LLM provider seam (foundation-plan D-A1) |
| D-040 | F1: MAF Agent Harness is the autonomous plan/execute backbone (foundation D-020) |
| D-041 | F2: front-door run service (foundation-plan D-A2) |
| D-042 | F3: durable session + job→session push-back (foundation-plan D-A3) |
| D-043 | F4: Entra ID identity & RBAC — front-door OIDC + one authorization gate (D-A4) |
| D-044 | F4-T3: the core rule — user-triggered workflows are user-specific via `require_actor` |
| D-045 | F4-T2: workload identity federation (a pod mints its own token, no secret at rest) |
| D-046 | F4-T4: On-Behalf-Of exchange for user-scoped downstream (wired, dormant) |
| D-047 | F4-T6: the two non-Entra transport bridges carry identity as a claim |
| D-048 | F5: real HPC execution via a Nextflow launcher behind the QM activities (D-A5, D-A5a) |
| D-049 | F6: OpenShift delivery — one image, one config source, three plain secrets (D-A6, D-A6a) |
| D-050 | F7: the generic data-source seam (compose two half-contracts, don't merge them) |
| D-051 | Foundation review (F4–F7): adversarial review + fixes |
| D-052 | Role-scoped skill visibility (salvaged from the phase6-authz branch) |
| D-053 | Consolidate ELN source selection onto the F7 seam; memory honors `data_sources` (audit DUP-1) |
| D-054 | Per-source ELN cursors + a per-scope token lock (close the two F-review deferrals) |
| D-055 | GxP freshness + read-time provenance in graph retrieval (audit KM-6, KM-7) |
| D-056 | Retrieval-quality gate: a starter gold set + registered metrics (audit KM-13) |
| D-057 | Four more engine gaps closed (KM-5, KM-14 retrieval half, AG-14, AG-15) |
| D-058 | Prove the harness loop live; close the F3-T3 awaiting-todo deferral |
| D-059 | F10-E/B: per-task model routing + answer verification & confidence routing (D-A11) |
| D-060 | F10-C: per-tool authorization middleware (supersedes D-044 scope, D-A12) |
| D-061 | F10-G: audit hash-chain + bi-temporal note fields (D-A15) |
| D-062 | F10-A: hybrid retrieval — dense + lexical entry points, RRF fusion (D-A10) |
| D-063 | F10-F: classification metrics (P/R/F1) + eval drift detection (D-A14) |
| D-064 | F10-D: sub-agent orchestration via Temporal child workflows (D-A13) |
| D-065 | F10 post-implementation review cycle: verified fixes |
| D-066 | Resilience hardening: DB-query clamps, session reattach, turn/token budgets |
| D-067 | Fail-closed startup: unauthenticated + network-exposed refuses to boot |
| D-068 | Write tools are role-gated by default (DEFAULT_WRITE_TOOL_GATES) |
| D-069 | Submitter checkout ownership enforced with an OS-level advisory lock |
| D-070 | ELN sync cursor semantics: future-tolerance clamp, overlap window, chunked activities |
| D-071 | Deterministic config capture in workflows; idempotent session events |
| D-072 | CHECKMATE campaign 2026-07: adversarially-verified review, hardening, and refactor pass |
| D-073 | Final adversarial diff pass: campaign-introduced defects caught and fixed |
| D-074 | Compared against Google's Open Knowledge Format (OKF v0.1): design reaffirmed, two follow-ups queued |
| D-075 | Config-extensibility: `@tool` registry + `AgentProfile` seam (audit doc 10, items 2–3) |
| D-076 | Config-extensibility: `DataSourceSpec` discriminated union (audit doc 10, item 4) |
| D-077 | The turn stream emits its plan and its job launches (F2/F3 deferred item closed) |
| D-078 | Memory notes are retired when their cluster merges or shrinks |
| D-079 | Workflow versioning is a deploy checklist, not a CI guard |
| D-080 | Chemical safety: a deterministic, advisory structural screen (never a clearance) |
| D-081 | Config-extensibility: MCP transport union, skill manifest + enable-list, config idiom rule (audit doc 10, items 5–7) |
| D-082 | Graph-cache TTL (DA-5 / decision D-1) and the Helm render gate (DA-10 / decision D-2) |
| D-083 | F11 waves 0–3: closing the capability gaps (deployment, reachability, chemistry) |
| D-084 | F11 waves 3–4: operating the system; the knowledge model reasoning about itself |
| D-085 | F11 completion: the five items blocked on a decision or a prerequisite |
| D-086 | First reconciliation with `main` (PRs #17–#20): hazard screen, event sink, tool registry |
| D-087 | Second reconciliation with `main` (PR #21): the MCP transport union |
| D-088 | Third reconciliation with `main` (PR #23): ADR renumbering, and the chart's env parity guard |
| D-089 | No external sources; PDF/PPTX/DOCX/XLSX are in scope |
| D-090 | Reported-issue sweep: the azide the screener could not see, two missing session routes, and the note-repo footgun |
| D-091 | Restoring the tree the Replit restructure rewound |
| D-092 | Process/analytical-development capability research: quick wins, one durable big win, and what was rejected |
| D-093 | A raw exception in a fan-out child suspends as a task failure, not a workflow failure |
| D-094 | CI's `kg-validate` step needs a real (even empty) `knowledge` directory |
| D-095 | xTB capability seams (X1) and the properties the SCF already produced (X2) |
| D-096 | xTB descriptors as BO featurization (U1) |
| D-097 | The single point runs on a relaxed geometry, and the skill catalogue that found it |
| D-098 | X3/X4: geometries, free energies, the reaction composite, and durable routing |
| D-099 | Durable capabilities declare their own queue |
| D-100 | Sizing for real substrates: the workload is 200-800 Da |
| D-101 | X5/X6/X7: the binaries, and what they change |
| D-102 | X9 revisited: preconditioning the path the binary cannot take |
| D-103 | X8: the calculators as an MCP server, and the line identity draws |
| D-104 | X11: two molecules together, and the half of the amine problem that is refused |
| D-105 | Fourth reconciliation with `main` (PR #28): the restored tree meets the xTB layer |
| D-106 | Heavy review of the xTB layer: five defects the tests did not catch |
| D-107 | Fifth reconciliation with `main` (PR #31): a unit boundary and a sign, both silent |
| D-108 | One conformer ensemble, one reaction composite: the duplicates are removed |
| D-109 | Four fixes from the live e2e pass, and two root causes that were not what they looked like |
| D-110 | The connector seam: one way to add a tool, a skill, or an agentic workflow |
| D-111 | Stage C: the domain connectors, and two defects the migration surfaced |
| D-112 | `bo` as the reference connector-owned durable capability |
| D-113 | Stages D and E: profiles select an agent, templates fix a procedure |
| D-114 | Sixth reconciliation with `main`: the xTB layer meets the connector seam |
| D-115 | The two remaining Stage C items, answered: neither becomes a bundle |
| D-116 | Seventh reconciliation with `main` (PR #30): two capabilities the merge silently restored |
| D-117 | Consolidating the outstanding branches, and deleting what four generations of the design left behind |
| D-118 | One connector seam for MCP, Temporal and long-running HPC tools |
| D-119 | Production scale: the event loop, the connection pool, and a guard that switched itself off |
| D-120 | A data source becomes a manifest: the second config-side union replaced by a folder |
| D-121 | The front door as a multi-process service: pure-ASGI headers, a durable turn claim, a pool timeout that sheds |
| D-122 | The GxP audit trail defaults to durable, because opting in per call site did not work |
| D-123 | One agent per concurrent turn: a shared chat client corrupts streamed tool calls |
| D-124 | The artifact store: a calculation's by-products outlive its tempdir, and the cost policy the cache lacked |
| D-130 | Turn teardown runs in a cancelled task, so its cleanup has to be shielded to happen at all |
| D-131 | The connector health probe follows the address override, instead of probing the pod itself |
| D-132 | The Hessian is its own calculation: splitting the matrix from the thermochemistry computed over it |
| D-133 | A submission is a note and what it needs, so a computed result can cite the compound it is about |
| D-134 | Edges carry relations and their own validity, so the graph stops being a citation network |
| D-135 | A dataset may be vendored into the image at build time — the one amendment to D-089's scope |
| D-136 | The shipped defaults were never executed: three configurations that fail on first contact |
| D-137 | The plan the model could approve for itself: a pre-execution gate that is not a tool |
| D-138 | Fifty questions, asked live: the job surface was dead, the trace was blind, and a failed tool was silent |
| D-139 | Three silent failures: a degraded turn, a pooled calibration, and two counters wired to nothing |
| D-140 | A template's job step: resolved off the workflow thread, and finally able to fail |
| D-141 | Two facts that stopped at a process boundary: a session's profile, and the turn's correlation id |
| D-142 | A production value has to be executed, not type-checked — and two guards that were off in the one deployment that needed them |
| D-143 | Nobody was collecting the metrics, and the durable history is never compacted — one fixed, one where the obvious fix corrupts data |
