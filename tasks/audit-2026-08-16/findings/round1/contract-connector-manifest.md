# Round 1 — the connector contract (Chemclaw3 ↔ Chemclaw3-mcp)

Scope: the connector manifests this repo ships (`src/chemclaw/connectors/*/connector.yaml`), the
loader/registry/transport/jobs that read them, and the tool surface `/workspace/chemclaw3-mcp`
actually serves. All findings below were reproduced by running both sides.

**What is genuinely consistent, measured not assumed** — so the findings are read against a
boundary that is mostly sound:

- Tool *names* agree on every shared bundle, checked live over the wire with bearer auth against
  running servers: `chem` declared `[green_metrics, render_structure, resolve_compound,
  stoichiometry_table]` == served; `safety` declared `[ich_impurity_limit,
  screen_genotoxic_alerts, screen_hazards]` == served. `served-not-declared = []` and
  `declared-not-served = []` on both.
- Auth mode and the credential's *name* agree on all five fleet servers:
  `CHEMCLAW_{CHEM,SAFETY,CALC,PROPS,RXNPREDICT}_TOKEN` in the manifest == the `token_env` passed to
  `connector_app` in each `app.py`. Core sends it as an `httpx.Auth` (`connectors/identity.py:133`),
  so it is present on `initialize()` too; a live `chem`/`safety` handshake + `list_tools` succeeded.
- The `calc` payload contract is exact. I ran every tool core calls against the real server and fed
  each payload to core's parser: 13/13 validate, and the only fields core drops are the deliberate
  `calc_key`/`calc_version`/`structure_id` extras. `structure_id` agrees **byte for byte** in both
  directions (`st_739a222f45be0c3a` for `CCO`; `st_e868cd6fe533107f` and `st_da4c850d40ee104f` for
  two computed geometries). Core-composed `predict_logd` reproduces the server's own to the last
  digit (`log_d = 1.3921342808501795`). `CALCULATION_EPOCH` is `"1"` on both sides.
- `make connector-validate`, `skill-validate`, `template-validate`, `prose-validate`,
  `datasource-validate` all pass on the shipped configuration.

---

## 1. Every calc-server failure reaches its caller as a bare `ExceptionGroup`, so `CalcToolError`/`CalcServerError` are never delivered

**Severity** — Critical

**Location**
- Core: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/remote.py:123-144` (`calc_session` yields
  *inside* `async with streamablehttp_client(...)`), with the classifications it promises at
  `remote.py:195-207`, and the three entry points `remote_call` (`:276`), `remote_version` (`:294`),
  `cached_remote` (`:317`).
- Core: `/home/user/Chemclaw3/src/chemclaw/durable/publish.py:32-140` — `_BAD_DATA_TYPES` /
  `BAD_DATA_RETRY`, matched by exception **class name**.
- Core: `/home/user/Chemclaw3/src/chemclaw/agent/tool_authz.py:327` vs `:333` — the domain-error arm
  vs the "unhandled" arm.
- Server: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/tools.py` (any tool),
  reached over `streamable_http` with `mcp==1.29.0` / `anyio==4.14.2`.

**Trigger** — Any failed call to the calculation server. Reproduced against a live
`uvicorn chemclaw_mcp_calc.app:app` on `127.0.0.1:8860`:

```
(a) tool refusal, invalid SMILES   -> ExceptionGroup   in _BAD_DATA_TYPES=False -> temporal RETRIES
(c) METHOD_NOT_FOUND, unknown tool -> ExceptionGroup   in _BAD_DATA_TYPES=False -> temporal RETRIES
(d) unsupported solvent            -> ExceptionGroup   in _BAD_DATA_TYPES=False -> temporal RETRIES
cached_remote(predict_pka, bad SMILES)  -> raised ExceptionGroup (innermost CalcToolError)
remote_version(predict_pka, bad SMILES) -> raised ExceptionGroup (innermost CalcToolError)
```

`_call` *does* raise the right class — it is then swallowed twice by anyio task groups on the way
out of `ClientSession.__aexit__` and `streamable_http_client.__aexit__`, and re-raised as a nested
`ExceptionGroup`. `calc_session`'s own guard cannot help: past the `yield` it sets `connected =
True` and re-raises whatever arrives (`remote.py:139-146`).

**Consequence** — Two controls that this module exists to provide are both inert:

1. **Durable jobs retry bad data.** Temporal matches `non_retryable_error_types` by class name
   (`publish.py:28`). `ExceptionGroup` is not in `_BAD_DATA_TYPES`, so every calc job
   (`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`,
   `compute_interaction_energy`) burns its full `activity_max_attempts` on an unbalanced equation,
   an out-of-range atom index or an unparameterised solvent — the exact inversion `CalcToolError`'s
   docstring says it was split out to prevent.
2. **The chemist gets a nonsense message.** `tool_authz.py:327` hands `ChemclawError` /
   `SubsystemUnavailableError` messages to the model verbatim; an `ExceptionGroup` is neither, so it
   falls to `:333` `unexpected_error_result()`. The server's actual sentence — "the 'crest' binary
   is not installed", "compute_xtb_energy failed: invalid SMILES: 'not-a-smiles'" — is replaced by a
   generic crash notice, and the top-level `str(exc)` is `unhandled errors in a TaskGroup
   (1 sub-exception)`.

**Evidence** — Full traceback captured; the innermost frame is
`remote.py:207 raise CalcToolError(...)` wrapped by
`anyio/_backends/_asyncio.py:815 raise BaseExceptionGroup(...)` twice. The reason the suite is green
is that `tests/test_calc_remote.py:234` fakes `streamablehttp_client` with a `_Wire` whose
`__aexit__` just `return False` — no task group, so no wrapping. Every assertion in
`test_a_failure_inside_the_session_body_is_not_relabelled_as_an_outage` and
`test_a_protocol_error_is_classified_by_who_is_at_fault` asserts a class the real transport never
delivers.

**Fix** — Unwrap at the boundary. In `remote_call` / `remote_version` / `cached_remote` (or in a
single wrapper around the `async with calc_session()` body), catch `BaseExceptionGroup` and re-raise
the single leaf when there is one, preserving `CalcToolError`/`CalcServerError` (`except*` is
available on 3.11). Add a test that opens a *real* `streamablehttp_client` against an in-process
ASGI transport rather than `_Wire`, since the current double is what hides the defect.

---

## 2. The server's "infrastructure fault" class is delivered as non-retryable bad data

**Severity** — High

**Location**
- Server: `/workspace/chemclaw3-mcp/packages/mcp_server_kit/src/mcp_server_kit/app.py:104-108` —
  any tool exception whose cause is not a `ValueError` is logged and replaced with
  `ToolError("an internal error occurred")`.
- Server: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/engine/xtb_cli.py:173` —
  `class CliError(RuntimeError)`, whose own docstring says it is "an infrastructure fault"; raised
  at `:389` (binary absent), `:416` (timeout), `:421` (non-zero exit).
- Core: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/remote.py:206-207` — **every**
  `result.isError` becomes `CalcToolError`, which is registered non-retryable via `ChemclawError` in
  `durable/publish.py:35`.

**Trigger** — Server started with `CHEMCLAW_XTB_ENGINE=xtb CHEMCLAW_XTB_BINARY=definitely-not-installed`
(stands in for any absent binary, xtb timeout, or non-zero exit — all `CliError`), then
`optimize_geometry(smiles="CCO")` through core's client:

```
WIRE optimize_geometry: isError= True  text= an internal error occurred
CORE: innermost = CalcToolError  "optimize_geometry failed: an internal error occurred"
```

**Consequence** — The server has a deliberate two-class taxonomy (`ValueError` = a refusal the
caller caused, everything else = a fault a retry may fix) and the wire flattens it: both arrive as
`isError=True` with only the *text* differing. Core then assigns the whole flattened set to the
non-retryable class. Once finding 1 is fixed — which is the obvious fix — an `xtb` timeout, an OOM
during a Hessian, or a pod that lost its binary will permanently fail a durable calc job instead of
retrying it. That is the mirror image of finding 1 and is *created* by fixing it, so the two must be
fixed together.

Related, and why the existing classification code cannot save it: core's
`_REQUEST_FAULT_CODES` branch (`remote.py:89`, `:195`) is unreachable against this server. Measured
over the wire with `mcp==1.29.0`, FastMCP never emits a JSON-RPC error for a schema violation:

```
extra unknown argument  -> isError=False (silently ignored, tool ran)
wrong-typed argument    -> isError=True  "1 validation error ... charge"
missing required arg    -> isError=True  "1 validation error ... smiles"
```

So the docstring claim at `remote.py:196-199` ("FastMCP answers `-32602` for arguments that fail a
tool's own schema before its body ever runs") is false for the server this module dials, and the
`INTERNAL_ERROR → CalcServerError` arm the tests exercise is equally unreachable — the *only* live
arm is the `isError` one at `:207`.

**Fix** — Give the fault class a machine-readable marker rather than prose. Cheapest honest option:
have `mcp_server_kit` raise the masked notice with a stable prefix or a structured content block
(e.g. `{"fault": "internal"}`) and have `remote.py::_call` map that to `CalcServerError`; everything
else stays `CalcToolError`. Then drop or re-scope the `_REQUEST_FAULT_CODES` branch and its two
tests, which assert a path the server cannot produce.

---

## 3. `compare_solvents` is both a `props` MCP tool and a `calc` durable job; nothing detects the collision

**Severity** — High

**Location**
- Core: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/connector.yaml:130` —
  `jobs: - name: compare_solvents` (a durable xTB solvent screen over a reaction, `params_model:
  chemclaw.connectors.calc.specs:SolventScreenJobSpec`, `inline_wait_seconds: 20`).
- Fleet: `/workspace/chemclaw3-mcp/servers/props/connector.yaml:40` and `:52` — `compare_solvents`
  declared and classified `read_only`; implementation
  `/workspace/chemclaw3-mcp/servers/props/src/chemclaw_mcp_props/tools.py:402`
  `def compare_solvents(names: list[str]) -> ComparisonResult` — a table lookup.
- Core: `/home/user/Chemclaw3/src/chemclaw/connectors/registry.py:571-587` (`job_tools` checks
  job-vs-job only) and `:662` (`connector_tool_names` returns a **set** union, so the duplicate
  disappears from every name-based check).

**Trigger** — Do exactly what `servers/props/connector.yaml:5-6` instructs: put the bundle directory
on `CHEMCLAW_CONNECTORS_DIR`.

```
BOTH an MCP tool and a durable job: [('compare_solvents', ['props'], ['calc'])]
in-process capability tool compare_solvents present: 1
MCP connector also advertising compare_solvents: props http://127.0.0.1:8850/mcp
validate_connectors -> (does not mention it)
```

Two distinct tool objects with the same name reach `create_agent`: the generated job launcher
registered in-process by `chemclaw_agent._register_generated_tools` (`chemclaw_agent.py:500`) and
the MCP tool from the `props` connection (`connector_specs`, `allowed_tools=('...','compare_solvents',...)`).

**Consequence** — Three separate breakages, all silent:

1. The model is offered one name with two incompatible schemas (`{reaction, solvents, ...}` vs
   `{names: list[str]}`); which one it actually reaches depends on tool-list ordering inside
   LangGraph, not on any decision this repo makes.
2. The authorization key is the tool *name*. `state_changing_tool_names()` (`registry.py:601-619`)
   unions every job name, so the props read-only lookup inherits `calc`'s state-changing
   classification and the plan gate refuses it under an unapproved plan — over-gating exactly the
   "what is the flash point of the solvent you are proposing" question the props manifest says must
   be answerable *before* approval.
3. Conversely, if the MCP tool wins dispatch, a plan-gated durable launch name resolves to an
   ungated lookup.

`job_tools()` only refuses *two connectors claiming one job name*; a job name colliding with an
endpoint tool name is not checked anywhere, in either repo.

**Fix** — Add a rule to `validate_connectors`: across enabled bundles, the union of every
`endpoint.tools` and every `jobs[].name` must have no duplicate. Then rename one side — the props
lookup (`compare_solvent_properties`) is the cheaper rename, since `calc`'s `compare_solvents` is
named by profiles, eval probes and skills as a string.

---

## 4. `connector-validate` fails on any foreign bundle dropped on `CHEMCLAW_CONNECTORS_DIR` — the wiring every fleet manifest documents

**Severity** — High

**Location**
- Core: `/home/user/Chemclaw3/src/chemclaw/connectors/registry.py:215-222` — `server_tools_module`
  returns `None` only when `exc.name in {target, package}`, i.e. `chemclaw.connectors.<n>.server.tools`
  or `chemclaw.connectors.<n>.server`.
- Core: `/home/user/Chemclaw3/src/chemclaw/cli/validate_connectors.py:150-155` — a
  `ModuleNotFoundError` that escapes that guard is reported as a validation *failure*.
- Fleet: `/workspace/chemclaw3-mcp/servers/props/connector.yaml:5-6` and
  `/workspace/chemclaw3-mcp/servers/rxnpredict/connector.yaml:3-4` — both instruct exactly this
  drop-in ("the capability appears with no core edit").

**Trigger** — A bundle whose name has no in-tree `src/chemclaw/connectors/<name>/` package at all.
Python then reports the *grandparent* as missing:

```
importlib.import_module('chemclaw.connectors.props.server.tools')
  -> exc.name = 'chemclaw.connectors.props'
  guard set    = {'chemclaw.connectors.props.server', 'chemclaw.connectors.props.server.tools'}
```

```
$ CHEMCLAW_CONNECTORS_DIR="<fleet manifests>:<repo connectors>" python -m chemclaw.cli.validate_connectors
connector 'props':      its server module could not be imported (No module named 'chemclaw.connectors.props')
connector 'rxnpredict': its server module could not be imported (No module named 'chemclaw.connectors.rxnpredict')
```

**Consequence** — The documented zero-core-edit extension path is not usable: adding `props` or
`rxnpredict` turns CI red, and the message blames the *bundle* ("its server module could not be
imported") for a condition that is normal and expected for a connector this release does not run.
`chem` and `safety` escape only by accident — their in-tree directories still exist with an
`__init__.py`, so `exc.name` is `chemclaw.connectors.chem.server`, which the guard does cover. The
guard's own docstring says the package name was added "because a bundle can now be declared and not
run"; it added one level too few.

**Fix** — Widen the guard to any prefix of the target: return `None` when
`target.startswith(exc.name)` and `exc.name.startswith("chemclaw.connectors.")`. That keeps the
property the docstring cares about (a *transitive* missing dependency still propagates, because its
`exc.name` is `rdkit`, `tblite`, … and not a prefix of the target).

---

## 5. Rule 5 of `connector-validate` no-ops for exactly the two cross-repo bundles it is needed for

**Severity** — Medium

**Location**
- Core: `/home/user/Chemclaw3/src/chemclaw/cli/validate_connectors.py:158-159` — `if module is None:
  return []`, reached for every bundle without an in-tree `server/tools.py`.
- Core: `src/chemclaw/connectors/chem/connector.yaml:26-29` and
  `src/chemclaw/connectors/safety/connector.yaml:21-24`, both of which claim: *"nothing structurally
  forces them to agree … `make connector-validate` against a running server is the check that
  catches a drift."*

**Trigger** — Baseline configuration, no change needed.

```
server_tools_module('chem')   -> None
server_tools_module('safety') -> None
server_tools_module('calc')   -> <module ...connectors/calc/server/tools.py>
```

**Consequence** — The claim quoted above is false: `_served_tool_problems` imports a **local Python
module**, it never dials a running server. So the only two bundles whose served surface lives in
another repository — the only two where drift is possible at all — are the two the rule silently
skips. A `Chemclaw3-mcp` release that adds a tool to `servers/safety` (the `index_*`-shaped hazard
the rule's docstring is written about) would be reachable by anything that can open a socket to that
pod, with `make connector-validate` green.

I checked for actual drift and found none today (finding 0 above: names match live, both
directions). The finding is that nothing would tell you if that changed. The sibling
`template-validate` handles the same gap honestly — it *prints* the loss:
`note: template 'hazard-briefing' names ['screen_hazards'], whose bundle is declared but not run
here — name-checked, arguments unchecked`.

**Fix** — Either make the rule reachable (an opt-in `connector-validate --live` that completes an
MCP handshake against `_endpoint_url(...)` for endpoint-bearing bundles with no local module and
diffs `list_tools()` against `endpoint.tools` — the exact script used to produce the evidence in
finding 0 is ~20 lines), or, at minimum, emit the same kind of explicit `note:` line
`validate_templates` does, so "0 problems" stops implying "checked".

---

## 6. Dropping the fleet's `manifests/` directory in wins the `calc` name and silently removes seven tools plus all five durable jobs

**Severity** — Medium (documented hazard, but the shipped instruction leads straight into it)

**Location**
- Core: `/home/user/Chemclaw3/src/chemclaw/connectors/registry.py:108-127` (`_bundle_dirs`, "first
  dir wins", `found.setdefault`).
- Fleet: `/workspace/chemclaw3-mcp/manifests/calc/connector.yaml` (a symlink to
  `servers/calc/connector.yaml`) sits in the same `manifests/` directory as `props`, `rxnpredict`,
  `chem`, `safety` — the directory `servers/props/connector.yaml:5` and
  `servers/rxnpredict/connector.yaml:3` tell an operator to put on the path.

**Trigger** — `CHEMCLAW_CONNECTORS_DIR="/workspace/chemclaw3-mcp/manifests:<repo connectors>"`.

```
connector 'calc': tool 'calculator_outliers'    is served on /mcp but the manifest does not declare it
connector 'calc': tool 'calculator_trust'       ... (same)
connector 'calc': tool 'compute_thermochemistry'... (same)
connector 'calc': tool 'fetch_artifact'         ... (same)
connector 'calc': tool 'find_calculations'      ... (same)
connector 'calc': tool 'list_artifacts'         ... (same)
connector 'calc': tool 'report_measurement'     ... (same)
```

**Consequence** — Both repos forbid this in prose (`servers/calc/connector.yaml:3-8`,
`src/chemclaw/connectors/calc/connector.yaml:11-14`) and the validator does catch it, which is the
good news. The bad news is threefold: (a) the fleet ships the forbidden manifest *inside* the
directory it tells operators to mount, so the safe and the unsafe drop-in are the same path;
(b) the error text blames the server for serving undeclared tools, when the real cause is a manifest
name collision — an operator reading it would go looking in the wrong repository; (c) all five
`calc` durable jobs vanish from the surface with no message at all, because the winning manifest has
no `jobs:` block and nothing compares job sets across a collision.

**Fix** — In `Chemclaw3-mcp`, move `manifests/calc` out of `manifests/` (e.g. `manifests-internal/`),
so the documented drop-in directory contains only bundles that are safe to drop in. In this repo,
make `_bundle_dirs` record shadowed duplicates and have `validate_connectors` report
`connector 'calc' at <dirA> shadows a second manifest at <dirB>` — a collision is worth naming even
when the winner is otherwise valid.

---

## Checked and clean (so the boundary is not re-litigated next round)

- **Declared vs served, every bundle.** `chem` 4/4, `safety` 3/3 (live). `calc` fleet manifest 17/17
  vs `tools.py`'s 17 `@server.tool()`s. `props` 6/6, `rxnpredict` 6/6 (in-process `list_tools`).
  Core's own in-repo servers: `bo` 5/5, `calc` 15/15, `molfp` 2/2, `rxnfp` 1/1 — `connector-validate`
  rule 5 covers these four and passes.
- **Argument shapes for the one cross-repo template step.** `data/templates/hazard-briefing.yaml:23`
  calls `screen_hazards(smiles=["${inputs.smiles}"])`; the server's schema is
  `{"smiles": {"items": {"type":"string"}, "type":"array"}}, required=["smiles"]`. Agrees — verified
  by hand, because `template-validate` cannot reach it (finding 5).
- **Payload models.** Field-by-field diff of 13 shared models across the two projects' venvs: no
  divergence beyond the intended `calc_key`/`calc_version`/`structure_id` extras, which core's
  pydantic `extra="ignore"` drops. Shared literals identical (`FukuiMode`, `EnsembleSearch`,
  `CrestEffort`). `stable_hash` byte-identical implementation; `xtb_geometry_decimals = 4` on both.
- **`calculation_key` accepts-sets vs what core actually sends.** All 12 keyed tools core calls
  return a key with the exact argument dicts `compose.py` and `connectors/calc/server/tools.py`
  build; the two geometry helpers (`embed_structure`, `combine_structures`) are refused by name, as
  `remote.py:276-289` expects; `compute_thermochemistry` is refused, as core's composition expects.
- **Auth.** Token env var names match on all five servers; bearer travels on `initialize()`.
- **`CALCULATION_EPOCH`** is `"1"` in `science/calc/store.py:71` and
  `servers/calc/.../engine/key.py:68`. Note the identity docstring's claim that it is "the one
  constant both repositories must change in the same PR" is over-strict rather than wrong: core
  folds its epoch *around* the server's already-epoch-folded `params_hash`
  (`remote.py:255-262`), so bumping either side alone already invalidates every `calc` row. Harmless,
  but the coupling it asks for is not real.
