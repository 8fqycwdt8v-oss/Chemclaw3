# Round 1 — `agent/` state & store modules, design & simplification lens

Slice: `session_store.py`, `preferences.py`, `scratchpad.py`, `attachments.py`, `turn_cost_store.py`,
`durable_tools.py`, `verifier.py`, `profile_discovery.py`, `message_migration.py`.

All files read in full. Findings below are ordered by severity. Every "dead" claim was checked
against the whole `src/` + `tests/` tree with an AST-level call scan (not grep alone), and against
the dynamic-registration paths this repo actually uses (`@tool` registry, Temporal workflow
registration, `durable.publish._BAD_DATA_TYPES` name matching, `__all__` re-exports).

---

## `verifier.py` carries a three-function dead parameter chain whose docstrings justify it with a deleted feature

- **Severity**: medium
- **Location**:
  - `src/chemclaw/agent/verifier.py:399` (`score_answer(..., evidence=None)`)
  - `src/chemclaw/agent/verifier.py:435` (the only site that forwards it)
  - `src/chemclaw/agent/verifier.py:534,547` (`verify_turn_answer(..., evidence=None)` and the
    `evidence if evidence is not None else turn_evidence(...)` branch)
  - `src/chemclaw/agent/verifier.py:533` (`verify_turn_answer(..., client=None)`)
- **Trigger**: `score_answer` has exactly one production caller,
  `src/chemclaw/api/runner_answer.py:49`:
  `review = await score_answer(answer, tool_outputs, tools_called)` — no `evidence`. No test passes
  it either. So `score_answer`'s `evidence` is always `None`, it forwards `None`, and
  `verify_turn_answer`'s `evidence is not None` branch is unreachable in the whole tree.
  `verify_turn_answer`'s `client` is passed by nobody at all — tests inject a client into
  `verify_answer` directly.
- **Consequence**: two keyword-only parameters, one dead branch and ~20 lines of docstring survive on
  the answer hot path, and the docstrings are *actively misleading* about why. `verify_turn_answer`'s
  says: *"`evidence` is that same argument one caller further out: the challenge panel needed the
  turn's evidence for its briefs **and** scores the answer, so without this it built the identical
  value twice — measured at 14 ms per build"*. The challenge panel was deleted (the module docstring
  at line 38-40 says so itself, two hundred lines above). A reader trying to establish who the second
  caller is has to re-derive the deletion. The `14 ms` saving is now a saving of zero.
- **Evidence**: AST scan over `src/` + `tests/` for calls to `score_answer` / `verify_turn_answer` /
  `verify_answer` carrying each keyword (`/tmp/probe1.py`):

  ```
  --- callers passing evidence=:
      src/chemclaw/agent/verifier.py:435 verify_turn_answer(evidence=...)   # score_answer forwarding its own None
  --- callers passing client=:
      src/chemclaw/agent/verifier.py:548 verify_answer(client=...)          # verify_turn_answer forwarding its own None
      tests/test_verifier.py:143 ... :950  verify_answer(client=...)         # 11 test sites, all on verify_answer
  ```

  So: zero callers of `score_answer(evidence=...)`, zero callers of
  `verify_turn_answer(client=...)`.
- **Fix**: delete `evidence` from `score_answer` and from `verify_turn_answer`; `verify_turn_answer`
  becomes `chunks = turn_evidence(answer, tool_outputs)` unconditionally. Delete `client` from
  `verify_turn_answer` (keep it on `verify_answer`, which tests genuinely inject into). Delete the
  two docstring paragraphs that argue for them. Behaviour-preserving: the removed branch is
  unreachable.

---

## `PreferenceStore._memory` is an unbounded, write-only cache of every user's preferences in the deployed configuration

- **Severity**: medium
- **Location**: `src/chemclaw/agent/preferences.py:56` (`self._memory`), `:84` (`remember`),
  `:99-120` (`recall`), `:130` (`forget`), `:146` (`_STORE = PreferenceStore()` — module global)
- **Trigger**: run with `session_store == "postgres"` (the shipped/deployed value). Every
  `remember_preference` tool call does `self._memory[(owner, key)] = value` at line 84 *before* the
  mode check at line 85, unconditionally. `recall` at line 99 takes the Postgres branch and only
  ever touches `_memory` inside the `except Exception` handler. `_STORE` is a module singleton with
  no eviction and no reset, so it lives for the process.
- **Consequence**: a front-door pod accumulates one entry per distinct `(owner, key)` for its entire
  uptime — every chemist who has ever set a preference on that replica, with the value — read only if
  Postgres throws. The class docstring calls this "an in-memory fallback for dev and tests"; in
  production it is a permanent shadow copy nobody reads. It is also the one place in this slice that
  holds per-user content outside a bounded structure: `attachments.AttachmentStore` next door
  deliberately uses `core.bounded.BoundedLru` for exactly this reason, and `preferences` does not.
- **Evidence**: `/tmp/probe2.py` — real `PreferenceStore`, `settings.session_store="postgres"`, a
  stub `db.connection` so the writes "succeed":

  ```
  session_store = postgres
  _memory entries after 5000 postgres-mode writes: 5000
  recall(user-0) from postgres (stub returns no rows): []
  ```

  5000 users' preferences retained in process memory; the happy-path read returns the (empty)
  Postgres answer and never consults them.
- **Fix**: make `_memory` the *store* only when it is the configured store. Move the write behind the
  mode check:

  ```python
  async def remember(self, owner, key, value):
      if settings.session_store != "postgres":
          self._memory[(owner, key)] = value
          return True
      ...
  ```

  and have the `recall` failure path raise rather than fall back (it already raises when `_memory`
  holds nothing for the owner — under this change that is always). Same for `forget`. Not strictly
  behaviour-preserving: it removes the "Postgres write failed but this session still sees the value"
  case — which is precisely the case `remember`'s own return value already exists to tell the chemist
  about ("Remembered … FOR THIS SESSION ONLY"). If that case is wanted, keep it but bound it with
  `BoundedLru` and say so in the docstring.

---

## `scratchpad.STORE_TABLES` documents two consumers, has neither, and is used only to print `2` in a log line

- **Severity**: medium
- **Location**: `src/chemclaw/agent/scratchpad.py:84-88` (the constant and its comment), `:155` (its
  only use)
- **Trigger**: read the comment, then search for readers.
- **Consequence**: the comment states *"two things need the list and neither can derive it. The
  erasure sweep has to delete a departing person's memories, and the retention sweep has to prune
  them by age."* Both halves are false against the code:
  - the erasure sweep, `src/chemclaw/agent/leaver.py:158-159`, hard-codes the table names in its
    statement pairs (`DELETE FROM store_vectors …`, `DELETE FROM store …`) and imports only
    `memory_prefix` from this module;
  - the retention sweep does not touch them at all — `src/chemclaw/durable/retention.py:_PRUNABLE`
    is `{"session_events", "session_messages", "tool_result_blobs", "checkpoints"}`, and its comment
    says the register is "explicit and closed: a new table is a deliberate addition here, never
    something a wildcard sweeps up".

  The only surviving use is `len(STORE_TABLES)` in `logger.info("memory store ready (%d tables)")`,
  i.e. a hard-coded `2` dressed as a derived value. A future edit adding a third store table would
  update this constant, both documented consumers would silently continue not using it, and the log
  line would be the only thing that changed.
- **Evidence**:
  ```
  $ grep -rn "STORE_TABLES" src/ tests/ --include=*.py
  src/chemclaw/agent/scratchpad.py:88:STORE_TABLES: tuple[str, ...] = ("store", "store_vectors")
  src/chemclaw/agent/scratchpad.py:155:        logger.info("memory store ready (%d tables)", len(STORE_TABLES))
  ```
  and `retention.py:122-126` shows `_PRUNABLE` without `store`/`store_vectors`.
- **Fix**: either make the claim true — have `leaver.py` build its two statements from
  `STORE_TABLES` and add the two tables to `_PRUNABLE` — or delete the constant and log
  `"memory store ready"`. Do not leave a constant whose stated justification is fabricated. The
  delete-and-simplify half is behaviour-preserving; wiring retention to it is not (it starts deleting
  rows), which is itself the point: the comment currently asserts a retention policy that does not
  exist.

---

## Session-layer connection plumbing is cloned four ways, one clone is a no-op wrapper, and a `dsn` override deleted from one class survives in two others

- **Severity**: medium
- **Location**:
  - `src/chemclaw/agent/session_store.py:200-228` (`_session_dsn`, `_session_connection`)
  - `src/chemclaw/agent/session_store.py:240-246`, `:316-322`, `:410-416` — three byte-identical
    `__init__` + `_connection` pairs
  - `src/chemclaw/agent/preferences.py:53-69` — a fourth private copy, with its own
    `@asynccontextmanager`
  - `src/chemclaw/agent/turn_cost_store.py:57-59` — a fifth
  - `src/chemclaw/agent/session_events.py:72` — a sixth
- **Trigger**: structural; visible by reading the four modules side by side.
- **Consequence** (three separate costs):
  1. **`_session_connection` adds nothing.** Its whole body is
     `async with db.connection(dsn) as conn: yield conn`. Every property its 10-line docstring
     claims — pooling, the "Postgres unreachable at <host>" message, the per-statement timeout — is a
     property of `core.db.connection` (`src/chemclaw/core/db.py:165-195`), documented there. The
     docstring says it was *"Extracted once the third store in this module needed the identical four
     lines"*; the four lines are one line, in `db`.
  2. **Three identical class members.** AST-normalised bodies of the three classes' `__init__` and
     `_connection`:
     ```
     PostgresHistoryProvider.__init__:   'self._dsn = _session_dsn()'
     PostgresHistoryProvider._connection:'return _session_connection(self._dsn)'
     SessionOwnerStore.__init__:         'self._dsn = _session_dsn()'
     SessionOwnerStore._connection:      'return _session_connection(self._dsn)'
     SessionTurnClaims.__init__:         'self._dsn = _session_dsn()'
     SessionTurnClaims._connection:      'return _session_connection(self._dsn)'
     ```
     Three classes carrying one field and one accessor each, all resolving the same global config,
     none of which can differ.
  3. **The deleted parameter survives twice.** `_session_dsn`'s docstring records that a `dsn`
     override was removed because *"all twenty [call sites] construct these classes with no
     arguments … A parameter nothing passes is a parameter that documents a capability the deployment
     does not have."* The identical parameter is still on `PreferenceStore.__init__(dsn=None)`
     (`preferences.py:53`) and on `session_events._dsn(dsn)` /
     `record_session_event(..., dsn=None)` / `claim_unconsumed(..., dsn=None)`. Confirmed: zero call
     sites in `src/` or `tests/` pass either — all ten `PreferenceStore(` constructions are
     `PreferenceStore()`, and no caller of `record_session_event`/`claim_unconsumed` passes `dsn=`.
     The argument that removed one applies verbatim to the other two, and the surviving copies are
     what make a reader believe the deployment can split these databases.
- **Evidence**: AST dump above (`/tmp/probe1.py`);
  `grep -rn "PreferenceStore(" src/ tests/` → 11 sites, all zero-arg;
  `grep -rn "session_store_dsn" src/` → the same `settings.session_store_dsn or settings.postgres_dsn`
  expression written out at 5 places (`session_store.py:212`, `preferences.py:55`,
  `turn_cost_store.py:59`, `session_events.py:72`, `evals/live.py:392`, plus `api/routes/ops.py:78`).
- **Fix**, all behaviour-preserving:
  - delete `_session_connection`; call `db.connection(...)` directly (it is the same object).
  - promote `_session_dsn()` to a public `session_dsn()` in `core.db` (or keep it in `session_store`
    and export it) and have `preferences`, `turn_cost_store`, `session_events`, `evals/live` and
    `routes/ops` call it instead of respelling the `or` chain.
  - collapse the three `__init__`/`_connection` pairs — either onto a tiny shared base, or by
    dropping `self._dsn` entirely and having each method call `db.connection(session_dsn())`, which
    also makes the DSN honour a reloaded setting instead of freezing it at construction.
  - drop the unused `dsn` parameters from `PreferenceStore.__init__`, `record_session_event` and
    `claim_unconsumed`, for the reason already recorded in `_session_dsn`'s own docstring.

---

## `turn_cost_store.read_spend_by_actor` — the function the table exists for — has no production reader

- **Severity**: medium
- **Location**: `src/chemclaw/agent/turn_cost_store.py:45-54` (`_SPEND_BY_ACTOR`), `:86-95`
  (`read_spend_by_actor`)
- **Trigger**: search the tree for readers.
- **Consequence**: `turn_costs` is write-only in every running surface. `PostgresTurnCostSink.record`
  is reached from `turn_cost.py:98`; `read_spend_by_actor` is reached from `tests/` only — no CLI
  command, no API route, no report, no metric exporter. Its docstring says *"The whole point of the
  table, expressed as the one query that answers it."* A ~10-line aggregate query and its public
  function are therefore an interface with a test suite and no user, and the table it reads grows on
  every turn with nothing that ever looks at it.
- **Evidence**:
  ```
  $ grep -rn "read_spend_by_actor" src/
  src/chemclaw/agent/turn_cost_store.py:86:async def read_spend_by_actor(...)
  ```
  (all other 9 hits are in `tests/test_postgres_turn_cost_store.py`.) Checked for dynamic reach:
  it is not `@tool`-registered, not a Temporal activity/workflow, not named in
  `api/app.py`'s route surface, not in any `__all__` consumed elsewhere.
- **Fix**: either give it the one caller it was written for — a `chemclaw` CLI subcommand
  (`chemclaw spend --days 30`) or an ops route beside `routes/ops.py` — or delete the function and
  the query and let the table be a pure ledger for out-of-band SQL. Deleting is behaviour-preserving;
  wiring is the better outcome and is one route handler.

---

## `history_provider()` returns `Any`; the two providers share no declared contract, and the two call sites disagree about whether the contract holds

- **Severity**: low
- **Location**:
  - `src/chemclaw/agent/session_store.py:231` (`PostgresHistoryProvider`), `:461`
    (`InMemoryHistoryProvider`)
  - `src/chemclaw/agent/chemclaw_agent.py:253` (`def history_provider() -> Any:`)
  - `src/chemclaw/api/runner.py:746` (`if not hasattr(history, "save_messages"): return`)
  - `src/chemclaw/api/routes/sessions.py:132` (`await ...history.get_messages(...)`, unguarded)
- **Trigger**: inject any object into `app.state.history` that implements `get_messages` but not
  `save_messages` — the runner tolerates it silently (line 746 returns), the transcript route calls
  it fine. Inject the mirror — `save_messages` only — and `GET /sessions/{id}/messages` raises
  `AttributeError` → 500. Nothing declares which is legal.
- **Consequence**: `history_provider`'s docstring asserts *"Both offer the same two primitives"* and
  nothing checks it: `-> Any` disables `mypy --strict` on every downstream use, and the compensation
  is a `hasattr` string check at one of the two call sites. The four method signatures also carry
  `**kwargs: Any` — a residue of the removed MAF `HistoryProvider` base class that the class
  docstring at `:234-238` says is gone — and no caller in `src/` or `tests/` passes a single extra
  keyword to either primitive (`state=` is passed, and it is a named parameter).
- **Evidence**: `grep -rn "\.save_messages(\|\.get_messages(" src/` → exactly two production call
  sites, shown above; only one guards. `**kwargs` receives nothing at either.
- **Fix**: declare a `HistoryProvider` `Protocol` (two methods, `state: dict | None`, no `**kwargs`)
  in `session_store.py`; annotate `history_provider() -> HistoryProvider` and
  `app.state.history`. Drop `**kwargs` from all four signatures. Drop the `hasattr` guard in
  `runner.py:746` — it is then a type error, caught by `make type` instead of at runtime. Behaviour-
  preserving except that a deliberately partial test double now fails typing, which is the point.

---

## `verifier` writes the same token-boundary regex twice, once as a helper and once inline

- **Severity**: low
- **Location**: `src/chemclaw/agent/verifier.py:479` (`_mentions`) and `:630` (inside
  `promised_uncalled_tools`)
- **Trigger**: structural. Both build `rf"(?<![\w-]){re.escape(X)}(?![\w-])"`.
- **Consequence**: `_mentions` carries a five-line docstring explaining why `-` is inside the
  boundary class (`playbook-degassing-old` must not ground `playbook-degassing`). The identical
  pattern at line 630 carries none, and a future correction to the boundary rule — the exact class of
  bug `_mentions`' docstring documents — would have to be applied twice or it silently diverges:
  `turn_evidence` would treat `reaction-1`/`reaction-12` one way and `promised_uncalled_tools` the
  other. The reason the second is inline is only that it needs `match.start()` for ordering, which is
  a return-type difference, not a logic difference.
- **Evidence**: the two lines are character-identical apart from the interpolated name.
- **Fix**: behaviour-preserving —
  ```python
  def _token_match(text: str, token: str) -> re.Match[str] | None:
      return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text)

  def _mentions(text: str, note_id: str) -> bool:
      return _token_match(text, note_id) is not None
  ```
  and `promised_uncalled_tools` uses `_token_match(answer, name)`. One regex, one docstring.

---

## `TurnReview.challenged` / `hold_id` are fields nothing writes — declared on a deadline that has no enforcement

- **Severity**: low
- **Location**: `src/chemclaw/agent/verifier.py:386-392`, read at
  `src/chemclaw/api/runner_answer.py:56-57`, mirrored at `src/chemclaw/api/events.py:252-257`
- **Trigger**: any turn. `score_answer` (`:432-469`) constructs `TurnReview()` and never assigns
  either field, so `challenged` is always `False` and `hold_id` always `None` on every code path.
- **Consequence**: two dead fields on the model, copied into two dead fields on the wire event, on
  every answer. The comment at `:384-390` is honest about it — *"Both of these are permanently at
  their defaults … They go in the same cut that retires the transcript route … This is the one shape
  the repo otherwise forbids … so it is on a deadline rather than left to be rediscovered."* The
  deadline is a sentence in a comment: there is no test, no `# TODO(date)`, no failing assertion,
  nothing that will make the cut happen. The mechanism that was supposed to prevent rediscovery is
  the thing being rediscovered here.
- **Evidence**: `grep -rn "challenged\|hold_id" src/` → 5 hits, all declarations and one
  pass-through copy; zero assignments outside the field defaults.
- **Fix**: if the three-repo cut is genuinely scheduled, add the enforcement the comment implies — a
  test asserting `TurnReview.model_fields["challenged"]` is absent after a named date, or an entry in
  whatever the repo uses to fail on expiry. If it is not scheduled, delete both fields here and keep
  them only on the SSE event (`api/events.py`), where the cross-repo contract actually lives; the
  agent layer does not need to carry a field it cannot set. Behaviour-preserving either way — the
  values never change.

---

## Smaller items, grouped

- **`attachments.__all__` re-exports `content_type_for` for a test and nobody else.**
  `src/chemclaw/agent/attachments.py:39,55` imports it from `chemclaw.ingest.documents.formats` and
  republishes it; the only importer of `chemclaw.agent.attachments.content_type_for` in the tree is
  `tests/test_document_formats.py:20`. `api/routes/sessions.py:13-19` and `cli/backfill_corpus.py:26`
  import other names. A re-export whose only consumer is a test is an API surface the deployment does
  not have; drop it from the import and `__all__` and let the test import from `ingest.documents.formats`
  where the function lives. Behaviour-preserving. (Severity: low.)

- **`message_migration` imports two private names out of `session_store`.**
  `src/chemclaw/agent/message_migration.py:274`:
  `from chemclaw.agent.session_store import _session_connection, _session_dsn`. `session_store`
  imports `message_migration` at module scope (`:51-55`), so the two modules are mutually dependent
  and the cycle is broken by a function-local import — whose docstring justifies it on purity
  grounds. A leading underscore is a statement that a name has no readers outside its module; this
  one has two, across a cycle. Combined with the DSN finding above: promote the resolver to a public
  `session_dsn()`, delete `_session_connection`, and `message_migration` imports one public name.
  Behaviour-preserving. (Severity: low.)

- **`convert_stored_messages`' refusal bookkeeping is quadratic and re-reads refused rows every
  batch.** `src/chemclaw/agent/message_migration.py:281-283`: the query is
  `LIMIT batch + len(refused)` — refused rows keep their `maf` stamp and so are re-selected on every
  iteration — and the filter rebuilds `set(refused)` once *per row* inside the comprehension. It is
  correct (refused rows are always the lowest-id `maf` rows because the pass runs in id order), but
  the correctness argument is nowhere in the code and takes a paragraph to reconstruct. Hoist the set
  (`seen = set(refused)` before the comprehension) and add the one sentence explaining why
  `batch + len(refused)` is the right limit — or, simpler and equally correct, keep a
  `last_id` cursor and select `WHERE message_shape = 'maf' AND id > %s`, which drops the
  re-selection and the set entirely. Behaviour-preserving. (Severity: low.)

- **`durable_tools` is named for tools and half of it is not tools.** Of its seven public symbols,
  `job_status`, `request_note_reindex` and `cancel_job` are explicitly *not* agent tools — the last
  two say so in their own docstrings — and are imported by `api/app.py:39` as front-door operations.
  The name misleads about what the module contains, which matters because the module docstring's own
  headline claim is about which things are tools. Not worth a move on its own; worth renaming to
  `durable_jobs.py` (or splitting the three operational functions into `agent/durable_ops.py`) the
  next time it is touched. Behaviour-preserving rename. (Severity: low.)

---

## What I checked and did *not* find a problem with

- `attachments._ParseSlots` (`:119-227`) is intricate — five methods, a hand-rolled counter, a
  waiter deque — and I could not find a simpler shape that keeps its stated properties. The two
  reasons it gives for not being an `asyncio.Semaphore` both check out: `asyncio.Semaphore` inherits
  `_LoopBoundMixin` and raises across loops, and the release genuinely must hang off the worker
  future's completion rather than off the awaiting request. The complexity is bought, not spent.
- `durable_tools._report_id` (`:108-158`) is long, but every clause in the key is load-bearing and
  the argument for each is checkable against the code (`requested_by` and `requested_roles` are in
  the payload; the model-written half is canonicalised, the entitlement half is not). No change
  proposed.
- `profile_discovery.py` is 110 lines, one responsibility, no duplication, no dead symbols
  (`ProfileError` reaches `durable/publish._BAD_DATA_TYPES:76` by class *name*, which is exactly the
  dynamic-registration case that would make a naive "unused" call wrong). `profile_files()` has only
  a test caller outside its own module but is the natural seam for `load_profiles` and is worth
  keeping public. Nothing to report.
- `scratchpad.memory_namespace` / `memory_prefix` look like a one-caller abstraction pair but are
  not: `memory_prefix` is the erasure key `leaver.py:295` builds, and the split is what stops the
  join being re-derived in two places. Correct as written.
