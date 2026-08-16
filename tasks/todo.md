# Tranche 4 — `calc`: the physics leaves, the cache and the orchestration stay

Status: **in progress.** mcp side building; Chemclaw3 side not started.

## The architecture, after the 2026-08-16 decision

> **Chemclaw3 keeps orchestration and the cache. Chemclaw3-mcp holds the physics, exposed as
> individually-keyed primitives.**

This replaces the earlier "9 fast tools move, durable jobs stay" split, which measurement showed
could not work. Three findings forced it:

1. **The durable jobs need almost the whole engine.** `reaction.py`, `xtb_scan.py`,
   `conformers.py` and `complexes.py` transitively import `structure`, `xtb_engine`, `xtb_opt`,
   `xtb_thermo`, `xtb_hessian`, `xtb_spec`, `crest_cli`, `anc`. Leaving the jobs here and moving
   the fast tools would have **copied** the engine, not moved it — against D-148, which says
   `connectors.calc` is a wrapper around `science.calc` and "never a second copy of it".
2. **A composite tool cannot be cached by its caller.** `compute_thermochemistry`'s key names the
   geometry the refinement loop *settles on*, which is an output. Measured cost of moving it whole:

   | | cold | repeat |
   |---|---|---|
   | `CCO` | 0.816 s | **0.007 s** |
   | `CC(=O)OCC` | 3.273 s | **0.012 s** |

   It has no top-level cache today — the economy is entirely the nested `xtb.opt`/`xtb.hess`
   entries, which a single remote call would swallow.
3. **`predict_logd` is the same shape and worse**: pyridine 20.603 s → **0.005 s**, acetic acid
   2.610 s → 0.003 s. Its expensive half is a *cached pKa*; the rest is Crippen, pure RDKit.

The resolution for all three is the same and is why the architecture is stated as primitives:
**stop shipping composites, ship their parts.** Chemclaw3 composes, and every part is cached.

## What changes in Chemclaw3-mcp

- **Changes:** the fleet's informal ~20 s expectation. Some primitives are minutes. Duration is
  not the property that fleet promises.
- **Does not change:** no egress at request time, and **no state** — no database, no job records,
  no resumption. Temporal on this side owns durability. Anything on that side that wants to
  persist is a signal it belongs on this side.

## The cross-repo key contract, verified live (2026-08-16)

`Chemclaw3-mcp#5` is merged. With its `calc` server running on 8860 and Chemclaw3 deriving the same
values locally, the identities are **byte-identical**:

| | server | here |
|---|---|---|
| `xtb.sp` key | `xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7` | identical |
| `pka` version | `…/cal-0.28733:-29.3116/base-0.241396:-22.1843/u-1.6:1.0/opt-…` | identical |
| `solubility` version | `esol-delaney@2004/rdkit-2026.3.5/u-0.75` | identical |
| `developability` version | `rdkit-2026.3.5` | identical |
| `structure_id` for `CCO` | `st_739a222f45be0c3a` | identical |

and `calculation_key(tool, args)` returns exactly what the compute tool stamps on its result, checked
on four tools over the wire. **This is the property the cache depends on**: one byte of disagreement
and every lookup misses forever while `calculator_trust` reports a confident `UNCALIBRATED`.

The strings also settle why the key comes back as a structured object rather than a flat string —
`esol-delaney@2004` carries an `@` and `cal-0.28733:-29.3116` carries a `:`, so a client splitting
the flat form on either delimiter would build a key that never matches.

## Steps

- [x] mcp: expose the primitives — `optimize_geometry`, `compute_hessian` (new; multi-MB `.npy`
      blobs must cross the wire), single-point/properties, the scan *step*, and the CREST search
      whole (it cannot be decomposed). `calculation_key` covers every one.
- [ ] mcp: port `reaction`, `xtb_scan`, `conformers`, `complexes`, `crest_cli` engines.
- [ ] Chemclaw3: the twelve `run_cached_*` wrappers move onto `cached_compute` with a remote
      closure — the only wrapper whose callback is already async and returns a plain dict.
- [ ] Chemclaw3: `compute_thermochemistry` becomes remote optimise + remote Hessian + **local**
      RRHO arithmetic, keeping the refinement loop here. `predict_logd` becomes remote `predict_pka`
      + local Crippen.
- [ ] Chemclaw3: the five Temporal activities call remote primitives, each wrapped in
      `durable/heartbeat.py::beating` — a minutes-long blocking call with no heartbeat is an
      activity Temporal declares dead. **This needs no new machinery**: `beating` was extracted for
      exactly this shape ("one opaque call with nothing finer to report than *still running*") from
      the CREST subprocess, the HPC poll and the BoFire fit. A remote computation is the fourth
      instance, and its guarantee — no exit leaves the wrapped work running — is what makes a
      dropped connection safe.
- [ ] Chemclaw3: delete `science/calc`'s engine modules; keep `store`, `postgres_store`,
      `postgres_artifacts`, `calibration`, `uncertainty`, `specs`, `solvents`, `results`, the six
      read/write tools, and every workflow.
- [ ] Delete `run_cached_with_artifacts` — zero production callers, confirmed.
- [ ] No code in this repository may derive a `calc_version`. `calculator_trust` and
      `calculator_outliers` re-derive it locally today (`connectors/calc/server/tools.py:433,451`,
      used at `:480` and `:585`); they must read it from the server. Add a test that no local
      derivation survives — `binary_version()` returns `"absent"` rather than raising, so a pod
      without the binary would produce a valid-looking version matching **zero** ledger rows and
      report a confident `UNCALIBRATED`.
- [ ] `CALCULATION_EPOCH` is the one constant both repos must keep in step.

## The client seam exists; its cost does not

`durable/template_activities.py` already calls `open_connector_specs` from **inside a Temporal
activity** (`:263`, `:391`), so an activity opening its own connector session per invocation is
established practice rather than new machinery.

What it costs is a session setup per calculation, and that is not optional:
`connectors/identity.py` states that "the transport's tasks inherit the context of whoever opened
the connection, so the identity is only truthful if a connection belongs to exactly one turn" —
two concurrent callers over one shared session misattribute each other's calls. So no process-wide
session. Estimated effect on a cache *hit* (the `calculation_key` round trip): ~7 ms → ~50 ms.
Noise against an SCF; measure it rather than assume it once the client is real.

## Verify

`make eval-strict` before and after; a cache hit/miss measurement proving a persisted result is
still never recomputed (D-011) — the whole reason the cache stays; the three regressions above
re-measured against the composed path, which must restore them; `tests/test_qm_persistence.py` and
`test_qm_workflow.py` green, since `qm` is a cache client that must not notice the split; a live
turn against the running server.
