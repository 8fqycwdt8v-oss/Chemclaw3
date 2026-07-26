# Proposal: xTB as a full capability layer, exposed as agent tools

**Status:** proposal (not decided). On acceptance this becomes BACKLOG items X1–X7 and one ADR
(D-0xx) in `DECISIONS.md`.
**Scope:** how ChemClaw should use the xTB ecosystem (tblite / xtb / CREST) and what tool surface
the agent should see.
**Non-scope:** DFT/HPC accuracy work (deferred, see `DEFERRED.md`), docking, ML potentials.

---

## 0. TL;DR — the recommendation in eight lines

1. Today we use **one** of xTB's capabilities (a single-point energy) through **one** of its three
   engines (`tblite` in-process). The Hamiltonian is the cheap part; everything a chemist actually
   asks for — geometries, ΔG, reactivity sites, conformers — is what we are not exposing.
2. Do **not** add one tool per xtb flag. Add **four internal seams** and then a **small,
   chemistry-shaped tool surface** on top of them.
3. The four seams: a **`StructureRef`** (content-addressed geometry handle), a single **`XtbSpec`**
   request model, an **`XtbEngine`** protocol with capability-declaring backends, and **one cache
   key derivation** reusing `CalculationKey`.
4. Backends stage in as a ladder: `tblite` (have it) → `tblite+ASE` (pure Python: opt, Hessian,
   thermo, scans) → `xtb` binary (GFN-FF, ANCopt, Fukui/IP-EA, metadynamics, CPCM-X) →
   `crest` (conformer/tautomer/protomer ensembles).
5. Nine agent tools, each answering a chemistry question, not naming a program option. The
   composite `compute_reaction_energy` is the highest-value single item in this document.
6. Long calculations route to Temporal automatically by a predicted-cost rule; the tool returns
   either a result or a job id, so the model never learns two APIs for one question.
7. The expert escape hatch is a **typed** spec, never a raw input file or raw argv — that is the
   prompt-injection boundary.
8. Cost of the whole ladder is dominated by **X3** (adds `ase`) and **X5** (adds two binaries to
   the image, with a licensing question for legal). X1/X2/X4 add no new dependencies at all.

---

## 1. Where we are today

### 1.1 What exists

| Piece | File | What it does |
|---|---|---|
| Engine primitives | `calc/xtb_engine.py` | RDKit ETKDG embed (seeded), optional MMFF pre-opt, `tblite` single point, optional ALPB |
| Energy calculator | `calc/xtb.py` | `XtbInput{smiles, charge}` → `XtbResult{total_energy_hartree}`, cached |
| pKa calculator | `calc/pka.py` | Enumerate O-H/S-H sites → ALPB-solvated ΔE(deprotonation) → linear calibration |
| Cache | `calc/store.py` | `CalculationKey(calc_type, calc_version, input_hash, params_hash)` + `run_cached` |
| Agent tools | `agents/calc_tools.py` | `compute_xtb_energy`, `predict_pka`, `predict_solubility` |
| Judgment | `skills/calculation-selection/SKILL.md` | Which calculator, and how far to trust it |
| Durable path | `workflows/qm_job.py`, `agents/qm_tools.py` | Temporal QM job (DFT, mock/Nextflow), job id + poll |

This is a good foundation. The cache key already versions on `tblite`+`rdkit` builds
(`engine_version()`), the honesty guards already exist (charge-vs-formal-charge, closed-shell
rejection), and the tool registry (`@tool`) means a new tool is a decorator, not an edit to
orchestration code. **Nothing below asks to change any of that** — the proposal extends it.

### 1.2 The gap, stated precisely

`calc.xtb.run_xtb` computes an absolute GFN2 energy of an **unoptimized, single-conformer,
gas-phase** geometry. Our own skill correctly tells the agent this number is only meaningful
relatively — which is another way of saying the tool answers almost no question on its own.

Concretely, a chemist asking any of these gets nothing today:

- "Is this the right tautomer / which tautomer dominates in water?"
- "What is ΔG of this esterification at 80 °C in toluene?"
- "Which ring position is attacked by the electrophile?"
- "How high is the rotational barrier about this amide bond?"
- "Give me the lowest-energy conformer geometry so I can look at it."
- "Which of these three solvents stabilises the anion best?"

Every one of these is inside xTB's reach at a cost of seconds to minutes. That is the gap.

### 1.3 Capability actually consumed vs. available

Even from `tblite` alone — no new dependency, no binary — we currently read `energy` and discard
everything else the same SCF already produced: `gradient`, Mulliken `charges`, Wiberg
`bond-orders`, `dipole`, `quadrupole`, `orbital-energies` (→ HOMO/LUMO/gap), atom-resolved
`energies`. The cheapest win in this document is **X2**: surface what we already compute.

---

## 2. What the xTB ecosystem offers

Three distinct delivery vehicles, deliberately kept apart in this proposal because they have
different install costs, licences, and failure modes.

### 2.1 `tblite` (Python, already a dependency)

The modern reimplementation of the GFN Hamiltonians as a library. Per single point:

| Capability | Access |
|---|---|
| GFN1-xTB / GFN2-xTB energies | `Calculator(method, numbers, positions, charge, uhf)` |
| Analytical gradients | `result.get("gradient")` — **enables optimization and finite-difference Hessians** |
| Mulliken charges, Wiberg bond orders | `result.get("charges" / "bond-orders")` |
| Dipole, quadrupole | `result.get("dipole" / "quadrupole")` |
| Orbital energies / occupations | `result.get("orbital-energies" / "orbital-occupations")` → HOMO, LUMO, gap |
| Atom-resolved energies | `result.get("energies")` |
| Implicit solvation | `calc.add("alpb-solvation" \| "gbsa-solvation" \| "cpcm-solvation", solvent)` |
| Open shell | `uhf=` constructor argument |
| Fermi smearing, accuracy, SCF iterations | `calc.set("electronic-temperature" \| "accuracy" \| "max-iter", …)` |
| External electric field | `calc.add("electric-field", vector)` |

What it does **not** give: any optimizer, any Hessian, any dynamics, GFN-FF, GFN0.

### 2.2 `ase` + `tblite.ase` (pure Python, LGPL-2.1, ~2 MB)

`tblite` ships an ASE calculator. ASE contributes the drivers `tblite` lacks, in Python, with no
binary and no subprocess:

- **Geometry optimization** — `BFGS`/`LBFGS`/`FIRE` in Cartesians, with convergence on max force.
- **Vibrational analysis** — `ase.vibrations.Vibrations`, finite differences over 6N displacements.
- **Thermochemistry** — `ase.thermochemistry.IdealGasThermo`: ZPE, H, S, G at T and p (RRHO).
- **Constraints** — `FixAtoms`, `FixBondLength`, `FixInternals` → constrained opt and relaxed scans.
- **Reaction paths** — NEB.
- **MD** — Langevin/NVT, if we ever want it.

This is the single highest leverage dependency in the proposal: it converts the gradient we already
compute into geometries, frequencies, and free energies without adding a binary to the image.

Cost caveat, honestly stated: ASE optimizes in Cartesian coordinates, so it needs roughly 2–4× more
steps than xtb's ANCopt (approximate normal coordinates). For ≤60-atom drug-like molecules that is
still seconds. It matters at 150+ atoms, which is exactly where the CLI backend earns its place.

### 2.3 `xtb` binary (Fortran, LGPL-3.0) and `crest` (GPL-3.0)

Everything above plus what only the programs have:

| Capability | Invocation | Why we want it |
|---|---|---|
| **GFN-FF** force field | `--gfnff` | 1000+ atom systems, ensemble pre-screening; ~10³× faster than GFN2 |
| **GFN0-xTB** | `--gfn 0` | Very fast pre-optimizer for bad starting geometries |
| **ANCopt** | `--opt <level>` (`crude`…`extreme`) | Robust, internal-coordinate optimization; the reference implementation |
| **Hessian + RRHO thermo** | `--hess`, `--ohess` | Frequencies + G/H/S with the modified-RRHO rotor treatment (better than plain ideal-gas RRHO for floppy molecules) |
| **Biased Hessian (SPH)** | `--bhess` | Thermal corrections at *non-stationary* points — the honest way to get ΔG‡ from a constrained scan maximum |
| **Fukui indices** | `--vfukui` | f⁻/f⁺/f⁰ per atom → **site selectivity**, the question chemists ask most |
| **Vertical IP/EA/ω** | `--vip`, `--vea`, `--vipea`, `--vomega` | Redox windows, global electrophilicity index |
| **CPCM-X** | `--cpcmx <solvent>` | Better solvation free energies than ALPB, incl. non-aqueous |
| **ESP / population / orbitals** | `--esp`, `--pop`, `--molden` | Visualisable electrostatics; σ-hole analysis |
| **Constraints, scans, walls, external charges** | `--input` control file (`$constrain`, `$scan`, `$wall`, `$metadyn`) | Relaxed scans, barrier estimates, cavity confinement |
| **Metadynamics** | `--metadyn` | Conformer/rare-event sampling |
| **ONIOM** | `--oniom` | QM/QM′ embedding for a reactive site in a large scaffold |
| **CREST ensembles** | `crest <in> --gfn2 --alpb <solv>` | iMTD-GC conformer/rotamer ensembles + Boltzmann populations |
| **CREST protomers/tautomers** | `--protonate`, `--deprotonate`, `--tautomerize` | Tautomer ranking, microstate enumeration — a direct upgrade path for `calc/pka.py` |
| **CREST entropy mode** | `--entropy` | Conformational entropy, which single-structure RRHO systematically misses |

Machine-readable output: `--json` writes `xtbout.json`; CREST writes `crest_conformers.xyz` +
`crest.energies`. Both parse cleanly — no screen-scraping of human-readable output required, which
is what makes a CLI backend acceptable at all.

### 2.4 Escalation boundary

xTB is semiempirical. Typical honest error bars: relative conformer energies ~1 kcal/mol, reaction
energies for well-balanced isodesmic comparisons ~2–3 kcal/mol, absolute barriers frequently
5+ kcal/mol off. **The tools must carry this, not the prose.** Where a question needs better, the
answer is the existing `submit_qm_job` DFT path (or CENSO-style refinement of a CREST ensemble,
which we are not proposing to build now). Section 8 makes this a mechanical property of the result
model rather than a hope about the model's phrasing.

---

## 3. Design principles (inherited, not invented)

These come straight from `CLAUDE.md` and `docs/architektur.md`; every decision below is justified
against them.

1. **Skills hold judgment, tools hold capability.** A tool never decides *whether* a calculation is
   warranted; a skill never re-implements one.
2. **Compute once, never twice (D-011).** Everything goes through `CalculationKey` + `run_cached`,
   versioned so an engine upgrade is a miss and not a stale hit.
3. **Durability lives only in Temporal (D-002).** Fast work runs inline; long work is a durable job
   with a pushed-back completion event. MAF holds no durable state.
4. **Fail fast over a meaningless number (G4).** Existing precedent: we reject charge mismatches and
   open-shell species rather than returning a converged-but-wrong energy.
5. **Config, never magic numbers.** Methods, solvents, thresholds, timeouts, budgets — all
   `pydantic-settings`, ENV-overridable.
6. **Rule of Three.** No abstraction until the second real caller. This is why the backends are
   staged rather than all built behind a protocol on day one (§12).
7. **One extension seam per capability kind.** A tool registers itself with `@tool`; nothing in
   `build_agent` is edited.

---

## 4. Architecture

Five layers. The middle three are new; the outer two already exist.

```
skills/          xtb-workflow-design · reactivity-descriptors · calculation-selection (judgment)
                                    ▲
agents/xtb_tools.py                 │  9 chemistry-shaped tools  (@tool, audited, authz-gated)
                                    ▲
calc/xtb/tasks/*.py                 │  typed calculators: sp, opt, hess, thermo, scan, fukui, …
                                    │  each: XtbSpec → CalculationKey → run_cached → typed result
                                    ▲
calc/xtb/engines/*.py               │  XtbEngine protocol: tblite | ase | cli | crest
                                    ▲
tblite (lib) · ase · xtb(1) · crest(1)
```

Four seams carry the whole design.

### 4.1 Seam A — `StructureRef`: geometry as a first-class, content-addressed value

**The problem.** Today every calculator starts from a SMILES and re-embeds. That makes composition
impossible: "optimize this, then get its frequencies, then its charges" would re-embed three times
and silently compute three different geometries. Any multi-step xTB workflow is broken without a
geometry handle.

**The proposal.** A structure is content-addressed, exactly like a calculation:

```python
class Structure(BaseModel):
    """A concrete 3D molecular structure — the unit that xTB tasks consume and produce.

    Content-addressed: `structure_id` is a stable hash of the chemical content
    (elements, rounded coordinates, charge, multiplicity), so two tasks that produce
    the same geometry share a cache entry regardless of how they got there.
    """

    elements: list[int]           # atomic numbers
    positions: list[list[float]]  # Angstrom, rounded to `settings.xtb_geometry_precision`
    charge: int = 0
    multiplicity: int = 1
    smiles: str | None = None     # canonical, when the structure came from / maps to one
    origin: str | None = None     # the CalculationKey.as_str() that produced it — lineage

    @property
    def structure_id(self) -> str:
        return "st_" + stable_hash(
            {"e": self.elements, "p": self.positions,
             "q": self.charge, "m": self.multiplicity}
        )
```

Every tool accepts a `StructureRef = str`, resolved in one place:

- a **SMILES** → embed deterministically (existing `geometry()`, seeded) and register;
- a **`st_…` id** → load from the structure store;
- an **`ens_…` id** → the ensemble's Boltzmann-lowest member (with a note in the result).

Three properties fall out, and they are the reason this seam is worth building first:

- **Composition** — `optimize_geometry` returns a `structure_id`; `compute_thermochemistry` consumes
  it. The agent chains tools without ever handling coordinates.
- **Cache sharing across paths** — a Hessian on an optimized geometry hits the cache whether that
  geometry came from ASE, from ANCopt, or from a user upload, because the key is the *content*.
- **Lineage for GxP** — `origin` makes every geometry traceable to the calculation that produced it,
  which is precisely the provenance story `StoredResult.provenance` was designed for.

**Storage.** Reuse `ResultStore` with `calc_type="structure"` and `calc_version="1"`; no new
backend, no new migration beyond what `calc/migrate.py` already does. Coordinates are rounded before
hashing (default 1e-4 Å) so bit-level float noise from a re-run does not fork the cache.

### 4.2 Seam B — `XtbSpec`: one request model for every task

One model, discriminated on `task`, is what keeps N capabilities from becoming N bespoke plumbings.

```python
class Solvation(BaseModel):
    """Implicit solvation request. `model` is validated against the engine's supported set."""
    model: Literal["none", "alpb", "gbsa", "cpcmx"] = "none"
    solvent: str | None = None


class XtbSpec(BaseModel):
    """A single xTB task request — the one thing that gets hashed into a cache key.

    Every field that can change a number is here and nowhere else; that invariant is
    what makes `cache_key()` correct by construction rather than by review.
    """
    task: Literal["sp", "opt", "hess", "thermo", "scan", "fukui", "ipea", "conformers", "md"]
    structure: StructureRef
    method: Literal["gfn0", "gfn1", "gfn2", "gfnff"] = "gfn2"
    solvation: Solvation = Solvation()
    accuracy: float = 1.0
    electronic_temperature_k: float = 300.0
    opt_level: Literal["crude", "loose", "normal", "tight", "vtight"] = "normal"
    temperature_k: float = 298.15
    pressure_pa: float = 101_325.0
    constraints: list[Constraint] = []
    scan: ScanSpec | None = None
    charge: int | None = None        # None → take from the structure
    multiplicity: int | None = None

    def cache_key(self, engine: "XtbEngine") -> CalculationKey:
        return CalculationKey.build(
            calc_type=f"xtb.{self.task}",
            calc_version=f"{self.method}+{engine.version()}",
            inputs={"structure": resolve(self.structure).structure_id,
                    "charge": self.charge, "multiplicity": self.multiplicity},
            params=self.model_dump(exclude={"task", "structure", "method", "charge",
                                            "multiplicity"}),
        )
```

Note what this buys: `cache_key` is written **once**. Adding a task or a knob cannot silently break
cache correctness, because any new field lands in `params` automatically. The existing per-calculator
`_calc_version()` functions in `calc/xtb.py` and `calc/pka.py` stay valid and unchanged — this is
additive.

### 4.3 Seam C — `XtbEngine`: capability-declaring backends

```python
class XtbEngine(Protocol):
    """One way of executing an `XtbSpec`. Backends differ in capability, not in contract."""

    name: str

    def version(self) -> str:
        """Build identity for the cache key — every component that can shift a number."""

    def capabilities(self) -> frozenset[str]:
        """Task names this backend can execute."""

    async def run(self, spec: XtbSpec) -> XtbRawResult:
        """Execute `spec`, or raise `UnsupportedCapability` / `EngineError`."""
```

| Backend | Capabilities | Install cost |
|---|---|---|
| `tblite` | `sp`, `fukui`* | none (have it) |
| `ase` | `sp`, `opt`, `hess`, `thermo`, `scan` | `ase` pip dependency |
| `cli` (`xtb`) | all of the above + `ipea`, `md`, GFN-FF/GFN0, CPCM-X, `--bhess` | binary in image |
| `crest` | `conformers`, tautomers, protomers | binary in image |

\* Fukui via three single points (N, N−1, N+1 electrons) with the finite-difference definition —
available without the binary, at the cost of two extra SCFs and a restriction to charge states we
can legitimately compute.

**Selection is config-driven and deterministic**, never model-chosen:

```python
def select_engine(spec: XtbSpec) -> XtbEngine:
    """Cheapest configured backend that supports `spec.task`, honoring the preference order.

    Raises `UnsupportedCapability` naming the missing backend rather than silently
    degrading to a method that answers a different question (G4).
    """
```

`settings.xtb_backend_preference = "tblite,ase,cli,crest"`. In a container without the binaries, a
thermochemistry request runs on `ase`; a `conformers` request **fails with a message naming the
missing backend** rather than returning an RDKit ensemble dressed up as a CREST result. Failing loud
here is the whole point: a quietly substituted method is an invalid result in a GxP context.

### 4.4 Seam D — one cache-key derivation

Already shown in §4.2. Two consequences worth stating explicitly:

- **`engine.version()` must include everything that moves a number**: the `tblite` build, the `xtb`
  binary version, the `crest` version, `ase`, and `rdkit` (it steers the seeded embedding). This is
  the existing `engine_version()` policy, widened. Widening invalidates existing entries — correct,
  since those entries did not record the stack that produced them.
- **The `xtb.opt` result is a structure**, so it participates in the *structure* cache as well as the
  calculation cache. Optimizing an already-optimized structure hits the calculation cache; a Hessian
  on that structure hits regardless of who optimized it.

---

## 5. The proposed tool surface

### 5.1 Naming philosophy

Tools are named after the **question**, not the program option. `--ohess` is not a chemistry
question; "what is the free energy of this species" is. Nine tools, each with a docstring the model
reads as its contract (MAF derives the schema from the signature + docstring, so the docstring is
load-bearing product surface, not a comment).

| Tool | Question it answers | Task(s) | Typical cost |
|---|---|---|---|
| `compute_xtb_energy` *(existing, extended)* | "How stable is this?" | `sp` | < 1 s |
| `optimize_geometry` | "What does it actually look like?" | `opt` | 1–20 s |
| `compute_thermochemistry` | "What is G/H/S — and is this even a minimum?" | `opt`+`hess` | 10 s–5 min |
| `compute_electronic_properties` | "HOMO/LUMO, dipole, charges, bond orders?" | `sp` | < 1 s |
| `predict_site_reactivity` | "Which atom reacts?" | `fukui`(+`ipea`) | 1–5 s |
| `generate_conformers` | "Which conformer dominates?" | `conformers` | 1–60 min |
| `compute_reaction_energy` | "What is ΔE/ΔH/ΔG for this reaction?" | composite | 1–30 min |
| `scan_coordinate` | "How high is the barrier about this bond?" | `scan` | 1–20 min |
| `compare_solvent_effects` | "Which solvent stabilises this best?" | `sp`×N | seconds |
| `run_xtb_task` *(privileged escape hatch)* | anything the above cannot express | any | varies |

Ten entries including the escape hatch. Compare with the alternative of one tool per xtb flag
(~30 tools), which would bloat every turn's tool list and force the model to learn xtb's CLI.

### 5.2 The three that carry the most value

**`compute_reaction_energy` — the composite chemists actually want.**

```python
@tool
async def compute_reaction_energy(
    reactants: list[str],
    products: list[str],
    solvent: str | None = None,
    temperature_k: float = 298.15,
    level: Literal["quick", "standard", "thorough"] = "standard",
) -> ReactionEnergyResult:
    """Compute the reaction energy (ΔE) and, above `quick`, ΔH and ΔG for a balanced reaction.

    Each species is treated the same way, so the comparison is internally consistent:
    `quick` optimizes one embedded conformer; `standard` adds a frequency-based thermal
    correction; `thorough` first searches conformers and Boltzmann-averages them. Checks
    that the reaction is atom- and charge-balanced and refuses an unbalanced one rather
    than reporting a meaningless difference.

    Args:
        reactants: SMILES of every reactant, one entry per stoichiometric equivalent.
        products: SMILES of every product, one entry per stoichiometric equivalent.
        solvent: Implicit solvent name, or None for gas phase.
        temperature_k: Temperature for the thermal corrections.
        level: Accuracy/cost trade-off; `thorough` runs as a background job.

    Returns:
        ΔE, ΔH, ΔG in kcal/mol with the method, the per-species breakdown, and the
        stated method uncertainty. Free energies are semiempirical estimates — report
        the uncertainty with the number.
    """
```

This is the only tool that composes the full ladder (conformers → opt → hess → thermo) behind one
call, and the only one whose result is directly usable in a report. Everything else in the ladder is
in service of it. It also mechanically enforces the discipline chemists apply by hand: same method,
same solvation, same conformer treatment on both sides, balance checked.

**`predict_site_reactivity` — the highest question-per-CPU-second ratio.**

```python
@tool
async def predict_site_reactivity(
    smiles: str,
    mode: Literal["electrophilic", "nucleophilic", "radical"] = "electrophilic",
    solvent: str | None = None,
) -> SiteReactivityResult:
    """Rank atoms by their susceptibility to attack, for regioselectivity questions.

    Uses condensed Fukui indices from GFN2-xTB (finite-difference over the N, N-1 and
    N+1 electron systems), reported per atom with the SMILES atom index and element.
    Fukui indices rank sites within one molecule; they are not comparable between
    molecules and do not account for sterics or the specific reagent — treat the
    ranking as a hypothesis to test, not a prediction of yield.
    ...
    """
```

Regioselectivity is one of the most common bench questions and today we have no answer for it at
all. Cost is three single points.

**`compute_thermochemistry` — the one that must refuse to lie.**

```python
class ThermochemistryResult(BaseModel):
    """RRHO thermochemistry at the semiempirical level, with the caveats attached to the data."""

    structure_id: str
    method: str
    is_minimum: bool               # False if any imaginary frequency above the cutoff
    imaginary_frequencies: list[float]
    zero_point_energy_kcal: float
    enthalpy_kcal: float
    entropy_cal_per_mol_k: float
    gibbs_free_energy_kcal: float
    temperature_k: float
    lowest_frequencies_cm: list[float]   # the floppy modes RRHO handles worst
    uncertainty_kcal: float              # from config, method-dependent
    conformer_treatment: Literal["single", "boltzmann"]
```

`is_minimum=False` with a populated `imaginary_frequencies` list is the point of the model: a
Gibbs energy computed at a saddle point is not a Gibbs energy, and the result says so structurally
rather than relying on the agent to notice. `conformer_treatment="single"` is the second built-in
caveat — a single-conformer ΔG is the most common silent error in semiempirical work.

### 5.3 The escape hatch, and its security boundary

```python
@tool
async def run_xtb_task(spec: XtbSpec) -> XtbRawResult:
    """Run an arbitrary xTB task from a fully specified request. Expert use only.

    Prefer the question-shaped tools; reach for this only when none of them expresses
    the calculation (an unusual constraint set, a non-default method/solvation
    combination, a scan the shaped tool cannot describe).
    """
```

**The boundary rule: the argument is a typed `XtbSpec`, never a string.** No raw argv, no
model-authored `$…` control file, no file paths. The reason is concrete rather than theoretical:
xtb's control-file syntax can reference external files and point charges, and content in a SMILES,
an ELN record, or a retrieved document reaches this tool through the model. A typed spec means the
worst a prompt injection achieves is an expensive but well-formed calculation — which the authz gate
and the cost budget already bound.

Gated in `settings.tool_role_gates` to a privileged role; `md` and `conformers` additionally listed
in `entra_expensive_actions` so `authorize_trigger` applies (the same treatment `submit_qm_job` has).

### 5.4 Backward compatibility

`compute_xtb_energy(smiles, charge)` keeps its signature and gains optional `structure`, `solvent`,
and `method` parameters with today's values as defaults. Existing cache entries stay valid as long
as the defaults reproduce the current `_calc_version()`; if `engine.version()` widens (§4.4), the
resulting invalidation is a deliberate, documented one-off, not a surprise. `predict_pka` is
untouched in X1–X5 and gains an optional CREST-backed tautomer/protomer pre-step in X6 as a new,
separately versioned calculator — never as an in-place change to a calibrated method.

---

## 6. Execution tiering: inline vs. durable

### 6.1 The rule

Cost is predicted before running, from a config-driven model — atom count, task, and method:

```python
def estimated_seconds(spec: XtbSpec) -> float:
    """Order-of-magnitude runtime estimate, used only to decide inline vs. durable.

    Deliberately crude and config-tunable: the cost of being wrong is a job that ran
    inline and blocked a turn (bounded by the inline timeout), never a wrong number.
    """
```

- `estimate < settings.xtb_inline_budget_seconds` (default 20) → **run inline**, return the result.
- otherwise → **start a Temporal `XtbJobWorkflow`** and return `{"status": "running", "job_id": …}`.

The tool returns a discriminated union and its docstring tells the model to poll
`get_qm_job_status` (generalized to `get_job_status` in X5, keeping the old name as an alias). One
tool per question, two possible outcomes — rather than a `compute_x` / `submit_x_job` pair per
capability, which doubles the surface and makes the model choose an execution strategy it has no
basis to choose.

### 6.2 Why Temporal and not a thread

Identical to the reasoning behind `QMJobWorkflow`, and it comes with machinery we would otherwise
rebuild: durable retry, worker-restart resumption, the `job_completed` push-back to the session
(F3-T3), harness todo tracking (`mark_awaiting_job`), and the PR-gated knowledge-graph write. A
20-minute CREST run that dies on a pod eviction with no record is exactly what Temporal exists to
prevent.

Queue: reuse `hpc-jobs` (few, heavy workers) initially — xTB jobs are CPU-bound like the QM jobs.
Add `settings.xtb_task_queue` defaulting to it so a deployment can split them later without a code
change.

### 6.3 Rough cost model

Order-of-magnitude only, to be calibrated by a benchmark in X2 rather than trusted from this table.
Assumes a 30-heavy-atom drug-like molecule on one core.

| Task | Scaling | Estimate |
|---|---|---|
| `sp` (GFN2) | O(N²–N³) | 0.05–0.3 s |
| `opt` (ASE/BFGS) | 30–100 gradient calls | 2–30 s |
| `opt` (ANCopt) | 10–30 cycles | 1–5 s |
| `hess` (finite difference) | 6N+1 gradients | 20 s–3 min |
| `hess` (`xtb --ohess`) | one shot | 5–40 s |
| `fukui` | 3 SP | < 1 s |
| `scan` (30 points, relaxed) | 30 constrained opts | 1–15 min |
| `conformers` (CREST iMTD-GC) | metadynamics + many opts | 5–60 min |

The two lines that justify the CLI backend are `opt` and `hess`; the line that justifies Temporal is
`conformers`.

---

## 7. Caching and provenance

Nothing new is invented — the existing store is used more aggressively.

1. **Every task is cached** under `calc_type=f"xtb.{task}"`, versioned by method + engine build.
2. **Structures are cached** under `calc_type="structure"` and content-addressed, so an optimized
   geometry is computed once per (molecule, method, solvation) and then reused by every downstream
   task forever.
3. **Composites cache at both levels.** `compute_reaction_energy` is cached as a whole *and* every
   species' opt/hess is cached individually — so a second reaction sharing a reactant pays only for
   the new species. This is where the compute-once rule stops being a micro-optimization: a
   ten-reaction screen of one scaffold reuses most of its work.
4. **Lineage.** `Structure.origin` records the producing `CalculationKey`; a report can reconstruct
   the full chain SMILES → embed → opt → hess → ΔG, which is what a GxP reviewer asks for.
5. **Ensembles** are cached as an ordered list of `structure_id`s plus energies and Boltzmann
   weights, so a conformer search is never repeated for a different downstream property.

---

## 8. Correctness and honesty guards

The existing guards are kept and extended. Each is a hard failure or a structural field, never
prose in a skill — a caveat the model may or may not repeat is not a control.

| Guard | Where | Behavior |
|---|---|---|
| Charge vs. formal charge | existing, kept | `ValueError` |
| Open shell | existing, **relaxed** | Rejected only where the backend cannot handle it; with an explicit `multiplicity` the CLI/`uhf` path is legitimate, so the check becomes "reject *unspecified* open shell" |
| Embedding failure | existing, kept | `ValueError` after the random-coords retry |
| **Imaginary frequencies** | new | `is_minimum=False` + the frequencies, on every thermochem result |
| **Optimization convergence** | new | `converged: bool` + cycle count; a non-converged geometry is never silently promoted to a `structure_id` usable downstream |
| **Reaction balance** | new | Atom- and charge-balance checked; unbalanced → `ValueError` |
| **Conformer treatment** | new | Every ΔG carries `conformer_treatment`; `"single"` is a stated limitation, not an omission |
| **Method domain** | new | GFN-FF has no electronic structure → electronic-property requests on it are refused, not answered with zeros |
| **Solvation domain** | new | Solvent names validated per model against the parametrized set; an unknown solvent is an error, not a silent gas-phase run |
| **Uncertainty attached** | new | Every energetic result carries `uncertainty_kcal` from config, as `PkaResult.uncertainty` already does |
| **Absolute vs. relative** | new | Result models expose relative quantities where the physics is relative; absolute totals stay available but are documented as reference-only |

The last row deserves emphasis: today's skill *asks* the agent not to present an absolute Hartree
value as an answer. Making the model return `relative_energy_kcal` against a stated reference turns
that request into a property of the data.

---

## 9. Security, authorization, audit

- **Every tool is `@tool`-registered**, so the GxP audit middleware and `enforce_tool_authz` wrap it
  with no per-tool wiring. No change to the safety rubric.
- **Subprocess hardening** for the `cli`/`crest` backends, since this is the first place ChemClaw
  shells out:
  - fixed argv list, `shell=False`, absolute binary path from config;
  - a fresh scratch directory per run, deleted after; `--namespace` to keep xtb's scratch files
    contained; `cwd` set to the scratch dir so nothing is written next to application code;
  - wall-clock timeout (`settings.xtb_cli_timeout_seconds`) and an output-size cap;
  - no network needed → runs under the existing rootless container user with no additional grants;
  - structure files written by us from the typed `Structure`, never from model-authored text;
  - stderr captured and surfaced as a typed `EngineError`, not swallowed.
- **Authorization**: `run_xtb_task` gated by role in `tool_role_gates`; `conformers` and `md` added
  to `entra_expensive_actions` so `authorize_trigger` gates them exactly as `submit_qm_job` is.
- **Injection boundary**: restated because it is the one genuinely new risk — the escape hatch takes
  a typed spec, never a control file or argv. Add a test asserting no engine ever interpolates a
  model-supplied string into a command line.

---

## 10. Skills (the judgment layer)

Tools stay mechanical; three skills carry the decisions.

1. **`calculation-selection`** *(existing, extended)* — the router: which tool for which question,
   and the escalation boundary to DFT. Add the new tools to its front-matter `tools:` list (the
   `make skill-validate` check in D-081 then enforces that the names stay real).
2. **`xtb-workflow-design`** *(new)* — the ladder as judgment:
   - when a single point is enough vs. when a geometry must be optimized first;
   - when a conformer search is mandatory (flexible molecules, any ΔG for a chain with rotatable
     bonds) vs. wasteful (rigid, few rotatable bonds);
   - method choice: GFN-FF for pre-screening large systems, GFN0 to rescue bad geometries, GFN2 as
     the default, GFN1 where GFN2 is known to struggle;
   - solvation choice: ALPB as default, CPCM-X when the solvation free energy itself matters;
   - how to read `is_minimum=False`, a non-converged optimization, or a `conformer_treatment` of
     `"single"`;
   - the honest error bars, and the point at which the answer is "this needs DFT".
3. **`reactivity-descriptors`** *(new)* — how to read Fukui indices, HOMO/LUMO gaps, electrophilicity
   indices and charges without over-claiming: they rank sites within a molecule, they are hypotheses
   to test, and they say nothing about sterics or the specific reagent.

`qm-job-submission` gains one paragraph drawing the boundary: try the xTB ladder first; escalate to
DFT only when the xTB answer is inside the error bar of the decision being made.

---

## 11. Configuration

All ENV-overridable under `CHEMCLAW_`, in the existing `FastCalculators` settings group (or a new
`XtbSettings` group if it outgrows it).

| Setting | Default | Purpose |
|---|---|---|
| `xtb_backend_preference` | `"tblite,ase,cli,crest"` | Ordered backend preference |
| `xtb_binary_path` | `"xtb"` | Absolute path in the container image |
| `crest_binary_path` | `"crest"` | ditto |
| `xtb_cli_timeout_seconds` | `1800` | Subprocess wall clock |
| `xtb_inline_budget_seconds` | `20` | Inline vs. durable routing threshold |
| `xtb_task_queue` | `= hpc_task_queue` | Durable xTB jobs' queue |
| `xtb_default_solvation` | `"alpb"` | Default implicit solvation model |
| `xtb_default_solvent` | `None` | Gas phase unless asked |
| `xtb_opt_level` | `"normal"` | ANCopt/ASE convergence level |
| `xtb_geometry_precision` | `1e-4` | Coordinate rounding before hashing |
| `xtb_imaginary_frequency_cutoff_cm` | `-50.0` | Below this counts as a real imaginary mode |
| `xtb_conformer_energy_window_kcal` | `6.0` | CREST retention window |
| `xtb_max_conformers` | `50` | Cap on downstream per-conformer work |
| `xtb_reaction_energy_uncertainty_kcal` | `3.0` | Reported uncertainty for ΔE/ΔH/ΔG |
| `xtb_thermo_uncertainty_kcal` | `2.0` | Reported uncertainty for thermochemistry |
| `xtb_max_atoms_gfn2` | `300` | Above this, require GFN-FF or refuse |

---

## 12. Phased rollout

Each phase is independently shippable and independently valuable. `make lint type test` green plus
the acceptance check is the definition of done, per `CLAUDE.md`.

### X1 — Foundations: `Structure`, `StructureRef`, `XtbSpec`, one cache key

*No new dependency, no new science, no new tool.* Build the four seams and port
`calc/xtb.py` onto them behind its existing public API.

**Acceptance:** `compute_xtb_energy` behaves identically (existing tests unchanged and green);
optimizing nothing, a SMILES and its `structure_id` produce the same cache key; a structure
round-trips through the store.
**Risk:** low. **Payoff:** everything else becomes small.

### X2 — Free properties from the SCF we already run

`compute_electronic_properties` and `predict_site_reactivity` (Fukui by finite difference over three
`tblite` single points).

**Acceptance:** HOMO/LUMO gap of benzene within a documented tolerance of the literature GFN2 value;
Fukui ranking puts the *ortho/para* positions of phenol above *meta* for electrophilic attack.
**Deps added:** none. **Risk:** low. **Payoff:** two real capabilities for near-zero cost — the
best ratio in this proposal, and the phase to start with if only one ships.

### X3 — Geometries and free energies without a binary

Add `ase`. Build the `ase` backend: `optimize_geometry`, `compute_thermochemistry`, `scan_coordinate`.

**Acceptance:** optimizing ethanol converges and lowers the energy; its Hessian has zero imaginary
frequencies while a deliberately distorted geometry reports one; the ΔG of a known conformational
equilibrium reproduces its literature GFN2 value within the stated uncertainty.
**Deps added:** `ase`. **Risk:** medium — finite-difference Hessians are slow for large molecules,
which is what makes the routing rule in §6 necessary in this phase rather than later.
**Payoff:** the largest single jump in what the agent can answer.

### X4 — The composite

`compute_reaction_energy` and `compare_solvent_effects`, both composed from X1–X3.

**Acceptance:** a balanced esterification returns ΔE/ΔH/ΔG with a per-species breakdown; an
unbalanced reaction is rejected; a second reaction sharing a reactant demonstrably reuses the cached
species (assert cache hits, not just wall clock).
**Deps added:** none. **Risk:** low. **Payoff:** the first tool whose output goes into a report
unedited. Ties directly into the existing solvent-selection eval cases.

### X5 — The `xtb` binary

CLI backend, subprocess hardening, `--json` parsing, GFN-FF/GFN0, ANCopt, `--ohess`, `--vfukui`,
`--vipea`/`--vomega`, CPCM-X, `--bhess`. Temporal `XtbJobWorkflow` and the inline/durable routing
rule. Generalize `get_qm_job_status` → `get_job_status`.

**Acceptance:** the CLI backend reproduces the `tblite` single-point energy for a reference molecule
within a documented tolerance; a 200-atom system runs under GFN-FF; a job over the inline budget
returns a job id and pushes back a completion event; the injection test (§9) passes.
**Deps added:** `xtb` in the container image (LGPL-3.0). **Risk:** medium — image size, and the
first subprocess in the codebase. **Payoff:** the capabilities that have no Python equivalent.

### X6 — CREST ensembles

`generate_conformers`; ensembles as first-class cached objects; Boltzmann weighting;
`level="thorough"` in `compute_reaction_energy` wired to real ensembles. Optionally, a
tautomer/protomer-aware pKa v2 as a **separately versioned** calculator alongside today's.

**Acceptance:** butane's conformer search finds anti and gauche with sensible populations; a
Boltzmann-averaged ΔG differs from the single-conformer value on a flexible substrate (i.e. the
feature demonstrably matters); pKa v2 does not change any pKa v1 cached value.
**Deps added:** `crest` in the image (**GPL-3.0 — see §14**). **Risk:** medium-high — runtime is
minutes to hours, so this phase is entirely dependent on X5's durable routing.

### X7 — The expert seam

`run_xtb_task` with the typed spec, role gate, constraints and scan specs fully expressed,
metadynamics.

**Acceptance:** an authorized role can run a constrained optimization the shaped tools cannot
express; an unauthorized role is denied and the denial is in the audit trail.
**Risk:** low, given §9. **Payoff:** the pressure valve that keeps the shaped tools from accreting
options — build it last, once real usage has shown which options are actually missing.

---

## 13. Testing and evaluation

- **Real calculations in unit tests**, following the existing `tests/test_xtb.py` precedent: small
  molecules, real GFN2, asserted against literature or internally consistent relationships. Not
  mocks — `CLAUDE.md` is explicit that tests prove behavior.
- **Physics invariants** as tests, which are stronger than golden numbers: optimization never raises
  the energy; a Hessian at a converged minimum has no imaginary modes; ΔE of a reaction equals the
  negative of its reverse; a cached result is bit-identical to its recomputation.
- **Cache tests**: same molecule via SMILES and via `structure_id` → one computation; a bumped
  `engine.version()` → a miss.
- **Backend equivalence**: `tblite` and `cli` single points agree within a documented tolerance —
  this is what makes backend substitution defensible at all.
- **Determinism**: same input, same seed, same answer across processes (the D-011 property).
- **Eval cases** (`evals/cases/`) for the new questions: a regioselectivity case with a known
  outcome, a tautomer-ranking case, a solvent-selection case that connects to the existing
  `pharma-solvent-heavy` and `green-esterification` cases.
- **Skill validation**: `make skill-validate` already checks declared tool names against the
  registry (D-081) — new skills get that for free.

---

## 14. Risks and open questions

**For the user / the project owner:**

1. **Licensing.** `xtb` is LGPL-3.0; `crest` is GPL-3.0. We invoke both as separate processes, which
   is the conventional reading of an arm's-length boundary, but shipping the binaries inside a
   distributed container image is a distribution event and should be confirmed with legal before X5
   and X6. This is the one item that can block a phase for non-technical reasons — worth raising
   early. `tblite` (LGPL-3.0) and `ase` (LGPL-2.1) are already or would be Python dependencies with
   the same consideration at lower stakes.
2. **Image size and the OpenShift build.** `xtb` + `crest` add roughly 100–200 MB. Acceptable, or do
   they belong in a separate compute image that only the `hpc-jobs` worker pulls? The latter is
   cleaner and costs one more image in the build.
3. **Accuracy expectations.** Are semiempirical free energies (±2–3 kcal/mol) decision-useful for
   the intended users, or is the real requirement DFT — in which case the priority ordering here
   changes and the deferred HPC path moves up?
4. **GxP method validation.** If xTB results can reach a regulated report, does the method version
   need formal qualification? That would argue for pinning binary versions in config and recording
   them in every result, which the cache key already does — but the process question is not
   technical.

**Technical risks:**

| Risk | Mitigation |
|---|---|
| Finite-difference Hessians too slow for real molecules | Cost model + durable routing (X3/X5); prefer `--ohess` once the binary exists |
| Conformer searches dominate runtime | Cap by `xtb_max_conformers` and the energy window; `level="thorough"` is opt-in and durable |
| Semiempirical results over-trusted downstream | Uncertainty in every result model; `is_minimum` / `conformer_treatment` as structural fields; skills carry the escalation boundary |
| Backend results diverge | Backend-equivalence tests; `engine.version()` in every cache key so results are never mixed across backends silently |
| Cache invalidation churn on upgrades | Accepted and documented: a widened version string is a correctness feature, not a regression |
| Tool-surface bloat | Ten tools, question-shaped, with the escape hatch built last |

---

## 15. What we deliberately do not build

Recording these keeps the scope honest and gives the next reader the reasons, per `DEFERRED.md`
practice.

- **One tool per xtb flag.** ~30 tools the model must learn; the shaped surface plus the typed escape
  hatch covers the same space.
- **A raw CLI / raw control-file passthrough.** The injection boundary in §9.
- **Transition-state search (NEB, `--path`).** Genuinely useful, but TS work needs judgment and
  verification the agent cannot yet supply; a relaxed scan (X3) gives a barrier *estimate* honestly
  labeled as one. Revisit once the ladder is in daily use.
- **MD as anything but an expert capability.** Long, stochastic, and rarely the answer to a bench
  question; available through X7 only.
- **ONIOM / QM-QM′.** No current use case.
- **xtb-IFF docking.** A different problem domain.
- **CENSO / DFT refinement of ensembles.** The escalation path is the existing `submit_qm_job`.
- **Replacing `predict_pka` v1.** It is calibrated through a specific code path; a CREST-aware v2
  ships alongside it as a separate calculator version, never as an in-place edit.

---

## 16. Recommendation

Approve **X1 + X2** now: no new dependencies, low risk, and they convert work we already do into two
capabilities the agent lacks entirely. Treat **X3** as the main event and schedule it next — `ase`
is the cheapest possible route to geometries and free energies. Gate **X5/X6** on the licensing and
image questions in §14, since those are decisions rather than engineering.

If only one phase ever ships, it should be **X2**: three single points, no new dependency, and it
answers the regioselectivity question that currently has no answer at all.
