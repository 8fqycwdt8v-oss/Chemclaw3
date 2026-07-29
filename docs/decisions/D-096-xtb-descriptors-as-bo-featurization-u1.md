# D-096 — xTB descriptors as BO featurization (U1)

**Context.** `docs/xtb-use-cases.md` §6.2 ranked this the highest-value xTB integration and noted
it needs **no new xTB capability** — only wiring. A BoFire campaign over "which ligand / base /
solvent" modelled the choice as a bare category, so the surrogate learned an independent effect per
label and could say nothing about an option nobody had run. With eight ligands and a budget of
twelve experiments, most of the budget goes to discovering that the model has no opinion.

**Decision.** `CategoricalParameter` gains two optional fields: `structures` (category → SMILES,
the declared input) and `descriptors` (category → values, the computed output). `bo.featurize`
fills the second from the first through `calc.xtb_props`, and `bo.engine` maps a featurized
parameter to BoFire's `CategoricalDescriptorInput` instead of `CategoricalInput`.

**Both halves are carried deliberately.** `structures` is provenance — which molecule produced
which descriptor row — and `descriptors` is what the surrogate saw. Storing the *values* in the
spec (rather than recomputing per round) is what keeps a durable campaign's featurization stable
across rounds and worker restarts: a campaign cannot silently re-featurize itself mid-run because
a calculator was upgraded.

**Descriptor set, and one deliberate omission.** HOMO (donor strength), LUMO (acceptor strength),
dipole (polarity), and the most positive / most negative Mulliken charge (electrostatic extremes,
carrying H-bond donor and acceptor character). The **HOMO-LUMO gap is excluded**: it equals
`lumo - homo` exactly, so shipping it alongside both would hand the GP a perfectly collinear
column — worse kernel conditioning for no information.

**The trap this decision walked into and out of.** Swapping the BoFire feature type looks
sufficient but is not obviously so: a strategy's `input_preprocessing_specs` reports ORDINAL even
for a descriptor input, which reads like the descriptors are being ignored. They are not — that
field is the *pre-processing* step, and the encoding that matters is the surrogate's own
`categorical_encodings`, which defaults to DESCRIPTOR for a `CategoricalDescriptorInput` and to
ORDINAL for a plain one. Since we *depend on a default rather than setting it*, and since the
failure mode is silent (the campaign still runs, still returns candidates, and simply stops
generalizing), `tests/test_bo_featurize.py` pins both encodings explicitly.

**Verification is the payoff, not the plumbing.** With three ligands observed and PCy3 unobserved,
the bare surrogate predicts exactly the mean of the observed values for PCy3 — the arithmetic
signature of having no information about it — while the featurized surrogate moves the prediction
toward its descriptor neighbour PtBu3. That is asserted directly, because a test that only checks
candidate shape would pass just as happily on a featurization that was wired up but inert. The
descriptors are also checked to carry real chemistry (trialkylphosphines rank above
triarylphosphines on HOMO; the aryl ligand's low-lying pi* shows in its LUMO), and the
values-matrix row/column order is asserted against the declared order, since BoFire matches by
position and a transpose would build a working campaign on the wrong molecules.

**Ancillary move.** `default_store()` moved from `agents.calc_tools` to `calc.postgres_store`:
storage is not a calculator concept, and the featurizer needs the same seam. Tests that patch it
at the importing module are unaffected.

**Limit, stated in the skill rather than hidden.** The featurization is **electronic only**.
Cone angles and buried volume need a 3D geometry, so two ligands differing mainly in bulk look
similar — a real limitation for phosphine selection specifically, and one the geometry tasks
(plan X3) would address.
