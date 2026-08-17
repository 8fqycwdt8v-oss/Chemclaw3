# core kernel — design: reachability/consequence verdicts

Scope: only findings marked **critical** or **high**. Exactly one qualifies — "Three dead Entra
settings…" (high). The other eight findings in the file are medium or low and are out of scope.

Working tree checked against `HEAD` (`581e3982`) before starting: `git status --porcelain` showed
only untracked audit files, no source modification and no `MUTANT` marker, so no diff against the
pristine copy was needed.

---

## Three dead Entra settings, one of them set to a real-looking tenant URL by the shipped chart

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. Exhaustive reader census across `src/`, `tests/`, `deploy/`, `.env.example`** (not just `src/`,
and excluding `.venv`/`.git`/`__pycache__`):

```
$ grep -rn --binary-files=without-match -E "entra_token_endpoint|entra_sa_token_path|entra_token_refresh_leeway|ENTRA_TOKEN_ENDPOINT|ENTRA_SA_TOKEN_PATH|ENTRA_TOKEN_REFRESH_LEEWAY" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=__pycache__
src/chemclaw/core/config/entra.py:74:    entra_token_endpoint: str = ""
src/chemclaw/core/config/entra.py:75:    entra_sa_token_path: str = "/var/run/secrets/azure/tokens/azure-identity-token"
src/chemclaw/core/config/entra.py:76:    entra_token_refresh_leeway_seconds: float = Field(default=300.0, gt=0)
src/chemclaw/core/logging.py:446:# silently include `entra_token_endpoint` ...        <- comment
tests/test_logging.py:628:    rejects — `entra_token_endpoint` is a URL, ...        <- docstring
deploy/helm/chemclaw/values.yaml:385:  CHEMCLAW_ENTRA_TOKEN_ENDPOINT: "...TENANT/oauth2/v2.0/token"
.env.example:753-755
(remainder: docs/ and tasks/ only)
```

Per-tree counts, as the scope note asked:

| setting | `src/` executable readers | `tests/` readers | `deploy/` | `.env.example` |
|---|---|---|---|---|
| `entra_token_endpoint` | **0** (1 declaration + 1 comment) | **0** (1 docstring mention) | 1 (`values.yaml:385`) | 1 (`:753`) |
| `entra_sa_token_path` | **0** (declaration only) | **0** | **0** | 1 (`:754`) |
| `entra_token_refresh_leeway_seconds` | **0** (declaration only) | **0** | **0** | 1 (`:755`) |
| `entra_http_timeout_seconds` (control) | **live** — `api/auth.py:83` | `tests/test_auth.py:263,273` | 0 | 1 (`:756`) |

So only **one** of the three is read by the chart at all; the other two are read by nothing
anywhere, not even `values.yaml`. `entra_http_timeout_seconds` is live, as the finding says.

**2. Ruled out the dynamic readers an AST scan for literal names would miss.** Two candidates
existed and both are negative:

- `core/logging.py:732` does `getattr(settings, name, "")`, but the loop is over `_SECRET_SETTINGS`
  (`logging.py:449-464`), a hand-written 9-entry tuple that does not contain any of the three. The
  comment immediately above it (`:445-448`) says the list is *"Listed rather than derived from a
  name pattern … because deriving would silently include `entra_token_endpoint`"* — i.e. the one
  textual hit in `src/` is an explicit statement that this field is **not** in the inventory.
- `cli/validate_prose_contract.py:428` does `fields = set(Settings.model_fields)`, which consumes
  the field **names** — `check_operator_prose` requires every `CHEMCLAW_*` key appearing in the
  operator corpus to resolve to a `Settings` field, and `.env.example` is in that corpus
  (`_operator_sources`, `:285-292`). This is a name-consumer, not a value-reader, but it means the
  fix must delete the `.env.example` rows in the same commit or `make prose-validate` goes red.
  The finding's fix does say that.

**3. Reachability of the trigger — traced end to end.** The chart genuinely delivers the key:
`templates/config.yaml:16` is `{{- range $key, $value := .Values.config }}`, and
`templates/deployment-service.yaml:47-48` mounts it with `envFrom` → `chemclaw.envFrom`. So every
`helm install` renders `CHEMCLAW_ENTRA_TOKEN_ENDPOINT` into the ConfigMap and into every pod's
environment. Nothing upstream stands in the way — no validator, no startup guard. Trigger
**reachable**, exactly as described.

**4. Consequence — measured.**

```
$ uv run python -c "... Settings(_env_file=None) with CHEMCLAW_ENTRA_TOKEN_ENDPOINT set ..."
default token_endpoint = ''
set token_endpoint     = 'https://login.microsoftonline.com/real-tenant/oauth2/v2.0/token'
```

The value is parsed and stored, and then read by nothing. No exception, no log line, no metric.
"Nothing happens" is exact.

**5. Is the committed value a real tenant?** No.

```
deploy/helm/chemclaw/values.yaml:383:  CHEMCLAW_ENTRA_TENANT_ID: "00000000-0000-0000-0000-000000000000"
deploy/helm/chemclaw/values.yaml:385:  CHEMCLAW_ENTRA_TOKEN_ENDPOINT: "https://login.microsoftonline.com/TENANT/oauth2/v2.0/token"
```

The tenant slot holds the literal uppercase string `TENANT`. It is a placeholder, not a tenant id,
not a GUID, and not a real directory. Its two neighbours in the same block are the all-zeros GUID,
the same convention. **No tenant identifier is committed and nothing is leaked.**

**6. Tested the fix's own claim, which is the part that fails.** The finding says deleting the
field is behaviour-preserving except that "a deployment still passing the env var now fails loudly
at startup instead of silently doing nothing, which is the correct outcome", justified by
`extra="forbid"`. Measured:

```
$ uv run python -c "..."
env_prefix = CHEMCLAW_ | extra = forbid
pydantic-settings 2.15.0 pydantic 2.13.4
unknown key CHEMCLAW_TOTALLY_BOGUS_KEY: ACCEPTED (extra not forbidden)
field-deleted simulation: ACCEPTED silently, timeout= 10.0
```

`extra="forbid"` is indeed set, but it governs values *passed to the constructor*, not environment
variables: `EnvSettingsSource` only collects prefixed names that match a declared field and drops
the rest before validation ever sees them. I confirmed it both ways — with the real `Settings`
(unknown `CHEMCLAW_*` key accepted) and with a minimal `BaseSettings` carrying the identical
`env_prefix`/`extra` config and no `entra_token_endpoint` field, which is precisely the
post-deletion state (accepted silently).

**7. Something the reporter missed, which cuts the other way — the phantom surface is larger than
three settings.** The chart still asserts the deleted F4-T2 mechanism is live, in the present
tense, in five more places:

```
deploy/helm/chemclaw/templates/deployment-service.yaml:23:  azure.workload.identity/use: "true"
deploy/helm/chemclaw/templates/deployment-workers.yaml:27:  azure.workload.identity/use: "true"
deploy/helm/chemclaw/templates/migrate-job.yaml:32:        azure.workload.identity/use: "true"
deploy/helm/chemclaw/templates/schedules-job.yaml:30:        azure.workload.identity/use: "true"
deploy/helm/chemclaw/values.yaml:574: # Workload Identity Federation: the ServiceAccount is annotated so the pod's projected SA token can
deploy/helm/chemclaw/values.yaml:579:    azure.workload.identity/client-id: "00000000-0000-0000-0000-000000000000"
```

That label makes the Azure Workload Identity webhook inject the projected token volume and the
`AZURE_*` variables into four workloads. Nothing consumes them:

```
$ grep -rn "AZURE_CLIENT_ID\|AZURE_TENANT_ID\|AZURE_FEDERATED_TOKEN_FILE\|AZURE_AUTHORITY" src tests
(no matches)
```

So the correct scope of the cleanup is "three settings **plus** four pod labels and a
ServiceAccount annotation", not three settings.

### Why

Every factual element of the mechanism holds and I could not dent it: three settings, zero
executable readers in any tree, the chart ships one of them, the trigger is reachable from an
ordinary `helm install`, and the consequence is precisely "nothing happens, silently". The two
dynamic-reader escape hatches that could have refuted "dead" both come back negative, one of them
with the code's own comment naming the field as deliberately excluded. I would have refuted this
if I could; I cannot.

What does not hold is the framing and the severity.

**The title's alarming half is inaccurate.** "A real-looking tenant URL" implies a real tenant
identifier is committed. The tenant slot is the literal word `TENANT`, and the two neighbouring
identity values are all-zeros GUIDs. The *URL shape* is real — it is the genuine Microsoft v2.0
token endpoint format — but nothing identifying is in the repository, so the disclosure reading a
reader will take from that title is wrong.

**The consequence is genuinely nil, which caps the severity.** There is no runtime effect, no
availability effect and no security effect. The absent mechanism has no worse fallback that runs
in its place — it simply has no callers, so nothing silently degrades to a weaker path. The harm
is entirely "an operator configures something inert and is not told", which is a config-hygiene and
misleading-surface defect. Judged against the same file's own calibration, that is medium: the
reporter rates `CalculatorSettings`' false "None of them enters a cache key" claim as **medium**,
and that one has a real runtime consequence (`xtb_geometry_decimals` re-addresses every structure
and forces recomputation). A defect with strictly less consequence than a medium cannot be high.

**And the recommended fix does not deliver the benefit it claims,** which matters because the fix
is what a maintainer would act on. Deleting the field does not convert a silent no-op into a loud
startup failure — measured, pydantic-settings ignores unknown prefixed environment variables
regardless of `extra="forbid"`. Post-deletion, a deployment still passing
`CHEMCLAW_ENTRA_TOKEN_ENDPOINT` is ignored just as silently as before; only the reason changes,
from "nothing reads it" to "nothing declares it". The deletion is still worth doing — it removes a
config surface that claims a control exists — but it should be argued on that ground, and paired
with the four pod labels and the ServiceAccount annotation, not sold as a fail-fast improvement it
does not produce.
