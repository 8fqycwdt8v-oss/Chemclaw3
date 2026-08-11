# D-2026-08-11-what-the-removal-found — Deleting the framework is what exposed the readers that only knew one shape

**Status:** accepted · **Date:** 2026-08-11

Completes M13 of [`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md).
That ADR decided the rebuild; this one records what taking the dependency out actually turned up,
because two of the four findings are defects rather than tidying and one of them reverses a premise
another module still argued from.

## Context

M0–M12 built the LangGraph layer 1 and ran both engines against one event contract. M13 is the
removal: `agent-framework-anthropic`, `agent-framework-core` and `agent-framework-openai` out of
`pyproject.toml`, the `maf` stack out of `tests/test_third_party_layering.py`, and the documents
brought into line.

The removal was expected to be mechanical. It was not, and the reason is worth naming: while both
engines were live, a module could read the *old* shape and be right about half the rows. With one
engine left, "half the rows" became "every row a live session writes", and three of the four
findings below are that same sentence in three places.

## Decisions

### 1. `chemclaw.cli.explain` read one stored shape, and the fix is that only one function may know them

`session_messages` holds two serializations — the ones the previous framework wrote and the ones
LangChain writes — because M6's conversion pass is resumable and a rollout is not atomic. That is
the whole reason `message_shape` exists.

`explain` parsed the payload itself, matching on `contents`. Measured: a row written by any turn
since M6 renders as role `unknown` with empty text, so the audit reconstruction — the tool that
answers "why was this run?" for a GxP auditor — showed an **empty conversation** for exactly the
sessions still in use. It did not fail; it printed nothing and looked like a quiet session.

The fix is not "teach `explain` the second shape". `session_store.message_from_row` already knew
both, and a second reader is how a table with two shapes acquires a reader that knows one. It is
public now, `explain` calls it, and `tests/test_explain.py` asserts both shapes from both sides —
reading only the *new* one would be the same bug pointed at the archive.

**The general rule this states:** a column holding more than one serialization gets exactly one
function that decides which it is. Anything else asks that function.

### 2. Two dead paths were deleted rather than left as "the other engine's half"

`connectors.registry.open_reachable` (opening process-lived connector tool objects) and
`api.runner_usage.usage_tokens` (reading the framework's `UsageDetails` content) had no production
caller once the branch went. Both survived the earlier phases because they read as one half of a
pair. They are deleted, with `usage_tokens`'s two tests re-pointed at `graph_usage_tokens` on real
chunk shapes — the assertions were about the arithmetic, which is unchanged, not about the reader.

Same argument retires the twin-distinguishing names. Eight `lg_`-prefixed middlewares and
`make_langgraph_audit_middleware` were named to sit beside a twin that no longer exists, and the
prose throughout the tree already cited the *unprefixed* names — so the rename makes ~30 docstring
references true again rather than merely shorter.

### 3. Per-model token attribution is genuinely lost, and the counter label still does not come back

`core/metrics.py` justified emitting only a `profile` label by pointing at the framework's
`gen_ai.client.token.usage` histogram, which carried request model, response model, provider and
token type. That instrumentation went with the dependency and the LangChain stack ships no
equivalent (measured, and already recorded in `core/logging.py`, `deploy/README.md` and the
runbook) — so the comment argued from a premise that no longer holds.

The premise is corrected and the decision is **unchanged**: the axis is not added as a counter
label. `turn_costs` carries model attribution per turn, which
[`D-2026-08-01-spend-is-a-ledger-not-a-label`](D-2026-08-01-spend-is-a-ledger-not-a-label.md)
decided is where it belongs; a lossier second answer as a label would be exactly the two systems to
reconcile the original comment warned about. What changes is that the sentence now says the axis is
absent instead of claiming it is emitted elsewhere.

### 4. Prose is a claim about the tree, so a dead framework's name in the present tense is a defect

~180 `MAF` mentions remained in `src/`. Roughly half were load-bearing history — "this is shaped
this way because that framework did X" — and stay, because deleting the reason leaves an unexplained
shape. The rest asserted the present tense about a dependency that is gone: a module docstring
describing `build_agent` and a `SkillsProvider`, a config section titled "The MAF conversational
agent", an audit middleware described as an adapter to a second implementation that does not exist.

Those are rewritten. The line is: **past tense about the framework is evidence; present tense about
it is false.** `docs/reference/architektur.md`, `docs/planning/implementation-plan.md` and
`implementation-tickets.md` keep theirs — each already opens by saying it is a historical document —
and `BACKLOG.md`'s mentions are all inside closed rows, which are a log and not a claim about now.

## Consequences

- `explain` reconstructs current sessions. The regression test fails if either shape is dropped.
- `message_from_row` is public API of `agent/session_store.py` and is the only shape reader.
- Nothing in `src/` or `tests/` imports `agent_framework`; the suite runs green with the three
  distributions uninstalled, which is how this was verified rather than by grepping.
- `tests/test_third_party_layering.py` has no `maf` stack and no `maf` allow-list rows. Its
  both-directions pinning would now fail on one, which is the property that keeps this true.

## What this does not close

The three M12 probes still need a live credential and a real tenant: the D-123 concurrency
comparison, the plan→approve→execute round trip, and team routing accuracy. They gate no code in
this change — the team ships off by default for exactly that reason — but nothing here should be
read as having measured them.
