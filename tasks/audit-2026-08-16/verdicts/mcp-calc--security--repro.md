# Refutation pass — `servers/calc/` security findings (round 1), lens: does it reproduce?

Scope: the two findings marked **critical** or **high**. The three medium/low findings
(`calculation_key` cost, XYZ comment injection, NaN coordinates) are out of scope and were not
verified.

Environment: `/workspace/chemclaw3-mcp` at `HEAD` = `9217011`, working tree **clean**
(`git status --porcelain` empty, `git diff HEAD -- servers/calc packages/mcp_server_kit` empty, no
`MUTANT` markers) — so nothing below is an artefact of another agent's mutation experiment.
4 CPUs (`nproc`), so `asyncio`'s default executor is `min(32, 4+4) = 8` workers. `xtb` binary
absent (`xtb_cli.is_available()` -> `False`), i.e. the shipped-image configuration.

All scripts are mine, written from the source; none of the reporter's scaffolding was used.
They are under `/tmp/repro/`.

---

## An atomic number of 0 terminates the server process — no exception, no error response, exit code 0

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. The primitive, in isolation** (`/tmp/repro/z0.py` — my own, three lines of tblite, no
chemclaw code at all):

```
$ uv run python /tmp/repro/z0.py; echo "EXIT=$?"
BEFORE
 ** On entry to DSYGVD parameter number  6 had an illegal value
EXIT=0
```

`STILL ALIVE` and the `except Exception` branch never run. The interpreter is gone at
`Calculator("GFN2-xTB", [0,0], ...).singlepoint()`. Exit code **0**, as claimed.

**2. Every guard on the way in** (`/tmp/repro/path.py`):

```
canon: '*'
[…] UFFTYPER: Unrecognized atom type: *_ (0)
elements: [0] id: st_19eb37cb543b56f0
two-dummy accepted: st_bd2d704055ef662a
```

`require_canonical_smiles("*")` returns `'*'`; `structure_from_smiles("*")` yields `elements == [0]`;
`Structure(elements=[0,0], positions=[[0,0,0],[1,0,0]])` constructs and even mints a
`structure_id` — the same `st_bd2d704055ef662a` the finding quotes. Nothing between the wire and
tblite looks at the values in `elements`.

**3. Both tool entry points, in-process** (`/tmp/repro/tool.py`, `/tmp/repro/tool3.py`):

```
# await compute_xtb_energy(smiles="*")
BEFORE
[…] UFFTYPER: Unrecognized atom type: *_ (0)
 ** On entry to DSYGVD parameter number  6 had an illegal value
EXIT=0            <- "STILL ALIVE" never printed

# await compute_properties_at(structure=Structure(elements=[0,0], ...))
BEFORE
structure ok st_bd2d704055ef662a
 ** On entry to DSYGVD parameter number  6 had an illegal value
EXIT=0
```

(Passing a raw `dict` to `compute_properties_at` in-process raises `AttributeError` instead — the
tool body wants a `Structure`. Over MCP the transport coerces the dict into `Structure` first, and
then it is fatal; the second run above is that shape.)

**4. End to end against the real app under uvicorn** (`/tmp/repro/e2e2.py` — my harness:
`uvicorn chemclaw_mcp_calc.app:app`, `CHEMCLAW_CALC_TOKEN` set, real MCP streamable-HTTP
handshakes via `mcp.client`):

```
server pid=3225 up
  [innocent-before]  ok isError=False
  [innocent-during]  ok isError=False
Session termination failed: All connection attempts failed
  [ATTACKER] CLIENT HUNG (no response in 40s)
healthz after: NO RESPONSE ConnectError
server returncode: 0
  [innocent-after] BROKEN ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
--- log tail:
  server calc request: path=/mcp actor=- session=- dry_run=-
  [06:00:29] UFFTYPER: Unrecognized atom type: *_ (0)
  … Terminating session: f999b462b11948ce8245bd9890179c14
   ** On entry to DSYGVD parameter number  6 had an illegal value
```

Attacker gets no response and hangs; the server process is gone with `returncode 0`; `/healthz`
stops answering; every later session fails to connect.

**5. In-flight requests** (`/tmp/repro/inflight.py`) — the one part of the finding a single-shot run
does not show. One innocent `compute_xtb_energy("C"*40)` (a ~2 s call), attacker fires 0.6 s later:

```
up
Session termination failed: All connection attempts failed
Session termination failed: All connection attempts failed
  [innocent-longrunning] HUNG
  [ATTACKER]             HUNG
server returncode: 0
```

The innocent in-flight call is killed with the process and its client hangs too — exactly as
stated. (In run 4 the innocent call happened to *finish* before the SCF reached LAPACK, which is
why it shows `ok`; that is timing, not a limit on the blast radius.)

**6. Line numbers**, checked verbatim:

- `engine/structure.py:55` -> `elements: list[int] = Field(min_length=1)` ✓
- `engine/xtb_engine.py:227` -> `calc = Calculator(method, numbers, positions * ANGSTROM_TO_BOHR, …)` ✓
- `tools.py:188/189` -> `@server.tool()` / `async def compute_xtb_energy(...)` ✓
- `tools.py:558` -> `async def relax_structure(` ✓, `tools.py:590` -> `async def compute_properties_at(` ✓

### Why

Everything the finding asserts reproduces on my own scripts: the fatal call, the absence of any
exception, exit code 0, both stated triggers, the reachability of each through the public tool
surface, and the loss of concurrent and in-flight sessions in the deployed uvicorn shape. The cited
symbols and line numbers are real and current. Nothing upstream prevents it — I also checked the
calling backend (`/home/user/Chemclaw3/src/chemclaw`) for a SMILES or element filter and found none.

Two small things I would not let stand as written, neither of which changes the verdict:

- *"a supervisor sees a clean shutdown rather than a crash"* is an inference, not a measurement, and
  it is probably wrong for the likely deployment: a Kubernetes Deployment's `restartPolicy` is
  always `Always`, so the container restarts and `restartCount` increments regardless of exit code.
  There is no Deployment manifest in this repo to check (`servers/calc/deploy/` contains only
  `networkpolicy.yaml`), so the concealment claim is untested either way. The availability harm does
  not depend on it.
- I would rate this **high** rather than critical: it is availability-only (no auth bypass, no data
  disclosure, no corruption), the tool surface is bearer-authenticated, and a supervised container
  comes back. What keeps it high rather than medium is that the trigger is a *one-character
  legitimate-looking SMILES* an LLM could emit unprompted, it costs one request, and it can be
  repeated indefinitely to hold the capability down for every tenant of the process.

The suggested fix is also the right shape: the bound belongs in
`Structure._normalize_and_validate`, since `structure_from_mol` funnels the SMILES path through the
same model — I verified that path in step 2. Worth adding to the reporter's note: `Z == 0` is not
the only unsupported value, it is only the *fatal* one; the fix should be a range check (`1..86`),
which also converts the existing tblite `Z > 86` exception into a boundary error with a caller-safe
message.

---

## One tool call can burn unbounded CPU: no size cap, no atom cap, no timeout, no concurrency limit

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. The four "no" claims, against the source** (grep, not prose):

```
$ grep -rn "max_length\|max_atoms" servers/calc/src/chemclaw_mcp_calc/
  engine/config.py:100:    xtb_hessian_max_atoms: int = 150
  engine/xtb_hessian.py:141:  if len(structure.elements) > settings.xtb_hessian_max_atoms:
  (no `max_length` anywhere)
$ grep -rn "Field(min_length" …
  structure.py:55 elements · structure.py:56 positions · pka.py:104 · solubility.py:43 ·
  logd.py:56 · xtb.py:36           <- all min-only
$ grep -rn "ThreadPoolExecutor\|set_default_executor" servers/ packages/
  (nothing — 20 call sites of bare asyncio.to_thread in tools.py)
```

So: one atom ceiling on the whole surface and it is consulted by `compute_hessian` only; no length
ceiling on any `smiles`; no executor of the server's own. The only wall-clock timeout,
`xtb_cli_timeout_seconds = 3600`, guards the binary path, and `xtb_cli.is_available()` is `False` in
this image — I ran that. `_optimize_with_library` (`xtb_opt.py:267`) is L-BFGS-B with
`maxiter=1500` and no time bound. All four sub-claims hold.

**2. Cost per request, re-measured** (`/tmp/repro/timing.py`, `/tmp/repro/timing200.py`), my numbers
next to the reporter's:

| input | atoms | mine | reported |
|---|---|---|---|
| `compute_xtb_energy` `C`×10 | 32 | **0.0 s** | 2.0 s |
| `C`×40 | 122 | **1.7 s** | 2.3 s |
| `C`×80 | 242 | **6.5 s** | 9.3 s |
| `C`×200 | 602 | **82.8 s** | 108.7 s |

**3. pKa amplification** (`/tmp/repro/pka.py`):

```
C(O)C                              -> 1.7s   (reported 6.0s)
C(O)C(O)C(O)C(O)C                  -> 3.8s   (reported 15.6s)
C(O)C(O)C(O)C(O)C(O)C(O)C(O)C(O)C  -> 7.3s   (reported 19.3s)
```

Monotonic in the site count, as claimed.

**4. Head-of-line blocking, against the real server under uvicorn** (`/tmp/repro/hol.py`: 8
concurrent `compute_xtb_energy("C"*80)`, one `predict_developability_profile("CCO")` fired 2 s into
the load, `/healthz` polled throughout):

```
baseline:
  [baseline-cheap]      0.1s isError=False
under load:
    healthz -> 200 in 0.06s / 0.19s / 0.45s / 0.38s / 0.32s
  [CHEAP-under-load]   19.0s isError=False
  [heavy0..7]          20.9 – 22.2s
8 heavy done in 22.6s
```

A 0.1 s call became a **19.0 s** call — 190×— and `/healthz` answered 200 in under half a second
the whole time. Reported: 0.1 s -> 30.2 s, healthz green. Same phenomenon, same order.

### Why

Every structural claim is true in the source and every measurement reproduces within a reasonable
factor. The one number I would strike is the reporter's `C`×10 = 2.0 s: mine is 0.0 s, and 2.0 s is
almost certainly their first call paying import and `calc_version` resolution rather than the SCF —
it makes small inputs look 20× more expensive than they are. Their `C`×200 (108.7 s vs my 82.8 s),
their under-load cheap call (30.2 s vs my 19.0 s) and their pKa figures (~2.5× mine) all run high,
which I read as a busier box, not a different mechanism; none of them changes the conclusion, since
the conclusion is *unboundedness* and not any particular second count.

Two additions the reporter did not make, both of which cut in favour of the finding:

- I checked the calling side for an upstream cap — `/home/user/Chemclaw3/src/chemclaw` has no SMILES
  length or atom-count limit on the path into the calc connector either (the only `max_length` in
  the calc bundle is `specs.py:79`, the 2–4 atom list for a scan coordinate). So there is no control
  anywhere in the chain, not just none in this server.
- `predict_developability_profile` is a pure-RDKit descriptor call with no SCF at all, and it still
  waited 19 s. The blocking is the shared executor, not contention for the Hamiltonian — which is
  why the reporter's third fix (an explicit `ThreadPoolExecutor` sized by config) is the one that
  actually restores fairness; the input caps alone would only raise the price of the attack.

Severity **high** stands: an authenticated caller — or an ordinary agent turn on a large molecule —
degrades every other user of the process by two orders of magnitude for minutes, with no bound, no
cancellation on client abandonment, and a liveness probe that never notices.
