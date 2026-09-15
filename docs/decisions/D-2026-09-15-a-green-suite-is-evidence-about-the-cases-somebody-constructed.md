# D-2026-09-15-a-green-suite-is-evidence-about-the-cases-somebody-constructed — what five fresh-context reviewers found in four features that were green, ADR'd and reviewed

**Status:** accepted · **Date:** 2026-09-15 · Follows up `D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`, `D-2026-09-15-an-answer-days-later-is-answered-against-a-corpus-that-moved`, `D-2026-09-15-a-flagged-answer-that-goes-out-flagged-is-a-verdict-nobody-acted-on` and `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`, none of which it supersedes: each decision stands and this is what shipping it got wrong.

## Context

PR #376 shipped four features from an evaluation of Paperclip — a durable per-actor budget (A), a
premise re-check on long-open questions (B), answer revision rounds (C) and a nightly
blocked-work sweep (D) — with 9,228 tests green, four ADRs, and a CHECKMATE pass.

A follow-up review put **five fresh-context reviewers** over it, one per feature plus one on the
cross-cutting claims, each instructed to measure rather than reason and to label every finding
CONFIRMED or PLAUSIBLE. They returned **44 findings**. Four of them would have hurt a deployment,
and none of the four required exotic conditions: an uptime of one day, a citation of a reaction
record, a connector, or a site with enough open questions.

## Decision

Fix all of them, and record what the class has in common.

**Every one of the four serious defects lives in a case no test constructed, and three of them
share one shape: a fixture that does not hold the condition its assertion names.**

| | The defect | What the test did instead |
|---|---|---|
| A | The in-process counter had no window, and `check` takes `max(in-process, durable)`, so a pod that stayed up across a boundary pinned a principal at their **lifetime** spend for ever | the roll test asserted through `budget_store.usage()` and never re-checked the tracker that did the spending |
| C | The revision loop ran **after** the turn's `AsyncExitStack` unwound, so every connector tool was dead during the pass whose purpose is to re-ground the answer | every arm passed `connectors=[]` |
| D | `collect_check_ins` had no `LIMIT`; past ~7,600 waiting rows the result exceeds Temporal's blob limit and the sweep fails **every night, permanently** | nothing ever executed the workflow; the "end to end" test wrote a hand-typed literal |

The fourth, B, is a different and worse shape: **the code contradicted a sentence another module in
the same tree had already written down.** `[[reaction-…]]` names a row in a record store, and
`kg/graph.dangling_links` says in as many words that without the external-id exemption "every
campaign and optimization note would be reported broken for links that resolve". `kg/premise.py`
was written without that branch and became the counter-example to that sentence, four days later,
refusing the archetypal use of a tool whose default kind is `measurement`.

### What the fixes are

**A.** Both halves of the budget roll on one clock. The 80% warning is edge-triggered — it fired on
every turn in a band some 130 turns wide, while the alert over that series rules out a `for:` clause
on the stated ground that a crossing "never produces a repetition". `CancelledError` is caught
separately, because it is the loss mode a rollout produces on every deploy and `except Exception`
does not catch it; the front door drains in-flight bookings on shutdown. The WARNING line names the
principal, which three documents said it did and it did not.

**B.** External citations are skipped rather than judged. `absent` refuses the *ask* but never the
*answer*: it is the one arm that is not self-validating — a wedged sidecar, broken frontmatter or a
PVC mounted empty all produce it, and `build_graph` returns an empty graph rather than raising — so
refusing a chemist on it is unappealable and backwards from the failure direction the original ADR
argues for. At the ask the model is being told and can rewrite its own citation. The premise is
derived by `AwaitRequest` itself, because the field said it "cannot be omitted" while two of three
producers omitted it, and ids are filtered through `is_note_slug`, which the migration comment
already claimed constrained the column and nothing enforced.

**C.** The stack stays open through the last graph invocation. A round that produces no text, or
raises, restores the answer the turn already had instead of shipping a blank one booked as
`completed=True`. Caps are re-asked after the loop. A status about the *check* is no longer a claim
about the *answer*: `TurnReview` splits `unsupported` from `review_notes`, so a judge outage no
longer multiplies every flagged turn's spend by `max_rounds + 1` arguing with a status line — and
the wire is byte-identical, verified, so no coordination with `Chemclaw3_ui` or `Chemclaw3_mock` is
needed. The revision prompt and the retracted answer are withdrawn from the checkpointed thread.

**D.** Paged on a requester keyset, so one person's questions never split into two notices that each
claim to be the whole of their blocked work. It delivers as `work-check-in` rather than as a digest.
The metric is replay-guarded. Tonight's page supersedes that page's unread notices, scoped to the
requesters it is about to write to.

### Corrections to four merged records

`CLAUDE.md` forbids editing a merged ADR, so these are recorded here rather than silently repaired:

- `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late` says `OWNED_SCHEDULE_IDS` "held
  15 ids". It held **16** when that was written and holds 17 now. The conclusion it supports —
  that scheduled agent work is not forbidden here — is unaffected.
- The same ADR's "18% of the file" is **16.6%**; the numerator (1,636 characters) is exact and the
  denominator was the file mid-development.
- `D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota` states the tightening half of its own
  behavioural change and not the loosening half. The in-process counter previously never reset
  within a pod's life, so a slow burner on a stable pod was refused once they had spent the cap
  *ever*; they may now spend it every 24 hours. `.env.example` carries that sentence now, where an
  operator setting the window will read it.

## Consequences

**Six defects were found in code the four features did not add**, which is the part worth keeping.
An alert-expression extractor correct by coincidence across 51 rules. A ratio-alert guard whose
docstring promises "a third one added tomorrow is covered on the day it is added" — the third was
added *that day* and escaped it, because the guard matched `rate(` and the rule used `increase(`.
`test_every_declared_delivery_kind_has_a_producer`, which cannot see a producer that *omits* the
keyword, which is how a sweep came to deliver itself as a digest. A ratio dividing per-turn by
per-pass counts, silent at total failure for every setting but one. And a runbook line pointing at
`chemclaw_retrieval_*`, a family that has never existed — the trailing `*` is what let it past
`make prose-validate`, whose metric check requires the backticked span to end at the name.

**A guard that has only ever been run against conforming input is an untested branch.** Four of
those six are guards that passed for years while being unable to see the thing they exist to catch.

**The chart gate was run rather than deferred.** `helm`, `promtool` and `kubeconform` install in
under a minute — `docs/guides/runbook.md` says so, and the review that preceded PR #376 did not try,
so 20 render tests skipped and the alert expressions were never evaluated.
`tests/test_deploy_chart.py` goes to **207 passed, 0 skipped**, and `make helm-validate` parses all
rules. This is the "sandbox is not offline" lesson `CLAUDE.md` records about Docker, arriving a
second time about a different set of binaries: **a tool that is merely absent reads exactly like a
tool that is unavailable, and believing the second costs coverage in silence.**

**One fix in this round was itself wrong first, and is worth recording because it is the same
class.** The deferral counter added for D initially counted *requesters not reached* — and the
common deferral is a run that finishes its page and stops because a later page exists, where that
figure is exactly 0. It would have read zero on the ordinary case of the thing it exists to report.
It counts runs, and D's own run-budget test pins it.
