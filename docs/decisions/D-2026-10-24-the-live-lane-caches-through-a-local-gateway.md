# D-2026-10-24-the-live-lane-caches-through-a-local-gateway — the live lane reaches Anthropic through a local caching gateway

**Status:** accepted · **Date:** 2026-10-24

## Context

Every model call goes to one OpenAI-compatible gateway
([D-2026-09-04-a-gateway-is-the-only-provider](D-2026-09-04-a-gateway-is-the-only-provider.md)).
Pointed straight at Anthropic's OpenAI-compatible endpoint, the live lane gets no prompt caching:
the vendor's documentation says caching "is not supported" there and `usage.prompt_tokens_details`
is "always empty", and two identical 13k-token requests confirmed it (no cached tokens either time).
A ChemClaw turn resends its whole prefix — 128 tool schemas and three system blocks, byte-identical
across turns of one process — on every model call, so an `an-03`-sized turn (5–7 tool calls) bills
about 237,000 uncached input tokens. That made live evaluations (`make model-text-eval`, 500 turns)
too expensive to run.

## Options

1. **Keep the compatible endpoint.** Nothing to own; every token is billed at full input price.
2. **A native Anthropic chat model in `src/`.** Caching through the vendor SDK, but a second
   provider path that D-2026-09-04 exists to forbid, and one more upstream class to adopt whole.
3. **A translation shim of our own** (OpenAI chat completions in, Messages out). No new dependency,
   but streaming, tool calls and usage mapping become code this repository owns.
4. **LiteLLM's proxy on loopback**, configured to inject `cache_control` at the system message and
   at the last message. ChemClaw still dials one OpenAI-compatible URL; the cached-token count comes
   back in `prompt_tokens_details.cached_tokens`, which the ledger already books as
   `cache_read_tokens`.

## Decision

Option 4, for the live lane only (`infra/live/gateway/`). It keeps D-2026-09-04 intact (caching is
the gateway's business), adds no code to `src/`, and was measured on the lane against option 1 with
the same probe and the same judge:

| per turn (`an-03`, 5–7 tool calls) | compatible endpoint | caching gateway, warm |
| --- | ---: | ---: |
| uncached input tokens | ~237,000 | 6 |
| cache read / write tokens | 0 / 0 | 233,002 / ~4,000 |
| cost at the model's list price | $0.50 | $0.08 |

The first turn after a cold start writes the prefix (~120,000 tokens). Verdicts on four runs each
were partial/served/served/served (endpoint) and partial/fabricated/served/served (gateway); the
fabricated verdict was an invented retention shift, a content error the shape gate exists for, not
a translation fault.

## Consequences

- The vendor key lives only in the gateway's environment; ChemClaw authenticates to it with a token
  `up.sh` mints under `.live/gateway/` (0600) — outside the lane's `run/`, so `make live-down`
  leaves the gateway running. `tests/test_live_gateway.py` holds both.
- LiteLLM holds the key, so its version is pinned in `up.sh` and bumped by hand, never by
  Dependabot.
- A deployment's gateway is outside this decision: whether it caches is its own configuration
  (the BACKLOG deferred row on prompt-cache control names it).
- A cache hit needs a byte-stable prefix: anything per-turn placed into the system blocks or the
  tool list silently turns every turn back into a full write.

Revisit when: a release of `langchain_openai` or of the vendor's compatible endpoint carries
`cache_control`, or the gateway's mapping of tool calls or usage diverges from the endpoint's on a
live run.
