# D-2026-09-24-a-note-arrives-when-the-corpus-says-it-did — an arrival signal for undated notes

**Status:** accepted · **Date:** 2026-09-24 · Closes the `BACKLOG.md` row *"An agent-recorded note
the model could not date reaches no subscriber who has a watermark"*.

## Context

`durable/digest._is_new` judged freshness on a note's `valid_from` and read an absent one as
*open-ended* — right about the fact, and wrong about the question a digest asks, which is whether
the *note* is new to this subscriber. 34 of the shipped corpus's 41 notes carry no `valid_from`,
across twelve types, so a subscriber who had been told anything was never told about any of them
again. Two producers were already dated by construction (`report_note(drafted_on=…)`,
`note_with_run_provenance(ran_on=…)`); `agent/graph_tools.record_knowledge_note` cannot be,
because the model may legitimately not know when a fact became true.

## Decision

**A note with no `valid_from` is judged on the date its file was committed to the corpus**
(`kg.graph.note_arrivals`), read from the notes repository's own history with one `git log
--no-renames --diff-filter=A` and cached per process by `HEAD`. Every write already commits
through `kg/record.py`, and every pod's clone carries the same commits, so a commit date is a
property of the corpus where a file mtime is a property of one checkout. It stands in for a
*missing* `valid_from` and for nothing else, so no dated note changes behaviour.

Measured before building, as the row asked, on a synthetic 10,000-note corpus:

| Corpus | Full scan | Since a remembered `HEAD`, 100 new commits |
| --- | --- | --- |
| 1,000 commits of 10 notes | 2.3 s, 12.6 MB peak | — |
| 10,000 commits of 1 note | 2.9 s | **78 ms** |

So the hourly digest pays the full scan once per worker process and a range afterwards. A
remembered commit that is no longer an ancestor (a rewritten history) is read again whole. A
corpus that is not a git work tree reads as no arrivals, which is exactly the previous behaviour.

## Alternatives

- **Default `valid_from` to today in `record_knowledge_note`.** Declined: it trades a silence for
  a false claim about when chemistry became true, on the field `Note.is_current` reads.
- **Remember delivered undated ids on the subscription.** Declined: `agent/subscriptions.py`
  bounds `last_seen_note_ids` to one day on purpose (DARK-7), and an undated-id set grows with the
  corpus instead.
- **A stored arrival column (front-matter or a table).** Declined for now: the history already
  holds the fact, and a second copy is a second thing to keep true. **Revisit when:** the notes
  corpus stops being a git repository, or `note_arrivals`' full scan exceeds
  `_GIT_REVISION_TIMEOUT_SECONDS` — visible as `kg.note_arrivals_unreadable` in the background
  worker's log.

## Consequences

- **A moved note arrives again**: `--no-renames` dates the path, so a note moved between type
  directories is told once more. The right direction for a digest — told again, rather than never.
- A note deleted and re-added arrives on its re-add.
- An edit is not an arrival.
- **Dated in UTC** (`%ct`), and the watermark is read in UTC too. `%cs` — the committer's own
  offset — made a note committed at 23:30 -05:00 read as a day before a watermark set early the
  next UTC day, and it was never reported.
- **A rebased commit arrives again.** `kg/git_writer.py` replays unpushed commits with `rebase`,
  which moves the committer date, so a note a subscriber was told about before its push can be told
  once more on the day it lands. The same direction as a move: again, never silence.
- **Where it answers.** Readers scan the writer's own full clone wherever
  `CHEMCLAW_KNOWLEDGE_REPO_URL` is set (`deploy/knowledge-sync.sh`), so arrivals are read from
  real history. Without one, the pod reads an `rsync`-published shallow replica that is not a git
  work tree, gets no arrivals and behaves as before — and records no notes either, so there is
  nothing undated to miss.
- **Not changed: a dated note that arrives late.** `valid_from or arrived` still judges an
  ELN-dated note on its experiment date, so one ingested today and dated last year is not news.
  The argument above points at `max(valid_from, arrived)`; that is a change for dated notes, which
  this decision did not measure, and it is left for one that does.
- A scan that fails where `rev-parse` answered is logged as `kg.note_arrivals_unreadable`, which is
  what makes the revisit trigger above observable.

## What keeps it true

- `tests/test_note_arrivals.py::test_a_digest_reports_an_undated_note_that_arrived_after_the_watermark`
- `tests/test_note_arrivals.py::test_a_later_call_scans_only_what_arrived_since`
- `tests/test_note_arrivals.py::test_an_undated_note_is_new_on_the_day_it_arrived_and_once`
- `tests/test_note_arrivals.py::test_an_arrival_is_dated_in_utc_whatever_the_committers_offset`
