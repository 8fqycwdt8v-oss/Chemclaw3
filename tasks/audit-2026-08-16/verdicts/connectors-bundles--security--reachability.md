# Adversarial verification — `connectors-bundles--security.md`, reachability/consequence lens

Two findings in scope (both **high**). The five medium/low findings were ignored per scope.

All work done at `HEAD = e48441d`. No source file was modified; the only mutation was one row
inserted into and deleted from `calculation_results` in the local Postgres. Probe scripts are under
`/tmp/vprobe/`.

---

## The four connectors this repo actually runs serve their whole MCP surface unauthenticated

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium (high if `networkPolicy.enabled=false`, or on a CNI that does
  not enforce NetworkPolicy)

- **What I did**

  1. Confirmed the declaration split, from the shipped manifests through the real resolver:

     ```
     $ uv run python /tmp/vprobe/p1.py
     calc    declared bearer env -> None
     bo      declared bearer env -> None
     molfp   declared bearer env -> None
     rxnfp   declared bearer env -> None
     chem    declared bearer env -> 'CHEMCLAW_CHEM_TOKEN'
     safety  declared bearer env -> 'CHEMCLAW_SAFETY_TOKEN'
     ```

  2. Went further than the reporter did. Rather than the "REACHED MCP TRANSPORT" surrogate, I ran
     the **real** `calc` app under uvicorn with its lifespan (`chemclaw.connectors.calc.server.app:app`,
     port 18815) against the live Postgres, and completed a full unauthenticated MCP session —
     `initialize`, `notifications/initialized`, `tools/list`, `tools/call` — with no `Authorization`
     header at any point:

     ```
     healthz 200 {"status":"ok","connector":"calc"}
     initialize -> 200
     session id: 93a61970654c45049b1d27b990807dfe
     tools/list -> 200 ['report_measurement', 'find_calculations', 'list_artifacts',
       'fetch_artifact', 'calculator_trust', 'calculator_outliers', 'compute_xtb_energy',
       'predict_solubility', 'predict_pka', 'compute_electronic_properties',
       'predict_site_reactivity', 'optimize_geometry', 'compute_thermochemistry',
       'predict_developability_profile', 'predict_logd']
     find_calculations -> 200 {"result": {"content": [], ...}}
     ```

  3. Proved the payload is real, not an empty-store artefact. Inserted one marked row and re-ran the
     same anonymous session:

     ```
     $ psql ... INSERT INTO calculation_results (...) VALUES ('audit-probe-key','pka','v1',...,
         '{"secret_project":"PROJECT-ZEBRA","pka":4.2}','computed');
     find_calculations -> 200 ... "result": {"pka": 4.2, "secret_project": "PROJECT-ZEBRA"} ...
     ```

     (row deleted afterwards; `DELETE 1`.)

  4. Tested the third consequence bullet the same way:

     ```
     report_measurement -> "NOT recorded. The calibration ledger is disabled in this deployment,
       so the measurement for CCO was not stored and nothing will be scored against it."
     ```

  5. Traced reachability out to the deployment. `deploy/helm/chemclaw/templates/` has exactly one
     `kind: Route` (`service-route.yaml`, the front door); connector Services are default ClusterIP.
     `networkpolicy.yaml:127-176` admits `podSelector: chemclaw.selectorLabels` (in-namespace only)
     plus `.Values.networkPolicy.monitoringNamespaces`, default
     `kubernetes.io/metadata.name: openshift-user-workload-monitoring`; `networkPolicy.enabled`
     defaults `true`.
  6. Checked what those peers already hold. `deployment-connectors.yaml:67` and `:138` include
     `chemclaw.envFrom` and `chemclaw.env`, and `values.yaml:493` makes `CHEMCLAW_POSTGRES_DSN` a
     **required** `secretKeyRef` rendered into every pod.
  7. Checked what this repo ships for `chem`/`safety`:
     `ls src/chemclaw/connectors/chem/` → `__init__.py  connector.yaml` (and `safety/` adds only
     `skills/`). Neither has a `server/` package.

- **Why**

  The **mechanism is beyond dispute** — I reproduced it end-to-end with a real payload, which is
  more than the finding did, and `connector_app`'s own docstring concedes it ("every tool served is
  reachable by anything that can open a socket to this pod"). What does not hold is the framing
  around it, in three places, and together they move this off "high".

  **1. The trigger sentence overstates the peer set, and the peers it does name mostly gain
  nothing.** "Any process that can open a TCP connection to a connector pod's `connectorPort`" is
  true of a laptop and of `make connectors`; in the shipped chart it is true of exactly two sets.
  The first — Chemclaw's own pods — already carries `CHEMCLAW_POSTGRES_DSN` as a required secret
  ref (step 6), so a compromised front-door, worker or connector pod reads `calculation_results`,
  `bo_campaigns` and `bo_suggestions` *directly*. For that half of the grant the open `/mcp` is not
  an escalation; it is a slower route to data the pod can already `SELECT`. The genuinely new
  exposure is the second set, the monitoring namespace — which is real, is a cross-boundary grant,
  and is the part of this finding worth acting on — but it is one hop behind a compromise of the
  cluster's own monitoring stack, and there is no Route or Ingress in front of any of it.

  **2. "The whole gate stack is an alternate-code-path bypass" conflates two threat models.**
  `agent/authz.py`, `tool_authz` and the plan gate constrain *the model inside a turn*. They were
  never a network control and could not be one — the identity headers are advisory by construction
  (`CallerLogMiddleware`, `_recorded_provenance`). An attacker holding in-cluster network position
  is not "bypassing" the agent gates any more than a `psql` session is; describing it as a bypass of
  the gate stack borrows severity from a control that was never claimed to cover this.

  **3. The asymmetry argument — the finding's stated case — is backwards.** "The two bundles this
  repo does **not** run are credential-gated, the four it does run are open" reads as evidence that
  the project gated some servers and forgot others. It did not. `chem` and `safety` ship no server
  at all here (step 7): their `token_env` is an **outbound client** credential, declared because
  `Chemclaw3-mcp`'s server enforces a bearer on the far side — their own manifests say so
  ("Bearer, because the server on the other side enforces one … `mode: none` here would not mean
  'no auth needed' — it would mean every call is refused"), and `values.yaml:178/197` points them at
  `chemclaw3-mcp-safety:8859` / `chemclaw3-mcp-chem:8858`. The 401 in the reporter's probe came from
  `BearerAuthMiddleware` on a **synthetic app that is never deployed anywhere**. So the comparison
  proves the middleware works (which is worth knowing, and which I do not dispute) but proves
  nothing about intent, and the sentence built on it is false.

  **4. One consequence bullet does not hold under shipped defaults.** `report_measurement`
  "writes the calibration ledger that `calculator_trust` reports from" — `calibration_enabled`
  defaults `False` (`core/config/memory.py:116`) and the chart never sets it (`grep CALIBRATION
  values.yaml` → nothing), so the anonymous call returns "NOT recorded" and stores nothing (step 4).
  The reporter knows this — their own low-severity finding cites the same default — which makes its
  omission here an internal inconsistency rather than an oversight I am supplying. What a chemist
  is shown by `calculator_trust` is therefore **not** currently forgeable by this path; it becomes
  so the day an operator turns the ledger on.

  What survives, and is real: anonymous read of every stored calculation payload and its artifacts,
  anonymous `resume_campaign` on a derivable id, and anonymous CPU spend on the physics server using
  the pod's own `CHEMCLAW_CALC_TOKEN` — for any peer the NetworkPolicy admits, and for anything at
  all if `networkPolicy.enabled` is turned off or the cluster's CNI ignores NetworkPolicy. That is a
  genuine defence-in-depth gap with a cheap, already-built fix, and the proposed fix is correct.
  It is medium, not high.

---

## `expensive: true` on CREST is bypassed by `level: "thorough"` on three ungated jobs

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  1. Ran the gate for real, against the shipped manifests, under a real-deployment configuration
     (`entra_required=true`) with an authenticated but role-less actor and an operator who *did*
     configure a privileged role:

     ```
     $ CHEMCLAW_ENTRA_AUDIENCE=api://chemclaw CHEMCLAW_ENTRA_TENANT_ID=tid uv run python /tmp/vprobe/p2.py
     expensive_actions(): ['compute_dft_energy', 'compute_interaction_energy',
                           'request_development_report', 'sample_conformers',
                           'start_optimization_campaign']
     compute_reaction_energy    expensive=False -> LAUNCH ALLOWED
         payload={'kind':'reaction','reactants':['O'],'products':['O'],'level':'thorough'}
     compare_solvents           expensive=False -> LAUNCH ALLOWED
         payload={...,'solvents':['water','thf'],'level':'thorough'}
     scan_coordinate            expensive=False -> LAUNCH ALLOWED
     sample_conformers          expensive=True  -> REFUSED: user chemist-oid lacks a privileged
                                                   role for sample_conformers
     ```

  2. Re-ran it with the **shipped chart's** value (`CHEMCLAW_ENTRA_PRIVILEGED_ROLES: ""`,
     `values.yaml:408`): byte-identical output. `authorize_trigger` fails closed on an empty
     privileged set (`agent/authz.py:384`), so `sample_conformers` is refused for *every* user.

  3. Instrumented `compose.remote_call` / `compose.cached_remote` to record which remote tool each
     path actually asks for (`/tmp/vprobe/p3.py`):

     ```
     reaction_energy(level=quick    ) -> ['embed_structure', 'relax_structure']
     reaction_energy(level=standard ) -> ['embed_structure', 'relax_structure']
     reaction_energy(level=thorough ) -> ['embed_structure', 'embed_structure',
                                          'search_conformer_ensemble']
     sample_conformers (expensive:true) -> ['embed_structure', 'search_conformer_ensemble']
     scan_coordinate                    -> ['embed_structure', 'scan_point']
     ```

  4. Read `connectors/calc/specs.py:76-83` (`ScanJobSpec`) and
     `grep -n "conformer_ensemble" connectors/calc/compose.py` → definition at :365, exactly one
     caller at :605, inside `_species_energy`'s `if level == "thorough"`.

- **Why**

  The **core mechanism is CONFIRMED and I would not soften it**: `compute_reaction_energy` and
  `compare_solvents` at `level: "thorough"` reach the identical `search_conformer_ensemble` remote
  call that `sample_conformers` is gated for, and `prepare_job_launch` never calls
  `authorize_trigger` for them because their `JobSpec.expensive` is false. Step 1 shows the two
  outcomes side by side in one run: same underlying calculation, one refused, one launched. The
  manifest comment asserting a control ("So it carries the role gate that used to sit on
  `run_xtb_task`") is exactly the kind of claim the code does not honour.

  **Two things I would add that make it worse than reported.** Under the shipped chart's own
  defaults (step 2) the gate is not merely leaky, it is *inverted*: `entra_privileged_roles` is
  empty, `authorize_trigger` fails closed, so the honest route — `sample_conformers` — is refused
  for every chemist in the deployment while the ungated route runs the same CREST search for all of
  them. And `compare_solvents` multiplies it: `reaction_energy` runs once per entry in
  `[None, *solvents]` (`compose.py:854`), so a single ungated call is `n_species × (1 + n_solvents)`
  searches.

  **What does not hold is the scope in the title and the severity label.**

  *`scan_coordinate` is simply not in this.* `ScanJobSpec` (`specs.py:76-83`) declares
  `kind/smiles/atoms/values/solvent` and **no `level` field at all** — the finding's own stated
  trigger ("call the ungated `compute_reaction_energy` (or `compare_solvents`) with
  `level: 'thorough'`") is unexpressible for it — and step 3 shows its path terminating at
  `scan_point`, never touching `search_conformer_ensemble`. `conformer_ensemble` has exactly one
  caller in the whole module and it is inside the `level == "thorough"` branch. So the title's
  "three ungated jobs" is two, and one third of the finding's Location block cites a job with no
  path to the defect.

  *The severity is one notch high.* The consequence is an entitlement/cost bypass: an authenticated
  chemist spends CPU an operator meant to restrict. No confidentiality boundary is crossed, no
  stored record is corrupted, and — importantly for this audit's bar — **nothing a chemist is shown
  is wrong**: the `thorough` result is the correct answer, computed correctly, just paid for by a
  user who was not supposed to be able to order it. `compute_reaction_energy` is itself a job an
  unprivileged user is meant to run; only the `thorough` level leaks. That is a real control failure
  worth fixing, and fix (b) in the finding — hanging `authorize_trigger` off the declared
  `precondition` when `spec.level == "thorough"` — is the right shape, since
  `prepare_job_launch` runs preconditions on both launch paths. Medium.
