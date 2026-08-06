# D-2026-08-06-the-method-decides-which-solvents-exist — The method decides which solvents exist, and it can be asked

**Status:** accepted · **Date:** 2026-08-06

## Context

A chemist asked `compare_solvents` for "2-MeTHF" — among the most common process solvents there is.
The model passed the name through faithfully, the turn reported the job running, and ~30 s later an
activity died deep inside the durable path on tblite's `String value for epsilon was not found among
database of solvents` (live full-stack pass, 2026-08-04).

Nothing was wrong with the durability. GFN2-xTB's ALPB is an *implicit* solvation model with a fixed
parameter set: a solvent it has no parameters for cannot be approximated, only failed on. The
information needed to refuse the call was available before any workflow started, and the only thing
that had it was the calculation itself.

There was also a list already — `xtb_engine.COMMON_SOLVENTS`, thirteen names quoted in the error
message for an unrecognized solvent, whose comment called it "a curated list of the solvents process
chemistry actually asks about". It omitted `dmf`, `dioxane`, `benzene` and `nitromethane`. All four
are supported. All four are ordinary. A hand-written list drifts in both directions and neither
direction announces itself.

## Decision

**The set of solvent names the method accepts is a measured constant, and a launch-time precondition
refuses a durable job that names one outside it.**

Three parts, and the order matters:

- `chemclaw.science.calc.solvents` holds `ALPB_SOLVENTS`, every name tblite accepts for
  `alpb-solvation`, **obtained by probing** the solvent-name table compiled into `_libtblite`
  against a live `Calculator`. `SUGGESTED_SOLVENTS` — the shortlist a message quotes — is a strict
  subset of it, which is a shape that cannot drift.
- Every calc job that takes a solvent declares
  `precondition: chemclaw.science.calc.solvents:require_supported_solvents`. `prepare_job_launch`
  runs it before any workflow starts, on both launchers (the generated tool and the template
  workflow's job step, D-168).
- `xtb_engine` quotes the same shortlist, so the in-process path and the durable path cannot
  disagree about what the method supports.

The module imports nothing but the standard library, because the precondition is resolved by
importing it **in the chat service's process** — the boundary D-118 exists to hold.

## Why it was measured rather than recalled

tblite has two tables and rejects a name from each differently, which a list written from memory
would flatten:

| rejected name | message |
|---|---|
| `2-methyltetrahydrofuran`, `mtbe` | `String value for epsilon was not found among database of solvents` |
| `heptane`, `cyclohexane`, `xylene` | `No ALPB/GBSA parameters found for the method/solvent` |

The first is absent from the dielectric database. The second is *present* there and has no Born
parameters for the Hamiltonian. Only the intersection runs — 42 accepted spellings, including
aliases a chemist and a model both write (`h2o`, `mecn`, `nhexane`, and tblite's own
`dichlormethane`). The set is identical for GFN1-xTB and GFN2-xTB, so it is one constant rather than
a per-method map with one entry written twice.

`tests/test_solvents.py` re-derives it against the installed tblite in **both** directions: no
listed name the library refuses (which would put the 30-second failure back with the guard
apparently in place), and no accepted name missing (which would refuse a calculation the method can
do, with no error anywhere to trace it to).

## Consequences

- "2-MeTHF" now fails in the same turn, naming `thf` and `tetrahydrofuran` as the closest supported
  spellings — which is usually the right substitution anyway. `difflib` supplies the suggestion and
  stays silent when nothing is close: proposing `phenol` for `mtbe` would be worse than proposing
  nothing.
- A tblite upgrade that adds or drops a solvent fails a test instead of surfacing as a wrong
  refusal, months later, as "the system says we can't run that".
- The check is duck-typed over the params object because five specs carry a solvent in two shapes
  (`solvents: list[str]` and `solvent: str | None`) and a manifest names one precondition per job.
  A test derives the five from the manifests and their params models, so a sixth solvent-taking job
  without the guard fails there rather than in a live run.
- Gas phase is spelled `solvent: null` and passes untouched — it is not a solvent.
