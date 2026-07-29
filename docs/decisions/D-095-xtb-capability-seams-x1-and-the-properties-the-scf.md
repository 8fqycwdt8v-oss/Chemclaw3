# D-095 — xTB capability seams (X1) and the properties the SCF already produced (X2)

**Context.** `docs/xtb-tools-proposal.md` inventories what the xTB ecosystem offers against what
ChemClaw consumed: one capability (a single-point energy) through one of three engines (`tblite`
in-process). The same SCF that produced that energy also produced Mulliken charges, Wiberg bond
orders, the dipole, and the orbital energies, all of which were read and discarded. This ADR covers
the first two phases of that proposal — the ones that add no dependency.

**Decision 1 — geometry becomes a content-addressed value (`calc/structure.py`).** Every calculator
previously went SMILES → embed → compute in one breath, so two tasks on "the same molecule" silently
produced two different geometries and nothing could reuse one. `Structure` carries elements,
positions (Angstrom, rounded to `xtb_geometry_decimals` on construction), charge, multiplicity, and
an optional `origin`; `structure_id` is a stable hash of the chemical content alone.

*The unplanned payoff is in the cache key.* Keying on `structure_id` rather than on
`(smiles, embed_seed)` is strictly stronger: the seed's effect is already inside the coordinates, so
the key stays correct without naming it, and a geometry arriving later from an optimizer or a file
hits the same entry. `xtb.sp`'s `params_hash` is now empty by construction — the honest statement
that a single point has no free parameters beyond its structure and method.

*It also generalizes a guard rather than weakening one.* The old `require_closed_shell` refused every
odd-electron system because a SMILES does not encode multiplicity. `Structure` validates the electron
count *against a declared* multiplicity instead, so an accidental radical still fails fast (with a
message naming the fix) while a deliberate open shell is computable. That is what makes the Fukui
ions legitimate rather than silent — the previous check would have made X2 impossible.

**Decision 2 — one cache-key derivation (`calc/xtb_spec.py`).** `XtbSpec` holds every field that can
move a number and derives the key once over `model_dump()`, so a new knob is keyed by construction
rather than by review. It shipped with three callers on day one (`sp`, `properties`, `fukui`).

**Decision 3 — three things the proposal describes were deliberately *not* built.** The `XtbEngine`
protocol (one backend today), the structure *store* (nothing in X1/X2 produces a geometry, so it
would have one writer and no reader), and a `calc/xtb/` package (cannot coexist with `calc/xtb.py`;
`calc/` is flat). Each would have been a one-caller abstraction written before knowing what X3 needs
— the Rule of Three case the proposal's own §12 makes. `XtbSpec` shipped because it has three
callers; that is the line.

**Decision 4 — Fukui indices are computed on an MMFF-relaxed geometry, and that is load-bearing.**
Measured, not assumed: on a raw ETKDG embedding the residual distortion breaks the symmetry of
chemically equivalent ring positions badly enough to invert the ordering for phenol and toluene
(*ortho* and *meta* overlap). Relaxing first restores the equivalence — toluene's two *ortho* carbons
agree to 1e-4 — and recovers *para* > *ortho* > *meta*, while nitrobenzene correctly inverts to
*meta*. `calc.pka` already set the same flag for the same reason. A GFN2 optimization would be better
and is the first thing X3 improves; until then `structure_id` records honestly that these are
force-field geometries.

**Consequence — a one-time cache invalidation, accepted.** `calc_type` moved from `xtb` to `xtb.sp`
and the key's inputs changed, so existing `calculation_results` rows for the energy calculator are
orphaned. Energies are unchanged; only the addressing is. The cost is one recomputation of a
sub-second calculator, and it is the documented kind of invalidation (D-011: a widened key is a
correctness feature). Nothing else was touched — `XtbInput`/`XtbResult`/`run_xtb`/`run_cached_xtb`
keep their signatures and their values, and `calc.pka`'s calibrated path is untouched.

**Verification.** The physics is asserted rather than assumed: the definitional identity
f⁰ = (f⁻ + f⁺)/2 per atom, the per-molecule normalization Σf ≈ 1, benzene's six equivalent aromatic
bond orders and zero dipole, and — the discriminating case — nitrobenzene inverting to *meta* while
phenol and toluene direct *ortho/para*. A descriptor that merely tracked ring position would pass the
activating cases and fail that one. Ring positions in the tests are derived from the molecular graph,
not hardcoded, so a change in RDKit's canonical atom order cannot leave them silently checking the
wrong atoms.
