# D-2026-09-18-a-risk-the-code-has-closed-is-not-a-risk-anybody-accepted — the readiness record's §4, re-read against HEAD

**Status:** accepted · **Date:** 2026-09-18 · Amends
`D-2026-09-14-what-a-deployment-team-is-getting` in place, on the grounds
`D-2026-09-14-tools-were-never-the-variable` already argued for its §3: that document is a **state
record**, so a clause of it describing a state the tree has left is not a decision to supersede,
it is a description that has stopped being one. Supersedes nothing.

## Context

`D-2026-09-14-what-a-deployment-team-is-getting` §4 lists fourteen accepted risks — the section its
own preamble calls "the one that decides it". It was written on 2026-09-14. Sixty-six commits
merged in the four days after.

Re-read against HEAD, twelve of the fourteen are unchanged and two are not, and **both moved in the
direction that flatters least while reading as honest**: another session *closed* the gap, and the
record went on accepting it.

| §4 row | At HEAD |
|---|---|
| "A turn's spend is unbounded in the shipped configuration … `agent_max_turn_billed_tokens` ships at **0**, which is off" | `src/chemclaw/core/config/agent.py:639` reads `Field(default=300_000, ge=0)`. Turned on by `D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` (`be9f8a4d`), whose own table records the argument it had to answer |
| "Two background-worker replicas re-embed the whole corpus … `note_file_fingerprints` is `mtime_ns:size`" | `kg/graph.py::note_file_fingerprints` returns `sha256:<hex>` over the file's bytes, since `D-2026-09-16-a-fingerprint-that-names-a-checkout-is-not-a-fingerprint-of-a-note` — driven there over two real clones of one commit, 40 of 40 re-embedded per pass before and 0 after. `BACKLOG.md`'s own row was rewritten in that commit and says so; the readiness record was not |

The other twelve were re-derived rather than assumed and each still holds: every `retention_*_days`
is still 0; `tests/test_entra_end_to_end.py` still cannot drive MSAL; the five `DEFERRED.md` rows
§4 points at (`push-to-registry`, the two missing charts, cross-process single-flight, the
`propose_report` alias, `note_proposed`) and the two `BACKLOG.md` rows (the `github-actions`
closure, `/readyz` against a paused Postgres) all exist and are accurate; `SERVED_ELSEWHERE_ALLOWANCE`
and `tests/conftest.py::_report_sibling_skips` both resolve; and `printenv 'API-KEY'` is absent in
this environment, so the probe-score row is as true as it was.

**Why nothing caught the two.** `tests/test_readiness_record.py` asks whether every control the
record *names* exists, whether the four sections are still there, and whether the external benchmark
figure survived an edit. All three passed throughout, because in both cases the control existed the
whole time. What moved was its **default** — a number in the record's prose, about a value the
config holds, that no assertion read.

## Decision

### 1. Amend §4 in place, and move the spend row to §2

A state record is amended, not superseded: a reader consults it for what is true now, and a §4 that
has to be read alongside a later ADR to find out which of its rows are live is worse than no §4.
This is the same argument `D-2026-09-14-tools-were-never-the-variable` made for §3, applied to the
section the preamble says decides the question.

The spend row does not simply leave: a turn's spend *is* bounded now, so the claim belongs in §2
beside the iteration cap it is the other half of — "a turn cannot loop forever" and "a turn cannot
spend without limit" are different questions, and `api/budget.py` can see neither.

The replica row stays accepted and is rewritten to the reason that is actually left, which is
`BACKLOG.md`'s: **nobody has driven two workers**, not that two are known to break.

### 2. A row that states a shipped default is checked against the setting

`tests/test_readiness_record.py::test_a_claim_about_a_shipped_default_agrees_with_the_setting`
parses the three shapes the record uses — `` `name` ships at **N** ``, `` `name` **ships on** ``,
and a `*` glob standing for a family every member of which must agree — and resolves each against
`chemclaw.core.config.settings`. Driven: stating the old "ships at **0**" fails naming the
300,000 it really ships; stating `retention_*_days` as 30 fails naming all five settings.

**"ships on" rather than a number, deliberately.** The record's own preamble refuses figures that
are a claim about one commit, and 300,000 is one — it is a site's knob and the next tuning commit
moves it. What a deployment team needs from that row is whether the ceiling is *on*, which is the
half that changed and the half a test can hold without pinning a number that should move.

What this cannot do is read the sentence around the claim. A row may say "which is off" beside a
setting that is on and this will not see it; it sees the number, which is what moved both times.

### 3. The preamble's count of untracked rows goes rather than moves

§4's preamble said "for **four** of them — neither [register], because the answer is a setting, a
credential or a tenant". Deleting the spend row made it three. The count is removed rather than
decremented, for `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`'s reason: nothing holds
it, the sentence's point is that *some* gaps have no ticket, and the number was load-bearing for
nothing except being right.

## What this does not decide

Whether the twelve remaining rows are the right things to accept. That judgement is the deployment
team's, which is what §5 of the record already says.

## What keeps it true

- `tests/test_readiness_record.py::test_a_claim_about_a_shipped_default_agrees_with_the_setting` —
  the guard this ADR adds.
- `tests/test_readiness_record.py::test_every_test_the_readiness_record_names_exists` — the new §2
  row cites `tests/test_spend_cap.py`, and this is what makes that citation resolve.
- `tests/test_readiness_record.py::test_the_record_still_carries_all_four_sections` — §4 losing a
  row must not become §4 losing its section.
- `tests/test_decision_log.py` — this ADR's id, filename, heading and ledger row.
