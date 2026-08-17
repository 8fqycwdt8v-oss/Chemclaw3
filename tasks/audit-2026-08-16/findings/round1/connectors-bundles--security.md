# Connector bundles (`calc`, `bo`, `qm`, `chem`, `safety`, `molfp`, `rxnfp`) — security and hardening

Slice: `src/chemclaw/connectors/{calc,bo,qm,chem,safety,molfp,rxnfp}/` including every
`connector.yaml`. Shared connector runtime (`server.py`, `manifest.py`, `registry.py`,
`identity.py`, `caller.py`, `jobs.py`) read as context for how the bundles' declarations are
enforced.

Seven findings. All reproductions were run in this environment with `uv run`; the scripts are under
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/`.

---

## The four connectors this repo actually runs serve their whole MCP surface unauthenticated

- **Severity**: high
- **Location**: `src/chemclaw/connectors/calc/connector.yaml:31-32`,
  `src/chemclaw/connectors/bo/connector.yaml:18-19`,
  `src/chemclaw/connectors/molfp/connector.yaml:22-23`,
  `src/chemclaw/connectors/rxnfp/connector.yaml:18-19` (`auth: mode: none`), enforced by
  `src/chemclaw/connectors/server.py:199-228` (`BearerAuthMiddleware.dispatch`) and
  `src/chemclaw/connectors/server.py:117-130` (`_declared_bearer_env`).
- **Trigger**: any process that can open a TCP connection to a connector pod's `connectorPort`
  POSTs an MCP `initialize` to `/mcp` with **no** `Authorization` header.
- **Consequence**: every authorization decision in this system is made in core
  (`agent/authz.py`, `agent/tool_authz.py`, the D-167 plan gate that reads
  `state_changing`/`read_only` out of these same manifests). The connector itself decides nothing —
  `_declared_bearer_env` returns `None` for `mode: none` and `dispatch` calls `call_next`
  unconditionally. So the whole gate stack is an alternate-code-path bypass:
  - `find_calculations()` with every filter empty returns the newest
    `calc_find_max_results` (default 50) rows of *every calculation the organisation has ever run*,
    each with its full result payload; `list_artifacts` + `fetch_artifact` then read the stored
    geometries, Hessians and logs behind them (`calc/server/tools.py:202-409`).
  - `resume_campaign(campaign_id)` returns another chemist's decision space and the observation
    history behind it (`bo/server/tools.py:641-668`); the id is a pure hash of the decision space
    (`science/bo/campaign_record.py::campaign_id_for`), so it is derivable, not secret.
  - `report_measurement` writes the calibration ledger that `calculator_trust` reports from
    (`calc/server/tools.py:133-181`) — the numbers an agent quotes as "how far to trust this
    calculator".
  - every compute tool spends real CPU on the physics server behind `calc_server_url`, using the
    pod's own credential.
  The `bo` bundle's own code already concedes half of this — `_recorded_provenance`
  (`bo/server/tools.py:363-398`) marks the actor `unverified:` precisely because "the pod does not
  even authenticate *core*". It fixes the attribution and leaves the access.
  The compensating control is the chart's `connector-ingress` NetworkPolicy
  (`deploy/helm/chemclaw/templates/networkpolicy.yaml:127-176`), which admits Chemclaw's own pods
  **and every namespace in `networkPolicy.monitoringNamespaces`** — and its own comment states that
  the grant "is not 'metrics only'" because the same port carries `/mcp`. A compromised Prometheus
  (or any other pod in this release, including a connector pod itself) therefore reaches the full
  surface above.
  The manifest validator does not catch it:
  `HttpEndpoint._a_networked_endpoint_carries_a_credential` (`manifest.py:134-158`) tests the
  *declared* loopback URL and is explicitly documented as not testing the deployed one, which the
  chart always overrides (`templates/config.yaml:30`, `_helpers.tpl:457`).
- **Evidence**: `scratchpad/probe_auth.py` and `scratchpad/probe_unauth.py`. The second builds the
  real `connector_app(...)` for each bundle and POSTs an MCP `initialize` with no credential (the
  lifespan is deliberately not run, so "reached the MCP transport" surfaces as the session
  manager's own `RuntimeError`):

  ```
  calc    declared bearer env -> None
  bo      declared bearer env -> None
  molfp   declared bearer env -> None
  rxnfp   declared bearer env -> None
  chem    declared bearer env -> 'CHEMCLAW_CHEM_TOKEN'
  safety  declared bearer env -> 'CHEMCLAW_SAFETY_TOKEN'

  calc   no-auth POST /mcp -> REACHED MCP TRANSPORT (Task group is not initialized...)
  bo     no-auth POST /mcp -> REACHED MCP TRANSPORT (Task group is not initialized...)
  molfp  no-auth POST /mcp -> REACHED MCP TRANSPORT (Task group is not initialized...)
  rxnfp  no-auth POST /mcp -> REACHED MCP TRANSPORT (Task group is not initialized...)
  chem   no-auth POST /mcp -> HTTP 401 'unauthorized'
  safety no-auth POST /mcp -> HTTP 401 'unauthorized'
  ```

  The asymmetry is the argument: the two bundles this repo does **not** run are credential-gated,
  the four it does run are open, and there is no technical obstacle — both halves of the bearer
  path already ship (`identity.py::_EnvBearerAuth` on the client, `BearerAuthMiddleware` on the
  server) and are proven working by `chem`/`safety`.
- **Fix**: give the four bundles the same declaration `chem`/`safety` already carry, e.g. in
  `calc/connector.yaml`:

  ```yaml
  auth:
    mode: bearer
    token_env: CHEMCLAW_CALC_CONNECTOR_TOKEN
  ```

  plus the matching secret in `values.yaml`/`config.yaml` (the chart already mounts
  `CHEMCLAW_CHEM_TOKEN`/`CHEMCLAW_SAFETY_TOKEN`, so this is one more entry per bundle). Then
  strengthen `_a_networked_endpoint_carries_a_credential` — or add a `connector-validate` rule — so
  a bundle whose server this repository *ships* cannot declare `mode: none` at all, which is the
  case the loopback exemption was never meant to cover.

---

## `expensive: true` on CREST is bypassed by `level: "thorough"` on three ungated jobs

- **Severity**: high
- **Location**: `src/chemclaw/connectors/calc/connector.yaml` — `compute_reaction_energy`,
  `compare_solvents`, `scan_coordinate` carry no `expensive:` key, while `sample_conformers` and
  `compute_interaction_energy` do. Path:
  `src/chemclaw/connectors/calc/compose.py:604-613` (`_species_energy`, `if level == "thorough"`)
  → `compose.py:365-409` (`conformer_ensemble` → `cached_remote(..., "search_conformer_ensemble")`).
  Gate: `src/chemclaw/connectors/jobs.py:302-303` (`if job.expensive: authorize_trigger(job.name)`)
  and `src/chemclaw/agent/authz.py:249-275` (`expensive_actions` derives its set from
  `job.expensive`).
- **Trigger**: call the ungated `compute_reaction_energy` (or `compare_solvents`) with
  `level: "thorough"`.
- **Consequence**: `authorize_trigger` is never invoked, so under a real deployment
  (`entra_required=true`) a user with **no** privileged role runs exactly the calculation the
  privileged-role gate exists to protect. The manifest states the rationale in its own words —
  "A CREST search is the one genuinely costly thing in this bundle — minutes of saturated CPU, and
  unlike the xTB tasks its cost is not bounded by the input's size. So it carries the role gate
  that used to sit on `run_xtb_task`" — and then leaves three other tools that reach the identical
  remote call with no gate at all. This is the comment asserting a control the code does not have.
- **Evidence**: `scratchpad/probe_crest.py` stubs `compose.remote_call`/`compose.cached_remote` to
  record the first remote tool each path asks for:

  ```
  ReactionJobSpec validated: {'kind': 'reaction', 'reactants': ['O'], 'products': ['O'],
                              'solvent': None, 'temperature_k': None, 'level': 'thorough',
                              'symmetry_numbers': None}
  compute_reaction_energy(level=thorough) reached remote tool -> search_conformer_ensemble
  sample_conformers (expensive: true) reached remote tool -> search_conformer_ensemble
  ```

  and `scratchpad/probe_gate.py` reads the shipped manifests:

  ```
  bo     start_optimization_campaign  expensive=True
  calc   compute_reaction_energy      expensive=False
  calc   compare_solvents             expensive=False
  calc   scan_coordinate              expensive=False
  calc   sample_conformers            expensive=True
  calc   compute_interaction_energy   expensive=True
  qm     compute_dft_energy           expensive=True
  ```
- **Fix**: the cheapest correct change is to make the *level* carry the gate rather than the tool.
  Either (a) add `expensive: true` to `compute_reaction_energy` and `compare_solvents` — blunt, it
  gates the cheap `quick`/`standard` cases too — or (b) preferably, extend the declared
  `precondition` on those two jobs to call `authorize_trigger("sample_conformers")` (or a dedicated
  action name) when `spec.level == "thorough"`, so the entitlement follows the work rather than the
  tool name. `prepare_job_launch` already runs the precondition on both launchers (agent tool and
  template step), so one function covers every path.

---

## No count cap on the durable calc job specs — one call implies 12,600 CREST searches

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/calc/specs.py:45-82` — `reactants`, `products` and
  `solvents` are `Field(min_length=1)` and `values` is `Field(min_length=2)`, none with
  `max_length`. Fan-out at `compose.py:720-732` (one `_species_energy` per list entry) and
  `compose.py:854` (`asyncio.gather` over `[None, *solvents]`).
- **Trigger**: `compare_solvents` with 300 distinct species on each side (a trivially balanced
  equation: the same multiset both sides) and all 42 supported ALPB solvent names, at
  `level: "thorough"`.
- **Consequence**: `require_supported_solvents` — the only declared precondition — checks
  *membership*, never count or duplication, so this passes launch. Combined with the previous
  finding (no `expensive` gate on `compare_solvents`), one unprivileged tool call queues
  42 × 300 = 12,600 CREST searches, each documented as "minutes of saturated CPU", on a shared
  worker queue. The tool returns a job id after 20 s (`inline_wait_seconds`) and the job then runs
  for as long as it runs. `xtb_scan_max_points` (24) *is* enforced — but inside
  `compose.scan_profile`, i.e. after the workflow has been started and the payload accepted, so a
  100,000-element `values` array is a Temporal payload the boundary happily takes.
- **Evidence**: `scratchpad/probe_caps.py`:

  ```
  ReactionJobSpec accepted species: 300 + 300 level: thorough -> precondition passed
  SolventScreenJobSpec accepted solvents: 42 x species: 300 -> precondition passed;
      12600 CREST searches implied
  ScanJobSpec accepted values: 100000 (compose caps at xtb_scan_max_points only AFTER
      the workflow starts)
  ```
- **Fix**: put the bounds on the specs, where they are checked before anything is queued —
  `reactants`/`products`: `Field(min_length=1, max_length=settings.calc_max_species)`;
  `solvents`: `Field(min_length=1, max_length=len(ALPB_SOLVENTS))` (a screen cannot meaningfully
  exceed the parameterised set, and duplicates should be rejected);
  `values`: `Field(min_length=2, max_length=...)` mirroring `xtb_scan_max_points` so the existing
  ceiling is enforced at the boundary rather than mid-job. Keep the config knobs in
  `core/config/calculators.py` per the repo's "config, never magic numbers" rule.

---

## `QmJobSpec.method` / `basis_set` are unvalidated model-authored strings that reach the HPC pipeline

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/qm/specs.py:38-40` (`Field(min_length=1)` and nothing
  else) → `src/chemclaw/connectors/qm/hpc/nextflow.py:98-106` (`params: {smiles, method,
  basis_set}` POSTed to `/workflow/launch`) → `src/chemclaw/connectors/qm/knowledge.py:99-104`
  (interpolated into the note body).
- **Trigger**: the LLM authors `compute_dft_energy(params={molecule_smiles: "CCO", method: "<any
  string>", basis_set: "<any string>"})`. `prepare_job_launch` validates against the params model
  (which imposes only `min_length=1`), the job declares no `precondition`, and `prepare_input`
  canonicalizes only `molecule_smiles`.
- **Consequence**: two sinks, neither guarded here.
  1. The strings land in a Nextflow launch's `params` map. Nextflow pipelines routinely interpolate
     `params.*` into `process` `script:` blocks, which are shell. This repository cannot see that
     pipeline, so the exploitability depends on it — but the boundary that *can* constrain the value
     is this one, and it constrains nothing: no allowlist, no length cap, no character class, no
     newline rejection.
  2. The same strings are written verbatim into a `job-result` note body that the PR-gate commits
     to the knowledge-graph repo. A `method` containing `[[some-note]]` mints a wikilink; one
     containing newlines fabricates additional bullet lines in a record a human is asked to approve.
  `specs.py` documents the decision as "Kept as free strings: the valid set is a chemistry judgment
  the `qm-job-submission` skill holds, not something to freeze as an enum here" — but a skill is a
  prompt (`qm/skills/qm-job-submission/SKILL.md` offers a three-row table of suggestions), and a
  prompt is not a validator. Nothing anywhere in `src/` constrains these two fields; grep for
  `basis_set` returns only producers and consumers.
- **Evidence**: `src/chemclaw/connectors/qm/specs.py:38-40` in full —

  ```python
  molecule_smiles: str = Field(min_length=1, description="The molecule as a SMILES string.")
  method: str = Field(min_length=1, description='QM method / level of theory, e.g. "B3LYP".')
  basis_set: str = Field(min_length=1, description='Basis set, e.g. "def2-SVP".')
  ```

  `molecule_smiles` is separately hardened (`prepare_input` → `require_canonical_smiles`); the other
  two are not touched by anything on the path to `launch_run`.
- **Fix**: constrain the two fields at the model, where every launcher and the validator see it:
  `pattern=r"^[A-Za-z0-9()\-+*/,._]{1,64}$"` at minimum (that admits `B3LYP`, `wB97X-D`,
  `def2-TZVP`, `cc-pVDZ` and excludes whitespace, quotes, `$`, backticks and newlines). If an
  allowlist is genuinely a chemistry judgment, express it as a `precondition:` on the
  `compute_dft_energy` job — the manifest already has that hook and `prepare_job_launch` already
  runs it pre-launch on both launch paths.

---

## Unbounded, uncancellable work in the `bo` tools on an unauthenticated surface

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/bo/server/tools.py:438-581`
  (`suggest_next_experiment`: `count: int = 1` with no bound, `observations` unbounded,
  `await asyncio.to_thread(propose_candidates, ...)` at :546) and :782-850
  (`predict_outcome`: `points` and `observations` unbounded,
  `await asyncio.to_thread(interrogate_surrogate, ...)` at :847).
- **Trigger**: `suggest_next_experiment(problem=<2 continuous params>, observations=<6 runs>,
  count=1000)` — repeated a few dozen times.
- **Consequence**: the acquisition optimisation is superlinear in `count` and cubic in the
  observation count, and it runs in `asyncio.to_thread`, i.e. on the default
  `ThreadPoolExecutor` (`min(32, cpu+4)` workers). `asyncio.to_thread` cannot be cancelled: when
  the caller's 120 s `request_timeout` (`bo/connector.yaml:20`) expires, or the HTTP connection
  simply drops, the thread keeps computing to completion. So a handful of requests permanently
  occupies every executor slot in the process and every other `to_thread` in that pod
  (`generate_screening_design`, `campaign_progress`) queues behind them. Reachable without any
  credential — see the first finding — so this needs neither a session nor an actor.
- **Evidence**: `scratchpad/probe_count.py`, measured here on a 2-parameter / 6-observation
  problem, i.e. the smallest realistic input:

  ```
  count=  1 -> 1 candidates in 10.7s
  count=  2 -> 2 candidates in 8.9s
  count=  4 -> 4 candidates in 8.9s
  ```

  ~9 s per batch at these sizes; `count` is an unbounded `int`, so a single call can be made to
  run for hours past the 120 s the client will wait. (Contrast the sibling bundles, which *do*
  clamp their model-supplied bounds: `science/fingerprints/store.py:472-475` clamps `top_k` to
  `[1, fingerprint_max_top_k]` and `threshold` to `[0, 1]` "at the single chokepoint both entry
  points share".)
- **Fix**: clamp at the tool boundary the way `find_matches` already does —
  `count = min(max(count, 1), settings.bo_max_batch)`, and reject an `observations`/`points` list
  longer than a configured ceiling with a message the model can act on. Both settings belong in
  `core/config`, next to `bo_max_rounds`.

---

## `calculator_outliers(matching=…)` compiles a caller-supplied SMARTS with no length bound

- **Severity**: low
- **Location**: `src/chemclaw/connectors/calc/server/tools.py:609-636` (`_only_matching`).
- **Trigger**: `calculator_outliers("pka", matching="<long adversarial recursive SMARTS>")` in a
  deployment with `calibration_enabled=true` and at least one reconciled measurement.
- **Consequence**: `_only_matching` calls `substructure_pattern(query)` directly.
  `core/chem.py:323-344` rejects only an unparseable or zero-atom pattern — it applies no length
  limit. The `molfp` sibling *does*
  (`science/fingerprints/molfp/search.py`: `substructure_query_max_length`, refused before
  matching), so the same class of input is bounded on one entry point and unbounded on the other.
  The `asyncio.wait_for(..., substructure_match_timeout_seconds)` here releases the event loop and
  the caller but, as the `molfp` docstring already states for the identical construct, "cannot kill
  the RDKit thread, which holds one CPU until the pattern completes". `calculator_outliers` is
  `read_only` in the manifest, so it is deliberately reachable under an unapproved plan.
  Severity is low only because the scan is over the reconciled ledger, which is empty by default
  (`calibration_enabled` defaults to False) — the exposure grows with the ledger.
- **Evidence**: `calc/server/tools.py:617` calls `substructure_pattern(query)` with no preceding
  length check; `core/chem.py:339-344` is the whole validation; the guarded caller
  (`science/fingerprints/molfp/search.py`) checks `len(query) > settings.substructure_query_max_length`
  *before* compiling.
- **Fix**: move the length check into `core/chem.substructure_pattern` so both callers inherit it —
  that function's own docstring already gives the reason ("a second copy of 'SMARTS or SMILES, and
  reject the empty one' is exactly the kind of chemistry rule that drifts apart unnoticed"), and the
  length bound is the third rule that belongs with the other two.

---

## Launcher-supplied `workflowId` is interpolated into the artifact-store URL path

- **Severity**: low
- **Location**: `src/chemclaw/connectors/qm/hpc/nextflow.py:153`
  (`url = f"{settings.hpc_artifact_store_url}/{handle.scheduler_job_id}/qm_output.txt"`), value
  from `nextflow.py:117` (`run_id = response.json().get("workflowId")`), model
  `qm/specs.py:86-93` (`scheduler_job_id: str = Field(min_length=1)`).
- **Trigger**: the launcher (or anything that can answer for it — the response is not signed and
  the poll/launch client follows the operator's `hpc_api_base_url`) returns
  `{"workflowId": "../../../admin/secrets"}`.
- **Consequence**: httpx normalises the dot segments, so the GET issued is
  `http://artifacts.internal/admin/secrets/qm_output.txt` — an arbitrary path on the artifact host,
  fetched **with the artifact credential attached** (`_artifact_headers`, which falls back to the
  Seqera launcher token on a same-origin store). The response body is then returned as
  `raw_output` and regex-parsed; a non-matching body raises with the raw text in the message
  (`activities.py:155`, `ValueError(f"unparseable QM output: {raw_output!r}")`), and `ValueError`
  is the one family `_sanitize_tool_errors` passes to the model verbatim
  (`connectors/server.py:372-380`) — so the fetched content reaches the conversation. The same
  string is also interpolated into the poll path (`nextflow.py:132`) and into the mock energy
  derivation (`activities.py:100`, `int(handle.scheduler_job_id[-4:], 16)`, which raises on a
  non-hex tail).
- **Evidence**:

  ```
  constructed: http://artifacts.internal/runs/../../../admin/secrets/qm_output.txt
  httpx sends: http://artifacts.internal/admin/secrets/qm_output.txt
  request path: b'/admin/secrets/qm_output.txt'
  ```
- **Fix**: constrain the handle at the model, which is the one place both the poll and the fetch
  read it from: `scheduler_job_id: str = Field(min_length=1, max_length=128,
  pattern=r"^[A-Za-z0-9_-]+$")`. That rejects `/`, `.`, `%` and `@` and costs nothing — Tower run
  ids and the mock's `mock-<hex>` both satisfy it.

---

## What was checked and found sound

Stated so the absence of a finding is evidence rather than silence.

- **SQL injection**: every store the bundles reach parameterises. `science/calc/postgres_store.py`
  uses one static `_FIND` statement with a params tuple (its own comment: "one statement serves
  every combination — the alternative is assembling SQL from whichever filters"), and
  `postgres_artifacts.py` likewise. `find_calculations`'s model-supplied `limit` is clamped to
  `[1, calc_find_max_results]`; `calculator_outliers`'s to `[1, calc_outliers_max_results]`;
  `fetch_artifact`'s `max_chars` to `[1, calc_artifact_max_chars]` with the negative-slice case
  handled.
- **The `state_changing` / `read_only` partition**: I traced every one of the 15 `calc` and 5 `bo`
  tool bodies. The classification is correct in both bundles, including the two `bo` tools whose
  classification was corrected once already (`suggest_next_experiment` and `predict_outcome` do
  reach `featurize_problem` → `cached_remote`; `generate_screening_design` and `campaign_progress`
  genuinely do not), and including `calculator_trust`/`calculator_outliers`, which reach the server
  only for `calculation_key` and compute nothing.
- **Identity headers as authorization**: no bundle gates on `X-Chemclaw-Actor`. The one consumer,
  `bo/server/tools.py::_recorded_provenance`, stamps `unverified:` and uses the value for
  attribution only.
- **Credential handling in outbound clients**: `nextflow._artifact_headers` correctly refuses to
  send the launcher's bearer to a foreign origin; `registry.connector_http_client` sets
  `follow_redirects=False` and `identity.turn_identity_hook` *strips* (not merely skips) the
  `X-Chemclaw-*` headers on a cross-origin hop. `calc/remote.py::_token` reads the bearer per
  request from the env rather than caching it. No credential is written to any manifest.
- **`molfp`/`rxnfp` input hardening**: query length, corpus scan size, hit count and match wall-time
  are all bounded, and truncation is reported in the payload rather than only in the log.
- **`fetch_artifact` reference parsing**: `rpartition("#")` plus a lookup restricted to the
  artifacts actually linked to that calculation key — no filesystem path is constructed, and binary
  content is refused by decode rather than by a media-type table.
