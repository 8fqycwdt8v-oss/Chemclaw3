# `src/chemclaw/durable/` — correctness pass

Files read in full: `retention.py`, `orchestrator.py`, `schedules.py`, `report_workflow.py`,
`eln_sync.py`, `memory_jobs.py`, `observation_jobs.py`, `job_record.py`, `job_record_store.py`,
`connector_job.py`, `interaction_approval.py`, `publish.py`, `notify.py`, `digest.py`,
`heartbeat.py`, `registry.py`, `background_worker.py`, `serve.py`, `note_index.py`,
`artifact_eviction.py`, `document_sync.py`, `eval_drift.py`, `template_job.py`,
`template_activities.py`. Collaborating modules read where a claim depended on them:
`agent/message_pairing.py`, `agent/session_events.py`, `agent/subscriptions.py`,
`ingest/eln/sync.py`, `ingest/eln/adapter.py`, `ingest/documents/sync.py`, `core/db.py`,
`infra/sql/014`, `infra/sql/019`.

Two findings are reproduced against the live environment (Postgres up, `uv run`). Scripts and
their verbatim output are quoted in the Evidence sections.

---

## The digest dedupe key omits the session, so two chemists watching the same query silently lose one digest each, permanently

- **Severity**: critical
- **Location**: `src/chemclaw/durable/notify.py:41` (`_dedupe_key`), consumed at
  `src/chemclaw/durable/digest.py:146-166` (`DigestWorkflow.run`); the index that enforces it is
  `infra/sql/014_session_event_dedupe.sql:10-12`
- **Trigger**: Two subscribers save the *same* standing query (`watch_for("biaryl coupling")`),
  and one `DigestWorkflow` run finds the same matching notes for both — which is the ordinary case
  for two new subscribers, since neither has a watermark yet and the query is identical. Both
  deliveries happen inside the same workflow run, so `workflow_id` and `run_id` are the same, and
  `kind` is the literal `"digest"` for both. The only thing that differs between the two calls is
  the `session_id` (`digest-alice` vs `digest-bob`) — and `_dedupe_key` does not take it.
- **Consequence**: The second `INSERT` hits
  `ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING` and vanishes. No exception is
  raised, so `notify_session_best_effort` returns `True`, so `DigestWorkflow` runs
  `acknowledge_digest` for the subscriber who received nothing. `mark_reported` then advances that
  subscription's `last_seen_at` and stores the ids it never delivered in `last_seen_note_ids`, and
  `_is_new` (`digest.py:99-121`) rejects those notes from every future run — by date if they are
  older, by the id list if they are same-day. The matches are lost for good, with no log line, no
  metric, and `delivered` reported as if both went out. This is precisely the failure the module
  docstring says the ordering exists to prevent ("a duplicate digest line is a nuisance, a missed
  one defeats the entire feature") — the ordering was fixed and the identity of the message was
  not.
- **Evidence**:

  `notify.py:41-53` derives the key from the run, the kind and a payload digest only:

  ```python
  def _dedupe_key(workflow_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> str:
      digest = hashlib.sha256(
          json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
      ).hexdigest()
      return f"{workflow_id}:{run_id}:{kind}:{digest}"
  ```

  and `notify_session` (`notify.py:79-91`) passes `session_id` as a *field* of the row while
  deriving the key without it. The index is global (`session_events_dedupe_idx UNIQUE, btree
  (dedupe_key) WHERE dedupe_key IS NOT NULL`), so the uniqueness is across all sessions.

  Reproduction (`/tmp/.../repro_digest.py`, run against the live Postgres):

  ```
  alice key: digest-scheduled-2026-08-17T00:00:00Z:run-abc-123:digest:718af0af…f448eee
  bob   key: digest-scheduled-2026-08-17T00:00:00Z:run-abc-123:digest:718af0af…f448eee
  keys identical: True
  record_session_event for digest-alice returned normally (no error raised)
  record_session_event for digest-bob   returned normally (no error raised)
  digest-alice inbox: 1 event(s) -> [{'query': 'biaryl coupling', 'note_ids': ['rxn-001', 'rxn-002']}]
  digest-bob   inbox: 0 event(s) -> []
  ```

  And the second leg — that Bob's watermark advance is terminal (`/tmp/.../repro_isnew.py`):

  ```
  note dated the day before the (phantom) delivery, re-qualifies? False
  note dated the same day,                          re-qualifies? False
  ```

  (`DigestWorkflow` is the only caller that sends several events of one kind to *different*
  sessions in one run; `ConnectorJobWorkflow` and `EvalDriftWorkflow` each use a single channel, so
  they are unaffected.)
- **Fix**: Put the recipient in the identity, since it is part of what makes the event logical:

  ```python
  def _dedupe_key(workflow_id, run_id, session_id, kind, payload) -> str:
      ...
      return f"{workflow_id}:{run_id}:{session_id}:{kind}:{digest}"
  ```

  with `notify_session` passing `session_id` through. That keeps the at-least-once retry
  idempotence the key exists for (same session, same run, same payload) and removes the
  cross-session collision. A test asserting that two different sessions in one run both receive an
  identical payload would pin it.

---

## A truncated ELN chunk advances the cursor past entries it never fetched, whenever any entry in the chunk carries a `modified_at`

- **Severity**: high
- **Location**: `src/chemclaw/durable/eln_sync.py:95-121` (`_BoundedIngest`), consumed by
  `ElnSyncWorkflow.run` at `eln_sync.py:232-263`; the cursor it fights with is
  `ingest/eln/sync.py:150,183` (`cursor = max(cursor, entry_window(...))`)
- **Trigger**: A backlog larger than `eln_sync_batch_size` (i.e. a first backfill — the case the
  chunk loop was written for) in which at least one entry inside the first chunk reports an
  amendment (`RawEntry.modified_at`) later than the creation timestamps of entries further down
  the backlog. All three shipped adapters populate `modified_at` (`json_adapter.py:150`,
  `ord_adapter.py:123`, `warehouse/adapter.py:161`), and the Snowflake binding declares it
  (`ingest/sources/eln-snowflake/datasource.yaml:45`).
- **Consequence**: `_BoundedIngest` partitions and truncates on `created_at`, while `sync_entries`
  advances the cursor on `entry_window` = `max(created_at, modified_at)`. So the chunk keeps the
  oldest-*created* L entries but returns a cursor equal to the newest *amendment* among them. The
  workflow feeds that cursor back (`source_since = chunk.summary.next_cursor`) and — on a scheduled
  run — persists it via `store_sync_cursor`. The adapter's next fetch filters on
  `entry_window >= since`, so every truncated-away entry whose window is below that amendment
  timestamp is never fetched again, by this drain or by any later scheduled run. Real experiments
  are dropped from the corpus with `rejected=0`, no warning and a green `ingested=N` log line.
  The wedge guard at `eln_sync.py:254` does not fire — the cursor *did* advance.

  The module docstring's claim is the inverse of what happens: "Because the cap applies only past
  `since`, every kept chunk that was truncated strictly advances the cursor — the loop always makes
  progress." It advances; it just advances past unread work.
- **Evidence**: Reproduction (`/tmp/.../repro_eln.py`) driving the real `_BoundedIngest` and the
  real `sync_entries` with in-memory fingerprint stores and the repo's `FakeSubmitter`, over a fake
  adapter that honours the documented contract (`return [e for e in entries if
  entry_window(e.created_at, e.modified_at) >= since]`). Six entries created Jan 1–Jan 6 2026;
  `e-1` amended on Jun 1 2026; `eln_sync_batch_size` = 2:

  ```
  chunk 1: since=0001-01-01 ingested=['e-1', 'e-2'] skipped=[] has_more=True  next_cursor=2026-06-01
  chunk 2: since=2026-06-01 ingested=['e-1']        skipped=[] has_more=False next_cursor=2026-06-01

  entries the sync ever ingested: ['e-1', 'e-2']
  entries in the source         : ['e-1', 'e-2', 'e-3', 'e-4', 'e-5', 'e-6']
  PERMANENTLY SKIPPED           : ['e-3', 'e-4', 'e-5', 'e-6']
  stored cursor after the run   : 2026-06-01T00:00:00+00:00
  ```

  The warehouse adapter makes this *more* likely, not less: it orders and limits by the watermark
  (`sql.watermark_expression`, `warehouse/adapter.py:85-100`), so an amended-long-ago row arrives in
  the page and then sorts to the *front* of `_BoundedIngest`'s `created_at` ordering — exactly the
  row above.
- **Fix**: The truncation and the cursor must agree on which timestamp they are measuring. Sort and
  partition `_BoundedIngest` on `entry_window(entry.created_at, entry.modified_at)` rather than on
  `created_at`:

  ```python
  key = lambda e: (entry_window(e.created_at, e.modified_at), e.entry_id)
  entries = sorted(await self._inner.fetch_new_entries(since), key=key)
  overlap = [e for e in entries if entry_window(e.created_at, e.modified_at) <= self._since]
  new     = [e for e in entries if entry_window(e.created_at, e.modified_at) >  self._since]
  ```

  That restores the invariant the loop actually needs — the kept chunk's max window is a lower
  bound on every truncated entry's window — and makes the docstring's progress claim true. (It also
  fixes the second half of the same mismatch: an entry amended long ago currently lands in the
  *uncapped* `overlap` bucket, so a mass re-amendment produces an unbounded chunk, defeating the
  batch bound.) A test with one amended entry inside a truncated chunk would pin it.

---

## The `job_records` search treats a chemist's `%` and `_` as SQL wildcards

- **Severity**: low
- **Location**: `src/chemclaw/durable/job_record_store.py:60-67` (`_SEARCH`) and `:131-136`
  (`read_job_record_summaries`)
- **Trigger**: `search_job_records(text="50% yield")` — a natural thing to type when looking for a
  past run — or any query containing `_`.
- **Consequence**: `pattern = f"%{text}%"` interpolates the user's text into a `LIKE` pattern with
  no escaping, so `%` becomes "any run of characters" and `_` becomes "any single character". The
  search silently returns rows that do not contain the phrase, and the model reading the result
  treats them as past runs matching the question.
- **Evidence**: against the live Postgres —

  ```
  $ psql -tAc "SELECT 'ran at 50 mmol scale, poor yield' ILIKE '%' || '50% yield' || '%';"
  t
  ```

  A run whose rationale is "ran at 50 mmol scale, poor yield" is reported as a hit for the search
  `50% yield`.
- **Fix**: escape the three LIKE metacharacters before wrapping, and declare the escape:

  ```python
  escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
  pattern = f"%{escaped}%"
  ```

  with `ILIKE %s ESCAPE '\'` in `_SEARCH` (or keep the default backslash escape, which psycopg
  passes through unchanged).

---

## `sessions_deferred` / `threads_deferred` are documented as counts but can never exceed 1

- **Severity**: low
- **Location**: `src/chemclaw/durable/retention.py:171-188` (`RetentionOutcome`),
  `:289-294` (`_prune_session_messages`), `:373-376` (`_prune_checkpoints`)
- **Trigger**: Any retention pass against a backlog larger than
  `retention_max_sessions_per_pass` (default 500).
- **Consequence**: Both selects ask for exactly `cap + 1` rows
  (`_EXPIRED_SESSIONS`/`_EXPIRED_THREADS` carry `LIMIT %s` bound to `cap + 1`), so
  `max(len(session_ids) - cap, 0)` is arithmetically bounded by 1. The model's docstring says
  `sessions_deferred` "is how many expired sessions the pass did not reach, because a cap that is
  not reported reads as 'there was nothing more': a table still growing would look bounded in every
  result this job returns" and that an operator "deciding whether to raise
  `retention_max_sessions_per_pass` needs to know which one is hitting it". The field cannot
  support either use: a backlog of 50,000 sessions and a backlog of 501 both report `1`. The
  *later* sentence in the same docstring ("Non-zero simply means the next scheduled pass has work")
  describes what the code actually does, so the two halves of one docstring disagree.
- **Evidence**: `retention.py:291-294`

  ```python
  await cur.execute(_EXPIRED_SESSIONS, (days, cap + 1))
  session_ids = [row[0] for row in await cur.fetchall()]
  deferred = max(len(session_ids) - cap, 0)          # len(session_ids) <= cap + 1
  ```

  and identically at `:374-376` for threads.
- **Fix**: either rename the fields to what they are (`more_sessions_pending: bool`,
  `more_threads_pending: bool`) and drop the count language from the docstring, or run a separate
  `SELECT count(*)` for the real backlog size when the cap is hit. The first is cheaper and matches
  every use the code actually makes of the value.

---

## Retention's per-session cap is permanently consumed by sessions it refuses

- **Severity**: low
- **Location**: `src/chemclaw/durable/retention.py:295-323` (`_prune_session_messages`),
  `_EXPIRED_SESSIONS` at `:152-156`
- **Trigger**: A session containing a `session_messages` row in neither stored shape — one for
  which `stored_call_ids` returns `None` (a payload with no `"contents"` list and no `"data"`
  dict: a truncated write, a hand-edited row, a future serialization). Its session id sorts low.
- **Consequence**: `_EXPIRED_SESSIONS` selects by `ORDER BY session_id LIMIT cap`, and the refusal
  path is a `continue` *inside* the `for session_id in session_ids[:cap]` loop — so the refused
  session consumes one of the cap's slots and, because nothing about it changes, is selected again
  in exactly the same position on every subsequent pass, forever. `cap` such sessions (default 500)
  wedge the sweep completely: `session_messages` stops being pruned for the whole deployment while
  every pass reports success. The `droppable_rows` docstring calls the refusal "self-correcting,
  because the next pass sees the same rows once somebody has looked at them" — nothing surfaces
  which sessions those are except a WARNING log line, and nothing repairs the row, so "somebody has
  looked at them" is not a thing that happens on its own.
- **Evidence**: `retention.py:295-315` — the `continue` at `:315` is reached after the session has
  already been counted against `session_ids[:cap]`; the selection at `:152-156` has no exclusion
  and no ordering that would rotate past a stuck session. `deferred` (see the finding above) cannot
  reveal it either, since it saturates at 1.
- **Fix**: order the batch so a refused session cannot pin the window — e.g. select the oldest
  expired rows' sessions (`ORDER BY min(created_at)`) and, in the same pass, count refusals so the
  outcome distinguishes "nothing left to prune" from "the cap is full of sessions I will never
  prune". Adding a `refused_sessions: list[str]` to `RetentionOutcome` also gives the operator the
  ids the log line currently truncates to ten.

---

## Checked and found sound (through the correctness lens)

Recording these so the absence of a finding is a result rather than a gap:

- `heartbeat.beating` — the `finally` cancels *and awaits* the task on every exit path, including
  the non-`CancelledError` ones; the interval floor `max(1.0, timeout/4)` is real and matches the
  comment.
- `connector_job.failure_reason` — the loop stops at the first non-structural frame, which is what
  the docstring says (it does not walk to the innermost cause).
- `connector_job.child_workflow_id` — `run_id` genuinely differs across executions, so
  `REJECT_DUPLICATE` plus `ALLOW_DUPLICATE_FAILED_ONLY` compose as described.
- `artifact_eviction._EVICT_TO_FIT` — `ROWS UNBOUNDED PRECEDING` includes the current row, so
  `cumulative > ceiling` keeps exactly the prefix that fits; `last_access_at` is `NOT NULL`
  (019), so the `GREATEST(NULL, 1.0)` trap is not reachable.
- `memory_jobs._slice_for_this_run` — the daily rotation `(ordinal * cap) % n` does step by `cap`
  each day and does cover the corpus, as claimed (windows start at multiples of `gcd(cap, n) <=
  cap`).
- `observation_jobs.promote_observations_activity` — the propose-then-`set_status` order is
  retry-safe: a retry re-reads `promotable()`, which excludes what was already marked.
- `document_sync` — the sweep refusal reads `has_more`/`failed_roots`/`scanned` off the drain's own
  merged report, and `merge_reports` takes `has_more` from the last chunk, so the post-
  `continue_as_new` case still evaluates correctly.
- `session_events.claim_unconsumed` — the single `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` plus
  the client-side re-sort by id is correct for the at-most-once contract it claims.
- No chemistry arithmetic remains in this slice: after the physics moved to `Chemclaw3-mcp`, the
  only numbers computed here are runtimes, byte counts and row counts.
