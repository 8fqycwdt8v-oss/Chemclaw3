# Foundation Assessment: does Chemclaw3 deliver the "Claude-Code-for-chemists" vision?

> **Scope of this document.** A deliberate, evidence-backed pressure-test of the *current*
> Chemclaw3 against the stated goal: *an intelligent assistant that chemical & analytical
> development colleagues use daily — like Claude Code for coding or Cowork for daily work —
> that autonomously uses tools and data to offload work and support scientific decisions.*
> Per the user's instruction, this is about **getting the foundation right**, not adding new
> Skills or MCP tools now. Method: full read of the code, `DECISIONS.md`, both design docs,
> the branch topology, plus web research on (a) what makes the Claude Code / Cowork experience
> work and (b) the 2024–2026 state of the art in agentic chemistry.

---

## 1. Verdict in one paragraph

Chemclaw3 is an **excellent capability spine with no assistant on top of it.** The compute,
knowledge, durability, and governance layers are real, tested, and unusually disciplined — and
several core choices (Temporal-for-durability, BoFire BO, the PR-gate, ORD ingestion) are
*exactly* what industry leaders are converging on. But the thing that makes Claude Code and
Cowork feel like autonomous collaborators — **a running agent with a front door, a durable
session, an autonomous plan/execute loop, live data connectivity, and identity** — is almost
entirely absent. Today the agent is **wired but never actually run** anywhere outside tests.
So: the vision is **not yet fulfilled**, but the hard, expensive half (correct scientific
capability + durable execution + auditability) is largely built. The missing half is mostly
*integration and interaction* foundation, plus two genuine scientific gaps (the **analytical**
development half, and **calibrated uncertainty**).

---

## 2. Scorecard — Chemclaw3 vs the 12 foundations of the Claude-Code/Cowork experience

Derived from Anthropic's engineering guidance on the agent harness, context engineering, Agent
Skills, MCP, Cowork governance, and background/scheduled execution.

| # | Foundation | Status | Evidence |
|---|---|---|---|
| 1 | **Agentic loop actually run** (gather→act→verify→repeat) | 🔴 **Absent in practice** | `build_agent` is never invoked outside `tests/`; no CLI/server/REPL; `agent.run` appears only in a docstring |
| 2 | **Explicit plan + persistent todo list** | 🟠 Experimental, unmerged | Only on the stale `agent-todo-planning` branch (`create_harness_agent`, `harness_enabled=False`) |
| 3 | **Autonomous multi-step tool use** (well-designed tools) | 🟡 Tools yes, loop no | ~13 clean tools + instructions describe a loop, but nothing drives it live |
| 4 | **Context engineering** (bounded, curated context) | 🟢 Strong | `gather_evidence` chunk cap, sized excerpts, offline distillation (D-024) |
| 5 | **Compaction** for long horizons | 🟢 Present (main) | Deterministic `CompactionProvider` (D-025) — *note: absent on the harness branch's base* |
| 6 | **Persistent project + session memory** | 🟡 Split | KG memory is strong; **conversation history is in-memory only** (`InMemoryHistoryProvider`) — a restart loses the session |
| 7 | **Just-in-time retrieval** | 🟢 Strong | Graph traversal, `find_notes`/`expand_note`, `gather_evidence` |
| 8 | **Subagents** (isolated parallel context) | 🔴 Absent | No fan-out; single agent only |
| 9 | **Skills = judgment** (SKILL.md, progressive disclosure) | 🟢 Strong | 13 skills, `SkillsProvider`, validated in CI |
| 10 | **MCP connectors to *real external* data** | 🟠 Internal only | MCP used only for in-house fingerprint search; no Benchling/LIMS/Slack/Drive/instruments |
| 11 | **HITL approvals, permissioning, identity, hooks** | 🟡 Gate yes, identity no | PR-gate + audit middleware are strong; **RBAC/identity is unbuilt** (Phase 6); `actor="unknown"` |
| 12 | **Background/scheduled/multi-surface + a front door** | 🟠 Backend yes, door no | Temporal background + Schedules are strong; **there is no chat/web/mobile/CLI surface at all** |

**Cluster read:** Memory/retrieval discipline (4,5,7,9) and the durable async backbone (12
backend) are genuinely strong. The **engine as-run (1,2,8)**, **external connectivity (10)**,
**identity (11)**, and above all **the front door (12 frontend)** are the weak wall.

---

## 3. What is genuinely strong — preserve, do not rebuild

These are ahead of most homegrown chemistry agents and align with 2026 best practice — keep them
central:

- **Real fast calculators** (GFN2-xTB via `tblite`, ESOL solubility, xTB-pKa), each **cached
  compute-once** with version-in-key (D-011/D-033). Tool-centric capability over model knowledge
  is the ChemCrow lesson, done right.
- **BoFire Bayesian optimization** behind a neutral adapter (D-012). BO is *the* landed
  process-dev technology (cf. Bayer/CIME4R) — this choice is validated.
- **The PR-gate** ("AI proposes, human signs off") — a genuine differentiator that maps directly
  onto the FDA/EMA Jan-2026 GxP guiding principles (human approval, ALCOA+ audit trail). Keep it
  the terminal gate for every agent-authored artifact.
- **Temporal durability + task-queue split + Schedules** — the recommended production pattern
  ("reasoning in the agent, durability in Temporal"); the LangChain/Temporal plugin endorses
  exactly this split.
- **Knowledge as versioned git-markdown + PR diffs** — auditable, human-editable source of truth
  (right for GxP).
- **The eval/metric layer, the calc cache, fingerprint search, memory layers, config discipline,
  and the `DECISIONS.md` log** — the engineering hygiene here is exemplary and rare.

---

## 4. The foundation decisions to make **first** (the forks everything else hangs on)

These are not tasks — they are choices that determine what the tasks even are. Make them before
building.

### D-A. MAF stays — deliver the Claude-Code-*like* experience natively on MAF **(resolved)**
- **Decision (user):** keep MAF as the orchestrator (D-013 stands). The goal is *a Claude-Code-like
  experience*, not Claude Code — built on MAF and tailored to pharma development. So the question is
  not "which framework" but "how do we get foundations 1/2/8/11/12 *on MAF*."
- **The MAF-native answer already exists in-repo:** the **MAF Agent Harness** (`TodoProvider` +
  `AgentModeProvider` via `create_harness_agent`) is exactly MAF's plan→approve→execute loop with a
  self-managed todo list — i.e. the Claude-Code-style engine, using MAF's own building blocks. This
  *is* the right direction; it just needs to be matured and reconciled (D-B).
- **The one real caveat to manage, not a reason to switch:** the harness is `[Experimental]` in
  `agent-framework-core`. Mitigation is already designed (the harness concept's H0 spike + a
  **fallback to the classic `Agent`** behind `harness_enabled`). Keep that fallback load-bearing so
  MAF-harness API churn can never break the product. Owning a thin, well-isolated harness wrapper on
  MAF is the accepted cost of the MAF choice.
- **Note (scope-limited):** the part of `architektur.md` that is out of step with reality is **not**
  MAF — it is the **Azure/Entra deployment assumption** (§6–§8), since the code runs on Anthropic
  Claude with no Azure. That is D-D's concern (deployment/identity substrate), and it does not touch
  the MAF orchestration decision.

### D-B. Adopt the plan/execute harness — and reconcile the divergent branches
- Foundations #1/#2 (the autonomous loop + visible plan) are the *soul* of the Claude-Code
  experience, and they exist **only** on `claude/agent-todo-planning-vmnwbo` — which forked from a
  **Phase-5-era base and never received Phase 5b + the entire deep-review hardening** (audit
  store, ID canonicalization, schedules, ELN cursor, git-submitter hardening, etc.). So **neither
  branch has both** the harness and the mature spine. This divergence is itself a foundation
  problem: decide the backbone, then **rebase the harness idea onto current `main`** (or re-do it
  natively if D-A picks the Agent SDK). Do not let the two lines drift further.

### D-C. Don't build sources yet — build the *generic attachment seam* **(scope set by user)**
- The vision needs many future data sources — **LIMS, MES, analytical instruments, live ELN,
  literature** — and the entire stack today is **synthesis/reaction-centric** (RDKit/xTB/ECFP4/ORD)
  with **no analytical data** and only three static ELN samples. But per the brief, **build no
  concrete source or analytics tool now.** The foundation task is instead the **generic data-source
  attachment seam**: one stable ingest+retrieve contract + a config-driven source registry, so any
  future source (LIMS/MES/analytical/ELN) attaches as *one thin adapter + one registry entry* with
  zero core change. The repo already has both half-contracts (`ElnAdapter`, `SourceRetriever`) — the
  work is to unify and harden them (plan Phase F7).
- **Deferred behind the seam (not now):** concrete connectors and their standards — **AnIML/Allotrope**
  (analytical data), **SiLA2/LAP** (instruments), **Benchling API/MCP** (ELN) — and analytical
  *models* (retention prediction, peak deconvolution, spectral/impurity ID). Each is one adapter/tool
  when that source is actually needed. **Do not invent a homegrown analytical schema; do not pick a
  standard until a real source is built.**

### D-D. Deployment & identity target **(resolved)**
- **Hosting:** **OpenShift**, with heavy compute via **Nextflow on HPC** and the LLM served by a
  **custom OpenLLM-like adapter**. So `architektur.md` §6's *Azure hosting* (AI Foundry/Container
  Apps) is what changes — to OpenShift.
- **Identity:** **Azure Entra ID is mandatory.** Users authenticate via Entra, and **every backend
  *workflow* is user-specific via Entra** — the requesting user's Entra identity is required,
  authorizing context on each durable run (a workflow with no Entra user is rejected). This makes
  `architektur.md` **§7/§8 ("Entra ID durchgängig") a live requirement, not aspirational** — only
  unbuilt, not wrong. Two carve-outs: **(a) raw LLM inference uses one generic API credential, not
  Entra** (the model call is not a user-scoped resource); **(b)** because the cluster is OpenShift
  not Azure, backend service identity uses **Entra Workload Identity Federation** (federated SA
  tokens, no stored secrets) instead of Managed Identity, with the §7 Temporal/HPC bridges unchanged.
- **Identity becomes load-bearing the moment autonomy is real** — an agent that can trigger
  expensive paths must know *who* asked and *whether they may*. See the plan's Phase F4.

---

## 5. MISSING (foundation-level, in priority order)

1. **A front door / runtime that actually runs the agent** — the single most important gap. A
   process that builds the agent, opens tool/MCP lifecycles, holds a session, and loops on user
   input. For a *non-developer domain expert*, the minimum viable surface is **chat (web), not a
   terminal**. Nothing in the repo does this today.
2. **Durable conversation session** — replace `InMemoryHistoryProvider` with a persistent store so
   a chemist's thread survives restarts and can be resumed (foundation #6). Keep durability rules
   intact (session state ≠ Temporal job state).
3. **Job → live-session push-back** — the `notify_agent` / plan-1.7 callback does not exist
   (`grep` finds zero). Long Temporal jobs return an id the user must *poll*; there is no way to
   wake a session when a DFT/BO run finishes. This is the seam that makes async feel alive, and it
   is the explicit prerequisite the harness concept's `awaiting` state depends on.
4. **Identity / RBAC (Phase 6)** — no user identity, all skills to all users, `actor="unknown"`.
   Needed before autonomy can safely trigger expensive/irreversible paths.
5. **Generic data-source attachment seam** — see D-C. *Not* concrete sources (no analytical/LIMS/MES/
   live-ELN connectors now), but the **one stable ingest+retrieve contract + source registry** so any
   future source attaches as a thin adapter with zero core change. Foundation-level because the seam
   shape must be right before sources land. (Today: only three static ELN samples; the seam makes the
   next source cheap.)
6. **Calibrated uncertainty / applicability domain on every prediction** — partially present
   (solubility/pKa report an uncertainty) but **not systematic and not conformal**. Research flags
   this as the field's weakest link and a trust differentiator; adopt **conformal prediction** as
   the uniform contract for predictor outputs. (This is a foundation *contract*, cheap to
   standardize now, expensive to retrofit later.)
7. **Subagents / fan-out** (foundation #8) — natural once the runtime exists; the deep-research
   harness already wants it. Deferrable, but design the runtime so it is possible.
8. **A scalable retrieval index atop the git-KG** — NetworkX-in-memory is fine for correctness and
   audit but does not scale to years of data or semantic recall. Add a **derived** vector/graph
   index (pgvector; or Graphiti-style time-bounded facts) as a *retrieval layer over* the git
   source of truth — not a replacement for it (consistent with D-004).

---

## 6. CHANGE (things that exist but should be reworked at the foundation)

- **Reconcile the harness branch with `main`** (D-B) — the highest-leverage change. One backbone,
  one branch, both the loop *and* the mature spine.
- **Rewrite `architektur.md` §6/§7/§8 to reflect reality** — the Azure/Entra/Copilot-Studio
  framing is now misleading (the system is Claude + local Temporal + no auth). Either commit to
  Azure or rewrite these sections around the actual target (D-D). Right now the primary design doc
  describes a system that does not exist.
- **`agent_model` default** — pin to a current, intended model deliberately (it is
  `claude-sonnet-5`); make the provider/model choice a conscious foundation setting tied to D-A.
- **Promote uncertainty from per-calculator to a cross-cutting contract** (D-C/§5.7) — like the
  calc cache generalized compute-once, generalize "every prediction carries calibrated
  uncertainty + applicability flag."
- **Session/actor threading** — `build_agent(actor=…, allowed_skills=…)` seams exist but are fed
  `"unknown"`/`None`; wire them to a real identity once D-D lands, rather than leaving dead seams.

---

## 7. OBSOLETE / over-built — reconsider or stop gold-plating

- **The Azure *hosting* framing** (`architektur.md` §6 — AI Foundry/Container Apps, Copilot Studio)
  is obsolete relative to the real target (**OpenShift + HPC/Nextflow + internal LLM adapter**) and
  should be rewritten. **§7/§8 (Entra ID) are NOT obsolete** — Entra identity is a mandatory,
  retained requirement; only the service-auth mechanism changes (Managed Identity → Entra Workload
  Identity Federation on OpenShift). Don't build Azure-*hosting* machinery, but do build Entra.
- **Beware polishing Phase-6 seams before a front door exists.** Several recent commits added
  identity/audit/approval *seams* (role-filtered skills, durable approval hold, audit store) that
  **cannot be exercised because no session runtime calls them.** This is disciplined seam-work, but
  the ordering is now inverted: the front door (foundation #1/#12) should come *before* more seam
  polishing, or the seams keep accreting untested-against-reality.
- **The `QM*/submit_to_hpc` naming** is a mock standing in for deferred DFT (correctly deferred,
  D-010). Not obsolete, but the naming implies a capability that is a `sleep`; keep the
  rename-to-generic-`CalculationWorkflow` intent (plan 1c.5) on the radar so the mock's HPC framing
  doesn't mislead new contributors.
- **`search_tools.py` in-process duplication** of the MCP path (kept only for the credential-free
  demo, D-029) — acceptable today, but revisit once a real runtime + demo strategy exists; it is a
  maintenance seam with one artificial caller.
- Nothing in the *compute/knowledge spine* is obsolete — resist the temptation to rebuild it.

---

## 8. Recommended foundation-first sequence (no new Skills/MCP tools)

Ordered so each step unlocks the next; explicitly excludes new capability skills/tools per the
brief.

1. **Confirm the MAF harness as the backbone (D-A, resolved) and decide D-D (deployment/identity
   target).** MAF stays; the remaining fork is the deployment/identity substrate. (Small, decisive;
   write the D-D ADR.)
2. **Stand up a real front door + run loop** — the chosen runtime actually builds and runs the
   agent behind a **chat surface a chemist can use**, with tool/MCP lifecycle handled. This alone
   moves foundations 1/3/12 from red to green.
3. **Reconcile the harness onto current `main`** (D-B): visible plan → approval → execute loop,
   with the runaway cap. Foundations 2 (and the plan-approval gate).
4. **Durable session store + job→session push-back** (missing §5.2/§5.3): sessions survive
   restarts; finished Temporal jobs wake the session. Closes the async-feels-alive loop.
5. **Identity: Entra everywhere** (D-D, §5.4): Entra OIDC at the front door; **every backend
   workflow user-specific via Entra** (required, authorizing input, reject-if-absent); workload
   identity federation for service auth; authorization *before* expensive triggers. Wire the seams.
6. **Generic data-source attachment seam** (D-C): unify `ElnAdapter` + `SourceRetriever` into one
   ingest+retrieve contract + a source registry, proved with a reference adapter. **No concrete
   source/schema** — LIMS/MES/ELN/analytical attach later, one adapter each.
7. **Uncertainty contract + derived retrieval index** (§5.6/§5.8): standardize calibrated
   uncertainty; add pgvector/Graphiti as a retrieval layer over the git-KG.
8. *Only then* return to capability breadth (concrete sources, retrosynthesis via ASKCOS/AiZynth,
   Chemprop predictors, analytical models) — as Skills/MCP/adapters over a solid foundation.

**Definition of "foundation done":** a chemist opens a chat surface, authenticates as themselves,
asks an open multi-step question, watches the agent post a plan, approves it, sees it execute with
tools + a long Temporal job, gets pushed the result when it lands, and every agent-written artifact
is a PR they sign — all surviving a restart. None of that is possible today; all of it is reachable
from the current spine.

---

## 9. Open decisions to confirm (owner: user)

1. **D-A** — *resolved:* keep MAF; deliver the experience via the MAF Agent Harness, with the
   classic-`Agent` fallback kept load-bearing against the harness's `[Experimental]` status.
2. **D-D** — *resolved:* hosting on **OpenShift** (HPC/Nextflow, internal OpenLLM-like adapter);
   identity via **Azure Entra ID**, mandatory for users and all backend components (§7/§8 retained;
   Managed Identity → Entra Workload Identity Federation is the only substrate change).
3. **D-C** — is the analytical-development half in v1 scope? (Recommendation: yes — it's the
   whitespace; at minimum commit the data-standard choice now.)
4. **Front-door surface** — web chat first? Slack? Both? (Recommendation: web chat for the
   non-developer domain expert.)
5. **Harness default** — once reconciled, does it become the default backbone, or stay opt-in?
