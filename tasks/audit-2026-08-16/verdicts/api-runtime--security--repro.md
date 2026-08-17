# api runtime — security and hardening · reproduction verdicts

Lens: **does it actually reproduce?** Findings 2–6 in the source file are severity medium/low and
are out of scope; exactly one finding is `high` and it is the only one judged here.

I did not run, read or import any of the reporter's `/tmp/repro_*.py`. Everything below comes from
scripts I wrote from the source: `/tmp/v_approval.py`, `/tmp/v_approval2.py`, `/tmp/v_approval3.py`,
`/tmp/v_rbac.py`.

`src/chemclaw/api/runner.py` and `src/chemclaw/agent/plan_gate.py` were byte-identical to `HEAD`
(`581e3982`) at verification time (`diff <(git show HEAD:…) …` → no output for both), so no
mutation-experiment contamination is in play and the cited line numbers are the real current ones.

---

## A one-shot plan approval is not spent on two reachable turn endings, so one human approval authorizes unbounded state-changing turns

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I drove the **real `run_turn`** end to end with the real `build_langgraph_agent`, the real
middleware chain (so the real `enforce_plan_approval`), the real `plan_state.session_todos`, the
real `plan_identity` and the real `InMemoryPlanApprovalStore` (a shipped backend, not a double).
The only substitutions were the ones a test cannot avoid: a `ScriptedChatModel` in place of a live
provider (the repo's own `tests/fakes_langgraph`), one shared `InMemorySaver` handed to both
`runner._turn_checkpointer` and `agent.checkpointer.checkpointer` so the graph's todos and the
spend's plan read come from one place, and a stubbed Temporal health probe.

Config: `harness_enabled=True`, `harness_autonomy="plan_only"` — which is exactly the shipped
chart's posture (`deploy/helm/chemclaw/values.yaml:339` sets `CHEMCLAW_HARNESS_ENABLED: "true"`,
and `plan_only` is the code default in `core/config/agent.py:142`). The script prints
`gate_applies(default profile) = True`.

Protocol per session: turn 1 the model writes a plan; a human records one approval against
`plan_identity(todos)`; turn 2 makes a gated call (`propose_knowledge_note`, in
`STATE_CHANGING_TOOLS`) and ends in the shape under test; then I ask the store whether the approval
is still live and drive a further turn.

**`uv run python /tmp/v_approval2.py` — the negative and positive controls, so "the gate passed"
is not an assumption:**

```
=== NEGATIVE CONTROL: same gated call, nobody approved ===
    ToolFailedEvent 'PlanNotApprovedError: propose_knowledge_note changes stored data or starts
     work, and the plan it is part of has not been approved yet; …'

=== APPROVED ===
    ToolFailedEvent "GitSubmitError: note_repo_dir '.' resolves to /home/user/Chemclaw3 …"
approval_stands after that answering turn: False
```

The gate genuinely refuses without an approval, and with the approval the call passes the gate and
reaches the tool **body** (the `GitSubmitError` is raised inside `propose_knowledge_note`, not by any
middleware). And on a normal answering turn the approval **is** spent — `approval_stands → False`.
That is the baseline the two claimed paths are measured against.

**`uv run python /tmp/v_approval.py` — the empty-answer ending:**

```
approval_stands after human approval: True

-- turn 2 (gated call + empty answer) --
turn for session verify-approval-1 ended with no answer text after 1 tool call(s)
['CapabilityDegradedEvent', 'ToolCallEvent', 'ToolFailedEvent', 'ErrorEvent']
plan unchanged? True
approval_stands AFTER the empty-answer turn: True

-- turn 3 (second gated turn, no new human decision) --
['CapabilityDegradedEvent', 'ToolCallEvent', 'ToolFailedEvent', 'ErrorEvent']
approval_stands after turn 3: True
```

Turn 3's gated call also passed the gate (same `GitSubmitError`-from-the-body signature, not a
`PlanNotApprovedError`) on the single decision made before turn 2.

**`uv run python /tmp/v_approval2.py` — the disconnect ending** (`athrow(CancelledError)` after the
gated tool's event and before the answer, which is what sse-starlette delivers on `http.disconnect`,
per D-130's own measurement):

```
approval_stands before: True
turn for session disc-1 was torn down before it answered (client disconnect …); rolling session state back
['CapabilityDegradedEvent', 'ToolCallEvent', 'ToolFailedEvent', '<<client disconnect>>', '<<CancelledError propagated>>']
approval_stands AFTER the disconnect: True
plan still the same: ['screen the species']
next turn: ['CapabilityDegradedEvent', 'ToolCallEvent', 'ToolFailedEvent', 'TokenEvent', 'AnswerEvent']
```

The approval survives the teardown, the plan hash is unchanged (the todos live in the checkpointer,
which the rollback does not touch — it clears and restores `session.state` only), and the next turn's
gated call is authorized again.

**`uv run python /tmp/v_rbac.py`** — what else would stop it in the shipped posture
(`entra_required=true`, `CHEMCLAW_ENTRA_PRIVILEGED_ROLES: ""`, `tool_authz_default="allow"`):

```
propose_knowledge_note           refused: … it changes stored data, so it requires a privileged role …
remember_preference              ALLOWED by RBAC
watch_for                        ALLOWED by RBAC
stop_watching                    ALLOWED by RBAC
forget_preference                ALLOWED by RBAC
request_development_report       ALLOWED by RBAC
```

### Why

The mechanism is exactly as stated and the line numbers are current. `consume_turn_approval` has
precisely two call sites in `run_turn` — `runner.py:448-449` (after `yield answer`) and
`runner.py:522-523` (inside `except Exception`) — and both the `return` at `runner.py:431` and the
`except (GeneratorExit, asyncio.CancelledError)` clause at `runner.py:450` leave the turn without
passing either. My control run proves this is a *skip*, not a store that never consumes: the same
session, the same plan, the same tool, ending with prose instead, does spend the approval.

The finding's stated consequence also holds rather than merely being asserted:

- the approval is not merely "still recorded" — `approval_stands` returns `True` and the real
  `enforce_plan_approval` admits a later turn's state-changing call, verified against a negative
  control that shows the same call refused when no approval exists;
- the docstring premise the finding attacks ("a turn that was undone has not used its
  authorization") is false as measured: the gated tool had already run when the disconnect landed,
  and the rollback restores `session.state` and nothing else;
- it is not the "one-turn residual" `consume_turn_approval`'s docstring claims. Repeating the
  disconnect is repeatable indefinitely, because nothing on that path ever writes `consumed_at`.

Two things I would add that make it worse, not better:

1. **There is a third non-spending ending, not two.** `/tmp/v_approval3.py` cancels while the
   generator is suspended in `yield answer` (`runner.py:444`) — the window sse-starlette actually
   uses to send the answer. The turn takes the `answered or run_complete` branch, logs *"torn down
   after its exchange completed … the committed turn is kept"*, and `approval_stands` is still
   `True` afterwards. So even a fully successful, fully answered turn leaves the approval live if
   the client drops during the send, and line 448 is one line past the point of no return.
2. **The disconnect trigger is deterministic and entirely client-side.** The finding's "empty
   answer" path depends on model behaviour an attacker cannot force; the disconnect path needs
   nothing but closing the socket after the `tool_result` frame, which requires no privilege beyond
   "can take a turn".

The one part I would soften — not enough to move the verdict — is the worked example. In the
shipped chart's own posture the named tool `propose_knowledge_note` is independently refused by
`authorize_tool` (it is in `DEFAULT_WRITE_TOOL_GATES` and the privileged role set is empty, which
fails closed). So the bypass is not a path to a knowledge-graph write on that configuration. It *is*
the only remaining control on `remember_preference`, `forget_preference`, `watch_for`,
`stop_watching` and `request_development_report`, all in `STATE_CHANGING_TOOLS`, none in
`DEFAULT_WRITE_TOOL_GATES`, all of which my RBAC run shows open to any authenticated user under the
default `tool_authz_default="allow"`. The claim "one human approval authorizes unbounded
state-changing turns" therefore stands on its own; only the choice of illustrative tool is off.

High rather than critical: the gate is defence-in-depth over RBAC, it requires the harness to be
enabled, and the state changes reachable through the hole are user-scoped preferences,
subscriptions and a report workflow rather than a knowledge-repo push.
