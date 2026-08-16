# agent state slice — CORRECTNESS

Slice: `src/chemclaw/agent/{session_store,preferences,scratchpad,attachments,turn_cost_store,durable_tools,verifier,profile_discovery,message_migration}.py`

Environment used: dockerd + `make up` + `make db-migrate` (live Postgres/Temporal), `uv run`.
Every finding below was reproduced by running code; the printed output is quoted verbatim.

Checked and found sound (so the negative result is on record): `SessionOwnerStore` /
`SessionTurnClaims` / `PostgresHistoryProvider` round-trip and lease semantics (driven against live
Postgres — claim/refresh/release, takeover after expiry, `IS NOT DISTINCT FROM` owner match,
`updated_at` ordering, no-message sessions excluded, `AIMessage(tool_calls=…)`/`ToolMessage`
round-trip through `message_to_dict`/`message_from_row`); `turn_cost_store`'s upsert (`correlation_id`
is a per-turn `uuid4`, so the "count a turn twice" hazard the docstring names is real and closed);
`convert_stored_messages`' resumability and its growing `LIMIT batch + len(refused)` (terminates,
converts nothing twice); `_report_id`'s hash payload (JSON-encoded, so the un-delimited
sections/actor/roles concatenation is not actually ambiguous); `_ParseSlots`' slot accounting
(take/hand-off/withdraw/give-back balance in every path I could construct); `profile_discovery`'s
idempotence.

---

## `memory_store()` publishes the store before its migrations have run — concurrent first turns get an unusable store

- **Severity**: high
- **Location**: `src/chemclaw/agent/scratchpad.py:147-156` (`memory_store`)
- **Trigger**: two turns on one process reach `api/runner._turn_store()` → `memory_store()` while the
  `store*` tables are absent or behind (a fresh deployment, or any langgraph bump that adds a row to
  `AsyncPostgresStore.MIGRATIONS`). Requires `agent_memory_enabled=true` + `session_store=postgres`.
- **Consequence**: the second and later callers get a store whose schema is half-created, and their
  memory writes fail. The chemist's durable memory silently does not get written on the first turns
  after a deploy. This is the *identical* defect `agent/checkpointer.py` documents at length and
  fixed with `_init_lock` ("Publishing before the await is what made this a race rather than a style
  question… a second turn arriving inside either await saw a non-`None` global and got a saver whose
  tables do not exist yet") — the sibling initializer never got the same treatment.
- **Evidence**: the global is assigned *before* the awaited `setup()`, and there is no lock, so two
  callers also both pass the `is None` check:

  ```python
  global _store
  if _store is None:
      _store = AsyncPostgresStore(await _checkpoint_pool())   # published here
      await _store.setup()                                    # …usable only after here
  ```

  Reproduced (`/tmp/audit/t_store_race.py`: drop `store`, `store_vectors`, `store_migrations`,
  `vector_migrations`, then four concurrent `memory_store()` + `aput`):

  ```
  dropped store tables
   -> ok0
   -> UndefinedColumn: column "expires_at" of relation "store" does not exist
   -> UndefinedColumn: column "expires_at" of relation "store" does not exist
   -> UndefinedColumn: column "expires_at" of relation "store" does not exist
  ```

  Re-running once the tables exist gives `['ok0','ok1','ok2','ok3']`, confirming the window is the
  cold-start / new-migration one and nothing else.
- **Fix**: give `memory_store()` the same shape `checkpointer()` already has — take
  `checkpointer._initialization_lock()`, re-check under it, build into a local, `await setup()`, and
  only then assign the global:

  ```python
  global _store
  if _store is not None:
      return _store
  async with _initialization_lock():
      if _store is None:
          store = AsyncPostgresStore(await _checkpoint_pool())
          await store.setup()
          _store = store
  return _store
  ```
  (The lock is already shared-loop-scoped and dropped by `close_checkpointer`, so this needs no new
  primitive. It is also not reentrant, so `memory_store` must be called outside `checkpointer()`,
  which it is.)

---

## `read_attachment` returns the *stale* file when a chemist re-uploads a corrected file under the same name

- **Severity**: medium
- **Location**: `src/chemclaw/agent/attachments.py:322-331` (`AttachmentStore.add`) and `:383-401`
  (`read_attachment`); the route at `src/chemclaw/api/routes/sessions.py:182` (`ATTACHMENTS.add`)
- **Trigger**: upload `data.csv`, notice an error, upload the corrected `data.csv` to the same
  session, then ask the agent about the file.
- **Consequence**: `add` appends without de-duplicating by name, and `read_attachment` returns the
  **first** name match from `for_session` (which is oldest-first). The model reads the superseded
  version and answers from it. `list_attachments` shows two entries with the identical `name`, so
  the model has no handle that reaches the corrected file at all — the newer upload is
  unaddressable. This is the "wrong answer from stale data" case, not a cosmetic one: the two files
  differ in exactly the numbers the chemist re-uploaded to fix.
- **Evidence**: `/tmp/audit/t_attach.py` (two parses of `data.csv`, yields 50 then 95):

  ```
  read_attachment -> '<retrieved-note-… id="attachment:data.csv">\nrun | yield\n---\n1 | 50\n</…>'
  listed: data.csv '…1 | 50…'
  listed: data.csv '…1 | 95…'
  ```
  The loop that produces this: `for attachment in STORE.for_session(session_id): if attachment.name == name: return …`.
  The same collision is reachable without a re-upload, because `_safe_name` is many-to-one:
  `report(1).pdf` and `report_1_.pdf` both sanitize to `report_1_.pdf`.
- **Fix**: make the name the key. Either replace an existing same-name entry in `add`
  (`items = [i for i in items if i.name != attachment.name] + [attachment]`), or have
  `read_attachment` scan `reversed(...)` so the newest wins *and* have the upload route disambiguate
  the name it returns (e.g. `data-2.csv`) so the listing has one handle per file. Replacing is the
  smaller change and matches what a chemist means by re-uploading the same filename.

---

## `recall_preferences` silently reports a *partial* preference set as the complete one during a database outage

- **Severity**: medium
- **Location**: `src/chemclaw/agent/preferences.py:97-120` (`PreferenceStore.recall`)
- **Trigger**: `session_store=postgres`; the chemist has N preferences stored in Postgres; this
  process has written ≥1 of them in its own lifetime (so `_memory` is non-empty for that owner); the
  read then fails (broker/database blip, pool timeout, statement timeout).
- **Consequence**: the guard only covers the *zero* case. With one in-process entry, `recall`
  swallows the error and returns that single preference. The tool's own model-facing docstring says
  "Every preference this chemist has set, key-sorted", so the model is told, with no signal of
  degradation, that a chemist whose standing constraint is "no chlorinated solvents on scale" has no
  such constraint. That is precisely the failure the in-code comment argues against — "A wrong
  answer is worse than a failed one, because the chemist then re-states preferences that also will
  not persist" — applied only to the empty case, where an *incomplete* answer is the more dangerous
  one because it looks authoritative.
- **Evidence**: the guard is a membership test, not a completeness test:
  ```python
  except Exception:
      logger.warning(...)
      if not any(row_owner == owner for row_owner, _key in self._memory):
          raise
  # falls through to return whatever this process happens to hold
  ```
  Reproduced (`/tmp/audit/t_pref.py`, `_connection` patched to raise `ConnectionError`, `_memory`
  holding one of Anna's five preferences):
  ```
  recall after DB outage -> [('units', 'mol%')]
  empty-memory fallback: raised ConnectionError
  ```
  Note also that `_memory` is never populated from a *successful* read, so it is never a superset and
  the partial case is the normal one during an outage, not an edge.
- **Fix**: raise on any read failure in Postgres mode. `_memory` in Postgres mode is a write-through
  echo, never an authoritative replica, so it cannot answer "what has this chemist set". If a partial
  answer is wanted, it must be labelled — return a marker preference/flag the tool renders as
  "preferences could not be read; this list may be incomplete" rather than an unmarked list.

---

## `close_checkpointer()` invalidates the memory store without clearing it; `close_memory_store()` has no callers anywhere

- **Severity**: low
- **Location**: `src/chemclaw/agent/scratchpad.py:159-166` (`close_memory_store`), pairing with
  `src/chemclaw/agent/checkpointer.py:379` (`close_checkpointer`)
- **Trigger**: any process that calls `close_checkpointer()` and then takes another turn — today
  that is the test suite (`tests/test_checkpointer_schema.py`, `tests/test_message_migration.py`)
  and any future orderly-shutdown/loop-restart path.
- **Consequence**: `close_checkpointer` closes the pool the store borrows, but `_store` stays cached,
  so `memory_store()` hands back a store over a closed pool and every durable memory write raises
  `PoolClosed`. `close_memory_store` exists exactly to prevent this and is dead code:
  `grep -rn "close_memory_store"` over the whole repo (excluding `.venv`) matches only its own
  definition and the docstring beside it — no caller in `src/`, none in `tests/`.
- **Evidence**: `/tmp/audit/t_store_stale.py`:
  ```
  first use ok
  same object returned: True
  second use -> PoolClosed the pool 'pool-1' is already closed
  ```
- **Fix**: `close_checkpointer()` should drop the borrower it invalidates — either call
  `scratchpad.close_memory_store()` from it (a `agent.scratchpad` import inside the function, the
  direction the pool already flows), or have `memory_store()` validate `_store`'s pool is open before
  returning it. Leaving a `close_*` function with no caller as the sole enforcement of the invariant
  is the shape this repo elsewhere calls "a claim that a control exists".

---

## `ungrounded_parameter_shapes` joins tool outputs with `\n`, so a parameter shape can be "grounded" by two outputs that each contain half of it

- **Severity**: low
- **Location**: `src/chemclaw/agent/verifier.py:582` (`seen = "\n".join(tool_outputs)`)
- **Trigger**: two tool results in one turn where the tail of one and the head of the next form a
  matching shape across the join — every pattern in `_PARAMETER_SHAPES` uses `\s*`, and `\s` matches
  `\n`.
- **Consequence**: the gate is suppressed for that whole shape class, so a fabricated flow
  rate / gradient / limit in the answer passes unflagged. It is a false *negative* in a check whose
  documented purpose is catching a fabricated chromatographic method, and it is invisible: nothing
  logs a near-miss.
- **Evidence**: `/tmp/audit/t_shapes.py` — answer `"Use a flow rate of 1.2 mL/min on a Kinetex
  column."`, outputs `["batch B-17 was run at 5", "mL/min conversions are in the appendix",
  "Kinetex C18"]`. No single output contains a flow rate:
  ```
  fired: []
  fired (separator that cannot be crossed): ['flow rate: 1.2 mL/min', 'column brand: Kinetex']
  ```
  The second line is the same call with a separator the regexes cannot span, showing the join is the
  only reason the class was suppressed.
- **Fix**: ask the question per output rather than over a concatenation:
  ```python
  if match is None or any(pattern.search(out) for out in tool_outputs):
      continue
  ```
  Same cost, and it says what the docstring says it says ("no tool result in the turn contains that
  same class anywhere").
