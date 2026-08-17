# Verdicts — `api-runtime--security.md`, reachability/consequence lens

Scope: critical/high findings only. The file contains **one** — the plan-approval finding below.
The other four are severity medium (×3) and low (×2) by the reporter's own labels and are out of
scope; I did not assess them.

Working tree checked against the pristine `HEAD` copy first:
`diff -q pristine/src/chemclaw/api/runner.py src/chemclaw/api/runner.py` and the same for
`agent/plan_gate.py` — both identical, so nothing below is an artifact of another agent's mutation.

---

## A one-shot plan approval is not spent on two reachable turn endings, so one human approval authorizes unbounded state-changing turns

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed — not critical; see the last section for why)

### What I did

**1. Is the gated posture reachable in a real deployment?** The finding hedges this ("a deployment
with the shipped `plan_only` posture"). It is stronger than that — the shipped chart sets *both*
keys explicitly:

```
$ grep -n "HARNESS" -A1 deploy/helm/chemclaw/values.yaml
339:  CHEMCLAW_HARNESS_ENABLED: "true"
340:  CHEMCLAW_HARNESS_AUTONOMY: "plan_only"
```

The code default is `harness_enabled=False` (`core/config/agent.py:141`) and `.env.example` says
`false`, so a bare `uv run` deployment is not gated at all — but the OpenShift chart that is the
delivery vehicle turns it on. `gate_applies(get_profile(None))` printed `True` under those values,
and `langgraph_agent.py:630` attaches `enforce_plan_approval` under that same predicate. Posture:
reachable, and it is the deployed one.

**2. Does the control work on the normal path, and fail on the two named ones?** `/tmp/verify_approval.py`
drives the real `run_turn` (real teardown clauses, real `consume_turn_approval`, real
`InMemoryPlanApprovalStore` — the backend `session_store="memory"` actually gets, not a double)
with a stub graph, and drives the real `enforce_plan_approval` middleware before and after each
turn to see whether a gated call (`propose_knowledge_note`) still executes:

```
gate_applies(default profile) = True

--- baseline: a NORMAL turn spends the approval ---
  approval live before: True
  events: ['CapabilityDegradedEvent', 'TokenEvent', 'AnswerEvent']
  approval live after : False

--- path 1: EMPTY-ANSWER turn (runner.py:431 `return`) ---
  approval live before: True
  [turn 1] gated call EXECUTED (tool body ran: True)
  events: ['CapabilityDegradedEvent', 'ErrorEvent']
  approval live after : True
  [turn 2, no new approval] gated call EXECUTED (tool body ran: True)

--- path 2: CLIENT DISCONNECT mid-stream (runner.py:450 clause) ---
  approval live before: True
  events: ['CapabilityDegradedEvent']
  approval live after : True
  [turn 2, no new approval] gated call EXECUTED (tool body ran: True)

--- how many turns can one approval carry? ---
  turns run under ONE approval before it was spent: 25 (still live: True)
```

The baseline is the part that matters for "is this a bypass or a control that never works": the
normal path *does* spend it (`live after: False`). The two named paths do not, and the tool body
runs in the following turn with no new human decision. The 25 is my loop bound, not the system's.

**3. Is the cancellation shape the finding names the one production produces?** `/tmp/verify_approval2.py`
reproduces the two realistic shapes rather than raising `CancelledError` in the consumer by hand —
cancelling the task that runs the SSE body (what sse-starlette does on `http.disconnect`), and the
front door's own `asyncio.timeout(settings.service_turn_timeout_seconds)` at `routes/turns.py:162`:

```
A) front-door turn deadline (asyncio.timeout around run_turn)
   approval live before: True
   -> TimeoutError, exactly the branch routes/turns.py:181 catches
   approval live after : True
B) client disconnect (body task cancelled)
   approval live before: True
   -> body task cancelled
   approval live after : True
```

**4. Is there a second consumer anywhere?** `grep -rn "\.consume(\|consume_turn_approval" src/`:
`plan_approval_store().consume` has exactly one caller, `consume_turn_approval`, called only at
`runner.py:449` and `runner.py:523`. No route, no middleware, no retention job spends one.

**5. Does the stranded row expire?** `grep -rn "plan_approvals" src/chemclaw/durable/retention.py`
returns nothing. `plan_approvals` is not pruned. "Live forever" is literal, not rhetorical.

### Why

Both gaps are exactly where the finding puts them and neither is guarded upstream.

- The `return` at `runner.py:431` sits inside the `try`, so it is not caught by the
  `except Exception` clause (which *does* consume, at 522-523) and the `finally` contains no
  `await`. Nothing between the empty-answer branch and the generator's exit touches the store.
- The `except (GeneratorExit, asyncio.CancelledError)` clause at 450 re-raises on both of its
  branches (`answered or run_complete`, and the rollback) without consuming, and cannot consume —
  D-130's rule forbids an `await` there.

The finding's demolition of the docstring's justification is correct on the code: the teardown at
`runner.py:500-501` restores `session.state` and nothing else, and the module's own comment says
"No durable delete accompanies this any more". A tool that ran has run. The premise "a turn that was
undone has not used its authorization" is false for every side effect in `STATE_CHANGING_TOOLS`.

Three things the reporter did not have, which strengthen it:

1. **A third trigger, needing no attacker and no model quirk.** The front door's own wall-clock
   deadline (`service_turn_timeout_seconds`, 600 s) cancels `run_turn` through `asyncio.timeout`,
   lands in the same clause, and leaves the approval live — measured above as branch A. The
   disconnect variant needs a client to close the socket; the empty-answer variant needs the model
   to emit no prose; this one needs only a slow turn. A `plan_only` deployment whose turns
   occasionally time out accumulates live approvals with nobody doing anything.
2. **Why it survived review.** `tests/test_plan_gate.py` calls `consume_turn_approval` directly at
   all five of its call sites (lines 366, 394-395, 447, 496). No test drives a *turn ending*
   through `run_turn`, so neither gap has a test that could go red. The behaviour is unpinned in
   both directions.
3. **The proposed "cheapest correct version" is wrong and would break the feature.** The finding
   suggests spending inside `enforce_plan_approval` on the call it lets through. `approval_stands`
   folds `consumed_at` into the verdict, so the *second* gated call of the same approved plan, in
   the same turn, is then refused — which is precisely what `consume_turn_approval`'s docstring
   already warns about ("consuming on entry would refuse the plan's own second iteration").
   Measured (`/tmp/verify_fix.py`):
   ```
   gated call #1 in the turn allowed: True
   gated call #2 in the SAME turn allowed: False
   ```
   The correct shape is either a turn-scoped "this turn has already spent it" marker in front of a
   first-call consume, or — simpler — spending on *every* exit path of `run_turn` (a shielded task
   for the clause that may not `await`, as the finding's second option says).

### The one part I would not assert, and why the severity is high rather than critical

The finding's headline is "unbounded state-changing turns", and the repeat loop is real. But its
step "the plan is not rewritten, so `plan_identity` keeps matching *P*" is an assumption about
model behaviour that it does not measure, and it is load-bearing for the *worst* variant. Two
guards bite if it fails: `rewrites_the_plan_in_this_batch` refuses any gated call arriving beside a
`write_todos`, and `_plan_behind` hashes the turn's live `todos`, so a plan rewritten earlier in the
turn produces a hash with no decision. So the true DARK-1 shape ("ask a completely different
question, and it executes different state-changing tools") holds only while the model leaves the
todo list alone. The variant that holds unconditionally is the same-plan repeat: the user retries
after an empty answer or a timeout, the plan is unchanged by construction, and the plan's
state-changing tools execute again — a second knowledge-repo branch push, a second durable job
launch — under one decision.

Against critical: `POST /sessions/{id}/plan/decision` carries no privileged-role requirement
(`routes/plan.py`; `CurrentUser` + `CurrentSession` only), so the approver is the session's own
chemist. This is not a two-person control being defeated — a user who wanted another turn could
simply re-approve. And every gated tool still passes `enforce_tool_authz`, with
`DEFAULT_WRITE_TOOL_GATES` holding `propose_knowledge_note`, `record_confirmed_answer`,
`record_failure` and `compute_dft_energy` behind the privileged role set, and every call is still
audited. So the bypass grants no capability the caller lacked; it removes the per-turn human
confirmation that is the whole of the `plan_only` posture, on three reachable paths, with the
stranded authorization never expiring. High is right.
