# Verdicts — `infra-deploy-ci--design.md`, reachability lens

Scope: the three findings marked **high**. No **critical** findings in the file. Medium/low ignored.

Working-tree hygiene: no source file was mutated. The only writes were `/tmp/no_tblite_probe.py` and
`/tmp/audit-live/` (both removed). `git status --porcelain` outside `tasks/audit-*` is empty.

---

## `tblite` is a production dependency with zero runtime importers — 17.5 MB in every image

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  ```
  $ grep -rn "^\s*\(from\|import\) tblite" src/          → 0 hits (all 20 src/ hits are prose)
  $ du -sh .venv/lib/python3.11/site-packages/tblite*
  7.6M tblite · 32K tblite-0.7.0.dist-info · 9.9M tblite.libs      (17.5 MB confirmed)
  $ uv run python -c "<walk distributions, find requirers of tblite>"
  chemclaw -> tblite>=0.7.0                                        (sole requirer confirmed)
  ```

  Independent import-blocker probe (`/tmp/no_tblite_probe.py`: a `meta_path` finder that raises on
  `tblite`, then `pkgutil.walk_packages` + `importlib.import_module` over every module under
  `chemclaw.`):

  ```
  modules needing tblite: []
  other import errors (unrelated): 0
  ```

  Every module in the tree imports cleanly with `tblite` made unimportable. Trigger confirmed
  reachable: `deploy/Containerfile:106` is `uv sync --frozen --no-dev`, and
  `uv export --frozen --no-dev` lists `tblite==0.7.0` in the 213-package production closure.

  Nothing upstream blocks the proposed fix: no test asserts `tblite` is a `[project]` dependency
  (`tests/test_packaging.py` says nothing about dependency placement), CI is `uv sync --locked`
  (dev group included), so `tests/test_solvents.py` keeps its library.

  The reporter also missed a piece of in-tree evidence that *strengthens* the mechanism:
  `tests/test_third_party_layering.py:148` declares `"tblite": "xtb"` as a policed stack with **no**
  allowed `(package, "xtb")` row — i.e. a test already fails the build if any `src/` package imports
  it. So the dependency is not merely unused, it is *forbidden* to be used from this tree.

- **Why**: every factual claim reproduces. What does not hold is the **severity**. The consequence
  is 17.5 MB of dead disk and CVE surface in an image whose production closure already carries
  `torch==2.13.0` (1.1 GB installed) and `rdkit` (74 MB) as first-class dependencies — tblite is
  ~1.5% of torch alone. There is no functional failure, no wrong answer to a chemist, no runtime
  path affected, and the fix is behaviour-preserving. That is a real, cheap, worth-doing cleanup
  plus one false docstring (`connectors/calc/worker.py:5`, mangled and now untrue) — **medium**, not
  high. "High" would be justified if the dead dependency could change an answer or break a start-up;
  it cannot, because nothing can legally import it.

---

## `make live-e2e-full-stack-down` stops nothing and exits 0 when the e2e run directory is absent

- **Verdict**: OVERSTATED
- **Severity I would assign**: low
- **What I did**: reproduced the control flow exactly, under `bash -x`:

  ```
  $ mkdir -p /tmp/audit-live/run && echo 2147480000 > /tmp/audit-live/run/api.pid
  $ CHEMCLAW_LIVE_DIR=/tmp/audit-live bash -x infra/live/e2e-full-stack/up.sh down
  + readonly RUN_DIR=/tmp/audit-live/e2e/run
  + log 'stopping Chemclaw3_ui'
  + log 'nothing running'
  [e2e] nothing running
  + return                       <-- returns before `bash …/processes.sh down`
  rc=0
  $ ls /tmp/audit-live/run/  →  api.pid   (untouched)
  ```

  `processes.sh` is never invoked. The divergence with `status()` is real
  (`up.sh:239` has the same guard *without* `return`, `up.sh:221` has it *with*).

  Then I traced reachability back to the entry points:

  - `up()` does `mkdir -p "$RUN_DIR"` at `up.sh:164`, **before** it starts anything and before
    `bash "$REPO_ROOT/infra/live/processes.sh" up` at `:212`. So within the e2e lane's own lifecycle
    the guard can never fire while this repo's processes are up.
  - `down()` removes pidfiles (`rm -f "$pidfile"`) but never the directory, so `$LIVE_DIR/e2e/run`
    persists once created — repeat `down`s take the full path.
  - No target or script anywhere deletes `$LIVE_DIR` or `$LIVE_DIR/e2e`
    (`grep -rn "rm -rf" infra/live/ Makefile` → only a `mktemp -d` trap at `Makefile:221`).
  - `processes.sh`'s **own** `down()` (`:205`) carries the identical early-return guard on its own
    `RUN_DIR`, so calling it unconditionally — the proposed fix — is a no-op in the absent case.
    The fix is safe; the current code is just not reachable the way the finding implies.

- **Why**: the mechanism is exactly as described and the fix is correct, so the DRY/consistency
  half stands. What does not hold is the reachability weight. The only ways to produce the trigger
  are (a) a human typing `make live-up` and then `make live-e2e-full-stack-down` — mixing two lanes,
  which no script, target or documented flow does — or (b) hand-deleting `$LIVE_DIR/e2e`. Neither is
  producible by "a real caller"; both are an operator using the wrong verb. The consequence is also
  softer than "reports success": the command prints `[e2e] nothing running`, which is a visible (if
  misleading) statement, and the recovery is `make live-down` in the same shell. This is a
  developer-laptop harness under `infra/live/` — never executed in CI, never in an image's runtime
  path, no data at risk. **Low.**

---

## The live lane's process harness is cloned across three scripts, and the clone dropped the fix

- **Verdict**: OVERSTATED
- **Severity I would assign**: low
- **What I did**: confirmed the duplication, then attacked the consequence.

  Duplication is real:

  ```
  $ diff <(sed -n '/^wait_for() {/,/^}/p' infra/live/processes.sh) \
         <(sed -n '/^wait_for() {/,/^}/p' infra/live/e2e-full-stack/up.sh)
  2c2
  <   local name="$1" url="$2" attempts="${3:-300}"
  ---
  >   local name="$1" url="$2" attempts="${3:-120}"
  (plus two dropped comments and the e2e- log prefix)
  ```

  `python_bin()` is byte-identical at `processes.sh:79` and `soak.sh:53`. `start()` differs only in
  the log path and two comments. All of that stands.

  The consequence does not. The finding's load-bearing claim is that the 120 default hits *"the two
  processes with the heaviest imports of the eleven it starts"* — `props` and `rxnpredict`,
  asserted as "RDKit-class import cost". I checked the actual servers upstream:

  - `Chemclaw3-mcp/servers/props/pyproject.toml` — dependencies are `mcp-server-kit` and `uvicorn`,
    with the comment *"Deliberately short. The engine is arithmetic over a CSV … no CoolProp, no
    scientific stack, no HTTP client."* `props` imports **no** scientific stack at all.
  - `Chemclaw3-mcp/servers/rxnpredict/pyproject.toml` — core is `mcp-server-kit`,
    `pydantic-settings`, `rdkit`, `uvicorn`. `torch`/`transformers` are **optional extras**, and
    `up.sh:86-95` starts it with `fake_a`/`fake_c` precisely because those are *"a deterministic tool
    surface with no model weights and no checkpoint download"*.

  And the processes the 300 s budget was actually measured on are **not** governed by the 120:

  ```
  $ grep -n "wait_for " infra/live/e2e-full-stack/up.sh
  82: props · 94: rxnpredict · 121: mock-hpc-eln · 155: ui-bff
  ```

  This repo's connectors and four Temporal workers — the ones loading `torch, rdkit, bofire`, the
  exact set `processes.sh`'s comment names as paging ~1 GB — are started at `up.sh:212` by
  `bash "$REPO_ROOT/infra/live/processes.sh" up`, a **subprocess that uses its own `wait_for` with
  the 300 default**. The fix was not dropped for a single process it was measured on.

  Scale check on the remaining candidate:

  ```
  $ uv run python -X importtime -c "import rdkit.Chem"      → 68 ms cumulative (warm)
  $ uv run python -c "import torch, rdkit.Chem, bofire"     → 1.7 s (warm)
  ```

  rdkit is 74 MB of the ~1 GB the 300 s comment describes; `rxnpredict` (rdkit-only, fake models) is
  a small fraction of the workload that justified the number, and `props` is none of it.

  Two smaller sub-claims: the `log` stdout/stderr divergence is real (`processes.sh:32` stdout,
  `up.sh:30` stderr), but the asserted "same hazard exists in `processes.sh`'s `connector_urls`" does
  not — `connector_urls()` runs `"$1" -c '…'` and calls no `log`, so nothing corrupts its
  substitution today.

- **Why**: the duplication is genuine and the extraction is a reasonable cleanup, so this survives as
  a design finding. But the alarm attached to it — *"the four-repo lane reproduces the failure the
  single-repo lane fixed"* — is unsupported and factually wrong about both named processes: the
  heavy-import processes keep the 300 s budget via `processes.sh`, `props` has no scientific stack,
  and `rxnpredict` is started with fakes by design. No cold-cache measurement was taken for either
  server; the "heaviest imports of the eleven" claim is asserted, and upstream's own manifests
  contradict it. Strip that and what remains is ~95 lines of duplicated bash in a dev-only lane that
  CI never runs: **low**.

---

### Summary

| Finding | Filed | Verdict | Assigned |
| --- | --- | --- | --- |
| `tblite` production dependency with no importer | high | OVERSTATED | medium |
| `live-e2e-full-stack-down` early return | high | OVERSTATED | low |
| Live-lane harness cloned; 120 vs 300 | high | OVERSTATED | low |

All three mechanisms are real and on disk — none is fabricated, and each fix is behaviour-preserving
and worth taking. What none of the three has is the consequence weight the **high** label asserts:
one is dead bytes in an image that already ships torch, one needs an operator to type the wrong
verb in a laptop-only harness, and one's alarming half is contradicted by the upstream manifests of
the two servers it names.
