# Rebuilding layer 1 on LangGraph

Prompted by: asking what it would cost to replace MAF with LangGraph. The answer is ~11–14 weeks,
and the reason to do it is not capability but defect load — four workarounds in this tree exist for
MAF bugs, two of which were silent (`agent_pool.py` for the 8/8 concurrent-turn failure, and the
`require_per_service_call_history_persistence` fix for the streaming defect that meant harness mode
*never worked* while every unit test passed).

Decided in [`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`](../docs/decisions/D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md)
(supersedes D-013/D-038/D-040/D-151, amends D-002's implementation) and
[`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`](../docs/decisions/D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md).
Full phase detail in the approved plan; this file tracks state.

**Not a port — a rebuild.** Five things that are hand-built today become framework features, each
one deleting chemclaw code (≈1,300 LOC removed against ≈3,000 rewritten). Everything lands behind
`CHEMCLAW_AGENT_ENGINE` (`maf` | `langgraph`) with both engines green, so an unfinished engine is
never what a deployment gets.

**Go/no-go is the end of M4.** M5 onward is the commitment.

## Plan

### M0 — decision record and dual-engine seam
- [x] Two ADRs + their ledger rows in `docs/decisions/README.md`.
- [x] `agent_engine: Literal["maf", "langgraph"] = "maf"` in `core/config/agent.py`, documented in
      `.env.example` (`test_config.py`'s three env-example gates).
- [x] Dependencies and the `_STACKS` rows **deferred to M1**, not skipped: `pyproject.toml` states
      deps are added "only when a module actually imports them", and
      `test_third_party_layering.py` pins rows in *both* directions
      (`test_no_declared_module_stack_is_stale`), so an allow-list row cannot precede its import.
- [x] `make lint type test` green; default engine `maf`; zero behavioural change.

### M1 — the engine skeleton and the LLM seam (M2 merged in)
- [x] `agent/langgraph_agent.py` — `build_langgraph_agent()` over `create_agent`, same
      instructions/tools/profile narrowing as the MAF agent. **Not** `agent/graph.py`: in this
      codebase *the graph* is the knowledge graph (`kg/graph.py`, whose own `build_graph` builds
      the NetworkX index), and two unrelated `build_graph`s one import apart is exactly the name
      collision `ARCHITECTURE.md` exists to prevent.
- [x] `instructions_for(profile)` extracted in `chemclaw_agent.py` — third caller of a fallback
      rule the file already records as having drifted once when duplicated.
- [x] `llm_provider.build_chat_model()` beside `build_chat_client()` (M2, merged: a graph cannot
      be built without a model). Shared endpoint/credential/route/transport, shared
      `_require_anthropic_key`.
- [x] Deps into `pyproject.toml` + `_STACKS` + the `("chemclaw.agent", "langgraph")` row.
      `langchain_openai`/`langchain_anthropic` map to the **`llm`** stack, not the framework's —
      they are provider SDK wrappers, and labelling them otherwise would let any package holding
      the framework row build a model client.
- [x] `tests/test_langgraph_agent.py` — the loop runs a tool call to a final answer, the surface
      equals the registry, a profile strictly narrows.
- [ ] `agent/state.py` — **deferred to M5**, the first phase with a field to put in it
      (`plan_hash`, `approvals`). A state schema whose every field is unread is a stub.
- [ ] `agent/harness_types.py` deletion — **moved to M13**. `loop_cap.py` and `plan_gate.py` still
      import it, and those are the MAF path, which stays live until the branch is deleted.

### M3 — tool middleware chain
- [x] Six `@wrap_tool_call` wrappers, attached in the MAF chain's nesting order (converters
      outermost → audit → authz → dry-run → repeat → announce innermost).
- [x] **The decisions extracted first, the plumbing ported second.** `tool_authz.dry_run_refusal`,
      `.denial_result`, `.domain_error_result`, `.failure_detail`; `repeat_guard.count_call`;
      `audit._recording` (an `asynccontextmanager` holding the whole GxP trail — identity
      precedence, span, latency, the three outcomes, the shielded cancelled-write). Both engines'
      middlewares are wrappers over these and nothing else, so a refusal's wording, a repeat
      threshold or an audit row cannot depend on `agent_engine`.
- [x] `agent/authz.py`, `audit_store.py`, `audit_anchor.py` untouched — already framework-free.
- [x] Tests drive real turns through the compiled graph and assert what the *model* is handed;
      they deliberately do not restate the decisions, which are already pinned against the shared
      functions in `test_tool_authz.py` / `test_repeat_guard.py` / `test_audit.py`.
- [ ] `enforce_plan_approval` is the seventh and belongs to M5 — it reads plan/session state this
      engine does not have yet.

### M4 — skills · **go/no-go: GO**
- [x] **The load-bearing unknown is resolved.** Narrowing *is* expressible at the backend, and the
      read half genuinely closes. `SkillsMiddleware` discovers via `backend.ls()` and bodies are
      read via `backend.read()`, so one predicate bounds both.
- [x] `agent/skill_backend.py::NarrowedSkillsBackend` — `ls`/`read`/`glob`/`grep` all gated, the
      write half refused outright, `virtual_mode=True`.
- [x] `skill_access.skill_permits()` — the three narrowings as one engine-neutral predicate; the
      MAF `SkillsSource` decorators and this backend call the same three `permits` methods.
- [x] `langgraph_agent.skills_backend()` / `_skills_middleware()`, multi-root via `CompositeBackend`
      (the configured tree *plus* every connector bundle's own, D-118).
- [x] `tests/test_skill_backend.py` — eight tests, each asking "is it hidden" *and* "is it
      unreachable"; plus a cross-engine test that both engines narrow identically.
- [x] **The read tool.** One hand-written `read_file` bound to the profile's narrowed backend, not
      deepagents' `FilesystemMiddleware` — that would have registered write/edit/glob/grep/execute,
      a general filesystem capability acquired as a side effect of wanting to read a `SKILL.md`,
      every name of which `available_tool_names()`, the prose contract, `tool_role_gates` and the
      safety rubric would then have to answer for. Progressive disclosure needs one verb.
- [x] **The name is pinned to deepagents' own prompt**, not chosen: `SKILLS_SYSTEM_PROMPT` tells
      the model to "use `read_file` on the path shown", so any other name leaves every skill
      advertised and unloadable — a failure indistinguishable from a model declining to use skills.
- [x] **`skills_metadata` staleness fixed** by `ReloadingSkillsMiddleware`, which hides the cached
      key from the state upstream's `before_agent` reads. (The first attempt — a hook returning
      `{"skills_metadata": None}` — left the *key* present and rendered an empty list from turn two
      on; see the review section.) Proven with a real `InMemorySaver` across two turns of one
      `thread_id`, and proven load-bearing by mutation. This is what makes the engine match MAF,
      where `RoleScopedSkillsSource._permits` is consulted on every `get_skills`.
- [x] `skill_tool_names()` returns **both engines' names unioned**, not a branch on
      `agent_engine`: its four callers are validators asking a deployment-wide question, so a
      branch would make `make prose-validate` pass or fail depending on which engine happened to be
      configured (D-117 is the precedent — three validators once unioned two of four name spaces).
- [x] `build_langgraph_agent(checkpointer=...)` accepted early, because the behaviour under test
      only exists once state survives a turn. A fix whose proof waits for a later phase is a fix
      nobody has checked.

### M5 — the plan gate
- [x] `agent/state.py` — `ChemclawState(PlanningState)` with `awaiting_jobs`. `Todo` is
      `{content, status}` with **no description field**, which is where MAF's `awaiting-job:`
      marker lived, so promoting it is forced rather than merely tidier — and the gate's exclusion
      becomes structural instead of a string parse.
- [x] `TodoListMiddleware` attached; `write_todos` is the plan the gate reads.
- [x] The decision extracted: `plan_gate.plan_identity`, `.approval_stands`,
      `.plan_approval_refusal`, `.gated_call`. Both engines bind an approval to the same hash —
      the one divergence that would be *retroactive*, silently invalidating decisions a chemist has
      already recorded.
- [x] `lg_enforce_plan_approval` reads the plan from `request.state["todos"]` rather than an
      ambient session object, so it asks the plan as it stands at this instant — the property
      `plan_is_approved` had to arrange deliberately under MAF.
- [x] `harness_tool_names()` — a fifth name space in `available_tool_names()`, derived from
      `TodoListMiddleware().tools` rather than spelled out (D-117 again).
- [x] The `mode_set` retraction is simply absent: the tool is never exposed, so there is nothing
      to retract. `harness_mode.py`'s subclass-and-mutate has no counterpart here.
- [x] **Loop cap as an explicit counter.** `ChemclawState.model_calls` + `lg_loop_cap`
      (`@before_model`), which both enforces the cap and records it; `loop_cap.loop_capped` reads
      the record. Fixes MAF's blind spot at `harness_max_loop_iterations == 1`, tested at exactly
      that value and mutation-checked.
- [x] `interrupt()` — **dropped from M5 with reasons; see the corrections below.** Neither
      remaining "gate" is in-turn, so there is nothing left for it to unify here.

### M6 — durable state on the checkpointer
- [x] `agent/message_migration.py` — the pure MAF-payload → LangChain conversion, tested against
      payloads produced by MAF itself rather than hand-written dicts. Refuses an unknown role or a
      result with no `call_id` instead of guessing.
- [x] `infra/sql/043_session_message_shape.sql` + the inventory row — a per-row `message_shape`
      stamp defaulting to `maf`, so a non-atomic rollout has no ambiguous rows and the original
      stays readable until the conversion is trusted on real data.
- [ ] `AsyncPostgresSaver`; `session_messages` demoted to a read-model projection.
- [ ] Delete the rollback watermark, the mid-turn-resume wait loop, and `message_pairing.py`'s
      orphan repair — **after** a kill-mid-turn test proves the checkpointer never half-writes.
- [ ] Keep `SessionOwnerStore` / `SessionTurnClaims` (chemclaw policy, not framework concern).
- [ ] `agent/message_migration.py` + a per-row shape version so both forms read during rollout.
      *The one irreversible step.*
- [ ] Check whether `agent_durable_compaction_enabled` collapses into the normal path; if so delete
      it, its `.env.example` rows and D-151's `DEFERRED.md` row in the same commit.

### M7 — connectors and per-turn tools
- [ ] Re-base the degrading MCP connectors on `langchain-mcp-adapters`.
- [ ] Compile the graph **per turn** — LangGraph binds tools at construction, and a connector
      connection must belong to exactly one turn. Measure against MAF's ~90 ms baseline.
- [ ] `durable/template_activities.py`: replay through the ported `wrap_tool_call` chain.
- [ ] Delete `agent/agent_pool.py` + test + the D-123 `DEFERRED.md` row — gated on M12's probe.

### M8 — streaming and the event contract
- [ ] `graph.astream(stream_mode=["messages","updates","custom"], subgraphs=True)`.
- [ ] **Delete `core/turn_signals.py`** — the contextvar side-channel exists only because MAF had no
      custom stream.
- [ ] `api/events.py` gains agent attribution + a `handoff` event. Sequence the cross-repo change
      `Chemclaw3_mock` → `Chemclaw3` → `Chemclaw3_ui`.

### M9 — agent teams
- [ ] Five specialists (`evidence`, `computation`, `design`, `safety`, `reporting`) as profiles +
      subgraphs; supervisor routing via `Command(goto=…, graph=Command.PARENT)`.
- [ ] The four invariants of the subagent ADR, each as a test — attenuation-only, `require_actor`
      inside subagents (**verify identity propagation first**; deepagents #569), audit records the
      specialist beside the human, skills do not inherit.

### M10 — `Send` fan-out
- [ ] `gather_evidence` becomes real map-reduce, one branch per source into an `operator.add` field.
- [ ] Re-measure the retrieval balance from `D-2026-08-01-a-cap-that-starves-a-source` per branch.

### M11 — long-term memory on `BaseStore`
- [ ] Map `chemclaw/memory/` onto `BaseStore` over the deployed pgvector. `Store` is memory, not
      knowledge — the PR-gate still stands between an agent and layer 4.

### M12 — live re-validation
- [ ] Concurrency probe (8 turns × 3 configs) — gates the `agent_pool` deletion.
- [ ] Durable-launcher probe — `CapabilityDegradedEvent` still precedes the first token.
- [ ] Plan → approve → execute, live. *This is the one that historically silently did not work.*
- [ ] Team routing accuracy + per-specialist token cost vs. the single-agent baseline.
- [ ] `make eval-strict` scored against the MAF baseline.

### M13 — remove MAF and update the documents
- [ ] Drop `agent-framework-*`, the `maf` stack rows, and the `agent_engine` switch with its branch.
- [ ] ~135 mentions across maintained docs; `docs/archive/` is not maintained — leave it. ADRs are
      append-only.
- [ ] Verify whether session affinity is still required at all.

## Review

_(filled in as phases land)_

**M0.** Two ADRs, the config switch, this file. One thing changed from the approved plan: the
dependency and `_STACKS` work moved to M1. Both `pyproject.toml`'s own comment ("only when a module
actually imports them") and `test_no_declared_module_stack_is_stale` forbid declaring a stack before
the import exists — the layering policy is pinned in both directions precisely so a row cannot sit
there re-blessing an import nobody has written yet. Landing them with `agent/graph.py` is the
sequencing those two rules already required.

The gate run also surfaced an unrelated pre-existing bug (confirmed against the clean tree, not
introduced here): Hypothesis drew a note body of `"\ud800"` and `Path.write_text` raised
`UnicodeEncodeError`. An unpaired surrogate is reachable — an agent-authored note arrives as JSON,
which can carry one — and it breaks the PR-gate commit, the proposal store and the index refresh
alike, so `Note` now refuses unencodable text at the schema boundary. Fixed in its own commit
(`ca37353`) to keep this one clean. `make lint type test` green: 3913 passed, 0 failed.

**M1.** The engine builds and runs; `make lint type test` green at 3921 passed, 0 failed.
Four things went differently from the plan, all of them discovered by doing it:

- **M2 merged in.** A graph cannot be compiled without a model, so the LLM seam landed here.
- **`state.py` deferred to M5, `harness_types.py` deletion moved to M13.** Both for the same
  reason the M0 deps moved: a state schema with no field anyone reads is a stub, and
  `harness_types.py` is still imported by `loop_cap.py`/`plan_gate.py` on the live MAF path.
- **Named `langgraph_agent.py`, not `graph.py`.** See above — `build_graph` was already taken by
  the knowledge graph.
- **A real mistake worth recording:** `tests/test_graph.py` already existed (23 tests for the
  NetworkX indexer) and was overwritten before the collision was noticed. Caught by comparing
  collected-test counts across the change rather than by the suite, which passed either way —
  a deleted test file cannot fail. Restored from `HEAD` and verified byte-identical; the new
  tests live in `tests/test_langgraph_agent.py`. The lesson is in `tasks/lessons.md`.

Measured while building, and both change later phases:

- `deepagents.SkillsMiddleware` registers **no** skill tools. It puts skill *paths* in the system
  prompt and expects the model to `read_file` them, so progressive disclosure depends on a
  filesystem tool over the same backend. That makes M4's custom backend load-bearing twice: it
  narrows what is *listed* and it bounds what can be *read*. An unscoped `FilesystemBackend` would
  hand a GxP system a general file-read primitive — the opposite of what D-038 bought by disabling
  MAF's file batteries. `skill_tool_names()` has no direct equivalent and must be re-derived.
- `SkillsMiddleware.before_agent` caches `skills_metadata` in state and skips the load when it is
  already present "from a prior turn or checkpointed session". With M6's checkpointer that means
  role-scoped narrowing is computed once per session — a role change mid-session would read stale.
  Needs an explicit answer in M4, not a discovery in M6.

**M3.** Six middlewares on the graph engine; `make lint type test` green at 3926 passed, 0 failed,
nothing lost (collected ids diffed again).

The shape that mattered: **extract the decision, then port the plumbing.** Every one of these
middlewares turned out to be one sentence of policy inside one framework's calling convention, and
the sentences are all load-bearing — a dry-run refusal's wording is what tells a chemist nothing
ran, the repeat threshold encodes a measured finding (7–8 identical calls, 128–142 s against
16.9 s), and the audit trail's `cancelled` outcome exists because a subtle omission in it went
unnoticed until D-130 measured it. Porting those by hand would have produced a second copy free to
drift, and an audit trail that disagrees with itself depending on a config flag is not a trail.

`audit._recording` is the one that justified the effort: ~90 lines of identity precedence, span,
latency histogram, three outcomes and a shielded write that must survive a teardown. It is now an
`asynccontextmanager` both engines wrap, and each engine supplies exactly three things — the tool's
name, its arguments, and its result.

Two differences worth recording rather than smoothing over:

- **`lg_surface_authorization_denials` exists for a different reason than its MAF twin.** MAF
  collapses every tool exception into "Function failed." with no text, so there the converter
  *recovers* a message the framework discarded. LangChain does not do that, so here the converter
  instead keeps a deliberately-worded refusal from being reported as a tool error the model might
  retry. Same behaviour, different justification; the docstring says so rather than implying the
  MAF rationale still applies.
- **A LangChain gate must echo `tool_call_id`.** An assistant `tool_use` block with no matching
  `tool_result` is a malformed exchange the provider rejects, so a refusal that forgot the id would
  turn a blocked call into a dead turn. `_refusal_message` is the one place that is handled.

Also fixed while testing: `dry_run_refusal` reads the ambient flag, so asking it *outside* the
dry-run block returns `None` — correct answer, wrong question. And most of the side-effecting
surface does not exist in the registry until something has assembled the toolset once, because
that is when the generated connector-job and template launchers register.

**M4 — go/no-go: GO, with two pieces still open.** `make lint type test` green at 3938 passed, 0
failed, nothing lost.

**The question M4 existed to answer.** Can the three narrowings — deployment enablement, capability
scoping, role gating — be expressed against a backend rather than a `SkillsSource`? Yes.
`SkillsMiddleware` discovers skills with `backend.ls(source)` and their bodies are read with
`backend.read(...)`, so a single `permits(skill_name)` predicate bounds both halves.

**And the reason it had to be the backend rather than the listing.** Under MAF, narrowing the
advertised list *was* the gate — `SkillsProvider` was the only route to a body. deepagents puts
each skill's *path* into the system prompt and expects an ordinary filesystem tool to fetch it, so
a listing-only filter would hide a role-gated skill and then hand it over to anyone who guessed
`/tree/<name>/SKILL.md` — a shape the prompt has already taught the model. Four reach paths (`ls`,
`read`, `glob`, `grep`) plus their async twins had to be closed, not one.

Three things measured rather than assumed:

- **`virtual_mode=False` is deepagents' default and its own warning says it "allows absolute paths
  and `'..'` to bypass `root_dir`".** In a GxP system that is a general file-read primitive handed
  to a model. The backend sets `virtual_mode=True`; `test_path_traversal_is_refused` pins it.
- **The async twins need no override** — `FilesystemBackend` implements them as
  `asyncio.to_thread(self.read, ...)`, so they dispatch through the subclass. The gate depends on
  that upstream detail completely, so it is a test rather than a comment.
- **`test_every_reach_path_the_protocol_exposes_is_gated` probes the protocol rather than a written
  list**, so a future release adding a reach path fails here instead of opening a quiet hole.

The refusal deliberately does not distinguish a gated skill from a nonexistent one — otherwise the
gate is an enumeration oracle.

**M4 closed.** `make lint type test` green at 3942 passed, 0 failed; `make prose-validate` and
`make skill-validate` both pass. Nothing lost.

The read tool turned out to be the more interesting of the two. The obvious move —
`FilesystemMiddleware`, which exists precisely to give a model filesystem tools — is the wrong one
here: it registers a write/edit/glob/grep/execute surface, and in this system every tool name has
four other obligations (the prose contract, `available_tool_names`, `tool_role_gates`, the safety
rubric). Acquiring a general filesystem capability as a side effect of wanting to read one Markdown
file is exactly what D-038 refused when it disabled MAF's batteries. One verb, bound to the
narrowed backend, carrying no authority of its own.

Its *name* is not a design choice at all, which is easy to miss: deepagents' skills prompt names
`read_file` explicitly, so the tool is only correct if it matches. Pinned against the prompt.

The staleness fix was verified the way this file keeps insisting on: the hook was deleted and the
test watched to fail. Without that check it would have been a test that passes because the caching
never engaged under `ainvoke` without a checkpointer — which is precisely the shape of a green test
that proves nothing.

## Review of M0–M4 (before starting M5)

Six findings, all fixed. Three were real defects, two were tests that proved less than they
claimed, one was a config knob that lied.

**1. The staleness "fix" deleted the skills layer.** `reload_skills_each_turn` returned
`{"skills_metadata": None}` to clear the cache. That writes `None` into the slot but leaves the
*key* present, so `SkillsMiddleware.before_agent` still short-circuited and rendered an empty list:
measured, **28 skills on turn one and 0 on every turn after**. Worse than the staleness it
replaced. Replaced by `ReloadingSkillsMiddleware`, which hides the key from the state its
`before_agent` reads, so the load actually runs. Verified by mutation: reintroducing the staleness
fails the test, and forcing an empty listing fails it too.

**2. The test that caught it could not have caught it.** It asserted only "the gated skill is gone
from turn two", which an empty list also satisfies — `set() == set() - {gated}` holds. And its
parser silently matched nothing, so the set comparison was vacuous on both sides. The test now
asserts turn two equals turn one *minus exactly the gated skill*, and `_listed_skills` asserts it
parsed something, so a broken parser is a failure rather than a quiet pass.

**3. A refusal meant different things on the two engines.** `_refusal_message` set
`status="error"`, which reaches Anthropic as `is_error` on the tool_result block. The MAF twin
deliberately makes a denial the tool's *successful* result so the model reads it as the answer
rather than a transient failure worth retrying. Exactly the divergence M3's shared decisions exist
to prevent, sneaking back in through the envelope they were wrapped in. Removed.

**4. `agent_engine` was a knob that did nothing.** Added in M0, documented in `.env.example`,
enforced-as-documented by `test_config.py` — and read by no code at all, so
`CHEMCLAW_AGENT_ENGINE=langgraph` silently served MAF. `build_agent` now refuses, naming the phase
(M8) that makes the selection real. A config value that quietly does nothing is worse than one that
is missing.

**5. Dead parameters.** `skills_middleware(profile, tools, backend=None)` had one caller which
always passed `backend`, making the other two dead and the fallback unreachable. Now
`_skills_middleware(backend)`, private, since `skills_backend` is the seam tests use.

**6. Stale prose.** The engine module still said skills were "deliberately not here yet", and the
test module still said asserting on M3–M4 would be "a test of a plan". Both were true when written
and false when read, which is the failure mode `CLAUDE.md` opens by describing.

Two claims were checked and held, so they are now pinned rather than believed: `wrap_tool_call`
middleware nests in list order (first = outermost, measured), and no `..` path escapes the skills
tree (deepagents refuses traversal outright, so `/alpha/../beta/SKILL.md` raises rather than
resolving past the gate).

**M5 (partial).** The plan gate holds on the graph engine; `make lint type test` green at 3948
passed, 0 failed, `prose-validate` passes.

**A correction to the plan, made while implementing it.** M5 was framed as "collapse the harness
plan gate, `interaction_tools.py` and the KG PR-gate onto one `interrupt()`". Two of those three
belong together and the third does not, and the difference is their lifecycle rather than their
shape:

- The plan gate and the interaction approval are **in-turn**: a human answers in seconds while a
  turn is live, which is exactly what `interrupt()`/`Command(resume=…)` models.
- The **KG PR-gate is not an in-turn gate at all.** It is a git pull request a human reviews hours
  or days later, and D-005 is about a human signing off on a *merge*. Holding a turn — and its SSE
  stream — open across a code review is not a design, it is a leak. Forcing it into `interrupt()`
  would have replaced a durable, resumable, auditable artifact with a suspended coroutine.

So the collapse is two gates, not three, and the PR-gate stays a PR. That is a smaller win than
the plan claimed (~350 LOC was the estimate; the honest figure is lower) and it is the right
outcome.

**And the plan gate itself is deliberately still a refusal, not an interrupt.** Under MAF an
unapproved state-changing call is refused and the human approves out of band; switching the graph
engine to *suspend* instead would be a behaviour change on one engine while both are live, which is
the divergence this migration's whole discipline is against. `interrupt()` becomes the mechanism
when the front door can drive it (M8) and both engines can be cut over together — with its own
decision record, because changing when a chemist is asked is a product change and not a port.

**M5 done.** `make lint type test` green at 3950 passed, 0 failed; `prose-validate` and
`skill-validate` pass; nothing lost.

**The second correction, and it retires the phase's headline.** M5 was "collapse three gates onto
one `interrupt()`". Reading the third one closed the question: `interaction_tools.py` starts an
`InteractionApprovalWorkflow` — a *durable Temporal workflow* that holds a candidate until a click,
built precisely so the turn does **not** wait. Its own docstring says "asynchronous". So of the
three, only the plan gate is in-turn at all; the interaction approval and the KG PR-gate are both
deliberately out-of-turn holds that outlive the turn that raised them.

There is therefore nothing for `interrupt()` to unify. The estimate of ~350 LOC removed by "one
human gate" was wrong, and it was wrong because the plan grouped three things by what they *sound*
like — all three ask a human — rather than by lifecycle, which is the only property that decides
whether a coroutine can be held open across them. The real shared code was the plan-approval
decision itself, and that is extracted.

**What the loop cap turned into.** `ModelCallLimitMiddleware` was the obvious answer and does not
work here: it keeps `thread_model_call_count` (persisted, whole-session) and `run_model_call_count`
(per-turn, *not* persisted), so a checkpointed session's final state carries the wrong one and "was
this turn capped" is unanswerable from it — measured. Enforcing with it and counting again for the
record would be two counters for one number, so `lg_loop_cap` does both. That is a deliberate
departure from "use the framework's machinery", written down where the next reader will ask.

It ends the run rather than raising, matching MAF: the partial answer still goes out and a surface
marks it partial. Raising would discard work a chemist is entitled to see.

**M6 (started).** The two pieces that need no database are in and green (3962 passed). The research
behind the rest was done by three parallel subagents, and it moves M6 in ways the plan did not
anticipate. Six findings, in descending order of how much they change the phase:

**1. The checkpointer would open a GDPR hole.** `agent/leaver.py::_ERASE` deletes a departing
person's `session_messages`, `session_events` and `session_turns`, scoped through `session_owners`,
in a load-bearing order. `AsyncPostgresSaver.setup()` creates **four more tables** —
`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` — holding the same
conversation as graph state. Wiring the checkpointer without adding the first three to `_ERASE`
would leave a departing person's turn state behind an erasure that reports success. This is now the
first item of M6, not a detail of it, and it needs a test that derives the table list rather than
restating it.

**2. The blast radius is not `session_store.py`.** Four other places read `session_messages` in MAF
shape: `durable/retention.py:235` (`Message.from_dict` + `droppable_rows`, inside a *data-destroying*
nightly sweep), `api/schemas.py::_transcript` (the whole HTTP transcript projection, built on
`function_call`/`function_result`/`call_id`), `cli/explain.py` (the GxP "why was this run" trail,
joined on `correlation_id`), and `cli/live_storm.py:890` (`message::text like`). The plan's
"demote to a read-model projection" has to keep all four working, and `explain.py`'s join means
whatever writes the projection must keep writing `correlation_id`.

**3. `setup()` cannot run on Chemclaw's pool.** Migrations 6–8 are `CREATE INDEX CONCURRENTLY`,
which Postgres refuses inside a transaction block, and `core/db.py:142` builds pools without
`autocommit`, so psycopg opens an implicit transaction on first execute. Needs a dedicated
autocommit connection or a checkpointer-specific pool — `_pool_for` already keys by `(dsn, options)`,
so a distinct pool costs nothing structurally.

**4. One `asyncio.Lock` per saver serializes every checkpointer statement**, and `alist` yields
*inside* both the lock and the borrowed connection. A paginated history read would hold a pooled
connection for the whole iteration, on a pool shared with the calculation cache and the vector
index. That is an argument for the separate pool in (3) on its own merits.

**5. A rewind is not a delete, and it becomes the head.** `checkpoint_id` is a UUIDv6, so it sorts
by time; `aupdate_state` on a historical checkpoint writes a *new* checkpoint into the **same
thread**, which immediately becomes that thread's tip. So `rollback_to(session_id, watermark)` does
not map onto "fork and carry on" as cleanly as the ADR implies — measured on a real graph. There is
an `acopy_thread` for forking elsewhere, and the M6 design has to say which of the two a disconnect
should do.

**6. State must be msgpack-encodable.** The serde is `JsonPlusSerializer` over ormsgpack with a type
allowlist, not JSON. `BaseMessage` round-trips exactly (verified, including `tool_calls` and `id`),
and `ChemclawState`'s own fields are `list[str]`/`int`, so nothing is blocked — but a future state
field holding a chemclaw model needs `with_allowlist` or it raises at write time.

**And what this environment cannot do.** There is no Postgres and no docker daemon here, so the
migration rehearsal the plan names as the mitigation for the one irreversible step — running the
converter against a copy of a real `session_messages` — **cannot be performed in this session**.
The pure conversion is exhaustively tested and the shape stamp makes a bad conversion recoverable,
but the rehearsal is still owed before any deployment converts a row.
