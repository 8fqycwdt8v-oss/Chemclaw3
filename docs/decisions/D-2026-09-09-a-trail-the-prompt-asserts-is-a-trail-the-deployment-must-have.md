# D-2026-09-09-a-trail-the-prompt-asserts-is-a-trail-the-deployment-must-have — A trail the prompt asserts is a trail the deployment must have

**Status:** accepted · **Date:** 2026-09-09 · Revisits `D-122`, whose polarity argument stands.

## Context

`_INSTRUCTIONS` said, unconditionally and in the present tense: *"Traceability: every tool call is
recorded in an append-only audit trail — actor, tool, arguments, outcome, latency, correlation id
and deployment revision."*

D-122's **stated** condition for the log-only fallback is *"where no database is configured"*. Its
**implemented** condition is `session_store != "postgres"`. Those are different predicates, and the
gap is not academic: `postgres_dsn` has a *default value* pointing at the `make up` database, and
`.env.example` ships `CHEMCLAW_SESSION_STORE=memory`. So on the configuration `CLAUDE.md` instructs
a developer to stand up, a database is configured, migrated and reachable, `audit_events` exists,
and every row is discarded.

Measured — one completed turn that called a tool:

```
audit_events                       0
session_messages                   0
chemclaw_audit_sink_failures_total 0
explain <sid>  ->  "no messages, tool calls or jobs recorded for this session"
```

Flipping only `CHEMCLAW_SESSION_STORE=postgres`: 1, 4, and a full reconstruction. The chart sets
`postgres`, so a chart release is safe and the dev/bare front door is not.

Two more claims in the same prompt were false on any narrowed connector set. Measured off the wire
against a compiled graph with a capturing model, the prompt named **16 tools nothing binds** on a
deployment with no bundle — among them `screen_hazards`, in the paragraph that says *"before you
propose a synthesis, a reagent, or a set of conditions, call `screen_hazards` on the species
involved and report every flag it returns."* And upstream's empty-skills listing told the model
*"You can create skills in …"*, which `skill_backend.SkillsReadOnlyRefusal` refuses on every verb.

## Decision

**The prompt is assembled from blocks that declare the tools they name**, and a block whose tools
the graph did not bind is not sent. Measured: 16 unbound names → 0 at the cold config (prompt 14,982
→ 12,738 chars), 10 → 0 with bundles declared but unreachable, 2 → 0 with bundles bound.
`prose-validate` gains a rule checking that each block requires exactly the tools it names — driven
to a red, and it caught three real declaration errors before any test existed.

**The traceability paragraph is one of two alternatives, selected from the sink object the graph was
built with** — the same object the middleware writes through — so the prompt and the writer cannot
disagree. Under `NullAuditSink` the claim is retracted rather than merely dropped.

**The resolution is announced**: a WARNING at front-door startup naming the setting that would fix
it, beside an INFO inventory line stating what this deployment is configured with and what it holds.

**The condition stays `session_store`**, deliberately. It remains the deployment's *statement* that
durable records belong in Postgres; inferring durability from a DSN that always has a value would
make the sink switch on a default nobody set. For the same reason the DSN is not in the warning's
condition — it is always configured, so a DSN-qualified warning is a warning that always fires,
dressed as a narrower one.

D-122's polarity argument is untouched and is not reopened: a forgotten keyword must not downgrade
the record. What is added is that a **silent** downgrade is the same defect one level up — the
deployment forgot, not the call site, and nothing told it.

## The regression this found, which is the reason the change is worth its risk

Splitting the prompt into blocks exposed that the **envelope rule shared a paragraph with
`record_knowledge_note`**. `agent/subagents.py` subtracts every side-effecting tool from the
helper's surface, so the compiled helper graph was sent **no envelope rule at all** — half the
injection defence — while `tests/test_framing.py` stayed green because it reads the maximal text.
The block is split, and a test now asserts the three safety-floor sentences survive narrowing to an
empty surface, failing if the envelope rule is put back behind a tool.

## What the ratchet says, and what was deliberately not done

`tests/test_context_floor.py` now charges the *maximal* instructions and carries the difference as a
named line, because the fixture binds no `SERVED_ELSEWHERE` bundle: charging what it observes would
be the "ratchet measures a smaller system" defect a fourth time. That line is 206 tokens.

**The ceiling was not raised.** `PREFIX_BOUND = CEILINGS["__default__"] + SERVED_ELSEWHERE_ALLOWANCE`,
and `tests/test_compaction.py` holds `agent_tool_result_clear_trigger` and
`agent_context_token_budget` at exactly their claimed allowances above it — no headroom on either,
and the budget is derived *downwards* from the 128k window so it cannot absorb a rise. The designed
answer to a prefix this large is to narrow it, which is what this change does: the text actually
sent is **177 tokens smaller**.

## Left open

The **skills listing** over-promises the same way `_INSTRUCTIONS` did. Measured on a deployment with
bundles declared and servers unreachable, the system message still names `screen_hazards`,
`ich_impurity_limit` and `screen_genotoxic_alerts`, because the skills backend narrows by what the
manifests *advertise* rather than by what a turn *binds*. Closing it changes `skill_permits`'
basis. What this ADR fixed is `instructions_for`, not the whole system message.
