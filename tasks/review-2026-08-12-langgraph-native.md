# LangGraph-native audit — custom code vs. the native primitive (2026-08-12)

**Question.** For every piece of hand-rolled infrastructure in the agentic layer: *does LangGraph
already do this, and are we using it properly?* A hand-rolled equivalent of a native primitive is a
finding even when it works, because it is code we own and maintain.

**Method — installed source, not documentation.** The venv is synced and matches `uv.lock` exactly,
so every claim below was checked with `inspect.getsource` against the *actual installed library*,
and the behavioural claims were **run**. Documentation was not trusted; neither was prose in this
repo. Where something could not be settled by running it, it is marked **unverified** explicitly.

| Package | Resolved |
|---|---|
| `langgraph` | 1.2.10 |
| `langgraph-checkpoint` | 4.2.0 |
| `langgraph-checkpoint-postgres` | 3.1.2 |
| `langgraph-prebuilt` | 1.1.0 |
| `langchain` | 1.3.14 |
| `langchain-core` | 1.5.3 |
| `deepagents` | 0.6.12 |

**Audited from zero**, including the three pillars the repo had already declined with written
reasons — because a decline made against an earlier version can go stale, and because a reason that
turns out to be wrong invites a future reader to reopen the decision at the worst moment.

---

## Summary

| # | Custom code | Native equivalent | Verdict |
|---|---|---|---|
| N-1 | `agent/state.py` manual per-turn reset in `turn_input()` | `Annotated[int, UntrackedValue]` | **REPLACE** — ~4 lines, removes a defect class |
| N-2 | `agent/compaction.py` `KeepLastConversationGroupsEdit` | `trim_messages` (a function, not a `ContextEdit`) | **KEEP-WITH-BETTER-REASON — and fix a measured defect** |
| N-3 | `agent/loop_cap.py` | `ModelCallLimitMiddleware(run_limit=…)` | **KEEP-WITH-BETTER-REASON** (see N-1: the mechanism it needed was the channel, not the middleware) |
| N-4 | no `recursion_limit` anywhere | native `recursion_limit` | **ADOPT** — the default permits ~5,458 model calls per turn |
| N-5 | `plan_gate` / `interaction_approval` / KG PR-gate | `interrupt()` + `Command(resume=)` | **KEEP all three — but the ADR's stated reason is false** |
| N-6 | `BaseStore` declined | `BaseStore` / `AsyncPostgresStore` | **KEEP the decline** — the sharpest reason verified TRUE at source |
| N-7 | `agent/audit.py` hash-chained trail | `get_state_history` | **KEEP** — history is *not* a replacement candidate, and conflating them would be a compliance regression |
| N-8 | `agent/repeat_guard.py`, `cached_compute` | `RetryPolicy`, `CachePolicy` | **DO NOT ADOPT** — `RetryPolicy` cannot reach a `create_agent` graph; caching is state-keyed and Redis-only |
| N-9 | nothing | `durability=` | **KEEP the default** (`"async"`) — `"exit"` loses the whole turn on SIGKILL (measured 0 writes) |
| N-10 | stream modes, `_AttributedSpecialist`, `tool_invocation.py`, `ReloadingSkillsMiddleware` | `tasks`/`checkpoints`/`astream_events`, `Send`/supervisor, `ToolNode` | **KEEP — a vindication set**; one stale docstring to correct |

**Score: of 10 audited surfaces, 6 are vindicated or correctly declined, 2 need a better-stated
reason, and 2 are genuine adoptions (N-1, N-4).** That is a good result for the migration, and it is
worth saying plainly: this layer was not built by someone reaching for hand-rolled code first.

---

## N-1 · REPLACE · A native per-run channel exists, and upstream uses it for exactly this

**The custom code.** `agent/state.py:49-66` declares two plain fields on `ChemclawState`:

```python
class ChemclawState(PlanningState):
    model_calls: int
    loop_capped: bool
```

A plain field resolves to `LastValue` (`langgraph/graph/state.py:1860-1885`), which **is
checkpointed** — and `thread_id = session_id` (`api/runner.py:282`), so it persists for the life of
the conversation. `agent/state.py:69-84` `turn_input()` therefore zeroes both by hand on every turn,
and `tests/test_langgraph_agent.py` asserts that every invoke site goes through it.

**This is the mechanism behind a shipped defect.** D-2026-08-12 defect 1: nothing zeroed
`model_calls`, so the "per-turn" cap counted the *session* — turns 0–2 answered and turn 3 onward
returned the chemist's own question having never called the model. Reproduced independently in this
audit: three `invoke` calls on one `thread_id` gave `plain` = 2 → 4 → 6, with a fresh thread
restarting at 2.

**The native primitive.** `langgraph/channels/untracked_value.py:15` —

> `class UntrackedValue(...)`: *"Stores the last value received, **never checkpointed**."*

`checkpoint()` returns `MISSING` (`:47`); `update([])` returns `False` (`:56`), so it survives
supersteps *within* a run but not across runs. **That is per-run semantics, natively.**

**And upstream's own middleware uses it for precisely this counter** —
`langchain/agents/middleware/model_call_limit.py:33-34`:

```python
thread_model_call_count: NotRequired[Annotated[int, PrivateStateAttr]]
run_model_call_count:    NotRequired[Annotated[int, UntrackedValue, PrivateStateAttr]]
```

**Verified to survive `create_agent`'s schema merging** — the real risk, since `create_agent`
composes middleware state schemas. Measured over three turns on one `thread_id` with `InMemorySaver`:

```
CHANNELS: {'messages':'BinaryOperatorAggregate','todos':'LastValue',
           'model_calls':'UntrackedValue','loop_capped':'UntrackedValue'}
 turn 1: hook saw model_calls=[0]   turn 2: [0]   turn 3: [0]
 get_state values keys: ['messages']
```

Zero reset code; the counter starts at 0 every turn and stays readable in the `invoke` output, so
`loop_cap.loop_capped(state)` keeps working.

**Alternatives ruled out, so this is a search and not a guess:**

- **`EphemeralValue`** is per-*superstep*, not per-run (`pregel/_algo.py:325-329` clears every
  un-written available channel each step). Measured: node `a` writes 100, `b` sees 100, `c` sees
  `<MISSING>`. Using it here would zero the counter between every model call and the cap would never
  fire.
- **`Runtime` / `Runtime.context`** is run-scoped but `frozen=True, slots=True`
  (`langgraph/types.py:413`) — read-only, not a writable counter.
- **`RemainingSteps` / `IsLastStep`** count supersteps, not model calls, and are read-only.
- **`@before_agent`** is a valid second native reset point (runs once at graph entry,
  `langchain/agents/factory.py:1592-1594`) and is strictly better than `turn_input()` because a
  caller cannot bypass it — but it still adds a node and a hand-written reset.

**Migration cost: ~4 lines.** `state.py:60,66` become
`Annotated[int, UntrackedValue]` / `Annotated[bool, UntrackedValue]`; `turn_input()` drops the two
zeroes and reduces to `{"messages": [("user", message)]}`.

**The elegance argument, which is the real point:** the invariant moves from *a convention enforced
by a test* to *the schema*. The defect class disappears rather than being guarded — and F-4 (the
mid-turn resume re-zeroing the cap via a second `turn_input()`) stops being expressible.

**Risk: low, with one latent caveat that must be recorded.** A resume is a *new run*, so an
`UntrackedValue` counter resets across `interrupt()`/`Command(resume=)`. Measured: a graph
interrupting at `m2` shows `run=0` on resume after `m1` set it to 1. No `interrupt()` exists anywhere
in `src/`, so this is inert today — but if HITL is ever added mid-turn, the cap resets on resume.
`before_agent` shares this property; only the current `turn_input` scheme does not.

---

## N-2 · KEEP-WITH-BETTER-REASON, and fix a measured defect · The conversation window does not bound

**The literal claim in `agent/compaction.py` is TRUE.** `langchain/agents/middleware/context_editing.py`
defines exactly one `ContextEdit` — `ClearToolUsesEdit` (`:58`) — and says so at `:193`. There is no
conversation-window `ContextEdit` upstream, so a first-party edit is genuinely required. The
`SummarizationMiddleware` decline also stands: a summarizer reads retrieved evidence and replays it
as conversation, which is an indirect-prompt-injection surface.

**But the edit does not do what its docstring says.** `agent/compaction.py:97-150`:

> "It is what makes the thread *bounded* rather than merely cheaper."

`apply` checks the trigger **in tokens** and then cuts by **group count**:

```python
if count_tokens(messages) <= self.trigger: return
starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
if len(starts) <= self.keep: return          # ← 12 huge groups: no reduction at all
cut = starts[-self.keep]
del messages[:cut]
```

Keeping a fixed *count* of groups bounds nothing when the groups are large. Measured at the shipped
defaults (`agent_context_token_budget=100_000`, `agent_keep_last_conversation_groups=12`) over 20
tool-free prose groups of ~12k tokens each — the case where `ClearToolUsesEdit` reclaims exactly
zero:

| | tokens | under trigger? |
|---|---:|:---:|
| before | 240,230 | — |
| after `keep=12` group window | **144,142** | **no** |
| after `trim_messages(max_tokens=trigger)` | 96,096 | yes |

So the failure mode the edit exists to prevent — a hard provider context-limit error — still happens,
and the second early-return means a thread of ≤12 groups is never reduced at all no matter how large.
**It reduces; it does not bound.**

**The fix is native and small.** `langchain_core.messages.utils.trim_messages` (`:1133`) is a
token-based window. Pairing safety was measured against the repo's own
`message_pairing.calls_without_adjacent_results`: `trim_messages(strategy="last", start_on="human")`
produced **zero** orphaned tool calls at every budget from 2000 down to 250 tokens and always cut on
a `HumanMessage`. Without `start_on="human"` it cut mid-group (head = `ToolMessage` at max=900),
which a provider rejects — so `start_on="human"` is load-bearing and the guarantee is then equivalent
to the group-boundary argument the docstring makes.

**Migration cost: ~6 lines inside the existing `apply`**, plus one config decision —
`agent_keep_last_conversation_groups` loses its meaning as *the* mechanism. By this repo's own
precedent (`compaction.py:167-170` on why `agent_keep_last_tool_groups` kept its name), renaming an
ENV-visible knob costs every deployment, so it should become a *floor* rather than be renamed.
**Risk: low** — in-place mutation is preserved as the `ContextEdit` protocol requires, and
`tests/test_compaction.py` already asserts pairing.

---

## N-3 · KEEP-WITH-BETTER-REASON · The loop cap

`agent/loop_cap.py:87-94` declines `ModelCallLimitMiddleware` because, measured against a
checkpointed session, final state carried the thread count and *no run count*, making "was this turn
capped" unanswerable. **That measurement is correct**, and this audit can now name both causes:
`UntrackedValue` keeps `run_model_call_count` out of `get_state()`, and `PrivateStateAttr`
(= `OmitFromSchema(input=True, output=True)`, `types.py:343`) keeps it out of the `invoke` output.

**But those are properties of upstream's field *declaration*, not of the channel.** Declaring
`model_calls: Annotated[int, UntrackedValue]` **without** `PrivateStateAttr` gets the per-run reset
natively *and* keeps the value readable — verified. The repo rejected the middleware and, with it,
discarded the mechanism. Keeping the hand-written enforcement is defensible (one number that is both
the limit and the record, which is a real argument); keeping the hand-written *reset* is not.

The decline should be re-recorded with this correction, per N-1.

---

## N-4 · ADOPT · `recursion_limit` is never set, and its default is not a cost control

`recursion_limit` has **zero hits repo-wide**; the only invocation config passed is
`{"configurable": {"thread_id": …}}` (`api/runner.py:282`). And `enforce_loop_cap` attaches **only**
when `harness_enabled_for(profile)` (`agent/langgraph_agent.py:254-256`), while `harness_enabled`
defaults to `False` (`core/config/agent.py:131`).

**This finding was drafted wrong twice. Both corrections are recorded, because the wrong versions
are the kind a reader would reach independently.**

*First draft:* "a default deployment has no bound at all." **False** — LangGraph applies one.
*Second draft:* "the bound is 10007." **Also false for this repo** — that is the bare-`StateGraph`
default, and `create_agent` overrides it.

Three different defaults are live in this dependency set, and only the third governs here:

| Source | Value | Applies to |
|---|---|---|
| `langchain_core/runnables/config.py:171` | 25 | plain LCEL runnables; `ensure_config(None)` |
| `langgraph/_internal/_config.py:32` | `getenv("LANGGRAPH_DEFAULT_RECURSION_LIMIT", "10007")` | a bare `StateGraph` — measured: `GraphRecursionError: Recursion limit of 10007 reached` |
| `langchain/agents/factory.py:1779` | **9999**, via `.with_config({"recursion_limit": 9_999})` | **every `create_agent` graph — i.e. this repo's agent** |

Verified empirically on a graph built the way the repo builds it:
`agent.config == {'recursion_limit': 9999, …}`.

**What 9999 means in the units that cost money — corrected, and this is the third correction this
finding has needed.** An earlier draft said *"~1.83 supersteps per model call"*, derived by counting
`stream_mode="updates"` events. **That was wrong**: `updates` yields *node* updates, which are not
Pregel supersteps.

Measured properly, by binary search on the minimal `recursion_limit` at which a run of N model calls
completes without `GraphRecursionError`:

| graph | measured | supersteps per model call |
|---|---|---|
| bare `create_agent` | N=1→4, N=2→6, N=5→12, N=10→22 | **2** |
| Chemclaw, harness **off** (the default path) | `2N + 1` | **2** |
| Chemclaw, harness **on** | `4N + 1` | **4** |

The harness adds nodes — `TodoListMiddleware`, `enforce_loop_cap`, skills, compaction — and each
`before_model`/`after_model` hook is its own node.

So the effective ceiling is roughly **5,000 model calls** on the default classic path and **2,500**
with the harness on. Still thousands; the point stands, and the coincidence that 1.83 ≈ 2 made the
wrong derivation look right on one path while being twice wrong on the other.

**Why the exact number matters rather than being pedantry:** a `recursion_limit` derived from 1.83
would sit *below* what a healthy harness turn needs (a 25-iteration turn needs 101 supersteps, and
1.83 × 25 ≈ 46), so the "fix" would have started truncating good turns. Any derived limit must use
the measured 4, with headroom, because the constant is the *middleware count* and changes whenever a
middleware is added.

Three consequences:

1. **It is not a cost control.** One runaway turn on a default deployment can make thousands of
   billed model calls before anything stops it.
2. **It fails by raising, not by degrading.** `GraphRecursionError` propagates; the chemist loses the
   partial answer. That directly contradicts the design choice `loop_cap.py:101-103` makes
   explicitly — *"the answer the last iteration managed still goes out… a raised error would discard
   work a chemist is entitled to see."* The repo has a considered position on this and does not apply
   it on the default path.
3. **Nobody in this repo chose it.** 9999 is a number `create_agent` picked as a
   "don't get in the user's way" backstop, inherited silently. A deployment's runaway ceiling should
   be a declared fact, not an upstream default that a minor release can move.

**Recommendation: set `recursion_limit` explicitly in the invocation config**, derived from
`harness_max_loop_iterations` (or its own setting), on every entry point. Additive, cannot regress a
working deployment, and it makes the bound a declared fact rather than an inherited default.

Note this also fixes the conditional-attachment problem *without* re-litigating it:
`_harness_middleware`'s docstring justifies conditionality as *"matching MAF … while both are live"*
(`agent/langgraph_agent.py:245-249`), and MAF was removed in M13 — the dual-engine period that
motivated it has ended, so the conditional's stated reason has expired.

---

## N-5 · KEEP all three gates — but the recorded reason is false

The repo has three hand-rolled human-in-the-loop mechanisms and uses `interrupt()` nowhere:

| | What it is | Durability | Lifetime |
|---|---|---|---|
| **A. Plan gate** (`plan_gate.py:295`, `plan_approval_store.py`, `routes/plan.py`) | **Not a pause.** A `@wrap_tool_call` that raises `PlanNotApprovedError` (an `AuthorizationError`). Fails closed; the human approves out-of-band and the chemist re-asks | Postgres `plan_approvals` | one turn, spent by `consume_turn_approval` |
| **B. Interaction hold** (`durable/interaction_approval.py:72`, `interaction_tools.py`) | A true durable pause — Temporal `wait_condition` on a `decide` signal, 7-day bound | Temporal history | until decided or expiry |
| **C. KG PR-gate** (`kg/pr_gate.py:71`, `proposal_store.py`) | **Not a pause.** Returns a reference immediately | Postgres + git branch | unbounded |

**The ADR's stated reason is measurably false.** `D-2026-08-11-a-policy-nobody-can-see…:25-26` says
`interrupt()` is declined because *"a PR-gate review takes days and an SSE stream cannot be held open
across one."* But `interrupt()` does **not** hold a stream open: `invoke()` **returns normally** with
`__interrupt__` in its output. Measured across two processes — process A invoked, received the
interrupt as an ordinary return value, and exited; **process B, a fresh interpreter with a newly
compiled graph and no shared memory, resumed the thread with `Command(resume=…)` and ran to
completion.** Cold-process resume works.

**The conclusion nevertheless holds, for three better reasons — each checkable:**

1. **The thread is the session.** `thread_id = session_id`, so a pending interrupt would live on the
   chemist's conversation. Measured: an ordinary new input on that thread **silently discards** the
   pending interrupt, and a later `Command(resume="yes")` becomes a **silent no-op** — no error, and
   the approved work never runs. Across a days-long review that is near-certain.
2. **Retention would delete the record.** `durable/retention.py:138` prunes the checkpoint tables by
   thread. The resume value — the sign-off itself — lives in `checkpoint_writes`. A compliance record
   on a prunable table is not a compliance record.
3. **Two of the three cannot call it at all.** `interrupt()` only works inside a running graph node.
   `propose_note` has 12 call sites across 9 modules including Temporal activities and the CLI.

Plus: an `Interrupt` carries a value and a content-derived id — **no actor, no timestamp, no reason**.
`plan_approvals` carries actor and `consumed_at`; `note_proposals` carries `decided_by`, `decided_at`,
`reason`. And resume **re-executes the node from the top**, so any pre-`interrupt` side effect runs
twice.

**The decisive point:** for A and C the durable record is required *independently* of any pause
mechanism. Even if `interrupt()` handled the pause perfectly, both stores would remain. The pause is
the cheap half; the record is the point.

**Action: supersede the ADR's reason, not its decision.** As written it invites a future reader to
reopen the question the moment they discover `invoke()` returns.

**One genuine gap, additive rather than replacing:** none of the three covers a *short, in-turn*
clarification ("you said 'the usual solvent' — which?") — seconds, same turn, no compliance record.
That is what `interrupt()` is actually good at, and the repo has no mechanism for it. Backlog row,
not a migration.

**Unverified:** cold resume was measured with `InMemorySaver` across two processes, not
`AsyncPostgresSaver`; the discard-on-new-input behaviour was measured on a plain `StateGraph`, not
the repo's `create_agent` graph. The mechanism lives in `_loop.py` and is graph- and
saver-agnostic, so both should hold — but that is inference, not measurement.

---

## N-6 · KEEP the `BaseStore` decline · The sharpest reason is TRUE at source

The repo declined `BaseStore` with five reasons
(`D-2026-08-10-basestore-is-not-where-this-systems-memory-lives`), the sharpest being that a store
write passes through none of the tool middlewares including `audit._recording`. **Verified TRUE.**

`wrap_tool_call` wrappers are chained (`langchain/agents/factory.py:1018-1026`) and handed **only** to
`ToolNode` (`:1060-1066`). `store` is passed straight to `graph.compile(store=store)` (`:1789`) and
surfaces as `Runtime.store` (`langgraph/runtime.py:114`), reachable from any node or hook.
`create_agent` injects **no** store-backed tools. So a `store.put()` from a node, a `before_model`
hook, or the store's own indexing task provably reaches none of the seven `wrap_tool_call`
middlewares.

The other four reasons also hold; reason 5 (GDPR false-green) is independently disqualifying — the
`store` table's columns are `prefix, key, value, created_at, updated_at, expires_at, ttl_minutes`,
none of which is in `{actor, owner, holder, …}`, so `tests/test_leaver.py`'s derived erasure sweep
would pass while erasure silently failed.

**One nuance worth adding to the ADR:** the audit gap is avoidable *by construction*. If store access
were exposed **as a tool**, it would be a tool call and would pass through all seven wrappers —
`ToolRuntime` carries `store`. So reason 4 is a statement about `BaseStore`-as-ambient-surface, not
about store-backed memory in principle. This does not weaken the decision (reasons 1, 2 and 5 stand
alone), but the ADR's own "what must ship with it" list asks for an answer to how a store write
reaches the audit trail — the answer is *"only through a tool"*, and it should be written down.

**Unverified:** `langgraph/store/postgres`'s concrete SQL and `store_migrations` DDL were not read;
only `langgraph/store/base`.

---

## N-7 · KEEP the audit trail · State history is not a replacement, and conflating them would regress compliance

`get_state_history` (`langgraph/pregel/main.py:1480-1531`) is a thin map over
`checkpointer.list(...)` returning, per superstep: channel values, `next` tasks, config, metadata,
pending writes. It fails **every** requirement of the existing GxP trail:

| Requirement | `audit_events` | checkpoint history |
|---|---|---|
| Append-only | no update/delete path | `update_state`/`bulk_update_state` (`main.py:1590,2515`) rewrite/fork; `delete_thread` `DELETE`s all three tables (`checkpoint/postgres/__init__.py:381-400`) |
| Tamper-evident | `prev_hash`/`row_hash` chain, advisory-lock serialized, `make audit-verify` | none |
| Retained for compliance | own policy | **pruned by age** (`durable/retention.py:138-140`) |
| Records the question | actor, tool, arguments, outcome, effect, latency, correlation | channel values at a superstep — no actor, no outcome |

The last row matters most: `agent/audit.py` records three outcomes including `cancelled`, written on
a shielded task so it outlives the cancellation. A checkpoint of a cancelled turn records the state,
not the *attempt*.

It duplicates nothing and replaces nothing. Its legitimate use is debugging time-travel. **If
`get_state_history` is ever adopted it must be labelled a debugging affordance, never a record.**

---

## N-8 · DO NOT ADOPT · `RetryPolicy` and `CachePolicy` are unreachable or unusable here

**Node `RetryPolicy` cannot reach a `create_agent` graph.** It attaches only on the *builder* —
`StateGraph.add_node(retry_policy=)` (`graph/state.py:382`) or `set_node_defaults`
(`:271-334`) — and not on `compile()` or `create_agent`. Every `add_node` in `factory.py`
(`:1502,1506,1527,1548,1569,1588`) passes none, and `:1787-1801` compiles without defaults. Probed
on a built agent: `model → retry_policy=None`, `tools → retry_policy=None`; a `ConnectionError` from
the model node propagates on attempt 1. The native lever *for `create_agent`* is middleware instead —
langchain 1.3.14 ships `ModelRetryMiddleware` and `ToolRetryMiddleware`. **Nothing to migrate away
from; an unused lever exists.** See N-9 for why adding it would be wrong anyway.

**Node caching exists but is unusable for anything this repo wants.**
`CachePolicy(key_func=default_cache_key, ttl=None)` (`types.py:519-532`) keys on **the node's whole
input state** (`pregel/_algo.py:668-685`, `default_cache_key = pickle.dumps(frozen(args))`). Backends
shipped: `InMemoryCache` and `RedisCache` only — **no Sqlite, no Postgres** — and this deployment
runs no Redis. Measured: with a `CachePolicy` force-attached to the `tools` node, two *identical*
tool calls in one turn still executed **twice** and produced **two** cache entries, because the
message list grows between supersteps so the key differs every time. `create_agent(cache=…)` is
accepted and set on the graph but is inert without node policies. **Do not adopt.**

**`agent/repeat_guard.py` is therefore not a duplicate — measured, not argued.** Three independent
reasons: (a) granularity — `CachePolicy` is per *node*, and `create_agent` has one `tools` node for
every call, so a per-call key is inexpressible; (b) semantics — the guard *refuses* and hands the
model an actionable message, whereas a cache silently serves a stale answer, and
`repeat_guard.py:14-17` names the case that makes this load-bearing (`get_durable_job_status`
legitimately changes within a turn, so a cache would pin a job at "running"); (c) backend. The
nearest native thing, `ToolCallLimitMiddleware`, counts per *tool*, not per identical argument tuple,
so it cannot distinguish legitimate polling from the measured pathology. **KEEP.**

**`science/calc/store.py` `cached_compute` is correctly out of scope** — a content-addressed Postgres
cache keyed on a versioned `CalculationKey`, called from pure science functions reached by Temporal
activities, never from a graph node. `CachePolicy` can express neither its key nor its backend. Its
known concurrent-miss race is already a `DEFERRED.md` row with a trigger. **KEEP.**

---

## N-9 · KEEP the default · Durability modes, and the one thing worth knowing

`Durability = Literal["sync","async","exit"]` (`types.py:87`). **The default is `"async"`**
(`pregel/main.py:2603`). The repo sets it nowhere — and that is correct.

Measured on a 2-superstep turn with a counting saver:

| durability | `put` | `put_writes` | history |
|---|---:|---:|---:|
| *(default)* / `"async"` | 5 | 4 | 5 |
| `"sync"` | 5 | 4 | 5 |
| `"exit"` | **1** | **0** | **1** |

And measured at the instant of a mid-turn failure: `async` had persisted `put=3, put_writes=2`;
`exit` had persisted **nothing**.

For a system whose checkpointer *is* the turn-state store, `"exit"` would be actively wrong — a
SIGKILL, OOM or pod eviction loses the entire turn including tool side effects already committed
elsewhere — and `"sync"` would add a blocking Postgres round-trip per superstep on the interactive
path to close a loss window at most one superstep wide. **No change. Document that the window
exists.**

*Unverified:* the store-side difference was proven with an in-process saver and an exception, not an
actual SIGKILL against `AsyncPostgresSaver`.

---

## N-10 · KEEP · Streaming modes, `Send`, subgraphs and `ToolNode` — a vindication set

All seven stream modes exist at 1.2.10 (`types.py:120`) and none is deprecated; the only
experimental streaming surface is the **v3 protocol** (`@beta`).

- **The three modes chosen are right.** Measured: `tasks` fires twice per node and its `result` is
  the *same* `{channel: value}` dict `updates` already delivers — 2× the volume for two fields the
  repo cannot use. `checkpoints` re-serializes the **entire state** per step (measured `n_messages`
  1→2→2→3→4 in one turn) and emits nothing without a checkpointer. **KEEP.**
- **`graph_stream.py:20-23`'s refusal to read tool calls from `messages` is VINDICATED, and the
  failure was reproduced.** A model streaming one tool call in three fragments yields a
  complete-looking first chunk with **empty args**, then a phantom second call with **no id**. A
  naive reader emits two `ToolCallEvent`s for one call. `updates` delivers one whole `AIMessage`.
- **`astream_events`: correctly unused.** v1 is documented as deprecating, v2 gives richer provenance
  but the same payloads and would *complicate* the translator (tool results paired by `run_id`
  instead of `tool_call_id`; `todos` is not a runnable event, so `updates` would still be needed),
  and v3 is beta. **KEEP unused.**
- **`langgraph-supervisor` / `langgraph-swarm`: DO NOT ADOPT.** Both resolve against this env, but
  `langgraph_supervisor/supervisor.py:431` builds on `create_react_agent`, which is itself
  `@deprecated(LangGraphDeprecatedSinceV10)` — "use `create_agent`". Depending on a 0.0.x package
  built on a deprecated constructor settles it.
- **deepagents dispatches via a `task` StructuredTool** invoking the compiled runnable inline
  (`subagents.py:693/721`), not `Send`. Note `Send` *is* already in play beneath the agent —
  `factory.py:1881` returns `Send("tools", …)` per pending call to parallelize tool execution.
- **`_AttributedSpecialist` is VINDICATED twice.** The `with_config` override is *required*:
  `subagents.py:563` calls `.with_config({...})` and invokes the result, so without it every
  specialist tool call is attributed to the supervisor **silently**. And the namespace claim is
  measured true — under deepagents dispatch the specialist's tokens arrive under
  `('tools:1af6c645-…',)`, so `_agent_of(namespace)` would have yielded the literal `"tools"`.
  Switching to node dispatch also carries a **measured hazard**: the parent's `updates` for the
  specialist node carries the subagent's whole message list, so every specialist tool call would be
  re-emitted at the root namespace with `agent=""`. **KEEP.**
- **No subgraph duplication.** Exactly two graph-construction sites exist in `src/`: `create_agent`
  and `retrieval/fanout.py`. `team.py:311` compiles specialists through the *same*
  `build_langgraph_agent` — reuse, not duplication.
- **`agent/tool_invocation.py` is VINDICATED strongly.** A `ToolNode` is not constructible in a
  Temporal activity without two private symbols: invoking one outside a graph raises
  `ValueError: Missing required config key` and requires injecting a hand-built `Runtime` under
  `langgraph._internal._constants.CONFIG_KEY_RUNTIME`; and its `awrap_tool_call` takes a *single
  composed* wrapper whose composer, `langchain.agents.factory._chain_async_tool_call_wrappers`, is
  underscore-private. Worse, **`ToolNode` stringifies the return value** — a tool returning
  `{"value": 3, "unit": "kJ"}` comes back as `ToolMessage(content='{"value": 3, …}')`, which would
  silently turn every template step's structured result into a JSON string interpolated into
  `${steps.<id>.result}`.
- **`ReloadingSkillsMiddleware`: KEEP, fix one sentence.** No upstream knob exists —
  `SkillsMiddleware.__init__` takes only `backend`, `sources`, `system_prompt`, and the cache skip is
  unconditional (`skills.py:960,1006`). The staleness is real: `PrivateStateAttr` hides
  `skills_metadata` from the *schema*, not from the checkpoint. **But `langgraph_agent.py:222-227`'s
  stated reason is stale** — it claims a three-argument override raised `TypeError`, and that does
  not reproduce at these versions (a three-required-arg `abefore_agent` ran clean end to end). The
  real arity constraint is on the *base* `AgentMiddleware.before_agent(self, state, runtime)`. Per
  the repo's own rule about prose, the citation should be corrected.
