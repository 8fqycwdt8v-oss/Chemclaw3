# 08 — Agentic Engine: Completeness Gap Analysis

*Scope: the agentic engine only (MAF orchestration, tools/skills, durability seam, HITL gates,
observability, evals). Completeness — "is the capability present and wired?" — not correctness/
security bugs (covered by audits 03–07). Date: 2026-07-22.*

## Is there an agentic engine, and what shape?

Yes. Chemclaw is a **single-agent** system built on **Microsoft Agent Framework (MAF)**, not a
bespoke loop. `agents/chemclaw_agent.py::build_agent` wires one `Agent` (or, behind a config flag,
MAF's `create_harness_agent`) with: typed Python function tools + config-declared MCP stdio servers
for capability; a `FileSkillsSource`-backed `SkillsProvider` for progressive-disclosure judgment
(`SKILL.md`); an injectable chat client selected by `agents/llm_provider.py`; a `HistoryProvider`
(in-memory or durable Postgres); a `CompactionProvider` for bounded context; and one
`function_middleware` audit trail over every tool call. Durability of long jobs lives **outside** the
agent, in Temporal (`workflows/`), reached through thin non-blocking tool adapters. The front door
(`service/app.py` + `service/runner.py`) runs one turn per SSE request. This is a real, layered
agentic engine; most "loop" mechanics are delegated to MAF rather than hand-rolled, which is the
right call — the gaps below are mostly at the seams MAF does *not* cover (evals, cost, versioning
provenance, admission control).

---

## 1. Orchestration loop

**Verdict: Present.**

**Evidence:** Classic path builds a stock MAF `Agent` (`agents/chemclaw_agent.py:137-150`), whose
`agent.run(..., stream=True)` runs MAF's internal tool-calling loop (model → tool execution →
observe result → re-prompt until a final answer); `service/runner.py:73-83` consumes that stream.
The harness path (`_build_harness_agent`, `:153-198`) adds a self-managed todo list, an explicit
plan/execute `AgentModeProvider`, and a bounded completion loop (`loop_should_continue=
todos_remaining(...)`, `loop_max_iterations=settings.harness_max_loop_iterations`) — i.e. real
plan→act→observe→reflect. `_INSTRUCTIONS` (`:49-83`) spells out an explicit gather→cross-learn→
compute→propose research loop.

**Assessment:** This is a genuine act→observe loop, not prompt-and-parse. Reflection/re-planning is
only in the harness path, which is **off by default** (`harness_enabled=False`, `config.py:269`), so
the default production shape is the classic tool-calling loop (which still observes tool results and
re-plans within a turn, just without an explicit todo/plan artifact). For a Q&A research assistant
that is appropriate; the deferral of durable multi-step research is conscious (DEFERRED.md, "Durable
multi-step deep research").

**Gap severity: None.**

## 2. Tool/skill registration & discovery

**Verdict: Present (registration/discovery); Partial (versioning).**

**Evidence:** Tools are typed Python functions; MAF infers each schema from the signature + docstring
(`agents/qm_tools.py:25-42`, docstring *is* the tool description). The tool set is a hardcoded list,
`_capability_tools()` (`chemclaw_agent.py:217-236`). MCP capability servers are **config-driven and
dynamic** (`settings.mcp_servers`, `config.py:223-236`; `_mcp_capability_tools`, `:239-259`) with
per-server `allowed_tools`. Skills are discovered dynamically from the filesystem
(`FileSkillsSource(settings.skills_dirs)`, `chemclaw_agent.py:119-121`) with progressive disclosure,
role-scoped by `RoleScopedSkillsSource` (`agents/skill_access.py`). `scripts/validate_skills.py`
gates SKILL.md frontmatter (name/description) in CI. **No version field** exists on skills
(frontmatter is name/description only) or on the tool schemas.

**Assessment:** The mechanism is consistent and documented (`skills/README.md`). Skill discovery is
dynamic; the in-process tool list is hardcoded but that is fine — adding structural capability is a
config entry (MCP), and the Rule-of-Three argues against a plugin registry for ~13 stable functions.
The missing piece is explicit **versioning** of a tool/skill contract (see §14 — the real cost is
provenance, not registration).

**Gap severity: Low.**

## 3. Durable execution / resumability

**Verdict: Present & good.**

**Evidence:** Long/expensive work is Temporal, replay-safe: `QMJobWorkflow` sequences activities with
config timeouts and resumes from event history (`workflows/qm_job.py:32-96`, docstring `:8-9`). The
agent holds no durable job state (`qm_tools.py` returns a job id immediately). Conversation history is
durable via `PostgresHistoryProvider` (`session_store=postgres`, `chemclaw_agent.py:201-214`;
`config.py:294-302`) so a chat survives a pod restart. The async approval hold is a durable workflow
(`InteractionApprovalWorkflow`, `workflows/interaction_approval.py:61-98`). Job→session push-back is a
durable Postgres mailbox (`agents/session_events.py`).

**Assessment:** Checkpointing/resumability is a first-class, correctly-placed concern (durability in
Temporal, never MAF — D-002). No gap.

**Gap severity: None.**

## 4. Idempotency of agent-triggered side effects

**Verdict: Present & good.**

**Evidence:** `submit_qm_job` uses a deterministic workflow id (`qm-{qm_job_key(job)}`) +
`WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY` and swallows `WorkflowAlreadyStartedError`,
returning the existing id — a retried/duplicate submit is a no-op, only a *failed* job re-runs
(`qm_tools.py:56-72`). The calculation store never recomputes a cached result (D-011). The PR-gate
uses a stable branch name `note/<id>` and is an explicit idempotent no-op for a byte-identical note
(`kg/pr_gate.py:31-45,64-67`). `start_approval` derives a stable id and returns the existing hold
(`interaction_tools.py:25-42`). Note publish is best-effort with a bounded retry policy
(`qm_job.py:78-95`).

**Assessment:** The two irreversible/expensive agent side effects (launch a job, write knowledge) are
both idempotent by construction. The one known non-idempotent window — two *concurrent* cache misses
on the same key both computing — is consciously deferred as benign (DEFERRED.md, "Per-key in-flight
dedup in the calc store"; trigger: expensive real HPC runs).

**Gap severity: None.**

## 5. State/memory management

**Verdict: Present & good.**

**Evidence:** Short-term scratchpad = the session thread (`HistoryProvider`) + harness todo list,
kept **bounded** by `CompactionProvider` / `TokenBudgetComposedStrategy` (collapse stale tool-result
dumps, then slide the window, within `agent_context_token_budget`; LLM-free)
(`chemclaw_agent.py:262-306`; `config.py:237-247`). Long-term cross-session memory = the Git Markdown
knowledge graph + the distilled memory layers (`memory/playbook.py`, `optimization.py`,
`interaction.py`), all entering via the PR-gate. The two are cleanly separated: conversation state is
*not* Temporal job state (D-002), and durable knowledge is *not* conversation history.

**Assessment:** Clean short/long-term separation, and context growth is explicitly bounded. No gap.

**Gap severity: None.**

## 6. Error handling within the agent loop

**Verdict: Partial.**

**Evidence:** A tool exception is audited (with latency + truncated args) and re-raised
(`agents/audit.py:103-128`). At the turn boundary, `run_turn` catches **all** exceptions and collapses
them into one user-safe `ErrorEvent`, logging the trace server-side and aborting the turn
(`service/runner.py:84-95`). Transient model-call failures (429/5xx) are retried with backoff by the
OpenAI SDK via `settings.llm_max_retries` (`llm_provider.py:53-59`). There is **no** distinct
in-loop handling for tool-error vs model-error vs timeout vs malformed tool-call output, and no
agent-level retry of a failed tool mid-turn — any failure ends the turn.

**Assessment:** The error taxonomy is coarse: a transient Postgres blip inside one tool kills the
whole turn rather than retrying that tool (durable jobs are unaffected — they retry in Temporal).
For an interactive assistant this is a degraded-UX gap, not a data-integrity one (the human simply
re-asks; nothing partial is persisted outside the idempotent paths of §4). Malformed tool-call
recovery is whatever MAF does internally, which this codebase neither reinforces nor observes.

**Gap severity: Medium.** *Failure scenario:* a momentary DB timeout during `gather_evidence` aborts
the entire chemist turn with a generic error, instead of retrying the sweep or degrading gracefully.

## 7. Guardrails / output validation

**Verdict: Partial.**

**Evidence:** **Tool-call args** are validated: tools take typed pydantic/typed signatures
(`QMJobInput`, `OptimizationProblem`/`Observation`, `submit_qm_job(molecule_smiles, method,
basis_set)`), values are clamped (`expand_note` clamps `hops` to `graph_max_hops`,
`graph_tools.py:85-87`), the durable boundary rejects bad data (`require_actor`, canonical-SMILES
checks in activities), and front-door input is size-bounded (`service/app.py:82-92`). Untrusted
retrieved content is framed to blunt injection (`agents/framing.py`). **Final outputs are not
schema-validated**: the agent's answer is free prose streamed straight to the user
(`runner.py:74-83`); structured/validated agent proposals (`response_format` + `resp.value`) are
explicitly **deferred** (BACKLOG.md "MAF out-of-the-box features → Structured outputs").

**Assessment:** Anything the agent *persists* is validated downstream — notes pass `kg/validate.py`
+ the human PR-gate, so a malformed proposal cannot silently corrupt the graph. The unvalidated
surface is only the prose answer to a human who is themselves the sign-off. Given the GxP "human
signs off" posture, unvalidated advisory prose is acceptable; the gap matters most where the agent's
tool *arguments* to a costly path could be malformed — but those are pydantic-typed at the tool and
re-checked at the durable boundary. Deferral of structured outputs is reasonable until a
machine-consumed payload exists.

**Gap severity: Low.**

## 8. Human-in-the-loop gates

**Verdict: Present (knowledge PR-gate — enforced in code, always on); Partial (pre-execution job
approval — enforced only under harness/Entra).**

**Evidence:** The knowledge PR-gate is enforced in code, not policy: every agent-authored note goes
through `propose_note`, which *rejects* a non-agent note and always lands on a branch/PR for human
merge (`kg/pr_gate.py:48-83`); all write tools (`propose_knowledge_note`, `record_confirmed_answer`)
route through it (`graph_tools.py:99-133`, `memory_tools.py`). The async confirmed-answer approval is
a durable Yes/No hold (`interaction_approval.py`). Expensive triggers are gated by the single
`authorize_trigger` gate called before any durable work (`agents/authz.py:22-41`; called in
`qm_tools.py:45`). The harness `plan_only` mode presents a plan for approval before executing
(`chemclaw_agent.py:174-197`). **However:** `authorize_trigger` is a no-op unless `entra_required`
(`authz.py:33-34`), and the plan-approval gate only exists when `harness_enabled` — both **off by
default**. So on the default classic/dev path, the agent can autonomously call `submit_qm_job` with
no human pre-approval.

**Assessment:** The *irreversible knowledge-write* gate — the actual GxP "AI proposes, human signs
off" line — is unconditionally enforced in code, which is the important thing. The *pre-execution
job* gate is present but config-gated; that this is open in dev is by design (D-043 seam). The
residual concern is a real Entra deployment that enables the harness/autonomy without declaring
`entra_expensive_actions` — but `config.py:597-601` fails startup if roles/actions are half-declared.
QM jobs are also idempotent + cached + human-polled, so an autonomous submit is low-blast-radius.

**Gap severity: Medium.** *Failure scenario:* a deployment runs the harness in `execute` autonomy
with `entra_expensive_actions` left empty — the agent auto-launches HPC jobs with no human checkpoint
(the gate silently passes), though the startup validator and the cost of real HPC (still mock here)
bound the risk.

## 9. Multi-agent coordination

**Verdict: N/A (single-agent by design).**

**Evidence:** One `Agent` per process (`service/app.py:117-139`, "One agent per process"). No
handoff/delegation, no sub-agent spawning, no shared-context protocol anywhere in `agents/`.

**Assessment:** Correctly out of scope — the architecture is deliberately a single MAF agent over
Temporal jobs (D-002). Not a gap.

**Gap severity: None.**

## 10. Observability

**Verdict: Partial.**

**Evidence:** Every tool call is recorded once — correlation id (conversation), actor, tool,
truncated args, outcome, truncated effect summary, latency — to the stdlib log always and to an
append-only Postgres table when a sink is wired (`agents/audit.py:34-152`; `agents/audit_store.py`,
`audit_events`). The SSE stream surfaces live tokens, tool calls (name + 200-char arg preview),
approval requests, and the final answer (`service/runner.py:79-83,102-118`). OTel export is opt-in
(`config.py:67-77`). **Not captured:** the model's reasoning/plan text, the full (untruncated)
tool args/results, and the prompt/skill version in effect — args are truncated to
`agent_audit_max_arg_chars` (200) and results summarized, so a run cannot be **replayed step-by-step
with reasoning**.

**Assessment:** For the GxP question "who ran what, when, to what effect" the durable audit trail is
strong and purpose-built. For *debugging agent reasoning* (why did it pick this tool, what did it
conclude) it is thin — you see the tool sequence and outcomes but not the deliberation or the full
payloads. Truncation is a deliberate PII/size tradeoff, but it means no faithful replay.

**Gap severity: Medium.** *Failure scenario:* an agent gives a subtly wrong answer; the audit trail
shows which tools ran but not the reasoning or the full evidence it weighed, so root-causing a bad
turn relies on server logs + guesswork rather than a replayable trace.

## 11. Cost/token tracking

**Verdict: Absent.**

**Evidence:** No token or cost accounting exists anywhere in `agents/`, `service/`, `workflows/`,
`evals/` (grep for `usage`/`prompt_tokens`/`completion_tokens`/`cost` finds only unrelated matches).
The audit trail records latency, never tokens. `llm_max_tokens`/`agent_context_token_budget` bound a
single call/context but nothing measures consumption per run/user/task.

**Assessment:** The design deliberately reaches the LLM through **one generic credential against an
internal OpenAI-compatible endpoint**, explicitly *not* per-user-billed (`llm_provider.py:9-13`;
foundation-plan §0). So there is no external per-token bill to attribute, which lowers the urgency.
But a shared internal endpoint still has finite capacity, and without per-user/task token accounting
there is no fairness signal, no way to spot a runaway conversation burning context, and no cost
input to the tool-utility A/B beyond metric deltas. This is a genuine absence, not a deferral (no
DEFERRED/ADR entry claims it).

**Gap severity: Medium.** *Failure scenario:* a harness run loops near `max_loop_iterations` on a
large corpus, consuming a disproportionate share of the shared endpoint's throughput, and nothing
attributes or caps that consumption — it surfaces only as other chemists' turns slowing down.

## 12. Model routing & fallback

**Verdict: Partial.**

**Evidence:** The provider is a single config choice (`openai_compatible` XOR `anthropic`,
`llm_provider.py:26-40`); one `base_url`/`model` per deployment. Transient rate-limit/5xx are retried
with backoff via `llm_max_retries` (OpenAI SDK) and `llm_timeout_seconds` (`llm_provider.py:53-59`).
There is **no** fallback model/endpoint on hard unavailability and **no** task-based routing — a dead
endpoint fails the turn after its retries.

**Assessment:** For the target topology (one internal endpoint) a secondary endpoint would improve
availability but is not obviously required — the internal endpoint *is* the one sanctioned target,
and retry/backoff covers the common transient case. A version change is a config edit
(`llm_model`), not a code change, which is fine. The absence of any fallback endpoint is a real but
low-urgency availability gap given the single-endpoint design intent.

**Gap severity: Low.**

## 13. Evaluation harness

**Verdict: Partial (chemistry-metric evals present; agent-behavior evals absent — partly deferred).**

**Evidence:** `evals/` is a real, versioned, CI-gated harness — but it scores **chemistry metrics**
(E-factor, PMI, prediction accuracy) over frontmatter case files (`evals/harness.py`,
`evals/metrics.py`) plus a per-task **tool-utility A/B** (`evals/ab.py`, pure comparison over
already-scored values). It does **not** evaluate agent behavior: no automated suite over
tool-selection, prompt changes, or skill judgment. BACKLOG.md explicitly scopes this out ("the
wholesale MAF eval harness … cherry-pick only its tool-call checks") and the agent-harness note
records that Phase 5b's harness "does not replace" flows.

**Assessment:** The chemistry eval layer is good and correctly placed. But for an agent whose
behavior is steered by a prose `_INSTRUCTIONS` block and ~12 SKILL.md judgment files, the lack of an
automated **agent-behavior / prompt / skill regression** suite means a prompt or skill edit can
silently change which tools the agent picks or how it cites, with only manual spot-checks catching it
— thin for a GxP system that must defend reproducibility. The A/B harness is a building block but is
not wired to any live agent-run gate.

**Gap severity: Medium.** *Failure scenario:* someone tightens the `deep-research` skill wording; the
agent quietly stops calling `predict_solubility` proactively; no eval catches the regression before
it ships because the eval suite scores chemistry outputs, not agent tool-use.

## 14. Prompt/skill versioning & change management

**Verdict: Partial.**

**Evidence:** Skills and the `_INSTRUCTIONS` prompt are plain files/strings in Git, so *content* is
versioned and reviewable in a PR, and `scripts/validate_skills.py` gates frontmatter in CI. But there
is **no version field** on a skill and, critically, the audit trail records tool calls **without the
prompt/skill version (Git SHA) in effect** (`audit.py:34-45`) — you cannot tie a past agent result
to the exact prompt/skill revision that produced it. Pre-live testing of a prompt/skill change is
limited to frontmatter validation; there is no behavioral gate (see §13).

**Assessment:** "Which prompt/skill produced this result?" is answerable only by correlating a run's
timestamp against `git log` of `skills/` and `chemclaw_agent.py` — indirect and fragile, and broken
entirely if a deployment runs ahead of/behind the repo. For a GxP platform that reuses "AI proposes,
human signs off" as its spine, weak result→version provenance is a real reproducibility gap. Cheap
fix: stamp the effective prompt/skill/config version onto each audit correlation.

**Gap severity: Medium.** *Failure scenario:* an auditor asks which skill version generated a merged
knowledge note six months ago; the audit trail names the tools but not the skill/prompt revision, so
the answer requires reconstructing the deployment's Git state at that time.

## 15. Concurrency / rate limits

**Verdict: Partial.**

**Evidence:** The front door bounds live-session **memory** via an LRU cap
(`_LiveSessions`, `service/app.py:46-74`; `service_max_live_sessions`) and message **size**
(`config.py:283-286`), and the harness loop is capped per run (`harness_max_loop_iterations`, the
runaway guard). Temporal task queues bound *job* worker concurrency. But there is **no** per-user or
global limit on concurrent in-flight agent turns, and **no** admission control/semaphore throttling
parallel model calls against the shared LLM endpoint — back-pressure relies entirely on the OpenAI
SDK retrying 429s (`llm_max_retries`).

**Assessment:** The LRU cap prevents an unbounded-memory leak but is not a concurrency limit — N
chemists can each hold a turn open, each fanning out tool calls and hitting the one internal endpoint,
with nothing capping the aggregate. Retries absorb transient 429s but are not admission control (they
can amplify load under saturation). For a shared, finite internal endpoint this is a real
runaway-parallelism gap; downstream Postgres/Temporal are more naturally bounded.

**Gap severity: Medium.** *Failure scenario:* a burst of concurrent chemist sessions (or several
autonomous harness runs) saturates the internal LLM endpoint; with no admission control the SDK
retries pile on, degrading latency for everyone rather than shedding or queuing load.

---

## Gap findings table

| ID | Capability | Current State | What's Missing | Why It Matters For This System | Severity | Effort |
|---|---|---|---|---|---|---|
| AG-1 | Orchestration loop | MAF tool-calling loop (classic) + plan/execute harness (flagged off) | Nothing material; reflection only in the off-by-default harness | Loop is real and delegated to MAF; deferral of durable deep-research is conscious | None | — |
| AG-2 | Tool/skill registration & discovery | Typed tools (hardcoded list) + dynamic MCP config + filesystem skill discovery; frontmatter CI-gated | Explicit version field on tools/skills | Registration is consistent; missing versioning bites as provenance (AG-9), not discovery | Low | S |
| AG-3 | Durable execution / resumability | Temporal jobs (replay-safe) + durable Postgres sessions + durable approval hold | — | Correctly placed; crash/restart loses nothing | None | — |
| AG-4 | Idempotency of side effects | Deterministic workflow ids + FAILED_ONLY reuse; idempotent PR-gate & approval | — (concurrent cache-miss dedup deferred, benign) | The two irreversible effects (job launch, knowledge write) are idempotent by construction | None | — |
| AG-5 | State/memory management | Session thread + compaction (bounded) short-term; Git graph + memory layers long-term | — | Clean separation, context growth bounded LLM-free | None | — |
| AG-6 | Error handling in loop | Tool errors audited+raised; whole turn → one ErrorEvent; SDK retries model 429/5xx | Distinct tool/model/timeout/malformed handling; in-turn tool retry/degrade | A transient tool blip aborts the whole chemist turn (no data risk — jobs retry in Temporal) | Medium | M |
| AG-7 | Guardrails / output validation | Tool args pydantic-typed + clamped + framed; downstream kg-validate + PR-gate | Schema validation of final agent output (structured outputs deferred) | Persisted output is validated + human-signed; only advisory prose is unvalidated | Low | M |
| AG-8 | Human-in-the-loop gates | Knowledge PR-gate always enforced in code; job/plan gates config-gated (Entra/harness) | Pre-execution job approval on the default classic/dev path | GxP knowledge-write line is hard-enforced; autonomous job launch un-gated unless Entra+harness on | Medium | M |
| AG-9 | Multi-agent coordination | Single agent per process | — | Deliberately single-agent (D-002) | N/A | — |
| AG-10 | Observability | Append-only tool-audit (cid/actor/tool/args/outcome/latency) + live SSE + opt-in OTel | Reasoning/plan capture + full payloads → step-by-step replay | GxP "who did what" is strong; debugging *why* a turn went wrong is thin | Medium | M |
| AG-11 | Cost/token tracking | None | Per-run/user/task token accounting | Internal flat-credential endpoint (no external bill) but shared finite capacity is unmeasured | Medium | M |
| AG-12 | Model routing & fallback | Single config-selected endpoint + SDK retry/backoff | Fallback endpoint/model; task routing | Single-endpoint design intent; retry covers transient, no failover on hard outage | Low | M |
| AG-13 | Evaluation harness | Chemistry-metric evals + tool-utility A/B (CI-gated) | Automated agent-behavior / prompt / skill regression suite | Prompt/skill edits can silently regress tool-selection with no gate — weak for GxP reproducibility | Medium | L |
| AG-14 | Prompt/skill versioning & change mgmt | Files versioned in Git; frontmatter CI-gated | Result→version provenance in the audit trail; behavioral pre-live gate | Cannot tie a past agent result to the exact prompt/skill revision — a GxP reproducibility gap | Medium | S |
| AG-15 | Concurrency / rate limits | Live-session LRU (memory) + msg-size cap + per-run loop cap; SDK 429 retry | Per-user/global turn concurrency + admission control on the shared LLM endpoint | Unbounded parallel turns can saturate the one internal endpoint; retries amplify, not shed | Medium | M |

*No Critical gaps: the durability, idempotency, memory-separation, and GxP knowledge-write gate — the
capabilities that protect data integrity and the "human signs off" line — are all present and
correctly placed. The Medium cluster is about **operating** the engine safely at scale
(observability depth, cost/quota, concurrency) and **defending change over time** (agent-behavior
evals, result→version provenance), not about the core loop being missing.*

---

## Executive summary — the five most important agentic-engine gaps

- **No result→prompt/skill-version provenance (AG-14, Medium, cheap fix).** The audit trail records
  every tool call but never the prompt/skill/config revision in effect, so a past agent result cannot
  be tied to the exact version that produced it — a direct hit on the GxP reproducibility this system
  is built to defend. Stamping the effective version onto each audit correlation is a small change.

- **No agent-behavior evaluation (AG-13, Medium).** `evals/` scores *chemistry* (E-factor, PMI,
  prediction accuracy) and tool-utility, but nothing automatically tests that a prompt or SKILL.md
  edit still selects the right tools and cites correctly. Behavior is steered by prose files with only
  manual spot-checks and frontmatter validation as a gate — a prompt tweak can silently regress
  tool-use.

- **Cost/token consumption is entirely unmeasured (AG-11, Medium).** Justified for external billing
  (one flat internal credential), but the shared internal endpoint has finite capacity and there is
  no per-user/task token accounting — no fairness signal, no runaway-conversation detection, no cost
  input to the tool-utility A/B.

- **No admission control on concurrent turns (AG-15, Medium).** The front door bounds session
  *memory* (LRU) and message size but not the number of concurrent in-flight turns hitting the one
  internal LLM endpoint; back-pressure is only SDK 429-retry, which amplifies rather than sheds load
  under saturation.

- **HITL job-gate and error-recovery are config/coarse (AG-8, AG-6, Medium).** The GxP *knowledge*
  gate is unconditionally enforced in code (good), but pre-execution *job* approval is open unless
  Entra+harness are both enabled, and any in-turn failure (e.g. a momentary DB blip) collapses the
  whole chemist turn into one error with no tool-level retry or graceful degradation.
