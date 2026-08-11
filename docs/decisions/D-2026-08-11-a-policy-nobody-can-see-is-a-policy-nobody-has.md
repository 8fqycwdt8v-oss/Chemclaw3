# D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has — the deep-agents audit, and the context policy the framework removal took with it

**Status:** accepted · **Date:** 2026-08-11

## Context

Layer 1 was rebuilt on LangGraph (D-2026-08-10) and `agent-framework-*` was removed (M13,
D-2026-08-11-what-the-removal-found). The rebuild composes `langchain.agents.create_agent` with a
chosen subset of `deepagents` middleware rather than calling `deepagents.create_deep_agent`. This
audit asks whether that composition is complete against what "deep agents" actually names, and it
was run against LangChain's own four pillars — subagents, filesystem, context management, shell —
plus the middleware stack `create_deep_agent` composes by default.

Five of six pillars came back sound, and each was declined or narrowed for a reason already written
down. `create_deep_agent` is not called because its default stack always registers
`FilesystemMiddleware` — a write/edit/glob/grep surface, plus shell — and every one of those names
would then have to be answered for by `available_tool_names`, gated by `tool_role_gates` and
justified in the safety rubric, which is a general filesystem acquired as a side effect of wanting
to read a `SKILL.md` (`agent/skill_backend.py`, D-038/D-118). Skills are gated on the *backend*
rather than on the advertised list, which is a real security property rather than an API detail,
because `SkillsMiddleware` publishes skill *paths* into the system prompt. Subagents exist and are
stricter than upstream's (`reject_widening`, a non-removable `safety` specialist, audit attribution
through a contextvar), off by default pending M12's measurement. `BaseStore` is declined with five
reasons, the sharpest being that a store write passes through none of the six tool middlewares
including `audit._recording`. `interrupt()` is declined on a lifecycle argument that holds: a PR-gate
review takes days and an SSE stream cannot be held open across one.

The sixth pillar is not sound. **Context management does not exist**, and what makes that worth an
ADR rather than a backlog row is not the absence — it is that everything *describing* the absent
mechanism survived the removal:

- `agent_context_token_budget`, `agent_keep_last_tool_groups` and
  `agent_keep_last_conversation_groups` have **zero readers** anywhere in `src/` or `tests/`. Their
  implementation was `chemclaw_agent._build_compaction`, assembled from the previous framework's
  strategy classes, and it left with that framework.
- The config comment above the three describes, in the present tense, a reduction that does not
  happen. `.env.example` ships all three.
- The system prompt tells the *model*: "Long conversations: this session's context is compacted to a
  token budget, so an older turn can age out of what you currently see with no marker left behind"
  (`agent/chemclaw_agent.py`). It was being instructed to hedge about a mechanism that had been
  deleted.
- The graph replays the whole checkpointed thread every turn (`thread_id` = session id). The failure
  at the provider's context limit is a hard error, not a degradation.

Two smaller things were found in the same sweep and share the shape: **`ChemclawState.awaiting_jobs`**
was declared, described as live in three docstrings, and never written or read — against that file's
own rule that a declared field nothing consults reads as coverage while proving nothing. And the CLI
documented "the CLI's in-memory checkpointer" and a session "resumable across invocations" while
`_build_cli_agent` passed no `checkpointer=` at all.

And one thing that was absent rather than misdescribed: the LangGraph checkpoint tables
(`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) were in no retention register. They are
created by `AsyncPostgresSaver.setup()` rather than by a migration in `infra/sql`, so they appear in
no schema review and in no inventory — `tests/test_schema_inventory.py` pins that inventory to
exactly what the migrations create, so it could not have noticed. `agent/leaver.py` already erased
them per actor; nothing disposed of them, so a deployment that erased nobody kept every turn's state
for its whole life.

## Decision

**1. Restore D-025's policy as `agent/compaction.py`, attached unconditionally.**

Two edits inside `wrap_model_call`, reclaiming cheapest-first exactly as D-025 specified: clear
stale tool results first (they are this system's largest context consumers), then drop the oldest
conversation groups if that was not enough.

- The first edit is upstream's `ClearToolUsesEdit` — it *is* `ToolResultCompactionStrategy` under
  another name, and re-writing it would have been a second copy of somebody else's tested code.
  `langgraph_agent`'s own opening argument (use the framework's machinery rather than re-implement
  it) applies with more force to a strategy than it did to the agent loop.
- The second is first-party (`KeepLastConversationGroupsEdit`), because upstream ships no
  conversation window. It is what makes the thread *bounded*: clearing tool results reclaims nothing
  from a conversation that called no tools.
- **No summarizer**, unchanged from D-025 and worth restating now that `SummarizationMiddleware` is
  one import away: a summarizer reads retrieved evidence and writes text that is replayed as
  conversation, which is an indirect-prompt-injection surface pointed at the thread.
- **Unconditional**, unlike the harness middleware beside it. An unbounded thread is a property of a
  session, not of the plan/execute mode; the single-turn agent accumulates one just as fast.

**Non-destructive, and that is a change from D-025.** Both edits narrow the list *this model call*
is sent and leave graph state untouched, so the next turn re-derives the reduction from the full
thread. D-025 also ran its strategy over the persisted history so the next turn "started smaller",
and the commit that deleted the durable half of that named why it was wrong: a context heuristic
must not edit a record somebody else's policy governs. The checkpointer is turn state rather than
the GxP record, but the argument survives one step down — a reduction that is recomputed costs an
estimator pass, a reduction that is *applied* costs history. Age is what bounds the checkpoint
tables, which is decision 2.

**2. Prune the checkpoint tables by thread** (`retention_checkpoints_days`, 0 by default like every
other window). By *thread*, not by row: `parent_checkpoint_id` chains a thread's checkpoints, so
removing the old rows from a live thread would leave the survivors pointing at nothing. A thread
expires when its newest checkpoint does. All three tables go in **one** transaction, against this
module's own commit-per-table rule, because they are one thread's state split across three keys with
no foreign key to enforce it — committing separately offers a crash a choice between surviving
`checkpoints` rows referring to blobs that are gone and orphaned blobs no later pass can find.
Absent tables are *skipped*, not raised on: a deployment that has never run the graph engine does
not have them, and a sweep that failed there would stop pruning the three tables it can handle.

**3. Delete `awaiting_jobs`; give the CLI the checkpointer it documents.** The field is removed
rather than filled in, and the three docstrings now say the true thing: nothing writes job
bookkeeping into `todos` at all, because a launched job is a `job_records` row and a `session_events`
push-back. The CLI gets `cli_checkpointer()` — Postgres where the deployment has one, `InMemorySaver`
otherwise — handed to both the graph *and* `_plan_command`, which is the part that mattered more
than the lost conversation: `/plan` reads `plan_state.session_todos`, which resolves *the configured*
checkpointer, so it was asking a store the graph had never written to and answered "(no plan yet)"
under every configuration. The harness the CLI exists to exercise could not be exercised.

## The measurement

Prose about compaction is what caused this defect, so the claim is a number. A thread of realistic
turns — one 20,000-character evidence sweep per turn, which is the largest real result
`api/tool_results.py` measured — under the shipped defaults (budget 100,000; keep 2 tool results,
12 conversation groups). "Sent" is what the model was actually handed, recorded off a fake model
inside a real compiled graph:

| turns | thread tokens | sent to the model | pairing intact |
|------:|--------------:|------------------:|:--------------:|
| 5 | 25,805 | 25,811 | yes |
| 10 | 51,610 | 51,616 | yes |
| 20 | 103,230 | **13,740** | yes |
| 40 | 206,470 | **17,540** | yes |
| 80 | 412,950 | **25,140** | yes |
| 160 | 825,910 | **40,340** | yes |

Below the budget the model receives the whole thread — the six extra tokens are the new question,
not an edit. Above it the sent size stops tracking the thread size: an eightfold longer conversation
costs three times the context rather than eight. "Pairing intact" is
`message_pairing.calls_without_adjacent_results` — the strict, on-the-wire rule a provider actually
enforces — asserted on what was sent, because a reduction that strands a tool call is rejected
outright and replayed on every later turn.

**The cost was measured too, because the middleware pays it on every model call, not only over
budget.** `ContextEditingMiddleware` deep-copies the message list before applying its edits,
unconditionally. On the same threads that is 0.17 ms at 5 turns, 0.65 ms at 20, and 6.1 ms at 160
(the estimator pass beside it is 0.04–0.6 ms) — against a median turn measured at 16.9 s on the
fastest archived run. It is noise, which is the answer, but it is an answer rather than an
assumption: an unconditional deep copy of a thread is exactly the kind of thing that is fine until
somebody's thread is not.

`chemclaw_context_compactions_total` and `chemclaw_context_reclaimed_tokens_total` are the operator's
version of that table. A model call that needed no reduction increments neither, so a flat zero means
"never over budget" and an absent series means "not wired" — the distinction this subsystem could
not express for the whole of M13.

The checkpoint prune was verified against a live Postgres rather than only in the suite (this
sandbox's pgvector is 0.6.0 and the full migration set needs 0.7+, so the Postgres-backed tests skip
here and run in CI): an expired thread left 0 rows in all three tables, a thread inside its window
kept all 3, and a schema with no checkpoint tables reported them skipped while still pruning
`session_events`.

## Consequences

- Every deployment's model input is bounded from this commit; none was before. The three settings
  mean what their comment says for the first time since M13.
- `agent_keep_last_tool_groups` counts the newest *tool results*, not tool-call groups, because
  `ClearToolUsesEdit` is what counts them. The name is D-025's and stays: it is ENV-visible, and
  renaming it would cost every deployment that sets it to buy a more accurate word.
- A cleared tool result carries a placeholder that says what happened and that the tool can be
  re-run, not upstream's bare `[cleared]`. An unexplained placeholder reads as a tool that returned
  nothing, which the model would reasonably answer by calling it again — and `repeat_guard` would
  then refuse that call, turning one reclaimed payload into a refusal a chemist sees.
- **One thing is lost against D-025**, and it is named rather than glossed: its strategy collapsed an
  older tool result into a "short cited `[Tool results: …]` trace", and `ClearToolUsesEdit` replaces
  the payload outright, so the citation goes with it. Keeping the trace would couple this module to
  the result shape of every tool, for a benefit that exists only above the budget — where the
  alternative is a hard context-limit failure and the model's own prose in the thread still carries
  what it concluded. `exclude_tools` is the escape hatch and is deliberately empty: excluding the
  evidence sweeps would exclude exactly what this edit exists to reclaim.
  `docs/guides/harness-konzept.md` §9 now records this as a named trade rather than an open risk.
- `core.db.existing_tables` is now shared by erasure and retention. It was private to `agent/leaver.py`
  and the second caller wanted the same answer about the same tables for the same reason.
- `infra/sql/README.md` gains a prose section for the four tables its inventory table structurally
  cannot list. The inventory's contract — exactly what the migrations create — is right and stays;
  what was wrong is that four real tables were therefore invisible to a reader of the only document
  that claims to describe this schema.
- Prompt caching remains absent and remains a `DEFERRED.md` row with a measurement behind it
  (`langchain_openai` contains zero occurrences of `cache_control`). Nothing here changes it.
