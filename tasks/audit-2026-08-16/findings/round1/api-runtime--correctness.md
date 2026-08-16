# api-runtime — CORRECTNESS

Slice: `src/chemclaw/api/{runner,runner_trace,graph_stream,events,state,schemas}.py`.
Everything below was reproduced against the live venv with real compiled graphs
(`build_langgraph_agent`) driven by `tests/fakes_langgraph.ScriptedChatModel`. Scripts are in
`/tmp/exp_*.py`; the output quoted is what they printed.

Two claims I set out to falsify and could **not** — recorded so a later round does not re-spend the
time:

- *"A turn torn down mid-tool-call leaves an unmatched `tool_use` in the checkpointer and bricks the
  thread"* (the hazard `runner.py:214-224` says is structurally gone). A checkpoint with an orphaned
  `tool_use` **is** committed for the whole duration of a tool call (`/tmp/exp_orphan2.py`:
  `ckpt#4 orphan={'call-1'}`), but LangGraph repairs it on resume — cancelling a turn 1 s into a 5 s
  tool call and taking a second turn on the same thread handed the model a synthetic
  `ToolMessage("Tool call slow_lookup with id call-1 was cancelled/not executed")`
  (`/tmp/exp_orphan3.py`). No finding.
- *"`PlanEvent.plan_hash` is hashed over the rendered checkbox lines, so no decision can match it"*.
  It is not: `_todo_contents` and `plan_state.session_todos` filter identically and produced the
  same digest `1e4ca90d3730d068` on a real run (`/tmp/exp_plan.py`). No finding.

---

## A model that does not stream loses its whole answer

- **Severity**: medium
- **Location**: `src/chemclaw/api/graph_stream.py:343-352` (`_text_of`), consumed at
  `graph_stream.py:152-153` and `src/chemclaw/api/runner.py:311-312`, `401-431`
- **Trigger**: any chat model for which `BaseChatModel._should_stream()` is false — i.e.
  `disable_streaming=True`, or `disable_streaming="tool_calling"` (which applies on *every*
  Chemclaw turn, because `create_agent` always binds tools), or a provider class that implements
  only `_generate`. Neither `_openai_model` nor `_anthropic_model` sets the field today, so this is
  latent rather than live, but it is one field on a standard LangChain model object and nothing in
  the slice degrades if it flips.
- **Consequence**: `_text_of` returns `""` for anything that is not an `AIMessageChunk`. LangGraph's
  `messages` mode delivers the *complete* answer for a non-streaming model — as a plain `AIMessage`,
  from `StreamMessagesHandler.on_llm_end`
  (`.venv/.../langgraph/pregel/_messages.py:163-176`) — so the text is on the stream and is thrown
  away by a type test. `run_turn` then sees `answer_parts == []`, takes the `empty_answer` branch
  (`runner.py:402-431`), emits `ErrorEvent(code="empty_answer")`, **returns without an
  `AnswerEvent`**, and books `record_turn_cost(completed=False)`. Every turn in the deployment
  fails, silently, while the model is answering correctly.
- **Evidence**: what the `messages` mode actually delivers, same graph, one field changed:

  ```
  {}                          [('AIMessageChunk', "'The pKa is 4.20.'"), ('AIMessageChunk', "''")]
  {'disable_streaming': True} [('AIMessage',      "'The pKa is 4.20.'")]
  ```

  and end-to-end through `graph_events` (`/tmp/exp_nostream.py`):

  ```
  streaming                  events=['TokenEvent'] answer_parts='The pKa is 4.20.'
  disable_streaming=True     events=[]             answer_parts=''
  ```

- **Fix**: read the text off `BaseMessage`, not off `AIMessageChunk`, and use the message id to keep
  it exclusive. Concretely, in `_text_of`: accept any `BaseMessage`, return `str(chunk.text or "")`;
  in `graph_events`, keep a `set` of emitted message ids and skip a message whose id was already
  streamed as chunks (LangGraph already dedupes the aggregated chunk against the streamed ones via
  `_emit(..., dedupe=True)`, so in practice only the non-streaming shape is new). A regression test
  is one line of fixture: `ScriptedChatModel([...], disable_streaming=True)`.

---

## A `task` helper's private tool output joins the parent turn's grounding corpus, transcript and result store

- **Severity**: medium
- **Location**: `src/chemclaw/api/graph_stream.py:166-198` (`graph_events`, the `updates` branch)
  and its block comment at `:168-187`; consumed at `runner.py:432-437`
  (`build_answer_event(text, tool_trace.outputs, tool_trace.called_tools)` and
  `_record_transcript(..., tool_exchanges)`).
- **Trigger**: any turn in which the model calls `task`. `SubAgentMiddleware` is in deepagents'
  `_REQUIRED_MIDDLEWARE` and `agent/subagents.py` deliberately claims the `general-purpose` name, so
  the tool is present on every profile — this is the normal path, not a corner.
- **Consequence**: the comment at `:168-187` states that the `below_root` test stops a helper's work
  from being taken for the supervisor's, and names the exact damages: *"its output joined
  `ToolCallTrace.outputs` and the parent session's fetchable `result_ref` indistinguishably"*. The
  code only (a) relabels the events with `agent="subagent"` and (b) withholds the helper's
  `PlanEvent`. The same `trace` and the same `exchanges` list are still passed for below-root
  updates, so all three named damages still happen:
  1. `trace.outputs` — the corpus `build_answer_event`/`score_answer` grade the supervisor's answer
     against — receives raw tool output the supervisor never saw. The supervisor sees only the
     helper's one-paragraph report, so a figure the helper's tool returned and the report garbled or
     omitted is still scored as *grounded*. That is the exact fabrication check being weakened.
  2. `trace.called_tools` reports the helper's tools as the turn's.
  3. `exchanges` receives the helper's `AIMessage`/`ToolMessage` pairs, which `_record_transcript`
     writes into the **parent session's** `session_messages`. `TranscriptMessage` /
     `TranscriptToolCall` (`api/schemas.py:96-155`) carry no `agent` field, so on reload the
     helper's private calls render as the supervisor's, with no way to mark them.
  4. With a sink attached, `trace.returned` stores the helper's result under the parent session's
     `tool_result_links` and hands the ref out on the wire.
- **Evidence** (`/tmp/exp_subagent.py`, real graph, model script = `task` → helper calls `ls` →
  helper reports → parent answers):

  ```
  ToolCallEvent   {"tool": "task", "agent": ""}
  ToolCallEvent   {"tool": "ls",   "agent": "subagent"}
  ToolResultEvent {"tool": "ls",   "preview": "['/bo/', '/calc/', ...]", "agent": "subagent"}
  TokenEvent      {"text": "HELPER-REPORT: the secret number is 4242", "agent": "subagent"}
  ToolResultEvent {"tool": "task", "preview": "HELPER-REPORT: the secret number is 4242"}

  --- trace.outputs:      ["['/bo/', '/calc/', '/qm/', '/safety/', '/skills/']",
                           'HELPER-REPORT: the secret number is 4242']
  --- trace.called_tools: ['task', 'ls']
  --- exchanges:          [('AIMessage','chemclaw',''), ('AIMessage','chemclaw',''),
                           ('ToolMessage','ls',"['/bo/', ...]"),
                           ('ToolMessage','task','HELPER-REPORT: the secret number is 4242')]
  ```

  `trace.outputs[0]` and `exchanges[1..2]` are the helper's, in the parent's ledgers.
- **Fix**: pass the trace and the exchanges list only for root updates — give `_from_update` a
  second trace (or a `record=not below_root` flag) so a helper's calls still produce *events* (they
  are worth showing, attributed) but do not append to `ToolCallTrace.outputs`/`_issued`, do not
  reach `exchanges`, and do not write a blob under the parent session. If the ref store is wanted
  for helper results, key it under an explicitly sub-agent-scoped correlation id. Then either the
  comment describes the code, or delete the three damages it names.

---

## `_record_transcript` destroys an answer it has already produced when the write fails in an unexpected way

- **Severity**: medium
- **Location**: `src/chemclaw/api/runner.py:744-775` (`_record_transcript`, the
  `except (ConnectionError, psycopg.Error)` clause) reached from `runner.py:437`
- **Trigger**: `history.save_messages(...)` raising anything outside that pair. Today's durable path
  is well covered (`core/db` normalises pool/connect failures to `ConnectionError` and query
  failures are `psycopg.Error`), so the live trigger set is narrow — a serialization failure inside
  `Jsonb(message_to_dict(message))` for a message carrying a non-JSON `artifact`, an injected or
  test provider, a future provider. The *handling* is the defect regardless of the trigger's
  frequency, because of what it costs.
- **Consequence**: the docstring says "Best-effort, like every other write on this path… no
  rendering is worth failing an answered turn over". It is not: the raise escapes into `run_turn`'s
  `except Exception`, which replaces the finished answer with `ErrorEvent(code="internal",
  retryable=False)`. `answered` never becomes `True`, so `record_turn_cost` books the turn
  `completed=False` and `consume_turn_approval` runs on the failure path. The chemist watched the
  answer stream in token by token and is then told the turn could not be completed — and the answer
  is gone, because `run_turn` never yields the `AnswerEvent`.
- **Evidence** (`/tmp/exp_transcript.py`, a real `run_turn` with an injected history provider whose
  `save_messages` raises):

  ```
  ConnectionError  -> [('TokenEvent', 'The pKa is 4.20.'), ('AnswerEvent', 'The pKa is 4.20.')]
  TypeError        -> [('TokenEvent', 'The pKa is 4.20.'), ('ErrorEvent',  'internal')]
  ```

  Identical turns, identical answer produced; one is delivered and one is destroyed by the type of
  the transcript-write failure.
- **Fix**: catch `Exception` in `_record_transcript` (keeping `BaseException`-derived cancellation
  uncaught so a real teardown still propagates) and route it through the same `degraded(...)` call.
  The narrow tuple is exactly the shape `api/state._release_turn_claim:291` already widened for the
  same reason, with the reason written down there.

---

## `ToolCallTrace.feed` and everything it fixes is unreachable from the shipped path

- **Severity**: low
- **Location**: `src/chemclaw/api/runner_trace.py:124-192` (`feed`), `:194-196` (`flush`),
  `:247-254` (`_take`), `:310-353` (`_arguments_complete`, `_result_text`); called at
  `runner.py:324`
- **Trigger**: any turn. `graph_stream` builds its events from the `updates` stream and calls
  `trace.issued(...)` / `trace.returned(...)` directly; nothing anywhere in `src/` calls `feed`
  (verified by grep — the only callers are `tests/fakes.py:94` and the test modules).
- **Consequence**: `self._fragments` is never written, so `tool_trace.flush()` at `runner.py:324`
  always returns `[]` — the "a tool call whose arguments finished on the final update has nothing
  following it to close it out" flush is a no-op. About 120 lines of `runner_trace.py` are dead,
  including the three defects the docstrings present as fixed-and-guarded (D-138's empty-arguments
  bug, D-159's announce-before-execute rule, and the OpenAI-Responses case that emitted ten
  `tool_call` events for one call). The properties themselves survive — `graph_stream` announces the
  call from a whole `tool_calls` entry before the tool node runs — but the tests that pin them
  exercise no production code, so the *tests* are the thing that is now wrong about the system.
- **Evidence**: `rg '\.feed\(' src/` → no matches. `_fragments` is assigned only inside `feed`
  (`runner_trace.py:175`, `:182`).
- **Fix**: delete `feed`, `flush`, `_take`, `_arguments_complete`, `_result_text` and the runner's
  `for call in tool_trace.flush(): yield call`, and move the D-138/D-159 assertions onto
  `graph_stream` where the behaviour now lives.

---

## `graph_events` raises on a custom-stream payload that is not a dict, where `_custom_event` tolerates one

- **Severity**: low
- **Location**: `src/chemclaw/api/graph_stream.py:155` vs `:212-213`
- **Trigger**: any node calling `get_stream_writer()` with a non-mapping payload — the writer takes
  `Any` and there is no schema between the writer and this reader (the module's own docstring says
  "a writer payload is whatever the node passed and there is no schema between them"). Today the
  only two writers (`core/turn_signals._emit`, `retrieval/fanout._report`) both pass dicts, so this
  is latent.
- **Consequence**: `(payload or {}).get(_SIGNAL_KEY)` raises `AttributeError` on, e.g., a string
  payload. It escapes `graph_events`, escapes the `async for` in `run_turn`, and is caught by
  `except Exception` — so one node's debug write turns the whole turn into
  `ErrorEvent(code="internal")` mid-stream. `_custom_event`, seven lines below, guards the identical
  read with `isinstance(payload, dict)`, so the two readers of one payload disagree about whether
  its shape is guaranteed.
- **Evidence**: `graph_stream.py:155` `if isinstance(signal := (payload or {}).get(_SIGNAL_KEY), …)`
  against `graph_stream.py:212` `if not isinstance(payload, dict): return None`.
- **Fix**: hoist the guard — `if isinstance(payload, dict) and isinstance(signal := payload.get(...))`
  — or drain the signal from inside `_custom_event`, which already has the guard and already calls
  `on_signal`.

---

## `run_turn` stamps five turn-scoped contextvars before the `try` that unstamps them

- **Severity**: low
- **Location**: `src/chemclaw/api/runner.py:187-225` (the stamping) vs `:226` (`try:`) and
  `:524-594` (`finally`)
- **Trigger**: anything raising between line 187 and line 226. The only real candidate is
  `state_snapshot = copy.deepcopy(session.state)` at `:225` — `session.state` is caller-supplied
  harness bookkeeping, and a value that refuses `deepcopy` (an open handle, a lock, a generator)
  raises there.
- **Consequence**: `set_current_session_id`, `set_current_identity`, `set_current_correlation_id`,
  `begin_call_watch`, `begin_loop_watch` and `set_dry_run` have all already run; the `finally` that
  resets them has not been entered. An async generator body runs in its caller's context, so the
  worker task keeps this turn's session id, actor, roles and dry-run flag — which the audit trail,
  the authorization gate and job attribution all read ambiently. The next turn on that worker is
  attributed to the previous user. This is precisely the leak the `finally` block's own comment
  (`:525-529`) says must not be allowed to happen; it just guards a different way of skipping it.
- **Evidence**: `runner.py:187` `session_token = set_current_session_id(...)` … `:225`
  `state_snapshot = copy.deepcopy(session.state)` … `:226` `try:`.
- **Fix**: move `try:` to immediately after `turn_started = time.perf_counter()`, or move the six
  stamping calls and the snapshot inside the existing `try`. Nothing between them needs to be
  outside it.
