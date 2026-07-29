# D-137 — The plan the model could approve for itself: a pre-execution gate that is not a tool

`SECURITY.md`, `docs/harness-konzept.md` §6 and `build_agent`'s docstring all described a GxP
pre-execution gate: in `plan_only` the agent proposes a plan and waits for a human before
executing. The shipped production configuration runs exactly that (`harness_enabled=true`,
`harness_autonomy=plan_only`).

**The gate did not exist.** MAF's `AgentModeProvider.before_run` injects a `mode_set` tool into the
model's own tool surface on every run, declared `approval_mode="never_require"`, and its
instructions tell the model to use it: *"When approval is granted, always switch to execute mode
(using the `mode_set` tool)"* — where "approval is granted" is the model's own reading of the
conversation. `grep set_agent_mode` returned zero callers in the repository. `plan_mode_required_for`,
which `harness-konzept.md` §6 specifies as the enforcement mechanism, exists nowhere in the code.

Three properties were missing, and the third is the one that makes this worse than a missing
control rather than merely equal to one:

1. Nothing stopped the model changing its own mode.
2. Nothing bound an approval to a *particular* plan, so a plan approved and then rewritten kept
   its authorization.
3. The audit middleware attributes every tool call to the ambient actor — so the trail recorded the
   agent's self-authorization under the **chemist's** Entra oid. An attributable-looking approval
   with no human act behind it is evidence of the wrong thing.

**The fix, and why it is shaped this way.**

*Retract, do not reimplement.* `PlanApprovalModeProvider` runs MAF's `before_run` unchanged and
then removes `mode_set` from the invocation's tool list. The same method also injects `mode_get`,
the mode instructions, and the external-change notification; a reimplementation would silently drop
whichever of those upstream adds next. `mode_get` stays — reading the mode is harmless, and a model
that cannot see its own mode behaves worse, not better.

*Use the supported external seam.* MAF ships `set_agent_mode` precisely for callers outside the
model, and it records the previous mode so the next `before_run` tells the agent the mode changed
underneath it. Writing session state directly would have skipped that and left the agent anchored
to what it last believed.

*Bind the approval to a plan hash.* An approval recording only "this session may execute" would
authorize whatever the plan later became. The hash is over the rendered todo lines — exactly the
strings the surfaces display (`todo_titles` feeds `PlanEvent`) — so what was approved and what was
shown cannot diverge. Hashing richer internal state would let the authorized artifact drift from the
displayed one. A changed plan is a different hash and is unapproved; the decision route answers 409
rather than silently approving the current plan.

*Persist it.* `plan_approvals` is append-only: each row is a GxP record of something a person did at
a moment, so a second decision is a second row and the read path takes the latest — a rejection
after an approval revokes it. It is durable rather than session state because the mode it authorizes
is *already* durable: an approval that vanished on an LRU eviction while its effect persisted would
leave a session running in execute mode with nothing recording who allowed it.

*Not an agent tool.* `POST /sessions/{id}/plan/decision` is owner-scoped and reachable only by an
authenticated principal, for the same reason `POST /approvals/{id}/decision` is not a tool (D-005).

**Why the existing tests could not see it.** Two tests asserted the gate. One checked
`mode_provider.default_mode` — the initial value. One checked that the loop does not *auto*-start.
Neither ever had the model call `mode_set`, which was the only thing that broke it. A test for an
access-control property has to attempt the access. `tests/test_harness_mode.py` now does, and it
also pins the upstream behaviour: if MAF ever stops injecting `mode_set`, the assertion that it is
absent would start passing vacuously, so a second test asserts stock `AgentModeProvider` still
advertises it. That failing is a signal to re-decide, not a bug.
