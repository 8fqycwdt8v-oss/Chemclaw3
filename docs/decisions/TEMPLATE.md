# D-YYYY-MM-DD-<slug> — <the decision, as a short sentence>

**Status:** proposed | accepted · **Date:** YYYY-MM-DD

<!--
Copy to `D-YYYY-MM-DD-<slug>.md` (today's date; the whole stem is the id), replace the heading's
placeholder with that stem, and add a row to `README.md` in record order. If it changes what is in
force, update `CURRENT.md`.

Write an ADR only for a choice between options or a decline. A defect fix is a commit and a test.
`tests/test_decision_log.py` requires the `## Options` section, and a `Revisit when:` line on any
ADR that declines something.
-->

## Context

What forces the choice now: the problem, the constraints, and what is measured.

## Options

1. **<option>** — what it is, what it costs, what it buys.
2. **<option>** — …

## Decision

The option taken, and why it beats the others.

## Consequences

What changes, what becomes harder, and which test or setting holds the decision.

Revisit when: <the condition that would reopen this, ideally one a test or a file can show>.
