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
| an honest cell (`(\d{3,6})`) | **0.472 ms** — 349x cheaper |
| an honest page of 2,000 cells | **0.042 s** |

- [x] `eln_regex_page_budget_seconds`, half of `eln_sync_timeout_seconds` — stated as a *split*, not a
      measurement, because the page also writes and nobody has measured that.
- [x] `expr.pattern_budget()` — a re-entrant contextvar deadline; `_cell_budget()` clamps each
      `search` to what the page has left, so the last cell cannot overshoot by a whole cell budget.
      Driven: refused at 2.00 s against a 2 s budget, **+0.001 s** overshoot, vs 32.5 s unbounded.
- [x] Two refusals, not one: the per-cell one names a pattern to rewrite, the page one names how far
      the page got. `_PageBudget` carries the budget so the message quotes the number in force — it
      read the *setting* first and said 150 s at a 2 s budget.
- [x] Opened inside `sync_entries`, `memory_jobs.read_corpus` and both `live_data` loops rather than at
      each activity, for the reason `turn_caps` was extracted.
- [ ] A derived test: a module that maps entries in a loop must enter it.
- [ ] Tests: the measured case refused; an honest page untouched; the overshoot bound; the two
      refusals distinguished; `PatternBudgetError` still non-retryable.
- [ ] ADR + delete the row.

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
- [ ] `WriteOutcome` (or the inner call) yields the subject paths that changed; the batcher counts
      **distinct** ones.
- [ ] A superseding ADR: `D-2026-09-14` writes `len(batch)` into a merged document, and its own guard
      sentence ("notes recorded", not "notes offered") already forbids what the mixed batch does.
- [ ] Also drifted there: it says "the four in `git_writer.py`"; there are six `WriteOutcome(`
      constructions and one relies on the default.
- [ ] A test over a mixed batch, checked against `git diff-tree --name-only` as well as the metric.

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

- [ ] The chart guard, covering both reachable arms and the `minReplicas > maxReplicas` case.
- [ ] The rollout-peak arithmetic charges 3 front-door pools + a readiness pool for a Deployment that
      never surges (137 declared vs 120 honest). Safe direction, but "the arithmetic is right at zero"
      does not survive the peak keys — decide whether to fix or record it.
- [ ] `service.replicas` rendering nowhere under the shipped HPA: prose, or a guard.
- [ ] ADR + delete the row.

## Verification

- [ ] `make helm-validate` renders only defaults plus the flag union, so it could not have caught R3.
      Whatever guard lands needs its own render arm.
- [ ] Full serial suite; `make lint type` immediately before the commit; staged-content check.
- [ ] Fresh-context subagent review before the PR.

## Review

Three rows closed, three ADRs, 45 → 43 open. **Every one of the three was wrong about something**, and
in each case the error was in the direction that made the fix look harder or the defect smaller than it
was — which is now six of eighteen worked rows found stale or misstated.

**R1's mechanism was wrong.** The row described the accumulation as per-cell timeouts adding up; they
cannot, because a cell that exceeds its budget ends the page after one cell non-retryably. The reachable
case is a *polynomial* pattern that completes at 66% of the budget: 165 ms a cell, 330 s a page against a
300 s activity deadline. Finding that changed what the fix had to bound, and the honest-page ratio (349x)
is what settles the trade the row said needed deciding.

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
