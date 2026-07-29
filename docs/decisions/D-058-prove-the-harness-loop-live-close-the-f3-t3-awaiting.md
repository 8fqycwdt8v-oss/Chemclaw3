# D-058 — Prove the harness loop live; close the F3-T3 awaiting-todo deferral

**Context.** D-040 wired MAF's harness (`TodoProvider`/`AgentModeProvider`/`AgentLoopMiddleware`),
but every test built it with a dummy `object()` client — construction was proven, the loop itself
never actually ran. Separately, F3-T3 shipped job→session push-back but explicitly deferred
"flipping the harness `awaiting` todo on completion" (`BACKLOG.md`) because it needed the loop
exercised live to get the mutation right, not guessed at in the abstract.

**Decisions.**
- **A real scripted chat client, not a mock of the loop.** `tests/test_harness_execution.py` adds
  `ScriptedChatClient(FunctionInvocationLayer, BaseChatClient)` — the same base classes every
  concrete MAF client composes — whose replies are a fixed script. `build_agent(chat_client=...)`
  wires it through the *actual* harness path (`_build_harness_agent`), so `TodoProvider`,
  `AgentModeProvider`, `AgentLoopMiddleware`, and `todos_remaining` all run for real: the scripted
  model adds todos, the loop re-invokes it while any remain open (reading real todo-store state,
  not a stub), completes them one by one, and the loop stops itself. Three cases proven live:
  `execute` autonomy loops a two-step plan to completion; `plan_only` autonomy produces the plan and
  genuinely stops (not just a different `default_mode` value); `harness_max_loop_iterations` caps a
  todo the model never finishes. Nothing about the loop or todo store is faked — only the model.
- **`agents/harness_todo.py`: the awaiting-job bridge, scoped to what's actually buildable today.**
  `mark_awaiting_job`/`complete_awaiting_job` operate directly on MAF's `TodoSessionStore`.
  `TodoItem` has no field for an arbitrary job id, so the link is a description-string convention —
  never model-authored: `submit_qm_job` creates the "awaiting" todo itself right after Temporal
  hands back a job id, so the match is exact-string. On the `job_completed` push-back
  (`service/app.py`'s `/sessions/{id}/events`), the live session is looked up in `_LiveSessions` and
  the matching todo is flipped complete. This closes exactly what F3-T3 deferred.
  - **Not attempted:** resuming the *same* streamed turn while the job is still running. That needs
    deciding how a new turn gets triggered server-side with no client request in flight — genuinely
    open (`docs/harness-konzept.md` §4, and the F1 backlog's `awaiting`-state-resume follow-up) and
    not guessed at here. The flipped todo is picked up on the session's *next* turn instead.
  - A fresh submit marks awaiting; a re-submit that hits `WorkflowAlreadyStartedError` (an
    already-running *or already-completed* job, D-011) does not — marking again for an
    already-completed job would create a todo no future push-back will ever flip, blocking
    `todos_remaining` forever.
  - Gated on `settings.harness_enabled` at both ends (submit and completion) and on the ambient live
    session being present, so the classic (default) agent path never writes to a todo list nothing
    reads, and the CLI (single-shot, no `AgentSession`) is an inert no-op.
- **New ambient: `agents.session_context.get_current_session`/`set_current_session`.** A second
  contextvar alongside the existing session-id one, carrying the live `AgentSession` object —
  needed because `TodoSessionStore` operates on `session.state`, not reachable from the id alone.
  Kept separate rather than changing the id ambient's contract so every existing id-only consumer is
  unaffected. `service/runner.py::run_turn` sets/resets it alongside the id, same turn lifecycle.

**Result.** `make lint type test` green. New tests: `test_harness_execution.py` (3, the live loop),
`test_harness_todo.py` (4, the bridge in isolation), plus wiring tests in `test_qm_tools.py` (3) and
`test_service.py` (2). No changes to `agents/chemclaw_agent.py` — the harness wiring from D-040 was
correct as built; this proves it and closes the one deferral that was actually gated on doing so.
