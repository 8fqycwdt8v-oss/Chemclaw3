# Task: integrate CREST fully (pKa on ensembles, and every other workflow it earns)

Measured starting point (crest 3.0.2 + xtb installed from conda-forge in this session, driven
through the shipped code):

- `search_conformer_ensemble(search="deprotomers")` **crashes** — `_from_xyz` reuses the template's
  13 elements for a 12-atom anion.
- `search_conformer_ensemble(search="protomers")` **finds no file** — CREST writes `protonated.xyz`,
  the code looks for `protomers.xyz`.
- both would carry the **neutral charge** on a charged species, so any downstream relax runs the
  wrong electron count.
- `tautomers` works but every member is labelled with the *input* SMILES.

So "CREST is integrated" was true of `conformers`/`complex` only, and untestable while the binary
was absent from both images.

## Chemclaw3-mcp (`servers/calc`) — primitives

- [x] A1 `crest_cli`: parse elements from the ensemble file (CREST presorts H to the end and
      changes the atom count), `protonated.xyz`, per-search charge shift (+1/-1), drop the
      template SMILES on constitution-changing searches.
- [x] A2 perceive each member's SMILES from its geometry (`rdDetermineBonds`), best-effort — a
      deprotomer ensemble is only useful if you can see *which site* came off. Never invent: on
      failure the member carries no SMILES.
- [x] A3 tests: fixture-driven parse tests (no binary) + a crest-gated end-to-end test.
- [x] A4 ship `crest`+`xtb` in the image (micromamba/conda-forge), so the tools stop refusing.
- [x] A5 ADR + README + MODULES + manifest.

## Chemclaw3 — the composite

- [x] B1 `compose.microstate_pka`: CREST deprotomer/protomer ensemble + conformer ensembles,
      Boltzmann-weighted ΔG, calibrated → pKa. Reuses `conformer_ensemble` / `relax_to_minimum`.
- [x] B2 new `PkaEnsembleJobSpec` + dispatch + `EnsemblePkaResult` + `predict_pka_ensemble` job.
- [ ] B3 **fit the calibration here** over a reference set and report Spearman/R²/RMSE. A new
      `calc_version`, so the existing `predict_pka` ledger is untouched.
- [x] B4 ADR + BACKLOG/DEFERRED + connector.yaml.

## Explicitly not doing (no caller — Rule of Three)

QCG explicit solvation, `--msreact`, `--entropy` as a runtype, `--mecp`. DEFERRED rows with triggers.

## Review

**What shipped, and what it cost.**

`Chemclaw3-mcp` — `crest_cli` parses elements from the ensemble file, shifts the charge per search,
names one output file per search with no fallback, and perceives each member's SMILES from its
geometry for the three constitution-changing searches. `chem.perceive_smiles` and
`chem.atomic_numbers` are new; `crest_perceive_max_atoms` bounds the first. The `Containerfile`
gains a `sampling` stage installing pinned crest 3.0.2 + xtb 6.7.1 from conda-forge, and sets
`CHEMCLAW_CREST_THREADS=4` because the scrubbed child environment does not inherit the image's
`OMP_NUM_THREADS=1` and CREST would otherwise size itself to the node.

`Chemclaw3` — `microstate_pka` composes two cached searches into a macrostate pKa;
`macrostate_free_energy_kcal` and `rt_kcal` are new in `thermo.py`; `searched_members` was factored
out of `conformer_ensemble` because a `ConformerEnsemble` reports energies *relative to its own
lowest member* and two macrostates cannot be compared that way. `MicrostatePkaJobSpec` →
`predict_pka_ensemble`, `expensive: true`, with the selection skill updated.

**Two things found on the way that are not this task's:**

- Every `calc` composite is dropped by the publish seam — `payload_kind` is the envelope's class
  name, which no projector matches. Queued in `BACKLOG.md` §3 rather than fixed here: it changes
  what nine job types publish to an external store.
- `tests/test_calc_ensembles.py::test_refining_the_same_ensemble_twice_pays_nothing` fails with
  Postgres down, because the artifact offload silently fails and the Hessians recompute. Not a code
  defect; it is `CLAUDE.md`'s "the sandbox is not offline" warning arriving in person.

**Two of my own, corrected before shipping:** the near-degenerate microstate window was a constant
0.5925 kcal/mol (RT at 298 K) while the caller can set the temperature — now `rt_kcal(temperature)`;
and the calibration now records the search depth it was fitted at, because a deeper search moves the
free-energy difference the slope was fitted against.

**The `auto` branch rule changed once, against a measurement of my own reasoning rather than of
code:** "a proton on N, O or S means ask the acid question" sends ethylamine down the acid branch,
where the answer is its N-H acidity at ~36 rather than the 10.7 anybody means. Acid is now O-H/S-H
only; anything else with nitrogen is a base.
