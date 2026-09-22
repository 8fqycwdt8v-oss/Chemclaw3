# Wave 4 — three bounds deferred as "a decision rather than an edit", now decided

The remaining backlog has a different character from waves 1–3: the self-contained defects are worked,
and what is left is mostly rows that *chose* not to fix something and said why. Seven of the 45 carry
an explicit decline with a trigger. These three do not — each says the fix is ADR-sized, and each was
right that it is a decision. **All three also turn out to be wrong about something**, which is now
five of fifteen worked rows found stale or misstated, and is why each is re-derived before any code.

## R1 — a per-cell regex budget does not add up to a page bound

Row: `eln_regex_timeout_seconds` bounds one `search`, and `warehouse/adapter._read` runs one per
reaction field, per attribute, and per component and impurity *row*.

**What the row got wrong about its own mechanism.** It describes the accumulation as timeouts adding
up. They cannot: `PatternBudgetError` is in `durable/publish._BAD_DATA_TYPES`, so a cell that exceeds
the per-cell budget ends the page after **one** cell, non-retryably (Wave 1). Measured: the reachable
case is a pattern that is slow and *completes*.

| measured on this box | |
|---|---|
| `a*a*a*$` over a 6,000-char cell | **165 ms** — 66% of the per-cell budget, never refused |
| twenty such cells x 100-entry batch | **330 s** vs `eln_sync_timeout_seconds` 300 s (1.1x over) |
| cells reached before the activity deadline | 1,818 of 2,000 |
| an honest cell (`(\d{3,6})`), warm | **0.0024 ms** — ~68,000x cheaper |
| an honest page of 2,000 cells | **0.0048 s** — ~31,000x inside the budget |

- [x] `eln_regex_page_budget_seconds`, half of `eln_sync_timeout_seconds` — stated as a *split*, not a
      measurement, because the page also writes and nobody has measured that.
- [x] `expr.pattern_budget()` — a re-entrant contextvar **accumulator of matching time**;
      `_cell_budget()` clamps each `search` to what the page has left, so the last one cannot
      overshoot by a whole cell budget. Driven: +0.001 s of overshoot at a 2 s budget, vs 32.5 s
      unbounded. It was a wall-clock *deadline* first, which billed the page's own stores and fetches
      — a review measured 1.13 ms of matching exhausting a 500 ms budget.
- [x] **Three** refusals, not two: a page spent before the search, a page spent *inside* one (which
      must not claim the pattern is innocent, since it never got its full allowance), and a pattern
      that blew its own ceiling unclamped. The two-way version's pattern arm was unreachable — driven
      over 39 clamped remainings, it fired 0 times.
- [x] Opened inside `sync_entries`, `memory_jobs.read_corpus`, `ingest/eln/validate.py` and both
      `live_data` loops — five sites — rather than at each activity, for the reason `turn_caps` was
      extracted.
- [x] A derived test: a module with a `map_to_ord` call *inside* a loop must name it. It found the
      `validate.py` site; a companion test pins the four it matches so it cannot pass vacuously.
- [x] Tests: the measured case refused; an honest page untouched; the overshoot bound; all three
      refusals distinguished; I/O not billed; a spent page never handing the engine a negative
      timeout (which `regex` reads as *no* timeout).
- [x] ADR + delete the row.

## R2 — a batched flush books the whole batch as notes recorded

**The row's premise is false, and that makes the fix a commit rather than an ADR-sized contract
change.** It says the honest number "is not available at that layer" because a changed-file count
"counts *dependency* notes and retirement rewrites too". Measured: those are separable by the
`(overwrite, amendment)` flag pair, there is exactly one subject file per `NoteWrite`, and
`_write_and_commit` already holds `prior` — the pre-write bytes of every planned path — one line
before it asks git the same question as a boolean.

- Reproduced at HEAD: batch of 4, three byte-identical, one new → metric **+4** for one note.
- At the shipped `backfill_commit_batch_size` 50: 49 identical + 1 new → metric **+50**.
- Exactly as large as the undercount `D-2026-09-14` fixed, in the other direction.
- Nothing turns red: the two counter tests sit on the all-new and all-noop ends, so a partially
  identical batch is covered by neither — the same coverage shape that ADR called out.
- [x] `_changed_subjects` counts the **distinct** changed subjects; `flush` is a pass-through, so
      `len(batch)` is deleted rather than corrected.
- [x] Floored at 1 where a commit landed. Without it, a write whose subject is identical while a
      *dependency* changed reported `notes=0 written=False` on a write that committed and pushed —
      a regression a review measured against `origin/main`, breaking `WriteOutcome`'s own contract.
- [x] A superseding ADR, since `D-2026-09-14` writes `len(batch)` into a merged document.
- [x] A test over a mixed batch, checked against `git diff-tree --name-only` as well as the metric,
      plus the dependency-only commit arm.
- [x] The count of `WriteOutcome(` constructions: **five** at HEAD, none defaulted. My first ADR text
      quoted the pre-change six inside the document that changes it.

## R3 — a front door scaled to zero renders a release where every pod refuses to start

**The row's headline reproducer does not reproduce.** `--set service.replicas=0` is a **no-op**:
`service.autoscaling.enabled` ships true, and both readers of `service.replicas` are gated on the HPA
being off. Under shipped defaults `service.replicas: 2` renders **nowhere at all** — dead config
carrying no prose, which is its own finding.

It does reproduce two other ways (`autoscaling.enabled=false` + `replicas=0`, or `maxReplicas=0`),
and the blast radius is **worse** than the row states:

- The failure is at `core/config/__init__.py`'s module-level singleton, so it is not about which
  process *reads* the setting — every process that imports `chemclaw.core.config` dies. Driven over
  nine entrypoints: all nine.
- `deploy/entrypoint.sh` runs `python -m chemclaw.cli.egress_preload` under `set -euo pipefail`
  *before* its `case`, so every container dies in the shell prologue.
- The `migrate`/`schedules`/`convert` hook Jobs fail too, so `helm upgrade` never converges.
- `maxReplicas=0` is strictly worse than `replicas=0`: the HPA-on branch omits `replicas` entirely, so
  a rejected HPA leaves Kubernetes defaulting the front door to 1 crash-looping pod.

**The decision is that the bound is right and the chart should say so.** There is no front-doorless
story: no `service.enabled` gate exists, `deployment-service.yaml` and the Service open with no `if`,
and nothing in `deploy/` or `docs/` asks for one. So `service_fleet_replicas` stays `gt=0` and the
chart refuses to render a zero front door, beside its 19 existing `fail` guards — a render-time
refusal naming the key instead of eleven crash-looping pods and a stuck upgrade.

- [x] The chart guard in `chemclaw.frontDoorProcesses`, covering both reachable arms. Verified it
      refuses nothing legitimate (fixed count with the HPA off, `maxReplicas=1`) and is safe on an
      empty, null or non-numeric value.
- [x] The rollout-peak over-declaration (137 vs 120) becomes unreachable once zero is refused, so it
      is recorded rather than fixed.
- [x] `service.replicas` rendering nowhere under the shipped HPA: documented in `values.yaml` and
      pinned by a test that reds if it ever becomes live.
- [x] ADR + delete the row.

## Verification

- [x] `make helm-validate` renders only defaults plus the flag union, so it could not have caught R3.
      The new tests carry their own render arms.
- [x] Fresh-context subagent review — read-only this time. Five confirmed defects and eight falsified
      figures, all listed below and all fixed.
- [x] Full serial suite over the review fixes: **10527 passed, 7 skipped, 0 failed** (23:27). The
      7 skips are environmental and declared — 4 tiktoken cache, 1 no IPv6, 2 surfaces that are
      deliberately not a pod; none Postgres- or Temporal-gated.
- [x] `make lint type` immediately before the commit, staged content verified, and the committed
      tree checked to import — the three steps a broken Wave 3 commit taught.

## Review

Three rows closed, three ADRs, 45 → 43 open. **Every one of the three was wrong about something**, and
in each case the error was in the direction that made the fix look harder or the defect smaller than it
was — which is now six of eighteen worked rows found stale or misstated.

**R1's mechanism was wrong.** The row described the accumulation as per-cell timeouts adding up; they
cannot, because a cell that exceeds its budget ends the page after one cell non-retryably. The reachable
case is a *polynomial* pattern that completes at 66% of the budget: 165 ms a cell, 330 s a page against a
300 s activity deadline. Finding that changed what the fix had to bound, and the honest-page ratio
(~68,000x) is what settles the trade the row said needed deciding.

**R2's premise was false.** It recorded the honest count as unavailable at that layer and a changed-file
count as unable to separate dependencies from subjects. `prior` already holds the pre-write bytes of
every planned target, and the flag pair separates the three kinds of file exactly — so the ADR-sized
contract change was a filter on two flags, and `len(batch)` deleted rather than corrected.

**R3's reproducer does not reproduce.** `--set service.replicas=0` changes not one byte of the shipped
render, because the HPA ships enabled and both readers of that key are gated on it being off. The blast
radius is meanwhile *worse* than stated: the failure is at the config singleton, so all nine entrypoints
die, and `entrypoint.sh`'s egress preload kills every container before its `case` — hook Jobs included,
which is what turns a broken release into a stuck upgrade.

Two things I got wrong mid-wave and fixed by measuring:

- My first R1 driver used a timing-out pattern and measured the wrong thing entirely; the page it
  "proved" unbounded actually aborts after one cell.
- The page refusal quoted `settings.eln_regex_page_budget_seconds` rather than the budget in force, so it
  said 150 s at a 2 s budget — a false figure in a message a site acts on.

The derived guard over page loops found a fourth one I had missed (`ingest/eln/validate.py`), and its
first spelling over-matched three modules that map one entry at a time, so it now asks whether the call
is *inside* the loop and a companion test stops it passing vacuously.

### What the fresh-context review found

Five confirmed defects and eight falsified figures. Three of the defects were mine, introduced by this
wave, and the figures are the category this whole session has been about — so they are listed in full.

**The page budget was a wall clock, not a matching budget.** `pattern_budget` is opened *around* the
page loop, whose body awaits five stores per entry and, in `memory_jobs.read_corpus`, every
`fetch_new_entries` of every page of every source. A `monotonic()` deadline there bills Postgres and the
source: driven, **1.13 ms** of matching exhausted a 500 ms budget. And it was worse than a wrong
message — `PatternBudgetError` is non-retryable, so a page that used to reach the 300 s activity timeout
and be *retried* would fail permanently at 150 s with no cursor advanced. It is now an accumulator of
the time the engine is actually given.

**The page-vs-pattern branch was unreachable.** A clamped search is given exactly what the page has
left, so it times out at the instant the page runs dry — my "re-read the remaining budget" test is
always spent by then. Over 39 clamped remainings against `(a+)+$` the pattern arm fired **0** times, so
every catastrophic pattern was reported under a sentence claiming no transform had exceeded its ceiling.
Three outcomes now, and the clamped one says the pattern's own cost is *not established*.

**`_changed_subjects` regressed the non-batched path.** A write whose subject is byte-identical while a
dependency or retirement changed does commit, and counting only subjects gave `notes=0 written=False` on
a write that committed and pushed — breaking `WriteOutcome`'s stated contract and undercounting the
metric this fix exists to correct, in a case the old code got right. Floored at 1 where a commit landed.

**`cells` counted regex applications, not cells** (several steps per value, none for a NULL column), in
a number the refusal makes load-bearing. Renamed `searches` and the message says "transform(s)".

**`live_data`'s prose check swallowed the refusal** through `except Exception`, so exhausting the budget
skipped every remaining entry and the check still passed with a smaller denominator — the "silent
denominator" failure its own docstring names.

**The falsified figures.** An honest cell is **0.0024 ms** warm, not 0.472 ms: I timed the first call,
including this module's `lru_cache` compile miss (0.3 ms on its own). So 349x → ~68,000x, 0.94 s → 0.0048
s, ~160x → ~31,000x. My own table also gave 0.472 ms and 0.042 s for the same measurement — 22x apart,
both wrong — which is precisely the "two mutually exclusive numbers for one fact" defect I have been
deleting from this repository all session. Corrected in `core/config/eln.py`, `.env.example`,
`expr.pattern_budget`, the tests and here. Also: the R2 ADR quoted the *pre-change* count of
`WriteOutcome(` constructions inside the document that changes it (five at HEAD, none defaulted); two
test docstrings described scenarios their bodies do not build; and the derived guard's docstring claimed
`validate.py` was excluded when it is correctly matched.

Confirmed correct and left alone: 165 ms, 66%, 330 s, 1,818 of 2,000, +0.001 s of overshoot, the 50x
batch overcount, and every figure in R3.

Lessons 121–125 added.
