# D-2026-08-26-a-sampler-nobody-ships-is-a-refusal-with-a-manual — CREST ships in the `calc` image, and three of its four searches were broken

**Status:** accepted · **Date:** 2026-08-26 · Extends
`D-2026-08-16-the-physics-leaves-the-cache-stays`, which moved the sampling primitives to
`Chemclaw3-mcp`'s `servers/calc` and left the binary they need out of the image.

## Context

Four CREST searches — `conformers`, `tautomers`, `protomers`, `deprotomers` — plus the `--nci`
binding-mode search have been exposed as primitives since the split, with a Chemclaw3 composite over
each: `conformer_ensemble`, `refined_ensemble`, `ensemble_property`, `interaction`, and the
`sample_conformers` and `compute_interaction_energy` durable jobs. All of it was unreachable. The
`crest` binary is compiled Fortran distributed through conda-forge rather than PyPI, so
`pyproject.toml` cannot express it, and the image shipped without it.

Both repositories recorded that state honestly and treated it as parity: "absent from Chemclaw3's
own environment too, so nothing that previously worked has stopped working". That is true and it is
not the whole cost. **A capability that refuses everywhere is never exercised anywhere**, and the
tests that covered it could only assert the refusal — `test_a_crest_search_refuses_by_name_when_the_binary_is_absent`
is a test of `shutil.which` returning None.

## What driving the real binary found

`crest` 3.0.2 and `xtb` 6.7.1 installed from conda-forge, driven through the shipped `crest_cli` on
phenol. Three of the four searches failed on the first call:

| search | result |
| --- | --- |
| `deprotomers` | `ValidationError: 12 positions for 13 elements` — **had never returned an ensemble** |
| `protomers` | `CliError: wrote no ensemble file` — the table named `protomers.xyz`; CREST writes `protonated.xyz` |
| `tautomers` | worked, and labelled all four members with the *input* SMILES, so a keto tautomer came back claiming to be phenol |
| `conformers` | worked |

Three distinct causes, each invisible without the binary:

1. **The parser inherited the template's elements.** `_from_xyz` is `xtb_cli`'s, where it is right —
   xtb echoes the same atoms in the same order, so trusting the template makes a mismatch loud.
   CREST does neither: `--protonate` returns one atom more, `--deprotonate` one fewer, and all three
   protonation modes **presort the input so every hydrogen is written last**. Even at equal counts
   the template's order would have relabelled the atoms of a molecule resorted underneath it.
2. **The charge was the input's.** A deprotomer ensemble came back at charge 0. The members feed
   `relax_structure` and `compute_hessian` on the caller's side, so this is not a labelling error —
   it is a converged energy for a species that does not exist, reported as the anion's.
3. **A fallback made a wrong answer available.** `crest_conformers.xyz` stood behind all three
   protonation searches. It holds the *input* molecule's conformers, so a run that wrote no ensemble
   would have returned the neutral species with a shifted charge stamped on it — no error anywhere.

## Decision

**Ship the binaries.** `servers/calc/Containerfile` gains a `sampling` build stage that installs
pinned `crest` and `xtb` from conda-forge with `micromamba` into a self-contained prefix, which the
final stage copies and puts on `PATH`. Verified by building it: both run in the final
`python:3.11-slim` image with no `LD_LIBRARY_PATH` (conda's RPATH is `$ORIGIN/../lib`), for +270 MB.

Pinned, because `crest --version` and `xtb --version` are both interpolated into `calc_version`: an
unpinned rebuild would silently re-key every cached calculation.

**`CHEMCLAW_XTB_ENGINE=tblite` is pinned in the image, and that is the second half of the
decision.** `auto` resolves to the `xtb` binary the moment one is on `PATH`, and the backend is
interpolated into `calc_version` — measured, `opt-GFN2-xTB+tblite-0.7.0/…` against
`opt-GFN2-xTB+xtb-6.7.1/tblite-0.7.0/…`. Shipping the binary as the default would therefore have
done three things on the day the image deployed, none of which announces itself: every row in
`calculation_results` misses forever; every reconciled residual in the calibration ledger becomes
unreachable, because `predictions` is keyed on `calc_version` and read with an exact predicate, so
`calculator_trust("pka")` reports a confident `UNCALIBRATED` at n=0; and `predict_pka`'s base branch
relaxes through the new backend while its slope and intercept were fitted through the old one. The
binary is therefore *available*, not *active* — one environment variable for a deployment that wants
ANCopt and has decided to recompute. CREST needs none of this: it carries its own GFN
implementation, which is why the capability half of this ADR is unaffected by the pin.

`CHEMCLAW_CREST_THREADS=4` is set in the image. The three `*_NUM_THREADS=1` variables bind the
in-process stack and do **not** reach the sampler — `crest_cli` scrubs the environment to four
allow-listed variables — so without this CREST's OpenMP sizes itself from `/proc/cpuinfo`, which is
the node's core count and not the container's limit.

**Fix the three defects**, in `crest_cli`:

- parse elements from the ensemble file rather than the template;
- shift the charge per search (`+1` protomers, `-1` deprotomers) and carry the multiplicity through
  untouched, because a proton is a nucleus without electrons;
- one output filename per search and **no fallback** — a missing file is an error;
- perceive each member's SMILES from its geometry for the constitution-changing searches, and keep
  the input's for a conformer or binding-mode search, which genuinely are the same molecule.

**Perception is best-effort and never a guess.** `chem.perceive_smiles` infers bond orders from
interatomic distances plus the *known* charge (`rdDetermineBonds`), 4 ms on phenol's anion, bounded
by `crest_perceive_max_atoms` because the assignment is combinatorial. On any failure the member
travels with no label. Two properties of what it returns are worth stating: it answers `[O-]c1ccccc1`
for phenol's deprotomer — which is the site information the whole search exists to produce — and it
returns *a* valid resonance structure rather than the canonical drawing, so 2,4-dinitrophenol's
delocalised anion comes back in a quinoid form. It names the site; it is not a depiction.

## Consequences

- The sampling half of `calc` is live: `sample_conformers`, `compute_interaction_energy` and the new
  `predict_pka_ensemble` do work rather than refusing.
- Two ADRs' worth of composites (`refined_ensemble`, `ensemble_property`, `interaction`) are
  exercised end to end for the first time.
- **Licence.** `crest` is GPL-3.0 and `xtb` is LGPL-3.0. Both are invoked as separate processes over
  files and neither is linked, so the licences do not reach either codebase — but shipping them in
  an image is distribution, and its obligations attach to whoever publishes it. Taken deliberately;
  recorded here so it is not discovered in an audit as an accident of a base image.
- Removing them stays supported and stays loud: `is_available()` goes False, the searches refuse by
  name, and `resolve_backend()` falls back to `tblite` with the version string saying so.
- The suite runs in two configurations now rather than one, and the gated tests
  (`tests/test_crest_ensembles.py`) invert on `is_available()` so neither configuration asserts a
  refusal as permanent.

## The rule

**A capability that refuses in every environment is not "at parity" — it is untested code with a
polite error message.** The refusal was accurate, documented in four places, and hid three defects
that a single real invocation found. Where a dependency is what stands between a shipped capability
and its first execution, shipping the dependency *is* the test, and the honest interim state is
"unverified", not "unavailable and therefore fine".
