# D-2026-08-07-the-mark-means-observed-not-processed — the sweep reads the drain's own evidence

**Status:** accepted

## Context

`D-2026-08-06-a-share-is-mounted-not-called` shipped the mounted-share corpus with a mark-and-sweep
deletion and one rule, stated at the top of `ingest/documents/sync.py`: *nothing is deleted on
doubt*, because "an unreachable share and an empty one look identical, and of the two possible
mistakes re-indexing is recoverable and deleting is not."

An adversarial review of that code found the rule was honoured at **root** granularity and nowhere
else. Five paths dropped a file from a pass without the pass ever admitting it was incomplete, and
the sweep then deleted the row for a file that was **present and readable on the share**. Each was
reproduced as a failing test before it was fixed.

Two root causes, both about a word.

**1. "Mark" had come to mean *successfully processed*, when it must mean *observed to exist*.**
`sync_share` restamped only the files whose fingerprint matched. A file the walk *saw* but could not
`stat` (an ACL push, a DFS referral flap, an `ESTALE` on reconnect — routine on CIFS) was skipped
with a log line and no mark. A file whose fingerprint had moved but which then failed to open — a
document a chemist has open in Word, which is the single commonest state on a live share — was
neither marked nor upserted. Both had rows; both got swept; neither file had gone anywhere. The
comment in `_parse_changed` argued carefully about *not storing a fingerprint* and did not notice
that the same choice handed the row to the sweep.

**2. The sweep's guard was a boolean each caller derived, so each caller could derive it wrong —
and one did.** `prune_share(..., crawl_was_complete: bool)` trusted its caller. The durable workflow
computed it from a sticky `degraded` flag that caught a wedged drain; the CLI, whose docstring calls
itself "the CLI mirror of the durable workflow", computed `not merged.failed_roots` and dropped
exactly the check that made the workflow safe. `python -m chemclaw.cli.sync_share sharedrive
--limit 0` — accepted by argparse — therefore scanned nothing, reported "more to come", and deleted
every row for the source. That is the same defect shape as
`D-2026-08-05-one-rule-in-three-places-is-three-rules`.

A third, separate defect made the first two reachable far more often than they look. The walk sorted
siblings by bare `name` while `after` compared whole joined paths, and those are different orders:
`"."` (0x2E) sorts below `"/"` (0x2F), so `Report` (a directory) precedes `Report.txt` by name while
`Docs/Report.txt` precedes `Docs/Report/a.txt` by path. Any resumed drain that stopped inside
`Report/` skipped `Report.txt` **on every later pass**, and the sweep — seeing a complete crawl with
no failed roots — deleted it. Sibling *roots* did it too: `Data` and `Data-Archive` pass every
binding check and sort the opposite way from the paths they yield, so a resume dropped the whole
second root. The comment at `crawl_share` claimed this exact case was handled.

## Decision

### The mark means "observed to exist"

`sync_share` restamps everything the walk saw: fingerprint matches, entries that failed to `stat`
(carried on the new `CrawlResult.unreadable`), and files that were opened and refused (returned from
`_parse_changed`). Their fingerprints are still not stored — that is deliberate and unchanged, so a
refusal stays visible in `skipped_scan`/`skipped_unreadable` on every run instead of reading zero
after the first. What changes is only that their *existence* is recorded, so the sweep leaves the
rows they already had alone.

This costs nothing and needs no new failure mode: `touch` is by path, so touching a path with no row
is a no-op.

### `prune_share` takes the drain's merged report, not a boolean

The report already carries every fact the decision needs, so the caller hands over evidence rather
than a conclusion. Three refusals, each with the reason named in the log line:

- **a root failed to walk** — half a share is not a share;
- **the drain never finished** (`has_more` still set) — it stopped early or wedged, so the unvisited
  tail is unmarked and would sweep wholesale;
- **it saw no candidates at all** — a detached CIFS volume leaves its mount point behind as an empty
  directory, and with `roots: [{path: "."}]` there is no missing root to notice. A genuinely empty
  share keeps stale rows until it has a file again, which is the harmless half of the trade.

`DocumentSyncState.degraded` is **deleted**. It was a second copy of the rule living beside the
evidence, and the copy the CLI kept was the wrong one; whether a drain may sweep is now read off the
merged report in both callers. `_merge_by_source` already unions `failed_roots` and carries
`has_more`, so the guard survives `continue_as_new` — and `tests/test_document_share.py` now pins
that, because it is the property that replaced the flag.

### The walk sorts on the separator

Directories are keyed `name + "/"`, roots as `path + "/"`, so sibling order and joined-path order are
the same order. `--limit` is refused below 1.

## Consequences

- Six reproductions became regression tests, and every one failed before the fix. The suite went from
  36 to 38 tests in this file.
- One breaking signature inside the package: `prune_share`'s fourth parameter is a `SyncReport`, and
  the `prune_document_share` activity's third argument with it. Both are days old with two callers.
- A share that is legitimately empty now retains its rows. Stated rather than hidden: it is the same
  trade the module's opening paragraph already makes, applied to one more case.
- The three prose claims that asserted properties the code did not have are corrected, not deleted —
  each now states the ordering rule, the meaning of the mark, and why the guard reads evidence.

## Alternatives rejected

**Keep the boolean and fix the CLI.** The narrowest possible change, and it leaves the structure that
produced the defect: two callers deriving one safety rule, with nothing making the next caller derive
it correctly. The evidence is already computed and already merged; passing it is less code, not more.

**Treat a failed `stat` or a failed read as a degraded run.** Correct but far too blunt — one locked
Word document on a 500k-file share would suspend deletion for the whole corpus indefinitely, and
"the sweep silently stopped working months ago" is its own silent failure. Marking the file as seen
is exact: it says the one true thing that is known about it.

**Compare `after` on tuples of path segments instead of sorting on the separator.** Equivalent, and
it moves the cost from one sort key to every comparison in the walk while making the cursor no longer
a plain string — the cursor is persisted in workflow state and printed in reports.
