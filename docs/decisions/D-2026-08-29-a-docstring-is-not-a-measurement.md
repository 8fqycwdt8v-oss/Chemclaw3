# D-2026-08-29-a-docstring-is-not-a-measurement — five independent reviews of the SDK-audit change, seven defects, and what they had in common

**Status:** accepted · **Date:** 2026-08-29 · Follows `D-2026-08-29-an-iteration-cap-is-not-a-cost-cap`

## Context

`D-2026-08-29-an-iteration-cap-is-not-a-cost-cap` shipped three guards — a per-turn spend cap,
session forking, and a per-profile reasoning-effort knob — with 24 tests, `make lint type test`
green, and CI green. It was then reviewed by five independent readers, each given one area, no
access to the author's reasoning, and one instruction that turned out to be the whole of it:
**run things rather than read the docstrings.**

They found seven defects. Every one is in code that shipped green, and five of the seven are places
where a first-party docstring asserted precisely the property that measurement contradicted. That
pattern is the subject of this ADR; the individual fixes are the smaller half.

## What was wrong

**The fork inherited its parent's age.** `durable/retention.py` expires a thread on
`max((checkpoint->>'ts')::timestamptz)` and a copied checkpoint carries the parent's `ts`, so a fork
of a conversation last touched a year ago was past the window the instant it existed. Measured: the
next sweep deleted the fork's entire thread while `session_owners` and `session_messages` survived —
a session that lists, opens, renders twenty turns of transcript, and then takes its next turn with
**no history at all**, because context comes from the checkpointer rather than from the rows the
chemist can see. `_RESTAMP_NEWEST` starts the fork's clock at the fork.

The docstring made this worse than a plain oversight. Copy-rather-than-pointer was argued *for* on
the grounds that a pointer would make the child "lose its own past when the parent aged out" — and
the copy lost it at the same instant by a different route. The argument was right about the risk and
wrong that copying alone addressed it.

**The fork's ownership row was written after its transaction.** `agent/leaver.py` scopes erasure
through `SELECT session_id FROM session_owners WHERE owner = ANY(...)`, so rows that landed without
an ownership row are **structurally unreachable** by the one sweep that must never miss anything.
Measured: a chemist's transcript survived their own erasure while the report said it was complete.
The comment defending the ordering reasoned carefully about retention's orphan anti-join and never
considered erasure. Both writes are now in one transaction, which removes the ordering question
rather than answering it.

**The fork did not copy `tool_result_links`.** The transcript carries `result_ref` handles that
`api/tool_results.py` resolves through that table joined on `session_id`, so every stored result in
a fork collapsed to its 400-character preview — the regression `D-2026-08-09-a-preview-is-not-a-result`
exists to prevent. Second-order: with no link from the fork, deleting the *parent* destroyed blobs
the fork's own transcript still pointed at. The links are copied; the blobs are shared, which is
what a content-addressed store is for.

**The spend cap under-counted two whole classes of provider call.** A repaired tool call is two
calls — `model_calls.RepairInvalidToolCalls` re-invokes the handler and returns only the repair —
and `MeterTurnSpend` wraps it from outside, so it booked one bill for two: measured 700 against
1,200 spent, on precisely the malformed-argument loop the guard exists to stop. And a model call
inside a *tool body* is not a graph node at all: `agent/condense.py` makes one per protocol, up to
24, and measured 5,200 tokens against a 150-token budget with the cap never firing. Both ride the
message stream the runner meters, so `enforce_spend_cap` now takes the larger of its channel and
`turn_usage.metered_turn_tokens()`, and degrades to exactly the old behaviour off the request path.

**`reasoning_effort` does not mean the same thing on both providers.** The original ADR states that
"both installed clients take `reasoning_effort`, so no translation exists to write", on the strength
of the attribute round-tripping on the constructed client. Measured through `_get_request_payload`
instead:

    reasoning_effort="high"  ->  output_config={'effort': 'high'}
                                 thinking={'type': 'adaptive', 'display': 'summarized'}

So on Anthropic the knob silently enables extended thinking — which cannot be combined with a set
`temperature` (a 400, and `_failover_exceptions` deliberately does not fail 400s over, so *every*
turn fails), draws its tokens from `llm_max_tokens`, and sends `output_config` to models with no
effort levels. `LlmSettings._effort_is_provider_scoped` refuses the combination at startup rather
than translating it or ignoring it: translating means inventing a thinking budget nobody asked for,
and ignoring means a profile saying `effort: high` quietly getting default effort, which is a
control that reads as one and is not.

## The common cause, which is the reason this is an ADR

Four of the seven were found by a single technique: **ask the object what it will actually send, not
what it was constructed with.** One `_get_request_payload` call, one retention sweep, one erasure
run, one `tool_result_links` query. None took more than a few minutes, and each disproved a
paragraph of confident prose written by someone who had not run it.

The shipped test suite could not have found them, and a mutation audit says why. Of fifteen
feature-level mutations, eleven went red and six survived — including `_spend_cap_event` neutered
entirely (229 tests green across every file mentioning spend), the fork route rewritten to assign
ownership to the wrong principal and drop the parent's profile (64 tests green), and the `>=`
boundary flipped. The pattern in the survivors is uniform: **the tests covered the mechanisms and
not the seams.** `test_spend_cap.py` drove a compiled graph and never went through `run_turn`;
`test_session_fork.py` drove `fork_session` and never went through the route.

Two shipped docstrings were also false in a way no test could catch, because they described a
counterfactual: `MeterTurnSpend.state_schema` was called the thing that makes the channel exist
(`create_agent(state_schema=ChemclawState)` already does, measured — removing the attribute changes
nothing), and a fixture's rationale claimed to distinguish two branches that `cost // 2 + (cost -
cost // 2) == cost` makes identical. Both have been rewritten to say what they actually establish.

## Decision

1. Fix the seven defects, each with a regression test that fails against the previous commit —
   verified by reverting the fix and watching precisely those tests go red, not by inspection.
2. Cover the seams the mutation audit named: a front-door test through `run_turn` for the spend
   cap (mutations R and S), the fork route's success path and its 409 (T and U), and the exact-on-
   budget boundary (C). Each was written *against* its mutation and confirmed to catch it.
3. Correct every docstring the review falsified, in place, saying what was believed and what
   measurement showed — the ADR record and the code comment disagreeing is how the next reader
   inherits the mistake.

## The fix for the effort defect was itself incomplete, which is the same lesson again

The first version of that fix put the guard in `LlmSettings._effort_is_provider_scoped` — a
`@model_validator` refusing `llm_effort` on the Anthropic path. That covers the *deployment* knob
and nothing else. `AgentProfile.effort` is a second input: a YAML field resolved per agent, reaching
`build_chat_model` as an argument without passing through any settings validator.

Measured against the shipped code default (`llm_provider="anthropic"`, `llm_effort=None`, so the
validator was satisfied), a profile carrying `effort: high` built a `ChatAnthropic` whose payload
was `output_config={'effort': 'high'}` plus `thinking={'type': 'adaptive'}` — precisely what the
validator exists to prevent, reached through the other door.

So the gate moved to `build_chat_model`, the one place where the deployment setting and the profile
override are resolved into a single answer and every client below is built from it. The settings
validator stays, because failing at startup is better than failing at the first turn for the input
that *can* be checked at startup; it is now the early warning rather than the control.

The general form, and the reason this paragraph exists rather than a quiet second commit: **a guard
placed on one of several inputs is not a guard on the invariant.** Ask what the invariant is —
here, "effort never reaches an Anthropic client" — and put the check where every path converges. The
first version answered a narrower question ("is this setting valid?") and looked complete because
the setting was the input the author had been thinking about.

## Consequences

- `llm_effort` is refused on the Anthropic path. That narrows a knob the previous ADR advertised as
  provider-agnostic, and the narrowing is the honest version: it never was.
  `test_the_anthropic_payload_is_why_that_refusal_exists` pins upstream's behaviour, so if
  `langchain-anthropic` stops injecting thinking the refusal can be revisited from evidence.
- A fork now costs one extra row per stored tool result and one `UPDATE` on one checkpoint.
- The spend cap reads two sources. It is still a floor rather than an exact figure — the stream
  accumulates as chunks arrive — which is consistent with a guard already documented as one call
  loose, and is why `max` is right where a sum would double-count.
- **The general rule, which is what should outlive these seven fixes:** a docstring asserting a
  property is a claim about what its author believed. Where the property is checkable in minutes —
  a payload, a sweep, a query — the claim belongs in a test or beside the number that produced it.
  This repository already says that ("Measure it, don't argue it"); what this review adds is that
  the rule binds hardest on the *interfaces between your code and somebody else's*, because that is
  where an attribute you set and a field they send diverge without telling you.
