# Verification: `servers/calc` — design and simplification (reachability lens)

Source: `tasks/audit-2026-08-16/findings/round1/mcp-calc--design.md`.
In scope: the two findings marked **high**. The other five are medium/low and were not examined.

Working tree at `/workspace/chemclaw3-mcp` was clean (`git status --porcelain` empty, HEAD
`9217011`), so no mutation experiment is in play and the pristine copy was not needed.

All reproductions below were run with `uv run --project servers/calc` inside
`/workspace/chemclaw3-mcp`. To make `resolve_backend()` answer `"xtb"` under the shipped `auto`
default I put a stub on PATH at `/tmp/verif/fakebin/xtb` that prints
`* xtb version 6.6.1 (conda-forge) compiled`, and wrapped `xtb_cli.run` with a recorder.

---

## `engine` is in every xTB key but the single point, the properties and the Fukui paths never dispatch on it

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**The mechanism reproduces exactly.** With the stub binary on PATH:

```
$ PATH=/tmp/verif/fakebin:$PATH uv run --project servers/calc python /tmp/verif/repro2.py
resolve_backend: xtb | binary: /tmp/verif/fakebin/xtb | ver: 6.6.1
compute_xtb_energy  version: GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2
                    key    : xtb.sp@GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7
                    energy : -11.393351662348477
  xtb_cli.run calls: []
electronic_props    version: GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2 | run calls: []
site_reactivity     version: GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2 | run calls: []
```

and without it (same process, `auto` → `tblite`, then the same run with the pin):

```
--- no binary (auto->tblite) ---
version: GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2
energy : -11.393351662348477
--- no binary, PINNED CHEMCLAW_XTB_ENGINE=xtb ---
version: GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2
energy : -11.393351662348477
```

Same float to the last bit, three different `calc_version` strings. The reporter's numbers are
reproducible and their code reading is right.

I also closed the "was the binary path ever wired" question independently:

```
$ grep -rn "xtb_cli.run(" servers/calc/src/
engine/xtb_hessian.py:148:  outcome = xtb_cli.run(structure, task="hess", ...)
engine/xtb_opt.py:390:      outcome = xtb_cli.run(structure, task="opt", ...)
```

Two call sites, both guarded on `engine == "xtb"` (`xtb_hessian.py:147`, `xtb_opt.py:182`).
Nothing ever calls `task="sp"`, so `_BINARY_TASKS = {"opt", "hess"}` is the correct set and
`compute_hessian` / free `optimize_geometry` are *not* affected — the location list is accurate.

The cited test guard is as weak as claimed: `test_calc_version.py:140-146` asserts only
`"GFN2-xTB" in version`, `"tblite-" in version`, `"auto" not in version`.

**Reachability — this is where the finding weakens.** The trigger needs `resolve_backend()` to
answer `"xtb"`, which needs either the binary on PATH under `auto`, or `CHEMCLAW_XTB_ENGINE=xtb`.
Neither exists in any shipped artifact in either repository:

- `servers/calc/Containerfile` installs no binary and sets no such variable — it says so in its own
  header comment, and the build stage is a `pip wheel` of two Python packages only.
- `grep -rn "xtb_engine|XTB_ENGINE"` across `/workspace/chemclaw3-mcp` returns only `config.py:49`
  (the field, defaulting to `"auto"`), `xtb_spec.py:51` (the read) and prose. No YAML sets it.
- `manifests/calc/` holds only `connector.yaml`; `servers/calc/deploy/` holds only a NetworkPolicy.
  Neither carries an env block.
- On the caller side, `src/chemclaw/connectors/calc/remote.py` sends `{"tool": ..., "arguments":
  ...}` and per-tool argument dicts. **No tool on the server takes a backend argument**, so no MCP
  request, no agent, and no durable job can select `xtb`. `grep -rn "XTB_ENGINE"` over
  `/home/user/Chemclaw3` returns nothing, and `deploy/Containerfile:58` records that the binaries
  were deliberately removed from the backend image too.

So request-level reachability is zero. The trigger is an *operator* rebuild of the calc image — a
configuration the Containerfile documents as supported, which is why this is not REFUTED.

**Consequence — the alarming half does not hold.** The finding says the defect produces "a
partitioned `predictions` ledger, which `calculator_trust` reports as `UNCALIBRATED`". It does not,
from any of the three cited paths:

- `_log_prediction` in `src/chemclaw/connectors/calc/server/tools.py` has exactly two callers,
  `:675` (`"solubility"`) and `:712` (`"pka"`). `compute_xtb_energy`, `compute_electronic_properties`
  and `predict_site_reactivity` write **no** ledger row at all.
- `calculator_trust` accepts only those two names — `_CALIBRATED` at `tools.py:421-424` — and raises
  for anything else.
- `predict_solubility` is ESOL/RDKit and its version contains no engine. `predict_pka`'s version
  *does* carry an engine, but via `relaxation_spec()`, whose `optimize_structure` really does
  dispatch on it; that is the item the reporter themself filed under "checked and found sound",
  not this defect.

The cache half is real but narrower than "permanently partitioned … recomputed once per pod
flavour". A partition needs a heterogeneous fleet; one Deployment runs one image, so the steady
state is a single `calc_version` and the actual cost is a **one-time invalidation** of every
`xtb.sp` / `xtb.properties` / `xtb.fukui` row on the day the binary is added or its version bumps —
sub-second recomputes producing bit-identical numbers. Critically, the error is over-specification,
never under-specification: I could construct no pair of genuinely different calculations that
collide on one key, so this can only cause spurious **misses**, never a false hit. No chemist is
ever shown a wrong number, a wrong safety verdict or a wrong impurity figure by this path; the value
returned is the same float either way.

### Why

Mechanism confirmed, code reading confirmed, reproduction confirmed. Two things stop it being a
"high":

1. The stated worst consequence — a broken calibration ledger reporting a confident `UNCALIBRATED` —
   is not produced by any of the three cited tools, because none of them logs a prediction. That
   clause was carried over from the pKa argument and does not apply here.
2. The trigger is unreachable from any caller and from every shipped deployment; it requires a
   deliberate image change, and even then the harm is redundant CPU plus a provenance string that
   names the wrong program.

What survives is real and worth fixing: the module's own stated rule ("name every program whose
output survives into the stored payload, and no program that does not run") is violated, the
documented upgrade path ("add the binary and the version string moves to record that it did") is
false for three tools, and the test that appears to guard it does not. That is a **medium**.

---

## The frozen-atom fallback keeps `engine="xtb"`, so every `scan_point` on a binary pod is keyed as the wrong program

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Reproduced with the same stub binary, calling the real tool functions rather than internals:

```
$ PATH=/tmp/verif/fakebin:$PATH uv run --project servers/calc python /tmp/verif/repro4.py
resolve_backend: xtb
relax(frozen) engine      : xtb
relax(frozen) calc_version: GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2
relax(frozen) calc_key    : xtb.opt@GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:e3eb8399f7b354a7
xtb_cli.run calls         : []
scan_point engine         : xtb | version: GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2
xtb_cli.run calls         : []
```

`xtb_opt.py:388-389` is exactly as described — `if spec.frozen_atoms: return
_optimize_with_library(spec, structure)`, with `spec` unmodified — and `_optimize_with_library`
stamps `calc_version=spec.calc_version()` (`:352`) and `engine=spec.engine` (`:358`).
`scan.scan_point_inputs:129` always passes `frozen_atoms=tuple(atoms)`, so every scan point on such
a pod takes the branch. The reporter's reading and reproduction are correct.

**Reachability** is the identical gate as the previous finding, with the identical answer: no
shipped image installs the binary, no manifest or Helm value sets `CHEMCLAW_XTB_ENGINE`, and the
`relax_structure` / `scan_point` wire arguments (`src/chemclaw/connectors/calc/compose.py:329-336`)
carry `structure`, `atoms`, `value`, `solvent` — nothing that selects a backend. Operator-only.

**Consequence — accurate in the payload, overstated downstream.** I traced where the mislabelled
`engine` actually surfaces:

- `optimize_geometry`, the one agent-facing tool whose `OptimizationSummary` carries `engine`
  (`xtb_opt.py:127`), builds its spec through `optimization_inputs`, which sets **no** frozen atoms.
  It therefore dispatches correctly to the binary and is unaffected.
- The scan path's chemist-facing model is `ScanResult` (`connectors/calc/compose.py:344-358`). It
  carries `method`, `solvent`, the points and the minimum structure — **no `engine` field**. The
  wrong label is dropped before anything renders it.
- `grep -n "engine"` over `connectors/calc/{results,activities,workflows}.py` returns nothing.

So "a scan profile's geometries are attributed to a program that was never invoked" is true of the
cached payload and false of anything a reader sees. I also checked the false-hit direction and could
not construct one: `frozen_atoms` is in `params`, and the binary path never runs with a non-empty
`frozen_atoms`, so a frozen tblite result cannot collide with a genuine ANCopt result. Again: only
spurious misses.

One point in the finding's favour that I verified and that the reporter understated: the *energies*
are safe. Both branches evaluate energy through `_energy_and_gradient` (tblite) regardless of which
optimizer moved the atoms, so a scan profile mixing a binary-relaxed reference with tblite-relaxed
points differs only by the small geometry difference, not by a method change.

### Why

The mechanism, the code citation and the stated payload-level consequence all hold — this is the
sharper of the two findings, and the fix it proposes (override `for_structure` in `OptSpec`) is the
right shape, matching how the open-shell case is already handled at `xtb_spec.py:133-135`.

What does not hold is **high**. The trigger cannot be produced by any caller or any shipped
deployment; it needs an operator to rebuild the image. If triggered, no wrong number and no wrong
geometry is produced — the geometry is a correct constrained tblite relaxation — and the wrong
`engine` label never reaches a chemist, because the only surface that renders it (`optimize_geometry`
→ `OptimizationSummary`) is on the free-optimization path that dispatches correctly. The residual
harm is a false provenance string in the cache and a wasted cache partition on binary install.
That is a **medium**: a genuine broken invariant with a cheap correct fix, not an operational defect.
