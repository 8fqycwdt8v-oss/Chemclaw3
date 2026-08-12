# D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it — Prompt caching, in the seam that knows the provider

**Status:** accepted · **Date:** 2026-08-12

## Context

Every model call re-sent the whole static prefix at full price. Measured live across 22 billed
turns on `claude-haiku-4-5`: `cache_read_tokens = 0` and `cache_write_tokens = 0` on **every** turn,
an input:output ratio of **199:1** (3,783,663 in against 18,967 out), and single turns reaching
189k–260k input tokens. Three `claude-sonnet-5` turns then cost €5.38 — more than all 22 haiku turns
together — at roughly 616k input tokens each.

That prefix is not incidental and it is not small. Measured on the default profile: 25,548
characters of system prompt across two blocks (the instructions plus what `SkillsMiddleware`
publishes) and 29 tool schemas — about **21,300 tokens**, byte-identical for the life of a profile,
sitting ahead of a conversation tail that is not.

`grep -rn "cache_control\|ephemeral" src/` returned nothing in the agent layer, and a captured
request payload confirmed it: no `cache_control` anywhere. The *ledger* meanwhile already had the
shape for it — `api/runner_usage.py` splits usage into input/output/cache-read/cache-write and
`turn_costs` has both cache columns (REV-10, D-144) — so the observability had been built for a
mechanism that was never wired.

## Decisions

### 1. The caching middleware is chosen in the F0 provider seam, not beside the middleware chain

`agent/llm_provider.prompt_caching_middleware()` returns `[AnthropicPromptCachingMiddleware(...)]`
on the Anthropic path and `[]` on `openai_compatible`; `build_langgraph_agent` splices whatever it
returns and stays provider-agnostic, exactly as it does for the model itself.

This is the same argument F0 already makes and not a new one. A cache breakpoint is spelled
`cache_control: {"type": "ephemeral"}` on Anthropic and has **no counterpart** on the internal
OpenAI-compatible endpoint, so *which* middleware a deployment gets is a provider question, and the
seam is where provider questions are answered. It is also what the layering gate permits:
`tests/test_third_party_layering.py` allows `("chemclaw.agent", "llm")` only at function scope, so
a module-level `langchain_anthropic` import next to the middleware chain would have failed it.

The production `openai_compatible` target therefore gets an empty list and never imports
`langchain_anthropic` at all — the guarantee is structural rather than resting on somebody else's
`isinstance` check.

### 2. Upstream's middleware, not a hand-rolled breakpoint pass

`langchain_anthropic.middleware.AnthropicPromptCachingMiddleware` (1.5.1) already places the three
breakpoints this needs: the last block of the system prompt, the last tool definition, and — via a
top-level `cache_control` request parameter — the message tail. `langgraph_agent`'s own opening
argument applies: use the framework's machinery rather than re-implement it.

The Anthropic wire order is `tools` → `system` → `messages`, so the system breakpoint caches the
tool schemas with it; the tool breakpoint caches the schemas alone, so a changed system prompt does
not also cost them; and the tail breakpoint means each call in a tool loop reads what the previous
call wrote, which is what makes a long turn cheap rather than only its first hop. Three of the
API's four allowed breakpoints.

`unsupported_model_behavior="ignore"` rather than upstream's `"warn"`, because every test that
calls `build_langgraph_agent(model=fake)` is the "unsupported model" case and a warning per model
call would be noise about an expected situation.

TTL stays at upstream's 5-minute default. The 1-hour cache doubles the write premium (2× rather
than 1.25×) to buy survival across idle gaps — a trade a deployment with measured traffic can make
and this seam cannot make for it, and a second setting with no reader is what `agent/compaction.py`
records the cost of.

### 3. `llm_prompt_caching` defaults to **on**

A cache write costs 1.25× and a read 0.1×, so two calls over one prefix already repay the write.
One agent turn makes a model call per tool round trip, so break-even arrives *inside the first
turn* rather than across turns — and the prefix here is ~21,300 tokens, far above every model's
minimum cacheable size. Off is for a deployment that has measured the opposite: turns rarer than
the 5-minute TTL with exactly one model call each, where every write is paid and never read.

Below the provider's minimum cacheable prefix (~1,024 tokens, and **not monotonic** across
models — 512 on the newest, 4,096 on some) the breakpoint is accepted, no entry is created, both
cache counters come back zero, and the request is answered normally. It degrades silently; there is
no error to handle. Nothing in this repo counts tokens to pre-empt that, because a threshold copied
in here would be a second and staler statement of a number only the provider knows.

### 4. The cost ledger was reading the wrong key, and every real cache write was booked as input

This is the finding, not the tidying. `graph_usage_tokens` read `input_token_details["cache_creation"]`.
LangChain's Anthropic reader publishes cache writes **twice** — once flat as `cache_creation`, once
broken out per TTL as `ephemeral_5m_input_tokens`/`ephemeral_1h_input_tokens` — and when the per-TTL
breakdown is present it **zeroes the flat key** to avoid double counting. Anthropic returns that
breakdown, so the flat key is zero exactly when a write happened.

Measured live, first call over the cached prefix:

```
{"cache_read": 0, "cache_creation": 0, "ephemeral_5m_input_tokens": 21325}
```

Reading only the flat key booked all 21,325 written tokens as full-price `input`, left `cache_write`
at 0, and wrote a `turn_costs` row claiming a deployment that caches on every turn had never written
a cache entry — understating the call (a write is 1.25×) while the counter that exists to show
caching working showed it never happening. `_cache_creation` now reads `per-TTL or flat`, mirroring
upstream's own rule for the same quantity so this can never disagree with the `input_tokens` total
it is subtracted from.

The two TTLs are summed into the one `cache_write_tokens` column. They are priced differently
(1.25× vs 2×), so a deployment running both at once would want a column each; nothing sets a TTL
other than the 5-minute default, so that split would be a migration for a distinction no caller can
currently make.

## Consequences

Measured live end to end, replaying the payload the graph actually builds through
`claude-haiku-4-5`, twice:

| | `input` | `cache_read` | `cache_write` |
|---|---:|---:|---:|
| call 1 (cold tail) | 3 | 15,136 | 6,208 |
| call 2 | 3 | 21,344 | 0 |

21,344 of 21,347 input tokens served from cache on the repeat, against 21,328 at full price before
this existed. At Anthropic's 0.1× read rate that is roughly a **90% cut in input cost** on every
call after the first, on the traffic shape the finding measured.

`tests/test_prompt_caching.py` pins all of it: the seam's decision per provider, the three
breakpoints in a captured payload (with the caching-off pair that makes the positive mean
something), the ledger against the *recorded* shape of a live cached call, and one credential-gated
live test that is the only one proving the provider honoured any of it.

## Alternatives considered

**A breakpoint on the system prompt only.** Cheaper to reason about and it would have cached the
same ~21,300 tokens, but it leaves the conversation tail at full price on every hop of a tool loop
— which is where the 199:1 ratio actually comes from.

**Reading `cache_creation` and the TTL keys additively.** Correct today by construction (upstream
zeroes one when the other is set) and wrong the moment that invariant changes. `specific or flat` is
upstream's own rule for the same number, so the two cannot drift apart.
