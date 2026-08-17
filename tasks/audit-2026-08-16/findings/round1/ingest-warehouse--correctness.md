# ingest/eln + ingest/sources — CORRECTNESS

Slice: `src/chemclaw/ingest/eln/**` (adapter, json_adapter, ord_adapter, ord, note, ingest, validate,
sync, cursor, `warehouse/*`) and `src/chemclaw/ingest/sources/**`.
All reproductions were run against the repo's own offline fakes (`tests/warehouse_fake.py`,
`InMemoryFingerprintStore`, `FakeSubmitter`) with `PYTHONPATH=/home/user/Chemclaw3 uv run python …`.
Scripts live in `/tmp/repro_paging.py`, `/tmp/repro_null_created.py`, `/tmp/repro_zero_key.py`.

---

## One row with a NULL (or unparseable) created_at column kills the whole warehouse sync, permanently

- **Severity**: critical
- **Location**: `src/chemclaw/ingest/eln/warehouse/adapter.py:160` (`_raw_entry`) → `:375` (`_timestamp`),
  raised out of `fetch_new_entries` (`:85`)
- **Trigger**: the entry relation contains one row whose declared `created_at` column is NULL (or
  holds a string `parse_iso_utc` cannot read) while its `modified_at` column is set. The fetch
  predicate is `COALESCE(LAST_MODIFIED_TS, CREATED_TS) >= ?` (`sql.watermark_expression`), so that
  row **is** returned by the query — a legacy/backfilled row whose creation stamp was lost in an ETL
  is exactly the shape.
- **Consequence**: `_timestamp` raises `ElnMappingError` *inside `fetch_new_entries`*, not inside
  `map_to_ord`. `sync_entries` calls `fetch_new_entries` **outside** its per-entry `try`
  (`ingest/eln/sync.py`, `entries = await adapter.fetch_new_entries(...)`), so the exception escapes
  the whole activity. Nothing in that batch is ingested — including the perfectly good rows fetched
  alongside it — the cursor never advances, and because the bad row keeps matching the same
  predicate, every subsequent scheduled run dies on it again. `ElnMappingError` is a `ChemclawError`,
  which `durable/publish` marks **non-retryable**, so the workflow fails rather than retrying. The
  source is dark from that moment on, and no `IngestSummary` is ever produced to say why.
  This directly contradicts two claims in the code: the module docstring ("`chemclaw.ingest.eln.sync`
  supplies … reject-and-continue"; "a row the binding cannot map is rejected with its reason and the
  batch continues") and `_timestamp`'s own docstring ("or **reject the row** naming what was
  missing"). The two file-drop adapters do honour the contract here — they `continue` past an
  unreadable file — so the warehouse adapter is the one that breaks it.
- **Evidence**: `/tmp/repro_null_created.py`, two rows (`GOOD-1` valid, `LEGACY-9` with
  `CREATED_TS = None`, `LAST_MODIFIED_TS` set), driven through the real `sync_entries`:

  ```
  sync_entries RAISED ElnMappingError: entry 'LEGACY-9' has no usable 'CREATED_TS';
      the sync cursor is a timestamp and cannot order a row without one
  -> GOOD-1 was never ingested; the whole batch died on one row.
  ```

- **Fix**: do the timestamp read where rejection is possible. Either build `RawEntry` lazily and let
  `map_to_ord` raise (so `sync_entries`' reject-and-continue arm sees it), or — simplest — have
  `fetch_new_entries` drop such rows the way it already drops keyless ones: skip the bundle, log one
  aggregated WARNING naming the entry ids and the column, and carry on with the rest of the page.
  Falling back to the watermark value (`COALESCE`-equivalent: use `modified_at` when `created_at` is
  absent) is also defensible, since that is the value the query already ordered the row by.

---

## A Unicode minus or en dash before a temperature flips its sign: “−78 °C” is ingested as **+78 °C**

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/json_adapter.py:64` (`_TEMPERATURE`), used by `_condition`
  (headline `temperature_c`) and by `_segment_steps` → `ReactionStep.temperature_c`
- **Trigger**: any procedure text using the typographic minus `−` (U+2212) or an en dash `–`
  (U+2013) in front of a temperature — i.e. anything pasted from a journal PDF (ACS/RSC typeset
  cryogenic temperatures with U+2212 as a matter of house style) or typed in Word with autocorrect
  on. `_TEMPERATURE`'s sign is `-?`, which matches **only** ASCII hyphen-minus; the dash is then
  simply not consumed and the number is read bare.
- **Consequence**: a wrong chemistry number, silently. A `-78 °C` lithiation becomes
  `temperature_c = 78.0`, which is rendered into the proposed note as `- temperature: 78.0 °C` and
  into the step line as `(_temperature_, 78.0 °C)`. A 156 °C error with the wrong sign is the worst
  possible answer to "what temperature did we run that at?", and it looks entirely plausible to the
  reviewer at the PR-gate because the verbatim prose beside it says `−78`. The headline field is
  only affected when the structured `temperature_c` is absent, but `_segment_steps` runs the regex
  on the prose **unconditionally**, so the per-step temperature is wrong on every such entry.
  The comment above the pattern reasons explicitly about sign handling ("The lookbehind stops a `-`
  preceded by a digit/dot from being read as a minus sign") — the reasoning is right and covers only
  one of the three characters that occur in practice.
- **Evidence**:

  ```
  'cooled to -78 °C'  -> -78.0     # ASCII hyphen
  'cooled to −78 °C'  ->  78.0     # U+2212 MINUS SIGN
  'cooled to –78 °C'  ->  78.0     # U+2013 EN DASH

  _segment_steps('Cool the solution to −78 °C. Add n-BuLi dropwise. Warm to 20 °C.')
  1 temperature 'Cool the solution to −78 °C'  78.0
  2 addition    'Add n-BuLi dropwise'          None
  3 temperature 'Warm to 20 °C'                20.0
  ```

- **Fix**: accept the whole minus family in the sign, keeping the existing lookbehind so a range
  separator is still not read as a sign:
  `re.compile(r"(?<![\d.])([-−‐-―]?\d+(?:\.\d+)?)\s*°\s*C\b")`, and normalise the
  matched leading character to `-` before `float()`. The range case (`60–80 °C`) keeps working
  because the lookbehind still rejects a dash preceded by a digit. Add the three spellings to the
  regex's test.

---

## Rows tied on the watermark beyond `fetch_limit` are stranded forever, and nothing reports it

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/warehouse/sql.py:58-81` (`entry_statement`: `WHERE wm >= ?
  ORDER BY wm ASC LIMIT ?`, no unique tiebreak) with the inclusive cursor in
  `warehouse/adapter.py:85` and `ingest/eln/sync.py`'s `cursor = max(cursor, window)`
- **Trigger**: more rows share one watermark value at the cursor boundary than `entry.fetch_limit`
  (default 500, max 5 000). Realistic sources of a tie: a `DATE`-typed `created_at` (every row that
  day ties), a bulk migration that stamps one load timestamp on the backfill, or a nightly ETL that
  writes `LAST_MODIFIED_TS = <batch start>` to every row it touched.
- **Consequence**: the page is the first `fetch_limit` rows of an *unordered* tie group, the cursor
  advances only to that same tied timestamp, and the next fetch (`>= cursor`) asks the identical
  question and gets the identical page back. The rows past the LIMIT are never ingested — not this
  run, not any run. The sync's own reporting makes it invisible: every run logs
  `ingested=<fetch_limit> rejected=0` and looks like healthy steady progress. The
  `next_cursor <= source_since` wedge guard in `durable/eln_sync.py` never fires either, because it
  is gated on `has_more`, which comes from `_BoundedIngest.truncated` (a `created_at > since` count),
  and the tied rows are all *at* `since`, so `truncated` is False.
  `entry_statement`'s docstring asserts the opposite property — "Ordering ascending and taking the
  first `limit` is exactly that [i.e. makes progress]" — which is true only when the tie group fits
  in one page. The neighbouring test
  `test_a_page_of_amended_rows_does_not_stall_the_sync_forever` uses amendment stamps one minute
  apart, so it never exercises a tie.
- **Evidence**: `/tmp/repro_paging.py` case A — three rows sharing one `CREATED_TS`,
  `fetch_limit: 2`, driven through the real `sync_entries` against `WatermarkWarehouse` (which
  honours WHERE/ORDER BY/LIMIT):

  ```
  round 0: cursor_in=2026-01-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 1: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 2: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 3: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  EVER INGESTED: ['RX-A', 'RX-B']          # RX-C is unreachable
  ```

- **Fix**: make the page a keyset over a *unique* tuple. Order by `(watermark, key)` and page with
  `(watermark, key) > (last_watermark, last_key)` — i.e. carry the last key alongside the cursor, or
  emit `WHERE wm > ? OR (wm = ? AND key > ?)`. If persisting a composite cursor is too big a change,
  at minimum add `ORDER BY <watermark> ASC, <key> ASC` (making the page deterministic) and have the
  adapter raise/WARN when a full page is returned whose first and last watermarks are equal — a page
  that cannot advance the cursor is a condition the code can detect exactly.

---

## The cursor advances on `max(created, modified)` while the fetch pages on `COALESCE(modified, created)` — rows between the two are skipped forever

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/warehouse/sql.py:45` (`watermark_expression` →
  `COALESCE(modified, created)`) vs `src/chemclaw/ingest/eln/adapter.py:82-94` (`entry_window` →
  `max(created, modified)`), consumed by `ingest/eln/sync.py`'s `cursor = max(cursor, window)`
- **Trigger**: any fetched row whose `modified_at` **predates** its `created_at`, on a page that was
  truncated by `fetch_limit`. That combination is not exotic: it is any warehouse where
  `LAST_MODIFIED_TS` is an ETL/row-load stamp and `CREATED_TS` is the chemist-entered experiment
  timestamp, or a legacy record migrated with a fixed modification date.
- **Consequence**: the two functions compute different watermarks for the same row. The query pages
  on the smaller one, the cursor jumps to the larger one, and every row whose watermark lies in
  between — rows the LIMIT cut off — is permanently below the next `WHERE wm >= ?` and is never
  fetched again. Silent: no rejection, no counter, no log line. `entry_window`'s docstring calls the
  `max` "cheap insurance" and says "no test can distinguish the two"; a test does distinguish them,
  and the insurance costs rows.
- **Evidence**: `/tmp/repro_paging.py` case B — `SKEW-1` (created 2026-06-01, modified 2026-01-02),
  `PLAIN-2` (2026-02-01), `PLAIN-3` (2026-03-01), `fetch_limit: 1`:

  ```
  round 0: cursor_in=2026-01-01… ingested=['SKEW-1'] cursor_out=2026-06-01T00:00:00+00:00
  round 1: cursor_in=2026-06-01… ingested=[]         cursor_out=2026-06-01T00:00:00+00:00
  round 2: cursor_in=2026-06-01… ingested=[]         cursor_out=2026-06-01T00:00:00+00:00
  EVER INGESTED: ['SKEW-1']                # PLAIN-2 and PLAIN-3 are unreachable
  ```

  The page's maximum watermark was 2026-01-02; the cursor was set to 2026-06-01.
- **Fix**: one definition of "the timestamp this entry was fetched by". Either have
  `watermark_expression` emit `GREATEST(modified, created)` so SQL and Python agree on `max`, or —
  better, because it needs no dialect function — have the warehouse adapter report
  `modified_at` to the sync as the value the query actually ordered on, so `entry_window` reproduces
  `COALESCE`. Independently, cap the cursor at the maximum watermark actually returned by the page:
  the cursor must never move past a row the fetch could still owe us.

---

## `iso_date` / `iso_datetime` do not normalise to UTC, so one instant yields two different `performed_at` dates

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/eln/warehouse/expr.py:163-196` (`_iso_date`, `_iso_datetime`),
  and the same pattern in `warehouse/adapter.py:_optional_timestamp` and
  `ingest/eln/adapter.py:parse_iso_utc`
- **Trigger**: an offset-aware value that is not already at +00:00 — a Snowflake `TIMESTAMP_TZ`
  column (the client returns it in the session timezone, which defaults to `America/Los_Angeles`),
  or an ISO string spelled with a local offset.
- **Consequence**: `parse_iso_utc` returns the parsed datetime **unchanged** when it already carries
  a tzinfo; it only fills in UTC for naive values. So `_iso_datetime`'s docstring claim — "Read an
  instant, **normalised to UTC**" — is false, and `_iso_date` takes `.date()` in whatever offset the
  source used. The same instant therefore maps to two different calendar dates depending on how it
  was spelled. `performed_at` is not decoration: it becomes `Note.valid_from` (`ingest/eln/note.py`),
  which is the time axis for recency ranking and the bi-temporal "what did we know at T" queries, and
  it is rendered into the note body as `- performed: <date>` — which also means the body-comparison
  amendment check compares a date that can move with a session setting.
- **Evidence**:

  ```
  iso_date('2026-08-17T00:30:00Z')          -> 2026-08-17
  iso_date('2026-08-16T17:30:00-07:00')     -> 2026-08-16    # same instant
  iso_date(datetime(...,-07:00))            -> 2026-08-16
  iso_datetime(datetime(...,-07:00))        -> 2026-08-16 17:30:00-07:00   # not UTC
  ```

- **Fix**: make `parse_iso_utc` actually normalise — `parsed.astimezone(UTC)` when tzinfo is present,
  `replace(tzinfo=UTC)` when it is not — and have `_iso_date`/`_optional_timestamp` convert an
  incoming aware `datetime` to UTC before taking `.date()`. That is one line in each place and makes
  every docstring in the chain true.

---

## An entry key of `0` is dropped as "carried no key"

- **Severity**: low
- **Location**: `src/chemclaw/ingest/eln/warehouse/adapter.py:104`
  (`keyed = [row for row in rows if row.get(entry.key)]`)
- **Trigger**: the entry relation's `key` column is numeric and one row holds `0` (an
  auto-increment/sequence starting at zero, or a legacy sentinel). Also fires for `False`.
- **Consequence**: a truthiness test where an `is None` test was meant. The row is silently dropped
  from the batch and counted under a WARNING that misdiagnoses it — "carried no REACTION_ID" — when
  the column was populated. The reaction is never ingested and never rejected, so it appears in no
  summary. `expr.render_template` states the correct rule 40 lines away ("An explicit `is None`
  rather than `or ""`: a reaction id of `0`, or any other falsy value the source legitimately
  recorded, is a value"), so this is the module's own rule broken by its main caller.
- **Evidence**: `/tmp/repro_zero_key.py`, rows with `REACTION_ID` 0 and 1:

  ```
  eln-test: 1 of 2 rows carried no REACTION_ID and were skipped
  fetched entry ids: ['1']
  ```

- **Fix**: `keyed = [row for row in rows if row.get(entry.key) is not None]` (and keep an explicit
  empty-string check if a blank VARCHAR key should also be skipped).

---

## Vendored CSV: a quoted field spanning lines is silently concatenated without a separator

- **Severity**: low
- **Location**: `src/chemclaw/ingest/sources/vendored_dataset.py:113`
  (`csv.DictReader(data.decode("utf-8").splitlines())`)
- **Trigger**: a `records.csv` whose `text_column` (or any carried column) contains an embedded
  newline inside a quoted field — the normal way a CSV carries a multi-line description or a
  multi-line synonym list.
- **Consequence**: `str.splitlines()` strips the line terminators before the csv parser sees them,
  so the parser rejoins the fragments with **nothing** between them. The retrieved evidence text is
  corrupted at the join ("line one" + "line two" → "line oneline two"), which both garbles what the
  chemist reads in the citation and breaks the substring match `retrieve` is built on. The manifest
  checksum does not catch it — the checksum hashes the file bytes, which are fine; the corruption is
  in the parse.
- **Evidence**:

  ```
  via splitlines: [{'name': 'ethanol', 'note': 'line oneline two'}, ...]
  via StringIO  : [{'name': 'ethanol', 'note': 'line one\nline two'}, ...]
  ```

- **Fix**: `csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))` — the documented way to
  feed the csv module, which preserves embedded newlines.

---

## `SnowflakeWarehouse._connect` is a check-then-act across an `await`, so concurrent first use opens two sessions and orphans one

- **Severity**: low
- **Location**: `src/chemclaw/ingest/eln/warehouse/snowflake.py:174-182` (`_connect`)
- **Trigger**: two coroutines reach `cursor()` on the same `SnowflakeWarehouse` before the first
  connection completes — the normal case for the retrieve half, which is one long-lived instance in
  the chat process serving concurrent SSE turns (and the retriever is explicitly documented as
  running "inside a `gather`").
- **Consequence**: both see `self._connection is None`, both `await asyncio.to_thread(client.connect, …)`,
  and the second assignment wins. The first connection is now unreachable: the class deliberately has
  no `close()` ("there is deliberately no `close`"), so the orphaned Snowflake session lives until
  its own idle timeout, consuming a session slot and, on a warehouse billed per session, real money.
  It is not a wrong answer, but it is the check-then-act shape this lens looks for and it is
  unbounded in principle (N concurrent cold starts → N sessions, N−1 orphaned).
- **Evidence**: the `await` sits between the test and the assignment:

  ```python
  if self._connection is None:
      client = _client()
      try:
          self._connection = await asyncio.to_thread(client.connect, **self._options)
  ```

- **Fix**: guard with an `asyncio.Lock` created in `__init__` and re-check inside it, which is three
  lines and removes the window entirely.

---

## Checked and found sound (so the negative result is on the record)

- **Unit arithmetic in `ord_adapter`** — `_TO_MG` (kg 1e6 / g 1e3 / µg 1e-3), `_TO_MMOL`
  (mol 1e3 / µmol 1e-3 / nmol 1e-6), `_TO_HOURS` (min 1/60, s 1/3600, day 24) and `_TO_CELSIUS`
  (`(F−32)·5/9`, `K−273.15`) are all correct against the primary quantity, and an unknown unit
  raises rather than defaulting to 1.0.
- **`ord.transformation_smiles` / `reaction_smiles`** — the agent-slot split (`_AGENT_ROLES` =
  solvent + catalyst) is applied consistently in both, and only the fingerprint form drops agents;
  the two do not disagree about which species the middle slot names.
- **`note._scale`** — `sum(mass_mg)/1000` for reactants only, with `amount_mmol` counted only for
  reactants carrying no mass, so a component with both is not double-counted. Matches its docstring.
- **`validate_ord`** — element-set subsumption including `AddHs` and `step_components()` on the
  input side; a sound necessary condition, not a count comparison.
- **`expr._value_map`** — both sides stringified (so YAML integer keys work) and boolean keys
  rejected at load; the `default` branch does not swallow an unmapped value silently.
- **`expr.render_template`** — `is None` rather than truthiness, so a legitimately falsy value
  renders.
- **`sql.related_statement`** — one `IN (…)` per child block with every key bound, `ORDER BY fk,
  order_by` so per-entry ordering survives the batch fan-out; `_attach_related` files rows by key
  and pre-seeds every bundle, so a block with no rows yields `[]` rather than a missing key.
- **`sources/registry`** — `_source_dirs` `setdefault` genuinely gives earlier dirs precedence as
  documented; `discovered()`'s cache holds manifests only, and halves are rebuilt per call.
- **`sources/manifest`** — the `name`-in-`config` validator does prevent the "got multiple values for
  keyword argument 'name'" startup failure it claims to.
