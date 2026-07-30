# D-152 — Metrics carry labels, caching is measured rather than built, and the CLI meets the harness

**Status:** accepted · **Context:** the agentic-system review's last three open items — REV-10's
attribution half, REV-9's prompt caching, and the live-harness smoke test that closed the review.
They are one ADR because the smoke test is what turned the other two from readings into
measurements, and because it found a defect of its own.

## 1. REV-10 — the registry gains labels, and half the item was already solved

`chemclaw_tokens_total` and its four D-144 siblings answer "what is this costing". They could not
answer "costing *on what*", because the registry had no label support at all: every store was a
`dict[str, float]` keyed on the bare metric name, so `chemclaw_tokens_total{profile="…"}` was not
expressible.

### Per-model attribution is not built here, on purpose

MAF already emits `gen_ai.client.token.usage` labelled by request model, response model, provider
and token type, and the shipped Helm chart turns OTel on. Adding a `model` label to this registry
would mean two systems publishing the same number under different names, to be reconciled by
whoever reads a dashboard. So the model axis stays where it already is.

Two gaps recorded rather than closed, because both are upstream:

- MAF records only the `input` and `output` token types, so D-144's cache-read and cache-write
  dimensions are **not** in that histogram. `/metrics` remains the only place they appear.
- OTel has never heard of a Chemclaw **profile**. That is the gap worth filling locally, and it is
  what shipped.

### Labels are declared, not free-form

`_COUNTER_LABELS` names each labelled counter's permitted label names; an undeclared label raises a
`KeyError` exactly as an undeclared *metric* already does. The reason is not tidiness: a label
typo's failure mode is not a crash but a **second, silent time series** that no dashboard queries
and nobody notices, which is worse than the metric being absent. A counter missing from the map is
unlabelled and behaves exactly as before — pre-seeded to zero, rendered as one bare line.

Labelled series are **not** pre-seeded; a series appears on first observation. That is both
Prometheus convention and this repo's own REV-19 rule against publishing a fabricated zero.

### The series cap

`_MAX_SERIES_PER_COUNTER = 64`, past which a new label set is refused with a once-per-metric
warning. A label *value* is not bounded by this module — today it comes from configuration, but a
future label could come from a provider response — and an unbounded map keyed on it is the same
slow leak this codebase has already fixed three times (the budget tracker's per-user counters, the
front door's live sessions, the note index). Refusing loudly once beats being accepted quietly
until the pod runs out of memory. 64 is generous: `profile` is a handful of names, so reaching the
cap means something is wrong, which is exactly what the warning says.

### The unauthenticated-`/metrics` argument

`test_metrics_carry_no_identifiers_or_turn_content` asserted that `le` was the only label anywhere
in the exposition, and its reason was correct: `/metrics` is unauthenticated, so anything that
reaches it is public. That assertion becomes an **allowlist of declared label names**. The
substance of the guard is unchanged — the question is still "can a session id, an actor, or turn
content reach this surface", and the answer is still no, because a profile name is configuration:
low-cardinality, operator-chosen, and not derived from a user or their input.

## 2. REV-9 — the decision is to measure, not to build

The backlog entry proposed marking the fixed prompt prefix cacheable. Verifying it corrected the
entry twice, in the same direction both times: the saving is real but not reachable from here.

- **The ~14.6 k prefix was measured on the wrong provider.** That number came from the Anthropic
  dev path. Production is `openai_compatible`, where `agent_framework_openai` contains **zero**
  occurrences of `cache_control`. The mechanism is not reachable from production at all.
- **"The ~3.5 k system half is cacheable" is false through `Agent`.** `SkillsProvider` merges the
  skills manifest into the instructions with an f-string, which would `repr()` a structured block
  list into a string.

Both halves are changes in `agent_framework`, not in Chemclaw. Building a Chemclaw-side mechanism
on top of that would be building the half that does not carry the saving.

**So: no mechanism ships, and a measurement does.** `chemclaw_cache_read_tokens_total` already
answers the question that decides whether there is anything to build — MAF's `OpenAIChatClient`
maps the provider's `cached_tokens` onto it, so a provider that caches the prefix without being
asked is already visible. `docs/guides/runbook.md` §(viii) states the question, the metric, and what
each outcome implies; the largest possible outcome is "the saving is already banked, build
nothing".

`chemclaw_cache_write_tokens_total`'s HELP text now says it is **structurally 0 on
`openai_compatible`**, which reports cache reads and has no cache-write concept. A permanent honest
zero that looks like a broken counter is a worse operational surface than no counter, and the fix is
one sentence of HELP text rather than suppressing the series.

## 3. The live harness smoke test — and the defect it found

`CHEMCLAW_HARNESS_ENABLED=true` ships in the Helm chart while the code default and every test run
`false`. So the production agent-construction path had never met a live model — the exact shape of
gap that produced all three of the review's original Criticals. The CLI is the seam:
`uv run chemclaw --admin -m "…"` builds the same `build_agent` the front door does.

**The first turn under the shipped configuration crashed before reaching the model**:

```
RuntimeError: ToolApprovalMiddleware requires an AgentSession.
```

`cli/chat.py::converse` called `agent.run(prompt, tools=…)` with no session, relying on the agent's
implicit thread — its docstring said so explicitly, "no session plumbing needed here". True without
the harness; false with it, because the middleware stack `harness_enabled` installs requires a
session. The front door always passed `session=session` and therefore never met this. **Under the
configuration the shipped chart sets, `make chat` and `uv run chemclaw` could not take a single
turn.**

The fix is what the front door already does: `_run` creates one `AgentSession` for the CLI run and
threads it through `converse` and `_repl`. A CLI run *is* one conversation, so this is also the
plainer expression of the intent the old docstring was working around. The session id is the fixed
name `cli` rather than a fresh uuid, so under `session_store=postgres` a terminal session resumes
across invocations — and the CLI is single-user admin by construction (`resolve_identity`), so
there is nobody to collide with.

With that fixed, the smoke test passed end to end against a live model with the real connector
fleet: 27 skills discovered and `ionization-and-partitioning` selected, `resolve_compound` then
`predict_pka` over the calc connector's MCP transport, the result served through the Postgres
calculation cache, and every call in the audit trail under one correlation id and the admin actor.
The answer carried the calculator's stated ±1.6 pKa uncertainty and volunteered that the value sits
above the literature figure — the plan-only autonomy behaved as designed, and an earlier run with
the connectors down had the model refuse to substitute a different tool and ask instead.

**What this does not prove:** one turn on one provider. It closes "the harness path has never been
executed", not "the harness path is tested". A recurring live check is a separate question from
this ADR — CI has no credential, and inventing one is a decision about secrets, not about metrics.

## 4. Two corrections owed from the review's earlier batches

Recorded here rather than by editing the ADRs that carry them, because a merged ADR is not edited.

- **D-143 cites `_SELECT`** as "the read that has no `LIMIT`". `_SELECT` was **dead code** — the
  statement the read path actually runs is `_SELECT_WITH_ID`, which is what the accompanying test
  pins. The finding is correct; the identifier in the prose was not. The dead constant is deleted
  here so nothing can cite it again.
- **The review report's §4** still says nothing scrapes `/metrics` and names two counters as never
  incremented. Both were fixed by D-139 and D-143; the report predates them and is corrected in
  place, since it is a report rather than a decision.
