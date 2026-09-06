# D-2026-09-05-six-reviews-of-eight-hours-work — what fresh context found in the fleet budget

**Status:** accepted · **Date:** 2026-09-05

## Context

`D-2026-09-05-a-pool-count-is-not-a-connection-count` and the `awaits_answer` gate in
`D-2026-09-05-…` shipped the same day. Six reviewers were then given the merged code, Postgres and
a broker, and told explicitly that the ADRs, docstrings and commit messages were claims to falsify
rather than evidence — this repository's own rule about prose, applied to prose written hours
earlier by the session that wrote the code.

They found six defects in it, two of them in controls, and three false statements in the ADR that
justified it. This is what they found and what it changed. **Two of the defects were in tests I had
written to prove the fixes**, which is the finding worth carrying: a control and its test were
written together, so the same misconception shaped both.

## The gate covered one of two launchers

`awaits_answer` runs a connector job with no wall-clock ceiling, and the operator gate went into
`build_job_tool` on that function's own claim to be "the one function both the runtime and
`make connector-validate` build a job through". It is not. The template workflow's job step resolves
through `authorize_job_step` → `prepare_job_launch` and builds no tool at all; verified against a
live broker, an ungated declaration reached `ConnectorJobInput` and the child started with
`workflow_execution_timeout=0`.

`prepare_job_launch`'s docstring, forty lines above where the gate went, says it is one function
*because* there is more than one launcher, and that the template's `ResolvedJob` had already dropped
`expensive` and `precondition` the same way — D-168's entire subject. **And the suite asserted the
bypass**: `tests/test_template_job_step.py` substituted `awaits_answer=True` onto a bundle nobody
had granted and pinned the unbounded child as correct, while its sibling on the chat path had to
grant first.

`require_funded_ceiling` is that check with one definition, called from the pre-flight. Building a
tool now refuses nothing, which is the second fix rather than a side effect: raising at build time
took `registry.job_tools()` with it, and that rebuilds *every* enabled bundle's launchers — so one
typo in the allowlist refused the whole in-process tool surface on every turn, reaching the chemist
as `bad_tool_arguments` because `runner.py` classifies the `ValueError` family that way. The
allowlist is also stripped now: it grants an entitlement, and this repository's other entitlement
lists strip while the `os.pathsep` family it borrowed its separator from does not.

## Two independent ceilings shared one guard

The session-store ceiling was checked inside `if self.pg_fleet_max_connections:`, and the alert's
second branch inside `max(chemclaw_pg_fleet_max_connections) > 0`. `postgres.maxConnections: 0` is a
documented value meaning "declare no ceiling", so a deployment that declared only the *session*
ceiling had both nets off: measured, a session store charged **500 connections against a declared
180**, constructed without a word and unwatched at runtime. Each now gates on its own ceiling.

Two smaller things in the same expression. `A - B` is a vector join, so `sum(pool_max_size) -
sum(session_pool_max_size)` returns empty when a pod publishes neither gauge — the primary
comparison silent while the fleet is over; both sums take `or vector(0)`. And both alert
descriptions named a knob inert on the branch that fired, `ChemclawPgPoolSaturated` worst of all: its
remediation refuses a split deployment at startup exactly as changing nothing does.

## The refusal's arithmetic did not reach its own number

The message printed the raw settings rather than the terms the figure was built from, so it added up
on one of three paths: a split said 112 beside a decomposition summing to 166, and a hand-set pair
too small to hold its readiness pools said 32 beside one summing to **−13** — three narrow pools out
of two. `_fleet_pool_widths` derives the split through the same branches as
`fleet_connections_per_server`, so the breakdown is that function's own answer by construction, and
a test parses the message and checks it multiplies out.

## Three statements in the previous ADR that are false

It is merged, so this supersedes those statements rather than editing them.

**"Declaring a phantom second ceiling pads the right-hand side and turns the alert off."** Backwards,
and it reached five documents including the runbook line an operator follows during an incident.
Nothing in the tree ever adds the two ceilings — that was a summed expression replaced *within the
same pull request*, and the sentence outlived it. Driven: with the session server over, an
undeclared ceiling is **silent** and a declared one **fires**. Declaring can only add a firing
condition.

**"The expression is checked by `promtool check rules` and exercised by `promtool test rules` …
with a deliberately flipped expectation as a negative control."** No such fixture exists.
`grep -rn "alert_rule_test"` finds nothing, and CI runs `check rules` only — syntax and shape, not
semantics. The sentence tells the next maintainer a semantic guard exists; the defect it fixed had
shipped green under exactly that check. Those runs were a measurement taken while writing, and this
ADR says so instead.

**"No existing release changes behaviour: with no split not a single number moves."** Contradicted by
its own two preceding sentences. The declared floor moves 208 → 166, live backends per front-door
pod 7 → 5, and `chemclaw_pg_pool_max_size` — the alert's own left-hand side — 48 → 33 at the code
default. What is true is narrower: **no existing release breaks**, because every change is toward
holding fewer connections and refusing fewer deployments.

(Also: that ADR's enumeration says 50,320 misses where two source comments say 49,993, from an
unseeded run of the same experiment. Both are ≈25%; neither is reproducible as written, which is the
argument for citing the property rather than the sample.)

## What this does not do

`pg_endpoint` compares DSN strings, and **both** halves now split the fleet that way — the startup
check and the runtime gauge. So one server spelled two ways is charged and alerted as two servers
each inside their ceiling, and the real total is checked by nothing; the released expression *before*
the split gauge existed would have caught that case. That is a `BACKLOG.md` row, written this time,
with the measurement and the trade it turns on: asking the server (`pg_control_system()`, 0.24 ms,
unprivileged) answers it exactly and costs the alert its series during a database outage.

## The pattern worth keeping

Two of the six defects were in tests written to prove the fixes. The template-path test asserted the
bypass; the alert test searched the whole template file for PromQL fragments and began passing
against the alert's own `description` once that description quoted them — measured, the expression
gained `or vector(0)` twice and not one assertion moved. Both were written by the session that wrote
the code, in the same hour, from the same misconception. A test written beside its fix inherits the
fix's blind spot, and the only thing that found either was mutating the source and watching what
stayed green.
