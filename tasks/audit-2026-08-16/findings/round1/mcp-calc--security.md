# `servers/calc/` — security and hardening (round 1)

Repo: `/workspace/chemclaw3-mcp`. Slice: `servers/calc/` (`tools.py`, `engine/pka.py`,
`engine/xtb_opt.py`, `engine/xtb_cli.py`, tests), with `engine/structure.py`,
`engine/xtb_engine.py` and `engine/identity.py` read where the slice's inputs land.

Every finding below was reproduced against the real server on this machine
(`.venv`, tblite 0.7.0 / rdkit 2026.3.5, no `xtb` binary — the shipped-image configuration).

---

## An atomic number of 0 terminates the server process — no exception, no error response, exit code 0

- **Severity**: critical
- **Location**:
  - `servers/calc/src/chemclaw_mcp_calc/engine/structure.py:55` (`Structure.elements: list[int] = Field(min_length=1)` — no bound on the values)
  - `servers/calc/src/chemclaw_mcp_calc/engine/xtb_engine.py:227` (`make_calculator` → `Calculator(...)`)
  - reached from `servers/calc/src/chemclaw_mcp_calc/tools.py:189` (`compute_xtb_energy`), `:590` (`compute_properties_at`), `:558` (`relax_structure`), `:618` (`compute_hessian`), `:672` (`scan_point`)
- **Trigger**: either of
  1. `compute_xtb_energy(smiles="*")` — a one-character SMILES. RDKit parses `*` as a dummy atom with atomic number 0; `require_canonical_smiles` accepts it (it is ASCII, non-empty, has one atom), `structure_from_smiles` embeds it, and `elements == [0]`.
  2. any structure-in primitive with an all-dummy geometry, e.g.
     `compute_properties_at(structure={"elements": [0, 0], "positions": [[0,0,0],[1,0,0]], "charge": 0, "multiplicity": 1})`.
     `Structure`'s validator passes it: lengths match, rows are 3-vectors, and `electrons = sum(elements) - charge = 0` is even, so the closed-shell test is satisfied.
- **Consequence**: tblite builds a Hamiltonian with an empty basis and LAPACK's `DSYGVD` argument checker calls `XERBLA`, which **terminates the process**. Not an exception — the Python interpreter is gone. In the deployed shape (one uvicorn process serving every connected turn on one loop, `app.py`) this takes down every concurrent session, in-flight requests included. The caller gets no response at all: the connection dies mid-stream and the client hangs until its own timeout. The exit status is **0**, so a supervisor sees a clean shutdown rather than a crash.
- **Evidence**:

  Process death, isolated (`BEFORE` prints, `STILL ALIVE` never does, exit code 0):

  ```
  BEFORE
   ** On entry to DSYGVD parameter number  6 had an illegal value
  EXIT=0
  ```

  End-to-end against the real app under uvicorn (bearer token set, MCP handshake, one attacker
  session and two innocent ones):

  ```
  server pid=8840 up:
  {"status":"ok","server":"calc","revision":"unknown"}
    [innocent session] RESULT ok
    [innocent session] RESULT ok
  Session termination failed: All connection attempts failed
  [attacker] TimeoutError
  [innocent session] BROKEN: ExceptionGroup unhandled errors in a TaskGroup (1 sub-exception)
  --- after:
  (healthz: NO RESPONSE)
  SERVER PROCESS GONE
  --- server log tail:
  Processing request of type CallToolRequest
  [19:50:02] UFFTYPER: Unrecognized atom type: *_ (0)
   ** On entry to DSYGVD parameter number  6 had an illegal value
  ```

  The attacker call in that run was `compute_xtb_energy(smiles="*")`. The identical run with
  `compute_properties_at(structure={"elements": [0,0], ...})` produced the same log tail and the
  same dead process.

  The dummy atom survives every guard on the way in:

  ```python
  require_canonical_smiles("*")            -> '*'
  structure_from_smiles("*").elements      -> [0]
  Structure(elements=[0,0], ...)           -> accepted, structure_id st_bd2d704055ef662a
  Structure(elements=[500,500], ...)       -> accepted (tblite then raises: "No support for elements with Z >86")
  ```

  Note the asymmetry: `Z > 86` is refused *by tblite, as an exception* (recoverable). `Z == 0` is
  not refused by anything and is fatal. Mixed structures such as `*C*` survive — the crash needs
  the total basis to be empty, i.e. every atom a dummy.
- **Fix**: bound the element numbers where every other physical consistency check already lives —
  `Structure._normalize_and_validate` in `engine/structure.py`:

  ```python
  if any(number < 1 or number > 86 for number in self.elements):
      raise ValueError("every element must be an atomic number tblite supports (1-86)")
  ```

  That covers both triggers, because the SMILES path builds its `Structure` through the same model
  (`structure_from_mol`). It turns the fatal case into the same caller-safe `ValueError` the
  `Z > 86` case already produces. Add a test in `tests/test_engine.py` asserting `Structure(elements=[0], ...)`
  raises — it must be a *validation* test, since a test that calls tblite would kill the pytest
  process rather than fail.

---

## One tool call can burn unbounded CPU: no size cap, no atom cap, no timeout, no concurrency limit

- **Severity**: high
- **Location**: `servers/calc/src/chemclaw_mcp_calc/tools.py` — every compute tool (`:189`, `:210`, `:233`, `:263`, `:295`, `:338`, `:387`, `:558`, `:590`, `:672`); `engine/pka.py:374` `_predict_acid_pka` / `:332` `_predict_base_pka`; `engine/xtb_opt.py:267` `_optimize_with_library`
- **Trigger**: a single authenticated tool call whose input is large but entirely well-formed, e.g.
  `compute_xtb_energy(smiles="C"*200)` (200 characters, 602 atoms). No API-level cap rejects it:
  `PkaInput.smiles` is `Field(min_length=1)` with no maximum, `Structure.elements` is
  `Field(min_length=1)` with no maximum, and the only atom-count ceiling anywhere on the surface is
  `xtb_hessian_max_atoms = 150`, which `compute_hessian` alone consults
  (`engine/xtb_hessian.py:141`). `mcp_server_kit.DEFAULT_MAX_REQUEST_BYTES` is 1 MB, so the HTTP
  layer accepts a ~1,000,000-character SMILES.
- **Consequence**: measured cost per request, and the in-process path (`_optimize_with_library`,
  `_finite_difference`, `gfn2_energy`) has **no wall-clock timeout at all** — only the `xtb`
  binary path does, and that binary is not in this image. `asyncio.to_thread` uses the loop's
  default executor (`min(32, cpu+4)` threads; **8** on this box), and a thread running a tblite SCF
  is not cancellable, so an abandoned client (`connector.yaml` `request_timeout: 900`) does not free
  it. Saturating the executor stalls every other tool call on the process while `/healthz` keeps
  returning 200 — it is a plain coroutine with no thread hop, so a Kubernetes liveness probe stays
  green on a server that is serving nothing.
- **Evidence**: measured on this machine.

  Cost of one request:

  ```
  compute_xtb_energy  C10  (32 atoms)    2.0s
  compute_xtb_energy  C40  (122 atoms)   2.3s
  compute_xtb_energy  C80  (242 atoms)   9.3s
  compute_xtb_energy  C200 (602 atoms) 108.7s
  ```

  `predict_pka` amplifies further — it runs one full embed+SCF per ionisable site
  (`_conjugate_bases` enumerates every O-H/S-H proton, `_predict_acid_pka` takes a `min` over all
  of them; the base branch runs a full `optimize_structure` per protomer):

  ```
  predict_pka 'C(O)C'                          acidic_sites=1 ->  6.0s
  predict_pka 'C(O)C(O)C(O)C(O)C'              acidic_sites=4 -> 15.6s
  predict_pka 'C(O)C(O)C(O)C(O)C(O)C(O)C(O)C(O)C' acidic_sites=8 -> 19.3s
  ```

  Head-of-line blocking, against the real server under uvicorn — 8 concurrent
  `compute_xtb_energy(smiles="C"*80)` calls, one trivial `predict_developability_profile("CCO")`,
  and a `/healthz` poll throughout:

  ```
    baseline cheap descriptor call: 0.1s
    healthz -> 200      (x6, throughout the load)
    under load cheap descriptor call: 30.2s
    8 heavy calls done in 33.2s
  ```

  A 0.1 s call became a 30.2 s call, and the health endpoint never noticed.
- **Fix**: three changes, all at the boundary rather than in the physics.
  1. Give `Structure` an atom-count ceiling and the SMILES inputs a length ceiling, both from
     `CalcSettings` (e.g. `xtb_max_atoms`, default around the existing 150–300, and
     `xtb_max_smiles_chars`). `Field(max_length=...)` on `PkaInput.smiles`/`SolubilityInput.smiles`/
     `DescriptorInput.smiles` and a check in `Structure._normalize_and_validate` cover every entry
     point, since all of them construct one of those models.
  2. Cap the site enumeration in `pka.py` — refuse rather than compute when
     `ionisable_sites(...).total` exceeds a configured bound; the module already has the counter and
     `logd` already refuses polyprotic molecules on scientific grounds.
  3. Bound concurrency explicitly instead of inheriting the interpreter default: create one
     `ThreadPoolExecutor(max_workers=settings.xtb_worker_threads)` in `app.py`, and either set it as
     the loop's default executor or route the tool bodies through it, so the number of simultaneous
     SCFs is a deployment decision rather than a function of `os.cpu_count()`.

---

## `calculation_key` is documented "Cheap; no SCF" and costs 51.6 s on a 200-character SMILES

- **Severity**: medium
- **Location**: `servers/calc/src/chemclaw_mcp_calc/tools.py:159-185` (`calculation_key`, docstring line 160: *"Cheap; no SCF."*), backed by `engine/identity.py:_optimize_geometry` / `_xtb_energy` / `_electronic_properties` / `_site_reactivity`, each of which calls `structure_from_smiles(..., optimize=True)`
- **Trigger**: `calculation_key("optimize_geometry", {"smiles": "C"*200})`
- **Consequence**: the docstring's claim is true only about the SCF. The derivation still runs the
  full RDKit pipeline — ETKDG embedding plus an MMFF relaxation (`xtb_engine.geometry(..., optimize=True)`)
  — because `structure_id` is the `input_hash` and it can only be had by embedding. So the tool the
  docstring tells a caller to hit **before every compute** as a cheap cache probe is itself a CPU
  amplifier with the same missing bounds as the compute tools, and it is superlinear. A caller that
  rate-limits computes but not probes (which is exactly what this docstring encourages) has no cap
  at all. It shares the executor with the compute tools, so it feeds the previous finding.
- **Evidence**:

  ```
  calculation_key(optimize_geometry, "C"*10)  ->  0.0s
  calculation_key(optimize_geometry, "C"*100) ->  6.9s
  calculation_key(optimize_geometry, "C"*200) -> 51.6s
  ```

  (Contrast the module docstring of `engine/identity.py`: *"So this module answers the question one
  round trip earlier, and cheaply: canonicalise, embed, hash, read the versions. **No SCF** —
  `tests/test_calculation_key.py` proves it by making every path through `Calculator` raise."* The
  test proves the absence of an SCF, which is true, and nothing measures the embed.)
- **Fix**: the input-size ceiling from the previous finding fixes the exploit, because the identity
  path builds the same `Structure`. Separately, correct the docstring: "cheap" should read "no SCF;
  cost is one RDKit embedding, which scales with the molecule" — otherwise the next caller budgets
  for it wrongly again.

---

## `Structure.smiles` is unvalidated free text and is written into the XYZ comment line, where a newline forges atom records

- **Severity**: medium
- **Location**: `servers/calc/src/chemclaw_mcp_calc/engine/xtb_cli.py:264-271` (`_to_xyz`, line 266:
  `lines = [str(len(structure.elements)), structure.smiles or ""]`), reachable from
  `tools.py:558` (`relax_structure`) → `xtb_opt.py:390` (`_optimize_with_binary`) → `xtb_cli.run`;
  the field itself is `engine/structure.py:62` (`smiles: str | None = None`, no validator)
- **Trigger**: on a deployment that has the `xtb` binary (`CHEMCLAW_XTB_ENGINE=xtb`, or the `auto`
  default on an image that ships it — `xtb_cli.py`'s own docstring names adding the binary as a
  supported deployment), call `relax_structure` with a structure whose `smiles` carries newlines:

  ```json
  {"elements": [1, 1], "positions": [[0,0,0],[0.74,0,0]],
   "smiles": "h2\nAu 0.0000000000 0.0000000000 0.0000000000\nAu 5.0000000000 0.0000000000 0.0000000000"}
  ```

  Nothing validates `smiles` on the way in: `Structure` checks lengths, coordinate arity and the
  electron count, and `_normalize_and_validate` never looks at the string. The structure-in
  primitives take a whole `Structure` off the wire.
- **Consequence**: the file `xtb` reads is a *valid* XYZ file describing a different molecule. XYZ
  is "count, comment, then `count` atom records"; the injected lines occupy the atom-record
  positions and the real ones fall past the count and are ignored. `xtb` then computes on Au₂ at
  5 Å while this server believes it computed H₂ — and `_from_xyz` (`xtb_cli.py:274`) deliberately
  takes the elements **from the template**, so the mismatch cannot be seen on the way back. The
  result is returned with the submitted structure's `structure_id` and `calc_key`, i.e. an energy
  and geometry for one molecule stored under another molecule's content address in Chemclaw3's
  calculation cache and calibration ledger. Silent, and durable — the poisoned row is served to
  every later lookup of that key.

  This also contradicts the module's stated security property (`xtb_cli.py:44-48`):
  *"Every invocation is an argv list with `shell=False`, built from a typed request; there is no
  control file, no shell string, and no path from model-authored text to a flag."* True about argv;
  the input **file** is built from unvalidated caller text, and the file is as much of an input
  surface as argv is.
- **Evidence**: the file `_to_xyz` produces for the payload above, printed verbatim:

  ```
  2
  h2
  Au 0.0000000000 0.0000000000 0.0000000000
  Au 5.0000000000 0.0000000000 0.0000000000
  H 0.0000000000 0.0000000000 0.0000000000
  H 0.7400000000 0.0000000000 0.0000000000
  ```

  The declared count is 2 and the first two records after the comment are the injected gold atoms.
  (The parse itself could not be executed here — this image ships no `xtb` binary, as `xtb_cli`'s
  docstring states — so the consequence above follows from the XYZ format's definition, not from a
  run.)
- **Fix**: two lines, both worth having.
  1. Sanitise at serialisation, where the format's rule lives —
     `lines = [str(len(structure.elements)), (structure.smiles or "").replace("\n", " ").replace("\r", " ")]`.
  2. Validate the field at the boundary: a `field_validator("smiles")` on `Structure` rejecting any
     string containing whitespace or non-ASCII. `engine/chem.py:require_molecule` already defines
     exactly that rule for SMILES and explains why (*"the parser treats any whitespace as the end of
     the structure and ignores the rest"*) — the wire-facing `Structure.smiles` is the one place a
     SMILES enters this server without it.

---

## `NaN`/`Inf` coordinates are accepted, hashed, and mint a key for a geometry that can never be computed

- **Severity**: low
- **Location**: `servers/calc/src/chemclaw_mcp_calc/engine/structure.py:67-98` (`_normalize_and_validate`) and `:100-132` (`structure_id`)
- **Trigger**: `compute_properties_at`/`relax_structure`/`calculation_key` with
  `{"elements": [1,1], "positions": [[0,0,0],[NaN,0,0]]}` (JSON-RPC clients emit these; pydantic
  accepts non-finite floats by default).
- **Consequence**: the validator passes it (`round(nan, 4)` is `nan`), `stable_hash` serialises it
  via `default=str` and returns a perfectly ordinary-looking `structure_id`, and `calculation_key`
  will hand a caller a complete `CalculationKey` for it. The compute path then fails inside tblite
  (`(sygvd) failed to solve eigenvalue problem. info=4`) — recoverable, and sanitised to a generic
  message by `connector_app`, so the only real harm is a cache key that addresses a calculation
  nobody can ever run. Coincident atoms behave the same way (`Too close interatomic distances found`).
- **Evidence**:

  ```
  positions nan accepted: [[0.0, 0.0, 0.0], [nan, 0.0, 0.0]] st_7bd75aec681c69d2
  positions inf accepted: [[0.0, 0.0, 0.0], [inf, 0.0, 0.0]] st_8ec4ab409b3de6c7
  coincident accepted: st_f86e765d6891b486
  ...
  nan        -> TBLiteRuntimeError: (sygvd) failed to solve eigenvalue problem. info=4
  coincident -> TBLiteRuntimeError: Too close interatomic distances found
  ```
- **Fix**: in the same validator that already rounds them, require finiteness:
  `if not all(math.isfinite(v) for row in self.positions for v in row): raise ValueError("every coordinate must be finite")`.
  Note this changes `structure_id` for no existing legitimate structure, so the content-address
  contract with Chemclaw3 is untouched.

---

## What I looked at and did not find

Reported for completeness, since a clean result through this lens is a result:

- **Command injection into `xtb`/`crest`**: none found. `run()` builds an argv list with
  `shell=False` in a fresh `TemporaryDirectory` with a four-name environment allowlist
  (`_ENV_ALLOWLIST`), and `run_isolated` starts a new session so a timeout kills the whole process
  group. `solvent` is the only string a caller controls that reaches argv and it passes two gates:
  `XtbSpec._solvent_must_be_parameterised` (a 41-name frozenset allowlist) *and* `_safe`.
  `temperature_k` reaches `crest --temp` without `_safe`, but `EnsembleSpec` constrains it `gt=0`,
  so it can never render with a leading `-`.
- **Secrets in logs or errors**: none. The subprocess environment is an allowlist, `CliError` is
  deliberately not a `ValueError` so its stderr tail is replaced by `connector_app`'s generic
  notice, and no setting holding a credential is interpolated into a message.
- **Unsafe deserialisation**: `unpack_array` passes `allow_pickle=False`, and `pack_array` writes
  with it too. `json.loads` on `xtbout.json` reads a file the server's own subprocess wrote in a
  tempdir it created.
- **Path traversal**: no caller-supplied value reaches a path. Every file is a fixed name inside a
  per-run `TemporaryDirectory`.
- **Auth**: `packages/mcp_server_kit/auth.py` fails closed on an unset `token_env`, compares as
  bytes with `compare_digest`, and is middleware rather than a route dependency (a mount would
  bypass `Depends`). `/metrics` is intentionally open and carries counts only. Out of this slice,
  and it held up on reading.
