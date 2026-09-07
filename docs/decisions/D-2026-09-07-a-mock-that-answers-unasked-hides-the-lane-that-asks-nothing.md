# D-2026-09-07-a-mock-that-answers-unasked-hides-the-lane-that-asks-nothing — the live lane's endpoint was kinder than a gateway, and each kindness disabled a control

**Status:** accepted · **Date:** 2026-09-07 · **Extends** the LOAD-1 argument in `cli/mock_llm`'s
docstring from the tool surface to the wire · **Makes measurable**
D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget and
D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget (their estimator calibration)

## Context

`chemclaw.cli.mock_llm` is the endpoint the entire live lane runs against — `make live-storm`,
`live-soak`, `live-degradation`, `live-probes` and `infra/live/e2e-full-stack/up.sh` all start it
unless a gateway is named. Every green result those lanes produce is therefore evidence about the
mock as much as about the system, and the mock's own docstring already argues that case for the
*tool surface* (LOAD-1: a stub that emitted an argument name no tool took, published as "100 tool
calls, the tool path is genuinely exercised").

Nothing made the same argument about the *wire*. Measured 2026-09-07:
`grep -rln "chat.completion.chunk|ChatCompletionChunk" tests/` matched no file — no test in this
suite drove a turn through `_chat_stream` at all, so a frame-shape regression there was caught by
nothing, and the only check on the chat-completions route was
`tests/test_live_storm.py`'s assertion that the *route exists*.

Behind that silence, the mock was measured against a real OpenAI-compatible gateway
(`https://api.anthropic.com/v1/chat/completions`, `claude-haiku-4-5-20251001`). It diverged in four
ways. Three of them are the same failure: **the mock was more forgiving than the endpoint it stands
in for, and a kindness in a mock is silent where an omission is loud.**

### 1. Usage reported on a stream that never asked for it

```
MOCK  streaming, no stream_options → frames=7, frames_with_usage=1  {'prompt_tokens':900,…}
REAL  streaming, no stream_options → frames=7, frames_with_usage=0  None
through ChatOpenAI(stream_usage=False):  MOCK {'input_tokens':900,…}   REAL None
```

`_chat_stream` put usage on the terminal frame unconditionally. A gateway emits it only when
`stream_options.include_usage` was sent, and `ChatOpenAI` sends that key exactly when
`stream_usage=True` (verified against a capturing endpoint: `{'include_usage': True}` when on,
absent when off).

`llm_stream_usage` (`core/config/llm.py`) exists precisely as the escape hatch for an endpoint that
rejects `stream_options`. A deployment that flips it off books **0** tokens on every turn —
`turn_usage`, `api/budget.py`, `agent/spend_cap.py` and `context_budget.note_model_call` all read
that one field — while every mock-driven lane went on reporting a fully metered turn.
`_openai_compatible_model`'s docstring records this failure having shipped once already, by a
different route ("a runaway-cost guard that meters zero is not conservative, it is disarmed"); the
mock was the reason its recurrence could not be *seen*.

### 2. A constant input bill, so the estimator calibration is pinned at its clamp

```
chars=25 → MOCK prompt_tokens=900, REAL 12 | chars=2500 → MOCK 900, REAL 321 | chars=100000 → MOCK 900, REAL 12509
50 folded calls at a 10,000-token estimate: mock numbers → estimator_ratio 1.0 ; a 1.34x lane → 1.34
```

`agent/context_budget._Calibration.ratio()` is clamped at 1.0 from below by design — a
mismeasurement may only make the policy compact *earlier*. With a bill independent of request size,
`billed / estimated` is always well under 1, so the clamp answers 1.0 forever. The EWMA, the seed
correction, `agent_context_calibration_max_factor` and the ">1.0 tightens the budget" branch that
`D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` and
`D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget` both rest on were reachable only
from unit tests with hand-fed numbers.

### 3. No request-level failure, so `context_length` had no lane

```
MOCK  bad model 200 | overflow 200 | bad auth 200
REAL  bad model 404 not_found_error
      overflow  400 {"code":"invalid_request_error","message":"prompt is too long: 300024 tokens > 200000 maximum",…}
      bad auth  401
```

`Behaviour.http_status` injects a failure *per behaviour*. `llm_provider._is_context_length` — and
therefore `classify_model_failure`'s `context_length` label and `_failover_exceptions`' deliberate
refusal to fail a 400 over — needs a failure that is a property of the **request**: the same
behaviour serving a short thread and refusing a grown one. Nothing could produce that. The marker
the classifier matches on (`"prompt is too long"`) had been added on faith; it is the gateway's
actual wording, now checked.

### 4. No cached-token accounting

`turn_usage.graph_usage_tokens` carries ~60 lines of arithmetic about `cache_read`/`cache_creation`
and the service-tier prefixes upstream puts on them (`priority_cache_read`, matched by suffix). The
mock omitted `prompt_tokens_details` entirely, so `chemclaw_cache_read_tokens_total` read 0 on every
lane. The gateway probed here also reports no `prompt_tokens_details` (an OpenAI, vLLM or LiteLLM
gateway does), which makes the mock the only place that arithmetic can be driven over a wire at all.

## Decision

**A mock may be narrower than the endpoint it stands in for; it may never be more forgiving.**

1. `_chat_stream` emits usage only when the request carried `stream_options.include_usage`. The
   shipped default (`llm_stream_usage=True`) is unaffected; a lane that turns the hatch off now sees
   what a deployment sees.
2. `Behaviour.input_tokens` becomes `int | None`. `None` bills the *serialized request* at
   `input_tokens_per_char` (default 0.25 — `count_tokens_approximately`'s own chars/4), so a thread
   that grows costs more and a factor above 0.25 drives a calibration ratio above 1, including above
   `max_factor`. The constant stays the default: existing lanes are unchanged, and a behaviour that
   asserts a fixed count still can.
3. `Behaviour.refuse_over_input_tokens` refuses a request whose billed input exceeds it, with the
   gateway's own 400 body field for field. It is validated at startup against a constant
   `input_tokens` — the two knobs only mean anything together, and a size refusal over a constant
   bill is a per-behaviour failure wearing the name of a request-level one.
4. `Behaviour.cached_tokens` / `Behaviour.service_tier` publish `prompt_tokens_details.cached_tokens`
   and the response tier. Both are omitted from the wire when unset, so the default request every
   lane already gets is byte-identical.
5. `tests/test_mock_llm_contract.py` is the first test to drive a turn through the mock's wire.
   It runs `build_app` in process over `httpx.ASGITransport` under a real
   `langchain_openai.ChatOpenAI` — no port, no network, no credential — and asserts the shapes
   measured against the gateway on this date.

## Consequences

- **No lane changes, by construction.** The three knobs default to today's behaviour and the usage
  gate is satisfied by the shipped `llm_stream_usage=True`. Verified: the full suite is green and a
  storm turn still meters.
- **What is now checkable rather than believed**: that the escape hatch disarms metering; that the
  calibration can observe a ratio above 1; that `context_length` is reachable; that a cached prefix
  reaches the price split through a tier prefix; and that the tool-call frame encoding is what a
  real client assembles into exactly one call.
- **The knobs' only caller today is that test.** Wiring them into `cli/storm_behaviours.py` — a
  size-billed behaviour for the calibration, an oversize one for `make live-degradation`'s
  `context_length` arm — is a change in a file this work did not own and is the obvious next step;
  until it happens the lanes have the *capability* and not the coverage.
- **What was measured and found sound, and deliberately not changed**: the tool-call streaming
  encoding (indexed slot with `id`/`type`/`name`, then argument-only fragments on the same `index`);
  usage riding the same frame as `finish_reason` on both sides rather than a trailing choice-less
  chunk; the non-streaming body field for field; `_validate` and `already_has_tool_results`.
- **Declined**: 401 and 404 request-level branches. Measured against `classify_model_failure`, both
  land on the `error` label — exactly where an injected `http_status` already lands — so they buy no
  reachable path, and a knob with no consumer is the shape this repository deletes.

## Supersedes / relates

Extends the LOAD-1 argument in `cli/mock_llm`'s own docstring from the tool surface to the wire.
Relates to `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` and
`D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget` (whose calibration this makes
measurable), and to `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` — the numbers above
are claims about 2026-09-07 and about one gateway, which is why they are in an ADR and in test
docstrings rather than in `CLAUDE.md`.
