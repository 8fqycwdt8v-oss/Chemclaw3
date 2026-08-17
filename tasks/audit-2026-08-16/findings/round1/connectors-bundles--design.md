# Connector bundles (`calc`, `bo`, `qm`, `chem`, `safety`, `molfp`, `rxnfp`) — design and simplification

Lens: structure that costs more than it buys. Nine findings, each reproduced. A short
"checked and clean" note is at the end, because two of the things this lens usually finds
(dead public symbols, upward layering violations at package granularity) are genuinely absent here.

---

## One private-constant import drags LangGraph and layer 1 into every `calc` and `bo` process

- **Severity**: high
- **Location**: `src/chemclaw/connectors/calc/remote.py:42` — `from chemclaw.connectors.registry import _READ_TIMEOUT_GRACE_SECONDS`
- **Trigger**: importing any module that reaches `connectors/calc/remote.py`. That is the whole
  `calc` bundle (`server/tools.py`, `compose.py`, `activities.py` via `compose`) **and** the `bo`
  bundle (`connectors/bo/calculators.py:16`). So: `chemclaw.connectors.calc.server.app`,
  `chemclaw.connectors.calc.worker`, `chemclaw.connectors.bo.server.app`,
  `chemclaw.connectors.bo.worker` — four of the deployed processes.
- **Consequence**: the sole use of that import is a `5.0` float. Reaching it imports
  `chemclaw.connectors.registry`, which imports `connectors/identity.py`, which imports
  `chemclaw.agent.turn_flags` — and `chemclaw.agent` pulls `langgraph`, `langchain_core`,
  `langchain_mcp_adapters` (~110 modules) plus `chemclaw.agent.authz` and
  `chemclaw.agent.session_events`. The conversation-orchestration layer and its whole framework are
  loaded into the connector pods, which is the exact inversion of what a bundle boundary is for:
  D-118's premise is that a bundle's worker carries its own closure and the chat service carries
  none; here the bundle carries the chat service's. `tests/test_connector_isolation.py` cannot see
  it — it asserts only that `tblite`/`bofire`/`botorch`/`torch` stay out of the *agent's* process,
  and never asks the reverse question. `tests/test_layering.py` cannot see it either: it sanctions
  `chemclaw.connectors -> chemclaw.agent` at *package* granularity (line 321, "connector jobs and
  identity plumbing authorize against agent's authz/identity context"), and this edge is neither
  connector jobs nor identity plumbing.
- **Evidence**:

  Import fan-out attributable to that one line (`/tmp/chain2.py`):

  ```
  mcp                                        new= 623 agent_loaded=False
  mcp.client.streamable_http                 new=   0 agent_loaded=False
  chemclaw.core.config                       new= 340 agent_loaded=False
  chemclaw.core.errors                       new=   1 agent_loaded=False
  chemclaw.core.ids                          new=   1 agent_loaded=False
  chemclaw.science.calc.store                new=  27 agent_loaded=False
  ---- now the registry line ----
  chemclaw.connectors.registry               new=1065 agent_loaded=True
        roots=['chemclaw', 'langchain_core', 'langchain_mcp_adapters', 'langgraph']
  ```

  What each deployed process actually loads today:

  ```
  chemclaw.connectors.calc.server.app: langgraph=True  agent=True mods=2318 t=2.00s
  chemclaw.connectors.calc.worker:     langgraph=True  agent=True mods=2298 t=1.95s
  chemclaw.connectors.bo.worker:       langgraph=True  agent=True mods=6582 t=6.91s
  chemclaw.connectors.bo.server.app:   langgraph=True  agent=True mods=6608 t=7.02s
  chemclaw.connectors.qm.worker:       langgraph=False agent=True mods=1829 t=1.25s
  chemclaw.connectors.molfp.server.app:langgraph=False agent=True mods=1304 t=1.12s
  ```

  Causality proven by stubbing only that symbol (`/tmp/proof.py`) — `sys.modules` pre-seeded with a
  `chemclaw.connectors.registry` module carrying just `_READ_TIMEOUT_GRACE_SECONDS = 5.0`:

  ```
  calc server app WITHOUT the registry import: langgraph=False agent=True mods=1473 t=1.34s
  ```

  2318 → 1473 modules, 2.00 s → 1.34 s cold import, and `langgraph`/`langchain_*` gone entirely.
  (`chemclaw.agent` still arrives, via `connectors/server.py` — that is the sanctioned edge.)
- **Fix**: move the constant, and the policy it encodes, to `chemclaw.core.http`. That module's own
  docstring already states the rule for exactly this case ("It lives here because `connectors -> api`
  is an edge the layering policy explicitly removed"), and both users are "how do we talk to somebody
  else's HTTP endpoint". Concretely: define `READ_TIMEOUT_GRACE_SECONDS` in `core/http.py`, import it
  from there in both `connectors/registry.py:89` and `connectors/calc/remote.py:127`. Behaviour-preserving
  — the value and both call sites are unchanged. Then extend `tests/test_connector_isolation.py` with the
  reverse assertion (`langgraph` and `langchain_core` must not be in `sys.modules` after importing a
  bundle's server app or worker), or the edge grows back.

---

## A calc-server outage reaches the model as "an internal error occurred"

- **Severity**: high
- **Location**: `src/chemclaw/connectors/calc/remote.py:55-72` (`CalcServerError`) against
  `src/chemclaw/connectors/server.py:373` (`_sanitize_tool_errors`)
- **Trigger**: any of the 11 `calc` tools that cross the wire, called while the physics server is
  unreachable. Reproduced with `CHEMCLAW_CALC_SERVER_URL=http://127.0.0.1:59999/mcp`.
- **Consequence**: `CalcServerError` derives from `SubsystemUnavailableError`, which
  `core/errors.py:37` deliberately places **outside** the `ChemclawError`/`ValueError` hierarchy.
  `_sanitize_tool_errors` forwards a tool exception verbatim only when its `__cause__`
  `isinstance(..., ValueError)`; everything else is replaced with a generic notice. So the carefully
  written outage message — whose own docstring says "The message is written for the **chemist**,
  because `agent/tool_authz.py` hands it to the model verbatim" — never leaves the connector pod.
  The two-class design (`CalcServerError` vs `CalcToolError`) survives on the *durable* path, where
  Temporal matches by class name, and is silently discarded on the *tool* path, which is the one a
  chemist is sitting in front of. Note the direction: `CalcToolError` **is** a `ValueError`, so the
  "you asked for something impossible" half passes through intact and the "we are down, retry later"
  half is the one that is lost. Affects `compute_xtb_energy`, `predict_solubility`, `predict_pka`,
  `compute_electronic_properties`, `predict_site_reactivity`, `optimize_geometry`,
  `compute_thermochemistry`, `predict_developability_profile`, `predict_logd`, `calculator_trust`,
  `calculator_outliers` — 11 of the bundle's 15 tools.
- **Evidence** (`/tmp/calcerr2.py`, run against the served tool manager after
  `import chemclaw.connectors.calc.server.app` has applied the sanitizer):

  ```
  CalcServerError is a ValueError? False
  CalcToolError   is a ValueError? True
  compute_xtb_energy     -> Error executing tool compute_xtb_energy: an internal error occurred
  predict_pka            -> Error executing tool predict_pka: an internal error occurred
  calculator_trust       -> Error executing tool calculator_trust: an internal error occurred
  ```

  For contrast, the same call one layer down raises:
  `CalcServerError: the calculation service is not answering, so no calculation was run. This is an
  outage rather than a problem with what was asked; the same request will work once it is back.`
- **Fix**: `_sanitize_tool_errors` should forward `SubsystemUnavailableError` as well as
  `ValueError` — `core/errors.py`'s module docstring already names both as "cross-cutting error
  contracts" and says the middleware that shows either to the model must import both. One line:
  `if isinstance(exc.__cause__, ValueError | SubsystemUnavailableError): raise`. Behaviour-preserving
  for every other error class. The docstring in `remote.py:55-72` should then stop citing
  `agent/tool_authz.py` as the reader on this path, because it is not.

---

## Two identity functions for one QM job, disagreeing about the backend

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/qm/specs.py:60` (`qm_job_key`) and
  `src/chemclaw/connectors/qm/cache.py:88` (`calculation_key`) / `:69` (`calc_version`)
- **Trigger**: a deployment that runs `hpc_launch_interface=mock` with `hpc_pipeline_version` set
  (the shipped chart pins `CHEMCLAW_HPC_PIPELINE_VERSION: "1.0.0"`, so a staging release that flips
  only the interface to `mock` is the ordinary case), submits `compute_dft_energy` for a molecule,
  then switches to `nextflow` at the same pipeline version and submits it again.
- **Consequence**: `calc_version()` folds the backend into the calculation-store key — and
  `cache.py`'s own docstring argues at length that it must, because "a deployment that ran the mock
  and then pointed at a real cluster served that fabricated number as a cache hit". `qm_job_key`
  does not. It is the identity behind the **note id** (`note_from_qm_result` → `job-<qm_job_key>`)
  and the launcher `Idempotency-Key`. So the store correctly treats the mock energy and the DFT
  energy as two calculations, while the knowledge graph has one id for both. The mock's fabricated
  energy (`activities.poll_hpc_status:100`, derived from the hex digits of a job id) and the real
  DFT energy compete for `knowledge/job-result/job-<hash>.md`. Since the PR-gate means a human
  decides, the already-merged mock note is the one that stays if the re-proposal is not merged, and
  there is no second id under which the real answer can land. The invariant `cache.py` states
  ("naming the backend is what makes the two families of rows unable to reach each other") is
  enforced in one of the two identity derivations over the same three inputs.
- **Evidence** (`/tmp/qmkey.py`):

  ```
  mock       interface=mock      qm_job_key=26c289481818da3a  calc_key=dft@mock-1.0.0:a7d334ebee616d78:2f253b746a0fcd8d
  nextflow   interface=nextflow  qm_job_key=26c289481818da3a  calc_key=dft@nextflow-1.0.0:a7d334ebee616d78:2f253b746a0fcd8d
  note id: job-26c289481818da3a
  ```
- **Fix**: there should be one derivation. Make `qm_job_key` the flat rendering of the same identity
  `calculation_key` builds — i.e. `stable_hash` over `{smiles, method, basis_set, calc_version()}` —
  so the backend enters both, or (better) delete `qm_job_key` and derive the note id from
  `calculation_key(spec).as_str()`'s hash components, since `calculation_key` already canonicalizes
  the SMILES identically. Not behaviour-preserving: it changes note ids and launcher idempotency keys
  for every existing job, so it needs the same treatment as a cache-epoch bump. The minimal
  behaviour-preserving version is to add `hpc_launch_interface` to `qm_job_key`'s payload alongside
  the pipeline version, which changes ids only for deployments that were already ambiguous.

---

## `ExperimentSuggestion` ships the lead objective's scale twice

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/bo/server/tools.py:144-145` (`scale`, `scales`),
  produced at `:570-571`, consumed at `:169` and `:184-195`
- **Trigger**: every successful `suggest_next_experiment` call.
- **Consequence**: `scale` is byte-identical to `scales[0]` in the payload, so the model reads the
  same object twice. Worse, nothing enforces the relationship: `summary` reads the *spread* off
  `self.scale` and the objective *names, count and cold-start check* off `self.scales`, so a
  suggestion constructed with an inconsistent pair (the tests already construct the two independently
  — `tests/test_bo_predict.py:508-509`) produces one sentence naming objective A as lead and reading
  objective B's spread. Two fields for one fact, with the invariant living only in the single
  producer.
- **Evidence** (`/tmp/boscale.py`, two-objective suggestion):

  ```
  bytes with scale: 1183
  bytes without   : 1057
  scale == scales[0]: True
  ```

  and the dump shows the `yield` block verbatim under both `"scale"` and `"scales"[0]`.
- **Fix**: delete the `scale` field and make it a plain read-through
  (`return self.scales[0] if self.scales else None`) used by `summary`, dropping the duplicate from
  the wire. One dependency to update: the bundled skill names the field
  (`src/chemclaw/connectors/bo/skills/experiment-design/SKILL.md:139`, "a `scale` giving what the
  objective actually…") — change it to `scales[0]`. Behaviour-preserving for `summary` and for any
  reader of `scales`; it removes one key from the response.

---

## The same five-line validation preamble in three `bo` tools

- **Severity**: low
- **Location**: `src/chemclaw/connectors/bo/server/tools.py:530-536`, `:633-637`, `:835-840`;
  plus the two-line featurize pair at `:541-544` and `:845-846`
- **Trigger**: adding a sixth tool, or changing what "these observations describe this problem"
  means.
- **Consequence**: three verbatim copies of

  ```python
  problem = OptimizationProblem.model_validate(problem)
  history = [Observation.model_validate(item) for item in _as_list(observations, "observations")]
  require_names_do_not_clash(problem)
  _require_observed_params_match(problem, history)
  require_observations_cover_objectives(problem, history)
  ```

  and two of the featurize-then-check pair. They agree today; the cost is that a fourth
  observations-taking tool silently omitting one line is a validation gap with no signal. This file
  invokes the repo's Rule of Three by name for `_as_list` ("One helper, one sentence, three call
  sites, per this repo's Rule of Three") and then does not apply it to the larger clone directly
  above it.
- **Evidence**: `grep -n` over the file, reproduced above; the three sites are byte-identical apart
  from `predict_outcome` parsing `points` between lines 835 and 837.
- **Fix**: `async def _validated(problem, observations) -> tuple[OptimizationProblem, list[Observation]]`
  and `async def _featurized(problem) -> FeaturizedProblem` (featurize + `require_descriptors_distinguish_categories`),
  called from the three and the two sites respectively. Behaviour-preserving — same calls, same order.

---

## `calc/worker.py` claims to load `tblite`; nothing in the tree imports it, and it is still a declared dependency

- **Severity**: low
- **Location**: `src/chemclaw/connectors/calc/worker.py:5-7`; `pyproject.toml:152`
- **Trigger**: reading the module, or building any image.
- **Consequence**: the docstring says "`tblite` and the `calc.*` closure are loaded in this process
  and nowhere else, because this import happens here and core's workers never make it." Both halves
  are false since the physics moved out. No module under `src/` imports `tblite`; the layering test
  explicitly forbids one from ever doing so again
  (`tests/test_third_party_layering.py:141-149`: "**No package may import it any more**… an `import
  tblite` reappearing anywhere in this tree is a copy of a capability that lives elsewhere"). And
  `chemclaw.science.calc.*` is emphatically *not* loaded only here — `postgres_store` alone is
  imported by five bundle modules including `bo` and `qm`. Meanwhile `tblite>=0.7.0` is still a
  declared runtime dependency, so 17.5 MB of compiled QM library ships in the image every one of the
  chart's processes runs from, with zero importers. `calc/specs.py` and `calc/results.py` had their
  equivalent claims updated in the same move; this one was missed.
- **Evidence**:

  ```
  $ grep -rn "^import tblite\|^from tblite" --include="*.py" src/     # (no matches)
  $ du -sh .venv/lib/python3.11/site-packages/tblite*
  7.6M  .../tblite
  32K   .../tblite-0.7.0.dist-info
  9.9M  .../tblite.libs
  ```
- **Fix**: rewrite the docstring to say what the process actually holds (the D-011 cache, the
  calibration ledger, the compositions, and the MCP client to the physics server), and drop
  `tblite` from `pyproject.toml`'s dependencies — nothing imports it, and the layering test already
  guarantees nothing will. Behaviour-preserving; `make lint type test` is the check.

---

## Dead stdio `main()` duplicated in the `molfp` and `rxnfp` tool modules

- **Severity**: low
- **Location**: `src/chemclaw/connectors/molfp/server/tools.py:77-83` and
  `src/chemclaw/connectors/rxnfp/server/tools.py:55-61`
- **Trigger**: none — that is the finding.
- **Consequence**: two copies of

  ```python
  def main() -> None:
      """Run the server over stdio (the default MCP transport)."""
      server.run()

  if __name__ == "__main__":
      main()
  ```

  with no caller anywhere: no `[project.scripts]` entry (`pyproject.toml:172-174` declares only
  `chemclaw`), no Makefile target, no manifest declaring `transport: stdio` (both bundles declare
  `transport: http`), and no reference in `src/`, `tests/` or `docs/guides`. The two other in-repo
  servers (`calc`, `bo`) have no such function, so it is not a bundle convention either. The repo's
  own rule is "only code that is actually used. Delete dead params, empty interfaces, and 'for later'
  stubs on sight."
- **Evidence**: `grep -rn "def main" src/chemclaw/connectors/` returns four hits — the two above,
  plus `worker.py:65` and `server_entry.py:41`, which are the real entry points and are both called.
  `grep -rn "stdio"` over `src/`, `Makefile`, `README.md` and `docs/guides` finds no invocation of
  either module. The public-symbol scan below found no other unreferenced symbol in the slice.
- **Fix**: delete both blocks and the two docstring sentences that advertise them
  (`molfp/server/tools.py:10-12`, `rxnfp/server/tools.py:5-6`). Behaviour-preserving.

---

## `nextflow._client` is an `async def` that never awaits

- **Severity**: low
- **Location**: `src/chemclaw/connectors/qm/hpc/nextflow.py:80-87`, called at `:113` and `:131`
- **Trigger**: reading either call site.
- **Consequence**: the function body is a single `httpx.AsyncClient(...)` construction with no
  `await` in it, so the coroutine exists only to be immediately awaited, and both call sites read
  `async with await _client(transport)` — a double-keyword form that makes a reader look for the
  asynchronous step that is not there. It also allocates a coroutine object per launcher call for
  nothing.
- **Evidence**: the whole body, lines 82-87, is one `return httpx.AsyncClient(...)`. Both call sites
  are `async with await _client(transport) as client:`.
- **Fix**: `def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:` and
  `async with _client(transport) as client:` at both sites. Behaviour-preserving.

---

## `CHEMCLAW_CALC_SERVER_URL` is declared twice in the same `config:` mapping

- **Severity**: low
- **Location**: `deploy/helm/chemclaw/values.yaml:335` and `:380` (adjacent to this slice — it is
  the `calc` bundle's remote address, but the file itself belongs to the deploy reviewer)
- **Trigger**: `helm template` / any YAML load of the chart values.
- **Consequence**: a duplicate key in one mapping. Both loaders in play (Helm's `sigs.k8s.io/yaml`
  and PyYAML's `SafeLoader`) take the last silently. The two values happen to be identical today, so
  nothing is wrong yet — but each carries its own multi-line justification comment, one of them five
  lines long and written as if it were the only declaration, so the next operator who edits line 335
  will change nothing and have no way to notice.
- **Evidence**:

  ```
  $ python - <<'…'   # SafeLoader with a duplicate-detecting mapping constructor
  DUPLICATE KEYS in a mapping: ['CHEMCLAW_CALC_SERVER_URL']
  effective value: http://chemclaw3-mcp-calc:8860/mcp
  ```
- **Fix**: delete the earlier declaration (line 330-335, whose comment duplicates the later one's
  point) and keep the annotated one at line 376-380. Behaviour-preserving — the rendered ConfigMap
  is unchanged.

---

## Checked and found clean

Recorded because a negative result through this lens is worth as much as a finding:

- **Dead public symbols.** All 104 public functions/classes across the seven bundles were extracted
  by AST and cross-referenced against `src/`, `data/`, `docs/guides`, `tests/` and `examples/`
  (`/tmp/deadscan3.py`). Every one has a live non-test reference. Twelve are referenced only inside
  their own module — all of them return types (`CalculationRecord`, `OutlierReport`,
  `StoredArtifact`, `ArtifactContent`, `SurrogateAnswer`, `ObjectiveScale`) or single-module helpers
  (`check_balance`, `no_progress`, `radical_multiplicity`, `remote_compute`, `calc_session`) — none
  dead. Dynamic registration was checked before concluding: `@durable_activity`/`@durable_workflow`
  registration by import side effect (the three `worker.py` modules), `@server.tool()` MCP
  registration, `connector.yaml`'s `params_model`/`precondition`/`workflow` string references, and
  the `XtbJobSpec` discriminated union all resolve to symbols in use.
- **Upward layering.** No bundle module imports `chemclaw.api`, `chemclaw.service` or
  `chemclaw.cli`; the tally of every `from chemclaw.*` import across the seven bundles shows only
  `core`, `science`, `durable`, `kg` and sibling connector modules. The one upward edge that does
  exist is the accidental `agent`/`langgraph` pull documented as finding 1.
- **Module-global state.** `molfp/server/tools.py:32` and `rxnfp/server/tools.py:22` hold a
  module-level `_store`, and `calc/server/tools.py:65` / `bo/server/tools.py:74` a module-level
  `FastMCP`. All four are per-process singletons a server needs; the store constructors are lazy
  handles, not connections. Not a finding.
- **Hardcoded config.** Every threshold, timeout and limit reachable in the slice reads from
  `settings` (`xtb_scan_max_points`, `calc_screen_max_parallel`, `crest_effort`,
  `hpc_poll_max_consecutive_errors`, `note_excerpt_chars`, …). The only literals are structural
  (`_COORDINATES`, `_STATE_BY_LAUNCHER_STATUS`, the launcher's REST paths, display rounding).
- **`chem` and `safety`.** Manifest-and-skill only, with no Python beyond a docstring `__init__`.
  Their comments claim the manifests are retained because four validators build the known-tool set
  from `connectors_dirs`; that is consistent with what `registry.enabled()` does, and the tool lists
  they declare are the only copies in this repository.
