# ingest/eln + ingest/sources — CORRECTNESS, reachability/consequence lens

In scope: the one **critical** and the three **high** findings. Medium/low ignored.

Working tree was clean (`git status --porcelain` empty) before and after; no source file was
mutated. All runs used the reporter's own scripts in `/tmp` plus two of my own
(`/tmp/v_temp.py`, `/tmp/v_note.py`, `/tmp/v_bounded.py`).

Two facts that bear on every warehouse finding below, established once:

- **The warehouse half is opt-in.** `src/chemclaw/core/config/sources.py:45` —
  `data_sources: str = "graph,eln-json"`. `eln-snowflake` is *discovered* but not enabled;
  reaching any warehouse defect needs an operator to add it to `CHEMCLAW_DATA_SOURCES` and
  point the binding at real tables. That lowers "reachable today" but not "reachable in a real
  deployment" — the manifest ships as the intended production integration and nothing in
  `binding.py` constrains the *data*, only the identifiers.
- **`_BoundedIngest` is not a backstop.** `durable/eln_sync.py:112-121` truncates only entries
  with `created_at > since`, and `truncated` is that count. I drove it directly
  (`/tmp/v_bounded.py`) against both paging repros:

  ```
  A tie: fetched ['RX-A', 'RX-B'] has_more(truncated) = False
  B skew: fetched ['SKEW-1']      has_more(truncated) = False
  ```

  So the workflow's `next_cursor <= source_since` wedge guard (`durable/eln_sync.py:254`) is
  gated behind `has_more` and is genuinely unreachable in both cases, exactly as reported.

---

## One row with a NULL (or unparseable) created_at column kills the whole warehouse sync, permanently

- **Verdict**: OVERSTATED
- **Severity I would assign**: high

- **What I did**

  Ran the reporter's script unmodified:

  ```
  $ PYTHONPATH=/home/user/Chemclaw3 uv run python /tmp/repro_null_created.py
  sync_entries RAISED ElnMappingError: entry 'LEGACY-9' has no usable 'CREATED_TS';
      the sync cursor is a timestamp and cannot order a row without one
  -> GOOD-1 was never ingested; the whole batch died on one row.
  ```

  Then traced the trigger and the consequence:
  - Reachability of the row: `sql.watermark_expression` (`sql.py:53-55`) emits
    `COALESCE(LAST_MODIFIED_TS, CREATED_TS)` whenever `modified_at` is declared — and the
    shipped `eln-snowflake/datasource.yaml` declares it, with the manifest comment urging every
    site to. A row with `CREATED_TS IS NULL` and `LAST_MODIFIED_TS` set therefore has a non-NULL
    watermark and *is* returned. (Without `modified_at` the watermark is `CREATED_TS`, `NULL >= ?`
    is NULL, and the row is filtered out — so the trigger genuinely needs the amendment column,
    which the finding states.) `EntryBinding` validates identifiers only; nothing validates data.
  - Consequence path: `_raw_entry` (`adapter.py:154-165`) builds `created_at` eagerly inside
    `fetch_new_entries`, which `sync_entries` calls *outside* the per-entry `try`
    (`sync.py:132` vs `sync.py:188`). `ElnMappingError` is in `_BAD_DATA_TYPES`
    (`durable/publish.py:32-42`) by exact class name, so `BAD_DATA_RETRY` makes it
    non-retryable; `sync_eln_entries` does not catch it and `ElnSyncWorkflow.run` does not
    catch it, so the whole workflow run fails.

- **Why**

  The mechanism, the trigger and the contract violation all reproduce. Two things the reporter
  missed, one in each direction:

  **Worse than reported.** `durable/memory_jobs.py:81-84` has the identical shape —
  `for raw in await adapter.fetch_new_entries(since):` with the `try` starting on the *next*
  line, wrapping only `map_to_ord`. So the same single NULL row also kills `read_corpus`, and
  with it all three memory-synthesis workflows (campaign synthesis, playbook distillation,
  optimization campaign), not just the ELN sync. The blast radius is four workflows.

  **Not as bad as reported.** The severity label is the part that does not hold. Nothing is
  silent here and nothing is wrong: the workflow fails loudly, the Temporal failure carries the
  verbatim message `entry 'LEGACY-9' has no usable 'CREATED_TS'` — i.e. the entry id and the
  offending column, which is the diagnosis — and the corpus is not corrupted, only not
  advanced. No chemist is shown a wrong number; a scheduled job goes red. Recovery is fixing
  the row upstream or the code, then re-running from the unchanged cursor. Set against the two
  paging findings in this same file, which are *silent* permanent row loss and are labelled
  high, a loud recoverable stall behind an opt-in source cannot be a step above them.
  `critical` is one notch too high; `high` is right, and the fix is still worth making.

---

## A Unicode minus or en dash before a temperature flips its sign: “−78 °C” is ingested as **+78 °C**

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  `/tmp/v_temp.py` against the real `_TEMPERATURE` and `_segment_steps`:

  ```
  'cooled to -78 °C' -> -78.0
  'cooled to −78 °C' ->  78.0     # U+2212
  'cooled to –78 °C' ->  78.0     # U+2013
  '60-80 °C'         ->  80.0     # range still reads the upper bound
  'at -10 °C'        -> -10.0     # ASCII negative still works

  1 temperature 'Cool the solution to −78 °C' 78.0
  2 addition    'Add n-BuLi dropwise'         None
  3 temperature 'Warm to 20 °C'               20.0
  ```

  Then drove a real export file through the real adapter and the real note renderer
  (`/tmp/v_note.py`, one JSON entry in `/tmp/eln_export_v/`), which is what a chemist is
  actually shown:

  ```
  headline temperature_c: 78.0

  - scale: 0.46 g of reactants charged
  - temperature: 78.0 °C
  - yield: 85.0%
  ...
  ## Procedure
  1. Cool the solution to −78 °C (_temperature_, 78.0 °C)
  ```

- **Why**

  Reachable by default, unlike the warehouse findings: `eln-json` is in the shipped
  `data_sources` default and in `infra/live/e2e-full-stack/up.sh`. Nothing upstream normalises
  the text — `_parse_timestamp`/`_build` pass `procedure` through verbatim — and no validator
  or model constrains a procedure string. A procedure pasted from an ACS/RSC PDF, or typed in
  Word with autocorrect, is the ordinary content of that field.

  The consequence is exactly as stated and slightly wider. Two rendered surfaces carry the
  wrong sign: the headline conditions bullet (`note.py:96-97`) and the per-step line
  (`note.py:236-237`). Beyond the note, `temperature_c` feeds the memory layer —
  `memory/progression.py:131` reports "temperature changed from …" and
  `memory/optimization.py:116` fills the campaign table's `Temp (°C)` column — and both read
  `OrdReaction`s produced by `read_corpus`, i.e. the same ingest. So one sign flip propagates
  into synthesised campaign notes as well.

  The one mitigation is the PR-gate: the step line prints the wrong number *next to* the
  verbatim `−78 °C`, so a careful reviewer can catch it there. That is not enough to downgrade.
  The headline bullet — the line a retrieval answer surfaces and the memory tables copy — has
  no prose beside it, and "the reviewer might notice" is precisely the excuse this audit
  disallows for a chemistry number. A cryogenic lithiation recorded as +78 °C is a 156 °C error
  with the wrong sign on the one condition that makes that reaction survivable. High is right.

---

## Rows tied on the watermark beyond `fetch_limit` are stranded forever, and nothing reports it

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Ran `/tmp/repro_paging.py` case A (three rows sharing one `CREATED_TS`, `fetch_limit: 2`,
  four rounds through the real `sync_entries` against the in-repo `WatermarkWarehouse`, which
  honours WHERE/ORDER BY/LIMIT):

  ```
  round 0: cursor_in=2026-01-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 1: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 2: … (identical)   round 3: … (identical)
  EVER INGESTED: ['RX-A', 'RX-B']
  ```

  Anticipating the obvious objection — the repro's `fetch_limit` is below
  `eln_sync_batch_size`, which trips the adapter's own startup WARNING — I re-ran it with
  `CHEMCLAW_ELN_SYNC_BATCH_SIZE=1`, so `fetch_limit (2) > batch_size (1)` and the warning does
  not fire. **The result is byte-identical**: `EVER INGESTED: ['RX-A', 'RX-B']`. The
  misconfiguration warning is not what produces the wedge.

  Also confirmed the guard is unreachable (`/tmp/v_bounded.py`, above): `truncated = False`
  because every tied row has `created_at <= since` once the cursor sits on the tie.

- **Why**

  `sql.entry_statement` (`sql.py:76-81`) emits `ORDER BY <watermark> ASC LIMIT ?` with no unique
  tiebreak, and the cursor can only advance to `max(window)` over the page, which for a tie
  group is the tie value itself. There is no upstream constraint that could prevent a tie:
  `EntryBinding` checks identifiers, `fetch_limit` is bounded 1..5000 by pydantic but that
  bounds the page, not the tie, and the `where:` predicate is the site's own. A bulk migration
  stamping one `LAST_MODIFIED_TS` across the backfill — the reporter's own example — makes
  *every* row tie, and since that value is then the earliest watermark, a first sync ingests
  `fetch_limit` rows and the source never ingests anything again.

  Two corrections, both making it worse rather than better:

  1. The reporter's stability assumption is the *charitable* one. The fake's `sorted()` is
     stable, so the same page returns each run. A real warehouse gives no order among ties, so
     Snowflake may return a different arbitrary subset each run — coverage becomes random and
     there is still no guarantee any given row is ever seen.
  2. "every run logs `ingested=<fetch_limit> rejected=0`" is true only while the notes are
     unmerged; my repro shows the `awaiting_merge` WARNING does fire in that state. Once a human
     merges those first-page notes, the body comparison in `sync.py:200-207` turns them into
     `skipped_existing`, and the run logs `ingested=0 rejected=0 awaiting_merge=0` — i.e. a
     perfectly healthy "nothing new tonight", forever, while the rest of the warehouse is never
     read. The steady state is *more* silent than reported.

  Silent permanent loss of ingestable records, reachable from a manifest an operator writes.
  High.

---

## The cursor advances on `max(created, modified)` while the fetch pages on `COALESCE(modified, created)` — rows between the two are skipped forever

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  `/tmp/repro_paging.py` case B (`SKEW-1` created 2026-06-01 / modified 2026-01-02, plus
  `PLAIN-2` at 2026-02-01 and `PLAIN-3` at 2026-03-01, `fetch_limit: 1`):

  ```
  round 0: cursor_in=2026-01-01… ingested=['SKEW-1'] cursor_out=2026-06-01T00:00:00+00:00
  round 1: cursor_in=2026-06-01… ingested=[]         cursor_out=2026-06-01T00:00:00+00:00
  round 2/3: identical
  EVER INGESTED: ['SKEW-1']
  ```

  Re-run with `CHEMCLAW_ELN_SYNC_BATCH_SIZE=1` (so the fetch_limit/batch_size warning does not
  fire): identical. `_BoundedIngest` reports `truncated = False`, so the wedge guard never runs.

- **Why**

  The two watermarks really are different functions of the same row: `sql.py:53-55` returns
  `COALESCE(modified, created)`, `adapter.py:94` returns `max(created, modified)`. They differ
  exactly when `modified < created`, and `sync.py:183` (`cursor = max(cursor, window)`) then
  moves the cursor to a value the query never ordered by. Nothing upstream prevents it — there
  is no CHECK constraint the code can rely on, `_optional_timestamp` accepts whatever the driver
  hands back, and the future-horizon guard (`sync.py:163-183`) only refuses values beyond wall
  clock, which a legacy `created_at` is not.

  One narrowing of the reporter's trigger, which I checked rather than assumed: the loss needs
  the page to be **truncated**. Without truncation, every row at or after `since` was already
  returned in that fetch, so nothing is owed; and the horizon guard stops the cursor from ever
  jumping past wall clock, so no *future* row can fall below it. With truncation the loss is
  guaranteed, not merely possible: the remainder's watermarks are all ≥ the page's maximum
  watermark, and every one of them below the skewed `created_at` is now permanently under the
  next `WHERE wm >= ?`. On a first sync of a warehouse with history the page is full by
  definition, so "truncated" is the normal case rather than the corner.

  I am less convinced by one of the two motivating scenarios — an ETL load stamp normally
  *post*dates row creation, so that half is weak — but the other one stands on its own: a
  migration that writes a single fixed `LAST_MODIFIED_TS` while preserving original
  `CREATED_TS` values produces `modified < created` on every row it touched. One is enough.

  Silent, permanent, no rejection and no counter. High, same tier as the tie defect, and the two
  share a root cause worth fixing once: the fetch and the cursor do not agree on what "the
  timestamp this entry was fetched by" means.
