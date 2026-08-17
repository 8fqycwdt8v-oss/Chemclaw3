# Verdicts — `servers/calc/` security (round 1), reachability/consequence lens

Repo under test: `/workspace/chemclaw3-mcp` @ `9217011`, working tree clean (`git status --porcelain`
empty), so nothing below is an artifact of another agent's mutation experiment. Caller side read at
`/home/user/Chemclaw3/src/chemclaw/connectors/calc/`.

In scope: the two findings marked **critical** and **high**. The remaining three are medium/low and
were not examined.

---

## An atomic number of 0 terminates the server process — no exception, no error response, exit code 0

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical (the label is right; if anything the reporter understated the
  *repeatability*, see below)
- **What I did**:

  1. Isolated mechanism, in the server's own venv:

     ```
     $ cd /workspace/chemclaw3-mcp && uv run python /tmp/z1.py     # make_calculator("GFN2-xTB", [0], [[0,0,0]]).singlepoint()
     BEFORE
     calc built
      ** On entry to DSYGVD parameter number  6 had an illegal value
     EXIT=0
     ```

     `STILL ALIVE` is in the script and never prints. No Python exception is raised — a
     `except BaseException` around the call does not fire.

  2. Every input gate on the way in, run for real:

     ```
     canon: '*'                                  # require_canonical_smiles("*") accepts it
     [UFFTYPER: Unrecognized atom type: *_ (0)]
     elements: [0]  st_19eb37cb543b56f0          # structure_from_smiles("*") builds a Structure
     ```

     `Structure._normalize_and_validate` (`engine/structure.py:67-98`) checks array lengths,
     coordinate arity and the electron/multiplicity parity only. `sum([0]) - 0 == 0` is even, so the
     closed-shell test passes. I read the validator: there is no bound on element values anywhere in
     the model.

  3. End-to-end against the real app under uvicorn on port 8871 (bearer set, MCP handshake, two
     innocent sessions in flight, one attacker session sending `compute_xtb_energy(smiles="*")`):

     ```
     healthz: {"status":"ok","server":"calc","revision":"unknown"}
       [innocent1] RESULT ok  {...calc_key: developability@rdkit-2026.3.5...}
       [innocent2] RESULT ok  {...}
       [innocent3] BROKEN: ExceptionGroup unhandled errors in a TaskGroup
       [attacker]  BROKEN: ExceptionGroup unhandled errors in a TaskGroup
     --- after:
     healthz: NO RESPONSE ConnectError
     === ps: SERVER PROCESS GONE
     === log tail:
       Processing request of type CallToolRequest
       [05:53:13] UFFTYPER: Unrecognized atom type: *_ (0)
        ** On entry to DSYGVD parameter number  6 had an illegal value
     ```

  4. Exit status of the **uvicorn** process (not just a bare script), captured by running it in the
     foreground under a wrapper and firing the same call:

     ```
     UVICORN_EXIT=0
     ```

  5. Reachability from the real caller. `/home/user/Chemclaw3/src/chemclaw/connectors/calc/server/tools.py:640`
     `compute_xtb_energy(smiles, charge)` passes the model's string straight to
     `cached_remote(default_store(), "compute_xtb_energy", {"smiles": smiles, ...})`. I read
     `connectors/calc/remote.py` end to end: `cached_remote` → `remote_key` (a `calculation_key`
     round trip, which does **not** run an SCF and so returns normally for `*`) → miss →
     `remote_compute`. Nothing on the Chemclaw3 side canonicalises, length-checks or element-checks
     the SMILES before it goes on the wire; `grep -rn "max_length|len(smiles)" src/chemclaw` finds a
     cap only for SMARTS substructure queries (`substructure_query_max_length`), nowhere on the calc
     path.

  6. The structure-in trigger is reachable through the *read-only* half of the surface. Against the
     live server:

     ```
     embed_structure(smiles="*", multiplicity=1, relax_with_force_field=True)
       -> {"elements":[0], "positions":[[0,0,0]], "charge":0, "multiplicity":1,
           "smiles":"*", "structure_id":"st_19eb37cb543b56f0"}
     ```

     `embed_structure` is listed under `read_only:` in `servers/calc/connector.yaml`, and
     `connectors/calc/compose.py:161` is exactly the durable-job path that calls it with an
     agent-supplied SMILES and then hands the result to `relax_structure` (`compose.py:184`).

- **Why**: every element of the finding reproduces — the crash, the absence of an exception, the
  death of concurrent innocent sessions, the dead `/healthz`, and the zero exit status. The trigger
  is a one-character tool argument on a tool the backend exposes to the model with no validation of
  any kind between the model and `Calculator(...)`.

  Two things I checked *against* the finding and could not use to kill it:

  - **Is anything upstream in the way?** `agent/plan_gate.py` gates `state_changing` tools behind an
    approved plan in `plan_only` mode, and `compute_xtb_energy` is `state_changing`. That is a human
    approval of *the task*, not of the argument — a chemist approving "compute the xTB energy of
    this" is precisely the approval that lets `*` through — and it does not apply in execute mode at
    all. The NetworkPolicy (`servers/calc/deploy/networkpolicy.yaml`) restricts ingress to the
    `chemclaw` pod and the monitoring namespace, so the trigger has to come through the agent; a
    poisoned retrieved document or a curious user is sufficient, and `embed_structure` — the tool
    that mints the poison structure — is `read_only`, i.e. callable by an *unapproved* plan.
  - **Does the pod stay dead?** No. Kubernetes `restartPolicy: Always` restarts on exit 0 too, so
    the finding's "a supervisor sees a clean shutdown rather than a crash" is a claim about
    *telemetry* (true: nothing non-zero, no OOMKill, no signal) rather than about permanence. The
    real harm is the dropped in-flight sessions plus the missing crash signal, not an unrecoverable
    outage. The finding does not claim otherwise, so this does not downgrade it. Note also that this
    repo ships **no Deployment manifest at all** (only a NetworkPolicy), so what restarts the process
    is a deployment-side fact neither the finding nor I can verify from here.

  **What the reporter missed, and it makes it worse.** The dropped socket surfaces in Chemclaw3 as
  `CalcServerError` (`remote.py::_call`'s trailing `except Exception`), which subclasses
  `SubsystemUnavailableError` and is deliberately **absent** from `durable/publish.py::_BAD_DATA_TYPES`
  — i.e. it is *retryable*, and `activity_max_attempts` defaults to 5
  (`core/config/temporal.py:47`). So a durable job that reaches the poison structure through
  `compose.embed` → `compose.relax` does not kill the server once: it kills it, Temporal reads the
  dead socket as a transient outage, and the retry kills the restarted pod again, up to five times.
  A one-character molecule becomes a self-sustaining outage loop rather than a single restart.

---

## One tool call can burn unbounded CPU: no size cap, no atom cap, no timeout, no concurrency limit

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (the reporter's label is right)
- **What I did**: every structural claim read, every measured claim re-measured on this box
  (4 CPUs — `os.cpu_count() == 4`, so the default executor is `min(32, 4+4) == 8`, matching the
  finding's "8 on this box").

  1. **The caps do not exist.** Read directly: `engine/xtb.py:XtbInput.smiles = Field(min_length=1)`,
     `engine/pka.py:101 PkaInput.smiles = Field(min_length=1)`,
     `engine/solubility.py:40 SolubilityInput.smiles = Field(min_length=1)`,
     `engine/descriptors.py:37 DescriptorInput.smiles: str` (no constraint at all),
     `engine/structure.py:55 elements: list[int] = Field(min_length=1)`. No `max_length`, no
     `max_items`, anywhere. `grep -rn xtb_hessian_max_atoms servers/calc/src` returns
     `engine/config.py:100` (the definition) and `engine/xtb_hessian.py:135,141` (the only reader) —
     so the finding is right that the single atom ceiling on the surface guards `compute_hessian`
     alone. `DEFAULT_MAX_REQUEST_BYTES = 1_000_000` confirmed at
     `packages/mcp_server_kit/src/mcp_server_kit/app.py:55`.

  2. **No wall-clock bound on the in-process path.** `grep -n "to_thread|asyncio.wait_for" tools.py`
     returns 18 `asyncio.to_thread` calls and **zero** `wait_for`. `xtb_cli_timeout_seconds` /
     `crest_timeout_seconds` bound the subprocess path only, and this image ships no `xtb` binary
     (declared in `servers/calc/pyproject.toml`).

  3. **Cost per request**, measured in-process:

     ```
     compute_xtb_energy C10  (32 atoms)   0.1s
     compute_xtb_energy C40  (122 atoms)  0.8s
     compute_xtb_energy C80  (242 atoms)  5.9s
     compute_xtb_energy C200 (602 atoms) 78.7s
     ```

     Lower in absolute terms than the reporter's (2.0 / 2.3 / 9.3 / 108.7 — different machine load),
     identical in shape: strongly superlinear, ~80 s for one 200-character string.

  4. **Head-of-line blocking**, against the real app under uvicorn (port 8873): 8 concurrent
     `compute_xtb_energy(smiles="C"*80)`, one cheap `predict_developability_profile("CCO")` fired
     1 s into the load, `/healthz` polled throughout:

     ```
       baseline cheap: 0.1s
        healthz -> 200   (every poll, throughout)
       UNDER LOAD cheap: 13.3s
       8 heavy done in 15.5s
     ```

     A 0.1 s call became a 13.3 s call (130x) while the health endpoint never dipped. Confirmed by
     reading `mcp_server_kit/app.py:202` that `healthz` is a plain coroutine returning a dict — no
     thread hop, so it cannot observe executor saturation by construction.

  5. **The abandoned-client claim** — the part that turns a slow request into an accumulating leak —
     which the reporter asserted but did not demonstrate. I did:

     ```
       idle-baseline cheap call 0.2s
       [8 clients each fire compute_xtb_energy("C"*80) and abandon at 3s]
       all 8 clients gone
       right-after-abandon cheap call 10.1s
     ```

     Eight clients disconnected; the very next cheap call still waited 10.1 s. The threads keep
     running with no client attached, exactly as claimed. `asyncio.to_thread` is not cancellable and
     nothing else bounds them.

  6. **Reachability.** `connectors/calc/server/tools.py` passes the model's SMILES through
     `cached_remote` unmodified (verified by reading `compute_xtb_energy`, `predict_solubility`,
     `predict_pka`); the client-side bound is `calc_server_timeout_seconds = 900.0`
     (`core/config/calculators.py:171`), which abandons the *call* and, per (5), frees nothing. So a
     caller can enqueue faster than the executor drains.

- **Why**: every structural claim is true as written and every measured claim reproduces on
  independent measurement. The trigger needs no malformed input at all — `"C"*200` is a well-formed
  molecule, and a chemist submitting a real 600-atom natural product or peptide produces the same
  effect by accident, which is what makes this reachable without an attacker. The `state_changing`
  plan gate is the only thing upstream and it approves the task, not the size.

  Two small corrections that do not change the verdict:

  - "a Kubernetes liveness probe stays green" is an inference about a deployment object that does not
    exist in this repo (`servers/calc/deploy/` contains only `networkpolicy.yaml`). What I verified is
    the narrower and sufficient fact: `/healthz` returns 200 throughout full executor saturation
    because it never touches a thread.
  - The finding's `predict_pka` amplification numbers I did not re-measure; the mechanism (one
    embed+SCF per ionisable site, `min` over all of them) is visible in `engine/pka.py` and the
    per-call costs above already carry the finding on their own.
