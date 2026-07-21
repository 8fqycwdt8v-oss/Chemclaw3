# Foundation Plan: from capability spine to a MAF-based pharma-dev assistant

> **Companion to** `docs/foundation-assessment.md` (the *what/why*). This document is the
> *how* — an extensive, staged, acceptance-gated plan that resolves every issue the assessment
> identified, on the **real target stack** and on **MAF** (D-013 stands). It follows the
> conventions of `docs/implementation-plan.md`: small individually-acceptable steps, a
> **CHECKMATE** (G1–G7) after each phase, config-not-magic-numbers, ADR per decision,
> `make lint type test` green as a gate.
>
> **Goal restated (user):** a Claude-Code-*like experience* — an autonomous, tool-using,
> plan/execute assistant — **built on MAF** and **tailored to pharmaceutical process &
> analytical development**. Not a Claude Code clone; the MAF Agent Harness is the engine.
>
> **Scope guardrail (user brief):** foundation only. This plan adds **no new capability Skills/MCP
> tools and no concrete data sources or analytics tools** (no LIMS/MES/ELN/analytical connectors, no
> analytical/retro/prediction models). It *does* build the **generic seams** those attach to later —
> above all the data-source attachment seam (F7) — so adding a source or tool afterwards is a thin
> adapter, not a core change.

## 0. Target-environment model (the substrate this plan builds for)

Confirmed deployment context — the whole plan is shaped by it:

| Concern | Reality | Consequence for the plan |
|---|---|---|
| **Runtime platform** | **OpenShift** (Red Hat Kubernetes) | Containers, Deployments/Routes/Secrets. Only the *hosting* moves off Azure — see the identity row: Entra stays. `architektur.md` §6 (Azure hosting) is rewritten for OpenShift; **§7/§8 (Entra) are retained requirements** (F9). |
| **Identity (mandatory)** | **Azure Entra ID** for users; **every backend workflow is user-specific via Entra** | User auth = OIDC with Entra as IdP. The authenticated user's Entra identity is **required, authorizing context on every backend workflow** (Temporal jobs, syncs, reports) — a run is always attributable to and authorized for a specific Entra user. This makes `architektur.md` §7 a live requirement. **Exception:** raw LLM inference uses one generic API credential, not Entra (see invariants). Entra identity *on OpenShift* uses workload identity federation, plus the §7 bridges where a component can't speak Entra natively (Temporal, HPC). See F4. |
| **Heavy/long compute** | **HPC running Nextflow** pipelines | The mocked `submit_to_hpc` becomes a **Nextflow launch+poll** adapter (F5). D-010's "defer HPC until access exists" trigger is now **met**. |
| **LLM** | **custom OpenLLM-like adapter** (self-hosted, OpenAI-compatible endpoint) | The agent's chat client must be **decoupled from Anthropic** and pointed at the internal endpoint (F0). Tool-calling reliability of the internal model is the #1 project risk. |

> **Interpretation to confirm:** I read this as (a) the LLM is served by your internal
> OpenLLM-like OpenAI-compatible endpoint that the MAF agent calls, and (b) heavy scientific
> workflows (QM/DFT and pipelines) run as **Nextflow** jobs on the HPC, launched durably via
> Temporal. If instead the *Nextflow workflows themselves* are the only LLM consumer, say so —
> F0 and F5 swap emphasis.

**New deployment invariants (added to the plan's existing principles):**
- **One provider seam.** The LLM provider is a single config-selected adapter; no client class is
  imported at a call site (KISS/DRY, mirrors the ELN adapter registry D-028).
- **HPC specifics live in one module** (`workflows/activities.py`), exactly as its docstring
  already promises — the workflow and agent never learn what a Nextflow launch looks like.
- **The internal CA/TLS + tokens come from OpenShift secrets** through the one pydantic config.
- **Durability stays in Temporal; identity is a claim, not transport** (unchanged from §7/§8).
- **User identity is Entra, and every backend *workflow* is user-specific via Entra.** Users
  authenticate to the front door via Entra OIDC; the authenticated user's Entra identity
  (`oid`/`upn` + roles) is **mandatory context on every backend workflow** — it authorizes the run
  (may this user trigger this path?) and attributes it (who launched this Nextflow/BO job), not just
  as audit but as a required input. A workflow with no Entra user is rejected. See F4.
- **LLM calls are the one Entra exception.** The internal OpenLLM-like endpoint is reached with a
  **single generic API credential** (a config secret), **not** per-user Entra and not workload
  identity — the model call is not a user-scoped resource access. Everything *around* the call
  (which user's turn, which authorized todo) is still Entra-scoped; only the raw inference credential
  is generic.
- **Other backend→backend calls** (ELN/LIMS, internal APIs) carry Entra: OBO for user-scoped data,
  workload identity for service-scoped resources, with the §7 **bridges** only where a component
  cannot validate an Entra JWT (Temporal service auth, HPC).

---

## Phase F0 — LLM provider seam + tool-calling spike ⭐ **blocks everything**

**Why first:** the agent literally cannot run in your environment until its chat client talks to
the internal OpenLLM-like endpoint, and the *entire* Claude-Code-like experience (harness, tool
loop, plan/execute) depends on whether the internal model does MAF function-calling reliably.

- **F0.1 Provider adapter behind config.** Generalize `_default_chat_client`
  (`agents/chemclaw_agent.py`) into a provider switch: `llm_provider ∈ {openai_compatible,
  anthropic, …}`, with `llm_base_url`, `llm_model`, `llm_api_key`/token, `llm_tls_ca_bundle`,
  `llm_timeout_seconds`, `llm_max_retries`. For an OpenLLM-style OpenAI-compatible server, use
  MAF's OpenAI-compatible chat client pointed at `llm_base_url`; keep Anthropic as a secondary
  option for local dev. **No provider class imported outside the adapter.** The internal LLM endpoint
  is reached with a **single generic API credential** (`llm_api_key`, a config secret) — **not**
  Entra and not workload identity (the model call is not a user-scoped resource; §0). Entra scoping
  applies to *who* is taking the turn and *which authorized todo* runs, handled in F4, not to the
  raw inference credential.
- **F0.2 Tool-calling capability spike (the H0 of this plan).** Before building on it, prove the
  internal model can (a) select and call MAF function tools, (b) drive the **harness todo tools**
  (`add/complete/list todo`) and the plan/execute mode transition, (c) return the structured
  outputs the agent needs. Produce a short spike report: pass, or the specific weakness. If tool
  use is weak: constrained/grammar decoding, a tool-call-format shim, few-shot tool exemplars, or
  a stronger model reserved for the *planning* turns while a cheaper model executes.
- **F0.3 Streaming + params.** Wire token streaming (needed by the front door, F2), temperature/
  max-tokens, and stop conditions from config; confirm the endpoint's streaming shape.

> **CHECKMATE F0** (G1–G7 + spike): Does `agent.run` complete end-to-end against the internal
> endpoint with ≥1 real tool call? Is switching provider a single config change (no hardcoded
> Anthropic)? Is the tool-calling spike **documented pass/fail**, with a mitigation if weak? Are
> endpoint/token/CA all config, sourced from env? ADR: **D-A1 internal LLM adapter**.

---

## Phase F1 — Harness backbone, reconciled onto `main`

**Why:** foundations #1/#2 (autonomous loop + visible plan) are the experience's core and exist
only on the stale `agent-todo-planning` branch. Neither branch has both the harness **and** the
Phase-5b/hardening spine. Reconcile once, deliberately.

- **F1.1 Rebase the harness change onto current `main`.** Port the three config fields
  (`harness_enabled`, `harness_autonomy`, `harness_max_loop_iterations`), the `create_harness_agent`
  wiring with generic batteries **off**, the `todos_remaining` execute-loop, and the classic-`Agent`
  **fallback**. Take the harness *idea*, not the branch's older, thinner `build_agent`.
- **F1.2 Re-unify the tool/skill set.** The harness branch dropped `gather_evidence`, the MCP
  fingerprint search, `suggest_next_experiment`, `record_confirmed_answer`, the **audit
  middleware**, role-filtered skills, and **compaction** only because it forked pre-5b — keep all
  of them wired on the harness agent (they are current-`main` foundations, not clutter).
- **F1.3 Plan → approve → execute.** `harness_autonomy=plan_only` (interactive) is the safe default
  for pharma; `execute` adds the capped completion loop. The **plan-approval step is the pre-
  execution GxP gate** (complements the PR-gate's post-production gate). Keep the runaway cap.

> **CHECKMATE F1** (G1–G7): Does the harness run over the **full** current tool+skill set (not the
> reduced branch set)? Is plan→approve→execute demonstrated behind `harness_enabled`? Is the
> classic fallback intact and tested? `make lint type test` green. ADR: **D-020 finalized** (harness
> is the backbone, fallback load-bearing against `[Experimental]` API).

---

## Phase F2 — Front door + run harness (make the agent actually run)

**Why:** the decisive gap — today the agent is built only in tests. A **non-developer chemist**
needs a browser chat surface, not a terminal.

- **F2.1 Run-loop service.** A small ASGI service (FastAPI) that: builds the agent per session,
  **opens the MCP tool lifecycle** (`async with *agent.mcp_tools`), runs a turn, streams the
  response, and manages the session lifecycle. This is the missing caller the agent docstring
  describes.
- **F2.2 Chat surface.** Decision (F2 ADR): (a) **thin built-in** — FastAPI + a minimal chat UI /
  SSE endpoint the corporate portal embeds; or (b) **adopt an OpenAI-compatible chat UI** (e.g. an
  Open-WebUI-style front) in front of a compatibility endpoint. *Recommendation:* thin built-in
  web chat — full control over plan display, approvals, citations, and the PR-gate affordance,
  which a generic chat UI can't render. Mobile/Slack are later surfaces behind the same service.
- **F2.3 Turn UX for the experience.** Render the **plan/todo list**, tool-call trace, cited note
  ids, "job started (id …)" for async work, and the **[Yes]/[No] approval** affordances (the
  interaction-approval and plan-approval seams). Streaming responses.

> **CHECKMATE F2** (G1–G7): A chemist opens a browser, asks a multi-step question, watches a plan +
> tool use, gets a cited answer — running in a container against the internal LLM. Is the MCP
> lifecycle handled once in the service (not leaked per tool)? ADR: **D-A2 front-door service**.

---

## Phase F3 — Durable session + job → session push-back

**Why:** foundations #6 and the async-feels-alive loop. Sessions must survive pod restarts, and a
finished Nextflow/BO job must **wake the session** instead of forcing the user to poll.

- **F3.1 Persistent session store.** Replace `InMemoryHistoryProvider` with a Postgres-backed
  history/session provider (session id per user+thread, resumable). **Session state ≠ Temporal job
  state** — the layer rule holds (D-002/D-025); compaction still applies. (Redis is an alternative
  for the hot session cache; Postgres for durability — decide in the ADR.)
- **F3.2 The `notify_agent` / plan-1.7 callback (finally built).** On Temporal job completion, wake
  the owning session: the completing workflow signals a completion event the front-door service
  subscribes to (or writes a `session_events` row the service tails); the service appends the
  result to the session and flips the **`awaiting` todo → `completed`** (harness §4). No busy-wait.
- **F3.3 Awaiting state end-to-end.** A todo that launched a Nextflow/BO job is `awaiting(job_id)`;
  the callback completes it and the harness loop resumes with the now-unblocked follow-up todos.

> **CHECKMATE F3** (G1–G7): Does a session survive a front-door restart and resume? Does a long
> job's result **appear in the session on completion** with no polling? Is `awaiting→completed`
> shown? Durability stays in Temporal (no new durable store for jobs). ADR: **D-A3 session +
> callback**.

---

## Phase F4 — Entra ID identity & RBAC, system-wide (mandatory)

**Why:** identity via **Azure Entra ID** is a hard requirement — for **users and every backend
component** — and it becomes load-bearing the moment the harness can autonomously trigger expensive
HPC/BO paths ("who asked", "may they"). This phase makes `architektur.md` §7/§8 real, with one
change from the original: the hosting is **OpenShift, not Azure-native**, so service identity comes
from **Entra Workload Identity Federation** instead of Azure Managed Identity. The §7 maturity table
maps directly onto the work below.

- **F4.1 User auth at the front door (Entra OIDC).** The front-door service is an **Entra app
  registration**; users sign in with Entra (M365 SSO). Validate the JWT against the tenant JWKS,
  **check the audience** (confused-deputy risk, §7 — the service is both OAuth client and resource),
  and extract `oid`/`upn` + app-roles/groups.
- **F4.2 Backend service-to-service auth via Entra (workload identity federation).** Every backend
  pod (front door, `hpc-worker`, `background-worker`, MCP servers) federates its **OpenShift
  service-account token to an Entra app registration** → short-lived Entra tokens, **no stored
  secrets**. Each component authenticates to every Entra-protected dependency (the internal-LLM
  adapter endpoint, Graph, internal APIs) with its own Entra identity. Where a dependency isn't
  Entra-native, use the §7 bridge (next two items).
- **F4.3 Downstream data access as the user (OBO).** ELN/LIMS (Benchling) and any per-user resource
  are called with an **On-Behalf-Of** Entra token so access is the chemist's, not a generic service
  principal's — the data-governance requirement from §7.4.
- **F4.4 Every backend workflow is user-specific via Entra (mandatory).** The authenticated user's
  Entra `oid`/`upn` (+ roles) is a **required field on every workflow input** (`QMJobInput` already
  carries `requested_by` — make it the Entra `oid`, non-optional, and add it to the BO/ELN/report/
  memory workflow inputs). The worker **authorizes the run against that identity** (F4.5) and
  **rejects a workflow with no Entra user** — no anonymous background execution. This is stronger
  than audit: it is an access-control precondition on the durable path, so an autonomously-planned
  todo can't launch an expensive job outside the requesting user's entitlements.
- **F4.5 The two non-Entra bridges (§7 — unchanged in principle).**
  - **Temporal** has no native Entra token auth: workers/clients authenticate by **mTLS/API-key**;
    the user's Entra identity rides **inside the workflow payload** (F4.4) — identity as data, not
    transport (§7.2) — so both authorization and "who launched this Nextflow job" hold on the
    durable path.
  - **HPC/Nextflow** speaks no Entra: a thin **bridging service** maps the Entra identity → the HPC/
    Nextflow service identity and **logs every mapping** — the single point that knows both worlds
    (§7.3). (Seqera Platform can also do Entra SSO for *human* console access.)
- **F4.6 Authorization at one point + wire the existing seams.** Thread the real `actor` into the
  audit trail (`make_audit_middleware`, D-034) and the durable `audit_events` sink; scope advertised
  skills by Entra app-role (`RoleFilteredSkillsSource`, D-035); **authorize expensive triggers**
  (Nextflow submit, BO campaign) against the Entra role/group **before** the harness executes the
  todo — the single fachliche authorization point (§8), not scattered across layers.

> **CHECKMATE F4** (G1–G7 + security review): Is **every backend workflow user-specific via Entra**
> (a run with no Entra user is rejected, authorized against that identity)? Can an unauthorized user
> not trigger an expensive path even in autonomous execute mode? Does the identity show end to end,
> including the Temporal payload? Is authorization at **one** point? (The generic LLM API credential
> is the one documented Entra exception, plus the Temporal/HPC transport bridges.) ADR:
> **D-A4 Entra ID identity/RBAC on OpenShift** (realizes §7/§8; Managed Identity → Workload Identity
> Federation is the only substrate change).

---

## Phase F5 — HPC/Nextflow real execution path

**Why:** turns the mock spine into real heavy compute. D-010's deferral trigger ("HPC access
exists") is now met. Everything is already shaped for this — only `workflows/activities.py` changes.

- **F5.1 Nextflow launch adapter.** Replace `submit_to_hpc`/`poll_hpc_status` with a launcher:
  submit a pipeline run, poll status with `activity.heartbeat()` against preemption, fetch result
  artifacts. Interface decision (F5 ADR): **Seqera Platform (Tower) API** (cleanest, has run
  status/REST), **`nextflow` CLI over SSH** to a login node, or an internal **REST launcher**.
  Keep the `HpcJobHandle` seam; the workflow is untouched.
- **F5.2 Real pipeline + parse.** Wrap the real QM/DFT (or other) computation as a **Nextflow
  pipeline**; parse outputs (e.g. cclib) → typed `QMJobResult` → **calculation store** (compute-once)
  → **PR-gated note** (unchanged paths). Generalize `QMJobWorkflow`→`CalculationWorkflow` naming per
  plan 1c.5 so "HPC" framing doesn't imply a `sleep`.
- **F5.3 Config + provenance.** Nextflow endpoint/creds/CA, pipeline **name+version** (the version
  goes **in the cache key** so a pipeline update is a miss, not a stale hit — D-011/D-033),
  poll/heartbeat/timeouts, artifact object-store location.
- **F5.4 Worker placement.** The `hpc-jobs` Temporal worker runs where it can reach the HPC/Nextflow
  launcher (an OpenShift pod with network egress to HPC, or on an HPC-adjacent host); `background-
  jobs` workers stay light in-cluster. Heartbeats guard against worker/preemption loss.

> **CHECKMATE F5** (G1–G7 + durability spike): A **real** Nextflow pipeline runs end-to-end durably
> (kill the worker mid-run → resumes, no re-run of completed steps); result cached; note PR-gated;
> pipeline version in the key. ADR: **D-A5 Nextflow HPC backend** (updates the HPC/DFT deferred row).

---

## Phase F6 — OpenShift deployment & delivery

**Why:** none of the above is real until it runs in-cluster with OIDC, secrets, and workers wired.

- **F6.1 Images.** Containerfiles for: front-door service, `hpc-worker`, `background-worker`, and
  the MCP capability servers. Rootless, UBI-based, non-root UID (OpenShift SCC-friendly).
- **F6.2 Manifests.** Helm chart (or Kustomize): Deployments, Services, **Routes** (front door),
  HPA for stateless services, readiness/liveness probes, `NetworkPolicy` (egress to HPC + internal
  LLM + Postgres only), ConfigMaps + **Secrets** (or External Secrets/Vault) feeding the one
  pydantic config.
- **F6.3 Stateful deps.** Temporal — **self-hosted on OpenShift vs Temporal Cloud** (F6 ADR;
  self-host keeps everything in-cluster and OIDC-consistent, Cloud reduces ops). Postgres/pgvector
  (managed or in-cluster operator) with mTLS + `statement_timeout` (already configurable, D-034).
- **F6.4 CI/CD.** Extend the existing `make lint type test` gate → build image → push to the
  internal registry → deploy (OpenShift Pipelines/Tekton or the current GitHub Actions → registry).
  Migrations (`make db-migrate`, tracked ledger D-034) run as a pre-deploy Job.
- **F6.5 Observability.** Flip on the **OTel toggle** (already built, D-027) → an in-cluster
  collector; ship logs/metrics/traces; dashboards for loop iterations, tool latency, job status.
- **F6.6 Config/secrets + Entra workload identity.** Every endpoint/CA/queue is one config value
  from OpenShift env/secrets — no second source. Crucially, **service credentials are Entra Workload
  Identity Federation**, not long-lived secrets: each Deployment's service account is federated to an
  Entra app registration, so pods mint short-lived Entra tokens at runtime (F4.2) and there are no
  client secrets to rotate in-cluster. Three credentials are **plain secrets** (Vault/secret store),
  not workload identity: the **generic LLM API key** (§0/F0.1), the Temporal mTLS certs, and the
  HPC-bridge credential.

> **CHECKMATE F6** (G1–G7): The full stack deploys to an OpenShift namespace; the front door is
> reachable via a Route behind OIDC; workers connect to Temporal + the HPC launcher + the internal
> LLM; probes green; secrets never in images. ADR: **D-A6 OpenShift topology**.

---

## Phase F7 — Generic data-source attachment seam (framework only, **no concrete sources**)

**Why:** the vision needs many future data sources — **LIMS, MES, analytical instruments, live ELN,
literature** — but per the brief we build **no concrete source or analytics tool now**. Instead we
make attaching one later trivial: a **stable, source-agnostic plug-in seam** so a new source is *one
thin adapter + one registry entry*, with zero core change and no source-specifics leaking upward.
This is foundation work (the seam), not capability work (the sources). The repo already has the two
half-contracts — this phase unifies and hardens them into one documented attachment point.

- **F7.1 One generic external-source contract.** Consolidate the existing **`ElnAdapter`**
  (ingestion: `fetch_new_entries(since) → RawEntry[]`, `map_to_*`) and the **`SourceRetriever`**
  (retrieval: `retrieve(query, filters) → EvidenceChunk[]` with mandatory citation) into one clearly
  documented **`DataSource`** seam with two independent halves — *ingest* (pull → normalize →
  PR-gated knowledge note and/or deterministic serving copy) and *retrieve* (query → cited chunks).
  A source may implement either or both. **Only the contract is fixed; never the shape** (D-018/D-023
  discipline preserved).
- **F7.2 Config-driven source registry.** Generalize the ELN adapter registry (D-028) into a
  **data-source registry**: a new source is a registry entry selected by config
  (`CHEMCLAW_DATA_SOURCES`), never a code change in the core. Ingestion sources reuse the durable
  `background-jobs` sync + cursor machinery (D-035); retrieval sources are auto-discovered by the
  research/report harness (`gather_evidence` already fans out over `SourceRetriever`s — adding one is
  one list entry, D-023).
- **F7.3 Canonical target stays generic; source schemas come *with* the source.** The mapping target
  remains the existing note graph (arbitrary `type`) + the ORD subset for reactions. We **do not**
  invent an analytical/LIMS/MES schema now — each source brings its own canonical mapping *when it is
  actually built* (e.g. AnIML/Allotrope for analytical, a LIMS result schema for LIMS), added behind
  the seam without touching the core. The seam does not presuppose any of them.
- **F7.4 Identity, provenance, access flow through the seam generically.** Every ingested record
  carries provenance (source id + native ref); a user-scoped source honors Entra/OBO (F4.3); the
  PR-gate stays the terminal gate for any *knowledge* a source proposes, while pure *serving* copies
  (search indices) are not gated (the D-018 split, generalized).
- **F7.5 Prove the seam with a reference adapter only.** Ship **one in-memory/reference `DataSource`**
  (used by tests and the credential-free demo) to prove attachment end-to-end — **not** a real LIMS/
  MES/analytical connector. Real sources are later, one adapter each.

> **CHECKMATE F7** (G1–G7): Can a *hypothetical* new source be attached as **one adapter + one config
> entry with zero core change** (demonstrated with the reference adapter)? Do ingest and retrieve
> stay independent, both source-agnostic (G6)? Does **no** source-specific type leak above the
> adapter? Is the PR-gate/serving split preserved? ADR: **D-A7 generic data-source seam** (no concrete
> sources, no analytical schema — deferred to when a real source is built).

> **Deferred behind this seam (not now):** concrete **LIMS / MES / analytical-instrument / live-ELN /
> literature** adapters and their standards (**AnIML/Allotrope** for analytical data, **SiLA2/LAP**
> for instruments, **Benchling API/MCP** for ELN) — each becomes one adapter + registry entry when
> that source is actually needed. Analytical *models* (retention prediction, peak deconvolution,
> spectral ID) are later capability work, unrelated to this seam.

---

## Phase F8 — Prediction trust + retrieval scale

**Why:** two foundation *contracts* cheap to set now, expensive to retrofit — the field's weakest
link (calibrated uncertainty) and a known scaling limit (NetworkX-only retrieval).

- **F8.1 Uniform uncertainty contract.** Every predictor returns **calibrated uncertainty +
  applicability-domain flag**; adopt **conformal prediction** where feasible (distribution-free
  coverage). Generalize today's per-calculator uncertainty (solubility RMSE, pKa) into a
  cross-cutting contract, like the calc cache generalized compute-once.
- **F8.2 Derived retrieval index (not a replacement).** Add **pgvector** embeddings over notes as an
  *entry-point* into the existing graph traversal — git-markdown stays the auditable source of
  truth (D-004 intact). Optionally model **time-bounded facts** (Graphiti-style) for evolving
  process knowledge. This is the derived index the SOTA review flagged as the missing scaling layer.

> **CHECKMATE F8** (G1–G7): Predictions carry calibrated uncertainty an analyst can act on; retrieval
> scales to a large corpus while graph traversal (not top-k) remains the reasoning path. ADRs:
> **D-A8 uncertainty contract**, **D-A9 derived retrieval index**.

---

## Phase F9 — Docs, ADRs, and autonomy evals (cross-cutting, continuous)

Runs alongside every phase; closes the "docs describe a system that doesn't exist" gap.

- **Rewrite `architektur.md` §6** (hosting) for OpenShift instead of Azure AI Foundry/Container
  Apps, Nextflow-on-HPC instead of raw SLURM, and the internal OpenLLM-like adapter instead of
  Anthropic/Azure OpenAI. **Keep §7/§8 (Entra ID durchgängig) — they are the requirement**, adjusting
  only the one mechanism that changes off Azure: **Managed Identity → Entra Workload Identity
  Federation** for backend service auth (the Temporal-claim and HPC-bridge patterns are unchanged).
- **ADR log:** D-A1…D-A9 above, plus finalize D-020 (harness backbone). Keep the terse-running-log
  discipline.
- **Autonomy metrics in the eval layer (2b):** register **plan quality** (needed vs planned steps),
  **"did plan/execute help"** A/B vs single-shot per task type, and **runaway/abort rate** (harness
  concept §7) — so autonomy must *prove* its value, selectively, not universally (D-009).

---

## Dependency graph & recommended order

```
F0 (LLM adapter+spike) ─┬─> F1 (harness) ─┬─> F2 (front door) ─> F3 (session+callback) ─> F4 (identity)
                        │                 │
                        └─────────────────┴─> F6 (OpenShift deploy) ── enables in-cluster test of F2–F5
F5 (Nextflow HPC) depends on F0 (config pattern) + F6 (worker placement); independent of F1–F4
F7 (generic source seam) / F8 (trust+retrieval) depend only on the spine — schedule after F2 exists
F9 runs continuously
```

**Critical path to a usable assistant:** F0 → F1 → F2 → F3 → (F4 for multi-user) → F6 to run it in
OpenShift. F5 makes heavy compute real; F7 makes future sources trivially attachable; F8 makes
predictions trustworthy. Per the brief, this plan adds **no new capability Skills/MCP tools and no
concrete data sources or analytics tools** — F7 is the *generic attachment seam only* (proved with a
reference adapter), and concrete sources (LIMS/MES/ELN/analytical) plus analytical/retro/prediction
*models* are explicitly later, one adapter/tool at a time behind the seam.

## Top risks (watch actively)

1. **Internal model tool-calling reliability (F0.2)** — the single biggest threat to the whole
   experience. If the OpenLLM-like model is weak at function-calling, the harness degrades; mitigate
   with constrained decoding / a stronger planning model / the classic fallback.
2. **Harness `[Experimental]` API churn** — keep the classic-`Agent` fallback load-bearing (F1.3).
3. **Nextflow launch-interface fit (F5.1)** — Seqera API vs SSH/CLI vs REST changes the adapter;
   pick early, keep it in one module.
4. **OpenShift SCC / egress** — non-root images and explicit NetworkPolicies to HPC + internal LLM
   are easy to get wrong; bake into F6 from the start.
5. **Data-source scope creep** — F7 is the *seam*, not a source. Resist adding a real LIMS/MES/
   analytical connector or schema now; ship only the generic contract + registry + one reference
   adapter. Concrete sources come later, one adapter each.

## Decisions to confirm before F-phases start

1. **LLM endpoint shape** — is the OpenLLM-like adapter OpenAI-compatible (base_url + tool-calling)?
   Streaming supported? (Shapes F0.)
2. **Identity** — *resolved:* **Azure Entra ID**, mandatory for users and all backend components.
   Open sub-question: backend service auth via **Entra Workload Identity Federation** on OpenShift
   (recommended — no stored secrets) vs client-credentials with cert/secret? And which resources sit
   behind the two §7 bridges (Temporal, HPC) in your tenant? (F4.)
3. **Front-door surface** — thin built-in web chat (recommended) vs adopting an existing chat UI. (F2.)
4. **Nextflow launch interface** — Seqera Platform API / SSH+CLI / internal REST launcher? (F5.)
5. **Temporal** — self-hosted on OpenShift vs Temporal Cloud? Session store Postgres vs Redis? (F3/F6.)
6. **Data-source seam** — confirm F7 is *seam-only* now (agreed: no concrete LIMS/MES/analytical
   source or schema yet). Which source is likely *first* to attach later (LIMS? MES? live ELN?), so
   the seam is validated against a realistic shape — without building it now?
