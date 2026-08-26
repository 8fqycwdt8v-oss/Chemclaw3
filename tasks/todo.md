# Rotational energies and rotamer barriers — concept — 2026-08-26

## Task
"I want to be able to get rotational energies and the barrier energy between rotamers for
individual compounds. Develop a concept for how this could be done — especially how the user
can tell the agent **which bond to rotate**."

Concept only: no capability is built here. The deliverable is the decision record plus the
queue row that makes it workable.

## Plan
- [x] **1 · Establish what already exists**, by reading it rather than assuming.
      `scan_coordinate` (`connectors/calc/connector.yaml`), `ScanJobSpec`/`ScanResult`,
      `compose.scan_profile`, `thermo.boltzmann_populations`, `publish/project.py::_scan`,
      and the two skills that already hold the judgment (`conformational-analysis`,
      `atropisomer-assessment`).
- [x] **2 · Measure the claim the whole first half rests on** — that atom indices are not a
      usable way to name a bond — instead of asserting it. RDKit 2026.3.5, the version
      `pyproject.toml` pins.
- [x] **3 · Decide the three pieces** and place each on the side of the existing boundaries
      (`D-2026-08-16` composability, `D-2026-08-25-the-loop-is-a-composite-not-a-template`,
      "enumerate, then compute — and never the reverse").
- [x] **4 · Write it up** as `docs/decisions/D-2026-08-26-a-torsion-is-named-not-indexed.md`
      (status *proposed*), ledger row, and one `BACKLOG.md` row so it is workable.
- [x] **5 · Verify**: `make lint type test` and the validators that read this corpus
      (`prose-validate`, `skill-validate`).

## What the measurement found

Three numbers changed the design; all three are in the ADR with their reproduction.

1. **A stale atom index is not an error — it is a different bond.** `(5, 4)` is the amide
   C–N of `c1ccc(NC(C)=O)cc1` and an aromatic *ring* bond of `CC(=O)Nc1ccccc1` — the same
   compound, rewritten. In range, really bonded, no error anywhere. `scan_profile` bounds-checks
   indices and nothing else, so that scan runs and returns a profile.
2. **RDKit's canonical atom ranking is invariant across writings**, so a handle derived from it
   names one bond in every spelling of the molecule — measured identical over three writings of
   acetanilide and two of 2-methylbiphenyl.
3. **The rotatable-bond count is a druglikeness descriptor, not a list of torsions.** It reports
   **0** for toluene, p-xylene and *tert*-butylbenzene (terminal tops excluded by `!D1`) and **1**
   for acetanilide — the one it excludes being the amide C–N, which is the bond an anilide barrier
   question is about. So the enumerator cannot be a thin wrapper over it.

## Review

The concept is three pieces, and none of them is new machinery:

- `enumerate_torsions` — a free, pure enumeration on `chem` (so, in `Chemclaw3-mcp`), the sixth
  member of a family this repo already has five of, returning a **content-addressed handle** per
  torsion plus the label, symmetry order and period a chemist and a scan each need.
- `profile_rotation` — a durable composite on `calc`, here, because its key would name the wells
  it settles on and because it loops. Every point it computes is a separately-keyed primitive, so
  a finer re-run pays only for the new points.
- Eyring arithmetic in `science/calc/thermo.py`, beside RRHO, with the uncertainty band — because
  a half-life the model works out in its head is the one number in this chain where ±1 kcal/mol
  is a factor of five.

What it deliberately refuses is written down too: no 2D surfaces, no transition-state claim, no
ring torsions, no enumeration inside the compute job, and the barrier is never a measurement.
