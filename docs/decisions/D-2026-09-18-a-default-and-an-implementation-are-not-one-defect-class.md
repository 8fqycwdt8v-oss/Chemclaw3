# D-2026-09-18-a-default-and-an-implementation-are-not-one-defect-class — the readiness record's two stale rows were two kinds of staleness

**Status:** accepted · **Date:** 2026-09-18 · Corrects the generalisation in
`D-2026-09-18-a-risk-the-code-has-closed-is-not-a-risk-anybody-accepted`'s "Why nothing caught the
two". That ADR is merged and is not edited; its decisions all stand, and so does the guard it built.
Supersedes nothing.

## Context

`D-2026-09-18-a-risk-the-code-has-closed-is-not-a-risk-anybody-accepted` found two §4 rows of the
readiness record stale in the flattering direction, amended both, and built
`test_a_claim_about_a_shipped_default_agrees_with_the_setting` so it could not happen again. The
finding was right and the guard drives. Its explanation of *why nothing caught the two* is not:

> What moved was its **default** — a number in the record's prose, about a value the config holds,
> that no assertion read.

That is true of the spend row and false of the other one, and the commit message repeated it.

| §4 row | What actually moved |
|---|---|
| "`agent_max_turn_billed_tokens` ships at **0**, which is off" | a **default**. `src/chemclaw/core/config/agent.py` reads `Field(default=300_000, ge=0)`; the symbol, its type and every caller are unchanged |
| "`note_file_fingerprints` is `mtime_ns:size`" | an **implementation**. `src/chemclaw/kg/graph.py::note_file_fingerprints` returns `sha256:<hex>` over the file's bytes since `D-2026-09-16-a-fingerprint-that-names-a-checkout-is-not-a-fingerprint-of-a-note`. No setting exists, no number exists, and nothing in any config holds the old value |

The two are not one class, and the difference decides what a guard can do. A default is a value
some object holds at runtime, so a claim about it can be *resolved* — parse the name, read the
attribute, compare. A claim about an implementation is prose about what a function does, and
comparing prose to a function body is the half the record's own preamble already concedes no test
performs.

## Decision

### 1. The new guard covers the first class, and the record says which

`test_a_claim_about_a_shipped_default_agrees_with_the_setting` resolves a named setting against
`chemclaw.core.config.settings`. It would not have caught the fingerprint row at any strength: there
is no attribute to read. Saying it closes "the reason nothing caught the two" overstates it by half,
which matters because an overstated guard is read as covering the case it does not — the same
failure in kind as a record naming a control that is not there, which is the thing the readiness
record exists to avoid.

### 2. What the behaviour class would need, and it is not free

A §4 row describing a **behaviour** is holdable, but only by making the claim and the check the same
object rather than two descriptions of one thing. The mechanism already exists in this file family:
§4 rows carry a citation column, and
`test_every_test_the_readiness_record_names_exists` resolves a `file::test_name` citation against the
tree. So a behaviour row is held when it cites, by `file::test_name`, **a test that pins the
behaviour the row describes** — because closing the gap means deleting or rewriting that test, and
deleting it reds the record.

Neither row did. The fingerprint row cited nothing executable, and the amended replica row cites
`BACKLOG.md` and a live Temporal edge. The cost is real and is why this is a rule rather than a
sweep: it asks somebody to write a test asserting the *current, unsatisfactory* behaviour, which is
a test whose only purpose is to fail when the behaviour improves. That is worth it for a row a
deployment team would act on and not worth it for every row, so it is a judgement at the point of
writing the row, not a gate.

What is **not** closable this way, and is stated so nobody looks for it later: a row whose cited
test is edited rather than deleted when the behaviour changes. The citation still resolves. That is
the residue the record's preamble already names — whether the clause beside a citation is a fair
description of what that test proves is a review matter.

### 3. Two counts in that ADR were claims about a commit

Its §2 says the guard "parses the three shapes the record uses". `_SHIPPED_DEFAULT.finditer` returns
**two** matches over the record at HEAD: the "ships at N" arm has no live instance, because it is the
shape of the row that same commit moved to §2. The count is not corrected to two here — the
population of claims is now derived by `_SETTING_SUBJECT` from the record itself, and the `#:`
comment and docstring that stated it say what the shapes are without counting the rows that use
them. This is `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` happening inside the commit
that was enforcing it over a tool surface, which is the third time this tree has recorded that
pattern and is why the remedy is deletion rather than a corrected figure.

## What this does not decide

Whether §4's remaining rows are the right things to accept, which is the deployment team's judgement
and is what §5 of the record says. And it does not require anything of the rows that exist today: it
states what a *future* behaviour row owes if it is to be held.

## What keeps it true

- `tests/test_readiness_record.py::test_a_claim_about_a_shipped_default_agrees_with_the_setting` —
  the first class, and the subject set it now derives rather than counts.
- `tests/test_readiness_record.py::test_every_test_the_readiness_record_names_exists` — the
  resolver §2 above depends on: a behaviour row's `file::test_name` citation reds when that test
  goes.
- `tests/test_decision_log.py` — this ADR's id, filename, heading and ledger row.
