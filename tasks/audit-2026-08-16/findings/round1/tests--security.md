# tests/ — security and hardening lens (round 1)

Slice: the test suite itself — coverage holes, doubles that stand in for the real thing, vacuous
proofs. Lens: what untrusted input reaches, what fails open, what leaks.

Three findings. Two are places where the suite proves a security property for *one* instance of a
pattern and the identical pattern is live and untested elsewhere; one is a control with no test at
all. A short "checked and found sound" list is at the end, because several of the obvious targets
here are genuinely well covered and saying so is part of the result.

---

## The webhook signature check crashes on a non-ASCII header — the exact defect the connector's bearer check has a test for

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/proposals.py:66` (`_webhook_signature_ok`); the coverage
  gap is `tests/test_note_proposals.py:495-620` (six webhook tests, every offered signature
  ASCII hex)
- **Trigger**: `CHEMCLAW_NOTE_WEBHOOK_SECRET` set (the hardened posture), then
  `POST /events/knowledge-merged` with a header whose value is `sha256=` followed by any byte
  outside ASCII. Starlette decodes header bytes as latin-1, so this is a single raw byte on the
  wire.
- **Consequence**: `hmac.compare_digest` on `str` raises
  `TypeError: comparing strings with non-ASCII characters is not supported`. The request becomes an
  unhandled 500 with a traceback rather than the 401 the branch was written to produce. Repeatable
  at will by any principal that can reach the route (in the shipped dev posture, `entra_required`
  off, that is anyone), so it is also a lever on the 5xx rate an operator alerts on. The control
  still fails closed, but by exception rather than by decision — which is precisely the property
  `chemclaw/connectors/server.py:209-224` records as unacceptable for its own bearer comparison.
- **Evidence**:

  ```python
  # src/chemclaw/api/routes/proposals.py:60-66
  secret = settings.note_webhook_secret
  if not secret or not header.startswith("sha256="):
      return False
  expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
  return hmac.compare_digest(expected, header.removeprefix("sha256="))   # str, str
  ```

  Run through the real ASGI stack (`/tmp/webhook_probe.py`):

  ```
  valid signature       -> 202
  non-ascii signature   -> 500 Internal Server Error
  ```

  and directly (`/tmp/webhook_probe2.py`):

  ```
  TypeError comparing strings with non-ASCII characters is not supported
  ```

  The suite knows this defect class. `tests/test_connector_identity.py:454`
  (`test_a_non_ascii_authorization_header_is_refused_not_a_server_error`) exists for it, its
  docstring states the general rule — *"`compare_digest` on `str` raises `TypeError` unless both
  sides are ASCII … turned the auth boundary into a 500 with a traceback that any remote party
  could produce at will"* — and `connectors/server.py` was fixed to compare bytes with
  `surrogateescape`. `grep -rn compare_digest src/` returns exactly two comparison sites; the
  second one never got the test or the fix.

  The webhook tests that do exist cover: unsigned (401), correct (202), tampered MAC (401), no
  secret configured (202/401 split), malformed body (422), and 422-amplification. All six build the
  header as `f"sha256={digest}"` from `hmac.new(...).hexdigest()`, so every offered value is ASCII
  hex by construction and none of them can reach the raising branch.
- **Fix**: compare bytes, mirroring the connector's fixed form, and refuse before comparing when
  the header is absent or empty:

  ```python
  offered = header.removeprefix("sha256=").strip()
  if not offered:
      return False
  return hmac.compare_digest(
      offered.encode("utf-8", "surrogateescape"),
      expected.encode("utf-8", "surrogateescape"),
  )
  ```

  Add the missing test beside the connector's, driving the header as raw bytes (httpx will send
  `bytes` header values; `TestClient` reproduces it as shown above).

---

## The warehouse binding's identifier test covers one field of nine, and the field it does not cover is spliced into SQL unchecked

- **Severity**: medium
- **Location**: `tests/test_warehouse_binding.py:119`
  (`test_an_identifier_that_is_not_an_identifier_is_rejected`) — the only identifier-rejection test
  in the file, and it mutates one field. The unchecked field is
  `VectorBinding.server_embed_function` (`src/chemclaw/ingest/eln/warehouse/binding.py:441`),
  interpolated at `src/chemclaw/ingest/eln/warehouse/sql.py:132-136`.
- **Trigger**: a `datasource.yaml` under `CHEMCLAW_DATA_SOURCES_DIR` whose
  `config.binding.vector` sets `embedding: server` and a `server_embed_function` containing
  anything at all. It passes `load_binding` and is written verbatim into the similarity statement.
- **Consequence**: arbitrary SQL is appended to every vector query the share issues against the
  warehouse — a second `FROM`, a `UNION`, a comment that truncates the rest of the statement. The
  trust boundary is the manifest directory (same as the deliberately-literal `where:` clause), so
  this is not a remote injection; what it is, is the module's *stated* invariant being false, and a
  test suite that cannot tell. `sql.py`'s own docstring says: *"a binding contributes identifiers,
  the engine contributes structure, and everything else is a parameter … Relation and column names
  reach the statement text, and each one was matched against `binding._IDENTIFIER` before it got
  here. … The one deliberate exception is `where:`."* That is two claims, and both are wrong for
  this field: it reaches the statement text, it was not matched, and it is a second exception.
  `VectorBinding._is_coherent`'s docstring — *"Identifiers are checked"* — is wrong in the same
  way.
- **Evidence**: `_is_coherent` checks `relation`, `key`, `vector_column`, every
  `content_columns` entry and every `filter_columns` value. It does not check
  `server_embed_function`, which is declared `str` rather than `Identifier` and validated only for
  presence when `embedding == "server"`.

  ```
  $ uv run python /tmp/probe_vector.py
  VALIDATED OK, server_embed_function = "SNOWFLAKE.CORTEX.EMBED(1,'x') FROM SECRETS.CREDS UNION SELECT PWD, 1 --"
  SQL: SELECT RX_ID, BODY, VECTOR_COSINE_SIMILARITY(EMB, SNOWFLAKE.CORTEX.EMBED(1,'x') FROM
       SECRETS.CREDS UNION SELECT PWD, 1 --(?)) AS CHEMCLAW_SCORE FROM ELN.V_RX
       ORDER BY CHEMCLAW_SCORE DESC LIMIT ?
  ```

  What the suite does instead: `test_an_identifier_that_is_not_an_identifier_is_rejected` mutates
  `binding["ingest"]["entry"]["relation"]` only. There is no identifier test anywhere in the file
  for the `vector:` half. And the one test that does exercise this field —
  `tests/test_warehouse_retriever.py:167`
  (`test_server_side_embedding_binds_the_query_text_instead_of_a_vector`) — *asserts the verbatim
  splice* (`assert "SNOWFLAKE.CORTEX.EMBED_TEXT_768(?, ?)" in statement`) without ever asserting
  that the value was validated. It documents the hole rather than closing it.

  Supporting evidence that this is a systemic shape rather than one slip: the `Identifier` type
  alias does not validate anything — it is `Annotated[str, Field(min_length=1)]`. Every real check
  is a hand-written `_check_identifier(...)` call inside a `model_validator`, so a field is safe
  only if somebody remembered to add a line. Measured (`/tmp/ident_probe.py`):

  ```
  AttributeBinding.exclude accepted: ['X; DROP TABLE T --']    # typed `Identifier`, never checked
  content_columns rejected: ValidationError                    # typed `Identifier`, checked by hand
  ```

  (`AttributeBinding.exclude` is filtered in Python and never reaches SQL, so it is not itself a
  defect — it is the proof that the annotation carries no guarantee.)
- **Fix**: two changes, one in each half.
  1. `binding.py`: in `VectorBinding._is_coherent`, add
     `if self.server_embed_function: _check_identifier(self.server_embed_function, "vector server_embed_function")`
     (a warehouse function name is a dotted identifier, so `_IDENTIFIER` fits it unmodified).
  2. `tests/test_warehouse_binding.py`: replace the single-field test with a parametrised sweep
     over *every* field that `sql.py` interpolates — `entry.relation`, `entry.key`,
     `entry.created_at`, `entry.modified_at`, `related.relation`, `related.foreign_key`,
     `related.order_by`, `vector.relation`, `vector.key`, `vector.vector_column`,
     `vector.content_columns[*]`, `vector.filter_columns[*]`, `vector.server_embed_function` —
     each asserting `BindingError`. Derive the list from the fields `sql.py` reads rather than
     writing it out, so the next interpolated field fails the suite instead of shipping.

---

## `_add_cors` has no test at all, and accepts `*` as an origin

- **Severity**: low
- **Location**: `src/chemclaw/api/middleware.py:203-212` (`_add_cors`). Test coverage:
  `grep -rn "cors\|CORS" tests/` returns exactly two lines, both in
  `tests/test_config.py:165-168`, asserting only that the default is the empty string.
- **Trigger**: `CHEMCLAW_SERVICE_CORS_ORIGINS=*` (or any value an operator writes; the config
  comment calls it "a comma-separated allow-list" and nothing rejects a wildcard).
- **Consequence**: the front door installs `CORSMiddleware(allow_origins=["*"],
  allow_methods=["*"], allow_headers=["*"])`. Measured (`/tmp/cors_probe.py`):

  ```
  simple GET ACAO: *
  preflight: 200 {'access-control-allow-origin': '*',
                  'access-control-allow-methods': 'DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT',
                  'access-control-allow-headers': 'authorization', ...}
  ```

  The blast radius is bounded and I am not going to overstate it: `allow_credentials` is not set,
  so a browser attaches no cookies, and the front door authenticates with a bearer token an
  attacker page cannot read out of another origin's JS. Where it does bite is the posture the
  repository ships as the dev default — `entra_required=False`, every request served as
  `dev-user` with all gates open — where a wildcard turns any page the developer visits into a
  client that can list their sessions, post turns and read transcripts on `127.0.0.1:8080`. That
  combination is one env var away and nothing in the suite would notice it.

  The deeper point for this slice: `_add_cors` is the one piece of cross-origin policy in the
  system and *no* test executes its non-empty branch. Whether a configured origin is honoured,
  whether a non-listed origin is refused, whether `*` should be refused outright — none of it is
  asserted, so the middleware's behaviour is whatever Starlette's defaults happen to be on the
  installed version. Every neighbouring piece of HTTP armor in the same module *is* tested
  (`_add_body_size_limit` in `tests/test_request_limits.py:201-263`, `_SecurityHeaders` and
  `_refuse_unauthenticated_exposure` in `tests/test_auth.py:352-398`); CORS is the one that was
  skipped.
- **Fix**: add three tests against the real app — a listed origin gets its own value echoed back,
  an unlisted origin gets no `Access-Control-Allow-Origin`, and the empty default installs no
  middleware at all (assert `CORSMiddleware` is absent from `app.user_middleware`). Then decide
  the `*` question explicitly: either refuse it in `Settings` validation with a message naming
  the dev posture, or pin it in a test as a deliberate allowance. Silence is the one option that
  should not survive.

---

## Checked and found sound (so the absence of findings here is a result, not a gap in the pass)

These were read properly, not grepped, and several were probed against the live environment.

- **Route-level authn coverage** (`tests/test_route_auth_coverage.py`). Walks the built app's
  `route.dependant` tree by *identity* of `require_principal`, pins the probe allowlist in both
  directions, pins the non-`APIRoute` surface (which is how `/openapi.json` was caught), and
  carries four mutation proofs that manufacture the omission and watch the sweep name it. This is
  the strongest structural test in the suite.
- **Session ownership** (`tests/test_service.py:462, 483, 933, 955, 1465`). The inventory of
  `{session_id}` routes is asserted as an exact set *and* swept behaviourally for a non-owner, so
  a new session-scoped route fails twice. The NULL-owner-under-enforcement case is tested on both
  the live-cache and the durable-rehydration path, and the docstring's claim that reverting
  `_owner_authorizes` breaks it is accurate.
- **Skill backend as a gate** (`tests/test_skill_backend.py`). Every reach path is probed for a
  refused skill, the classification of "reach" vs "write" is *derived* from
  `dir(BackendProtocol) | dir(FilesystemBackend)` so an upstream addition must be triaged before
  the file passes, async twins are driven to completion (a coroutine `repr` names no skill, which
  would have made them pass by never running), and the refusal is asserted not to be an
  enumeration oracle.
- **Trigger/tool authorization** (`tests/test_authz.py`). Refusals are matched on message, not
  just type — with the reasoning recorded from a measured mutation where a bare
  `pytest.raises(AuthorizationError)` stayed green after deleting the `actor is None` branch. The
  `authorize_trigger("literal")` call sites are enumerated by AST and checked against the
  effective gate set, with an emptiness floor.
- **Database privileges.** `tests/test_database_privileges.py` derives the write matrix from SQL
  literals in `src/` and fails in both directions. The suite never *executes* the grant file, so I
  did: created `grantprobe`, migrated it, created `chemclaw_app`, ran `apply_grants`, and read
  `information_schema.role_table_grants`. The effective matrix matches what the test's regex model
  claims, and as `chemclaw_app`:
  `UPDATE audit_events` → *permission denied*, `DELETE FROM audit_events` → *permission denied*,
  `INSERT INTO schema_migrations` → *permission denied*. The append-only claim holds. (An
  end-to-end test that applies the file would still be worth having — the LangGraph tables are
  granted only on the *second* run by construction — but that is an availability property, not a
  security one.)
- **Lexical search injection.** `core/fulltext.py` splices only substrings of
  `websearch_to_tsquery(%(q)s)::text`, a value Postgres produced from a bound parameter; the
  chemist's query never reaches statement text. The claim is backed by a stated fuzz measurement
  and the code matches it.
- **The dry-run / plan-gate path predicate** (`authz.writes_durable_memory`). I looked for the
  obvious normalization mismatch between the gate (`path.startswith("/memories/")`) and
  deepagents' router (`path == "/memories"` also routes to the store). The one divergent path is
  exactly `/memories`, and `_check_fs_permission(filesystem_permissions(), "write", "/memories")`
  returns `deny`, so it is unreachable. Measured, not assumed.
- **Prompt-injection framing.** All five model-facing untrusted-content call sites
  (`gather_evidence`, `expand_note`, `find_past_jobs`, both attachment tools, the job-result
  push-back) are framed and tested. There is no *structural* test enumerating the set — a sixth
  path would be unframed and silent — but I could not find an unframed one today, so this is a
  note rather than a finding.

### One thing I could not complete

A full-suite coverage run (`pytest --cov`) was started against the live Postgres/Temporal stack to
put numbers on "what the suite never executes" in the security modules. It reached ~3% of
collected tests in ~25 minutes and was killed; the findings above are from reading and from
targeted reproduction instead. If a later round wants the coverage map, it needs a dedicated run
on an unloaded box, not a slot inside an audit session.
