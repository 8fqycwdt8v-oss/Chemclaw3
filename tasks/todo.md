# Wave 7 — the record and the tree disagree

Waves 5 and 6 merged as #435. Wave 7 opened on a finding about wave 4's own work, which is why it
starts with an audit rather than with a row.

## R1 — a row that outlived its closure, and the ADR that was never written

Picking the next rows, I read *"A per-cell regex budget does not add up to a page bound"* — the row
**wave 4 closed**. Same title, same anchors.

- [x] Verified the fix shipped: `core/config/eln.py::eln_regex_page_budget_seconds` (150.0),
      `ingest/eln/warehouse/expr.py::pattern_budget`, five call sites
      (`sync_entries`, `memory_jobs.read_corpus`, `ingest/eln/validate.py`, both `live_data` loops),
      `.env.example` line 880. The code is live.
- [x] Verified the record is not: **no file in `docs/decisions/` mentions
      `eln_regex_page_budget_seconds` or `pattern_budget`**, and the row was still in
      `BACKLOG.md`. Wave 4's own `tasks/todo.md` ticked "ADR + delete the row".
- [x] Wrote the missing ADR — `D-2026-09-22-a-page-budget-counts-matching-time-not-wall-clock` —
      from the code, because that is where the measurements live. It records four choices with their
      rejected alternatives: per *page* rather than per entry or per activity; an **accumulator of
      charged matching time** rather than a `monotonic()` deadline (the deadline billed the loop's
      five stores per entry, so 1.13 ms of matching exhausted a 500 ms budget and turned a retryable
      timeout into a permanent non-retryable wedge at half the activity deadline); each search
      clamped to what the page has left; and three refusals rather than two, the two-way form's
      pattern arm having fired 0 times over 39 clamped remainings.
- [x] Deleted the row, and said in the ADR's own opening that it is late and why that matters.
- [x] Ledger row + topic-table citation; `test_decision_log` 20 passed.

**The cost of the miss is the point.** My wave 5 triage counted that row among the 28 actionable
ones, so the gap cost a second reader — me — real time, and anyone reading the backlog was told a
bound did not exist that had shipped. Lesson 137.

## R2 — a comment in `ci.yml` that gives the wrong diagnosis

- [x] CI's log carries a "Cross-repository checks did not run" block on **every** run, while
      `ci.yml` says that block "should be absent — its presence means this variable stopped reaching
      them". Both halves are wrong: the variable arrives, and what is missing is the *sibling's own*
      `.venv`.
- [x] **Nearly filed the design as a defect.** `tests/siblings.py::sibling_python` splits the two
      costs deliberately and argues only one is plausible in CI — reading manifests needs a shallow
      clone, running the servers to measure their schemas needs RDKit, torch and a T5 checkpoint's
      dependencies. So the 5 schema-measuring tests skip by design, the epilogue's job is to say so,
      and `SERVED_ELSEWHERE_ALLOWANCE`, `FLEET_PUBLISHED_ALLOWANCE` and `PREFIX_BOUND` are unchecked
      in CI *as a decision*. Lesson 138.
- [x] So the fix is the sentence, not the workflow: it now says the block is expected, names what
      would signal the variable actually stopping (a "could not be read" skip with no path, against
      the current "<path> has no .venv"), and states that installing the sibling in CI is a real
      decision nobody has taken, with what it buys and costs.

## R3 — the loop-schedulability row I promised on #435

- [x] Added, with all seven measurements. The finding worth keeping is the *shape*: the assertion
      counts heartbeat beats over windows of different length (660 ms offloaded against 1285 ms
      control on CI) and compares the raw count to an absolute bar, so the arm that offloads
      successfully is measured over a shorter window and allowed fewer beats **for being faster**.
      Its own docstring records three earlier duration-shaped forms, each replaced after failing
      under load; an absolute count has the same defect one step along. The fix is a rate against the
      control in the same process — 3.86x and 5.84x on CI's samples, ~77x here, 1.0x by construction
      if the offload is deleted.

## Verification

- [x] `make lint`, `make prose-validate` green; `test_decision_log`, `test_backlog_register`,
      `test_deferred_register`, `test_claude_md_figures`, `test_lessons_stay_a_digest`,
      `test_docstring_paths` — all green. `ci.yml` parses.
- [ ] A read-only audit of all 41 remaining rows against the tree, for others in R1's state.
- [ ] Substantive rows once the audit says which are real.
- [ ] Full suite, fresh-context review, PR, merge on green CI.

## Review

Wave 7's first two findings are both about the *record* rather than the code, and in opposite
directions: one row said a bound was missing that had shipped, and one comment said a control was
broken when it was working as designed. Between them they are the argument for auditing the backlog
against the tree before picking work from it — which is now running, and which my wave 5 triage
should have done rather than reading the rows and believing them.
