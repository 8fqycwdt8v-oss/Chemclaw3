# D-2026-09-18-a-non-zero-default-is-the-durable-form-of-that-claim — the ADR about stale numbers shipped one

**Status:** accepted · **Date:** 2026-09-18 · Corrects one figure in
`D-2026-09-18-a-default-and-an-implementation-are-not-one-defect-class`, whose decisions all stand,
and one clause of its ledger row. Neither merged record is edited. Supersedes nothing.

## Context

`D-2026-09-18-a-default-and-an-implementation-are-not-one-defect-class` divides two kinds of
staleness — a moved *default*, which a guard can resolve, and a changed *implementation*, which it
cannot — and its §3 is titled "Two counts in that ADR were claims about a commit". Its own table
says, present tense:

> a **default**. `src/chemclaw/core/config/agent.py` reads `Field(default=300_000, ge=0)`; the
> symbol, its type and every caller are unchanged

The tree reads `Field(default=3_000_000, ge=0)`, and has since `44e7c0e1` — the **immediate parent**
of the commit that merged that ADR. So the figure was already falsified by the commit the ADR was
written on top of: a 10× error, transcribed out of the older merged ADR rather than measured. It has
since moved again for a reason: `D-2026-09-18-a-cap-below-an-ordinary-turn-is-a-guard-that-kills-another`
found 300,000 funded three model calls against an iteration cap of 25, and derived the cap as
`harness_max_loop_iterations × agent_context_token_budget` instead.

The same figure is live one document over, at `docs/decisions/README.md`'s row for
`D-2026-09-18-a-risk-the-code-has-closed-is-not-a-risk-anybody-accepted`:

> `agent_max_turn_billed_tokens` ships at 300,000 rather than 0

That is `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` happening inside the ADR whose §3
is about exactly that, and then again in the index a reader consults *as current*.

## Decision

### 1. The correction is stated, and it is not a corrected digit

Writing `3_000_000` into a follow-up record is the same defect one commit later, and the same
argument that closed §3 applies unchanged. The durable form of the claim is:

> `agent_max_turn_billed_tokens` carries a **non-zero** default, and what holds it is
> `tests/test_spend_cap.py::test_the_turn_cap_stays_above_what_the_other_two_guards_authorise`.

That test is stronger than any figure a record could carry: it asserts the cap sits at or above
`harness_max_loop_iterations × agent_context_token_budget`, so it fails both if the cap goes back to
0 and if it drops below a turn the other two guards already authorise. A number in a record cannot
notice either.

### 2. The ledger row is corrected in place, because an index is not a merged record

`docs/decisions/README.md` is the navigational index, written in the present tense and read as
current. The mitigating reading — that its row records what the prior ADR found when it was
written — is available and is the `_HISTORICAL` argument `tests/test_repo_map.py` applies to
`docs/decisions/`; it is not available for the *index*, which is the one document in that directory
whose job is to say what is true now. The clause becomes "ships with a non-zero default rather than
0", naming no figure, which is the remedy this repository's rules ask for everywhere else.

### 3. Why this is a new file rather than an edit

CLAUDE.md: a merged ADR is never edited; a decision that has changed gets a new ADR. Both merged
ADRs above keep every word. The index row is not a merged record and is corrected in place.

## What this does not decide

What the cap should be. That is
`D-2026-09-18-a-cap-below-an-ordinary-turn-is-a-guard-that-kills-another`'s, and it stands.

## What keeps it true

- `tests/test_spend_cap.py::test_the_turn_cap_stays_above_what_the_other_two_guards_authorise` —
  the non-zero default, as a relation rather than a figure.
- `tests/test_spend_cap.py::test_the_turn_cap_funds_more_calls_than_the_loop_cap_permits` — the
  same guarantee in calls, which is the unit it failed in.
- `tests/test_readiness_record.py::test_a_claim_about_a_shipped_default_agrees_with_the_setting` —
  the readiness record's own claim about this setting, resolved against `settings`.
- `tests/test_decision_log.py` — this ADR's id, filename, heading and ledger row.
