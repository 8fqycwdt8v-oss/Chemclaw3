# Rotational energies and rotamer barriers — implemented — 2026-08-26

## Task
"Get rotational energies and the barrier energy between rotamers for individual compounds —
especially, how the user tells the agent **which bond to rotate**." Concept first, then build it.

`D-2026-08-26-a-torsion-is-named-not-indexed` is the record; this is the working log.

## Plan
- [x] **1 · Read what exists** — `scan_coordinate`, `compose.scan_profile`, `thermo`, and the two
      skills that already hold the judgment.
- [x] **2 · Measure the premise** rather than assert it (RDKit 2026.3.5, the pinned build).
- [x] **3 · Decide** — three pieces, each on the side of a boundary already drawn.
- [x] **4 · `Chemclaw3-mcp`** — `enumerate_torsions` on `servers/chem`, plus `render_structure`'s
      `highlight_atoms`, plus the automorphism check and the contract table.
- [x] **5 · Here** — `torsion_handle`, `Torsion`/`Rotamer`/`RotationBarrier`/`RotationProfile`,
      Eyring in `thermo.py`, `rotation_units` in `budget.py`, `RotationJobSpec`,
      `compose.rotation_profile`, the activity dispatch, the manifest job, the projector and its
      properties, `rotational-barrier.yaml`, both skills.
- [x] **6 · Tests** — 41 new, driven through the real composite against a fake with a real
      torsional potential.
- [x] **7 · Verify** — `make lint type test`, `connector-validate`, `template-validate`,
      `skill-validate`, `prose-validate`; both repos.

## What building it found

1. **A stale atom index is not an error.** `(4, 5)` is the amide C–N of `c1ccc(NC(C)=O)cc1` and an
   aromatic *ring* bond of `CC(=O)Nc1ccccc1`. `scan_profile` bounds-checks and nothing else.
2. **The rotatable-bond descriptor is not a torsion list.** 0 for toluene, p-xylene and
   *tert*-butylbenzene; 1 for acetanilide, and that one is not the amide.
3. **Symmetry classes match automorphism orbits** on 21 molecules — 0 false merges. Shipped as a
   test, not as a claim.
4. **`skills/atropisomer-assessment`'s half-life anchors were wrong by two orders of magnitude.**
   Its prose said "27 → about a day"; 27 kcal/mol is 80 days, and 30 is 35 years, not "a few".
   The error was largest exactly at the ICH class boundary the skill exists to decide.
5. **Every `calc` durable job was publishing nothing.** `CalcJobWorkflow` sends
   `payload_kind=type(result).__name__` and its result is the `XtbJobResult` *envelope*, so
   `projector_for("calc.compute_reaction_energy", "XtbJobResult")` was `None` — while
   `tests/test_publish_reaches_the_hooks.py` was green asserting a `payload_kind` production has
   never sent. Fixed at the projection boundary (`unwrap_envelope`), not by re-shaping what the
   chat sees.

## Review

The three pieces, and why each is where it is:

- **`enumerate_torsions` on `chem`** (so, `Chemclaw3-mcp`): a pure graph operation, the sixth in a
  family of five, under the house rule *enumerate, then compute — and never the reverse*. It mints
  a handle from the canonical symmetry classes plus the RDKit build, so a rewritten SMILES keeps the
  name and a toolchain bump breaks it loudly.
- **`profile_rotation` here**: its key would name the wells it settles on, so `D-2026-08-16` says
  it is not shippable as a tool; it loops, so `D-2026-08-25-the-loop-is-a-composite-not-a-template`
  says it is not a template. Every point it computes is a separately-keyed primitive.
- **Eyring beside RRHO**: arithmetic over a result, not a calculation — the same rule that kept the
  RRHO half here when the physics left.

What is deliberately not done: 2D surfaces, transition-state claims, ring torsions, enumeration
inside the compute job. And the two open ends, both needing the live lane rather than more code —
no barrier has been computed against real xTB, and the conformer-dependence warning threshold is
unset. Both are in the ADR.
