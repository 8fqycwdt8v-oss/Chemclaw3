# core kernel — design: reproduction verdicts

Lens: **does it actually reproduce?** Scope: critical/high only. The file contains exactly one
such finding; the other eight are medium/low and were not verified.

Working tree checked at `581e39826af02add1d550882a06c9bc339a36e28`. Every file this verdict rests
on (`core/config/entra.py`, `deploy/helm/chemclaw/values.yaml`, `.env.example`, `api/auth.py`) was
`diff`ed against the pristine `HEAD` copy at
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/pristine` and is
byte-identical — no other agent's mutation is in play here.

---

## Three dead Entra settings, one of them set to a real-looking tenant URL by the shipped chart

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. Re-derived the reader counts myself.** I did not run the reporter's scan. I wrote my own,
deliberately *stricter* — it walks `ast.Name`, `ast.Attribute` **and** `ast.Constant` string
literals, so a dynamic `getattr(settings, "entra_token_endpoint")` or a
`monkeypatch.setattr(settings, "…")` would be caught, which a Name/Attribute-only scan misses:

```
$ uv run python /tmp/probe/entra_dead.py
== AST references (Name/Attribute/str-literal), excluding the declaring module ==
  entra_http_timeout_seconds             -> ['src/chemclaw/api/auth.py:83', 'tests/test_auth.py:273']
  entra_sa_token_path                    -> NO REFERENCE
  entra_token_endpoint                   -> NO REFERENCE
  entra_token_refresh_leeway_seconds     -> NO REFERENCE
```

**2. Grepped every surface the scope asked for**, both the Python identifier and the `CHEMCLAW_*`
env spelling, across the whole repo (excluding only `.git`/`.venv`/`node_modules`):

| setting | `src/` | `tests/` | `deploy/` | `.env.example` |
|---|---|---|---|---|
| `entra_token_endpoint` | 1 (declaration) + 1 **comment** (`core/logging.py:446`) | 0 | **1** (`values.yaml:385`) | 1 (`:753`) |
| `entra_sa_token_path` | 1 (declaration) | 0 | 0 | 1 (`:754`) |
| `entra_token_refresh_leeway_seconds` | 1 (declaration) | 0 | 0 | 1 (`:755`) |
| `entra_http_timeout_seconds` *(the live control)* | **`api/auth.py:83`** | `test_auth.py:273` | **0** | 1 (`:756`) |

`core/logging.py:446` is prose only. I read the code around it: redaction iterates `_SECRET_SETTINGS`
(`logging.py:449-464`) and calls `getattr(settings, name)` at `:732` for names in that tuple —
`entra_token_endpoint` is not in it. It is named in the comment as an example of what a
*name-pattern* derivation would wrongly sweep in. Not a reader.

**3. Rendered the chart** rather than trusting the YAML, to prove the key reaches a pod:

```
$ helm template chemclaw deploy/helm/chemclaw | grep -n CHEMCLAW_ENTRA
2453:  CHEMCLAW_ENTRA_AUDIENCE: "api://chemclaw"
2454:  CHEMCLAW_ENTRA_PRIVILEGED_ROLES: ""
2455:  CHEMCLAW_ENTRA_REQUIRED: "true"
2456:  CHEMCLAW_ENTRA_TENANT_ID: "00000000-0000-0000-0000-000000000000"
2457:  CHEMCLAW_ENTRA_TOKEN_ENDPOINT: "https://login.microsoftonline.com/TENANT/oauth2/v2.0/token"
```

`templates/config.yaml:16` ranges `.Values.config` into `ConfigMap/chemclaw-config`, and all five
workloads mount it with `envFrom: configMapRef: chemclaw-config`. So the value genuinely lands in
every pod's environment.

**4. Ran the operator scenario end to end** — substituted a real-shaped tenant GUID as the finding's
trigger describes, with `warnings.simplefilter("error")` so any warning would abort:

```
$ uv run python /tmp/probe/entra_runtime.py
accepted:
  entra_token_endpoint            = https://login.microsoftonline.com/72f988bf-…-2d7cd011db47/oauth2/v2.0/token
  entra_sa_token_path             = /var/run/secrets/somewhere/else/token
  entra_token_refresh_leeway_secs = 1.0
no exception, no warning raised.

any outbound token-mint call site in src/?
  NONE
```

(the second half greps `src/` for `oauth2/v2.0/token|client_assertion|grant_type|urn:ietf:params:oauth`
— zero hits, so there is no token-minting path for these to configure). No metric either: nothing
in `src/` publishes a token-mint or federation series.

**5. Tested the finding's fix rationale**, which turned out to be measurably false — see below.

**6. Checked the sibling repo** for the name-collision that gives the `calculators.py` precedent its
teeth: `search_code "entra repo:8fqycwdt8v-oss/Chemclaw3-mcp"` → `total_count: 0`.

### Why

**The mechanism is real and reproduces exactly.** Three fields declared at
`core/config/entra.py:74-76` — line numbers and symbols current — with zero readers anywhere in
`src/` or `tests/`, one of them shipped into every pod's environment by the chart under the heading
`# Entra identity is mandatory system-wide` with no caveat attached. An operator who fills in their
tenant gets no error, no warning, no metric and no effect. All of that I reproduced independently.

Four things are exaggerated, and together they move this off `high`.

**1. The title's "real-looking tenant URL" does not hold.** The audit scope asks me to say exactly
what is committed and whether it is real. It is
`https://login.microsoftonline.com/TENANT/oauth2/v2.0/token` — the tenant slot is the literal
uppercase word `TENANT`. That is an unambiguous template placeholder, not a real tenant, not a GUID,
not a customer domain, and not one of Microsoft's magic values (`common`, `organizations`,
`consumers`). The adjacent `CHEMCLAW_ENTRA_TENANT_ID` is `00000000-0000-0000-0000-000000000000`, the
nil GUID — also plainly a placeholder. **Nothing about any real directory is committed to this
repository.** Only the host (`login.microsoftonline.com`) is genuine, which makes the *shape* real
and the *identity* obviously blank. The finding's own body concedes this when it says the operator
"substitutes their tenant" — substitution is what a placeholder is for. "A real-looking tenant URL"
in a finding title reads as a leaked identifier; it is not one.

**2. "There is no error, warning or metric that says so" is true of the chart path only.** The
finding lists a second trigger — an operator setting `CHEMCLAW_ENTRA_SA_TOKEN_PATH` from
`.env.example:754`. The four comment lines immediately above that row (`.env.example:749-752`) say:
*"Nothing mints a token today — the workload federation and On-Behalf-Of exchanges were deleted
unused — but the timeout below also bounds the front door's JWKS fetch, which is live."* That is
precisely the warning the finding says does not exist, sitting directly above the line being edited.
The field's own comment (`entra.py:68-73`) says the same. So half the stated trigger surface is
already annotated, and only the `values.yaml` path — which I confirmed carries no such note — is
genuinely silent.

**3. The severity rests on an analogy that does not transfer.** The finding's case for `high` is
that this is "the identical defect one file over" from `calculators.py`, which has a build-failing
test. But the `calculators.py` harm is sharp for a reason that is absent here, and the test's own
docstring says so: *"the server reads the same names under the same `CHEMCLAW_` prefix. An operator
who set `CHEMCLAW_XTB_OPT_MAX_STEPS` on this deployment got no error, no warning and no effect,
while the identically-spelled setting on the calculation server was the one that actually decided
the calculation."* That is a knob set on the wrong deployment silently overriding nothing while the
right deployment quietly used a different value — a wrong-answer path. I checked whether the same
collision exists for Entra: `Chemclaw3-mcp` contains **zero** references to `entra` anywhere. There
is no second reader, no divergent value, no wrong answer. What remains is one operator setting a
string that does nothing — no security consequence (nothing is minted, no credential is handled, no
authorization decision changes), no availability consequence, no correctness consequence. That is
misleading configuration surface, which is medium. It is worth deleting; it is not worth `high`.

**4. The fix's central safety claim is measurably wrong.** The finding argues the deletion is
correct because *"`extra="forbid"` is on the composed `Settings`, so the only visible change is that
a deployment still passing the env var now fails loudly at startup instead of silently doing
nothing."* I measured it:

```
$ uv run python …
env_prefix: CHEMCLAW_ extra: forbid
known var picked up: api://probe          # CHEMCLAW_ENTRA_AUDIENCE -> proves the env source works
  CHEMCLAW_ENTRA_TOKEN_ENDPOINT      -> accepted silently
  CHEMCLAW_ENTRA_NOT_A_FIELD         -> accepted silently
  CHEMCLAW_BOGUS                     -> accepted silently
pydantic-settings 2.15.0
```

`extra="forbid"` governs extra keys passed to the *model*; it does not reject unknown
prefix-matching **environment variables** in pydantic-settings 2.15.0. Deleting the three fields
therefore does not convert silence into a loud failure — it relocates the silence from "a field
nobody reads" to "an env var nobody parses". The only part of the proposed fix that actually removes
the trap is deleting the `values.yaml:385` key. A reviewer acting on this finding as written would
ship the deletion believing a startup guard now exists that does not.

### What the reporter missed, which makes the *mechanism* worse than described

Two things strengthen the finding even as its severity comes down, and both are better arguments
than the one given:

**The chart ships the dead setting and omits the live one.** Of the four settings in that block,
exactly one has a reader — `entra_http_timeout_seconds` (`api/auth.py:83`, bounding the JWKS
fetch). It appears in `.env.example:756` and **not** in `values.yaml` at all. The rendered ConfigMap
above shows it: the chart names `CHEMCLAW_ENTRA_TOKEN_ENDPOINT`, which does nothing, and says
nothing about the timeout, which bounds a real outbound call. The finding notes the block "reads as
a working group of four"; the chart actually inverts the signal, surfacing precisely the inert
member.

**The test written to catch this exact mistake is green, and declares itself the only place the
mistake can be caught.** `tests/test_helm_chart.py:180` is
`test_chart_config_keys_have_a_consumer`, docstring: *"Every `CHEMCLAW_*` key the chart injects has
a reader. … a key that is none of those is accepted silently by pydantic-settings when it arrives as
an environment variable, so the operator who sets it gets no error and no effect. **This is the only
place that mistake can be caught.**"* Its actual predicate is `_field_for(key) in
Settings.model_fields` — the *existence of a field*, not a reader of it. So a dead field satisfies
it:

```
$ uv run pytest tests/test_helm_chart.py::test_chart_config_keys_have_a_consumer \
                tests/test_config.py::test_no_calculator_setting_is_declared_without_a_reader -q
..                                                                       [100%]
2 passed in 2.94s
```

The guard that exists for this, and claims exclusivity over it, passes while the condition it
describes holds. That is a stronger and more actionable statement of the defect than "the identical
defect one file over", and it points at the better fix: tighten
`test_chart_config_keys_have_a_consumer`'s definition of "reader" from *a field is declared* to *a
field is read*, which catches this key and any future one without needing the `calculators.py` test
widened at all.
