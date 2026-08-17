# Verdicts — `connectors-bundles--security.md`, lens: does it actually reproduce?

Two findings in scope (both **high**). The other five are medium/low and were not examined.

Everything below was re-derived from source. I did not run the reporter's scripts and did not reuse
their scaffolding; my repros are under `/tmp/repro_auth.py`, `/tmp/serve_calc.py`,
`/tmp/repro_gate.py`, `/tmp/repro_gate2.py`, `/tmp/repro_crest.py`. HEAD = `e48441d`.

---

## The four connectors this repo actually runs serve their whole MCP surface unauthenticated

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  I refused the reporter's method (building `connector_app(...)` in-process and reading a
  `RuntimeError` as "reached the transport"). Instead I served the real bundle app under uvicorn on
  a real socket and spoke real MCP to it with `curl`, with no `Authorization` header at any point:

  ```
  uv run python /tmp/serve_calc.py chemclaw.connectors.calc.server.app 8815
  ```

  1. Health, then an unauthenticated `initialize`:

     ```
     $ curl -s http://127.0.0.1:8815/healthz
     {"status":"ok","connector":"calc"}

     $ curl -i -X POST http://127.0.0.1:8815/mcp -H 'Accept: application/json, text/event-stream' \
         -d '{"jsonrpc":"2.0","id":1,"method":"initialize",...}'
     HTTP/1.1 200 OK
     mcp-session-id: 192e39695b044c02a4281daa4a213433
     data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26",...,
            "serverInfo":{"name":"calc","version":"1.29.0"}}}
     ```

  2. Unauthenticated `tools/list` returned the **entire 15-tool surface**:

     ```
     15 ['report_measurement', 'find_calculations', 'list_artifacts', 'fetch_artifact',
         'calculator_trust', 'calculator_outliers', 'compute_xtb_energy', 'predict_solubility',
         'predict_pka', 'compute_electronic_properties', 'predict_site_reactivity',
         'optimize_geometry', 'compute_thermochemistry', 'predict_developability_profile',
         'predict_logd']
     ```

  3. I then proved the *consequence* rather than just the reachability. I wrote one row into
     `calculation_results` through the repo's own `PostgresStore.put` (key
     `pka@v1:SECRET-PROJECT-MOLECULE-HASH:p`, payload `{"pka": 4.2, "secret_project":
     "Nightingale"}`) and called `find_calculations` with **every filter empty**, still with no
     credential:

     ```
     {"result":[{"calc_ref":"pka@v1:SECRET-PROJECT-MOLECULE-HASH:p","calc_type":"pka",
       "calc_version":"v1","result":{"pka":4.2,"secret_project":"Nightingale"},
       "provenance":"computed","computed_at":"2026-08-17T08:30:46.667938Z","compute_seconds":1.0}]}
     ```

     The row was deleted afterwards (`deleted 1`); the working tree and the DB are as I found them.

  4. Manifest side, read directly: `calc/connector.yaml:31-32`, `bo:18-19`, `molfp:22-23`,
     `rxnfp:18-19` all carry `auth: mode: none`, and `_declared_bearer_env` (`server.py:117-130`)
     returns `None` for `NoAuth`, so `BearerAuthMiddleware.dispatch` (`server.py:199-228`) takes the
     `if token_env is None: return await call_next(request)` branch. Symbols and line ranges are
     current.

  5. NetworkPolicy: `deploy/helm/chemclaw/templates/networkpolicy.yaml` `connector-ingress` admits
     `podSelector: chemclaw.selectorLabels` (every pod in the release, including connector pods) plus
     one rule per `networkPolicy.monitoringNamespaces`, defaulting to
     `openshift-user-workload-monitoring` (`values.yaml:688-689`), on `connectorPort` — the same port
     `/mcp` is on. The template's own comment concedes this. `values.yaml:161-186` shows `molfp`,
     `rxnfp` (and calc/bo) render real Deployments/Services on that port, while `chem`/`safety` carry
     a `url:` and render none.

  6. `HttpEndpoint._a_networked_endpoint_carries_a_credential` (`manifest.py:134-158`) is exactly as
     described: `if isinstance(self.auth, NoAuth) and not is_loopback_url(self.url)`, testing the
     *declared* loopback URL, which the four bundles all satisfy.

  7. One thing the reporter missed, which I checked because it looked like it might refute: FastMCP
     ships DNS-rebinding protection on by default (`allowed_hosts=['127.0.0.1:*','localhost:*',
     '[::1]:*']`). It is **not** a control here — it validates a header the client writes:

     ```
     Host: chemclaw-calc.prod.svc.cluster.local:8815  -> 421
     Host: 127.0.0.1:8815                             -> 200
     ```

     An attacker simply sets the second. (It does mean an operator pointing
     `CHEMCLAW_CONNECTOR_URLS` at a Service DNS name gets 421s for *legitimate* traffic while the
     forged-Host path stays open — the check filters the honest caller and not the dishonest one.)

- **Why**

  Every load-bearing element reproduces on my own scaffolding, and the strongest one —
  unauthenticated exfiltration of a stored calculation payload from a real socket — the reporter did
  not even attempt. The path is not merely "reached the transport": a full MCP session completes,
  the tool list is served, and a tool executes against Postgres and returns another party's data.

  The one part of the report that *is* scaffolding-dependent is the `chem`/`safety` half of the
  asymmetry table. Those two bundles ship no `server/app.py` in this repo (`ls
  src/chemclaw/connectors/*/server/app.py` returns only bo/calc/molfp/rxnfp), so the reporter's
  "chem -> HTTP 401" line comes from a synthetic app built by their probe, not from anything this
  repo runs; `chem`/`safety` declare `mode: bearer` because the *remote* `Chemclaw3-mcp` server
  enforces it, which is a client-side declaration, not proof that this repo's server-side path works.
  That weakens the rhetorical framing ("the two bundles this repo does not run are credential-gated")
  but not the finding: `BearerAuthMiddleware` is real, installed on every `connector_app`, and would
  enforce the moment a manifest declared `mode: bearer` — which is precisely the reporter's fix.

  Severity stays **high** rather than critical: reaching the port still requires a foothold inside the
  release namespace or a monitoring namespace, and `networkPolicy.enabled` gates the whole template.
  But "the second lock, not the first" is the chart's own framing, and there is no first lock.

---

## `expensive: true` on CREST is bypassed by `level: "thorough"` on three ungated jobs

- **Verdict**: CONFIRMED (title overstated by one job — see below)
- **Severity I would assign**: high

- **What I did**

  Two independent measurements, neither using the reporter's stubs.

  1. **Does the gate actually not fire?** `/tmp/repro_gate2.py` binds a real authenticated actor with
     a non-privileged role via `identity_context.set_current_identity`, under
     `entra_required=true` + `entra_privileged_roles=chemclaw.privileged`, and calls the real
     `prepare_job_launch` with the shipped manifests:

     ```
     actor: chemist@example.com roles: frozenset({'chemclaw.user'})
       compute_reaction_energy    launch: ACCEPTED (level=thorough)
       compare_solvents           launch: ACCEPTED (level=thorough)
       sample_conformers          launch: REFUSED — user chemist@example.com lacks a privileged
                                                    role for sample_conformers
     ```

     and the derived set, read from the live registry:

     ```
     expensive_actions() -> ['compute_dft_energy', 'compute_interaction_energy',
                             'request_development_report', 'sample_conformers',
                             'start_optimization_campaign']
       compute_reaction_energy    expensive=False
       compare_solvents           expensive=False
       scan_coordinate            expensive=False
       sample_conformers          expensive=True
       compute_interaction_energy expensive=True
     ```

     `compute_reaction_energy` is also absent from `DEFAULT_WRITE_TOOL_GATES`
     (`authz.py:81-88` — only `compute_dft_energy` and the three KG writers), so nothing else closes it.

  2. **Is it the same work?** `/tmp/repro_crest.py` patches only the *transport*
     (`chemclaw.connectors.calc.remote.calc_session`) with a fake MCP session that records the tool
     names asked for. `cached_remote`, `remote_key`, `remote_compute`, `_call` and the whole of
     `compose` run unmodified — a strictly deeper cut than the reporter's, who replaced
     `compose.cached_remote`/`compose.remote_call` themselves:

     ```
     compute_reaction_energy(level=thorough): call order ['embed_structure','embed_structure',
                                                          'search_conformer_ensemble']
     compute_reaction_energy(level=standard): call order ['embed_structure','relax_structure',
                                                          'relax_structure']
     sample_conformers (expensive: true):     call order ['embed_structure',
                                                          'search_conformer_ensemble']
     compare_solvents(level=thorough):        call order ['embed_structure','embed_structure',
                                                          'search_conformer_ensemble',
                                                          'embed_structure','embed_structure',
                                                          'search_conformer_ensemble']
     ```

     The ungated `compute_reaction_energy` at `level="thorough"` issues the byte-identical remote
     tool the gated `sample_conformers` issues; at `level="standard"` it does not. `compose.py:604-613`
     (`if level == "thorough": ... conformer_ensemble(...)`) and `compose.py:365-409`
     (`cached_remote(store, "search_conformer_ensemble", ...)`) are current and say what the finding
     says.

- **Why**

  The trigger is reachable, the gate demonstrably does not fire, and the protected work demonstrably
  runs — measured on the shipped manifests with the real launcher and the real composition code. The
  manifest's own justification for putting the gate on `sample_conformers` ("a CREST search is the one
  genuinely costly thing in this bundle") is therefore a control the code does not have on two of the
  paths that reach it. `compare_solvents` makes it worse than the report says: it multiplies the
  ungated searches by `len(solvents) × len(species)` — my two-solvent-equivalent run already fired
  two searches per screen, and nothing bounds either list at the spec (this is the reporter's own
  third finding, which is medium and out of my scope, but the two compound).

  **The one part that does not hold**: the title's "three ungated jobs". `scan_coordinate` cannot
  reach CREST. `ScanJobSpec` (`specs.py:73-80`) has **no `level` field at all**, and
  `grep -n "conformer_ensemble(" compose.py` returns exactly one call site, line 605, inside
  `_species_energy` — `scan_profile` never touches it. The finding's own **Trigger** and **Fix**
  sections name only `compute_reaction_energy` and `compare_solvents`, and its Location section's
  literal claim ("carry no `expensive:` key") is true of all three, so this is a titling error rather
  than a severity inflation. The mechanism, the trigger, the consequence and the severity all stand
  for the two jobs that matter.
