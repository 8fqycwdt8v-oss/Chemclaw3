# D-2026-08-27-a-gradient-is-the-evidence-a-frequency-set-cannot-carry — three cross-repository seams, verified against both trees

## Status

Accepted. Extends `D-2026-08-16-the-physics-leaves-the-cache-stays` (the cache/engine split),
`D-2026-08-26-semiempirical-is-the-whole-tier` (there is no tier to escalate to) and
`D-2026-08-26-a-torsion-is-named-not-indexed` (a torsion is a handle, not a pair of integers).
Supersedes nothing.

## Context

Two audits of `Chemclaw3-mcp` reported four defects whose fix has a half on this side. Each was
verified against both trees before anything was changed, and one of the four was not as described.

### 1. A refusal that named a route that does not exist

`servers/calc`'s `compute_hessian` refuses above `xtb_hessian_max_atoms` (150) with *"Submit it
through Chemclaw3's durable QM job path instead"* — while the same function's docstring, and the
comment on the setting, both state in the present tense that the wording was changed *because* this
server has no durable path and pointing at one would send a chemist looking for a route nobody can
take. The message was never changed to match. `D-2026-08-26-semiempirical-is-the-whole-tier` then
deleted the QM tier outright, so the sentence names something that does not exist on either side.

It reaches the model verbatim: `_call` maps a refusal onto `CalcToolError(str(exc))` and
`agent/tool_authz.py` hands that to the model. **A false instruction is worse than a bare refusal,
because the model acts on it.**

**What this system actually offers a caller whose molecule is too large**, established by reading
the composites rather than by supposing:

- **Nothing by escalating.** Every durable job here composes the *same* `compute_hessian` primitive,
  under the same server ceiling. Escalating a 200-atom Hessian to Temporal changes which process
  waits and nothing else.
- **`level="quick"`**, which every Hessian-taking composite accepts: it differences electronic
  energies and takes no Hessian at all (`budget._PER_SPECIES` prices it at 2 primitives against 3).
- **A truncated model system** — the chemistry answer, for a molecule whose remote substituents
  cannot matter to the mode in question.

### 2. A payload field this repository was dropping

`servers/calc` returns `max_gradient_hartree_per_angstrom` beside every Hessian, deliberately,
because `compute_hessian` differentiates *whatever* geometry it is handed. `HessianPayload` here did
not declare it, and pydantic ignores an undeclared key — so the number arrived and was discarded on
every call.

That is where a silent wrong answer lands. `thermo._vibrational` sums over **positive** wavenumbers
only, so the small spurious modes a non-stationary geometry produces are dropped rather than
flagged: the zero-point energy comes out quietly too low, and — the part that matters —
**a geometry displaced along a soft direction often shows no imaginary mode at all**, so
`is_minimum` was `True` and nothing in the result disagreed.

### 3. The epoch transcription — *not as described*

The report was that `science/calc/store.py` holds `CALCULATION_EPOCH = "1"` against the server's
`"2"`. It holds `"2"`; the bump is logged in place with its reason (the per-atom reactivity panel).
The interesting half of the report survives the correction, though, and it is the opposite of what
it looked like: `remote_key` **composes** the two rather than comparing them —

    params_hash = H({"epoch": <ours>, "remote_params": H({"epoch": <theirs>, "params": ...})})

— so the two constants are independent invalidators. Either may move alone; neither addresses the
other's rows; equality is not required and never was. `servers/calc/tests/test_key_contract.py`
asserts `CALCULATION_EPOCH == CHEMCLAW3_EPOCH` under a docstring calling it "one constant with two
homes", which is a coupling the code does not have and which would go red on a legitimate one-sided
bump.

### 4. An X–H rotor could not be scanned, and the refusal misdescribed it

`servers/chem` splits rotors whose rotating end carries only hydrogens into two kinds, for a
measured reason: a **`top`** (three hydrogens — a methyl) really is inside the quasi-RRHO free-rotor
treatment of the low modes, while an **`xh`** (one or two — an O–H, S–H, N–H) is not. Acetamide's
amide N–H is 16–18 kcal/mol and acetic acid's syn/anti O–H is 5–6 with two genuinely distinct
rotamers. Both are reported with `atoms: []`, because a dihedral through either needs a hydrogen
index and that means something only inside one explicit-H numbering.

This side had one refusal for both, and it called every dihedral-less rotor "a methyl or tert-butyl
rotation" whose "energetic effect is already in the free-rotor treatment" — false for the class the
other side now names correctly, and told to the model as fact. Worse, `TorsionSpec.atoms` required
exactly four, so an `xh` entry could not be *submitted* at all while the manifest instructed the
model to pass the entry through unchanged.

The refusal was also reached by counting `len(torsion.atoms)`, which is why it fired on any entry
with an empty list whatever bond it named — including n-butane's central C–C, which is what the test
covering it used.

## Decision

**1. The refusal a chemist reads is written on the side that knows the alternatives.**
`science/calc/budget.py::require_hessian_affordable` is a preflight in the `require_within_budget`
family, counted in atoms because a Hessian's runaway is one molecule rather than a fan-out, called
from `compose.hessian` — the single function every Hessian in this repository passes through. The
server's ceiling stays authoritative; `calc_hessian_max_atoms` defaults to the same 150 and is
deliberately *not* derived from it, so a stricter deployment gets a truer refusal sooner and a
looser one simply falls through to the server's.

**2. `HessianPayload` declares the gradient, `ThermochemistryResult` reports the verdict, and
`is_minimum` obeys it.** `is_stationary` is a **tri-state**: `None` means the backend reported no
gradient (the `xtb` binary does not, and a cache row written before the field existed carries none),
which must stay a different answer from "it is stationary". `is_minimum` is now
`not imaginary and is_stationary is not False` — never `True` on evidence the result has not got.
The threshold is `xtb_stationary_gradient_tolerance`, defaulted to the 5e-4 the server's own
optimizer converges at, so a geometry that came out of `relax_structure` passes by construction.

No epoch bump: the field is optional and adds a fact rather than restating one, so every row already
in `calculation_results` still validates and still means what it said. Discarding every cached
Hessian in the system to learn a number the server will send with the next one is not a trade worth
making.

**3. The two epochs compose; nothing here asserts they match.** The relationship is written where
the constant is declared and pinned by
`tests/test_calc_remote.py::test_the_two_epochs_compose_rather_than_having_to_match`, which measures
both halves: a bump on this side moves the address while the server answers identically, and the
server's own digest survives verbatim inside the composed one, which is how its bump reaches the
address too.

**4. An X–H rotor is scanned; only a symmetric top is refused.** `compose._rotor_dihedral` builds
the dihedral for an `xh` rotor in the structure's own explicit-H numbering — `(reference, anchor,
rotating, hydrogen)`, both loose ends chosen by lowest index so two runs of the same question drive
the same four atoms and each scan point's key is stable. `_explicit_molecule` turns the numbering
reliance into an assertion by comparing `AddHs`'s element list against `structure.elements`, exactly
as the server's own `_mol_with_conformer` does. `_checked_dihedral` now bounds indices by the
explicit-H atom count rather than the heavy-atom count, and `TorsionSpec.atoms` accepts 0 or 4.

A dihedral-less entry for a bond that *does* have a heavy dihedral is a third case and is refused as
malformed, naming `enumerate_torsions` — it is neither kind, and answering it with a sentence about
methyl rotations is the conflation this decision removes.

## Consequences

- **No `Chemclaw3-mcp` change is needed for the X–H scan.** `scan_point` bounds its atom indices by
  `len(structure.elements)`, which is the explicit-H count, and `drive_coordinate` rebuilds the
  molecule with `parse_molecule` (`AddHs`). A dihedral ending on a hydrogen was always accepted; the
  refusal was entirely on this side.
- **Two changes are owed by `Chemclaw3-mcp`** and are stated in `docs/planning/BACKLOG.md`: the
  `compute_hessian` size refusal must stop naming a route (its test pins the string), and
  `test_key_contract.py`'s epoch-equality assertion must become a statement about the *composition*
  or be dropped.
- `is_minimum` can now be `False` with an empty `imaginary_frequencies_cm`, so the three warnings
  that attributed it to an imaginary mode name whichever finding actually stands. The refinement
  loop is unaffected: it returns immediately when `imaginary_displacement is None`, which is
  precisely the new case.
- `max_gradient_hartree_per_angstrom` is **not** published as a fact. The registry already defines
  `max_gradient` in hartree/bohr for the optimization that produced a geometry, and one property
  name carrying two units is the silent-wrong-number shape the registry exists to prevent.
  `is_stationary` is not published either — it is the second half of `is_minimum`, which is.
