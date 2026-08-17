# `durable/` — correctness pass, reachability/consequence verification

Lens: **is the trigger reachable, and is the consequence what is claimed?** In scope: the two
findings marked critical/high. The three `low` findings are out of scope and were not examined.

Working tree was not mutated. `diff -rq` of `src/chemclaw/durable/` against the pristine `HEAD`
copy showed no differences before I started; all experiments ran from `/tmp`.

---

## The digest dedupe key omits the session, so two chemists watching the same query silently lose one digest each, permanently

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Reproduced the whole chain against the live Postgres (`infra-postgres-1`, up), in a throwaway
schema, driving the real `_dedupe_key`, the real `record_session_event`, the real
`claim_unconsumed` and the real `_is_new`, with the exact payload shape `DigestWorkflow.run`
builds (`{"query": ..., "note_ids": ...}`, kind `"digest"`, one workflow run):

```
$ uv run python /tmp/repro_digest_v.py
alice key: digest-scheduled-2026-08-17T00:00:00Z:run-abc-123:digest:718af0af…f448eee
bob   key: digest-scheduled-2026-08-17T00:00:00Z:run-abc-123:digest:718af0af…f448eee
keys identical: True
insert for digest-alice: returned normally
insert for digest-bob:   returned normally (no exception)
digest-alice inbox: 1 [{'query': 'biaryl coupling', 'note_ids': ['rxn-001', 'rxn-002']}]
digest-bob   inbox: 0 []
phantom-delivered note dated the day before, re-qualifies? False
phantom-delivered note dated the same day,   re-qualifies? False
```

Then traced the two ends the finding does not:

```
$ grep -rn '"digest"' --include=*.py src/
src/chemclaw/durable/digest.py:148
src/chemclaw/durable/schedules.py:89,132          # the prune namespace + the PlannedSchedule
$ grep -rn "stream_new_events\|claim_unconsumed" --include=*.py src/
… src/chemclaw/api/routes/streams.py:111          # the only consumer in the product
$ grep -rn "digest_enabled" --include=*.py src/
src/chemclaw/core/config/memory.py:134:    digest_enabled: bool = False
$ grep -rn DIGEST_ENABLED .env.example
.env.example:934:CHEMCLAW_DIGEST_ENABLED=false
```

`src/chemclaw/api/routes/streams.py:111-113` is the sole tailer, and its claim is scoped in SQL:

```python
async for pushed in front_door.stream_new_events(
    session_id, kinds=("job_completed", "job_failed")
):
```

### Why

The **mechanism is exactly as reported and reproduces on the first try** — `_dedupe_key`
(`notify.py:41`) derives identity from `workflow_id:run_id:kind:sha256(payload)` while
`notify_session` passes `session_id` only as a row *field*; the index
(`infra/sql/014_session_event_dedupe.sql:10-12`) is global over `dedupe_key`; the second insert is
swallowed by `ON CONFLICT … DO NOTHING` with no exception, so `notify_session_best_effort` returns
`True` and `acknowledge_digest` runs for a subscriber who got nothing. The watermark advance is
terminal: `_is_new` rejects the phantom-delivered notes both by date and by
`last_seen_note_ids`. None of that is in dispute, and the reporter under-states one part — the
collision is not limited to "two new subscribers with no watermark". The payload is
`{"query", "note_ids"}`, so the *steady state* collides too: Alice and Bob both caught up, one new
note lands, both digests carry `note_ids=["note-X"]`, identical payload, Bob loses it. That is the
common case, not the edge case.

What does not hold is the **consequence as stated in the title**, and it is what the severity rests
on. "Two chemists … lose one digest each" implies one of them receives theirs. Neither does.
`kind="digest"` has **no consumer anywhere in this repo**. The only surface that reads
`session_events` is `GET /sessions/{session_id}/events`, and it scopes its *destructive*
`claim_unconsumed` to `("job_completed", "job_failed")` in the SQL itself — deliberately, per its
own docstring, because the claim is at-most-once. A digest row is therefore never claimed, never
rendered, and sits in `session_events` until `leaver.py`/retention deletes it. Alice's "delivered"
digest reaches a chemist exactly as often as Bob's dropped one: never. Nothing in the CLI reads it
either (`grep session_events src/chemclaw/cli/` is empty), and `tests/test_digest.py` only exercises
`collect_digests`/`_is_new` — there is no end-to-end delivery test because there is no end-to-end
delivery.

On top of that the job is **off by default and gets no Schedule**: `digest_enabled: bool = False`,
`.env.example` ships `false`, and `planned_schedules()` appends `PlannedSchedule("digest", …)` only
inside `if settings.digest_enabled`. So reaching the defect at all takes an operator opt-in into a
feature that then delivers nothing to anyone.

So the real, present-day consequence is narrower than reported: an operator who enables the digest
silently corrupts the `subscriptions` watermark for the losing subscriber (permanent, proven above),
in a pipeline whose output no chemist can see. That is a genuine latent data-state defect and it
must be fixed before a consumer is wired up — the proposed fix is right and cheap — but it is not
critical today, because "critical" implies a chemist is being told something wrong or is missing
knowledge they would otherwise have, and here the delivery leg does not exist. **Medium.** The day a
surface claims `kind="digest"`, re-rate it high.

One correction to the finding's parenthetical: `DigestWorkflow` is not merely "the only caller that
sends several events of one kind to different sessions in one run" — it is the only caller of a
`kind` that nothing consumes, which is why the collision has never been observed.

---

## A truncated ELN chunk advances the cursor past entries it never fetched, whenever any entry in the chunk carries a `modified_at`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (agreed)

### What I did

Rebuilt the reproduction independently rather than re-running the reporter's script: real
`_BoundedIngest`, real `sync_entries`, real `JsonExportAdapter.map_to_ord`, real
`InMemoryFingerprintStore`, the repo's `FakeSubmitter`, and `ElnSyncWorkflow.run`'s chunk loop
transcribed verbatim including the wedge guard. The fake source honours the shipped file adapters'
contract exactly (`entry_window(created, modified) >= since`, ordered by `created_at` — see
`json_adapter.py:145,157`). Six entries created Jan 1–6 2026, `e-1` amended 2026-06-01,
`eln_sync_batch_size = 2`:

```
$ uv run python /tmp/repro_eln_v.py
chunk: since=0001-01-01 ingested=['e-1', 'e-2'] rejected=[] has_more=True  next_cursor=2026-06-01
chunk: since=2026-06-01 ingested=['e-1']        rejected=[] has_more=False next_cursor=2026-06-01

entries the sync ever ingested: ['e-1', 'e-2']
entries in the source         : ['e-1', 'e-2', 'e-3', 'e-4', 'e-5', 'e-6']
PERMANENTLY SKIPPED           : ['e-3', 'e-4', 'e-5', 'e-6']
stored cursor after the run   : 2026-06-01T00:00:00+00:00
adapter fetch `since` values  : ['0001-01-01', '2026-06-01']
```

And the second half of the same mismatch, which the finding only mentions inside its Fix section —
the uncapped `overlap` bucket — with the shipped `eln_sync_batch_size` of 100:

```
$ uv run python /tmp/repro_overlap_v.py
eln_sync_batch_size            : 100
entries handed to one attempt  : 1000
bounded.truncated              : False
```

Reachability trace, outermost entry point inward:

```
$ grep -n "PlannedSchedule(\"eln-sync\"" -B4 src/chemclaw/durable/schedules.py
    schedules = [ PlannedSchedule("eln-sync", ElnSyncWorkflow, eln_every), …   # unconditional
$ grep -n "data_sources:" src/chemclaw/core/config/sources.py
45:    data_sources: str = "graph,eln-json"
$ grep -n "eln_sync_batch_size" src/chemclaw/core/config/eln.py
42:    eln_sync_batch_size: int = Field(default=100, ge=1)
$ grep -n "modified" src/chemclaw/ingest/eln/{json_adapter,ord_adapter,warehouse/adapter}.py
json_adapter.py:141,145,150 · ord_adapter.py:109,118,123 · warehouse/adapter.py:161-162
$ grep -n "ORDER BY" src/chemclaw/ingest/eln/warehouse/sql.py
79:        f"ORDER BY {watermark} ASC "     # watermark = COALESCE(modified, created)
$ grep -n "fetch_limit" src/chemclaw/ingest/sources/eln-snowflake/datasource.yaml
47:        fetch_limit: 500
```

### Why

Every link the finding claims holds, and nothing upstream blocks any of them.

**Trigger is reachable from the shipped default configuration, with no opt-in.** Unlike the digest,
`PlannedSchedule("eln-sync", ElnSyncWorkflow, …)` is in the unconditional part of
`planned_schedules()`, and `eln-json` is in the default `data_sources`. There is no validator,
pydantic constraint, manifest schema or startup guard between an export directory and this loop —
`RawEntry.modified_at` is an ordinary optional field, and all three shipped adapters populate it
when the source carries it. The only precondition is a backlog above `eln_sync_batch_size` (100)
with one amendment inside the kept prefix, which is precisely the first-backfill case the chunk
loop was written for.

**The mechanism is a straight disagreement between two lines.** `_BoundedIngest.fetch_new_entries`
partitions and truncates on `entry.created_at` (`eln_sync.py:116-121`); `sync_entries` advances on
`cursor = max(cursor, entry_window(raw.created_at, raw.modified_at))` (`sync.py:150,183`). The kept
chunk's max *window* is therefore not a lower bound on the truncated entries' windows, which is the
invariant the loop's progress argument silently assumes. The module docstring at
`eln_sync.py:103` ("every kept chunk that was truncated strictly advances the cursor — the loop
always makes progress") is true and irrelevant: it advances past unread work. That docstring is
a claim, and the run above is the check.

**Consequence is as stated, not a worse-sounding paraphrase.** I verified each part rather than
taking it: `has_more` is `True` on the poisoning chunk so the workflow does *not* stop; the wedge
guard at `eln_sync.py:254` (`chunk.summary.next_cursor <= source_since`) does not fire because the
cursor genuinely moved; `store_sync_cursor` persists the poisoned value on the scheduled path; the
returned `IngestSummary` carries `rejected=[]`; and the next fetch, at `since = 2026-06-01`, does
not return the skipped entries. The 1-day `eln_sync_overlap_seconds` rewind recovers only entries
whose window is within a day of the poisoned cursor — an amendment more than a day newer than the
truncated entries' timestamps loses them outright. Recovery exists only as a manual backfill with
an explicit `since`, which nobody knows to run because nothing reports the gap.

Two things to add that make it slightly worse, and one that makes it slightly less silent:

- **The batch bound is defeated in the other direction too**, and this is a distinct reachable
  defect that the finding buries in a parenthetical. Any entry with `created_at <= since` lands in
  the `overlap` list, which is returned *uncapped*. A bulk re-amendment upstream (a metadata
  migration, a status backfill) therefore hands one activity attempt the entire amended set —
  measured above: 1000 entries into a 100-entry bound, with `truncated=False`, so the workflow sees
  a well-behaved terminal chunk while the activity blows through `eln_sync_timeout_seconds` and
  retries forever. That is the exact wedge the chunk loop was built to prevent.
- **The warehouse adapter is the worst case, and for the reason the finding gives.** Its page is
  `ORDER BY COALESCE(modified, created) ASC LIMIT fetch_limit`, with `fetch_limit` defaulting to
  500 against a batch size of 100 (and the binding *requires* it to exceed the batch size for the
  drain to progress). So the page routinely holds 500 rows whose watermarks span a wide range, and
  `_BoundedIngest` then re-sorts them by `created_at` — an old row amended recently sorts to the
  front, is kept, and its amendment becomes the cursor for the 400 rows just truncated away. There
  is no late-arrival signal on this adapter at all.
- **For the two file adapters there is one partial signal the finding says does not exist.** On the
  *next* run, the skipped export files fall into the `elif is_late_arrival(path, since)` branch
  (`json_adapter.py:154`, shared by `ord_adapter.py`) and produce one aggregated WARNING naming up
  to ten of them. But it fires only when the file's mtime happens to be at or after the poisoned
  cursor — true for files copied in during a bulk backfill, false for an export directory appended
  to in real time, which is the steady-state deployment. So it is a coin-flip diagnostic, not a
  guard, and the durable `IngestSummary` an operator actually reads still says `rejected=0`.

High is the right severity: silent, permanent loss of real experiments from the knowledge corpus on
a default-enabled scheduled path, invisible in the job's own report. I would not push it to critical
— nothing here produces a *wrong* answer to a chemist; it produces an incomplete corpus, and the
retrieval layer's citations will simply not include the missing runs. The proposed fix (partition
and sort `_BoundedIngest` on `entry_window`) is the correct one and repairs both halves.
