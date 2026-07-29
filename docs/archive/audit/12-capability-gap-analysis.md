# 12 — Capability Gap Analysis: what is missing that would benefit the infrastructure

**Repo:** `/home/user/Chemclaw3` @ `d77302e` · **Date:** 2026-07-25 · **Scope:** whole codebase
**Type:** completeness analysis (absences), not a defect audit. Nothing here is executed — every
item is a proposal.

---

## What this adds over the existing gap docs

`08-agentic-engine-gaps.md` (AG-*) and `09-knowledge-management-gaps.md` (KM-*) asked *"is the
capability present and wired?"* against a checklist of agentic-engine and knowledge-management
concerns. Both were thorough, and their remaining open items are still open. This pass asks a
different question — **"what does this system need that nobody has listed yet?"** — and sweeps the
whole tree rather than two named domains.

The result reframes the priority order. AG-*/KM-* concluded, correctly, that the *engine* is in good
shape and the residue is about operating it at scale. This pass finds that the sharpest gaps are not
in the engine at all — they are at the **seams around it**:

1. **Three built subsystems have no caller.** The Phase-5b report harness, the durable BO campaign,
   and the human side of the approval hold are all implemented, tested, and registered on a worker —
   and unreachable from any tool, route, schedule, or UI control.
2. **The Helm chart cannot run the knowledge layer.** No volume, no git-sync, no push credential.
   The system's core asset is baked-at-build and per-replica in the target deployment.
3. **The chemistry surface is thinner than the prompt claims.** No compound-name resolution, no
   hazard screen, no experiment date, no purity/impurity fields — while `_INSTRUCTIONS` advertises
   impurity answers and protocol design.

Those outrank every "add a capability" idea below, because they are capability the repo has
**already paid for and cannot use**.

**Reading convention.** Severity **Crit / High / Med / Low** is *value of closing it*, not defect
severity. Effort **S** (<1 day) / **M** (a few days) / **L** (week+). Every finding cites the file
that proves it, so any claim here can be falsified directly. Where an item overlaps an existing
AG-*/KM-*/`docs/planning/DEFERRED.md` entry, that is stated — restatement is deliberate only when this pass found
the trigger has since fired.

---

## A. Reachability — built capability with no way to invoke it

The repo's discipline is "no abstraction without a second real caller." These are the inverse
failure: a *complete implementation* with **zero** callers. That is dead weight that also reads as
finished, which is worse than absent — the backlog marks all three phases complete.

### RCH-1 — `DevelopmentReportWorkflow` has no trigger · **High** · **S**

Phase 5b's entire deliverable — source-agnostic report harness, per-section durable activities,
child-workflow fan-out (F10-D2), PR-gated report note citing every source — is registered on the
background worker (`workers/background_worker.py:52`) and referenced by nothing else. Grep for the
symbol outside tests returns the worker registration, `docs/planning/parity-plan.md`, and `docs/planning/BACKLOG.md`.
There is no agent tool, no HTTP route, no Temporal Schedule. The only way to start a development
report in a running deployment is the Temporal CLI.

**Shape of the fix.** One tool mirroring `submit_qm_job` exactly: `request_development_report(topic,
sections)` → `require_actor()` → deterministic workflow id → returns the id immediately; completion
arrives through the existing job→session push-back (F3-T3). ~40 lines against a fully-built backend.

### RCH-2 — `BoCampaignWorkflow` has no trigger · **High** · **S**

Same shape (`workers/background_worker.py:63`). `suggest_next_experiment` is the *inline, one-shot*
BoFire ask; the durable multi-round campaign — the thing that actually runs an optimization to
convergence with worker-restart durability — cannot be started from the product.

Sharper than RCH-1, because a skill actively points the agent at it:
`skills/experiment-design/SKILL.md:43` tells the agent that for iterative work "the durable
`BoCampaignWorkflow`" is the path. The agent is being instructed to reach for a capability that is
not in its tool list. See IDEA-7 — this is a class of defect, not a one-off.

**Shape of the fix.** `start_optimization_campaign(spec)` + `get_campaign_status(id)`, same seam as
the QM pair.

### RCH-3 — the human-in-the-loop approval hold is a dead end · **High** · **M**

`InteractionApprovalWorkflow` (D-032) is the durable Yes/No hold, and `agents/interaction_tools.py`
is documented as "the seam a chat UI hooks onto" (`:6-8`). Trace the seam to its consumer and there
isn't one:

- `start_approval` / `decide_approval` / `approval_status` are **not** in `_capability_tools()`
  (`agents/chemclaw_agent.py:222-241`).
- There is no HTTP route — the app has five (`/healthz`, `/readyz`, `POST /sessions`,
  `POST /sessions/{id}/messages`, `GET /sessions/{id}/events`).
- The web client renders `approval_request` as a **trace line** with no control:
  `service/static/app.js:58-59` → `add("trace", "⏸ approval requested: …")`.

So a hold, once opened, can only time out. This matters beyond the interaction note: F10-B's
confidence routing is specified to route low-confidence answers into *this* hold, and the harness's
`plan_only` approval is meant to key off the same seam — and the chart ships
`CHEMCLAW_HARNESS_AUTONOMY: "plan_only"` as the production default
(`deploy/helm/chemclaw/values.yaml`). The default deployment's default mode is one that asks for an
approval nobody can give.

**Shape of the fix.** `GET /approvals` + `POST /approvals/{id}/decision` (authenticated, owner-
scoped, delivering the existing Temporal signal) and two buttons in `app.js`. Deliberately *not* an
agent tool — the agent must not be able to approve itself.

### RCH-4 — the PR-gate queue has no in-product surface · **Med** · **S**

`propose_note` lands a note on a `note/<id>` branch and returns. Nothing lists pending proposals,
and the branch/PR URL never reaches the session that produced it. The GxP "AI proposes, human signs
off" line — the architecture's spine — exists only in a git host's UI, disconnected from the
conversation that created the proposal, so the reviewer has no context and the chemist gets no
confirmation their contribution landed.

**Shape of the fix.** Emit the branch ref as a session event on successful `propose_note` (the
mailbox and SSE channel already exist); optionally `GET /proposals` listing open `note/*` branches.

### RCH-5 — `PlanEvent` and `JobStartedEvent` are contracted, rendered, never emitted · **Med** · **S**

Both are in the typed union (`service/events.py:15,37`) and both are handled by the UI
(`app.js:44`), but `service/runner.py` emits neither — documented as deferred-within-F2 since the
front door was built. Combined with RCH-3, the production-default `plan_only` mode has *no* way to
show its plan and *no* way to approve it. Known-deferred; this pass reclassifies it as blocking,
because the deployment default now depends on it.

---

## B. Deployment truth — the chart cannot run the design

F6 is marked implemented with "offline-verified: YAML parse + brace-balance + Settings map." That
verification checks the chart is *well-formed*, not that it is *sufficient*. Three gaps survive it.

### DEP-1 — the knowledge graph is not deployable · **Crit** · **M**

Every reader resolves `settings.knowledge_dir` as a plain local filesystem path:
`agents/graph_tools.py:76,107`, `report/retrievers.py:68,187,217`, `report/vector_index.py:262`,
`agents/verifier.py:175`, `kg/validate.py:46`.

The Helm chart mounts **no volume for it**, sets **no `CHEMCLAW_KNOWLEDGE_DIR`**, and runs **no
git-sync**. The only volume anywhere in the chart is the Temporal mTLS secret
(`deployment-service.yaml:50-58`, `deployment-workers.yaml:36-40,79-83`). Consequences:

- In-cluster, the graph is whatever `knowledge/` was baked into the image at build time. **A merged
  note never reaches a running pod** — the PR-gate's whole point — until the next image build.
- `service.replicas: 2` scaling to 6 means each replica holds its own private copy.
- The same applies to the background worker, so memory synthesis reasons over a frozen corpus.

The read path is otherwise ready for this: `kg/graph.py`'s parsed-note cache is keyed on a
`(path, mtime_ns, size)` directory fingerprint, so it busts correctly the moment a sync writes.

**Shape of the fix.** A git-sync sidecar (or an init container + a periodic `background-jobs` pull
activity) into a shared volume, plus `CHEMCLAW_KNOWLEDGE_DIR` in `values.yaml`. Pairs with SCH-6.

### DEP-2 — the PR-gate write path has no credential in-cluster · **Crit** · **M**

`GitNoteSubmitter` requires a **dedicated clone** of the knowledge repo with push access — its
module docstring is emphatic (`kg/git_submitter.py:17`, `:65`, `:94`, `:109`) — and it runs
`fetch` / `push --force-with-lease` against a remote (`:199`, `:226`).

The chart declares exactly three plain secrets — `llmApiKey`, `hpcApiToken`, `postgresDsn` — plus
Temporal mTLS (`values.yaml`, `secrets.keys`). There is **no git token or SSH key, no
`CHEMCLAW_NOTE_REPO_DIR` value, and no clone step** in the image or entrypoint
(`deploy/entrypoint.sh`).

So in the target deployment every agent-authored note — job results, BO recommendations, campaign
narratives, playbooks, interaction notes, reports — fails at push. Combined with DEP-1, the
knowledge layer is non-functional in-cluster in **both** directions.

### DEP-3 — the MCP Deployments are default-on and cannot work · **High** · **S**

`deployment-mcp.yaml`'s own header says these pods should exist "only if a networked transport is
enabled … kept behind a flag so a stdio-only deployment does not create idle pods." The flag
defaults the other way: `values.yaml` sets `mcp.molfp.enabled: true` and `mcp.rxnfp.enabled: true`.

What those pods run is `python -m mcp_servers.molfp.server` (`deploy/entrypoint.sh`), which is
`FastMCP.run()` — **stdio transport** (`mcp_servers/molfp/server.py:50`). In a container with no
stdin, that reads EOF and exits. The Deployment has no Service, no port, no probes. Result: two
crash-looping pods per server, while the agent independently spawns its *own* stdio subprocess in
its own pod — which is where the capability actually comes from.

**Shape of the fix.** Immediately: default `enabled: false`. Properly: add the streamable-HTTP MCP
transport (TOOL-1), which is what this template was written in anticipation of.

### DEP-4 — no RED metrics, no `/metrics` · **Med** · **S**

Observability is structured logs plus opt-in OTel *traces*. There is no metrics surface anywhere
(`grep prometheus|/metrics|Counter|Histogram` over `service/`, `chemclaw/` finds only
`service/budget.py`'s internal counters). Nothing exports: turn rate/latency/error, shed turns
(503 from admission control), budget refusals (429), audit-sink failures (the known SEC-3
swallow-and-warn), MCP subprocess health, or Schedule staleness.

Concretely: the front door autoscales on `targetCPUUtilizationPercentage: 70`, which for an
SSE-streaming, LLM-latency-dominated service is close to a random signal — it will scale on token
decoding, not on queueing. Admission control (AG-15) sheds load correctly and **silently**.

---

## C. Scheduling and data lifecycle

`scripts/schedules.py` is clean, idempotent, and declarative (it even prunes). The gaps are in what
is *not* scheduled, and in how thin each Schedule's spec is.

### SCH-1 — no data retention anywhere in the system · **High** · **M**

There is no `DELETE`, no TTL, no retention window, and no retention config in any module or
migration (`grep retention|DELETE FROM|vacuum` over the tree: zero non-`_prune`-of-Schedules hits).
Every Postgres table grows without bound: `audit_events` (one row per tool call, forever),
`session_messages`, `session_events`, `calculation_results`, `note_index`, and both fingerprint
tables.

This is not merely a disk-cost item, which is why it ranks High:

- `audit_events` is **hash-chained** (`infra/sql/011_audit_hash_chain.sql`). Deleting old rows
  breaks the chain — the verifier reports the break as tampering. Retention here must be *designed*
  (segment-and-anchor, or archive-then-reseal), and designing it after ten million rows exist is
  strictly harder than designing it now.
- GxP retention is a *requirement*, not a cleanup task: "keep for N years, then dispose, provably."
  A system with no disposal story has an incomplete records story.

**Shape of the fix.** A `background-jobs` retention workflow in `planned_schedules()`, per-table
policy from config, and an explicit chain-preserving strategy for `audit_events`.

### SCH-2 — the derived note index is never refreshed · **High** · **S**

F10-A shipped `note_index` (`infra/sql/012`) plus `make reindex`, and its own backlog entry defers
"a scheduled `background-jobs` reindex activity." `OWNED_SCHEDULE_IDS` (`scripts/schedules.py:58`)
contains `eln-sync`, `campaign-synthesis`, `playbook-distillation`, `optimization-campaign`,
`eval-drift` — no reindex.

So under `retrieval_mode="hybrid"` the dense and lexical legs serve whatever the last manual
`make reindex` captured, against a graph that changes on every merge. This is worse than the legs
being absent: RRF fusion will confidently rank stale entries *alongside* live graph hits, with no
staleness signal, and KM-7 freshness enforcement (`Note.is_current`) can't help because the stale
rows never get re-read. The single cheapest high-value Schedule to add.

### SCH-3 — the Schedule spec is minimal · **Med** · **S**

`_build_schedule` (`scripts/schedules.py:91-100`) sets only `ScheduleIntervalSpec(every=…)`. Missing:

- **No overlap policy.** The default lets a run start while the previous is still going. The three
  memory jobs re-scan the *whole corpus* each run (`memory/jobs.py` builds from `all_records`), so
  as the corpus grows, self-overlap is the expected steady state, not an edge case.
- **No jitter.** All three memory jobs share `memory_synthesis_schedule_minutes`, so they fire
  simultaneously, each loading the full reaction set, on one background worker (`replicas: 1`).
- **No catchup window / pause switch / per-job enable** — except the one-off `eval_drift_enabled`
  special case, which shows the shape is wanted but wasn't generalized.

### SCH-4 — no schedule health surface · **Med** · **S**

Nothing reports last-run, last-success, or failure count per Schedule. An ELN sync that fails every
run (bad credential, moved export path) advances no cursor and raises no alarm — it surfaces weeks
later as "the agent doesn't seem to know about recent experiments," which is the hardest class of
bug to attribute. Pairs with DEP-4.

### SCH-5 — `make audit-verify` is manual-only · **Med** · **S**

A tamper-evident chain that is verified only when someone remembers to look detects tampering only
when someone remembers to look. It is a natural sixth planned Schedule, alerting through the
*must-deliver* notify seam already built for `eval-drift` (`workflows/eval_drift.py`).

### SCH-6 — everything is poll-on-a-timer; nothing is event-driven · **Low** · **M**

There is no inbound event path at all — no webhook route, no "a note merged" or "an ELN batch
landed" trigger. Freshness is bounded below by the slowest configured interval, everywhere. A single
authenticated `POST /events/knowledge-merged` would collapse SCH-2's staleness window to seconds,
give DEP-1's sync a trigger, and is the natural notification hook for RCH-4.

---

## D. Agent and turn lifecycle

### ~~AGT-1 — no turn cancellation~~ · **WITHDRAWN — verified false**

**Original claim:** that no `CancelledError` handling existed, so an abandoned turn held its
admission permit and never booked its tokens.

**Verified wrong during implementation.** The claim rested on a `grep` for `CancelledError` /
`is_disconnected` returning nothing, which was true but not load-bearing: the handling is
structural rather than by name. `sse-starlette` closes the streaming generator on disconnect, which
raises `GeneratorExit` at the suspended `yield`; the front door's `finally`
(`service/app.py`, `_turn_events`) releases the semaphore permit and discards the active-turn slot,
and `run_turn`'s own `finally` (`service/runner.py`) books the metered tokens against the budget.
`except Exception` deliberately does not swallow the `BaseException`, and no `await` sits in the
runner's `finally` (which would raise "async generator ignored GeneratorExit"). This was hardened
in `4bc9b04` ("cancellation-safe counters"); the analysis missed it.

Measured, not argued: `tests/test_turn_cancellation.py` drives a turn, abandons it after three
events, and asserts both that the permit and turn slot come back and that the ~30 metered tokens
are booked. Those tests are **kept** — nothing previously proved this behavior, so a plausible
future refactor (an `await` added to the runner's `finally`, an `except Exception` widened to
`BaseException`) would silently reintroduce exactly the leak this finding alleged.

What remains genuinely absent is smaller and not what was claimed: there is no *explicit* stop
control — a client can only abandon a turn by dropping the connection — and an abandoned turn is
not logged, so it is invisible in operations. Both are **Low**, and the second is subsumed by
DEP-4's metrics.

### AGT-2 — no mid-turn resume for durable jobs · **High** · **M**

The standing F1/F3 follow-up, restated because it is the system's defining interaction and it is
still split in half: a chemist asks for a calculation, the tool returns a job id, the turn *ends*,
and the result arrives as a push-back event picked up on the *next* turn. "Compute this, then reason
about the result" — the reason this stack has Temporal at all — cannot happen in one exchange.

Both halves of the machinery exist: the durable hold (D-032) and the awaiting-todo flip (D-058,
`agents/harness_todo.py`). What is missing is the runner awaiting them inside one streamed turn.

### AGT-3 — no file or attachment ingress · **Med** · **M**

There is no upload route and no non-text input path. A chemist cannot hand the agent a spectrum, a
CSV of runs, a vendor CoA, or a PDF SOP. The *only* way data enters the system is the scheduled ELN
sync. For a lab assistant this is the highest-frequency real request, and it is the natural first
consumer of the gated OCR/vision item in `docs/planning/parity-plan.md` — which is currently gated on "a real
scanned-notebook source attaches via the F7 seam," a trigger that can never fire while there is no
way to attach anything.

### AGT-4 — no per-user preference or personalization layer · **Med** · **S**

Every memory layer is corpus-level: `campaign`, `playbook`, `optimization-campaign`, `interaction`.
Nothing remembers *this chemist* — their project, their preferred solvent system or units, or that
they rejected an analogy last week. Entity and session identity both exist (`Principal.oid`,
`session_owners`), so the key is available; only the layer is missing. `interaction` notes are
adjacent but are global and PR-gated, which is right for shared knowledge and wrong for a personal
preference.

### AGT-5 — no clarifying-question protocol · **Med** · **M**

`_INSTRUCTIONS` tells the agent to "say plainly when the data is silent," but there is no event type
or contract letting the agent *ask and block*. An ambiguous question ("what did we get on the
Suzuki?") produces a best-guess sweep rather than "which of these four campaigns?" —
which is both worse and more expensive. `ApprovalRequestEvent` is structurally very close;
generalizing it into a question/answer round-trip also delivers RCH-3's surface work.

### AGT-6 — no structured final output · **Low** · **S**

Known-deferred ("until the first call site that needs a validated payload"). Noted only because
RCH-1 and RCH-2 create exactly that call site: a report or campaign request assembled from a chat
turn is a machine-consumed payload. The deferral's own trigger fires the moment those land.

---

## E. Tool integration

### TOOL-1 — MCP is stdio-only · **High** · **M**

`McpServerSpec` is `name` / `command` / `args` / `allowed_tools` (`chemclaw/config.py:35-46`) →
`MCPStdioTool`. There is no HTTP/SSE/streamable-HTTP MCP client anywhere. The agent therefore cannot
attach **any** server it does not spawn as a subprocess inside its own pod: no shared internal MCP
service, no third-party MCP server, no capability that scales independently of the front door, and
nothing written in another language.

D-029's promise — "adding a capability is a config entry" — is true only within one pod. Adding the
networked transport makes it true at org level and simultaneously resolves DEP-3.

### TOOL-2 — no chemical identity resolution · **High** · **M**

Every chemistry tool takes SMILES: `compute_xtb_energy(smiles)`, `predict_pka(smiles)`,
`predict_solubility(smiles)`, `find_similar_molecules(smiles)`, `find_substructure_matches(pattern)`.
Chemists write `Pd(dppf)Cl2`, `DIPEA`, `2-MeTHF`, `TBTU`. ELN free text writes the same. There is no
name→structure resolver, no CAS/registry lookup, and no synonym table anywhere in the repo.

The consequences compound rather than add:

- `find_notes` is literal substring matching (KM-4), so a query by trivial name misses a
  SMILES-keyed corpus entirely — not partially.
- The deferred "per-step species linking from free-text prose" is blocked *precisely* on this: its
  `docs/planning/DEFERRED.md` entry says linking needs "a name→SMILES tool," which does not exist.
- KNW-4's vocabulary problem and KNW-7's compound notes both need a canonical identity to hang on.

One `resolve_compound(name) -> smiles | candidates` tool — internal registry first, then an external
resolver behind the F7 source seam — is the **highest-leverage single tool addition in the repo**.

### TOOL-3 — no chemical safety / hazard capability · **High** · **M**

`docs/planning/BACKLOG.md:568` has carried "chemical/biological safety layer — distinct from Entra-ID/RBAC" as an
open user decision since the research review, with the note "decide scope before any capability
phase that could propose a hazardous route/procedure." That phase shipped: `_INSTRUCTIONS` directs
the agent to "help design new conditions/protocols," and `propose_knowledge_note` writes those
proposals into the knowledge graph for future reuse and cross-project distillation.

Today nothing in the stack can flag an energetic intermediate, an incompatible quench, a
peroxide-former held past its date, or a reagent/solvent pair with a documented runaway. The
proposal is reviewed by a human at the PR-gate — which is the right final control, and is *not* a
screen, because the reviewer sees a plausible-looking protocol with no hazard annotation.

This is the only gap in this document whose failure mode is physical rather than informational,
which is why it is High despite being a user-owned scoping decision.

**Shape of the fix.** Deterministic first, in the repo's own idiom: GHS/H-code lookup over resolved
components (needs TOOL-2), a binary incompatibility matrix, and a SMARTS set for energetic/
peroxide-forming motifs — exposed as a tool the protocol-design skill *must* call before proposing,
plus a registered `hazard` metric in `evals/` so the behavior is pinned. LLM judgment stays out of
the screen; it belongs in the skill.

### TOOL-4 — no stoichiometry / scale-up calculator · **Med** · **S**

The chemistry already exists in the repo but is not exposed to the agent: `eln/validate.py` computes
mass balance for *validation*, `evals/metrics.py` computes E-factor and PMI for *scoring*. The agent
has no tool to answer "what do I weigh out for 250 g at 1.2 equiv, and what's the projected
solvent volume" — the single most common bench question. Pure repackaging of existing code.

### TOOL-5 — no structure rendering · **Med** · **S**

The UI is text-only; a molecule or reaction answer is a SMILES string, which most chemists read
slowly and some not at all. RDKit is already a dependency and draws SVG in three lines. Lowest
effort-to-perceived-quality ratio in this document.

### TOOL-6 — no literature/patent retriever; its stated trigger has fired · **Med** · **M**

`docs/planning/DEFERRED.md:14` defers external retrievers with the trigger *"after Phase 5b core; add as one more
retriever behind the same interface."* Phase 5b is complete, F7 shipped the source registry, and
F10-A shipped RRF fusion over registry-declared retrievers. **The seam is finished and empty** — the
trigger has fired and the deferral was not re-examined.

Process R&D questions ("has anyone run this coupling on a chloro-pyridine") are literature questions
at least as often as internal-corpus questions, and the internal corpus of a new deployment is empty
by construction. Needs an API/source decision (the reason it is M, not S).

### TOOL-7 — no unit handling at the LLM boundary · **Low** · **S**

Amounts, temperatures, and times are bare floats with units encoded in field names
(`temperature_c`, `amount_mmol`, `mass_mg`, `time_h`). Internally consistent and fine. It means every
LLM-facing boundary re-derives units from a name, and any tool that returns a number to the model
returns it unlabelled.

---

## F. Knowledge model

### KNW-1 — the reaction schema has no date · **High** · **S**

`OrdReaction` (`eln/ord.py:88-102`) carries `reaction_id`, `inputs`, `outcomes`, `temperature_c`,
`time_h`, `yield_percent`, `provenance`, `project`, `steps`, `procedure_text` — and **no timestamp
for when the experiment was run.**

For the largest note class in the system, this removes the time axis entirely:

- Reaction evidence cannot be recency-ranked or time-scoped. The bi-temporal `valid_from`/`valid_to`
  added in F10-G2 apply to *notes*, and the ELN→note mapper has nothing to populate them from — so
  the bi-temporal capability is real and permanently unfed for reactions.
- "What did we know at time T" — F10-G2's stated purpose — is unanswerable for reactions.
- `memory/chains.py` orders a campaign by product→reactant identity alone. A cyclic chain is flagged
  `ordered=False` (CHECKMATE 5, F1) *because there is no time axis to fall back on* — with a date it
  would simply sort.

Every ELN records this field. Closing it is one optional field plus two adapter lines.

### KNW-2 — the prompt promises outputs the schema has no field for · **High** · **S**

`agents/chemclaw_agent.py:52` instructs the agent that its job is answering "about any output
(**yield, purity, impurities**)". The schema has `yield_percent` and nothing else — `grep -i
purity|impurit` across `eln/`, `kg/`, `memory/`, `agents/` returns that prompt line and one unrelated
docstring in `memory/optimization.py`. There is no purity field, no impurity list, and no analytical
result anywhere in the canonical record.

For a *process* R&D system this is the load-bearing gap of the two: impurity control, not yield, is
usually the point of late-stage development. The prompt writes a cheque the schema cannot cash, and
the honest fallback ("the data is silent") will fire on every purity question, forever, with the
chemist unable to tell a data gap from a capability gap.

**Shape of the fix.** `purity_percent: float | None` plus a small `impurities: list[Impurity]`
(`name | smiles | area_percent`) on the outcome, optional throughout so existing exports still
validate. The alternative — deleting the claim from `_INSTRUCTIONS` — is honest but wrong.

### KNW-3 — no negative-result capture · **Med** · **S**

Nothing in the schema or the memory layers marks an experiment as failed. Worse, the distillation is
structurally biased toward success: `find_playbook_candidates` distils patterns that **recur** across
projects, and failures do not recur — they get abandoned. "Don't try X, we did, it decomposed on
scale" is the most valuable and most systematically lost piece of process knowledge in any pharma
R&D organization, and this system currently has no place to put it.

**Shape of the fix.** `outcome_class: success | failure | inconclusive` on `OrdReaction` plus a
`failure-mode` note type; a distillation pass over failures is then symmetric with the playbook one.

### KNW-4 — no conditions vocabulary or entity resolution · **Med** · **M**

Solvents, bases, catalysts, and workup operations are free strings or raw SMILES with no controlled
vocabulary, no synonym set, and no canonical list. `DMF`, `N,N-dimethylformamide`, and
`CN(C)C=O` are three unrelated tokens to every lexical path. `optimization_campaign` grouping
therefore compares conditions that are textually different and chemically identical, splitting what
should be one campaign. Pairs with TOOL-2 — same underlying need, different consumer.

### KNW-5 — the graph has no analytics, and cannot answer "what don't we know" · **Med** · **M**

`kg/graph.py` exposes exactly `build_graph` and `neighborhood`. There is no centrality, no
clustering, no orphan detection beyond `kg-validate`'s link check, and — the real absence — **no gap
query**. The system cannot answer:

- which transformation do we have the least evidence for?
- which project has runs but no distilled playbook?
- which compound appears in many reactions but has no property data?

A knowledge graph that can only be walked outward from a hit answers *"what do we know about X"* and
never *"what don't we know."* The second question is the one that steers experimental design — and
it is precisely what `suggest_next_experiment` should be seeded from, instead of a decision space the
LLM assembles by hand from prose evidence.

This is the most *interesting* gap in the document: the data to answer it is already indexed, and
answering it turns the graph from a lookup store into a research instrument.

### KNW-6 — note types are an unvalidated open string · **Med** · **S**

`Note.type: str` with a slug-character check only (`kg/note.py:67`). The codebase mints `reaction`,
`campaign`, `playbook`, `optimization-campaign`, `interaction`, `report`, `job-result`,
`bo-candidate` from eight different call sites, with nothing enumerating or validating the set. A
typo mints a new type silently, and retrieval filters that key off type — the committed
`retrieval-coupling-playbook-filter` eval case does exactly this — then miss with no error.

### KNW-7 — compound notes; trigger arguably met · **Low** · **M**

`docs/planning/DEFERRED.md:30` defers compound notes until "compound notes exist (a later ELN step)" — a
self-referential trigger. Molecules are indexed by SMILES in a fingerprint table but are not graph
citizens, so a substructure hit **cannot cite anything** (the retriever's citation-honesty caveat,
CHECKMATE 5b F3), and the agent must bridge via `find_notes(smiles)` substring — the exact fragile
path KM-4 flags. TOOL-2 and KNW-4 both want a compound node to hang canonical identity on; when
either lands, this stops being deferrable.

---

## G. Free ideation — topics no plan document mentions

Speculative by design; each is grounded in parts the repo already has.

### IDEA-1 — standing queries and a "what changed" digest

The system is strictly pull. It has durable sessions, a session-event mailbox, per-user identity, and
fingerprint search — every ingredient for *push* — and uses none of them that way. A subscription
("tell me when a reaction like this lands in the ELN," "when a playbook touching my project merges")
is mostly assembly: a stored query, a Schedule, and the existing push-back channel. It is also the
difference between a tool people remember to open and one that earns attention.

### IDEA-2 — reconcile predicted against actual, continuously

The stack predicts (xTB, pKa, solubility, BO surrogates) and, separately, ingests what actually
happened (ELN). Nothing closes the loop. `prediction_error` exists in `evals/metrics.py` but scores
against a *held-out reference*, not against reality as it arrives.

Recording each prediction with its later observed value would yield live calibration per calculator —
which is exactly what "how far to trust it" means, and which `skills/calculation-selection/SKILL.md`
currently answers in prose. It would also give the tool-utility A/B (`evals/ab.py`) a real signal
instead of a synthetic one.

### IDEA-3 — greenness and cost as objectives, not just metrics

E-factor and PMI are computed as eval metrics only. They are not available to the agent as a tool and
not registered as BO objectives (`bo/objectives.py` registers `solubility_max`). "Find conditions with
comparable yield and half the PMI" is a mainstream process-chemistry question, and this repo has every
piece needed to answer it with none of them connected. Registering a PMI objective is a handful of
lines against `bo/objectives.py`.

### IDEA-4 — an explicit dry-run / what-if mode

Every expensive path is idempotent and cached, but there is no way to ask "what *would* you do, and
what would it cost" without doing it. For a system whose production default autonomy is `plan_only`,
a dry-run that returns the tool plan plus an estimated cost while executing nothing is both a natural
product primitive and a cheap safety valve — and it is the obvious consumer of the cost accounting
AG-11 is asking for.

### IDEA-5 — provenance-weighted trust in the fusion

`Note.confidence` exists and is now read (KM-5); `created_by` separates human from agent. But nothing
weighs a *source tier*: a validated internal ELN entry, an agent-distilled playbook, and (once
TOOL-6 lands) a literature analogy all rank identically once RRF fuses them. RRF is deliberately
score-agnostic, which is right for combining heterogeneous rankers and wrong for combining
heterogeneous *evidence classes*. A source-tier weight in the fusion is a small change with a large
effect — and it is the honest mechanical expression of the architecture's own "keep evidenced history
separate from transferred analogy" rule, which today is enforced only by asking the LLM nicely.

### IDEA-6 — corpus onboarding / backfill

The only ingestion path is the incremental, cursored ELN sync. A real deployment arrives with a decade
of existing reports, SOPs, and filings, and its first question is "make our existing documents
answerable." Nothing drives a bulk backfill, and the day-one experience of a correctly-installed
Chemclaw is an empty graph. The F7 seam, the PR-gate, and the report harness can all serve this; what
is missing is a batch driver and a triage flow for what a human must review.

### IDEA-7 — contract-test the prose surface against the code surface

Two findings above are the same defect in different files: `experiment-design/SKILL.md:43` points the
agent at `BoCampaignWorkflow`, which no tool exposes (RCH-2); `_INSTRUCTIONS` promises impurity
answers no field can supply (KNW-2). Both are **prose that names capability the code does not have** —
invisible to `mypy --strict`, invisible to the test suite, and invisible to `make skill-validate`,
which checks frontmatter only.

A validator that extracts tool and symbol mentions from `skills/*/SKILL.md` and `_INSTRUCTIONS` and
asserts each names a registered tool would have caught both, in CI, for maybe fifty lines. It is also
the *deterministic half* of AG-13 (the agent-behavior eval deferred as needing a live LLM) — this half
needs no model at all, and AG-13's deferral does not cover it.

---

## Sequencing

**Wave 0 — a deployment that works at all.** DEP-1, DEP-2, DEP-3, SCH-2, RCH-3.
Without these the target deployment cannot read the knowledge graph, cannot write to it, crash-loops
two pods by default, serves stale hybrid retrieval, and asks for approvals nobody can give. Nothing
below matters until this wave is closed.

**Wave 1 — reach the capability already built.** RCH-1, RCH-2, RCH-5, RCH-4. (AGT-1 was withdrawn
as a false finding — see above.) Four small tools/routes unlock two complete subsystems. Highest
value-per-line in the document by a wide margin.

**Wave 2 — the chemistry the prompt already promises.** KNW-2, KNW-1, TOOL-2, TOOL-3, TOOL-4, TOOL-5.
Two schema fields, a resolver, a hazard screen, and two repackagings. TOOL-3's *scope* is a user
decision (it has been open in `docs/planning/BACKLOG.md` since the research review) and should be taken before the
protocol-design capability is exercised in anger — the deterministic screen above is a proposal, not
an assumption.

**Wave 3 — operate it.** SCH-1, SCH-3, SCH-4, SCH-5, DEP-4, AGT-2.
Retention (design it before the tables are large and the chain is long), schedule hygiene and health,
a metrics surface, and the mid-turn resume that makes the durable-job interaction whole.

**Wave 4 — depth.** KNW-3, KNW-4, KNW-5, KNW-6, TOOL-1, TOOL-6, AGT-3, AGT-4, AGT-5, SCH-6, KNW-7,
and the IDEA-* set. KNW-5 (graph analytics / gap queries) and IDEA-2 (live calibration) are the two
with the most upside; IDEA-7 is S-effort and could be pulled into Wave 1 on its own merits.

**Cheapest five, independent of wave:** DEP-3 (flip a default), SCH-2 (one Schedule entry), RCH-1 and
RCH-2 (one tool each), KNW-1 (one field). All S, all unblocking something disproportionate.

---

## Deliberately not flagged

These were examined and are **correctly** deferred or out of scope; listing them so this document is
not read as contradicting `docs/planning/DEFERRED.md`:

- **HPC/DFT real integration, Postgres RLS graph mirror, `knowledge/` as its own repo, the Snowflake
  connector, live Entra/Temporal/OpenShift edges** — all gated on infrastructure that does not exist
  in this environment. Real, listed, correctly waiting.
- **Sub-quadratic playbook clustering, per-key in-flight calc dedup, `within=` id-array scaling,
  substructure pattern-fingerprint prefilter** — all gated on a corpus scale not yet reached, each
  with a numeric trigger. Correct as written.
- **Conversational multi-agent mesh** — a single agent plus role-scoped skills is the KISS answer, and
  the trigger in `docs/planning/parity-plan.md` is a good one. Building it now would be the one-caller
  abstraction the repo's own rules forbid.
- **GAMP 5 / 21 CFR Part 11 artifacts** — process deliverables, QA-owned, not code. The repo's job is
  emitting the substrate, which it does.
- **LLM summarization of compacted history** — declined for a documented injection-risk reason that
  still holds.
- **AG-13 agent-behavior eval** — genuinely blocked on a live LLM endpoint. But see IDEA-7: its
  deterministic half is not blocked and is not covered by the deferral.

Two deferrals **were** re-examined and are proposed for reopening because their stated triggers have
fired: **TOOL-6** (external retrievers — "after Phase 5b core," which is done and the seam is empty)
and **KNW-7** (compound notes — a self-referential trigger that TOOL-2 or KNW-4 would satisfy).

---

## Summary — the five that matter most

1. **The knowledge layer is not deployable** (DEP-1 + DEP-2). No volume, no git-sync, no push
   credential. In-cluster the graph is read-only-at-build and write-broken — the core asset of the
   architecture, non-functional in both directions, behind a chart that passes its own verification
   because that verification checks well-formedness, not sufficiency.
2. **Three finished subsystems have no caller** (RCH-1, RCH-2, RCH-3). The report harness, the durable
   BO campaign, and the human side of the approval hold are all built, tested, registered — and
   unreachable. One skill already points the agent at one of them.
3. **The prompt promises chemistry the schema cannot supply** (KNW-2, KNW-1). Impurities and purity
   are advertised with no field; reactions carry no date, which silently disables the bi-temporal
   capability F10-G2 just added and forces the memory layer to guess at ordering.
4. **No identity resolution, and no hazard screen** (TOOL-2, TOOL-3). Every tool speaks SMILES while
   every human speaks names — and an agent explicitly instructed to design protocols has nothing
   between its proposal and the knowledge graph but a human reading prose.
5. **Nothing is ever deleted, and the derived index is never refreshed** (SCH-1, SCH-2). Retention
   must be designed *around* the audit hash chain, which only gets harder with every row; and hybrid
   retrieval currently ranks a stale index confidently alongside live graph hits.
