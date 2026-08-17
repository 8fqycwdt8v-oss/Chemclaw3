# api runtime — design and simplification

Slice: `src/chemclaw/api/{runner,runner_trace,graph_stream,events,state,schemas}.py`
Lens: design and simplification only. Correctness/security findings are another reviewer's.

---

## Half of `runner_trace.py` is dead: the streamed-reassembly machinery has no production caller

- **Severity**: high
- **Location**: `src/chemclaw/api/runner_trace.py:99-100` (`_names`, `_fragments`), `:124-192` (`ToolCallTrace.feed`), `:194-196` (`flush`), `:247-254` (`_take`), `:310-329` (`_arguments_complete`), `:332-353` (`_result_text`); dead consumer at `src/chemclaw/api/runner.py:320-325`
- **Trigger**: any turn at all. `ToolCallTrace.feed` is the only writer of `self._fragments` and `self._names` (writes at `:175`, `:182`, `:142`). Nothing in `src/` calls `feed`. An AST scan of every attribute access named `feed`/`flush`/`issued`/`returned`/`outputs`/`called_tools` across `src/` returns:

  ```
  ('src/chemclaw/api/graph_stream.py', 275, 'issued')
  ('src/chemclaw/api/graph_stream.py', 302, 'returned')
  ('src/chemclaw/api/runner.py',       324, 'flush')
  ('src/chemclaw/api/runner.py',       407, 'called_tools')
  ('src/chemclaw/api/runner.py',       412, 'called_tools')
  ('src/chemclaw/api/runner.py',       434, 'outputs')
  ('src/chemclaw/api/runner.py',       435, 'called_tools')
  ```

  `graph_stream` reads LangChain's already-whole `message.tool_calls` / `ToolMessage` and calls `issued`/`returned` directly — it never streams fragments in. So `_fragments` is permanently `{}`, `flush()` permanently returns `[]`, and `runner.py:324` (`for call in tool_trace.flush(): yield call`) is a loop over an empty list on every turn.

- **Consequence**: 124 lines of function body plus roughly 60 lines of docstring/comment (out of 353 in the file) describe a mechanism that does not run — including the module's entire "duck-typed throughout, deliberately" premise, which was written against the removed framework's private content classes. The live path is not duck-typed at all: it consumes `langchain_core.messages.ToolMessage` and the `tool_calls` list, both imported by name in `graph_stream.py:42`. A reader arriving at this module is handed a detailed, measured, ADR-cited account (D-138, D-159, the OpenAI-Responses ten-events case) of code that no input can reach, and three test files spend their assertions on it (`tests/test_review_2026_08_05.py`, `tests/test_runner.py`, plus the `feed_trace` helper in `tests/fakes.py`) — coverage that reads as protection of the live tool trace and is not.

- **Evidence**: ran a real compiled graph turn (`/tmp/prove_flush.py`, `build_langgraph_agent(ScriptedChatModel(...))` driven through `graph_events`):

  ```
  events: ['ToolCallEvent', 'ToolFailedEvent', 'TokenEvent']
  trace._fragments: {}
  trace._names: {}
  trace._issued: {'call-1': 'load_skill'}
  trace.flush(): []
  ```

  A turn that emitted a real `ToolCallEvent` left both reassembly buffers untouched and flushed nothing.

- **Fix**: delete `feed`, `flush`, `_take`, `_arguments_complete`, `_result_text`, `self._names`, `self._fragments`, and the `for call in tool_trace.flush()` loop at `runner.py:320-325`. What remains is `issued`, `returned`, `outputs`, `called_tools` and `_issued` — a ~90-line class that is honestly "the turn's tool ledger", not "reassembling a streamed tool call". Rename the module accordingly (`api/tool_trace.py`) and rewrite its docstring to describe what it does. Behaviour-preserving: the one surviving read of `_names` is the fallback at `:236` (`self._issued.get(key) or self._names.get(key, key)`), which with an always-empty `_names` already evaluates to `self._issued.get(key) or key`. The three test files' `feed`-driven cases go with it — they are the only reason the code looks alive.

---

## `run_turn` is 483 lines / 94 statements / 26 branches carrying twelve pieces of mutable local state

- **Severity**: medium
- **Location**: `src/chemclaw/api/runner.py:112-594` (`run_turn`)
- **Trigger**: reading it. Measured with an AST pass over the slice:

  ```
  src/chemclaw/api/runner.py:112 run_turn  lines=483 stmts=94 branches=26
  ```

  It is an async generator holding, simultaneously: `plan_gated`, `answered`, `run_complete`, `answer_parts`, `turn_usage`, `started_jobs`, `tool_exchanges`, `state_snapshot`, and five contextvar reset tokens (`session_token`, `identity_token`, `correlation_token`, `calls_token`, `loop_token`, `dry_run_token` — six), across an `AsyncExitStack`, two nested `async for` streams, `except (GeneratorExit, CancelledError)`, `except Exception`, and a `finally` that must not `await`.

- **Consequence**: the interactions between those flags are not derivable from the code — they are held only in ~120 lines of prose comment (`:167-179` alone spends 13 lines distinguishing `answered` from `run_complete`, and `:477-483` re-explains the same distinction from the other end). Every one of those explanations exists because the distinction was got wrong once. That is the signature of a function past the size a reviewer can hold: the defence against the next mistake is a paragraph, not a structure. The `finally` block's central invariant — "nothing in this block may `await`" — is likewise held by a comment (`:525-529`) and nothing else; adding one `await` to any of the six teardown calls silently skips the five contextvar resets and leaks one turn's identity into the next turn on the worker.

- **Evidence**: the AST measurement above; the comment density (`:162-225` is 64 lines to reach the `try`); the two-flag rollback predicate whose correct value is argued for in two separate multi-paragraph comments.

- **Fix**, in order of payoff and all behaviour-preserving:
  1. Extract the ambient-state setup/teardown into one `async with` context manager (`_turn_ambient(session, actor, roles, dry_run)`) that yields the correlation id and owns all six tokens. The "no `await` in `finally`" rule then lives in one small object with one job, and can be asserted by a test on that object instead of by prose in the caller.
  2. Extract the terminal-telemetry block (`:530-587`: budget, duration histogram, cost ledger, five token counters) into `_book_turn(...)` — it is 58 lines of pure bookkeeping with no interaction with the turn's control flow.
  3. Extract the two guard branches (`:362-386` loop cap, `:387-431` empty answer) into small generators returning the `ErrorEvent` or `None`.
  What is left is the lifecycle the module docstring claims the module is.

---

## The graph-stream consumption block is written twice, including the answer-assembly predicate

- **Severity**: medium
- **Location**: `src/chemclaw/api/runner.py:296-313` and `:348-360`
- **Trigger**: `settings.mid_turn_resume_enabled` is on and the turn launched a durable job, so the resume path runs.
- **Consequence**: two copies of the same seven-argument `graph_events(...)` call (differing only in `message` and `on_signal`) and two copies of

  ```python
  if isinstance(event, TokenEvent) and not event.agent:
      answer_parts.append(event.text)
  yield event
  ```

  That predicate is the rule that decides what becomes the chemist's answer and the durable transcript — `events.py:92-105` documents it as load-bearing precisely because getting it wrong splices a subagent's working prose into the answer. It is currently possible to change one copy and not the other, and the resume copy is the one that runs least often, so the divergence would show up only under a non-default setting. The same class of duplication appears at `graph_stream.py:297` and `:303`, where `str(getattr(message, "tool_call_id", ""))` is computed twice for one message — line 297 already binds it to `call_id`, and line 303 recomputes it instead of using it.

- **Evidence**: the two blocks, verbatim in the file; `_from_update`'s `call_id` at `:297` is used at `:298` and then rebuilt at `:303`.
- **Fix**: inside `run_turn`, bind the fixed arguments once and consume through one helper:

  ```python
  def _stream(text: str, on_signal: Any) -> AsyncIterator[Event]:
      return graph_events(graph, text, config=graph_config, trace=tool_trace,
                          on_signal=on_signal, usage=turn_usage, exchanges=tool_exchanges)

  def _collect(event: Event) -> None:
      if isinstance(event, TokenEvent) and not event.agent:
          answer_parts.append(event.text)
  ```

  and call `_collect(event); yield event` in both loops. Behaviour-preserving — no change to iteration, cancellation delivery or ordering, since `_stream` returns the same async iterator rather than wrapping it in a second generator. In `graph_stream.py:303`, pass `call_id`. Behaviour-preserving.

---

## Ten symbols are private by name and public by use, three of them exported in `__all__`

- **Severity**: medium
- **Location**: `src/chemclaw/api/state.py:53` (`_LiveSessions`), `:170` (`_WORKER_ID`), `:179` (`_claim_turn_slot`), `:214` (`_hold_turn_claim`), `:261` (`_release_turn_claim`), `:313` (`_default_owner_store`), `:328` (`_default_turn_claims`); `src/chemclaw/api/schemas.py:29` (`_TRANSCRIPT_ARG_CHARS`), `:278` (`_transcript`), `:393` (`_proposal_summary`); consumers at `src/chemclaw/api/app.py:77-79,101-107,234,241,273`, `src/chemclaw/api/routes/turns.py:27-31,75,96,207,225`, `src/chemclaw/api/routes/sessions.py:133`, `src/chemclaw/api/routes/proposals.py:97,105`
- **Trigger**: any change to one of these signatures. A reviewer (or a dead-code tool, or an IDE's "unused private member" hint) reads a leading underscore as "this module owns it", and here every one of them is a cross-module contract with two to four call sites in other files.
- **Consequence**: the naming actively misleads about blast radius. `api/app.py:97-107` makes the contradiction explicit — its `__all__` lists `"_LiveSessions"`, `"_TRANSCRIPT_ARG_CHARS"` and `"_transcript"`, i.e. the module declares as its public export surface three names spelled as private. `_claim_turn_slot` and `_release_turn_claim` are the front door's concurrency guard, reached from `routes/turns.py` at four sites; `_hold_turn_claim` is spawned as a bare task at `turns.py:96`. These are the highest-consequence functions in the front door and they are spelled as if nobody outside `state.py` could be affected.
- **Evidence**: the grep of cross-module references listed above; `src/chemclaw/api/app.py:97-107` (`__all__` containing underscore names, with the comment "Types and pure helpers the suite imports from here").
- **Fix**: drop the underscore from every symbol imported by another module — `LiveSessions`, `WORKER_ID`, `claim_turn_slot`, `hold_turn_claim`, `release_turn_claim`, `default_owner_store`, `default_turn_claims`, `TRANSCRIPT_ARG_CHARS`, `transcript`, `proposal_summary` — and update the ten import sites and `__all__`. Purely mechanical and behaviour-preserving. Anything that genuinely has no cross-module caller keeps its underscore, which then means something again.

---

## `graph_events` types its token ledger and signal callback as `Any` although both concrete types are imported in the same file

- **Severity**: medium
- **Location**: `src/chemclaw/api/graph_stream.py:85-86` (`on_signal: Any, usage: Any`), `:202` (`_custom_event(payload: Any, on_signal: Any)`), `src/chemclaw/api/runner.py:710` (`_record_transcript(..., session: Any, ...)`)
- **Trigger**: any edit to `TurnUsage`, to the `Signal` union, or to `TurnSession`.
- **Consequence**: the repository runs `mypy --strict` over every first-party package (`make type`), and `Any` is the one annotation that silently switches it off for whatever it touches. `usage` is the turn's token ledger — the number the budget guard meters and the cost ledger bills — and `usage.add(graph_usage_tokens(chunk))` at `graph_stream.py:135` is checked against nothing. `on_signal(signal)` at `:216` is likewise unchecked, so a change to the callback contract (the runner passes `lambda s: started_jobs.append(s.job_id) if isinstance(s, JobSignal) else None`) would not be caught. None of these `Any`s buys decoupling: `graph_stream.py:59` already imports `graph_usage_tokens` from `runner_usage` (so `TurnUsage` is one name further in the same module), and `:62-69` already imports `Signal`. `runner.py:42` already imports `TurnSession`, and `run_turn` itself annotates the same object as `TurnSession` at `:113` before passing it to `_record_transcript` as `Any`.
- **Evidence**: `graph_stream.py:59` `from chemclaw.api.runner_usage import graph_usage_tokens`; `graph_stream.py:68` `Signal,`; `runner.py:42` `from chemclaw.agent.session import TurnSession`; `runner.py:113` `session: TurnSession` vs `runner.py:710` `session: Any`. The three documented `Any`s in this slice (`connectors`, `history`, `graph_factory` in `run_turn`) each carry a written reason; these four carry none.
- **Fix**: `usage: TurnUsage`, `on_signal: Callable[[Signal], None]`, `payload: object`, `session: TurnSession`. Behaviour-preserving at runtime. One caveat to handle in the same change: `tests/test_langgraph_stream.py:33` passes a duck-typed `_Usage` fake — either widen it to a real `TurnUsage` or declare a two-method `Protocol` for it; do not leave the `Any` to accommodate a test fake.

---

## Two module docstrings advertise an approval-prompt renderer that does not exist, and cite a call that was deleted

- **Severity**: low
- **Location**: `src/chemclaw/api/runner_trace.py:1` (module title), `src/chemclaw/api/runner.py:15-17`, `src/chemclaw/api/runner.py:467,481`
- **Trigger**: reading either module to find where approval prompts are rendered.
- **Consequence**: `runner_trace.py`'s title is *"Reading a turn's streamed updates: tool-call reassembly and the approval prompt."* The string "approval" appears exactly once in the file — in that title. There is no approval code in the module. `runner.py:15-16` repeats the claim from the outside: "`api/runner_trace.py` (reassembling a streamed tool call **and rendering an approval prompt**)". Approval prompts are actually produced by `graph_stream._signal_event` from an `ApprovalSignal` (`graph_stream.py:408-412`). Separately, `runner.py:467` and `:481` reason about the rollback predicate in terms of "the last `agent.run`" — a call that does not exist anywhere in the file (the runner drives `graph_events`), so the two comments that explain the single subtlest predicate in the module explain it against the previous engine's API.
- **Evidence**: `grep -ci approval src/chemclaw/api/runner_trace.py` → 1 (the title); `grep -n "agent\.run" src/chemclaw/api/runner.py` → only `:467` and `:481`, both inside comments.
- **Fix**: retitle `runner_trace.py` to what it is (see the first finding — "the turn's tool-call ledger"), delete the approval clause from `runner.py:15-16`, and replace `agent.run` with `graph_events` in the two rollback comments. Documentation-only.

---

## An orphaned constant comment sits above `ToolCallTrace` describing a constant that no longer exists

- **Severity**: low
- **Location**: `src/chemclaw/api/runner_trace.py:40-46`
- **Trigger**: reading the file top-down.
- **Consequence**: six lines of module-level comment explaining "how many characters of a tool call's arguments the trace event carries", ending with an explanation of why it "now reads the same setting rather than repeating its default" — followed by two blank lines and `class ToolCallTrace:`. The constant it documents was removed when the value moved to `settings.agent_audit_max_arg_chars`; the comment stayed. Positioned where it is, it reads as the class's own leading comment, so the first thing a reader learns about `ToolCallTrace` is a truncation budget that is applied 177 lines later at `:217` and `:239`.
- **Evidence**: `runner_trace.py:40-48` — the comment block, then blank lines, then the class with no intervening definition.
- **Fix**: delete the block, or move its one live sentence ("this is the same budget the audit trail applies, which is why it reads `settings.agent_audit_max_arg_chars`") to the two lines that read the setting. Documentation-only.

---

## `_LiveSessions` is a pass-through wrapper over `BoundedLru` that adds one constructor call

- **Severity**: low
- **Location**: `src/chemclaw/api/state.py:53-111` (`_LiveSessions`), against `src/chemclaw/core/bounded.py:44-95` (`BoundedLru`)
- **Trigger**: any change to the live-session cache's semantics — it has to be understood in two places.
- **Consequence**: 59 lines (class + three methods + docstrings) whose entire delta over `BoundedLru[str, LiveSession]` is that `add()` builds the `LiveSession` record and returns it. `__len__` delegates to `BoundedLru.__len__`, which already exists (`bounded.py:62`); `get` delegates to `BoundedLru.get` (`bounded.py:70`). The eviction/pinning semantics that make up the bulk of the class docstring (`state.py:53-74`) are `BoundedLru`'s, restated. There are four call sites total: `deps.py:106`, `deps.py:128`, `deps.py:143`, `routes/sessions.py:64`.
- **Evidence**: `bounded.py` exposes `__init__, __len__, __contains__, get, peek, put`; `_LiveSessions` exposes `__init__, __len__, add, get` and forwards all four.
- **Fix**: type `app.state.live_sessions` as `BoundedLru[str, LiveSession]` and let the two `add` sites construct the record:

  ```python
  entry = LiveSession(session=session, owner=owner, profile=profile)
  front.live_sessions.put(session_id, entry)
  ```

  Behaviour-preserving (`add` is exactly `put` plus the constructor plus returning the value it just built). It removes one of the two places the eviction/pinning contract is documented, which is the actual cost being paid here. Note this touches `api/app.py`'s `__all__`, so fold it into the naming fix above.

---

## The handoff/`agent`-attribution state machine in `graph_events` is unreachable

- **Severity**: low
- **Location**: `src/chemclaw/api/graph_stream.py:130` (`agent = ""`), `:159-163` (the `HandoffEvent` branch), `:153`, `:190` (`agent or (...)`), `:413-419` (`_signal_event`'s `HandoffSignal` arm)
- **Trigger**: no input reaches it. `HandoffSignal` is emitted only by `core.turn_signals.record_handoff` (`turn_signals.py:226-232`), which has zero callers in `src/` — its own definition is the only occurrence.
- **Consequence**: `agent` is provably `""` for every turn, so `agent or ("subagent" if namespace else "")` at `:153` and `:190` is just `"subagent" if namespace else ""`, and the `if isinstance(event, HandoffEvent): agent = event.to` branch never runs. The subagent attribution that *does* work comes entirely from the namespace test. Roughly 40 lines of comment across `:138-149`, `:159-163` and `_from_update`'s docstring `:244-263` argue about which of two attribution mechanisms is authoritative, when only one of them is connected to anything. The comments are honest about it ("Nothing raises this event today"), which is better than the alternative but does not remove the reading cost — a reader must reconstruct the reachability argument themselves to know which half of the mechanism is live.
- **Evidence**: `grep -rn "record_handoff" src/` returns only `core/turn_signals.py:226` (the `def`). `grep -rn "HandoffSignal" src/` returns only the class, the union member, the `_emit` in `record_handoff`, and `graph_stream`'s import and `isinstance` check.
- **Fix**: this is deliberately-kept scaffolding for subagent work (`events.py:444-447` says the union member is retained because removing it is a cross-repo change), so keep `HandoffEvent` in the union and keep `_signal_event`'s arm. But collapse the caller: delete the `agent` local, the `HandoffEvent` branch at `:159-163`, and the `agent or (...)` disjunctions, replacing them with the namespace test that is actually doing the work; and drop the `agent: str` parameter threading through `_from_update`, whose docstring already spends 20 lines saying it is always empty. Behaviour-preserving today by construction, and it makes re-adding handoffs a change with one obvious insertion point rather than a mechanism half-wired and half-argued.
