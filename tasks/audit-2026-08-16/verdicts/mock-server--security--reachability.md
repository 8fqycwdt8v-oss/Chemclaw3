# Verdicts — `mock-server--security.md`, reachability lens

Lens: *is the trigger reachable, and is the consequence what is claimed?* In scope: the three
findings marked **high**. Findings 4–7 (medium/low) not reviewed.

## The deployment question, settled first

Everything below rests on this, so it is established once rather than repeated.

`chemclaw3_mock` **has no deployment**. Checked, not assumed:

```
$ ls -a /workspace/chemclaw3_mock
.git .gitignore .pytest_cache .venv ISSUES.md LICENSE README.md app
chemclaw3_mock.egg-info pyproject.toml start-mcp.sh start.sh tests uv.lock
$ find /workspace/chemclaw3_mock -maxdepth 2 \( -iname '*docker*' -o -iname '*compose*' -o -iname '*.y*ml' \) | grep -v .venv
      (no output)
$ ls -a /workspace/chemclaw3_mock/.github
ls: cannot access '.github': No such file or directory
$ grep -rni mock /home/user/Chemclaw3/deploy/helm/
      (no output)
```

No Dockerfile, no compose file, no chart, no CI workflow of its own, and no reference in the core's
Helm chart. The only two ways it starts are `start.sh` and
`infra/live/e2e-full-stack/up.sh::start_mock_hpc_eln`, both of which run it as a foreground uvicorn
process on a developer's machine. So "CI" is not even a deployment path for this repo; a developer
box or a live-lane runner is the whole population. It does bind `0.0.0.0:8090` (both start paths),
and it is the *only* process in the four-repo harness that does — `props` and `rxnpredict` bind
`127.0.0.1` (up.sh:81, :93) — so LAN reachability on a dev box is real. But it holds no secret, no
credential, no customer data: every energy is `sha256(smiles|method|basis)` mapped into
[-2000, -50] Ha, and every ELN record is a published USPTO/ORD fixture committed to the repo.

The second fact that governs severity, and which the findings file records but does not carry
through: **the launcher's only credential is `mock-hpc-token`, printed in the repo's own README** as
the value to set on both sides. The reporter files that as finding 6, severity *low*. Any finding
whose consequence is "an unauthenticated caller can do X" is therefore bounded above by the severity
of "the credential is public", because the credential being public already grants X to the same
population. Two of the three findings below are exactly that shape.

---

## Unauthenticated `/_mock/reset` recycles workflow ids, so a live handle starts serving a different job's result

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (reporter said high — the security half is overstated, the
  correctness half is *understated*)

- **What I did**: drove the real ASGI app
  (`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/v1_reset.py`,
  run under `/workspace/chemclaw3_mock/.venv/bin/python`):

  ```
  caller A: id=mock-run-000001 artifact='energy=-719.740267 converged=True'  (CCO)
  anon POST /_mock/reset -> 200 {"status":"reset"}
  A polls its handle after reset -> 404
  caller B: id=mock-run-000001 artifact='energy=-391.177524 converged=True'  (benzene)
  ID REUSED: True | A's handle now serves: energy=-391.177524 converged=True
  routes in openapi: ['/artifacts/{scheduler_job_id}/qm_output.txt', '/eln/reset',
                      '/eln/{source}/entries', '/eln/{source}/entries/{entry_id}',
                      '/healthz', '/workflow/launch', '/workflow/{scheduler_job_id}']
  ```

  Every clause reproduces: the anonymous reset, the 404 on the live handle, the id reissue, the
  wrong artifact served under A's handle, and the absence of `/_mock/reset` from the OpenAPI
  document. `grep -rn "_mock/reset" /workspace/chemclaw3_mock` matches only its own definition — the
  route has no caller.

  I then traced the consequence into the core rather than taking it on the reporter's word.
  `src/chemclaw/connectors/qm/activities.py::parse_qm_output(job, raw_output)` builds
  `QMJobResult(molecule_smiles=job.molecule_smiles, ..., total_energy_hartree=float(...))` — the
  molecule comes from the *caller's* job, the energy comes from the *fetched artifact*. There is no
  cross-check between them. So B's benzene energy is persisted, cached under A's ethanol key and
  written into A's PR-gated note, with nothing raising. The reporter's "nothing anywhere reports an
  error" is exactly right.

  Same script, control arm — construct two fresh `JobStore`s, no HTTP at all:

  ```
  fresh-process id recycling (no reset route): mock-run-000001 == mock-run-000001 -> True
  ```

- **Why**: the mechanism, the trigger and the consequence all hold, so this is CONFIRMED. What I
  dispute is which half of it matters.

  *The security half is overstated.* The reachable population for an anonymous `POST /_mock/reset`
  is "whoever can reach port 8090 on a developer's laptop". That same population can already launch,
  poll and read every artifact by pasting `mock-hpc-token` out of the README, and can already reset
  the store by killing the process. Adding `Depends(require_launcher_auth)` to this route would not
  narrow the population by one host. Framing it as an authentication defect on an unauthenticated
  route buys nothing.

  *The correctness half is understated, and worse than reported.* The control arm above is the
  point: `_sequence = 0` is the JobStore's **initial** state, not something `reset()` invents, so a
  plain process restart recycles ids identically with no HTTP request, no attacker and no
  authentication question. And the harness has a verb for exactly that —
  `infra/live/e2e-full-stack/up.sh:254 restart()`, whose comment reads "the shape the chaos round
  needs", `kill -9` followed by `start_mock_hpc_eln`. So the trigger the reporter needed an
  unauthenticated route for is *already a supported operation of the test harness*. Delete the route
  as the fix proposes and the defect survives untouched. The real fix is the second half of their
  own recommendation — non-recycling ids (`uuid4`, or a sequence `reset()` does not zero) — and that
  half stands on its own.

  Two corrections to the stated consequence, both minor and both in the finding's favour on net.
  (1) The DoS claim is slightly hot: `_poll_nextflow` absorbs a 404 as a transient error up to
  `hpc_poll_max_consecutive_errors` (default 30, `core/config/hpc.py:71`) at
  `hpc_poll_interval_seconds` (2.0s) apart, so a job does not fail on the first 404 — it fails after
  ~60s of them plus `BAD_DATA_RETRY`. Same end state, later. (2) The race window is correspondingly
  *wide*, not narrow: A tolerates 404s for ~60s, so anything that launches inside that minute picks
  up A's recycled id. That is the opposite of a hard-to-hit race.

  Medium rather than high because the blast radius is a test harness producing a wrong number in a
  dev run, not a production system — but it is a genuine silent-wrong-data trap in the one lane
  built to validate the durable QM path, and it deserves the id fix.

---

## A typo'd `MOCK_HPC_ENFORCE_AUTH` value silently turns off all HPC authentication

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**: ran the app once per candidate value in a clean subprocess environment
  (`.../scratchpad/v2_envbool.py`), sending an anonymous `POST /workflow/launch` each time:

  ```
    MOCK_HPC_ENFORCE_AUTH=None         -> hpc_enforce_auth=True   anon launch -> 401
    MOCK_HPC_ENFORCE_AUTH='true'       -> hpc_enforce_auth=True   anon launch -> 401
    MOCK_HPC_ENFORCE_AUTH='TRUE'       -> hpc_enforce_auth=True   anon launch -> 401
    MOCK_HPC_ENFORCE_AUTH=' on '       -> hpc_enforce_auth=True   anon launch -> 401
    MOCK_HPC_ENFORCE_AUTH='1'          -> hpc_enforce_auth=True   anon launch -> 401
    MOCK_HPC_ENFORCE_AUTH='enabled'    -> hpc_enforce_auth=False  anon launch -> 200
    MOCK_HPC_ENFORCE_AUTH='True!'      -> hpc_enforce_auth=False  anon launch -> 200
    MOCK_HPC_ENFORCE_AUTH='y'          -> hpc_enforce_auth=False  anon launch -> 200
    MOCK_HPC_ENFORCE_AUTH='false'/'0'/'no' -> hpc_enforce_auth=False  anon launch -> 200
  ```

  Checked the two callers: `start.sh:11` sets `MOCK_HPC_ENFORCE_AUTH="${MOCK_HPC_ENFORCE_AUTH:-true}"`
  and `up.sh:116` sets `MOCK_HPC_ENFORCE_AUTH=true` unconditionally (not overridable — it is a
  literal, not a `${...:-}` default).

- **Why**: the parser behaves exactly as described — `enabled`, `True!` and `y` all fail open, and
  the absent case defaults safe, so the failure mode really is "someone tried to be explicit and got
  the opposite". Mechanism: granted. What does not hold is **high**, on both legs.

  *Reachability.* The trigger is not a request an attacker sends; it is an operator typing a wrong
  value into a variable that neither start path leaves to chance. `up.sh` hard-codes `true` with no
  env passthrough, so the entire four-repo harness cannot express this misconfiguration. `start.sh`
  respects an export, so a human who exports `MOCK_HPC_ENFORCE_AUTH=enabled` reaches it — that is
  the whole reachable set, and it is one person on one machine, not a caller.

  *Consequence.* The finding says the result is that "launch, poll and artifact fetch all serve
  anonymous requests". True. But the state it is being compared against — auth *on* — is a gate whose
  key is `mock-hpc-token`, published in this repo's README, in `start.sh:10`, and in `up.sh:115`. The
  same reporter files that as finding 6 at severity **low**. A parser bug that converts a
  publicly-keyed lock into no lock cannot be two severity bands above the publicly-keyed lock
  itself; the delta in what an unauthorized party can actually do is nil. And what they can do at
  the far end is launch a job whose "result" is a hash of the SMILES they submitted.

  There is no secret behind this gate, no data behind it, no deployment, and a correct default. The
  suggested fix (reject unrecognized values at import) is right and cheap — `_env_bool` is three
  lines — and I would still take it, as a **low**. The reporter's own strongest sentence, "a security
  switch is exactly the setting where 'I don't understand this value' must not mean off", is a
  hygiene argument, and hygiene is what this is.

---

## The ELN control surface has no authentication and deletes JSON files it did not create

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**: ran `.../scratchpad/v3_eln.py` against the real app with lifespan active,
  planting a foreign file the mock never wrote and then calling the route anonymously:

  ```
  after startup seed: eln json files = 32
  victim exists before: True
  unauth GET /eln/ord/entries -> 200, 59 records, 100400 bytes
  unauth POST /eln/json/entries -> 201
  unauth POST /eln/reset -> 200 {"eln_json_curated":25,"eln_json_real":7,"eln_ord_curated":2...
  victim exists after reset: False | non-.json survives: True
  files after reset: 32
  ```

  Then the control arm — the *same* app, restarted, with **no HTTP request of any kind**:

  ```
  -- control: restart the app, no HTTP request at all --
  victim exists before restart: True
  victim exists after plain startup: False
  ```

  And a traversal probe, since the finding's directory argument depends on which directory is
  reachable:

  ```
  'json'      -> 200
  'ord'       -> 200
  '../../etc' -> 404
  '%2e%2e'    -> 422
  ```

- **Why**: the routes are unauthenticated (no `Depends` in `app/eln/router.py`, confirmed) and
  `_clear_dir` does unlink every `*.json` including foreign ones (confirmed above). Mechanism:
  granted. Two things break the consequence.

  **First, the route adds no destructive capability that the process does not already exercise on
  every boot.** `app/main.py::_lifespan` calls `seed_all(reset=True)` unconditionally when
  `eln_seed_on_startup` is true — which `start.sh:13` and `up.sh:118` both set to `true`. My control
  arm shows the identical file deleted by a plain startup with no request. The repo also *declares*
  this: `.gitignore` carries `/data/` with the comment "Runtime-seeded ELN export directory
  (regenerated on app startup, not source)", and `git ls-files data` returns nothing. So the target
  is a mock-owned scratch directory whose documented contract is "wiped and rewritten at boot".
  "Destroys whatever else lives there" is true of the directory, not of the route; anyone who put a
  file they cared about there lost it at the next `up.sh restart mock-hpc-eln`. The reporter's fix
  (1) — authenticate the route — therefore closes a door that stands open beside it.

  **Second, the loss is self-healing.** `/eln/reset` is `seed_all(reset=True)`: it clears *and
  immediately reseeds*. My run went 32 files → reset → 32 files. The net loss is (a) live entries
  appended since the last seed and (b) foreign `*.json` — in a directory declared regenerable. The
  core only ever *reads* this directory (`ingest/eln/json_adapter.py:114`,
  `ord_adapter.py:82`); nothing in Chemclaw3 writes a cursor, a lock or a state file into it, so
  there is no core-owned artifact to lose. The worst real outcome is an ELN sync running at that
  instant seeing a torn directory and re-ingesting on the next pass.

  On the other two clauses: `GET /eln/{source}/entries` "leaks 45 records" — those records are
  `app/eln/fixtures_data.py` and public ORD/USPTO data committed to this repo and served from it;
  there is nothing to leak. And the directory choice is validator-constrained: `_SourceName =
  Literal["json","ord"]` makes FastAPI reject anything else (404/422 above), so `_directory()` can
  only ever return one of the two configured paths — no traversal, no arbitrary-directory delete.
  That is an upstream guard the finding does not credit, and it is the difference between this and a
  genuine arbitrary-file-deletion bug.

  Low rather than medium because every leg is bounded by "dev-only process, regenerable scratch
  directory, public fixture data, and the same deletion happens at boot anyway". The half of the fix
  worth keeping is (2) — but note it must change the *startup* path too, or it fixes nothing.
