# Reachability/consequence verdicts — `connectors-bundles--design.md`

Lens: **is the trigger reachable, and is the consequence what is claimed?**

In scope: the two findings marked **high**. The remaining seven are medium/low and were not
examined.

Working tree was clean at `HEAD` (`85735693`) when this ran; no source file was mutated. All
experiments are stand-alone scripts under `/tmp`.

---

## One private-constant import drags LangGraph and layer 1 into every `calc` and `bo` process

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**

  The import chain and every number in the finding reproduce exactly.

  ```
  $ uv run python /tmp/imp1.py <module>          # importlib + sys.modules census
  chemclaw.connectors.calc.server.app: langgraph=True  agent=True mods=2318 t=2.06s
  chemclaw.connectors.calc.worker:     langgraph=True  agent=True mods=2298 t=2.11s
  chemclaw.connectors.bo.server.app:   langgraph=True  agent=True mods=6608 t=7.34s
  chemclaw.connectors.qm.worker:       langgraph=False agent=True mods=1829 t=1.38s
  chemclaw.connectors.molfp.server.app:langgraph=False agent=True mods=1304 t=1.19s
  ```

  Causality, by pre-seeding `sys.modules` with a stub `chemclaw.connectors.registry` carrying only
  `_READ_TIMEOUT_GRACE_SECONDS = 5.0` (`/tmp/proof1.py`, `/tmp/proof3.py`):

  ```
  STUBBED chemclaw.connectors.calc.server.app: langgraph=False agent=True mods=1473 t=1.36s
  STUB    chemclaw.connectors.calc.worker:     langgraph=False agent=True mods=2132
  STUB    chemclaw.connectors.bo.worker:       langgraph=False agent=True mods=6416
  STUB    chemclaw.connectors.bo.server.app:   langgraph=False agent=True mods=6106
  ```

  Then I asked the question the finding did not: **is `connectors/registry` reachable from a
  connector server pod by any other route?** It is — `connectors/server.py:106` imports it lazily
  inside `_declared_bearer_env`, which `BearerAuthMiddleware.dispatch` calls on the first request
  that is not `/healthz` or `/metrics` (`server.py:195`, `:449`). Measured (`/tmp/proof2.py`):

  ```
  after connectors.server import: langgraph= False registry= False
  _declared_bearer_env('calc') -> None
  after that call:               langgraph= True  registry= True
  ```

  So I simulated the fix and then the first request in one process (`/tmp/rss2.py` — stub the
  registry, import the app, delete the stub, call `_declared_bearer_env`):

  ```
  after app import (fix applied):  rss_MB= 157.8 mods= 1474 langgraph= False
  first HTTP request -> _declared_bearer_env('calc') = None
  after first request:             rss_MB= 216.1 mods= 2319 langgraph= True
  ```

  RSS with and without the import, per process (`/tmp/rss.py`, `ru_maxrss`):

  ```
  calc.worker      plain 213.4 MB / 2299 mods   stub 204.7 MB / 2133 mods   (-8.7 MB, -166 mods)
  bo.worker        plain 1053.5 MB / 6583 mods  stub 1044.0 MB / 6417 mods  (-9.5 MB, -166 mods)
  calc.server.app  plain 215.9 MB / 2319 mods   stub 158.0 MB / 1474 mods   (transient — see above)
  ```

  Supporting claims checked and true: `tests/test_connector_isolation.py:37` guards only
  `_HEAVY = ("tblite", "bofire", "botorch", "torch")` in the agent's direction;
  `tests/test_layering.py:321` sanctions `("chemclaw.connectors", "chemclaw.agent")` at package
  granularity; `connectors/worker.py` does **not** import `connectors.registry`, so the two worker
  pods really would shed it. `deploy/helm/chemclaw/values.yaml` confirms the four named pods render
  (`calc`: server+worker, `bo`: server+worker).

- **Why**

  The mechanism is exactly as described — a private constant crossing a bundle boundary is a real
  design defect and the one-line fix is right. Two parts of the *consequence* do not hold, and they
  are the parts carrying the "high" label.

  1. **"drags … layer 1 into every `calc` and `bo` process" is not attributable to this line.**
     `chemclaw.agent` is `True` in *every* connector process measured, including `qm.worker` and
     `molfp.server.app`, which never touch `connectors/calc/remote.py`. It arrives via
     `connectors/server.py` → `connectors/identity.py` → `chemclaw.agent.turn_flags` — the edge
     `test_layering.py:321` explicitly sanctions. Removing the registry import leaves `agent=True`
     in all four cases. Only the `langgraph`/`langchain_*` half is attributable, and the finding's
     own headline claims both.

  2. **For half the named processes the benefit does not survive the first request.** The two
     server pods (`calc`, `bo`) import `connectors.registry` — and therefore LangGraph — from
     `BearerAuthMiddleware.dispatch` on the first non-health MCP call. `/tmp/rss2.py` shows the pod
     returning to *exactly* the unfixed footprint (216.1 MB, 2319 modules, `langgraph=True`). The
     headline 2318 → 1473 / 2.00 s → 1.34 s figure is the one taken from a server app, and it is a
     cold-start-only figure there. What the fix actually buys in steady state is 8.7 MB on
     `calc.worker` and 9.5 MB on `bo.worker` — 4 % and 0.9 % of those pods' resident sets — plus a
     deferred (not removed) import on the two servers.

  No functional, correctness, security or isolation consequence is claimed or exists: nothing in
  the connector pods calls into LangGraph, and the layering test's sanction means no gate is being
  bypassed. This is import hygiene worth fixing — the private-symbol coupling alone justifies the
  three-line move to `core/http.py` — but the severity rests entirely on a bloat figure that
  evaporates for `calc.server.app` and `bo.server.app` and is single-digit megabytes for the two
  workers. Low.

  One correction to the proposed fix, for whoever acts on it: the reverse assertion the finding
  suggests adding to `tests/test_connector_isolation.py` ("`langgraph` must not be in `sys.modules`
  after importing a bundle's server app") would pass while being untrue of the running pod. If the
  invariant is wanted, it has to be asserted after a request has gone through
  `BearerAuthMiddleware`, or `_declared_bearer_env` has to stop reaching the registry too.

---

## A calc-server outage reaches the model as "an internal error occurred"

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium

- **What I did**

  Reproduced end to end against the served tool manager with the physics server pointed at a dead
  port (`/tmp/calcrepro2.py`, `CHEMCLAW_CALC_SERVER_URL=http://127.0.0.1:59999/mcp`, after
  `import chemclaw.connectors.calc.server.app` has run `connector_app` → `_sanitize_tool_errors`
  at `server.py:410`). All eleven named tools, no exceptions:

  ```
  CalcServerError is ValueError? False
  CalcToolError   is ValueError? True

  compute_xtb_energy              ToolError: … : an internal error occurred | cause=CalcServerError
  predict_solubility              ToolError: … : an internal error occurred | cause=CalcServerError
  predict_pka                     ToolError: … : an internal error occurred | cause=CalcServerError
  compute_electronic_properties   ToolError: … : an internal error occurred | cause=CalcServerError
  predict_site_reactivity         ToolError: … : an internal error occurred | cause=CalcServerError
  optimize_geometry               ToolError: … : an internal error occurred | cause=CalcServerError
  compute_thermochemistry         ToolError: … : an internal error occurred | cause=CalcServerError
  predict_developability_profile  ToolError: … : an internal error occurred | cause=CalcServerError
  predict_logd                    ToolError: … : an internal error occurred | cause=CalcServerError
  calculator_trust                ToolError: … : an internal error occurred | cause=CalcServerError
  calculator_outliers             ToolError: … : an internal error occurred | cause=CalcServerError
  ```

  The written message survives only on `__cause__`, which never crosses the wire:

  ```
  __cause__ = CalcServerError: the calculation service is not answering, so no calculation was
  run. This is an outage rather than a problem with what was asked; the same request will work
  once it is back.
  ```

  Counts check out: `grep -c "@server.tool" src/chemclaw/connectors/calc/server/tools.py` = 15; the
  four not affected are the store-only ones (`fetch_artifact`, `list_artifacts`,
  `find_calculations`, `report_measurement`). 11 of 15, as stated.

  I then traced the message the rest of the way to the chemist rather than stopping at the pod
  boundary, because "the caller might catch it" is not an answer here:

  - `agent/audit.py:203 returned_failure` — an MCP tool never raises; `langchain_mcp_adapters`
    converts `isError=True` into a returned `ToolMessage(status="error")`.
  - `agent/tool_authz.py:336-337` — `surface_domain_errors` therefore takes the *returned* branch,
    not the `except (ChemclawError, SubsystemUnavailableError)` branch at `:327`. That handler is
    unreachable for this failure.
  - `agent/tool_authz.py:157 answered_failure` — "the words are kept verbatim", already narrowed by
    `connectors/server.py`'s sanitizer. So the model reads
    `Error executing tool compute_xtb_energy: an internal error occurred`.
  - `agent/tool_authz.py:141 returned_failure_detail` returns `message.text`, so the chemist's
    streamed failure notice carries the same sanitized sentence. There is no second surface where
    the real text appears.

  Reachability of the trigger in a real deployment is not in doubt: `values.yaml` points
  `CHEMCLAW_CALC_SERVER_URL` at `http://chemclaw3-mcp-calc:8860/mcp`, a service rendered by a
  *different* release (companion repo), reached across a NetworkPolicy `egressDestinations` entry an
  operator must add by hand. Every ordinary failure of that dependency — not deployed yet, rolling
  restart, missing egress rule — produces this.

- **Why**

  Every factual claim reproduces. `SubsystemUnavailableError` is deliberately outside the
  `ValueError` hierarchy (`core/errors.py:37`), `_sanitize_tool_errors` forwards only
  `isinstance(exc.__cause__, ValueError)` (`server.py:373`), and the asymmetry the finding points
  at is real and exactly backwards from what you would want: `CalcToolError` ("you asked for
  something impossible") is a `ValueError` and passes through intact, while
  `CalcServerError` ("we are down, retry later") is the one silently replaced. The finding is also
  honest about the limits of its own claim — it correctly excludes the durable path, and I confirmed
  that: `connectors/worker.py` runs the activities in-process and Temporal matches
  `non_retryable_error_types` by class name, so the two-class distinction does survive there.

  What I would not grant is **high**. The failing tool still fails *loudly*: the model is told the
  named tool errored, the audit trail books `outcome="error"`, and the chemist's transcript shows a
  failure notice. No fabricated energy, pKa or logD is served, and no answer is silently degraded —
  `answered_failure` also strips `status="error"` so the provider does not read a retry flag. The
  loss is diagnostic quality: "an internal error occurred" gives the model no basis to distinguish
  *its request* from *the infrastructure*, so the plausible bad outcomes are the model
  re-parameterising a perfectly good SMILES, or abandoning a calculation that a retry in five
  minutes would have completed. That is a real defect with a one-line fix and a bad failure mode
  under a common ops condition, but it is a message-quality defect, not a wrong-science or
  silent-failure one. Medium.

  Two things the reporter missed, both of which sharpen the fix rather than change the verdict:

  - `calculator_trust` and `calculator_outliers` are in the affected set, and those are the two
    tools whose whole job is telling a chemist how much to trust a number. An opaque failure there
    invites the model to proceed *without* the calibration check rather than to report it as
    unavailable — the worst of the eleven, and the finding lists them last without comment.
  - `_sanitize_tool_errors` is shared by `connector_app`, so the proposed one-line change is not
    calc-local. I checked what else it would newly forward: `SubsystemUnavailableError` subclasses
    in the tree are `CalcServerError`, `retrieval.vectors.base.VectorStoreError` and
    `ingest.documents.index.DocumentIndexError`, and neither of the latter two is raised on any
    connector server's tool path today. So the blast radius of the fix is the calc bundle, as the
    finding implies — but that is a property of who raises the class, not of the sanitizer, and it
    will change the day another bundle raises one.

---

## Not examined

Seven findings marked medium or low (`qm` dual identity, `ExperimentSuggestion.scale`, the `bo`
validation preamble, the `calc/worker.py` docstring + `tblite` dependency, the dead stdio `main()`
pair, `nextflow._client`, the duplicate `CHEMCLAW_CALC_SERVER_URL` key) are out of scope for this
pass.
