# core infra — security and hardening (round 1)

Slice: `src/chemclaw/core/{db,migrate,grants,http,egress,logging,metrics,metrics_bridge,embeddings,temporal_client,identity_context,turn_signals}.py`

All reproductions below were run in this environment with `uv run`. Scripts under `/tmp`.

---

## `llm_fallback_api_key` is a live credential outside the redaction inventory

- **Severity**: high
- **Location**: `src/chemclaw/core/logging.py:449-464` (`_SECRET_SETTINGS`); the field is
  `src/chemclaw/core/config/llm.py:70`, consumed at `src/chemclaw/agent/llm_provider.py:251`
- **Trigger**: set `CHEMCLAW_LLM_FALLBACK_API_KEY` (the AG-12 failover credential) and let any
  record carrying it reach a handler — a `repr(settings)` in a traceback, an SDK error quoting its
  own configuration, a `logger.error("... %s", key)`.
- **Consequence**: the second LLM endpoint's API key is written to stdout in the clear, and under
  `CHEMCLAW_LOG_JSON=true` into the cluster's log store. The primary key on the *same line* is
  scrubbed, so the log reads as if redaction is working. `_SECRET_SETTINGS` is a hand-maintained
  list whose own comment calls itself "a credential inventory … visible in review"; the fallback
  credential was added to `LlmSettings` and never added here.
- **Evidence**: `_SECRET_SETTINGS` names `llm_api_key` and no fallback. Measured
  (`/tmp/repro2.py`, real `Settings` instance):

  ```
  primary key still in redacted repr(settings)?  False
  fallback key still in redacted repr(settings)? True
      llm_api_key='***'
      llm_fallback_api_key='fallbackKEY0123456789abcd'
  ```

  Nothing else catches it either: it has no vendor prefix, so `_STRUCTURAL_SECRETS` only fires if
  the value happens to sit behind a literal `api_key=` / `Bearer ` anchor *and* contains a digit.
- **Fix**: add `"llm_fallback_api_key"` to `_SECRET_SETTINGS`. Better, stop maintaining the list by
  hand for `Settings` fields: mark credential fields with a pydantic `Field(json_schema_extra={"secret": True})`
  (or a `SecretStr` type) and derive `_SECRET_SETTINGS` from `model_fields`, so adding a credential
  to config cannot silently skip the inventory. The module comment argues against deriving from a
  *name pattern*, which is a different and correct argument.

---

## Turn signals carry raw exception text to the browser with no redaction

- **Severity**: medium
- **Location**: `src/chemclaw/core/turn_signals.py:85-110` (`ToolFailureSignal`), `:150-174`
  (`_emit`), `:221` (`record_tool_failure`); producer `src/chemclaw/agent/tool_authz.py:136-138`
  (`failure_detail`) and `:364`; consumer `src/chemclaw/api/graph_stream.py:413-414` →
  `ToolFailedEvent` (`src/chemclaw/api/events.py:260-271`).
- **Trigger**: any tool whose failure message embeds a URL with userinfo or a credential-bearing
  upstream body. `failure_detail` is literally `f"{type(exc).__name__}: {exc}"[:N]` — no filtering —
  and `_emit` publishes the model verbatim onto the turn's custom stream, which
  `graph_stream` turns into a `tool_failed` SSE event.
- **Consequence**: `redact_secrets` exists in this same kernel and is documented as the redaction
  "exposed so anything that *persists* an error message can apply the same one". It is applied to
  log records and (per its docstring) to `note_proposals.reason`. It is **not** applied to the one
  channel that ships upstream error text straight to an end user's browser. The same string is
  scrubbed in the pod's log and delivered in the clear to the chemist.
- **Evidence**: `httpx` embeds the full request URL, userinfo included, in `HTTPStatusError`:

  ```
  detail   : HTTPStatusError: Client error '401 Unauthorized' for url
             'https://svc:s3cr3tPassw0rd@nonexistent.invalid/v1/embeddings' | ...
  leaks?    True
  redacted : HTTPStatusError: Client error '401 Unauthorized' for url
             'https://svc:***@nonexistent.invalid/v1/embeddings' | ...
  ```

  `core/embeddings.py:80-84` states explicitly that `llm_base_url` "is a plain `str` with no
  validator forbidding userinfo, so `https://svc:token@chemclaw-llm/v1` is a configuration this
  deployment accepts" — i.e. this exact shape is a supported config, and the embedding/LLM path is
  reached from tools. `connectors/qm/hpc/nextflow.py:116` similarly puts up to 500 characters of an
  upstream body into a `NextflowError` message.
- **Fix**: apply `redact_secrets` inside `_emit` (or in `record_tool_failure` / the
  `ToolFailureSignal` validator) so every free-text signal field passes the same redactor a log line
  does. One call, at the single publish point, covers every producer.

---

## `register_secret_env` reads `os.environ`, but `Settings` also loads `.env` — same secret, two outcomes

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:474-496` (`_RUNTIME_SECRET_ENVS`,
  `register_secret_env`) and `:741-748` (`_secret_values`'s `os.environ.get`); the registrant is
  `src/chemclaw/retrieval/vectors/qdrant.py:114`; the field is
  `src/chemclaw/core/config/store.py:151`. `env_file=".env"` is set at
  `src/chemclaw/core/config/__init__.py:144-149`.
- **Trigger**: put `CHEMCLAW_VECTOR_STORE_API_KEY=<key>` in `.env` instead of the process
  environment. `Settings.vector_store_api_key` is populated (pydantic-settings reads `.env`), but
  pydantic-settings does **not** write to `os.environ`, so `_secret_values`' lookup returns `""`.
- **Consequence**: the Qdrant credential is never redacted, while the identical value supplied as a
  real environment variable is. The redaction of a `Settings` field depends on which of two
  documented, supported config sources the operator chose — a property no operator can see. The
  config comment at `store.py:147-150` asserts the key is "Registered with the log-redaction
  inventory … so a client echoing its own configuration into a traceback cannot put the key in a
  log"; that guarantee is conditional and the condition is unstated.
- **Evidence**: `/tmp/envtest/t3.py`, one script, two invocations (lines chosen with no
  `api_key=`-shaped anchor so the structural backstop cannot mask the result):

  ```
  # value supplied via .env
  in os.environ: False | redacted: unauthorized: credential qdrantSecretFromDotEnv123 was rejected ...
  in os.environ: False | redacted: Settings(vector_store_api_key='qdrantSecretFromDotEnv123')
  # identical value supplied as an environment variable
  in os.environ: True  | redacted: unauthorized: credential *** was rejected ...
  in os.environ: True  | redacted: Settings(vector_store_api_key='***')
  ```

  Secondary, same root cause: registration happens *at first read* (`open_qdrant_client`), so every
  log line emitted before the first Qdrant client is built is unprotected even in the env-var case.
- **Fix**: for a value that *is* a `Settings` field, redact the field, not the variable name — i.e.
  add `vector_store_api_key` to `_SECRET_SETTINGS` (or to the derived inventory proposed in the
  first finding) and keep `register_secret_env` only for values with no field at all (the ELN
  warehouse manifest case, `ingest/eln/warehouse/connect.py:69`, which is genuinely env-only).

---

## The calc server bearer is in neither inventory, and the `Bearer` backstop drops digit-free tokens

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:626` (the `Bearer|Token` rule) and `:592`
  (`_HAS_DIGIT`); the credential is `src/chemclaw/core/config/calculators.py:166`
  (`calc_server_token_env`), read at `src/chemclaw/connectors/calc/remote.py:166` and put into a
  header at `:113`.
- **Trigger**: `CHEMCLAW_CALC_TOKEN=<token>` reaching a log line. Two sub-cases:
  1. Any spelling that is not `Bearer <token>` — e.g. a header dict logged as
     `token=%s`, or an upstream error quoting the value alone.
  2. `Bearer <token>` where the token contains no digit.
- **Consequence**: the bearer that authenticates this repo to the out-of-release calculation MCP
  server is written to logs in the clear. It is covered by neither inventory: it has no `Settings`
  *value* field (only a variable **name**), nothing calls `register_secret_env` for it, and the
  connector-manifest resolution in `SecretRedactingFilter.__init__` (`logging.py:792-801`) does not
  see it — `config/calculators.py:155-161` states plainly that the calc server is "Not a connector
  bundle, deliberately", so it has no manifest to be discovered through.
- **Evidence**: `/tmp/repro_redact.py` and `/tmp/repro2.py`:

  ```
  bare form       : calc auth failed for token calcTOK0123456789abcdefgh      # unredacted
  header form     : {'Authorization': 'Bearer ***'}                            # caught, has a digit
  no-digit bearer : {'Authorization': 'Bearer alphaonlytokenvaluexyz'}         # unredacted
  ```

  The digit requirement is a deliberate, argued trade at `logging.py:550-555`, and the comment
  bounds the cost as "a token of pure letters (rare, and still covered by the value inventory when
  this process holds it)". For this credential the value inventory does **not** hold it, so the
  stated backstop does not exist and the trade is unbounded.
- **Fix**: call `register_secret_env(settings.calc_server_token_env)` in
  `connectors/calc/remote.py::_token`, at the read — the placement `logging.py:488-494` prescribes.
  (And prefer the field-based inventory from finding 1 if the token is ever promoted to a setting.)

---

## `_endpoint_slot` logs `llm_base_url` on a redaction guarantee that does not hold for every password

- **Severity**: low
- **Location**: `src/chemclaw/core/embeddings.py:113-132` (the `log.info` at `:129`), relying on
  `src/chemclaw/core/logging.py:519-521` (`_URL_USERINFO`)
- **Trigger**: `CHEMCLAW_LLM_BASE_URL=https://svc:s3c/r3t@chemclaw-llm/v1` — a userinfo password
  containing an unescaped `/`. `_URL_USERINFO`'s password group is `[^/\s@]{0,512}`, which stops at
  the `/` and then fails to reach the `@`, so the whole alternative fails and nothing is replaced.
- **Consequence**: the docstring's justification for logging the URL at all — "The URL itself is
  safe to log because `SecretRedactingFilter` strips userinfo from every record that reaches a
  handler" — is false for this password shape, and `_endpoint_slot` emits the credential once per
  process at INFO. This is exactly the case the surrounding design went to trouble to avoid for the
  persisted column.
- **Evidence**: `/tmp/repro2.py`

  ```
  userinfo      : embedding endpoint https://svc:***@chemclaw-llm/v1 is recorded as ep-x
  userinfo w/ / : endpoint https://svc:s3c/r3t@chemclaw-llm/v1
  ```
- **Fix**: log the *digest* and the host, not the full URL — `log.info("embedding endpoint %s is
  recorded as %s", urlsplit(base_url)._replace(netloc=host).geturl(), digest)` — or strip userinfo
  at the call site rather than delegating to a pattern that can miss. The operator need
  (digest → which endpoint) is served by scheme+host+path.

---

## The migration drift guard does not cover the one file executed on every run

- **Severity**: low
- **Location**: `src/chemclaw/core/migrate.py:172-175`
- **Trigger**: change the contents of `infra/sql/000_schema_migrations.sql` (or point
  `CHEMCLAW_SQL_MIGRATIONS_DIR` at a tree with a different one) and run `make db-migrate` against a
  database that has already migrated.
- **Consequence**: `await conn.execute(sources[_LEDGER_FILE])` runs unconditionally, before the
  loop, and the loop `continue`s past the ledger file, so its checksum is never recorded and never
  compared. Every other file's post-apply edit is refused with `MigrationError`; the ledger's is
  executed silently, as the schema-owning `postgres_migration_dsn` principal, on every deploy. The
  file's own header comment claims the guard's coverage ("`checksum` is the SHA-256 of the file text
  at apply time, so editing a file that has already run is detected as drift") without noting the
  exception it is describing itself as.
- **Evidence**: `migrate.py:173` executes it before any ledger read; `:174-176` skips it. The
  shipped `infra/sql/000_schema_migrations.sql` is a bare `CREATE TABLE IF NOT EXISTS`, so nothing
  is currently wrong — the gap is that nothing would notice if it stopped being that.
- **Fix**: record the ledger file in `schema_migrations` too, after the bootstrap `execute`, and run
  the same legacy/drift comparison against it on subsequent runs. It bootstraps its own row on the
  first pass, which is the standard shape.

---

## `error_detail`'s docstring names callers that do not exist

- **Severity**: low
- **Location**: `src/chemclaw/core/http.py:6-9`
- **Trigger**: read the module docstring, then `grep -rn "error_detail" src/`.
- **Consequence**: the docstring justifies the module's existence with "several modules (the
  Nextflow launcher, the Entra token/OBO exchanges)". Only `connectors/qm/hpc/nextflow.py` calls it
  (three sites). The Entra half is gone. This matters under this lens because the security argument
  in the same docstring — "On a failed request an OAuth/launcher body carries an error description,
  not a credential, so a bounded excerpt is safe" — is calibrated for an OAuth exchange that no
  longer happens, while the surviving caller is a Seqera/Tower API whose body is not covered by that
  reasoning and whose output flows into `NextflowError` (see finding 2).
- **Evidence**:
  ```
  $ grep -rn "error_detail" --include=*.py src/ | grep -v "def error_detail"
  src/chemclaw/connectors/qm/hpc/nextflow.py:19,116,134,164
  src/chemclaw/core/http.py:6
  ```
- **Fix**: correct the docstring to name the one real caller, and either state the safety argument
  for a Tower response body or pass the excerpt through `redact_secrets`.

---

# Verified negatives (claims that hold)

Recorded because each was a load-bearing comment I checked rather than took, and each held.

- **No ReDoS in `SecretRedactingFilter`.** The module makes a strong, measured claim about bounded
  tails (`logging.py:559-592`). Re-measured on the current patterns via `redact_secrets`, 15 hostile
  inputs × 3 sizes (10 KB → 40 KB → 160 KB): every case scales linearly. Worst constant is a
  `x://b*20:c*500` repeat at 16.9 ms / 72.6 ms / 295.7 ms for 4× input each step — 4× time for 4×
  input. Nothing quadratic remains. (`/tmp/redos.py`)
- **`is_loopback_url` never answers True for a non-loopback host.** Probed 17 spellings including
  `http://localhost@evil.example.com/`, `http://evil.example.com#@localhost/`,
  `http://localhost\@evil.example.com/`, `http://%6c%6f%63%61%6c%68%6f%73%74/`,
  `http://[::ffff:127.0.0.1]/`, `http://2130706433/`, `http://127.1/`. Every non-loopback case is
  `False`; every miss (`127.0.0.2`, `127.1`, decimal/hex forms) falls on the credential-demanding
  side, which is the direction the docstring claims. (`/tmp/loop.py`)
- **`_trace_config` sets every hide flag the installed `TraceConfig` declares.** Diffed the 14 names
  written out at `logging.py:413-436` against `dataclasses.fields(openinference.instrumentation.TraceConfig)`:
  zero `hide_*` fields unset, zero names that are not real fields.
- **`connect_options()` omitting `tls` with `temporal_api_key` set is not a plaintext-credential
  bug.** `temporalio.service.ConnectConfig._to_bridge_config` enables TLS by default when
  `api_key is not None and tls is None` (read from the installed distribution), so a Cloud API key
  cannot ride an `http://` target through this path.
- **`core/db.py::_redact` covers every DSN spelling I could construct** — URL userinfo, URL
  `?password=`, libpq `key=value`, percent-encoded password, and an unparseable string (replaced
  wholesale with `<postgres>`).
- **`apply_vector_recall_settings` and `existing_tables` are fully parameterized.** No SQL string
  interpolation anywhere in this slice; `set_config(name, value, true)` over `unnest` is the correct
  way to avoid interpolating an operator-supplied `SET` value.
- **`Metrics` label cardinality is bounded** (`_MAX_SERIES_PER_COUNTER = 64`) and `_escape` covers
  the three characters the exposition format defines; no declared label value derives from request
  input.

# Not findings, noted

- `SecretRedactingFilter.__init__` fails **open** (`logging.py:802-819`): a broken connector
  manifest leaves connector bearer tokens unscrubbed for the process's whole life. This is argued at
  length, logged at ERROR, and counted on `chemclaw_degraded_total{subsystem="log_redaction"}`, so
  it is a visible, alertable degradation rather than a silent one. I am not raising it, but it is
  the one deliberate fail-open in the slice and the three inventory gaps above all widen its blast
  radius — the credentials in findings 1, 3 and 4 have *no* counter when they leak.
