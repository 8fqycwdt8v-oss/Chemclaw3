# D-2026-09-05-four-reviews-of-one-days-measurement — what four fresh contexts found in yesterday's A/B

**Status:** accepted · **Date:** 2026-09-05 · Reviews
`D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter` (#308) and #310. Applies
`D-2026-09-04-a-review-of-a-review-finds-the-fixes`' rule to the change that shipped one commit
after it: **a fix is a change, and a change gets reviewed by somebody who did not write it.**
The merged ADRs are not edited; this is the correction.

## Context

#308 built the tool-utility A/B, ran it over 221 probes, and shipped five files of code, four of
tests, and four documents of prose about the result. Its gate was green and its author wrote its
tests, which is exactly the configuration the ADR one commit earlier says is evidence about internal
consistency rather than about correctness. Four reviewers were given disjoint slices — the A/B
wiring, the judge and lane changes, the test infrastructure, and every documented number against the
committed evidence — and told that a finding without a command ranks below what was run.

They returned seventeen findings. Two of the four HIGHs are defects in code that ran; the other two
are false sentences, one of which contradicts an ADR that was already in `main` when it was written.

## The defects, and what each one cost

**`--sample` was the anti-pattern its own comment disclaims.** `probes[::len // n][:n]` computes an
integer stride, so for every `n` above half the corpus the stride is 1, the slice is a no-op and the
truncation *is* `probes[:n]` — the first n probes in section order, which the comment beside it
calls out by name as the thing not to do. Over the shipped A+C set, **110 of the 221 possible values
of `--sample` returned exactly that**. Below the halfway point it was still wrong in the other
direction: truncating a strided list cuts from the tail, so `--sample 50` stopped at probe 196 of
220 and never asked a section-13 or -14 question. It shipped with **zero** tests. It is now
`_systematic_sample`, a named function pinned at both ends and the degenerate middle, asserting the
property rather than a proxy for it — cut the corpus into `n` equal bands and each contributes
exactly one probe. The first version of that assertion was itself wrong (it demanded the last draw
sit in the final tenth, which is false at `n = 2`, where the correct stratified draw is the first
probe of each half).

**A crash in the live lane's new fleet derivation was invisible.** `for name in $(fleet_bundle_names
…)` does not propagate a non-zero exit under `set -e` — only an assignment does. A traceback inside
the derivation would print to stderr and the loop would then run over whatever partial output
preceded it, starting some bundles and skipping others, with the failure surfacing minutes later as
the front door's `ConnectorsUnavailable` naming a connector nobody had noticed was missing. That is
the *same* defect the derivation was written to remove, one layer up. Both directions are now
measured: the assignment form dies, the loop form reached the end and exited 0. An empty derivation
is refused too — `fleet_checkout_python` already catches a missing checkout, and this catches a
present one that publishes no manifest this repository declares an endpoint for.

**Handing the judge the agent's cached client armed a footgun rather than defusing one — and the
fix for it is not in this commit, because `main` had already made the whole thing moot.**
`_tls_http_client` is a process-wide singleton the agent holds for its whole life, and
`AsyncAnthropic` stores a caller-supplied `http_client` unwrapped and `aclose()`s it from its own
`close()`/`__aexit__`. So one idiomatic `async with AsyncAnthropic(...)` in the judge would have
closed the agent's client for the rest of the process — in exactly the private-CA configuration
#308's change existed to fix. Nothing triggered it, which is what made it worth fixing before rather
than after.

The fix written here shared the *policy* instead of the pool — a `tls_verify()` returning the
`SSLContext`, immutable configuration with no owner — and **it was thrown away on the merge**:
`D-2026-09-04-a-gateway-is-the-only-provider` (#313) landed while this review was running and
rebuilt `live_judge` on `build_chat_model`, so the judge no longer constructs an Anthropic client at
all and reaches the private CA through the one seam that already handles it. Taking that instead of
carrying a second answer is the correct outcome, and it cost nothing but the writing. **The finding
was real and the remedy was superseded**, which is the ordinary shape of a review that runs while
its base branch moves — and is why this ADR records it rather than quietly dropping it.

One thing from that discarded fix is worth keeping on record, because it is about caches rather than
about Anthropic: with `@cache` on `tls_verify`,
`test_the_private_ca_client_is_built_once_per_process` passed alone and failed in file order,
because its neighbour cleared one cache and not the other. **A second cache keyed off one setting is
a second thing every test has to clear, and the two disagreeing is a stale CA.**

**And a guard that leaked what it was checking.** `_assert_baseline_profile` opens a session to prove
the front door knows the control profile, and never deleted it — one orphan `session_owners` row per
A/B run, for a conversation that never had a turn.

## The false sentences

**#308 asserted that CI never installed `helm`, one commit after the ADR that says it does.**
`D-2026-09-04-a-review-of-a-review-finds-the-fixes` had already established that `ubuntu-latest`
ships Helm, so the `check` job has been rendering the chart throughout and the five HIGH chart
defects survived because the tests rendered **one** set of values — "not a gate that could not run,
but a gate that ran against one configuration". #308's branch was cut before that ADR landed, which
explains how it was written and not why it survived review: the claim was checkable against `main` at
merge time. The `Install Helm` step stays, because it fixes a real and different defect — `check` was
rendering on whatever version the runner image carried that week while `chart` rendered on a pinned
v3.13.0, and two jobs validating one chart through two renderers is drift worth ending. The
rationale beside it now says that instead.

**The count was 33 and it is 59.** Measured with `helm` off `PATH`: 59 skips across
`test_deploy_chart.py` and `test_helm_chart.py`, and the epilogue's own printed number was right all
along. 33 came from a `BACKLOG.md` row, was never re-derived, and was written into a docstring
**inside the commit that added the counter whose entire job is to make the count unnecessary** —
`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`, violated by the mechanism that
implements it. Neither the docstring nor the workflow comment states a number now.

**The cost table rested on nothing committed.** Every other figure in that run traces to
`evidence.json` or a transcript beside it; the token counters were Prometheus deltas that existed
only in the prose quoting them, which is the "number nobody can re-derive" failure this repository
names — and it does not become acceptable because the subject is a cost rather than a result. Both
scrapes are now committed as `token-counters.txt`, which also explains the arithmetic the reviewer
caught: the exposition rounds to six significant figures, so the components miss their totals by one.

**Two more, both about populations.** The bucket-A verdict table put a 169-probe row beside a
173-probe one, because the toolless arm keeps the four verdicts whose augmented half was ungraded —
a table whose rows are different populations is not a comparison, and it now states both. And the
four dropped pairs were described as "evidence about the grader rather than about either arm" while
**all four are on the augmented side**, every one for the same reason: the judge's reply hit its
token ceiling on the transcripts that carry tool calls. Four of 221 cannot move the headline; the
asymmetry is a selection effect that a run with more of them would have to fix first.

Also corrected: `bucket A's 169` (the paired count wearing the corpus's sentence — it is 173),
`125 of 288` (the denominator is 292; the numerator was right), and a `BACKLOG.md` row still naming
`os.getpid()` a day after the commit that replaced it — stale prose left behind by the commit that
edited seventy lines of the same file.

## Consequences

- **The finding rate says the review was worth more than the change.** Four reviewers over one day's
  work found two live defects, one dormant one, and four false claims — in a diff that passed
  `make lint type test`, ten validators and 6,602 tests. Reviewing a measurement's *prose* against
  its own committed evidence is the arm that found the most, and it is the arm nothing automates.
- **`^` in `templates/resolve._WHOLE` is dead notation**, and the test added for it in #308 cannot
  pin it: `_WHOLE` is used only as `.match()`, which anchors at position 0 whatever the pattern
  says. mutmut deleting that `^` would report a permanently surviving mutant that no test can kill —
  a true statement about the notation, which would read as a false one about the suite. The test now
  says so rather than implying it guards something.
- **`TEST_SCHEMA` had one byte of headroom.** A full uuid4 hex put
  `f"{TEST_SCHEMA}_no_checkpointer"` at 62 bytes against PostgreSQL's 63, which truncates silently
  rather than erroring. Twelve hex digits is 2^48 draws against the handful a machine makes in a day
  and leaves twenty bytes.
- **A review that runs while its base branch moves must re-read the base before it merges.** Three
  PRs landed during this one; #313 rewrote both files the judge finding touched, and #312/#311 did
  not collide. The conflict was the *cheap* way to find that out — had the judge fix merged cleanly
  into a tree that no longer needed it, this repository would have carried two answers to the same
  question, which is the failure its own `connectors/README.md` opens with.
- **What this review did not do is re-run the measurement.** Every number in the result stands —
  all of the pairing, verdict, transition, tool-use and ratio figures reproduced exactly from the
  committed evidence, which is what made the four that did not stand out.
