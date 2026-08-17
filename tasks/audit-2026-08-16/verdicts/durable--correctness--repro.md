# `durable/` — correctness pass, reproduction verdicts

Lens: *does it actually reproduce?* Two findings are in scope (critical + high). Both were
re-derived from source with scripts written from scratch (`/tmp/verif/repro1.py`,
`/tmp/verif/repro2.py`, `/tmp/verif/repro2b.py`) — the reporter's scripts were never run, and
neither was any test in `tests/`. Postgres (`infra-postgres-1`) and the Temporal time-skipping
server were live; `uv run` throughout.

---

## The digest dedupe key omits the session, so two chemists watching the same query silently lose one digest each, permanently

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (one notch below the filed `critical` — see the caveats at the
  end; the mechanism itself is not in doubt)

- **What I did**

  I did not hand-compute the key. I ran the **real `DigestWorkflow`** on Temporal's time-skipping
  server with the three real activities (`collect_digests`, `record_session_event_activity`,
  `acknowledge_digest`), against the **live Postgres** (real `subscriptions` and `session_events`
  tables) and the **real `knowledge/` corpus**, with `settings.background_task_queue` pointed at the
  test queue. Two owners, one identical standing query:

  ```
  $ uv run python /tmp/verif/repro1.py
    verif-alice: sub id=1 matches=['bo-suzuki-next', 'campaign-biaryl-scope', ... 9 ids]
    verif-bob:   sub id=2 matches=['bo-suzuki-next', 'campaign-biaryl-scope', ... same 9 ids]
  workflow reported delivered = 2
    inbox digest-verif-alice: 1 event(s) -> [{'query': 'biaryl coupling', 'note_ids': [...9 ids]}]
    inbox digest-verif-bob:   0 event(s) -> []
    watermark: ('verif-alice', 2026-08-17 08:42:48.027139+00, [...9 ids])
    watermark: ('verif-bob',   2026-08-17 08:42:48.063723+00, [...9 ids])
    row: ('digest-verif-alice', 'digest',
          'verif-digest-1:12ed71f9-…:digest:dfc2f22207162965f220019f441b32f9d4f322b64f59f931523a65e8bf73486a')
  ```

  One row in `session_events` for a run that "delivered = 2". Bob's mailbox is empty and Bob's
  watermark advanced over the nine ids he was never sent. (Test rows cleaned up afterwards; no
  source file was modified.)

  Second leg — is the advance terminal? The seed corpus is entirely `valid_from = None`, and
  `_is_new` returns `True` unconditionally in that case, so on *that* corpus Bob recovers next run.
  On the corpus that actually matters it does not: `ingest/eln/note.py:54` sets
  `valid_from=reaction.performed_at`, so every ELN-ingested note is dated. Against a dated note and
  Bob's just-advanced watermark:

  ```
  dated yesterday : False
  dated today     : False
  undated         : True
  ```

  So for the real (dated) notes the rejection is permanent, exactly as filed.

- **Why**

  The cited code is real and current. `notify.py:41` derives the key from
  `f"{workflow_id}:{run_id}:{kind}:{digest}"` with no `session_id`; `notify.py:86` passes
  `session_id` as a *field* of `SessionEventInput` while computing the key without it;
  `infra/sql/014_session_event_dedupe.sql:10-12` makes the index global
  (`UNIQUE (dedupe_key) WHERE dedupe_key IS NOT NULL`), and `session_events.py:36-39` inserts
  `ON CONFLICT (dedupe_key) … DO NOTHING`, which is silent. `digest.py:146-150` sends the payload
  `{"query", "note_ids"}` — neither `subscription_id` nor `owner`, both of which `DigestItem`
  carries — so two subscribers whose query and match set coincide produce a byte-identical payload
  and therefore an identical key. `DO NOTHING` raises nothing, so `notify_session_best_effort`
  returns `True`, `acknowledge_digest` runs, and `mark_reported` advances the watermark. Every link
  in the chain executed as described.

  The trigger is not exotic: two brand-new subscribers with no watermark and the same query is the
  base case, and it is the one I ran. It also fires for any two subscribers whose watermarks and
  match sets have converged.

  Three qualifications, none of which break the finding:

  1. `settings.digest_enabled` defaults to **False** (`core/config/memory.py:134`) and
     `schedules.py:130` only plans the schedule when it is on, so the bug is dormant until a
     deployment turns the feature on. The finding does not mention this.
  2. The lost thing is a *notification*, not knowledge — the notes stay in the graph and stay
     findable by search. That is the difference between this and the ELN finding below, and it is
     why I put this at high rather than critical.
  3. The title says both chemists "lose one digest each". In the run above Alice received hers and
     Bob lost his: with N colliding subscribers, one is delivered and N-1 vanish.

  One thing the reporter missed that makes it slightly worse: `delivered` is the workflow's return
  value, so the *only* number an operator sees is the count of subscribers whose watermark advanced,
  which is precisely the count that is wrong. There is no counter, log line or metric anywhere on
  the swallow path — I confirmed by reading `record_session_event`, which does not inspect the
  rowcount.

---

## A truncated ELN chunk advances the cursor past entries it never fetched, whenever any entry in the chunk carries a `modified_at`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)

- **What I did**

  I did not use a hand-written fake adapter. I ran the **real `ElnSyncWorkflow`** on the
  time-skipping server over the **shipped `JsonExportAdapter`** reading a temp export directory,
  with the real `_BoundedIngest`, the real `sync_entries`, in-memory fingerprint stores, the repo's
  `FakeSubmitter`, an in-memory cursor store, an empty `knowledge_dir`, and
  `eln_sync_batch_size = 2`. Six entries created 2026-01-01…01-06; the *oldest-created* one
  (`eln-2026-101`) carries `"modified": "2026-06-01T00:00:00Z"`.

  ```
  $ uv run python /tmp/verif/repro2b.py
      [store_sync_cursor] eln-json -> 2026-06-01T00:00:00+00:00
      [store_sync_cursor] eln-json -> 2026-06-01T00:00:00+00:00
  workflow summary.ingested : ['eln-2026-101', 'eln-2026-101', 'eln-2026-102']
  workflow summary.rejected : []
  workflow next_cursor      : 2026-06-01T00:00:00+00:00
  stored cursor             : 2026-06-01 00:00:00+00:00
  entries in the source     : ['eln-2026-101', …, 'eln-2026-106']
  NEVER INGESTED            : ['eln-2026-103', 'eln-2026-104', 'eln-2026-105', 'eln-2026-106']
  ```

  Four of six real experiments dropped, `rejected=0`, the workflow completing successfully. The
  chunk-2 fetch (`since = 2026-06-01`) returns only `eln-2026-101`, so `has_more` is False and the
  drain loop exits believing it is done; the wedge guard at `eln_sync.py:254` never fires because
  the cursor *did* advance.

  I ran the same scenario twice on purpose. In `/tmp/verif/repro2.py` the export files had
  just-written mtimes, and the shipped adapter's `is_late_arrival` path emitted
  `"5 export file(s) arrived after the sync cursor but carry an older timestamp"` — which would have
  been a partial refutation of the finding's "no warning". So I backdated the file mtimes to their
  honest January values (`repro2b.py`), which is the realistic case, and **the warning disappears
  entirely** while the loss is identical. The finding's "no warning, no metric" claim therefore
  holds where it counts; and it holds unconditionally for the warehouse adapter, which has no file
  mtime to test.

- **Why**

  Every cited line is real and current. `eln_sync.py:114-121` sorts, partitions and truncates on
  `entry.created_at`:

  ```python
  entries = sorted(await self._inner.fetch_new_entries(since),
                   key=lambda entry: (entry.created_at, entry.entry_id))
  overlap = [entry for entry in entries if entry.created_at <= self._since]
  new     = [entry for entry in entries if entry.created_at >  self._since]
  self.truncated = len(new) > self._limit
  return overlap + new[: self._limit]
  ```

  while `ingest/eln/sync.py:150,183` advances the cursor on `entry_window` = `max(created_at,
  modified_at)`, and `eln_sync.py:248,263` feeds that cursor straight back into the next chunk and
  into `store_sync_cursor`. The two halves measure different timestamps, so the invariant the
  chunk loop needs — *the kept chunk's max is a lower bound on every truncated entry* — is simply
  not established.

  Reachability is not theoretical. The adapter contract (`ingest/eln/adapter.py:131-143`) *requires*
  `fetch_new_entries` to filter on `entry_window`, and both file adapters do
  (`json_adapter.py:145`, `ord_adapter.py:118`) and both populate `modified_at`
  (`json_adapter.py:150`, `ord_adapter.py:123`). The comment block at `sync.py:141-149` shows the
  cursor was *deliberately* moved onto `entry_window` to fix a different wedge — so this is not a
  stray inconsistency, it is one fix landing on a different timestamp from the one the bounding
  layer uses.

  The module docstring at `eln_sync.py:103` ("every kept chunk that was truncated strictly advances
  the cursor — the loop always makes progress") is a false claim about the code, exactly as the
  finding says. It advances; it advances past unread work.

  Nothing upstream prevents it: `has_more` is computed from `created_at` truncation, so the
  workflow's own progress guard has no view of the mismatch, and `rejected` stays empty because
  nothing was rejected — the entries were never fetched.
