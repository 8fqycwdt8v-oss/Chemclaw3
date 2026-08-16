# Sweep: secrets and data egress

Scope: every `Settings` field that holds a credential, `core/logging.py`'s `SecretRedactingFilter`,
the OTEL/OpenInference span pipeline, the LangSmith pin, connector bearer tokens, the HPC clients,
`.env.example`, and `deploy/helm/`. Everything below was run in this environment
(`uv run python`, venv synced); the output quoted is what the run printed.

Five findings. Section 6 lists the claims I checked that **held**, because "I looked and it was
fine" is a result the next reviewer should not have to re-derive.

---

## Every configured credential is printed to stderr when config validation fails

- **Severity**: high
- **Location**: `src/chemclaw/core/config/__init__.py:327` (`settings = Settings()`), reached
  through any `@model_validator(mode="after")` in `src/chemclaw/core/config/*.py` —
  `llm.py:174 _embedding_provider_config`, `llm.py` provider check, `store.py:157
  _external_vector_store_is_addressable`, `store.py:174 _pool_bounds_are_orderable`,
  `__init__.py:151 _guards_that_the_comments_already_demand`.
- **Trigger**: a deployment whose `Settings` fails one of those cross-field validators. The most
  likely one in production is the shipped chart's own posture:
  `deploy/helm/chemclaw/values.yaml` sets `CHEMCLAW_LLM_PROVIDER: "openai_compatible"`, and that
  validator requires `llm_base_url` *and* `llm_model`. Forgetting one is a one-line mistake.
- **Consequence**: pydantic attaches the **entire settings input dict** to the `ValidationError` as
  `input_value`. `settings = Settings()` runs at *module import*, so the error is not caught by
  anything — it goes to stderr through Python's own excepthook, **before `configure_logging()` has
  ever run**, so no `SecretRedactingFilter` exists on any handler. Even if one did, it redacts
  values read off `settings`, and `settings` is precisely the object that failed to construct.
  The container crash-loops, so the line is re-emitted on every restart and collected by the
  cluster log stack.
- **Evidence**:

  `/tmp/clean/repro2.py` (clean dir, no `.env`):

  ```python
  os.environ.update({
    "CHEMCLAW_LLM_PROVIDER": "openai_compatible",
    "CHEMCLAW_LLM_API_KEY": "sk-internal-9f2a4c",
  })
  import chemclaw.core.config
  ```

  output:

  ```
  pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
    Value error, llm_provider='openai_compatible' requires llm_base_url, llm_model to be set
    [type=value_error, input_value={'llm_provider': 'openai_...': 'sk-internal-9f2a4c'}, input_type=dict]
  ```

  The whole API key is in the traceback. With more variables set, pydantic's repr truncates to
  head+tail, and the *tail* is still a credential:

  ```
  input_value={'llm_provider': 'openai_...AL-TEMPORAL-KEY-abc123'}
  ```

  (`CHEMCLAW_TEMPORAL_API_KEY=REAL-TEMPORAL-KEY-abc123`.) And the truncation is only in `__str__` —
  `exc.errors()[0]["input"]` carries every value in full. Measured with the full secret set:

  ```
  INPUT: {'vector_store_api_key': 'qdr8ntKeyAbc123XyzPq', 'note_webhook_secret': 'WEBHOOKSECRET-abc123',
          'framing_envelope_secret': 'ENVELOPESECRET-abc123', 'llm_api_key': 'PRIMARYKEY-abc123',
          'hpc_api_token': 'HPCTOKEN-abc123', 'postgres_dsn': 'postgresql://u:DBPASSWORD1@h:5432/d',
          'vector_store_provider': 'qdrant', 'vector_store_url': '', 'temporal_api_key': 'TEMPORALKEY-abc123'}
  ```

  This is the one leak in the tree that the entire redaction apparatus is structurally unable to
  cover: it happens one line before the apparatus exists.
- **Fix**: catch it at the construction site and re-raise without the payload —

  ```python
  try:
      settings = Settings()
  except ValidationError as exc:  # never let the input dict reach stderr
      raise SystemExit(
          "invalid Chemclaw configuration:\n"
          + "\n".join(
              f"  {'.'.join(str(p) for p in e['loc']) or '<settings>'}: {e['msg']}"
              for e in exc.errors()
          )
      ) from None   # `from None` drops the chained ValidationError from the traceback
  ```

  `from None` is load-bearing: without it the original exception is still printed as the cause.
  A complementary hardening is to declare the credential fields as `SecretStr`, which makes
  pydantic's own repr `SecretStr('**********')` everywhere, but that changes every read site, and
  the wrapper above is sufficient and local.

---

## `llm_fallback_api_key` is a credential outside the redaction inventory

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:449` (`_SECRET_SETTINGS`) vs
  `src/chemclaw/core/config/llm.py:70` (`llm_fallback_api_key`), used at
  `src/chemclaw/agent/llm_provider.py:251`.
- **Trigger**: a deployment that configures the AG-12 failover endpoint with its own credential,
  and any log line that quotes it — the obvious one being the failover endpoint answering 401.
- **Consequence**: the fallback key is emitted verbatim in log lines and tracebacks, while the
  primary key in the same line is redacted. The comment above `_SECRET_SETTINGS` calls the list
  "a credential inventory ... one line per addition and visible in review"; this is a credential
  it does not contain.
- **Evidence**: `/tmp/t9.py`, run through the real `configure_logging()`:

  ```
  ERROR repro: settings repr: {'llm_api_key': '***', 'llm_fallback_api_key': 'FALLBACK-abc12345'}
  ERROR repro: failover failed
  Traceback (most recent call last):
  RuntimeError: fallback endpoint 401: Incorrect API key provided: FALLBACK-abc12345
  ```

  Note the first line: the *structural* rule does not save it either. `_STRUCTURAL_SECRETS`'s
  key-name anchor is `\b(?:access_token|refresh_token|api[_-]?key|client_secret)`, and in
  `llm_fallback_api_key` the character before `api_key` is `_`, which is a word character — so
  `\b` never matches. `llm_api_key` on the same line was redacted by the *value* inventory, not by
  the pattern. So for this field both mechanisms miss.

  The gap is unguarded by design of the test: `tests/test_logging.py:632` asserts only that every
  name in `_SECRET_SETTINGS` exists on `Settings` — the direction that would have caught this (every
  credential-holding `Settings` field is in `_SECRET_SETTINGS`) is not asserted anywhere.

  It also propagates into the Helm guard. `tests/test_helm_chart.py:626
  test_no_secret_is_carried_in_the_plaintext_config_map` decides "is this a credential?" *by asking
  `_SECRET_SETTINGS`*, so a deployment that put the fallback key in `.Values.config` would render it
  into a plaintext ConfigMap — readable by every principal with the OpenShift `view` role — and the
  test would stay green:

  ```
  CHEMCLAW_LLM_FALLBACK_API_KEY -> llm_fallback_api_key guarded= False
  CHEMCLAW_VECTOR_STORE_API_KEY -> vector_store_api_key guarded= False
  CHEMCLAW_HPC_ARTIFACT_STORE_TOKEN -> hpc_artifact_store_token guarded= True
  ```
- **Fix**: add `"llm_fallback_api_key"` to `_SECRET_SETTINGS`, and add the missing direction to
  `tests/test_logging.py` — assert that every `Settings` field whose name matches a credential
  shape (`*_api_key`, `*_token`, `*_secret`, `*_dsn`, `*password*`) is either in `_SECRET_SETTINGS`
  or in an explicit, commented allowlist of "named like a credential, is not one"
  (`entra_token_endpoint`, `budget_max_tokens_per_user`, `calc_server_token_env`, …). The module
  comment argues against *deriving* the inventory from a name pattern, which is right; using the
  pattern as a *tripwire* that forces a decision is the complement, not the same thing.

---

## `register_secret_env` protects an env-var name, so a `.env`-supplied secret is never redacted

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:488 register_secret_env` /
  `:741` in `_secret_values`; callers
  `src/chemclaw/retrieval/vectors/qdrant.py:114` and
  `src/chemclaw/ingest/eln/warehouse/connect.py:69`.
- **Trigger**: `vector_store_api_key` (or a warehouse credential) supplied through the `.env` file
  that `Settings.model_config` explicitly supports (`env_file=".env"`,
  `src/chemclaw/core/config/__init__.py:145`) rather than through a real environment variable.
- **Consequence**: `_secret_values` resolves registered names with `os.environ.get(env_name, "")`.
  pydantic-settings reads `.env` **without writing to `os.environ`**, so the lookup returns `""`
  and the credential is redacted nowhere. `register_secret_env`'s docstring says "every log line
  from then on has its value scrubbed"; that is true only when the value happens to also be in the
  process environment.
- **Evidence**: `/tmp/envtest/.env` containing
  `CHEMCLAW_VECTOR_STORE_API_KEY=qdr8ntKeyAbc123XyzPq`, then:

  ```
  loaded from .env: qdr8ntKeyAbc123XyzPq
  in os.environ? False
  'qdrant refused: bad key qdr8ntKeyAbc123XyzPq'      <- redact_secrets() output, unchanged
  ```

  Two secondary properties of the same design, both real:
  1. Registration happens inside `open_qdrant_client()`, so in any process that has not yet built a
     Qdrant client the key is unredacted even when it *is* in `os.environ`. The docstring calls
     this placement "the one that cannot drift from the read" — it cannot drift, but it is also
     late, and the interval it leaves open is "process start until first vector search".
  2. `vector_store_api_key` is not in `_SECRET_SETTINGS`, so — as in the previous finding — the
     Helm ConfigMap guard does not cover it either.
- **Fix**: give `register_secret_env` a value-taking sibling and call it with the resolved setting
  (`register_secret_value(settings.vector_store_api_key)`), or simply put `vector_store_api_key`
  in `_SECRET_SETTINGS` — it *is* a `Settings` field, which is what that list is for, and the
  name-based seam exists for the warehouse case where no `Settings` field exists. For the warehouse
  itself, register the value `connect_options` actually resolved rather than the variable's name.

---

## The `calc` connector's bearer token is outside the connector-token inventory

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:777 SecretRedactingFilter.__init__` vs
  `src/chemclaw/connectors/calc/connector.yaml:30-31` (`auth: mode: none`) and
  `src/chemclaw/connectors/calc/remote.py:154 _token()` /
  `:113 headers = {"Authorization": f"Bearer {token}"}`.
- **Trigger**: a deployment with `CHEMCLAW_CALC_TOKEN` set — which the shipped chart provides as a
  real Secret slot (`deploy/helm/chemclaw/values.yaml:532 calcToken:
  "CHEMCLAW_CALC_TOKEN"`) — and any log line quoting the value other than immediately after the
  literal string `Bearer `.
- **Consequence**: `SecretRedactingFilter.__init__` builds its list from
  `manifest.endpoint.auth.token_env` for manifests whose auth is `BearerAuth`. `calc`'s manifest
  declares `mode: none`, because its token is read from `settings.calc_server_token_env` instead —
  so the one connector whose credential does *not* come from the manifest is the one the inventory
  cannot see. The class docstring states the rule as covering "each connector's bearer token,
  resolved from `manifest.endpoint.auth.token_env` per enabled HTTP connector"; the code matches the
  docstring and the docstring does not match the deployment.
- **Evidence**: `/tmp/t3.py`, with both `CHEMCLAW_CALC_TOKEN` and `CHEMCLAW_CHEM_TOKEN` set:

  ```
  connector token envs resolved: ('CHEMCLAW_CHEM_TOKEN', 'CHEMCLAW_SAFETY_TOKEN')
  "calc connect failed with headers {'Authorization': 'Bearer ***'} raw=calcTok3nAbcdefXYZ"
  'chem raw token calcTok3nAbcdefXYZ vs ***'
  ```

  The chem token is redacted by value wherever it appears; the calc token survives everywhere the
  `Bearer ` structural anchor does not reach. And the structural backstop has a second hole here:
  it requires a digit in the value, so a calc token drawn from letters only is not redacted even
  behind `Bearer ` —

  ```
  LEAKED   opaque bearer no digit -> Authorization: Bearer abcdefghijklmnopqrstuvwxyz
  ```

  (that trade-off is argued in the module comment and is defensible *as a backstop*; it stops being
  defensible when it is the only mechanism covering a credential).
- **Fix**: add `settings.calc_server_token_env` to the names `SecretRedactingFilter.__init__`
  resolves — one line beside the manifest sweep. The better structural fix is to make `calc`
  declare `auth: {mode: bearer, token_env: CHEMCLAW_CALC_TOKEN}` in its manifest like `chem` and
  `safety` do, and have `remote.py` read the manifest rather than a parallel `Settings` field, so
  there is one answer to "which variable holds this connector's credential".

---

## A git push failure is redacted before Postgres and unredacted into Temporal history

- **Severity**: low
- **Location**: `src/chemclaw/kg/git_submitter.py:231`
  (`raise GitSubmitError(f"git {' '.join(args)} failed: {stderr}")`) vs
  `src/chemclaw/kg/pr_gate.py:140` (`reason = redact_secrets(str(exc))[...]`).
- **Trigger**: a PR-gate note publish whose `git push` fails, in a deployment where the knowledge
  remote carries its credential in the URL userinfo.
- **Consequence**: `pr_gate` redacts the message before writing `note_proposals.reason`, and then
  `raise`s the *same, unredacted* exception. It crosses the activity boundary in
  `durable/publish.py::publish_note`, where Temporal serialises it into an `ApplicationFailure` and
  stores the message in workflow history — a durable store with its own retention and its own UI,
  and one nothing in this tree redacts. The comment at `pr_gate.py:136` states the exact leak it is
  fixing ("git quoting a push URL with its token in the userinfo — measures 118 characters against
  a 300-character cut, so the token was stored verbatim") and fixes it on one of the two paths the
  same string takes.
- **Evidence**: `redact_secrets` has exactly one caller outside `core/logging.py`:

  ```
  $ grep -rn "redact_secrets" --include=*.py src/ | grep -v core/logging
  src/chemclaw/kg/pr_gate.py:16:from chemclaw.core.logging import redact_secrets
  src/chemclaw/kg/pr_gate.py:140:        reason = redact_secrets(str(exc))[: settings.proposal_reason_chars]
  ```

  The log path is covered (`_URL_USERINFO` handles both userinfo spellings — measured:
  `https://x-access-token:***@github.com/o/r` and `https://***@github.com/o/r`), and the Postgres
  path is covered. The Temporal path is not.

  Honest caveat, since a finding is a reproduction: I did **not** reproduce a token-bearing git
  stderr against a real remote — whether one appears depends on how the operator supplies the
  credential. `deploy/knowledge-sync.sh:105` uses a `GIT_ASKPASS` helper specifically so the token
  is not in the URL, which closes it for the *sidecar*; the app-side submitter inherits no such
  helper, and `knowledge.sync.repoUrl` is a free-form operator value rendered as a plain env var
  (`_helpers.tpl:267`), so the userinfo form is available to it. What I verified is the asymmetry:
  the codebase judged this string to need redaction on one destination and does not redact it on
  the other.
- **Fix**: redact at the raise rather than at each persistence site —
  `raise GitSubmitError(f"git {' '.join(args)} failed: {redact_secrets(stderr)}")` in
  `git_submitter._git` — which makes `pr_gate`'s call a harmless second pass and covers every
  future consumer of the message. (`kg` importing `core.logging` is already the arrangement
  `pr_gate` uses, so this adds no layering edge.)

---

## Checked and sound

These are the claims I set out to break and could not. Recorded so the next pass does not
re-litigate them.

**OTEL content suppression holds, including for tool arguments and tool results.**
`core/logging._trace_config` sets every `hide_*` field the installed `TraceConfig` dataclass
declares (verified against `dataclasses.fields`). The existing test only exercises a turn with no
tool call, so I ran one *with* a tool: a scripted model emitting a `tool_call` whose argument is a
marker SMILES, a real tool returning a marker string, through `build_langgraph_agent` and the real
`_instrument_llm_calls` (`/tmp/spans.py`). Scanning every attribute of all 11 exported spans for
four distinct markers:

```
=== content_allowed = False  spans: 11        (no matches)
=== content_allowed = True   spans: 11        (38 matches, including
    _Fake :: llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments = {"smiles": "CCOSMILESMARKERCCO"}
    marker_tool :: input.value = CCOSMILESMARKERCCO
    tools :: output.value = ... "TOOLRESULTMARKER:CCOSMILESMARKERCCO" ...)
```

So `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` is genuinely the switch, and molecule structures do not
leave the pod on a span with it off. One forward-looking note, not a finding today:
`TraceConfig.enable_genai_semconv` is a non-`hide_*` field that reads from
`OPENINFERENCE_ENABLE_GENAI_SEMCONV`, is not set by `_trace_config`, and is not covered by
`test_every_hide_flag_is_set_together` (which filters to `hide_*`). `mask()` keys on OpenInference
attribute names only. The installed `openinference-instrumentation-langchain` emits no `gen_ai.*`
attributes, so nothing leaks now — but a release that starts to would bypass every hide flag.
Pinning `enable_genai_semconv=False` explicitly costs one line.

**The LangSmith egress pin works, including against the warm `lru_cache` it was written for.**
`/tmp/envtest/t8.py`, with `LANGSMITH_TRACING=true` set *before* `import langchain` (the ordering
the module docstring says an environ-only pin fails on):

```
before: True
after : False
env: false false
handlers: []
```

`CallbackManager.configure()` attaches no `LangChainTracer`. The chart pins the same two variables
independently (`_helpers.tpl:40-43`).

**`JsonFormatter` does not re-render `exc_info`.** It reads `record.exc_text` (the filter's already
redacted string) and only falls back to a `redact_secrets`-wrapped render, including for
`stack_info` — I read the code path and it matches its docstring.

**SSE error events are classified, not detailed.** `api/runner.py:502` logs the exception
server-side and emits a fixed sentence plus a code and correlation id. The other `ErrorEvent` sites
carry only budget/loop-cap text.

**No credential rides a redirect.** `connectors/registry.py:332` sets `follow_redirects=False`
explicitly; every other `httpx` client in the tree takes httpx's default, which is also `False`
(`connectors/qm/hpc/nextflow.py:82,157`, `connectors/health.py:96`,
`agent/llm_provider.py:345`, `core/embeddings.py:262`). `nextflow._artifact_headers` genuinely does
what its comment says: it hands the launcher token to the artifact store only on an origin match,
and `_same_origin` normalises default ports.

**`.env.example` and the Helm chart contain no real values.** Every credential row in
`.env.example` is empty or a commented `sk-ant-...` placeholder; `values.yaml` ships
`CHEMCLAW_ENTRA_TENANT_ID: "00000000-…"` and the templated Secret is `"CHANGE-ME"` per key. The
`config:` ConfigMap holds no field that is in `_SECRET_SETTINGS` (the guard test passes) — the gap
is only the two fields that are *not* in that list, reported above.

**The structural backstop's remaining misses are the documented trade-offs.** Battery in `/tmp/t7.py`:
JWTs, libpq `password=`, both URL-userinfo spellings, `api_key`/`x-api-key` in JSON and dict reprs,
and quoted `password='…'` are all redacted. What is not: `Authorization: Basic <b64>` (dropped
deliberately, argued in the comment), a digit-free opaque bearer (the digit requirement, argued),
a PEM private key block, and an Azure-style `AccountKey=…`. None of those four is a credential this
process holds today; the first two become live only through the `calc`-token gap above.
