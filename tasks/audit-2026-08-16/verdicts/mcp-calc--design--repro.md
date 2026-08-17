# Repro verdicts — `mcp-calc--design.md`

Lens: *does it actually reproduce?* In scope: the two findings marked **high**. The remaining five
are medium/low and were not examined.

Method note: I did not run any of the reporter's scripts (`/tmp/calcaudit/*`) and did not use their
transcripts as evidence. My whole scaffolding is a **fake `xtb` on `PATH`** at
`/tmp/repro/fakebin/xtb` that answers `--version` with a real xtb banner and, for *any other*
invocation, appends its argv to `/tmp/repro/invocations.log` and exits 1. That inverts the burden:
if the binary path were ever taken, the run would fail loudly and the log would be non-empty. The
log stayed empty through every run below. Scripts are `/tmp/repro/f1.py`, `f1b.py`, `f1c.py`,
`f2.py`, `f2b.py`, `guard.py`. Working tree of `/workspace/chemclaw3-mcp` was clean
(`git status --porcelain` empty, no `MUTANT` markers), at `9217011`.

---

## `engine` is in every xTB key but the single point, the properties and the Fukui paths never dispatch on it

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

Ran the same three tools in two otherwise identical processes, differing only in whether the fake
binary is on `PATH` (`uv run python /tmp/repro/f1b.py`, once bare and once under
`PATH=/tmp/repro/fakebin:$PATH`):

```
=== POD A: no binary ===                  === POD B: binary installed (auto default) ===
resolve_backend   tblite                  resolve_backend   xtb
sp_energy   -11.393351662348477           sp_energy   -11.393351662348477
sp_version  GFN2-xTB+tblite+tblite-0.7.0/ sp_version  GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/
            rdkit-2026.3.5/h2                         rdkit-2026.3.5/h2
sp_key      xtb.sp@…tblite…:389b625b3220108a:b41312b0cdc59ab7
            xtb.sp@…xtb-6.6.1…:389b625b3220108a:b41312b0cdc59ab7
props_version / fukui_version: same split, same two strings
xtb_cli_run_calls   []                    xtb_cli_run_calls   []
```

and `cat /tmp/repro/invocations.log` → **empty**: the fake binary was asked `--version` and nothing
else. The energies are bit-identical (`-11.393351662348477` on both — the reporter's number to the
last digit); the input and params hashes are identical (`389b625b3220108a:b41312b0cdc59ab7`); only
the `calc_version` segment moves.

Reachability needed no environment pin: `CHEMCLAW_XTB_ENGINE` was unset, the default is `auto`
(`engine/config.py:49`), and `resolve_backend()` flipped to `"xtb"` purely because a binary appeared
on `PATH`.

I also checked the *ledger* half of the claimed consequence, which the reporter asserted but did not
measure (`/tmp/repro/f1c.py`, `predict_pka("CC(=O)O")`):

```
pod A  pka=6.512636701266935  …/opt-GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2
pod B  pka=6.512636701266935  …/opt-GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2
cli_calls: []   (both)
```

`science/calc/calibration.py:52` keys the ledger on `(calc_type, calc_version, input_hash)` and
`calibration_for` requires the version verbatim, so a bit-identical pKa is filed under two primary
keys and `calculator_trust("pka")` on the pod that merely installed the binary sees zero reconciled
rows — the `UNCALIBRATED` state at `calibration.py:158`.

Finally, the reporter's claim that the named guard is vacuous — verified directly rather than by
reading (`/tmp/repro/guard.py` applies the exact predicate of
`test_calc_version.py:118` to the strings produced on pod B):

```
compute_xtb_energy    version=GFN2-xTB+xtb+xtb-6.6.1/…   guard passes = True   xtb_cli.run calls = 0
compute_electronic_properties  …                          guard passes = True   xtb_cli.run calls = 0
predict_site_reactivity        …                          guard passes = True   xtb_cli.run calls = 0
```

(Running the test file itself under the fake binary is not a valid check of vacuity — it fails on
its `optimize_geometry` case, because *that* path really does shell out and my fake cannot compute.
That failure is about my fake, not about the guard, which is why I applied the predicate directly.)

### Why

Every cited symbol and line is real and current: `xtb_spec.py:98` is the `engine` field, `:153` the
`calc_version` return, `xtb.py:49` `_energy`, `xtb_props.py:170` `compute_properties`, `:222`
`compute_fukui`, `tools.py:189/263/295` the three tool defs. All three engine functions compute
`resolved = spec.for_structure(structure)`, use `resolved.calc_version()` / `resolved.cache_key()`,
and then call `gfn2_energy` / `run_singlepoint` — tblite — with no branch on `resolved.engine`.

The finding's arithmetic also checks out: `grep` for callers of `xtb_cli.run` returns exactly two,
`xtb_opt.py:390` (`task="opt"`) and `xtb_hessian.py:148` (`task="hess"`), and the latter *is* guarded
by `if spec.for_structure(structure).engine == "xtb"`. So two of five spec consumers dispatch and
three do not, exactly as claimed, while `CliTask` still declares `"sp"` and `_task_flags` still
implements it.

I looked for something upstream that would kill this and found nothing: no caller pins the engine,
the `auto` default resolves on `shutil.which`, and the shipped image's lack of a binary is a fact
about the image rather than a control in the code.

What keeps it at high rather than critical: no number is wrong — the physics is correct tblite output
in both cases. The harm is provenance and identity. What pushes it *up* to high rather than medium is
that the same pod flip silently resets the pKa calibration ledger, and that the pKa version segment
naming the optimizer is dishonest on the acid branch in a stronger sense than the reporter noticed —
there, no optimizer ran *at all* (`cli_calls: []`, and acetic acid never reaches
`optimize_structure`), yet the string names `xtb-6.6.1`.

---

## The frozen-atom fallback keeps `engine="xtb"`, so every `scan_point` on a binary pod is keyed as the wrong program

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

`uv run python /tmp/repro/f2.py`, again once bare and once with the fake binary on `PATH`. It embeds
`CCO`, calls `relax_structure(s, None, [0])` and then `scan_point(s, [0,1], 1.6)`:

```
=== POD A (no binary) ===              === POD B (binary installed) ===
backend                tblite          backend                xtb
relax_frozen_engine    tblite          relax_frozen_engine    xtb
relax_frozen_version   …+tblite+…      relax_frozen_version   GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/…
relax_frozen_key       xtb.opt@…tblite…:389b625b3220108a:e3eb8399f7b354a7
                       xtb.opt@…xtb-6.6.1…:389b625b3220108a:e3eb8399f7b354a7
relax_frozen_energy    -11.39432303541285   /   -11.394323035412848
structure_id           st_91f28f73539f29c8  /   st_91f28f73539f29c8   (identical)
scan_engine            tblite          scan_engine            xtb
scan_version           …+tblite+…      scan_version           …+xtb+xtb-6.6.1/…
xtb_cli_run_calls      []              xtb_cli_run_calls      []
```

`/tmp/repro/invocations.log` empty again: on pod B the fake binary was never executed for a
calculation, so the Cartesian L-BFGS-B path produced both geometries — identical `structure_id` to
pod A, energies agreeing to 1e-15 — while `OptimizationResult.engine` says `xtb` and `calc_version`
names `xtb-6.6.1`.

Line numbers verified: `xtb_opt.py:388-389` is exactly
`if spec.frozen_atoms: return _optimize_with_library(spec, structure)`; `:181-182` is the
`resolved.engine == "xtb"` dispatch above it; `:96-98` is the `engine` field and the comment the
finding quotes; `scan.py:129` is `OptSpec(solvent=solvent, frozen_atoms=tuple(atoms))`, so *every*
scan point takes the branch; `tools.py:559`/`:672` are the two tool defs.

### Why

The mechanism is a two-line read and the measurement matches it: `optimize_structure` dispatches on
the resolved spec, `_optimize_with_binary` immediately hands a **still-`engine="xtb"`** spec to
`_optimize_with_library`, and that function stamps `engine=spec.engine` and
`calc_version=spec.calc_version()` from it. The contrast the finding draws with the open-shell case
is real: `xtb_spec.py:133-135` rewrites the spec in `for_structure`, so dispatch, version and key
cannot disagree there; the frozen-atom fallback was written at the dispatch site and the key never
learned about it. The claimed consequence — a free ANCopt relaxation and a constrained tblite
relaxation stamped with the same `calc_version` on the same pod — follows directly, and it inverts
the documented reason the `engine` field exists.

No answer-substitution is possible (the `frozen_atoms` tuple is in `params`, so free and frozen keys
differ in their params hash), which is why I would not call this critical. High is right: it fires on
*every point of every relaxed scan* on such a pod, not on a corner case.

**One thing the reporter missed, which makes it slightly worse.** The frozen fallback jumps *below*
`optimize_structure`'s GFN-FF refusal, so on a binary pod a constrained GFN-FF relaxation leaks
tblite's raw error — the exact string the code at `xtb_opt.py:184-192` says it exists to avoid
("Named here rather than surfacing tblite's own …"). `/tmp/repro/f2b.py`:

```
which(xtb): /tmp/repro/fakebin/xtb
spec.engine = xtb
raised TBLiteValueError: Method 'GFN-FF' is not available for this calculator
```

The proposed fix (overriding `for_structure` in `OptSpec`) closes this too, since the GFN-FF branch
would then be reached with `engine == "tblite"`.
