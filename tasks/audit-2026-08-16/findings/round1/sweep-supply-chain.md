# Sweep: supply chain and dependency hygiene

Method: parsed every `import` in `src/` with `ast` (all 3rd-party top-level names), mapped every
declared dependency to the module names its installed distribution actually provides, computed the
reverse-dependency graph and the *applicable* (marker-evaluated) upper caps across the whole 241-package
locked closure, and reproduced each candidate failure by blocking the module with a `MetaPathFinder`
and importing the real entrypoint. All measurements below were run in this environment against the
synced venv and `uv.lock` as committed.

Summary of what the mechanical comparison produced:

- Declared in `pyproject.toml`, **zero** top-level imports in `src/`: `tblite`, `httpx2`.
- Imported by `src/`, **not declared**: `langsmith`, `starlette`, `pydantic`, `openpyxl`, `torch`,
  `botorch`, `linear_operator`, `cryptography`, `typing_extensions`, `snowflake`
  (`snowflake` is deliberate and documented; `typing_extensions`/`cryptography`/`pydantic` are
  discussed under "checked, not findings").

---

## `langsmith` — the egress control imports a package nothing declares, and pyproject asserts the opposite

- **Severity**: high
- **Location**: `src/chemclaw/core/egress.py:36` (`import langsmith`), reached from
  `src/chemclaw/core/config/__init__.py:78` and called at module scope on line 338.
  Contradicting claim: `pyproject.toml:23`.
- **Trigger**: any resolution of the dependency graph in which `langsmith` is not in the closure —
  i.e. `langchain-core` making its `langsmith` requirement optional (an extra), or `deepagents`
  being dropped/replaced. Nothing in `pyproject.toml` prevents `uv lock --upgrade` from producing
  that closure, because `langsmith` is not declared.
- **Consequence**: **every entrypoint of the service fails to start.** `pin_langsmith_egress` is
  called at import time of `chemclaw.core.config`, which is the one module every entrypoint imports
  (`api/app.py`, `cli/chat.py`, `cli/connectors_dev.py`, `connectors/server_entry.py`,
  `durable/background_worker.py` — the module's own docstring says so). The failure is a bare
  `ModuleNotFoundError` at config import, not a degraded mode.
  Secondary: the *version* of the library that implements a network-egress control is chosen
  entirely by two third parties. The tightest first-party-visible floor is `deepagents>=0.7.5`'s
  `langsmith>=0.10.9`; `langchain-core`'s own floor is `langsmith>=0.3.45`, and **langsmith 0.3.45
  has no `configure` at all** (measured below) — so on a closure where `deepagents` is not present,
  the resolver may legally pick a langsmith on which `pin_langsmith_egress` raises `AttributeError`
  at startup instead of disabling tracing.
- **Evidence**:

  `pyproject.toml:19-23`, the `deepagents` block, states as fact:

  ```
  # (It also drags `langchain-google-genai` in as a hard requirement: ~22 MB of Google client and
  # `langsmith` that nothing here imports. ...)
  ```

  `src/chemclaw/core/egress.py` imports it and calls into it:

  ```
  36:import langsmith
  71:    langsmith.configure(enabled=False)
  ```

  Reverse-dependency computation over `uv.lock`:

  ```
  langsmith <- ['deepagents', 'langchain-core']      # nothing first-party
  ```

  Reproduction — a `MetaPathFinder` that refuses `langsmith`, then importing the config module
  (`/tmp/no_langsmith.py`):

  ```
  $ uv run python /tmp/no_langsmith.py
  STARTUP FAILED: ModuleNotFoundError : No module named 'langsmith'
  ```

  The floor question, measured against real releases:

  ```
  $ uv run --with "langsmith==0.3.45" --no-project python -c "import langsmith; print(hasattr(langsmith,'configure'))"
  0.3.45  has configure: False
  $ ... 0.6.0 / 0.7.34 / 0.8.0 / 0.9.0 / 0.10.9  -> configure: True
  ```

  Applicable-cap analysis (markers evaluated, extras excluded) confirms nothing else constrains it:

  ```
  langsmith==0.10.17
      next-minor 0.11.0 blocked by: nothing
      next-major 1.0.0 blocked by: ['langchain-core']
  ```

  What I checked and did **not** find wrong: the control itself works. With the `lru_cache` warmed
  on `LANGSMITH_TRACING=true` *before* the pin runs — the ordering the docstring says is the real
  one — the pin still wins:

  ```
  before pin, tracing_is_enabled: True
  after pin,  tracing_is_enabled: False
  env now: false false
  handlers attached: []
  ```

  So the defect is purely the undeclared, unfloored dependency, not the mechanism.
- **Fix**: add `"langsmith>=0.6"` to `[project.dependencies]` (0.6 is the oldest release measured to
  export `configure`; `>=0.10.9` if you want to keep today's resolution), delete the
  "nothing here imports" clause from the `deepagents` comment, and add `langsmith.configure` to
  `tests/test_upstream_surface.py` — it is a third-party symbol a security control calls, which is
  exactly what that file says it exists to pin, and it currently pins nothing from `langsmith`.

---

## `tblite` is declared as a runtime dependency, is forbidden in `src/` by the repo's own test, and ships 17.5 MB into every image

- **Severity**: medium
- **Location**: `pyproject.toml:152` (`"tblite>=0.7.0"`). Contradicting enforcement:
  `tests/test_third_party_layering.py:144`. Sole importer: `tests/test_solvents.py:41,79`.
- **Trigger**: every image build. `deploy/Containerfile` runs `uv sync --frozen --no-dev`, and
  `tblite` is a project dependency, not a dev-group one, so it is installed into the runtime venv of
  the service pod, both Temporal workers, and every connector pod.
- **Consequence**: 17.5 MB of compiled Fortran/C quantum-chemistry libraries (`tblite` 7.6 MB +
  `tblite.libs` 9.9 MB of shared objects) in every production image, for a package the codebase has
  a test forbidding anyone from importing. Beyond size, it is a compiled-extension attack surface
  present in production for a test-only need, and it is one of the four names
  `tests/test_connector_isolation.py` lists as `_HEAVY` — i.e. it exists in that test only so the
  suite can assert connectors do *not* load it.
- **Evidence**:

  Mechanical check — the only declared runtime dependency whose importers are all under `tests/`:

  ```
  === runtime deps whose only importer is tests/ ===
    tblite  -> imported only by tests/: ['tblite']
  ```

  No import anywhere in `src/`:

  ```
  $ grep -rn "import tblite\|from tblite" src/    # no matches (only prose in docstrings/READMEs)
  ```

  The repo actively forbids it (`tests/test_third_party_layering.py:141-148`):

  ```
  # The xTB engine itself. **No package may import it any more** ... an `import tblite`
  # reappearing anywhere in this tree is a copy of a capability that lives elsewhere
  "tblite": "xtb",
  ```

  Nothing else in the closure needs it:

  ```
  tblite required by: {'chemclaw'}
  ```

  It is in the shipped export:

  ```
  $ uv export --no-hashes --no-dev --format requirements-txt | grep '^tblite=='
  tblite==0.7.0
  $ du -sh .venv/lib/python3.11/site-packages/tblite*
  7.6M    tblite
  9.9M    tblite.libs
  ```

  And the test that needs it passes, so it genuinely belongs in the dev group rather than being
  deleted: `uv run python -m pytest tests/test_solvents.py -q` → `13 passed in 0.47s`.
- **Fix**: move `"tblite>=0.7.0"` from `[project.dependencies]` to `[dependency-groups] dev`. Nothing
  in `src/` changes; `make test` still installs it (`uv sync` installs dev groups) and
  `uv sync --frozen --no-dev` in the Containerfile stops installing it.

---

## `starlette` — nine first-party modules import it, nothing declares it, and no package in the closure caps its major

- **Severity**: medium
- **Location**: imported directly in
  `src/chemclaw/api/middleware.py`, `api/routes/approvals.py`, `api/routes/ops.py`,
  `api/routes/plan.py`, `api/routes/proposals.py`, `api/routes/streams.py`,
  `connectors/server.py`, `core/asgi.py`, `core/worker_http.py`. Absent from
  `[project.dependencies]`.
- **Trigger**: `uv lock --upgrade` (or any fresh resolve) at a moment when a `starlette` major is
  published. Nothing constrains it.
- **Consequence**: a Starlette major lands directly into nine first-party modules with no
  declaration to block it and no floor to guarantee the symbols they name. The imported surface is
  broad and includes the pieces most likely to move in a major:
  `BaseHTTPMiddleware` / `RequestResponseEndpoint`, `MutableHeaders`, `Headers`, `ASGIApp`,
  `Message`, `Receive`, `Scope`, `Send`, `Route`, `Starlette`, `CORSMiddleware`,
  `JSONResponse`/`PlainTextResponse`/`Response`, `Request`. This has already happened once
  silently: `fastapi==0.141.1` requires `starlette>=0.46.0` with no upper bound, and the closure
  resolved **1.6.0** — a 0.x → 1.x major that no line in this repository asked for or reviewed.
- **Evidence**: applicable-cap analysis over the installed closure (markers evaluated, extra-gated
  requirements excluded — `starlette[full]`'s httpx pin is not installed and was excluded):

  ```
  starlette==1.6.0
      next-minor 1.7.0 blocked by: nothing
      next-major 2.0.0 blocked by: NOTHING — a major can land unannounced
  ```

  Requirement strings from the installed dists, i.e. everyone who asks for starlette:

  ```
  fastapi==0.141.1     -> starlette>=0.46.0
  mcp==1.29.0          -> starlette>=0.27      (py<3.14)
  sse-starlette==3.4.8 -> starlette>=0.49.1
  ```

  All three are floors; none is a ceiling.
- **Fix**: declare `"starlette>=1.6,<2"` in `[project.dependencies]`. It is a direct dependency by
  the repository's own stated rule — the same rule the `httpx` and `langchain-core` comments in
  `pyproject.toml` invoke ("Declared, not left transitive ... eight first-party modules import it
  directly").

---

## `openpyxl` — the front door's import graph depends on an Excel library whose only supplier is a reaction-fingerprint package, with a bare unversioned requirement

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/parse.py:30` (`from openpyxl import load_workbook`).
  Absent from `[project.dependencies]`.
- **Trigger**: `drfp` dropping or gating its `openpyxl` requirement in any future release — an
  entirely plausible change, since `drfp` is a reaction-fingerprint library and openpyxl is
  unrelated to what this repository uses `drfp` for (`science/fingerprints`).
- **Consequence**: **`chemclaw.api.app` — the FastAPI front door — stops importing.** Not a degraded
  document-ingest path: a hard startup failure, because `parse.py` imports `openpyxl` at module
  scope and is on the app's import graph. Additionally there is no floor *and* no ceiling anywhere
  in the closure: `drfp` requires the bare name `openpyxl`, so any version at all satisfies the
  graph, including a major that removes `load_workbook`'s `read_only`/`data_only` keywords that
  `_parse_xlsx` passes.
- **Evidence**:

  ```
  openpyxl <- ['drfp']
  drfp==0.3.7  ->  openpyxl        # no specifier, no marker, no extra

  openpyxl==3.1.5
      next-minor 3.2.0 blocked by: nothing
      next-major 4.0.0 blocked by: NOTHING — a major can land unannounced
  ```

  Reproduction (`/tmp/blockmod.py` refuses `openpyxl`, then imports the real module):

  ```
  $ uv run python /tmp/blockmod.py openpyxl chemclaw.api.app
  FAIL: chemclaw.api.app without openpyxl -> ModuleNotFoundError: No module named 'openpyxl'
  $ uv run python /tmp/blockmod.py openpyxl chemclaw.ingest.documents.parse
  FAIL: chemclaw.ingest.documents.parse without openpyxl -> ModuleNotFoundError: No module named 'openpyxl'
  ```
- **Fix**: declare `"openpyxl>=3.1,<4"` in `[project.dependencies]`.

---

## `torch` / `botorch` / `linear_operator` — imported directly, supplied only through the `bofire[optimization]` extra

- **Severity**: medium
- **Location**: `src/chemclaw/science/bo/engine.py:29` (`import torch`), `:54`
  (`from botorch.exceptions.errors import BotorchError, ModelFittingError`), `:55`
  (`from linear_operator.utils.errors import NanError, NotPSDError`). None declared.
- **Trigger**: `bofire` changing what its `optimization` extra pulls in — e.g. moving to a different
  surrogate backend, or splitting `botorch[fully_bayesian]` out. The chain is
  `bofire[optimization] -> botorch[fully_bayesian]>=0.18.1 -> torch>=2.4, linear_operator>=0.6.1`;
  four links, none of them owned here.
- **Consequence**: the BO connector worker stops importing. These three are also the *error* types
  that `_translating_surrogate_errors` catches to produce `SurrogateFitError` — the module docstring's
  Science-4 boundary claim ("Errors leak nowhere past this boundary") rests on symbol paths in
  packages this project does not name. `botorch` and `torch` majors are additionally uncapped by
  anything in the closure.
- **Evidence**:

  ```
  torch           <- ['botorch', 'linear-operator']
  botorch         <- ['bofire[optimization]']
  linear-operator <- ['botorch', 'gpytorch']

  botorch==0.18.1  next-major 1.0.0 blocked by: NOTHING
  torch==2.13.0    next-major 3.0.0 blocked by: NOTHING
  ```

  Reproduction:

  ```
  $ uv run python /tmp/blockmod.py torch chemclaw.connectors.bo.worker
  FAIL: chemclaw.connectors.bo.worker without torch -> ModuleNotFoundError: No module named 'torch'
  $ uv run python /tmp/blockmod.py torch chemclaw.science.bo.engine
  FAIL: chemclaw.science.bo.engine without torch -> ModuleNotFoundError: No module named 'torch'
  ```

  Corroborating: `pyproject.toml`'s own mypy overrides already list `linear_operator.*` as a module
  this repository imports — the tree knows it is a direct dependency in one config block and not in
  the other.
- **Fix**: declare `"botorch>=0.18.1,<1"`, `"torch>=2.4"` and `"linear-operator>=0.6.1"` — or, if the
  intent is that these travel with `bofire`, stop importing them at `science/bo/engine.py` and catch
  the boundary errors by a `bofire`-exported alias. Declaring is the smaller change.

---

## `scipy>=1.11`'s stated justification names two modules that no longer exist

- **Severity**: low
- **Location**: `pyproject.toml:146-150`.
- **Trigger**: reading the file to decide whether the floor may be raised or lowered.
- **Consequence**: the floor is justified by a feature the code does not use — the exact failure
  mode the `langchain>=1.3.14` comment two dozen lines above names and says it was corrected for
  ("A pin justified by code that does not exist is the failure this tree's own rule names"). It is
  currently harmless (nothing needs scipy < 1.11), but the next person reasoning about scipy will
  reason from a false premise about what the project uses.
- **Evidence**: the comment reads:

  ```
  146:    # Promoted from transitive (scikit-learn/bofire) to declared when `calc.xtb_opt` and
  147:    # `calc.xtb_thermo` began importing it directly: the L-BFGS-B optimizer over tblite's
  148:    # analytic gradient, and the null-space projection that separates a molecule's
  149:    # vibrations from its translations and rotations.
  150:    "scipy>=1.11",
  ```

  Neither module exists:

  ```
  $ find src -name "xtb_opt*" -o -name "xtb_thermo*" | wc -l
  0
  $ ls src/chemclaw/science/calc/
  __init__.py artifacts.py calibration.py logd.py models.py postgres_artifacts.py
  postgres_store.py solvents.py store.py thermo.py uncertainty.py
  ```

  The entire remaining scipy usage in `src/` is one symbol, and it is only the second half of the
  justification (the null-space projection); the L-BFGS-B optimizer left with the physics:

  ```
  $ grep -rn "import scipy\|from scipy" src/
  src/chemclaw/science/calc/thermo.py:41:from scipy.linalg import null_space
  ```

  `scipy.linalg.null_space` has been present since scipy 1.1.0; `>=1.11` is not held up by it
  (verified importable on 1.11.4).
- **Fix**: rewrite the comment to name `science/calc/thermo.py:41`'s `null_space` as the only
  first-party use, and either keep `>=1.11` for an honest reason (it is what `scipy-stubs` and
  `mypy --strict` are measured against) or state that the floor is arbitrary-but-recent.

---

## CI installs its supply-chain tooling from mutable tags

- **Severity**: low
- **Location**: `.github/workflows/image.yml:124,135-138` (syft), `:38,41,148` and
  `.github/workflows/ci.yml:75,80,154,161` (actions).
- **Trigger**: an upstream repository (or an account with write access to it) re-pointing a git tag.
  Git tags are mutable; `actions/checkout@v4` and `astral-sh/setup-uv@v5` are *branch-like* moving
  major tags by design, and `anchore/syft@v1.35.0` is a release tag fetched over raw.githubusercontent
  and piped into `sh` with no checksum.
- **Consequence**: arbitrary code execution inside the image job, which holds `actions: write` and a
  populated uv cache. This is the same defect class the repository already reasons about carefully
  for its own artifacts (`deploy/Containerfile` documents the floating-base trade-off explicitly and
  `deploy/helm/chemclaw/values.yaml:14` carries an `image.digest` field for release pinning), so the
  gap is an inconsistency rather than an unconsidered risk.
- **Evidence**:

  ```
  .github/workflows/ci.yml:75:      - uses: actions/checkout@v4
  .github/workflows/ci.yml:80:        uses: astral-sh/setup-uv@v5
  .github/workflows/ci.yml:161:        uses: azure/setup-helm@v4
  .github/workflows/image.yml:41:        uses: astral-sh/setup-uv@v5
  .github/workflows/image.yml:148:        uses: actions/upload-artifact@v4

  .github/workflows/image.yml:137:  curl -sSfL "https://raw.githubusercontent.com/anchore/syft/${SYFT_VERSION}/install.sh" \
  .github/workflows/image.yml:138:    | sh -s -- -b /usr/local/bin
  ```

  The comment at `:131-134` calls `v1.35.0` "Pinned", and verifies only that the tag *resolves* —
  which a re-pointed tag also does.
- **Fix**: pin every `uses:` to a full commit SHA with the tag in a trailing comment, and fetch syft
  by SHA (`raw.githubusercontent.com/anchore/syft/<sha>/install.sh`) or verify the installer's
  checksum before piping it to `sh`.

---

## `httpx2` is declared and nothing imports it; the LLM request path mixes the two stacks (measured: currently benign)

- **Severity**: low
- **Location**: `pyproject.toml:63` (`"httpx2>=2.10"`); the mixing site is
  `src/chemclaw/agent/llm_provider.py:339-345` (`_tls_http_client` returns an `httpx.AsyncClient`)
  handed to `ChatOpenAI(http_async_client=...)` at `:261`.
- **Trigger**: `CHEMCLAW_LLM_PROVIDER=openai_compatible` with `llm_tls_ca_bundle` set — the
  documented production target — on any request.
- **Consequence today**: none that I could produce. Reporting it because the sweep asked which
  modules mix the stacks and whether any request path does, and the answer is "one does, and it
  works only because httpx 0.28 and httpx2 2.10 are still API-compatible and openai's base client
  has a broad `except Exception` fallback". That is an undeclared coupling to two libraries' internal
  agreement, not a property either project promises. Separately, `httpx2` is declared while nothing
  in `src/` imports it — `openai>=3.1` requires `httpx2<3,>=2.7.0` unconditionally, so the line
  changes no resolution.
- **Evidence**: the stacks are genuinely distinct classes:

  ```
  openai 3.1.0 -> transport module: httpx2 2.10.0
  httpx 0.28.1  httpx2 2.10.0
  same AsyncClient class? False
  httpx2.ConnectError mro: (httpx2.ConnectError, httpx2.NetworkError, ... )
  httpx.ConnectError  mro: (httpx.ConnectError,  httpx.NetworkError,  ... )
  httpx2.TimeoutException is httpx.TimeoutException: False
  ```

  Four measured behaviours, `httpx2` client vs the `httpx` client `_tls_http_client()` actually
  returns, against local servers (`/tmp/repro_mix.py`, `/tmp/repro_conn.py`, `/tmp/repro_timeout.py`,
  `/tmp/repro_stream.py`):

  ```
  unary completion   httpx2: OK -> hi        httpx: OK -> hi
  streaming + usage  httpx2: OK 'hello' usage=(3,2,5)   httpx: OK 'hello' usage=(3,2,5)
  connect refused    httpx2: openai.APIConnectionError  httpx: openai.APIConnectionError
  server never answers, timeout=2.0
                     httpx2: APITimeoutError after 2.3s httpx: APITimeoutError after 2.0s
  ```

  So the AG-12 failover set in `_failover_exceptions()` (`APIConnectionError`,
  `InternalServerError`) still fires and `settings.llm_timeout_seconds` is still honoured. The
  declared-but-unimported check:

  ```
  === DECLARED deps with NO top-level import in src/ ===
    httpx2       provides ['..', 'httpx2']
    tblite       provides ['tblite', 'tblite.libs']
  ```
- **Fix**: either leave the declaration and correct the comment at `pyproject.toml:50-62` to say
  plainly that no first-party module imports `httpx2` and it is declared to make the transport
  choice visible (not because "four first-party modules import it directly", which is the reason
  given for `httpx` immediately above it and is not true of `httpx2`); or make
  `_tls_http_client()` return an `httpx2.AsyncClient`, which is the client the SDK it is handed to
  actually runs on and would give the declaration a first-party importer.

---

# Checked and found clean (recorded so the next sweep does not redo it)

- **`make deps-audit`**: green. `No known vulnerabilities found` over the 213-package shipped
  export. I also audited the **full** closure including the dev group (875 export lines, which
  `deps-audit`'s `--no-dev` never sees): also `No known vulnerabilities found`. The classification
  logic in the recipe (real finding vs unreachable advisory DB) behaved as documented.
- **`uv lock --check`**: the lockfile is in sync with `pyproject.toml` (`Resolved 247 packages`).
- **Contradicting floors**: I looked specifically for the shape the `langchain-openai` comment
  describes (two floors where the resolver silently takes the older). Computing every *applicable*
  cap in the closure against every declared floor, there is no remaining case. The only binding
  caps on declared packages are self-imposed (`deepagents<0.8`, `mcp<2`) or benign
  (`langchain` caps `langgraph<1.3.0`; the declared floor is `>=1.2.10` and 1.2.11 resolves).
  `langchain-openai>=1.5.1` does require `openai<4.0.0,>=2.45.0` as the comment claims — verified.
- **`tests/test_upstream_surface.py` vs actual upstream-internal use**: the file's `measured` floors
  (`langchain` 1.3.14, `langgraph` 1.2.10, `deepagents` 0.7.5, `langchain-mcp-adapters` 0.3.2) match
  `pyproject.toml` exactly. I enumerated private-attribute access on upstream objects across `src/`:
  the only ones are `connectors/server.py:306,359` (`server._tool_manager`, an `mcp.server.fastmcp.FastMCP`
  private) and `core/metrics.py:112` (first-party). The `mcp` one **is** ratcheted, just not in
  `test_upstream_surface.py` — `tests/test_connector_transport.py:372` pins it explicitly. The one
  gap is `langsmith.configure`, covered as a finding above.
- **`core/egress.py`'s central claim**: verified by measurement (warm `lru_cache` on a truthy
  `LANGSMITH_TRACING`, pin applied afterwards, `tracing_is_enabled()` → False and
  `CallbackManager.configure()` attaches zero handlers). The docstring is accurate.
- **`openinference-instrumentation-langchain` declares no runtime constraint on `langchain-core`**
  (only extras-gated `==1.3.3` for type-check and `>=0.3.9` for instruments), which looked like a
  hazard. Measured against the installed `langchain-core 1.5.5`: it still emits one LLM span with
  `llm.token_count.prompt/.completion/.total`, `llm.provider` and `openinference.span.kind=LLM`.
  Working today; worth re-running on a `langchain-core` bump, since nothing would catch it going
  quiet.
- **`pydantic`, `typing_extensions`, `cryptography`**: imported but undeclared, and I decided each
  is not worth a finding. `pydantic` is capped `<3` by nine packages in the closure and floored
  `>=2.11` by `mcp`, so neither a silent major nor a too-old resolve is reachable.
  `typing_extensions` is required by 38 packages. `cryptography` has one site
  (`ingest/eln/warehouse/snowflake.py`), which is itself a module designed to be importable only
  where a binding names it, and it arrives via the declared `pyjwt[crypto]`.
- **`deploy/Containerfile`** base-image floating tag: it is a stated, argued compromise with `dnf -y
  update` and a build-arg escape hatch, and `deploy/helm/chemclaw/values.yaml` carries an
  `image.digest` field for releases. Not a finding.
