# Verdicts — `infra-deploy-ci--design.md`, lens: **does it actually reproduce?**

Scope: the three findings marked **high**. No findings are marked critical. Everything else in the
file is medium/low and was not examined.

All work done at `HEAD = 8573569`, against the source, with my own scripts (`/tmp/verif_tblite.py`,
`/tmp/verif_tblite2.py`, an inline `down()` scenario). I did not run the reporter's `/tmp/no_tblite.py`
and did not accept any transcript in the findings file. No source file was mutated.

---

## `tblite` is a production dependency with zero runtime importers — 17.5 MB in every image

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**

  Every factual sub-claim reproduces. I re-derived each one.

  The declaration is where the finding says it is — `pyproject.toml:152`, inside `[project].dependencies`,
  and it is in the `--no-dev` closure the Containerfile installs:

  ```
  $ uv export --no-dev --no-hashes | grep '^tblite'
  tblite==0.7.0
  ```

  `deploy/Containerfile:101` (finding says 106; the recipe line is 101, the `RUN` block spans 101-105)
  is `RUN uv sync --frozen --no-dev`, so that closure is what lands in the image.

  My own import-blocker probe — a `meta_path` finder that raises on `tblite`, then
  `pkgutil.walk_packages` over the whole first-party package:

  ```
  $ uv run python /tmp/verif_tblite.py
  modules walked: 340
  needed tblite : []
  other failures: 0
  tblite in sys.modules: False
  ```

  340 modules, every one imports clean with `tblite` unreachable. Nothing in `src/` needs it.

  Nothing else in the closure pulls it in either — I checked the installed metadata rather than
  trusting the claim:

  ```
  $ uv run python /tmp/verif_tblite2.py
  requirers of tblite: [('chemclaw', 'tblite>=0.7.0')]
  ```

  Size reproduces: `7.6M` + `9.9M` + `32K` = 17.5 MB.

  The false docstring is real and worse than quoted. `src/chemclaw/connectors/calc/worker.py:5`
  says *"`tblite` and the `calc.*` closure are loaded in this process and nowhere else"* — it is
  loaded in no process; and lines 3-5 are indeed mangled (`serves\nwhatever\nimporting this bundle's
  modules registered`). `pyproject.toml:146-149` carries a second stale claim the finding missed:
  the comment above `scipy` still says scipy was promoted "when `calc.xtb_opt` and `calc.xtb_thermo`
  began importing it directly" — those modules are gone too.

- **Why the grade does not hold**

  The mechanism is entirely confirmed. What does not survive is the consequence as framed. The
  finding's headline metric is "17.5 MB in every image", and I measured what that 17.5 MB sits next
  to:

  ```
  $ uv export --no-dev --no-hashes | grep -E '^(torch|jaxlib|xgboost|rdkit|nvidia-cuda)'
  jaxlib==0.9.2
  nvidia-cuda-cupti==13.0.85 …
  rdkit==2026.3.5
  torch==2.13.0
  xgboost==3.2.0 …

  $ du -sh .venv/lib/python3.11/site-packages/*  | sort -rh | head -5
  2.9G  nvidia
  1.1G  torch
  689M  triton
  327M  jaxlib
  227M  xgboost
  ```

  torch, the CUDA wheels, jaxlib and xgboost are all *production* dependencies, so the runtime image
  already carries multiple GB. `tblite` is ~0.3% of the closure — a rounding error against what the
  same `uv sync --frozen --no-dev` installs one line earlier. "17.5 MB in every image" is arithmetically
  true and operationally irrelevant, and it is the only stated consequence. No security or correctness
  consequence is claimed and I found none: with the library blocked, all 340 modules still import.

  What is genuinely worth fixing here is the *statement*, not the megabytes: a declared production
  dependency with zero importers plus a module docstring asserting the opposite is a documentation
  defect with a one-line fix. That is a low, not a high.

---

## `make live-e2e-full-stack-down` stops nothing and exits 0 when the e2e run directory is absent

- **Verdict**: CONFIRMED
- **Severity I would assign**: low

- **What I did**

  Line numbers are exact and current: `down()` opens at `infra/live/e2e-full-stack/up.sh:219`, the
  early return is line 221, the delegation `bash "$REPO_ROOT/infra/live/processes.sh" down` is line
  235 (the finding says 234 — off by one), and `status()` at 238 has the identical guard *without*
  the `return`, so it delegates unconditionally.

  I wrote my own scenario rather than reusing the reporter's, and used a **real live process** so
  the consequence is observed, not inferred — a `sleep 600` whose pid is recorded as
  `$LIVE_DIR/run/api.pid` (the layout `processes.sh` uses, `RUN_DIR=$LIVE_DIR/run`, distinct from
  the e2e script's `RUN_DIR=$LIVE_DIR/e2e/run`):

  ```
  started fake api pid=18349
  --- CASE A: e2e run dir absent ---
  [e2e] stopping Chemclaw3_ui
  [e2e] nothing running
  rc=0
  RESULT A: fake api STILL ALIVE
  api.pid                       # pidfile untouched

  --- CASE B: e2e run dir present (mkdir -p $LIVE_DIR/e2e/run) ---
  [e2e] stopping Chemclaw3_ui
  [e2e] stopping this repo's connectors/workers/front door
  [live] api stopped (pid 18349)
  rc=0
  RESULT B: killed
  ```

  One `mkdir` is the entire difference between "delegates and kills the front door" and "reports
  success having stopped nothing". Exit status is 0 in both.

  Trigger reachability confirmed against the Makefile: `live-up` (line 296) runs `processes.sh up`,
  which creates `$LIVE_DIR/run`; `live-e2e-full-stack-down` (line 308) runs `up.sh down`, which reads
  `$LIVE_DIR/e2e/run`. `up.sh`'s own `mkdir -p "$RUN_DIR"` is at line 164, inside `up()` and *before*
  it shells out to `processes.sh up` at line 211 — so a partial e2e start cannot produce this state,
  and after any full e2e run the directory persists (`down()` only `rm -f`s the pidfiles). The
  reachable path is exactly the one the finding names: single-repo lane up, four-repo `down` invoked.

- **Why**

  The defect is real, reproduces on the first attempt from a clean scenario, and the divergence from
  `status()` twelve lines below is inside one file — nothing upstream prevents it and nothing
  downstream notices. I am adding one thing the reporter missed: the `log "stopping Chemclaw3_ui"`
  at line 220 fires *before* the guard, so the failure mode also prints a line claiming an action it
  is about to skip, which is what makes the exit-0 read as success.

  I grade it low rather than high only because of blast radius: this is a developer live-test
  harness, not a shipped path. Nothing in `deploy/`, `.github/workflows/` or the image touches it,
  the worst outcome is stale local processes holding ports and pool slots after a wrong-command
  sequence, and the next `processes.sh up` reports "already running" rather than failing confusingly.
  Real bug, cheap fix, small stakes.

---

## The live lane's process harness is cloned across three scripts, and the clone dropped the fix

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**

  The code facts check out; the consequence does not.

  Cited lines are all real and current — `processes.sh:32,33,81,119,204,222,246` and
  `up.sh:30,31,42,54,219,238,254` are `log`/`die`/`start`/`wait_for`/`down`/`status`/`restart` in
  both files, and `soak.sh:53`'s `python_bin()` is byte-identical to `processes.sh:79`. The budget
  divergence is exactly as quoted (`${3:-300}` vs `${3:-120}`), and every `wait_for` call site in
  `up.sh` (lines 82, 94, 121, 155) uses the default, so 120 really is the budget for `props` and
  `rxnpredict`.

  Then I tested the claim the *severity* rests on: that 120 attempts (≈120 s, one `sleep 1` per
  iteration, `curl --max-time 2` returning instantly on a refused connection) will kill healthy
  `props`/`rxnpredict`, described as "the two processes with the heaviest imports of the eleven it
  starts".

  First, against the primary source — the upstream servers' own manifests, not this repo's
  description of them:

  - `Chemclaw3-mcp/servers/props/pyproject.toml`: `dependencies = ["mcp-server-kit", "uvicorn[standard]"]`,
    with the comment *"The engine is arithmetic over a CSV … no CoolProp, no scientific stack, no HTTP client."*
    No RDKit at all.
  - `Chemclaw3-mcp/servers/rxnpredict/pyproject.toml`: `["mcp-server-kit", "pydantic-settings", "rdkit", "uvicorn[standard]"]`.
    torch appears **only** in optional extras.
  - `Chemclaw3-mcp/pyproject.toml` (workspace root, what `mcp_python_bin()`'s `uv sync` resolves):
    *"Predictor dependencies, each behind its own extra and **none installed here**."*

  So the lane's `props` process imports uvicorn, and `rxnpredict` imports uvicorn + RDKit. Timed here:

  ```
  props-class (import uvicorn)                          : 0.16 s
  rxnpredict-class (import rdkit.Chem, uvicorn)         : 0.22 s
  torch + rdkit.Chem + bofire (the 300 s comment's set) : 1.75 s
  this repo's connector composite (build_composite())   : 8.76 s
  ```

  The ordering is the refutation. `props` and `rxnpredict` are the two *lightest* processes in the
  lane, ~40x cheaper than the front door / connector / worker processes — and those heavy processes
  are started by `processes.sh up` (called from `up.sh:211`), where the 300 default is **intact**.
  The dependency set the 300 s comment was measured on is never waited on at 120.

  Scaling the comment's own measurement (~1 GB not ready at 90 s ⇒ ≈11 MB/s cold): RDKit's ~74 MB
  pages in around 7 s, ~17x inside the 120 s budget. `mcp_python_bin()`'s `uv sync` runs *before*
  `start`, and `start_ui`'s `npm install` likewise (`up.sh:148-150`), so neither install cost is
  inside a timed window.

  I also diffed all five shared multi-line functions myself rather than the two the finding shows:

  ```
  start()    : identical but the log path and two comment lines
  wait_for() : identical but the log path, four comment lines, 300 vs 120
  down()     : different bodies — kill-process-group vs SIGTERM, .port cleanup, the delegation, two extra log lines
  status()   : different bodies — the skipped-front-door notice, %-20s vs %-16s, the delegation
  restart()  : entirely different — `up` vs a five-branch case over the external process names
  ```

- **Why the grade does not hold**

  Two of the finding's three load-bearing statements fail.

  1. *"the four-repo lane reproduces the failure the single-repo lane fixed, on the two processes
     with the heaviest imports of the eleven it starts"* — false on both halves. Those two are the
     lightest, by the upstream repo's own manifests and by measurement; the heaviest keep the 300
     budget. No failure is reproduced, and none is even predicted once the actual dependency closures
     are read.
  2. *"eight, ~95 lines, in two files; three of the eight have diverged"* — the real duplication is
     `start()` + `wait_for()` (~30 lines, near-identical) plus two one-liners, plus `python_bin()`
     across `processes.sh`/`soak.sh`. `down`, `status` and `restart` are not diverged clones; they
     are different implementations doing different jobs, which the diff above shows line for line.

  What is left standing is a genuine but small simplification finding: ~30 duplicated lines and a
  default that differs between two copies for no stated reason. Worth an extract-to-`lib.sh`; not
  worth a high. (One item the reporter missed cuts the same way — `start_mock_vendor` at `up.sh:130-141`
  inlines a *fourth* copy of the readiness loop with `seq 1 60`, a budget half the one being reported
  as too small, and nobody has hit it.)

  Note the fix's premise is also wrong in the same place: "the reason the number is 300 applies to
  `props`/`rxnpredict` more strongly than to anything `processes.sh` starts". The reason is a ~1 GB
  cold page-in of torch+rdkit+bofire; `props` imports uvicorn.
