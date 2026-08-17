# api runtime — security and hardening

Slice: `src/chemclaw/api/runner.py`, `runner_trace.py`, `graph_stream.py`, `events.py`, `state.py`,
`schemas.py`.

Everything below was reproduced by running code in this checkout (`uv run`), not read off a
docstring. Scripts are in `/tmp/repro_*.py`; their output is quoted verbatim.

Things I checked and found sound, so nobody re-spends the time: the ownership scoping of
`tool_result_links` (a content-addressed ref is joined through the session link, so an identical
blob shared between two sessions is not cross-readable); `MessageIn._bounded` (the 100 k-char cap is
read from settings at validation time, as claimed); `SessionIn.profile` (validated against the
registry in `routes/sessions.py` before it can ever reach `spend_labels`, so the metric-label
cardinality argument in `runner.py:541` holds); `_job_results_message` (job ids *and* payloads are
inside `frame_untrusted`, and `framing._defang` covers the invisible-character spellings);
`_claim_turn_slot`'s check-and-set (genuinely no `await` between the test and the write); no SQL
interpolation, no dynamic import, no outbound URL construction anywhere in the slice.

---

## A one-shot plan approval is not spent on two reachable turn endings, so one human approval authorizes unbounded state-changing turns

- **Severity**: high
- **Location**: `src/chemclaw/api/runner.py:431` (the `return` on the empty-answer branch) and
  `src/chemclaw/api/runner.py:450-502` (the `except (GeneratorExit, asyncio.CancelledError)` clause),
  against `run_turn`'s only two calls to `consume_turn_approval` at lines 448-449 and 522-523.
- **Trigger**: a deployment with the shipped `plan_only` posture (`gate_applies(get_profile(None))`
  is `True` for `harness_enabled=True, harness_autonomy="plan_only"`). A human approves plan *P*
  once. Then either
  1. **disconnect**: `POST /sessions/{id}/messages`, watch the stream until the `tool_result` event
     for the gated tool arrives, then close the connection; or
  2. **empty answer**: get the model to make its gated tool call and emit no prose — the exact
     shape `runner.py:387-400` documents as observed in a live run (29 tool calls, 197 s, empty
     answer).

  Repeat from step 1. The plan is not rewritten, so `plan_identity` keeps matching *P* and
  `approval_stands` keeps returning `True`.
- **Consequence**: `plan_approvals.consumed_at` is never written, so the approval stays live
  forever. Every repetition executes `STATE_CHANGING_TOOLS` — `propose_knowledge_note` (a
  knowledge-graph write), every durable job launcher — under one human decision. This is DARK-1's
  outcome ("one decision authorized every later turn"), which `plan_gate.gate_applies` says the
  runner exists to prevent, reachable from the client side at will.

  `consume_turn_approval`'s docstring justifies the cancellation gap with *"a turn torn down before
  it answered deliberately does not spend the approval: a turn that was undone has not used its
  authorization."* The premise is false. The teardown at `runner.py:500-501` restores
  `session.state` and nothing else — the module's own comment says so ("No durable delete
  accompanies this any more"). Tool side effects that already ran are not undone by anything.
  The empty-answer path has no such justification at all: the block comment at `runner.py:421-431`
  enumerates three things the old fall-through broke and adds `return` to fix them, without noticing
  that the `return` now also jumps over the approval spend.
- **Evidence**: both paths reproduced against the real `run_turn` with a stub graph.

  `/tmp/repro_approval.py` (disconnect):
  ```
  gate_applies(default profile): True
  client saw: tool_result propose_knowledge_note -> disconnecting now
  gated tools that actually executed: ['propose_knowledge_note']
  consume_turn_approval called for: []
  RESULT: APPROVAL STILL LIVE (bypass)
  ```

  `/tmp/repro_empty.py` (empty answer, no disconnect needed):
  ```
  events: ['tool_call', 'tool_result', 'error']
  gated tools executed: ['propose_knowledge_note']
  consume_turn_approval called: []
  RESULT: APPROVAL STILL LIVE (bypass)
  ```

  `grep -rn "\.consume("` confirms `plan_approval_store().consume` has exactly one caller,
  `consume_turn_approval`, which has exactly the two call sites above.
- **Fix**: spend the approval when the *first gated call executes*, not when the turn ends. The
  cheapest correct version keeps `run_turn` out of it: have `enforce_plan_approval` mark the
  approval consumed on the call it lets through (it already `await`s the store, and
  `consume_turn_approval` is documented as idempotent), and keep the end-of-turn call as a
  belt-and-braces no-op. If it must stay in the runner, add the spend before the `return` at
  line 431 and make the cancellation clause spend it too when `tool_trace.called_tools` contains a
  gated call — the clause cannot `await` (D-130), so schedule it as a shielded task the way
  `_release_turn_claim` already does.

---

## Unredacted exception text — internal hostnames, server file paths — is streamed to the browser on `tool_failed`

- **Severity**: medium
- **Location**: `src/chemclaw/api/graph_stream.py:413-414` (`_signal_event`, the
  `ToolFailureSignal → ToolFailedEvent` arm) and the contract it fills,
  `src/chemclaw/api/events.py:260-272`.
- **Trigger**: any tool call that raises an exception outside the two vetted families. The producer
  is `tool_authz.announce_tool_failures`, which calls `failure_detail(exc)` =
  `f"{type(exc).__name__}: {exc}"`; `_signal_event` copies that string onto `ToolFailedEvent.message`
  and `routes/turns.py:180` serialises it as one SSE `data:` line.
- **Consequence**: an authenticated end user's browser receives the raw text of internal faults.
  Concretely: a `psycopg.OperationalError` carries the DSN's host, port and user; a
  `FileNotFoundError` carries an absolute server path. This directly contradicts `runner.py`'s
  module docstring — *"Errors are turned into a single `ErrorEvent` with a user-safe message rather
  than propagating a stack trace to the browser — a failed turn must not take down the stream or
  leak internals"* — and contradicts the *same repo's own decision about the same string*:
  `tool_authz.unexpected_error_result` withholds it from the **model** precisely because "its text
  can carry a DSN, a path or a row of data", then hands it to the browser two middlewares later.
  `events.py` documents `ErrorEvent` as "safe to show the user (no stack traces)" and says nothing
  of the kind about `ToolFailedEvent`, which is the one that is not.

  Amplification worth noting (not separately reproduced): where a tool takes a caller-influenced
  path or identifier, the distinction between `FileNotFoundError`, `IsADirectoryError` and
  `PermissionError` in this field is a filesystem-existence oracle driven from the chat box.
- **Evidence**: `/tmp/repro_leak.py` builds the real signal and runs it through the real
  `_signal_event`:
  ```
  event: tool_failed
  data: {"type":"tool_failed","tool":"query_eln","message":"OperationalError: failed to resolve host 'db-prod-01.internal.example': [Errno -2] Name or service not known","agent":""}

  event: tool_failed
  data: {"type":"tool_failed","tool":"query_eln","message":"FileNotFoundError: [Errno 2] No such file or directory: '/etc/chemclaw/secrets/llm_api_key'","agent":""}
  ```
- **Fix**: apply the split the codebase already made for the model, one layer further out. In
  `_signal_event`, emit the exception *type* plus the turn's `correlation_id` for an unvetted
  failure and keep the message only for the families someone vetted (`ChemclawError`,
  `SubsystemUnavailableError`, and an MCP server's own sanitized `returned_failure_detail`, which is
  already written for a reader). Put the full text where the runner already puts exception detail —
  the server log. If a deployment genuinely wants verbose failures on the wire, gate it on the
  existing `include_detailed_errors` knob rather than defaulting it on.

---

## `ToolResultEvent.note_ids` is uncapped, defeating every other budget on that event

- **Severity**: medium
- **Location**: `src/chemclaw/api/runner_trace.py:237-245` (`ToolCallTrace.returned`, the
  `note_ids=mentioned_ids(text)` argument), against the claims in
  `src/chemclaw/api/events.py:292-297` and `:305-312`.
- **Trigger**: one tool call returning a large result containing many distinct note ids — a wide
  `gather_evidence` sweep, an MCP connector answering with a big payload, or a document whose text
  an ELN/share ingest put into the corpus. No authentication beyond "can take a turn" is needed, and
  the *storage* cap does not stop it: a result over `stream_max_result_bytes` is refused by
  `_stored_ref` (which logs and returns `""`) and the event is still built and yielded.
- **Consequence**: every other field on this event is bounded — `preview` to
  `agent_audit_max_arg_chars` (200), `numbers` to `stream_max_result_numbers` (512), `result_ref` to
  a 64-char digest — and `note_ids` is O(size of the tool result). Measured, a 4 MiB result the
  store *rejected* still produced a **2.29 MiB single SSE frame** and 119 838 list entries. The
  event's own docstring justifies capping `numbers` with "a result is arbitrary text and the event
  goes to a browser, so the list must be bounded", and then asserts of `note_ids`: *"Bounded by the
  notes one call can return, which is far smaller than their text."* That is the checkable claim,
  and it is wrong — the list is linear in the text, and nothing bounds the text.
  Server-side the same call also holds the full result in `ToolCallTrace.outputs` (`runner_trace.py:111`)
  for the turn's whole life, with no cap and no eviction, so the memory cost is paid twice per call.
- **Evidence**: `/tmp/repro_noteids.py`, driving the real `ToolCallTrace.returned`:
  ```
  tool result size          : 4.00 MiB
  stream_max_result_bytes   : 131072  -> result is OVER the cap, not stored
  preview chars             : 200 (capped)
  numbers                   : 0 (capped at 512)
  note_ids                  : 119838 (NO CAP)
  serialized SSE frame      : 2.29 MiB
  ```
  A result *within* the store cap (128 KiB) is not benign either — `/tmp/note_ids_amp.py` gives
  9 363 ids and a 103 KB frame, a 517× overrun of the preview budget this event exists to keep.
- **Fix**: give `note_ids` the treatment `_capped_numbers` already implements — a
  `stream_max_result_note_ids` setting, truncate past it, and log what was dropped (a dropped id can
  only cost a citation its verification, exactly the safe direction the numbers cap argues for).
  Separately, cap what `returned` retains: either bound `self.outputs` by total bytes or store the
  grounding corpus by ref rather than by value.

---

## `active_turns` is unbounded and its entries pin `_LiveSessions` past its cap; the docstring's bound is not the bound

- **Severity**: medium
- **Location**: `src/chemclaw/api/state.py:88-107` (`_LiveSessions.add` and its eviction argument)
  and `src/chemclaw/api/state.py:179-211` (`_claim_turn_slot`). The pin source is
  `api/app.py:218-226` (`_turn_in_flight`), which reads `app.state.active_turns`.
- **Trigger**: one authenticated caller mints N sessions and issues N concurrent
  `POST /sessions/{id}/messages`. `routes/turns.py:75` calls `_claim_turn_slot` **before**
  `semaphore.acquire()` at lines 105/124, so every one of the N requests takes a lease and pins its
  live session, regardless of how many will ever hold a permit. Note there is no per-principal rate
  limit by default: `service_rate_limit_per_minute` is `0.0`, which `enforce_request_budget` treats
  as off.
- **Consequence**: two bounds fail at once.
  - `_LiveSessions.add`'s docstring justifies the over-capacity window with *"turns in flight are
    bounded by the admission semaphore, orders of magnitude below the cache cap"*. They are not:
    pins are bounded by concurrent in-flight requests, not by `service_max_concurrent_turns`. The
    LRU grows without limit while the pins hold.
  - `active_turns` itself has no cap at all, and `_claim_turn_slot` sweeps it with an O(n)
    `list(active_turns.items())` on **every** POST — so an attacker-grown map makes each subsequent
    request more expensive (and the `chemclaw_turns_in_flight` gauge does the same O(n) sum on every
    Prometheus scrape).

  The entries are normally released by the SSE generator's `finally`, so the sustained version needs
  the window `_claim_turn_slot`'s own docstring names — "a client gone after the streaming response
  is handed off but before its generator is first advanced. An async generator that never started
  runs no `finally` at all" — in which the lease is held for its full
  `service_turn_timeout_seconds + service_turn_admission_timeout_seconds` = **605 s** with no
  cleanup at all. The transient version needs nothing but concurrency.
- **Evidence**: `/tmp/repro_pin.py`, driving the real `_LiveSessions` and `_claim_turn_slot`:
  ```
  service_max_live_sessions = 10
  service_max_concurrent_turns = 8
  live sessions actually held  = 500
  active_turns leases held     = 500
  lease duration (s)           = 605.0
  ```
- **Fix**: take the `active_turns` lease *after* the admission permit, or (better, since the 409
  must be answerable with a status code before the response is handed off) keep the pre-admission
  claim but cap `active_turns` — it is another "keyed by an unbounded identity" map, exactly the
  bug class `core/bounded.BoundedLru` exists for, and it should be one. Correct the `_LiveSessions`
  docstring to say what actually bounds the pin set. Sweep expired leases on a timer as well as on
  arrival so the O(n) scan is not paid per request.

---

## `run_turn`'s teardown `finally` is not exception-safe, and on the abandonment path it raises and skips the five contextvar resets it promises

- **Severity**: low
- **Location**: `src/chemclaw/api/runner.py:524-594`, specifically `end_call_watch(calls_token)` at
  line 588 and everything after it.
- **Trigger**: the caller's `async for` frame unwinds before the generator is closed — an exception
  in the loop body, or the cancellation delivered to the outer frame rather than into `run_turn`'s
  own await. The generator is then abandoned and finalised by asyncio's async-gen finaliser, which
  runs `athrow(GeneratorExit)` in a **fresh task with a fresh context**, so every token created
  during the turn is foreign and `ContextVar.reset(token)` raises `ValueError`.
- **Consequence**: the `finally` aborts at line 588. `end_loop_watch`, `reset_dry_run`,
  `reset_current_session_id`, `reset_current_correlation_id` and `reset_current_identity` never run
  — the exact set the block comment at lines 525-529 says must always run ("would leak one turn's
  ambient identity into the next turn on this worker"). The comment guards only against an `await`
  in the block; the mechanism that actually skips them is a raising `reset()`, which the block has
  no protection against. The failure is also unattributable in the log: it surfaces as a bare
  `Task exception was never retrieved`, which is precisely the shape `_release_turn_claim`'s
  docstring identifies as the thing to avoid.

  I could **not** demonstrate cross-turn identity leakage from this, and say so rather than
  overstate it: each HTTP request runs in its own task with its own context copy, so the
  un-reset vars belong to a context that is being discarded anyway. What is demonstrated is that the
  teardown block's stated guarantee does not hold and that a per-abandoned-turn orphan traceback is
  produced.
- **Evidence**: emitted by `/tmp/repro_approval.py` above, unprompted:
  ```
  Task exception was never retrieved
  future: <Task finished ... coro=<<async_generator_athrow without __name__>()>
    exception=ValueError("<Token var=<ContextVar name='chemclaw_repeated_calls' ...>
    was created in a different Context")>
  ...
    File "/home/user/Chemclaw3/src/chemclaw/api/runner.py", line 588, in run_turn
      end_call_watch(calls_token)
    File "/home/user/Chemclaw3/src/chemclaw/agent/repeat_guard.py", line 66, in end_call_watch
      _calls.reset(token)
  ValueError: <Token ...> was created in a different Context
  ```
- **Fix**: make each reset independent and non-fatal — either wrap the five in one helper that
  catches `ValueError` per var and logs once, or (cleaner) stop relying on token resets for values
  whose whole lifetime is the turn and let the context copy die with the task. Whichever way, the
  block must not be able to abort halfway; a teardown whose remaining steps depend on the previous
  one not raising is a teardown that does not run.

---

## A subagent's tool output joins the parent turn's grounding corpus and its fetchable result store, contrary to the comment that says the namespace test prevents it

- **Severity**: low (latent — nothing raises subagents in this tree today)
- **Location**: `src/chemclaw/api/graph_stream.py:166-198` (the `below_root` comment) versus
  `:284-306` (the `trace.returned` call, which is unconditional on attribution).
- **Trigger**: any update arriving under a non-empty namespace carrying a `ToolMessage` — i.e. any
  tool a `task`-dispatched helper runs, once subagents return.
- **Consequence**: the comment states that without the namespace test a helper's "output joined
  `ToolCallTrace.outputs` and the parent session's fetchable `result_ref` indistinguishably, and its
  `write_todos` surfaced as a root `PlanEvent`". The test that was added fixes the *event
  attribution* and *withholds the plan*; it changes nothing about `outputs`, `called_tools` or the
  blob written through the sink. So a helper's tool output still (a) becomes part of the corpus
  `score_answer` grades the supervisor's answer against and (b) is stored and advertised as
  fetchable under the **parent session's** id. Under
  `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`'s own rule, a helper may be running a
  narrower surface than its caller; nothing here carries that attenuation into the result store.
- **Evidence**: `/tmp/repro_subagent.py`, driving the real `graph_events` with a namespace of
  `("tools:abc123",)`:
  ```
  events: [('tool_call', 'subagent'), ('tool_result', 'subagent')]
  trace.outputs (the answer-grounding corpus): ['HELPER-ONLY EVIDENCE']
  trace.called_tools: ['gather_evidence']
  blobs written to the PARENT session's store: [('gather_evidence', 'HELPER-ONLY EVIDENCE')]
  result_ref advertised: ['ref-gather_evidence']
  ```
- **Fix**: pass `below_root` down and skip `trace.returned` for it (emit the `ToolResultEvent`
  directly, attributed, without touching `outputs` or the sink), or give `ToolCallTrace` a separate
  sub-corpus so the answer gate grades against what the *supervisor* actually saw. Either way,
  correct the comment so it describes what the code does.
