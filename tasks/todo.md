# Backlog implementation — waves

Working `docs/planning/BACKLOG.md` in waves. Each wave: implement, prove, fresh-context subagent
review, PR, merge on green. A closed row is **deleted** from `BACKLOG.md` in the commit that closes
it (the file's own rule).

Rows are picked by *workability here*: a row whose close needs a live gateway, a real cluster, a
credential with a balance, or a corpus this checkout does not have is not in a wave. Those stay
queued and the reason they stay is already written on each row.

## Wave 1 — five self-contained defects

- [x] **R1 — warehouse regex has no bound** (§1). `regex` runs that one transform under
  `timeout=`, because it checks its deadline inside its own matching loop; `re` stays the engine
  everywhere else. Both alternatives the row named are refused with measurements, in
  `D-2026-09-21-a-pattern-that-cannot-be-timed-out-is-run-by-an-engine-that-can`.
  `PatternBudgetError` is deliberately not an `ElnMappingError` — the per-entry handler would skip
  the row and re-run the same unfinishable match on the next one.
- [x] **R2 — a superseded re-proposal is answered `already_open`** (§3). `_REVIVE` puts the body
  back in the queue in both backends, the sweep that keeps one open row per name runs on a revive
  as it does on an insert, `revived` is the counter's fourth arrival, and `propose_skill` tests the
  standing row's *state* rather than its hash (which, being the lookup key, could only ever say
  "present").
- [x] **R3 — one unimportable sibling bundle skipped the allowance bound for all of them** (§5).
  One subprocess per bundle; the helper returns per-bundle reasons; both callers assert on what
  they measured (a partial total is a lower bound and can still fail honestly) and then skip naming
  what they did not.
- [x] **R4 — two timing bounds red the gate for machine load.** The conflicts scan is **counted**
  now, not timed, with an overlapping-corpus control so the counter is shown able to see the
  quadratic arm. The prefix burst keeps a ratio — there is no count there — but its control is the
  driven mutation rather than a stand-in, and the margin went 1.3x to 4x by making the control
  large next to machine noise.
- [x] **R5 — `retention_tool_results_days` ships at 0.** The row's premise was **stale**: the
  decision is recorded in four places and a 30-day default was tried and withdrawn on measurement.
  What was actually missing is the guarantee — `_NOT_PRUNED` states a reason per entry and
  `_PRUNABLE` had nowhere to put one, so a swept table's argument lived only in a comment. Derived
  rather than restated: a swept table must appear in the module docstring's list, where five of the
  six already were. The new test found the sixth *and* a seventh instance nobody had named.

## Verification

- `make lint` — green (exit 0, read on its own line).
- `make type` — green, 958 files.
- `make test` — serial, with dockerd up, `make up` run and migrations applied, so the
  Postgres-backed set is **not** skipped.

Baseline before the wave: **2 failed, 10375 passed, 84 skipped**. Both were artefacts rather than
findings and both are gone:

- `test_no_adr_cites_a_commit_a_squash_will_strand` — a **shallow clone**. The two commits exist;
  `origin/main` was 80 deep. `git fetch --depth=2000` turned it green, which is the honest fix
  rather than an edit to two merged ADRs.
- `test_every_compiled_graph_in_this_tree_names_its_checkpointer` — caused by this branch, and
  worth recording as a finding about the guard: it skipped `re.compile` by name, so the second
  pattern engine read as a bare graph compile. Now a named `_PATTERN_ENGINES` set, so a third entry
  has to be a third *engine*.

Also turned into evidence rather than left as a caveat: `Chemclaw3-mcp` is cloned beside this
checkout with a built `.venv`, so `tests/test_context_floor.py`'s cross-repository measurements
**ran** (23 passed, 0 skipped) instead of skipping. R3 cannot be reviewed without that.

## Review

- [ ] Wave 1 reviewed by fresh-context subagents before the PR is opened.
