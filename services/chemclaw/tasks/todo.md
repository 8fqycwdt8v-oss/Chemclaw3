# xTB capability layer — implementation

Proposal: `docs/xtb-tools-proposal.md`. Branch: `claude/xtb-chemclaw-tools-proposal-nujp14`.

Scope of *this* change: **X1 (seams) + X2 (properties from the SCF we already run)** — the two
phases the proposal recommends approving first (no new dependency, no new science, high payoff).
X3+ stay proposal-only.

## Design decisions taken during planning (deviations from the proposal, with reasons)

1. **Flat modules in `calc/`, not a `calc/xtb/` package.** A package named `xtb` cannot coexist
   with the existing `calc/xtb.py`, and `calc/` is flat today (`pka.py`, `solubility.py`,
   `store.py`). Renaming the shipped module would churn imports for no behavioral gain.
2. **No `XtbEngine` Protocol yet.** There is exactly one backend (`tblite`); a protocol with one
   implementation is the speculative abstraction `CLAUDE.md` forbids (Rule of Three). It arrives
   with the second backend (X3/X5). `engine_version()` already carries the version role the
   protocol's `version()` would.
3. **No structure *store* yet.** `Structure` ships as a content-addressed **value type** (the
   cache-key input and the composition seam). Nothing in X1/X2 *produces* a new geometry, so a
   persistence layer would have one writer and no reader. It arrives with `optimize_geometry` (X3),
   which is the first task whose output is a geometry.
4. **`XtbSpec` does ship.** It has three real callers on day one (`sp`, `properties`, `fukui`) and
   is the single home of the cache-key derivation — the invariant that makes adding a knob safe.
5. **Fukui indices reported per atom, un-condensed.** Verified during planning: raw per-atom f⁻ on
   ring carbons reproduces the textbook ordering for both activating and deactivating substituents
   (phenol/toluene → *para* > *ortho* > *meta*; nitrobenzene → *meta* > *ortho* > *para*), while
   summing the attached hydrogens' contributions **degrades** the *ortho*/*meta* separation. Data,
   not preference.

## Build

- [x] **1. `geometry()` returns Angstrom.** Move the Bohr conversion into the engine boundary so
      `Structure` can hold the interchange unit. Both existing callers pass the tuple straight
      through, so the change is contained and energy-preserving.
- [x] **2. `calc/structure.py`** — `Structure` value type: elements, positions (Å, normalized by
      rounding), charge, multiplicity, optional smiles/origin; `structure_id` = `st_` + stable hash
      of the chemical content; `from_smiles`/`from_mol`; `uhf` derived from multiplicity;
      electron-parity validation generalizing today's closed-shell check.
- [x] **3. `calc/xtb_spec.py`** — `XtbSpec` (task, method, solvent, accuracy, electronic
      temperature) with the **one** `cache_key(structure)` derivation.
- [x] **4. `calc/xtb_engine.py`** — `run_singlepoint(...)` returning the full tblite result
      (energy, gradient, charges, bond orders, dipole, orbital energies); silence tblite's SCF
      table (`verbosity=0`). `gfn2_energy` stays as the thin energy-only wrapper both existing
      calculators use.
- [x] **5. `calc/xtb_props.py`** — two cached calculators:
      `compute_properties` (HOMO/LUMO/gap, dipole, Mulliken charges, Wiberg bond orders) and
      `compute_fukui` (f⁻/f⁺/f⁰ by finite difference over the N, N−1, N+1 electron systems).
- [x] **6. Port `calc/xtb.py`** onto `Structure` + `XtbSpec` without changing its public API or its
      cached numbers.
- [x] **7. Tools** — `compute_electronic_properties`, `predict_site_reactivity` in
      `agents/calc_tools.py` (`@tool`, so audit + authz wrap them with no extra wiring).
- [x] **8. Config** — `xtb_geometry_decimals`, `xtb_bond_order_threshold`, `xtb_fukui_top_n`.
- [x] **9. Skills** — extend `calculation-selection`; add `reactivity-descriptors` (how to read a
      Fukui ranking without over-claiming).
- [x] **10. Tests** — structure identity/normalization/parity; spec key derivation; properties
      against known GFN2 values; the two regioselectivity cases above; cache hit/miss;
      determinism across SMILES spellings.
- [x] **11. `make lint type test` green**; update `BACKLOG.md`/`DECISIONS.md`.

## Verification (done)

`make lint type test` → **740 passed, 41 skipped** (the skips are the offline sandbox's Postgres and
Temporal suites, unrelated), ruff clean, `mypy --strict` clean across 235 source files,
`make skill-validate` clean. Every new behavior is proven by a real GFN2 calculation, not a mock.

Physics asserted, not assumed:
- water HOMO/LUMO/dipole against known GFN2 values (dipole 1.8–2.2 D);
- benzene's 6 aromatic C–C bond orders ≈ 1.4 and its zero dipole;
- phenol and toluene rank *para* > *ortho* > *meta* for electrophilic attack;
- nitrobenzene inverts to *meta* > *ortho* > *para* — the deactivating-director case that would
  pass by luck if the descriptor were merely correlating with something else;
- f⁻ + f⁺ = 2f⁰ per atom (the definitional identity), and Σf ≈ 1 per Fukui function (normalization);
- the ported energy path reproduces the pre-change cached numbers exactly.

## Review

**What changed and why it is small.** 3 new + 5 changed source files, 2 new + 3 changed test
modules, 1 new + 1 changed skill. The seams are additive:
`XtbInput`/`XtbResult`/`run_xtb`/`run_cached_xtb` keep their signatures and their computed values,
so nothing downstream moved. The one behavioral change to existing code is `geometry()` returning Å
instead of Bohr, with the conversion moved one layer down into `run_singlepoint` — both existing
callers pass the tuple straight through, and `test_pka.py` / `test_xtb.py` prove the energies are
unchanged.

**One consequence to state plainly:** the energy calculator's cache key changed shape (`xtb` →
`xtb.sp`, and the inputs now name the geometry), so existing `calculation_results` rows for it are
orphaned and will recompute once. The *energies* are identical — only the addressing moved. For a
sub-second calculator this is the cheap, documented kind of invalidation (D-011); recorded in D-082
rather than hidden.

**The design decision worth re-reading.** Three of the five decisions above are *refusals to build*
what the proposal describes (the engine protocol, the structure store, a package layout). Each
would have had one caller today. Building them now would have meant writing the X3 abstraction
before knowing what X3 needs — the proposal's own Rule-of-Three note argues against exactly that.
`XtbSpec` shipped because it has three callers on day one; that is the line.

**What the cache key gained, unplanned.** Keying on `structure_id` rather than on
`(smiles, embed_seed)` turned out to be strictly stronger: the seed's effect is *already* inside
the geometry, so the key stays correct without naming it, and a geometry that arrives from anywhere
else later (an optimizer, a file) hits the same cache entry. The `params_hash` for `xtb.sp` is now
empty by construction, which is the honest statement that a single point has no free parameters
beyond its structure and method.

**Not done, deliberately:** X3+ (optimization, Hessian/thermochemistry, reaction energies, CREST).
The proposal's phase boundaries hold — X3 adds a dependency and is a separate reviewable change.
