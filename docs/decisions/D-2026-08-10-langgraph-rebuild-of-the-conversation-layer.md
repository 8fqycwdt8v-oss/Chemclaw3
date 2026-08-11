# D-2026-08-10-langgraph-rebuild-of-the-conversation-layer — Layer 1 is rebuilt on LangGraph, and turn state stops being hand-built

**Status:** accepted · **Date:** 2026-08-10

Supersedes [`D-013`](D-013-maf-stays-the-orchestrator-reaffirmed-vs-langgraph.md),
[`D-038`](D-038-maf-agent-harness-as-an-optional-third-reasoning.md),
[`D-040`](D-040-f1-maf-agent-harness-is-the-autonomous-plan-execute.md) and
[`D-151`](D-151-the-durable-history-compacts-itself-because-maf-s.md). Amends the *implementation*
half of [`D-002`](D-002-maf-for-orchestration-temporal-for-durability-kept.md) — its rule is
untouched; see §3. All five are left as merged, as a merged ADR must be.

## Context

D-013 reconsidered MAF against LangGraph and kept MAF on three grounds. Two have since expired, and
neither expired because of anything LangGraph did to win an argument — the ground moved underneath
both of them:

| D-013's ground | Status |
| --- | --- |
| (a) LangGraph's edge is durable/checkpointed execution, moot because durability is Temporal's (D-002) | **Holds.** §3 is what this ADR does about it. |
| (b) MAF's native Agent-Skills (`SKILL.md` progressive disclosure) is load-bearing | **Expired.** `deepagents.SkillsMiddleware` implements the same three-level disclosure over a pluggable backend. |
| (c) Entra/Azure fit | **Expired.** D-039 made the LLM seam a generic OpenAI-compatible endpoint reached with one service credential. Neither framework has an Entra advantage; `agent/llm_provider.py` returns `Any`. |

D-013 also recorded the mitigation that makes this decision affordable: *"the agent layer is kept
thin and framework-swappable, bounding MAF's maturity risk."* That held. `agent_framework` is
imported at 19 sites in `src/`, policed by `tests/test_third_party_layering.py`, and
`core/tool_registry.py`, `api/events.py`, `agent/profiles.py`, the connector manifests and the
`session_messages` schema carry no framework types at all.

## The measurement that decides it

The case is not capability. It is that four separate pieces of this tree exist to work around
defects in the framework, and two of them were silent:

- [`D-123`](D-123-one-agent-per-concurrent-turn-because-the-anthropic.md) — `agent_framework_anthropic`
  keeps the streaming tool-call identity on the *client instance*. Measured, 8 attempts per
  configuration: 8 concurrent turns on a shared client failed **8/8**; on per-turn clients, **0/8**.
  `agent/agent_pool.py` (113 lines) exists for this and its own docstring says it is written to be
  deleted.
- `chemclaw_agent.py:473` — `require_per_service_call_history_persistence = False`. MAF's
  `MessageInjectionMiddleware` rebuilds the response with `ChatResponse.from_updates()` on the
  streaming path and drops the sentinel `conversation_id`, putting a `user` block between `tool_use`
  and `tool_result`. 100% of tool calls, both autonomy modes — **harness mode never worked**, and
  every unit test passed while it did not.
- D-151 — `CompactionProvider.after_run` reads `session.state[history_source_id]["messages"]`, which
  `PostgresHistoryProvider` deliberately never populates. Under the production default it is a
  permanent no-op, so `agent/history_compaction.py` reimplements MAF's compaction model against SQL.
- [`D-2026-08-08-a-private-import-of-a-type-alias-is-not-a-dependency`](D-2026-08-08-a-private-import-of-a-type-alias-is-not-a-dependency.md)
  — private-module churn, mitigated by re-declaring MAF's type aliases locally with a drift-detector
  test.

Two of the four (the pool, and D-151's cause) disappear outright under LangGraph rather than being
re-implemented. That is the difference between a swap and a rebuild.

## Decision

**Rebuild layer 1 on LangGraph, natively — not a port.** The instruction that shaped this is to use
the framework's full capability surface and optimize for the cleanliness of the result, accepting
more rework to get it. Concretely, five things become framework features that are hand-built today,
and each one *deletes* chemclaw code:

| Today | Rebuilt | Net |
| --- | --- | --- |
| Loop predicate + `loop_cap.py`'s stop-decision inference + `harness_types.py` aliases | An explicit `StateGraph` with named nodes and edges | −190 LOC; the cap becomes a counter, not an inference |
| Three human gates: `plan_approval_store.py`, `interaction_tools.py`, the KG PR-gate | One `interrupt()` / `Command(resume=…)` | −350 LOC, and DRY as CLAUDE.md asks |
| `session_store.py`'s rollback watermark, mid-turn resume, half-written-exchange guard | Checkpointer time-travel + `interrupt` resume | −400 LOC |
| `harness_mode.py` subclassing `AgentModeProvider` to *retract* the `mode_set` tool MAF injects | Never expose the tool (`request.override(tools=…)`) | −200 LOC |
| `agent_pool.py` | Deleted | −113 LOC |

Opaque `session.state` becomes a typed state schema (`agent/state.py`) with named,
reducer-governed fields. That is the change the rest depends on: `loop_cap.py`'s inference,
`harness_todo.py`'s `"awaiting-job:<id>"` description-string convention, and D-151's whole
translation layer are each a consequence of state that had no shape.

**A new capability follows from it**, and is the reason to do this now rather than later: a
supervisor plus specialist subagents, `Send` fan-out for the evidence sweep, and `BaseStore` for
cross-session memory. The substrate already exists — `AgentProfile` is an attenuate-only bundle with
build-time rejection of unknown tool names — so a specialist is a profile plus a compiled subgraph
and not a new concept. Its security invariants are a separate decision:
[`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`](D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md).

## 3. Where the durability line now falls

D-002's rule stands: **long and expensive work is Temporal's.** What changes is an implementation
consequence D-002 could not have anticipated, because it was true of MAF and not of frameworks in
general — MAF gave layer 1 no durability at all, so chemclaw hand-built turn-level durability inside
`agent/session_store.py`. That is the code D-002's rule was protecting against, and it ended up
written anyway, in the layer the rule names.

- **Temporal keeps** QM/DFT via Nextflow, BoFire campaigns, report generation, re-index and every
  connector job. Unchanged. No LangGraph durable-execution feature is used for any of them.
- **The checkpointer takes** turn state, `rollback_to(session_id, watermark)` (time-travel fork),
  mid-turn resume (`interrupt` / `Command(resume=…)`) and survival across a pod restart.

`CLAUDE.md`'s "Durability lives **only** in Temporal, never in MAF" — quoted verbatim in
`tests/test_third_party_layering.py`'s module docstring — is reworded to name what it was always
about: never in the conversation layer's own ad-hoc stores. The rule got stricter, not looser; what
it forbids is now enforced by there being nothing hand-built left to enforce against.

`session_messages` survives as a **read-model projection** for `GET /sessions/{id}/messages` and the
audit trail, written from the checkpoint stream and never read back into the graph. That keeps the
route, the ownership gate and the GxP record intact while removing the complexity that existed only
because MAF made the table authoritative: `message_pairing.py`'s orphan repair, the deliberately
absent `LIMIT`, and the write-back-on-read.

## What is deliberately not adopted

- **The checkpointer as a job store.** It is turn state, not durable execution. A connector job that
  runs for six hours on a cluster is Temporal's, and nothing here changes that.
- **`Store` as knowledge.** Layer 4 is Git plus Markdown behind the PR-gate. `BaseStore` is memory;
  anything agent-generated that reaches the knowledge graph still goes through a human (D-005).
- **deepagents' generic batteries** — file memory, file access, shell, web search — for the same
  reason D-038 disabled MAF's: capability comes from connectors, not from the harness's built-ins.

## Consequences

- Migration is phased behind `CHEMCLAW_AGENT_ENGINE` (`maf` | `langgraph`), so an unfinished engine
  is never what a deployment gets. Both engines emit the same `api/events.py` stream — that contract
  is the conformance boundary, which is what lets the two be scored against each other on one eval
  suite instead of argued about.
- The switch and the MAF branch are deleted together once the rebuild is proven **live**. Not by the
  test suite: the four defects above include two that passed every unit test, so the gate is a live
  re-run of the concurrency probe, the durable-launcher probe, an end-to-end plan→approve→execute,
  and `make eval-strict` against the MAF baseline.
- This trades one young framework's defect load for another's. LangChain 1.x has open issues on
  dynamic tool addition and no observational `before_tool`/`after_tool` hook. That is a real cost and
  the live re-validation is what turns it from an assumption into a measurement.
- Session affinity may become unnecessary. `deploy/helm/chemclaw/templates/service-route.yaml` and
  `poddisruptionbudget.yaml` justify it by "the harness todo list lives in MAF `session.state`"; with
  turn state in Postgres that justification is gone. To be verified, not assumed.
