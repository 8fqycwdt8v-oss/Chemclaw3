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

## R3 — the loop-schedulability assertion, added as a row and then fixed

- [x] Added as a row first, with all seven measurements. The finding worth keeping is the *shape*: the assertion
      counts heartbeat beats over windows of different length (660 ms offloaded against 1285 ms
      control on CI) and compares the raw count to an absolute bar, so the arm that offloads
      successfully is measured over a shorter window and allowed fewer beats **for being faster**.
      Its own docstring records three earlier duration-shaped forms, each replaced after failing
      under load; an absolute count has the same defect one step along. The fix is a rate against the
      control in the same process — 3.86x and 5.84x on CI's samples, ~77x here, 1.0x by construction
      if the offload is deleted.
- [x] **Then fixed, because a fourth CI sample made it 3 failures in 4 runs** (2, 3, pass, 3 beats),
      which is the common case on that runner rather than an outlier. The assertion is now a rate:
      beats-per-second offloaded over beats-per-second blocked, with `max(blocked_beats, 1)` because
      the control quantises at 0-1. Bar **2.5**, set from the four observations — 3.86x, 5.84x,
      5.88x, ~50-113x — and the old standard of "3.2x below the worst honest run and 4x above the
      block" is **not available**, since CI's honest run is 3.86x against a block of 1.0x. The
      comment says that plainly instead of burying it in a constant, and names the instrument change
      (an `asyncio.sleep(0)` heartbeat, counting loop turns rather than 1 ms ticks) as what to do if
      a future observation lands under the bar.
- [x] Verified both ways: 3 of 3 passes here, and with `asyncio.to_thread` replaced by an inline
      call — the defect the test exists for — it reds at exactly **1.00x**. Row deleted, 42 -> 41.

## Verification

- [x] `make lint`, `make prose-validate` green; `test_decision_log`, `test_backlog_register`,
      `test_deferred_register`, `test_claude_md_figures`, `test_lessons_stay_a_digest`,
      `test_docstring_paths` — all green. `ci.yml` parses.
- [x] A read-only audit of all 42 rows against the tree. See R4.
- [x] **CI found two failures I had not run for.** `test_docstring_paths` reds on the new guard's
      own docstrings, which cite dead paths as examples of dead paths — the wave-5 guard collision
      again, with my work on both sides this time. Fixed by not backticking them, rather than by
      spending entries in an allowlist whose friction is the point. And `test_context_budget`'s
      fourth sample, which turned R3 from a row into a fix.
- [ ] Full suite over the fixes, fresh-context review, PR, merge on green CI.
- [ ] Substantive rows from the audit's 30 live ones — next wave.

## R4 — the audit, and the guard it argued for

All 42 open rows checked against `HEAD`, read-only: **1 stale-closed, 5 stale-wrong, 6 declined,
30 live**. So R1 was the *only* row closed and undeleted — the register is in better shape than R1
suggested, which is worth saying as plainly as the finding was.

What it did find is rot in the *citations*, and that turned out to be the derivable half.

- [x] **A guard over anchors**, `test_every_anchor_resolves_to_something_in_the_tree`. Scope chosen
      from measurement rather than taste: 140 backticked tokens in the file look path-like and 124
      resolve, but most of the remainder are ordinary prose shorthand (`fanout.py`, `serve.py`,
      `spend_cap.py`) that no guard should turn into a style rule. Requiring a directory **and** a
      `::symbol` excludes those by construction and leaves 43 real anchors.
- [x] Three were dead: `agent/interaction_tools.py::start_approval` (a module that never existed —
      the row *quotes* it to say so, so it is an allowlisted exemption in the `_HISTORICAL` shape,
      with its own test that the sentence still says it does not exist);
      `retrieval/vector_index.py::note_reindex_effective`, which is a `Settings` property in
      `core/config/__init__.py`; and `tests/test_sibling_manifest_agreement.py::_DISPATCHERS`, now
      `_CALC_SEAM.dispatchers`. The last one I had already spotted in wave 5's triage and left.
- [x] **Both arms falsified.** Breaking one anchor reds it; neutering the exemption's sentence reds
      the allowlist guard — but only after fixing that guard, because `retracted` was in the marker
      set and the exempted line says "is retracted" elsewhere, so the check passed against a
      sentence rewritten to claim the module *does* carry the approval flow. Driven, and it passed.
      A retraction says a claim was withdrawn; only "never existed" licenses the exemption.
- [x] **The guard then rejected the first anchor added after it — mine.** A dotted `Class.method`
      citation fails a whole-string word match, because the source spells the two on separate lines.
      `_resolve` now requires every dotted component.
- [x] Six figures corrected, each re-measured here rather than taken from the audit: the
      side-effecting surface is **54**, not 49 (`authz.side_effecting_tools()`); the knowledge corpus
      is **34 of 41 notes undated across twelve types**, not 33 of 40 across ten, whose own per-type
      numbers summed to 32; `deep-research`'s corpus is **41**, where the row said 39 and then 40
      three sentences apart; the bundle-skill universe is a glob over **seven** bundles, not the
      three the row named; `STANDARDIZATION_VERSION` is std9.
- [x] **One figure was mine, made stale by my own merged commit.** The mutation row said the test
      selection is "a hand-kept list of 16 files" — my commit added the seventeenth. Rather than
      renumber it I removed the transcription; the load-bearing "2 of those contain a `TRUNCATE`"
      re-measured and still holds. The same 16 is in a merged ADR, which `CLAUDE.md` forbids
      editing, so it stands there as the cost of having written a count into prose.
- [x] Five drifted `path:LINE` citations fixed, including one I added this morning — converted to a
      symbol so it cannot rot again. That whole class is outside the new guard and its docstring
      says so, with the five as the evidence.

## Review

Wave 7's findings are about the *record* rather than the code, and they point in opposite
directions. One row said a bound was missing that had shipped, in wave 4, with the plan file ticking
an ADR that does not exist. One comment in `ci.yml` said a control was broken when it was working
exactly as designed, and I had the workflow fix half-drafted before reading the code it was about.
Both are the argument for auditing the register against the tree before picking work from it, which
my wave 5 triage should have done rather than reading rows and believing them.

**The audit's headline is reassuring and worth stating that way.** One row of 42 was closed and
undeleted — the one I had already found. Thirty are accurate as written. So the register is not
rotten; what rots is the *citations*, and that is the half a rule can hold, which is why this wave
ends with a guard rather than with a list of corrections.

Three things in here were mine. A count my own merged commit made stale, in three places, in the
commit that made it stale. A `path:LINE` citation I added this morning, in a class of citation the
audit had just shown drifts. And a guard whose exemption arm passed against a sentence rewritten to
say the opposite, because I had put `retracted` in the marker set — caught only because I drove both
arms instead of the one I expected to fail.

Lessons 137-139.
