# Verdicts — `api-runtime--design.md`, reachability/consequence lens

Scope note: the findings file contains **no critical** finding and **one high** finding. Everything
else is medium or low and was not examined. One verdict follows.

The slice under review (`runner_trace.py`, `runner.py`, `graph_stream.py`) is byte-identical to the
pristine `HEAD` copy — no other agent's mutation is in play.

---

## Half of `runner_trace.py` is dead: the streamed-reassembly machinery has no production caller

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. Static reachability.** `grep -rn "runner_trace" --include=*.py .` outside `tests/` returns two
importers and eight prose mentions. The two importers are `api/runner.py:56` (constructs at `:271`,
calls `flush()` at `:324`, reads `called_tools` at `:407,412,435` and `outputs` at `:434`) and
`api/graph_stream.py:58` (calls `trace.issued(...)` at `:275` and `await trace.returned(...)` at
`:302`). The `feed` mentions in `cli/mock_llm.py:67`, `cli/storm_behaviours.py:113` and
`cli/live_storm.py:283` are all inside comments/docstrings. No `.feed(` call exists in `src/`.

**2. Live turn on a real compiled graph** (`/tmp/verify_flush3.py`, `build_langgraph_agent` +
`ScriptedChatModel` driven through `graph_events`, a call that *succeeds* and returns a result):

```
events: ['ToolCallEvent', 'ToolResultEvent', 'TokenEvent']
_fragments: {}
_names: {}
_issued: {'call-1': 'ls'}
flush(): []
outputs: ["['/bo/', '/calc/', '/qm/', '/safety/', '"]
called_tools: ['ls']
```

Same result for a failing call. Both reassembly buffers untouched; `flush()` empty.

**3. Mutation, isolating `feed` from `flush`.** A pytest plugin replacing `feed` with a raiser and
`flush` with `assert not self._fragments and not self._names; return []`:

```
PYTHONPATH=/tmp uv run pytest -q -p killfeed2 \
  tests/test_langgraph_stream.py tests/test_turn_observability.py tests/test_mid_turn_resume.py
42 passed in 9.74s
```

Every end-to-end `run_turn` test — full turns, cancellation-adjacent, mid-turn resume, cost/telemetry
booking — passes with `feed` unreachable and the buffers asserted empty at every `flush()`.

(A first pass that killed `flush` *as well* failed 17 tests; that is `flush`'s live call site at
`runner.py:324` being called, not evidence that it returns anything. It is why I re-ran isolated.)

**4. Measurement of the "half" claim** (`/tmp/count.py`, AST + tokenize over the file):

```
feed                lines 124-192 total= 69 docstring= 6 comment=32 blank=1 code=30
flush               lines 194-196 total=  3 docstring= 1 comment= 0 blank=0 code= 2
_take               lines 247-254 total=  8 docstring= 0 comment= 2 blank=0 code= 6
_arguments_complete lines 310-329 total= 20 docstring=12 comment= 0 blank=2 code= 6
_result_text        lines 332-353 total= 22 docstring=15 comment= 0 blank=3 code= 4
TOTAL dead: code=48 doc/comment=68 span-sum=116
file total lines: 353
```

**5. The test surface.** `grep -rn "fed(" tests/` — the helper is named `fed`, not `feed_trace`, and
it has **three** module callers, not two: `test_runner.py` (32), `test_review_2026_08_05.py` (8),
`test_tool_results.py` (7). I read the call sites.

### Why

**The mechanism is fully confirmed and I would not have needed the reporter's word for it.** Nothing
in `src/` calls `feed`; `_fragments`/`_names` have no other writer; `flush()` at `runner.py:324`
provably iterates an empty list on every turn; the surviving `_names` read at `:236` provably reduces
to `self._issued.get(key) or key`. Three independent methods agree.

What does not hold is the size, the coverage characterisation, and the severity.

**The arithmetic double-counts and the title overstates.** The five dead functions occupy 122 lines
of 353 (35%), of which **48 are executable code** — 14% of the file. The finding's "124 lines of
function body **plus** roughly 60 lines of docstring/comment" presents those as additive, but the
124-line span already contains ~68 of the doc/comment lines it then adds again. You only reach "half"
by also counting the class docstring (`:49-87`, 40 lines) and the module docstring's `feed` paragraph
(`:12-24`) — a count the finding gestures at but does not perform. ~48 lines of dead code is worth
deleting; it is not half a module.

**The coverage claim is materially wrong, and wrong in the direction that makes the Fix unsafe.**
"Three test files spend their assertions on it … coverage that reads as protection of the live tool
trace and is not" — but a large share of those assertions protect code that *is* live.
`returned`, `_capped_numbers` and `_stored_ref` are all reached from `graph_stream.py:302` on every
real turn; `fed()` is merely the vehicle those tests use to get to them. My first mutation run makes
this concrete: killing `feed` failed five `test_tool_results.py` tests that are entirely about the
live store — `test_an_oversize_result_is_refused_whole_and_says_so`,
`test_the_cap_is_measured_in_bytes_not_characters`, `test_setting_the_cap_to_zero_disables_the_store`,
`test_the_trace_names_the_result_it_stored`, `test_a_trace_with_no_sink_reports_no_ref`.

The finding's Fix says "The three test files' `feed`-driven cases go with it — they are the only
reason the code looks alive." Executed literally, that deletes live coverage. Two of the cases it
would remove matter on exactly the surface this audit is told to protect:

- `test_runner.py:478-491` — `ich_impurity_limit` for palladium. It asserts that when the result is
  longer than the 200-char preview budget, `ToolResultEvent.numbers` still carries `{100.0, 10.0,
  1.0}` **and** that those figures are absent from the preview. Delete it and a regression that drops
  `numbers` ships silently: the chemist is shown a truncated prose preview of an ICH limit table with
  the ppm figures cut off, and nothing on the wire says a number was lost.
- `test_runner.py:494-511` — the `stream_max_result_numbers` cap emitting a warning rather than
  truncating silently. Same class of harm.

These need **rewriting** to call `trace.issued(...)` / `await trace.returned(...)` directly (a
mechanical change; `graph_stream` already calls them that way), not deleting. The Fix as written is
not behaviour-preserving in the sense that matters — the runtime behaviour is preserved, the
regression detector on an impurity-limit answer is not.

Two smaller inaccuracies: the helper is `fed`, not `feed_trace`; and `_result_text` is cited as the
reference behaviour by two *other* modules' comments (`api/tool_results.py:7`,
`api/schemas.py:315-318` — "A result that came back empty gets no ref, matching
`runner_trace._result_text`"), so deleting it orphans two more comments the Fix does not mention.

**Severity high does not survive the consequence test.** There is no runtime consequence whatsoever:
no wrong answer, no exception, no leak, no degraded safety output, no divergence risk — the dead path
and the live path share `issued`/`returned`, so there are not two implementations that could drift.
The trigger is "a reader opens the file", and the cost is reading time plus a misleading module title.
That is a real cleanup and worth doing, but it is a **medium**. Reserving "high" for findings with no
observable behaviour is what makes "high" stop meaning anything.

One thing the reporter missed that slightly strengthens the case for deletion: `feed`'s `async` is
now purely vestigial. The module docstring justifies it by "the write has to happen before the event
naming it is yielded" — but that write moved into `returned`, which the live caller awaits directly.
So `tests/fakes.py::fed` exists only to `asyncio.run` a coroutine that, in every one of its 47 call
sites, never suspends.
