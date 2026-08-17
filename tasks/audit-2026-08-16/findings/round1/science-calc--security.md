# science/calc — security and hardening

Slice: `src/chemclaw/science/calc/` (store, postgres_store, artifacts, postgres_artifacts,
calibration, models, thermo, uncertainty, solvents, logd). Reviewed against the live stack
(dockerd + `make up` + `make db-migrate`); every reproduction below was run with `uv run` and its
output is quoted verbatim.

Boundary map used for reachability (read, not assumed): the agent-facing entry points into this
slice are the `calc` connector's MCP tools in
`src/chemclaw/connectors/calc/server/tools.py` (`report_measurement`, `predict_logd`,
`find_calculations`, `list_artifacts`, `fetch_artifact`, `calculator_trust`,
`calculator_outliers`), plus `connectors/calc/compose.py` which feeds server payloads into
`ArrayOffloadingStore` and `thermo.py`. `connectors/calc/connector.yaml` declares
`endpoint.auth.mode: none`, and `agent/authz.py::DEFAULT_WRITE_TOOL_GATES` does **not** list
`report_measurement`, so with the default `tool_authz_default="allow"` that write tool is
callable by any authenticated user through the agent, and by anything that can open a socket to
the connector pod without any identity at all.

---

## A non-finite measurement permanently collapses the calibration report into the "never measured" payload

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/calibration.py:252` (`record_observation`),
  `:309` (`summarize`), `:106` (`Calibration`); reached from
  `src/chemclaw/connectors/calc/server/tools.py:133` (`report_measurement`)
- **Trigger**: one tool call `report_measurement(property_name="pka", smiles=<any>,
  measured_value=1e400)`. `1e400` is *strictly valid JSON* — no `NaN`/`Infinity` literal needed —
  and pydantic's JSON parser turns it into `inf`:

      $ uv run python /tmp/repro1b.py
      1e400 -> inf
      NaN -> nan
      LogdInput 1e400 -> inf

  Nothing between that boundary and the database checks finiteness: `record_observation`'s
  signature is `observed_value: float`, `PredictionRecord.predicted_value` is a bare `float`, and
  `measurements.value` / `predictions.observed_value` are `DOUBLE PRECISION`, which accepts
  `Infinity`. Precondition: `calibration_enabled=true` (it defaults False — but a deployment that
  leaves it off is not using the ledger at all, so the exposed configuration is the one that
  matters).
- **Consequence**: `summarize` computes `bias`/`mean_absolute_error`/`rmse` over the poisoned
  residual and gets `±inf`; pydantic then serializes every one of them as **`null`**. `null` is
  precisely the payload this model documents as meaning "nothing to compute them from" — the
  `None`-instead-of-0.0 design exists so a chemist cannot read an unmeasured calculator as an
  accurate one, and a single call reintroduces the ambiguity in the other direction. Worse,
  `verdict` — which `calculator_trust`'s docstring instructs the model to **read first** — is
  computed from `n`, which is still 10, so it keeps saying "Quote the figures with the count".
  There is no repair path: `grep` finds no `DELETE FROM measurements` / `DELETE FROM predictions`
  anywhere in `src/`, `durable/retention.py` never touches either table, and the upsert is keyed
  `(property, input_hash)`, so short of manual SQL the calculator is permanently unreportable.
- **Evidence** (`/tmp/repro3_calib8.py`, against the live Postgres — 10 honest observations, then
  one `1e400`):

      BEFORE: {"calc_type":"audit-probe8","n":10,"enabled":true,"bias":-0.049999,
               "mean_absolute_error":0.049999,"rmse":0.049999,"uncertainty_coverage":1.0,...
               "verdict":"Measured over 10 observation(s) of this calculator version. Quote the
               figures with the count, ..."}
      AFTER : {"calc_type":"audit-probe8","n":10,"enabled":true,"bias":null,
               "mean_absolute_error":null,"rmse":null,"uncertainty_coverage":0.9,...
               "verdict":"Measured over 10 observation(s) of this calculator version. Quote the
               figures with the count, ..."}

  Stored row (from `/tmp/repro2_calib.py`):
  `('audit-probe', 'h2', inf, 'chemist-reported')`.
- **Fix**: validate finiteness at the value's entry into the ledger, in this module rather than at
  each caller — `predicted_value` / `predicted_uncertainty` on `PredictionRecord` and the
  `observed_value` argument of `record_observation` should reject non-finite input with a
  `ChemclawError` (pydantic: `Field(allow_inf_nan=False)`; for the plain argument, an explicit
  `math.isfinite` check). Independently, `Calibration.verdict` should not claim the figures are
  quotable when any of `bias`/`mae`/`rmse` is `None`, since `None` now has two causes.

---

## The calibration ledger has no ownership, attribution or immutability — any caller silently overwrites another's measurement

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/calibration.py:61` (`_UPSERT_MEASUREMENT`),
  `:89` (`_RECORD_OBSERVATION`), `:252` (`record_observation`)
- **Trigger**: any two `report_measurement` calls naming the same `(property_name, smiles)`, from
  any two users, in any order. The second wins.
- **Consequence**: three distinct gaps, all in the write path that `calculator_trust` and
  `calculator_outliers` read back as *measured* accuracy:
  1. `ON CONFLICT (property, input_hash) DO UPDATE SET value = EXCLUDED.value` discards the prior
     measurement with no history row and no versioning. `measurements` has no owner column
     (`infra/sql/030_measurements.sql`), and `source` is a caller-chosen string that
     `report_measurement` hardcodes to the constant `"chemist-reported"` — so the ledger cannot
     say *who* reported a value, and the rewrite leaves no trace.
  2. `_RECORD_OBSERVATION` is `UPDATE predictions SET observed_value = %s ... WHERE calc_type = %s
     AND input_hash = %s` with **no `AND observed_value IS NULL`**, so the overwrite also re-scores
     every already-reconciled prediction of that molecule across every calculator version.
  3. The write is not gated: `report_measurement` is absent from
     `agent/authz.py::DEFAULT_WRITE_TOOL_GATES`, and `tool_authz_default` defaults to `"allow"`,
     so `authorize_tool` returns without a role check; the connector itself declares
     `auth: mode: none`, so the same write is reachable with no identity at all from anything that
     can reach the pod.
- **Evidence** (`/tmp/repro2_calib.py`, live Postgres): three honest measurements give
  `bias -0.0999`; one further call re-reporting `h1` as `999.0` returns `rows scored: 1` and the
  aggregate becomes `bias -331.73`, `mae 331.73`, with the original 4.1 gone from the table:

      HONEST  : 3 -0.09999999999999964 0.09999999999999964 ...
      OVERWRIT: rows scored: 1 | 3 -331.73333333333335 331.73333333333335
      STORED  : [('audit-probe','h1',999.0,'chemist-reported'), ...]

- **Fix**: `record_observation` should take the acting principal
  (`chemclaw.core.identity_context.get_current_actor()` / `agent/authz.py::require_actor`) and
  persist it as `source` plus a dedicated `reported_by` column, and the measurement table should
  be append-only with the aggregate reading the latest row per `(property, input_hash, reporter)`
  rather than being destructively upserted. At minimum, add `AND observed_value IS NULL` to
  `_RECORD_OBSERVATION` so a re-report cannot silently rewrite an already-scored prediction, and
  add `report_measurement` to `DEFAULT_WRITE_TOOL_GATES` — it is a state-changing tool by the
  manifest's own classification, and the built-in gate exists exactly so a write is not open
  merely because nobody remembered to list it.

---

## `logd_from_pka`: an unbounded `ph` overflows, and a non-finite `ph` walks straight through the polyprotic domain guard

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/logd.py:235` (`ionised_ratio = 10.0**exponent`),
  `:198` (`_require_a_single_equilibrium`); entry point
  `src/chemclaw/connectors/calc/server/tools.py:913` (`predict_logd(smiles, ph=None)`)
- **Trigger**: the agent calls `predict_logd` with a `ph` it chose. `ph` is an unconstrained
  `float | None` at the tool signature, on `LogdInput`, and in `logd_from_pka` — there is no
  bound, no finiteness check, and no plausible-range validation anywhere on the path.
  - `ph=400` → `10.0**(400 - 10)` raises `OverflowError`.
  - `ph=1e400` (valid JSON, parsed to `inf`) → `ionised_ratio = inf` →
    `ionised_fraction = inf/(1+inf) = nan` → `nan > settings.logd_negligible_ionised_fraction` is
    **False**, so the guard returns without raising → `log_d = clogp - log10(inf) = -inf`.
  - `ph=NaN` → same path, `log_d = nan`.
- **Consequence**: `OverflowError` is not a `ValueError`, so
  `connectors/server.py::_sanitize_tool_errors` replaces it with
  `"Error executing tool predict_logd: an internal error occurred"` — a deterministic bad-input
  case presented to the chemist as an infrastructure fault. The non-finite case is worse: it
  defeats `_require_a_single_equilibrium`, whose entire stated purpose is to refuse rather than
  return "a number known to be wrong by 2-5 log units", and returns a `LogdResult` with
  `log_d = -inf` (serialized as `null`) for a diacid at a pH where the control was supposed to
  fire. The guard fails **open** on exactly the inputs it was not written for.
- **Evidence** (`/tmp/repro1_logd.py`; the last line is the control showing the guard works on
  finite input):

      mono ph=400                  -> OverflowError: (34, 'Numerical result out of range')
      mono ph=1e400(inf)           -> log_d=-inf ph=inf
      mono ph=nan                  -> log_d=nan ph=nan
      diacid ph=inf                -> log_d=-inf ph=inf
      diacid ph=nan                -> log_d=nan ph=nan
      diacid ph=7.4 (control)      -> CalculationDomainError: 'OC(=O)CCC(=O)O' has 2 acidic ...

- **Fix**: constrain `ph` where the quantity is defined rather than at each caller — put the bound
  on `LogdInput.ph` and on `logd_from_pka`'s parameter (aqueous pH is meaningful over roughly
  `-2 … 16`; anything outside is a `CalculationDomainError` naming the range, which is the
  refusal style this module already uses). That single bound removes the overflow and the
  `inf`/`nan` bypass together, because a finite bounded exponent cannot produce either.

---

## `ArrayOffloadingStore.put` raises on an unvalidated payload field, contradicting its own documented contract — and silently corrupts a non-string one

- **Severity**: low
- **Location**: `src/chemclaw/science/calc/artifacts.py:335`
  (`files[name] = base64.b64decode(str(encoded))`)
- **Trigger**: the calculation server returns a `compute_hessian` payload whose `hessian_npy`
  is not clean base64. The decode happens on the **raw, unvalidated** wire payload:
  `connectors/calc/compose.py:216` wraps the store, `remote.py:352` calls
  `cached_compute(store, key, _compute)`, and `cached_compute` calls `store.put(...)` with the
  dict straight from `remote_compute`; `HessianPayload.model_validate(payload)` only runs
  *afterwards*, back in `compose.hessian`.
- **Consequence**: two contradictions of the class's own docstring, which states "Losing a
  by-product costs a future recomputation and never the calculation in hand … so a store that
  refuses is a debug line and an uncached result, not a raise", and wraps only `put_all` in
  `try/except`:
  - a malformed base64 string raises `binascii.Error` out of `put`, so the Hessian that was just
    computed (minutes of server time, already in hand) is thrown away and the caller gets an
    exception instead of an uncached result;
  - `str(encoded)` on a non-string field plus `b64decode`'s default `validate=False` **silently
    discards** every non-alphabet character, so a payload where `hessian_npy` arrives as a list
    decodes to different bytes than were sent, is content-addressed under that wrong digest, and
    is stored as a cache hit for every future caller with nothing raised.
- **Evidence** (`/tmp/repro4_offload.py`):

      valid b64                -> stored ok
      malformed b64 (len 1)    -> RAISED Error: Invalid base64-encoded string: ...
      not a string (int)       -> RAISED Error: Invalid base64-encoded string: ...
      not a string (list)      -> stored ok        # ← silently decoded "['AAAA']" as "AAAA"

- **Fix**: move the decode inside the existing `try` (or guard it with
  `isinstance(encoded, str)` and `base64.b64decode(encoded, validate=True)`), and on failure log
  and `return` without a row — which is exactly what the docstring already promises. Do not
  `str()`-coerce: a field that is not a string is a malformed payload, and coercing it is what
  turns a loud failure into a poisoned cache row.

---

## The flat calculation key is ambiguous, so two distinct keys can share one permanent cache row

- **Severity**: low
- **Location**: `src/chemclaw/science/calc/store.py:122` (`CalculationKey.as_str`), used as the
  Postgres primary key in `postgres_store.py:28/41` and as `calc_key` in `postgres_artifacts.py`
- **Trigger**: `as_str()` is `f"{calc_type}@{calc_version}:{input_hash}:{params_hash}"`, and none
  of the four components is constrained — `calc_version` is free text that this codebase's own
  comments confirm contains both delimiters in production (`remote.py:230`: "a real `calc_version`
  contains both delimiters — `esol-delaney@2004` carries the `@`, `cal-0.28733:-29.3116` carries
  the `:`"), and `input_hash` is taken **verbatim** from the server's `calculation_key` reply
  (`remote.py:249`) with no format validation.
- **Consequence**: two different `CalculationKey`s collapse to one flat string, and the flat
  string is the identity — `PostgresStore.get` returns the row and re-attaches the *requested*
  key to it (`postgres_store.py:97`), so nothing anywhere detects the mismatch and one
  calculation is served as the answer to another. It is permanent by design: `durable/retention.py`
  never prunes `calculation_results`, and D-011 says a persisted result is never recomputed.
  `_stored_from_row`'s docstring shows the delimiter hazard was seen in the *decoding* direction
  ("the flat string is an index key, not a serialization format") and not in the identity
  direction, where it decides which row a lookup hits.
- **Evidence** (`/tmp/repro5_key.py`):

      k1 flat: pka@fit-2.4:9f2c1ab30de4f501:bb:cc
      k2 flat: pka@fit-2.4:9f2c1ab30de4f501:bb:cc
      distinct keys: True | same flat form: True
      get(k2) -> {'pka': 4.2} (row written for k1)

- **Fix**: make the flat form injective — length-prefix or percent-escape the components, or
  simply hash the four-tuple (`stable_hash`) and keep the human-readable parts in their existing
  columns, which `find` already reads instead of parsing the key. Independently, constrain
  `input_hash`/`params_hash` with a hex pattern (`Field(pattern=r"^[0-9a-f]{16,64}$")`), since
  both are hashes by contract and one of them arrives across a wire unvalidated.

---

## `PostgresArtifactStore.open` never verifies the content address the store is named after, and decompresses without a bound

- **Severity**: low
- **Location**: `src/chemclaw/science/calc/postgres_artifacts.py:120` (`open`),
  `src/chemclaw/science/calc/artifacts.py:84` (`decode`)
- **Trigger**: any read of a blob whose stored bytes no longer match `content_hash` — a partially
  restored dump, a replicated row, or anything with write access to `artifact_blobs`.
- **Consequence**: the module's premise is that "a blob is named by the SHA-256 of its
  uncompressed bytes … the hash is also the read path", but `open()` returns
  `decode(codec, data)` without ever recomputing the digest, and never compares against the
  recorded `byte_size` either (it selects only `codec, data`). So the one property that makes the
  store content-addressed is asserted on write and never checked on read; the bytes then go to
  `thermo.unpack_npy` → `np.load`. `decode` also calls `zlib.decompress(payload)` with no
  `max_length`, so the write-side `too_large` check on the *uncompressed* size is the only bound,
  and it is not re-applied to what actually comes back.
  (Checked and **not** a finding: `unpack_npy` really does pass `allow_pickle=False`, as its
  docstring claims — `np.load(io.BytesIO(...), allow_pickle=False)` at `thermo.py:115` — so the
  pickle-RCE path is genuinely closed.)
- **Fix**: in `open()`, select `byte_size` alongside `codec, data`, pass it to
  `zlib.decompress(payload, bufsize)` as `max_length`, and verify
  `content_address(decoded) == content_hash` before returning — a mismatch is a `None` (treated as
  an eviction, which every caller already handles) plus a `logger.error`, not a returned blob.

---

## Checked and found sound

Recorded so the absence is a result rather than a gap in coverage:

- **SQL injection**: none. Every statement in `postgres_store.py`, `postgres_artifacts.py` and
  `calibration.py` is a module-level constant with `%s`/`%(name)s` placeholders; `_FIND`'s
  `%s IS NULL OR col = %s` shape means no filter combination assembles SQL. No f-string reaches a
  cursor in this slice.
- **Unsafe deserialization**: `unpack_npy` sets `allow_pickle=False` (verified in code, matching
  its docstring); `json.loads` on JSONB is only a fallback for a non-dict driver return; no
  `pickle`, `eval`, `yaml.load` or dynamic import anywhere in the slice.
- **Path traversal / command construction**: no filesystem or subprocess use remains in this
  package — the physics that shelled out lives in `Chemclaw3-mcp` now. `artifacts.media_type_for`
  is a dict lookup with an opaque-bytes default, not a name-driven dispatch.
- **Secrets in logs**: the log lines here carry calculation keys and hashes only; the DSN is
  redacted before any error text by `core/db.py::_redact` (round-trips through libpq's own
  parser, so URL-userinfo, query-parameter and keyword forms are all covered), and
  `connectors/server.py::_sanitize_tool_errors` replaces any non-`ValueError` exception text
  before it reaches the model. The calibration docstrings' claim that "the connector's error
  sanitizer turns it into a caller-safe message" is true.
- **Result-set caps**: `find_calculations` clamps `limit` into
  `[1, settings.calc_find_max_results]` before building the `CalculationQuery`, and it is the only
  construction site of that model in `src/`; `fetch_artifact` clamps `max_chars` the same way and
  correctly floors at 1 so a negative value cannot slice from the end. `reconciled_for` is
  deliberately unbounded, and its stated justification holds — its growth is bounded by rows a
  human typed in, not by calculator traffic.
- **Out-of-range atomic numbers**: `Structure.elements` is unbounded `list[int]`, and
  `models.symbols` / `thermo._atomic_masses` pass those integers straight to RDKit's periodic
  table. I probed for a native crash and found none: RDKit raises `RuntimeError` (119, 200, 1000,
  2**31) or `OverflowError` (negative) cleanly, so this is an ugly error message, not a
  memory-safety issue.
