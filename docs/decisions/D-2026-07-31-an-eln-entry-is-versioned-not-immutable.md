# D-2026-07-31-an-eln-entry-is-versioned-not-immutable — An ELN entry is versioned, not immutable

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-018 (ELN ingestion), D-005 (the
PR-gate)

## Context

`ingest/eln/sync.py` skipped any entry whose note id was already merged, with no content
comparison, and said why: *"ELN exports are immutable (the overlap window's premise)."*

That is an assumption about someone else's system, and it is false of every ELN in practice. Entries
are amended **in place** — a yield corrected after assay, an impurity added to the profile, an entry
retracted — while `created_at` stays put. Two things followed, and the second is worse:

1. The sync's check was on the note *id*, so a corrected entry was reported as
   `skipped_existing` — indistinguishable in the summary from a genuine no-op replay.
2. The adapters filtered on `created_at`, so an entry amended after the overlap window closed was
   **never fetched again at all**. The correction could not be dropped by the sync because the sync
   never saw it.

So a yield revised from 82% to 31% after assay stayed 82% in the graph, permanently, with no
rejection, no counter and no log line.

## Decision

**An entry is identified by its id and versioned by its content.** Two changes, one at each layer.

**The fetch window widens.** `RawEntry` gains `modified_at`, and `entry_window(created, modified)`
— one definition, so two adapters cannot disagree — returns the later of the two for the `since`
comparison. Both file adapters read it: the JSON adapter from a `modified` field, the ORD adapter
from `provenance.record_modified`, which ORD models as a list, so the newest member decides.
`modified_at` is optional and `None` means "this source does not report amendments", not "never
amended" — for those sources the overlap replay remains the only thing that catches a change, which
is what it was already doing.

**The sync compares bodies, not ids.** `_merged_note_bodies` replaces `_merged_note_ids`, and an
entry is skipped only when the note it would produce is byte-identical to the one already merged.
Anything else is re-proposed.

**An amendment is a re-proposal, not a new note version.** This is the choice worth arguing. The
note id is stable (`reaction-<entry_id>`), so a corrected entry re-proposes the *same* note with
different content — and the PR-gate renders that to a reviewer as a **diff**, which is exactly what
a git-backed knowledge graph is for. A separate versioning scheme (mint `reaction-<id>-v2`, set
`valid_to` on the old one) would add a second mechanism to express what git already expresses, and
would leave the graph carrying two notes for one experiment. D-2026-07-31-a-proposal-is-a-record
already keeps every submitted version's content in `note_proposals`, so the history survives the
file being replaced.

## Consequences

A correction reaches the graph, through the same human review as everything else. The reviewer sees
what changed rather than a fresh note.

**Comparison is on the note body, normalized.** Reading a note back through `kg.note.read_note`
drops the rendered file's trailing newline, so an exact comparison would have called every merged
note amended and re-proposed the entire corpus on the first overlap replay. Trailing whitespace is
not an amendment.

**A source that stamps a modification time on every export costs nothing extra**, because the check
is on content, not on the presence of a timestamp. That failure would have been louder and more
continuous than the one being fixed, so it is pinned by its own test.

**Source provenance is structured in the same change**, because it is the other half of "which
record is this": `eln:<operator>` became `eln-json:<entry_id>:<operator>`. With two ELN sources
enabled, colliding `entry_id`s produce the same note id and the second silently loses to the
already-merged check — with nothing in either record to say they came from different systems. The
adapter names the *format* it reads rather than an instance, which is as much as a file-drop
adapter can honestly claim: it is handed a directory, not a tenant. A connector talking to a real
ELN knows its instance and should say so.

**Retraction is not implemented.** A withdrawn entry that simply disappears from the export is
invisible to a sync that only ever reads what is present — noticing absence needs a full
reconciliation pass, and treating a missing file as a retraction would make an export glitch
indistinguishable from a withdrawal. That wants its own decision, and the backlog row now says so
rather than implying the amendment work covered it.
