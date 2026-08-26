# Rotational energies and rotamer barriers — implemented, then run for real — 2026-08-26

**One control was rewritten rather than dropped, and it is the finding worth repeating.**
`test_the_bundle_has_no_way_to_write_the_note_itself` asserted `not hasattr(qm_knowledge,
"write_knowledge_node")` — a guard named after a single module, which would have gone *dark* the
moment that module was deleted while still reading, in review, as a control. That is the
`map_to_hpc_identity` shape this repo already has a name for. It is now an AST walk over every
bundle asserting none imports `kg.pr_gate` or names `propose_note`: strictly stronger, and it does
not depend on which bundles exist.

**Two pieces of genuinely dead code fell out of the removal**, both kept alive only by tests that
called them directly: `Structure.as_xyz` (its one caller was the launcher) and its test.

**What the validators caught that grep did not**: `make prose-validate` and `make skill-validate`
found five live claims left over — two skills still declaring `compute_dft_energy` in their
frontmatter, and three backticked paths naming deleted files. Worth running the whole validator set,
not just `lint type test`.

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


## Addendum — run against the real GFN2 server (same day)

`tblite` is the GFN2 Hamiltonian as a PyPI wheel and was already installed, so "needs a cluster"
was wrong. `servers/chem` on 8858 and `servers/calc` on 8860, the handle minted over MCP by the
real chem server, the profile composed against the real calc server, Postgres in front.

**The chemistry came out right** — n-butane 0.62 kcal/mol gauche gap and 59.1% anti (against this
tree's own 59.14% CREST anchor), biphenyl twisted 41.8 degrees with a 1.51 kcal/mol perpendicular
barrier, DMA's amide at 18.10 kcal/mol and a 2.1 s half-life. Released wells at 64.0/296.1 degrees,
off the 30-degree grid, so the release stage moves a well on real physics.

**Two defects the fake could not express**, both fixed with tests verified by reverting each fix:

1. One well per period reported **no barrier at all** — a zero-length arc when a well's successor
   is its own image a period away. That is the amide case, which is what the capability is for.
2. The discontinuity check compared a step against 3 kcal/mol, so it fired on every barrier steep
   enough to matter and stayed quiet on the freely-rotating ones. Now a ratio to the profile's own
   typical step, calibrated on three measured smooth profiles.
