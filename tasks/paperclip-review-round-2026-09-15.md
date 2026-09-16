# Deep review of the four Paperclip items, and what it found

PR #376 shipped four features (A durable budget, B premise re-check, C answer revision rounds,
D the blocked-work sweep) with green CI. This is the follow-up review: **five fresh-context
reviewers, one per feature plus one on the cross-cutting claims**, each told to measure rather than
reason and to label every finding CONFIRMED or PLAUSIBLE.

They returned **44 findings**. The headline is not any single one of them — it is that a green
9,228-test suite, four ADRs and a CHECKMATE pass had all held over defects that a probe found in
minutes, because **every one of them lives in a case no test constructed**.

## The four that would have hurt a deployment

| | What | Why it survived |
|---|---|---|
| A-F1 | The in-process counter had **no window**, and `check` takes `max(in-process, durable)`. So a pod that stayed up across a boundary pinned a principal at their *lifetime* spend for ever. Measured: 900 tokens booked, durable row correctly rolled to (0,0), long-lived pod still refuses against a 500 cap while a fresh tracker admits. | The roll test asserted through `budget_store.usage()` and never re-checked the tracker that did the spending. |
| B-1 | `[[reaction-…]]` resolves outside the graph, so every citation of a reaction *record* was reported `absent` and **refused the ask**. `measurement` is the tool's default kind and "confirm the yield for [[reaction-abc]]" is its archetypal use. | Both other citation readers exempt the external namespace; this one was written without that branch and no test cited one. |
| C-1 | The revision loop runs **after** the turn's `AsyncExitStack` unwinds, so every connector tool is dead during the pass that exists to re-ground the answer. | Every arm of the new test file passed `connectors=[]`. |
| D-2 | `collect_check_ins` has no `LIMIT`. Measured against the live broker at 10,000 rows: `Complete result exceeds size limit`, non-retryable, **every night, permanently** — the exact defect the sweep exists to fix, reproduced by its own scaling. | Nothing ever ran the workflow; the "end to end" test wrote a hand-typed literal. |

## The pattern worth keeping

Three of the four are the same shape: **a fixture that does not hold the condition the assertion
names.** The budget roll test aged one half of a two-half system. The revision tests bound no
connectors. The check-in test never called the writer. Each suite was green and each was green about
something other than the feature.

The fourth (B-1) is different and worse: the code contradicted a sentence another module had already
written down. `dangling_links` says in as many words that without the external-id exemption "every
campaign and optimization note would be reported broken for links that resolve" — and this function
had become that counter-example, in the same tree, four days later.

## Also found, none of them in the new features

- An alert-expression extractor that was correct **by coincidence** across 51 rules.
- `test_every_ratio_alert_has_a_traffic_floor`, whose docstring claims "a third one added tomorrow
  is covered on the day it is added" — the third was added *that day* and the guard did not see it,
  because it matched `rate(` and the new rule used `increase(`.
- A ratio alert dividing per-turn by per-pass counts, silent at total failure for every setting of
  `answer_review_max_rounds` but 1.
- `test_every_declared_delivery_kind_has_a_producer` cannot see a producer that *omits* the keyword,
  which is how the check-in sweep came to deliver itself as `kind="digest"`.
- The runbook sending an operator to `chemclaw_retrieval_*`, a family that has never existed; the
  trailing `*` is what let it past `make prose-validate`.
- Three documents stating in the present tense that a WARNING log line carried the principal's
  identity. It carried the scope *kind*.

## Verification

Unlike the review that preceded PR #376, this one ran the chart gate: `helm`, `promtool` and
`kubeconform` install in under a minute (the runbook says so and was not believed), and
`tests/test_deploy_chart.py` goes from **20 skipped to 201 passed, 0 skipped**, with
`make helm-validate` parsing all 207 rules. The sandbox note in `CLAUDE.md` is about Docker; it
applies to the chart binaries identically.
