# core kernel — security and hardening (round 1)

Slice: `src/chemclaw/core/config/*.py`, `core/errors.py`, `core/ids.py`, `core/bounded.py`,
`core/chem.py`, `core/reagents.py`.

Five findings. Every reproduction below was run in this environment with `uv run`; the exact
printed output is quoted.

Claims I checked and found **true** (no finding, recorded so the next pass does not re-do them):

- `core/chem.require_molecule` really does reject the three input classes its docstring names.
  I probed the obvious escapes it does not mention — `NUL`, `VT`, `US`, `DEL`, `|` — and all are
  rejected. `"CCO\x00"` (trailing NUL, nothing after it) is accepted and canonicalizes to `CCO`,
  which loses nothing.
- `core/chem.STANDARDIZATION_VERSION` really is folded into both fingerprint `definition` strings
  (`molfp/fingerprint.py:72`, `rxnfp/fingerprint.py:85`).
- `core/bounded.py`'s "callers that run under threads (the budget tracker) hold their own lock" is
  true — `api/budget.py:87` holds a `threading.Lock` around every mutation.
- `core/bounded.BoundedLru`'s over-capacity-under-pin hazard is not reachable: the only caller
  passing `pinned` is `api/state.py`, whose pin source is "a turn in flight", bounded by the
  admission semaphore (default 8) far below the 1000-entry cap.
- `core/http.LOOPBACK_HOSTS` is `{127.0.0.1, localhost, ::1}` — narrow in the safe direction, and
  it does *not* contain `0.0.0.0`, so `_refuse_unauthenticated_exposure` cannot be tricked by the
  default bind.

---

## `resolve_compound_name` fabricates a structure for a name it does not know

- **Severity**: medium
- **Location**: `src/chemclaw/core/reagents.py:314` (`resolve_compound_name`), amplified at
  `src/chemclaw/core/reagents.py:366` (`density_of`); reached from
  `src/chemclaw/ingest/eln/ord_adapter.py:333`.
- **Trigger**: any reagent *name* that is also a parseable SMILES string. The module's whole
  premise is that a name that is not in `_RAW_SYNONYMS` returns `None`; instead the miss falls
  through to `require_canonical_smiles(name)` and the **name is reinterpreted as a structure**.
  Concretely: an ORD/ELN record whose compound identifier is `NAME: "CO"`.
- **Consequence**: `CO` — carbon monoxide to every chemist who writes it — is ingested as
  **methanol**, and is *labelled* methanol, because the reverse index resolves the fabricated
  structure back to a display name. `CN` (cyanide) becomes methylamine, `NO` (nitric oxide)
  becomes hydroxylamine, `N` becomes ammonia. The wrong structure then gets a `compound_id`, a
  fingerprint row, a graph note, and is what a hazard screen reads — which is the exact failure
  class `core/chem.py`'s own module docstring spends a paragraph on ("a pyrophoric reagent and an
  alkane sharing one compound id is the worst instance of this defect, since a hazard screen reads
  that id"). It is also a structure-injection path: a lower-trust ELN/ORD export can put *any*
  structure into the graph through a field the ingest code believes is a trivial name.
- **Evidence**: the module docstring states the opposite as its reason for existing —
  "Resolution is deliberately *conservative*: an unknown name returns no match rather than a
  guess. Fabricating a structure from a name is the one failure mode that would be worse than the
  gap" (`reagents.py:22-24`). The fallback that breaks it is `reagents.py:330-339`. The consuming
  docstring at `ord_adapter.py:315` repeats the claim: "Refusing to invent a structure is the
  point (a fabricated one propagates silently into a fingerprint index, a similarity hit and
  eventually a proposed note)."

  Run output:

  ```
  'CO'                 -> smiles='CO'    name='methanol'           source='smiles'
  'CN'                 -> smiles='CN'    name='CN'                 source='smiles'
  'NO'                 -> smiles='NO'    name='NO'                 source='smiles'
  'N'                  -> smiles='N'     name='N'                  source='smiles'
  'O'                  -> smiles='O'     name='water'              source='smiles'
  'carbon monoxide'    -> None (honest miss)
  'sodium cyanide'     -> None (honest miss)
  'NaCN'               -> None (honest miss)
  'nitric oxide'       -> None (honest miss)

  density_of('CO')  = 0.792   (methanol's density)
  display_name('CO')= methanol
  ```

  The last two lines are the sharpest form: `density_of`'s docstring says "`None` is the
  load-bearing answer … a guessed density is a weighing error that looks like an answer", and it
  returns methanol's 0.792 g/mL for carbon monoxide. (`density_of` currently has **no caller in
  this repository** — grep finds only `reagents.py` and tests — so that half is latent; the
  `ord_adapter` path is live.)

  Note the honest misses beside the fabrications: the table has no `NaCN`, no `carbon monoxide`,
  no `nitric oxide`. So the fallback fires precisely on the short forms a chemist actually writes
  for the reagents the table is missing.
- **Fix**: the SMILES fallback should not accept a *bare* single- or two-atom formula-shaped
  string, and more generally it should not be reached at all from a caller that asked "resolve
  this **name**". Two concrete changes, either of which closes it:
  1. Split the entry point: `resolve_compound_name(name)` consults the table only and returns
     `None` on a miss; a separate `resolve_structure_or_name(text)` keeps today's two-stage
     behaviour, and `ord_adapter._structure_of` calls the *name*-only one for its `NAME` /
     `IUPAC_NAME` branch (it already handles `SMILES` and `INCHI` identifiers explicitly one loop
     earlier, so it never needed the SMILES fallback).
  2. If the two-stage entry point must stay, refuse the fallback for any query that is a known
     ambiguous element/formula spelling — at minimum reject a query whose parse yields ≤ 2 heavy
     atoms and whose characters are all element symbols (`C`, `N`, `O`, `S`, `P`, `B`, `F`, `I`,
     `CO`, `CN`, `NO`, `CS`, `SO`, `NN`, `OO`). Option 1 is the elegant one; option 2 is a
     denylist and will drift.

---

## `substructure_pattern` silently truncates the query — the class `require_molecule` exists to close

- **Severity**: medium
- **Location**: `src/chemclaw/core/chem.py:323` (`substructure_pattern`), reached from
  `science/fingerprints/molfp/search.py:149` and `connectors/calc/server/tools.py:616`.
- **Trigger**: a substructure query containing whitespace or an edge non-ASCII character — e.g.
  `find_substructure_matches("c1ccccc1 and N")`, or a query lifted out of a note's prose code span
  such as `` `80 °C` ``.
- **Consequence**: RDKit's SMARTS parser stops at the whitespace and *silently returns the prefix*,
  so a narrow query becomes a broad one and the scan reports every compound matching the prefix as
  a hit for the query the caller typed. `"c1ccccc1 and N"` matches every benzene-bearing compound
  in the corpus; `"°C"` compiles to a bare carbon atom, i.e. "match every organic molecule". The
  result is reported as a complete, exact answer — there is no truncation flag for this, only for
  the record cap and the result cap. It is also an amplification: a query the caller believed was
  selective turns into a full-corpus positive sweep whose hit list is capped and returned.
- **Evidence**: `chem.py:232-262` (`require_molecule`) is 30 lines of docstring devoted to exactly
  this: "The parser treats any whitespace as the end of the structure and ignores the rest … That
  is the whole silent-truncation class: a malformed or concatenated string does not fail, it
  narrows to a *different, smaller molecule* than the caller submitted", and it names the shipped
  bug it was written for (`screen_hazards("CCO junk")` returning a clean screen of ethanol).
  `substructure_pattern`, 60 lines further down the same file, calls `Chem.MolFromSmarts(query) or
  Chem.MolFromSmiles(query)` (line 339) with no such gate — it checks only for `None` and for a
  zero-atom pattern. Its docstring discusses the zero-atom case at length and never mentions
  truncation.

  Run output:

  ```
  ACCEPTED 'c1ccccc1 junk'         -> smarts='c1ccccc1'  atoms=6
  ACCEPTED 'CCO\tsomething'        -> smarts='CCO'       atoms=3
  ACCEPTED 'c1ccccc1 and N'        -> smarts='c1ccccc1'  atoms=6
  ACCEPTED '°C'                    -> smarts='C'         atoms=1
  ACCEPTED 'CC°'                   -> smarts='CC'        atoms=2
  ACCEPTED '[#6] ignore-the-rest'  -> smarts='[#6]'      atoms=1
  ```

  For contrast, `require_molecule` rejects every one of the whitespace forms.
- **Fix**: apply the same string-level gate `require_molecule` already owns, before parsing.
  Extract the three checks from `require_molecule` into a private `_reject_truncating_input(text)`
  and call it from both, so the two cannot drift again:

  ```python
  def _reject_truncating_input(text: str, kind: str) -> str:
      stripped = text.strip()
      if not stripped or any(ch.isspace() for ch in stripped):
          raise InvalidSmilesError(f"invalid {kind} (empty or contains whitespace): {text!r}")
      if not stripped.isascii():
          raise InvalidSmilesError(f"invalid {kind} (non-ASCII characters): {text!r}")
      return stripped
  ```

  `substructure_pattern` then parses the returned `stripped`. (Refuse rather than accept the
  prefix: a query the caller cannot see was rewritten is worse than an error message.)

---

## A stale or mistyped `.env` key prints its value in the startup traceback, before redaction exists

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/__init__.py:143-148` (`model_config`,
  `extra="forbid"` + `env_file=".env"`) and `:327` (`settings = Settings()` at module import).
- **Trigger**: a `.env` file in the process CWD containing any `CHEMCLAW_*` key that is not a
  `Settings` field — a typo (`CHEMCLAW_LLM_APIKEY=…`), or a field that has since been renamed or
  deleted. The last case is not hypothetical for this repo: 24 calculator fields were removed
  from `config/calculators.py` when the physics moved out, and a `.env` carrying one now fails
  this way.
- **Consequence**: pydantic's `extra_forbidden` error embeds the **value** verbatim, and the error
  is raised at *import* of `chemclaw.core.config` — i.e. before `configure_logging` has installed
  `SecretRedactingFilter`, and as an uncaught exception that goes to stderr via the default
  excepthook rather than through the logging path at all. So the credential is never seen by
  `redact_secrets`. It lands in the container log, the CI job log and the operator's terminal in
  full. Every entrypoint imports this module (`api/app.py`, `cli/chat.py`, `cli/connectors_dev.py`,
  `connectors/server_entry.py`, `durable/background_worker.py`), so all of them do it.
- **Evidence**: with `/tmp/.../.env` containing
  `CHEMCLAW_LLM_API_KEY_BACKUP=sk-supersecret-0123456789`:

  ```
  Traceback (most recent call last):
    File "<string>", line 2, in <module>
    File "/home/user/Chemclaw3/src/chemclaw/core/config/__init__.py", line 327, in <module>
      settings = Settings()
  pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  chemclaw_llm_api_key_backup
    Extra inputs are not permitted [type=extra_forbidden,
      input_value='sk-supersecret-0123456789', input_type=str]
  ```

  Note the same key set only in `os.environ` (not in `.env`) is silently ignored — I checked:
  `CHEMCLAW_LLM_API_KEY_BACKUP=… python -c "Settings()"` printed `no error; extras ignored`. So
  `extra="forbid"` buys typo protection only for the `.env` route, and that is exactly the route
  that leaks.
- **Fix**: catch and re-raise with the values stripped, at the one construction site:

  ```python
  try:
      settings = Settings()
  except ValidationError as exc:
      names = sorted({str(e["loc"][0]) for e in exc.errors()})
      raise SystemExit(
          f"invalid configuration for: {', '.join(names)} "
          "(values withheld; see core/config/ for the current field set)"
      ) from None
  ```

  `from None` matters — chaining would put the original message back in the traceback. If a
  richer message is wanted, build it from `exc.errors()` while dropping the `input` key of each
  entry.

---

## Two credentials the process holds are outside the secret inventory that governs both redaction and the ConfigMap/Secret split

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/llm.py:70` (`llm_fallback_api_key`) and
  `src/chemclaw/core/config/calculators.py:306` (`calc_server_token_env`, whose *value* is the
  calc-server bearer). The inventory they are missing from is
  `src/chemclaw/core/logging.py:447-464` (`_SECRET_SETTINGS`).
- **Trigger**: any log line, exception message or traceback carrying either value. For
  `llm_fallback_api_key` this is reached whenever the failover endpoint is configured
  (`agent/llm_provider.py:251`); for the calc bearer whenever `connectors/calc/remote.py:114-115`
  builds its `Authorization` header and something quotes the request.
- **Consequence**: two things, because `_SECRET_SETTINGS` is load-bearing twice.
  1. **Redaction.** The sibling field `llm_api_key` is scrubbed and the fallback is not, so a
     deployment with failover configured logs its second endpoint's credential in the clear where
     the first is masked.
  2. **The chart guard.** `tests/test_helm_chart.py:621-650`
     (`test_no_secret_is_carried_in_the_plaintext_config_map`) is written *against*
     `_SECRET_SETTINGS` — "a check against the redaction inventory rather than against a
     hand-kept list, because those are the same question asked twice". A credential absent from
     the inventory can therefore be declared in `.Values.config`, which renders into a plaintext
     ConfigMap readable by every principal holding the OpenShift `view` role, and the test passes.
     `llm_fallback_api_key` is in exactly that position today.

  The calc bearer is a structural gap rather than an omission: it is not a `Settings` *value*
  (only the variable name is), it is not `_KNOWLEDGE_REPO_TOKEN_ENV`, it is not in
  `connector_token_envs` (because `config/calculators.py:295-300` deliberately makes `calc` *not*
  a connector bundle), and nothing calls `register_secret_env` for it — unlike
  `retrieval/vectors/qdrant.py:114` and `ingest/eln/warehouse/connect.py:69`, which both do.
  It has partial cover from the `Bearer <opaque>` structural rule, but only in that one spelling.
- **Evidence**: `_SECRET_SETTINGS` is `('llm_api_key', 'hpc_api_token', 'hpc_artifact_store_token',
  'temporal_api_key', 'postgres_dsn', 'postgres_migration_dsn', 'session_store_dsn',
  'note_webhook_secret', 'framing_envelope_secret')`. Run output:

  ```
  RAW      : ... llm_api_key='primaryKEY0123456789abcdef'
                 llm_fallback_api_key='fallbackKEY0123456789abcdef'
                 calc_token='calcTOKEN0123456789abcdef'
  REDACTED : ... llm_api_key='***'
                 llm_fallback_api_key='fallbackKEY0123456789abcdef'
                 calc_token='calcTOKEN0123456789abcdef'
  ```

  The `api[_-]?key` structural rule does not save the fallback either: it is anchored with `\b`
  immediately before `api`, and in `llm_fallback_api_key` the preceding character is `_`, which is
  a word character — so there is no boundary and the rule never fires.

  `tests/test_logging.py:632` asserts the inventory names only *real* fields. There is no test in
  the other direction — that every credential field is named — which is why this went unnoticed.
- **Fix**: add `"llm_fallback_api_key"` to `_SECRET_SETTINGS`; add
  `register_secret_env(settings.calc_server_token_env)` inside `connectors/calc/remote.py::_token`
  (at the read, matching the placement rule the qdrant and warehouse call sites already follow).
  Then add the missing direction to `tests/test_logging.py`: assert that every `Settings` field
  whose name matches `(api_key|token|secret|password|dsn)$` and is not in a small, argued
  allow-list (`entra_token_endpoint`, `calc_server_token_env`, `entra_sa_token_path`, …) appears
  in `_SECRET_SETTINGS`. A name heuristic is not a good *inventory*, but it is a good *alarm*.

---

## No transport constraint on the identity root of trust (`entra_jwks_url`, `entra_issuer`)

- **Severity**: low
- **Location**: `src/chemclaw/core/config/entra.py:36-37` (fields) and `:127-174`
  (`_entra_enforcement_is_configured`); consumed at `src/chemclaw/api/auth.py:83`
  (`PyJWKClient(endpoint, …)`).
- **Trigger**: a deployment that fronts its tenant keys through an internal proxy and sets
  `CHEMCLAW_ENTRA_JWKS_URL=http://keys.internal/jwks` alongside `CHEMCLAW_ENTRA_REQUIRED=true`.
  The settings object accepts it and the service boots reporting enforcement.
- **Consequence**: every token signature is verified against a key set fetched over cleartext.
  An on-path attacker inside that network segment answers the JWKS fetch with their own RSA public
  key and then mints a token with an arbitrary `oid`, `upn` and `roles` — including any role in
  `entra_privileged_roles` — that passes `validate_token`, `authorize_tool`, `authorize_trigger`
  and the skill gate. The audit trail attributes the actions to whatever principal they chose.
  Precondition is an operator choice plus network position, which is why this is low rather than
  higher, but the failure is total when it obtains.
- **Evidence**: the fields are bare `str` with no `AnyHttpUrl` type and no validator. The one
  validator in the file checks presence only:

  ```
  if not (self.entra_tenant_id or self.entra_jwks_url):
      raise ValueError("entra_tenant_id or entra_jwks_url must be set when entra_required …")
  ```

  and its docstring frames itself as catching "footguns the front-door/authorization code cannot
  catch at request time" — this is one, and it is not caught. `api/auth.py:83` passes the endpoint
  straight to `PyJWKClient`, which uses urllib and will honour `http://`. Run output:

  ```
  Settings(entra_required=True, entra_audience='api://chemclaw',
           entra_jwks_url='http://keys.attacker.example/jwks',
           entra_issuer='http://issuer.attacker.example/v2.0')
  -> accepted. jwks endpoint = http://keys.attacker.example/jwks
     issuer = http://issuer.attacker.example/v2.0
  ```
- **Fix**: extend `_entra_enforcement_is_configured` with a scheme check on the two overrides,
  scoped to `entra_required` so loopback dev and test fixtures are untouched:

  ```python
  for name, value in (("entra_jwks_url", self.entra_jwks_url),
                      ("entra_issuer", self.entra_issuer)):
      if value and not value.startswith("https://") and not is_loopback_url(value):
          raise ValueError(
              f"{name} must be https (or loopback): every authentication decision in this "
              f"deployment is made against keys fetched from it, got {value!r}"
          )
  ```

  `core.http.is_loopback_url` already exists and is the codebase's one definition of "unreachable
  from the network", so reusing it keeps this from becoming a second notion of a safe address —
  the same argument `core/http.py`'s own module docstring makes.
