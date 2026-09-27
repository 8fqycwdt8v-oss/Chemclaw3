# D-2026-09-14-an-undated-note-is-not-news-every-hour — the branch written to keep DARK-7's promise re-reported 82% of the corpus on every digest run

## Status

Accepted.

## Context

Wave 4's remaining item was "an observation notifier": `promote_observations_activity` writes a
`playbook` note into the graph when a mined observation crosses both thresholds, and nothing tells
anybody it landed.

Measuring who would be told first — the method that retracted two items in
`D-2026-09-14-two-gaps-the-code-had-already-argued-shut` — found that the mechanism already exists
and is the one `D-2026-09-14-a-contradiction-only-a-querier-sees-is-not-a-warning` used: a new note
matching a standing query is exactly a digest item. Driven, a freshly written `playbook` note *does*
reach a matching subscriber.

It reaches through the wrong branch, and that is the finding.

`_is_new` opens:

```python
if valid_from is None or subscription.last_seen_at is None:
    # No date on either side: report it once...
    return True
```

The comment says "report it once". The branch returns `True` unconditionally, and the id memory
cannot cover it — `last_seen_note_ids` is scoped to the watermark's *date* and resets when that
rolls over. So an undated note re-qualifies on **every** run, forever, against
`agent/subscriptions.py`'s own promise that "asking twice does not double-notify" (DARK-7). That is
the failure this function was written to fix, in the branch written to fix it.

Measured on the shipped corpus: **32 of 39 notes carry no `valid_from`** — compound, playbook,
campaign, interaction, job-result, failure-mode and five more types. At the hourly cadence a
subscriber's digest was mostly the same notes over and over.

## Decision

Two halves, and neither is a patch around the other.

**An undated note is not new to a subscriber who has been told anything.** What `None` means settles
this rather than a preference between two failures: `Note.is_current` reads it as *open-ended* —
true for as long as anyone has known — so such a note did not become knowledge after a watermark. A
subscriber who has never been told anything still hears it once, which is the whole of "silence is
the worse answer" that survives.

**`playbook_note` takes `minted_on`.** A playbook is the one note type nobody writes on a day: a
miner concludes it. Undated, it looked to the only mechanism that notifies anybody exactly like
something that had always been there — so under the rule above it would reach nobody with a
watermark, which is the observation-notifier gap reappearing one layer down. The day the miner ran
is the day the corpus first supported the rule, which is what `valid_from` means. It is a parameter
rather than `date.today()` inside because one caller is a Temporal activity and only activities may
read a wall clock; `observation_jobs.workflow_safe_today` exists for that and is what it passes.

## What the fix costs, stated

A genuinely new note that omits its date now reaches only a subscriber who has never been told
anything. That is the honest consequence of reading `None` as open-ended, and it is why the second
half exists rather than being optional: the note type this silence actually cost is the one the
wave item was about. Any future producer of a genuinely-new note owes the same date, and the
31 already-written undated notes are correctly treated as pre-existing.

## The test that encoded the wrong belief

`test_a_note_with_no_date_is_reported_once_rather_than_never` asserted "silence is the worse
answer" and passed over a code path delivering the other failure — its own name says "once" while
what it pinned was "every time", because it drove one call rather than two. It is replaced by
`test_an_undated_note_is_told_once_and_then_not_again`, which drives both.

## Consequences

- A subscriber's digest stops repeating most of its corpus every hour.
- A promoted observation reaches the chemists watching its subject, which is Wave 4's notifier.
- `playbook_note`'s other caller (`memory/jobs.py`'s distillation) still mints undated playbooks;
  that is a second producer with the same gap and it is left for the pass that touches it, since
  nothing there has a clock argued for it yet.

## What keeps it true

- `tests/test_digest.py::test_an_undated_note_is_told_once_and_then_not_again`,
  `::test_a_distilled_playbook_carries_the_day_it_was_minted`.
- `tests/test_observations.py::test_a_promoted_observation_is_dated_so_it_reaches_a_subscriber`.
