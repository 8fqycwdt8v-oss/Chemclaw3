<!-- STATUS: a review record. Findings are marked with how they were established — executed,
     measured, source-verified, or estimated — because the difference decides what to act on.
     Refuted leads are kept in place; a lead that looked right and was not is the more useful
     record. Read top to bottom. -->

# Agentic system review — 2026-07

Scope: how well the agentic system performs, and whether it can be made faster **and** more
reliable at once. Weighted equally, at the reviewer's request: configurability, monitorability,
answer quality and grounding, scalability and concurrency, security/GxP, and cost/token economics.

Baseline: `dc66be3`. A live Anthropic key was available, so the LLM boundary was exercised for the
first time — which is where most of what follows was found.

---

## Verdict

**The system is in better engineering shape than most, and it had never been run.**

That is the finding, and everything below is a consequence of it. 1176 tests, `mypy --strict` over
19 packages, an 80 % branch-coverage floor, six bespoke wiring validators, 124 ADRs, and a
50-concurrent-user load test with honest retractions in it. All of that verifies *shapes*. Three
separate shipped defaults turned out to be wrong at a boundary no test crosses, and each was fatal
on first contact:

| | What happened | How found |
|---|---|---|
| Default LLM config | Every turn returned `400 temperature is deprecated for this model` | Captured the real request |
| Shipped Helm chart | Front door and every worker CrashLoopBackOff at startup | Executed the ASGI lifespan |
| Agent pool | A raising factory deadlocked the pod permanently, `/healthz` still green | Reproduced the deadlock |

None is exotic. All three are the configuration the repository ships. The suite is green because
every test injects a fake chat client and every chart test constructs `Settings(**helm_values)`
without executing one — two blind spots pointing the same way.

**Performance is genuinely good and mostly already fixed.** The leads this review started from —
no connection pool, serial retriever fan-out, event-loop blocking, single-process ceiling — were
closed by D-119/D-121 before it began, with measurements. What remained was a per-turn connector
seam with three unintended consequences, and one large unexploited cost lever.

**The GxP claim at the centre of the system is false in the shipped production configuration.**
That is the most serious finding and it is not yet fixed; it needs a design decision, below.

---

## 1. The pre-execution approval gate does not exist — Critical

`SECURITY.md`, `docs/harness-konzept.md` §6 and the `build_agent` docstring all describe a GxP
pre-execution gate: in `plan_only`, the agent proposes and waits for human approval before
executing. Production runs exactly this (`values.yaml`: `HARNESS_ENABLED: "true"`,
`HARNESS_AUTONOMY: "plan_only"`).

**The model authorizes itself.** MAF's `AgentModeProvider.before_run` injects a `mode_set` tool
into the model's own tool surface on every harness run, declared `approval_mode="never_require"`.
The upstream instructions tell the model to use it: *"When approval is granted, always switch to
execute mode (using the `mode_set` tool)"* — where "approval is granted" is the model's own reading
of the conversation. `grep set_agent_mode` across the repository returns zero callers.

| Question | Answer |
|---|---|
| What flips plan → execute? | The model calling `mode_set("execute")`. Nothing else. |
| Bound to a specific plan? | No. `mode_set(mode: str)` carries no plan reference or hash. |
| Recorded with an actor? | Yes — and this is the worst part. The audit middleware records `mode_set` under `get_current_actor()`, the **chemist's** Entra oid. The trail shows an attributable approval with no human act behind it. |
| Does authz stop it? | No. `mode_set` is not in `DEFAULT_WRITE_TOOL_GATES`, `tool_authz_default` is `"allow"`, and production sets no `tool_role_gates`. |
| Can a profile remove it? | No. `tool_names` narrows `_capability_tools`; `mode_set` is injected by a context provider downstream of that. |

`docs/harness-konzept.md` §6 specifies the real design — `plan_mode_required_for` as a hard tool
lock. That identifier exists nowhere in the codebase.

**Why it survived.** Two tests assert the gate. `test_agent.py` asserts `mode.default_mode` — the
initial value. `test_harness_execution.py::test_plan_only_autonomy_does_not_auto_loop` asserts the
loop does not *auto*-start. Neither ever has the model call `mode_set`, which is the only thing
that breaks it.

**Scenario.** A chemist asks for a route assessment. The agent plans, presents, and the chemist
replies "looks reasonable, but check the exotherm first." The model reads approval, calls
`mode_set("execute")`, and loops autonomously through up to 25 iterations launching durable jobs —
`authorize_trigger` passes, because the chemist does hold the role — landing PR-gate proposals. The
audit trail is indistinguishable from a real approval.

**Not fixed here, deliberately.** The fix is architectural: `mode_set` must leave the model's
surface, and the flip must become an owner-scoped route recording `(session_id, plan_hash, actor,
decided_at)`. `POST /approvals/{id}/decision` already does exactly this for durable jobs and is
explicitly not an agent tool, for this reason — so the pattern to copy is in the tree. This needs
an ADR and a decision about what a "plan hash" covers.

---

## 2. Fixed in this review

Each verified before and after; commits carry the detail.

| Finding | Established by | State |
|---|---|---|
| `temperature` rejected by the default model — every turn 400s | Captured the real outgoing request | **Fixed.** `llm_temperature` is `float \| None`, omitted from the payload when unset. Verified: the same turn now completes. |
| Shipped chart has no OTel SDK but sets `OTEL_ENABLED=true` — every pod CrashLoopBackOffs | Executed the ASGI lifespan under the Helm ConfigMap | **Fixed.** SDK + OTLP exporter declared; new test *executes* `configure_telemetry()` under the shipped value. |
| `AgentPool` permanently deadlocks on a raising factory | Reproduced (TimeoutError on old code) | **Fixed.** Count committed after the build. |
| Every connector call capped at 5 s; overrun burns the full `request_timeout` | Reproduced against a real server (8 s tool → 60 s failure) | **Fixed.** `request_timeout` now bounds the read; connect stays short. |
| Six `httpx.AsyncClient`s leaked per turn | Reproduced (`is_closed=False` on all six) | **Fixed.** Verified: all six released after teardown. |
| Six connector connects serial before the first token | Source-verified | **Fixed.** Gathered; degradation semantics confirmed intact. |

---

## 3. Cost and token economics — the largest unexploited lever

**Measured, by capturing the real request for one turn** (counts from Anthropic's own tokenizer):

| Block | Tokens |
|---|---:|
| System instructions + skills manifest (27 skills) | 3,463 |
| Tool schemas (28 tools) | 11,132 |
| **Fixed prefix, before the chemist says anything** | **14,595** |
| With the 21 connector MCP tools attached (production) | ~20,500 |

`cache_control` appears **zero** times in first-party code. That prefix is re-paid on every model
call — ~2.5 per turn — and up to 25 times per turn in harness mode. Most expensive single schemas:
`start_optimization_campaign` (1,935), `compute_reaction_energy` (1,183), `compute_dft_energy`
(1,106).

Three things make this harder than "add cache_control", and they should be decided together:

1. **MAF's Anthropic client reaches only the system half.** `instructions` accepts structured
   blocks, so the ~3.5 k is cacheable; there is no `cache_control` hook for `tools`, which is the
   11 k that dominates.
2. **Production is `openai_compatible`**, which has no explicit breakpoint concept and relies on
   the server's automatic prefix caching.
3. **The prefix is not byte-stable.** Tool schemas are re-fetched per turn via `tools/list`, so a
   momentarily unreachable connector silently drops its tools from that turn's schema block. Tools
   sit at prefix position 0, so one flapping connector invalidates the entire prefix — for that
   turn and the next. Current guidance is explicit that naive full-context caching can *increase*
   latency; stability of the cached prefix is the precondition, and nothing holds it today.

Caching the tool schemas requires either an upstream change or bypassing MAF's tool serialization.
Recorded, not attempted.

Also open: `chemclaw_tokens_total` collapses input and output into one scalar before the counter
sees it, so the number is priced-blind by construction. Cache-read/write tokens are not read at
all. There is no cost accounting anywhere (AG-11, still open). MAF already implements the full
GenAI token model — input/output/cache-creation/cache-read, split by type — reachable through the
OTel pipeline that, until this review, could not start.

---

## 4. Monitorability

**Nothing scrapes `/metrics`.** No ServiceMonitor, PodMonitor, or scrape annotation anywhere under
`deploy/` — only a prose mention in `values.yaml`. Every metric in the system is currently
uncollected in production. This is the cheapest high-value fix in the review and is not yet done.

The metrics module itself is well-built: 14 counters, 2 histograms, 7 callable-bound gauges. Two
structural limits:

- **No labels, anywhere.** Storage is `dict[str, float]` keyed by bare name. So "20 % of turns fail
  on a provider 400" is indistinguishable from "20 % fail because a tool broke"; the two distinct
  409 causes fuse into one number; and no per-model, per-profile or per-actor attribution is
  possible.
- **Two declared counters are never incremented** — `chemclaw_jobs_started_total`,
  `chemclaw_notes_proposed_total`. They render a permanent `0`. The gauge path explicitly refuses
  to do this ("a fabricated zero would be indistinguishable from a genuinely idle service");
  counters get no such protection. So a PR-gate that fails every note write, or a durable job
  subsystem that launches nothing, looks identical to a quiet one.

**Failure modes with no signal at all**, i.e. invisible indefinitely: audit-sink failures inside
any Temporal worker (the metric exists but is front-door-only, and `agents/audit.py` wraps the
import in `except Exception: pass` because `service` is unimportable there); tool error rate
(success and failure share one unlabelled histogram); durable job success/failure; PR-gate write
failures; anything at all happening inside a connector or worker process, none of which serve
`/metrics`.

**Correlation.** `correlation_id` is per turn and joins turn → tool correctly. It is **not**
propagated to connectors (the identity headers carry actor/roles/session/dry-run only), **not** in
`ConnectorJobInput`, and **not** into HPC. Adding it is roughly a four-line change and makes the
audit trail joinable across all four runtimes. Note that fixing OTel does not fix this: nothing
attaches `correlation_id` to a span or propagates `traceparent`, so `trace_id` and `correlation_id`
would remain disjoint identifier spaces.

**Turn TTFT cannot be computed** from anything shipped — there is no first-token observation. For
an SSE product that is the headline user-facing latency number.

---

## 5. Configurability

The config is genuinely good — one `pydantic-settings` source, ~215 fields, `extra="forbid"`, nine
cross-field validators catching real half-configurations. The problems are at its edges.

**Code↔Helm divergence is a defect class, not a defect.** The OTel crash and the harness gate are
both instances. `tests/test_helm_chart.py` constructs `Settings(**helm_values)`, which proves a
value is well-typed and nothing about whether it works. Two structural holes: keys injected by
`templates/config.yaml` (`note_repo_dir`, `connector_urls`) are outside the parity test entirely,
and there is no inverse test asserting a production value is exercised anywhere.

`harness_enabled` is the sharpest case: `false` in code and every test, `true` in production. The
production agent-construction path has never been exercised by a live model — the 50-user load test
ran on the classic path. `DEFERRED.md` justifies accepting a known harness limitation on the
grounds that "`harness_enabled` is off by default"; the shipped chart sets it true, so that
rationale no longer holds for the deployed configuration.

**Dark by default, and arguably wrong:**

- `budget_enabled` off — the only ceiling above the per-turn loop cap. The load test that validated
  the system ran with **budgets on**; the chart deploys them off.
- `audit_verify_enabled` off — the tamper-evident hash chain exists and nothing ever verifies it.
  For a GxP system that is a gap in the control, not a tuning choice.
- `connectors_required` off — a pod serves with a silently reduced tool surface. Worth considering
  `true`: the chaos run shows a killed connector fleet still returning HTTP 200.

**Reads as implemented, is not:** `deployment_revision` can never be non-`"unknown"` in production —
no chart key, Containerfile ARG, or build step sets it, though its docstring claims the image build
injects the digest. AG-14 (tie a GxP result to the version that produced it) is therefore unmet
while appearing done.

**Silently accepted footguns** with no validator: `session_store="memory"` with
`uvicorn_workers > 1` (the config comment says "this must stay 1"); `mid_turn_resume_timeout ≥
turn_timeout` (the comment says "must stay below"); `budget_enabled` with all four caps zero;
`embedding_dim` disagreeing with the `vector(N)` column.

**Doc-code disagreements:** `harness_max_loop_iterations` is 25 in code, 15 in
`docs/harness-konzept.md`; the same line claims the cap applies only in execute mode, and the code
applies it unconditionally.

---

## 6. Reliability — remaining open findings

Ordered by severity. All source-verified unless noted.

1. **CREST jobs heartbeat once against a 600 s timeout.** `run_cached_ensemble` and
   `run_cached_interaction` — the only two jobs marked `expensive: true`, whose own manifest says
   their cost "is not bounded by the input's size" — have no `progress` parameter at all, so the
   fix is plumbing rather than a kwarg. A CREST search over 10 minutes is declared dead and retried
   up to 5 times, each restarting from zero because the store is written only on completion: ~50
   minutes of saturated CPU spent to fail a job that would have succeeded. A third instance sits in
   `calc/reaction.py` at `level="thorough"`.
2. **Postgres after-run compaction is a silent no-op.** MAF's `CompactionProvider.after_run` reads
   `session.state[source_id]["messages"]`, whose only writer in all of MAF is
   `InMemoryHistoryProvider`. `PostgresHistoryProvider` writes rows and never touches state, so
   after-run compaction returns early. Production is `session_store=postgres`. Consequence:
   `session_messages` is read with no LIMIT on every turn, so a long-lived session re-reads and
   deserializes its entire history before every model call — O(all turns so far), forever. The
   docstring promises the opposite ("shrink the persisted history so the next turn starts
   smaller"). Retention prunes by age and is off by default, which does not bound one long session.
   *Worth a live check before acting.*
3. **`open_reachable`'s return value is discarded by all four callers.** Its docstring says the
   unreachable-connector list is "for the caller to surface". Nothing surfaces it. A turn proceeds
   with a silently degraded capability set and neither the model nor the chemist is told — in a GxP
   setting an answer produced while the safety connector was down is byte-identical to one produced
   with it up. Worse in `template_activities`, where the output enters the PR-gate with no marker.
4. **Job→session push-back is at-most-once.** Rows are marked `consumed_at` by the claiming UPDATE
   before any is yielded, so a consumer that goes away between claim and yield loses them
   permanently. A 4-hour CREST job finishes, the tab was closed during a rolling deploy, the
   chemist is never told. The result stays durable; the notification does not. The digest subsystem
   chose at-least-once for the same problem and nothing records that the two differ.
5. **CHAOS-1 root-caused, and `BACKLOG.md` names the wrong object.** The blocker is not the
   in-process `active_turns` set — `discard` is synchronous and runs before the await. It is the
   60 s `session_turns` lease: `_release_turn_claim` catches `RuntimeError`, which is exactly what
   Python raises when a closing async generator awaits something that suspends, and the DB
   round-trip always suspends. The measured 63 s matches
   `service_turn_claim_lease_seconds` = 60.0. This also explains why the recorded fix attempt
   failed: a detached task created inside a generator being torn down, with no strong reference
   held, was garbage-collected before running.
6. **Retrieval metrics are outside drift detection.** `retrieval_recall` and `retrieval_precision`
   — the only metrics that run a live retriever, and `retrieval_recall` is gated — are absent from
   `evals/baseline.json`. `detect_drift` iterates the baseline, so they get zero coverage.
   *Verified empirically: collapsing both to 0.0 produces no alert.* Easy to miss because the
   generic `precision`/`recall`/`f1` are present. The guard test asserts `check_eval_drift() == []`,
   which an incomplete baseline satisfies trivially.
7. **`find_job` does filesystem I/O inside workflow code**, and the comment above it says the
   lookup is I/O-free. `@cache` pins it per process, so exposure is across processes. The failure
   is worse than a clean non-determinism error: `ConnectorError` is a `ValueError`, not a
   `FailureError`, and no `failure_exception_types` is declared — so it fails the *workflow task*,
   which Temporal retries indefinitely. The run hangs rather than failing. No test ever constructs
   a `JobStep`, so this line is never reached from workflow code under test.
8. **Prediction calibration pools every calculator version.** `calc_version` is never passed when
   recording, defaults to `""`, and the unique index degenerates to `(calc_type, input_hash)` — a
   v2 prediction destroys v1's row. The read path has no version predicate either, so a correct
   caller alone would not fix it. `calculator_trust` reports the pooled figure to the chemist.
   Dormant: `calibration_enabled` is off — which is exactly when the numbers start being quoted.
9. **Rehydrated and LRU-evicted sessions revert to the default profile**, permanently for the
   session's life. The profile is never persisted (`grep -i profile infra/sql/` — zero matches).
   Widening, not escalation, since authz still applies at call time. The eviction path matters more
   than restart: `_LiveSessions` is capacity-bounded with no TTL, so session 1001 evicts session 1
   on a busy pod. All three rehydration tests use `agent_factory=lambda _profile: _FakeAgent()`,
   discarding the argument under test.
10. **The Anthropic client ignores `llm_timeout_seconds`, `llm_max_retries` and the CA bundle** —
    the entire branch is `AnthropicClient(model=...)`. Actual timeout is the SDK's 600 s, not the
    configured 60 s. Not the Helm production path, but it is the default for CLI and dev.
11. **Every tool call takes a single global Postgres advisory lock, inline**, on a fixed key, so
    audit-append throughput is O(1) in replicas. Not a bottleneck at current load (~3 appends/s
    against an estimated few-hundred/s ceiling); recorded with that number because the correct fix
    has GxP consequences and needs an ADR, not a patch.

---

## 7. Leads that were refuted

Kept because a lead that looked right and was not is the more useful record.

- **`make eval` exits 0 on a failed gate.** True, deliberate, documented, and pinned by
  `test_cli_reports_failing_gate_but_exits_zero`. The compensating control — pytest asserting
  pinned per-case values — is complete. Not a defect. (The *baseline* gap in §6.6 is real and is a
  different thing.)
- **`BAD_DATA_RETRY`'s class-name list is unguarded.** Refuted:
  `test_every_chemclaw_error_subclass_is_listed_non_retryable` walks every module of 14 packages
  and closes recursively over `ChemclawError.__subclasses__()`. Residual, Low: the guard closes
  over `ChemclawError` only, so `AuthorizationError`, `ConnectorError` and five others are outside
  it — a denial in a template step is retried 5× identically.
- **The calc store serves stale numbers across calculator upgrades.** Refuted: `calc_version`
  shells out to the real binaries (`xtb --version`, `crest --version`, tblite/RDKit builds), so an
  upgrade is a cache miss. The `provenance` overwrite is literally true but inert — no production
  path writes a non-default provenance, and there is no actor column to lose.
- **The `deepcopy(session.state)` per turn is expensive.** Only under `session_store=memory`;
  production is Postgres, where the state is small. Dev/CLI cost only.

---

## 8. What to do next, in order

1. **Close the approval gate** (§1). The only finding where the system's central GxP claim is false
   in the shipped configuration. Needs an ADR.
2. **Add a scrape target** (§4). Every metric is currently uncollected; this is a few lines of
   chart.
3. **Plumb `progress=` into the two CREST jobs** (§6.1). Strictly less compute *and* strictly fewer
   spurious failures.
4. **Add the retrieval metrics to `evals/baseline.json`** (§6.6), and make `save_baseline`
   reachable from a Makefile target so it cannot drift again.
5. **Decide the caching strategy** (§3) — including whether to carry a patch or an upstream change
   for tool-schema breakpoints, and how to make the prefix byte-stable first.
6. **Split the token counter** into input/output/cache and add labels (§3, §4), or lean on MAF's
   GenAI semconv now that the OTel pipeline can start.
7. **Propagate `correlation_id`** to connectors and `ConnectorJobInput` (§4).
8. **Grow the chart parity test** to read `templates/config.yaml` and to assert that production
   values are executed, not just constructed (§5). That is the test class that would have caught
   two of this review's three Criticals.

---

## Appendix — on method

Three findings came from running the system rather than reading it, and would not have been found
any other way: the `temperature` 400 (capture the real request), the OTel crashloop (execute the
lifespan), the 5 s connector cap (drive a real server with a slow tool). Two more came from
reproducing a hypothesis rather than reasoning about it: the pool deadlock and the client leak.

The general lesson is the one `tasks/lessons.md` already records in other words — *green tests
prove the paths you thought of*. The specific one is narrower and worth adding: **a shipped default
is a claim about the world, and the only way to check a claim about the world is to run it.**
