# ingest/eln + ingest/sources — CORRECTNESS · repro lens

Four findings in scope (critical/high). All reproductions below are mine, written from the source.
I deliberately did **not** use `tests/warehouse_fake.py` / `WatermarkWarehouse`: re-implementing
`WHERE`/`ORDER BY`/`LIMIT`/`COALESCE` semantics in Python is exactly the scaffolding a "does it
really page like that" claim must not lean on. Instead I wrote a `Warehouse` driver backed by real
**sqlite3** (`/tmp/audit_wh/sqlwh.py`) and fed it the statement `sql.entry_statement` actually
generates, with the engine's own bound params. Placeholder `?`, same as Snowflake.

Scripts: `/tmp/audit_wh/sqlwh.py`, `/tmp/audit_wh/f1_null_created.py`, `/tmp/audit_wh/f2_minus.py`,
`/tmp/audit_wh/f34_paging.py`, `/tmp/audit_wh/f34_bounded.py`.

All six cited files are byte-identical to `HEAD` and to the pristine copy (`diff -q` clean), so every
line number below is current. Every cited line number and symbol in the four findings checked out:
`warehouse/adapter.py:85/104/160/375`, `warehouse/sql.py:45` and `:58-81`,
`ingest/eln/adapter.py:82-94`, `json_adapter.py:64`.

---

## One row with a NULL (or unparseable) created_at column kills the whole warehouse sync, permanently

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (down from critical — see the last paragraph)
- **What I did**: `uv run python /tmp/audit_wh/f1_null_created.py`. Two rows in a real sqlite
  `RX` table — `GOOD-1` (`CREATED_TS` set, `MOD_TS` NULL) and `LEGACY-9` (`CREATED_TS` **NULL**,
  `MOD_TS` set) — plus their charge rows, driven through `WarehouseElnAdapter` and the real
  `sync_entries` with `InMemoryFingerprintStore`s and a recording submitter. Printed:

  ```
  fetch_new_entries RAISED ElnMappingError: entry 'LEGACY-9' has no usable 'CREATED_TS';
      the sync cursor is a timestamp and cannot order a row without one
  statement sent: SELECT * FROM RX WHERE COALESCE(MOD_TS, CREATED_TS) >= ?
                  ORDER BY COALESCE(MOD_TS, CREATED_TS) ASC LIMIT ?
  rows the SQL actually returned: ['GOOD-1', 'LEGACY-9']
  sync_entries RAISED ElnMappingError: entry 'LEGACY-9' has no usable 'CREATED_TS'; ...
  ```

- **Why**: every link in the chain holds, and the middle one is the part I most wanted to check
  independently — a *real* SQL engine, given the engine's own generated predicate, does return the
  NULL-`CREATED_TS` row, because `COALESCE(MOD_TS, CREATED_TS)` falls back to the populated
  amendment stamp. So the trigger is not hypothetical: the query is written to fetch exactly this
  row. From there it is mechanical: `_raw_entry` (`warehouse/adapter.py:160`) calls `_timestamp`
  (`:375`), which raises inside the list comprehension at `:129` — i.e. inside `fetch_new_entries`,
  which `sync.py:132` calls **outside** the per-entry `try` at `:188`. `GOOD-1` was fetched by the
  same query and is never ingested. `ElnMappingError` is listed by exact class name in
  `durable/publish.py:_BAD_DATA_TYPES` (line 39), so `BAD_DATA_RETRY` makes it non-retryable and the
  activity fails outright; `store_sync_cursor` is only reached after a *successful* chunk, so the
  cursor never advances and the identical row is refetched on every subsequent firing.

  One thing the reporter missed that makes it worse: `ElnSyncWorkflow.run` loops sources
  **sequentially** (`durable/eln_sync.py:217`) and the activity failure fails the whole workflow, so
  every ingest source ordered after the poisoned one also stops syncing — the blast radius is the
  ELN sync as a whole, not just the one warehouse.

  Where I temper it: "no `IngestSummary` is ever produced to say why" is literally true but reads as
  "silent", and it is not. The workflow fails with an exception whose message names the entry id and
  the exact column. That is a loud, precisely-diagnosed outage requiring manual intervention rather
  than silent data loss — which is why I would file it high rather than critical. The contract
  violation the finding builds on is real and correctly identified: the module docstring at
  `warehouse/adapter.py:8-11` and `_timestamp`'s own docstring at `:376` both promise
  reject-and-continue for this row, and neither is true.

---

## A Unicode minus or en dash before a temperature flips its sign: "−78 °C" is ingested as **+78 °C**

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**: first the regex alone, then end-to-end through the real `JsonExportAdapter` and
  `note_from_ord_reaction` (`/tmp/audit_wh/f2_minus.py`, one temp export dir per spelling):

  ```
  'cooled to -78 °C' -> -78.0     # U+002D
  'cooled to −78 °C' ->  78.0     # U+2212 MINUS SIGN
  'cooled to –78 °C' ->  78.0     # U+2013 EN DASH
  'cooled to ‐78 °C' ->  78.0     # U+2010 HYPHEN  (a fourth spelling, not in the finding)
  '60-80 °C'         ->  80.0     # range still reads the upper bound
  ```

  End-to-end, same entry with only the dash character varied:

  ```
  --- ASCII hyphen ---            --- U+2212 ---
  headline temperature_c: -78.0   headline temperature_c: 78.0
  step temps: [-78.0, None, 20.0] step temps: [78.0, None, 20.0]
  note: - temperature: -78.0 °C   note: - temperature: 78.0 °C
  note: 1. Cool the solution to   note: 1. Cool the solution to −78 °C
        -78 °C (_temperature_,          (_temperature_, 78.0 °C)
        -78.0 °C)
  ```

- **Why**: reproduces exactly as claimed, including both consequence sites the finding names — the
  headline `temperature_c` (via `_condition`, `json_adapter.py:188`, which falls back to the prose
  regex only when the structured field is absent, as stated) and the per-step `temperature_c` (via
  `_segment_steps`, `:221`, which runs on the prose unconditionally, also as stated). I re-derived
  the mechanism from the pattern itself: `(?<![\d.])(-?\d+…)` — `-?` is a literal U+002D character
  class of one, so U+2212/U+2013 are simply not consumed and `float()` sees a bare `78`. The note
  body carries `- temperature: 78.0 °C` next to verbatim prose reading `−78`, which is the reviewer
  trap the finding describes. `json_adapter` is a live shipped source
  (`ingest/sources/eln-json/datasource.yaml` → `chemclaw.ingest.eln.json_adapter:JsonExportAdapter`),
  so the path is reachable in a default deployment. I add a fourth spelling the finding's proposed
  fix already happens to cover: U+2010 HYPHEN also flips the sign.

  The 156 °C-with-the-wrong-sign framing is fair for a cryogenic lithiation, and the sign error is
  the kind a downstream reader cannot detect from the structured record alone. High is right; I would
  not go to critical only because a human merges the note at the PR-gate with the verbatim prose
  visible beside the wrong number.

---

## Rows tied on the watermark beyond `fetch_limit` are stranded forever, and nothing reports it

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**: `/tmp/audit_wh/f34_paging.py` case A — three rows sharing one `CREATED_TS`
  (`2026-06-01`), `MOD_TS` NULL, `fetch_limit: 2`, driven four rounds through the real
  `sync_entries` against real sqlite:

  ```
  round 0: cursor_in=2026-01-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 1: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 2: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  round 3: cursor_in=2026-06-01… ingested=['RX-A','RX-B'] cursor_out=2026-06-01T00:00:00+00:00
  EVER SEEN: ['RX-A','RX-B']  | all rows: ['RX-A','RX-B','RX-C']
  ```

  And `/tmp/audit_wh/f34_bounded.py` case A′ — the same rows through the durable
  `_BoundedIngest` wrapper, to test the finding's claim about the wedge guard:

  ```
  round 0: in=2026-01-01… ingested=['RX-A','RX-B'] has_more(truncated)=False out=2026-06-01…
  round 1: in=2026-06-01… ingested=['RX-A','RX-B'] has_more(truncated)=False out=2026-06-01…
  ```

- **Why**: the fixed point is real and I got it out of a real SQL engine, not a hand-written
  filter. `entry_statement` (`sql.py:76-81`) emits no unique tiebreak, so the page is `LIMIT n` over
  a tie group in unspecified order; `sync.py:183` moves the cursor only to `entry_window`, which for
  a tie group *is* the tied value; and the next `>= cursor` fetch asks the identical question. `RX-C`
  is unreachable in round 1 and in every round after.

  The finding's secondary claim about the durable wedge guard also holds and I checked it directly:
  `_BoundedIngest.truncated` is computed from `created_at > since` (`durable/eln_sync.py:114`), and
  in the tie case every row's `created_at` equals `since`, so `has_more` is **False** — printed
  above. The workflow therefore concludes the drain is complete and the
  `next_cursor <= source_since` guard at `eln_sync.py:257` is never reached, exactly as claimed.

  Two small corrections that do not change the verdict. (1) The reachability rests on a tie group
  larger than `fetch_limit` (default 500, min configurable 1, max 5 000); of the finding's three
  proposed causes the strongest is the whole-table ETL that stamps one `LAST_MODIFIED_TS` — because
  the watermark is `COALESCE(modified, created)`, one such rewrite ties *every* row in the relation
  and a first sync then strands everything past row 500 permanently. A `DATE`-typed `created_at`
  needs >500 reactions in a day, which is a much bigger ELN. (2) "every run logs
  `ingested=<fetch_limit> rejected=0` and looks like healthy steady progress" is not quite the
  observable: while the notes are unmerged the run emits a WARNING (`eln sync proposed 2
  entry/entries whose notes are still unmerged…`, visible in my transcript), and once a human merges
  them the entries fall into `skipped_existing` and the run reads `ingested=0` forever. Neither
  reading names the stranded rows, so the finding's conclusion — invisible — survives; its specific
  log line does not.

---

## The cursor advances on `max(created, modified)` while the fetch pages on `COALESCE(modified, created)` — rows between the two are skipped forever

- **Verdict**: CONFIRMED (and understated)
- **Severity I would assign**: high
- **What I did**: `/tmp/audit_wh/f34_paging.py` case B — `SKEW-1` (created 2026-06-01, modified
  2026-01-02), `PLAIN-2` (created 2026-02-01), `PLAIN-3` (created 2026-03-01), `fetch_limit: 1`:

  ```
  round 0: cursor_in=2026-01-01… ingested=['SKEW-1'] cursor_out=2026-06-01T00:00:00+00:00
  round 1: cursor_in=2026-06-01… ingested=[]         cursor_out=2026-06-01T00:00:00+00:00
  round 2: cursor_in=2026-06-01… ingested=[]         cursor_out=2026-06-01T00:00:00+00:00
  EVER SEEN: ['SKEW-1']  | all rows: ['SKEW-1','PLAIN-2','PLAIN-3']
  ```

  Then `/tmp/audit_wh/f34_bounded.py` case C, which I added: `AMEND-1` (created 2026-01-05,
  **modified 2026-08-10** — an ordinary in-place amendment, no clock skew), `PLAIN-2` (created
  2026-02-01, never amended), `fetch_limit: 500`, through the durable `_BoundedIngest` with
  `batch_limit=1`:

  ```
  round 0: in=2026-01-01… ingested=['AMEND-1'] has_more=True  out=2026-08-10T00:00:00+00:00
  round 1: in=2026-08-10… ingested=['AMEND-1'] has_more=False out=2026-08-10T00:00:00+00:00
  EVER: ['AMEND-1'] | all: ['AMEND-1','PLAIN-2']
  ```

- **Why**: case B reproduces the finding exactly, and I confirmed the algebra it rests on is
  complete rather than anecdotal: `watermark = COALESCE(mod, created)` and
  `entry_window = max(created, mod)` are *equal* whenever `mod` is NULL or `mod >= created`, and
  differ **only** when `mod < created`. So the finding has correctly isolated the one input class
  that separates the two, and its consequence — the cursor lands above the page's own maximum
  watermark, and everything the LIMIT cut off between the two is permanently below the next
  `>= cursor` — is what my run shows. `entry_window`'s docstring (`ingest/eln/adapter.py:89-92`)
  says the `max` "is cheap insurance and no test can distinguish the two"; case B is that test, and
  it distinguishes them by two lost rows.

  Where the finding is *understated*: it presents clock skew as the necessary precondition. Case C
  shows the same root defect — more than one definition of "the timestamp this row was fetched by" —
  losing a row with **no skew at all**, through the durable wrapper that production actually uses.
  `_BoundedIngest.fetch_new_entries` re-sorts the page by `created_at` and truncates
  (`durable/eln_sync.py:110-116`), while the SQL ordered it by the amendment watermark; the kept row
  carries the later `entry_window`, the cursor jumps to it, and `PLAIN-2` — which the SQL had
  returned in the very same page — is below the next `>= cursor` forever. That is a third disagreeing
  notion of the watermark on the same axis, and it means the finding's own fix ("one definition of
  the timestamp this entry was fetched by", plus "cap the cursor at the maximum watermark actually
  returned by the page") is the right shape but has to cover `_BoundedIngest` too, which the finding
  does not mention.
