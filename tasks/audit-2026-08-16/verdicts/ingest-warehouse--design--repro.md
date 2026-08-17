# Adversarial verification — `ingest-warehouse--design.md`, lens: does it reproduce?

Scope: the two findings marked **high**. The other four are medium/low and were not examined.
All scripts below are mine (`/tmp/av1`, `/tmp/av2`); I did not run the reporter's `/tmp/wh/*`.
All five cited source files are byte-identical to `HEAD` (`diff <(git show HEAD:<path>) <path>`),
so nothing here is an artefact of another agent's working-tree edit.

---

## The two file-drop adapters' `fetch_new_entries` are a 30-line clone, and it has already diverged

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (if anything a notch understated — see the last paragraph)
- **What I did**

  Wrote `/tmp/av1/repro1.py` from the source, not from the finding: for each adapter, a directory
  holding `a_bad.json` (valid JSON, encoded **latin-1** — a `0xe9` inside an operator name) and
  `b_good.json` (clean UTF-8, sorting after it), then `await Adapter(dir).fetch_new_entries(2020-01-01)`.

  ```
  $ uv run python /tmp/av1/repro1.py
  --- json: bytes of a_bad.json contain 0xe9: True
  json_adapter -> RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 68: invalid continuation byte
     the good file after it was never read
  --- ord: bytes of a_bad.json contain 0xe9: True
  LOG WARNING skipping unreadable ORD export a_bad.json: 'utf-8' codec can't decode byte 0xe9 in position 131: invalid continuation byte
  ord_adapter -> OK, entries: ['good']
  ```

  Reachability: `src/chemclaw/ingest/sources/eln-json/datasource.yaml:16` declares
  `ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter`, so this is a wired production half,
  not a test-only class.

  The regression test asymmetry checks out: `tests/test_eln.py:1680`
  (`test_one_non_utf8_ord_export_does_not_abort_the_directory`, latin-1 payload at line 1698) covers
  the ORD copy; `grep -rn "latin\|UnicodeDecode" tests/` returns no counterpart for the JSON one.

- **Why**

  Every line number and symbol in the finding is real and current. `json_adapter.py:142` catches
  `(OSError, json.JSONDecodeError, ElnFormatError)`; `ord_adapter.py:115` catches
  `(OSError, UnicodeDecodeError, json.JSONDecodeError, OrdFormatError)`. `UnicodeDecodeError` is a
  `ValueError` and `json.JSONDecodeError` is its sibling, so the JSON adapter's tuple genuinely does
  not cover it — reproduced above, with the good file never reached. The docstring at
  `json_adapter.py:119-124` ("one broken export file must not abort the whole fetch") is false on
  this input.

  **What the finding missed, and it makes it worse.** The escaping `UnicodeDecodeError` is not caught
  by `sync_entries` either (its `except` at `sync.py:217` wraps `map_to_ord`, not the fetch), so it
  leaves the `sync_eln_entries` activity. `durable/publish.py:32` lists `_BAD_DATA_TYPES` by *exact
  class name* — `"ValueError"` is there, `"UnicodeDecodeError"` is not — and Temporal matches
  non-retryable types by exact name, so the failure is treated as **retryable**: it burns
  `activity_max_attempts` and then fails `ElnSyncWorkflow`. Because the workflow drains sources in a
  `for source in sources` loop (`durable/eln_sync.py:222`), one latin-1 file in the `eln-json`
  directory also stops every source ordered after it in that run.

---

## `warehouse/adapter._optional_timestamp` silently drops an unparseable `modified_at`, wedging the sync cursor forever

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**

  1. The parse itself (`uv run python -c ...`): `parse_iso_utc('01/08/2026 12:00')` raises
     `ValueError: Invalid isoformat string`, and `warehouse.adapter._optional_timestamp` on the same
     string returns `None`. So the swallow at `adapter.py:397-398` is real.

  2. Wrote my own fake warehouse (`/tmp/av2/fakewh.py`) that honours the statement it is handed —
     it applies `COALESCE(LAST_MODIFIED_TS, CREATED_TS)` as a filter/sort/limit using *its own*
     parser for its own format, which is what a real warehouse does. Then drove the **real**
     `WarehouseElnAdapter` over four rows: two old reactions amended recently (`CREATED_TS` a real
     `datetime`, as the Snowflake DictCursor returns; `LAST_MODIFIED_TS` a site VARCHAR audit column
     `DD/MM/YYYY HH:MM`) and two genuinely new reactions after them, with `fetch_limit: 2`.

     ```
     A. page FULL of rows with an unparseable modified_at (fetch_limit=2, 2 such rows):
       A run 1: since=2026-07-01 fetched=['R-1', 'R-2'] -> cursor=2026-07-01
       A run 2: since=2026-07-01 fetched=['R-1', 'R-2'] -> cursor=2026-07-01
       A run 3: since=2026-07-01 fetched=['R-1', 'R-2'] -> cursor=2026-07-01
       A run 4: since=2026-07-01 fetched=['R-1', 'R-2'] -> cursor=2026-07-01
       A: never fetched = ['R-3', 'R-4']

     statement: SELECT * FROM V_RXN WHERE COALESCE(LAST_MODIFIED_TS, CREATED_TS) >= ?
                ORDER BY COALESCE(LAST_MODIFIED_TS, CREATED_TS) ASC LIMIT ?
     ```

  3. Then drove the **real `sync_entries`** (`/tmp/av2/repro2c.py`) over the same warehouse with
     in-memory stores and a stub submitter:

     ```
     ===== RUN 1..3 =====
     LOG INFO chemclaw.ingest.eln.sync: eln sync: ingested=2 rejected=0 skipped_existing=0 awaiting_merge=2
     SUMMARY next_cursor= 2026-07-01T00:00:00+00:00   (every run, unchanged)
     ```

     and in the merged steady state (`_merged_note_bodies` returning the two note bodies, i.e. a
     human has merged them — `/tmp/av2/repro2d.py`):

     ```
     LOG INFO chemclaw.ingest.eln.sync: eln sync: ingested=0 rejected=0 skipped_existing=2 awaiting_merge=0
     SUMMARY next_cursor= 2026-07-01T00:00:00+00:00   (every run, unchanged)
     ```

     No WARNING, no rejection, cursor frozen, `R-3`/`R-4` never ingested. "Completely silent" is
     literally true in steady state.

  4. Checked the workflow's wedge guard by reading `durable/eln_sync.py:110-118` and `:238-252`:
     `_BoundedIngest` caps only entries with `created_at > since`; every wedge row has
     `created_at <= since`, so `truncated` stays `False`, `has_more` is `False`, the loop `break`s at
     `if not chunk.has_more` and the `next_cursor <= source_since` warning at `:246` is never reached.
     Confirmed as the finding states.

- **Why**

  Mechanism, trigger and consequence all reproduce from scratch against unmodified source, with the
  real adapter, the real `sql.entry_statement` and the real `sync_entries`. Line numbers are current
  (`adapter.py:386-398`, reached from `:161-163`; twin at `json_adapter.py:372-379`; the comment it
  contradicts at `sync.py:141-149`).

  Three corrections that do not change the verdict but should be carried into the fix:

  - **Half the stated trigger does not hold.** `_optional_timestamp` has an explicit
    `isinstance(value, datetime)` arm (`adapter.py:390-391`), and `snowflake.connector`'s `DictCursor`
    returns `TIMESTAMP_TZ` as a `datetime`, not as `'2026-08-01 12:00:00.000 -0700'`. The
    Snowflake-string spelling is speculative; the **site VARCHAR audit column** is the real and
    sufficient trigger, and it is the one I reproduced.

  - **One such row wedges nothing.** With `fetch_limit=2` and only *one* bad row, my run B recovered
    (`run 1 fetched ['R-1','R-3'] -> cursor=2026-08-10`, then normal progress, nothing lost). The
    wedge needs enough such rows to fill the page — which the finding's prose says, but its own
    single-row transcript does not show, so the transcript reads stronger than the single row
    warrants. Realistic anyway: a site's audit-column format is uniform, so it is all rows or none.

  - **The proposed fix is the wrong shape.** `_optional_timestamp` is called from `_raw_entry`, which
    runs inside `fetch_new_entries` — not inside `map_to_ord`. Making it raise does **not** land in
    `sync_entries`' reject-and-continue arm; it aborts the whole fetch. I measured this by
    monkey-patching a strict variant in-process (no source edit): `C run 1: fetch RAISED
    ElnMappingError: unreadable timestamp '01/08/2026 12:00'`, batch over. The same trap already
    exists for the required column: `_timestamp` (`adapter.py:375-383`) raises out of
    `fetch_new_entries` too, so one row with an unreadable `CREATED_TS` already aborts every warehouse
    batch. Whatever replaces the silent `None` has to reject the row **inside** `fetch_new_entries`
    (skip + WARNING, as both file adapters do) or move the parse into `map_to_ord`.
