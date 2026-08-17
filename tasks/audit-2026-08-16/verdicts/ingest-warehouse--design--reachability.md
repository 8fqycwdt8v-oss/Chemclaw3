# Adversarial verification — `ingest/eln/**` + `ingest/sources/**` — design & simplification

Lens: **is the trigger reachable, and is the consequence what is claimed?**

In scope: the two findings marked **high**. The remaining four are medium/low and were not examined.

---

## The two file-drop adapters' `fetch_new_entries` are a 30-line clone, and it has already diverged

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed; the consequence is in fact worse than stated, see below)

### What I did

1. Read both bodies. `json_adapter.py:142` catches `(OSError, json.JSONDecodeError, ElnFormatError)`;
   `ord_adapter.py:115` catches `(OSError, UnicodeDecodeError, json.JSONDecodeError, OrdFormatError)`.
   The divergence is exactly as described.

2. `/tmp/whv/repro1.py` — a drop directory holding one latin-1 file (`a_bad.json`, an accented
   operator name) and one perfectly good UTF-8 file after it, handed to each adapter:

   ```
   json_adapter -> RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 68: invalid continuation byte
   WARNING:chemclaw.ingest.eln.ord_adapter:skipping unreadable ORD export a_bad.json: 'utf-8' codec can't decode byte 0xe9 in position 105: invalid continuation byte
   ord_adapter  -> OK ['good']
   ```

3. `/tmp/whv/repro1b.py` — the exception escapes the whole core loop, because
   `sync.py:132` (`entries = await adapter.fetch_new_entries(...)`) sits *outside* the
   reject-and-continue `try` at `sync.py:188`:

   ```
   sync_entries -> RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 68: invalid continuation byte
   UnicodeDecodeError non-retryable? False
   OSError non-retryable? False
   ```

4. `/tmp/whv/repro1c.py` — driven through the real Temporal activity in an `ActivityEnvironment`
   with `settings.eln_export_dir` pointed at that directory:

   ```
   activity RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 68: invalid continuation byte
   ```

5. Reachability upstream: `src/chemclaw/core/config/sources.py:45` — `data_sources: str = "graph,eln-json"`.
   `eln-json` is **on by default**, and its manifest carries an empty `config:` so it reads
   `settings.eln_export_dir` (`data/eln-exports`). Nothing between the drop directory and
   `Path.read_text(encoding="utf-8")` inspects, validates or transcodes the file — there is no
   manifest schema, pydantic model, Helm default or startup guard in the path. The trigger is "a
   file appears in a watched directory", which is the outermost entry point this source has.

6. Checked the reporter's side note: `rg reaction_smiles src/chemclaw/ingest/eln/json_adapter.py`
   returns **0** hits, while `warehouse/binding.py:4` and `warehouse/README.md:4` both assert
   "`json_adapter` knows a payload has `reaction_smiles`". The example in those docstrings is
   invented, as filed.

7. `diff` against `.../scratchpad/pristine` for both adapters: identical — nothing another agent's
   edit could account for.

### Why

Trigger, mechanism and consequence all hold, and two parts are **worse** than the finding says:

- **Not just "files sorting after the bad one".** The exception escapes before
  `fetch_new_entries` returns its list, so the *entire* batch is lost — including every good file
  that sorted *before* the bad one and was already parsed into `entries`.
- **Not just "aborts the batch".** `UnicodeDecodeError` is absent from `durable/publish._BAD_DATA_TYPES`
  (verified by executing the membership test above), so Temporal classifies it as **retryable**:
  `BAD_DATA_RETRY` burns `activity_max_attempts` (5) and then fails the activity, which fails
  `ElnSyncWorkflow`. `store_sync_cursor` is never reached, and because `ElnSyncWorkflow.run` walks
  its sources in a plain `for` loop, **every ingest source ordered after `eln-json` never syncs
  either.** One cp1252 export file stops all ELN ingestion in the default configuration until a
  human deletes or re-encodes it.

The one thing that keeps this off "critical": it is loud. A repeatedly failing scheduled workflow is
visible in Temporal, and no chemist is shown a wrong safety, impurity or structure answer — the
corpus stops growing rather than growing wrong. High is the right label.

The adapter's own docstring at `json_adapter.py:119-124` ("one broken export file must not abort the
whole fetch") is false as written, and `ord_adapter.py:110-114`'s comment is the same fix applied to
the other copy — which is the finding's actual point and it stands.

---

## `warehouse/adapter._optional_timestamp` silently drops an unparseable `modified_at`, wedging the sync cursor forever

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

1. Confirmed the mechanism by execution. `warehouse/adapter.py:386-398` returns `None` for a present
   but unreadable value; `json_adapter.py:372-379` raises for the same case, and its docstring
   argues the warehouse's policy is the wrong one. Against the real functions:

   ```
   '2026-08-01 12:00:00.000 -0700' -> parse_iso_utc FAIL ValueError | _optional_timestamp -> None
   '01/08/2026 12:00'              -> parse_iso_utc FAIL ValueError | _optional_timestamp -> None
   '2026-08-01 12:00:00.000-0700'  -> parse_iso_utc 2026-08-01 12:00:00-07:00   (no space: parses)
   datetime obj                    -> 2026-08-01 12:00:00-07:00
   date obj                        -> 2026-08-01 00:00:00+00:00
   ```

2. Reproduced the **wedge** end-to-end, through the real `WarehouseElnAdapter` and the real
   `sync_entries` loop (`/tmp/whv/repro2.py`). It is the existing regression fixture
   `tests/test_warehouse_adapter.py::test_a_page_of_amended_rows_does_not_stall_the_sync_forever`
   with one change: `LAST_MODIFIED_TS` holds Snowflake's `TIMESTAMP_TZ` *string* rendering, and the
   fake warehouse parses that spelling for its own `WHERE/ORDER BY/LIMIT` (as a warehouse would).
   `fetch_limit: 2`, three amended rows, one genuinely new reaction:

   ```
   run 1: since=2026-01-01T00:00:00+00:00 ingested=['OLD-1', 'OLD-2'] ... next_cursor=2026-01-01T00:00:00+00:00
   run 2: since=2026-01-01T00:00:00+00:00 ingested=['OLD-1', 'OLD-2'] ... next_cursor=2026-01-01T00:00:00+00:00
   ... (runs 3-6 identical)
   NEW-1 ever ingested? False
   ```

   The cursor never advances, `OLD-3` and `NEW-1` are never reached, `rejected=[]` throughout, and
   `has_more` is false (the re-fetched rows are all `created_at <= since`, so `_BoundedIngest.truncated`
   stays false and `ElnSyncWorkflow`'s wedge guard at `eln_sync.py:254` is never reached). That half
   of the finding is exactly right.

3. Traced the trigger back to the outermost entry point, and this is where it weakens.

   - `src/chemclaw/core/config/sources.py:45`: `data_sources = "graph,eln-json"`. **`eln-snowflake`
     is not enabled**, and its own manifest header says so ("Discovered but NOT enabled").
   - The shipped binding declares `modified_at: LAST_MODIFIED_TS` — a timestamp column. Against the
     real driver (`snowflake.py:189`, `client.DictCursor`), a `TIMESTAMP_*` column arrives as a
     Python `datetime`, which `_optional_timestamp` handles at line 390 and which parses. **The
     shipped configuration cannot trigger this.**
   - The trigger therefore requires a site to bind `modified_at` to a column that reaches Python as
     a *string* (a VARCHAR audit column, or a view doing `TO_VARCHAR`), **and** for that string to
     be one the warehouse still honours in `COALESCE(...) >= ?` and `ORDER BY`. That is a narrow
     band: the finding's own second spelling, `'01/08/2026 12:00'`, is not in it — a warehouse
     asked to coerce that against a bound timestamp raises, which `_SnowflakeCursor.execute`
     turns into a non-retryable `WarehouseQueryError`, i.e. a loud failure, not a silent wedge.
     Only a string the warehouse parses and Python does not produces the described outcome. I could
     not execute that half (no tenant), so it stays an argument, not a measurement.

4. Checked the finding's claim about *silence*. Partly wrong: in the reproduction the stuck entries
   fire `awaiting_merge` on **every** run —

   ```
   eln sync proposed 2 entry/entries whose notes are still unmerged; they will be re-proposed
   every run until a human merges or rejects them: OLD-1, OLD-2
   ```

   — a WARNING naming the exact stuck ids. The wedge is only genuinely silent once those notes are
   merged, at which point `sync.py:206` books them as `skipped_existing` and the run logs
   `ingested=0 rejected=0 skipped_existing=N`. So "completely silent" is the steady state, not the
   whole story, and there is a naming signal in the phase before it.

5. Checked the proposed fix, and it does not do what the finding says. `_optional_timestamp` is
   reached from `_raw_entry`, which is called at `adapter.py:129` **inside `fetch_new_entries`** —
   and `sync.py:132` calls `fetch_new_entries` *outside* the `try` at `sync.py:188`. Raising there
   would therefore **abort the whole fetch**, not "become a per-row `ElnMappingError` which
   `sync_entries`' reject-and-continue arm already reports by id and reason". Worse: `ElnMappingError`
   *is* in `_BAD_DATA_TYPES`, so it would fail the activity non-retryably and fail the workflow on the
   first such row — trading a wedge for the same total halt finding #1 describes. (`_timestamp`,
   the required-`created_at` reader two lines above, already has this shape today.) Any real fix has
   to reject the *row*, which means the rejection has to happen where the sync can see it.

6. `diff` of `warehouse/adapter.py` against `.../scratchpad/pristine`: identical.

### Why

The mechanism is real, the inconsistency between the two same-named `_optional_timestamp`s is real,
and the wedge reproduces exactly as described once the trigger is granted — I confirm all of that
plainly, and the reproduction is worth keeping.

What does not hold at **high** is the reachability and the framing around it. Nothing today can
produce the trigger: the source is off by default, has no tenant, and the one binding that ships
names a timestamp column that arrives as a `datetime` and parses. Reaching it needs an operator to
enable the source *and* bind the amendment watermark to a string column *and* for that string to sit
in the narrow band the warehouse parses and `datetime.fromisoformat` does not — every neighbouring
mistake (an epoch number, a `DD/MM/YYYY` audit string) fails loudly at the warehouse instead. Two
supporting claims are also wrong as written: the failure is not silent in the pre-merge phase, and
the proposed fix would land the error where nothing catches it.

That is a genuine latent defect in code written to be configured by a site that does not exist yet —
medium. It should be fixed, and the fix should raise a *rejection the sync can attribute to a row*
rather than an exception inside `fetch_new_entries`.
