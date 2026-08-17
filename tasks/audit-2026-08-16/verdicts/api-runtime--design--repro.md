# Reproduction verdicts — `api runtime — design and simplification`

Scope: only findings marked **critical** or **high**. The file contains exactly one such finding
(the `runner_trace.py` dead-code finding, severity `high`); the other eight are medium/low and were
not examined.

Working tree checked against the pristine `HEAD` copy first:
`diff -q pristine/src/chemclaw/api/{runner_trace,runner,graph_stream}.py <working tree>` → no
differences, so nothing below is an artefact of another agent's mutation experiment.

---

## Half of `runner_trace.py` is dead: the streamed-reassembly machinery has no production caller

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. My own AST scan of all of `src/`** (`/tmp/ast_feed.py` — walks every `*.py` under `src/`, collects
every `Call` whose func is an `Attribute` named `feed`/`flush`/`issued`/`returned`, and every
`Attribute` access named `_fragments`/`_names`/`outputs`/`called_tools`). Cross-module hits:

```
('src/chemclaw/api/graph_stream.py', 275, 'issued')
('src/chemclaw/api/graph_stream.py', 302, 'returned')
('src/chemclaw/api/runner.py', 324, 'flush')
('src/chemclaw/api/runner.py', 407, 'attr:called_tools')
('src/chemclaw/api/runner.py', 412, 'attr:called_tools')
('src/chemclaw/api/runner.py', 434, 'attr:outputs')
('src/chemclaw/api/runner.py', 435, 'attr:called_tools')
```

Byte-identical to the reporter's table. **No `.feed(` call exists anywhere in `src/`.** A plain
`grep -rn "feed" --include=*.py src/` returns only prose: the module/class docstrings in
`runner_trace.py` itself and three comments in `cli/{live_storm,mock_llm,storm_behaviours}.py` that
*describe* `feed`'s behaviour without calling it.

Intra-module, `_fragments` is written only at `:175` and `:182` and `_names` only at `:142` — all
three inside `feed` — and popped only in `_take` (`:250`, `:253`). `_result_text` is called only from
`feed:152`; `_arguments_complete` only from `feed:186`. So the reporter's dependency claim holds.

**2. Runtime instrumentation of the production path.** I wrapped `ToolCallTrace.feed` and
`.flush` at import time (`/tmp/probe/trace_probe.py`, loaded as a pytest plugin), attributing each
call by scanning the *whole* stack for `/src/chemclaw/` frames outside `runner_trace.py` — not by the
immediate caller frame, which mis-attributes because `tests/test_runner.py` contains the substring
`runner.py` and because `tests.fakes.fed` calls through `asyncio.run` (my first pass got a false
"non-empty at flush" hit from exactly that; the corrected pass is below).

```
$ PYTHONPATH=/tmp/probe uv run pytest -q -p trace_probe tests/test_runner.py tests/test_service.py \
    tests/test_service_events.py tests/test_langgraph_stream.py tests/test_tool_results.py \
    tests/test_review_2026_08_05.py
149 passed in 22.72s
=== TRACE PROBE ===
{'feed_calls': 59, 'feed_from_src': 0, 'flush_from_runner': 59}
feed called from src: []
flush-from-runner with non-empty buffers:
```

59 real turns reached `runner.py:324`. In **zero** of them were `_fragments` or `_names` non-empty at
that point, and **zero** of the 59 `feed` calls came from any `src/` frame — every one was a test
calling `tests.fakes.fed`.

**3. My own end-to-end turn through `run_turn`** (not through `graph_events`, which is all the
reporter drove, and with a tool call that *succeeds* rather than fails —
`/tmp/probe/my_repro.py`: patch `ToolCallTrace.__init__` to capture the instance the runner builds,
then `run_turn(session, "hello", connectors=[], graph_factory=…)` over a real compiled
`build_langgraph_agent` on a `ScriptedChatModel` scripted to call `ask_clarifying_question`):

```
event types: ['ToolCallEvent', 'QuestionEvent', 'ToolResultEvent', 'TokenEvent', 'AnswerEvent']
trace._fragments: {}
trace._names: {}
trace._issued: {'call-1': 'ask_clarifying_question'}
trace.outputs: ['Question put to the chemist; awaiting their answer.']
trace.called_tools: ['ask_clarifying_question']
trace.flush(): []
```

A complete production turn with a successful tool call and a real `ToolResultEvent`: both reassembly
buffers untouched, `flush()` empty. This is strictly stronger evidence than the reporter's, whose
turn only produced a `ToolFailedEvent` and never exercised `trace.returned`.

**4. Line-count check.** The five dead spans (`:124-192`, `:194-196`, `:247-254`, `:310-329`,
`:332-353`) total **122** lines, not 124 — a 2-line overstatement, immaterial. Adding the prose that
exists only to explain them (the class docstring `:49-87` = 39 lines, the module docstring's
`feed`/duck-typing paragraphs `:12-24` = 13, the orphaned constant comment `:40-45` = 6) gives ~180 of
353, so "half the file" is fair rather than rhetorical.

### Why

The claim is a reachability claim and it settles cleanly in both directions: statically there is no
caller of `feed` in `src/`, and dynamically 59 instrumented production turns plus one hand-built
`run_turn` turn all left `_fragments`/`_names` empty and `flush()` returning `[]`. `graph_stream`
does exactly what the finding says — reads LangChain's already-whole `message.tool_calls` and
`ToolMessage` (imported by name at `graph_stream.py:42`) and calls `issued`/`returned` directly, so
nothing can ever put a fragment into the trace. The `runner.py:320-325` loop is over an empty list on
every turn.

The behaviour-preservation argument for the proposed fix also holds: with `_names` permanently `{}`,
`returned`'s `self._issued.get(key) or self._names.get(key, key)` at `:236` is already
`self._issued.get(key) or key`.

Three corrections/additions the reporter got slightly wrong or missed, none of which change the
verdict:

- The test helper is named **`fed`**, not `feed_trace` (`tests/fakes.py:81`).
- It is used by **four** files' worth of assertions, not three — the finding names
  `test_review_2026_08_05.py` (8 sites) and `test_runner.py` (32) but misses
  **`tests/test_tool_results.py` (7 sites)**.
- That miss makes the finding's own point sharper. `test_tool_results.py` is where the *live*
  behaviour of `_stored_ref` is pinned — the byte-measured cap, the oversize refusal, the
  no-sink case, `cap=0` — and every one of those tests reaches it through the dead `feed` entry
  point. The asserted behaviour is real (it lives in `returned`/`_stored_ref`, which `graph_stream`
  does call), but it is currently only reached in the suite via a path production never takes, so
  the deletion must re-point those seven cases onto `trace.returned(...)` directly rather than delete
  them with `feed`.

Two docstrings elsewhere cite `_result_text` by name (`api/schemas.py:317`, `api/tool_results.py:7`)
and would go stale with it — a note for whoever does the deletion, not a reason to keep it.

On severity: this is a design/simplification finding, so there is no runtime defect and nothing a
user can trigger. I would still keep **high** rather than drop to medium, because the misdirection is
not passive — the module's title, its class docstring and ~180 of its 353 lines describe the dead
mechanism as the module's purpose, and the suite's assurance about the *live* result-storage
behaviour is currently obtained through it.
