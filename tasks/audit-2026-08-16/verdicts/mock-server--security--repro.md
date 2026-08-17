# Verdicts — chemclaw3_mock — security & hardening (lens: does it actually reproduce?)

Scope: the three findings marked **high** in
`tasks/audit-2026-08-16/findings/round1/mock-server--security.md`. No finding in that file is
marked critical. Medium/low findings are out of scope and were not verified.

Everything below was re-derived from source. I did not run the reporter's scripts; mine are
`/tmp/v1_reset.py`, `/tmp/v1_core_client.py`, `/tmp/v2_envbool.py`, `/tmp/v3_eln.py` plus the
inline `curl` lines quoted per section. `/workspace/chemclaw3_mock` is clean at
`2f09174` (`git status --porcelain` → only an untracked `uv.lock`), so nothing here is an
artifact of another agent's mutation.

## Deployment question, settled first (applies to all three)

The mock has **no Dockerfile, no compose file, no CI workflow and no manifest of any kind** —
`find . -iname 'Dockerfile*' -o -iname '*compose*' -o -path '*.github*'` returns nothing. Its only
two entry points are `start.sh` (a developer typing `./start.sh`) and
`infra/live/e2e-full-stack/up.sh::start_mock_hpc_eln`, which execs
`uvicorn app.main:app --host 0.0.0.0 --port 8090` on the developer's own machine. It is never a
deployed service.

The bind *is* `0.0.0.0` in both, so it is reachable from the local network. But the credential that
gate checks is published: `README.md:211` prints the default token in a table
(`| MOCK_HPC_API_TOKEN | mock-hpc-token |`) and `README.md:72` tells you to export it. So on this
server, "unauthenticated" and "authenticated with the documented constant" grant the same
capability to the same set of people. Any finding whose whole consequence is *an anonymous caller
can do X* has essentially zero marginal exposure over the repo's own instructions — which is why
the reporter's own finding 6 correctly rates the constant-token/0.0.0.0 pair **low**. Findings that
survive that argument must survive on *correctness*, not on exposure.

---

## Unauthenticated `/_mock/reset` recycles workflow ids, so a live handle starts serving a different job's result

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**:

  1. Wrote my own repro (`/tmp/v1_reset.py`) that runs the real ASGI app under **uvicorn on a
     socket** — not `TestClient`, not the repo's fixtures — with `MOCK_HPC_API_TOKEN=s3cret-token`
     (deliberately *not* the default, so nothing can pass by knowing the README) and talks to it
     with plain `httpx`:

     ```
     caller A: id=mock-run-000001 artifact(200)='energy=-1382.626558 converged=True'   # CCO
     anonymous POST /_mock/reset -> 200 {"status":"reset"}
     caller A re-polls its live handle -> 404 {"detail":"unknown workflow id"}
     caller B: id=mock-run-000001 artifact(200)='energy=-1411.521183 converged=True'   # benzene
     ID REUSED: True
     A's handle now serves B's artifact: True
     reset with wrong token -> 200
     ```

  2. Then drove the **core's own client** (`chemclaw.connectors.qm.hpc.nextflow`, not a hand-rolled
     HTTP call) against a live mock on :8390, `/tmp/v1_core_client.py`:

     ```
     A handle: mock-run-000001
     anonymous /_mock/reset -> 200 {"status":"reset"}
     B handle: mock-run-000001 | same id as A: True
       A poll 0: RunState.RUNNING
       A poll 1: RunState.SUCCEEDED
     A fetched artifact: energy=-345.864973 converged=True
     energy the core would cache under 'CCO': -345.864973
     B's artifact: energy=-345.864973 converged=True
     A's cached energy is actually B's benzene energy: True
     ```

     CCO's true value on the same server is a different number — launching CCO fresh gives
     `energy=-523.476741`:

     ```
     $ curl -s -X POST .../workflow/launch -d '{"params":{"smiles":"CCO",...}}'  -> mock-run-000002
     $ curl -s .../artifacts/mock-run-000002/qm_output.txt   -> energy=-523.476741 converged=True
     ```

     So the core's `launch → poll → fetch` for ethanol returned benzene's energy, through the
     production code path, with no error anywhere.

  3. Checked the caller claim: `grep -rn "_mock/reset"` across the mock repo matches only
     `app/hpc/router.py:58`; across `/home/user/Chemclaw3` it matches only the findings file
     itself. `tests/conftest.py:23` calls `job_store.reset()` in-process. The route has no caller.

  4. Line numbers verified current: `app/hpc/router.py:58-62` is the reset route and is the only
     route in the file with no `dependencies=`; `app/hpc/store.py:110-114` is `reset()` with
     `self._sequence = 0` on line 114.

- **Why**: it reproduces exactly as described, on my own scaffolding, and end-to-end through the
  core's real client. Two things I would add that make it *worse* than the report says, and one
  that makes the report's framing weaker:

  - **The recycling does not need the route or an attacker.** Killing and restarting the mock
    reproduces it identically, because `_sequence` is process state:

    ```
    # restart the mock, then, with A still holding mock-run-000001 from before:
    $ curl -X POST .../workflow/launch -d '{"params":{"smiles":"c1ccccc1",...}}' -> mock-run-000001
    $ curl .../artifacts/mock-run-000001/qm_output.txt -> energy=-345.864973 converged=True
    ```

    `infra/live/e2e-full-stack/up.sh` ships `restart mock-hpc-eln` as a first-class verb,
    commented as "the shape the chaos round needs". So the wrong-answer path is reachable in the
    harness's own chaos scenario with nobody attacking anything.

  - **The core's poll loop widens the window rather than closing it.**
    `src/chemclaw/connectors/qm/activities.py:119-131` absorbs a `NextflowError` as a transient
    blip up to `hpc_poll_max_consecutive_errors` — default **30**
    (`src/chemclaw/core/config/hpc.py:71`) at a 2 s interval. So the 404 the report calls "denial
    of service" is *swallowed for ~60 s*, and any launch inside that window hands A's id back to a
    second job and turns the swallowed error into a silent wrong result. The DoS half is the
    benign half; the swallowing is what makes the silent half likely.

  - The "unauthenticated" framing carries less weight than the report gives it, per the deployment
    section above: the launcher token is printed in the README, so gating the route would not
    change who can call it. That is why I am confirming on the correctness half, which the task
    asked to be judged on its own merits and which holds completely. The report's fix is right for
    the wrong reason: adding `Depends(require_launcher_auth)` buys almost nothing; making ids
    non-recycling (seed from a monotonic source `reset()` does not touch, or `uuid4`) is the fix
    that matters, and it also closes the restart path the report never noticed.

---

## A typo'd `MOCK_HPC_ENFORCE_AUTH` value silently turns off all HPC authentication

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**: `/tmp/v2_envbool.py` starts a fresh interpreter per value (so `Settings` is
  really re-read at import) and sends a header-less launch and artifact fetch each time:

  ```
  MOCK_HPC_ENFORCE_AUTH=<unset>   hpc_enforce_auth = True    launch -> 401
  MOCK_HPC_ENFORCE_AUTH='true'    hpc_enforce_auth = True    launch -> 401
  MOCK_HPC_ENFORCE_AUTH='yes'     hpc_enforce_auth = True    launch -> 401
  MOCK_HPC_ENFORCE_AUTH='on'      hpc_enforce_auth = True    launch -> 401
  MOCK_HPC_ENFORCE_AUTH='TRUE '   hpc_enforce_auth = True    launch -> 401
  MOCK_HPC_ENFORCE_AUTH='enabled' hpc_enforce_auth = False   launch -> 200 {'workflowId': 'mock-run-000001'}
                                                             artifact (no header) -> 200 energy=-523.476741 converged=True
  MOCK_HPC_ENFORCE_AUTH='True!'   hpc_enforce_auth = False   launch -> 200
  MOCK_HPC_ENFORCE_AUTH='y'       hpc_enforce_auth = False   launch -> 200
  MOCK_HPC_ENFORCE_AUTH='tru'     hpc_enforce_auth = False   launch -> 200
  MOCK_HPC_ENFORCE_AUTH='enforce' hpc_enforce_auth = False   launch -> 200
  ```

  `app/config.py:21-25` is exactly as quoted and the accepted set is exactly
  `{"1","true","yes","on"}` after `.strip().lower()`.

- **Why**: the mechanism is real and reproduces on every value the report names but one — its
  `1 ` example is wrong, `_env_bool` calls `.strip()` first, so a trailing space is fine (see
  `'TRUE '` → True above). What does not hold is the severity.

  The consequence is "an anonymous caller can use the mock launcher". On this server that is the
  *same* capability the README hands out for free: `README.md:211` publishes `mock-hpc-token` as
  the default and `README.md:72` tells you to export it, and `start.sh:10` /
  `up.sh::start_mock_hpc_eln` both use it. An attacker with the repo does not need the typo. The
  report's own finding 6 rates precisely that state of affairs **low**; a bug whose only effect is
  to reach a state already rated low cannot itself be high.

  What the gate protects is also nothing: a hash-derived fake energy
  (`app/hpc/store.py:47-51`), an in-memory job dict, and an artifact string. No secret, no real
  compute, no tenant data. Both starters set the literal `true`, so the typo requires a human to
  hand-edit the value.

  I agree with the *fix* — fail-closed parsing of a security switch is correct and costs four
  lines — but it is a hygiene item on a dev tool, not a high-severity security finding. Note it is
  also not specific to auth: `MOCK_ELN_SEED_ON_STARTUP` goes through the same `_env_bool`, so if
  this is fixed it should be fixed in the parser, not in one caller.

---

## The ELN control surface has no authentication and deletes JSON files it did not create

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**: `/tmp/v3_eln.py`, against a live mock on :8390 with export dirs at
  `/tmp/mockdirs/{eln,ord}`:

  ```
  unauth GET /eln/ord/entries        -> 200  45 records, 77398 bytes
  unauth POST /eln/json/entries      -> 201  -> id uspto-live-0033
  victim exists before reset: True | eln files: 34
  unauth POST /eln/reset             -> 200  {'eln_json_curated': 25, 'eln_json_real': 7, ...}
  victim exists after reset: False | non-json survivor: True
  reset with garbage token           -> 200
  ```

  So: every claimed mechanism is real. `app/eln/router.py` imports no `Depends`, no route carries
  a dependency, and `app/eln/seed.py:30-33` unlinks every `*.json` (my `not-json.txt` decoy
  survived, confirming the glob is `*.json` and nothing broader).

- **Why**: OVERSTATED because each of the three consequences collapses when checked.

  1. **"deletes JSON files it did not create" is not a capability the endpoint adds.**
     `app/main.py:_lifespan` calls `seed_all(reset=True)` on **every startup** when
     `MOCK_ELN_SEED_ON_STARTUP` is true — which `start.sh:13` and `up.sh` both set, and which is
     the default. Measured: I dropped `site-notebook-2019.json` into the export dir, restarted the
     process, made **no HTTP request at all**, and it was gone:

     ```
     $ echo '{"id":"site-notebook-2019"}' > /tmp/mockdirs/eln/site-notebook-2019.json
     $ <restart uvicorn app.main:app>
     $ ls /tmp/mockdirs/eln/site-notebook-2019.json
     ls: cannot access ...: No such file or directory
     ```

     Pointing this process at a directory *is* granting it "wipe the `*.json` in here on boot".
     The unauthenticated route lets a stranger trigger a thing the operator already triggers every
     time they start the server.

  2. **Nothing else writes into that directory.** `grep -rn "eln_export_dir\|ord_export_dir" src/`
     in the core returns only `json_adapter.py`, `ord_adapter.py` and `validate.py` — all readers.
     `up.sh` sets `CHEMCLAW_ELN_EXPORT_DIR="$MOCK_REPO/data/eln/exports"`, a directory inside the
     mock's own checkout. The "destroys whatever else lives there" victim has no producer; I had
     to plant the file myself.

  3. **The unauthenticated write injects nothing.** `add_entry` takes only an `int`
     `archetype_index` query parameter — the request **body is ignored entirely**. Verified:

     ```
     $ curl -X POST .../eln/json/entries -d '{"id":"EVIL","payload":"<script>"}'
     {"id":"uspto-live-0033","timestamp":"2024-01-16T17:00:01Z","reactants":[{"smiles":"COc1ccc(Br)cc1",...
     ```

     The file written is a deep copy of a curated fixture with a generated id
     (`app/eln/seed.py:104-119`, `:122-139`). A caller controls the *count* of appended records,
     never their content — so this is not a path into the core's knowledge graph, it is at most
     the unbounded-append cost already filed as the reporter's own medium finding 4.

  4. **The "45 records leaked" are public.** They are `app/eln/fixtures_data.py` /
     `real_hte.py` / `real_procedures.py` — fixtures committed to a public repo. Reading them over
     HTTP discloses nothing reading the repo does not.

  What genuinely survives is the asymmetry the report names in its last sentence: the HPC half has
  a token seam and the ELN half has none, on one process and one port, and `_clear_dir` is broader
  than it needs to be. Both are worth the small fix. Neither is high: on a dev-only process, with
  public fixture data, whose destructive step already runs unprompted at boot, and whose write path
  is content-fixed.
